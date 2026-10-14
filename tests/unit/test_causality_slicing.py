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
    """full_sample_declared=True（引擎构造参数）→ 返回全量（vectorized 逃生口）。"""

    def test_full_sample_declared_returns_all(self, sample_df):
        c = StrategyContext(_full_sample_declared=True)
        c.data = {"600519.SH": sample_df}
        c._slicing_enabled = True
        c.current_bar_ts = pd.Timestamp("2024-01-03")
        df = c.get_data("600519.SH")
        assert len(df) == 5  # 全量，忽略切片时点

    def test_full_sample_declared_is_engine_construct_param(self, ctx):
        """P0-08 子项②：全量逃生口为引擎构造参数，策略运行期不可自设。

        反红验收②基础：探针策略写 ``context.full_sample_declared = True``
        必须无效（抛 AttributeError），防止策略自行开启逃生口绕过因果切片。
        """
        assert ctx.full_sample_declared is False
        with pytest.raises(AttributeError):
            ctx.full_sample_declared = True
        # 赋值被拒后状态不变（未开启逃生口）
        assert ctx.full_sample_declared is False


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


class TestReusedContextNoLeftover:
    """用例⑪（P0-08 子项⑦ / R-25 处置）：复用同一 context 跨两次 run 不残留。

    残留缺陷（R-25 核验）：mixed 引擎官方支持重复 run，但 context 切片状态
    （_slicing_enabled / current_bar_ts / 访问账目）不在重置清单 → 上次 run 末
    bar 时点残留。子项⑦在 run() 起点复位三态 → 本测试验证复位生效。
    """

    def test_reused_context_no_slicing_state_leftover(self) -> None:
        from typing import List, Optional

        from myquant.backtest.mixed_engine import MixedBacktestEngine
        from myquant.models.backtest import BacktestConfig
        from myquant.models.market_data import Bar
        from myquant.strategies.base import StrategyBase

        class _ReuseProbe(StrategyBase):
            """复用探针：on_init 读一次全量（触发未封门记账），on_bar 空信号。"""

            name = "reuse_probe"

            def on_init(self, context: StrategyContext) -> None:
                df = context.get_data("000001.SZ")
                self.init_len: Optional[int] = (
                    len(df) if df is not None else -1
                )

            def on_bar(self, bar: Bar, context: StrategyContext) -> List[object]:
                return []

            def generate_signal(self, bar: Bar, context: StrategyContext) -> object:
                return None

        idx = pd.date_range("2024-01-01", periods=10, freq="B")
        df10 = pd.DataFrame(
            {
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": [10.0 + 0.1 * i for i in range(10)],
                "pre_close": [10.0] + [10.0 + 0.1 * i for i in range(9)],
                "volume": 1_000_000.0,
            },
            index=idx,
        )
        config = BacktestConfig(
            start_date="2024-01-01",
            end_date="2024-01-20",
            symbols=["000001.SZ"],
            initial_capital=1_000_000.0,
            participation_rate=1.0,
        )
        probe = _ReuseProbe()
        engine = MixedBacktestEngine(
            config=config, strategy=probe, data={"000001.SZ": df10}
        )

        r1 = engine.run()
        assert r1 is not None and r1.is_success
        # 第一次 run 末：切片已置位 + 时点推进到末 bar；on_init 全量访问已记账
        assert engine.context._slicing_enabled is True
        assert engine.context.current_bar_ts is not None
        assert engine.context.current_bar_ts == idx[-1]
        assert "000001.SZ" in engine.context.accessed_max_dates

        # 第二次 run：run 起点复位（旧时点/账目清空）→ on_bar 循环重新置位
        r2 = engine.run()
        assert r2 is not None and r2.is_success
        assert engine.context._slicing_enabled is True  # 主循环重新置位
        assert engine.context.current_bar_ts == idx[-1]  # 推进到本次末 bar
        # 访问账目已复位清空（on_bar 已封门不记账；若复位缺失，第一次的账目会残留）
        assert engine.context.accessed_max_dates == {}


class TestWindowIsolation:
    """P0-08 子项⑧：context.data 窗口隔离（S-2 阶段化 + S-13 精确定义）。

    保留 keys 枚举（keys()/bool/len/contains 照常）；封死 value 直取
    （__getitem__ 在 on_bar 阶段返回 ≤current_bar_ts 切片，取不到全量含未来）。
    改动仅接口层（DataWindow 代理），不动任何策略代码（S-3）。
    """

    def test_on_bar_getitem_returns_slice(self, ctx):
        """用例 1/10：on_bar 阶段 context.data["x"] 取不到全量（返回切片）。"""
        ctx._slicing_enabled = True
        ctx.current_bar_ts = pd.Timestamp("2024-01-03")  # index[2]
        df = ctx.data["600519.SH"]
        assert len(df) == 3
        assert df.index[-1] == pd.Timestamp("2024-01-03")

    def test_keys_enumeration_kept(self, ctx):
        """不误伤：keys 枚举/布尔/长度照常（5 个策略接触点 ma/momentum/multi/grid）。"""
        ctx._slicing_enabled = True
        ctx.current_bar_ts = pd.Timestamp("2024-01-03")
        assert list(ctx.data.keys()) == ["600519.SH"]
        assert bool(ctx.data) is True
        assert len(ctx.data) == 1
        assert "600519.SH" in ctx.data

    def test_on_init_getitem_returns_full(self, ctx):
        """S-2 阶段语义：on_init（未切片）context.data["x"] 返回全量（保 ma 预计算）。"""
        assert ctx._slicing_enabled is False
        df = ctx.data["600519.SH"]
        assert len(df) == 5

    def test_escape_hatch_getitem_returns_full(self, sample_df):
        """不误伤：vectorized 逃生口（full_sample_declared=True）value 直取全量。"""
        c = StrategyContext(_full_sample_declared=True)
        c.data = {"600519.SH": sample_df}
        c._slicing_enabled = True
        c.current_bar_ts = pd.Timestamp("2024-01-03")
        df = c.data["600519.SH"]
        assert len(df) == 5

    def test_fail_closed_when_no_timestamp(self, ctx):
        """切片开启但时点未设 → 空（fail-closed，与 get_data 一致，不静默全量）。"""
        ctx._slicing_enabled = True
        ctx.current_bar_ts = None
        df = ctx.data["600519.SH"]
        assert len(df) == 0

    def test_get_returns_full_for_builder_power(self, ctx):
        """pool_builder 幂等路径：data.get() 恒返回全量（build 二次 as_of 截断）。"""
        ctx._slicing_enabled = True
        ctx.current_bar_ts = pd.Timestamp("2024-01-03")
        df = ctx.data.get("600519.SH")
        assert len(df) == 5  # get 走 dict 原生 → 全量，供 build 自行截断

    def test_setitem_write_works(self, ctx, sample_df):
        """live 写入点：data[symbol]=... 照常工作（dict 子类 __setitem__）。"""
        ctx.data["600519.SH"] = sample_df
        assert ctx.data["600519.SH"] is sample_df  # on_init 阶段（未切片）直取

    def test_engine_on_bar_data_is_scoped(self) -> None:
        """引擎集成（用例⑩）：真实 mixed 引擎 on_bar 阶段 data[symbol] 为切片。

        第 i 根 bar 断言长度为 i+1——若窗口隔离未生效（全量直取）则恒为 10，测试红。
        """
        from typing import List

        from myquant.backtest.mixed_engine import MixedBacktestEngine
        from myquant.models.backtest import BacktestConfig
        from myquant.models.market_data import Bar
        from myquant.strategies.base import StrategyBase

        class _DataProbe(StrategyBase):
            """数据探针：每根 on_bar 记录 context.data[symbol] 的切片长度。"""

            name = "data_probe"

            def __init__(self) -> None:
                super().__init__()
                self.lens: List[int] = []

            def on_bar(self, bar: Bar, context: StrategyContext) -> List[object]:
                df = context.data["000001.SZ"]
                self.lens.append(len(df))
                return []

            def generate_signal(self, bar: Bar, context: StrategyContext) -> object:
                return None

        idx = pd.date_range("2024-01-01", periods=10, freq="B")
        df10 = pd.DataFrame(
            {
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": [10.0 + 0.1 * i for i in range(10)],
                "pre_close": [10.0] + [10.0 + 0.1 * i for i in range(9)],
                "volume": 1_000_000.0,
            },
            index=idx,
        )
        config = BacktestConfig(
            start_date="2024-01-01",
            end_date="2024-01-20",
            symbols=["000001.SZ"],
            initial_capital=1_000_000.0,
            participation_rate=1.0,
        )
        probe = _DataProbe()
        engine = MixedBacktestEngine(
            config=config, strategy=probe, data={"000001.SZ": df10}
        )
        result = engine.run()
        assert result is not None and result.is_success
        assert probe.lens == list(range(1, 11))
