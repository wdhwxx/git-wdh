# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_data_quality_gate.py
"""
P1-13 回测期数据质量门禁单元测试
==========================================================
覆盖：
  - provider.scan_data_quality：NaN 价 / OHLC 关系违例统计（L0 纯函数）
  - provider 模块级累计：accumulate_data_quality / get_data_quality_stats /
    reset_data_quality_stats（快照隔离 + 跨回测清空）
  - engine._scan_entry_data_quality：入口聚合统计（data dict / None / 覆盖率截断）
  - checks.data_pit.check_data_pit：coverage_rate<0.5 → critical（None 不判）
  - 引擎创建前置 fail-closed：enabled=True + manifest quality 真实值 →
    assert_realism_pass 抛 RealismGateError 阻断（反红）
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pandas as pd
import pytest

from myquant.data_access.provider import (
    accumulate_data_quality,
    get_data_quality_stats,
    reset_data_quality_stats,
    scan_data_quality,
)
from myquant.backtest.engine import _scan_entry_data_quality
from myquant.realism_gate import (
    RealismGateConfig,
    RealismGateError,
    RealismManifest,
    assert_realism_pass,
    set_config,
)
from myquant.realism_gate.checks.data_pit import check_data_pit
from myquant.realism_gate.gate import reset_realism_checks


def _df(rows: Dict[str, list]) -> pd.DataFrame:
    """构造中文列行情 DataFrame（索引为日期）。"""
    dates = pd.date_range("2024-01-02", periods=len(rows["开盘价"]), freq="B")
    return pd.DataFrame(rows, index=dates)


def _clean_df(n: int = 5) -> pd.DataFrame:
    return _df({
        "开盘价": [10.0] * n,
        "最高价": [11.0] * n,
        "最低价": [9.0] * n,
        "收盘价": [10.5] * n,
        "成交量": [1000] * n,
    })


@pytest.fixture(autouse=True)
def _reset_gate_state() -> None:
    """每个测试前后清空检查注册表 + 恢复默认配置 + 清空数据质量累计。"""
    reset_realism_checks()
    set_config(RealismGateConfig(enabled=False, audit_violations=False))
    reset_data_quality_stats()
    yield
    reset_realism_checks()
    set_config(RealismGateConfig())
    reset_data_quality_stats()


def _fill_minimal(m: RealismManifest) -> None:
    """填入所有 critical 字段的最小合法值（与 test_realism_gate 同构）。"""
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
        "context_data_scoped": False, "assessed_at_run_end": True,
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
    m.set_field("fingerprint_hash", "abc123")
    m.set_field("snapshot_id", "snap-001")
    m.update_group("pool", {"pit_applied": True, "exclusion_basis": "as_of",
                            "requested_count": 50, "admitted_count": 50,
                            "dropped_count": 0, "drop_reasons": {}})
    m.update_group("survivorship", {"discount": None, "window": "1y", "status": "unknown"})
    m.update_group("invariants", {"price_bounds": {}, "volume_bound": {}, "equity_identity": {},
                                    "cash_non_negative": {}, "t1_consistency": {}})


# ─────────────────────────────────────────────
# provider.scan_data_quality（L0 纯函数）
# ─────────────────────────────────────────────
class TestScanDataQuality:
    """数据质量扫描：NaN 价 / OHLC 关系违例统计。"""

    def test_clean_df_zero(self) -> None:
        stats = scan_data_quality(_clean_df())
        assert stats == {"nan_price_rows": 0, "ohlc_violation_rows": 0}

    def test_nan_price_rows(self) -> None:
        df = _clean_df(4)
        df.loc[df.index[2], "收盘价"] = float("nan")
        df.loc[df.index[3], "最高价"] = float("nan")
        stats = scan_data_quality(df)
        assert stats["nan_price_rows"] == 2
        assert stats["ohlc_violation_rows"] == 0

    def test_ohlc_violation_rows(self) -> None:
        df = _clean_df(4)
        # high < low（最严重违例）
        df.loc[df.index[0], "最高价"] = 8.0
        df.loc[df.index[0], "最低价"] = 9.5
        # high < close
        df.loc[df.index[1], "最高价"] = 9.0
        stats = scan_data_quality(df)
        assert stats["nan_price_rows"] == 0
        assert stats["ohlc_violation_rows"] == 2

    def test_missing_price_cols_returns_zero(self) -> None:
        df = pd.DataFrame({"日期": [1, 2], "成交量": [100, 200]})
        assert scan_data_quality(df) == {"nan_price_rows": 0, "ohlc_violation_rows": 0}


# ─────────────────────────────────────────────
# provider 模块级累计（get/reset/accumulate）
# ─────────────────────────────────────────────
class TestQualityAccumulator:
    """加载窗口数据质量累计（get_bars 出口）。"""

    def test_accumulate_and_get_snapshot(self) -> None:
        accumulate_data_quality("600519.SH", _clean_df(3))
        assert get_data_quality_stats()["600519.SH"] == {
            "nan_price_rows": 0, "ohlc_violation_rows": 0,
        }
        bad = _clean_df(2)
        bad.loc[bad.index[0], "开盘价"] = float("nan")
        accumulate_data_quality("600519.SH", bad)
        stats = get_data_quality_stats()["600519.SH"]
        assert stats["nan_price_rows"] == 1

    def test_getter_returns_isolated_snapshot(self) -> None:
        accumulate_data_quality("000001.SZ", _clean_df())
        snap = get_data_quality_stats()
        snap["000001.SZ"]["nan_price_rows"] = 99
        assert get_data_quality_stats()["000001.SZ"]["nan_price_rows"] == 0

    def test_reset_clears(self) -> None:
        accumulate_data_quality("000001.SZ", _clean_df())
        reset_data_quality_stats()
        assert get_data_quality_stats() == {}

    def test_empty_df_ignored(self) -> None:
        accumulate_data_quality("000001.SZ", pd.DataFrame())
        assert get_data_quality_stats() == {}


# ─────────────────────────────────────────────
# engine._scan_entry_data_quality（入口聚合统计）
# ─────────────────────────────────────────────
class TestEntryQualityStats:
    """引擎创建前入口数据质量统计（替换硬编码乐观值）。"""

    @staticmethod
    def _config(symbols: list) -> Any:
        return SimpleNamespace(symbols=symbols)

    def test_clean_data_full_coverage(self) -> None:
        data = {"600519.SH": _clean_df(), "000001.SZ": _clean_df()}
        stats = _scan_entry_data_quality(data, self._config(["600519.SH", "000001.SZ"]))
        assert stats == {"nan_price_rows": 0, "ohlc_violation_rows": 0, "coverage_rate": 1.0}

    def test_nan_aggregated(self) -> None:
        bad = _clean_df(2)
        bad.loc[bad.index[0], "最低价"] = float("nan")
        data = {"600519.SH": _clean_df(), "000001.SZ": bad}
        stats = _scan_entry_data_quality(data, self._config(["600519.SH", "000001.SZ"]))
        assert stats["nan_price_rows"] == 1
        assert stats["ohlc_violation_rows"] == 0

    def test_provider_data_placeholder(self) -> None:
        # provider 场景（data 非 dict/None）：入口无预加载数据可扫描 → 构造占位
        # （0 NaN/0 OHLC + coverage 1.0 无缺陷证据；运行期 provider 累计收尾回写覆盖）
        stats = _scan_entry_data_quality(None, self._config(["600519.SH"]))
        assert stats == {"nan_price_rows": 0, "ohlc_violation_rows": 0, "coverage_rate": 1.0}
        stats = _scan_entry_data_quality(object(), self._config(["600519.SH"]))
        assert stats["coverage_rate"] == 1.0

    def test_coverage_capped_at_one(self) -> None:
        # data 混入请求池外标的（如基准）→ 覆盖率截断 1.0
        data = {"600519.SH": _clean_df(), "000001.SZ": _clean_df(), "000300.SH": _clean_df()}
        stats = _scan_entry_data_quality(data, self._config(["600519.SH", "000001.SZ"]))
        assert stats["coverage_rate"] == 1.0

    def test_coverage_low(self) -> None:
        data = {"600519.SH": _clean_df()}
        stats = _scan_entry_data_quality(data, self._config(
            ["600519.SH", "000001.SZ", "000002.SZ", "000003.SZ"]))
        assert stats["coverage_rate"] == pytest.approx(0.25)


# ─────────────────────────────────────────────
# checks.data_pit.check_data_pit（覆盖率判据）
# ─────────────────────────────────────────────
class TestDataPitCoverage:
    """data_pit 覆盖率判据：<0.5 → critical；None 不判。"""

    def _manifest_with_quality(self, coverage: Any) -> RealismManifest:
        m = RealismManifest()
        m.set_field("quality.nan_price_rows", 0)
        m.set_field("quality.ohlc_violation_rows", 0)
        m.set_field("quality.coverage_rate", coverage)
        return m

    def test_coverage_low_critical(self) -> None:
        m = self._manifest_with_quality(0.4)
        violations = check_data_pit(m)
        paths = [v.field_path for v in violations if v.critical]
        assert "quality.coverage_rate" in paths

    def test_coverage_ok_no_violation(self) -> None:
        m = self._manifest_with_quality(0.6)
        violations = check_data_pit(m)
        assert all(v.field_path != "quality.coverage_rate" for v in violations)

    def test_coverage_none_skipped(self) -> None:
        m = self._manifest_with_quality(None)
        violations = check_data_pit(m)
        assert all(v.field_path != "quality.coverage_rate" for v in violations)


# ─────────────────────────────────────────────
# 引擎创建前置 fail-closed（反红）
# ─────────────────────────────────────────────
class TestEngineFailClosed:
    """enabled=True + manifest quality 真实值 → assert_realism_pass 阻断（P1-13 反红）。"""

    @staticmethod
    def _register_data_pit() -> None:
        """注册真实 data_pit 检查（fixture 每测清空注册表，须显式重注册）。"""
        from myquant.realism_gate.checks.data_pit import check_data_pit
        from myquant.realism_gate.gate import register_realism_check

        register_realism_check("data_pit", check_data_pit)

    @staticmethod
    def _manifest_with_quality(nan: int, ohlc: int, coverage: float) -> RealismManifest:
        m = RealismManifest()
        _fill_minimal(m)
        m.update_group("quality", {
            "nan_price_rows": nan,
            "ohlc_violation_rows": ohlc,
            "coverage_rate": coverage,
        })
        return m

    def test_nan_prices_blocked(self) -> None:
        self._register_data_pit()
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        m = self._manifest_with_quality(nan=3, ohlc=0, coverage=1.0)
        with pytest.raises(RealismGateError):
            assert_realism_pass(m)

    def test_ohlc_violation_blocked(self) -> None:
        self._register_data_pit()
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        m = self._manifest_with_quality(nan=0, ohlc=2, coverage=1.0)
        with pytest.raises(RealismGateError):
            assert_realism_pass(m)

    def test_low_coverage_blocked(self) -> None:
        self._register_data_pit()
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        m = self._manifest_with_quality(nan=0, ohlc=0, coverage=0.4)
        with pytest.raises(RealismGateError):
            assert_realism_pass(m)

    def test_clean_data_passes(self) -> None:
        self._register_data_pit()
        set_config(RealismGateConfig(enabled=True, audit_violations=False))
        m = self._manifest_with_quality(nan=0, ohlc=0, coverage=1.0)
        assert_realism_pass(m)  # 不抛即通过

    def test_audit_mode_not_blocking(self) -> None:
        # enabled=False（审计档）：真实值写入但入口不阻断（软接线）
        self._register_data_pit()
        set_config(RealismGateConfig(enabled=False, audit_violations=False))
        m = self._manifest_with_quality(nan=5, ohlc=0, coverage=0.3)
        assert_realism_pass(m)  # 审计档不抛
