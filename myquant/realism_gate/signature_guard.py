# 文件名：myquant/realism_gate/signature_guard.py
"""
realism_gate/signature_guard.py - 跨运行可比性守门（§4.10 / P1-16 #366）
=====================================================================
问题（v2.2 G7）：参数优化 / 批量回测 / 候选池聚合排行时，把"真实性配置不同"
（不同策略池 / 数据口径 / 参数范围 / 引擎模式）的回测结果混在一个排行里比收益，
会重演 batch_backtest 指纹缺 mode 的同类缺陷——跨配置不可比。

守门语义：
  - 每次回测产出 RealismManifest.manifest_signature()（剔除运行特异字段后的
    字段哈希）；
  - 聚合排序前以签名为**分组键**：同一签名内才可合并排行；
  - 合并多份结果时校验签名一致，不一致 → 拒绝合并并输出差异清单（哪条记录
    属哪个签名组），防跨配置混排。

分层：本模块为 L0 纯函数（只依赖 manifest.py 的 RealismGateError，不 import
backtest/execution/data_access，R-02/R-16 分层合规）；items 以鸭子类型 dict
消费（读 manifest_signature 字段），避免与 backtest 循环 import。

防未来函数：本模块只做静态分组/比较，不涉时序数据。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from myquant.realism_gate.manifest import RealismGateError

logger = logging.getLogger("myquant.realism_gate.signature_guard")

# 结果记录里携带的签名字段名（param_optimizer.outcome / batch rows 落盘统一口径）
DEFAULT_SIG_FIELD: str = "manifest_signature"


def group_by_signature(
    items: Sequence[Any],
    sig_field: str = DEFAULT_SIG_FIELD,
) -> Dict[Optional[str], List[Any]]:
    """按 manifest_signature 把结果记录分组（分组键 = 签名）。

    Args:
        items: 结果记录列表（dict；非 dict 视为无签名归入 None 组）。
        sig_field: 签名字段名（默认 manifest_signature）。

    Returns:
        dict: {签名: [记录, ...]}；无签名字段的记录归入 None 组。
    """
    groups: Dict[Optional[str], List[Any]] = {}
    for it in items:
        sig = it.get(sig_field) if isinstance(it, dict) else None
        groups.setdefault(sig, []).append(it)
    return groups


def _identity(record: Any) -> str:
    """取一条记录的人类可读标识（用于差异清单）。"""
    if not isinstance(record, dict):
        return type(record).__name__
    parts: List[str] = []
    for key in ("strategy", "symbol", "params", "split", "rule_id"):
        val = record.get(key)
        if val not in (None, "", {}):
            parts.append(f"{key}={val}")
    return ",".join(parts) or "?"


def assert_uniform_signature(
    items: Sequence[Any],
    context: str = "聚合排行",
    sig_field: str = DEFAULT_SIG_FIELD,
) -> None:
    """合并多份结果前校验 manifest 签名一致；不一致即拒绝合并（fail-closed）。

    Args:
        items: 待合并的结果记录列表。
        context: 聚合场景名（写入错误消息，如 "参数优化排行"/"候选池合并"）。
        sig_field: 签名字段名。

    Raises:
        RealismGateError: 出现 ≥2 个不同签名（跨配置禁止合并排行），
            消息含差异清单（每个签名组的记录数与代表标识）。
    """
    groups = group_by_signature(items, sig_field=sig_field)
    present = {sig for sig in groups if sig}  # 有签名的组
    if len(present) <= 1:
        return
    lines: List[str] = [
        f"{context}：发现 {len(present)} 个不同 manifest 签名，"
        f"跨真实性配置禁止合并排行（§4.10）"
    ]
    for sig, group in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0])):
        reps = "; ".join(_identity(it) for it in group[:3])
        sig_label = sig if sig else "<无签名字段>"
        lines.append(f"  签名={sig_label}（{len(group)} 条）: {reps}")
    raise RealismGateError("\n".join(lines), violations=[])


def split_ranked_uniform(
    items: Sequence[Any],
    sig_field: str = DEFAULT_SIG_FIELD,
) -> Dict[Optional[str], List[Any]]:
    """跨签名场景的安全出口：按签名分组返回，调用方须分别排序、禁止跨组合并。

    与 assert_uniform_signature 的区别：本函数不抛错，而是显式分组交回调用方，
    供"同签名内排序、跨签名分开展示"的聚合路径使用。

    Args:
        items: 结果记录列表。
        sig_field: 签名字段名。

    Returns:
        dict: {签名: [该组记录, ...]}，每组内保持原顺序。
    """
    return group_by_signature(items, sig_field=sig_field)


def diff_signatures(
    manifest_a: Dict[str, Any],
    manifest_b: Dict[str, Any],
) -> Dict[str, tuple]:
    """对比两份 manifest 字段，返回差异清单（对称差 + 值不同）。

    用于跨签名拒绝合并时的人工复核：列出哪条真实性字段不一致。

    Args:
        manifest_a: 第一份 manifest.fields（dict）。
        manifest_b: 第二份 manifest.fields（dict）。

    Returns:
        dict: {字段名: (值A, 值B)}；仅含 A 独有/B 独有/两边都有但值不同的字段。
    """
    diff: Dict[str, tuple] = {}
    keys = set(manifest_a) | set(manifest_b)
    for key in sorted(keys):
        a = manifest_a.get(key, "<缺>")
        b = manifest_b.get(key, "<缺>")
        if a != b:
            diff[key] = (a, b)
    return diff
