# -*- coding: utf-8 -*-
"""覆盖率门禁测试（P1，v1.2 §6.2 P1-6 / §8 V-9）。

覆盖：分层归属正确性、分层清单 vs 全量清单 diff 为空（V-9）、C-1a/C-1b 总量棘轮
边界、C-2 分层下限、C-3 关键文件下限、C-4 新增文件反红、C-5 语句棘轮反红、
C-7 跳过预算、coverage.json 缺失 fail-closed、报告模式标记。
判据用构造的 fake coverage.json（不依赖真实运行产物），仅 V-9 分层清单校验真实项目。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# 注：用 import 全路径形式（而非 from-import），使 R-19 引用级解析能识别本模块被测试引用
import myquant.leakage_gate.coverage_gate as cg

# 项目根：tests/unit/test_coverage_gate.py → 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def relaxed_baselines(monkeypatch: pytest.MonkeyPatch) -> None:
    """放松 C-1a/C-1b 基线到可控小值（fake 数据仅几百行，避免误触真实基线）。"""
    monkeypatch.setattr(cg, "COVERED_LINES_BASELINE", 50)
    monkeypatch.setattr(cg, "COVERAGE_BASELINE", 60.0)


def _mk_file(
    statements: int,
    covered: int,
    *,
    missing: list[int] | None = None,
) -> dict[str, object]:
    """构造 coverage.json 单个文件条目。"""
    return {
        "summary": {
            "covered_lines": covered,
            "num_statements": statements,
            "percent_covered": round(covered * 100.0 / statements, 1),
            "missing_lines": missing or [],
            "excluded_lines": [],
        },
    }


def _write_fake_coverage(
    tmp_path: Path,
    files: dict[str, dict[str, object]],
    totals: dict[str, object],
) -> Path:
    """写一份构造的 coverage.json（files 键为 "myquant/<rel>"）。"""
    data = {
        "meta": {"format": 3, "version": "7.x"},
        "files": files,
        "totals": totals,
    }
    p = tmp_path / "coverage.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ── 1. 分层归属正确性 ────────────────────────────────────
@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        # T0：精确文件 + 目录前缀
        ("backtest/engine.py", "T0"),
        ("backtest/mixed_engine.py", "T0"),
        ("backtest/vectorized_engine.py", "T0"),
        ("strategies/kunpeng_strategy.py", "T0"),
        ("live/data_feed.py", "T0"),
        ("leakage_gate/gate.py", "T0"),
        ("realism_gate/manifest.py", "T0"),
        ("risk/risk_manager.py", "T0"),
        ("execution/execution_router.py", "T0"),
        ("engine/base.py", "T0"),
        # 子包 __init__ 归目录层
        ("leakage_gate/__init__.py", "T0"),
        # T1
        ("core/event_bus.py", "T1"),
        ("data_access/manager.py", "T1"),
        ("database/session.py", "T1"),
        ("monitoring/metrics_collector.py", "T1"),
        ("models/market_data.py", "T1"),
        ("indicators/rsrs.py", "T1"),
        ("ml/features.py", "T1"),
        ("strategies/ma_cross.py", "T1"),
        ("backtest/order_matcher.py", "T1"),
        # T2
        ("api/routes.py", "T2"),
        ("web/dashboard.py", "T2"),
        ("gui/main_window.py", "T2"),
        ("utils/file_utils.py", "T2"),
        ("acceptance/acceptance_router.py", "T2"),
        # T3：迁移/自测/顶层包元数据
        ("database/migrations/0008_add_value_encrypted.py", "T3"),
        ("utils/chaos_drill_selftest.py", "T3"),
        ("__init__.py", "T3"),
        # 未知目录显式暴露
        ("mystery/new_module.py", None),
    ],
)
def test_tier_of(rel: str, expected: str | None) -> None:
    assert cg.tier_of(rel) == expected


# ── 2. V-9：分层清单 vs 全量清单 diff 为空 ────────────────
def test_manifest_covers_all_files() -> None:
    """真实项目：myquant/**/*.py 全部归层且无 UNMAPPED。"""
    src = PROJECT_ROOT / "myquant"
    all_py = {p.relative_to(src).as_posix() for p in src.rglob("*.py")}
    manifest = cg.build_tier_manifest(PROJECT_ROOT)
    assert set(manifest) == all_py, "分层清单与文件系统清单不一致（diff 非空）"
    unmapped = [rel for rel, tier in manifest.items() if tier == "UNMAPPED"]
    assert not unmapped, f"存在未分层文件: {unmapped}"


# ── 3. C-1a/C-1b 总量棘轮 ───────────────────────────────
def test_pass_when_meets_baselines(tmp_path: Path, relaxed_baselines: None) -> None:
    files = {
        "myquant/backtest/engine.py": _mk_file(100, 90),
        "myquant/core/event_bus.py": _mk_file(100, 85),
    }
    totals = {"num_statements": 200, "covered_lines": 175, "percent_covered": 87.5}
    cov = _write_fake_coverage(tmp_path, files, totals)
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert report.ok, report.violations
    assert report.total_percent == 87.5


def test_c1a_red_when_lines_below(tmp_path: Path) -> None:
    files = {"myquant/backtest/engine.py": _mk_file(100, 80)}
    totals = {
        "num_statements": 100,
        "covered_lines": 80,
        "percent_covered": 80.0,
    }
    cov = _write_fake_coverage(tmp_path, files, totals)
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert not report.ok
    assert any(v.startswith("C-1a") for v in report.violations)


def test_c1b_red_when_percent_below(tmp_path: Path) -> None:
    files = {"myquant/backtest/engine.py": _mk_file(100, 60)}
    totals = {
        "num_statements": 100,
        "covered_lines": 60,
        "percent_covered": 60.0,
    }
    cov = _write_fake_coverage(tmp_path, files, totals)
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert not report.ok
    assert any(v.startswith("C-1b") for v in report.violations)


# ── 4. C-2 分层下限 ─────────────────────────────────────
def test_c2_red_when_tier_below(tmp_path: Path) -> None:
    # T0 文件仅 50%（< 68 下限）
    files = {"myquant/backtest/engine.py": _mk_file(100, 50)}
    totals = {"num_statements": 100, "covered_lines": 50, "percent_covered": 50.0}
    cov = _write_fake_coverage(tmp_path, files, totals)
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert not report.ok
    assert any(v.startswith("C-2 T0") for v in report.violations)


# ── 5. C-3 关键文件下限 ─────────────────────────────────
def test_c3_red_when_key_file_below(tmp_path: Path) -> None:
    # engine.py floor=85；构造 50%
    files = {
        "myquant/backtest/engine.py": _mk_file(100, 50),
        "myquant/core/event_bus.py": _mk_file(100, 90),
        "myquant/data_access/manager.py": _mk_file(100, 90),
    }
    totals = {"num_statements": 300, "covered_lines": 230, "percent_covered": 76.7}
    cov = _write_fake_coverage(tmp_path, files, totals)
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert not report.ok
    assert any(v.startswith("C-3 backtest/engine.py") for v in report.violations)


# ── 6. C-4 新增文件反红 ─────────────────────────────────
def test_c4_red_for_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 存量注册表：engine/event_bus/manager 已冻结；live/new_gate_module.py 未注册 → 新增
    monkeypatch.setattr(cg, "FILE_STMT_CEILING", {
        "backtest/engine.py": 999,
        "core/event_bus.py": 999,
        "data_access/manager.py": 999,
    })
    monkeypatch.setattr(cg, "T2_FILES", frozenset({"utils/file_utils.py"}))
    files = {
        "myquant/backtest/engine.py": _mk_file(100, 90),
        "myquant/core/event_bus.py": _mk_file(100, 90),
        "myquant/data_access/manager.py": _mk_file(100, 90),
        "myquant/live/new_gate_module.py": _mk_file(100, 40),  # T0 新增，仅 40% < 60
    }
    totals = {
        "num_statements": 400,
        "covered_lines": 310,
        "percent_covered": 77.5,
    }
    cov = _write_fake_coverage(tmp_path, files, totals)
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert not report.ok
    assert any("C-4 新增文件 live/new_gate_module.py" in v for v in report.violations)


# ── 7. C-5 存量语句数棘轮反红 ────────────────────────────
def test_c5_red_when_statements_grow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cg, "FILE_STMT_CEILING", {"backtest/engine.py": 10})
    files = {"myquant/backtest/engine.py": _mk_file(20, 18)}
    totals = {"num_statements": 20, "covered_lines": 18, "percent_covered": 90.0}
    cov = _write_fake_coverage(tmp_path, files, totals)
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert not report.ok
    assert any(v.startswith("C-5 backtest/engine.py") for v in report.violations)


# ── 8. C-7 跳过预算 ─────────────────────────────────────
def test_c7_red_when_skipped_above_budget(tmp_path: Path) -> None:
    files = {"myquant/backtest/engine.py": _mk_file(100, 90)}
    totals = {"num_statements": 100, "covered_lines": 90, "percent_covered": 90.0}
    cov = _write_fake_coverage(tmp_path, files, totals)
    junit = tmp_path / "test-results.xml"
    junit.write_text('<testsuite tests="10" skipped="12"></testsuite>', encoding="utf-8")
    report = cg.evaluate(cov, junit_xml=junit, project_root=PROJECT_ROOT)
    assert not report.ok
    assert any(v.startswith("C-7") for v in report.violations)


def test_c7_pass_when_within_budget(tmp_path: Path, relaxed_baselines: None) -> None:
    files = {"myquant/backtest/engine.py": _mk_file(100, 90)}
    totals = {"num_statements": 100, "covered_lines": 90, "percent_covered": 90.0}
    cov = _write_fake_coverage(tmp_path, files, totals)
    junit = tmp_path / "test-results.xml"
    junit.write_text('<testsuite tests="10" skipped="2"></testsuite>', encoding="utf-8")
    report = cg.evaluate(cov, junit_xml=junit, project_root=PROJECT_ROOT)
    assert report.ok


# ── 8.5 C-8 季度治理检查（P4 §6.5，告警非阻断）─────────────
def test_c8_warns_when_review_overdue(tmp_path: Path, relaxed_baselines: None,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"myquant/backtest/engine.py": _mk_file(100, 90)}
    totals = {"num_statements": 100, "covered_lines": 90, "percent_covered": 90.0}
    cov = _write_fake_coverage(tmp_path, files, totals)
    monkeypatch.setattr(cg, "BASELINE_LAST_RAISED", "2020-01-01")  # 早已超季度
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert report.ok  # C-8 告警非阻断
    assert report.quarterly_review_due is True
    assert any(w.startswith("C-8") for w in report.warnings)


def test_c8_silent_within_quarter(tmp_path: Path, relaxed_baselines: None,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"myquant/backtest/engine.py": _mk_file(100, 90)}
    totals = {"num_statements": 100, "covered_lines": 90, "percent_covered": 90.0}
    cov = _write_fake_coverage(tmp_path, files, totals)
    monkeypatch.setattr(cg, "BASELINE_LAST_RAISED", "2099-01-01")  # 未来=未到期
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert report.quarterly_review_due is False
    assert report.warnings == []


def test_c8_bad_date_warns(tmp_path: Path, relaxed_baselines: None,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"myquant/backtest/engine.py": _mk_file(100, 90)}
    totals = {"num_statements": 100, "covered_lines": 90, "percent_covered": 90.0}
    cov = _write_fake_coverage(tmp_path, files, totals)
    monkeypatch.setattr(cg, "BASELINE_LAST_RAISED", "not-a-date")
    report = cg.evaluate(cov, project_root=PROJECT_ROOT)
    assert report.quarterly_review_due is True  # 非法日期按到期处理（fail-safe）


# ── 9. fail-closed 与报告模式 ────────────────────────────
def test_fail_closed_when_json_missing(tmp_path: Path) -> None:
    report = cg.evaluate(tmp_path / "missing.json", project_root=PROJECT_ROOT)
    assert not report.ok
    assert any("fail-closed" in v for v in report.violations)


def test_report_only_marked(tmp_path: Path, relaxed_baselines: None) -> None:
    files = {"myquant/backtest/engine.py": _mk_file(100, 90)}
    totals = {"num_statements": 100, "covered_lines": 90, "percent_covered": 90.0}
    cov = _write_fake_coverage(tmp_path, files, totals)
    report = cg.evaluate(cov, project_root=PROJECT_ROOT, report_only=True)
    assert "报告模式" in report.summary()
