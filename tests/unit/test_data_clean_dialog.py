# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_data_clean_dialog.py
"""
数据清洗对话框（myquant/gui/data_clean_dialog.py）单元测试（G0-9 补测）
==========================================================
offscreen 构建 + 纯函数覆盖：
  - match_rule：'前缀.后缀' / 仅前缀 / 空规则
  - split_rules：逗号（中英文）拆分
  - load_all_symbols：存在库读取 / 缺失路径返回 None
  - DataCleanDialog：offscreen 构建、窗口标题、project_root 保存
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from myquant.gui.data_clean_dialog import (  # noqa: E402
    DataCleanDialog,
    load_all_symbols,
    match_rule,
    split_rules,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


class TestMatchRule:
    def test_prefix_suffix(self) -> None:
        assert match_rule("000001.SH", "000.SH") is True
        assert match_rule("000001.SH", "000.SZ") is False

    def test_prefix_suffix_case_insensitive(self) -> None:
        assert match_rule("399001.SZ", "399.sz") is True

    def test_plain_prefix(self) -> None:
        assert match_rule("399001.SZ", "399") is True
        assert match_rule("600519.SH", "399") is False

    def test_empty_rule(self) -> None:
        assert match_rule("600519.SH", "  ") is False


class TestSplitRules:
    def test_comma_and_chinese_comma(self) -> None:
        assert split_rules("000,399，600") == ["000", "399", "600"]

    def test_blank_filtered(self) -> None:
        assert split_rules("000, ,399") == ["000", "399"]

    def test_empty_text(self) -> None:
        assert split_rules("") == []


class TestLoadAllSymbols:
    def test_returns_symbols(self, tmp_path: Path) -> None:
        db = tmp_path / "market_data.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE market_data (symbol TEXT, period TEXT, adjust TEXT)"
            )
            conn.executemany(
                "INSERT INTO market_data VALUES (?, 'daily', 'raw')",
                [("000001.SZ",), ("600519.SH",)],
            )
            conn.commit()
        finally:
            conn.close()
        symbols = load_all_symbols(db)
        assert symbols == ["000001.SZ", "600519.SH"]

    def test_missing_path_returns_none(self, tmp_path: Path) -> None:
        assert load_all_symbols(tmp_path / "nope.db") is None


class TestDataCleanDialog:
    def test_construct_and_title(self, tmp_path: Path, qapp: QApplication) -> None:
        dlg = DataCleanDialog(project_root=tmp_path)
        assert dlg.project_root == tmp_path
        assert "清洗" in dlg.windowTitle()
        dlg.close()
