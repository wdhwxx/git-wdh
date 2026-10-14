# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_paper_run_dialog.py
"""
Paper 仿真运行控制台单元测试（#62 / #64）
=========================================
使用 offscreen 平台测试 PaperRunDialog 的创建、初始状态、盈亏颜色逻辑、
live_status.json 解析。不实际拉起子进程。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# offscreen 平台（必须在 QApplication 创建前设置）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from myquant.gui.paper_run_dialog import PaperRunDialog


@pytest.fixture(scope="session")
def qapp():
    """QApplication 单例。"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def dialog(qapp, tmp_path: Path) -> PaperRunDialog:
    """创建一个基于临时目录的弹窗实例。"""
    dlg = PaperRunDialog(tmp_path)
    yield dlg
    dlg.close()
    dlg.deleteLater()


class TestDialogInit:
    """弹窗初始化测试。"""

    def test_window_title(self, dialog: PaperRunDialog) -> None:
        assert dialog.windowTitle() == "鲲鹏仿真运测"

    def test_initial_stats_are_placeholder(self, dialog: PaperRunDialog) -> None:
        """初始指标卡全部为 --。"""
        for key in ("bar", "signal", "orders", "trades", "day_pnl", "total_pnl"):
            assert dialog._stat_labels[key].text() == "--", f"{key} not --"

    def test_primary_button_text_initial(self, dialog: PaperRunDialog) -> None:
        """初始主按钮文案为启动常驻引擎。"""
        assert dialog.btn_primary.text() == "启动常驻引擎"

    def test_panel_state_initial(self, dialog: PaperRunDialog) -> None:
        """初始 badge 为未启动。"""
        assert "未启动" in dialog._status_badge.text()


class TestPnlColor:
    """盈亏颜色逻辑（红涨绿跌）。"""

    def test_profit_red(self, dialog: PaperRunDialog) -> None:
        """盈利显示红色 + 号。"""
        dialog._set_pnl_label("total_pnl", 12450.0)
        text = dialog._stat_labels["total_pnl"].text()
        assert text.startswith("+")
        assert "12,450" in text

    def test_loss_green(self, dialog: PaperRunDialog) -> None:
        """亏损显示绿色无 + 号。"""
        dialog._set_pnl_label("day_pnl", -300.0)
        text = dialog._stat_labels["day_pnl"].text()
        assert not text.startswith("+")
        assert "-300" in text

    def test_zero_neutral(self, dialog: PaperRunDialog) -> None:
        """持平显示中性色。"""
        dialog._set_pnl_label("total_pnl", 0.0)
        assert dialog._stat_labels["total_pnl"].text() == "0"


class TestLiveStatusParsing:
    """live_status.json 解析逻辑测试。"""

    def test_poll_missing_file_no_crash(self, dialog: PaperRunDialog, tmp_path: Path) -> None:
        """live_status.json 不存在时不崩溃。"""
        dialog._project_root = tmp_path
        dialog._poll_live_status()  # 不应抛异常

    def test_poll_updates_stats(self, dialog: PaperRunDialog, tmp_path: Path) -> None:
        """有 live_status.json 时正确解析并更新指标。"""
        stats_dir = tmp_path / "outputs" / "paper_stats"
        stats_dir.mkdir(parents=True)
        import json
        data = {
            "stats": {
                "total_bars": 210,
                "buy_signals": 89,
                "sell_signals": 98,
                "total_orders": 5,
                "total_trades": 0,
            },
            "total_assets": 2012450.0,
            "day_start_assets": 2000000.0,
            "initial_capital": 2000000.0,
        }
        (stats_dir / "live_status.json").write_text(
            json.dumps(data), encoding="utf-8"
        )
        dialog._project_root = tmp_path
        dialog._poll_live_status()
        assert dialog._stat_labels["bar"].text() == "210"
        assert dialog._stat_labels["orders"].text() == "5"
        # 总盈亏 = 2012450 - 2000000 = 12450
        assert "12,450" in dialog._stat_labels["total_pnl"].text()
        # 当日盈亏 = 2012450 - 2000000 = 12450
        assert "12,450" in dialog._stat_labels["day_pnl"].text()

    def test_corrupt_json_no_crash(self, dialog: PaperRunDialog, tmp_path: Path) -> None:
        """损坏的 JSON 不崩溃。"""
        stats_dir = tmp_path / "outputs" / "paper_stats"
        stats_dir.mkdir(parents=True)
        (stats_dir / "live_status.json").write_text("{not valid json", encoding="utf-8")
        dialog._project_root = tmp_path
        dialog._poll_live_status()  # 不应抛异常
