# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_cleanup_dialog.py
"""
项目清理对话框（myquant/gui/cleanup_dialog.py）单元测试（G0-9 补测）
==========================================================
offscreen 构建：构造不抛错、project_root 保存、窗口标题。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from myquant.gui.cleanup_dialog import CleanupDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


class TestCleanupDialog:
    def test_construct_saves_root(self, qapp: QApplication, tmp_path: Path) -> None:
        dlg = CleanupDialog(project_root=tmp_path)
        assert dlg.project_root == tmp_path
        assert "清理" in dlg.windowTitle()
        dlg.close()
