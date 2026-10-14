# 文件名：myquant/leakage_gate/consumption.py
"""
leakage_gate/consumption.py - 消费侧列门禁（L0.5，M0-07）
=============================================================
把台账 `severity: 阻断` 变成出口可见的阻断项：策略实际引用列 ∪ 声明消费列
与阻断列交集非空 → 报红（R11 / RL-13，v3.0 §6.1.3）。

输入（v3.0 §6.1.3）：
  ① 台账 severity 映射（config/data_time_semantics.yaml，经 core.config_loader）
  ② 策略实际引用列清单（AST 扫描策略源码：df["列名"] / df.列名 / columns=[...] /
     字符串常量比对台账键名）
  ③ 契约 consumed_columns 声明为辅（config/strategy_registry.yaml）

判据：`实测引用列 ∪ 声明列 ∩ {severity == 阻断} ≠ ∅` → 出口报红。

执行点：assert_all_gates_pass（create_engine / create_live_engine，v3.0 §5.4 第 3 步）。

分层：leakage_gate=L0。本模块只读策略源码**文件**（AST 解析，非 import），
不 import strategies 包（L0→L3 反向跨层会被 R-02 拦截）；配置经 core.config_loader（L0 合法）。

防未来函数：本模块只比对"已发生引用"与"已登记台账"，不预测未来。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from myquant.core.config_loader import ConfigLoader
from myquant.leakage_gate.contract import _get_entry, _load_ledger, _load_registry
from myquant.leakage_gate.gate import Violation, register_gate_check
from myquant.leakage_gate.static_scan import extract_referenced_columns  # noqa: F401  # canonical 单实现（M1B-01）

logger = logging.getLogger("myquant.leakage_gate.consumption")

# 策略源码目录（只读文件做 AST，非 import；分层合规）
_STRATEGIES_DIR: Path = Path(__file__).resolve().parents[2] / "myquant" / "strategies"

# 台账阻断档 severity 值（v3.0 §6.1：severity=阻断）
_BLOCKED_SEVERITY: str = "阻断"


# ═══════════════════════════════════════════════
#  AST 列引用提取（纯函数）
# ═══════════════════════════════════════════════

def extract_columns_from_source(source: str, ledger_keys: Set[str]) -> Set[str]:
    """
    从策略源码提取引用列（纯函数，AST 扫描）。

    覆盖（v3.0 §6.1.3 引用列取法）：
      - df["列名"]（ast.Subscript 内字符串常量）
      - df.列名（ast.Attribute.attr）
      - columns=[...] / 任意字符串常量比对台账键名
      - 非台账键名的字符串常量被过滤（不误报注释/日志等）

    M1B-01 起委托 static_scan.extract_referenced_columns（canonical 单一实现，
    与静态扫描器输出列引用清单判定一致，v3.0 §6.5 验收）。

    Args:
        source: 策略源码文本。
        ledger_keys: 台账全部列名集合（候选过滤）。

    Returns:
        Set[str]: 命中的引用列集合。
    """
    return extract_referenced_columns(source, ledger_keys)


# ═══════════════════════════════════════════════
#  策略源码定位（文件级，非 import）
# ═══════════════════════════════════════════════

def _file_defines_name(source: str, strategy_id: str) -> bool:
    """
    判断源码文件是否定义了 name == strategy_id 的策略类。

    Args:
        source: 源码文本。
        strategy_id: 策略契约主键（name）。

    Returns:
        bool: 文件中存在 name 属性等于 strategy_id 的类。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if (
                isinstance(item, ast.Assign)
                and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)
                and item.targets[0].id == "name"
                and isinstance(item.value, ast.Constant)
                and item.value.value == strategy_id
            ):
                return True
    return False


def _strategy_source(strategy_id: str) -> Optional[str]:
    """
    读取定义指定策略的源码文件文本（AST 定位，非 import）。

    Args:
        strategy_id: 策略契约主键（name）。

    Returns:
        Optional[str]: 源码文本；未定位或读取失败返回 None。
    """
    if not _STRATEGIES_DIR.is_dir():
        logger.error(f"策略目录不存在: {_STRATEGIES_DIR}")
        return None
    for py in sorted(_STRATEGIES_DIR.glob("*.py")):
        if py.name in ("__init__.py", "base.py", "registry.py", "param_tuner.py"):
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError as e:
            logger.error(f"策略源码读取失败 {py}: {e}")
            continue
        if _file_defines_name(text, strategy_id):
            return text
    return None


# ═══════════════════════════════════════════════
#  纯函数比对（验收核心）
# ═══════════════════════════════════════════════

def validate_consumption(
    declared: List[str], actual: Set[str], blocked: Set[str]
) -> List[str]:
    """
    消费侧列门禁纯函数比对（M0-07 验收：实测引用列 ∩ severity=阻断 ≠ ∅ → 命中）。

    Args:
        declared: 契约 consumed_columns 声明列（可漏报，取并集）。
        actual: AST 实测引用列。
        blocked: 台账中 severity=阻断 的列集合。

    Returns:
        List[str]: 命中阻断列（排序）；无命中为空列表。
    """
    return sorted((set(declared) | actual) & blocked)


def audit_blocked_column_literals(
    sources: Sequence[str],
    blocked_columns: Optional[Sequence[str]] = None,
) -> List[Dict[str, int]]:
    """
    阻断列名字面量出现次数审计（总验收 G-02 兜底，R11 补充防线，警告档不阻断）。

    背景：R11 的 AST 列引用扫描对**间接引用**不生效——`col = "每股收益"; df[col]`、
    `df[cols]`（变量列表）、循环遍历列名等场景静态扫不到（v3.0 §12.4-G-02）。
    本审计对 `severity: 阻断` 的列名做源码中文字面量出现计数：命中即返回
    （**警告，不阻断**），由人工复核是否为间接引用或误报。此为 R11 的补充防线，
    不替代主判据 `validate_consumption` 的出口报红。

    Args:
        sources: 待审计的源码文本列表。
        blocked_columns: 阻断列清单；None 时从列级台账加载（severity=阻断）。

    Returns:
        List[Dict[str, int]]: [{column, count}] 命中（count>0，按列排序）；无命中为空列表。
    """
    if blocked_columns is not None:
        blocked = sorted(str(c) for c in blocked_columns)
    else:
        blocked = sorted(_blocked_columns(_load_ledger() or {}))
    if not blocked:
        return []
    hits: List[Dict[str, int]] = []
    for col in blocked:
        count = sum(text.count(col) for text in sources)
        if count > 0:
            hits.append({"column": col, "count": count})
    return hits


# ═══════════════════════════════════════════════
#  门禁检查入口
# ═══════════════════════════════════════════════

def _blocked_columns(ledger: Dict[str, Any]) -> Set[str]:
    """提取台账中 severity=阻断 的列集合。"""
    columns = ledger.get("columns")
    if not isinstance(columns, dict):
        return set()
    return {
        c for c, m in columns.items()
        if isinstance(m, dict) and str(m.get("severity", "")).strip() == _BLOCKED_SEVERITY
    }


def check_consumption(strategy_id: str, decision_time: str) -> List[Violation]:
    """
    消费侧列门禁检查（挂入门禁中心，register_gate_check("consumption", ...)）。

    Args:
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。

    Returns:
        List[Violation]: 引用阻断档数据列的违规（R11/RL-13）；无违规为空列表。
    """
    ledger = _load_ledger()
    if ledger is None:
        return [
            Violation(
                gate="consumption", rule_id="R11", severity="阻断",
                message="列级台账缺失或不可读；消费侧门禁无法判定（契约不能自证 PIT，须先完成 M0P-02）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        ]
    columns = ledger.get("columns")
    if not isinstance(columns, dict) or not columns:
        return [
            Violation(
                gate="consumption", rule_id="R11", severity="阻断",
                message="列级台账 columns 为空（RL-11：数据列时点未登记）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        ]

    registry = _load_registry()
    entry = _get_entry(registry or {}, strategy_id) if registry else None
    if entry is None:
        return [
            Violation(
                gate="consumption", rule_id="R11", severity="阻断",
                message=f"策略未登记于契约注册表: {strategy_id}（消费侧门禁无法比对）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        ]

    blocked = _blocked_columns(ledger)
    declared = [str(c) for c in (entry.get("consumed_columns") or [])]
    source = _strategy_source(strategy_id) or ""
    actual = extract_columns_from_source(source, set(columns))
    hits = validate_consumption(declared, actual, blocked)
    if hits:
        return [
            Violation(
                gate="consumption", rule_id="R11", severity="阻断",
                message=f"引用台账阻断档数据列: {hits}（RL-13/R11，v3.0 §6.1.3）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        ]
    return []


def ensure_registered() -> None:
    """
    幂等注册消费侧检查到门禁中心（导入即注册；测试 reset_gate_checks 后可恢复）。

    重复注册由 gate.register_gate_check 的 ValueError 拦截，此处捕获保证幂等。
    """
    try:
        register_gate_check("consumption", check_consumption)
    except ValueError:
        pass  # 已注册（幂等）


ensure_registered()
