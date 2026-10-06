# 文件名：tests/unit/test_action_engine.py
"""
D-1 自动化响应编排规则引擎 + D-2 自愈动作库测试（回写#243/#244）
==================================================================
覆盖：规则注册/校验、字典归一化、条件匹配（级别/类别/来源/指标 AND 语义）、
三阶段安全模型（shadow 只读 / confirm 人工确认 / auto 自动执行）、Kill Switch
独立停止、冷却防抖、幂等、回滚（pause→resume 还原 / notify 不可撤销）、
run_cycle 周期巡检、全链路留痕（执行历史 + 审计）、缺注入回调不假成功（KP-M02）、
D-2 自愈动作库（行情中断→切备用行情源/暂停策略、拒单率飙升→暂停策略/通知交易员、
数据质量异常→停止信号生成/回滚数据版本：dry-run 演练→执行→undo 恢复→审计）、
单例 get/reset。全部注入假执行器/假告警管理器/内存审计，不碰 quant.db。
"""

from typing import Any, Dict, List, Optional

import pytest

from myquant.monitoring.action_engine import (
    STATUS_BLOCKED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_NOT_SUPPORTED,
    STATUS_PENDING_APPROVAL,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_ROLLED_BACK,
    STATUS_SKIPPED,
    ActionApproval,
    ActionEngine,
    ActionExecutor,
    ActionRule,
    ActionResult,
    NotifyExecutor,
    NotifyTraderExecutor,
    PauseStrategyExecutor,
    RollbackDataVersionExecutor,
    StopSignalGenerationExecutor,
    SwitchDataSourceExecutor,
    get_action_engine,
    get_action_executor,
    list_action_types,
    register_action_executor,
    reset_action_engine,
)


# ── 测试辅助 ──────────────────────────────────────────────────

class _AuditRecorder:
    """内存审计记录器（替代 AuditLogStore，隔离不碰库）。"""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def append(
        self,
        kind: str,
        action: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        rec = {"kind": kind, "action": action, "actor": actor,
               "detail": dict(detail or {})}
        self.records.append(rec)
        return rec


class _FakeAlertManager:
    """假告警管理器（run_cycle 用）。"""

    def __init__(self, alerts: Optional[List[Any]] = None) -> None:
        self.alerts: List[Any] = list(alerts or [])
        self.calls: int = 0

    def get_unacknowledged(self, limit: int = 50) -> List[Any]:
        self.calls += 1
        return list(self.alerts)


class _FakeAlertSink:
    """假告警接收器（notify 执行器用，返回通道数）。"""

    def alert(self, **kw: Any) -> int:
        return 2


class _BrokenAlertManager:
    """拉取告警抛异常的假管理器。"""

    def get_unacknowledged(self, limit: int = 50) -> List[Any]:
        raise RuntimeError("bus down")


class _FakeExecutor:
    """假执行器（验证 execute/undo 调用契约）。"""

    action_type = "fake_act"

    def execute(self, ctx: Dict[str, Any], params: Dict[str, Any],
                dry_run: bool = False) -> Dict[str, Any]:
        self.last_call = {"ctx": ctx, "params": params, "dry_run": dry_run}
        return {
            "status": STATUS_EXECUTED,
            "message": "fake 执行成功",
            "details": {"fake": True},
            "rollback_info": {"fake_key": "fake_val"},
        }

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        self.last_undo = rollback_info
        return True


def _register_fake_executor() -> _FakeExecutor:
    """注册一个 fake_act 执行器（继承 ActionExecutor，绑定假实例回调）。"""
    if get_action_executor("fake_act") is not None:
        raise AssertionError("fake_act 已注册（测试隔离失败）")
    fake = _FakeExecutor()

    class _FakeExecutorCls(ActionExecutor):
        action_type = "fake_act"

        def execute(self, ctx: Dict[str, Any], params: Dict[str, Any],
                    dry_run: bool = False) -> Dict[str, Any]:
            return fake.execute(ctx, params, dry_run)

        def undo(self, rollback_info: Dict[str, Any]) -> bool:
            return fake.undo(rollback_info)

    register_action_executor(_FakeExecutorCls)
    return fake


@pytest.fixture(autouse=True)
def _cleanup_executor():
    yield
    if get_action_executor("fake_act") is not None:
        from myquant.monitoring import action_engine as ae_mod
        ae_mod._ACTION_EXECUTORS.pop("fake_act", None)


def _alert(**kw: Any) -> Dict[str, Any]:
    base = {
        "level": "ERROR",
        "category": "market_staleness",
        "source": "monitoring.watchdog",
        "message": "行情断流",
        "deduplication_key": "watchdog:staleness",
    }
    base.update(kw)
    return base


def _pause_rule(rule_id: str = "r1", mode: Optional[str] = None,
                condition: Optional[Dict[str, Any]] = None,
                cooldown: float = 300.0) -> ActionRule:
    return ActionRule(
        rule_id=rule_id,
        name="暂停策略",
        mode=mode,
        condition=condition or {"categories": ["market_staleness"]},
        action={"type": "pause_strategy",
                "params": {"pause_fn": lambda: None, "resume_fn": lambda: None,
                           "target": "engine"}},
        cooldown_seconds=cooldown,
    )


# ── 规则注册与校验 ────────────────────────────────────────────

class TestRuleRegistration:
    def test_register_and_get(self) -> None:
        engine = ActionEngine(mode="shadow")
        rule = _pause_rule()
        assert engine.register_rule(rule) == "r1"
        assert engine.get_rule("r1") is rule
        assert len(engine.get_rules()) == 1

    def test_duplicate_rule_rejected(self) -> None:
        engine = ActionEngine()
        engine.register_rule(_pause_rule())
        with pytest.raises(ValueError):
            engine.register_rule(_pause_rule())

    def test_empty_rule_id_rejected(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.register_rule(ActionRule(rule_id="  "))

    def test_unknown_action_type_rejected(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.register_rule(
                ActionRule(rule_id="r9", action={"type": "no_such_action"})
            )

    def test_illegal_mode_rejected(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.register_rule(_pause_rule(mode="auto_fast"))

    def test_illegal_condition_rejected(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.register_rule(_pause_rule(condition={"levels": ["PANIC"]}))
        with pytest.raises(ValueError):
            engine.register_rule(_pause_rule(condition={"metrics": [{"name": "m", "op": "~"}]}))
        with pytest.raises(ValueError):
            engine.register_rule(_pause_rule(condition="not-dict"))

    def test_illegal_cooldown_rejected(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.register_rule(_pause_rule(cooldown=-1.0))

    def test_remove_rule(self) -> None:
        engine = ActionEngine()
        engine.register_rule(_pause_rule())
        assert engine.remove_rule("r1") is True
        assert engine.remove_rule("r1") is False

    def test_non_actionrule_rejected(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.register_rule({"rule_id": "x"})


# ── 字典归一化与批量加载 ──────────────────────────────────────

class TestRuleFromDict:
    def test_rule_from_dict_full(self) -> None:
        rule = ActionEngine.rule_from_dict({
            "rule_id": "rd1", "name": "拒单飙升暂停", "enabled": True,
            "mode": "confirm",
            "condition": {"categories": ["trade"]},
            "action": {"type": "notify", "params": {"message": "hi"}},
            "cooldown_seconds": 60, "description": "desc",
        })
        assert rule.rule_id == "rd1"
        assert rule.mode == "confirm"
        assert rule.cooldown_seconds == 60.0

    def test_rule_from_dict_defaults(self) -> None:
        rule = ActionEngine.rule_from_dict(
            {"id": "rd2", "action": {"type": "notify"}}
        )
        assert rule.rule_id == "rd2"
        assert rule.enabled is True
        assert rule.mode is None
        assert rule.cooldown_seconds == 300.0

    def test_rule_from_dict_missing_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            ActionEngine.rule_from_dict({"action": {"type": "notify"}})

    def test_rule_from_dict_missing_action_rejected(self) -> None:
        with pytest.raises(ValueError):
            ActionEngine.rule_from_dict({"rule_id": "rd3"})

    def test_rule_from_dict_bad_cooldown_falls_back(self) -> None:
        rule = ActionEngine.rule_from_dict({
            "rule_id": "rd4", "action": {"type": "notify"},
            "cooldown_seconds": -5,
        })
        assert rule.cooldown_seconds == 300.0

    def test_load_rules_batch(self) -> None:
        engine = ActionEngine()
        n = engine.load_rules([
            {"rule_id": "b1", "action": {"type": "notify"}},
            {"rule_id": "b2", "action": {"type": "pause_strategy"}},
        ])
        assert n == 2
        assert len(engine.get_rules()) == 2

    def test_load_rules_rejects_non_list(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.load_rules({"rule_id": "x"})


# ── 条件匹配 ──────────────────────────────────────────────────

class TestConditionMatch:
    def test_level_match(self) -> None:
        engine = ActionEngine()
        rule = _pause_rule(condition={"levels": ["ERROR", "CRITICAL"]})
        assert engine._match_rule(rule, _alert(level="ERROR"), {})
        assert engine._match_rule(rule, _alert(level="CRITICAL"), {})
        assert not engine._match_rule(rule, _alert(level="WARNING"), {})

    def test_category_match(self) -> None:
        engine = ActionEngine()
        rule = _pause_rule(condition={"categories": ["market_staleness"]})
        assert engine._match_rule(rule, _alert(category="market_staleness"), {})
        assert not engine._match_rule(rule, _alert(category="trade"), {})

    def test_source_prefix_match(self) -> None:
        engine = ActionEngine()
        rule = _pause_rule(condition={"sources": ["monitoring.watchdog"]})
        assert engine._match_rule(rule, _alert(source="monitoring.watchdog"), {})
        assert engine._match_rule(
            rule, _alert(source="monitoring.watchdog.sub"), {}
        )
        assert not engine._match_rule(rule, _alert(source="live.engine"), {})

    def test_empty_conditions_match_any(self) -> None:
        engine = ActionEngine()
        rule = _pause_rule(condition={})
        assert engine._match_rule(rule, _alert(level="INFO"), {})

    def test_metric_gate_and_semantics(self) -> None:
        engine = ActionEngine()
        rule = _pause_rule(condition={
            "metrics": [
                {"name": "order_reject_rate", "op": ">", "value": 0.001},
                {"name": "order_count", "op": ">=", "value": 100},
            ]
        })
        assert engine._match_rule(
            rule, None, {"order_reject_rate": 0.01, "order_count": 200}
        )
        assert not engine._match_rule(
            rule, None, {"order_reject_rate": 0.0001, "order_count": 200}
        )
        assert not engine._match_rule(
            rule, None, {"order_reject_rate": 0.01, "order_count": 50}
        )

    def test_metric_missing_fail_closed(self) -> None:
        engine = ActionEngine()
        rule = _pause_rule(condition={"metrics": [{"name": "rate", "op": ">", "value": 1.0}]})
        assert not engine._match_rule(rule, None, {})

    def test_alert_none_with_level_constraints_not_match(self) -> None:
        engine = ActionEngine()
        rule = _pause_rule(condition={"levels": ["ERROR"]})
        assert not engine._match_rule(rule, None, {})

    def test_metric_only_rule_without_alert(self) -> None:
        engine = ActionEngine()
        rule = _pause_rule(condition={"metrics": [{"name": "rate", "op": ">", "value": 1.0}]})
        assert engine._match_rule(rule, None, {"rate": 2.0})


# ── 三阶段安全模型 ────────────────────────────────────────────

class TestThreePhaseSafety:
    def test_shadow_proposes_without_execute(self) -> None:
        executed: List[str] = []
        rule = ActionRule(
            rule_id="s1",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy",
                    "params": {"pause_fn": lambda: executed.append("x"),
                               "resume_fn": lambda: None, "target": "engine"}},
        )
        engine = ActionEngine(mode="shadow")
        engine.register_rule(rule)
        results = engine.handle_alert(_alert())
        assert len(results) == 1
        assert results[0].status == STATUS_PROPOSED
        assert results[0].executed is False
        assert results[0].dry_run is True
        assert executed == []  # 只读：不真实执行

    def test_confirm_pending_then_approve_executes(self) -> None:
        state: List[str] = []
        rule = ActionRule(
            rule_id="c1",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy",
                    "params": {"pause_fn": lambda: state.append("paused"),
                               "resume_fn": lambda: state.clear(), "target": "engine"}},
        )
        engine = ActionEngine(mode="confirm", clock=lambda: 100.0)
        engine.register_rule(rule)
        results = engine.handle_alert(_alert())
        assert results[0].status == STATUS_PENDING_APPROVAL
        assert state == []  # 未审批不执行
        approvals = engine.pending_approvals()
        assert len(approvals) == 1
        ok = engine.approve(approvals[0]["approval_id"], actor="trader", reason="确认")
        assert ok.status == STATUS_EXECUTED
        assert state == ["paused"]
        assert engine.pending_approvals() == []

    def test_confirm_duplicate_pending_skipped(self) -> None:
        engine = ActionEngine(mode="confirm")
        engine.register_rule(_pause_rule())
        first = engine.handle_alert(_alert())
        assert first[0].status == STATUS_PENDING_APPROVAL
        second = engine.handle_alert(_alert())  # 同 dedup key → 同 action_key
        assert second[0].status == STATUS_SKIPPED
        assert len(engine.pending_approvals()) == 1

    def test_confirm_reject_no_execute(self) -> None:
        state: List[str] = []
        rule = ActionRule(
            rule_id="c2",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy",
                    "params": {"pause_fn": lambda: state.append("paused"),
                               "resume_fn": lambda: None, "target": "engine"}},
        )
        engine = ActionEngine(mode="confirm")
        engine.register_rule(rule)
        engine.handle_alert(_alert())
        approval_id = engine.pending_approvals()[0]["approval_id"]
        result = engine.reject(approval_id, actor="trader", reason="人工否决")
        assert result.status == STATUS_REJECTED
        assert state == []
        assert engine.pending_approvals() == []

    def test_approval_timeout_expires(self) -> None:
        clock = {"t": 1000.0}

        def _clock() -> float:
            return clock["t"]

        engine = ActionEngine(mode="confirm", clock=_clock,
                              approval_timeout_seconds=60.0)
        engine.register_rule(_pause_rule())
        engine.handle_alert(_alert())
        assert len(engine.pending_approvals()) == 1
        clock["t"] = 2000.0  # 超时
        n = engine._expire_pending_approvals()
        assert n == 1
        assert engine.pending_approvals() == []

    def test_expired_approval_cannot_approve(self) -> None:
        clock = {"t": 1000.0}

        def _clock() -> float:
            return clock["t"]

        engine = ActionEngine(mode="confirm", clock=_clock,
                              approval_timeout_seconds=60.0)
        engine.register_rule(_pause_rule())
        engine.handle_alert(_alert())
        approval_id = engine.pending_approvals()[0]["approval_id"]
        clock["t"] = 2000.0
        engine._expire_pending_approvals()
        with pytest.raises(ValueError):
            engine.approve(approval_id, actor="trader")

    def test_auto_executes_directly(self) -> None:
        state: List[str] = []
        rule = ActionRule(
            rule_id="a1",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy",
                    "params": {"pause_fn": lambda: state.append("paused"),
                               "resume_fn": lambda: None, "target": "engine"}},
        )
        engine = ActionEngine(mode="auto")
        engine.register_rule(rule)
        results = engine.handle_alert(_alert())
        assert results[0].status == STATUS_EXECUTED
        assert results[0].executed is True
        assert state == ["paused"]

    def test_per_rule_mode_overrides_engine(self) -> None:
        state: List[str] = []
        rule = ActionRule(
            rule_id="m1", mode="shadow",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy",
                    "params": {"pause_fn": lambda: state.append("paused"),
                               "resume_fn": lambda: None, "target": "engine"}},
        )
        engine = ActionEngine(mode="auto")
        engine.register_rule(rule)
        results = engine.handle_alert(_alert())
        assert results[0].status == STATUS_PROPOSED
        assert state == []  # 规则级 shadow 覆盖引擎 auto


# ── Kill Switch ───────────────────────────────────────────────

class TestKillSwitch:
    def test_kill_switch_blocks_auto(self) -> None:
        engine = ActionEngine(mode="auto")
        engine.register_rule(_pause_rule())
        engine.set_kill_switch(True, actor="admin", reason="演练停止")
        results = engine.handle_alert(_alert())
        assert results[0].status == STATUS_BLOCKED
        assert results[0].executed is False
        assert engine.kill_switch is True

    def test_kill_switch_blocks_approve(self) -> None:
        engine = ActionEngine(mode="confirm")
        engine.register_rule(_pause_rule())
        engine.handle_alert(_alert())
        approval_id = engine.pending_approvals()[0]["approval_id"]
        engine.set_kill_switch(True, actor="admin")
        result = engine.approve(approval_id, actor="trader")
        assert result.status == STATUS_BLOCKED

    def test_kill_switch_reset_allows_execution(self) -> None:
        state: List[str] = []
        rule = ActionRule(
            rule_id="k1",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy",
                    "params": {"pause_fn": lambda: state.append("paused"),
                               "resume_fn": lambda: None, "target": "engine"}},
        )
        engine = ActionEngine(mode="auto")
        engine.register_rule(rule)
        engine.set_kill_switch(True, actor="admin")
        engine.handle_alert(_alert())
        assert state == []
        engine.set_kill_switch(False, actor="admin")
        results = engine.handle_alert(
            _alert(deduplication_key="watchdog:staleness-2")
        )
        assert results[0].status == STATUS_EXECUTED
        assert state == ["paused"]

    def test_kill_switch_history(self) -> None:
        engine = ActionEngine()
        engine.set_kill_switch(True, actor="admin", reason="r1")
        engine.set_kill_switch(False, actor="ops", reason="r2")
        history = engine.kill_switch_history()
        assert len(history) == 2
        assert history[0]["active"] is True
        assert history[1]["active"] is False

    def test_kill_switch_requires_actor(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.set_kill_switch(True, actor="  ")


# ── 冷却防抖 / 幂等 ───────────────────────────────────────────

class TestCooldown:
    def test_cooldown_skips_same_key(self) -> None:
        clock = {"t": 1000.0}

        def _clock() -> float:
            return clock["t"]

        engine = ActionEngine(mode="auto", clock=_clock)
        engine.register_rule(_pause_rule(cooldown=300.0))
        first = engine.handle_alert(_alert())
        assert first[0].status == STATUS_EXECUTED
        # 冷却窗口内同 key 跳过
        second = engine.handle_alert(_alert())
        assert second[0].status == STATUS_SKIPPED
        # 窗口外再执行
        clock["t"] = 2000.0
        third = engine.handle_alert(_alert(deduplication_key="watchdog:staleness-9"))
        assert third[0].status == STATUS_EXECUTED

    def test_different_dedup_key_not_cooldown(self) -> None:
        engine = ActionEngine(mode="auto")
        engine.register_rule(_pause_rule(cooldown=300.0))
        first = engine.handle_alert(_alert(deduplication_key="k1"))
        second = engine.handle_alert(_alert(deduplication_key="k2"))
        assert first[0].status == STATUS_EXECUTED
        assert second[0].status == STATUS_EXECUTED

    def test_disabled_rule_not_matched(self) -> None:
        engine = ActionEngine(mode="auto")
        rule = _pause_rule()
        rule.enabled = False
        engine.register_rule(rule)
        results = engine.handle_alert(_alert())
        assert results == []


# ── 回滚 ──────────────────────────────────────────────────────

class TestRollback:
    def test_rollback_pause_via_resume(self) -> None:
        state: List[str] = ["paused"]

        def resume() -> None:
            state.clear()

        rule = ActionRule(
            rule_id="rb1",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy",
                    "params": {"pause_fn": lambda: None,
                               "resume_fn": resume, "target": "engine"}},
        )
        engine = ActionEngine(mode="auto")
        engine.register_rule(rule)
        results = engine.handle_alert(_alert())
        action_id = results[0].action_id
        assert engine.rollback(action_id, actor="trader", reason="恢复") is True
        assert state == []

    def test_rollback_without_resume_false(self) -> None:
        rule = ActionRule(
            rule_id="rb2",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy",
                    "params": {"pause_fn": lambda: None, "target": "engine"}},
        )
        engine = ActionEngine(mode="auto")
        engine.register_rule(rule)
        results = engine.handle_alert(_alert())
        assert engine.rollback(results[0].action_id, actor="trader") is False

    def test_rollback_notify_undo_false(self) -> None:
        engine = ActionEngine(mode="auto")
        engine._alert_manager = _FakeAlertSink()  # 注入假接收器，隔离不碰库
        engine.register_rule(ActionRule(
            rule_id="rb3",
            condition={"categories": ["market_staleness"]},
            action={"type": "notify", "params": {"message": "test"}},
        ))
        results = engine.handle_alert(_alert())
        assert results[0].status == STATUS_EXECUTED
        # 通知不可撤销
        assert engine.rollback(results[0].action_id, actor="trader") is False

    def test_rollback_unknown_action_id_false(self) -> None:
        engine = ActionEngine(mode="auto")
        assert engine.rollback("no_such_id", actor="trader") is False

    def test_rollback_requires_actor(self) -> None:
        engine = ActionEngine(mode="auto")
        engine.register_rule(_pause_rule())
        results = engine.handle_alert(_alert())
        with pytest.raises(ValueError):
            engine.rollback(results[0].action_id, actor="  ")

    def test_custom_executor_rollback(self) -> None:
        fake = _register_fake_executor()
        engine = ActionEngine(mode="auto")
        engine.register_rule(ActionRule(
            rule_id="rb4",
            condition={"categories": ["market_staleness"]},
            action={"type": "fake_act", "params": {}},
        ))
        results = engine.handle_alert(_alert())
        assert results[0].status == STATUS_EXECUTED
        assert engine.rollback(results[0].action_id, actor="trader") is True
        assert fake.last_undo == {"fake_key": "fake_val"}


# ── 执行器契约 ────────────────────────────────────────────────

class TestExecutorContract:
    def test_pause_without_injection_not_supported(self) -> None:
        rule = ActionRule(
            rule_id="np1",
            condition={"categories": ["market_staleness"]},
            action={"type": "pause_strategy", "params": {"target": "engine"}},
        )
        engine = ActionEngine(mode="auto")
        engine.register_rule(rule)
        results = engine.handle_alert(_alert())
        assert results[0].status == STATUS_NOT_SUPPORTED  # KP-M02 不假成功

    def test_notify_executor_dry_run(self) -> None:
        out = NotifyExecutor().execute(
            ctx={}, params={"message": "hi"}, dry_run=True
        )
        assert out["status"] == STATUS_EXECUTED
        assert out["details"]["dry_run"] is True

    def test_notify_executor_bad_level_failed(self) -> None:
        out = NotifyExecutor().execute(
            ctx={}, params={"level": "PANIC"}, dry_run=False
        )
        assert out["status"] == STATUS_FAILED

    def test_register_duplicate_executor_rejected(self) -> None:
        with pytest.raises(ValueError):
            register_action_executor(NotifyExecutor)

    def test_list_action_types_contains_builtins(self) -> None:
        types = list_action_types()
        assert "notify" in types
        assert "pause_strategy" in types


# ── run_cycle 周期巡检 ────────────────────────────────────────

class TestRunCycle:
    def test_cycle_evaluates_unacked_alerts(self) -> None:
        engine = ActionEngine(mode="shadow")
        engine.register_rule(_pause_rule())
        engine._alert_manager = _FakeAlertManager([
            _alert(deduplication_key="k1"),
            _alert(deduplication_key="k2", level="WARNING"),
        ])
        summary = engine.run_cycle()
        assert summary["evaluated"] == 2
        assert summary["proposed"] == 2
        assert summary["error"] is None

    def test_cycle_tallies_auto_execution(self) -> None:
        engine = ActionEngine(mode="auto")
        engine.register_rule(_pause_rule())
        engine._alert_manager = _FakeAlertManager([
            _alert(deduplication_key="k1"),
            _alert(deduplication_key="k2"),
        ])
        summary = engine.run_cycle()
        assert summary["executed"] == 2

    def test_cycle_manager_missing_degraded(self) -> None:
        engine = ActionEngine(mode="shadow")
        engine._alert_manager = None
        # 不触发惰性 get_alert_manager 单例（测试隔离）：直接注入不可用管理器
        engine._alert_manager = _BrokenAlertManager()
        summary = engine.run_cycle()
        assert summary["error"] is not None
        assert summary["evaluated"] == 0

    def test_cycle_no_rules_zero(self) -> None:
        engine = ActionEngine(mode="auto")
        engine._alert_manager = _FakeAlertManager([_alert()])
        summary = engine.run_cycle()
        assert summary["evaluated"] == 0

    def test_cycle_expires_approvals(self) -> None:
        clock = {"t": 1000.0}

        def _clock() -> float:
            return clock["t"]

        engine = ActionEngine(mode="confirm", clock=_clock,
                              approval_timeout_seconds=60.0)
        engine.register_rule(_pause_rule())
        engine._alert_manager = _FakeAlertManager([_alert(deduplication_key="k1")])
        engine.run_cycle()
        assert len(engine.pending_approvals()) == 1
        clock["t"] = 5000.0
        summary = engine.run_cycle()
        assert summary["expired_approvals"] >= 1


# ── 留痕 / 审计 / 观测 ────────────────────────────────────────

class TestTrail:
    def test_history_json_serializable(self) -> None:
        engine = ActionEngine(mode="auto")
        engine.register_rule(_pause_rule())
        engine.handle_alert(_alert())
        history = engine.history()
        assert history and isinstance(history[0], dict)
        import json
        json.dumps(history)  # 不可序列化则抛异常

    def test_history_limit_validation(self) -> None:
        engine = ActionEngine()
        with pytest.raises(ValueError):
            engine.history(limit=0)

    def test_history_cap(self) -> None:
        engine = ActionEngine(mode="auto", max_history=3)
        engine.register_rule(_pause_rule())
        for i in range(6):
            engine.handle_alert(_alert(deduplication_key=f"k{i}"))
        assert len(engine.history()) == 3

    def test_audit_records_written(self) -> None:
        audit = _AuditRecorder()
        engine = ActionEngine(mode="auto", audit_log=audit)
        engine.register_rule(_pause_rule())
        engine.handle_alert(_alert())
        assert any(r["kind"] == "action_engine" for r in audit.records)
        assert any("executed" in r["action"] for r in audit.records)

    def test_audit_failure_not_blocking(self) -> None:
        class _BrokenAudit:
            def append(self, *a: Any, **kw: Any) -> Dict[str, Any]:
                raise RuntimeError("audit down")

        engine = ActionEngine(mode="auto", audit_log=_BrokenAudit())
        engine.register_rule(_pause_rule())
        results = engine.handle_alert(_alert())
        assert results[0].status == STATUS_EXECUTED

    def test_summary(self) -> None:
        engine = ActionEngine(mode="shadow")
        engine.register_rule(_pause_rule())
        engine.handle_alert(_alert())
        s = engine.summary()
        assert s["mode"] == "shadow"
        assert s["rules"] == 1
        assert s["proposed_total"] == 1

    def test_mode_change_history_and_validation(self) -> None:
        engine = ActionEngine(mode="shadow")
        engine.set_mode("confirm", actor="admin", reason="阶段二")
        assert engine.mode == "confirm"
        assert engine.mode_history()[-1]["new"] == "confirm"
        with pytest.raises(ValueError):
            engine.set_mode("nope", actor="admin")


# ── 单例 ──────────────────────────────────────────────────────

class TestSingleton:
    def test_get_and_reset(self) -> None:
        reset_action_engine()
        try:
            engine = get_action_engine()
            assert isinstance(engine, ActionEngine)
            assert get_action_engine() is engine
            reset_action_engine()
            assert get_action_engine() is not engine
        finally:
            reset_action_engine()


# ═══════════════════════════════════════════════════════════════
#  D-2 自愈动作库（回写#244）
# ═══════════════════════════════════════════════════════════════

class TestSwitchDataSource:
    """行情中断→切换备用行情源（可回滚）。"""

    def test_dry_run_precheck_without_switch(self) -> None:
        state: List[str] = ["main"]

        def switch() -> None:
            state[0] = "backup"

        def restore() -> None:
            state[0] = "main"

        out = SwitchDataSourceExecutor().execute(
            ctx={},
            params={"switch_fn": switch, "restore_fn": restore,
                    "target_source": "main", "backup_source": "backup"},
            dry_run=True,
        )
        assert out["status"] == STATUS_EXECUTED
        assert state == ["main"]  # 演练不真实切换

    def test_execute_switches_and_undo_restores(self) -> None:
        state: List[str] = ["main"]

        def switch() -> None:
            state[0] = "backup"

        def restore() -> None:
            state[0] = "main"

        ex = SwitchDataSourceExecutor()
        out = ex.execute(
            ctx={},
            params={"switch_fn": switch, "restore_fn": restore,
                    "target_source": "main", "backup_source": "backup"},
        )
        assert out["status"] == STATUS_EXECUTED
        assert state == ["backup"]
        assert ex.undo(out["rollback_info"]) is True
        assert state == ["main"]

    def test_missing_switch_fn_not_supported(self) -> None:
        out = SwitchDataSourceExecutor().execute(
            ctx={}, params={"target_source": "main"}
        )
        assert out["status"] == STATUS_NOT_SUPPORTED  # KP-M02

    def test_undo_without_restore_false(self) -> None:
        out = SwitchDataSourceExecutor().execute(
            ctx={}, params={"switch_fn": lambda: None, "target_source": "main"}
        )
        assert out["status"] == STATUS_EXECUTED
        assert SwitchDataSourceExecutor().undo(out["rollback_info"]) is False

    def test_scenario_trigger_action_restore_audit(self) -> None:
        """完整场景：行情中断告警 → auto 切源 → 审计 → undo 恢复。"""
        state: List[str] = ["main"]
        audit = _AuditRecorder()

        def switch() -> None:
            state[0] = "backup"

        def restore() -> None:
            state[0] = "main"

        engine = ActionEngine(mode="auto", audit_log=audit)
        engine.register_rule(ActionRule(
            rule_id="ds1",
            condition={"categories": ["market_staleness"], "levels": ["ERROR"]},
            action={"type": "switch_data_source",
                    "params": {"switch_fn": switch, "restore_fn": restore,
                               "target_source": "main", "backup_source": "backup"}},
        ))
        results = engine.handle_alert(
            _alert(deduplication_key="watchdog:staleness-d2")
        )
        assert results[0].status == STATUS_EXECUTED
        assert state == ["backup"]
        assert any("executed" in r["action"] for r in audit.records)  # 审计
        assert engine.rollback(results[0].action_id, actor="ops") is True
        assert state == ["main"]
        assert any("rollback" in r["action"] for r in audit.records)


class TestStopSignalGeneration:
    """数据质量异常→停止信号生成（可回滚）。"""

    def test_dry_run_precheck(self) -> None:
        stopped: List[str] = []

        def stop() -> None:
            stopped.append("engine")

        def resume() -> None:
            stopped.clear()

        out = StopSignalGenerationExecutor().execute(
            ctx={}, params={"stop_fn": stop, "resume_fn": resume, "target": "engine"},
            dry_run=True,
        )
        assert out["status"] == STATUS_EXECUTED
        assert stopped == []

    def test_execute_stops_and_undo_resumes(self) -> None:
        stopped: List[str] = []

        def stop() -> None:
            stopped.append("engine")

        def resume() -> None:
            stopped.clear()

        ex = StopSignalGenerationExecutor()
        out = ex.execute(
            ctx={}, params={"stop_fn": stop, "resume_fn": resume, "target": "engine"}
        )
        assert out["status"] == STATUS_EXECUTED
        assert stopped == ["engine"]
        assert ex.undo(out["rollback_info"]) is True
        assert stopped == []

    def test_missing_stop_fn_not_supported(self) -> None:
        out = StopSignalGenerationExecutor().execute(ctx={}, params={})
        assert out["status"] == STATUS_NOT_SUPPORTED

    def test_scenario_shadow_first(self) -> None:
        """验收：先 shadow 演练（只读建议）再放开 auto。"""
        stopped: List[str] = []

        def stop() -> None:
            stopped.append("engine")

        def resume() -> None:
            stopped.clear()

        engine = ActionEngine(mode="shadow")
        engine.register_rule(ActionRule(
            rule_id="ss1",
            condition={"categories": ["data_quality"]},
            action={"type": "stop_signal_generation",
                    "params": {"stop_fn": stop, "resume_fn": resume,
                               "target": "engine"}},
        ))
        results = engine.handle_alert(
            _alert(category="data_quality", deduplication_key="dq:bad")
        )
        assert results[0].status == STATUS_PROPOSED  # shadow 只读
        assert stopped == []


class TestRollbackDataVersion:
    """数据质量异常→回滚数据版本（可回滚）。"""

    def test_dry_run_precheck(self) -> None:
        version: List[str] = ["v3"]

        def rollback_fn() -> None:
            version[0] = "v2"

        def restore_fn() -> None:
            version[0] = "v3"

        out = RollbackDataVersionExecutor().execute(
            ctx={},
            params={"rollback_fn": rollback_fn, "restore_fn": restore_fn,
                    "current_version": "v3", "target_version": "v2"},
            dry_run=True,
        )
        assert out["status"] == STATUS_EXECUTED
        assert version == ["v3"]  # 演练不真实回滚

    def test_execute_rollback_and_undo_restore(self) -> None:
        version: List[str] = ["v3"]

        def rollback_fn() -> None:
            version[0] = "v2"

        def restore_fn() -> None:
            version[0] = "v3"

        ex = RollbackDataVersionExecutor()
        out = ex.execute(
            ctx={},
            params={"rollback_fn": rollback_fn, "restore_fn": restore_fn,
                    "current_version": "v3", "target_version": "v2"},
        )
        assert out["status"] == STATUS_EXECUTED
        assert version == ["v2"]
        assert ex.undo(out["rollback_info"]) is True
        assert version == ["v3"]

    def test_missing_rollback_fn_not_supported(self) -> None:
        out = RollbackDataVersionExecutor().execute(
            ctx={}, params={"current_version": "v3"}
        )
        assert out["status"] == STATUS_NOT_SUPPORTED

    def test_scenario_trigger_action_restore_audit(self) -> None:
        """完整场景：数据质量异常 → auto 回滚版本 → 审计 → undo 恢复。"""
        version: List[str] = ["v3"]
        audit = _AuditRecorder()

        def rollback_fn() -> None:
            version[0] = "v2"

        def restore_fn() -> None:
            version[0] = "v3"

        engine = ActionEngine(mode="auto", audit_log=audit)
        engine.register_rule(ActionRule(
            rule_id="rv1",
            condition={"categories": ["data_quality"]},
            action={"type": "rollback_data_version",
                    "params": {"rollback_fn": rollback_fn, "restore_fn": restore_fn,
                               "current_version": "v3", "target_version": "v2",
                               "target": "factor_data"}},
        ))
        results = engine.handle_alert(
            _alert(category="data_quality", deduplication_key="dq:corrupt")
        )
        assert results[0].status == STATUS_EXECUTED
        assert version == ["v2"]
        assert any("executed" in r["action"] for r in audit.records)
        assert engine.rollback(results[0].action_id, actor="ops") is True
        assert version == ["v3"]


class TestNotifyTrader:
    """拒单率飙升→通知交易员（不可撤销）。"""

    def test_execute_sends_via_manager(self) -> None:
        sink = _FakeAlertSink()
        engine = ActionEngine(mode="auto")
        engine._alert_manager = sink
        engine.register_rule(ActionRule(
            rule_id="nt1",
            condition={"metrics": [{"name": "order_reject_rate", "op": ">",
                                    "value": 0.01}]},
            action={"type": "notify_trader",
                    "params": {"message": "拒单率飙升", "dedup_key": "exec:reject"}},
        ))
        results = engine.evaluate(metrics={"order_reject_rate": 0.05})
        assert results[0].status == STATUS_EXECUTED
        assert results[0].details["channels"] == 2

    def test_undo_false(self) -> None:
        assert NotifyTraderExecutor().undo({"any": 1}) is False

    def test_bad_level_failed(self) -> None:
        out = NotifyTraderExecutor().execute(
            ctx={}, params={"level": "PANIC"}, dry_run=False
        )
        assert out["status"] == STATUS_FAILED

    def test_alert_channel_failure_failed(self) -> None:
        """投递通道异常 → 如实报告 failed（不假成功）。"""

        class _RaisingSink:
            def alert(self, **kw: Any) -> int:
                raise RuntimeError("channel down")

        engine = ActionEngine(mode="auto")
        engine._alert_manager = _RaisingSink()
        engine.register_rule(ActionRule(
            rule_id="nt2",
            condition={"categories": ["execution"]},
            action={"type": "notify_trader", "params": {"message": "拒单飙升"}},
        ))
        results = engine.handle_alert(
            _alert(category="execution", deduplication_key="exec:reject")
        )
        assert results[0].status == STATUS_FAILED
        assert "投递失败" in results[0].message

    def test_registered_in_engine(self) -> None:
        assert get_action_executor("notify_trader") is NotifyTraderExecutor
        assert get_action_executor("switch_data_source") is SwitchDataSourceExecutor
        assert get_action_executor("stop_signal_generation") is StopSignalGenerationExecutor
        assert get_action_executor("rollback_data_version") is RollbackDataVersionExecutor
