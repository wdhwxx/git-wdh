# 文件名：tests/unit/test_realism_manifest.py
"""
tests/unit/test_realism_manifest.py - RealismManifest 数据契约测试（P0-01 第一刀）
================================================================================

覆盖：
  - R-19 零覆盖收录（realism_gate.manifest 被 tests import）
  - build() 完整性校验：缺 critical 字段 → ManifestIncompleteError（fail-closed）
  - set_field/get_field 点号路径分组注入
  - status 推导（degraded 信号）
  - manifest_signature 跨运行可比性（剔除 run_id/时间戳）
  - 序列化 to_dict/from_dict 往返
  - 枚举取值域校验
"""

import pytest

from myquant.realism_gate import (
    CRITICAL_FIELDS,
    ManifestIncompleteError,
    RealismManifest,
)


class TestManifestImportable:
    """R-19 收录：realism_gate.manifest 被 tests import 即视为覆盖。"""

    def test_importable(self):
        from myquant.realism_gate.manifest import (
            ManifestIncompleteError,
            RealismGateError,
            RealismManifest,
        )
        assert callable(RealismManifest)
        assert RealismGateError is not None
        assert ManifestIncompleteError is not None


class TestFieldInjection:
    """set_field / get_field 点号路径分组注入。"""

    def test_set_flat_field(self):
        m = RealismManifest()
        m.set_field("run_id", "run-001")
        assert m.get_field("run_id") == "run-001"

    def test_set_nested_group(self):
        m = RealismManifest()
        m.set_field("causality.context_sliced", True)
        m.set_field("causality.guard_injected", True)
        assert m.get_field("causality.context_sliced") is True
        assert m.get_field("causality.guard_injected") is True

    def test_update_group(self):
        m = RealismManifest()
        m.update_group("causality", {"context_sliced": True, "guard_injected": True})
        assert m.get_field("causality.context_sliced") is True

    def test_get_default(self):
        m = RealismManifest()
        assert m.get_field("nonexistent.field", "default") == "default"


class TestBuildIncomplete:
    """build() 缺 critical 字段 → ManifestIncompleteError（fail-closed）。"""

    def test_empty_manifest_raises(self):
        m = RealismManifest()
        with pytest.raises(ManifestIncompleteError) as exc:
            m.build()
        assert len(exc.value.missing) > 0
        # run_id 必须在缺失列表中
        assert "run_id" in exc.value.missing

    def test_partial_manifest_raises(self):
        m = RealismManifest()
        m.set_field("run_id", "run-001")
        m.set_field("strategy_id", "rsrs")
        with pytest.raises(ManifestIncompleteError) as exc:
            m.build()
        # run_id/strategy_id 已填，但其余 critical 字段仍缺
        assert "causality.context_sliced" in exc.value.missing


class TestBuildComplete:
    """完整 manifest build 通过 + status 推导。"""

    @staticmethod
    def _fill_minimal(m: RealismManifest) -> None:
        """填入所有 critical 字段的最小合法值。"""
        m.set_field("run_id", "run-001")
        m.set_field("strategy_id", "rsrs")
        m.set_field("engine_mode_requested", "auto")
        m.set_field("engine_mode_resolved", "mixed")
        m.set_field("start_date", "2024-01-01")
        m.set_field("end_date", "2024-02-28")
        m.set_field("initial_capital", 1_000_000.0)
        m.update_group("causality", {
            "context_sliced": False,
            "current_bar_ts_enforced": False,
            "full_sample_declared": False,
            "guard_injected": True,
            "context_data_scoped": False,
            # P0-01（#344）：运行期形态（build 完整性通过即可）
            "assessed_at_run_end": True,
        })
        m.update_group("price_limit", {"enabled": True, "pct_by_board": {}, "pre_close_source": "data_col", "intraday_touch_modeled": True})
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
        m.set_field("data.snapshot_applicable", False)
        m.set_field("data.snapshot_mismatch", False)
        m.update_group("pool", {"pit_applied": True, "exclusion_basis": "as_of",
                                "requested_count": 50, "admitted_count": 50,
                                "dropped_count": 0, "drop_reasons": {}})
        m.update_group("survivorship", {"discount": None, "window": "1y", "status": "unknown"})
        m.update_group("invariants", {"price_bounds": {}, "volume_bound": {}, "equity_identity": {},
                                      "cash_non_negative": {}, "t1_consistency": {}})

    def test_build_ok_status(self):
        m = RealismManifest()
        self._fill_minimal(m)
        m.build()
        assert m.status == "ok"

    def test_build_degraded_on_degraded_symbols(self):
        m = RealismManifest()
        self._fill_minimal(m)
        m.set_field("adjust_degraded_symbols", ["000300.SH"])
        m.build()
        assert m.status == "degraded"

    def test_build_degraded_on_today_exclusion(self):
        m = RealismManifest()
        self._fill_minimal(m)
        m.set_field("pool.exclusion_basis", "today")
        m.build()
        assert m.status == "degraded"


class TestEnumValidation:
    """枚举取值域校验。"""

    def test_invalid_enum_value_violates(self):
        m = RealismManifest()
        TestBuildComplete._fill_minimal(m)
        m.set_field("price_limit.pre_close_source", "invalid_enum")
        m.build()
        paths = [v.field_path for v in m.violations]
        assert "price_limit.pre_close_source" in paths


class TestSignature:
    """manifest_signature 跨运行可比性。"""

    def test_signature_stable_across_run_id(self):
        m1 = RealismManifest()
        TestBuildComplete._fill_minimal(m1)
        m1.build()
        m2 = RealismManifest()
        TestBuildComplete._fill_minimal(m2)
        m2.set_field("run_id", "different-run-id")  # 不同 run_id
        m2.build()
        # run_id 被剔除，签名应一致
        assert m1.manifest_signature() == m2.manifest_signature()

    def test_signature_changes_on_field(self):
        m1 = RealismManifest()
        TestBuildComplete._fill_minimal(m1)
        m1.build()
        m2 = RealismManifest()
        TestBuildComplete._fill_minimal(m2)
        m2.set_field("slippage.value", 0.002)  # 不同成本
        m2.build()
        assert m1.manifest_signature() != m2.manifest_signature()


class TestSerialization:
    """to_dict / from_dict 往返。"""

    def test_roundtrip(self):
        m = RealismManifest()
        TestBuildComplete._fill_minimal(m)
        m.build()
        d = m.to_dict()
        m2 = RealismManifest.from_dict(d)
        assert m2.status == m.status
        assert m2.get_field("run_id") == m.get_field("run_id")
        assert m2.manifest_signature() == m.manifest_signature()


class TestSeal:
    """seal / is_sealed（两阶段握手 §4.4 第⑤步）。"""

    def test_seal(self):
        m = RealismManifest()
        assert m.is_sealed() is False
        m.seal()
        assert m.is_sealed() is True


class TestCriticalFieldsCompleteness:
    """CRITICAL_FIELDS 非空且覆盖 §4.2 关键分组。"""

    def test_critical_fields_nonempty(self):
        assert len(CRITICAL_FIELDS) > 30

    def test_critical_fields_cover_key_groups(self):
        paths = CRITICAL_FIELDS
        assert any(p.startswith("causality.") for p in paths)
        assert any(p.startswith("price_limit.") for p in paths)
        assert any(p.startswith("adjust_") for p in paths)
        assert any(p.startswith("pool.") for p in paths)
