# 文件名：myquant/gui/widgets/side_panel.py
"""
side_panel.py - 侧边面板（TODO占位）
======================================
侧边面板，包含策略参数配置、账户概览、持仓列表等。

v1状态: ⚪ TODO占位，后续迭代实现。

TODO(后续迭代):
  - 策略参数配置面板（动态表单）
  - 账户概览（总资产/可用资金/持仓市值/盈亏）
  - 持仓列表表格
  - 订单列表表格
  - 成交明细表格
  - 风控状态指示器
  - 实时行情订阅列表
"""

from __future__ import annotations

from typing import Any, Optional

from myquant.core.logger import get_logger

logger = get_logger("gui.widgets.side_panel")


class SidePanel:
    """
    侧边面板（TODO占位）

    ⚠️ v1未实现，后续迭代开发。
    """

    def __init__(self, parent: Optional[Any] = None):
        self.parent = parent
        logger.warning("[gui] SidePanel v1未实现，后续迭代开发")

    def update(self) -> None:
        """更新面板（v1空实现）"""
        pass
