# 文件名：tests/leakage_gate/test_contract.py
"""
test_contract.py - 契约完整性校验测试（M0-06 同批 / M0-10 核心）
===================================================================
覆盖：
  - 声明完整（R09）：注册表 7 策略声明完整通过；缺 decision_time / pit_status /
    available_time / basis 错误 → 报红；注册表缺失/策略未登记 → 报红
  - 台账比对（R10）：台账缺失 fail-closed；columns 空；basis 与台账目标基不一致；
    收盘价日基非 trade_date → 报红；台账一致 → 通过
  - 门禁注册：ensure_registered 幂等；出口断言汇总契约违规报红

契约真源 = config/strategy_registry.yaml（M0-03 机读注册表）；本测试直接读真源校验，
台账经 monkeypatch 注入（真实台账 M0P-02 建）。

防未来函数：测试全部使用已发生的固定时间点，不预测未来。
"""

import pytest

import myquant.leakage_gate.contract as contract_module
from myquant.leakage_gate.gate import (
    _GATE_CHECKS,
    LeakageGateError,
    assert_all_gates_pass,
    reset_gate_checks,
)

_DECISION_TIME: str = "2026-10-10"

# 与 config/strategy_registry.yaml 一致的完整契约（参照 rsrs 条目）
_FULL_ENTRY = {
    "name": "fake_ok",
    "strategy_id": "fake_ok_strategy",
    "type": "daily_equity",
    "decision_time": "T日收盘后 15:00",
    "execution_time": "T+1开盘 09:30",
    "holding_period": "20交易日",
    "universe": {"type": "static", "source": "config/stock_pool.yaml", "pit_status": "未建设"},
    "data_availability": {
        "market_data": {"available_time": "T日15:00", "adjust": "hfq",
                        "adjust_asof": "end_date 必传",
                        "fields": ["开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"]},
        "financial_data": {"available_time": "ann_date", "basis": "eff_disclosure_date", "fields": []},
    },
    "consumed_columns": ["收盘价", "成交量"],
    "ruleset": "daily_close_decision",
    "owner": "pythonwdhmm",
}


def _make_entry(**overrides):
    """构造契约条目 dict（contract 校验输入为 yaml 条目）。"""
    attrs = dict(_FULL_ENTRY)
    attrs.update(overrides)
    return attrs


# 台账 fixture：一致的临时台账（参照 v3.0 §6.1 台账形态）
def _make_ledger(**overrides) -> dict:
    ledger = {
        "columns": {
            "收盘价": {"derived_at": ["market_data.close (raw)"],
                      "date_basis": "trade_date", "severity": "通过",
                      "consumers": ["全部策略"]},
            "每股收益": {"derived_at": ["scripts/数据导出_DB到processed.py"],
                        "date_basis": "report_date", "target_basis": "eff_disclosure_date",
                        "severity": "阻断", "consumers": []},
        }
    }
    for k, v in overrides.items():
        ledger[k] = v
    return ledger


@pytest.fixture(autouse=True)
def _monkey_ledger(monkeypatch: pytest.MonkeyPatch):
    """默认注入一致台账，避免真实台账缺失导致的 fail-closed 干扰声明校验用例。"""
    monkeypatch.setattr(contract_module, "_load_ledger", lambda: _make_ledger())


@pytest.fixture(autouse=True)
def _clean_gates() -> None:
    reset_gate_checks()
    yield
    reset_gate_checks()


# ═══════════════════════════════════════════════
#  声明完整（R09）
# ═══════════════════════════════════════════════

def test_declaration_complete_for_all_registered() -> None:
    """注册表 7 个策略契约声明完整（读真源，无 R09 违规）。"""
    registry = contract_module._load_registry()
    assert registry is not None, "strategy_registry.yaml 缺失"
    entries = [e for e in registry.get("strategies") or [] if isinstance(e, dict)]
    assert len(entries) == 7, f"注册表策略数异常: {len(entries)}"
    for entry in entries:
        name = entry.get("name", "?")
        violations = contract_module._validate_declaration(entry, name, _DECISION_TIME)
        assert not violations, f"{name} 声明不完整: {[v.message for v in violations]}"


def test_check_contract_registry_missing_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约注册表缺失 → R09 阻断。"""
    monkeypatch.setattr(contract_module, "_load_registry", lambda: None)
    violations = contract_module.check_contract("rsrs", _DECISION_TIME)
    assert len(violations) == 1
    assert violations[0].rule_id == "R09"
    assert "注册表" in violations[0].message


def test_check_contract_unregistered_strategy() -> None:
    """策略未登记于契约注册表 → R09 阻断。"""
    violations = contract_module.check_contract("no_such_strategy", _DECISION_TIME)
    assert len(violations) == 1
    assert violations[0].rule_id == "R09"
    assert "未登记" in violations[0].message


def test_missing_decision_time_red() -> None:
    """缺 decision_time → R09。"""
    entry = _make_entry(decision_time="")
    violations = contract_module._validate_declaration(entry, "fake", _DECISION_TIME)
    assert any(v.rule_id == "R09" and "decision_time" in v.message for v in violations)


def test_missing_pit_status_red() -> None:
    """universe 未显式声明 pit_status → R09（契约不得自证 PIT）。"""
    entry = _make_entry(universe={"type": "static", "source": "config/stock_pool.yaml"})
    violations = contract_module._validate_declaration(entry, "fake", _DECISION_TIME)
    assert any(v.rule_id == "R09" and "pit_status" in v.message for v in violations)


def test_missing_available_time_red() -> None:
    """financial_data.available_time 缺失 → R09。"""
    da = dict(_FULL_ENTRY["data_availability"])
    da["financial_data"] = {"basis": "eff_disclosure_date", "fields": []}
    entry = _make_entry(data_availability=da)
    violations = contract_module._validate_declaration(entry, "fake", _DECISION_TIME)
    assert any(v.rule_id == "R09" and "available_time" in v.message for v in violations)


def test_wrong_financial_basis_red() -> None:
    """financial_data.basis 非 eff_disclosure_date → R09（RL-02 契约层）。"""
    da = dict(_FULL_ENTRY["data_availability"])
    da["financial_data"] = {"available_time": "ann_date", "basis": "report_date", "fields": []}
    entry = _make_entry(data_availability=da)
    violations = contract_module._validate_declaration(entry, "fake", _DECISION_TIME)
    assert any(v.rule_id == "R09" and "basis" in v.message and "eff_disclosure_date" in v.message
               for v in violations)


# ═══════════════════════════════════════════════
#  与列级台账比对（R10）
# ═══════════════════════════════════════════════

def test_ledger_missing_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """台账缺失 → R10 阻断（契约不能自证 PIT，须先 M0P-02）。"""
    monkeypatch.setattr(contract_module, "_load_ledger", lambda: None)
    entry = _make_entry()
    violations = contract_module._validate_against_ledger(entry, "fake", _DECISION_TIME)
    assert len(violations) == 1
    assert violations[0].rule_id == "R10"
    assert "缺失" in violations[0].message or "不可读" in violations[0].message


def test_ledger_empty_columns_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """台账 columns 为空 → R10（RL-11：数据列时点未登记）。"""
    monkeypatch.setattr(contract_module, "_load_ledger", lambda: {"columns": {}})
    entry = _make_entry()
    violations = contract_module._validate_against_ledger(entry, "fake", _DECISION_TIME)
    assert len(violations) == 1
    assert violations[0].rule_id == "R10"
    assert "columns 为空" in violations[0].message


def test_ledger_basis_mismatch_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """台账财务列 target_basis 与声明 basis 不一致 → R10。"""
    ledger = _make_ledger()
    ledger["columns"]["每股收益"]["target_basis"] = "report_date"
    monkeypatch.setattr(contract_module, "_load_ledger", lambda: ledger)
    entry = _make_entry()
    violations = contract_module._validate_against_ledger(entry, "fake", _DECISION_TIME)
    assert any(v.rule_id == "R10" and "目标基不一致" in v.message for v in violations)


def test_ledger_close_date_basis_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """台账 收盘价 date_basis 非 trade_date → R10。"""
    ledger = _make_ledger()
    ledger["columns"]["收盘价"]["date_basis"] = "next_trade_date"
    monkeypatch.setattr(contract_module, "_load_ledger", lambda: ledger)
    entry = _make_entry()
    violations = contract_module._validate_against_ledger(entry, "fake", _DECISION_TIME)
    assert any(v.rule_id == "R10" and "收盘价" in v.message for v in violations)


def test_ledger_consistent_pass() -> None:
    """台账一致（收盘价 trade_date / 每股收益 target_basis=eff_disclosure_date）→ 通过。"""
    entry = _make_entry()
    violations = contract_module._validate_against_ledger(entry, "fake", _DECISION_TIME)
    assert not violations


# ═══════════════════════════════════════════════
#  门禁注册与出口
# ═══════════════════════════════════════════════

def test_ensure_registered_idempotent() -> None:
    """ensure_registered 幂等：重复调用不抛错、注册表仍唯一。"""
    contract_module.ensure_registered()
    contract_module.ensure_registered()
    assert "contract" in _GATE_CHECKS
    assert [n for n in _GATE_CHECKS if n == "contract"].count("contract") == 1


def test_assert_gates_red_on_contract_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约违规经出口断言汇总报红（注入残缺注册表条目）。"""
    bad_registry = {"strategies": [{"name": "fake_bad"}]}
    monkeypatch.setattr(contract_module, "_load_registry", lambda: bad_registry)
    contract_module.ensure_registered()
    with pytest.raises(LeakageGateError) as exc_info:
        assert_all_gates_pass("fake_bad", _DECISION_TIME)
    assert any(v.rule_id == "R09" for v in exc_info.value.violations)
