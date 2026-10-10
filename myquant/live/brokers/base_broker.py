# 文件名：myquant/live/brokers/base_broker.py
"""
base_broker.py - 券商抽象基类（实盘适配器统一入口）
====================================================
所有实盘券商适配器的基类，继承 ExecutionInterface，增加实盘特有能力。

继承关系:
  ExecutionInterface (myquant/execution/base.py)  ← 回测/实盘共用执行接口
    └── BaseBroker (本文件)                        ← 实盘券商基类，增加连接/订阅/回调
          ├── PaperBroker   (paper_broker.py)      ← 本地模拟撮合
          ├── XTPBroker     (xtp_broker.py)        ← 中泰XTP
          └── QMTBroker     (qmt_broker.py)        ← 迅投QMT

核心能力（为实时行情及实盘无缝对接预留）:
  1. 连接管理 — connect/disconnect/heartbeat/auto_reconnect
  2. 行情订阅 — subscribe/unsubscribe（WebSocket/推送方式预留）
  3. 回调注册 — on_order_update/on_trade/on_account（事件驱动方式预留）
  4. 订单操作 — submit_order/cancel_order/get_order/get_active_orders（继承）
  5. 资金持仓 — get_account/get_positions/get_trades（继承）
  6. 券商信息 — broker_name/broker_type/supported_order_types
  7. 配置加载 — load_config（从live_config.yaml读取券商配置）
  8. 健康检查 — health_check（连接状态/延迟/订单队列）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（订阅/回调/重连全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 订单提交只使用当前已知信息，成交回报异步处理

防未来函数说明:
  - 券商适配器不做任何行情预测，只执行策略发来的订单
  - 行情数据通过data_feed模块获取并清洗后再分发给策略
  - 订单成交价格由券商撮合决定，适配器不假设成交价
  - 回调机制确保成交回报异步处理，不阻塞主循环

依赖: abc, typing, time, myquant.execution.base, myquant.models.trade, myquant.core.logger
"""

from __future__ import annotations

import functools
import threading
import time
from abc import abstractmethod
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from myquant.core.logger import get_logger
from myquant.execution.base import ExecutionInterface
from myquant.models.trade import Order, OrderSide, OrderType, Position, Trade

logger = get_logger(__name__)

def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """线程锁装饰器：调用时获取 self._lock（RLock，可重入，支持嵌套调用）"""
    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper




# ═══════════════════════════════════════════════
#  券商类型枚举
# ═══════════════════════════════════════════════

class BrokerType(Enum):
    """券商类型"""
    PAPER = "paper"           # 本地模拟撮合（Paper Trading）
    XTP = "xtp"               # 中泰XTP
    QMT = "qmt"               # 迅投QMT
    CUSTOM = "custom"         # 自定义券商

    def __str__(self) -> str:
        return self.value


# ═══════════════════════════════════════════════
#  回调类型别名
# ═══════════════════════════════════════════════

# 订单更新回调: (order: Order) -> None
OrderCallback = Callable[[Order], None]
# 成交回报回调: (trade: Trade) -> None
TradeCallback = Callable[[Trade], None]
# 账户更新回调: (account: Dict[str, Any]) -> None
AccountCallback = Callable[[Dict[str, Any]], None]
# 行情推送回调: (symbol: str, data: Dict[str, Any]) -> None
MarketDataCallback = Callable[[str, Dict[str, Any]], None]


# ═══════════════════════════════════════════════
#  BaseBroker - 券商抽象基类
# ═══════════════════════════════════════════════

class BaseBroker(ExecutionInterface):
    """
    券商抽象基类（所有实盘券商适配器的父类）

    继承 ExecutionInterface，增加实盘特有能力：
      - 连接管理（connect/disconnect/heartbeat/auto_reconnect）
      - 行情订阅（subscribe/unsubscribe，为实时行情预留）
      - 回调注册（on_order_update/on_trade/on_account，为事件驱动预留）
      - 配置加载（load_config，从live_config.yaml读取）
      - 健康检查（health_check）

    子类必须实现的抽象方法（继承自ExecutionInterface）:
      - submit_order(order)    — 提交订单
      - cancel_order(order_id) — 撤销订单
      - get_order(order_id)    — 查询订单
      - get_active_orders(symbol) — 查询活跃订单
      - get_account()          — 查询账户资金
      - get_positions()        — 查询持仓

    子类可选择性覆盖的方法:
      - connect()              — 连接券商（默认设置_connected=True）
      - disconnect()           — 断开连接
      - heartbeat()            — 心跳检测
      - subscribe()            — 订阅行情
      - unsubscribe()          — 取消订阅
      - health_check()         — 健康检查

    Usage:
        class MyBroker(BaseBroker):
            @property
            def broker_name(self) -> str:
                return "my_broker"

            @property
            def broker_type(self) -> BrokerType:
                return BrokerType.CUSTOM

            def submit_order(self, order):
                # 实现下单逻辑
                ...

        broker = MyBroker()
        broker.load_config(config_dict)
        broker.connect()
        broker.register_order_callback(my_on_order)
        order = broker.submit_order(Order(...))
    """

    def __init__(self, name: str = "base_broker"):
        """
        初始化券商基类

        Parameters
        ----------
        name : str
            券商名称（用于日志标识）
        """
        super().__init__(name=name)

        # 线程锁：保护回调注册表/连接状态/统计的并发访问（RLock 可重入）
        self._lock = threading.RLock()

        # 连接状态
        self._connected = False
        self._last_heartbeat: float = 0.0
        self._connect_time: float = 0.0

        # 配置
        self._config: Dict[str, Any] = {}

        # 回调注册表
        self._order_callbacks: List[OrderCallback] = []
        self._trade_callbacks: List[TradeCallback] = []
        self._account_callbacks: List[AccountCallback] = []
        self._market_data_callbacks: Dict[str, List[MarketDataCallback]] = {}

        # 订阅列表
        self._subscribed_symbols: Set[str] = set()

        # 本地订单簿（order_id → Order）：提交的订单统一登记，供查单/补单对账；
        # 子类（如 PaperBroker）若已有 self.orders 命名，get_orders() 会兼容兜底
        self._orders: Dict[str, Order] = {}

        # 0.9（回写#294）：提交边界硬闸状态源接线（装配层注入，broker 不反向依赖）
        self._risk_pause_checkers: List[Callable[[], bool]] = []
        self._trading_hours_checker: Optional[Callable[[], bool]] = None
        self._kill_switch_flag_path: str = self._KILL_SWITCH_FLAG_DEFAULT

        # 统计
        self._stats = {
            "total_orders": 0,
            "filled_orders": 0,
            "cancelled_orders": 0,
            "rejected_orders": 0,
            "total_trades": 0,
            "connect_count": 0,
            "disconnect_count": 0,
            "reconnect_count": 0,
            "last_error": "",
        }

        logger.info(f"券商基类初始化: {name}")

    # ═══════════════════════════════════════════
    #  券商信息属性（子类必须实现）
    # ═══════════════════════════════════════════

    @property
    @abstractmethod
    def broker_name(self) -> str:
        """
        券商名称（用于标识和日志）

        示例子类实现:
            return "paper_broker"
            return "xtp_broker"
        """
        pass

    @property
    @abstractmethod
    def broker_type(self) -> BrokerType:
        """
        券商类型

        示例子类实现:
            return BrokerType.PAPER
            return BrokerType.XTP
        """
        pass

    @property
    def supported_order_types(self) -> Set[OrderType]:
        """
        支持的订单类型（子类可覆盖）

        默认支持市价单和限价单，STOP/STOP_LIMIT/TRAILING_STOP需券商支持。
        """
        return {OrderType.MARKET, OrderType.LIMIT}

    @property
    def supports_realtime_data(self) -> bool:
        """
        是否支持实时行情推送（子类可覆盖）

        PaperBroker和部分券商可能不支持实时推送，需要轮询。
        """
        return False

    # ═══════════════════════════════════════════
    #  配置加载
    # ═══════════════════════════════════════════

    @_locked
    def load_config(self, config: Dict[str, Any]) -> None:
        """
        加载券商配置（从live_config.yaml的broker段读取）

        Parameters
        ----------
        config : Dict[str, Any]
            券商配置字典，通常包含:
            - type: 券商类型
            - adapter: 适配器名称
            - host/port: 连接地址
            - account/password: 账号密码（建议从环境变量读取）
            - heartbeat_interval: 心跳间隔
            - reconnect_times: 重连次数
            - reconnect_interval: 重连间隔
            - order_timeout: 订单超时
            - max_retry_times: 最大重试次数
        """
        self._config = config or {}
        logger.info(f"[{self.broker_name}] 配置已加载: {list(self._config.keys())}")

    @property
    @_locked
    def config(self) -> Dict[str, Any]:
        """当前券商配置（只读）"""
        return dict(self._config)

    # ═══════════════════════════════════════════
    #  连接管理
    # ═══════════════════════════════════════════

    @_locked
    def connect(self) -> bool:
        """
        连接券商（子类可覆盖，实现真实连接逻辑）

        默认实现：设置_connected=True，记录连接时间。
        真实券商适配器应在此方法中建立TCP/WebSocket连接、登录认证。

        Returns
        -------
        bool : True=连接成功，False=连接失败
        """
        self._connected = True
        self._connect_time = time.time()
        self._stats["connect_count"] += 1
        logger.info(f"[{self.broker_name}] 已连接")
        return True

    @_locked
    def disconnect(self) -> None:
        """
        断开连接（子类可覆盖，实现真实断开逻辑）

        默认实现：设置_connected=False，清理订阅。
        真实券商适配器应在此方法中关闭连接、登出。
        """
        self._connected = False
        self._stats["disconnect_count"] += 1
        # 清理订阅（断开后订阅失效）
        self._subscribed_symbols.clear()
        logger.info(f"[{self.broker_name}] 已断开连接")

    @property
    @_locked
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected

    @_locked
    def heartbeat(self) -> bool:
        """
        心跳检测（子类可覆盖，实现真实心跳逻辑）

        默认实现：检查_connected状态，更新last_heartbeat时间。
        真实券商适配器应在此方法中发送心跳包、检测连接活性。

        Returns
        -------
        bool : True=连接正常，False=连接异常
        """
        if not self._connected:
            return False
        self._last_heartbeat = time.time()
        return True

    @property
    @_locked
    def last_heartbeat(self) -> float:
        """最后心跳时间戳"""
        return self._last_heartbeat

    @property
    @_locked
    def connect_time(self) -> float:
        """连接建立时间戳"""
        return self._connect_time

    @property
    @_locked
    def connection_duration(self) -> float:
        """连接持续时间（秒）"""
        if not self._connected or self._connect_time == 0:
            return 0.0
        return time.time() - self._connect_time

    @_locked
    def auto_reconnect(self) -> bool:
        """
        自动重连（子类可覆盖，实现真实重连逻辑）

        默认实现：调用disconnect()后调用connect()。
        真实券商适配器应在此方法中实现指数退避重连策略。

        Returns
        -------
        bool : True=重连成功，False=重连失败
        """
        if self._connected:
            logger.warning(f"[{self.broker_name}] 已连接，无需重连")
            return True

        max_retry = self._config.get("reconnect_times", 5)
        interval = self._config.get("reconnect_interval", 5)

        for attempt in range(1, max_retry + 1):
            logger.info(f"[{self.broker_name}] 第{attempt}次重连...")
            try:
                if self.connect():
                    self._stats["reconnect_count"] += 1
                    logger.info(f"[{self.broker_name}] 重连成功（第{attempt}次）")
                    return True
            except Exception as e:
                logger.error(f"[{self.broker_name}] 重连失败: {e}")
            if attempt < max_retry:
                time.sleep(interval)

        self._stats["last_error"] = "重连失败，已达最大重试次数"
        logger.error(f"[{self.broker_name}] 重连失败，已达最大重试次数({max_retry})")
        return False

    # ═══════════════════════════════════════════
    #  行情订阅（为实时行情预留，子类可覆盖）
    # ═══════════════════════════════════════════

    @_locked
    def subscribe(
        self,
        symbol: str,
        callback: Optional[MarketDataCallback] = None,
        data_type: str = "tick",
    ) -> bool:
        """
        订阅实时行情（为实时行情预留，子类可覆盖）

        默认实现：记录订阅关系，调用回调注册。
        支持实时推送的券商（XTP/QMT）应在此方法中建立行情订阅。
        不支持推送的券商（PaperBroker）可通过轮询方式模拟。

        Parameters
        ----------
        symbol : str
            标的代码，如 "000001.SZ"
        callback : MarketDataCallback | None
            行情推送回调函数 (symbol, data) -> None
        data_type : str
            行情类型：tick=逐笔 / bar=K线 / snapshot=快照

        Returns
        -------
        bool : True=订阅成功，False=订阅失败
        """
        if not self._connected:
            logger.warning(f"[{self.broker_name}] 未连接，无法订阅 {symbol}")
            return False

        self._subscribed_symbols.add(symbol)

        if callback is not None:
            if symbol not in self._market_data_callbacks:
                self._market_data_callbacks[symbol] = []
            if callback not in self._market_data_callbacks[symbol]:
                self._market_data_callbacks[symbol].append(callback)

        logger.info(f"[{self.broker_name}] 已订阅 {symbol} ({data_type})")
        return True

    @_locked
    def unsubscribe(self, symbol: str) -> bool:
        """
        取消订阅实时行情

        Parameters
        ----------
        symbol : str
            标的代码

        Returns
        -------
        bool : True=取消成功，False=未订阅
        """
        if symbol not in self._subscribed_symbols:
            logger.warning(f"[{self.broker_name}] 未订阅 {symbol}，无需取消")
            return False

        self._subscribed_symbols.discard(symbol)
        self._market_data_callbacks.pop(symbol, None)
        logger.info(f"[{self.broker_name}] 已取消订阅 {symbol}")
        return True

    @_locked
    def get_subscribed_symbols(self) -> List[str]:
        """获取当前订阅的标的列表"""
        return sorted(self._subscribed_symbols)

    @_locked
    def is_subscribed(self, symbol: str) -> bool:
        """是否已订阅某标的"""
        return symbol in self._subscribed_symbols

    # ═══════════════════════════════════════════
    #  回调注册（为事件驱动预留）
    # ═══════════════════════════════════════════

    @_locked
    def register_order_callback(self, callback: OrderCallback) -> None:
        """
        注册订单更新回调

        当订单状态变更时（提交/成交/撤销/拒绝），调用所有注册的回调。

        Parameters
        ----------
        callback : OrderCallback
            回调函数 (order: Order) -> None
        """
        if callback not in self._order_callbacks:
            self._order_callbacks.append(callback)
            logger.debug(f"[{self.broker_name}] 注册订单回调: {callback.__name__}")

    @_locked
    def register_trade_callback(self, callback: TradeCallback) -> None:
        """
        注册成交回报回调

        当有新的成交时，调用所有注册的回调。

        Parameters
        ----------
        callback : TradeCallback
            回调函数 (trade: Trade) -> None
        """
        if callback not in self._trade_callbacks:
            self._trade_callbacks.append(callback)
            logger.debug(f"[{self.broker_name}] 注册成交回调: {callback.__name__}")

    @_locked
    def register_account_callback(self, callback: AccountCallback) -> None:
        """
        注册账户更新回调

        当账户资金/持仓变更时，调用所有注册的回调。

        Parameters
        ----------
        callback : AccountCallback
            回调函数 (account: Dict) -> None
        """
        if callback not in self._account_callbacks:
            self._account_callbacks.append(callback)
            logger.debug(f"[{self.broker_name}] 注册账户回调: {callback.__name__}")

    @_locked
    def unregister_order_callback(self, callback: OrderCallback) -> None:
        """注销订单更新回调"""
        if callback in self._order_callbacks:
            self._order_callbacks.remove(callback)

    @_locked
    def unregister_trade_callback(self, callback: TradeCallback) -> None:
        """注销成交回报回调"""
        if callback in self._trade_callbacks:
            self._trade_callbacks.remove(callback)

    @_locked
    def unregister_account_callback(self, callback: AccountCallback) -> None:
        """注销账户更新回调"""
        if callback in self._account_callbacks:
            self._account_callbacks.remove(callback)

    # ═══════════════════════════════════════════
    #  回调触发（内部方法，子类调用）
    # ═══════════════════════════════════════════

    def _notify_order_update(self, order: Order) -> None:
        """触发订单更新回调（子类在订单状态变更时调用）

        线程安全：锁内拷贝回调列表，锁外执行回调，
        避免回调内再取其他锁造成死锁或长时间持锁。
        """
        with self._lock:
            callbacks = list(self._order_callbacks)
        for callback in callbacks:
            try:
                callback(order)
            except Exception as e:
                logger.error(f"[{self.broker_name}] 订单回调异常: {e}")

    def _notify_trade(self, trade: Trade) -> None:
        """触发成交回报回调（子类在收到成交时调用）

        线程安全：统计更新与列表拷贝在锁内，回调在锁外执行。
        """
        with self._lock:
            self._stats["total_trades"] += 1
            callbacks = list(self._trade_callbacks)
        for callback in callbacks:
            try:
                callback(trade)
            except Exception as e:
                logger.error(f"[{self.broker_name}] 成交回调异常: {e}")

    def _notify_account_update(self, account: Dict[str, Any]) -> None:
        """触发账户更新回调（子类在账户变更时调用）

        线程安全：锁内拷贝回调列表，锁外执行回调。
        """
        with self._lock:
            callbacks = list(self._account_callbacks)
        for callback in callbacks:
            try:
                callback(account)
            except Exception as e:
                logger.error(f"[{self.broker_name}] 账户回调异常: {e}")

    def _notify_market_data(self, symbol: str, data: Dict[str, Any]) -> None:
        """触发行情推送回调（子类在收到行情时调用）

        线程安全：锁内拷贝回调列表，锁外执行回调。
        """
        with self._lock:
            callbacks = list(self._market_data_callbacks.get(symbol, []))
        for callback in callbacks:
            try:
                callback(symbol, data)
            except Exception as e:
                logger.error(f"[{self.broker_name}] 行情回调异常({symbol}): {e}")

    # ═══════════════════════════════════════════
    #  订单操作（继承自ExecutionInterface，子类必须实现 _submit_order_impl）
    # ═══════════════════════════════════════════

    # 0.9（回写#294）：broker 提交边界硬闸——submit_order 基类不可绕过包装：
    # 先过薄闸（kill_switch / risk_paused / 非交易时段 → 拒 BUY；SELL/平仓/撤单
    # 永远放行 I-1/I-3），再进子类 _submit_order_impl 具体逻辑。所有适配器自动生效，
    # "绕过引擎直调 broker.submit_order" 也经过包装（用例12 成立条件）。
    # 状态源：kill_switch 标志文件（与阶段4 监护进程同一真源，文件存在=置位）；
    # risk_paused / 非交易时段经装配层注入判定回调（broker 不反向依赖 risk/engine）。
    _KILL_SWITCH_FLAG_DEFAULT = "run/kill_switch.flag"

    def set_risk_pause_checker(self, checker: Optional[Callable[[], bool]]) -> None:
        """注入 risk_paused 判定（RiskManager 状态非 NORMAL；装配层注入）。"""
        self._risk_pause_checkers = [checker] if checker is not None else []

    def add_risk_pause_checker(self, checker: Callable[[], bool]) -> None:
        """追加 risk_paused 判定回调（多源：状态暂停 / 熔断暂停）。"""
        self._risk_pause_checkers.append(checker)

    def set_trading_hours_checker(self, checker: Optional[Callable[[], bool]]) -> None:
        """注入交易时段判定（TradingHoursRule 同源：True=当前可交易；装配层注入）。"""
        self._trading_hours_checker = checker

    def set_kill_switch_flag_path(self, path: Optional[str]) -> None:
        """覆盖 kill_switch 标志文件路径（默认 run/kill_switch.flag，阶段4 同一真源）。"""
        self._kill_switch_flag_path = str(path) if path else self._KILL_SWITCH_FLAG_DEFAULT

    def kill_switch_active(self) -> bool:
        """kill_switch 激活判定：标志文件存在即置位（零依赖，进程崩了文件还在）。"""
        try:
            from pathlib import Path

            return Path(self._kill_switch_flag_path).exists()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[broker] kill_switch 标志读取异常（按未置位处理）: {e}")
            return False

    def _submission_gate(self, order: Order) -> Optional[str]:
        """0.9 提交薄闸：仅拦 BUY；SELL/平仓/撤单永远放行。返回拒绝原因或 None。"""
        if order.side != OrderSide.BUY:
            return None
        # 1) kill_switch 激活（文件信号真源，阶段4 监护进程同一通道）
        if self.kill_switch_active():
            return "kill_switch 激活（标志文件存在），拒绝开仓（broker 提交边界硬闸）"
        # 2) risk_paused（RiskManager 状态非 NORMAL，装配层注入判定）
        for checker in self._risk_pause_checkers:
            try:
                if checker():
                    return "风控暂停（risk_paused），拒绝开仓（broker 提交边界硬闸）"
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[broker] risk_pause_checker 异常（按未暂停处理）: {e}")
        # 3) 非交易时段（TradingHoursRule 同源判定，装配层注入）
        if self._trading_hours_checker is not None:
            try:
                if not self._trading_hours_checker():
                    return "非交易时段，拒绝开仓（broker 提交边界硬闸）"
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[broker] trading_hours_checker 异常（按可交易处理）: {e}")
        return None

    def _gate_reject(self, order: Order, reason: str) -> Order:
        """薄闸拒单：留审计（scope=broker_submission_gate）+ 返回 REJECTED 订单。"""
        logger.error(f"[broker] 提交硬闸拒绝 {order.symbol} {order.side.value}: {reason}")
        try:
            from myquant.api.middleware.auth import get_operation_guard

            guard = get_operation_guard()
            if guard is not None:
                guard.audit(
                    "broker_submission_gate", operator=self.broker_name,
                    action=f"submit_order {order.symbol} {order.side.value} qty={order.quantity}",
                    result="denied", reviewer=None, reason=reason,
                    actor_type="system",
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[broker] 提交硬闸审计留痕失败（不阻断拒单）: {e}")
        from myquant.models.trade import OrderStatus

        rejected = Order(
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
            stop_price=order.stop_price,
            status=OrderStatus.REJECTED,
            strategy_id=order.strategy_id,
            extra={**dict(order.extra or {}), "gate": "broker_submission_gate",
                   "rejected_reason": reason},
        )
        return rejected

    def submit_order(self, order: Order) -> Order:
        """
        提交订单（基类薄闸入口，不可绕过；子类实现 _submit_order_impl）。

        【0.9 提交边界硬闸】先过薄闸再进具体逻辑：
          - BUY：kill_switch 激活 / risk_paused / 非交易时段 → 返回 REJECTED 订单 + 审计；
          - SELL / 平仓 / 撤单：永远放行（I-1/I-3，R-7 应急卖单不受影响）。

        【防未来函数】订单提交只使用当前已知信息，不假设成交价。
        成交价格由券商撮合决定，通过_trade回调异步通知。

        Parameters
        ----------
        order : Order
            订单对象

        Returns
        -------
        Order : 订单（状态已更新，通常为 SUBMITTED、REJECTED）
        """
        reject_reason = self._submission_gate(order)
        if reject_reason is not None:
            return self._gate_reject(order, reject_reason)
        return self._submit_order_impl(order)

    @abstractmethod
    def _submit_order_impl(self, order: Order) -> Order:
        """
        提交订单（子类必须实现；0.9 起原 submit_order 逻辑改名为本方法）。

        【防未来函数】订单提交只使用当前已知信息，不假设成交价。
        成交价格由券商撮合决定，通过_trade回调异步通知。

        Parameters
        ----------
        order : Order
            订单对象

        Returns
        -------
        Order : 订单（状态已更新，通常为 SUBMITTED 或 REJECTED）
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        撤销订单（子类必须实现）

        Parameters
        ----------
        order_id : str
            订单ID

        Returns
        -------
        bool : True=撤销请求已发送，False=订单不存在或不可撤销
        """
        pass

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        """
        查询订单详情（子类必须实现）

        Parameters
        ----------
        order_id : str
            订单ID

        Returns
        -------
        Order | None : 订单对象，不存在返回None
        """
        pass

    @abstractmethod
    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """
        查询活跃订单（子类必须实现）

        Parameters
        ----------
        symbol : str | None
            可选，按标的筛选

        Returns
        -------
        List[Order] : 活跃订单列表
        """
        pass

    # ═══════════════════════════════════════════
    #  补单对账（任务45 / T3：断线重连后仅补发未成交订单）
    # ═══════════════════════════════════════════

    @_locked
    def resubmit_order(self, order: Order) -> Order:
        """
        断线补单强制重发（默认实现 = submit_order）

        语义: 对账确认【券商端无此单】后的显式补发通道。与 submit_order
        的区别在于——子类若在 submit_order 中做订单幂等去重（如 QmtBroker
        任务43 防桥接重发），补发副本（同一 order_id）会被幂等拦截而空转，
        因此真实券商子类必须覆盖本方法跳过幂等、真正重新委托。

        基类默认实现直接委托 submit_order（无幂等拦截的模拟券商天然兼容；
        有幂等去重的子类必须覆盖）。

        Parameters
        ----------
        order : Order
            补发订单（BrokerManager 传入的 PENDING 干净副本，order_id 不变）

        Returns
        -------
        Order : 补发后的订单（SUBMITTED 或 REJECTED）
        """
        return self.submit_order(order)

    @_locked
    def get_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """
        查询本地订单簿全量订单（补单对账数据源）

        返回订单簿中全部订单（含已成交/已撤/已拒终态），
        供 BrokerManager.resend_pending_orders() 与本地状态对账使用。

        Parameters
        ----------
        symbol : str | None
            可选，按标的筛选

        Returns
        -------
        List[Order] : 本地订单簿全量订单列表
        """
        store = getattr(self, "_orders", None)
        if store is None:
            # 兼容既有子类命名（PaperBroker 使用 self.orders）
            store = getattr(self, "orders", {})
        orders = list(store.values())
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders

    @_locked
    def query_broker_orders(
        self, symbol: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """
        查询券商端订单状态快照（补单对账用，子类可覆盖为真实 SDK 查询）

        返回结构与真实券商查询对齐:
          [{"order_id": str, "qmt_order_id": int|None, "status": OrderStatus,
            "symbol": str, "quantity": float}, ...]

        保守语义:
          - 返回 None        = 查询失败/无法确认券商端状态 → 调用方【不补发】
            （宁可漏单也不重复报单，防止断线期间查询失败触发补单风暴）
          - 返回 []          = 券商端确实无任何订单
          - 返回列表          = 券商端当日委托状态快照

        基类默认基于本地订单簿快照（模拟/测试场景，券商端=本地）；
        真实券商子类（QmtBroker）应覆盖为 SDK 当日委托查询（query_stock_orders）。

        Parameters
        ----------
        symbol : str | None
            可选，按标的筛选

        Returns
        -------
        Optional[List[Dict[str, Any]]] : 券商端订单状态快照；查询失败返回 None
        """
        try:
            result: List[Dict[str, Any]] = []
            for order in self.get_orders(symbol):
                extra = order.extra or {}
                result.append({
                    "order_id": str(order.order_id),
                    "qmt_order_id": extra.get("qmt_order_id"),
                    "status": order.status,
                    "symbol": order.symbol,
                    "quantity": order.quantity,
                })
            return result
        except Exception as e:  # noqa: BLE001 - 查询异常按"无法确认"处理
            logger.error(f"[{self.broker_name}] 券商端订单状态查询异常: {e}")
            return None

    # ═══════════════════════════════════════════
    #  资金与持仓查询（继承自ExecutionInterface，子类必须实现）
    # ═══════════════════════════════════════════

    @abstractmethod
    def get_account(self) -> Dict[str, Any]:
        """
        查询账户资金（子类必须实现）

        Returns
        -------
        Dict[str, Any] :
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
        查询当前持仓（子类必须实现）

        Returns
        -------
        List[Position] : 持仓列表
        """
        pass

    # ═══════════════════════════════════════════
    #  成交记录查询（继承自ExecutionInterface，子类可覆盖）
    # ═══════════════════════════════════════════

    def get_trades(self, symbol: Optional[str] = None) -> List[Trade]:
        """
        查询成交记录（子类可覆盖，默认返回空列表）

        Parameters
        ----------
        symbol : str | None
            可选，按标的筛选

        Returns
        -------
        List[Trade] : 成交记录列表
        """
        return []

    # ═══════════════════════════════════════════
    #  健康检查
    # ═══════════════════════════════════════════

    @_locked
    def health_check(self) -> Dict[str, Any]:
        """
        健康检查（子类可覆盖，增加券商特定检查项）

        Returns
        -------
        Dict[str, Any] :
            {
                "broker_name": str,
                "broker_type": str,
                "connected": bool,
                "connection_duration": float,
                "last_heartbeat": float,
                "subscribed_symbols": List[str],
                "stats": Dict,
                "status": "healthy" | "degraded" | "down",
            }
        """
        connected = self._connected
        heartbeat_ok = self.heartbeat() if connected else False

        if not connected:
            status = "down"
        elif not heartbeat_ok:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "broker_name": self.broker_name,
            "broker_type": str(self.broker_type),
            "connected": connected,
            "connection_duration": self.connection_duration,
            "last_heartbeat": self._last_heartbeat,
            "subscribed_symbols": self.get_subscribed_symbols(),
            "supported_order_types": [str(t) for t in self.supported_order_types],
            "supports_realtime_data": self.supports_realtime_data,
            "stats": dict(self._stats),
            "status": status,
        }

    # ═══════════════════════════════════════════
    #  统计信息
    # ═══════════════════════════════════════════

    @property
    @_locked
    def stats(self) -> Dict[str, Any]:
        """券商运行统计（只读）"""
        return dict(self._stats)

    @_locked
    def reset_stats(self) -> None:
        """重置统计信息"""
        self._stats = {
            "total_orders": 0,
            "filled_orders": 0,
            "cancelled_orders": 0,
            "rejected_orders": 0,
            "total_trades": 0,
            "connect_count": 0,
            "disconnect_count": 0,
            "reconnect_count": 0,
            "last_error": "",
        }
        logger.info(f"[{self.broker_name}] 统计已重置")

    # ═══════════════════════════════════════════
    #  工具方法
    # ═══════════════════════════════════════════

    @_locked
    def _record_order_submit(self, order: Order) -> None:
        """记录订单提交统计（子类调用）"""
        self._stats["total_orders"] += 1

    @_locked
    def _record_order_filled(self, order: Order) -> None:
        """记录订单成交统计（子类调用）"""
        self._stats["filled_orders"] += 1

    @_locked
    def _record_order_cancelled(self, order: Order) -> None:
        """记录订单撤销统计（子类调用）"""
        self._stats["cancelled_orders"] += 1

    @_locked
    def _record_order_rejected(self, order: Order, reason: str = "") -> None:
        """记录订单拒绝统计（子类调用）"""
        self._stats["rejected_orders"] += 1
        if reason:
            self._stats["last_error"] = f"订单被拒: {reason}"

    @_locked
    def _validate_order(self, order: Order) -> List[str]:
        """
        校验订单合法性（子类可调用）

        Returns
        -------
        List[str] : 错误列表（空=通过）
        """
        errors = order.validate()

        # 检查订单类型是否支持
        if order.order_type not in self.supported_order_types:
            errors.append(
                f"订单类型 {order.order_type} 不受支持，"
                f"支持的类型: {[str(t) for t in self.supported_order_types]}"
            )

        # 检查连接状态
        if not self._connected:
            errors.append("券商未连接，无法提交订单")

        return errors

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self.broker_name}, "
            f"type={self.broker_type}, "
            f"connected={self._connected}, "
            f"subscribed={len(self._subscribed_symbols)})"
        )
