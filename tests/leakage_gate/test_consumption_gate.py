# 文件名：tests/leakage_gate/test_consumption_gate.py
"""
test_consumption_gate.py - 消费侧列门禁测试（M0-07 同批 / M0-12 核心）
========================================================================
覆盖：
  - AST 列引用提取（纯函数）：df["列名"] / df.列名 / columns=[...] / 字符串常量
  - 纯函数比对（验收核心）：实测 ∪ 声明 ∩ 阻断列 ≠ ∅ → 命中；无命中通过
  - 门禁装配：台账缺失 fail-closed；columns 空；未登记策略；引用阻断列报红
  - 出口断言：注入引用"每股收益"的策略 → 出口必报红（R11 反向测试）

防未来函数：测试全部使用已发生的固定时间点，不预测未来。
"""

import pytest

import myquant.leakage_gate.consumption as consumption_module
import myquant.leakage_gate.contract as contract_module
from myquant.leakage_gate.gate import (
    _GATE_CHECKS,
    GateConfig,
    LeakageGateError,
    assert_all_gates_pass,
    reset_gate_checks,
    set_config,
)

_DECISION_TIME: str = "2026-10-10"

# 台账：收盘价通过（trade_date），每股收益阻断（target_basis=eff_disclosure_date）
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
    """默认注入一致台账（收盘价通过 + 每股收益阻断）。

    注：consumption.py 经 `from contract import _load_ledger` 绑定函数对象，
    须 monkeypatch consumption 模块命名空间的绑定名（monkeypatch contract 无效）。
    """
    monkeypatch.setattr(consumption_module, "_load_ledger", lambda: _make_ledger())
    monkeypatch.setattr(consumption_module, "_load_registry",
                        lambda: contract_module._load_yaml(contract_module._REGISTRY_PATH))


@pytest.fixture(autouse=True)
def _clean_gates() -> None:
    """重置注册表并恢复 enabled=True（对抗 tests/conftest.py 的测试隔离关闭）。"""
    reset_gate_checks()
    set_config(GateConfig())
    yield
    reset_gate_checks()
    set_config(GateConfig())


# ═══════════════════════════════════════════════
#  AST 列引用提取（纯函数）
# ═══════════════════════════════════════════════

def test_extract_df_subscript() -> None:
    """df["每股收益"] → 命中每股收益。"""
    source = 'val = df["每股收益"]'
    keys = {"每股收益", "收盘价"}
    assert consumption_module.extract_columns_from_source(source, keys) == {"每股收益"}


def test_extract_df_attr() -> None:
    """df.收盘价 → 命中收盘价。"""
    source = "val = df.收盘价.shift(1)"
    keys = {"收盘价", "每股收益"}
    assert consumption_module.extract_columns_from_source(source, keys) == {"收盘价"}


def test_extract_columns_list() -> None:
    """columns=[...] → 命中列表内列名。"""
    source = 'cols = ["开盘价", "收盘价"]'
    keys = {"开盘价", "收盘价", "每股收益"}
    assert consumption_module.extract_columns_from_source(source, keys) == {"开盘价", "收盘价"}


def test_extract_filters_non_ledger() -> None:
    """非精确匹配的字符串常量被过滤（日志/说明不误报），仅 df["列名"] 命中。"""
    source = 'logger.info("测试每股收益场景")\nval = df["收盘价"]'
    keys = {"收盘价", "每股收益"}
    assert consumption_module.extract_columns_from_source(source, keys) == {"收盘价"}


def test_extract_syntax_error_safe() -> None:
    """源码语法错误 → 空集合（不抛异常）。"""
    assert consumption_module.extract_columns_from_source("def broken(:\n", {"收盘价"}) == set()


# ═══════════════════════════════════════════════
#  纯函数比对（M0-07 验收核心）
# ═══════════════════════════════════════════════

def test_validate_hit_on_blocked_column() -> None:
    """实测引用阻断列 → 命中。"""
    hits = consumption_module.validate_consumption(
        declared=["收盘价"], actual={"每股收益"}, blocked={"每股收益"}
    )
    assert hits == ["每股收益"]


def test_validate_hit_on_declared_blocked() -> None:
    """声明列含阻断列 → 命中（声明侧输入，取并集）。"""
    hits = consumption_module.validate_consumption(
        declared=["收盘价", "每股收益"], actual={"收盘价"}, blocked={"每股收益"}
    )
    assert hits == ["每股收益"]


def test_validate_clean_pass() -> None:
    """无引用/声明阻断列 → 空。"""
    hits = consumption_module.validate_consumption(
        declared=["收盘价"], actual={"收盘价", "开盘价"}, blocked={"每股收益"}
    )
    assert hits == []


# ═══════════════════════════════════════════════
#  门禁装配
# ═══════════════════════════════════════════════

def test_check_ledger_missing_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """台账缺失 → R11 阻断（fail-closed，无法判定不放行）。"""
    monkeypatch.setattr(consumption_module, "_load_ledger", lambda: None)
    violations = consumption_module.check_consumption("rsrs", _DECISION_TIME)
    assert len(violations) == 1
    assert violations[0].rule_id == "R11"
    assert "缺失" in violations[0].message


def test_check_unregistered_strategy() -> None:
    """未登记策略 → R11 阻断。"""
    violations = consumption_module.check_consumption("no_such", _DECISION_TIME)
    assert len(violations) == 1
    assert violations[0].rule_id == "R11"
    assert "未登记" in violations[0].message


def test_check_real_strategies_clean() -> None:
    """7 个真实策略扫描：无每股收益引用 → 通过（台账一致时）。"""
    for name in ["rsrs", "ma_cross", "grid", "momentum", "mean_reversion", "multi_factor", "kunpeng"]:
        violations = consumption_module.check_consumption(name, _DECISION_TIME)
        assert not violations, f"{name} 意外报红: {[v.message for v in violations]}"


def test_check_blocked_column_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向测试：注入引用每股收益的假策略源码 → 必报红（R11/RL-13）。"""
    bad_source = 'def generate_signal(df):\n    eps = df["每股收益"]\n    return eps'
    monkeypatch.setattr(consumption_module, "_strategy_source", lambda _sid: bad_source)
    monkeypatch.setattr(
        consumption_module, "_load_registry",
        lambda: {"strategies": [{"name": "fake_leak", "consumed_columns": ["收盘价"]}]},
    )
    violations = consumption_module.check_consumption("fake_leak", _DECISION_TIME)
    assert len(violations) == 1
    assert violations[0].rule_id == "R11"
    assert "每股收益" in violations[0].message


# ═══════════════════════════════════════════════
#  出口断言（注入泄漏必报红）
# ═══════════════════════════════════════════════

def test_assert_gates_red_on_blocked_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """出口断言汇总：注入引用每股收益的策略 → 门禁报红。"""
    bad_source = 'def generate_signal(df):\n    eps = df["每股收益"]\n    return eps'
    monkeypatch.setattr(consumption_module, "_strategy_source", lambda _sid: bad_source)
    monkeypatch.setattr(
        consumption_module, "_load_registry",
        lambda: {"strategies": [{"name": "fake_leak", "consumed_columns": ["收盘价"]}]},
    )
    consumption_module.ensure_registered()
    with pytest.raises(LeakageGateError) as exc_info:
        assert_all_gates_pass("fake_leak", _DECISION_TIME)
    assert any(v.rule_id == "R11" for v in exc_info.value.violations)


def test_ensure_registered_idempotent() -> None:
    """ensure_registered 幂等：重复调用不抛错、注册表仍唯一。"""
    consumption_module.ensure_registered()
    consumption_module.ensure_registered()
    assert "consumption" in _GATE_CHECKS
    assert [n for n in _GATE_CHECKS if n == "consumption"].count("consumption") == 1


# ═══════════════════════════════════════════════
#  阻断列名字面量出现次数审计（总验收 G-02 兜底，R11 补充防线）
# ═══════════════════════════════════════════════

def test_g02_audit_literal_hit() -> None:
    """显式阻断列：源码出现阻断列中文字面量 → 计数命中（警告档，不阻断）。"""
    hits = consumption_module.audit_blocked_column_literals(
        ['col = "每股收益"\ndf[col]  # 间接引用', "logger.info('每股收益说明')"],
        blocked_columns=["每股收益"],
    )
    assert hits == [{"column": "每股收益", "count": 2}]


def test_g02_audit_no_hit_when_absent() -> None:
    """源码未出现阻断列字面量 → 空。"""
    hits = consumption_module.audit_blocked_column_literals(
        ['val = df["收盘价"]'], blocked_columns=["每股收益"]
    )
    assert hits == []


def test_g02_audit_counts_each_blocked_column() -> None:
    """多个阻断列各自计数，按列排序。"""
    hits = consumption_module.audit_blocked_column_literals(
        ['a = "流通股本"\nb = "流通股本" + "每股收益"'],
        blocked_columns=["每股收益", "流通股本"],
    )
    # 按列排序（Unicode：每股收益 < 流通股本）
    assert hits == [{"column": "每股收益", "count": 1}, {"column": "流通股本", "count": 2}]


def test_g02_audit_defaults_from_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """blocked_columns 未传 → 从台账加载 severity=阻断 列。"""
    hits = consumption_module.audit_blocked_column_literals(
        ['eps = "每股收益"  # 间接引用路径 col="每股收益"; df[col]']
    )
    # 台账（_make_ledger）中阻断列为每股收益；源码含赋值+注释两处字面量 → 计数 2
    assert hits == [{"column": "每股收益", "count": 2}]


def test_g02_audit_empty_blocked() -> None:
    """无阻断列 → 空（不抛错）。"""
    assert consumption_module.audit_blocked_column_literals(["x"], blocked_columns=[]) == []
