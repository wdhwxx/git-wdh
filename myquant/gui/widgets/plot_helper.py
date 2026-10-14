# 文件名：myquant/gui/widgets/plot_helper.py
"""
plot_helper.py - 绘图辅助（TODO占位）
======================================
绘图辅助工具，封装matplotlib/pyqtgraph绘图接口。

v1状态: ⚪ TODO占位，后续迭代实现。

TODO(后续迭代):
  - 净值曲线绘制
  - 回撤曲线绘制
  - 绩效指标仪表盘
  - 持仓饼图
  - 收益分布直方图
  - 滚动夏普/回撤图
  - 月度收益热力图
  - 实时行情走势图
"""

from __future__ import annotations

from typing import Any, List

from myquant.core.logger import get_logger

logger = get_logger("gui.widgets.plot_helper")


class PlotHelper:
    """
    绘图辅助（TODO占位）

    ⚠️ v1未实现，后续迭代开发。
    """

    def __init__(self):
        logger.warning("[gui] PlotHelper v1未实现，后续迭代开发")

    def plot_equity_curve(self, dates: List[Any], values: List[float]) -> Any:
        """绘制净值曲线（v1空实现）"""
        logger.info(f"[gui] plot_equity_curve: {len(dates)}个点（v1空实现）")
        return None

    def plot_drawdown(self, dates: List[Any], drawdowns: List[float]) -> Any:
        """绘制回撤曲线（v1空实现）"""
        logger.info(f"[gui] plot_drawdown: {len(dates)}个点（v1空实现）")
        return None
