# 文件名：myquant/risk/guardian.py
"""
guardian.py - 风控守护
=======================
风控守护进程，负责健康监控、强制平仓、风控告警、守护决策。

核心能力:
  1. 健康监控 — 检查数据/账户/引擎健康状态
  2. 守护决策 — 根据RiskManager状态做出守护决策（告警/暂停/强制平仓）
  3. 强制平仓 — 严重风控事件时强制平仓接口（v1只记录，不实际执行）
  4. 风控告警 — 风控事件告警（v1记录日志，后续对接邮件/短信/飞书）
  5. 与RiskManager集成 — 读取风控状态/事件，做出守护决策

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（守护循环/自动平仓/多渠道告警全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前已知的风控状态/账户/持仓数据

防未来函数说明:
  - 健康检查只使用当前已知的系统状态，不预判未来
  - 守护决策只基于当前风控状态和已发生的事件
  - 强制平仓只基于当前市场价，不预判未来价格
  - 告警只基于已发生的风控事件，不预判未来风险

v1实现范围（简化骨架）:
  - RiskGuardian核心类
  - 健康检查（check_health）— 数据/账户/引擎状态检查
  - 守护决策（evaluate）— 根据风控状态做出决策
  - 强制平仓接口（force_close_all / force_close_symbol）— v1只记录日志
  - 风控告警（alert）— v1记录日志，后续对接通知渠道
  - 与RiskManager集成
  - 守护决策历史记录
  - 配置加载

TODO(后续迭代):
  - 守护循环（独立线程，定时检查风控状态）
  - 自动强制平仓（严重风控事件时自动执行，对接broker）
  - 多渠道告警（邮件/短信/飞书/钉钉/Webhook）
  - 数据健康监控（数据延迟/断连/缺失检测）
  - 账户健康监控（资金异常/持仓异常/连接断开）
  - 引擎健康监控（策略异常/死循环/内存溢出）
  - 熔断机制（连续亏损/连续风控拒绝自动暂停）
  - 风控仪表盘（实时健康状态/决策历史展示）
  - 与DataQualityEvent联动（脏数据严重时触发守护）
  - 守护决策持久化（写入文件/数据库）

依赖: typing, dataclasses, time, enum, myquant.risk.risk_manager, myquant.core.logger
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.risk.risk_manager import RiskManager, RiskStatus

logger = get_logger("risk.guardian")


# ═══════════════════════════════════════════════
#  健康状态枚举
# ═══════════════════════════════════════════════

class HealthStatus(Enum):
    """系统健康状态"""
    HEALTHY = "HEALTHY"      # 健康：所有检查通过
    DEGRADED = "DEGRADED"    # 降级：部分检查失败，但可继续运行
    UNHEALTHY = "UNHEALTHY"  # 不健康：严重问题，应暂停交易


# ═══════════════════════════════════════════════
#  告警级别
# ═══════════════════════════════════════════════

class AlertLevel(Enum):
    """告警级别"""
    INFO = "INFO"        # 信息：正常运行信息
    WARNING = "WARNING"  # 警告：需要关注，但不影响运行
    CRITICAL = "CRITICAL"  # 严重：需要立即处理，可能需要暂停/平仓


# ═══════════════════════════════════════════════
#  守护决策类型
# ═══════════════════════════════════════════════

class DecisionType(Enum):
    """守护决策类型"""
    NO_ACTION = "no_action"          # 无操作：一切正常
    ALERT = "alert"                  # 告警：发出告警但不干预
    PAUSE = "pause"                  # 暂停：暂停新开仓
    RESUME = "resume"                # 恢复：恢复交易
    FORCE_CLOSE_ALL = "force_close_all"      # 强制平仓所有
    FORCE_CLOSE_SYMBOL = "force_close_symbol"  # 强制平仓指定标的


# ═══════════════════════════════════════════════
#  守护决策
# ═══════════════════════════════════════════════

@dataclass
class GuardianDecision:
    """
    守护决策

    Attributes
    ----------
    timestamp   : 决策时间戳
    decision_type : 决策类型
    severity    : 严重程度
    reason      : 决策原因
    details     : 详细信息
    executed    : 是否已执行（v1强制平仓只记录，不实际执行）
    """
    timestamp: float = field(default_factory=time.time)
    decision_type: DecisionType = DecisionType.NO_ACTION
    severity: AlertLevel = AlertLevel.INFO
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    executed: bool = False

    def __repr__(self) -> str:
        return (
            f"GuardianDecision({self.decision_type.value}, "
            f"severity={self.severity.value}, reason={self.reason[:30]})"
        )


# ═══════════════════════════════════════════════
#  健康检查结果
# ═══════════════════════════════════════════════

@dataclass
class HealthCheckResult:
    """
    健康检查结果

    Attributes
    ----------
    status  : 整体健康状态
    checks  : 各检查项结果 {检查名: 是否通过}
    issues  : 问题列表
    """
    status: HealthStatus = HealthStatus.HEALTHY
    checks: Dict[str, bool] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)

    def add_check(self, name: str, passed: bool, issue: str = "") -> None:
        """添加检查项"""
        self.checks[name] = passed
        if not passed and issue:
            self.issues.append(issue)
        self._recalculate_status()

    def _recalculate_status(self) -> None:
        """重新计算整体状态"""
        if not self.checks:
            self.status = HealthStatus.HEALTHY
            return
        failed = [n for n, p in self.checks.items() if not p]
        if not failed:
            self.status = HealthStatus.HEALTHY
        elif len(failed) <= len(self.checks) // 3:
            self.status = HealthStatus.DEGRADED
        else:
            self.status = HealthStatus.UNHEALTHY

    def __repr__(self) -> str:
        return f"HealthCheckResult(status={self.status.value}, checks={len(self.checks)}, issues={len(self.issues)})"


# ═══════════════════════════════════════════════
#  准入守卫注册表（G1 EX-11C）
# ═══════════════════════════════════════════════
# 高危写操作准入（KP-M04）：guardian 不再延迟导入 api.middleware.auth（L3→L6 反向），
# 改为由合法层（L6 api/web 装配器）经 register_operation_guard 注入守卫实例。
# 未装配时默认放行并记录 decision（与 OperationGuard 未启用时"仅审计留痕"语义一致）。
_OPERATION_GUARD: Optional[Any] = None


def register_operation_guard(guard: Any) -> None:
    """注册高危写操作准入守卫实例（装配层注入）。

    Args:
        guard: 准入守卫实例，须实现
            ``check(scope, operator, reviewer=None, action="") -> (ok, reason)``
            （如 api.middleware.auth.OperationGuard）。
    """
    global _OPERATION_GUARD
    _OPERATION_GUARD = guard
    logger.info("[guardian] 高危写操作准入守卫已装配: %s", type(guard).__name__)


# ═══════════════════════════════════════════════
#  风控守护
# ═══════════════════════════════════════════════

class RiskGuardian:
    """
    风控守护

    负责健康监控、守护决策、强制平仓、风控告警。
    v1为简化骨架：提供完整接口和决策逻辑，强制平仓只记录不执行，
    守护循环由后续迭代实现（独立线程定时调用evaluate）。

    使用方式:
        from myquant.risk import RiskManager, RiskGuardian, RiskContext

        # 初始化
        risk_mgr = RiskManager()
        guardian = RiskGuardian(risk_mgr)

        # 每根bar后调用守护决策
        context = RiskContext(...)
        decision = guardian.evaluate(context)
        if decision.decision_type == DecisionType.PAUSE:
            risk_mgr.pause(decision.reason)
        elif decision.decision_type == DecisionType.FORCE_CLOSE_ALL:
            # v1只记录，后续迭代对接broker执行
            guardian.force_close_all(decision.reason)

        # 健康检查
        health = guardian.check_health(context)
        if health.status == HealthStatus.UNHEALTHY:
            guardian.force_close_all("系统不健康，强制平仓")

    与实盘引擎集成:
        # engine.py 中
        self.guardian = RiskGuardian(self.risk_manager)

        def on_bar(self, bar):
            # 1. 风控持仓监控
            warnings = self.risk_manager.check_positions(context)
            # 2. 守护决策
            decision = self.guardian.evaluate(context)
            # 3. 执行守护决策
            self._execute_guardian_decision(decision)
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        config: Optional[Dict[str, Any]] = None,
    ):
        """
        初始化风控守护

        Parameters
        ----------
        risk_manager : RiskManager
            风控管理器（守护读取其状态/事件）
        config : Dict | None
            守护配置
        """
        self._risk_manager = risk_manager
        self._config = config or {}

        # 守护配置（默认值）
        self._max_consecutive_rejections = self._config.get("max_consecutive_rejections", 10)
        self._force_close_on_unhealthy = self._config.get("force_close_on_unhealthy", True)
        self._alert_on_warning = self._config.get("alert_on_warning", True)
        self._auto_pause_on_critical = self._config.get("auto_pause_on_critical", True)

        # 守护决策历史
        self._decisions: List[GuardianDecision] = []

        # 告警历史
        self._alerts: List[Dict[str, Any]] = []

        # 连续拒绝计数（熔断用）
        self._consecutive_rejections = 0

        # 健康状态缓存
        self._last_health: Optional[HealthCheckResult] = None

        # 强制平仓回调（后续迭代对接broker，v1为None）
        self._force_close_callback: Optional[Callable[[str, Optional[str]], bool]] = None

        logger.info(
            f"风控守护初始化: "
            f"max_consecutive_rejections={self._max_consecutive_rejections}, "
            f"force_close_on_unhealthy={self._force_close_on_unhealthy}"
        )

    # ═══════════════════════════════════════════
    #  健康检查
    # ═══════════════════════════════════════════

    def check_health(self, context: Optional[Any] = None) -> HealthCheckResult:
        """
        健康检查

        检查系统各组件健康状态，返回整体健康状态。
        v1检查项：风控状态、账户资金、持仓数据、数据时效性。

        Parameters
        ----------
        context : RiskContext | None
            风控上下文（包含账户/持仓/行情数据）

        Returns
        -------
        HealthCheckResult : 健康检查结果
        """
        result = HealthCheckResult()

        # 1. 风控状态检查
        risk_status = self._risk_manager.get_status()
        risk_healthy = risk_status in (RiskStatus.NORMAL, RiskStatus.WARNING)
        result.add_check(
            "risk_status",
            risk_healthy,
            f"风控状态异常: {risk_status.value}" if not risk_healthy else "",
        )

        # 2. 账户资金检查（如果有context）
        if context and hasattr(context, "cash"):
            cash_ok = context.cash >= 0
            result.add_check(
                "account_cash",
                cash_ok,
                f"账户资金异常: cash={context.cash}" if not cash_ok else "",
            )

            # 总资产检查
            if hasattr(context, "total_assets"):
                assets_ok = context.total_assets > 0
                result.add_check(
                    "total_assets",
                    assets_ok,
                    f"总资产异常: total_assets={context.total_assets}" if not assets_ok else "",
                )

        # 3. 持仓数据检查（如果有context）
        if context and hasattr(context, "positions"):
            positions_valid = True
            for pos in context.positions:
                if pos.get("quantity", 0) == 0:
                    continue
                if pos.get("market_price", 0) <= 0:
                    positions_valid = False
                    break
                if pos.get("avg_cost", 0) <= 0:
                    positions_valid = False
                    break
            result.add_check(
                "positions_data",
                positions_valid,
                "持仓数据异常（价格/成本为0）" if not positions_valid else "",
            )

        # 4. 连续拒绝检查（熔断）
        rejection_ok = self._consecutive_rejections < self._max_consecutive_rejections
        result.add_check(
            "consecutive_rejections",
            rejection_ok,
            f"连续风控拒绝过多: {self._consecutive_rejections}/{self._max_consecutive_rejections}"
            if not rejection_ok else "",
        )

        # 5. 风控事件检查（最近是否有严重事件）
        recent_events = self._risk_manager.get_recent_events(count=5)
        has_critical = any(
            e.event_type.value in ("order_rejected", "status_change")
            and "PAUSED" in (e.reason or "")
            for e in recent_events
        )
        result.add_check(
            "recent_events",
            not has_critical,
            "最近有严重风控事件（PAUSED）" if has_critical else "",
        )

        self._last_health = result

        if result.status != HealthStatus.HEALTHY:
            logger.warning(f"[guardian] 健康检查: {result.status.value}, issues={result.issues}")

        return result

    # ═══════════════════════════════════════════
    #  守护决策（核心）
    # ═══════════════════════════════════════════

    def evaluate(self, context: Optional[Any] = None) -> GuardianDecision:
        """
        守护决策（核心方法）

        根据风控状态、健康检查结果、连续拒绝计数，做出守护决策。
        决策优先级：强制平仓 > 暂停 > 告警 > 无操作。

        决策逻辑:
          1. 健康检查 → UNHEALTHY 且配置允许 → 强制平仓所有
          2. 连续拒绝超阈值 → 暂停（熔断）
          3. 风控状态 PAUSED → 告警（已暂停，提醒人工处理）
          4. 风控状态 WARNING → 告警
          5. 健康检查 DEGRADED → 告警
          6. 一切正常 → 无操作

        Parameters
        ----------
        context : RiskContext | None
            风控上下文

        Returns
        -------
        GuardianDecision : 守护决策
        """
        # 1. 健康检查
        health = self.check_health(context)

        # 2. 决策逻辑（优先级从高到低）

        # 2.1 不健康 → 强制平仓所有
        if health.status == HealthStatus.UNHEALTHY and self._force_close_on_unhealthy:
            decision = GuardianDecision(
                decision_type=DecisionType.FORCE_CLOSE_ALL,
                severity=AlertLevel.CRITICAL,
                reason=f"系统不健康，强制平仓所有持仓: {'; '.join(health.issues)}",
                details={"health": health.status.value, "issues": health.issues},
            )
            self._record_decision(decision)
            self.alert(AlertLevel.CRITICAL, decision.reason)
            return decision

        # 2.2 连续拒绝超阈值 → 暂停（熔断）
        if self._consecutive_rejections >= self._max_consecutive_rejections:
            decision = GuardianDecision(
                decision_type=DecisionType.PAUSE,
                severity=AlertLevel.CRITICAL,
                reason=f"连续风控拒绝{self._consecutive_rejections}次，触发熔断暂停",
                details={"consecutive_rejections": self._consecutive_rejections},
            )
            self._record_decision(decision)
            self.alert(AlertLevel.CRITICAL, decision.reason)
            # 自动暂停风控
            if self._auto_pause_on_critical:
                self._risk_manager.pause(decision.reason)
            return decision

        # 2.3 风控状态 PAUSED → 告警
        risk_status = self._risk_manager.get_status()
        if risk_status == RiskStatus.PAUSED:
            decision = GuardianDecision(
                decision_type=DecisionType.ALERT,
                severity=AlertLevel.CRITICAL,
                reason=f"风控状态为PAUSED: {self._risk_manager.get_status_reason()}",
                details={"risk_status": risk_status.value},
            )
            self._record_decision(decision)
            self.alert(AlertLevel.CRITICAL, decision.reason)
            return decision

        # 2.4 风控状态 WARNING → 告警
        if risk_status == RiskStatus.WARNING:
            decision = GuardianDecision(
                decision_type=DecisionType.ALERT,
                severity=AlertLevel.WARNING,
                reason=f"风控状态为WARNING: {self._risk_manager.get_status_reason()}",
                details={"risk_status": risk_status.value},
            )
            self._record_decision(decision)
            if self._alert_on_warning:
                self.alert(AlertLevel.WARNING, decision.reason)
            return decision

        # 2.5 健康检查 DEGRADED → 告警
        if health.status == HealthStatus.DEGRADED:
            decision = GuardianDecision(
                decision_type=DecisionType.ALERT,
                severity=AlertLevel.WARNING,
                reason=f"系统降级: {'; '.join(health.issues)}",
                details={"health": health.status.value, "issues": health.issues},
            )
            self._record_decision(decision)
            self.alert(AlertLevel.WARNING, decision.reason)
            return decision

        # 2.6 一切正常 → 无操作
        decision = GuardianDecision(
            decision_type=DecisionType.NO_ACTION,
            severity=AlertLevel.INFO,
            reason="系统正常，无需干预",
            details={"health": health.status.value, "risk_status": risk_status.value},
        )
        self._record_decision(decision)
        return decision

    # ═══════════════════════════════════════════
    #  强制平仓（v1只记录，不实际执行）
    # ═══════════════════════════════════════════

    def force_close_all(self, reason: str = "风控守护强制平仓") -> bool:
        """
        强制平仓所有持仓

        v1：只记录决策和告警，不实际执行平仓。
        后续迭代：通过 _force_close_callback 对接 broker 执行。

        Parameters
        ----------
        reason : str
            平仓原因

        Returns
        -------
        bool : 是否执行成功（v1始终返回True，表示已记录）
        """
        decision = GuardianDecision(
            decision_type=DecisionType.FORCE_CLOSE_ALL,
            severity=AlertLevel.CRITICAL,
            reason=reason,
            executed=False,  # v1不实际执行
        )
        # KP-M04（T-30）：高危写操作准入——未装配守卫默认放行（guardian 仍记录 decision）
        guard = _OPERATION_GUARD
        if guard is None:
            logger.warning(
                "[guardian] 未装配准入守卫（register_operation_guard），强制平仓所有默认放行"
            )
            ok, why = True, ""
        else:
            ok, why = guard.check(
                "force_close", operator="guardian",
                action=f"force_close_all reason={reason}")
        if not ok:
            logger.error(f"[guardian] 强制平仓所有被准入拒绝: {why}")
            return False
        self._record_decision(decision)
        self.alert(AlertLevel.CRITICAL, f"[强制平仓所有] {reason}")

        # 如果有回调，执行回调（后续迭代对接broker）
        if self._force_close_callback:
            try:
                executed = self._force_close_callback("all", None)
                decision.executed = executed
                if executed:
                    logger.info(f"[guardian] 强制平仓所有已执行: {reason}")
                else:
                    logger.error(f"[guardian] 强制平仓所有执行失败: {reason}")
                return executed
            except Exception as e:
                logger.error(f"[guardian] 强制平仓回调异常: {e}")
                return False

        logger.warning(f"[guardian] 强制平仓所有（v1仅记录，未实际执行）: {reason}")
        return False  # KP-M02: 无回调不假成功（未实际平仓）

    def force_close_symbol(self, symbol: str, reason: str = "风控守护强制平仓") -> bool:
        """
        强制平仓指定标的

        v1：只记录决策和告警，不实际执行平仓。

        Parameters
        ----------
        symbol : str
            标的代码
        reason : str
            平仓原因

        Returns
        -------
        bool : 是否执行成功
        """
        decision = GuardianDecision(
            decision_type=DecisionType.FORCE_CLOSE_SYMBOL,
            severity=AlertLevel.CRITICAL,
            reason=f"{symbol}: {reason}",
            details={"symbol": symbol},
            executed=False,
        )
        # KP-M04（T-30）：高危写操作准入——未装配守卫默认放行（guardian 仍记录 decision）
        guard = _OPERATION_GUARD
        if guard is None:
            logger.warning(
                "[guardian] 未装配准入守卫（register_operation_guard），强制平仓 %s 默认放行",
                symbol,
            )
            ok, why = True, ""
        else:
            ok, why = guard.check(
                "force_close", operator="guardian",
                action=f"force_close_symbol {symbol} reason={reason}")
        if not ok:
            logger.error(f"[guardian] 强制平仓 {symbol} 被准入拒绝: {why}")
            return False
        self._record_decision(decision)
        self.alert(AlertLevel.CRITICAL, f"[强制平仓 {symbol}] {reason}")

        if self._force_close_callback:
            try:
                executed = self._force_close_callback("symbol", symbol)
                decision.executed = executed
                return executed
            except Exception as e:
                logger.error(f"[guardian] 强制平仓回调异常: {e}")
                return False

        logger.warning(f"[guardian] 强制平仓 {symbol}（v1仅记录，未实际执行）: {reason}")
        return False  # KP-M02: 无回调不假成功（未实际平仓）

    def set_force_close_callback(self, callback: Callable[[str, Optional[str]], bool]) -> None:
        """
        设置强制平仓回调（后续迭代对接broker）

        Parameters
        ----------
        callback : Callable[[str, Optional[str]], bool]
            回调函数 signature: callback(close_type, symbol) -> bool
            close_type: "all" 或 "symbol"
            symbol: 标的代码（close_type="all"时为None）
        """
        self._force_close_callback = callback
        logger.info("[guardian] 强制平仓回调已设置")

    # ═══════════════════════════════════════════
    #  风控告警
    # ═══════════════════════════════════════════

    def alert(self, level: AlertLevel, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        """
        风控告警

        v1：记录日志和告警历史。
        后续迭代：对接邮件/短信/飞书/钉钉/Webhook多渠道告警。

        Parameters
        ----------
        level : AlertLevel
            告警级别
        message : str
            告警消息
        details : Dict | None
            详细信息
        """
        alert_record = {
            "timestamp": time.time(),
            "level": level.value,
            "message": message,
            "details": details or {},
        }
        self._alerts.append(alert_record)

        # 超过最大记录数则丢弃最早的
        if len(self._alerts) > 1000:
            self._alerts = self._alerts[-1000:]

        # v1：记录日志
        if level == AlertLevel.CRITICAL:
            logger.critical(f"[guardian][{level.value}] {message}")
        elif level == AlertLevel.WARNING:
            logger.warning(f"[guardian][{level.value}] {message}")
        else:
            logger.info(f"[guardian][{level.value}] {message}")

    def get_alerts(
        self,
        level: Optional[AlertLevel] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        获取告警历史

        Parameters
        ----------
        level : AlertLevel | None
            告警级别过滤（None=全部）
        limit : int
            返回最大数量

        Returns
        -------
        List[Dict] : 告警列表（按时间倒序）
        """
        alerts = self._alerts
        if level:
            alerts = [a for a in alerts if a["level"] == level.value]
        alerts = sorted(alerts, key=lambda a: a["timestamp"], reverse=True)
        return alerts[:limit]

    # ═══════════════════════════════════════════
    #  连续拒绝计数（熔断用）
    # ═══════════════════════════════════════════

    def record_rejection(self) -> None:
        """记录一次风控拒绝（engine在订单被拒时调用）"""
        self._consecutive_rejections += 1
        logger.debug(f"[guardian] 连续拒绝计数: {self._consecutive_rejections}")

    def record_acceptance(self) -> None:
        """记录一次订单通过（重置连续拒绝计数）"""
        if self._consecutive_rejections > 0:
            self._consecutive_rejections = 0
            logger.debug("[guardian] 连续拒绝计数已重置")

    @property
    def consecutive_rejections(self) -> int:
        """当前连续拒绝计数"""
        return self._consecutive_rejections

    # ═══════════════════════════════════════════
    #  守护决策历史
    # ═══════════════════════════════════════════

    def _record_decision(self, decision: GuardianDecision) -> None:
        """记录守护决策（内部方法）"""
        self._decisions.append(decision)
        if len(self._decisions) > 1000:
            self._decisions = self._decisions[-1000:]

    def get_decisions(
        self,
        decision_type: Optional[DecisionType] = None,
        limit: int = 100,
    ) -> List[GuardianDecision]:
        """
        获取守护决策历史

        Parameters
        ----------
        decision_type : DecisionType | None
            决策类型过滤（None=全部）
        limit : int
            返回最大数量

        Returns
        -------
        List[GuardianDecision] : 决策列表（按时间倒序）
        """
        decisions = self._decisions
        if decision_type:
            decisions = [d for d in decisions if d.decision_type == decision_type]
        decisions = sorted(decisions, key=lambda d: d.timestamp, reverse=True)
        return decisions[:limit]

    @property
    def decision_count(self) -> int:
        """决策总数"""
        return len(self._decisions)

    # ═══════════════════════════════════════════
    #  健康状态
    # ═══════════════════════════════════════════

    @property
    def last_health(self) -> Optional[HealthCheckResult]:
        """最近一次健康检查结果"""
        return self._last_health

    def get_health_status(self) -> HealthStatus:
        """获取当前健康状态（如果没有检查过，返回HEALTHY）"""
        if self._last_health:
            return self._last_health.status
        return HealthStatus.HEALTHY

    # ═══════════════════════════════════════════
    #  守护摘要
    # ═══════════════════════════════════════════

    def get_summary(self) -> Dict[str, Any]:
        """
        获取守护摘要

        Returns
        -------
        Dict[str, Any] :
            {
                "health_status": str,
                "risk_status": str,
                "consecutive_rejections": int,
                "decision_count": int,
                "alert_count": int,
                "recent_decisions": List,
                "recent_alerts": List,
            }
        """
        return {
            "health_status": self.get_health_status().value,
            "risk_status": self._risk_manager.get_status().value,
            "consecutive_rejections": self._consecutive_rejections,
            "decision_count": len(self._decisions),
            "alert_count": len(self._alerts),
            "recent_decisions": [
                {"timestamp": d.timestamp, "type": d.decision_type.value,
                 "severity": d.severity.value, "reason": d.reason[:50]}
                for d in self.get_decisions(limit=5)
            ],
            "recent_alerts": [
                {"timestamp": a["timestamp"], "level": a["level"],
                 "message": a["message"][:50]}
                for a in self.get_alerts(limit=5)
            ],
        }

    # ═══════════════════════════════════════════
    #  重置
    # ═══════════════════════════════════════════

    def reset(self) -> None:
        """重置守护（清空决策/告警/计数，健康状态）"""
        self._decisions.clear()
        self._alerts.clear()
        self._consecutive_rejections = 0
        self._last_health = None
        logger.info("[guardian] 风控守护已重置")

    def __repr__(self) -> str:
        return (
            f"RiskGuardian(health={self.get_health_status().value}, "
            f"risk={self._risk_manager.get_status().value}, "
            f"decisions={len(self._decisions)}, "
            f"rejections={self._consecutive_rejections})"
        )
