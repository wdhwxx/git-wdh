# 文件名：myquant/gui/widgets/kline_item.py
"""
kline_item.py - K线绘制控件（TODO占位）
==========================================
K线图绘制控件，支持K线、均线、成交量、指标叠加。

v1状态: ⚪ TODO占位，后续迭代实现。

TODO(后续迭代):
  - K线绘制（阳线/阴线）
  - 均线叠加（MA5/MA10/MA20/MA60）
  - 成交量柱状图
  - 技术指标叠加（MACD/RSI/KDJ/布林带）
  - 十字光标（显示OHLCV）
  - 缩放/平移
  - 买卖点标记
  - 支持pyqtgraph/matplotlib后端
  - 实时行情更新（实盘模式）

防未来函数说明:
  - K线图只展示当前及历史数据，不展示未来bar
  - 实盘模式只更新当前bar，不预判未来价格
"""

from __future__ import annotations

from typing import Any, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("gui.widgets.kline_item")


class KlineItem:
    """
    K线绘制控件（TODO占位）

    ⚠️ v1未实现，后续迭代开发。
    """

    def __init__(self, parent: Optional[Any] = None):
        self.parent = parent
        self._bars: List[Any] = []
        logger.warning("[gui] KlineItem v1未实现，后续迭代开发")

    def set_bars(self, bars: List[Any]) -> None:
        """设置K线数据"""
        self._bars = bars
        logger.info(f"[gui] KlineItem设置 {len(bars)} 根K线（v1仅存储，不绘制）")

    def update(self) -> None:
        """更新绘制（v1空实现）"""
        pass
