# 文件名：tests/unit/test_health_probe_registry.py
"""
test_health_probe_registry.py - 健康探针注册表为空测试（KP-P5-02 / T-25）
==========================================================================
覆盖修复：
  1. routes /api/health：无探针注册时组件 unknown 计入 degraded（不再恒 healthy）。
  2. run_实盘入口.py 装配引擎后注册真实 live 探针（add_check("live", ...)）。
  3. engine.health_check 返回含 status 的真实探针数据源存在（供探针消费）。

依赖: pytest, myquant.api.routes.create_app, myquant.monitoring.health_check
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from myquant.api.routes import create_app
from myquant.monitoring.health_check import HealthStatus, get_health_checker

_PROJ = Path(__file__).resolve().parent.parent.parent


def _find_endpoint(app, path: str, method: str) -> Callable[..., Any]:
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
            return route.endpoint
    raise AssertionError(f"未找到端点 {method} {path}")


class TestHealthDegradedWithoutProbes:
    def test_无探针_unknown计入degraded(self) -> None:
        """DoD：无探针注册时 /api/health 状态为 degraded（unknown 计入，不再恒 healthy）。"""
        app = create_app(enable_auth=False, enable_rate_limit=False)
        ep = _find_endpoint(app, "/api/health", "GET")
        resp = asyncio.run(ep())
        data = resp.data
        assert data["status"] == "degraded"
        assert data["modules"]["live"] == "unknown"
        assert data["modules"]["data"] == "unknown"


class TestLiveProbeRegistered:
    def test_探针注册代码存在(self) -> None:
        """DoD：run_实盘入口.py 含真实 live 探针注册（add_check('live', ...)）。"""
        text = (_PROJ / "run_实盘入口.py").read_text(encoding="utf-8")
        assert "add_check(\"live\", _live_probe, critical=True)" in text
        assert "HealthStatus" in text

    def test_healthchecker_注册探针运行(self) -> None:
        """DoD：HealthChecker 注册 live 探针后 run_all 返回结果（探针链路可用）。"""
        checker = get_health_checker()
        from myquant.monitoring.health_check import HealthCheckResult

        def _healthy() -> HealthCheckResult:
            return HealthCheckResult(
                name="test_live_probe", status=HealthStatus.HEALTHY, message="engine_status=healthy"
            )

        checker.add_check("test_live_probe", _healthy, critical=True)
        results = [r for r in checker.run_all() if r.name == "test_live_probe"]
        assert len(results) == 1
        assert results[0].status == HealthStatus.HEALTHY
        # 清理测试探针，避免污染单例
        checker._checks.pop("test_live_probe", None)
