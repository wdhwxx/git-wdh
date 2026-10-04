# 文件名：tests/unit/test_api_docs_closed.py
"""
test_api_docs_closed.py - API文档匿名开放测试（KP-P4-04 / T-24）
==================================================================
覆盖修复：
  1. 生产（默认 enable_docs=False）：/docs /redoc /openapi.json 路由不存在。
  2. enable_docs=True：文档路由存在（开发模式可用）。
  3. 认证/限流豁免路径不含 /docs /redoc /openapi.json（文档不再匿名豁免）。

依赖: pytest, myquant.api.routes.create_app
"""

from __future__ import annotations

from myquant.api.routes import (
    _AuthMiddleware,
    _RateLimitMiddleware,
    create_app,
)


def _paths(app) -> set:
    return {getattr(r, "path", None) for r in app.routes}


def _mw_kwargs(app, cls) -> dict:
    for m in app.user_middleware:
        if m.cls is cls:
            return m.kwargs
    return {}


class TestDocsClosedByDefault:
    def test_默认关闭文档路由(self) -> None:
        """DoD：生产默认（enable_docs=False）无 /docs /redoc /openapi.json 路由。"""
        app = create_app(enable_auth=False, enable_rate_limit=False)
        paths = _paths(app)
        assert "/docs" not in paths
        assert "/redoc" not in paths
        assert "/openapi.json" not in paths

    def test_开启时文档路由存在(self) -> None:
        """DoD：enable_docs=True 时文档路由存在（开发模式）。"""
        app = create_app(enable_auth=False, enable_rate_limit=False, enable_docs=True)
        paths = _paths(app)
        assert "/docs" in paths
        assert "/openapi.json" in paths


class TestDocsNotExempt:
    def test_认证豁免不含文档(self) -> None:
        """DoD：认证豁免路径仅健康检查，不含 /docs /redoc /openapi.json。"""
        app = create_app(enable_auth=True, enable_rate_limit=False)
        exempt = set(_mw_kwargs(app, _AuthMiddleware).get("exempt_paths", []))
        assert exempt == {"/api/health"}
        assert "/docs" not in exempt
        assert "/openapi.json" not in exempt

    def test_限流豁免不含文档(self) -> None:
        """DoD：限流豁免路径不含文档端点。"""
        app = create_app(enable_auth=False, enable_rate_limit=True)
        exempt = set(_mw_kwargs(app, _RateLimitMiddleware).get("exempt_paths", []))
        assert "/docs" not in exempt
        assert "/openapi.json" not in exempt
