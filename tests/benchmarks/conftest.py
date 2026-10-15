# 文件名：tests/benchmarks/conftest.py
"""
基准测试专用 fixture 与阈值配置
==================================
提供：
  - perf_bars_10k：10000 根合成K线 fixture
  - perf_bars_5k：5000 根合成K线 fixture
  - 性能阈值常量：INDICATOR_THRESHOLD_MS / ENGINE_THROUGHPUT / EXECUTION_LATENCY_MS

所有数据使用历史日期（2024-01），严格禁用未来函数。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# 确保项目根目录和 benchmarks 工具可导入
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from _benchmark_utils import make_synthetic_bars  # noqa: E402

# ═══════════════════════════════════════════
#  性能阈值配置（P2-6 起为"参考值"，判定已相对化）
# ═══════════════════════════════════════════
# P2-6（v1.2 §6.3）：以下绝对毫秒/吞吐常量仅作展示参考，不再参与阻断——
# 判定口径改为相对基线倍数（_benchmark_utils.RATIO_LIMIT=5.0，本次 mean_ms /
# 上一轮 median_ms），首次运行仅建基线不红；趋势记录自动落盘
# docs/benchmarks/trend.jsonl（非阻断）。

# 单个技术指标计算阈值（10000根K线，均值ms，参考值）
INDICATOR_THRESHOLD_MS = 200.0

# 回测引擎吞吐量阈值（bars/sec，参考值）
ENGINE_THROUGHPUT_MIN = 500.0

# 单次下单延迟阈值（ms，参考值）
EXECUTION_LATENCY_MS = 5.0

# 基准迭代次数
BENCH_ITERATIONS = 30
BENCH_WARMUP = 3


# ═══════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════

@pytest.fixture(scope="module")
def perf_bars_10k() -> List[Dict[str, Any]]:
    """10000 根合成K线（历史日期，用于指标性能测试）"""
    return make_synthetic_bars(n=10000)


@pytest.fixture(scope="module")
def perf_bars_5k() -> List[Dict[str, Any]]:
    """5000 根合成K线（历史日期，用于引擎吞吐量测试）"""
    return make_synthetic_bars(n=5000)


@pytest.fixture(scope="module")
def perf_bars_1k() -> List[Dict[str, Any]]:
    """1000 根合成K线（历史日期，用于快速测试）"""
    return make_synthetic_bars(n=1000)
