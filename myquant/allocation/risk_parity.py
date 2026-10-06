# 文件名：myquant/allocation/risk_parity.py
"""
risk_parity.py - 风险平价分配器
==================================
风险平价（Risk Parity）资产配置：让每个资产对组合的风险贡献相等。

核心能力:
  1. 逆波动率加权（Inverse Volatility Weighting）— 最简单的风险平价
     - 权重 = (1/波动率_i) / Σ(1/波动率_j)
     - 高波动资产少配，低波动资产多配
  2. 完整风险平价（迭代优化）— 考虑资产间相关性
     - 通过迭代使每个资产的边际风险贡献相等
     - 支持协方差矩阵输入
  3. 约束应用 — 自动应用单资产上下限/排除资产约束
  4. 结果校验 — 分配后自动校验权重和=1、非负、约束合规
  5. 风险贡献计算 — 输出各资产风险贡献占比（metadata）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（动态波动率更新/再平衡/约束全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前已知的历史波动率/协方差，不预测未来波动

防未来函数说明:
  - 风险平价只使用当前已知的历史波动率/协方差矩阵
  - 不预测未来波动率、不预判未来走势
  - 波动率数据由调用方传入（基于历史数据计算），分配器不访问未来数据
  - 迭代优化只基于传入的协方差矩阵，不包含未来信息
  - 再平衡只基于当前持仓和目标权重，不预判未来价格

风险平价的优点:
  - 不依赖收益率预测（只需要波动率/协方差）
  - 天然分散化，避免集中度风险
  - 风险贡献均衡，降低单一资产风险暴露
  - 实证研究表明长期表现优于等权/市值加权

风险平价的缺点:
  - 低波动资产可能分配过高权重（需要杠杆才能达到目标收益）
  - 协方差矩阵估计误差影响结果
  - 再平衡频率高时交易成本较大
  - 不考虑资产预期收益率

v1实现范围:
  - RiskParityAllocator 类
  - 逆波动率加权模式（默认，简单快速）
  - 完整风险平价迭代优化模式（考虑相关性）
  - 风险贡献计算
  - 约束自动应用
  - 结果自动校验
  - 波动率为0/负数/缺失的边界处理

TODO(后续迭代):
  - 风险预算（Risk Budgeting）— 自定义各资产风险贡献比例
  - 因子风险平价（Factor Risk Parity）
  - 动态波动率估计（EWMA/GARCH）
  - 协方差矩阵收缩（Ledoit-Wolf Shrinkage）
  - 带交易成本的再平衡优化
  - 目标波动率风险平价（Target Volatility Risk Parity）
  - 杠杆风险平价（Leveraged Risk Parity）

依赖: typing, math, myquant.allocation.base, myquant.core.logger
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from myquant.allocation.base import (
    AllocationConstraints,
    AllocationResult,
    AllocatorBase,
)
from myquant.core.logger import get_logger

logger = get_logger("allocation.risk_parity")


# ═══════════════════════════════════════════════
#  风险平价分配器
# ═══════════════════════════════════════════════

class RiskParityAllocator(AllocatorBase):
    """
    风险平价分配器

    让每个资产对组合的风险贡献相等。支持两种模式：
    1. inverse_volatility（逆波动率加权）— 最简单，不考虑相关性
    2. risk_parity（完整风险平价）— 迭代优化，考虑相关性

    使用方式:
        from myquant.allocation import RiskParityAllocator

        # 逆波动率加权（默认）
        allocator = RiskParityAllocator(mode="inverse_volatility")
        result = allocator.allocate(
            assets=["000001.SZ", "600000.SH", "600519.SH"],
            total_capital=100000,
            risk_data={"volatilities": {"000001.SZ": 0.20, "600000.SH": 0.15, "600519.SH": 0.30}},
        )
        # 低波动(600000.SH 15%)分配最多，高波动(600519.SH 30%)分配最少

        # 完整风险平价（考虑相关性）
        allocator2 = RiskParityAllocator(mode="risk_parity")
        result2 = allocator2.allocate(
            assets=["A", "B", "C"],
            total_capital=100000,
            risk_data={
                "volatilities": {"A": 0.20, "B": 0.15, "C": 0.25},
                "covariance": [
                    [0.04, 0.015, 0.02],
                    [0.015, 0.0225, 0.012],
                    [0.02, 0.012, 0.0625],
                ],
            },
        )

    与实盘引擎集成:
        # engine.py 中
        self.allocator = RiskParityAllocator(mode="inverse_volatility")

        def on_rebalance(self):
            # 计算近期波动率（基于历史数据，防未来函数）
            volatilities = self._calculate_recent_volatilities(self.tradable_symbols)
            result = self.allocator.allocate(
                assets=self.tradable_symbols,
                total_capital=self.portfolio.total_assets,
                risk_data={"volatilities": volatilities},
                constraints=self.allocation_constraints,
            )
            # 生成调仓订单
            adjustments = self.allocator.rebalance(
                target_weights=result.weights,
                current_weights=self.portfolio.current_weights,
                threshold=0.02,
            )
            self._submit_rebalance_orders(adjustments, result)
    """

    name = "risk_parity"
    description = "风险平价分配：每个资产对组合风险贡献相等"

    def __init__(
        self,
        mode: str = "inverse_volatility",
        constraints: Optional[AllocationConstraints] = None,
        max_iterations: int = 1000,
        tolerance: float = 1e-8,
        learning_rate: float = 0.01,
    ):
        """
        初始化风险平价分配器

        Parameters
        ----------
        mode : str
            分配模式：
            - "inverse_volatility"：逆波动率加权（默认，简单快速，不考虑相关性）
            - "risk_parity"：完整风险平价（迭代优化，考虑相关性，需要协方差矩阵）
        constraints : AllocationConstraints | None
            分配约束条件
        max_iterations : int
            迭代优化最大次数（仅risk_parity模式）
        tolerance : float
            迭代收敛容差（仅risk_parity模式）
        learning_rate : float
            迭代学习率（仅risk_parity模式）
        """
        super().__init__(constraints=constraints)
        if mode not in ("inverse_volatility", "risk_parity"):
            raise ValueError(f"不支持的模式: {mode}，可选: inverse_volatility, risk_parity")
        self.mode = mode
        self.max_iterations = max_iterations
        self.tolerance = tolerance
        self.learning_rate = learning_rate

    def allocate(
        self,
        assets: Sequence[str],
        total_capital: float,
        risk_data: Optional[Dict[str, Any]] = None,
        constraints: Optional[AllocationConstraints] = None,
        **kwargs: Any,
    ) -> AllocationResult:
        """
        执行风险平价分配

        流程:
          1. 空资产列表 → 返回空结果
          2. 提取波动率数据（必需）
          3. 根据模式计算权重：
             - inverse_volatility：逆波动率加权
             - risk_parity：迭代优化（需要协方差矩阵）
          4. 应用约束条件
          5. 归一化权重
          6. 计算风险贡献（metadata）
          7. 构建分配结果
          8. 校验分配结果

        Parameters
        ----------
        assets : Sequence[str]
            资产/策略名称列表
        total_capital : float
            总资金
        risk_data : Dict | None
            风险数据，必需包含：
            - "volatilities": Dict[str, float] — 各资产年化波动率（>0）
            可选包含（risk_parity模式）：
            - "covariance": List[List[float]] — 协方差矩阵
            - "asset_order": List[str] — 协方差矩阵对应的资产顺序
        constraints : AllocationConstraints | None
            分配约束
        **kwargs : Any
            额外参数

        Returns
        -------
        AllocationResult : 分配结果
        """
        # 1. 空资产列表处理
        asset_list = list(assets)
        if not asset_list:
            logger.warning("[risk_parity] 资产列表为空，返回空分配结果")
            return AllocationResult(
                allocator_name=self.name,
                assets=[],
                weights={},
                amounts={},
                total_capital=total_capital,
                metadata={"warning": "empty_assets"},
            )

        # 2. 提取波动率数据
        volatilities = self._extract_volatilities(asset_list, risk_data)
        if not volatilities:
            logger.warning("[risk_parity] 无有效波动率数据，回退到等权分配")
            return self._fallback_equal_weight(asset_list, total_capital, constraints)

        # 3. 根据模式计算权重
        active_constraints = constraints or self.constraints

        if self.mode == "inverse_volatility":
            inv_vols = self._inverse_volatility_weights(asset_list, volatilities)
            raw_weights = self.normalize_weights(inv_vols)  # 先归一化到0~1，再应用约束
            method = "inverse_volatility"
        else:  # risk_parity
            covariance = self._extract_covariance(asset_list, risk_data)
            if covariance is None:
                logger.warning("[risk_parity] 无协方差矩阵，回退到逆波动率加权")
                inv_vols = self._inverse_volatility_weights(asset_list, volatilities)
                raw_weights = self.normalize_weights(inv_vols)  # 先归一化
                method = "inverse_volatility_fallback"
            else:
                raw_weights, iterations = self._risk_parity_iterative(
                    asset_list, volatilities, covariance
                )
                method = f"risk_parity_iterative_{iterations}it"

        # 4. 应用约束条件
        constrained_weights = self.apply_constraints(raw_weights, active_constraints)

        # 5. 归一化
        final_weights = self.normalize_weights(constrained_weights)

        # 6. 计算风险贡献（metadata）
        risk_contributions = self._calculate_risk_contributions(
            asset_list, final_weights, volatilities,
            self._extract_covariance(asset_list, risk_data) if self.mode == "risk_parity" else None,
        )

        # 7. 构建分配结果
        metadata = {
            "method": method,
            "mode": self.mode,
            "asset_count": len(asset_list),
            "volatilities": dict(volatilities),
            "risk_contributions": risk_contributions,
            "constraints_applied": active_constraints is not None,
        }
        result = self._build_result(
            assets=asset_list,
            weights=final_weights,
            total_capital=total_capital,
            metadata=metadata,
        )

        # 8. 校验分配结果
        errors = self.validate(result, constraints=active_constraints)
        if errors:
            logger.warning(f"[risk_parity] 分配结果校验有警告: {errors}")
            result.metadata["validation_errors"] = errors
        else:
            logger.info(
                f"[risk_parity] 分配完成: {len(asset_list)}个资产, "
                f"模式={self.mode}, 方法={method}, "
                f"总资金={total_capital:,.2f}"
            )

        return result

    # ═══════════════════════════════════════════
    #  逆波动率加权
    # ═══════════════════════════════════════════

    @staticmethod
    def _inverse_volatility_weights(
        assets: List[str],
        volatilities: Dict[str, float],
    ) -> Dict[str, float]:
        """
        逆波动率加权

        w_i = (1/σ_i) / Σ(1/σ_j)

        Parameters
        ----------
        assets : List[str]
            资产列表
        volatilities : Dict[str, float]
            各资产波动率

        Returns
        -------
        Dict[str, float] : 权重（未归一化，和可能不为1）
        """
        inv_vols = {}
        for asset in assets:
            vol = volatilities.get(asset, 0)
            if vol > 0:
                inv_vols[asset] = 1.0 / vol
            else:
                # 波动率为0或无效，给一个很小的逆波动率（避免除零）
                inv_vols[asset] = 1.0 / 1e-8
        return inv_vols

    # ═══════════════════════════════════════════
    #  完整风险平价（迭代优化）
    # ═══════════════════════════════════════════

    def _risk_parity_iterative(
        self,
        assets: List[str],
        volatilities: Dict[str, float],
        covariance: List[List[float]],
    ) -> Tuple[Dict[str, float], int]:
        """
        完整风险平价迭代优化

        目标：使每个资产的风险贡献 RC_i 相等
        RC_i = w_i * (Σw)_i / sqrt(w^T Σ w)

        迭代算法：
        1. 初始化权重为逆波动率加权
        2. 计算组合波动率和各资产风险贡献
        3. 根据风险贡献偏差调整权重
        4. 重复直到收敛或达到最大迭代次数

        Parameters
        ----------
        assets : List[str]
            资产列表
        volatilities : Dict[str, float]
            各资产波动率
        covariance : List[List[float]]
            协方差矩阵（n×n）

        Returns
        -------
        Tuple[Dict[str, float], int] : (权重字典, 迭代次数)
        """
        n = len(assets)

        # 1. 初始化权重为逆波动率加权（归一化）
        inv_vols = self._inverse_volatility_weights(assets, volatilities)
        total_inv = sum(inv_vols.values())
        weights = [inv_vols[a] / total_inv for a in assets]

        # 2. 迭代优化（P3-17：max_iterations=0时循环不执行，预初始化避免 NameError）
        iteration = -1
        for iteration in range(self.max_iterations):
            # 计算 Σw（协方差矩阵乘权重向量）
            sigma_w = [
                sum(covariance[i][j] * weights[j] for j in range(n))
                for i in range(n)
            ]

            # 计算组合波动率 sqrt(w^T Σ w)
            port_var = sum(weights[i] * sigma_w[i] for i in range(n))
            port_vol = math.sqrt(max(port_var, 1e-12))

            # 计算各资产风险贡献 RC_i = w_i * (Σw)_i / port_vol
            risk_contribs = [
                weights[i] * sigma_w[i] / port_vol
                for i in range(n)
            ]

            # 目标风险贡献 = 总风险 / n
            total_rc = sum(risk_contribs)
            target_rc = total_rc / n if n > 0 else 0

            # 检查收敛
            max_deviation = max(abs(rc - target_rc) for rc in risk_contribs) if risk_contribs else 0
            if max_deviation < self.tolerance:
                logger.debug(f"[risk_parity] 迭代收敛: {iteration+1}次, 最大偏差={max_deviation:.2e}")
                break

            # 调整权重：风险贡献高于目标的减仓，低于目标的加仓
            for i in range(n):
                if risk_contribs[i] > target_rc:
                    # 风险贡献过高，减仓
                    adjustment = self.learning_rate * (risk_contribs[i] - target_rc) / max(target_rc, 1e-12)
                    weights[i] = max(weights[i] - adjustment, 1e-8)
                else:
                    # 风险贡献过低，加仓
                    adjustment = self.learning_rate * (target_rc - risk_contribs[i]) / max(target_rc, 1e-12)
                    weights[i] = weights[i] + adjustment

            # 归一化
            total_w = sum(weights)
            if total_w > 0:
                weights = [w / total_w for w in weights]

        else:
            logger.warning(f"[risk_parity] 达到最大迭代次数 {self.max_iterations}，可能未完全收敛")

        return {assets[i]: weights[i] for i in range(n)}, iteration + 1

    # ═══════════════════════════════════════════
    #  风险贡献计算
    # ═══════════════════════════════════════════

    @staticmethod
    def _calculate_risk_contributions(
        assets: List[str],
        weights: Dict[str, float],
        volatilities: Dict[str, float],
        covariance: Optional[List[List[float]]] = None,
    ) -> Dict[str, float]:
        """
        计算各资产风险贡献占比

        如果有协方差矩阵，使用完整风险贡献公式；
        否则使用简化的波动率加权风险贡献。

        Parameters
        ----------
        assets : List[str]
            资产列表
        weights : Dict[str, float]
            权重字典
        volatilities : Dict[str, float]
            波动率字典
        covariance : List[List[float]] | None
            协方差矩阵

        Returns
        -------
        Dict[str, float] : 各资产风险贡献占比（0~1，和为1）
        """
        n = len(assets)
        if n == 0:
            return {}

        w_list = [weights.get(a, 0) for a in assets]

        if covariance is not None and len(covariance) == n:
            # 完整风险贡献：RC_i = w_i * (Σw)_i / sqrt(w^T Σ w)
            sigma_w = [
                sum(covariance[i][j] * w_list[j] for j in range(n))
                for i in range(n)
            ]
            port_var = sum(w_list[i] * sigma_w[i] for i in range(n))
            port_vol = math.sqrt(max(port_var, 1e-12))
            risk_contribs = [
                w_list[i] * sigma_w[i] / port_vol
                for i in range(n)
            ]
        else:
            # 简化风险贡献：RC_i ≈ w_i * σ_i（不考虑相关性）
            risk_contribs = [
                w_list[i] * volatilities.get(assets[i], 0)
                for i in range(n)
            ]

        total_rc = sum(risk_contribs)
        if total_rc > 0:
            return {assets[i]: risk_contribs[i] / total_rc for i in range(n)}
        return {assets[i]: 1.0 / n for i in range(n)}

    # ═══════════════════════════════════════════
    #  数据提取辅助方法
    # ═══════════════════════════════════════════

    @staticmethod
    def _extract_volatilities(
        assets: List[str],
        risk_data: Optional[Dict[str, Any]],
    ) -> Dict[str, float]:
        """
        从risk_data中提取有效波动率

        Parameters
        ----------
        assets : List[str]
            资产列表
        risk_data : Dict | None
            风险数据

        Returns
        -------
        Dict[str, float] : 有效波动率字典（只包含>0的波动率）
        """
        if not risk_data:
            return {}
        vols = risk_data.get("volatilities", {})
        valid_vols = {}
        for asset in assets:
            vol = vols.get(asset)
            if vol is not None and isinstance(vol, (int, float)) and vol > 0:
                valid_vols[asset] = float(vol)
            else:
                logger.warning(f"[risk_parity] 资产{asset}波动率无效({vol})，将使用默认值")
        return valid_vols

    @staticmethod
    def _extract_covariance(
        assets: List[str],
        risk_data: Optional[Dict[str, Any]],
    ) -> Optional[List[List[float]]]:
        """
        从risk_data中提取协方差矩阵

        Parameters
        ----------
        assets : List[str]
            资产列表
        risk_data : Dict | None
            风险数据

        Returns
        -------
        List[List[float]] | None : 协方差矩阵（n×n），无效时返回None
        """
        if not risk_data:
            return None
        cov = risk_data.get("covariance")
        if cov is None:
            return None
        n = len(assets)
        # 验证矩阵维度
        if not isinstance(cov, list) or len(cov) != n:
            logger.warning(f"[risk_parity] 协方差矩阵维度不匹配: 期望{n}×{n}")
            return None
        for row in cov:
            if not isinstance(row, list) or len(row) != n:
                logger.warning(f"[risk_parity] 协方差矩阵行维度不匹配")
                return None
        return cov

    def _fallback_equal_weight(
        self,
        assets: List[str],
        total_capital: float,
        constraints: Optional[AllocationConstraints],
    ) -> AllocationResult:
        """
        回退到等权分配（当无有效波动率数据时）

        Parameters
        ----------
        assets : List[str]
            资产列表
        total_capital : float
            总资金
        constraints : AllocationConstraints | None
            约束条件

        Returns
        -------
        AllocationResult : 等权分配结果
        """
        n = len(assets)
        base_weight = 1.0 / n if n > 0 else 0
        raw_weights = {a: base_weight for a in assets}
        active_constraints = constraints or self.constraints
        constrained = self.apply_constraints(raw_weights, active_constraints)
        final = self.normalize_weights(constrained)
        return self._build_result(
            assets=assets,
            weights=final,
            total_capital=total_capital,
            metadata={
                "method": "equal_weight_fallback",
                "mode": self.mode,
                "warning": "no_valid_volatilities",
                "asset_count": n,
            },
        )


# ═══════════════════════════════════════════════════════════════
#  D-4 风险预算再分配（回写#246）
# ═══════════════════════════════════════════════════════════════
# 目的：基于监控反馈（滚动绩效/波动/环境匹配）在多策略间再分配风险预算——
# 低匹配/高回撤/负绩效策略自动降配，把释放的预算按质量分重新分配到其他策略。
# 再分配有上限约束（每策略权重上限 max_weights、单步最大降幅 max_step、
# 单次总调幅上限 max_total_shift、权重下限 min_weight）与审批留痕。
# 安全模型（对齐 D-1 三阶段）：mode=shadow 只出建议（proposed），经 approve
# 人工确认后 applied；mode=auto 自动生效（安全测试后放开）。
# 再分配可审计（reallocation_history 全量留痕 + AuditLogStore kind=risk_realloc）、
# 可回滚（rollback 恢复 weights_before）。
# 防未来：只消费已发生的监控反馈（滚动绩效/波动率/环境匹配分），不预测未来。

# 再分配状态
REALLOC_STATUS_PROPOSED = "proposed"
REALLOC_STATUS_APPLIED = "applied"
REALLOC_STATUS_REJECTED = "rejected"
REALLOC_STATUS_ROLLED_BACK = "rolled_back"
REALLOC_MODES = ("shadow", "auto")

# 算法参数（非业务阈值；构造可注入）
REALLOC_DEFAULT_MIN_WEIGHT: float = 0.05       # 每策略权重下限
REALLOC_DEFAULT_MAX_STEP: float = 0.10         # 单策略单次最大降幅（单步护栏）
REALLOC_DEFAULT_MAX_TOTAL_SHIFT: float = 0.20  # 单次总调幅上限（护栏）
REALLOC_DEFAULT_MATCH_SCORE_MIN: float = 60.0  # 环境匹配分下限（低于 → 降配惩罚）
REALLOC_DEFAULT_SCORE_MATCH_WEIGHT: float = 0.6  # 质量分=匹配权重×匹配分 + (1-权重)×风险项
REALLOC_DEFAULT_LOW_MATCH_PENALTY: float = 0.5   # 低匹配惩罚系数
REALLOC_DEFAULT_NEGATIVE_PERF_PENALTY: float = 0.7  # 负绩效惩罚系数
REALLOC_DEFAULT_HISTORY_LIMIT: int = 500
REALLOC_MAX_ITER: int = 20                        # 约束重归一化迭代上限


@dataclass
class ReallocationRecord:
    """
    一次风险预算再分配的全链路留痕（JSON 可序列化）。

    Attributes
    ----------
    realloc_id : str
        再分配记录唯一 ID（审批/回滚句柄）。
    weights_before : dict
        再分配前权重（应用时有效）。
    weights_after : dict
        再分配后的目标权重。
    status : str
        proposed / applied / rejected / rolled_back。
    feedback : dict
        触发再分配的监控反馈快照（策略 → 滚动绩效/波动率/环境匹配）。
    scores : dict
        各策略质量分（算法定义，0~1）。
    caps : dict
        上限约束明细（min_weight/max_step/max_total_shift/max_weights）。
    mode : str
        shadow / auto。
    actor : str
        操作者。
    reason : str
        原因。
    created_at / decided_at : float
        提议/决策时刻（当前时点，防未来）。
    """

    realloc_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    weights_before: Dict[str, float] = field(default_factory=dict)
    weights_after: Dict[str, float] = field(default_factory=dict)
    status: str = REALLOC_STATUS_PROPOSED
    feedback: Dict[str, Any] = field(default_factory=dict)
    scores: Dict[str, float] = field(default_factory=dict)
    caps: Dict[str, Any] = field(default_factory=dict)
    mode: str = REALLOC_MODES[0]
    actor: str = "system"
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化（JSON 可读）。"""
        return {
            "realloc_id": self.realloc_id,
            "weights_before": dict(self.weights_before),
            "weights_after": dict(self.weights_after),
            "status": self.status,
            "feedback": dict(self.feedback),
            "scores": dict(self.scores),
            "caps": dict(self.caps),
            "mode": self.mode,
            "actor": self.actor,
            "reason": self.reason,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
        }


class RiskBudgetReallocator:
    """
    基于监控反馈的多策略风险预算再分配器（D-4）。

    用法:
        from myquant.allocation.risk_parity import RiskBudgetReallocator
        rr = RiskBudgetReallocator(mode="shadow")          # shadow 只出建议
        rec = rr.propose({"a": 0.5, "b": 0.5}, {
            "a": {"rolling_perf": 0.12, "volatility": 0.10, "regime_match": 90},
            "b": {"rolling_perf": -0.05, "volatility": 0.30, "regime_match": 30},
        }, actor="system", reason="月度再平衡")
        rr.approve(rec.realloc_id, actor="risk_owner", reason="确认")  # 生效
        weights = rr.effective_weights()                   # 生效权重
        rr.rollback(rec.realloc_id, actor="ops", reason="回滚")       # 恢复

    质量分（算法定义，非业务阈值）：
      score = 0.6×match_norm + 0.4×(1 - risk_norm)；
      低匹配（regime_match < match_score_min）×0.5；负绩效（rolling_perf<0）×0.7；
      无监控反馈的策略质量分=0（低证据降配，受 max_step/下限护栏约束）。
    目标权重：按质量分比例分配 → 上限/下限约束迭代重归一化 → 单步降幅与总调幅
    护栏 → 归一化到和=1（尾差归最大者）。
    """

    def __init__(
        self,
        min_weight: float = REALLOC_DEFAULT_MIN_WEIGHT,
        max_step: float = REALLOC_DEFAULT_MAX_STEP,
        max_total_shift: float = REALLOC_DEFAULT_MAX_TOTAL_SHIFT,
        match_score_min: float = REALLOC_DEFAULT_MATCH_SCORE_MIN,
        max_weights: Optional[Dict[str, float]] = None,
        score_match_weight: float = REALLOC_DEFAULT_SCORE_MATCH_WEIGHT,
        low_match_penalty: float = REALLOC_DEFAULT_LOW_MATCH_PENALTY,
        negative_perf_penalty: float = REALLOC_DEFAULT_NEGATIVE_PERF_PENALTY,
        mode: str = REALLOC_MODES[0],
        audit_log: Any = None,
        clock: Optional[Callable[[], float]] = None,
        max_history: int = REALLOC_DEFAULT_HISTORY_LIMIT,
    ) -> None:
        """
        Parameters
        ----------
        min_weight : float
            每策略权重下限（0<=min_weight<1/n 语义由调用方保证，构造仅校验范围）。
        max_step : float
            单策略单次最大降幅（护栏，>0）。
        max_total_shift : float
            单次总调幅上限（护栏，>0）。
        match_score_min : float
            环境匹配分下限（低于 → 低匹配降配惩罚，[0,100]）。
        max_weights : dict | None
            每策略权重上限 {strategy: max_weight}（护栏，>0 且 <1）。
        score_match_weight : float
            质量分中匹配分权重（0~1；风险项权重=1-该值）。
        low_match_penalty : float
            低匹配惩罚系数（(0,1]）。
        negative_perf_penalty : float
            负绩效惩罚系数（(0,1]）。
        mode : str
            shadow（只出建议，需 approve 人工确认）/ auto（自动生效）。
        audit_log : AuditLogStore | None
            审计存储（kind=risk_realloc）；None 惰性尝试，失败降级内存留痕。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        max_history : int
            再分配历史留存上限（正整数）。
        """
        for name, v in (
            ("min_weight", min_weight), ("max_step", max_step),
            ("max_total_shift", max_total_shift), ("match_score_min", match_score_min),
        ):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{name} 必须为数值，收到 {v!r}") from None
            if fv < 0:
                raise ValueError(f"{name} 不能为负，收到 {v!r}")
        if float(max_step) <= 0 or float(max_total_shift) <= 0:
            raise ValueError("max_step/max_total_shift 必须 >0")
        if float(match_score_min) > 100:
            raise ValueError(f"match_score_min 不能超过 100，收到 {match_score_min!r}")
        try:
            sw = float(score_match_weight)
        except (TypeError, ValueError):
            raise ValueError(f"score_match_weight 必须为数值，收到 {score_match_weight!r}") from None
        if not (0.0 <= sw <= 1.0):
            raise ValueError(f"score_match_weight 必须在 [0,1]，收到 {score_match_weight!r}")
        for name, v in (("low_match_penalty", low_match_penalty),
                        ("negative_perf_penalty", negative_perf_penalty)):
            try:
                pv = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{name} 必须为数值，收到 {v!r}") from None
            if not (0.0 < pv <= 1.0):
                raise ValueError(f"{name} 必须在 (0,1]，收到 {v!r}")
        if mode not in REALLOC_MODES:
            raise ValueError(f"mode 必须为 {REALLOC_MODES}，收到 {mode!r}")
        if not isinstance(max_history, int) or max_history <= 0:
            raise ValueError(f"max_history 必须为正整数，收到 {max_history!r}")

        self._min_weight: float = float(min_weight)
        self._max_step: float = float(max_step)
        self._max_total_shift: float = float(max_total_shift)
        self._match_score_min: float = float(match_score_min)
        self._max_weights: Dict[str, float] = dict(max_weights or {})
        for s, cap in self._max_weights.items():
            try:
                cap_f = float(cap)
            except (TypeError, ValueError):
                raise ValueError(f"max_weights[{s}] 必须为数值，收到 {cap!r}") from None
            if not (0.0 < cap_f <= 1.0):
                raise ValueError(f"max_weights[{s}] 必须在 (0,1]，收到 {cap!r}")
            self._max_weights[s] = cap_f
        self._score_match_weight: float = sw
        self._low_match_penalty: float = float(low_match_penalty)
        self._negative_perf_penalty: float = float(negative_perf_penalty)
        self._mode: str = mode
        self._audit_log: Any = audit_log
        self._audit_tried: bool = audit_log is not None
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._max_history: int = int(max_history)

        self._effective: Optional[Dict[str, float]] = None
        self._history: List[ReallocationRecord] = []
        self._by_id: Dict[str, ReallocationRecord] = {}

        logger.debug(
            "RiskBudgetReallocator 初始化: mode=%s min_weight=%.2f max_step=%.2f "
            "max_total_shift=%.2f match_score_min=%.1f",
            self._mode, self._min_weight, self._max_step,
            self._max_total_shift, self._match_score_min,
        )

    # ── 质量分（算法定义）──────────────────────────────────────

    def _normalize_input_weights(self, weights: Dict[str, float]) -> Dict[str, float]:
        """校验并归一化输入权重（有限、>=0、和>0 → 归一化和=1）。"""
        if not isinstance(weights, dict) or not weights:
            raise ValueError("weights 必须为非空 dict")
        total = 0.0
        for s, w in weights.items():
            try:
                wf = float(w)
            except (TypeError, ValueError):
                raise ValueError(f"weights[{s}] 必须为数值，收到 {w!r}") from None
            if not math.isfinite(wf) or wf < 0:
                raise ValueError(f"weights[{s}] 必须为非负有限数，收到 {w!r}")
            total += wf
        if total <= 0:
            raise ValueError("weights 之和必须 >0")
        return {s: round(float(w) / total, 6) for s, w in weights.items()}

    def _validate_feedback(
        self, feedback: Dict[str, Any]
    ) -> Dict[str, Any]:
        """校验监控反馈（有限数；非法字段防御式忽略并告警）。"""
        if not isinstance(feedback, dict):
            raise ValueError(f"feedback 必须为 dict，收到 {type(feedback).__name__}")
        cleaned: Dict[str, Any] = {}
        for s, info in feedback.items():
            if not isinstance(info, dict):
                logger.warning("[realloc] %s 反馈非 dict，忽略", s)
                continue
            item: Dict[str, Any] = {}
            for key in ("rolling_perf", "volatility", "regime_match"):
                if key in info:
                    try:
                        val = float(info[key])
                    except (TypeError, ValueError):
                        logger.warning("[realloc] %s.%s 非法，忽略", s, key)
                        continue
                    if not math.isfinite(val):
                        logger.warning("[realloc] %s.%s 非有限数，忽略", s, key)
                        continue
                    item[key] = val
            cleaned[s] = item
        return cleaned

    def _compute_scores(self, feedback: Dict[str, Any]) -> Dict[str, float]:
        """
        各策略质量分（算法定义，0~1）：
          match_norm = clip(regime_match, 0, 100)/100；
          risk_norm = volatility / max(volatilities, 1e-9)（全为 0 时 risk_norm=0）；
          score = score_match_weight×match_norm + (1-score_match_weight)×(1-risk_norm)；
          低匹配（regime_match < match_score_min）×low_match_penalty；
          负绩效（rolling_perf < 0）×negative_perf_penalty；
          无反馈字段（缺 regime_match/volatility）→ 对应项取 0（低证据降配）。
        """
        scores: Dict[str, float] = {}
        volatilities = [
            float(info["volatility"])
            for info in feedback.values()
            if "volatility" in info and info["volatility"] >= 0
        ]
        max_vol = max(volatilities, default=0.0)
        for s, info in feedback.items():
            match_raw = info.get("regime_match")
            match_norm = (
                max(0.0, min(100.0, float(match_raw))) / 100.0
                if match_raw is not None else 0.0
            )
            vol = info.get("volatility")
            risk_norm = (float(vol) / max_vol) if (vol is not None and max_vol > 0) else 0.0
            risk_norm = max(0.0, min(1.0, risk_norm))
            score = (
                self._score_match_weight * match_norm
                + (1.0 - self._score_match_weight) * (1.0 - risk_norm)
            )
            if match_raw is not None and float(match_raw) < self._match_score_min:
                score *= self._low_match_penalty
            perf = info.get("rolling_perf")
            if perf is not None and float(perf) < 0:
                score *= self._negative_perf_penalty
            scores[s] = max(0.0, min(1.0, score))
        return scores

    def _build_target(self, weights: Dict[str, float], scores: Dict[str, float]) -> Dict[str, float]:
        """
        目标权重计算：按质量分比例分配 → 上限/下限约束迭代重归一化 →
        单步降幅护栏 → 总调幅护栏 → 归一化到和=1。
        """
        names = list(weights.keys())
        total = sum(scores.get(s, 0.0) for s in names)
        if total <= 1e-12:
            # 全部质量分为 0（无差异化依据）→ 不调整（不编造）
            return dict(weights)
        # 1. 按质量分比例分配
        target = {s: round(total_w := (scores.get(s, 0.0) / total), 6) for s in names}
        # 2. 上限/下限约束迭代重归一化（护栏：不超上限、不低于下限）
        for _ in range(REALLOC_MAX_ITER):
            clamped = {}
            for s in names:
                t = target.get(s, 0.0)
                cap = self._max_weights.get(s, 1.0)
                t = max(self._min_weight, min(t, cap))
                clamped[s] = t
            residual = 1.0 - sum(clamped.values())
            if abs(residual) < 1e-9:
                target = clamped
                break
            # 把残差按质量分比例分给未触顶/未触底策略
            free = [
                s for s in names
                if (residual > 0 and clamped[s] < self._max_weights.get(s, 1.0))
                or (residual < 0 and clamped[s] > self._min_weight)
            ]
            free_score = sum(scores.get(s, 0.0) for s in free)
            if free_score <= 1e-12:
                target = clamped
                break
            target = {
                s: clamped[s] + residual * (scores.get(s, 0.0) / free_score)
                if s in free else clamped[s]
                for s in names
            }
        # 3. 单步降幅护栏：单策略单次最大降幅
        adjusted = dict(target)
        for s in names:
            delta = adjusted[s] - weights.get(s, 0.0)
            if delta < -self._max_step:
                adjusted[s] = weights.get(s, 0.0) - self._max_step
        # 4. 总调幅护栏：单次总调幅上限
        shift = sum(abs(adjusted.get(s, 0.0) - weights.get(s, 0.0)) for s in names)
        if shift > self._max_total_shift and shift > 1e-12:
            factor = self._max_total_shift / shift
            adjusted = {
                s: weights.get(s, 0.0)
                + (adjusted.get(s, 0.0) - weights.get(s, 0.0)) * factor
                for s in names
            }
        # 5. 归一化到和=1（尾差归最大权重策略，保留尾差精度保证和=1）
        total2 = sum(adjusted.values())
        if total2 <= 0:
            return dict(weights)
        normalized = {s: max(self._min_weight, v / total2) for s, v in adjusted.items()}
        total3 = sum(normalized.values())
        if total3 > 1e-9:
            normalized = {s: v / total3 for s, v in normalized.items()}
        rounded = {s: round(v, 6) for s, v in normalized.items()}
        diff = 1.0 - sum(rounded.values())
        biggest = max(rounded, key=rounded.get)
        if abs(diff) >= 1e-6:
            rounded[biggest] = round(rounded[biggest] + diff, 6)
        else:
            # 尾差小于 6 位小数半单位：round 会吞掉 → 最大者保留尾差全精度
            rounded[biggest] = rounded[biggest] + diff
        return rounded

    # ── 再分配流程（shadow 提议 → approve 生效 / auto 直接生效）──

    def _caps_summary(self) -> Dict[str, Any]:
        return {
            "min_weight": self._min_weight,
            "max_step": self._max_step,
            "max_total_shift": self._max_total_shift,
            "match_score_min": self._match_score_min,
            "max_weights": dict(self._max_weights),
        }

    def _build_record(
        self,
        weights: Dict[str, float],
        feedback: Dict[str, Any],
        actor: str,
        reason: str,
    ) -> ReallocationRecord:
        weights_norm = self._normalize_input_weights(weights)
        feedback_clean = self._validate_feedback(feedback)
        scores = self._compute_scores(feedback_clean)
        # 无监控反馈的策略 → 质量分 0（低证据降配，受 max_step/下限护栏约束）
        for s in weights_norm:
            if s not in scores:
                scores[s] = 0.0
        target = self._build_target(weights_norm, scores)
        return ReallocationRecord(
            weights_before=dict(weights_norm),
            weights_after=dict(target),
            feedback=dict(feedback_clean),
            scores={s: round(v, 6) for s, v in scores.items()},
            caps=self._caps_summary(),
            mode=self._mode,
            actor=actor,
            reason=reason,
            created_at=self._clock(),
        )

    def propose(
        self,
        weights: Dict[str, float],
        feedback: Dict[str, Any],
        actor: str = "system",
        reason: str = "",
    ) -> ReallocationRecord:
        """
        提出再分配建议（shadow 模式；需 approve 人工确认后生效）。

        Raises
        ------
        ValueError : 权重/反馈非法、actor 为空时抛出。
        """
        if not (actor or "").strip():
            raise ValueError("propose actor 不能为空")
        rec = self._build_record(weights, feedback, actor, reason)
        rec.status = REALLOC_STATUS_PROPOSED
        self._record(rec)
        self._audit("realloc", "proposed", actor, rec.to_dict())
        return rec

    def approve(
        self,
        realloc_id: str,
        actor: str = "system",
        reason: str = "人工确认再分配生效",
    ) -> ReallocationRecord:
        """
        人工确认再分配生效（shadow 模式；对应 D-1 confirm 阶段）。

        Raises
        ------
        ValueError : 记录不存在/非 proposed/actor 为空时抛出。
        """
        realloc_id = (realloc_id or "").strip()
        if not realloc_id:
            raise ValueError("approve realloc_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("approve actor 不能为空")
        rec = self._by_id.get(realloc_id)
        if rec is None or rec.status != REALLOC_STATUS_PROPOSED:
            raise ValueError(f"再分配记录 {realloc_id} 不存在或非 proposed")
        rec.status = REALLOC_STATUS_APPLIED
        rec.decided_at = self._clock()
        rec.actor = actor
        rec.reason = reason
        self._effective = dict(rec.weights_after)
        self._audit("realloc", "approved", actor, rec.to_dict())
        return rec

    def reject(
        self,
        realloc_id: str,
        actor: str = "system",
        reason: str = "人工否决",
    ) -> ReallocationRecord:
        """人工否决再分配建议（不生效）。"""
        realloc_id = (realloc_id or "").strip()
        if not realloc_id:
            raise ValueError("reject realloc_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("reject actor 不能为空")
        rec = self._by_id.get(realloc_id)
        if rec is None or rec.status != REALLOC_STATUS_PROPOSED:
            raise ValueError(f"再分配记录 {realloc_id} 不存在或非 proposed")
        rec.status = REALLOC_STATUS_REJECTED
        rec.decided_at = self._clock()
        rec.actor = actor
        rec.reason = reason
        self._audit("realloc", "rejected", actor, rec.to_dict())
        return rec

    def apply_auto(
        self,
        weights: Dict[str, float],
        feedback: Dict[str, Any],
        actor: str = "system",
        reason: str = "",
    ) -> ReallocationRecord:
        """自动生效再分配（auto 模式；安全测试通过后放开）。"""
        if not (actor or "").strip():
            raise ValueError("apply_auto actor 不能为空")
        rec = self._build_record(weights, feedback, actor, reason)
        rec.status = REALLOC_STATUS_APPLIED
        rec.decided_at = self._clock()
        self._effective = dict(rec.weights_after)
        self._record(rec)
        self._audit("realloc", "applied", actor, rec.to_dict())
        return rec

    def rollback(self, realloc_id: str, actor: str = "system", reason: str = "再分配回滚") -> bool:
        """
        回滚一次已生效再分配（恢复 weights_before；可回滚）。

        Returns
        -------
        bool : 是否成功回滚（记录不存在/非 applied 返回 False）。
        """
        realloc_id = (realloc_id or "").strip()
        if not realloc_id:
            raise ValueError("rollback realloc_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("rollback actor 不能为空")
        rec = self._by_id.get(realloc_id)
        if rec is None or rec.status != REALLOC_STATUS_APPLIED:
            return False
        self._effective = dict(rec.weights_before)
        rec.status = REALLOC_STATUS_ROLLED_BACK
        rec.decided_at = self._clock()
        rec.actor = actor
        rec.reason = reason
        self._audit("realloc", "rolled_back", actor, rec.to_dict())
        return True

    # ── 观测 ───────────────────────────────────────────────────

    def effective_weights(self) -> Optional[Dict[str, float]]:
        """当前生效权重（未生效/未应用返回 None）。"""
        if self._effective is None:
            return None
        return dict(self._effective)

    def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """返回再分配全量留痕（JSON 可序列化；可审计/可查）。"""
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"history limit 必须为正整数，收到 {limit!r}")
        return [r.to_dict() for r in self._history[-limit:]]

    def summary(self) -> Dict[str, Any]:
        """再分配器状态汇总。"""
        return {
            "mode": self._mode,
            "effective_applied": self._effective is not None,
            "proposed_total": sum(
                1 for r in self._history if r.status == REALLOC_STATUS_PROPOSED
            ),
            "applied_total": sum(
                1 for r in self._history if r.status == REALLOC_STATUS_APPLIED
            ),
            "rejected_total": sum(
                1 for r in self._history if r.status == REALLOC_STATUS_REJECTED
            ),
            "rolled_back_total": sum(
                1 for r in self._history if r.status == REALLOC_STATUS_ROLLED_BACK
            ),
            "history_total": len(self._history),
        }

    # ── 内部 ───────────────────────────────────────────────────

    def _record(self, rec: ReallocationRecord) -> ReallocationRecord:
        """留痕（上限裁剪最旧）。"""
        self._history.append(rec)
        self._by_id[rec.realloc_id] = rec
        if len(self._history) > self._max_history:
            drop = self._history[: -self._max_history]
            self._history = self._history[-self._max_history:]
            for r in drop:
                self._by_id.pop(r.realloc_id, None)
        return rec

    def _audit(
        self, action: str, sub_action: str, actor: str, detail: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """审计留痕（kind=risk_realloc；失败降级内存留痕不阻断）。"""
        if self._audit_log is None:
            if not self._audit_tried:
                try:
                    from myquant.monitoring.audit_log import AuditLogStore
                    self._audit_log = AuditLogStore()
                except Exception as e:  # noqa: BLE001 —— 审计不可用降级
                    logger.warning("[realloc] 审计存储不可用（降级内存留痕）: %s", e)
                self._audit_tried = True
            if self._audit_log is None:
                return None
        try:
            return self._audit_log.append(
                "risk_realloc", f"realloc:{sub_action}", actor, dict(detail or {})
            )
        except Exception as e:  # noqa: BLE001 —— 审计失败不阻断再分配
            logger.warning("[realloc] 审计留痕失败: %s", e)
            return None
