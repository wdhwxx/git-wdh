# 文件名：tests/unit/test_param_tuner.py
"""
D-5 参数动态调整测试（回写#247）
==================================
覆盖：ParamTuner 构造防御；propose 参数校验（非空/有限/白名单/无通配/actor）；
未批准不生效（propose 只登记建议不触碰生效参数）；approve 人工确认生效
（apply_fn 注入应用/缺注入不假成功/参数合并/effective 更新）；模型版本
registry 衔接（registry_fn 登记回填版本/缺注入 registry_version=None/
登记失败留痕继续不阻断生效）；reject 否决；auto 直接生效；rollback 回滚恢复
params_before（未知/非 applied/缺 rollback_fn False）；留痕审计（history JSON
可序列化/上限裁剪/审计写入/审计失败不阻断/summary）；action_engine 集成
（ApplyParamChangeExecutor：注册/缺注入 not_supported/dry-run 预检/
执行与回滚/审批流全链路可审计）。
"""

import json
from typing import Any, Dict, List, Optional

import pytest

from myquant.monitoring.action_engine import (
    STATUS_EXECUTED,
    STATUS_NOT_SUPPORTED,
    ActionEngine,
    ActionRule,
    ApplyParamChangeExecutor,
    get_action_executor,
)
from myquant.strategies.param_tuner import (
    PARAM_STATUS_APPLIED,
    PARAM_STATUS_PROPOSED,
    PARAM_STATUS_REJECTED,
    PARAM_STATUS_ROLLED_BACK,
    ParamTuner,
)

# ── 测试辅助 ──────────────────────────────────────────────────

class _AuditRecorder:
    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def append(self, kind: str, action: str, actor: str,
               detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rec = {"kind": kind, "action": action, "actor": actor,
               "detail": dict(detail or {})}
        self.records.append(rec)
        return rec


class _FakeStrategy:
    """模拟 StrategyBase 参数容器（params dict）。"""

    def __init__(self, **params: Any) -> None:
        self.params: Dict[str, Any] = dict(params)


# ── 构造防御 ──────────────────────────────────────────────────

class TestConstructor:
    def test_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            ParamTuner(mode="instant")

    def test_history_limit_rejected(self) -> None:
        with pytest.raises(ValueError):
            ParamTuner(max_history=0)


# ── propose：校验与未批准不生效 ──────────────────────────────

class TestPropose:
    def test_empty_strategy_rejected(self) -> None:
        tuner = ParamTuner()
        with pytest.raises(ValueError):
            tuner.propose("  ", {"fast": 10}, actor="system")

    def test_empty_changes_rejected(self) -> None:
        tuner = ParamTuner()
        with pytest.raises(ValueError):
            tuner.propose("ma_strategy", {}, actor="system")

    def test_non_finite_value_rejected(self) -> None:
        tuner = ParamTuner()
        with pytest.raises(ValueError):
            tuner.propose("ma_strategy", {"fast": float("nan")}, actor="system")

    def test_wildcard_key_rejected(self) -> None:
        tuner = ParamTuner()
        with pytest.raises(ValueError):
            tuner.propose("ma_strategy", {"*": 1}, actor="system")

    def test_whitelist_enforced(self) -> None:
        tuner = ParamTuner(allowed_keys=["fast"])
        with pytest.raises(ValueError):
            tuner.propose("ma_strategy", {"slow": 30}, actor="system")
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        assert prop.param_changes == {"fast": 10}

    def test_empty_whitelist_rejects_all(self) -> None:
        tuner = ParamTuner(allowed_keys=[])
        with pytest.raises(ValueError):
            tuner.propose("ma_strategy", {"fast": 10}, actor="system")

    def test_actor_required(self) -> None:
        tuner = ParamTuner()
        with pytest.raises(ValueError):
            tuner.propose("ma_strategy", {"fast": 10}, actor="  ")

    def test_propose_not_applied_yet(self) -> None:
        """验收：未批准不生效——propose 后生效参数仍为 None。"""
        tuner = ParamTuner()
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system",
                             reason="监控反馈")
        assert prop.status == PARAM_STATUS_PROPOSED
        assert tuner.effective_params("ma_strategy") is None  # 未批准不生效

    def test_params_after_merge(self) -> None:
        tuner = ParamTuner()
        prop = tuner.propose(
            "ma_strategy", {"fast": 10}, actor="system",
            base_params={"fast": 5, "slow": 30},
        )
        assert prop.params_before == {"fast": 5, "slow": 30}
        assert prop.params_after == {"fast": 10, "slow": 30}  # 局部覆盖

    def test_cold_start_without_base(self) -> None:
        tuner = ParamTuner()
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        assert prop.params_before == {}
        assert prop.params_after == {"fast": 10}


# ── approve：人工确认生效 + 模型版本 registry 衔接 ───────────

class TestApprove:
    def test_missing_apply_fn_not_supported(self) -> None:
        """缺注入 apply_fn 不假成功（KP-M02）。"""
        tuner = ParamTuner()
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        with pytest.raises(ValueError):
            tuner.approve(prop.proposal_id, actor="risk_owner")

    def test_approve_applies_and_tracks(self) -> None:
        strategy = _FakeStrategy(fast=5, slow=30)
        applied: List[Dict[str, Any]] = []
        tuner = ParamTuner(
            apply_fn=lambda p: (strategy.params.update(p["param_changes"]),
                                applied.append(p)),
        )
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        approved = tuner.approve(prop.proposal_id, actor="risk_owner",
                                 reason="确认")
        assert approved.status == PARAM_STATUS_APPLIED
        assert strategy.params["fast"] == 10   # 生效
        assert applied and applied[0]["param_changes"] == {"fast": 10}
        assert tuner.effective_params("ma_strategy") == {"fast": 10}

    def test_approve_validation(self) -> None:
        tuner = ParamTuner(apply_fn=lambda p: None)
        with pytest.raises(ValueError):
            tuner.approve("", actor="risk_owner")
        with pytest.raises(ValueError):
            tuner.approve("no_such", actor="risk_owner")
        with pytest.raises(ValueError):
            tuner.approve("x", actor="  ")
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        tuner.approve(prop.proposal_id, actor="risk_owner")
        with pytest.raises(ValueError):
            tuner.approve(prop.proposal_id, actor="risk_owner")  # 已 applied

    def test_apply_failure_propagates(self) -> None:
        tuner = ParamTuner(apply_fn=lambda p: (_ for _ in ()).throw(
            RuntimeError("apply down")))
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        with pytest.raises(ValueError):
            tuner.approve(prop.proposal_id, actor="risk_owner")
        assert tuner.effective_params("ma_strategy") is None  # 未生效

    def test_registry_linkage_version_recorded(self) -> None:
        """验收：模型版本管理（已有 registry）衔接。"""
        versions: List[Dict[str, Any]] = []
        tuner = ParamTuner(
            apply_fn=lambda p: None,
            registry_fn=lambda p: versions.append(p) or {"version": 7},
        )
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        approved = tuner.approve(prop.proposal_id, actor="risk_owner")
        assert approved.registry_version == 7
        assert versions and versions[0]["param_changes"] == {"fast": 10}

    def test_registry_fn_absent_skipped(self) -> None:
        tuner = ParamTuner(apply_fn=lambda p: None)
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        approved = tuner.approve(prop.proposal_id, actor="risk_owner")
        assert approved.registry_version is None  # 未注入不编造

    def test_registry_failure_does_not_block_apply(self) -> None:
        tuner = ParamTuner(
            apply_fn=lambda p: None,
            registry_fn=lambda p: (_ for _ in ()).throw(RuntimeError("reg down")),
        )
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        approved = tuner.approve(prop.proposal_id, actor="risk_owner")
        assert approved.status == PARAM_STATUS_APPLIED  # 生效不被登记失败阻断


# ── reject / auto / rollback ─────────────────────────────────

class TestRejectAutoRollback:
    def test_reject_no_effect(self) -> None:
        tuner = ParamTuner(apply_fn=lambda p: None)
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        rejected = tuner.reject(prop.proposal_id, actor="risk_owner", reason="否决")
        assert rejected.status == PARAM_STATUS_REJECTED
        assert tuner.effective_params("ma_strategy") is None

    def test_reject_validation(self) -> None:
        tuner = ParamTuner()
        with pytest.raises(ValueError):
            tuner.reject("no_such", actor="risk_owner")

    def test_auto_applies_immediately(self) -> None:
        strategy = _FakeStrategy(fast=5)
        tuner = ParamTuner(mode="auto",
                           apply_fn=lambda p: strategy.params.update(
                               p["param_changes"]))
        prop = tuner.apply_auto("ma_strategy", {"fast": 10}, actor="system",
                                base_params={"fast": 5})
        assert prop.status == PARAM_STATUS_APPLIED
        assert strategy.params["fast"] == 10
        assert tuner.effective_params("ma_strategy") == {"fast": 10}

    def test_rollback_restores_previous(self) -> None:
        """验收：参数调整全流程留痕 + 生效回滚。"""
        strategy = _FakeStrategy(fast=5, slow=30)
        tuner = ParamTuner(
            apply_fn=lambda p: strategy.params.update(p["param_changes"]),
            rollback_fn=lambda p: strategy.params.update(p["params_before"]),
        )
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system",
                             base_params={"fast": 5, "slow": 30})
        tuner.approve(prop.proposal_id, actor="risk_owner")
        assert strategy.params["fast"] == 10
        assert tuner.rollback(prop.proposal_id, actor="ops", reason="回滚") is True
        assert strategy.params["fast"] == 5   # 恢复旧参数
        assert tuner.effective_params("ma_strategy") == {"fast": 5, "slow": 30}
        assert prop.status == PARAM_STATUS_ROLLED_BACK

    def test_rollback_unknown_or_not_applied_or_no_fn_false(self) -> None:
        tuner = ParamTuner()
        assert tuner.rollback("no_such", actor="ops") is False
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        assert tuner.rollback(prop.proposal_id, actor="ops") is False  # 非 applied
        tuner2 = ParamTuner(apply_fn=lambda p: None)  # 缺 rollback_fn
        p2 = tuner2.propose("ma_strategy", {"fast": 10}, actor="system")
        tuner2.approve(p2.proposal_id, actor="risk_owner")
        assert tuner2.rollback(p2.proposal_id, actor="ops") is False

    def test_rollback_validation(self) -> None:
        tuner = ParamTuner()
        with pytest.raises(ValueError):
            tuner.rollback("", actor="ops")
        with pytest.raises(ValueError):
            tuner.rollback("x", actor="  ")


# ── 留痕 / 审计 / 观测 ────────────────────────────────────────

class TestTrail:
    def test_history_json_serializable(self) -> None:
        tuner = ParamTuner(apply_fn=lambda p: None)
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        tuner.approve(prop.proposal_id, actor="risk_owner")
        json.dumps(tuner.history())

    def test_history_limit_validation(self) -> None:
        tuner = ParamTuner()
        with pytest.raises(ValueError):
            tuner.history(limit=0)

    def test_history_cap(self) -> None:
        tuner = ParamTuner(max_history=3)
        for i in range(6):
            tuner.propose("ma_strategy", {"fast": i}, actor="system")
        assert len(tuner.history()) == 3

    def test_audit_records_written(self) -> None:
        audit = _AuditRecorder()
        tuner = ParamTuner(apply_fn=lambda p: None, audit_log=audit)
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        tuner.approve(prop.proposal_id, actor="risk_owner")
        assert any(r["kind"] == "param_tuner" for r in audit.records)
        assert any("approved" in r["action"] for r in audit.records)

    def test_audit_failure_not_blocking(self) -> None:
        class _BrokenAudit:
            def append(self, *a: Any, **kw: Any) -> Dict[str, Any]:
                raise RuntimeError("audit down")

        tuner = ParamTuner(apply_fn=lambda p: None, audit_log=_BrokenAudit())
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        approved = tuner.approve(prop.proposal_id, actor="risk_owner")
        assert approved.status == PARAM_STATUS_APPLIED

    def test_summary(self) -> None:
        tuner = ParamTuner(apply_fn=lambda p: None)
        tuner.propose("ma_strategy", {"fast": 10}, actor="system")
        s = tuner.summary()
        assert s["mode"] == "confirm"
        assert s["proposed_total"] == 1
        assert s["strategies_tracked"] == 0  # 未批准未跟踪


# ── action_engine 集成（执行器接线）──────────────────────────

class TestActionEngineIntegration:
    def test_executor_registered(self) -> None:
        assert get_action_executor("apply_param_change") is ApplyParamChangeExecutor

    def test_missing_injection_not_supported(self) -> None:
        out = ApplyParamChangeExecutor().execute(
            ctx={}, params={"strategy_name": "ma_strategy",
                            "param_changes": {"fast": 10}}
        )
        assert out["status"] == STATUS_NOT_SUPPORTED  # KP-M02

    def test_dry_run_precheck(self) -> None:
        called: List[str] = []
        out = ApplyParamChangeExecutor().execute(
            ctx={},
            params={"apply_fn": lambda p: called.append("x"),
                    "strategy_name": "ma_strategy",
                    "param_changes": {"fast": 10}},
            dry_run=True,
        )
        assert out["status"] == STATUS_EXECUTED
        assert called == []  # 演练不真实执行

    def test_execute_and_rollback(self) -> None:
        strategy = _FakeStrategy(fast=5)
        audit = _AuditRecorder()
        engine = ActionEngine(mode="auto", audit_log=audit)
        engine.register_rule(ActionRule(
            rule_id="pt1",
            condition={"metrics": [{"name": "drawdown_pct", "op": ">",
                                    "value": 15.0}]},
            action={"type": "apply_param_change",
                    "params": {
                        "apply_fn": lambda p: strategy.params.update(
                            p["param_changes"]),
                        "rollback_fn": lambda: strategy.params.update(
                            {"fast": 5}),
                        "strategy_name": "ma_strategy",
                        "param_changes": {"fast": 10},
                        "reason": "监控反馈：回撤升高调参"}},
        ))
        results = engine.evaluate(metrics={"drawdown_pct": 18.0})
        assert results[0].status == STATUS_EXECUTED
        assert strategy.params["fast"] == 10
        assert any("executed" in r["action"] for r in audit.records)
        assert engine.rollback(results[0].action_id, actor="ops") is True
        assert strategy.params["fast"] == 5  # 回滚恢复旧参数

    def test_scenario_trigger_action_audit(self) -> None:
        """验收：自动提议→人工确认→生效回滚全链路留痕、未批准不生效。"""
        strategy = _FakeStrategy(fast=5, slow=30)
        audit = _AuditRecorder()
        tuner = ParamTuner(
            apply_fn=lambda p: strategy.params.update(p["param_changes"]),
            rollback_fn=lambda p: strategy.params.update(p["params_before"]),
            audit_log=audit,
        )
        # 自动提议（监控反馈 → 参数建议）
        prop = tuner.propose("ma_strategy", {"fast": 10}, actor="system",
                             reason="监控反馈：环境匹配下降",
                             base_params={"fast": 5, "slow": 30})
        assert tuner.effective_params("ma_strategy") is None  # 未批准不生效
        # 人工确认 → 生效
        tuner.approve(prop.proposal_id, actor="risk_owner", reason="确认")
        assert strategy.params["fast"] == 10
        assert tuner.effective_params("ma_strategy") == {"fast": 10, "slow": 30}
        # 回滚 → 恢复
        assert tuner.rollback(prop.proposal_id, actor="ops", reason="回滚") is True
        assert strategy.params["fast"] == 5
        # 全流程留痕可审计（同一条提议随生命周期变更状态；分步动作在审计记录）
        trail = tuner.history()
        assert len(trail) == 1 and trail[0]["status"] == PARAM_STATUS_ROLLED_BACK
        audit_actions = {r["action"] for r in audit.records}
        assert {"param:proposed", "param:approved", "param:rolled_back"} <= audit_actions
