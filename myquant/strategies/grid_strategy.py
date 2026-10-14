# 文件名：myquant/strategies/grid_strategy.py
"""
strategies/grid_strategy.py - 网格交易策略
============================================
价格区间内等分网格，跌破一格买入一份，涨破一格卖出一份。
适合震荡市，区间外止损/止盈。

策略逻辑:
  1. 网格初始化：grid_lower ~ grid_upper 等分 grid_num 格，每格对应一份仓位
  2. 每根bar收盘后：
     a. 止损检查：价格 < grid_lower * (1 - stop_loss_pct) → 清仓
     b. 止盈检查：价格 > grid_upper * (1 + take_profit_pct) → 清仓
     c. 计算当前价格所在网格位置（0 ~ grid_num）
     d. 与上一次位置比较：
        - 位置下降（跌破一格）→ 买入一份（LIMIT单挂在网格线下沿）
        - 位置上升（涨破一格）→ 卖出一份（LIMIT单挂在网格线上沿）
  3. 每份仓位 = position_per_grid * 可用资金，A股100股整数倍
  4. 多标的支持：每个标的独立网格状态

防未来函数（铁律）:
  - 网格线在 on_init 时基于历史数据固定，运行中不随价格漂移
  - 当前bar收盘价确定网格位置，信号在收盘后生成
  - 回测成交由引擎在下一根bar处理（T日信号T+1成交）
  - 实盘生成 LIMIT 订单挂在网格线上，不预测成交价
  - 源码无预测方法/无硬编码未来日期

为实盘无缝对接准备:
  - 继承 StrategyBase，回测/实盘共用同一策略实例
  - on_bar 逐根驱动（回测/实盘统一入口）
  - 生成 OrderType.LIMIT 订单（实盘券商挂单，回测引擎模拟）
  - 持仓状态用 context.get_position() 查询，不硬编码
  - 仓位计算用 calc_position_size()（A股100股整数倍）
  - 多标的独立网格，支持组合网格策略
  - auto_grid 模式基于历史波动率自动计算区间（实盘可定期重算）

使用方式:
    from myquant.strategies import create_strategy
    strategy = create_strategy("grid",
        grid_lower=10.0, grid_upper=20.0, grid_num=5,
        position_per_grid=0.1, stop_loss_pct=0.05)
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
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
class GridStrategy(StrategyBase):
    """
    网格交易策略

    核心参数:
      grid_lower: 网格下界价格（默认10.0）
      grid_upper: 网格上界价格（默认20.0）
      grid_num: 网格数量（默认5，即6条网格线）
      position_per_grid: 每格仓位比例（默认0.1，即10%可用资金）
      stop_loss_pct: 区间下破止损比例（默认0.05，即跌破下界5%清仓）
      take_profit_pct: 区间上破止盈比例（默认0.10，即涨破上界10%清仓）
      max_positions: 最大持仓标的数（默认1，单标的网格）
      auto_grid: 是否自动计算网格区间（默认False，用固定上下界）
      auto_grid_lookback: 自动网格回看bar数（默认20，基于历史波动率）
      auto_grid_atr_mult: 自动网格ATR倍数（默认2.0，区间=中轴±ATR*倍数）

    信号生成:
      - 跌破一格 → SignalType.BUY（strength=1.0，LIMIT单挂网格线下沿）
      - 涨破一格 → SignalType.SELL（strength=1.0，LIMIT单挂网格线上沿）
      - 止损/止盈 → SignalType.STOP_LOSS / TAKE_PROFIT（市价清仓）
      - 网格位置不变 → SignalType.HOLD

    防未来函数:
      - 网格线固定，不随当前价格漂移
      - 位置判断只用当前及历史收盘价
      - 信号在收盘后生成，下一根bar成交
    """

    name = "grid"

    # 防泄漏门禁契约元数据（M0-04，主键=name，DEC-01；对齐 config/strategy_registry.yaml）
    strategy_id: str = "grid_strategy"
    decision_time: str = "T日收盘后 15:00"
    execution_time: str = "T+1开盘 09:30"
    holding_period: str = "波动区间滚动"
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
    consumed_columns: List[str] = ["收盘价", "最高价", "最低价", "成交量"]  # ATR 用 high/low
    ruleset: str = "daily_close_decision"

    def __init__(
        self,
        grid_lower: float = 10.0,
        grid_upper: float = 20.0,
        grid_num: int = 5,
        position_per_grid: float = 0.1,
        stop_loss_pct: float = 0.05,
        take_profit_pct: float = 0.10,
        max_positions: int = 1,
        auto_grid: bool = False,
        auto_grid_lookback: int = 20,
        auto_grid_atr_mult: float = 2.0,
        capital_base: float = 1_000_000.0,
        **params: Any,
    ):
        super().__init__(
            grid_lower=grid_lower,
            grid_upper=grid_upper,
            grid_num=grid_num,
            position_per_grid=position_per_grid,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            max_positions=max_positions,
            auto_grid=auto_grid,
            auto_grid_lookback=auto_grid_lookback,
            auto_grid_atr_mult=auto_grid_atr_mult,
            capital_base=capital_base,
            **params,
        )
        # 参数校验
        if grid_upper <= grid_lower:
            raise ValueError(f"网格上界({grid_upper})必须大于下界({grid_lower})")
        if grid_num < 1:
            raise ValueError(f"网格数量({grid_num})必须>=1")
        if not 0 < position_per_grid <= 1.0:
            raise ValueError(f"每格仓位比例({position_per_grid})必须在(0,1]")
        if stop_loss_pct < 0 or take_profit_pct < 0:
            raise ValueError("止损/止盈比例不能为负")

        self.grid_lower = grid_lower
        self.grid_upper = grid_upper
        self.grid_num = grid_num
        self.position_per_grid = position_per_grid
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_positions = max_positions
        self.auto_grid = auto_grid
        self.auto_grid_lookback = auto_grid_lookback
        self.auto_grid_atr_mult = auto_grid_atr_mult
        self.capital_base = capital_base

        # 网格线（on_init 时计算，运行中固定）
        self._grid_lines: Optional[np.ndarray] = None
        self._grid_step: float = 0.0

        # 每标的状态：最后网格位置 / 是否已止损止盈 / 网格持仓份数
        self._last_level: Dict[str, int] = {}
        self._closed: Dict[str, bool] = {}
        self._position_units: Dict[str, int] = {}  # 每标的累计网格买入份数

        # 网格统计
        self._grid_buys: int = 0
        self._grid_sells: int = 0

        logger.info(
            f"网格策略初始化: [{grid_lower:.2f}, {grid_upper:.2f}], "
            f"{grid_num}格, 每格{position_per_grid:.0%}, "
            f"止损{stop_loss_pct:.0%}/止盈{take_profit_pct:.0%}, "
            f"auto_grid={auto_grid}"
        )

    # ═══════════════════════════════════════════════
    #  生命周期
    # ═══════════════════════════════════════════════

    def on_init(self, context: StrategyContext) -> None:
        """初始化：计算网格线（auto_grid 模式基于历史数据）"""
        super().on_init(context)

        if self.auto_grid:
            self._calc_auto_grid(context)
        else:
            self._grid_lines = np.linspace(
                self.grid_lower, self.grid_upper, self.grid_num + 1
            )
            self._grid_step = (self.grid_upper - self.grid_lower) / self.grid_num

        logger.info(
            f"网格线已固定: {self._grid_lines.tolist()}, "
            f"步长={self._grid_step:.4f}"
        )

    def _calc_auto_grid(self, context: StrategyContext) -> None:
        """基于历史波动率自动计算网格区间（只用历史数据，防未来）

        中轴 = 最近N根收盘价均值
        ATR 近似 = 最近N根(high-low)均值
        区间 = [中轴 - ATR*mult, 中轴 + ATR*mult]
        """
        symbols = list(context.data.keys()) if context.data else []
        if not symbols:
            logger.warning("auto_grid: 无历史数据，回退固定区间")
            self._grid_lines = np.linspace(
                self.grid_lower, self.grid_upper, self.grid_num + 1
            )
            self._grid_step = (self.grid_upper - self.grid_lower) / self.grid_num
            return

        # 用第一个有数据的标的计算区间（多标的网格建议用固定区间）
        df = context.get_data(symbols[0])
        if df is None or len(df) < self.auto_grid_lookback:
            logger.warning(
                f"auto_grid: 数据不足{self.auto_grid_lookback}根，回退固定区间"
            )
            self._grid_lines = np.linspace(
                self.grid_lower, self.grid_upper, self.grid_num + 1
            )
            self._grid_step = (self.grid_upper - self.grid_lower) / self.grid_num
            return

        lookback = df.tail(self.auto_grid_lookback)
        close = lookback['收盘价'].astype(float)
        mid = close.mean()

        # ATR 近似（high-low 均值，简化版）
        # P1-17修复：判定改中文列（此前 'high'/'low' 恒False，ATR≈(high-low)均值从未生效）
        if '最高价' in lookback.columns and '最低价' in lookback.columns:
            atr = (lookback['最高价'].astype(float) - lookback['最低价'].astype(float)).mean()
        else:
            atr = close.std()

        atr = max(atr, mid * 0.01)  # 防止ATR为0
        self.grid_lower = max(mid - atr * self.auto_grid_atr_mult, 0.01)
        self.grid_upper = mid + atr * self.auto_grid_atr_mult
        self._grid_lines = np.linspace(
            self.grid_lower, self.grid_upper, self.grid_num + 1
        )
        self._grid_step = (self.grid_upper - self.grid_lower) / self.grid_num

        logger.info(
            f"auto_grid计算完成: 中轴={mid:.2f}, ATR≈{atr:.2f}, "
            f"区间=[{self.grid_lower:.2f}, {self.grid_upper:.2f}]"
        )

    # ═══════════════════════════════════════════════
    #  核心信号生成
    # ═══════════════════════════════════════════════

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        """生成网格交易信号（核心方法）"""
        symbol = bar.symbol
        price = float(bar.close)

        # 数据不足或网格未初始化 → HOLD
        if self._grid_lines is None:
            return Signal(symbol=symbol, signal_type=SignalType.HOLD,
                          price=price, reason="grid_not_initialized")

        # 该标的已止损/止盈清仓 → 不再交易（等待人工重置或新周期）
        if self._closed.get(symbol, False):
            return Signal(symbol=symbol, signal_type=SignalType.HOLD,
                          price=price, reason="already_closed")

        # 1. 止损检查（跌破下界 stop_loss_pct）
        stop_price = self.grid_lower * (1.0 - self.stop_loss_pct)
        if price <= stop_price:
            self._closed[symbol] = True
            logger.warning(
                f"[网格止损] {symbol} 价格{price:.2f} <= 止损价{stop_price:.2f}, 清仓"
            )
            return Signal(
                symbol=symbol, signal_type=SignalType.STOP_LOSS,
                strength=1.0, price=price,
                reason=f"price<={stop_price:.2f}(grid_lower*{1-self.stop_loss_pct:.2f})",
                strategy_id=self.name,
            )

        # 2. 止盈检查（涨破上界 take_profit_pct）
        target_price = self.grid_upper * (1.0 + self.take_profit_pct)
        if price >= target_price:
            self._closed[symbol] = True
            logger.info(
                f"[网格止盈] {symbol} 价格{price:.2f} >= 止盈价{target_price:.2f}, 清仓"
            )
            return Signal(
                symbol=symbol, signal_type=SignalType.TAKE_PROFIT,
                strength=1.0, price=price,
                reason=f"price>={target_price:.2f}(grid_upper*{1+self.take_profit_pct:.2f})",
                strategy_id=self.name,
            )

        # 3. 计算当前价格所在网格位置（0 ~ grid_num）
        current_level = self._price_to_level(price)

        # 首次处理该标的 → 记录位置，不交易
        if symbol not in self._last_level:
            self._last_level[symbol] = current_level
            logger.debug(
                f"[网格初始化] {symbol} 价格{price:.2f} → 位置{current_level}/{self.grid_num}"
            )
            return Signal(symbol=symbol, signal_type=SignalType.HOLD,
                          price=price, reason=f"init_level={current_level}")

        last_level = self._last_level[symbol]

        # 4. 位置变化判断
        if current_level < last_level:
            # 跌破一格（或多格）→ 买入
            self._last_level[symbol] = current_level
            self._grid_buys += 1
            self._position_units[symbol] = self._position_units.get(symbol, 0) + 1
            grid_line_price = float(self._grid_lines[current_level])
            logger.info(
                f"[网格买入] {symbol} 位置 {last_level}→{current_level} "
                f"(价格{price:.2f}), 挂限价单@{grid_line_price:.2f}"
            )
            return Signal(
                symbol=symbol, signal_type=SignalType.BUY,
                strength=1.0, price=grid_line_price,
                reason=f"grid_drop {last_level}->{current_level}",
                strategy_id=self.name,
                extra={"grid_level": current_level, "limit_price": grid_line_price},
            )

        elif current_level > last_level:
            # 涨破一格（或多格）→ 卖出
            self._last_level[symbol] = current_level
            self._grid_sells += 1
            self._position_units[symbol] = max(0, self._position_units.get(symbol, 0) - 1)
            grid_line_price = float(self._grid_lines[current_level])
            logger.info(
                f"[网格卖出] {symbol} 位置 {last_level}→{current_level} "
                f"(价格{price:.2f}), 挂限价单@{grid_line_price:.2f}"
            )
            return Signal(
                symbol=symbol, signal_type=SignalType.SELL,
                strength=1.0, price=grid_line_price,
                reason=f"grid_rise {last_level}->{current_level}",
                strategy_id=self.name,
                extra={"grid_level": current_level, "limit_price": grid_line_price},
            )

        # 位置不变 → HOLD
        return Signal(symbol=symbol, signal_type=SignalType.HOLD,
                      price=price, reason=f"level_unchanged={current_level}")

    def _price_to_level(self, price: float) -> int:
        """价格 → 网格位置（0 ~ grid_num，clamp）"""
        if self._grid_step <= 0:
            return 0
        level = int((price - self.grid_lower) / self._grid_step)
        return max(0, min(level, self.grid_num))

    # ═══════════════════════════════════════════════
    #  订单生成（限价单，实盘挂单）
    # ═══════════════════════════════════════════════

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """每根bar处理（仅返回信号；订单由引擎统一执行）"""
        signal = self.generate_signal(bar, context)
        if signal.is_hold:
            return []

        self._signal_count += 1

        # P0-06修复：策略只产信号，订单由引擎统一执行
        # （引擎路径经 RiskManager / LookaheadGuard / signal_id 幂等去重）；
        # 不再在策略内 create_order + context.submit_order，避免双重报单并绕过风控。
        return [signal]

    # ═══════════════════════════════════════════════
    #  状态查询（供监控/报告使用）
    # ═══════════════════════════════════════════════

    def get_grid_status(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """获取网格策略状态（供实盘监控/Web看板使用）"""
        status = {
            "strategy": self.name,
            "grid_lower": self.grid_lower,
            "grid_upper": self.grid_upper,
            "grid_num": self.grid_num,
            "grid_step": self._grid_step,
            "grid_lines": self._grid_lines.tolist() if self._grid_lines is not None else [],
            "position_per_grid": self.position_per_grid,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "grid_buys": self._grid_buys,
            "grid_sells": self._grid_sells,
            "closed_symbols": [s for s, c in self._closed.items() if c],
        }
        if symbol:
            status["symbol"] = symbol
            status["last_level"] = self._last_level.get(symbol, -1)
            status["is_closed"] = self._closed.get(symbol, False)
        return status

    def on_stop(self, context: StrategyContext) -> None:
        """停止钩子：输出网格统计"""
        logger.info(
            f"网格策略停止: 买入{self._grid_buys}次, 卖出{self._grid_sells}次, "
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
        """mixed 引擎目标持仓：网格份数 × 每份股数。

        复用 generate_signal 更新网格状态（_last_level/_position_units/_closed），
        目标股数 = 累计网格份数 × 每份股数（cash×position_per_grid）。
        止损/止盈清仓后恒返回 0。
        """
        symbol = bar.symbol
        if self._closed.get(symbol, False):
            return 0.0
        signal = self.generate_signal(bar, context)
        if signal.signal_type in (SignalType.STOP_LOSS, SignalType.TAKE_PROFIT):
            return 0.0
        units = self._position_units.get(symbol, 0)
        if units <= 0:
            return 0.0
        cash = float(context.get_cash() or 0.0)
        per_unit = self._target_shares_from_capital(
            max(cash, 0.0), float(bar.close), self.position_per_grid,
        )
        return float(units) * per_unit

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """vectorized 引擎信号：目标持仓股数数组（网格状态机）。

        网格线按固定参数直接计算（vectorized 引擎不调用 on_init）；
        止损/止盈触发后清仓且不再交易。
        """
        n = len(data)
        if self._grid_lines is None:
            self._grid_lines = np.linspace(
                self.grid_lower, self.grid_upper, self.grid_num + 1
            )
            self._grid_step = (self.grid_upper - self.grid_lower) / self.grid_num
        close = data["收盘价"].astype(float).to_numpy()
        stop_price = self.grid_lower * (1.0 - self.stop_loss_pct)
        target_price = self.grid_upper * (1.0 + self.take_profit_pct)
        target = np.zeros(n)
        last_level: Optional[int] = None
        units = 0
        closed = False
        cash = max(float(self.capital_base), 0.0)
        for i in range(n):
            # 本行使用的数据截至 t 时刻（第 i 根，含第 i 根）
            c = float(close[i])
            if closed:
                continue
            if c <= stop_price or c >= target_price:
                closed = True
                units = 0
                continue
            level = (
                int((c - self.grid_lower) / self._grid_step)
                if self._grid_step > 0 else 0
            )
            level = max(0, min(level, self.grid_num))
            if last_level is None:
                last_level = level
            elif level < last_level:
                units += 1
                last_level = level
            elif level > last_level:
                units = max(0, units - 1)
                last_level = level
            per_unit = self._target_shares_from_capital(
                cash, c, self.position_per_grid,
            )
            target[i] = float(units) * per_unit
        return target


if __name__ == "__main__":
    # 自测 Demo：双接口 + 三引擎跑通
    import numpy as np
    import pandas as pd

    from myquant.backtest.engine import run_backtest
    from myquant.models.backtest import BacktestConfig

    n = 120
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(3)
    close = 15.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.008, n)))
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
    st = GridStrategy(grid_lower=12.0, grid_upper=18.0, grid_num=5,
                      position_per_grid=0.1, capital_base=1_000_000.0)
    assert st.supports_mode("mixed") and st.supports_mode("vectorized")
    arr = st.generate_signals_vectorized(df)
    assert len(arr) == n and float(arr[-1]) >= 0.0
    for mode in ("event", "mixed", "vectorized"):
        r = run_backtest(config, st, data=data, mode=mode)
        assert r.is_success, f"grid/{mode} 失败: {r.status}"
        print(
            f"  GridStrategy [{mode}] 净值点数={len(r.equity_curve)} "
            f"成交={len(r.trades)} 期末净值={r.final_equity:.4f}"
        )
    print("grid_strategy 双接口自测通过 ✅")

