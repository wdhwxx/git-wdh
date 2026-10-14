# 文件名：tests/unit/test_margin_monitor.py
"""
test_margin_monitor.py - 保证金率/资金费率监控单元测试（B-9，回写#229）
==========================================================================
验收口径（清单 B-9 行）：保证金/强平价进入看板；接近强平 → P0 告警；阈值取自 P0-2。
本测试用注入桩组合 + 假告警 + 假指标 + 真实 EventBus 验证：
  EVENT 路径（成交/持仓变动/标记价事件触发重算，不等周期巡检）；
  强平距离 < 阈值 → P0；POLL 兜底；未配置（None）不告警不编造；
  指标可见；防未来（只消费已发生快照时间戳）；计算防御（缺失数据降级不抛）。

注意：LivePortfolio 正被 B-1 并行修改——本测试**不顶层 import portfolio**，
全部用鸭子类型桩注入，避免 import 耦合失败。
"""

from __future__ import annotations

import pytest

from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import Event, EventBus, EventType
from myquant.monitoring.margin_monitor import (
    DEDUP_FUNDING,
    DEDUP_STRONG_FLAT,
    DEDUP_USAGE,
    REGISTERED_METRICS,
    MarginMonitor,
)


# ─────────────────────────────────────────────
#  夹具
# ─────────────────────────────────────────────
class _FakeAlert:
    """记录 alert() 调用的假告警管理器。"""

    def __init__(self) -> None:
        self.calls: list = []

    def alert(self, **kw):
        self.calls.append(kw)
        return 1


class _FakeMetrics:
    """记录 set_gauge 调用的假指标采集器（可 get_value 查询）。"""

    def __init__(self) -> None:
        self.gauges: dict = {}

    def set_gauge(self, name, value, labels=None):
        self.gauges[name] = float(value)

    def get_value(self, name, default=0.0):
        return self.gauges.get(name, default)


class _StubPortfolio:
    """鸭子类型组合：get_account/get_market_value/get_total_assets（只读公开 getter）。"""

    def __init__(self, equity: float, frozen: float, mv: float) -> None:
        self._equity = float(equity)
        self._frozen = float(frozen)
        self._mv = float(mv)
        self.account_reads = 0

    def set_equity(self, equity: float) -> None:
        """模拟标记价/成交后权益变动（已发生事实）。"""
        self._equity = float(equity)

    def get_account(self):
        self.account_reads += 1
        return {
            "total_assets": self._equity,
            "frozen_cash": self._frozen,
            "market_value": self._mv,
        }

    def get_market_value(self):
        return self._mv

    def get_total_assets(self):
        return self._equity


class _Clock:
    """可控时钟（防未来：last_calc_time 应等于本时钟）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _make_monitor(pf: _StubPortfolio, am: _FakeAlert, met: _FakeMetrics,
                  clock: _Clock, **kw) -> MarginMonitor:
    """构造测试用监控器：默认强平距离阈值 5%、维持保证金率 0.5%。"""
    defaults = dict(
        portfolio=pf,
        strong_flat_distance_pct=5.0,
        maintenance_margin_rate=0.005,
        alert_manager=am,
        metrics_collector=met,
        clock=clock,
    )
    defaults.update(kw)
    return MarginMonitor(**defaults)


# ─────────────────────────────────────────────
#  计算口径
# ─────────────────────────────────────────────
class TestCalculation:
    """保证金占用率/维持保证金/强平距离/资金费率计算口径。"""

    def test_占用率与强平距离口径(self) -> None:
        # 权益=100000，冻结(占用)=10000，市值=100000，维持保证金率=0.005
        # 占用率=10000/100000*100=10%；维持保证金=100000*0.005=500；
        # 强平距离=(100000-500)/100000*100=99.5%
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        mon = _make_monitor(pf, _FakeAlert(), _FakeMetrics(), clock)
        st = mon.refresh()
        assert st["margin_usage_ratio"] == pytest.approx(10.0)
        assert st["maintenance_margin"] == pytest.approx(500.0)
        assert st["liquidation_distance_pct"] == pytest.approx(99.5)

    def test_维持保证金率未启用则距离不计算(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        # maintenance_margin_rate 不传 → None=未启用
        mon = MarginMonitor(
            portfolio=pf, strong_flat_distance_pct=5.0,
            alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics(), clock=clock,
        )
        st = mon.refresh()
        assert st["maintenance_margin"] is None
        assert st["liquidation_distance_pct"] is None

    def test_资金费率隔夜成本口径(self) -> None:
        # 市值=100000，费率=0.001 → 隔夜成本=100
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=0.0, mv=100000.0)
        mon = MarginMonitor(
            portfolio=pf, funding_rate=0.001,
            alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics(), clock=clock,
        )
        st = mon.refresh()
        assert st["funding_cost_estimate"] == pytest.approx(100.0)
        assert st["funding_rate"] == pytest.approx(0.001)


# ─────────────────────────────────────────────
#  EVENT 路径（R-02）
# ─────────────────────────────────────────────
class TestEventPath:
    """成交/持仓/标记价事件触发重算（不等周期巡检）。"""

    def test_成交事件触发重算(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        am, met = _FakeAlert(), _FakeMetrics()
        mon = _make_monitor(pf, am, met, clock)
        bus = EventBus()
        mon.attach_event_bus(bus)

        before = pf.account_reads
        bus.publish(Event(event_type=EventType.ORDER_FILLED))
        # 事件路径已触发一次重算（未走 poll）
        assert pf.account_reads == before + 1

    def test_持仓更新事件触发重算(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        mon = _make_monitor(pf, _FakeAlert(), _FakeMetrics(), clock)
        bus = EventBus()
        mon.attach_event_bus(bus)
        before = pf.account_reads
        bus.publish(Event(event_type=EventType.POSITION_UPDATE))
        assert pf.account_reads == before + 1

    def test_标记价变动tick触发重算(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        mon = _make_monitor(pf, _FakeAlert(), _FakeMetrics(), clock)
        bus = EventBus()
        mon.attach_event_bus(bus)
        before = pf.account_reads
        # 标记价变动 = TICK 行情事件
        bus.publish(Event(event_type=EventType.TICK))
        assert pf.account_reads == before + 1

    def test_标记价恶化后事件路径立即P0(self) -> None:
        # 先健康；标记价变动(TICK)使权益恶化到距离<5% → 事件路径直接 P0，不等 poll
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=0.0, mv=100000.0)
        am = _FakeAlert()
        mon = _make_monitor(pf, am, _FakeMetrics(), clock)
        mon.refresh()
        assert am.calls == []  # 健康不告警

        bus = EventBus()
        mon.attach_event_bus(bus)
        pf.set_equity(510.0)  # 标记价大跌 → 权益恶化（已发生事实）
        bus.publish(Event(event_type=EventType.TICK))
        p0 = [c for c in am.calls if c["deduplication_key"] == DEDUP_STRONG_FLAT]
        assert len(p0) == 1
        assert p0[0]["level"] == SeverityLevel.CRITICAL


# ─────────────────────────────────────────────
#  强平距离 P0 告警
# ─────────────────────────────────────────────
class TestStrongFlatAlert:
    """注入 strong_flat_distance_pct=5.0 → 距离<5% → P0 告警。"""

    def test_距离低于阈值触发P0(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=510.0, frozen=0.0, mv=100000.0)
        am = _FakeAlert()
        mon = _make_monitor(pf, am, _FakeMetrics(), clock)  # strong_flat=5.0
        st = mon.refresh()
        # (510-500)/510*100 ≈ 1.96% < 5%
        assert st["liquidation_distance_pct"] < 5.0
        p0 = [c for c in am.calls if c["deduplication_key"] == DEDUP_STRONG_FLAT]
        assert len(p0) == 1
        assert p0[0]["level"] == SeverityLevel.CRITICAL
        assert p0[0]["category"] == "margin"
        # message 含距离与阈值
        assert "%" in p0[0]["message"]

    def test_距离高于阈值不告警(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=0.0, mv=100000.0)
        am = _FakeAlert()
        mon = _make_monitor(pf, am, _FakeMetrics(), clock)
        mon.refresh()
        assert am.calls == []


# ─────────────────────────────────────────────
#  保证金占用超阈 P0
# ─────────────────────────────────────────────
class TestUsageAlert:
    """占用率超阈 → P0 强平预警（阈值 PENDING_CAL，配置驱动）。"""

    def test_占用率超阈触发P0(self) -> None:
        clock = _Clock(1000.0)
        # 占用=50000/权益=100000=50% > 阈值15%
        pf = _StubPortfolio(equity=100000.0, frozen=50000.0, mv=100000.0)
        am = _FakeAlert()
        mon = MarginMonitor(
            portfolio=pf, margin_usage_ratio_threshold=15.0,
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        mon.refresh()
        p0 = [c for c in am.calls if c["deduplication_key"] == DEDUP_USAGE]
        assert len(p0) == 1
        assert p0[0]["level"] == SeverityLevel.CRITICAL


# ─────────────────────────────────────────────
#  POLL 兜底
# ─────────────────────────────────────────────
class TestPollFallback:
    """poll() 周期兜底重算。"""

    def test_poll重算并返回快照(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        mon = _make_monitor(pf, _FakeAlert(), _FakeMetrics(), clock)
        reads_before = pf.account_reads
        st = mon.poll()
        assert pf.account_reads == reads_before + 1
        assert "margin_usage_ratio" in st


# ─────────────────────────────────────────────
#  未配置（None）→ 不告警不编造
# ─────────────────────────────────────────────
class TestUnconfiguredNone:
    """阈值未配置（None）→ 不触发 P0、不编造数值；指标照常计算。"""

    def test_未配置强平阈值不告警(self) -> None:
        clock = _Clock(1000.0)
        # 权益恶化到距离≈1.96%，但 strong_flat_distance_pct=None
        pf = _StubPortfolio(equity=510.0, frozen=0.0, mv=100000.0)
        am = _FakeAlert()
        mon = MarginMonitor(
            portfolio=pf, strong_flat_distance_pct=None, maintenance_margin_rate=0.005,
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        st = mon.refresh()
        assert am.calls == []  # 不告警
        assert st["liquidation_distance_pct"] is not None  # 指标照常计算
        assert st["strong_flat_distance_pct"] is None  # 不编造阈值

    def test_未配置占用阈值不告警(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=90000.0, mv=100000.0)
        am = _FakeAlert()
        mon = MarginMonitor(
            portfolio=pf, margin_usage_ratio_threshold=None,
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        mon.refresh()
        assert am.calls == []

    def test_未配置资金费率阈值不告警(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=0.0, mv=100000.0)
        am = _FakeAlert()
        mon = MarginMonitor(
            portfolio=pf, funding_rate=0.001, funding_rate_threshold=None,
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        mon.refresh()
        assert am.calls == []


# ─────────────────────────────────────────────
#  资金费率告警
# ─────────────────────────────────────────────
class TestFundingRateAlert:
    """费率超阈 → 告警（级别按严重度）。"""

    def test_费率超阈触发WARNING(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=0.0, mv=100000.0)
        am = _FakeAlert()
        mon = MarginMonitor(
            portfolio=pf, funding_rate=0.001, funding_rate_threshold=0.0005,
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        mon.refresh()
        fr = [c for c in am.calls if c["deduplication_key"] == DEDUP_FUNDING]
        assert len(fr) == 1
        assert fr[0]["level"] == SeverityLevel.WARNING


# ─────────────────────────────────────────────
#  指标可见
# ─────────────────────────────────────────────
class TestMetrics:
    """set_gauge 后 collector 可查；注册清单完整。"""

    def test_指标写入可查(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        met = _FakeMetrics()
        mon = _make_monitor(pf, _FakeAlert(), met, clock)
        mon.refresh()
        assert met.get_value("margin_usage_ratio") == pytest.approx(10.0)
        assert met.get_value("maintenance_margin") == pytest.approx(500.0)
        assert met.get_value("liquidation_distance_pct") == pytest.approx(99.5)

    def test_注册清单完整(self) -> None:
        for name in (
            "margin_usage_ratio", "maintenance_margin",
            "liquidation_distance_pct", "funding_rate",
        ):
            assert name in REGISTERED_METRICS


# ─────────────────────────────────────────────
#  防未来
# ─────────────────────────────────────────────
class TestNoFuture:
    """占用率/距离只基于已发生快照时间戳。"""

    def test_last_calc_time等于当前时钟而非未来(self) -> None:
        clock = _Clock(2000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        mon = _make_monitor(pf, _FakeAlert(), _FakeMetrics(), clock)
        st = mon.refresh()
        # 计算时刻=注入时钟（已发生），不引用未来时间
        assert st["last_calc_time"] == 2000.0

    def test_只消费当前快照不预测(self) -> None:
        # 先记录当前权益；refresh 后改变组合（未来动作），结果不得混入未来值
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
        mon = _make_monitor(pf, _FakeAlert(), _FakeMetrics(), clock)
        before = mon.refresh()
        # 此后才发生的权益恶化（未来动作）不应反映在上一次快照里
        pf.set_equity(510.0)
        assert before["equity"] == pytest.approx(100000.0)
        assert before["liquidation_distance_pct"] == pytest.approx(99.5)


# ─────────────────────────────────────────────
#  计算防御（缺失数据不抛、明确降级）
# ─────────────────────────────────────────────
class TestDefensive:
    """缺失/脏数据不抛异常，明确降级。"""

    def test_账户快照抛错降级不抛(self) -> None:
        class _BadPortfolio:
            def get_account(self):
                raise RuntimeError("boom")

            def get_market_value(self):
                return 0.0

        clock = _Clock(1000.0)
        mon = MarginMonitor(
            portfolio=_BadPortfolio(), alert_manager=_FakeAlert(), clock=clock,
        )
        st = mon.refresh()  # 不得抛
        assert st["degraded"] is True
        assert st["margin_usage_ratio"] is None

    def test_无组合数据源降级(self) -> None:
        # portfolio=None → 惰性 import 失败路径不可控，这里用 None 显式降级
        # 直接构造并强制 _portfolio=None 走降级分支
        clock = _Clock(1000.0)
        mon = MarginMonitor(
            portfolio=object(), alert_manager=_FakeAlert(), clock=clock,
        )
        mon._portfolio = None  # 模拟惰性获取失败
        # _resolve_portfolio 会尝试惰性 import；为稳定测试，预置一个抛错桩
        mon._resolve_portfolio = lambda: None  # type: ignore[assignment]
        st = mon.refresh()
        assert st["degraded"] is True

    def test_权益为零不计算占用率不抛(self) -> None:
        clock = _Clock(1000.0)
        pf = _StubPortfolio(equity=0.0, frozen=100.0, mv=0.0)
        mon = _make_monitor(pf, _FakeAlert(), _FakeMetrics(), clock)
        st = mon.refresh()
        assert st["margin_usage_ratio"] is None  # 权益 0 → 不除零
        assert am_calls_empty(mon)

    def test_负阈值拒绝(self) -> None:
        clock = _Clock(1000.0)
        with pytest.raises(ValueError):
            MarginMonitor(
                portfolio=_StubPortfolio(1, 0, 0),
                strong_flat_distance_pct=-1.0, clock=clock,
            )


def am_calls_empty(mon: MarginMonitor) -> bool:
    """辅助：to_dict 不告警。"""
    return mon.to_dict()["alert_triggered"] is False
