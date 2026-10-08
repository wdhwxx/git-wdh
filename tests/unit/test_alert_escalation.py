# 文件名：tests/unit/test_alert_escalation.py
"""
test_alert_escalation.py - A-10 告警分级与升级落地（回写#214）单元测试
========================================================================
覆盖：
  1. P0~P3 业务分级 → SeverityLevel 统一映射（ALERT_LEVEL_CODE_MAP / p0_p3_level）
  2. runbook_id 显式字段贯通：details.runbook_id 提升 / category 默认兜底 /
     显式优先 / to_dict 带出 / 落库
  3. 未确认自动升级：注入超时未确认 WARNING → check_escalations → 升级 ERROR 且
     落库 acked + escalated 标记（不依赖通道投递成功）
  4. 升级守卫：escalation_enabled=False / 已确认 / 已解决 / 已升级 不重复升级
  5. A-7 收口：attach_engine 装配 attach_reconciliation_event_trigger（异常不阻断、
     enable_reconcile=False 不接线）

运行方式:
  pytest tests/unit/test_alert_escalation.py -v

设计原则（防未来）：
  使用固定偏移的历史时间戳（now - 310s）模拟"已超时未确认"，临时 sqlite 不碰
  真实 quant.db，不依赖随机时钟。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from myquant.core.constants import SeverityLevel
from myquant.database.connection import DatabaseConnection
from myquant.live.scheduler import LiveScheduler, attach_engine
from myquant.core.alert_store import AlertStore
from myquant.monitoring.alerting import (
    ALERT_LEVEL_CODE_MAP,
    DEFAULT_RUNBOOK_BY_CATEGORY,
    Alert,
    AlertManager,
    ConsoleAlertChannel,
    p0_p3_level,
)


# ═══════════════════════════════════════════
#  Fixture：临时库连接 + AlertStore（不碰真实 quant.db）
# ═══════════════════════════════════════════

@pytest.fixture()
def tmp_conn(tmp_path: Path) -> DatabaseConnection:
    return DatabaseConnection(tmp_path / "alert_esc.db")


@pytest.fixture()
def store(tmp_conn: DatabaseConnection) -> AlertStore:
    return AlertStore(connection=tmp_conn)


def _manager_with_store(store: AlertStore) -> AlertManager:
    """带临时落库 + console 通道的管理器（避免未投递落盘写 logs）。"""
    mgr = AlertManager(store=store)
    mgr.add_channel(ConsoleAlertChannel("test-console"))
    return mgr


# ═══════════════════════════════════════════
#  1. P0~P3 统一映射（P0-3 分级表）
# ═══════════════════════════════════════════

class TestP0P3LevelMap:
    def test_map_constant_values(self) -> None:
        assert ALERT_LEVEL_CODE_MAP["P0"] is SeverityLevel.CRITICAL
        assert ALERT_LEVEL_CODE_MAP["P1"] is SeverityLevel.ERROR
        assert ALERT_LEVEL_CODE_MAP["P2"] is SeverityLevel.WARNING
        assert ALERT_LEVEL_CODE_MAP["P3"] is SeverityLevel.INFO

    def test_p0_p3_level_direct(self) -> None:
        assert p0_p3_level("P0") is SeverityLevel.CRITICAL
        assert p0_p3_level("P1") is SeverityLevel.ERROR
        assert p0_p3_level("P2") is SeverityLevel.WARNING
        assert p0_p3_level("P3") is SeverityLevel.INFO

    def test_case_and_whitespace_tolerant(self) -> None:
        assert p0_p3_level(" p0 ") is SeverityLevel.CRITICAL
        assert p0_p3_level("p3") is SeverityLevel.INFO

    def test_unknown_code_rejected(self) -> None:
        with pytest.raises(ValueError):
            p0_p3_level("P9")
        with pytest.raises(ValueError):
            p0_p3_level(None)  # type: ignore[arg-type]


# ═══════════════════════════════════════════
#  2. runbook_id 显式字段贯通
# ═══════════════════════════════════════════

class TestRunbookIdFlow:
    def test_details_runbook_id_promoted(self) -> None:
        a = Alert(level=SeverityLevel.WARNING, message="t", category="broker",
                  details={"runbook_id": "RB-CUSTOM"})
        assert a.runbook_id == "RB-CUSTOM"
        # details 与显式字段一致
        assert a.details["runbook_id"] == "RB-CUSTOM"

    def test_category_default_fallback(self) -> None:
        a = Alert(level=SeverityLevel.ERROR, message="t", category="trade")
        assert a.runbook_id == DEFAULT_RUNBOOK_BY_CATEGORY["trade"]

    def test_explicit_runbook_wins(self) -> None:
        a = Alert(level=SeverityLevel.CRITICAL, message="t", category="trade",
                  runbook_id="RB-FORCED", details={"runbook_id": "RB-DETAIL"})
        assert a.runbook_id == "RB-FORCED"

    def test_to_dict_includes_runbook(self) -> None:
        a = Alert(level=SeverityLevel.WARNING, message="t", category="risk",
                  runbook_id="RB-RISK-001")
        assert a.to_dict()["runbook_id"] == "RB-RISK-001"

    def test_runbook_persisted_on_send(self, store: AlertStore) -> None:
        mgr = _manager_with_store(store)
        a = Alert(level=SeverityLevel.CRITICAL, message="对账差异",
                  category="reconciliation", alert_id="rb-1",
                  timestamp=time.time())
        mgr.send_alert(a, dedup_enabled=False)
        row = store.get("rb-1")
        assert row is not None
        assert row["runbook_id"] == DEFAULT_RUNBOOK_BY_CATEGORY["reconciliation"]


# ═══════════════════════════════════════════
#  3. 超时未确认自动升级（核心验收）
# ═══════════════════════════════════════════

class TestAutoEscalation:
    def _send_stale_warning(self, mgr: AlertManager, alert_id: str = "stale1") -> Alert:
        """注入一条已超时（>escalation_time）的未确认 WARNING。"""
        stale = Alert(
            level=SeverityLevel.WARNING,
            message="超时未确认告警",
            category="broker",
            alert_id=alert_id,
            timestamp=time.time() - 310.0,  # 已超默认 300s
        )
        mgr.send_alert(stale, dedup_enabled=False)
        return stale

    def test_stale_warning_escalates_to_error_and_persists(
        self, store: AlertStore
    ) -> None:
        mgr = _manager_with_store(store)
        self._send_stale_warning(mgr)

        n = mgr.check_escalations()
        assert n == 1

        # 原告警：acked + escalated 落库
        row = store.get("stale1")
        assert row is not None
        assert row["status"] == "acked"
        assert row["escalated"] is True

        # 升级告警：ERROR 且带 [升级] 前缀落库
        errs = store.load_history(limit=10, level=SeverityLevel.ERROR.value)
        assert any(e["message"].startswith("[升级]") for e in errs)

    def test_escalation_happens_even_without_channels(
        self, store: AlertStore
    ) -> None:
        """补缺：无通道投递（sent=0）时仍升级并落库 escalated（原 sent>0 门控已移除）。"""
        mgr = AlertManager(store=store)  # 不挂任何通道
        self._send_stale_warning(mgr)
        assert mgr.check_escalations() == 1
        row = store.get("stale1")
        assert row is not None and row["escalated"] is True

    def test_no_repeat_escalation(self, store: AlertStore) -> None:
        mgr = _manager_with_store(store)
        self._send_stale_warning(mgr)
        assert mgr.check_escalations() == 1
        # 原告警已 acked，不再被升级
        assert mgr.check_escalations() == 0

    def test_acknowledged_not_escalated(self, store: AlertStore) -> None:
        mgr = _manager_with_store(store)
        acked = Alert(level=SeverityLevel.WARNING, message="t", alert_id="ack1",
                      timestamp=time.time() - 310.0, acknowledged=True)
        mgr.send_alert(acked, dedup_enabled=False)
        assert mgr.check_escalations() == 0

    def test_resolved_not_escalated(self, store: AlertStore) -> None:
        mgr = _manager_with_store(store)
        resolved = Alert(level=SeverityLevel.WARNING, message="t", alert_id="res1",
                         timestamp=time.time() - 310.0, resolved=True)
        mgr.send_alert(resolved, dedup_enabled=False)
        assert mgr.check_escalations() == 0

    def test_recent_warning_not_escalated(self, store: AlertStore) -> None:
        mgr = _manager_with_store(store)
        fresh = Alert(level=SeverityLevel.WARNING, message="t", alert_id="fresh1",
                      timestamp=time.time())  # 刚产生，未超时
        mgr.send_alert(fresh, dedup_enabled=False)
        assert mgr.check_escalations() == 0

    def test_escalation_disabled(self, store: AlertStore) -> None:
        mgr = _manager_with_store(store)
        rule = mgr.get_alert_rule()
        rule.escalation_enabled = False
        mgr.set_alert_rule(rule)
        self._send_stale_warning(mgr)
        assert mgr.check_escalations() == 0


# ═══════════════════════════════════════════
#  5. A-7 收口：attach_engine 事件驱动对账装配
# ═══════════════════════════════════════════

class _FakePortfolio:
    """记录 attach 调用；可选让 attach 抛异常以验证不阻断。"""

    def __init__(self, attach_raises: bool = False) -> None:
        self.calls: List[Any] = []
        self._attach_raises = attach_raises

    def attach_reconciliation_event_trigger(self, bus: Any, broker: Any) -> List[str]:
        if self._attach_raises:
            raise RuntimeError("reconcile attach boom")
        self.calls.append((bus, broker))
        return ["h1", "h2"]


def _base_kwargs(**over: Any) -> Dict[str, Any]:
    kw: Dict[str, Any] = dict(
        enable_patrol=False,
        enable_daily_report=False,
        enable_t1_settle=False,
        enable_monthly_governance=False,
    )
    kw.update(over)
    return kw


class TestAttachReconciliationEvent:
    def test_event_trigger_wired_on_attach(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_bus = SimpleNamespace()
        monkeypatch.setattr(
            "myquant.core.event_bus.get_event_bus", lambda: fake_bus
        )
        sched = LiveScheduler()
        portfolio = _FakePortfolio()
        broker = object()
        tasks = attach_engine(sched, portfolio=portfolio, broker=broker,
                              **_base_kwargs())
        assert len(portfolio.calls) == 1
        bus_used, broker_used = portfolio.calls[0]
        assert bus_used is fake_bus
        assert broker_used is broker
        assert "escalation" in tasks  # 升级周期任务仍在

    def test_attach_exception_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "myquant.core.event_bus.get_event_bus", lambda: SimpleNamespace()
        )
        sched = LiveScheduler()
        portfolio = _FakePortfolio(attach_raises=True)
        # 抛异常不应阻断 attach_engine 主流程
        tasks = attach_engine(sched, portfolio=portfolio, broker=object(),
                              **_base_kwargs())
        assert "escalation" in tasks

    def test_disabled_reconcile_not_wired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "myquant.core.event_bus.get_event_bus", lambda: SimpleNamespace()
        )
        sched = LiveScheduler()
        portfolio = _FakePortfolio()
        attach_engine(sched, portfolio=portfolio, broker=object(),
                      **_base_kwargs(enable_reconcile=False))
        assert portfolio.calls == []

    def test_no_portfolio_method_not_wired(self) -> None:
        sched = LiveScheduler()
        plain = SimpleNamespace()  # 无 attach_reconciliation_event_trigger
        tasks = attach_engine(sched, portfolio=plain, broker=object(),
                              **_base_kwargs())
        assert "escalation" in tasks
