# 文件名：tests/fixtures/strategies.py
"""
strategies.py - 策略测试 Fixture
====================================
提供已注册策略实例、信号样例、策略上下文等 fixture，
供策略单元测试、回测集成测试使用。

覆盖场景:
  - MACrossStrategy 均线策略实例
  - 已注册策略（通过 StrategyRegistry）
  - 买入/卖出信号样例
  - 策略上下文 StrategyContext
  - 多策略实例列表

设计原则:
  1. 全部使用历史数据（2024-01），严格禁用未来函数
  2. 为实盘策略对接预留：策略实例可直接用于实盘引擎
  3. 可被 pytest 通过 conftest.py 的 pytest_plugins 自动加载
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from myquant.strategies.base import Signal, SignalType, StrategyContext
from myquant.strategies.ma_strategy import MACrossStrategy
from myquant.strategies.registry import register_strategy, list_strategies, create_strategy


# ═══════════════════════════════════════════
#  策略实例 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def ma_strategy() -> MACrossStrategy:
    """均线交叉策略实例（fast=5, slow=20）"""
    return MACrossStrategy(fast_period=5, slow_period=20)


@pytest.fixture
def ma_strategy_fast() -> MACrossStrategy:
    """快速均线策略实例（fast=3, slow=10）"""
    return MACrossStrategy(fast_period=3, slow_period=10)


@pytest.fixture
def registered_ma_strategy() -> str:
    """注册均线策略到全局注册器，返回策略名"""
    name = "test_ma_cross_fixture"
    register_strategy(name, MACrossStrategy, description="fixture测试均线策略")
    return name


@pytest.fixture
def multiple_strategies() -> List[MACrossStrategy]:
    """多策略实例列表（用于组合回测）"""
    return [
        MACrossStrategy(fast_period=5, slow_period=20),
        MACrossStrategy(fast_period=3, slow_period=10),
        MACrossStrategy(fast_period=10, slow_period=30),
    ]


# ═══════════════════════════════════════════
#  信号样例 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def buy_signal() -> Signal:
    """买入信号样例"""
    return Signal(
        symbol="000001.SZ",
        signal_type=SignalType.BUY,
        strength=1.0,
        price=10.5,
        strategy_id="ma_cross",
        reason="金叉买入",
    )


@pytest.fixture
def sell_signal() -> Signal:
    """卖出信号样例"""
    return Signal(
        symbol="000001.SZ",
        signal_type=SignalType.SELL,
        strength=1.0,
        price=12.0,
        strategy_id="ma_cross",
        reason="死叉卖出",
    )


@pytest.fixture
def hold_signal() -> Signal:
    """持有信号样例（无操作）"""
    return Signal(
        symbol="000001.SZ",
        signal_type=SignalType.HOLD,
        strength=0.0,
        price=11.0,
        strategy_id="ma_cross",
        reason="继续持有",
    )


@pytest.fixture
def sample_signals(buy_signal: Signal, sell_signal: Signal, hold_signal: Signal) -> List[Signal]:
    """混合信号列表（买/卖/持有）"""
    return [buy_signal, hold_signal, sell_signal]


# ═══════════════════════════════════════════
#  策略上下文 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def strategy_context() -> StrategyContext:
    """策略上下文实例（空数据+空组合）"""
    return StrategyContext(
        data={},
        portfolio=None,
        executor=None,
        config={"initial_capital": 100000.0},
    )


@pytest.fixture
def strategy_context_with_position() -> StrategyContext:
    """含配置的策略上下文（初始资金10万）"""
    return StrategyContext(
        data={},
        portfolio=None,
        executor=None,
        config={"initial_capital": 100000.0, "commission_rate": 0.0003},
    )
