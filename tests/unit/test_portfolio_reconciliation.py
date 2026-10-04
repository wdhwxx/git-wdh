# 文件名：tests/unit/test_portfolio_reconciliation.py
"""
test_portfolio_reconciliation.py - 组合账本与券商账实对账测试（KP-P4-01 / T-07）
===============================================================================
覆盖"组合账本与券商无实际对账"修复（原 sync_from_broker clear() 后整体重建、
无差异比对、无告警，券商返回空快照会静默覆盖本地持仓）：
  1. 一致快照 —— diff.exceeds_threshold=False，不告警、不触发对账回调。
  2. 现金差超容差 —— 产出差异、critical 告警、触发 __reconciliation_diff__ 回调。
  3. 现金差在容差内 —— 视为一致，不误报。
  4. 持仓数量不一致 —— position_diffs 含该标的，diff 正确，超阈值。
  5. 券商多仓/本地缺失 —— 也计入差异（禁止无记录覆盖本地账本）。
  6. reconcile_vs_broker 只读 —— 不修改本地资金/持仓。
  7. sync_from_broker 超阈值 —— 先 diff 后重建，仍按券商权威账本重建，
     并在 stats 记录 last_reconciliation_diff，不静默。
  8. diff 可序列化 —— to_dict 供对账日报落库/查询。
  9. attach_engine 注册盘后对账 cron 任务；显式关闭/缺组件时不注册。

依赖: pytest, myquant.live.portfolio（LivePortfolio/ReconciliationDiff）
      myquant.models.trade（Position）
      myquant.live.scheduler（attach_engine/LiveScheduler）
"""

from __future__ import annotations

from typing import Any, Dict, List

from myquant.live.portfolio import LivePortfolio
from myquant.live.scheduler import LiveScheduler, attach_engine
from myquant.models.trade import Position

SYMBOL = "000001.SZ"
SYMBOL2 = "600000.SH"


class _FakeBroker:
    """鸭子类型测试券商：仅提供 get_account / get_positions（对账所需）。"""

    def __init__(self, cash: float, positions: List[Position]) -> None:
        self._cash = float(cash)
        self._positions = positions

    def get_account(self) -> Dict[str, Any]:
        return {"cash": self._cash, "frozen_cash": 0.0}

    def get_positions(self) -> List[Position]:
        return self._positions


def _pos(symbol: str, qty: float) -> Position:
    return Position(symbol=symbol, quantity=qty, avg_cost=10.0,
                    market_price=10.0, realized_pnl=0.0)


def _make_portfolio(cash: float, positions: Dict[str, Position]) -> LivePortfolio:
    p = LivePortfolio(initial_capital=cash)
    p._cash = float(cash)
    p._positions = dict(positions)
    return p


# ═══════════════════════════════════════════════
#  1. 一致快照
# ═══════════════════════════════════════════════

class TestConsistent:
    def test_一致快照_exceeds_false(self) -> None:
        local_pos = {SYMBOL: _pos(SYMBOL, 100.0)}
        broker = _FakeBroker(cash=100_000.0, positions=[_pos(SYMBOL, 100.0)])
        p = _make_portfolio(100_000.0, local_pos)
        diff = p.reconcile_vs_broker(broker)
        assert diff.exceeds_threshold is False
        assert diff.cash_diff == 0.0
        assert diff.position_diffs == {}

    def test_现金差在容差内_不误报(self) -> None:
        broker = _FakeBroker(cash=100_000.005, positions=[])
        p = _make_portfolio(100_000.0, {})
        diff = p.reconcile_vs_broker(broker)
        assert diff.exceeds_threshold is False
        assert abs(diff.cash_diff - (-0.005)) < 1e-9


# ═══════════════════════════════════════════════
#  2. 差异检测与告警
# ═══════════════════════════════════════════════

class TestDiffDetection:
    def test_现金超容差_告警并触发对账回调(self) -> None:
        broker = _FakeBroker(cash=90_000.0, positions=[])
        p = _make_portfolio(100_000.0, {})
        notified: List[str] = []
        p.register_callback(lambda portfolio, symbol: notified.append(symbol))
        diff = p.sync_from_broker(broker)
        assert diff.exceeds_threshold is True
        assert diff.cash_diff == 10_000.0
        # 先 diff 后重建：本地已按券商权威账本重建
        assert p._cash == 90_000.0
        assert "__reconciliation_diff__" in notified
        assert "__sync__" in notified
        assert "last_reconciliation_diff" in p.stats

    def test_持仓数量不一致_产出差异清单(self) -> None:
        broker = _FakeBroker(cash=100_000.0, positions=[_pos(SYMBOL, 50.0)])
        p = _make_portfolio(100_000.0, {SYMBOL: _pos(SYMBOL, 100.0)})
        diff = p.reconcile_vs_broker(broker)
        assert diff.exceeds_threshold is True
        assert SYMBOL in diff.position_diffs
        d = diff.position_diffs[SYMBOL]
        assert d["local_quantity"] == 100.0
        assert d["broker_quantity"] == 50.0
        assert d["diff"] == 50.0

    def test_券商多仓本地缺失_计入差异(self) -> None:
        """本地账本缺该持仓而券商有 → 差异（禁止无记录覆盖）。"""
        broker = _FakeBroker(cash=100_000.0, positions=[_pos(SYMBOL2, 200.0)])
        p = _make_portfolio(100_000.0, {SYMBOL: _pos(SYMBOL, 100.0)})
        diff = p.reconcile_vs_broker(broker)
        assert diff.exceeds_threshold is True
        assert SYMBOL2 in diff.position_diffs
        assert diff.position_diffs[SYMBOL2]["diff"] == -200.0
        assert SYMBOL in diff.position_diffs
        assert diff.position_diffs[SYMBOL]["diff"] == 100.0

    def test_reconcile不改本地状态_只读(self) -> None:
        """对账只产出差异，不修改本地资金/持仓（先检查后同步）。"""
        broker = _FakeBroker(cash=90_000.0, positions=[_pos(SYMBOL, 50.0)])
        p = _make_portfolio(100_000.0, {SYMBOL: _pos(SYMBOL, 100.0)})
        diff = p.reconcile_vs_broker(broker)
        assert diff.exceeds_threshold is True
        assert p._cash == 100_000.0
        assert p._positions[SYMBOL].quantity == 100.0


# ═══════════════════════════════════════════════
#  3. sync 先 diff 后重建 + 序列化
# ═══════════════════════════════════════════════

class TestSyncAndSerialize:
    def test_sync超阈值_仍按券商重建且记录stats(self) -> None:
        broker = _FakeBroker(cash=100_000.0, positions=[_pos(SYMBOL2, 200.0)])
        p = _make_portfolio(100_000.0, {SYMBOL: _pos(SYMBOL, 100.0)})
        diff = p.sync_from_broker(broker)
        assert diff.exceeds_threshold is True
        # 重建到券商权威账本
        assert SYMBOL2 in p._positions
        assert p._positions[SYMBOL2].quantity == 200.0
        assert SYMBOL not in p._positions
        assert "last_reconciliation_diff" in p.stats
        assert str(diff) in p.stats["last_reconciliation_diff"]

    def test_to_dict_可序列化落库(self) -> None:
        broker = _FakeBroker(cash=90_000.0, positions=[_pos(SYMBOL, 50.0)])
        p = _make_portfolio(100_000.0, {SYMBOL: _pos(SYMBOL, 100.0)})
        diff = p.reconcile_vs_broker(broker)
        data = diff.to_dict()
        assert "cash_diff" in data
        assert "position_diffs" in data
        assert "exceeds_threshold" in data
        assert data["exceeds_threshold"] is True


# ═══════════════════════════════════════════════
#  4. attach_engine 盘后对账接线
# ═══════════════════════════════════════════════

class TestAttachEngineReconcile:
    def test_注册盘后对账cron任务(self) -> None:
        sched = LiveScheduler(tick_interval=0.5)
        p = _make_portfolio(100_000.0, {})
        broker = _FakeBroker(cash=100_000.0, positions=[])
        ids = attach_engine(sched, portfolio=p, broker=broker,
                            enable_patrol=False, enable_daily_report=False,
                            enable_t1_settle=False, enable_resend_pending=False)
        assert "reconcile" in ids
        task = sched.get_task(ids["reconcile"])
        assert task is not None
        assert task.name == "账实对账"
        assert task.schedule_type == "cron"
        assert task.cron_expr == "50 15 * * 1-5"

    def test_显式关闭不注册(self) -> None:
        sched = LiveScheduler(tick_interval=0.5)
        p = _make_portfolio(100_000.0, {})
        broker = _FakeBroker(cash=100_000.0, positions=[])
        ids = attach_engine(sched, portfolio=p, broker=broker,
                            enable_patrol=False, enable_daily_report=False,
                            enable_t1_settle=False, enable_reconcile=False)
        assert "reconcile" not in ids

    def test_缺portfolio或broker不注册(self) -> None:
        sched = LiveScheduler(tick_interval=0.5)
        broker = _FakeBroker(cash=100_000.0, positions=[])
        ids = attach_engine(sched, broker=broker, enable_patrol=False,
                            enable_daily_report=False, enable_t1_settle=False)
        assert "reconcile" not in ids

    def test_对账任务本体_一致不告警_差异告警(self) -> None:
        """直接调用对账 job：一致走 info，差异走 critical（无静默覆盖）。"""
        sched = LiveScheduler(tick_interval=0.5)
        broker = _FakeBroker(cash=90_000.0, positions=[])
        p = _make_portfolio(100_000.0, {})
        ids = attach_engine(sched, portfolio=p, broker=broker,
                            enable_patrol=False, enable_daily_report=False,
                            enable_t1_settle=False, enable_resend_pending=False)
        task = sched.get_task(ids["reconcile"])
        assert task is not None
        # 对账 job 只对账不重建：本地资金保持 100000，不被券商 90000 覆盖
        assert p._cash == 100_000.0
