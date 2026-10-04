# 文件名：tests/unit/test_emergency_dryrun.py
"""
test_emergency_dryrun.py - 熔断强平 dry-run 假成功测试（KP-M02 / T-18）
========================================================================
覆盖"熔断强平 dry-run 恒 ok=True 假成功"修复：
  1. dry-run 不标 executed（仅计划，不真实执行）。
  2. dry-run 计划项计入 planned，不冒充 success。
  3. 真实执行（confirm=True）标 executed 且 success 计数。
  4. guardian 无回调时 force_close_all / force_close_symbol 均返回 False（不假成功）。
  5. engine._on_force_close 消费 executed，dry-run 时显式 CRITICAL。

依赖: pytest, myquant.live.emergency_liquidation, myquant.risk.guardian,
      myquant.live.engine
"""

from __future__ import annotations

from myquant.live.emergency_liquidation import EmergencyLiquidationService
from myquant.live.engine import LiveEngine
from myquant.models.trade import Order, OrderStatus
from myquant.risk.guardian import RiskGuardian


class _FakeOrder:
    def __init__(self, order_id: str, symbol: str) -> None:
        self.order_id = order_id
        self.symbol = symbol
        self.status = OrderStatus.SUBMITTED


class _FakePos:
    def __init__(self, symbol: str, quantity: float, market_price: float) -> None:
        self.symbol = symbol
        self.quantity = quantity
        self.market_price = market_price
        self.extra = {}


class _FakeBroker:
    """最小券商桩：2 持仓 + 1 挂单。"""

    def __init__(self) -> None:
        self.is_connected = True
        self._cancelled: list[str] = []
        self._submitted: list[str] = []

    def connect(self) -> bool:
        return True

    def get_active_orders(self, symbol=None):
        return [_FakeOrder("o_1", "000001.SZ")]

    def cancel_order(self, order_id: str) -> bool:
        self._cancelled.append(order_id)
        return True

    def get_positions(self):
        return [
            _FakePos("000001.SZ", 100.0, 10.0),
            _FakePos("000002.SZ", 200.0, 20.0),
        ]

    def submit_order(self, order: Order) -> Order:
        order.order_id = "filled_1"
        order.status = OrderStatus.SUBMITTED
        self._submitted.append(order.symbol)
        return order


class _FakeService:
    """engine._on_force_close 消费用的假应急服务（dry-run 结构）。"""

    def execute(self, confirm: bool = False, symbol=None):
        return {
            "ok": True,
            "executed": False,
            "mode": "DRY_RUN",
            "liquidation": {"planned": 2},
        }


class TestEmergencyDryRun:
    def test_dry_run_never_executed_planned_only(self) -> None:
        """DoD：dry-run 不标 executed、计划项计 planned、不冒充 success。"""
        broker = _FakeBroker()
        svc = EmergencyLiquidationService(broker)
        report = svc.execute(confirm=False)
        assert report["executed"] is False
        liq = report["liquidation"]
        assert liq["planned"] == 2
        assert liq["success"] == 0
        assert report["ok"] is True  # 计划生成成功（但未真实执行）

    def test_confirm_executes_marks_executed(self) -> None:
        """DoD：confirm=True 标 executed 且 success 计数。"""
        broker = _FakeBroker()
        svc = EmergencyLiquidationService(broker)
        report = svc.execute(confirm=True)
        assert report["executed"] is True
        liq = report["liquidation"]
        assert liq["success"] == 2
        assert liq["planned"] == 0
        assert broker._cancelled == ["o_1"]
        assert broker._submitted == ["000001.SZ", "000002.SZ"]

    def test_empty_ok_but_not_executed(self) -> None:
        """DoD：空仓 dry-run 仍 executed=False（不因 ok=True 误判已平仓）。"""
        class _EmptyBroker(_FakeBroker):
            def get_positions(self):
                return []

        svc = EmergencyLiquidationService(_EmptyBroker())
        report = svc.execute(confirm=False)
        assert report["ok"] is True
        assert report["executed"] is False


class TestGuardianNoCallback:
    def _guardian(self) -> RiskGuardian:
        return RiskGuardian(risk_manager=object())  # type: ignore[arg-type]

    def test_force_close_all_no_callback_false(self) -> None:
        """DoD：无回调时 force_close_all 返回 False（不假成功）。"""
        g = self._guardian()
        assert g.force_close_all(reason="test") is False

    def test_force_close_symbol_no_callback_false(self) -> None:
        """DoD：无回调时 force_close_symbol 返回 False。"""
        g = self._guardian()
        assert g.force_close_symbol("000001.SZ", reason="test") is False


class TestEngineConsumeExecuted:
    def test_on_force_close_dry_run_logs_critical(self, caplog) -> None:
        """DoD：dry-run 时 engine._on_force_close 显式 CRITICAL（未真实执行）。"""
        engine = object.__new__(LiveEngine)
        engine._emergency_service = _FakeService()
        import logging

        with caplog.at_level(logging.CRITICAL, logger="live.engine"):
            ret = engine._on_force_close("all")
        assert ret is True
        assert any("未真实执行" in r.message for r in caplog.records)
