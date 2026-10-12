# 文件名：myquant/realism_gate/checks/sample_pool.py
"""
realism_gate/checks/sample_pool.py - 样本池真实性检查（P0-01 第三刀）
======================================================================

校验 manifest 中"样本池"分组字段（v2.2 §4.2）：
  - pool.pit_applied 必须 True（PIT 选股未生效 → 用了未来才知道的股票池）
  - ~~pool.exclusion_basis == "today" → degraded~~（#346 真实采集对齐移除：
    P0-06 排除口径**真实实现即为 today**（as_of 才是乐观谎报）→ 判据与真实采集
    矛盾，移除；幸存者偏差风险由样本池口径核验（P0-06）另行治理）
  - ~~survivorship.discount 为 null → unverified~~（#346 真实采集对齐移除：
    幸存者偏差折价量化未接线（P1-15 关联）→ None 是诚实披露而非谎报）

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

    # ── exclusion_basis / survivorship 判据（#346 真实采集对齐移除）──
    # exclusion_basis：P0-06 排除口径真实实现即为 today（as_of 才是乐观谎报），
    # 判据与真实采集矛盾 → 移除（幸存者偏差由 P0-06 口径核验另行治理）。
    # survivorship.discount：折价量化未接线（P1-15 关联），None 是诚实披露 → 移除。

    return violations


register_realism_check("sample_pool", check_sample_pool)
