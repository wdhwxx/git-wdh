# 文件名：myquant/monitoring/circuit_breaker.py
"""
circuit_breaker.py - 熔断线（B-6，EVENT 触发 / R-02）
================================================================
风控击穿后的最高优先级保护：一旦收到"风控击穿"事件，立即串行动作链，
在券商侧与风控侧同时把账户锁死，防止风险继续扩大。

动作链（触发判定 → 自动动作，单步失败不阻断其余）:
  1. 禁开仓  —— 联动 RiskManager：禁止一切新开仓（可回滚）。
  2. 撤全部挂单 —— 遍历 broker.get_active_orders() + cancel_order，
     释放冻结资金、避免与后续平仓重复委托；单笔异常不阻断其余。
  3. 降杠杆  —— 预留接口：若 broker 暴露 set_leverage 则联动降到 1 倍；
     当前系统无落地接口，仅记录动作（docstring 注明）。
  4. Kill Switch —— 撤单 + 禁开仓达成即视为 Kill Switch 生效，状态可观测。
  5. 自动全平（可选，默认关闭）—— 联动 EmergencyLiquidationService；
     必须显式配置 auto_liquidate=True 才会 confirm=True 真实全平
     （人工确认兜底原则）。

联动:
  - RiskManager（myquant/risk/risk_manager.py）：pause_for_circuit_breaker /
    resume_from_circuit_breaker（只回滚熔断自己造成的暂停，幂等）。
  - EmergencyLiquidationService（myquant/live/emergency_liquidation.py）：
    execute(confirm=...)，默认 dry_run；仅 auto_liquidate 时真实执行。

复位（人工，受 A-16 RBAC 鉴权）:
  - reset(actor, reason) 恢复开仓能力（可回滚）+ 审计 append
    （kind="circuit", action="reset"）+ INFO 告警。
  - 复位必须经 POST /api/monitor/circuit/reset（端点内 guard.check
    permission="reset_circuit_breaker"，越权 403 + 审计 deny）。

全流程留痕:
  - 触发 / 每个自动动作（撤单/禁开仓/降杠杆/Kill Switch/全平）/ 复位，
    均审计 append(kind="circuit")；触发为 P0/CRITICAL 告警，复位为 INFO。

人工兜底与权限边界:
  - 复位需 admin（RBAC 已在端点）；自动全平需显式配置启用；
  - shadow 演练模式：演练只记录动作、不真动单（不撤单/不暂停/不全平）。

防未来函数:
  - triggered_at 直接取"已发生触发事件"携带的 timestamp（事件发生时刻），
    不使用未来数据；审计/告警时间戳取记录时刻 time.time()（当前时点）。

依赖: time, dataclasses, typing, myquant.core.event_bus / logger / constants
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import Event, EventBus, EventType
from myquant.core.logger import get_logger

logger = get_logger("monitoring.circuit_breaker")


# ═══════════════════════════════════════════════
#  常量（模块顶部，禁止魔法数字散落）
# ═══════════════════════════════════════════════

#: 默认触发熔断的事件类型：风控击穿 + 触及风控限额（均为"已发生"事件）
DEFAULT_TRIGGER_EVENT_TYPES: Tuple[EventType, ...] = (
    EventType.RISK_VIOLATION,
    EventType.RISK_LIMIT_HIT,
)

#: 审计类别（与 audit_log.append(kind=...) 约定）
AUDIT_KIND: str = "circuit"

#: 审计动作名
ACTION_TRIP: str = "trip"
ACTION_RESET: str = "reset"
ACTION_CANCEL_ORDERS: str = "cancel_orders"
ACTION_DISABLE_OPEN: str = "disable_open"
ACTION_DELEVER: str = "delever"
ACTION_KILL_SWITCH: str = "kill_switch"
ACTION_LIQUIDATE: str = "auto_liquidate"

#: 降杠杆目标倍数（当前仅在 broker 暴露 set_leverage 时落地）
DELEVER_TARGET_RATIO: float = 1.0


# ═══════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════

@dataclass
class CircuitBreakerConfig:
    """
    熔断线配置

    Attributes
    ----------
    auto_liquidate : bool
        触发后是否自动全平（confirm=True 真实执行）。默认 False——
        人工确认兜底原则：熔断默认只撤单+禁开仓+Kill Switch，自动全平
        必须显式启用。
    shadow : bool
        shadow/演练模式。True 时只记录动作、发告警、留审计，但不真动单
        （不撤单/不暂停风控/不调用 set_leverage/不全平）。
    trigger_event_types : Tuple[EventType, ...]
        触发熔断的事件类型集合（默认风控击穿 + 触及限额）。
    liquidate_confirm : bool
        若启用 auto_liquidate，是否真实 confirm 执行（默认 True；shadow
        模式下强制退化为 dry_run）。
    """

    auto_liquidate: bool = False
    shadow: bool = False
    trigger_event_types: Tuple[EventType, ...] = DEFAULT_TRIGGER_EVENT_TYPES
    liquidate_confirm: bool = True


# ═══════════════════════════════════════════════
#  熔断线
# ═══════════════════════════════════════════════

class CircuitBreaker:
    """
    熔断线（EVENT 触发，R-02：事件到达即同步执行动作链，不等周期轮询）。

    用法:
        from myquant.core.event_bus import get_event_bus
        cb = CircuitBreaker(broker=broker, risk_manager=risk_mgr,
                            event_bus=get_event_bus(), audit_store=audit)
        cb.subscribe_to_event_bus()
        # 风控击穿事件 publish 后，动作链同步执行完毕
        # 人工复位（经 API RBAC 鉴权后）：
        cb.reset(actor="admin", reason="风险已解除")

    Parameters
    ----------
    broker : Any
        券商适配器（需具备 get_active_orders / cancel_order；可选 set_leverage）。
        None 时撤单/降杠杆动作安全跳过。
    risk_manager : Any
        风控管理器（需具备 pause_for_circuit_breaker / resume_from_circuit_breaker /
        can_open_position）。None 时禁开仓动作安全跳过。
    event_bus : EventBus | None
        事件总线；None 时 subscribe_to_event_bus() 会懒加载全局单例。
    alert_manager : Any | None
        告警管理器（需具备 alert(level, message, source, category, details)）。
        None 时告警降级为 logger。
    audit_store : Any | None
        审计只追加库（需具备 append(kind, action, actor, detail)）。
        None 时审计降级为 logger。
    liquidation_service : Any | None
        紧急平仓服务（需具备 execute(confirm, symbol) -> dict）。None 且启用
        auto_liquidate 时，若 broker 可用则现场构造 EmergencyLiquidationService。
    config : CircuitBreakerConfig | None
        配置（None 用默认配置）。
    """

    def __init__(
        self,
        broker: Optional[Any] = None,
        risk_manager: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
        alert_manager: Optional[Any] = None,
        audit_store: Optional[Any] = None,
        liquidation_service: Optional[Any] = None,
        config: Optional[CircuitBreakerConfig] = None,
    ) -> None:
        self._broker = broker
        self._risk = risk_manager
        self._bus = event_bus
        self._alert = alert_manager
        self._audit = audit_store
        self._liquidation = liquidation_service
        self._config = config or CircuitBreakerConfig()

        # 熔断状态（可观测）
        self._tripped: bool = False
        self._triggered_at: float = 0.0
        self._reason: str = ""
        self._trigger_event_id: str = ""
        self._actions: List[Dict[str, Any]] = []
        self._trip_count: int = 0
        self._reset_count: int = 0
        self._handler_ids: List[str] = []

        logger.info(
            f"[circuit_breaker] 初始化: shadow={self._config.shadow}, "
            f"auto_liquidate={self._config.auto_liquidate}, "
            f"触发事件={[t.value for t in self._config.trigger_event_types]}"
        )

    # ═══════════════════════════════════════════
    #  事件订阅（R-02：事件路径触发，不等周期）
    # ═══════════════════════════════════════════

    def subscribe_to_event_bus(self, bus: Optional[EventBus] = None) -> List[str]:
        """
        订阅触发事件（风控击穿 / 触及限额）。事件到达即同步执行动作链。

        Parameters
        ----------
        bus : EventBus | None
            事件总线；None 时使用构造注入或全局单例。

        Returns
        -------
        List[str] : 订阅的 handler_id 列表
        """
        if bus is not None:
            self._bus = bus
        if self._bus is None:
            self._bus = get_event_bus_safe()
        if self._bus is None:
            logger.warning("[circuit_breaker] 无可用事件总线，跳过订阅")
            return []

        for et in self._config.trigger_event_types:
            hid = self._bus.subscribe(et, self._on_trigger_event)
            self._handler_ids.append(hid)
        logger.info(f"[circuit_breaker] 已订阅 {len(self._handler_ids)} 个触发事件")
        return list(self._handler_ids)

    def unsubscribe(self) -> int:
        """取消全部触发事件订阅，返回取消的 handler 数。"""
        if self._bus is None or not self._handler_ids:
            return 0
        count = 0
        for hid in self._handler_ids:
            try:
                if self._bus.unsubscribe(hid):
                    count += 1
            except Exception as e:  # noqa: BLE001 - 单个取消失败不阻断其余
                logger.warning(f"[circuit_breaker] 取消订阅失败 {hid}: {e}")
        self._handler_ids = []
        return count

    # ═══════════════════════════════════════════
    #  触发（事件回调 + 手动入口）
    # ═══════════════════════════════════════════

    def _on_trigger_event(self, event: Event) -> None:
        """
        触发事件回调（事件总线同步分发）。

        只消费事件已携带的 data / timestamp（已发生事实），不预测未来。

        Parameters
        ----------
        event : Event
            风控击穿 / 触及限额事件
        """
        data = getattr(event, "data", None) or {}
        reason = str(
            data.get("reason")
            or data.get("message")
            or f"风控事件 {event.type_name}"
        )
        self.trip(reason=reason, source_event=event)

    def trip(
        self,
        reason: str,
        source_event: Optional[Event] = None,
    ) -> Dict[str, Any]:
        """
        触发熔断（幂等：已触发则直接返回当前状态，不重复动作）。

        动作链顺序：禁开仓 → 撤全部挂单 → 降杠杆 → Kill Switch → （可选）自动全平。
        每步单步失败记录不阻断其余。

        Parameters
        ----------
        reason : str
            触发原因（风控击穿描述）
        source_event : Event | None
            触发源事件（用于防未来：triggered_at 取其发生时刻）

        Returns
        -------
        Dict[str, Any] : to_dict() 快照
        """
        reason_s = (reason or "").strip()
        if not reason_s:
            reason_s = "风控击穿（未携带原因）"

        if self._tripped:
            logger.warning("[circuit_breaker] 已处于熔断状态，忽略重复触发")
            return self.to_dict()

        # 防未来：triggered_at 取已发生事件的 timestamp；无事件则记录时刻
        ts = 0.0
        if source_event is not None:
            ts = float(getattr(source_event, "timestamp", 0.0) or 0.0)
        if ts <= 0.0:
            ts = time.time()  # 记录时刻（当前时点）

        self._tripped = True
        self._triggered_at = ts
        self._reason = reason_s
        self._trigger_event_id = str(getattr(source_event, "event_id", "") or "")
        self._trip_count += 1
        self._actions = []

        # 触发留痕 + P0/CRITICAL 告警
        self._audit_log(
            ACTION_TRIP,
            actor="circuit_breaker",
            detail={
                "reason": reason_s,
                "event_id": self._trigger_event_id,
                "shadow": self._config.shadow,
                "triggered_at": self._triggered_at,
            },
        )
        self._send_alert(
            SeverityLevel.CRITICAL,
            f"[P0] 熔断触发：{reason_s}",
            details={"reason": reason_s, "triggered_at": self._triggered_at,
                     "shadow": self._config.shadow},
        )
        logger.critical(f"[circuit_breaker] 熔断触发: {reason_s} (shadow={self._config.shadow})")

        # 动作链（单步失败不阻断其余）
        self._act_disable_open()
        self._act_cancel_orders()
        self._act_delever()
        self._act_kill_switch()
        if self._config.auto_liquidate:
            self._act_liquidate()

        return self.to_dict()

    # ═══════════════════════════════════════════
    #  人工复位（可回滚；须经 API RBAC 鉴权）
    # ═══════════════════════════════════════════

    def reset(self, actor: str = "system", reason: str = "人工复位") -> Dict[str, Any]:
        """
        人工复位熔断：恢复开仓能力（可回滚）+ 审计 + INFO 告警。

        幂等：未处于熔断状态时仍留痕审计（记录一次授权复位尝试），返回
        was_tripped=False，不抛出。

        Parameters
        ----------
        actor : str
            操作者（API 透传 X-User）
        reason : str
            复位原因

        Returns
        -------
        Dict[str, Any] : 复位结果 {reset, was_tripped, restored_open, tripped, ...}
        """
        actor_s = (actor or "system").strip() or "system"
        reason_s = (reason or "人工复位").strip()

        was_tripped = self._tripped
        restored_open = False

        if self._tripped:
            # 回滚禁开仓（只回滚熔断自己造成的暂停）
            if self._risk is not None:
                try:
                    restored_open = bool(
                        self._risk.resume_from_circuit_breaker(reason_s)
                    )
                except Exception as e:  # noqa: BLE001 - 恢复失败留痕不阻断复位
                    logger.error(f"[circuit_breaker] 恢复风控开仓失败: {e}")
            self._tripped = False
            self._reset_count += 1

        # 复位留痕 + INFO 告警
        self._audit_log(
            ACTION_RESET,
            actor=actor_s,
            detail={
                "reason": reason_s,
                "was_tripped": was_tripped,
                "restored_open": restored_open,
                "prior_reason": self._reason,
            },
        )
        self._send_alert(
            SeverityLevel.INFO,
            f"[P2] 熔断复位: {reason_s}",
            details={"actor": actor_s, "was_tripped": was_tripped,
                     "restored_open": restored_open},
        )

        result: Dict[str, Any] = {
            "reset": True,
            "was_tripped": was_tripped,
            "restored_open": restored_open,
            "tripped": self._tripped,
            "can_open_position": self._current_open_allowed(),
            "actor": actor_s,
            "reason": reason_s,
        }
        logger.info(f"[circuit_breaker] 熔断复位: {result}")

        # 复位后清理触发上下文（动作历史保留供观测）
        if was_tripped:
            self._triggered_at = 0.0
            self._reason = ""
            self._trigger_event_id = ""
        return result

    # ═══════════════════════════════════════════
    #  动作链
    # ═══════════════════════════════════════════

    def _act_disable_open(self) -> None:
        """禁开仓：联动 RiskManager 暂停（可回滚）；shadow 不真暂停。"""
        if self._risk is None:
            self._record_action(
                ACTION_DISABLE_OPEN, True, {"skipped": "no_risk_manager"})
            return
        if self._config.shadow:
            self._record_action(
                ACTION_DISABLE_OPEN, True,
                {"shadow": True, "note": "演练：不真暂停风控"})
            return
        try:
            blocked_now = bool(
                self._risk.pause_for_circuit_breaker(self._reason)
            )
        except Exception as e:  # noqa: BLE001 - 单步失败不阻断动作链
            logger.error(f"[circuit_breaker] 禁开仓失败: {e}")
            self._record_action(ACTION_DISABLE_OPEN, False, {"error": str(e)})
            return
        self._record_action(
            ACTION_DISABLE_OPEN, True,
            {"blocked_now": blocked_now,
             "can_open_position": self._current_open_allowed()})

    def _act_cancel_orders(self) -> None:
        """撤全部挂单：遍历 get_active_orders + cancel_order，单笔异常不阻断。"""
        if self._broker is None:
            self._record_action(
                ACTION_CANCEL_ORDERS, True, {"skipped": "no_broker"})
            return
        if self._config.shadow:
            self._record_action(
                ACTION_CANCEL_ORDERS, True,
                {"shadow": True, "note": "演练：不真撤单"})
            return

        get_active = getattr(self._broker, "get_active_orders", None)
        if not callable(get_active):
            self._record_action(
                ACTION_CANCEL_ORDERS, True,
                {"skipped": "broker_unsupported_get_active_orders"})
            return
        try:
            orders = list(get_active() or [])
        except Exception as e:  # noqa: BLE001 - 查询失败记录并继续
            logger.warning(f"[circuit_breaker] 活跃挂单查询失败: {e}")
            self._record_action(ACTION_CANCEL_ORDERS, False, {"error": str(e)})
            return

        cancelled = 0
        failed = 0
        for order in orders:
            oid = str(getattr(order, "order_id", "") or "")
            try:
                ok = bool(self._broker.cancel_order(oid))
                if ok:
                    cancelled += 1
                else:
                    failed += 1
            except Exception as e:  # noqa: BLE001 - 单笔失败不阻断其余
                failed += 1
                logger.warning(f"[circuit_breaker] 撤单失败 {oid}: {e}")
        self._record_action(
            ACTION_CANCEL_ORDERS, failed == 0,
            {"total": len(orders), "cancelled": cancelled, "failed": failed})

    def _act_delever(self) -> None:
        """
        降杠杆：若 broker 暴露 set_leverage 则联动降到 DELEVER_TARGET_RATIO；
        当前系统无现成降杠杆落地接口，仅记录动作并注明（留接口）。
        """
        setter = (
            getattr(self._broker, "set_leverage", None)
            if self._broker is not None else None
        )
        if not callable(setter):
            self._record_action(
                ACTION_DELEVER, True,
                {"landed": False,
                 "note": "当前系统无降杠杆落地接口（broker 未暴露 set_leverage），预留"})
            return
        if self._config.shadow:
            self._record_action(
                ACTION_DELEVER, True,
                {"shadow": True, "target": DELEVER_TARGET_RATIO})
            return
        try:
            setter(DELEVER_TARGET_RATIO)
        except Exception as e:  # noqa: BLE001 - 单步失败不阻断
            logger.error(f"[circuit_breaker] 降杠杆失败: {e}")
            self._record_action(ACTION_DELEVER, False, {"error": str(e)})
            return
        self._record_action(
            ACTION_DELEVER, True,
            {"landed": True, "target": DELEVER_TARGET_RATIO})

    def _act_kill_switch(self) -> None:
        """Kill Switch：撤单 + 禁开仓达成即生效，记录可观测状态。"""
        self._record_action(
            ACTION_KILL_SWITCH, True,
            {"engaged": True, "shadow": self._config.shadow,
             "can_open_position": self._current_open_allowed()})

    def _act_liquidate(self) -> None:
        """
        自动全平（仅 auto_liquidate=True 时调用）：联动 EmergencyLiquidationService。
        shadow 模式强制 dry_run（不真实下单）。
        """
        svc = self._liquidation
        if svc is None:
            if self._broker is None:
                self._record_action(
                    ACTION_LIQUIDATE, False, {"skipped": "no_service_no_broker"})
                return
            from myquant.live.emergency_liquidation import (
                EmergencyLiquidationService,
            )
            svc = EmergencyLiquidationService(self._broker)

        confirm = self._config.liquidate_confirm and not self._config.shadow
        try:
            report = svc.execute(confirm=confirm)
        except Exception as e:  # noqa: BLE001 - 全平失败不阻断熔断留痕
            logger.error(f"[circuit_breaker] 自动全平异常: {e}")
            self._record_action(ACTION_LIQUIDATE, False, {"error": str(e)})
            return
        self._record_action(
            ACTION_LIQUIDATE, bool(report.get("ok")),
            {"mode": report.get("mode"), "executed": report.get("executed"),
             "cancelled_orders": len(report.get("cancelled_orders", []))})

    # ═══════════════════════════════════════════
    #  留痕 / 告警 / 观测
    # ═══════════════════════════════════════════

    def _record_action(
        self, action: str, ok: bool, detail: Dict[str, Any]
    ) -> None:
        """记录一个自动动作（动作历史 + 审计 append）。"""
        entry: Dict[str, Any] = {
            "action": action,
            "ok": ok,
            "timestamp": time.time(),  # 记录时刻（当前时点）
            "detail": detail,
        }
        self._actions.append(entry)
        self._audit_log(action, actor="circuit_breaker", detail=detail)

    def _audit_log(
        self, action: str, actor: str, detail: Dict[str, Any]
    ) -> None:
        """审计 append（kind="circuit"）；审计失败记 error 不阻断主流程。"""
        if self._audit is None:
            logger.debug(f"[circuit_breaker] 审计(无库) {action}: {detail}")
            return
        try:
            self._audit.append(
                kind=AUDIT_KIND, action=action, actor=actor, detail=detail)
        except Exception as e:  # noqa: BLE001 - 审计失败不阻断
            logger.error(f"[circuit_breaker] 审计追加失败 action={action}: {e}")

    def _send_alert(
        self, level: SeverityLevel, message: str, details: Dict[str, Any]
    ) -> None:
        """发送告警；告警管理器缺失则降级为 logger，不阻断动作链。"""
        if self._alert is None:
            logger.log(
                level.value == "critical" and 50 or 20,
                f"[circuit_breaker][alert:{level.value}] {message} {details}")
            return
        try:
            self._alert.alert(
                level=level, message=message, source="circuit_breaker",
                category="risk", details=details,
                deduplication_key=f"circuit:{level.value}:{self._reason[:32] or 'trip'}",
            )
        except Exception as e:  # noqa: BLE001 - 告警失败不阻断
            logger.error(f"[circuit_breaker] 告警发送失败: {e}")

    def _current_open_allowed(self) -> bool:
        """当前是否允许新开仓（熔断态 = 不允许；否则问风控）。"""
        if self._tripped:
            return False
        if self._risk is not None and hasattr(self._risk, "can_open_position"):
            try:
                return bool(self._risk.can_open_position())
            except Exception:  # noqa: BLE001 - 查询异常保守放行由风控兜底
                return True
        return True

    # ═══════════════════════════════════════════
    #  依赖挂载（供 API 端点 / 测试注入）
    # ═══════════════════════════════════════════

    def attach_audit_store(self, audit_store: Any) -> None:
        """挂载审计库（API 端点从服务容器注入临时/全局审计库）。"""
        self._audit = audit_store

    def attach_alert_manager(self, alert_manager: Any) -> None:
        """挂载告警管理器。"""
        self._alert = alert_manager

    # ═══════════════════════════════════════════
    #  可观测状态
    # ═══════════════════════════════════════════

    @property
    def tripped(self) -> bool:
        """是否处于熔断状态。"""
        return self._tripped

    @property
    def triggered_at(self) -> float:
        """触发时间戳（取已发生事件时刻，防未来）。"""
        return self._triggered_at

    def to_dict(self) -> Dict[str, Any]:
        """
        可观测快照。

        Returns
        -------
        Dict[str, Any] : {tripped, triggered_at, reason, actions, ...}
        """
        return {
            "tripped": self._tripped,
            "triggered_at": self._triggered_at,
            "reason": self._reason,
            "trigger_event_id": self._trigger_event_id,
            "shadow": self._config.shadow,
            "auto_liquidate": self._config.auto_liquidate,
            "trip_count": self._trip_count,
            "reset_count": self._reset_count,
            "can_open_position": self._current_open_allowed(),
            "actions": list(self._actions),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"CircuitBreaker(tripped={self._tripped}, "
            f"trip_count={self._trip_count}, reset_count={self._reset_count}, "
            f"shadow={self._config.shadow})"
        )


# ═══════════════════════════════════════════════
#  全局单例（供 API 端点 / 实盘引擎复用同一实例）
# ═══════════════════════════════════════════════

_global_circuit_breaker: Optional[CircuitBreaker] = None


def get_event_bus_safe() -> Optional[EventBus]:
    """安全获取全局事件总线（不可用返回 None，不抛）。"""
    try:
        from myquant.core.event_bus import get_event_bus
        return get_event_bus()
    except Exception as e:  # noqa: BLE001 - 无总线时降级不阻断
        logger.warning(f"[circuit_breaker] 全局事件总线不可用: {e}")
        return None


def get_circuit_breaker() -> CircuitBreaker:
    """
    获取全局熔断线单例（默认不接券商/风控，仅状态机 + 告警）。

    实盘进程应通过服务容器注册带真实 broker/risk_manager 的实例；
    API 端点优先使用容器内实例，缺失时回退本默认实例（复位仍可用）。
    """
    global _global_circuit_breaker
    if _global_circuit_breaker is None:
        try:
            from myquant.monitoring.alerting import get_alert_manager
            am: Optional[Any] = get_alert_manager()
        except Exception as e:  # noqa: BLE001 - 告警缺失降级
            logger.warning(f"[circuit_breaker] 告警管理器不可用: {e}")
            am = None
        _global_circuit_breaker = CircuitBreaker(
            broker=None, risk_manager=None, event_bus=None,
            alert_manager=am, audit_store=None,
        )
    return _global_circuit_breaker


def reset_circuit_breaker_singleton() -> None:
    """重置全局单例（测试隔离用）。"""
    global _global_circuit_breaker
    _global_circuit_breaker = None


# ═══════════════════════════════════════════════
#  自测（python -m myquant.monitoring.circuit_breaker）
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """独立自测：桩 broker/风控/告警/审计跑通触发→动作链→复位全链路，0=通过。"""
    import tempfile
    from pathlib import Path

    from myquant.core.event_bus import EventBus
    from myquant.database.connection import DatabaseConnection
    from myquant.core.audit_log import AuditLogStore
    from myquant.risk.risk_manager import RiskManager

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

    class _FakeOrder:
        def __init__(self, oid: str, sym: str) -> None:
            self.order_id = oid
            self.symbol = sym

    class _FakeBroker:
        def __init__(self) -> None:
            self.is_connected = True
            self.cancelled: List[str] = []

        def get_active_orders(self, symbol=None):
            return [_FakeOrder("o1", "000001.SZ"), _FakeOrder("o2", "000002.SZ")]

        def cancel_order(self, oid: str) -> bool:
            self.cancelled.append(oid)
            return True

    class _FakeAlert:
        def __init__(self) -> None:
            self.levels: List[Any] = []

        def alert(self, level, message, source="", category="",
                  details=None, deduplication_key="", runbook_id="") -> int:
            self.levels.append(level)
            return 1

    tmp = tempfile.TemporaryDirectory()
    conn = DatabaseConnection(str(Path(tmp.name) / "cb.db"))
    try:
        audit = AuditLogStore(connection=conn)
        broker = _FakeBroker()
        risk = RiskManager()
        am = _FakeAlert()
        bus = EventBus()

        cb = CircuitBreaker(
            broker=broker, risk_manager=risk, event_bus=bus,
            alert_manager=am, audit_store=audit,
        )
        cb.subscribe_to_event_bus(bus)

        # 触发前可开仓
        check("触发前可开仓", risk.can_open_position())
        # 注入风控击穿事件（事件路径，不等周期）
        bus.publish(Event(
            event_type=EventType.RISK_VIOLATION,
            data={"reason": "账户回撤击穿阈值"}, source="test",
        ))
        check("事件触发后熔断生效", cb.tripped)
        check("禁开仓生效", not risk.can_open_position())
        check("撤全部挂单", broker.cancelled == ["o1", "o2"], f"{broker.cancelled}")
        check("触发为 CRITICAL 告警", SeverityLevel.CRITICAL in am.levels,
              f"{[l.value for l in am.levels]}")
        check("审计有 circuit 记录",
              len(audit.query(kind="circuit", limit=50)) >= 3)

        # 复位（可回滚）
        result = cb.reset(actor="admin", reason="风险解除")
        check("复位成功", result["reset"] and result["was_tripped"])
        check("复位后开仓恢复", risk.can_open_position(),
              f"{risk.can_open_position()}")
        check("复位有审计 reset",
              any(r["action"] == "reset" for r in audit.query(kind="circuit", limit=50)))
        check("复位为 INFO 告警", SeverityLevel.INFO in am.levels)

        # shadow 演练不真动单
        broker2 = _FakeBroker()
        risk2 = RiskManager()
        cb_shadow = CircuitBreaker(
            broker=broker2, risk_manager=risk2, event_bus=None,
            alert_manager=_FakeAlert(), audit_store=None,
            config=CircuitBreakerConfig(shadow=True),
        )
        cb_shadow.trip(reason="演练：风控击穿")
        check("shadow 仍记录触发", cb_shadow.tripped)
        check("shadow 不真撤单", broker2.cancelled == [], f"{broker2.cancelled}")
        check("shadow 不真禁开仓", risk2.can_open_position())

        # 防未来：triggered_at 取事件时间戳
        past = 1_700_000_000.0
        cb3 = CircuitBreaker()
        ev = Event(event_type=EventType.RISK_VIOLATION,
                   data={"reason": "防未来"}, timestamp=past)
        cb3.trip(reason="防未来", source_event=ev)
        check("防未来：triggered_at=事件时刻", cb3.triggered_at == past,
              f"{cb3.triggered_at}")
    finally:
        conn.close_all()
        tmp.cleanup()

    logger.info("自测结果: %d 通过, %d 失败", passed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    raise SystemExit(_run_self_test())
