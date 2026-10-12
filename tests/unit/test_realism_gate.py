# 文件名：tests/unit/test_realism_gate.py
"""
tests/unit/test_realism_gate.py - 门禁中心出口断言测试（P0-01 第二刀）
=====================================================================

覆盖：
  - R-19 零覆盖收录（realism_gate.gate 被 tests import）
  - RealismGateConfig 默认值（enabled=False 起步，分期启用期）
  - register_realism_check 注册/重复注册/空名防御
  - assert_realism_pass 端到端：
    * 完整 manifest 无违规 → seal() 通过
    * critical 违规 + enabled=True → 抛 RealismGateError
    * critical 违规 + enabled=False → 不抛（audit 档）
    * 缺 critical 字段 → ManifestIncompleteError（fail-closed）
    * 检查函数自身异常 → fail-closed（记 gate-error 违规）
  - reset_realism_checks 测试隔离
"""

import pytest

from myquant.realism_gate import (
    ManifestIncompleteError,
    RealismGateConfig,
    RealismGateError,
    RealismManifest,
    RealismViolation,
    assert_realism_pass,
    get_config,
    register_realism_check,
    reset_realism_checks,
    set_config,
)


@pytest.fixture(autouse=True)
def _reset_gate_state():
    """每个测试前后清空检查注册表 + 恢复默认配置（测试隔离）。"""
    reset_realism_checks()
    set_config(RealismGateConfig(enabled=False, audit_violations=False))  # 测试不落审计
    yield
    reset_realism_checks()
    set_config(RealismGateConfig())


def _fill_minimal(m: RealismManifest) -> None:
    """填入所有 critical 字段的最小合法值（与 test_realism_manifest 同构）。"""
    m.set_field("run_id", "run-test")
    m.set_field("strategy_id", "rsrs")
    m.set_field("engine_mode_requested", "auto")
    m.set_field("engine_mode_resolved", "mixed")
    m.set_field("start_date", "2024-01-01")
    m.set_field("end_date", "2024-02-28")
    m.set_field("initial_capital", 1_000_000.0)
    m.update_group("causality", {
        "context_sliced": False, "current_bar_ts_enforced": False,
        "full_sample_declared": False, "guard_injected": True,
        "context_data_scoped": False,
        # P0-01（#344）：运行期形态（本 helper 无检查器注册，build 完整性通过即可）
        "assessed_at_run_end": True,
    })
    m.update_group("price_limit", {"enabled": True, "pct_by_board": {}, "pre_close_source": "data_col"})
    m.update_group("suspension", {"enabled": True, "basis": "volume_zero"})
    m.set_field("t1_position_freeze.enabled", True)
    m.update_group("lot_size", {"enforced": True, "value": 100})
    m.update_group("liquidity", {"participation_cap": None, "declared_unlimited": True})
    m.update_group("slippage", {"type": "fixed", "value": 0.001, "is_zero_cost": False})
    m.update_group("commission", {"buy_rate": 0.0003, "sell_rate": 0.0003,
                                   "min_commission": 5.0, "stamp_tax": 0.001, "transfer_fee": 0.00002})
    m.set_field("fee_base_includes_slippage", False)
    m.set_field("fee_dilution_basis", "per_order")
    m.set_field("adjust_effective_by_symbol", {"600519.SH": "hfq"})
    m.set_field("adjust_degraded_symbols", [])
    m.update_group("quality", {"nan_price_rows": 0, "ohlc_violation_rows": 0, "coverage_rate": 1.0})
    m.set_field("fingerprint_hash", "abc123")
    m.set_field("snapshot_id", "snap-001")
    m.update_group("pool", {"pit_applied": True, "exclusion_basis": "as_of",
                            "requested_count": 50, "admitted_count": 50,
                            "dropped_count": 0, "drop_reasons": {}})
    m.update_group("survivorship", {"discount": None, "window": "1y", "status": "unknown"})
    m.update_group("invariants", {"price_bounds": {}, "volume_bound": {}, "equity_identity": {},
                                    "cash_non_negative": {}, "t1_consistency": {}})


class TestGateImportable:
    """R-19 收录。"""

    def test_importable(self):
        from myquant.realism_gate.gate import (
            RealismGateConfig, assert_realism_pass, register_realism_check,
            reset_realism_checks, get_config, set_config,
        )
        assert callable(assert_realism_pass)
        assert callable(register_realism_check)


class TestChecksImportable:
    """R-19 收录：四个 checks 模块被 tests import 即视为覆盖。"""

    def test_market_rules_importable(self):
        from myquant.realism_gate.checks.market_rules import check_market_rules
        assert callable(check_market_rules)

    def test_cost_model_importable(self):
        from myquant.realism_gate.checks.cost_model import check_cost_model
        assert callable(check_cost_model)

    def test_data_pit_importable(self):
        from myquant.realism_gate.checks.data_pit import check_data_pit
        assert callable(check_data_pit)

    def test_sample_pool_importable(self):
        from myquant.realism_gate.checks.sample_pool import check_sample_pool
        assert callable(check_sample_pool)

    def test_checks_registration_on_import(self):
        """四检查器函数可调用（注册在模块导入时完成；此处验证函数本身可导入）。"""
        from myquant.realism_gate.checks.market_rules import check_market_rules
        from myquant.realism_gate.checks.cost_model import check_cost_model
        from myquant.realism_gate.checks.data_pit import check_data_pit
        from myquant.realism_gate.checks.sample_pool import check_sample_pool
        assert callable(check_market_rules)
        assert callable(check_cost_model)
        assert callable(check_data_pit)
        assert callable(check_sample_pool)


class TestReportImportable:
    """R-19 收录：report.py 被 tests import。"""

    def test_report_importable(self):
        from myquant.realism_gate.report import write_blocked_artifact, write_manifest_to_results
        assert callable(write_blocked_artifact)
        assert callable(write_manifest_to_results)


class TestConfig:
    """RealismGateConfig 默认值。"""

    def test_default_enabled_false(self):
        """v2.2 分期启用期：默认 enabled=False（仅审计不阻断）。"""
        cfg = RealismGateConfig()
        assert cfg.enabled is False
        assert cfg.audit_violations is True

    def test_get_set_config(self):
        cfg = RealismGateConfig(enabled=True)
        set_config(cfg)
        assert get_config().enabled is True


class TestRegisterCheck:
    """注册防御。"""

    def test_register_ok(self):
        def fake_check(m):
            return []
        register_realism_check("fake", fake_check)

    def test_register_empty_name_raises(self):
        with pytest.raises(ValueError, match="name 不能为空"):
            register_realism_check("   ", lambda m: [])

    def test_register_duplicate_raises(self):
        register_realism_check("dup", lambda m: [])
        with pytest.raises(ValueError, match="重复注册"):
            register_realism_check("dup", lambda m: [])


class TestAssertPass:
    """assert_realism_pass 端到端。"""

    def test_complete_manifest_no_violation_seals(self):
        """完整 manifest + 无注册检查 → 通过 + seal()。"""
        m = RealismManifest()
        _fill_minimal(m)
        assert_realism_pass(m)
        assert m.is_sealed() is True
        assert m.status == "ok"

    def test_critical_violation_enabled_raises(self):
        """critical 违规 + enabled=True → 抛 RealismGateError。"""
        set_config(RealismGateConfig(enabled=True, audit_violations=False))

        def bad_check(m):
            return [RealismViolation(
                field_path="adjust_degraded_symbols",
                critical=True,
                message="复权降级标的非空",
                actual_value=["000300.SH"],
            )]
        register_realism_check("data_pit", bad_check)

        m = RealismManifest()
        _fill_minimal(m)
        with pytest.raises(RealismGateError) as exc:
            assert_realism_pass(m)
        assert len(exc.value.violations) >= 1
        assert "adjust_degraded_symbols" in exc.value.violations[0].field_path
        assert m.is_sealed() is False  # 阻断时未封印

    def test_critical_violation_disabled_audit_only(self):
        """critical 违规 + enabled=False → 不抛（audit 档）。"""
        set_config(RealismGateConfig(enabled=False, audit_violations=False))

        def bad_check(m):
            return [RealismViolation(
                field_path="pool.exclusion_basis",
                critical=True,
                message="样本池 today 口径",
                actual_value="today",
            )]
        register_realism_check("sample_pool", bad_check)

        m = RealismManifest()
        _fill_minimal(m)
        # 不抛异常（audit 档）
        assert_realism_pass(m)
        # 注意：有 critical 违规时不 seal（仅 audit 记录）
        # 本测试只断言不抛异常

    def test_incomplete_manifest_raises_fail_closed(self):
        """缺 critical 字段 → ManifestIncompleteError（fail-closed）。"""
        m = RealismManifest()
        m.set_field("run_id", "run-incomplete")
        with pytest.raises(ManifestIncompleteError) as exc:
            assert_realism_pass(m)
        assert len(exc.value.missing) > 0
        assert "strategy_id" in exc.value.missing

    def test_check_exception_fail_closed(self):
        """检查函数自身异常 → fail-closed（记 gate-error 违规，阻断）。"""
        set_config(RealismGateConfig(enabled=True, audit_violations=False))

        def exploding_check(m):
            raise RuntimeError("检查器内部崩溃")
        register_realism_check("exploder", exploding_check)

        m = RealismManifest()
        _fill_minimal(m)
        with pytest.raises(RealismGateError) as exc:
            assert_realism_pass(m)
        # 违规中应包含 gate-error
        paths = [v.field_path for v in exc.value.violations]
        assert any("gate_error" in p for p in paths)

    def test_build_stage_enum_violation_collected(self):
        """build() 阶段检出的枚举取值域违规也被计入 critical 违规。"""
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        m = RealismManifest()
        _fill_minimal(m)
        m.set_field("price_limit.pre_close_source", "invalid_value")
        with pytest.raises(RealismGateError) as exc:
            assert_realism_pass(m)


# ═══════════════════════════════════════════════
#  P1-03 fail-closed on misconfiguration
# ═══════════════════════════════════════════════

class TestMisconfigurationFailClosed:
    """P1-03：配置值类型错误 → RealismGateError（不静默 bool() 转换）。"""

    def test_enabled_string_raises(self):
        """enabled="false"（字符串）→ 抛 RealismGateError（bool("false")=True 隐患）。"""
        with pytest.raises(RealismGateError, match="enabled.*类型错误"):
            RealismGateConfig.from_dict({"enabled": "false"})

    def test_enabled_int_raises(self):
        """enabled=1（int）→ 抛 RealismGateError。"""
        with pytest.raises(RealismGateError, match="enabled.*类型错误"):
            RealismGateConfig.from_dict({"enabled": 1})

    def test_audit_violations_string_raises(self):
        """audit_violations="yes"（字符串）→ 抛 RealismGateError。"""
        with pytest.raises(RealismGateError, match="audit_violations.*类型错误"):
            RealismGateConfig.from_dict({"audit_violations": "yes"})

    def test_empty_dict_defaults(self):
        """段缺失（空 dict）→ 默认值（enabled=False, audit=True）。"""
        cfg = RealismGateConfig.from_dict({})
        assert cfg.enabled is False
        assert cfg.audit_violations is True

    def test_valid_bool_passes(self):
        """正确 bool 值 → 正常构造。"""
        cfg = RealismGateConfig.from_dict({"enabled": True, "audit_violations": False})
        assert cfg.enabled is True
        assert cfg.audit_violations is False
