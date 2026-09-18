# 文件名：myquant/backtest/fee_common.py
"""
backtest/fee_common.py - 全局唯一费用计算入口
==============================================
三模式回测引擎（vectorized / mixed / event）共用的费用计算模块，
作为费用的唯一真相来源（Single Source of Truth），彻底消除多引擎费用逻辑漂移。

功能清单:
  1. 标量接口  : 单笔成交的佣金 / 印花税 / 过户费 / 滑点明细
  2. 向量化接口: 批量成交逐元素计算（numpy），面向 vectorized 引擎大批量参数扫描
  3. 与 CommissionModel / SlippageModel 完全一致（event 引擎作为真值基准）
  4. 配置外置  : 通过 CommissionModel / SlippageModel 实例注入，禁止硬编码费率

设计原则:
  - 费用只依赖成交本身（方向/金额/数量/价格），不依赖任何行情时间序列，无未来函数风险；
  - 标量路径委托 CommissionModel / SlippageModel，保证 event 引擎基准不变；
  - 向量化路径与标量路径数学等价（自测逐元素验证一致）；
  - RATIO / FIXED 模式直接向量化；TIERED 阶梯模式（现有 CommissionModel 未提供阶梯表）回退逐元素标量。

A股交易成本标准（交接文档7.1节）:
  - 佣金  : 万2（0.0002），最低 5 元，买卖双向
  - 印花税: 千1（0.001），仅卖出
  - 过户费: 万0.1（0.00001），买卖双向
  - 滑点  : 万2（0.0002），买卖双向

依赖:
  标准库: dataclasses, enum, logging, typing
  第三方: numpy
  本地  : myquant.models.backtest (CommissionModel, SlippageModel, CommissionType)
          myquant.models.trade   (OrderSide)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Union

import numpy as np

from myquant.models.backtest import CommissionModel, CommissionType, SlippageModel
from myquant.models.trade import OrderSide

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# side 字符串归一化映射（防御式输入）
_SIDE_ALIASES: Dict[str, OrderSide] = {
    "BUY": OrderSide.BUY,
    "B": OrderSide.BUY,
    "买": OrderSide.BUY,
    "买入": OrderSide.BUY,
    "LONG": OrderSide.BUY,
    "SELL": OrderSide.SELL,
    "S": OrderSide.SELL,
    "卖": OrderSide.SELL,
    "卖出": OrderSide.SELL,
    "SHORT": OrderSide.SELL,
}

# 浮点比较容差（向量化 vs 标量一致性校验用）
_FEE_EPSILON: float = 1e-9

# 方向数组整数编码（0=买入, 1=卖出），用于混合方向批量计算
_SIDE_BUY: int = 0
_SIDE_SELL: int = 1


# ─────────────────────────────────────────────
# 输入归一化
# ─────────────────────────────────────────────

def normalize_side(side: Union[OrderSide, str]) -> OrderSide:
    """将方向输入归一化为 OrderSide 枚举。

    Args:
        side: OrderSide 枚举，或 'BUY'/'SELL'/'买'/'卖' 等字符串。

    Returns:
        归一化后的 OrderSide 枚举。

    Raises:
        ValueError: 传入的方向无法识别时抛出。
    """
    if isinstance(side, OrderSide):
        return side
    if isinstance(side, str):
        key = side.strip().upper()
        resolved = _SIDE_ALIASES.get(key)
        if resolved is not None:
            return resolved
    raise ValueError(f"无法识别的订单方向: {side!r}（支持 BUY/SELL 或其中英文别名）")


def normalize_side_array(sides: np.ndarray) -> np.ndarray:
    """将方向数组归一化为整数编码数组（0=买入, 1=卖出）。

    用于混合方向的批量费用计算（vectorized 引擎买卖同批处理）。

    Args:
        sides: 由 OrderSide / 字符串 / 整数组成的数组。

    Returns:
        int8 数组（0=买入, 1=卖出）。

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
        direction = normalize_side(s)  # type: ignore[arg-type]
        out[i] = _SIDE_BUY if direction == OrderSide.BUY else _SIDE_SELL
    return out


def _validate_notional(notional: float) -> float:
    """校验成交金额非负且为有限数值。

    Args:
        notional: 成交金额（quantity * price）。

    Returns:
        校验通过后的金额。

    Raises:
        ValueError: 金额为负、NaN 或无穷时抛出。
    """
    value = float(notional)
    if value < 0:
        raise ValueError(f"成交金额不能为负: {notional!r}")
    if not np.isfinite(value):
        raise ValueError(f"成交金额必须为有限数值: {notional!r}")
    return value


# ─────────────────────────────────────────────
# FeeBreakdown - 单笔成交费用明细（标量）
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class FeeBreakdown:
    """单笔成交费用明细（标量）。

    Attributes:
        commission  : 佣金（含最低 5 元约束）。
        stamp_tax   : 印花税（仅卖出，买入为 0）。
        transfer_fee: 过户费（买卖双向）。
        slippage    : 滑点金额（每股价差 × 数量）。
    """

    commission: float
    stamp_tax: float
    transfer_fee: float
    slippage: float

    @property
    def total_commission(self) -> float:
        """费用合计（佣金 + 印花税 + 过户费）。"""
        return self.commission + self.stamp_tax + self.transfer_fee

    @property
    def total_cost(self) -> float:
        """总成本（费用合计 + 滑点）。"""
        return self.total_commission + self.slippage

    def to_dict(self) -> Dict[str, float]:
        """序列化为字典。"""
        return {
            "commission": self.commission,
            "stamp_tax": self.stamp_tax,
            "transfer_fee": self.transfer_fee,
            "slippage": self.slippage,
            "total_commission": self.total_commission,
            "total_cost": self.total_cost,
        }

    def __repr__(self) -> str:
        return (
            f"FeeBreakdown(佣金={self.commission:.4f} 印花税={self.stamp_tax:.4f} "
            f"过户费={self.transfer_fee:.4f} 滑点={self.slippage:.4f} "
            f"合计={self.total_cost:.4f})"
        )


# ─────────────────────────────────────────────
# VectorizedFeeResult - 批量成交费用结果（向量化）
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class VectorizedFeeResult:
    """批量成交费用结果（向量化）。

    各字段为与输入 notional 等长的 numpy 数组，逐元素对应。
    输入为空数组时各字段为空数组。
    """

    commission: np.ndarray
    stamp_tax: np.ndarray
    transfer_fee: np.ndarray
    slippage: np.ndarray

    @property
    def total_commission(self) -> np.ndarray:
        """费用合计数组（佣金 + 印花税 + 过户费）。"""
        return self.commission + self.stamp_tax + self.transfer_fee

    @property
    def total_cost(self) -> np.ndarray:
        """总成本数组（费用合计 + 滑点）。"""
        return self.total_commission + self.slippage

    def __len__(self) -> int:
        """批量成交笔数。"""
        return int(self.commission.size)

    def to_dict(self) -> Dict[str, np.ndarray]:
        """序列化为字典（numpy 数组）。"""
        return {
            "commission": self.commission,
            "stamp_tax": self.stamp_tax,
            "transfer_fee": self.transfer_fee,
            "slippage": self.slippage,
            "total_commission": self.total_commission,
            "total_cost": self.total_cost,
        }


# ─────────────────────────────────────────────
# FeeCalculator - 全局唯一费用计算入口
# ─────────────────────────────────────────────

class FeeCalculator:
    """全局唯一费用计算入口。

    三模式回测引擎（vectorized / mixed / event）均通过本类计算费用，
    禁止在引擎内部各自实现费用逻辑，以彻底消除多引擎费用逻辑漂移。

    用法:
        calc = FeeCalculator(commission_model=CommissionModel(), slippage_model=SlippageModel())
        fee = calc.calc_fee(side="BUY", notional=10000.0, price=10.0, quantity=1000)
        fees = calc.calc_fee_vectorized(side="SELL", notional=np.array([...]), ...)
    """

    def __init__(
        self,
        commission_model: Optional[CommissionModel] = None,
        slippage_model: Optional[SlippageModel] = None,
    ):
        """初始化费用计算器。

        Args:
            commission_model: 手续费模型（None 时使用 A股写实默认模型）。
            slippage_model: 滑点模型（None 时使用默认 2 基点固定滑点）。

        Raises:
            TypeError: 传入的模型类型不合法时抛出。
        """
        if commission_model is not None and not isinstance(commission_model, CommissionModel):
            raise TypeError(
                f"commission_model 必须为 CommissionModel 实例，收到: {type(commission_model).__name__}"
            )
        if slippage_model is not None and not isinstance(slippage_model, SlippageModel):
            raise TypeError(
                f"slippage_model 必须为 SlippageModel 实例，收到: {type(slippage_model).__name__}"
            )
        self.commission_model: CommissionModel = commission_model or CommissionModel()
        self.slippage_model: SlippageModel = slippage_model or SlippageModel()
        logger.debug(
            "FeeCalculator 初始化: 佣金模式=%s, 滑点模式=%s",
            self.commission_model.type.value,
            self.slippage_model.type,
        )

    # ─────────────────────────────────────────
    # 标量接口
    # ─────────────────────────────────────────

    def calc_commission_breakdown(
        self, side: Union[OrderSide, str], notional: float
    ) -> Tuple[float, float, float]:
        """计算单笔成交的佣金/印花税/过户费明细（标量）。

        数学定义与 CommissionModel.calc_buy / calc_sell 完全一致，
        event 引擎（真值基准）的总费用 = commission + stamp_tax + transfer_fee。

        Args:
            side: 订单方向（BUY/SELL 或其别名）。
            notional: 成交金额 = quantity * price。

        Returns:
            (commission, stamp_tax, transfer_fee) 三元组。

        Raises:
            ValueError: 方向或金额非法时抛出。
        """
        direction = normalize_side(side)
        amount = _validate_notional(notional)
        model = self.commission_model

        if direction == OrderSide.BUY:
            # 本行使用的数据截至 t 时刻（仅依赖成交金额与模型参数）
            commission = self._calc_commission_scalar(
                rate=model.buy_rate, amount=amount
            )
            return commission, 0.0, amount * model.transfer_fee
        # 卖出：佣金 + 印花税 + 过户费
        commission = self._calc_commission_scalar(
            rate=model.sell_rate, amount=amount
        )
        stamp_tax = amount * model.stamp_tax
        transfer_fee = amount * model.transfer_fee
        return commission, stamp_tax, transfer_fee

    def _calc_commission_scalar(self, rate: float, amount: float) -> float:
        """按模型类型计算佣金（标量）。

        Args:
            rate: 佣金率（买入或卖出）。
            amount: 成交金额。

        Returns:
            佣金金额。
        """
        model = self.commission_model
        if model.type == CommissionType.FIXED:
            # FIXED: rate 视为固定金额/笔（与现有 CommissionModel 行为一致）
            return max(float(rate), model.min_commission)
        # RATIO（默认）与 TIERED（现有模型未提供阶梯表，按比例处理）: 比例佣金 + 最低佣金
        return max(amount * float(rate), model.min_commission)

    def calc_slippage_per_share(
        self,
        price: float,
        volume: float = 0.0,
        order_size: float = 0.0,
    ) -> float:
        """计算每股价差滑点（标量）。

        Args:
            price: 成交价（已含滑点前的基准价）。
            volume: 当日成交量（volume_based 模式需要）。
            order_size: 订单数量（volume_based 模式需要）。

        Returns:
            每股价差滑点（元/股）。

        Raises:
            ValueError: 价格为负时抛出。
        """
        if price < 0:
            raise ValueError(f"成交价不能为负: {price!r}")
        return float(
            self.slippage_model.calc(
                price=price, volume=volume, order_size=order_size
            )
        )

    def calc_slippage_amount(
        self,
        price: float,
        quantity: float,
        volume: float = 0.0,
        order_size: float = 0.0,
    ) -> float:
        """计算整笔成交的滑点金额（标量）。

        Args:
            price: 成交价。
            quantity: 成交数量。
            volume: 当日成交量。
            order_size: 订单数量。

        Returns:
            滑点总金额 = 每股价差 × 数量。
        """
        if quantity < 0:
            raise ValueError(f"成交数量不能为负: {quantity!r}")
        per_share = self.calc_slippage_per_share(
            price=price, volume=volume, order_size=order_size
        )
        return per_share * quantity

    def calc_fee(
        self,
        side: Union[OrderSide, str],
        notional: float,
        price: float = 0.0,
        quantity: float = 0.0,
        volume: float = 0.0,
        order_size: float = 0.0,
    ) -> FeeBreakdown:
        """一站式计算单笔成交的完整费用明细（标量）。

        Args:
            side: 订单方向（BUY/SELL 或其别名）。
            notional: 成交金额。
            price: 成交价（滑点计算用，可传 0 表示忽略滑点）。
            quantity: 成交数量（滑点计算用）。
            volume: 当日成交量（volume_based 滑点模式用）。
            order_size: 订单数量（volume_based 滑点模式用）。

        Returns:
            完整费用明细 FeeBreakdown。

        Raises:
            ValueError: 方向或金额非法时抛出。
        """
        commission, stamp_tax, transfer_fee = self.calc_commission_breakdown(
            side=side, notional=notional
        )
        slippage = self.calc_slippage_amount(
            price=price,
            quantity=quantity,
            volume=volume,
            order_size=order_size,
        )
        return FeeBreakdown(
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage=slippage,
        )

    # ─────────────────────────────────────────
    # 向量化接口
    # ─────────────────────────────────────────

    def calc_commission_breakdown_vectorized(
        self,
        side: Union[OrderSide, str, np.ndarray, list],
        notional: Union[np.ndarray, list],
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """批量计算佣金/印花税/过户费明细（向量化）。

        side 支持两种形态：
          - 标量（OrderSide / 'BUY' / 'SELL' 等）：整批同向；
          - 数组（长度与 notional 一致）：逐元素方向，买卖混合批处理。

        RATIO / FIXED 模式直接 numpy 向量化；TIERED 模式回退逐元素标量。
        逐元素与标量接口 calc_commission_breakdown 数学等价。

        Args:
            side: 订单方向（标量整批同向，或数组逐元素）。
            notional: 成交金额数组（非负）。

        Returns:
            (commission_arr, stamp_tax_arr, transfer_fee_arr) 三元组 numpy 数组。

        Raises:
            ValueError: 方向非法、金额含负值或 side 数组长度不匹配时抛出。
        """
        amounts = np.asarray(notional, dtype=np.float64)
        if amounts.size == 0:
            return (
                np.zeros(0, dtype=np.float64),
                np.zeros(0, dtype=np.float64),
                np.zeros(0, dtype=np.float64),
            )
        if np.any(amounts < 0):
            raise ValueError(f"成交金额数组不能包含负值: {amounts!r}")
        if not np.all(np.isfinite(amounts)):
            raise ValueError("成交金额数组必须全部为有限数值")

        # side 归一化：标量 → 整批同向；数组 → 逐元素
        if isinstance(side, (str, OrderSide)):
            direction = normalize_side(side)
            buy_mask = np.full(amounts.size, direction == OrderSide.BUY, dtype=bool)
        else:
            side_arr = np.asarray(side, dtype=object)
            if side_arr.size != amounts.size:
                raise ValueError(
                    f"side 数组长度 {side_arr.size} 与 notional 长度 {amounts.size} 不匹配"
                )
            side_int = normalize_side_array(side_arr)
            buy_mask = side_int == _SIDE_BUY
        sell_mask = ~buy_mask

        model = self.commission_model

        # 佣金：RATIO 直接向量化；FIXED 为常量；TIERED 回退逐元素标量
        commission = np.empty(amounts.size, dtype=np.float64)
        if model.type == CommissionType.RATIO:
            commission[buy_mask] = np.maximum(
                amounts[buy_mask] * float(model.buy_rate), model.min_commission
            )
            commission[sell_mask] = np.maximum(
                amounts[sell_mask] * float(model.sell_rate), model.min_commission
            )
        elif model.type == CommissionType.FIXED:
            buy_fixed = max(float(model.buy_rate), model.min_commission)
            sell_fixed = max(float(model.sell_rate), model.min_commission)
            commission = np.where(buy_mask, buy_fixed, sell_fixed)
        else:  # TIERED: 逐元素标量回退
            commission = np.fromiter(
                (
                    self._calc_commission_scalar(
                        rate=(
                            model.buy_rate
                            if normalize_side(side_i) == OrderSide.BUY
                            else model.sell_rate
                        ),
                        amount=float(a),
                    )
                    for side_i, a in zip(
                        np.where(buy_mask, OrderSide.BUY.value, OrderSide.SELL.value),
                        amounts,
                    )
                ),
                dtype=np.float64,
            )

        stamp_tax = np.where(sell_mask, amounts * float(model.stamp_tax), 0.0)
        transfer_fee = amounts * float(model.transfer_fee)
        return commission, stamp_tax, transfer_fee

    def calc_slippage_per_share_vectorized(
        self,
        price: Union[np.ndarray, list],
        volume: Optional[Union[np.ndarray, list]] = None,
        order_size: Optional[Union[np.ndarray, list]] = None,
    ) -> np.ndarray:
        """批量计算每股价差滑点（向量化）。

        Args:
            price: 成交价数组（非负）。
            volume: 当日成交量数组（volume_based 模式需要）。
            order_size: 订单数量数组（volume_based 模式需要）。

        Returns:
            每股价差滑点数组。

        Raises:
            ValueError: 价格含负值时抛出。
        """
        prices = np.asarray(price, dtype=np.float64)
        if prices.size == 0:
            return np.zeros(0, dtype=np.float64)
        if np.any(prices < 0):
            raise ValueError(f"成交价数组不能包含负值: {prices!r}")

        model = self.slippage_model
        mode = model.type
        if mode == "none":
            return np.zeros_like(prices)
        if mode == "fixed_bps":
            # 本行使用的数据截至 t 时刻（仅依赖成交价与滑点参数）
            return prices * float(model.fixed_bps) / 10000.0
        if mode == "volume_based":
            vols = (
                np.asarray(volume, dtype=np.float64)
                if volume is not None
                else np.zeros_like(prices)
            )
            sizes = (
                np.asarray(order_size, dtype=np.float64)
                if order_size is not None
                else np.zeros_like(prices)
            )
            # volume<=0 的位置回退固定 bps 滑点，避免除零
            impact = np.divide(
                sizes,
                vols,
                out=np.zeros_like(prices),
                where=vols > 0,
            )
            base = prices * float(model.fixed_bps) / 10000.0
            return np.where(
                vols > 0,
                prices * float(model.volume_impact) * impact,
                base,
            )
        logger.warning("未知滑点模式 %r，按零滑点处理", mode)
        return np.zeros_like(prices)

    def calc_fee_vectorized(
        self,
        side: Union[OrderSide, str],
        notional: Union[np.ndarray, list],
        price: Optional[Union[np.ndarray, list]] = None,
        quantity: Optional[Union[np.ndarray, list]] = None,
        volume: Optional[Union[np.ndarray, list]] = None,
        order_size: Optional[Union[np.ndarray, list]] = None,
    ) -> VectorizedFeeResult:
        """一站式批量计算完整费用明细（向量化）。

        Args:
            side: 订单方向（BUY/SELL 或其别名，整批同向）。
            notional: 成交金额数组。
            price: 成交价数组（滑点计算用，None 时滑点为 0）。
            quantity: 成交数量数组（滑点金额 = 每股价差 × 数量）。
            volume: 当日成交量数组。
            order_size: 订单数量数组。

        Returns:
            完整费用明细 VectorizedFeeResult。

        Raises:
            ValueError: 方向、金额或数组长度不匹配时抛出。
        """
        amounts = np.asarray(notional, dtype=np.float64)
        commission, stamp_tax, transfer_fee = self.calc_commission_breakdown_vectorized(
            side=side, notional=amounts
        )
        if price is None:
            slippage = np.zeros_like(amounts)
        else:
            prices = np.asarray(price, dtype=np.float64)
            if prices.shape != amounts.shape:
                raise ValueError(
                    f"price 数组形状 {prices.shape} 与 notional 形状 {amounts.shape} 不匹配"
                )
            per_share = self.calc_slippage_per_share_vectorized(
                price=prices, volume=volume, order_size=order_size
            )
            if quantity is None:
                slippage = per_share
            else:
                qtys = np.asarray(quantity, dtype=np.float64)
                if qtys.shape != amounts.shape:
                    raise ValueError(
                        f"quantity 数组形状 {qtys.shape} 与 notional 形状 {amounts.shape} 不匹配"
                    )
                if np.any(qtys < 0):
                    raise ValueError("成交数量数组不能包含负值")
                slippage = per_share * qtys
        return VectorizedFeeResult(
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage=slippage,
        )

    # ─────────────────────────────────────────
    # 一致性校验辅助
    # ─────────────────────────────────────────

    def assert_vectorized_consistent(
        self,
        side: Union[OrderSide, str],
        notional: np.ndarray,
        price: Optional[np.ndarray] = None,
        quantity: Optional[np.ndarray] = None,
        volume: Optional[np.ndarray] = None,
        order_size: Optional[np.ndarray] = None,
    ) -> bool:
        """校验向量化结果与逐元素标量结果完全一致。

        用于自测与 CI，确保两套实现无漂移。

        Args:
            side: 订单方向。
            notional: 成交金额数组。
            price: 成交价数组（可选）。
            quantity: 成交数量数组（可选）。
            volume: 当日成交量数组（可选）。
            order_size: 订单数量数组（可选）。

        Returns:
            完全一致返回 True，否则返回 False。

        Raises:
            ValueError: 任一元素不一致时抛出（携带具体位置）。
        """
        vec = self.calc_fee_vectorized(
            side=side,
            notional=notional,
            price=price,
            quantity=quantity,
            volume=volume,
            order_size=order_size,
        )
        n = len(notional)
        for i in range(n):
            scalar = self.calc_fee(
                side=side,
                notional=float(notional[i]),
                price=float(price[i]) if price is not None else 0.0,
                quantity=float(quantity[i]) if quantity is not None else 0.0,
                volume=float(volume[i]) if volume is not None else 0.0,
                order_size=float(order_size[i]) if order_size is not None else 0.0,
            )
            if (
                abs(scalar.commission - vec.commission[i]) > _FEE_EPSILON
                or abs(scalar.stamp_tax - vec.stamp_tax[i]) > _FEE_EPSILON
                or abs(scalar.transfer_fee - vec.transfer_fee[i]) > _FEE_EPSILON
                or abs(scalar.slippage - vec.slippage[i]) > _FEE_EPSILON
            ):
                raise ValueError(
                    f"第 {i} 笔向量化与标量不一致: "
                    f"scalar={scalar.to_dict()}, "
                    f"vec=(佣金={vec.commission[i]}, 印花税={vec.stamp_tax[i]}, "
                    f"过户费={vec.transfer_fee[i]}, 滑点={vec.slippage[i]})"
                )
        return True

    def __repr__(self) -> str:
        return (
            f"FeeCalculator(佣金={self.commission_model.type.value}, "
            f"买率={self.commission_model.buy_rate}, 卖率={self.commission_model.sell_rate}, "
            f"印花税={self.commission_model.stamp_tax}, 过户费={self.commission_model.transfer_fee}, "
            f"滑点={self.slippage_model.type})"
        )


# ─────────────────────────────────────────────
# 模块级便捷函数（三引擎可直接函数式调用）
# ─────────────────────────────────────────────

_DEFAULT_CALCULATOR: Optional[FeeCalculator] = None


def get_default_calculator() -> FeeCalculator:
    """获取全局默认费用计算器（惰性单例）。

    Returns:
        全局 FeeCalculator 实例。
    """
    global _DEFAULT_CALCULATOR
    if _DEFAULT_CALCULATOR is None:
        _DEFAULT_CALCULATOR = FeeCalculator()
    return _DEFAULT_CALCULATOR


def calculate_fee(
    side: Union[OrderSide, str],
    notional: float,
    commission_model: Optional[CommissionModel] = None,
    slippage_model: Optional[SlippageModel] = None,
    price: float = 0.0,
    quantity: float = 0.0,
    volume: float = 0.0,
    order_size: float = 0.0,
) -> FeeBreakdown:
    """模块级便捷函数：计算单笔成交完整费用（标量）。

    Args:
        side: 订单方向。
        notional: 成交金额。
        commission_model: 手续费模型（None 使用默认）。
        slippage_model: 滑点模型（None 使用默认）。
        price: 成交价（滑点用）。
        quantity: 成交数量（滑点用）。
        volume: 当日成交量。
        order_size: 订单数量。

    Returns:
        完整费用明细 FeeBreakdown。
    """
    calc = FeeCalculator(
        commission_model=commission_model, slippage_model=slippage_model
    )
    return calc.calc_fee(
        side=side,
        notional=notional,
        price=price,
        quantity=quantity,
        volume=volume,
        order_size=order_size,
    )


def calculate_fee_vectorized(
    side: Union[OrderSide, str],
    notional: Union[np.ndarray, list],
    commission_model: Optional[CommissionModel] = None,
    slippage_model: Optional[SlippageModel] = None,
    price: Optional[Union[np.ndarray, list]] = None,
    quantity: Optional[Union[np.ndarray, list]] = None,
    volume: Optional[Union[np.ndarray, list]] = None,
    order_size: Optional[Union[np.ndarray, list]] = None,
) -> VectorizedFeeResult:
    """模块级便捷函数：批量计算完整费用（向量化）。

    Args:
        side: 订单方向。
        notional: 成交金额数组。
        commission_model: 手续费模型（None 使用默认）。
        slippage_model: 滑点模型（None 使用默认）。
        price: 成交价数组。
        quantity: 成交数量数组。
        volume: 当日成交量数组。
        order_size: 订单数量数组。

    Returns:
        完整费用明细 VectorizedFeeResult。
    """
    calc = FeeCalculator(
        commission_model=commission_model, slippage_model=slippage_model
    )
    return calc.calc_fee_vectorized(
        side=side,
        notional=notional,
        price=price,
        quantity=quantity,
        volume=volume,
        order_size=order_size,
    )


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

def _run_self_test() -> int:
    """运行 fee_common 模块自测。

    Returns:
        通过的测试项数量。
    """
    passed = 0
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 60)
    print("fee_common 自测开始")
    print("=" * 60)

    # 默认模型（A股写实标准）
    model = CommissionModel()
    slippage = SlippageModel()
    calc = FeeCalculator(commission_model=model, slippage_model=slippage)

    # 1. 标量买入费用 vs CommissionModel.calc_buy 总费用一致
    buy_breakdown = calc.calc_commission_breakdown(side="BUY", notional=100000.0)
    buy_total = buy_breakdown[0] + buy_breakdown[1] + buy_breakdown[2]
    check(
        "标量买入明细之和 == CommissionModel.calc_buy",
        abs(buy_total - model.calc_buy(100000.0)) < _FEE_EPSILON,
        f"明细和={buy_total}, calc_buy={model.calc_buy(100000.0)}",
    )

    # 2. 标量卖出费用 vs CommissionModel.calc_sell 总费用一致
    sell_breakdown = calc.calc_commission_breakdown(side="SELL", notional=100000.0)
    sell_total = sell_breakdown[0] + sell_breakdown[1] + sell_breakdown[2]
    check(
        "标量卖出明细之和 == CommissionModel.calc_sell",
        abs(sell_total - model.calc_sell(100000.0)) < _FEE_EPSILON,
        f"明细和={sell_total}, calc_sell={model.calc_sell(100000.0)}",
    )

    # 3. 印花税仅卖出（买入为 0，卖出 = 金额 × 千1）
    check(
        "买入印花税为 0",
        buy_breakdown[1] == 0.0,
        f"买入印花税={buy_breakdown[1]}",
    )
    check(
        "卖出印花税 = 金额 × 千1",
        abs(sell_breakdown[1] - 100000.0 * 0.001) < _FEE_EPSILON,
        f"卖出印花税={sell_breakdown[1]}",
    )

    # 4. 最低佣金 5 元（小额成交）
    small = calc.calc_commission_breakdown(side="BUY", notional=1000.0)
    check(
        "小额成交触发最低佣金 5 元",
        abs(small[0] - 5.0) < _FEE_EPSILON,
        f"小额佣金={small[0]}",
    )

    # 5. 滑点：固定 2 bps
    slip = calc.calc_slippage_per_share(price=10.0)
    check(
        "固定滑点 2bps: 10元 × 2/10000 = 0.002",
        abs(slip - 0.002) < _FEE_EPSILON,
        f"滑点={slip}",
    )

    # 6. 一站式 calc_fee 明细
    fee = calc.calc_fee(side="BUY", notional=100000.0, price=10.0, quantity=10000)
    check(
        "一站式 calc_fee 佣金正确",
        abs(fee.commission - buy_breakdown[0]) < _FEE_EPSILON,
        f"佣金={fee.commission}",
    )
    check(
        "一站式 calc_fee 滑点金额 = 0.002 × 10000",
        abs(fee.slippage - 20.0) < _FEE_EPSILON,
        f"滑点金额={fee.slippage}",
    )

    # 7. 向量化 vs 标量逐元素一致（RATIO + fixed_bps）
    notional_arr = np.array([1000.0, 50000.0, 100000.0, 1000000.0])
    price_arr = np.array([10.0, 20.0, 30.0, 40.0])
    qty_arr = np.array([100, 2500, 3333, 25000])
    for side_str in ("BUY", "SELL"):
        try:
            calc.assert_vectorized_consistent(
                side=side_str,
                notional=notional_arr,
                price=price_arr,
                quantity=qty_arr,
            )
            check(f"向量化 vs 标量一致（{side_str}, RATIO）", True)
        except ValueError as exc:
            check(f"向量化 vs 标量一致（{side_str}, RATIO）", False, str(exc))

    # 8. FIXED 模式向量化 vs 标量一致
    fixed_model = CommissionModel(type=CommissionType.FIXED, buy_rate=3.0, sell_rate=3.0)
    fixed_calc = FeeCalculator(commission_model=fixed_model, slippage_model=slippage)
    try:
        fixed_calc.assert_vectorized_consistent(
            side="SELL", notional=notional_arr, price=price_arr, quantity=qty_arr
        )
        check("向量化 vs 标量一致（FIXED）", True)
    except ValueError as exc:
        check("向量化 vs 标量一致（FIXED）", False, str(exc))

    # 9. volume_based 滑点向量化 vs 标量一致
    vol_model = SlippageModel(type="volume_based", fixed_bps=2.0, volume_impact=0.1)
    vol_calc = FeeCalculator(commission_model=model, slippage_model=vol_model)
    vol_arr = np.array([10000.0, 50000.0, 0.0, 100000.0])  # 含 0 成交量
    size_arr = np.array([1000.0, 2000.0, 0.0, 3000.0])
    try:
        vol_calc.assert_vectorized_consistent(
            side="BUY",
            notional=notional_arr,
            price=price_arr,
            quantity=qty_arr,
            volume=vol_arr,
            order_size=size_arr,
        )
        check("volume_based 滑点向量化 vs 标量一致", True)
    except ValueError as exc:
        check("volume_based 滑点向量化 vs 标量一致", False, str(exc))

    # 10. 非法输入防御
    try:
        calc.calc_commission_breakdown(side="HOLD", notional=1000.0)
        check("非法方向抛 ValueError", False, "未抛出")
    except ValueError:
        check("非法方向抛 ValueError", True)
    try:
        calc.calc_commission_breakdown(side="BUY", notional=-100.0)
        check("负金额抛 ValueError", False, "未抛出")
    except ValueError:
        check("负金额抛 ValueError", True)
    try:
        calc.calc_fee_vectorized(
            side="BUY",
            notional=notional_arr,
            price=np.array([1.0, 2.0]),  # 形状不匹配
        )
        check("数组形状不匹配抛 ValueError", False, "未抛出")
    except ValueError:
        check("数组形状不匹配抛 ValueError", True)
    try:
        FeeCalculator(commission_model="bad")  # type: ignore[arg-type]
        check("非法模型类型抛 TypeError", False, "未抛出")
    except TypeError:
        check("非法模型类型抛 TypeError", True)

    # 11. 模块级便捷函数
    fee_fn = calculate_fee(side="SELL", notional=200000.0, price=25.0, quantity=8000)
    check(
        "模块级 calculate_fee 与实例一致",
        abs(fee_fn.total_cost - calc.calc_fee(side="SELL", notional=200000.0, price=25.0, quantity=8000).total_cost) < _FEE_EPSILON,
        f"便捷={fee_fn.total_cost}",
    )

    # 12. 空数组安全
    empty = calc.calc_fee_vectorized(side="BUY", notional=[])
    check(
        "空数组返回空结果",
        len(empty) == 0,
        f"空数组长度={len(empty)}",
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
