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
    """构造 causality 组最小 manifest（纯函数单测用，运行期形态）。"""
    manifest = RealismManifest()
    manifest.update_group(
        "causality",
        {
            "context_sliced": context_sliced,
            "current_bar_ts_enforced": False,
            "full_sample_declared": False,
            "guard_injected": False,
            # P0-01（#344）：运行期真实采集已完成 → 判据生效
            "assessed_at_run_end": True,
        },
    )
    return manifest


def _make_entry_manifest() -> RealismManifest:
    """构造 create_engine 入口形态 causality 组（引擎未运行，诚实占位）。"""
    manifest = RealismManifest()
    manifest.update_group(
        "causality",
        {
            "context_sliced": False,
            "current_bar_ts_enforced": False,
            "full_sample_declared": False,
            "guard_injected": False,
            "context_data_scoped": True,
            # P0-01（#344）：引擎未运行 → 判据门控关闭（入口不误伤）
            "assessed_at_run_end": False,
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
        elif path == "causality.assessed_at_run_end":
            # P0-01（#344）：运行期形态（阻断闭环判据需生效）
            manifest.set_field(path, True)
        elif path == "causality.full_sample_declared":
            manifest.set_field(path, False)
        elif path in ("quality.nan_price_rows", "quality.ohlc_violation_rows"):
            manifest.set_field(path, 0)
        elif path == "quality.coverage_rate":
            # P1-13（#363）：覆盖率判据（<0.5 → critical）新增后喂合法值防误伤
            manifest.set_field(path, 1.0)
        elif path in ("data.snapshot_applicable", "data.snapshot_mismatch"):
            # P1-12（#364）：默认不适用目录快照（data_pit 跳过指纹判据）、无不一致
            manifest.set_field(path, False)
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


# ═══════════════════════════════════════════════
#  子项⑥：访问器层记账（P1-07② 判据 + 用例⑭交叉校验）
# ═══════════════════════════════════════════════

import pandas as pd  # noqa: E402  记账用例用

from myquant.strategies.base import StrategyContext  # noqa: E402

_DATA_IDX = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
_DATA_DF = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=_DATA_IDX)
_SYMBOL = "000001.SZ"


def _make_context(enable_slicing: bool) -> StrategyContext:
    """构造带数据的记账测试上下文。"""
    context = StrategyContext()
    context.data = {_SYMBOL: _DATA_DF}
    context._slicing_enabled = enable_slicing
    return context


def test_accounting_detects_future_read() -> None:
    """真拦截：未封门路径 get_data 全量（含未来行）→ accessed_future → critical。"""
    context = _make_context(enable_slicing=False)
    context.current_bar_ts = _DATA_IDX[1]  # 当前 bar 01-02，数据含 01-03/01-04 未来
    context.get_data(_SYMBOL)
    acct = context.collect_access_accounting()
    assert acct["accessed_future"] is True
    assert len(acct["violations"]) == 1
    assert acct["violations"][0]["symbol"] == _SYMBOL
    # manifest 层 → 检查器判 critical
    manifest = _make_manifest(context_sliced=False)
    manifest.update_group(
        "causality",
        {
            "accessed_future": acct["accessed_future"],
            "static_declared_but_accessed_future": acct["static_declared_but_accessed_future"],
            "accounting_violations": acct["violations"],
        },
    )
    v = [x for x in check_causality(manifest) if x.field_path == "causality.accessed_future"]
    assert len(v) == 1
    assert v[0].critical is True


def test_accounting_cross_check_violation() -> None:
    """用例⑭：静态声明已封门（context_sliced=true）却实测读到未来 → 交叉校验报错。"""
    context = _make_context(enable_slicing=True)
    context.current_bar_ts = _DATA_IDX[1]
    # 模拟引擎侧残留通道访问：未封门记账（直接注入实测记录，声明与实测矛盾）
    context.accessed_max_dates[_SYMBOL] = _DATA_IDX[3]
    acct = context.collect_access_accounting()
    assert acct["accessed_future"] is True
    assert acct["static_declared_but_accessed_future"] is True
    manifest = _make_manifest(context_sliced=True)  # 静态声明 true
    manifest.update_group("causality", {"static_declared_but_accessed_future": True})
    v = [
        x for x in check_causality(manifest)
        if x.field_path == "causality.static_declared_but_accessed_future"
    ]
    assert len(v) == 1
    assert v[0].critical is True


def test_accounting_no_violation_when_sliced() -> None:
    """不误伤：已封门路径 get_data 切片 → 不记账、不违规。"""
    context = _make_context(enable_slicing=True)
    context.current_bar_ts = _DATA_IDX[3]
    context.get_data(_SYMBOL)
    assert _SYMBOL not in context.accessed_max_dates  # 已封门不记账
    acct = context.collect_access_accounting()
    assert acct["accessed_future"] is False
    assert acct["static_declared_but_accessed_future"] is False


def test_accounting_no_violation_on_init() -> None:
    """不误伤：on_init 阶段（current_bar_ts 未设）get_data 全量 → 记账但不判违规。"""
    context = _make_context(enable_slicing=False)
    context.current_bar_ts = None
    context.get_data(_SYMBOL)
    assert _SYMBOL in context.accessed_max_dates  # 未封门记账（on_init 放行窗口）
    acct = context.collect_access_accounting()
    assert acct["accessed_future"] is False  # 无当前 bar → 不判


def test_accounting_escape_hatch_declared_no_violation() -> None:
    """不误伤：vectorized 逃生口（full_sample_declared=True）→ 记账但不判违规。"""
    context = StrategyContext(_full_sample_declared=True)
    context.data = {_SYMBOL: _DATA_DF}
    context.get_data(_SYMBOL)
    assert _SYMBOL in context.accessed_max_dates  # 全量声明路径记账
    acct = context.collect_access_accounting()
    assert acct["accessed_future"] is False  # current_bar_ts 未设 → 不判


def test_engine_refresh_writes_accounting_to_manifest() -> None:
    """引擎驱动：未封门读未来 → run 收尾刷新把 accessed_future 写进 manifest。"""
    from myquant.backtest.engine import BacktestEngine

    engine = BacktestEngine(config=None)
    engine.context = _make_context(enable_slicing=False)
    engine.context.current_bar_ts = _DATA_IDX[1]
    engine.context.get_data(_SYMBOL)  # 全量 → 记账
    engine._realism_manifest = _make_manifest(context_sliced=False)
    engine._refresh_causality_manifest()
    m = engine._realism_manifest
    assert m.get_field("causality.accessed_future") is True
    acct_v = m.get_field("causality.accounting_violations", [])
    assert isinstance(acct_v, list) and len(acct_v) == 1
    # 检查器判 critical（接入 _collect_violations 的完整链路）
    all_v = _collect_violations(m)
    assert any(x.field_path == "causality.accessed_future" and x.critical for x in all_v)


# ═══════════════════════════════════════════════
#  P0-01 因果性判据正式生效点（#344）：运行期门控 + 两阶段握手第⑤步
# ═══════════════════════════════════════════════

def test_entry_manifest_not_judged_before_run() -> None:
    """P0-01：引擎未运行（入口占位 assessed=False）→ causality 判据不参与判定。"""
    manifest = _make_entry_manifest()
    assert check_causality(manifest) == []  # 门控关闭


def test_entry_assert_realism_pass_seals_when_enabled() -> None:
    """P0-01：enabled=True + 入口形态完整 manifest → 通过并 seal（不误伤 create_engine）。"""
    previous = get_config()
    try:
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        manifest = _make_full_manifest(context_sliced=False)
        manifest.update_group("causality", {"assessed_at_run_end": False})  # 入口形态
        assert_realism_pass(manifest)
        assert manifest.sealed is True
    finally:
        set_config(previous)


def test_handshake_step5_run_requires_sealed_when_enabled() -> None:
    """P0-01 两阶段握手第⑤步：enabled=True + 引擎未封印 → run 入口阻断。"""
    from myquant.backtest.engine import BacktestEngine
    from myquant.realism_gate.manifest import RealismGateError

    previous = get_config()
    try:
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        engine = BacktestEngine(config=None)
        engine._initialized = True  # 跳过 on_init，直接测入口断言路径
        engine._realism_manifest = _make_entry_manifest()  # 未 seal
        with pytest.raises(RealismGateError):
            engine.run()
    finally:
        set_config(previous)


def test_handshake_step5_run_passes_when_sealed() -> None:
    """P0-01 两阶段握手第⑤步：封印后 run 入口放行（enabled=True）。"""
    from myquant.backtest.engine import BacktestEngine

    previous = get_config()
    try:
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        engine = BacktestEngine(config=None)
        engine._initialized = True  # 跳过 on_init（直接测入口断言路径）
        engine._realism_manifest = _make_entry_manifest()
        engine._realism_manifest.seal()
        # 无 data → 入口断言通过后走 fail 路径（不 raise 门禁错误）
        result = engine.run()
        assert result.status == "failed"
    finally:
        set_config(previous)


def test_handshake_step5_run_audit_mode_not_blocked() -> None:
    """P0-01 两阶段握手第⑤步：enabled=False（审计档）未封印 → 不阻断。"""
    from myquant.backtest.engine import BacktestEngine

    previous = get_config()
    try:
        set_config(RealismGateConfig(enabled=False, audit_violations=False))
        engine = BacktestEngine(config=None)
        engine._initialized = True  # 跳过 on_init（直接测入口断言路径）
        engine._realism_manifest = _make_entry_manifest()  # 未 seal
        result = engine.run()
        assert result.status == "failed"  # 入口断言放行（审计档）
    finally:
        set_config(previous)
