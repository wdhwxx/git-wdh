# 文件名：myquant/monitoring/metrics_collector.py
"""
metrics_collector.py - 运行指标采集（阶段9任务11增强：数据质量指标自动采集链路）
====================================================================================
功能:
  1. MetricType 枚举：COUNTER / GAUGE / HISTOGRAM
  2. Metric 数据类：名称、类型、值、标签、帮助文本、时间戳
  3. MetricsCollector 采集器：注册指标、更新值、查询、导出
  4. Prometheus 格式导出：to_prometheus()（符合Prometheus文本格式规范）
  5. 内置指标：事件总线统计、数据质量统计、系统健康、告警统计、实盘预留
  6. 标签维度支持（symbol/data_type/dirty_type/source/step/level/category 等）

阶段9任务11核心增强（数据质量指标自动采集链路）:
  - collect_from_data_quality_event() 从DataQualityEvent实时更新指标（流式支持，实盘必备）
  - collect_from_pipeline_event() 从PipelineCompleteEvent采集指标
  - collect_from_pipeline_result() 增强：从PipelineResult/CleanPipelineResult自动提取完整统计
  - collect_data_quality() 增强：质量等级映射使用QualityGrade枚举、步骤名规范化
  - collect_from_alert_manager() 增强：按级别+分类统计、升级数、聚合待处理数、通道统计
  - create_alert_callback() 创建AlertManager告警回调（告警发送时自动更新system_alerts_total）
  - subscribe_to_event_bus() 订阅事件总线自动采集（无需手动调用collect方法）
  - 新增内置指标：data_quality_total_records / data_quality_repair_count / data_quality_pipeline_runs / data_quality_pipeline_failures
  - 指标过期检测：get_stale_metrics() 长时间未更新的指标标记为stale
  - Prometheus导出增强：可选时间戳、标签值转义、NaN/Inf处理

阶段9任务12-D增强（与RiskManager联动）:
  - collect_from_strategy_pause_event() 从StrategyPauseEvent采集风控暂停/恢复指标
  - 新增内置指标：risk_pause_total / risk_resume_total / risk_paused
  - subscribe_to_event_bus() 增订阅 RISK_PAUSED / RISK_RESUMED（实盘下单开关状态可观测）

A-2 L1 中间件监控增强:
  - 新增内置指标：middleware_queue_depth（RabbitMQ 队列深度，GAUGE×queue）/
    middleware_dlq_count（死信队列积压，GAUGE×queue）/
    middleware_event_bus_backlog（事件总线历史积压条数，GAUGE）/
    middleware_event_bus_backlog_ratio（事件总线积压率 0~1，GAUGE）
  - collect_from_event_bus() 增补事件总线积压/积压率采集（handler 错误/调用已有）
  - collect_from_rabbitmq_bus() 从 RabbitMQ 传输采集队列深度/DLQ 计数
    （transport.get_queue_stats()，未配置 Management 或失败返回 None 不伪造数据）
  - check_middleware_backlog() 积压健康判定：队列深度超阈值→DEGRADED、
    DLQ 超阈值→UNHEALTHY、总线积压率超阈值→DEGRADED、
    无采集数据→DEGRADED（fail-closed），供 HealthPatrol/scheduler 注册巡检

A-3 引擎统一埋点增强:
  - 新增内置指标：engine_orders_total / engine_filled_total /
    engine_rejected_total / engine_trades_total / engine_signals_total /
    engine_risk_rejected_total / engine_errors_total（GAUGE，状态快照）
  - collect_from_engine() 从 LiveEngine.get_status() 采集引擎累计统计，
    联动已有 LIVE_POSITIONS_COUNT / LIVE_PNL_TOTAL（portfolio 注入时）；
    配合引擎侧 publish_chain 发布的 8 类链路事件，/api/metrics 可见
    实盘指标随状态变化（A-3 验收）

为实盘无缝对接预留:
  - /metrics 端点供 Prometheus 抓取（任务16 API 将集成 to_prometheus()）
  - 实时指标更新（流式清洗时逐条 collect_from_data_quality_event）
  - 指标过期机制（长时间未更新的指标标记为 stale，实盘断连检测）
  - 多维度标签（symbol/data_type/source/strategy/step/level/category）
  - 与任务22 scheduler 集成：定期采集并导出
  - 告警回调联动：AlertManager发送告警时自动更新指标
  - 事件总线自动订阅：DataQualityEvent发布时自动采集

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 指标只记录当前和历史已发生的数据，不预测未来

防未来函数说明:
  - 所有指标值基于当前已采集的数据计算
  - 不使用任何预测模型推断未来指标趋势
  - 指标时间戳为采集时刻的当前时间
  - Prometheus 导出只包含当前快照，不包含预测值
  - collect_from_data_quality_event 只处理已发布的事件，不预加载未来数据
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.constants import MetricType  # G2-1 下沉 core，re-export 兼容旧引用
from myquant.core.logger import get_logger
from myquant.core.registry import (  # G2-5：+工厂注册（供 live/scheduler 经 L0 取用）
    register_collector,
    register_component,
)

logger = get_logger("monitoring.metrics")


# ═══════════════════════════════════════════════
#  指标类型枚举（G2-1 下沉 myquant/core/constants；re-export 见顶部导入）
# ═══════════════════════════════════════════════

# ═══════════════════════════════════════════════
#  指标数据类
# ═══════════════════════════════════════════════

@dataclass
class Metric:
    """
    指标数据点

    Attributes
    ----------
    name : str
        指标名称（如 data_quality_dirty_ratio）
    type : MetricType
        指标类型
    value : float
        指标值
    labels : Dict[str, str]
        标签维度（如 {"symbol": "000001.SZ", "data_type": "bar"}）
    help : str
        帮助文本（Prometheus HELP 行）
    timestamp : float
        采集时间戳
    metric_id : str
        指标唯一ID
    """
    name: str
    type: MetricType
    value: float
    labels: Dict[str, str] = field(default_factory=dict)
    help: str = ""
    timestamp: float = field(default_factory=time.time)
    metric_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def labels_key(self) -> str:
        """标签组合的唯一键（用于内部存储）"""
        if not self.labels:
            return ""
        return ",".join(f"{k}={v}" for k, v in sorted(self.labels.items()))

    @property
    def full_name(self) -> str:
        """带标签的完整名称（用于显示）"""
        if not self.labels:
            return self.name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(self.labels.items()))
        return f"{self.name}{{{label_str}}}"

    @property
    def age_seconds(self) -> float:
        """距上次更新的时间（秒）"""
        return time.time() - self.timestamp

    def is_stale(self, max_age_seconds: float = 300.0) -> bool:
        """检查指标是否过期（长时间未更新）"""
        return self.age_seconds > max_age_seconds

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "name": self.name,
            "type": self.type.value,
            "value": self.value,
            "labels": self.labels,
            "help": self.help,
            "timestamp": self.timestamp,
            "age_seconds": round(self.age_seconds, 2),
        }

    def __repr__(self) -> str:
        return f"Metric({self.full_name}={self.value}, type={self.type.value})"


# ═══════════════════════════════════════════════
#  指标定义（注册元数据）
# ═══════════════════════════════════════════════

@dataclass
class _MetricDefinition:
    """内部指标定义（注册时的元数据）"""
    name: str
    type: MetricType
    help: str = ""
    label_names: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════
#  内置指标名称常量
# ═══════════════════════════════════════════════

class BuiltinMetrics:
    """内置指标名称常量"""
    # 事件总线指标
    EVENT_BUS_TOTAL_EVENTS = "event_bus_total_events"
    EVENT_BUS_SYNC_HANDLERS = "event_bus_sync_handlers"
    EVENT_BUS_ASYNC_HANDLERS = "event_bus_async_handlers"
    EVENT_BUS_HANDLER_ERRORS = "event_bus_handler_errors"
    EVENT_BUS_HANDLER_CALLS = "event_bus_handler_calls"

    # 数据质量指标（任务11增强）
    DATA_QUALITY_TOTAL_RECORDS = "data_quality_total_records"
    DATA_QUALITY_DIRTY_COUNT = "data_quality_dirty_count"
    DATA_QUALITY_CLEAN_COUNT = "data_quality_clean_count"
    DATA_QUALITY_DIRTY_RATIO = "data_quality_dirty_ratio"
    DATA_QUALITY_QUALITY_GRADE = "data_quality_quality_grade"
    DATA_QUALITY_STEP_DURATION = "data_quality_step_duration_seconds"
    DATA_QUALITY_PIPELINE_DURATION = "data_quality_pipeline_duration_seconds"
    DATA_QUALITY_DIRTY_TYPE_COUNT = "data_quality_dirty_type_count"
    DATA_QUALITY_REPAIR_COUNT = "data_quality_repair_count"
    DATA_QUALITY_PIPELINE_RUNS = "data_quality_pipeline_runs"
    DATA_QUALITY_PIPELINE_FAILURES = "data_quality_pipeline_failures"

    # 系统指标
    SYSTEM_HEALTH_STATUS = "system_health_status"
    SYSTEM_ALERTS_TOTAL = "system_alerts_total"
    SYSTEM_ALERTS_UNACKNOWLEDGED = "system_alerts_unacknowledged"
    SYSTEM_ALERTS_ESCALATED = "system_alerts_escalated"

    # 实盘指标（为后续预留）
    LIVE_POSITIONS_COUNT = "live_positions_count"
    LIVE_ORDERS_TOTAL = "live_orders_total"
    LIVE_PNL_TOTAL = "live_pnl_total"

    # 风控指标（任务12-D增强：暂停/恢复事件联动，实盘下单开关状态可观测）
    RISK_PAUSE_TOTAL = "risk_pause_total"
    RISK_RESUME_TOTAL = "risk_resume_total"
    RISK_PAUSED = "risk_paused"

    # 中间件指标（A-2 L1 中间件监控）
    MIDDLEWARE_QUEUE_DEPTH = "middleware_queue_depth"
    MIDDLEWARE_DLQ_COUNT = "middleware_dlq_count"
    MIDDLEWARE_EVENT_BUS_BACKLOG = "middleware_event_bus_backlog"
    MIDDLEWARE_EVENT_BUS_BACKLOG_RATIO = "middleware_event_bus_backlog_ratio"

    # 引擎实盘指标（A-3 引擎统一埋点：collect_from_engine 从引擎状态快照采集）
    ENGINE_ORDERS_TOTAL = "engine_orders_total"
    ENGINE_FILLED_TOTAL = "engine_filled_total"
    ENGINE_REJECTED_TOTAL = "engine_rejected_total"
    ENGINE_TRADES_TOTAL = "engine_trades_total"
    ENGINE_SIGNALS_TOTAL = "engine_signals_total"
    ENGINE_RISK_REJECTED_TOTAL = "engine_risk_rejected_total"
    ENGINE_ERRORS_TOTAL = "engine_errors_total"

    # 交易异常告警闭环（A-8：事件到达即同步计数，按事件类型/级别打标签）
    TRADE_EXCEPTION_ALERTS_TOTAL = "trade_exception_alerts_total"

    # 交易所限流/频控监控（A-12：按动作类型累计计数，进 /api/metrics）
    RATE_LIMIT_ORDERS_TOTAL = "rate_limit_orders_total"
    RATE_LIMIT_QUERIES_TOTAL = "rate_limit_queries_total"
    RATE_LIMIT_CANCELS_TOTAL = "rate_limit_cancels_total"

    # 安全监控（A-13：按事件类型计数进 /api/metrics；P0-7 不含密钥原文）
    SECURITY_EVENTS_TOTAL = "security_events_total"

    # ══ B-2 实盘资金/PnL 指标（权益曲线时序回放；资金类落时序库经 BASE-9 加密）══
    # 防脏门：仅当账户对账一致（未冻结）时才写入时序库（collect_pnl_snapshot）。
    LIVE_TOTAL_ASSETS = "live_total_assets"                 # 总资产（权益曲线主序列）
    LIVE_AVAILABLE_CASH = "live_available_cash"             # 可用资金
    LIVE_POSITION_MARKET_VALUE = "live_position_market_value"  # 持仓市值
    LIVE_REALIZED_PNL = "live_realized_pnl"                 # 已实现盈亏
    LIVE_UNREALIZED_PNL = "live_unrealized_pnl"             # 未实现盈亏

    # ══ Wave1 各分片指标注册清单（B-1~B-9；名以各模块实际常量为准，类型按语义）══
    # B-3 限额使用率（GAUGE；labels: dimension/enabled）
    LIMIT_USAGE_CONCENTRATION = "limit_usage_concentration"
    LIMIT_USAGE_LEVERAGE = "limit_usage_leverage"
    LIMIT_USAGE_SINGLE_STOCK = "limit_usage_single_stock"
    # B-4/5 执行链路延迟/滑点 + 拒单（P99/均值 GAUGE；累计计数 COUNTER）
    EXEC_SIGNAL_TO_FILL_P99_MS = "execution_signal_to_fill_p99_ms"
    EXEC_ACK_P99_MS = "execution_ack_p99_ms"
    EXEC_SIGNAL_TO_SUBMIT_P99_MS = "execution_signal_to_submit_p99_ms"
    EXEC_SUBMIT_TO_FILL_P99_MS = "execution_submit_to_fill_p99_ms"
    SLIPPAGE_BUY_PCT = "slippage_buy_pct"
    SLIPPAGE_SELL_PCT = "slippage_sell_pct"
    SLIPPAGE_BUY_P99_PCT = "slippage_buy_p99_pct"
    SLIPPAGE_SELL_P99_PCT = "slippage_sell_p99_pct"
    ORDER_REJECT_RATE = "order_reject_rate"
    ORDERS_SUBMITTED_TOTAL = "orders_submitted_total"
    ORDERS_REJECTED_TOTAL = "orders_rejected_total"
    # B-7 数据新鲜度 SLI（GAUGE；新鲜度 lag 带 data_key 标签）
    DATA_FRESHNESS_LAG_SECONDS = "data_freshness_lag_seconds"
    DATA_GAP_COUNT = "data_gap_count"
    FACTOR_SYNC_DELAY_SECONDS = "factor_sync_delay_seconds"
    FINANCIAL_SYNC_DELAY_SECONDS = "financial_sync_delay_seconds"
    # B-9 保证金监控（GAUGE）
    MARGIN_USAGE_RATIO = "margin_usage_ratio"
    MAINTENANCE_MARGIN = "maintenance_margin"
    LIQUIDATION_DISTANCE_PCT = "liquidation_distance_pct"
    FUNDING_RATE = "funding_rate"
    FUNDING_COST_ESTIMATE = "funding_cost_estimate"

    # 质量等级数值映射（0=excellent, 1=good, 2=fair, 3=poor, 4=critical）
    QUALITY_GRADE_MAP = {
        "excellent": 0,
        "good": 1,
        "fair": 2,
        "poor": 3,
        "critical": 4,
    }


# ═══════════════════════════════════════════════
#  MetricsCollector 指标采集器（任务11增强）
# ═══════════════════════════════════════════════

class MetricsCollector:
    """
    指标采集器

    管理指标的注册、更新、查询和导出，支持 Prometheus 文本格式。

    阶段9任务11增强：
      - collect_from_data_quality_event() 实时事件采集（流式支持）
      - collect_from_pipeline_event() 流水线事件采集
      - collect_from_pipeline_result() 增强自动提取
      - create_alert_callback() 告警回调联动
      - subscribe_to_event_bus() 事件总线自动订阅
      - get_stale_metrics() 指标过期检测
      - Prometheus导出增强（时间戳/转义/NaN处理）

    阶段9任务12-D增强（与RiskManager联动）：
      - collect_from_strategy_pause_event() 策略暂停/恢复事件采集
      - risk_pause_total / risk_resume_total / risk_paused 内置指标

    Usage:
        collector = MetricsCollector()
        collector.register_builtin_metrics()

        # 实时事件采集（实盘流式场景）
        collector.collect_from_data_quality_event(data_quality_event)

        # 告警回调联动
        alert_manager.add_alert_callback(collector.create_alert_callback())

        # 事件总线自动订阅
        collector.subscribe_to_event_bus(event_bus)

        # 导出 Prometheus 格式
        prom_text = collector.to_prometheus()
    """

    def __init__(self, max_metrics_per_name: int = 1000, default_stale_seconds: float = 300.0):
        """
        Parameters
        ----------
        max_metrics_per_name : int
            每个指标名称下最多保留的标签组合数（防止内存泄漏）
        default_stale_seconds : float
            默认指标过期时间（秒），超过此时间未更新视为stale
        """
        self._definitions: Dict[str, _MetricDefinition] = {}
        self._metrics: Dict[str, Dict[str, Metric]] = {}  # name -> labels_key -> Metric
        self._max_metrics_per_name = max_metrics_per_name
        self._default_stale_seconds = default_stale_seconds
        self._collect_count = 0
        self._event_bus_subscribed: bool = False
        self._event_bus_handler_ids: List[str] = []

    # ═══════════════════════════════════════════
    #  指标注册
    # ═══════════════════════════════════════════

    def register(
        self,
        name: str,
        metric_type: MetricType,
        help: str = "",
        label_names: Optional[List[str]] = None,
    ) -> "MetricsCollector":
        """
        注册指标定义

        Parameters
        ----------
        name : str
            指标名称
        metric_type : MetricType
            指标类型
        help : str
            帮助文本
        label_names : List[str] | None
            允许的标签名称列表（None=不限制）

        Returns
        -------
        MetricsCollector : self（链式调用）
        """
        self._definitions[name] = _MetricDefinition(
            name=name,
            type=metric_type,
            help=help,
            label_names=label_names or [],
        )
        if name not in self._metrics:
            self._metrics[name] = {}
        logger.debug(f"注册指标: {name} (type={metric_type.value})")
        return self

    def is_registered(self, name: str) -> bool:
        """检查指标是否已注册"""
        return name in self._definitions

    def get_definition(self, name: str) -> Optional[_MetricDefinition]:
        """获取指标定义"""
        return self._definitions.get(name)

    def register_builtin_metrics(self) -> "MetricsCollector":
        """
        注册内置指标（事件总线 + 数据质量 + 系统 + 实盘预留）

        任务11增强：新增4个数据质量指标 + 1个系统指标
        """
        # 事件总线指标
        self.register(BuiltinMetrics.EVENT_BUS_TOTAL_EVENTS, MetricType.COUNTER,
                      "Total number of events published on the event bus")
        self.register(BuiltinMetrics.EVENT_BUS_SYNC_HANDLERS, MetricType.GAUGE,
                      "Number of active sync event handlers")
        self.register(BuiltinMetrics.EVENT_BUS_ASYNC_HANDLERS, MetricType.GAUGE,
                      "Number of active async event handlers")
        self.register(BuiltinMetrics.EVENT_BUS_HANDLER_ERRORS, MetricType.COUNTER,
                      "Total number of handler execution errors",
                      label_names=["handler_id", "event_type"])
        self.register(BuiltinMetrics.EVENT_BUS_HANDLER_CALLS, MetricType.COUNTER,
                      "Total number of handler calls",
                      label_names=["handler_id", "event_type"])

        # 数据质量指标（任务11增强：新增total_records/repair_count/pipeline_runs/pipeline_failures）
        self.register(BuiltinMetrics.DATA_QUALITY_TOTAL_RECORDS, MetricType.COUNTER,
                      "Total number of data records processed by cleaning pipeline",
                      label_names=["symbol", "data_type"])
        self.register(BuiltinMetrics.DATA_QUALITY_DIRTY_COUNT, MetricType.COUNTER,
                      "Count of dirty data records found during cleaning",
                      label_names=["symbol", "data_type", "dirty_type"])
        self.register(BuiltinMetrics.DATA_QUALITY_CLEAN_COUNT, MetricType.COUNTER,
                      "Count of clean data records after cleaning",
                      label_names=["symbol", "data_type"])
        self.register(BuiltinMetrics.DATA_QUALITY_DIRTY_RATIO, MetricType.GAUGE,
                      "Ratio of dirty data records (bad / total)",
                      label_names=["symbol", "data_type"])
        self.register(BuiltinMetrics.DATA_QUALITY_QUALITY_GRADE, MetricType.GAUGE,
                      "Data quality grade level (0=excellent, 1=good, 2=fair, 3=poor, 4=critical)",
                      label_names=["symbol", "data_type"])
        self.register(BuiltinMetrics.DATA_QUALITY_STEP_DURATION, MetricType.HISTOGRAM,
                      "Duration of each cleaning step in seconds",
                      label_names=["symbol", "data_type", "step"])
        self.register(BuiltinMetrics.DATA_QUALITY_PIPELINE_DURATION, MetricType.HISTOGRAM,
                      "Total duration of cleaning pipeline in seconds",
                      label_names=["symbol", "data_type"])
        self.register(BuiltinMetrics.DATA_QUALITY_DIRTY_TYPE_COUNT, MetricType.COUNTER,
                      "Count of dirty data by type",
                      label_names=["dirty_type"])
        self.register(BuiltinMetrics.DATA_QUALITY_REPAIR_COUNT, MetricType.COUNTER,
                      "Count of repaired data records by repair strategy",
                      label_names=["symbol", "data_type", "repair_strategy"])
        self.register(BuiltinMetrics.DATA_QUALITY_PIPELINE_RUNS, MetricType.COUNTER,
                      "Total number of cleaning pipeline runs",
                      label_names=["symbol", "data_type", "status"])
        self.register(BuiltinMetrics.DATA_QUALITY_PIPELINE_FAILURES, MetricType.COUNTER,
                      "Total number of cleaning pipeline failures",
                      label_names=["symbol", "data_type", "error_type"])

        # 系统指标（任务11增强：新增alerts_escalated）
        self.register(BuiltinMetrics.SYSTEM_HEALTH_STATUS, MetricType.GAUGE,
                      "System health status (0=healthy, 1=degraded, 2=unhealthy)")
        self.register(BuiltinMetrics.SYSTEM_ALERTS_TOTAL, MetricType.COUNTER,
                      "Total number of alerts sent",
                      label_names=["level", "category"])
        self.register(BuiltinMetrics.SYSTEM_ALERTS_UNACKNOWLEDGED, MetricType.GAUGE,
                      "Number of unacknowledged alerts")
        self.register(BuiltinMetrics.SYSTEM_ALERTS_ESCALATED, MetricType.COUNTER,
                      "Total number of escalated alerts (WARNING -> ERROR)")

        # 实盘指标（为后续预留）
        self.register(BuiltinMetrics.LIVE_POSITIONS_COUNT, MetricType.GAUGE,
                      "Current number of open positions")
        self.register(BuiltinMetrics.LIVE_ORDERS_TOTAL, MetricType.COUNTER,
                      "Total number of orders placed")
        self.register(BuiltinMetrics.LIVE_PNL_TOTAL, MetricType.GAUGE,
                      "Total realized PnL")

        # 风控指标（任务12-D增强：暂停/恢复事件联动）
        self.register(BuiltinMetrics.RISK_PAUSE_TOTAL, MetricType.COUNTER,
                      "Total number of strategy pauses triggered by risk control",
                      label_names=["pause_level", "symbol"])
        self.register(BuiltinMetrics.RISK_RESUME_TOTAL, MetricType.COUNTER,
                      "Total number of strategy resumes",
                      label_names=["pause_level", "symbol"])
        self.register(BuiltinMetrics.RISK_PAUSED, MetricType.GAUGE,
                      "Current strategy paused state (1=paused, 0=resumed)",
                      label_names=["pause_level", "symbol"])

        # 中间件指标（A-2 L1 中间件监控）
        self.register(BuiltinMetrics.MIDDLEWARE_QUEUE_DEPTH, MetricType.GAUGE,
                      "RabbitMQ queue message depth (ready + unacknowledged)",
                      label_names=["queue"])
        self.register(BuiltinMetrics.MIDDLEWARE_DLQ_COUNT, MetricType.GAUGE,
                      "RabbitMQ dead-letter queue backlog count",
                      label_names=["queue"])
        self.register(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG, MetricType.GAUGE,
                      "Event bus history backlog size (events retained)")
        self.register(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG_RATIO, MetricType.GAUGE,
                      "Event bus history backlog ratio (0.0 ~ 1.0, 1.0 = ring full)")

        # 引擎实盘指标（A-3 引擎统一埋点：collect_from_engine 状态快照采集）
        self.register(BuiltinMetrics.ENGINE_ORDERS_TOTAL, MetricType.GAUGE,
                      "Total orders placed by live engine (snapshot)")
        self.register(BuiltinMetrics.ENGINE_FILLED_TOTAL, MetricType.GAUGE,
                      "Total filled orders by live engine (snapshot)")
        self.register(BuiltinMetrics.ENGINE_REJECTED_TOTAL, MetricType.GAUGE,
                      "Total rejected orders by live engine (snapshot)")
        self.register(BuiltinMetrics.ENGINE_TRADES_TOTAL, MetricType.GAUGE,
                      "Total trades filled by live engine (snapshot)")
        self.register(BuiltinMetrics.ENGINE_SIGNALS_TOTAL, MetricType.GAUGE,
                      "Total signals generated by live engine (snapshot)")
        self.register(BuiltinMetrics.ENGINE_RISK_REJECTED_TOTAL, MetricType.GAUGE,
                      "Total signals rejected by risk control (snapshot)")
        self.register(BuiltinMetrics.ENGINE_ERRORS_TOTAL, MetricType.GAUGE,
                      "Total errors in live engine (snapshot)")

        # 交易异常告警闭环（A-8：拒单/失败/成交回报异常/强平事件计数）
        self.register(BuiltinMetrics.TRADE_EXCEPTION_ALERTS_TOTAL, MetricType.COUNTER,
                      "Total trade exception events triggering alerts "
                      "(order.rejected/order.failed/fill.report_error/emergency.liquidation)",
                      label_names=["event_type", "level"])

        # 交易所限流/频控监控（A-12：报单/查询/撤单累计计数，供 /api/metrics 观测窗口压力）
        self.register(BuiltinMetrics.RATE_LIMIT_ORDERS_TOTAL, MetricType.COUNTER,
                      "Total order submissions observed by exchange rate-limit monitor (cumulative)")
        self.register(BuiltinMetrics.RATE_LIMIT_QUERIES_TOTAL, MetricType.COUNTER,
                      "Total quote/query requests observed by exchange rate-limit monitor (cumulative)")
        self.register(BuiltinMetrics.RATE_LIMIT_CANCELS_TOTAL, MetricType.COUNTER,
                      "Total cancel requests observed by exchange rate-limit monitor (cumulative)")

        # 安全监控（A-13：登录失败/未授权访问/凭证变更/密钥泄漏按事件类型计数；P0-7 不含密钥原文）
        self.register(BuiltinMetrics.SECURITY_EVENTS_TOTAL, MetricType.COUNTER,
                      "Total security events detected by security monitor "
                      "(login_failed/unauthorized_access/credential_change/secret_exposure)",
                      label_names=["event_type"])

        # ── B-2 实盘资金/PnL 指标（GAUGE；防脏门：对账一致才写时序库）──
        self.register(BuiltinMetrics.LIVE_TOTAL_ASSETS, MetricType.GAUGE,
                      "Live account total assets (equity curve primary series; reconciled, frozen-gated)",
                      label_names=["account_id"])
        self.register(BuiltinMetrics.LIVE_AVAILABLE_CASH, MetricType.GAUGE,
                      "Live account available cash",
                      label_names=["account_id"])
        self.register(BuiltinMetrics.LIVE_POSITION_MARKET_VALUE, MetricType.GAUGE,
                      "Live aggregate position market value",
                      label_names=["account_id"])
        self.register(BuiltinMetrics.LIVE_REALIZED_PNL, MetricType.GAUGE,
                      "Live realized PnL (reconciled, frozen-gated)",
                      label_names=["account_id"])
        self.register(BuiltinMetrics.LIVE_UNREALIZED_PNL, MetricType.GAUGE,
                      "Live unrealized PnL (reconciled, frozen-gated)",
                      label_names=["account_id"])

        # ── B-3 限额使用率（GAUGE；enabled=false 表示该维度未配置限额 PENDING_CAL）──
        self.register(BuiltinMetrics.LIMIT_USAGE_CONCENTRATION, MetricType.GAUGE,
                      "Concentration limit usage ratio (max single-symbol exposure / total assets)",
                      label_names=["dimension", "enabled"])
        self.register(BuiltinMetrics.LIMIT_USAGE_LEVERAGE, MetricType.GAUGE,
                      "Leverage limit usage ratio (total market value / net assets)",
                      label_names=["dimension", "enabled"])
        self.register(BuiltinMetrics.LIMIT_USAGE_SINGLE_STOCK, MetricType.GAUGE,
                      "Single-stock position limit usage ratio",
                      label_names=["dimension", "enabled"])

        # ── B-4/5 执行链路延迟/滑点/拒单（P99 与均值 GAUGE；累计 COUNTER）──
        self.register(BuiltinMetrics.EXEC_SIGNAL_TO_FILL_P99_MS, MetricType.GAUGE,
                      "P99 signal-to-fill latency in milliseconds")
        self.register(BuiltinMetrics.EXEC_ACK_P99_MS, MetricType.GAUGE,
                      "P99 order-send-to-ack latency in milliseconds")
        self.register(BuiltinMetrics.EXEC_SIGNAL_TO_SUBMIT_P99_MS, MetricType.GAUGE,
                      "P99 signal-to-submit latency in milliseconds")
        self.register(BuiltinMetrics.EXEC_SUBMIT_TO_FILL_P99_MS, MetricType.GAUGE,
                      "P99 submit-to-fill latency in milliseconds")
        self.register(BuiltinMetrics.SLIPPAGE_BUY_PCT, MetricType.GAUGE,
                      "Mean buy slippage in percent (positive = paid more than ref)")
        self.register(BuiltinMetrics.SLIPPAGE_SELL_PCT, MetricType.GAUGE,
                      "Mean sell slippage in percent (negative = sold below ref)")
        self.register(BuiltinMetrics.SLIPPAGE_BUY_P99_PCT, MetricType.GAUGE,
                      "P99 buy slippage in percent")
        self.register(BuiltinMetrics.SLIPPAGE_SELL_P99_PCT, MetricType.GAUGE,
                      "P99 sell slippage in percent")
        self.register(BuiltinMetrics.ORDER_REJECT_RATE, MetricType.GAUGE,
                      "Order rejection rate over recent window (rejected / submitted)")
        self.register(BuiltinMetrics.ORDERS_SUBMITTED_TOTAL, MetricType.COUNTER,
                      "Total orders submitted (cumulative)")
        self.register(BuiltinMetrics.ORDERS_REJECTED_TOTAL, MetricType.COUNTER,
                      "Total orders rejected (cumulative)")

        # ── B-7 数据新鲜度 SLI（GAUGE；lag 按 data_key 标签）──
        self.register(BuiltinMetrics.DATA_FRESHNESS_LAG_SECONDS, MetricType.GAUGE,
                      "Latest data freshness lag in seconds (per data key)",
                      label_names=["data_key"])
        self.register(BuiltinMetrics.DATA_GAP_COUNT, MetricType.GAUGE,
                      "Count of daily data gaps detected")
        self.register(BuiltinMetrics.FACTOR_SYNC_DELAY_SECONDS, MetricType.GAUGE,
                      "Factor data sync delay in seconds")
        self.register(BuiltinMetrics.FINANCIAL_SYNC_DELAY_SECONDS, MetricType.GAUGE,
                      "Financial data sync delay in seconds")

        # ── B-9 保证金监控（GAUGE）──
        self.register(BuiltinMetrics.MARGIN_USAGE_RATIO, MetricType.GAUGE,
                      "Margin usage ratio in percent (occupied margin / equity)")
        self.register(BuiltinMetrics.MAINTENANCE_MARGIN, MetricType.GAUGE,
                      "Maintenance margin requirement")
        self.register(BuiltinMetrics.LIQUIDATION_DISTANCE_PCT, MetricType.GAUGE,
                      "Distance to liquidation in percent")
        self.register(BuiltinMetrics.FUNDING_RATE, MetricType.GAUGE,
                      "Current funding rate")
        self.register(BuiltinMetrics.FUNDING_COST_ESTIMATE, MetricType.GAUGE,
                      "Estimated funding cost (overnight)")

        return self

    # ═══════════════════════════════════════════
    #  指标更新
    # ═══════════════════════════════════════════

    def _ensure_metric(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> Metric:
        """确保指标存在（不存在则创建，自动注册）"""
        labels = labels or {}
        if name not in self._definitions:
            # 自动注册为 GAUGE 类型
            self.register(name, MetricType.GAUGE, f"Auto-registered metric: {name}")

        if name not in self._metrics:
            self._metrics[name] = {}

        labels_key = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

        if labels_key not in self._metrics[name]:
            # 防止内存泄漏
            if len(self._metrics[name]) >= self._max_metrics_per_name:
                # 移除最旧的指标
                oldest_key = next(iter(self._metrics[name]))
                del self._metrics[name][oldest_key]
                logger.warning(f"指标 {name} 超过最大标签组合数，已移除最旧的")

            definition = self._definitions[name]
            self._metrics[name][labels_key] = Metric(
                name=name,
                type=definition.type,
                value=0.0,
                labels=dict(labels),
                help=definition.help,
            )

        return self._metrics[name][labels_key]

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        设置仪表盘指标值（可增可减）

        Parameters
        ----------
        name : str
            指标名称
        value : float
            指标值
        labels : Dict[str, str] | None
            标签维度
        """
        metric = self._ensure_metric(name, labels)
        metric.value = float(value)
        metric.timestamp = time.time()

    def inc_counter(
        self,
        name: str,
        amount: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        增加计数器指标值（只增不减）

        Parameters
        ----------
        name : str
            指标名称
        amount : float
            增加量（必须 >= 0）
        labels : Dict[str, str] | None
            标签维度
        """
        if amount < 0:
            logger.warning(f"计数器 {name} 尝试增加负值 {amount}，已忽略")
            return
        metric = self._ensure_metric(name, labels)
        metric.value += amount
        metric.timestamp = time.time()

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        观察直方图指标（当前版本简化为记录最新值）

        Parameters
        ----------
        name : str
            指标名称
        value : float
            观察值
        labels : Dict[str, str] | None
            标签维度
        """
        metric = self._ensure_metric(name, labels)
        metric.value = float(value)
        metric.timestamp = time.time()

    def reset(self, name: Optional[str] = None) -> int:
        """
        重置指标值

        Parameters
        ----------
        name : str | None
            指定指标名称（None=重置所有）

        Returns
        -------
        int : 重置的指标数据点数量
        """
        count = 0
        if name:
            if name in self._metrics:
                count = len(self._metrics[name])
                self._metrics[name] = {}
        else:
            for metrics_dict in self._metrics.values():
                count += len(metrics_dict)
            self._metrics = {name: {} for name in self._definitions}
        return count

    # ═══════════════════════════════════════════
    #  指标查询
    # ═══════════════════════════════════════════

    def get(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
    ) -> Optional[Metric]:
        """
        获取单个指标数据点

        Parameters
        ----------
        name : str
            指标名称
        labels : Dict[str, str] | None
            标签维度（None=获取无标签的指标）

        Returns
        -------
        Metric | None
        """
        if name not in self._metrics:
            return None
        labels = labels or {}
        labels_key = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return self._metrics[name].get(labels_key)

    def get_all(self, name: Optional[str] = None) -> List[Metric]:
        """
        获取所有指标数据点

        Parameters
        ----------
        name : str | None
            指定指标名称（None=获取所有指标）

        Returns
        -------
        List[Metric]
        """
        if name:
            return list(self._metrics.get(name, {}).values())
        result = []
        for metrics_dict in self._metrics.values():
            result.extend(metrics_dict.values())
        return result

    def get_value(
        self,
        name: str,
        labels: Optional[Dict[str, str]] = None,
        default: float = 0.0,
    ) -> float:
        """获取指标值（便捷方法）"""
        metric = self.get(name, labels)
        return metric.value if metric else default

    def list_metric_names(self) -> List[str]:
        """列出所有已注册的指标名称"""
        return list(self._definitions.keys())

    @property
    def metrics_count(self) -> int:
        """当前指标数据点总数"""
        return sum(len(v) for v in self._metrics.values())

    @property
    def definitions_count(self) -> int:
        """已注册指标定义数"""
        return len(self._definitions)

    def get_stale_metrics(self, max_age_seconds: Optional[float] = None) -> List[Metric]:
        """
        获取过期的指标（长时间未更新）

        任务11新增：用于实盘断连检测、数据源异常监控

        Parameters
        ----------
        max_age_seconds : float | None
            最大允许未更新时间（None=使用默认值）

        Returns
        -------
        List[Metric] : 过期的指标列表
        """
        threshold = max_age_seconds or self._default_stale_seconds
        stale = []
        for metrics_dict in self._metrics.values():
            for metric in metrics_dict.values():
                if metric.is_stale(threshold):
                    stale.append(metric)
        return stale

    # ═══════════════════════════════════════════
    #  Prometheus 格式导出（任务11增强）
    # ═══════════════════════════════════════════

    def to_prometheus(self, include_timestamp: bool = False) -> str:
        """
        导出为 Prometheus 文本格式

        任务11增强：
          - 可选时间戳（include_timestamp=True时附加毫秒时间戳）
          - 标签值转义（双引号/反斜杠/换行符）
          - NaN/Inf值处理（Prometheus不支持，输出为NaN/+Inf/-Inf）
          - 空指标跳过（无数据点的指标不输出）

        Parameters
        ----------
        include_timestamp : bool
            是否在数据行末尾附加时间戳（毫秒）

        Returns
        -------
        str : Prometheus 格式的指标文本

        格式说明:
            # HELP metric_name help text
            # TYPE metric_name counter|gauge|histogram
            metric_name{label1="value1",label2="value2"} value [timestamp_ms]
        """
        lines = []
        for name, definition in self._definitions.items():
            metrics = self._metrics.get(name, {})
            if not metrics:
                continue

            # HELP 行
            if definition.help:
                lines.append(f"# HELP {name} {definition.help}")
            # TYPE 行
            lines.append(f"# TYPE {name} {definition.type.value}")

            # 数据点
            for metric in metrics.values():
                label_str = ""
                if metric.labels:
                    label_parts = []
                    for k, v in sorted(metric.labels.items()):
                        escaped_v = self._escape_prometheus_label_value(v)
                        label_parts.append(f'{k}="{escaped_v}"')
                    label_str = "{" + ",".join(label_parts) + "}"

                # 格式化值（处理NaN/Inf）
                value_str = self._format_prometheus_value(metric.value)

                # 数据行
                data_line = f"{name}{label_str} {value_str}"
                if include_timestamp:
                    timestamp_ms = int(metric.timestamp * 1000)
                    data_line += f" {timestamp_ms}"
                lines.append(data_line)

        return "\n".join(lines) + "\n" if lines else ""

    @staticmethod
    def _escape_prometheus_label_value(value: str) -> str:
        """转义Prometheus标签值（双引号/反斜杠/换行符）"""
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    @staticmethod
    def _format_prometheus_value(value: float) -> str:
        """格式化 Prometheus 指标值（处理整数/小数/NaN/Inf）"""
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
        if value == int(value):
            return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def to_dict(self) -> Dict[str, Any]:
        """导出为字典格式（JSON 友好）"""
        result = {}
        for name, metrics in self._metrics.items():
            if not metrics:
                continue
            definition = self._definitions.get(name)
            result[name] = {
                "type": definition.type.value if definition else "unknown",
                "help": definition.help if definition else "",
                "data_points": [m.to_dict() for m in metrics.values()],
            }
        return result

    # ═══════════════════════════════════════════
    #  从事件总线采集指标（内置）
    # ═══════════════════════════════════════════

    def collect_from_event_bus(self, bus: Any) -> int:
        """
        从事件总线采集指标

        Parameters
        ----------
        bus : EventBus
            事件总线实例

        Returns
        -------
        int : 采集的指标数量
        """
        count = 0
        try:
            stats = bus.stats
            self.set_gauge(BuiltinMetrics.EVENT_BUS_TOTAL_EVENTS,
                           float(stats.get("total_events_published", 0)))
            self.set_gauge(BuiltinMetrics.EVENT_BUS_SYNC_HANDLERS,
                           float(stats.get("sync_handlers", 0)))
            self.set_gauge(BuiltinMetrics.EVENT_BUS_ASYNC_HANDLERS,
                           float(stats.get("async_handlers", 0)))
            count += 3

            # A-2 L1 中间件监控：事件总线积压（历史环形缓冲占用条数 + 积压率）
            history_size = int(stats.get("history_size", 0) or 0)
            self.set_gauge(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG,
                           float(history_size))
            count += 1
            try:
                history = getattr(bus, "_history", None)
                maxlen = getattr(history, "maxlen", None) if history is not None else None
                if maxlen and maxlen > 0:
                    ratio = min(1.0, history_size / float(maxlen))
                    self.set_gauge(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG_RATIO,
                                   float(ratio))
                    count += 1
            except Exception as e:
                # 容量不可得时只保留积压条数，不阻断主链路
                logger.debug(f"事件总线积压率采集异常: {e}")

            # 处理器级别统计
            try:
                all_stats = bus.get_handler_stats()
                for hs in all_stats:
                    labels = {"handler_id": hs.handler_id, "event_type": hs.event_type}
                    self.set_gauge(BuiltinMetrics.EVENT_BUS_HANDLER_CALLS,
                                   float(hs.call_count), labels=labels)
                    self.set_gauge(BuiltinMetrics.EVENT_BUS_HANDLER_ERRORS,
                                   float(hs.error_count), labels=labels)
                    count += 2
            except Exception as e:
                # #R1修复：单个handler指标采集失败不影响其他handler，但记录debug日志便于排查
                logger.debug(f"事件总线handler指标采集异常: {e}")

            self._collect_count += 1
            logger.debug(f"从事件总线采集了 {count} 个指标")
        except Exception as e:
            logger.error(f"从事件总线采集指标失败: {e}", exc_info=True)
        return count

    # ═══════════════════════════════════════════
    #  A-2 L1 中间件监控：RabbitMQ 队列深度/DLQ 采集
    # ═══════════════════════════════════════════

    def collect_from_rabbitmq_bus(self, transport: Any) -> int:
        """
        从 RabbitMQ 传输采集队列深度 / 死信队列（DLQ）积压计数（A-2）

        依赖 transport.get_queue_stats()：未配置 Management HTTP API 或采集失败
        返回 None——本方法记 warning 并返回 0，不伪造指标（fail-closed）。

        Parameters
        ----------
        transport : RabbitMQTransport
            RabbitMQ 传输实例（需实现 get_queue_stats()）

        Returns
        -------
        int : 采集的指标数量（不可用时为 0）
        """
        count = 0
        try:
            stats = transport.get_queue_stats()
            if stats is None:
                logger.warning(
                    "RabbitMQ 队列深度采集不可用（未配置 management_url 或采集失败），"
                    "middleware_queue_depth / middleware_dlq_count 不更新"
                )
                return 0
            for queue_name, depth in stats.items():
                if queue_name.endswith(".dlq"):
                    self.set_gauge(BuiltinMetrics.MIDDLEWARE_DLQ_COUNT,
                                   float(depth), labels={"queue": queue_name})
                else:
                    self.set_gauge(BuiltinMetrics.MIDDLEWARE_QUEUE_DEPTH,
                                   float(depth), labels={"queue": queue_name})
                count += 1
            self._collect_count += 1
            logger.debug(f"从 RabbitMQ 采集了 {count} 个中间件指标")
        except Exception as e:
            logger.error(f"从 RabbitMQ 采集指标失败: {e}", exc_info=True)
        return count

    # ═══════════════════════════════════════════
    #  A-3 引擎统一埋点：实盘引擎状态采集
    # ═══════════════════════════════════════════

    def collect_from_engine(self, engine: Any) -> int:
        """
        从实盘引擎采集实盘指标（A-3，随巡检/调度周期调用）

        从 engine.get_status() 状态快照采集累计统计（GAUGE 复制当前值，
        与 EVENT_BUS_TOTAL_EVENTS 的既有 set_gauge 行为一致），并联动
        已有 LIVE_POSITIONS_COUNT / LIVE_PNL_TOTAL（portfolio 注入时）。

        Parameters
        ----------
        engine : LiveEngine
            实盘引擎实例（需实现 get_status()）

        Returns
        -------
        int : 采集的指标数量（引擎状态不可用时为 0）
        """
        count = 0
        try:
            status = engine.get_status()
            stats = status.get("stats", {}) or {}

            # 引擎累计统计快照（只读已发生事实，防未来）
            snapshot_mapping = {
                BuiltinMetrics.ENGINE_ORDERS_TOTAL: stats.get("total_orders", 0),
                BuiltinMetrics.ENGINE_FILLED_TOTAL: stats.get("filled_orders", 0),
                BuiltinMetrics.ENGINE_REJECTED_TOTAL: stats.get("rejected_orders", 0),
                BuiltinMetrics.ENGINE_TRADES_TOTAL: stats.get("total_trades", 0),
                BuiltinMetrics.ENGINE_SIGNALS_TOTAL: stats.get("total_signals", 0),
                BuiltinMetrics.ENGINE_RISK_REJECTED_TOTAL: stats.get("risk_rejected", 0),
                BuiltinMetrics.ENGINE_ERRORS_TOTAL: stats.get("errors", 0),
            }
            for name, value in snapshot_mapping.items():
                self.set_gauge(name, float(value or 0))
                count += 1

            # 实盘持仓数/PnL（已有 LIVE_* 内置指标；portfolio 未注入则跳过）
            portfolio = getattr(engine, "_portfolio", None)
            if portfolio is not None:
                try:
                    self.set_gauge(BuiltinMetrics.LIVE_POSITIONS_COUNT,
                                   float(portfolio.get_position_count() or 0))
                    count += 1
                except Exception as e:
                    logger.debug(f"引擎持仓数采集异常: {e}")
                try:
                    self.set_gauge(BuiltinMetrics.LIVE_PNL_TOTAL,
                                   float(portfolio.get_total_pnl() or 0))
                    count += 1
                except Exception as e:
                    logger.debug(f"引擎 PnL 采集异常: {e}")

            self._collect_count += 1
            logger.debug(f"从引擎采集了 {count} 个指标")
        except Exception as e:
            logger.error(f"从引擎采集指标失败: {e}", exc_info=True)
        return count

    # ═══════════════════════════════════════════
    #  A-2 L1 中间件监控：积压健康判定（超阈值告警）
    # ═══════════════════════════════════════════

    def check_middleware_backlog(
        self,
        queue_depth_threshold: float = 1000.0,
        dlq_threshold: float = 100.0,
        backlog_ratio_threshold: float = 0.9,
    ) -> Any:
        """
        中间件积压健康检查（A-2，供 HealthPatrol / scheduler 注册为巡检项）

        判定规则：
          - 任一 RabbitMQ 队列深度 > queue_depth_threshold → DEGRADED（处理延迟风险）
          - 任一 DLQ 积压计数 > dlq_threshold → UNHEALTHY（消息丢失风险，需人工介入）
          - 事件总线积压率 > backlog_ratio_threshold → DEGRADED（handler 跟不上）
          - 4 类中间件指标均无数据点 → DEGRADED（无采集数据不得伪装健康，fail-closed）

        Parameters
        ----------
        queue_depth_threshold : float
            队列深度告警阈值（默认 1000 条）
        dlq_threshold : float
            DLQ 积压告警阈值（默认 100 条）
        backlog_ratio_threshold : float
            事件总线积压率告警阈值（默认 0.9）

        Returns
        -------
        HealthCheckResult : 判定结果（message 汇总超限项，details 含明细）
        """
        from myquant.core.health_check import HealthCheckResult, HealthStatus

        problems: List[str] = []
        unhealthy = False
        details: Dict[str, Any] = {}

        depth_metrics = self.get_all(BuiltinMetrics.MIDDLEWARE_QUEUE_DEPTH)
        dlq_metrics = self.get_all(BuiltinMetrics.MIDDLEWARE_DLQ_COUNT)
        backlog_metrics = self.get_all(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG)
        backlog_ratio_metrics = self.get_all(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG_RATIO)

        # 无任何中间件数据点：不可评估，fail-closed 判降级
        if not (depth_metrics or dlq_metrics or backlog_metrics or backlog_ratio_metrics):
            return HealthCheckResult(
                name="middleware_backlog",
                status=HealthStatus.DEGRADED,
                message="中间件指标未采集（无数据点），无法确认健康",
                details={"queues": [], "dlq_over": [], "backlog_ratio_over": []},
            )

        # 1. 队列深度
        depth_over = []
        for m in depth_metrics:
            if m.value > queue_depth_threshold:
                depth_over.append(m.labels.get("queue", "?"))
                problems.append(
                    f"队列 {m.labels.get('queue', '?')} 深度 {m.value:.0f} > "
                    f"阈值 {queue_depth_threshold:.0f}"
                )
        details["queues"] = depth_over

        # 2. DLQ 积压（更严重：消息可能已丢失）
        dlq_over = []
        for m in dlq_metrics:
            if m.value > dlq_threshold:
                dlq_over.append(m.labels.get("queue", "?"))
                problems.append(
                    f"DLQ {m.labels.get('queue', '?')} 积压 {m.value:.0f} > "
                    f"阈值 {dlq_threshold:.0f}"
                )
                unhealthy = True
        details["dlq_over"] = dlq_over

        # 3. 事件总线积压率
        ratio_over = []
        for m in backlog_ratio_metrics:
            if m.value > backlog_ratio_threshold:
                ratio_over.append(round(m.value, 4))
                problems.append(
                    f"事件总线积压率 {m.value:.2%} > 阈值 {backlog_ratio_threshold:.0%}"
                )
        details["backlog_ratio_over"] = ratio_over

        if unhealthy:
            status = HealthStatus.UNHEALTHY
        elif problems:
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.HEALTHY

        message = "; ".join(problems) if problems else "中间件积压正常"
        return HealthCheckResult(
            name="middleware_backlog",
            status=status,
            message=message,
            details=details,
        )

    # ═══════════════════════════════════════════
    #  任务11核心：数据质量指标自动采集
    # ═══════════════════════════════════════════

    def collect_from_data_quality_event(self, event: Any) -> int:
        """
        从DataQualityEvent实时采集数据质量指标（任务11核心，流式支持）

        适用于实盘流式清洗场景：每条脏数据事件发布时自动更新指标。

        采集的指标:
          - data_quality_dirty_count（按symbol/data_type/dirty_type标签）
          - data_quality_dirty_type_count（按dirty_type标签）
          - data_quality_dirty_ratio（按symbol/data_type标签）
          - data_quality_total_records（按symbol/data_type标签）

        Parameters
        ----------
        event : DataQualityEvent
            脏数据事件对象（需包含dirty_type/symbol/data_type/bad_count/total_count/severity属性）

        Returns
        -------
        int : 采集/更新的指标数量
        """
        count = 0
        try:
            dirty_type = getattr(event, "dirty_type", "") or "unknown"
            symbol = getattr(event, "symbol", "") or ""
            data_type = getattr(event, "data_type", "") or ""
            bad_count = int(getattr(event, "bad_count", 0) or 0)
            total_count = int(getattr(event, "total_count", 0) or 0)

            # 标签
            dq_labels = {}
            if symbol:
                dq_labels["symbol"] = symbol
            if data_type:
                dq_labels["data_type"] = data_type

            dirty_labels = dict(dq_labels)
            dirty_labels["dirty_type"] = dirty_type

            # 1. 脏数据计数（按symbol/data_type/dirty_type）
            if bad_count > 0:
                self.inc_counter(BuiltinMetrics.DATA_QUALITY_DIRTY_COUNT,
                                 float(bad_count), labels=dirty_labels)
                count += 1

                # 2. 脏数据类型分布（按dirty_type）
                self.inc_counter(BuiltinMetrics.DATA_QUALITY_DIRTY_TYPE_COUNT,
                                 float(bad_count), labels={"dirty_type": dirty_type})
                count += 1

            # 3. 总记录数
            if total_count > 0:
                self.inc_counter(BuiltinMetrics.DATA_QUALITY_TOTAL_RECORDS,
                                 float(total_count), labels=dq_labels)
                count += 1

                # 4. 脏数据占比（GAUGE，按symbol/data_type）
                if dq_labels:
                    dirty_ratio = bad_count / total_count if total_count > 0 else 0.0
                    self.set_gauge(BuiltinMetrics.DATA_QUALITY_DIRTY_RATIO,
                                   dirty_ratio, labels=dq_labels)
                    count += 1

            logger.debug(f"从DataQualityEvent采集了 {count} 个指标 "
                         f"(symbol={symbol}, dirty_type={dirty_type}, bad={bad_count}/{total_count})")
        except Exception as e:
            logger.error(f"从DataQualityEvent采集指标失败: {e}", exc_info=True)
        return count

    def collect_from_pipeline_event(self, event: Any) -> int:
        """
        从PipelineCompleteEvent采集数据质量指标（任务11新增）

        适用于流水线完成事件发布时自动采集完整统计。

        采集的指标:
          - data_quality_pipeline_runs（按symbol/data_type/status标签）
          - data_quality_pipeline_failures（失败时，按symbol/data_type/error_type）
          - data_quality_dirty_count / clean_count / dirty_ratio / quality_grade
          - data_quality_pipeline_duration

        Parameters
        ----------
        event : PipelineCompleteEvent
            流水线完成事件对象

        Returns
        -------
        int : 采集的指标数量
        """
        count = 0
        try:
            symbol = getattr(event, "symbol", "") or ""
            data_type = getattr(event, "data_type", "") or ""
            status = getattr(event, "status", "completed") or "completed"
            quality_grade = getattr(event, "quality_grade", "") or ""
            float(getattr(event, "bad_ratio", 0) or 0)
            cleaned_count = int(getattr(event, "cleaned_count", 0) or 0)
            bad_count = int(getattr(event, "bad_count", 0) or 0)
            total_count = int(getattr(event, "total_count", 0) or 0)
            duration = float(getattr(event, "duration", 0) or 0)
            error = getattr(event, "error", "") or ""

            labels = {}
            if symbol:
                labels["symbol"] = symbol
            if data_type:
                labels["data_type"] = data_type

            # 1. 流水线运行计数
            run_labels = dict(labels)
            run_labels["status"] = status
            self.inc_counter(BuiltinMetrics.DATA_QUALITY_PIPELINE_RUNS,
                             1.0, labels=run_labels)
            count += 1

            # 2. 流水线失败计数
            if status == "failed":
                fail_labels = dict(labels)
                fail_labels["error_type"] = error[:50] if error else "unknown"
                self.inc_counter(BuiltinMetrics.DATA_QUALITY_PIPELINE_FAILURES,
                                 1.0, labels=fail_labels)
                count += 1

            # 3. 数据质量统计（成功时）
            if status == "completed" and total_count > 0:
                count += self.collect_data_quality(
                    symbol=symbol,
                    data_type=data_type,
                    total_count=total_count,
                    clean_count=cleaned_count,
                    bad_count=bad_count,
                    quality_grade=quality_grade,
                    total_duration=duration,
                )

            logger.debug(f"从PipelineCompleteEvent采集了 {count} 个指标 "
                         f"(symbol={symbol}, status={status}, grade={quality_grade})")
        except Exception as e:
            logger.error(f"从PipelineCompleteEvent采集指标失败: {e}", exc_info=True)
        return count

    def collect_data_quality(
        self,
        symbol: str = "",
        data_type: str = "",
        total_count: int = 0,
        clean_count: int = 0,
        bad_count: int = 0,
        quality_grade: str = "",
        step_durations: Optional[Dict[str, float]] = None,
        total_duration: float = 0.0,
        dirty_type_counts: Optional[Dict[str, int]] = None,
        repair_counts: Optional[Dict[str, int]] = None,
    ) -> int:
        """
        采集数据质量指标（任务11增强）

        任务11增强:
          - 质量等级映射使用BuiltinMetrics.QUALITY_GRADE_MAP
          - 新增repair_counts参数（修复策略计数）
          - 步骤名规范化（去除空格、统一小写）
          - 更好的标签处理（空标签不添加）

        Parameters
        ----------
        symbol : str
            标的代码
        data_type : str
            数据类型（bar/tick）
        total_count : int
            原始数据总数
        clean_count : int
            清洗后有效数据数
        bad_count : int
            脏数据数
        quality_grade : str
            质量等级（excellent/good/fair/poor/critical）
        step_durations : Dict[str, float] | None
            各清洗步骤耗时（步骤名→秒）
        total_duration : float
            流水线总耗时（秒）
        dirty_type_counts : Dict[str, int] | None
            脏数据类型分布（类型名→数量）
        repair_counts : Dict[str, int] | None
            修复策略计数（策略名→数量），任务11新增

        Returns
        -------
        int : 采集的指标数量
        """
        count = 0
        labels = {}
        if symbol:
            labels["symbol"] = symbol
        if data_type:
            labels["data_type"] = data_type

        try:
            # 1. 总记录数
            if total_count > 0:
                self.inc_counter(BuiltinMetrics.DATA_QUALITY_TOTAL_RECORDS,
                                 float(total_count), labels=labels)
                count += 1

            # 2. 计数指标
            if total_count > 0 or bad_count > 0 or clean_count > 0:
                if bad_count > 0:
                    self.inc_counter(BuiltinMetrics.DATA_QUALITY_DIRTY_COUNT,
                                     float(bad_count), labels=labels)
                    count += 1
                if clean_count > 0:
                    self.inc_counter(BuiltinMetrics.DATA_QUALITY_CLEAN_COUNT,
                                     float(clean_count), labels=labels)
                    count += 1

                # 3. 脏数据占比
                if total_count > 0 and labels:
                    dirty_ratio = bad_count / total_count
                    self.set_gauge(BuiltinMetrics.DATA_QUALITY_DIRTY_RATIO,
                                   dirty_ratio, labels=labels)
                    count += 1

            # 4. 质量等级（映射为数值）
            if quality_grade and labels:
                grade_value = BuiltinMetrics.QUALITY_GRADE_MAP.get(quality_grade, -1)
                self.set_gauge(BuiltinMetrics.DATA_QUALITY_QUALITY_GRADE,
                               float(grade_value), labels=labels)
                count += 1

            # 5. 步骤耗时
            if step_durations:
                for step_name, duration in step_durations.items():
                    step_labels = dict(labels)
                    # 步骤名规范化：去除空格、统一小写
                    normalized_step = step_name.strip().lower().replace(" ", "_")
                    step_labels["step"] = normalized_step
                    self.observe_histogram(BuiltinMetrics.DATA_QUALITY_STEP_DURATION,
                                            float(duration), labels=step_labels)
                    count += 1

            # 6. 总耗时
            if total_duration > 0 and labels:
                self.observe_histogram(BuiltinMetrics.DATA_QUALITY_PIPELINE_DURATION,
                                        float(total_duration), labels=labels)
                count += 1

            # 7. 脏数据类型分布
            if dirty_type_counts:
                for dirty_type, cnt in dirty_type_counts.items():
                    if cnt > 0:
                        self.inc_counter(BuiltinMetrics.DATA_QUALITY_DIRTY_TYPE_COUNT,
                                         float(cnt), labels={"dirty_type": dirty_type})
                        count += 1

            # 8. 修复策略计数（任务11新增）
            if repair_counts:
                for repair_strategy, cnt in repair_counts.items():
                    if cnt > 0:
                        repair_labels = dict(labels)
                        repair_labels["repair_strategy"] = repair_strategy
                        self.inc_counter(BuiltinMetrics.DATA_QUALITY_REPAIR_COUNT,
                                         float(cnt), labels=repair_labels)
                        count += 1

            logger.debug(f"采集数据质量指标 {count} 个 (symbol={symbol}, type={data_type})")
        except Exception as e:
            logger.error(f"采集数据质量指标失败: {e}", exc_info=True)

        return count

    def collect_from_pipeline_result(self, pipeline_result: Any) -> int:
        """
        从 PipelineResult / CleanPipelineResult 对象采集数据质量指标（任务11增强）

        任务11增强：
          - 支持多种结果对象结构（PipelineResult/CleanPipelineResult/自定义结果）
          - 自动提取stats字典（total/cleaned/bad/quality_grade/bad_ratio）
          - 自动提取step_durations（从step_results/steps）
          - 自动提取dirty_type_counts（从quality_report/dirty_details）
          - 自动提取repair_counts（从repair_results/repaired）
          - 失败状态自动采集pipeline_failures

        Parameters
        ----------
        pipeline_result : Any
            清洗流水线结果对象

        Returns
        -------
        int : 采集的指标数量
        """
        try:
            symbol = getattr(pipeline_result, "symbol", "") or ""
            data_type = getattr(pipeline_result, "data_type", "") or ""

            # 提取stats（支持多种结构）
            stats = getattr(pipeline_result, "stats", None)
            if stats is None:
                stats = {}
            if isinstance(stats, dict):
                total_count = int(stats.get("total", 0) or 0)
                clean_count = int(stats.get("cleaned", stats.get("clean", 0)) or 0)
                bad_count = int(stats.get("bad", stats.get("dirty", 0)) or 0)
                quality_grade = str(stats.get("quality_grade", "") or "")
            else:
                total_count = int(getattr(stats, "total", 0) or 0)
                clean_count = int(getattr(stats, "cleaned", 0) or 0)
                bad_count = int(getattr(stats, "bad", 0) or 0)
                quality_grade = str(getattr(stats, "quality_grade", "") or "")

            # 从cleaned/bad列表提取数量
            if clean_count == 0:
                cleaned_list = getattr(pipeline_result, "cleaned", None)
                if cleaned_list is not None:
                    clean_count = len(cleaned_list)
            if bad_count == 0:
                bad_list = getattr(pipeline_result, "bad", None)
                if bad_list is not None:
                    bad_count = len(bad_list)

            # 总耗时
            total_duration = float(getattr(pipeline_result, "total_duration", 0) or 0)
            if total_duration == 0:
                total_duration = float(getattr(pipeline_result, "duration", 0) or 0)

            # 步骤耗时（支持多种结构）
            step_durations = getattr(pipeline_result, "step_durations", None)
            if step_durations is None:
                step_results = getattr(pipeline_result, "step_results", None)
                if step_results and isinstance(step_results, dict):
                    step_durations = {}
                    for step_name, sr in step_results.items():
                        if isinstance(sr, dict):
                            step_durations[step_name] = float(sr.get("duration", 0) or 0)
                        else:
                            step_durations[step_name] = float(getattr(sr, "duration", 0) or 0)

            # 脏数据类型分布
            dirty_type_counts = getattr(pipeline_result, "dirty_type_counts", None)
            if dirty_type_counts is None:
                quality_report = getattr(pipeline_result, "quality_report", None)
                if quality_report and isinstance(quality_report, dict):
                    dirty_type_counts = quality_report.get("dirty_type_counts")

            # 修复策略计数
            repair_counts = getattr(pipeline_result, "repair_counts", None)
            if repair_counts is None:
                repair_results = getattr(pipeline_result, "repair_results", None)
                if repair_results and isinstance(repair_results, dict):
                    repair_counts = {}
                    for strategy, rr in repair_results.items():
                        if isinstance(rr, dict):
                            repair_counts[strategy] = int(rr.get("repaired", 0) or 0)
                        else:
                            repair_counts[strategy] = int(getattr(rr, "repaired", 0) or 0)

            # 状态判断
            status = getattr(pipeline_result, "status", "completed") or "completed"
            if status == "failed":
                # 失败时采集失败计数
                fail_labels = {}
                if symbol:
                    fail_labels["symbol"] = symbol
                if data_type:
                    fail_labels["data_type"] = data_type
                fail_labels["error_type"] = str(getattr(pipeline_result, "error", "unknown") or "unknown")[:50]
                self.inc_counter(BuiltinMetrics.DATA_QUALITY_PIPELINE_FAILURES,
                                 1.0, labels=fail_labels)

                # 运行计数
                run_labels = dict(fail_labels)
                run_labels["status"] = "failed"
                del run_labels["error_type"]
                self.inc_counter(BuiltinMetrics.DATA_QUALITY_PIPELINE_RUNS,
                                 1.0, labels=run_labels)
                return 2

            # 成功时采集完整数据质量指标
            return self.collect_data_quality(
                symbol=symbol,
                data_type=data_type,
                total_count=total_count,
                clean_count=clean_count,
                bad_count=bad_count,
                quality_grade=quality_grade,
                step_durations=step_durations,
                total_duration=total_duration,
                dirty_type_counts=dirty_type_counts,
                repair_counts=repair_counts,
            )

        except Exception as e:
            logger.error(f"从PipelineResult采集指标失败: {e}", exc_info=True)
            return 0

    # ═══════════════════════════════════════════
    #  从健康检查器/告警管理器采集（任务11增强）
    # ═══════════════════════════════════════════

    def collect_from_health_checker(self, checker: Any) -> int:
        """
        从健康检查器采集系统健康指标

        Parameters
        ----------
        checker : HealthChecker
            健康检查器实例

        Returns
        -------
        int : 采集的指标数量
        """
        try:
            summary = checker.get_summary()
            status_map = {"healthy": 0, "degraded": 1, "unhealthy": 2}
            status_value = status_map.get(summary.get("overall_status", "healthy"), 1)
            self.set_gauge(BuiltinMetrics.SYSTEM_HEALTH_STATUS, float(status_value))
            return 1
        except Exception as e:
            logger.error(f"从健康检查器采集指标失败: {e}", exc_info=True)
            return 0

    def collect_from_alert_manager(self, alert_manager: Any) -> int:
        """
        从告警管理器采集告警指标（任务11增强）

        任务11增强：
          - 按级别+分类统计（而非仅按级别）
          - 采集升级告警数
          - 采集聚合待处理数
          - 采集通道统计（发送数/错误数）

        Parameters
        ----------
        alert_manager : AlertManager
            告警管理器实例

        Returns
        -------
        int : 采集的指标数量
        """
        count = 0
        try:
            stats = alert_manager.stats

            # 1. 未确认告警数
            self.set_gauge(BuiltinMetrics.SYSTEM_ALERTS_UNACKNOWLEDGED,
                           float(stats.get("unacknowledged_count", 0)))
            count += 1

            # 2. 升级告警数
            escalated_count = stats.get("escalated_count", 0)
            if escalated_count > 0:
                self.set_gauge(BuiltinMetrics.SYSTEM_ALERTS_ESCALATED,
                               float(escalated_count))
                count += 1

            # 3. 按级别+分类统计（从告警历史聚合）
            try:
                history = alert_manager.get_history(limit=500)
                level_category_counts: Dict[Tuple[str, str], int] = {}
                for alert in history:
                    key = (alert.level.value, alert.category)
                    level_category_counts[key] = level_category_counts.get(key, 0) + 1

                for (level, category), cnt in level_category_counts.items():
                    self.set_gauge(BuiltinMetrics.SYSTEM_ALERTS_TOTAL,
                                   float(cnt), labels={"level": level, "category": category})
                    count += 1
            except Exception:
                # 回退：按级别统计
                for level, cnt in stats.get("level_counts", {}).items():
                    self.set_gauge(BuiltinMetrics.SYSTEM_ALERTS_TOTAL,
                                   float(cnt), labels={"level": level, "category": "all"})
                    count += 1

            logger.debug(f"从告警管理器采集了 {count} 个指标")
        except Exception as e:
            logger.error(f"从告警管理器采集指标失败: {e}", exc_info=True)
        return count

    # ═══════════════════════════════════════════
    #  任务12-D核心：策略暂停/恢复事件采集（与RiskManager联动）
    # ═══════════════════════════════════════════

    def collect_from_strategy_pause_event(self, event: Any) -> int:
        """
        从StrategyPauseEvent采集风控暂停/恢复指标（任务12-D，与RiskManager联动）

        适用于实盘场景：RiskManager发布暂停/恢复事件时自动更新指标，
        使实盘下单开关状态（是否被风控暂停）实时可观测（Prometheus/API/看板）。

        采集的指标:
          - risk_pause_total（按pause_level/symbol标签）: 累计暂停次数
          - risk_resume_total（按pause_level/symbol标签）: 累计恢复次数
          - risk_paused（按pause_level/symbol标签）: 当前是否暂停（1=暂停，0=恢复）

        标签说明:
          - pause_level=global 时 symbol 固定为 ALL（全局暂停是系统级状态）
          - pause_level=symbol 时 symbol 为具体标的代码（按标的跟踪状态）

        Parameters
        ----------
        event : StrategyPauseEvent
            策略暂停/恢复事件（由RiskManager发布）

        Returns
        -------
        int : 更新/采集的指标数量
        """
        count = 0
        try:
            paused = bool(getattr(event, "paused", True))
            pause_level = getattr(event, "pause_level", "global") or "global"
            symbol = getattr(event, "symbol", "") or ""
            scope_symbol = "ALL" if pause_level == "global" else (symbol or "ALL")
            labels = {"pause_level": pause_level, "symbol": scope_symbol}

            if paused:
                self.inc_counter(BuiltinMetrics.RISK_PAUSE_TOTAL, 1.0, labels=labels)
                self.set_gauge(BuiltinMetrics.RISK_PAUSED, 1.0, labels=labels)
                count += 2
            else:
                self.inc_counter(BuiltinMetrics.RISK_RESUME_TOTAL, 1.0, labels=labels)
                self.set_gauge(BuiltinMetrics.RISK_PAUSED, 0.0, labels=labels)
                count += 2

            self._collect_count += 1
            logger.debug(f"策略暂停事件采集指标: paused={paused}, level={pause_level}, symbol={scope_symbol}")
            return count
        except Exception as e:
            logger.error(f"采集策略暂停事件指标失败: {e}", exc_info=True)
            return 0

    # ═══════════════════════════════════════════
    #  A-8 交易异常告警闭环：交易异常事件计数（事件到达即同步计数）
    # ═══════════════════════════════════════════

    def collect_from_trade_exception_event(self, event: Any) -> int:
        """
        从交易异常事件采集计数指标（A-8，触发模式 EVENT）

        事件到达即同步递增 ``trade_exception_alerts_total``，按
        ``event_type``（order.rejected/order.failed/fill.report_error/
        emergency.liquidation）与 ``level``（error/critical）打标签，
        使拒单/成交失败/强平实时可观测（/api/metrics 与看板）。

        防未来：只对"已发生"的交易异常事件计数（event_ts 为业务真实发生时刻）。

        Parameters
        ----------
        event : OrderEvent | SystemErrorEvent | Event
            交易异常事件（强类型或通用 Event；兼容 data 字典兜底取 event_type）

        Returns
        -------
        int : 更新的指标数量（成功=1，异常=0）
        """
        try:
            from myquant.core.constants import TRADE_EXCEPTION_ALERT_LEVEL_MAP
            from myquant.core.event_bus import EventType

            et = getattr(event, "event_type", None)
            et_name: str = et.value if isinstance(et, EventType) else str(et)
            level = TRADE_EXCEPTION_ALERT_LEVEL_MAP.get(et_name)
            level_value = level.value if level is not None else "error"

            self.inc_counter(
                BuiltinMetrics.TRADE_EXCEPTION_ALERTS_TOTAL,
                1.0,
                labels={"event_type": et_name, "level": level_value},
            )
            self._collect_count += 1
            logger.debug(
                f"交易异常事件计数: event_type={et_name}, level={level_value}"
            )
            return 1
        except Exception as e:
            logger.error(f"采集交易异常事件指标失败: {e}", exc_info=True)
            return 0

    # ═══════════════════════════════════════════
    #  任务11核心：告警回调 + 事件总线自动订阅
    # ═══════════════════════════════════════════

    def create_alert_callback(self) -> Callable[[Any], None]:
        """
        创建告警发送回调函数（任务11核心，与AlertManager联动）

        当AlertManager发送告警时，自动更新system_alerts_total指标。

        Usage:
            alert_manager.add_alert_callback(metrics_collector.create_alert_callback())

        Returns
        -------
        Callable[[Alert], None] : 回调函数
        """
        collector = self

        def alert_callback(alert: Any) -> None:
            """告警发送回调：更新system_alerts_total指标"""
            try:
                level = getattr(alert, "level", None)
                level_value = level.value if hasattr(level, "value") else str(level)
                category = getattr(alert, "category", "system") or "system"

                labels = {"level": level_value, "category": category}
                collector.inc_counter(BuiltinMetrics.SYSTEM_ALERTS_TOTAL, 1.0, labels=labels)

                # 升级告警额外计数
                if getattr(alert, "is_escalated", False):
                    collector.inc_counter(BuiltinMetrics.SYSTEM_ALERTS_ESCALATED, 1.0)

                logger.debug(f"告警回调更新指标: level={level_value}, category={category}")
            except Exception as e:
                logger.error(f"告警回调执行异常: {e}", exc_info=True)

        return alert_callback

    def subscribe_to_event_bus(self, bus: Any) -> List[str]:
        """
        订阅事件总线，自动采集数据质量指标（任务11核心）

        订阅后，DataQualityEvent和PipelineCompleteEvent发布时自动采集指标，
        无需手动调用collect方法。

        订阅的事件:
          - DATA_QUALITY_DIRTY → collect_from_data_quality_event()
          - DATA_QUALITY_PIPELINE_COMPLETE → collect_from_pipeline_event()
          - RISK_PAUSED / RISK_RESUMED → collect_from_strategy_pause_event()（任务12-D）

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

        # 订阅脏数据事件
        hid1 = bus.subscribe(
            EventType.DATA_QUALITY_DIRTY,
            lambda e: self.collect_from_data_quality_event(e),
        )
        handler_ids.append(hid1)

        # 订阅流水线完成事件
        hid2 = bus.subscribe(
            EventType.DATA_QUALITY_PIPELINE_COMPLETE,
            lambda e: self.collect_from_pipeline_event(e),
        )
        handler_ids.append(hid2)

        # 任务12-D：订阅策略暂停/恢复事件（与RiskManager联动，自动采集）
        hid3 = bus.subscribe(
            EventType.RISK_PAUSED,
            lambda e: self.collect_from_strategy_pause_event(e),
        )
        handler_ids.append(hid3)

        hid4 = bus.subscribe(
            EventType.RISK_RESUMED,
            lambda e: self.collect_from_strategy_pause_event(e),
        )
        handler_ids.append(hid4)

        # A-8：订阅交易异常事件，事件到达即同步计数（实时可观测）
        hid5 = bus.subscribe(
            EventType.ORDER_REJECTED,
            lambda e: self.collect_from_trade_exception_event(e),
        )
        handler_ids.append(hid5)

        hid6 = bus.subscribe(
            EventType.ORDER_FAILED,
            lambda e: self.collect_from_trade_exception_event(e),
        )
        handler_ids.append(hid6)

        hid7 = bus.subscribe(
            EventType.FILL_REPORT_ERROR,
            lambda e: self.collect_from_trade_exception_event(e),
        )
        handler_ids.append(hid7)

        hid8 = bus.subscribe(
            EventType.EMERGENCY_LIQUIDATION,
            lambda e: self.collect_from_trade_exception_event(e),
        )
        handler_ids.append(hid8)

        self._event_bus_subscribed = True
        self._event_bus_handler_ids = handler_ids
        logger.info(f"MetricsCollector 已订阅事件总线，handler_ids={handler_ids}")
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

    @property
    def event_bus_subscribed(self) -> bool:
        """是否已订阅事件总线"""
        return self._event_bus_subscribed

    # ═══════════════════════════════════════════
    #  统计与调试
    # ═══════════════════════════════════════════

    @property
    def stats(self) -> Dict[str, Any]:
        """获取采集器统计信息"""
        return {
            "definitions_count": self.definitions_count,
            "metrics_count": self.metrics_count,
            "collect_count": self._collect_count,
            "event_bus_subscribed": self._event_bus_subscribed,
            "default_stale_seconds": self._default_stale_seconds,
            "stale_metrics_count": len(self.get_stale_metrics()),
            "metric_names": self.list_metric_names(),
        }

    def __repr__(self) -> str:
        return (
            f"<MetricsCollector definitions={len(self._definitions)} "
            f"data_points={self.metrics_count} "
            f"collect_count={self._collect_count} "
            f"event_bus_subscribed={self._event_bus_subscribed}>"
        )


# ═══════════════════════════════════════════════
#  全局指标采集器单例
# ═══════════════════════════════════════════════

_global_collector: Optional[MetricsCollector] = None
_collector_lock = None  # 延迟导入


def get_metrics_collector() -> MetricsCollector:
    """获取全局指标采集器单例"""
    global _global_collector, _collector_lock
    if _collector_lock is None:
        import threading
        _collector_lock = threading.Lock()
    with _collector_lock:
        if _global_collector is None:
            _global_collector = MetricsCollector()
            _global_collector.register_builtin_metrics()
            register_collector(_global_collector)  # G2-1 自注册 core.registry（供 alerting 等经低层获取）
        return _global_collector


def reset_metrics_collector() -> None:
    """重置全局指标采集器（测试用）"""
    global _global_collector
    _global_collector = None


# G2-5：指标采集器工厂注册（模块导入即注册；live/scheduler 经 core.registry.get_component("metrics_collector") 取用，
# L4→L0 合法，消除 EX-13C 装配违规；reset 后工厂懒建重建，无 stale）
register_component("metrics_collector", get_metrics_collector)


# ═══════════════════════════════════════════════
#  B-2：PnL 采集（防脏门）+ 时序回放（权益曲线 / 最新快照）
# ═══════════════════════════════════════════════

#: B-2 写入时序库的 PnL/资金指标清单（GAUGE；4 个命中 BASE-9 敏感名需加密落盘）
PNL_EQUITY_METRICS: tuple = (
    BuiltinMetrics.LIVE_TOTAL_ASSETS,
    BuiltinMetrics.LIVE_AVAILABLE_CASH,
    BuiltinMetrics.LIVE_POSITION_MARKET_VALUE,
    BuiltinMetrics.LIVE_REALIZED_PNL,
    BuiltinMetrics.LIVE_UNREALIZED_PNL,
)

#: 指标名 → portfolio.get_account() 字段映射（防脏门通过后读取一致状态）
_PNL_FIELD_MAP: Dict[str, str] = {
    BuiltinMetrics.LIVE_TOTAL_ASSETS: "total_assets",
    BuiltinMetrics.LIVE_AVAILABLE_CASH: "available_cash",
    BuiltinMetrics.LIVE_POSITION_MARKET_VALUE: "market_value",
    BuiltinMetrics.LIVE_REALIZED_PNL: "realized_pnl",
    BuiltinMetrics.LIVE_UNREALIZED_PNL: "unrealized_pnl",
}


def _today_start_ts(now_ts: Optional[float] = None) -> float:
    """本地时区当日零点 epoch 秒（防未来：仅作为"当日曲线"左闭起点）。"""
    now_t = time.time() if now_ts is None else float(now_ts)
    lt = time.localtime(now_t)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _default_history_store() -> Any:
    """惰性构建时序库实例（默认项目库；测试注入临时库）。"""
    from myquant.core.metrics_history import MetricsHistoryStore
    return MetricsHistoryStore()


def _save_pnl_rows(store: Any, rows: List[Dict[str, Any]]) -> int:
    """
    落库 PnL 批次；敏感名命中但未绑 FieldCrypto 时自动绑定后重试一次（BASE-9）。

    资金/持仓/PnL 命中敏感名（asset/position/pnl）必须加密落盘；默认时序库未绑
    加密器时拒绝明文落盘，此处按 BASE-9"首次自动生成密钥"语义补绑后重试。

    Returns:
        int : 实际写入行数。

    Raises:
        Exception: 非"拒绝明文落盘"类错误向上抛出（调用方记日志不阻断 POLL）。
    """
    try:
        return store.save_batch(rows)
    except ValueError as e:
        if "拒绝明文落盘" in str(e) and getattr(store, "_field_crypto", None) is None:
            from myquant.core.field_crypto import FieldCrypto
            store.attach_field_crypto(FieldCrypto())
            logger.warning(
                "[metrics] PnL 敏感指标落库：自动绑定 FieldCrypto（BASE-9，首次自动生成密钥）"
            )
            return store.save_batch(rows)
        raise


def collect_pnl_snapshot(
    portfolio: Any,
    reason: str = "periodic",
    reconciler: Optional[Any] = None,
    store: Optional[Any] = None,
    collector: Optional[Any] = None,
    timestamp: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    采集一次 PnL/资金快照并落时序库（B-2 POLL 入口；防脏门）。

    防脏数据门：先经 LivePortfolio.record_pnl_snapshot——账户对账冻结（差异未
    人工解除）时返回 None 且不写时序库（portfolio 侧已经 reconciler 审计留痕
    action="pnl_block"）；一致（未冻结）后才把总资产/可用/持仓市值/已实现/未实现
    PnL 落 MetricsHistoryStore，并联动 MetricsCollector 的 live_* GAUGE。

    【防未来】timestamp 默认取当前采集时刻；只读当前已发生账户状态，不预测。

    Parameters
    ----------
    portfolio : LivePortfolio
        实盘账户（须具备 record_pnl_snapshot/get_account 接口）
    reason : str
        记录触发原因（periodic/settle/manual，仅留痕）
    reconciler : AccountReconciler | None
        对账器（None 由 portfolio 内部解析注入/全局单例）
    store : MetricsHistoryStore | None
        时序库（None 惰性默认库）
    collector : MetricsCollector | None
        指标采集器（None 不联动 /api/metrics；传入则 set_gauge 5 个 live_* 指标）
    timestamp : float | None
        采集时刻（epoch 秒；None=当前时刻）

    Returns
    -------
    dict | None :
        冻结被拒 → {"written": False, "blocked": True, ...}；
        成功 → {"written": True, "rows": n, "account_id", "values": {...}, "ts"}；
        portfolio 缺失等非法入参 → None。
    """
    if portfolio is None:
        logger.error("[metrics] collect_pnl_snapshot 拒绝：portfolio 为 None")
        return None

    ts = time.time() if timestamp is None else float(timestamp)

    # ── 防脏门：冻结时 portfolio.record_pnl_snapshot 返回 None 并已留痕 ──
    try:
        gate = portfolio.record_pnl_snapshot(reason)
    except Exception as e:  # 采集异常不阻断 POLL 主流程
        logger.error("[metrics] PnL 防脏门调用异常（不写库）: %s", e)
        return {"written": False, "blocked": True, "reason": reason, "ts": ts,
                "error": "gate_exception"}
    if gate is None:
        logger.warning(
            "[metrics] PnL 采集被防脏门拒绝（对账冻结），不写时序库: reason=%s", reason,
        )
        return {"written": False, "blocked": True, "reason": reason, "ts": ts}

    # ── 门通过：读取一致状态快照 ──
    try:
        acct = portfolio.get_account()
    except Exception as e:
        logger.error("[metrics] PnL 账户快照读取失败: %s", e)
        return {"written": False, "blocked": False, "reason": reason, "ts": ts,
                "error": "read_account"}

    account_id = str(acct.get("account_id", "default"))
    values: Dict[str, float] = {}
    rows: List[Dict[str, Any]] = []
    for metric_name, field in _PNL_FIELD_MAP.items():
        val = acct.get(field, 0.0)
        try:
            val_f = float(val)
        except (TypeError, ValueError):
            logger.error("[metrics] PnL 字段 %s 非法值 %r，记 0.0", field, val)
            val_f = 0.0
        values[metric_name] = val_f
        rows.append({
            "name": metric_name,
            "value": val_f,
            "labels": {"account_id": account_id},
            "timestamp": ts,
        })

    # ── 落时序库（敏感名自动加密；失败不阻断 POLL）──
    history = store if store is not None else _default_history_store()
    written = 0
    write_error: Optional[str] = None
    try:
        written = _save_pnl_rows(history, rows)
    except Exception as e:  # noqa: BLE001
        write_error = str(e)
        logger.error("[metrics] PnL 落时序库失败（不阻断 POLL）: %s", e)

    # ── 联动实时采集器（/api/metrics 当前值）──
    if collector is not None and write_error is None:
        try:
            for metric_name, val_f in values.items():
                collector.set_gauge(metric_name, val_f, labels={"account_id": account_id})
        except Exception as e:  # noqa: BLE001
            logger.error("[metrics] PnL 联动采集器失败: %s", e)

    return {
        "written": write_error is None,
        "blocked": False,
        "rows": written,
        "account_id": account_id,
        "values": values,
        "total_pnl": values[BuiltinMetrics.LIVE_REALIZED_PNL]
                     + values[BuiltinMetrics.LIVE_UNREALIZED_PNL],
        "reason": reason,
        "ts": ts,
        "error": write_error,
    }


def get_latest_pnl_snapshot(
    store: Optional[Any] = None,
    reconciler: Optional[Any] = None,
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    读取最新 PnL 快照（时序库各指标最新一点）+ 对账状态（B-2 / B-8 资金 PnL 视图）。

    【防未来】只查询当前已落库的历史点，不预测；无数据时各项为 None（不伪造）。

    Parameters
    ----------
    store : MetricsHistoryStore | None
        时序库（None 惰性默认库）
    reconciler : AccountReconciler | None
        对账器（None 则对账状态回退 unknown，不伪造）
    account_id : str | None
        账户标签过滤；None 不限标签

    Returns
    -------
    dict : {"pnl": {total_assets/available_cash/position_market_value/
            realized_pnl/unrealized_pnl/total_pnl}, "ts", "account_id",
            "reconciliation": {...}|None}
    """
    history = store if store is not None else _default_history_store()
    labels = {"account_id": account_id} if account_id else None
    pnl: Dict[str, Optional[float]] = {
        "total_assets": None,
        "available_cash": None,
        "position_market_value": None,
        "realized_pnl": None,
        "unrealized_pnl": None,
        "total_pnl": None,
    }
    latest_ts: float = 0.0
    for metric_name, field in _PNL_FIELD_MAP.items():
        try:
            rows = history.query(name=metric_name, labels=labels, order="desc", limit=1)
        except Exception as e:  # noqa: BLE001
            logger.error("[metrics] PnL 最新值查询失败（%s）: %s", metric_name, e)
            rows = []
        if rows:
            row = rows[0]
            pnl[field] = row["value"]
            latest_ts = max(latest_ts, float(row["timestamp"]))

    if pnl["realized_pnl"] is not None and pnl["unrealized_pnl"] is not None:
        pnl["total_pnl"] = pnl["realized_pnl"] + pnl["unrealized_pnl"]

    if reconciler is not None and hasattr(reconciler, "to_dict"):
        try:
            reconciliation: Optional[Dict[str, Any]] = reconciler.to_dict()
        except Exception as e:  # noqa: BLE001
            logger.error("[metrics] 对账状态读取失败: %s", e)
            reconciliation = None
    else:
        reconciliation = None

    return {
        "pnl": pnl,
        "ts": latest_ts if latest_ts > 0 else None,
        "account_id": account_id,
        "reconciliation": reconciliation,
    }


def replay_intraday_equity_curve(
    store: Optional[Any] = None,
    account_id: Optional[str] = None,
    metric_name: str = BuiltinMetrics.LIVE_TOTAL_ASSETS,
    now_ts: Optional[float] = None,
    max_points: int = 1440,
) -> List[Dict[str, Any]]:
    """
    回放当日权益曲线（B-2：基于 metrics_history 当日序列，升序）。

    【防未来】左闭起点取本地当日零点，右闭取 now_ts（当前时刻）；只回放
    当日已落库点。敏感指标（total_assets）经 query() 解密回读真实值；
    当日为高精度原始点，不降采样（query_downsampled 拒绝敏感名聚合）。

    Parameters
    ----------
    store : MetricsHistoryStore | None
        时序库（None 惰性默认库）
    account_id : str | None
        账户标签过滤；None 不限
    metric_name : str
        权益序列指标名（默认 live_total_assets）
    now_ts : float | None
        当前时刻（epoch 秒；None=当前时刻）
    max_points : int
        最大返回点数（>0）

    Returns
    -------
    list[dict] : [{"ts": epoch秒, "equity": 总资产}] 升序；无数据返回 []
    """
    if max_points <= 0:
        raise ValueError(f"max_points 必须为正整数，收到 {max_points}")
    history = store if store is not None else _default_history_store()
    end_ts = time.time() if now_ts is None else float(now_ts)
    start_ts = _today_start_ts(end_ts)
    labels = {"account_id": account_id} if account_id else None
    try:
        rows = history.query(
            name=metric_name, labels=labels,
            start_ts=start_ts, end_ts=end_ts,
            order="asc", limit=max_points,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[metrics] 当日权益曲线回放失败: %s", e)
        return []
    return [{"ts": float(r["timestamp"]), "equity": float(r["value"])} for r in rows]
