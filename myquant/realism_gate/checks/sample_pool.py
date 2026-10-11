# 文件名：myquant/realism_gate/checks/sample_pool.py
"""
realism_gate/checks/sample_pool.py - 样本池真实性检查（P0-01 第三刀）
======================================================================

校验 manifest 中"样本池"分组字段（v2.2 §4.2）：
  - pool.exclusion_basis == "today" → degraded（用今天口径选股，幸存者偏差）
  - survivorship.discount 为 null → unverified（幸存者偏差折价未量化）
  - pool.pit_applied 必须 True（PIT 选股未生效 → 用了未来才知道的股票池）

L0 纯函数：只消费 manifest 注入的字段值。
"""

from __future__ import annotations

from typing import List

from myquant.realism_gate.gate import register_realism_check
from myquant.realism_gate.manifest import RealismManifest, RealismViolation


def check_sample_pool(manifest: RealismManifest) -> List[RealismViolation]:
    """样本池真实性检查。

    Args:
        manifest: 已 build 的 RealismManifest。

    Returns:
        违规列表（无违规为空列表）。
    """
    violations: List[RealismViolation] = []

    # PIT 选股未生效
    if not manifest.get_field("pool.pit_applied", False):
        violations.append(RealismViolation(
            field_path="pool.pit_applied",
            critical=True,
            message="PIT 选股未生效（用了未来才知道的股票池，幸存者偏差）",
            actual_value=False,
        ))

    # today 口径
    basis = manifest.get_field("pool.exclusion_basis", "as_of")
    if basis == "today":
        violations.append(RealismViolation(
            field_path="pool.exclusion_basis",
            critical=True,
            message="样本池用 today 口径剔除（非 as_of 时点，幸存者偏差）",
            actual_value=basis,
        ))

    # 幸存者偏差折价未量化
    discount = manifest.get_field("survivorship.discount", None)
    if discount is None:
        violations.append(RealismViolation(
            field_path="survivorship.discount",
            critical=True,
            message="幸存者偏差折价未量化（null，回测收益可能被高估）",
            actual_value=None,
        ))

    return violations


register_realism_check("sample_pool", check_sample_pool)
