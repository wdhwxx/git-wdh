# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_plot_helper.py
"""
绘图辅助占位（myquant/gui/widgets/plot_helper.py）单元测试（G0-9 补测）
==========================================================
v1 为 TODO 占位：覆盖构造与各绘图方法空实现返回 None。
"""
from __future__ import annotations

from myquant.gui.widgets.plot_helper import PlotHelper


class TestPlotHelper:
    def test_init(self) -> None:
        PlotHelper()  # 构造不应抛错

    def test_plot_equity_curve_returns_none(self) -> None:
        helper = PlotHelper()
        result = helper.plot_equity_curve(["2024-01-01"], [1.0, 1.05])
        assert result is None

    def test_plot_drawdown_returns_none(self) -> None:
        helper = PlotHelper()
        result = helper.plot_drawdown(["2024-01-01"], [0.0, -0.03])
        assert result is None
