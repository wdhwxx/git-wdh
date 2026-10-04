# 文件名：tests/unit/test_backtest_not_placeholder.py
"""
test_backtest_not_placeholder.py - 回测API硬编码绩效常量测试（KP-P1-02 / T-21）
==============================================================================
覆盖"回测API硬编码绩效常量"修复：
  1. 未注册策略 → HTTP 501（不伪造 metrics）。
  2. 缺少 symbol/start_date/end_date → HTTP 422。
  3. 真实引擎成功（monkeypatch run_backtest）→ HTTP 200 + 指标来自真实结果（非硬编码常量）。
  4. 引擎失败（is_success=False）→ HTTP 501。
  5. 引擎抛异常 → HTTP 501。

依赖: pytest, myquant.api.routes.create_app, myquant.api.models.schemas
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from myquant.api.models.schemas import BacktestRequest
from myquant.api.routes import create_app


@pytest.fixture(scope="module")
def app():
    return create_app(enable_auth=False, enable_rate_limit=False)


def _find_endpoint(app, path: str, method: str) -> Callable[..., Any]:
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
            return route.endpoint
    raise AssertionError(f"未找到端点 {method} {path}")


def _run(ep: Callable[..., Any], req: BacktestRequest):
    return asyncio.run(ep(req))


def _req(**over):
    base = dict(
        strategy_name="ma_cross",
        symbol="000001.SZ",
        start_date="2024-01-01",
        end_date="2024-06-30",
        initial_capital=1_000_000.0,
        params={},
    )
    base.update(over)
    return BacktestRequest(**base)


def _fake_success() -> SimpleNamespace:
    m = SimpleNamespace(
        total_return=0.123,
        annualized_return=0.456,
        sharpe_ratio=2.0,
        max_drawdown=0.05,
        win_rate=0.6,
        total_trades=10,
        calmar_ratio=2.5,
        sortino_ratio=3.0,
    )
    return SimpleNamespace(
        is_success=True,
        status="completed",
        metrics=m,
        equity_curve=[{"timestamp": 1, "equity": 100000.0}],
        trades=[],
    )


class TestBacktestEndpoint:
    def test_未注册策略_501(self, app) -> None:
        """DoD：未注册策略返回 HTTP 501（不伪造 metrics）。"""
        ep = _find_endpoint(app, "/api/backtest/run", "POST")
        resp = _run(ep, _req(strategy_name="no_such"))
        assert resp.status_code == 501

    def test_缺参数_422(self, app) -> None:
        """DoD：缺 symbol/start_date/end_date 返回 HTTP 422。"""
        ep = _find_endpoint(app, "/api/backtest/run", "POST")
        resp = _run(ep, _req(symbol=""))
        assert resp.status_code == 422

    def test_真实引擎成功_指标非硬编码(self, app, monkeypatch) -> None:
        """DoD：真实引擎成功 → 200 且指标来自结果（非占位常量 0.0523）。"""
        fake = _fake_success()
        monkeypatch.setattr("myquant.backtest.engine.run_backtest", lambda *a, **k: fake)
        ep = _find_endpoint(app, "/api/backtest/run", "POST")
        resp = _run(ep, _req())
        # 成功路径返回 ApiResponse（HTTP 200）；校验指标来自 fake 真实值（非占位常量）
        assert getattr(resp, "status_code", 200) == 200
        metrics = resp.data["metrics"]
        assert metrics["total_return"] == 0.123
        assert metrics["total_trades"] == 10

    def test_引擎失败_501(self, app, monkeypatch) -> None:
        """DoD：引擎 is_success=False → HTTP 501。"""
        fake = SimpleNamespace(is_success=False, status="violation", metrics=None)
        monkeypatch.setattr("myquant.backtest.engine.run_backtest", lambda *a, **k: fake)
        ep = _find_endpoint(app, "/api/backtest/run", "POST")
        resp = _run(ep, _req())
        assert resp.status_code == 501

    def test_引擎异常_501(self, app, monkeypatch) -> None:
        """DoD：引擎抛异常 → HTTP 501（不伪造成功）。"""
        def _boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr("myquant.backtest.engine.run_backtest", _boom)
        ep = _find_endpoint(app, "/api/backtest/run", "POST")
        resp = _run(ep, _req())
        assert resp.status_code == 501
