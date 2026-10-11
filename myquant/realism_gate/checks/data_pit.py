# 文件名：myquant/realism_gate/checks/data_pit.py
"""
realism_gate/checks/data_pit.py - 数据 PIT 真实性检查（P0-01 第三刀）
======================================================================

校验 manifest 中"数据"分组字段（v2.2 §4.2）：
  - adjust_degraded_symbols 非空 → degraded（复权降级 raw，价格序列不可比）
  - fingerprint_hash 为 null → unverified（数据快照未指纹，无法复现）
  - quality.nan_price_rows > 0 → critical（含 NaN 价格行，回测可能产生异常成交）
  - quality.ohlc_violation_rows > 0 → critical（OHLC 关系违反：high<low 等）

L0 纯函数：只消费 manifest 注入的字段值。
"""

from __future__ import annotations

from typing import List

from myquant.realism_gate.gate import register_realism_check
from myquant.realism_gate.manifest import RealismManifest, RealismViolation


def check_data_pit(manifest: RealismManifest) -> List[RealismViolation]:
    """数据 PIT 真实性检查。

    Args:
        manifest: 已 build 的 RealismManifest。

    Returns:
        违规列表（无违规为空列表）。
    """
    violations: List[RealismViolation] = []

    # 复权降级标的
    degraded = manifest.get_field("adjust_degraded_symbols", [])
    if isinstance(degraded, list) and len(degraded) > 0:
        violations.append(RealismViolation(
            field_path="adjust_degraded_symbols",
            critical=True,
            message=f"{len(degraded)} 个标的复权降级 raw（{degraded[:5]}...），价格序列不可比",
            actual_value=degraded,
        ))

    # 快照指纹缺失
    fp = manifest.get_field("fingerprint_hash", None)
    if fp is None:
        violations.append(RealismViolation(
            field_path="fingerprint_hash",
            critical=True,
            message="数据快照指纹缺失（null），无法复现回测数据",
            actual_value=None,
        ))

    # NaN 价格行
    nan_rows = manifest.get_field("quality.nan_price_rows", 0)
    if (nan_rows or 0) > 0:
        violations.append(RealismViolation(
            field_path="quality.nan_price_rows",
            critical=True,
            message=f"数据含 {nan_rows} 行 NaN 价格（回测可能产生异常成交）",
            actual_value=nan_rows,
        ))

    # OHLC 关系违反
    ohlc_bad = manifest.get_field("quality.ohlc_violation_rows", 0)
    if (ohlc_bad or 0) > 0:
        violations.append(RealismViolation(
            field_path="quality.ohlc_violation_rows",
            critical=True,
            message=f"数据含 {ohlc_bad} 行 OHLC 关系违反（high<low 等）",
            actual_value=ohlc_bad,
        ))

    return violations


register_realism_check("data_pit", check_data_pit)
