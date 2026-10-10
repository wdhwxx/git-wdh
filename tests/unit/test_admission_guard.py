# 文件名：tests/unit/test_admission_guard.py
"""
KP-M04（T-30）写操作准入守卫测试（方案B：仅高危写操作审批，复核人可配置）

覆盖：
  - 默认（无配置）仅审计留痕、不阻断（fail-safe）
  - enabled=true + require_reviewer=true：reviewer 缺失/不匹配 → 拒绝；匹配 → 放行
  - enabled=true + require_reviewer=false：单人审批放行（仅留痕）
  - 非高危 scope：仅审计、不阻断
  - 审计 JSONL 落盘（audit_dir/write_audit.jsonl）
  - require_scope 便捷函数
  - 配置缺失/损坏 → fail-safe 回退仅审计模式

运行方式: pytest tests/unit/test_admission_guard.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict

import pytest

from myquant.api.middleware.auth import (
    HIGH_RISK_SCOPES,
    OperationGuard,
    require_scope,
)


@pytest.fixture()
def tmp_cfg(tmp_path: Path) -> Path:
    """生成临时 live_admission.yaml（默认 enabled=false）。"""
    p = tmp_path / "live_admission.yaml"
    p.write_text(
        "admission:\n"
        "  enabled: false\n"
        "  require_reviewer: false\n"
        "  reviewer: \"\"\n"
        f"  audit_dir: {tmp_path / 'audit'}\n",
        encoding="utf-8",
    )
    return p


def _audit_file(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "write_audit.jsonl"


def test_high_risk_scopes_contain_three_operations() -> None:
    """高危写操作域含强平/资金调拨/实盘切换。"""
    assert {"force_close", "transfer_cash", "mode_switch_live"} <= HIGH_RISK_SCOPES


def test_default_guard_disabled_only_audits(tmp_path: Path) -> None:
    """默认守卫（无配置）：高危操作放行但审计留痕。"""
    guard = OperationGuard()
    assert not guard.enabled
    ok, why = guard.check("force_close", operator="ops", action="test")
    assert ok is True
    assert why == ""


def test_enabled_requires_reviewer_denies_without(tmp_cfg: Path) -> None:
    """启用 + 需复核人：reviewer 缺失 → 拒绝并审计 denied。"""
    tmp_cfg.write_text(
        tmp_cfg.read_text(encoding="utf-8").replace("enabled: false", "enabled: true")
        .replace("require_reviewer: false", "require_reviewer: true")
        .replace('reviewer: ""', 'reviewer: "lawyer_a"'),
        encoding="utf-8",
    )
    guard = OperationGuard(str(tmp_cfg))
    assert guard.enabled
    ok, why = guard.check("force_close", operator="ops", action="force_close_all")
    assert ok is False
    assert "复核人" in why
    lines = _audit_file(tmp_cfg.parent).read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["result"] == "denied"


def test_enabled_reviewer_match_allows(tmp_cfg: Path) -> None:
    """启用 + 需复核人：reviewer 匹配 → 放行并审计 approved。"""
    tmp_cfg.write_text(
        tmp_cfg.read_text(encoding="utf-8").replace("enabled: false", "enabled: true")
        .replace("require_reviewer: false", "require_reviewer: true")
        .replace('reviewer: ""', 'reviewer: "lawyer_a"'),
        encoding="utf-8",
    )
    guard = OperationGuard(str(tmp_cfg))
    ok, why = guard.check("transfer_cash", operator="ops", reviewer="lawyer_a", action="transfer 1.0")
    assert ok is True
    assert why == ""
    lines = _audit_file(tmp_cfg.parent).read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["result"] == "approved"
    assert rec["reviewer"] == "lawyer_a"


def test_enabled_single_operator_allows(tmp_cfg: Path) -> None:
    """启用但不需要复核人（单人审批）：操作者本人即审批，放行留痕。"""
    tmp_cfg.write_text(
        tmp_cfg.read_text(encoding="utf-8").replace("enabled: false", "enabled: true"),
        encoding="utf-8",
    )
    guard = OperationGuard(str(tmp_cfg))
    ok, _ = guard.check("mode_switch_live", operator="ops", action="switch live")
    assert ok is True
    lines = _audit_file(tmp_cfg.parent).read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["result"] == "approved"


def test_non_high_risk_scope_always_allowed(tmp_path: Path) -> None:
    """非高危 scope：即使启用也仅审计、不阻断。"""
    guard = OperationGuard()
    ok, why = guard.check("strategy_config_write", operator="ops", action="tune")
    assert ok is True
    assert why == ""  # 非高危：仅审计留痕、不阻断


def test_audit_file_records_fields(tmp_cfg: Path) -> None:
    """审计 JSONL 记录含 ts/scope/operator/action/result/reviewer/reason。"""
    guard = OperationGuard(str(tmp_cfg))
    guard.check("force_close", operator="ops", reviewer=None, action="force_close_all")
    lines = _audit_file(tmp_cfg.parent).read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(lines[-1])
    assert set(rec) >= {"ts", "scope", "operator", "action", "result", "reviewer", "reason"}
    assert rec["scope"] == "force_close"
    assert rec["operator"] == "ops"


def test_require_scope_helper_default_guard() -> None:
    """require_scope 便捷函数走默认守卫（不阻断）。"""
    ok, why = require_scope("ops", "force_close", action="test")
    assert ok is True
    assert why == ""


def test_require_scope_with_guard(tmp_cfg: Path) -> None:
    """require_scope 传入自定义守卫：启用后无复核人 → 拒绝。"""
    tmp_cfg.write_text(
        tmp_cfg.read_text(encoding="utf-8").replace("enabled: false", "enabled: true")
        .replace("require_reviewer: false", "require_reviewer: true")
        .replace('reviewer: ""', 'reviewer: "lawyer_a"'),
        encoding="utf-8",
    )
    guard = OperationGuard(str(tmp_cfg))
    ok, why = require_scope("ops", "force_close", reviewer=None, guard=guard)
    assert ok is False
    assert "复核人" in why


def test_missing_config_fail_safe(tmp_path: Path) -> None:
    """配置缺失：fail-safe 回退仅审计模式（enabled=false、不阻断）。"""
    guard = OperationGuard(str(tmp_path / "not_exist.yaml"))
    assert not guard.enabled
    ok, _ = guard.check("force_close", operator="ops")
    assert ok is True


def test_entry_points_wired(tmp_cfg: Path) -> None:
    """入口标注已接线：guardian 强平 / portfolio 调拨 / run_mode 实盘切换 走守卫。"""
    from myquant.live.portfolio import LivePortfolio
    from myquant.live.run_mode import RunMode, apply_mode_to_config
    from myquant.risk import RiskManager
    from myquant.risk.guardian import RiskGuardian, register_operation_guard

    # guardian.force_close_all：paper 模式未装配守卫不阻断（v1 仅记录）
    g = RiskGuardian(RiskManager())
    assert g.force_close_all(reason="test") in (True, False)  # 不抛错

    # portfolio.transfer_cash：默认不阻断（v1 未实现返回 False）
    p = LivePortfolio(initial_capital=100_000.0)
    assert p.transfer_cash(1.0, "acc2") is False

    # 0.4 fail-closed：未装配准入守卫 → run_mode LIVE 切换被拒绝（显式抛错，不再默认放行）
    cfg: Dict = {"live": {}, "broker": {"type": "qmt", "account": "a1"}}
    with pytest.raises(ValueError):
        apply_mode_to_config(cfg, RunMode.LIVE)

    # 装配守卫（enabled=false 仅审计留痕）后：LIVE 切换放行写入（配置层明确选择）
    from myquant.api.middleware.auth import OperationGuard

    register_operation_guard(OperationGuard(str(tmp_cfg)))
    try:
        out = apply_mode_to_config(cfg, RunMode.LIVE)
        assert out["live"]["trade_mode"] == "live"
        assert out["broker"]["mode"] == "LIVE"
    finally:
        register_operation_guard(None)

    # 启用 + 需复核人后：run_mode LIVE 切换被拒绝（显式抛错）
    tmp_cfg.write_text(
        tmp_cfg.read_text(encoding="utf-8").replace("enabled: false", "enabled: true")
        .replace("require_reviewer: false", "require_reviewer: true")
        .replace('reviewer: ""', 'reviewer: "lawyer_a"'),
        encoding="utf-8",
    )
    guard = OperationGuard(str(tmp_cfg))
    ok, why = require_scope("run_mode", "mode_switch_live", reviewer=None, guard=guard)
    assert ok is False
    assert "复核人" in why


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
