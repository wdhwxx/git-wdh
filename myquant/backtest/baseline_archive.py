# 文件名：myquant/backtest/baseline_archive.py
"""
baseline_archive.py - 基线净值档案（行为回归对拍基准）
================================================================
为回测真实性门禁（v2.2 §4.8）建立"策略 × 其支持的引擎 × 当前配置"
的基线净值档案，供后续每张 M3/M6 任务卡完成后做行为回归对拍：
重跑同配置回测，净值序列 max abs diff ≤ 0.5% 且成交笔数差 ≤ 5% 视为
无回归；超阈值即回归信号（未在 §5.5 映射表登记的字段变化 = 回归事故）。

设计边界（重要，勿违反）:
  - 本模块只消费 BacktestResult / BacktestConfig / 策略实例，不 import
    L3/L4 业务模块（引擎/撮合/费用），保持 L 层纯业务；
  - 指纹口径：config_hash 剔除运行元数据（run_id/时间戳/result 文件名），
    否则每次重跑必 diff（R-19 实测证据：mixed 两遍仅 started_at/
    finished_at/run_id 三处差异）；
  - event 引擎路径需要撮合执行器工厂（G1 EX-09A），由本模块按
    engine.py 自测 Demo 同构装配 SimBroker；
  - 输出目录：outputs/realism_baseline/{strategy}_{engine}_{config_hash}.json。

Usage:
    from myquant.backtest.baseline_archive import BaselineArchiver
    archiver = BaselineArchiver(project_root=..., config_path=...)
    path = archiver.archive(strategy_name="rsrs", engine_mode="mixed")
    ok, report = archiver.verify(strategy_name="rsrs", engine_mode="mixed")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("myquant.backtest.baseline_archive")

# ── 常量（禁魔法字符串）──────────────────────────────────────────────
DEFAULT_CONFIG_PATH = "config/backtest_config.yaml"
DEFAULT_OUTPUT_REL = "outputs/realism_baseline"
FORMAT_VERSION = "1.0"

# 对拍阈值（v2.2 §4.8）
EQUITY_MAX_ABS_DIFF = 0.005  # 净值序列 max abs diff ≤ 0.5%
TRADE_COUNT_DIFF_RATIO = 0.05  # 成交笔数差 ≤ 5%

# 运行元数据键（config_hash / 指纹必须剔除，R-19 实测证据）
RUN_META_KEYS = ("run_id", "started_at", "finished_at", "status", "error_message")

# 净值序列指纹字段（EquityPoint.to_dict 的业务字段，剔除 extra）
EQUITY_FINGERPRINT_FIELDS = ("date", "equity", "cash", "position_value", "drawdown", "benchmark_equity")

# 成交指纹业务字段（TradeRecord.to_dict 业务字段，剔除运行期随机主键）
# trade_id 为 uuid4 运行期随机生成（models/trade.py:485），每次 run 必不同——
# 与 run_id/时间戳同属不稳定字段，剔除（R-19 实测：6695 笔全部 trade_id 不同，
# 其余 14 业务字段逐字段一致）。timestamp 为 bar 时间戳属业务数据，保留。
TRADE_FINGERPRINT_FIELDS = (
    "symbol", "side", "quantity", "price", "timestamp",
    "commission", "slippage", "pnl", "pnl_pct", "holding_days",
    "strategy_id", "signal_source", "entry_reason", "exit_reason",
)


class BaselineArchiver:
    """基线净值档案的构建 / 落盘 / 读取 / 对拍。

    Attributes:
        project_root: 项目根目录（用于解析配置与输出路径）。
        config_path: 回测配置文件路径（YAML）。
        out_dir: 基线档案输出目录。
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        config_path: Optional[Path] = None,
        out_dir: Optional[Path] = None,
    ) -> None:
        """初始化基线档案器。

        Args:
            project_root: 项目根目录；None 时按 __file__ 向上探测。
            config_path: 回测配置文件路径；None 时用 DEFAULT_CONFIG_PATH。
            out_dir: 输出目录；None 时用 project_root/DEFAULT_OUTPUT_REL。

        Raises:
            FileNotFoundError: 配置文件不存在。
        """
        self.project_root = project_root or self._detect_project_root()
        cfg = config_path or (self.project_root / DEFAULT_CONFIG_PATH)
        if not Path(cfg).is_absolute():
            cfg = self.project_root / cfg
        if not Path(cfg).exists():
            raise FileNotFoundError(f"回测配置文件不存在: {cfg}")
        self.config_path = Path(cfg)
        self.out_dir = Path(out_dir) if out_dir else (self.project_root / DEFAULT_OUTPUT_REL)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        logger.info("BaselineArchiver 初始化: config=%s, out=%s", self.config_path, self.out_dir)

    # ── 路径探测 ──────────────────────────────────────────────────────

    @staticmethod
    def _detect_project_root() -> Path:
        """向上探测项目根目录（查找 pyproject.toml）。

        Returns:
            项目根目录绝对路径。

        Raises:
            RuntimeError: 向上 20 层未找到 pyproject.toml。
        """
        current = Path(__file__).resolve().parent
        for _ in range(20):
            if (current / "pyproject.toml").exists():
                return current
            if current.parent == current:
                break
            current = current.parent
        raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")

    # ── 指纹计算（纯函数）────────────────────────────────────────────

    @staticmethod
    def _canonical_json(obj: Any) -> str:
        """将对象序列化为规范化 JSON（键排序、无空白），保证哈希口径稳定。

        Args:
            obj: 任意可 JSON 序列化对象。

        Returns:
            规范化 JSON 字符串。

        Raises:
            TypeError: 对象含不可序列化成员（防御）。
        """
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _sha256(text: str) -> str:
        """计算 UTF-8 字符串的 sha256 十六进制摘要。

        Args:
            text: 待哈希字符串。

        Returns:
            64 位十六进制哈希。
        """
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def compute_config_hash(self, config: Any) -> str:
        """计算回测配置指纹（剔除运行元数据）。

        口径：BacktestConfig.to_dict() 全部业务字段（strategy_name/
        symbols/start_date/end_date/frequency/initial_capital/benchmark/
        risk_free_rate/params），params 经 sort_keys 规范化 → sha256。
        运行元数据（run_id/时间戳等）不在 to_dict 中，天然剔除。

        Args:
            config: BacktestConfig 实例。

        Returns:
            64 位十六进制 config_hash。

        Raises:
            AttributeError: config 无 to_dict 方法（防御）。
        """
        if not hasattr(config, "to_dict"):
            raise AttributeError(f"config 必须实现 to_dict()，收到: {type(config).__name__}")
        cfg_dict = config.to_dict()
        return self._sha256(self._canonical_json(cfg_dict))

    def compute_equity_hash(self, result: Any) -> str:
        """计算净值曲线指纹（逐点业务字段序列）。

        口径：equity_curve 逐点提取 EQUITY_FINGERPRINT_FIELDS（剔除
        timestamp 原始浮点与 extra），规范化后 sha256。基准缺失时
        benchmark_equity 恒为 1.0（R-19 实测：000300.SH 缺失），仍参与
        指纹——该缺口属已知边界，见决策记录第 5 部分。

        Args:
            result: BacktestResult 实例（含 equity_curve 列表）。

        Returns:
            64 位十六进制哈希；空净值曲线返回 "empty_equity"。

        Raises:
            AttributeError: result 无 equity_curve 属性（防御）。
        """
        curve = getattr(result, "equity_curve", None)
        if curve is None:
            raise AttributeError(f"result 必须含 equity_curve，收到: {type(result).__name__}")
        if not curve:
            return "empty_equity"
        rows: List[Dict[str, Any]] = []
        for ep in curve:
            d = getattr(ep, "to_dict", lambda: {})()
            rows.append({k: d.get(k) for k in EQUITY_FINGERPRINT_FIELDS})
        return self._sha256(self._canonical_json(rows))

    def compute_trades_fingerprint(self, result: Any) -> str:
        """计算成交明细指纹（业务字段逐笔序列）。

        口径：trades 逐笔提取 TRADE_FINGERPRINT_FIELDS（TradeRecord.to_dict
        的 14 个业务字段，timestamp 为 bar 时间戳属业务数据保留）。
        剔除 trade_id：uuid4 运行期随机生成（models/trade.py:485），
        每次 run 必不同（R-19 实测 6695 笔全部不同），与 run_id 同类
        不稳定字段。extra 亦剔除。规范化后 sha256。

        Args:
            result: BacktestResult 实例（含 trades 列表）。

        Returns:
            64 位十六进制哈希；空成交返回 "empty_trades"。

        Raises:
            AttributeError: result 无 trades 属性（防御）。
        """
        trades = getattr(result, "trades", None)
        if trades is None:
            raise AttributeError(f"result 必须含 trades，收到: {type(result).__name__}")
        if not trades:
            return "empty_trades"
        rows: List[Dict[str, Any]] = []
        for t in trades:
            d = getattr(t, "to_dict", lambda: {})()
            rows.append({k: d.get(k) for k in TRADE_FINGERPRINT_FIELDS})
        return self._sha256(self._canonical_json(rows))

    # ── 档案构建与落盘 ───────────────────────────────────────────────

    def build_baseline(
        self,
        result: Any,
        strategy_name: str,
        engine_mode: str,
        config: Any,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构建基线档案 payload（§4.8 格式）。

        档案含：format_version / strategy / engine / config_hash /
        净值曲线哈希 / 关键指标（metrics.to_dict() 完整保留）/
        成交明细指纹 / 摘要（成交笔数、净值点数、最终净值）。
        运行元数据（run_id/时间戳）不入档——对拍口径只比较业务字段。

        Args:
            result: BacktestResult 实例。
            strategy_name: 策略名（如 rsrs）。
            engine_mode: 引擎模式（event/mixed/vectorized）。
            config: BacktestConfig 实例。
            extra_meta: 附加元数据（如 config 文件路径），可选。

        Returns:
            基线档案字典。

        Raises:
            AttributeError: result 缺 metrics / config 缺 to_dict（防御）。
        """
        metrics_dict = getattr(result.metrics, "to_dict", lambda: {})()
        baseline: Dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "strategy": strategy_name,
            "engine": engine_mode,
            "config_hash": self.compute_config_hash(config),
            "equity_curve_hash": self.compute_equity_hash(result),
            "trades_fingerprint": self.compute_trades_fingerprint(result),
            "metrics": metrics_dict,
            "summary": {
                "total_trades": len(getattr(result, "trades", [])),
                "equity_points": len(getattr(result, "equity_curve", [])),
                "final_equity": float(getattr(result, "final_equity", 1.0)),
            },
        }
        if extra_meta:
            baseline["extra_meta"] = extra_meta
        return baseline

    def baseline_filename(self, strategy_name: str, engine_mode: str, config_hash: str) -> str:
        """生成基线档案文件名（§4.8 命名约定）。

        Args:
            strategy_name: 策略名。
            engine_mode: 引擎模式。
            config_hash: 配置指纹。

        Returns:
            形如 {strategy}_{engine}_{config_hash}.json 的文件名。
        """
        return f"{strategy_name}_{engine_mode}_{config_hash}.json"

    def write_baseline(self, baseline: Dict[str, Any], strategy_name: str, engine_mode: str) -> Path:
        """将基线档案落盘到 out_dir（UTF-8，无 BOM）。

        Args:
            baseline: build_baseline 返回的档案字典。
            strategy_name: 策略名（用于文件名）。
            engine_mode: 引擎模式（用于文件名）。

        Returns:
            落盘文件绝对路径。

        Raises:
            KeyError: baseline 缺 config_hash（防御）。
        """
        if "config_hash" not in baseline:
            raise KeyError("baseline 缺 config_hash，无法生成文件名")
        path = self.out_dir / self.baseline_filename(strategy_name, engine_mode, baseline["config_hash"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False, indent=2)
        logger.info("基线档案已落盘: %s", path)
        return path

    def load_baseline(self, path: Path) -> Dict[str, Any]:
        """读取基线档案 JSON。

        Args:
            path: 档案文件路径。

        Returns:
            档案字典。

        Raises:
            FileNotFoundError: 文件不存在。
            json.JSONDecodeError: 内容非法 JSON（防御，向上抛）。
        """
        if not Path(path).exists():
            raise FileNotFoundError(f"基线档案不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ── 对拍 ─────────────────────────────────────────────────────────

    @staticmethod
    def _equity_series(result: Any) -> List[float]:
        """提取净值序列（对拍用，只取 equity 数值列）。

        Args:
            result: BacktestResult 实例。

        Returns:
            净值浮点序列。
        """
        return [float(getattr(ep, "equity", 1.0)) for ep in getattr(result, "equity_curve", [])]

    @staticmethod
    def _max_abs_diff(a: List[float], b: List[float]) -> float:
        """计算两条等长序列的最大绝对差（不等长时按对齐前缀）。

        Args:
            a: 序列 A。
            b: 序列 B。

        Returns:
            最大绝对差；任一为空返回 0.0。
        """
        if not a or not b:
            return 0.0
        n = min(len(a), len(b))
        return max(abs(x - y) for x, y in zip(a[:n], b[:n]))

    def verify_reproducible(
        self,
        archived: Dict[str, Any],
        fresh_result: Any,
        config: Any,
    ) -> Tuple[bool, Dict[str, Any]]:
        """基线档案 vs 重跑结果对拍（v2.2 §4.8 验收判据）。

        判定：
          - 净值序列 max abs diff ≤ 0.5%；
          - 成交笔数差 ≤ 5%（以基线为基准）；
          - config_hash / equity 哈希 / trades 指纹一致（严格位级）。

        超阈即回归信号（False）。阈值常量 EQUITY_MAX_ABS_DIFF /
        TRADE_COUNT_DIFF_RATIO 为唯一权威，不在此处写死数字。

        Args:
            archived: 已存档的基线档案（load_baseline 输出）。
            fresh_result: 重跑得到的 BacktestResult。
            config: 重跑使用的 BacktestConfig。

        Returns:
            (是否通过, 对拍报告字典)。
        """
        fresh_config_hash = self.compute_config_hash(config)
        fresh_equity_hash = self.compute_equity_hash(fresh_result)
        fresh_trades_fp = self.compute_trades_fingerprint(fresh_result)

        equity_a = archived.get("equity_curve_hash", "")
        equity_b = fresh_equity_hash
        trades_a = archived.get("trades_fingerprint", "")
        trades_b = fresh_trades_fp
        config_a = archived.get("config_hash", "")
        config_b = fresh_config_hash

        # 净值序列数值对拍（补充位级哈希——哈希碰撞防御）
        archived_summary = archived.get("summary", {})
        archived_points = int(archived_summary.get("equity_points", 0))
        archived_final = float(archived_summary.get("final_equity", 1.0))
        fresh_points = len(getattr(fresh_result, "equity_curve", []))
        fresh_final = float(getattr(fresh_result, "final_equity", 1.0))

        eq_max_diff = abs(archived_final - fresh_final)  # 最终净值差（曲线逐点已在哈希口径内）
        trade_diff = abs(
            int(archived_summary.get("total_trades", 0)) - len(getattr(fresh_result, "trades", []))
        )
        trade_ratio = trade_diff / max(1, int(archived_summary.get("total_trades", 0)))

        passed = (
            config_a == config_b
            and equity_a == equity_b
            and trades_a == trades_b
            and eq_max_diff <= EQUITY_MAX_ABS_DIFF
            and trade_ratio <= TRADE_COUNT_DIFF_RATIO
            and archived_points == fresh_points
        )

        report: Dict[str, Any] = {
            "passed": passed,
            "config_hash_match": config_a == config_b,
            "equity_hash_match": equity_a == equity_b,
            "trades_fingerprint_match": trades_a == trades_b,
            "equity_points": {"archived": archived_points, "fresh": fresh_points},
            "final_equity": {"archived": archived_final, "fresh": fresh_final},
            "final_equity_max_abs_diff": round(eq_max_diff, 10),
            "total_trades": {
                "archived": int(archived_summary.get("total_trades", 0)),
                "fresh": len(getattr(fresh_result, "trades", [])),
            },
            "trade_count_diff_ratio": round(trade_ratio, 6),
            "thresholds": {
                "equity_max_abs_diff": EQUITY_MAX_ABS_DIFF,
                "trade_count_diff_ratio": TRADE_COUNT_DIFF_RATIO,
            },
        }
        return passed, report

    # ── 运行装配（供薄入口复用官方装配路径）────────────────────────────

    def run_backtest_once(
        self,
        strategy_name: str,
        engine_mode: str,
        symbols: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        broker_factory: Optional[Any] = None,
    ) -> Tuple[Any, Any, Dict[str, Any]]:
        """以官方装配跑一次回测（复用 run_回测入口.py 装配链）。

        装配链：ConfigLoader 读 YAML → build_backtest_config →
        discover_strategies + create_strategy → HistoricalDataProvider →
        lookahead_guard reset + leakage_gate 配置注入 → BacktestScheduler
        (mode) → run()。event 引擎需撮合执行器工厂（G1 EX-09A：engine.py
        无工厂即 ValueError）；装配责任在调用方（入口脚本），本模块只
        透传 broker_factory，不直接 import L4（分层 R-02）。

        Args:
            strategy_name: 策略名（注册表内）。
            engine_mode: 引擎模式（event/mixed/vectorized/auto）。
            symbols: 标的列表覆盖（None = 用配置文件）。
            start_date: 起始日期覆盖（None = 用配置文件）。
            end_date: 结束日期覆盖（None = 用配置文件）。
            broker_factory: 撮合执行器工厂 factory(config) -> broker
                （None = 不装配，依赖外部已注册工厂或引擎默认）。

        Returns:
            (result, strategy, 环境字典)。环境字典含 config 等供
            build_baseline / verify_reproducible 使用。

        Raises:
            ValueError: 策略不存在 / 引擎不支持 / 回测失败。
        """
        from myquant.core.config_loader import ConfigLoader
        from myquant.backtest.config_builder import build_backtest_config
        from myquant.strategies.registry import discover_strategies, create_strategy
        from myquant.backtest.data_loader import HistoricalDataProvider
        from myquant.core.lookahead_guard import get_lookahead_guard
        from myquant.leakage_gate import GateConfig as _LG_GateConfig
        from myquant.leakage_gate import set_config as _lg_set_config
        from myquant.backtest.engine import BacktestScheduler, register_broker_factory
        import argparse as _argparse

        # 1. 加载配置（与 run_回测入口.py 同源）
        loader = ConfigLoader(base_dir=self.project_root)
        config_dict = loader.load(str(self.config_path))

        # 2. 命令行覆盖（与 CLI --symbols/--start/--end 语义一致）
        cli_args = _argparse.Namespace(
            symbols=",".join(symbols) if symbols else None,
            start=start_date,
            end=end_date,
            capital=None,
            stock_pool=None,
            config=None,
            output=None,
            strategy=None,
        )
        bt_config = build_backtest_config(config_dict, cli_args, self.project_root)

        # 3. 策略实例化
        discover_strategies()
        strategy = create_strategy(bt_config.strategy_name, **bt_config.params)

        # 4. 数据提供器（与入口同源）
        data_cfg = config_dict.get("data", {})
        data_dir = data_cfg.get("data_dir", "./datasets/processed")
        if not Path(data_dir).is_absolute():
            data_dir = str(self.project_root / data_dir)
        data_provider = HistoricalDataProvider(
            processed_dir=data_dir,
            default_adjust=data_cfg.get("adjust", "raw"),
            data_format=data_cfg.get("data_format", "parquet"),
        )

        # 5. 防未来函数校验器 + 泄漏门禁配置注入（与入口同源）
        guard = get_lookahead_guard()
        guard.reset()
        lg_cfg = config_dict.get("lookahead_guard", {}) or {}
        if "enabled" in lg_cfg:
            guard.enabled = bool(lg_cfg["enabled"])
        if "strict_mode" in lg_cfg:
            guard.strict = bool(lg_cfg["strict_mode"])
        _lg_cfg = config_dict.get("leakage_gate", {}) or {}
        if _lg_cfg:
            _lg_set_config(_LG_GateConfig(
                enabled=bool(_lg_cfg.get("enabled", True)),
                audit_violations=bool(_lg_cfg.get("audit_violations", True)),
            ))

        # 6. 撮合执行器工厂（装配责任在调用方；None 则不装配，
        #    依赖外部已注册工厂——G1 EX-09A 语义）
        if broker_factory is not None:
            register_broker_factory(broker_factory)

        # 7. 创建引擎并运行
        scheduler = BacktestScheduler(mode=engine_mode)
        engine = scheduler.create_engine(
            config=bt_config,
            strategy=strategy,
            data_provider=data_provider,
        )
        result = engine.run()
        if not getattr(result, "is_success", False):
            raise ValueError(
                f"回测失败: {getattr(result, 'error_message', '未知错误')} "
                f"(strategy={strategy_name}, engine={engine_mode})"
            )

        # 8. 防未来函数强制校验（与入口同源，不可跳过）
        try:
            guard.assert_pass()
        except Exception as e:
            raise ValueError(f"防未来函数校验未通过，回测结果不可信: {e}") from e

        env = {
            "config": bt_config,
            "strategy": strategy,
            "engine_mode": scheduler.selected_mode or engine_mode,
        }
        return result, strategy, env

    def archive(
        self,
        strategy_name: str,
        engine_mode: str,
        symbols: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        broker_factory: Optional[Any] = None,
    ) -> Path:
        """跑一次回测并建立基线档案。

        Args:
            strategy_name: 策略名。
            engine_mode: 引擎模式。
            symbols: 标的覆盖（可选）。
            start_date: 起始日期覆盖（可选）。
            end_date: 结束日期覆盖（可选）。
            broker_factory: 撮合执行器工厂（装配责任在调用方，可选）。

        Returns:
            基线档案文件路径。

        Raises:
            ValueError: 回测失败（含策略不支持引擎）。
        """
        result, strategy, env = self.run_backtest_once(
            strategy_name=strategy_name,
            engine_mode=engine_mode,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            broker_factory=broker_factory,
        )
        resolved_mode = str(env["engine_mode"])
        baseline = self.build_baseline(
            result=result,
            strategy_name=strategy_name,
            engine_mode=resolved_mode,
            config=env["config"],
            extra_meta={"config_file": str(self.config_path)},
        )
        return self.write_baseline(baseline, strategy_name, resolved_mode)

    def verify(
        self,
        strategy_name: str,
        engine_mode: str,
        symbols: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        broker_factory: Optional[Any] = None,
    ) -> Tuple[bool, Dict[str, Any], Optional[Path]]:
        """重跑同配置回测并与已存档基线对拍（§4.8）。

        Args:
            strategy_name: 策略名。
            engine_mode: 引擎模式。
            symbols: 标的覆盖（可选）。
            start_date: 起始日期覆盖（可选）。
            end_date: 结束日期覆盖（可选）。
            broker_factory: 撮合执行器工厂（装配责任在调用方，可选）。

        Returns:
            (是否通过, 对拍报告, 基线文件路径或 None)。

        Raises:
            FileNotFoundError: 基线档案不存在（需先 archive）。
            ValueError: 回测失败。
        """
        # 先按 config_hash 定位基线文件（当前配置）
        result, strategy, env = self.run_backtest_once(
            strategy_name=strategy_name,
            engine_mode=engine_mode,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            broker_factory=broker_factory,
        )
        resolved_mode = str(env["engine_mode"])
        config_hash = self.compute_config_hash(env["config"])
        baseline_path = self.out_dir / self.baseline_filename(strategy_name, resolved_mode, config_hash)
        if not baseline_path.exists():
            raise FileNotFoundError(
                f"基线档案不存在（需先 archive）: {baseline_path}"
            )
        archived = self.load_baseline(baseline_path)
        passed, report = self.verify_reproducible(archived, result, env["config"])
        return passed, report, baseline_path


# ── 自测 Demo（py 规则 5：可执行文件必须含 __main__ 入口）─────────────

def _run_self_test() -> int:
    """运行基线档案器自测（不落盘、不跑真实回测）。

    Returns:
        通过的测试项数量。
    """
    from myquant.models.backtest import BacktestConfig, BacktestResult, EquityPoint, TradeRecord

    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 60)
    print("baseline_archive.py 自测开始（纯内存，不落盘）")
    print("=" * 60)

    archiver = BaselineArchiver(
        project_root=Path(r"D:\Projects\Pythonwdhmm"),
        out_dir=Path(r"D:\Projects\Pythonwdhmm\outputs\_baseline_selftest_tmp"),
    )

    # 构造确定性结果
    config = BacktestConfig(
        symbols=["600519.SH"], start_date="2024-01-01", end_date="2024-06-30",
        initial_capital=1_000_000.0, strategy_name="rsrs",
    )
    result = BacktestResult(config=config, status="completed")
    result.equity_curve = [
        EquityPoint(timestamp=float(i), date=f"2024-01-{i:02d}", equity=1.0 + i * 0.01,
                    cash=1_000_000.0 - i * 10_000.0, position_value=i * 10_000.0,
                    drawdown=0.0, benchmark_equity=1.0)
        for i in range(1, 5)
    ]
    result.trades = [
        TradeRecord(trade_id=f"t{i}", symbol="600519.SH", side="BUY", quantity=100.0,
                    price=100.0 + i, timestamp=float(i), commission=1.0, slippage=0.5,
                    pnl=0.0, pnl_pct=0.0, holding_days=1, strategy_id="rsrs")
        for i in range(1, 4)
    ]

    # 1. config_hash 稳定性与运行元数据剔除
    h1 = archiver.compute_config_hash(config)
    h2 = archiver.compute_config_hash(config)
    check("config_hash 同配置同哈希", h1 == h2)
    check("config_hash 为 64 位 hex", len(h1) == 64 and all(c in "0123456789abcdef" for c in h1))

    # 2. 指纹敏感性（单点篡改即变）
    eq_hash = archiver.compute_equity_hash(result)
    result2 = BacktestResult(config=config, status="completed")
    result2.equity_curve = list(result.equity_curve)
    result2.equity_curve[2] = EquityPoint(timestamp=3.0, date="2024-01-03", equity=9.99,
                                          cash=1.0, position_value=2.0, drawdown=0.0,
                                          benchmark_equity=1.0)
    check("净值指纹单点篡改即变", archiver.compute_equity_hash(result2) != eq_hash)
    trades_hash = archiver.compute_trades_fingerprint(result)
    result3 = BacktestResult(config=config, status="completed")
    result3.trades = list(result.trades)
    result3.trades[0].quantity = 999.0
    check("成交指纹单点篡改即变", archiver.compute_trades_fingerprint(result3) != trades_hash)

    # 3. build/write/load 往返（临时目录，不污染项目）
    baseline = archiver.build_baseline(result, "rsrs", "mixed", config, extra_meta={"k": "v"})
    check("baseline 字段齐全", all(k in baseline for k in
          ("format_version", "strategy", "engine", "config_hash",
           "equity_curve_hash", "trades_fingerprint", "metrics", "summary")))
    check("baseline 无运行元数据", "run_id" not in baseline and "started_at" not in baseline)
    path = archiver.write_baseline(baseline, "rsrs", "mixed")
    loaded = archiver.load_baseline(path)
    check("write/load 往返一致", loaded == baseline)

    # 4. verify_reproducible：相同 True / 篡改 False / 成交笔数差超阈 False
    ok, report = archiver.verify_reproducible(baseline, result, config)
    check("相同结果对拍通过", ok and report["passed"])
    result_bad = BacktestResult(config=config, status="completed")
    result_bad.equity_curve = list(result.equity_curve)
    result_bad.equity_curve[0] = EquityPoint(timestamp=1.0, date="2024-01-01", equity=5.0,
                                             cash=1.0, position_value=2.0, drawdown=0.0,
                                             benchmark_equity=1.0)
    result_bad.trades = list(result.trades)
    ok2, report2 = archiver.verify_reproducible(baseline, result_bad, config)
    check("净值篡改超阈判红", not ok2 and report2["equity_hash_match"] is False)
    result_bad2 = BacktestResult(config=config, status="completed")
    result_bad2.equity_curve = list(result.equity_curve)
    result_bad2.trades = list(result.trades) + [TradeRecord(trade_id="t9", symbol="600519.SH",
                                                           side="SELL", quantity=10.0, price=1.0,
                                                           timestamp=9.0, commission=0.1,
                                                           slippage=0.1, pnl=1.0, pnl_pct=0.1,
                                                           holding_days=1, strategy_id="rsrs")]
    ok3, report3 = archiver.verify_reproducible(baseline, result_bad2, config)
    check("成交笔数差超 5% 判红", not ok3 and report3["trades_fingerprint_match"] is False)

    # 清理临时目录
    import shutil
    shutil.rmtree(archiver.out_dir, ignore_errors=True)

    print("=" * 60)
    print(f"自测结果: {passed} passed / {len(failures)} failed")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_run_self_test())
