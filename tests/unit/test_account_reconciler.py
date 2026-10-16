# 文件名：tests/unit/test_account_reconciler.py
"""
test_account_reconciler.py - B-1 账户实时对账 + T+1 终局对账测试（EVENT 事件驱动）
=================================================================================
依据《监控模块施工总清单》B-1 行验收口径，覆盖：
  1. EVENT 事件路径——publish ORDER_FILLED/POSITION_UPDATE → 经 portfolio 回调
     投递异步核对（flush 后断言核对被执行、状态可查），不等周期巡检。
  2. 制造差异（broker cash 差 1 分）→ 冻结 + CRITICAL 告警 + 审计留痕。
  3. T+1 终局差异 → 冻结 + 人工介入（mark_resolved 解冻 + 审计）。
  4. 容差引用断言——构造不同 slo.reconciliation 配置，行为跟随（证明取 R-04 真源）。
  5. 异步非阻塞——broker 注入 sleep 桩，断言事件 handler 立即返回。
  6. 同 account 在途任务去重——在途期间新事件合并为补一次。
  7. PnL 防脏门——冻结时 record_pnl_snapshot 被拒并留痕；解冻后放行。
  8. 防未来——last_check_ts 取核对时刻（当前时点），不引用未来时间。

依赖: pytest, myquant.live.portfolio（LivePortfolio）,
      myquant.monitoring.account_reconciler（AccountReconciler）,
      myquant.core.event_bus（EventBus/Event/EventType）
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import Event, EventBus, EventType
from myquant.live.portfolio import LivePortfolio
from myquant.monitoring.account_reconciler import AccountReconciler

# ═══════════════════════════════════════════════
#  测试桩
# ═══════════════════════════════════════════════

class _FakeBroker:
    """鸭子类型券商：仅提供对账所需 get_account / get_positions；可注入 sleep。"""

    def __init__(self, cash: float, positions: Optional[List[Any]] = None,
                 sleep: float = 0.0) -> None:
        self._cash = float(cash)
        self._positions = positions or []
        self._sleep = float(sleep)
        self.calls: int = 0

    def get_account(self) -> Dict[str, Any]:
        self.calls += 1
        if self._sleep > 0:
            time.sleep(self._sleep)  # 模拟慢券商/慢查询，验证异步非阻塞
        return {"cash": self._cash, "frozen_cash": 0.0}

    def get_positions(self) -> List[Any]:
        return self._positions


def _make_portfolio(cash: float) -> LivePortfolio:
    """构造一个与给定现金对齐的实时账本（本地现金=现金）。"""
    p = LivePortfolio(initial_capital=cash)
    p._cash = float(cash)
    return p


@pytest.fixture
def hooks() -> SimpleNamespace:
    """内存告警 / 审计桩：记录所有调用，绝不触碰 quant.db。"""
    alerts: List[Dict[str, Any]] = []
    audits: List[Dict[str, Any]] = []

    class _Mgr:
        def alert(self, **kw: Any) -> int:
            alerts.append(kw)
            return 1

    class _Audit:
        def append(self, kind: str, action: str, actor: str = "system",
                   detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            audits.append({"kind": kind, "action": action,
                           "actor": actor, "detail": detail})
            return {"seq": len(audits)}

    return SimpleNamespace(alerts=alerts, audits=audits, mgr=_Mgr(), audit=_Audit())


@pytest.fixture(autouse=True)
def _isolate_global_alert_manager(monkeypatch: Any, hooks: SimpleNamespace) -> None:
    """把 A-7 reconcile_vs_broker 内部的 get_alert_manager() 也指向内存桩，避免真实落库。"""
    monkeypatch.setattr("myquant.monitoring.alerting.get_alert_manager", lambda: hooks.mgr)


def _new_rec(hooks: SimpleNamespace, account_id: str = "acc1") -> AccountReconciler:
    return AccountReconciler(account_id=account_id, alert_manager=hooks.mgr,
                             audit_store=hooks.audit)


# ═══════════════════════════════════════════════
#  1. EVENT 事件路径（publish → 异步核对被调度 → 状态可查）
# ═══════════════════════════════════════════════

class TestEventPath:
    def test_publish_filled_triggers_async_reconcile(self, hooks: SimpleNamespace) -> None:
        """R-02 EVENT：发布 ORDER_FILLED → 经 portfolio 回调投递异步核对；
        flush 后断言核对确实执行（broker 被调用）且一致状态可查。"""
        bus = EventBus()
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.0)  # 账实一致
        p.attach_reconciliation_event_trigger(bus, broker)
        rec = _new_rec(hooks)
        p.set_account_reconciler(rec)
        try:
            bus.publish(Event(EventType.ORDER_FILLED, data={"symbol": "000001.SZ"}))
            # handler 已返回；等待异步核对落定
            assert rec.flush(timeout=3.0) is True
            assert broker.calls >= 1          # 核对确实被执行（非周期巡检）
            assert rec.is_consistent is True
            assert rec.is_frozen is False
            assert rec.to_dict()["last_check_ts"] > 0
        finally:
            rec.shutdown()

    def test_position_update_event_also_triggers(self, hooks: SimpleNamespace) -> None:
        bus = EventBus()
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.0)
        p.attach_reconciliation_event_trigger(bus, broker)
        rec = _new_rec(hooks)
        p.set_account_reconciler(rec)
        try:
            bus.publish(Event(EventType.POSITION_UPDATE, data={"symbol": "000001.SZ"}))
            assert rec.flush(timeout=3.0) is True
            assert broker.calls >= 1
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  2. 制造差异 → 冻结 + CRITICAL + 审计
# ═══════════════════════════════════════════════

class TestDiffFreeze:
    def test_cash_diff_1cent_triggers_freeze_alert_audit(self, hooks: SimpleNamespace) -> None:
        """容差=0：broker 现金差 1 分（0.01）即超阈 → 冻结 + CRITICAL + 审计 freeze。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(99_999.99)  # 本地 100000 - 券商 99999.99 = 0.01
        rec = _new_rec(hooks)
        try:
            rec.request_realtime_reconciliation(p, broker)
            assert rec.flush(timeout=3.0) is True
            assert rec.is_frozen is True
            assert rec.is_consistent is False
            crit = [a for a in hooks.alerts if a["level"] == SeverityLevel.CRITICAL]
            assert len(crit) >= 1
            assert crit[-1]["category"] == "reconciliation"
            assert any(a["action"] == "freeze" and a["kind"] == "reconciliation"
                       for a in hooks.audits)
        finally:
            rec.shutdown()

    def test_position_qty_diff_freezes(self, hooks: SimpleNamespace) -> None:
        """任一持仓数量差（容差=0）即超阈冻结。"""
        from myquant.models.trade import Position
        p = _make_portfolio(100_000.0)
        p._positions = {"600000.SH": Position(symbol="600000.SH", quantity=100.0,
                                              avg_cost=10.0, market_price=10.0)}
        broker = _FakeBroker(100_000.0, positions=[])  # 券商无此持仓
        rec = _new_rec(hooks)
        try:
            rec.request_realtime_reconciliation(p, broker)
            rec.flush(timeout=3.0)
            assert rec.is_frozen is True
            assert any(a["action"] == "freeze" for a in hooks.audits)
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  3. T+1 终局对账 → 冻结 + 人工介入
# ═══════════════════════════════════════════════

class TestT1Final:
    def test_t1_diff_freezes_then_manual_resolve(self, hooks: SimpleNamespace) -> None:
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(90_000.0)
        rec = _new_rec(hooks)
        try:
            diff = rec.run_t1_final_reconciliation(p, broker)
            assert diff.exceeds_threshold is True
            assert rec.is_frozen is True
            assert any(a["action"] == "t1_freeze" for a in hooks.audits)
            # 人工介入解冻
            rec.mark_resolved(actor="ops", note="已与券商核对调账")
            assert rec.is_frozen is False
            unfreezes = [a for a in hooks.audits
                         if a["action"] == "unfreeze" and a["actor"] == "ops"]
            assert len(unfreezes) == 1
            assert unfreezes[0]["detail"]["was_frozen"] is True
        finally:
            rec.shutdown()

    def test_mark_resolved_empty_actor_rejected(self, hooks: SimpleNamespace) -> None:
        rec = _new_rec(hooks)
        try:
            with pytest.raises(ValueError):
                rec.mark_resolved(actor="  ")
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  4. 容差引用断言（R-04 真源）
# ═══════════════════════════════════════════════

class TestToleranceSourcing:
    def test_behavior_follows_config_tolerance(self, monkeypatch: Any,
                                               hooks: SimpleNamespace) -> None:
        """构造不同 slo.reconciliation 配置，行为跟随：容差=100 时差 50 不冻结；
        同差异容差=0 时冻结——证明容差取 R-04 真源，非代码内自定义。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_050.0)  # 现金差 = -50

        # 容差=100：差 50 在容差内 → 一致不冻结
        monkeypatch.setattr(
            "myquant.core.settings.get_monitoring_config",
            lambda: {"slo": {"reconciliation": {
                "realtime_diff_tolerance": 100.0, "t1_diff_tolerance": 100.0}}},
        )
        rec1 = _new_rec(hooks, account_id="a1")
        try:
            rec1.request_realtime_reconciliation(p, broker)
            rec1.flush(timeout=3.0)
            assert rec1.is_consistent is True
            assert rec1.is_frozen is False
        finally:
            rec1.shutdown()

        # 同一差异，容差=0：→ 超阈冻结
        monkeypatch.setattr(
            "myquant.core.settings.get_monitoring_config",
            lambda: {"slo": {"reconciliation": {
                "realtime_diff_tolerance": 0.0, "t1_diff_tolerance": 0.0}}},
        )
        rec2 = _new_rec(hooks, account_id="a2")
        try:
            rec2.request_realtime_reconciliation(p, broker)
            rec2.flush(timeout=3.0)
            assert rec2.is_frozen is True
        finally:
            rec2.shutdown()

    def test_pending_cal_none_falls_back_zero(self, monkeypatch: Any,
                                              hooks: SimpleNamespace) -> None:
        """PENDING_CAL（yaml ~ / None）不编造数值：回退 LOCKED=0，差 0.005 即超阈。"""
        monkeypatch.setattr(
            "myquant.core.settings.get_monitoring_config",
            lambda: {"slo": {"reconciliation": {
                "realtime_diff_tolerance": None, "t1_diff_tolerance": None}}},
        )
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.005)
        rec = _new_rec(hooks)
        try:
            rec.request_realtime_reconciliation(p, broker)
            rec.flush(timeout=3.0)
            assert rec.is_frozen is True
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  5. 异步非阻塞
# ═══════════════════════════════════════════════

class TestAsyncNonBlocking:
    def test_handler_returns_immediately_with_slow_broker(self, hooks: SimpleNamespace) -> None:
        """broker get_account 睡 0.3s：事件 handler 应立即返回（远小于 0.3s），
        核对在后台线程执行；flush 后确认核对确实跑过。"""
        bus = EventBus()
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.0, sleep=0.3)
        p.attach_reconciliation_event_trigger(bus, broker)
        rec = _new_rec(hooks)
        p.set_account_reconciler(rec)
        try:
            start = time.time()
            bus.publish(Event(EventType.ORDER_FILLED, data={"symbol": "X"}))
            elapsed = time.time() - start
            assert elapsed < 0.2, f"事件 handler 阻塞 {elapsed:.3f}s"
            assert rec.flush(timeout=3.0) is True
            assert broker.calls >= 1
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  6. 在途去重
# ═══════════════════════════════════════════════

class TestInFlightDedup:
    def test_concurrent_events_merged_to_one_catchup(self, hooks: SimpleNamespace) -> None:
        """连续投递 3 次：第 1 次在途，后 2 次合并为补一次 → broker 被调 2 次（非 3 次）。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.0, sleep=0.08)
        rec = _new_rec(hooks)
        try:
            submitted_first = rec.request_realtime_reconciliation(p, broker)
            second = rec.request_realtime_reconciliation(p, broker)
            third = rec.request_realtime_reconciliation(p, broker)
            assert submitted_first is True
            assert second is False   # 在途 → 去重
            assert third is False
            assert rec.flush(timeout=3.0) is True
            assert broker.calls == 2, f"在途+补一次应为 2 次，实际 {broker.calls}"
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  7. PnL 防脏门
# ═══════════════════════════════════════════════

class TestPnlDirtyGate:
    def test_pnl_blocked_when_frozen_then_allowed_after_resolve(self,
                                                                hooks: SimpleNamespace) -> None:
        """冻结时 record_pnl_snapshot 返回 None 并审计 pnl_block；解冻后放行返回快照。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(90_000.0)
        rec = _new_rec(hooks)
        p.set_account_reconciler(rec)
        try:
            rec.request_realtime_reconciliation(p, broker)
            rec.flush(timeout=3.0)
            assert rec.is_frozen is True
            # 冻结 → 拒绝
            snap = p.record_pnl_snapshot("periodic")
            assert snap is None
            assert any(a["action"] == "pnl_block" and a["actor"] == "portfolio"
                       for a in hooks.audits)
            # 解冻 → 放行
            rec.mark_resolved(actor="ops")
            snap2 = p.record_pnl_snapshot("settle")
            assert snap2 is not None
            assert snap2["reason"] == "settle"
            assert "total_pnl" in snap2
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  8. 防未来
# ═══════════════════════════════════════════════

class TestNoLookahead:
    def test_last_check_ts_is_current_time(self, hooks: SimpleNamespace) -> None:
        """last_check_ts 取核对时刻（当前时点），不引用未来时间。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.0)
        rec = _new_rec(hooks)
        try:
            before = time.time()
            rec.request_realtime_reconciliation(p, broker)
            rec.flush(timeout=3.0)
            after = time.time()
            ts = rec.to_dict()["last_check_ts"]
            assert before - 1.0 <= ts <= after + 1.0
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  T0-13 / FI-20：executor.submit 异常 → _in_flight 必清理（防在途标记泄漏）
# ═══════════════════════════════════════════════

class TestInFlightLeak:
    """T0-13：submit 抛异常时 in_flight 必须恢复 False，否则后续实时核对永远被去重跳过。"""

    def test_submit_exception_clears_in_flight(self, hooks: SimpleNamespace) -> None:
        """executor.submit 抛异常（如已 shutdown）→ _in_flight 恢复 False，下次事件可继续触发。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.0)
        rec = _new_rec(hooks)
        try:
            # 先 shutdown executor，让后续 submit 抛 RuntimeError
            rec._executor.shutdown(wait=False)
            # 第一次提交：会因 executor shutdown 抛异常 → _submit_realtime 内部 catch 并清 in_flight
            result = rec.request_realtime_reconciliation(p, broker)
            # request 返回 True（它确实置了 in_flight=True 后调 _submit_realtime）
            assert result is True
            # 关键断言：in_flight 已被清理（没卡在 True）
            assert rec._in_flight is False, "T0-13 失败: submit 异常后 in_flight 泄漏"
            # pending 标记也清了
            assert rec._pending_after is False
        finally:
            # 已经 shutdown 过，不再重复
            pass

    def test_in_flight_recovers_after_exception(self, hooks: SimpleNamespace) -> None:
        """异常清理 in_flight 后，重建 executor 能正常提交下一次核对。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.0)
        rec = _new_rec(hooks)
        try:
            rec._executor.shutdown(wait=False)
            rec.request_realtime_reconciliation(p, broker)
            assert rec._in_flight is False
            # 重建 executor（模拟重启/重连）
            from concurrent.futures import ThreadPoolExecutor
            rec._executor = ThreadPoolExecutor(max_workers=2)
            result2 = rec.request_realtime_reconciliation(p, broker)
            assert result2 is True
            assert rec.flush(timeout=3.0) is True
            assert broker.calls >= 1
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  T0-14 / FI-21：核对异常 → consistent=UNKNOWN(None)；to_dict 增 success_ts/stale
# ═══════════════════════════════════════════════

class _ExplodingBroker:
    """get_account 抛异常的 broker：模拟 broker 查询持续失联。"""
    def get_account(self) -> Dict[str, Any]:
        raise RuntimeError("broker 查询失败（T0-14 模拟失联）")
    def get_positions(self) -> List[Any]:
        raise RuntimeError("broker 查询失败")


class TestReconcileUnknown:
    """T0-14：broker 查询异常时 consistent 置 None（UNKNOWN），不再保持旧 True。"""

    def test_exception_sets_consistent_none(self, hooks: SimpleNamespace) -> None:
        """broker 抛异常 → consistent=None（对账失联），不再是 True/False。"""
        p = _make_portfolio(100_000.0)
        broker = _ExplodingBroker()
        rec = _new_rec(hooks)
        try:
            rec.request_realtime_reconciliation(p, broker)
            assert rec.flush(timeout=3.0) is True
            state = rec.to_dict()
            assert state["consistent"] is None, "T0-14 失败: 异常后 consistent 应为 None"
        finally:
            rec.shutdown()

    def test_is_consistent_returns_false_when_unknown(self, hooks: SimpleNamespace) -> None:
        """consistent=None 时 is_consistent 属性保守返回 False（PnL 防脏门拒）。"""
        p = _make_portfolio(100_000.0)
        broker = _ExplodingBroker()
        rec = _new_rec(hooks)
        try:
            rec.request_realtime_reconciliation(p, broker)
            rec.flush(timeout=3.0)
            assert rec.is_consistent is False
        finally:
            rec.shutdown()

    def test_to_dict_has_success_and_stale(self, hooks: SimpleNamespace) -> None:
        """to_dict 包含 last_success_ts 和 stale_seconds 字段。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(100_000.0)
        rec = _new_rec(hooks)
        try:
            rec.request_realtime_reconciliation(p, broker)
            assert rec.flush(timeout=3.0) is True
            state = rec.to_dict()
            assert "last_success_ts" in state
            assert "stale_seconds" in state
            # 刚成功，stale_seconds 应该很小（<1s）
            assert state["last_success_ts"] > 0
            assert state["stale_seconds"] is not None
            assert 0 <= state["stale_seconds"] < 5.0
        finally:
            rec.shutdown()

    def test_stale_seconds_none_before_any_success(self, hooks: SimpleNamespace) -> None:
        """从未成功核对时 stale_seconds=None（不是 0）。"""
        rec = _new_rec(hooks)
        try:
            state = rec.to_dict()
            assert state["last_success_ts"] == 0.0
            assert state["stale_seconds"] is None
        finally:
            rec.shutdown()


# ═══════════════════════════════════════════════
#  T0-16：解冻入口 + 冻结超时升级告警（FI-23 / FI-08）
# ═══════════════════════════════════════════════

class TestUnfreezeAndTimeout:
    """T0-16：mark_resolved 解冻 + check_freeze_timeout 超时升级。"""

    def test_unfreeze_clears_frozen_and_audits(self, hooks: SimpleNamespace) -> None:
        """FI-23：解冻后 is_frozen=False，且有审计 action=unfreeze。"""
        p = _make_portfolio(100_000.0)
        # 制造 1 分钱差异 → 冻结
        broker = _FakeBroker(99_999.99)  # 差 1 分
        rec = _new_rec(hooks)
        try:
            rec.request_realtime_reconciliation(p, broker)
            rec.flush(timeout=3.0)
            assert rec.is_frozen is True
            # 解冻
            rec.mark_resolved(actor="admin", note="人工核查确认券商账正确")
            assert rec.is_frozen is False
            # 审计有 unfreeze 记录
            unfreeze_audits = [a for a in hooks.audits if a["action"] == "unfreeze"]
            assert len(unfreeze_audits) >= 1
            assert unfreeze_audits[-1]["actor"] == "admin"
        finally:
            rec.shutdown()

    def test_unfreeze_requires_actor(self, hooks: SimpleNamespace) -> None:
        """actor 为空 → ValueError。"""
        rec = _new_rec(hooks)
        try:
            with pytest.raises(ValueError):
                rec.mark_resolved(actor="", note="")
        finally:
            rec.shutdown()

    def test_freeze_timeout_triggers_escalation(self, hooks: SimpleNamespace) -> None:
        """FI-08：冻结超过 timeout → check_freeze_timeout 返回 True（升级告警）。"""
        p = _make_portfolio(100_000.0)
        broker = _FakeBroker(99_999.99)
        rec = _new_rec(hooks)
        try:
            rec.request_realtime_reconciliation(p, broker)
            rec.flush(timeout=3.0)
            assert rec.is_frozen is True
            # 刚冻结，未超时 → False
            assert rec.check_freeze_timeout(timeout_sec=1800.0) is False
            # 模拟冻结已超 30 分钟
            rec._frozen_since = time.time() - 1801.0
            assert rec.check_freeze_timeout(timeout_sec=1800.0) is True
            # 已发过升级告警，再调 → False（防重复）
            assert rec.check_freeze_timeout(timeout_sec=1800.0) is False
        finally:
            rec.shutdown()

    def test_no_freeze_no_timeout_check(self, hooks: SimpleNamespace) -> None:
        """未冻结 → check_freeze_timeout 直接返回 False。"""
        rec = _new_rec(hooks)
        try:
            assert rec.check_freeze_timeout() is False
        finally:
            rec.shutdown()
