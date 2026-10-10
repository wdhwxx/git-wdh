# 文件名：tests/leakage_gate/test_ledger_truth_source.py
"""
test_ledger_truth_source.py - 真实台账真源冒烟（M0P-01/02/03 固化）
====================================================================
覆盖（不 monkeypatch，走真实机读路径）：
  - 真实台账 config/data_time_semantics.yaml 经 core.config_loader 可读（14 列登记）
  - 关键断言：每股收益 severity=阻断/target_basis=eff_disclosure_date；收盘价 date_basis=trade_date
  - 无"待确认"残留（M0P-03：consumers 全部实测）
  - 7 策略在真实台账 + 真实注册表 + 真实源码下出口全通过
  - 真实台账下注入引用每股收益的策略 → 出口必报红（R11）

防未来函数：只校验已落盘台账与已登记策略，不预测未来。
"""

from pathlib import Path

import pytest

import myquant.leakage_gate.consumption as consumption_module
from myquant.leakage_gate.contract import (
    _load_ledger,
    ensure_registered as contract_ensure_registered,
)
from myquant.leakage_gate.consumption import ensure_registered as consumption_ensure_registered
from myquant.leakage_gate.gate import (
    GateConfig,
    LeakageGateError,
    assert_all_gates_pass,
    reset_gate_checks,
    set_config,
)
from myquant.strategies.registry import _strategy_registry

_LEDGER_PATH: Path = Path("config/data_time_semantics.yaml")
_DECISION_TIME: str = "2026-10-10"


@pytest.fixture(autouse=True)
def _restore_gates() -> None:
    """恢复 enabled=True（对抗 tests/conftest.py 的测试隔离关闭）并重置注册表。"""
    reset_gate_checks()
    set_config(GateConfig())
    yield
    reset_gate_checks()
    set_config(GateConfig())


# ═══════════════════════════════════════════════
#  真实台账真源（M0P-02）
# ═══════════════════════════════════════════════

def test_ledger_machine_readable_and_complete() -> None:
    """真实台账存在、可经 ConfigLoader 机读、登记 14 列。"""
    assert _LEDGER_PATH.is_file(), f"机读台账缺失: {_LEDGER_PATH}"
    ledger = _load_ledger()
    assert ledger is not None, "台账不可读（经 core.config_loader）"
    columns = ledger.get("columns")
    assert isinstance(columns, dict) and len(columns) == 14, f"台账应登记 14 列, 实际 {len(columns or {})}"
    for required in ["开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额", "涨跌幅",
                     "振幅", "量比", "换手率", "流通股本", "市盈动", "每股收益", "前收盘价"]:
        assert required in columns, f"台账缺列: {required}"


def test_ledger_key_semantics() -> None:
    """台账关键语义：每股收益阻断/目标日基；收盘价 trade_date；无待确认残留。"""
    columns = _load_ledger()["columns"]
    eps = columns["每股收益"]
    assert eps["severity"] == "阻断", "每股收益必须阻断（M1A-12 验证通过前）"
    assert eps["target_basis"] == "eff_disclosure_date", "每股收益目标日基必须 eff_disclosure_date"
    assert eps["date_basis"] == "report_date", "每股收益现状日基 report_date（穿越）"
    assert columns["收盘价"]["date_basis"] == "trade_date", "收盘价日基必须 trade_date"
    assert columns["前收盘价"]["date_basis"] == "shift_1", "前收盘价必须 shift_1 纯前向"
    for name, meta in columns.items():
        consumers = meta.get("consumers")
        assert isinstance(consumers, list), f"{name} consumers 缺失"
        assert all("待确认" not in str(c) for c in consumers), f"{name} consumers 含待确认"


# ═══════════════════════════════════════════════
#  真实出口冒烟（真实台账 + 真实注册表 + 真实源码）
# ═══════════════════════════════════════════════

def test_real_strategies_pass_real_ledger() -> None:
    """7 个真实策略在真实台账下出口断言全通过（无阻断列消费）。"""
    contract_ensure_registered()
    consumption_ensure_registered()
    for name in _strategy_registry:
        assert_all_gates_pass(strategy_id=name, decision_time=_DECISION_TIME)


def test_inject_eps_red_on_real_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实台账下注入引用每股收益的源码 → 出口必报红（R11/RL-13）。"""
    contract_ensure_registered()
    consumption_ensure_registered()
    monkeypatch.setattr(
        consumption_module, "_strategy_source",
        lambda _sid: 'def generate_signal(df):\n    eps = df["每股收益"]\n    return eps',
    )
    with pytest.raises(LeakageGateError) as exc_info:
        assert_all_gates_pass(strategy_id="rsrs", decision_time=_DECISION_TIME)
    assert any(v.rule_id == "R11" and "每股收益" in v.message for v in exc_info.value.violations)
