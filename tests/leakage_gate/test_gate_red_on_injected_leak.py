# 文件名：tests/leakage_gate/test_gate_red_on_injected_leak.py
"""
test_gate_red_on_injected_leak.py - 注入泄漏必报红（M0-08 同批 / M0-13 核心）
==============================================================================
覆盖：
  - 注入已知泄漏（策略引用台账阻断列 每股收益）→ assert_all_gates_pass 出口报红
  - 回测出口挂载：BacktestScheduler.create_engine 已挂 assert_all_gates_pass
    （spy 验证调用点与 strategy_id）
  - 实盘出口挂载：create_live_engine 已挂 assert_all_gates_pass（spy 验证）

防未来函数：测试全部使用已发生的固定时间点，不预测未来。
"""

import pytest

import myquant.leakage_gate.contract as contract_module
import myquant.leakage_gate.consumption as consumption_module
import myquant.live.engine as live_engine_module
from myquant.backtest.engine import BacktestScheduler
from myquant.leakage_gate.gate import (
    GateConfig,
    LeakageGateError,
    assert_all_gates_pass,
    reset_gate_checks,
    set_config,
)
from myquant.models.backtest import BacktestConfig
from myquant.strategies.base import StrategyBase

_DECISION_TIME: str = "2026-10-10"


def _make_ledger() -> dict:
    """台账：收盘价通过（trade_date），每股收益阻断。"""
    return {
        "columns": {
            "收盘价": {"derived_at": ["market_data.close (raw)"],
                      "date_basis": "trade_date", "severity": "通过",
                      "consumers": ["全部策略"]},
            "每股收益": {"derived_at": ["scripts/数据导出_DB到processed.py"],
                        "date_basis": "report_date", "target_basis": "eff_disclosure_date",
                        "severity": "阻断", "consumers": []},
        }
    }


@pytest.fixture(autouse=True)
def _restore_gates() -> None:
    """恢复 enabled=True（对抗 tests/conftest.py 的测试隔离关闭）并重置注册表。"""
    reset_gate_checks()
    set_config(GateConfig())
    yield
    reset_gate_checks()
    set_config(GateConfig())


@pytest.fixture(autouse=True)
def _inject_leak(monkeypatch: pytest.MonkeyPatch):
    """注入泄漏环境：台账（每股收益阻断）+ 引用每股收益的策略源码 + 注册表条目。"""
    monkeypatch.setattr(consumption_module, "_load_ledger", lambda: _make_ledger())
    monkeypatch.setattr(
        consumption_module, "_load_registry",
        lambda: {"strategies": [{"name": "fake_leak", "consumed_columns": ["收盘价"]}]},
    )
    monkeypatch.setattr(
        consumption_module, "_strategy_source",
        lambda _sid: 'def generate_signal(df):\n    eps = df["每股收益"]\n    return eps',
    )


# ═══════════════════════════════════════════════
#  注入已知泄漏 → 出口必报红
# ═══════════════════════════════════════════════

def test_assert_gates_red_on_injected_leak() -> None:
    """注入引用每股收益的策略 → 出口断言报红（R11/RL-13）。"""
    from myquant.leakage_gate.contract import ensure_registered as c_reg
    from myquant.leakage_gate.consumption import ensure_registered as s_reg

    c_reg()
    s_reg()
    with pytest.raises(LeakageGateError) as exc_info:
        assert_all_gates_pass("fake_leak", _DECISION_TIME)
    assert any(v.rule_id == "R11" and "每股收益" in v.message for v in exc_info.value.violations)


# ═══════════════════════════════════════════════
#  回测出口挂载（M0-08）
# ═══════════════════════════════════════════════

class _WiredSentinel(Exception):
    """哨兵异常：验证挂载点被调用后中断装配。"""


class _FakeWiredStrategy(StrategyBase):
    """最小可装配策略（实现抽象 generate_signal；name=挂载验证用）。"""

    name = "fake_wired"

    def generate_signal(self, data, **kwargs):  # noqa: ANN001
        return data


def test_create_engine_gate_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """BacktestScheduler.create_engine 已挂 assert_all_gates_pass（spy 验证）。"""
    calls: list[str] = []

    def spy(strategy_id: str, decision_time: str) -> None:
        calls.append(strategy_id)
        raise _WiredSentinel()

    monkeypatch.setattr("myquant.leakage_gate.assert_all_gates_pass", spy)
    cfg = BacktestConfig(
        initial_capital=1_000_000.0,
        symbols=["000001.SZ"],
        start_date="2024-01-01",
        end_date="2024-06-01",
        frequency="1d",
    )
    scheduler = BacktestScheduler(mode="auto")
    with pytest.raises(_WiredSentinel):
        scheduler.create_engine(config=cfg, strategy=_FakeWiredStrategy(), data=None, data_provider=None)
    assert calls == ["fake_wired"], f"assert_all_gates_pass 未以策略 name 调用: {calls}"


# ═══════════════════════════════════════════════
#  实盘出口挂载（M0-09）
# ═══════════════════════════════════════════════

def test_create_live_engine_gate_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_live_engine 已挂 assert_all_gates_pass（spy 验证，装配依赖打桩）。"""
    calls: list[str] = []

    def spy(strategy_id: str, decision_time: str) -> None:
        calls.append(strategy_id)
        raise _WiredSentinel()

    monkeypatch.setattr("myquant.leakage_gate.assert_all_gates_pass", spy)
    # 用真实已注册策略 ma_cross 装配（strategy 段；paper 模式跳过合规门禁），
    # 仅 spy 挂载点，不跑完整装配链。
    config = {
        "live": {"name": "t", "trade_mode": "paper"},
        "broker": {}, "execution": {}, "market_data": {},
        "strategy": {"name": "ma_cross"},
        "position": {}, "risk_control": {"enabled": False},
        "state_store": {}, "scheduler": {},
    }
    with pytest.raises(_WiredSentinel):
        live_engine_module.create_live_engine(config)
    assert calls == ["ma_cross"], f"create_live_engine 未以策略 name 调用 assert_all_gates_pass: {calls}"
