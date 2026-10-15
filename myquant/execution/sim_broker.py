# 文件名：myquant/execution/sim_broker.py
"""
execution/sim_broker.py - 回测模拟券商
========================================
回测模拟撮合实现，管理订单/持仓/资金，在新bar到来时撮合订单。

设计原则（规则2：为实盘无缝对接准备）:
  - 实现 ExecutionInterface 接口，与实盘券商适配器统一接口
  - 引擎层只依赖 ExecutionInterface，切换回测/实盘零修改
  - 订单状态由 OrderStateMachine 统一管理
  - 投资组合管理（资金/持仓/盈亏）内置于此，回测引擎直接调用

防未来函数（规则4）:
  - 撮合只使用当前bar的 open/high/low 数据
  - 市价单以当前bar开盘价成交（不是收盘价）
  - T日创建的订单，T+1日开盘撮合（由引擎层控制调用时机）
  - 不使用未来bar的数据

交易成本标准（交接文档7.1节，A股写实）:
  - 佣金：万2（0.0002），最低5元，买卖双向
  - 印花税：万5（0.0005），仅卖出；2023-08-28 起由千1减半
  - 过户费：万0.1（0.00001），买卖双向
  - 滑点：万2（0.0002），买卖双向
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.execution.base import ExecutionInterface
from myquant.execution.state_machine import OrderStateMachine
from myquant.models.backtest import CommissionModel, SlippageModel
from myquant.models.market_data import Bar
from myquant.models.trade import Order, OrderSide, Position, Trade

logger = get_logger(__name__)


class SimBroker(ExecutionInterface):
    """
    回测模拟券商（Simulated Broker）

    核心功能:
      - 实现 ExecutionInterface 全部接口
      - 管理订单簿（order book）
      - 管理持仓与资金
      - on_bar(bar) 撮合订单（市价单/限价单）
      - 计算滑点、佣金、印花税、过户费
      - 支持部分成交
      - 订单状态由 OrderStateMachine 管理

    使用方式:
        broker = SimBroker(initial_capital=1000000)
        broker.connect()
        order = Order(symbol="600519.SH", side=BUY, order_type=MARKET, quantity=100)
        broker.submit_order(order)
        trades = broker.on_bar(bar)  # 下一根bar撮合
        account = broker.get_account()
        positions = broker.get_positions()
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        commission_model: Optional[CommissionModel] = None,
        slippage_model: Optional[SlippageModel] = None,
        enable_partial_fill: bool = False,
        participation_rate: float = 1.0,
        price_limit_pct: float = 0.10,
        enforce_limit: bool = False,
        is_suspended: Optional[Callable[[str], bool]] = None,
        enable_t1: bool = True,
        lot_size: int = 0,
    ):
        """
        初始化模拟券商

        参数:
            initial_capital: 初始资金
            commission_model: 手续费模型（默认A股写实标准）
            slippage_model: 滑点模型（默认固定2基点）
            enable_partial_fill: 是否启用部分成交（默认全量成交，简化回测）
            participation_rate: 成交量参与率（0<rate<1 时按 bar.volume*rate 限成交，剩余走 partial_fill；默认 1.0 全量成交）
            enable_t1: 是否启用 T+1 持仓冻结（A股标准，当日买入不可当日卖出；P1-08）
            lot_size: 整手股数（P0-03 续卡 #348：>0 时**买入**按 lot_size 向下取整，
                不足一手不成交——A股整手约束；卖出不限制（零股可卖）；0 = 不限制
                （向后兼容，未装配整手约束的调用方行为不变）。
        """
        super().__init__(name="sim_broker")
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)       # 总资金
        self.frozen_cash = 0.0                     # 冻结资金（挂单中）
        self.positions: Dict[str, Position] = {}  # symbol -> Position
        self.orders: Dict[str, Order] = {}         # order_id -> Order
        self.trades: List[Trade] = []              # 成交记录
        self.commission_model = commission_model or CommissionModel()
        self.slippage_model = slippage_model or SlippageModel()
        # P2-01：费用统一委托 fee_common.FeeCalculator（与 mixed/vectorized 同一唯一入口）。
        # 延迟到构造函数内 import：避免执行层(sim_broker)顶层拉入 backtest→engine→risk
        # 包链（backtest/__init__ 聚合 batch_backtest→mixed_engine→risk.rules），
        # 守护 R5"应急链不依赖 risk/engine"红线；仅 SimBroker 真正构造时才加载。
        from myquant.backtest.fee_common import FeeCalculator

        self.fee_calculator = FeeCalculator(
            commission_model=self.commission_model,
            slippage_model=self.slippage_model,
        )
        self.enable_partial_fill = enable_partial_fill
        self.participation_rate = float(participation_rate)  # KP-P2-03: 成交量参与率
        self.price_limit_pct = float(price_limit_pct)   # KP-P2-02: 涨跌停幅度
        self.enforce_limit = bool(enforce_limit)        # KP-P2-02: 是否强制涨跌停校验（event 路径开启）
        self.is_suspended = is_suspended                # KP-P2-02: 停牌判定函数
        self._prev_close: Dict[str, float] = {}        # KP-P2-02: 昨收基准（上一bar收盘）
        self.enable_t1 = bool(enable_t1)                # P1-08: T+1 持仓冻结开关
        self._t1_holdings: Dict[str, float] = {}       # P1-08: 当日已成交买入冻结（当日买入不可卖）
        self.lot_size: int = int(lot_size)              # P0-03 续卡 #348: 整手约束
        self.state_machine = OrderStateMachine()
        self._market_prices: Dict[str, float] = {}  # 最新市场价
        logger.info(
            f"模拟券商初始化: 初始资金={initial_capital:,.0f}, "
            f"佣金率={self.commission_model.buy_rate}, "
            f"滑点={self.slippage_model.fixed_bps}bps"
        )

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """连接（模拟券商直接成功）"""
        self._connected = True
        logger.info("模拟券商已连接")
        return True

    def disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        logger.info("模拟券商已断开")

    # ------------------------------------------------------------------
    # 订单操作（实现 ExecutionInterface）
    # ------------------------------------------------------------------

    def submit_order(self, order: Order) -> Order:
        """
        提交订单

        - 校验订单合法性
        - 资金检查（买入冻结资金）
        - 注册订单到订单簿
        - 状态: PENDING → SUBMITTED

        参数:
            order: 订单对象

        返回:
            Order: 订单（状态已更新）
        """
        # 校验订单
        errors = order.validate()
        if errors:
            logger.error(f"订单校验失败: {errors}, order={order}")
            order.reject("; ".join(errors))
            return order

        # 资金检查（买入）
        if order.side == OrderSide.BUY:
            estimated_cost = order.quantity * (order.price or 0)
            if estimated_cost > self.available_cash:
                logger.warning(
                    f"订单资金不足: {order.symbol} 需要={estimated_cost:.2f}, "
                    f"可用={self.available_cash:.2f}"
                )
                order.reject("资金不足")
                return order
            # 冻结资金
            self.frozen_cash += estimated_cost

        # 注册订单
        self.orders[order.order_id] = order

        # 状态转换: PENDING → SUBMITTED
        self.state_machine.submit(order)

        logger.debug(f"订单已提交: {order}")
        return order

    def cancel_order(self, order_id: str) -> bool:
        """
        撤销订单

        - 释放冻结资金（买入订单）
        - 状态: SUBMITTED/PARTIAL_FILLED → CANCELLED

        参数:
            order_id: 订单ID

        返回:
            True=撤销成功
        """
        order = self.orders.get(order_id)
        if order is None:
            logger.warning(f"撤销订单失败: 订单不存在 order_id={order_id}")
            return False

        if not order.status.is_active:
            logger.warning(f"撤销订单失败: 订单非活跃 status={order.status}")
            return False

        # 释放冻结资金（买入订单未成交部分）
        if order.side == OrderSide.BUY:
            remaining = order.remaining_qty
            estimated_frozen = remaining * (order.price or 0)
            self.frozen_cash = max(0.0, self.frozen_cash - estimated_frozen)

        # 状态转换
        self.state_machine.cancel(order)
        logger.debug(f"订单已撤销: {order}")
        return True

    def get_order(self, order_id: str) -> Optional[Order]:
        """查询订单详情"""
        return self.orders.get(order_id)

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """查询活跃订单"""
        orders = [o for o in self.orders.values() if o.status.is_active]
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders

    # ------------------------------------------------------------------
    # 资金与持仓查询（实现 ExecutionInterface）
    # ------------------------------------------------------------------

    def get_account(self) -> Dict[str, Any]:
        """查询账户资金"""
        market_value = self._calc_market_value()
        return {
            "cash": round(self.cash, 2),
            "available_cash": round(self.available_cash, 2),
            "frozen_cash": round(self.frozen_cash, 2),
            "market_value": round(market_value, 2),
            "total_assets": round(self.cash + market_value, 2),
        }

    def get_positions(self) -> List[Position]:
        """查询当前持仓"""
        return [pos for pos in self.positions.values() if pos.quantity != 0]

    def get_position(self, symbol: str) -> Optional[Position]:
        """查询指定标的持仓"""
        return self.positions.get(symbol)

    def get_trades(self, symbol: Optional[str] = None) -> List[Trade]:
        """查询成交记录"""
        trades = self.trades
        if symbol:
            trades = [t for t in trades if t.symbol == symbol]
        return trades

    # ------------------------------------------------------------------
    # 核心：撮合（on_bar）
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> List[Trade]:
        """
        新bar到来时撮合所有匹配的活跃订单

        ★★★ T+1成交 ★★★
        上一根bar创建的订单，在当前bar用开盘价/高低价撮合。
        由引擎层控制调用时机（T日创建订单，T+1日调用on_bar）。

        撮合规则:
          - 市价单(MARKET): 以当前bar开盘价成交
          - 限价单(LIMIT):
            - 买入: 限价 >= 当日最低价 → 成交（成交价=min(限价,开盘价)）
            - 卖出: 限价 <= 当日最高价 → 成交（成交价=max(限价,开盘价)）

        参数:
            bar: 新K线数据

        返回:
            List[Trade]: 本bar产生的成交记录
        """
        # KP-P2-02: 记录昨收基准（上一bar收盘，当前尚未更新为 bar.close）
        if self.enforce_limit:
            self._prev_close[bar.symbol] = self._market_prices.get(bar.symbol, 0.0)
        # 更新市场价
        self._market_prices[bar.symbol] = bar.close
        pos = self.positions.get(bar.symbol)
        if pos:
            pos.update_market_price(bar.close)

        # 获取当前标的的活跃订单
        active_orders = self.get_active_orders(symbol=bar.symbol)
        if not active_orders:
            return []

        trades: List[Trade] = []
        for order in active_orders:
            trade = self._match_order(order, bar)
            if trade:
                trades.append(trade)

        return trades

    def _price_limit_check(self, order: Order, bar: Bar) -> Optional[str]:
        """涨跌停/停牌前置检查（KP-P2-02）。

        涨停价 = 昨收×(1+幅度)，跌停价 = 昨收×(1-幅度)。
        涨停时买单（挂涨停价买不到）拒绝；跌停时卖单拒绝；停牌跳过。
        未启用 enforce_limit 或缺昨收时返回 None（不拦截）。
        """
        if not self.enforce_limit:
            return None
        if self.is_suspended is not None and self.is_suspended(order.symbol):
            return "停牌"
        pc = self._prev_close.get(order.symbol, 0.0)
        if pc <= 0 or self.price_limit_pct <= 0:
            return None
        # P2-04 盘中触板建模：用 high/low（非 open），盘中触涨停即拒买、触跌停即拒卖
        if order.side == OrderSide.BUY and bar.high >= pc * (1 + self.price_limit_pct):
            return "涨停"
        if order.side == OrderSide.SELL and bar.low <= pc * (1 - self.price_limit_pct):
            return "跌停"
        return None

    def _match_order(self, order: Order, bar: Bar) -> Optional[Trade]:
        """
        撮合单个订单

        参数:
            order: 订单
            bar: 当前bar

        返回:
            Trade: 成交记录（未成交返回None）
        """
        from myquant.models.trade import OrderType

        # KP-P2-02: 涨停拒买/跌停拒卖/停牌跳过（event 路径；mixed/vectorized 走 OrderMatcher 已有判定）
        reject = self._price_limit_check(order, bar)
        if reject:
            logger.debug(f"[sim_broker] {reject}拒单（KP-P2-02）: {order.symbol} {order.side.value}")
            self.cancel_order(order.order_id)
            return None

        if order.order_type == OrderType.MARKET:
            fill_price = bar.open
        elif order.order_type == OrderType.LIMIT:
            if order.side == OrderSide.BUY:
                # 买入限价单: 限价 >= 最低价才成交
                if order.price is None or order.price < bar.low:
                    return None
                fill_price = min(order.price, bar.open)
            else:
                # 卖出限价单: 限价 <= 最高价才成交
                if order.price is None or order.price > bar.high:
                    return None
                fill_price = max(order.price, bar.open)
        else:
            # STOP/STOP_LIMIT/TRAILING_STOP 暂不支持（简化回测）
            logger.debug(f"不支持的订单类型: {order.order_type}")
            return None

        # 应用滑点
        fill_price = self._apply_slippage(fill_price, order.side, bar)

        # 成交数量（默认全量成交；KP-P2-03 支持按参与率限制成交量）
        # 0<rate<1 时 fill_qty = min(remaining, int(bar.volume*participation_rate))，剩余量走 partial_fill
        if 0.0 < self.participation_rate < 1.0:
            fill_qty = min(order.remaining_qty, int(bar.volume * self.participation_rate))
            fill_qty = max(int(fill_qty), 0)
        else:
            fill_qty = order.remaining_qty
        # P0-03 续卡（#348）：整手执行——买入按 lot_size 向下取整（不足一手不成交，
        # A股整手约束）；卖出不限制（零股可卖）。lot_size=0 时不限制（向后兼容）。
        if order.side == OrderSide.BUY and self.lot_size > 0:
            fill_qty = float(int(fill_qty // self.lot_size) * self.lot_size)
        if fill_qty <= 0:
            return None  # 参与率限制/整手截断下无可成交数量，保留订单待后续 bar

        # P1-08: T+1 持仓冻结（卖出时可用量 = 持仓 - 当日买入冻结）
        if order.side == OrderSide.SELL and self.enable_t1:
            pos = self.positions.get(order.symbol)
            if pos is not None:
                frozen = self._t1_holdings.get(order.symbol, 0.0)
                available = pos.quantity - frozen
                if fill_qty > available:
                    if available <= 0:
                        logger.debug(
                            f"[sim_broker] T+1 冻结拒单: {order.symbol} "
                            f"持仓={pos.quantity:.0f} 冻结={frozen:.0f} 可用=0"
                        )
                        return None
                    fill_qty = int(available)
                    logger.debug(
                        f"[sim_broker] T+1 截断: {order.symbol} 请求={order.remaining_qty:.0f} "
                        f"可用={available:.0f}（冻结={frozen:.0f}）"
                    )

        # 创建成交记录
        trade = Trade(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_qty,
            price=fill_price,
            timestamp=bar.timestamp,
            strategy_id=order.strategy_id,
        )

        # 计算手续费
        trade.commission = self._calc_commission(trade)

        # 计算滑点金额
        trade.slippage = self._calc_slippage_amount(fill_price, fill_qty, bar)

        # 更新订单状态
        self.state_machine.fill(order, fill_qty, fill_price)

        # 更新持仓与资金
        self._apply_trade(trade, order)

        # 注册成交
        self.trades.append(trade)

        logger.debug(
            f"模拟成交: {order.symbol} {order.side.value} "
            f"{fill_qty:.0f}@{fill_price:.2f} "
            f"手续费={trade.commission:.2f} 滑点={trade.slippage:.2f}"
        )

        return trade

    # ------------------------------------------------------------------
    # 滑点与手续费计算
    # ------------------------------------------------------------------

    def _apply_slippage(self, price: float, side: OrderSide, bar: Bar) -> float:
        """
        应用滑点到成交价

        买入滑点: 成交价上移（更贵）
        卖出滑点: 成交价下移（更便宜）

        参数:
            price: 原始成交价
            side: 订单方向
            bar: 当前bar

        返回:
            含滑点的成交价
        """
        slippage_per_share = self.slippage_model.calc(
            price=price,
            volume=bar.volume,
            order_size=0,  # 简化：不考虑量价冲击
        )
        if side == OrderSide.BUY:
            return price + slippage_per_share
        else:
            return price - slippage_per_share

    def _calc_slippage_amount(self, price: float, quantity: float, bar: Bar) -> float:
        """计算滑点金额"""
        slippage_per_share = self.slippage_model.calc(price=price, volume=bar.volume)
        return slippage_per_share * quantity

    def _calc_commission(self, trade: Trade) -> float:
        """
        计算手续费（A股写实标准）

        买入: 佣金(万2,最低5元) + 过户费(万0.1)
        卖出: 佣金(万2,最低5元) + 印花税(万5) + 过户费(万0.1)

        P2-01：委托 fee_common.FeeCalculator 唯一入口（与 mixed/vectorized 同实现），
        返回佣金+印花税+过户费合计；与 CommissionModel.calc_buy/calc_sell 数学等价。
        """
        amount = trade.notional  # 成交金额 = quantity * price
        commission, stamp_tax, transfer_fee = self.fee_calculator.calc_commission_breakdown(
            side=trade.side, notional=amount
        )
        return commission + stamp_tax + transfer_fee

    # ------------------------------------------------------------------
    # 持仓与资金更新
    # ------------------------------------------------------------------

    def _apply_trade(self, trade: Trade, order: Order) -> None:
        """
        应用成交到持仓和资金

        买入: 扣除资金（成交金额+手续费），增加持仓
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
            # P1-08: T+1 冻结累计（当日买入不可当日卖出）
            if self.enable_t1:
                self._t1_holdings[symbol] = self._t1_holdings.get(symbol, 0.0) + trade.quantity
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

    def settle_t1(self) -> None:
        """P1-08: T+1 日终结算——清空当日买入冻结记录。

        日终/次日开盘前调用（由引擎层控制时机），释放当日买入的冻结数量，
        使次日起该部分持仓可卖出。防未来：只释放【已发生】的当日买入。
        """
        if not self.enable_t1:
            return
        released = dict(self._t1_holdings)
        self._t1_holdings.clear()
        if released:
            logger.info(f"[sim_broker] T+1 结算完成，释放冻结: {released}")

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def available_cash(self) -> float:
        """可用资金 = 总资金 - 冻结资金"""
        return max(0.0, self.cash - self.frozen_cash)

    @property
    def total_assets(self) -> float:
        """总资产 = 资金 + 持仓市值"""
        return self.cash + self._calc_market_value()

    @property
    def equity(self) -> float:
        """净值（相对初始资金）"""
        if self.initial_capital <= 0:
            return 1.0
        return self.total_assets / self.initial_capital

    @property
    def position_count(self) -> int:
        """持仓标的数量"""
        return len([p for p in self.positions.values() if p.quantity != 0])

    # ------------------------------------------------------------------
    # 重置
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """重置模拟券商到初始状态（回测重启时用）"""
        self.cash = self.initial_capital
        self.frozen_cash = 0.0
        self.positions.clear()
        self.orders.clear()
        self.trades.clear()
        self._market_prices.clear()
        self.state_machine = OrderStateMachine()
        logger.info("模拟券商已重置")

    def __repr__(self) -> str:
        return (
            f"SimBroker(cash={self.cash:,.0f}, "
            f"positions={self.position_count}, "
            f"orders={len(self.orders)}, "
            f"trades={len(self.trades)}, "
            f"equity={self.equity:.4f})"
        )


def make_suspended_lookup(suspended_symbols: Optional[set] = None) -> Callable[[str], bool]:
    """构造停牌判断函数（P0-03 is_suspended 接真值）。

    根据传入的停牌股票集合返回一个闭包，供 SimBroker(is_suspended=...) 使用。
    空集合/None → 始终返回 False（不拦截任何股票，向后兼容）。

    Args:
        suspended_symbols: 停牌股票代码集合（如 {"600519.SH"}）；None/空集 = 不拦截。

    Returns:
        Callable[[str], bool]：输入 symbol，返回是否停牌。
    """
    halted = frozenset(suspended_symbols or set())

    def _lookup(symbol: str) -> bool:
        return symbol in halted

    return _lookup
