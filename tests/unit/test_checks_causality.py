"""P0-08 子项⑤反红测试：checks/causality.py 因果切片真实性检查（R-19 + 用例⑫②闭环）。

事实来源：施工方案 v2.2 §5.4.1 子项⑤（L738）+ P0-08 卡验收用例⑬/⑫②；蓝图目录树
checks/ 行 #334 前置登记；被测：myquant/realism_gate/checks/causality.py（新增）。

判据：``causality.context_sliced = False`` 且未声明全量逃生口
（``full_sample_declared = False``）→ critical 违规（接入 _collect_violations）。

用例⑫②闭环：子项④（#338）fail-open 降级写 context_sliced=false → 本检查器判 critical。
"""
from __future__ import annotations

import pytest

from myquant.realism_gate.checks.causality import check_causality
from myquant.realism_gate.gate import (
    RealismGateConfig,
    _collect_violations,
    assert_realism_pass,
    get_config,
    set_config,
)
from myquant.realism_gate.manifest import (
    CRITICAL_FIELDS,
    ENUM_DOMAINS,
    RealismGateError,
    RealismManifest,
)
from myquant.realism_gate.checks import causality as _causality_imported  # noqa: F401 导入即注册

# 越界秒级时间戳（与 test_slicing_failopen_guard 同口径）：
# datetime.fromtimestamp 与 pd.Timestamp(unit='s') 均抛 OverflowError
BAD_TIMESTAMP: float = 1e20


def _make_manifest(context_sliced: bool) -> RealismManifest:
    """构造 causality 组最小 manifest（纯函数单测用）。"""
    manifest = RealismManifest()
    manifest.update_group(
        "causality",
        {
            "context_sliced": context_sliced,
            "current_bar_ts_enforced": False,
            "full_sample_declared": False,
            "guard_injected": False,
        },
    )
    return manifest


def _make_full_manifest(context_sliced: bool) -> RealismManifest:
    """构造 CRITICAL_FIELDS 齐全、其余检查不触发的完整 manifest（阻断闭环用）。"""
    manifest = RealismManifest()
    for path in CRITICAL_FIELDS:
        if path in ENUM_DOMAINS:
            manifest.set_field(path, ENUM_DOMAINS[path][0])
        elif path == "causality.context_sliced":
            manifest.set_field(path, context_sliced)
        elif path in (
            "causality.current_bar_ts_enforced",
            "causality.guard_injected",
            "causality.context_data_scoped",
        ):
            manifest.set_field(path, False)
        elif path == "causality.full_sample_declared":
            manifest.set_field(path, False)
        elif path in ("quality.nan_price_rows", "quality.ohlc_violation_rows"):
            manifest.set_field(path, 0)
        elif path in ("pool.requested_count", "pool.admitted_count"):
            manifest.set_field(path, 1)
        elif path == "pool.dropped_count":
            manifest.set_field(path, 0)
        elif path == "pool.drop_reasons":
            manifest.set_field(path, [])
        elif path == "adjust_degraded_symbols":
            manifest.set_field(path, [])
        elif path in ("commission.buy_rate", "commission.sell_rate"):
            manifest.set_field(path, 0.0003)
        elif path == "slippage.is_zero_cost":
            manifest.set_field(path, False)
        else:
            manifest.set_field(path, "test")
    return manifest


# ═══════════════════════════════════════════════
#  纯函数单测（check_causality）
# ═══════════════════════════════════════════════


def test_context_sliced_false_no_full_sample_is_critical() -> None:
    """反红：未封门（False）且未声明全量 → 1 条 critical 违规。"""
    manifest = _make_manifest(context_sliced=False)
    violations = check_causality(manifest)
    assert len(violations) == 1
    assert violations[0].critical is True
    assert violations[0].field_path == "causality.context_sliced"


def test_context_sliced_true_no_violation() -> None:
    """不误伤：切片已生效（True）→ 无违规。"""
    manifest = _make_manifest(context_sliced=True)
    assert check_causality(manifest) == []


def test_full_sample_declared_escape_hatch_no_violation() -> None:
    """不误伤：未封门但声明全量逃生口（vectorized）→ 无违规。"""
    manifest = _make_manifest(context_sliced=False)
    manifest.set_field("causality.full_sample_declared", True)
    assert check_causality(manifest) == []


def test_missing_context_sliced_no_violation() -> None:
    """字段缺失（None）不在此触发——缺失由 manifest.build() fail-closed 处理。"""
    manifest = RealismManifest()
    manifest.set_field("causality.full_sample_declared", False)
    assert check_causality(manifest) == []


# ═══════════════════════════════════════════════
#  注册集成 + 用例⑫②闭环
# ═══════════════════════════════════════════════


def test_registered_and_collected() -> None:
    """接入 _collect_violations：注册后整体收集含 causality 违规。"""
    manifest = _make_manifest(context_sliced=False)
    all_v = _collect_violations(manifest)
    causality_v = [v for v in all_v if v.field_path == "causality.context_sliced"]
    assert len(causality_v) == 1
    assert causality_v[0].critical is True


def test_failopen_degradation_produces_critical() -> None:
    """用例⑫②闭环：子项④ fail-open 降级（#338）→ 检查器判 critical 违规。"""
    from myquant.backtest.engine import BacktestEngine
    from myquant.models.market_data import Bar
    from myquant.strategies.base import StrategyContext

    engine = BacktestEngine(config=None)
    engine.context = StrategyContext()
    engine._realism_manifest = _make_manifest(context_sliced=True)
    engine.on_market_event(Bar(timestamp=BAD_TIMESTAMP))
    # 子项④显式降级：保持未封门 + 写 context_sliced=false
    assert engine.context._slicing_enabled is False
    assert engine._realism_manifest.get_field("causality.context_sliced") is False
    # 子项⑤：false 且未声明全量 → critical 违规（接入 _collect_violations）
    all_v = _collect_violations(engine._realism_manifest)
    causality_v = [v for v in all_v if v.field_path == "causality.context_sliced"]
    assert len(causality_v) == 1
    assert causality_v[0].critical is True


# ═══════════════════════════════════════════════
#  出口阻断闭环（assert_realism_pass，enabled=True）
# ═══════════════════════════════════════════════


def test_assert_realism_pass_blocks_when_enabled() -> None:
    """enabled=True 且 context_sliced=false → 出口断言抛 RealismGateError（阻断）。"""
    previous = get_config()
    try:
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        manifest = _make_full_manifest(context_sliced=False)
        with pytest.raises(RealismGateError) as exc_info:
            assert_realism_pass(manifest)
        field_paths = [v.field_path for v in exc_info.value.violations]
        assert "causality.context_sliced" in field_paths
    finally:
        set_config(previous)


def test_assert_realism_pass_passes_when_sliced() -> None:
    """enabled=True 且 context_sliced=true → 出口断言通过（封印，不误伤）。"""
    previous = get_config()
    try:
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        manifest = _make_full_manifest(context_sliced=True)
        assert_realism_pass(manifest)
        assert manifest.sealed is True
    finally:
        set_config(previous)
