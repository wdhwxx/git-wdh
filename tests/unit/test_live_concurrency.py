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
import time
from unittest import mock

import pytest

from myquant.live.brokers.paper_broker import PaperBroker
from myquant.live.engine import LiveEngine, RiskConfig
from myquant.live.portfolio import LivePortfolio
from myquant.models.market_data import Bar, BarFrequency
from myquant.models.trade import Order, OrderSide, OrderType, Trade
from myquant.risk import DecisionType, RiskManager, RiskStatus
from myquant.risk.guardian import HealthCheckResult, HealthStatus
from myquant.strategies.base import Signal, SignalType


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


def test_engine_并发多标的买信号不超最大持仓() -> None:
    """BUG-03 验收：2 个不同标的同时 BUY 信号并发提交，提交订单数不超 max_positions"""
    engine = LiveEngine(
        risk_config=RiskConfig(max_positions=1, max_position_pct=0.5),
        enable_risk_manager=False,
        enable_state_store=False,
        enable_scheduler=False,
        enable_guardian=False,
        enable_event_bus=False,
        enable_lookahead_guard=False,
    )
    pf = LivePortfolio(initial_capital=1_000_000.0)
    broker = PaperBroker(initial_capital=1_000_000.0)
    broker.connect()
    broker.update_market_price("000001.SZ", 10.0)
    broker.update_market_price("000002.SZ", 10.0)
    engine.set_portfolio(pf)
    engine.set_broker(broker)

    ts = time.time()
    bar_a = Bar(symbol="000001.SZ", frequency=BarFrequency.DAILY, timestamp=ts,
                open=10.0, high=10.0, low=10.0, close=10.0,
                volume=1_000_000, amount=10_000_000)
    bar_b = Bar(symbol="000002.SZ", frequency=BarFrequency.DAILY, timestamp=ts,
                open=10.0, high=10.0, low=10.0, close=10.0,
                volume=1_000_000, amount=10_000_000)
    sig_a = Signal(symbol="000001.SZ", signal_type=SignalType.BUY, price=10.0, reason="并发测试A")
    sig_b = Signal(symbol="000002.SZ", signal_type=SignalType.BUY, price=10.0, reason="并发测试B")

    errors: list = []

    def fire(sig: Signal, bar: Bar) -> None:
        try:
            engine._process_signal(sig, bar)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sig.symbol}: {e}")

    t1 = threading.Thread(target=fire, args=(sig_a, bar_a))
    t2 = threading.Thread(target=fire, args=(sig_b, bar_b))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"并发信号处理异常: {errors[:3]}"
    buy_orders = [o for o in engine._orders if o.side == OrderSide.BUY]
    assert len(buy_orders) <= 1, f"并发 BUY 订单 {len(buy_orders)} 超 max_positions=1"
    assert engine._stats["risk_rejected"] >= 1, "第二个并发 BUY 信号应被风控拒绝（待成交占位生效）"


def test_guardian_health_unhealthy_强平回调接线且paper不下真单() -> None:
    """BUG-04 验收：mock health=UNHEALTHY → guardian 决策 FORCE_CLOSE_ALL，
    engine 先 PAUSE 风控、触发强平回调（EmergencyLiquidationService dry_run），
    paper 通道不产生真实平仓单。"""
    risk_mgr = RiskManager()
    engine = LiveEngine(
        risk_config=RiskConfig(max_positions=5),
        risk_manager=risk_mgr,
        enable_state_store=False,
        enable_scheduler=False,
        enable_guardian=True,
        enable_event_bus=False,
        enable_lookahead_guard=False,
    )
    pf = LivePortfolio(initial_capital=1_000_000.0)
    broker = PaperBroker(initial_capital=1_000_000.0)
    broker.connect()
    engine.set_portfolio(pf)
    engine.set_broker(broker)
    # 手动装配（不 start，避免真实轮询/调度线程）
    engine._setup_force_close()
    assert engine._emergency_service is not None, "guardian 一键平仓服务应已装配"

    unhealthy = HealthCheckResult()
    unhealthy.add_check("mock_position_risk", False, "模拟风控异常（测试）")
    assert unhealthy.status == HealthStatus.UNHEALTHY

    with mock.patch.object(engine._guardian, "check_health", return_value=unhealthy), \
         mock.patch.object(engine._emergency_service, "execute",
                           return_value={"ok": True, "mode": "DRY_RUN",
                                         "liquidation": {"total_symbols": 0}}) as fake_execute:
        engine._guardian_patrol()

    # 回调被调用且为 dry_run（confirm=False → paper 不下真单）
    fake_execute.assert_called_once()
    assert fake_execute.call_args.kwargs.get("confirm") is False
    assert risk_mgr.get_status() == RiskStatus.PAUSED, "强平前置应先 PAUSE 新开仓"
    sell_orders = [o for o in broker.orders.values() if o.side == OrderSide.SELL]
    assert not sell_orders, "dry_run 模式下 paper 不应产生真实平仓单"
    decisions = engine._guardian.get_decisions()
    assert decisions and decisions[0].decision_type == DecisionType.FORCE_CLOSE_ALL
