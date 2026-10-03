# 文件名：tests/unit/test_f15_ann_date_pointin.py
"""
F-15 基本面因子公告日时点化测试
================================
验证 financial_data 新增 ann_date（公告日）后，_build_fundamental_rows
严格按「公告日 <= trade_date」选取可见财报：
  - 报告期 6/30、公告日 8/28 的 Q2 财报，在 7/15 信号日不可见
    （旧版用 report_date<=trade_date 会错误地引用，构成未来函数）
  - 公告日为 NULL 的存量行回退 report_date（向后兼容）
  - 同一信号日取「公告日 <= 当日」的最新一期，而非全局最新
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from myquant.data_access.financial_service import FinancialImportService


def _make_service(fin_rows: List[Dict[str, Any]], trade_dates: List[str]) -> FinancialImportService:
    """绕过 __init__（需真实 DB 文件），仅 stub _queries 与 _latest_trade_dates。"""
    svc = object.__new__(FinancialImportService)
    svc._queries = SimpleNamespace(  # type: ignore[attr-defined]
        get_financial_data=lambda symbol: fin_rows,
    )
    svc._latest_trade_dates = lambda symbol, n: trade_dates[:n]  # type: ignore[attr-defined]
    return svc


def _row(
    report_date: str,
    ann_date: str | None,
    pe: float = 20.0,
    symbol: str = "000001",
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "market": "A股",
        "report_date": report_date,
        "report_type": "Q2",
        "ann_date": ann_date,
        "pe": pe,
        "pb": 1.5,
        "roe": 10.0,
        "gross_margin": 30.0,
        "debt_ratio": 40.0,
        "data_source": "test",
    }


class TestAnnDatePointInTime:
    """公告日时点过滤（防未来函数铁律 11）。"""

    def test_future_disclosure_not_visible_before_ann_date(self) -> None:
        """负向：Q2 报告期 6/30、公告日 8/28，在 7/15 信号日不可见。"""
        # 2024 年报：报告期 2023-12-31，公告日 2024-04-15
        ann_2023 = _row("2023-12-31", "2024-04-15", pe=15.0)
        # 2024 中报：报告期 2024-06-30，公告日 2024-08-28（未披露）
        q2_2024 = _row("2024-06-30", "2024-08-28", pe=25.0)
        # 信号日 2024-07-15：中报尚未披露，应看到 2023 年报 PE=15.0
        svc = _make_service([q2_2024, ann_2023], ["2024-07-15"])
        rows = svc._build_fundamental_rows(["000001"], lookback_days=1)
        pe_rows = [r for r in rows if r["factor_name"] == "市盈率PE"]
        assert len(pe_rows) == 1
        assert pe_rows[0]["factor_value"] == 15.0, (
            "7/15 信号日不应看到 8/28 才披露的中报 PE=25"
        )

    def test_disclosed_visible_after_ann_date(self) -> None:
        """正向：公告日之后信号日应看到最新披露的中报。"""
        ann_2023 = _row("2023-12-31", "2024-04-15", pe=15.0)
        q2_2024 = _row("2024-06-30", "2024-08-28", pe=25.0)
        # 信号日 2024-09-01：中报已披露，应看到 PE=25.0
        svc = _make_service([q2_2024, ann_2023], ["2024-09-01"])
        rows = svc._build_fundamental_rows(["000001"], lookback_days=1)
        pe_rows = [r for r in rows if r["factor_name"] == "市盈率PE"]
        assert len(pe_rows) == 1
        assert pe_rows[0]["factor_value"] == 25.0

    def test_null_ann_date_falls_back_to_report_date(self) -> None:
        """向后兼容：ann_date 为 NULL 的存量行回退 report_date 做时点过滤。"""
        # 存量数据无公告日，Q2 报告期 2024-06-30
        legacy = _row("2024-06-30", None, pe=30.0)
        svc = _make_service([legacy], ["2024-07-01"])
        rows = svc._build_fundamental_rows(["000001"], lookback_days=1)
        pe_rows = [r for r in rows if r["factor_name"] == "市盈率PE"]
        assert len(pe_rows) == 1
        assert pe_rows[0]["factor_value"] == 30.0

    def test_no_disclosed_report_before_first_ann_date(self) -> None:
        """最早公告日之前的信号日无任何基本面因子行。"""
        q2_2024 = _row("2024-06-30", "2024-08-28", pe=25.0)
        # 信号日 2024-01-01：任何财报都未披露
        svc = _make_service([q2_2024], ["2024-01-01"])
        rows = svc._build_fundamental_rows(["000001"], lookback_days=1)
        assert rows == []

    def test_multiple_trade_dates_use_correct_vintage(self) -> None:
        """多个信号日分别取各自时点的最新可见财报（vintage 不串味）。"""
        ann_2023 = _row("2023-12-31", "2024-04-15", pe=15.0)
        q2_2024 = _row("2024-06-30", "2024-08-28", pe=25.0)
        # 两个信号日：7/15 看中报前(15)，9/01 看中报后(25)
        svc = _make_service([q2_2024, ann_2023], ["2024-07-15", "2024-09-01"])
        rows = svc._build_fundamental_rows(["000001"], lookback_days=2)
        pe_by_date = {
            r["trade_date"]: r["factor_value"]
            for r in rows if r["factor_name"] == "市盈率PE"
        }
        assert pe_by_date["2024-07-15"] == 15.0
        assert pe_by_date["2024-09-01"] == 25.0


class TestCsvRowAnnDateMapping:
    """_map_csv_row 解析 ann_date 列。"""

    def test_parse_ann_date_column(self) -> None:
        svc = object.__new__(FinancialImportService)
        raw = {
            "symbol": "000001",
            "market": "A股",
            "report_date": "20240630",
            "report_type": "Q2",
            "ann_date": "20240828",
            "net_profit_parent": "100",
            "pe": "20",
        }
        rec = svc._map_csv_row(raw, "2026-01-01 00:00:00")
        assert rec is not None
        assert rec["ann_date"] == "2024-08-28"
        assert rec["report_date"] == "2024-06-30"

    def test_missing_ann_date_becomes_none(self) -> None:
        svc = object.__new__(FinancialImportService)
        raw = {
            "symbol": "000001",
            "report_date": "20240630",
            "net_profit_parent": "100",
        }
        rec = svc._map_csv_row(raw, "2026-01-01 00:00:00")
        assert rec is not None
        assert rec["ann_date"] is None
