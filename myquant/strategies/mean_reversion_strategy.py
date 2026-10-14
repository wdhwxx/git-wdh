# 文件名：myquant/strategies/mean_reversion_strategy.py
"""
strategies/mean_reversion_strategy.py - 均值回归交易策略
==========================================================
价格偏离均值后回归的策略：布林下轨+RSI超卖买入，
布林上轨+RSI超买卖出，或回归中轨平仓。
适合震荡市。

策略逻辑:
  1. 每根bar收盘后计算指标（复用 indicators 模块）:
     - 布林带：中轨=SMA(20), 上轨=中轨+2*STD, 下轨=中轨-2*STD
     - RSI(14)：Wilder平滑，0-100
  2. 买入条件（无持仓时）:
     - 价格 <= 布林下轨
     - AND (use_rsi_filter=False OR RSI <= rsi_oversold)
  3. 卖出条件（持仓时）:
     - 价格 >= 布林上轨 AND (use_rsi_filter=False OR RSI >= rsi_overbought)
     - OR exit_on_mid=True AND 价格 >= 中轨（回归中轨平仓）
     - OR 止损：跌幅 >= stop_loss_pct
     - OR 止盈：涨幅 >= take_profit_pct
  4. 仓位管理：单票最大仓位 max_position_pct，A股100股整数倍

防未来函数（铁律）:
  - 布林带/RSI 复用 indicators 模块（已内置防未来：rolling/EWM 只含当前及历史数据）
  - 指标计算用 context.data 中已收盘的历史bar，取最后一行
  - 信号在当前bar收盘后生成，成交由引擎在下一根bar处理（T日信号T+1成交）
  - 止损止盈用当前bar收盘价（已收盘数据）
  - 源码无预测方法/无硬编码未来日期

为实盘无缝对接准备:
  - 继承 StrategyBase，回测/实盘共用同一策略实例
  - on_bar 逐根驱动（回测/实盘统一入口）
  - 指标实例按 symbol 缓存（避免重复全量计算，实盘性能优化）
  - 复用 base.py 通用工具：check_stop_loss / check_take_profit / _get_close_series
  - 持仓状态用 context.get_position() 查询，不硬编码
  - 仓位计算用 calc_position_size()（A股100股整数倍）
  - get_mean_reversion_status() 供实盘监控/Web看板使用

使用方式:
    from myquant.strategies import create_strategy
    strategy = create_strategy("mean_reversion",
        bb_period=20, bb_std=2.0, rsi_period=14,
        rsi_oversold=30, rsi_overbought=70,
        exit_on_mid=True, stop_loss_pct=0.08)
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.indicators import RSI, BollingerBands
from myquant.models.market_data import Bar
from myquant.models.trade import Position
from myquant.strategies.base import (
    Signal,
    SignalType,
    StrategyBase,
    StrategyContext,
)
from myquant.strategies.registry import register_strategy

logger = get_logger(__name__)


@register_strategy
class MeanReversionStrategy(StrategyBase):
    """
    均值回归交易策略

    核心参数:
      bb_period: 布林带周期（默认20）
      bb_std: 布林带标准差倍数（默认2.0）
      rsi_period: RSI周期（默认14）
      rsi_oversold: RSI超卖阈值（默认30，<=此值视为超卖）
      rsi_overbought: RSI超买阈值（默认70，>=此值视为超买）
      stop_loss_pct: 止损比例（默认0.08，即-8%卖出）
      take_profit_pct: 止盈比例（默认0.10，即+10%卖出）
      max_position_pct: 单票最大仓位比例（默认0.20）
      exit_on_mid: 是否回归中轨平仓（默认True，持仓时价格>=中轨即卖出）
      use_rsi_filter: 是否用RSI过滤买卖信号（默认True）

    信号生成:
      - 布林下轨+RSI超卖 → SignalType.BUY
      - 布林上轨+RSI超买 / 回归中轨 / 止损 / 止盈 → SignalType.SELL
      - 无信号 → SignalType.HOLD

    防未来函数:
      - 布林带/RSI trailing 窗口，不含未来
      - 信号收盘后生成，下一根bar成交
    """

    name = "mean_reversion"

    # 防泄漏门禁契约元数据（M0-04，主键=name，DEC-01；对齐 config/strategy_registry.yaml）
    strategy_id: str = "mean_reversion_strategy"
    decision_time: str = "T日收盘后 15:00"
    execution_time: str = "T+1开盘 09:30"
    holding_period: str = "5交易日"
    universe: Dict[str, Any] = {
        "type": "static",
        "source": "config/stock_pool.yaml",
        "pit_status": "未建设",
    }
    data_availability: Dict[str, Any] = {
        "market_data": {
            "available_time": "T日15:00",
            "adjust": "hfq",
            "adjust_asof": "end_date 必传",
            "fields": ["开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"],
        },
        "financial_data": {
            "available_time": "ann_date",
            "basis": "eff_disclosure_date",
            "fields": [],
        },
    }
    consumed_columns: List[str] = ["收盘价", "成交量"]
    ruleset: str = "daily_close_decision"

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        stop_loss_pct: float = 0.08,
        take_profit_pct: float = 0.10,
        max_position_pct: float = 0.20,
        exit_on_mid: bool = True,
        use_rsi_filter: bool = True,
        capital_base: float = 1_000_000.0,
        **params: Any,
    ):
        super().__init__(
            bb_period=bb_period,
            bb_std=bb_std,
            rsi_period=rsi_period,
            rsi_oversold=rsi_oversold,
            rsi_overbought=rsi_overbought,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            max_position_pct=max_position_pct,
            exit_on_mid=exit_on_mid,
            use_rsi_filter=use_rsi_filter,
            capital_base=capital_base,
            **params,
        )
        # 参数校验
        if bb_period < 2:
            raise ValueError(f"布林带周期({bb_period})必须>=2")
        if bb_std <= 0:
            raise ValueError(f"布林带标准差倍数({bb_std})必须>0")
        if rsi_period < 2:
            raise ValueError(f"RSI周期({rsi_period})必须>=2")
        if not 0 <= rsi_oversold < rsi_overbought <= 100:
            raise ValueError(
                f"RSI阈值必须满足 0<=oversold({rsi_oversold})<overbought({rsi_overbought})<=100"
            )
        if stop_loss_pct < 0 or take_profit_pct < 0:
            raise ValueError("止损/止盈比例不能为负")
        if not 0 < max_position_pct <= 1.0:
            raise ValueError(f"单票最大仓位({max_position_pct})必须在(0,1]")

        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_position_pct = max_position_pct
        self.exit_on_mid = exit_on_mid
        self.use_rsi_filter = use_rsi_filter
        self.capital_base = capital_base
        self.min_bandwidth_pct = 0.005  # 最小带宽比例（0.5%），低于此值不交易（防零波动频繁信号）

        # 指标实例按 symbol 缓存（实盘性能优化，避免重复创建）
        self._bb_instances: Dict[str, BollingerBands] = {}
        self._rsi_instances: Dict[str, RSI] = {}

        # 统计
        self._buy_signals: int = 0
        self._sell_signals: int = 0

        logger.info(
            f"均值回归策略初始化: BB({bb_period},{bb_std}σ) "
            f"RSI({rsi_period}, 超卖<={rsi_oversold}/超买>={rsi_overbought}) "
            f"回归中轨平仓={exit_on_mid}, RSI过滤={use_rsi_filter}, "
            f"止损{stop_loss_pct:.0%}/止盈{take_profit_pct:.0%}"
        )

    # ═══════════════════════════════════════════════
    #  指标获取（按 symbol 缓存）
    # ═══════════════════════════════════════════════

    def _get_bb(self, symbol: str) -> BollingerBands:
        """获取或创建布林带指标实例（按 symbol 缓存）"""
        if symbol not in self._bb_instances:
            self._bb_instances[symbol] = BollingerBands(
                period=self.bb_period, std_dev=self.bb_std,
            )
        return self._bb_instances[symbol]

    def _get_rsi(self, symbol: str) -> RSI:
        """获取或创建RSI指标实例（按 symbol 缓存）"""
        if symbol not in self._rsi_instances:
            self._rsi_instances[symbol] = RSI(period=self.rsi_period)
        return self._rsi_instances[symbol]

    def _calc_indicators(
        self, symbol: str, context: StrategyContext, ts: Optional[float] = None,
    ) -> Optional[Dict[str, float]]:
        """计算当前bar的指标值（布林带+RSI），返回最新一行的字典

        数据不足时返回 None。
        复用 indicators 模块（已内置防未来函数）。
        ts: 当前bar时间戳（秒），用于截断 context.data 到当前bar，
            回测中 data 为全量历史，必须截断杜绝未来函数；
            实盘增量数据下 data 只含已收盘 bar，ts=None 时为无操作。
        """
        df = context.get_data(symbol)
        if df is None or df.empty:
            return None
        if ts is not None:
            try:
                bar_dt = pd.Timestamp(ts, unit='s')
                df = df[df.index <= bar_dt]
            except Exception as e:
                # #R1修复：时间戳无法解析时退回全量（由上层数据保证无未来），记录debug日志
                logger.debug(f"[{self.name}] 时间戳解析失败，退回全量数据: ts={ts}, err={e}")
        if len(df) < max(self.bb_period, self.rsi_period) + 1:
            return None

        try:
            bb = self._get_bb(symbol)
            bb_result = bb.calculate(df)
            rsi = self._get_rsi(symbol)
            rsi_result = rsi.calculate(df)

            # 取最后一行（最新值）
            latest_bb = bb_result.iloc[-1]
            latest_rsi = rsi_result.iloc[-1]

            if pd.isna(latest_bb['mid']) or pd.isna(latest_rsi['rsi']):
                return None

            return {
                'mid': float(latest_bb['mid']),
                'upper': float(latest_bb['upper']),
                'lower': float(latest_bb['lower']),
                'percent_b': float(latest_bb['percent_b']) if 'percent_b' in latest_bb else 0.0,
                'rsi': float(latest_rsi['rsi']),
            }
        except Exception as e:
            logger.warning(f"[均值回归] 指标计算失败 {symbol}: {e}")
            return None

    # ═══════════════════════════════════════════════
    #  核心信号生成
    # ═══════════════════════════════════════════════

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        """生成均值回归信号（核心方法）"""
        symbol = bar.symbol
        price = float(bar.close)

        # 计算指标（按当前bar时间戳截断，防未来）
        indicators = self._calc_indicators(symbol, context, bar.timestamp)
        if indicators is None:
            return Signal(symbol=symbol, signal_type=SignalType.HOLD,
                          price=price, reason="insufficient_data", strategy_id=self.name)

        mid = indicators['mid']
        upper = indicators['upper']
        lower = indicators['lower']
        rsi = indicators['rsi']

        # 带宽过窄检查（价格完全平稳时标准差=0，防频繁买卖）
        bandwidth = upper - lower
        if mid > 0 and bandwidth / mid < self.min_bandwidth_pct:
            return Signal(
                symbol=symbol, signal_type=SignalType.HOLD,
                price=price,
                reason=f"bandwidth_too_narrow ({bandwidth/mid:.4f}<{self.min_bandwidth_pct})",
                strategy_id=self.name,
                extra={"rsi": rsi, "bb_mid": mid, "bb_upper": upper, "bb_lower": lower},
            )

        position = context.get_position(symbol)
        has_position = position is not None and position.quantity > 0

        # 1. 持仓时：检查卖出条件
        if has_position:
            # 1a. 止损（复用 base.py check_stop_loss）
            if self.check_stop_loss(position, price, self.stop_loss_pct):
                self._sell_signals += 1
                logger.warning(
                    f"[均值回归止损] {symbol} 现价{price:.2f} "
                    f"成本{position.avg_cost:.2f} 跌幅>={self.stop_loss_pct:.0%}"
                )
                return Signal(
                    symbol=symbol, signal_type=SignalType.STOP_LOSS,
                    strength=1.0, price=price,
                    reason=f"stop_loss -{self.stop_loss_pct:.0%}",
                    strategy_id=self.name,
                    extra={"rsi": rsi, "bb_mid": mid, "bb_upper": upper, "bb_lower": lower},
                )

            # 1b. 止盈（复用 base.py check_take_profit）
            if self.check_take_profit(position, price, self.take_profit_pct):
                self._sell_signals += 1
                logger.info(
                    f"[均值回归止盈] {symbol} 现价{price:.2f} "
                    f"成本{position.avg_cost:.2f} 涨幅>={self.take_profit_pct:.0%}"
                )
                return Signal(
                    symbol=symbol, signal_type=SignalType.TAKE_PROFIT,
                    strength=1.0, price=price,
                    reason=f"take_profit +{self.take_profit_pct:.0%}",
                    strategy_id=self.name,
                    extra={"rsi": rsi, "bb_mid": mid, "bb_upper": upper, "bb_lower": lower},
                )

            # 1c. 布林上轨+RSI超买
            if price >= upper:
                if not self.use_rsi_filter or rsi >= self.rsi_overbought:
                    self._sell_signals += 1
                    logger.info(
                        f"[均值回归卖出] {symbol} 价格{price:.2f}>={upper:.2f}(上轨) "
                        f"RSI={rsi:.1f}>={self.rsi_overbought}"
                    )
                    return Signal(
                        symbol=symbol, signal_type=SignalType.SELL,
                        strength=1.0, price=price,
                        reason=f"bb_upper+rsi_overbought({rsi:.1f})",
                        strategy_id=self.name,
                        extra={"rsi": rsi, "bb_mid": mid, "bb_upper": upper, "bb_lower": lower},
                    )

            # 1d. 回归中轨平仓
            if self.exit_on_mid and price >= mid:
                self._sell_signals += 1
                logger.info(
                    f"[均值回归平仓] {symbol} 价格{price:.2f}>={mid:.2f}(中轨), 回归平仓"
                )
                return Signal(
                    symbol=symbol, signal_type=SignalType.SELL,
                    strength=1.0, price=price,
                    reason="revert_to_mid",
                    strategy_id=self.name,
                    extra={"rsi": rsi, "bb_mid": mid, "bb_upper": upper, "bb_lower": lower},
                )

        # 2. 无持仓时：检查买入条件
        else:
            if price <= lower:
                if not self.use_rsi_filter or rsi <= self.rsi_oversold:
                    self._buy_signals += 1
                    logger.info(
                        f"[均值回归买入] {symbol} 价格{price:.2f}<={lower:.2f}(下轨) "
                        f"RSI={rsi:.1f}<={self.rsi_oversold}"
                    )
                    return Signal(
                        symbol=symbol, signal_type=SignalType.BUY,
                        strength=1.0, price=price,
                        reason=f"bb_lower+rsi_oversold({rsi:.1f})",
                        strategy_id=self.name,
                        extra={"rsi": rsi, "bb_mid": mid, "bb_upper": upper, "bb_lower": lower},
                    )

        # 3. 无信号
        return Signal(
            symbol=symbol, signal_type=SignalType.HOLD,
            price=price,
            reason=f"hold (rsi={rsi:.1f}, %b={(price-lower)/(upper-lower) if upper>lower else 0:.2f})",
            strategy_id=self.name,
            extra={"rsi": rsi, "bb_mid": mid, "bb_upper": upper, "bb_lower": lower},
        )

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """每根bar处理（生成信号 + 转换为订单）"""
        signal = self.generate_signal(bar, context)
        if signal.is_hold:
            return []

        self._signal_count += 1

        # P0-06修复：策略只产信号，订单由引擎统一执行
        # （引擎路径经 RiskManager / LookaheadGuard / signal_id 幂等去重）；
        # 不再在策略内 create_order + context.submit_order，避免双重报单并绕过风控。
        return [signal]

    # ═══════════════════════════════════════════════
    #  状态查询
    # ═══════════════════════════════════════════════

    def get_mean_reversion_status(
        self, symbol: Optional[str] = None,
        context: Optional[StrategyContext] = None,
    ) -> Dict[str, Any]:
        """获取均值回归策略状态（供实盘监控/Web看板使用）"""
        status = {
            "strategy": self.name,
            "bb_period": self.bb_period,
            "bb_std": self.bb_std,
            "rsi_period": self.rsi_period,
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "max_position_pct": self.max_position_pct,
            "exit_on_mid": self.exit_on_mid,
            "use_rsi_filter": self.use_rsi_filter,
            "buy_signals": self._buy_signals,
            "sell_signals": self._sell_signals,
            "signal_count": self._signal_count,
        }
        if symbol and context:
            indicators = self._calc_indicators(symbol, context)
            if indicators:
                status["indicators"] = indicators
        return status

    def on_stop(self, context: StrategyContext) -> None:
        """停止钩子：输出信号统计"""
        logger.info(
            f"均值回归策略停止: 买入{self._buy_signals}次, 卖出{self._sell_signals}次, "
            f"信号总数={self._signal_count}"
        )
        super().on_stop(context)

    # ═══════════════════════════════════════════════
    #  第7项：双套信号接口（mixed / vectorized）
    # ═══════════════════════════════════════════════

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        """mixed 引擎目标持仓：买入满仓 / 卖出清仓 / 否则保持。

        复用 generate_signal（布林带+RSI+止损止盈，按 bar 时间戳截断防未来）。
        """
        signal = self.generate_signal(bar, context)
        if signal.is_buy:
            return self._target_shares_from_capital(
                self.capital_base, float(bar.close), self.max_position_pct,
            )
        if signal.is_sell:
            return 0.0
        # HOLD：保持现有持仓
        if position is not None:
            return float(position.quantity)
        return 0.0

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """vectorized 引擎信号：目标持仓股数数组（布林带+RSI 状态机）。

        口径与 event 一致：无持仓时下轨+超卖买入持有；持仓时
        止损/止盈/上轨+超买/回归中轨平仓。
        """
        n = len(data)
        need = max(self.bb_period, self.rsi_period) + 1
        if n < need:
            return np.zeros(n)
        close = data["收盘价"].astype(float)
        mid = close.rolling(self.bb_period).mean()
        std = close.rolling(self.bb_period).std()
        upper = mid + self.bb_std * std
        lower = mid - self.bb_std * std
        rsi = self._get_rsi("__vec__")
        rsi_df = rsi.calculate(data)
        rsi_vals = (
            rsi_df["rsi"].to_numpy(dtype=float)
            if "rsi" in rsi_df.columns else np.full(n, np.nan)
        )
        close_np = close.to_numpy(dtype=float)
        mid_np = mid.to_numpy(dtype=float)
        upper_np = upper.to_numpy(dtype=float)
        lower_np = lower.to_numpy(dtype=float)
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
                elif (not np.isnan(upper_np[i]) and c >= upper_np[i]
                      and (not self.use_rsi_filter
                           or (not np.isnan(rsi_vals[i])
                               and rsi_vals[i] >= self.rsi_overbought))):
                    exit_flag = True
                elif self.exit_on_mid and not np.isnan(mid_np[i]) and c >= mid_np[i]:
                    exit_flag = True
                if exit_flag:
                    holding = False
                    full_shares = 0.0
            else:
                if (not np.isnan(lower_np[i]) and c <= lower_np[i]
                        and (not self.use_rsi_filter
                             or (not np.isnan(rsi_vals[i])
                                 and rsi_vals[i] <= self.rsi_oversold))):
                    holding = True
                    full_shares = self._target_shares_from_capital(
                        self.capital_base, c, self.max_position_pct,
                    )
                    cost = c
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
    rng = np.random.default_rng(5)
    close = 20.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, n)))
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
    st = MeanReversionStrategy(capital_base=1_000_000.0)
    assert st.supports_mode("mixed") and st.supports_mode("vectorized")
    arr = st.generate_signals_vectorized(df)
    assert len(arr) == n and float(arr[-1]) >= 0.0
    for mode in ("event", "mixed", "vectorized"):
        r = run_backtest(config, st, data=data, mode=mode)
        assert r.is_success, f"mean_reversion/{mode} 失败: {r.status}"
        print(
            f"  MeanReversionStrategy [{mode}] 净值点数={len(r.equity_curve)} "
            f"成交={len(r.trades)} 期末净值={r.final_equity:.4f}"
        )
    print("mean_reversion_strategy 双接口自测通过 ✅")

