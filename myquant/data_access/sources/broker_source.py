# 文件名：myquant/data_access/sources/broker_source.py
"""
broker_source.py - 券商实时数据源（预留接口）
=============================================
为实盘交易预留的券商数据源接口，当前版本为骨架实现，
后续接入XTP（中泰）/QMT（迅投）等券商SDK时填充具体实现。

设计目标:
  1. 统一券商接口抽象，不同券商SDK只需实现子类
  2. 实时行情推送（WebSocket/回调，非轮询）
  3. 交易状态同步（委托/成交/持仓实时更新）
  4. 断线重连和状态恢复
  5. 与回测数据源接口完全一致，策略层零改动

当前状态: 接口骨架，方法抛出NotImplementedError
后续计划:
  - XtpBrokerSource  : 中泰XTP极速交易API
  - QmtBrokerSource   : 迅投QMT交易API
  - TqSdkBrokerSource : 天勤量化（期货）

依赖: typing, datetime, threading
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from myquant.core.logger import get_logger

logger = get_logger("data_access.sources.broker_source")

from .base import (
    DataSourceBase,
    DataSourceStatus,
    MarketDataResult,
)


# ─────────────────────────────────────────────
# 券商数据源基类
# ─────────────────────────────────────────────

class BrokerDataSource(DataSourceBase):
    """
    券商实时数据源基类（预留接口）
    ==============================
    所有券商数据源继承此类，实现具体的SDK对接。

    【与回测数据源的区别】
    1. 实时行情：通过SDK推送，非轮询
    2. 交易接口：可下单/撤单/查询持仓（本类仅行情，交易在execution层）
    3. 状态同步：委托/成交实时推送
    4. 断线重连：SDK自动重连，状态恢复

    Usage（后续实现示例）:
        class XtpBrokerSource(BrokerDataSource):
            @property
            def name(self): return "broker_xtp"
            def connect(self): ...  # XTP连接
            def subscribe_realtime(self, symbol, callback): ...
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        Parameters
        ----------
        config : dict | None
            券商配置：
            - broker_name : str  券商名称
            - account_id : str   资金账号
            - api_key : str      API密钥
            - api_secret : str   API密钥
            - server_addr : str  服务器地址
            - server_port : int  服务器端口
        """
        super().__init__(config)
        self._connected = False
        self._broker_name = self.config.get("broker_name", "unknown")

    # ── 基本属性 ──

    @property
    def name(self) -> str:
        return f"broker_{self._broker_name}"

    @property
    def source_type(self) -> str:
        return "broker"

    @property
    def priority(self) -> int:
        return 5  # 券商源优先级最高（实盘场景）

    @property
    def supported_markets(self) -> Set[str]:
        return {"A股"}

    @property
    def supported_periods(self) -> Set[str]:
        return {"tick", "minute", "daily"}

    @property
    def supports_financial(self) -> bool:
        return False  # 券商通常不提供财务数据

    @property
    def supports_realtime(self) -> bool:
        return True  # 券商源核心能力就是实时行情

    # ── 连接管理（子类实现） ──

    def connect(self) -> bool:
        """
        连接券商服务器（子类实现）

        Returns
        -------
        bool : 连接是否成功
        """
        raise NotImplementedError("子类必须实现connect方法")

    def disconnect(self) -> None:
        """断开券商服务器连接（子类实现）"""
        raise NotImplementedError("子类必须实现disconnect方法")

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected

    # ═══════════════════════════════════════════
    # 行情接口（子类实现）
    # ═══════════════════════════════════════════

    def get_market_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ) -> MarketDataResult:
        """
        获取历史行情数据（券商SDK通常提供有限的历史数据）

        【防未来函数】end_date 之后的数据严格不返回
        """
        if not self._connected:
            return MarketDataResult(symbol, [], source=self.name)
        # 子类实现具体逻辑
        raise NotImplementedError("子类必须实现get_market_data方法")

    def get_realtime_quote(self, symbol: str) -> Optional[Dict]:
        """
        获取实时报价（单次查询，非订阅）

        Returns
        -------
        dict | None : 实时报价 {
            symbol, last_price, open, high, low,
            volume, amount, bid1, ask1, time, ...
        }
        """
        if not self._connected:
            return None
        raise NotImplementedError("子类必须实现get_realtime_quote方法")

    def subscribe_realtime(
        self,
        symbol: str,
        callback: Callable[[Dict], None],
        period: str = "tick",
    ) -> bool:
        """
        订阅实时行情（券商SDK推送）

        Parameters
        ----------
        symbol : str
            股票代码
        callback : callable
            行情回调函数，接收dict参数
        period : str
            订阅周期 tick/minute

        Returns
        -------
        bool : 订阅是否成功
        """
        if not self._connected:
            return False
        # 注册回调
        success = super().subscribe_realtime(symbol, callback, period)
        if not success:
            return False
        # 子类实现SDK订阅逻辑
        raise NotImplementedError("子类必须实现SDK订阅逻辑")

    def unsubscribe_realtime(self, symbol: str, callback: Optional[Callable] = None) -> bool:
        """取消实时行情订阅"""
        if not self._connected:
            return False
        # 子类实现SDK取消订阅
        raise NotImplementedError("子类必须实现SDK取消订阅逻辑")

    # ═══════════════════════════════════════════
    # 交易状态接口（行情层只读，交易在execution层）
    # ═══════════════════════════════════════════

    def get_account_info(self) -> Optional[Dict]:
        """
        获取账户信息（资金/持仓）

        Returns
        -------
        dict | None : {
            total_asset, available_cash, market_value,
            positions: [{symbol, volume, available, cost_price, ...}],
            ...
        }
        """
        if not self._connected:
            return None
        raise NotImplementedError("子类必须实现get_account_info方法")

    def register_trade_callback(self, callback: Callable[[Dict], None]) -> None:
        """
        注册交易状态回调（委托/成交通知）

        Parameters
        ----------
        callback : callable
            交易回调函数，接收dict参数：
            {type: 'order'|'trade', order_id, symbol, price, volume, status, ...}
        """
        if not hasattr(self, "_trade_callbacks"):
            self._trade_callbacks = []
        self._trade_callbacks.append(callback)

    def _notify_trade_callbacks(self, data: Dict) -> None:
        """通知交易回调（子类收到推送时调用）"""
        for callback in getattr(self, "_trade_callbacks", []):
            try:
                callback(data)
            except Exception as e:
                logger.debug(f"交易回调异常: {e}")

    # ═══════════════════════════════════════════
    # 基础信息接口
    # ═══════════════════════════════════════════

    def get_stock_list(self, market: str = "A股") -> List[Dict]:
        """获取可交易股票列表（券商通常提供）"""
        if not self._connected:
            return []
        raise NotImplementedError("子类必须实现get_stock_list方法")

    # ═══════════════════════════════════════════
    # 健康管理
    # ═══════════════════════════════════════════

    def health_check(self) -> DataSourceStatus:
        """健康检查：检查券商连接状态"""
        if not self._connected:
            self._update_status(DataSourceStatus.DOWN)
            return DataSourceStatus.DOWN
        try:
            # 尝试查询账户信息验证连接
            info = self.get_account_info()
            if info is not None:
                self._update_status(DataSourceStatus.HEALTHY)
            else:
                self._update_status(DataSourceStatus.DEGRADED)
        except Exception:
            self._update_status(DataSourceStatus.DOWN)
        return self._status

    # ── 断线重连（子类实现） ──

    def reconnect(self) -> bool:
        """
        断线重连（子类实现）

        实盘场景网络波动时自动调用，恢复连接和订阅状态。
        """
        raise NotImplementedError("子类必须实现reconnect方法")
