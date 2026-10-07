# -*- coding: utf-8 -*-
"""T-49 KP-P6-05：模型治理（未批准不 active + 漂移度量 + 月频治理任务）。"""
from pathlib import Path

from myquant.live.scheduler import run_monthly_model_governance
from myquant.ml.model_monitor import ModelMonitor
from myquant.ml.model_registry import ModelVersion

ROOT = Path(__file__).resolve().parents[2]


class FakeMeta:
    def __init__(self, version, status, approval):
        self.version = version
        self.status = status
        self.approval = approval


class FakeRegistry:
    def __init__(self, metas=None, mark=None):
        self._metas = metas or {}
        self._mark = mark
        self.called = []

    def list_models(self):
        return list(self._metas)

    def get_model_metadata(self, name, version=None):
        return self._metas.get(name)

    def mark_pending_review(self, name, version):
        self.called.append((name, version))


class FakeMonitor:
    def psi_score(self, a, e):
        return 0.5

    def ks_statistic(self, a, e):
        return 0.7


def test_model_version_default_pending_review():
    v = ModelVersion(model_name="m", version=1)
    assert v.status == "pending_review"  # 未批准不 active
    assert v.tier == "dev"
    assert v.model_card is None
    assert v.frozen is False
    assert v.approval is None


def test_model_version_to_dict_governance_fields():
    v = ModelVersion(model_name="m", version=1, status="pending_review",
                     tier="prod", model_card={"use": "score"}, frozen=True,
                     approval={"approver": "a", "at": "t", "note": "ok"})
    d = v.to_dict()
    assert d["tier"] == "prod"
    assert d["model_card"] == {"use": "score"}
    assert d["frozen"] is True
    assert d["approval"]["approver"] == "a"


def test_registry_register_pending_review_src():
    src = (ROOT / "myquant" / "ml" / "model_registry.py").read_text(encoding="utf-8")
    assert 'status="pending_review"' in src
    assert "def approve_version" in src


def test_monitor_psi_ks():
    m = ModelMonitor()
    psi = m.psi_score([0.1, 0.1, 0.2, 0.2, 0.9, 0.9], [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    ks = m.ks_statistic([1.0, 2.0, 3.0, 4.0, 5.0], [6.0, 7.0, 8.0, 9.0, 10.0])
    assert psi > 0.0
    assert 0.0 < ks <= 1.0


def test_governance_unapproved_active_alerts():
    reg = FakeRegistry({"m1": FakeMeta(2, "active", None)}, mark="cb")
    mon = FakeMonitor()
    out = run_monthly_model_governance(reg, mon, model_names=["m1"], drift_checks={})
    assert out["m1"]["approved"] is False
    assert out["m1"]["active_version"] == 2
    assert ("m1", 2) in reg.called  # 违规 active 回写 pending_review


def test_governance_drift_alert():
    reg = FakeRegistry({"m1": FakeMeta(1, "active", {"approver": "a", "at": "t", "note": ""})})
    mon = FakeMonitor()
    out = run_monthly_model_governance(reg, mon, model_names=["m1"],
                                       drift_checks={"m1": ([1.0], [0.0])})
    assert out["m1"]["approved"] is True
    assert out["m1"]["drift_alert"] is True  # PSI=0.5 > 0.25


def test_scheduler_monthly_task_src():
    src = (ROOT / "myquant" / "live" / "scheduler.py").read_text(encoding="utf-8")
    assert "run_monthly_model_governance" in src
    assert "monthly_governance_cron" in src
    assert "未批准不 active" in src


def test_scheduler_monthly_governance_registered_by_default():
    """DoD（回写#253）：月频治理独立顶层注册——默认 enable_resend_pending=False 时仍注册
    （修复原缺陷：曾嵌套于 resend_pending 块内导致默认不注册）。"""
    from myquant.live.scheduler import LiveScheduler, attach_engine

    scheduler = LiveScheduler()
    tasks = attach_engine(scheduler)
    assert "monthly_governance" in tasks
    assert scheduler.get_task(tasks["monthly_governance"]) is not None
    assert "resend_pending" not in tasks  # 默认关闭，佐证月治理不依赖它


def test_scheduler_monthly_governance_disable():
    from myquant.live.scheduler import LiveScheduler, attach_engine

    scheduler = LiveScheduler()
    tasks = attach_engine(scheduler, enable_monthly_governance=False)
    assert "monthly_governance" not in tasks