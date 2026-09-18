# 文件名：myquant/live/brokers/qmt_broker.py
"""
qmt_broker.py - 迅投 QMT 券商适配器
====================================
QMT（迅投极速策略交易系统）适配器，接口 100% 对齐 BaseBroker。
当前交付为【模拟环境验证版】：mode="SIM" 下内置完整模拟撮合引擎，
接口完整可测；真实 QMT SDK（xtquant）通道在 mode="LIVE" 下统一占位，
接入点见下方「真实 QMT SDK 对接说明」。

继承关系:
  ExecutionInterface (myquant/execution/base.py)
    └── BaseBroker (base_broker.py)
          └── QmtBroker (本文件)

核心设计（为实时行情及实盘无缝对接）:
  1. 行情驱动撮合 — update_market_price(symbol, price) 与 PaperBroker 同接口，
     data_feed 收到行情推送后统一调用；成交回报走 _execute_fill 统一入口。
  2. 双模式切换 — mode="SIM" 本地模拟撮合；mode="LIVE" 走 _call_qmt_sdk 占位。
     上层（BrokerManager / 引擎）只依赖 ExecutionInterface，切换零修改。
  3. 回调驱动 — 成交/账户/行情回调注册机制继承 BaseBroker，
     与真实 QMT 异步回报同构（on_order/on_trade → _execute_fill）。
  4. 多账户预留 — account_id 参数 + get_account 返回 account_id 字段。

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数

防未来函数说明:
  - 成交只依据【已发生】的行情数据（update_market_price 传入的当前价）
  - 不预测未来价格，不假设成交价；限价单以当前价触及申报价才成交
  - T+1 冻结只累计当日【已成交】买入量，可卖数量=持仓-当日买入

依赖: typing, time, myquant.live.brokers.base_broker,
      myquant.models.trade, myquant.core.logger
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.live.brokers.base_broker import BaseBroker, _locked
from myquant.models.trade import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Trade,
)

logger = get_logger(__name__)

# ════════════════════════════════════════════════════════════════════
#  真实 QMT SDK 对接说明（后续迭代在 _call_qmt_sdk 中实现）
# ════════════════════════════════════════════════════════════════════
# 迅投 QMT 提供 xtquant 库：
#   from xtquant import xttrader, xtconstant
#   from xtquant.xttype import StockAccount
#
# 关键对象与挂载点（接入后上层代码零修改）:
#   - 连接   : XtQuantTrader(qmt_path, session_id) → start() → connect()
#             挂载点: connect() / disconnect()
#   - 账户   : StockAccount(account_id)   → 挂载点: __init__ / get_account()
#   - 下单   : order_stock(account, symbol, order_type, order_volume,
#                         price_type, price)   → 挂载点: submit_order()
#   - 撤单   : cancel_order_stock(account, order_id) → 挂载点: cancel_order()
#   - 行情   : subscribe_quote(symbol, callback)     → 挂载点: subscribe()
#   - 回报   : XtQuantTraderCallback.on_order / on_trade / on_tick
#             收到回报 → 更新订单状态 / 调用 self._execute_fill()（本类已实现）
#   - 资金   : query_stock_asset(account) → 挂载点: get_account()
#   - 持仓   : query_stock_positions(account) → 挂载点: get_positions()
#
# 接入步骤:
#   1. 安装 xtquant（QMT 客户端安装目录内置）
#   2. 在 _call_qmt_sdk 中实现各 method 分支（见各方法 TODO 注释）
#   3. 将 __init__ 中 mode 置为 "LIVE"，并填写 qmt_path / account_id
#   4. 上层（BrokerManager/引擎）无需任何改动
# ════════════════════════════════════════════════════════════════════


class QmtBroker(BaseBroker):
    """
    迅投 QMT 券商适配器

    Parameters
    ----------
    qmt_path : str
        QMT 客户端安装路径（LIVE 模式必填；SIM 模式仅作日志展示）
    account_id : str
        资金账号（多账户预留）
    mode : str
        "SIM" 模拟撮合（默认，无 QMT 环境可完整验证）
        "LIVE" 真实 QMT SDK（占位，未接入前调用核心动作会明确报错）
    initial_capital : float
        初始资金（元），仅 SIM 模式使用，默认 100 万
    commission_rate : float
        佣金费率（万2.5=0.00025），买卖双向
    min_commission : float
        单笔最低佣金（元），默认 5 元
    stamp_tax_rate : float
        印花税率（千1=0.001），仅卖出
    transfer_fee_rate : float
        过户费率（万0.1=0.00001），买卖双向
    slippage_mode : str
        滑点模式：ratio=按比例 / fixed=按固定金额（仅 SIM）
    slippage_value : float
        滑点数值（ratio 模式为万2=0.0002）
    enable_t1 : bool
        是否启用 T+1 交易限制（A股标准）
    lot_size : int
        最小交易单位（股），A股 1手=100股
    price_limit_pct : float
        涨跌停限制比例（普通股票 10%=0.10）
    """

    broker_name = "qmt_broker"
    broker_type = "qmt"

    # QMT 支持 A股 限价单与市价单
    supported_order_types: List[OrderType] = [OrderType.LIMIT, OrderType.MARKET]

    def __init__(
        self,
        qmt_path: str = "",
        account_id: str = "",
        mode: str = "SIM",
        initial_capital: float = 1_000_000.0,
        commission_rate: float = 0.00025,
        min_commission: float = 5.0,
        stamp_tax_rate: float = 0.001,
        transfer_fee_rate: float = 0.00001,
        slippage_mode: str = "ratio",
        slippage_value: float = 0.0002,
        enable_t1: bool = True,
        lot_size: int = 100,
        price_limit_pct: float = 0.10,
    ):
        super().__init__(name="qmt_broker")

        self._qmt_path = str(qmt_path or "")
        self._account_id = str(account_id or "")
        mode = str(mode or "SIM").upper()
        if mode not in ("SIM", "LIVE"):
            raise ValueError(f"mode 必须为 SIM 或 LIVE, 当前: {mode}")
        self._mode = mode

        # 资金参数（SIM 模式）
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.frozen_cash = 0.0

        # 交易成本参数
        self.commission_rate = float(commission_rate)
        self.min_commission = float(min_commission)
        self.stamp_tax_rate = float(stamp_tax_rate)
        self.transfer_fee_rate = float(transfer_fee_rate)
        self.slippage_mode = slippage_mode
        self.slippage_value = float(slippage_value)

        # 交易规则
        self.enable_t1 = bool(enable_t1)
        self.lot_size = int(lot_size)
        self.price_limit_pct = float(price_limit_pct)

        # 数据存（SIM 模式撮合）
        self._orders: Dict[str, Order] = {}
        self._trades: List[Trade] = []
        self._positions: Dict[str, Position] = {}
        self._t1_holdings: Dict[str, float] = {}   # 当日已成交买入（T+1 冻结）
        self._market_prices: Dict[str, float] = {}  # 已发生的最新行情价
        self._prev_close: Dict[str, float] = {}     # 昨收（首次行情时记录，涨跌停基准）

        # QMT 环境状态
        self._qmt_session_id = int(time.time()) % 1000000  # 会话标识（LIVE 用）
        self._xt_trader: Any = None                  # XtQuantTrader 实例占位

        # 真实 SDK 可用性标志（接入后置 True）
        self._qmt_sdk_ready = False

        logger.info(
            f"[qmt_broker] 初始化: mode={self._mode} "
            f"account_id={self._account_id or '(未绑定)'} "
            f"qmt_path={self._qmt_path or '(未配置)'}"
        )

    # ═══════════════════════════════════════════════
    #  配置加载
    # ═══════════════════════════════════════════════

    @_locked
    def load_config(self, config: Dict[str, Any]) -> None:
        """
        从配置字典加载 QMT 券商配置

        支持结构:
          {
            "qmt": {
              "qmt_path": "...",
              "account_id": "...",
              "mode": "SIM",
              "enable_t1": true,
              ...
            }
          }
        或扁平结构直接传 QMT 参数。
        """
        if not config:
            return
        cfg = config.get("qmt", config) if isinstance(config, dict) else {}
        if not isinstance(cfg, dict):
            cfg = {}

        if cfg.get("qmt_path"):
            self._qmt_path = str(cfg["qmt_path"])
        if cfg.get("account_id"):
            self._account_id = str(cfg["account_id"])
        mode = str(cfg.get("mode", self._mode)).upper()
        if mode in ("SIM", "LIVE"):
            self._mode = mode

        for key in ("commission_rate", "min_commission", "stamp_tax_rate",
                    "transfer_fee_rate", "slippage_value", "price_limit_pct"):
            if key in cfg and cfg[key] is not None:
                setattr(self, key, float(cfg[key]))
        for key in ("enable_t1",):
            if key in cfg and cfg[key] is not None:
                setattr(self, key, bool(cfg[key]))
        if cfg.get("slippage_mode") in ("ratio", "fixed"):
            self.slippage_mode = cfg["slippage_mode"]

        self._config = dict(cfg)
        logger.info(
            f"[qmt_broker] 配置加载: mode={self._mode} "
            f"account_id={self._account_id or '(未绑定)'}"
        )

    # ═══════════════════════════════════════════════
    #  连接管理
    # ═══════════════════════════════════════════════

    @_locked
    def connect(self) -> bool:
        """
        连接 QMT

        SIM 模式: 本地模拟连接成功（不依赖 QMT 客户端）。
        LIVE 模式: 调用真实 SDK（占位，未接入前抛 NotImplementedError）。
        """
        if self._connected:
            logger.info(f"[qmt_broker] 已连接，忽略重复连接")
            return True

        if self._mode == "LIVE":
            # TODO(QMT SDK): 真实接入
            #   from xtquant.xttrader import XtQuantTrader
            #   self._xt_trader = XtQuantTrader(self._qmt_path, self._qmt_session_id)
            #   self._xt_trader.start()
            #   self._xt_trader.connect()
            self._call_qmt_sdk("connect")

        # 模拟连接成功
        self._connected = True
        self._connect_time = time.time()
        self._last_heartbeat = time.time()
        self._stats["connect_count"] += 1
        logger.info(
            f"[qmt_broker] 连接成功: mode={self._mode} "
            f"account_id={self._account_id or '(未绑定)'}"
        )
        return True

    @_locked
    def disconnect(self) -> None:
        """断开连接"""
        if self._mode == "LIVE":
            # TODO(QMT SDK): self._xt_trader.stop() 等
            self._call_qmt_sdk("disconnect")
        if self._connected:
            self._connected = False
            self._stats["disconnect_count"] += 1
            logger.info(f"[qmt_broker] 已断开连接")

    @_locked
    def heartbeat(self) -> bool:
        """
        心跳检查（保持连接活跃）

        SIM 模式: 更新心跳时间戳，始终返回 True。
        LIVE 模式: 占位（真实 SDK 检查连接状态）。
        """
        if not self._connected:
            return False
        if self._mode == "LIVE":
            # TODO(QMT SDK): 检查 XtQuantTrader 连接状态
            self._call_qmt_sdk("heartbeat")
        self._last_heartbeat = time.time()
        return True

    def auto_reconnect(self) -> bool:
        """自动重连（断线时由调度器调用）"""
        if self._connected:
            return True
        ok = self.connect()
        if ok:
            self._stats["reconnect_count"] += 1
        return ok

    def health_check(self) -> Dict[str, Any]:
        """健康检查（连接状态/心跳延迟/订单队列/账户绑定）"""
        base = super().health_check()
        base.update(
            {
                "mode": self._mode,
                "account_id": self._account_id or None,
                "qmt_sdk_ready": self._qmt_sdk_ready,
                "active_orders": len(self.get_active_orders()),
                "pending_orders": sum(
                    1 for o in self._orders.values() if o.status is OrderStatus.SUBMITTED
                ),
            }
        )
        return base

    # ═══════════════════════════════════════════════
    #  订单操作（6 个抽象方法）
    # ═══════════════════════════════════════════════

    @_locked
    def submit_order(self, order: Order) -> Order:
        """
        提交订单

        流程:
          1. 校验订单合法性（复用基类 _validate_order）
          2. 资金/持仓检查（买入冻结资金，卖出检查可卖+T1）
          3. 注册订单到订单簿，状态 PENDING → SUBMITTED
          4. 触发订单更新回调

        SIM 模式: 订单进入本地订单簿，等待 update_market_price 驱动撮合。
        LIVE 模式: 调用真实 QMT SDK 下单（占位，未接入前明确报错）。

        【防未来函数】订单提交只使用当前已知信息，不假设成交价；
        成交由后续行情回报驱动（_execute_fill）。
        """
        # LIVE 模式走真实通道
        if self._mode == "LIVE":
            # TODO(QMT SDK): 下单
            #   order_stock(account, symbol, order_type, qty, price_type, price)
            #   收到 on_order 回报后更新订单状态
            self._call_qmt_sdk("submit_order", order=order)

        # 校验订单
        errors = self._validate_order(order)
        if errors:
            logger.error(f"[qmt_broker] 订单校验失败: {errors}")
            order.reject("; ".join(errors))
            self._record_order_rejected(order, "; ".join(errors))
            self._notify_order_update(order)
            return order

        # 资金检查（买入冻结）
        if order.side == OrderSide.BUY:
            estimated_price = order.price or self._market_prices.get(order.symbol, 0.0)
            estimated_cost = order.quantity * estimated_price
            if estimated_cost > self.available_cash:
                reason = f"资金不足: 需要={estimated_cost:.2f}, 可用={self.available_cash:.2f}"
                logger.warning(f"[qmt_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order
            self.frozen_cash += estimated_cost

        # 持仓检查（卖出 + T+1）
        if order.side == OrderSide.SELL:
            pos = self._positions.get(order.symbol)
            if pos is None or pos.quantity <= 0:
                reason = f"持仓不足: 无 {order.symbol} 持仓"
                logger.warning(f"[qmt_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order
            available_qty = pos.quantity
            if self.enable_t1:
                available_qty = pos.quantity - self._t1_holdings.get(order.symbol, 0.0)
            if order.quantity > available_qty:
                reason = (
                    f"可卖数量不足: 委托={order.quantity:.0f}, 可卖={available_qty:.0f}"
                    + (f" (T+1冻结={self._t1_holdings.get(order.symbol, 0.0):.0f})"
                       if self.enable_t1 else "")
                )
                logger.warning(f"[qmt_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order

        # 注册订单到订单簿（PENDING → SUBMITTED）
        if not order.submit():
            reason = f"订单状态无法提交: {order.status}"
            logger.warning(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._notify_order_update(order)
            return order
        self._orders[order.order_id] = order
        self._stats["total_orders"] += 1
        self._record_order_submit(order)

        logger.info(
            f"[qmt_broker] 订单已提交: {order.order_id} "
            f"{order.symbol} {order.side.value} {order.quantity:.0f} "
            f"@{order.price if order.price is not None else '市价'}"
        )
        self._notify_order_update(order)
        return order

    @_locked
    def cancel_order(self, order_id: str) -> bool:
        """撤单（仅可撤活跃订单）"""
        order = self._orders.get(order_id)
        if order is None or not order.status.is_active:
            logger.warning(f"[qmt_broker] 撤单失败: 订单不存在或非活跃 {order_id}")
            return False

        if self._mode == "LIVE":
            # TODO(QMT SDK): cancel_order_stock(account, order_id)
            self._call_qmt_sdk("cancel_order", order_id=order_id)

        order.cancel()
        self._record_order_cancelled(order)
        logger.info(f"[qmt_broker] 撤单成功: {order_id}")
        self._notify_order_update(order)
        return True

    @_locked
    def get_order(self, order_id: str) -> Optional[Order]:
        """查询订单（本地订单簿；LIVE 模式可扩展为 SDK 查询）"""
        return self._orders.get(order_id)

    @_locked
    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """查询活跃订单（可筛选标的）"""
        active = [o for o in self._orders.values() if o.status.is_active]
        if symbol:
            active = [o for o in active if o.symbol == symbol]
        return active

    @_locked
    def get_trades(self, symbol: Optional[str] = None) -> List[Trade]:
        """查询成交记录"""
        trades = list(self._trades)
        if symbol:
            trades = [t for t in trades if t.symbol == symbol]
        return trades

    @_locked
    def get_positions(self) -> List[Position]:
        """查询持仓"""
        return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[Position]:
        """查询单标的持仓"""
        return self._positions.get(symbol)

    @_locked
    def get_account(self) -> Dict[str, Any]:
        """
        查询账户资金

        SIM 模式: 本地账户 + QMT 特有字段（account_id）。
        LIVE 模式: 真实查询（占位）。
        """
        market_value = sum(
            p.quantity * p.market_price
            for p in self._positions.values()
            if p.quantity > 0
        )
        account = {
            "cash": round(self.cash, 2),
            "available_cash": round(self.available_cash, 2),
            "frozen_cash": round(self.frozen_cash, 2),
            "market_value": round(market_value, 2),
            "total_assets": round(self.cash + market_value, 2),
            "account_id": self._account_id,
            "mode": self._mode,
        }
        return account

    # ═══════════════════════════════════════════════
    #  行情订阅（为实时行情预留）
    # ═══════════════════════════════════════════════

    def subscribe(
        self,
        symbol: str,
        callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        data_type: str = "tick",
    ) -> bool:
        """
        订阅行情

        SIM 模式: 记录订阅列表（无外部行情源，由 update_market_price 喂入）。
        LIVE 模式: 占位（真实 QMT subscribe_quote）。

        Returns
        -------
        bool : 是否订阅成功
        """
        if self._mode == "LIVE":
            # TODO(QMT SDK): subscribe_quote(account, symbol)
            #   收到 on_tick 回调 → self.update_market_price(symbol, price)
            self._call_qmt_sdk("subscribe", symbol=symbol, data_type=data_type)
            return False

        self._subscribed_symbols.add(symbol)
        if callback is not None:
            self._market_data_callbacks.setdefault(symbol, []).append(callback)
        logger.info(f"[qmt_broker] 订阅行情: {symbol} ({data_type})")
        return True

    def unsubscribe(self, symbol: str) -> bool:
        """取消订阅"""
        if self._mode == "LIVE":
            # TODO(QMT SDK): unsubscribe_quote(symbol)
            self._call_qmt_sdk("unsubscribe", symbol=symbol)
        existed = symbol in self._subscribed_symbols
        self._subscribed_symbols.discard(symbol)
        if symbol in self._market_data_callbacks:
            del self._market_data_callbacks[symbol]
        if existed:
            logger.info(f"[qmt_broker] 取消订阅: {symbol}")
        return existed

    def is_subscribed(self, symbol: str) -> bool:
        return symbol in self._subscribed_symbols

    def get_subscribed_symbols(self) -> List[str]:
        return sorted(self._subscribed_symbols)

    # ═══════════════════════════════════════════════
    #  模拟环境（SIM 模式撮合驱动）
    # ═══════════════════════════════════════════════

    @_locked
    def update_market_price(
        self,
        symbol: str,
        price: float,
        timestamp: Optional[float] = None,
    ) -> List[Trade]:
        """
        更新市场价（模拟实时行情），触发限价单撮合

        ★★★ 为实时行情预留的核心接口 ★★★
        实盘时由 data_feed 模块收到行情推送后调用此方法（与 PaperBroker 同接口）。
        收到行情后检查该标的全部活跃订单，价格触及申报价则成交。

        【防未来函数】只使用传入的当前价撮合，不预判未来价格。

        Parameters
        ----------
        symbol : str
            标的代码
        price : float
            当前市场价（已发生行情）
        timestamp : float | None
            时间戳（默认当前时间）

        Returns
        -------
        List[Trade] : 本次价格更新产生的成交记录
        """
        if timestamp is None:
            timestamp = time.time()

        # 首次行情记录为昨收基准（涨跌停计算用）
        if symbol not in self._prev_close or self._prev_close[symbol] <= 0:
            self._prev_close[symbol] = price

        # 更新市场价
        self._market_prices[symbol] = price

        # 更新持仓市场价
        pos = self._positions.get(symbol)
        if pos:
            pos.update_market_price(price)

        # 触发行情推送回调
        self._notify_market_data(symbol, {"price": price, "timestamp": timestamp})

        # 撮合当前标的的活跃订单
        trades: List[Trade] = []
        for order in self.get_active_orders(symbol=symbol):
            trade = self._match_order(order, price, timestamp)
            if trade is not None:
                trades.append(trade)
        return trades

    def simulate_fill(
        self,
        order_id: str,
        price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> Optional[Trade]:
        """
        强制成交指定订单（测试/调试用，跳过行情撮合条件）

        Parameters
        ----------
        order_id : str
            订单ID
        price : float | None
            成交价（默认取订单价或当前市场价）
        quantity : float | None
            成交数量（默认全量剩余）

        Returns
        -------
        Trade | None : 成交记录
        """
        order = self._orders.get(order_id)
        if order is None or not order.status.is_active:
            logger.warning(f"[qmt_broker] 强制成交失败: 订单不存在或非活跃 {order_id}")
            return None

        fill_price = price
        if fill_price is None:
            fill_price = order.price or self._market_prices.get(order.symbol)
        if fill_price is None:
            logger.warning(f"[qmt_broker] 强制成交失败: 无成交价 {order_id}")
            return None

        fill_qty = quantity if quantity is not None else order.remaining_qty
        return self._execute_fill(order, fill_price, time.time(), fill_qty)

    def set_market_price(self, symbol: str, price: float) -> None:
        """直接设置市场价（不触发撮合，初始化用）"""
        self._market_prices[symbol] = price
        if symbol not in self._prev_close or self._prev_close[symbol] <= 0:
            self._prev_close[symbol] = price

    # ═══════════════════════════════════════════════
    #  内部：撮合与成交回报（真实 QMT 回调同构）
    # ═══════════════════════════════════════════════

    @_locked
    def _match_order(
        self,
        order: Order,
        current_price: float,
        timestamp: float,
    ) -> Optional[Trade]:
        """
        撮合单个订单（限价触及/市价即时）

        买入: 当前价 <= 限价 → 成交（成交价=当前价）
        卖出: 当前价 >= 限价 → 成交（成交价=当前价）
        市价: 按当前价成交
        """
        if order.side == OrderSide.BUY:
            if order.order_type == OrderType.MARKET:
                return self._execute_fill(order, current_price, timestamp)
            if order.price is not None and current_price <= order.price:
                return self._execute_fill(order, current_price, timestamp)
        else:
            if order.order_type == OrderType.MARKET:
                return self._execute_fill(order, current_price, timestamp)
            if order.price is not None and current_price >= order.price:
                return self._execute_fill(order, current_price, timestamp)
        return None

    @_locked
    def _execute_fill(
        self,
        order: Order,
        fill_price: float,
        timestamp: float,
        fill_qty: Optional[float] = None,
    ) -> Optional[Trade]:
        """
        执行成交（统一成交回报入口）

        ★★★ 与未来真实 QMT 回调同构 ★★★
        真实 QMT on_trade 回报到达后，同样调用本方法落地成交，
        保证模拟与实盘的持仓/资金/回调逻辑完全一致。

        流程:
          1. 涨跌停检查
          2. 创建成交记录
          3. 计算手续费
          4. 更新订单状态
          5. 更新持仓与资金
          6. 触发回调
        """
        if not order.status.is_active:
            return None

        # 涨跌停检查（基于已发生行情与昨收）
        if not self._check_price_limit(order.symbol, fill_price, order.side):
            logger.debug(
                f"[qmt_broker] 涨跌停限制，未成交: {order.symbol} "
                f"price={fill_price:.2f} side={order.side.value}"
            )
            return None

        fill_quantity = fill_qty if fill_qty is not None else order.remaining_qty
        if fill_quantity <= 0:
            return None

        # 创建成交记录
        trade = Trade(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_quantity,
            price=round(fill_price, 4),
            timestamp=timestamp,
            strategy_id=order.strategy_id,
        )
        trade.commission = self._calc_commission(trade)
        trade.slippage = self._calc_slippage_amount(fill_price, fill_quantity)

        # 更新订单状态（支持分批）
        order.fill(fill_quantity, fill_price)
        if order.is_filled:
            self._record_order_filled(order)

        # 更新持仓与资金
        self._apply_trade(trade, order)

        # 注册成交
        self._trades.append(trade)
        self._stats["total_trades"] += 1

        logger.info(
            f"[qmt_broker] 成交: {order.symbol} {order.side.value} "
            f"{fill_quantity:.0f}@{fill_price:.2f} "
            f"手续费={trade.commission:.2f}"
        )

        # 触发回调
        self._notify_order_update(order)
        self._notify_trade(trade)
        return trade

    @_locked
    def _apply_trade(self, trade: Trade, order: Order) -> None:
        """应用成交到持仓和资金"""
        symbol = trade.symbol
        pos = self._positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self._positions[symbol] = pos

        # 释放冻结资金（买入订单）
        if order.side == OrderSide.BUY:
            frozen_to_release = trade.quantity * (order.price or trade.price)
            self.frozen_cash = max(0.0, self.frozen_cash - frozen_to_release)

        # 更新持仓
        pos.apply_fill(trade.quantity, trade.price, trade.side)

        # 更新资金
        total_cost = trade.quantity * trade.price + trade.commission
        if trade.side == OrderSide.BUY:
            self.cash -= total_cost
            if self.enable_t1:
                self._t1_holdings[symbol] = self._t1_holdings.get(symbol, 0.0) + trade.quantity
        else:
            self.cash += trade.quantity * trade.price - trade.commission

        # 更新市场价
        pos.update_market_price(trade.price)

    def _calc_commission(self, trade: Trade) -> float:
        """计算手续费（A股写实标准：佣金+过户费+印花税）"""
        amount = trade.notional
        commission = max(amount * self.commission_rate, self.min_commission)
        commission += amount * self.transfer_fee_rate
        if trade.side == OrderSide.SELL:
            commission += amount * self.stamp_tax_rate
        return round(commission, 2)

    def _calc_slippage_amount(self, price: float, quantity: float) -> float:
        """计算滑点金额（仅 SIM 模式统计用）"""
        notional = price * quantity
        if self.slippage_mode == "ratio":
            return round(notional * self.slippage_value, 2)
        return round(self.slippage_value, 2)

    def _check_price_limit(
        self,
        symbol: str,
        price: float,
        side: OrderSide,
    ) -> bool:
        """
        涨跌停检查

        涨停价 = 昨收 * (1 + price_limit_pct)
        跌停价 = 昨收 * (1 - price_limit_pct)

        买入: 价格不能超过涨停价
        卖出: 价格不能低于跌停价

        没有昨收价时不限制（模拟行情初始状态）。
        """
        prev_close = self._prev_close.get(symbol)
        if prev_close is None or prev_close <= 0:
            return True
        limit_up = prev_close * (1 + self.price_limit_pct)
        limit_down = prev_close * (1 - self.price_limit_pct)
        if side == OrderSide.BUY:
            return price <= limit_up
        return price >= limit_down

    # ═══════════════════════════════════════════════
    #  账户/持仓便捷属性
    # ═══════════════════════════════════════════════

    @property
    def available_cash(self) -> float:
        """可用资金 = 总资金 - 冻结资金"""
        return self.cash - self.frozen_cash

    @property
    def total_assets(self) -> float:
        """总资产 = 现金 + 持仓市值"""
        market_value = sum(
            p.quantity * p.market_price
            for p in self._positions.values()
            if p.quantity > 0
        )
        return self.cash + market_value

    @property
    def equity(self) -> float:
        """净资产（与总资产一致，QMT 语义）"""
        return self.total_assets

    @property
    def position_count(self) -> int:
        """持仓数量"""
        return len([p for p in self._positions.values() if p.quantity > 0])

    # ═══════════════════════════════════════════════
    #  T+1 结算
    # ═══════════════════════════════════════════════

    @_locked
    def settle_t1(self) -> None:
        """
        T+1 结算：清空当日买入冻结记录（日终/次日开盘前调用）

        说明: 真实 QMT 由券商结算，本方法仅 SIM 模式模拟日终结算，
        便于多日连续回测。防未来：只释放【已发生】的当日买入。
        """
        self._t1_holdings.clear()
        logger.info("[qmt_broker] T+1 结算完成（当日买入冻结已释放）")

    # ═══════════════════════════════════════════════
    #  真实 SDK 占位
    # ═══════════════════════════════════════════════

    def _call_qmt_sdk(self, method: str, **kwargs: Any) -> Any:
        """
        真实 QMT SDK 调用统一占位

        未接入前调用会抛 NotImplementedError（明确报错，不静默降级）。
        接入步骤见模块头部「真实 QMT SDK 对接说明」。

        Parameters
        ----------
        method : str
            connect / disconnect / heartbeat / submit_order / cancel_order
            / subscribe / unsubscribe / get_account / get_positions
        """
        raise NotImplementedError(
            f"[qmt_broker] 真实QMT SDK 通道未接入 (method={method}, "
            f"mode={self._mode})。接入方法见模块头部『真实QMT SDK对接说明』"
        )

    # ═══════════════════════════════════════════════
    #  重置
    # ═══════════════════════════════════════════════

    @_locked
    def reset(self) -> None:
        """重置券商状态（SIM 模式，测试用）"""
        self.cash = self.initial_capital
        self.frozen_cash = 0.0
        self._orders.clear()
        self._trades.clear()
        self._positions.clear()
        self._t1_holdings.clear()
        self._market_prices.clear()
        self._prev_close.clear()
        self.reset_stats()
        logger.info("[qmt_broker] 状态已重置")

    def __repr__(self) -> str:
        return (
            f"QmtBroker(mode={self._mode}, account_id={self._account_id or '未绑定'}, "
            f"connected={self._connected})"
        )
