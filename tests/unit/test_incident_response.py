# -*- coding: utf-8 -*-
"""T-36 KP-P5-11 事故响应制度：可执行处置（告警+暂停开仓/降级）+ 制度文档 + 配置留痕。"""
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from myquant.live.scheduler import LiveScheduler, attach_engine

ROOT = Path(__file__).resolve().parents[2]
INCIDENT_DOC = ROOT / "docs" / "实盘事故响应与复盘制度.md"
LIVE_CONFIG = ROOT / "config" / "live_config.yaml"


@pytest.fixture()
def scheduler():
    return LiveScheduler()


def _install_hooks(monkeypatch, *, is_healthy, is_critical, calls, engine=None):
    def _fake_run_all():
        return []

    def _fake_get_summary():
        return {
            "is_healthy": is_healthy,
            "is_critical": is_critical,
            "overall_status": "unhealthy" if is_critical else ("degraded" if not is_healthy else "healthy"),
            "degraded_count": 0 if is_healthy else 1,
            "unhealthy_count": 1 if is_critical else 0,
        }

    hc = SimpleNamespace(run_all=_fake_run_all, get_summary=_fake_get_summary)
    monkeypatch.setattr("myquant.core.health_check.get_health_checker", lambda: hc)
    monkeypatch.setattr(
        "myquant.monitoring.alerting.get_alert_manager",
        lambda: SimpleNamespace(alert=lambda **kw: calls.append(kw)),
    )
    monkeypatch.setattr(
        "myquant.live.scheduler.HealthPatrol.build_report",
        lambda self, **kw: {"status": "healthy", "checks": []},
    )


def _run_patrol(monkeypatch, *, is_healthy, is_critical, pause_on_critical, engine):
    calls: list = []
    paused: list = []
    eng = SimpleNamespace(pause=lambda *a, **k: paused.append(True)) if engine is None else engine
    _install_hooks(monkeypatch, is_healthy=is_healthy, is_critical=is_critical, calls=calls, engine=eng)
    sched = LiveScheduler()
    tasks = attach_engine(
        sched, engine=eng, pause_on_critical=pause_on_critical,
    )
    patrol_task = sched.get_task(tasks["patrol"])
    patrol_task.func()
    return calls, paused


def test_critical_pause_on_critical_pauses(monkeypatch):
    """非健康 CRITICAL + pause_on_critical=True → 暂停引擎（暂停开仓/降级）。"""
    _, paused = _run_patrol(
        monkeypatch, is_healthy=False, is_critical=True, pause_on_critical=True, engine=None,
    )
    assert paused == [True]


def test_critical_no_pause_by_default(monkeypatch):
    """pause_on_critical 缺省 False → 不自动暂停（处置需显式开启）。"""
    _, paused = _run_patrol(
        monkeypatch, is_healthy=False, is_critical=True, pause_on_critical=False, engine=None,
    )
    assert paused == []


def test_warning_no_pause(monkeypatch):
    """非健康但非 CRITICAL（WARNING）→ 不暂停开仓。"""
    _, paused = _run_patrol(
        monkeypatch, is_healthy=False, is_critical=False, pause_on_critical=True, engine=None,
    )
    assert paused == []


def test_pause_failure_does_not_crash(monkeypatch):
    """engine.pause 异常 → 仅 warning，不中断巡检。"""
    class Boom:
        def pause(self):
            raise RuntimeError("pause failed")

    _, paused = _run_patrol(
        monkeypatch, is_healthy=False, is_critical=True, pause_on_critical=True, engine=Boom(),
    )
    assert paused == []


def test_incident_document_exists():
    """制度文档存在且含分级/指标/通报升级/无责复盘核心要素。"""
    assert INCIDENT_DOC.exists()
    text = INCIDENT_DOC.read_text(encoding="utf-8")
    for kw in ("P0", "P1", "P2", "P3", "MTTD", "MTTR", "通报", "升级", "无责复盘", "演练"):
        assert kw in text, f"制度文档缺关键词: {kw}"


def test_live_config_on_unhealthy_declared():
    """live_config.yaml 巡检 action 声明可执行处置 on_unhealthy=[alert, pause_trading]。"""
    cfg = yaml.safe_load(io.open(LIVE_CONFIG, "r", encoding="utf-8"))
    tasks = cfg.get("scheduler", {}).get("tasks", [])
    patrol = next((t for t in tasks if t.get("name") == "盘中状态巡检"), None)
    assert patrol is not None
    assert patrol.get("action") == "health_check"
    assert patrol.get("on_unhealthy") == ["alert", "pause_trading"]
