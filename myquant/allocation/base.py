# 文件名：myquant/allocation/base.py
"""
base.py - 资金分配器抽象基类
==============================
定义资金分配器的统一接口和分配结果数据结构。

核心能力:
  1. AllocationResult — 分配结果数据类（权重/金额/校验）
  2. AllocatorBase — 分配器抽象基类（统一接口）
  3. 分配结果校验 — 权重和=1、非负、单资产上下限
  4. 权重归一化 — 自动归一化到和为1

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（动态再平衡/约束条件/风险预算全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前已知的资产/策略风险数据

防未来函数说明:
  - 分配只使用当前已知的资产/策略列表、风险数据（波动率/协方差）
  - 不使用任何未来收益数据或预判未来走势
  - 风险平价只基于历史波动率（当前已知），不预测未来波动
  - 再平衡只基于当前持仓和目标权重，不预判未来价格

v1实现范围:
  - AllocationResult 分配结果数据类
  - AllocatorBase 抽象基类
  - 分配结果校验（权重和=1、非负、单资产上下限）
  - 权重归一化
  - 约束条件支持（单资产上限/下限）
  - 再平衡接口（rebalance）

TODO(后续迭代):
  - target_volatility.py 目标波动率分配
  - Black-Litterman 模型
  - 因子风险平价
  - 均值-方差优化（Markowitz）
  - 风险预算（Risk Budgeting）
  - 动态再平衡调度（定时/阈值触发）
  - 交易成本优化（再平衡时考虑手续费/滑点）
  - 多账户资金分配
  - 分配结果持久化

依赖: typing, dataclasses, abc, myquant.core.logger
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from myquant.core.logger import get_logger

logger = get_logger("allocation.base")


# ═══════════════════════════════════════════════
#  分配结果数据类
# ═══════════════════════════════════════════════

@dataclass
class AllocationResult:
    """
    资金分配结果

    Attributes
    ----------
    allocator_name : str
        分配器名称
    assets : List[str]
        资产/策略名称列表
    weights : Dict[str, float]
        各资产分配权重（0~1，和为1）
    amounts : Dict[str, float]
        各资产分配金额（= weights * total_capital）
    total_capital : float
        总资金
    metadata : Dict[str, Any]
        额外信息（风险贡献、迭代次数等）
    """
    allocator_name: str = ""
    assets: List[str] = field(default_factory=list)
    weights: Dict[str, float] = field(default_factory=dict)
    amounts: Dict[str, float] = field(default_factory=dict)
    total_capital: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """初始化后自动计算金额（如果weights和total_capital已设置）"""
        if self.weights and self.total_capital > 0 and not self.amounts:
            self.amounts = {
                asset: weight * self.total_capital
                for asset, weight in self.weights.items()
            }

    @property
    def weight_sum(self) -> float:
        """权重之和"""
        return sum(self.weights.values())

    @property
    def is_valid(self) -> bool:
        """分配是否有效（权重和≈1，所有权重≥0）"""
        if not self.weights:
            return False
        if abs(self.weight_sum - 1.0) > 1e-6:
            return False
        if any(w < -1e-9 for w in self.weights.values()):
            return False
        return True

    def get_weight(self, asset: str) -> float:
        """获取指定资产权重"""
        return self.weights.get(asset, 0.0)

    def get_amount(self, asset: str) -> float:
        """获取指定资产金额"""
        return self.amounts.get(asset, 0.0)

    def summary(self) -> str:
        """生成分配摘要文本"""
        lines = [
            f"分配器: {self.allocator_name}",
            f"总资金: {self.total_capital:,.2f}",
            f"资产数: {len(self.assets)}",
            f"权重和: {self.weight_sum:.6f}",
            f"有效性: {'✅有效' if self.is_valid else '❌无效'}",
            "─" * 40,
            f"{'资产':<15} {'权重':>10} {'金额':>15}",
        ]
        for asset in self.assets:
            w = self.weights.get(asset, 0)
            a = self.amounts.get(asset, 0)
            lines.append(f"{asset:<15} {w*100:>9.2f}% {a:>15,.2f}")
        if self.metadata:
            lines.append("─" * 40)
            lines.append("元数据:")
            for k, v in self.metadata.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"AllocationResult(allocator={self.allocator_name}, "
            f"assets={len(self.assets)}, "
            f"valid={self.is_valid})"
        )


# ═══════════════════════════════════════════════
#  分配约束
# ═══════════════════════════════════════════════

@dataclass
class AllocationConstraints:
    """
    分配约束条件

    Attributes
    ----------
    min_weight : float
        单资产最小权重（默认0，不允许做空）
    max_weight : float
        单资产最大权重（默认1，无上限）
    asset_min_weights : Dict[str, float]
        各资产单独最小权重（覆盖全局min_weight）
    asset_max_weights : Dict[str, float]
        各资产单独最大权重（覆盖全局max_weight）
    exclude_assets : List[str]
        排除的资产列表（权重强制为0）
    """
    min_weight: float = 0.0
    max_weight: float = 1.0
    asset_min_weights: Dict[str, float] = field(default_factory=dict)
    asset_max_weights: Dict[str, float] = field(default_factory=dict)
    exclude_assets: List[str] = field(default_factory=list)

    def get_min_weight(self, asset: str) -> float:
        """获取指定资产最小权重"""
        if asset in self.exclude_assets:
            return 0.0
        return self.asset_min_weights.get(asset, self.min_weight)

    def get_max_weight(self, asset: str) -> float:
        """获取指定资产最大权重"""
        if asset in self.exclude_assets:
            return 0.0
        return self.asset_max_weights.get(asset, self.max_weight)

    def is_excluded(self, asset: str) -> bool:
        """资产是否被排除"""
        return asset in self.exclude_assets


# ═══════════════════════════════════════════════
#  分配器抽象基类
# ═══════════════════════════════════════════════

class AllocatorBase(ABC):
    """
    资金分配器抽象基类

    所有分配器必须继承此类，实现 allocate 方法。

    使用方式:
        from myquant.allocation import EqualWeightAllocator

        allocator = EqualWeightAllocator()
        result = allocator.allocate(
            assets=["000001.SZ", "600000.SH", "600519.SH"],
            total_capital=100000,
        )
        print(result.summary())

    与实盘引擎集成:
        # engine.py 中
        self.allocator = EqualWeightAllocator()

        def on_rebalance(self):
            # 再平衡日调用分配器
            result = self.allocator.allocate(
                assets=self.strategy_symbols,
                total_capital=self.portfolio.total_assets,
                constraints=self.allocation_constraints,
            )
            # 根据分配结果生成调仓订单
            self._generate_rebalance_orders(result)
    """

    name: str = "base_allocator"
    description: str = ""

    def __init__(self, constraints: Optional[AllocationConstraints] = None):
        """
        初始化分配器

        Parameters
        ----------
        constraints : AllocationConstraints | None
            分配约束条件（None则使用默认约束）
        """
        self.constraints = constraints or AllocationConstraints()

    @abstractmethod
    def allocate(
        self,
        assets: Sequence[str],
        total_capital: float,
        risk_data: Optional[Dict[str, Any]] = None,
        constraints: Optional[AllocationConstraints] = None,
        **kwargs: Any,
    ) -> AllocationResult:
        """
        执行资金分配（抽象方法）

        Parameters
        ----------
        assets : Sequence[str]
            资产/策略名称列表
        total_capital : float
            总资金
        risk_data : Dict | None
            风险数据（波动率、协方差矩阵等，不同分配器需要不同数据）
        constraints : AllocationConstraints | None
            分配约束（None则使用实例默认约束）
        **kwargs : Any
            分配器特定参数

        Returns
        -------
        AllocationResult : 分配结果
        """
        pass

    # ═══════════════════════════════════════════
    #  分配结果校验
    # ═══════════════════════════════════════════

    def validate(
        self,
        result: AllocationResult,
        tolerance: float = 1e-6,
        constraints: Optional[AllocationConstraints] = None,
    ) -> List[str]:
        """
        校验分配结果

        Parameters
        ----------
        result : AllocationResult
            待校验的分配结果
        tolerance : float
            数值容差
        constraints : AllocationConstraints | None
            校验用的约束条件（None则使用实例默认约束）

        Returns
        -------
        List[str] : 错误列表（空列表=校验通过）
        """
        errors: List[str] = []

        if not result.weights:
            errors.append("分配结果为空")
            return errors

        # 权重和校验
        weight_sum = result.weight_sum
        if abs(weight_sum - 1.0) > tolerance:
            errors.append(f"权重和={weight_sum:.6f}，不等于1.0（容差={tolerance}）")

        # 非负校验
        for asset, weight in result.weights.items():
            if weight < -tolerance:
                errors.append(f"资产{asset}权重为负: {weight}")

        # 约束校验
        active_constraints = constraints or self.constraints
        for asset, weight in result.weights.items():
            min_w = active_constraints.get_min_weight(asset)
            max_w = active_constraints.get_max_weight(asset)
            if active_constraints.is_excluded(asset) and weight > tolerance:
                errors.append(f"资产{asset}被排除，但权重={weight}")
            if weight < min_w - tolerance:
                errors.append(f"资产{asset}权重={weight:.4f} < 最小权重={min_w:.4f}")
            if weight > max_w + tolerance:
                errors.append(f"资产{asset}权重={weight:.4f} > 最大权重={max_w:.4f}")

        if errors:
            logger.warning(f"[allocation] 分配校验失败: {errors}")
        else:
            logger.debug(f"[allocation] 分配校验通过: {result.allocator_name}")

        return errors

    # ═══════════════════════════════════════════
    #  权重归一化
    # ═══════════════════════════════════════════

    @staticmethod
    def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        """
        归一化权重（使权重和为1）

        Parameters
        ----------
        weights : Dict[str, float]
            原始权重

        Returns
        -------
        Dict[str, float] : 归一化后的权重
        """
        total = sum(weights.values())
        if total <= 0:
            n = len(weights)
            return {k: 1.0 / n for k in weights} if n > 0 else {}
        return {k: v / total for k, v in weights.items()}

    # ═══════════════════════════════════════════
    #  应用约束（裁剪到上下限）
    # ═══════════════════════════════════════════

    def apply_constraints(
        self,
        weights: Dict[str, float],
        constraints: Optional[AllocationConstraints] = None,
    ) -> Dict[str, float]:
        """
        应用约束条件（裁剪权重到上下限，然后归一化）

        Parameters
        ----------
        weights : Dict[str, float]
            原始权重
        constraints : AllocationConstraints | None
            约束条件（None则使用实例默认约束）

        Returns
        -------
        Dict[str, float] : 应用约束后的权重
        """
        c = constraints or self.constraints
        result = {}
        for asset, weight in weights.items():
            if c.is_excluded(asset):
                result[asset] = 0.0
            else:
                min_w = c.get_min_weight(asset)
                max_w = c.get_max_weight(asset)
                result[asset] = max(min_w, min(max_w, weight))
        # 归一化
        return self.normalize_weights(result)

    # ═══════════════════════════════════════════
    #  再平衡（基于当前持仓和目标权重）
    # ═══════════════════════════════════════════

    def rebalance(
        self,
        target_weights: Dict[str, float],
        current_weights: Dict[str, float],
        threshold: float = 0.0,
    ) -> Dict[str, float]:
        """
        计算再平衡调仓量

        Parameters
        ----------
        target_weights : Dict[str, float]
            目标权重
        current_weights : Dict[str, float]
            当前权重
        threshold : float
            再平衡阈值（偏差小于此值的资产不调整，默认0=全部调整）

        Returns
        -------
        Dict[str, float] : 各资产权重调整量（正=加仓，负=减仓）
        """
        adjustments = {}
        all_assets = set(target_weights.keys()) | set(current_weights.keys())
        for asset in all_assets:
            target = target_weights.get(asset, 0.0)
            current = current_weights.get(asset, 0.0)
            diff = target - current
            if abs(diff) <= threshold:
                adjustments[asset] = 0.0
            else:
                adjustments[asset] = diff
        return adjustments

    # ═══════════════════════════════════════════
    #  构建分配结果
    # ═══════════════════════════════════════════

    def _build_result(
        self,
        assets: Sequence[str],
        weights: Dict[str, float],
        total_capital: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AllocationResult:
        """
        构建分配结果（内部方法）

        Parameters
        ----------
        assets : Sequence[str]
            资产列表
        weights : Dict[str, float]
            权重字典
        total_capital : float
            总资金
        metadata : Dict | None
            额外信息

        Returns
        -------
        AllocationResult : 分配结果
        """
        amounts = {
            asset: weights.get(asset, 0.0) * total_capital
            for asset in assets
        }
        return AllocationResult(
            allocator_name=self.name,
            assets=list(assets),
            weights=dict(weights),
            amounts=amounts,
            total_capital=total_capital,
            metadata=metadata or {},
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name})"
