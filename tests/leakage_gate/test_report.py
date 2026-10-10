# 文件名：tests/leakage_gate/test_report.py
"""
test_report.py - 泄漏审计报告（M3-01）专项测试
==============================================
覆盖：报告构建（全部审计段）、落盘/读取回读、摘要、保留期、
缺失数据源的空段与来源标记、门禁统计 collect_gate_stats、
与 scripts/清理历史回测输出.py 的衔接（cleanup_service 识别并按期清理）。

R-19 零覆盖：显式子模块 import。
"""

from pathlib import Path

import pytest

import myquant.leakage_gate.report as rp
from myquant.leakage_gate import gate
from myquant.leakage_gate.gate import Violation
from myquant.utils import cleanup_service


def _sample_sections():
    return {
        "strategy_id": "strat_a",
        "decision_time": "2026-10-10",
        "gate_stats": {
            "consumption": {"blocked": 1, "warning": 2, "total": 3},
            "static_scan": {"blocked": 0, "warning": 1, "total": 1},
        },
        "violations": [
            Violation(gate="consumption", rule_id="R11", severity="阻断",
                      message="未注册消费列", strategy_id="strat_a", decision_time="2026-10-10"),
        ],
        "consumption_hits": [
            {"column": "收盘价", "severity": "阻断", "strategy_id": "strat_a"},
        ],
        "truncated_recompute": {"checked": 10, "recomputed": 2},
        "sanity": ["特征最近时间超过 train_end_time"],
        "data_version": {"market_data": 2, "financial_data": 3},
        "lookahead_guard": {"enabled": True, "indicators_recorded": 5},
        "adjust_degradation": ["600000", "000001"],
    }


def test_build_complete_sections():
    rep = rp.build_leakage_report("exp-1", "backtest", **_sample_sections())
    assert rep["meta"]["experiment_id"] == "exp-1"
    assert rep["meta"]["experiment_type"] == "backtest"
    assert rep["meta"]["retention_days"] == rp.REPORT_RETENTION_DAYS
    assert rep["gates"]["consumption"] == {"blocked": 1, "warning": 2, "total": 3}
    assert rep["violations"][0]["rule_id"] == "R11"
    assert rep["consumption"]["hits"][0]["column"] == "收盘价"
    assert rep["truncated_recompute"]["recomputed"] == 2
    assert rep["sanity"] == ["特征最近时间超过 train_end_time"]
    assert rep["data_version"]["market_data"] == 2
    assert rep["lookahead_guard"]["enabled"] is True
    assert sorted(rep["adjust_degradation"]["symbols"]) == ["000001", "600000"]


def test_build_missing_sections_empty_with_source():
    rep = rp.build_leakage_report("exp-2", "training")
    assert rep["gates"] == {}
    assert rep["violations"] == []
    assert rep["consumption"]["hits"] == []
    assert rep["truncated_recompute"] == {}
    assert rep["sanity"] == []
    assert rep["data_version"] == {}
    assert rep["lookahead_guard"] == {}
    # 缺失数据源 → 空清单但保留来源标记（不杜撰）
    assert rep["adjust_degradation"]["symbols"] == []
    assert rep["adjust_degradation"]["source"]


def test_write_and_load_roundtrip(tmp_path):
    rep = rp.build_leakage_report("exp-3", "param_optimization", **_sample_sections())
    path = rp.write_leakage_report(rep, output_dir=tmp_path, timestamp="20261010_120000")
    assert path.name == "leakage_report_20261010_120000.json"
    loaded = rp.load_leakage_report(str(path))
    assert loaded["meta"]["experiment_id"] == "exp-3"
    assert loaded["gates"] == rep["gates"]


def test_write_missing_meta_raises(tmp_path):
    with pytest.raises(ValueError):
        rp.write_leakage_report({"x": 1}, output_dir=tmp_path)


def test_summarize_report():
    rep = rp.build_leakage_report("exp-4", "backtest", **_sample_sections())
    text = rp.summarize_report(rep)
    assert "exp-4" in text
    assert "阻断 1 / 警告 3" in text
    assert "复权降级标的: 2" in text


def test_write_experiment_report_convenience(tmp_path):
    path = rp.write_experiment_report(
        "exp-5", "training", output_dir=tmp_path, strategy_id="s")
    assert path.exists()
    loaded = rp.load_leakage_report(str(path))
    assert loaded["meta"]["strategy_id"] == "s"


def test_collect_gate_stats():
    """门禁统计：按门禁分 severity 计数，不抛错。"""
    gate.reset_gate_checks()
    try:
        gate.register_gate_check("stats_a", lambda sid, dt: [
            Violation(gate="stats_a", rule_id="R99", severity="阻断", message="m"),
            Violation(gate="stats_a", rule_id="R99", severity="警告", message="m"),
        ])
        stats = gate.collect_gate_stats("strat_a", "2026-10-10")
        assert stats["stats_a"] == {"blocked": 1, "warning": 1, "total": 2}
    finally:
        gate.reset_gate_checks()


def test_cleanup_service_integrates_report_pattern():
    """与 scripts/清理历史回测输出.py 衔接：模式识别 + 按期归组清理。"""
    assert "leakage_report_*.json" in cleanup_service.BACKTEST_OUTPUT_PATTERNS
    m = cleanup_service.PERIOD_FILE_PATTERN.match("leakage_report_20261010_120000.json")
    assert m and m.group(2) == "20261010_120000"


def test_plan_outputs_cleanup_prunes_old_report(tmp_path):
    """保留最近 1 期：旧 leakage_report 被清理、新期保留（随回测产物一致）。"""
    out = tmp_path / "outputs"
    out.mkdir(parents=True)
    old = out / "leakage_report_20261010_120000.json"
    new = out / "leakage_report_20261010_130000.json"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    keep_files, delete_files = cleanup_service.plan_outputs_cleanup(out, keep=1)
    assert new in keep_files
    assert old in delete_files
