# 文件名：myquant/gui/widgets/__init__.py
"""
widgets - GUI自定义控件
========================
v1占位，后续迭代实现。

TODO(后续迭代):
  - kline_item.py K线绘制控件
  - side_panel.py 侧边面板（参数配置、账户概览）
  - plot_helper.py 绘图辅助
"""

from myquant.gui.widgets.kline_item import KlineItem
from myquant.gui.widgets.side_panel import SidePanel
from myquant.gui.widgets.plot_helper import PlotHelper

__all__ = ["KlineItem", "SidePanel", "PlotHelper"]
