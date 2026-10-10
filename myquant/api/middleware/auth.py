# 文件名：myquant/api/middleware/auth.py
"""
auth.py - API认证中间件
========================
REST API认证中间件，支持API Key认证。

v1实现范围（基础骨架）:
  - APIKeyAuth 认证类（API Key验证）
  - verify_api_key() 验证函数
  - get_current_user() 获取当前用户（FastAPI依赖注入）
  - 支持从Header/Query参数获取API Key
  - 支持多API Key配置（环境变量/配置文件）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（JWT/OAuth2/角色权限全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 认证只验证当前凭证，不预测未来

防未来函数说明:
  - 认证只验证当前传入的API Key/凭证
  - 不使用任何未来数据或预测
  - 令牌过期时间基于当前时间判断，不包含未来信息

TODO(后续迭代):
  - JWT Token认证
  - OAuth2认证
  - 角色权限管理（RBAC）
  - 用户登录/注册
  - 令牌刷新机制
  - API Key加密存储
  - 登录日志/审计
  - IP白名单

依赖: typing, os, secrets, myquant.core.logger
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Any, Dict, Optional, Set, Tuple

from myquant.core.logger import get_logger

logger = get_logger("api.middleware.auth")


# ═══════════════════════════════════════════════
#  API Key认证
# ═══════════════════════════════════════════════

class APIKeyAuth:
    """
    API Key认证

    v1基础骨架：支持从Header/Query获取API Key，验证是否在允许列表中。

    使用方式:
        from myquant.api.middleware.auth import APIKeyAuth

        auth = APIKeyAuth(api_keys={"my-secret-key"})
        if auth.verify("my-secret-key"):
            # 认证通过
            pass

    与FastAPI集成（后续迭代）:
        from fastapi import Depends, HTTPException, Header

        async def get_api_key(x_api_key: str = Header(None)):
            if not auth.verify(x_api_key):
                raise HTTPException(status_code=401, detail="Invalid API Key")
            return x_api_key
    """

    def __init__(
        self,
        api_keys: Optional[Set[str]] = None,
        env_var: str = "QUANT_API_KEYS",
        header_name: str = "X-API-Key",
        query_param: str = "api_key",
        enabled: bool = True,
        allow_query_param: bool = False,
    ):
        """
        初始化API Key认证

        Parameters
        ----------
        api_keys : Set[str] | None
            允许的API Key集合（None则从环境变量读取）
        env_var : str
            环境变量名（多个Key用逗号分隔）
        header_name : str
            API Key的Header名称
        query_param : str
            API Key的Query参数名
        enabled : bool
            是否启用认证（False则跳过认证，开发环境用）
        allow_query_param : bool
            是否允许从 Query 参数提取 API Key（默认 False，H6 修复：
            Query 传 key 会进入浏览器历史/代理日志/访问日志，默认只走 Header；
            蓝图 v1 双通道能力保留，显式开启可用）
        """
        self.header_name = header_name
        self.query_param = query_param
        self.enabled = enabled
        self.allow_query_param = allow_query_param

        # 加载API Key
        self._api_keys: Set[str] = set()
        if api_keys:
            self._api_keys.update(api_keys)

        # 从环境变量加载
        env_keys = os.environ.get(env_var, "")
        # KP-P1-04: 环境变量缺失时经 settings（.env/config）兜底，
        #           避免 .env 未被显式加载时密钥不生效
        if not env_keys:
            try:
                from myquant.core.settings import get_settings as _gs
                env_keys = _gs().get(env_var, "") or ""
            except Exception:
                env_keys = ""
        if env_keys:
            for key in env_keys.split(","):
                key = key.strip()
                if key:
                    self._api_keys.add(key)

        if not self._api_keys and enabled:
            logger.warning(
                f"[auth] 未配置API Key，认证将拒绝所有请求。"
                f"请设置环境变量 {env_var} 或传入 api_keys 参数。"
            )

        logger.info(
            f"[auth] API Key认证初始化: enabled={enabled}, "
            f"keys={len(self._api_keys)}, header={header_name}"
        )

    @staticmethod
    def _key_fingerprint(api_key: str) -> str:
        """API Key 指纹（sha256 前 8 位），日志追踪用，不可还原明文。"""
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]

    def verify(self, api_key: Optional[str]) -> bool:
        """
        验证API Key

        Parameters
        ----------
        api_key : str | None
            待验证的API Key

        Returns
        -------
        bool : 是否验证通过
        """
        if not self.enabled:
            return True  # 认证禁用，全部通过

        if not api_key:
            logger.warning("[auth] 认证失败：未提供API Key")
            return False

        # 使用常量时间比较，防止时序攻击
        for valid_key in self._api_keys:
            if secrets.compare_digest(api_key, valid_key):
                return True

        logger.warning(
            f"[auth] 认证失败：无效API Key（sha256前8位: {self._key_fingerprint(api_key)}）"
        )
        return False

    def extract_from_headers(self, headers: Dict[str, Any]) -> Optional[str]:
        """
        从请求头提取API Key

        Parameters
        ----------
        headers : Dict
            请求头字典

        Returns
        -------
        str | None : API Key（未找到返回None）
        """
        # 不区分大小写查找
        for key, value in headers.items():
            if key.lower() == self.header_name.lower():
                return value
        return None

    def extract_from_query(self, query_params: Dict[str, Any]) -> Optional[str]:
        """
        从Query参数提取API Key

        Parameters
        ----------
        query_params : Dict
            Query参数字典

        Returns
        -------
        str | None : API Key（未找到返回None）
        """
        return query_params.get(self.query_param)

    def verify_request(self, headers: Dict[str, Any], query_params: Dict[str, Any]) -> bool:
        """
        验证请求（从Header和Query中提取API Key）

        Parameters
        ----------
        headers : Dict
            请求头
        query_params : Dict
            Query参数

        Returns
        -------
        bool : 是否验证通过
        """
        api_key = self.extract_from_headers(headers)
        if not api_key and self.allow_query_param:
            api_key = self.extract_from_query(query_params)
        return self.verify(api_key)

    @property
    def key_count(self) -> int:
        """已配置的API Key数量"""
        return len(self._api_keys)

    def add_key(self, api_key: str) -> None:
        """添加API Key"""
        self._api_keys.add(api_key)
        logger.info(f"[auth] 添加API Key: {self._key_fingerprint(api_key)}")

    def remove_key(self, api_key: str) -> None:
        """移除API Key"""
        self._api_keys.discard(api_key)
        logger.info(f"[auth] 移除API Key: {self._key_fingerprint(api_key)}")


# ═══════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════

def create_auth(
    api_keys: Optional[Set[str]] = None,
    enabled: bool = True,
) -> APIKeyAuth:
    """
    便捷函数：创建API Key认证实例

    Parameters
    ----------
    api_keys : Set[str] | None
        允许的API Key集合
    enabled : bool
        是否启用认证

    Returns
    -------
    APIKeyAuth : 认证实例
    """
    return APIKeyAuth(api_keys=api_keys, enabled=enabled)


# ════════════════════════════════════════════════════════════════════════════════════════
#  KP-M04（T-30）写操作准入守卫（方案B：仅高危写操作审批，复核人可配置）
#  三道防线/职责分离代码载体：高危写操作（强平/资金调拨/实盘切换）启用后
#  须经审批放行，未审批拒绝；普通写操作仅审计留痕。审计写 JSONL 文件。
# ════════════════════════════════════════════════════════════════════════════════════════════

HIGH_RISK_SCOPES: frozenset = frozenset({
    "force_close",       # 强制平仓（guardian.force_close_all / force_close_symbol）
    "transfer_cash",     # 资金调拨（portfolio.transfer_cash）
    "mode_switch_live",  # 实盘模式切换（run_mode.apply_mode_to_config -> LIVE）
})

SCOPE_DESCRIPTIONS = {
    "force_close": "强制平仓",
    "transfer_cash": "资金调拨",
    "mode_switch_live": "实盘模式切换",
}


class OperationGuard:
    """
    写操作准入守卫（KP-M04 方案B 落地）

    配置（config/live_admission.yaml）：
      admission:
        enabled: false            # 是否启用准入审批（默认 false 仅审计留痕，不阻断）
        require_reviewer: false   # 是否要求外部复核人（职责分离第二人）
        reviewer: ""              # 复核人标识（require_reviewer=true 时必填）
        audit_dir: logs/audit     # 审计落盘目录
        high_risk_scopes: []      # 自定义高危操作域（缺省=HIGH_RISK_SCOPES）

    防未来：只审查当前操作的审批状态，不预测未来；配置缺失/加载失败一律
    fail-safe 回退 enabled=false（仅审计留痕），绝不阻断正常流程。
    """

    def __init__(self, config_path: Optional[str] = None):
        self._enabled = False
        self._require_reviewer = False
        self._reviewer = ""
        self._audit_dir = "logs/audit"
        self._high_risk_scopes: Set[str] = set(HIGH_RISK_SCOPES)
        # 0.4/F-1：配置加载失败留痕（run_实盘入口.py LIVE 装配据此拒启，能力缺失 fail-closed）
        self._load_error: str = ""
        if config_path:
            self._load(config_path)

    def _load(self, config_path: str) -> None:
        """加载 live_admission.yaml；任何异常置 _load_error（F-1：能力缺失由装配层拒启）。"""
        try:
            from myquant.core.config_loader import ConfigLoader
            cfg = ConfigLoader().load(config_path)
            adm = cfg.get("admission") or {}
            self._enabled = bool(adm.get("enabled", False))
            self._require_reviewer = bool(adm.get("require_reviewer", False))
            self._reviewer = str(adm.get("reviewer", "") or "")
            audit_dir = adm.get("audit_dir")
            if isinstance(audit_dir, str) and audit_dir.strip():
                self._audit_dir = audit_dir.strip()
            scopes = adm.get("high_risk_scopes")
            if isinstance(scopes, list):
                self._high_risk_scopes = {str(s) for s in scopes if str(s)}
        except Exception as e:
            self._load_error = f"{type(e).__name__}: {e}"
            logger.warning(f"[admission] 准入配置加载失败: {self._load_error}")

    @property
    def load_error(self) -> str:
        """准入配置加载错误（空=加载成功）。"""
        return self._load_error

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def high_risk_scopes(self) -> Set[str]:
        return set(self._high_risk_scopes)

    def check(
        self,
        scope: str,
        operator: str,
        reviewer: Optional[str] = None,
        action: str = "",
        actor_type: str = "human",
    ) -> Tuple[bool, str]:
        """
        高危写操作准入检查。

        Parameters
        ----------
        scope : str
            写操作域（HIGH_RISK_SCOPES）
        operator : str
            操作主体标识
        reviewer : str | None
            外部复核人（require_reviewer=true 时 human 角色必填）
        action : str
            操作说明
        actor_type : str
            0.4/决策点3 双语义："human" 维持现语义（require_reviewer 时
            校验复核人）；"system"（guardian/引擎/应急脚本等程序角色）
            跳过 require_reviewer 复核人校验但**保留审计**——防止把应急
            强平/系统自动操作焊死在复核人缺失上（R-6），审计记录带 actor_type。

        Returns
        -------
        (ok, reason) : ok=False 表示应拒绝执行，reason 为拒绝原因。
        """
        if scope not in self._high_risk_scopes:
            # 非高危写操作：仅审计留痕，不阻断
            self.audit(scope, operator, action, "allowed", reviewer,
                       "非高危写操作（仅审计）", actor_type=actor_type)
            return True, ""
        if not self._enabled:
            self.audit(scope, operator, action, "allowed", reviewer,
                       "准入未启用（仅审计留痕）", actor_type=actor_type)
            return True, ""
        if self._require_reviewer and actor_type != "system":
            if not reviewer or reviewer != self._reviewer:
                desc = SCOPE_DESCRIPTIONS.get(scope, scope)
                reason = f"高危写操作[{desc}]需配置复核人审批：reviewer 缺失或不匹配"
                self.audit(scope, operator, action, "denied", reviewer, reason,
                           actor_type=actor_type)
                return False, reason
        self.audit(scope, operator, action, "approved", reviewer, "审批通过",
                   actor_type=actor_type)
        return True, ""

    def audit(
        self,
        scope: str,
        operator: str,
        action: str,
        result: str,
        reviewer: Optional[str] = None,
        reason: str = "",
        actor_type: str = "human",
    ) -> None:
        """审计留痕：logger + JSONL 落盘 audit_dir/write_audit.jsonl（异常仅告警不阻断）。"""
        from datetime import datetime
        ts = datetime.now().isoformat(timespec="seconds")
        record = {
            "ts": ts,
            "scope": scope,
            "operator": operator,
            "action": action,
            "result": result,
            "reviewer": reviewer or "",
            "reason": reason,
            "actor_type": actor_type,
        }
        logger.info(
            f"[admission] {result} scope={scope} operator={operator} "
            f"action={action} reviewer={reviewer or ''} actor_type={actor_type} {reason}"
        )
        try:
            import json
            from pathlib import Path
            d = Path(self._audit_dir)
            d.mkdir(parents=True, exist_ok=True)
            with (d / "write_audit.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[admission] 审计落盘失败（仅告警不阻断）: {e}")


_default_guard: Optional[OperationGuard] = None


def get_operation_guard(config_path: Optional[str] = None) -> OperationGuard:
    """获取准入守卫：默认单例（不加载配置 -> 仅审计留痕）；显式传配置路径则新建。"""
    global _default_guard
    if config_path is None:
        if _default_guard is None:
            _default_guard = OperationGuard()
        return _default_guard
    return OperationGuard(config_path)


def require_scope(
    subject: str,
    scope: str,
    reviewer: Optional[str] = None,
    action: str = "",
    guard: Optional[OperationGuard] = None,
) -> Tuple[bool, str]:
    """subject 为主体标识，scope 为写操作域。返回 (ok, reason)。"""
    g = guard if guard is not None else get_operation_guard()
    return g.check(scope, operator=subject, reviewer=reviewer, action=action)


# ════════════════════════════════════════════════════════════════════════════════════════
#  A-16 监控面访问控制 RBAC（R-06 / P0-7 §5）
#  角色→动作矩阵：viewer / operator / admin / auditor；
#  写动作（ack/resolve/改阈值/复位熔断）逐角色鉴权 + 审计留痕（BASE-7）。
#  单人系统降级：默认本人=admin+auditor（既有调用方不传角色仍可用，不破坏兼容）。
# ════════════════════════════════════════════════════════════════════════════════════════

class MonitorRole:
    """监控面角色常量（对齐 docs/监控档案/安全合规.md §5）。"""

    VIEWER: str = "viewer"        # 查看者：查看看板/告警（脱敏视图）
    OPERATOR: str = "operator"    # 告警处置者：查看 + 确认/解决告警
    ADMIN: str = "admin"          # 配置管理者：查看/处置 + 改阈值/告警规则/复位熔断
    AUDITOR: str = "auditor"      # 只读审计：查看审计日志，一切写操作禁止


# 动作（权限点）
PERM_VIEW_ALERTS: str = "view_alerts"
PERM_ACK_ALERTS: str = "ack_alerts"
PERM_RESOLVE_ALERTS: str = "resolve_alerts"
PERM_MODIFY_CONFIG: str = "modify_config"
PERM_RESET_CIRCUIT: str = "reset_circuit_breaker"

# 角色→权限矩阵（P0-7 §5）
ROLE_PERMISSIONS: Dict[str, frozenset] = {
    MonitorRole.VIEWER: frozenset({PERM_VIEW_ALERTS}),
    MonitorRole.OPERATOR: frozenset({PERM_VIEW_ALERTS, PERM_ACK_ALERTS, PERM_RESOLVE_ALERTS}),
    MonitorRole.ADMIN: frozenset({
        PERM_VIEW_ALERTS, PERM_ACK_ALERTS, PERM_RESOLVE_ALERTS,
        PERM_MODIFY_CONFIG, PERM_RESET_CIRCUIT,
    }),
    # auditor：只读审计通道保留（防未来多人协作），无任何写权限
    MonitorRole.AUDITOR: frozenset({PERM_VIEW_ALERTS}),
}

# 单人系统降级默认角色：不传角色 → admin（保持既有端点可用；P0-7 §5 本人=admin+auditor）
DEFAULT_ROLE: str = MonitorRole.ADMIN

_VALID_ROLES: frozenset = frozenset(ROLE_PERMISSIONS.keys())


def normalize_role(role: Optional[str]) -> str:
    """
    归一化角色声明：空/非法 → DEFAULT_ROLE（fail-safe 向后兼容）。

    Parameters
    ----------
    role : str | None
        调用方声明的角色（Header X-User-Role 等）

    Returns
    -------
    str : 合法角色名
    """
    r = (role or "").strip().lower()
    if r in _VALID_ROLES:
        return r
    if r:
        logger.warning(f"[rbac] 未知角色 {role!r}，按默认角色 {DEFAULT_ROLE} 处理")
    return DEFAULT_ROLE


def role_can(role: Optional[str], permission: str) -> bool:
    """
    判定角色是否拥有某权限点。

    Parameters
    ----------
    role : str | None
        角色（未归一化也接受）
    permission : str
        权限点（PERM_*）

    Returns
    -------
    bool : True=授权
    """
    return permission in ROLE_PERMISSIONS.get(normalize_role(role), frozenset())


# ── 请求上下文当前角色（看板/单测可切换；默认 admin）──
_CURRENT_ROLE: str = DEFAULT_ROLE


def set_current_role(role: Optional[str]) -> None:
    """设置当前上下文角色（看板按角色隐藏/禁用按钮；单测 monkeypatch 用）。"""
    global _CURRENT_ROLE
    _CURRENT_ROLE = normalize_role(role)


def get_current_role() -> str:
    """获取当前上下文角色。"""
    return _CURRENT_ROLE


def current_role_can(permission: str) -> bool:
    """当前上下文角色是否拥有某权限点（看板按钮禁用判定用）。"""
    return role_can(_CURRENT_ROLE, permission)


class RbacGuard:
    """
    监控面 RBAC 守卫：写动作鉴权 + 审计留痕（BASE-7 审计流）。

    用法:
        guard = RbacGuard(audit_store)          # audit_store 为 AuditLogStore（可注入）
        ok, reason = guard.check(role="viewer", permission="ack_alerts",
                                 actor="op-1", resource="alert:a1")
        # ok=False → 403；无论放行/拒绝都写审计（拒绝留 deny 痕迹）
    """

    def __init__(self, audit_store: Any = None):
        """
        Parameters
        ----------
        audit_store : AuditLogStore | None
            审计库（注入；None 时审计只记 logger 不抛异常，不阻断主流程）
        """
        self._audit = audit_store

    def check(
        self,
        role: Optional[str],
        permission: str,
        actor: str = "anonymous",
        resource: str = "",
    ) -> Tuple[bool, str]:
        """
        鉴权并留痕。

        Returns
        -------
        (ok, reason) : ok=False 应拒绝（403）；reason 为拒绝/放行说明
        """
        r = normalize_role(role)
        ok = permission in ROLE_PERMISSIONS.get(r, frozenset())
        if ok:
            self._trace(r, permission, actor, resource, "allowed", "")
            return True, ""
        reason = f"角色[{r}]无权执行[{permission}]"
        self._trace(r, permission, actor, resource, "denied", reason)
        return False, reason

    def _trace(
        self,
        role: str,
        permission: str,
        actor: str,
        resource: str,
        result: str,
        reason: str,
    ) -> None:
        """审计留痕：logger + AuditLogStore.append（失败仅告警不阻断）。"""
        logger.info(f"[rbac] {result} role={role} perm={permission} actor={actor} "
                    f"resource={resource} {reason}")
        if self._audit is None:
            return
        try:
            self._audit.append(
                kind="access",
                action=permission if result == "allowed" else f"deny:{permission}",
                actor=actor or "anonymous",
                detail={"resource": resource, "role": role, "result": result,
                        "reason": reason},
            )
        except Exception as e:  # 审计失败不阻断业务（仅告警）
            logger.warning(f"[rbac] 审计留痕失败（不阻断）: {e}")


# ═══════════════════════════════════════════════
#  自测（python -m myquant.api.middleware.auth）
# ═══════════════════════════════════════════════

def _run_rbac_self_test() -> int:
    """RBAC 角色矩阵 + 守卫留痕自测，0=通过。"""
    passed = 0
    failed = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            logger.info("  ✓ %s", label)
        else:
            failed += 1
            logger.error("  ✗ %s %s", label, detail)

    # 角色矩阵
    check("viewer 仅可查看", role_can("viewer", PERM_VIEW_ALERTS)
          and not role_can("viewer", PERM_ACK_ALERTS))
    check("operator 可确认/解决", role_can("operator", PERM_ACK_ALERTS)
          and role_can("operator", PERM_RESOLVE_ALERTS)
          and not role_can("operator", PERM_MODIFY_CONFIG))
    check("admin 可改阈值/复位熔断", role_can("admin", PERM_MODIFY_CONFIG)
          and role_can("admin", PERM_RESET_CIRCUIT))
    check("auditor 只读无写权限", role_can("auditor", PERM_VIEW_ALERTS)
          and not role_can("auditor", PERM_ACK_ALERTS))

    # 归一化：非法/空 → 默认 admin（向后兼容）
    check("非法角色→默认admin", normalize_role("hacker") == DEFAULT_ROLE)
    check("空角色→默认admin", normalize_role("") == DEFAULT_ROLE)
    check("大小写归一", normalize_role("Operator") == "operator")

    # 守卫：低权限拒绝 + 留痕；高权限放行
    class _FakeAudit:
        def __init__(self) -> None:
            self.records = []

        def append(self, kind, action, actor="system", detail=None):
            self.records.append((kind, action, actor, detail or {}))
            return {"seq": len(self.records)}

    audit = _FakeAudit()
    guard = RbacGuard(audit_store=audit)
    ok, reason = guard.check(role="viewer", permission=PERM_ACK_ALERTS,
                             actor="op-1", resource="alert:a1")
    check("viewer ack 被拒", not ok and "无权" in reason)
    check("拒绝留 deny 痕迹", any(a == "deny:ack_alerts" for _, a, _, _ in audit.records))
    ok2, _ = guard.check(role="operator", permission=PERM_ACK_ALERTS,
                         actor="op-2", resource="alert:a1")
    check("operator ack 放行", ok2)
    check("放行留 allowed 审计", any(a == "ack_alerts" for _, a, _, _ in audit.records))

    logger.info("RBAC 自测结果: %d 通过, %d 失败", passed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)-5s %(message)s")
    raise SystemExit(_run_rbac_self_test())
