# 文件名：myquant/backtest/order_matcher.py
"""
backtest/order_matcher.py - 统一撮合底层
==========================================
三模式回测引擎（vectorized / mixed / event）共用的**同一套**撮合逻辑，
消除多引擎成交判定与成交价计算的逻辑漂移。

职责边界:
  - 本模块只负责"成交判定 + 成交价/成交量计算"，是纯价格撮合；
  - 资金 / 持仓 / 订单状态机 / 费用 / 滑点分别由券商层、状态机、fee_common 负责；
  - 滑点由调用方通过 fee_common 叠加到基础成交价上。

支持的订单类型与撮合规则（A股写实，与原有 SimBroker 对 MARKET/LIMIT 完全一致）:
  - MARKET     : 市价单，以开盘价成交（fill = open）
  - LIMIT 买入 : 最低价 <= 限价 才成交，成交价 = min(限价, 开盘价)
  - LIMIT 卖出 : 最高价 >= 限价 才成交，成交价 = max(限价, 开盘价)
  - STOP 买入  : 最高价 >= 触发价 才触发，成交价 = max(触发价, 开盘价)（向上突破）
  - STOP 卖出  : 最低价 <= 触发价 才触发，成交价 = min(触发价, 开盘价)（向下跌破）
  - STOP_LIMIT 买入: 最高价 >= 触发价 且 限价 >= 最低价 才成交，
                      成交价 = min(限价, max(开盘价, 触发价))
  - STOP_LIMIT 卖出: 最低价 <= 触发价 且 限价 <= 最高价 才成交，
                      成交价 = max(限价, min(开盘价, 触发价))

防未来函数（规则4）:
  - 撮合只使用当前 bar 的 open/high/low（T 日），T 日创建的订单 T+1 日开盘撮合，
    由引擎层控制调用时机；本模块不引用任何未来数据；
  - 每处涉及价格的判断均在注释中标明"本行使用的数据截至 t 时刻"。

依赖:
  标准库: dataclasses, logging, typing
  第三方: numpy
  本地  : myquant.models.market_data (Bar)
          myquant.models.trade (Order, OrderSide, OrderType)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

import numpy as np

from myquant.models.market_data import Bar
from myquant.models.trade import Order, OrderSide, OrderType

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 向量化撮合的 side 整数编码（0=买入, 1=卖出）
_SIDE_BUY: int = 0
_SIDE_SELL: int = 1

# 支持的订单类型集合（防御式校验）
_SUPPORTED_ORDER_TYPES: Tuple[str, ...] = (
    "MARKET", "LIMIT", "STOP", "STOP_LIMIT",
)

# 金额容差（价格比较）
_PRICE_EPSILON: float = 1e-9


# ─────────────────────────────────────────────
# 归一化辅助
# ─────────────────────────────────────────────

def normalize_side_value(side: Union[OrderSide, str]) -> OrderSide:
    """将方向归一化为 OrderSide 枚举。

    Args:
        side: OrderSide 或 'BUY'/'SELL' 等字符串。

    Returns:
        归一化的 OrderSide。

    Raises:
        ValueError: 方向无法识别时抛出。
    """
    if isinstance(side, OrderSide):
        return side
    if isinstance(side, str):
        key = side.strip().upper()
        if key in ("BUY", "B", "买", "买入"):
            return OrderSide.BUY
        if key in ("SELL", "S", "卖", "卖出"):
            return OrderSide.SELL
    raise ValueError(f"无法识别的订单方向: {side!r}")


def normalize_order_type(order_type: Union[OrderType, str]) -> OrderType:
    """将订单类型归一化为 OrderType 枚举。

    Args:
        order_type: OrderType 或 'MARKET'/'LIMIT'/'STOP'/'STOP_LIMIT'。

    Returns:
        归一化的 OrderType。

    Raises:
        ValueError: 订单类型无法识别或不受支持时抛出。
    """
    if isinstance(order_type, OrderType):
        resolved = order_type
    elif isinstance(order_type, str):
        key = order_type.strip().upper()
        try:
            resolved = OrderType(key)
        except ValueError:
            raise ValueError(f"无法识别的订单类型: {order_type!r}") from None
    else:
        raise ValueError(f"无法识别的订单类型: {order_type!r}")

    if resolved.value not in _SUPPORTED_ORDER_TYPES:
        raise ValueError(
            f"不支持的订单类型: {resolved.value}（支持: {', '.join(_SUPPORTED_ORDER_TYPES)}）"
        )
    return resolved


def _side_array_to_int(sides: np.ndarray) -> np.ndarray:
    """将方向数组归一化为整数编码数组（0=买入, 1=卖出）。

    Args:
        sides: 由 OrderSide/字符串/整数组成的数组。

    Returns:
        int 数组（0/1）。

    Raises:
        ValueError: 包含无法识别的方向时抛出。
    """
    out = np.empty(sides.size, dtype=np.int8)
    for i, s in enumerate(sides):
        if isinstance(s, (int, np.integer)):
            val = int(s)
            if val not in (_SIDE_BUY, _SIDE_SELL):
                raise ValueError(f"无法识别的方向整数编码: {val!r}")
            out[i] = val
            continue
        normalized = normalize_side_value(s)  # type: ignore[arg-type]
        out[i] = _SIDE_BUY if normalized == OrderSide.BUY else _SIDE_SELL
    return out


def _order_type_array_to_str(order_types: np.ndarray) -> np.ndarray:
    """将订单类型数组归一化为大写字符串数组。

    Args:
        order_types: 由 OrderType/字符串组成的数组。

    Returns:
        str 数组（'MARKET'/'LIMIT'/'STOP'/'STOP_LIMIT'）。

    Raises:
        ValueError: 包含无法识别或不受支持的订单类型时抛出。
    """
    out = np.empty(order_types.size, dtype=object)
    for i, t in enumerate(order_types):
        normalized = normalize_order_type(t)  # type: ignore[arg-type]
        out[i] = normalized.value
    return out


# ─────────────────────────────────────────────
# Fill - 单笔撮合结果
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class Fill:
    """单笔订单撮合结果（标量）。

    Attributes:
        order_id  : 订单 ID。
        symbol    : 标的代码。
        side      : 订单方向。
        order_type: 订单类型。
        quantity  : 成交数量。
        price     : 基础成交价（未含滑点，滑点由 fee_common 叠加）。
        timestamp : 成交时间戳。
        strategy_id: 来源策略 ID。
    """

    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: float
    timestamp: float
    strategy_id: str = ""

    def __repr__(self) -> str:
        return (
            f"Fill({self.order_id[:8]}.. {self.symbol} {self.side.value} "
            f"{self.order_type.value} {self.quantity:.0f}@{self.price:.2f})"
        )


# ─────────────────────────────────────────────
# VectorizedFillResult - 批量撮合结果
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class VectorizedFillResult:
    """批量订单撮合结果（向量化）。

    各字段为与输入等长的 numpy 数组，逐元素对应。

    Attributes:
        fill_prices    : 成交价数组（未成交为 NaN）。
        fill_quantities: 成交数量数组（未成交为 0）。
        filled_mask    : 是否成交的布尔数组。
    """

    fill_prices: np.ndarray
    fill_quantities: np.ndarray
    filled_mask: np.ndarray

    def __len__(self) -> int:
        """订单数量。"""
        return int(self.filled_mask.size)

    def __repr__(self) -> str:
        return (
            f"VectorizedFillResult(n={len(self)}, "
            f"filled={int(np.count_nonzero(self.filled_mask))})"
        )


# ─────────────────────────────────────────────
# OrderMatcher - 统一撮合器
# ─────────────────────────────────────────────

class OrderMatcher:
    """统一订单撮合器（三引擎共用）。

    提供标量与向量化两套接口，成交判定与成交价计算逻辑完全一致。

    用法:
        matcher = OrderMatcher()
        # 标量（event/mixed 引擎）
        fill = matcher.match(order=order, bar=bar)
        # 向量化（vectorized 引擎批量）
        result = matcher.match_vectorized(
            sides=sides, order_types=types, limit_prices=lp,
            stop_prices=sp, opens=opens, highs=highs, lows=lows,
            quantities=qtys,
        )
    """

    def __init__(
        self,
        enable_partial_fill: bool = False,
        price_limit_pct: float = 0.10,
        enable_price_limit: bool = True,
    ):
        """初始化撮合器。

        Args:
            enable_partial_fill: 是否允许按 max_fill_qty 部分成交
                （True 时标量 match 接受 max_fill_qty 限制成交量）。
            price_limit_pct: 涨跌停幅度（默认 0.10 = ±10%，主板标准）。
                ST 股 0.05、科创板/创业板 0.20 可由调用方按标的传入。
            enable_price_limit: 是否启用涨跌停校验（默认 True）。
                停牌校验（volume=0）始终启用。
        """
        self.enable_partial_fill = bool(enable_partial_fill)
        self.price_limit_pct = float(price_limit_pct)
        self.enable_price_limit = bool(enable_price_limit)
        logger.debug(
            "OrderMatcher 初始化: partial_fill=%s, price_limit=%s(±%.0f%%)",
            self.enable_partial_fill, self.enable_price_limit,
            self.price_limit_pct * 100,
        )

    # ─────────────────────────────────────────
    # 标量撮合
    # ─────────────────────────────────────────

    def match(
        self,
        order: Order,
        bar: Bar,
        max_fill_qty: Optional[float] = None,
    ) -> Optional[Fill]:
        """撮合单个订单与单根 bar。

        Args:
            order: 委托订单（状态须为活跃：SUBMITTED/PARTIAL_FILLED）。
            bar: 当前 K 线（用其 open/high/low 判定与定价）。
            max_fill_qty: 成交量上限（仅 enable_partial_fill=True 时生效，
                None 表示不限制，默认全量成交剩余数量）。

        Returns:
            成交结果 Fill；未成交返回 None。

        Raises:
            ValueError: 订单或 bar 数据非法时抛出。
        """
        if not isinstance(order, Order):
            raise TypeError(f"order 必须为 Order 实例，收到: {type(order).__name__}")
        if not isinstance(bar, Bar):
            raise TypeError(f"bar 必须为 Bar 实例，收到: {type(bar).__name__}")
        order_type = normalize_order_type(order.order_type)
        side = normalize_side_value(order.side)

        # 停牌/涨跌停前置校验（蓝图要求：撮合层处理涨跌停）
        halt_reason = self._check_halt_and_limit(side, bar)
        if halt_reason is not None:
            logger.debug(
                "撮合跳过[%s] %s %s: %s",
                order.order_id[:8], order.symbol, side.value, halt_reason,
            )
            return None

        # 本行使用的数据截至 t 时刻（仅当前 bar 的 open/high/low）
        open_px = float(bar.open)
        high_px = float(bar.high)
        low_px = float(bar.low)

        limit_px = float(order.price) if order.price is not None else float("nan")
        stop_px = float(order.stop_price) if order.stop_price is not None else float("nan")

        fill_price = self._match_price(
            side=side,
            order_type=order_type,
            limit_px=limit_px,
            stop_px=stop_px,
            open_px=open_px,
            high_px=high_px,
            low_px=low_px,
        )
        if fill_price is None:
            return None

        # 成交数量：默认剩余数量；允许部分成交时受 max_fill_qty 限制
        qty = float(order.remaining_qty)
        if self.enable_partial_fill and max_fill_qty is not None:
            qty = min(qty, float(max_fill_qty))
        if qty <= 0:
            return None

        return Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=side,
            order_type=order_type,
            quantity=qty,
            price=fill_price,
            timestamp=bar.timestamp,
            strategy_id=order.strategy_id,
        )

    def _check_halt_and_limit(
        self,
        side: OrderSide,
        bar: Bar,
    ) -> Optional[str]:
        """检查停牌与涨跌停限制，返回阻止原因；可交易返回 None。

        本行使用的数据截至 t 时刻：仅当前 bar 的 volume/open/pre_close。

        规则（A股写实，与风控层 PriceLimitRule 一致）：
          - 停牌（volume=0）：买卖均不成交
          - 涨停开盘（open >= pre_close×(1+limit_pct)）：买单不成交
          - 跌停开盘（open <= pre_close×(1-limit_pct)）：卖单不成交
          - pre_close<=0 时跳过涨跌停检查（数据不足）

        Args:
            side: 订单方向。
            bar: 当前 K 线。

        Returns:
            阻止原因字符串；可交易时返回 None。
        """
        # 停牌：成交量为 0（数据层约定 volume==0 标记停牌）
        if bar.volume <= 0:
            return "停牌(volume=0)"

        # 涨跌停（需要 pre_close；enable_price_limit=False 时不校验也不抛错）
        # KP-P2-10/T-02：fail-closed——涨跌停校验启用时 pre_close 缺失必须显式报错，
        # 禁止"pre_close<=0 静默跳过"导致约束恒空转（数据层应已派生前收盘价；
        # 首根 bar 无前驱时本方法不会被调用，见引擎 T+1 撮合时机）。
        if self.enable_price_limit:
            if bar.pre_close <= 0:
                raise ValueError(
                    "pre_close 缺失，涨跌停校验不可空转（数据层需派生前收盘价）"
                )
            limit_up = bar.pre_close * (1.0 + self.price_limit_pct)
            limit_down = bar.pre_close * (1.0 - self.price_limit_pct)
            if side == OrderSide.BUY and bar.open >= limit_up - _PRICE_EPSILON:
                return f"涨停开盘({bar.open:.2f}>={limit_up:.2f})"
            if side == OrderSide.SELL and bar.open <= limit_down + _PRICE_EPSILON:
                return f"跌停开盘({bar.open:.2f}<={limit_down:.2f})"

        return None

    @staticmethod
    def _match_price(
        side: OrderSide,
        order_type: OrderType,
        limit_px: float,
        stop_px: float,
        open_px: float,
        high_px: float,
        low_px: float,
    ) -> Optional[float]:
        """计算基础成交价（未含滑点）。

        本行使用的数据截至 t 时刻：仅当前 bar 的 open/high/low 与订单价格参数。

        Args:
            side: 订单方向。
            order_type: 订单类型。
            limit_px: 限价（非 LIMIT/STOP_LIMIT 单为 NaN）。
            stop_px: 触发价（非 STOP/STOP_LIMIT 单为 NaN）。
            open_px: 当前 bar 开盘价。
            high_px: 当前 bar 最高价。
            low_px: 当前 bar 最低价。

        Returns:
            基础成交价；未成交返回 None。
        """
        if order_type == OrderType.MARKET:
            # 市价单：以开盘价成交
            return open_px

        if order_type == OrderType.LIMIT:
            if side == OrderSide.BUY:
                # 买入限价单：最低价 <= 限价才成交，价 = min(限价, 开盘价)
                if low_px <= limit_px + _PRICE_EPSILON:
                    return min(limit_px, open_px)
            else:
                # 卖出限价单：最高价 >= 限价才成交，价 = max(限价, 开盘价)
                if high_px >= limit_px - _PRICE_EPSILON:
                    return max(limit_px, open_px)
            return None

        if order_type == OrderType.STOP:
            if side == OrderSide.BUY:
                # 止损买入（向上突破）：最高价 >= 触发价，价 = max(触发价, 开盘价)
                if high_px >= stop_px - _PRICE_EPSILON:
                    return max(stop_px, open_px)
            else:
                # 止损卖出（向下跌破）：最低价 <= 触发价，价 = min(触发价, 开盘价)
                if low_px <= stop_px + _PRICE_EPSILON:
                    return min(stop_px, open_px)
            return None

        if order_type == OrderType.STOP_LIMIT:
            if side == OrderSide.BUY:
                # 触发（最高价 >= 触发价）且限价不低于最低价
                if high_px >= stop_px - _PRICE_EPSILON and limit_px >= low_px - _PRICE_EPSILON:
                    return min(limit_px, max(open_px, stop_px))
            else:
                # 触发（最低价 <= 触发价）且限价不高于最高价
                if low_px <= stop_px + _PRICE_EPSILON and limit_px <= high_px + _PRICE_EPSILON:
                    return max(limit_px, min(open_px, stop_px))
            return None

        logger.warning("未知订单类型 %s，不成交", order_type)
        return None

    # ─────────────────────────────────────────
    # 向量化撮合
    # ─────────────────────────────────────────

    def match_vectorized(
        self,
        sides: np.ndarray,
        order_types: np.ndarray,
        limit_prices: np.ndarray,
        stop_prices: np.ndarray,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        quantities: np.ndarray,
        pre_closes: Optional[np.ndarray] = None,
        volumes: Optional[np.ndarray] = None,
    ) -> VectorizedFillResult:
        """批量撮合订单（同一根 bar 或逐根对应）。

        所有数组须等长，逐元素为一笔订单。成交判定与成交价计算
        与标量 match 完全一致（数学等价，自测验证）。

        Args:
            sides: 方向数组（OrderSide/'BUY'/'SELL'/0/1）。
            order_types: 订单类型数组（OrderType/'MARKET'/'LIMIT'/'STOP'/'STOP_LIMIT'）。
            limit_prices: 限价数组（非 LIMIT/STOP_LIMIT 单可传 NaN）。
            stop_prices: 触发价数组（非 STOP/STOP_LIMIT 单可传 NaN）。
            opens: 开盘价数组。
            highs: 最高价数组。
            lows: 最低价数组。
            quantities: 委托数量数组。
            pre_closes: 昨收价数组（可选，传入后启用涨跌停校验）。
                None 时跳过涨跌停检查（向后兼容）。
            volumes: 成交量数组（可选，传入后启用停牌校验）。
                None 时跳过停牌检查（向后兼容）。

        Returns:
            批量撮合结果 VectorizedFillResult。

        Raises:
            ValueError: 数组长度不一致或含非法值时抛出。
        """
        arrays = {
            "sides": np.asarray(sides, dtype=object),
            "order_types": np.asarray(order_types, dtype=object),
            "limit_prices": np.asarray(limit_prices, dtype=np.float64),
            "stop_prices": np.asarray(stop_prices, dtype=np.float64),
            "opens": np.asarray(opens, dtype=np.float64),
            "highs": np.asarray(highs, dtype=np.float64),
            "lows": np.asarray(lows, dtype=np.float64),
            "quantities": np.asarray(quantities, dtype=np.float64),
        }
        n = arrays["opens"].size
        for name, arr in arrays.items():
            if arr.size != n:
                raise ValueError(
                    f"数组 {name} 长度 {arr.size} 与 opens 长度 {n} 不一致"
                )
        if n == 0:
            return VectorizedFillResult(
                fill_prices=np.zeros(0, dtype=np.float64),
                fill_quantities=np.zeros(0, dtype=np.float64),
                filled_mask=np.zeros(0, dtype=bool),
            )

        side_int = _side_array_to_int(arrays["sides"])
        type_str = _order_type_array_to_str(arrays["order_types"])

        # 本行使用的数据截至 t 时刻（仅当前 bar 的 open/high/low）
        open_px = arrays["opens"]
        high_px = arrays["highs"]
        low_px = arrays["lows"]
        limit_px = arrays["limit_prices"]
        stop_px = arrays["stop_prices"]

        is_buy = side_int == _SIDE_BUY
        is_sell = ~is_buy
        is_market = type_str == "MARKET"
        is_limit = type_str == "LIMIT"
        is_stop = type_str == "STOP"
        is_stop_limit = type_str == "STOP_LIMIT"

        fill_price = np.full(n, np.nan, dtype=np.float64)

        # MARKET：以开盘价成交
        fill_price[is_market] = open_px[is_market]

        # LIMIT
        limit_buy = is_limit & is_buy
        mask = limit_buy & (low_px <= limit_px + _PRICE_EPSILON)
        fill_price[mask] = np.minimum(limit_px[mask], open_px[mask])
        limit_sell = is_limit & is_sell
        mask = limit_sell & (high_px >= limit_px - _PRICE_EPSILON)
        fill_price[mask] = np.maximum(limit_px[mask], open_px[mask])

        # STOP
        stop_buy = is_stop & is_buy
        mask = stop_buy & (high_px >= stop_px - _PRICE_EPSILON)
        fill_price[mask] = np.maximum(stop_px[mask], open_px[mask])
        stop_sell = is_stop & is_sell
        mask = stop_sell & (low_px <= stop_px + _PRICE_EPSILON)
        fill_price[mask] = np.minimum(stop_px[mask], open_px[mask])

        # STOP_LIMIT
        sl_buy = is_stop_limit & is_buy
        mask = (
            sl_buy
            & (high_px >= stop_px - _PRICE_EPSILON)
            & (limit_px >= low_px - _PRICE_EPSILON)
        )
        fill_price[mask] = np.minimum(
            limit_px[mask], np.maximum(open_px[mask], stop_px[mask])
        )
        sl_sell = is_stop_limit & is_sell
        mask = (
            sl_sell
            & (low_px <= stop_px + _PRICE_EPSILON)
            & (limit_px <= high_px + _PRICE_EPSILON)
        )
        fill_price[mask] = np.maximum(
            limit_px[mask], np.minimum(open_px[mask], stop_px[mask])
        )

        # ── 停牌/涨跌停过滤（与标量 _check_halt_and_limit 逻辑一致）──
        # 停牌：volume=0 → 全部不成交
        if volumes is not None:
            vol_arr = np.asarray(volumes, dtype=np.float64)
            if vol_arr.size != n:
                raise ValueError(
                    f"volumes 长度 {vol_arr.size} 与 opens 长度 {n} 不一致"
                )
            halted = vol_arr <= 0
            if np.any(halted):
                fill_price[halted] = np.nan
                logger.debug("向量化撮合: %d 笔因停牌(volume=0)跳过", int(np.sum(halted)))

        # 涨跌停：涨停买单不成交 / 跌停卖单不成交
        if self.enable_price_limit and pre_closes is not None:
            pc_arr = np.asarray(pre_closes, dtype=np.float64)
            if pc_arr.size != n:
                raise ValueError(
                    f"pre_closes 长度 {pc_arr.size} 与 opens 长度 {n} 不一致"
                )
            valid_pc = pc_arr > 0
            limit_up = pc_arr * (1.0 + self.price_limit_pct)
            limit_down = pc_arr * (1.0 - self.price_limit_pct)
            limit_up_block = valid_pc & is_buy & (open_px >= limit_up - _PRICE_EPSILON)
            limit_down_block = valid_pc & is_sell & (open_px <= limit_down + _PRICE_EPSILON)
            blocked = limit_up_block | limit_down_block
            if np.any(blocked):
                fill_price[blocked] = np.nan
                logger.debug(
                    "向量化撮合: %d 笔因涨跌停跳过(涨停买=%d, 跌停卖=%d)",
                    int(np.sum(blocked)),
                    int(np.sum(limit_up_block)),
                    int(np.sum(limit_down_block)),
                )

        filled_mask = ~np.isnan(fill_price)
        fill_qtys = np.where(filled_mask, arrays["quantities"], 0.0)

        return VectorizedFillResult(
            fill_prices=fill_price,
            fill_quantities=fill_qtys,
            filled_mask=filled_mask,
        )

    # ─────────────────────────────────────────
    # 一致性校验辅助
    # ─────────────────────────────────────────

    def assert_vectorized_consistent(
        self,
        sides: np.ndarray,
        order_types: np.ndarray,
        limit_prices: np.ndarray,
        stop_prices: np.ndarray,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        quantities: np.ndarray,
    ) -> bool:
        """校验向量化撮合与逐单标量撮合结果完全一致。

        Args:
            同 match_vectorized 的参数说明。

        Returns:
            完全一致返回 True。

        Raises:
            ValueError: 任一元素不一致时抛出（携带具体位置）。
        """
        n = int(opens.size)
        # 一致性校验：标量与向量化必须使用一致的 volume/pre_close（KP-P2-10/T-02：
        # 不再用 pre_close=0 跳过涨跌停，改用统一有效前收盘价 10.0，两侧判定一致）。
        result = self.match_vectorized(
            sides=sides,
            order_types=order_types,
            limit_prices=limit_prices,
            stop_prices=stop_prices,
            opens=opens,
            highs=highs,
            lows=lows,
            quantities=quantities,
            pre_closes=np.full(n, 10.0, dtype=np.float64),
            volumes=np.full(n, 1_000_000.0, dtype=np.float64),
        )
        for i in range(n):
            order = Order(
                symbol=f"T{i}",
                side=normalize_side_value(sides[i]),
                order_type=normalize_order_type(order_types[i]),
                quantity=float(quantities[i]),
                price=(
                    None
                    if np.isnan(float(limit_prices[i]))
                    else float(limit_prices[i])
                ),
                stop_price=(
                    None
                    if np.isnan(float(stop_prices[i]))
                    else float(stop_prices[i])
                ),
            )
            order.submit()
            bar = Bar(
                symbol=f"T{i}",
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(opens[i]),
                pre_close=10.0,
                volume=1_000_000.0,
                timestamp=float(i),
            )
            fill = self.match(order=order, bar=bar)
            vec_price = result.fill_prices[i]
            if fill is None:
                if not np.isnan(vec_price):
                    raise ValueError(
                        f"第 {i} 笔：标量未成交但向量化成交价={vec_price}"
                    )
                continue
            if np.isnan(vec_price) or abs(fill.price - vec_price) > 1e-9:
                raise ValueError(
                    f"第 {i} 笔成交价不一致: 标量={fill.price}, 向量化={vec_price}"
                )
            if abs(fill.quantity - result.fill_quantities[i]) > 1e-9:
                raise ValueError(
                    f"第 {i} 笔成交量不一致: 标量={fill.quantity}, "
                    f"向量化={result.fill_quantities[i]}"
                )
        return True

    def __repr__(self) -> str:
        return f"OrderMatcher(partial_fill={self.enable_partial_fill})"


# ─────────────────────────────────────────────
# 模块级便捷函数
# ─────────────────────────────────────────────

_DEFAULT_MATCHER: Optional[OrderMatcher] = None


def get_default_matcher() -> OrderMatcher:
    """获取全局默认撮合器（惰性单例）。

    Returns:
        全局 OrderMatcher 实例。
    """
    global _DEFAULT_MATCHER
    if _DEFAULT_MATCHER is None:
        _DEFAULT_MATCHER = OrderMatcher()
    return _DEFAULT_MATCHER


def match_order(order: Order, bar: Bar) -> Optional[Fill]:
    """模块级便捷函数：撮合单个订单。

    Args:
        order: 委托订单。
        bar: 当前 K 线。

    Returns:
        成交结果 Fill；未成交返回 None。
    """
    return get_default_matcher().match(order=order, bar=bar)


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

def _run_self_test() -> int:
    """运行 order_matcher 模块自测。

    Returns:
        通过的测试项数量。
    """
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 60)
    print("order_matcher 自测开始")
    print("=" * 60)

    matcher = OrderMatcher()

    def make_order(
        otype: str,
        side: str,
        qty: float = 100.0,
        price: Optional[float] = None,
        stop: Optional[float] = None,
    ) -> Order:
        order = Order(
            symbol="600519.SH",
            side=OrderSide(side),
            order_type=OrderType(otype),
            quantity=qty,
            price=price,
            stop_price=stop,
        )
        order.submit()
        return order

    def make_bar(
        open_px: float,
        high_px: float,
        low_px: float,
        # KP-P2-10/T-02：默认给有效前收盘价（10.0），基础撮合用例不会误触发
        # fail-closed；涨跌停/空转用例显式传 pre_close 控制行为。
        pre_close: float = 10.0,
        volume: float = 1_000_000.0,
    ) -> Bar:
        return Bar(
            symbol="600519.SH",
            open=open_px,
            high=high_px,
            low=low_px,
            close=open_px,
            pre_close=pre_close,
            volume=volume,
            timestamp=1_700_000_000.0,
        )

    # 1. MARKET 买卖以开盘价成交
    f1 = matcher.match(make_order("MARKET", "BUY"), make_bar(10.0, 11.0, 9.5))
    check("MARKET 买入以开盘价成交", f1 is not None and abs(f1.price - 10.0) < 1e-9, f"价格={f1}")
    f2 = matcher.match(make_order("MARKET", "SELL"), make_bar(10.0, 11.0, 9.5))
    check("MARKET 卖出以开盘价成交", f2 is not None and abs(f2.price - 10.0) < 1e-9, f"价格={f2}")

    # 2. LIMIT 买入可成交：low <= limit → min(limit, open)
    f3 = matcher.match(
        make_order("LIMIT", "BUY", price=10.5), make_bar(10.0, 11.0, 9.5)
    )
    check("LIMIT 买 可成交价=min(限价,开盘)", f3 is not None and abs(f3.price - 10.0) < 1e-9, f"价格={f3}")
    f4 = matcher.match(
        make_order("LIMIT", "BUY", price=10.5), make_bar(11.0, 11.5, 10.8)
    )
    check("LIMIT 买 不可成交（low>limit）返回 None", f4 is None, f"结果={f4}")

    # 3. LIMIT 卖出可成交：high >= limit → max(limit, open)
    f5 = matcher.match(
        make_order("LIMIT", "SELL", price=10.2), make_bar(10.0, 10.8, 9.8)
    )
    check("LIMIT 卖 可成交价=max(限价,开盘)", f5 is not None and abs(f5.price - 10.2) < 1e-9, f"价格={f5}")
    f6 = matcher.match(
        make_order("LIMIT", "SELL", price=10.2), make_bar(10.0, 10.1, 9.8)
    )
    check("LIMIT 卖 不可成交（high<limit）返回 None", f6 is None, f"结果={f6}")

    # 4. STOP 买入触发（向上突破）
    f7 = matcher.match(
        make_order("STOP", "BUY", stop=10.2), make_bar(10.0, 10.5, 9.8)
    )
    check("STOP 买 触发价=max(触发,开盘)", f7 is not None and abs(f7.price - 10.2) < 1e-9, f"价格={f7}")
    f8 = matcher.match(
        make_order("STOP", "BUY", stop=10.2), make_bar(10.0, 10.1, 9.8)
    )
    check("STOP 买 未触发返回 None", f8 is None, f"结果={f8}")

    # 5. STOP 卖出触发（向下跌破）
    f9 = matcher.match(
        make_order("STOP", "SELL", stop=9.8), make_bar(10.0, 10.5, 9.5)
    )
    check("STOP 卖 触发价=min(触发,开盘)", f9 is not None and abs(f9.price - 9.8) < 1e-9, f"价格={f9}")
    f10 = matcher.match(
        make_order("STOP", "SELL", stop=9.8), make_bar(10.0, 10.5, 9.9)
    )
    check("STOP 卖 未触发返回 None", f10 is None, f"结果={f10}")

    # 6. STOP_LIMIT 触发且限价满足
    f11 = matcher.match(
        make_order("STOP_LIMIT", "BUY", price=10.3, stop=10.2),
        make_bar(10.0, 10.5, 9.8),
    )
    check(
        "STOP_LIMIT 买 触发成交",
        f11 is not None and abs(f11.price - 10.2) < 1e-9,
        f"价格={f11}",
    )
    f12 = matcher.match(
        make_order("STOP_LIMIT", "BUY", price=9.7, stop=10.2),
        make_bar(10.0, 10.5, 9.8),
    )
    check(
        "STOP_LIMIT 买 限价低于最低价不可成交",
        f12 is None,
        f"结果={f12}",
    )
    f13 = matcher.match(
        make_order("STOP_LIMIT", "SELL", price=9.9, stop=9.8),
        make_bar(10.0, 10.5, 9.5),
    )
    check(
        "STOP_LIMIT 卖 触发成交",
        f13 is not None and abs(f13.price - 9.9) < 1e-9,
        f"价格={f13}",
    )

    # 7. 部分成交 max_fill_qty
    pm = OrderMatcher(enable_partial_fill=True)
    f14 = pm.match(
        make_order("MARKET", "BUY", qty=1000), make_bar(10.0, 11.0, 9.5), max_fill_qty=300
    )
    check(
        "部分成交 max_fill_qty=300",
        f14 is not None and abs(f14.quantity - 300.0) < 1e-9,
        f"数量={f14.quantity if f14 else None}",
    )

    # 8. 向量化 vs 标量逐项一致（混合组合）
    rng = np.random.default_rng(7)
    n = 2000
    types_all = np.array(["MARKET", "LIMIT", "STOP", "STOP_LIMIT"])
    sides_all = np.array(["BUY", "SELL"])
    order_types = rng.choice(types_all, size=n)
    sides = rng.choice(sides_all, size=n)
    opens = rng.uniform(5.0, 50.0, size=n)
    highs = opens * rng.uniform(1.0, 1.1, size=n)
    lows = opens * rng.uniform(0.9, 1.0, size=n)
    # 限价/触发价围绕开盘价分布，覆盖成交与不成交
    limit_prices = opens * rng.uniform(0.95, 1.05, size=n)
    stop_prices = opens * rng.uniform(0.95, 1.05, size=n)
    quantities = rng.uniform(100, 10000, size=n).round()
    try:
        matcher.assert_vectorized_consistent(
            sides=sides,
            order_types=order_types,
            limit_prices=limit_prices,
            stop_prices=stop_prices,
            opens=opens,
            highs=highs,
            lows=lows,
            quantities=quantities,
        )
        check("向量化 vs 标量撮合一致（2000 笔混合）", True)
    except ValueError as exc:
        check("向量化 vs 标量撮合一致（2000 笔混合）", False, str(exc))

    # 9. 与原有 SimBroker 的 MARKET/LIMIT 成交价一致（真值基准对照）
    from myquant.execution.sim_broker import SimBroker
    from myquant.models.backtest import SlippageModel

    broker = SimBroker(
        initial_capital=1_000_000.0,
        slippage_model=SlippageModel(type="none"),  # 关闭滑点便于对比基础成交价
    )
    broker.connect()
    legacy_ok = True
    for otype, side, px, o, h, l, expect in [
        ("MARKET", "BUY", None, 10.0, 11.0, 9.5, 10.0),
        ("LIMIT", "BUY", 10.5, 10.0, 11.0, 9.5, 10.0),
        ("LIMIT", "SELL", 10.2, 10.0, 10.8, 9.8, 10.2),
        ("LIMIT", "SELL", 10.2, 10.0, 10.1, 9.8, None),
    ]:
        order = make_order(otype, side, price=px)
        bar = make_bar(o, h, l)
        new_fill = matcher.match(order=order, bar=bar)
        # SimBroker 重新构造订单提交撮合
        b_order = Order(
            symbol="600519.SH", side=OrderSide(side),
            order_type=OrderType(otype), quantity=100.0, price=px,
        )
        broker.submit_order(b_order)
        trades = broker.on_bar(bar)
        legacy_price = trades[0].price if trades else None
        new_price = new_fill.price if new_fill else None
        if (legacy_price is None) != (new_price is None) or (
            legacy_price is not None and abs(legacy_price - new_price) > 1e-9
        ):
            legacy_ok = False
            failures.append(
                f"与 SimBroker 不一致: {otype}/{side} legacy={legacy_price} new={new_price}"
            )
        broker.reset()
    check("与原有 SimBroker MARKET/LIMIT 成交价一致", legacy_ok, "存在不一致")

    # 9b. 停牌校验（volume=0 → 买卖均不成交）
    f_halt_buy = matcher.match(
        make_order("MARKET", "BUY"),
        make_bar(10.0, 11.0, 9.5, pre_close=10.0, volume=0),
    )
    check("停牌(volume=0) 买单不成交", f_halt_buy is None, f"结果={f_halt_buy}")
    f_halt_sell = matcher.match(
        make_order("MARKET", "SELL"),
        make_bar(10.0, 11.0, 9.5, pre_close=10.0, volume=0),
    )
    check("停牌(volume=0) 卖单不成交", f_halt_sell is None, f"结果={f_halt_sell}")

    # 9c. 涨跌停校验（pre_close=10, limit=10% → 涨停11.0 / 跌停9.0）
    # 涨停开盘：买单不成交，卖单可成交
    f_limit_up_buy = matcher.match(
        make_order("MARKET", "BUY"),
        make_bar(11.0, 11.5, 11.0, pre_close=10.0),
    )
    check("涨停开盘(+10%) 买单不成交", f_limit_up_buy is None, f"结果={f_limit_up_buy}")
    f_limit_up_sell = matcher.match(
        make_order("MARKET", "SELL"),
        make_bar(11.0, 11.5, 11.0, pre_close=10.0),
    )
    check(
        "涨停开盘(+10%) 卖单可成交",
        f_limit_up_sell is not None and abs(f_limit_up_sell.price - 11.0) < 1e-9,
        f"结果={f_limit_up_sell}",
    )
    # 跌停开盘：卖单不成交，买单可成交
    f_limit_down_sell = matcher.match(
        make_order("MARKET", "SELL"),
        make_bar(9.0, 9.0, 8.5, pre_close=10.0),
    )
    check("跌停开盘(-10%) 卖单不成交", f_limit_down_sell is None, f"结果={f_limit_down_sell}")
    f_limit_down_buy = matcher.match(
        make_order("MARKET", "BUY"),
        make_bar(9.0, 9.0, 8.5, pre_close=10.0),
    )
    check(
        "跌停开盘(-10%) 买单可成交",
        f_limit_down_buy is not None and abs(f_limit_down_buy.price - 9.0) < 1e-9,
        f"结果={f_limit_down_buy}",
    )
    # KP-P2-10/T-02：pre_close<=0 且启用涨跌停校验 → fail-closed 显式抛错，禁止静默空转
    try:
        matcher.match(
            make_order("MARKET", "BUY"),
            make_bar(11.0, 11.5, 11.0, pre_close=0.0),
        )
        check(
            "pre_close=0 且启用涨跌停 → 应抛错（fail-closed，禁止空转）",
            False,
            "未抛出 ValueError",
        )
    except ValueError:
        check(
            "pre_close=0 且启用涨跌停 → 应抛错（fail-closed，禁止空转）",
            True,
            "抛出 ValueError",
        )

    # 9d. 向量化涨跌停/停牌一致性
    vec_n = 6
    vec_result = matcher.match_vectorized(
        sides=np.array(["BUY", "SELL", "BUY", "SELL", "BUY", "SELL"]),
        order_types=np.full(vec_n, "MARKET"),
        limit_prices=np.full(vec_n, np.nan),
        stop_prices=np.full(vec_n, np.nan),
        opens=np.array([11.0, 11.0, 9.0, 9.0, 10.0, 10.0]),
        highs=np.array([11.5, 11.5, 9.0, 9.0, 10.5, 10.5]),
        lows=np.array([11.0, 11.0, 8.5, 8.5, 9.5, 9.5]),
        quantities=np.full(vec_n, 100.0),
        pre_closes=np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0]),
        volumes=np.array([1e6, 1e6, 1e6, 1e6, 0, 0]),  # 最后两笔停牌
    )
    # 预期：第0笔(涨停买)不成交、第2笔(跌停买)成交、第3笔(跌停卖)不成交、
    #       第1笔(涨停卖)成交、第4/5笔(停牌)不成交
    expected_filled = [False, True, True, False, False, False]
    vec_ok = all(
        bool(vec_result.filled_mask[i]) == expected_filled[i]
        for i in range(vec_n)
    )
    check(
        "向量化涨跌停/停牌一致性(6笔)",
        vec_ok,
        f"filled_mask={vec_result.filled_mask.tolist()}",
    )

    # 10. 非法参数防御
    try:
        matcher.match(make_order("MARKET", "BUY"), "not_a_bar")  # type: ignore[arg-type]
        check("bar 类型非法抛 TypeError", False, "未抛出")
    except TypeError:
        check("bar 类型非法抛 TypeError", True)
    try:
        matcher.match("not_an_order", make_bar(1, 2, 0.5))  # type: ignore[arg-type]
        check("order 类型非法抛 TypeError", False, "未抛出")
    except TypeError:
        check("order 类型非法抛 TypeError", True)
    try:
        normalize_order_type("TRAILING_STOP")
        check("TRAILING_STOP 不支持抛 ValueError", False, "未抛出")
    except ValueError:
        check("TRAILING_STOP 不支持抛 ValueError", True)
    try:
        matcher.match_vectorized(
            sides=np.array(["BUY"]),
            order_types=np.array(["MARKET"]),
            limit_prices=np.array([np.nan]),
            stop_prices=np.array([np.nan]),
            opens=np.array([1.0]),
            highs=np.array([2.0]),
            lows=np.array([0.5]),
            quantities=np.array([100.0, 200.0]),  # 长度不一致
        )
        check("数组长度不一致抛 ValueError", False, "未抛出")
    except ValueError:
        check("数组长度不一致抛 ValueError", True)

    # 11. 空数组安全
    empty = matcher.match_vectorized(
        sides=np.array([]),
        order_types=np.array([]),
        limit_prices=np.array([]),
        stop_prices=np.array([]),
        opens=np.array([]),
        highs=np.array([]),
        lows=np.array([]),
        quantities=np.array([]),
    )
    check("空数组返回空结果", len(empty) == 0, f"长度={len(empty)}")

    # 12. 模块级便捷函数
    mf = match_order(make_order("MARKET", "SELL"), make_bar(10.0, 11.0, 9.5))
    check(
        "模块级 match_order 可用",
        mf is not None and abs(mf.price - 10.0) < 1e-9,
        f"结果={mf}",
    )

    print("=" * 60)
    if failures:
        print(f"自测结果: {passed} 项通过, {len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return len(failures)
    print(f"自测结果: 全部 {passed} 项通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    # 自测 Demo：运行全部校验项
    raise SystemExit(_run_self_test())
