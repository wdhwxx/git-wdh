# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_kline_item.py
"""
K线绘制控件占位（myquant/gui/widgets/kline_item.py）单元测试（G0-9 补测）
==========================================================
v1 为 TODO 占位：覆盖构造（parent 保存、空 _bars）与 set_bars 存储行为。
"""
from __future__ import annotations

from myquant.gui.widgets.kline_item import KlineItem


class TestKlineItem:
    def test_init_sets_parent_and_empty_bars(self) -> None:
        parent = object()
        item = KlineItem(parent=parent)
        assert item.parent is parent
        assert item._bars == []

    def test_init_default_parent_none(self) -> None:
        item = KlineItem()
        assert item.parent is None

    def test_set_bars_stores(self) -> None:
        item = KlineItem()
        bars = [{"date": "2024-01-01", "open": 1.0}, {"date": "2024-01-02", "open": 1.1}]
        item.set_bars(bars)
        assert item._bars == bars

    def test_update_is_noop(self) -> None:
        item = KlineItem()
        item.update()  # v1 空实现，不应抛错
