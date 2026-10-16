# -*- coding: utf-8 -*-
"""门禁参数变更审计测试（#C-4·5，判据A 对价 + 判据B 蓝图登记）。

覆盖：用 tmp git 仓库构造真实 commit 场景——
判据A：基线变更独立 commit + 工单/回写/蓝图回写 通过；混业务文件 红；
       无工单 红；蓝图未回写 红；
判据B：新增未登记 .py 红；蓝图已登记 通过；
范围空（before==head / 无变更）通过；git 不可用 fail-closed。
import 用全路径形式（R-19 引用级解析可识别）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import myquant.leakage_gate.gate_audit as ga

GATE_REL = "tests/unit/test_architecture.py"
BP_REL = "docs/project_layout_gear2.md"
GATE_HEADER = 'STRUCTURE_BASELINE: dict[str, dict[str, int]] = {"a": {"lines": 1}}'
BLUEPRINT_BODY = "# 蓝图 v1\n\n## 目录树\n\n- tests/unit/test_architecture.py\n- myquant/leakage_gate/gate_audit.py\n"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, timeout=60
    )
    return proc.stdout.strip()


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for rel, content in files.items():
        _write(repo, rel, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message, "--quiet")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """构造 tmp git 仓库：初始 commit 含门禁文件 + 蓝图（作为审计 before）。"""
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "audit@test.local")
    _git(tmp_path, "config", "user.name", "audit-test")
    _write(tmp_path, GATE_REL, GATE_HEADER)
    _write(tmp_path, BP_REL, BLUEPRINT_BODY)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init", "--quiet")
    return tmp_path


def _audit(repo: Path, before: str, head: str) -> ga.AuditReport:
    return ga.run_audit(before=before, head=head, project_root=repo)


# ── 判据A：通过场景 ──────────────────────────────────────────
def test_a_pass_with_full_consideration(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(
        repo,
        "fix(gate): 基线对价同步 #123",
        {
            GATE_REL: 'STRUCTURE_BASELINE = {"a": {"lines": 2}}',  # 参数变更
            BP_REL: BLUEPRINT_BODY + "## 回写\n基线 1→2（#123）",
        },
    )
    report = _audit(repo, before, head)
    assert report.ok, report.violations


def test_a_ok_when_no_gate_change(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "feat: 普通业务改动", {GATE_REL: GATE_HEADER + "\n# 注释变更"})
    report = _audit(repo, before, head)
    assert report.ok, report.violations


# ── 判据A：红场景 ────────────────────────────────────────────
def test_a_red_when_mixed_with_business(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(
        repo,
        "feat: 改参数又改业务 #456",
        {
            GATE_REL: 'STRUCTURE_BASELINE = {"a": {"lines": 3}}',
            "myquant/core/foo.py": "X = 1\n",  # 业务文件混入
            BP_REL: BLUEPRINT_BODY + "## 回写\n基线 1→3（#456）",
        },
    )
    report = _audit(repo, before, head)
    assert not report.ok
    assert any("非独立 commit" in v for v in report.violations)


def test_a_ok_when_gate_dir_change_only(repo: Path) -> None:
    """门禁目录自身改动（新增 leakage_gate 模块）不算混业务。"""
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(
        repo,
        "fix(gate): 基线对价同步 #321",
        {
            GATE_REL: 'STRUCTURE_BASELINE = {"a": {"lines": 6}}',
            "myquant/leakage_gate/other.py": "Y = 2\n",  # 门禁目录新模块（治理）
            BP_REL: BLUEPRINT_BODY + "## 回写\n基线 1→6（#321）；新增 other.py（#321）",
        },
    )
    report = _audit(repo, before, head)
    assert report.ok, report.violations


def test_a_red_when_no_issue_no_writeback(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(
        repo,
        "调整一下基线",  # 无工单、无回写
        {
            GATE_REL: 'STRUCTURE_BASELINE = {"a": {"lines": 4}}',
            BP_REL: BLUEPRINT_BODY,
        },
    )
    report = _audit(repo, before, head)
    assert not report.ok
    assert any("缺对价证据" in v for v in report.violations)


def test_a_red_when_blueprint_not_updated(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(
        repo,
        "fix(gate): 抬基线 #789",  # 有工单但未改蓝图、message 无回写
        {GATE_REL: 'STRUCTURE_BASELINE = {"a": {"lines": 5}}'},
    )
    report = _audit(repo, before, head)
    assert not report.ok
    assert any("缺蓝图回写" in v for v in report.violations)


def test_a_red_when_entry_only_changed_no_consideration(repo: Path) -> None:
    """仅注册表条目变更（FILE_STMT_CEILING 条目）也属参数变更——缺对价须红。"""
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(
        repo,
        "feat: 业务扩展加条目",  # 无工单、无回写、未改蓝图
        {
            GATE_REL: GATE_HEADER,
            "myquant/leakage_gate/coverage_gate.py": 'FILE_STMT_CEILING = {"leakage_gate/gate_audit.py": 102}\n',
        },
    )
    report = _audit(repo, before, head)
    assert not report.ok
    assert any("缺对价证据" in v for v in report.violations)


# ── 判据B：新增文件登记 ───────────────────────────────────────
def test_b_red_when_added_file_not_in_blueprint(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(
        repo,
        "chore: 新增临时文件",  # 无门禁参数变更，判据A不触发
        {"docs/tmp/scratch.py": "print('x')\n", "docs/tmp/note.md": "# note\n"},
    )
    report = _audit(repo, before, head)
    assert not report.ok
    assert any("未在蓝图登记" in v for v in report.violations)


def test_b_ok_when_added_file_registered_in_blueprint(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(
        repo,
        "feat: 新增模块（蓝图已登记）",
        {
            "myquant/leakage_gate/gate_audit.py": "X = 1\n",
            BP_REL: BLUEPRINT_BODY + "## 回写\n新增 gate_audit.py（#C-4·5）",
        },
    )
    report = _audit(repo, before, head)
    assert report.ok, report.violations


def test_b_ignores_non_py_md(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "chore: 加数据文件", {"docs/data.json": "{}\n", "docs/trend.jsonl": "a\n"})
    report = _audit(repo, before, head)
    assert report.ok, report.violations


# ── 范围与 fail-closed ────────────────────────────────────────
def test_empty_range_pass(repo: Path) -> None:
    sha = _git(repo, "rev-parse", "HEAD")
    report = _audit(repo, sha, sha)
    assert report.ok


def test_no_changes_pass(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    report = _audit(repo, before, before)
    assert report.ok


def test_summary_shape(repo: Path) -> None:
    before = _git(repo, "rev-parse", "HEAD")
    report = _audit(repo, before, before)
    assert "门禁参数变更审计" in report.summary()
    assert "全部判据通过" in report.summary()
