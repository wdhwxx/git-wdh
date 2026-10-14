# 文件名：tests/unit/test_signal_consistency.py
"""
test_signal_consistency.py - C-10 回测实盘逐笔信号一致性测试（回写#239）
========================================================================
覆盖：
  1. 逐笔对齐对比（signal_id 主对齐 + symbol/时间模糊兜底）。
  2. 偏差率计算（mismatched/aligned；aligned=0 不除零）。
  3. 错配明细字段（signal_id/ts/symbol/回测方向/实盘方向/回测价/实盘价/偏差类型）。
  4. 偏差率超阈值（注入阈值）→ WARNING 告警附明细（category/source/dedup/details）。
  5. None 配置（PENDING_CAL）→ 只计算不告警、不编造。
  6. 健康序列不告警。
  7. 防未来：run_comparison 拒绝未来时间点。
  8. 防御式校验（缺 symbol / 非数 ts / 越界阈值 / 负容差）。
  9. set_gauge 指标注册（total/aligned/mismatched/mismatch_rate）。
  10. one-sided 单侧信号入明细但不进 aligned 分母；时间漂移超容差判定。

依赖: pytest, myquant.monitoring.signal_consistency。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.signal_consistency import (
    CATEGORY_SIGNAL_CONSISTENCY,
    DEDUP_DEVIATION_RATE,
    DEV_DIRECTION,
    DEV_MISSING_LIVE,
    DEV_PRICE,
    DEV_TIME,
    METRIC_ALIGNED,
    METRIC_MISMATCH_RATE,
    METRIC_MISMATCHED,
    METRIC_TOTAL,
    REGISTERED_METRICS,
    SOURCE_MODULE,
    SignalConsistencyChecker,
)

_BASE: float = 1_700_000_000.0


class _FakeAlert:
    """捕获 alert() 调用的告警管理器桩。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def alert(self, **kwargs: Any) -> int:
        self.calls.append(dict(kwargs))
        return 1


class _FakeMetrics:
    """捕获 set_gauge 的指标桩。"""

    def __init__(self) -> None:
        self.gauges: Dict[str, float] = {}

    def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
        self.gauges[name] = float(value)


def _sig(i: int, side: str = "buy", price: float = 10.0,
         ts: float = None, symbol: str = "000001.SZ",
         sid: str = None) -> Dict[str, Any]:
    """造一条信号 dict（默认 ts 间隔 60s，symbol 固定）。"""
    return {
        "signal_id": sid or f"s{i}",
        "timestamp": _BASE + i * 60.0 if ts is None else ts,
        "symbol": symbol,
        "side": side,
        "price": price,
    }


@pytest.fixture()
def clock_now() -> List[float]:
    return [_BASE + 3600.0]


def _make_checker(clock_now: List[float], **kw: Any) -> SignalConsistencyChecker:
    """构造固定时钟的校验器（注入 alert/metrics 桩，阈值可传）。"""
    kw.setdefault("alert_manager", _FakeAlert())
    kw.setdefault("metrics_collector", _FakeMetrics())
    kw.setdefault("clock", lambda: clock_now[0])
    return SignalConsistencyChecker(**kw)


class TestAlignmentAndRate:
    def test_完全一致序列_aligned_mismatch0(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now, signal_deviation_rate_max=0.1,
                          price_tolerance=0.01, time_tolerance=5.0)
        bt = [_sig(i, "buy", 10.0 + i * 0.01) for i in range(10)]
        live = [_sig(i, "buy", 10.0 + i * 0.01) for i in range(10)]
        p = c.run_comparison(bt, live)
        assert p["aligned"] == 10
        assert p["total"] == 10
        assert p["mismatched"] == 0
        assert p["mismatch_rate"] == 0.0

    def test_signal_id主对齐(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now, price_tolerance=0.01, time_tolerance=5.0)
        # signal_id 相同即配对，即使 ts 略不同（在容差内）
        bt = [_sig(0, "buy", 10.0, sid="X1")]
        live = [_sig(0, "buy", 10.0, ts=_BASE + 5.0, sid="X1")]
        p = c.run_comparison(bt, live)
        assert p["aligned"] == 1
        assert p["mismatched"] == 0

    def test_方向不一致判direction_mismatch(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now, price_tolerance=0.01, time_tolerance=5.0)
        bt = [_sig(0, "buy", 10.0), _sig(1, "buy", 10.0)]
        live = [_sig(0, "sell", 10.0), _sig(1, "buy", 10.0)]
        p = c.run_comparison(bt, live)
        assert p["mismatched"] == 1
        assert p["mismatch_rate"] == 0.5
        d = p["details"][0]
        assert DEV_DIRECTION in d["deviation_types"]
        assert d["backtest_side"] == "buy"
        assert d["live_side"] == "sell"

    def test_价格差超容差判price(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now, price_tolerance=0.01, time_tolerance=5.0)
        bt = [_sig(0, "buy", 10.0)]
        live = [_sig(0, "buy", 10.05)]   # 差 0.05 > 0.01
        p = c.run_comparison(bt, live)
        assert p["mismatched"] == 1
        assert DEV_PRICE in p["details"][0]["deviation_types"]
        assert p["details"][0]["backtest_price"] == 10.0
        assert p["details"][0]["live_price"] == 10.05

    def test_价格差在容差内不判price(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now, price_tolerance=0.10, time_tolerance=5.0)
        bt = [_sig(0, "buy", 10.0)]
        live = [_sig(0, "buy", 10.05)]   # 差 0.05 < 0.10
        p = c.run_comparison(bt, live)
        assert p["mismatched"] == 0

    def test_时间漂移超容差判time_drift(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now, price_tolerance=0.01, time_tolerance=5.0)
        bt = [_sig(1, "buy", 10.0)]
        live = [_sig(1, "buy", 10.0, ts=_BASE + 1 * 60.0 + 60.0)]  # 漂移 60s > 5s
        p = c.run_comparison(bt, live)
        assert p["mismatched"] == 1
        assert DEV_TIME in p["details"][0]["deviation_types"]

    def test_空序列不除零(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now, signal_deviation_rate_max=0.1)
        p = c.run_comparison([], [])
        assert p["aligned"] == 0
        assert p["mismatched"] == 0
        assert p["mismatch_rate"] == 0.0


class TestMismatchDetail:
    def test_明细字段齐全(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now, price_tolerance=0.01, time_tolerance=5.0)
        bt = [_sig(0, "buy", 10.5, sid="ABC", symbol="600519.SH")]
        live = [_sig(0, "sell", 11.0, sid="ABC", symbol="600519.SH")]
        p = c.run_comparison(bt, live)
        d = p["details"][0]
        assert d["signal_id"] == "ABC"
        assert d["symbol"] == "600519.SH"
        assert d["backtest_side"] == "buy"
        assert d["live_side"] == "sell"
        assert d["backtest_price"] == 10.5
        assert d["live_price"] == 11.0
        assert DEV_DIRECTION in d["deviation_types"]
        assert DEV_PRICE in d["deviation_types"]


class TestAlertOnThreshold:
    def test_偏差超阈触发WARNING附明细(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        c = _make_checker(clock_now, signal_deviation_rate_max=0.10,
                          price_tolerance=0.01, time_tolerance=5.0,
                          alert_manager=am)
        bt = [_sig(i, "buy", 10.0) for i in range(10)]
        live = [_sig(i, "buy", 10.0) for i in range(10)]
        live[3] = _sig(3, "sell", 10.0)   # 1 笔方向翻转 → 1/10=10%（不超 >10%）
        p = c.run_comparison(bt, live)
        # rate=0.1 不 > 0.10 → 不告警
        assert am.calls == []
        assert p["alert_triggered"] is False

    def test_偏差严格超阈才告警(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        c = _make_checker(clock_now, signal_deviation_rate_max=0.10,
                          price_tolerance=0.01, time_tolerance=5.0,
                          alert_manager=am)
        bt = [_sig(i, "buy", 10.0) for i in range(10)]
        live = [_sig(i, "buy", 10.0) for i in range(10)]
        live[3] = _sig(3, "sell", 10.0)
        live[5] = _sig(5, "sell", 10.0)   # 2/10=20% > 10%
        p = c.run_comparison(bt, live)
        assert len(am.calls) == 1
        a = am.calls[0]
        assert a["level"] == SeverityLevel.WARNING
        assert a["category"] == CATEGORY_SIGNAL_CONSISTENCY
        assert a["source"] == SOURCE_MODULE
        assert a["deduplication_key"] == DEDUP_DEVIATION_RATE
        assert a["details"]["mismatch_rate"] == 0.2
        assert a["details"]["rate_max"] == 0.10
        assert a["details"]["mismatched"] == 2
        assert a["details"]["aligned"] == 10
        assert len(a["details"]["details_summary"]) == 2
        assert p["alert_triggered"] is True


class TestPendingCalNoAlert:
    def test_阈值None不告警不编造(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        c = _make_checker(clock_now, signal_deviation_rate_max=None,
                          price_tolerance=0.01, time_tolerance=5.0,
                          alert_manager=am)
        bt = [_sig(i, "buy", 10.0) for i in range(5)]
        live = [_sig(i, "sell", 99.0) for i in range(5)]   # 全错
        p = c.run_comparison(bt, live)
        assert am.calls == []                 # 不告警
        assert p["mismatched"] == 5           # 指标照常计算
        assert p["aligned"] == 5
        assert p["alert_triggered"] is False

    def test_健康序列不告警(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        c = _make_checker(clock_now, signal_deviation_rate_max=0.01,
                          price_tolerance=0.01, time_tolerance=5.0,
                          alert_manager=am)
        p = c.run_comparison([_sig(i, "buy", 10.0) for i in range(5)],
                             [_sig(i, "buy", 10.0) for i in range(5)])
        assert am.calls == []
        assert p["alert_triggered"] is False


class TestFutureGuard:
    def test_拒绝未来回测信号(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now)
        with pytest.raises(ValueError):
            c.run_comparison([_sig(0, "buy", 10.0, ts=clock_now[0] + 100.0)], [])

    def test_拒绝未来实盘信号(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now)
        with pytest.raises(ValueError):
            c.run_comparison([], [_sig(0, "buy", 10.0, ts=clock_now[0] + 100.0)])

    def test_已发生信号正常通过(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now)
        past = clock_now[0] - 60.0
        p = c.run_comparison([_sig(0, "buy", 10.0, ts=past)],
                             [_sig(0, "buy", 10.0, ts=past)])
        assert p["aligned"] == 1


class TestDefensive:
    def test_缺symbol拒绝(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now)
        with pytest.raises(ValueError):
            c.run_comparison([{"timestamp": 1.0, "side": "buy", "price": 1.0}], [])

    def test_非数时间戳拒绝(self, clock_now: List[float]) -> None:
        c = _make_checker(clock_now)
        with pytest.raises(ValueError):
            c.run_comparison(
                [{"signal_id": "x", "timestamp": "not-a-ts", "symbol": "A", "side": "buy"}],
                [],
            )

    def test_越界偏差率阈值拒绝(self, clock_now: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_checker(clock_now, signal_deviation_rate_max=1.5)
        with pytest.raises(ValueError):
            _make_checker(clock_now, signal_deviation_rate_max=-0.1)

    def test_负容差拒绝(self, clock_now: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_checker(clock_now, price_tolerance=-1.0)
        with pytest.raises(ValueError):
            _make_checker(clock_now, time_tolerance=-5.0)

    def test_越界窗口拒绝(self, clock_now: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_checker(clock_now, deviation_window=0.0)


class TestOneSided:
    def test_one_sided入明细不进aligned分母(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        c = _make_checker(clock_now, signal_deviation_rate_max=None,
                          alert_manager=am)
        bt = [_sig(0, "buy", 10.0), _sig(1, "buy", 10.0)]
        live = [_sig(0, "buy", 10.0)]   # 缺 s1（回测有实盘无）
        p = c.run_comparison(bt, live)
        assert p["aligned"] == 1
        assert p["total"] == 2
        assert p["unmatched_backtest"] == 1
        assert p["unmatched_live"] == 0
        assert any(DEV_MISSING_LIVE in d["deviation_types"] for d in p["details"])


class TestMetrics:
    def test_set_gauge指标注册(self, clock_now: List[float]) -> None:
        met = _FakeMetrics()
        c = _make_checker(clock_now, signal_deviation_rate_max=0.1,
                          price_tolerance=0.01, time_tolerance=5.0,
                          metrics_collector=met)
        bt = [_sig(i, "buy", 10.0) for i in range(10)]
        live = [_sig(i, "buy", 10.0) for i in range(10)]
        live[3] = _sig(3, "sell", 10.0)   # 1/10
        p = c.run_comparison(bt, live)
        assert met.gauges[METRIC_TOTAL] == 10.0
        assert met.gauges[METRIC_ALIGNED] == 10.0
        assert met.gauges[METRIC_MISMATCHED] == 1.0
        assert abs(met.gauges[METRIC_MISMATCH_RATE] - p["mismatch_rate"]) < 1e-6

    def test_REGISTERED_METRICS声明(self) -> None:
        assert set(REGISTERED_METRICS) == {
            METRIC_TOTAL, METRIC_ALIGNED, METRIC_MISMATCHED, METRIC_MISMATCH_RATE,
        }
