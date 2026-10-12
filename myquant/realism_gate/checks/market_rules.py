# 文件名：myquant/realism_gate/checks/market_rules.py
"""
realism_gate/checks/market_rules.py - 行情规则真实性检查（P0-01 第三刀）
======================================================================

校验 manifest 中"行情规则"分组字段的配置真实性（v2.2 §4.2）：
  - price_limit.enabled 必须 True（涨跌停未生效 → 回测可在涨停价成交，虚增收益）
  - suspension.enabled 必须 True（停牌未生效 → 已停牌股票仍可交易）
  - t1_position_freeze.enabled 必须 True（T+1 未生效 → 当日买入可当日卖出，A股违规）
  - lot_size.enforced 必须 True（整手未生效 → 可买零股，与实盘不符；#348 恢复——
    #346 曾因撮合层无整手执行逻辑豁免，接线完成后重新强制）

L0 纯函数：只消费 manifest 注入的字段值，不 import L3/L4（R-02/R-16）。
导入即注册（register_realism_check）。
"""

from __future__ import annotations

from typing import List

from myquant.realism_gate.gate import register_realism_check
from myquant.realism_gate.manifest import RealismManifest, RealismViolation


def check_market_rules(manifest: RealismManifest) -> List[RealismViolation]:
    """行情规则真实性检查。

    Args:
        manifest: 已 build 的 RealismManifest。

    Returns:
        违规列表（无违规为空列表）。
    """
    violations: List[RealismViolation] = []

    # 涨跌停
    if not manifest.get_field("price_limit.enabled", False):
        violations.append(RealismViolation(
            field_path="price_limit.enabled",
            critical=True,
            message="涨跌停未启用（回测可在涨停价成交，虚增收益）",
            actual_value=False,
        ))

    # 停牌
    if not manifest.get_field("suspension.enabled", False):
        violations.append(RealismViolation(
            field_path="suspension.enabled",
            critical=True,
            message="停牌未启用（已停牌股票仍可交易）",
            actual_value=False,
        ))

    # T+1
    if not manifest.get_field("t1_position_freeze.enabled", False):
        violations.append(RealismViolation(
            field_path="t1_position_freeze.enabled",
            critical=True,
            message="T+1 持仓冻结未启用（当日买入可当日卖出，A股规则违规）",
            actual_value=False,
        ))

    # 整手（P0-03 续卡 #348：撮合层整手执行接线完成，恢复判据——
    # 此前 #346 因撮合层无整手执行逻辑豁免；接线后 enforced 必须 True）
    if not manifest.get_field("lot_size.enforced", False):
        violations.append(RealismViolation(
            field_path="lot_size.enforced",
            critical=True,
            message="整手约束未启用（可买零股，与实盘不符）",
            actual_value=False,
        ))

    return violations


register_realism_check("market_rules", check_market_rules)
