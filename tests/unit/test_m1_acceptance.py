# 文件名：tests/unit/test_m1_acceptance.py
"""
test_m1_acceptance.py - M1 保命验收单元测试（A-15-Min / A-15-Full）
==========================================================================
覆盖：
  1. 保命闭环四环覆盖率检查 = 100%（行情活性/券商断连/交易异常/持仓对账）。
  2. A-15-Min：覆盖率 100% + FI-01/03/04/05 注入检出且走事件路径。
  3. A-15-Full：FI-01~05 全部检出 + MTTx 满足预算 + EVENT 事件路径 +
     分类（TP/FP/FN）达标；时间戳单调（fault_ts<=created_at<=acked_at<=resolved_at，防未来）。
  4. 同一 runner 连跑 min→full 去重隔离（回归修复：跨 run 同去重键告警不误取/不抑制）。
  5. 非法 mode 抛 ValueError。

依赖: pytest, myquant.monitoring.m1_acceptance。
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from myquant.monitoring.m1_acceptance import M1AcceptanceRunner


@pytest.fixture()
def runner() -> M1AcceptanceRunner:
    """临时告警库隔离的验收执行器（自动清理）。"""
    r = M1AcceptanceRunner()
    yield r
    r.close()


def _assert_timestamps_monotonic(fi: Dict[str, Any]) -> None:
    """防未来：故障注入与告警时间戳必须单调递增（不引用未来）。"""
    if not fi.get("captured"):
        return
    fault = fi["fault_ts"]
    created = fi["created_at"]
    assert created >= fault, f"{fi['case_id']} created_at 早于 fault_ts"
    if fi.get("acked_at") is not None:
        assert fi["acked_at"] >= created, f"{fi['case_id']} acked_at 早于 created_at"
    if fi.get("resolved_at") is not None:
        assert fi["resolved_at"] >= fault, f"{fi['case_id']} resolved_at 早于 fault_ts"


class TestCoverage:
    def test_四环覆盖率100(self, runner: M1AcceptanceRunner) -> None:
        coverage = runner.check_coverage()
        assert coverage["covered"] == coverage["total"] == 4
        assert coverage["coverage_pct"] == 1.0
        assert all(ring["covered"] for ring in coverage["rings"])

    def test_各环证据非空(self, runner: M1AcceptanceRunner) -> None:
        coverage = runner.check_coverage()
        for ring in coverage["rings"]:
            assert ring["ring"] and ring["item"] and ring["evidence"]


class TestMinAcceptance:
    def test_min通过_覆盖率100_保命用例检出(self, runner: M1AcceptanceRunner) -> None:
        rep = runner.run("min")
        assert rep["mode"] == "min"
        assert rep["verdict"]["pass"] is True
        assert rep["coverage"]["coverage_pct"] == 1.0
        # A-15-Min 要求 FI-01/03/04/05 全部检出走事件路径
        assert all(r["captured"] for r in rep["fi"])
        assert set(r["case_id"] for r in rep["fi"]) == {"FI-01", "FI-03", "FI-04", "FI-05"}
        for r in rep["fi"]:
            assert r["event_path"], f"{r['case_id']} 未走事件路径"
            _assert_timestamps_monotonic(r)

    def test_min_fi时间戳单调(self, runner: M1AcceptanceRunner) -> None:
        rep = runner.run("min")
        for r in rep["fi"]:
            _assert_timestamps_monotonic(r)


class TestFullAcceptance:
    def test_full通过_MTTx预算与分类达标(self, runner: M1AcceptanceRunner) -> None:
        rep = runner.run("full")
        assert rep["mode"] == "full"
        v = rep["verdict"]
        assert v["pass"] is True
        assert v["coverage_ok"] is True
        assert v["fi_captured"] is True
        assert v["mttd_ok"] is True
        assert v["mtta_ok"] is True
        assert v["mttr_ok"] is True
        assert v["event_path_ok"] is True
        assert v["accuracy_ok"] is True
        assert v["fp_rate_ok"] is True
        # 五用例全覆盖
        assert set(r["case_id"] for r in rep["fi"]) == {"FI-01", "FI-02", "FI-03", "FI-04", "FI-05"}
        assert all(r["captured"] for r in rep["fi"])

    def test_full_MTTx预算逐用例(self, runner: M1AcceptanceRunner) -> None:
        rep = runner.run("full")
        budgets = {"mttd": 60.0, "mtta": 300.0, "mttr": 1800.0}
        for r in rep["fi"]:
            assert r["captured"]
            for key, budget in budgets.items():
                val = r.get(f"{key}_seconds")
                assert val is not None, f"{r['case_id']} 缺 {key}"
                assert val < budget, f"{r['case_id']} {key}={val}s >= {budget}s"

    def test_full_时间戳单调防未来(self, runner: M1AcceptanceRunner) -> None:
        rep = runner.run("full")
        for r in rep["fi"]:
            _assert_timestamps_monotonic(r)

    def test_full_事件路径非巡检(self, runner: M1AcceptanceRunner) -> None:
        """EVENT 项必须经事件路径验证（fault_ts→created_at 即时，远小于 60s 巡检）。"""
        rep = runner.run("full")
        for r in rep["fi"]:
            assert r["event_path"]
            # 检测即时发生（系统时间戳，事件路径而非等满周期）
            assert r["mttd_seconds"] < 60.0

    def test_full_分类口径(self, runner: M1AcceptanceRunner) -> None:
        rep = runner.run("full")
        cls = rep["verdict"]["classification"]
        assert cls["tp"] >= 1
        assert cls["fp"] == 0
        assert cls["fn"] == 0
        assert cls["accuracy"] == 1.0
        assert cls["detection_rate"] == 1.0


class TestIsolationAndGuard:
    def test_同runner连跑min_full_去重隔离(self) -> None:
        """回归：同一 runner 先 min 后 full，跨 run 同去重键告警不被抑制/不误取。"""
        r = M1AcceptanceRunner()
        try:
            rep_min = r.run("min")
            rep_full = r.run("full")
            assert rep_min["verdict"]["pass"] is True
            assert rep_full["verdict"]["pass"] is True
            assert rep_full["verdict"]["fi_captured"] is True
            # full 阶段 FI-01~05 全部有本轮新告警（非复用上轮）
            for case in rep_full["fi"]:
                assert case["captured"]
                _assert_timestamps_monotonic(case)
        finally:
            r.close()

    def test_非法mode抛错(self, runner: M1AcceptanceRunner) -> None:
        with pytest.raises(ValueError):
            runner.run("invalid")

    def test_report结构完整(self, runner: M1AcceptanceRunner) -> None:
        rep = runner.run("min")
        for key in ("mode", "coverage", "fi", "verdict"):
            assert key in rep
        assert isinstance(rep["fi"], list) and len(rep["fi"]) >= 4
