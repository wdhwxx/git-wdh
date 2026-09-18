# -*- coding: utf-8 -*-
"""
C2 修复回归测试：实盘多线程并发修改持仓/资金无锁
==================================================
覆盖场景（对应实盘真实线程模型：行情轮询线程 + 调度任务线程 + 成交回报线程）:
  A. 多线程并发 submit_order + update_market_price 撮合 → 账目守恒
  B. 多线程并发 portfolio.apply_trade → 现金守恒
  C. 并发 sync_from_broker + update_market_price（跨锁调用无死锁）
  D. 成交回调内再调用 broker / portfolio（锁外回调无死锁）
  E. 限价单并发撮合 → 成交数量守恒

回归依据: 审计问题 C2（live/portfolio.py 与 live/brokers/*.py 无锁竞态）
"""
import threading

import pytest

from myquant.live.brokers.paper_broker import PaperBroker
from myquant.live.portfolio import LivePortfolio
from myquant.models.trade import Order, OrderSide, OrderType, Trade


@pytest.fixture()
def paper_broker() -> PaperBroker:
    b = PaperBroker(initial_capital=1_000_000.0)
    b.connect()
    return b


@pytest.fixture()
def live_portfolio() -> LivePortfolio:
    return LivePortfolio(initial_capital=1_000_000.0)


def test_并发下单撮合账目守恒(paper_broker: PaperBroker) -> None:
    """A. 8 线程 × 20 单并发市价单 + 行情撮合，资金/持仓/订单数守恒"""
    broker = paper_broker
    broker.update_market_price("000001.SZ", 10.0)  # 建立行情基准
    n_threads, orders_per_thread = 8, 20
    errors: list = []

    def worker(tid: int) -> None:
        try:
            for i in range(orders_per_thread):
                o = Order(symbol="000001.SZ", side=OrderSide.BUY,
                          order_type=OrderType.MARKET, quantity=100)
                broker.submit_order(o)
                broker.update_market_price("000001.SZ", 10.0)
        except Exception as e:  # noqa: BLE001
            errors.append(f"thread{tid}: {e}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"线程异常: {errors[:3]}"
    total = n_threads * orders_per_thread
    assert len(broker.orders) == total, f"订单数 {len(broker.orders)} != {total}"
    # 每单 100 股：成交价含买入滑点 10*(1+0.0002)=10.002 → 成交额 1000.2
    # 佣金 max(1000.2*0.0002,5)=5 元 + 过户费 1000.2*0.00001
    per_trade_cost = 1000.2 + 5.0 + 1000.2 * 0.00001
    assert abs(broker.get_account()["cash"] - (1_000_000.0 - total * per_trade_cost)) < 1.0
    assert abs(broker.get_position("000001.SZ").quantity - total * 100) < 1e-6


def test_并发apply_trade现金守恒(live_portfolio: LivePortfolio) -> None:
    """B. 8 线程 × 50 笔并发成交回报，现金/统计/持仓守恒"""
    pf = live_portfolio
    n_threads, trades_per_thread = 8, 50
    errors: list = []

    def worker(tid: int) -> None:
        try:
            for i in range(trades_per_thread):
                t = Trade(order_id=f"o_{tid}_{i}", symbol="000002.SZ",
                          side=OrderSide.BUY, quantity=100, price=10.0,
                          commission=5.01)
                pf.apply_trade(t)
        except Exception as e:  # noqa: BLE001
            errors.append(f"thread{tid}: {e}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"线程异常: {errors[:3]}"
    total = n_threads * trades_per_thread
    expected_cash = 1_000_000.0 - total * (1000.0 + 5.01)
    assert abs(pf.cash - expected_cash) < 1e-6, f"现金 {pf.cash:.4f} != {expected_cash:.4f}"
    assert pf.stats["total_trades"] == total
    assert abs(pf.get_position("000002.SZ").quantity - total * 100) < 1e-6


def test_并发跨锁调用无死锁() -> None:
    """C. portfolio.sync_from_broker（portfolio锁→broker锁）与行情线程并发，无死锁"""
    broker = PaperBroker(initial_capital=500_000.0)
    broker.connect()
    pf = LivePortfolio(initial_capital=500_000.0)
    broker.update_market_price("000003.SZ", 20.0)
    broker.submit_order(Order(symbol="000003.SZ", side=OrderSide.BUY,
                              order_type=OrderType.MARKET, quantity=1000))
    broker.update_market_price("000003.SZ", 20.5)
    stop = threading.Event()

    def sync_loop() -> None:
        while not stop.is_set():
            pf.sync_from_broker(broker)

    def price_loop() -> None:
        for i in range(200):
            broker.update_market_price("000003.SZ", 20.0 + i * 0.01)

    t1 = threading.Thread(target=sync_loop)
    t2 = threading.Thread(target=price_loop)
    t1.start()
    t2.start()
    t2.join(timeout=30)
    stop.set()
    t1.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive(), "跨锁调用死锁"
    assert pf.get_account()["total_assets"] > 0


def test_回调嵌套调用无死锁() -> None:
    """D. 成交/订单回调内再调用 broker 与 portfolio（锁外回调路径）"""
    broker = PaperBroker(initial_capital=100_000.0)
    broker.connect()
    pf = LivePortfolio(initial_capital=100_000.0)
    calls = {"trade": 0, "order": 0}

    def on_trade(trade: Trade) -> None:
        pf.apply_trade(trade)  # 回调内再进 portfolio（模拟引擎成交回报处理）
        calls["trade"] += 1

    def on_order(order: Order) -> None:
        broker.get_order(order.order_id)  # 回调内再查 broker
        calls["order"] += 1

    broker.register_trade_callback(on_trade)
    broker.register_order_callback(on_order)
    broker.update_market_price("000004.SZ", 5.0)
    for i in range(100):
        broker.submit_order(Order(symbol="000004.SZ", side=OrderSide.BUY,
                                  order_type=OrderType.MARKET, quantity=100))
        broker.update_market_price("000004.SZ", 5.0 + i * 0.01)

    assert calls["trade"] == 100 and calls["order"] >= 100
    assert abs(pf.get_position("000004.SZ").quantity - 10_000) < 1e-6


def test_限价单并发撮合守恒(paper_broker: PaperBroker) -> None:
    """E. 6 线程 × 30 单并发限价单（限价远高于行情），全部成交且账目守恒"""
    broker = paper_broker
    broker.update_market_price("000005.SZ", 30.0)
    n_threads, orders_per_thread = 6, 30
    errors: list = []

    def worker(tid: int) -> None:
        try:
            for i in range(orders_per_thread):
                broker.submit_order(Order(symbol="000005.SZ", side=OrderSide.BUY,
                                          order_type=OrderType.LIMIT,
                                          quantity=100, price=100.0))
                broker.update_market_price("000005.SZ", 30.0 + (tid + i) * 0.001)
        except Exception as e:  # noqa: BLE001
            errors.append(f"thread{tid}: {e}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"线程异常: {errors[:3]}"
    total = n_threads * orders_per_thread
    filled = sum(1 for o in broker.orders.values() if o.is_filled)
    assert filled == total, f"成交 {filled} != {total}"
    assert abs(broker.get_position("000005.SZ").quantity - total * 100) < 1e-6
    assert broker.get_account()["cash"] > 0
