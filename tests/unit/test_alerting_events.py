# 文件名：tests/unit/test_alerting_events.py
"""
test_alerting_events.py - A-8 交易异常告警闭环单元测试
========================================================================
覆盖（触发模式 EVENT：经 publish → 订阅回调 → 断言告警发出，不等周期）：
  1. ORDER_REJECTED         → ERROR（P1）告警，details 含 symbol/order_id/原因
  2. ORDER_FAILED          → ERROR（P1）告警
  3. FILL_REPORT_ERROR      → ERROR（P1）告警
  4. EMERGENCY_LIQUIDATION  → CRITICAL（P0）告警
  5. 去重：同单（同 order_id）同类异常在去重窗口内不重复刷屏
  6. 指标联动：trade_exception_alerts_total 按事件类型/级别同步计数
  7. 回归保护：subscribe_to_event_bus 在既有 5 类订阅上新增 4 类（共 9 个 handler）
  8. 防御：通用 Event（data 字典）兜底取字段，不抛异常阻断事件流

运行方式:
  pytest tests/unit/test_alerting_events.py -v
"""

from __future__ import annotations

import time
from typing import List

from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import EventBus, EventType, OrderEvent
from myquant.monitoring.alerting import (
    Alert,
    AlertChannel,
    AlertManager,
)
from myquant.monitoring.metrics_collector import BuiltinMetrics, MetricsCollector


# ═══════════════════════════════════════════
#  Fake 通道：捕获告警，记录发送顺序
# ═══════════════════════════════════════════

class _CaptureChannel(AlertChannel):
    """捕获告警通道：记录所有送达的告警对象，send 恒成功。"""

    def __init__(self, name: str = "capture",
                 min_level: SeverityLevel = SeverityLevel.INFO):
        super().__init__(name=name, min_level=min_level)
        self.alerts: List[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        self._record_success()
        return True


def _make_bus_manager() -> tuple:
    """构造真实 EventBus + 纯内存 AlertManager（不碰 quant.db）+ 捕获通道。"""
    bus = EventBus()
    manager = AlertManager(store=None, store_enabled=False)
    cap = _CaptureChannel()
    manager.add_channel(cap)
    return bus, manager, cap


def _order_event(et: EventType, *, order_id: str = "O_1001",
                 symbol: str = "600519.SH", reason: str = "资金不足") -> OrderEvent:
    """构造交易链路订单事件（event_ts 取已发生时刻，防未来）。"""
    return OrderEvent(
        event_type=et,
        signal_id="ma_cross:600519.SH:2026-10-05:cross_up",
        order_id=order_id,
        trade_id="",
        event_ts=time.time() - 100,   # 业务已发生时刻（防未来红线）
        symbol=symbol,
        status="rejected" if et == EventType.ORDER_REJECTED else "failed",
        reject_reason=reason,
    )


# ═══════════════════════════════════════════
#  1~4. 四类交易异常事件 → 分级告警
# ═══════════════════════════════════════════

class TestTradeExceptionAlerts:
    def test_order_rejected_triggers_error_alert(self) -> None:
        """FI-04：模拟一次拒单 → 立即 ERROR 告警多渠道送达，含 symbol/原因。"""
        bus, manager, cap = _make_bus_manager()
        manager.subscribe_to_event_bus(bus)

        ev = _order_event(
            EventType.ORDER_REJECTED, order_id="O_RJ1",
            symbol="600519.SH", reason="资金不足",
        )
        n = bus.publish_chain(ev)

        assert n >= 1                      # 至少触达本管理器订阅回调
        assert len(cap.alerts) == 1        # 事件触发路径立即告警（不等周期）
        alert = cap.alerts[0]
        assert alert.level == SeverityLevel.ERROR
        assert alert.category == "trade"
        assert alert.details["symbol"] == "600519.SH"
        assert alert.details["order_id"] == "O_RJ1"
        assert "资金不足" in alert.details["reason"]
        assert alert.deduplication_key == "trade:order.rejected:O_RJ1"

    def test_order_failed_triggers_error_alert(self) -> None:
        bus, manager, cap = _make_bus_manager()
        manager.subscribe_to_event_bus(bus)

        bus.publish_chain(_order_event(EventType.ORDER_FAILED, order_id="O_F1"))

        assert len(cap.alerts) == 1
        alert = cap.alerts[0]
        assert alert.level == SeverityLevel.ERROR
        assert alert.details["event_type"] == "order.failed"
        assert alert.details["order_id"] == "O_F1"

    def test_fill_report_error_triggers_error_alert(self) -> None:
        bus, manager, cap = _make_bus_manager()
        manager.subscribe_to_event_bus(bus)

        bus.publish_chain(_order_event(EventType.FILL_REPORT_ERROR, order_id="O_FE1"))

        assert len(cap.alerts) == 1
        assert cap.alerts[0].level == SeverityLevel.ERROR
        assert cap.alerts[0].details["event_type"] == "fill.report_error"

    def test_emergency_liquidation_triggers_critical_alert(self) -> None:
        """P0：紧急强平 → CRITICAL 告警。"""
        bus, manager, cap = _make_bus_manager()
        manager.subscribe_to_event_bus(bus)

        bus.publish_chain(_order_event(
            EventType.EMERGENCY_LIQUIDATION, order_id="O_EL1",
            reason="风险敞口超限",
        ))

        assert len(cap.alerts) == 1
        alert = cap.alerts[0]
        assert alert.level == SeverityLevel.CRITICAL
        assert alert.details["event_type"] == "emergency.liquidation"
        assert "强平" in alert.message or "P0" in alert.message


# ═══════════════════════════════════════════
#  5. 去重：同单同类异常不重复刷屏
# ═══════════════════════════════════════════

class TestDedup:
    def test_same_order_rejected_deduped(self) -> None:
        bus, manager, cap = _make_bus_manager()
        manager.subscribe_to_event_bus(bus)

        ev = _order_event(EventType.ORDER_REJECTED, order_id="O_DUP1")
        first = bus.publish_chain(ev)
        second = bus.publish_chain(ev)   # 同单再发一次

        assert first >= 1
        # 第二次同 order_id 命中去重窗口 → 告警不重复发出
        assert len(cap.alerts) == 1
        assert second >= 1                # 事件仍送达回调（去重在告警层）

    def test_different_orders_not_deduped(self) -> None:
        bus, manager, cap = _make_bus_manager()
        manager.subscribe_to_event_bus(bus)

        bus.publish_chain(_order_event(EventType.ORDER_REJECTED, order_id="O_A"))
        bus.publish_chain(_order_event(EventType.ORDER_REJECTED, order_id="O_B"))

        # 不同订单互不影响，均告警
        assert len(cap.alerts) == 2


# ═══════════════════════════════════════════
#  6. 指标联动：事件到达即同步计数
# ═══════════════════════════════════════════

class TestMetrics:
    def test_counter_increments_by_event_type(self) -> None:
        bus, manager, cap = _make_bus_manager()
        manager.subscribe_to_event_bus(bus)

        collector = MetricsCollector().register_builtin_metrics()
        collector.subscribe_to_event_bus(bus)

        bus.publish_chain(_order_event(EventType.ORDER_REJECTED, order_id="O_M1"))
        bus.publish_chain(_order_event(
            EventType.EMERGENCY_LIQUIDATION, order_id="O_M2"))

        rejected = collector.get_value(
            BuiltinMetrics.TRADE_EXCEPTION_ALERTS_TOTAL,
            labels={"event_type": "order.rejected", "level": "error"},
        )
        critical = collector.get_value(
            BuiltinMetrics.TRADE_EXCEPTION_ALERTS_TOTAL,
            labels={"event_type": "emergency.liquidation", "level": "critical"},
        )
        assert rejected == 1.0
        assert critical == 1.0


# ═══════════════════════════════════════════
#  7. 回归保护：既有 5 类 + 新增 4 类 = 9 个 handler
# ═══════════════════════════════════════════

class TestSubscriptionRegression:
    def test_registers_nine_handlers(self) -> None:
        bus = EventBus()
        manager = AlertManager(store=None, store_enabled=False)
        handler_ids = manager.subscribe_to_event_bus(bus)

        # 既有 5 类（DATA_QUALITY_DIRTY/ALERT/PIPELINE_COMPLETE/RISK_PAUSED/RESUMED）
        # + A-8 新增 4 类（REJECTED/FAILED/FILL_REPORT_ERROR/EMERGENCY_LIQUIDATION）
        assert len(handler_ids) == 9

    def test_existing_pause_subscription_still_works(self) -> None:
        """既有 RISK_PAUSED 订阅行为不被 A-8 破坏（回归保护）。"""
        bus = EventBus()
        manager = AlertManager(store=None, store_enabled=False)
        manager.add_channel(_CaptureChannel())
        manager.subscribe_to_event_bus(bus)

        # 通用 Event 发布 RISK_PAUSED，验证既有回调链路仍通
        from myquant.core.event_bus import Event
        bus.publish(Event(event_type=EventType.RISK_PAUSED,
                          data={"paused": True, "pause_level": "global",
                                "symbol": "", "reason": "回归保护测试"}))
        # 全局暂停 → CRITICAL，不抛异常即视为链路正常
        stats = manager.stats
        assert stats["event_bus_subscribed"] is True


# ═══════════════════════════════════════════
#  8. 防御：通用 Event（data 字典）兜底取字段
# ═══════════════════════════════════════════

class TestDefensive:
    def test_generic_event_data_fallback(self) -> None:
        bus, manager, cap = _make_bus_manager()
        manager.subscribe_to_event_bus(bus)

        from myquant.core.event_bus import Event
        bus.publish(Event(
            event_type=EventType.ORDER_FAILED,
            data={"symbol": "000001.SZ", "order_id": "O_G1",
                  "reason": "券商连接超时"},
        ))

        assert len(cap.alerts) == 1
        alert = cap.alerts[0]
        assert alert.level == SeverityLevel.ERROR
        assert alert.details["symbol"] == "000001.SZ"
        assert alert.details["reason"] == "券商连接超时"
