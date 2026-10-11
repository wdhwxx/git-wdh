# 文件名：myquant/backtest/incremental_backtest.py
"""
增量回测调度器（数据指纹识别变更 + 只重算变更标的）
====================================================
配合 data_fingerprint 模块实现增量回测：
  - 对每个标的计算确定性数据指纹（内容+结构，行/列顺序无关）
  - FingerprintStore 持久化指纹仓库，get_changed_symbols 识别新增/变更标的
  - 只对变更标的执行回测（进程池并行），未变更标的直接复用上次结果缓存
  - --force-full 语义：忽略指纹差异，全部标的强制重算
  - 接入 P2 并行治理层：parallel_factor_context 自动管理 factor_cache L1 缓存

核心组件:
  - SymbolTrial / evaluate_symbol : 单标的回测任务（可 pickle，供进程池）
  - IncrementalPlan              : 调度计划（变更/复用标的划分）
  - IncrementalBacktester        : 增量回测调度器（指纹对比、并行重算、缓存合并）
  - IncrementalBacktestResult    : 聚合结果（summary/to_dict）

正确性约束:
  - 结果缓存按 fingerprint_hash 匹配：仅当缓存指纹与当前指纹一致才可复用，
    否则即使标的未列入变更也必须重算（防御：指纹仓库与结果缓存不同步）
  - 本模块不做时间序列计算，无未来函数风险（回测由 BacktestEngine 严格 T+1 撮合）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from myquant.backtest.data_loader import require_ohlcv
from myquant.backtest.engine import BacktestScheduler
from myquant.models.backtest import BacktestConfig
from myquant.strategies.ma_strategy import MACrossStrategy
from myquant.utils.data_fingerprint import (
    DataFingerprint,
    FingerprintStore,
    compute_fingerprint,
)
from myquant.utils.parallel import parallel_factor_context
from myquant.utils.timer import format_duration

logger = logging.getLogger("myquant.backtest.incremental_backtest")

# ── 常量（禁魔法数字） ──────────────────────────────────────────────
DEFAULT_INITIAL_CAPITAL: float = 1_000_000.0
DEFAULT_FREQUENCY: str = "1d"
DEFAULT_DATA_SOURCE: str = "incremental"
from myquant.core.constants import RESULTS_FORMAT_VERSION  # P3-D10-001 整改：双源收敛为单一权威（myquant/core/constants.py）

# 策略注册表：策略名 -> 策略类（扩展只需在此注册）
STRATEGY_REGISTRY: Dict[str, type] = {
    "ma_cross": MACrossStrategy,
}


# ── 单标的回测任务（可 pickle） ──────────────────────────────────────
@dataclass(frozen=True)
class SymbolTrial:
    """单标的回测任务（供子进程反序列化）。

    Attributes:
        symbol: 标的代码。
        strategy_name: 策略注册名。
        params: 策略构造参数。
        data: 单标的行情字典 {symbol: DataFrame}。
        start_date: 回测起始日期。
        end_date: 回测结束日期。
        initial_capital: 初始资金。
        frequency: K 线频率。
    """

    symbol: str
    strategy_name: str
    params: Dict[str, Any]
    data: Dict[str, pd.DataFrame]
    start_date: str
    end_date: str
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    frequency: str = DEFAULT_FREQUENCY


def evaluate_symbol(trial: SymbolTrial) -> Dict[str, Any]:
    """模块级任务函数：回测单个标的并返回指标（永不抛出）。

    异常被捕获并记入 status="error"，保证进程池不因单标的失败中断整批。

    Args:
        trial: 单标的回测任务。

    Returns:
        指标字典（含 status / metrics / trade_count / equity_length）。
    """
    base: Dict[str, Any] = {
        "symbol": trial.symbol,
        "status": "error",
        "metrics": None,
        "trade_count": None,
        "equity_length": None,
        "error": None,
    }
    try:
        cls = STRATEGY_REGISTRY.get(trial.strategy_name)
        if cls is None:
            raise ValueError(
                f"未知策略: {trial.strategy_name!r}，已注册: {sorted(STRATEGY_REGISTRY)}"
            )
        strategy = cls(**trial.params)
        config = BacktestConfig(
            strategy_name=trial.strategy_name,
            symbols=[trial.symbol],
            start_date=trial.start_date,
            end_date=trial.end_date,
            frequency=trial.frequency,
            initial_capital=trial.initial_capital,
            params=trial.params,
        )
        # P1-04：走 create_engine（旁路收敛，门禁软接线统一入口）
        scheduler = BacktestScheduler(mode="auto")
        engine = scheduler.create_engine(config=config, strategy=strategy, data=trial.data)
        result = engine.run()
        metrics = result.metrics
        base.update(
            {
                "status": "completed",
                "metrics": {
                    "total_return": float(metrics.total_return),
                    "sharpe_ratio": float(metrics.sharpe_ratio),
                    "max_drawdown": float(metrics.max_drawdown),
                },
                "trade_count": int(len(result.trades)),
                "equity_length": int(len(result.equity_curve)),
            }
        )
    except Exception as exc:  # noqa: BLE001 - 任务函数必须隔离所有异常
        base["error"] = f"{type(exc).__name__}: {exc}"
    return base


# ── 调度计划 ─────────────────────────────────────────────────────────
@dataclass
class IncrementalPlan:
    """增量回测调度计划。

    Attributes:
        force_full: 是否强制全量（忽略指纹差异）。
        total: 标的总数。
        changed: 需重算的标的（变更/新增/强制全量）。
        unchanged: 可复用缓存结果的标的。
    """

    force_full: bool
    total: int
    changed: List[str]
    unchanged: List[str]

    def __post_init__(self) -> None:
        """防御式校验：changed/unchanged 划分与 total 一致。"""
        if self.total != len(self.changed) + len(self.unchanged):
            raise ValueError(
                f"调度计划不一致: total={self.total}, "
                f"changed={len(self.changed)}, unchanged={len(self.unchanged)}"
            )

    def summary(self) -> str:
        """生成调度计划摘要文本。"""
        mode = "强制全量" if self.force_full else "增量"
        lines = [f"调度模式: {mode}（标的 {self.total} 个）"]
        lines.append(f"  需重算 {len(self.changed)} 个: {', '.join(self.changed) or '(无)'}")
        lines.append(f"  复用缓存 {len(self.unchanged)} 个: {', '.join(self.unchanged) or '(无)'}")
        return "\n".join(lines)


# ── 增量回测调度器 ───────────────────────────────────────────────────
class IncrementalBacktester:
    """增量回测调度器：指纹识别变更 + 只重算变更标的 + 结果缓存复用。

    Args:
        strategy_name: 策略注册名（见 STRATEGY_REGISTRY）。
        params: 策略构造参数。
        data: 多标的行情字典 {symbol: DataFrame}。
        fingerprint_path: 指纹仓库 JSON 路径（不存在自动创建）。
        results_path: 结果缓存 JSON 路径（不存在自动创建）。
        cpu_workers: 计算进程池大小；None 使用默认。
        start_date: 回测起始日期。
        end_date: 回测结束日期。
        initial_capital: 初始资金（>0）。
        frequency: K 线频率。

    Example:
        >>> bt = IncrementalBacktester("ma_cross", {"fast_period": 5, "slow_period": 20}, data, fp_path, res_path)
        >>> result = bt.run()                 # 首次全量
        >>> # ... 修改部分标的数据 ...
        >>> result2 = bt.run()                # 增量：只重算变更标的
    """

    def __init__(
        self,
        strategy_name: str,
        params: Dict[str, Any],
        data: Dict[str, pd.DataFrame],
        fingerprint_path: str,
        results_path: str,
        cpu_workers: Optional[int] = None,
        start_date: str = "",
        end_date: str = "",
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        frequency: str = DEFAULT_FREQUENCY,
        commission: Optional[Any] = None,
        slippage: Optional[Any] = None,
        benchmark: Optional[str] = None,
        risk_free_rate: Optional[float] = None,
    ) -> None:
        """初始化并做防御式参数校验。"""
        if not isinstance(strategy_name, str) or not strategy_name.strip():
            raise ValueError("strategy_name 必须为非空字符串")
        if strategy_name not in STRATEGY_REGISTRY:
            raise ValueError(
                f"未知策略: {strategy_name!r}，已注册: {sorted(STRATEGY_REGISTRY)}"
            )
        if not isinstance(data, dict) or len(data) == 0:
            raise ValueError("data 必须为非空字典 {symbol: DataFrame}")
        if cpu_workers is not None and (not isinstance(cpu_workers, int) or cpu_workers < 1):
            raise ValueError(f"cpu_workers 必须为正整数或 None，收到: {cpu_workers!r}")
        if not isinstance(initial_capital, (int, float)) or initial_capital <= 0:
            raise ValueError(f"initial_capital 必须为正数，收到: {initial_capital!r}")
        if not isinstance(frequency, str) or not frequency.strip():
            raise ValueError("frequency 必须为非空字符串")

        for symbol, df in data.items():
            if not isinstance(df, pd.DataFrame):
                raise TypeError(f"标的 {symbol} 的数据必须为 DataFrame")
            require_ohlcv(df, symbol=symbol)

        self.strategy_name = strategy_name
        self.params = dict(params)
        self.data = data
        self.cpu_workers = cpu_workers
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.frequency = frequency
        self.commission = commission
        self.slippage = slippage
        self.benchmark = benchmark
        self.risk_free_rate = risk_free_rate

        self._fingerprint_path = Path(fingerprint_path)
        self._results_path = Path(results_path)
        self._store = FingerprintStore(self._fingerprint_path)
        self._results: Dict[str, Dict[str, Any]] = self._load_results()

    # ── 指纹与结果缓存 ──

    def _config_signature(self) -> str:
        """计算回测参数签名（策略/参数/成本/绩效/区间，与 batch_backtest._config_signature 同构 10 字段）。

        任一参数变化（佣金率/滑点/基准/无风险利率/区间/资金）即签名变化；
        plan() 比对缓存签名，不一致时强制重算，避免参数变更后静默复用旧缓存。
        防未来函数：仅对当前配置做确定性哈希，不涉及时序数据。

        Returns:
            SHA-256 十六进制签名。
        """
        payload: Dict[str, Any] = {
            "strategy_name": self.strategy_name,
            "params": self.params,
            "commission": self.commission,
            "slippage": self.slippage,
            "benchmark": self.benchmark,
            "risk_free_rate": self.risk_free_rate,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "frequency": self.frequency,
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def compute_fingerprints(self) -> Dict[str, DataFingerprint]:
        """计算全部标的的当前数据指纹。

        Returns:
            {symbol: DataFingerprint}。
        """
        fps: Dict[str, DataFingerprint] = {}
        for symbol, df in self.data.items():
            fps[symbol] = compute_fingerprint(
                df, symbol=symbol, data_source=DEFAULT_DATA_SOURCE
            )
        return fps

    def _load_results(self) -> Dict[str, Dict[str, Any]]:
        """从磁盘加载结果缓存（文件不存在/损坏则初始化为空）。"""
        if not self._results_path.exists():
            return {}
        try:
            with open(self._results_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("结果缓存解析失败，初始化为空: %s（%s）",
                           self._results_path, exc)
            return {}
        return data.get("results", {})

    def _save_results(self) -> None:
        """原子写入结果缓存。"""
        self._results_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": RESULTS_FORMAT_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "results": self._results,
        }
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._results_path.parent), suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(self._results_path))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # ── 调度计划 ──

    def plan(self, force_full: bool = False) -> IncrementalPlan:
        """计算调度计划：变更标的（需重算）与复用标的（缓存命中）。

        Args:
            force_full: True 时忽略指纹差异，全部标的视为变更（--force-full）。

        Returns:
            调度计划。

        Raises:
            ValueError: force_full 非 bool。
        """
        if not isinstance(force_full, bool):
            raise TypeError(f"force_full 必须为 bool，收到: {type(force_full).__name__}")
        fps = self.compute_fingerprints()
        symbols = sorted(self.data.keys())

        if force_full:
            return IncrementalPlan(force_full=True, total=len(symbols),
                                   changed=symbols, unchanged=[])

        # 指纹识别变更：新增标的 / 内容结构变化均计入变更
        changed = self._store.get_changed_symbols(fps)

        # 正确性兜底：变更集合中若有标的在结果缓存中缺失或指纹不匹配，强制纳入重算
        final_changed: List[str] = []
        for symbol in symbols:
            cached = self._results.get(symbol)
            fp = fps.get(symbol)
            if symbol in changed:
                final_changed.append(symbol)
            elif (cached is None or fp is None
                  or cached.get("fingerprint_hash") != fp.hash_value
                  or cached.get("config_signature") != self._config_signature()
                  or cached.get("format_version") != RESULTS_FORMAT_VERSION):
                # 缓存缺失/指纹不匹配/配置签名不一致（参数或费用变更）/格式版本升版，防御性重算
                final_changed.append(symbol)
                logger.info("防御性重算: %s（缓存缺失/指纹不匹配/配置签名或格式版本不一致）", symbol)

        final_changed = sorted(set(final_changed))
        unchanged = [s for s in symbols if s not in final_changed]
        return IncrementalPlan(force_full=False, total=len(symbols),
                               changed=final_changed, unchanged=unchanged)

    # ── 执行 ──

    def run(self, force_full: bool = False) -> "IncrementalBacktestResult":
        """执行增量回测：识别变更 -> 并行重算变更标的 -> 合并缓存 -> 更新指纹。

        Args:
            force_full: True 时强制全量重算（--force-full 开关）。

        Returns:
            增量回测聚合结果。
        """
        plan = self.plan(force_full=force_full)
        fps = self.compute_fingerprints()

        trials = [
            SymbolTrial(
                symbol=symbol,
                strategy_name=self.strategy_name,
                params=self.params,
                data={symbol: self.data[symbol]},
                start_date=self.start_date,
                end_date=self.end_date,
                initial_capital=self.initial_capital,
                frequency=self.frequency,
            )
            for symbol in plan.changed
        ]

        eval_t0 = time.perf_counter()
        if trials:
            # 并行上下文：自动关闭 factor_cache L1 并告警，退出恢复
            with parallel_factor_context(cpu_workers=self.cpu_workers) as pm:
                outcomes = pm.map_cpu(
                    evaluate_symbol, trials,
                    return_exceptions=True, check_pickle=True,
                )
            elapsed = time.perf_counter() - eval_t0

            # 合并新结果到缓存（含异常兜底）
            for trial, outcome in zip(trials, outcomes):
                symbol = trial.symbol
                fp = fps[symbol]
                if isinstance(outcome, BaseException):
                    self._results[symbol] = {
                        "fingerprint_hash": fp.hash_value,
                        "config_signature": self._config_signature(),
                        "format_version": RESULTS_FORMAT_VERSION,
                        "status": "error",
                        "metrics": None,
                        "trade_count": None,
                        "equity_length": None,
                        "error": f"{type(outcome).__name__}: {outcome}",
                        "computed_at": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    outcome["fingerprint_hash"] = fp.hash_value
                    outcome["config_signature"] = self._config_signature()
                    outcome["format_version"] = RESULTS_FORMAT_VERSION
                    outcome["computed_at"] = datetime.now(timezone.utc).isoformat()
                    self._results[symbol] = outcome
        else:
            elapsed = time.perf_counter() - eval_t0

        # 更新指纹仓库 + 持久化结果
        self._store.sync(fps)
        self._save_results()

        # 组装结果（按输入顺序）
        per_symbol: Dict[str, Dict[str, Any]] = {}
        for symbol in sorted(self.data.keys()):
            per_symbol[symbol] = self._results.get(symbol, {
                "status": "error", "error": "结果缓存缺失",
            })

        return IncrementalBacktestResult(
            plan=plan,
            per_symbol=per_symbol,
            elapsed_seconds=elapsed,
        )


# ── 聚合结果 ─────────────────────────────────────────────────────────
@dataclass
class IncrementalBacktestResult:
    """增量回测聚合结果。

    Attributes:
        plan: 调度计划（重算/复用划分）。
        per_symbol: {symbol: 结果字典}（含状态与指标）。
        elapsed_seconds: 并行重算耗时（不含指纹/落盘）。
    """

    plan: IncrementalPlan
    per_symbol: Dict[str, Dict[str, Any]]
    elapsed_seconds: float

    @property
    def recomputed_count(self) -> int:
        """本次重算标的总数。"""
        return len(self.plan.changed)

    @property
    def reused_count(self) -> int:
        """复用缓存结果标的总数。"""
        return len(self.plan.unchanged)

    @property
    def error_count(self) -> int:
        """本次重算中失败标的数。"""
        return sum(
            1 for s in self.plan.changed
            if self.per_symbol.get(s, {}).get("status") != "completed"
        )

    def reused_ratio(self) -> float:
        """缓存复用比例（0~1），用于省时估算。"""
        total = self.plan.total
        if total == 0:
            return 0.0
        return self.reused_count / total

    def summary(self) -> str:
        """生成增量回测报告文本。"""
        lines = ["增量回测报告"]
        lines.append("=" * 64)
        lines.append(f"标的总数: {self.plan.total}")
        lines.append(
            f"重算 {self.recomputed_count} 个 | 复用 {self.reused_count} 个 "
            f"| 失败 {self.error_count} 个 | 复用率 {self.reused_ratio():.1%}"
        )
        lines.append(f"并行重算耗时: {format_duration(self.elapsed_seconds)}")
        lines.append("-" * 64)
        lines.append(f"{'标的':<12} {'状态':<10} {'总收益':>9} {'夏普':>8} {'回撤':>9} {'来源':<8}")
        for symbol in sorted(self.per_symbol.keys()):
            entry = self.per_symbol[symbol]
            status = entry.get("status", "error")
            metrics = entry.get("metrics")
            if status == "completed" and metrics is not None:
                src = "重算" if symbol in self.plan.changed else "复用"
                lines.append(
                    f"{symbol:<12} {status:<10} {metrics['total_return']:>9.2%} "
                    f"{metrics['sharpe_ratio']:>8.3f} {metrics['max_drawdown']:>9.2%} {src:<8}"
                )
            else:
                err = (entry.get("error") or "未知错误")[:40]
                lines.append(f"{symbol:<12} {status:<10} {'-':>9} {'-':>8} {'-':>9} 失败:{err}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可读字典（供 JSON 落盘）。"""
        return {
            "force_full": self.plan.force_full,
            "total_count": self.plan.total,
            "recomputed_count": self.recomputed_count,
            "reused_count": self.reused_count,
            "error_count": self.error_count,
            "reused_ratio": round(self.reused_ratio(), 6),
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "changed": self.plan.changed,
            "unchanged": self.plan.unchanged,
            "per_symbol": self.per_symbol,
        }


# ── 演示 / 自测入口 ───────────────────────────────────────────────────
def _main() -> int:
    """模块自测：首次全量 -> 修改单标的 -> 增量只重算变更标的。"""
    from myquant.backtest.param_optimizer import make_demo_data

    print("=" * 64)
    print("  incremental_backtest.py — 增量回测（指纹识别变更）自测 Demo")
    print("=" * 64)

    cache_dir = Path(tempfile.mkdtemp(prefix="incremental_test_"))
    fp_path = cache_dir / "fingerprints.json"
    res_path = cache_dir / "results.json"

    # 1. 构造演示数据（5 标的）
    symbols = ["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH", "600001.SH"]
    data = make_demo_data(symbols=symbols, n_bars=300, seed=7)
    print(f"[1] 构造 {len(symbols)} 标的演示行情（各 300 根 K 线）")

    params = {"fast_period": 5, "slow_period": 20}

    def _run(tag: str, force_full: bool = False) -> IncrementalBacktestResult:
        bt = IncrementalBacktester(
            "ma_cross", params, data, str(fp_path), str(res_path),
            start_date="2023-01-02", end_date="2024-01-31",
        )
        result = bt.run(force_full=force_full)
        print(f"\n[{tag}] {result.plan.summary()}")
        return result

    # 2. 首次运行：无历史指纹，全量重算
    r1 = _run("首次运行（无指纹，预期全量）")
    assert r1.recomputed_count == len(symbols), "首次应全量重算"
    assert r1.reused_count == 0
    print(f"    ✓ 首次全量重算 {r1.recomputed_count} 个标的")

    # 3. 模拟数据更新：修改最后一个标的的收盘价
    changed_symbol = symbols[-1]
    df_modified = data[changed_symbol].copy()
    df_modified.iloc[-1, df_modified.columns.get_loc("close")] *= 1.1
    data[changed_symbol] = df_modified
    print(f"\n[3] 模拟数据更新：修改 {changed_symbol} 最后一日收盘价")

    # 4. 第二次运行：增量，只重算变更标的
    r2 = _run("增量运行（指纹识别变更）")
    assert r2.recomputed_count == 1, "增量应只重算 1 个变更标的"
    assert r2.reused_count == len(symbols) - 1
    assert r2.plan.changed == [changed_symbol], f"变更标的应为 {changed_symbol}"
    print(f"    ✓ 增量只重算 {changed_symbol}，其余 {r2.reused_count} 个复用缓存")

    # 5. 复用标的指标与首次一致（未重算未变化）
    reused_symbol = symbols[0]
    assert reused_symbol not in r2.plan.changed
    assert r2.per_symbol[reused_symbol]["status"] == "completed"
    assert r2.per_symbol[reused_symbol]["metrics"] == r1.per_symbol[reused_symbol]["metrics"]
    print(f"    ✓ 复用标的 {reused_symbol} 指标与首次一致")

    # 6. 强制全量：忽略指纹差异
    r3 = _run("强制全量（--force-full）", force_full=True)
    assert r3.recomputed_count == len(symbols), "force_full 应全量重算"
    assert r3.reused_count == 0
    print(f"    ✓ 强制全量重算 {r3.recomputed_count} 个标的")

    # 7. 数据未变化：再次增量，0 个重算
    r4 = _run("数据未变化增量（预期 0 重算）")
    assert r4.recomputed_count == 0, "数据未变化应 0 重算"
    assert r4.reused_count == len(symbols)
    print(f"    ✓ 数据未变化，0 个重算，{r4.reused_count} 个全部复用")

    print("\n" + "=" * 64)
    print(f"  自测通过: 全量 -> 增量1变更 -> 强制全量 -> 0变更 流程闭环")
    print("=" * 64)

    import shutil
    shutil.rmtree(cache_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
