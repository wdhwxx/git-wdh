# 文件名：myquant/core/event_bus.py
"""
event_bus.py - 进程内事件总线（阶段9增强：数据质量事件 + 发布订阅增强）
============================================================================
功能:
  1. 发布/订阅模式 (Pub/Sub)
  2. 支持同步和异步事件处理器
  3. 事件优先级
  4. 通配符订阅 (*)
  5. 一次性订阅 (once)
  6. 事件历史记录
  7. 线程安全
  8. 回测与实盘共用

阶段9新增（数据质量事件 + 发布订阅增强）:
  9. 数据质量事件类型：DATA_QUALITY_DIRTY / PIPELINE_COMPLETE / QUALITY_ALERT / CRITICAL
  10. 强类型事件类：DataQualityEvent / PipelineCompleteEvent / QualityAlertEvent
  11. 事件过滤器：按 symbol / data_type / severity / event_type 过滤
  12. 处理器统计：调用次数 / 错误次数 / 耗时 / 最后错误
  13. 告警去重：时间窗口内相同 key 的告警只发布一次
  14. 类型化发布方法：publish_data_quality / publish_pipeline_complete / publish_quality_alert
  15. DataQualityEventPublisher 便捷发布器（供清洗流水线/实盘数据流调用）
  16. 阻塞等待事件：wait_for_event()

阶段9任务12-B新增（策略暂停/恢复事件）:
  17. 风控事件类型：RISK_PAUSED / RISK_RESUMED
  18. StrategyPauseEvent 强类型事件：供实盘引擎/API接收风控暂停与恢复动作

依赖: threading, asyncio, collections, time, uuid, dataclasses
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Union

from myquant.core.exceptions import EventError, EventHandlerError

logger = logging.getLogger("myquant.event_bus")


# ═══════════════════════════════════════════════
#  事件类型
# ═══════════════════════════════════════════════

class EventType(Enum):
    """系统事件类型"""
    # 市场事件
    TICK = "market.tick"
    BAR = "market.bar"
    ORDER_BOOK = "market.order_book"

    # 信号事件
    SIGNAL = "strategy.signal"
    SIGNAL_GENERATED = "strategy.signal_generated"

    # 订单事件
    ORDER_SUBMIT = "order.submit"
    ORDER_FILLED = "order.filled"
    ORDER_PARTIAL = "order.partial_filled"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_REJECTED = "order.rejected"
    # ── 交易异常事件（A-8 交易异常告警闭环；取值沿用 order.* 点分命名风格）──
    ORDER_FAILED = "order.failed"                    # 订单发送/执行失败（拒单之外的失败，P1）
    FILL_REPORT_ERROR = "fill.report_error"           # 成交回报对账/上报异常（P1）
    EMERGENCY_LIQUIDATION = "emergency.liquidation"  # 紧急强平（P0，最高优先级告警）

    # 持仓事件
    POSITION_UPDATE = "position.update"
    POSITION_OPEN = "position.open"
    POSITION_CLOSE = "position.close"

    # 风控事件
    RISK_ALERT = "risk.alert"
    RISK_VIOLATION = "risk.violation"
    RISK_LIMIT_HIT = "risk.limit_hit"
    RISK_PAUSED = "risk.paused"        # 风控/策略暂停（数据质量或手动触发，供实盘引擎/API消费）
    RISK_RESUMED = "risk.resumed"      # 风控/策略恢复

    # 系统事件
    SYSTEM_START = "system.start"
    SYSTEM_STOP = "system.stop"
    SYSTEM_ERROR = "system.error"
    DATA_LOADED = "data.loaded"
    BACKTEST_COMPLETE = "backtest.complete"

    # ── 数据质量事件（阶段9新增）──
    DATA_QUALITY_DIRTY = "data_quality.dirty"                    # 发现脏数据
    DATA_QUALITY_PIPELINE_COMPLETE = "data_quality.pipeline_complete"  # 清洗流水线完成
    DATA_QUALITY_ALERT = "data_quality.alert"                    # 数据质量告警
    DATA_QUALITY_CRITICAL = "data_quality.critical"              # 严重数据质量问题（阻断）


# ═══════════════════════════════════════════════
#  事件基类
# ═══════════════════════════════════════════════

@dataclass
class Event:
    """事件对象"""
    event_type: Union[EventType, str]
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = ""
    priority: int = 0  # 越大越优先

    @property
    def type_name(self) -> str:
        if isinstance(self.event_type, EventType):
            return self.event_type.value
        return self.event_type

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "event_type": self.type_name,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "priority": self.priority,
            "data": self.data,
        }

    def __repr__(self) -> str:
        return f"Event(type={self.type_name}, id={self.event_id}, src={self.source})"


# ═══════════════════════════════════════════════
#  交易链路事件定义（BASE-4：事件链路 id 规范 schema 定稿）
# ═══════════════════════════════════════════════

@dataclass
class ChainEvent(Event):
    """
    交易链路事件基类（事件链路 id 规范 · BASE-4 schema 定稿）

    所有交易事件统一携带 ``signal_id / order_id / trade_id / event_ts``
    四元组，为滑点/延迟/链路追踪/根因分析打底（《施工总清单_V1.2》BASE-4）。

    四元组语义（链路唯一真源，A-3 起发布事件全部携带）:
      signal_id : str
          确定性信号 ID（strategies/base.py ``Signal.make_signal_id`` 派生，
          格式 ``strategy_id:symbol:日期:signal_type``）；同一信号
          signal→order→fill 全链路各事件保持一致。
      order_id : str
          订单 ID（Signal.to_order 幂等设计：有 signal 时以 signal_id
          派生 order_id；无 signal 的订单用券商返回 ID）。
      trade_id : str
          成交 ID（database/trade_log.py fills 表唯一，与券商逐笔一致）。
      event_ts : float | None
          事件业务发生时刻（epoch 秒）。**防未来红线**：必须为业务真实
          发生时刻，由发布方填写；禁止用未来时间戳；缺失时不得静默用
          Event.timestamp（事件创建时刻）替代业务时刻，应由发布方补填。
    """
    signal_id: str = ""
    order_id: str = ""
    trade_id: str = ""
    event_ts: Optional[float] = None

    def link_ids(self) -> Dict[str, Any]:
        """返回链路四元组（供日志/指标/追踪打点）"""
        return {
            "signal_id": self.signal_id,
            "order_id": self.order_id,
            "trade_id": self.trade_id,
            "event_ts": self.event_ts,
        }

    def missing_link_ids(self) -> List[str]:
        """返回缺失的链路 id 列表（防御式校验，供发布前提示）"""
        missing: List[str] = []
        if not self.signal_id:
            missing.append("signal_id")
        if not self.order_id:
            missing.append("order_id")
        if not self.trade_id:
            missing.append("trade_id")
        if self.event_ts is None:
            missing.append("event_ts")
        return missing

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update(self.link_ids())
        return d


@dataclass
class SignalEvent(ChainEvent):
    """
    信号事件（EventType.SIGNAL / SIGNAL_GENERATED）——A-3 引擎埋点载体。

    附加字段：
      strategy_id : str  策略 ID
      symbol      : str  标的代码
      signal_type : str  信号类型（如 ma_cross）
    """
    event_type: Union[EventType, str] = EventType.SIGNAL_GENERATED
    strategy_id: str = ""
    symbol: str = ""
    signal_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "signal_type": self.signal_type,
        })
        return d


@dataclass
class OrderEvent(ChainEvent):
    """
    订单事件（构造时传 EventType.ORDER_SUBMIT/FILLED/PARTIAL/CANCELLED/REJECTED
    之一）——A-3 引擎逐笔订单埋点载体；status 与 event_type 同源冗余存证。

    附加字段：
      symbol        : str  标的代码
      status        : str  订单状态（submitted/filled/partial/cancelled/rejected）
      reject_reason : str  拒单原因（REJECTED 时）
    """
    event_type: Union[EventType, str] = EventType.ORDER_SUBMIT
    symbol: str = ""
    status: str = ""
    reject_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "symbol": self.symbol,
            "status": self.status,
            "reject_reason": self.reject_reason,
        })
        return d


@dataclass
class PositionUpdateEvent(ChainEvent):
    """
    持仓更新事件（EventType.POSITION_UPDATE）——A-3 持仓变化埋点载体。

    附加字段：
      symbol    : str   标的代码
      side      : str   方向（buy/sell）
      quantity  : float 变化数量（正=增持，负=减持）
      avg_price : float 更新后持仓均价
    """
    event_type: Union[EventType, str] = EventType.POSITION_UPDATE
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    avg_price: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "avg_price": self.avg_price,
        })
        return d


@dataclass
class SystemErrorEvent(ChainEvent):
    """
    系统错误事件（EventType.SYSTEM_ERROR）——A-3 8 类事件之一；
    交易链路异常时仍携带四元组（可定位到出错信号/订单/成交）。

    附加字段：
      error     : str  错误信息
      component : str  出错组件（如 engine/broker/risk）
    """
    event_type: Union[EventType, str] = EventType.SYSTEM_ERROR
    error: str = ""
    component: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "error": self.error,
            "component": self.component,
        })
        return d


# ═══════════════════════════════════════════════
#  数据质量事件定义（阶段9新增）
# ═══════════════════════════════════════════════

@dataclass
class DataQualityEvent(Event):
    """
    脏数据事件（清洗过程中发现脏数据时发布）

    用于：
      - 监控告警（任务10）：接收事件触发分级告警
      - 风控联动（任务12）：严重脏数据自动暂停策略
      - 指标采集（任务11）：统计脏数据占比

    Attributes
    ----------
    dirty_type : str
        脏数据类型（DirtyDataType 的 value，如 "ohlc_logic_error"）
    symbol : str
        标的代码
    data_type : str
        数据类型（bar/tick）
    severity : str
        严重级别（info/warning/error/critical）
    errors : List[str]
        错误原因列表
    record : Dict | None
        脏数据原始记录
    quality_grade : str
        数据质量等级（QualityGrade 的 value）
    bad_count : int
        本批脏数据数量
    total_count : int
        本批数据总数
    repair_strategy : str
        采用的修复策略（RepairStrategy 的 value）
    source_step : str
        发现脏数据的清洗步骤（CleanStep 的 value）
    """
    event_type: Union[EventType, str] = EventType.DATA_QUALITY_DIRTY
    dirty_type: str = ""
    symbol: str = ""
    data_type: str = ""
    severity: str = "error"
    errors: List[str] = field(default_factory=list)
    record: Optional[Dict[str, Any]] = None
    quality_grade: str = ""
    bad_count: int = 0
    total_count: int = 0
    repair_strategy: str = ""
    source_step: str = ""

    @property
    def is_critical(self) -> bool:
        """是否为严重级别"""
        return self.severity in ("error", "critical")

    @property
    def bad_ratio(self) -> float:
        """脏数据占比"""
        if self.total_count <= 0:
            return 0.0
        return self.bad_count / self.total_count

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "dirty_type": self.dirty_type,
            "symbol": self.symbol,
            "data_type": self.data_type,
            "severity": self.severity,
            "errors": self.errors,
            "quality_grade": self.quality_grade,
            "bad_count": self.bad_count,
            "total_count": self.total_count,
            "bad_ratio": self.bad_ratio,
            "repair_strategy": self.repair_strategy,
            "source_step": self.source_step,
        })
        return d

    def summary(self) -> str:
        """人类可读摘要"""
        return (
            f"DataQualityEvent[{self.severity}]: "
            f"symbol={self.symbol or 'N/A'}, "
            f"type={self.data_type or 'N/A'}, "
            f"dirty={self.dirty_type or 'unknown'}, "
            f"bad={self.bad_count}/{self.total_count}"
        )


@dataclass
class PipelineCompleteEvent(Event):
    """
    清洗流水线完成事件

    在 CleanPipeline 执行完成（成功或失败）后发布，
    供监控/指标采集/下游流程消费。

    Attributes
    ----------
    symbol : str
        标的代码
    data_type : str
        数据类型（bar/tick）
    status : str
        流水线状态（completed/failed）
    cleaned_count : int
        清洗后有效数据数
    bad_count : int
        脏数据数
    total_count : int
        原始数据总数
    quality_grade : str
        数据质量等级
    bad_ratio : float
        脏数据占比
    duration : float
        总耗时（秒）
    steps_executed : List[str]
        已执行步骤名称列表
    bad_file : str | None
        脏数据文件路径
    report_file : str | None
        质量报告文件路径
    error : str | None
        错误信息（失败时）
    """
    event_type: Union[EventType, str] = EventType.DATA_QUALITY_PIPELINE_COMPLETE
    symbol: str = ""
    data_type: str = ""
    status: str = "completed"
    cleaned_count: int = 0
    bad_count: int = 0
    total_count: int = 0
    quality_grade: str = ""
    bad_ratio: float = 0.0
    duration: float = 0.0
    steps_executed: List[str] = field(default_factory=list)
    bad_file: Optional[str] = None
    report_file: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        """是否成功完成"""
        return self.status == "completed"

    @property
    def valid_ratio(self) -> float:
        """有效数据占比"""
        if self.total_count <= 0:
            return 0.0
        return self.cleaned_count / self.total_count

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "symbol": self.symbol,
            "data_type": self.data_type,
            "status": self.status,
            "cleaned_count": self.cleaned_count,
            "bad_count": self.bad_count,
            "total_count": self.total_count,
            "quality_grade": self.quality_grade,
            "bad_ratio": self.bad_ratio,
            "valid_ratio": self.valid_ratio,
            "duration": self.duration,
            "steps_executed": self.steps_executed,
            "bad_file": self.bad_file,
            "report_file": self.report_file,
            "error": self.error,
        })
        return d

    def summary(self) -> str:
        return (
            f"PipelineCompleteEvent[{self.status}]: "
            f"{self.data_type}/{self.symbol}, "
            f"valid={self.cleaned_count}, bad={self.bad_count}, "
            f"grade={self.quality_grade or 'N/A'}, "
            f"duration={self.duration:.4f}s"
        )


@dataclass
class QualityAlertEvent(Event):
    """
    数据质量告警事件（分级告警，供监控/风控消费）

    与 DataQualityEvent 的区别：
      - DataQualityEvent 是原始脏数据发现事件（可能高频）
      - QualityAlertEvent 是经过聚合/分级后的告警事件（低频，用于通知）

    Attributes
    ----------
    alert_level : str
        告警级别（info/warning/error/critical）
    message : str
        告警消息
    symbol : str
        标的代码
    data_type : str
        数据类型
    quality_grade : str
        数据质量等级
    bad_ratio : float
        脏数据占比
    threshold : float
        触发阈值
    dirty_type : str
        主要脏数据类型
    deduplication_key : str
        去重键（相同 key 在时间窗口内只告警一次）
    """
    event_type: Union[EventType, str] = EventType.DATA_QUALITY_ALERT
    alert_level: str = "warning"
    message: str = ""
    symbol: str = ""
    data_type: str = ""
    quality_grade: str = ""
    bad_ratio: float = 0.0
    threshold: float = 0.0
    dirty_type: str = ""
    deduplication_key: str = ""

    @property
    def is_critical(self) -> bool:
        return self.alert_level in ("error", "critical")

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "alert_level": self.alert_level,
            "message": self.message,
            "symbol": self.symbol,
            "data_type": self.data_type,
            "quality_grade": self.quality_grade,
            "bad_ratio": self.bad_ratio,
            "threshold": self.threshold,
            "dirty_type": self.dirty_type,
            "deduplication_key": self.deduplication_key,
        })
        return d

    def summary(self) -> str:
        return (
            f"QualityAlertEvent[{self.alert_level}]: "
            f"{self.message} "
            f"(symbol={self.symbol or 'N/A'}, "
            f"bad_ratio={self.bad_ratio:.2%})"
        )


@dataclass
class StrategyPauseEvent(Event):
    """
    策略暂停/恢复事件（阶段9任务12-B新增，供实盘引擎/API消费）

    风控管理器触发暂停或恢复时发布，用于：
      - 实盘引擎：收到暂停事件立即停止策略下单，收到恢复事件重新开放
      - API/Web：同步风控/策略状态
      - 监控看板：展示策略启停状态

    与 QualityAlertEvent 的区别：
      - QualityAlertEvent 描述"数据质量问题本身"（告警）
      - StrategyPauseEvent 描述"风控因此采取的动作"（策略暂停/恢复）
    实盘引擎只需订阅本事件即可驱动下单开关，无需理解告警细节。

    Attributes
    ----------
    event_type : EventType
        RISK_PAUSED（暂停）或 RISK_RESUMED（恢复）
    paused : bool
        True=暂停，False=恢复
    pause_level : str
        暂停级别：global（全局暂停，禁止所有新开仓）/ symbol（单标的暂停）
    symbol : str
        相关标的代码（global 级别时为触发暂停的标的，供追溯）
    reason : str
        暂停/恢复原因
    strategy : str
        受影响策略名称（空=全部策略）
    quality_grade : str
        相关数据质量等级（数据质量触发时携带）
    bad_ratio : float
        脏数据占比（数据质量触发时携带）
    severity : str
        严重级别（info/warning/error/critical）
    source : str
        事件来源（默认 risk_manager）
    """
    event_type: Union[EventType, str] = EventType.RISK_PAUSED
    paused: bool = True
    pause_level: str = "global"     # global / symbol
    symbol: str = ""
    reason: str = ""
    strategy: str = ""
    quality_grade: str = ""
    bad_ratio: float = 0.0
    severity: str = "warning"
    source: str = "risk_manager"

    @property
    def is_paused(self) -> bool:
        """是否暂停事件"""
        return self.paused

    @property
    def is_resumed(self) -> bool:
        """是否恢复事件"""
        return not self.paused

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d.update({
            "paused": self.paused,
            "pause_level": self.pause_level,
            "symbol": self.symbol,
            "reason": self.reason,
            "strategy": self.strategy,
            "quality_grade": self.quality_grade,
            "bad_ratio": self.bad_ratio,
            "severity": self.severity,
        })
        return d

    def summary(self) -> str:
        action = "暂停" if self.paused else "恢复"
        return (
            f"StrategyPauseEvent[{action}]: level={self.pause_level}, "
            f"symbol={self.symbol or 'ALL'}, reason={self.reason[:40]}"
        )


# ═══════════════════════════════════════════════
#  事件过滤器（阶段9新增）
# ═══════════════════════════════════════════════

@dataclass
class EventFilter:
    """
    事件过滤器（订阅时可指定，只接收匹配的事件）

    所有条件为 AND 关系；任一条件为 None 表示不限制。

    Usage:
        # 只接收 000001.SZ 的 error 级以上脏数据事件
        f = EventFilter(
            symbols={"000001.SZ"},
            min_severity="error",
        )
        bus.subscribe(EventType.DATA_QUALITY_DIRTY, handler, event_filter=f)
    """
    symbols: Optional[Set[str]] = None
    data_types: Optional[Set[str]] = None
    min_severity: Optional[str] = None  # info/warning/error/critical
    event_types: Optional[Set[str]] = None

    # 严重级别数值映射
    _SEVERITY_ORDER = {"info": 10, "warning": 20, "error": 30, "critical": 40}

    def matches(self, event: Event) -> bool:
        """检查事件是否匹配过滤器"""
        # 按事件类型过滤
        if self.event_types is not None:
            if event.type_name not in self.event_types:
                return False

        # 按 symbol 过滤（DataQualityEvent / PipelineCompleteEvent / QualityAlertEvent）
        if self.symbols is not None:
            symbol = getattr(event, "symbol", "")
            if symbol and symbol not in self.symbols:
                return False

        # 按 data_type 过滤
        if self.data_types is not None:
            data_type = getattr(event, "data_type", "")
            if data_type and data_type not in self.data_types:
                return False

        # 按最低严重级别过滤
        if self.min_severity is not None:
            severity = getattr(event, "severity", None)
            if severity is None:
                severity = getattr(event, "alert_level", None)
            if severity is not None:
                evt_level = self._SEVERITY_ORDER.get(severity, 0)
                min_level = self._SEVERITY_ORDER.get(self.min_severity, 0)
                if evt_level < min_level:
                    return False

        return True


# ═══════════════════════════════════════════════
#  处理器统计（阶段9新增）
# ═══════════════════════════════════════════════

@dataclass
class HandlerStats:
    """处理器运行统计"""
    handler_id: str
    event_type: str
    call_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    last_called: float = 0.0
    total_duration: float = 0.0

    @property
    def avg_duration(self) -> float:
        """平均耗时（秒）"""
        if self.call_count <= 0:
            return 0.0
        return self.total_duration / self.call_count

    @property
    def error_rate(self) -> float:
        """错误率"""
        if self.call_count <= 0:
            return 0.0
        return self.error_count / self.call_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handler_id": self.handler_id,
            "event_type": self.event_type,
            "call_count": self.call_count,
            "error_count": self.error_count,
            "error_rate": self.error_rate,
            "last_error": self.last_error,
            "last_called": self.last_called,
            "avg_duration": self.avg_duration,
        }


# ═══════════════════════════════════════════════
#  事件处理器包装
# ═══════════════════════════════════════════════

@dataclass
class _Handler:
    callback: Callable
    event_type: str
    once: bool = False
    priority: int = 0
    handler_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    is_async: bool = False
    event_filter: Optional[EventFilter] = None
    stats: HandlerStats = field(default_factory=lambda: HandlerStats(handler_id="", event_type=""))

    def __post_init__(self):
        if not self.stats.handler_id:
            self.stats.handler_id = self.handler_id
            self.stats.event_type = self.event_type


# ═══════════════════════════════════════════════
#  事件总线
# ═══════════════════════════════════════════════

class EventBus:
    """
    进程内事件总线 (线程安全)

    支持:
      - 同步处理器
      - 异步处理器 (自动检测)
      - 通配符订阅: subscribe("*", handler) 接收所有事件
      - 一次性订阅: once()
      - 优先级排序
      - 事件历史回溯
      - 事件过滤器（阶段9新增）
      - 处理器统计（阶段9新增）
      - 类型化发布方法（阶段9新增）
      - 告警去重（阶段9新增）
      - 阻塞等待事件（阶段9新增）

    Usage:
        bus = EventBus()

        # 订阅
        def on_tick(event: Event):
            print(f"Tick: {event.data}")
        bus.subscribe(EventType.TICK, on_tick)

        # 发布
        bus.publish(Event(EventType.TICK, data={"price": 10.5}))

        # 数据质量事件（阶段9）
        bus.publish_data_quality(
            dirty_type="ohlc_logic_error",
            symbol="000001.SZ",
            data_type="bar",
            severity="error",
        )
    """

    def __init__(
        self,
        max_history: int = 1000,
        dedup_window_seconds: float = 60.0,
        use_object_pool: bool = False,
        pool_max_idle: int = 1000,
        max_dedup_keys: int = 10_000,
        sync_budget_seconds: float = 5.0,
    ):
        """
        Parameters
        ----------
        max_history : int
            事件历史最大保留条数
        dedup_window_seconds : float
            告警去重时间窗口（秒），相同 deduplication_key 的告警在此窗口内只发布一次
        use_object_pool : bool
            是否启用事件对象池（P1 第8项）。启用后可通过 acquire_event()/release_event()
            复用 Event 对象，减少高频事件流的内存分配。默认关闭（向后兼容）。
        pool_max_idle : int
            事件对象池空闲对象缓存上限。
        max_dedup_keys : int
            告警去重缓存最大键数（M3：超限清理最旧一半，防无界增长）。
        sync_budget_seconds : float
            同步发布预算（秒，M8）：handler 累计执行超时后跳过剩余 handler，
            防慢 handler 阻塞发布链；0 表示不启用（向后兼容）。
        """
        self._handlers: Dict[str, List[_Handler]] = defaultdict(list)
        self._lock = threading.RLock()
        self._history: Deque[Event] = deque(maxlen=max_history)
        self._async_handlers: Dict[str, List[_Handler]] = defaultdict(list)
        self._event_count = 0
        self._dedup_window = dedup_window_seconds
        self._dedup_cache: Dict[str, float] = {}
        self._max_dedup_keys = max_dedup_keys
        self._sync_budget_seconds = sync_budget_seconds
        self._wait_events: Dict[str, List[threading.Event]] = defaultdict(list)
        # P1 第8项：事件对象池（默认关闭，向后兼容）
        self._use_object_pool = use_object_pool
        self._event_pool: Optional[Any] = None
        if use_object_pool:
            from myquant.utils.memory_pool import EventObjectPool

            self._event_pool = EventObjectPool(max_idle=pool_max_idle, name="eventbus_pool")

    # ═══════════════════════════════════════════
    #  订阅
    # ═══════════════════════════════════════════

    def subscribe(
        self,
        event_type: Union[EventType, str],
        handler: Callable[[Event], Any],
        priority: int = 0,
        event_filter: Optional[EventFilter] = None,
    ) -> str:
        """
        订阅事件

        Parameters
        ----------
        event_type : EventType | str
            事件类型
        handler : Callable[[Event], Any]
            事件处理器
        priority : int
            优先级（越大越优先）
        event_filter : EventFilter | None
            事件过滤器（阶段9新增），只接收匹配的事件

        Returns
        -------
        str : handler_id (用于取消订阅)
        """
        type_name = event_type.value if isinstance(event_type, EventType) else event_type
        is_async = asyncio.iscoroutinefunction(handler)

        h = _Handler(
            callback=handler,
            event_type=type_name,
            priority=priority,
            is_async=is_async,
            event_filter=event_filter,
        )

        with self._lock:
            if is_async:
                self._async_handlers[type_name].append(h)
                self._async_handlers[type_name].sort(key=lambda x: -x.priority)
            else:
                self._handlers[type_name].append(h)
                self._handlers[type_name].sort(key=lambda x: -x.priority)

        logger.debug(f"订阅事件: {type_name}, handler={h.handler_id}")
        return h.handler_id

    def once(
        self,
        event_type: Union[EventType, str],
        handler: Callable[[Event], Any],
        priority: int = 0,
        event_filter: Optional[EventFilter] = None,
    ) -> str:
        """一次性订阅 (触发后自动取消)"""
        type_name = event_type.value if isinstance(event_type, EventType) else event_type
        is_async = asyncio.iscoroutinefunction(handler)

        h = _Handler(
            callback=handler,
            event_type=type_name,
            once=True,
            priority=priority,
            is_async=is_async,
            event_filter=event_filter,
        )

        with self._lock:
            if is_async:
                self._async_handlers[type_name].append(h)
            else:
                self._handlers[type_name].append(h)
        return h.handler_id

    def unsubscribe(self, handler_id: str) -> bool:
        """取消订阅"""
        with self._lock:
            for handlers in list(self._handlers.values()) + list(self._async_handlers.values()):
                for i, h in enumerate(handlers):
                    if h.handler_id == handler_id:
                        handlers.pop(i)
                        logger.debug(f"取消订阅: handler={handler_id}")
                        return True
        return False

    def unsubscribe_all(self, event_type: Union[EventType, str]) -> int:
        """取消某事件的所有订阅"""
        type_name = event_type.value if isinstance(event_type, EventType) else event_type
        with self._lock:
            count = len(self._handlers.get(type_name, []))
            count += len(self._async_handlers.get(type_name, []))
            self._handlers.pop(type_name, None)
            self._async_handlers.pop(type_name, None)
        return count

    # ═══════════════════════════════════════════
    #  发布（基础）
    # ═══════════════════════════════════════════

    def _prune_dedup_cache(self) -> None:
        """M3修复: 修剪告警去重缓存——过期键（10×窗口）＋超上限键（清最旧一半）"""
        if not self._dedup_cache:
            return
        now = time.time()
        with self._lock:
            expired = [
                k
                for k, v in self._dedup_cache.items()
                if (now - v) > self._dedup_window * 10
            ]
            for k in expired:
                del self._dedup_cache[k]
            if len(self._dedup_cache) > self._max_dedup_keys:
                ordered = sorted(
                    self._dedup_cache.items(), key=lambda kv: kv[1]
                )
                for k, _ in ordered[: len(ordered) // 2]:
                    self._dedup_cache.pop(k, None)
                logger.warning(
                    f"[event_bus] 去重缓存超上限({self._max_dedup_keys})，"
                    f"已清理最旧 {len(ordered) // 2} 个键"
                )

    def publish(self, event: Event) -> int:
        """
        发布事件 (同步)
        返回触发的处理器数量
        """
        with self._lock:
            self._history.append(event)
            self._event_count += 1
        # M3修复: 每次发布后统一修剪去重缓存（过期键 + 超上限），
        # 不再只依赖告警路径的惰性清理
        self._prune_dedup_cache()

        type_name = event.type_name
        triggered = 0

        # 收集匹配的处理器 (精确 + 通配符)
        handlers_to_call = []
        once_ids_to_remove = []

        with self._lock:
            # 精确匹配
            for h in self._handlers.get(type_name, []):
                if h.event_filter and not h.event_filter.matches(event):
                    continue
                handlers_to_call.append(h)
                if h.once:
                    once_ids_to_remove.append(h.handler_id)
            # 通配符
            for h in self._handlers.get("*", []):
                if h.event_filter and not h.event_filter.matches(event):
                    continue
                handlers_to_call.append(h)
                if h.once:
                    once_ids_to_remove.append(h.handler_id)

        # 按优先级排序
        handlers_to_call.sort(key=lambda x: -x.priority)

        # 执行（M8修复: 同步发布预算——累计耗时超预算后跳过剩余 handler，
        # 防单个慢 handler 拖垮整个发布链）
        _budget = self._sync_budget_seconds
        _total_start = time.time()
        for _i, h in enumerate(handlers_to_call):
            if _budget > 0 and (time.time() - _total_start) > _budget:
                logger.warning(
                    f"[event_bus] 同步发布预算 {_budget}s 超时，"
                    f"跳过 handler {h.handler_id}（{type_name}），"
                    f"剩余 {len(handlers_to_call) - _i} 个"
                )
                continue
            start = time.time()
            try:
                h.callback(event)
                triggered += 1
            except Exception as e:
                h.stats.error_count += 1
                h.stats.last_error = str(e)
                logger.error(f"Event handler error [{h.handler_id}]: {e}", exc_info=True)
            finally:
                h.stats.call_count += 1
                h.stats.last_called = time.time()
                h.stats.total_duration += time.time() - start

        # 清理 once
        for hid in once_ids_to_remove:
            self.unsubscribe(hid)

        # 通知等待者
        self._notify_waiters(type_name, event)

        return triggered

    async def publish_async(self, event: Event) -> int:
        """
        发布事件 (异步，支持异步处理器)
        """
        with self._lock:
            self._history.append(event)
            self._event_count += 1
        # M3修复: 统一修剪去重缓存
        self._prune_dedup_cache()

        type_name = event.type_name
        triggered = 0

        with self._lock:
            sync_handlers = [
                h for h in (
                    self._handlers.get(type_name, []) +
                    self._handlers.get("*", [])
                )
                if not (h.event_filter and not h.event_filter.matches(event))
            ]
            async_handlers = [
                h for h in (
                    self._async_handlers.get(type_name, []) +
                    self._async_handlers.get("*", [])
                )
                if not (h.event_filter and not h.event_filter.matches(event))
            ]

        # 同步处理器（M8修复: 预算超时跳过剩余）
        _budget = self._sync_budget_seconds
        _total_start = time.time()
        for _i, h in enumerate(sorted(sync_handlers, key=lambda x: -x.priority)):
            if _budget > 0 and (time.time() - _total_start) > _budget:
                logger.warning(
                    f"[event_bus] publish_async 同步预算 {_budget}s 超时，"
                    f"跳过 handler {h.handler_id}（{type_name}）"
                )
                continue
            start = time.time()
            try:
                h.callback(event)
                triggered += 1
            except Exception as e:
                h.stats.error_count += 1
                h.stats.last_error = str(e)
                logger.error(f"Sync handler error: {e}")
            finally:
                h.stats.call_count += 1
                h.stats.last_called = time.time()
                h.stats.total_duration += time.time() - start
            if h.once:
                self.unsubscribe(h.handler_id)

        # 异步处理器
        for h in sorted(async_handlers, key=lambda x: -x.priority):
            start = time.time()
            try:
                await h.callback(event)
                triggered += 1
            except Exception as e:
                h.stats.error_count += 1
                h.stats.last_error = str(e)
                logger.error(f"Async handler error: {e}")
            finally:
                h.stats.call_count += 1
                h.stats.last_called = time.time()
                h.stats.total_duration += time.time() - start
            if h.once:
                self.unsubscribe(h.handler_id)

        # 通知等待者
        self._notify_waiters(type_name, event)

        return triggered

    def publish_many(self, events: List[Event]) -> int:
        """
        批量发布事件（P1 第8项：批处理模式）

        相比逐条调用 publish：
          - 一次加锁记录历史与计数
          - 按事件类型分组，一次完成 handler 收集 / 排序 / 执行
          - once 一次性清理（而非逐条清理）
        适用于高频事件批量提交（tick/bar 流、数据质量批事件）。

        Parameters
        ----------
        events : List[Event]
            待批量发布的事件列表（可为空列表，安全返回 0）。

        Returns
        -------
        int : 实际触发的处理器调用次数
        """
        if not events:
            return 0

        with self._lock:
            for ev in events:
                self._history.append(ev)
                self._event_count += 1

        # 按类型分组
        by_type: Dict[str, List[Event]] = defaultdict(list)
        for ev in events:
            by_type[ev.type_name].append(ev)

        # 一次收集待执行处理器（精确 + 通配，各自匹配事件）
        slots: List[tuple] = []
        once_ids: List[str] = []
        with self._lock:
            for type_name, type_events in by_type.items():
                for h in self._handlers.get(type_name, []):
                    if h.event_filter:
                        matched = [e for e in type_events if h.event_filter.matches(e)]
                    else:
                        matched = list(type_events)
                    if matched:
                        slots.append((h, matched))
                        if h.once:
                            once_ids.append(h.handler_id)
                for h in self._handlers.get("*", []):
                    if h.event_filter:
                        matched = [e for e in type_events if h.event_filter.matches(e)]
                    else:
                        matched = list(type_events)
                    if matched:
                        slots.append((h, matched))
                        if h.once:
                            once_ids.append(h.handler_id)

        # 按优先级排序后执行（stats 与逐条 publish 口径一致）
        slots.sort(key=lambda x: -x[0].priority)
        triggered = 0
        for h, matched in slots:
            for ev in matched:
                start = time.time()
                try:
                    h.callback(ev)
                    triggered += 1
                except Exception as e:  # 防御式：单 handler 异常不影响其他
                    h.stats.error_count += 1
                    h.stats.last_error = str(e)
                    logger.error(f"Event handler error [{h.handler_id}]: {e}", exc_info=True)
                finally:
                    h.stats.call_count += 1
                    h.stats.last_called = time.time()
                    h.stats.total_duration += time.time() - start

        # once 统一清理
        for hid in once_ids:
            self.unsubscribe(hid)

        # 通知等待者（每个事件）
        for type_name, type_events in by_type.items():
            for ev in type_events:
                self._notify_waiters(type_name, ev)

        return triggered

    def batch(self, auto_flush: bool = False) -> "EventBatch":
        """
        创建事件批处理上下文（P1 第8项）

        Parameters
        ----------
        auto_flush : bool
            True 时 with 块退出自动 flush；False 需手动调用 flush()。

        Returns
        -------
        EventBatch : 批处理收集器

        Usage:
            bus = EventBus()
            with bus.batch(auto_flush=True) as b:
                b.add(event1)
                b.add(event2)
        """
        return EventBatch(self, auto_flush=auto_flush)

    def acquire_event(
        self,
        event_type: Union[EventType, str],
        data: Optional[Dict] = None,
        **kwargs: Any,
    ) -> Event:
        """
        从事件对象池获取事件对象（P1 第8项）

        启用 use_object_pool=True 时复用池内对象；未启用时等价于直接构造 Event。

        Parameters
        ----------
        event_type : EventType | str
            事件类型
        data : dict | None
            事件数据
        **kwargs : Any
            其余事件字段（source / priority 等）

        Returns
        -------
        Event : 初始化完毕的事件对象
        """
        if self._event_pool is not None:
            return self._event_pool.acquire(event_type, data=data, **kwargs)
        return Event(event_type=event_type, data=data or {}, **kwargs)

    def release_event(self, event: Optional[Event]) -> bool:
        """
        归还事件对象到对象池（P1 第8项）

        Parameters
        ----------
        event : Event | None
            待归还事件对象

        Returns
        -------
        bool : True 表示已归还/无需处理；False 表示 event 为空
        """
        if event is None:
            return False
        if self._event_pool is not None:
            return self._event_pool.release(event)
        return True

    # ── 快捷发布 ──
    def emit(self, event_type: Union[EventType, str], data: Optional[Dict] = None, **kwargs) -> int:
        """快捷发布"""
        event = Event(event_type=event_type, data=data or {}, **kwargs)
        return self.publish(event)

    # ═══════════════════════════════════════════
    #  类型化发布方法（阶段9新增）
    # ═══════════════════════════════════════════

    def publish_chain(
        self,
        event: ChainEvent,
        require_event_ts: bool = False,
    ) -> int:
        """
        发布交易链路事件（BASE-4 事件链路 id 规范：统一入口 + 四元组校验）

        Parameters
        ----------
        event : ChainEvent
            链路事件（SignalEvent/OrderEvent/PositionUpdateEvent/SystemErrorEvent）
        require_event_ts : bool
            是否强制 event_ts 非空。默认 False：缺失记 warning 仍发布
            （A-3 埋点过渡期不阻断）；True：缺失记 error 并返回 0（不发布）。

        Returns
        -------
        int : 触发的处理器数量（强制模式缺 event_ts 时为 0）

        Raises
        ------
        TypeError : event 不是 ChainEvent 子类时抛出（防误用普通 Event）
        """
        if not isinstance(event, ChainEvent):
            raise TypeError(
                f"publish_chain 仅接受 ChainEvent，收到 {type(event).__name__}"
            )
        missing = event.missing_link_ids()
        if event.event_ts is None:
            if require_event_ts:
                logger.error(
                    f"[chain] 发布拒绝：{event.type_name} 缺 event_ts"
                    f"（缺失 {missing}），业务发生时刻必须由发布方填写"
                )
                return 0
            logger.warning(
                f"[chain] 发布 {event.type_name} 缺 event_ts（缺失 {missing}），"
                f"请发布方补填业务发生时刻（防未来红线，禁止用未来时间戳）"
            )
        logger.info(f"[chain] 发布 {event.type_name}: {event.link_ids()}")
        return self.publish(event)

    def publish_data_quality(
        self,
        dirty_type: str = "",
        symbol: str = "",
        data_type: str = "",
        severity: str = "error",
        errors: Optional[List[str]] = None,
        record: Optional[Dict[str, Any]] = None,
        quality_grade: str = "",
        bad_count: int = 0,
        total_count: int = 0,
        repair_strategy: str = "",
        source_step: str = "",
        source: str = "data_clean",
        priority: int = 0,
        **kwargs: Any,
    ) -> int:
        """
        发布脏数据事件

        Parameters
        ----------
        dirty_type : str
            脏数据类型（DirtyDataType value）
        symbol : str
            标的代码
        data_type : str
            数据类型（bar/tick）
        severity : str
            严重级别（info/warning/error/critical）
        errors : List[str] | None
            错误原因列表
        record : Dict | None
            脏数据原始记录
        quality_grade : str
            数据质量等级
        bad_count : int
            脏数据数量
        total_count : int
            数据总数
        repair_strategy : str
            修复策略
        source_step : str
            发现脏数据的步骤
        source : str
            事件来源
        priority : int
            优先级

        Returns
        -------
        int : 触发的处理器数量
        """
        event = DataQualityEvent(
            event_type=EventType.DATA_QUALITY_DIRTY,
            dirty_type=dirty_type,
            symbol=symbol,
            data_type=data_type,
            severity=severity,
            errors=errors or [],
            record=record,
            quality_grade=quality_grade,
            bad_count=bad_count,
            total_count=total_count,
            repair_strategy=repair_strategy,
            source_step=source_step,
            source=source,
            priority=priority,
            data=kwargs,
        )
        logger.debug(f"发布脏数据事件: {event.summary()}")
        return self.publish(event)

    def publish_pipeline_complete(
        self,
        symbol: str = "",
        data_type: str = "",
        status: str = "completed",
        cleaned_count: int = 0,
        bad_count: int = 0,
        total_count: int = 0,
        quality_grade: str = "",
        bad_ratio: Optional[float] = None,
        duration: float = 0.0,
        steps_executed: Optional[List[str]] = None,
        bad_file: Optional[str] = None,
        report_file: Optional[str] = None,
        error: Optional[str] = None,
        source: str = "data_clean.pipeline",
        priority: int = 0,
        **kwargs: Any,
    ) -> int:
        """
        发布清洗流水线完成事件

        Parameters
        ----------
        symbol : str
            标的代码
        data_type : str
            数据类型
        status : str
            状态（completed/failed）
        cleaned_count : int
            有效数据数
        bad_count : int
            脏数据数
        total_count : int
            总数
        quality_grade : str
            质量等级
        bad_ratio : float | None
            脏数据占比（None则自动计算）
        duration : float
            耗时（秒）
        steps_executed : List[str] | None
            已执行步骤
        bad_file : str | None
            脏数据文件路径
        report_file : str | None
            报告文件路径
        error : str | None
            错误信息
        source : str
            事件来源
        priority : int
            优先级

        Returns
        -------
        int : 触发的处理器数量
        """
        if bad_ratio is None:
            bad_ratio = bad_count / total_count if total_count > 0 else 0.0

        event = PipelineCompleteEvent(
            event_type=EventType.DATA_QUALITY_PIPELINE_COMPLETE,
            symbol=symbol,
            data_type=data_type,
            status=status,
            cleaned_count=cleaned_count,
            bad_count=bad_count,
            total_count=total_count,
            quality_grade=quality_grade,
            bad_ratio=bad_ratio,
            duration=duration,
            steps_executed=steps_executed or [],
            bad_file=bad_file,
            report_file=report_file,
            error=error,
            source=source,
            priority=priority,
            data=kwargs,
        )
        logger.debug(f"发布流水线完成事件: {event.summary()}")
        return self.publish(event)

    def publish_quality_alert(
        self,
        alert_level: str = "warning",
        message: str = "",
        symbol: str = "",
        data_type: str = "",
        quality_grade: str = "",
        bad_ratio: float = 0.0,
        threshold: float = 0.0,
        dirty_type: str = "",
        deduplication_key: str = "",
        source: str = "data_clean.alert",
        priority: int = 0,
        dedup_enabled: bool = True,
        **kwargs: Any,
    ) -> int:
        """
        发布数据质量告警事件（支持去重）

        Parameters
        ----------
        alert_level : str
            告警级别（info/warning/error/critical）
        message : str
            告警消息
        symbol : str
            标的代码
        data_type : str
            数据类型
        quality_grade : str
            质量等级
        bad_ratio : float
            脏数据占比
        threshold : float
            触发阈值
        dirty_type : str
            主要脏数据类型
        deduplication_key : str
            去重键（相同 key 在时间窗口内只告警一次）
        source : str
            事件来源
        priority : int
            优先级
        dedup_enabled : bool
            是否启用去重（默认 True）

        Returns
        -------
        int : 触发的处理器数量（去重抑制时返回 0）
        """
        # 去重检查
        if dedup_enabled and deduplication_key:
            now = time.time()
            with self._lock:
                last_time = self._dedup_cache.get(deduplication_key)
                if last_time is not None and (now - last_time) < self._dedup_window:
                    logger.debug(
                        f"告警去重抑制: key={deduplication_key}, "
                        f"距上次={now - last_time:.1f}s < {self._dedup_window}s"
                    )
                    return 0
                self._dedup_cache[deduplication_key] = now
                # 清理过期的去重缓存
                expired = [k for k, v in self._dedup_cache.items() if (now - v) > self._dedup_window * 10]
                for k in expired:
                    del self._dedup_cache[k]

        event = QualityAlertEvent(
            event_type=EventType.DATA_QUALITY_ALERT,
            alert_level=alert_level,
            message=message,
            symbol=symbol,
            data_type=data_type,
            quality_grade=quality_grade,
            bad_ratio=bad_ratio,
            threshold=threshold,
            dirty_type=dirty_type,
            deduplication_key=deduplication_key,
            source=source,
            priority=priority,
            data=kwargs,
        )
        logger.warning(f"发布质量告警: {event.summary()}")
        return self.publish(event)

    def publish_critical(
        self,
        message: str = "严重数据质量问题，流程已阻断",
        symbol: str = "",
        data_type: str = "",
        bad_ratio: float = 0.0,
        threshold: float = 0.5,
        dirty_type: str = "",
        source: str = "data_clean.critical",
        **kwargs: Any,
    ) -> int:
        """
        发布严重数据质量事件（CRITICAL级，用于风控暂停策略）

        这是 publish_quality_alert 的便捷方法，alert_level 固定为 critical。

        Parameters
        ----------
        message : str
            告警消息
        symbol : str
            标的代码
        data_type : str
            数据类型
        bad_ratio : float
            脏数据占比
        threshold : float
            阈值
        dirty_type : str
            主要脏数据类型
        source : str
            事件来源

        Returns
        -------
        int : 触发的处理器数量
        """
        dedup_key = f"critical:{symbol}:{data_type}:{dirty_type}"
        return self.publish_quality_alert(
            alert_level="critical",
            message=message,
            symbol=symbol,
            data_type=data_type,
            bad_ratio=bad_ratio,
            threshold=threshold,
            dirty_type=dirty_type,
            deduplication_key=dedup_key,
            source=source,
            priority=100,  # 严重事件最高优先级
            **kwargs,
        )

    # ═══════════════════════════════════════════
    #  便捷订阅方法（阶段9新增）
    # ═══════════════════════════════════════════

    def subscribe_data_quality(
        self,
        handler: Callable[[DataQualityEvent], Any],
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
        min_severity: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        """
        便捷订阅脏数据事件（可选过滤条件）

        Parameters
        ----------
        handler : Callable[[DataQualityEvent], Any]
            事件处理器
        symbol : str | None
            只接收指定标的的事件（None=不限）
        data_type : str | None
            只接收指定数据类型的事件（None=不限）
        min_severity : str | None
            最低严重级别（None=不限）
        priority : int
            优先级

        Returns
        -------
        str : handler_id
        """
        evt_filter = None
        if symbol or data_type or min_severity:
            evt_filter = EventFilter(
                symbols={symbol} if symbol else None,
                data_types={data_type} if data_type else None,
                min_severity=min_severity,
            )
        return self.subscribe(
            EventType.DATA_QUALITY_DIRTY,
            handler,
            priority=priority,
            event_filter=evt_filter,
        )

    def subscribe_pipeline_complete(
        self,
        handler: Callable[[PipelineCompleteEvent], Any],
        symbol: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        """便捷订阅流水线完成事件"""
        evt_filter = EventFilter(symbols={symbol} if symbol else None) if symbol else None
        return self.subscribe(
            EventType.DATA_QUALITY_PIPELINE_COMPLETE,
            handler,
            priority=priority,
            event_filter=evt_filter,
        )

    def subscribe_quality_alert(
        self,
        handler: Callable[[QualityAlertEvent], Any],
        min_level: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        """便捷订阅质量告警事件"""
        evt_filter = EventFilter(min_severity=min_level) if min_level else None
        return self.subscribe(
            EventType.DATA_QUALITY_ALERT,
            handler,
            priority=priority,
            event_filter=evt_filter,
        )

    # ═══════════════════════════════════════════
    #  阻塞等待事件（阶段9新增）
    # ═══════════════════════════════════════════

    def wait_for_event(
        self,
        event_type: Union[EventType, str],
        timeout: float = 30.0,
        event_filter: Optional[EventFilter] = None,
    ) -> Optional[Event]:
        """
        阻塞等待指定类型的事件（线程安全，超时返回 None）

        Parameters
        ----------
        event_type : EventType | str
            等待的事件类型
        timeout : float
            超时时间（秒）
        event_filter : EventFilter | None
            可选过滤器

        Returns
        -------
        Event | None : 接收到的事件，超时返回 None
        """
        type_name = event_type.value if isinstance(event_type, EventType) else event_type
        wait_event = threading.Event()
        result: Dict[str, Optional[Event]] = {"event": None}

        def _wait_handler(event: Event):
            if event_filter and not event_filter.matches(event):
                return
            result["event"] = event
            wait_event.set()

        handler_id = self.subscribe(event_type, _wait_handler, event_filter=event_filter)
        try:
            wait_event.wait(timeout=timeout)
            return result["event"]
        finally:
            self.unsubscribe(handler_id)

    def _notify_waiters(self, type_name: str, event: Event) -> None:
        """通知等待该事件类型的线程（内部方法）"""
        # 注意：wait_for_event 通过订阅机制实现，无需额外通知
        # 此方法预留用于未来的条件变量优化
        pass

    # ═══════════════════════════════════════════
    #  历史与统计
    # ═══════════════════════════════════════════

    def get_history(
        self,
        event_type: Optional[Union[EventType, str]] = None,
        limit: int = 100,
    ) -> List[Event]:
        """获取事件历史"""
        if event_type is None:
            return list(self._history)[-limit:]
        type_name = event_type.value if isinstance(event_type, EventType) else event_type
        return [e for e in self._history if e.type_name == type_name][-limit:]

    def clear_history(self) -> None:
        self._history.clear()

    def clear_dedup_cache(self) -> None:
        """清空调警去重缓存（阶段9新增）"""
        with self._lock:
            self._dedup_cache.clear()

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            sync_count = sum(len(v) for v in self._handlers.values())
            async_count = sum(len(v) for v in self._async_handlers.values())
        return {
            "total_events_published": self._event_count,
            "sync_handlers": sync_count,
            "async_handlers": async_count,
            "history_size": len(self._history),
            "dedup_cache_size": len(self._dedup_cache),
        }

    def get_handler_stats(self, handler_id: Optional[str] = None) -> Union[HandlerStats, List[HandlerStats]]:
        """
        获取处理器统计（阶段9新增）

        Parameters
        ----------
        handler_id : str | None
            指定处理器 ID；None 则返回所有处理器统计

        Returns
        -------
        HandlerStats | List[HandlerStats]
        """
        with self._lock:
            all_handlers = []
            for handlers in self._handlers.values():
                all_handlers.extend(handlers)
            for handlers in self._async_handlers.values():
                all_handlers.extend(handlers)

            if handler_id:
                for h in all_handlers:
                    if h.handler_id == handler_id:
                        return h.stats
                raise EventError(f"处理器不存在: {handler_id}")

            return [h.stats for h in all_handlers]

    def get_handler_errors(self, handler_id: str) -> Optional[str]:
        """获取指定处理器的最后错误（阶段9新增）"""
        stats = self.get_handler_stats(handler_id)
        if isinstance(stats, HandlerStats):
            return stats.last_error
        return None

    def reset_handler_stats(self, handler_id: Optional[str] = None) -> int:
        """
        重置处理器统计（阶段9新增）

        Parameters
        ----------
        handler_id : str | None
            指定处理器；None 则重置所有

        Returns
        -------
        int : 重置的处理器数量
        """
        count = 0
        with self._lock:
            all_handlers = []
            for handlers in self._handlers.values():
                all_handlers.extend(handlers)
            for handlers in self._async_handlers.values():
                all_handlers.extend(handlers)

            for h in all_handlers:
                if handler_id is None or h.handler_id == handler_id:
                    h.stats = HandlerStats(handler_id=h.handler_id, event_type=h.event_type)
                    count += 1
        return count

    def __repr__(self) -> str:
        return f"EventBus(events={self._event_count}, handlers={self.stats['sync_handlers']})"


# ═══════════════════════════════════════════════
#  DataQualityEventPublisher 便捷发布器（阶段9新增）
# ═══════════════════════════════════════════════

class DataQualityEventPublisher:
    """
    数据质量事件便捷发布器

    封装 EventBus，提供面向清洗流水线/实盘数据流的简洁发布 API。
    可注入到 CleanPipeline、DataFeed 等组件中，实现事件联动。

    为实盘无缝对接预留:
      - 流式清洗时逐条发布脏数据事件
      - 流水线完成时发布汇总事件
      - 质量降级时发布告警事件
      - 严重脏数据时发布 CRITICAL 事件（触发风控暂停）

    Usage:
        publisher = DataQualityEventPublisher(source="clean_pipeline")

        # 清洗过程中发现脏数据
        publisher.publish_dirty(
            dirty_type="ohlc_logic_error",
            symbol="000001.SZ",
            data_type="bar",
            severity="error",
            errors=["high < low"],
        )

        # 流水线完成
        publisher.pipeline_complete(
            symbol="000001.SZ", data_type="bar",
            cleaned_count=1000, bad_count=5, total_count=1005,
        )

        # 质量告警
        publisher.alert(
            alert_level="warning",
            message="脏数据占比超过5%",
            symbol="000001.SZ", bad_ratio=0.08, threshold=0.05,
        )
    """

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        source: str = "data_clean",
        enabled: bool = True,
    ):
        """
        Parameters
        ----------
        bus : EventBus | None
            事件总线实例（None则使用全局单例）
        source : str
            事件来源标识
        enabled : bool
            是否启用发布（False时所有发布方法返回0，不实际发布）
        """
        self._bus = bus or get_event_bus()
        self._source = source
        self._enabled = enabled
        self._publish_count = 0

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def source(self) -> str:
        return self._source

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def publish_count(self) -> int:
        return self._publish_count

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def publish_dirty(
        self,
        dirty_type: str = "",
        symbol: str = "",
        data_type: str = "",
        severity: str = "error",
        errors: Optional[List[str]] = None,
        record: Optional[Dict[str, Any]] = None,
        quality_grade: str = "",
        bad_count: int = 0,
        total_count: int = 0,
        repair_strategy: str = "",
        source_step: str = "",
        priority: int = 0,
        **kwargs: Any,
    ) -> int:
        """发布脏数据事件"""
        if not self._enabled:
            return 0
        self._publish_count += 1
        return self._bus.publish_data_quality(
            dirty_type=dirty_type,
            symbol=symbol,
            data_type=data_type,
            severity=severity,
            errors=errors,
            record=record,
            quality_grade=quality_grade,
            bad_count=bad_count,
            total_count=total_count,
            repair_strategy=repair_strategy,
            source_step=source_step,
            source=self._source,
            priority=priority,
            **kwargs,
        )

    def pipeline_complete(
        self,
        symbol: str = "",
        data_type: str = "",
        status: str = "completed",
        cleaned_count: int = 0,
        bad_count: int = 0,
        total_count: int = 0,
        quality_grade: str = "",
        bad_ratio: Optional[float] = None,
        duration: float = 0.0,
        steps_executed: Optional[List[str]] = None,
        bad_file: Optional[str] = None,
        report_file: Optional[str] = None,
        error: Optional[str] = None,
        priority: int = 0,
        **kwargs: Any,
    ) -> int:
        """发布流水线完成事件"""
        if not self._enabled:
            return 0
        self._publish_count += 1
        return self._bus.publish_pipeline_complete(
            symbol=symbol,
            data_type=data_type,
            status=status,
            cleaned_count=cleaned_count,
            bad_count=bad_count,
            total_count=total_count,
            quality_grade=quality_grade,
            bad_ratio=bad_ratio,
            duration=duration,
            steps_executed=steps_executed,
            bad_file=bad_file,
            report_file=report_file,
            error=error,
            source=f"{self._source}.pipeline",
            priority=priority,
            **kwargs,
        )

    def alert(
        self,
        alert_level: str = "warning",
        message: str = "",
        symbol: str = "",
        data_type: str = "",
        quality_grade: str = "",
        bad_ratio: float = 0.0,
        threshold: float = 0.0,
        dirty_type: str = "",
        deduplication_key: str = "",
        priority: int = 0,
        dedup_enabled: bool = True,
        **kwargs: Any,
    ) -> int:
        """发布质量告警事件"""
        if not self._enabled:
            return 0
        self._publish_count += 1
        return self._bus.publish_quality_alert(
            alert_level=alert_level,
            message=message,
            symbol=symbol,
            data_type=data_type,
            quality_grade=quality_grade,
            bad_ratio=bad_ratio,
            threshold=threshold,
            dirty_type=dirty_type,
            deduplication_key=deduplication_key,
            source=f"{self._source}.alert",
            priority=priority,
            dedup_enabled=dedup_enabled,
            **kwargs,
        )

    def critical(
        self,
        message: str = "严重数据质量问题，流程已阻断",
        symbol: str = "",
        data_type: str = "",
        bad_ratio: float = 0.0,
        threshold: float = 0.5,
        dirty_type: str = "",
        **kwargs: Any,
    ) -> int:
        """发布严重数据质量事件（CRITICAL级，触发风控暂停）"""
        if not self._enabled:
            return 0
        self._publish_count += 1
        return self._bus.publish_critical(
            message=message,
            symbol=symbol,
            data_type=data_type,
            bad_ratio=bad_ratio,
            threshold=threshold,
            dirty_type=dirty_type,
            source=f"{self._source}.critical",
            **kwargs,
        )

    def subscribe(
        self,
        event_type: Union[EventType, str],
        handler: Callable[[Event], Any],
        priority: int = 0,
        event_filter: Optional[EventFilter] = None,
    ) -> str:
        """订阅事件（委托给 bus）"""
        return self._bus.subscribe(event_type, handler, priority=priority, event_filter=event_filter)

    def unsubscribe(self, handler_id: str) -> bool:
        """取消订阅（委托给 bus）"""
        return self._bus.unsubscribe(handler_id)

    def __repr__(self) -> str:
        return (
            f"DataQualityEventPublisher(source={self._source}, "
            f"enabled={self._enabled}, published={self._publish_count})"
        )


# ═══════════════════════════════════════════════
#  事件批处理收集器（P1 第8项）
# ═══════════════════════════════════════════════


class EventBatch:
    """
    事件批处理收集器（P1 第8项：批处理模式）

    用于将高频事件先累积，再一次性批量发布（publish_many），降低锁竞争
    与 handler 调度开销。

    Usage:
        bus = EventBus()
        with bus.batch(auto_flush=True) as b:
            b.add(event1)
            b.add(event2)      # with 退出时自动 flush

        # 手动 flush
        b = bus.batch()
        b.add(event1)
        b.add_all([event2, event3])
        b.flush()
    """

    def __init__(self, bus: EventBus, auto_flush: bool = False):
        self._bus = bus
        self._events: List[Event] = []
        self._auto_flush = auto_flush
        self._flushed_total = 0

    def add(self, event: Optional[Event]) -> "EventBatch":
        """累积一个事件到批处理队列（None 安全，忽略空事件）。"""
        if event is not None:
            self._events.append(event)
        return self

    def add_all(self, events: Iterable[Event]) -> "EventBatch":
        """批量累积多个事件。"""
        for ev in events:
            self.add(ev)
        return self

    @property
    def size(self) -> int:
        """当前队列中未 flush 的事件数量。"""
        return len(self._events)

    @property
    def events(self) -> List[Event]:
        """当前队列中的事件快照。"""
        return list(self._events)

    def flush(self) -> int:
        """立即批量发布已累积事件并清空队列。

        Returns
        -------
        int : 本次 flush 触发的处理器调用次数
        """
        if not self._events:
            return 0
        n = self._bus.publish_many(self._events)
        self._flushed_total += n
        self._events.clear()
        return n

    @property
    def flushed_total(self) -> int:
        """累计 flush 触发的处理器调用次数。"""
        return self._flushed_total

    def __enter__(self) -> "EventBatch":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if self._auto_flush:
            self.flush()
        return False


# ═══════════════════════════════════════════════
#  全局事件总线单例
# ═══════════════════════════════════════════════

_global_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """获取全局事件总线单例"""
    global _global_bus
    with _bus_lock:
        if _global_bus is None:
            _global_bus = EventBus()
        return _global_bus


def reset_event_bus() -> None:
    """重置全局事件总线单例（测试用）"""
    global _global_bus
    with _bus_lock:
        _global_bus = None


# ═══════════════════════════════════════════════
#  自测 Demo（P1 第8项：批处理模式 + 事件对象池）
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """运行 EventBus 批处理 / 对象池自测。"""
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 60)
    print("event_bus.py 批处理模式自测开始")
    print("=" * 60)

    # 1. publish_many 与逐条 publish 等价（触发数 / handler 接收数）
    bus = EventBus()
    recv = {"n": 0, "events": []}

    def on_ev(event: Event) -> None:
        recv["n"] += 1
        recv["events"].append(event.data)

    bus.subscribe("TEST_EVT", on_ev)
    events = [Event(event_type="TEST_EVT", data={"i": i}) for i in range(5)]
    n1 = bus.publish_many(events)
    check("publish_many 触发数=5", n1 == 5, f"{n1}")
    check("handler 接收数=5", recv["n"] == 5, f"{recv['n']}")
    check("事件数据顺序正确", [e["i"] for e in recv["events"]] == [0, 1, 2, 3, 4], f"{recv['events']}")
    check("历史计数=5", bus._event_count == 5, f"{bus._event_count}")

    # 2. 空列表安全
    n2 = bus.publish_many([])
    check("空列表返回0", n2 == 0 and bus._event_count == 5, f"n2={n2}")

    # 3. 事件过滤器在批处理中生效
    bus2 = EventBus()
    filt_recv = {"n": 0}
    bus2.subscribe(
        EventType.DATA_QUALITY_DIRTY,
        lambda e: filt_recv.__setitem__("n", filt_recv["n"] + 1),
        event_filter=EventFilter(min_severity="error"),
    )
    evs = [
        DataQualityEvent(dirty_type="a", symbol="000001.SZ", severity="error"),
        DataQualityEvent(dirty_type="b", symbol="000001.SZ", severity="warning"),
        DataQualityEvent(dirty_type="c", symbol="000001.SZ", severity="error"),
    ]
    n3 = bus2.publish_many(evs)
    check("过滤器只放行匹配事件(2/3)", n3 == 2 and filt_recv["n"] == 2, f"n3={n3}, n={filt_recv['n']}")

    # 4. once 在批处理中只触发一次
    bus3 = EventBus()
    once_cnt = {"n": 0}
    bus3.once("ONCE_EVT", lambda e: once_cnt.__setitem__("n", once_cnt["n"] + 1))
    bus3.publish_many([Event(event_type="ONCE_EVT", data={})] * 3)
    bus3.publish_many([Event(event_type="ONCE_EVT", data={})] * 3)
    check("once 首次批触发3次后自动取消", once_cnt["n"] == 3, f"{once_cnt['n']}")

    # 5. EventBatch 累积 + flush
    bus4 = EventBus()
    b_cnt = {"n": 0}

    def on_b(event: Event) -> None:
        b_cnt["n"] += 1

    bus4.subscribe("B_EVT", on_b)
    with bus4.batch() as b:
        b.add(Event(event_type="B_EVT", data={"k": 1}))
        b.add_all([Event(event_type="B_EVT", data={"k": 2}), Event(event_type="B_EVT", data={"k": 3})])
        check("with 未退出时不自动 flush", b_cnt["n"] == 0 and b.size == 3, f"n={b_cnt['n']}, size={b.size}")
    check("with 退出后仍为0（auto_flush=False）", b_cnt["n"] == 0, f"{b_cnt['n']}")
    b2 = bus4.batch(auto_flush=True)
    b2.add_all([Event(event_type="B_EVT", data={"k": 4}), Event(event_type="B_EVT", data={"k": 5})])
    with b2:
        pass
    check("auto_flush 退出自动发布", b_cnt["n"] == 2, f"{b_cnt['n']}")
    check("flushed_total 累计=2", b2.flushed_total == 2, f"{b2.flushed_total}")

    # 6. 对象池集成：use_object_pool=True
    bus_pool = EventBus(use_object_pool=True, pool_max_idle=10)
    pool_recv = {"n": 0}

    def on_pool(event: Event) -> None:
        pool_recv["n"] += 1

    bus_pool.subscribe("POOL_EVT", on_pool)
    ev = bus_pool.acquire_event("POOL_EVT", data={"x": 1}, source="test", priority=2)
    check("对象池事件字段正确", ev.data.get("x") == 1 and ev.source == "test" and ev.priority == 2, f"{ev}")
    bus_pool.publish(ev)
    check("对象池事件正常发布", pool_recv["n"] == 1, f"{pool_recv['n']}")
    check("对象池归还成功", bus_pool.release_event(ev) is True, f"{bus_pool.release_event(ev)}")
    pool_stats = bus_pool._event_pool.stats()
    check("对象池归还后 in_use=0", pool_stats["in_use"] == 0, f"{pool_stats}")

    # 7. 默认（未启用对象池）acquire_event 等价直接构造
    bus_default = EventBus()
    ev_d = bus_default.acquire_event("D_EVT", data={"y": 2})
    check("未启用对象池时直接构造", ev_d.data.get("y") == 2 and bus_default._event_pool is None, f"{ev_d}")
    check("未启用对象池时 release 安全", bus_default.release_event(ev_d) is True, f"{bus_default.release_event(ev_d)}")

    # 8. 批处理与逐条发布 handler 接收总数一致（同批次）
    bus_cmp = EventBus()
    cnt_a = {"n": 0}
    cnt_b = {"n": 0}
    bus_cmp.subscribe("CMP", lambda e: cnt_a.__setitem__("n", cnt_a["n"] + 1))
    es = [Event(event_type="CMP", data={"i": i}) for i in range(4)]
    bus_cmp.publish_many(es)
    bus_cmp2 = EventBus()
    bus_cmp2.subscribe("CMP", lambda e: cnt_b.__setitem__("n", cnt_b["n"] + 1))
    for e in es:
        bus_cmp2.publish(e)
    check("批量 vs 逐条 接收总数一致", cnt_a["n"] == cnt_b["n"] == 4, f"batch={cnt_a['n']}, one_by_one={cnt_b['n']}")

    # 9. 交易链路事件（BASE-4 事件链路 id 规范）
    chain_recv: List[Event] = []
    bus_chain = EventBus()
    bus_chain.subscribe("*", lambda e: chain_recv.append(e))
    sig = SignalEvent(
        signal_id="ma_cross:000001.SZ:2026-10-05:ma_cross",
        symbol="000001.SZ",
        strategy_id="ma_cross",
        signal_type="cross_up",
        event_ts=1700000000.0,
    )
    d = sig.to_dict()
    check(
        "SignalEvent 四元组序列化",
        d["signal_id"].startswith("ma_cross:000001.SZ")
        and d["event_ts"] == 1700000000.0
        and d["event_type"] == "strategy.signal_generated",
        f"{d.get('signal_id')}",
    )
    ord_ev = OrderEvent(
        event_type=EventType.ORDER_FILLED,
        signal_id=sig.signal_id,       # 链路连续：同 signal_id
        order_id=sig.signal_id,        # Signal.to_order 幂等派生
        trade_id="T_1001",
        event_ts=1700000000.5,
        symbol="000001.SZ",
        status="filled",
    )
    check(
        "OrderEvent 链路贯穿同 signal_id",
        ord_ev.signal_id == sig.signal_id and ord_ev.order_id == sig.signal_id,
        f"{ord_ev.order_id}",
    )
    bus_chain.publish_chain(sig)
    bus_chain.publish_chain(ord_ev)
    check(
        "publish_chain 发布两事件均送达",
        len(chain_recv) == 2 and all(ev.signal_id == sig.signal_id for ev in chain_recv),
        f"recv={len(chain_recv)}",
    )
    bad = ChainEvent(event_type="test.chain")  # event_type 必填（防御：链路事件类型必须明确）
    check(
        "missing_link_ids 识别四缺",
        bad.missing_link_ids() == ["signal_id", "order_id", "trade_id", "event_ts"],
        f"{bad.missing_link_ids()}",
    )
    rejected = bus_chain.publish_chain(bad, require_event_ts=True)
    check("require_event_ts 拒绝发布缺时间戳事件", rejected == 0, f"triggered={rejected}")
    try:
        bus_chain.publish_chain(Event(EventType.TICK))  # type: ignore[arg-type]
        check("publish_chain 拒绝非链路事件", False, "未抛 TypeError")
    except TypeError:
        check("publish_chain 拒绝非链路事件", True, "TypeError")

    print("=" * 60)
    if failures:
        print(f"自测结果: {passed} 项通过, {len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return len(failures)
    print(f"自测结果: 全部 {passed} 项通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    # 自测 Demo：批处理模式 + 事件对象池（P1 第8项）
    raise SystemExit(_run_self_test())
