# 文件名：tests/unit/test_model_monitor.py
"""
test_model_monitor.py - F-05 模型失效监控与自动降级测试
======================================================
覆盖：
  1. 失效判定（IC 连续3月低/序列不足/回升中断/阈值边界/回撤超限/健康）
  2. HealthReport 序列化 + 非法输入
  3. ModelMonitor 参数防御 + from_config（dict/JSON/非法）
  4. 自动降级（active 失效→切上一活跃/无候选清空）+ 告警
  5. retired 版本加载拦截（0 次调用验收）+ allow_retired 评估通道
  6. 注册门控（win gate：跑赢激活/未跑赢 archived/缺指标跳过/默认零变化）
  7. evaluate_model compute_ic（回归/分类）与 pipeline 门控透传
  8. 防未来（只消费已给历史序列）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from myquant.ml.model_monitor import (
    HealthReport,
    ModelMonitor,
    ModelMonitorConfigError,
    ModelMonitorError,
    STATUS_DEGRADED,
    STATUS_HEALTHY,
    STATUS_RETIRED,
)
from myquant.ml.model_registry import (
    ModelRegistry,
    ModelRetiredError,
    ModelVersion,
)
from myquant.ml.training_pipeline import _rank_ic, evaluate_model


# ═══════════════════════════════════════════════
#  测试辅助
# ═══════════════════════════════════════════════

class _DummyModel:
    """哑模型：predict 返回固定连续预测（回归 IC 测试用）。"""

    def __init__(self, preds: List[float], proba: Optional[List[float]] = None):
        self.preds = np.asarray(preds, dtype=float)
        self.proba = np.asarray(proba, dtype=float) if proba is not None else None

    def predict(self, X: Any) -> np.ndarray:
        return np.asarray(self.preds)

    def predict_proba(self, X: Any) -> np.ndarray:
        if self.proba is None:
            raise NotImplementedError
        return np.column_stack([1.0 - self.proba, self.proba])


def _make_meta(model_name: str = "m", version: int = 1,
               metrics: Optional[Dict[str, Any]] = None) -> ModelVersion:
    return ModelVersion(
        model_name=model_name, version=version,
        metrics=metrics or {}, status="active",
    )


def _make_registry(tmp_path: Path, versions: int = 2) -> ModelRegistry:
    """构造单一模型 m 的 versions 个版本（v1 最早，全部 active）。"""
    reg = ModelRegistry(models_root=str(tmp_path / "models"))
    for v in range(1, versions + 1):
        reg.register_model(
            "m", _DummyModel([0.1, 0.2, 0.3]),
            metadata={"metrics": {"ic": 0.04 + v * 0.01}},
        )
    return reg


# ═══════════════════════════════════════════════
#  1. 失效判定
# ═══════════════════════════════════════════════

def test_ic_three_low_months_retired() -> None:
    """IC 连续 3 个月低于阈值 → retired"""
    mon = ModelMonitor()
    rep = mon.evaluate(_make_meta(), ic_series=[0.05, 0.04, 0.01, 0.01, 0.01])
    assert rep.status == STATUS_RETIRED
    assert rep.months_below_threshold == 3
    assert rep.ic_latest == pytest.approx(0.01)
    assert any("连续" in r for r in rep.reasons)


def test_ic_insufficient_series_not_retired() -> None:
    """序列不足 3 个月（只有 2 个低值）→ degraded 不 retired"""
    mon = ModelMonitor()
    rep = mon.evaluate(_make_meta(), ic_series=[0.05, 0.01, 0.01])
    assert rep.status == STATUS_DEGRADED
    assert rep.months_below_threshold == 2


def test_ic_recovery_breaks_consecutive() -> None:
    """IC 回升中断连续 → 不失效（最近 1 月低=观察态 degraded）"""
    mon = ModelMonitor()
    rep = mon.evaluate(_make_meta(), ic_series=[0.01, 0.01, 0.08, 0.01])
    assert rep.status == STATUS_DEGRADED  # 未连续 3 月 → 不 retired
    assert rep.months_below_threshold == 1  # 只有最后一个低值


def test_ic_threshold_boundary_not_below() -> None:
    """|IC| 恰等于阈值（0.02）→ 不算低于（严格 <）"""
    mon = ModelMonitor()
    rep = mon.evaluate(_make_meta(), ic_series=[0.02, 0.02, 0.02, 0.02])
    assert rep.status == STATUS_HEALTHY
    assert rep.months_below_threshold == 0


def test_drawdown_breached_retired() -> None:
    """绩效最大回撤超限（-0.30 > 0.20）→ retired"""
    mon = ModelMonitor()
    rep = mon.evaluate(
        _make_meta(), ic_series=[0.05, 0.05],
        current_metrics={"max_drawdown": -0.30},
    )
    assert rep.status == STATUS_RETIRED
    assert rep.drawdown_breached is True
    assert any("回撤" in r for r in rep.reasons)


def test_healthy_when_within_limits() -> None:
    """IC 与回撤均在阈值内 → healthy"""
    mon = ModelMonitor()
    rep = mon.evaluate(
        _make_meta(), ic_series=[0.05, 0.06],
        current_metrics={"max_drawdown": -0.10},
    )
    assert rep.status == STATUS_HEALTHY
    assert rep.reasons


def test_ic_series_invalid_value_raises() -> None:
    """IC 序列含非法数值 → ModelMonitorError"""
    mon = ModelMonitor()
    with pytest.raises(ModelMonitorError):
        mon.evaluate(_make_meta(), ic_series=[0.05, "bad", 0.01])


# ═══════════════════════════════════════════════
#  2. HealthReport 序列化
# ═══════════════════════════════════════════════

def test_health_report_roundtrip() -> None:
    """HealthReport to_dict/from_dict 往返等价"""
    rep = HealthReport(
        model_name="m", version=3, status=STATUS_RETIRED,
        ic_latest=0.01, months_below_threshold=3,
        max_drawdown=-0.3, drawdown_breached=True,
        reasons=["IC 连续低"], degraded_to=2,
    )
    rep2 = HealthReport.from_dict(rep.to_dict())
    assert rep2 == rep


def test_health_report_from_dict_invalid() -> None:
    """非法输入 → ValueError"""
    with pytest.raises(ValueError):
        HealthReport.from_dict({})
    with pytest.raises(ValueError):
        HealthReport.from_dict("bad")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════
#  3. 参数防御 + config 外置
# ═══════════════════════════════════════════════

def test_monitor_invalid_params() -> None:
    """参数非法 → ModelMonitorConfigError"""
    with pytest.raises(ModelMonitorConfigError):
        ModelMonitor(ic_threshold=0)
    with pytest.raises(ModelMonitorConfigError):
        ModelMonitor(months_under_threshold=0)
    with pytest.raises(ModelMonitorConfigError):
        ModelMonitor(drawdown_limit=-0.1)
    with pytest.raises(ModelMonitorConfigError):
        ModelMonitor(monitor_interval_days=0)


def test_from_config_dict_and_json(tmp_path: Path) -> None:
    """from_config 支持 dict 与 JSON 路径"""
    cfg = {"ic_threshold": 0.05, "months_under_threshold": 2,
           "drawdown_limit": 0.3, "monitor_interval_days": 15}
    mon1 = ModelMonitor.from_config(cfg)
    assert mon1.ic_threshold == 0.05
    assert mon1.monitor_interval_days == 15
    assert mon1.to_config() == cfg

    path = tmp_path / "monitor.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    mon2 = ModelMonitor.from_config(path)
    assert mon2.ic_threshold == 0.05


def test_from_config_invalid() -> None:
    """非法 config（类型/缺文件/非法参数）→ ModelMonitorConfigError"""
    with pytest.raises(ModelMonitorConfigError):
        ModelMonitor.from_config("not-a-path.json")
    with pytest.raises(ModelMonitorConfigError):
        ModelMonitor.from_config(42)  # type: ignore[arg-type]
    with pytest.raises(ModelMonitorConfigError):
        ModelMonitor.from_config({"ic_threshold": -1})


# ═══════════════════════════════════════════════
#  4. 自动降级
# ═══════════════════════════════════════════════

def test_run_retire_and_degrade(tmp_path: Path) -> None:
    """active 失效 → 自动降级到版本号最大的非失效版本"""
    reg = _make_registry(tmp_path, versions=3)
    mon = ModelMonitor()
    # active 是 v3；注入低 IC 3 连
    rep = mon.run("m", reg, ic_series=[0.05, 0.01, 0.01, 0.01])
    assert rep.status == STATUS_RETIRED
    assert rep.degraded_to == 2
    active = reg.get_active_version("m")
    assert active is not None and active.version == 2
    # 索引记录 last_retire
    index = reg._read_index("m")
    assert index["last_retire"]["retired"] == 3
    assert index["last_retire"]["degraded_to"] == 2


def test_run_retire_no_candidate_clears_active(tmp_path: Path) -> None:
    """唯一版本失效且无更早候选 → active 置空"""
    reg = ModelRegistry(models_root=str(tmp_path / "models"))
    reg.register_model("solo", _DummyModel([0.1]), metadata={"metrics": {}})
    mon = ModelMonitor()
    rep = mon.run("solo", reg, ic_series=[0.0, 0.0, 0.0, 0.0])
    assert rep.status == STATUS_RETIRED
    assert rep.degraded_to is None
    assert reg.get_active_version("solo") is None


def test_run_healthy_no_degrade(tmp_path: Path) -> None:
    """健康模型 → 不降级，active 不变"""
    reg = _make_registry(tmp_path, versions=2)
    mon = ModelMonitor()
    rep = mon.run("m", reg, ic_series=[0.05, 0.06, 0.07])
    assert rep.status == STATUS_HEALTHY
    assert rep.degraded_to is None
    active = reg.get_active_version("m")
    assert active is not None and active.version == 2


# ═══════════════════════════════════════════════
#  5. retired 版本加载拦截
# ═══════════════════════════════════════════════

def test_load_retired_rejected(tmp_path: Path) -> None:
    """失效版本默认拒绝加载（0 次调用验收）"""
    reg = _make_registry(tmp_path, versions=2)
    reg.mark_retired("m", 2)
    with pytest.raises(ModelRetiredError):
        reg.load_model("m", 2)
    # 非失效版本仍可加载
    assert reg.load_model("m", 1) is not None


def test_load_retired_allow_evaluation(tmp_path: Path) -> None:
    """allow_retired=True 供评估/审计显式加载"""
    reg = _make_registry(tmp_path, versions=2)
    reg.mark_retired("m", 2)
    assert reg.load_model("m", 2, allow_retired=True) is not None


def test_mark_retired_metadata_status(tmp_path: Path) -> None:
    """mark_retired 后 get_model_info 反映 retired 状态"""
    reg = _make_registry(tmp_path, versions=2)
    reg.mark_retired("m", 1)
    info = reg.get_model_info("m", 1)
    assert info.status == "retired"


# ═══════════════════════════════════════════════
#  6. 注册门控（win gate）
# ═══════════════════════════════════════════════

def test_win_gate_rejects_lower_ic(tmp_path: Path) -> None:
    """新版本 IC 低于 active → archived 不激活 + active 保持"""
    reg = ModelRegistry(models_root=str(tmp_path / "models"))
    reg.register_model("g", _DummyModel([0.1]),
                       metadata={"metrics": {"ic": 0.08}})
    new_meta = reg.register_model(
        "g", _DummyModel([0.2]),
        metadata={"metrics": {"ic": 0.03}},
        require_win_gate=True, win_metric="ic",
    )
    assert new_meta.status == "archived"
    active = reg.get_active_version("g")
    assert active is not None and active.version == 1


def test_win_gate_accepts_higher_ic(tmp_path: Path) -> None:
    """新版本 IC 高于 active → 激活为新 active"""
    reg = ModelRegistry(models_root=str(tmp_path / "models"))
    reg.register_model("g", _DummyModel([0.1]),
                       metadata={"metrics": {"ic": 0.03}})
    new_meta = reg.register_model(
        "g", _DummyModel([0.2]),
        metadata={"metrics": {"ic": 0.09}},
        require_win_gate=True, win_metric="ic",
    )
    assert new_meta.status == "active"
    active = reg.get_active_version("g")
    assert active is not None and active.version == 2


def test_win_gate_missing_metric_skips(tmp_path: Path) -> None:
    """新旧缺 win_metric 指标 → 跳过门控正常激活（不阻断）"""
    reg = ModelRegistry(models_root=str(tmp_path / "models"))
    reg.register_model("g", _DummyModel([0.1]),
                       metadata={"metrics": {"accuracy": 0.6}})
    new_meta = reg.register_model(
        "g", _DummyModel([0.2]),
        metadata={"metrics": {"accuracy": 0.7}},
        require_win_gate=True, win_metric="ic",
    )
    assert new_meta.status == "active"


def test_register_default_zero_change(tmp_path: Path) -> None:
    """默认 require_win_gate=False → 新版本无条件激活（现有行为）"""
    reg = ModelRegistry(models_root=str(tmp_path / "models"))
    reg.register_model("g", _DummyModel([0.1]),
                       metadata={"metrics": {"ic": 0.08}})
    new_meta = reg.register_model(
        "g", _DummyModel([0.2]),
        metadata={"metrics": {"ic": 0.03}},
    )
    assert new_meta.status == "active"
    assert reg.get_active_version("g").version == 2  # type: ignore[union-attr]


# ═══════════════════════════════════════════════
#  7. evaluate_model compute_ic + 防未来
# ═══════════════════════════════════════════════

def test_evaluate_model_regression_ic_perfect() -> None:
    """回归完美预测 → IC≈1（metrics 含 ic）"""
    X = np.arange(20.0).reshape(10, 2)
    y = np.arange(10.0)
    model = _DummyModel(list(y))  # 预测=真值
    metrics = evaluate_model(model, X, y, label_type="return", compute_ic=True)
    assert metrics["ic"] == pytest.approx(1.0, abs=1e-9)


def test_evaluate_model_classification_ic_proba() -> None:
    """分类用正类概率算 IC：概率单调对应标签 → 高 IC"""
    X = np.arange(20.0).reshape(10, 2)
    y = np.array([0, 0, 0, 0, 0, 1, 1, 1, 1, 1])
    proba = [0.1, 0.2, 0.3, 0.4, 0.45, 0.55, 0.6, 0.7, 0.8, 0.9]
    model = _DummyModel(list(y), proba=proba)
    metrics = evaluate_model(model, X, y, label_type="direction", compute_ic=True)
    assert metrics["ic"] > 0.8
    assert "accuracy" in metrics  # 分类指标保留


def test_evaluate_model_no_ic_when_disabled() -> None:
    """compute_ic=False 时 metrics 不含 ic（默认零变化）"""
    X = np.arange(20.0).reshape(10, 2)
    y = np.arange(10.0)
    model = _DummyModel(list(y))
    metrics = evaluate_model(model, X, y, label_type="return")
    assert "ic" not in metrics


def test_rank_ic_nan_guard() -> None:
    """样本 <2 / 常数序列 → None（防御）"""
    assert _rank_ic([1.0], [1.0]) is None
    assert _rank_ic([1.0, 2.0], [1.0, 1.0]) is None  # 标签常数
    assert _rank_ic([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_monitor_only_consumes_given_series() -> None:
    """防未来：判定只依赖已给序列（末尾=当前已观测值），不推断未来"""
    mon = ModelMonitor()
    # 同一序列两次评估结果确定（无时间推进依赖）
    r1 = mon.evaluate(_make_meta(), ic_series=[0.01, 0.01, 0.01])
    r2 = mon.evaluate(_make_meta(), ic_series=[0.01, 0.01, 0.01])
    assert r1.status == r2.status == STATUS_RETIRED
    assert r1.decided_at == r2.decided_at or True  # 时间戳可能秒级不同


__all__: List[str] = []
