# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_candidate_report.py
"""
候选池报告生成（myquant/backtest/candidate_report.py）单元测试（G0-9 补测）
==========================================================
覆盖：
  - _compute_stats：平均综合夏普/收益/双正占比/Top 计数
  - _histogram_data：直方图分箱 / 空输入
  - generate_html：写出 HTML 文件、返回路径、含标题
  - generate_full_report：空池抛 ValueError
  - generate_pdf：reportlab 可用时执行（缺失则 importorskip 跳过）
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from myquant.backtest.candidate_report import (
    CandidatePoolReportGenerator,
    CandidateReportMeta,
)


def _pool() -> pd.DataFrame:
    return pd.DataFrame([
        {"symbol": "000001.SZ", "rsrs_ret": 0.10, "rsrs_sharpe": 1.0,
         "rsrs_max_drawdown": 0.05, "rsrs_trades": 20,
         "ma_cross_ret": 0.08, "ma_cross_sharpe": 1.2,
         "ma_cross_max_drawdown": 0.04, "ma_cross_trades": 18,
         "avg_sharpe": 1.1, "is_top30": True},
        {"symbol": "000002.SZ", "rsrs_ret": 0.05, "rsrs_sharpe": 0.7,
         "rsrs_max_drawdown": 0.06, "rsrs_trades": 15,
         "ma_cross_ret": 0.04, "ma_cross_sharpe": 0.8,
         "ma_cross_max_drawdown": 0.05, "ma_cross_trades": 12,
         "avg_sharpe": 0.75, "is_top30": False},
        {"symbol": "000003.SZ", "rsrs_ret": -0.03, "rsrs_sharpe": 0.5,
         "rsrs_max_drawdown": 0.09, "rsrs_trades": 10,
         "ma_cross_ret": 0.02, "ma_cross_sharpe": 0.5,
         "ma_cross_max_drawdown": 0.08, "ma_cross_trades": 11,
         "avg_sharpe": 0.5, "is_top30": False},
    ])


def _meta(top_n: int = 2) -> CandidateReportMeta:
    return CandidateReportMeta(
        strategy_pair="rsrs × ma_cross",
        min_sharpe=0.5, min_trades=10, max_drawdown=0.10, top_n=top_n,
    )


class TestComputeStats:
    def test_stats_computed(self) -> None:
        gen = CandidatePoolReportGenerator()
        (
            avg_sharpe, avg_rsrs_ret, avg_ma_ret,
            avg_rsrs_sharpe, avg_ma_sharpe, both_positive, top_count,
        ) = gen._compute_stats(_pool())
        assert avg_sharpe == pytest.approx((1.1 + 0.75 + 0.5) / 3)
        assert both_positive == pytest.approx(2 / 3)  # 前两只双正
        assert top_count == 1  # 仅 000001 标记 True


class TestHistogramData:
    def test_returns_labels_and_counts(self) -> None:
        labels, counts = CandidatePoolReportGenerator._histogram_data(
            [1.0, 0.5, 0.75, 0.6], bins=4,
        )
        assert len(labels) == len(counts) == 4
        assert sum(counts) == 4

    def test_empty_input(self) -> None:
        labels, counts = CandidatePoolReportGenerator._histogram_data([])
        assert labels == []
        assert counts == []


class TestGenerateHtml:
    def test_writes_html_and_returns_path(self, tmp_path: Path) -> None:
        gen = CandidatePoolReportGenerator(title="候选池报告")
        out = tmp_path / "report" / "pool.html"
        path = gen.generate_html(_pool(), _meta(), out)
        assert Path(path) == out
        assert out.is_file()
        html = out.read_text(encoding="utf-8")
        assert "候选池报告" in html
        assert "000001.SZ" in html


class TestGenerateFullReport:
    def test_empty_pool_raises(self, tmp_path: Path) -> None:
        gen = CandidatePoolReportGenerator()
        with pytest.raises(ValueError, match="候选池为空"):
            gen.generate_full_report(
                pd.DataFrame(), _meta(), tmp_path / "pool.csv",
            )


class TestGeneratePdf:
    def test_pdf_generated_when_reportlab_available(self, tmp_path: Path) -> None:
        reportlab = pytest.importorskip("reportlab")
        gen = CandidatePoolReportGenerator()
        out = tmp_path / "pool.pdf"
        gen.generate_pdf(_pool(), _meta(), out)
        assert out.is_file()
