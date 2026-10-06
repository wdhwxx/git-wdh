# 文件名：myquant/risk/rules.py
"""
rules.py - 风控规则库
======================
定义各类风控规则，用于订单前置检查、持仓监控、账户风险控制。

核心能力:
  1. 仓位限制 — 单票最大仓位、最大持仓数
  2. 单笔限额 — 单笔订单最大金额
  3. 最大回撤 — 账户最大回撤限制
  4. 止损规则 — 个股止损、禁止加仓
  5. 止盈规则 — 个股止盈、禁止加仓（防追高）
  6. 流控规则 — 下单频率限制
  7. 涨跌停规则 — 涨跌停不能买入/卖出
  8. 交易时段 — 非交易时段不能下单
  9. 黑名单规则 — 禁止交易的标的
  10. 资金可用性 — 可用资金检查

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（规则可配置、可扩展、可热加载）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前已知的账户/持仓/行情数据

防未来函数说明:
  - 所有规则只使用当前已知的账户状态、持仓数据、行情价格
  - 不使用任何未来bar的数据或预判未来走势
  - 回撤计算基于已实现的历史净值，不包含未来数据
  - 止损/止盈基于当前市场价，不预判未来价格

v1实现范围:
  - 10类核心风控规则
  - 规则库统一管理（添加/删除/检查/配置加载）
  - 风控检查结果标准化
  - 配置驱动（可从dict加载）

TODO(后续迭代):
  - 风控规则与DataQualityEvent联动（脏数据严重时暂停策略）
  - 动态风控阈值（根据波动率自动调整）
  - 多账户风控隔离
  - 风控规则热加载（运行时修改配置）
  - 风控事件审计日志
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from typing import Any, Callable, Dict, List, Optional, Set

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config
from myquant.models.trade import Order, OrderSide

logger = get_logger("risk.rules")


# ═══════════════════════════════════════════════
#  风控检查结果
# ═══════════════════════════════════════════════

@dataclass
class RiskCheckResult:
    """
    风控检查结果

    Attributes
    ----------
    passed    : 是否通过
    rule_name : 规则名称
    reason    : 拒绝原因（通过时为空）
    details   : 详细信息（用于审计和调试）
    """
    passed: bool
    rule_name: str
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        status = "PASS" if self.passed else "REJECT"
        return f"[{status}] {self.rule_name}: {self.reason or '通过'}"


# ═══════════════════════════════════════════════
#  风控上下文
# ═══════════════════════════════════════════════

@dataclass
class RiskContext:
    """
    风控检查上下文

    传递给风控规则的环境信息，包含账户、持仓、行情等当前已知数据。
    """
    cash: float = 0.0
    total_assets: float = 0.0
    initial_capital: float = 0.0
    positions: List[Dict[str, Any]] = field(default_factory=list)
    position_count: int = 0
    peak_assets: float = 0.0
    current_price: Dict[str, float] = field(default_factory=dict)
    order_count_today: int = 0
    last_order_time: float = 0.0
    trading_date: str = ""
    day_start_assets: float = 0.0  # 当日开盘时点资产快照（每日亏损基准，任务46/P1）
    extra: Dict[str, Any] = field(default_factory=dict)
    # KP-P3-06 异常交易类监控字段
    pending_orders: List[Dict[str, Any]] = field(default_factory=list)  # 活跃未成交单（自成交防范）
    cancel_count_today: int = 0        # 当日撤单数
    new_order_count_today: int = 0     # 当日新下单数

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取指定标的持仓"""
        for pos in self.positions:
            if pos.get("symbol") == symbol:
                return pos
        return None

    def get_position_value(self, symbol: str) -> float:
        """获取指定标的持仓市值"""
        pos = self.get_position(symbol)
        if not pos:
            return 0.0
        return abs(pos.get("quantity", 0)) * pos.get("market_price", 0)

    @property
    def drawdown(self) -> float:
        """当前回撤（0~1，0=无回撤）"""
        if self.peak_assets <= 0:
            return 0.0
        return max(0.0, (self.peak_assets - self.total_assets) / self.peak_assets)

    @property
    def daily_loss_pct(self) -> float:
        """
        当日亏损比例（0~1，0=当日未亏损）

        以当日开盘时点资产快照为基准：当前资产低于日初 → 亏损比例；
        当前资产不低于日初（持平/盈利）→ 0（只算负向；防未来：只依据
        已发生的事实——日初快照与当前资产）。
        """
        if self.day_start_assets <= 0:
            return 0.0
        return max(0.0, (self.day_start_assets - self.total_assets) / self.day_start_assets)


# ═══════════════════════════════════════════════
#  风控规则基类
# ═══════════════════════════════════════════════

class RiskRule(ABC):
    """
    风控规则抽象基类

    所有风控规则必须继承此类，实现 check 方法。
    规则是无状态的，所有状态通过 RiskContext 传递。
    """

    name: str = "base_rule"
    description: str = ""

    def __init__(self, enabled: bool = True, **params: Any):
        self.enabled = enabled
        self.params = params

    @abstractmethod
    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        """执行风控检查"""
        pass

    def _pass(self, **details) -> RiskCheckResult:
        """生成通过结果"""
        return RiskCheckResult(passed=True, rule_name=self.name, details=details)

    def _reject(self, reason: str, **details) -> RiskCheckResult:
        """生成拒绝结果"""
        return RiskCheckResult(passed=False, rule_name=self.name, reason=reason, details=details)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(enabled={self.enabled})"


# ═══════════════════════════════════════════════
#  规则1：仓位限制
# ═══════════════════════════════════════════════

class PositionLimitRule(RiskRule):
    """仓位限制规则：单票最大仓位比例、最大持仓数"""
    name = "position_limit"
    description = "仓位限制：单票最大仓位、最大持仓数"

    def __init__(self, max_position_pct: float = 0.30, max_positions: int = 5,
                 max_quantity_per_symbol: float = 0, **params):
        super().__init__(**params)
        self.max_position_pct = max_position_pct
        self.max_positions = max_positions
        self.max_quantity_per_symbol = max_quantity_per_symbol

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        if order.side != OrderSide.BUY:
            return self._pass()

        # 最大持仓数
        current_positions = context.position_count
        has_position = context.get_position(order.symbol) is not None
        if not has_position and current_positions >= self.max_positions:
            return self._reject(
                f"持仓数已满: 当前{current_positions}只, 上限{self.max_positions}只")

        # 单票最大仓位
        if context.total_assets > 0:
            current_value = context.get_position_value(order.symbol)
            price = order.price or context.current_price.get(order.symbol, 0)
            order_value = order.quantity * price
            new_value = current_value + order_value
            new_pct = new_value / context.total_assets
            if new_pct > self.max_position_pct:
                return self._reject(
                    f"单票仓位超限: 买入后{new_pct*100:.1f}%, 上限{self.max_position_pct*100:.1f}%")

        # 单票最大持仓数量
        if self.max_quantity_per_symbol > 0:
            pos = context.get_position(order.symbol)
            current_qty = pos.get("quantity", 0) if pos else 0
            if current_qty + order.quantity > self.max_quantity_per_symbol:
                return self._reject("单票持仓数超限")

        return self._pass()


# ═══════════════════════════════════════════════
#  规则2：单笔限额
# ═══════════════════════════════════════════════

class SingleOrderLimitRule(RiskRule):
    """单笔订单限额规则：防止误操作下大单"""
    name = "single_order_limit"
    description = "单笔订单最大金额限制"

    def __init__(self, max_order_amount: float = 100_000, **params):
        super().__init__(**params)
        self.max_order_amount = max_order_amount

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        price = order.price or context.current_price.get(order.symbol, 0)
        order_amount = order.quantity * price
        if order_amount > self.max_order_amount:
            return self._reject(
                f"单笔金额超限: {order_amount:,.0f}元, 上限{self.max_order_amount:,.0f}元")
        return self._pass(order_amount=order_amount)


# ═══════════════════════════════════════════════
#  规则3：最大回撤
# ═══════════════════════════════════════════════

class MaxDrawdownRule(RiskRule):
    """最大回撤规则：回撤超阈值禁止新开仓"""
    name = "max_drawdown"
    description = "账户最大回撤限制，超阈值禁止新开仓"

    def __init__(self, max_drawdown_pct: float = 0.20, **params):
        super().__init__(**params)
        self.max_drawdown_pct = max_drawdown_pct

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        if order.side != OrderSide.BUY:
            return self._pass()
        drawdown = context.drawdown
        if drawdown > self.max_drawdown_pct:
            return self._reject(
                f"账户回撤超限: 当前{drawdown*100:.1f}%, 上限{self.max_drawdown_pct*100:.1f}%, 禁止新开仓")
        return self._pass(drawdown=drawdown)


# ═══════════════════════════════════════════════
#  规则3.5：单日亏损上限（任务46 / P1）
# ═══════════════════════════════════════════════

class DailyLossLimitRule(RiskRule):
    """单日亏损上限规则：当日账户亏损超阈值禁止新开仓（次日自动归零重置）"""
    name = "daily_loss_limit"
    description = "单日亏损上限：当日亏损超阈值禁止新开仓"

    def __init__(self, max_daily_loss_pct: float = 0.01, **params):
        super().__init__(**params)
        self.max_daily_loss_pct = max_daily_loss_pct

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        if order.side != OrderSide.BUY:
            return self._pass()
        daily_loss = context.daily_loss_pct
        if daily_loss > self.max_daily_loss_pct:
            return self._reject(
                f"当日亏损超限: 当前{daily_loss*100:.2f}%, "
                f"上限{self.max_daily_loss_pct*100:.1f}%, 禁止新开仓（次日自动重置）")
        return self._pass(daily_loss=daily_loss)


# ═══════════════════════════════════════════════
#  规则4：个股止损
# ═══════════════════════════════════════════════

class StopLossRule(RiskRule):
    """个股止损规则：亏损超阈值禁止加仓（只做前置检查，不自动止损）"""
    name = "stop_loss"
    description = "个股止损：亏损超阈值禁止加仓"

    def __init__(self, max_loss_pct: float = 0.10, **params):
        super().__init__(**params)
        self.max_loss_pct = max_loss_pct

    def stop_price(self, avg_cost: float) -> float:
        """止损触发价（KP-P3-07：与实盘持仓止损阈值同一口径，回测/混合引擎复用）。"""
        return avg_cost * (1.0 - self.max_loss_pct)

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        if order.side != OrderSide.BUY:
            return self._pass()
        pos = context.get_position(order.symbol)
        if not pos:
            return self._pass()
        avg_cost = pos.get("avg_cost", 0)
        market_price = pos.get("market_price", 0)
        if avg_cost <= 0 or market_price <= 0:
            return self._pass()
        if pos.get("quantity", 0) > 0:
            loss_pct = (avg_cost - market_price) / avg_cost
            if loss_pct > self.max_loss_pct:
                return self._reject(
                    f"个股亏损超限: {order.symbol} 亏损{loss_pct*100:.1f}%, "
                    f"上限{self.max_loss_pct*100:.1f}%, 禁止加仓")
        return self._pass()


# ═══════════════════════════════════════════════
#  规则5：个股止盈（防追高）
# ═══════════════════════════════════════════════

class TakeProfitRule(RiskRule):
    """个股止盈规则：盈利超阈值禁止加仓（防追高）"""
    name = "take_profit"
    description = "个股止盈：盈利超阈值禁止加仓（防追高）"

    def __init__(self, max_profit_pct: float = 0.50, **params):
        super().__init__(**params)
        self.max_profit_pct = max_profit_pct

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        if order.side != OrderSide.BUY:
            return self._pass()
        pos = context.get_position(order.symbol)
        if not pos:
            return self._pass()
        avg_cost = pos.get("avg_cost", 0)
        market_price = pos.get("market_price", 0)
        if avg_cost <= 0 or market_price <= 0:
            return self._pass()
        if pos.get("quantity", 0) > 0:
            profit_pct = (market_price - avg_cost) / avg_cost
            if profit_pct > self.max_profit_pct:
                return self._reject(
                    f"个股盈利超限: {order.symbol} 盈利{profit_pct*100:.1f}%, "
                    f"上限{self.max_profit_pct*100:.1f}%, 禁止加仓（防追高）")
        return self._pass()


# ═══════════════════════════════════════════════
#  规则6：流控
# ═══════════════════════════════════════════════

class RateLimitRule(RiskRule):
    """下单频率限制规则：每日最大下单数、最小间隔"""
    name = "rate_limit"
    description = "下单频率限制：每日最大下单数、最小间隔"

    def __init__(self, max_orders_per_day: int = 100, min_interval_seconds: float = 1.0, **params):
        super().__init__(**params)
        self.max_orders_per_day = max_orders_per_day
        self.min_interval_seconds = min_interval_seconds

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        # P1-01修复：每日最大下单数只约束开仓（BUY）；平仓/止损卖单（SELL）不受其限，
        # 防止累计到上限后连止损卖单都被拒绝、持仓无法退出（真钱安全红线）。
        # 最小间隔检查仍对所有方向生效。
        if order.side == OrderSide.BUY and context.order_count_today >= self.max_orders_per_day:
            return self._reject(
                f"今日下单数超限: {context.order_count_today}笔, 上限{self.max_orders_per_day}笔")
        now = time.time()
        elapsed = now - context.last_order_time
        if context.last_order_time > 0 and elapsed < self.min_interval_seconds:
            return self._reject(
                f"下单间隔过短: {elapsed:.2f}秒, 最小间隔{self.min_interval_seconds}秒")
        return self._pass()


# ═══════════════════════════════════════════════
#  规则7：涨跌停限制
# ═══════════════════════════════════════════════

class PriceLimitRule(RiskRule):
    """涨跌停限制规则：涨停不能买，跌停不能卖"""
    name = "price_limit"
    description = "涨跌停限制：涨停不能买，跌停不能卖"

    def __init__(self, limit_pct: float = 0.10, **params):
        super().__init__(**params)
        self.limit_pct = limit_pct

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        current_price = context.current_price.get(order.symbol, 0)
        prev_close = context.extra.get("prev_close", {}).get(order.symbol, 0)
        if current_price <= 0 or prev_close <= 0:
            # KP-P3-04：缺价 fail-closed（无法判定涨跌停 → 拒绝下单，而非放行）
            return self._reject(
                f"价格信息不足，拒绝下单（fail-closed）: {order.symbol} "
                f"current_price={current_price} prev_close={prev_close}"
            )
        change_pct = (current_price - prev_close) / prev_close
        if order.side == OrderSide.BUY and change_pct >= self.limit_pct:
            return self._reject(f"涨停不能买入: {order.symbol} 涨幅{change_pct*100:.1f}%")
        if order.side == OrderSide.SELL and change_pct <= -self.limit_pct:
            return self._reject(f"跌停不能卖出: {order.symbol} 跌幅{change_pct*100:.1f}%")
        return self._pass(change_pct=change_pct)


# ═══════════════════════════════════════════════
#  规则8：交易时段限制
# ═══════════════════════════════════════════════

#: 交易日历缓存（模块级懒加载，避免重复读盘；KP-P3-10）
_TRADING_CALENDAR_CACHE: Optional[Set[date]] = None


def _load_trading_calendar_cache() -> Optional[Set[date]]:
    """懒加载真实交易日历（data/trade_calendar.csv）；失败回退 None（工作日近似）。"""
    global _TRADING_CALENDAR_CACHE
    if _TRADING_CALENDAR_CACHE is None:
        try:
            from pathlib import Path as _Path

            from myquant.utils.trade_calendar import load_trade_calendar
            root = _Path(__file__).resolve().parent.parent.parent
            _TRADING_CALENDAR_CACHE = load_trade_calendar(root)
        except Exception:
            _TRADING_CALENDAR_CACHE = None
    return _TRADING_CALENDAR_CACHE


class TradingHoursRule(RiskRule):
    """交易时段限制规则：A股交易时段外禁止下单"""
    name = "trading_hours"
    description = "交易时段限制：非交易时段禁止下单"

    def __init__(self, morning_start: str = "09:30", morning_end: str = "11:30",
                 afternoon_start: str = "13:00", afternoon_end: str = "15:00",
                 trading_calendar: Optional[Set[date]] = None, **params):
        super().__init__(**params)
        self.morning_start = self._parse_time(morning_start)
        self.morning_end = self._parse_time(morning_end)
        self.afternoon_start = self._parse_time(afternoon_start)
        self.afternoon_end = self._parse_time(afternoon_end)
        self.trading_calendar = trading_calendar  # 显式注入优先；None 时懒加载缓存

    @staticmethod
    def _parse_time(time_str: str) -> dtime:
        h, m = map(int, time_str.split(":"))
        return dtime(hour=h, minute=m)

    def _is_trading_day(self, d: date) -> bool:
        """是否交易日：显式日历优先；否则懒加载真实日历；再回退工作日近似（KP-P3-10）。"""
        cal = self.trading_calendar
        if cal is None:
            cal = _load_trading_calendar_cache()
        if cal is not None:
            return d in cal
        return d.weekday() < 5

    def _is_trading_time(self, now: Optional[datetime] = None) -> bool:
        if now is None:
            now = datetime.now()
        current = now.time()
        return (self.morning_start <= current <= self.morning_end or
                self.afternoon_start <= current <= self.afternoon_end)

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        now = context.extra.get("current_time")
        if now and isinstance(now, str):
            now = datetime.strptime(now, "%H:%M:%S")
        now_dt = now or datetime.now()
        # 交易日维度：用真实当前日期（current_time 仅盘中时钟 HH:MM:SS，不承载日期）
        if not self._is_trading_day(datetime.now().date()):
            return self._reject(f"非交易日: {datetime.now().date().isoformat()}")
        if not self._is_trading_time(now):
            current_time = (now_dt).strftime("%H:%M:%S")
            return self._reject(f"非交易时段: 当前{current_time}")
        return self._pass()


# ═══════════════════════════════════════════════
#  规则9：黑名单
# ═══════════════════════════════════════════════

class BlacklistRule(RiskRule):
    """黑名单规则：禁止交易指定标的"""
    name = "blacklist"
    description = "黑名单：禁止交易指定标的"

    def __init__(self, blacklist: Optional[Set[str]] = None, **params):
        super().__init__(**params)
        self.blacklist: Set[str] = set(blacklist or set())

    def add_symbol(self, symbol: str) -> None:
        self.blacklist.add(symbol)

    def remove_symbol(self, symbol: str) -> None:
        self.blacklist.discard(symbol)

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        # P1-18：黑名单只禁止买入/新开仓，允许卖出平仓（否则持仓标的入黑名单即困仓无法卖出）
        from myquant.models.trade import OrderSide
        if order.symbol in self.blacklist and order.side == OrderSide.BUY:
            return self._reject(f"标的在黑名单中（禁止买入）: {order.symbol}")
        return self._pass()


# ═══════════════════════════════════════════════
#  规则10：资金可用性
# ═══════════════════════════════════════════════

class CashAvailabilityRule(RiskRule):
    """资金可用性规则：买入金额不超过可用资金"""
    name = "cash_availability"
    description = "资金可用性：买入金额不超过可用资金"

    def __init__(self, min_cash_reserve: float = 0.0, **params):
        super().__init__(**params)
        self.min_cash_reserve = min_cash_reserve

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        if order.side != OrderSide.BUY:
            return self._pass()
        price = order.price or context.current_price.get(order.symbol, 0)
        order_amount = order.quantity * price
        estimated_commission = max(5.0, order_amount * 0.00025)
        total_needed = order_amount + estimated_commission + self.min_cash_reserve
        if total_needed > context.cash:
            return self._reject(
                f"可用资金不足: 需要{total_needed:,.0f}元, 可用{context.cash:,.0f}元")
        return self._pass(needed=total_needed, available=context.cash)


# ═══════════════════════════════════════════════
#  规则注册表
# ═══════════════════════════════════════════════

# ═══════════════════════════════════════════════
#  异常交易类风控（KP-P3-06）
# ═══════════════════════════════════════════════

class SelfTradeRule(RiskRule):
    """自成交防范：同标的反向未成交单互斥（异常交易类风控）。

    同一标的已存在反向未成交单时拒绝同向新单，避免自成交/对敲。
    防未来：只基于当前已存在的未成交单（context.pending_orders）。
    """
    name = "self_trade"
    description = "自成交防范：同标的反向未成交单互斥"

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        opposite_side = OrderSide.SELL if order.side == OrderSide.BUY else OrderSide.BUY
        for po in context.pending_orders:
            if po.get("symbol") != order.symbol:
                continue
            raw = po.get("side")
            po_side = raw.value if hasattr(raw, "value") else raw
            if str(po_side).upper() == opposite_side.value:
                return self._reject(
                    f"自成交防范：{order.symbol} 存在反向未成交单，拒绝同向互斥下单"
                )
        return self._pass()


class CancelRateRule(RiskRule):
    """撤单率监控：cancel/(new+cancel) 超阈值拒绝（异常交易类风控）。

    当日撤单率过高说明频繁撤单（异常/激进交易），超阈值拒绝新单。
    防未来：只依据已发生的当日下单/撤单计数。
    """
    name = "cancel_rate"
    description = "撤单率监控：cancel/(new+cancel) 超阈值拒绝"

    def __init__(self, max_cancel_rate: float = 0.5, **params: Any) -> None:
        super().__init__(**params)
        self.max_cancel_rate = float(max_cancel_rate)

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        new_cnt = int(context.new_order_count_today)
        cancel_cnt = int(context.cancel_count_today)
        total = new_cnt + cancel_cnt
        if total > 0:
            rate = cancel_cnt / total
            if rate > self.max_cancel_rate:
                return self._reject(
                    f"撤单率超限: {rate*100:.1f}% (撤单{cancel_cnt}/总{total}), "
                    f"上限{self.max_cancel_rate*100:.1f}%"
                )
        return self._pass()


class FatFingerRule(RiskRule):
    """胖手指防护：单笔金额超限或申报价偏离现价过大（异常交易类风控）。

    防未来：只基于当前已可得现价（context.current_price）与本次申报价格，
    不引用未来数据。市价单无申报价时仅做金额上限判定。
    """
    name = "fat_finger"
    description = "胖手指防护：单笔金额超限或价格偏离现价过大"

    def __init__(
        self,
        max_order_amount: float = 1_000_000,
        max_price_deviation_pct: float = 0.20,
        **params: Any,
    ) -> None:
        super().__init__(**params)
        self.max_order_amount = float(max_order_amount)
        self.max_price_deviation_pct = float(max_price_deviation_pct)

    def check(self, order: Order, context: RiskContext) -> RiskCheckResult:
        price = order.price if (order.price or 0) > 0 else context.current_price.get(order.symbol, 0.0)
        amount = order.quantity * price
        if amount > self.max_order_amount:
            return self._reject(
                f"胖手指：单笔金额超限 {amount:,.0f} > {self.max_order_amount:,.0f}"
            )
        ref = context.current_price.get(order.symbol)
        if ref and ref > 0 and price and price > 0:
            dev = abs(price - ref) / ref
            if dev > self.max_price_deviation_pct:
                return self._reject(
                    f"胖手指：申报价偏离现价 {dev*100:.1f}% > "
                    f"{self.max_price_deviation_pct*100:.1f}%"
                )
        return self._pass()


RULE_REGISTRY: Dict[str, type] = {
    "position_limit": PositionLimitRule,
    "single_order_limit": SingleOrderLimitRule,
    "max_drawdown": MaxDrawdownRule,
    "daily_loss_limit": DailyLossLimitRule,
    "stop_loss": StopLossRule,
    "take_profit": TakeProfitRule,
    "rate_limit": RateLimitRule,
    "price_limit": PriceLimitRule,
    "trading_hours": TradingHoursRule,
    "blacklist": BlacklistRule,
    "cash_availability": CashAvailabilityRule,
    "self_trade": SelfTradeRule,
    "cancel_rate": CancelRateRule,
    "fat_finger": FatFingerRule,
}


# ═══════════════════════════════════════════════
#  风控规则库
# ═══════════════════════════════════════════════

class RiskRuleLibrary:
    """
    风控规则库

    统一管理多个风控规则，支持添加/删除/批量检查/配置加载。
    """

    def __init__(self):
        self._rules: Dict[str, RiskRule] = {}
        self._check_order: List[str] = []

    def add_rule(self, rule: RiskRule) -> None:
        if rule.name in self._rules:
            logger.warning(f"[risk] 规则已存在，将覆盖: {rule.name}")
        self._rules[rule.name] = rule
        if rule.name not in self._check_order:
            self._check_order.append(rule.name)
        logger.info(f"[risk] 添加规则: {rule.name}")

    def remove_rule(self, rule_name: str) -> bool:
        if rule_name in self._rules:
            del self._rules[rule_name]
            self._check_order.remove(rule_name)
            return True
        return False

    def get_rule(self, rule_name: str) -> Optional[RiskRule]:
        return self._rules.get(rule_name)

    def enable_rule(self, rule_name: str) -> bool:
        rule = self._rules.get(rule_name)
        if rule:
            rule.enabled = True
            return True
        return False

    def disable_rule(self, rule_name: str) -> bool:
        rule = self._rules.get(rule_name)
        if rule:
            rule.enabled = False
            return True
        return False

    @property
    def rule_names(self) -> List[str]:
        return list(self._check_order)

    @property
    def enabled_rules(self) -> List[str]:
        return [n for n in self._check_order if self._rules[n].enabled]

    def check_all(self, order: Order, context: RiskContext) -> RiskCheckResult:
        """批量检查所有已启用规则，第一个拒绝立即返回（短路）"""
        all_details: Dict[str, Any] = {}
        for rule_name in self._check_order:
            rule = self._rules.get(rule_name)
            if not rule or not rule.enabled:
                continue
            result = rule.check(order, context)
            all_details[rule_name] = result.details
            if not result.passed:
                logger.warning(f"[risk] 风控拒绝: {rule_name} - {result.reason}")
                result.details["all_checks"] = all_details
                return result
        return RiskCheckResult(passed=True, rule_name="all",
                                reason="全部风控规则通过", details=all_details)

    def check_all_detailed(self, order: Order, context: RiskContext) -> List[RiskCheckResult]:
        """批量检查所有规则，返回所有结果（不短路，用于审计）"""
        results = []
        for rule_name in self._check_order:
            rule = self._rules.get(rule_name)
            if not rule:
                continue
            results.append(rule.check(order, context))
        return results

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "RiskRuleLibrary":
        """
        从配置字典加载规则库

        单条规则构造失败时（参数非法等）：
          - 默认库（create_default_library）存在同名规则 → 补默认规则兜底，
            防止风控规则静默缺失（真钱安全第一）；
          - 默认库无同名规则（无法兜底）→ 硬失败，配置错误必须显式暴露。
        """
        library = cls()
        default_library = create_default_library()
        for rule_name, rule_config in config.items():
            if rule_name not in RULE_REGISTRY:
                # P1-18：未知规则名必须硬失败，不允许静默跳过（否则配置拼错=无任何风控）
                raise ValueError(
                    f"[risk] 未知风控规则: {rule_name!r}，不在注册表中，"
                    f"配置拼错必须显式暴露，禁止静默跳过（可用规则: {sorted(RULE_REGISTRY)}）"
                )
            rule_class = RULE_REGISTRY[rule_name]
            enabled = rule_config.get("enabled", True)
            params = {k: v for k, v in rule_config.items() if k != "enabled"}
            try:
                rule = rule_class(enabled=enabled, **params)
                library.add_rule(rule)
            except Exception as e:
                fallback = default_library.get_rule(rule_name)
                if fallback is None:
                    raise ValueError(
                        f"[risk] 规则 {rule_name} 配置构造失败且默认库无同名规则"
                        f"可兜底，启动硬失败: {e}"
                    ) from e
                logger.error(
                    f"[risk] 创建规则失败: {rule_name} - {e}；已补默认规则兜底"
                )
                library.add_rule(fallback)
        return library

    def __repr__(self) -> str:
        return f"RiskRuleLibrary(rules={len(self._rules)}, enabled={len(self.enabled_rules)})"


# ═══════════════════════════════════════════════
#  默认风控规则库（v1推荐配置）
# ═══════════════════════════════════════════════

def create_default_library() -> RiskRuleLibrary:
    """创建默认风控规则库（v1推荐配置，A股市场）"""
    library = RiskRuleLibrary()
    library.add_rule(PositionLimitRule(max_position_pct=0.30, max_positions=5))
    library.add_rule(SingleOrderLimitRule(max_order_amount=200_000))
    library.add_rule(MaxDrawdownRule(max_drawdown_pct=0.20))
    library.add_rule(DailyLossLimitRule(max_daily_loss_pct=0.01))
    library.add_rule(StopLossRule(max_loss_pct=0.10))
    library.add_rule(TakeProfitRule(max_profit_pct=0.50))
    library.add_rule(RateLimitRule(max_orders_per_day=100, min_interval_seconds=1.0))
    library.add_rule(PriceLimitRule(limit_pct=0.10))
    library.add_rule(TradingHoursRule())
    library.add_rule(BlacklistRule())
    library.add_rule(CashAvailabilityRule(min_cash_reserve=1000))
    return library


# ═══════════════════════════════════════════════
#  限额使用率监控（B-3，回写#223）
# ═══════════════════════════════════════════════
# 验收口径（施工总清单 B-3 行 + P0-2 锁定表风控行）：
#   限额使用率引用 P0-2 锁定清单（usage >= 80% 预警、usage > 95% 强告警，LOCKED）；
#   集中度/杠杆/单票仓位限额分别设值并纳入指标与告警；
#   限额使用率指标进入 /api/metrics，超阈值分级告警；阈值取自 P0-2 锁定表。
#
# 阈值真源 R-04：config/settings.yaml 的 monitoring.slo.risk 块，
#   读取入口 myquant.core.settings.get_monitoring_config()，代码内不硬编码业务阈值。
#   - position_limit_usage_warn=0.80  （预警线，LOCKED）
#   - position_limit_usage_alert=0.95 （强告警线，LOCKED；语义以 usage > 0.95 为准）
#   - concentration_limit / leverage_limit / single_stock_position_limit
#       （PENDING_CAL，当前为 ~/None → 该维度"未启用"，不告警、不编造数值）
#
# 与本文件既有规则类（PositionLimitRule 等"下单前风控"）的关系：
#   既有规则在 check_order 路径做"拒单"硬约束；本监控类是"监控侧 usage 度量 + 告警"，
#   只观测已发生的持仓/资金快照、不拦截订单，二者互不改动行为。

#: 受监控的三个限额维度
DIM_CONCENTRATION: str = "concentration"   # 集中度
DIM_LEVERAGE: str = "leverage"            # 杠杆
DIM_SINGLE_STOCK: str = "single_stock"    # 单票仓位
_ALL_DIMENSIONS: tuple = (DIM_CONCENTRATION, DIM_LEVERAGE, DIM_SINGLE_STOCK)

#: 各维度对应的使用率指标名（GAUGE；BuiltinMetrics 登记由主控 Wave2 统一做）
METRIC_LIMIT_USAGE: Dict[str, str] = {
    DIM_CONCENTRATION: "limit_usage_concentration",
    DIM_LEVERAGE: "limit_usage_leverage",
    DIM_SINGLE_STOCK: "limit_usage_single_stock",
}

#: 告警 category（与 alerting.py category="risk" 一致，走风控 Runbook 兜底）
_ALERT_CATEGORY: str = "risk"


def get_limit_usage_metrics_registration() -> List[Dict[str, Any]]:
    """
    返回限额使用率指标注册清单（供主控 Wave2 统一登记 BuiltinMetrics）。

    本监控类经 get_metrics_collector().set_gauge 懒加载写入指标（_ensure_metric
    自动建 GAUGE），但指标的正式登记（名称/类型/标签/描述）由主控统一收口，
    本函数仅返回声明清单。

    Returns:
        list[dict]: 每项含 name / type / labels / description。
    """
    return [
        {
            "name": METRIC_LIMIT_USAGE[DIM_CONCENTRATION],
            "type": "GAUGE",
            "labels": ["dimension", "enabled"],
            "description": (
                "集中度限额使用率（最大单标的市值/总资产 ÷ concentration_limit）；"
                "enabled=false 表示该维度未配置限额（PENDING_CAL）"
            ),
        },
        {
            "name": METRIC_LIMIT_USAGE[DIM_LEVERAGE],
            "type": "GAUGE",
            "labels": ["dimension", "enabled"],
            "description": (
                "杠杆限额使用率（总市值/净资产 ÷ leverage_limit）；"
                "enabled=false 表示该维度未配置限额（PENDING_CAL）"
            ),
        },
        {
            "name": METRIC_LIMIT_USAGE[DIM_SINGLE_STOCK],
            "type": "GAUGE",
            "labels": ["dimension", "enabled"],
            "description": (
                "单票仓位限额使用率（最大单票市值/总资产 ÷ single_stock_position_limit）；"
                "enabled=false 表示该维度未配置限额（PENDING_CAL）"
            ),
        },
    ]


class LimitUsageMonitor:
    """
    限额使用率监控器（监控侧 usage 度量 + 分级告警，B-3）。

    对集中度 / 杠杆 / 单票仓位三个维度，基于"已发生"的持仓/资金快照计算使用率，
    并对照 P0-2 锁定阈值分级告警；只观测、不拦截订单（与下单前风控规则职责分离）。

    口径（按现有数据可得性定，docstring 显式注明）:
      - 集中度 exposure = 最大单标的市值 / 总资产
      - 单票仓位 exposure = 最大单票市值 / 总资产
          （集中度与单票仓位同源于"最大单标的市值/总资产"这一事实量，但分别对照
          concentration_limit 与 single_stock_position_limit 两条独立政策线，故分设指标与告警）
      - 杠杆 exposure = 总市值 / 净资产(equity)
          （A股现金账户无融资融券时总市值≤净资产，usage≤1；若数据可得则按此等效口径）

    使用率与分级（阈值 R-04，读 slo.risk）:
      usage = exposure / 该维度限额
      - usage <  warn(0.80)        → 正常，不告警
      - warn <= usage <= alert    → WARNING（P2 级预警）
      - usage >  alert(0.95)      → ERROR（P1 级强告警；语义以严格大于 95% 为准）

    未启用维度：该维度限额配置为 None（PENDING_CAL）→ 不告警，指标以 enabled=false
    输出"未配置"语义，绝不编造阈值数值。

    防未来：只消费传入/已注入 getter 返回的"当前及已发生"持仓与资金快照，
    不引用任何未来 bar / 预测价格。

    使用方式:
        mon = LimitUsageMonitor()
        mon.attach_snapshot_source(portfolio.get_account, portfolio.get_positions_snapshot)
        result = mon.collect()      # POLL 巡检入口；check_order 路径也可顺带调用
    """

    def __init__(
        self,
        warn_threshold: Optional[float] = None,
        alert_threshold: Optional[float] = None,
        concentration_limit: Optional[float] = None,
        leverage_limit: Optional[float] = None,
        single_stock_limit: Optional[float] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        account_getter: Optional[Callable[[], Dict[str, Any]]] = None,
        positions_getter: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ) -> None:
        """
        Parameters
        ----------
        warn_threshold : float | None
            预警线使用率（如 0.80）。None 读 slo.risk.position_limit_usage_warn。
        alert_threshold : float | None
            强告警线使用率（如 0.95，usage > 此值为 ERROR）。
            None 读 slo.risk.position_limit_usage_alert。
        concentration_limit / leverage_limit / single_stock_limit : float | None
            各维度限额（分母）。None 读 slo.risk.concentration_limit / leverage_limit /
            single_stock_position_limit；读到 None（~）则该维度"未启用"。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（进 /api/metrics）。
        account_getter : Callable[[], dict] | None
            只读账户快照源（如 portfolio.get_account），返回含 total_assets/
            market_value/equity 的 dict。
        positions_getter : Callable[[], list[dict]] | None
            只读持仓快照源（如 portfolio.get_positions_snapshot），返回持仓 dict 列表。
            每个持仓 dict 须含 symbol 与 market_value（或 quantity/market_price 兜底）。

        Raises
        ------
        ValueError : 分级阈值非法（warn/alert 不在 (0,1]、alert <= warn）时抛出。
        """
        slo = get_monitoring_config().get("slo", {}).get("risk", {}) or {}

        self._warn: float = (
            float(warn_threshold) if warn_threshold is not None
            else float(slo.get("position_limit_usage_warn", 0.80))
        )
        self._alert: float = (
            float(alert_threshold) if alert_threshold is not None
            else float(slo.get("position_limit_usage_alert", 0.95))
        )
        # 各维度限额：显式注入 > slo 配置；读到 None（PENDING_CAL）→ 未启用
        self._limits: Dict[str, Optional[float]] = {
            DIM_CONCENTRATION: self._resolve_limit(concentration_limit, slo.get("concentration_limit")),
            DIM_LEVERAGE: self._resolve_limit(leverage_limit, slo.get("leverage_limit")),
            DIM_SINGLE_STOCK: self._resolve_limit(single_stock_limit, slo.get("single_stock_position_limit")),
        }

        # 防御式校验（拒绝脏配置运行）
        if not (0.0 < self._warn <= 1.0):
            raise ValueError(f"position_limit_usage_warn 必须在 (0,1]，实际 {self._warn!r}")
        if not (0.0 < self._alert <= 1.0):
            raise ValueError(f"position_limit_usage_alert 必须在 (0,1]，实际 {self._alert!r}")
        if self._alert <= self._warn:
            raise ValueError(
                f"alert 线必须 > warn 线，实际 warn={self._warn!r} alert={self._alert!r}"
            )

        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector
        self._account_getter = account_getter
        self._positions_getter = positions_getter
        logger.debug(
            "LimitUsageMonitor 初始化: warn=%.2f alert=%.2f limits=%s",
            self._warn, self._alert, self._limits,
        )

    @staticmethod
    def _resolve_limit(explicit: Optional[float], slo_value: Any) -> Optional[float]:
        """
        解析单维度限额：显式注入优先；否则取 slo 配置；~ / None / 非法 → None（未启用）。

        绝不把 None 编造为数值。
        """
        if explicit is not None:
            v = float(explicit)
            return v if v > 0 else None
        if slo_value is None:
            return None
        try:
            v = float(slo_value)
        except (TypeError, ValueError):
            logger.warning("限额配置非法（已忽略，维度未启用）: %r", slo_value)
            return None
        return v if v > 0 else None

    # ── 快照源装配 ──────────────────────────────────────────
    def attach_snapshot_source(
        self,
        account_getter: Optional[Callable[[], Dict[str, Any]]] = None,
        positions_getter: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ) -> None:
        """
        装配只读持仓/资金快照源（POLL 巡检时自动取用）。

        Parameters
        ----------
        account_getter : Callable[[], dict] | None
            账户资金快照 getter（如 portfolio.get_account）。
        positions_getter : Callable[[], list[dict]] | None
            持仓快照 getter（如 portfolio.get_positions_snapshot）。
        """
        if account_getter is not None and not callable(account_getter):
            raise ValueError("account_getter 必须是可调用对象")
        if positions_getter is not None and not callable(positions_getter):
            raise ValueError("positions_getter 必须是可调用对象")
        self._account_getter = account_getter
        self._positions_getter = positions_getter

    # ── 快照抽取（防御式；只取已发生事实）────────────────────
    @staticmethod
    def _position_market_value(pos: Dict[str, Any]) -> float:
        """
        从持仓 dict 取市值：优先 market_value，兜底 |quantity|*market_price。

        防未来：只取快照中已记录的数量与最新市场价，不预测价格。
        """
        mv = pos.get("market_value")
        if mv is not None:
            try:
                return max(0.0, float(mv))
            except (TypeError, ValueError):
                pass
        try:
            qty = abs(float(pos.get("quantity", 0.0)))
            price = float(pos.get("market_price", 0.0))
            return max(0.0, qty * price)
        except (TypeError, ValueError):
            return 0.0

    def _pull_snapshot(
        self,
        account: Optional[Dict[str, Any]],
        positions: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """
        合并显式传入快照与注入 getter 快照，抽取计算所需事实量。

        Returns
        -------
        dict : {total_assets, market_value, equity, max_single_value, positions_count}
            缺失/非法量记 0.0，不编造。
        """
        acct: Dict[str, Any] = {}
        if account is not None:
            acct = dict(account)
        elif self._account_getter is not None:
            try:
                got = self._account_getter()
                if isinstance(got, dict):
                    acct = got
            except Exception as e:  # noqa: BLE001
                logger.warning("读取账户快照失败（按空账户处理）: %s", e)
                acct = {}

        pos_list: List[Dict[str, Any]] = []
        if positions is not None:
            pos_list = list(positions)
        elif self._positions_getter is not None:
            try:
                got = self._positions_getter()
                if isinstance(got, list):
                    pos_list = list(got)
            except Exception as e:  # noqa: BLE001
                logger.warning("读取持仓快照失败（按空持仓处理）: %s", e)
                pos_list = []

        def _num(key: str, default: float = 0.0) -> float:
            try:
                return float(acct.get(key, default))
            except (TypeError, ValueError):
                return default

        total_assets = max(0.0, _num("total_assets"))
        market_value = max(0.0, _num("market_value"))
        equity = max(0.0, _num("equity", total_assets))  # 净资产缺省回退总资产

        max_single_value = 0.0
        sum_pos_value = 0.0
        for pos in pos_list:
            if not isinstance(pos, dict):
                continue
            mv = self._position_market_value(pos)
            sum_pos_value += mv
            if mv > max_single_value:
                max_single_value = mv
        # 账户总市值字段缺省时，用持仓市值合计兜底
        if market_value <= 0.0:
            market_value = sum_pos_value

        return {
            "total_assets": total_assets,
            "market_value": market_value,
            "equity": equity,
            "max_single_value": max_single_value,
            "positions_count": len(pos_list),
        }

    # ── 使用率计算 ───────────────────────────────────────────
    def _compute_usages(self, snap: Dict[str, Any]) -> Dict[str, Optional[float]]:
        """
        由快照事实量计算各维度使用率（usage = exposure / limit）。

        Returns
        -------
        dict : {dimension: usage|None}。
            维度未启用（limit=None）→ None；事实量不足（分母<=0）→ 该维度 None，不编造。
        """
        total_assets = snap["total_assets"]
        equity = snap["equity"]
        market_value = snap["market_value"]
        max_single = snap["max_single_value"]

        usages: Dict[str, Optional[float]] = {}
        for dim in _ALL_DIMENSIONS:
            limit = self._limits.get(dim)
            if limit is None:
                usages[dim] = None  # 未启用
                continue
            if dim == DIM_CONCENTRATION:
                exposure = (max_single / total_assets) if total_assets > 0 else 0.0
            elif dim == DIM_SINGLE_STOCK:
                exposure = (max_single / total_assets) if total_assets > 0 else 0.0
            elif dim == DIM_LEVERAGE:
                exposure = (market_value / equity) if equity > 0 else 0.0
            else:  # pragma: no cover - 防御
                usages[dim] = None
                continue
            usages[dim] = exposure / limit if limit > 0 else None
        return usages

    # ── 告警接线 ────────────────────────────────────────────
    def _fire_alert(self, level: SeverityLevel, dimension: str, usage: float) -> None:
        """
        发送分级告警（category=risk，dedup_key=limit:<dimension>:<level>）。

        说明：B-3 要求 dedup_key=limit:<dimension>；这里追加级别后缀，
        保证 usage 由 0.85 升至 0.97 时 ERROR 不被 WARNING 的去重窗口吞掉（升级必达）。
        details 含 usage/limit 及配置来源（R-04）。
        """
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = get_alert_manager()
        limit = self._limits[dimension]
        level_cn = "强告警" if level == SeverityLevel.ERROR else "预警"
        mgr.alert(
            level=level,
            message=(
                f"[limit-usage] {dimension} 限额使用率 {usage:.1%} "
                f"（限额 {limit:.3f}）{level_cn}"
            ),
            source="risk.limit_usage",
            category=_ALERT_CATEGORY,
            details={
                "dimension": dimension,
                "usage": round(usage, 4),
                "limit": limit,
                "warn_threshold": self._warn,
                "alert_threshold": self._alert,
                "configured_from": "config/settings.yaml monitoring.slo.risk",
            },
            deduplication_key=f"limit:{dimension}:{'error' if level == SeverityLevel.ERROR else 'warn'}",
        )

    # ── 指标写入（懒加载 collector）──────────────────────────
    def _write_metrics(self, usages: Dict[str, Optional[float]]) -> None:
        """
        经 set_gauge 写入三个使用率指标（进 /api/metrics）。

        未启用维度写 0.0 + labels enabled=false（输出"未配置"语义，不编造数值）。
        """
        col = self._metrics_collector
        if col is None:
            try:
                from myquant.monitoring.metrics_collector import get_metrics_collector
                col = get_metrics_collector()
                self._metrics_collector = col
            except Exception as e:  # noqa: BLE001
                logger.warning("获取 metrics_collector 失败（指标未写入）: %s", e)
                return
        for dim in _ALL_DIMENSIONS:
            usage = usages.get(dim)
            enabled = usage is not None
            try:
                col.set_gauge(
                    METRIC_LIMIT_USAGE[dim],
                    round(usage, 4) if usage is not None else 0.0,
                    labels={"dimension": dim, "enabled": "true" if enabled else "false"},
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"限额使用率指标 {dim} 写入失败（不阻断检测）: %s", e)

    # ── 核心入口：POLL 巡检 / check_order 顺带调用 ───────────
    def check(
        self,
        account: Optional[Dict[str, Any]] = None,
        positions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        计算三类使用率 → 分级告警 → 写指标（事件/周期触发入口）。

        Parameters
        ----------
        account : dict | None
            账户资金快照；None 取注入的 account_getter。
        positions : list[dict] | None
            持仓快照；None 取注入的 positions_getter。

        Returns
        -------
        dict : 各维度 usage、是否启用、触发的告警级别。
        """
        snap = self._pull_snapshot(account, positions)
        usages = self._compute_usages(snap)
        self._write_metrics(usages)

        fired: Dict[str, str] = {}
        for dim in _ALL_DIMENSIONS:
            usage = usages.get(dim)
            if usage is None:
                continue  # 未启用：不告警
            # 分级：> alert → ERROR(P1)；>= warn → WARNING(P2)
            if usage > self._alert:
                self._fire_alert(SeverityLevel.ERROR, dim, usage)
                fired[dim] = "ERROR"
            elif usage >= self._warn:
                self._fire_alert(SeverityLevel.WARNING, dim, usage)
                fired[dim] = "WARNING"
        return {
            "snapshot": snap,
            "usages": {d: (round(u, 4) if u is not None else None) for d, u in usages.items()},
            "enabled": {d: self._limits.get(d) is not None for d in _ALL_DIMENSIONS},
            "fired": fired,
        }

    def collect(
        self,
        account: Optional[Dict[str, Any]] = None,
        positions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """collect() = check()（POLL 巡检周期入口的别名）。"""
        return self.check(account=account, positions=positions)

    def get_status(self) -> Dict[str, Any]:
        """返回监控配置快照（阈值/各维度限额/启用状态），供巡检报告/看板暴露。"""
        return {
            "warn_threshold": self._warn,
            "alert_threshold": self._alert,
            "limits": dict(self._limits),
            "enabled": {d: self._limits.get(d) is not None for d in _ALL_DIMENSIONS},
            "configured_from": "config/settings.yaml monitoring.slo.risk",
        }


# ═══════════════════════════════════════════════
#  LimitUsageMonitor __main__ 自测 demo
# ═══════════════════════════════════════════════
def _run_limit_usage_self_test() -> int:
    """模块自测：注入假告警/假指标与固定快照，验证 0.5/0.85/0.97 分级与未启用语义。"""
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name} {info}")

    class _FakeAlert:
        def __init__(self) -> None:
            self.calls: list = []

        def alert(self, **kw):
            self.calls.append(kw)
            return 1

    class _FakeMetrics:
        def __init__(self) -> None:
            self.gauges: Dict[str, float] = {}
            self.labels: Dict[str, dict] = {}

        def set_gauge(self, name: str, value: float, labels=None):
            self.gauges[name] = float(value)
            self.labels[name] = labels or {}

    # 构造快照：总资产 100 万，最大单票 S1 市值=max_single，另有 S2=10 万（更小，不反超）；
    # 总市值=max_single+10 万，净资产=100 万。
    #   集中度/单票 exposure = max_single/100万；杠杆 exposure = 总市值/净资产
    def snap(max_single: float, total_assets: float = 1_000_000.0,
             equity: float = 1_000_000.0):
        other = 100_000.0
        market_value = max_single + other
        account = {"total_assets": total_assets, "market_value": market_value, "equity": equity}
        positions = [
            {"symbol": "S1", "market_value": max_single},
            {"symbol": "S2", "market_value": other},
        ]
        return account, positions

    # 1) usage=0.5（最大单票 15 万/限额 30 万）→ 不告警
    am1, met1 = _FakeAlert(), _FakeMetrics()
    mon1 = LimitUsageMonitor(
        warn_threshold=0.80, alert_threshold=0.95,
        concentration_limit=0.30, leverage_limit=1.0, single_stock_limit=0.30,
        alert_manager=am1, metrics_collector=met1,
    )
    acct, pos = snap(150_000.0)  # 15万/30万 = 0.5
    r1 = mon1.check(account=acct, positions=pos)
    check("1) usage=0.5 不告警", am1.calls == [] and r1["fired"] == {}, f"calls={am1.calls} fired={r1['fired']}")

    # 2) usage=0.85（最大单票 25.5 万/限额 30 万）→ WARNING
    am2, met2 = _FakeAlert(), _FakeMetrics()
    mon2 = LimitUsageMonitor(
        warn_threshold=0.80, alert_threshold=0.95,
        concentration_limit=0.30, leverage_limit=1.0, single_stock_limit=0.30,
        alert_manager=am2, metrics_collector=met2,
    )
    acct, pos = snap(255_000.0)  # 25.5万/30万 = 0.85
    r2 = mon2.check(account=acct, positions=pos)
    warns = [c for c in am2.calls if c["level"] == SeverityLevel.WARNING]
    check("2) usage=0.85 触发 WARNING", len(warns) >= 1 and r2["fired"].get("concentration") == "WARNING",
          f"calls={am2.calls} fired={r2['fired']}")

    # 3) usage=0.97（最大单票 29.1 万/限额 30 万）→ ERROR
    am3, met3 = _FakeAlert(), _FakeMetrics()
    mon3 = LimitUsageMonitor(
        warn_threshold=0.80, alert_threshold=0.95,
        concentration_limit=0.30, leverage_limit=1.0, single_stock_limit=0.30,
        alert_manager=am3, metrics_collector=met3,
    )
    acct, pos = snap(291_000.0)  # 29.1万/30万 = 0.97
    r3 = mon3.check(account=acct, positions=pos)
    errs = [c for c in am3.calls if c["level"] == SeverityLevel.ERROR]
    check("3) usage=0.97 触发 ERROR", len(errs) >= 1 and r3["fired"].get("concentration") == "ERROR",
          f"calls={am3.calls} fired={r3['fired']}")

    # 4) 未配置维度（limit=None）→ 不告警、指标 enabled=false
    am4, met4 = _FakeAlert(), _FakeMetrics()
    mon4 = LimitUsageMonitor(
        warn_threshold=0.80, alert_threshold=0.95,
        concentration_limit=None, leverage_limit=1.0, single_stock_limit=None,
        alert_manager=am4, metrics_collector=met4,
    )
    acct, pos = snap(291_000.0)
    r4 = mon4.check(account=acct, positions=pos)
    conc_calls = [c for c in am4.calls if c["details"]["dimension"] == "concentration"]
    check("4) 未配置集中度维度不告警", conc_calls == [] and r4["enabled"]["concentration"] is False,
          f"calls={conc_calls} enabled={r4['enabled']}")
    check("4) 未配置维度指标 enabled=false", met4.labels.get("limit_usage_concentration", {}).get("enabled") == "false",
          f"labels={met4.labels}")

    # 5) 指标写入 collector 可查
    check("5) 三指标已写入 collector",
          met3.gauges.get("limit_usage_concentration") == 0.97
          and "limit_usage_leverage" in met3.gauges
          and "limit_usage_single_stock" in met3.gauges,
          f"gauges={met3.gauges}")

    # 6) 指标注册清单含三 GAUGE
    reg = get_limit_usage_metrics_registration()
    check("6) 指标注册清单=3 项 GAUGE", len(reg) == 3 and all(r["type"] == "GAUGE" for r in reg),
          f"reg={reg}")

    # 7) 非法阈值抛 ValueError
    raised = False
    try:
        LimitUsageMonitor(warn_threshold=0.95, alert_threshold=0.80, alert_manager=_FakeAlert())
    except ValueError:
        raised = True
    check("7) alert<=warn 抛 ValueError", raised, "")

    print("=" * 60)
    print(f"rules.py LimitUsageMonitor 自测: {passed} 项通过")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"失败 {len(failures)} 项")
        return 1
    print("全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_limit_usage_self_test())
