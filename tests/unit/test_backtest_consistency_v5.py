# 文件名：tests/unit/test_backtest_consistency_v5.py
"""
test_backtest_consistency_v5.py - P3-2 三引擎一致性回归单元测试
================================================================
覆盖 myquant/backtest/consistency_check.py（P3-1 新增组件）：
  - ComparisonTolerance   绝对+相对双阈值（isclose 语义）
  - ConsistencyChecker    三模式显式运行 + 逐日净值对拍
  - PairComparison/ConsistencyReport 对拍结果与汇总报告
  - DemoThreeModeStrategy 三模式演示策略桩
  - make_demo_data        确定性演示行情

测试维度:
  1. 双阈值：band / is_close / 非法参数防御
  2. 引擎运行：合法/非法模式、策略类型校验、复用实例重置
  3. 对拍：逐日对齐（每日期最后一个点）、空曲线/无公共日期边界、超差点报告
  4. 汇总：check_all 三模式 3 对、modes 非法防御、JSON 序列化
  5. 演示策略：三套接口行为一致（按 symbol 分桶计数）、reset、防御校验
  6. 演示行情：确定性可复现、列结构完整

运行方式:
  pytest tests/unit/test_backtest_consistency_v5.py -v
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from myquant.backtest.consistency_check import (
    DEFAULT_HOLD_SHARES,
    DEFAULT_HOLD_START,
    DEFAULT_PAIR_TOLERANCES,
    ENGINE_EVENT,
    ENGINE_MIXED,
    ENGINE_VECTORIZED,
    SUPPORTED_ENGINES,
    ComparisonTolerance,
    ConsistencyChecker,
    ConsistencyReport,
    DemoThreeModeStrategy,
    load_real_data,
    make_demo_data,
)
from myquant.models.backtest import BacktestConfig
from myquant.models.market_data import Bar, BarFrequency
from myquant.strategies.base import SignalType

# ─────────────────────────────────────────────
#  辅助：构造标准演示配置与数据
# ─────────────────────────────────────────────

def _make_config() -> BacktestConfig:
    """构造标准演示回测配置。"""
    return BacktestConfig(
        strategy_name="demo_three_mode",
        symbols=["000001.SZ"],
        start_date="2023-01-02",
        end_date="2024-01-31",
        initial_capital=1_000_000.0,
        frequency="1d",
    )


def _make_strategy() -> DemoThreeModeStrategy:
    """构造重置后的演示策略。"""
    return DemoThreeModeStrategy()


# ═════════════════════════════════════════════
#  1. ComparisonTolerance 双阈值
# ═════════════════════════════════════════════

class TestComparisonTolerance:
    """绝对+相对双阈值测试。"""

    def test_defaults(self) -> None:
        """默认双阈值应为 abs_tol=1e-3, rel_tol=1e-6。"""
        tol = ComparisonTolerance()
        assert tol.abs_tol == 1e-3
        assert tol.rel_tol == 1e-6

    def test_band_combines_abs_and_rel(self) -> None:
        """容差带 = abs_tol + rel_tol * |reference|。"""
        tol = ComparisonTolerance(abs_tol=0.1, rel_tol=0.01)
        assert tol.band(0.0) == pytest.approx(0.1)          # 小净值由绝对容差兜底
        assert tol.band(100.0) == pytest.approx(1.1)        # 大净值加入相对容差

    def test_is_close_within_band(self) -> None:
        """双阈值内应判为接近。"""
        tol = ComparisonTolerance(abs_tol=1e-3, rel_tol=1e-6)
        assert tol.is_close(1.0, 1.0000005) is True
        assert tol.is_close(1.0009, 1.0) is True            # 绝对容差内
        assert tol.is_close(1.0011, 1.0) is False           # 超出
        assert tol.is_close(10.000009, 10.0) is True        # 相对容差内

    def test_invalid_negative_tolerance_raises(self) -> None:
        """负阈值应抛出 ValueError。"""
        with pytest.raises(ValueError):
            ComparisonTolerance(abs_tol=-1e-3)
        with pytest.raises(ValueError):
            ComparisonTolerance(rel_tol=-1e-6)

    def test_to_dict(self) -> None:
        """to_dict 应导出 abs_tol/rel_tol。"""
        tol = ComparisonTolerance(abs_tol=0.01, rel_tol=1e-4)
        assert tol.to_dict() == {"abs_tol": 0.01, "rel_tol": 1e-4}


# ═════════════════════════════════════════════
#  2. ConsistencyChecker 引擎运行
# ═════════════════════════════════════════════

class TestConsistencyChecker:
    """校验器基础能力测试。"""

    def test_pair_key_normalized(self) -> None:
        """引擎对键应无序归一化。"""
        checker = ConsistencyChecker()
        assert checker._pair_key("mixed", "vectorized") == (
            "mixed",
            "vectorized",
        )
        assert checker._pair_key("vectorized", "MIXED") == (
            "mixed",
            "vectorized",
        )

    def test_tolerance_for_uses_default(self) -> None:
        """未配置的对应回退默认双阈值（mixed vs vectorized 用紧阈值）。"""
        checker = ConsistencyChecker()
        tol = checker._tolerance_for(ENGINE_MIXED, ENGINE_VECTORIZED)
        assert tol.rel_tol == DEFAULT_PAIR_TOLERANCES[
            (ENGINE_MIXED, ENGINE_VECTORIZED)
        ].rel_tol
        # 未注册的引擎对回退通用默认
        assert checker._tolerance_for("unknown_a", "unknown_b").abs_tol == 1e-3

    def test_tolerance_for_global_override(self) -> None:
        """全局 abs_tol/rel_tol 应覆盖所有对拍对。"""
        checker = ConsistencyChecker(abs_tol=0.5, rel_tol=1e-2)
        for key in DEFAULT_PAIR_TOLERANCES:
            assert checker._tolerance_for(*key).abs_tol == 0.5
            assert checker._tolerance_for(*key).rel_tol == 1e-2

    def test_tolerance_for_custom_pair(self) -> None:
        """按对自定义容差应生效。"""
        custom = {(ENGINE_MIXED, ENGINE_VECTORIZED): ComparisonTolerance(0.2, 0.05)}
        checker = ConsistencyChecker(pair_tolerances=custom)
        assert checker._tolerance_for(ENGINE_VECTORIZED, ENGINE_MIXED).abs_tol == 0.2

    def test_run_engine_invalid_mode_raises(self) -> None:
        """非法模式应抛出 ValueError。"""
        checker = ConsistencyChecker()
        with pytest.raises(ValueError):
            checker.run_engine(
                mode="bogus",
                config=_make_config(),
                strategy=_make_strategy(),
                data=make_demo_data(),
            )

    def test_run_engine_non_strategy_raises(self) -> None:
        """非 StrategyBase 策略应抛出 TypeError。"""
        checker = ConsistencyChecker()
        with pytest.raises(TypeError):
            checker.run_engine(
                mode=ENGINE_EVENT,
                config=_make_config(),
                strategy=object(),  # type: ignore[arg-type]
                data=make_demo_data(),
            )

    def test_run_engine_resets_strategy_between_runs(self) -> None:
        """同一策略实例连续运行应每次重置状态（否则跨标的状态错位）。"""
        checker = ConsistencyChecker()
        data = make_demo_data()
        strategy = _make_strategy()
        # 先跑一个引擎使内部计数前进，再跑另一个引擎应重新从 0 计
        checker.run_engine(ENGINE_VECTORIZED, _make_config(), strategy, data)
        assert strategy._bar_by_symbol == {}  # run_engine 已调用 reset
        result = checker.run_engine(ENGINE_MIXED, _make_config(), strategy, data)
        assert result.is_success

    def test_run_engine_all_three_modes_success(self) -> None:
        """三种模式都应能独立跑通演示策略。"""
        checker = ConsistencyChecker()
        data = make_demo_data()
        for mode in SUPPORTED_ENGINES:
            result = checker.run_engine(mode, _make_config(), _make_strategy(), data)
            assert result.is_success, f"{mode} 引擎运行失败: {result.error_message}"


# ═════════════════════════════════════════════
#  3. 净值对拍 compare_pair
# ═════════════════════════════════════════════

class TestComparePair:
    """逐日净值对拍测试。"""

    @staticmethod
    def _make_result(dates: list[str], equities: list[float]) -> Any:
        """构造最小 BacktestResult 替身（仅需 equity_curve；final_equity 为属性派生）。"""
        from myquant.models.backtest import BacktestResult, EquityPoint

        return BacktestResult(
            status="success",
            equity_curve=[
                EquityPoint(
                    date=d,
                    equity=e,
                    cash=0.0,
                    position_value=0.0,
                )
                for d, e in zip(dates, equities)
            ],
            trades=[],
        )

    def test_identical_curves_pass(self) -> None:
        """完全一致的净值曲线应通过（0 超差）。"""
        checker = ConsistencyChecker()
        dates = [f"2023-01-{i:02d}" for i in range(1, 6)]
        eq = [1.0, 1.01, 1.02, 1.03, 1.04]
        cmp = checker.compare_pair(
            self._make_result(dates, eq),
            self._make_result(dates, eq),
            ENGINE_MIXED,
            ENGINE_VECTORIZED,
            "mixed_vs_vectorized",
        )
        assert cmp.ok is True
        assert cmp.n_points == 5
        assert cmp.n_mismatch == 0
        assert cmp.max_abs_diff == pytest.approx(0.0)

    def test_tiny_float_noise_within_tolerance_passes(self) -> None:
        """合法浮点偏差（1e-9 级）不应误报。"""
        checker = ConsistencyChecker()
        dates = [f"2023-01-{i:02d}" for i in range(1, 6)]
        a = [1.0, 1.01, 1.02, 1.03, 1.04]
        b = [x + 1e-9 * i for i, x in enumerate(a)]  # 在 a 上叠加 1e-9 级噪声
        cmp = checker.compare_pair(
            self._make_result(dates, a),
            self._make_result(dates, b),
            ENGINE_MIXED,
            ENGINE_VECTORIZED,
            "mixed_vs_vectorized",
        )
        assert cmp.ok is True
        assert cmp.n_mismatch == 0

    def test_big_deviation_fails_with_mismatch_dates(self) -> None:
        """明显偏差应判失败并报告超差日期。"""
        checker = ConsistencyChecker()
        dates = [f"2023-01-{i:02d}" for i in range(1, 6)]
        a = [1.0, 1.01, 1.02, 1.03, 1.04]
        b = [1.0, 1.05, 1.02, 1.10, 1.04]  # 第 2、4 点偏差 0.04 / 0.07
        cmp = checker.compare_pair(
            self._make_result(dates, a),
            self._make_result(dates, b),
            ENGINE_MIXED,
            ENGINE_VECTORIZED,
            "mixed_vs_vectorized",
        )
        assert cmp.ok is False
        assert cmp.n_mismatch == 2
        assert cmp.n_points == 5
        assert "2023-01-02" in cmp.mismatch_dates  # 第2个点（index 1）
        assert cmp.reason != ""

    def test_empty_curve_fails_with_reason(self) -> None:
        """空净值曲线应判失败并给出明确原因。"""
        checker = ConsistencyChecker()
        dates = [f"2023-01-{i:02d}" for i in range(1, 6)]
        eq = [1.0, 1.01, 1.02, 1.03, 1.04]
        cmp = checker.compare_pair(
            self._make_result([], []),
            self._make_result(dates, eq),
            ENGINE_EVENT,
            ENGINE_MIXED,
            "event_vs_mixed",
        )
        assert cmp.ok is False
        assert "空净值曲线" in cmp.reason

    def test_no_common_dates_fails_with_reason(self) -> None:
        """无公共日期应判失败并给出明确原因。"""
        checker = ConsistencyChecker()
        cmp = checker.compare_pair(
            self._make_result(["2023-01-01", "2023-01-02"], [1.0, 1.01]),
            self._make_result(["2024-01-01", "2024-01-02"], [1.0, 1.01]),
            ENGINE_EVENT,
            ENGINE_MIXED,
            "event_vs_mixed",
        )
        assert cmp.ok is False
        assert "无公共净值日期" in cmp.reason

    def test_multi_point_per_date_alignment(self) -> None:
        """多标的下每日期多个净值点应取最后一个点对齐（消除记录粒度差异）。"""
        checker = ConsistencyChecker()
        # 引擎 A：每日期 2 个点（模拟 event/mixed 每日期×每标的），最后一个点即组合净值
        dates_a = ["2023-01-01", "2023-01-01", "2023-01-02", "2023-01-02"]
        eq_a = [0.99, 1.00, 1.01, 1.02]  # 每日期最后一个点: 1.00 / 1.02
        # 引擎 B：每日期 1 个点（模拟 vectorized）
        dates_b = ["2023-01-01", "2023-01-02"]
        eq_b = [1.00, 1.02]
        cmp = checker.compare_pair(
            self._make_result(dates_a, eq_a),
            self._make_result(dates_b, eq_b),
            ENGINE_MIXED,
            ENGINE_VECTORIZED,
            "mixed_vs_vectorized",
        )
        assert cmp.ok is True
        assert cmp.n_points == 2


# ═════════════════════════════════════════════
#  4. 汇总校验 check_all
# ═════════════════════════════════════════════

class TestCheckAll:
    """三模式汇总校验测试。"""

    def test_default_three_modes_three_pairs_pass(self) -> None:
        """默认三模式演示策略应 3/3 对全部通过。"""
        checker = ConsistencyChecker()
        report = checker.check_all(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=60),
        )
        assert isinstance(report, ConsistencyReport)
        assert report.total_pairs == 3
        assert report.passed_pairs == 3
        assert report.overall_ok is True
        assert set(report.engine_summaries.keys()) == set(SUPPORTED_ENGINES)

    def test_mixed_vs_vectorized_matches_to_machine_precision(self) -> None:
        """mixed vs vectorized 共用撮合/费用口径，应逐点精确一致（紧阈值 1e-6）。"""
        checker = ConsistencyChecker()
        report = checker.check_all(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=60),
        )
        mv = next(c for c in report.comparisons if c.name == "mixed_vs_vectorized")
        assert mv.ok is True
        assert mv.max_rel_diff < 1e-6

    def test_event_pairs_converge_within_default_tolerance(self) -> None:
        """event 参与的对拍应在默认收敛容差（rel 5e-3）内通过。"""
        checker = ConsistencyChecker()
        report = checker.check_all(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=60),
        )
        for c in report.comparisons:
            if c.engine_a == ENGINE_EVENT or c.engine_b == ENGINE_EVENT:
                assert c.ok is True
                assert c.max_rel_diff < 5e-3

    def test_partial_modes(self) -> None:
        """只对拍 2 种模式应产生 1 对。"""
        checker = ConsistencyChecker()
        report = checker.check_all(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=60),
            modes=(ENGINE_MIXED, ENGINE_VECTORIZED),
        )
        assert report.total_pairs == 1
        assert report.engine_summaries.keys() == {ENGINE_MIXED, ENGINE_VECTORIZED}

    def test_invalid_mode_raises(self) -> None:
        """含非法模式应抛出 ValueError。"""
        checker = ConsistencyChecker()
        with pytest.raises(ValueError):
            checker.check_all(
                config=_make_config(),
                strategy=_make_strategy(),
                data=make_demo_data(),
                modes=(ENGINE_MIXED, "bogus"),
            )

    def test_less_than_two_modes_raises(self) -> None:
        """不足 2 种模式应抛出 ValueError。"""
        checker = ConsistencyChecker()
        with pytest.raises(ValueError):
            checker.check_all(
                config=_make_config(),
                strategy=_make_strategy(),
                data=make_demo_data(),
                modes=(ENGINE_MIXED,),
            )

    def test_report_to_dict_json_serializable(self) -> None:
        """报告 to_dict 应可 JSON 序列化且含关键字段。"""
        checker = ConsistencyChecker()
        report = checker.check_all(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=60),
        )
        d = report.to_dict()
        assert d["overall_ok"] is True
        assert d["passed_pairs"] == 3
        assert d["total_pairs"] == 3
        assert d["strategy_name"] == "demo_three_mode"
        assert len(d["comparisons"]) == 3
        json.dumps(d, ensure_ascii=False)  # 不应抛异常

    def test_report_summary_contains_key_sections(self) -> None:
        """summary() 应包含三引擎摘要、对拍明细与整体结论。"""
        checker = ConsistencyChecker()
        report = checker.check_all(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=60),
        )
        text = report.summary()
        assert "三模式净值一致性校验报告" in text
        assert "overall" not in text  # 面向人的文案
        for engine in SUPPORTED_ENGINES:
            assert engine in text
        assert "mixed_vs_vectorized" in text


# ═════════════════════════════════════════════
#  5. DemoThreeModeStrategy 演示策略
# ═════════════════════════════════════════════

class TestDemoThreeModeStrategy:
    """三模式演示策略桩测试。"""

    def test_engine_modes_all_supported(self) -> None:
        """三模式应全部支持。"""
        strategy = _make_strategy()
        assert set(strategy.engine_modes()) == set(SUPPORTED_ENGINES)
        for mode in SUPPORTED_ENGINES:
            assert strategy.supports_mode(mode) is True

    def test_generate_signals_vectorized(self) -> None:
        """vectorized 目标序列：第 hold_start 根起为 hold_shares。"""
        strategy = _make_strategy()
        df = pd.DataFrame({"close": [10.0] * 50})
        target = strategy.generate_signals_vectorized(df)
        assert target.shape == (50,)
        assert target[0] == 0.0
        assert target[DEFAULT_HOLD_START - 1] == 0.0
        assert target[DEFAULT_HOLD_START] == pytest.approx(DEFAULT_HOLD_SHARES)
        assert target[-1] == pytest.approx(DEFAULT_HOLD_SHARES)

    def test_generate_mixed_loop_per_symbol_counting(self) -> None:
        """mixed loop 应按 symbol 分桶计数（多标的互不干扰）。"""
        strategy = _make_strategy()
        bar_a = Bar(
            symbol="000001.SZ", frequency=BarFrequency.DAILY,
            timestamp=1672617600.0, open=10.0, high=10.1,
            low=9.9, close=10.0, volume=1000.0, amount=10000.0,
        )
        bar_b = Bar(
            symbol="000002.SZ", frequency=BarFrequency.DAILY,
            timestamp=1672617600.0, open=10.0, high=10.1,
            low=9.9, close=10.0, volume=1000.0, amount=10000.0,
        )
        ctx = None  # type: ignore[var-annotated]
        for _ in range(DEFAULT_HOLD_START):
            strategy.generate_mixed_loop(bar_a, ctx, None)
        # 标的 A 已到第 hold_start 根，应返回目标持仓
        assert strategy.generate_mixed_loop(
            bar_a, ctx, None
        ) == pytest.approx(DEFAULT_HOLD_SHARES)
        # 标的 B 仍是第 1 根，应返回 0
        assert strategy.generate_mixed_loop(bar_b, ctx, None) == 0.0

    def test_on_bar_buy_at_hold_start(self) -> None:
        """event 信号：各标的自第 hold_start 根发 BUY。"""
        strategy = _make_strategy()
        bar = Bar(
            symbol="000001.SZ", frequency=BarFrequency.DAILY,
            timestamp=1672617600.0, open=10.0, high=10.1,
            low=9.9, close=10.0, volume=1000.0, amount=10000.0,
        )
        ctx = None  # type: ignore[var-annotated]
        signals: list[Any] = []
        for _ in range(DEFAULT_HOLD_START):
            signals.extend(strategy.on_bar(bar, ctx))
        assert len(signals) == 0
        signals = strategy.on_bar(bar, ctx)
        assert len(signals) == 1
        assert signals[0].signal_type == SignalType.BUY
        assert signals[0].symbol == "000001.SZ"

    def test_calc_buy_quantity_fixed(self) -> None:
        """event 买入数量应为固定目标持仓。"""
        strategy = _make_strategy()
        assert strategy.calc_buy_quantity(price=10.0, context=None) == pytest.approx(
            DEFAULT_HOLD_SHARES
        )

    def test_reset_clears_state(self) -> None:
        """reset 应清空按标的的 bar 计数。"""
        strategy = _make_strategy()
        bar = Bar(
            symbol="000001.SZ", frequency=BarFrequency.DAILY,
            timestamp=1672617600.0, open=10.0, high=10.1,
            low=9.9, close=10.0, volume=1000.0, amount=10000.0,
        )
        ctx = None  # type: ignore[var-annotated]
        strategy.on_bar(bar, ctx)
        strategy.on_bar(bar, ctx)
        assert strategy._bar_by_symbol != {}
        strategy.reset()
        assert strategy._bar_by_symbol == {}

    def test_invalid_params_raise(self) -> None:
        """非法构造参数应抛出 ValueError。"""
        with pytest.raises(ValueError):
            DemoThreeModeStrategy(hold_start=-1)
        with pytest.raises(ValueError):
            DemoThreeModeStrategy(hold_shares=0.0)

    def test_name(self) -> None:
        """策略名应为 demo_three_mode。"""
        assert _make_strategy().name == "demo_three_mode"


# ═════════════════════════════════════════════
#  6. make_demo_data 演示行情
# ═════════════════════════════════════════════

class TestMakeDemoData:
    """确定性演示行情测试。"""

    def test_structure(self) -> None:
        """返回 {symbol: DataFrame} 且包含 OHLCV 列。"""
        data = make_demo_data(symbols=["000001.SZ", "000002.SZ"], n_bars=50)
        assert set(data.keys()) == {"000001.SZ", "000002.SZ"}
        df = data["000001.SZ"]
        assert df.shape[0] == 50
        for col in ("open", "high", "low", "close", "volume", "amount"):
            assert col in df.columns
        assert isinstance(df.index, pd.DatetimeIndex)

    def test_deterministic_same_seed(self) -> None:
        """同 seed 结果应完全一致（可复现）。"""
        a = make_demo_data(n_bars=50, seed=7)
        b = make_demo_data(n_bars=50, seed=7)
        pd.testing.assert_frame_equal(a["000001.SZ"], b["000001.SZ"])

    def test_different_seed_differs(self) -> None:
        """不同 seed 结果应不同。"""
        a = make_demo_data(n_bars=50, seed=7)
        b = make_demo_data(n_bars=50, seed=8)
        assert not a["000001.SZ"].equals(b["000001.SZ"])

    def test_too_few_bars_raises(self) -> None:
        """n_bars < 20 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            make_demo_data(n_bars=10)


# ═════════════════════════════════════════════
#  7. load_real_data 真实数据接入（P2-05）
# ═════════════════════════════════════════════

class TestLoadRealData:
    """P2-05：从生产 processed 目录加载真实 OHLCV（mock provider，不读真实目录）。"""

    def _fake_df(self, n: int = 30) -> pd.DataFrame:
        """构造一个最小有效 OHLCV DataFrame（中文表头，对齐 provider 返回口径）。"""
        idx = pd.date_range("2024-01-02", periods=n, freq="B")
        close = 10.0 + np.arange(n) * 0.01
        return pd.DataFrame(
            {
                "开盘价": close * 0.999,
                "最高价": close * 1.01,
                "最低价": close * 0.99,
                "收盘价": close,
                "成交量": 100_000.0,
                "成交额": close * 100_000.0,
            },
            index=idx,
        )

    def test_loads_available_symbols(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """成功加载的标的进入返回 dict；空 DataFrame 标的被跳过。"""
        fake_df = self._fake_df()

        class _FakeProvider:
            def __init__(self, processed_dir: str) -> None:
                self.processed_dir = processed_dir

            def get_bars(self, symbol: str, start_date: str = None, end_date: str = None):
                # 000002.SZ 模拟无数据（与生产 processed 缺文件行为一致）
                if symbol == "000002.SZ":
                    return pd.DataFrame()
                return fake_df

        monkeypatch.setattr(
            "myquant.data_access.provider.HistoricalDataProvider", _FakeProvider
        )
        data = load_real_data(
            symbols=["000001.SZ", "000002.SZ"],
            start_date="2024-01-01",
            end_date="2024-03-01",
        )
        assert set(data.keys()) == {"000001.SZ"}
        assert not data["000001.SZ"].empty

    def test_all_empty_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """所有标的都无数据时应抛 ValueError（fail-closed，不静默返回空 dict）。"""
        class _FakeProvider:
            def __init__(self, processed_dir: str) -> None:
                pass

            def get_bars(self, symbol: str, start_date: str = None, end_date: str = None):
                return pd.DataFrame()

        monkeypatch.setattr(
            "myquant.data_access.provider.HistoricalDataProvider", _FakeProvider
        )
        with pytest.raises(ValueError, match="未加载到真实数据"):
            load_real_data(
                symbols=["000001.SZ"], start_date="2024-01-01", end_date="2024-03-01"
            )

    def test_empty_symbols_raises(self) -> None:
        """symbols 为空时应抛 ValueError（防御式输入校验）。"""
        with pytest.raises(ValueError, match="symbols 不能为空"):
            load_real_data(symbols=[], start_date="2024-01-01", end_date="2024-03-01")
