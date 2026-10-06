# 文件名：tests/unit/test_business_cabin.py
"""
test_business_cabin.py - B-8 业务驾驶舱 v1 四视图 单元测试
=================================================================
覆盖（不依赖 streamlit / 真实 quant.db，全部 fake 注入）：
  1. build_cockpit_payload 四键聚合：reconciliation / pnl / limit_usage / execution
  2. ①对账状态：消费 AccountReconciler.to_dict（consistent/frozen/last_diff）
  3. ②资金 PnL：消费 B-2 get_pnl_snapshot（时序库最新点）
  4. ③限额使用：collector limit_usage_* GAUGE 当前值 + LimitUsageMonitor.get_status 配置
  5. ④执行质量：ExecutionStatsMonitor.summary()（滑点/拒单/延迟）+ collector 兜底 GAUGE
  6. 无数据防御：空依赖不抛、graceful 返回空/None
  7. API /api/monitor/cockpit 返回与看板同源（同一 build_cockpit_payload）

运行方式:
  pytest tests/unit/test_business_cabin.py -v
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.api.routes import create_app  # noqa: E402
from myquant.api.dependencies.get_services import get_service_container  # noqa: E402
from myquant.database.connection import DatabaseConnection  # noqa: E402
from myquant.live.portfolio import LivePortfolio  # noqa: E402
from myquant.monitoring.field_crypto import FieldCrypto  # noqa: E402
from myquant.monitoring.metrics_collector import (  # noqa: E402
    BuiltinMetrics,
    collect_pnl_snapshot,
)
from myquant.monitoring.metrics_history import MetricsHistoryStore  # noqa: E402
from myquant.web.dashboard import MonitorCenterDataProvider  # noqa: E402


# ═══════════════════════════════════════════════
#  Fakes
# ═══════════════════════════════════════════════

class _FakeReconciler:
    """假对账器（to_dict 对齐 AccountReconciler）。"""

    def __init__(self, frozen: bool = False) -> None:
        self._frozen = frozen

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": "default", "consistent": not self._frozen,
            "frozen": self._frozen, "in_flight": False,
            "last_check_ts": time.time(), "resolved_by": None,
            "last_diff": {"cash_diff": 0.0} if not self._frozen else {"cash_diff": 123.4},
            "last_t1_diff": None,
        }


class _FakeMetric:
    def __init__(self, name: str, value: float, labels: Optional[Dict[str, str]] = None):
        self.name = name
        self.value = value
        self.labels = labels or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value,
                "labels": self.labels, "timestamp": 0.0}


class _FakeCollector:
    """假采集器：set_gauge 记录 + get_all(name) 查询（对齐 MetricsCollector 子集）。"""

    def __init__(self) -> None:
        self._points: List[_FakeMetric] = []

    def set_gauge(self, name: str, value: float,
                  labels: Optional[Dict[str, str]] = None) -> None:
        self._points.append(_FakeMetric(name, float(value), labels))

    def get_all(self, name: Optional[str] = None) -> List[_FakeMetric]:
        out = list(self._points)
        if name:
            out = [m for m in out if m.name == name]
        return out


class _FakeLimitMonitor:
    """假限额监控：get_status() 返回配置快照。"""

    def get_status(self) -> Dict[str, Any]:
        return {
            "warn_threshold": 0.8, "alert_threshold": 0.95,
            "limits": {"concentration": 0.2, "leverage": 1.5, "single_stock": None},
            "enabled": {"concentration": True, "leverage": True, "single_stock": False},
        }


class _FakeExecutionMonitor:
    """假执行统计：summary() 返回结构化快照。"""

    def summary(self) -> Dict[str, Any]:
        return {
            "latency": {
                "send_to_ack_ms": {"p99": 12.0, "mean": 5.0, "count": 10},
                "signal_to_fill_ms": {"p99": 30.0, "mean": 10.0, "count": 10},
                "ack_limit_ms": 20.0,
            },
            "slippage": {
                "buy_pct": {"mean": 0.1, "p99": 0.5, "count": 5},
                "sell_pct": {"mean": -0.1, "p99": -0.4, "count": 5},
            },
            "rejection": {
                "window_rejected": 1, "window_submitted": 100,
                "reject_rate": 0.01, "submitted_total": 100, "rejected_total": 1,
            },
        }


@pytest.fixture()
def store(tmp_path: Path) -> MetricsHistoryStore:
    conn = DatabaseConnection(str(tmp_path / "cabin.db"))
    s = MetricsHistoryStore(connection=conn)
    s.attach_field_crypto(FieldCrypto(key_file=str(tmp_path / "k")))
    return s


def _find_endpoint(app: Any, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
            return route.endpoint
    raise AssertionError(f"未找到端点 {method} {path}")


def _run(ep, *args, **kwargs):
    return asyncio.run(ep(*args, **kwargs))


# ═══════════════════════════════════════════════
#  1/6. 四视图聚合 + 无数据防御
# ═══════════════════════════════════════════════

class TestCockpitAggregation:
    def test_four_views_keys(self, store) -> None:
        collector = _FakeCollector()
        provider = MonitorCenterDataProvider(
            metrics_collector=collector,
            metrics_history_store=store,
            account_reconciler=_FakeReconciler(),
            limit_usage_monitor=_FakeLimitMonitor(),
            execution_stats_monitor=_FakeExecutionMonitor(),
        )
        payload = provider.build_cockpit_payload()
        assert set(payload.keys()) == {"reconciliation", "pnl", "limit_usage", "execution"}

    def test_no_data_defensive_no_throw(self, tmp_path: Path) -> None:
        conn = DatabaseConnection(str(tmp_path / "empty.db"))
        empty = MetricsHistoryStore(connection=conn)
        provider = MonitorCenterDataProvider(
            metrics_collector=_FakeCollector(),
            metrics_history_store=empty,
        )
        payload = provider.build_cockpit_payload()  # 不抛
        # 对账：未注入且全局单例可用 → dict；PnL 空库 → None；限额/执行 graceful
        assert payload["pnl"]["pnl"]["total_assets"] is None
        assert payload["execution"]["available"] is False
        conn.close_all()


# ═══════════════════════════════════════════════
#  2. ①对账状态
# ═══════════════════════════════════════════════

class TestReconciliationView:
    def test_reconciliation_fields(self, store) -> None:
        provider = MonitorCenterDataProvider(
            metrics_collector=_FakeCollector(), metrics_history_store=store,
            account_reconciler=_FakeReconciler(frozen=False),
        )
        rec = provider.get_reconciliation_status()
        assert rec["consistent"] is True and rec["frozen"] is False
        assert rec["last_diff"] == {"cash_diff": 0.0}

    def test_frozen_shown(self, store) -> None:
        provider = MonitorCenterDataProvider(
            metrics_collector=_FakeCollector(), metrics_history_store=store,
            account_reconciler=_FakeReconciler(frozen=True),
        )
        rec = provider.get_reconciliation_status()
        assert rec["frozen"] is True and rec["consistent"] is False


# ═══════════════════════════════════════════════
#  3. ②资金 PnL（与 B-2 同源）
# ═══════════════════════════════════════════════

class TestPnlView:
    def test_pnl_view_after_collect(self, store) -> None:
        pf = LivePortfolio(initial_capital=1_000_000.0, account_id="default")
        recon = _FakeReconciler()
        pf.set_account_reconciler(recon)
        collect_pnl_snapshot(portfolio=pf, store=store, reconciler=recon)
        provider = MonitorCenterDataProvider(
            metrics_collector=_FakeCollector(), metrics_history_store=store,
            account_reconciler=recon,
        )
        pnl = provider.get_pnl_snapshot()["pnl"]
        assert pnl["total_assets"] == pytest.approx(pf.get_total_assets())
        assert pnl["available_cash"] == pytest.approx(pf.available_cash)


# ═══════════════════════════════════════════════
#  4. ③限额使用
# ═══════════════════════════════════════════════

class TestLimitUsageView:
    def test_limit_usage_gauges_and_config(self, store) -> None:
        collector = _FakeCollector()
        collector.set_gauge(BuiltinMetrics.LIMIT_USAGE_CONCENTRATION, 0.82,
                            labels={"dimension": "concentration", "enabled": "true"})
        collector.set_gauge(BuiltinMetrics.LIMIT_USAGE_LEVERAGE, 0.40,
                            labels={"dimension": "leverage", "enabled": "true"})
        provider = MonitorCenterDataProvider(
            metrics_collector=collector, metrics_history_store=store,
            limit_usage_monitor=_FakeLimitMonitor(),
        )
        view = provider.get_limit_usage_view()
        assert view["usages"]["concentration"]["value"] == pytest.approx(0.82)
        assert view["usages"]["single_stock"]["enabled"] is False  # 未配置
        assert view["config"]["warn_threshold"] == pytest.approx(0.8)


# ═══════════════════════════════════════════════
#  5. ④执行质量
# ═══════════════════════════════════════════════

class TestExecutionView:
    def test_execution_summary(self, store) -> None:
        provider = MonitorCenterDataProvider(
            metrics_collector=_FakeCollector(), metrics_history_store=store,
            execution_stats_monitor=_FakeExecutionMonitor(),
        )
        view = provider.get_execution_quality_view()
        assert view["available"] is True
        assert view["summary"]["latency"]["send_to_ack_ms"]["p99"] == pytest.approx(12.0)
        assert view["summary"]["rejection"]["reject_rate"] == pytest.approx(0.01)

    def test_execution_gauges_fallback(self, store) -> None:
        collector = _FakeCollector()
        collector.set_gauge("execution_ack_p99_ms", 15.0)
        collector.set_gauge("order_reject_rate", 0.005)
        provider = MonitorCenterDataProvider(
            metrics_collector=collector, metrics_history_store=store,
        )
        view = provider.get_execution_quality_view()
        assert view["summary"] is None  # 未注入 monitor
        assert view["gauges"]["ack_p99_ms"] == pytest.approx(15.0)
        assert view["gauges"]["order_reject_rate"] == pytest.approx(0.005)
        assert view["available"] is True


# ═══════════════════════════════════════════════
#  7. API /api/monitor/cockpit 与看板同源
# ═══════════════════════════════════════════════

class TestCockpitEndpoint:
    def test_cockpit_endpoint(self, store) -> None:
        container = get_service_container()
        container.register("metrics_history_store", store)
        container.register("account_reconciler", _FakeReconciler())
        container.register("limit_usage_monitor", _FakeLimitMonitor())
        container.register("execution_stats_monitor", _FakeExecutionMonitor())
        app = create_app(enable_auth=False, enable_rate_limit=False)
        ep = _find_endpoint(app, "/api/monitor/cockpit", "GET")
        resp = _run(ep)
        data = resp.data
        assert set(data.keys()) == {"reconciliation", "pnl", "limit_usage", "execution"}
        assert data["reconciliation"]["frozen"] is False
        assert data["execution"]["summary"]["latency"]["send_to_ack_ms"]["p99"] == pytest.approx(12.0)
        # 与看板同源：再用 provider 取一次对账状态一致
        provider = MonitorCenterDataProvider(
            metrics_history_store=store, account_reconciler=_FakeReconciler(),
        )
        assert provider.get_reconciliation_status()["consistent"] == data["reconciliation"]["consistent"]
