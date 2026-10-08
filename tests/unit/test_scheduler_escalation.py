# 文件名：tests/unit/test_scheduler_escalation.py
"""
test_scheduler_escalation.py - 告警升级无调用方测试（KP-P5-04 / T-27）
========================================================================
覆盖修复：
  1. attach_engine 注册告警升级检查周期任务（escalation）。
  2. 周期任务调用 get_alert_manager().check_escalations()。

依赖: pytest, myquant.live.scheduler
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from myquant.live.scheduler import LiveScheduler, attach_engine


@pytest.fixture()
def scheduler():
    return LiveScheduler()


class TestSchedulerEscalation:
    def test_升级检查任务注册(self, scheduler) -> None:
        """DoD：attach_engine 注册告警升级检查任务。"""
        tasks = attach_engine(scheduler)
        assert "escalation" in tasks
        assert scheduler.get_task(tasks["escalation"]) is not None

    def test_升级检查被调用(self, scheduler, monkeypatch) -> None:
        """DoD：周期任务触发 check_escalations()。"""
        calls: list = []
        monkeypatch.setattr(
            "myquant.core.registry.get_component",
            lambda name: (
                (lambda: SimpleNamespace(check_escalations=lambda: calls.append(1) or 1))
                if name == "alert_manager" else None
            ),
        )
        tasks = attach_engine(scheduler)
        esc_task = scheduler.get_task(tasks["escalation"])
        esc_task.func()
        assert len(calls) == 1

    def test_升级检查可禁用(self, scheduler) -> None:
        """DoD：enable_escalation=False 时不注册升级检查任务。"""
        tasks = attach_engine(scheduler, enable_escalation=False)
        assert "escalation" not in tasks
