# -*- coding: utf-8 -*-
"""T-43 KP-P2-07 幸存者偏差工具接入：批量回测收尾注入 data_coverage + 报告强制渲染声明节。"""
import os
from types import SimpleNamespace

from myquant.backtest.batch_backtest import BatchBacktester, BatchBacktestResult


def _bt():
    # 绕过 __init__ 防御校验，只测 _build_data_coverage（纯局部 import + 降级逻辑）
    return BatchBacktester.__new__(BatchBacktester)


def test_build_data_coverage_merges_audit_and_discount(monkeypatch):
    fake_audit = SimpleNamespace(to_dict=lambda: {"db_symbol_count": 123, "statement": "S", "delisted_coverage": False})
    monkeypatch.setattr(
        "myquant.backtest.survivorship_bias.audit_data_coverage",
        lambda **k: fake_audit,
    )
    monkeypatch.setattr(
        "myquant.backtest.survivorship_bias.estimate_survivorship_discount",
        lambda **k: {"discount": 0.012, "discount_annualized": 0.005},
    )
    dc = _bt()._build_data_coverage()
    assert dc["db_symbol_count"] == 123
    assert dc["discount"] == 0.012
    assert "statement" in dc


def test_build_data_coverage_discount_error_degrades(monkeypatch):
    fake_audit = SimpleNamespace(to_dict=lambda: {"db_symbol_count": 5, "statement": "S"})
    monkeypatch.setattr("myquant.backtest.survivorship_bias.audit_data_coverage", lambda **k: fake_audit)

    def boom(**k):
        raise ValueError("样本不足")

    monkeypatch.setattr("myquant.backtest.survivorship_bias.estimate_survivorship_discount", boom)
    dc = _bt()._build_data_coverage()
    assert dc["discount"] is None
    assert "discount_error" in dc


def test_build_data_coverage_audit_failure_returns_none(monkeypatch):
    def boom(**k):
        raise FileNotFoundError("quant.db 缺失")

    monkeypatch.setattr("myquant.backtest.survivorship_bias.audit_data_coverage", boom)
    assert _bt()._build_data_coverage() is None


def test_batch_backtest_result_default_none():
    r = BatchBacktestResult(plan=None, per_symbol={}, elapsed_seconds=0.0,
                            degradation_events=[], memory_level="ok", batch_count=1)
    assert r.data_coverage is None


def test_batch_backtest_result_field_exists():
    r = BatchBacktestResult(plan=None, per_symbol={}, elapsed_seconds=0.0,
                            degradation_events=[], memory_level="ok", batch_count=1,
                            data_coverage={"db_symbol_count": 9})
    assert r.data_coverage["db_symbol_count"] == 9


def test_run_injects_data_coverage_src():
    src = os.path.join(os.path.dirname(__file__), "..", "..", "myquant", "backtest", "batch_backtest.py")
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "data_coverage = self._build_data_coverage()" in text
    assert "data_coverage=data_coverage" in text
    assert "def _build_data_coverage" in text


def test_full_report_forces_coverage_src():
    src = os.path.join(os.path.dirname(__file__), "..", "..", "myquant", "backtest", "batch_report.py")
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "data_coverage = audit_data_coverage().to_dict()" in text
    assert "强制渲染数据口径声明节" in text