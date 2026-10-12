# 文件名：myquant/leakage_gate/cross_sectional.py
"""
leakage_gate/cross_sectional.py - 横截面 as-of 校验器（阶段 1-B，M1B-04）
=========================================================================
对候选池 / 三因子筛选断言"当日截面只用当日数据"（v3.0 §6.5-2 / RL-07 / R07）：
任一横截面统计（排名/分位/均值/标准差）的输入面板不得包含决策日之后的数据。

分层：leakage_gate=L0。本模块只读策略源码**文件**（AST 解析，非 import），
不 import strategies 包（L0→L3 反向跨层会被 R-02 拦截）；运行时面板校验为纯函数，
由调用方（回测/因子计算）在得到当日截面时传入校验。

职责（v3.0 §6.5-2 / §10 规则表 R07）：
  - validate_asof                 纯函数：面板时间列逐行不得晚于决策时点
  - validate_cross_sectional_panel 纯函数：当日截面 = 只含该日数据，无未来日期
  - check_cross_sectional         门禁检查（register_gate_check("cross_sectional", ...)）：
    扫描策略源码，R07 警告档标记横截面排名/分位输入未按决策日切齐的候选点

R03（跨时间全样本拟合）的截面/时序区分判定在 static_scan（R03 警告档，G-04），
本模块只负责"当日截面"的 as-of 运行时校验与 R07 静态告警，不重复实现。

防未来函数：本模块只校验"已提供"的面板数据时间是否 ≤ 决策时点，不预测未来。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, List, Optional

from myquant.leakage_gate.contract import _load_ledger
from myquant.leakage_gate.gate import Violation, register_gate_check
from myquant.leakage_gate.signal_words import is_full_sample_receiver

logger = logging.getLogger("myquant.leakage_gate.cross_sectional")

# 策略源码目录（只读文件做 AST，非 import；分层合规）
_STRATEGIES_DIR: Path = Path(__file__).resolve().parents[2] / "myquant" / "strategies"


# ═══════════════════════════════════════════════
#  运行时 as-of 校验（纯函数）
# ═══════════════════════════════════════════════

def validate_asof(
    data: Any,
    decision_time: str,
    time_col: str = "日期",
) -> List[str]:
    """校验面板时间列逐行不得晚于决策时点（纯函数）。

    Args:
        data: 面板（pandas DataFrame/Series），需含时间列或 DatetimeIndex。
        decision_time: 决策时点（YYYY-MM-DD）。
        time_col: 时间列名；若面板索引为 DatetimeIndex 且该列不存在则用索引。

    Returns:
        List[str]: 未来日期行描述（无未来数据为空列表）。
    """
    import pandas as pd

    if data is None:
        return ["面板为空，无法校验横截面 as-of"]
    if not isinstance(data, (pd.DataFrame, pd.Series)):
        return [f"面板类型非法（须 DataFrame/Series），got: {type(data).__name__}"]
    try:
        decision_ts = pd.Timestamp(decision_time)
    except Exception as e:  # noqa: BLE001 - 决策时点解析失败按无法判定处理
        return [f"决策时点 {decision_time!r} 解析失败: {e}"]

    if isinstance(data, pd.Series):
        if time_col in data.index.names or data.index.name == time_col:
            times = data.index
        else:
            return [f"Series 缺少时间列 {time_col!r}"]
    else:
        if time_col in data.columns:
            times = pd.to_datetime(data[time_col], errors="coerce")
        elif isinstance(data.index, pd.DatetimeIndex):
            times = data.index
        else:
            return [f"DataFrame 缺少时间列 {time_col!r} 且索引非 DatetimeIndex"]

    bad: List[str] = []
    try:
        mask = times > decision_ts
        future = times[mask]
        for ts in future.head(20):
            bad.append(f"决策时点 {decision_time} 之后出现数据行: {ts}")
        if len(future) > 20:
            bad.append(f"…共 {len(future)} 行晚于决策时点（截断显示前 20 行）")
    except Exception as e:  # noqa: BLE001 - 时间比较异常按告警处理
        bad.append(f"横截面 as-of 时间比较失败: {e}")
    return bad


def validate_cross_sectional_panel(
    panel: Any,
    decision_time: str,
    date_col: Optional[str] = None,
    symbol_col: str = "symbol",
) -> List[str]:
    """校验横截面面板每个日期组只含当日数据（纯函数，RL-07）。

    适用形态：
      - 面板含 date 列（每行一个 (date, symbol) 观测）→ 按 date 分组，组内不得混入未来日
      - 面板 index 为 MultiIndex(date, symbol) → 逐日期切片校验

    Args:
        panel: 横截面面板（DataFrame）。
        decision_time: 决策时点（YYYY-MM-DD）。
        date_col: 日期列名；缺省时若 index 为 DatetimeIndex 用之，否则查 date/trade_date。
        symbol_col: 标的列名（仅用于报错定位，可选）。

    Returns:
        List[str]: 违规描述（无违规为空列表）。
    """
    import pandas as pd

    if panel is None:
        return ["横截面面板为空，无法校验"]
    if not isinstance(panel, pd.DataFrame):
        return [f"横截面面板须为 DataFrame，got: {type(panel).__name__}"]
    try:
        decision_ts = pd.Timestamp(decision_time)
    except Exception as e:  # noqa: BLE001
        return [f"决策时点 {decision_time!r} 解析失败: {e}"]

    # 定位日期列
    if date_col is None:
        for cand in ("date", "trade_date", "日期", "timestamp"):
            if cand in panel.columns:
                date_col = cand
                break
        if date_col is None and isinstance(panel.index, pd.MultiIndex):
            date_col = panel.index.names[0] or "level_0"
    if date_col is None and isinstance(panel.index, pd.DatetimeIndex):
        date_col = "index"
    if date_col is None:
        return [f"横截面面板缺少日期列（尝试 date/trade_date/日期/timestamp 均未找到）"]

    if date_col == "index":
        dates = panel.index
    elif date_col.startswith("level_"):
        dates = panel.index.get_level_values(int(date_col.split("_")[1]))
    else:
        dates = pd.to_datetime(panel[date_col], errors="coerce")

    bad: List[str] = []
    try:
        future = dates > decision_ts
        n_future = int(future.sum())
        if n_future:
            bad.append(f"横截面面板含 {n_future} 行晚于决策时点 {decision_time}（未来数据）")
        # 逐日切片内部校验：date <= decision_time 的组内不得混入该日之后的观测
        if date_col not in ("index",) and not date_col.startswith("level_"):
            grp = pd.to_datetime(panel[date_col], errors="coerce")
            day_counts = grp.value_counts()
            for dt, _ in day_counts.items():
                if dt is not pd.NaT and dt > decision_ts:
                    bad.append(f"截面日 {dt.date()} 晚于决策时点（RL-07：当日截面须只用当日数据）")
    except Exception as e:  # noqa: BLE001
        bad.append(f"横截面 as-of 面板校验失败: {e}")
    return bad


# ═══════════════════════════════════════════════
#  策略源码定位（文件级，非 import）
# ═══════════════════════════════════════════════

def _file_defines_name(source: str, strategy_id: str) -> bool:
    """判断源码文件是否定义了 name == strategy_id 的策略类。"""
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
    """读取定义指定策略的源码文本（AST 定位，非 import）。"""
    if not _STRATEGIES_DIR.is_dir():
        logger.error(f"策略目录不存在: {_STRATEGIES_DIR}")
        return None
    for py in sorted(_STRATEGIES_DIR.glob("*.py")):
        if py.name in ("__init__.py", "base.py", "registry.py", "param_tuner.py"):
            continue
        try:
            text = py.read_text(encoding="utf-8-sig")
        except OSError as e:
            logger.error(f"策略源码读取失败 {py}: {e}")
            continue
        if _file_defines_name(text, strategy_id):
            return text
    return None


# 横截面排名/分位接收者命名意图：统一判定在 leakage_gate.signal_words（S-6，R07/R03 共用）


# ═══════════════════════════════════════════════
#  门禁检查入口
# ═══════════════════════════════════════════════

def check_cross_sectional(strategy_id: str, decision_time: str) -> List[Violation]:
    """横截面 as-of 门禁检查（register_gate_check("cross_sectional", ...)）。

    静态扫描策略源码：对具全样本接收者命名（panel/full/all_/entire/whole）的
    rank/quantile 调用给 R07 警告（RL-07）。运行时面板 as-of 由调用方用
    validate_asof / validate_cross_sectional_panel 校验，本门禁不持有运行数据。

    Args:
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。

    Returns:
        List[Violation]: R07 警告违规（severity=警告）。按 S-4（P1-07⑥a），出口断言
        assert_all_gates_pass 只阻断 severity=阻断 的违规，故本门禁的 R07 警告仅提示、
        不阻断引擎创建（对齐 static_scan 的"警告档不阻断"）；无警告为空列表。
    """
    source = _strategy_source(strategy_id)
    if source is None:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        logger.error(f"策略源码解析失败: {e}")
        return []
    violations: List[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("rank", "quantile")):
            continue
        receiver = ""
        if isinstance(func.value, ast.Name):
            receiver = func.value.id
        elif isinstance(func.value, ast.Attribute):
            receiver = func.value.attr
        if is_full_sample_receiver(receiver):
            violations.append(
                Violation(
                    gate="cross_sectional", rule_id="R07", severity="警告",
                    message=f"横截面排名/分位输入候选 {receiver}.{func.attr}() 未按决策日切齐"
                            f"（RL-07，line {getattr(node, 'lineno', 0)}）；运行时须经 "
                            f"validate_asof/validate_cross_sectional_panel 校验当日截面只用当日数据",
                    strategy_id=strategy_id, decision_time=decision_time,
                )
            )
    return violations


def ensure_registered() -> None:
    """幂等注册横截面检查到门禁中心（导入即注册；测试 reset_gate_checks 后可恢复）。"""
    try:
        register_gate_check("cross_sectional", check_cross_sectional)
    except ValueError:
        pass  # 已注册（幂等）


ensure_registered()
