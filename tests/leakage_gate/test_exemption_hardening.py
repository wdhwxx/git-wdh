# 文件名：tests/leakage_gate/test_exemption_hardening.py
"""
test_exemption_hardening.py - P1-14 豁免流加固反红测试（#365）
================================================================
覆盖方案 v2.2 附录 C「P0-2 豁免流不持久化/无审批闭环」落地后的硬约束：
  1. 长期全局豁免被拒（valid_until - valid_from > 90 天 → grant 拒）；
  2. rule_id 白名单校验（R99 等非法规则 → grant 拒，防 typo 对不上违规）；
  3. gate-error（门禁自检故障）不可豁免——grant 拒 + covers 双保险永不压制；
  4. 审批状态机 pending→approved→revoked：仅 approved 且未过期压制违规；
  5. grant 强制 pending（调用方传 approved 也被降级，防绕过审批闭环）；
  6. approver 与 actor 分离，grant/approve 各落一条 audit_log（双审计）；
  7. 持久化：落盘后新 store 重载豁免仍在（重启不丢）。

R-19 零覆盖：显式 import 子模块；测试一律 set_store/ExemptionStore() 内存或
tmp_path 文件，不触碰生产默认台账 database/exemptions.json。
"""

from datetime import datetime

import pytest

import myquant.leakage_gate.exemption as ex
from myquant.leakage_gate.gate import Violation


NOW = datetime(2026, 10, 10).replace(hour=0, minute=0, second=0, microsecond=0)


def _make(vid="EX-H1", rule_id="R03", valid_from="2026-08-15",
          valid_until="2026-10-31", approver="", actor="", status="pending"):
    """构造一条豁免（不经过 grant 校验，便于测试边界值）。"""
    return ex.Exemption(
        exemption_id=vid,
        reason="P1-14 反红测试：保守近似，无未来数据",
        rule_id=rule_id,
        approver=approver,
        valid_from=valid_from,
        valid_until=valid_until,
        target="",
        status=status,
        actor=actor,
    )


# ── 1. 90 天上限 ─────────────────────────────────────────────────────────

class TestMaxDays:
    def test_over_90_days_rejected(self):
        """有效期跨度 > 90 天（长期全局豁免）→ grant 拒。"""
        store = ex.ExemptionStore()
        long_ex = _make(valid_from="2026-01-01", valid_until="2026-12-31")
        with pytest.raises(ex.ExemptionError, match="90 天"):
            store.grant(long_ex)

    def test_exactly_90_days_allowed(self):
        """恰好 90 天 → 放行（边界含）。"""
        store = ex.ExemptionStore()
        e = store.grant(_make(valid_from="2026-08-01", valid_until="2026-10-30"))
        assert e.exemption_id == "EX-H1"


# ── 2. rule_id 白名单 ────────────────────────────────────────────────────

class TestRuleIdWhitelist:
    def test_unknown_rule_id_rejected(self):
        """非法 rule_id（R99，不在白名单）→ grant 拒。"""
        store = ex.ExemptionStore()
        with pytest.raises(ex.ExemptionError, match="规则 ID 非法"):
            store.grant(_make(rule_id="R99"))

    def test_gate_error_rule_id_rejected(self):
        """gate-error（门禁自检故障）→ grant 直接拒（不可豁免）。"""
        store = ex.ExemptionStore()
        with pytest.raises(ex.ExemptionError, match="不可豁免"):
            store.grant(_make(rule_id="gate-error"))


# ── 3. gate-error 双保险：covers 永不压制 ────────────────────────────────

class TestGateErrorNotExemptable:
    def test_covers_never_matches_gate_error(self):
        """即使台账里存在一条 approved 的 R03 豁免，gate-error 违规也绝不被压制。"""
        store = ex.ExemptionStore()
        e = store.grant(_make(rule_id="R03"))
        assert store.approve(e.exemption_id, approver="lead") is True
        v_gate_err = Violation(gate="realism", rule_id="gate-error",
                               severity="阻断", message="检查函数自身异常")
        assert store.covers(v_gate_err, NOW) is False  # 双保险，不依赖 grant 拦截


# ── 4/5. 审批状态机：pending 不压、approved 才压、强制 pending ─────────────

class TestApprovalStateMachine:
    def test_pending_does_not_suppress(self):
        """grant 后默认 pending → 不压制违规（须先 approve）。"""
        store = ex.ExemptionStore()
        store.grant(_make(rule_id="R03"))
        v = Violation(gate="g", rule_id="R03", severity="阻断", message="m")
        assert store.covers(v, NOW) is False

    def test_approved_suppresses(self):
        """approve 后 → 压制违规。"""
        store = ex.ExemptionStore()
        e = store.grant(_make(rule_id="R03"))
        assert store.approve(e.exemption_id, approver="lead") is True
        v = Violation(gate="g", rule_id="R03", severity="阻断", message="m")
        assert store.covers(v, NOW) is True

    def test_grant_forces_pending_even_if_caller_sets_approved(self):
        """调用方直接构造 status=approved 传入 grant → 被强制降级 pending（防绕过审批）。"""
        store = ex.ExemptionStore()
        sneaky = _make(rule_id="R03", status="approved", approver="someone")
        stored = store.grant(sneaky)
        assert stored.status == "pending"
        assert stored.approver == ""  # approve 时才填实审批人

    def test_revoked_does_not_suppress(self):
        """revoke 后 → 不压制；记录保留为 revoked（软删除可追溯）。"""
        store = ex.ExemptionStore()
        e = store.grant(_make(rule_id="R03"))
        store.approve(e.exemption_id, approver="lead")
        assert store.revoke(e.exemption_id) is True
        v = Violation(gate="g", rule_id="R03", severity="阻断", message="m")
        assert store.covers(v, NOW) is False
        kept = store.load()
        assert len(kept) == 1 and kept[0].status == "revoked"


# ── 6. approver 与 actor 分离 + 双审计 ───────────────────────────────────

class TestActorApproverSeparation:
    def test_grant_records_actor_approve_records_approver(self, monkeypatch):
        """grant 落 actor（申请人）、approve 落 approver（审批人），两条 audit 分离。"""
        recorded = []

        class _FakeAudit:
            def append(self, actor_kind, kind, actor="", detail=None):
                recorded.append((actor_kind, kind, actor, detail))

        monkeypatch.setattr("myquant.leakage_gate.exemption.AuditLogStore", _FakeAudit)
        store = ex.ExemptionStore()
        e = store.grant(_make(rule_id="R03"), actor="alice_dev")  # 申请人
        store.approve(e.exemption_id, approver="bob_lead")        # 审批人
        kinds = [(k, actor) for (_ak, k, actor, _d) in recorded]
        assert ("exemption_grant", "alice_dev") in kinds
        assert ("exemption_approve", "bob_lead") in kinds
        # actor 与 approver 不得是同一人自批自（grant 记录申请人、approve 记录审批人）
        grant_actor = [a for (k, a) in kinds if k == "exemption_grant"][0]
        approve_actor = [a for (k, a) in kinds if k == "exemption_approve"][0]
        assert grant_actor != approve_actor

    def test_approve_rejects_empty_approver(self):
        """approve 审批人不能为空（审批闭环需留痕）。"""
        store = ex.ExemptionStore()
        e = store.grant(_make(rule_id="R03"))
        with pytest.raises(ex.ExemptionError, match="审批人"):
            store.approve(e.exemption_id, approver="")


# ── 7. 持久化：重启后豁免仍在 ────────────────────────────────────────────

class TestPersistence:
    def test_persists_across_store_reload(self, tmp_path):
        """落盘 exemptions.json → 新 store 重载：approved 豁免仍在且仍压制违规。"""
        path = str(tmp_path / "exemptions.json")
        s1 = ex.ExemptionStore(path=path)
        e = s1.grant(_make(rule_id="R03", vid="EX-PERSIST"))
        s1.approve(e.exemption_id, approver="lead")
        # 模拟重启：全新 store 实例读同一文件
        s2 = ex.ExemptionStore(path=path)
        loaded = s2.load()
        assert [x.exemption_id for x in loaded] == ["EX-PERSIST"]
        assert loaded[0].status == "approved"
        v = Violation(gate="g", rule_id="R03", severity="阻断", message="m")
        assert s2.covers(v, NOW) is True

    def test_revoked_survives_reload(self, tmp_path):
        """revoke 状态落盘后重载仍是 revoked（不物理丢失，可追溯）。"""
        path = str(tmp_path / "exemptions.json")
        s1 = ex.ExemptionStore(path=path)
        e = s1.grant(_make(rule_id="R03", vid="EX-RV"))
        s1.revoke(e.exemption_id)
        s2 = ex.ExemptionStore(path=path)
        assert s2.load()[0].status == "revoked"
