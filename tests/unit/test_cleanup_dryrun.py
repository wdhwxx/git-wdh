# 文件名：tests/unit/test_cleanup_dryrun.py
"""
test_cleanup_dryrun.py - 日志清理可绕过 dry-run 测试（KP-M05 / T-29）
=======================================================================
覆盖修复：
  1. execute_plan 在 options.execute=False（dry-run 预览）时不删除任何文件。
  2. execute_plan 在 options.execute=True 时实际删除。

依赖: pytest, myquant.utils.cleanup_service
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from myquant.utils.cleanup_service import (
    CleanupOptions,
    CleanupPlan,
    ScanResult,
    execute_plan,
)


def _make_plan(root: Path, execute: bool, target: Path) -> CleanupPlan:
    junk = ScanResult()
    junk.add(target, "test", "t29-dryrun")
    return CleanupPlan(
        project_root=root,
        junk=junk,
        outputs_keep=[],
        outputs_delete=[],
        scanned_at=datetime.now(),
        options=CleanupOptions(execute=execute),
    )


class TestCleanupDryRun:
    def test_dryrun_不删除(self, tmp_path) -> None:
        """DoD：execute=False（dry-run）时 execute_plan 不删除文件。"""
        target = tmp_path / "junk.bin"
        target.write_bytes(b"x" * 10)
        plan = _make_plan(tmp_path, execute=False, target=target)
        res = execute_plan(plan)
        assert res == (0, 0, 0)
        assert target.exists()

    def test_execute_删除(self, tmp_path) -> None:
        """DoD：execute=True 时 execute_plan 实际删除文件。"""
        target = tmp_path / "junk.bin"
        target.write_bytes(b"x" * 10)
        plan = _make_plan(tmp_path, execute=True, target=target)
        res = execute_plan(plan)
        assert res[0] == 1
        assert not target.exists()
