# 文件名：myquant/api/__init__.py
"""
api - REST API模块
===================
量化交易系统REST API，基于FastAPI。

v1实现:
  - routes.py: 路由聚合（健康/策略/回测/实盘/风控/数据/订单/资金分配）
  - middleware/: 认证（API Key）、限流（固定窗口/令牌桶）
  - models/: 请求/响应数据模型（Pydantic）
  - dependencies/: 服务依赖注入（服务容器单例）

TODO(后续迭代):
  - 数据质量查询接口
  - WebSocket实时行情推送
  - 策略参数动态配置API
  - 风控规则配置API
  - 用户管理API
  - 系统监控API

运行方式:
    pip install fastapi uvicorn
    python run_api.py
    # 或
    uvicorn myquant.api.routes:create_app --host 0.0.0.0 --port 8000
"""

from myquant.api.routes import create_app, run_api, FASTAPI_AVAILABLE
from myquant.api.models.schemas import (
    ApiResponse,
    ErrorResponse,
    HealthResponse,
    StrategyInfo,
    BacktestRequest,
    BacktestResponse,
    LiveStatusResponse,
    RiskStatusResponse,
    DataQualityResponse,
    OrderRequest,
    OrderResponse,
)
from myquant.api.middleware.auth import APIKeyAuth
from myquant.api.middleware.rate_limit import (
    FixedWindowLimiter,
    TokenBucketLimiter,
    RateLimitResult,
)
from myquant.api.dependencies.get_services import ServiceContainer

__all__ = [
    "create_app",
    "run_api",
    "FASTAPI_AVAILABLE",
    "ApiResponse",
    "ErrorResponse",
    "HealthResponse",
    "StrategyInfo",
    "BacktestRequest",
    "BacktestResponse",
    "LiveStatusResponse",
    "RiskStatusResponse",
    "DataQualityResponse",
    "OrderRequest",
    "OrderResponse",
    "APIKeyAuth",
    "FixedWindowLimiter",
    "TokenBucketLimiter",
    "RateLimitResult",
    "ServiceContainer",
]
