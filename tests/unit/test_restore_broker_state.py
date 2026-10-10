# 文件名：tests/unit/test_restore_broker_state.py
"""
test_restore_broker_state.py - 重启恢复券商订单与持仓测试（KP-P3-03 / T-13）
======================================================================
覆盖"重启不恢复券商订单与持仓，双账本漂移"修复：
  1. PaperBroker.restore_state —— 在途订单重新登记 + 冻结回写 + T+1 冻结回写（DoD）。
  2. QmtBroker.restore_state —— 同上。
  3. engine.restore_live_state 补传 broker → broker.restore_state 被调用（DoD）。
  4. engine._reconcile_restored_orders —— 快照 vs 券商订单差集 → 未领回挂单 cancel（DoD）。
  5. 超阈值 → pause + CRITICAL（live 模式）。

依赖: pytest, myquant.live.brokers.paper_broker / qmt_broker,
      myquant.live.engine, myquant.live.state_store, myquant.models.trade
"""

from __future__ import annotations

import logging

from myquant.live.brokers.paper_broker import PaperBroker
from myquant.live.brokers.qmt_broker import QmtBroker
from myquant.live.engine import create_live_engine
from myquant.live.state_store import restore_live_state
from myquant.models.trade import OrderSide, OrderStatus, OrderType


def _active_buy_dict(order_id: str = "o1", qty: float = 100.0, price: float = 10.0) -> dict:
    return {
        "order_id": order_id,
        "symbol": "000001.SZ",
        "side": OrderSide.BUY.value,
        "order_type": OrderType.MARKET.value,
        "quantity": qty,
        "price": price,
        "status": OrderStatus.SUBMITTED.value,
        "filled_qty": 0.0,
        "filled_price": 0.0,
    }


def _state_with(orders: list, t1: dict | None = None) -> dict:
    return {"orders": orders, "t1_holdings": t1 or {}}


# ═══════════════════════════════════════════════
#  1/2. broker.restore_state
# ═══════════════════════════════════════════════

class TestBrokerRestoreState:
    def test_paper恢复在途订单与冻结(self) -> None:
        """DoD：restore_state 重建在途订单、回写冻结与 T+1 冻结。"""
        broker = PaperBroker(initial_capital=1_000_000.0)
        state = _state_with(
            [_active_buy_dict("o1", qty=100, price=10.0)],
            t1={"000001.SZ": 50.0},
        )
        broker.restore_state(state)
        assert "o1" in broker._orders
        assert broker._orders["o1"].status.is_active
        assert broker.frozen_cash == 100.0 * 10.0  # 冻结回写
        assert broker._t1_holdings.get("000001.SZ") == 50.0

    def test_paper_终态订单不恢复(self) -> None:
        broker = PaperBroker(initial_capital=1_000_000.0)
        d = _active_buy_dict("o2")
        d["status"] = OrderStatus.FILLED.value  # 终态不登记
        broker.restore_state(_state_with([d]))
        assert "o2" not in broker._orders

    def test_qmt恢复在途订单与冻结(self) -> None:
        broker = QmtBroker(mode="SIM", initial_capital=1_000_000.0)
        state = _state_with(
            [_active_buy_dict("q1", qty=100, price=10.0)],
            t1={"000001.SZ": 50.0},
        )
        broker.restore_state(state)
        assert "q1" in broker._orders
        assert broker._orders["q1"].status.is_active
        assert broker.frozen_cash == 100.0 * 10.0
        assert broker._t1_holdings.get("000001.SZ") == 50.0


# ═══════════════════════════════════════════════
#  3. engine 补传 broker → restore_state 被调用
# ═══════════════════════════════════════════════

class TestEngineRestoreBroker:
    def test_restore_live_state_调用broker_restore_state(self) -> None:
        """DoD：restore_live_state 传 broker 时调用 broker.restore_state。"""
        broker = PaperBroker(initial_capital=1_000_000.0)
        calls: list = []

        def spy_restore(state):  # type: ignore[no-untyped-def]
            calls.append(state)
            broker.frozen_cash = 123.0

        broker.restore_state = spy_restore  # type: ignore[method-assign]
        restored = restore_live_state(
            snapshot=type("S", (), {
                "version": 1, "timestamp": 0.0,
                "state": {"orders": [], "t1_holdings": {}},
            })(),
            portfolio=None,
            broker=broker,
        )
        assert len(calls) == 1
        assert restored["restored_broker"] is True

    def _make_engine(self):
        config = {
            "live": {"trade_mode": "paper", "name": "t13", "initial_capital": 100000},
            "broker": {"type": "paper"},
            "execution": {}, "market_data": {}, "strategy": {},
            "position": {}, "risk_control": {},
            "state_store": {"auto_recover_on_start": False},  # T-34 显式必填
            "scheduler": {}, "lookahead_guard": {},
        }
        return create_live_engine(config)

    def test_reconcile_未领回挂单取消(self) -> None:
        """DoD：快照订单不在券商在途 → cancel（未领回挂单）。"""
        engine = self._make_engine()
        mgr = engine._broker_manager
        cancelled: list = []
        mgr.cancel_order = lambda oid: cancelled.append(oid) or True  # type: ignore[method-assign]
        snapshot_orders = [_active_buy_dict("snap1"), _active_buy_dict("snap2")]
        engine._reconcile_restored_orders(snapshot_orders)
        assert set(cancelled) == {"snap1", "snap2"}

    def test_reconcile_超阈值_pause(self, caplog) -> None:
        """DoD：live 模式未领回订单超阈值(1) → 触发 pause + CRITICAL。"""
        engine = self._make_engine()
        engine._trade_mode = "live"
        mgr = engine._broker_manager
        mgr.cancel_order = lambda oid: True  # type: ignore[method-assign]
        paused: list = []
        engine.pause = lambda reason="": paused.append(True)  # type: ignore[method-assign]
        with caplog.at_level(logging.CRITICAL, logger="live.engine"):
            engine._reconcile_restored_orders(
                [_active_buy_dict("a"), _active_buy_dict("b")]
            )
        assert paused == [True]
        assert any("超过阈值" in r.message for r in caplog.records)
