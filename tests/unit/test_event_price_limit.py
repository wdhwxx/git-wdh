# 文件名：tests/unit/test_event_price_limit.py
"""
test_event_price_limit.py - event 路径涨跌停/停牌校验测试（KP-P2-02 / T-17）
============================================================================
覆盖"event 引擎不校验涨跌停/停牌"修复（缺陷范围仅 event 路径，mixed/vectorized
走 OrderMatcher 已有判定）：
  1. 涨停拒买（enforce_limit=True）。
  2. 跌停拒卖。
  3. 停牌跳过。
  4. 非涨跌停正常成交。
  5. 默认 enforce_limit=False 不影响既有撮合。

依赖: pytest, myquant.execution.sim_broker, myquant.models.trade / market_data
"""

from __future__ import annotations

from myquant.execution.sim_broker import SimBroker
from myquant.models.market_data import Bar, BarFrequency
from myquant.models.trade import Order, OrderSide, OrderType


def _make_bar(symbol: str = "000001.SZ", open_: float = 10.0, close: float = 10.0) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=1_700_000_000.0,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1_000_000,
        frequency=BarFrequency.DAILY,
    )


def _buy_order(symbol: str = "000001.SZ") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=10.0,
    )


def _sell_order(symbol: str = "000001.SZ") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=100,
        price=10.0,
    )


class TestEventPriceLimit:
    def test_涨停拒买(self) -> None:
        """DoD：昨收10 → 次日开盘11.0(涨停) → 买单拒（无成交且订单撤销）。"""
        broker = SimBroker(enforce_limit=True, price_limit_pct=0.10)
        broker.on_bar(_make_bar(open_=10.0, close=10.0))  # 首根设置昨收
        order = broker.submit_order(_buy_order())
        trades = broker.on_bar(_make_bar(open_=11.0, close=11.0))  # 涨停开盘
        assert trades == []
        assert order.status.is_active is False  # 已撤销

    def test_跌停拒卖(self) -> None:
        """DoD：昨收10 → 次日开盘9.0(跌停) → 卖单拒。"""
        broker = SimBroker(enforce_limit=True, price_limit_pct=0.10)
        broker.on_bar(_make_bar(open_=10.0, close=10.0))
        order = broker.submit_order(_sell_order())
        trades = broker.on_bar(_make_bar(open_=9.0, close=9.0))  # 跌停开盘
        assert trades == []
        assert order.status.is_active is False

    def test_停牌跳过(self) -> None:
        """DoD：is_suspended 命中 → 拒单。"""
        broker = SimBroker(
            enforce_limit=True,
            is_suspended=lambda s: s == "000001.SZ",
        )
        broker.on_bar(_make_bar(open_=10.0, close=10.0))
        order = broker.submit_order(_buy_order())
        trades = broker.on_bar(_make_bar(open_=10.5, close=10.5))
        assert trades == []
        assert order.status.is_active is False

    def test_非涨跌停正常成交(self) -> None:
        """DoD：开盘价未触涨跌停 → 正常成交。"""
        broker = SimBroker(enforce_limit=True, price_limit_pct=0.10)
        broker.on_bar(_make_bar(open_=10.0, close=10.0))
        broker.submit_order(_buy_order())
        trades = broker.on_bar(_make_bar(open_=10.5, close=10.5))
        assert len(trades) == 1
        assert trades[0].side == OrderSide.BUY

    def test_默认不强制_不影响既有撮合(self) -> None:
        """DoD：enforce_limit 默认 False → 涨停开盘仍撮合（保持既有行为）。"""
        broker = SimBroker()  # 默认
        broker.on_bar(_make_bar(open_=10.0, close=10.0))
        broker.submit_order(_buy_order())
        trades = broker.on_bar(_make_bar(open_=11.0, close=11.0))
        assert len(trades) == 1


class TestPriceLimitP003:
    """P0-03 第五刀：四组断言之"改幅度"与"板块映射"。"""

    def test_改幅度_5pct生效(self) -> None:
        """price_limit_pct=0.05 → 5% 幅度：昨收10 → 涨停10.5 → 10.5 开盘买单拒。"""
        broker = SimBroker(enforce_limit=True, price_limit_pct=0.05)
        broker.on_bar(_make_bar(open_=10.0, close=10.0))  # 建立昨收
        broker.submit_order(_buy_order())
        trades = broker.on_bar(_make_bar(open_=10.5, close=10.5))  # 5% 涨停
        assert len(trades) == 0, f"5% 涨停买单应 0 成交，实际 {len(trades)} 笔"

    def test_改幅度_5pct_中间价放行(self) -> None:
        """price_limit_pct=0.05 → 5% 幅度：昨收10 → 10.3 开盘（未达5%涨停）→ 买单成交。"""
        broker = SimBroker(enforce_limit=True, price_limit_pct=0.05)
        broker.on_bar(_make_bar(open_=10.0, close=10.0))
        broker.submit_order(_buy_order())
        trades = broker.on_bar(_make_bar(open_=10.3, close=10.3))  # 未达 5% 涨停
        assert len(trades) == 1

    def test_板块映射_创业板20pct(self) -> None:
        """板块映射：30xxxx 创业板 → 20% 幅度（price_limit_pct_for_symbol 返回 0.20）。"""
        from myquant.models.backtest import price_limit_pct_for_symbol
        # 创业板股票代码 → 应返回 0.20
        assert price_limit_pct_for_symbol("300750.SZ") == 0.20
        assert price_limit_pct_for_symbol("300001.SZ") == 0.20

    def test_板块映射_主板10pct(self) -> None:
        """板块映射：60xxxx 沪市主板 → 10% 幅度。"""
        from myquant.models.backtest import price_limit_pct_for_symbol
        assert price_limit_pct_for_symbol("600519.SH") == 0.10
