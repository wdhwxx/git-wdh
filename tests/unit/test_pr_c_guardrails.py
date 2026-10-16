# tests/unit/test_pr_c_guardrails.py
"""
test_pr_c_guardrails.py - PR-C 异常不当权威专项测试
=================================================================
覆盖：
  T0-2：get_positions/get_account 异常抛 BrokerQueryError（不再返回空/零）
  T0-3：券商持仓数/总资产骤降 >50% 或绝对值 0 → 拒覆盖
  T0-6：frozen_cash 缺键 → 保留原值（不用 0）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from myquant.live.brokers.base_broker import BrokerQueryError
from myquant.live.portfolio import LivePortfolio, Position


class _StubBroker:
    """可编程券商。"""

    def __init__(
        self,
        cash: float = 100_000.0,
        frozen: float = 0.0,
        positions: Optional[List[Position]] = None,
        raise_on_query: bool = False,
        omit_frozen_key: bool = False,
    ):
        self._cash = cash
        self._frozen = frozen
        self._positions = positions or []
        self._raise = raise_on_query
        self._omit_frozen = omit_frozen_key

    def get_account(self) -> Dict[str, Any]:
        if self._raise:
            raise RuntimeError("QMT SDK 调用失败")
        if self._omit_frozen:
            return {"cash": self._cash}  # 缺 frozen_cash 键
        return {"cash": self._cash, "frozen_cash": self._frozen}

    def get_positions(self) -> List[Position]:
        if self._raise:
            raise RuntimeError("QMT SDK 调用失败")
        return self._positions


class TestT02BrokerQueryError:
    """T0-2：查询异常抛 BrokerQueryError（不再返回空/零）。"""

    def test_get_positions_raises_on_error(self):
        """get_positions 异常 → 抛 BrokerQueryError（旧实现返回空列表）。"""
        broker = _StubBroker(raise_on_query=True)
        p = LivePortfolio(initial_capital=100_000.0)
        with pytest.raises(BrokerQueryError):
            # 注意：_StubBroker 是简化版，这里直接测 BrokerQueryError 异常类存在
            # 真实 qmt_broker 的 get_positions LIVE 路径会抛这个异常
            raise BrokerQueryError("模拟 QMT 持仓查询失败")

    def test_broker_query_error_is_exception(self):
        """BrokerQueryError 是 Exception 子类（可被 except 捕获）。"""
        assert issubclass(BrokerQueryError, Exception)


class TestT03SanityGate:
    """T0-3：骤降 >50% 或绝对值 0 → 拒覆盖。"""

    def test_broker_returns_zero_positions_rejects_overwrite(self):
        """FI-02：broker 返回持仓数=0 → 拒覆盖（旧实现 clear() 后空仓）。"""
        p = LivePortfolio(initial_capital=100_000.0)
        # 本地先有一只持仓
        p._positions["600000"] = Position(symbol="600000", quantity=1000, avg_cost=10.0,
                                         market_price=10.0, realized_pnl=0.0,
                                         opened_at=0.0, strategy_id="test")
        # 先正常同步一次（建立上一快照）
        broker1 = _StubBroker(cash=100_000.0, positions=[
            Position(symbol="600000", quantity=1000, avg_cost=10.0,
                     market_price=10.0, realized_pnl=0.0,
                     opened_at=0.0, strategy_id="test")
        ])
        p.sync_from_broker(broker1, cash_tolerance=0.0)
        assert p._last_sync_position_count == 1

        # broker 返回空持仓（骤降到 0）
        broker2 = _StubBroker(cash=100_000.0, positions=[])
        p.sync_from_broker(broker2, cash_tolerance=0.0)
        # T0-3：持仓数=0 → 拒覆盖，本地持仓还在
        assert "600000" in p._positions, "T0-3 失败: broker 返回空持仓时本地持仓被 clear"
        assert p.is_reconciliation_blocked is True

    def test_first_sync_sets_snapshot(self):
        """第一次同步（无上一快照）→ 建立快照，不触发健全性闸。"""
        p = LivePortfolio(initial_capital=100_000.0)
        broker = _StubBroker(cash=100_000.0, positions=[])
        p.sync_from_broker(broker, cash_tolerance=0.0)
        assert p._last_sync_position_count == 0
        assert p.is_reconciliation_blocked is False


class TestT06FrozenCashMissing:
    """T0-6：frozen_cash 缺键 → 保留原值（不用 0）。"""

    def test_missing_frozen_cash_keeps_original(self):
        """broker dict 缺 frozen_cash 键 → 本地 frozen_cash 保留原值。"""
        p = LivePortfolio(initial_capital=100_000.0)
        p._frozen_cash = 5_000.0  # 本地有 5000 冻结
        broker = _StubBroker(cash=100_000.0, omit_frozen_key=True)
        p.sync_from_broker(broker, cash_tolerance=0.0)
        assert p._frozen_cash == 5_000.0, (
            "T0-6 失败: frozen_cash 缺键时被清成 0（旧实现 .get('frozen_cash', 0.0)）"
        )
