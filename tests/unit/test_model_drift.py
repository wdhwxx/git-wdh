# 文件名：tests/unit/test_model_drift.py
"""
test_model_drift.py - C-1 模型漂移监控实时化测试
=====================================================
覆盖：
  1. ModelMonitor.evaluate_drift：PSI/KS 超基线阈值 → drift；阈值内 → healthy；
     阈值未配置（PENDING_CAL）→ not_configured（仅度量不预警）；部分阈值配置只判配置项；
     空序列返回 0；防御式校验（model_name/version/阈值/序列非法均显式报错）。
  2. DriftReport.to_dict 序列化。
  3. scheduler.run_daily_model_drift_check：漂移 → WARNING 告警（details 附 PSI/KS/阈值 +
     dedup_key=model:drift:<model>:<version>）；阈值未配置不告警；健康不告警；非法 pair 跳过；
     告警发送失败不阻断（防未来只消费已发生分布）。
  4. scheduler.get_model_drift_thresholds 读 P0-2 真源（R-04，当前 PENDING_CAL=~None）。
  5. attach_engine 注册日频模型漂移任务（enable_daily_model_drift，可禁用）。

依赖: pytest, myquant.ml.model_monitor, myquant.live.scheduler。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from myquant.core.constants import SeverityLevel
from myquant.live.scheduler import (
    LiveScheduler,
    attach_engine,
    get_model_drift_thresholds,
    run_daily_model_drift_check,
)
from myquant.ml.model_monitor import (
    DRIFT_STATUS_DRIFT,
    DRIFT_STATUS_HEALTHY,
    DRIFT_STATUS_NOT_CONFIGURED,
    DriftReport,
    ModelMonitor,
    ModelMonitorConfigError,
    ModelMonitorError,
)

_DRIFT_ACT: List[float] = [8.0] * 50 + [9.0] * 50   # 明显漂移（实际分布）
_DRIFT_EXP: List[float] = [0.0] * 50 + [1.0] * 50   # 基线分布
_PSI_THRESH: float = 0.25
_KS_THRESH: float = 0.30


class _CaptureAM:
    """捕获 alert() 调用的告警管理器桩。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def alert(self, **kwargs: Any) -> int:
        """记录告警调用并返回通道数（验收桩）。"""
        self.calls.append(dict(kwargs))
        return 1


class TestEvaluateDrift:
    def test_漂移超阈值_drift(self) -> None:
        r = ModelMonitor().evaluate_drift(
            _DRIFT_ACT, _DRIFT_EXP, "kunpeng", 3,
            psi_threshold=_PSI_THRESH, ks_threshold=_KS_THRESH,
        )
        assert r.status == DRIFT_STATUS_DRIFT
        assert r.psi is not None and r.psi > _PSI_THRESH
        assert r.ks is not None and r.ks > _KS_THRESH
        assert any("PSI" in reason for reason in r.reasons)
        assert any("KS" in reason for reason in r.reasons)
        # 附值：drift 报告携带 psi/ks 与阈值
        assert r.psi_threshold == _PSI_THRESH
        assert r.ks_threshold == _KS_THRESH

    def test_阈值内_healthy(self) -> None:
        r = ModelMonitor().evaluate_drift(
            _DRIFT_EXP, _DRIFT_EXP, "kunpeng", 3,
            psi_threshold=_PSI_THRESH, ks_threshold=_KS_THRESH,
        )
        assert r.status == DRIFT_STATUS_HEALTHY
        assert r.psi == 0.0 and r.ks == 0.0

    def test_阈值未配置_not_configured(self) -> None:
        r = ModelMonitor().evaluate_drift(_DRIFT_ACT, _DRIFT_EXP, "kunpeng", 3)
        assert r.status == DRIFT_STATUS_NOT_CONFIGURED
        assert r.psi_threshold is None and r.ks_threshold is None
        assert any("未配置" in reason for reason in r.reasons)

    def test_部分阈值配置_只判配置项(self) -> None:
        # 仅配置 psi，漂移 psi 超 → drift
        r1 = ModelMonitor().evaluate_drift(
            _DRIFT_ACT, _DRIFT_EXP, "k", 1, psi_threshold=_PSI_THRESH, ks_threshold=None,
        )
        assert r1.status == DRIFT_STATUS_DRIFT
        # 仅配置 psi，且 psi 在阈值内 → healthy（非 not_configured）
        r2 = ModelMonitor().evaluate_drift(
            _DRIFT_EXP, _DRIFT_EXP, "k", 1, psi_threshold=_PSI_THRESH, ks_threshold=None,
        )
        assert r2.status == DRIFT_STATUS_HEALTHY

    def test_空序列_返回0(self) -> None:
        r = ModelMonitor().evaluate_drift([], [], "k", 1,
                                          psi_threshold=_PSI_THRESH, ks_threshold=_KS_THRESH)
        assert r.psi == 0.0 and r.ks == 0.0 and r.status == DRIFT_STATUS_HEALTHY

    def test_防御_非法入参(self) -> None:
        m = ModelMonitor()
        with pytest.raises(ModelMonitorError):
            m.evaluate_drift(_DRIFT_ACT, _DRIFT_EXP, "", 3)
        with pytest.raises(ModelMonitorError):
            m.evaluate_drift(_DRIFT_ACT, _DRIFT_EXP, "k", -1)
        with pytest.raises(ModelMonitorConfigError):
            m.evaluate_drift(_DRIFT_ACT, _DRIFT_EXP, "k", 1, psi_threshold=-1)
        with pytest.raises(ModelMonitorError):
            m.evaluate_drift("not-a-seq", _DRIFT_EXP, "k", 1)


class TestDriftReport:
    def test_to_dict(self) -> None:
        r = DriftReport(model_name="k", version=1, psi=0.3, ks=0.4,
                        psi_threshold=0.25, ks_threshold=0.3,
                        status=DRIFT_STATUS_DRIFT, reasons=["PSI 超基线"])
        d = r.to_dict()
        for key in ("model_name", "version", "psi", "ks", "psi_threshold",
                    "ks_threshold", "status", "reasons"):
            assert key in d
        assert d["model_name"] == "k" and d["status"] == DRIFT_STATUS_DRIFT


class TestRunDailyDriftCheck:
    def test_漂移触发WARNING告警(self) -> None:
        am = _CaptureAM()
        summary = run_daily_model_drift_check(
            ModelMonitor(),
            drift_checks={"kunpeng": (_DRIFT_ACT, _DRIFT_EXP)},
            versions={"kunpeng": 3},
            alert_manager=am,
            psi_threshold=_PSI_THRESH, ks_threshold=_KS_THRESH,
        )
        assert summary["kunpeng"]["drift"] is True
        assert summary["kunpeng"]["alerted"] is True
        assert len(am.calls) == 1
        call = am.calls[0]
        assert call["level"] == SeverityLevel.WARNING
        assert call["category"] == "model"
        assert call["deduplication_key"] == "model:drift:kunpeng:3"
        assert call["details"]["psi"] > _PSI_THRESH
        assert call["details"]["ks"] > _KS_THRESH

    def test_阈值未配置不告警(self) -> None:
        am = _CaptureAM()
        summary = run_daily_model_drift_check(
            ModelMonitor(),
            drift_checks={"k": (_DRIFT_ACT, _DRIFT_EXP)},
            versions={"k": 1}, alert_manager=am,
        )  # 阈值走配置真源，当前 PENDING_CAL=None
        assert summary["k"]["status"] == DRIFT_STATUS_NOT_CONFIGURED
        assert summary["k"]["alerted"] is False
        assert len(am.calls) == 0

    def test_健康不告警(self) -> None:
        am = _CaptureAM()
        summary = run_daily_model_drift_check(
            ModelMonitor(),
            drift_checks={"k": (_DRIFT_EXP, _DRIFT_EXP)},
            versions={"k": 1}, alert_manager=am,
            psi_threshold=_PSI_THRESH, ks_threshold=_KS_THRESH,
        )
        assert summary["k"]["drift"] is False
        assert summary["k"]["alerted"] is False
        assert len(am.calls) == 0

    def test_非法pair跳过(self) -> None:
        am = _CaptureAM()
        summary = run_daily_model_drift_check(
            ModelMonitor(),
            drift_checks={"k": (_DRIFT_ACT,)},
            versions={"k": 1}, alert_manager=am,
        )
        assert summary["k"]["status"] == "invalid"
        assert summary["k"]["alerted"] is False
        assert len(am.calls) == 0

    def test_告警失败不阻断(self) -> None:
        am = SimpleNamespace(alert=lambda **kw: (_ for _ in ()).throw(RuntimeError("chan fail")))
        summary = run_daily_model_drift_check(
            ModelMonitor(),
            drift_checks={"k": (_DRIFT_ACT, _DRIFT_EXP)},
            versions={"k": 1}, alert_manager=am,
            psi_threshold=_PSI_THRESH, ks_threshold=_KS_THRESH,
        )
        assert summary["k"]["status"] == DRIFT_STATUS_DRIFT
        assert summary["k"]["alerted"] is False


class TestGetModelDriftThresholds:
    def test_读配置真源_R04(self) -> None:
        thr = get_model_drift_thresholds()
        assert set(thr.keys()) == {"psi_threshold", "ks_threshold", "ic_threshold"}
        # 当前 PENDING_CAL=~None：未配置则 None（不编造默认）
        assert thr["psi_threshold"] is None
        assert thr["ks_threshold"] is None

    def test_配置有值时读取(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "myquant.core.settings.get_monitoring_config",
            lambda: {"slo": {"model": {"psi_threshold": 0.25, "ks_threshold": 0.3,
                                        "ic_threshold": 0.02}}},
        )
        thr = get_model_drift_thresholds()
        assert thr["psi_threshold"] == 0.25
        assert thr["ks_threshold"] == 0.3
        assert thr["ic_threshold"] == 0.02


class TestAttachDailyDrift:
    @pytest.fixture()
    def scheduler(self) -> LiveScheduler:
        return LiveScheduler()

    def test_日频任务注册(self, scheduler: LiveScheduler) -> None:
        tasks = attach_engine(scheduler)
        assert "daily_model_drift" in tasks
        assert scheduler.get_task(tasks["daily_model_drift"]) is not None

    def test_日频任务调用run(self, scheduler: LiveScheduler, monkeypatch) -> None:
        calls: List[str] = []
        monkeypatch.setattr(
            "myquant.live.scheduler.run_daily_model_drift_check",
            lambda *a, **kw: calls.append("run") or {"__ran": True},
        )
        tasks = attach_engine(scheduler)
        task = scheduler.get_task(tasks["daily_model_drift"])
        task.func()
        assert calls == ["run"]

    def test_可禁用(self, scheduler: LiveScheduler) -> None:
        tasks = attach_engine(scheduler, enable_daily_model_drift=False)
        assert "daily_model_drift" not in tasks
