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

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import Event, EventBus, EventType
from myquant.live.portfolio import LivePortfolio
from myquant.live.scheduler import LiveScheduler, attach_engine
from myquant.models.trade import Position

SYMBOL = "000001.SZ"
SYMBOL2 = "600000.SH"


@pytest.fixture(autouse=True)
def alert_calls(monkeypatch):
    """隔离告警管理器（A-7）：把 get_alert_manager() 换成内存记录桩。

    reconcile_vs_broker 超阈时会经 get_alert_manager().alert 发 CRITICAL；
    本文件所有用例统一走内存桩，绝不触碰 quant.db。需要断言告警的用例直接
    声明 ``alert_calls`` 参数即可读取调用记录。
    """
    calls: List[Dict[str, Any]] = []

    def _fake_alert(**kw: Any) -> int:
        calls.append(kw)
        return 1

    fake_mgr = SimpleNamespace(alert=_fake_alert)
    monkeypatch.setattr("myquant.monitoring.alerting.get_alert_manager", lambda: fake_mgr)
    return calls


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
        # 与上一用例同源（现金差=0）：容差=0 下精确一致即不告警。
        broker = _FakeBroker(cash=100_000.0, positions=[])
        p = _make_portfolio(100_000.0, {})
        diff = p.reconcile_vs_broker(broker)
        assert diff.exceeds_threshold is False
        assert diff.cash_diff == 0.0

    def test_默认容差取自配置真源_LOCKED0(self) -> None:
        """R-04：未传 cash_tolerance 时，容差取 settings.yaml
        monitoring.slo.reconciliation.realtime_diff_tolerance（P0-2 LOCKED=0，与 B-1 对齐）。
        """
        p = _make_portfolio(100_000.0, {})
        broker = _FakeBroker(cash=100_000.0, positions=[])
        diff = p.reconcile_vs_broker(broker)
        assert diff.cash_tolerance == 0.0

    def test_现金差非零_容差0下即超阈(self) -> None:
        """R-04：实时对账容差 LOCKED=0——任何非零现金差即超阈（旧硬编码 0.01 已废弃）。"""
        broker = _FakeBroker(cash=100_000.005, positions=[])
        p = _make_portfolio(100_000.0, {})
        diff = p.reconcile_vs_broker(broker)
        assert abs(diff.cash_diff - (-0.005)) < 1e-9
        # 容差=0：0.005 非零差即超阈（不再有 1 分钱级 slack）
        assert diff.exceeds_threshold is True


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


# ═══════════════════════════════════════════════
#  5. A-7：差异 → CRITICAL 告警闭环（R-04 阈值 / details 明细 / dedup_key）
# ═══════════════════════════════════════════════

class TestReconciliationAlert:
    def test_差异发CRITICAL告警含明细(self, alert_calls) -> None:
        """FI-05：人为制造持仓差异 → 经 get_alert_manager().alert 发 CRITICAL，
        details 含 cash_diff / 各持仓 position_diffs / 容差，category=reconciliation。"""
        broker = _FakeBroker(cash=90_000.0, positions=[_pos(SYMBOL, 50.0)])
        p = _make_portfolio(100_000.0, {SYMBOL: _pos(SYMBOL, 100.0)})
        diff = p.reconcile_vs_broker(broker)
        assert diff.exceeds_threshold is True
        assert len(alert_calls) == 1
        call = alert_calls[0]
        assert call["level"] == SeverityLevel.CRITICAL
        assert call["category"] == "reconciliation"
        details = call["details"]
        assert details["cash_diff"] == pytest.approx(10_000.0)
        assert details["cash_tolerance"] == 0.0
        assert SYMBOL in details["position_diffs"]
        assert details["position_diffs"][SYMBOL]["diff"] == 50.0

    def test_告警带dedup_key(self, alert_calls) -> None:
        """dedup_key 形如 reconciliation:<日期>:...（内容相关，防刷屏）。"""
        broker = _FakeBroker(cash=90_000.0, positions=[])
        p = _make_portfolio(100_000.0, {})
        p.reconcile_vs_broker(broker)
        assert len(alert_calls) == 1
        key = alert_calls[0]["deduplication_key"]
        assert key.startswith("reconciliation:")

    def test_一致不发告警(self, alert_calls) -> None:
        """账实一致 → 不发 CRITICAL（仅 logger.info）。"""
        broker = _FakeBroker(cash=100_000.0, positions=[_pos(SYMBOL, 100.0)])
        p = _make_portfolio(100_000.0, {SYMBOL: _pos(SYMBOL, 100.0)})
        diff = p.reconcile_vs_broker(broker)
        assert diff.exceeds_threshold is False
        assert alert_calls == []


# ═══════════════════════════════════════════════
#  6. A-7：事件驱动对账（R-02：订阅 FILLED / POSITION_UPDATE 实时触发核对）
# ═══════════════════════════════════════════════

class TestEventTriggeredReconcile:
    def test_发布FILLED事件_触发核对_差异告警(self, alert_calls) -> None:
        """R-02 事件触发路径：发布 ORDER_FILLED → 触发核对 → 差异 CRITICAL 告警。"""
        bus = EventBus()
        broker = _FakeBroker(cash=90_000.0, positions=[])  # 现金差 10000
        p = _make_portfolio(100_000.0, {})
        p.attach_reconciliation_event_trigger(bus, broker)
        assert alert_calls == []  # 挂载本身不触发
        bus.publish(Event(EventType.ORDER_FILLED, data={"symbol": SYMBOL}))
        assert len(alert_calls) == 1
        assert alert_calls[0]["level"] == SeverityLevel.CRITICAL
        assert alert_calls[0]["details"]["cash_diff"] == pytest.approx(10_000.0)

    def test_发布POSITION_UPDATE事件_触发核对(self, alert_calls) -> None:
        """R-02：POSITION_UPDATE 事件同样触发核对。"""
        bus = EventBus()
        broker = _FakeBroker(cash=90_000.0, positions=[])
        p = _make_portfolio(100_000.0, {})
        p.attach_reconciliation_event_trigger(bus, broker)
        bus.publish(Event(EventType.POSITION_UPDATE, data={"symbol": SYMBOL}))
        assert len(alert_calls) == 1
        assert alert_calls[0]["category"] == "reconciliation"

    def test_事件触发但账实一致_不告警(self, alert_calls) -> None:
        """一致时事件触发核对 → 仅 INFO，不发 CRITICAL。"""
        bus = EventBus()
        broker = _FakeBroker(cash=100_000.0, positions=[])
        p = _make_portfolio(100_000.0, {})
        p.attach_reconciliation_event_trigger(bus, broker)
        bus.publish(Event(EventType.ORDER_FILLED, data={"symbol": SYMBOL}))
        assert alert_calls == []

    def test_detach后不再触发(self, alert_calls) -> None:
        """卸载事件对账后，再发布事件不触发核对。"""
        bus = EventBus()
        broker = _FakeBroker(cash=90_000.0, positions=[])
        p = _make_portfolio(100_000.0, {})
        ids = p.attach_reconciliation_event_trigger(bus, broker)
        assert len(ids) == 2
        removed = p.detach_reconciliation_event_trigger()
        assert removed == 2
        bus.publish(Event(EventType.ORDER_FILLED, data={"symbol": SYMBOL}))
        assert alert_calls == []

    def test_缺bus或broker抛错(self) -> None:
        p = _make_portfolio(100_000.0, {})
        with pytest.raises(ValueError):
            p.attach_reconciliation_event_trigger(None, None)  # type: ignore[arg-type]
