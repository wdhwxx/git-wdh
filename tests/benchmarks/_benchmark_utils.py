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

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# P2-6（v1.2 §6.3）：绝对毫秒墙钟阈值→硬件无关相对指标。
# 判定口径 = 相对基线倍数（本次 mean_ms / 上一轮基线 median_ms），上限 RATIO_LIMIT：
#   - 无基线（首次运行/换机首跑）：passed=True 仅建基线，不阻断；
#   - 有基线：ratio ≤ RATIO_LIMIT 通过（正常负载/换机 2~3 倍波动不红，5 倍以上视为回归）。
# 每次运行自动追加趋势记录（docs/benchmarks/trend.jsonl），结果仅报告非阻断。
RATIO_LIMIT: float = 5.0


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
    baseline_ms: Optional[float] = None
    ratio_to_baseline: Optional[float] = None
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
            # P2-6 相对化判定（threshold_ms 保留为兼容参数，仅参考不再阻断）
            if self.baseline_ms is not None and self.baseline_ms > 0:
                self.ratio_to_baseline = self.mean_ms / self.baseline_ms
                self.passed = self.ratio_to_baseline <= RATIO_LIMIT
            else:
                # 首建基线（无上一轮数据）：不阻断，只记录
                self.passed = True

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
        if self.baseline_ms is not None:
            status = "PASS" if self.passed else "FAIL"
            ratio = f"{self.ratio_to_baseline:.2f}" if self.ratio_to_baseline is not None else "N/A"
            lines.append(
                f"    相对基线: {self.baseline_ms:.4f} ms → 倍数 {ratio}×"
                f" (上限 {RATIO_LIMIT:.1f}×) → {status}"
            )
        elif self.threshold_ms is not None:
            lines.append(
                f"    绝对阈值: {self.threshold_ms} ms (兼容参数，首建基线不判定，仅参考)"
            )
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


def _trend_path() -> Path:
    """趋势报告默认落点（项目根/docs/benchmarks/trend.jsonl）。"""
    return Path(__file__).resolve().parent.parent.parent / "docs" / "benchmarks" / "trend.jsonl"


def load_latest_baseline(path: Path, name: str) -> Optional[float]:
    """从趋势文件中读取该基准项最近一轮的 median_ms 作为相对基线。

    Args:
        path: 趋势文件（JSONL，每行一条趋势记录）。
        name: 基准项名称。

    Returns:
        float | None: 最近一轮 median_ms；文件缺失或无该名称记录返回 None。
    """
    if not path.is_file():
        return None
    latest: Optional[float] = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("name") == name and isinstance(rec.get("median_ms"), (int, float)):
            latest = float(rec["median_ms"])
    return latest


def append_trend(path: Path, result: BenchmarkResult) -> None:
    """追加一条趋势记录（JSONL；供 docs/benchmarks/ 趋势报告，非阻断）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {
        "name": result.name,
        "mean_ms": result.mean_ms,
        "median_ms": result.median_ms,
        "p95_ms": result.p95_ms,
        "p99_ms": result.p99_ms,
        "baseline_ms": result.baseline_ms,
        "ratio_to_baseline": result.ratio_to_baseline,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class BenchmarkRunner:
    """性能基准运行器"""

    def __init__(self, warmup: int = 3, iterations: int = 50,
                 trend_path: Optional[Path] = None) -> None:
        """
        Parameters
        ----------
        warmup : int
            预热次数（不计入统计）
        iterations : int
            正式测量次数
        trend_path : Path | None
            趋势报告路径（None 用默认 docs/benchmarks/trend.jsonl）
        """
        self.warmup = warmup
        self.iterations = iterations
        self.trend_path: Path = trend_path if trend_path is not None else _trend_path()
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
            兼容参数（历史调用保留；P2-6 起判定改为相对基线倍数，见 RATIO_LIMIT）
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

        baseline = load_latest_baseline(self.trend_path, name)
        result = BenchmarkResult(
            name=name,
            iterations=self.iterations,
            durations=durations,
            threshold_ms=threshold_ms,
            baseline_ms=baseline,
        )
        self.results.append(result)
        append_trend(self.trend_path, result)
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

    列名与系统规范数据 schema 保持一致（中文 OHLCV）：
      symbol / date / 开盘价 / 最高价 / 最低价 / 收盘价 / 成交量，
    以兼容回测引擎（engine._build_unified_bars 按中文列构造 Bar）与
    技术指标/策略（统一读取 开盘价/最高价/最低价/收盘价/成交量）。
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
            "开盘价": round(open_p, 4),
            "最高价": round(high_p, 4),
            "最低价": round(low_p, 4),
            "收盘价": round(close_p, 4),
            "成交量": volume,
        })
        price = close_p
        date += timedelta(days=1)
    return bars
