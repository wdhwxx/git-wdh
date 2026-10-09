# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_backtest_config_dialog.py
"""
回测配置对话框（myquant/gui/backtest_config_dialog.py）单元测试（G0-9 补测）
==========================================================
offscreen 构建 + 纯逻辑覆盖：
  - 构造不抛错、project_root 保存、窗口标题
  - _cleanup_old_dashboards：超出 keep 的旧看板文件被清理并返回 (removed, kept)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from myquant.gui.backtest_config_dialog import BacktestConfigDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


class TestBacktestConfigDialog:
    def test_construct_saves_root(self, qapp: QApplication, tmp_path: Path) -> None:
        dlg = BacktestConfigDialog(project_root=tmp_path)
        assert dlg.project_root == tmp_path
        assert "回测配置" in dlg.windowTitle()
        dlg.close()


class TestCleanupOldDashboards:
    def test_removes_beyond_keep(self, qapp: QApplication, tmp_path: Path) -> None:
        import time
        dlg = BacktestConfigDialog(project_root=tmp_path)
        out_dir = tmp_path / "dashboards"
        out_dir.mkdir(exist_ok=True)
        names = ("回测看板_d1.html", "回测看板_d2.html", "回测看板_d3.html", "回测看板_d4.html")
        for name in names:
            (out_dir / name).write_text("x", encoding="utf-8")
        # 依次修改 mtime，使 d1 最旧、d4 最新
        base = time.time()
        for i, name in enumerate(names):
            ts = base - (4 - i) * 1000
            os.utime(out_dir / name, (ts, ts))
        removed, kept = dlg._cleanup_old_dashboards(str(out_dir), keep=2)
        assert len(removed) == 2  # 最旧 2 个被移除
        assert len(kept) == 2
        dlg.close()
