# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_data_loader.py
"""
优化数据加载器（myquant/data_access/loader.py）单元测试（G0-9 补测）
==========================================================
覆盖（注入 fake provider/watcher，避免真实数据/内存依赖）：
  - LoadStats：avg_load_time_ms / success_rate / to_dict / summary
  - OptimizedDataLoader 构造：prefetch_count<0 抛 / chunk_days 过小抛 / provider 注入
  - _adapt_to_memory：CRITICAL 停预取 / WARNING 减半 / NORMAL 恢复
  - _should_chunk：关闭分块 / CRITICAL 强制 / 日期跨度阈值 / 非法日期降级
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from myquant.data_access.loader import MIN_CHUNK_DAYS, LoadStats, OptimizedDataLoader
from myquant.utils.memory_watcher import MemoryLevel


class _FakeProvider:
    """最小假 provider（仅用于注入构造）。"""


class _FakeWatcher:
    """假内存监控器。"""

    def __init__(self, level: MemoryLevel, percent: float, critical: bool = False) -> None:
        self._level = level
        self._percent = percent
        self._critical = critical

    def check(self) -> MemoryLevel:
        return self._level

    def get_memory_info(self) -> Dict[str, Any]:
        return {"percent": self._percent}

    def is_critical(self) -> bool:
        return self._critical


# ─────────────────────────────────────────────
# LoadStats
# ─────────────────────────────────────────────
class TestLoadStats:
    def test_avg_load_time_ms(self) -> None:
        s = LoadStats()
        s.total_load_time_ms = 300.0
        s.loaded_symbols = 3
        assert s.avg_load_time_ms == pytest.approx(100.0)

    def test_avg_load_time_zero_symbols(self) -> None:
        s = LoadStats()
        s.total_load_time_ms = 100.0
        assert s.avg_load_time_ms == 0.0

    def test_success_rate(self) -> None:
        s = LoadStats()
        s.total_symbols = 10
        s.loaded_symbols = 7
        assert s.success_rate == pytest.approx(0.7)

    def test_success_rate_zero_total(self) -> None:
        s = LoadStats()
        assert s.success_rate == 0.0

    def test_to_dict_fields(self) -> None:
        s = LoadStats()
        s.total_symbols = 5
        s.loaded_symbols = 4
        s.errors = ["e1"]
        d = s.to_dict()
        assert d["total_symbols"] == 5
        assert d["success_rate"] == pytest.approx(0.8)
        assert d["errors"] == ["e1"]

    def test_summary_contains_counts(self) -> None:
        s = LoadStats()
        s.total_symbols = 6
        s.loaded_symbols = 5
        text = s.summary()
        assert "总标的数: 6" in text
        assert "成功加载: 5" in text


# ─────────────────────────────────────────────
# 构造
# ─────────────────────────────────────────────
class TestConstructor:
    def test_negative_prefetch_raises(self) -> None:
        with pytest.raises(ValueError):
            OptimizedDataLoader(provider=_FakeProvider(), prefetch_count=-1)

    def test_small_chunk_days_raises(self) -> None:
        with pytest.raises(ValueError):
            OptimizedDataLoader(provider=_FakeProvider(), chunk_days=MIN_CHUNK_DAYS - 1)

    def test_provider_injected(self) -> None:
        p = _FakeProvider()
        loader = OptimizedDataLoader(provider=p)
        assert loader.provider is p


# ─────────────────────────────────────────────
# _adapt_to_memory
# ─────────────────────────────────────────────
class TestAdaptToMemory:
    def _make(self, level: MemoryLevel, base: int = 2) -> OptimizedDataLoader:
        return OptimizedDataLoader(
            provider=_FakeProvider(),
            memory_watcher=_FakeWatcher(level, percent=90.0),
            prefetch_count=base,
        )

    def test_critical_stops_prefetch(self) -> None:
        loader = self._make(MemoryLevel.CRITICAL)
        level = loader._adapt_to_memory()
        assert level == MemoryLevel.CRITICAL
        assert loader.prefetch_count == 0
        assert loader.stats.memory_degradations == 1

    def test_warning_halves_prefetch(self) -> None:
        loader = self._make(MemoryLevel.WARNING, base=4)
        loader._adapt_to_memory()
        assert loader.prefetch_count == 2
        assert loader.stats.memory_degradations == 1

    def test_normal_restores_prefetch(self) -> None:
        loader = self._make(MemoryLevel.NORMAL, base=2)
        loader.prefetch_count = 0  # 模拟此前被降级
        loader._adapt_to_memory()
        assert loader.prefetch_count == 2


# ─────────────────────────────────────────────
# _should_chunk
# ─────────────────────────────────────────────
class TestShouldChunk:
    def test_chunking_disabled(self) -> None:
        loader = OptimizedDataLoader(
            provider=_FakeProvider(),
            memory_watcher=_FakeWatcher(MemoryLevel.NORMAL, 50.0),
            chunk_days=100,
            enable_chunking=False,
        )
        assert loader._should_chunk("2024-01-01", "2025-01-01") is False

    def test_critical_forces_chunk(self) -> None:
        loader = OptimizedDataLoader(
            provider=_FakeProvider(),
            memory_watcher=_FakeWatcher(MemoryLevel.CRITICAL, 90.0, critical=True),
            chunk_days=100,
        )
        assert loader._should_chunk("2024-01-01", "2024-02-01") is True

    def test_range_over_threshold_chunks(self) -> None:
        loader = OptimizedDataLoader(
            provider=_FakeProvider(),
            memory_watcher=_FakeWatcher(MemoryLevel.NORMAL, 50.0),
            chunk_days=100,
        )
        # 2024-01-01 ~ 2024-12-31 > 100 天
        assert loader._should_chunk("2024-01-01", "2024-12-31") is True

    def test_range_within_threshold_no_chunk(self) -> None:
        loader = OptimizedDataLoader(
            provider=_FakeProvider(),
            memory_watcher=_FakeWatcher(MemoryLevel.NORMAL, 50.0),
            chunk_days=100,
        )
        assert loader._should_chunk("2024-01-01", "2024-02-01") is False

    def test_invalid_dates_no_chunk(self) -> None:
        loader = OptimizedDataLoader(
            provider=_FakeProvider(),
            memory_watcher=_FakeWatcher(MemoryLevel.NORMAL, 50.0),
            chunk_days=100,
        )
        assert loader._should_chunk("bad-date", "2024-02-01") is False

    def test_missing_dates_no_chunk(self) -> None:
        loader = OptimizedDataLoader(
            provider=_FakeProvider(),
            memory_watcher=_FakeWatcher(MemoryLevel.NORMAL, 50.0),
            chunk_days=100,
        )
        assert loader._should_chunk(None, None) is False
