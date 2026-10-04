# 文件名：tests/unit/test_api_not_implemented.py
"""
test_api_not_implemented.py - 占位接口伪造成功测试（KP-P1-01 / T-20）
========================================================================
覆盖"占位接口伪造成功（order/live start-stop/risk pause-resume）"修复：
  1. POST /api/order → HTTP 501（非 200）。
  2. POST /api/order/{id}/cancel → HTTP 501。
  3. POST /api/live/start → HTTP 501。
  4. POST /api/live/stop → HTTP 501。
  5. POST /api/risk/pause → HTTP 501。
  6. POST /api/risk/resume → HTTP 501。
  7. cli.py --host 默认收紧为 127.0.0.1。

注：环境无 httpx，测试直接调用 FastAPI route endpoint 断言返回 501 JSONResponse。

依赖: pytest, myquant.api.routes.create_app, myquant.api.cli
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from myquant.api.cli import parse_api_args
from myquant.api.models.schemas import OrderRequest
from myquant.api.routes import create_app


def _find_endpoint(app, path: str, method: str) -> Callable[..., Any]:
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
            return route.endpoint
    raise AssertionError(f"未找到端点 {method} {path}")


def _run(ep: Callable[..., Any], *args: Any, **kwargs: Any):
    return asyncio.run(ep(*args, **kwargs))


@pytest.fixture(scope="module")
def app():
    return create_app(enable_auth=False, enable_rate_limit=False)


class TestPlaceholderEndpoints501:
    def test_order_501(self, app) -> None:
        """DoD：未接线下单端点返回 HTTP 501（不伪造成功）。"""
        ep = _find_endpoint(app, "/api/order", "POST")
        req = OrderRequest(symbol="000001.SZ", side="BUY", quantity=100, price=10.0)
        resp = _run(ep, req)
        assert resp.status_code == 501

    def test_order_cancel_501(self, app) -> None:
        """DoD：未接线撤单端点返回 HTTP 501。"""
        ep = _find_endpoint(app, "/api/order/{order_id}/cancel", "POST")
        resp = _run(ep, "ord_1")
        assert resp.status_code == 501

    def test_live_start_501(self, app) -> None:
        """DoD：未接线实盘启动端点返回 HTTP 501。"""
        ep = _find_endpoint(app, "/api/live/start", "POST")
        resp = _run(ep)
        assert resp.status_code == 501

    def test_live_stop_501(self, app) -> None:
        """DoD：未接线实盘停止端点返回 HTTP 501。"""
        ep = _find_endpoint(app, "/api/live/stop", "POST")
        resp = _run(ep)
        assert resp.status_code == 501

    def test_risk_pause_501(self, app) -> None:
        """DoD：未接线风控暂停端点返回 HTTP 501。"""
        ep = _find_endpoint(app, "/api/risk/pause", "POST")
        resp = _run(ep)
        assert resp.status_code == 501

    def test_risk_resume_501(self, app) -> None:
        """DoD：未接线风控恢复端点返回 HTTP 501。"""
        ep = _find_endpoint(app, "/api/risk/resume", "POST")
        resp = _run(ep)
        assert resp.status_code == 501


class TestCliHostDefault:
    def test_host_default_loopback(self) -> None:
        """DoD：cli --host 默认 127.0.0.1（收紧回环）。"""
        args = parse_api_args([])
        assert args.host == "127.0.0.1"
