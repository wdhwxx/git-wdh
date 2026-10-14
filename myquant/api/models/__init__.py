# 文件名：myquant/api/models/__init__.py
"""
models - API数据模型
=====================
REST API请求/响应数据模型（Pydantic）。

v1实现:
  - schemas.py: 通用响应/健康/策略/回测/实盘/风控/数据质量模型

TODO(后续迭代):
  - 数据质量详细schema
  - 订单/持仓/行情详细模型
  - 分页响应模型
  - WebSocket消息模型
"""

from myquant.api.models.schemas import (
    ApiResponse,
    ErrorResponse,
    HealthResponse,
    StrategyInfo,
    StrategyListResponse,
    BacktestRequest,
    BacktestMetrics,
    BacktestResponse,
    LiveStatusResponse,
    RiskStatusResponse,
    DataQualityResponse,
    OrderRequest,
    OrderResponse,
    PYDANTIC_AVAILABLE,
)

__all__ = [
    "ApiResponse",
    "ErrorResponse",
    "HealthResponse",
    "StrategyInfo",
    "StrategyListResponse",
    "BacktestRequest",
    "BacktestMetrics",
    "BacktestResponse",
    "LiveStatusResponse",
    "RiskStatusResponse",
    "DataQualityResponse",
    "OrderRequest",
    "OrderResponse",
    "PYDANTIC_AVAILABLE",
]
