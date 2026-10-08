# 文件名：tests/unit/test_scheduler_health_alert.py
"""
test_scheduler_health_alert.py - 监控无阈值告警闭环测试（KP-P5-03 / T-26）
==========================================================================
覆盖修复：
  1. attach_engine 注册健康巡检任务（60s 间隔）。
  2. 巡检执行 HealthChecker.run_all，非 healthy 时经 alert_mgr.alert 告警。
  3. healthy 时不触发告警。

依赖: pytest, myquant.live.scheduler
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from myquant.live.scheduler import HealthPatrol, LiveScheduler, attach_engine


@pytest.fixture()
def scheduler():
    return LiveScheduler()


def _install_health_hooks(monkeypatch, *, is_healthy, calls):
    def _fake_run_all():
        return []

    def _fake_get_summary():
        return {
            "is_healthy": is_healthy,
            "is_critical": False,
            "overall_status": "healthy" if is_healthy else "degraded",
            "degraded_count": 0 if is_healthy else 1,
            "unhealthy_count": 0,
        }

    hc = SimpleNamespace(run_all=_fake_run_all, get_summary=_fake_get_summary)
    monkeypatch.setattr("myquant.core.health_check.get_health_checker", lambda: hc)
    monkeypatch.setattr(
        "myquant.core.registry.get_component",
        lambda name: (
            (lambda: SimpleNamespace(alert=lambda **kw: calls.append(kw)))
            if name == "alert_manager" else None
        ),
    )
    monkeypatch.setattr(
        "myquant.live.scheduler.HealthPatrol.build_report",
        lambda self, **kw: {"status": "healthy", "checks": []},
    )


class TestSchedulerHealthAlert:
    def test_巡检任务注册(self, scheduler) -> None:
        """DoD：attach_engine 注册健康巡检任务。"""
        tasks = attach_engine(scheduler)
        assert "patrol" in tasks
        assert scheduler.get_task(tasks["patrol"]) is not None

    def test_非healthy_触发告警(self, scheduler, monkeypatch) -> None:
        """DoD：健康检查非 healthy 时经告警管理器 alert。"""
        calls: list = []
        _install_health_hooks(monkeypatch, is_healthy=False, calls=calls)
        tasks = attach_engine(scheduler)
        patrol_task = scheduler.get_task(tasks["patrol"])
        patrol_task.func()
        assert len(calls) == 1
        assert "健康检查非健康" in calls[0]["message"]

    def test_healthy_不触发告警(self, scheduler, monkeypatch) -> None:
        """DoD：健康检查 healthy 时不触发告警。"""
        calls: list = []
        _install_health_hooks(monkeypatch, is_healthy=True, calls=calls)
        tasks = attach_engine(scheduler)
        patrol_task = scheduler.get_task(tasks["patrol"])
        patrol_task.func()
        assert calls == []
