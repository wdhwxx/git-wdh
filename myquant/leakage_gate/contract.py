# 文件名：myquant/leakage_gate/contract.py
"""
leakage_gate/contract.py - 契约完整性校验（M0-06）
=====================================================
校验策略契约声明（decision_time / available_time / universe PIT / consumed_columns /
契约完整性），并与列级时点台账（config/data_time_semantics.yaml）比对——
**契约不能自证 PIT**（v3.0 附录 B 注 2）。

契约真源 = config/strategy_registry.yaml（M0-03 机读注册表，供出口强制与契约校验消费）。
本模块不 import strategies 包（分层：leakage_gate=L0、strategies=L3，L0→L3 反向跨层会被
R-02 门禁拦截，M0-14 验收）；策略类属性契约由 M0-04 注册强制校验（strategies 层内自校验），
本模块校验 yaml 真源声明 + 与台账比对。

职责（v3.0 §5.4 第 6 步 / §6.2-5 / §5.2-245）：
  - 只负责校验，不负责注册
  - 以门禁检查挂入 gate.py（register_gate_check("contract", check_contract)，导入即注册），
    由 assert_all_gates_pass 在唯一出口（create_engine / create_live_engine）统一执行

校验项（验收：decision_time / available_time 声明完整；与列级台账比对）：
  A. 声明完整（rule_id=R09，红线 RL-02 契约层环节）：
     - decision_time / execution_time / holding_period 非空
     - universe 为 dict 且含显式 pit_status（避免契约自证 PIT，v3.0 附录 B 注 4）
     - data_availability 为 dict 且含 market_data 与 financial_data 两节，
       各节 available_time 非空；financial_data.basis 非空且必须为 eff_disclosure_date
     - consumed_columns 为非空 list（R11 声明侧输入，可漏报但不得缺失）
     - ruleset 非空（strategy_registry.yaml 映射）
  B. 与列级台账比对（rule_id=R10，红线 RL-11）：
     - 台账必须存在且可读；缺失 → 阻断违规（契约不能自证 PIT，须先完成 M0P-02）
     - 声明 financial_data.basis 与台账财务列 target_basis 一致（目标日基语义对齐）
     - 行情列 收盘价 已登记且 date_basis=trade_date（T 日可得，与声明一致）

防未来函数：本模块只校验"已声明"的契约与"已登记"的台账，不预测未来。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from myquant.core.config_loader import ConfigLoader
from myquant.leakage_gate.gate import Violation, register_gate_check

logger = logging.getLogger("myquant.leakage_gate.contract")

# 契约真源（M0-03 机读注册表；契约主键=name，DEC-01）
_REGISTRY_PATH: str = "config/strategy_registry.yaml"

# 列级时点台账（机读，M0P-02 建；契约不能自证 PIT，v3.0 附录 B 注 2）
_LEDGER_PATH: str = "config/data_time_semantics.yaml"

# 统一时点日落点（v3.0 §6.4.2：财务 join 必须走 eff_disclosure_date，RL-02）
_EFF_DISCLOSURE_BASIS: str = "eff_disclosure_date"

# 行情族日基（T 日可得；台账登记口径，v3.0 §6.1）
_TRADE_DATE_BASIS: str = "trade_date"


# ═══════════════════════════════════════════════
#  配置读取（经 core.config_loader，L0 合法）
# ═══════════════════════════════════════════════

def _load_yaml(path: str) -> Optional[Dict[str, Any]]:
    """
    读取 yaml 配置（经 core.config_loader）。

    Args:
        path: 相对项目根的配置路径。

    Returns:
        Optional[Dict[str, Any]]: 配置字典；缺失/解析失败返回 None。
    """
    try:
        return ConfigLoader().load(path)
    except Exception as e:  # noqa: BLE001 - 配置缺失/损坏按校验失败处理（fail-closed）
        logger.error(f"配置加载失败 {path}: {e}")
        return None


def _load_registry() -> Optional[Dict[str, Any]]:
    """读取策略契约注册表（config/strategy_registry.yaml）。"""
    return _load_yaml(_REGISTRY_PATH)


def _load_ledger() -> Optional[Dict[str, Any]]:
    """读取列级时点台账（config/data_time_semantics.yaml）。"""
    return _load_yaml(_LEDGER_PATH)


def _get_entry(
    registry: Dict[str, Any], strategy_id: str
) -> Optional[Dict[str, Any]]:
    """
    按契约主键 name 取注册表条目。

    Args:
        registry: strategy_registry.yaml 内容。
        strategy_id: 策略契约主键（name）。

    Returns:
        Optional[Dict[str, Any]]: 策略条目；未登记返回 None。
    """
    for item in registry.get("strategies") or []:
        if isinstance(item, dict) and item.get("name") == strategy_id:
            return item
    return None


# ═══════════════════════════════════════════════
#  声明完整校验（R09 / RL-02 契约层）
# ═══════════════════════════════════════════════

def _validate_declaration(
    entry: Dict[str, Any], strategy_id: str, decision_time: str
) -> List[Violation]:
    """
    校验契约声明完整性（深度结构，R09）。

    Args:
        entry: strategy_registry.yaml 中策略条目。
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。

    Returns:
        List[Violation]: 声明不完整的违规列表（无违规为空列表）。
    """
    v: List[Violation] = []

    def red(msg: str) -> Violation:
        return Violation(
            gate="contract", rule_id="R09", severity="阻断",
            message=msg, strategy_id=strategy_id, decision_time=decision_time,
        )

    if not str(entry.get("decision_time", "")).strip():
        v.append(red("decision_time 声明缺失"))
    if not str(entry.get("execution_time", "")).strip():
        v.append(red("execution_time 声明缺失"))
    if not str(entry.get("holding_period", "")).strip():
        v.append(red("holding_period 声明缺失"))

    universe = entry.get("universe")
    if not isinstance(universe, dict) or not str(universe.get("pit_status", "")).strip():
        v.append(
            red("universe 声明缺失或未显式声明 pit_status（契约不得自证 PIT，v3.0 附录 B 注4）")
        )

    da = entry.get("data_availability")
    if not isinstance(da, dict):
        v.append(red("data_availability 声明缺失"))
    else:
        md = da.get("market_data")
        fd = da.get("financial_data")
        if not isinstance(md, dict) or not str(md.get("available_time", "")).strip():
            v.append(red("data_availability.market_data.available_time 声明缺失"))
        if not isinstance(fd, dict) or not str(fd.get("available_time", "")).strip():
            v.append(red("data_availability.financial_data.available_time 声明缺失"))
        if isinstance(fd, dict) and not str(fd.get("basis", "")).strip():
            v.append(
                red(f"data_availability.financial_data.basis 声明缺失（须为 {_EFF_DISCLOSURE_BASIS}，RL-02）")
            )
        elif (
            isinstance(fd, dict)
            and str(fd.get("basis", "")).strip() != _EFF_DISCLOSURE_BASIS
        ):
            v.append(
                red(
                    f"data_availability.financial_data.basis 必须为 {_EFF_DISCLOSURE_BASIS}"
                    f"（统一时点日落点，RL-02），实际: {fd.get('basis')!r}"
                )
            )

    cc = entry.get("consumed_columns")
    if not isinstance(cc, list) or not cc:
        v.append(red("consumed_columns 声明缺失（R11 声明侧输入，须非空列表）"))

    if not str(entry.get("ruleset", "")).strip():
        v.append(red("ruleset 声明缺失（strategy_registry.yaml 映射）"))

    return v


# ═══════════════════════════════════════════════
#  与列级台账比对（R10 / RL-11）
# ═══════════════════════════════════════════════

def _validate_against_ledger(
    entry: Dict[str, Any], strategy_id: str, decision_time: str
) -> List[Violation]:
    """
    契约声明与列级时点台账比对（R10）。

    契约不能自证 PIT：台账缺失/不可读/为空 → 阻断违规（须先完成 M0P-02）；
    声明 financial_data.basis 与台账财务列 target_basis 必须一致；
    行情列 收盘价 必须登记且 date_basis=trade_date（T 日可得）。

    Args:
        entry: strategy_registry.yaml 中策略条目。
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。

    Returns:
        List[Violation]: 比对不一致的违规列表（无违规为空列表）。
    """
    v: List[Violation] = []

    def red(msg: str) -> Violation:
        return Violation(
            gate="contract", rule_id="R10", severity="阻断",
            message=msg, strategy_id=strategy_id, decision_time=decision_time,
        )

    ledger = _load_ledger()
    if ledger is None:
        v.append(
            red(
                f"列级台账 {_LEDGER_PATH} 缺失或不可读；契约不能自证 PIT"
                f"（v3.0 附录 B 注2），须先完成 M0P-02 建台账"
            )
        )
        return v

    columns = ledger.get("columns")
    if not isinstance(columns, dict) or not columns:
        v.append(red("列级台账 columns 为空（RL-11：数据列时点未登记）"))
        return v

    # 财务族：声明 basis 与台账财务列 target_basis 一致（目标日基语义对齐）
    basis_declared: str = ""
    da = entry.get("data_availability")
    if isinstance(da, dict):
        fd = da.get("financial_data")
        if isinstance(fd, dict):
            basis_declared = str(fd.get("basis", "")).strip()
    if basis_declared:
        bad_target = sorted(
            c for c, m in columns.items()
            if isinstance(m, dict)
            and str(m.get("target_basis", "")).strip()
            and str(m.get("target_basis", "")).strip() != basis_declared
        )
        if bad_target:
            v.append(
                red(
                    f"声明 financial_data.basis={basis_declared!r} 与台账目标基不一致: "
                    f"{bad_target}（RL-11）"
                )
            )

    # 行情族：收盘价必须登记且日基为 trade_date（T 日可得）
    close_meta = columns.get("收盘价")
    if (
        not isinstance(close_meta, dict)
        or str(close_meta.get("date_basis", "")).strip() != _TRADE_DATE_BASIS
    ):
        v.append(
            red(
                f"台账行情列 收盘价 未登记或 date_basis 非 {_TRADE_DATE_BASIS}"
                f"（RL-11：行情日基必须 T 日可得，与声明 available_time 一致）"
            )
        )

    return v


# ═══════════════════════════════════════════════
#  门禁检查入口
# ═══════════════════════════════════════════════

def check_contract(strategy_id: str, decision_time: str) -> List[Violation]:
    """
    契约完整性门禁检查（挂入门禁中心，register_gate_check("contract", ...)）。

    Args:
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。

    Returns:
        List[Violation]: 全部契约违规（声明不完整 R09 + 台账比对 R10）；无违规为空列表。
    """
    registry = _load_registry()
    if registry is None:
        return [
            Violation(
                gate="contract", rule_id="R09", severity="阻断",
                message=f"契约注册表 {_REGISTRY_PATH} 缺失或不可读（须先完成 M0-03）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        ]
    entry = _get_entry(registry, strategy_id)
    if entry is None:
        return [
            Violation(
                gate="contract", rule_id="R09", severity="阻断",
                message=f"策略未登记于契约注册表: {strategy_id}（契约主键=name，DEC-01）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        ]
    violations: List[Violation] = list(
        _validate_declaration(entry, strategy_id, decision_time)
    )
    violations.extend(_validate_against_ledger(entry, strategy_id, decision_time))
    return violations


def ensure_registered() -> None:
    """
    幂等注册契约检查到门禁中心（导入即注册；测试 reset_gate_checks 后可恢复）。

    重复注册由 gate.register_gate_check 的 ValueError 拦截，此处捕获保证幂等。
    """
    try:
        register_gate_check("contract", check_contract)
    except ValueError:
        pass  # 已注册（幂等）


ensure_registered()
