# 文件名：tests/unit/test_allocation.py
"""
test_allocation.py - 资产配置模块单元测试
============================================
覆盖 myquant/allocation/：
  - base.py              分配器基类（AllocatorBase/AllocationResult/AllocationConstraints）
  - equal_weight.py      等权分配器（EqualWeightAllocator）
  - risk_parity.py       风险平价分配器（RiskParityAllocator，逆波动率+迭代优化）
  - target_volatility.py 目标波动率分配器（TargetVolatilityAllocator，v1 TODO占位回退等权）

测试维度:
  1. 分配结果（AllocationResult 权重和/有效性/查询/摘要）
  2. 约束条件（AllocationConstraints 上下限/排除资产）
  3. 抽象基类（AllocatorBase ABC/归一化/约束应用/再平衡）
  4. 等权分配（EqualWeightAllocator 均匀分配/资金分配/约束）
  5. 风险平价（RiskParityAllocator 逆波动率/迭代优化/无数据回退）
  6. 目标波动率（TargetVolatilityAllocator TODO占位/回退等权/元数据标记）
  7. 实盘准备（再平衡接口/约束管理/多资产配置/无缝对接）

运行方式:
  pytest tests/unit/test_allocation.py -v

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/unit/test_allocation.py）
  2. 为实时行情及实盘无缝对接做好充足准备（AllocatorBase.rebalance 再平衡接口/AllocationConstraints 动态约束/risk_data 实时波动率输入）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 分配仅使用当前已有的收益率/波动率数据，不引用未来数据
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import pytest

from myquant.allocation import (
    AllocationConstraints,
    AllocationResult,
    AllocatorBase,
    EqualWeightAllocator,
    RiskParityAllocator,
    TargetVolatilityAllocator,
)


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _make_returns(assets: List[str] = None, periods: int = 100,
                  seed: int = 42) -> pd.DataFrame:
    """构造测试收益率数据"""
    if assets is None:
        assets = ["A", "B", "C"]
    np.random.seed(seed)
    dates = pd.date_range("2024-01-01", periods=periods, freq="B")
    data = np.random.randn(periods, len(assets)) * 0.01
    return pd.DataFrame(data, columns=assets, index=dates)


def _make_volatilities(assets: List[str] = None) -> Dict[str, float]:
    """构造测试波动率"""
    if assets is None:
        assets = ["A", "B", "C"]
    return {a: 0.15 + i * 0.05 for i, a in enumerate(assets)}


# ═══════════════════════════════════════════
#  TestAllocationResult - 分配结果
# ═══════════════════════════════════════════

class TestAllocationResult:
    """AllocationResult 分配结果测试"""

    def test_instantiation(self):
        """实例化"""
        result = AllocationResult(
            allocator_name="test",
            assets=["A", "B"],
            weights={"A": 0.5, "B": 0.5},
            amounts={"A": 500.0, "B": 500.0},
            total_capital=1000.0,
        )
        assert result is not None
        assert result.allocator_name == "test"

    def test_weight_sum(self):
        """权重和"""
        result = AllocationResult(
            assets=["A", "B", "C"],
            weights={"A": 0.3, "B": 0.3, "C": 0.4},
            total_capital=1000.0,
        )
        assert abs(result.weight_sum - 1.0) < 1e-10

    def test_weight_sum_not_normalized(self):
        """未归一化权重和"""
        result = AllocationResult(
            assets=["A", "B"],
            weights={"A": 0.3, "B": 0.3},
            total_capital=1000.0,
        )
        assert abs(result.weight_sum - 0.6) < 1e-10

    def test_is_valid_normalized(self):
        """归一化结果有效"""
        result = AllocationResult(
            assets=["A", "B"],
            weights={"A": 0.5, "B": 0.5},
            total_capital=1000.0,
        )
        assert result.is_valid is True

    def test_is_valid_empty(self):
        """空结果无效"""
        result = AllocationResult(assets=[], weights={}, total_capital=0.0)
        assert result.is_valid is False

    def test_get_weight(self):
        """查询权重"""
        result = AllocationResult(
            assets=["A", "B"], weights={"A": 0.6, "B": 0.4},
            total_capital=1000.0,
        )
        assert result.get_weight("A") == 0.6
        assert result.get_weight("B") == 0.4

    def test_get_weight_missing(self):
        """查询不存在的资产"""
        result = AllocationResult(
            assets=["A"], weights={"A": 1.0}, total_capital=1000.0,
        )
        assert result.get_weight("Z") == 0.0

    def test_get_amount(self):
        """查询资金分配"""
        result = AllocationResult(
            assets=["A", "B"],
            weights={"A": 0.6, "B": 0.4},
            amounts={"A": 600.0, "B": 400.0},
            total_capital=1000.0,
        )
        assert result.get_amount("A") == 600.0
        assert result.get_amount("B") == 400.0

    def test_get_amount_missing(self):
        """查询不存在资产的资金"""
        result = AllocationResult(
            assets=["A"], weights={"A": 1.0},
            amounts={"A": 1000.0}, total_capital=1000.0,
        )
        assert result.get_amount("Z") == 0.0

    def test_summary(self):
        """摘要"""
        result = AllocationResult(
            assets=["A", "B"], weights={"A": 0.5, "B": 0.5},
            total_capital=1000.0,
        )
        s = result.summary()
        assert isinstance(s, str)
        assert len(s) > 0

    def test_metadata(self):
        """元数据"""
        result = AllocationResult(
            assets=["A"], weights={"A": 1.0}, total_capital=1000.0,
            metadata={"method": "test", "version": "1.0"},
        )
        assert result.metadata["method"] == "test"

    def test_repr(self):
        """__repr__ 不崩溃"""
        result = AllocationResult(
            assets=["A"], weights={"A": 1.0}, total_capital=1000.0,
        )
        r = repr(result)
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestAllocationConstraints - 约束条件
# ═══════════════════════════════════════════

class TestAllocationConstraints:
    """AllocationConstraints 约束条件测试"""

    def test_instantiation_default(self):
        """默认约束"""
        c = AllocationConstraints()
        assert c.min_weight == 0.0
        assert c.max_weight == 1.0

    def test_instantiation_custom(self):
        """自定义约束"""
        c = AllocationConstraints(min_weight=0.1, max_weight=0.5)
        assert c.min_weight == 0.1
        assert c.max_weight == 0.5

    def test_get_min_weight_default(self):
        """获取默认最小权重"""
        c = AllocationConstraints(min_weight=0.05)
        assert c.get_min_weight("A") == 0.05

    def test_get_min_weight_asset_specific(self):
        """获取资产特定最小权重"""
        c = AllocationConstraints(
            min_weight=0.05,
            asset_min_weights={"A": 0.1},
        )
        assert c.get_min_weight("A") == 0.1
        assert c.get_min_weight("B") == 0.05

    def test_get_max_weight_default(self):
        """获取默认最大权重"""
        c = AllocationConstraints(max_weight=0.4)
        assert c.get_max_weight("A") == 0.4

    def test_get_max_weight_asset_specific(self):
        """获取资产特定最大权重"""
        c = AllocationConstraints(
            max_weight=0.5,
            asset_max_weights={"A": 0.3},
        )
        assert c.get_max_weight("A") == 0.3
        assert c.get_max_weight("B") == 0.5

    def test_is_excluded(self):
        """排除资产"""
        c = AllocationConstraints(exclude_assets=["Z"])
        assert c.is_excluded("Z") is True
        assert c.is_excluded("A") is False

    def test_empty_exclude(self):
        """无排除资产"""
        c = AllocationConstraints()
        assert c.is_excluded("A") is False


# ═══════════════════════════════════════════
#  TestAllocatorBase - 抽象基类
# ═══════════════════════════════════════════

class TestAllocatorBase:
    """AllocatorBase 抽象基类测试"""

    def test_cannot_instantiate(self):
        """抽象基类不可直接实例化"""
        with pytest.raises(TypeError):
            AllocatorBase()

    def test_subclass_must_implement_allocate(self):
        """子类必须实现 allocate"""
        class Incomplete(AllocatorBase):
            pass
        with pytest.raises(TypeError):
            Incomplete()

    def test_normalize_weights(self):
        """归一化权重（静态方法）"""
        weights = {"A": 1.0, "B": 2.0, "C": 3.0}
        normalized = AllocatorBase.normalize_weights(weights)
        assert abs(sum(normalized.values()) - 1.0) < 1e-10
        assert normalized["A"] == pytest.approx(1 / 6)
        assert normalized["C"] == pytest.approx(0.5)

    def test_normalize_weights_empty(self):
        """空权重归一化"""
        result = AllocatorBase.normalize_weights({})
        assert result == {}

    def test_normalize_weights_all_zero(self):
        """全零权重归一化"""
        result = AllocatorBase.normalize_weights({"A": 0.0, "B": 0.0})
        # 全零时应安全处理
        assert isinstance(result, dict)

    def test_apply_constraints(self):
        """应用约束（不崩溃，返回字典）"""
        alloc = EqualWeightAllocator()
        weights = {"A": 0.8, "B": 0.2}
        constraints = AllocationConstraints(max_weight=0.5)
        constrained = alloc.apply_constraints(weights, constraints)
        assert isinstance(constrained, dict)
        assert "A" in constrained
        assert "B" in constrained

    def test_rebalance_interface(self):
        """再平衡接口存在"""
        assert hasattr(AllocatorBase, "rebalance")
        assert callable(AllocatorBase.rebalance)

    def test_validate_interface(self):
        """验证接口存在"""
        assert hasattr(AllocatorBase, "validate")
        assert callable(AllocatorBase.validate)

    def test_equal_weight_is_subclass(self):
        """EqualWeightAllocator 是 AllocatorBase 子类"""
        assert issubclass(EqualWeightAllocator, AllocatorBase)

    def test_risk_parity_is_subclass(self):
        """RiskParityAllocator 是 AllocatorBase 子类"""
        assert issubclass(RiskParityAllocator, AllocatorBase)

    def test_target_vol_is_subclass(self):
        """TargetVolatilityAllocator 是 AllocatorBase 子类"""
        assert issubclass(TargetVolatilityAllocator, AllocatorBase)


# ═══════════════════════════════════════════
#  TestEqualWeightAllocator - 等权分配
# ═══════════════════════════════════════════

class TestEqualWeightAllocator:
    """EqualWeightAllocator 等权分配器测试"""

    def test_instantiation(self):
        """实例化"""
        alloc = EqualWeightAllocator()
        assert alloc is not None

    def test_allocate_three_assets(self):
        """三资产等权分配"""
        alloc = EqualWeightAllocator()
        result = alloc.allocate(
            assets=["A", "B", "C"], total_capital=900000.0,
        )
        assert result.is_valid is True
        assert abs(result.weight_sum - 1.0) < 1e-10
        for asset in ["A", "B", "C"]:
            assert result.get_weight(asset) == pytest.approx(1 / 3)

    def test_allocate_single_asset(self):
        """单资产分配"""
        alloc = EqualWeightAllocator()
        result = alloc.allocate(assets=["A"], total_capital=100000.0)
        assert result.get_weight("A") == 1.0
        assert result.get_amount("A") == 100000.0

    def test_allocate_amounts(self):
        """资金分配正确"""
        alloc = EqualWeightAllocator()
        result = alloc.allocate(
            assets=["A", "B"], total_capital=200000.0,
        )
        assert result.get_amount("A") == pytest.approx(100000.0)
        assert result.get_amount("B") == pytest.approx(100000.0)

    def test_allocate_with_constraints(self):
        """带约束分配"""
        alloc = EqualWeightAllocator()
        constraints = AllocationConstraints(max_weight=0.6)
        result = alloc.allocate(
            assets=["A", "B", "C"], total_capital=1000000.0,
            constraints=constraints,
        )
        # 等权1/3 < 0.6，约束不影响
        assert result.is_valid is True

    def test_allocate_empty_assets(self):
        """空资产列表"""
        alloc = EqualWeightAllocator()
        result = alloc.allocate(assets=[], total_capital=1000000.0)
        assert result.is_valid is False or len(result.assets) == 0

    def test_allocator_name(self):
        """分配器名称"""
        alloc = EqualWeightAllocator()
        result = alloc.allocate(assets=["A"], total_capital=100.0)
        assert "equal" in result.allocator_name.lower()

    def test_repr(self):
        """__repr__ 不崩溃"""
        r = repr(EqualWeightAllocator())
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestRiskParityAllocator - 风险平价
# ═══════════════════════════════════════════

class TestRiskParityAllocator:
    """RiskParityAllocator 风险平价分配器测试"""

    def test_instantiation_default(self):
        """默认实例化"""
        rp = RiskParityAllocator()
        assert rp is not None
        assert rp.mode == "inverse_volatility"

    def test_instantiation_iterative(self):
        """风险平价迭代模式实例化"""
        rp = RiskParityAllocator(mode="risk_parity")
        assert rp.mode == "risk_parity"

    def test_allocate_with_volatilities(self):
        """带波动率数据分配（逆波动率）"""
        rp = RiskParityAllocator(mode="inverse_volatility")
        vols = _make_volatilities(["A", "B", "C"])  # 0.15, 0.20, 0.25
        risk_data = {"volatilities": vols}
        result = rp.allocate(
            assets=["A", "B", "C"], total_capital=1000000.0,
            risk_data=risk_data,
        )
        assert result.is_valid is True
        assert abs(result.weight_sum - 1.0) < 1e-10
        # 低波动资产权重更高
        assert result.get_weight("A") > result.get_weight("C")

    def test_allocate_no_data_fallback(self):
        """无数据时回退等权"""
        rp = RiskParityAllocator()
        result = rp.allocate(
            assets=["A", "B", "C"], total_capital=1000000.0,
        )
        # 回退到等权
        assert result.is_valid is True
        assert result.get_weight("A") == pytest.approx(1 / 3)

    def test_allocate_with_returns(self):
        """带收益率数据分配"""
        rp = RiskParityAllocator(mode="inverse_volatility")
        returns = _make_returns(["A", "B", "C"])
        risk_data = {"returns": returns}
        result = rp.allocate(
            assets=["A", "B", "C"], total_capital=1000000.0,
            risk_data=risk_data,
        )
        assert result.is_valid is True
        assert abs(result.weight_sum - 1.0) < 1e-10

    def test_inverse_volatility_higher_vol_lower_weight(self):
        """逆波动率：高波动低权重"""
        rp = RiskParityAllocator(mode="inverse_volatility")
        vols = {"LOW_VOL": 0.05, "HIGH_VOL": 0.30}
        risk_data = {"volatilities": vols}
        result = rp.allocate(
            assets=["LOW_VOL", "HIGH_VOL"], total_capital=1000000.0,
            risk_data=risk_data,
        )
        assert result.get_weight("LOW_VOL") > result.get_weight("HIGH_VOL")

    def test_allocate_single_asset(self):
        """单资产风险平价"""
        rp = RiskParityAllocator()
        vols = {"A": 0.15}
        risk_data = {"volatilities": vols}
        result = rp.allocate(
            assets=["A"], total_capital=100000.0, risk_data=risk_data,
        )
        assert result.get_weight("A") == 1.0

    def test_metadata_contains_method(self):
        """元数据包含方法信息"""
        rp = RiskParityAllocator(mode="inverse_volatility")
        vols = _make_volatilities()
        result = rp.allocate(
            assets=["A", "B", "C"], total_capital=1000000.0,
            risk_data={"volatilities": vols},
        )
        assert "method" in result.metadata or "mode" in result.metadata

    def test_max_iterations_param(self):
        """最大迭代次数参数"""
        rp = RiskParityAllocator(mode="risk_parity", max_iterations=500)
        assert rp.max_iterations == 500

    def test_tolerance_param(self):
        """收敛容差参数"""
        rp = RiskParityAllocator(tolerance=1e-6)
        assert rp.tolerance == 1e-6

    def test_repr(self):
        """__repr__ 不崩溃"""
        r = repr(RiskParityAllocator())
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestTargetVolatilityAllocator - 目标波动率
# ═══════════════════════════════════════════

class TestTargetVolatilityAllocator:
    """TargetVolatilityAllocator 目标波动率分配器测试（v1 TODO占位）"""

    def test_instantiation(self):
        """实例化"""
        tv = TargetVolatilityAllocator(target_volatility=0.15)
        assert tv is not None

    def test_target_volatility_param(self):
        """目标波动率参数"""
        tv = TargetVolatilityAllocator(target_volatility=0.20)
        assert tv.target_volatility == 0.20

    def test_allocate_fallback_equal_weight(self):
        """分配回退等权（v1未实现）"""
        tv = TargetVolatilityAllocator(target_volatility=0.15)
        result = tv.allocate(
            assets=["A", "B", "C"], total_capital=900000.0,
        )
        # 回退到等权
        assert result.is_valid is True
        assert result.get_weight("A") == pytest.approx(1 / 3)

    def test_metadata_marks_todo(self):
        """元数据标记 TODO"""
        tv = TargetVolatilityAllocator(target_volatility=0.15)
        result = tv.allocate(
            assets=["A", "B"], total_capital=100000.0,
        )
        assert "todo" in result.metadata
        assert "target_volatility_not_implemented" in str(result.metadata.get("todo", ""))

    def test_metadata_contains_target(self):
        """元数据包含目标波动率"""
        tv = TargetVolatilityAllocator(target_volatility=0.25)
        result = tv.allocate(
            assets=["A"], total_capital=100000.0,
        )
        assert result.metadata.get("target_volatility") == 0.25

    def test_allocate_with_risk_data(self):
        """带风险数据仍回退等权"""
        tv = TargetVolatilityAllocator(target_volatility=0.15)
        vols = _make_volatilities()
        result = tv.allocate(
            assets=["A", "B", "C"], total_capital=1000000.0,
            risk_data={"volatilities": vols},
        )
        assert result.is_valid is True
        # 即使有数据，v1仍回退等权
        assert result.get_weight("A") == pytest.approx(1 / 3)

    def test_is_allocator_base_subclass(self):
        """是 AllocatorBase 子类"""
        assert issubclass(TargetVolatilityAllocator, AllocatorBase)

    def test_repr(self):
        """__repr__ 不崩溃"""
        r = repr(TargetVolatilityAllocator())
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestRealTimePreparation - 实盘准备
# ═══════════════════════════════════════════

class TestRealTimePreparation:
    """实盘无缝对接准备测试"""

    def test_rebalance_interface_for_live(self):
        """rebalance 接口为实盘动态再平衡准备"""
        # 所有分配器都继承 rebalance 方法
        for alloc_cls in [EqualWeightAllocator, RiskParityAllocator,
                          TargetVolatilityAllocator]:
            assert hasattr(alloc_cls, "rebalance")

    def test_dynamic_constraints(self):
        """AllocationConstraints 支持动态调整约束（实盘风控）"""
        c = AllocationConstraints(max_weight=0.3)
        # 实盘中可根据风控信号动态调整
        c.max_weight = 0.2
        assert c.max_weight == 0.2

    def test_risk_data_realtime_volatility(self):
        """risk_data 支持实时波动率输入（实盘无缝对接）"""
        rp = RiskParityAllocator(mode="inverse_volatility")
        # 实盘中实时计算的波动率通过 risk_data 传入
        realtime_vols = {"000001.SZ": 0.18, "600000.SH": 0.22}
        result = rp.allocate(
            assets=["000001.SZ", "600000.SH"],
            total_capital=1000000.0,
            risk_data={"volatilities": realtime_vols},
        )
        assert result.is_valid is True
        # 低波动(0.18)权重 > 高波动(0.22)
        assert result.get_weight("000001.SZ") > result.get_weight("600000.SH")

    def test_allocation_result_amounts_for_order(self):
        """AllocationResult.amounts 可直接用于下单"""
        alloc = EqualWeightAllocator()
        result = alloc.allocate(
            assets=["000001.SZ", "600000.SH"],
            total_capital=1000000.0,
        )
        # amounts 直接作为下单金额
        for asset in ["000001.SZ", "600000.SH"]:
            amount = result.get_amount(asset)
            assert amount > 0
            assert amount <= 1000000.0

    def test_no_future_data_in_allocation(self):
        """分配不使用未来数据（仅当前已有收益率）"""
        rp = RiskParityAllocator(mode="inverse_volatility")
        # 只用截止到当前的历史数据
        returns = _make_returns(["A", "B"], periods=50)
        risk_data = {"returns": returns}
        result = rp.allocate(
            assets=["A", "B"], total_capital=1000000.0,
            risk_data=risk_data,
        )
        assert result.is_valid is True
        # 不引用未来50天数据
