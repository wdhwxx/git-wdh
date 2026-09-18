# 文件名：myquant/allocation/equal_weight.py
"""
equal_weight.py - 等权分配器
==============================
最简单的资金分配方法：每个资产分配相同的权重 = 1/N。

核心能力:
  1. 等权分配 — 每个资产权重 = 1/N（N=资产数）
  2. 约束应用 — 自动应用单资产上下限/排除资产约束
  3. 结果校验 — 分配后自动校验权重和=1、非负、约束合规
  4. 空资产处理 — 资产列表为空时返回空结果
  5. 单资产处理 — 只有1个资产时权重=1.0

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（约束/再平衡/动态调整全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前已知的资产列表，不使用任何未来数据

防未来函数说明:
  - 等权分配只使用当前已知的资产列表，不使用任何收益/风险数据
  - 不预测未来收益、不预判未来走势
  - 约束条件只基于当前配置，不包含未来信息
  - 再平衡只基于当前持仓和目标权重，不预判未来价格

等权分配的优点:
  - 简单直观，无需估计参数
  - 避免集中度风险
  - 天然分散化
  - 不需要收益率/波动率预测

等权分配的缺点:
  - 不考虑资产风险差异（高波动和低波动同权）
  - 不考虑资产相关性
  - 再平衡频率高时交易成本较大

v1实现范围:
  - EqualWeightAllocator 类
  - 等权分配核心逻辑
  - 约束自动应用
  - 结果自动校验
  - 空/单资产边界处理

TODO(后续迭代):
  - 等权+波动率调整（Inverse Volatility Weighting）
  - 等权+流动性调整
  - 等权+行业中性约束
  - 智能再平衡（阈值触发+交易成本优化）
  - 分配结果与订单生成联动

依赖: typing, myquant.allocation.base, myquant.core.logger
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from myquant.allocation.base import (
    AllocationConstraints,
    AllocationResult,
    AllocatorBase,
)
from myquant.core.logger import get_logger

logger = get_logger("allocation.equal_weight")


# ═══════════════════════════════════════════════
#  等权分配器
# ═══════════════════════════════════════════════

class EqualWeightAllocator(AllocatorBase):
    """
    等权分配器

    每个资产分配相同的权重 = 1/N（N=资产数）。
    自动应用约束条件（单资产上下限、排除资产），然后归一化。

    使用方式:
        from myquant.allocation import EqualWeightAllocator

        # 基本用法
        allocator = EqualWeightAllocator()
        result = allocator.allocate(
            assets=["000001.SZ", "600000.SH", "600519.SH"],
            total_capital=100000,
        )
        print(result.summary())
        # 每个资产权重 = 1/3 ≈ 33.33%

        # 带约束
        from myquant.allocation import AllocationConstraints
        constraints = AllocationConstraints(
            max_weight=0.40,
            exclude_assets=["600519.SH"],
        )
        result = allocator.allocate(
            assets=["000001.SZ", "600000.SH", "600519.SH"],
            total_capital=100000,
            constraints=constraints,
        )
        # 600519.SH被排除，剩余2个资产等权各50%

    与实盘引擎集成:
        # engine.py 中
        self.allocator = EqualWeightAllocator()

        def on_rebalance(self):
            result = self.allocator.allocate(
                assets=self.tradable_symbols,
                total_capital=self.portfolio.total_assets,
                constraints=self.allocation_constraints,
            )
            # 生成调仓订单
            adjustments = self.allocator.rebalance(
                target_weights=result.weights,
                current_weights=self.portfolio.current_weights,
                threshold=0.02,  # 偏差超过2%才调整
            )
            self._submit_rebalance_orders(adjustments, result)
    """

    name = "equal_weight"
    description = "等权分配：每个资产分配相同权重 1/N"

    def __init__(self, constraints: Optional[AllocationConstraints] = None):
        """
        初始化等权分配器

        Parameters
        ----------
        constraints : AllocationConstraints | None
            分配约束条件（None则使用默认约束）
        """
        super().__init__(constraints=constraints)

    def allocate(
        self,
        assets: Sequence[str],
        total_capital: float,
        risk_data: Optional[Dict[str, Any]] = None,
        constraints: Optional[AllocationConstraints] = None,
        **kwargs: Any,
    ) -> AllocationResult:
        """
        执行等权分配

        流程:
          1. 过滤空资产列表
          2. 计算基础等权权重 = 1/N
          3. 应用约束条件（裁剪到上下限、排除资产）
          4. 归一化权重（使和为1）
          5. 构建分配结果
          6. 校验分配结果（日志记录，不抛异常）

        Parameters
        ----------
        assets : Sequence[str]
            资产/策略名称列表
        total_capital : float
            总资金
        risk_data : Dict | None
            风险数据（等权分配不需要，保留接口兼容）
        constraints : AllocationConstraints | None
            分配约束（None则使用实例默认约束）
        **kwargs : Any
            额外参数（等权分配不使用，保留接口兼容）

        Returns
        -------
        AllocationResult : 分配结果
        """
        # 1. 空资产列表处理
        asset_list = list(assets)
        if not asset_list:
            logger.warning("[equal_weight] 资产列表为空，返回空分配结果")
            return AllocationResult(
                allocator_name=self.name,
                assets=[],
                weights={},
                amounts={},
                total_capital=total_capital,
                metadata={"warning": "empty_assets"},
            )

        # 2. 计算基础等权权重
        n = len(asset_list)
        base_weight = 1.0 / n
        raw_weights = {asset: base_weight for asset in asset_list}

        # 3. 应用约束条件
        active_constraints = constraints or self.constraints
        constrained_weights = self.apply_constraints(raw_weights, active_constraints)

        # 4. 归一化（apply_constraints内部已归一化，这里确保）
        final_weights = self.normalize_weights(constrained_weights)

        # 5. 构建分配结果
        metadata = {
            "method": "equal_weight",
            "asset_count": n,
            "base_weight": base_weight,
            "constraints_applied": active_constraints is not None,
        }
        result = self._build_result(
            assets=asset_list,
            weights=final_weights,
            total_capital=total_capital,
            metadata=metadata,
        )

        # 6. 校验分配结果（日志记录，不抛异常）
        errors = self.validate(result, constraints=active_constraints)
        if errors:
            logger.warning(f"[equal_weight] 分配结果校验有警告: {errors}")
            result.metadata["validation_errors"] = errors
        else:
            logger.info(
                f"[equal_weight] 分配完成: {n}个资产, "
                f"每资产权重={base_weight*100:.2f}%, "
                f"总资金={total_capital:,.2f}"
            )

        return result
