# 文件名：tests/unit/test_meta_monitor.py
"""
test_meta_monitor.py - BASE-5 监控自身可观测性（元监控）单元测试
===========================================================
覆盖：
  1. MetaMonitor 全组件注入健康运行：五类探针 / 指标进 collector / 落库可查
  2. 异常场景：事件总线积压 → degraded；独立告警通道收到 meta 告警
  3. R-08 独立冷却：同 key 冷却窗口内只发一次；业务告警管理器完全隔离
  4. HealthPatrol 集成：attach_meta_monitor 后巡检报告含 meta_monitor 检查项
  5. 防御式：非法参数回退 / 探针异常记 down 不阻断 / 看板占位不告警
  6. 探针可注入覆盖（set_probe）

运行方式:
  pytest tests/unit/test_meta_monitor.py -v
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from myquant.core.event_bus import Event, EventBus
from myquant.live.scheduler import HealthPatrol, PATROL_HEALTHY, PATROL_DEGRADED
from myquant.monitoring.alerting import AlertManager, LogAlertChannel
from myquant.monitoring.meta_monitor import (
    DEFAULT_META_COOLDOWN_SECONDS,
    MetaMonitor,
    META_DOWN,
)
from myquant.monitoring.metrics_collector import MetricsCollector
from myquant.core.metrics_history import MetricsHistoryStore


@pytest.fixture()
def components(tmp_path: Path) -> Dict[str, Any]:
    """隔离组件：不触全局单例，临时库不碰 quant.db"""
    bus = EventBus(max_history=100)
    collector = MetricsCollector().register_builtin_metrics()
    store = MetricsHistoryStore(db_path=tmp_path / "meta_test.db")
    business = AlertManager()
    business.add_channel(LogAlertChannel(name="test-log", logger_name="test.business"))
    return {"bus": bus, "collector": collector, "store": store, "business": business}


def make_meta(components: Dict[str, Any], **kwargs: Any) -> MetaMonitor:
    return MetaMonitor(
        collector=components["collector"],
        metrics_history=components["store"],
        event_bus=components["bus"],
        business_alert_manager=components["business"],
        **kwargs,
    )


# ═══════════════════════════════════════════
#  1. 健康运行
# ═══════════════════════════════════════════

class TestHealthyRun:
    def test_all_probes_and_healthy_status(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        report = meta.run()
        assert report["status"] == "healthy"
        assert set(report["checks"].keys()) == {
            "collector_health",
            "event_bus_backlog",
            "storage_write_latency",
            "alert_send_success",
            "dashboard_availability",
        }

    def test_metrics_into_collector(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        meta.run()
        assert components["collector"].get_value("meta_status") == 0.0
        assert components["collector"].get_value("meta_dashboard_availability") == 0.0

    def test_metrics_persisted(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        meta.run()
        rows = components["store"].query("meta_status")
        assert len(rows) == 1
        assert rows[0]["value"] == 0.0

    def test_probe_status_interface(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        status, detail = meta.probe_status()  # 未跑过自动先跑一轮
        assert status == "healthy"
        assert "checks" in detail and "last_run_ts" in detail

    def test_alert_send_success_rate_recorded(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        meta.run()
        rate = components["collector"].get_value("meta_alert_send_success_rate")
        assert rate == 1.0  # 尚无告警发送记录 → 1.0


# ═══════════════════════════════════════════
#  2/3. 异常 + R-08 独立冷却
# ═══════════════════════════════════════════

class TestAbnormalAndCooldown:
    def test_backlog_degraded_and_meta_alert_sent(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components, cooldown_seconds=60.0)
        for _ in range(95):  # 历史 95/100 → ratio 0.95 >= 0.8
            components["bus"].publish(Event("FILL", data={"i": 1}))
        report = meta.run()
        assert report["status"] == "degraded"
        # 独立告警通道已收到 meta 告警
        hist = meta._get_meta_manager().stats["history_size"]
        assert hist >= 1
        # 业务告警管理器完全隔离（未收到 meta 告警）
        assert components["business"].stats["history_size"] == 0

    def test_independent_cooldown_dedup(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components, cooldown_seconds=60.0)
        for _ in range(95):
            components["bus"].publish(Event("FILL", data={"i": 1}))
        meta.run()
        hist_size = lambda: meta._get_meta_manager().stats["history_size"]  # noqa: E731
        before = hist_size()
        meta.run()  # 同状态同冷却窗口 → 不再发送
        assert hist_size() == before

    def test_new_instance_can_alert_again(self, components: Dict[str, Any]) -> None:
        for _ in range(95):
            components["bus"].publish(Event("FILL", data={"i": 1}))
        meta1 = make_meta(components, cooldown_seconds=60.0)
        meta1.run()
        meta2 = make_meta(components, cooldown_seconds=60.0)
        meta2.run()  # 新冷却缓存 → 可再告警
        assert meta2._get_meta_manager().stats["history_size"] >= 1


# ═══════════════════════════════════════════
#  4. HealthPatrol 集成
# ═══════════════════════════════════════════

class TestHealthPatrolIntegration:
    def test_patrol_report_contains_meta_monitor(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        patrol = HealthPatrol(metrics_history=components["store"])
        patrol.attach_meta_monitor(meta)
        report = patrol.build_report()  # 各组件未接入 → down；meta 应为 healthy
        assert "meta_monitor" in report["checks"]
        assert report["checks"]["meta_monitor"]["status"] == "healthy"

    def test_meta_down_pulls_patrol_down(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        meta.set_probe("collector_health", lambda: (META_DOWN, {"reason": "injected"}))
        patrol = HealthPatrol()
        patrol.attach_meta_monitor(meta)
        report = patrol.build_report()
        assert report["checks"]["meta_monitor"]["status"] == "down"

    def test_no_meta_monitor_skipped(self, components: Dict[str, Any]) -> None:
        patrol = HealthPatrol()
        report = patrol.build_report()
        assert "meta_monitor" not in report["checks"]


# ═══════════════════════════════════════════
#  5/6. 防御式 + 探针注入
# ═══════════════════════════════════════════

class TestDefensive:
    def test_invalid_cooldown_falls_back(self) -> None:
        meta = MetaMonitor(cooldown_seconds=-5)
        assert meta._cooldown_seconds == DEFAULT_META_COOLDOWN_SECONDS

    def test_probe_exception_marks_down_but_continues(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)

        def boom() -> tuple:
            raise RuntimeError("probe failed")

        meta.set_probe("collector_health", boom)
        report = meta.run()  # 不应抛异常
        assert report["checks"]["collector_health"]["status"] == "down"
        # 其余探针仍采集
        assert "event_bus_backlog" in report["checks"]

    def test_dashboard_standby_healthy_no_alert(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        status, detail = meta._probe_dashboard_availability()
        assert status == "healthy"
        assert detail["mode"] == "standby"

    def test_dashboard_probe_injected(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        meta.set_dashboard_probe(lambda: (False, {"reason": "streamlit down"}))
        status, detail = meta._probe_dashboard_availability()
        assert status == "down"
        assert detail["reason"] == "streamlit down"

    def test_set_probe_validation(self, components: Dict[str, Any]) -> None:
        meta = make_meta(components)
        with pytest.raises(ValueError):
            meta.set_probe("", lambda: ("healthy", {}))  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            meta.set_probe("x", "not-callable")  # type: ignore[arg-type]
