# 文件名：tests/benchmarks/test_engine_throughput.py
"""
回测引擎吞吐量基准测试
========================
测试回测引擎每秒处理 bar 数（bars/sec），
覆盖单策略单标的、多标的、含指标计算等场景。

运行方式：
  独立运行：python tests/benchmarks/test_engine_throughput.py
  pytest收集：pytest tests/benchmarks/test_engine_throughput.py -v

阈值（conftest.py 中配置）：
  引擎吞吐量 > 500 bars/sec（5000根K线+均线策略）

设计原则：
  1. 全部使用历史数据（合成K线 2024-01），严格禁用未来函数
  2. 吞吐量直接反映实盘承载能力（历史回测速度 = 实盘处理上限参考）
  3. 可独立运行也可 pytest 收集
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

# 路径配置
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from _benchmark_utils import (  # noqa: E402
    RATIO_LIMIT,
    BenchmarkRunner,
    make_synthetic_bars,
)
from conftest import BENCH_ITERATIONS, BENCH_WARMUP  # noqa: E402

from myquant.backtest.engine import BacktestEngine  # noqa: E402
from myquant.models.backtest import BacktestConfig  # noqa: E402
from myquant.strategies.ma_strategy import MACrossStrategy  # noqa: E402


def _to_engine_df(bars: List[Dict[str, Any]]) -> pd.DataFrame:
    """将K线字典列表转为引擎所需的 DataFrame（DatetimeIndex）"""
    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["date"], format="ISO8601")
    df = df.set_index("date")
    return df


def _make_engine(bars: List[Dict[str, Any]], symbol: str = "PERF.SZ") -> BacktestEngine:
    """创建回测引擎（均线策略+合成数据）"""
    df = _to_engine_df(bars)
    strat = MACrossStrategy(fast_period=5, slow_period=20)
    config = BacktestConfig(initial_capital=100000)
    return BacktestEngine(config=config, strategy=strat, data={symbol: df})


# ═══════════════════════════════════════════
#  吞吐量基准
# ═══════════════════════════════════════════

def test_engine_throughput_5k():
    """回测引擎吞吐量：5000根K线 + 均线策略"""
    bars = make_synthetic_bars(n=5000)
    n_bars = len(bars)

    def _run():
        engine = _make_engine(bars)
        engine.run()

    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    result = runner.run(f"引擎吞吐量({n_bars}根+均线策略)", _run)
    # 计算 bars/sec
    throughput = n_bars / (result.mean_ms / 1000.0)
    print(result.format())
    # P2-6 相对化：吞吐量换算自耗时，判定改为相对基线倍数（硬件无关，替代绝对 bars/sec）
    if result.baseline_ms is not None:
        print(f"    吞吐量:   {throughput:.1f} bars/sec (相对基线判定, ratio={result.ratio_to_baseline:.2f}× ≤ {RATIO_LIMIT:.1f}×)")
    assert result.ratio_to_baseline is None or result.ratio_to_baseline <= RATIO_LIMIT, (
        f"引擎吞吐量退化 {result.ratio_to_baseline:.2f}× 超相对基线上限 {RATIO_LIMIT:.1f}×"
    )


def test_engine_throughput_1k():
    """回测引擎吞吐量：1000根K线 + 均线策略（小数据集延迟）"""
    bars = make_synthetic_bars(n=1000)
    n_bars = len(bars)

    def _run():
        engine = _make_engine(bars)
        engine.run()

    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    result = runner.run(f"引擎吞吐量({n_bars}根+均线策略)", _run, threshold_ms=5000.0)
    throughput = n_bars / (result.mean_ms / 1000.0)
    print(result.format())
    print(f"    吞吐量:   {throughput:.1f} bars/sec")
    runner.assert_all_pass()


def test_engine_throughput_multi_symbol():
    """回测引擎吞吐量：3标的同时回测（每标的2000根）"""
    bars1 = make_synthetic_bars(n=2000, seed=41)
    bars2 = make_synthetic_bars(n=2000, seed=42)
    bars3 = make_synthetic_bars(n=2000, seed=43)
    total_bars = len(bars1) + len(bars2) + len(bars3)

    def _run():
        df1 = _to_engine_df(bars1)
        df2 = _to_engine_df(bars2)
        df3 = _to_engine_df(bars3)
        strat = MACrossStrategy(fast_period=5, slow_period=20)
        config = BacktestConfig(initial_capital=300000)
        engine = BacktestEngine(
            config=config, strategy=strat,
            data={"S1.SZ": df1, "S2.SZ": df2, "S3.SZ": df3},
        )
        engine.run()

    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    result = runner.run(f"引擎吞吐量(3标的x2000根={total_bars}根)", _run, threshold_ms=10000.0)
    throughput = total_bars / (result.mean_ms / 1000.0)
    print(result.format())
    print(f"    吞吐量:   {throughput:.1f} bars/sec (3标的合计)")
    runner.assert_all_pass()


# ═══════════════════════════════════════════
#  独立运行入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  回测引擎吞吐量基准测试")
    print("=" * 60)
    test_engine_throughput_1k()
    test_engine_throughput_5k()
    test_engine_throughput_multi_symbol()
    print("\n全部引擎吞吐量基准测试完成")
