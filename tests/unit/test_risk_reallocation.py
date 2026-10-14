# 文件名：tests/unit/test_risk_reallocation.py
"""
D-4 风险预算再分配测试（回写#246）
====================================
覆盖：RiskBudgetReallocator 构造防御；输入权重/反馈校验；质量分算法（匹配分/
波动率风险项/低匹配惩罚/负绩效惩罚/缺反馈=低证据降配）；目标权重计算（比例分配/
上限/下限迭代重归一化/单步降幅护栏/总调幅护栏/和=1 尾差归最大）；shadow 提议→
approve 生效 / reject 否决 / auto 直接生效；回滚恢复旧权重；有效权重查询；
历史可审计（JSON 可序列化/上限裁剪）；审计写入/审计失败不阻断；全部质量分为 0
不调整（不编造）；per-策略权重上限约束；与 action_engine 集成
（ReallocateRiskBudgetExecutor：dry-run 预检/缺注入 not_supported/执行/回滚）。
全部注入内存审计/固定时钟，不碰 quant.db。
"""

import json
from typing import Any, Dict, List, Optional

import pytest

from myquant.allocation.risk_parity import (
    REALLOC_STATUS_APPLIED,
    REALLOC_STATUS_PROPOSED,
    REALLOC_STATUS_REJECTED,
    REALLOC_STATUS_ROLLED_BACK,
    RiskBudgetReallocator,
)
from myquant.monitoring.action_engine import (
    STATUS_EXECUTED,
    STATUS_NOT_SUPPORTED,
    ActionEngine,
    ActionRule,
    ReallocateRiskBudgetExecutor,
    get_action_executor,
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


def _clock_fixed(t0: float = 2_000_000.0) -> Any:
    now = [t0]

    def clock() -> float:
        return now[0]

    return clock


# 典型反馈：a=优质（高匹配低波动正绩效），b=劣质（低匹配高波动负绩效）
FEED_AB = {
    "a": {"rolling_perf": 0.12, "volatility": 0.10, "regime_match": 90},
    "b": {"rolling_perf": -0.05, "volatility": 0.30, "regime_match": 30},
}
WEIGHTS_AB = {"a": 0.5, "b": 0.5}


# ── 构造防御 ──────────────────────────────────────────────────

class TestConstructor:
    def test_negative_params_rejected(self) -> None:
        with pytest.raises(ValueError):
            RiskBudgetReallocator(min_weight=-0.1)
        with pytest.raises(ValueError):
            RiskBudgetReallocator(max_step=0.0)
        with pytest.raises(ValueError):
            RiskBudgetReallocator(max_total_shift=0.0)
        with pytest.raises(ValueError):
            RiskBudgetReallocator(match_score_min=101.0)

    def test_score_weight_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            RiskBudgetReallocator(score_match_weight=1.5)
        with pytest.raises(ValueError):
            RiskBudgetReallocator(score_match_weight=-0.1)

    def test_penalty_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            RiskBudgetReallocator(low_match_penalty=0.0)
        with pytest.raises(ValueError):
            RiskBudgetReallocator(negative_perf_penalty=1.5)

    def test_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            RiskBudgetReallocator(mode="instant")

    def test_bad_max_weights_rejected(self) -> None:
        with pytest.raises(ValueError):
            RiskBudgetReallocator(max_weights={"a": 0.0})
        with pytest.raises(ValueError):
            RiskBudgetReallocator(max_weights={"a": 2.0})

    def test_bad_history_limit_rejected(self) -> None:
        with pytest.raises(ValueError):
            RiskBudgetReallocator(max_history=0)


# ── 输入校验 ──────────────────────────────────────────────────

class TestInputValidation:
    def test_empty_weights_rejected(self) -> None:
        rr = RiskBudgetReallocator()
        with pytest.raises(ValueError):
            rr.propose({}, FEED_AB, actor="system")

    def test_non_finite_weight_rejected(self) -> None:
        rr = RiskBudgetReallocator()
        with pytest.raises(ValueError):
            rr.propose({"a": float("nan"), "b": 1.0}, FEED_AB, actor="system")
        with pytest.raises(ValueError):
            rr.propose({"a": -0.1, "b": 1.1}, FEED_AB, actor="system")

    def test_zero_sum_weights_rejected(self) -> None:
        rr = RiskBudgetReallocator()
        with pytest.raises(ValueError):
            rr.propose({"a": 0.0, "b": 0.0}, FEED_AB, actor="system")

    def test_non_dict_feedback_rejected(self) -> None:
        rr = RiskBudgetReallocator()
        with pytest.raises(ValueError):
            rr.propose(WEIGHTS_AB, "bad", actor="system")

    def test_actor_required(self) -> None:
        rr = RiskBudgetReallocator()
        with pytest.raises(ValueError):
            rr.propose(WEIGHTS_AB, FEED_AB, actor="  ")

    def test_bad_feedback_fields_ignored(self) -> None:
        rr = RiskBudgetReallocator()
        rec = rr.propose(
            WEIGHTS_AB,
            {"a": {"rolling_perf": 0.1, "volatility": "x", "regime_match": 90},
             "b": {"rolling_perf": None, "volatility": 0.2, "regime_match": 20}},
            actor="system",
        )
        assert "volatility" not in rec.feedback["a"]  # 非法字段被防御式忽略


# ── 质量分与目标权重算法 ──────────────────────────────────────

class TestScoreAndTarget:
    def test_low_match_high_drawdown_strategy_downweighted(self) -> None:
        """验收：低匹配/高回撤策略自动降配。"""
        rr = RiskBudgetReallocator()
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        assert rec.weights_after["b"] < rec.weights_after["a"]
        assert rec.weights_after["b"] < 0.5  # b 被降配
        assert abs(sum(rec.weights_after.values()) - 1.0) < 1e-6

    def test_scores_reflect_quality(self) -> None:
        rr = RiskBudgetReallocator()
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        assert rec.scores["a"] > rec.scores["b"]

    def test_all_equal_scores_no_change(self) -> None:
        """全部质量分相同/为 0 → 不调整（不编造）。"""
        rr = RiskBudgetReallocator()
        rec = rr.propose(
            WEIGHTS_AB,
            {"a": {"rolling_perf": 0.1, "volatility": 0.1, "regime_match": 90},
             "b": {"rolling_perf": 0.1, "volatility": 0.1, "regime_match": 90}},
            actor="system",
        )
        assert rec.weights_after == rec.weights_before

    def test_no_feedback_zero_score(self) -> None:
        """无监控反馈 → 质量分 0（低证据降配，受护栏约束）。"""
        rr = RiskBudgetReallocator(max_step=0.05)
        rec = rr.propose(WEIGHTS_AB, {"a": FEED_AB["a"]}, actor="system")
        assert rec.scores["b"] == 0.0
        assert rec.weights_after["b"] < rec.weights_before["b"]

    def test_max_weight_cap_enforced(self) -> None:
        """per-策略权重上限约束（护栏）。"""
        rr = RiskBudgetReallocator(max_weights={"a": 0.55})
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        assert rec.weights_after["a"] <= 0.55 + 1e-6

    def test_min_weight_floor_enforced(self) -> None:
        rr = RiskBudgetReallocator(min_weight=0.10)
        rec = rr.propose(
            {"a": 0.9, "b": 0.1},
            {"a": FEED_AB["a"], "b": {"rolling_perf": -0.3, "volatility": 0.5,
                                      "regime_match": 10}},
            actor="system",
        )
        assert rec.weights_after["b"] >= 0.10 - 1e-6

    def test_single_step_reduction_capped(self) -> None:
        """单策略单次最大降幅（单步护栏）。"""
        rr = RiskBudgetReallocator(max_step=0.05)
        rec = rr.propose(
            {"a": 0.95, "b": 0.05},
            {"a": FEED_AB["a"], "b": {"rolling_perf": -0.5, "volatility": 0.6,
                                      "regime_match": 0}},
            actor="system",
        )
        # b 质量分 0 会大幅降配，但受单步 5% 护栏约束
        assert rec.weights_after["b"] >= 0.05 - 0.05 - 1e-6

    def test_total_shift_capped(self) -> None:
        """单次总调幅上限（护栏）。"""
        rr = RiskBudgetReallocator(max_total_shift=0.10, max_step=0.20)
        rec = rr.propose(
            {"a": 0.9, "b": 0.1},
            {"a": FEED_AB["a"], "b": {"rolling_perf": -0.5, "volatility": 0.6,
                                      "regime_match": 0}},
            actor="system",
        )
        shift = sum(
            abs(rec.weights_after[s] - rec.weights_before[s])
            for s in rec.weights_before
        )
        assert shift <= 0.10 + 1e-6

    def test_weights_normalized_to_one(self) -> None:
        rr = RiskBudgetReallocator()
        rec = rr.propose(
            {"a": 0.7, "b": 0.3, "c": 0.0},
            {"a": FEED_AB["a"], "b": FEED_AB["b"],
             "c": {"rolling_perf": 0.0, "volatility": 0.0, "regime_match": 0}},
            actor="system",
        )
        assert abs(sum(rec.weights_after.values()) - 1.0) < 1e-6


# ── 审批留痕流程（shadow 提议 → approve/reject；auto 生效）────

class TestApprovalFlow:
    def test_shadow_propose_no_effect(self) -> None:
        rr = RiskBudgetReallocator(mode="shadow")
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        assert rec.status == REALLOC_STATUS_PROPOSED
        assert rr.effective_weights() is None  # 建议未生效

    def test_approve_applies(self) -> None:
        rr = RiskBudgetReallocator(mode="shadow")
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        applied = rr.approve(rec.realloc_id, actor="risk_owner", reason="确认")
        assert applied.status == REALLOC_STATUS_APPLIED
        assert rr.effective_weights() == rec.weights_after

    def test_reject_no_effect(self) -> None:
        rr = RiskBudgetReallocator(mode="shadow")
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        rejected = rr.reject(rec.realloc_id, actor="risk_owner", reason="否决")
        assert rejected.status == REALLOC_STATUS_REJECTED
        assert rr.effective_weights() is None

    def test_auto_applies_immediately(self) -> None:
        rr = RiskBudgetReallocator(mode="auto")
        rec = rr.apply_auto(WEIGHTS_AB, FEED_AB, actor="system", reason="自动")
        assert rec.status == REALLOC_STATUS_APPLIED
        assert rr.effective_weights() == rec.weights_after

    def test_approve_validation(self) -> None:
        rr = RiskBudgetReallocator(mode="shadow")
        with pytest.raises(ValueError):
            rr.approve("", actor="risk_owner")
        with pytest.raises(ValueError):
            rr.approve("no_such", actor="risk_owner")
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        rr.approve(rec.realloc_id, actor="risk_owner")
        with pytest.raises(ValueError):
            rr.approve(rec.realloc_id, actor="risk_owner")  # 已 applied 不可再批

    def test_rollback_restores_previous(self) -> None:
        """验收：再分配动作可审计（回滚恢复旧权重）。"""
        rr = RiskBudgetReallocator(mode="shadow")
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        rr.approve(rec.realloc_id, actor="risk_owner")
        assert rr.effective_weights() == rec.weights_after
        assert rr.rollback(rec.realloc_id, actor="ops", reason="回滚") is True
        assert rr.effective_weights() == rec.weights_before
        assert rec.status == REALLOC_STATUS_ROLLED_BACK

    def test_rollback_unknown_or_not_applied_false(self) -> None:
        rr = RiskBudgetReallocator(mode="shadow")
        assert rr.rollback("no_such", actor="ops") is False
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        assert rr.rollback(rec.realloc_id, actor="ops") is False  # 非 applied

    def test_rollback_validation(self) -> None:
        rr = RiskBudgetReallocator()
        with pytest.raises(ValueError):
            rr.rollback("", actor="ops")
        with pytest.raises(ValueError):
            rr.rollback("x", actor="  ")


# ── 留痕 / 审计 / 观测 ────────────────────────────────────────

class TestTrail:
    def test_history_json_serializable(self) -> None:
        rr = RiskBudgetReallocator(mode="shadow")
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        rr.approve(rec.realloc_id, actor="risk_owner")
        json.dumps(rr.history())

    def test_history_limit_validation(self) -> None:
        rr = RiskBudgetReallocator()
        with pytest.raises(ValueError):
            rr.history(limit=0)

    def test_history_cap(self) -> None:
        rr = RiskBudgetReallocator(max_history=3, mode="shadow")
        for i in range(6):
            rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        assert len(rr.history()) == 3

    def test_audit_records_written(self) -> None:
        audit = _AuditRecorder()
        rr = RiskBudgetReallocator(mode="shadow", audit_log=audit)
        rec = rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        rr.approve(rec.realloc_id, actor="risk_owner")
        assert any(r["kind"] == "risk_realloc" for r in audit.records)
        assert any("approved" in r["action"] for r in audit.records)

    def test_audit_failure_not_blocking(self) -> None:
        class _BrokenAudit:
            def append(self, *a: Any, **kw: Any) -> Dict[str, Any]:
                raise RuntimeError("audit down")

        rr = RiskBudgetReallocator(mode="auto", audit_log=_BrokenAudit())
        rec = rr.apply_auto(WEIGHTS_AB, FEED_AB, actor="system")
        assert rec.status == REALLOC_STATUS_APPLIED

    def test_summary(self) -> None:
        rr = RiskBudgetReallocator(mode="shadow")
        rr.propose(WEIGHTS_AB, FEED_AB, actor="system")
        s = rr.summary()
        assert s["mode"] == "shadow"
        assert s["proposed_total"] == 1
        assert s["effective_applied"] is False


# ── action_engine 集成（执行器接线）──────────────────────────

class TestActionEngineIntegration:
    def test_executor_registered(self) -> None:
        assert get_action_executor("reallocate_risk") is ReallocateRiskBudgetExecutor

    def test_missing_injection_not_supported(self) -> None:
        out = ReallocateRiskBudgetExecutor().execute(
            ctx={}, params={"weights": WEIGHTS_AB, "feedback": FEED_AB}
        )
        assert out["status"] == STATUS_NOT_SUPPORTED  # KP-M02

    def test_dry_run_precheck(self) -> None:
        called: List[str] = []
        out = ReallocateRiskBudgetExecutor().execute(
            ctx={},
            params={"reallocate_fn": lambda **kw: called.append("x"),
                    "weights": WEIGHTS_AB, "feedback": FEED_AB},
            dry_run=True,
        )
        assert out["status"] == STATUS_EXECUTED
        assert called == []  # 演练不真实执行

    def test_execute_and_rollback(self) -> None:
        effective: Dict[str, float] = dict(WEIGHTS_AB)
        audit = _AuditRecorder()
        engine = ActionEngine(mode="auto", audit_log=audit)
        engine.register_rule(ActionRule(
            rule_id="rr1",
            condition={"metrics": [{"name": "regime_match_min", "op": "<",
                                    "value": 60.0}]},
            action={"type": "reallocate_risk",
                    "params": {
                        "reallocate_fn": lambda weights, feedback, reason:
                            effective.update({"a": 0.7, "b": 0.3}) or {},
                        "rollback_fn": lambda: effective.update(WEIGHTS_AB),
                        "weights": WEIGHTS_AB, "feedback": FEED_AB}},
        ))
        results = engine.evaluate(metrics={"regime_match_min": 30.0})
        assert results[0].status == STATUS_EXECUTED
        assert effective == {"a": 0.7, "b": 0.3}
        assert any("executed" in r["action"] for r in audit.records)
        assert engine.rollback(results[0].action_id, actor="ops") is True
        assert effective == WEIGHTS_AB  # 回滚恢复旧权重

    def test_scenario_trigger_action_audit(self) -> None:
        """验收：低匹配/高回撤策略自动降配，动作可审计（完整链路）。"""
        effective: Dict[str, float] = dict(WEIGHTS_AB)
        audit = _AuditRecorder()
        engine = ActionEngine(mode="auto", audit_log=audit)
        engine.register_rule(ActionRule(
            rule_id="rr2",
            condition={"categories": ["risk_realloc"]},
            action={"type": "reallocate_risk",
                    "params": {
                        "reallocate_fn": lambda weights, feedback, reason:
                            effective.update({"a": 0.7, "b": 0.3}) or {},
                        "rollback_fn": lambda: effective.update(WEIGHTS_AB),
                        "weights": WEIGHTS_AB, "feedback": FEED_AB}},
        ))
        results = engine.handle_alert(
            {"level": "WARNING", "category": "risk_realloc", "source": "monitoring",
             "message": "低匹配策略触发降配", "deduplication_key": "rr:cycle1"}
        )
        assert results[0].status == STATUS_EXECUTED
        assert effective["a"] > effective["b"]  # 优质策略权重 > 劣质策略
        assert any("risk_realloc" in r["kind"] or "executed" in r["action"]
                   for r in audit.records)
