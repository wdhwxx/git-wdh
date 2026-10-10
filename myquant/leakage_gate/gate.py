# 文件名：myquant/leakage_gate/gate.py
"""
leakage_gate/gate.py - 门禁中心（最小壳，M0-05）
==================================================
统一配置、Violation、违规汇总、assert_all_gates_pass 出口断言。

设计（v3.0 §5.4/§6.2-4）：
  - GateConfig          门禁启用/关闭配置（enabled / audit_violations）
  - Violation           单条门禁违规记录（gate / rule_id / severity / message / strategy_id / decision_time）
  - LeakageGateError    出口断言失败异常（携带 violations）
  - register_gate_check 门禁检查注册钩子：后续各门禁模块（contract/consumption/
                        static_scan/cross_sectional/pit_proxy）导入时自注册，
                        本模块不直接依赖它们（职责分离，v3.0 §5.4 第 6 步汇总）
  - assert_all_gates_pass 唯一出口强制（run_回测入口.py create_engine /
                        run_实盘入口.py create_live_engine 调用，v3.0 §5.4）：
    依次执行已注册检查并汇总违规；
    - enabled=True  ：违规 → 落 audit_log + 抛 LeakageGateError（阻断出口）
    - enabled=False ：违规 → 仅落 audit_log（警告档），不阻断出口

防未来函数：违规只记录已发生的校验结果；审计 created_ts 由 AuditLogStore 取当前时点
（myquant/core/audit_log.py，BASE-7 哈希链 append-only）；审计写失败记 error 不阻断主流程。

分层：myquant.leakage_gate 登记 MODULE_LAYER L0（test_architecture.py，M0-14）；
仅依赖 myquant.core（L0 合法），不依赖任何 L5 组件。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List

from myquant.core.audit_log import AuditLogStore

logger = logging.getLogger("myquant.leakage_gate.gate")


# ═══════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class GateConfig:
    """
    门禁中心配置（M0-05 最小壳）。

    Attributes:
        enabled: 门禁总开关；True=违规抛 LeakageGateError 阻断出口，
                 False=违规仅审计（警告档）不阻断。
        audit_violations: 违规是否落 audit_log（BASE-7 哈希链 append-only）。
    """

    enabled: bool = True
    audit_violations: bool = True


_CONFIG: GateConfig = GateConfig()


def get_config() -> GateConfig:
    """
    返回当前门禁配置（模块级单例）。

    Returns:
        GateConfig: 当前门禁配置。
    """
    return _CONFIG


def set_config(config: GateConfig) -> None:
    """
    覆盖门禁配置（测试与运行期装配用）。

    Args:
        config: 新的门禁配置。
    """
    global _CONFIG
    _CONFIG = config


# ═══════════════════════════════════════════════
#  Violation 与异常
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class Violation:
    """
    单条门禁违规记录（台账条目）。

    Attributes:
        gate: 违规门禁名（contract/consumption/static_scan/cross_sectional/pit_proxy）。
        rule_id: 规则 ID（R01~R11，见 v3.0 §10）。
        severity: 档位（阻断/警告；对应红线清单 RL-01~14 的豁免档位）。
        message: 违规说明。
        strategy_id: 涉事策略名（契约主键 name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。
    """

    gate: str
    rule_id: str
    severity: str
    message: str
    strategy_id: str = ""
    decision_time: str = ""


class LeakageGateError(RuntimeError):
    """
    出口断言失败异常（携带全部 violations）。

    由 assert_all_gates_pass 在 enabled 且存在违规时抛出；
    调用方（引擎创建点）捕获后阻断回测/实盘启动。
    """

    def __init__(self, violations: List[Violation]):
        self.violations: List[Violation] = list(violations)
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [f"防泄漏门禁未通过（{len(self.violations)} 项违规）:"]
        for v in self.violations:
            suffix = (
                f" (strategy={v.strategy_id}, decision_time={v.decision_time})"
                if v.strategy_id
                else ""
            )
            lines.append(f"  [{v.gate}/{v.rule_id}/{v.severity}] {v.message}{suffix}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════
#  门禁检查注册与执行
# ═══════════════════════════════════════════════

# 门禁检查注册表：name -> 检查函数（输入 strategy_id/decision_time，输出 List[Violation]）
_GATE_CHECKS: Dict[str, Callable[[str, str], List[Violation]]] = {}


def register_gate_check(
    name: str, fn: Callable[[str, str], List[Violation]]
) -> None:
    """
    注册门禁检查函数（各门禁模块导入时自注册）。

    Args:
        name: 门禁名（须唯一）。
        fn: 检查函数，签名 (strategy_id: str, decision_time: str) -> List[Violation]，
            无违规返回空列表。

    Raises:
        ValueError: name 为空或重复注册。
    """
    name_s = (name or "").strip()
    if not name_s:
        raise ValueError("门禁检查注册失败: name 不能为空")
    if name_s in _GATE_CHECKS:
        raise ValueError(f"门禁检查重复注册: {name_s}")
    _GATE_CHECKS[name_s] = fn
    logger.debug(f"门禁检查已注册: {name_s}")


def reset_gate_checks() -> None:
    """
    清空门禁检查注册表（测试隔离用，不影响模块级配置）。
    """
    _GATE_CHECKS.clear()


def _collect_violations(strategy_id: str, decision_time: str) -> List[Violation]:
    """
    执行全部已注册门禁检查，汇总违规（按注册顺序）。

    门禁检查自身异常按 fail-closed 处理：记一条"阻断"级违规（gate-error），
    不静默跳过（防线自身故障不得放行）。

    Args:
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。

    Returns:
        List[Violation]: 汇总的全部违规（无违规为空列表）。
    """
    all_v: List[Violation] = []
    for name, fn in _GATE_CHECKS.items():
        try:
            vs = fn(strategy_id, decision_time) or []
            all_v.extend(vs)
        except Exception as e:  # noqa: BLE001 - 门禁检查自身异常按违规记录（fail-closed）
            all_v.append(
                Violation(
                    gate=name,
                    rule_id="gate-error",
                    severity="阻断",
                    message=f"门禁检查执行异常: {e}",
                    strategy_id=strategy_id,
                    decision_time=decision_time,
                )
            )
    return all_v


# ═══════════════════════════════════════════════
#  出口断言
# ═══════════════════════════════════════════════

def _append_audit(violations: List[Violation]) -> None:
    """
    违规落 audit_log（append-only 哈希链）；写失败记 error 不阻断主流程。

    Args:
        violations: 待落审计的违规列表。
    """
    try:
        detail = {
            "count": len(violations),
            "violations": [
                {
                    "gate": v.gate,
                    "rule_id": v.rule_id,
                    "severity": v.severity,
                    "message": v.message,
                    "strategy_id": v.strategy_id,
                    "decision_time": v.decision_time,
                }
                for v in violations
            ],
        }
        AuditLogStore().append(
            "leakage_gate", "violation", actor="leakage_gate", detail=detail
        )
    except Exception as e:  # noqa: BLE001 - 审计写失败不阻断主流程（防未来：已发生即记录，失败仅 error）
        logger.error(f"门禁违规落审计失败（不阻断主流程）: {e}")


def assert_all_gates_pass(strategy_id: str, decision_time: str) -> None:
    """
    门禁中心出口断言（唯一出口强制，v3.0 §5.4）。

    依次执行已注册门禁检查并汇总违规：
    - 无违规：直接通过；
    - 有违规：若 audit_violations 则落 audit_log；
      enabled=True 抛 LeakageGateError（阻断出口），
      enabled=False 仅审计（警告档）不阻断。

    Args:
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。

    Raises:
        LeakageGateError: enabled 且存在违规时抛出。
    """
    violations = _collect_violations(strategy_id, decision_time)
    if not violations:
        return
    config = get_config()
    if config.audit_violations:
        _append_audit(violations)
    if config.enabled:
        raise LeakageGateError(violations)
    logger.warning(
        f"门禁违规（disabled 档，仅审计不阻断）: {len(violations)} 项，strategy={strategy_id}"
    )
