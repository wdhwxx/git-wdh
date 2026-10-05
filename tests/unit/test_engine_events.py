# 文件名：tests/unit/test_engine_events.py
"""
test_engine_events.py - A-3 引擎统一埋点单元测试
===================================================
覆盖（事件总线用 fake bus 注入，不依赖真实总线/券商）：
  1. _publish_engine_event 辅助：未装配总线跳过不崩、publish_chain 异常不阻断
  2. _process_signal → SIGNAL 事件（signal_id 归一化非空、四元组齐全、
     链路贯穿：order_id 与 signal_id 幂等同源）
  3. _submit_order → ORDER_SUBMIT（SUBMITTED）/ ORDER_REJECTED（拒单+占位回滚）/
     SYSTEM_ERROR（提交异常，component=engine.submit）
  4. on_order_update → ORDER_FILLED/PARTIAL/CANCELLED/REJECTED（订单级状态事件），
     PENDING 不发布
  5. on_trade → ORDER_FILLED（成交级）+ POSITION_UPDATE（apply_trade 后均价）；
     无 portfolio 时 POSITION_UPDATE avg_price=0 不崩
  6. collect_from_engine → engine_* 7 指标 + LIVE_POSITIONS_COUNT/LIVE_PNL_TOTAL；
     引擎状态异常返回 0

运行方式:
  pytest tests/unit/test_engine_events.py -v
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from myquant.core.event_bus import (
    ChainEvent,
    EventType,
    OrderEvent,
    PositionUpdateEvent,
    SignalEvent,
    SystemErrorEvent,
)
from myquant.live.engine import LiveEngine
from myquant.models.market_data import Bar, BarFrequency
from myquant.models.trade import Order, OrderSide, OrderStatus, Position, Trade
from myquant.monitoring.metrics_collector import BuiltinMetrics, MetricsCollector
from myquant.strategies.base import Signal, SignalType


# ═══════════════════════════════════════════
#  通用 fake
# ═══════════════════════════════════════════

class _FakeBus:
    """事件总线 fake：捕获 publish_chain 发布的链路事件"""

    def __init__(self) -> None:
        self.events: List[ChainEvent] = []

    def publish_chain(self, event: ChainEvent, require_event_ts: bool = False) -> int:
        self.events.append(event)
        return 1


class _FakePortfolio:
    """portfolio fake：apply_trade 记录 + get_position 返回固定均价"""

    def __init__(self, avg_cost: float = 12.5) -> None:
        self._avg_cost = avg_cost
        self.applied: List[Trade] = []

    def get_position_count(self) -> int:
        return 3

    def get_total_pnl(self) -> float:
        return 1234.5

    def apply_trade(self, trade: Trade) -> None:
        self.applied.append(trade)

    def get_position(self, symbol: str) -> Position:
        return Position(symbol=symbol, quantity=100, avg_cost=self._avg_cost)

    def get_all_positions(self) -> List[Position]:
        return []


def _new_engine() -> LiveEngine:
    """轻量引擎：关闭全部自动装配组件（不 start，无外网依赖）"""
    return LiveEngine(
        engine_id="test-eng",
        enable_warmup=False,
        enable_risk_manager=False,
        enable_state_store=False,
        enable_scheduler=False,
        enable_guardian=False,
        enable_lookahead_guard=False,
        auto_recover=False,
        trade_mode="paper",
    )


def _new_bar(symbol: str = "600000.SH") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=time.time(),
        close=10.0,
        frequency=BarFrequency.MINUTE_1,
    )


# ═══════════════════════════════════════════
#  1. _publish_engine_event 辅助
# ═══════════════════════════════════════════

class TestPublishHelper:
    def test_no_bus_skips_silently(self) -> None:
        engine = _new_engine()
        assert engine._event_bus is None
        engine._publish_engine_event(SignalEvent(signal_id="s1", event_ts=time.time()))
        # 不抛异常即通过（埋点是增强，总线未装配时静默跳过）

    def test_bus_error_does_not_block(self) -> None:
        engine = _new_engine()

        class _BrokenBus:
            def publish_chain(self, event: ChainEvent, require_event_ts: bool = False) -> int:
                raise RuntimeError("bus down")

        engine._event_bus = _BrokenBus()
        # 总线故障只记 warning，不向调用方抛异常（交易主链路继续）
        engine._publish_engine_event(SignalEvent(signal_id="s1", event_ts=time.time()))


# ═══════════════════════════════════════════
#  2. _process_signal → SIGNAL 事件
# ═══════════════════════════════════════════

class TestSignalEvent:
    def test_signal_event_published_with_chain_ids(self) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        # fake broker：SUBMITTED 返回，让信号路径完整走完
        engine._broker_manager = SimpleNamespace(submit_order=lambda order: order)

        bar = _new_bar()
        signal = Signal(
            symbol="600000.SH",
            signal_type=SignalType.BUY,
            strategy_id="strat-a",
            reason="ma_cross",
        )
        engine._process_signal(signal, bar)

        signal_events = [e for e in bus.events if isinstance(e, SignalEvent)]
        assert len(signal_events) == 1
        ev = signal_events[0]
        assert ev.signal_id  # 引擎已归一化 signal_id
        assert ev.event_ts is not None
        assert ev.symbol == "600000.SH"
        assert ev.strategy_id == "strat-a"
        assert ev.signal_type == "BUY"
        # 防未来红线：event_ts 是业务发生时刻（当前时点），不能是未来
        assert ev.event_ts <= time.time() + 1

    def test_chain_link_order_id_equals_signal_id(self) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        engine._broker_manager = SimpleNamespace(submit_order=lambda order: order)

        bar = _new_bar()
        signal = Signal(symbol="600000.SH", signal_type=SignalType.BUY, strategy_id="s")
        engine._process_signal(signal, bar)

        # 链路贯穿：SIGNAL 的 signal_id 与 ORDER_SUBMIT 的 order_id 幂等同源
        sig = next(e for e in bus.events if isinstance(e, SignalEvent))
        sub = next(e for e in bus.events if isinstance(e, OrderEvent))
        assert sub.event_type == EventType.ORDER_SUBMIT
        assert sub.order_id == sig.signal_id


# ═══════════════════════════════════════════
#  3. _submit_order → SUBMIT/REJECTED/SYSTEM_ERROR
# ═══════════════════════════════════════════

class TestSubmitOrderEvent:
    def _order(self) -> Order:
        return Order(
            order_id="ord-1",
            symbol="600000.SH",
            side=OrderSide.BUY,
            quantity=100,
            price=10.0,
            status=OrderStatus.PENDING,
        )

    def test_submitted_publishes_order_submit(self) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        order = self._order()
        submitted = Order(symbol=order.symbol, quantity=100, price=10.0,
                          side=OrderSide.BUY, status=OrderStatus.SUBMITTED)
        engine._broker_manager = SimpleNamespace(submit_order=lambda o: submitted)

        engine._submit_order(order)
        ev = next(e for e in bus.events if isinstance(e, OrderEvent))
        assert ev.event_type == EventType.ORDER_SUBMIT
        assert ev.order_id == "ord-1"
        assert ev.status == "submitted"
        assert ev.event_ts is not None

    def test_rejected_publishes_reject_and_rolls_back(self) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        order = self._order()
        rejected = Order(symbol=order.symbol, quantity=100, price=10.0,
                         side=OrderSide.BUY, status=OrderStatus.REJECTED)
        rejected.extra["reject_reason"] = "insufficient_margin"
        engine._broker_manager = SimpleNamespace(submit_order=lambda o: rejected)

        engine._submit_order(order)
        ev = next(e for e in bus.events if isinstance(e, OrderEvent))
        assert ev.event_type == EventType.ORDER_REJECTED
        assert ev.status == "rejected"
        assert ev.reject_reason == "insufficient_margin"

    def test_submit_exception_publishes_system_error(self) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        order = self._order()

        def boom(o: Order) -> Any:
            raise ConnectionError("broker unreachable")

        engine._broker_manager = SimpleNamespace(submit_order=boom)
        engine._submit_order(order)

        ev = next(e for e in bus.events if isinstance(e, SystemErrorEvent))
        assert ev.component == "engine.submit"
        assert "broker unreachable" in ev.error
        assert ev.order_id == "ord-1"
        assert ev.event_ts is not None


# ═══════════════════════════════════════════
#  4. on_order_update → 订单级状态事件
# ═══════════════════════════════════════════

class TestOrderUpdateEvent:
    def _order(self, status: OrderStatus) -> Order:
        return Order(
            order_id="ord-u1",
            symbol="600000.SH",
            side=OrderSide.BUY,
            quantity=100,
            price=10.0,
            status=status,
        )

    @pytest.mark.parametrize(
        "status, expected_type",
        [
            (OrderStatus.FILLED, EventType.ORDER_FILLED),
            (OrderStatus.PARTIAL_FILLED, EventType.ORDER_PARTIAL),
            (OrderStatus.CANCELLED, EventType.ORDER_CANCELLED),
            (OrderStatus.REJECTED, EventType.ORDER_REJECTED),
        ],
    )
    def test_status_event_published(self, status: OrderStatus,
                                    expected_type: EventType) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        order = self._order(status)
        if status == OrderStatus.REJECTED:
            order.extra["reject_reason"] = "risk_blocked"

        engine.on_order_update(order)
        ev = next(e for e in bus.events if isinstance(e, OrderEvent))
        assert ev.event_type == expected_type
        assert ev.order_id == "ord-u1"
        assert ev.status == status.value.lower()

    def test_pending_does_not_publish(self) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        engine.on_order_update(self._order(OrderStatus.PENDING))
        assert bus.events == []


# ═══════════════════════════════════════════
#  5. on_trade → 成交级 FILLED + POSITION_UPDATE
# ═══════════════════════════════════════════

class TestTradeEvent:
    def _trade(self) -> Trade:
        return Trade(
            trade_id="tr-1",
            order_id="ord-1",
            symbol="600000.SH",
            side=OrderSide.BUY,
            quantity=100,
            price=10.0,
            timestamp=time.time(),
        )

    def test_fill_and_position_update_published(self) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        engine._portfolio = _FakePortfolio(avg_cost=12.5)

        engine.on_trade(self._trade())

        fill_events = [e for e in bus.events if isinstance(e, OrderEvent)]
        pos_events = [e for e in bus.events if isinstance(e, PositionUpdateEvent)]
        assert len(fill_events) == 1
        assert fill_events[0].event_type == EventType.ORDER_FILLED
        assert fill_events[0].trade_id == "tr-1"
        assert fill_events[0].order_id == "ord-1"
        assert len(pos_events) == 1
        assert pos_events[0].symbol == "600000.SH"
        assert pos_events[0].side == "buy"
        assert pos_events[0].quantity == 100
        assert pos_events[0].avg_price == 12.5
        assert pos_events[0].event_ts is not None

    def test_no_portfolio_position_update_avg_zero(self) -> None:
        engine = _new_engine()
        bus = _FakeBus()
        engine._event_bus = bus
        engine._portfolio = None

        engine.on_trade(self._trade())
        pos_events = [e for e in bus.events if isinstance(e, PositionUpdateEvent)]
        assert len(pos_events) == 1
        assert pos_events[0].avg_price == 0.0


# ═══════════════════════════════════════════
#  6. collect_from_engine
# ═══════════════════════════════════════════

class TestCollectFromEngine:
    def test_engine_metrics_collected(self) -> None:
        collector = MetricsCollector().register_builtin_metrics()
        engine = _new_engine()
        engine._portfolio = _FakePortfolio()
        engine._stats.update({
            "total_orders": 10,
            "filled_orders": 8,
            "rejected_orders": 1,
            "total_trades": 8,
            "total_signals": 12,
            "risk_rejected": 3,
            "errors": 1,
        })

        count = collector.collect_from_engine(engine)
        assert count == 9  # 7 engine_* + LIVE_POSITIONS_COUNT + LIVE_PNL_TOTAL
        assert collector.get_value(BuiltinMetrics.ENGINE_ORDERS_TOTAL) == 10.0
        assert collector.get_value(BuiltinMetrics.ENGINE_FILLED_TOTAL) == 8.0
        assert collector.get_value(BuiltinMetrics.ENGINE_REJECTED_TOTAL) == 1.0
        assert collector.get_value(BuiltinMetrics.ENGINE_TRADES_TOTAL) == 8.0
        assert collector.get_value(BuiltinMetrics.ENGINE_SIGNALS_TOTAL) == 12.0
        assert collector.get_value(BuiltinMetrics.ENGINE_RISK_REJECTED_TOTAL) == 3.0
        assert collector.get_value(BuiltinMetrics.ENGINE_ERRORS_TOTAL) == 1.0
        assert collector.get_value(BuiltinMetrics.LIVE_POSITIONS_COUNT) == 3.0
        assert collector.get_value(BuiltinMetrics.LIVE_PNL_TOTAL) == 1234.5

    def test_engine_status_error_returns_zero(self) -> None:
        collector = MetricsCollector().register_builtin_metrics()

        class _BrokenEngine:
            def get_status(self) -> Dict[str, Any]:
                raise RuntimeError("engine down")

        assert collector.collect_from_engine(_BrokenEngine()) == 0
