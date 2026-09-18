# 文件名：myquant/allocation/__init__.py
"""
allocation - 组合资金分配模块
===============================
多策略/多资产资金分配，支持等权、风险平价等分配方法。

v1实现:
  - base.py: 分配器抽象基类 + 分配结果 + 约束条件
  - equal_weight.py: 等权分配器
  - risk_parity.py: 风险平价分配器（逆波动率加权 + 完整迭代优化）
  - target_volatility.py: TODO占位（后续迭代实现）

TODO(后续迭代):
  - target_volatility.py 目标波动率分配
  - Black-Litterman 模型
  - 均值-方差优化（Markowitz）
  - 风险预算（Risk Budgeting）
  - 因子风险平价
  - 动态再平衡调度
  - 交易成本优化
"""

from myquant.allocation.base import (
    AllocationResult,
    AllocationConstraints,
    AllocatorBase,
)
from myquant.allocation.equal_weight import EqualWeightAllocator
from myquant.allocation.risk_parity import RiskParityAllocator
from myquant.allocation.target_volatility import TargetVolatilityAllocator

__all__ = [
    "AllocationResult",
    "AllocationConstraints",
    "AllocatorBase",
    "EqualWeightAllocator",
    "RiskParityAllocator",
    "TargetVolatilityAllocator",
]
