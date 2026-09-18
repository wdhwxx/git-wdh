# 文件名：tests/benchmarks/_benchmark_utils.py
"""
轻量级性能基准工具（纯Python，无外部依赖）
==============================================
不依赖 pytest-benchmark，可独立运行也可被 pytest 收集。

功能：
  - BenchmarkRunner：运行函数 N 次，统计均值/中位数/P95/P99/ops/sec
  - assert_under：阈值断言
  - 格式化输出性能报告

设计原则：
  1. 全部使用历史数据（合成K线 2024-01），严格禁用未来函数
  2. 为实时行情及实盘对接预留：吞吐量/延迟指标直接反映实盘承载能力
  3. 可独立运行：python tests/benchmarks/test_xxx.py
  4. 可被 pytest 收集：pytest tests/benchmarks/
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class BenchmarkResult:
    """单次基准测试结果"""
    name: str
    iterations: int
    durations: List[float] = field(default_factory=list)  # 秒
    total_time: float = 0.0
    mean_ms: float = 0.0
    median_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    ops_per_sec: float = 0.0
    threshold_ms: Optional[float] = None
    passed: bool = True

    def __post_init__(self) -> None:
        if self.durations:
            self.total_time = sum(self.durations)
            ms = [d * 1000 for d in self.durations]
            self.mean_ms = statistics.mean(ms)
            self.median_ms = statistics.median(ms)
            self.p95_ms = _percentile(ms, 95)
            self.p99_ms = _percentile(ms, 99)
            self.min_ms = min(ms)
            self.max_ms = max(ms)
            self.ops_per_sec = self.iterations / self.total_time if self.total_time > 0 else 0
            if self.threshold_ms is not None:
                self.passed = self.mean_ms <= self.threshold_ms

    def format(self) -> str:
        """格式化输出"""
        lines = [
            f"  [{self.name}]",
            f"    迭代次数: {self.iterations}",
            f"    总耗时:   {self.total_time:.4f}s",
            f"    均值:     {self.mean_ms:.4f} ms",
            f"    中位数:   {self.median_ms:.4f} ms",
            f"    P95:      {self.p95_ms:.4f} ms",
            f"    P99:      {self.p99_ms:.4f} ms",
            f"    最小/最大: {self.min_ms:.4f} / {self.max_ms:.4f} ms",
            f"    吞吐量:   {self.ops_per_sec:.1f} ops/sec",
        ]
        if self.threshold_ms is not None:
            status = "PASS" if self.passed else "FAIL"
            lines.append(f"    阈值:     {self.threshold_ms} ms (均值) → {status}")
        return "\n".join(lines)


def _percentile(data: List[float], p: float) -> float:
    """计算百分位数（线性插值）"""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_data) else f
    if f == c:
        return sorted_data[f]
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


class BenchmarkRunner:
    """性能基准运行器"""

    def __init__(self, warmup: int = 3, iterations: int = 50) -> None:
        """
        Parameters
        ----------
        warmup : int
            预热次数（不计入统计）
        iterations : int
            正式测量次数
        """
        self.warmup = warmup
        self.iterations = iterations
        self.results: List[BenchmarkResult] = []

    def run(
        self,
        name: str,
        func: Callable[[], Any],
        threshold_ms: Optional[float] = None,
        setup: Optional[Callable[[], Any]] = None,
    ) -> BenchmarkResult:
        """
        运行基准测试

        Parameters
        ----------
        name : str
            测试名称
        func : Callable
            被测函数（无参数）
        threshold_ms : float | None
            均值阈值（ms），超过则 FAIL
        setup : Callable | None
            每次迭代前的准备函数（不计入耗时）
        """
        # 预热
        for _ in range(self.warmup):
            if setup:
                setup()
            func()

        # 正式测量
        durations: List[float] = []
        for _ in range(self.iterations):
            if setup:
                setup()
            t0 = time.perf_counter()
            func()
            t1 = time.perf_counter()
            durations.append(t1 - t0)

        result = BenchmarkResult(
            name=name,
            iterations=self.iterations,
            durations=durations,
            threshold_ms=threshold_ms,
        )
        self.results.append(result)
        return result

    def report(self) -> str:
        """生成完整报告"""
        lines = ["=" * 60, "  性能基准报告", "=" * 60]
        all_passed = True
        for r in self.results:
            lines.append(r.format())
            if not r.passed:
                all_passed = False
        lines.append("-" * 60)
        status = "ALL PASS" if all_passed else "HAS FAILURE"
        lines.append(f"  总计: {len(self.results)} 项 → {status}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def assert_all_pass(self) -> None:
        """断言所有测试通过（用于 pytest）"""
        failures = [r for r in self.results if not r.passed]
        if failures:
            names = ", ".join(r.name for r in failures)
            raise AssertionError(f"性能基准未通过阈值: {names}")


def make_synthetic_bars(n: int = 10000, start_date: str = "2024-01-02", seed: int = 42) -> List[Dict[str, Any]]:
    """
    生成合成K线数据（随机游走，历史日期，跳过周末）
    用于性能基准测试，严格使用历史数据防未来函数。
    """
    import random
    random.seed(seed)
    bars: List[Dict[str, Any]] = []
    from datetime import datetime, timedelta
    date = datetime.strptime(start_date, "%Y-%m-%d")
    price = 10.0
    for _ in range(n):
        while date.weekday() >= 5:
            date += timedelta(days=1)
        change = random.gauss(0, 0.015)
        open_p = price
        close_p = max(0.01, price * (1 + change))
        high_p = max(open_p, close_p) * (1 + abs(random.gauss(0, 0.008)))
        low_p = min(open_p, close_p) * (1 - abs(random.gauss(0, 0.008)))
        volume = int(random.uniform(50000, 200000))
        bars.append({
            "symbol": "PERF.SZ",
            "date": date.strftime("%Y-%m-%d"),
            "open": round(open_p, 4),
            "high": round(high_p, 4),
            "low": round(low_p, 4),
            "close": round(close_p, 4),
            "volume": volume,
            "amount": round(volume * close_p, 2),
        })
        price = close_p
        date += timedelta(days=1)
    return bars
