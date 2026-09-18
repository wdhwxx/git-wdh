# 文件名：tests/unit/test_backtest_fee_v5.py
"""
test_backtest_fee_v5.py - P1 第9项：费用模块单元测试
======================================================
覆盖 myquant/backtest/ 费用统一层（V5 引擎核心层）：
  - fee_common.FeeCalculator   全局唯一费用入口（标量 + 向量化）
  - fee_common.normalize_side_array   逐元素方向数组
  - fee_table.FeeTable / TableFeeCalculator / enable_table_lookup   查表加速

测试维度:
  1. 佣金：notional × 0.0002（买/卖）
  2. 最低佣金：低于 5 元按 5 元
  3. 印花税：仅卖出 × 0.001
  4. 过户费：× 0.00001（买卖相同）
  5. 滑点：fixed_bps=2.0 每股
  6. 一站式 calc_fee 明细（commission/stamp_tax/transfer_fee/slippage）
  7. 向量化 vs 标量一致（买卖混合方向数组）
  8. assert_vectorized_consistent 不抛异常
  9. 防御式校验（非法方向/负金额/非法模型/形状不匹配/空数组）
  10. 查表：默认关闭、网格单调递增、佣金拐点入网、误差 < 0.1%、精度警告

运行方式:
  pytest tests/unit/test_backtest_fee_v5.py -v
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
import pytest

from myquant.backtest.fee_common import (
    FeeCalculator,
    get_default_calculator,
    normalize_side_array,
)
from myquant.backtest.fee_table import (
    TableFeeCalculator,
    build_fee_table,
    enable_table_lookup,
    is_table_lookup_enabled,
)
from myquant.models.backtest import CommissionModel, SlippageModel
from myquant.models.trade import OrderSide


# ═══════════════════════════════════════════════
#  Fixture：默认费用配置与实例
# ═══════════════════════════════════════════════

@pytest.fixture
def default_calc() -> FeeCalculator:
    """默认费用计算器（内置 CommissionModel / SlippageModel 默认值）。"""
    return FeeCalculator()


@pytest.fixture(autouse=True)
def _restore_table_lookup() -> None:
    """每条用例结束后恢复 fee_table 全局开关为关闭（默认）。"""
    yield
    enable_table_lookup(False)


# ═══════════════════════════════════════════════
#  1. 佣金 / 最低佣金
# ═══════════════════════════════════════════════

class TestCommission:
    """佣金计算"""

    def test_buy_commission_rate(self, default_calc: FeeCalculator) -> None:
        """买入佣金 = 金额 × 0.0002"""
        bd = default_calc.calc_fee(OrderSide.BUY, 100_000.0)
        assert bd.commission == pytest.approx(20.0, abs=1e-9)

    def test_sell_commission_rate(self, default_calc: FeeCalculator) -> None:
        """卖出佣金 = 金额 × 0.0002（佣金买卖相同）"""
        bd = default_calc.calc_fee(OrderSide.SELL, 100_000.0)
        assert bd.commission == pytest.approx(20.0, abs=1e-9)

    def test_min_commission(self, default_calc: FeeCalculator) -> None:
        """小额成交触发最低佣金 5 元"""
        bd = default_calc.calc_fee(OrderSide.BUY, 1_000.0)
        assert bd.commission == pytest.approx(5.0, abs=1e-9)

    def test_commission_not_below_min(self, default_calc: FeeCalculator) -> None:
        """任意小额佣金不低于 5 元"""
        bd = default_calc.calc_fee(OrderSide.BUY, 0.01)
        assert bd.commission >= 5.0

    def test_commission_straddle_min(self, default_calc: FeeCalculator) -> None:
        """金额跨越拐点 25000 元（5/0.0002）两侧行为正确"""
        below = default_calc.calc_fee(OrderSide.BUY, 24_999.0)
        above = default_calc.calc_fee(OrderSide.BUY, 25_001.0)
        assert below.commission == pytest.approx(5.0, abs=1e-9)
        assert above.commission == pytest.approx(25_001.0 * 0.0002, abs=1e-9)


# ═══════════════════════════════════════════════
#  2. 印花税 / 过户费
# ═══════════════════════════════════════════════

class TestStampAndTransfer:
    """印花税与过户费"""

    def test_stamp_tax_buy_zero(self, default_calc: FeeCalculator) -> None:
        """买入印花税为 0"""
        bd = default_calc.calc_fee(OrderSide.BUY, 100_000.0)
        assert bd.stamp_tax == 0.0

    def test_stamp_tax_sell_rate(self, default_calc: FeeCalculator) -> None:
        """卖出印花税 = 金额 × 0.001"""
        bd = default_calc.calc_fee(OrderSide.SELL, 100_000.0)
        assert bd.stamp_tax == pytest.approx(100.0, abs=1e-9)

    def test_transfer_fee_both_side(self, default_calc: FeeCalculator) -> None:
        """过户费 = 金额 × 0.00001（买卖相同）"""
        bd_buy = default_calc.calc_fee(OrderSide.BUY, 100_000.0)
        bd_sell = default_calc.calc_fee(OrderSide.SELL, 100_000.0)
        assert bd_buy.transfer_fee == pytest.approx(1.0, abs=1e-9)
        assert bd_sell.transfer_fee == pytest.approx(1.0, abs=1e-9)


# ═══════════════════════════════════════════════
#  3. 滑点
# ═══════════════════════════════════════════════

class TestSlippage:
    """滑点计算（默认 fixed_bps=2.0）"""

    def test_slippage_per_share(self, default_calc: FeeCalculator) -> None:
        """固定 2bps：10 元 × 2/10000 = 0.002 元/股"""
        sp = default_calc.calc_slippage_per_share(10.0)
        assert sp == pytest.approx(0.002, abs=1e-9)

    def test_slippage_amount(self, default_calc: FeeCalculator) -> None:
        """滑点总额 = 每股滑点 × 数量"""
        sa = default_calc.calc_slippage_amount(10.0, quantity=1_000.0)
        assert sa == pytest.approx(2.0, abs=1e-9)

    def test_calc_fee_includes_slippage(self, default_calc: FeeCalculator) -> None:
        """一站式 calc_fee 滑点字段正确"""
        bd = default_calc.calc_fee(OrderSide.BUY, 10_000.0, price=10.0, quantity=1_000.0)
        assert bd.slippage == pytest.approx(2.0, abs=1e-9)


# ═══════════════════════════════════════════════
#  4. 一站式 calc_fee
# ═══════════════════════════════════════════════

class TestCalcFee:
    """一站式费用计算"""

    def test_calc_fee_buy_total(self, default_calc: FeeCalculator) -> None:
        """买入总佣金 = 佣金 + 过户费（无印花税）"""
        bd = default_calc.calc_fee(OrderSide.BUY, 100_000.0)
        assert bd.commission == pytest.approx(20.0, abs=1e-9)
        assert bd.transfer_fee == pytest.approx(1.0, abs=1e-9)
        assert bd.stamp_tax == 0.0
        assert bd.total_commission == pytest.approx(21.0, abs=1e-9)

    def test_calc_fee_sell_total(self, default_calc: FeeCalculator) -> None:
        """卖出总佣金 = 佣金 + 印花税 + 过户费"""
        bd = default_calc.calc_fee(OrderSide.SELL, 100_000.0)
        assert bd.total_commission == pytest.approx(121.0, abs=1e-9)

    def test_calc_fee_total_cost(self, default_calc: FeeCalculator) -> None:
        """总成本 = 佣金 + 印花税 + 过户费 + 滑点"""
        bd = default_calc.calc_fee(OrderSide.BUY, 10_000.0, price=10.0, quantity=1_000.0)
        # 佣金 2.0(≥5→5.0) + 过户费 0.1 + 滑点 2.0
        assert bd.total_commission == pytest.approx(5.0 + 0.1, abs=1e-9)
        assert bd.total_cost == pytest.approx(5.1 + 2.0, abs=1e-9)

    def test_module_level_calculate_fee(self) -> None:
        """模块级 calculate_fee 与实例一致"""
        from myquant.backtest.fee_common import calculate_fee

        bd = calculate_fee(OrderSide.SELL, 100_000.0)
        assert bd.total_commission == pytest.approx(121.0, abs=1e-9)


# ═══════════════════════════════════════════════
#  5. 向量化费用
# ═══════════════════════════════════════════════

class TestVectorizedFee:
    """向量化费用与标量一致性"""

    def test_normalize_side_array(self) -> None:
        """方向数组规范化：0=买, 1=卖"""
        arr = normalize_side_array(np.array(["BUY", "SELL", "buy", "sell"]))
        assert arr.tolist() == [0, 1, 0, 1]
        assert arr.dtype == np.int8

    def test_vectorized_vs_scalar_buy(self, default_calc: FeeCalculator) -> None:
        """买入向量化 vs 标量一致"""
        notional = np.array([1_000.0, 25_000.0, 100_000.0, 500_000.0])
        vr = default_calc.calc_fee_vectorized(OrderSide.BUY, notional)
        for i, n in enumerate(notional):
            sd = default_calc.calc_fee(OrderSide.BUY, float(n))
            assert vr.commission[i] == pytest.approx(sd.commission, abs=1e-6)
            assert vr.stamp_tax[i] == pytest.approx(sd.stamp_tax, abs=1e-6)
            assert vr.transfer_fee[i] == pytest.approx(sd.transfer_fee, abs=1e-6)

    def test_vectorized_vs_scalar_sell(self, default_calc: FeeCalculator) -> None:
        """卖出向量化 vs 标量一致（含印花税）"""
        notional = np.array([1_000.0, 25_000.0, 100_000.0])
        vr = default_calc.calc_fee_vectorized(OrderSide.SELL, notional)
        for i, n in enumerate(notional):
            sd = default_calc.calc_fee(OrderSide.SELL, float(n))
            assert vr.stamp_tax[i] == pytest.approx(sd.stamp_tax, abs=1e-6)

    def test_vectorized_mixed_sides(self, default_calc: FeeCalculator) -> None:
        """买卖混合批量：方向数组逐元素应用"""
        sides = np.array([0, 1, 0, 1], dtype=np.int8)  # 买 卖 买 卖
        notional = np.array([50_000.0, 80_000.0, 30_000.0, 200_000.0])
        vr = default_calc.calc_fee_vectorized(sides, notional)
        for i, (s, n) in enumerate(zip(sides.tolist(), notional.tolist())):
            sd = default_calc.calc_fee(OrderSide.BUY if s == 0 else OrderSide.SELL, float(n))
            assert vr.stamp_tax[i] == pytest.approx(sd.stamp_tax, abs=1e-6)
            assert vr.commission[i] == pytest.approx(sd.commission, abs=1e-6)

    def test_assert_vectorized_consistent(self, default_calc: FeeCalculator) -> None:
        """内置一致性自检不抛异常"""
        notional = np.array([500.0, 5_000.0, 25_000.0, 100_000.0, 800_000.0])
        default_calc.assert_vectorized_consistent(OrderSide.BUY, notional)
        default_calc.assert_vectorized_consistent(OrderSide.SELL, notional)

    def test_vectorized_empty(self, default_calc: FeeCalculator) -> None:
        """空数组返回空结果"""
        vr = default_calc.calc_fee_vectorized(OrderSide.BUY, np.array([]))
        assert vr.commission.size == 0

    def test_vectorized_shape_mismatch(self, default_calc: FeeCalculator) -> None:
        """数组形状不匹配抛 ValueError"""
        notional = np.array([1_000.0, 2_000.0])
        price = np.array([10.0])
        with pytest.raises(ValueError):
            default_calc.calc_fee_vectorized(OrderSide.BUY, notional, price=price)


# ═══════════════════════════════════════════════
#  6. 防御式校验
# ═══════════════════════════════════════════════

class TestDefensive:
    """非法输入防御"""

    def test_invalid_side(self, default_calc: FeeCalculator) -> None:
        """非法方向抛 ValueError"""
        with pytest.raises(ValueError):
            default_calc.calc_commission_breakdown("HOLD", 1_000.0)

    def test_negative_notional(self, default_calc: FeeCalculator) -> None:
        """负金额抛 ValueError"""
        with pytest.raises(ValueError):
            default_calc.calc_commission_breakdown(OrderSide.BUY, -100.0)

    def test_invalid_model(self) -> None:
        """非法模型类型抛 TypeError"""
        with pytest.raises(TypeError):
            FeeCalculator(commission_model="bad")  # type: ignore[arg-type]

    def test_get_default_calculator(self) -> None:
        """模块级默认计算器可用"""
        assert isinstance(get_default_calculator(), FeeCalculator)

    def test_custom_models(self) -> None:
        """自定义佣金/滑点模型生效"""
        cm = CommissionModel(buy_rate=0.001, sell_rate=0.001, min_commission=1.0)
        sm = SlippageModel(type="fixed_bps", fixed_bps=10.0)
        calc = FeeCalculator(commission_model=cm, slippage_model=sm)
        bd = calc.calc_commission_breakdown(OrderSide.BUY, 10_000.0)
        # 返回 tuple (commission, stamp_tax, transfer_fee)
        assert bd[0] == pytest.approx(10.0, abs=1e-9)  # 10000 × 0.001
        assert calc.calc_slippage_per_share(10.0) == pytest.approx(0.01, abs=1e-9)  # 10 × 10/10000


# ═══════════════════════════════════════════════
#  7. 查表加速（fee_table）
# ═══════════════════════════════════════════════

class TestFeeTable:
    """费用查表加速"""

    def test_disabled_by_default(self) -> None:
        """默认关闭查表"""
        assert is_table_lookup_enabled() is False

    def test_enable_then_disable(self) -> None:
        """开启后启用，可恢复关闭"""
        assert enable_table_lookup(True) is True
        assert is_table_lookup_enabled() is True
        assert enable_table_lookup(False) is False
        assert is_table_lookup_enabled() is False

    def test_grid_monotonic(self) -> None:
        """网格单调递增"""
        table = build_fee_table()
        diffs = np.diff(table.grid)
        assert np.all(diffs > 0)

    def test_grid_contains_breakpoint(self) -> None:
        """佣金拐点 25000 元（5/0.0002）强制入网"""
        table = build_fee_table()
        bp = 5.0 / 0.0002  # 25000.0
        nearest = table.grid[np.argmin(np.abs(table.grid - bp))]
        assert nearest == pytest.approx(bp, abs=1e-6)

    def test_table_lookup_vs_exact(self) -> None:
        """查表 vs 精确计算相对误差 < 0.1%（随机 2000 点对拍）"""
        table = build_fee_table()
        exact = FeeCalculator()
        rng = np.random.default_rng(42)
        # 采样范围限定在查表域内 [DEFAULT_MIN_AMOUNT=1000, 1e6]，避免越界 clamp 引入误差
        amounts = rng.uniform(1_000.0, 1_000_000.0, size=2000)
        max_rel = 0.0
        for n in amounts:
            for side in (OrderSide.BUY, OrderSide.SELL):
                tc = table.lookup_commission_breakdown(side, float(n))
                ec = exact.calc_commission_breakdown(side, float(n))
                denom = max(sum(ec), 1e-9)
                rel = abs(sum(tc) - sum(ec)) / denom
                max_rel = max(max_rel, rel)
        assert max_rel < 0.001

    def test_table_fee_calculator_vs_fee_calculator(self) -> None:
        """TableFeeCalculator 与 FeeCalculator 对拍一致（默认构造）"""
        tbl_calc = TableFeeCalculator()
        exact = FeeCalculator()
        for n in (1_000.0, 25_000.0, 100_000.0, 800_000.0):
            tb = tbl_calc.calc_commission_breakdown(OrderSide.SELL, n)
            eb = exact.calc_commission_breakdown(OrderSide.SELL, n)
            assert sum(tb) == pytest.approx(sum(eb), rel=1e-3)

    def test_table_lookup_vectorized(self) -> None:
        """向量化查表与标量查表一致"""
        table = build_fee_table()
        amounts = np.array([1_000.0, 25_000.0, 100_000.0])
        vc, vt, vf = table.lookup_commission_breakdown_vectorized(OrderSide.BUY, amounts)
        for i, n in enumerate(amounts):
            sc, st, sf = table.lookup_commission_breakdown(OrderSide.BUY, float(n))
            assert vc[i] == pytest.approx(sc, abs=1e-9)
            assert vf[i] == pytest.approx(sf, abs=1e-9)
            assert vt[i] == 0.0

    def test_build_invalid_args(self) -> None:
        """参数非法抛 ValueError"""
        with pytest.raises(ValueError):
            build_fee_table(min_amount=0.0)
        with pytest.raises(ValueError):
            build_fee_table(min_amount=100_000.0, max_amount=10_000.0)
        with pytest.raises(ValueError):
            build_fee_table(bins=1)

    def test_enable_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """开启查表输出精度警告"""
        import logging

        with caplog.at_level(logging.WARNING, logger="myquant.backtest.fee_table"):
            enable_table_lookup(True)
        assert "费用查表加速已启用" in caplog.text
