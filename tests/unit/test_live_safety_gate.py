# 文件名：tests/unit/test_live_safety_gate.py
"""
test_live_safety_gate.py - 实盘安全门禁第0阶段验收测试（回写#287）
==========================================================================
覆盖：验收项逐项断言 / 隔离环境重建跨 run 去重 / 报告 JSON 可序列化
字段齐全 / 异常不吞如实记录 failed。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from myquant.acceptance.live_safety_gate import GATE_ITEMS, LiveSafetyGateRunner


@pytest.fixture(scope="module")
def gate_runner() -> LiveSafetyGateRunner:
    """验收执行器（模块级共享；每次 run 内部重建隔离环境）。"""
    return LiveSafetyGateRunner()


def test_gate_items_registered() -> None:
    """已登记验收项覆盖 0.1 施工项（用例9/10 + F-2）。"""
    assert "0.1-9" in GATE_ITEMS
    assert "0.1-10" in GATE_ITEMS
    assert "0.1-F2" in GATE_ITEMS


def test_gate_01_all_pass(gate_runner: LiveSafetyGateRunner) -> None:
    """0.1 三项验收全通过（引擎隔离 / 熔断动作链 / 总线降级）。"""
    report: Dict[str, Any] = gate_runner.run(mode="full")
    assert report["summary"]["failed"] == 0
    assert report["summary"]["total"] == len(GATE_ITEMS)
    for it in report["items"]:
        assert it["pass"] is True, f"{it['item']} 未通过: {it['detail']}"


def test_gate_report_serializable_fields(gate_runner: LiveSafetyGateRunner) -> None:
    """报告 JSON 可序列化且字段齐全（item/pass/detail/threshold_source）。"""
    report: Dict[str, Any] = gate_runner.run(mode="full")
    json.dumps(report)  # 不可序列化会抛 TypeError
    assert {"mode", "items", "summary", "generated_at", "caveats"} <= set(report.keys())
    assert {"passed", "failed", "total"} <= set(report["summary"].keys())
    for it in report["items"]:
        assert {"item", "pass", "detail", "threshold_source"} <= set(it.keys())


def test_gate_rerun_rebuilds_isolation(gate_runner: LiveSafetyGateRunner) -> None:
    """跨 run 重建隔离：连续两次 run 结果均通过且互不污染。"""
    r1: Dict[str, Any] = gate_runner.run(mode="full")
    r2: Dict[str, Any] = gate_runner.run(mode="full")
    assert r1["summary"]["failed"] == 0
    assert r2["summary"]["failed"] == 0
    # 熔断实例为每次新建，触发计数不应累积
    for it in r2["items"]:
        assert it["pass"] is True


def test_gate_exception_not_swallowed(
    gate_runner: LiveSafetyGateRunner, monkeypatch: Any,
) -> None:
    """异常不吞：检查项抛异常时如实记录该项 failed 而非中断/静默。"""

    def _boom(self) -> Dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        LiveSafetyGateRunner, "_check_01_engine_isolation", _boom,
    )
    report: Dict[str, Any] = gate_runner.run(mode="full")
    item = next(it for it in report["items"] if it["item"] == "0.1-9")
    assert item["pass"] is False
    assert "RuntimeError" in item["detail"]
    assert report["summary"]["failed"] >= 1
