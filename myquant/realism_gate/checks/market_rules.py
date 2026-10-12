# 文件名：myquant/realism_gate/checks/market_rules.py
"""
realism_gate/checks/market_rules.py - 行情规则真实性检查（P0-01 第三刀）
======================================================================

校验 manifest 中"行情规则"分组字段的配置真实性（v2.2 §4.2）：
  - price_limit.enabled 必须 True（涨跌停未生效 → 回测可在涨停价成交，虚增收益）
  - suspension.enabled 必须 True（停牌未生效 → 已停牌股票仍可交易）
  - t1_position_freeze.enabled 必须 True（T+1 未生效 → 当日买入可当日卖出，A股违规）
  - ~~lot_size.enforced 必须 True~~（#346 真实采集对齐移除：撮合层 SimBroker
    无整手执行逻辑（grep 零消费）→ ``False`` 是**真实事实**而非谎报；可买零股的
    风险由 P0-03 撮合层整手执行接线解决，接线后 enforced=True 恢复本判据）

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

    # ── lot_size.enforced 判据（#346 真实采集对齐移除）──
    # 撮合层（SimBroker）无整手执行逻辑（grep 零消费）→ False 是真实事实而非
    # 谎报；判据移除避免入口误伤（enabled=True 时引擎无法创建）。可买零股风险
    # 由 P0-03 撮合层整手执行接线解决；接线后 enforced=True 恢复本判据。

    return violations


register_realism_check("market_rules", check_market_rules)
