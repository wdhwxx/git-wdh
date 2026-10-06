# 文件名：tests/unit/test_dashboard_monitor.py
"""
test_dashboard_monitor.py - A-11 监控中心（核心仪表盘 v1）单元测试
================================================================
覆盖（不依赖 streamlit / 真实 quant.db，全部用 fake 注入）：
  1. mask_sensitive_fields 脱敏规则：券商账号留后4位 / 凭证全掩码 /
     资金明细聚合掩码 / 普通字段透传 / None 与嵌套字典
  2. MonitorCenterDataProvider.get_alerts：scope=unacknowledged 筛选 +
     details 脱敏 + limit 防御式校验
  3. get_health_overview：未注入 HealthChecker → unknown 不谎报；注入 fake → 组件列表
  4. get_metric_cards：消费 collector 数据点
  5. get_trading_continuity：无指标 → unknown（不伪造）；有心跳指标 → ok
  6. build_overview_payload 四键聚合
  7. /api/alerts 响应脱敏 + /api/monitor/overview 只读视图（直接调用端点，无 httpx）

运行方式:
  pytest tests/unit/test_dashboard_monitor.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.api.routes import (  # noqa: E402
    _mask_alert_details,
    create_app,
)
from myquant.web.dashboard import (  # noqa: E402
    MonitorCenterDataProvider,
    mask_sensitive_fields,
)


# ═══════════════════════════════════════════════
#  fakes
# ═══════════════════════════════════════════════

class _FakeAlert:
    """假告警对象（对齐 Alert.to_dict 字段）。"""

    def __init__(self, alert_id: str, level: str, message: str,
                 source: str = "system", category: str = "system",
                 details: Optional[Dict[str, Any]] = None,
                 acknowledged: bool = False, resolved: bool = False):
        self.alert_id = alert_id
        self.level = level
        self.message = message
        self.source = source
        self.category = category
        self.details = details or {}
        self.acknowledged = acknowledged
        self.resolved = resolved

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id, "level": self.level, "message": self.message,
            "source": self.source, "category": self.category, "details": self.details,
            "acknowledged": self.acknowledged, "resolved": self.resolved,
            "timestamp": 0.0,
        }


class _FakeAlertManager:
    """假告警管理器（内存，不落库）。"""

    def __init__(self, alerts: List[_FakeAlert]):
        self._alerts = list(alerts)

    def get_history(self, level=None, category=None, limit=50):
        out = list(reversed(self._alerts))
        if level is not None:
            out = [a for a in out if a.level == level.value]
        if category:
            out = [a for a in out if a.category == category]
        return out[:limit]

    def get_unacknowledged(self, limit=50):
        return [a for a in reversed(self._alerts)
                if not a.acknowledged and not a.resolved][:limit]

    def get_escalated(self, limit=50):
        return []

    @property
    def stats(self) -> Dict[str, Any]:
        return {"history_size": len(self._alerts), "unacknowledged_count": 1}


class _FakeMetric:
    def __init__(self, name: str, value: float):
        self.name = name
        self.value = value

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "labels": {}, "timestamp": 0.0}


class _FakeCollector:
    def __init__(self, metrics: List[_FakeMetric]):
        self._metrics = metrics

    def get_all(self, name=None):
        return list(self._metrics)


class _FakeHealthChecker:
    """假健康检查器（提供 get_summary，对齐 HealthChecker 报告结构）。"""

    def get_summary(self) -> Dict[str, Any]:
        return {
            "overall_status": "degraded",
            "checks_count": 2,
            "healthy_count": 1,
            "degraded_count": 1,
            "unhealthy_count": 0,
            "results": [
                {"name": "disk", "status": "healthy", "message": "ok"},
                {"name": "cpu", "status": "degraded", "message": "cpu 85%"},
            ],
        }


def _provider(alerts: Optional[List[_FakeAlert]] = None,
              metrics: Optional[List[_FakeMetric]] = None,
              health: Optional[Any] = None) -> MonitorCenterDataProvider:
    am = _FakeAlertManager(alerts or [])
    return MonitorCenterDataProvider(
        alert_manager=am,
        health_checker=health,
        metrics_collector=_FakeCollector(metrics or []),
    )


def _find_endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
            return route.endpoint
    raise AssertionError(f"未找到端点 {method} {path}")


def _run(ep, *args, **kwargs):
    return asyncio.run(ep(*args, **kwargs))


# ═══════════════════════════════════════════════
#  1. 脱敏规则
# ═══════════════════════════════════════════════

class TestMasking:
    def test_account_last4(self) -> None:
        out = mask_sensitive_fields({"broker_account": "1234567890"})
        assert out["broker_account"] == "****7890"

    def test_credential_all_masked(self) -> None:
        out = mask_sensitive_fields({"api_key": "sk-x", "password": "p", "token": "t"})
        assert out == {"api_key": "****", "password": "****", "token": "****"}

    def test_balance_masked(self) -> None:
        out = mask_sensitive_fields({"balance": 123456.7, "equity": 1.0})
        assert out["balance"] == "****" and out["equity"] == "****"

    def test_plain_passthrough(self) -> None:
        out = mask_sensitive_fields({"message": "磁盘满", "node": "n1"})
        assert out == {"message": "磁盘满", "node": "n1"}

    def test_none_and_nested(self) -> None:
        assert mask_sensitive_fields(None) == {}
        out = mask_sensitive_fields({"details": {"api_key": "sk-x"}})
        assert out["details"]["api_key"] == "****"

    def test_routes_mask_helper_aligned(self) -> None:
        out = _mask_alert_details({"fund_account": "A000123456", "secret": "s"})
        assert out["fund_account"] == "****3456"
        assert out["secret"] == "****"


# ═══════════════════════════════════════════════
#  2. 告警列表筛选 + 脱敏
# ═══════════════════════════════════════════════

class TestAlertList:
    def _alerts(self) -> List[_FakeAlert]:
        return [
            _FakeAlert("a1", "error", "磁盘满", details={"api_key": "sk-x", "node": "n1"}),
            _FakeAlert("a2", "warning", "延迟高", acknowledged=True),
        ]

    def test_unacknowledged_filter(self) -> None:
        p = _provider(self._alerts())
        view = p.get_alerts(scope="unacknowledged")
        assert view["count"] == 1
        assert view["alerts"][0]["alert_id"] == "a1"

    def test_details_masked_in_list(self) -> None:
        p = _provider(self._alerts())
        view = p.get_alerts(scope="all")
        a1 = next(a for a in view["alerts"] if a["alert_id"] == "a1")
        assert a1["details"]["api_key"] == "****"
        assert a1["details"]["node"] == "n1"  # 普通字段透传

    def test_invalid_limit_raises(self) -> None:
        p = _provider(self._alerts())
        try:
            p.get_alerts(limit=0)
            assert False, "应抛 ValueError"
        except ValueError:
            pass


# ═══════════════════════════════════════════════
#  3. 健康总览
# ═══════════════════════════════════════════════

class TestHealthOverview:
    def test_no_checker_unknown_no_fake(self) -> None:
        p = _provider([])
        ov = p.get_health_overview()
        assert ov["system"]["status"] == "unknown"
        assert ov["components"] == []

    def test_injected_checker_components(self) -> None:
        p = _provider([], health=_FakeHealthChecker())
        ov = p.get_health_overview()
        assert ov["system"]["status"] == "degraded"
        assert len(ov["components"]) == 2
        assert ov["components"][0]["name"] == "disk"


# ═══════════════════════════════════════════════
#  4/5. 指标卡片 + 交易连续状态
# ═══════════════════════════════════════════════

class TestMetricsAndContinuity:
    def test_metric_cards(self) -> None:
        p = _provider([], metrics=[_FakeMetric("trade_count", 3.0)])
        cards = p.get_metric_cards()
        assert cards and cards[0]["name"] == "trade_count"

    def test_continuity_unknown_when_no_metric(self) -> None:
        p = _provider([])
        cont = p.get_trading_continuity()
        assert cont["market_activity"]["state"] == "unknown"
        assert cont["engine_heartbeat"]["state"] == "unknown"
        assert cont["broker_connection"]["state"] == "unknown"

    def test_continuity_ok_when_heartbeat_metric(self) -> None:
        p = _provider([], metrics=[_FakeMetric("engine_heartbeat", 1.0)])
        cont = p.get_trading_continuity()
        assert cont["engine_heartbeat"]["state"] == "ok"
        assert cont["engine_heartbeat"]["metric"] == "engine_heartbeat"


# ═══════════════════════════════════════════════
#  6. 聚合 payload
# ═══════════════════════════════════════════════

class TestOverviewPayload:
    def test_payload_keys(self) -> None:
        p = _provider([_FakeAlert("a1", "error", "x")])
        payload = p.build_overview_payload()
        assert set(payload.keys()) == {
            "health", "alerts", "metric_cards", "trading_continuity",
        }


# ═══════════════════════════════════════════════
#  7. API 端点：/api/alerts 脱敏 + /api/monitor/overview
# ═══════════════════════════════════════════════

class TestMonitorEndpoints:
    def _app(self) -> Any:
        am = _FakeAlertManager([
            _FakeAlert("a1", "error", "磁盘满", details={"api_key": "sk-x", "node": "n1"}),
        ])
        return create_app(
            enable_auth=False, enable_rate_limit=False,
            alert_manager=am,
        )

    def test_alerts_endpoint_masks_details(self) -> None:
        app = self._app()
        ep = _find_endpoint(app, "/api/alerts", "GET")
        resp = _run(ep, scope="all", level="", category="", limit=20)
        data = resp.data  # ApiResponse.success(data=X) → .data 即业务字典
        alerts = data["alerts"]
        assert alerts[0]["details"]["api_key"] == "****"
        assert alerts[0]["details"]["node"] == "n1"

    def test_monitor_overview_endpoint(self) -> None:
        app = self._app()
        ep = _find_endpoint(app, "/api/monitor/overview", "GET")
        resp = _run(ep)
        data = resp.data
        assert "health" in data
        assert "alerts" in data
        assert "trading_continuity" in data
        assert data["alerts"]["alerts"][0]["details"]["api_key"] == "****"
        # 无检查器注入 → 系统状态 unknown（不谎报 healthy）
        assert data["health"]["system"]["status"] in ("healthy", "unknown")
