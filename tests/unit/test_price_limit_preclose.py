# 文件名：tests/unit/test_price_limit_preclose.py
"""
test_price_limit_preclose.py - 涨跌停不可成交约束接线测试（KP-P2-10 / T-02）
============================================================================
覆盖"涨跌停校验恒空转（pre_close 恒 0）"修复：
  1. order_matcher 标量 fail-closed：启用涨跌停且 pre_close<=0 → 抛错，禁止空转。
  2. 正常 pre_close：涨停开盘买单不成交 / 跌停开盘卖单不成交。
  3. mixed 引擎涨停样本：买单成交数 0（前收盘价缺失时引擎兜底派生后仍正确拦截）。
  4. vectorized 引擎涨停样本：买单成交数 0。
  5. provider.get_bars：processed 缺前收盘价列 → 自动派生（收盘价.shift(1)）。
  6. DoD：涨停开盘日 生产双引擎（mixed/vectorized）买单均 0 成交。

依赖: pytest, myquant.backtest.order_matcher（OrderMatcher）
      myquant.backtest.mixed_engine（MixedBacktestEngine）
      myquant.backtest.vectorized_engine（VectorizedBacktestEngine）
      myquant.data_access.provider（HistoricalDataProvider）
      myquant.models.backtest（BacktestConfig）
      myquant.models.market_data（Bar）
      myquant.models.trade（Order/OrderSide/OrderType/Position）
      myquant.strategies.base（StrategyBase/StrategyContext/Signal/SignalType）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from myquant.backtest.mixed_engine import MixedBacktestEngine
from myquant.backtest.order_matcher import OrderMatcher
from myquant.backtest.vectorized_engine import VectorizedBacktestEngine
from myquant.data_access.provider import HistoricalDataProvider
from myquant.models.backtest import BacktestConfig
from myquant.models.market_data import Bar
from myquant.models.trade import Order, OrderSide, OrderType, Position
from myquant.strategies.base import (
    Signal,
    SignalType,
    StrategyBase,
    StrategyContext,
)

SYMBOL = "000001.SZ"


# ═══════════════════════════════════════════════
#  测试桩：行情 / 配置 / 策略
# ═══════════════════════════════════════════════

def _make_limitup_df() -> pd.DataFrame:
    """涨停开盘日样本：bar0/bar1 平价，末根 bar2 以涨停价(10×1.1=11.0)开盘。

    涨停日置于末根（其后无后续 bar），使涨停日买单被拦截后无法在后续 bar 补成交，
    从而三引擎"涨停开盘买单 0 成交"断言成立。不含前收盘价列（验证引擎兜底派生）。
    """
    closes = np.array([10.0, 10.0, 11.0])
    opens = np.array([10.0, 10.0, 11.0])  # 末根涨停开盘
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": opens,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": np.full(len(closes), 1_000_000.0),
        },
        index=idx,
    )


def _make_config(**kwargs: Any) -> BacktestConfig:
    defaults: Dict[str, Any] = dict(
        initial_capital=1_000_000.0,
        symbols=[SYMBOL],
        start_date="2024-01-01",
        end_date="2024-12-31",
        frequency="1d",
    )
    defaults.update(kwargs)
    return BacktestConfig(**defaults)


class _TargetVecStrategy(StrategyBase):
    """vectorized 桩：固定目标持仓股数序列。"""

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


def _make_order(side: OrderSide, qty: int = 100, symbol: str = SYMBOL) -> Order:
    return Order(symbol=symbol, side=side, order_type=OrderType.MARKET, quantity=qty)


def _make_bar(
    open_px: float,
    pre_close: float,
    volume: float = 1e6,
    symbol: str = SYMBOL,
) -> Bar:
    return Bar(
        symbol=symbol,
        open=open_px,
        high=open_px,
        low=open_px,
        close=open_px,
        pre_close=pre_close,
        volume=volume,
        timestamp=pd.Timestamp("2024-01-03").timestamp(),
    )


# ═══════════════════════════════════════════════
#  1. order_matcher 标量 fail-closed
# ═══════════════════════════════════════════════

class TestScalarFailClosed:
    """KP-P2-10：启用涨跌停且 pre_close<=0 → 抛错，禁止静默空转。"""

    def test_启用涨跌停_preclose缺失抛错(self) -> None:
        matcher = OrderMatcher(enable_price_limit=True)
        with pytest.raises(ValueError, match="pre_close 缺失"):
            matcher.match(_make_order(OrderSide.BUY), _make_bar(11.0, pre_close=0.0))

    def test_停牌优先于涨跌停不抛错(self) -> None:
        """停牌(volume=0)先返回，不会因 pre_close=0 进入涨跌停分支。"""
        matcher = OrderMatcher(enable_price_limit=True)
        bar = _make_bar(11.0, pre_close=0.0, volume=0)
        assert matcher.match(_make_order(OrderSide.BUY), bar) is None

    def test_禁用涨跌停preclose缺失不抛错(self) -> None:
        """enable_price_limit=False 时不做涨跌停校验，也不抛错（向后兼容）。"""
        matcher = OrderMatcher(enable_price_limit=False)
        assert matcher.match(_make_order(OrderSide.BUY), _make_bar(11.0, pre_close=0.0)) is not None


# ═══════════════════════════════════════════════
#  2. 正常 pre_close：涨停/跌停拦截
# ═══════════════════════════════════════════════

class TestPriceLimitBlock:
    """pre_close 有效时涨跌停开盘拦截（A股写实）。"""

    def test_涨停开盘买单不成交(self) -> None:
        matcher = OrderMatcher(enable_price_limit=True, price_limit_pct=0.10)
        # pre_close=10 → 涨停=11.0；open=11.0 ≥ 涨停 → 买单不成交
        assert matcher.match(_make_order(OrderSide.BUY), _make_bar(11.0, pre_close=10.0)) is None
        # 卖单在涨停开盘可成交
        assert matcher.match(_make_order(OrderSide.SELL), _make_bar(11.0, pre_close=10.0)) is not None

    def test_跌停开盘卖单不成交(self) -> None:
        matcher = OrderMatcher(enable_price_limit=True, price_limit_pct=0.10)
        # pre_close=10 → 跌停=9.0；open=9.0 ≤ 跌停 → 卖单不成交
        assert matcher.match(_make_order(OrderSide.SELL), _make_bar(9.0, pre_close=10.0)) is None
        # 买单在跌停开盘可成交
        assert matcher.match(_make_order(OrderSide.BUY), _make_bar(9.0, pre_close=10.0)) is not None


# ═══════════════════════════════════════════════
#  3. mixed / vectorized 引擎涨停样本：买单 0 成交
# ═══════════════════════════════════════════════

class TestEnginesLimitUpBlock:
    """DoD：涨停开盘日 生产双引擎买单成交数均 0（前收盘价缺失时引擎兜底派生）。

    #347 P0-03 卡本体收尾：mixed/vectorized 引擎 OrderMatcher 装配改为
    price_limit_pct=config.price_limit_pct + 板块映射（撮合层涨跌停校验恒启用，
    KP-P2-10 契约不变；enforce_limit 只控制 event 路径 SimBroker）。
    """

    # target[1]=1000 → 于末根(bar2)涨停开盘撮合 → 拦截；涨停日后无后续 bar → 全程 0 买单
    TARGETS: List[float] = [0.0, 1000.0, 1000.0]

    def test_mixed引擎_涨停买单0成交(self) -> None:
        df = _make_limitup_df()  # 无前收盘价列 → 引擎兜底派生
        engine = MixedBacktestEngine(
            config=_make_config(),
            strategy=_TargetLoopStrategy(self.TARGETS),
            data={SYMBOL: df},
        )
        result = engine.run()
        assert result.is_success
        buy_fills = [t for t in result.trades if t.side == OrderSide.BUY.value]
        assert len(buy_fills) == 0, f"涨停开盘买单应 0 成交，实际 {len(buy_fills)} 笔"

    def test_vectorized引擎_涨停买单0成交(self) -> None:
        # P1-10：vectorized 切 data_col 模式（缺前收盘价列即 fail-closed），
        # 此处直接提供真实前收盘价列（[NaN,10.0,10.0]）验证涨停拦截；
        # 引擎兜底派生行为由 mixed 路径与 provider 派生测试覆盖。
        df = _make_limitup_df()
        df["pre_close"] = [np.nan, 10.0, 10.0]
        engine = VectorizedBacktestEngine(
            config=_make_config(),
            strategy=_TargetVecStrategy(self.TARGETS),
            data={SYMBOL: df},
        )
        result = engine.run()
        assert result.is_success
        buy_fills = [t for t in result.trades if t.side == OrderSide.BUY.value]
        assert len(buy_fills) == 0, f"涨停开盘买单应 0 成交，实际 {len(buy_fills)} 笔"


# ═══════════════════════════════════════════════
#  P0-03 板块映射（#347）：OrderMatcher per-symbol 幅度
# ═══════════════════════════════════════════════

class TestPerSymbolBoardMapping:
    """price_limit_pct_by_symbol 命中时按标的幅度拦截（板块映射接生产调用）。

    创业板 20%：open=11.0（10×1.1）在 20% 幅度内 → 可成交；
    主板 10% 同 open 涨停 → 买单拦截；创业板 open=12.0（10×1.2）→ 涨停拦截。
    """

    def test创业板20pct_11元不拦截(self) -> None:
        matcher = OrderMatcher(
            enable_price_limit=True,
            price_limit_pct_by_symbol={"300750.SZ": 0.20},
        )
        order = _make_order(OrderSide.BUY, symbol="300750.SZ")
        # pre_close=10，创业板 20% → 涨停=12.0；open=11.0 未到涨停 → 买单成交
        assert matcher.match(order, _make_bar(11.0, pre_close=10.0)) is not None

    def test创业板20pct_12元涨停拦截(self) -> None:
        matcher = OrderMatcher(
            enable_price_limit=True,
            price_limit_pct_by_symbol={"300750.SZ": 0.20},
        )
        order = _make_order(OrderSide.BUY, symbol="300750.SZ")
        # open=12.0 ≥ 涨停 12.0 → 买单拦截
        assert matcher.match(order, _make_bar(12.0, pre_close=10.0)) is None

    def test主板10pct_11元涨停拦截(self) -> None:
        matcher = OrderMatcher(
            enable_price_limit=True,
            price_limit_pct_by_symbol={"600519.SH": 0.10},
        )
        order = _make_order(OrderSide.BUY, symbol="600519.SH")
        # 主板 10% → 涨停=11.0；open=11.0 → 买单拦截
        assert matcher.match(order, _make_bar(11.0, pre_close=10.0)) is None

    def test未映射symbol回退全局幅度(self) -> None:
        matcher = OrderMatcher(
            enable_price_limit=True,
            price_limit_pct=0.10,
            price_limit_pct_by_symbol={"300750.SZ": 0.20},
        )
        order = _make_order(OrderSide.BUY, symbol="000001.SZ")
        # 未映射 → 全局 10% → 涨停=11.0；open=11.0 → 拦截
        assert matcher.match(order, _make_bar(11.0, pre_close=10.0)) is None


# ═══════════════════════════════════════════════
#  P0-03 续卡（#348）：OrderMatcher 整手执行
# ═══════════════════════════════════════════════

class TestOrderMatcherLotSizeEnforcement:
    """OrderMatcher 按 lot_size 限制零股买入（买入整手截断/卖出不限/默认兼容）。

    match 标量：lot_size>0 且 BUY → floor 截断，不足一手不成交；SELL 不截断。
    match_vectorized：is_buy 行截断、is_sell 行原量；lot_size=0 时不限制。
    """

    def test_scalar_buy_truncated_to_lot(self) -> None:
        matcher = OrderMatcher(lot_size=100)
        order = _make_order(OrderSide.BUY, qty=150)
        fill = matcher.match(order, _make_bar(10.0, pre_close=10.0))
        assert fill is not None
        assert fill.quantity == 100

    def test_scalar_buy_below_one_lot_not_filled(self) -> None:
        matcher = OrderMatcher(lot_size=100)
        order = _make_order(OrderSide.BUY, qty=50)
        # 不足一手 → qty=0 → 不成交
        assert matcher.match(order, _make_bar(10.0, pre_close=10.0)) is None

    def test_scalar_sell_not_truncated(self) -> None:
        matcher = OrderMatcher(lot_size=100)
        order = _make_order(OrderSide.SELL, qty=150)
        fill = matcher.match(order, _make_bar(10.0, pre_close=10.0))
        assert fill is not None
        assert fill.quantity == 150

    def test_scalar_default_lot_zero_backward_compatible(self) -> None:
        matcher = OrderMatcher()
        order = _make_order(OrderSide.BUY, qty=50)
        fill = matcher.match(order, _make_bar(10.0, pre_close=10.0))
        assert fill is not None
        assert fill.quantity == 50

    def test_vectorized_buy_truncated_sell_not(self) -> None:
        matcher = OrderMatcher(lot_size=100)
        result = matcher.match_vectorized(
            sides=np.array([OrderSide.BUY.value, OrderSide.BUY.value, OrderSide.SELL.value], dtype=object),
            order_types=np.array(["MARKET", "MARKET", "MARKET"], dtype=object),
            limit_prices=np.full(3, np.nan),
            stop_prices=np.full(3, np.nan),
            opens=np.full(3, 10.0),
            highs=np.full(3, 10.0),
            lows=np.full(3, 10.0),
            quantities=np.array([150.0, 50.0, 150.0]),
            pre_closes=np.full(3, 10.0),
            volumes=np.full(3, 1e6),
        )
        # 买入 150 → 100；买入 50（不足一手）→ 0；卖出 150 → 150
        assert list(result.fill_quantities) == [100.0, 0.0, 150.0]


# ═══════════════════════════════════════════════
#  5. provider.get_bars 前收盘价兜底派生
# ═══════════════════════════════════════════════

class TestProviderPreCloseDerive:
    """KP-P2-10：加载器统一兜底派生前收盘价（收盘价.shift(1)）。"""

    def test_processed缺前收盘价自动派生(self, tmp_path: Path) -> None:
        df = _make_limitup_df()
        path = tmp_path / f"{SYMBOL}.parquet"
        df.to_parquet(path)
        provider = HistoricalDataProvider(processed_dir=tmp_path, default_adjust="raw")
        out = provider.get_bars(SYMBOL)
        assert not out.empty
        assert "前收盘价" in out.columns
        np.testing.assert_allclose(
            out["前收盘价"].iloc[1:].to_numpy(),
            out["收盘价"].shift(1).iloc[1:].to_numpy(),
        )
        # 首行无前驱 → NaN
        assert pd.isna(out["前收盘价"].iloc[0])

    def test_processed含前收盘价不透传篡改(self, tmp_path: Path) -> None:
        df = _make_limitup_df()
        df["前收盘价"] = [np.nan, 10.0, 10.0]
        path = tmp_path / f"{SYMBOL}.parquet"
        df.to_parquet(path)
        provider = HistoricalDataProvider(processed_dir=tmp_path, default_adjust="raw")
        out = provider.get_bars(SYMBOL)
        np.testing.assert_allclose(out["前收盘价"].iloc[1:].to_numpy(), [10.0, 10.0])


# ═══════════════════════════════════════════════
#  6. 集成：mixed 引擎 pre_close 缺失 + 跌停拦截联动
# ═══════════════════════════════════════════════

class TestMixedEngineDeriveIntegration:
    """mixed 引擎兜底派生 + 涨跌停拦截联合生效。"""

    def test_缺失前收盘价_跌停卖单0成交(self) -> None:
        # 构造跌停开盘日：bar0/bar1 平价，末根 bar2 以跌停价(10×0.9=9.0)开盘；
        # target[0]=1000 → bar1 正常买入成交，target[1]=0 → bar2 跌停开盘卖出被拦截，
        # 跌停日后无后续 bar → 卖单全程 0 成交
        closes = np.array([10.0, 10.0, 9.0])
        opens = np.array([10.0, 10.0, 9.0])
        idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
        df = pd.DataFrame(
            {
                "open": opens,
                "high": closes,
                "low": closes,
                "close": closes,
                "volume": np.full(len(closes), 1_000_000.0),
            },
            index=idx,
        )
        engine = MixedBacktestEngine(
            config=_make_config(),
            strategy=_TargetLoopStrategy([1000.0, 0.0, 0.0]),
            data={SYMBOL: df},
        )
        result = engine.run()
        assert result.is_success
        # bar1 正常买入成交（非跌停日）
        buy_fills = [t for t in result.trades if t.side == OrderSide.BUY.value]
        assert len(buy_fills) == 1, f"bar1 应买入 1 笔，实际 {len(buy_fills)} 笔"
        # bar2 跌停开盘卖单被拦截 → 0 卖单
        sell_fills = [t for t in result.trades if t.side == OrderSide.SELL.value]
        assert len(sell_fills) == 0, f"跌停开盘卖单应 0 成交，实际 {len(sell_fills)} 笔"
