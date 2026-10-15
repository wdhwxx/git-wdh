# 文件名：tests/unit/test_fee_common.py
"""
P3-5（v1.2 §6.4 ROI 5）：backtest/fee_common.py 覆盖率补测（44.9%→85%）

覆盖范围：
  1. 方向归一化（normalize_side / normalize_side_array / 非法输入）
  2. 金额校验（_validate_notional：负值 / NaN / Inf）
  3. FeeBreakdown / VectorizedFeeResult 数据类属性与序列化
  4. FeeCalculator：构造校验 / 标量费用 / 滑点 / 一站式 / 向量化 / 一致性校验
  5. 模块级便捷函数与默认单例

设计原则（fee_common docstring）：费用只依赖成交本身，无未来函数风险；
向量化与标量数学等价（assert_vectorized_consistent 自证）。
"""

from __future__ import annotations

import numpy as np
import pytest

from myquant.backtest.fee_common import (
    FeeBreakdown,
    FeeCalculator,
    VectorizedFeeResult,
    calculate_fee,
    calculate_fee_vectorized,
    get_default_calculator,
    normalize_side,
    normalize_side_array,
)
from myquant.models.backtest import (
    CommissionModel,
    CommissionType,
    SlippageModel,
)
from myquant.models.trade import OrderSide

# ─────────────────────────────────────────────
# normalize_side
# ─────────────────────────────────────────────

class TestNormalizeSide:
    def test_enum_passthrough(self) -> None:
        assert normalize_side(OrderSide.BUY) is OrderSide.BUY
        assert normalize_side(OrderSide.SELL) is OrderSide.SELL

    def test_string_aliases(self) -> None:
        for alias in ("BUY", "B", "买", "买入", "LONG", "buy", "  BUY  "):
            assert normalize_side(alias) is OrderSide.BUY, alias
        for alias in ("SELL", "S", "卖", "卖出", "SHORT", "sell"):
            assert normalize_side(alias) is OrderSide.SELL, alias

    def test_invalid_side_raises(self) -> None:
        with pytest.raises(ValueError, match="无法识别的订单方向"):
            normalize_side("HOLD")
        with pytest.raises(ValueError, match="无法识别的订单方向"):
            normalize_side(123)  # type: ignore[arg-type]

    def test_invalid_string_type(self) -> None:
        with pytest.raises(ValueError):
            normalize_side("")


# ─────────────────────────────────────────────
# normalize_side_array
# ─────────────────────────────────────────────

class TestNormalizeSideArray:
    def test_enum_array(self) -> None:
        arr = normalize_side_array(np.array([OrderSide.BUY, OrderSide.SELL]))
        assert arr.tolist() == [0, 1]

    def test_string_array(self) -> None:
        arr = normalize_side_array(np.array(["BUY", "卖", "LONG", "SHORT"]))
        assert arr.tolist() == [0, 1, 0, 1]

    def test_int_array(self) -> None:
        arr = normalize_side_array(np.array([0, 1, 0]))
        assert arr.tolist() == [0, 1, 0]

    def test_invalid_int_raises(self) -> None:
        with pytest.raises(ValueError, match="无法识别的方向整数编码"):
            normalize_side_array(np.array([2]))

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError, match="无法识别的订单方向"):
            normalize_side_array(np.array(["HOLD"]))

    def test_empty_array(self) -> None:
        arr = normalize_side_array(np.array([], dtype=object))
        assert arr.size == 0


# ─────────────────────────────────────────────
# FeeBreakdown
# ─────────────────────────────────────────────

class TestFeeBreakdown:
    def test_total_commission_and_cost(self) -> None:
        fb = FeeBreakdown(commission=10.0, stamp_tax=5.0, transfer_fee=1.0, slippage=4.0)
        assert fb.total_commission == pytest.approx(16.0)
        assert fb.total_cost == pytest.approx(20.0)

    def test_to_dict(self) -> None:
        fb = FeeBreakdown(commission=10.0, stamp_tax=5.0, transfer_fee=1.0, slippage=4.0)
        d = fb.to_dict()
        assert d["total_commission"] == pytest.approx(16.0)
        assert d["total_cost"] == pytest.approx(20.0)
        assert set(d) == {"commission", "stamp_tax", "transfer_fee",
                          "slippage", "total_commission", "total_cost"}

    def test_repr_contains_key_fields(self) -> None:
        fb = FeeBreakdown(1.0, 2.0, 3.0, 4.0)
        assert "佣金" in repr(fb) and "合计" in repr(fb)


# ─────────────────────────────────────────────
# VectorizedFeeResult
# ─────────────────────────────────────────────

class TestVectorizedFeeResult:
    def test_properties_and_len(self) -> None:
        r = VectorizedFeeResult(
            commission=np.array([1.0, 2.0]),
            stamp_tax=np.array([0.0, 3.0]),
            transfer_fee=np.array([0.1, 0.2]),
            slippage=np.array([0.5, 0.6]),
        )
        assert r.total_commission.tolist() == pytest.approx([1.1, 5.2])
        assert r.total_cost.tolist() == pytest.approx([1.6, 5.8])
        assert len(r) == 2

    def test_to_dict(self) -> None:
        r = VectorizedFeeResult(
            commission=np.array([1.0]),
            stamp_tax=np.array([0.0]),
            transfer_fee=np.array([0.1]),
            slippage=np.array([0.0]),
        )
        d = r.to_dict()
        assert d["total_cost"][0] == pytest.approx(1.1)
        assert d["commission"][0] == pytest.approx(1.0)


# ─────────────────────────────────────────────
# FeeCalculator 构造
# ─────────────────────────────────────────────

class TestFeeCalculatorInit:
    def test_default_models(self) -> None:
        calc = FeeCalculator()
        assert calc.commission_model.type == CommissionType.RATIO
        assert calc.slippage_model.type == "fixed_bps"

    def test_invalid_commission_model_raises(self) -> None:
        with pytest.raises(TypeError, match="commission_model 必须为 CommissionModel"):
            FeeCalculator(commission_model="bad")  # type: ignore[arg-type]

    def test_invalid_slippage_model_raises(self) -> None:
        with pytest.raises(TypeError, match="slippage_model 必须为 SlippageModel"):
            FeeCalculator(slippage_model=123)  # type: ignore[arg-type]


# ─────────────────────────────────────────────
# 标量佣金/印花税/过户费
# ─────────────────────────────────────────────

class TestCalcCommissionBreakdown:
    def test_buy_no_stamp_tax(self) -> None:
        calc = FeeCalculator()
        comm, stamp, transfer = calc.calc_commission_breakdown(side="BUY", notional=100000.0)
        # 佣金 万2 = 20 元（>5 元最低佣金）
        assert comm == pytest.approx(20.0)
        assert stamp == 0.0
        # 过户费 万0.1 = 1 元
        assert transfer == pytest.approx(1.0)

    def test_sell_has_stamp_tax(self) -> None:
        calc = FeeCalculator()
        comm, stamp, transfer = calc.calc_commission_breakdown(side="SELL", notional=100000.0)
        assert comm == pytest.approx(20.0)
        assert stamp == pytest.approx(50.0)  # 万5
        assert transfer == pytest.approx(1.0)

    def test_min_commission_floor(self) -> None:
        calc = FeeCalculator()
        comm, _, _ = calc.calc_commission_breakdown(side="BUY", notional=1000.0)
        assert comm == pytest.approx(5.0)  # 最低 5 元

    def test_fixed_commission_type(self) -> None:
        # 实现语义：FIXED 返回 max(rate, min_commission)（最低佣金封底）
        model = CommissionModel(type=CommissionType.FIXED, buy_rate=3.0, sell_rate=3.0)
        calc = FeeCalculator(commission_model=model)
        comm, _, _ = calc.calc_commission_breakdown(side="BUY", notional=1e6)
        assert comm == pytest.approx(5.0)  # max(3.0, 最低佣金 5.0)

    def test_invalid_notional_negative(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="成交金额不能为负"):
            calc.calc_commission_breakdown(side="BUY", notional=-100.0)

    def test_invalid_notional_nan(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="必须为有限数值"):
            calc.calc_commission_breakdown(side="BUY", notional=float("nan"))

    def test_invalid_notional_inf(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="必须为有限数值"):
            calc.calc_commission_breakdown(side="BUY", notional=float("inf"))


# ─────────────────────────────────────────────
# 滑点
# ─────────────────────────────────────────────

class TestSlippage:
    def test_fixed_bps_per_share(self) -> None:
        calc = FeeCalculator()
        assert calc.calc_slippage_per_share(price=10.0) == pytest.approx(0.002)  # 2bps

    def test_slippage_amount(self) -> None:
        calc = FeeCalculator()
        assert calc.calc_slippage_amount(price=10.0, quantity=10000) == pytest.approx(20.0)

    def test_negative_price_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="成交价不能为负"):
            calc.calc_slippage_per_share(price=-1.0)

    def test_negative_quantity_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="成交数量不能为负"):
            calc.calc_slippage_amount(price=10.0, quantity=-5.0)

    def test_volume_based_mode(self) -> None:
        model = SlippageModel(type="volume_based", fixed_bps=2.0, volume_impact=0.1)
        calc = FeeCalculator(slippage_model=model)
        # volume=10000, order_size=1000 → impact=0.1 → 10*0.1*0.1=0.1
        assert calc.calc_slippage_per_share(price=10.0, volume=10000.0, order_size=1000.0) \
            == pytest.approx(0.1)

    def test_volume_based_zero_volume_falls_back(self) -> None:
        model = SlippageModel(type="volume_based", fixed_bps=2.0, volume_impact=0.1)
        calc = FeeCalculator(slippage_model=model)
        # volume<=0 → 回退固定 bps（2bps）
        assert calc.calc_slippage_per_share(price=10.0, volume=0.0, order_size=1000.0) \
            == pytest.approx(0.002)

    def test_none_mode_zero_slippage(self) -> None:
        model = SlippageModel(type="none")
        calc = FeeCalculator(slippage_model=model)
        assert calc.calc_slippage_per_share(price=10.0) == 0.0


# ─────────────────────────────────────────────
# 一站式标量 calc_fee
# ─────────────────────────────────────────────

class TestCalcFee:
    def test_full_breakdown(self) -> None:
        calc = FeeCalculator()
        fee = calc.calc_fee(side="BUY", notional=100000.0, price=10.0, quantity=10000)
        assert fee.commission == pytest.approx(20.0)
        assert fee.stamp_tax == 0.0
        assert fee.transfer_fee == pytest.approx(1.0)
        assert fee.slippage == pytest.approx(20.0)
        assert fee.total_cost == pytest.approx(41.0)

    def test_sell_full_breakdown(self) -> None:
        calc = FeeCalculator()
        fee = calc.calc_fee(side="SELL", notional=100000.0, price=10.0, quantity=10000)
        assert fee.stamp_tax == pytest.approx(50.0)
        assert fee.total_commission == pytest.approx(71.0)

    def test_zero_price_ignores_slippage(self) -> None:
        calc = FeeCalculator()
        fee = calc.calc_fee(side="BUY", notional=100000.0)
        assert fee.slippage == 0.0


# ─────────────────────────────────────────────
# 向量化佣金/印花税/过户费
# ─────────────────────────────────────────────

class TestCalcCommissionBreakdownVectorized:
    def test_scalar_side_batch(self) -> None:
        calc = FeeCalculator()
        comm, stamp, transfer = calc.calc_commission_breakdown_vectorized(
            side="SELL", notional=np.array([1000.0, 100000.0])
        )
        assert comm[0] == pytest.approx(5.0)   # 最低佣金
        assert comm[1] == pytest.approx(20.0)
        assert stamp[1] == pytest.approx(50.0)
        assert transfer[0] == pytest.approx(0.01)

    def test_mixed_side_array(self) -> None:
        calc = FeeCalculator()
        comm, stamp, transfer = calc.calc_commission_breakdown_vectorized(
            side=np.array(["BUY", "SELL"]),
            notional=np.array([100000.0, 100000.0]),
        )
        assert stamp.tolist() == pytest.approx([0.0, 50.0])

    def test_empty_array(self) -> None:
        calc = FeeCalculator()
        comm, stamp, transfer = calc.calc_commission_breakdown_vectorized(
            side="BUY", notional=np.array([])
        )
        assert comm.size == 0 and stamp.size == 0 and transfer.size == 0

    def test_negative_amount_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="不能包含负值"):
            calc.calc_commission_breakdown_vectorized(
                side="BUY", notional=np.array([-1.0])
            )

    def test_non_finite_amount_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="全部为有限数值"):
            calc.calc_commission_breakdown_vectorized(
                side="BUY", notional=np.array([np.inf])
            )

    def test_side_length_mismatch_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="不匹配"):
            calc.calc_commission_breakdown_vectorized(
                side=np.array(["BUY", "SELL"]), notional=np.array([1.0, 2.0, 3.0])
            )

    def test_invalid_side_array_element_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError):
            calc.calc_commission_breakdown_vectorized(
                side=np.array(["BUY", "HOLD"]), notional=np.array([1.0, 2.0])
            )

    def test_fixed_mode(self) -> None:
        # FIXED：max(rate, min_commission) 封底 → 买卖均 5.0
        model = CommissionModel(type=CommissionType.FIXED, buy_rate=3.0, sell_rate=5.0)
        calc = FeeCalculator(commission_model=model)
        comm, _, _ = calc.calc_commission_breakdown_vectorized(
            side=np.array(["BUY", "SELL"]), notional=np.array([1e6, 1e6])
        )
        assert comm.tolist() == pytest.approx([5.0, 5.0])

    def test_tiered_mode_falls_back(self) -> None:
        # TIERED 模式回退逐元素标量，结果应与 RATIO 一致
        tiered = CommissionModel(type=CommissionType.TIERED, buy_rate=0.0002, sell_rate=0.0002)
        ratio = CommissionModel(type=CommissionType.RATIO, buy_rate=0.0002, sell_rate=0.0002)
        c1 = FeeCalculator(commission_model=tiered)
        c2 = FeeCalculator(commission_model=ratio)
        n = np.array([1000.0, 50000.0, 100000.0])
        r1 = c1.calc_commission_breakdown_vectorized(side="SELL", notional=n)
        r2 = c2.calc_commission_breakdown_vectorized(side="SELL", notional=n)
        assert r1[0].tolist() == pytest.approx(r2[0].tolist(), abs=1e-9)


# ─────────────────────────────────────────────
# 向量化滑点
# ─────────────────────────────────────────────

class TestSlippageVectorized:
    def test_empty_price(self) -> None:
        calc = FeeCalculator()
        out = calc.calc_slippage_per_share_vectorized(price=np.array([]))
        assert out.size == 0

    def test_negative_price_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="不能包含负值"):
            calc.calc_slippage_per_share_vectorized(price=np.array([-1.0]))

    def test_fixed_bps_vectorized(self) -> None:
        calc = FeeCalculator()
        out = calc.calc_slippage_per_share_vectorized(price=np.array([10.0, 20.0]))
        assert out.tolist() == pytest.approx([0.002, 0.004])

    def test_none_mode_zero(self) -> None:
        calc = FeeCalculator(slippage_model=SlippageModel(type="none"))
        out = calc.calc_slippage_per_share_vectorized(price=np.array([10.0, 20.0]))
        assert out.tolist() == [0.0, 0.0]

    def test_volume_based_vectorized(self) -> None:
        model = SlippageModel(type="volume_based", fixed_bps=2.0, volume_impact=0.1)
        calc = FeeCalculator(slippage_model=model)
        out = calc.calc_slippage_per_share_vectorized(
            price=np.array([10.0, 10.0]),
            volume=np.array([10000.0, 0.0]),
            order_size=np.array([1000.0, 1000.0]),
        )
        # 有量：0.1；无量回退：0.002
        assert out.tolist() == pytest.approx([0.1, 0.002])

    def test_unknown_mode_zero_with_warning(self) -> None:
        model = SlippageModel(type="bogus_mode")
        calc = FeeCalculator(slippage_model=model)
        out = calc.calc_slippage_per_share_vectorized(price=np.array([10.0]))
        assert out.tolist() == [0.0]


# ─────────────────────────────────────────────
# 一站式向量化 calc_fee_vectorized
# ─────────────────────────────────────────────

class TestCalcFeeVectorized:
    def test_full_vectorized(self) -> None:
        calc = FeeCalculator()
        r = calc.calc_fee_vectorized(
            side="BUY",
            notional=np.array([100000.0]),
            price=np.array([10.0]),
            quantity=np.array([10000.0]),
        )
        assert r.commission[0] == pytest.approx(20.0)
        assert r.slippage[0] == pytest.approx(20.0)
        assert r.total_cost[0] == pytest.approx(41.0)

    def test_price_shape_mismatch_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="不匹配"):
            calc.calc_fee_vectorized(
                side="BUY",
                notional=np.array([1.0, 2.0]),
                price=np.array([1.0]),
            )

    def test_quantity_shape_mismatch_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="不匹配"):
            calc.calc_fee_vectorized(
                side="BUY",
                notional=np.array([1.0, 2.0]),
                price=np.array([1.0, 2.0]),
                quantity=np.array([1.0]),
            )

    def test_negative_quantity_raises(self) -> None:
        calc = FeeCalculator()
        with pytest.raises(ValueError, match="不能包含负值"):
            calc.calc_fee_vectorized(
                side="BUY",
                notional=np.array([1.0]),
                price=np.array([1.0]),
                quantity=np.array([-1.0]),
            )

    def test_price_none_zero_slippage(self) -> None:
        calc = FeeCalculator()
        r = calc.calc_fee_vectorized(side="BUY", notional=np.array([100000.0]))
        assert r.slippage[0] == 0.0

    def test_quantity_none_slippage_is_per_share(self) -> None:
        calc = FeeCalculator()
        r = calc.calc_fee_vectorized(
            side="BUY", notional=np.array([100000.0]), price=np.array([10.0])
        )
        assert r.slippage[0] == pytest.approx(0.002)


# ─────────────────────────────────────────────
# 一致性校验
# ─────────────────────────────────────────────

class TestVectorizedConsistency:
    def test_consistent_ratio(self) -> None:
        calc = FeeCalculator()
        assert calc.assert_vectorized_consistent(
            side="SELL",
            notional=np.array([1000.0, 50000.0, 100000.0, 1e6]),
            price=np.array([10.0, 20.0, 30.0, 40.0]),
            quantity=np.array([100.0, 2500.0, 3333.0, 25000.0]),
        ) is True

    def test_consistent_volume_based(self) -> None:
        model = SlippageModel(type="volume_based", fixed_bps=2.0, volume_impact=0.1)
        calc = FeeCalculator(slippage_model=model)
        assert calc.assert_vectorized_consistent(
            side="BUY",
            notional=np.array([1000.0, 50000.0, 100000.0]),
            price=np.array([10.0, 20.0, 30.0]),
            quantity=np.array([100.0, 2500.0, 3333.0]),
            volume=np.array([10000.0, 50000.0, 0.0]),
            order_size=np.array([1000.0, 2000.0, 0.0]),
        ) is True

    def test_scalar_side_consistent(self) -> None:
        # assert_vectorized_consistent 仅支持标量 side（内部按逐元素标量校验）
        calc = FeeCalculator()
        assert calc.assert_vectorized_consistent(
            side="BUY",
            notional=np.array([1000.0, 50000.0, 100000.0]),
        ) is True


# ─────────────────────────────────────────────
# 模块级便捷函数与单例
# ─────────────────────────────────────────────

class TestModuleLevelFunctions:
    def test_get_default_calculator_singleton(self) -> None:
        assert get_default_calculator() is get_default_calculator()

    def test_calculate_fee(self) -> None:
        fee = calculate_fee(side="SELL", notional=200000.0, price=25.0, quantity=8000)
        assert fee.commission == pytest.approx(40.0)
        assert fee.stamp_tax == pytest.approx(100.0)
        assert fee.slippage == pytest.approx(40.0)

    def test_calculate_fee_vectorized(self) -> None:
        r = calculate_fee_vectorized(
            side="BUY",
            notional=np.array([100000.0]),
            price=np.array([10.0]),
            quantity=np.array([10000.0]),
        )
        assert r.total_cost[0] == pytest.approx(41.0)

    def test_calculate_fee_with_custom_models(self) -> None:
        # FIXED 2.0 元/笔但最低佣金 5 元封底
        model = CommissionModel(type=CommissionType.FIXED, buy_rate=2.0, sell_rate=2.0)
        fee = calculate_fee(side="BUY", notional=1e6, commission_model=model)
        assert fee.commission == pytest.approx(5.0)
