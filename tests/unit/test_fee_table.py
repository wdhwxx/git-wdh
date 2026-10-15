# 文件名：tests/unit/test_fee_table.py
"""
P3-5（v1.2 §6.4 ROI 5）：backtest/fee_table.py 覆盖率补测（57.1%→85%）

覆盖范围：
  1. 全局启用开关（is_table_lookup_enabled / enable_table_lookup 状态跃迁与警告）
  2. FeeTable：构建 / 拐点入网 / 标量与向量化查表 / clamp / 精度评估
  3. TableFeeCalculator：构造 / 覆盖接口 / 与精确计算一致
  4. estimate_max_error / 参数防御

背景（模块 docstring）：fee_table 为"已实现未接线"的待命加速层（P2-06 评估），
默认关闭；生产路径始终走 fee_common.FeeCalculator。本测试守护其查表精度与接口。
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from myquant.backtest.fee_common import FeeCalculator
from myquant.backtest.fee_table import (
    DEFAULT_BINS,
    DEFAULT_MAX_AMOUNT,
    DEFAULT_MIN_AMOUNT,
    FeeTable,
    TableFeeCalculator,
    build_fee_table,
    enable_table_lookup,
    estimate_max_error,
    is_table_lookup_enabled,
)
from myquant.backtest.fee_table import logger as _fee_table_logger
from myquant.models.backtest import CommissionModel, CommissionType

_ERROR_TOLERANCE: float = 0.001


@pytest.fixture(autouse=True)
def _reset_table_switch() -> None:
    """每个用例前重置全局查表开关（防测试间串扰）。"""
    enable_table_lookup(False)
    yield
    enable_table_lookup(False)


# ─────────────────────────────────────────────
# 全局开关
# ─────────────────────────────────────────────

class TestTableLookupSwitch:
    def test_default_disabled(self) -> None:
        assert is_table_lookup_enabled() is False

    def test_enable_toggle(self) -> None:
        assert enable_table_lookup(True) is True
        assert is_table_lookup_enabled() is True
        assert enable_table_lookup(False) is False
        assert is_table_lookup_enabled() is False

    def test_enable_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=_fee_table_logger.name):
            enable_table_lookup(True)
        assert "费用查表加速已启用" in caplog.text

    def test_re_enable_no_warning_duplicate(self, caplog: pytest.LogCaptureFixture) -> None:
        enable_table_lookup(True)  # 第一次跃迁已警告
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_fee_table_logger.name):
            enable_table_lookup(True)  # 已启用，不重复警告
        assert "费用查表加速已启用" not in caplog.text


# ─────────────────────────────────────────────
# build_fee_table 参数防御
# ─────────────────────────────────────────────

class TestBuildFeeTableValidation:
    def test_invalid_min_amount(self) -> None:
        with pytest.raises(ValueError, match="min_amount 必须 > 0"):
            build_fee_table(min_amount=0)

    def test_max_not_greater_than_min(self) -> None:
        with pytest.raises(ValueError, match="max_amount 必须 > min_amount"):
            build_fee_table(min_amount=1e6, max_amount=1e5)

    def test_bins_less_than_two(self) -> None:
        with pytest.raises(ValueError, match="bins 必须 >= 2"):
            build_fee_table(bins=1)

    def test_invalid_model_type(self) -> None:
        with pytest.raises(TypeError, match="commission_model 必须为 CommissionModel"):
            build_fee_table(commission_model="bad")  # type: ignore[arg-type]


# ─────────────────────────────────────────────
# FeeTable 构建与结构
# ─────────────────────────────────────────────

class TestFeeTableStructure:
    def test_default_build(self) -> None:
        table = build_fee_table()
        assert table.min_amount == DEFAULT_MIN_AMOUNT
        assert table.max_amount == DEFAULT_MAX_AMOUNT
        assert table.bins == DEFAULT_BINS

    def test_grid_monotonic_increasing(self) -> None:
        table = build_fee_table()
        assert bool(np.all(np.diff(table.grid) > 0))

    def test_break_point_in_grid(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model)
        expected = model.min_commission / model.buy_rate  # 5/0.0002 = 25000
        assert any(abs(g - expected) < 1e-6 for g in table.grid)

    def test_break_point_outside_range_excluded(self) -> None:
        # 拐点 25000 超出 [10000, 20000] → 不入网格
        table = build_fee_table(min_amount=10000.0, max_amount=20000.0)
        assert not any(abs(g - 25000.0) < 1e-6 for g in table.grid)

    def test_repr(self) -> None:
        table = build_fee_table(bins=64)
        assert "FeeTable" in repr(table)


# ─────────────────────────────────────────────
# 标量/向量化查表
# ─────────────────────────────────────────────

class TestFeeTableLookup:
    def test_buy_lookup(self) -> None:
        model = CommissionModel()
        calc = FeeCalculator(commission_model=model)
        table = build_fee_table(commission_model=model)
        t_comm, t_stamp, t_transfer = table.lookup_commission_breakdown(
            side="BUY", notional=100000.0
        )
        p_comm, p_stamp, p_transfer = calc.calc_commission_breakdown(
            side="BUY", notional=100000.0
        )
        assert t_comm == pytest.approx(p_comm, rel=_ERROR_TOLERANCE)
        assert t_stamp == p_stamp == 0.0
        assert t_transfer == pytest.approx(p_transfer, abs=1e-6)

    def test_sell_lookup(self) -> None:
        model = CommissionModel()
        calc = FeeCalculator(commission_model=model)
        table = build_fee_table(commission_model=model)
        t_comm, t_stamp, t_transfer = table.lookup_commission_breakdown(
            side="SELL", notional=100000.0
        )
        p_comm, p_stamp, p_transfer = calc.calc_commission_breakdown(
            side="SELL", notional=100000.0
        )
        assert t_comm == pytest.approx(p_comm, rel=_ERROR_TOLERANCE)
        assert t_stamp == pytest.approx(p_stamp, abs=1e-6)
        assert t_transfer == pytest.approx(p_transfer, abs=1e-6)

    def test_vectorized_equals_scalar(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model, bins=512)
        samples = np.array([1000.0, 5000.0, 25000.0, 100000.0, 1e7])
        vec = table.lookup_commission_breakdown_vectorized(side="SELL", notional=samples)
        for i, amt in enumerate(samples):
            scalar = table.lookup_commission_breakdown(side="SELL", notional=amt)
            assert vec[0][i] == pytest.approx(scalar[0], abs=1e-9)
            assert vec[1][i] == pytest.approx(scalar[1], abs=1e-9)
            assert vec[2][i] == pytest.approx(scalar[2], abs=1e-9)

    def test_buy_vectorized_stamp_zero(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model)
        _, stamp, _ = table.lookup_commission_breakdown_vectorized(
            side="BUY", notional=np.array([1000.0, 1e6])
        )
        assert stamp.tolist() == [0.0, 0.0]

    def test_negative_amount_raises(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model)
        with pytest.raises(ValueError, match="不能包含负值"):
            table.lookup_commission_breakdown_vectorized(
                side="BUY", notional=np.array([-1.0])
            )

    def test_invalid_side_raises(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model)
        with pytest.raises(ValueError):
            table.lookup_commission_breakdown(side="HOLD", notional=1000.0)

    def test_negative_notional_scalar_raises(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model)
        with pytest.raises(ValueError, match="成交金额不能为负"):
            table.lookup_commission_breakdown(side="BUY", notional=-1.0)

    def test_clamp_below_min(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model, min_amount=10000.0)
        comm, _, _ = table.lookup_commission_breakdown(side="BUY", notional=1.0)
        # clamp 到下限端点值
        assert comm == pytest.approx(table.buy_commission[0], abs=1e-9)

    def test_clamp_above_max(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model, max_amount=100000.0)
        comm, _, _ = table.lookup_commission_breakdown(side="BUY", notional=1e12)
        assert comm == pytest.approx(table.buy_commission[-1], abs=1e-9)


# ─────────────────────────────────────────────
# 精度评估
# ─────────────────────────────────────────────

class TestPrecisionEstimate:
    def test_estimate_max_relative_error_small(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model, bins=1024)
        rel, _ = table.estimate_max_relative_error(
            commission_model=model, n_samples=5000, seed=42
        )
        assert rel < _ERROR_TOLERANCE

    def test_estimate_max_relative_error_reproducible(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model, bins=512)
        r1 = table.estimate_max_relative_error(commission_model=model, n_samples=1000, seed=7)
        r2 = table.estimate_max_relative_error(commission_model=model, n_samples=1000, seed=7)
        assert r1 == r2

    def test_n_samples_zero_raises(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model)
        with pytest.raises(ValueError, match="n_samples 必须 > 0"):
            table.estimate_max_relative_error(commission_model=model, n_samples=0)

    def test_estimate_max_error_function(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model, bins=1024)
        err = estimate_max_error(table, commission_model=model, n_samples=2000)
        assert err < _ERROR_TOLERANCE

    def test_estimate_max_error_bad_type(self) -> None:
        with pytest.raises(TypeError, match="table 必须为 FeeTable"):
            estimate_max_error("bad")  # type: ignore[arg-type]

    def test_bins_128_still_precise(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model, bins=128)
        rel, _ = table.estimate_max_relative_error(
            commission_model=model, n_samples=2000, seed=1
        )
        assert rel < _ERROR_TOLERANCE


# ─────────────────────────────────────────────
# TableFeeCalculator
# ─────────────────────────────────────────────

class TestTableFeeCalculator:
    def test_auto_build_table(self) -> None:
        calc = TableFeeCalculator()
        assert isinstance(calc.table, FeeTable)

    def test_accepts_existing_table(self) -> None:
        model = CommissionModel()
        table = build_fee_table(commission_model=model, bins=256)
        calc = TableFeeCalculator(commission_model=model, table=table)
        assert calc.table is table

    def test_commission_matches_precise(self) -> None:
        model = CommissionModel()
        calc = TableFeeCalculator(commission_model=model, bins=4096)
        precise = FeeCalculator(commission_model=model)
        for amt in (1000.0, 25000.0, 100000.0, 1e7):
            t_fee = calc.calc_commission_breakdown(side="SELL", notional=amt)
            p_fee = precise.calc_commission_breakdown(side="SELL", notional=amt)
            assert t_fee[0] == pytest.approx(p_fee[0], rel=_ERROR_TOLERANCE)

    def test_vectorized_matches_precise(self) -> None:
        model = CommissionModel()
        calc = TableFeeCalculator(commission_model=model, bins=4096)
        precise = FeeCalculator(commission_model=model)
        n = np.array([1000.0, 50000.0, 100000.0, 1e6])
        t = calc.calc_commission_breakdown_vectorized(side="BUY", notional=n)
        p = precise.calc_commission_breakdown_vectorized(side="BUY", notional=n)
        assert t[0].tolist() == pytest.approx(p[0].tolist(), rel=_ERROR_TOLERANCE)

    def test_slippage_stays_precise(self) -> None:
        calc = TableFeeCalculator()
        fee = calc.calc_fee(side="BUY", notional=100000.0, price=10.0, quantity=10000)
        assert fee.slippage == pytest.approx(20.0)

    def test_repr(self) -> None:
        calc = TableFeeCalculator(bins=64)
        assert "TableFeeCalculator" in repr(calc)

    def test_fixed_mode_table(self) -> None:
        # FIXED 查表：max(rate, min_commission) 封底 → 5.0
        model = CommissionModel(type=CommissionType.FIXED, buy_rate=3.0, sell_rate=3.0)
        calc = TableFeeCalculator(commission_model=model, bins=64)
        fee = calc.calc_commission_breakdown(side="BUY", notional=1e6)
        assert fee[0] == pytest.approx(5.0, abs=1e-6)
