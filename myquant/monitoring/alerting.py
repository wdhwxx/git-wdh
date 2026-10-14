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
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple
from urllib import request as urlrequest
from urllib.error import URLError

from myquant.core.constants import (
    DEFAULT_DEPENDENCY_MAP,
    TRADE_EXCEPTION_ALERT_LEVEL_MAP,
    MetricType,
    SeverityLevel,
)
from myquant.core.logger import get_logger
from myquant.core.registry import (
    register_component,  # G2-5：get_alert_manager 工厂注册（供 live/scheduler 经 L0 取用）
)

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
#  交易异常事件 → 告警映射（A-8 交易异常告警闭环）
# ═══════════════════════════════════════════════
#  - 触发模式 EVENT（R-02）：由事件流实时触发告警，不等周期巡检兜底。
#  - 防未来：只响应"已发生"的交易异常事件（事件时间 = 业务发生时刻 event_ts），
#    不预测未来可能发生的拒单/失败。
#  - ORDER_REJECTED / ORDER_FAILED / FILL_REPORT_ERROR → ERROR（P1）
#  - EMERGENCY_LIQUIDATION → CRITICAL（P0，最高优先级）
# 注：TRADE_EXCEPTION_ALERT_LEVEL_MAP 已下沉 myquant/core/constants（G2-1 断环）
# A-8 需要订阅并自动告警的交易异常事件类型（顺序即订阅登记顺序）
TRADE_EXCEPTION_ALERT_EVENT_TYPES: Tuple[str, ...] = (
    "order.rejected",
    "order.failed",
    "fill.report_error",
    "emergency.liquidation",
)


# ═══════════════════════════════════════════════
#  P0~P3 业务分级 → SeverityLevel 统一映射（A-10，回写#214；P0-3 告警分级定稿）
# ═══════════════════════════════════════════════
# 语义取自 docs/监控档案/告警分级.md §1 分级表（响应时限：P0=1min / P1=5min /
# P2=15min / P3=次日）。与 A-8 已提交映射自洽：P1 交易异常(order.rejected 等)→ERROR、
# P0 emergency.liquidation→CRITICAL；本表作为统一映射被引用，不改动 A-8 handler 行为。
#  - P0 资金异常/风控击穿/交易中断 → CRITICAL（最高级）
#  - P1 核心策略异常/行情中断/大量拒单 → ERROR
#  - P2 性能退化/数据延迟 → WARNING
#  - P3 趋势/日报 → INFO
ALERT_LEVEL_CODE_MAP: Dict[str, "SeverityLevel"] = {
    "P0": SeverityLevel.CRITICAL,
    "P1": SeverityLevel.ERROR,
    "P2": SeverityLevel.WARNING,
    "P3": SeverityLevel.INFO,
}


def p0_p3_level(code: str) -> "SeverityLevel":
    """把 P0~P3 业务分级代码映射为 SeverityLevel（统一入口，大小写与首尾空白容错）。

    Args:
        code: 业务分级代码（"P0"/"P1"/"P2"/"P3"，大小写不敏感、容忍首尾空白）。

    Returns:
        SeverityLevel: 映射后的告警级别（P0→CRITICAL、P1→ERROR、P2→WARNING、P3→INFO）。

    Raises:
        ValueError: code 为空或非 P0~P3 时抛出（防御式，禁止静默落到某一级）。
    """
    if code is None:
        raise ValueError("p0_p3_level: code 不能为空")
    key = str(code).strip().upper()
    if key not in ALERT_LEVEL_CODE_MAP:
        raise ValueError(
            f"非法告警分级代码: {code!r}（须为 P0~P3，与 P0-3 告警分级表对齐）"
        )
    return ALERT_LEVEL_CODE_MAP[key]


# A-10（回写#214）：按 category 的默认 Runbook 映射（P0-4 模板；与 docs/监控档案/
# 告警分级.md §2 示例告警 FI-xx 对齐）。显式 runbook_id / details.runbook_id 优先，
# 二者皆空时按 category 兜底补齐，保证 P0/P1 告警必挂 Runbook。
# 注：broker 默认值与 myquant.monitoring.watchdog.RUNBOOK_BROKER_DISCONNECT 逐字一致
# （避免跨模块 import 耦合，字符串常量对齐即可）。
DEFAULT_RUNBOOK_BY_CATEGORY: Dict[str, str] = {
    "data_quality": "RB-DATA-QUALITY-001",   # 数据质量/新鲜度缺口（P2）
    "connection": "RB-CONNECTION-001",       # 通用连接类（P0）
    "broker": "RB-BROKER-DISCONNECT-001",    # 券商连接断开（FI-03，P0）
    "trade": "RB-TRADE-EXCEPTION-001",       # 拒单/成交回报异常（FI-04，P1）
    "risk": "RB-RISK-001",                   # 风控/保证金占用（P0）
    "reconciliation": "RB-RECONCILE-001",    # 持仓/资金对账差异（FI-05，P0）
    "health": "RB-HEALTH-001",               # 巡检非健康（health_check）
    "system": "RB-SYSTEM-001",               # 系统兜底
}


# ═══════════════════════════════════════════════
#  C-9 告警治理与降噪（回写#238）常量
# ═══════════════════════════════════════════════
# 治理流水线：聚合分组（接入既有 AlertAggregator）+ 依赖抑制（集成 C-7
# IncidentContextEngine.DEFAULT_DEPENDENCY_MAP）+ 动态基线降噪（集成 C-6
# DynamicBaseline）+ 元监控独立冷却（R-08）+ 周复盘 TP/FP/FN 统计（P0-8）。
# 防未来：治理只消费 alert.timestamp <= now 的告警；周统计只取 created_at<=now。

#: 元监控告警 dedup 键前缀（R-08：独立冷却窗口，与业务 dedup 300s 隔离）
META_DEDUP_PREFIX: str = "meta:"

#: 元监控告警 category（meta_monitor 探针告警落此 category）
META_CATEGORY: str = "meta"

#: 治理裁决原因常量（GovernanceDecision.reason）
GOVERNANCE_ALLOWED: str = "allowed"
GOVERNANCE_SUPPRESSED_DEPENDENCY: str = "dependency_suppressed"
GOVERNANCE_SUPPRESSED_BASELINE: str = "baseline_suppressed"
GOVERNANCE_SUPPRESSED_META: str = "meta_cooldown_suppressed"

#: 周长度（秒，7 天）
WEEK_SECONDS: float = 7.0 * 24.0 * 3600.0

#: 周复盘单次拉取告警历史上限（只消费 created_at<=now 的行，防未来）
WEEKLY_LOAD_LIMIT: int = 10000

#: P0-8 周统计达标阈值（目标准确率>95% 且 误报率<5%；按周至少 4 周判定）
WEEKLY_ACCURACY_TARGET: float = 0.95
WEEKLY_FALSE_POSITIVE_RATE_TARGET: float = 0.05

#: 达标判定注释（本周为统计口径实现，至少累计 4 周才作达标结论）
WEEKLY_VERDICT_NOTE: str = "按周统计至少 4 周判定，本周为统计口径实现"

#: 未来点判定容差（秒）：ts 晚于 now 超过此值视为未来点
_GOVERNANCE_FUTURE_EPS: float = 1.0

#: 治理统计指标名（经 get_metrics_collector() 动态注册，禁止改 metrics_collector.py 本体）
METRIC_ALERT_SUPPRESSED_TOTAL: str = "alert_suppressed_total"
METRIC_ALERT_AGGREGATED_TOTAL: str = "alert_aggregated_total"
METRIC_ALERT_ACCURACY: str = "alert_accuracy"
METRIC_ALERT_FALSE_POSITIVE_RATE: str = "alert_false_positive_rate"

#: 本模块治理侧注册的全部指标名（供主控/驾驶舱查询）
GOVERNANCE_REGISTERED_METRICS: List[str] = [
    METRIC_ALERT_SUPPRESSED_TOTAL,
    METRIC_ALERT_AGGREGATED_TOTAL,
    METRIC_ALERT_ACCURACY,
    METRIC_ALERT_FALSE_POSITIVE_RATE,
]


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
    classification : str
        处置分类（TP/FP/auto，空=未打标），P0-8 统计读取源
    runbook_id : str
        A-10（回写#214）显式 Runbook 绑定（P0-4 模板）；由 __post_init__ 归一化
        （显式字段 > details.runbook_id > category 默认映射），贯通落库/查询
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
    # BASE-1/P0-8：处置分类（TP/FP/auto，空=未打标），供 P0-8 统计脚本读取（R-01）
    classification: str = ""
    # A-10（回写#214）：显式 Runbook 绑定；默认空，由 __post_init__ 归一化填充
    runbook_id: str = ""

    def __post_init__(self) -> None:
        """A-10（回写#214）：runbook_id 归一化。

        优先级：显式构造字段 > details["runbook_id"] 透传 > 按 category 默认 Runbook
        （DEFAULT_RUNBOOK_BY_CATEGORY）兜底；并把最终值回填 details["runbook_id"]，
        使落库 details_json 与 runbook_id 列一致（A-4/A-6 历史 details.runbook_id
        告警自动归入显式字段）。防未来：只消费本告警已携带的静态字段，不预测未来事件。
        """
        if not self.runbook_id:
            rid = (self.details or {}).get("runbook_id")
            if rid:
                self.runbook_id = str(rid)
            else:
                self.runbook_id = DEFAULT_RUNBOOK_BY_CATEGORY.get(self.category, "")
        # 显式字段为准，回填 details（保证 runbook_id 列与 details_json 一致）
        if self.runbook_id and (self.details or {}).get("runbook_id") != self.runbook_id:
            self.details["runbook_id"] = self.runbook_id

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
            "classification": self.classification,
            "runbook_id": self.runbook_id,
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
        {"info": "#17a2b8", "warning": "#ffc107", "error": "#dc3545", "critical": "#6f42c1"}.get(alert.level.value, "#6c757d")
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
#  C-9 告警治理与降噪框架（回写#238）
# ═══════════════════════════════════════════════

@dataclass
class GovernanceDecision:
    """治理裁决结果（C-9）：单条告警在投递前经治理流水线后的去向。

    Attributes
    ----------
    allowed : bool
        True=放行投递；False=被治理抑制（不单独告警）。
    reason : str
        裁决原因（GOVERNANCE_ALLOWED / *_SUPPRESSED_* 常量之一）。
    group_key : str
        聚合分组键（同根因链路相似告警归并依据；空=未分组）。
    detail : str
        人可读裁决明细（留痕/日志/测试断言用）。
    """

    allowed: bool
    reason: str = GOVERNANCE_ALLOWED
    group_key: str = ""
    detail: str = ""


class _NoopAlertSink:
    """空告警接收器：供治理侧 DynamicBaseline 注入，吞掉其内部 _fire_alert，
    避免 C-6 异常告警与治理后放行的原告警重复投递。

    C-6 record() 返回值才是治理裁决依据；本 sink 仅负责"不二次发告警"。
    """

    def alert(self, **_kwargs: Any) -> int:
        """吞掉告警（返回 1 表示"已处理"，不真正投递）。"""
        return 1


class AlertGovernor:
    """告警治理与降噪框架（C-9，回写#238）。

    在 AlertManager.send_alert 投递前对每条告警做治理裁决（只消费已发生时间戳，
    严格防未来）。治理流水线（按序裁决，先命中先返回）：

      1. **元监控独立冷却（R-08）**：dedup_key 以 ``meta:`` 开头的告警走独立冷却
         窗口（与业务 dedup 隔离），且**跳过**依赖抑制与动态基线降噪——元监控告警
         不得被业务降噪规则误吞。
      2. **动态基线降噪（集成 C-6 DynamicBaseline）**：告警 ``details.governance``
         携带 ``series``/``value`` 时，喂入 DynamicBaseline.record()。值落在动态基线
         带内（非异常）→ 抑制该静态阈值告警（替代静态阈值误报源）；确证异常 → 放行。
         冷启动样本不足（无法判定）→ 不抑制（不编造判断）。
      3. **依赖抑制（集成 C-7 DEFAULT_DEPENDENCY_MAP）**：同窗内底层（根因）category
         已命中时，其依赖映射可引发的上层 category 告警抑制（不单独告警，仅留痕）。
      4. **聚合分组**：同根因链路相似告警在聚合窗口内归并（暴露 summary 统计；
         实际投递归并由既有 AlertAggregator 承担，本框架计数并暴露 alert_aggregated_total）。

    指标（经 get_metrics_collector() 动态注册，不改 metrics_collector.py 本体）：
      - alert_suppressed_total     （COUNTER）每抑制一条告警 +1（labels=reason）
      - alert_aggregated_total     （COUNTER）每归并一条相似告警 +1
      - alert_accuracy             （GAUGE）周复盘准确率（weekly_alert_stats 落盘）
      - alert_false_positive_rate （GAUGE）周复盘误报率（weekly_alert_stats 落盘）

    Usage:
        gov = AlertGovernor()
        mgr.attach_governor(gov)
        decision = gov.evaluate(alert)   # 由 send_alert 内部调用
        print(gov.summary())
    """

    def __init__(
        self,
        dependency_map: Optional[Dict[str, List[str]]] = None,
        dependency_suppression_enabled: bool = True,
        suppression_window_seconds: float = 60.0,
        aggregation_window_seconds: float = 60.0,
        dynamic_baseline: Any = None,
        baseline_noise_reduction_enabled: bool = False,
        meta_cooldown_seconds: float = 120.0,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        dependency_map : dict | None
            "底层→上层"依赖映射 {底层category: [上层category,...]}；None 用 C-7
            IncidentContextEngine.DEFAULT_DEPENDENCY_MAP（可注入扩展）。
        dependency_suppression_enabled : bool
            是否启用依赖抑制（默认 True；仅同窗底层根因命中时抑制上层，安全）。
        suppression_window_seconds : float
            依赖抑制窗口（秒，>0）：底层根因命中后多久内其上层告警被抑制。
        aggregation_window_seconds : float
            聚合分组窗口（秒，>0）：同组键相似告警在此窗口内计为归并。
        dynamic_baseline : DynamicBaseline | None
            C-6 动态基线实例（降噪 oracle）；None=不做动态基线降噪。
        baseline_noise_reduction_enabled : bool
            是否启用动态基线降噪（默认 False；未配置基线时不抑制，安全）。
        meta_cooldown_seconds : float
            R-08 元监控独立冷却窗口（秒，>0；与业务 dedup 隔离，默认 120s）。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（动态注册，失败不阻断）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。

        Raises
        ------
        ValueError : 窗口/冷却非正数 / dependency_map 非法时抛出。
        """
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._metrics_collector = metrics_collector

        # ── 防御式校验窗口参数 ──
        self._suppression_window: float = self._positive(
            suppression_window_seconds, "suppression_window_seconds")
        self._aggregation_window: float = self._positive(
            aggregation_window_seconds, "aggregation_window_seconds")
        self._meta_cooldown: float = self._positive(
            meta_cooldown_seconds, "meta_cooldown_seconds")

        self._dependency_suppression_enabled = bool(dependency_suppression_enabled)
        self._baseline_noise_reduction_enabled = bool(baseline_noise_reduction_enabled)

        # ── 依赖映射归一化（复用 C-7 DEFAULT_DEPENDENCY_MAP 作缺省）──
        raw_map = dependency_map
        if raw_map is None:
            try:
                raw_map = DEFAULT_DEPENDENCY_MAP  # G2-1 下沉 core/constants（顶部导入）
            except Exception as e:  # noqa: BLE001 —— C-7 不可用时退化为空映射（不抑制依赖）
                logger.warning("加载 C-7 DEFAULT_DEPENDENCY_MAP 失败（依赖抑制按空映射）: %s", e)
                raw_map = {}
        if not isinstance(raw_map, dict):
            raise ValueError(f"dependency_map 必须为 dict，收到 {type(raw_map).__name__}")
        self._dep_map: Dict[str, List[str]] = {}
        for low, ups in raw_map.items():
            if not isinstance(low, str) or not low:
                raise ValueError(f"dependency_map 的 key 必须为非空字符串，收到 {low!r}")
            if not isinstance(ups, (list, tuple)):
                raise ValueError(f"dependency_map[{low!r}] 必须为 list/tuple")
            self._dep_map[low] = [str(u) for u in ups if isinstance(u, str) and u]

        # ── 动态基线降噪 oracle（C-6；注入空告警 sink 防双发）──
        self._baseline: Any = dynamic_baseline
        self._baseline_min_samples: int = 0
        if self._baseline is not None:
            try:
                self._baseline_min_samples = int(self._baseline.to_dict().get("min_samples", 0))
            except Exception as e:  # noqa: BLE001
                logger.warning("读取 DynamicBaseline min_samples 失败（按 0 冷启动判定）: %s", e)
            # C-6 内部 _fire_alert 改投空 sink，避免与治理后放行的原告警重复
            try:
                self._baseline._alert_manager = _NoopAlertSink()
            except Exception as e:  # noqa: BLE001
                logger.warning("注入 DynamicBaseline 空告警 sink 失败（可能双发告警）: %s", e)

        # ── 治理状态（全部按已观测时间戳推进，防未来）──
        self._root_seen: Dict[str, float] = {}        # 底层根因 category -> 命中 ts
        self._meta_dedup: Dict[str, float] = {}        # 元监控 dedup_key -> 上次放行 ts
        self._groups: Dict[str, float] = {}            # 聚合分组键 -> 首次归并 ts
        self._baseline_fed: Dict[str, int] = {}        # 每 series 已喂点数（冷启动判定）

        # ── 治理计数器（内存镜像 + collector）──
        self._allowed_total: int = 0
        self._suppressed_total: int = 0
        self._suppressed_by_dependency: int = 0
        self._suppressed_by_baseline: int = 0
        self._suppressed_by_meta: int = 0
        self._aggregated_total: int = 0

        self._register_metrics()
        logger.debug(
            "AlertGovernor 初始化: dep_map=%s suppression_window=%.0fs "
            "aggregation_window=%.0fs meta_cooldown=%.0fs baseline=%s",
            self._dep_map, self._suppression_window, self._aggregation_window,
            self._meta_cooldown, "on" if self._baseline is not None else "off",
        )

    # ═══════════════════════════════════════════════
    #  治理裁决入口（send_alert 投递前调用）
    # ═══════════════════════════════════════════════
    def evaluate(self, alert: "Alert") -> GovernanceDecision:
        """对单条告警做治理裁决（只消费 alert.timestamp <= now，防未来）。

        Parameters
        ----------
        alert : Alert
            待投递告警（其 timestamp 必须 <= 当前时钟）。

        Returns
        -------
        GovernanceDecision : allowed=True 放行；False=被抑制（不单独告警）。

        Raises
        ------
        ValueError : alert 为未来时间戳时抛出（防未来红线）。
        """
        if alert is None:
            raise ValueError("evaluate: alert 不能为 None")
        now = self._clock()
        ts = float(alert.timestamp)
        if ts > now + _GOVERNANCE_FUTURE_EPS:
            raise ValueError(
                f"拒绝未来告警：ts={ts:.3f} 晚于当前 {now:.3f}（防未来红线）"
            )

        # 1) 元监控独立冷却（R-08）：meta:* 走独立冷却，跳过其余业务降噪
        if self._is_meta_alert(alert):
            if self._meta_in_cooldown(alert, now):
                self._suppress(alert, GOVERNANCE_SUPPRESSED_META,
                               f"meta 独立冷却窗口内: {alert.deduplication_key}")
                return GovernanceDecision(
                    allowed=False, reason=GOVERNANCE_SUPPRESSED_META,
                    detail=f"meta:{alert.deduplication_key} 冷却窗口内",
                )
            # 放行：记录独立冷却时间戳（与业务 dedup 完全隔离）
            self._meta_dedup[alert.deduplication_key] = now
            self._record_allowed(alert)
            return GovernanceDecision(allowed=True, reason=GOVERNANCE_ALLOWED,
                                      detail="meta 告警放行（独立冷却）")

        # 2) 动态基线降噪（集成 C-6）：值落在动态基线内 → 抑制静态阈值误报
        if self._baseline_noise_reduction_enabled and self._baseline is not None:
            if self._baseline_says_normal(alert):
                self._suppress(alert, GOVERNANCE_SUPPRESSED_BASELINE,
                               f"动态基线内（非异常）: {alert.category}")
                return GovernanceDecision(
                    allowed=False, reason=GOVERNANCE_SUPPRESSED_BASELINE,
                    detail="指标值落在动态基线带内，静态阈值告警判为误报",
                )

        # 3) 依赖抑制（集成 C-7）：同窗底层根因命中 → 上层告警抑制
        if self._dependency_suppression_enabled and self._dependency_suppressed(alert, now):
            self._suppress(alert, GOVERNANCE_SUPPRESSED_DEPENDENCY,
                           f"上层依赖被根因抑制: {alert.category}")
            return GovernanceDecision(
                allowed=False, reason=GOVERNANCE_SUPPRESSED_DEPENDENCY,
                detail="同窗底层根因已命中，上层告警仅作关联上下文",
            )

        # 4) 聚合分组：同根因链路相似告警在聚合窗口内归并（计数暴露 summary）
        group_key = self._group_key_of(alert)
        if group_key and self._already_grouped(group_key, now):
            self._aggregated_total += 1
            self._inc_counter(METRIC_ALERT_AGGREGATED_TOTAL, labels={"reason": "group"})
        else:
            self._groups[group_key] = now
        # 非抑制告警即潜在根因：登记其 category 供后续上层依赖抑制
        self._root_seen[alert.category] = now
        self._evict_expired(now)
        self._record_allowed(alert, group_key=group_key)
        return GovernanceDecision(allowed=True, reason=GOVERNANCE_ALLOWED,
                                 group_key=group_key)

    # ═══════════════════════════════════════════════
    #  治理子判定
    # ═══════════════════════════════════════════════
    @staticmethod
    def _is_meta_alert(alert: "Alert") -> bool:
        """是否为元监控告警（dedup_key 以 meta: 开头，或 category=meta）。"""
        return (
            str(alert.deduplication_key).startswith(META_DEDUP_PREFIX)
            or alert.category == META_CATEGORY
        )

    def _meta_in_cooldown(self, alert: "Alert", now: float) -> bool:
        """元监控告警独立冷却窗口内是否已发过（与业务 dedup 隔离）。"""
        last = self._meta_dedup.get(alert.deduplication_key)
        return last is not None and (now - last) < self._meta_cooldown

    def _baseline_says_normal(self, alert: "Alert") -> bool:
        """动态基线降噪 oracle：True=值落在动态基线内（应抑制静态阈值告警）。

        只消费 alert.details.governance.{series,value} 与 alert.timestamp（已发生）。
        冷启动样本不足（无法判定）返回 False（不抑制、不编造）。
        """
        details = alert.details or {}
        gov = details.get("governance") or {}
        series = gov.get("series")
        value = gov.get("value")
        if not series or value is None:
            return False
        try:
            v = float(value)
        except (TypeError, ValueError):
            logger.debug("治理基线降噪 value 非数值，跳过: %r", value)
            return False
        fed_before = self._baseline_fed.get(series, 0)
        try:
            event = self._baseline.record(series, float(alert.timestamp), v)
        except ValueError as e:
            # 未来点/非法点 → 不抑制（不编造判断，留痕）
            logger.debug("治理基线 record 拒绝（不抑制）: %s", e)
            return False
        self._baseline_fed[series] = fed_before + 1
        # C-6 判定异常 → 真异常，放行（不抑制）
        if event is not None:
            return False
        # record() 返回 None：样本已暖（>=min_samples）且值落在基线带内 → 真正常，抑制误报
        if self._baseline_min_samples > 0 and (fed_before + 1) >= self._baseline_min_samples:
            return True
        # 冷启动样本不足 → 无法判定，不抑制
        return False

    def _dependency_suppressed(self, alert: "Alert", now: float) -> bool:
        """依赖抑制：alert.category 是否为某同窗已命中底层根因的上层依赖。"""
        for root_cat, root_ts in list(self._root_seen.items()):
            if (now - root_ts) > self._suppression_window:
                continue  # 根因已出窗，不抑制
            if alert.category in self._dep_map.get(root_cat, []):
                return True
        return False

    def _group_key_of(self, alert: "Alert") -> str:
        """计算聚合分组键（同根因链路相似告警归并依据）。

        优先用 dedup_key 根因前缀（同根因链路）；无 dedup_key 时用 category。
        """
        if alert.deduplication_key:
            # 取根因前缀（冒号前两段），把同链路相似告警归并
            parts = str(alert.deduplication_key).split(":")
            return ":".join(parts[:2]) if len(parts) >= 2 else alert.deduplication_key
        return alert.category

    def _already_grouped(self, group_key: str, now: float) -> bool:
        """同组键是否在聚合窗口内已归并过。"""
        last = self._groups.get(group_key)
        return last is not None and (now - last) < self._aggregation_window

    # ═══════════════════════════════════════════════
    #  计数 / 留痕 / 指标
    # ═══════════════════════════════════════════════
    def _suppress(self, alert: "Alert", reason: str, detail: str) -> None:
        """登记一次抑制（计数器 + collector + 日志留痕）。"""
        self._suppressed_total += 1
        if reason == GOVERNANCE_SUPPRESSED_DEPENDENCY:
            self._suppressed_by_dependency += 1
        elif reason == GOVERNANCE_SUPPRESSED_BASELINE:
            self._suppressed_by_baseline += 1
        elif reason == GOVERNANCE_SUPPRESSED_META:
            self._suppressed_by_meta += 1
        self._inc_counter(METRIC_ALERT_SUPPRESSED_TOTAL, labels={"reason": reason})
        logger.info("告警治理抑制: reason=%s category=%s dedup=%s (%s)",
                    reason, alert.category, alert.deduplication_key, detail)

    def _record_allowed(self, alert: "Alert", group_key: str = "") -> None:
        """登记一次放行。"""
        self._allowed_total += 1

    def _evict_expired(self, now: float) -> None:
        """淘汰出窗的根因/分组记录（按已观测水位线，防无界增长）。"""
        for root_cat in [k for k, v in self._root_seen.items()
                         if (now - v) > self._suppression_window]:
            del self._root_seen[root_cat]
        for gk in [k for k, v in self._groups.items()
                   if (now - v) > self._aggregation_window]:
            del self._groups[gk]

    def summary(self) -> Dict[str, Any]:
        """治理面板快照（JSON 可序列化）：抑制/归并/放行计数 + 窗口参数。"""
        return {
            "allowed_total": self._allowed_total,
            "suppressed_total": self._suppressed_total,
            "suppressed_by_dependency": self._suppressed_by_dependency,
            "suppressed_by_baseline": self._suppressed_by_baseline,
            "suppressed_by_meta_cooldown": self._suppressed_by_meta,
            "aggregated_total": self._aggregated_total,
            "suppression_window_seconds": self._suppression_window,
            "aggregation_window_seconds": self._aggregation_window,
            "meta_cooldown_seconds": self._meta_cooldown,
            "dependency_suppression_enabled": self._dependency_suppression_enabled,
            "baseline_noise_reduction_enabled": self._baseline_noise_reduction_enabled,
            "root_categories_seen": sorted(self._root_seen.keys()),
        }

    # ═══════════════════════════════════════════════
    #  指标动态注册（不改 metrics_collector.py 本体）
    # ═══════════════════════════════════════════════
    def _register_metrics(self) -> None:
        """经 get_metrics_collector() 动态注册治理指标（失败仅记 warning）。"""
        col = self._resolve_collector()
        if col is None:
            return
        # MetricType 已由 core/constants 顶部导入（G2-1 下沉）
        helps = {
            METRIC_ALERT_SUPPRESSED_TOTAL: "Total alerts suppressed by governance (dependency/baseline/meta-cooldown)",
            METRIC_ALERT_AGGREGATED_TOTAL: "Total similar alerts merged by governance aggregation grouping",
            METRIC_ALERT_ACCURACY: "Weekly alert accuracy TP/(TP+FP) (P0-8)",
            METRIC_ALERT_FALSE_POSITIVE_RATE: "Weekly alert false-positive rate FP/(TP+FP) (P0-8)",
        }
        for name in GOVERNANCE_REGISTERED_METRICS:
            try:
                register = getattr(col, "register", None)
                if callable(register):
                    mtype = (MetricType.GAUGE if name in (METRIC_ALERT_ACCURACY,
                             METRIC_ALERT_FALSE_POSITIVE_RATE) else MetricType.COUNTER)
                    register(name, mtype, help=helps.get(name, name), label_names=[])
            except Exception as e:  # noqa: BLE001 —— 单个注册失败不阻断其余
                logger.warning("治理指标 %s 注册失败（继续其余）: %s", name, e)

    def _resolve_collector(self) -> Any:
        """解析指标采集器：构造注入优先，其次 core.registry 全局注册表；不可用返回 None（仅记 warning）。"""
        col = self._metrics_collector
        if col is not None:
            return col
        try:
            from myquant.core.registry import get_collector
            col = get_collector()
            self._metrics_collector = col
            return col
        except Exception as e:  # noqa: BLE001
            logger.warning("获取 metrics_collector 失败（治理指标未写入）: %s", e)
            return None

    def _inc_counter(self, name: str, amount: float = 1.0,
                     labels: Optional[Dict[str, str]] = None) -> None:
        """inc_counter 一个治理计数器；采集器不可用仅记 warning 不阻断。"""
        col = self._resolve_collector()
        if col is None:
            return
        try:
            col.inc_counter(name, amount=amount, labels=labels or {})
        except Exception as e:  # noqa: BLE001
            logger.warning("治理指标 %s 递增失败（不阻断治理）: %s", name, e)

    def set_accuracy_gauges(self, accuracy: Optional[float],
                            fp_rate: Optional[float]) -> None:
        """周复盘后落盘准确率/误报率 GAUGE（None 落 0.0 占位）。"""
        col = self._resolve_collector()
        if col is None:
            return
        try:
            col.set_gauge(METRIC_ALERT_ACCURACY,
                          0.0 if accuracy is None else float(accuracy))
            col.set_gauge(METRIC_ALERT_FALSE_POSITIVE_RATE,
                          0.0 if fp_rate is None else float(fp_rate))
        except Exception as e:  # noqa: BLE001
            logger.warning("落盘治理准确率指标失败（不阻断）: %s", e)

    # ═══════════════════════════════════════════════
    #  防御式校验工具
    # ═══════════════════════════════════════════════
    @staticmethod
    def _positive(value: float, name: str) -> float:
        """正数校验：非法抛 ValueError（防御式，禁止静默落到错误窗口）。"""
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{name} 必须为数值，收到 {value!r}") from e
        if v <= 0:
            raise ValueError(f"{name} 必须为正数，收到 {v!r}")
        return v

    def __repr__(self) -> str:
        return (
            f"<AlertGovernor suppressed={self._suppressed_total} "
            f"allowed={self._allowed_total} aggregated={self._aggregated_total}>"
        )


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

    def __init__(
        self,
        dedup_window_seconds: float = 300.0,
        max_history: int = 1000,
        store: Optional["AlertStore"] = None,
        store_enabled: bool = True,
    ):
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

        # BASE-1 告警持久化：发送落库、确认/解决/升级同步库、库内存双读
        self._store: Optional["AlertStore"] = store
        self._store_enabled: bool = store_enabled

        # A-9 告警通道冗余：未投递计数 + 逐通道投递顺序留痕（降级顺序可观测/元监控）
        self._undelivered_count: int = 0
        self._last_channel_attempts: List[Dict[str, Any]] = []

        # C-9 告警治理与降噪（回写#238）：治理入口（None=不治理，保持既有行为）。
        # 由 attach_governor() 装配；全局单例在 get_alert_manager() 按 settings 接入。
        self._governor: Optional[AlertGovernor] = None

    def attach_store(self, store: "AlertStore") -> "AlertManager":
        """绑定告警持久化存储（BASE-1：发送落库/状态同步/双读）。"""
        self._store = store
        return self

    def attach_governor(self, governor: AlertGovernor) -> "AlertManager":
        """装配 C-9 告警治理与降噪框架（send_alert 投递前裁决）。

        Parameters
        ----------
        governor : AlertGovernor
            治理框架实例（聚合分组/依赖抑制/动态基线降噪/元监控独立冷却）。

        Returns
        -------
        AlertManager : self

        Raises
        ------
        ValueError : governor 非 AlertGovernor 实例时抛出。
        """
        if not isinstance(governor, AlertGovernor):
            raise ValueError(
                f"governor 必须为 AlertGovernor 实例，收到 {type(governor).__name__}"
            )
        self._governor = governor
        logger.info("AlertManager 已装配告警治理框架: %s", governor)
        return self

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

        # C-9 告警治理与降噪（回写#238）：投递前治理裁决（聚合分组/依赖抑制/
        # 动态基线降噪/元监控独立冷却）。被抑制的告警不单独投递（不更新 dedup、
        # 不落库、不通道发送），仅留治理计数。governor=None 时保持既有行为不变。
        if self._governor is not None:
            try:
                decision = self._governor.evaluate(alert)
            except ValueError as e:
                # 防未来红线：未来告警拒绝投递（不静默吞，记 error 留痕）
                logger.error("告警治理拒绝未来告警（不投递）: %s", e)
                return 0
            if not decision.allowed:
                logger.debug(
                    "告警被治理抑制: reason=%s category=%s dedup=%s",
                    decision.reason, alert.category, alert.deduplication_key,
                )
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

        # BASE-1 告警落库（失败不阻断投递：告警投递优先，落库失败记 error 留痕）
        if self._store is not None and self._store_enabled:
            try:
                self._store.save_alert(alert)
                # A-10（回写#214）：runbook_id 贯通落库——save_alert 仅写主体字段，
                # 显式 runbook_id 经既有 set_runbook_id 回填 alerts.runbook_id 列
                # （幂等；绑定失败仅记 error，不阻断告警投递）。
                if alert.runbook_id:
                    try:
                        self._store.set_runbook_id(alert.alert_id, alert.runbook_id)
                    except Exception as re:  # noqa: BLE001
                        logger.error(
                            f"告警 runbook_id 落库失败（不阻断投递）: "
                            f"alert_id={alert.alert_id}: {re}"
                        )
            except Exception as e:
                logger.error(
                    f"告警落库失败（不阻断投递）: alert_id={alert.alert_id}: {e}",
                    exc_info=True,
                )

        # 通道路由 + 多通道分发（A-9：按配置顺序逐通道尝试，记录投递结果=降级顺序留痕）
        channels = self._route_channels(alert.level)
        sent_count = 0
        attempts: List[Dict[str, Any]] = []
        for channel in channels:
            ok = False
            try:
                ok = bool(channel.send(alert))
            except Exception as e:
                # 通道发送抛异常：记 error 并计入通道失败统计（不阻断其余通道）
                logger.error(f"告警通道 [{channel.name}] 发送异常: {e}", exc_info=True)
                try:
                    channel._record_failure(str(e))
                except Exception:
                    pass
            if ok:
                sent_count += 1
            attempts.append({"name": channel.name, "success": ok})

        # A-9：逐通道投递顺序留痕（供元监控/单测校验降级顺序：主通道先试、失败转备用）
        self._last_channel_attempts = attempts

        # A-9 P0 冗余保证：CRITICAL 告警必须至少 2 通道可达；不足记 warning 留痕（元监控）
        if alert.level == SeverityLevel.CRITICAL and sent_count < 2:
            logger.warning(
                f"[A-9] P0 告警仅 {sent_count} 通道可达（<2），冗余不足: {alert.summary()}"
            )

        if sent_count == 0:
            # KP-P5-05 + A-9: 未投递回退写 logs/alerts_undelivered.log（含完整告警内容，不静默丢失）
            self._undelivered_count += 1
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
        runbook_id: str = "",
    ) -> int:
        """便捷方法：创建并发送告警。

        Args:
            level: 告警级别。
            message: 告警消息。
            source: 告警来源（模块/组件名）。
            category: 告警分类（决定默认 Runbook 兜底）。
            details: 详细信息；内含 runbook_id 时自动提升为显式字段（A-10）。
            deduplication_key: 去重键。
            runbook_id: A-10 显式 Runbook 绑定（P0-4）；空则按 details.runbook_id /
                category 默认映射兜底。

        Returns:
            int: 成功发送的通道数量。
        """
        alert = Alert(
            level=level,
            message=message,
            source=source,
            category=category,
            details=details or {},
            deduplication_key=deduplication_key,
            runbook_id=runbook_id,
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

        # A-8 交易异常告警闭环：订阅 4 类交易异常事件，事件流实时触发告警（R-02）
        hid6 = bus.subscribe(
            EventType.ORDER_REJECTED,
            lambda e: self.handle_trade_exception_event(e),
        )
        handler_ids.append(hid6)

        hid7 = bus.subscribe(
            EventType.ORDER_FAILED,
            lambda e: self.handle_trade_exception_event(e),
        )
        handler_ids.append(hid7)

        hid8 = bus.subscribe(
            EventType.FILL_REPORT_ERROR,
            lambda e: self.handle_trade_exception_event(e),
        )
        handler_ids.append(hid8)

        hid9 = bus.subscribe(
            EventType.EMERGENCY_LIQUIDATION,
            lambda e: self.handle_trade_exception_event(e),
        )
        handler_ids.append(hid9)

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

    def handle_trade_exception_event(self, event: Any) -> Optional[int]:
        """
        处理交易异常事件（A-8 交易异常告警闭环，触发模式 EVENT）

        由事件流实时触发告警（不等周期巡检，R-02），覆盖：
          - ORDER_REJECTED        → ERROR（P1），含 symbol/拒单原因（reject_reason）
          - ORDER_FAILED         → ERROR（P1），含 symbol/order_id/失败原因
          - FILL_REPORT_ERROR    → ERROR（P1），含 symbol/order_id/回报异常原因
          - EMERGENCY_LIQUIDATION→ CRITICAL（P0），紧急强平最高优先级告警

        去重键 ``trade:<event_type>:<order_id>``：同一订单的同类异常在去重窗口内
        只告警一次，避免同单重复刷屏；order_id 缺失时退化为 symbol/unknown。

        防未来：只消费已发生的事件（event_ts 为业务真实发生时刻），不预测。

        Parameters
        ----------
        event : OrderEvent | SystemErrorEvent | Event
            交易异常事件（强类型 OrderEvent 或通用 Event；兼容 data 字典兜底取字段）

        Returns
        -------
        int | None : 成功投递的通道数量；处理异常时返回 None（不阻断事件流）
        """
        from myquant.core.event_bus import EventType

        try:
            et = getattr(event, "event_type", None)
            et_name: str = et.value if isinstance(et, EventType) else str(et)
            level = TRADE_EXCEPTION_ALERT_LEVEL_MAP.get(et_name, SeverityLevel.ERROR)

            # 强类型字段优先，通用 Event 的 data 字典兜底
            data = getattr(event, "data", None) or {}
            symbol = getattr(event, "symbol", "") or (data.get("symbol") or "")
            order_id = getattr(event, "order_id", "") or ""
            trade_id = getattr(event, "trade_id", "") or ""
            reason = (
                getattr(event, "reject_reason", "")
                or getattr(event, "error", "")
                or data.get("reason")
                or data.get("error")
                or data.get("reject_reason")
                or ""
            )
            reason = str(reason)

            # 按事件类型生成人类可读消息
            if et_name == "order.rejected":
                message = (
                    f"订单被拒单（{symbol or 'N/A'}）: {reason or '券商/风控拒单，无原因'}"
                )
            elif et_name == "order.failed":
                message = (
                    f"订单发送/执行失败（{symbol or 'N/A'}, order_id={order_id or 'N/A'}）: "
                    f"{reason or '未知失败原因'}"
                )
            elif et_name == "fill.report_error":
                message = (
                    f"成交回报异常（{symbol or 'N/A'}, order_id={order_id or 'N/A'}）: "
                    f"{reason or '成交回报对账/上报失败'}"
                )
            else:  # emergency.liquidation → P0
                message = (
                    f"[P0] 触发紧急强平（{symbol or 'N/A'}, order_id={order_id or 'N/A'}）: "
                    f"{reason or '风险敞口超限，已自动强平'}"
                )

            dedup_key = f"trade:{et_name}:{order_id or symbol or 'unknown'}"

            alert = Alert(
                level=level,
                message=message,
                source="live_engine",
                category="trade",
                details={
                    "event_type": et_name,
                    "symbol": symbol,
                    "order_id": order_id,
                    "trade_id": trade_id,
                    "reason": reason,
                },
                deduplication_key=dedup_key,
            )
            return self.send_alert(alert)

        except Exception as e:
            logger.error(f"处理交易异常事件告警失败: {e}", exc_info=True)
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

            # BASE-1 恢复通知：流水线完成（健康恢复信号）→ 同 symbol/data_type
            # 的未解决数据质量告警发 INFO 恢复通知并置 resolved（断流→恢复成对记录）
            if status == "completed":
                try:
                    self._send_recovery_for(symbol, data_type)
                except Exception as e:
                    logger.error(f"恢复通知处理异常: {e}", exc_info=True)

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

    def _send_recovery_for(self, symbol: str, data_type: str) -> int:
        """
        BASE-1 恢复通知：健康恢复信号到达时，对同 symbol/data_type 的
        未解决数据质量告警自动发 INFO 恢复通知并置 resolved（成对记录）。

        匹配规则：
          - 内存中 category=data_quality、未解决、dedup_key 以
            agg:/qa:/pipeline_fail:/pipeline_quality: 开头的告警
          - symbol/data_type 非空时须出现在 dedup_key 中

        Parameters
        ----------
        symbol : str
            标的代码（可能为空，为空则不限标的）
        data_type : str
            数据类型（可能为空，为空则不限类型）

        Returns
        -------
        int : 恢复通知数量
        """
        prefixes = ("agg:", "qa:", "pipeline_fail:", "pipeline_quality:")
        recovered = 0
        for alert in list(self._history):
            if alert.resolved or alert.category != "data_quality":
                continue
            dk = alert.deduplication_key or ""
            if not any(dk.startswith(p) for p in prefixes):
                continue
            if symbol and symbol not in dk:
                continue
            if data_type and data_type not in dk:
                continue
            recovery = Alert(
                level=SeverityLevel.INFO,
                message=f"[恢复] {alert.message}（健康已恢复，置 resolved）",
                source=alert.source,
                category="data_quality",
                details=dict(alert.details, original_alert_id=alert.alert_id, recovery=True),
                deduplication_key=f"recovered:{alert.deduplication_key}",
                parent_alert_id=alert.alert_id,
            )
            self.send_alert(recovery, dedup_enabled=False)
            # 置原告警 resolved（内存 + 库）
            alert.resolved = True
            alert.acknowledged = True
            self._sync_store_status(alert.alert_id, "resolved")
            recovered += 1
            logger.info(
                f"恢复通知: {alert.alert_id} 已置 resolved, "
                f"recovery_key=recovered:{alert.deduplication_key}"
            )
        return recovered

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
                    # A-10（回写#214）：升级状态机不依赖通道投递是否成功——判定只取决于
                    # "WARNING 超时未确认"；通道失败另有 A-9 未投递落盘兜底（_write_undelivered），
                    # 不影响升级状态流转与落库。原 sent>0 门控在"无通道/纯内存"时永不升级，
                    # 与 A-10 验收（超时未确认→升级 ERROR 且落库 escalated 标记）相悖，故移除。
                    alert.acknowledged = True  # 原告警已确认/已升级（避免重复升级）
                    # BASE-1：升级同步库（原告警置 acked + escalated=1）
                    if self._store is not None and self._store_enabled:
                        try:
                            self._store.update_status(alert.alert_id, "acked")
                            self._store.mark_escalated(alert.alert_id)
                        except Exception as e:
                            logger.error(f"升级同步库失败: {alert.alert_id}: {e}")
                    escalated += 1
                    logger.warning(
                        f"告警升级: {alert.alert_id} WARNING -> ERROR (通道投递={sent})"
                    )

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

    @staticmethod
    def _alert_from_row(row: Dict[str, Any]) -> Alert:
        """由告警存储行（dict）重建 Alert（库内存双读的"库"侧还原）。

        升级标记 escalated=1 时 escalated_from 以 WARNING 近似还原
        （库仅存标志位，不存原级别；历史展示以 is_escalated 为准）。
        """
        try:
            level = SeverityLevel(row["level"])
        except ValueError:
            level = SeverityLevel.WARNING
        alert = Alert(
            level=level,
            message=row["message"],
            source=row["source"],
            category=row["category"],
            details=row.get("details", {}),
            deduplication_key=row.get("deduplication_key", ""),
            timestamp=row.get("created_at", 0.0),
            alert_id=row.get("alert_id", ""),
            acknowledged=row.get("status", "open") in ("acked", "resolved"),
            resolved=row.get("status", "open") == "resolved",
        )
        alert.classification = row.get("classification", "")
        # A-10（回写#214）：runbook_id 随行还原（库内存双读"库"侧一致展示）
        alert.runbook_id = row.get("runbook_id") or ""
        if row.get("escalated"):
            alert.escalated_from = SeverityLevel.WARNING
        return alert

    def _merge_store_rows(
        self,
        memory_alerts: List[Alert],
        limit: int,
        level: Optional[SeverityLevel] = None,
        category: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Alert]:
        """库内存双读合并：内存结果不足 limit 时读库补足（按 alert_id 去重）。"""
        if self._store is None or not self._store_enabled or len(memory_alerts) >= limit:
            return memory_alerts
        try:
            db_rows = self._store.load_history(
                limit=limit,
                level=level.value if level else None,
                category=category,
                status=status,
            )
        except Exception as e:
            logger.error(f"告警历史读库失败（回退内存）: {e}")
            return memory_alerts
        known_ids = {a.alert_id for a in memory_alerts}
        merged = list(memory_alerts)
        for row in db_rows:
            if row["alert_id"] not in known_ids:
                merged.append(self._alert_from_row(row))
                known_ids.add(row["alert_id"])
                if len(merged) >= limit:
                    break
        return merged

    def get_history(
        self,
        level: Optional[SeverityLevel] = None,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[Alert]:
        """查询告警历史（库内存双读：内存实时 + 库权威补足）"""
        results = []
        for alert in reversed(self._history):
            if level and alert.level != level:
                continue
            if category and alert.category != category:
                continue
            results.append(alert)
            if len(results) >= limit:
                break
        return self._merge_store_rows(results, limit, level=level, category=category)

    def get_unacknowledged(self, limit: int = 50) -> List[Alert]:
        """获取未确认的告警（库内存双读）"""
        mem = [a for a in reversed(self._history) if not a.acknowledged and not a.resolved][:limit]
        return self._merge_store_rows(mem, limit, status="open")

    def get_escalated(self, limit: int = 50) -> List[Alert]:
        """获取升级告警（库内存双读）"""
        mem = [a for a in reversed(self._history) if a.is_escalated][:limit]
        if self._store is not None and self._store_enabled and len(mem) < limit:
            try:
                db_rows = self._store.load_escalated(limit=limit)
            except Exception as e:
                logger.error(f"升级告警读库失败（回退内存）: {e}")
                db_rows = []
            known_ids = {a.alert_id for a in mem}
            for row in db_rows:
                if row["alert_id"] not in known_ids:
                    mem.append(self._alert_from_row(row))
                    known_ids.add(row["alert_id"])
                    if len(mem) >= limit:
                        break
        return mem

    def acknowledge(self, alert_id: str) -> bool:
        """确认告警（内存 + 同步库）"""
        for alert in self._history:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                self._sync_store_status(alert_id, "acked")
                return True
        # 内存未命中（重启后仅库有）也尝试落库
        if self._store is not None and self._store_enabled:
            try:
                return self._store.update_status(alert_id, "acked")
            except Exception as e:
                logger.error(f"确认告警落库失败: {alert_id}: {e}")
        return False

    def resolve(self, alert_id: str) -> bool:
        """解决告警（内存 + 同步库）"""
        for alert in self._history:
            if alert.alert_id == alert_id:
                alert.resolved = True
                alert.acknowledged = True
                self._sync_store_status(alert_id, "resolved")
                return True
        if self._store is not None and self._store_enabled:
            try:
                return self._store.update_status(alert_id, "resolved")
            except Exception as e:
                logger.error(f"解决告警落库失败: {alert_id}: {e}")
        return False

    def _sync_store_status(self, alert_id: str, status: str) -> None:
        """同步告警状态到库（BASE-1：确认/解决同步库；失败记 error 不阻断）"""
        if self._store is not None and self._store_enabled:
            try:
                self._store.update_status(alert_id, status)
            except Exception as e:
                logger.error(f"告警状态同步库失败: alert_id={alert_id}, status={status}: {e}")

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
            "store_enabled": self._store is not None and self._store_enabled,
            # A-9 告警通道冗余元监控计数
            "undelivered_count": self._undelivered_count,
            "last_channel_attempts": self._last_channel_attempts,
            # C-9 告警治理与降噪面板（governor=None 时为 None，未治理）
            "governance": self._governor.summary() if self._governor is not None else None,
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
            # BASE-1：默认绑定告警持久化存储（发送落库/状态同步/双读）；
            # store 初始化失败降级为纯内存，不阻断告警功能
            try:
                from myquant.core.alert_store import AlertStore
                _global_alert_manager.attach_store(AlertStore())
            except Exception as e:
                logger.warning(f"告警持久化存储初始化失败（降级为纯内存）: {e}")
            # 默认注册控制台和日志通道
            _global_alert_manager.add_channel(ConsoleAlertChannel())
            _global_alert_manager.add_channel(LogAlertChannel())
            # KP-P5-05: 按 notification.channels 装配 Email/Webhook 通道
            configure_notification_channels(_global_alert_manager)
        return _global_alert_manager


# G2-5：告警管理器工厂注册（模块导入即注册；live/scheduler 经 core.registry.get_component("alert_manager") 取用，
# L4→L0 合法，消除 EX-13E 装配违规；reset 后工厂懒建重建，无 stale）
register_component("alert_manager", get_alert_manager)


def configure_notification_channels(manager: AlertManager) -> int:
    """KP-P5-05 + BASE-3: 按配置装配告警通道与告警行为（改 settings.yaml 生效）。

    BASE-3 扩展：
      - monitoring.alerts 装配 AlertManager（store_enabled 落库开关、
        dedup_window_seconds 去重窗口、AlertRule 聚合/升级参数）
      - monitoring.notification 装配 Email/Webhook 通道（原有）
    """
    added = 0
    # BASE-3：告警行为装配（独立 try，失败不阻断通道装配）
    added += _apply_alert_config(manager)
    # C-9：告警治理与降噪框架装配（依赖抑制/聚合分组/元监控冷却/动态基线降噪开关）
    try:
        added += _apply_governance_config(manager)
    except Exception as e:  # noqa: BLE001
        logger.warning("治理框架装配调用失败（不阻断通道装配）: %s", e)
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
        # Webhook 通道（feishu / wecom / dingtalk；A-9 告警通道冗余，按配置顺序装配）
        for wh_name in ("feishu", "wecom", "dingtalk"):
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


def _apply_alert_config(manager: AlertManager) -> int:
    """
    BASE-3：按 monitoring.alerts 装配告警行为（改 settings.yaml 生效，无需改代码）。

    装配项：
      1. store_enabled      → 告警落库开关（BASE-1；false 时纯内存不落库）
      2. dedup_window_seconds → 去重窗口（AlertManager._dedup_window）
      3. aggregation/escalation 三参数 → AlertRule（聚合窗口/升级时限/升级开关）

    失败记 warning 不阻断（装配是增强项，告警主功能优先）。

    Returns
    -------
    int : 成功装配的项数（0~6）
    """
    applied = 0
    try:
        from myquant.core.settings import get_monitoring_config
        alerts_cfg = get_monitoring_config().get("alerts") or {}
    except Exception as e:
        logger.warning(f"监控配置读取失败（保持默认告警行为）: {e}")
        return 0

    try:
        # 1. 落库开关
        store_enabled = alerts_cfg.get("store_enabled", True)
        if isinstance(store_enabled, bool):
            manager._store_enabled = store_enabled
            applied += 1
        # 2. 去重窗口
        dedup = alerts_cfg.get("dedup_window_seconds")
        if isinstance(dedup, (int, float)) and dedup > 0:
            manager._dedup_window = float(dedup)
            applied += 1
        # 3. AlertRule 聚合/升级参数
        rule = AlertRule.default_rule()
        rule_changed = False
        agg = alerts_cfg.get("aggregation_window_seconds")
        if isinstance(agg, (int, float)) and agg > 0:
            rule.aggregation_window = float(agg)
            rule_changed = True
        esc_t = alerts_cfg.get("escalation_time_seconds")
        if isinstance(esc_t, (int, float)) and esc_t > 0:
            rule.escalation_time = float(esc_t)
            rule_changed = True
        esc_on = alerts_cfg.get("escalation_enabled")
        if isinstance(esc_on, bool):
            rule.escalation_enabled = esc_on
            rule_changed = True
        if rule_changed:
            manager.set_alert_rule(rule)
            applied += 1
        if applied:
            logger.info(
                f"[config] 告警行为已按 monitoring.alerts 装配: "
                f"store_enabled={manager._store_enabled}, "
                f"dedup_window={manager._dedup_window:.0f}s, "
                f"aggregation={rule.aggregation_window:.0f}s, "
                f"escalation_time={rule.escalation_time:.0f}s, "
                f"escalation_enabled={rule.escalation_enabled}"
            )
    except Exception as e:
        logger.warning(f"告警行为装配失败（保持默认）: {e}")
    return applied


def _apply_governance_config(manager: AlertManager) -> int:
    """C-9（回写#238）：按 monitoring.alerts 装配告警治理与降噪框架（改 settings 生效）。

    装配项（monitoring.alerts 段新增降噪键，只增不改既有键；R-04 阈值纪律）：
      - governance_dependency_suppression_enabled      依赖抑制开关（集成 C-7 依赖映射）
      - governance_dependency_suppression_window_seconds 依赖抑制窗口（秒）
      - governance_aggregation_group_window_seconds     聚合分组窗口（秒）
      - governance_meta_cooldown_seconds                R-08 元监控独立冷却窗口（与业务 dedup 隔离）
      - governance_baseline_noise_reduction_enabled    动态基线降噪开关（集成 C-6；默认关）

    失败记 warning 不阻断（治理是增强项，告警主功能优先；装配失败=不治理）。

    Parameters
    ----------
    manager : AlertManager
        告警管理器（attach_governor 装配）。

    Returns
    -------
    int : 成功装配=1，未装配=0。
    """
    try:
        from myquant.core.settings import get_settings
        # get_monitoring_config() 对 alerts 段有白名单（只透传 6 个既有键），
        # C-9 新增降噪键须读原始 monitoring.alerts 块（不改 settings.py 白名单）。
        raw_alerts = (get_settings().get_dict("monitoring", {}) or {}).get("alerts") or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("监控配置读取失败（不装配治理框架）: %s", e)
        return 0

    try:
        dep_on = raw_alerts.get("governance_dependency_suppression_enabled", True)
        dep_win = raw_alerts.get("governance_dependency_suppression_window_seconds", 60)
        agg_win = raw_alerts.get("governance_aggregation_group_window_seconds", 60)
        meta_cd = raw_alerts.get("governance_meta_cooldown_seconds", 120)
        base_on = raw_alerts.get("governance_baseline_noise_reduction_enabled", False)

        def _f(v: Any, dflt: float) -> float:
            return float(v) if isinstance(v, (int, float)) else dflt

        governor = AlertGovernor(
            dependency_suppression_enabled=bool(dep_on),
            suppression_window_seconds=_f(dep_win, 60.0),
            aggregation_window_seconds=_f(agg_win, 60.0),
            meta_cooldown_seconds=_f(meta_cd, 120.0),
            baseline_noise_reduction_enabled=bool(base_on),
            # 动态基线实例由监控侧（C-6）另行注入；此处仅开/关降噪开关
            dynamic_baseline=None,
        )
        manager.attach_governor(governor)
        logger.info(
            "[config] 告警治理框架已按 monitoring.alerts 装配: "
            "dep_suppression=%s dep_window=%.0fs agg_window=%.0fs "
            "meta_cooldown=%.0fs baseline_nr=%s",
            dep_on, governor._suppression_window, governor._aggregation_window,
            governor._meta_cooldown, base_on,
        )
        return 1
    except Exception as e:  # noqa: BLE001
        logger.warning("告警治理框架装配失败（保持既有告警行为）: %s", e)
        return 0


def weekly_alert_stats(
    store: "AlertStore",
    week_end: Optional[float] = None,
    fi_records: Optional[Sequence[Dict[str, Any]]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """C-9 / P0-8 周告警复盘统计：TP/FP/FN + 准确率/误报率/检出率 + 达标判定。

    按周从 AlertStore.load_history 取告警，依据 classification 字段（TP/FP/auto）
    与周时间窗口计算（严格防未来：只消费 created_at <= now 的行）。

    口径（P0-8 ①，与 measurability.ClassificationStats 同口径）：
      - 准确率 accuracy        = TP/(TP+FP)
      - 误报率 false_positive_rate = FP/(TP+FP)
      - 检出率 detection_rate  = TP/(TP+FN)（FN 由 FI 故障注入主动测量；本周口径 FN 默认 0）
      - 达标 passed = 准确率 > 0.95 且 误报率 < 0.05

    Parameters
    ----------
    store : AlertStore
        告警持久化存储（BASE-1）。
    week_end : float | None
        周窗口右端（epoch 秒）；None 取当前时钟。防未来：week_end 钳制到 <= now。
    fi_records : list[dict] | None
        故障注入记录（FN 主动测量输入，每条含 captured: bool）；None 时 FN=0。
    clock : Callable[[], float] | None
        当前时间戳函数（测试注入）；None 用 time.time。

    Returns
    -------
    dict : 周统计（周起止/TP/FP/FN/auto/unclassified/准确率/误报率/检出率/
        达标判定/目标阈值/注释/公式）。样本不足时比率为 None（诚实标注不造数）。

    Raises
    ------
    ValueError : store 为 None 时抛出。
    """
    if store is None:
        raise ValueError("weekly_alert_stats: store 不能为 None")
    now = (clock if clock is not None else time.time)()
    end = float(week_end) if week_end is not None else now
    # 防未来：周右端不得晚于当前时钟
    end = min(end, now)
    start = end - WEEK_SECONDS

    rows = store.load_history(limit=WEEKLY_LOAD_LIMIT)
    tp = fp = auto = unclassified = 0
    for row in rows:
        created = row.get("created_at")
        if created is None:
            continue
        try:
            ts = float(created)
        except (TypeError, ValueError):
            continue
        if ts > now + _GOVERNANCE_FUTURE_EPS:
            continue  # 防未来：跳过未来时间戳
        if ts < start or ts > end:
            continue
        label = str(row.get("classification") or "").strip().upper()
        if label == "TP":
            tp += 1
        elif label == "FP":
            fp += 1
        elif label == "AUTO":
            auto += 1
        else:
            unclassified += 1

    # FN：由 FI 故障注入主动测量（复用 measurability.ClassificationStats.count_fn）
    fn = 0
    if fi_records is not None:
        try:
            from myquant.core.measurability import ClassificationStats
            fn = ClassificationStats.count_fn(fi_records)
        except Exception as e:  # noqa: BLE001
            logger.warning("统计 FN 失败（按 0 计）: %s", e)

    tp_plus_fp = tp + fp
    tp_plus_fn = tp + fn
    accuracy = round(tp / tp_plus_fp, 6) if tp_plus_fp > 0 else None
    fp_rate = round(fp / tp_plus_fp, 6) if tp_plus_fp > 0 else None
    detection_rate = round(tp / tp_plus_fn, 6) if tp_plus_fn > 0 else None

    # 达标判定：准确率>0.95 且 误报率<0.05（两者皆可计算才判；样本不足诚实判 False）
    if accuracy is not None and fp_rate is not None:
        passed = (accuracy > WEEKLY_ACCURACY_TARGET
                  and fp_rate < WEEKLY_FALSE_POSITIVE_RATE_TARGET)
    else:
        passed = False

    return {
        "week_start": round(start, 3),
        "week_end": round(end, 3),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "auto": auto,
        "unclassified": unclassified,
        "accuracy": accuracy,
        "false_positive_rate": fp_rate,
        "detection_rate": detection_rate,
        "accuracy_target": WEEKLY_ACCURACY_TARGET,
        "false_positive_rate_target": WEEKLY_FALSE_POSITIVE_RATE_TARGET,
        "passed": passed,
        "note": WEEKLY_VERDICT_NOTE,
        "formula": {
            "accuracy": "TP/(TP+FP)",
            "false_positive_rate": "FP/(TP+FP)",
            "detection_rate": "TP/(TP+FN)",
        },
    }


def _write_undelivered(alert: Alert) -> None:
    """KP-P5-05 + A-9: 告警未通过任何通道投递时回退写 logs/alerts_undelivered.log。

    A-9 完善：落盘含完整告警内容（级别/来源/分类/去重键/消息/details JSON，
    含 symbol/order_id/原因等关键字段），不静默丢失任何上下文。
    写入失败记 error 不阻断主流程（告警投递已尽力，落盘是最后兜底）。
    """
    try:
        from myquant.core.settings import get_settings
        log_dir = get_settings().project_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alert.timestamp))}] "
            f"[{alert.level.value}] source={alert.source} "
            f"category={alert.category} dedup_key={alert.deduplication_key} "
            f"message={alert.message} "
            f"details={json.dumps(alert.details, ensure_ascii=False)}\n"
        )
        with open(log_dir / "alerts_undelivered.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as e:
        logger.error(f"写入未投递告警日志失败: {e}", exc_info=True)


def reset_alert_manager() -> None:
    """重置全局告警管理器（测试用）"""
    global _global_alert_manager
    _global_alert_manager = None


# ═══════════════════════════════════════════════
#  自测 Demo（A-10，回写#214；临时库不碰真实 quant.db）
# ═══════════════════════════════════════════════

def _run_a10_self_test() -> int:
    """A-10 自测：P0~P3 统一映射 / runbook_id 贯通 / 超时未确认自动升级落库。

    防未来：使用固定偏移的历史时间戳（now-310s）模拟"已超时未确认"，不依赖随机时钟。

    Returns:
        int: 失败项数（0 = 全绿）。
    """
    import tempfile
    from pathlib import Path

    from myquant.core.alert_store import AlertStore
    from myquant.database.connection import DatabaseConnection

    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name}: {detail}")

    # 1) P0~P3 统一映射（对齐 P0-3 分级表）
    check("P0->CRITICAL", p0_p3_level("P0") is SeverityLevel.CRITICAL)
    check("P1->ERROR", p0_p3_level("P1") is SeverityLevel.ERROR)
    check("P2->WARNING", p0_p3_level("P2") is SeverityLevel.WARNING)
    check("P3->INFO", p0_p3_level("P3") is SeverityLevel.INFO)
    check("p0 小写/空白容错", p0_p3_level(" p0 ") is SeverityLevel.CRITICAL)
    try:
        p0_p3_level("P9")
        check("非法分级抛错", False, "未抛 ValueError")
    except ValueError:
        check("非法分级抛错", True)

    # 2) runbook_id 归一化：details 透传 / category 默认 / 显式优先
    a_detail = Alert(level=SeverityLevel.WARNING, message="t",
                     category="broker", details={"runbook_id": "RB-X"})
    check("details.runbook_id 提升为显式字段", a_detail.runbook_id == "RB-X")
    a_default = Alert(level=SeverityLevel.WARNING, message="t", category="broker")
    check("category 默认 runbook 兜底", a_default.runbook_id == "RB-BROKER-DISCONNECT-001")
    a_explicit = Alert(level=SeverityLevel.CRITICAL, message="t",
                       category="broker", runbook_id="RB-FORCED")
    check("显式 runbook 优先", a_explicit.runbook_id == "RB-FORCED")
    check("to_dict 含 runbook_id", a_explicit.to_dict().get("runbook_id") == "RB-FORCED")

    # 3) 超时未确认自动升级（临时库；加 console 通道避免触发未投递落盘写 logs）
    with tempfile.TemporaryDirectory() as tmp:
        conn = DatabaseConnection(Path(tmp) / "esc_self.db")
        store = AlertStore(connection=conn)
        mgr = AlertManager(store=store)
        mgr.add_channel(ConsoleAlertChannel("self-test-console"))
        stale = Alert(
            level=SeverityLevel.WARNING, message="超时未确认告警",
            category="broker", alert_id="self-stale1",
            timestamp=time.time() - 310.0,   # 已超默认 escalation_time=300s
        )
        mgr.send_alert(stale, dedup_enabled=False)
        n = mgr.check_escalations()
        check("升级计数=1", n == 1, f"n={n}")
        row = store.get("self-stale1")
        check("原告警置 acked", row is not None and row["status"] == "acked")
        check("原告警 escalated 标记落库", row is not None and row["escalated"] is True)
        check("原告警 runbook_id 落库",
              row is not None and row["runbook_id"] == "RB-BROKER-DISCONNECT-001")
        errs = store.load_history(limit=10, level=SeverityLevel.ERROR.value)
        check("升级告警 ERROR 且带[升级]前缀落库",
              any(e["message"].startswith("[升级]") for e in errs))
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    print(f"[A-10] alerting 自测: {passed} 项通过, {len(failures)} 项失败")
    for f in failures:
        print(f"  - {f}")
    return len(failures)


def _run_c9_governance_self_test(dynamic_baseline_cls: Any = None,
                              alert_store_cls: Any = None) -> int:
    """C-9（回写#238）治理自测：依赖抑制 / 元监控独立冷却 / 动态基线降噪 /
    聚合分组 / 周统计 TP-FP-FN 达标判定 / 指标注册 / 防未来 / 防御校验。

    防未来：用固定时钟（clock_now 远大于喂入点 ts），周统计用历史 ts 落临时库。

    Returns:
        int: 失败项数（0 = 全绿）。
    """
    import tempfile
    from pathlib import Path

    from myquant.database.connection import DatabaseConnection

    if dynamic_baseline_cls is None or alert_store_cls is None:
        raise ValueError("自测需经 __main__ 注入 DynamicBaseline/AlertStore（G2-1 断环）")
    DynamicBaseline = dynamic_baseline_cls
    AlertStore = alert_store_cls

    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name}: {detail}")

    # ── 固定时钟：远大于所有喂入 ts（全部历史点 <= now，严格防未来）──
    clock_now = [1_700_000_000.0 + 1_000_000.0]

    def clock() -> float:
        return clock_now[0]

    # 1) 依赖抑制：watchdog 根因命中 → 同窗 data_quality 上层告警被抑制
    gov = AlertGovernor(
        dependency_suppression_enabled=True,
        suppression_window_seconds=60.0,
        aggregation_window_seconds=60.0,
        meta_cooldown_seconds=120.0,
        clock=clock,
    )
    root = Alert(level=SeverityLevel.ERROR, message="行情断流",
                 category="watchdog", source="monitoring.watchdog",
                 deduplication_key="watchdog:staleness", timestamp=clock())
    d_root = gov.evaluate(root)
    check("C9-1) 根因 watchdog 告警放行", d_root.allowed, f"{d_root}")
    upper = Alert(level=SeverityLevel.WARNING, message="数据质量下降",
                  category="data_quality", source="monitoring.data_quality",
                  deduplication_key="dq:gap", timestamp=clock())
    d_up = gov.evaluate(upper)
    check("C9-1) 上层 data_quality 被根因抑制",
          not d_up.allowed and d_up.reason == GOVERNANCE_SUPPRESSED_DEPENDENCY,
          f"{d_up}")
    # 非依赖关系的 broker 告警放行
    other = Alert(level=SeverityLevel.WARNING, message="券商延迟",
                  category="broker", deduplication_key="broker:latency",
                  timestamp=clock())
    d_other = gov.evaluate(other)
    check("C9-1) 非依赖 broker 告警放行", d_other.allowed, f"{d_other}")

    # 2) 元监控独立冷却（R-08）：meta:* 走独立冷却，跳过业务降噪
    meta1 = Alert(level=SeverityLevel.WARNING, message="[meta] collector stale",
                  category="meta", source="meta_monitor",
                  deduplication_key="meta:collector_health", timestamp=clock())
    check("C9-2) 首轮 meta 告警放行", gov.evaluate(meta1).allowed)
    meta2 = Alert(level=SeverityLevel.WARNING, message="[meta] collector stale 再发",
                  category="meta", source="meta_monitor",
                  deduplication_key="meta:collector_health", timestamp=clock())
    d_meta2 = gov.evaluate(meta2)
    check("C9-2) 冷却窗口内 meta 告警被独立冷却抑制",
          not d_meta2.allowed and d_meta2.reason == GOVERNANCE_SUPPRESSED_META,
          f"{d_meta2}")

    # 3) 动态基线降噪（集成 C-6）：值落在动态基线内 → 抑制静态阈值误报
    step = 60.0
    base = 1_700_000_000.0

    def ts(k: int) -> float:
        return base + k * step

    db = DynamicBaseline(window=20, k=3.0, min_samples=10, clock=clock)
    gov2 = AlertGovernor(
        dependency_suppression_enabled=False,
        baseline_noise_reduction_enabled=True,
        dynamic_baseline=db,
        clock=clock,
    )
    # 喂平稳段（mean≈100）：冷启动样本不足（<min_samples=10）→ 不抑制（不编造判断）
    normal = [100.0, 101.0, 99.0, 100.5, 99.5, 100.2, 99.8, 100.1, 99.9]  # 9 点 <10
    for i, v in enumerate(normal):
        a = Alert(level=SeverityLevel.WARNING, message="静态阈值告警",
                  category="system", deduplication_key=f"static:lat:{i}",
                  details={"governance": {"series": "latency_ms", "value": v}},
                  timestamp=ts(i))
        d = gov2.evaluate(a)
        check(f"C9-3) 冷启动段第{i}点不抑制", d.allowed, f"{d}")
    # 第 10 个点（fed=10>=min_samples 已暖机）：值 100.2 落在基线带内 → 抑制误报
    a_normal = Alert(level=SeverityLevel.WARNING, message="静态阈值误报",
                      category="system", deduplication_key="static:lat:normal",
                      details={"governance": {"series": "latency_ms", "value": 100.2}},
                      timestamp=ts(15))
    d_norm = gov2.evaluate(a_normal)
    check("C9-3) 暖机后基线内值抑制静态阈值误报",
          not d_norm.allowed and d_norm.reason == GOVERNANCE_SUPPRESSED_BASELINE,
          f"{d_norm}")

    # 4) 聚合分组：同根因链路相似告警在窗口内归并（暴露 summary）
    gov3 = AlertGovernor(aggregation_window_seconds=60.0, clock=clock)
    g1 = Alert(level=SeverityLevel.WARNING, message="a", category="data_quality",
               deduplication_key="dq:gap:one", timestamp=clock())
    gov3.evaluate(g1)
    g2 = Alert(level=SeverityLevel.WARNING, message="b", category="data_quality",
               deduplication_key="dq:gap:two", timestamp=clock())
    gov3.evaluate(g2)
    check("C9-4) 同链路相似告警归并计数=1",
          gov3.summary()["aggregated_total"] == 1,
          f"{gov3.summary()}")

    # 5) 周统计 TP/FP/FN + 达标判定（临时库落历史告警）
    with tempfile.TemporaryDirectory() as tmp:
        conn = DatabaseConnection(Path(tmp) / "c9_week.db")
        store = AlertStore(connection=conn)
        now = clock_now[0]
        # 落 19 条 TP + 1 条 FP（本周窗口内），classification 打标
        for i in range(19):
            a = Alert(level=SeverityLevel.WARNING, message=f"tp-{i}",
                      category="system", alert_id=f"c9-tp-{i}",
                      timestamp=now - 3600.0, classification="TP")
            store.save_alert(a)
            store.set_classification(f"c9-tp-{i}", "TP")
        for i in range(1):
            a = Alert(level=SeverityLevel.WARNING, message=f"fp-{i}",
                      category="system", alert_id=f"c9-fp-{i}",
                      timestamp=now - 3600.0, classification="FP")
            store.save_alert(a)
            store.set_classification(f"c9-fp-{i}", "FP")
        stats = weekly_alert_stats(store, clock=clock)
        check("C9-5) TP=19 / FP=1",
              stats["tp"] == 19 and stats["fp"] == 1, f"{stats}")
        check("C9-5) 准确率=19/20=0.95 边界不达标(>0.95)",
              stats["accuracy"] == 0.95 and stats["passed"] is False,
              f"{stats['accuracy']}/{stats['passed']}")
        check("C9-5) 误报率=1/20=0.05 边界(<0.05 不达标)",
              stats["false_positive_rate"] == 0.05 and not stats["passed"])
        check("C9-5) 含达标注释", "至少 4 周" in stats["note"])
        # 再落 1 条 TP → 20/21≈0.9524>0.95 且 fp=1/21≈0.0476<0.05 → 达标
        a = Alert(level=SeverityLevel.WARNING, message="tp-more",
                  category="system", alert_id="c9-tp-more", timestamp=now - 1800.0)
        store.save_alert(a)
        store.set_classification("c9-tp-more", "TP")
        stats2 = weekly_alert_stats(store, clock=clock)
        check("C9-5) 20TP/1FP → 准确率>0.95 且误报率<0.05 → 达标",
              stats2["passed"] is True, f"{stats2['accuracy']}/{stats2['false_positive_rate']}")
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # 6) 防未来：拒绝未来告警
    gov4 = AlertGovernor(clock=clock)
    future = Alert(level=SeverityLevel.WARNING, message="future", category="system",
                   timestamp=clock_now[0] + 1000.0)
    rejected = False
    try:
        gov4.evaluate(future)
    except ValueError:
        rejected = True
    check("C9-6) 拒绝未来告警", rejected)

    # 7) 防御式校验：非法窗口 / attach 非 governor
    bad = False
    try:
        AlertGovernor(suppression_window_seconds=-1)
    except ValueError:
        bad = True
    check("C9-7) 非法窗口拒绝", bad)
    mgr = AlertManager()
    bad2 = False
    try:
        mgr.attach_governor("not-a-governor")  # type: ignore[arg-type]
    except ValueError:
        bad2 = True
    check("C9-7) attach 非 governor 拒绝", bad2)

    print(f"[C-9] alerting 治理自测: {passed} 项通过, {len(failures)} 项失败")
    for f in failures:
        print(f"  - {f}")
    return len(failures)


if __name__ == "__main__":
    from myquant.core.alert_store import AlertStore
    from myquant.monitoring.dynamic_baseline import DynamicBaseline

    _a10 = _run_a10_self_test()
    _c9 = _run_c9_governance_self_test(
        dynamic_baseline_cls=DynamicBaseline, alert_store_cls=AlertStore)
    raise SystemExit(_a10 + _c9)
