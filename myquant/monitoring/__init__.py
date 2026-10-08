# 文件名：myquant/monitoring/__init__.py
"""
monitoring - 监控告警模块
=========================
提供量化系统运行监控、告警通知和指标采集能力：
  - health_check       : 系统健康检查（磁盘/进程/事件总线/数据源连通性）
  - alerting           : 告警通道管理（控制台/日志/邮件/Webhook，分级去重）
  - metrics_collector  : 运行指标采集（Prometheus格式导出，数据质量/系统/实盘指标）

阶段9任务9：模块骨架完成
  - health_check.py    : HealthChecker + HealthStatus + 内置检查项
  - alerting.py        : AlertManager + AlertChannel(Console/Log/Email/Webhook预留)
  - metrics_collector.py : MetricsCollector + MetricType + Prometheus导出

阶段9任务10：alerting.py 增强（DataQualityEvent自动告警）
  - AlertRule          : 告警规则配置（脏数据类型→级别/占比阈值→级别/级别→通道/聚合窗口/升级时间）
  - AlertAggregator    : 告警聚合器（同一标的+脏数据类型在窗口内聚合，流式支持）
  - EmailAlertChannel  : 实际SMTP发送（HTML/纯文本模板，TLS/SSL）
  - WebhookAlertChannel: 实际HTTP发送（飞书/钉钉/企业微信/通用，重试机制）
  - AlertManager增强   : 智能分级+聚合+自动告警+告警升级+发送回调+通道路由

为后续任务预留:
  - 任务11: metrics_collector.py 采集脏数据占比/清洗质量指标（collect_data_quality已预留）
  - 任务12: risk/risk_manager.py 对接DataQualityEvent，脏数据严重时自动暂停策略
  - 任务16: api/routes.py 新增 /metrics 端点（Prometheus抓取）
  - 任务22: live/scheduler.py 定期巡检调用 HealthChecker + check_escalations()
"""

from myquant.core.health_check import (  # G2-4 下沉：health_check 迁 core/（L0），monitoring 仅 re-export 兼容
    HealthStatus,
    HealthCheckResult,
    HealthChecker,
    check_disk_space,
    check_event_bus_status,
    check_process_uptime,
    get_health_checker,
    reset_health_checker,
)
from myquant.monitoring.alerting import (
    Alert,
    AlertRule,
    AlertAggregator,
    AlertChannel,
    ConsoleAlertChannel,
    LogAlertChannel,
    EmailAlertChannel,
    WebhookAlertChannel,
    AlertManager,
    get_alert_manager,
    reset_alert_manager,
)
from myquant.monitoring.metrics_collector import (
    MetricType,
    Metric,
    BuiltinMetrics,
    MetricsCollector,
    get_metrics_collector,
    reset_metrics_collector,
)

__all__ = [
    # health_check
    "HealthStatus",
    "HealthCheckResult",
    "HealthChecker",
    "check_disk_space",
    "check_event_bus_status",
    "check_process_uptime",
    "get_health_checker",
    "reset_health_checker",
    # alerting
    "Alert",
    "AlertRule",
    "AlertAggregator",
    "AlertChannel",
    "ConsoleAlertChannel",
    "LogAlertChannel",
    "EmailAlertChannel",
    "WebhookAlertChannel",
    "AlertManager",
    "get_alert_manager",
    "reset_alert_manager",
    # metrics_collector
    "MetricType",
    "Metric",
    "BuiltinMetrics",
    "MetricsCollector",
    "get_metrics_collector",
    "reset_metrics_collector",
]
