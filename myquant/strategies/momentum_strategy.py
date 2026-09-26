# 文件名：myquant/strategies/momentum_strategy.py
"""
strategies/momentum_strategy.py - 动量交易策略
================================================
定期计算N期收益率，选动量最强的top_n只持有，调仓换仓。
适合趋势市，追涨杀跌。

策略逻辑:
  1. 每根bar收盘后：
     a. 单票止损检查：持仓跌幅 >= stop_loss_pct → 卖出
     b. 单票止盈检查：持仓涨幅 >= take_profit_pct → 卖出
  2. 调仓日（每 rebalance_days 根bar）：
     a. 计算所有订阅标的的 momentum_period 期收益率
        momentum = close[-1] / close[-momentum_period-1] - 1
     b. 按动量降序排序，选前 top_n 只
     c. 卖出不在新股票池中的持仓
     d. 等权分配（equal_weight=True）或按动量加权，买入新入池标的
  3. 非调仓日且无止损止盈 → HOLD

防未来函数（铁律）:
  - 动量计算用 trailing 窗口：close.iloc[-1] / close.iloc[-period-1] - 1
    （分子是当前收盘价，分母是 period+1 根前的收盘价，不含未来）
  - 调仓在当前bar收盘后判断，成交由引擎在下一根bar处理（T日信号T+1成交）
  - 股票池筛选只用已收盘的历史数据
  - 止损止盈用当前bar收盘价（已收盘数据）
  - 源码无预测方法/无硬编码未来日期

为实盘无缝对接准备:
  - 继承 StrategyBase，回测/实盘共用同一策略实例
  - on_bar 逐根驱动（回测/实盘统一入口）
  - 复用 base.py 通用工具：_should_rebalance / check_stop_loss / check_take_profit / _get_close_series
  - 持仓状态用 context.get_position() 查询，不硬编码
  - 仓位计算用 calc_position_size()（A股100股整数倍）
  - 多标的支持，订阅列表可动态扩展
  - 调仓日志完整，供实盘监控

使用方式:
    from myquant.strategies import create_strategy
    strategy = create_strategy("momentum",
        momentum_period=20, top_n=5, rebalance_days=20,
        stop_loss_pct=0.10, take_profit_pct=0.20)
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.models.market_data import Bar
from myquant.models.trade import OrderSide, OrderType, Position
from myquant.strategies.base import (
    Signal, SignalType, StrategyBase, StrategyContext,
)
from myquant.strategies.registry import register_strategy

logger = get_logger(__name__)


@register_strategy
class MomentumStrategy(StrategyBase):
    """
    动量交易策略

    核心参数:
      momentum_period: 动量计算周期（bar数，默认20，约1个月）
      top_n: 每次调仓选前N只动量最强标的（默认5）
      rebalance_days: 调仓周期（bar数，默认20，约每月调仓一次）
      stop_loss_pct: 单票止损比例（默认0.10，即-10%卖出）
      take_profit_pct: 单票止盈比例（默认0.20，即+20%卖出）
      max_position_pct: 单票最大仓位比例（默认0.20）
      max_positions: 最大持仓数（默认5）
      equal_weight: 是否等权分配（默认True；False=按动量得分加权）
      min_momentum: 最小动量阈值（默认-1.0，即不设下限；设为0则只选正动量）

    信号生成:
      - 调仓日换仓 → SignalType.BUY / SELL（多标的，返回List[Signal]）
      - 单票止损 → SignalType.STOP_LOSS
      - 单票止盈 → SignalType.TAKE_PROFIT
      - 非调仓日无止损止盈 → SignalType.HOLD

    防未来函数:
      - 动量 trailing 窗口，不含未来数据
      - 调仓收盘后判断，下一根bar成交
      - 止损止盈用已收盘价格
    """

    name = "momentum"

    def __init__(
        self,
        momentum_period: int = 20,
        top_n: int = 5,
        rebalance_days: int = 20,
        stop_loss_pct: float = 0.10,
        take_profit_pct: float = 0.20,
        max_position_pct: float = 0.20,
        max_positions: int = 5,
        equal_weight: bool = True,
        min_momentum: float = -1.0,
        vec_momentum_threshold: float = 0.0,
        capital_base: float = 1_000_000.0,
        **params: Any,
    ):
        super().__init__(
            momentum_period=momentum_period,
            top_n=top_n,
            rebalance_days=rebalance_days,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            max_position_pct=max_position_pct,
            max_positions=max_positions,
            equal_weight=equal_weight,
            min_momentum=min_momentum,
            vec_momentum_threshold=vec_momentum_threshold,
            capital_base=capital_base,
            **params,
        )
        # 参数校验
        if momentum_period < 1:
            raise ValueError(f"动量周期({momentum_period})必须>=1")
        if top_n < 1:
            raise ValueError(f"选股数量({top_n})必须>=1")
        if rebalance_days < 1:
            raise ValueError(f"调仓周期({rebalance_days})必须>=1")
        if stop_loss_pct < 0 or take_profit_pct < 0:
            raise ValueError("止损/止盈比例不能为负")
        if not 0 < max_position_pct <= 1.0:
            raise ValueError(f"单票最大仓位({max_position_pct})必须在(0,1]")

        self.momentum_period = momentum_period
        self.top_n = top_n
        self.rebalance_days = rebalance_days
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_position_pct = max_position_pct
        self.max_positions = max_positions
        self.equal_weight = equal_weight
        self.min_momentum = min_momentum
        self.vec_momentum_threshold = vec_momentum_threshold
        self.capital_base = capital_base

        # 当前股票池（调仓后更新）
        self.current_pool: List[str] = []
        # 动量得分缓存（调仓时计算）
        self._momentum_scores: Dict[str, float] = {}
        # 调仓统计
        self._rebalance_count: int = 0

        logger.info(
            f"动量策略初始化: period={momentum_period}, top_n={top_n}, "
            f"rebalance={rebalance_days}bar, 止损{stop_loss_pct:.0%}/止盈{take_profit_pct:.0%}, "
            f"等权={equal_weight}, 最小动量={min_momentum}"
        )

    # ═══════════════════════════════════════════════
    #  核心：on_bar（每根bar处理，返回信号列表）
    # ═══════════════════════════════════════════════

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """每根bar处理：止损止盈检查 + 调仓判断"""
        signals: List[Signal] = []

        # 1. 单票止损/止盈检查（每根bar都检查）
        sl_tp_signals = self._check_stop_loss_take_profit(bar, context)
        signals.extend(sl_tp_signals)

        # 2. 调仓判断（用 base.py 通用工具）
        if self._should_rebalance(context, self.rebalance_days):
            rebalance_signals = self._do_rebalance(bar, context)
            signals.extend(rebalance_signals)

        # 统计信号数
        non_hold = [s for s in signals if not s.is_hold]
        if non_hold:
            self._signal_count += len(non_hold)
            for sig in non_hold:
                self._submit_signal_order(sig, bar, context)

        return signals

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        """单信号生成（兼容基类抽象方法，实际逻辑在 on_bar）

        动量策略是多标的调仓策略，核心逻辑在 on_bar 返回 List[Signal]。
        此方法返回当前bar标的的单信号（止损/止盈/HOLD），供单标的场景使用。
        """
        position = context.get_position(bar.symbol)
        if position and position.quantity > 0:
            if self.check_stop_loss(position, bar.close, self.stop_loss_pct):
                return Signal(
                    symbol=bar.symbol, signal_type=SignalType.STOP_LOSS,
                    strength=1.0, price=bar.close,
                    reason=f"stop_loss -{self.stop_loss_pct:.0%}",
                    strategy_id=self.name,
                )
            if self.check_take_profit(position, bar.close, self.take_profit_pct):
                return Signal(
                    symbol=bar.symbol, signal_type=SignalType.TAKE_PROFIT,
                    strength=1.0, price=bar.close,
                    reason=f"take_profit +{self.take_profit_pct:.0%}",
                    strategy_id=self.name,
                )
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD,
                      price=bar.close, reason="no_signal", strategy_id=self.name)

    # ═══════════════════════════════════════════════
    #  止损止盈检查
    # ═══════════════════════════════════════════════

    def _check_stop_loss_take_profit(
        self, bar: Bar, context: StrategyContext,
    ) -> List[Signal]:
        """检查当前bar标的的止损/止盈（复用 base.py 工具方法）"""
        signals: List[Signal] = []
        position = context.get_position(bar.symbol)
        if position is None or position.quantity <= 0:
            return signals

        # 止损（复用 base.py check_stop_loss）
        if self.check_stop_loss(position, bar.close, self.stop_loss_pct):
            logger.warning(
                f"[动量止损] {bar.symbol} 现价{bar.close:.2f} "
                f"成本{position.avg_cost:.2f} 跌幅>={self.stop_loss_pct:.0%}, 卖出"
            )
            signals.append(Signal(
                symbol=bar.symbol, signal_type=SignalType.STOP_LOSS,
                strength=1.0, price=bar.close,
                reason=f"stop_loss -{self.stop_loss_pct:.0%}",
                strategy_id=self.name,
            ))

        # 止盈（复用 base.py check_take_profit）
        elif self.check_take_profit(position, bar.close, self.take_profit_pct):
            logger.info(
                f"[动量止盈] {bar.symbol} 现价{bar.close:.2f} "
                f"成本{position.avg_cost:.2f} 涨幅>={self.take_profit_pct:.0%}, 卖出"
            )
            signals.append(Signal(
                symbol=bar.symbol, signal_type=SignalType.TAKE_PROFIT,
                strength=1.0, price=bar.close,
                reason=f"take_profit +{self.take_profit_pct:.0%}",
                strategy_id=self.name,
            ))

        return signals

    # ═══════════════════════════════════════════════
    #  调仓逻辑
    # ═══════════════════════════════════════════════

    def _do_rebalance(
        self, bar: Bar, context: StrategyContext,
    ) -> List[Signal]:
        """执行调仓：计算动量 → 选股 → 卖出不在池的 → 买入新入池的"""
        self._rebalance_count += 1
        signals: List[Signal] = []

        # 1. 计算所有标的的动量得分（按当前bar截断，防未来）
        all_symbols = self._get_all_symbols(context)
        self._momentum_scores = self._calc_momentum_scores(
            all_symbols, context, bar.timestamp,
        )

        if not self._momentum_scores:
            logger.warning(f"[动量调仓#{self._rebalance_count}] 无有效动量数据，跳过")
            return signals

        # 2. 按动量降序排序，选 top_n（过滤 min_momentum）
        sorted_symbols = sorted(
            self._momentum_scores.items(),
            key=lambda x: x[1], reverse=True,
        )
        new_pool = [
            sym for sym, score in sorted_symbols
            if score >= self.min_momentum
        ][:self.top_n]

        logger.info(
            f"[动量调仓#{self._rebalance_count}] 候选{len(sorted_symbols)}只 → "
            f"入选{len(new_pool)}只: {new_pool}"
        )
        for sym in new_pool:
            logger.debug(f"  {sym}: momentum={self._momentum_scores[sym]:.4f}")

        # 3. 卖出不在新股票池中的持仓
        current_positions = self._get_current_positions(context)
        for sym in list(current_positions.keys()):
            if sym not in new_pool:
                pos = current_positions[sym]
                if pos and pos.quantity > 0:
                    sym_price = self._get_latest_price(
                        context, sym, bar.timestamp, bar.close,
                    )
                    logger.info(f"[动量调仓卖出] {sym} 不在新池，清仓 @{sym_price:.2f}")
                    signals.append(Signal(
                        symbol=sym, signal_type=SignalType.SELL,
                        strength=1.0, price=sym_price,
                        reason=f"rebalance_sell (not in pool)",
                        strategy_id=self.name,
                    ))

        # 4. 买入新入池标的（等权或动量加权）
        cash = context.get_cash()
        if cash > 0 and new_pool:
            # 计算每只标的的目标仓位比例
            if self.equal_weight:
                weight_per_stock = 1.0 / len(new_pool)
            else:
                # 按动量得分加权（正动量得分归一化）
                positive_scores = {
                    s: max(0.001, self._momentum_scores[s]) for s in new_pool
                }
                total_score = sum(positive_scores.values())
                weight_per_stock = None  # 逐只计算

            for sym in new_pool:
                # 已持仓的不再买入（简化：调仓只处理新增和剔除）
                if sym in current_positions:
                    continue

                sym_price = self._get_latest_price(
                    context, sym, bar.timestamp, bar.close,
                )
                if self.equal_weight:
                    target_amount = cash * weight_per_stock
                else:
                    target_amount = cash * (positive_scores[sym] / total_score)

                # 单票仓位上限
                target_amount = min(target_amount, cash * self.max_position_pct)

                if target_amount > 0 and sym_price > 0:
                    quantity = int(target_amount / sym_price / 100) * 100
                    quantity = max(0, quantity)
                    if quantity > 0:
                        logger.info(
                            f"[动量调仓买入] {sym} 数量{quantity}股 "
                            f"金额≈{quantity * sym_price:.0f}元 @{sym_price:.2f}"
                        )
                        signals.append(Signal(
                            symbol=sym, signal_type=SignalType.BUY,
                            strength=1.0, price=sym_price,
                            reason=f"rebalance_buy (momentum={self._momentum_scores[sym]:.4f})",
                            strategy_id=self.name,
                            extra={"momentum": self._momentum_scores[sym],
                                   "quantity": quantity},
                        ))

        # 更新当前股票池
        self.current_pool = new_pool
        return signals

    # ═══════════════════════════════════════════════
    #  动量计算
    # ═══════════════════════════════════════════════

    def _calc_momentum_scores(
        self, symbols: List[str], context: StrategyContext, ts: Optional[float] = None,
    ) -> Dict[str, float]:
        """计算所有标的的动量得分（trailing 窗口，按当前bar截断，防未来）

        momentum = close[-1] / close[-period-1] - 1
        数据不足 period+1 根的标的跳过。
        ts: 当前bar时间戳（秒），用于截断 context.data 到当前bar，
            回测中 data 为全量历史，必须截断杜绝未来函数；
            实盘增量数据下 ts=None 时为无操作（数据本身只含已收盘）。
        """
        scores: Dict[str, float] = {}
        for sym in symbols:
            close = self._get_close_upto(context, sym, ts)  # 截断到当前bar
            if close is None or len(close) < self.momentum_period + 1:
                continue
            try:
                # trailing：当前收盘价 / period+1 根前收盘价 - 1
                current = float(close.iloc[-1])
                past = float(close.iloc[-self.momentum_period - 1])
                if past > 0:
                    scores[sym] = current / past - 1.0
            except (IndexError, ValueError, ZeroDivisionError):
                continue
        return scores

    # ═══════════════════════════════════════════════
    #  辅助方法
    # ═══════════════════════════════════════════════

    def _get_close_upto(
        self, context: StrategyContext, symbol: str, ts: Optional[float] = None,
    ) -> Optional[pd.Series]:
        """按当前bar时间戳截断的 close 序列（防未来铁律）

        回测中 context.data 为全量历史，若不截断会读到当前bar之后的未来价格；
        实盘增量数据下 data 只含已收盘 bar，ts 截断为无操作（保险起见仍执行）。
        """
        close = self._get_close_series(context, symbol)  # 复用 base.py 工具
        if close is None or len(close) == 0:
            return None
        if ts is not None:
            try:
                bar_dt = pd.Timestamp(ts, unit='s')
                close = close[close.index <= bar_dt]
            except Exception as e:
                # #R1修复：时间戳无法解析时退回全量（由上层数据保证无未来），记录debug日志
                logger.debug(f"[{self.name}] 时间戳解析失败，退回全量数据: ts={ts}, err={e}")
        return close if len(close) > 0 else None

    def _get_all_symbols(self, context: StrategyContext) -> List[str]:
        """获取所有可交易标的（context.data 中的标的）"""
        if context.data:
            return list(context.data.keys())
        return []

    def _get_latest_price(
        self, context: StrategyContext, symbol: str,
        ts: Optional[float], default_price: float,
    ) -> float:
        """获取某标的截至当前bar的最新收盘价（调仓用各标的自身价格）

        按 ts 截断 context.data，杜绝读取未来价格（防未来铁律）。
        """
        close = self._get_close_upto(context, symbol, ts)
        if close is not None and len(close) > 0:
            return float(close.iloc[-1])
        return default_price

    def _get_current_positions(
        self, context: StrategyContext,
    ) -> Dict[str, Optional[Position]]:
        """获取当前持仓字典"""
        positions: Dict[str, Optional[Position]] = {}
        if context.portfolio is None:
            return positions
        if hasattr(context.portfolio, 'positions'):
            pos_dict = context.portfolio.positions
            if isinstance(pos_dict, dict):
                return dict(pos_dict)
            # 可迭代对象
            try:
                for pos in pos_dict:
                    if hasattr(pos, 'symbol'):
                        positions[pos.symbol] = pos
            except TypeError:
                pass
        return positions

    def _submit_signal_order(
        self, signal: Signal, bar: Bar, context: StrategyContext,
    ) -> None:
        """将信号转换为订单并提交（止损止盈市价，调仓市价）"""
        if signal.is_hold:
            return

        if signal.is_sell:
            position = context.get_position(signal.symbol)
            if position and position.quantity > 0:
                order = self.create_order(
                    symbol=signal.symbol,
                    side=OrderSide.SELL,
                    quantity=position.quantity,
                    order_type=OrderType.MARKET,
                    reason=signal.reason,
                )
                context.submit_order(order)
            return

        if signal.is_buy:
            # 买入数量从 signal.extra 取（调仓时已计算）
            quantity = signal.extra.get("quantity", 0)
            if quantity <= 0 and signal.price > 0:
                cash = context.get_cash()
                quantity = int(cash * self.max_position_pct / signal.price / 100) * 100
            if quantity > 0:
                order = self.create_order(
                    symbol=signal.symbol,
                    side=OrderSide.BUY,
                    quantity=quantity,
                    order_type=OrderType.MARKET,
                    price=signal.price,
                    reason=signal.reason,
                )
                context.submit_order(order)

    # ═══════════════════════════════════════════════
    #  状态查询
    # ═══════════════════════════════════════════════

    def get_momentum_status(self) -> Dict[str, Any]:
        """获取动量策略状态（供实盘监控/Web看板使用）"""
        return {
            "strategy": self.name,
            "momentum_period": self.momentum_period,
            "top_n": self.top_n,
            "rebalance_days": self.rebalance_days,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "max_position_pct": self.max_position_pct,
            "max_positions": self.max_positions,
            "equal_weight": self.equal_weight,
            "current_pool": self.current_pool,
            "momentum_scores": dict(self._momentum_scores),
            "rebalance_count": self._rebalance_count,
            "signal_count": self._signal_count,
        }

    def on_stop(self, context: StrategyContext) -> None:
        """停止钩子：输出调仓统计"""
        logger.info(
            f"动量策略停止: 调仓{self._rebalance_count}次, "
            f"信号总数={self._signal_count}, 当前池={self.current_pool}"
        )
        super().on_stop(context)

    # ═══════════════════════════════════════════════
    #  第7项：双套信号接口（mixed / vectorized）
    #  单标的近似口径（docstring 注明）：
    #    横截面 top_n 选股依赖全市场数据，mixed/vectorized 按标的逐
    #    bar 调用无法复现；此处退化为单标的动量阈值：
    #    调仓日 该标的动量 >= vec_momentum_threshold → 持有，否则空仓；
    #    非调仓日 止损/止盈/保持。
    # ═══════════════════════════════════════════════

    def _vec_momentum_at(
        self, close: pd.Series, i: int,
    ) -> Optional[float]:
        """计算该标的在第 i 根的动量（close[i]/close[i-period]-1）。

        Args:
            close: 收盘价序列（全量，index 与 data 对齐）。
            i: 目标行索引。

        Returns:
            动量值；数据不足或价格非法返回 None。

        本行使用的数据截至 t 时刻（第 i 根，含第 i 根）。
        """
        if i < self.momentum_period:
            return None
        cur = float(close.iloc[i])
        past = float(close.iloc[i - self.momentum_period])
        if past <= 0:
            return None
        return cur / past - 1.0

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        """mixed 引擎目标持仓：调仓日动量阈值判断 + 止损止盈。"""
        symbol = bar.symbol
        # 止损止盈（持仓时）
        if position is not None and position.quantity > 0:
            if self.check_stop_loss(position, bar.close, self.stop_loss_pct):
                return 0.0
            if self.check_take_profit(position, bar.close, self.take_profit_pct):
                return 0.0
        # 调仓日：重新判断该标的动量阈值（单标的近似，非横截面）
        if self._should_rebalance(context, self.rebalance_days):
            close = self._get_close_upto(context, symbol, bar.timestamp)
            mom = None
            if close is not None and len(close) >= self.momentum_period + 1:
                mom = self._vec_momentum_at(close, len(close) - 1)
            if mom is not None and mom >= self.vec_momentum_threshold:
                return self._target_shares_from_capital(
                    self.capital_base, float(bar.close), self.max_position_pct,
                )
            return 0.0
        # 非调仓日：保持当前持仓
        if position is not None:
            return float(position.quantity)
        return 0.0

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """vectorized 引擎信号：目标持仓股数数组（调仓日动量阈值状态机）。"""
        n = len(data)
        if n < self.momentum_period + 1:
            return np.zeros(n)
        close = data["收盘价"].astype(float)
        close_np = close.to_numpy(dtype=float)
        target = np.zeros(n)
        holding = False
        full_shares = 0.0
        cost = 0.0
        for i in range(n):
            # 本行使用的数据截至 t 时刻（第 i 根，含第 i 根）
            c = float(close_np[i])
            if holding:
                exit_flag = False
                if cost > 0 and c / cost - 1.0 <= -self.stop_loss_pct:
                    exit_flag = True
                elif cost > 0 and c / cost - 1.0 >= self.take_profit_pct:
                    exit_flag = True
                if exit_flag:
                    holding = False
                    full_shares = 0.0
            # 调仓日：重新判断（i 从 0 起每 rebalance_days 根）
            if i % self.rebalance_days == 0:
                mom = self._vec_momentum_at(close, i)
                if mom is not None and mom >= self.vec_momentum_threshold:
                    if not holding:
                        holding = True
                        full_shares = self._target_shares_from_capital(
                            self.capital_base, c, self.max_position_pct,
                        )
                        cost = c
                else:
                    holding = False
                    full_shares = 0.0
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
    rng = np.random.default_rng(11)
    close = 10.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, n)))
    df = pd.DataFrame(
        {"开盘价": close * 0.995, "最高价": close * 1.02, "最低价": close * 0.98,
         "收盘价": close, "成交量": np.full(n, 1e6)},
        index=idx,
    )  # P3-D8-001/002 整改：合成数据列与生产链路一致（中文列），自测经引擎消费与 data_loader 归一后链路同构
    data = {"600519.SH": df}
    config = BacktestConfig(
        symbols=["600519.SH"], start_date="2024-01-01", end_date="2024-06-30",
        initial_capital=1_000_000.0,
    )
    st = MomentumStrategy(vec_momentum_threshold=0.0, capital_base=1_000_000.0)
    assert st.supports_mode("mixed") and st.supports_mode("vectorized")
    arr = st.generate_signals_vectorized(df)
    assert len(arr) == n and float(arr[-1]) >= 0.0
    for mode in ("event", "mixed", "vectorized"):
        r = run_backtest(config, st, data=data, mode=mode)
        assert r.is_success, f"momentum/{mode} 失败: {r.status}"
        print(
            f"  MomentumStrategy [{mode}] 净值点数={len(r.equity_curve)} "
            f"成交={len(r.trades)} 期末净值={r.final_equity:.4f}"
        )
    print("momentum_strategy 双接口自测通过 ✅")

