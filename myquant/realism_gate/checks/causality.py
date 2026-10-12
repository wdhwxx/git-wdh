# 文件名：myquant/realism_gate/checks/causality.py
"""
realism_gate/checks/causality.py - 因果切片真实性检查（P0-08 子项⑤）
====================================================================

判据（施工方案 v2.2 §5.4.1 子项⑤/⑥，L738-739）：
  1. ``causality.context_sliced = False`` 且 **未声明全量逃生口**
     （``causality.full_sample_declared = False``）→ **critical 违规**。
  2. **访问器层实测**（P1-07② 判据，子项⑥）：``causality.accessed_future = True``
     （run 收尾评估 accessed_max_date > current_bar_date）→ **critical 违规**——
     真拦截：策略实际读到了未来。
  3. **静态声明交叉校验**（子项⑥用例⑭）：``causality.static_declared_but_accessed_future
     = True``（静态声明 context_sliced=true 但访问器层实测读到未来）→ **critical
     违规**——声明与实测矛盾（manifest 谎报已保护）。

语义：引擎逐 bar 置位 ``_slicing_enabled``（event/mixed 路径）或声明全量逃生口
（vectorized 路径，``full_sample_declared=True``）。两信号均缺 = 回测在
**未封门**状态下运行——策略 ``get_data()`` 可取到全量（含未来数据）却无任何
声明 → 回测结果不真实。**宁可阻断，不可静默放行。**

接入子项④（止血 C，#338）fail-open 降级信号：engine/mixed 置位异常时显式写
``causality.context_sliced=false`` → 本检查将其判为 critical，用例⑫②闭环。

L0 纯函数：只消费 manifest 注入的字段值，不 import L3/L4（R-02/R-16）。
字段缺失（None）不在此触发——缺失由 manifest.build() fail-closed 处理。
"""

from __future__ import annotations

from typing import List

from myquant.realism_gate.gate import register_realism_check
from myquant.realism_gate.manifest import RealismManifest, RealismViolation


def check_causality(manifest: RealismManifest) -> List[RealismViolation]:
    """因果切片真实性检查：未封门且未声明全量 → critical 违规。

    Args:
        manifest: 已 build 的 RealismManifest。

    Returns:
        违规列表（无违规为空列表）。
    """
    violations: List[RealismViolation] = []

    context_sliced = manifest.get_field("causality.context_sliced", None)
    full_sample_declared = manifest.get_field("causality.full_sample_declared", False)

    if context_sliced is False and not full_sample_declared:
        violations.append(RealismViolation(
            field_path="causality.context_sliced",
            critical=True,
            message=(
                "因果切片未生效且未声明全量逃生口（context_sliced=false、"
                "full_sample_declared=false）——策略 get_data 可取全量（含未来），"
                "回测结果不真实"
            ),
            actual_value=context_sliced,
        ))

    # ── 子项⑥：访问器层实测未来（P1-07② 真拦截判据）──
    if manifest.get_field("causality.accessed_future", False):
        acct_v = manifest.get_field("causality.accounting_violations", [])
        violations.append(RealismViolation(
            field_path="causality.accessed_future",
            critical=True,
            message=(
                "访问器层实测读到未来数据（accessed_max_date > current_bar_date）："
                f"{acct_v}"
            ),
            actual_value=True,
        ))

    # ── 子项⑥：静态声明交叉校验（用例⑭）──
    # context_sliced=true 是静态声明，记账是实测；两者不一致即报错
    if manifest.get_field("causality.static_declared_but_accessed_future", False):
        violations.append(RealismViolation(
            field_path="causality.static_declared_but_accessed_future",
            critical=True,
            message=(
                "静态声明 context_sliced=true（已封门）但访问器层实测读到未来——"
                "声明与实测矛盾（manifest 谎报已保护）"
            ),
            actual_value=True,
        ))

    return violations


register_realism_check("causality", check_causality)
