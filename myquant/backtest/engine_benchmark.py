# 文件名：myquant/backtest/engine_benchmark.py
"""
backtest/engine_benchmark.py - P3-3 三模式回测引擎性能基准
==========================================================
对 event / mixed / vectorized 三套引擎在同一策略、同一数据上做
可复现的性能基准采集（耗时 / 吞吐量 / 相对加速比），输出结构化报告。

设计原则（代码规则）:
  - 业务逻辑全量实现，无占位符；防御式编程显式校验输入边界。
  - 严格防未来函数：只消费各引擎已产出的回测结果，不引入未来数据。
  - 可复现：演示行情用确定性随机种子（同 seed 结果一致）。
  - 口径透明：耗时取多次运行平均值（预热一次不计时），
    吞吐量 = 全部标的 bar 总数 / 平均耗时（bar/秒），
    相对加速比以 event（最慢的事件引擎）为基准 =1.0。
  - 演示策略复用 consistency_check.DemoThreeModeStrategy（三模式均可跑）。

对外组件:
  - EngineBenchmarkConfig   : 基准配置（规模 / 重复次数 / 种子）
  - EngineBenchmarkResult   : 单引擎基准结果（耗时 / 吞吐 / 成交 / 期末净值）
  - EngineBenchmarkReport   : 三模式基准汇总报告（含相对加速比）
  - EngineBenchmark         : 基准执行器（预热 + 重复计时 + 汇总）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from myquant.backtest.consistency_check import (
    DemoThreeModeStrategy,
    make_demo_data,
)
from myquant.core.logger import get_logger
from myquant.models.backtest import BacktestConfig, BacktestResult

logger = get_logger(__name__)

# ═══════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════

ENGINE_EVENT: str = "event"
ENGINE_MIXED: str = "mixed"
ENGINE_VECTORIZED: str = "vectorized"
DEFAULT_ENGINES: tuple = (ENGINE_EVENT, ENGINE_MIXED, ENGINE_VECTORIZED)

# 基准默认规模（快速/可复现）
DEFAULT_N_BARS: int = 500
DEFAULT_N_SYMBOLS: int = 2
DEFAULT_REPETITIONS: int = 3
DEFAULT_SEED: int = 7

# 快速模式规模（--quick）
QUICK_N_BARS: int = 200
QUICK_N_SYMBOLS: int = 1
QUICK_REPETITIONS: int = 1

# 相对加速比基准引擎（最慢的事件引擎）
BASELINE_ENGINE: str = ENGINE_EVENT

# 计时次数下限
_MIN_REPETITIONS: int = 1


# ═══════════════════════════════════════════════
#  配置与结果
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class EngineBenchmarkConfig:
    """三模式性能基准配置。

    Attributes:
        n_bars: 每标的 K 线数量（>= 20）。
        n_symbols: 标的数量（>= 1）。
        repetitions: 每引擎计时重复次数（>= 1，取平均）。
        seed: 演示行情随机种子（确定性可复现）。
        warmup: 是否先跑一次预热（不计时），默认 True。
    """

    n_bars: int = DEFAULT_N_BARS
    n_symbols: int = DEFAULT_N_SYMBOLS
    repetitions: int = DEFAULT_REPETITIONS
    seed: int = DEFAULT_SEED
    warmup: bool = True

    def __post_init__(self) -> None:
        """防御校验：规模与重复次数必须合法。"""
        if self.n_bars < 20:
            raise ValueError(f"n_bars 至少 20 根，收到: {self.n_bars}")
        if self.n_symbols < 1:
            raise ValueError(f"n_symbols 必须 >= 1，收到: {self.n_symbols}")
        if self.repetitions < _MIN_REPETITIONS:
            raise ValueError(
                f"repetitions 必须 >= {_MIN_REPETITIONS}，收到: {self.repetitions}"
            )

    @property
    def total_bars(self) -> int:
        """全部标的 bar 总数（吞吐量计算分母）。"""
        return self.n_bars * self.n_symbols


@dataclass
class EngineBenchmarkResult:
    """单引擎基准结果。

    Attributes:
        engine: 引擎模式（event/mixed/vectorized）。
        elapsed: 平均耗时（秒）。
        bars_per_sec: 吞吐量（bar/秒）。
        trades: 成交笔数。
        final_equity: 期末净值。
        is_success: 回测是否成功。
        error: 失败原因（成功时为空）。
    """

    engine: str
    elapsed: float
    bars_per_sec: float
    trades: int
    final_equity: float
    is_success: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为 JSON 可序列化字典。"""
        return {
            "engine": self.engine,
            "elapsed_s": round(self.elapsed, 6),
            "bars_per_sec": round(self.bars_per_sec, 2),
            "trades": self.trades,
            "final_equity": round(self.final_equity, 6),
            "is_success": self.is_success,
            "error": self.error,
        }


@dataclass
class EngineBenchmarkReport:
    """三模式性能基准汇总报告。

    Attributes:
        config: 基准配置。
        results: 各引擎基准结果（顺序与 DEFAULT_ENGINES 一致）。
        speedups: {engine: 相对 BASELINE_ENGINE 的加速比}（基准引擎自身为 1.0）。
    """

    config: EngineBenchmarkConfig
    results: list[EngineBenchmarkResult] = field(default_factory=list)
    speedups: dict[str, float] = field(default_factory=dict)

    @property
    def baseline(self) -> str:
        """相对加速比基准引擎。"""
        return BASELINE_ENGINE

    def to_dict(self) -> dict[str, Any]:
        """导出为 JSON 可序列化字典。"""
        return {
            "config": {
                "n_bars": self.config.n_bars,
                "n_symbols": self.config.n_symbols,
                "repetitions": self.config.repetitions,
                "seed": self.config.seed,
                "warmup": self.config.warmup,
                "total_bars": self.config.total_bars,
            },
            "baseline": self.baseline,
            "results": [r.to_dict() for r in self.results],
            "speedups": self.speedups,
        }

    def summary(self) -> str:
        """格式化摘要输出（CLI 与自测展示）。"""
        lines: list[str] = [
            "=" * 68,
            (
                f"  三模式回测引擎性能基准 | 规模: {self.config.n_symbols} 标的 × "
                f"{self.config.n_bars} 根 × {self.config.repetitions} 次(平均)"
            ),
            f"  基准: {self.baseline}（最慢事件引擎）= 1.0x",
            "=" * 68,
        ]
        for r in self.results:
            mark = "✅" if r.is_success else "❌"
            lines.append(
                f"  {mark} [{r.engine:<10}] 平均耗时={r.elapsed:>8.4f}s "
                f"吞吐量={r.bars_per_sec:>10.2f} bar/s "
                f"成交={r.trades:>4} 期末净值={r.final_equity:.6f}"
            )
            if not r.is_success and r.error:
                lines.append(f"        失败原因: {r.error}")
        lines.append("-" * 68)
        for engine in DEFAULT_ENGINES:
            sp = self.speedups.get(engine, 1.0)
            label = "基准" if engine == self.baseline else f"{sp:.2f}x"
            lines.append(f"  {engine:<10} 相对加速比: {label}")
        lines.append("=" * 68)
        return "\n".join(lines)


# ═══════════════════════════════════════════════
#  基准执行器
# ═══════════════════════════════════════════════

class EngineBenchmark:
    """三模式回测引擎性能基准执行器。

    用法:
        benchmark = EngineBenchmark(EngineBenchmarkConfig())
        report = benchmark.run()
        print(report.summary())
    """

    def __init__(
        self,
        config: EngineBenchmarkConfig | None = None,
        engines: tuple = DEFAULT_ENGINES,
    ) -> None:
        """初始化基准执行器。

        Args:
            config: 基准配置；None 时用默认。
            engines: 参与基准的引擎模式序列。

        Raises:
            ValueError: 含非法引擎模式。
        """
        self._config = config or EngineBenchmarkConfig()
        invalid = [e for e in engines if e not in DEFAULT_ENGINES]
        if invalid:
            raise ValueError(
                f"非法引擎模式: {invalid}（支持: {', '.join(DEFAULT_ENGINES)}）"
            )
        self._engines: tuple = tuple(engines)

    # ── 数据与配置构造 ──

    def _build_data(self) -> dict[str, pd.DataFrame]:
        """构造确定性演示行情。"""
        return make_demo_data(
            symbols=[f"BENCH{i:06d}.SZ" for i in range(self._config.n_symbols)],
            n_bars=self._config.n_bars,
            seed=self._config.seed,
        )

    def _build_backtest_config(self) -> BacktestConfig:
        """构造回测配置。"""
        return BacktestConfig(
            strategy_name="demo_three_mode",
            symbols=[f"BENCH{i:06d}.SZ" for i in range(self._config.n_symbols)],
            start_date="2023-01-02",
            end_date="2024-01-31",
            initial_capital=1_000_000.0,
            frequency="1d",
        )

    # ── 单次计时运行 ──

    @staticmethod
    def _run_engine(
        engine: str,
        config_bt: BacktestConfig,
        strategy: DemoThreeModeStrategy,
        data: dict[str, pd.DataFrame],
    ) -> BacktestResult:
        """运行单引擎一次回测（复用一致性校验的显式构造，避免依赖调度器）。

        Args:
            engine: 引擎模式。
            config_bt: 回测配置。
            strategy: 三模式演示策略。
            data: 行情数据。

        Returns:
            BacktestResult。

        Raises:
            ValueError: 非法引擎模式。
        """
        from myquant.backtest.engine import BacktestEngine
        from myquant.backtest.mixed_engine import MixedBacktestEngine
        from myquant.backtest.vectorized_engine import VectorizedBacktestEngine

        if hasattr(strategy, "reset"):
            strategy.reset()
        if engine == ENGINE_VECTORIZED:
            eng: Any = VectorizedBacktestEngine(
                config=config_bt, strategy=strategy, data=data
            )
        elif engine == ENGINE_MIXED:
            eng = MixedBacktestEngine(config=config_bt, strategy=strategy, data=data)
        elif engine == ENGINE_EVENT:
            eng = BacktestEngine(config=config_bt, strategy=strategy, data=data)
        else:
            raise ValueError(f"非法引擎模式: {engine!r}")
        return eng.run()

    def _time_engine(
        self,
        engine: str,
        config_bt: BacktestConfig,
        strategy: DemoThreeModeStrategy,
        data: dict[str, pd.DataFrame],
    ) -> tuple[BacktestResult, float]:
        """对单引擎做 预热(可选) + 重复计时，返回 (末次结果, 平均耗时)。

        Args:
            engine: 引擎模式。
            config_bt: 回测配置。
            strategy: 三模式演示策略。
            data: 行情数据。

        Returns:
            (最后一次回测结果, 平均耗时秒)。
        """
        if self._config.warmup:
            self._run_engine(engine, config_bt, strategy, data)

        times: list[float] = []
        last_result: BacktestResult | None = None
        for _ in range(self._config.repetitions):
            t0 = time.perf_counter()
            last_result = self._run_engine(engine, config_bt, strategy, data)
            times.append(time.perf_counter() - t0)
        avg = float(np.mean(times)) if times else 0.0
        assert last_result is not None
        return last_result, avg

    # ── 主入口 ──

    def run(self) -> EngineBenchmarkReport:
        """运行三模式性能基准并汇总。

        Returns:
            EngineBenchmarkReport 汇总报告。
        """
        data = self._build_data()
        config_bt = self._build_backtest_config()
        strategy = DemoThreeModeStrategy()

        results: list[EngineBenchmarkResult] = []
        for engine in self._engines:
            result_bt, avg = self._time_engine(
                engine, config_bt, strategy, data
            )
            is_success = result_bt.is_success
            results.append(
                EngineBenchmarkResult(
                    engine=engine,
                    elapsed=avg,
                    bars_per_sec=self._config.total_bars / avg if avg > 0 else 0.0,
                    trades=len(result_bt.trades),
                    final_equity=result_bt.final_equity,
                    is_success=is_success,
                    error="" if is_success else result_bt.error_message,
                )
            )
            logger.info(
                "基准完成: engine=%s, avg=%.4fs, throughput=%.2f bar/s, success=%s",
                engine, avg, results[-1].bars_per_sec, is_success,
            )

        # 相对加速比：以 baseline 引擎为 1.0
        baseline_elapsed: float | None = None
        for r in results:
            if r.engine == BASELINE_ENGINE and r.elapsed > 0:
                baseline_elapsed = r.elapsed
        speedups: dict[str, float] = {}
        for r in results:
            if r.engine == BASELINE_ENGINE:
                speedups[r.engine] = 1.0
            elif baseline_elapsed and r.elapsed > 0:
                speedups[r.engine] = baseline_elapsed / r.elapsed
            else:
                speedups[r.engine] = 0.0

        return EngineBenchmarkReport(
            config=self._config,
            results=results,
            speedups=speedups,
        )


# ═══════════════════════════════════════════════
#  模块自测
# ═══════════════════════════════════════════════

def _main() -> int:
    """模块自测：快速三模式性能基准并输出报告。"""
    print("=" * 68)
    print("  engine_benchmark.py — 三模式回测引擎性能基准 自测 Demo")
    print("=" * 68)

    config = EngineBenchmarkConfig(
        n_bars=QUICK_N_BARS,
        n_symbols=QUICK_N_SYMBOLS,
        repetitions=QUICK_REPETITIONS,
        seed=DEFAULT_SEED,
    )
    benchmark = EngineBenchmark(config)
    report = benchmark.run()

    print(report.summary())
    print(f"  自测结论: 三模式性能基准采集完成（{report.config.total_bars} bars）")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
