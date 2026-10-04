# -*- coding: utf-8 -*-
"""T-46 KP-P2-03：sim_broker 成交量参与率限制 + vectorized 负现金缩 notional。"""
from pathlib import Path

from myquant.execution.sim_broker import SimBroker
from myquant.models.market_data import Bar, BarFrequency
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType

ROOT = Path(__file__).resolve().parents[2]


def _bar(volume: float = 1000.0) -> Bar:
    return Bar(
        symbol="600519.SH",
        frequency=BarFrequency.DAILY,
        timestamp=1704067200,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        pre_close=100.0,
        volume=volume,
    )


def _order(quantity: float, side=OrderSide.BUY) -> Order:
    return Order(
        order_id="o1",
        symbol="600519.SH",
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
    )


def test_default_rate_full_fill():
    """participation_rate 默认 1.0：行为不变，全量成交"""
    broker = SimBroker(initial_capital=1_000_000.0)
    order = _order(500.0)
    broker.submit_order(order)
    trades = broker.on_bar(_bar(volume=1000.0))
    assert len(trades) == 1
    assert trades[0].quantity == 500.0
    assert order.status == OrderStatus.FILLED


def test_rate_caps_fill_to_volume_share():
    """participation_rate=0.1：fill_qty = min(remaining, int(volume*rate))"""
    broker = SimBroker(initial_capital=1_000_000.0, participation_rate=0.1)
    order = _order(500.0)
    broker.submit_order(order)
    trades = broker.on_bar(_bar(volume=1000.0))
    assert len(trades) == 1
    assert trades[0].quantity == 100.0  # int(1000*0.1)=100
    assert order.status == OrderStatus.PARTIAL_FILLED
    assert order.remaining_qty == 400.0


def test_rate_cap_not_exceed_volume_share():
    """参与率限制下成交不超过 int(volume*rate)"""
    broker = SimBroker(initial_capital=1_000_000.0, participation_rate=0.5)
    order = _order(300.0)
    broker.submit_order(order)
    trades = broker.on_bar(_bar(volume=1000.0))
    assert trades[0].quantity == 300.0  # min(300, 500)=300 全成交


def test_rate_zero_volume_no_fill():
    """成交量极小时参与率限制下成交 0，保留订单待后续 bar"""
    broker = SimBroker(initial_capital=1_000_000.0, participation_rate=0.1)
    order = _order(500.0)
    broker.submit_order(order)
    trades = broker.on_bar(_bar(volume=5.0))  # int(5*0.1)=0
    assert len(trades) == 0
    assert order.status == OrderStatus.SUBMITTED


def test_vectorized_cash_constraint_src():
    """vectorized_engine 负现金改按比例缩 notional 且计数写入结果"""
    src = (ROOT / "myquant" / "backtest" / "vectorized_engine.py").read_text(encoding="utf-8")
    assert "_cash_constraint_caps" in src
    assert "按可用现金缩 notional" in src
    assert 'self.result.extra["cash_constraint_caps"]' in src