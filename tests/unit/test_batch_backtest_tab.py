# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_batch_backtest_tab.py
"""
批量回测标签页（myquant/gui/batch_backtest_tab.py）单元测试（G0-9 补测）
==========================================================
offscreen 构建 + 构造校验：
  - 非法 project_root 抛 ValueError（非目录 / 非 Path）
  - 合法 project_root 构建、默认日期保存、sizeHint 收敛
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from myquant.gui.batch_backtest_tab import (  # noqa: E402
    DEFAULT_END_DATE,
    DEFAULT_START_DATE,
    BatchBacktestTab,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


class TestConstructorValidation:
    def test_non_dir_raises(self, qapp: QApplication, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="project_root 必须为存在的目录"):
            BatchBacktestTab(project_root=tmp_path / "nope")

    def test_non_path_raises(self, qapp: QApplication) -> None:
        with pytest.raises(ValueError):
            BatchBacktestTab(project_root="not/a/path")


class TestBatchBacktestTab:
    def test_construct_and_defaults(self, qapp: QApplication, tmp_path: Path) -> None:
        tab = BatchBacktestTab(project_root=tmp_path)
        assert tab._project_root == tmp_path
        assert tab._default_start == DEFAULT_START_DATE
        assert tab._default_end == DEFAULT_END_DATE

    def test_size_hint_capped(self, qapp: QApplication, tmp_path: Path) -> None:
        tab = BatchBacktestTab(project_root=tmp_path)
        hint = tab.sizeHint()
        assert hint.width() > 0
        assert hint.height() > 0
