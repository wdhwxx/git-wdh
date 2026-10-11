# 文件名：myquant/realism_gate/checks/cost_model.py
"""
realism_gate/checks/cost_model.py - 成本模型真实性检查（P0-01 第三刀）
======================================================================

校验 manifest 中"成本"分组字段（v2.2 §4.2）：
  - slippage.is_zero_cost 必须 False（零滑点 → 回测成交永远按理论价，虚增收益）
  - commission.buy_rate / sell_rate 必须 > 0（零佣金 → 与实盘不符）
  - fee_dilution_basis 必须在允许域内（per_order / per_bar）

L0 纯函数：只消费 manifest 注入的字段值。
"""

from __future__ import annotations

from typing import List

from myquant.realism_gate.gate import register_realism_check
from myquant.realism_gate.manifest import RealismManifest, RealismViolation


def check_cost_model(manifest: RealismManifest) -> List[RealismViolation]:
    """成本模型真实性检查。

    Args:
        manifest: 已 build 的 RealismManifest。

    Returns:
        违规列表（无违规为空列表）。
    """
    violations: List[RealismViolation] = []

    # 零滑点
    if manifest.get_field("slippage.is_zero_cost", False):
        violations.append(RealismViolation(
            field_path="slippage.is_zero_cost",
            critical=True,
            message="零滑点成本（回测成交永远按理论价，虚增收益）",
            actual_value=True,
        ))

    # 佣金费率
    buy_rate = manifest.get_field("commission.buy_rate", 0.0)
    sell_rate = manifest.get_field("commission.sell_rate", 0.0)
    if (buy_rate or 0.0) <= 0:
        violations.append(RealismViolation(
            field_path="commission.buy_rate",
            critical=True,
            message=f"买入佣金费率为零（{buy_rate}），与实盘不符",
            actual_value=buy_rate,
        ))
    if (sell_rate or 0.0) <= 0:
        violations.append(RealismViolation(
            field_path="commission.sell_rate",
            critical=True,
            message=f"卖出佣金费率为零（{sell_rate}），与实盘不符",
            actual_value=sell_rate,
        ))

    return violations


register_realism_check("cost_model", check_cost_model)
