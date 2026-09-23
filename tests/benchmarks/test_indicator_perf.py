# 文件名：tests/benchmarks/test_indicator_perf.py
"""
技术指标计算耗时基准测试
============================
覆盖 8 个核心技术指标在 10000 根K线上的计算耗时，
以及增量更新（实盘逐根推送）的延迟。

运行方式：
  独立运行：python tests/benchmarks/test_indicator_perf.py
  pytest收集：pytest tests/benchmarks/test_indicator_perf.py -v

阈值（conftest.py 中配置）：
  单指标全量计算 < 200ms（10000根K线）
  增量更新 < 5ms/根

设计原则：
  1. 全部使用历史数据（合成K线 2024-01），严格禁用未来函数
  2. 增量更新测试为实盘逐根推送场景预留
  3. 可独立运行也可 pytest 收集
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

# 路径配置
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from _benchmark_utils import BenchmarkRunner, make_synthetic_bars  # noqa: E402
from conftest import (  # noqa: E402
    BENCH_ITERATIONS,
    BENCH_WARMUP,
    INDICATOR_THRESHOLD_MS,
)

from myquant.indicators.moving_average import SMA, EMA, WMA, MACD  # noqa: E402
from myquant.indicators.momentum import RSI, KDJ  # noqa: E402
from myquant.indicators.volatility import BollingerBands, ATR  # noqa: E402


def _to_dataframe(bars: List[Dict[str, Any]]) -> pd.DataFrame:
    """将K线字典列表转为 DataFrame"""
    return pd.DataFrame(bars)


# ═══════════════════════════════════════════
#  全量计算基准（10000 根K线）
# ═══════════════════════════════════════════

def test_sma_perf():
    """SMA(20) 全量计算耗时"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = SMA(period=20)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("SMA(20) 全量计算(10000根)", lambda: ind.calculate(df), threshold_ms=INDICATOR_THRESHOLD_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_ema_perf():
    """EMA(20) 全量计算耗时"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = EMA(period=20)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("EMA(20) 全量计算(10000根)", lambda: ind.calculate(df), threshold_ms=INDICATOR_THRESHOLD_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_wma_perf():
    """WMA(20) 全量计算耗时"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = WMA(period=20)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("WMA(20) 全量计算(10000根)", lambda: ind.calculate(df), threshold_ms=INDICATOR_THRESHOLD_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_macd_perf():
    """MACD(12,26,9) 全量计算耗时"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = MACD(fast=12, slow=26, signal=9)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("MACD(12,26,9) 全量计算(10000根)", lambda: ind.calculate(df), threshold_ms=INDICATOR_THRESHOLD_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_rsi_perf():
    """RSI(14) 全量计算耗时"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = RSI(period=14)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("RSI(14) 全量计算(10000根)", lambda: ind.calculate(df), threshold_ms=INDICATOR_THRESHOLD_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_kdj_perf():
    """KDJ(9,3,3) 全量计算耗时"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = KDJ(n=9, m1=3, m2=3)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("KDJ(9,3,3) 全量计算(10000根)", lambda: ind.calculate(df), threshold_ms=INDICATOR_THRESHOLD_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_bollinger_perf():
    """BollingerBands(20,2) 全量计算耗时"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = BollingerBands(period=20, std_dev=2)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("Bollinger(20,2) 全量计算(10000根)", lambda: ind.calculate(df), threshold_ms=INDICATOR_THRESHOLD_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_atr_perf():
    """ATR(14) 全量计算耗时"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = ATR(period=14)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("ATR(14) 全量计算(10000根)", lambda: ind.calculate(df), threshold_ms=INDICATOR_THRESHOLD_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


# ═══════════════════════════════════════════
#  增量更新基准（实盘逐根推送场景）
# ═══════════════════════════════════════════

def test_sma_incremental_update_perf():
    """SMA 增量更新延迟（实盘逐根推送，单根更新）"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = SMA(period=20)
    ind.calculate(df)  # 先全量计算初始化
    new_bar = df.iloc[-1].copy()  # Series（非单行DataFrame）
    new_bar["收盘价"] = new_bar["收盘价"] * 1.001
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("SMA 增量更新(单根)", lambda: ind.update(new_bar), threshold_ms=5.0)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_rsi_incremental_update_perf():
    """RSI 增量更新延迟（实盘逐根推送）"""
    bars = make_synthetic_bars(n=10000)
    df = _to_dataframe(bars)
    ind = RSI(period=14)
    ind.calculate(df)
    new_bar = df.iloc[-1].copy()  # Series
    new_bar["收盘价"] = new_bar["收盘价"] * 1.001
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("RSI 增量更新(单根)", lambda: ind.update(new_bar), threshold_ms=5.0)
    print(runner.results[-1].format())
    runner.assert_all_pass()


# ═══════════════════════════════════════════
#  独立运行入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  技术指标计算耗时基准测试")
    print("=" * 60)
    test_sma_perf()
    test_ema_perf()
    test_wma_perf()
    test_macd_perf()
    test_rsi_perf()
    test_kdj_perf()
    test_bollinger_perf()
    test_atr_perf()
    test_sma_incremental_update_perf()
    test_rsi_incremental_update_perf()
    print("\n全部指标基准测试完成")
