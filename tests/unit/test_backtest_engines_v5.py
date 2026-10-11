# 文件名：tests/unit/test_backtest_engines_v5.py
"""
test_backtest_engines_v5.py - P1 第9项：vectorized / mixed 引擎单元测试
======================================================================
覆盖 myquant/backtest/ V5 引擎核心层：
  - vectorized_engine.VectorizedBacktestEngine   纯向量化引擎
  - mixed_engine.MixedBacktestEngine             混合引擎（生产主力）

测试维度:
  1. vectorized：T+1 时序、成交价、手续费/滑点、多标的、防御校验、手算净值
  2. mixed：target 模式、止损、条件单(STOP_LIMIT)、on_bar 信号回退、防御校验
  3. 一致性（验收指标）：mixed vs vectorized 净值逐点一致 + 成交费用逐笔一致

运行方式:
  pytest tests/unit/test_backtest_engines_v5.py -v
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from myquant.backtest.fee_common import FeeCalculator
from myquant.backtest.mixed_engine import MixedBacktestEngine
from myquant.backtest.vectorized_engine import VectorizedBacktestEngine
from myquant.models.backtest import BacktestConfig
from myquant.models.market_data import Bar
from myquant.models.trade import Order, OrderSide, OrderType, Position
from myquant.strategies.base import (
    Signal,
    SignalType,
    StrategyBase,
    StrategyContext,
)


# ═══════════════════════════════════════════════
#  测试桩：行情数据 / 配置 / 策略
# ═══════════════════════════════════════════════

def _make_ohlcv(n: int, base: float = 10.0, drift: float = 0.02) -> pd.DataFrame:
    """构造合成行情 DataFrame（n 根日线，价格缓慢上涨）。"""
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = base * (1.0 + drift) ** np.arange(n)
    return pd.DataFrame(
        {
            "open": close * 0.995,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            # P1-10：模拟生产链路 provider 派生的前收盘价列（引擎 data_col 模式要求上游提供）
            "pre_close": np.concatenate([[np.nan], close[:-1]]),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def _make_downtrend(n: int) -> pd.DataFrame:
    """构造先涨后跌的行情 DataFrame（用于止损触发测试）。"""
    closes = np.array(
        [10.0, 10.2, 10.4, 10.0, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5][:n]
    )
    idx = pd.date_range("2024-02-01", periods=n, freq="B")
    open_px = closes * 0.995
    high = np.maximum(open_px, closes) * 1.02
    low = np.minimum(open_px, closes) * 0.98
    return pd.DataFrame(
        {
            "open": open_px,
            "high": high,
            "low": low,
            "close": closes,
            # P1-10：模拟生产链路 provider 派生的前收盘价列（引擎 data_col 模式要求上游提供）
            "pre_close": np.concatenate([[np.nan], closes[:-1]]),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def _make_config(symbols: Optional[List[str]] = None, **kwargs: Any) -> BacktestConfig:
    """构造回测配置（默认参数）。"""
    defaults: Dict[str, Any] = dict(
        initial_capital=1_000_000.0,
        symbols=symbols or ["000001.SZ"],
        start_date="2024-01-01",
        end_date="2024-12-31",
        frequency="1d",
    )
    defaults.update(kwargs)
    return BacktestConfig(**defaults)


class _TargetVecStrategy(StrategyBase):
    """vectorized 桩：固定目标持仓股数序列。

    generate_signals_vectorized 直接返回预设 target 股数序列
    （T 日收盘决定，引擎按 position[t]=target[t-1] 于 T+1 开盘撮合）。
    """

    name = "target_vec"

    def __init__(self, targets: Optional[List[float]] = None, **params: Any):
        super().__init__(**params)
        self.targets: List[float] = list(targets or [])

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        n = len(data)
        out = np.zeros(n, dtype=np.float64)
        t = np.asarray(self.targets, dtype=np.float64)
        m = min(n, t.size)
        out[:m] = t[:m]
        return out

    def generate_signal(self, bar: Any, context: StrategyContext):
        # 向量化模式不使用事件信号
        return None


class _TargetLoopStrategy(StrategyBase):
    """mixed 桩：generate_mixed_loop 返回与 vectorized 相同的目标序列。"""

    name = "target_loop"

    def __init__(self, targets: Optional[List[float]] = None, **params: Any):
        super().__init__(**params)
        self.targets: List[float] = list(targets or [])
        self._bar_idx: int = 0

    def reset(self) -> None:
        self._bar_idx = 0

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        i = self._bar_idx
        self._bar_idx += 1
        if i < len(self.targets):
            return self.targets[i]
        return 0.0

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)


class _SignalLoopStrategy(StrategyBase):
    """mixed on_bar 桩：指定 bar 序号触发买卖信号（事件信号模式）。"""

    name = "signal_loop"

    def __init__(
        self,
        buy_at: Optional[List[int]] = None,
        sell_at: Optional[List[int]] = None,
        **params: Any,
    ):
        super().__init__(**params)
        self.buy_at: List[int] = list(buy_at or [])
        self.sell_at: List[int] = list(sell_at or [])
        self._bar_idx: int = 0

    def reset(self) -> None:
        self._bar_idx = 0

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        i = self._bar_idx
        self._bar_idx += 1
        signals: List[Signal] = []
        if i in self.buy_at:
            signals.append(Signal(symbol=bar.symbol, signal_type=SignalType.BUY))
        if i in self.sell_at:
            signals.append(Signal(symbol=bar.symbol, signal_type=SignalType.SELL))
        return signals

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)


class _StopLossStrategy(StrategyBase):
    """mixed 桩：满仓买入后持仓不动，由引擎 stop_loss_pct 触发止损。"""

    name = "stop_loss"

    def __init__(self, targets: Optional[List[float]] = None, **params: Any):
        super().__init__(**params)
        self.targets: List[float] = list(targets or [])
        self._bar_idx: int = 0

    def reset(self) -> None:
        self._bar_idx = 0

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        i = self._bar_idx
        self._bar_idx += 1
        if i < len(self.targets):
            return self.targets[i]
        return 0.0

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)


class _ConditionalLoopStrategy(StrategyBase):
    """mixed 桩：第 0 根收盘提交 STOP_LIMIT 突破买入条件单。"""

    name = "conditional_loop"

    def __init__(self, **params: Any):
        super().__init__(**params)
        self._bar_idx = 0
        self.submitted: bool = False

    def reset(self) -> None:
        self._bar_idx = 0
        self.submitted = False

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        i = self._bar_idx
        self._bar_idx += 1
        if i == 0 and not self.submitted:
            self.submitted = True
            order = self.create_order(
                symbol=bar.symbol,
                side=OrderSide.BUY,
                quantity=1000.0,
                order_type=OrderType.STOP_LIMIT,
                price=10.3,
                reason="conditional",
            )
            order.stop_price = 10.2
            context.submit_order(order)
        return 0.0

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)


# 共用的固定目标序列：t=2 起持仓 1000 股，t=6 平仓
TARGETS: List[float] = [0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 0.0, 0.0, 0.0, 0.0]


# ═══════════════════════════════════════════════
#  1. vectorized 引擎
# ═══════════════════════════════════════════════

class TestVectorizedEngine:
    """纯向量化引擎"""

    def test_basic_run_success(self) -> None:
        """基本回测运行成功且 completed"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(TARGETS), data=data
        )
        result = engine.run()
        assert result is not None and result.is_success
        assert result.status == "completed"

    def test_equity_curve_length(self) -> None:
        """净值曲线点数 = bar 数"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(TARGETS), data=data
        )
        result = engine.run()
        assert len(result.equity_curve) == 10

    def test_t_plus_one_timing(self) -> None:
        """T+1 时序：target[2]=1000 → 第 3 根起持仓（position[t]=target[t-1]）"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(TARGETS), data=data
        )
        result = engine.run()
        buys = [t for t in result.trades if t.side == OrderSide.BUY.value]
        assert len(buys) == 1
        assert buys[0].quantity == pytest.approx(1000.0)

    def test_fill_price_is_next_open(self) -> None:
        """买入成交价 = T+1 开盘价"""
        data = {"000001.SZ": _make_ohlcv(10)}
        open3 = data["000001.SZ"]["open"].iloc[3]
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(TARGETS), data=data
        )
        result = engine.run()
        buys = [t for t in result.trades if t.side == OrderSide.BUY.value]
        assert buys[0].price == pytest.approx(float(open3), abs=1e-9)

    def test_commission_matches_fee_calculator(self) -> None:
        """TradeRecord.commission = 总手续费（佣金+印花税+过户费），与 FeeCalculator 对拍"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(TARGETS), data=data
        )
        result = engine.run()
        calc = FeeCalculator()
        for trade in result.trades:
            # 用成交价 × 数量 计算金额基数（TradeRecord.price 为成交价，不含滑点）
            notional = trade.price * trade.quantity
            bd = calc.calc_fee(
                OrderSide(trade.side),
                notional,
                price=trade.price,
                quantity=trade.quantity,
            )
            assert trade.commission == pytest.approx(bd.total_commission, abs=1e-6)
            assert trade.slippage == pytest.approx(bd.slippage, abs=1e-6)

    def test_sell_includes_stamp_tax(self) -> None:
        """卖出成交手续费含印花税"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(TARGETS), data=data
        )
        result = engine.run()
        sells = [t for t in result.trades if t.side == OrderSide.SELL.value]
        assert len(sells) == 1
        notional = sells[0].price * sells[0].quantity
        calc = FeeCalculator()
        bd = calc.calc_fee(OrderSide.SELL, notional)
        assert sells[0].commission == pytest.approx(bd.total_commission, abs=1e-6)
        assert bd.stamp_tax > 0

    def test_multi_symbol(self) -> None:
        """多标的回测成功（组合净值）"""
        data = {
            "000001.SZ": _make_ohlcv(10, base=10.0),
            "600519.SH": _make_ohlcv(10, base=8.0),
        }
        engine = VectorizedBacktestEngine(
            config=_make_config(symbols=["000001.SZ", "600519.SH"]),
            strategy=_TargetVecStrategy(TARGETS),
            data=data,
        )
        result = engine.run()
        assert result is not None and result.is_success
        assert len(result.equity_curve) == 10

    def test_empty_data_failed(self) -> None:
        """空数据返回 failed 状态"""
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy([]), data={}
        )
        result = engine.run()
        assert result is not None and not result.is_success
        assert result.status == "failed"

    def test_invalid_capital_mode(self) -> None:
        """非法 capital_mode 抛 ValueError"""
        data = {"000001.SZ": _make_ohlcv(10)}
        with pytest.raises(ValueError):
            VectorizedBacktestEngine(
                config=_make_config(),
                strategy=_TargetVecStrategy(TARGETS),
                data=data,
                capital_mode="bogus",
            )

    def test_manual_equity_consistency(self) -> None:
        """期末净值与手算一致（买入/卖出 + 费用）"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(TARGETS), data=data
        )
        result = engine.run()
        # 手算：初始资金 + Σ(卖出净额) - Σ(买入净额)
        cash = 1_000_000.0
        for trade in result.trades:
            gross = trade.price * trade.quantity
            if trade.side == OrderSide.SELL.value:
                cash += gross - trade.commission - trade.slippage
            else:
                cash -= gross + trade.commission + trade.slippage
        # 期末无持仓（TARGETS 末尾为 0），总资产 = 现金；final_equity 为归一化净值
        assert result.final_equity == pytest.approx(cash / 1_000_000.0, abs=1e-6)


# ═══════════════════════════════════════════════
#  2. mixed 引擎
# ═══════════════════════════════════════════════

class TestMixedEngine:
    """混合引擎（生产主力）"""

    def test_target_mode_run(self) -> None:
        """target 模式回测成功"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = MixedBacktestEngine(
            config=_make_config(), strategy=_TargetLoopStrategy(TARGETS), data=data
        )
        result = engine.run()
        assert result is not None and result.is_success

    def test_target_t_plus_one(self) -> None:
        """target 模式 T+1 时序：t=2 目标 1000 → t=3 开盘成交"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = MixedBacktestEngine(
            config=_make_config(), strategy=_TargetLoopStrategy(TARGETS), data=data
        )
        result = engine.run()
        buys = [t for t in result.trades if t.side == OrderSide.BUY.value]
        assert len(buys) == 1
        open3 = data["000001.SZ"]["open"].iloc[3]
        assert buys[0].price == pytest.approx(float(open3), abs=1e-9)

    def test_stop_loss(self) -> None:
        """止损触发产生卖出（下跌行情）"""
        data = {"000001.SZ": _make_downtrend(10)}
        # 满仓 1000 股持有，价格回落触发 10% 止损
        hold = [1000.0] * 10
        engine = MixedBacktestEngine(
            config=_make_config(),
            strategy=_StopLossStrategy(hold),
            data=data,
            stop_loss_pct=0.10,
        )
        result = engine.run()
        assert result is not None and result.is_success
        sells = [t for t in result.trades if t.side == OrderSide.SELL.value]
        assert len(sells) >= 1
        stop_sells = [t for t in sells if t.exit_reason == "stop_loss"]
        assert len(stop_sells) >= 1

    def test_stop_loss_fill_price(self) -> None:
        """止损卖出成交价 = min(止损价, 触发日开盘)"""
        data = {"000001.SZ": _make_downtrend(10)}
        hold = [1000.0] * 10
        engine = MixedBacktestEngine(
            config=_make_config(),
            strategy=_StopLossStrategy(hold),
            data=data,
            stop_loss_pct=0.10,
        )
        result = engine.run()
        stop_sells = [t for t in result.trades if t.exit_reason == "stop_loss"]
        assert stop_sells
        # 成本价 = 买入成交价，止损价 = 成本 × 0.9
        buys = [t for t in result.trades if t.side == OrderSide.BUY.value]
        cost = buys[0].price
        stop_px = cost * 0.9
        # 止损单按 STOP 卖规则撮合：price = min(stop_px, 触发日 open)
        assert stop_sells[0].price <= stop_px + 1e-9

    def test_conditional_order(self) -> None:
        """条件单（STOP_LIMIT）触发买入"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = MixedBacktestEngine(
            config=_make_config(), strategy=_ConditionalLoopStrategy(), data=data
        )
        result = engine.run()
        buys = [t for t in result.trades if t.side == OrderSide.BUY.value]
        assert len(buys) == 1

    def test_on_bar_signal_mode(self) -> None:
        """on_bar 信号模式回退：买入/卖出各 1 笔"""
        data = {"000001.SZ": _make_ohlcv(10)}
        engine = MixedBacktestEngine(
            config=_make_config(), strategy=_SignalLoopStrategy(buy_at=[3], sell_at=[7]), data=data
        )
        result = engine.run()
        buys = [t for t in result.trades if t.side == OrderSide.BUY.value]
        sells = [t for t in result.trades if t.side == OrderSide.SELL.value]
        assert len(buys) == 1
        assert len(sells) == 1

    def test_invalid_stop_loss_pct(self) -> None:
        """非法 stop_loss_pct 抛 ValueError"""
        data = {"000001.SZ": _make_ohlcv(10)}
        with pytest.raises(ValueError):
            MixedBacktestEngine(
                config=_make_config(),
                strategy=_TargetLoopStrategy(TARGETS),
                data=data,
                stop_loss_pct=1.5,
            )

    def test_empty_data_failed(self) -> None:
        """空数据返回 failed 状态"""
        engine = MixedBacktestEngine(
            config=_make_config(), strategy=_TargetLoopStrategy([]), data={}
        )
        result = engine.run()
        assert result is not None and not result.is_success


# ═══════════════════════════════════════════════
#  3. 三引擎一致性（验收指标）
# ═══════════════════════════════════════════════

class TestEngineConsistency:
    """mixed vs vectorized 一致性"""

    def test_equity_curves_identical(self) -> None:
        """同一策略/数据/配置：mixed 与 vectorized 净值逐点一致"""
        data = {"000001.SZ": _make_ohlcv(10)}
        cfg = _make_config()
        vec_engine = VectorizedBacktestEngine(
            config=cfg, strategy=_TargetVecStrategy(TARGETS), data=data
        )
        mix_engine = MixedBacktestEngine(
            config=cfg, strategy=_TargetLoopStrategy(TARGETS), data=data
        )
        vec_result = vec_engine.run()
        mix_result = mix_engine.run()
        assert vec_result.is_success and mix_result.is_success
        vec_eq = [p.equity for p in vec_result.equity_curve]
        mix_eq = [p.equity for p in mix_result.equity_curve]
        assert len(vec_eq) == len(mix_eq)
        np.testing.assert_allclose(np.asarray(vec_eq), np.asarray(mix_eq), rtol=1e-9, atol=1e-9)

    def test_trades_identical(self) -> None:
        """mixed 与 vectorized 成交逐笔一致（价格/数量/手续费/滑点）"""
        data = {"000001.SZ": _make_ohlcv(10)}
        cfg = _make_config()
        vec_result = VectorizedBacktestEngine(
            config=cfg, strategy=_TargetVecStrategy(TARGETS), data=data
        ).run()
        mix_result = MixedBacktestEngine(
            config=cfg, strategy=_TargetLoopStrategy(TARGETS), data=data
        ).run()
        assert len(vec_result.trades) == len(mix_result.trades)
        for vt, mt in zip(vec_result.trades, mix_result.trades):
            assert vt.side == mt.side
            assert vt.quantity == pytest.approx(mt.quantity, abs=1e-9)
            assert vt.price == pytest.approx(mt.price, abs=1e-9)
            assert vt.commission == pytest.approx(mt.commission, abs=1e-9)
            assert vt.slippage == pytest.approx(mt.slippage, abs=1e-9)

    def test_multi_symbol_consistency(self) -> None:
        """多标的下 mixed 与 vectorized 逐标的费用/撮合一致

        注：两引擎多标组合的资金模型不同（vectorized 默认 per_symbol 每标的独立
        资金；mixed 单账户多标的共享资金），故组合净值不可直接对拍。
        一致性按"同标的同口径"逐标的验证：成交（side/quantity/price/
        commission/slippage）与期末净值逐点一致——对应验收指标
        "三套引擎费用、撮合计算结果一致"。
        """
        data = {
            "000001.SZ": _make_ohlcv(10, base=10.0),
            "600519.SH": _make_ohlcv(10, base=8.0),
        }
        for symbol, df in data.items():
            cfg = _make_config(symbols=[symbol])
            vec_result = VectorizedBacktestEngine(
                config=cfg, strategy=_TargetVecStrategy(TARGETS), data={symbol: df}
            ).run()
            mix_result = MixedBacktestEngine(
                config=cfg, strategy=_TargetLoopStrategy(TARGETS), data={symbol: df}
            ).run()
            assert vec_result.is_success and mix_result.is_success
            # 期末净值一致
            assert mix_result.final_equity == pytest.approx(vec_result.final_equity, abs=1e-9)
            # 成交逐笔一致
            assert len(vec_result.trades) == len(mix_result.trades)
            for vt, mt in zip(vec_result.trades, mix_result.trades):
                assert (vt.symbol, vt.side) == (mt.symbol, mt.side)
                assert vt.quantity == pytest.approx(mt.quantity, abs=1e-9)
                assert vt.price == pytest.approx(mt.price, abs=1e-9)
                assert vt.commission == pytest.approx(mt.commission, abs=1e-9)
                assert vt.slippage == pytest.approx(mt.slippage, abs=1e-9)

    def test_final_equity_matches_fee_calculator(self) -> None:
        """mixed 引擎全部成交费用与 FeeCalculator 逐笔对拍（费用一致）"""
        data = {"000001.SZ": _make_ohlcv(10)}
        mix_result = MixedBacktestEngine(
            config=_make_config(), strategy=_TargetLoopStrategy(TARGETS), data=data
        ).run()
        calc = FeeCalculator()
        assert mix_result.trades
        for trade in mix_result.trades:
            notional = trade.price * trade.quantity
            bd = calc.calc_fee(
                OrderSide(trade.side),
                notional,
                price=trade.price,
                quantity=trade.quantity,
            )
            assert trade.commission == pytest.approx(bd.total_commission, abs=1e-6)
            assert trade.slippage == pytest.approx(bd.slippage, abs=1e-6)


# ═══════════════════════════════════════════
#  P0-10回归：停牌日买单未成交 → 不得记幽灵持仓
# ═══════════════════════════════════════════

class TestP010GhostPosition:
    """P0-10：持仓必须按实际成交量回写，未成交不得虚记持仓。

    原缺陷：position 全程由 target 递推，而现金流只对 traded 生效——
    停牌(volume=0)/涨跌停拦截导致买单未成交时，cashflow=0 但持仓仍记为已持有
    → 幽灵持仓、净值虚高（污染参数扫描与生产阶段一筛选）。
    """

    def test_halted_buy_not_recorded_as_position(self) -> None:
        """第3根(index=2)停牌：T+1买入1000股无法成交 → 无BUY成交、其后持仓市值恒为0"""
        n = 8
        closes = np.full(n, 10.0)
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        vol = np.full(n, 1_000_000.0)
        vol[2] = 0.0  # 停牌日
        df = pd.DataFrame(
            {
                "open": closes * 0.995,
                "high": closes * 1.02,
                "low": closes * 0.98,
                "close": closes,
                # P1-10：模拟生产链路 provider 派生的前收盘价列
                "pre_close": np.concatenate([[np.nan], closes[:-1]]),
                "volume": vol,
            },
            index=idx,
        )
        # target[1]=1000 → T+1 在 index=2 开盘买1000股（恰遇停牌）
        targets = [0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0]
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(targets),
            data={"000001.SZ": df},
        )
        result = engine.run()
        assert result.is_success

        # 停牌日买单未成交 → 无任何 BUY 成交记录
        buys = [t for t in result.trades if t.side == OrderSide.BUY.value]
        assert buys == [], f"停牌日不应成交，实际BUY成交: {buys}"

        # 停牌后任一日持仓市值必须为0（原缺陷会记为 1000股×10元=1万幽灵持仓）
        for pt in result.equity_curve[2:]:
            assert pt.position_value == pytest.approx(0.0, abs=1e-6), (
                f"P0-10：{pt.date} 出现幽灵持仓市值 {pt.position_value}"
            )


class TestP103LightGuardWired:
    """P1-03回归：轻量防未来防线（第二道）已接入引擎收尾校验。"""

    def test_vectorized_marks_light_guard_passed(self) -> None:
        from myquant.backtest.vectorized_engine import VectorizedBacktestEngine
        engine = VectorizedBacktestEngine(
            config=_make_config(), strategy=_TargetVecStrategy(TARGETS),
            data={"000001.SZ": _make_ohlcv(10)},
        )
        result = engine.run()
        assert result.is_success
        assert result.extra.get("light_lookahead_passed") is True