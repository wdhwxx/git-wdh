# 文件名：tests/unit/test_chaos_drill.py
"""
D-6 混沌工程与故障演练 单测
==========================
覆盖 ChaosDrillRunner 核心编排（隔离环境注入→观测→计时→分类→记录→复盘）、
P0-8 用例库复用（R-01 同库）、FI-07 外部心跳临界抖动不误判（R-08）、
复盘报告落盘。

基准（复用既有测试文件命名与防未来/隔离模式）：全部使用系统时钟已发生时间戳，
isolated 临时 AlertStore + 真实 AlertManager；隔离环境，不触碰工作区真实库。
"""
import json
import time

import pytest

from myquant.core.constants import SeverityLevel
from myquant.acceptance.chaos_drill import (
    CLASSIFICATION_FN,
    CLASSIFICATION_UNKNOWN,
    ChaosDrillRunner,
    DEFAULT_OBSERVE_WINDOW_SECONDS,
    DrillCase,
    DrillCaseResult,
    InjectOutcome,
    build_builtin_cases,
    _budget_for,
)
from myquant.core.measurability import (
    CLASSIFICATION_TP,
    FI_CASES,
    list_fi_cases,
)
from myquant.monitoring.meta_monitor import (
    ExternalHeartbeatGuard,
    FileHeartbeatCarrier,
)


# ═══════════════════════════════════════════════
#  P0-8 用例库复用（R-01：演练/验收同库，不另立用例）
# ═══════════════════════════════════════════════
class TestFiRegistry:
    def test_p0_8_cases_cover_fi_01_to_07(self):
        """P0-8 用例库必须恰好含 FI-01~07（字段唯一口径）。"""
        ids = {c.case_id for c in FI_CASES}
        assert ids == {"FI-01", "FI-02", "FI-03", "FI-04", "FI-05", "FI-06", "FI-07"}

    def test_p0_8_case_fields_complete(self):
        """每个 FI 用例字段齐全（fault_desc/expected_capture/kpi…）。"""
        for c in FI_CASES:
            assert c.case_id
            assert c.fault_desc
            assert c.expected_capture
            assert c.kpi

    def test_list_fi_cases_returns_same_library(self):
        """list_fi_cases() 与 FI_CASES 同源（脚本入库用）。"""
        rows = list_fi_cases()
        assert {r["case_id"] for r in rows} == {c.case_id for c in FI_CASES}


# ═══════════════════════════════════════════════
#  预算解析
# ═══════════════════════════════════════════════
class TestBudgetForKpi:
    def test_mttd_budget(self):
        b = _budget_for("MTTD<1min,MTTA<5min")
        assert b["mttd_budget"] == 60.0
        assert b["mtta_budget"] == 300.0

    def test_kpi8_no_budget(self):
        b = _budget_for("无计时预算")
        assert b["mttd_budget"] is None
        assert b["mtta_budget"] is None
        assert b["mttr_budget"] is None


# ═══════════════════════════════════════════════
#  ChaosDrillRunner 核心编排（fake 注入器，确定性）
# ═══════════════════════════════════════════════
class TestRunnerCore:
    def _make_runner(self, store_db_path=None, clock=None):
        return ChaosDrillRunner(store_db_path=store_db_path, clock=clock)

    @staticmethod
    def _last_alert_id(runner) -> str:
        """经 manager._history 定位最近一条告警 ID（与 runner 捕获同源）。"""
        hist = list(getattr(runner.manager, "_history", []))
        return getattr(hist[-1], "alert_id", "")

    def _fake_tp_case(self, fi, captured_alert_id_provider, **kw):
        def inject():
            return InjectOutcome(fault_ts=time.time(),
                                 captured_alert_id=captured_alert_id_provider(), **kw)
        return DrillCase(fi=fi, inject_fn=inject,
                         observe_window_seconds=DEFAULT_OBSERVE_WINDOW_SECONDS)

    def test_tp_timing_and_classification(self, tmp_path):
        """捕获即 TP；MTTD/MTTA/MTTR 按 P0-8 ② 计时并落库确认/解决。"""
        db = str(tmp_path / "drill.db")
        runner = self._make_runner(store_db_path=db)
        fault_ts = time.time()
        captured = {}

        def inject():
            runner.manager.alert(
                level=SeverityLevel.CRITICAL, message="drill TP",
                source="fake", category="fake", deduplication_key="tp",
            )
            captured["alert_id"] = self._last_alert_id(runner)
            return InjectOutcome(fault_ts=fault_ts,
                                 captured_alert_id=captured["alert_id"])

        fi = next(c for c in FI_CASES if c.case_id == "FI-01")
        runner.register_case(DrillCase(fi=fi, inject_fn=inject))
        report = runner.run()
        res = report["results"][0]
        assert res["detected"] is True
        assert res["classification"] == CLASSIFICATION_TP
        assert res["fault_ts"] == fault_ts
        assert res["created_at"] >= fault_ts
        assert res["mttd"] >= 0
        assert res["mtta"] >= 0
        assert res["mttr"] >= 0
        # 告警落库行状态闭环为 RESOLVED（演练模拟确认/解决）
        row = runner.store.get(captured["alert_id"])
        assert row is not None
        assert row.get("acked_at") is not None
        assert row.get("resolved_at") is not None
        runner.close()

    def test_miss_is_fn(self, tmp_path):
        """注入后未捕获（故障未被告警）→ 漏报 FN，不计时。"""
        runner = self._make_runner(store_db_path=str(tmp_path / "m.db"))

        def inject():
            return InjectOutcome(fault_ts=time.time())  # 不产生任何告警

        fi = next(c for c in FI_CASES if c.case_id == "FI-01")
        runner.register_case(DrillCase(fi=fi, inject_fn=inject))
        report = runner.run()
        res = report["results"][0]
        assert res["detected"] is False
        assert res["classification"] == CLASSIFICATION_FN
        assert res["mttd"] is None
        runner.close()

    def test_inject_error_is_fn(self, tmp_path):
        """注入函数抛异常 → 如实记录为漏报 FN 并附 note，不阻断其他用例。"""
        runner = self._make_runner(store_db_path=str(tmp_path / "e.db"))

        def inject():
            raise RuntimeError("drill inject failed")

        fi = next(c for c in FI_CASES if c.case_id == "FI-01")
        runner.register_case(DrillCase(fi=fi, inject_fn=inject))
        report = runner.run()
        res = report["results"][0]
        assert res["classification"] == CLASSIFICATION_FN
        assert "注入失败" in res["note"]
        runner.close()

    def test_duplicate_register_rejected(self, tmp_path):
        """同一 case_id 重复注册被拒绝。"""
        runner = self._make_runner(store_db_path=str(tmp_path / "d.db"))
        fi = next(c for c in FI_CASES if c.case_id == "FI-01")
        case = DrillCase(fi=fi, inject_fn=lambda: InjectOutcome())
        runner.register_case(case)
        with pytest.raises(ValueError):
            runner.register_case(case)
        runner.close()

    def test_write_report_json(self, tmp_path):
        """复盘报告落盘为可解析 JSON，含 summary 与逐用例数据。"""
        out = tmp_path / "drill_report.json"
        runner = self._make_runner(store_db_path=str(tmp_path / "w.db"))

        def inject():
            fault_ts = time.time()  # 先记录故障注入时刻（已发生）
            runner.manager.alert(
                level=SeverityLevel.CRITICAL, message="report",
                source="fake", category="fake", deduplication_key="rep",
            )
            return InjectOutcome(fault_ts=fault_ts,
                                 captured_alert_id=self._last_alert_id(runner))

        fi = next(c for c in FI_CASES if c.case_id == "FI-01")
        runner.register_case(DrillCase(fi=fi, inject_fn=inject))
        runner.run()
        p = runner.write_report(str(out))
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "summary" in data and "results" in data
        assert data["summary"]["cases_total"] == 1
        assert data["summary"]["detect_rate"] == 1.0
        assert p.exists()
        runner.close()

    def test_budget_met_flags(self, tmp_path):
        """KPI 带预算的用例，满足预算则 met_* 置 True。"""
        runner = self._make_runner(store_db_path=str(tmp_path / "b.db"))

        def inject():
            fault_ts = time.time()  # 先记录故障注入时刻（已发生）
            runner.manager.alert(
                level=SeverityLevel.CRITICAL, message="b",
                source="fake", category="fake", deduplication_key="budget",
            )
            return InjectOutcome(fault_ts=fault_ts,
                                 captured_alert_id=self._last_alert_id(runner))

        fi = next(c for c in FI_CASES if c.case_id == "FI-01")
        runner.register_case(DrillCase(fi=fi, inject_fn=inject))
        report = runner.run()
        res = report["results"][0]
        assert res["budgets"]["mttd_budget"] == 60.0
        assert res["budgets"]["met_mttd"] is True
        runner.close()


# ═══════════════════════════════════════════════
#  内置用例接线（真实路径可演示）
# ═══════════════════════════════════════════════
class TestBuiltinCases:
    def test_all_builtin_registered(self):
        """register_builtin_cases 注册 FI-01~07 全部用例，顺序按 P0-8。"""
        runner = ChaosDrillRunner()
        n = runner.register_builtin_cases()
        assert n == 7
        assert runner.case_ids == ["FI-01", "FI-02", "FI-03",
                                   "FI-04", "FI-05", "FI-06", "FI-07"]
        runner.close()

    def test_builtin_cases_all_captured(self):
        """内置 7 用例注入后均被告警捕获 → TP（可演示最小可行版本）。"""
        runner = ChaosDrillRunner()
        runner.register_builtin_cases()
        report = runner.run()
        assert report["summary"]["cases_total"] == 7
        assert report["summary"]["tp"] == 7
        assert report["summary"]["fn"] == 0
        runner.close()

    def test_builtin_select_subset(self):
        """select 过滤：只演练 FI-01/FI-06 两项。"""
        runner = ChaosDrillRunner()
        runner.register_builtin_cases(select=("FI-01", "FI-06"))
        report = runner.run(select=("FI-01", "FI-06"))
        assert {r["case_id"] for r in report["results"]} == {"FI-01", "FI-06"}
        runner.close()


# ═══════════════════════════════════════════════
#  FI-07 外部心跳：临界网络抖动不误判（R-08）
# ═══════════════════════════════════════════════
class TestExternalHeartbeatNoFalsePositive:
    def test_single_timeout_not_dead(self, tmp_path):
        """临界网络抖动（单次超时）不判死主进程（R-08：连续超时才判死）。"""
        carrier = FileHeartbeatCarrier(directory=str(tmp_path))
        guard = ExternalHeartbeatGuard(
            interval_seconds=1.0, consecutive_timeout=3, timeout_factor=2.0,
        )
        fault_ts = time.time()
        carrier.send_heartbeat({"ts": fault_ts, "status": "ok"})
        # 仅一次超过 timeout（1*2.0=2.0s）的抖动 → degraded，不 dead
        _alive, status, _missed = guard.check_alive(carrier, now=fault_ts + 2.5)
        carrier.close()
        assert status == "degraded"

    def test_consecutive_timeouts_dead(self, tmp_path):
        """连续多次超时达阈值才判死（FI-07 kill 真实判死路径）。"""
        carrier = FileHeartbeatCarrier(directory=str(tmp_path))
        guard = ExternalHeartbeatGuard(
            interval_seconds=1.0, consecutive_timeout=3, timeout_factor=2.0,
        )
        fault_ts = time.time()
        carrier.send_heartbeat({"ts": fault_ts, "status": "ok"})
        # kill：心跳停写；3 次连续超时 → dead
        for i in range(1, 4):
            guard.check_alive(carrier, now=fault_ts + i * 2.0 + 0.1)
        _alive, status, _missed = guard.check_alive(carrier, now=fault_ts + 4.0 + 0.1)
        carrier.close()
        assert status == "dead"
