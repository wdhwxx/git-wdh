# 文件名：myquant/risk/__init__.py
"""
risk - 风控模块（阶段9任务12增强：数据质量事件联动）
======================================================
风控规则库、风控管理器、风控守护进程、数据质量风控联动。

v1实现:
  - rules.py: 10类核心风控规则 + 规则库统一管理
  - risk_manager.py: 风控核心管理器（订单前置检查/持仓监控/状态管理）
  - guardian.py: 风控守护进程（健康监控、强制平仓）

阶段9任务12增强:
  - risk_manager.py 对接 DataQualityEvent:
    - DataQualityRiskConfig: 数据质量风控阈值配置
    - SymbolDataQualityStatus: 按标的跟踪数据质量状态（单标的暂停）
    - subscribe_to_event_bus(): 订阅CRITICAL/ALERT/PIPELINE_COMPLETE事件
    - handle_quality_alert_event(): critical全局暂停，error+高占比单标的暂停
    - handle_pipeline_complete_event(): 按质量等级分级处理
    - check_order() 增强: 单标的数据质量暂停时拒绝买入
    - resume() 增强: 恢复时清除数据质量暂停标记
    - 任务12-B: 暂停/恢复时发布 StrategyPauseEvent 到事件总线（供实盘引擎/API消费）
    - 任务12-C: AlertManager 订阅暂停事件自动告警（全局暂停→CRITICAL）
    - 任务12-D: MetricsCollector 订阅暂停事件更新指标（risk_pause_total等）
    - 任务12-E: 数据质量恢复后自动恢复策略（可配置恢复阈值：
      auto_resume_enabled / resume_quality_grades / resume_bad_ratio_threshold /
      consecutive_good_required，恢复时联动发布事件+告警+指标）

TODO(后续迭代):
  - 动态风控阈值（根据波动率/持仓集中度自动调整）
  - 多账户风控隔离
  - 风控规则热加载（运行时修改配置）
  - 风控事件审计日志持久化（写入文件/数据库）
"""

from myquant.risk.rules import (
    RiskCheckResult,
    RiskContext,
    RiskRule,
    PositionLimitRule,
    SingleOrderLimitRule,
    MaxDrawdownRule,
    StopLossRule,
    TakeProfitRule,
    RateLimitRule,
    PriceLimitRule,
    TradingHoursRule,
    BlacklistRule,
    CashAvailabilityRule,
    DailyLossLimitRule,
    RiskRuleLibrary,
    RULE_REGISTRY,
    create_default_library,
)
from myquant.risk.risk_manager import (
    RiskManager,
    RiskStatus,
    RiskEvent,
    RiskEventType,
    DataQualityRiskConfig,
    SymbolDataQualityStatus,
)
from myquant.risk.guardian import (
    RiskGuardian,
    HealthStatus,
    AlertLevel,
    DecisionType,
    GuardianDecision,
    HealthCheckResult,
)

__all__ = [
    "RiskCheckResult",
    "RiskContext",
    "RiskRule",
    "PositionLimitRule",
    "SingleOrderLimitRule",
    "MaxDrawdownRule",
    "StopLossRule",
    "TakeProfitRule",
    "RateLimitRule",
    "PriceLimitRule",
    "TradingHoursRule",
    "BlacklistRule",
    "CashAvailabilityRule",
    "DailyLossLimitRule",
    "RiskRuleLibrary",
    "RULE_REGISTRY",
    "create_default_library",
    "RiskManager",
    "RiskStatus",
    "RiskEvent",
    "RiskEventType",
    "DataQualityRiskConfig",
    "SymbolDataQualityStatus",
    "RiskGuardian",
    "HealthStatus",
    "AlertLevel",
    "DecisionType",
    "GuardianDecision",
    "HealthCheckResult",
]
