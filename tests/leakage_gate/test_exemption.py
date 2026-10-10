# 文件名：tests/leakage_gate/test_exemption.py
"""
test_exemption.py - 豁免流（M3-02）专项测试
==============================================
覆盖：豁免登记/持久化/撤销、有效期判定、过期由门禁中心自动拦截、
grant 落 audit_log（append-only 哈希链）、set_store 隔离。

R-19 零覆盖：显式子模块 import；门禁集成用例显式恢复 GateConfig(enabled=True)。
"""

from datetime import datetime, timedelta

import pytest

import myquant.leakage_gate.exemption as ex
from myquant.leakage_gate.gate import (
    GateConfig,
    LeakageGateError,
    Violation,
    assert_all_gates_pass,
    register_gate_check,
    reset_gate_checks,
    set_config,
)


NOW = datetime(2026, 10, 10).replace(hour=0, minute=0, second=0, microsecond=0)


def _exempt(rule_id="R99", target="", valid_until="2026-12-31", vid="EX-T1"):
    return ex.Exemption(
        exemption_id=vid,
        reason="测试豁免：保守近似，无未来数据",
        rule_id=rule_id,
        approver="tester",
        valid_from="2026-01-01",
        valid_until=valid_until,
        target=target,
    )


def test_grant_load_roundtrip_memory():
    store = ex.ExemptionStore()
    store.grant(_exempt())
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].exemption_id == "EX-T1"
    assert loaded[0].approver == "tester"


def test_grant_persists_file(tmp_path):
    path = str(tmp_path / "exemptions.json")
    store = ex.ExemptionStore(path=path)
    store.grant(_exempt(vid="EX-P1"))
    store2 = ex.ExemptionStore(path=path)
    assert [e.exemption_id for e in store2.load()] == ["EX-P1"]


def test_grant_invalid_period_raises():
    store = ex.ExemptionStore()
    with pytest.raises(ex.ExemptionError):
        store.grant(_exempt(valid_until="2025-01-01"))  # valid_from > valid_until


def test_grant_duplicate_id_raises():
    store = ex.ExemptionStore()
    store.grant(_exempt())
    with pytest.raises(ex.ExemptionError):
        store.grant(_exempt(vid="EX-T1"))


def test_revoke():
    store = ex.ExemptionStore()
    store.grant(_exempt())
    assert store.revoke("EX-T1") is True
    assert store.revoke("EX-T1") is False
    assert store.load() == []


def test_is_active_boundaries():
    e = _exempt(valid_until="2026-10-10")
    # valid_until 当日仍有效（含）；次日过期
    assert e.is_active(NOW) is True
    assert e.is_active(NOW + timedelta(days=1)) is False
    assert e.is_expired(NOW) is False
    assert e.is_expired(NOW + timedelta(days=1)) is True
    # 生效日前未生效
    e_future = ex.Exemption(
        exemption_id="EX-F", reason="r", rule_id="R99", approver="t",
        valid_from="2027-01-01", valid_until="2027-12-31")
    assert e_future.is_active(NOW) is False


def test_covers_active_global_and_targeted():
    store = ex.ExemptionStore()
    store.grant(_exempt(rule_id="R99", target=""))
    store.grant(_exempt(rule_id="R01", target="strat_a", vid="EX-T2"))
    v_global = Violation(gate="g", rule_id="R99", severity="阻断", message="m")
    assert store.covers(v_global, NOW) is True
    # 定向豁免只覆盖指定策略
    v_a = Violation(gate="g", rule_id="R01", severity="阻断", message="m", strategy_id="strat_a")
    v_b = Violation(gate="g", rule_id="R01", severity="阻断", message="m", strategy_id="strat_b")
    assert store.covers(v_a, NOW) is True
    assert store.covers(v_b, NOW) is False


def test_covers_ignores_expired():
    store = ex.ExemptionStore()
    store.grant(_exempt(rule_id="R99", valid_until="2026-09-01"))  # 已过期
    v = Violation(gate="g", rule_id="R99", severity="阻断", message="m")
    assert store.covers(v, NOW) is False  # 过期豁免不压制 → 门禁拦截


def test_suppress_removes_only_active():
    store = ex.ExemptionStore()
    store.grant(_exempt(rule_id="R99", valid_until="2026-12-31"))   # 有效
    store.grant(_exempt(rule_id="R98", valid_until="2026-09-01", vid="EX-X"))  # 过期
    v1 = Violation(gate="g", rule_id="R99", severity="阻断", message="m1")
    v2 = Violation(gate="g", rule_id="R98", severity="阻断", message="m2")
    kept = store.suppress([v1, v2], NOW)
    assert [v.rule_id for v in kept] == ["R98"]  # 过期 R98 保留阻断，有效 R99 被压制


def test_empty_store_no_suppress():
    store = ex.ExemptionStore()
    v = Violation(gate="g", rule_id="R99", severity="阻断", message="m")
    assert store.suppress([v], NOW) == [v]


def test_grant_appends_audit(monkeypatch):
    """grant 落 audit_log（kind=exemption_grant）。"""
    recorded = []

    class _FakeStore:
        def append(self, actor_kind, kind, actor="", detail=None):
            recorded.append((actor_kind, kind, actor, detail))

    monkeypatch.setattr("myquant.leakage_gate.exemption.AuditLogStore", _FakeStore)
    store = ex.ExemptionStore()
    store.grant(_exempt())
    assert recorded and recorded[0][1] == "exemption_grant"
    assert recorded[0][0] == "leakage_gate"


def test_gate_blocks_expired_exemption():
    """验收 M3-02：豁免过期 → 门禁中心自动拦截（抛 LeakageGateError）。"""
    set_config(GateConfig(enabled=True, audit_violations=False))
    reset_gate_checks()
    try:
        store = ex.ExemptionStore()
        ex.set_store(store)
        # 有效豁免 → 压制违规 → 通过
        store.grant(_exempt(rule_id="R99", valid_until="2026-12-31"))
        register_gate_check("exempt_test", lambda sid, dt: [
            Violation(gate="exempt_test", rule_id="R99", severity="阻断", message="m",
                      strategy_id=sid, decision_time=dt)])
        assert_all_gates_pass("strat_a", "2026-10-10")  # 不抛错（被压制）

        # 豁免过期 → 不压制 → 门禁自动拦截
        store.grant(_exempt(rule_id="R97", valid_until="2026-09-01", vid="EX-EXP"))
        reset_gate_checks()
        register_gate_check("exempt_expired", lambda sid, dt: [
            Violation(gate="exempt_expired", rule_id="R97", severity="阻断", message="m",
                      strategy_id=sid, decision_time=dt)])
        with pytest.raises(LeakageGateError):
            assert_all_gates_pass("strat_a", "2026-10-10")
    finally:
        set_config(GateConfig(enabled=False, audit_violations=True))
        reset_gate_checks()
        ex.set_store(None)


def test_default_store_isolated():
    """set_store(None) 后回默认空库；模块级便捷 grant_exemption 可用。"""
    ex.set_store(None)
    store = ex.ExemptionStore()
    ex.set_store(store)
    ex.grant_exemption(_exempt(vid="EX-DEF"))
    assert any(e.exemption_id == "EX-DEF" for e in ex.get_store().load())
    ex.set_store(None)
