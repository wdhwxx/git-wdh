# 文件名：tests/unit/test_compliance_gate.py
"""
test_compliance_gate.py - 程序化交易报备合规门禁测试（KP-P6-01 / T-10）
======================================================================
覆盖"报备/高频/异常交易控制点全缺失"修复的前置门禁部分：
  1. 缺字段拦截 —— compliance 缺失 → exit_code 2（DoD）。
  2. 放行单测 —— 字段齐全 + 已备案 + 开关开启 → exit_code 0（DoD）。
  3. 合规开关未开启 → 拦截。
  4. 备案状态非 approved → 拦截。
  5. 报备日期格式非法 → 拦截。
  6. run_mode.validate_live 联动 —— LIVE + 未备案 → 错误列表含合规项。
  7. run_mode.validate_live 放行 —— LIVE + 已备案 → 空错误允许。
  8. create_live_engine 装配前拦截 —— trade_mode=live + 未备案 → ValueError。
  9. 备案快照落盘 —— dump_snapshot 写 JSON。

条款映射需持牌律师/合规官确认（以证监会/交易所官网最新原文为准），
本测试仅验证结构性门禁逻辑。

依赖: pytest, myquant.live.acceptance, myquant.live.run_mode,
      myquant.live.engine
"""

from __future__ import annotations

import json

import pytest

from myquant.live.acceptance import (
    DEFAULT_COMPLIANCE_FIELDS,
    ComplianceGate,
)
from myquant.live.engine_assembly import (
    create_live_engine,  # 回写#422：装配层迁出，调用点直连
)
from myquant.live.run_mode import RunMode, validate_live


def _compliant_config() -> dict:
    """字段齐全 + 已备案 + 开关开启 的合规配置。"""
    return {
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
        }
    }


def _empty_config() -> dict:
    """compliance 段为空/缺失。"""
    return {"compliance": {"enabled": False, "filing": {}}}


# ═══════════════════════════════════════════════
#  1/2/3/4/5. ComplianceGate 直接判定
# ═══════════════════════════════════════════════

class TestComplianceGate:
    def test_缺字段拦截_exit2(self) -> None:
        """DoD：compliance 缺失/空 → exit_code 2，overall False。"""
        report = ComplianceGate(_empty_config()).check()
        assert not report.overall
        assert set(report.missing) == set(DEFAULT_COMPLIANCE_FIELDS)
        assert ComplianceGate(_empty_config()).exit_code() == 2

    def test_放行_exit0(self) -> None:
        """DoD：字段齐全 + 已备案 + 开关开启 → exit_code 0，overall True。"""
        report = ComplianceGate(_compliant_config()).check()
        assert report.overall
        assert report.errors == []
        assert ComplianceGate(_compliant_config()).exit_code() == 0

    def test_开关未开启_拦截(self) -> None:
        cfg = dict(_compliant_config())
        cfg["compliance"]["enabled"] = False
        report = ComplianceGate(cfg).check()
        assert not report.overall
        assert any("compliance.enabled" in e for e in report.errors)
        assert ComplianceGate(cfg).exit_code() == 2

    def test_备案状态非approved_拦截(self) -> None:
        cfg = dict(_compliant_config())
        cfg["compliance"]["filing"]["filing_status"] = "pending"
        report = ComplianceGate(cfg).check()
        assert not report.overall
        assert any("备案状态" in e for e in report.errors)

    def test_报备日期格式非法_拦截(self) -> None:
        cfg = dict(_compliant_config())
        cfg["compliance"]["filing"]["filing_date"] = "2026/01/15"
        report = ComplianceGate(cfg).check()
        assert not report.overall
        assert any("报备日期" in e for e in report.errors)

    def test_custom_required_fields(self) -> None:
        """可自定义必需字段集（条款口径待律师确认可调整）。"""
        gate = ComplianceGate(
            {"compliance": {"enabled": True, "filing": {}}},
            required_fields={"filing_number": "报备编号"},
        )
        report = gate.check()
        assert report.missing == ["filing_number"]
        assert gate.exit_code() == 2


# ═══════════════════════════════════════════════
#  6/7. run_mode.validate_live 联动
# ═══════════════════════════════════════════════

class TestValidateLiveCompliance:
    def test_LIVE未备案_错误含合规项(self) -> None:
        config = {
            "live": {"trade_mode": "live"},
            "broker": {"type": "qmt", "account": "ACC-0001"},
            **_empty_config(),
        }
        errors = validate_live(RunMode.LIVE, config, confirm_live=True)
        assert any("报备" in e or "compliance" in e for e in errors)

    def test_LIVE已备案_放行(self) -> None:
        config = {
            "live": {"trade_mode": "live"},
            "broker": {"type": "qmt", "account": "ACC-0001"},
            **_compliant_config(),
        }
        errors = validate_live(RunMode.LIVE, config, confirm_live=True)
        assert errors == []

    def test_非LIVE_不校验合规(self) -> None:
        errors = validate_live(RunMode.PAPER, _empty_config())
        assert errors == []


# ═══════════════════════════════════════════════
#  8. create_live_engine 装配前拦截
# ═══════════════════════════════════════════════

class TestEngineComplianceGate:
    def test_engine_live未备案_ValueError(self) -> None:
        config = {
            "live": {"trade_mode": "live", "name": "t10", "initial_capital": 100000},
            "broker": {"type": "qmt", "account": "ACC-0001", "mode": "LIVE"},
            "execution": {},
            "market_data": {},
            "strategy": {},
            "position": {},
            "risk_control": {},
            "state_store": {"auto_recover_on_start": False},  # KP-P5-08 显式必填对齐
            "scheduler": {},
            "lookahead_guard": {},
            **_empty_config(),
        }
        with pytest.raises(ValueError, match="合规门禁"):
            create_live_engine(config)

    def test_engine_非live_不触发合规(self) -> None:
        """paper 模式不受合规门禁影响（向后兼容）。"""
        config = {
            "live": {"trade_mode": "paper", "name": "t10", "initial_capital": 100000},
            "broker": {"type": "paper"},
            "execution": {},
            "market_data": {},
            "strategy": {},
            "position": {},
            "risk_control": {},
            "state_store": {"auto_recover_on_start": False},  # KP-P5-08 显式必填对齐
            "scheduler": {},
            "lookahead_guard": {},
        }
        engine = create_live_engine(config)
        assert engine is not None


# ═══════════════════════════════════════════════
#  9. 备案快照落盘
# ═══════════════════════════════════════════════

class TestSnapshotDump:
    def test_dump_snapshot_写JSON(self, tmp_path) -> None:
        path = tmp_path / "compliance_snapshot.json"
        snap = ComplianceGate(_compliant_config()).dump_snapshot(str(path))
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["overall"] is True
        assert snap["overall"] is True
        assert data["snapshot"]["filing_status"] == "approved"
