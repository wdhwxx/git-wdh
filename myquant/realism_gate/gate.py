# 文件名：myquant/realism_gate/gate.py
"""
realism_gate/gate.py - 门禁中心出口断言（P0-01 第二刀）
==========================================================

统一配置、检查注册、清单完整性校验、出口断言、审计落链。

设计（v2.2 §4.3 出口断言语义 + §4.4 两阶段握手）：
  - RealismGateConfig      门禁启用/关闭配置（enabled / audit_violations）
  - register_realism_check 检查注册钩子：各真实性检查模块（checks/*）导入时自注册，
                           本模块不直接依赖它们（职责分离）
  - assert_realism_pass    唯一出口断言（BacktestScheduler.create_engine 调用，§4.4）：
    时序（不可颠倒）：
      ① manifest 由引擎装配期 collect_manifest() 采集字段
      ② assert_realism_pass(manifest) 内部先 build() 完整性校验
         —— 缺 critical → ManifestIncompleteError（fail-closed）
      ③ 执行已注册检查函数，汇总违规
      ④ enabled=True 且有 critical 违规 → 落 audit_log + 抛 RealismGateError（阻断）
      ⑤ enabled=False → 仅落 audit_log（audit 档），不阻断
      ⑥ 通过 → seal(manifest)（§4.4 第⑤步：引擎入口断言 is_sealed()）

与 leakage_gate/gate.py 的三处刻意不同（v2.2 §4.3）：
  - 配置缺失 = 阻断（leakage_gate 段缺失落默认 True，本门禁要求显式启用）
  - 必须落盘（清单强制写入 results.json，不允许只进审计链）
  - **无 severity 字段**（只保留 critical 布尔，唯一阻断判据）

分层：myquant.realism_gate 登记 MODULE_LAYER L0；
仅依赖 myquant.core.audit_log（L0 合法），**禁止** import myquant.backtest /
myquant.execution / myquant.data_access（R-02/R-16）。运行时事实由 L3/L4 注入 manifest。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List

from myquant.core.audit_log import AuditLogStore
from myquant.realism_gate.manifest import (
    ManifestIncompleteError,
    RealismGateError,
    RealismManifest,
    RealismViolation,
)

logger = logging.getLogger("myquant.realism_gate.gate")


# ═══════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class RealismGateConfig:
    """门禁中心配置（v2.2 §4.3）。

    Attributes:
        enabled: 门禁总开关；True=critical 违规抛 RealismGateError 阻断出口，
                 False=critical 违规仅审计落盘不阻断（audit 档，分期启用期用）。
        audit_violations: 违规是否落 audit_log（BASE-7 哈希链 append-only）。
    """

    enabled: bool = False
    audit_violations: bool = True


_CONFIG: RealismGateConfig = RealismGateConfig()


def get_config() -> RealismGateConfig:
    """返回当前门禁配置（模块级单例）。"""
    return _CONFIG


def set_config(config: RealismGateConfig) -> None:
    """覆盖门禁配置（测试与运行期装配用）。

    Args:
        config: 新的门禁配置。
    """
    global _CONFIG
    _CONFIG = config


# ═══════════════════════════════════════════════
#  检查注册表
# ═══════════════════════════════════════════════

# 检查函数签名：接收已 build 的 RealismManifest，返回 List[RealismViolation]
RealismCheckFn = Callable[[RealismManifest], List[RealismViolation]]

_CHECKS: Dict[str, RealismCheckFn] = {}


def register_realism_check(name: str, fn: RealismCheckFn) -> None:
    """注册真实性门禁检查函数（各 checks/* 模块导入时自注册）。

    Args:
        name: 检查名（须唯一，如 "market_rules" / "cost_model" / "data_pit" / "sample_pool"）。
        fn: 检查函数，签名 (manifest: RealismManifest) -> List[RealismViolation]，
            无违规返回空列表。

    Raises:
        ValueError: name 为空或重复注册。
    """
    name_s = (name or "").strip()
    if not name_s:
        raise ValueError("真实性门禁检查注册失败: name 不能为空")
    if name_s in _CHECKS:
        raise ValueError(f"真实性门禁检查重复注册: {name_s}")
    _CHECKS[name_s] = fn
    logger.debug(f"真实性门禁检查已注册: {name_s}")


def reset_realism_checks() -> None:
    """清空检查注册表（测试隔离用，不影响模块级配置）。"""
    _CHECKS.clear()


def _collect_violations(manifest: RealismManifest) -> List[RealismViolation]:
    """执行全部已注册检查，汇总违规（按注册顺序）。

    检查函数自身异常按 fail-closed 处理：记一条 critical 的 realism-gate-error 违规，
    不静默跳过（防线自身故障不得放行）。

    Args:
        manifest: 已 build 的 RealismManifest 实例。

    Returns:
        汇总的全部违规（无违规为空列表）。
    """
    all_v: List[RealismViolation] = []
    for name, fn in _CHECKS.items():
        try:
            vs = fn(manifest) or []
            all_v.extend(vs)
        except Exception as e:  # noqa: BLE001 - 检查自身异常按违规记录（fail-closed）
            logger.error(f"真实性门禁检查执行异常（fail-closed）: {name}: {e}")
            all_v.append(RealismViolation(
                field_path=f"{name}.gate_error",
                critical=True,
                message=f"真实性门禁检查执行异常: {e}",
                actual_value=None,
            ))
    return all_v


# ═══════════════════════════════════════════════
#  审计落链
# ═══════════════════════════════════════════════

def _append_audit(violations: List[RealismViolation]) -> None:
    """违规落 audit_log（append-only 哈希链）；写失败记 error 不阻断主流程。

    Args:
        violations: 待落审计的违规列表。
    """
    try:
        detail = {
            "count": len(violations),
            "violations": [
                {
                    "field_path": v.field_path,
                    "critical": v.critical,
                    "message": v.message,
                    "actual_value": repr(v.actual_value),
                    "decision_time": v.decision_time,
                }
                for v in violations
            ],
        }
        AuditLogStore().append(
            "realism_gate", "violation", actor="realism_gate", detail=detail
        )
    except Exception as e:  # noqa: BLE001 - 审计写失败不阻断主流程
        logger.error(f"真实性门禁违规落审计失败（不阻断主流程）: {e}")


# ═══════════════════════════════════════════════
#  出口断言（唯一）
# ═══════════════════════════════════════════════

def assert_realism_pass(manifest: RealismManifest) -> None:
    """真实性门禁出口断言（唯一出口强制，v2.2 §4.3 + §4.4）。

    时序（不可颠倒）：
      ① 先 build(manifest) 完整性校验——缺 critical 字段 → ManifestIncompleteError
      ② 执行已注册检查函数，汇总违规
      ③ 有 critical 违规：
         - enabled=True → 落 audit_log + 抛 RealismGateError（阻断引擎创建/启动）
         - enabled=False → 仅落 audit_log（audit 档）+ 不阻断（分期启用期）
      ④ 无违规 → seal(manifest)（§4.4 第⑤步，引擎入口断言 is_sealed()）

    Args:
        manifest: 待断言的 RealismManifest（由引擎装配期采集字段后传入）。

    Raises:
        ManifestIncompleteError: critical 字段缺失时（fail-closed）。
        RealismGateError: enabled 且存在 critical 违规时（阻断出口）。
    """
    # ① 完整性校验（fail-closed：缺 critical 字段即阻断，不允许空值通过）
    manifest.build()

    # ② 执行已注册检查
    check_violations = _collect_violations(manifest)
    # 合并 build() 阶段已检出的违规（枚举取值域等）
    all_violations = list(manifest.violations) + check_violations

    # 过滤出 critical 违规（v2.2：无 severity，critical 是唯一阻断判据）
    critical_violations = [v for v in all_violations if v.critical]

    if not critical_violations:
        # ④ 通过 → 封印
        manifest.seal()
        logger.debug(
            f"真实性门禁通过（run_id={manifest.get_field('run_id', '?')}，"
            f"status={manifest.status}）"
        )
        return

    # ③ 有 critical 违规
    config = get_config()
    if config.audit_violations:
        _append_audit(critical_violations)

    if config.enabled:
        # 阻断路径：抛 RealismGateError（携带 violations + manifest 快照）
        raise RealismGateError(
            f"真实性门禁未通过：{len(critical_violations)} 项 critical 违规"
            f"（run_id={manifest.get_field('run_id', '?')}）",
            violations=critical_violations,
            manifest=manifest,
        )

    # audit 档：仅审计落盘，不阻断
    logger.warning(
        f"真实性门禁违规（disabled 档，仅审计不阻断）: {len(critical_violations)} 项 "
        f"critical，run_id={manifest.get_field('run_id', '?')}"
    )
