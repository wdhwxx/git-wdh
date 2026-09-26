# 文件名：myquant/gui/__init__.py
"""
gui - 桌面图形界面
===================
量化交易系统桌面GUI。

v1实现:
  - main_window.py: 主窗口（tkinter，菜单栏+工具栏+状态栏+占位面板）
  - widgets/: 自定义控件（v1占位，后续迭代实现）

TODO(后续迭代):
  - K线图控件
  - 持仓/订单/成交表格
  - 回测结果可视化
  - 实盘控制面板
  - 数据质量探查页面
  - 迁移到PyQt5/PySide6
"""

from myquant.gui.main_window import MainWindow
from myquant.gui.paper_run_dialog import PaperRunDialog

__all__ = ["MainWindow", "PaperRunDialog"]
