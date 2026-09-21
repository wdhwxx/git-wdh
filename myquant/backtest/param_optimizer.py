# 文件名：myquant/backtest/param_optimizer.py
"""
策略参数优化器（网格搜索 + 并行加速）
====================================
对策略参数网格做穷举评估，接入 P2 并行治理层：
  - ParallelManager 计算进程池并行评估参数组合（多标的 x 多参数组）
  - parallel_factor_context 上下文：进入自动关闭 factor_cache L1 缓存并告警，
    退出自动恢复（支持嵌套），杜绝并行期间 L1 命中率低下的隐性损耗
  - pickle 预检：任务提交进程池前自动拦截不可 pickle 任务（pickle_checker）
  - StageTimer 分阶段计时（由入口脚本接入，定位各阶段耗时）

核心组件:
  - ParamSpec / ParamGrid : 参数网格定义（离散候选值，笛卡尔积展开）
  - ParamTrial            : 单次评估任务（可 pickle，供子进程反序列化）
  - evaluate_params       : 模块级任务函数（进程池可 pickle 的顶层函数）
  - ParamOptimizer        : 优化器（并行调度、指标排序、最优参数输出）
  - ParamOptimizationResult : 聚合结果（summary/to_dict/to_dataframe）

设计原则:
  - Windows spawn 约束：任务函数与策略工厂必须位于本真实模块（可被子进程 import），
    执行入口放 scripts/ 的 if __name__ == "__main__" 下
  - 任务函数永不抛出：子进程内 try/except 捕获，异常记入 status="error"，
    配合 return_exceptions 双重兜底，单组失败不影响整批
  - 并行上下文只包裹评估段，数据准备/排序输出在单进程完成，避免无谓串行
"""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from myquant.backtest.data_loader import require_ohlcv
from myquant.backtest.engine import BacktestEngine
from myquant.models.backtest import BacktestConfig
from myquant.strategies.base import StrategyBase
from myquant.strategies.ma_strategy import MACrossStrategy
from myquant.utils.parallel import ParallelManager, parallel_factor_context
from myquant.utils.timer import format_duration

logger = logging.getLogger("myquant.backtest.param_optimizer")

# ── 常量（禁魔法数字） ──────────────────────────────────────────────
DEFAULT_OBJECTIVE: str = "sharpe_ratio"
DEFAULT_INITIAL_CAPITAL: float = 1_000_000.0
DEFAULT_FREQUENCY: str = "1d"
RANK_TOP_N: int = 10

# 目标指标白名单：指标名 -> 排序方向（True=越大越好，False=越小越好）
# 注：max_drawdown 为负值，越小（越接近 0）越好，故方向为 False
OBJECTIVE_DIRECTION: Dict[str, bool] = {
    "sharpe_ratio": True,
    "total_return": True,
    "max_drawdown": False,
}

# 策略注册表：策略名 -> 策略类（扩展新策略只需在此注册）
STRATEGY_REGISTRY: Dict[str, type] = {
    "ma_cross": MACrossStrategy,
}


# ── 参数网格定义 ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class ParamSpec:
    """单个参数的候选值集合（离散、去重保序）。

    Attributes:
        name: 参数名（须与策略构造参数一致）。
        values: 候选值元组（不可变、非空、去重保序）。
    """

    name: str
    values: Tuple[Any, ...]

    def __post_init__(self) -> None:
        """防御式校验：参数名非空字符串，候选值非空。"""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(f"参数名必须为非空字符串，收到: {self.name!r}")
        if not isinstance(self.values, (tuple, list)) or len(self.values) == 0:
            raise ValueError(f"参数 {self.name} 的候选值集合不能为空")
        # 冻结为元组并去重保序
        seen: List[Any] = []
        for v in self.values:
            if v not in seen:
                seen.append(v)
        object.__setattr__(self, "values", tuple(seen))

    def __len__(self) -> int:
        """候选值数量。"""
        return len(self.values)


@dataclass(frozen=True)
class ParamGrid:
    """参数网格：多个 ParamSpec 的笛卡尔积组合。

    Attributes:
        specs: 参数规格元组（至少一个）。
    """

    specs: Tuple[ParamSpec, ...]

    def __post_init__(self) -> None:
        """防御式校验：至少一个参数规格。"""
        if not isinstance(self.specs, (tuple, list)) or len(self.specs) == 0:
            raise ValueError("参数网格至少需要一个参数规格")
        names = [s.name for s in self.specs]
        if len(names) != len(set(names)):
            raise ValueError(f"参数网格存在重复参数名: {names}")

    def __len__(self) -> int:
        """组合总数（笛卡尔积）。"""
        total: int = 1
        for spec in self.specs:
            total *= len(spec)
        return total

    def combinations(self) -> List[Dict[str, Any]]:
        """展开为全部参数组合字典列表（笛卡尔积，按规格顺序）。"""
        value_lists: List[List[Any]] = [list(spec.values) for spec in self.specs]
        result: List[Dict[str, Any]] = []
        for combo in itertools.product(*value_lists):
            result.append(dict(zip([s.name for s in self.specs], combo)))
        return result


# ── 评估任务与任务函数（可 pickle） ───────────────────────────────────
@dataclass(frozen=True)
class ParamTrial:
    """单次参数评估任务（可被子进程反序列化）。

    Attributes:
        params: 本组参数组合字典。
        strategy_name: 策略注册名（见 STRATEGY_REGISTRY）。
        symbol: 标的代码。
        data: 单标的行情字典 {symbol: DataFrame}（只含本标的，减小序列化体积）。
        start_date: 回测起始日期（YYYY-MM-DD）。
        end_date: 回测结束日期（YYYY-MM-DD）。
        initial_capital: 初始资金。
        frequency: K 线频率。
    """

    params: Dict[str, Any]
    strategy_name: str
    symbol: str
    data: Dict[str, pd.DataFrame]
    start_date: str
    end_date: str
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    frequency: str = DEFAULT_FREQUENCY

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可读字典（data 仅记标的名，避免整块数据入报告）。"""
        return {
            "params": self.params,
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "frequency": self.frequency,
        }


def create_strategy(name: str, params: Dict[str, Any]) -> StrategyBase:
    """按注册名创建策略实例（模块级工厂，供子进程 pickle 引用）。

    Args:
        name: 策略注册名。
        params: 策略构造参数（含默认值合并）。

    Returns:
        策略实例。

    Raises:
        ValueError: 策略名未注册。
        TypeError: 策略构造参数非法。
    """
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"未知策略: {name!r}，已注册策略: {sorted(STRATEGY_REGISTRY)}"
        )
    return cls(**params)


def evaluate_params(trial: ParamTrial) -> Dict[str, Any]:
    """模块级任务函数：在子进程中评估一组参数，返回指标字典（永不抛出）。

    任何异常都被捕获并记入 status="error"（含错误类型与消息），
    保证进程池 map_cpu 不因单组失败中断整批评估。

    Args:
        trial: 评估任务。

    Returns:
        指标字典：
            - params: 参数组合
            - symbol: 标的
            - status: "completed" / "error"
            - total_return / sharpe_ratio / max_drawdown / trade_count: 绩效
            - error: 错误信息（status="error" 时非空）
    """
    base: Dict[str, Any] = {
        "params": trial.params,
        "symbol": trial.symbol,
        "status": "error",
        "total_return": None,
        "sharpe_ratio": None,
        "max_drawdown": None,
        "trade_count": None,
        "error": None,
    }
    try:
        strategy = create_strategy(trial.strategy_name, trial.params)
        config = BacktestConfig(
            strategy_name=trial.strategy_name,
            symbols=[trial.symbol],
            start_date=trial.start_date,
            end_date=trial.end_date,
            frequency=trial.frequency,
            initial_capital=trial.initial_capital,
            params=trial.params,
        )
        engine = BacktestEngine(config=config, strategy=strategy, data=trial.data)
        result = engine.run()
        metrics = result.metrics
        base.update(
            {
                "status": "completed",
                "total_return": float(metrics.total_return),
                "sharpe_ratio": float(metrics.sharpe_ratio),
                "max_drawdown": float(metrics.max_drawdown),
                "trade_count": int(len(result.trades)),
            }
        )
    except Exception as exc:  # noqa: BLE001 - 任务函数必须隔离所有异常
        base["error"] = f"{type(exc).__name__}: {exc}"
    return base


# ── 优化结果容器 ─────────────────────────────────────────────────────
@dataclass
class ParamOptimizationResult:
    """参数优化聚合结果。

    Attributes:
        strategy_name: 策略注册名。
        objective: 目标指标名。
        trials: 全部评估结果字典列表（含错误组）。
        elapsed_seconds: 并行评估总耗时。
    """

    strategy_name: str
    objective: str
    trials: List[Dict[str, Any]]
    elapsed_seconds: float

    # ── 派生统计（惰性缓存字段） ──
    _completed: Optional[List[Dict[str, Any]]] = None
    _error_count: Optional[int] = None

    @property
    def total_count(self) -> int:
        """评估任务总数。"""
        return len(self.trials)

    @property
    def completed_count(self) -> int:
        """成功完成任务数。"""
        return len(self._completed_outcomes())

    @property
    def error_count(self) -> int:
        """失败任务数。"""
        if self._error_count is None:
            self._error_count = sum(
                1 for t in self.trials if t.get("status") != "completed"
            )
        return self._error_count

    def _completed_outcomes(self) -> List[Dict[str, Any]]:
        """缓存成功评估结果列表。"""
        if self._completed is None:
            self._completed = [
                t for t in self.trials if t.get("status") == "completed"
            ]
        return self._completed

    def _score(self, outcome: Dict[str, Any]) -> Optional[float]:
        """按目标指标对单条结果打分；非 completed 或无该指标返回 None。"""
        value = outcome.get(self.objective)
        if value is None:
            return None
        return float(value)

    def ranked(self, top: int = RANK_TOP_N) -> List[Dict[str, Any]]:
        """按目标指标排序返回前 top 名（失败组恒排最后）。

        Args:
            top: 返回条数上限（非负整数）。

        Returns:
            排序后的结果字典列表，每条附加 "_rank" 排名。
        """
        if top < 0:
            raise ValueError(f"top 必须为非负整数，收到: {top!r}")
        direction = OBJECTIVE_DIRECTION[self.objective]
        scored = [(t, self._score(t)) for t in self.trials]
        # 有分值的按方向排序在前，None（失败/缺指标）按原始顺序排最后
        keyed = [(t, s, -s if (s is not None and direction) else s) for t, s in scored]
        ordered = sorted(
            keyed,
            key=lambda x: (x[1] is None, x[2] if x[1] is not None else 0),
            reverse=False,
        )
        ranked_list: List[Dict[str, Any]] = []
        for rank, (t, _s, _k) in enumerate(ordered[:top], start=1):
            item = dict(t)
            item["_rank"] = rank
            ranked_list.append(item)
        return ranked_list

    def best(self) -> Optional[Dict[str, Any]]:
        """返回最优参数组合（无成功结果时返回 None）。"""
        if self.completed_count == 0:
            return None
        return self.ranked(top=1)[0]

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可读字典（供 JSON 落盘）。"""
        return {
            "strategy_name": self.strategy_name,
            "objective": self.objective,
            "total_count": self.total_count,
            "completed_count": self.completed_count,
            "error_count": self.error_count,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "best": self.best(),
            "trials": self.trials,
        }

    def to_dataframe(self) -> pd.DataFrame:
        """转 DataFrame：每行一条结果，params 展开为独立列，失败组指标置空。"""
        rows: List[Dict[str, Any]] = []
        for t in self.trials:
            row = {
                "symbol": t.get("symbol"),
                "status": t.get("status"),
                "total_return": t.get("total_return"),
                "sharpe_ratio": t.get("sharpe_ratio"),
                "max_drawdown": t.get("max_drawdown"),
                "trade_count": t.get("trade_count"),
                "error": t.get("error"),
            }
            for k, v in (t.get("params") or {}).items():
                row[f"param_{k}"] = v
            rows.append(row)
        return pd.DataFrame(rows)

    def summary(self) -> str:
        """生成优化汇总报告文本（含最优参数与 Top 榜）。"""
        lines = ["策略参数优化报告"]
        lines.append("=" * 64)
        lines.append(f"策略: {self.strategy_name} | 目标: {self.objective}")
        lines.append(
            f"任务: {self.total_count}（成功 {self.completed_count} / "
            f"失败 {self.error_count}）| 评估耗时: {format_duration(self.elapsed_seconds)}"
        )
        best = self.best()
        if best is not None:
            lines.append("-" * 64)
            lines.append("最优参数组合:")
            lines.append(f"  {best.get('params')}")
            lines.append(
                f"  总收益 {best.get('total_return'):.2%} | "
                f"夏普 {best.get('sharpe_ratio'):.3f} | "
                f"最大回撤 {best.get('max_drawdown'):.2%} | "
                f"交易数 {best.get('trade_count')}"
            )
        top = self.ranked(top=RANK_TOP_N)
        if top:
            lines.append("-" * 64)
            lines.append(f"Top {len(top)} 排名:")
            lines.append(f"{'排名':<5} {'标的':<12} {'参数':<30} {'夏普':>8} {'收益':>8} {'回撤':>8}")
            for item in top:
                lines.append(
                    f"{item['_rank']:<5} {str(item.get('symbol')):<12} "
                    f"{str(item.get('params')):<30} "
                    f"{float(item['sharpe_ratio'] or 0):>8.3f} "
                    f"{float(item['total_return'] or 0):>8.2%} "
                    f"{float(item['max_drawdown'] or 0):>8.2%}"
                )
        return "\n".join(lines)


# ── 优化器 ────────────────────────────────────────────────────────────
class ParamOptimizer:
    """策略参数网格搜索优化器（进程池并行评估）。

    Args:
        strategy_name: 策略注册名（见 STRATEGY_REGISTRY）。
        grid: 参数网格。
        data: 多标的行情字典 {symbol: DataFrame}（含 close 列）。
        objective: 目标指标（见 OBJECTIVE_DIRECTION）。
        cpu_workers: 计算进程池大小；None 使用默认。
        start_date: 回测起始日期。
        end_date: 回测结束日期。
        initial_capital: 初始资金（>0）。
        frequency: K 线频率。

    Example:
        >>> grid = ParamGrid((ParamSpec("fast_period", (3, 5, 8)),))
        >>> opt = ParamOptimizer("ma_cross", grid, data)
        >>> result = opt.optimize()
    """

    def __init__(
        self,
        strategy_name: str,
        grid: ParamGrid,
        data: Dict[str, pd.DataFrame],
        objective: str = DEFAULT_OBJECTIVE,
        cpu_workers: Optional[int] = None,
        start_date: str = "",
        end_date: str = "",
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        frequency: str = DEFAULT_FREQUENCY,
    ) -> None:
        """初始化优化器并做防御式参数校验。"""
        if not isinstance(strategy_name, str) or not strategy_name.strip():
            raise ValueError("strategy_name 必须为非空字符串")
        if strategy_name not in STRATEGY_REGISTRY:
            raise ValueError(
                f"未知策略: {strategy_name!r}，已注册: {sorted(STRATEGY_REGISTRY)}"
            )
        if not isinstance(grid, ParamGrid):
            raise TypeError(f"grid 必须为 ParamGrid，收到: {type(grid).__name__}")
        if not isinstance(data, dict) or len(data) == 0:
            raise ValueError("data 必须为非空字典 {symbol: DataFrame}")
        if objective not in OBJECTIVE_DIRECTION:
            raise ValueError(
                f"未知目标指标: {objective!r}，可选: {sorted(OBJECTIVE_DIRECTION)}"
            )
        if cpu_workers is not None and (not isinstance(cpu_workers, int) or cpu_workers < 1):
            raise ValueError(f"cpu_workers 必须为正整数或 None，收到: {cpu_workers!r}")
        if not isinstance(initial_capital, (int, float)) or initial_capital <= 0:
            raise ValueError(f"initial_capital 必须为正数，收到: {initial_capital!r}")
        if not isinstance(frequency, str) or not frequency.strip():
            raise ValueError("frequency 必须为非空字符串")

        # 校验行情列（防御：缺 OHLCV 列导致子进程集体失败；中文列优先，兼容英文列）
        for symbol, df in data.items():
            if not isinstance(df, pd.DataFrame):
                raise TypeError(f"标的 {symbol} 的数据必须为 DataFrame")
            require_ohlcv(df, symbol=symbol)

        self.strategy_name = strategy_name
        self.grid = grid
        self.data = data
        self.objective = objective
        self.cpu_workers = cpu_workers
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.frequency = frequency

    def _build_trials(self) -> List[ParamTrial]:
        """展开全部任务：参数组合 x 标的，每任务只携带本标的行情。"""
        trials: List[ParamTrial] = []
        for combo in self.grid.combinations():
            for symbol, df in self.data.items():
                trials.append(
                    ParamTrial(
                        params=combo,
                        strategy_name=self.strategy_name,
                        symbol=symbol,
                        data={symbol: df},
                        start_date=self.start_date,
                        end_date=self.end_date,
                        initial_capital=self.initial_capital,
                        frequency=self.frequency,
                    )
                )
        return trials

    def optimize(self) -> ParamOptimizationResult:
        """执行并行网格搜索。

        并行上下文只包裹评估段：进入自动关闭 factor_cache L1 并告警，
        退出自动恢复；进程池提交前做 pickle 预检拦截不可序列化任务。

        Returns:
            聚合结果（含最优参数、Top 榜、耗时）。
        """
        trials = self._build_trials()
        total = len(trials)
        if total == 0:
            raise ValueError("参数网格展开后无任何任务（数据或网格为空）")

        logger.info("参数优化启动: %d 组任务（%d 参数组合 x %d 标的）",
                    total, len(self.grid), len(self.data))

        def _progress(done: int, count: int) -> None:
            """进度回显（仅 logging.info，避免刷屏）。"""
            logger.info("并行评估进度: %d/%d", done, count)

        # 并行上下文：自动管理缓存开关（L1 关闭 + 告警），退出自动恢复
        eval_t0 = time.perf_counter()
        with parallel_factor_context(cpu_workers=self.cpu_workers) as pm:
            outcomes = pm.map_cpu(
                evaluate_params,
                trials,
                return_exceptions=True,
                check_pickle=True,
                progress_cb=_progress,
            )
        elapsed = time.perf_counter() - eval_t0

        # 归一化：异常对象（兜底）转为 error 结果字典
        normalized: List[Dict[str, Any]] = []
        for trial, outcome in zip(trials, outcomes):
            if isinstance(outcome, BaseException):
                normalized.append(
                    {
                        "params": trial.params,
                        "symbol": trial.symbol,
                        "status": "error",
                        "total_return": None,
                        "sharpe_ratio": None,
                        "max_drawdown": None,
                        "trade_count": None,
                        "error": f"{type(outcome).__name__}: {outcome}",
                    }
                )
            else:
                normalized.append(outcome)

        errors = sum(1 for o in normalized if o.get("status") != "completed")
        if errors:
            logger.warning("参数优化完成: %d/%d 组失败（已隔离，不影响其余）",
                           errors, total)

        return ParamOptimizationResult(
            strategy_name=self.strategy_name,
            objective=self.objective,
            trials=normalized,
            elapsed_seconds=elapsed,
        )


# ── 演示 / 自测入口 ───────────────────────────────────────────────────
def make_demo_data(
    symbols: Sequence[str] = ("000001.SZ", "000002.SZ", "600000.SH"),
    n_bars: int = 300,
    seed: int = 42,
) -> Dict[str, pd.DataFrame]:
    """构造确定性演示行情（趋势 + 周期噪声），供脚本与自测使用。

    Args:
        symbols: 标的代码序列。
        n_bars: 每标的 K 线数量（>20）。
        seed: 随机种子（同 seed 结果可复现）。

    Returns:
        {symbol: DataFrame}，列为 open/high/low/close/volume/amount，
        index 为工作日 DatetimeIndex。

    Raises:
        ValueError: n_bars 过小。
    """
    if n_bars < 20:
        raise ValueError(f"n_bars 至少 20 根以便均线预热，收到: {n_bars}")
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-02", periods=n_bars, freq="B")
    data: Dict[str, pd.DataFrame] = {}
    for idx, symbol in enumerate(symbols):
        drift = 0.0006 + 0.0004 * idx
        noise = rng.normal(0.0, 0.01, n_bars)
        trend = drift * np.arange(n_bars)
        wave = 0.012 * np.sin(np.arange(n_bars) / (18.0 + idx))
        closes = 10.0 * (1.0 + trend + noise + wave)
        data[symbol] = pd.DataFrame(
            {
                "open": closes * 0.997,
                "high": closes * 1.01,
                "low": closes * 0.99,
                "close": closes,
                "volume": 100_000.0 * (1.0 + 0.5 * rng.random(n_bars)),
                "amount": closes * 100_000.0,
            },
            index=dates,
        )
    return data


def _main() -> int:
    """模块自测：小网格 + 3 标的，验证并行接入与最优参数输出。"""
    import time

    print("=" * 64)
    print("  param_optimizer.py — 参数优化（并行接入）自测 Demo")
    print("=" * 64)

    # 1. 构造演示数据（确定性）
    data = make_demo_data(n_bars=300, seed=7)

    # 2. 参数网格：fast/slow 双均线
    grid = ParamGrid(
        (
            ParamSpec("fast_period", (3, 5, 8, 12)),
            ParamSpec("slow_period", (15, 25, 40)),
        )
    )
    print(f"[数据] {len(data)} 标的，每标的 {300} 根 K 线")
    print(f"[网格] {len(grid)} 组参数组合 x {len(data)} 标的 = "
          f"{len(grid) * len(data)} 组任务")

    # 3. 执行优化（parallel_factor_context 自动管理缓存开关）
    t0 = time.perf_counter()
    optimizer = ParamOptimizer(
        strategy_name="ma_cross",
        grid=grid,
        data=data,
        objective="sharpe_ratio",
        start_date="2023-01-02",
        end_date="2024-01-31",
    )
    result = optimizer.optimize()
    elapsed = time.perf_counter() - t0

    # 4. 输出
    print(result.summary())
    best = result.best()
    assert best is not None, "应有最优参数"
    assert best["status"] == "completed", "最优参数应来自成功组"
    assert result.completed_count + result.error_count == result.total_count
    assert all(t["status"] in ("completed", "error") for t in result.trials)
    print(f"[总耗时] {elapsed:.3f}s（并行评估 {result.elapsed_seconds:.3f}s）")
    print("=" * 64)
    print(f"  自测通过: {result.total_count} 组任务，"
          f"成功 {result.completed_count} / 失败 {result.error_count}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
