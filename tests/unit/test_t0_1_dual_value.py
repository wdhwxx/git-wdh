# tests/unit/test_t0_1_dual_value.py
"""
test_t0_1_dual_value.py - T0-1 双值并存防镜像覆写专项测试
=================================================================
验收 FI-01/02 + FI-11 衍生：
  1. broker 返回空/0 → 本地 book_cash/_positions 不变 + CRITICAL（不被覆写）
  2. diff 超阈 → 置 reconciliation_blocked，本地账保持不变（不再被镜像抹平）
  3. diff 在容差内 → 正常同步（旧行为保留）
  4. adopt_broker_ledger(reason, actor) 显式收敛 + 解除 blocked
  5. broker 返回 0 现金 → 不覆写（旧代码会把本地账清成 0）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from myquant.live.portfolio import LivePortfolio, Position


class _StubBroker:
    """可编程券商：get_account/get_positions 返回可编程值。"""

    def __init__(self, cash: float, frozen: float = 0.0, positions: Optional[List[Position]] = None):
        self._cash = cash
        self._frozen = frozen
        self._positions = positions or []

    def get_account(self) -> Dict[str, Any]:
        return {"cash": self._cash, "frozen_cash": self._frozen}

    def get_positions(self) -> List[Position]:
        return self._positions


class TestDualValueNoOverwrite:
    """T0-1 核心：diff 超阈时本地账不被覆写。"""

    def test_diff_exceeds_does_not_overwrite_book_cash(self):
        """本地 10 万，券商 9 万（差 1 万超容差）→ book_cash 保持 10 万。"""
        p = LivePortfolio(initial_capital=100_000.0)
        broker = _StubBroker(cash=90_000.0)  # 差 1 万

        p.sync_from_broker(broker, cash_tolerance=0.0)
        assert p.book_cash == 100_000.0, "T0-1 失败: diff 超阈时 book_cash 被覆写"
        assert p.broker_cash == 90_000.0, "broker_cash 应刷新为券商快照值"
        assert p.is_reconciliation_blocked is True

    def test_broker_returns_zero_does_not_zero_out_book(self):
        """FI-02：broker 返回 cash=0 → 本地 book_cash 不清零（旧代码会清零）。"""
        p = LivePortfolio(initial_capital=100_000.0)
        broker = _StubBroker(cash=0.0)  # broker 返回空/0

        p.sync_from_broker(broker, cash_tolerance=0.0)
        assert p.book_cash == 100_000.0, "T0-1 失败: broker 返回0时 book_cash 被清零"
        assert p.is_reconciliation_blocked is True

    def test_diff_within_tolerance_auto_syncs(self):
        """diff 在容差内 → 正常同步（旧行为保留）。"""
        p = LivePortfolio(initial_capital=100_000.0)
        broker = _StubBroker(cash=100_000.0)  # 完全一致

        p.sync_from_broker(broker, cash_tolerance=0.0)
        assert p.book_cash == 100_000.0
        assert p.is_reconciliation_blocked is False

    def test_adopt_broker_ledger_converges(self):
        """adopt_broker_ledger(reason, actor) 显式收敛资金 + 解除 blocked。"""
        p = LivePortfolio(initial_capital=100_000.0)
        broker = _StubBroker(cash=90_000.0)  # 差 1 万

        p.sync_from_broker(broker, cash_tolerance=0.0)
        assert p.is_reconciliation_blocked is True
        assert p.book_cash == 100_000.0

        # 人工核查后显式 adopt
        p.adopt_broker_ledger(reason="人工核查确认券商账正确", actor="admin")
        assert p.book_cash == 90_000.0, "adopt 后 book_cash 应=broker_cash"
        assert p.is_reconciliation_blocked is False

    def test_adopt_requires_reason_and_actor(self):
        """adopt_broker_ledger 缺 reason/actor → ValueError（审计必填）。"""
        p = LivePortfolio(initial_capital=100_000.0)
        with pytest.raises(ValueError):
            p.adopt_broker_ledger(reason="", actor="admin")
        with pytest.raises(ValueError):
            p.adopt_broker_ledger(reason="test", actor="")

    def test_diff_exceeds_does_not_overwrite_positions(self):
        """持仓 diff 超阈 → 本地 _positions 不被 clear 覆写。"""
        p = LivePortfolio(initial_capital=100_000.0)
        # 本地先有一只持仓
        p._positions["600000"] = Position(symbol="600000", quantity=1000, avg_cost=10.0,
                                         market_price=10.0, realized_pnl=0.0,
                                         opened_at=0.0, strategy_id="test")
        # broker 返回空持仓（差 1000 股）
        broker = _StubBroker(cash=100_000.0, positions=[])

        p.sync_from_broker(broker, cash_tolerance=0.0)
        assert "600000" in p._positions, "T0-1 失败: 持仓 diff 超阈时本地持仓被 clear"
        assert p.is_reconciliation_blocked is True
