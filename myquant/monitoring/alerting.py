# 文件名：myquant/monitoring/alerting.py
"""
alerting.py - 告警通道管理（阶段9任务10增强：DataQualityEvent自动告警 + 分级去重 + 多通道）
========================================================================================
功能:
  1. Alert 告警数据类：级别、消息、来源、去重键、详情、升级/聚合字段
  2. AlertRule 告警规则配置：脏数据类型→级别映射、占比阈值→级别、级别→通道映射、聚合窗口、升级时间
  3. AlertChannel 抽象基类：统一告警发送接口
  4. 内置通道：ConsoleAlertChannel（控制台）、LogAlertChannel（日志）
  5. 邮件通道：EmailAlertChannel（SMTP实际发送，HTML/纯文本模板）
  6. Webhook通道：WebhookAlertChannel（HTTP POST，支持飞书/钉钉/企业微信/通用，重试机制）
  7. AlertAggregator 告警聚合器：同一标的+脏数据类型在窗口内聚合，流式支持
  8. AlertManager 管理器：
     - 智能分级：按AlertRule自动判定告警级别
     - 告警聚合：高频脏数据事件聚合为一条告警
     - 告警去重：时间窗口内相同key只发一次
     - 多通道路由：按级别选择通道组合
     - 告警升级：WARNING超时未确认自动升级为ERROR
     - 告警回调：发送告警时通知metrics_collector等组件
     - 静默模式：维护时间段不发送
     - 告警历史：记录、查询、确认、解决

阶段9任务10核心增强（接收DataQualityEvent自动告警）:
  - AlertRule 可配置告警规则（阈值/通道/聚合/升级）
  - AlertAggregator 流式聚合（实盘高频场景必备）
  - EmailAlertChannel 实际SMTP发送
  - WebhookAlertChannel 实际HTTP发送（飞书/钉钉/企业微信）
  - handle_data_quality_event() 智能分级 + 聚合 + 自动告警
  - handle_pipeline_complete_event() 按质量等级分级告警
  - check_escalations() 告警升级检查（scheduler定期调用）
  - on_alert_callbacks 告警发送回调（metrics联动）

阶段9任务12-C增强（与RiskManager联动）:
  - handle_strategy_pause_event() 处理策略暂停/恢复事件自动告警
  - 全局暂停（pause_level=global）→ CRITICAL
  - 单标的暂停（pause_level=symbol）→ ERROR
  - 恢复事件 → INFO（通知暂停已解除）
  - subscribe_to_event_bus() 增订阅 RISK_PAUSED / RISK_RESUMED

为实盘无缝对接预留:
  - 多通道并行发送（邮件+Webhook+钉钉+企业微信）
  - 流式聚合：实盘高频脏数据事件不刷屏
  - 告警升级：严重问题未及时处理自动升级
  - 告警回调：与metrics_collector/risk_manager联动
  - 与实盘引擎集成：交易异常/风控触发/断连自动告警
  - 与任务22 scheduler 集成：定期巡检 + 告警升级检查

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 告警只基于当前已发生的事件，不预测未来

防未来函数说明:
  - 告警只响应已经发生的事件（DataQualityEvent/系统事件）
  - 不使用任何预测模型推断未来可能发生的异常
  - 告警时间戳为事件发生/接收时刻的当前时间
  - 聚合只基于已收到的事件，不预加载未来数据
  - 升级只基于已存在告警的确认状态，不预测是否会被确认
"""

from __future__ import annotations

import json
import queue
import smtplib
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple
from urllib import request as urlrequest
from urllib.error import URLError

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger

logger = get_logger("monitoring.alerting")


# ═══════════════════════════════════════════════
#  策略暂停/恢复事件 → 告警级别映射（任务12-C，与RiskManager联动）
# ═══════════════════════════════════════════════
#  - global：全局暂停（整个策略停摆，禁止所有新开仓）→ CRITICAL
#  - symbol：单标的暂停（仅该标的禁止买入，范围受限）→ ERROR
#  - 恢复事件统一为 INFO（通知暂停已解除）
STRATEGY_PAUSE_ALERT_LEVEL_MAP: Dict[str, SeverityLevel] = {
    "global": SeverityLevel.CRITICAL,
    "symbol": SeverityLevel.ERROR,
}
STRATEGY_RESUME_ALERT_LEVEL: SeverityLevel = SeverityLevel.INFO


# ═══════════════════════════════════════════════
#  告警数据类
# ═══════════════════════════════════════════════

@dataclass
class Alert:
    """
    告警对象

    Attributes
    ----------
    alert_id : str
        告警唯一ID
    level : SeverityLevel
        告警级别（INFO/WARNING/ERROR/CRITICAL）
    message : str
        告警消息
    source : str
        告警来源（模块名/组件名）
    category : str
        告警分类（data_quality / system / risk / trade / connection）
    details : Dict[str, Any]
        详细信息
    deduplication_key : str
        去重键（相同 key 在时间窗口内只发送一次）
    timestamp : float
        告警时间戳
    acknowledged : bool
        是否已确认
    resolved : bool
        是否已解决
    escalated_from : SeverityLevel | None
        升级前的级别（None表示未升级）
    parent_alert_id : str | None
        父告警ID（升级告警关联原始告警）
    aggregation_count : int
        聚合的原始事件数量（聚合告警>1）
    """
    level: SeverityLevel
    message: str
    source: str = "monitoring"
    category: str = "system"
    details: Dict[str, Any] = field(default_factory=dict)
    deduplication_key: str = ""
    timestamp: float = field(default_factory=time.time)
    alert_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    acknowledged: bool = False
    resolved: bool = False
    escalated_from: Optional[SeverityLevel] = None
    parent_alert_id: Optional[str] = None
    aggregation_count: int = 1

    @property
    def level_name(self) -> str:
        return self.level.value

    @property
    def is_critical(self) -> bool:
        return self.level >= SeverityLevel.ERROR

    @property
    def is_escalated(self) -> bool:
        """是否为升级告警"""
        return self.escalated_from is not None

    @property
    def age_seconds(self) -> float:
        """告警存在时间（秒）"""
        return time.time() - self.timestamp

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "alert_id": self.alert_id,
            "level": self.level.value,
            "message": self.message,
            "source": self.source,
            "category": self.category,
            "details": self.details,
            "deduplication_key": self.deduplication_key,
            "timestamp": self.timestamp,
            "acknowledged": self.acknowledged,
            "resolved": self.resolved,
            "escalated_from": self.escalated_from.value if self.escalated_from else None,
            "parent_alert_id": self.parent_alert_id,
            "aggregation_count": self.aggregation_count,
        }

    def summary(self) -> str:
        """人类可读摘要"""
        parts = [f"[{self.level.value.upper()}] {self.source}: {self.message}"]
        if self.aggregation_count > 1:
            parts.append(f"(聚合{self.aggregation_count}条)")
        if self.is_escalated:
            parts.append(f"[升级自{self.escalated_from.value}]")
        return " ".join(parts)

    def __repr__(self) -> str:
        return f"Alert(id={self.alert_id}, level={self.level.value}, src={self.source})"


# ═══════════════════════════════════════════════
#  AlertRule 告警规则配置（阶段9任务10新增）
# ═══════════════════════════════════════════════

@dataclass
class AlertRule:
    """
    告警规则配置

    控制告警的分级、通道选择、聚合和升级行为。

    Attributes
    ----------
    dirty_type_levels : Dict[str, SeverityLevel]
        脏数据类型 → 最低告警级别映射（如 ohlc_logic_error → ERROR）
    bad_ratio_thresholds : List[Tuple[float, SeverityLevel]]
        脏数据占比阈值 → 告警级别（按阈值降序排列，如 [(0.5, CRITICAL), (0.1, ERROR), (0.05, WARNING)]）
    level_channels : Dict[SeverityLevel, List[str]]
        告警级别 → 通道名称列表（如 CRITICAL → ["email", "webhook", "console"]）
    aggregation_window : float
        聚合窗口（秒），同一标的+脏数据类型在此窗口内聚合为一条告警
    escalation_time : float
        告警升级时间（秒），WARNING级超过此时间未确认自动升级为ERROR
    escalation_enabled : bool
        是否启用告警升级
    default_level : SeverityLevel
        默认告警级别（未匹配规则时使用）
    min_bad_count_for_alert : int
        最少脏数据数才触发告警（低于此数不告警）
    auto_acknowledge_info : bool
        是否自动确认INFO级告警
    """
    dirty_type_levels: Dict[str, SeverityLevel] = field(default_factory=dict)
    bad_ratio_thresholds: List[Tuple[float, SeverityLevel]] = field(default_factory=list)
    level_channels: Dict[SeverityLevel, List[str]] = field(default_factory=dict)
    aggregation_window: float = 60.0
    escalation_time: float = 300.0
    escalation_enabled: bool = True
    default_level: SeverityLevel = SeverityLevel.WARNING
    min_bad_count_for_alert: int = 1
    auto_acknowledge_info: bool = True

    @classmethod
    def default_rule(cls) -> "AlertRule":
        """
        创建默认告警规则

        默认规则:
          - OHLC逻辑错误/负价格/买卖盘交叉 → ERROR
          - 价格跳变/成交量异常/零价格/零成交量 → WARNING
          - 脏数据占比 >=50% → CRITICAL, >=10% → ERROR, >=5% → WARNING
          - CRITICAL → 所有通道, ERROR → email+log+console, WARNING → log+console, INFO → console
          - 聚合窗口60秒, 升级时间5分钟
        """
        return cls(
            dirty_type_levels={
                "ohlc_logic_error": SeverityLevel.ERROR,
                "negative_price": SeverityLevel.ERROR,
                "negative_volume": SeverityLevel.ERROR,
                "bid_ask_cross": SeverityLevel.ERROR,
                "missing_symbol": SeverityLevel.ERROR,
                "invalid_timestamp": SeverityLevel.ERROR,
                "time_monotonic_error": SeverityLevel.ERROR,
                "duplicate_timestamp": SeverityLevel.WARNING,
                "duplicate_tick": SeverityLevel.WARNING,
                "price_jump": SeverityLevel.WARNING,
                "volume_anomaly": SeverityLevel.WARNING,
                "zero_price": SeverityLevel.WARNING,
                "zero_volume": SeverityLevel.INFO,
                "unknown": SeverityLevel.WARNING,
            },
            bad_ratio_thresholds=[
                (0.5, SeverityLevel.CRITICAL),
                (0.1, SeverityLevel.ERROR),
                (0.05, SeverityLevel.WARNING),
            ],
            level_channels={
                SeverityLevel.CRITICAL: ["console", "log", "email", "webhook"],
                SeverityLevel.ERROR: ["console", "log", "email"],
                SeverityLevel.WARNING: ["console", "log"],
                SeverityLevel.INFO: ["console"],
            },
            aggregation_window=60.0,
            escalation_time=300.0,
            escalation_enabled=True,
            default_level=SeverityLevel.WARNING,
            min_bad_count_for_alert=1,
            auto_acknowledge_info=True,
        )

    def get_level_for_dirty_type(self, dirty_type: str) -> SeverityLevel:
        """根据脏数据类型获取最低告警级别"""
        return self.dirty_type_levels.get(dirty_type, self.default_level)

    def get_level_for_bad_ratio(self, bad_ratio: float) -> Optional[SeverityLevel]:
        """根据脏数据占比获取告警级别（未达最低阈值返回None）"""
        for threshold, level in sorted(self.bad_ratio_thresholds, key=lambda x: -x[0]):
            if bad_ratio >= threshold:
                return level
        return None

    def get_channels_for_level(self, level: SeverityLevel) -> List[str]:
        """根据告警级别获取通道名称列表"""
        return self.level_channels.get(level, ["console", "log"])

    def should_alert(self, bad_count: int, bad_ratio: float, dirty_type: str = "") -> bool:
        """
        判断是否应该触发告警

        Parameters
        ----------
        bad_count : int
            脏数据数量
        bad_ratio : float
            脏数据占比
        dirty_type : str
            脏数据类型

        Returns
        -------
        bool : 是否应该告警
        """
        if bad_count < self.min_bad_count_for_alert:
            return False
        # 有明确脏数据类型级别映射的，直接告警
        if dirty_type and dirty_type in self.dirty_type_levels:
            return True
        # 否则检查占比阈值
        return self.get_level_for_bad_ratio(bad_ratio) is not None

    def determine_level(
        self,
        dirty_type: str = "",
        bad_ratio: float = 0.0,
        event_severity: str = "",
    ) -> SeverityLevel:
        """
        综合判定告警级别（取最高级别）

        Parameters
        ----------
        dirty_type : str
            脏数据类型
        bad_ratio : float
            脏数据占比
        event_severity : str
            事件本身的严重级别（info/warning/error/critical）

        Returns
        -------
        SeverityLevel : 综合判定的最高告警级别
        """
        candidates = [self.default_level]

        # 脏数据类型级别
        if dirty_type:
            candidates.append(self.get_level_for_dirty_type(dirty_type))

        # 占比阈值级别
        ratio_level = self.get_level_for_bad_ratio(bad_ratio)
        if ratio_level:
            candidates.append(ratio_level)

        # 事件本身级别
        if event_severity:
            sev_map = {
                "info": SeverityLevel.INFO,
                "warning": SeverityLevel.WARNING,
                "error": SeverityLevel.ERROR,
                "critical": SeverityLevel.CRITICAL,
            }
            if event_severity in sev_map:
                candidates.append(sev_map[event_severity])

        # 取最高级别
        return max(candidates, key=lambda x: x.level)


# ═══════════════════════════════════════════════
#  告警通道抽象基类
# ═══════════════════════════════════════════════

class AlertChannel(ABC):
    """
    告警通道抽象基类

    所有告警通道必须实现 send() 方法。
    通道可以设置最低告警级别（min_level），低于该级别的告警不会发送。
    """

    def __init__(self, name: str, min_level: SeverityLevel = SeverityLevel.INFO, enabled: bool = True):
        self.name = name
        self.min_level = min_level
        self.enabled = enabled
        self._sent_count = 0
        self._error_count = 0
        self._last_error: Optional[str] = None
        self._last_sent_time: float = 0.0

    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """发送告警，返回是否成功"""
        ...

    def can_send(self, alert: Alert) -> bool:
        """检查该通道是否应该发送此告警"""
        return self.enabled and alert.level >= self.min_level

    def _record_success(self) -> None:
        self._sent_count += 1
        self._last_sent_time = time.time()

    def _record_failure(self, error: str) -> None:
        self._error_count += 1
        self._last_error = error

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "min_level": self.min_level.value,
            "sent_count": self._sent_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "last_sent_time": self._last_sent_time,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name={self.name}, min={self.min_level.value})>"


# ═══════════════════════════════════════════════
#  内置告警通道：控制台
# ═══════════════════════════════════════════════

class ConsoleAlertChannel(AlertChannel):
    """
    控制台告警通道

    将告警输出到标准输出（带颜色），适用于开发/调试环境。
    """

    _COLORS = {
        SeverityLevel.INFO: "\033[36m",
        SeverityLevel.WARNING: "\033[33m",
        SeverityLevel.ERROR: "\033[31m",
        SeverityLevel.CRITICAL: "\033[35m",
    }
    _RESET = "\033[0m"

    def __init__(self, name: str = "console", min_level: SeverityLevel = SeverityLevel.INFO, use_color: bool = True):
        super().__init__(name=name, min_level=min_level)
        self.use_color = use_color

    def send(self, alert: Alert) -> bool:
        try:
            color = self._COLORS.get(alert.level, "") if self.use_color else ""
            reset = self._RESET if self.use_color else ""
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert.timestamp))
            print(f"{color}[{timestamp}] {alert.summary()}{reset}")
            if alert.details:
                print(f"{color}  详情: {alert.details}{reset}")
            self._record_success()
            return True
        except Exception as e:
            self._record_failure(str(e))
            logger.error(f"控制台告警发送失败: {e}")
            return False


# ═══════════════════════════════════════════════
#  内置告警通道：日志
# ═══════════════════════════════════════════════

class LogAlertChannel(AlertChannel):
    """
    日志告警通道

    将告警写入日志文件（使用项目统一日志系统），适用于所有环境。
    """

    def __init__(self, name: str = "log", min_level: SeverityLevel = SeverityLevel.WARNING, logger_name: str = "monitoring.alerts"):
        super().__init__(name=name, min_level=min_level)
        self._alert_logger = get_logger(logger_name)

    def send(self, alert: Alert) -> bool:
        try:
            log_msg = f"[{alert.category}] {alert.summary()}"
            if alert.level == SeverityLevel.INFO:
                self._alert_logger.info(log_msg, extra={"alert_details": alert.details})
            elif alert.level == SeverityLevel.WARNING:
                self._alert_logger.warning(log_msg, extra={"alert_details": alert.details})
            elif alert.level == SeverityLevel.ERROR:
                self._alert_logger.error(log_msg, extra={"alert_details": alert.details})
            else:
                self._alert_logger.critical(log_msg, extra={"alert_details": alert.details})
            self._record_success()
            return True
        except Exception as e:
            self._record_failure(str(e))
            logger.error(f"日志告警发送失败: {e}")
            return False


# ═══════════════════════════════════════════════
#  邮件告警通道（阶段9任务10增强：实际SMTP发送）
# ═══════════════════════════════════════════════

class EmailAlertChannel(AlertChannel):
    """
    邮件告警通道（SMTP实际发送）

    使用smtplib发送邮件，支持HTML和纯文本格式，支持TLS/SSL。

    Usage:
        email_ch = EmailAlertChannel(
            smtp_host="smtp.example.com",
            smtp_port=465,
            username="alert@example.com",
            password="password",
            recipients=["admin@example.com", "ops@example.com"],
            use_tls=True,
        )
        alert_manager.add_channel(email_ch)
    """

    def __init__(
        self,
        name: str = "email",
        min_level: SeverityLevel = SeverityLevel.ERROR,
        smtp_host: str = "",
        smtp_port: int = 465,
        username: str = "",
        password: str = "",
        recipients: Optional[List[str]] = None,
        from_addr: Optional[str] = None,
        use_tls: bool = True,
        use_ssl: bool = False,
        subject_template: str = "[{level}] {message}",
        timeout: float = 30.0,
        async_mode: bool = True,
        queue_size: int = 100,
    ):
        super().__init__(name=name, min_level=min_level)
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.recipients = recipients or []
        self.from_addr = from_addr or username
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.subject_template = subject_template
        self.timeout = timeout
        # P3-9：异步发送队列（同步 SMTP I/O 不阻塞告警主流程）
        self.async_mode = async_mode
        self._mail_queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._mail_stop = threading.Event()
        self._mail_worker = threading.Thread(
            target=self._mail_worker_loop,
            name=f"email-alert-{name}",
            daemon=True,
        )
        self._mail_worker.start()

    def _build_subject(self, alert: Alert) -> str:
        """构建邮件主题"""
        try:
            return self.subject_template.format(
                level=alert.level.value.upper(),
                message=alert.message[:50],
                source=alert.source,
                category=alert.category,
            )
        except Exception:
            return f"[{alert.level.value.upper()}] {alert.message[:50]}"

    def _build_html_body(self, alert: Alert) -> str:
        """构建HTML邮件正文"""
        level_color = {
            "info": "#17a2b8",
            "warning": "#ffc107",
            "error": "#dc3545",
            "critical": "#6f42c1",
        }.get(alert.level.value, "#6c757d")

        details_rows = ""
        for k, v in alert.details.items():
            details_rows += f"<tr><td style='padding:4px 12px;border-bottom:1px solid #eee;'>{k}</td><td style='padding:4px 12px;border-bottom:1px solid #eee;'>{v}</td></tr>\n"

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert.timestamp))
        agg_info = f"<p>聚合事件数: <strong>{alert.aggregation_count}</strong></p>" if alert.aggregation_count > 1 else ""
        esc_info = f"<p style='color:#dc3545;'>⚠️ 此告警由 {alert.escalated_from.value} 级自动升级</p>" if alert.is_escalated else ""

        return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5;">
<div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
  <div style="background: {level_color}; color: white; padding: 16px 24px;">
    <h2 style="margin: 0;">[{alert.level.value.upper()}] 告警通知</h2>
  </div>
  <div style="padding: 24px;">
    <p style="font-size: 16px; margin: 0 0 16px 0;"><strong>{alert.message}</strong></p>
    <p style="color: #666; margin: 4px 0;">来源: {alert.source}</p>
    <p style="color: #666; margin: 4px 0;">分类: {alert.category}</p>
    <p style="color: #666; margin: 4px 0;">时间: {timestamp}</p>
    <p style="color: #666; margin: 4px 0;">告警ID: {alert.alert_id}</p>
    {agg_info}
    {esc_info}
    {"<h3 style='margin-top:20px;'>详细信息</h3><table style='width:100%;border-collapse:collapse;'>" + details_rows + "</table>" if alert.details else ""}
  </div>
  <div style="background: #f8f9fa; padding: 12px 24px; text-align: center; color: #999; font-size: 12px;">
    Pythonwdhmm 量化系统自动告警
  </div>
</div>
</body>
</html>"""

    def _build_text_body(self, alert: Alert) -> str:
        """构建纯文本邮件正文"""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert.timestamp))
        lines = [
            f"[{alert.level.value.upper()}] 告警通知",
            "=" * 40,
            f"消息: {alert.message}",
            f"来源: {alert.source}",
            f"分类: {alert.category}",
            f"时间: {timestamp}",
            f"告警ID: {alert.alert_id}",
        ]
        if alert.aggregation_count > 1:
            lines.append(f"聚合事件数: {alert.aggregation_count}")
        if alert.is_escalated:
            lines.append(f"⚠️ 此告警由 {alert.escalated_from.value} 级自动升级")
        if alert.details:
            lines.append("\n详细信息:")
            for k, v in alert.details.items():
                lines.append(f"  {k}: {v}")
        lines.append("\n-- Pythonwdhmm 量化系统自动告警 --")
        return "\n".join(lines)

    def send(self, alert: Alert) -> bool:
        """
        发送邮件告警（P3-9：默认异步入队，不阻塞告警主流程；async_mode=False 走同步直发）

        Returns
        -------
        bool : 异步模式下为「已入队」；同步模式下为「是否发送成功」
        """
        if not self.smtp_host or not self.recipients:
            logger.warning(f"邮件通道未配置SMTP或收件人，跳过发送: {alert.summary()}")
            self._record_failure("SMTP not configured")
            return False

        # 构建消息（模板/正文异常在此兜底，不进入发送队列）
        try:
            msg = self._build_message(alert)
        except Exception as e:
            self._record_failure(str(e))
            logger.error(f"邮件消息构建失败: {e}", exc_info=True)
            return False

        if not self.async_mode:
            return self._send_sync(alert, msg)

        try:
            self._mail_queue.put_nowait((alert, msg))
        except queue.Full:
            self._record_failure("mail queue full")
            logger.error(f"邮件告警队列已满，丢弃: {alert.summary()}")
            return False
        return True

    def _build_message(self, alert: Alert) -> MIMEMultipart:
        """构建 MIME 邮件消息（主题/HTML/纯文本）"""
        subject = self._build_subject(alert)
        html_body = self._build_html_body(alert)
        text_body = self._build_text_body(alert)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.recipients)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        return msg

    def _send_sync(self, alert: Alert, msg: MIMEMultipart) -> bool:
        """同步 SMTP 直发（async_mode=False 与工作线程共用）"""
        try:
            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self.timeout)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)
                if self.use_tls:
                    server.starttls()

            if self.username and self.password:
                server.login(self.username, self.password)

            server.sendmail(self.from_addr, self.recipients, msg.as_string())
            server.quit()

            self._record_success()
            logger.info(f"邮件告警发送成功: {alert.summary()} -> {self.recipients}")
            return True

        except Exception as e:
            self._record_failure(str(e))
            logger.error(f"邮件告警发送失败: {e}", exc_info=True)
            return False

    def _mail_worker_loop(self) -> None:
        """后台工作线程：消费邮件队列，逐条同步发送（daemon，不阻塞进程退出）"""
        while not self._mail_stop.is_set():
            try:
                alert, msg = self._mail_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._send_sync(alert, msg)
            finally:
                self._mail_queue.task_done()

    def close(self) -> None:
        """停止后台发送线程（置停止事件；队列剩余任务不再处理）"""
        self._mail_stop.set()
# ═══════════════════════════════════════════════
#  Webhook告警通道（阶段9任务10增强：实际HTTP发送）
# ═══════════════════════════════════════════════

class WebhookAlertChannel(AlertChannel):
    """
    Webhook告警通道（HTTP POST实际发送）

    支持飞书、钉钉、企业微信和通用Webhook，带重试机制。

    Usage:
        # 飞书
        feishu = WebhookAlertChannel(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
            webhook_type="feishu",
        )
        # 钉钉
        dingtalk = WebhookAlertChannel(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=xxx",
            webhook_type="dingtalk",
        )
        # 通用
        generic = WebhookAlertChannel(
            webhook_url="https://api.example.com/alerts",
            webhook_type="generic",
            headers={"Authorization": "Bearer xxx"},
        )
    """

    def __init__(
        self,
        name: str = "webhook",
        min_level: SeverityLevel = SeverityLevel.WARNING,
        webhook_url: str = "",
        webhook_type: str = "generic",
        headers: Optional[Dict[str, str]] = None,
        template: Optional[Callable[[Alert], Dict[str, Any]]] = None,
        timeout: float = 10.0,
        retries: int = 3,
        retry_delay: float = 2.0,
    ):
        super().__init__(name=name, min_level=min_level)
        self.webhook_url = webhook_url
        self.webhook_type = webhook_type.lower()
        self.headers = headers or {"Content-Type": "application/json"}
        self.template = template
        self.timeout = timeout
        self.retries = max(1, retries)
        self.retry_delay = retry_delay
        # P1-13：webhook 发送改为后台线程队列，避免同步重试阻塞行情/风控交易线程
        self._send_q: "queue.Queue[bytes]" = queue.Queue(maxsize=256)
        self._worker = threading.Thread(
            target=self._webhook_worker, name="webhook-alert", daemon=True,
        )
        self._worker.start()

    def _webhook_worker(self) -> None:
        """后台线程：消费发送队列，真正执行阻塞式 HTTP POST + 重试。"""
        while True:
            try:
                data, summary = self._send_q.get()
            except Exception:
                return
            try:
                self._post_with_retry(data, summary)
            finally:
                self._send_q.task_done()

    def _post_with_retry(self, data: bytes, summary: str) -> bool:
        for attempt in range(1, self.retries + 1):
            try:
                req = urlrequest.Request(
                    self.webhook_url, data=data, headers=self.headers, method="POST",
                )
                with urlrequest.urlopen(req, timeout=self.timeout) as response:
                    status = response.getcode()
                    if 200 <= status < 300:
                        self._record_success()
                        logger.debug(f"Webhook告警发送成功 (attempt {attempt}): {summary}")
                        return True
                    error_msg = f"HTTP {status}"
                    logger.warning(f"Webhook告警发送失败 (attempt {attempt}/{self.retries}): {error_msg}")
            except URLError as e:
                error_msg = str(e)
                logger.warning(f"Webhook告警网络错误 (attempt {attempt}/{self.retries}): {e}")
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Webhook告警发送异常 (attempt {attempt}/{self.retries}): {e}", exc_info=True)
            if attempt < self.retries:
                time.sleep(self.retry_delay * attempt)
        self._record_failure(error_msg if "error_msg" in dir() else "max retries exceeded")
        logger.error(f"Webhook告警发送最终失败 ({self.retries}次重试): {summary}")
        return False

    def _build_feishu_payload(self, alert: Alert) -> Dict[str, Any]:
        """构建飞书Webhook payload"""
        level_emoji = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}.get(alert.level.value, "📢")
        text = f"{level_emoji} **[{alert.level.value.upper()}] {alert.message}**\n\n"
        text += f"来源: {alert.source}\n"
        text += f"分类: {alert.category}\n"
        text += f"时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alert.timestamp))}\n"
        if alert.aggregation_count > 1:
            text += f"聚合事件数: {alert.aggregation_count}\n"
        if alert.is_escalated:
            text += f"⚠️ 升级自 {alert.escalated_from.value} 级\n"
        if alert.details:
            text += "\n详细信息:\n"
            for k, v in alert.details.items():
                text += f"- {k}: {v}\n"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{level_emoji} 告警通知"},
                    "template": {"info": "blue", "warning": "orange", "error": "red", "critical": "red"}.get(alert.level.value, "grey"),
                },
                "elements": [
                    {"tag": "markdown", "content": text},
                ],
            },
        }

    def _build_dingtalk_payload(self, alert: Alert) -> Dict[str, Any]:
        """构建钉钉Webhook payload"""
        level_prefix = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}.get(alert.level.value, "📢")
        text = f"{level_prefix} **[{alert.level.value.upper()}] {alert.message}**\n\n"
        text += f"> 来源: {alert.source}\n"
        text += f"> 分类: {alert.category}\n"
        text += f"> 时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alert.timestamp))}\n"
        if alert.aggregation_count > 1:
            text += f"> 聚合事件数: {alert.aggregation_count}\n"
        if alert.details:
            text += "\n**详细信息:**\n"
            for k, v in alert.details.items():
                text += f"- {k}: {v}\n"

        return {
            "msgtype": "markdown",
            "markdown": {
                "title": f"[{alert.level.value.upper()}] {alert.message[:30]}",
                "text": text,
            },
        }

    def _build_wecom_payload(self, alert: Alert) -> Dict[str, Any]:
        """构建企业微信Webhook payload"""
        level_color = {"info": "#17a2b8", "warning": "#ffc107", "error": "#dc3545", "critical": "#6f42c1"}.get(alert.level.value, "#6c757d")
        content = f"**[{alert.level.value.upper()}] {alert.message}**\n"
        content += f"> 来源: {alert.source}\n"
        content += f"> 分类: {alert.category}\n"
        content += f"> 时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alert.timestamp))}\n"
        if alert.details:
            content += "\n**详细信息:**\n"
            for k, v in alert.details.items():
                content += f">- {k}: {v}\n"

        return {
            "msgtype": "markdown",
            "markdown": {
                "content": content,
            },
        }

    def _build_generic_payload(self, alert: Alert) -> Dict[str, Any]:
        """构建通用Webhook payload"""
        return alert.to_dict()

    def _build_payload(self, alert: Alert) -> Dict[str, Any]:
        """根据webhook类型构建payload"""
        if self.template:
            return self.template(alert)
        if self.webhook_type == "feishu":
            return self._build_feishu_payload(alert)
        elif self.webhook_type == "dingtalk":
            return self._build_dingtalk_payload(alert)
        elif self.webhook_type == "wecom":
            return self._build_wecom_payload(alert)
        else:
            return self._build_generic_payload(alert)

    def send(self, alert: Alert) -> bool:
        """发送Webhook告警——P1-13：入队后台线程发送，立即返回，不阻塞调用方交易线程。"""
        if not self.webhook_url:
            logger.warning(f"Webhook通道未配置URL，跳过发送: {alert.summary()}")
            self._record_failure("Webhook URL not configured")
            return False
        payload = self._build_payload(alert)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self._send_q.put_nowait((data, alert.summary()))
            return True
        except queue.Full:
            logger.warning("Webhook发送队列已满，丢弃告警: %s", alert.summary())
            self._record_failure("send queue full")
            return False


# ═══════════════════════════════════════════════
#  告警聚合器（阶段9任务10新增）
# ═══════════════════════════════════════════════

@dataclass
class _AggregationBucket:
    """内部聚合桶"""
    key: str
    symbol: str
    data_type: str
    dirty_type: str
    count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    max_level: SeverityLevel = SeverityLevel.INFO
    total_bad: int = 0
    total_total: int = 0
    errors: List[str] = field(default_factory=list)
    source_step: str = ""

    @property
    def age(self) -> float:
        return time.time() - self.first_seen

    @property
    def bad_ratio(self) -> float:
        if self.total_total <= 0:
            return 0.0
        return self.total_bad / self.total_total


class AlertAggregator:
    """
    告警聚合器

    将同一标的+同一脏数据类型在聚合窗口内的多条事件聚合为一条告警，
    避免实盘高频场景下告警刷屏。

    工作原理:
      1. 每条脏数据事件进入对应聚合桶（key = symbol:data_type:dirty_type）
      2. 桶的生命周期 = aggregation_window
      3. 桶过期时（或flush时）生成聚合告警
      4. 聚合告警包含：事件数、首次/最后时间、最高级别、总脏数据数/总数、错误列表

    为实盘无缝对接预留:
      - 流式聚合：实盘逐条事件实时聚合
      - 可配置聚合窗口：不同场景不同窗口
      - flush机制：流水线结束时强制刷新所有桶
    """

    def __init__(self, window_seconds: float = 60.0):
        self._buckets: Dict[str, _AggregationBucket] = {}
        self._window = window_seconds
        self._lock = threading.Lock()

    @property
    def window_seconds(self) -> float:
        return self._window

    @window_seconds.setter
    def window_seconds(self, value: float) -> None:
        self._window = max(1.0, value)

    def _make_key(self, symbol: str, data_type: str, dirty_type: str) -> str:
        return f"{symbol}:{data_type}:{dirty_type}"

    def add_event(
        self,
        symbol: str = "",
        data_type: str = "",
        dirty_type: str = "",
        severity: str = "warning",
        bad_count: int = 0,
        total_count: int = 0,
        errors: Optional[List[str]] = None,
        source_step: str = "",
    ) -> Optional[_AggregationBucket]:
        """
        添加事件到聚合桶

        Parameters
        ----------
        symbol : str
            标的代码
        data_type : str
            数据类型
        dirty_type : str
            脏数据类型
        severity : str
            事件严重级别
        bad_count : int
            本批脏数据数
        total_count : int
            本批总数
        errors : List[str] | None
            错误列表
        source_step : str
            来源步骤

        Returns
        -------
        _AggregationBucket | None : 如果桶过期需要发送告警则返回该桶，否则返回None
        """
        key = self._make_key(symbol, data_type, dirty_type)
        sev_map = {
            "info": SeverityLevel.INFO,
            "warning": SeverityLevel.WARNING,
            "error": SeverityLevel.ERROR,
            "critical": SeverityLevel.CRITICAL,
        }
        level = sev_map.get(severity, SeverityLevel.WARNING)

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.age >= self._window:
                # 新桶或旧桶过期
                if bucket and bucket.age >= self._window:
                    # 旧桶过期，需要先发送
                    expired = bucket
                    self._buckets[key] = _AggregationBucket(
                        key=key, symbol=symbol, data_type=data_type, dirty_type=dirty_type,
                        count=1, max_level=level, total_bad=bad_count, total_total=total_count,
                        errors=list(errors or []), source_step=source_step,
                    )
                    return expired
                # 全新桶
                self._buckets[key] = _AggregationBucket(
                    key=key, symbol=symbol, data_type=data_type, dirty_type=dirty_type,
                    count=1, max_level=level, total_bad=bad_count, total_total=total_count,
                    errors=list(errors or []), source_step=source_step,
                )
                return None

            # 已有桶，累加
            bucket.count += 1
            bucket.last_seen = time.time()
            if level > bucket.max_level:
                bucket.max_level = level
            bucket.total_bad += bad_count
            bucket.total_total += total_count
            if errors:
                bucket.errors.extend(errors[:5])  # 最多保留5条错误
            if source_step and not bucket.source_step:
                bucket.source_step = source_step
            return None

    def flush(self, key: Optional[str] = None) -> List[_AggregationBucket]:
        """
        强制刷新聚合桶

        Parameters
        ----------
        key : str | None
            指定桶的key（None=刷新所有桶）

        Returns
        -------
        List[_AggregationBucket] : 需要发送告警的桶列表
        """
        with self._lock:
            if key:
                bucket = self._buckets.pop(key, None)
                return [bucket] if bucket else []
            else:
                buckets = list(self._buckets.values())
                self._buckets.clear()
                return buckets

    def cleanup_expired(self) -> List[_AggregationBucket]:
        """
        清理并返回所有过期的聚合桶

        Returns
        -------
        List[_AggregationBucket] : 过期的桶列表
        """
        with self._lock:
            expired = []
            for key, bucket in list(self._buckets.items()):
                if bucket.age >= self._window:
                    expired.append(bucket)
                    del self._buckets[key]
            return expired

    def get_pending_count(self) -> int:
        """获取当前待聚合的桶数量"""
        with self._lock:
            return len(self._buckets)

    def get_pending_keys(self) -> List[str]:
        """获取当前待聚合的桶key列表"""
        with self._lock:
            return list(self._buckets.keys())


# ═══════════════════════════════════════════════
#  AlertManager 告警管理器（阶段9任务10增强）
# ═══════════════════════════════════════════════

class AlertManager:
    """
    告警管理器

    管理多个告警通道，支持智能分级、告警聚合、去重、多通道分发、
    告警升级、静默模式、告警历史和发送回调。

    阶段9任务10核心增强:
      - AlertRule 可配置告警规则
      - AlertAggregator 流式聚合
      - handle_data_quality_event() 智能分级 + 聚合 + 自动告警
      - check_escalations() 告警升级检查
      - on_alert_callbacks 告警发送回调（metrics联动）
      - EmailAlertChannel / WebhookAlertChannel 实际发送

    阶段9任务12-C增强（与RiskManager联动）:
      - handle_strategy_pause_event() 处理策略暂停/恢复事件自动告警
      - 全局暂停→CRITICAL / 单标的暂停→ERROR / 恢复→INFO

    Usage:
        manager = AlertManager()
        manager.set_alert_rule(AlertRule.default_rule())
        manager.add_channel(ConsoleAlertChannel())
        manager.add_channel(LogAlertChannel())
        manager.add_channel(EmailAlertChannel(smtp_host="...", recipients=["..."]))

        # 订阅事件总线自动告警
        manager.subscribe_to_event_bus(get_event_bus())

        # 定期检查告警升级（scheduler调用）
        manager.check_escalations()
    """

    def __init__(self, dedup_window_seconds: float = 300.0, max_history: int = 1000):
        self._channels: Dict[str, AlertChannel] = {}
        self._dedup_window = dedup_window_seconds
        self._dedup_cache: Dict[str, float] = {}
        self._history: Deque[Alert] = deque(maxlen=max_history)
        self._silenced: bool = False
        self._silence_until: float = 0.0
        self._event_bus_subscribed: bool = False
        self._event_bus_handler_ids: List[str] = []

        # 任务10新增
        self._alert_rule: AlertRule = AlertRule.default_rule()
        self._aggregator: AlertAggregator = AlertAggregator(
            window_seconds=self._alert_rule.aggregation_window
        )
        self._on_alert_callbacks: List[Callable[[Alert], None]] = []
        self._last_escalation_check: float = 0.0
        self._escalation_lock = threading.Lock()

    # ═══════════════════════════════════════════
    #  告警规则管理（任务10新增）
    # ═══════════════════════════════════════════

    def set_alert_rule(self, rule: AlertRule) -> "AlertManager":
        """
        设置告警规则

        Parameters
        ----------
        rule : AlertRule
            告警规则配置

        Returns
        -------
        AlertManager : self
        """
        self._alert_rule = rule
        self._aggregator.window_seconds = rule.aggregation_window
        logger.info(f"告警规则已更新: 聚合窗口={rule.aggregation_window}s, 升级时间={rule.escalation_time}s")
        return self

    def get_alert_rule(self) -> AlertRule:
        """获取当前告警规则"""
        return self._alert_rule

    # ═══════════════════════════════════════════
    #  告警回调（任务10新增，用于metrics联动）
    # ═══════════════════════════════════════════

    def add_alert_callback(self, callback: Callable[[Alert], None]) -> "AlertManager":
        """
        添加告警发送回调

        每次成功发送告警时调用所有回调，用于metrics_collector更新指标等。

        Parameters
        ----------
        callback : Callable[[Alert], None]
            回调函数，接收Alert对象

        Returns
        -------
        AlertManager : self
        """
        self._on_alert_callbacks.append(callback)
        return self

    def remove_alert_callback(self, callback: Callable[[Alert], None]) -> bool:
        """移除告警发送回调"""
        if callback in self._on_alert_callbacks:
            self._on_alert_callbacks.remove(callback)
            return True
        return False

    def _notify_callbacks(self, alert: Alert) -> None:
        """通知所有告警回调（内部方法）"""
        for callback in self._on_alert_callbacks:
            try:
                callback(alert)
            except Exception as e:
                logger.error(f"告警回调执行异常: {e}", exc_info=True)

    # ═══════════════════════════════════════════
    #  通道管理
    # ═══════════════════════════════════════════

    def add_channel(self, channel: AlertChannel) -> "AlertManager":
        """添加告警通道"""
        self._channels[channel.name] = channel
        logger.debug(f"添加告警通道: {channel.name} (min_level={channel.min_level.value})")
        return self

    def remove_channel(self, name: str) -> bool:
        """移除告警通道"""
        if name in self._channels:
            del self._channels[name]
            logger.debug(f"移除告警通道: {name}")
            return True
        return False

    def get_channel(self, name: str) -> Optional[AlertChannel]:
        """获取告警通道"""
        return self._channels.get(name)

    def list_channels(self) -> List[str]:
        """列出所有通道名称"""
        return list(self._channels.keys())

    def enable_channel(self, name: str) -> bool:
        ch = self._channels.get(name)
        if ch:
            ch.enabled = True
            return True
        return False

    def disable_channel(self, name: str) -> bool:
        ch = self._channels.get(name)
        if ch:
            ch.enabled = False
            return True
        return False

    # ═══════════════════════════════════════════
    #  通道路由（任务10增强：按AlertRule选择通道）
    # ═══════════════════════════════════════════

    def _route_channels(self, level: SeverityLevel) -> List[AlertChannel]:
        """
        根据告警级别和AlertRule路由通道

        Parameters
        ----------
        level : SeverityLevel
            告警级别

        Returns
        -------
        List[AlertChannel] : 应该发送的通道列表
        """
        channel_names = self._alert_rule.get_channels_for_level(level)
        channels = []
        for name in channel_names:
            ch = self._channels.get(name)
            if ch and ch.can_send(Alert(level=level, message="route_check")):
                channels.append(ch)
        # 如果规则中没有匹配的通道，回退到所有可用通道
        if not channels:
            for ch in self._channels.values():
                if ch.can_send(Alert(level=level, message="route_check")):
                    channels.append(ch)
        return channels

    # ═══════════════════════════════════════════
    #  告警发送
    # ═══════════════════════════════════════════

    def send_alert(self, alert: Alert, dedup_enabled: bool = True) -> int:
        """
        发送告警（通过路由选择的通道，带回调通知）

        Parameters
        ----------
        alert : Alert
            告警对象
        dedup_enabled : bool
            是否启用去重

        Returns
        -------
        int : 成功发送的通道数量
        """
        # 静默模式检查
        if self._silenced and time.time() < self._silence_until:
            logger.debug(f"告警被静默模式抑制: {alert.summary()}")
            return 0

        # 去重检查
        if dedup_enabled and alert.deduplication_key:
            now = time.time()
            last_time = self._dedup_cache.get(alert.deduplication_key)
            if last_time is not None and (now - last_time) < self._dedup_window:
                logger.debug(
                    f"告警去重抑制: key={alert.deduplication_key}, "
                    f"距上次={now - last_time:.1f}s < {self._dedup_window}s"
                )
                return 0
            self._dedup_cache[alert.deduplication_key] = now
            # 清理过期的去重缓存
            expired = [k for k, v in self._dedup_cache.items()
                       if (now - v) > self._dedup_window * 10]
            for k in expired:
                del self._dedup_cache[k]

        # INFO级自动确认
        if alert.level == SeverityLevel.INFO and self._alert_rule.auto_acknowledge_info:
            alert.acknowledged = True

        # 记录历史
        self._history.append(alert)

        # 通道路由 + 多通道分发
        channels = self._route_channels(alert.level)
        sent_count = 0
        for channel in channels:
            try:
                if channel.send(alert):
                    sent_count += 1
            except Exception as e:
                logger.error(f"告警通道 [{channel.name}] 发送异常: {e}", exc_info=True)

        if sent_count == 0:
            # KP-P5-05: 未投递回退写 logs/alerts_undelivered.log（不静默丢失）
            _write_undelivered(alert)
            if self._channels:
                logger.warning(f"告警未通过任何通道发送: {alert.summary()} (可用通道={len(self._channels)})")

        # 通知回调（metrics联动）
        if sent_count > 0:
            self._notify_callbacks(alert)

        return sent_count

    def alert(
        self,
        level: SeverityLevel,
        message: str,
        source: str = "monitoring",
        category: str = "system",
        details: Optional[Dict[str, Any]] = None,
        deduplication_key: str = "",
    ) -> int:
        """便捷方法：创建并发送告警"""
        alert = Alert(
            level=level,
            message=message,
            source=source,
            category=category,
            details=details or {},
            deduplication_key=deduplication_key,
        )
        return self.send_alert(alert)

    # ═══════════════════════════════════════════
    #  事件总线订阅 + 智能处理（任务10核心增强）
    # ═══════════════════════════════════════════

    def subscribe_to_event_bus(self, bus: Any) -> List[str]:
        """
        订阅事件总线，接收数据质量事件并自动告警

        Parameters
        ----------
        bus : EventBus
            事件总线实例

        Returns
        -------
        List[str] : 订阅的 handler_id 列表
        """
        from myquant.core.event_bus import EventType

        handler_ids = []

        hid1 = bus.subscribe(
            EventType.DATA_QUALITY_DIRTY,
            lambda e: self.handle_data_quality_event(e),
        )
        handler_ids.append(hid1)

        hid2 = bus.subscribe(
            EventType.DATA_QUALITY_ALERT,
            lambda e: self.handle_quality_alert_event(e),
        )
        handler_ids.append(hid2)

        hid3 = bus.subscribe(
            EventType.DATA_QUALITY_PIPELINE_COMPLETE,
            lambda e: self.handle_pipeline_complete_event(e),
        )
        handler_ids.append(hid3)

        # 任务12-C：订阅策略暂停/恢复事件（与RiskManager联动，自动告警）
        hid4 = bus.subscribe(
            EventType.RISK_PAUSED,
            lambda e: self.handle_strategy_pause_event(e),
        )
        handler_ids.append(hid4)

        hid5 = bus.subscribe(
            EventType.RISK_RESUMED,
            lambda e: self.handle_strategy_pause_event(e),
        )
        handler_ids.append(hid5)

        self._event_bus_subscribed = True
        self._event_bus_handler_ids = handler_ids
        logger.info(f"AlertManager 已订阅事件总线，handler_ids={handler_ids}")
        return handler_ids

    def unsubscribe_from_event_bus(self, bus: Any) -> int:
        """取消事件总线订阅"""
        count = 0
        for hid in self._event_bus_handler_ids:
            if bus.unsubscribe(hid):
                count += 1
        self._event_bus_handler_ids = []
        self._event_bus_subscribed = False
        return count

    def handle_data_quality_event(self, event: Any) -> Optional[int]:
        """
        处理脏数据事件（智能分级 + 聚合 + 自动告警）

        处理流程:
          1. 从事件提取 dirty_type/symbol/data_type/severity/bad_count/total_count
          2. 使用AlertRule判断是否应该告警（min_bad_count + 阈值）
          3. 使用AlertRule综合判定告警级别
          4. 进入AlertAggregator聚合（避免高频刷屏）
          5. 聚合桶过期时发送聚合告警

        Parameters
        ----------
        event : DataQualityEvent
            脏数据事件

        Returns
        -------
        int | None : 发送的通道数量（聚合中返回None）
        """
        try:
            dirty_type = getattr(event, "dirty_type", "")
            symbol = getattr(event, "symbol", "")
            data_type = getattr(event, "data_type", "")
            severity = getattr(event, "severity", "warning")
            bad_count = getattr(event, "bad_count", 0)
            total_count = getattr(event, "total_count", 0)
            errors = getattr(event, "errors", [])
            source_step = getattr(event, "source_step", "")

            bad_ratio = bad_count / total_count if total_count > 0 else 0.0

            # 判断是否应该告警
            if not self._alert_rule.should_alert(bad_count, bad_ratio, dirty_type):
                return None

            # 综合判定级别
            level = self._alert_rule.determine_level(
                dirty_type=dirty_type,
                bad_ratio=bad_ratio,
                event_severity=severity,
            )

            # 进入聚合器
            expired_bucket = self._aggregator.add_event(
                symbol=symbol,
                data_type=data_type,
                dirty_type=dirty_type,
                severity=severity,
                bad_count=bad_count,
                total_count=total_count,
                errors=errors,
                source_step=source_step,
            )

            # 如果有过期桶，发送聚合告警
            if expired_bucket:
                return self._send_aggregated_alert(expired_bucket, level)

            return None

        except Exception as e:
            logger.error(f"处理脏数据事件异常: {e}", exc_info=True)
            return None

    def _send_aggregated_alert(self, bucket: _AggregationBucket, level: SeverityLevel) -> int:
        """
        发送聚合告警（内部方法）

        Parameters
        ----------
        bucket : _AggregationBucket
            聚合桶
        level : SeverityLevel
            告警级别

        Returns
        -------
        int : 发送的通道数量
        """
        # 重新计算综合级别（取桶内最高级别和规则判定级别的最高值）
        final_level = max(level, bucket.max_level, key=lambda x: x.level)

        dirty_type_desc = {
            "ohlc_logic_error": "OHLC逻辑错误",
            "negative_price": "负价格",
            "negative_volume": "负成交量",
            "bid_ask_cross": "买卖盘交叉",
            "missing_symbol": "缺少标的代码",
            "invalid_timestamp": "无效时间戳",
            "time_monotonic_error": "时间不单调",
            "duplicate_timestamp": "重复时间戳",
            "duplicate_tick": "重复Tick",
            "price_jump": "价格跳变",
            "volume_anomaly": "成交量异常",
            "zero_price": "零价格",
            "zero_volume": "零成交量",
        }.get(bucket.dirty_type, bucket.dirty_type or "未知")

        message = (
            f"数据质量告警: {dirty_type_desc}, "
            f"symbol={bucket.symbol or 'N/A'}, "
            f"聚合{bucket.count}条事件, "
            f"脏数据{bucket.total_bad}/{bucket.total_total}"
            f"({bucket.bad_ratio:.1%})"
        )

        dedup_key = f"agg:{bucket.symbol}:{bucket.data_type}:{bucket.dirty_type}"

        alert = Alert(
            level=final_level,
            message=message,
            source="event_bus.data_quality.aggregated",
            category="data_quality",
            details={
                "dirty_type": bucket.dirty_type,
                "symbol": bucket.symbol,
                "data_type": bucket.data_type,
                "aggregation_count": bucket.count,
                "total_bad": bucket.total_bad,
                "total_total": bucket.total_total,
                "bad_ratio": round(bucket.bad_ratio, 4),
                "first_seen": bucket.first_seen,
                "last_seen": bucket.last_seen,
                "source_step": bucket.source_step,
                "errors": bucket.errors[:5],
            },
            deduplication_key=dedup_key,
            aggregation_count=bucket.count,
        )

        logger.info(f"发送聚合告警: {alert.summary()}")
        return self.send_alert(alert)

    def handle_quality_alert_event(self, event: Any) -> Optional[int]:
        """
        处理质量告警事件（直接转发为告警）

        Parameters
        ----------
        event : QualityAlertEvent
            质量告警事件

        Returns
        -------
        int | None : 发送的通道数量
        """
        try:
            alert_level = getattr(event, "alert_level", "warning")
            sev_map = {
                "info": SeverityLevel.INFO,
                "warning": SeverityLevel.WARNING,
                "error": SeverityLevel.ERROR,
                "critical": SeverityLevel.CRITICAL,
            }
            level = sev_map.get(alert_level, SeverityLevel.WARNING)

            message = getattr(event, "message", "数据质量告警")
            symbol = getattr(event, "symbol", "")
            bad_ratio = getattr(event, "bad_ratio", 0)
            threshold = getattr(event, "threshold", 0)
            dirty_type = getattr(event, "dirty_type", "")
            dedup_key = getattr(event, "deduplication_key", "") or f"qa:{symbol}:{dirty_type}"

            alert = Alert(
                level=level,
                message=message,
                source="event_bus.quality_alert",
                category="data_quality",
                details={
                    "symbol": symbol,
                    "bad_ratio": bad_ratio,
                    "threshold": threshold,
                    "dirty_type": dirty_type,
                    "quality_grade": getattr(event, "quality_grade", ""),
                },
                deduplication_key=dedup_key,
            )

            return self.send_alert(alert)

        except Exception as e:
            logger.error(f"处理质量告警事件异常: {e}", exc_info=True)
            return None

    def handle_strategy_pause_event(self, event: Any) -> Optional[int]:
        """
        处理策略暂停/恢复事件（任务12-C，与RiskManager联动）

        风控管理器触发暂停/恢复时自动告警：
          - 全局暂停（pause_level=global）→ CRITICAL 告警（category=risk）
          - 单标的暂停（pause_level=symbol）→ ERROR 告警（范围受限）
          - 恢复事件 → INFO 告警（通知暂停已解除）

        去重键：risk_pause:{pause_level}:{symbol}:{paused}，
        去重窗口内相同暂停事件不重复刷屏（避免高频告警风暴）。

        Parameters
        ----------
        event : StrategyPauseEvent
            策略暂停/恢复事件（由 RiskManager 发布，见 task 12-B）

        Returns
        -------
        int | None : 发送的通道数量
        """
        try:
            paused = getattr(event, "paused", True)
            pause_level = getattr(event, "pause_level", "global")
            symbol = getattr(event, "symbol", "")
            reason = getattr(event, "reason", "")
            severity = getattr(event, "severity", "warning")

            if paused:
                level = STRATEGY_PAUSE_ALERT_LEVEL_MAP.get(
                    pause_level, SeverityLevel.ERROR
                )
                if pause_level == "global":
                    message = f"策略已全局暂停: {reason or '数据质量严重，禁止所有新开仓'}"
                else:
                    message = (
                        f"策略已暂停（标的{symbol or 'N/A'}）: "
                        f"{reason or '该标的数据质量异常，禁止买入'}"
                    )
            else:
                level = STRATEGY_RESUME_ALERT_LEVEL
                message = f"策略已恢复: {reason or '风控暂停已解除'}"

            dedup_key = f"risk_pause:{pause_level}:{symbol}:{paused}"

            alert = Alert(
                level=level,
                message=message,
                source="risk_manager",
                category="risk",
                details={
                    "paused": paused,
                    "pause_level": pause_level,
                    "symbol": symbol,
                    "severity": severity,
                    "quality_grade": getattr(event, "quality_grade", ""),
                    "bad_ratio": getattr(event, "bad_ratio", 0.0),
                    "strategy": getattr(event, "strategy", ""),
                },
                deduplication_key=dedup_key,
            )

            return self.send_alert(alert)

        except Exception as e:
            logger.error(f"处理策略暂停事件异常: {e}", exc_info=True)
            return None

    def handle_pipeline_complete_event(self, event: Any) -> Optional[int]:
        """
        处理流水线完成事件（按状态和质量等级分级告警）

        处理规则:
          - 流水线失败 → ERROR
          - 质量等级 critical → CRITICAL
          - 质量等级 poor 或 bad_ratio > 10% → ERROR
          - 质量等级 fair 或 bad_ratio > 5% → WARNING
          - 其他 → 不告警（只记录）

        Parameters
        ----------
        event : PipelineCompleteEvent
            流水线完成事件

        Returns
        -------
        int | None : 发送的通道数量
        """
        try:
            status = getattr(event, "status", "completed")
            quality_grade = getattr(event, "quality_grade", "")
            bad_ratio = getattr(event, "bad_ratio", 0)
            symbol = getattr(event, "symbol", "")
            data_type = getattr(event, "data_type", "")
            cleaned_count = getattr(event, "cleaned_count", 0)
            bad_count = getattr(event, "bad_count", 0)
            total_count = getattr(event, "total_count", 0)
            duration = getattr(event, "duration", 0)
            error = getattr(event, "error", "")

            # 先flush聚合器（流水线结束，所有待聚合桶应发送）
            pending_buckets = self._aggregator.flush()
            for bucket in pending_buckets:
                bucket_level = self._alert_rule.determine_level(
                    dirty_type=bucket.dirty_type,
                    bad_ratio=bucket.bad_ratio,
                )
                self._send_aggregated_alert(bucket, bucket_level)

            # 流水线失败 → ERROR
            if status == "failed":
                alert = Alert(
                    level=SeverityLevel.ERROR,
                    message=f"清洗流水线失败: {symbol or 'N/A'}, error={error or 'unknown'}",
                    source="event_bus.pipeline",
                    category="data_quality",
                    details={
                        "symbol": symbol, "data_type": data_type,
                        "error": error, "duration": duration,
                    },
                    deduplication_key=f"pipeline_fail:{symbol}:{data_type}",
                )
                return self.send_alert(alert)

            # 质量等级判定
            if quality_grade == "critical" or bad_ratio >= 0.5:
                level = SeverityLevel.CRITICAL
            elif quality_grade == "poor" or bad_ratio >= 0.1:
                level = SeverityLevel.ERROR
            elif quality_grade == "fair" or bad_ratio >= 0.05:
                level = SeverityLevel.WARNING
            else:
                # 质量良好，不告警
                logger.debug(f"流水线完成质量良好，不告警: {symbol}, grade={quality_grade}, bad_ratio={bad_ratio:.1%}")
                return None

            alert = Alert(
                level=level,
                message=(
                    f"清洗质量告警: {symbol or 'N/A'}, "
                    f"grade={quality_grade or 'N/A'}, "
                    f"bad_ratio={bad_ratio:.1%}, "
                    f"cleaned={cleaned_count}, bad={bad_count}/{total_count}"
                ),
                source="event_bus.pipeline",
                category="data_quality",
                details={
                    "symbol": symbol, "data_type": data_type,
                    "quality_grade": quality_grade, "bad_ratio": bad_ratio,
                    "cleaned_count": cleaned_count, "bad_count": bad_count,
                    "total_count": total_count, "duration": duration,
                    "steps_executed": getattr(event, "steps_executed", []),
                },
                deduplication_key=f"pipeline_quality:{symbol}:{data_type}:{quality_grade}",
            )
            return self.send_alert(alert)

        except Exception as e:
            logger.error(f"处理流水线完成事件异常: {e}", exc_info=True)
            return None

    def flush_aggregator(self) -> int:
        """
        强制刷新聚合器，发送所有待聚合告警

        适用于流水线结束、实盘收盘等场景。

        Returns
        -------
        int : 发送的聚合告警数量
        """
        buckets = self._aggregator.flush()
        sent = 0
        for bucket in buckets:
            level = self._alert_rule.determine_level(
                dirty_type=bucket.dirty_type,
                bad_ratio=bucket.bad_ratio,
            )
            if self._send_aggregated_alert(bucket, level) > 0:
                sent += 1
        return sent

    # ═══════════════════════════════════════════
    #  告警升级（任务10新增）
    # ═══════════════════════════════════════════

    def check_escalations(self) -> int:
        """
        检查并执行待升级的告警

        WARNING级告警超过escalation_time未确认/未解决，自动升级为ERROR级，
        并重新发送告警（标记escalated_from）。

        适用于scheduler定期调用（如每5分钟检查一次）。

        Returns
        -------
        int : 升级的告警数量
        """
        if not self._alert_rule.escalation_enabled:
            return 0

        with self._escalation_lock:
            now = time.time()
            self._last_escalation_check = now
            escalated = 0

            # 迭代副本，避免发送升级告警时修改原deque
            for alert in list(self._history):
                # 只升级WARNING级、未确认、未解决、超过升级时间的告警
                if (alert.level == SeverityLevel.WARNING
                        and not alert.acknowledged
                        and not alert.resolved
                        and (now - alert.timestamp) >= self._alert_rule.escalation_time
                        and not alert.is_escalated):
                    # 创建升级告警
                    escalated_alert = Alert(
                        level=SeverityLevel.ERROR,
                        message=f"[升级] {alert.message} (WARNING级超过{self._alert_rule.escalation_time:.0f}秒未确认)",
                        source=alert.source,
                        category=alert.category,
                        details=dict(alert.details, original_alert_id=alert.alert_id),
                        deduplication_key=f"escalated:{alert.alert_id}",
                        escalated_from=SeverityLevel.WARNING,
                        parent_alert_id=alert.alert_id,
                    )
                    sent = self.send_alert(escalated_alert, dedup_enabled=False)
                    if sent > 0:
                        alert.acknowledged = True  # 标记原告警为已确认（避免重复升级）
                        escalated += 1
                        logger.warning(f"告警升级: {alert.alert_id} WARNING -> ERROR")

            if escalated > 0:
                logger.info(f"告警升级检查完成: 升级了 {escalated} 条告警")
            return escalated

    @property
    def last_escalation_check(self) -> float:
        """上次升级检查时间"""
        return self._last_escalation_check

    # ═══════════════════════════════════════════
    #  告警静默/维护模式
    # ═══════════════════════════════════════════

    def silence(self, duration_seconds: float = 3600.0) -> None:
        """启用告警静默（维护模式）"""
        self._silenced = True
        self._silence_until = time.time() + duration_seconds
        logger.warning(f"告警静默已启用，持续 {duration_seconds}s")

    def unsilence(self) -> None:
        """取消告警静默"""
        self._silenced = False
        self._silence_until = 0.0
        logger.info("告警静默已取消")

    @property
    def is_silenced(self) -> bool:
        return self._silenced and time.time() < self._silence_until

    # ═══════════════════════════════════════════
    #  告警历史与统计
    # ═══════════════════════════════════════════

    def get_history(
        self,
        level: Optional[SeverityLevel] = None,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[Alert]:
        """查询告警历史"""
        results = []
        for alert in reversed(self._history):
            if level and alert.level != level:
                continue
            if category and alert.category != category:
                continue
            results.append(alert)
            if len(results) >= limit:
                break
        return results

    def get_unacknowledged(self, limit: int = 50) -> List[Alert]:
        """获取未确认的告警"""
        return [a for a in reversed(self._history) if not a.acknowledged and not a.resolved][:limit]

    def get_escalated(self, limit: int = 50) -> List[Alert]:
        """获取升级告警"""
        return [a for a in reversed(self._history) if a.is_escalated][:limit]

    def acknowledge(self, alert_id: str) -> bool:
        """确认告警"""
        for alert in self._history:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                return True
        return False

    def resolve(self, alert_id: str) -> bool:
        """解决告警"""
        for alert in self._history:
            if alert.alert_id == alert_id:
                alert.resolved = True
                alert.acknowledged = True
                return True
        return False

    def clear_dedup_cache(self) -> None:
        """清空调警去重缓存"""
        self._dedup_cache.clear()

    @property
    def aggregator(self) -> AlertAggregator:
        """获取聚合器"""
        return self._aggregator

    @property
    def stats(self) -> Dict[str, Any]:
        """获取告警管理器统计"""
        channel_stats = {name: ch.stats for name, ch in self._channels.items()}
        level_counts: Dict[str, int] = {}
        category_counts: Dict[str, int] = {}
        for alert in self._history:
            level_counts[alert.level.value] = level_counts.get(alert.level.value, 0) + 1
            category_counts[alert.category] = category_counts.get(alert.category, 0) + 1

        return {
            "channels": channel_stats,
            "channels_count": len(self._channels),
            "history_size": len(self._history),
            "level_counts": level_counts,
            "category_counts": category_counts,
            "unacknowledged_count": len(self.get_unacknowledged()),
            "escalated_count": len(self.get_escalated()),
            "silenced": self.is_silenced,
            "event_bus_subscribed": self._event_bus_subscribed,
            "dedup_cache_size": len(self._dedup_cache),
            "aggregation_pending": self._aggregator.get_pending_count(),
            "aggregation_window": self._alert_rule.aggregation_window,
            "escalation_enabled": self._alert_rule.escalation_enabled,
            "escalation_time": self._alert_rule.escalation_time,
            "callbacks_count": len(self._on_alert_callbacks),
        }

    def __repr__(self) -> str:
        return (
            f"<AlertManager channels={len(self._channels)} "
            f"history={len(self._history)} "
            f"aggregating={self._aggregator.get_pending_count()} "
            f"silenced={self.is_silenced}>"
        )


# ═══════════════════════════════════════════════
#  全局告警管理器单例
# ═══════════════════════════════════════════════

_global_alert_manager: Optional[AlertManager] = None
_alert_manager_lock = None  # 延迟导入


def get_alert_manager() -> AlertManager:
    """获取全局告警管理器单例"""
    global _global_alert_manager, _alert_manager_lock
    if _alert_manager_lock is None:
        import threading
        _alert_manager_lock = threading.Lock()
    with _alert_manager_lock:
        if _global_alert_manager is None:
            _global_alert_manager = AlertManager()
            # 默认注册控制台和日志通道
            _global_alert_manager.add_channel(ConsoleAlertChannel())
            _global_alert_manager.add_channel(LogAlertChannel())
            # KP-P5-05: 按 notification.channels 装配 Email/Webhook 通道
            configure_notification_channels(_global_alert_manager)
        return _global_alert_manager


def configure_notification_channels(manager: AlertManager) -> int:
    """KP-P5-05: 按配置 notification.channels 装配 Email/Webhook 告警通道。"""
    added = 0
    try:
        from myquant.core.settings import get_settings
        notif = get_settings().get("notification", None) or {}
        if not (notif.get("enabled") and isinstance(notif.get("channels"), dict)):
            return 0
        channels = notif["channels"]
        # Email 通道
        email = channels.get("email") or {}
        if (
            isinstance(email, dict)
            and email.get("enabled")
            and email.get("smtp_host")
            and email.get("smtp_to")
        ):
            manager.add_channel(
                EmailAlertChannel(
                    name="email",
                    smtp_host=email["smtp_host"],
                    smtp_port=int(email.get("smtp_port", 465) or 465),
                    username=email.get("smtp_user", ""),
                    password=email.get("smtp_password", ""),
                    recipients=[email["smtp_to"]],
                    use_tls=True,
                )
            )
            added += 1
        # Webhook 通道（feishu / wecom）
        for wh_name in ("feishu", "wecom"):
            wh = channels.get(wh_name) or {}
            url = wh.get("webhook") if isinstance(wh, dict) else None
            if url:
                manager.add_channel(
                    WebhookAlertChannel(name=wh_name, webhook_url=url)
                )
                added += 1
    except Exception as e:
        logger.warning(f"告警通道装配失败: {e}")
    return added


def _write_undelivered(alert: Alert) -> None:
    """KP-P5-05: 告警未通过任何通道投递时回退写 logs/alerts_undelivered.log。"""
    try:
        from myquant.core.settings import get_settings
        log_dir = get_settings().project_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"[{alert.level.value}] source={alert.source} "
            f"category={alert.category} {alert.message}\n"
        )
        with open(log_dir / "alerts_undelivered.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as e:
        logger.warning(f"写入未投递告警日志失败: {e}")


def reset_alert_manager() -> None:
    """重置全局告警管理器（测试用）"""
    global _global_alert_manager
    _global_alert_manager = None
