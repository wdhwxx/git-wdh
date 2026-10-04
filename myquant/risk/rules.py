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
from datetime import datetime
from datetime import time as dtime
from typing import Any, Dict, List, Optional, Set

from myquant.core.logger import get_logger
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

class TradingHoursRule(RiskRule):
    """交易时段限制规则：A股交易时段外禁止下单"""
    name = "trading_hours"
    description = "交易时段限制：非交易时段禁止下单"

    def __init__(self, morning_start: str = "09:30", morning_end: str = "11:30",
                 afternoon_start: str = "13:00", afternoon_end: str = "15:00", **params):
        super().__init__(**params)
        self.morning_start = self._parse_time(morning_start)
        self.morning_end = self._parse_time(morning_end)
        self.afternoon_start = self._parse_time(afternoon_start)
        self.afternoon_end = self._parse_time(afternoon_end)

    @staticmethod
    def _parse_time(time_str: str) -> dtime:
        h, m = map(int, time_str.split(":"))
        return dtime(hour=h, minute=m)

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
        if not self._is_trading_time(now):
            current_time = (now or datetime.now()).strftime("%H:%M:%S")
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
