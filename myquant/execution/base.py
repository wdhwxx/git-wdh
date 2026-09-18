# 文件名：myquant/execution/base.py
"""
execution/base.py - 执行接口抽象基类
======================================
回测模拟撮合与实盘券商交易的统一执行接口。

设计原则（规则2：为实盘无缝对接准备）:
  - ExecutionInterface 定义统一执行接口
  - 回测模拟券商(sim_broker.py)与实盘券商适配器(live/brokers/)共同实现此接口
  - 引擎层只依赖 ExecutionInterface，切换回测/实盘零修改
  - 订单状态流转由 state_machine.py 统一管理

防未来函数（规则4）:
  - 回测模拟撮合只使用当前bar及历史数据
  - 市价单以开盘价成交（不是收盘价）
  - 订单创建与撮合不在同一根bar内完成（T+1成交）
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.models.trade import Order, Position, Trade

logger = get_logger(__name__)


class ExecutionInterface(ABC):
    """
    执行接口抽象基类（Abstract Base Class）

    回测模拟券商与实盘券商适配器的共同接口。

    核心接口:
      - submit_order(order)    — 提交订单
      - cancel_order(order_id) — 撤销订单
      - get_order(order_id)    — 查询订单
      - get_active_orders()    — 查询活跃订单
      - get_account()          — 查询账户资金
      - get_positions()        — 查询持仓

    子类必须实现以上所有方法。
    """

    def __init__(self, name: str = "execution"):
        """
        初始化执行接口

        参数:
            name: 执行器名称（用于日志标识）
        """
        self.name = name
        self._connected = False
        logger.info(f"执行接口初始化: {name}")

    # ------------------------------------------------------------------
    # 订单操作（子类必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """
        提交订单

        参数:
            order: 订单对象

        返回:
            Order: 订单（状态已更新，通常为 SUBMITTED 或 REJECTED）
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        撤销订单

        参数:
            order_id: 订单ID

        返回:
            True=撤销请求已发送/成功，False=订单不存在或不可撤销
        """
        pass

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        """
        查询订单详情

        参数:
            order_id: 订单ID

        返回:
            Order: 订单对象，不存在返回 None
        """
        pass

    @abstractmethod
    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """
        查询活跃订单（PENDING/SUBMITTED/PARTIAL_FILLED 状态）

        参数:
            symbol: 可选，按标的代码筛选

        返回:
            活跃订单列表
        """
        pass

    # ------------------------------------------------------------------
    # 资金与持仓查询（子类必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    def get_account(self) -> Dict[str, Any]:
        """
        查询账户资金

        返回:
            {
                "cash": float,           # 总资金
                "available_cash": float, # 可用资金
                "frozen_cash": float,    # 冻结资金
                "market_value": float,    # 持仓市值
                "total_assets": float,    # 总资产
            }
        """
        pass

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """
        查询当前持仓

        返回:
            持仓列表
        """
        pass

    # ------------------------------------------------------------------
    # 连接管理（子类可覆盖）
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        连接（实盘券商用，模拟券商直接返回True）

        返回:
            True=连接成功
        """
        self._connected = True
        logger.info(f"执行器已连接: {self.name}")
        return True

    def disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        logger.info(f"执行器已断开: {self.name}")

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected

    # ------------------------------------------------------------------
    # 成交记录查询（子类可覆盖）
    # ------------------------------------------------------------------

    def get_trades(self, symbol: Optional[str] = None) -> List[Trade]:
        """
        查询成交记录

        参数:
            symbol: 可选，按标的筛选

        返回:
            成交记录列表
        """
        return []

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self.name}, "
            f"connected={self._connected})"
        )
