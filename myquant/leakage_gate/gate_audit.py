# -*- coding: utf-8 -*-
"""门禁参数变更审计（判据 A 对价 + 判据 B 蓝图登记）。

背景：门禁参数（tests/unit/test_architecture.py 与 myquant/leakage_gate/
coverage_gate.py 中的基线、floor、硬顶、注册表）与协议代码位于同一可写仓库
——"改门禁的人自己就是被约束对象"。本模块把对价五要素机器化，使
"抬基线必须付对价"从口头纪律变成可判定的 CI 判据。

判据 A（对价审计）：commit 范围（before..head）内，门禁参数常量若有值变更，
  该变更所在 commit 必须同时满足：
    ① 独立 commit：该 commit 变更文件不含 myquant/ 业务代码（仅允许门禁两文件
       + docs/ + tests/）；
    ② commit message 含工单号 #\\d+ 或 "回写" 关键词；
    ③ 同 commit 蓝图 docs/project_layout_gear2.md 有变更（或 message 指认回写）。
  缺任一 → 红（阻断）。

判据 B（蓝图登记）：commit 范围内新增的 .py / .md 文件必须出现在蓝图全文
  （目录树或回写记录）——拦截 _reorder_tmp.py 类误提交。缺 → 红。

使用方：scripts/门禁审计.py（薄入口）+ CI gate-audit job。
分层：L0 纯模块（subprocess 调 git，不 import myquant 业务层）。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# 门禁参数所在文件（相对项目根）
GATE_FILES: frozenset[str] = frozenset(
    {
        "tests/unit/test_architecture.py",
        "myquant/leakage_gate/coverage_gate.py",
    }
)

# 判据 B 关注的新增文件后缀
_AUDITED_SUFFIXES: tuple[str, ...] = (".py", ".md")

# 判据 A：参数/基线常量名（diff 的 + 行命中任一即视为参数变更）
_PARAM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"STRUCTURE_HARD_CAP",
        r"OVERSIZE_LIMIT",
        r"OVERSIZE_WARN_MARGIN",
        r"OVERSIZE_WHITELIST",
        r"OVERSIZE_LINE_BASELINE",
        r"STRUCTURE_BASELINE",
        r"PACKAGE_STRUCTURE_BASELINE",
        r"ENTRY_ALLOWLIST",
        r"COVERAGE_BASELINE",
        r"COVERED_LINES_BASELINE",
        r"TIER_FLOOR",
        r"FILE_FLOOR",
        r"NEW_FILE_FLOOR",
        r"FILE_STMT_CEILING",
        r"T2_FILES",
        r"SOURCE_ASSERT_ONLY_MAX",
        r"SKIP_BUDGET",
    )
)

# 注册表条目（如 FILE_STMT_CEILING 的 "live/brokers/qmt_broker.py": 1033）
_ENTRY_RE = re.compile(r'"[^"]+\.py":\s*[\d.]')
# 包基线字段（PACKAGE_STRUCTURE_BASELINE 的 "lines"/"symbols"/"fan_out" 值）
_BASELINE_FIELD_RE = re.compile(r'"(lines|symbols|fan_out)":\s*\d+')


def _is_param_line(plus_line: str) -> bool:
    """该 + 行是否属于门禁参数变更（常量名 / 注册条目 / 基线字段）。"""
    return any(pat.search(plus_line) for pat in _PARAM_PATTERNS) or bool(
        _ENTRY_RE.search(plus_line) or _BASELINE_FIELD_RE.search(plus_line)
    )

# 工单号 / 回写指认（commit message 对价证据）
_ISSUE_RE = re.compile(r"#\d+")
_WRITEBACK_RE = re.compile(r"回写")


@dataclass
class AuditReport:
    """门禁审计判定报告。

    Attributes:
        before: 审计范围起点 SHA。
        head: 审计范围终点 SHA。
        violations: 违反判据列表（空即通过）。
    """

    before: str
    head: str
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """判据是否全部通过。"""
        return not self.violations

    def summary(self) -> str:
        """多行可读摘要。"""
        lines = [
            f"门禁参数变更审计（判据A 对价 + 判据B 蓝图登记；范围 {self.before[:7]}..{self.head[:7]}）",
        ]
        if self.violations:
            lines.append(f"  ❌ 违反判据 {len(self.violations)} 项:")
            for v in self.violations:
                lines.append(f"      - {v}")
        else:
            lines.append("  ✓ 全部判据通过（无参数对价缺失、无未登记新增文件）")
        return "\n".join(lines)


def _git(
    args: Sequence[str],
    project_root: Path,
    *,
    check: bool = True,
) -> str:
    """在项目根执行 git 命令（L0：subprocess 直调，无 shell）。

    Args:
        args: git 子命令参数列表。
        project_root: 仓库根目录。
        check: True=失败抛异常（fail-closed）；False=失败返回空串。

    Returns:
        命令 stdout（去尾空白）。

    Raises:
        RuntimeError: check=True 且 git 调用失败（含 git 不可用）。
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - 环境性
        raise RuntimeError(f"git 调用失败（fail-closed）: {exc}") from exc
    if check and proc.returncode != 0:
        raise RuntimeError(f"git 调用失败（fail-closed）: {proc.stderr.strip()}")
    if not check and proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _changed_files(before: str, head: str, project_root: Path) -> list[str]:
    """before..head 变更文件相对路径列表。"""
    out = _git(["diff", "--name-only", "--no-renames", before, head], project_root)
    return [ln for ln in out.splitlines() if ln.strip()]


def _added_files(before: str, head: str, project_root: Path) -> list[str]:
    """before..head 新增文件（diff-filter=A）相对路径列表。"""
    out = _git(
        ["diff", "--name-only", "--no-renames", "--diff-filter=A", before, head],
        project_root,
    )
    return [ln for ln in out.splitlines() if ln.strip()]


def _commits_touching(
    before: str,
    head: str,
    path: str,
    project_root: Path,
) -> list[str]:
    """before..head 范围内改过指定路径的 commit SHA 列表（时间升序）。"""
    out = _git(["log", "--format=%H", f"{before}..{head}", "--", path], project_root)
    return [ln for ln in out.splitlines() if ln.strip()]


def _commit_message(sha: str, project_root: Path) -> str:
    """取 commit message（subject + body）。"""
    return _git(["log", "-1", "--format=%B", sha], project_root)


def _commit_files(sha: str, project_root: Path) -> list[str]:
    """该 commit 变更的文件相对路径列表。"""
    out = _git(["show", "--name-only", "--format=", "--no-renames", sha], project_root)
    return [ln for ln in out.splitlines() if ln.strip()]


def _commit_has_param_change(sha: str, project_root: Path) -> bool:
    """该 commit 对门禁文件的 diff 是否含参数常量的**实质变更**。

    判定：diff 的 + 行命中参数常量名，且该 + 行内容未在对应 - 行中"原样重现"
    （同行重写/纯注释/格式整理不触发；数值变化、注册表新增条目触发）。
    """
    out = _git(["show", sha, "--", *sorted(GATE_FILES)], project_root, check=False)
    minus: set[str] = set()
    plus: list[str] = []
    for line in out.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            minus.add(line[1:].strip())
        elif line.startswith("+"):
            plus.append(line[1:].strip())
    for plus_line in plus:
        if not _is_param_line(plus_line):
            continue
        if plus_line not in minus:  # 实质新增/改动（非原样重现）
            return True
    return False


def _blueprint_has_path(rel_path: str, blueprint: Path) -> bool:
    """蓝图全文是否出现该文件路径（目录树登记或回写记录均算）。"""
    if not blueprint.is_file():
        return False
    text = blueprint.read_text(encoding="utf-8", errors="replace")
    name = Path(rel_path).name
    return rel_path in text or name in text


def run_audit(
    before: str,
    head: str,
    project_root: Path,
    blueprint: Path | None = None,
) -> AuditReport:
    """执行门禁参数变更审计（判据 A + 判据 B）。

    Args:
        before: 审计范围起点 SHA（CI 传 ${{ github.event.before }}）。
        head: 审计范围终点 SHA（CI 传 ${{ github.sha }}）。
        project_root: 仓库根目录。
        blueprint: 蓝图路径（默认 docs/project_layout_gear2.md）。

    Returns:
        判定报告（violations 为空即通过）。

    Raises:
        RuntimeError: git 不可用或命令失败（fail-closed）。
    """
    report = AuditReport(before=before, head=head)
    bp = blueprint if blueprint is not None else project_root / "docs/project_layout_gear2.md"
    if before == head or before == "0" * 40:
        # before==head：空范围；before 全 0：首次 push（GitHub 无历史可审计）
        return report

    changed = _changed_files(before, head, project_root)
    if not changed:
        return report

    # ── 判据 A：门禁参数变更须付对价 ──
    # 先收集所有"改过任一门禁文件且含参数实质变更"的 commit（去重），再逐个审计
    param_shas: set[str] = set()
    for gate_file in sorted(GATE_FILES):
        for sha in _commits_touching(before, head, gate_file, project_root):
            if _commit_has_param_change(sha, project_root):
                param_shas.add(sha)
    for sha in sorted(param_shas):
        files = _commit_files(sha, project_root)
        # 业务文件 = myquant/ 下非门禁目录、非门禁两文件（门禁目录自身改动=治理，不算混业务）
        business = [
            f
            for f in files
            if f.startswith("myquant/")
            and f not in GATE_FILES
            and not f.startswith("myquant/leakage_gate/")
        ]
        message = _commit_message(sha, project_root)
        changed_bp = "docs/project_layout_gear2.md" in files
        has_issue = bool(_ISSUE_RE.search(message))
        has_writeback = bool(_WRITEBACK_RE.search(message)) or changed_bp
        if business:
            report.violations.append(
                f"判据A {sha[:7]} 门禁参数变更非独立 commit：混入业务文件 "
                f"{business[0] if business else ''}（对价要求：独立 commit + 工单 #N + 蓝图回写）"
            )
        if not has_issue and not has_writeback:
            report.violations.append(
                f"判据A {sha[:7]} 门禁参数变更缺对价证据：commit message 无工单号(#N)且无回写指认"
            )
        if not changed_bp and not has_writeback:
            report.violations.append(
                f"判据A {sha[:7]} 门禁参数变更缺蓝图回写：同 commit 未改 "
                "docs/project_layout_gear2.md 且 message 无回写指认"
            )

    # ── 判据 B：新增 .py/.md 文件须蓝图登记 ──
    for rel in _added_files(before, head, project_root):
        if not rel.endswith(_AUDITED_SUFFIXES):
            continue
        if not _blueprint_has_path(rel, bp):
            report.violations.append(
                f"判据B 新增文件 {rel} 未在蓝图登记（docs/project_layout_gear2.md 目录树或回写记录）——"
                "先更新蓝图再提交（治理规则 1.2）"
            )

    return report
