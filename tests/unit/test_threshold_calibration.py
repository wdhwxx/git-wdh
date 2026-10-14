# 文件名：tests/unit/test_threshold_calibration.py
"""
D-3 阈值自动校准测试（回写#245）
==================================
覆盖：ThresholdCalibrator 构造防御；shadow 只出建议/auto 自动生效；不足样本与
常量序列不校准（不编造退化阈值）；人工确认 apply_proposal；人工覆盖 set_manual
（优先级最高）；回滚 rollback（恢复旧阈值/撤人工覆盖）；有效阈值解析优先级
（人工覆盖 > applied 校准 > None）；校准历史可审计（JSON 可序列化/上限裁剪）；
审计失败不阻断；_read_calibrator_config 读 settings.yaml 真源（R-04）；与
DynamicBaseline 集成（regime 切换触发校准、冷启动静态阈值消费校准阈值、
冷启动不校准、to_dict 含校准摘要、未挂接校准器行为不变）。
全部注入假告警/假指标/内存审计/固定时钟，不碰 quant.db。
"""

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from myquant.monitoring.dynamic_baseline import (
    CAL_STATUS_APPLIED,
    CAL_STATUS_PROPOSED,
    DynamicBaseline,
    ThresholdCalibrator,
    _read_calibrator_config,
)

# ── 测试辅助 ──────────────────────────────────────────────────

class _AuditRecorder:
    """内存审计记录器（隔离不碰库）。"""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def append(self, kind: str, action: str, actor: str,
               detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rec = {"kind": kind, "action": action, "actor": actor,
               "detail": dict(detail or {})}
        self.records.append(rec)
        return rec


class _FakeAlert:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def alert(self, **kw: Any) -> int:
        self.calls.append(kw)
        return 1


class _FakeMetrics:
    def __init__(self) -> None:
        self.gauges: Dict[str, float] = {}

    def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
        self.gauges[name] = float(value)


def _clock_fixed(t0: float = 1_000_000.0) -> Any:
    now = [t0]

    def clock() -> float:
        return now[0]

    clock.now = now  # type: ignore[attr-defined]
    return clock


def _series_points(base: float, n: int, wobble: float = 3.0) -> List[Tuple[float, float]]:
    return [(float(100 * k), base + (k % 7) * wobble / 7.0) for k in range(n)]


# ── 构造防御 ──────────────────────────────────────────────────

class TestConstructor:
    def test_illegal_method_rejected(self) -> None:
        with pytest.raises(ValueError):
            ThresholdCalibrator(method="mad")

    def test_negative_k_rejected(self) -> None:
        with pytest.raises(ValueError):
            ThresholdCalibrator(k=-1.0)

    def test_illegal_quantiles_rejected(self) -> None:
        with pytest.raises(ValueError):
            ThresholdCalibrator(quantile_low=0.9, quantile_high=0.1)
        with pytest.raises(ValueError):
            ThresholdCalibrator(quantile_low=0.0, quantile_high=0.5)

    def test_min_samples_lt_1_rejected(self) -> None:
        with pytest.raises(ValueError):
            ThresholdCalibrator(min_samples=0)

    def test_illegal_apply_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            ThresholdCalibrator(apply_mode="instant")

    def test_bad_history_limit_rejected(self) -> None:
        with pytest.raises(ValueError):
            ThresholdCalibrator(max_history=0)


# ── 校准触发 ──────────────────────────────────────────────────

class TestCalibrate:
    def test_shadow_proposes_without_effect(self) -> None:
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5,
                                  apply_mode="shadow")
        rec = cal.calibrate_from_history("s1", "trend_up", _series_points(100, 30))
        assert rec is not None
        assert rec.status == CAL_STATUS_PROPOSED
        assert rec.new_low < rec.new_high
        assert rec.samples >= 5
        # shadow 建议未生效
        assert cal.resolve("s1", "trend_up") is None

    def test_auto_applies_immediately(self) -> None:
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5,
                                  apply_mode="auto")
        rec = cal.calibrate_from_history("s1", "range", _series_points(50, 20))
        assert rec is not None
        assert rec.status == CAL_STATUS_APPLIED
        bounds = cal.resolve("s1", "range")
        assert bounds is not None
        assert bounds[0] < bounds[1]

    def test_insufficient_samples_no_calibration(self) -> None:
        cal = ThresholdCalibrator(min_samples=10)
        rec = cal.calibrate_from_history("s1", "trend_up", _series_points(100, 5))
        assert rec is None

    def test_constant_series_no_calibration(self) -> None:
        """常量序列退化区间不编造阈值。"""
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5)
        points = [(float(100 * k), 5.0) for k in range(20)]
        rec = cal.calibrate_from_history("s1", "trend_up", points)
        assert rec is None

    def test_quantile_method_calibrates(self) -> None:
        cal = ThresholdCalibrator(method="quantile", min_samples=10,
                                  quantile_low=0.1, quantile_high=0.9)
        values = [100.0 + (k % 10) for k in range(40)]
        points = [(float(k), v) for k, v in enumerate(values)]
        rec = cal.calibrate_from_history("s1", "range", points)
        assert rec is not None
        assert rec.new_low <= rec.new_high

    def test_non_finite_values_no_calibration(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        points = [(float(k), 100.0) for k in range(5)]
        points.append((500.0, float("inf")))
        assert cal.calibrate_from_history("s1", "r", points) is None

    def test_empty_history_no_calibration(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        assert cal.calibrate_from_history("s1", "r", []) is None

    def test_series_regime_validation(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        with pytest.raises(ValueError):
            cal.calibrate_from_history("", "r", _series_points(100, 10))
        with pytest.raises(ValueError):
            cal.calibrate_from_history("s1", "", _series_points(100, 10))


# ── 人工确认 / 覆盖 / 回滚 ────────────────────────────────────

class TestApplyAndOverride:
    def test_apply_proposal_applied(self) -> None:
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5,
                                  apply_mode="shadow")
        rec = cal.calibrate_from_history("s1", "trend_up", _series_points(100, 30))
        assert rec is not None
        applied = cal.apply_proposal(rec.calibration_id, actor="trader", reason="确认")
        assert applied.status == CAL_STATUS_APPLIED
        assert cal.resolve("s1", "trend_up") is not None

    def test_apply_proposal_validation(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        with pytest.raises(ValueError):
            cal.apply_proposal("no_such_id", actor="trader")
        rec = cal.calibrate_from_history("s1", "r", _series_points(100, 10))
        cal.apply_proposal(rec.calibration_id, actor="trader")  # 已 applied
        with pytest.raises(ValueError):
            cal.apply_proposal(rec.calibration_id, actor="trader")
        with pytest.raises(ValueError):
            cal.apply_proposal(rec.calibration_id, actor="  ")

    def test_manual_override_priority_and_history(self) -> None:
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5,
                                  apply_mode="auto")
        cal.calibrate_from_history("s1", "trend_up", _series_points(100, 30))
        assert cal.resolve("s1", "trend_up") is not None
        ov = cal.set_manual_thresholds("s1", "trend_up", 80.0, 120.0,
                                       actor="trader", reason="人工覆盖")
        assert ov.override is True
        assert cal.resolve("s1", "trend_up") == (80.0, 120.0)
        # 被覆盖的 applied 校准标记 overridden
        assert any(r["status"] == "overridden" for r in cal.history())

    def test_manual_override_validation(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        with pytest.raises(ValueError):
            cal.set_manual_thresholds("", "r", 0.0, 1.0, actor="trader")
        with pytest.raises(ValueError):
            cal.set_manual_thresholds("s1", "", 0.0, 1.0, actor="trader")
        with pytest.raises(ValueError):
            cal.set_manual_thresholds("s1", "r", 0.0, 1.0, actor="  ")
        with pytest.raises(ValueError):
            cal.set_manual_thresholds("s1", "r", 2.0, 1.0, actor="trader")
        with pytest.raises(ValueError):
            cal.set_manual_thresholds("s1", "r", float("nan"), 1.0, actor="trader")

    def test_rollback_applied_calibration_restores_old(self) -> None:
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5,
                                  apply_mode="auto")
        cal.calibrate_from_history("s1", "trend_up", _series_points(100, 30))
        bounds1 = cal.resolve("s1", "trend_up")
        rec2 = cal.calibrate_from_history("s1", "trend_up", _series_points(200, 30))
        assert rec2 is not None
        bounds2 = cal.resolve("s1", "trend_up")
        assert bounds2 != bounds1
        assert cal.rollback(rec2.calibration_id, actor="ops", reason="回滚") is True
        assert cal.resolve("s1", "trend_up") == bounds1

    def test_rollback_override_restores_calibration(self) -> None:
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5,
                                  apply_mode="auto")
        cal.calibrate_from_history("s1", "trend_up", _series_points(100, 30))
        bounds_cal = cal.resolve("s1", "trend_up")
        ov = cal.set_manual_thresholds("s1", "trend_up", 80.0, 120.0,
                                       actor="trader")
        assert cal.resolve("s1", "trend_up") == (80.0, 120.0)
        assert cal.rollback(ov.calibration_id, actor="ops") is True
        assert cal.resolve("s1", "trend_up") == bounds_cal

    def test_rollback_unknown_or_not_applied_false(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        assert cal.rollback("no_such_id", actor="ops") is False
        rec = cal.calibrate_from_history("s1", "r", _series_points(100, 10))
        # shadow 模式 proposed 不可回滚
        assert rec is not None and rec.status == CAL_STATUS_PROPOSED
        assert cal.rollback(rec.calibration_id, actor="ops") is False

    def test_rollback_validation(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        with pytest.raises(ValueError):
            cal.rollback("", actor="ops")
        with pytest.raises(ValueError):
            cal.rollback("x", actor="  ")


# ── 解析优先级 ─────────────────────────────────────────────────

class TestResolvePriority:
    def test_manual_overrides_applied(self) -> None:
        cal = ThresholdCalibrator(min_samples=5, apply_mode="auto")
        cal.calibrate_from_history("s1", "r", _series_points(100, 10))
        cal.set_manual_thresholds("s1", "r", 0.0, 1.0, actor="trader")
        assert cal.resolve("s1", "r") == (0.0, 1.0)

    def test_resolve_none_without_calibration(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        assert cal.resolve("s1", "r") is None

    def test_per_regime_isolation(self) -> None:
        cal = ThresholdCalibrator(min_samples=5, apply_mode="auto")
        cal.calibrate_from_history("s1", "trend_up", _series_points(100, 10))
        assert cal.resolve("s1", "trend_up") is not None
        assert cal.resolve("s1", "range") is None  # 不同 regime 独立


# ── 留痕 / 审计 / 观测 ─────────────────────────────────────────

class TestTrail:
    def test_history_json_serializable(self) -> None:
        cal = ThresholdCalibrator(min_samples=5, apply_mode="auto")
        cal.calibrate_from_history("s1", "r", _series_points(100, 10))
        cal.set_manual_thresholds("s1", "r", 0.0, 1.0, actor="trader")
        json.dumps(cal.history())  # 不可序列化则抛异常

    def test_history_limit_validation(self) -> None:
        cal = ThresholdCalibrator(min_samples=5)
        with pytest.raises(ValueError):
            cal.history(limit=0)

    def test_history_cap(self) -> None:
        cal = ThresholdCalibrator(min_samples=5, max_history=3,
                                  apply_mode="auto")
        for i in range(8):
            cal.calibrate_from_history(f"s{i}", "r", _series_points(100, 10))
        assert len(cal.history()) == 3

    def test_audit_records_written(self) -> None:
        audit = _AuditRecorder()
        cal = ThresholdCalibrator(min_samples=5, audit_log=audit)
        cal.calibrate_from_history("s1", "r", _series_points(100, 10))
        assert any(r["kind"] == "dynamic_baseline" for r in audit.records)
        assert any("calibrate" in r["action"] for r in audit.records)

    def test_audit_failure_not_blocking(self) -> None:
        class _BrokenAudit:
            def append(self, *a: Any, **kw: Any) -> Dict[str, Any]:
                raise RuntimeError("audit down")

        cal = ThresholdCalibrator(min_samples=5, audit_log=_BrokenAudit(),
                                  apply_mode="auto")
        rec = cal.calibrate_from_history("s1", "r", _series_points(100, 10))
        assert rec is not None and rec.status == CAL_STATUS_APPLIED

    def test_summary_counts(self) -> None:
        cal = ThresholdCalibrator(min_samples=5, apply_mode="shadow")
        cal.calibrate_from_history("s1", "r", _series_points(100, 10))
        s = cal.summary()
        assert s["method"] == "zscore"
        assert s["apply_mode"] == "shadow"
        assert s["proposed_total"] == 1
        assert s["history_total"] == 1


# ── 配置真源（R-04）───────────────────────────────────────────

class TestConfigSource:
    def test_read_calibrator_config_from_yaml(self) -> None:
        cfg = _read_calibrator_config()
        assert cfg["method"] == "zscore"
        assert cfg["k"] == 3.0
        assert cfg["quantile_low"] == 0.05
        assert cfg["quantile_high"] == 0.95
        assert cfg["min_samples"] == 20
        assert cfg["apply_mode"] == "shadow"


# ── DynamicBaseline 集成 ───────────────────────────────────────

class TestDynamicBaselineIntegration:
    def test_regime_switch_triggers_calibration(self) -> None:
        clock = _clock_fixed()
        am = _FakeAlert()
        met = _FakeMetrics()
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5,
                                  apply_mode="shadow", clock=clock)
        db = DynamicBaseline(window=10, k=3.0, min_samples=3, yoy_lag=2,
                             clock=clock, alert_manager=am,
                             metrics_collector=met, calibrator=cal)
        for k in range(8):
            db.record("s1", float(k * 100), 100.0 + (k % 3))
        db.set_regime("trend_up")
        # 新 regime 无历史 → 冷启动不校准
        assert cal.resolve("s1", "trend_up") is None
        for k in range(8, 14):
            db.record("s1", float(k * 100), 100.0 + (k % 3))  # 带小幅波动
        db.set_regime("range")
        db.set_regime("trend_up")  # 切回 → 用 trend_up 已积累历史自动校准（shadow 建议）
        proposals = [
            r for r in cal.history()
            if r["regime"] == "trend_up" and r["status"] == CAL_STATUS_PROPOSED
        ]
        assert proposals, "regime 切换应触发 shadow 校准建议"
        assert proposals[-1]["new_low"] < proposals[-1]["new_high"]
        # shadow 只出建议：resolve 仍为 None（需人工确认后生效）
        assert cal.resolve("s1", "trend_up") is None

    def test_static_fallback_uses_calibrated_bounds(self) -> None:
        """验收：regime 切换后阈值自适应——冷启动静态阈值消费校准阈值。"""
        clock = _clock_fixed()
        am = _FakeAlert()
        met = _FakeMetrics()
        cal = ThresholdCalibrator(method="zscore", k=3.0, min_samples=5,
                                  apply_mode="auto", clock=clock)
        db = DynamicBaseline(window=10, k=3.0, min_samples=5, yoy_lag=2,
                             clock=clock, alert_manager=am,
                             metrics_collector=met, calibrator=cal)
        for k in range(6):
            db.record("s1", float(k * 100), 100.0 + (k % 3))
        db.set_regime("trend_up")
        for k in range(6, 12):
            db.record("s1", float(k * 100), 100.0 + (k % 3))  # 带小幅波动
        db.set_regime("range")
        db.set_regime("trend_up")  # auto 生效
        bounds = cal.resolve("s1", "trend_up")
        assert bounds is not None
        # 冷启动（trend_up 样本不足 min_samples）→ 静态阈值 = 校准阈值
        assert db._resolve_static("s1") == bounds

    def test_manual_override_affects_anomaly_judgement(self) -> None:
        """人工覆盖优先：覆盖阈值后冷启动点超出即告警。"""
        clock = _clock_fixed()
        am = _FakeAlert()
        met = _FakeMetrics()
        cal = ThresholdCalibrator(min_samples=5, apply_mode="auto", clock=clock)
        db = DynamicBaseline(window=10, k=3.0, min_samples=5, yoy_lag=2,
                             clock=clock, alert_manager=am,
                             metrics_collector=met, calibrator=cal)
        cal.set_manual_thresholds("s1", "default", 0.0, 10.0,
                                  actor="trader", reason="人工覆盖")
        # 冷启动期（<min_samples）→ 静态阈值=人工覆盖 (0,10)
        event = db.record("s1", 100.0, 50.0)
        assert event is not None  # 50 > 10 → 异常
        assert am.calls  # 告警已发出

    def test_calibrator_summary_in_to_dict(self) -> None:
        clock = _clock_fixed()
        cal = ThresholdCalibrator(min_samples=5, clock=clock)
        db = DynamicBaseline(window=10, clock=clock, calibrator=cal)
        snap = db.to_dict()
        assert snap["calibrator"] is not None
        assert snap["calibrator"]["method"] == "zscore"
        # 未挂接校准器 → None
        db2 = DynamicBaseline(window=10, clock=clock)
        assert db2.to_dict()["calibrator"] is None

    def test_without_calibrator_behavior_unchanged(self) -> None:
        """未挂接校准器：静态阈值仍走注入（回归保护）。"""
        clock = _clock_fixed()
        db = DynamicBaseline(
            window=10, k=3.0, min_samples=5, yoy_lag=2, clock=clock,
            static_thresholds={"s1": (0.0, 10.0)},
        )
        assert db._resolve_static("s1") == (0.0, 10.0)
        event = db.record("s1", 100.0, 99.0)
        assert event is not None
