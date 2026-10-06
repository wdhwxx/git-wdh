# 文件名：tests/unit/test_attribution.py
"""
test_attribution.py - C-5 收益归因分析框架测试（回写#234）
========================================================================
覆盖：
  1. 四维归因（strategy/factor/symbol/period）贡献值与占比计算正确性。
  2. 归因自洽性（总收益对账：每维 Σ组贡献 == 总收益）。
  3. 实盘/回测同口径对比差异（绝对差 abs_diff、相对差 rel_diff、缺失侧标注）。
  4. 日报归因摘要结构（四维归因表 + 差异表 + 口径说明 + self_consistent）。
  5. 可视化数据结构与 JSON 可序列化。
  6. 防未来：add_record 拒绝 timestamp > now 的记录。
  7. 防御式校验（非法 source/period、非有限 pnl、非 list 批量、缺字段）。
  8. backtest/metrics.py 归因侧纯函数直测（group_contribution /
     group_risk_contribution / compare_attribution / period_bucket_of）。

依赖: pytest, myquant.monitoring.attribution, myquant.backtest.metrics。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from myquant.backtest.metrics import (
    compare_attribution,
    group_contribution,
    group_risk_contribution,
    period_bucket_of,
)
from myquant.monitoring.attribution import (
    ALL_DIMENSIONS,
    DIMENSION_FACTOR,
    DIMENSION_PERIOD,
    DIMENSION_STRATEGY,
    DIMENSION_SYMBOL,
    PERIOD_DAY,
    PERIOD_WEEK,
    SOURCE_BACKTEST,
    SOURCE_LIVE,
    AttributionAnalyzer,
)

_BASE: float = 1_700_000_000.0
_DAY: float = 86_400.0


def _make_analyzer(clock_vals: List[float], period: str = PERIOD_DAY) -> AttributionAnalyzer:
    """构造固定时钟的归因分析器。"""
    return AttributionAnalyzer(period=period, clock=lambda: clock_vals[0])


# 合成记录：回测 trend_a/meanrev_b 两策略 × 两因子 × 两标的 × 4 日
def _bt_records() -> List[Dict[str, Any]]:
    return [
        {"timestamp": _BASE + 0 * _DAY, "pnl": 100.0, "strategy": "trend_a",
         "factor": "ma_cross", "symbol": "600519"},
        {"timestamp": _BASE + 1 * _DAY, "pnl": 50.0, "strategy": "trend_a",
         "factor": "rsi", "symbol": "600519"},
        {"timestamp": _BASE + 2 * _DAY, "pnl": -30.0, "strategy": "meanrev_b",
         "factor": "rsi", "symbol": "000001"},
        {"timestamp": _BASE + 3 * _DAY, "pnl": 80.0, "strategy": "trend_a",
         "factor": "ma_cross", "symbol": "000001"},
    ]


def _live_records() -> List[Dict[str, Any]]:
    return [
        {"timestamp": _BASE + 0 * _DAY, "pnl": 80.0, "strategy": "trend_a",
         "factor": "ma_cross", "symbol": "600519"},
        {"timestamp": _BASE + 1 * _DAY, "pnl": 40.0, "strategy": "trend_a",
         "factor": "rsi", "symbol": "600519"},
        {"timestamp": _BASE + 2 * _DAY, "pnl": -10.0, "strategy": "meanrev_b",
         "factor": "rsi", "symbol": "000001"},
        {"timestamp": _BASE + 3 * _DAY, "pnl": 90.0, "strategy": "trend_a",
         "factor": "ma_cross", "symbol": "000001"},
    ]


@pytest.fixture()
def clock_vals() -> List[float]:
    return [_BASE + 5 * _DAY + 3600.0]  # 第 5 天 1 小时后（所有记录均为过去）


class TestFourDimensionAttribution:
    """1. 四维归因计算正确性。"""

    def test_策略维贡献与占比(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        bt = an.attribution(SOURCE_BACKTEST)
        # 总盈亏 = 100+50-30+80 = 200
        assert bt["total_pnl"] == pytest.approx(200.0)
        strat = {r["group"]: r for r in bt["dimensions"][DIMENSION_STRATEGY]}
        # trend_a = 100+50+80 = 230；meanrev_b = -30
        assert strat["trend_a"]["contribution"] == pytest.approx(230.0)
        assert strat["trend_a"]["ratio"] == pytest.approx(230.0 / 200.0)
        assert strat["trend_a"]["count"] == 3
        assert strat["meanrev_b"]["contribution"] == pytest.approx(-30.0)

    def test_因子维贡献(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        fac = {r["group"]: r for r in an.attribution(SOURCE_BACKTEST)["dimensions"][DIMENSION_FACTOR]}
        # ma_cross = 100+80 = 180；rsi = 50-30 = 20
        assert fac["ma_cross"]["contribution"] == pytest.approx(180.0)
        assert fac["rsi"]["contribution"] == pytest.approx(20.0)

    def test_标的维贡献(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        sym = {r["group"]: r for r in an.attribution(SOURCE_BACKTEST)["dimensions"][DIMENSION_SYMBOL]}
        # 600519 = 100+50 = 150；000001 = -30+80 = 50
        assert sym["600519"]["contribution"] == pytest.approx(150.0)
        assert sym["000001"]["contribution"] == pytest.approx(50.0)

    def test_时段维日桶(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals, period=PERIOD_DAY)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        per = an.attribution(SOURCE_BACKTEST)["dimensions"][DIMENSION_PERIOD]
        # 4 个不同自然日 → 4 个桶
        assert len(per) == 4
        # 每桶贡献为当日盈亏
        pmap = {r["group"]: r["contribution"] for r in per}
        assert sum(pmap.values()) == pytest.approx(200.0)

    def test_风险贡献字段存在(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        rows = an.attribution(SOURCE_BACKTEST)["dimensions"][DIMENSION_STRATEGY]
        for r in rows:
            assert "risk" in r and "risk_ratio" in r


class TestSelfConsistency:
    """2. 归因自洽性（总收益对账）。"""

    def test_每维组贡献之和等于总盈亏(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        an.add_records(SOURCE_LIVE, _live_records())
        for source in (SOURCE_BACKTEST, SOURCE_LIVE):
            attr = an.attribution(source)
            for dim in ALL_DIMENSIONS:
                s = sum(r["contribution"] for r in attr["dimensions"][dim])
                assert s == pytest.approx(attr["total_pnl"], abs=1e-6)

    def test_日报自洽标记为真(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        summary = an.daily_summary()
        assert summary["self_consistent"] is True

    def test_总盈亏为零时占比置零仍自洽(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        # 总盈亏 = 0：两笔 +50 / -50
        an.add_record(SOURCE_LIVE, _BASE, pnl=50.0, strategy="a", factor="f", symbol="x")
        an.add_record(SOURCE_LIVE, _BASE + _DAY, pnl=-50.0, strategy="b", factor="f", symbol="x")
        attr = an.attribution(SOURCE_LIVE)
        assert attr["total_pnl"] == pytest.approx(0.0)
        for r in attr["dimensions"][DIMENSION_STRATEGY]:
            assert r["ratio"] == 0.0  # 分母为零置 0，不抛异常


class TestLiveBacktestCompare:
    """3. 实盘/回测对比差异。"""

    def test_绝对差与相对差(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        an.add_records(SOURCE_LIVE, _live_records())
        cmp = an.compare()
        strat = {r["group"]: r for r in cmp[DIMENSION_STRATEGY]}
        # trend_a: bt=230, live=80+40+90=210 → abs_diff=-20
        assert strat["trend_a"]["backtest_value"] == pytest.approx(230.0)
        assert strat["trend_a"]["live_value"] == pytest.approx(210.0)
        assert strat["trend_a"]["abs_diff"] == pytest.approx(-20.0)
        assert strat["trend_a"]["rel_diff"] == pytest.approx(-20.0 / 230.0)
        # meanrev_b: bt=-30, live=-10 → abs_diff=20
        assert strat["meanrev_b"]["abs_diff"] == pytest.approx(20.0)

    def test_回测基准为零相对差为None(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_record(SOURCE_BACKTEST, _BASE, pnl=0.0, strategy="new_s", factor="f", symbol="x")
        an.add_record(SOURCE_LIVE, _BASE, pnl=100.0, strategy="new_s", factor="f", symbol="x")
        cmp = an.compare()
        row = [r for r in cmp[DIMENSION_STRATEGY] if r["group"] == "new_s"][0]
        assert row["abs_diff"] == pytest.approx(100.0)
        assert row["rel_diff"] is None  # bt=0 → 无定义

    def test_缺失侧标注(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        # 回测独有策略；实盘独有策略
        an.add_record(SOURCE_BACKTEST, _BASE, pnl=10.0, strategy="only_bt", factor="f", symbol="x")
        an.add_record(SOURCE_LIVE, _BASE, pnl=20.0, strategy="only_live", factor="f", symbol="x")
        cmp = an.compare()
        groups = {r["group"]: r for r in cmp[DIMENSION_STRATEGY]}
        assert groups["only_bt"]["missing"] == "live"
        assert groups["only_bt"]["live_value"] == pytest.approx(0.0)
        assert groups["only_live"]["missing"] == "backtest"
        assert groups["only_live"]["backtest_value"] == pytest.approx(0.0)


class TestDailySummary:
    """4. 日报归因摘要结构。"""

    def test_摘要结构齐全(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        an.add_records(SOURCE_LIVE, _live_records())
        s = an.daily_summary()
        # 四维归因表（backtest/live）+ 差异表（comparison）
        for side in ("backtest", "live"):
            assert set(s[side]["dimensions"].keys()) == set(ALL_DIMENSIONS)
            assert "total_pnl" in s[side] and "record_count" in s[side]
        assert set(s["comparison"].keys()) == set(ALL_DIMENSIONS)
        # 口径说明与对账标记
        assert "caliber_note" in s and s["caliber_note"]
        assert s["method"] == "additive_pnl"
        assert s["self_consistent"] is True

    def test_摘要JSON可序列化(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        an.add_records(SOURCE_LIVE, _live_records())
        s = an.daily_summary()
        text = json.dumps(s, ensure_ascii=False)
        back = json.loads(text)
        assert back["backtest"]["total_pnl"] == pytest.approx(200.0)


class TestVisualizationData:
    """5. 可视化数据结构与 JSON 可序列化。"""

    def test_可视化结构(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        an.add_records(SOURCE_LIVE, _live_records())
        viz = an.visualization_data()
        assert set(viz["dimensions"].keys()) == set(ALL_DIMENSIONS)
        for dim in ALL_DIMENSIONS:
            block = viz["dimensions"][dim]
            assert "backtest_series" in block and "live_series" in block
            assert "diff_table" in block
            for pt in block["backtest_series"]:
                assert {"label", "value", "ratio"} <= set(pt)
            for row in block["diff_table"]:
                assert {"label", "backtest", "live", "abs_diff", "rel_diff"} <= set(row)
        # period 时序
        assert "period_series" in viz
        assert viz["period_series"]["backtest"]
        assert viz["period_series"]["live"]
        # 时序按周期升序
        periods = [p["period"] for p in viz["period_series"]["backtest"]]
        assert periods == sorted(periods)

    def test_可视化JSON可序列化(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        an.add_records(SOURCE_BACKTEST, _bt_records())
        an.add_records(SOURCE_LIVE, _live_records())
        viz = an.visualization_data()
        text = json.dumps(viz, ensure_ascii=False)  # 不抛异常即通过
        assert isinstance(text, str) and len(text) > 0


class TestAntiFuture:
    """6. 防未来：拒绝未来时间戳记录。"""

    def test_拒绝未来记录(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        with pytest.raises(ValueError, match="未来"):
            an.add_record(SOURCE_LIVE, clock_vals[0] + 100.0, pnl=1.0)

    def test_接受当前时刻记录(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        # 等于 now（容差内）应接受
        an.add_record(SOURCE_LIVE, clock_vals[0], pnl=1.0)
        assert an.attribution(SOURCE_LIVE)["record_count"] == 1

    def test_批量摄入跳过未来行(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        recs = [
            {"timestamp": _BASE, "pnl": 1.0, "strategy": "a", "factor": "f", "symbol": "x"},
            {"timestamp": clock_vals[0] + 9999.0, "pnl": 1.0, "strategy": "a", "factor": "f",
             "symbol": "x"},  # 未来行应被跳过
        ]
        n = an.add_records(SOURCE_LIVE, recs)
        assert n == 1


class TestDefensive:
    """7. 防御式校验。"""

    def test_非法source拒绝(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        with pytest.raises(ValueError):
            an.add_record("paper", _BASE, pnl=1.0)
        with pytest.raises(ValueError):
            an.attribution("paper")

    def test_非法period拒绝(self, clock_vals: List[float]) -> None:
        with pytest.raises(ValueError):
            AttributionAnalyzer(period="month")

    def test_非有限pnl拒绝(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        with pytest.raises(ValueError):
            an.add_record(SOURCE_LIVE, _BASE, pnl=float("nan"))
        with pytest.raises(ValueError):
            an.add_record(SOURCE_LIVE, _BASE, pnl=float("inf"))

    def test_非可解析时间戳拒绝(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        with pytest.raises(ValueError):
            an.add_record(SOURCE_LIVE, "not-a-ts", pnl=1.0)  # type: ignore[arg-type]

    def test_非list批量拒绝(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        with pytest.raises(ValueError):
            an.add_records(SOURCE_LIVE, {"timestamp": _BASE, "pnl": 1.0})  # type: ignore[arg-type]

    def test_空记录归因(self, clock_vals: List[float]) -> None:
        an = _make_analyzer(clock_vals)
        attr = an.attribution(SOURCE_BACKTEST)
        assert attr["total_pnl"] == 0.0
        for dim in ALL_DIMENSIONS:
            assert attr["dimensions"][dim] == []


class TestMetricsPureFunctions:
    """8. backtest/metrics.py 归因侧纯函数直测。"""

    def test_group_contribution基本口径(self) -> None:
        recs = [
            {"strategy": "a", "pnl": 10.0},
            {"strategy": "a", "pnl": 20.0},
            {"strategy": "b", "pnl": -5.0},
        ]
        rows = group_contribution(recs, "strategy")
        m = {r["group"]: r for r in rows}
        assert m["a"]["contribution"] == pytest.approx(30.0)
        assert m["a"]["ratio"] == pytest.approx(30.0 / 25.0)
        assert m["a"]["count"] == 2
        assert m["b"]["contribution"] == pytest.approx(-5.0)
        # 按 |贡献| 降序
        assert rows[0]["group"] == "a"

    def test_group_contribution空输入(self) -> None:
        assert group_contribution([], "strategy") == []

    def test_group_contribution总盈亏为零(self) -> None:
        recs = [{"strategy": "a", "pnl": 10.0}, {"strategy": "b", "pnl": -10.0}]
        rows = group_contribution(recs, "strategy")
        for r in rows:
            assert r["ratio"] == 0.0

    def test_group_contribution非法输入(self) -> None:
        with pytest.raises(ValueError):
            group_contribution("not-a-list", "strategy")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            group_contribution([{"a": 1.0}], "")

    def test_period_bucket_of_day_week(self) -> None:
        # 2024-01-01 是周一
        ts = 1_704_067_200.0  # 2024-01-01 00:00 UTC+8 附近
        day_key = period_bucket_of(ts, PERIOD_DAY)
        assert len(day_key) == 10 and day_key[4] == "-"
        week_key = period_bucket_of(ts, PERIOD_WEEK)
        assert week_key.endswith("-W01") or "-W" in week_key
        with pytest.raises(ValueError):
            period_bucket_of(ts, "month")

    def test_group_risk_contribution(self) -> None:
        # 策略 a 两周期 [10, 20]；策略 b 两周期 [0, 0]
        recs = [
            {"strategy": "a", "period_bucket": "d1", "pnl": 10.0},
            {"strategy": "a", "period_bucket": "d2", "pnl": 20.0},
            {"strategy": "b", "period_bucket": "d1", "pnl": 0.0},
            {"strategy": "b", "period_bucket": "d2", "pnl": 0.0},
        ]
        rows = group_risk_contribution(recs, "strategy")
        m = {r["group"]: r for r in rows}
        assert m["a"]["periods"] == 2
        assert m["a"]["risk"] > 0  # std([10,20]) > 0
        assert m["b"]["risk"] == 0.0

    def test_compare_attribution差异对齐(self) -> None:
        bt = [{"group": "a", "contribution": 100.0}, {"group": "b", "contribution": 50.0}]
        live = [{"group": "a", "contribution": 120.0}, {"group": "c", "contribution": 10.0}]
        rows = compare_attribution(bt, live)
        m = {r["group"]: r for r in rows}
        assert m["a"]["abs_diff"] == pytest.approx(20.0)
        assert m["a"]["rel_diff"] == pytest.approx(0.2)
        assert m["b"]["missing"] == "live"  # 回测有、实盘无
        assert m["b"]["live_value"] == pytest.approx(0.0)
        assert m["c"]["missing"] == "backtest"  # 实盘有、回测无
        # bt=100 非零 → rel_diff 有值；若 bt=0 则 None
        bt2 = [{"group": "x", "contribution": 0.0}]
        live2 = [{"group": "x", "contribution": 5.0}]
        r2 = compare_attribution(bt2, live2)
        assert r2[0]["rel_diff"] is None
