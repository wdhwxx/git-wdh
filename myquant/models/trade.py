# 文件名：myquant/models/trade.py
"""
trade.py - 交易模型
====================
定义订单、持仓、成交记录等核心交易数据结构。

类清单:
  - OrderSide / OrderType / OrderStatus : 枚举
  - Order      : 委托订单
  - Position   : 持仓
  - Trade      : 成交记录
  - OrderFactory : 订单工厂 (快捷创建)

设计原则:
  - 不可变核心字段 (order_id, symbol, side)
  - 可变状态字段 (status, filled_qty) 通过方法变更
  - 所有字段带类型标注
  - 内置校验逻辑
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════
#  枚举定义
# ═══════════════════════════════════════════════

class OrderSide(Enum):
    """订单方向"""
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "OrderSide":
        return OrderSide.SELL if self == OrderSide.BUY else OrderSide.BUY

    def __str__(self) -> str:
        return self.value


class OrderType(Enum):
    """订单类型"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"

    def __str__(self) -> str:
        return self.value


class OrderStatus(Enum):
    """订单状态机"""
    PENDING = "PENDING"              # 待提交
    SUBMITTED = "SUBMITTED"          # 已提交
    PARTIAL_FILLED = "PARTIAL_FILLED"  # 部分成交
    FILLED = "FILLED"                # 全部成交
    CANCELLED = "CANCELLED"          # 已撤销
    REJECTED = "REJECTED"            # 被拒绝
    EXPIRED = "EXPIRED"              # 已过期

    @property
    def is_terminal(self) -> bool:
        """是否终态 (不可再变更)"""
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    @property
    def is_active(self) -> bool:
        """是否活跃 (可撤销)"""
        return self in (
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIAL_FILLED,
        )

    def __str__(self) -> str:
        return self.value


# 合法状态转换表
_VALID_TRANSITIONS: Dict[OrderStatus, set] = {
    OrderStatus.PENDING: {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED},
    OrderStatus.PARTIAL_FILLED: {OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}


# ═══════════════════════════════════════════════
#  Order - 委托订单
# ═══════════════════════════════════════════════

@dataclass
class Order:
    """
    委托订单

    Attributes
    ----------
    order_id    : 唯一标识
    symbol      : 标的代码 (如 '600519.SH')
    side        : 方向 BUY/SELL
    order_type  : 类型 MARKET/LIMIT/...
    quantity    : 委托数量 (股/手)
    price       : 委托价格 (市价单为0或None)
    stop_price  : 止损触发价
    status      : 当前状态
    filled_qty  : 已成交数量
    filled_price: 成交均价
    created_at  : 创建时间戳
    updated_at  : 最后更新时间戳
    strategy_id : 来源策略ID
    tag         : 自定义标签
    extra       : 扩展字段
    """

    order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    quantity: float = 0.0
    price: Optional[float] = None
    stop_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_price: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    strategy_id: str = ""
    tag: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── 校验 ──
    def validate(self) -> List[str]:
        """校验订单合法性，返回错误列表 (空=通过)"""
        errors: List[str] = []
        if not self.symbol:
            errors.append("symbol 不能为空")
        if self.quantity <= 0:
            errors.append(f"quantity 必须 > 0, 当前: {self.quantity}")
        if self.order_type == OrderType.LIMIT and self.price is None:
            errors.append("LIMIT 订单必须指定 price")
        if self.order_type == OrderType.STOP and self.stop_price is None:
            errors.append("STOP 订单必须指定 stop_price")
        if self.order_type in (OrderType.STOP_LIMIT,) and (self.price is None or self.stop_price is None):
            errors.append("STOP_LIMIT 订单必须指定 price 和 stop_price")
        if self.price is not None and self.price < 0:
            errors.append(f"price 不能为负: {self.price}")
        if self.stop_price is not None and self.stop_price < 0:
            errors.append(f"stop_price 不能为负: {self.stop_price}")
        return errors

    @property
    def is_valid(self) -> bool:
        return len(self.validate()) == 0

    # ── 状态变更 ──
    def transition_to(self, new_status: OrderStatus) -> bool:
        """状态转换 (校验合法性)"""
        if new_status in _VALID_TRANSITIONS.get(self.status, set()):
            self.status = new_status
            self.updated_at = time.time()
            return True
        return False

    def submit(self) -> bool:
        return self.transition_to(OrderStatus.SUBMITTED)

    def fill(self, qty: float, price: float) -> bool:
        """
        成交回报（防御式校验：非法数量与超额成交一律拒绝入账）

        Parameters
        ----------
        qty   : 本次成交数量，必须 > 0 且 <= 剩余可成交量（remaining_qty）
        price : 成交价格

        Returns
        -------
        True  : 全部成交（状态置 FILLED）
        False : 部分成交（状态置 PARTIAL_FILLED）；
                若订单非活跃、数量非法或超额，同样返回 False，
                但订单数量/状态保持不变（诊断信息写入 extra，可通过
                extra['fill_rejected'] 识别拒绝原因）。

        Notes
        -----
        超额成交（如券商重复推送成交回报）若直接入账会导致持仓虚增、
        资金错误，故在此统一拦截——该防线对所有调用方（实盘撮合、
        回测引擎、状态机）生效。
        """
        if not self.status.is_active:
            return False

        if qty <= 0:
            self.extra["fill_rejected"] = True
            self.extra["fill_reject_reason"] = f"成交数量必须 > 0, 当前: {qty}"
            return False

        remaining = self.remaining_qty
        if qty > remaining + 1e-9:
            # 超额成交/重复回报：拒绝入账，账目不更新
            self.extra["fill_rejected"] = True
            self.extra["fill_reject_reason"] = (
                f"成交数量 {qty} 超过剩余可成交量 {remaining:.6f}"
            )
            return False

        self.filled_qty += qty
        # 更新成交均价
        if self.filled_qty > 0:
            self.filled_price = (
                (self.filled_price * (self.filled_qty - qty) + price * qty)
                / self.filled_qty
            )
        self.updated_at = time.time()

        if self.filled_qty >= self.quantity:
            self.status = OrderStatus.FILLED
            return True
        else:
            self.status = OrderStatus.PARTIAL_FILLED
            return False

    def cancel(self) -> bool:
        return self.transition_to(OrderStatus.CANCELLED)

    def reject(self, reason: str = "") -> bool:
        if reason:
            self.extra["reject_reason"] = reason
        return self.transition_to(OrderStatus.REJECTED)

    # ── 计算属性 ──
    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.quantity - self.filled_qty)

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def fill_ratio(self) -> float:
        if self.quantity <= 0:
            return 0.0
        return self.filled_qty / self.quantity

    @property
    def notional_value(self) -> float:
        """名义价值 (委托金额)"""
        p = self.price if self.price else self.filled_price
        return self.quantity * p

    @property
    def filled_notional(self) -> float:
        """已成交金额"""
        return self.filled_qty * self.filled_price

    # ── 序列化 ──
    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "price": self.price,
            "stop_price": self.stop_price,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "filled_price": self.filled_price,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "strategy_id": self.strategy_id,
            "tag": self.tag,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Order":
        data = data.copy()
        data["side"] = OrderSide(data.get("side", "BUY"))
        data["order_type"] = OrderType(data.get("order_type", "MARKET"))
        data["status"] = OrderStatus(data.get("status", "PENDING"))
        return cls(**data)

    def __repr__(self) -> str:
        return (
            f"Order({self.order_id[:8]}.. {self.symbol} "
            f"{self.side.value} {self.order_type.value} "
            f"qty={self.quantity} px={self.price} "
            f"status={self.status.value} "
            f"filled={self.filled_qty}/{self.quantity})"
        )


# ═══════════════════════════════════════════════
#  Position - 持仓
# ═══════════════════════════════════════════════

@dataclass
class Position:
    """
    持仓

    Attributes
    ----------
    symbol       : 标的代码
    quantity     : 持仓数量 (正=多头, 负=空头)
    avg_cost     : 持仓均价
    market_price : 最新市场价
    realized_pnl : 已实现盈亏
    unrealized_pnl : 未实现盈亏 (动态计算)
    opened_at    : 建仓时间
    strategy_id  : 策略ID
    """

    symbol: str = ""
    quantity: float = 0.0
    avg_cost: float = 0.0
    market_price: float = 0.0
    realized_pnl: float = 0.0
    opened_at: float = field(default_factory=time.time)
    strategy_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    # ── 计算属性 ──
    @property
    def market_value(self) -> float:
        """市值"""
        return abs(self.quantity) * self.market_price

    @property
    def cost_value(self) -> float:
        """成本"""
        return abs(self.quantity) * self.avg_cost

    @property
    def unrealized_pnl(self) -> float:
        """浮动盈亏"""
        if self.quantity == 0:
            return 0.0
        return (self.market_price - self.avg_cost) * self.quantity

    @property
    def total_pnl(self) -> float:
        """总盈亏"""
        return self.realized_pnl + self.unrealized_pnl

    @property
    def pnl_ratio(self) -> float:
        """收益率"""
        if self.avg_cost == 0:
            return 0.0
        return (self.market_price - self.avg_cost) / self.avg_cost

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def direction(self) -> str:
        if self.is_long:
            return "LONG"
        elif self.is_short:
            return "SHORT"
        return "FLAT"

    # ── 操作 ──
    def update_market_price(self, price: float) -> None:
        self.market_price = price

    def apply_fill(self, qty: float, price: float, side: OrderSide) -> None:
        """
        应用成交到持仓

        Parameters
        ----------
        qty   : 成交数量 (正数, 必须 > 0)
        price : 成交价格
        side  : 成交方向

        Notes
        -----
        仓位反转（成交穿越 0，如多头持仓超卖成空头）时，
        剩余仓位以本次成交价建立新方向成本价——修复旧实现
        反转后保留原方向成本价导致的 PnL/市值/收益率失真。
        """
        if qty <= 0:
            raise ValueError(f"成交数量必须 > 0, 当前: {qty}")
        signed_qty = qty if side == OrderSide.BUY else -qty

        if self.quantity == 0:
            # 新建仓
            self.quantity = signed_qty
            self.avg_cost = price
            self.opened_at = time.time()
        elif (self.quantity > 0 and signed_qty > 0) or (self.quantity < 0 and signed_qty < 0):
            # 加仓: 更新均价
            total_cost = self.avg_cost * abs(self.quantity) + price * abs(signed_qty)
            self.quantity += signed_qty
            self.avg_cost = total_cost / abs(self.quantity)
        else:
            # 减仓/平仓
            old_qty = self.quantity
            close_qty = min(abs(signed_qty), abs(self.quantity))
            # 已实现盈亏
            if old_qty > 0:
                self.realized_pnl += (price - self.avg_cost) * close_qty
            else:
                self.realized_pnl += (self.avg_cost - price) * close_qty

            self.quantity += signed_qty
            if abs(self.quantity) < 1e-10:
                # 完全平仓
                self.quantity = 0.0
                self.avg_cost = 0.0
            elif self.quantity * old_qty < 0:
                # 仓位反转：剩余部分以本次成交价建立新方向成本价
                self.avg_cost = price

    # ── 序列化 ──
    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "avg_cost": self.avg_cost,
            "market_price": self.market_price,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "total_pnl": self.total_pnl,
            "pnl_ratio": self.pnl_ratio,
            "direction": self.direction,
            "opened_at": self.opened_at,
            "strategy_id": self.strategy_id,
        }

    def __repr__(self) -> str:
        return (
            f"Position({self.symbol} {self.direction} "
            f"qty={self.quantity:.0f} cost={self.avg_cost:.2f} "
            f"mkt={self.market_price:.2f} pnl={self.unrealized_pnl:.2f})"
        )


# ═══════════════════════════════════════════════
#  Trade - 成交记录
# ═══════════════════════════════════════════════

@dataclass
class Trade:
    """
    成交记录 (不可变)

    每笔成交生成一条 Trade，用于:
      - 绩效归因
      - 交易成本分析
      - 审计追踪
    """

    trade_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    order_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: float = 0.0
    price: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    timestamp: float = field(default_factory=time.time)
    strategy_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> float:
        """成交金额"""
        return self.quantity * self.price

    @property
    def total_cost(self) -> float:
        """总成本 (含手续费+滑点)"""
        return self.commission + self.slippage

    @property
    def net_amount(self) -> float:
        """净金额 (买入为负, 卖出为正)"""
        sign = -1 if self.side == OrderSide.BUY else 1
        return sign * self.notional - self.total_cost

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": self.price,
            "commission": self.commission,
            "slippage": self.slippage,
            "notional": self.notional,
            "total_cost": self.total_cost,
            "timestamp": self.timestamp,
            "strategy_id": self.strategy_id,
        }

    def __repr__(self) -> str:
        return (
            f"Trade({self.trade_id[:8]}.. {self.symbol} "
            f"{self.side.value} {self.quantity:.0f}@{self.price:.2f} "
            f"fee={self.commission:.2f})"
        )


# ═══════════════════════════════════════════════
#  OrderFactory - 订单工厂
# ═══════════════════════════════════════════════

class OrderFactory:
    """
    订单工厂 - 快捷创建各类订单

    Usage:
        order = OrderFactory.market_buy("600519.SH", 100)
        order = OrderFactory.limit_sell("000001.SZ", 200, price=15.5)
    """

    @staticmethod
    def market_buy(symbol: str, quantity: float, **kwargs) -> Order:
        return Order(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=quantity, **kwargs)

    @staticmethod
    def market_sell(symbol: str, quantity: float, **kwargs) -> Order:
        return Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=quantity, **kwargs)

    @staticmethod
    def limit_buy(symbol: str, quantity: float, price: float, **kwargs) -> Order:
        return Order(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=quantity, price=price, **kwargs)

    @staticmethod
    def limit_sell(symbol: str, quantity: float, price: float, **kwargs) -> Order:
        return Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=quantity, price=price, **kwargs)

    @staticmethod
    def stop_loss(symbol: str, quantity: float, stop_price: float, side: OrderSide = OrderSide.SELL, **kwargs) -> Order:
        return Order(symbol=symbol, side=side, order_type=OrderType.STOP, quantity=quantity, stop_price=stop_price, **kwargs)

    @staticmethod
    def stop_limit(symbol: str, quantity: float, stop_price: float, price: float, side: OrderSide = OrderSide.SELL, **kwargs) -> Order:
        return Order(symbol=symbol, side=side, order_type=OrderType.STOP_LIMIT, quantity=quantity, stop_price=stop_price, price=price, **kwargs)