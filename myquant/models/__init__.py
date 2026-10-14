# 文件名：myquant/models/__init__.py
"""
models - 数据模型层
===================
类型安全的数据类，作为整个量化系统的数据契约。

模块:
  - trade       : Order / Position / Trade / 订单状态枚举
  - market_data : Tick / Bar / Snapshot / MarketEvent / 数据校验
  - backtest    : BacktestConfig / BacktestResult / PerformanceMetrics
"""

__version__ = "0.1.0"

from myquant.models.trade import (
    OrderSide,
    OrderType,
    OrderStatus,
    Order,
    Position,
    Trade,
    OrderFactory,
)
from myquant.models.market_data import (
    Tick,
    Bar,
    BarFrequency,
    Snapshot,
    MarketEvent,
    MarketDataValidator,
)
from myquant.models.backtest import (
    BacktestConfig,
    BacktestResult,
    PerformanceMetrics,
    TradeRecord,
    EquityPoint,
)

__all__ = [
    # trade
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "Order",
    "Position",
    "Trade",
    "OrderFactory",
    # market_data
    "Tick",
    "Bar",
    "BarFrequency",
    "Snapshot",
    "MarketEvent",
    "MarketDataValidator",
    # backtest
    "BacktestConfig",
    "BacktestResult",
    "PerformanceMetrics",
    "TradeRecord",
    "EquityPoint",
]