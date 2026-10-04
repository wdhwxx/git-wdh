# 文件名：tests/unit/test_run_mode.py
"""运行模式切割接口（RunMode）单元测试。

覆盖 myquant/live/run_mode.py：
  - 枚举值
  - resolve_run_mode 优先级与配置推导
  - apply_mode_to_config 写回
  - validate_live 实盘安全门
  - check_mode_consistency 配置一致性
"""

from __future__ import annotations

import pytest

from myquant.live.run_mode import (
    RunMode,
    apply_mode_to_config,
    check_mode_consistency,
    describe,
    resolve_run_mode,
    validate_live,
)


def _cfg(
    trade_mode: str = "paper",
    broker_type: str = "paper",
    broker_mode: str = "SIM",
) -> dict:
    """构造最小实盘配置字典（含 KP-P6-01 报备合规段，放行 LIVE 校验）。"""
    return {
        "live": {"trade_mode": trade_mode},
        "broker": {"type": broker_type, "mode": broker_mode, "account": "888"},
        "compliance": {
            "enabled": True,
            "filing": {
                "filing_number": "ZJ2026-001",
                "filing_date": "2026-01-15",
                "account_id": "ACC-0001",
                "strategy_id": "ma_cross_v1",
                "software_version": "1.0.0",
                "filing_status": "approved",
            },
        },
    }


# ── 枚举 ──

def test_run_mode_enum_values() -> None:
    """RunMode 枚举值固定且中文说明非空。"""
    assert RunMode.MONITOR.value == "monitor"
    assert RunMode.PAPER.value == "paper"
    assert RunMode.SIM.value == "sim"
    assert RunMode.LIVE.value == "live"
    for mode in RunMode:
        assert describe(mode)


# ── resolve_run_mode ──

def test_resolve_default_is_paper() -> None:
    """无任何参数与配置时默认 paper。"""
    assert resolve_run_mode({}) is RunMode.PAPER


def test_resolve_cli_mode_wins() -> None:
    """显式 cli_mode 优先于 paper_flag / monitor_only / 配置。"""
    cfg = _cfg(trade_mode="live", broker_type="qmt", broker_mode="LIVE")
    assert resolve_run_mode(cfg, cli_mode="paper") is RunMode.PAPER
    assert resolve_run_mode(cfg, cli_mode="sim") is RunMode.SIM
    assert resolve_run_mode(cfg, cli_mode="live") is RunMode.LIVE
    assert resolve_run_mode(cfg, cli_mode="monitor") is RunMode.MONITOR


def test_resolve_paper_flag_and_monitor() -> None:
    """paper_flag 与 monitor_only 次优先。"""
    assert resolve_run_mode({}, paper_flag=True) is RunMode.PAPER
    assert resolve_run_mode({}, monitor_only=True) is RunMode.MONITOR
    # paper_flag 优先于 monitor_only
    assert resolve_run_mode({}, paper_flag=True, monitor_only=True) is RunMode.PAPER


def test_resolve_invalid_cli_mode() -> None:
    """非法 cli_mode 抛 ValueError。"""
    with pytest.raises(ValueError):
        resolve_run_mode({}, cli_mode="xxx")


def test_resolve_derive_from_config() -> None:
    """从配置推导：live 组合→LIVE；qmt+SIM→SIM；paper→PAPER。"""
    assert resolve_run_mode(_cfg(trade_mode="live", broker_type="qmt", broker_mode="LIVE")) is RunMode.LIVE
    assert resolve_run_mode(_cfg(broker_type="qmt", broker_mode="SIM")) is RunMode.SIM
    assert resolve_run_mode(_cfg(broker_type="paper")) is RunMode.PAPER


# ── apply_mode_to_config ──

def test_apply_monitor() -> None:
    """MONITOR 写回：trade_mode=paper + monitor_only=True。"""
    cfg = {"live": {}, "broker": {}}
    apply_mode_to_config(cfg, RunMode.MONITOR)
    assert cfg["live"]["trade_mode"] == "paper"
    assert cfg["live"]["monitor_only"] is True


def test_apply_paper() -> None:
    """PAPER 写回：trade_mode=paper + broker.type=paper。"""
    cfg = {"live": {}, "broker": {}}
    apply_mode_to_config(cfg, RunMode.PAPER)
    assert cfg["live"]["trade_mode"] == "paper"
    assert cfg["broker"]["type"] == "paper"


def test_apply_sim() -> None:
    """SIM 写回：trade_mode=paper + broker.mode=SIM，broker.type 保持。"""
    cfg = {"live": {}, "broker": {"type": "qmt"}}
    apply_mode_to_config(cfg, RunMode.SIM)
    assert cfg["live"]["trade_mode"] == "paper"
    assert cfg["broker"]["mode"] == "SIM"
    assert cfg["broker"]["type"] == "qmt"


def test_apply_live() -> None:
    """LIVE 写回：trade_mode=live + broker.mode=LIVE。"""
    cfg = {"live": {}, "broker": {}}
    apply_mode_to_config(cfg, RunMode.LIVE)
    assert cfg["live"]["trade_mode"] == "live"
    assert cfg["broker"]["mode"] == "LIVE"


# ── validate_live ──

def test_validate_live_non_live_ok() -> None:
    """非 LIVE 模式安全门直接放行。"""
    assert validate_live(RunMode.PAPER, _cfg()) == []
    assert validate_live(RunMode.SIM, _cfg()) == []
    assert validate_live(RunMode.MONITOR, _cfg()) == []


def test_validate_live_ok() -> None:
    """LIVE 且 confirm+account+qmt 券商 → 放行。"""
    cfg = _cfg(trade_mode="live", broker_type="qmt", broker_mode="LIVE")
    assert validate_live(RunMode.LIVE, cfg, confirm_live=True) == []


def test_validate_live_missing_confirm() -> None:
    """LIVE 缺 confirm → 拦截。"""
    cfg = _cfg(trade_mode="live", broker_type="qmt", broker_mode="LIVE")
    errors = validate_live(RunMode.LIVE, cfg, confirm_live=False)
    assert any("confirm-live" in e for e in errors)


def test_validate_live_paper_broker() -> None:
    """LIVE 但 broker 为 paper → 拦截。"""
    cfg = _cfg(trade_mode="live", broker_type="paper", broker_mode="SIM")
    errors = validate_live(RunMode.LIVE, cfg, confirm_live=True)
    assert any("qmt/ptrade" in e for e in errors)


def test_validate_live_missing_account() -> None:
    """LIVE 缺账户 → 拦截。"""
    cfg = _cfg(trade_mode="live", broker_type="qmt", broker_mode="LIVE")
    cfg["broker"]["account"] = ""
    cfg["broker"]["account_id"] = ""
    errors = validate_live(RunMode.LIVE, cfg, confirm_live=True)
    assert any("账户" in e for e in errors)


# ── check_mode_consistency ──

def test_consistency_ok() -> None:
    """paper / qmt+SIM / qmt+LIVE+live 三种自洽组合均无问题。"""
    assert check_mode_consistency(_cfg()) == []
    assert check_mode_consistency(_cfg(broker_type="qmt", broker_mode="SIM")) == []
    assert check_mode_consistency(_cfg(trade_mode="live", broker_type="qmt", broker_mode="LIVE")) == []


def test_consistency_live_trade_but_paper_broker() -> None:
    """trade_mode=live 但 broker 为 paper → 不一致。"""
    errors = check_mode_consistency(_cfg(trade_mode="live", broker_type="paper", broker_mode="SIM"))
    assert any("trade_mode=live" in e and "paper" in e for e in errors)


def test_consistency_paper_broker_live_mode() -> None:
    """paper 券商但 broker.mode=LIVE → 不一致。"""
    errors = check_mode_consistency(_cfg(broker_type="paper", broker_mode="LIVE"))
    assert any("LIVE" in e for e in errors)


def test_consistency_live_trade_but_sim_mode() -> None:
    """trade_mode=live 但 broker.mode=SIM → 不一致。"""
    errors = check_mode_consistency(_cfg(trade_mode="live", broker_type="qmt", broker_mode="SIM"))
    assert any("LIVE" in e for e in errors)
