# 文件名：myquant/backtest/batch_backtest.py
"""
批量回测主调度器（数据指纹 + 三模式调度 + 内存自动降级）
========================================================
run_批量回测.py 主入口的支撑核心：
  - 数据指纹：FingerprintStore 识别变更标的，只重算变更，未变更复用结果缓存
  - 三模式调度：BacktestScheduler（auto/event/mixed/vectorized）按策略能力自动选择，
    任务函数经 run_backtest 三模式调度执行
  - 内存自动降级：MemoryWatcher 水位监控，内存压力升高时自动降级引擎模式与并行度：
      引擎降级链 vectorized -> mixed -> event（内存占用递减）
      WARNING  -> 引擎降一级 + 并行度减半
      CRITICAL -> 强制 event 模式 + 串行（cpu=1）

核心组件:
  - BatchTrial / evaluate_batch_symbol : 单标的批量任务（可 pickle，三模式调度）
  - BatchPlan                          : 调度计划（变更/复用 + 引擎模式）
  - MemoryDegradePolicy                : 内存降级策略（引擎模式 + 并行度）
  - BatchBacktester                    : 批量调度器（指纹、分批、并行、降级、缓存）
  - BatchBacktestResult                : 聚合结果（summary/to_dict）

正确性约束:
  - 结果缓存按 fingerprint_hash 匹配，仓库与缓存不同步时防御性重算（同增量回测）
  - 任务函数模块级定义，满足 Windows spawn 下子进程 pickle 反序列化约束
  - 引擎模式降级不影响正确性（三模式净值在 P1 已验收一致），仅牺牲性能换内存

数据版本（P3-D8-003 整改声明）:
  - DatasetVersion（数据版本标记）由 data_access/ 数据层（fetcher/manager/full_clean_service）管理，
    回测侧不消费；回测防呆由 fingerprint_hash 指纹承担——版本标记仅服务于数据层，不回测路径，文档声明覆盖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from myquant.backtest.data_loader import require_ohlcv
from myquant.backtest.engine import register_broker_factory, run_backtest
from myquant.models.backtest import (
    BacktestConfig,
    CommissionModel,
    SlippageModel,
)
from myquant.strategies.grid_strategy import GridStrategy
from myquant.strategies.kunpeng_strategy import KunpengStrategy
from myquant.strategies.ma_strategy import MACrossStrategy, RSRSStrategy
from myquant.strategies.mean_reversion_strategy import MeanReversionStrategy
from myquant.strategies.momentum_strategy import MomentumStrategy
from myquant.strategies.multi_factor_strategy import MultiFactorStrategy
from myquant.utils.data_fingerprint import (
    DataFingerprint,
    FingerprintStore,
    compute_fingerprint,
)
from myquant.utils.memory_watcher import MemoryLevel, MemoryWatcher, get_memory_watcher
from myquant.utils.parallel import parallel_factor_context
from myquant.utils.timer import format_duration

logger = logging.getLogger("myquant.backtest.batch_backtest")


# ── 常量（禁魔法数字） ──────────────────────────────────────────────
DEFAULT_INITIAL_CAPITAL: float = 1_000_000.0
DEFAULT_FREQUENCY: str = "1d"
DEFAULT_DATA_SOURCE: str = "batch"
from myquant.core.constants import (
    RESULTS_FORMAT_VERSION,  # P3-D10-001 整改：双源收敛为单一权威（myquant/core/constants.py）
)

DEFAULT_MODE: str = "auto"
DEFAULT_BENCHMARK: str = "000300.SH"
DEFAULT_RISK_FREE_RATE: float = 0.02

# 引擎模式内存降级链（索引越大内存占用越低，event 最省）
MEMORY_DEGRADE_CHAIN: Tuple[str, ...] = ("vectorized", "mixed", "event")
# 内存降级时并行度除数（WARNING 减半）
DEGRADE_CPU_DIVISOR: int = 2
# 低内存水位引擎（CRITICAL 强制使用）
LOW_MEMORY_ENGINE: str = MEMORY_DEGRADE_CHAIN[-1]

# 策略注册表：策略名 -> 策略类（扩展只需在此注册；6 策略均实现三引擎接口）
STRATEGY_REGISTRY: Dict[str, type] = {
    "ma_cross": MACrossStrategy,
    "rsrs": RSRSStrategy,
    "grid": GridStrategy,
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "multi_factor": MultiFactorStrategy,
    "kunpeng": KunpengStrategy,
}


# ── 单标的批量任务（可 pickle） ──────────────────────────────────────
@dataclass(frozen=True)
class BatchTrial:
    """单标的批量回测任务（三模式调度，供子进程反序列化）。

    Attributes:
        symbol: 标的代码。
        strategy_name: 策略注册名。
        params: 策略构造参数。
        mode: 引擎模式（event/mixed/vectorized，已按内存降级解析）。
        data: 单标的行情字典 {symbol: DataFrame}。
        start_date: 回测起始日期。
        end_date: 回测结束日期。
        initial_capital: 初始资金。
        frequency: K 线频率。
        commission: 交易成本模型；None 时引擎用默认写实模型。
        slippage: 滑点模型；None 时引擎用默认固定 2bp。
        benchmark: 基准指数代码（绩效口径）。
        risk_free_rate: 无风险利率（夏普计算口径）。
        include_detail: True 时返回完整成交/净值明细（保存开关导出用，不入缓存）。
    """

    symbol: str
    strategy_name: str
    params: Dict[str, Any]
    mode: str
    data: Dict[str, pd.DataFrame]
    start_date: str
    end_date: str
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    frequency: str = DEFAULT_FREQUENCY
    commission: Optional[CommissionModel] = None
    slippage: Optional[SlippageModel] = None
    benchmark: str = DEFAULT_BENCHMARK
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
    include_detail: bool = False


def evaluate_batch_symbol(
    trial: BatchTrial,
    broker_factory: Optional[Callable[[Any], Any]] = None,
) -> Dict[str, Any]:
    """模块级任务函数：经三模式调度回测单标的并返回指标（永不抛出）。

    异常被捕获并记入 status="error"，保证进程池不因单标的失败中断整批。

    Args:
        trial: 单标的批量回测任务。

    Returns:
        指标字典（含 status / metrics / trade_count / equity_length / mode）。
    """
    base: Dict[str, Any] = {
        "symbol": trial.symbol,
        "status": "error",
        "metrics": None,
        "trade_count": None,
        "equity_length": None,
        "mode": trial.mode,
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
            commission=trial.commission or CommissionModel(),
            slippage=trial.slippage or SlippageModel(),
            benchmark=trial.benchmark,
            risk_free_rate=trial.risk_free_rate,
        )
        result = run_backtest(
            config, strategy, data=trial.data, mode=trial.mode,
            broker_factory=broker_factory,
        )
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
        if trial.include_detail:
            base["detail"] = {
                "trades": [trade.to_dict() for trade in result.trades],
                "equity": [point.to_dict() for point in result.equity_curve],
            }
    except Exception as exc:  # noqa: BLE001 - 任务函数必须隔离所有异常
        base["error"] = f"{type(exc).__name__}: {exc}"
    return base


# ── 调度计划 ─────────────────────────────────────────────────────────
@dataclass
class BatchPlan:
    """批量回测调度计划。

    Attributes:
        force_full: 是否强制全量。
        total: 标的总数。
        changed: 需重算的标的。
        unchanged: 可复用缓存结果的标的。
        base_mode: 用户请求/解析出的基准引擎模式（auto 时按策略能力解析）。
    """

    force_full: bool
    total: int
    changed: List[str]
    unchanged: List[str]
    base_mode: str

    def __post_init__(self) -> None:
        """防御式校验：划分一致性与 base_mode 合法。"""
        if self.total != len(self.changed) + len(self.unchanged):
            raise ValueError(
                f"调度计划不一致: total={self.total}, "
                f"changed={len(self.changed)}, unchanged={len(self.unchanged)}"
            )
        if self.base_mode not in MEMORY_DEGRADE_CHAIN:
            raise ValueError(
                f"base_mode 必须为 {MEMORY_DEGRADE_CHAIN}，收到: {self.base_mode!r}"
            )

    def summary(self) -> str:
        """生成调度计划摘要文本。"""
        mode = "强制全量" if self.force_full else "增量"
        lines = [f"调度模式: {mode}（标的 {self.total} 个）| 基准引擎: {self.base_mode}"]
        lines.append(f"  需重算 {len(self.changed)} 个: {', '.join(self.changed) or '(无)'}")
        lines.append(f"  复用缓存 {len(self.unchanged)} 个: {', '.join(self.unchanged) or '(无)'}")
        return "\n".join(lines)


# ── 内存降级策略 ─────────────────────────────────────────────────────
class MemoryDegradePolicy:
    """内存降级策略：根据内存水位决定引擎模式与并行度。

    Attributes:
        degrade_chain: 引擎模式降级链（内存占用递减）。
        halve_cpu_on_warning: WARNING 时是否将并行度减半。
    """

    def __init__(
        self,
        degrade_chain: Sequence[str] = MEMORY_DEGRADE_CHAIN,
        halve_cpu_on_warning: bool = True,
    ) -> None:
        """初始化降级策略。"""
        chain = list(degrade_chain)
        if len(chain) < 1:
            raise ValueError("降级链至少需要一个引擎模式")
        if len(set(chain)) != len(chain):
            raise ValueError(f"降级链存在重复模式: {chain}")
        if not isinstance(halve_cpu_on_warning, bool):
            raise TypeError(
                f"halve_cpu_on_warning 必须为 bool，收到: {type(halve_cpu_on_warning).__name__}"
            )
        self.degrade_chain: List[str] = chain
        self.halve_cpu_on_warning = halve_cpu_on_warning

    def resolve_mode(self, requested: str, level: MemoryLevel) -> Tuple[str, bool]:
        """按内存水位解析最终引擎模式。

        Args:
            requested: 请求的引擎模式（须在降级链中）。
            level: 内存水位级别。

        Returns:
            (最终引擎模式, 是否发生降级)。
        """
        if requested not in self.degrade_chain:
            raise ValueError(
                f"requested 模式 {requested!r} 不在降级链 {self.degrade_chain} 中"
            )
        if level == MemoryLevel.CRITICAL:
            target = self.degrade_chain[-1]
        elif level == MemoryLevel.WARNING:
            idx = self.degrade_chain.index(requested)
            target = self.degrade_chain[min(idx + 1, len(self.degrade_chain) - 1)]
        else:
            target = requested
        return target, target != requested

    def resolve_cpu(self, cpu_workers: Optional[int], level: MemoryLevel) -> int:
        """按内存水位解析最终并行度。

        Args:
            cpu_workers: 请求的并行度（None 表示默认）。
            level: 内存水位级别。

        Returns:
            最终并行度（CRITICAL 强制 1；WARNING 减半；NORMAL 保持）。
        """
        base = cpu_workers if cpu_workers is not None and cpu_workers >= 1 else 1
        if level == MemoryLevel.CRITICAL:
            return 1
        if level == MemoryLevel.WARNING and self.halve_cpu_on_warning:
            return max(1, base // DEGRADE_CPU_DIVISOR)
        return base


# ── 批量回测调度器 ───────────────────────────────────────────────────
class BatchBacktester:
    """批量回测调度器：指纹识别变更 + 三模式调度 + 内存自动降级。

    Args:
        strategy_name: 策略注册名（见 STRATEGY_REGISTRY）。
        params: 策略构造参数。
        data: 多标的行情字典 {symbol: DataFrame}。
        fingerprint_path: 指纹仓库 JSON 路径。
        results_path: 结果缓存 JSON 路径。
        mode: 引擎模式（auto/event/mixed/vectorized）。
        cpu_workers: 计算进程池大小；None 使用默认。
        start_date / end_date: 回测日期范围。
        initial_capital: 初始资金。
        frequency: K 线频率。
        commission: 交易成本模型；None 时引擎用默认写实模型。
        slippage: 滑点模型；None 时引擎用默认固定 2bp。
        benchmark: 基准指数代码（绩效口径，用于结果归一与对比）。
        risk_free_rate: 无风险利率（夏普计算口径）。
        include_detail: True 时保留本次重算标的的成交/净值明细（供导出，不入缓存）。
        batch_size: 每批标的数（None 表示一次全批）。
        memory_watcher: 内存监控器；None 使用全局单例。
        on_batch_done: 可选批次完成回调（当前批次号, 总批次数），
            供 GUI 进度面板实时刷新；None 表示不回调。

    Example:
        >>> bt = BatchBacktester("ma_cross", {"fast_period": 5, "slow_period": 20}, data, fp_path, res_path)
        >>> result = bt.run()
    """

    def __init__(
        self,
        strategy_name: str,
        params: Dict[str, Any],
        data: Dict[str, pd.DataFrame],
        fingerprint_path: str,
        results_path: str,
        mode: str = DEFAULT_MODE,
        cpu_workers: Optional[int] = None,
        start_date: str = "",
        end_date: str = "",
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        frequency: str = DEFAULT_FREQUENCY,
        commission: Optional[CommissionModel] = None,
        slippage: Optional[SlippageModel] = None,
        benchmark: str = DEFAULT_BENCHMARK,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
        include_detail: bool = False,
        batch_size: Optional[int] = None,
        memory_watcher: Optional[MemoryWatcher] = None,
        on_batch_done: Optional[Callable[[int, int], None]] = None,
        broker_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        """初始化并做防御式参数校验。

        Args:
            broker_factory: 撮合执行器工厂（G1 EX-09A 注入点，签名
                factory(config) -> broker，如 SimBroker；None 时不注册，
                BacktestEngine 回退全局 register_broker_factory）。"""
        if not isinstance(strategy_name, str) or not strategy_name.strip():
            raise ValueError("strategy_name 必须为非空字符串")
        if strategy_name not in STRATEGY_REGISTRY:
            raise ValueError(
                f"未知策略: {strategy_name!r}，已注册: {sorted(STRATEGY_REGISTRY)}"
            )
        if not isinstance(data, dict) or len(data) == 0:
            raise ValueError("data 必须为非空字典 {symbol: DataFrame}")
        if mode not in ("auto", *MEMORY_DEGRADE_CHAIN):
            raise ValueError(
                f"未知引擎模式: {mode!r}，可选: auto/event/mixed/vectorized"
            )
        if cpu_workers is not None and (not isinstance(cpu_workers, int) or cpu_workers < 1):
            raise ValueError(f"cpu_workers 必须为正整数或 None，收到: {cpu_workers!r}")
        if batch_size is not None and (not isinstance(batch_size, int) or batch_size < 1):
            raise ValueError(f"batch_size 必须为正整数或 None，收到: {batch_size!r}")
        if not isinstance(initial_capital, (int, float)) or initial_capital <= 0:
            raise ValueError(f"initial_capital 必须为正数，收到: {initial_capital!r}")
        if not isinstance(frequency, str) or not frequency.strip():
            raise ValueError("frequency 必须为非空字符串")
        if commission is not None and not isinstance(commission, CommissionModel):
            raise TypeError(
                f"commission 必须为 CommissionModel 或 None，收到: {type(commission).__name__}"
            )
        if slippage is not None and not isinstance(slippage, SlippageModel):
            raise TypeError(
                f"slippage 必须为 SlippageModel 或 None，收到: {type(slippage).__name__}"
            )
        if not isinstance(benchmark, str) or not benchmark.strip():
            raise ValueError("benchmark 必须为非空字符串")
        if (
            not isinstance(risk_free_rate, (int, float))
            or isinstance(risk_free_rate, bool)
            or risk_free_rate < 0
        ):
            raise ValueError(f"risk_free_rate 必须为非负数，收到: {risk_free_rate!r}")
        if not isinstance(include_detail, bool):
            raise TypeError(f"include_detail 必须为 bool，收到: {type(include_detail).__name__}")
        if on_batch_done is not None and not callable(on_batch_done):
            raise TypeError(
                f"on_batch_done 必须为可调用对象或 None，"
                f"收到: {type(on_batch_done).__name__}"
            )

        for symbol, df in data.items():
            if not isinstance(df, pd.DataFrame):
                raise TypeError(f"标的 {symbol} 的数据必须为 DataFrame")
            require_ohlcv(df, symbol=symbol)

        self.strategy_name = strategy_name
        self.params = dict(params)
        self.data = data
        self.mode = mode
        self.cpu_workers = cpu_workers
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.frequency = frequency
        self.commission = commission
        self.slippage = slippage
        self.benchmark = benchmark
        self.risk_free_rate = risk_free_rate
        self.include_detail = include_detail
        self.batch_size = batch_size
        self._on_batch_done = on_batch_done
        self._broker_factory: Optional[Callable[[Any], Any]] = broker_factory

        self._fingerprint_path = Path(fingerprint_path)
        self._results_path = Path(results_path)
        self._store = FingerprintStore(self._fingerprint_path)
        self._config_signature_stored: Optional[str] = None
        self._results: Dict[str, Dict[str, Any]] = self._load_results()
        self._memory_watcher = memory_watcher or get_memory_watcher()
        self._degrade_policy = MemoryDegradePolicy()

    # ── 指纹与结果缓存 ──

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

    def _config_signature(self) -> str:
        """计算回测参数签名（策略/参数/成本/绩效/区间）。

        任一参数变化（如佣金率、基准、无风险利率）即签名变化；
        plan() 比对缓存签名，不一致时整体判为重算，
        避免参数变更后静默复用旧缓存（旧缓存无签名视为不匹配，自动全量重算一次）。
        防未来函数：仅对当前配置做确定性哈希，不涉及时序数据。

        Returns:
            SHA-256 十六进制签名。
        """
        payload: Dict[str, Any] = {
            "strategy_name": self.strategy_name,
            "params": self.params,
            "commission": asdict(self.commission or CommissionModel()),
            "slippage": asdict(self.slippage or SlippageModel()),
            "benchmark": self.benchmark,
            "risk_free_rate": self.risk_free_rate,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "frequency": self.frequency,
        }
        canonical = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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
        self._config_signature_stored = data.get("config_signature")
        return data.get("results", {})

    def _save_results(self) -> None:
        """原子写入结果缓存。"""
        self._results_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": RESULTS_FORMAT_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "config_signature": self._config_signature(),
            # detail（成交/净值明细）仅用于导出，不落入缓存，避免 results.json 体积膨胀
            "results": {
                s: {k: v for k, v in r.items() if k != "detail"}
                for s, r in self._results.items()
            },
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

    # ── 引擎模式解析 ──

    def _resolve_base_mode(self) -> str:
        """解析基准引擎模式：auto 时按策略能力优先级选择。

        Returns:
            具体引擎模式（vectorized/mixed/event）。

        Raises:
            ValueError: 策略不支持任何模式。
        """
        if self.mode != "auto":
            return self.mode
        cls = STRATEGY_REGISTRY[self.strategy_name]
        strategy = cls(**self.params)
        modes = set(strategy.engine_modes())
        for candidate in MEMORY_DEGRADE_CHAIN:
            if candidate in modes:
                return candidate
        raise ValueError(
            f"策略 {self.strategy_name} 不支持任何可用引擎模式: {sorted(modes)}"
        )

    # ── 调度计划 ──

    def plan(self, force_full: bool = False) -> BatchPlan:
        """计算调度计划：变更标的（需重算）与复用标的（缓存命中）。

        Args:
            force_full: True 时忽略指纹差异，全部标的视为变更。

        Returns:
            调度计划。
        """
        if not isinstance(force_full, bool):
            raise TypeError(f"force_full 必须为 bool，收到: {type(force_full).__name__}")
        fps = self.compute_fingerprints()
        symbols = sorted(self.data.keys())
        base_mode = self._resolve_base_mode()

        config_sig = self._config_signature()
        config_changed = self._config_signature_stored != config_sig
        if config_changed:
            logger.info(
                "回测参数签名变化，全部 %d 只标的需重算（旧缓存签名不匹配）",
                len(symbols),
            )
        if force_full or config_changed:
            return BatchPlan(force_full=True, total=len(symbols),
                             changed=symbols, unchanged=[], base_mode=base_mode)

        changed = self._store.get_changed_symbols(fps)
        final_changed: List[str] = []
        for symbol in symbols:
            cached = self._results.get(symbol)
            fp = fps.get(symbol)
            if symbol in changed:
                final_changed.append(symbol)
            elif cached is None or fp is None or cached.get("fingerprint_hash") != fp.hash_value:
                final_changed.append(symbol)
                logger.info("防御性重算: %s（结果缓存缺失或指纹不匹配）", symbol)

        final_changed = sorted(set(final_changed))
        unchanged = [s for s in symbols if s not in final_changed]
        return BatchPlan(force_full=False, total=len(symbols),
                         changed=final_changed, unchanged=unchanged,
                         base_mode=base_mode)

    # ── 执行 ──

    def run(self, force_full: bool = False) -> "BatchBacktestResult":
        """执行批量回测：指纹识别变更 -> 分批并行三模式回测 -> 内存自动降级。

        Args:
            force_full: True 时强制全量重算。

        Returns:
            批量回测聚合结果。
        """
        plan = self.plan(force_full=force_full)
        fps = self.compute_fingerprints()

        # 分批次（batch_size 为 None 时一批完成；changed 为空时取 1 使 range 合法）
        changed = plan.changed
        batch_size = self.batch_size if self.batch_size is not None else max(1, len(changed))
        batches: List[List[str]] = []
        for i in range(0, len(changed), batch_size):
            batches.append(changed[i:i + batch_size])

        degradation_events: List[Dict[str, Any]] = []
        eval_t0 = time.perf_counter()
        # 兜底水位：无批次（全部复用）时供结果引用
        level = self._memory_watcher.check()

        for batch_idx, batch in enumerate(batches, start=1):
            # 批次开始前检查内存水位 → 决定引擎模式与并行度
            level = self._memory_watcher.check()
            base_mode = plan.base_mode
            mode, degraded = self._degrade_policy.resolve_mode(base_mode, level)
            cpu = self._degrade_policy.resolve_cpu(self.cpu_workers, level)

            if degraded:
                event = {
                    "batch": batch_idx,
                    "memory_level": level.value,
                    "from_mode": base_mode,
                    "to_mode": mode,
                    "cpu_workers": cpu,
                }
                degradation_events.append(event)
                logger.warning(
                    "内存降级（批次 %d）: %s -> %s（水位 %s，并行 %d）",
                    batch_idx, base_mode, mode, level.value, cpu,
                )

            trials = [
                BatchTrial(
                    symbol=symbol,
                    strategy_name=self.strategy_name,
                    params=self.params,
                    mode=mode,
                    data={symbol: self.data[symbol]},
                    start_date=self.start_date,
                    end_date=self.end_date,
                    initial_capital=self.initial_capital,
                    frequency=self.frequency,
                    commission=self.commission,
                    slippage=self.slippage,
                    benchmark=self.benchmark,
                    risk_free_rate=self.risk_free_rate,
                    include_detail=self.include_detail,
                )
                for symbol in batch
            ]

            if trials:
                with parallel_factor_context(cpu_workers=cpu) as pm:
                    # G1 EX-09A：主进程注册 + 任务函数经 partial 显式传工厂
                    # （Windows spawn 下子进程 pickle partial 参数，不依赖
                    # 全局注册表与 initializer——共享进程池单例场景下后者不可靠）
                    if self._broker_factory is not None:
                        register_broker_factory(self._broker_factory)
                    task_fn: Any = evaluate_batch_symbol
                    if self._broker_factory is not None:
                        task_fn = partial(
                            evaluate_batch_symbol,
                            broker_factory=self._broker_factory,
                        )
                    outcomes = pm.map_cpu(
                        task_fn, trials,
                        return_exceptions=True, check_pickle=True,
                    )
                for trial, outcome in zip(trials, outcomes):
                    symbol = trial.symbol
                    fp = fps[symbol]
                    if isinstance(outcome, BaseException):
                        self._results[symbol] = {
                            "fingerprint_hash": fp.hash_value,
                            "status": "error",
                            "metrics": None,
                            "trade_count": None,
                            "equity_length": None,
                            "mode": mode,
                            "error": f"{type(outcome).__name__}: {outcome}",
                            "computed_at": datetime.now(timezone.utc).isoformat(),
                        }
                    else:
                        outcome["fingerprint_hash"] = fp.hash_value
                        outcome["computed_at"] = datetime.now(timezone.utc).isoformat()
                        self._results[symbol] = outcome

            # 批次完成后回调（GUI 进度面板刷新用；无回调时零开销）
            if self._on_batch_done is not None:
                self._on_batch_done(batch_idx, len(batches))

        elapsed = time.perf_counter() - eval_t0

        # 更新指纹仓库 + 持久化结果
        # 只同步成功标的的指纹：error 结果不写入指纹仓库，
        # 保证下次增量运行必然重算失败标的（否则 error 被指纹固化永久复用）。
        ok_fps = {
            s: fp
            for s, fp in fps.items()
            if self._results.get(s, {}).get("status") == "completed"
        }
        self._store.sync(ok_fps)
        self._save_results()

        per_symbol: Dict[str, Dict[str, Any]] = {}
        for symbol in sorted(self.data.keys()):
            per_symbol[symbol] = self._results.get(symbol, {
                "status": "error", "error": "结果缓存缺失",
            })

        data_coverage = self._build_data_coverage()
        return BatchBacktestResult(
            plan=plan,
            per_symbol=per_symbol,
            elapsed_seconds=elapsed,
            degradation_events=degradation_events,
            memory_level=level.value,
            batch_count=len(batches),
            data_coverage=data_coverage,
        )

    def _build_data_coverage(self) -> Optional[Dict[str, Any]]:
        """KP-P2-07：批量回测收尾注入数据覆盖审计（幸存者偏差披露）。

        调 audit_data_coverage + estimate_survivorship_discount，写入
        result.data_coverage（报告强制渲染声明节依据）；DB 缺失/折价样本不足
        时降级（audit 声明仍落盘，discount=None），绝不阻断回测结果。
        """
        from myquant.backtest.survivorship_bias import (
            audit_data_coverage,
            estimate_survivorship_discount,
        )

        try:
            audit = audit_data_coverage()
            dc: Dict[str, Any] = audit.to_dict()
            try:
                dc.update(estimate_survivorship_discount())
            except ValueError as exc:
                dc["discount"] = None
                dc["discount_error"] = str(exc)
            return dc
        except Exception as exc:
            logger.warning("幸存者偏差数据覆盖审计失败（降级不阻断）: %s", exc)
            return None


# ── 聚合结果 ─────────────────────────────────────────────────────────
@dataclass
class BatchBacktestResult:
    """批量回测聚合结果。

    Attributes:
        plan: 调度计划（重算/复用/基准引擎模式）。
        per_symbol: {symbol: 结果字典}。
        elapsed_seconds: 并行回测耗时。
        degradation_events: 内存降级事件列表。
        memory_level: 最后批次内存水位。
        batch_count: 实际批次数。
    """

    plan: BatchPlan
    per_symbol: Dict[str, Dict[str, Any]]
    elapsed_seconds: float
    degradation_events: List[Dict[str, Any]]
    memory_level: str
    batch_count: int
    data_coverage: Optional[Dict[str, Any]] = None  # KP-P2-07：幸存者偏差数据覆盖审计

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
        """缓存复用比例（0~1）。"""
        total = self.plan.total
        if total == 0:
            return 0.0
        return self.reused_count / total

    def summary(self) -> str:
        """生成批量回测报告文本。"""
        lines = ["批量回测报告"]
        lines.append("=" * 64)
        lines.append(
            f"标的总数: {self.plan.total} | 基准引擎: {self.plan.base_mode} "
            f"| 批次数: {self.batch_count}"
        )
        lines.append(
            f"重算 {self.recomputed_count} 个 | 复用 {self.reused_count} 个 "
            f"| 失败 {self.error_count} 个 | 复用率 {self.reused_ratio():.1%}"
        )
        lines.append(f"并行回测耗时: {format_duration(self.elapsed_seconds)}")
        lines.append(f"内存水位: {self.memory_level}")
        if self.degradation_events:
            lines.append(f"降级事件: {len(self.degradation_events)} 次")
            for ev in self.degradation_events:
                lines.append(
                    f"  批次{ev['batch']}: {ev['from_mode']} -> {ev['to_mode']} "
                    f"(水位 {ev['memory_level']}, 并行 {ev['cpu_workers']})"
                )
        lines.append("-" * 64)
        lines.append(f"{'标的':<12} {'状态':<10} {'总收益':>9} {'夏普':>8} {'回撤':>9} {'引擎':<6} {'来源':<6}")
        for symbol in sorted(self.per_symbol.keys()):
            entry = self.per_symbol[symbol]
            status = entry.get("status", "error")
            metrics = entry.get("metrics")
            if status == "completed" and metrics is not None:
                src = "重算" if symbol in self.plan.changed else "复用"
                mode = entry.get("mode", "-")
                lines.append(
                    f"{symbol:<12} {status:<10} {metrics['total_return']:>9.2%} "
                    f"{metrics['sharpe_ratio']:>8.3f} {metrics['max_drawdown']:>9.2%} "
                    f"{mode:<6} {src:<6}"
                )
            else:
                err = (entry.get("error") or "未知错误")[:36]
                lines.append(f"{symbol:<12} {status:<10} {'-':>9} {'-':>8} {'-':>9} {'-':<6} 失败:{err}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可读字典（供 JSON 落盘）。"""
        return {
            "force_full": self.plan.force_full,
            "base_mode": self.plan.base_mode,
            "total_count": self.plan.total,
            "recomputed_count": self.recomputed_count,
            "reused_count": self.reused_count,
            "error_count": self.error_count,
            "reused_ratio": round(self.reused_ratio(), 6),
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "memory_level": self.memory_level,
            "batch_count": self.batch_count,
            "degradation_events": self.degradation_events,
            "changed": self.plan.changed,
            "unchanged": self.plan.unchanged,
            "per_symbol": self.per_symbol,
        }


# ── 演示 / 自测入口 ───────────────────────────────────────────────────
def _main() -> int:
    """模块自测：三模式调度 + 指纹增量 + 内存降级策略。"""
    import shutil

    from myquant.backtest.param_optimizer import make_demo_data

    print("=" * 64)
    print("  batch_backtest.py — 批量回测（指纹 + 三模式 + 内存降级）自测 Demo")
    print("=" * 64)

    # 1. 内存降级策略单测（不依赖真实内存）
    print("[1] 内存降级策略（resolve_mode / resolve_cpu）")
    policy = MemoryDegradePolicy()
    assert policy.resolve_mode("vectorized", MemoryLevel.NORMAL) == ("vectorized", False)
    assert policy.resolve_mode("vectorized", MemoryLevel.WARNING) == ("mixed", True)
    assert policy.resolve_mode("vectorized", MemoryLevel.CRITICAL) == ("event", True)
    assert policy.resolve_mode("event", MemoryLevel.WARNING) == ("event", False)
    assert policy.resolve_mode("event", MemoryLevel.CRITICAL) == ("event", False)
    assert policy.resolve_cpu(4, MemoryLevel.NORMAL) == 4
    assert policy.resolve_cpu(4, MemoryLevel.WARNING) == 2
    assert policy.resolve_cpu(4, MemoryLevel.CRITICAL) == 1
    assert policy.resolve_cpu(None, MemoryLevel.NORMAL) == 1
    print("    ✓ 降级链/并行度 8 项断言通过")

    # 2. 构造演示数据 + 批量回测（NORMAL 内存不降级，三模式调度）
    cache_dir = Path(tempfile.mkdtemp(prefix="batch_test_"))
    fp_path = cache_dir / "fingerprints.json"
    res_path = cache_dir / "results.json"
    symbols = ["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH", "600001.SH"]
    data = make_demo_data(symbols=symbols, n_bars=300, seed=7)
    print(f"\n[2] 构造 {len(symbols)} 标的演示行情")

    bt = BatchBacktester(
        "ma_cross", {"fast_period": 5, "slow_period": 20}, data,
        str(fp_path), str(res_path), mode="auto",
        start_date="2023-01-02", end_date="2024-01-31",
    )
    r1 = bt.run(force_full=True)
    print(f"    ✓ 首次全量: 重算 {r1.recomputed_count} | 引擎 {r1.plan.base_mode} | "
          f"降级 {len(r1.degradation_events)} 次")
    assert r1.recomputed_count == len(symbols)
    assert r1.plan.base_mode in MEMORY_DEGRADE_CHAIN
    assert all(
        s["status"] == "completed" for s in r1.per_symbol.values()
    ), "批量回测应全部成功"

    # 3. 指纹增量：修改一标的 → 只重算该标的
    changed_symbol = symbols[-1]
    modified = data[changed_symbol].copy()
    modified.iloc[-1, modified.columns.get_loc("close")] *= 1.1
    data[changed_symbol] = modified
    bt2 = BatchBacktester(
        "ma_cross", {"fast_period": 5, "slow_period": 20}, data,
        str(fp_path), str(res_path), mode="auto",
        start_date="2023-01-02", end_date="2024-01-31",
    )
    r2 = bt2.run()
    print(f"    ✓ 增量: 重算 {r2.recomputed_count} | 复用 {r2.reused_count}")
    assert r2.recomputed_count == 1 and r2.plan.changed == [changed_symbol]
    assert r2.reused_count == len(symbols) - 1

    # 3b. 数据未变化 → 0 重算全部复用（覆盖 changed 为空边界）
    bt2b = BatchBacktester(
        "ma_cross", {"fast_period": 5, "slow_period": 20}, data,
        str(fp_path), str(res_path), mode="auto",
        start_date="2023-01-02", end_date="2024-01-31",
    )
    r2b = bt2b.run()
    print(f"    ✓ 数据未变化: 重算 {r2b.recomputed_count} | 复用 {r2b.reused_count}")
    assert r2b.recomputed_count == 0 and r2b.reused_count == len(symbols)

    # 4. 显式三模式：三种模式都可跑
    print("[4] 显式三模式调度")
    for mode in ("vectorized", "mixed", "event"):
        bt3 = BatchBacktester(
            "ma_cross", {"fast_period": 5, "slow_period": 20},
            {symbols[0]: data[symbols[0]]}, str(fp_path), str(res_path),
            mode=mode, start_date="2023-01-02", end_date="2024-01-31",
        )
        r3 = bt3.run(force_full=True)
        assert r3.per_symbol[symbols[0]]["status"] == "completed", f"{mode} 模式应成功"
        print(f"    ✓ {mode} 模式回测成功")

    # 4b. 批次回调：on_batch_done 逐批触发且批次号正确
    print("[4b] 批次回调（on_batch_done）")
    callback_calls: List[Tuple[int, int]] = []
    bt4 = BatchBacktester(
        "ma_cross", {"fast_period": 5, "slow_period": 20},
        {symbols[0]: data[symbols[0]], symbols[1]: data[symbols[1]]},
        str(fp_path), str(res_path), mode="mixed",
        start_date="2023-01-02", end_date="2024-01-31",
        batch_size=1,
        on_batch_done=lambda i, n: callback_calls.append((i, n)),
    )
    r4 = bt4.run(force_full=True)
    assert callback_calls == [(1, 2), (2, 2)], f"批次回调顺序异常: {callback_calls}"
    assert r4.per_symbol[symbols[0]]["status"] == "completed", "mixed 模式应成功"
    print(f"    ✓ 回调逐批触发: {callback_calls}")

    print("\n" + "=" * 64)
    print("  自测通过: 降级策略 8 项 + 全量批量 + 指纹增量 + 三模式显式调度 + 批次回调")
    print("=" * 64)

    shutil.rmtree(cache_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
