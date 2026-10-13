# 文件名：myquant/realism_gate/checks/data_pit.py
"""
realism_gate/checks/data_pit.py - 数据 PIT 真实性检查（P0-01 第三刀）
======================================================================

校验 manifest 中"数据"分组字段（v2.2 §4.2）：
  - adjust_degraded_symbols 非空 → degraded（复权降级 raw，价格序列不可比）
  - ~~fingerprint_hash 为 null → unverified~~（#346 真实采集对齐移除：数据快照
    指纹采集未接线（P1-15 关联）→ ``None`` 是**诚实披露**而非谎报；接线后恢复判据）
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

    # ── P1-12 数据快照 pin 判据（#346 占位恢复）──
    # 仅对快照适用的回测（provider 场景 snapshot_applicable=True）判：
    #   · fingerprint_hash 为 None → 快照缺失（无法证明数据未被改动）→ critical；
    #   · snapshot_mismatch=True → 重跑发现 processed 目录与 sidecar 基线不一致
    #     （文件增删改，重跑不可复现）→ critical。
    # 内存预加载 / live（applicable=False）不适用目录快照，跳过不判。
    if manifest.get_field("data.snapshot_applicable", False):
        fp = manifest.get_field("fingerprint_hash", None)
        if fp is None:
            violations.append(RealismViolation(
                field_path="fingerprint_hash",
                critical=True,
                message="数据快照指纹缺失（无法证明 processed 数据在两次回测间未被改动）",
                actual_value=None,
            ))
        if manifest.get_field("data.snapshot_mismatch", False):
            violations.append(RealismViolation(
                field_path="data.snapshot_mismatch",
                critical=True,
                message="数据快照校验不一致：processed 目录与 sidecar 基线不符"
                        "（重跑不可复现；确认数据更新后删除 .snapshot.json 重新 pin）",
                actual_value=True,
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

    # 覆盖率过低（P1-13：过半请求标的无数据 → 样本代表性不足，fail-closed 阻断；
    # 非数值（未统计/占位串）不判——CRITICAL_FIELDS 兜底 "test" 类占位不应误伤）
    coverage = manifest.get_field("quality.coverage_rate", None)
    if (
        isinstance(coverage, (int, float))
        and not isinstance(coverage, bool)
        and coverage < 0.5
    ):
        violations.append(RealismViolation(
            field_path="quality.coverage_rate",
            critical=True,
            message=f"数据覆盖率 {coverage:.2%} < 50%（过半请求标的无数据，样本代表性不足）",
            actual_value=coverage,
        ))

    return violations


register_realism_check("data_pit", check_data_pit)
