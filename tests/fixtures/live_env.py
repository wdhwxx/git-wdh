# 文件名：tests/fixtures/live_env.py
"""
live_env.py - 实盘环境测试 Fixture
======================================
提供模拟券商、事件总线、实时持仓、行情订阅等实盘环境 fixture，
供实盘引擎、调度器、状态存储、风控等模块的集成测试使用。

覆盖场景:
  - SimBroker 模拟券商实例（已连接）
  - EventBus 事件总线实例
  - LivePortfolio 实时持仓实例
  - MarketDataFeed 行情订阅实例
  - 完整实盘环境（broker + portfolio + event_bus）
  - 含初始持仓的 portfolio

设计原则:
  1. 全部使用内存模拟，无网络IO，无真实券商连接
  2. 为实盘对接预留：fixture 接口与真实券商一致，替换即可上实盘
  3. 严格禁用未来函数，全部使用历史数据
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from myquant.core.event_bus import EventBus
from myquant.execution.sim_broker import SimBroker
from myquant.live.data_feed import MarketDataFeed
from myquant.live.portfolio import LivePortfolio
from myquant.models.trade import Order, OrderSide, OrderType, Trade

# ═══════════════════════════════════════════
#  模拟券商 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def mock_broker() -> SimBroker:
    """模拟券商实例（已连接，初始资金100万）"""
    broker = SimBroker(initial_capital=1000000.0)
    broker.connect()
    yield broker
    broker.disconnect()


@pytest.fixture
def mock_broker_small() -> SimBroker:
    """模拟券商实例（小资金10万，用于小额测试）"""
    broker = SimBroker(initial_capital=100000.0)
    broker.connect()
    yield broker
    broker.disconnect()


@pytest.fixture
def broker_with_position(mock_broker: SimBroker) -> SimBroker:
    """含持仓的模拟券商（已买入000001.SZ 1000股）"""
    order = Order(
        symbol="000001.SZ",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1000,
        price=10.0,
    )
    mock_broker.submit_order(order)
    return mock_broker


# ═══════════════════════════════════════════
#  事件总线 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def mock_event_bus() -> EventBus:
    """事件总线实例（关闭去重窗口，便于测试）"""
    return EventBus(max_history=1000, dedup_window_seconds=0)


@pytest.fixture
def event_bus_with_subscribers(mock_event_bus: EventBus) -> EventBus:
    """带测试订阅者的事件总线（记录所有收到的事件）"""
    received: List[Any] = []

    def _handler(event):
        received.append(event)

    mock_event_bus.subscribe(_handler)
    mock_event_bus._test_received = received  # type: ignore[attr-defined]
    return mock_event_bus


# ═══════════════════════════════════════════
#  实时持仓 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def live_portfolio() -> LivePortfolio:
    """实时持仓实例（初始资金100万）"""
    return LivePortfolio(initial_capital=1000000.0, account_id="test_account")


@pytest.fixture
def live_portfolio_small() -> LivePortfolio:
    """实时持仓实例（小资金10万）"""
    return LivePortfolio(initial_capital=100000.0, account_id="test_small")


@pytest.fixture
def portfolio_with_position(live_portfolio: LivePortfolio) -> LivePortfolio:
    """含初始持仓的实时持仓（000001.SZ 1000股，成本10元）"""
    trade = Trade(
        symbol="000001.SZ",
        side=OrderSide.BUY,
        quantity=1000,
        price=10.0,
    )
    live_portfolio.apply_trade(trade)
    live_portfolio.update_market_price("000001.SZ", 10.5)
    return live_portfolio


# ═══════════════════════════════════════════
#  行情订阅 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def market_data_feed() -> MarketDataFeed:
    """行情订阅实例（空订阅，用于测试订阅/退订）"""
    return MarketDataFeed()


# ═══════════════════════════════════════════
#  完整实盘环境 Fixture
# ═══════════════════════════════════════════

@pytest.fixture
def live_environment(mock_broker: SimBroker, live_portfolio: LivePortfolio,
                      mock_event_bus: EventBus) -> Dict[str, Any]:
    """
    完整实盘环境（broker + portfolio + event_bus）
    返回字典，测试中可直接解包使用。
    """
    return {
        "broker": mock_broker,
        "portfolio": live_portfolio,
        "event_bus": mock_event_bus,
    }
