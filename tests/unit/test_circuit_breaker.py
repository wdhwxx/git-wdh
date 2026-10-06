# 文件名：tests/unit/test_circuit_breaker.py
"""
test_circuit_breaker.py - B-6 熔断线（EVENT 触发）单元测试
==================================================================
覆盖（对齐施工总清单 B-6 验收口径）：
  1. 注入风控击穿事件（publish 事件路径，R-02 不等周期）→ 熔断生效：
     禁开仓（risk.can_open_position()==False）+ Kill Switch（撤单被调）
     + 全流程审计留痕 + CRITICAL 告警。
  2. 复位经 RBAC：admin 成功（开仓恢复）/ 越权 viewer → 403。
  3. 可回滚：reset 后开仓恢复。
  4. shadow 演练不真动单（不撤单/不暂停风控）。
  5. 事件路径验证（同步，不等周期）。
  6. 防未来（triggered_at 只取已发生事件时间戳）。
  7. 自动全平默认关闭（不真全平）；显式启用才走 liquidation。

运行方式:
  pytest tests/unit/test_circuit_breaker.py -q
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional

import pytest  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.core.constants import SeverityLevel  # noqa: E402
from myquant.core.event_bus import Event, EventBus, EventType  # noqa: E402
from myquant.database.connection import DatabaseConnection  # noqa: E402
from myquant.monitoring.audit_log import AuditLogStore  # noqa: E402
from myquant.monitoring.circuit_breaker import (  # noqa: E402
    CircuitBreaker,
    CircuitBreakerConfig,
    reset_circuit_breaker_singleton,
)
from myquant.risk.risk_manager import RiskManager  # noqa: E402


# ═══════════════════════════════════════════════
#  fakes / fixtures
# ═══════════════════════════════════════════════

class _FakeOrder:
    """最小挂单桩。"""

    def __init__(self, order_id: str, symbol: str) -> None:
        self.order_id = order_id
        self.symbol = symbol


class _FakeBroker:
    """最小券商桩：记录撤单调用。"""

    def __init__(self) -> None:
        self.is_connected = True
        self.cancelled: List[str] = []
        self.active_orders = [_FakeOrder("o_1", "000001.SZ"),
                              _FakeOrder("o_2", "000002.SZ")]

    def connect(self) -> bool:
        return True

    def get_active_orders(self, symbol: Optional[str] = None) -> List[_FakeOrder]:
        return list(self.active_orders)

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True


class _FakeAlertManager:
    """假告警管理器：记录每级告警。"""

    def __init__(self) -> None:
        self.alerts: List[Any] = []

    def alert(self, level, message, source="", category="",
              details=None, deduplication_key="", runbook_id="") -> int:
        self.alerts.append({"level": level, "message": message})
        return 1


@pytest.fixture
def temp_audit():
    """临时审计库（不碰 quant.db）。"""
    tmp = tempfile.TemporaryDirectory()
    conn = DatabaseConnection(str(Path(tmp.name) / "cb_test.db"))
    store = AuditLogStore(connection=conn)
    try:
        yield store
    finally:
        conn.close_all()
        tmp.cleanup()


def _circuit_rows(store: AuditLogStore) -> List[dict]:
    return store.query(kind="circuit", limit=100)


# ═══════════════════════════════════════════════
#  1/5. 事件路径触发 → 禁开仓 + Kill Switch + 审计 + CRITICAL
# ═══════════════════════════════════════════════

class TestTripViaEvent:
    def test_publish_risk_violation_trips_full_chain(self, temp_audit) -> None:
        """注入风控击穿事件 → 熔断同步生效：撤单被调、开仓被拒、审计留痕、CRITICAL。"""
        broker = _FakeBroker()
        risk = RiskManager()
        am = _FakeAlertManager()
        bus = EventBus()

        cb = CircuitBreaker(
            broker=broker, risk_manager=risk, event_bus=bus,
            alert_manager=am, audit_store=temp_audit,
        )
        cb.subscribe_to_event_bus(bus)

        # 触发前可开仓
        assert risk.can_open_position() is True

        # R-02：publish 事件即同步触发动作链，不等周期
        n = bus.publish(Event(
            event_type=EventType.RISK_VIOLATION,
            data={"reason": "账户回撤击穿 20% 阈值"},
            source="test",
        ))
        assert n >= 1  # 本熔断 handler 被同步调用

        # 熔断生效
        assert cb.tripped is True
        # 禁开仓：风控被熔断暂停
        assert risk.can_open_position() is False
        # Kill Switch：撤单被调（全部挂单）
        assert broker.cancelled == ["o_1", "o_2"]
        # CRITICAL 告警（P0）
        assert any(a["level"] == SeverityLevel.CRITICAL for a in am.alerts)
        # 审计留痕：触发 + 每个动作
        rows = _circuit_rows(temp_audit)
        actions = [r["action"] for r in rows]
        assert "trip" in actions
        assert "disable_open" in actions
        assert "cancel_orders" in actions
        assert "kill_switch" in actions

    def test_to_dict_observable(self, temp_audit) -> None:
        """熔断状态可观测：tripped/triggered_at/reason/actions 列表。"""
        cb = CircuitBreaker(alert_manager=_FakeAlertManager(), audit_store=temp_audit)
        cb.trip(reason="测试击穿")
        snap = cb.to_dict()
        assert snap["tripped"] is True
        assert snap["triggered_at"] > 0
        assert "测试击穿" in snap["reason"]
        assert isinstance(snap["actions"], list)
        assert snap["can_open_position"] is False


# ═══════════════════════════════════════════════
#  3. 可回滚：reset 后开仓恢复
# ═══════════════════════════════════════════════

class TestResetRollback:
    def test_reset_restores_open_position(self, temp_audit) -> None:
        broker = _FakeBroker()
        risk = RiskManager()
        cb = CircuitBreaker(
            broker=broker, risk_manager=risk, event_bus=None,
            alert_manager=_FakeAlertManager(), audit_store=temp_audit,
        )
        cb.trip(reason="击穿")
        assert risk.can_open_position() is False

        result = cb.reset(actor="admin", reason="风险解除")
        assert result["reset"] is True
        assert result["was_tripped"] is True
        assert result["restored_open"] is True
        # 开仓恢复
        assert risk.can_open_position() is True
        assert cb.tripped is False
        # 复位审计
        rows = _circuit_rows(temp_audit)
        assert any(r["action"] == "reset" and r["actor"] == "admin" for r in rows)

    def test_reset_idempotent_when_not_tripped(self, temp_audit) -> None:
        """未触发时复位：幂等返回 was_tripped=False，不抛。"""
        cb = CircuitBreaker(audit_store=temp_audit)
        result = cb.reset(actor="admin", reason="空复位")
        assert result["reset"] is True
        assert result["was_tripped"] is False
        assert result["restored_open"] is False

    def test_reset_only_rolls_back_circuit_pause(self, temp_audit) -> None:
        """熔断复位只回滚熔断自己造成的暂停（_circuit_paused 标记）。"""
        risk = RiskManager()
        cb = CircuitBreaker(risk_manager=risk, audit_store=temp_audit)
        cb.trip(reason="击穿")
        assert risk.circuit_paused is True
        # 复位
        cb.reset(actor="admin")
        assert risk.circuit_paused is False


# ═══════════════════════════════════════════════
#  4. shadow 演练不真动单
# ═══════════════════════════════════════════════

class TestShadowDrill:
    def test_shadow_does_not_touch_orders_or_risk(self, temp_audit) -> None:
        broker = _FakeBroker()
        risk = RiskManager()
        am = _FakeAlertManager()
        cb = CircuitBreaker(
            broker=broker, risk_manager=risk, event_bus=None,
            alert_manager=am, audit_store=temp_audit,
            config=CircuitBreakerConfig(shadow=True),
        )
        cb.trip(reason="演练：风控击穿")
        # 仍记录触发与动作（可观测），但不真动单
        assert cb.tripped is True
        assert broker.cancelled == []           # 不真撤单
        assert risk.can_open_position() is True  # 不真禁开仓
        # 动作记录标注 shadow
        actions = cb.to_dict()["actions"]
        cancel_act = next(a for a in actions if a["action"] == "cancel_orders")
        assert cancel_act["detail"].get("shadow") is True


# ═══════════════════════════════════════════════
#  6. 防未来：只消费已发生事件时间戳
# ═══════════════════════════════════════════════

class TestAntiFuture:
    def test_triggered_at_uses_event_timestamp(self) -> None:
        """triggered_at 取已发生事件携带的 timestamp（不偷用未来/当前时刻）。"""
        past_ts = 1_700_000_000.0
        cb = CircuitBreaker()
        ev = Event(event_type=EventType.RISK_VIOLATION,
                   data={"reason": "防未来"}, timestamp=past_ts)
        cb.trip(reason="防未来", source_event=ev)
        assert cb.triggered_at == past_ts

    def test_trip_without_event_uses_record_time(self) -> None:
        """无 source_event 时 triggered_at 退化为记录时刻（当前时点，非未来）。"""
        cb = CircuitBreaker()
        before = __import__("time").time()
        cb.trip(reason="手动")
        after = __import__("time").time()
        assert before - 1.0 <= cb.triggered_at <= after + 1.0


# ═══════════════════════════════════════════════
#  7. 自动全平默认关闭
# ═══════════════════════════════════════════════

class TestAutoLiquidate:
    def test_auto_liquidate_off_by_default(self, temp_audit) -> None:
        """默认不自动全平：即使有 liquidation_service 也不被调用。"""
        class _FakeLiquidation:
            def __init__(self) -> None:
                self.calls: List[bool] = []

            def execute(self, confirm=False, symbol=None):
                self.calls.append(confirm)
                return {"ok": True, "executed": False, "mode": "DRY_RUN",
                        "cancelled_orders": []}

        liq = _FakeLiquidation()
        cb = CircuitBreaker(
            liquidation_service=liq, audit_store=temp_audit,
        )
        cb.trip(reason="击穿")
        assert liq.calls == []  # 默认关闭，不调全平

    def test_auto_liquidate_on_explicit_config(self, temp_audit) -> None:
        """显式启用 auto_liquidate=True → 调用 liquidation_service.execute。"""
        class _FakeLiquidation:
            def __init__(self) -> None:
                self.confirm_seen: Optional[bool] = None

            def execute(self, confirm=False, symbol=None):
                self.confirm_seen = confirm
                return {"ok": True, "executed": confirm, "mode": "LIVE" if confirm else "DRY_RUN",
                        "cancelled_orders": []}

        liq = _FakeLiquidation()
        cb = CircuitBreaker(
            liquidation_service=liq, audit_store=temp_audit,
            config=CircuitBreakerConfig(auto_liquidate=True),
        )
        cb.trip(reason="击穿")
        assert liq.confirm_seen is True  # 显式启用 → 真实 confirm


# ═══════════════════════════════════════════════
#  2. 复位经 RBAC（admin 成功 / 越权 403）
# ═══════════════════════════════════════════════

class TestResetEndpointRBAC:
    """经真实 FastAPI 端点验证 RBAC 鉴权（越权 403 / admin 成功）。"""

    def _find_endpoint(self, app, path: str, method: str):
        for route in app.routes:
            if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
                return route.endpoint
        raise AssertionError(f"未找到端点 {method} {path}")

    def test_viewer_reset_403(self) -> None:
        from myquant.api.dependencies.get_services import get_service_container
        from myquant.api.routes import create_app

        tmp = tempfile.TemporaryDirectory()
        conn = DatabaseConnection(str(Path(tmp.name) / "rbac_cb.db"))
        store = AuditLogStore(connection=conn)
        container = get_service_container()
        container.register("audit_log_store", store)
        reset_circuit_breaker_singleton()
        app = create_app(enable_auth=False, enable_rate_limit=False)
        try:
            ep = self._find_endpoint(app, "/api/monitor/circuit/reset", "POST")
            resp = asyncio.run(ep(x_user_role="viewer", x_user="op"))
            assert resp.status_code == 403
            # 审计留 deny
            denies = [r["action"] for r in store.query(kind="access", limit=50)
                      if r["action"].startswith("deny:")]
            assert "deny:reset_circuit_breaker" in denies
        finally:
            container.unregister("audit_log_store")
            reset_circuit_breaker_singleton()
            conn.close_all()
            tmp.cleanup()

    def test_admin_reset_success(self) -> None:
        from myquant.api.dependencies.get_services import get_service_container
        from myquant.api.routes import create_app

        tmp = tempfile.TemporaryDirectory()
        conn = DatabaseConnection(str(Path(tmp.name) / "rbac_cb2.db"))
        store = AuditLogStore(connection=conn)
        container = get_service_container()
        container.register("audit_log_store", store)
        reset_circuit_breaker_singleton()
        app = create_app(enable_auth=False, enable_rate_limit=False)
        try:
            ep = self._find_endpoint(app, "/api/monitor/circuit/reset", "POST")
            resp = asyncio.run(ep(x_user_role="admin", x_user="op"))
            # 鉴权通过 → 真实复位成功（ApiResponse，非 501）
            assert not hasattr(resp, "status_code") or resp.status_code == 200
        finally:
            container.unregister("audit_log_store")
            reset_circuit_breaker_singleton()
            conn.close_all()
            tmp.cleanup()
