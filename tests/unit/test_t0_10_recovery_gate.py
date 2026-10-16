# tests/unit/test_t0_10_recovery_gate.py
"""
test_t0_10_recovery_gate.py - T0-10 崩溃恢复撤单专项测试
=================================================================
验收 FI-18：
  1. BrokerManager.restore_state 转发子券商（duck-typing）
  2. cancel_order 返回 False（券商拒绝）→ 不计入 reconciled_orders + CRITICAL
  3. cancel_order 抛异常 → 不计入 reconciled_orders + CRITICAL
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from myquant.live.brokers.base_broker import BaseBroker, BrokerType
from myquant.live.brokers.manager import BrokerManager


class _RecordingBroker(BaseBroker):
    """鸭子类型券商：记录 restore_state/cancel_order 调用。"""

    broker_name = "rec_broker"
    broker_type = BrokerType.CUSTOM

    def __init__(self, alias: str) -> None:
        super().__init__()
        self._alias = alias
        self.restore_called: bool = False
        self.restored_state: Optional[Dict[str, Any]] = None
        self.cancel_results: Dict[str, Any] = {}

    @property
    def alias(self) -> str:
        return self._alias

    def restore_state(self, state: Dict[str, Any]) -> None:
        self.restore_called = True
        self.restored_state = state

    def cancel_order(self, order_id: str) -> Any:
        r = self.cancel_results.get(order_id, True)
        if isinstance(r, Exception):
            raise r
        return r

    # BaseBroker 抽象方法桩（测试不需要真实实现）
    def connect(self): return True
    def disconnect(self): pass
    def get_account(self): return {"cash": 0.0}
    def get_positions(self): return []
    def get_orders(self): return []
    def submit_order(self, order): return order
    def _submit_order_impl(self, order): return order
    def get_active_orders(self): return []
    def get_order(self, order_id): return None


class TestRestoreStateForwarding:
    """T0-10 子项1：BrokerManager.restore_state 必须转发子券商。"""

    def test_restore_state_forwards_to_sub_brokers(self):
        mgr = BrokerManager()
        b1 = _RecordingBroker("b1")
        b2 = _RecordingBroker("b2")
        mgr.register_broker(b1, alias="b1", role="secondary")
        mgr.register_broker(b2, alias="b2", role="primary")

        state = {"primary_alias": "b2", "orders": [{"order_id": "X1"}]}
        mgr.restore_state(state)

        assert b1.restore_called is True
        assert b1.restored_state == state
        assert b2.restore_called is True
        assert b2.restored_state == state

    def test_restore_state_sub_broker_failure_does_not_block_others(self):
        mgr = BrokerManager()
        b1 = _RecordingBroker("b1")

        class _BrokenBroker(_RecordingBroker):
            def restore_state(self, state):
                raise RuntimeError("boom")

        b2 = _BrokenBroker("b2")
        mgr.register_broker(b1, alias="b1", role="secondary")
        mgr.register_broker(b2, alias="b2", role="primary")

        state = {"primary_alias": "b2"}
        mgr.restore_state(state)
        assert b1.restore_called is True


class TestCancelOrderReturnValue:
    """T0-10 子项3：cancel_order 返回 False/异常 → 不计入成功。"""

    def test_cancel_false_not_counted_as_reconciled(self):
        """cancel_order 返回 False → 该单不计入 reconciled_orders。"""
        from myquant.live.state_store import restore_live_state, StateSnapshot

        broker = _RecordingBroker("test")
        broker.cancel_results = {"O1": True, "O2": False, "O3": RuntimeError("net")}
        broker.get_orders = lambda: []  # type: ignore[assignment]

        snapshot = StateSnapshot(
            version=1, timestamp=0.0,
            state={"orders": [{"order_id": "O1"}, {"order_id": "O2"}, {"order_id": "O3"}]},
        )
        result = restore_live_state(snapshot, portfolio=None, broker=broker, strict=False)
        assert result["reconciled_orders"] == 1, (
            f"T0-10 失败: 只有1笔成功, 实际 {result['reconciled_orders']}"
        )

    def test_cancel_false_logged_in_notes(self):
        """cancel 失败的订单出现在 notes 里（需人工核查）。"""
        from myquant.live.state_store import restore_live_state, StateSnapshot

        broker = _RecordingBroker("test")
        broker.cancel_results = {"O1": False}
        broker.get_orders = lambda: []  # type: ignore[assignment]

        snapshot = StateSnapshot(version=1, timestamp=0.0, state={"orders": [{"order_id": "O1"}]})
        result = restore_live_state(snapshot, portfolio=None, broker=broker, strict=False)
        assert result["reconciled_orders"] == 0
        notes_str = " ".join(result["notes"])
        assert "cancel 失败" in notes_str or "需人工核查" in notes_str
