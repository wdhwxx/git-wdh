# 文件名：myquant/core/__init__.py
"""
core - 基础设施层
=================
提供量化系统全局基础能力：
  - settings       : 全局配置管理器
  - config_loader  : 通用配置加载器
  - constants      : 项目常量与枚举
  - exceptions     : 自定义异常体系
  - logger         : 统一日志
  - cache          : 计算结果缓存
  - async_runtime  : 异步运行时
  - event_bus      : 进程内事件总线
  - transports     : 外部消息中间件适配
"""

__version__ = "0.1.0"

from myquant.core.async_runtime import AsyncRuntime
from myquant.core.cache import MemoryCache, get_cache
from myquant.core.config_loader import ConfigLoader
from myquant.core.constants import (
    DataFrequency,
    MarketStatus,
    OrderSide,
    OrderStatus,
    OrderType,
)
from myquant.core.date_utils import (
    DAILY_PERIODS,
    INTRADAY_PERIODS,
    format_bar_date,
    get_date_part,
    get_period_seconds,
    get_period_start,
    get_time_part,
    is_daily_period,
    is_same_trading_day,
    normalize_to_datetime,
)
from myquant.core.event_bus import (
    DataQualityEvent,
    DataQualityEventPublisher,
    Event,
    EventBus,
    EventFilter,
    EventType,
    HandlerStats,
    PipelineCompleteEvent,
    QualityAlertEvent,
    StrategyPauseEvent,
    get_event_bus,
    reset_event_bus,
)
from myquant.core.exceptions import (
    CacheError,
    ConfigError,
    ConfigValidationError,
    DataNotFoundError,
    EventError,
    LookaheadError,
    QuantBaseError,
    RiskViolationError,
    StrategyError,
    TransportError,
)
from myquant.core.light_lookahead_guard import (
    LightLookaheadGuard,
    LightLookaheadViolation,
    get_light_lookahead_guard,
    is_future_data,
    is_lookahead_execution,
    reset_light_lookahead_guard,
)
from myquant.core.logger import get_logger, setup_logging
from myquant.core.lookahead_guard import LookaheadGuard, get_lookahead_guard
from myquant.core.settings import Settings, get_settings

__all__ = [
    "Settings",
    "get_settings",
    "ConfigLoader",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "MarketStatus",
    "DataFrequency",
    "QuantBaseError",
    "DataNotFoundError",
    "StrategyError",
    "RiskViolationError",
    "ConfigError",
    "ConfigValidationError",
    "CacheError",
    "EventError",
    "TransportError",
    "LookaheadError",
    "get_logger",
    "setup_logging",
    "MemoryCache",
    "get_cache",
    "AsyncRuntime",
    "EventBus",
    "Event",
    "EventType",
    "DataQualityEvent",
    "PipelineCompleteEvent",
    "QualityAlertEvent",
    "StrategyPauseEvent",
    "EventFilter",
    "HandlerStats",
    "DataQualityEventPublisher",
    "get_event_bus",
    "reset_event_bus",
    "LookaheadGuard",
    "get_lookahead_guard",
    "LightLookaheadGuard",
    "LightLookaheadViolation",
    "get_light_lookahead_guard",
    "reset_light_lookahead_guard",
    "is_future_data",
    "is_lookahead_execution",
    "normalize_to_datetime",
    "format_bar_date",
    "get_date_part",
    "get_time_part",
    "is_daily_period",
    "is_same_trading_day",
    "get_period_seconds",
    "get_period_start",
    "DAILY_PERIODS",
    "INTRADAY_PERIODS",
]