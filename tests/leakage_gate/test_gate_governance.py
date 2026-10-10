# 文件名：tests/leakage_gate/test_gate_governance.py
"""
test_gate_governance.py - 门禁中心治理测试（M0-05 同批 / M0-11 核心）
=======================================================================
覆盖：
  - GateConfig 开关：enabled=True 违规抛 LeakageGateError 阻断；enabled=False 仅审计不阻断
  - Violation 台账结构（gate/rule_id/severity/message/strategy_id/decision_time）
  - 违规落 audit_log（临时库验证 kind=leakage_gate/action=violation）
  - register_gate_check：重复注册报错；检查函数异常按 fail-closed 记违规

防未来函数：测试全部使用已发生的固定时间点，不预测未来。
"""

import pytest

import myquant.leakage_gate.gate as gate_module
from myquant.core.audit_log import AuditLogStore
from myquant.leakage_gate import (
    GateConfig,
    LeakageGateError,
    Violation,
    assert_all_gates_pass,
    get_config,
    register_gate_check,
    reset_gate_checks,
    set_config,
)

_DECISION_TIME: str = "2026-10-10"


@pytest.fixture(autouse=True)
def _clean_gates() -> None:
    """每个测试前后重置门禁检查注册表与模块级配置（隔离）。"""
    reset_gate_checks()
    set_config(GateConfig())
    yield
    reset_gate_checks()
    set_config(GateConfig())


def _mk_check(violations: list[Violation]):
    """构造返回固定违规列表的假门禁检查。"""

    def check(strategy_id: str, decision_time: str) -> list[Violation]:
        return list(violations)

    return check


# ═══════════════════════════════════════════════
#  开关行为
# ═══════════════════════════════════════════════

def test_default_config_enabled() -> None:
    """默认配置 enabled=True（违规即阻断）。"""
    assert get_config().enabled is True
    assert get_config().audit_violations is True


def test_no_violation_passes() -> None:
    """无违规时出口断言直接通过。"""
    register_gate_check("fake_pass", _mk_check([]))
    assert_all_gates_pass("s1", _DECISION_TIME)  # 不应抛异常


def test_enabled_raises_on_violation() -> None:
    """enabled=True 且存在违规时抛 LeakageGateError。"""
    register_gate_check(
        "fake_viol",
        _mk_check(
            [
                Violation(
                    gate="consumption",
                    rule_id="R11",
                    severity="阻断",
                    message="引用阻断档数据列: 每股收益",
                    strategy_id="s1",
                    decision_time=_DECISION_TIME,
                )
            ]
        ),
    )
    with pytest.raises(LeakageGateError) as exc_info:
        assert_all_gates_pass("s1", _DECISION_TIME)
    err: LeakageGateError = exc_info.value
    assert len(err.violations) == 1
    assert err.violations[0].rule_id == "R11"
    assert "每股收益" in err.violations[0].message


def test_disabled_only_audits() -> None:
    """enabled=False 时违规仅审计（警告档），不抛异常。"""
    set_config(GateConfig(enabled=False, audit_violations=True))
    register_gate_check(
        "fake_viol",
        _mk_check(
            [
                Violation(
                    gate="contract",
                    rule_id="R01",
                    severity="阻断",
                    message="缺契约",
                    strategy_id="s1",
                    decision_time=_DECISION_TIME,
                )
            ]
        ),
    )
    assert_all_gates_pass("s1", _DECISION_TIME)  # disabled 不抛异常


def test_disabled_no_audit_when_off() -> None:
    """audit_violations=False 且 disabled 时，违规不落审计也不阻断。"""
    set_config(GateConfig(enabled=False, audit_violations=False))
    register_gate_check(
        "fake_viol",
        _mk_check(
            [
                Violation(
                    gate="contract", rule_id="R99", severity="阻断",
                    message="x", strategy_id="s1", decision_time=_DECISION_TIME,
                )
            ]
        ),
    )
    assert_all_gates_pass("s1", _DECISION_TIME)


# ═══════════════════════════════════════════════
#  台账结构
# ═══════════════════════════════════════════════

def test_violation_shape() -> None:
    """Violation 台账结构：全字段冻结且可读。"""
    v = Violation(
        gate="static_scan", rule_id="R02", severity="阻断",
        message="center=True", strategy_id="s1", decision_time=_DECISION_TIME,
    )
    assert v.gate == "static_scan"
    assert v.rule_id == "R02"
    assert v.severity == "阻断"
    assert v.message == "center=True"
    assert v.strategy_id == "s1"
    assert v.decision_time == _DECISION_TIME
    with pytest.raises(AttributeError):
        v.rule_id = "R03"  # frozen：禁止改写


# ═══════════════════════════════════════════════
#  注册与异常路径
# ═══════════════════════════════════════════════

def test_register_duplicate_raises() -> None:
    """重复注册同名门禁检查报错。"""
    register_gate_check("dup", _mk_check([]))
    with pytest.raises(ValueError):
        register_gate_check("dup", _mk_check([]))


def test_register_empty_name_raises() -> None:
    """空 name 注册报错。"""
    with pytest.raises(ValueError):
        register_gate_check("", _mk_check([]))


def test_check_exception_fail_closed() -> None:
    """门禁检查自身异常按 fail-closed 记"阻断"违规（不静默放行）。"""

    def bad_check(strategy_id: str, decision_time: str) -> list[Violation]:
        raise RuntimeError("检查器内部故障")

    register_gate_check("broken", bad_check)
    with pytest.raises(LeakageGateError) as exc_info:
        assert_all_gates_pass("s1", _DECISION_TIME)
    violations: list[Violation] = exc_info.value.violations
    assert len(violations) == 1
    assert violations[0].rule_id == "gate-error"
    assert violations[0].severity == "阻断"
    assert "检查器内部故障" in violations[0].message


# ═══════════════════════════════════════════════
#  违规落 audit_log
# ═══════════════════════════════════════════════

def test_violation_recorded_to_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """违规落 audit_log（临时库验证 kind=leakage_gate/action=violation）。"""
    store = AuditLogStore(db_path=str(tmp_path / "t_audit.db"))
    monkeypatch.setattr(gate_module, "AuditLogStore", lambda: store)

    register_gate_check(
        "fake_viol",
        _mk_check(
            [
                Violation(
                    gate="consumption", rule_id="R11", severity="阻断",
                    message="引用每股收益", strategy_id="s1", decision_time=_DECISION_TIME,
                )
            ]
        ),
    )
    with pytest.raises(LeakageGateError):
        assert_all_gates_pass("s1", _DECISION_TIME)

    rows = store.query(kind="leakage_gate")
    assert len(rows) == 1
    assert rows[0]["action"] == "violation"
    assert rows[0]["actor"] == "leakage_gate"
    assert rows[0]["seq"] == 1
