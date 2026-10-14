# 文件名：myquant/api/middleware/__init__.py
"""
middleware - API中间件
=======================
REST API中间件，包括认证、限流等。

v1实现:
  - auth.py: API Key认证
  - rate_limit.py: 限流（固定窗口/令牌桶）

TODO(后续迭代):
  - JWT/OAuth2认证
  - 角色权限管理
  - 分布式限流（Redis）
  - 请求日志/审计
"""

from myquant.api.middleware.auth import APIKeyAuth, create_auth
from myquant.api.middleware.rate_limit import (
    FixedWindowLimiter,
    RateLimiter,
    RateLimitResult,
    TokenBucketLimiter,
    create_fixed_window_limiter,
    create_token_bucket_limiter,
)

__all__ = [
    "APIKeyAuth",
    "create_auth",
    "RateLimiter",
    "FixedWindowLimiter",
    "TokenBucketLimiter",
    "RateLimitResult",
    "create_fixed_window_limiter",
    "create_token_bucket_limiter",
]
