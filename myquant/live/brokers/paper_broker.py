# 文件名：myquant/live/brokers/paper_broker.py
"""
paper_broker.py - 本地模拟券商（Paper Trading）
==================================================
无券商环境下的本地模拟撮合，用于实盘逻辑测试和策略验证。

继承关系:
  BaseBroker (base_broker.py)
    └── PaperBroker (本文件)

核心能力:
  1. 本地模拟撮合 — 市价单/限价单，支持T+1和涨跌停限制
  2. 订单管理 — 提交/撤销/查询，状态机完整
  3. 持仓管理 — 实时持仓、市值、盈亏
  4. 资金管理 — 可用资金、冻结资金、总资产
  5. 交易成本 — 佣金/印花税/过户费/滑点（A股写实标准）
  6. 回调触发 — 订单更新/成交回报/账户更新（继承BaseBroker）
  7. 实时行情接口 — update_market_price() / on_bar()（为实时行情预留）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（update_market_price/on_bar/回调全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 撮合只使用当前已知价格，不假设未来价格

防未来函数说明:
  - 市价单以当前已知市场价成交（update_market_price传入的价格）
  - 限价单只在价格触及限价时成交，不预判未来价格
  - T日创建的订单，T+1日价格更新时撮合（由调用方控制时机）
  - 不使用任何未来bar的数据

交易成本标准（A股写实）:
  - 佣金：万2（0.0002），最低5元，买卖双向
  - 印花税：万5（0.0005），仅卖出；2023-08-28 起由千1减半
  - 过户费：万0.1（0.00001），买卖双向
  - 滑点：万2（0.0002），买卖双向

依赖: typing, time, myquant.live.brokers.base_broker, myquant.models.trade, myquant.core.logger
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.models.trade import (
    Order,
    OrderSide,
    OrderType,
    Position,
    Trade,
)

from .base_broker import BaseBroker, BrokerType, _locked

logger = get_logger(__name__)


# ═══════════════════════════════════════════════
#  PaperBroker - 本地模拟券商
# ═══════════════════════════════════════════════

class PaperBroker(BaseBroker):
    """
    本地模拟券商（Paper Trading）

    无券商环境下的本地模拟撮合，用于实盘逻辑测试和策略验证。
    继承 BaseBroker，自动获得连接管理/行情订阅/回调注册能力。

    使用方式:
        broker = PaperBroker(initial_capital=1000000)
        broker.connect()

        # 注册回调（为事件驱动预留）
        broker.register_order_callback(my_on_order)
        broker.register_trade_callback(my_on_trade)

        # 提交订单
        order = Order(symbol="000001.SZ", side=BUY, order_type=MARKET, quantity=100)
        broker.submit_order(order)

        # 更新市场价（模拟实时行情），触发撮合
        broker.update_market_price("000001.SZ", 10.5)

        # 查询
        account = broker.get_account()
        positions = broker.get_positions()
        trades = broker.get_trades()
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        commission_rate: float = 0.0002,
        min_commission: float = 5.0,
        stamp_tax_rate: float = 0.0005,
        transfer_fee_rate: float = 0.00001,
        slippage_mode: str = "ratio",
        slippage_value: float = 0.0002,
        enable_t1: bool = True,
        lot_size: int = 100,
        price_limit_pct: float = 0.10,
    ):
        """
        初始化模拟券商

        Parameters
        ----------
        initial_capital : float
            初始资金（元），默认100万
        commission_rate : float
            佣金费率（万2=0.0002），买卖双向
        min_commission : float
            单笔最低佣金（元），默认5元
        stamp_tax_rate : float
            印花税率（万5=0.0005），仅卖出；2023-08-28 起由千1减半
        transfer_fee_rate : float
            过户费率（万0.1=0.00001），买卖双向
        slippage_mode : str
            滑点模式：ratio=按比例 / fixed=按固定金额
        slippage_value : float
            滑点数值（ratio模式为万2=0.0002）
        enable_t1 : bool
            是否启用T+1交易限制（A股标准）
        lot_size : int
            最小交易单位（股），A股1手=100股
        price_limit_pct : float
            涨跌停限制比例（普通股票10%=0.10）
        """
        super().__init__(name="paper_broker")

        # 资金参数
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)        # 总资金
        self.frozen_cash = 0.0                      # 冻结资金（挂单中）

        # 交易成本参数
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax_rate = stamp_tax_rate
        self.transfer_fee_rate = transfer_fee_rate
        self.slippage_mode = slippage_mode
        self.slippage_value = slippage_value

        # 交易规则
        self.enable_t1 = enable_t1
        self.lot_size = lot_size
        self.price_limit_pct = price_limit_pct

        # 数据存储
        self.positions: Dict[str, Position] = {}   # symbol -> Position
        self.orders: Dict[str, Order] = {}          # order_id -> Order
        self.trades: List[Trade] = []                # 成交记录
        self._market_prices: Dict[str, float] = {}  # 最新市场价
        self._prev_close: Dict[str, float] = {}      # 昨收价（用于涨跌停计算）
        self._t1_holdings: Dict[str, float] = {}     # T+1冻结数量（当日买入不可卖）

        logger.info(
            f"模拟券商初始化: 初始资金={initial_capital:,.0f}, "
            f"佣金率={commission_rate}, 最低佣金={min_commission}, "
            f"印花税={stamp_tax_rate}, 滑点={slippage_value}({slippage_mode}), "
            f"T+1={'启用' if enable_t1 else '禁用'}"
        )

    # ═══════════════════════════════════════════
    #  券商信息属性（实现抽象方法）
    # ═══════════════════════════════════════════

    @property
    def broker_name(self) -> str:
        """券商名称"""
        return "paper_broker"

    @property
    def broker_type(self) -> BrokerType:
        """券商类型"""
        return BrokerType.PAPER

    @property
    def supported_order_types(self) -> set:
        """支持的订单类型（市价单+限价单）"""
        return {OrderType.MARKET, OrderType.LIMIT}

    @property
    def supports_realtime_data(self) -> bool:
        """
        是否支持实时行情推送

        PaperBroker 不支持真正的推送，但支持通过 update_market_price()
        手动更新价格来模拟实时行情。
        """
        return False

    # ═══════════════════════════════════════════
    #  连接管理（覆盖基类）
    # ═══════════════════════════════════════════

    @_locked
    def connect(self) -> bool:
        """连接（模拟券商直接成功）"""
        self._connected = True
        self._connect_time = time.time()
        self._stats["connect_count"] += 1
        logger.info("[paper_broker] 已连接（模拟模式）")
        return True

    @_locked
    def disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        self._stats["disconnect_count"] += 1
        self._subscribed_symbols.clear()
        logger.info("[paper_broker] 已断开连接")

    # ═══════════════════════════════════════════
    #  订单操作（实现抽象方法）
    # ═══════════════════════════════════════════

    @_locked
    def _submit_order_impl(self, order: Order) -> Order:
        """
        提交订单（0.9 起由基类薄闸 submit_order 调用的具体实现）

        流程:
          1. 校验订单合法性
          2. 资金检查（买入冻结资金）
          3. 持仓检查（卖出检查可用数量，T+1限制）
          4. 注册订单到订单簿
          5. 状态: PENDING → SUBMITTED
          6. 触发订单更新回调

        【防未来函数】订单提交只使用当前已知信息，不假设成交价。

        Parameters
        ----------
        order : Order
            订单对象

        Returns
        -------
        Order : 订单（状态已更新）
        """
        # 校验订单
        errors = self._validate_order(order)
        if errors:
            logger.error(f"[paper_broker] 订单校验失败: {errors}")
            order.reject("; ".join(errors))
            self._record_order_rejected(order, "; ".join(errors))
            self._notify_order_update(order)
            return order

        # 资金检查（买入）
        if order.side == OrderSide.BUY:
            estimated_price = order.price or self._market_prices.get(order.symbol, 0)
            estimated_cost = order.quantity * estimated_price
            if estimated_cost > self.available_cash:
                reason = (
                    f"资金不足: 需要={estimated_cost:.2f}, "
                    f"可用={self.available_cash:.2f}"
                )
                logger.warning(f"[paper_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order
            # 冻结资金
            self.frozen_cash += estimated_cost

        # 持仓检查（卖出）
        if order.side == OrderSide.SELL:
            pos = self.positions.get(order.symbol)
            if pos is None or pos.quantity <= 0:
                reason = f"持仓不足: 无 {order.symbol} 持仓"
                logger.warning(f"[paper_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order

            # T+1检查：当日买入的数量不可卖出
            available_qty = pos.quantity
            if self.enable_t1:
                t1_frozen = self._t1_holdings.get(order.symbol, 0)
                available_qty = pos.quantity - t1_frozen

            if order.quantity > available_qty:
                reason = (
                    f"可卖数量不足: 委托={order.quantity:.0f}, "
                    f"可卖={available_qty:.0f}"
                    + (f" (T+1冻结={t1_frozen:.0f})" if self.enable_t1 else "")
                )
                logger.warning(f"[paper_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order

        # 注册订单
        self.orders[order.order_id] = order
        self._record_order_submit(order)

        # 状态转换: PENDING → SUBMITTED
        order.submit()

        logger.info(
            f"[paper_broker] 订单已提交: {order.symbol} "
            f"{order.side.value} {order.order_type.value} "
            f"qty={order.quantity:.0f} px={order.price}"
        )

        # 触发订单更新回调
        self._notify_order_update(order)

        return order

    @_locked
    def restore_state(self, state: Dict[str, Any]) -> None:
        """崩溃恢复：重新登记在途订单并回写冻结（KP-P3-03）。

        只恢复快照中的已发生事实（在途订单/冻结资金/T+1 冻结数量），不生成新信号。
        """
        restored = 0
        for od in state.get("orders") or []:
            if not isinstance(od, dict):
                continue
            try:
                order = Order.from_dict(dict(od))
            except Exception as e:
                logger.warning(f"[paper_broker] 恢复订单解析失败，跳过: {e}")
                continue
            if order.order_id and order.status.is_active:
                self._orders[order.order_id] = order
                restored += 1
        frozen = 0.0
        for o in self._orders.values():
            if o.side == OrderSide.BUY and o.status.is_active:
                px = o.price or self._market_prices.get(o.symbol, 0) or 0
                frozen += o.remaining_qty * px
        self.frozen_cash = frozen
        t1 = state.get("t1_holdings") or {}
        if isinstance(t1, dict):
            self._t1_holdings = {k: float(v) for k, v in t1.items() if float(v) > 0}
        logger.info(f"[paper_broker] 崩溃恢复: 在途订单 {restored} 笔，冻结回写 {frozen:.2f}")

    def cancel_order(self, order_id: str) -> bool:
        """
        撤销订单

        流程:
          1. 检查订单是否存在且活跃
          2. 释放冻结资金（买入订单未成交部分）
          3. 状态: SUBMITTED/PARTIAL_FILLED → CANCELLED
          4. 触发订单更新回调

        Parameters
        ----------
        order_id : str
            订单ID

        Returns
        -------
        bool : True=撤销成功，False=订单不存在或不可撤销
        """
        order = self.orders.get(order_id)
        if order is None:
            logger.warning(f"[paper_broker] 撤销失败: 订单不存在 {order_id}")
            return False

        if not order.status.is_active:
            logger.warning(f"[paper_broker] 撤销失败: 订单非活跃 {order_id} status={order.status}")
            return False

        # 释放冻结资金（买入订单未成交部分）
        if order.side == OrderSide.BUY:
            remaining = order.remaining_qty
            estimated_frozen = remaining * (order.price or self._market_prices.get(order.symbol, 0))
            self.frozen_cash = max(0.0, self.frozen_cash - estimated_frozen)

        # 状态转换
        order.cancel()
        self._record_order_cancelled(order)

        logger.info(f"[paper_broker] 订单已撤销: {order_id}")

        # 触发订单更新回调
        self._notify_order_update(order)

        return True

    @_locked
    def get_order(self, order_id: str) -> Optional[Order]:
        """查询订单详情"""
        return self.orders.get(order_id)

    @_locked
    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """查询活跃订单（PENDING/SUBMITTED/PARTIAL_FILLED）"""
        orders = [o for o in self.orders.values() if o.status.is_active]
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders

    # ═══════════════════════════════════════════
    #  资金与持仓查询（实现抽象方法）
    # ═══════════════════════════════════════════

    @_locked
    def get_account(self) -> Dict[str, Any]:
        """
        查询账户资金

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
        market_value = self._calc_market_value()
        return {
            "cash": round(self.cash, 2),
            "available_cash": round(self.available_cash, 2),
            "frozen_cash": round(self.frozen_cash, 2),
            "market_value": round(market_value, 2),
            "total_assets": round(self.cash + market_value, 2),
        }

    @_locked
    def get_positions(self) -> List[Position]:
        """查询当前持仓（非零持仓）"""
        return [pos for pos in self.positions.values() if pos.quantity != 0]

    @_locked
    def get_position(self, symbol: str) -> Optional[Position]:
        """查询指定标的持仓"""
        return self.positions.get(symbol)

    @_locked
    def get_trades(self, symbol: Optional[str] = None) -> List[Trade]:
        """查询成交记录"""
        trades = self.trades
        if symbol:
            trades = [t for t in trades if t.symbol == symbol]
        return trades

    # ═══════════════════════════════════════════
    #  模拟撮合（为实时行情预留）
    # ═══════════════════════════════════════════

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
        实盘时由 data_feed 模块收到行情推送后调用此方法。
        PaperBroker 在此方法中检查所有活跃限价单，价格触及则成交。

        【防未来函数】只使用传入的当前价格撮合，不预判未来价格。

        Parameters
        ----------
        symbol : str
            标的代码
        price : float
            当前市场价
        timestamp : float | None
            时间戳（默认当前时间）

        Returns
        -------
        List[Trade] : 本次价格更新产生的成交记录
        """
        if timestamp is None:
            timestamp = time.time()

        # 更新市场价
        self._market_prices[symbol] = price

        # 更新持仓市场价
        pos = self.positions.get(symbol)
        if pos:
            pos.update_market_price(price)

        # 触发行情推送回调
        self._notify_market_data(symbol, {"price": price, "timestamp": timestamp})

        # 撮合当前标的的活跃限价单
        active_orders = self.get_active_orders(symbol=symbol)
        trades: List[Trade] = []

        for order in active_orders:
            # 市价单在提交时不立即成交，等待价格更新
            if order.order_type == OrderType.MARKET:
                trade = self._match_market_order(order, price, timestamp)
                if trade:
                    trades.append(trade)
            elif order.order_type == OrderType.LIMIT:
                trade = self._match_limit_order(order, price, timestamp)
                if trade:
                    trades.append(trade)

        # 触发账户更新回调
        if trades:
            self._notify_account_update(self.get_account())

        return trades

    @_locked
    def on_bar(self, bar: Dict[str, Any]) -> List[Trade]:
        """
        新bar到来时撮合（兼容回测风格调用）

        ★★★ T+1成交 ★★★
        上一根bar创建的订单，在当前bar用开盘价/高低价撮合。
        由调用方控制调用时机（T日创建订单，T+1日调用on_bar）。

        Parameters
        ----------
        bar : Dict[str, Any]
            K线数据，包含: symbol, open, high, low, close, volume, timestamp

        Returns
        -------
        List[Trade] : 本bar产生的成交记录
        """
        symbol = bar.get("symbol", "")
        open_price = bar.get("open", 0)
        high_price = bar.get("high", 0)
        low_price = bar.get("low", 0)
        close_price = bar.get("close", 0)
        timestamp = bar.get("timestamp", time.time())

        # 更新昨收价（用于涨跌停计算）
        if symbol in self._market_prices:
            self._prev_close[symbol] = self._market_prices[symbol]

        # 更新市场价为收盘价
        self._market_prices[symbol] = close_price
        pos = self.positions.get(symbol)
        if pos:
            pos.update_market_price(close_price)

        # 撮合当前标的的活跃订单
        active_orders = self.get_active_orders(symbol=symbol)
        trades: List[Trade] = []

        for order in active_orders:
            if order.order_type == OrderType.MARKET:
                # 市价单以开盘价成交
                trade = self._match_market_order(order, open_price, timestamp)
                if trade:
                    trades.append(trade)
            elif order.order_type == OrderType.LIMIT:
                if order.side == OrderSide.BUY:
                    # 买入限价单: 限价 >= 最低价才成交
                    if order.price is not None and order.price >= low_price:
                        fill_price = min(order.price, open_price)
                        trade = self._execute_fill(order, fill_price, timestamp)
                        if trade:
                            trades.append(trade)
                else:
                    # 卖出限价单: 限价 <= 最高价才成交
                    if order.price is not None and order.price <= high_price:
                        fill_price = max(order.price, open_price)
                        trade = self._execute_fill(order, fill_price, timestamp)
                        if trade:
                            trades.append(trade)

        # 触发账户更新回调
        if trades:
            self._notify_account_update(self.get_account())

        return trades

    # ═══════════════════════════════════════════
    #  撮合内部方法
    # ═══════════════════════════════════════════

    def _match_market_order(
        self,
        order: Order,
        price: float,
        timestamp: float,
    ) -> Optional[Trade]:
        """撮合市价单（以当前价格成交）"""
        if price <= 0:
            return None
        return self._execute_fill(order, price, timestamp)

    def _match_limit_order(
        self,
        order: Order,
        current_price: float,
        timestamp: float,
    ) -> Optional[Trade]:
        """
        撮合限价单（价格触及限价时成交）

        买入: 当前价 <= 限价 → 成交（成交价=当前价）
        卖出: 当前价 >= 限价 → 成交（成交价=当前价）
        """
        if order.price is None:
            return None

        if order.side == OrderSide.BUY:
            if current_price <= order.price:
                return self._execute_fill(order, current_price, timestamp)
        else:
            if current_price >= order.price:
                return self._execute_fill(order, current_price, timestamp)

        return None

    @_locked
    def _execute_fill(
        self,
        order: Order,
        fill_price: float,
        timestamp: float,
    ) -> Optional[Trade]:
        """
        执行成交（核心撮合逻辑）

        流程:
          1. 应用滑点
          2. 涨跌停检查
          3. 创建成交记录
          4. 计算手续费
          5. 更新订单状态
          6. 更新持仓与资金
          7. 注册成交
          8. 触发回调

        Parameters
        ----------
        order : Order
            订单
        fill_price : float
            原始成交价（未含滑点）
        timestamp : float
            成交时间戳

        Returns
        -------
        Trade | None : 成交记录（撮合失败返回None）
        """
        if not order.status.is_active:
            return None

        # 应用滑点
        fill_price = self._apply_slippage(fill_price, order.side)

        # 涨跌停检查
        if not self._check_price_limit(order.symbol, fill_price, order.side):
            logger.debug(
                f"[paper_broker] 涨跌停限制，未成交: {order.symbol} "
                f"price={fill_price:.2f} side={order.side.value}"
            )
            return None

        # 成交数量（默认全量成交）
        fill_qty = order.remaining_qty

        # 创建成交记录
        trade = Trade(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_qty,
            price=fill_price,
            timestamp=timestamp,
            strategy_id=order.strategy_id,
        )

        # 计算手续费
        trade.commission = self._calc_commission(trade)

        # 计算滑点金额
        trade.slippage = self._calc_slippage_amount(fill_price, fill_qty)

        # 更新订单状态
        order.fill(fill_qty, fill_price)
        if order.is_filled:
            self._record_order_filled(order)

        # 更新持仓与资金
        self._apply_trade(trade, order)

        # 注册成交
        self.trades.append(trade)
        self._stats["total_trades"] += 1

        logger.info(
            f"[paper_broker] 模拟成交: {order.symbol} {order.side.value} "
            f"{fill_qty:.0f}@{fill_price:.2f} "
            f"手续费={trade.commission:.2f} 滑点={trade.slippage:.2f}"
        )

        # 触发回调
        self._notify_order_update(order)
        self._notify_trade(trade)

        return trade

    # ═══════════════════════════════════════════
    #  滑点与手续费计算
    # ═══════════════════════════════════════════

    def _apply_slippage(self, price: float, side: OrderSide) -> float:
        """
        应用滑点到成交价

        买入滑点: 成交价上移（更贵）
        卖出滑点: 成交价下移（更便宜）
        """
        if self.slippage_mode == "ratio":
            slippage_per_share = price * self.slippage_value
        else:  # fixed
            slippage_per_share = self.slippage_value

        if side == OrderSide.BUY:
            return price + slippage_per_share
        else:
            return price - slippage_per_share

    def _calc_slippage_amount(self, price: float, quantity: float) -> float:
        """计算滑点金额"""
        if self.slippage_mode == "ratio":
            return price * self.slippage_value * quantity
        else:
            return self.slippage_value * quantity

    def _build_fee_calculator(self) -> "FeeCalculator":  # noqa: F821  # 类型定义于 backtest.fee_common，L3 内不跨层 import；注解字符串不求值（#380 复核：ruff 0.16.3 对字符串注解仍查 F821，noqa 非冗余，保留）
        """P1-04：live 费用口径统一走回测 FeeCalculator（同一模型同一参数），
        杜绝 live/backtest 各写一份导致口径漂移。"""
        from myquant.backtest.fee_common import FeeCalculator
        from myquant.models.backtest import CommissionModel
        model = CommissionModel(
            buy_rate=self.commission_rate,
            sell_rate=self.commission_rate,
            min_commission=self.min_commission,
            stamp_tax=self.stamp_tax_rate,
            transfer_fee=self.transfer_fee_rate,
        )
        return FeeCalculator(commission_model=model)

    def _calc_commission(self, trade: Trade) -> float:
        """
        计算手续费（委托 FeeCalculator，与回测三引擎同一入口）

        买入: 佣金(最低5元) + 过户费
        卖出: 佣金(最低5元) + 印花税 + 过户费
        """
        amount = trade.notional  # 成交金额 = quantity * price
        commission, stamp_tax, transfer_fee = (
            self._build_fee_calculator().calc_commission_breakdown(trade.side, amount)
        )
        return round(commission + stamp_tax + transfer_fee, 2)

    # ═══════════════════════════════════════════
    #  涨跌停检查
    # ═══════════════════════════════════════════

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
        else:
            return price >= limit_down

    # ═══════════════════════════════════════════
    #  持仓与资金更新
    # ═══════════════════════════════════════════

    @_locked
    def _apply_trade(self, trade: Trade, order: Order) -> None:
        """
        应用成交到持仓和资金

        买入: 扣除资金（成交金额+手续费），增加持仓，T+1冻结
        卖出: 增加资金（成交金额-手续费），减少持仓，计算已实现盈亏
        """
        symbol = trade.symbol
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self.positions[symbol] = pos

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
            # T+1冻结：当日买入的数量不可卖出
            if self.enable_t1:
                self._t1_holdings[symbol] = self._t1_holdings.get(symbol, 0) + trade.quantity
        else:
            self.cash += (trade.quantity * trade.price - trade.commission)

        # 更新市场价
        pos.update_market_price(trade.price)

    def _calc_market_value(self) -> float:
        """计算持仓总市值"""
        return sum(
            pos.market_value
            for pos in self.positions.values()
            if pos.quantity != 0
        )

    @_locked
    def settle_t1(self) -> None:
        """
        T+1结算（每日收盘后调用，释放当日买入的冻结数量）

        实盘时由 scheduler 在每日收盘后调用。
        """
        released = dict(self._t1_holdings)
        self._t1_holdings.clear()
        if released:
            logger.info(f"[paper_broker] T+1结算完成，释放: {released}")

    # ═══════════════════════════════════════════
    #  属性
    # ═══════════════════════════════════════════

    @property
    @_locked
    def available_cash(self) -> float:
        """可用资金 = 总资金 - 冻结资金"""
        return max(0.0, self.cash - self.frozen_cash)

    @property
    @_locked
    def total_assets(self) -> float:
        """总资产 = 资金 + 持仓市值"""
        return self.cash + self._calc_market_value()

    @property
    @_locked
    def equity(self) -> float:
        """净值（相对初始资金）"""
        if self.initial_capital <= 0:
            return 1.0
        return self.total_assets / self.initial_capital

    @property
    @_locked
    def position_count(self) -> int:
        """持仓标的数量"""
        return len([p for p in self.positions.values() if p.quantity != 0])

    # ═══════════════════════════════════════════
    #  重置
    # ═══════════════════════════════════════════

    @_locked
    def reset(self) -> None:
        """重置模拟券商到初始状态（测试重启时用）"""
        self.cash = self.initial_capital
        self.frozen_cash = 0.0
        self.positions.clear()
        self.orders.clear()
        self.trades.clear()
        self._market_prices.clear()
        self._prev_close.clear()
        self._t1_holdings.clear()
        self.reset_stats()
        logger.info("[paper_broker] 已重置到初始状态")

    def __repr__(self) -> str:
        return (
            f"PaperBroker(cash={self.cash:,.0f}, "
            f"positions={self.position_count}, "
            f"orders={len(self.orders)}, "
            f"trades={len(self.trades)}, "
            f"equity={self.equity:.4f})"
        )
