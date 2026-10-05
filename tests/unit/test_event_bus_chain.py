# 文件名：tests/unit/test_event_bus_chain.py
"""
test_event_bus_chain.py - BASE-4 事件链路 id 规范单元测试
===========================================================
覆盖：
  1. ChainEvent 四元组 schema：link_ids / missing_link_ids / to_dict 序列化
  2. 强类型交易事件类（SignalEvent/OrderEvent/PositionUpdateEvent/SystemErrorEvent）：
     构造、event_type、to_dict 带出四元组
  3. publish_chain 统一发布入口：缺 event_ts 记 warning 不阻断 /
     require_event_ts 拒绝发布 / 非链路事件抛 TypeError
  4. 链路连续性：同一 signal_id 贯穿 signal→order→fill（根因分析打底）
  5. 防未来：event_ts 必须由发布方填写（默认 None，不静默注入创建时刻）

运行方式:
  pytest tests/unit/test_event_bus_chain.py -v
"""

from __future__ import annotations

import pytest

from myquant.core.event_bus import (
    ChainEvent,
    Event,
    EventBus,
    EventType,
    OrderEvent,
    PositionUpdateEvent,
    SignalEvent,
    SystemErrorEvent,
)


# ═══════════════════════════════════════════
#  1. ChainEvent 四元组 schema
# ═══════════════════════════════════════════

class TestChainEventSchema:
    def test_default_link_ids_empty(self) -> None:
        ev = ChainEvent(event_type="test.chain")
        assert ev.signal_id == ""
        assert ev.order_id == ""
        assert ev.trade_id == ""
        assert ev.event_ts is None  # 防未来：不得自动注入创建时刻

    def test_missing_link_ids_reports_all_four(self) -> None:
        ev = ChainEvent(event_type="test.chain")
        assert ev.missing_link_ids() == ["signal_id", "order_id", "trade_id", "event_ts"]

    def test_missing_link_ids_partial(self) -> None:
        ev = ChainEvent(
            event_type="test.chain",
            signal_id="s1",
            order_id="o1",
            event_ts=100.0,  # trade_id 仍缺
        )
        assert ev.missing_link_ids() == ["trade_id"]

    def test_link_ids_and_to_dict(self) -> None:
        ev = ChainEvent(
            event_type="test.chain",
            signal_id="s1",
            order_id="o1",
            trade_id="t1",
            event_ts=100.0,
        )
        assert ev.link_ids() == {
            "signal_id": "s1",
            "order_id": "o1",
            "trade_id": "t1",
            "event_ts": 100.0,
        }
        d = ev.to_dict()
        assert d["signal_id"] == "s1"
        assert d["order_id"] == "o1"
        assert d["trade_id"] == "t1"
        assert d["event_ts"] == 100.0
        assert d["event_type"] == "test.chain"
        assert d["event_id"]  # 基类字段保留


# ═══════════════════════════════════════════
#  2. 强类型交易事件类
# ═══════════════════════════════════════════

class TestTradeEventClasses:
    def test_signal_event(self) -> None:
        ev = SignalEvent(
            signal_id="ma_cross:000001.SZ:2026-10-05:cross_up",
            strategy_id="ma_cross",
            symbol="000001.SZ",
            signal_type="cross_up",
            event_ts=100.0,
        )
        assert ev.event_type == EventType.SIGNAL_GENERATED
        d = ev.to_dict()
        assert d["strategy_id"] == "ma_cross"
        assert d["symbol"] == "000001.SZ"
        assert d["signal_type"] == "cross_up"
        assert d["signal_id"].startswith("ma_cross:000001.SZ")

    def test_order_event_all_five_types(self) -> None:
        """ORDER_SUBMIT/FILLED/PARTIAL/CANCELLED/REJECTED 均可用 OrderEvent"""
        for et, status in [
            (EventType.ORDER_SUBMIT, "submitted"),
            (EventType.ORDER_FILLED, "filled"),
            (EventType.ORDER_PARTIAL, "partial"),
            (EventType.ORDER_CANCELLED, "cancelled"),
            (EventType.ORDER_REJECTED, "rejected"),
        ]:
            ev = OrderEvent(
                event_type=et,
                symbol="000001.SZ",
                status=status,
                order_id=f"o_{status}",
                event_ts=100.0,
            )
            assert ev.event_type == et
            assert ev.to_dict()["order_id"] == f"o_{status}"
            assert ev.to_dict()["status"] == status

    def test_order_rejected_reason(self) -> None:
        ev = OrderEvent(
            event_type=EventType.ORDER_REJECTED,
            symbol="000001.SZ",
            status="rejected",
            reject_reason="risk_limit_hit",
            order_id="o_rej",
            event_ts=100.0,
        )
        assert ev.to_dict()["reject_reason"] == "risk_limit_hit"

    def test_position_update_event(self) -> None:
        ev = PositionUpdateEvent(
            symbol="000001.SZ",
            side="buy",
            quantity=100.0,
            avg_price=10.5,
            event_ts=100.0,
        )
        assert ev.event_type == EventType.POSITION_UPDATE
        d = ev.to_dict()
        assert d["side"] == "buy"
        assert d["quantity"] == 100.0
        assert d["avg_price"] == 10.5

    def test_system_error_event(self) -> None:
        ev = SystemErrorEvent(error="broker disconnected", component="broker", event_ts=100.0)
        assert ev.event_type == EventType.SYSTEM_ERROR
        d = ev.to_dict()
        assert d["error"] == "broker disconnected"
        assert d["component"] == "broker"
        assert "signal_id" in d  # 系统错误也携带四元组（A-3 8 类全带）


# ═══════════════════════════════════════════
#  3. publish_chain 统一发布入口
# ═══════════════════════════════════════════

class TestPublishChain:
    def test_publish_chain_delivers_to_subscribers(self) -> None:
        bus = EventBus()
        recv: list = []
        bus.subscribe(EventType.ORDER_FILLED, lambda e: recv.append(e))
        ev = OrderEvent(
            event_type=EventType.ORDER_FILLED,
            signal_id="s1",
            order_id="o1",
            trade_id="t1",
            event_ts=100.0,
            symbol="000001.SZ",
            status="filled",
        )
        assert bus.publish_chain(ev) == 1
        assert recv and recv[0].signal_id == "s1"

    def test_missing_event_ts_warns_but_publishes(self, caplog) -> None:
        """缺 event_ts：记 warning 仍发布（A-3 过渡期不阻断）"""
        bus = EventBus()
        recv: list = []
        bus.subscribe("*", lambda e: recv.append(e))
        ev = SignalEvent(signal_id="s1")  # 未填 event_ts
        with caplog.at_level("WARNING", logger="myquant.event_bus"):
            assert bus.publish_chain(ev) == 1
        assert any("缺 event_ts" in r.message for r in caplog.records)

    def test_require_event_ts_rejects(self, caplog) -> None:
        bus = EventBus()
        ev = SignalEvent(signal_id="s1")  # 未填 event_ts
        with caplog.at_level("ERROR", logger="myquant.event_bus"):
            assert bus.publish_chain(ev, require_event_ts=True) == 0
        assert any("发布拒绝" in r.message for r in caplog.records)

    def test_non_chain_event_raises(self) -> None:
        bus = EventBus()
        with pytest.raises(TypeError):
            bus.publish_chain(Event(EventType.TICK))  # type: ignore[arg-type]

    def test_wildcard_subscriber_receives_chain(self) -> None:
        bus = EventBus()
        recv: list = []
        bus.subscribe("*", lambda e: recv.append(e))
        bus.publish_chain(PositionUpdateEvent(symbol="000001.SZ", event_ts=100.0))
        assert len(recv) == 1
        assert isinstance(recv[0], ChainEvent)


# ═══════════════════════════════════════════
#  4. 链路连续性（滑点/延迟/根因分析打底）
# ═══════════════════════════════════════════

class TestChainContinuity:
    def test_signal_to_order_to_fill_same_signal_id(self) -> None:
        """同一信号 signal→order→fill 全链路 signal_id 贯穿"""
        signal_id = "ma_cross:000001.SZ:2026-10-05:cross_up"
        sig = SignalEvent(
            signal_id=signal_id, strategy_id="ma_cross",
            symbol="000001.SZ", signal_type="cross_up", event_ts=100.0,
        )
        submit = OrderEvent(
            event_type=EventType.ORDER_SUBMIT, signal_id=signal_id,
            order_id=signal_id, symbol="000001.SZ", status="submitted", event_ts=101.0,
        )
        filled = OrderEvent(
            event_type=EventType.ORDER_FILLED, signal_id=signal_id,
            order_id=signal_id, trade_id="T_1001",
            symbol="000001.SZ", status="filled", event_ts=103.0,
        )
        assert submit.signal_id == sig.signal_id == filled.signal_id
        assert filled.order_id == sig.signal_id  # Signal.to_order 幂等派生
        # 时间单调递增（延迟统计口径：event_ts 是业务发生时刻）
        assert sig.event_ts < submit.event_ts < filled.event_ts

    def test_delay_measurable_via_event_ts(self) -> None:
        """信号→下单延迟可计算：event_ts 差值（B-4 滑点/延迟采集打底）"""
        sig = SignalEvent(signal_id="s1", event_ts=100.0)
        submit = OrderEvent(
            event_type=EventType.ORDER_SUBMIT, signal_id="s1",
            order_id="o1", event_ts=120.0,
        )
        assert submit.event_ts - sig.event_ts == 20.0  # 下单延迟 20ms（口径示例）
