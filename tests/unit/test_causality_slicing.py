# 文件名：tests/unit/test_causality_slicing.py
"""
tests/unit/test_causality_slicing.py - P0-07 因果切片反红测试
================================================================

覆盖：
  - R-19 零覆盖收录（StrategyContext 切片逻辑被 tests import）
  - 默认 _slicing_enabled=False → get_data 返回全量（向后兼容）
  - _slicing_enabled=True + current_bar_ts 设置 → 返回切片视图（<= 当前 bar）
  - _slicing_enabled=True + current_bar_ts 未设置 → fail-closed 返回空
  - full_sample_declared=True → 返回全量（vectorized 逃生口）
  - 切片视图不含未来 bar（结构性证明：故意读 iloc[-1] 取到的是当前 bar）
"""

from datetime import datetime

import pandas as pd
import pytest

from myquant.strategies.base import StrategyContext


@pytest.fixture
def sample_df() -> pd.DataFrame:
    """构造 5 个交易日的合成行情。"""
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    return pd.DataFrame(
        {"收盘": [10.0, 10.1, 10.2, 10.3, 10.4]},
        index=idx,
    )


@pytest.fixture
def ctx(sample_df) -> StrategyContext:
    c = StrategyContext()
    c.data = {"600519.SH": sample_df}
    return c


class TestBackwardCompat:
    """默认切片关闭：get_data 返回全量（向后兼容现有策略）。"""

    def test_default_returns_full(self, ctx):
        df = ctx.get_data("600519.SH")
        assert len(df) == 5

    def test_missing_symbol_returns_none(self, ctx):
        assert ctx.get_data("UNKNOWN.SH") is None


class TestSlicingEnabled:
    """切片开启：get_data 返回 <= current_bar_ts 的视图。"""

    def test_slice_at_bar2(self, ctx):
        ctx._slicing_enabled = True
        ctx.current_bar_ts = pd.Timestamp("2024-01-03")  # Wed = index[2]
        df = ctx.get_data("600519.SH")
        assert len(df) == 3  # Mon/Tue/Wed
        assert df.index[-1] == pd.Timestamp("2024-01-03")

    def test_slice_excludes_future(self, ctx):
        """结构性证明：切片后 iloc[-1] 取不到未来 bar。"""
        ctx._slicing_enabled = True
        ctx.current_bar_ts = pd.Timestamp("2024-01-03")
        df = ctx.get_data("600519.SH")
        last = df.iloc[-1]
        assert last["收盘"] == 10.2  # Wed，不是 10.4（Fri 未来）

    def test_slice_at_first_bar(self, ctx):
        ctx._slicing_enabled = True
        ctx.current_bar_ts = pd.Timestamp("2024-01-01")
        df = ctx.get_data("600519.SH")
        assert len(df) == 1


class TestFailClosed:
    """切片开启但时点未设置 → fail-closed 返回空。"""

    def test_no_timestamp_returns_empty(self, ctx):
        ctx._slicing_enabled = True
        ctx.current_bar_ts = None
        df = ctx.get_data("600519.SH")
        assert len(df) == 0


class TestFullSampleEscape:
    """full_sample_declared=True → 返回全量（vectorized 逃生口）。"""

    def test_full_sample_declared_returns_all(self, ctx):
        ctx._slicing_enabled = True
        ctx.current_bar_ts = pd.Timestamp("2024-01-03")
        ctx.full_sample_declared = True
        df = ctx.get_data("600519.SH")
        assert len(df) == 5  # 全量，忽略切片时点


class TestOnInitStageFullAccess:
    """R-26 / P0-08⑦：on_init 阶段（引擎尚未开启切片）可全量访问 context.data。

    引擎只在 on_bar 逐 bar 推进时才置 ``_slicing_enabled=True``
    （backtest/engine.py:492、mixed_engine.py:744）；on_init 阶段保持默认 False，
    故 get_data 返回全量。该阶段契约是 P0-08 窗口隔离（on_init 放行全量、
    on_bar 封死 value 直取）的测试冻结——一旦 on_init 也被切片，本测试即红。
    """

    def test_on_init_stage_returns_full(self, ctx):
        # on_init 阶段引擎未置切片开关（保持默认 False）
        assert ctx._slicing_enabled is False
        df = ctx.get_data("600519.SH")
        assert len(df) == 5  # 全量可访问，未被切片
        assert df.index[0] == pd.Timestamp("2024-01-01")
