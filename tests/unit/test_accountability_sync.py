# 文件名：tests/unit/test_accountability_sync.py
"""
test_accountability_sync.py - 券商权威账本对账测试（KP-P3-08 / T-15）
====================================================================
覆盖"风控资金与持仓基准非券商权威账本"修复：
  1. LivePortfolio.diff_vs_broker —— 只 diff 不重建（DoD）。
  2. engine._broker_accountability_patrol —— sync_from_broker + 超阈 CRITICAL + pause。

依赖: pytest, myquant.live.portfolio, myquant.live.engine
"""

from __future__ import annotations

import logging

import pytest

from myquant.live.brokers.paper_broker import PaperBroker
from myquant.live.engine import create_live_engine
from myquant.live.portfolio import LivePortfolio


def _make_engine():
    config = {
        "live": {"trade_mode": "paper", "name": "t15", "initial_capital": 100000},
        "broker": {"type": "paper"},
        "execution": {}, "market_data": {}, "strategy": {},
        "position": {}, "risk_control": {}, "state_store": {"auto_recover_on_start": False},  # KP-P5-08 显式必填对齐
        "scheduler": {}, "lookahead_guard": {},
    }
    return create_live_engine(config)


class TestDiffVsBroker:
    def test_diff_vs_broker_只diff不重建(self) -> None:
        """DoD：账实不一致 → exceeds_threshold=True；diff_vs_broker 不改本地状态。"""
        broker = PaperBroker(initial_capital=100_000.0)
        portfolio = LivePortfolio(initial_capital=100_000.0)
        # 制造差异：券商现金被改小
        broker.cash = 50_000.0
        diff = portfolio.diff_vs_broker(broker)
        assert diff.exceeds_threshold is True
        assert diff.cash_diff == pytest.approx(50_000.0)
        # diff_vs_broker 不重建：本地现金保持
        assert portfolio._cash == pytest.approx(100_000.0)

    def test_diff_vs_broker_一致通过(self) -> None:
        broker = PaperBroker(initial_capital=100_000.0)
        portfolio = LivePortfolio(initial_capital=100_000.0)
        diff = portfolio.diff_vs_broker(broker)
        assert diff.exceeds_threshold is False


class TestAccountabilityPatrol:
    def test_patrol_账实差异超阈_pause_and_critical(self, caplog) -> None:
        """DoD：券商现金被篡改 → sync_from_broker 产生超阈差异 → CRITICAL + pause。"""
        engine = _make_engine()
        broker = engine._broker_manager.primary
        # 制造权威账本差异（券商现金被改小）
        broker.cash = 0.0
        paused: list = []
        engine.pause = lambda: paused.append(True)  # type: ignore[method-assign]
        with caplog.at_level(logging.CRITICAL, logger="live.engine"):
            engine._broker_accountability_patrol()
        assert paused == [True]
        assert any("券商账本差异超阈值" in r.message for r in caplog.records)

    def test_patrol_无差异不pause(self) -> None:
        """DoD：账实一致 → 不暂停。"""
        engine = _make_engine()
        paused: list = []
        engine.pause = lambda: paused.append(True)  # type: ignore[method-assign]
        engine._broker_accountability_patrol()
        assert paused == []
