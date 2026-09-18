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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

        # 2. 迭代优化
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
