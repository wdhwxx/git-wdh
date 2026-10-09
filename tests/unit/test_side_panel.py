# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_side_panel.py
"""
侧边面板占位（myquant/gui/widgets/side_panel.py）单元测试（G0-9 补测）
==========================================================
v1 为 TODO 占位：覆盖构造（parent 保存）与 update 空实现。
"""
from __future__ import annotations

from myquant.gui.widgets.side_panel import SidePanel


class TestSidePanel:
    def test_init_sets_parent(self) -> None:
        parent = object()
        panel = SidePanel(parent=parent)
        assert panel.parent is parent

    def test_init_default_parent_none(self) -> None:
        panel = SidePanel()
        assert panel.parent is None

    def test_update_is_noop(self) -> None:
        panel = SidePanel()
        panel.update()  # v1 空实现，不应抛错
