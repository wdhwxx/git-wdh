# 文件名：tests/unit/test_rbac.py
"""
test_rbac.py - A-16 监控面访问控制 RBAC 单元测试
====================================================
覆盖（对齐 docs/监控档案/安全合规.md §5 与清单 A-16 验收）：
  1. 角色→权限矩阵：viewer / operator / admin / auditor
  2. normalize_role：空/非法 → 默认 admin（单人系统降级，向后兼容）
  3. RbacGuard：低权限调用 ack/resolve → 拒绝且审计留 deny 痕迹；
     高权限 → 放行且审计留 allowed 记录
  4. 端点级：POST /api/alerts/{id}/ack|resolve
       - viewer/auditor → 403 且审计表无成功动作（留 deny）
       - operator/admin → 成功
       - 不传角色 → 成功（既有调用方兼容）
  5. 受控写操作：modify_config / reset_circuit_breaker
       - viewer → 403；admin → 阈值仍 501；熔断复位已接线（B-6）→ 成功+留痕
  6. current_role_can / set_current_role（看板按钮禁用判定）

运行方式:
  pytest tests/unit/test_rbac.py -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from myquant.api.middleware.auth import (  # noqa: E402
    DEFAULT_ROLE,
    MonitorRole,
    PERM_ACK_ALERTS,
    PERM_MODIFY_CONFIG,
    PERM_RESOLVE_ALERTS,
    PERM_RESET_CIRCUIT,
    PERM_VIEW_ALERTS,
    RbacGuard,
    ROLE_PERMISSIONS,
    current_role_can,
    normalize_role,
    role_can,
    set_current_role,
)
from myquant.api.dependencies.get_services import get_service_container  # noqa: E402
from myquant.api.routes import create_app  # noqa: E402
from myquant.database.connection import DatabaseConnection  # noqa: E402
from myquant.monitoring.audit_log import AuditLogStore  # noqa: E402


# ═══════════════════════════════════════════════
#  fakes / fixtures
# ═══════════════════════════════════════════════

class _FakeAlertManager:
    """假告警管理器：ack/resolve 对已知 id 返回 True，记录动作。"""

    def __init__(self, alert_ids: List[str]):
        self._ids = set(alert_ids)
        self.actions: List[str] = []

    def acknowledge(self, alert_id: str) -> bool:
        if alert_id not in self._ids:
            return False
        self.actions.append(f"ack:{alert_id}")
        return True

    def resolve(self, alert_id: str) -> bool:
        if alert_id not in self._ids:
            return False
        self.actions.append(f"resolve:{alert_id}")
        return True


def _find_endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
            return route.endpoint
    raise AssertionError(f"未找到端点 {method} {path}")


def _run(ep, *args, **kwargs):
    return asyncio.run(ep(*args, **kwargs))


@pytest.fixture
def temp_audit():
    """临时审计库（不碰 quant.db），测试后从容器注销。"""
    tmp = tempfile.TemporaryDirectory()
    conn = DatabaseConnection(str(Path(tmp.name) / "rbac_audit.db"))
    store = AuditLogStore(connection=conn)
    container = get_service_container()
    container.register("audit_log_store", store)
    try:
        yield store
    finally:
        container.unregister("audit_log_store")
        conn.close_all()
        tmp.cleanup()


@pytest.fixture
def app():
    am = _FakeAlertManager(["a1"])
    return create_app(enable_auth=False, enable_rate_limit=False, alert_manager=am)


def _audit_actions(store: AuditLogStore) -> List[str]:
    """审计表中所有 access 类动作名（含 deny: 前缀）。"""
    return [r["action"] for r in store.query(kind="access", limit=100)]


# ═══════════════════════════════════════════════
#  1/2. 角色矩阵 + 归一化
# ═══════════════════════════════════════════════

class TestRoleMatrix:
    def test_viewer_only_view(self) -> None:
        assert role_can("viewer", PERM_VIEW_ALERTS)
        assert not role_can("viewer", PERM_ACK_ALERTS)
        assert not role_can("viewer", PERM_MODIFY_CONFIG)

    def test_operator_can_ack_resolve(self) -> None:
        assert role_can("operator", PERM_ACK_ALERTS)
        assert role_can("operator", PERM_RESOLVE_ALERTS)
        assert not role_can("operator", PERM_MODIFY_CONFIG)

    def test_admin_can_modify_and_reset(self) -> None:
        assert role_can("admin", PERM_MODIFY_CONFIG)
        assert role_can("admin", PERM_RESET_CIRCUIT)

    def test_auditor_readonly(self) -> None:
        assert role_can("auditor", PERM_VIEW_ALERTS)
        assert not role_can("auditor", PERM_ACK_ALERTS)
        assert not role_can("auditor", PERM_RESOLVE_ALERTS)

    def test_all_roles_defined(self) -> None:
        assert set(ROLE_PERMISSIONS.keys()) == {
            MonitorRole.VIEWER, MonitorRole.OPERATOR,
            MonitorRole.ADMIN, MonitorRole.AUDITOR,
        }


class TestNormalizeRole:
    def test_empty_defaults_admin(self) -> None:
        assert normalize_role("") == DEFAULT_ROLE == "admin"

    def test_unknown_defaults_admin(self) -> None:
        assert normalize_role("hacker") == "admin"

    def test_case_insensitive(self) -> None:
        assert normalize_role("Operator") == "operator"


# ═══════════════════════════════════════════════
#  3. RbacGuard 单元
# ═══════════════════════════════════════════════

class TestRbacGuard:
    def test_deny_writes_deny_audit(self, temp_audit) -> None:
        guard = RbacGuard(audit_store=temp_audit)
        ok, reason = guard.check("viewer", PERM_ACK_ALERTS, actor="op", resource="alert:a1")
        assert not ok
        assert "无权" in reason
        actions = _audit_actions(temp_audit)
        assert "deny:ack_alerts" in actions

    def test_allow_writes_allowed_audit(self, temp_audit) -> None:
        guard = RbacGuard(audit_store=temp_audit)
        ok, _ = guard.check("operator", PERM_ACK_ALERTS, actor="op", resource="alert:a1")
        assert ok
        assert "ack_alerts" in _audit_actions(temp_audit)


# ═══════════════════════════════════════════════
#  4. 端点级：ack/resolve 鉴权
# ═══════════════════════════════════════════════

class TestAckResolveEndpoint:
    def test_viewer_ack_forbidden(self, app, temp_audit) -> None:
        ep = _find_endpoint(app, "/api/alerts/{alert_id}/ack", "POST")
        resp = _run(ep, "a1", x_user_role="viewer", x_user="op1")
        assert resp.status_code == 403
        # 审计表无成功 ack 动作，只留 deny
        actions = _audit_actions(temp_audit)
        assert "ack_alerts" not in actions
        assert "deny:ack_alerts" in actions

    def test_auditor_resolve_forbidden(self, app, temp_audit) -> None:
        ep = _find_endpoint(app, "/api/alerts/{alert_id}/resolve", "POST")
        resp = _run(ep, "a1", x_user_role="auditor", x_user="op1")
        assert resp.status_code == 403
        actions = _audit_actions(temp_audit)
        assert "resolve_alerts" not in actions
        assert "deny:resolve_alerts" in actions

    def test_operator_ack_ok(self, app) -> None:
        ep = _find_endpoint(app, "/api/alerts/{alert_id}/ack", "POST")
        resp = _run(ep, "a1", x_user_role="operator", x_user="op2")
        # ApiResponse.success → 非 JSONResponse；无 status_code 即成功
        assert not hasattr(resp, "status_code") or resp.status_code == 200

    def test_default_role_backward_compat(self, app) -> None:
        """不传 X-User-Role → 默认 admin → 成功（既有调用方不破坏）。"""
        ep = _find_endpoint(app, "/api/alerts/{alert_id}/ack", "POST")
        resp = _run(ep, "a1")
        assert not hasattr(resp, "status_code") or resp.status_code == 200

    def test_admin_resolve_ok(self, app, temp_audit) -> None:
        ep = _find_endpoint(app, "/api/alerts/{alert_id}/resolve", "POST")
        resp = _run(ep, "a1", x_user_role="admin", x_user="op3")
        assert not hasattr(resp, "status_code") or resp.status_code == 200
        assert "resolve_alerts" in _audit_actions(temp_audit)


# ═══════════════════════════════════════════════
#  5. 受控写操作占位：改阈值 / 复位熔断
# ═══════════════════════════════════════════════

class TestGatedPlaceholders:
    def test_viewer_modify_threshold_forbidden(self, app, temp_audit) -> None:
        ep = _find_endpoint(app, "/api/monitor/config/threshold", "POST")
        resp = _run(ep, x_user_role="viewer", x_user="op")
        assert resp.status_code == 403
        assert "deny:modify_config" in _audit_actions(temp_audit)

    def test_admin_modify_threshold_gated_501(self, app) -> None:
        ep = _find_endpoint(app, "/api/monitor/config/threshold", "POST")
        resp = _run(ep, x_user_role="admin", x_user="op")
        # 鉴权通过 → 未接线 501
        assert resp.status_code == 501

    def test_viewer_reset_circuit_forbidden(self, app, temp_audit) -> None:
        ep = _find_endpoint(app, "/api/monitor/circuit/reset", "POST")
        resp = _run(ep, x_user_role="viewer", x_user="op")
        assert resp.status_code == 403
        assert "deny:reset_circuit_breaker" in _audit_actions(temp_audit)

    def test_admin_reset_circuit_wired(self, app, temp_audit) -> None:
        """B-6 接线后：admin 鉴权通过 → 真实复位成功（非 501），且复位留审计。"""
        # 复位前先触发一次熔断（容器内注入一个 tripped 的熔断线），验证真实回滚链路
        from myquant.api.dependencies.get_services import get_service_container
        from myquant.monitoring.circuit_breaker import (
            CircuitBreaker, reset_circuit_breaker_singleton,
        )
        reset_circuit_breaker_singleton()
        cb = CircuitBreaker(alert_manager=None, audit_store=temp_audit)
        cb.trip(reason="测试：风控击穿")
        container = get_service_container()
        container.register("circuit_breaker", cb)
        try:
            ep = _find_endpoint(app, "/api/monitor/circuit/reset", "POST")
            resp = _run(ep, x_user_role="admin", x_user="op")
            # 鉴权通过 → 真实复位成功（ApiResponse，无 501）
            assert not hasattr(resp, "status_code") or resp.status_code == 200
            assert not cb.tripped
            # 复位留痕：kind=circuit, action=reset
            circuit_actions = [
                r["action"] for r in temp_audit.query(kind="circuit", limit=50)
            ]
            assert "reset" in circuit_actions
        finally:
            container.unregister("circuit_breaker")
            reset_circuit_breaker_singleton()


# ═══════════════════════════════════════════════
#  6. 看板当前角色判定
# ═══════════════════════════════════════════════

class TestCurrentRole:
    def test_set_and_check(self) -> None:
        set_current_role("viewer")
        assert current_role_can(PERM_VIEW_ALERTS)
        assert not current_role_can(PERM_ACK_ALERTS)
        set_current_role("admin")
        assert current_role_can(PERM_ACK_ALERTS)
        # 复位默认，避免污染其他用例
        set_current_role(DEFAULT_ROLE)
