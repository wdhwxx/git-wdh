# 文件名：myquant/strategies/base.py
"""
strategies/base.py - 策略抽象基类与股票池构建器
==================================================
定义策略统一接口、交易信号数据结构、三因子股票池筛选器。

设计原则（规则2：为实盘无缝对接准备）:
  - StrategyBase 定义统一生命周期：on_init → on_bar/on_tick(循环) → on_stop
  - 回测引擎与实盘引擎共用同一策略实例，切换零修改
  - 策略通过上下文(context)访问数据、组合、执行器，不直接依赖具体引擎
  - StockPoolBuilder 提供代码过滤 + 三因子交集筛选，回测/实盘共用

防未来函数（规则4）:
  - on_bar 接收的bar视为"已收盘"数据，信号在收盘后生成
  - T日生成的信号，T+1日开盘成交（由引擎层控制调用时机）
  - 三因子筛选使用截至T日的历史数据，不访问未来
  - RSRS等指标本身已防未来函数（iloc[i-period:i]不含第i根）

三因子股票池标准（交接文档7.2节）:
  - 波动率因子：20日波动率，取前1/3低波动
  - 价格因子：高价股，取前1/3
  - 动量因子：60日动量，取前1/3高动量
  - 三因子交集 → 最终股票池
  - 单票最大仓位20%，最多持有5只，初始资金100万

代码过滤标准（用户指定）:
  - 剔除科创板：688
  - 剔除创业板：300, 301
  - 剔除北交所：4, 8
  - 额外剔除：43, 83, 87, 88, 89, 92
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.models.market_data import Bar
from myquant.models.trade import Order, OrderSide, OrderType, Position

logger = get_logger(__name__)


# ═══════════════════════════════════════════════
#  交易信号
# ═══════════════════════════════════════════════

class SignalType(Enum):
    """信号类型"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"

    def __str__(self) -> str:
        return self.value


@dataclass
class Signal:
    """
    交易信号

    策略生成信号后，由引擎或策略本身转换为订单。
    """
    symbol: str
    signal_type: SignalType = SignalType.HOLD
    strength: float = 0.0
    price: float = 0.0
    timestamp: float = field(default_factory=time.time)
    reason: str = ""
    strategy_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_buy(self) -> bool:
        return self.signal_type == SignalType.BUY

    @property
    def is_sell(self) -> bool:
        return self.signal_type in (SignalType.SELL, SignalType.STOP_LOSS, SignalType.TAKE_PROFIT)

    @property
    def is_hold(self) -> bool:
        return self.signal_type == SignalType.HOLD

    def to_order(self, quantity: float, order_type: OrderType = OrderType.MARKET,
                  price: Optional[float] = None) -> Optional[Order]:
        """将信号转换为订单"""
        if self.is_hold:
            return None
        side = OrderSide.BUY if self.is_buy else OrderSide.SELL
        return Order(
            symbol=self.symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            strategy_id=self.strategy_id,
            tag=self.reason,
        )

    def __repr__(self) -> str:
        return (
            f"Signal({self.symbol} {self.signal_type.value} "
            f"strength={self.strength:.2f} price={self.price:.2f} "
            f"reason={self.reason})"
        )


# ═══════════════════════════════════════════════
#  策略上下文
# ═══════════════════════════════════════════════

@dataclass
class StrategyContext:
    """
    策略上下文

    引擎在调用策略方法时传入，策略通过上下文访问数据、组合、执行器。
    回测/实盘切换时，上下文实现不同，但策略代码零修改。
    """
    data: Dict[str, pd.DataFrame] = field(default_factory=dict)
    portfolio: Any = None
    executor: Any = None
    clock: Any = None
    config: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    def get_data(self, symbol: str) -> Optional[pd.DataFrame]:
        return self.data.get(symbol)

    def get_position(self, symbol: str) -> Optional[Position]:
        if self.portfolio is None:
            return None
        if hasattr(self.portfolio, 'get_position'):
            return self.portfolio.get_position(symbol)
        positions = getattr(self.portfolio, 'positions', {})
        return positions.get(symbol)

    def get_cash(self) -> float:
        if self.portfolio is None:
            return 0.0
        # available_cash 可能是属性或方法（mixed 引擎 portfolio 提供 available_cash()），统一处理
        avail = getattr(self.portfolio, 'available_cash', None)
        if avail is not None:
            try:
                return float(avail() if callable(avail) else avail)
            except (TypeError, ValueError):
                return 0.0
        raw = getattr(self.portfolio, 'cash', 0.0)
        try:
            return float(raw() if callable(raw) else raw)
        except (TypeError, ValueError):
            return 0.0

    def submit_order(self, order: Order) -> Order:
        if self.executor is None:
            logger.warning("上下文无执行器，订单未提交")
            return order
        return self.executor.submit_order(order)


# ═══════════════════════════════════════════════
#  策略抽象基类
# ═══════════════════════════════════════════════

class StrategyBase(ABC):
    """
    策略抽象基类

    所有策略必须继承此类，实现 generate_signal 方法。

    生命周期: on_init → on_bar/on_tick(循环) → on_stop
    """

    name: str = "base_strategy"

    def __init__(self, **params: Any):
        self.params: Dict[str, Any] = params
        self._initialized = False
        self._signal_count = 0
        self._trade_count = 0
        # ★ 信号过滤器参数（通用，所有策略共用，回测/实盘一致）
        # min_volume_ratio: 买入信号最低量比（0=不启用），量比=当日成交量/过去N日均量
        # volume_ratio_period: 量比计算的均量周期（默认5日）
        self.min_volume_ratio: float = float(params.get("min_volume_ratio", 0.0))
        self.volume_ratio_period: int = int(params.get("volume_ratio_period", 5))
        if self.min_volume_ratio > 0:
            logger.info(
                f"量比过滤已启用: min_volume_ratio={self.min_volume_ratio}, "
                f"period={self.volume_ratio_period}日"
            )
        logger.info(f"策略初始化: {self.name}, 参数: {params}")

    def on_init(self, context: StrategyContext) -> None:
        """初始化钩子"""
        self._initialized = True
        logger.info(f"策略 {self.name} 初始化完成")

    @abstractmethod
    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        """生成交易信号（核心抽象方法）"""
        pass

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """每根bar处理（默认实现：调用 generate_signal → 通用信号过滤）"""
        signal = self.generate_signal(bar, context)
        if signal is None:
            return []
        # ★ 通用信号过滤（量比等，回测/实盘共用）
        signal = self._apply_signal_filters(signal, bar, context)
        if signal and not signal.is_hold:
            self._signal_count += 1
            return [signal]
        return []


    # ═══════════════════════════════════════════════
    #  三引擎接口（第6项：策略基类三接口）
    #  on_bar / generate_mixed_loop / generate_signals_vectorized
    #  - on_bar  : event 引擎（真值基准）与 mixed 引擎的兼容回退
    #  - generate_mixed_loop : mixed 引擎（生产主力，目标持仓模式）
    #  - generate_signals_vectorized : vectorized 引擎（纯向量化，参数扫描）
    #  未实现的接口默认抛 NotImplementedError（明确提示适用引擎），
    #  引擎通过 supports_mode() / engine_modes() 探测策略能力。
    # ═══════════════════════════════════════════════

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        """混合引擎目标持仓接口（mixed_engine 专用）。

        每根 bar 调用一次，基于截至 T 日收盘的数据返回目标持仓股数，
        订单由引擎在 T+1 日开盘撮合（防未来函数）。

        默认实现抛 NotImplementedError：策略须显式实现本接口才能使用
        mixed 引擎的目标持仓模式（否则回退 on_bar 事件信号模式）。

        Args:
            bar: 当前 bar（T 日，已收盘）。
            context: 策略上下文。
            position: 当前持仓（无持仓为 None）。

        Returns:
            目标持仓股数（float）。

        Raises:
            NotImplementedError: 策略未实现本接口时抛出（明确提示）。
        """
        raise NotImplementedError(
            f"策略 {self.name} 未实现 generate_mixed_loop，无法使用 mixed 引擎的"
            "目标持仓模式。请在策略中实现该接口（返回目标持仓股数），"
            "或由引擎回退 on_bar 事件信号模式。"
        )

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """纯向量化引擎信号接口（vectorized_engine 专用）。

        一次性基于全量 DataFrame 输出目标持仓股数数组（长度 = len(data)），
        面向大批量参数扫描。T 日元素表示 T 日收盘决定的目标持仓，
        由引擎在 T+1 日开盘撮合（防未来函数）。

        默认实现抛 NotImplementedError：策略须显式实现本接口才能使用
        vectorized 引擎（否则调度器不会选择该模式）。

        Args:
            data: 单标的行情 DataFrame（open/high/low/close/volume，
                  index 为日期，按时间升序）。

        Returns:
            目标持仓股数数组（numpy.ndarray，长度 = len(data)）。

        Raises:
            NotImplementedError: 策略未实现本接口时抛出（明确提示）。
        """
        raise NotImplementedError(
            f"策略 {self.name} 未实现 generate_signals_vectorized，无法用于 "
            "vectorized 引擎。请在策略中实现该接口（基于全量 DataFrame "
            "输出目标持仓股数数组）。"
        )

    def _is_method_overridden(self, method_name: str) -> bool:
        """判断指定方法是否被子类覆盖（非 StrategyBase 基类默认实现）。

        Args:
            method_name: 方法名。

        Returns:
            True 表示子类覆盖了该方法。
        """
        for cls in type(self).__mro__:
            if method_name in vars(cls):
                return cls is not StrategyBase
        return False

    def supports_mode(self, mode: str) -> bool:
        """判断策略是否支持指定引擎模式。

        Args:
            mode: 引擎模式（"event"/"mixed"/"vectorized"）。

        Returns:
            True 表示支持。

        Raises:
            ValueError: 模式名非法时抛出。
        """
        mode_key = mode.strip().lower()
        if mode_key == "event":
            # on_bar（基类默认实现）+ generate_signal（抽象必实现）恒可用
            return True
        if mode_key == "mixed":
            return self._is_method_overridden("generate_mixed_loop")
        if mode_key == "vectorized":
            return self._is_method_overridden("generate_signals_vectorized")
        raise ValueError(f"未知引擎模式: {mode!r}（支持 event/mixed/vectorized）")

    def engine_modes(self) -> List[str]:
        """返回策略支持的引擎模式列表（按推荐优先级降序）。

        优先级: vectorized（性能）> mixed（生产主力）> event（真值基准）。

        Returns:
            支持的模式名列表，例如 ["event"]、["event", "mixed"]、["event", "mixed", "vectorized"]。
        """
        modes: List[str] = ["event"]
        if self._is_method_overridden("generate_mixed_loop"):
            modes.append("mixed")
        if self._is_method_overridden("generate_signals_vectorized"):
            modes.append("vectorized")
        return modes

    def _target_shares_from_capital(
        self, capital_base: float, price: float, pos_pct: float,
    ) -> float:
        """按资金基准 × 单票仓位比例估算目标股数（A股整手）。

        供 mixed / vectorized 引擎的目标持仓接口复用。

        Args:
            capital_base: 资金基准（元）。
            price: 当前价格（元）。
            pos_pct: 单票仓位比例 (0,1]。

        Returns:
            目标股数（float，100 股整数倍）。

        本行仅依赖传入的资金与价格，无行情时序。
        """
        if price is None or price <= 0 or capital_base <= 0:
            return 0.0
        pct = pos_pct if pos_pct and 0 < pos_pct <= 1.0 else 0.20
        lot = float(getattr(self, "lot_size", 100))
        shares = capital_base * pct / price
        return float(np.floor(shares / lot) * lot)

    def on_tick(self, tick: Any, context: StrategyContext) -> List[Signal]:
        """每个tick处理（实盘用，默认不处理）"""
        return []

    def on_stop(self, context: StrategyContext) -> None:
        """停止钩子"""
        logger.info(f"策略 {self.name} 停止: 信号数={self._signal_count}")

    def create_order(
        self, symbol: str, side: OrderSide, quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None, reason: str = "",
    ) -> Order:
        """创建订单（快捷方法）"""
        return Order(
            symbol=symbol, side=side, order_type=order_type,
            quantity=quantity, price=price,
            strategy_id=self.name, tag=reason,
        )

    def calc_position_size(
        self, price: float, cash: float,
        max_position_pct: float = 0.20,
        max_positions: int = 5,
        current_positions: int = 0,
    ) -> int:
        """计算仓位大小（A股100股整数倍）"""
        if current_positions >= max_positions:
            return 0
        max_amount = cash * max_position_pct
        shares = int(max_amount / price / 100) * 100
        return max(0, shares)

    def check_take_profit(
        self, position: Position, current_price: float,
        take_profit_pct: float = 0.20,
    ) -> bool:
        """检查是否触发目标止盈"""
        if position.quantity <= 0 or position.avg_cost <= 0:
            return False
        return_pct = (current_price - position.avg_cost) / position.avg_cost
        return return_pct >= take_profit_pct

    def check_stop_loss(
        self, position: Position, current_price: float,
        stop_loss_pct: float = 0.10,
    ) -> bool:
        """检查是否触发止损（与 check_take_profit 对称）"""
        if position.quantity <= 0 or position.avg_cost <= 0:
            return False
        return_pct = (current_price - position.avg_cost) / position.avg_cost
        return return_pct <= -stop_loss_pct

    def _should_rebalance(
        self, context: StrategyContext, rebalance_days: int,
    ) -> bool:
        """通用调仓周期判断（基于bar计数，回测/实盘统一）

        返回 True 时策略应执行调仓逻辑。
        调仓在当前bar收盘后判断，成交由引擎在下一根bar处理（防未来）。
        """
        if not hasattr(self, '_rebalance_bar_count'):
            self._rebalance_bar_count = 0
        self._rebalance_bar_count += 1
        if self._rebalance_bar_count >= rebalance_days:
            self._rebalance_bar_count = 0
            return True
        return False

    def _get_close_series(
        self, context: StrategyContext, symbol: str,
    ) -> Optional[pd.Series]:
        """便捷获取某标的的收盘价序列（float类型，按时间升序）

        策略通过 context.data 访问历史bar，不直接读文件/数据库。
        返回 None 表示数据不足。
        """
        df = context.get_data(symbol)
        if df is None or df.empty or 'close' not in df.columns:
            return None
        return df['收盘价'].astype(float)

    def calc_volume_ratio(
        self, context: StrategyContext, symbol: str,
        period: Optional[int] = None,
    ) -> Optional[float]:
        """
        计算量比（Volume Ratio）= 当日成交量 / 过去N日平均成交量。

        防未来函数（规则4）:
          - 当日成交量 = 序列最后一根（当前bar，T日，已收盘）
          - 过去N日均量 = 序列倒数第N+1到倒数第2根（T-1到T-N，不含T日）
          - 不访问任何未来数据

        参数:
            context: 策略上下文
            symbol: 标的代码
            period: 均量周期（默认用 self.volume_ratio_period，通常5日）

        返回:
            量比值（float），数据不足返回 None
        """
        if period is None:
            period = self.volume_ratio_period
        df = context.get_data(symbol)
        if df is None or df.empty or 'volume' not in df.columns:
            return None
        volume = df['成交量'].astype(float)
        # 需要至少 period+1 根数据（当日 + 过去N日）
        if len(volume) < period + 1:
            return None
        current_vol = volume.iloc[-1]  # T日成交量（当前bar，已收盘）
        avg_vol = volume.iloc[-period - 1:-1].mean()  # T-1到T-N日均量（不含T日）
        if avg_vol <= 0 or np.isnan(avg_vol):
            return None
        return float(current_vol / avg_vol)

    def _apply_signal_filters(
        self, signal: Signal, bar: Bar, context: StrategyContext,
    ) -> Signal:
        """
        通用信号过滤器（在 generate_signal 之后、on_bar 返回之前调用）。

        当前内置过滤器:
          1. 量比过滤：买入信号且量比 < min_volume_ratio 时，改为 HOLD
             （卖出/止损/止盈信号不受量比过滤影响，避免被套牢）

        后续可扩展：换手率过滤、涨跌幅过滤、振幅过滤等。
        回测/实盘共用同一套过滤逻辑，切换零修改。

        参数:
            signal: 策略生成的原始信号
            bar: 当前bar
            context: 策略上下文

        返回:
            过滤后的信号（可能被改为 HOLD）
        """
        # 量比过滤（仅对买入信号生效）
        if self.min_volume_ratio > 0 and signal.is_buy:
            vr = self.calc_volume_ratio(context, bar.symbol)
            if vr is None:
                # 数据不足无法计算量比，保守起见放行（不阻断信号）
                logger.debug(
                    f"量比过滤: {bar.symbol} 数据不足无法计算量比，放行"
                )
            elif vr < self.min_volume_ratio:
                logger.debug(
                    f"量比过滤: {bar.symbol} 量比={vr:.2f} < "
                    f"{self.min_volume_ratio}，买入信号改为HOLD"
                )
                signal.signal_type = SignalType.HOLD
                signal.strength = 0.0
                signal.reason = f"量比不足(vr={vr:.2f}<{self.min_volume_ratio})"
        return signal

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def signal_count(self) -> int:
        return self._signal_count

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, signals={self._signal_count})"


# ═══════════════════════════════════════════════
#  三因子股票池构建器
# ═══════════════════════════════════════════════

class StockPoolBuilder:
    """
    三因子股票池构建器

    流程: 代码前缀过滤 → 计算三因子 → 各取前1/3 → 交集
    """

    DEFAULT_EXCLUDE_PREFIXES: Tuple[str, ...] = (
        "688", "300", "301", "4", "8",
        "43", "83", "87", "88", "89", "92",
    )

    def __init__(
        self,
        exclude_prefixes: Optional[Tuple[str, ...]] = None,
        volatility_period: int = 20,
        momentum_period: int = 60,
        top_ratio: float = 1.0 / 3.0,
    ):
        self.exclude_prefixes = exclude_prefixes or self.DEFAULT_EXCLUDE_PREFIXES
        self.volatility_period = volatility_period
        self.momentum_period = momentum_period
        self.top_ratio = top_ratio
        logger.info(
            f"股票池构建器初始化: 波动率={volatility_period}日, "
            f"动量={momentum_period}日, 前{top_ratio:.2%}"
        )

    def filter_by_code(self, symbols: List[str]) -> List[str]:
        """按代码前缀过滤股票"""
        filtered = []
        for symbol in symbols:
            code = symbol.split('.')[0] if '.' in symbol else symbol
            excluded = any(code.startswith(p) for p in self.exclude_prefixes)
            if not excluded:
                filtered.append(symbol)
        logger.debug(f"代码过滤: {len(symbols)} → {len(filtered)}")
        return filtered

    def calculate_factors(self, data_dict: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """计算三因子得分"""
        records = []
        for symbol, df in data_dict.items():
            if df is None or len(df) < max(self.volatility_period, self.momentum_period) + 1:
                continue
            if 'close' not in df.columns:
                continue
            close = df['收盘价'].astype(float)

            # 波动率：20日收益率标准差
            returns = close.pct_change().dropna()
            volatility = returns.tail(self.volatility_period).std()

            # 价格：最新收盘价
            price = close.iloc[-1]

            # 动量：60日收益率
            momentum = (close.iloc[-1] / close.iloc[-self.momentum_period - 1]) - 1

            if not np.isnan(volatility) and not np.isnan(momentum):
                records.append({
                    'symbol': symbol,
                    'volatility': volatility,
                    'price': price,
                    'momentum': momentum,
                })

        # 防御：所有标的数据过短/缺 close 列/因子为 NaN 时 records 为空，
        # 空 DataFrame 无列，直接 set_index('symbol') 会抛 KeyError（批量全市场场景实测）
        if not records:
            return pd.DataFrame(index=pd.Index([], name="symbol"))

        factor_df = pd.DataFrame(records).set_index('symbol')
        logger.debug(f"三因子计算: {len(factor_df)} 只有完整因子")
        return factor_df

    def select_by_intersection(self, factor_df: pd.DataFrame) -> List[str]:
        """三因子交集筛选"""
        if factor_df.empty:
            return []
        n = len(factor_df)
        top_n = max(1, int(n * self.top_ratio))

        vol_top = set(factor_df.nsmallest(top_n, 'volatility').index)
        price_top = set(factor_df.nlargest(top_n, 'price').index)
        mom_top = set(factor_df.nlargest(top_n, 'momentum').index)

        pool = list(vol_top & price_top & mom_top)
        logger.info(
            f"三因子交集: 总{n} → 低波动{len(vol_top)} ∩ 高价{len(price_top)} "
            f"∩ 高动量{len(mom_top)} = {len(pool)}只"
        )
        return sorted(pool)

    def build(self, symbols: List[str], data_dict: Dict[str, pd.DataFrame]) -> List[str]:
        """完整构建股票池：代码过滤 → 三因子计算 → 交集筛选"""
        filtered = self.filter_by_code(symbols)
        valid_data = {s: data_dict[s] for s in filtered if s in data_dict and data_dict[s] is not None}
        factor_df = self.calculate_factors(valid_data)
        return self.select_by_intersection(factor_df)
