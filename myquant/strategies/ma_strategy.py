# 文件名：myquant/strategies/ma_strategy.py
"""
strategies/ma_strategy.py - RSRS三因子策略
============================================
核心策略实现：三因子股票池筛选 + RSRS择时信号 + 目标止盈 + 仓位管理。

策略逻辑（交接文档7.2节）:
  1. 股票池筛选（定期调仓，如每月/每周）:
     - 代码前缀过滤（剔除科创板/创业板/北交所等）
     - 三因子交集：低波动(20日) ∩ 高价 ∩ 高动量(60日)，各取前1/3
  2. RSRS择时信号（每日）:
     - period=18, norm_window=60
     - buy_threshold=0.7: RSRS标准化值 > 0.7 买入
     - sell_threshold=-0.7: RSRS标准化值 < -0.7 卖出
  3. 目标止盈: +20% 止盈卖出
  4. 止损: -15% 止损卖出（stop_loss_pct=0 时不启用）
  4. 仓位管理: 单票最大20%，最多持有5只，初始资金100万

防未来函数（规则4）:
  - RSRS指标本身已防未来函数（斜率用 iloc[i-period:i] 不含第i根）
  - T日收盘生成信号，T+1日开盘成交（由引擎层控制调用时机）
  - 股票池在T日收盘后构建，T+1日交易
  - 目标止盈检查使用当前bar收盘价（已收盘数据）

设计原则（规则2：为实盘无缝对接准备）:
  - 继承 StrategyBase，回测/实盘共用同一策略实例
  - 通过 StrategyContext 访问数据/组合/执行器，不依赖具体引擎
  - 股票池筛选和信号生成都支持流式更新（实盘逐bar驱动）
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.indicators import RSRS
from myquant.models.market_data import Bar
from myquant.models.trade import OrderSide, OrderType, Position
from myquant.strategies.base import (
    Signal, SignalType, StrategyBase, StrategyContext, StockPoolBuilder,
)
from myquant.strategies.registry import register_strategy

logger = get_logger(__name__)


@register_strategy
class RSRSStrategy(StrategyBase):
    """
    RSRS三因子策略

    核心参数:
      rsrs_period: RSRS回归窗口（默认18）
      rsrs_norm_window: RSRS标准化窗口（默认60）
      buy_threshold: 买入阈值（默认0.7）
      sell_threshold: 卖出阈值（默认-0.7）
      take_profit_pct: 目标止盈比例（默认0.20，即+20%）
      stop_loss_pct: 止损比例（默认0.15，即-15%；0=不启用止损）
      max_position_pct: 单票最大仓位比例（默认0.20）
      max_positions: 最大持仓数（默认5）
      pool_rebalance_days: 股票池调仓周期（交易日，默认20，约每月）
      initial_capital: 初始资金（默认100万）
      pool_top_ratio: 三因子股票池各因子取前比例（默认1/3，调大可扩大交集）

    防未来函数:
      - RSRS标准化值在T日收盘后可用（基于前18根K线回归+前60个β标准化）
      - T日生成的买卖信号，T+1日开盘成交
    """

    name = "rsrs"

    def __init__(
        self,
        rsrs_period: int = 18,
        rsrs_norm_window: int = 60,
        buy_threshold: float = 0.7,
        sell_threshold: float = -0.7,
        take_profit_pct: float = 0.20,
        stop_loss_pct: float = 0.15,
        max_position_pct: float = 0.20,
        max_positions: int = 5,
        pool_rebalance_days: int = 20,
        initial_capital: float = 1_000_000.0,
        pool_top_ratio: float = 0.5,  # 各因子取前50%（原1/3太严格导致交集太小）
        **params: Any,
    ):
        # 参数校验（防御式编程）
        if rsrs_period < 2:
            raise ValueError(f"RSRS周期({rsrs_period})必须>=2")
        if rsrs_norm_window < 2:
            raise ValueError(f"RSRS标准化窗口({rsrs_norm_window})必须>=2")
        if take_profit_pct < 0:
            raise ValueError("止盈比例不能为负")
        if stop_loss_pct < 0:
            raise ValueError("止损比例不能为负")
        if not 0 < max_position_pct <= 1.0:
            raise ValueError(f"单票最大仓位({max_position_pct})必须在(0,1]")
        if max_positions < 1:
            raise ValueError(f"最大持仓数({max_positions})必须>=1")

        super().__init__(
            rsrs_period=rsrs_period,
            rsrs_norm_window=rsrs_norm_window,
            buy_threshold=buy_threshold,
            sell_threshold=sell_threshold,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            max_position_pct=max_position_pct,
            max_positions=max_positions,
            pool_rebalance_days=pool_rebalance_days,
            initial_capital=initial_capital,
            **params,
        )
        self.rsrs_period = rsrs_period
        self.rsrs_norm_window = rsrs_norm_window
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_position_pct = max_position_pct
        self.max_positions = max_positions
        self.pool_rebalance_days = pool_rebalance_days
        self.initial_capital = initial_capital
        self.pool_top_ratio = pool_top_ratio

        # RSRS指标实例（每只股票一个，流式计算用）
        self._rsrs_instances: Dict[str, RSRS] = {}

        # ★ RSRS预计算缓存（修复未来函数：每只股票的完整RSRS序列，按日期索引）
        # on_init时预计算，generate_signal时直接取当前日期的值，不再每次重算
        self._rsrs_cache: Dict[str, pd.DataFrame] = {}

        # 股票池
        self.stock_pool: List[str] = []
        self._days_since_rebalance = 0
        self._bar_count = 0

        # 股票池构建器（top_ratio=0.5：各因子取前50%，避免交集太小）
        self.pool_builder = StockPoolBuilder(top_ratio=pool_top_ratio)

        logger.info(
            f"RSRS策略初始化: period={rsrs_period}, norm={rsrs_norm_window}, "
            f"buy={buy_threshold}, sell={sell_threshold}, "
            f"止盈={take_profit_pct:.0%}, 止损={stop_loss_pct:.0%}"
            f"{'(未启用)' if stop_loss_pct == 0 else ''}, "
            f"单票仓位={max_position_pct:.0%}, "
            f"最大持仓={max_positions}, 调仓周期={pool_rebalance_days}日, "
            f"三因子前{pool_top_ratio:.0%}"
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_init(self, context: StrategyContext) -> None:
        """初始化：预热RSRS指标，构建初始股票池"""
        super().on_init(context)

        # ★ 预计算所有股票的RSRS完整序列（修复未来函数 + 性能优化）
        # 一次性计算，后续 generate_signal 直接按日期取值，不再每次重算
        for symbol, df in context.data.items():
            if df is not None and len(df) > self.rsrs_period + self.rsrs_norm_window:
                try:
                    rsrs = self._get_or_create_rsrs(symbol)
                    self._rsrs_cache[symbol] = rsrs.calculate(df)
                except Exception as e:
                    logger.debug(f"RSRS预计算失败: {symbol}, error={e}")

        logger.info(f"RSRS预计算完成: {len(self._rsrs_cache)} 只股票")

        # P0-05修复：初始股票池不在 on_init 构建（on_init 拿不到任何 bar 时点，
        # 用全量数据构建=直接用回测末日数据，未来函数+调仓空转）；
        # 改为在 on_bar 首根可交易 bar 按当时数据截断构建（见 on_bar 惰性初始化）。

        self._initialized = True

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """
        每根bar处理

        流程:
          1. 更新bar计数，检查是否需要调仓
          2. 检查持仓的目标止盈
          3. 检查持仓的RSRS卖出信号
          4. 对股票池中的股票检查RSRS买入信号
          5. 返回所有信号

        防未来函数:
          - 使用bar及历史数据，不访问未来
          - 信号在T日收盘后生成，T+1日成交
        """
        self._bar_count += 1
        signals: List[Signal] = []

        # 1. 定期调仓（重建股票池）
        # P0-05修复：股票池必须按当前 bar 时点截断数据构建（as_of=bar.timestamp），
        # 禁止用回测全量末日数据——否则因子永远取自末日、池恒定、调仓退化为空转。
        if not self.stock_pool:
            # 首根可交易bar：按当时已发生数据构建初始池（历史不足61日时池为空，属正常预热）
            self._rebalance_pool(bar, context)
            self._days_since_rebalance = 0
        else:
            self._days_since_rebalance += 1
            if self._days_since_rebalance >= self.pool_rebalance_days:
                self._rebalance_pool(bar, context)
                self._days_since_rebalance = 0

        # 2. 检查持仓：目标止盈 + RSRS卖出
        sell_signals = self._check_sell_signals(bar, context)
        signals.extend(sell_signals)

        # 3. 检查股票池：RSRS买入信号
        buy_signals = self._check_buy_signals(bar, context)
        signals.extend(buy_signals)

        if signals:
            self._signal_count += len(signals)
            for s in signals:
                logger.debug(f"信号: {s}")

        return signals

    def on_stop(self, context: StrategyContext) -> None:
        """停止：生成策略统计"""
        logger.info(
            f"RSRS策略停止: 处理bar={self._bar_count}, "
            f"信号数={self._signal_count}, 股票池大小={len(self.stock_pool)}"
        )

    # ------------------------------------------------------------------
    # 核心：信号生成（实现抽象方法）
    # ------------------------------------------------------------------

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        """
        生成单只股票的RSRS信号

        参数:
            bar: 当前K线
            context: 策略上下文

        返回:
            Signal: 交易信号
        """
        symbol = bar.symbol
        rsrs_value = self._calc_rsrs_value(symbol, bar, context)

        if np.isnan(rsrs_value):
            return Signal(symbol=symbol, signal_type=SignalType.HOLD, reason="数据不足")

        # 买入信号
        if rsrs_value > self.buy_threshold:
            return Signal(
                symbol=symbol,
                signal_type=SignalType.BUY,
                strength=min(1.0, (rsrs_value - self.buy_threshold) / 1.0),
                price=bar.close,
                reason=f"RSRS={rsrs_value:.3f} > {self.buy_threshold}",
                strategy_id=self.name,
            )

        # 卖出信号
        if rsrs_value < self.sell_threshold:
            return Signal(
                symbol=symbol,
                signal_type=SignalType.SELL,
                strength=min(1.0, (self.sell_threshold - rsrs_value) / 1.0),
                price=bar.close,
                reason=f"RSRS={rsrs_value:.3f} < {self.sell_threshold}",
                strategy_id=self.name,
            )

        return Signal(symbol=symbol, signal_type=SignalType.HOLD, reason=f"RSRS={rsrs_value:.3f}")

    # ------------------------------------------------------------------
    # 卖出信号检查（持仓股）
    # ------------------------------------------------------------------

    def _check_sell_signals(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """检查持仓股的卖出信号：止损 + 目标止盈 + RSRS卖出

        优先级：止损 > 止盈 > RSRS卖出（止损/止盈触发后直接返回）。
        stop_loss_pct=0 时不启用止损。
        """
        signals: List[Signal] = []
        symbol = bar.symbol

        position = context.get_position(symbol)
        if position is None or position.quantity <= 0:
            return signals

        # 1. 止损检查（优先于止盈，复用 base.py check_stop_loss）
        if self.stop_loss_pct > 0 and self.check_stop_loss(
            position, bar.close, self.stop_loss_pct,
        ):
            logger.warning(
                f"[RSRS止损] {symbol} 现价{bar.close:.2f} "
                f"成本{position.avg_cost:.2f} "
                f"跌幅={(bar.close/position.avg_cost-1):.1%} "
                f">= -{self.stop_loss_pct:.0%}"
            )
            signals.append(Signal(
                symbol=symbol,
                signal_type=SignalType.STOP_LOSS,
                strength=1.0,
                price=bar.close,
                reason=f"止损: 成本={position.avg_cost:.2f}, 当前={bar.close:.2f}, "
                       f"跌幅={(bar.close/position.avg_cost-1):.1%} "
                       f"<= -{self.stop_loss_pct:.0%}",
                strategy_id=self.name,
            ))
            return signals  # 止损优先，不再检查止盈/RSRS

        # 2. 目标止盈检查
        if self.check_take_profit(position, bar.close, self.take_profit_pct):
            signals.append(Signal(
                symbol=symbol,
                signal_type=SignalType.TAKE_PROFIT,
                strength=1.0,
                price=bar.close,
                reason=f"目标止盈: 成本={position.avg_cost:.2f}, 当前={bar.close:.2f}, "
                       f"涨幅={(bar.close/position.avg_cost-1):.1%} >= {self.take_profit_pct:.0%}",
                strategy_id=self.name,
            ))
            return signals  # 止盈优先，不再检查RSRS

        # 3. RSRS卖出信号
        signal = self.generate_signal(bar, context)
        if signal.is_sell:
            signals.append(signal)

        return signals

    # ------------------------------------------------------------------
    # 买入信号检查（股票池）
    # ------------------------------------------------------------------

    def _check_buy_signals(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """检查股票池中的买入信号"""
        signals: List[Signal] = []
        symbol = bar.symbol

        # 只处理股票池中的股票
        if symbol not in self.stock_pool:
            return signals

        # 已持仓的不重复买入
        position = context.get_position(symbol)
        if position is not None and position.quantity > 0:
            return signals

        # 检查当前持仓数
        current_positions = self._count_positions(context)
        if current_positions >= self.max_positions:
            return signals

        # RSRS买入信号
        signal = self.generate_signal(bar, context)
        if signal.is_buy:
            signals.append(signal)

        return signals

    # ------------------------------------------------------------------
    # 股票池调仓
    # ------------------------------------------------------------------

    def _rebalance_pool(self, bar: Bar, context: StrategyContext) -> None:
        """重新构建股票池（P0-05修复：按 bar 时点截断历史数据，禁止用回测末日数据）"""
        if not context.data:
            return
        all_symbols = list(context.data.keys())
        new_pool = self.pool_builder.build(all_symbols, context.data, as_of=bar.timestamp)
        old_count = len(self.stock_pool)
        self.stock_pool = new_pool
        logger.info(
            f"股票池调仓: {old_count} → {len(new_pool)} 只 "
            f"(第{self._bar_count}根bar)"
        )

    # ------------------------------------------------------------------
    # RSRS计算
    # ------------------------------------------------------------------

    def _get_or_create_rsrs(self, symbol: str) -> RSRS:
        """获取或创建指定股票的RSRS指标实例"""
        if symbol not in self._rsrs_instances:
            self._rsrs_instances[symbol] = RSRS(
                period=self.rsrs_period,
                norm_window=self.rsrs_norm_window,
                buy_threshold=self.buy_threshold,
                sell_threshold=self.sell_threshold,
            )
        return self._rsrs_instances[symbol]

    def _calc_rsrs_value(self, symbol: str, bar: Bar, context: StrategyContext) -> float:
        """
        计算指定股票在当前bar日期的RSRS标准化值。

        ★★★ 修复未来函数（规则4）★★★
        旧实现：每次调用 rsrs.calculate(df) 重新计算完整序列，然后取最后一个有效值。
        这导致：回测中每个bar都返回最新日期（未来）的RSRS值，严重未来函数！

        新实现：
        1. on_init时预计算所有股票的完整RSRS序列，存入 _rsrs_cache
        2. 这里直接从缓存中取当前bar.timestamp对应日期的RSRS值
        3. 如果该日期为NaN，取之前最近的有效值
        4. 缓存中没有的股票（数据不足），返回NaN

        参数:
            symbol: 标的代码
            bar: 当前K线（timestamp用于定位日期）
            context: 策略上下文

        返回:
            RSRS标准化值（float），数据不足返回NaN
        """
        # 从缓存取预计算结果
        if symbol not in self._rsrs_cache:
            return np.nan

        cached = self._rsrs_cache[symbol]
        if cached is None or len(cached) == 0 or 'rsrs' not in cached.columns:
            return np.nan

        # 将bar.timestamp转换为日期（timestamp是Unix时间戳，秒级）
        try:
            bar_date = pd.to_datetime(bar.timestamp, unit='s').normalize()
        except (ValueError, TypeError, OSError):
            # 如果timestamp不是Unix时间戳，尝试直接转换
            try:
                bar_date = pd.to_datetime(bar.timestamp).normalize()
            except Exception:
                return np.nan

        # 取当前日期的RSRS值
        if bar_date in cached.index:
            val = cached.loc[bar_date, 'rsrs']
            if not np.isnan(val):
                return float(val)

        # 当前日期没有有效值，取之前最近的有效值
        valid = cached['rsrs'].dropna()
        valid_before = valid[valid.index <= bar_date]
        if len(valid_before) > 0:
            return float(valid_before.iloc[-1])

        return np.nan

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _count_positions(self, context: StrategyContext) -> int:
        """统计当前持仓数量"""
        if context.portfolio is None:
            return 0
        if hasattr(context.portfolio, 'position_count'):
            return context.portfolio.position_count
        positions = getattr(context.portfolio, 'positions', {})
        return len([p for p in positions.values() if hasattr(p, 'quantity') and p.quantity > 0])

    def calc_buy_quantity(self, price: float, context: StrategyContext) -> int:
        """计算买入数量（基于仓位管理规则）"""
        cash = context.get_cash()
        current_positions = self._count_positions(context)
        return self.calc_position_size(
            price=price,
            cash=cash,
            max_position_pct=self.max_position_pct,
            max_positions=self.max_positions,
            current_positions=current_positions,
        )

    def __repr__(self) -> str:
        return (
            f"RSRSStrategy(pool={len(self.stock_pool)}, "
            f"bars={self._bar_count}, signals={self._signal_count})"
        )

    # ═══════════════════════════════════════════════
    #  第7项：双套信号接口（mixed / vectorized）
    #  单标的近似口径（docstring 注明）：
    #    - mixed   : RSRS 阈值 + 止损 + 目标止盈，返回目标持仓股数
    #    - vector  : RSRS 阈值 + 止损 + 目标止盈状态机（无股票池/横截面）
    # ═══════════════════════════════════════════════

    def _vec_full_shares(self, price: float) -> float:
        """按初始资金 × 单票仓位估算满仓目标股数（A股整手）。

        本行仅依赖传入价格，无行情时序。
        """
        return self._target_shares_from_capital(
            self.initial_capital, price, self.max_position_pct,
        )

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        """mixed 引擎目标持仓：RSRS 买入满仓 / 卖出清仓 / 止损清仓 / 止盈清仓 / 否则保持。

        优先级：止损 > 止盈 > RSRS卖出。
        stop_loss_pct=0 时不启用止损。

        单标的近似口径：不包含股票池筛选与最大持仓数限制
        （该逻辑依赖全市场横截面，mixed 引擎按标的逐 bar 调用）。
        """
        if position is not None and position.quantity > 0:
            # 止损优先（与 event 的 _check_sell_signals 一致）
            if self.stop_loss_pct > 0 and self.check_stop_loss(
                position, bar.close, self.stop_loss_pct,
            ):
                return 0.0
            # 目标止盈
            if self.check_take_profit(position, bar.close, self.take_profit_pct):
                return 0.0
        signal = self.generate_signal(bar, context)
        if signal.is_buy:
            return self._vec_full_shares(float(bar.close))
        if signal.is_sell:
            return 0.0
        # HOLD：保持现有持仓
        if position is not None:
            return float(position.quantity)
        return 0.0

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """vectorized 引擎信号：目标持仓股数数组（RSRS 阈值 + 止损 + 止盈状态机）。

        优先级：止损 > 止盈 > RSRS卖出。stop_loss_pct=0 时不启用止损。

        单标的近似口径：无股票池/横截面选股。
        """
        n = len(data)
        if n < self.rsrs_period + self.rsrs_norm_window + 1:
            return np.zeros(n)
        rsrs = self._get_or_create_rsrs("__vec__")
        cached = rsrs.calculate(data)
        vals = cached["rsrs"].to_numpy(dtype=float)
        close = data["收盘价"].astype(float).to_numpy()
        target = np.zeros(n)
        holding = False
        full_shares = 0.0
        cost = 0.0
        for i in range(n):
            # 本行使用的数据截至 t 时刻（第 i 根，含第 i 根）
            c = float(close[i])
            if holding:
                # 止损 / 目标止盈 / RSRS 卖出 → 清仓（优先级：止损 > 止盈 > RSRS）
                if (self.stop_loss_pct > 0 and cost > 0
                        and c / cost - 1.0 <= -self.stop_loss_pct):
                    holding = False
                elif cost > 0 and c / cost - 1.0 >= self.take_profit_pct:
                    holding = False
                elif not np.isnan(vals[i]) and vals[i] < self.sell_threshold:
                    holding = False
                if not holding:
                    full_shares = 0.0
            else:
                if not np.isnan(vals[i]) and vals[i] > self.buy_threshold:
                    holding = True
                    full_shares = self._vec_full_shares(c)
                    cost = c
            target[i] = full_shares
        return target



# ═══════════════════════════════════════════════
#  均线策略示例（展示如何继承基类，结构图要求）
# ═══════════════════════════════════════════════

@register_strategy
class MACrossStrategy(StrategyBase):
    """
    均线交叉策略示例（结构图要求的ma_strategy示例）

    简单的双均线交叉策略，用于展示如何继承 StrategyBase。
    短均线上穿长均线买入，下穿卖出。

    参数:
        fast_period: 短均线周期（默认5）
        slow_period: 长均线周期（默认20）
    """

    name = "ma_cross"

    def __init__(
        self,
        fast_period: int = 5,
        slow_period: int = 20,
        position_pct: float = 0.20,
        capital_base: float = 1_000_000.0,
        **params: Any,
    ):
        super().__init__(
            fast_period=fast_period, slow_period=slow_period,
            position_pct=position_pct, capital_base=capital_base, **params,
        )
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.position_pct = position_pct
        self.capital_base = capital_base
        self._prev_diff: Dict[str, float] = {}
        logger.info(f"均线交叉策略初始化: fast={fast_period}, slow={slow_period}")

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        """
        生成均线交叉信号

        ★★★ 防未来函数（规则4）★★★
        严格根据 bar.timestamp 截取历史数据，只使用截至当前bar的数据，
        绝不访问当前bar之后的未来数据。
        """
        symbol = bar.symbol
        df = context.get_data(symbol)
        if df is None or len(df) < self.slow_period + 1:
            return Signal(symbol=symbol, signal_type=SignalType.HOLD, reason="数据不足")

        # ★★★ 防未来函数：截取到当前bar（含当前bar）★★★
        bar_dt = pd.Timestamp(bar.timestamp, unit='s')
        df = df[df.index <= bar_dt]

        if len(df) < self.slow_period + 1:
            return Signal(symbol=symbol, signal_type=SignalType.HOLD, reason="数据不足")

        close = df['收盘价'].astype(float)
        fast_ma = close.rolling(self.fast_period).mean()
        slow_ma = close.rolling(self.slow_period).mean()

        if len(fast_ma) < 2 or np.isnan(fast_ma.iloc[-1]) or np.isnan(slow_ma.iloc[-1]):
            return Signal(symbol=symbol, signal_type=SignalType.HOLD)

        current_diff = fast_ma.iloc[-1] - slow_ma.iloc[-1]
        prev_diff = self._prev_diff.get(symbol, current_diff)

        # 金叉：短均线从下方上穿长均线
        if prev_diff <= 0 and current_diff > 0:
            self._prev_diff[symbol] = current_diff
            return Signal(
                symbol=symbol, signal_type=SignalType.BUY,
                strength=0.5, price=bar.close,
                reason=f"金叉: MA{self.fast_period}上穿MA{self.slow_period}",
                strategy_id=self.name,
            )

        # 死叉：短均线从上方下穿长均线
        if prev_diff >= 0 and current_diff < 0:
            self._prev_diff[symbol] = current_diff
            return Signal(
                symbol=symbol, signal_type=SignalType.SELL,
                strength=0.5, price=bar.close,
                reason=f"死叉: MA{self.fast_period}下穿MA{self.slow_period}",
                strategy_id=self.name,
            )

        self._prev_diff[symbol] = current_diff
        return Signal(symbol=symbol, signal_type=SignalType.HOLD)

    # ═══════════════════════════════════════════════
    #  第7项：双套信号接口（mixed / vectorized）
    # ═══════════════════════════════════════════════

    def _target_shares_from_capital(
        self, capital_base: float, price: float, pos_pct: float,
    ) -> float:
        """按资金基准 × 单票仓位比例估算目标股数（A股整手）。

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

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        """mixed 引擎目标持仓：金叉满仓 / 死叉清仓 / 否则保持。"""
        signal = self.generate_signal(bar, context)
        if signal.is_buy:
            return self._target_shares_from_capital(
                self.capital_base, float(bar.close), self.position_pct,
            )
        if signal.is_sell:
            return 0.0
        # HOLD：保持现有持仓
        if position is not None:
            return float(position.quantity)
        return 0.0

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """vectorized 引擎信号：目标持仓股数数组（金叉持有、死叉空仓）。"""
        n = len(data)
        if n < self.slow_period + 1:
            return np.zeros(n)
        close = data["收盘价"].astype(float)
        fast = close.rolling(self.fast_period).mean()
        slow = close.rolling(self.slow_period).mean()
        target = np.zeros(n)
        holding = False
        full_shares = 0.0
        prev_diff = 0.0
        for i in range(n):
            # 本行使用的数据截至 t 时刻（第 i 根，含第 i 根）
            f = fast.iloc[i]
            sl = slow.iloc[i]
            if np.isnan(f) or np.isnan(sl):
                continue
            diff = float(f - sl)
            if not holding and prev_diff <= 0 and diff > 0:
                holding = True
                full_shares = self._target_shares_from_capital(
                    self.capital_base, float(close.iloc[i]), self.position_pct,
                )
            elif holding and prev_diff >= 0 and diff < 0:
                holding = False
                full_shares = 0.0
            prev_diff = diff
            target[i] = full_shares
        return target


if __name__ == "__main__":
    # 自测 Demo：双接口 + 三引擎跑通
    import numpy as np
    import pandas as pd

    from myquant.backtest.engine import run_backtest
    from myquant.models.backtest import BacktestConfig

    n = 120
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(7)
    close = 10.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    df = pd.DataFrame(
        {"open": close * 0.995, "high": close * 1.02, "low": close * 0.98,
         "close": close, "volume": np.full(n, 1e6)},
        index=idx,
    )
    data = {"600519.SH": df}
    config = BacktestConfig(
        symbols=["600519.SH"], start_date="2024-01-01", end_date="2024-06-30",
        initial_capital=1_000_000.0,
    )
    for name, cls in [("RSRSStrategy", RSRSStrategy), ("MACrossStrategy", MACrossStrategy)]:
        st = cls()
        assert st.supports_mode("mixed") and st.supports_mode("vectorized"), name
        arr = st.generate_signals_vectorized(df)
        assert len(arr) == n and float(arr[-1]) >= 0.0, name
        for mode in ("event", "mixed", "vectorized"):
            r = run_backtest(config, st, data=data, mode=mode)
            assert r.is_success, f"{name}/{mode} 失败: {r.status}"
            print(
                f"  {name} [{mode}] 净值点数={len(r.equity_curve)} "
                f"成交={len(r.trades)} 期末净值={r.final_equity:.4f}"
            )
    print("ma_strategy 双接口自测通过 ✅")

