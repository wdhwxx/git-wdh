# -*- coding: utf-8 -*-
"""
API 认证/限流中间件接线回归测试（审计 C1）
============================================
覆盖:
  1. create_app 默认开启认证/限流（安全默认）
  2. enable_auth/enable_rate_limit 参数真实挂载中间件
  3. 显式关闭时不挂载（开发降级路径）
  4. CORS 通配符与 credentials 互斥修正
  5. APIKeyAuth / FixedWindowLimiter 逻辑单元（401/429 判定基础）
  6. scripts/运行API.py 参数解析

回归依据: 审计问题 C1（api/routes.py 认证/限流参数悬空、CORS 无效组合）
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.api.middleware.auth import APIKeyAuth  # noqa: E402
from myquant.api.middleware.rate_limit import (  # noqa: E402
    FixedWindowLimiter,
)
from myquant.api.routes import (  # noqa: E402
    _AuthMiddleware,
    _RateLimitMiddleware,
    create_app,
)


def _middleware_classes(app) -> list:
    """提取已挂载中间件类列表"""
    return [m.cls for m in app.user_middleware]


def _middleware_kwargs(app, cls) -> dict:
    for m in app.user_middleware:
        if m.cls is cls:
            return m.kwargs
    return {}


class TestAuthWiring:
    """C1: 认证中间件接线"""

    def test_默认开启认证(self) -> None:
        app = create_app(enable_auth=True, enable_rate_limit=False)
        assert _AuthMiddleware in _middleware_classes(app)

    def test_默认参数安全开启(self) -> None:
        """create_app() 不带参数 → 认证/限流均开启（安全默认）"""
        app = create_app()
        classes = _middleware_classes(app)
        assert _AuthMiddleware in classes
        assert _RateLimitMiddleware in classes

    def test_显式关闭不挂载(self) -> None:
        app = create_app(enable_auth=False, enable_rate_limit=False)
        assert _AuthMiddleware not in _middleware_classes(app)
        assert _RateLimitMiddleware not in _middleware_classes(app)

    def test_豁免路径包含健康检查(self) -> None:
        """健康检查免认证（监控探活可用）；KP-P4-04: 文档端点不再豁免"""
        app = create_app(enable_auth=True, enable_rate_limit=False)
        kwargs = _middleware_kwargs(app, _AuthMiddleware)
        assert "/api/health" in kwargs["exempt_paths"]
        assert "/docs" not in kwargs["exempt_paths"]

    def test_auth_verify_request_逻辑(self) -> None:
        """APIKeyAuth 逻辑单元：header/query 取 key，无效拒绝"""
        auth = APIKeyAuth(api_keys={"secret-key-1"}, enabled=True)
        assert auth.verify_request({"X-API-Key": "secret-key-1"}, {}) is True
        # H6: query 传 key 默认禁用（防浏览器历史/代理日志泄露）
        assert auth.verify_request({}, {"api_key": "secret-key-1"}) is False
        assert auth.verify_request({"X-API-Key": "wrong"}, {}) is False
        assert auth.verify_request({}, {}) is False
        # H6: 蓝图双通道能力保留，显式开启 allow_query_param 可用
        auth_query = APIKeyAuth(
            api_keys={"secret-key-1"}, enabled=True, allow_query_param=True
        )
        assert auth_query.verify_request({}, {"api_key": "secret-key-1"}) is True

    def test_auth_禁用放行(self) -> None:
        auth = APIKeyAuth(api_keys={"k"}, enabled=False)
        assert auth.verify_request({}, {}) is True


class TestRateLimitWiring:
    """C1: 限流中间件接线"""

    def test_默认挂载限流(self) -> None:
        app = create_app(enable_auth=False, enable_rate_limit=True)
        assert _RateLimitMiddleware in _middleware_classes(app)

    def test_限流阈值可经环境变量配置(self) -> None:
        os.environ["QUANT_RATE_LIMIT_MAX"] = "7"
        os.environ["QUANT_RATE_LIMIT_WINDOW"] = "30"
        try:
            app = create_app(enable_auth=False, enable_rate_limit=True)
            kwargs = _middleware_kwargs(app, _RateLimitMiddleware)
            assert kwargs["limiter"].max_requests == 7
            assert kwargs["limiter"].window_seconds == 30
        finally:
            os.environ.pop("QUANT_RATE_LIMIT_MAX", None)
            os.environ.pop("QUANT_RATE_LIMIT_WINDOW", None)

    def test_限流器边界(self) -> None:
        """FixedWindowLimiter: 第 max+1 次请求被拒（429 判定基础）"""
        limiter = FixedWindowLimiter(max_requests=3, window_seconds=60)
        assert limiter.allow("127.0.0.1").allowed is True
        assert limiter.allow("127.0.0.1").allowed is True
        assert limiter.allow("127.0.0.1").allowed is True
        r = limiter.allow("127.0.0.1")
        assert r.allowed is False
        assert r.retry_after > 0
        # 不同 IP 独立计数
        assert limiter.allow("192.168.1.1").allowed is True


class TestCorsFix:
    """C1: CORS 通配符与 credentials 互斥修正"""

    def test_默认通配不携带凭据(self) -> None:
        app = create_app(enable_auth=False, enable_rate_limit=False)
        kwargs = _middleware_kwargs(app, __import__("fastapi.middleware.cors", fromlist=["CORSMiddleware"]).CORSMiddleware)
        assert kwargs["allow_origins"] == ["*"]
        assert kwargs["allow_credentials"] is False  # 修复前为 True（无效组合）

    def test_白名单时允许凭据(self) -> None:
        os.environ["QUANT_CORS_ORIGINS"] = "https://example.com"
        try:
            app = create_app(enable_auth=False, enable_rate_limit=False)
            from fastapi.middleware.cors import CORSMiddleware
            kwargs = _middleware_kwargs(app, CORSMiddleware)
            assert kwargs["allow_origins"] == ["https://example.com"]
            assert kwargs["allow_credentials"] is True
        finally:
            os.environ.pop("QUANT_CORS_ORIGINS", None)


class TestRunApiArgs:
    """C1: API 入口参数解析（P3-7：逻辑已下沉 myquant/api/cli.py，不再反向导入 scripts）"""

    def test_disable_参数(self) -> None:
        from myquant.api.cli import parse_api_args
        args = parse_api_args(["--disable-auth", "--disable-rate-limit"])
        assert args.disable_auth is True
        assert args.disable_rate_limit is True

    def test_默认开启(self) -> None:
        from myquant.api.cli import parse_api_args
        args = parse_api_args([])
        assert args.disable_auth is False
        assert args.disable_rate_limit is False
