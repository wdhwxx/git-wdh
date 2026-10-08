# 文件名：tests/unit/test_rolling_performance.py
"""
test_rolling_performance.py - C-2 滚动绩效指标监控测试（回写#232）
========================================================================
覆盖：
  1. 滚动窗口（日/周/月）收益、夏普、回撤、胜率计算。
  2. 日内回撤 / 日内亏损超策略阈值 → WARNING 告警（category=strategy、
     dedup=strategy:intraday_drawdown/loss:<name>、details 附值）。
  3. 阈值未配置（PENDING_CAL=None）→ 只计算不告警、不编造数值。
  4. 基于 metrics_history 回放（真实临时库）+ 策略标签过滤。
  5. 防未来：record 拒绝 timestamp>now 的绩效点。
  6. 防御式校验（空策略 / 负阈值 / 非有限权益 / 非法 store / 空 metric_name）。
  7. set_gauge 指标按策略 labels 落盘。
  8. record 幂等去重。

依赖: pytest, myquant.monitoring.rolling_performance,
      myquant.core.metrics_history, myquant.database.connection。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from myquant.core.constants import SeverityLevel
from myquant.database.connection import DatabaseConnection
from myquant.core.field_crypto import FieldCrypto
from myquant.core.metrics_history import MetricsHistoryStore
from myquant.monitoring.rolling_performance import (
    DEFAULT_STRATEGY,
    METRIC_INTRADAY_DRAWDOWN,
    METRIC_RETURN_30D,
    RollingPerformanceMonitor,
)

_BASE: float = 1_700_000_000.0
_DAY: float = 86_400.0


class _FakeAlert:
    """捕获 alert() 调用的告警管理器桩。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def alert(self, **kwargs: Any) -> int:
        self.calls.append(dict(kwargs))
        return 1


class _FakeMetrics:
    """捕获 set_gauge 的指标桩（labels 并入 key）。"""

    def __init__(self) -> None:
        self.gauges: Dict[str, float] = {}

    def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
        self.gauges[f"{name}:{(labels or {}).get('strategy', '?')}"] = float(value)


def _make_monitor(
    clock_vals: List[float],
    alert: Any = None,
    metrics: Any = None,
    limit: Any = 10.0,
) -> RollingPerformanceMonitor:
    """构造固定时钟的监控器（阈值可注入/缺省 10.0）。"""
    return RollingPerformanceMonitor(
        intraday_drawdown_limit=limit,
        alert_manager=alert if alert is not None else _FakeAlert(),
        metrics_collector=metrics if metrics is not None else _FakeMetrics(),
        clock=lambda: clock_vals[0],
    )


def _ten_day_equity() -> List[float]:
    """10 个自然日日末权益序列（100→110，含一次 105→101 回撤）。"""
    return [100.0, 102.0, 99.0, 103.0, 105.0, 101.0, 106.0, 108.0, 107.0, 110.0]


@pytest.fixture()
def clock_vals() -> List[float]:
    return [_BASE + 10 * _DAY + 3600.0]  # 第 10 天 1 小时后


class TestRollingMetrics:
    def test_滚动收益_30d(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        for i, eq in enumerate(_ten_day_equity()):
            mon.record("trend_a", _BASE + i * _DAY, eq)
        st = mon.poll()
        ra = st["strategies"]["trend_a"]
        assert ra["rolling"]["30d"]["return_pct"] == 10.0   # (110/100-1)*100
        assert ra["rolling"]["30d"]["days"] == 10
        assert ra["point_count"] == 10

    def test_滚动收益_7d_1d(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        for i, eq in enumerate(_ten_day_equity()):
            mon.record("trend_a", _BASE + i * _DAY, eq)
        ra = mon.poll()["strategies"]["trend_a"]
        # 7d 窗口=最后 7 日（103→110）→ 收益≈6.8%
        assert ra["rolling"]["7d"]["return_pct"] is not None
        assert ra["rolling"]["7d"]["return_pct"] > 5.0
        # 1d 窗口=最新 1 日 → 只有 1 个日末点 → return None（样本不足）
        assert ra["rolling"]["1d"]["return_pct"] is None
        assert ra["rolling"]["1d"]["days"] == 1

    def test_夏普_回撤_胜率(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        for i, eq in enumerate(_ten_day_equity()):
            mon.record("trend_a", _BASE + i * _DAY, eq)
        r30 = mon.poll()["strategies"]["trend_a"]["rolling"]["30d"]
        assert r30["sharpe"] is not None and r30["sharpe"] > 0      # 年化夏普>0
        assert r30["drawdown_pct"] > 0                              # 有回撤
        assert r30["win_rate"] is not None and 0 <= r30["win_rate"] <= 100

    def test_多策略分列(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        for i, eq in enumerate(_ten_day_equity()):
            mon.record("trend_a", _BASE + i * _DAY, eq)
            mon.record("trend_b", _BASE + i * _DAY, eq * 2.0)
        st = mon.poll()
        assert set(st["strategies"].keys()) == {"trend_a", "trend_b"}


class TestIntradayAlerts:
    def test_日内回撤超阈_WARNING(self, clock_vals: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_vals, alert=am, limit=10.0)
        now = clock_vals[0]
        mon.record("trend_a", now - 1800.0, 110.0)
        mon.record("trend_a", now - 900.0, 88.0)   # 回撤 (110-88)/110=20% >10%
        st = mon.poll()
        ra = st["strategies"]["trend_a"]
        assert ra["intraday_drawdown"] is not None and abs(ra["intraday_drawdown"] - 20.0) < 0.01
        dd = [c for c in am.calls if c["deduplication_key"] == "strategy:intraday_drawdown:trend_a"]
        assert len(dd) == 1
        assert dd[0]["level"] == SeverityLevel.WARNING
        assert dd[0]["category"] == "strategy"
        assert dd[0]["details"]["intraday_drawdown_limit"] == 10.0
        assert dd[0]["details"]["intraday_drawdown"] > 10.0
        assert st["alert_triggered"] is True

    def test_日内亏损超阈_WARNING(self, clock_vals: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_vals, alert=am, limit=10.0)
        now = clock_vals[0]
        mon.record("trend_a", now - 1800.0, 100.0)
        mon.record("trend_a", now - 900.0, 60.0)   # 收益 (60/100-1)=-40% → |亏损|>10
        mon.poll()
        loss = [c for c in am.calls if c["deduplication_key"] == "strategy:intraday_loss:trend_a"]
        assert len(loss) == 1
        assert loss[0]["level"] == SeverityLevel.WARNING
        assert loss[0]["category"] == "strategy"

    def test_阈值未配置_不告警不编造(self, clock_vals: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_vals, alert=am, limit=None)  # PENDING_CAL
        now = clock_vals[0]
        mon.record("trend_a", now - 1800.0, 110.0)
        mon.record("trend_a", now - 900.0, 50.0)   # 回撤巨大
        st = mon.poll()
        assert am.calls == []                                     # 不告警
        ra = st["strategies"]["trend_a"]
        assert ra["intraday_drawdown"] is not None               # 指标照常计算
        assert ra["intraday_drawdown"] > 40.0
        assert st["alert_triggered"] is False

    def test_健康日不告警(self, clock_vals: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_vals, alert=am, limit=10.0)
        now = clock_vals[0]
        mon.record("trend_a", now - 1800.0, 100.0)
        mon.record("trend_a", now - 900.0, 102.0)   # 当日上涨
        mon.poll()
        assert am.calls == []


class TestReplayFromHistory:
    @pytest.fixture()
    def store(self, tmp_path) -> MetricsHistoryStore:
        conn = DatabaseConnection(str(tmp_path / "rp.db"))
        s = MetricsHistoryStore(connection=conn)
        s.attach_field_crypto(FieldCrypto(key_file=str(tmp_path / "k")))
        return s

    def test_回放并按策略标签归组(self, store: MetricsHistoryStore, clock_vals: List[float]) -> None:
        for i, eq in enumerate(_ten_day_equity()):
            store.save_metric("strategy_equity", eq, labels={"strategy": "trend_a"},
                              timestamp=_BASE + i * _DAY)
        mon = _make_monitor(clock_vals)
        n = mon.replay_from_history(store, "strategy_equity")
        assert n == 10
        ra = mon.poll()["strategies"]["trend_a"]
        assert ra["point_count"] == 10
        assert ra["rolling"]["30d"]["return_pct"] == 10.0

    def test_回放策略过滤(self, store: MetricsHistoryStore, clock_vals: List[float]) -> None:
        for i, eq in enumerate(_ten_day_equity()):
            store.save_metric("strategy_equity", eq, labels={"strategy": "trend_a"},
                              timestamp=_BASE + i * _DAY)
            store.save_metric("strategy_equity", eq * 2.0, labels={"strategy": "trend_b"},
                              timestamp=_BASE + i * _DAY)
        mon = _make_monitor(clock_vals)
        n = mon.replay_from_history(store, "strategy_equity", strategy="trend_b")
        assert n == 10
        st = mon.poll()
        assert "trend_b" in st["strategies"]
        assert "trend_a" not in st["strategies"]

    def test_回放非法store或metric_name(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        with pytest.raises(ValueError):
            mon.replay_from_history(None, "strategy_equity")
        with pytest.raises(ValueError):
            mon.replay_from_history(_FakeMetrics(), "")


class TestDefensiveAndFuture:
    def test_拒绝未来绩效点(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        with pytest.raises(ValueError):
            mon.record("trend_a", clock_vals[0] + 100.0, 120.0)

    def test_空策略拒绝(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        with pytest.raises(ValueError):
            mon.record("", 1.0, 100.0)

    def test_非有限权益拒绝(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        with pytest.raises(ValueError):
            mon.record("trend_a", 1.0, float("nan"))

    def test_负阈值拒绝(self, clock_vals: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_monitor(clock_vals, limit=-1.0)

    def test_record幂等去重(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        mon.record("trend_a", _BASE, 100.0)
        mon.record("trend_a", _BASE, 100.0)   # 同策略同 ts 幂等
        assert mon._points["trend_a"] == [(_BASE, 100.0)]


class TestMetricsGauges:
    def test_set_gauge按策略labels(self, clock_vals: List[float]) -> None:
        met = _FakeMetrics()
        mon = _make_monitor(clock_vals, metrics=met, limit=None)
        for i, eq in enumerate(_ten_day_equity()):
            mon.record("trend_a", _BASE + i * _DAY, eq)
        mon.poll()
        assert f"{METRIC_RETURN_30D}:trend_a" in met.gauges
        assert f"{METRIC_INTRADAY_DRAWDOWN}:trend_a" in met.gauges
        assert met.gauges[f"{METRIC_RETURN_30D}:trend_a"] == 10.0
