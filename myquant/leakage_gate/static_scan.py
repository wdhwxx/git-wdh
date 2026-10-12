# 文件名：myquant/leakage_gate/static_scan.py
"""
leakage_gate/static_scan.py - 静态 AST 扫描器（阶段 1-B，M1B-01/02/03）
=====================================================================
在写代码阶段拦截泄漏模式（v3.0 §6.5 / §10 规则库），并输出**列引用清单**
供 consumption.py 消费（R11 声明侧输入的 AST 实测补充，§10.8 / G-02）。

特性（v3.0 §6.5-1 验收）：
  - 一次遍历产出：规则命中（RuleHit）+ 列引用清单（referenced_columns）
  - 内容哈希缓存：文件内容未变时不再全量重扫（scan_file 幂等）
  - 只扫变更文件：scan_paths 可只传变更文件；入口脚本 / pre-commit / CI 按需传路径
  - 分级：阻断（blocking）/ 警告（warning）；白名单可追溯（每条命中附"为何合法"）

规则库（v3.0 §10，命中数 2026-10-10 实测）：
  - R01a  特征侧负向移位 .shift(-n)（非标签/前瞻收益上下文）→ 阻断（0 命中）
  - R01b  标签/前瞻收益侧 .shift(-n) → 放行 + 登记（6 命中，§10.3 全部正当）
  - R02   rolling(window, center=True) → 阻断（0 命中，后置）
  - R03   跨时间全样本拟合 min/max/mean/std/quantile/winsorize → **警告档**
         （G-04 单独里程碑检查点：先警告 + 人工复核，见 docs/…/R03里程碑检查点.md）
  - R04   train_test_split / KFold(shuffle=True) → 阻断（0 命中，P2）
  - R05   StandardScaler().fit_transform(全量) → 阻断（0 命中，P2）
  - R07   横截面排名/分位输入未按决策日切齐 → 警告（横截面校验器负责运行时判定）
  - R09   财务 PIT join 用 report_date 而非 eff_disclosure_date → 阻断（4→0 命中，
         M1A-01/02 修复后收敛；白名单 financial_service/time_semantics/derived_metrics）
  - R10   数据导出路径派生列未登记于 data_time_semantics.yaml → 阻断（0 命中）
  - R11   引用台账 severity=阻断 数据列 → 阻断（执行点在 consumption.py，R11 归 L0.5；
         本模块输出列引用清单供其消费，见 referenced_columns_for_strategy）

防未来函数：本模块只分析"已写出的源码"，不预测未来；缓存以文件内容哈希为准。
"""

from __future__ import annotations

import ast
import hashlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from myquant.leakage_gate.contract import _get_entry, _load_ledger, _load_registry
from myquant.leakage_gate.gate import Violation, register_gate_check
from myquant.leakage_gate.signal_words import is_full_sample_receiver

logger = logging.getLogger("myquant.leakage_gate.static_scan")

# 策略源码目录（只读文件做 AST，非 import；分层 L0 合规）
_STRATEGIES_DIR: Path = Path(__file__).resolve().parents[2] / "myquant" / "strategies"

# 台账阻断档 severity（v3.0 §6.1：severity=阻断）
_BLOCKED_SEVERITY: str = "阻断"

# R09 白名单（正确 PIT 实现，可合法出现 report_date 字段/注释，不含"按报告期做时点匹配"签名）
_R09_CORRECT_MODULES: Tuple[str, ...] = (
    "financial_service.py",
    "time_semantics.py",
    "derived_metrics.py",
    "data_versioner.py",
    "schema.py",
    "queries.py",
    "fetcher.py",
)

# R10 元列白名单：时点语义/索引元数据列，合法不出现在数据列台账（但出现即非业务派生列）
_R10_META_COLUMNS: Tuple[str, ...] = (
    "available_time",
    "eff_disclosure_date",
    "因子日",
    "timestamp",
    "date",
)

# R03 全样本拟合白名单函数（截面/滚动对照实现，合法，不警告）
_R03_CROSS_SECTION_FUNCS: Tuple[str, ...] = (
    "_zscore_cross_sectional",
    "_zscore_ts_rolling",
    "preprocess_panel",
    "neutralize",
    "winsorize_mad",
    "winsorize_quantile",
    "winsorize_adaptive",
    "residual_diagnostics",
)

# R03 全样本接收者命名意图：统一判定在 leakage_gate.signal_words（S-6，R03/R07 共用）


# ═══════════════════════════════════════════════
#  数据模型
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class RuleHit:
    """单条规则命中。

    Attributes:
        rule_id: 规则 ID（R01a/R01b/R02/R03/R04/R05/R07/R09/R10/R11）。
        severity: 档位（阻断/警告）。
        line: 命中源码行号（1 基；0 表示未知）。
        message: 命中说明（阻断规则含反泄漏口径；白名单命中含"为何合法"）。
        file: 源文件名（未提供时为空串）。
    """

    rule_id: str
    severity: str
    line: int
    message: str
    file: str = ""


@dataclass(frozen=True)
class ScanResult:
    """单文件扫描结果。

    Attributes:
        file: 源文件相对路径/文件名。
        content_hash: 内容哈希（sha256），供缓存命中判定。
        hits: 全部规则命中（含阻断与警告）。
        referenced_columns: 列引用清单（供 consumption.py 消费，R11）。
        r01b_registered: R01b 放行登记（标签/前瞻收益侧负向移位，§10.3）。
    """

    file: str
    content_hash: str
    hits: List[RuleHit]
    referenced_columns: Set[str]
    r01b_registered: List[RuleHit] = field(default_factory=list)

    @property
    def blocking_hits(self) -> List[RuleHit]:
        """仅阻断档命中（出口强制/CI 失败判定用）。"""
        return [h for h in self.hits if h.severity == _BLOCKED_SEVERITY]


# ═══════════════════════════════════════════════
#  AST 列引用提取（canonical，供 consumption.py 消费）
# ═══════════════════════════════════════════════

def extract_referenced_columns(source: str, ledger_keys: Set[str]) -> Set[str]:
    """从源码提取引用列（纯函数，AST 扫描）。

    覆盖（v3.0 §6.1.3 引用列取法）：
      - df["列名"]（ast.Subscript 内字符串常量）
      - df.列名（ast.Attribute.attr）
      - columns=[...] / 任意字符串常量比对台账键名
      - 非台账键名的字符串常量被过滤（不误报注释/日志等）

    Args:
        source: 源码文本。
        ledger_keys: 台账全部列名集合（候选过滤）。

    Returns:
        Set[str]: 命中的引用列集合。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        logger.error(f"源码解析失败: {e}")
        return set()
    found: Set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in ledger_keys
        ):
            found.add(node.value)
        elif isinstance(node, ast.Attribute) and node.attr in ledger_keys:
            found.add(node.attr)
    return found


# ═══════════════════════════════════════════════
#  AST 辅助（父节点映射 / 函数上下文）
# ═══════════════════════════════════════════════

def _build_parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    """构建 AST 父节点映射（node -> parent）。"""
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_function(node: ast.AST, parents: Dict[ast.AST, ast.AST]) -> Optional[ast.FunctionDef]:
    """向上查找最近的函数定义节点。"""
    cur: Optional[ast.AST] = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


def _node_refers_to(node: ast.AST, token: str) -> bool:
    """判断 AST 子树是否引用指定 token（字符串常量 / 属性名 / 变量名）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == token:
        return True
    if isinstance(node, ast.Attribute) and node.attr == token:
        return True
    if isinstance(node, ast.Name) and node.id == token:
        return True
    for child in ast.iter_child_nodes(node):
        if _node_refers_to(child, token):
            return True
    return False


def _func_name(node: Optional[ast.FunctionDef]) -> str:
    """取函数名（无则空串）。"""
    return node.name if node is not None else ""


# 标签/前瞻收益上下文（R01b 放行语义，§10.3）
_LABEL_CONTEXT_TOKENS: Tuple[str, ...] = (
    "label", "forward_return", "generate_label", "前瞻收益", "目标", "return_"
)
# 特征侧上下文（R01a 阻断语义）
_FEATURE_CONTEXT_TOKENS: Tuple[str, ...] = (
    "feature", "compute", "indicator", "factor", "特征", "计算"
)


def _is_label_context(func_name: str) -> bool:
    """函数名是否属于标签/前瞻收益生成上下文（R01b 放行）。"""
    low = func_name.lower()
    return any(t in low for t in _LABEL_CONTEXT_TOKENS)


def _is_feature_context(func_name: str) -> bool:
    """函数名是否属于特征侧计算上下文（R01a 阻断）。"""
    low = func_name.lower()
    return any(t in low for t in _FEATURE_CONTEXT_TOKENS)


# ═══════════════════════════════════════════════
#  规则检测器（纯函数）
# ═══════════════════════════════════════════════

def _detect_shift(
    tree: ast.AST,
    parents: Dict[ast.AST, ast.AST],
    hits: List[RuleHit],
    r01b_registered: List[RuleHit],
) -> None:
    """R01a/R01b：.shift(-n) 按函数上下文判定（§10.3）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "shift"):
            continue
        # 判断是否负向移位（任一实参为负常量）
        negative = False
        for arg in node.args:
            if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                negative = True
            elif (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, (int, float))
                and arg.value < 0
            ):
                negative = True
        if not negative:
            continue
        line = getattr(node, "lineno", 0)
        fname = _func_name(_enclosing_function(node, parents))
        if _is_label_context(fname):
            r01b_registered.append(
                RuleHit(
                    rule_id="R01b", severity="放行", line=line,
                    message=f"标签/前瞻收益侧负向移位（.shift(-n)）: 函数 {fname!r}；"
                            f"仅用于训练目标/评估口径，非特征输入，放行并登记（§10.3）",
                )
            )
        else:
            hits.append(
                RuleHit(
                    rule_id="R01a", severity=_BLOCKED_SEVERITY, line=line,
                    message=f"特征侧负向移位 .shift(-n)（RL-01）：函数 {fname!r} 把含未来信息"
                            f"的量当作决策时点可用输入；标签/前瞻收益上下文应显式命名（R01b 放行）",
                )
            )


def _detect_rolling_center(tree: ast.AST, hits: List[RuleHit]) -> None:
    """R02：rolling(window, center=True) → 阻断（0 命中，后置）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "rolling"):
            continue
        for kw in node.keywords:
            if kw.arg == "center" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                hits.append(
                    RuleHit(
                        rule_id="R02", severity=_BLOCKED_SEVERITY,
                        line=getattr(node, "lineno", 0),
                        message="居中滚动 rolling(center=True)（RL-06）：当前 bar 引用前后 bar，泄漏",
                    )
                )


def _detect_random_split(tree: ast.AST, hits: List[RuleHit]) -> None:
    """R04：train_test_split / KFold(shuffle=True) → 阻断（0 命中，P2）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_split = isinstance(func, ast.Name) and func.id == "train_test_split"
        is_kfold = isinstance(func, ast.Name) and func.id == "KFold"
        if is_split:
            hits.append(
                RuleHit(
                    rule_id="R04", severity=_BLOCKED_SEVERITY,
                    line=getattr(node, "lineno", 0),
                    message="train_test_split（RL-04）：随机切分会跨时间泄漏，须用时间序列切分",
                )
            )
        elif is_kfold:
            for kw in node.keywords:
                if kw.arg == "shuffle" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    hits.append(
                        RuleHit(
                            rule_id="R04", severity=_BLOCKED_SEVERITY,
                            line=getattr(node, "lineno", 0),
                            message="KFold(shuffle=True)（RL-04）：随机切分会跨时间泄漏，须顺序切分",
                        )
                    )


def _detect_full_standardize(tree: ast.AST, hits: List[RuleHit]) -> None:
    """R05：StandardScaler().fit_transform(全量) → 阻断（0 命中，P2）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "fit_transform"):
            continue
        # 链式：StandardScaler().fit_transform(...)
        if isinstance(func.value, ast.Call) and _node_refers_to(func.value, "StandardScaler"):
            hits.append(
                RuleHit(
                    rule_id="R05", severity=_BLOCKED_SEVERITY,
                    line=getattr(node, "lineno", 0),
                    message="StandardScaler().fit_transform（RL-05）：全样本标准化泄漏，须嵌套标准化",
                )
            )


def _detect_full_sample_fit(
    tree: ast.AST,
    parents: Dict[ast.AST, ast.AST],
    hits: List[RuleHit],
) -> None:
    """R03：跨时间全样本拟合 → 警告档（G-04，先警告 + 人工复核）。

    仅对接收者命名具全样本意图（panel/full/all_/entire/whole）的裸聚合告警，
    避免误报截面 z-score（_zscore_cross_sectional / _zscore_ts_rolling / preprocess_panel）
    与滚动窗口（rolling().mean()）等合法对照实现。
    """
    aggregates = ("min", "max", "mean", "std", "quantile", "winsorize")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in aggregates):
            continue
        # 滚动窗口调用不警告（合法对照）
        if isinstance(func.value, ast.Call):
            inner = func.value.func
            if isinstance(inner, ast.Attribute) and inner.attr == "rolling":
                continue
        fname = _func_name(_enclosing_function(node, parents))
        if fname in _R03_CROSS_SECTION_FUNCS:
            continue  # 截面/滚动对照实现，白名单
        receiver = ""
        if isinstance(func.value, ast.Name):
            receiver = func.value.id
        elif isinstance(func.value, ast.Attribute):
            receiver = func.value.attr
        if is_full_sample_receiver(receiver):
            hits.append(
                RuleHit(
                    rule_id="R03", severity="警告", line=getattr(node, "lineno", 0),
                    message=f"跨时间全样本拟合候选: {receiver}.{func.attr}()（RL-12）——"
                            f"若作用于时间维全样本将泄漏；若为当日截面则合法。G-04 先警告 + 人工复核，"
                            f"见 docs/未来函数与数据泄漏门禁/R03里程碑检查点.md",
                )
            )


def _detect_cross_sectional_misuse(tree: ast.AST, hits: List[RuleHit]) -> None:
    """R07：横截面排名/分位输入未按决策日切齐 → 警告（运行时校验器负责判定）。"""
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
            hits.append(
                RuleHit(
                    rule_id="R07", severity="警告", line=getattr(node, "lineno", 0),
                    message=f"横截面排名/分位输入候选: {receiver}.{func.attr}()（RL-07）——"
                            f"须按决策日切齐当日截面，运行时由 cross_sectional 校验器判定",
                )
            )


def _detect_r09(
    tree: ast.AST,
    filename: str,
    hits: List[RuleHit],
) -> None:
    """R09：财务 PIT join 用 report_date 而非 eff_disclosure_date（阻断，RL-02）。

    精确签名（避免误报合法字段/注释）：
      信号A：字符串含 "ORDER BY report_date"（按报告期排序取"最新一期"）
      信号B：存在比较（<=/>=/</>），一侧引用 trade_date，另一侧引用 report_date/fd/fin_dates
    两信号同文件出现 → 命中（旧缺陷 数据导出.py:131/141 与 manager.py:1287/1293 的签名）。
    """
    base = Path(filename).name
    if base in _R09_CORRECT_MODULES:
        return  # 正确 PIT 实现白名单（§10.6 ✅/⚠️ 两类）
    has_signal_a = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "ORDER BY report_date" in node.value
        ):
            has_signal_a = True
            break
    if not has_signal_a:
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        op = node.ops[0]
        if not isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            continue
        left, right = node.left, node.comparators[0]
        left_td = _node_refers_to(left, "trade_date")
        right_td = _node_refers_to(right, "trade_date")
        # report_date 派生/别名（含 fd/fin_dates/fin_date/report_dt/f_date）
        _fin_tokens = ("report_date", "fd", "fin_dates", "fin_date", "fin_dt", "f_date", "report_dt")
        left_fin = any(_node_refers_to(left, t) for t in _fin_tokens)
        right_fin = any(_node_refers_to(right, t) for t in _fin_tokens)
        # fd（report_date 派生）与 trade_date 的时点比较，且未走 eff_disclosure_date
        if (left_td and right_fin) or (right_td and left_fin):
            hits.append(
                RuleHit(
                    rule_id="R09", severity=_BLOCKED_SEVERITY,
                    line=getattr(node, "lineno", 0),
                    message="财务 PIT join 用 report_date 做时点匹配（RL-02）：应按 eff_disclosure_date"
                            f"（ann_date 优先 → report_deadline 推定 → report_date 兜底）对齐，"
                            f"见 myquant/data_access/time_semantics.py（M1A-01/02 收敛）",
                )
            )
            return  # 命中一次即可


def _detect_r10(
    tree: ast.AST,
    ledger_keys: Set[str],
    hits: List[RuleHit],
) -> None:
    """R10：数据导出路径派生列未登记于 data_time_semantics.yaml（阻断，RL-11）。

    签名：`.insert(col_pos, "列名", ...)` 写入未登记于台账的列（新增派生列未登记）。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "insert"):
            continue
        # insert(col_index, name, values) → 第 2 个实参为列名字符串
        if len(node.args) >= 2:
            name_node = node.args[1]
            if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
                col = name_node.value
                if col not in ledger_keys and col not in _R10_META_COLUMNS:
                    hits.append(
                        RuleHit(
                            rule_id="R10", severity=_BLOCKED_SEVERITY,
                            line=getattr(node, "lineno", 0),
                            message=f"数据导出路径派生列 {col!r} 未登记于 config/data_time_semantics.yaml"
                                    f"（RL-11/R10）：新增派生列必须先登记日基与 severity，"
                                    f"否则时点语义不可判定",
                        )
                    )


# ═══════════════════════════════════════════════
#  主分析入口（纯函数）
# ═══════════════════════════════════════════════

def analyze_source(
    source: str,
    ledger_keys: Set[str],
    blocked: Set[str],
    filename: str = "",
) -> ScanResult:
    """对单份源码执行全部规则扫描 + 列引用提取（一次遍历）。

    Args:
        source: 源码文本。
        ledger_keys: 台账全部列名集合。
        blocked: 台账 severity=阻断 的列集合（R11 判定输入）。
        filename: 源文件名（R09/R10 白名单判定用）。

    Returns:
        ScanResult: 命中（阻断/警告）+ 列引用清单 + R01b 放行登记。
    """
    content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        logger.error(f"源码解析失败 {filename or '<string>'}: {e}")
        return ScanResult(file=filename, content_hash=content_hash, hits=[], referenced_columns=set())

    parents = _build_parent_map(tree)
    hits: List[RuleHit] = []
    r01b_registered: List[RuleHit] = []

    _detect_shift(tree, parents, hits, r01b_registered)
    _detect_rolling_center(tree, hits)
    _detect_random_split(tree, hits)
    _detect_full_standardize(tree, hits)
    _detect_full_sample_fit(tree, parents, hits)
    _detect_cross_sectional_misuse(tree, hits)
    _detect_r09(tree, filename, hits)
    _detect_r10(tree, ledger_keys, hits)

    referenced = extract_referenced_columns(source, ledger_keys)

    return ScanResult(
        file=filename,
        content_hash=content_hash,
        hits=hits,
        referenced_columns=referenced,
        r01b_registered=r01b_registered,
    )


# ═══════════════════════════════════════════════
#  内容哈希缓存 + 只扫变更文件
# ═══════════════════════════════════════════════

# 文件内容哈希缓存：path -> (content_hash, ScanResult)
_cache: Dict[str, Tuple[str, ScanResult]] = {}


def _file_text(path: Path) -> Optional[str]:
    """读取源码文本；缺失/读取失败返回 None。

    用 utf-8-sig 读取以剥离 BOM（\ufeff），避免带 BOM 文件（如 manager.py）在
    ast.parse 时报 "invalid non-printable character U+FEFF" 解析失败。
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        logger.error(f"源码读取失败 {path}: {e}")
        return None


def scan_file(
    path: Path,
    ledger_keys: Set[str],
    blocked: Set[str],
    use_cache: bool = True,
) -> Optional[ScanResult]:
    """扫描单文件（带内容哈希缓存）。

    Args:
        path: 源码文件路径。
        ledger_keys: 台账全部列名集合。
        blocked: 台账 severity=阻断 列集合。
        use_cache: 是否启用内容哈希缓存（缓存命中不再重扫）。

    Returns:
        Optional[ScanResult]: 扫描结果；文件不可读返回 None。
    """
    text = _file_text(path)
    if text is None:
        return None
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    key = str(path)
    if use_cache and key in _cache and _cache[key][0] == h:
        return _cache[key][1]
    result = analyze_source(text, ledger_keys, blocked, filename=str(path))
    if use_cache:
        _cache[key] = (h, result)
    return result


def scan_paths(
    paths: List[Path],
    ledger_keys: Set[str],
    blocked: Set[str],
    use_cache: bool = True,
) -> List[ScanResult]:
    """批量扫描（只扫传入的变更文件）。

    Args:
        paths: 源码文件路径列表。
        ledger_keys: 台账全部列名集合。
        blocked: 台账 severity=阻断 列集合。
        use_cache: 是否启用内容哈希缓存。

    Returns:
        List[ScanResult]: 可读文件的扫描结果列表。
    """
    results: List[ScanResult] = []
    for p in paths:
        if not p.suffix == ".py":
            continue
        r = scan_file(p, ledger_keys, blocked, use_cache=use_cache)
        if r is not None:
            results.append(r)
    return results


def clear_cache() -> None:
    """清空内容哈希缓存（测试隔离用）。"""
    _cache.clear()


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
        text = _file_text(py)
        if text is None:
            continue
        if _file_defines_name(text, strategy_id):
            return text
    return None


def referenced_columns_for_strategy(strategy_id: str) -> Set[str]:
    """供 consumption.py 消费：策略 AST 实测引用列（R11 声明侧补充，§10.8）。

    优先用内容哈希缓存（若该策略源码本次已扫描）；否则现场扫描一次。
    与 consumption.extract_columns_from_source 判定一致（单实现，本模块为 canonical）。

    Args:
        strategy_id: 策略契约主键（name）。

    Returns:
        Set[str]: 引用列集合。
    """
    ledger = _load_ledger()
    if ledger is None:
        return set()
    columns = ledger.get("columns")
    if not isinstance(columns, dict):
        return set()
    ledger_keys = set(columns)
    source = _strategy_source(strategy_id)
    if source is None:
        return set()
    # 命中缓存：按内容哈希复用扫描结果
    h = hashlib.sha256(source.encode("utf-8")).hexdigest()
    key = f"__strategy__:{strategy_id}"
    if key in _cache and _cache[key][0] == h:
        return set(_cache[key][1].referenced_columns)
    result = analyze_source(source, ledger_keys, _blocked_columns(ledger), filename=f"strategy:{strategy_id}")
    _cache[key] = (h, result)
    return set(result.referenced_columns)


def _blocked_columns(ledger: Dict[str, Any]) -> Set[str]:
    """提取台账中 severity=阻断 的列集合。"""
    columns = ledger.get("columns")
    if not isinstance(columns, dict):
        return set()
    return {
        c for c, m in columns.items()
        if isinstance(m, dict) and str(m.get("severity", "")).strip() == _BLOCKED_SEVERITY
    }


# ═══════════════════════════════════════════════
#  门禁检查入口（挂入门禁中心）
# ═══════════════════════════════════════════════

def check_static_scan(strategy_id: str, decision_time: str) -> List[Violation]:
    """静态扫描门禁检查（register_gate_check("static_scan", ...)）。

    扫描指定策略源码；仅阻断档命中（R01a/R02/R04/R05/R09/R10）报违规；
    R03/R07 警告档与 R01b 放行登记不阻断出口（G-04 / §10.3）。

    Args:
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。

    Returns:
        List[Violation]: 静态扫描阻断违规；无阻断为空列表。
    """
    ledger = _load_ledger()
    if ledger is None:
        return [
            Violation(
                gate="static_scan", rule_id="R10", severity=_BLOCKED_SEVERITY,
                message="列级台账缺失或不可读；静态扫描无法判定派生列登记（须先完成 M0P-02）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        ]
    columns = ledger.get("columns")
    if not isinstance(columns, dict) or not columns:
        return [
            Violation(
                gate="static_scan", rule_id="R10", severity=_BLOCKED_SEVERITY,
                message="列级台账 columns 为空（RL-11：数据列时点未登记）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        ]
    ledger_keys = set(columns)
    blocked = _blocked_columns(ledger)
    source = _strategy_source(strategy_id)
    if source is None:
        return []  # 未定位策略源码：不阻断（源码定位非强制，consumption 同口径）
    result = analyze_source(source, ledger_keys, blocked, filename=f"strategy:{strategy_id}")
    violations: List[Violation] = []
    for h in result.blocking_hits:
        violations.append(
            Violation(
                gate="static_scan", rule_id=h.rule_id, severity=h.severity,
                message=f"{h.message}（line {h.line}）",
                strategy_id=strategy_id, decision_time=decision_time,
            )
        )
    return violations


def ensure_registered() -> None:
    """幂等注册静态扫描检查到门禁中心（导入即注册；测试 reset_gate_checks 后可恢复）。"""
    try:
        register_gate_check("static_scan", check_static_scan)
    except ValueError:
        pass  # 已注册（幂等）


# ═══════════════════════════════════════════════
#  CLI 入口（scripts/静态扫描.py 薄入口委托）
# ═══════════════════════════════════════════════

def _default_scan_targets() -> List[Path]:
    """默认扫描范围：myquant/**/*.py + scripts/**/*.py。"""
    root = Path(__file__).resolve().parents[2]
    targets: List[Path] = []
    for base in ("myquant", "scripts"):
        d = root / base
        if d.is_dir():
            targets.extend(sorted(p for p in d.rglob("*.py") if "__pycache__" not in str(p)))
    return targets


def main_cli(argv: Optional[List[str]] = None) -> int:
    """静态扫描 CLI（薄入口委托实现；业务逻辑在本模块）。

    Args:
        argv: 命令行参数；None 时取 sys.argv[1:]。

    Returns:
        int: 0=无阻断命中（可含警告）；1=存在阻断命中或参数错误。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    all_flag = "--all" in argv
    json_flag = "--json" in argv
    paths_arg = [a for a in argv if not a.startswith("--")]
    ledger = _load_ledger()
    if ledger is None:
        print("✗ 列级台账 config/data_time_semantics.yaml 缺失或不可读；静态扫描无法判定（fail-closed）")
        return 1
    columns = ledger.get("columns")
    if not isinstance(columns, dict) or not columns:
        print("✗ 列级台账 columns 为空；静态扫描无法判定（RL-11）")
        return 1
    ledger_keys = set(columns)
    blocked = _blocked_columns(ledger)

    targets: List[Path] = []
    if paths_arg:
        targets = [Path(p) for p in paths_arg]
    elif all_flag:
        targets = _default_scan_targets()
    else:
        targets = _default_scan_targets()  # 默认全量（CI/本地）；pre-commit 传具体变更文件

    results = scan_paths(targets, ledger_keys, blocked, use_cache=True)
    all_hits = [h for r in results for h in r.hits]
    blocking = [h for h in all_hits if h.severity == _BLOCKED_SEVERITY]
    r01b = [h for r in results for h in r.r01b_registered]

    if json_flag:
        import json
        print(json.dumps({
            "files": len(results),
            "blocking_hits": [
                {"rule": h.rule_id, "file": h.file, "line": h.line, "message": h.message}
                for h in blocking
            ],
            "warning_hits": [
                {"rule": h.rule_id, "file": h.file, "line": h.line, "message": h.message}
                for h in all_hits if h.severity == "警告"
            ],
            "r01b_registered": len(r01b),
        }, ensure_ascii=False, indent=2))
        return 1 if blocking else 0

    print(f"静态扫描 {len(results)} 文件")
    if r01b:
        print(f"  R01b 放行登记 {len(r01b)} 处（标签/前瞻收益侧负向移位，§10.3）：")
        for h in r01b:
            print(f"    [{h.rule_id}] line {h.line} {h.message}")
    if all_hits:
        for h in all_hits:
            flag = "阻断" if h.severity == _BLOCKED_SEVERITY else "警告"
            print(f"  [{flag}][{h.rule_id}] {h.file}:{h.line} {h.message}")
    else:
        print("  无规则命中")
    if blocking:
        print(f"✗ 静态扫描发现 {len(blocking)} 项阻断命中，新代码不得进入回测/实盘链路")
        return 1
    print("✓ 静态扫描通过（无阻断命中）")
    return 0


ensure_registered()
