# 文件名：tests/unit/test_akshare_ann_date.py
"""B-09 财务取数补公告日期字段单测。

覆盖（不联网，monkeypatch requests.get / 纯函数）：
  1. _normalize_notice_date 归一化（东财 NOTICE_DATE 格式 → YYYY-MM-DD；无效 → ""）；
  2. 东财估值记录构造：财务主数据响应含 NOTICE_DATE → ann_date 被填充；
  3. 东财历史记录构造：NOTICE_DATE → ann_date 被填充；
  4. 新浪摘要记录构造：无公告日列 → ann_date 显式空串。
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from myquant.data_access.sources.akshare_source import AkshareDataSource


@pytest.fixture
def source() -> AkshareDataSource:
    return AkshareDataSource(config={"timeout": 5})


# ═══════════════════════════════════════════
# 1. _normalize_notice_date 纯函数
# ═══════════════════════════════════════════


def test_normalize_notice_date_datetime_format() -> None:
    """东财 "2026-08-15 00:00:00" → "2026-08-15"。"""
    assert AkshareDataSource._normalize_notice_date(
        "2026-08-15 00:00:00"
    ) == "2026-08-15"


def test_normalize_notice_date_digits() -> None:
    """YYYYMMDD 数字串 → YYYY-MM-DD。"""
    assert AkshareDataSource._normalize_notice_date("20260815") == "2026-08-15"


def test_normalize_notice_date_invalid() -> None:
    """None/空/非法输入 → 空串（消费侧视为无公告日）。"""
    assert AkshareDataSource._normalize_notice_date(None) == ""
    assert AkshareDataSource._normalize_notice_date("") == ""
    assert AkshareDataSource._normalize_notice_date("   ") == ""


# ═══════════════════════════════════════════
# 2. 东财估值：财务主数据 NOTICE_DATE 补 ann_date
# ═══════════════════════════════════════════

_VAL_ROW = {
    "TRADE_DATE": "2026-10-16 00:00:00",
    "PE_TTM": "30.5",
    "PB_MRQ": "8.2",
    "TOTAL_MARKET_CAP": "1.2万亿",
    "NOTLIMITED_MARKETCAP_A": "1.1万亿",
}
_FIN_ROW = {
    "EPSJB": "55.0",
    "BPS": "380.0",
    "NOTICE_DATE": "2026-08-15 00:00:00",
    "REPORT_DATE": "2026-06-30 00:00:00",
}


def _fake_resp(payload: Dict[str, Any]):
    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> Dict[str, Any]:
            return {"result": {"data": payload}}

    return _Resp()


def test_eastmoney_datacenter_ann_date_filled(
    source: AkshareDataSource,
) -> None:
    """估值接口无公告日，财务主数据响应 NOTICE_DATE 补齐 ann_date。"""
    def fake_get(url, params=None, headers=None, timeout=None):
        assert params["reportName"] in ("RPT_VALUEANALYSIS_DET",
                                        "RPT_F10_FINANCE_MAINFINADATA")
        if params["reportName"] == "RPT_VALUEANALYSIS_DET":
            return _fake_resp([_VAL_ROW])
        return _fake_resp([_FIN_ROW])

    with patch("requests.get", side_effect=fake_get):
        result = source._fetch_eastmoney_datacenter(
            "600519.SH", "600519", "2026-10-17"
        )
    assert result is not None
    assert result["ann_date"] == "2026-08-15"
    assert result["report_date"] == "2026-10-16"
    assert result["data_source"] == "akshare_eastmoney_datacenter"


def test_eastmoney_datacenter_ann_date_empty_when_no_fin(
    source: AkshareDataSource,
) -> None:
    """财务主数据响应为空 → ann_date 保持空串（不抛错）。"""
    def fake_get(url, params=None, headers=None, timeout=None):
        if params["reportName"] == "RPT_VALUEANALYSIS_DET":
            return _fake_resp([_VAL_ROW])
        return _fake_resp([])

    with patch("requests.get", side_effect=fake_get):
        result = source._fetch_eastmoney_datacenter(
            "600519.SH", "600519", "2026-10-17"
        )
    assert result is not None
    assert result["ann_date"] == ""


# ═══════════════════════════════════════════
# 3. 东财历史：NOTICE_DATE 补 ann_date
# ═══════════════════════════════════════════

def test_eastmoney_history_ann_date_filled(source: AkshareDataSource) -> None:
    """东财历史财务记录从 NOTICE_DATE 取 ann_date。"""
    hist_rows = [
        {
            "REPORT_DATE": "2026-06-30 00:00:00",
            "NOTICE_DATE": "2026-08-15 00:00:00",
            "EPSJB": "55.0",
            "BPS": "380.0",
            "TOTALOPERATEREVE": "1.2e11",
            "PARENTNETPROFIT": "5.5e10",
            "ROEJQ": "18.5",
            "XSMLL": "90.0",
        },
        {
            "REPORT_DATE": "2026-03-31 00:00:00",
            "NOTICE_DATE": "2026-04-28 00:00:00",
            "EPSJB": "20.0",
            "BPS": "350.0",
            "TOTALOPERATEREVE": "8e10",
            "PARENTNETPROFIT": "2e10",
            "ROEJQ": "6.0",
            "XSMLL": "88.0",
        },
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        return _fake_resp(hist_rows)

    with patch("requests.get", side_effect=fake_get):
        records = source._fetch_eastmoney_financial_history(
            "600519.SH", "600519", None, "20261017"
        )
    assert len(records) == 2
    assert records[0]["ann_date"] == "2026-08-15"
    assert records[0]["report_date"] == "20260630"
    assert records[1]["ann_date"] == "2026-04-28"


def test_eastmoney_history_ann_date_empty(source: AkshareDataSource) -> None:
    """NOTICE_DATE 缺失 → ann_date 空串（记录仍返回）。"""
    hist_rows = [{"REPORT_DATE": "2026-06-30 00:00:00", "EPSJB": "1.0"}]

    def fake_get(url, params=None, headers=None, timeout=None):
        return _fake_resp(hist_rows)

    with patch("requests.get", side_effect=fake_get):
        records = source._fetch_eastmoney_financial_history(
            "600519.SH", "600519", None, "20261017"
        )
    assert len(records) == 1
    assert records[0]["ann_date"] == ""


# ═══════════════════════════════════════════
# 4. 新浪摘要：无公告日列 → ann_date 显式空
# ═══════════════════════════════════════════

def test_sina_abstract_ann_date_empty(source: AkshareDataSource) -> None:
    """新浪财务摘要无公告日期列，ann_date 显式空串（record 正常返回）。"""
    import pandas as pd

    df = pd.DataFrame(
        {
            "选项": ["报告期", "净利润"],
            "指标": ["--", "净利润"],
            "20260630": ["2026-06-30", "55.0亿"],
        }
    )

    def fake_retry(func, *args, **kwargs):
        return df

    class _FakeAk:
        stock_financial_abstract = None  # 被 _retry_request patch 拦截，不真调用

    with patch.object(source, "_retry_request", side_effect=fake_retry):
        result = source._try_sina_finance(
            _FakeAk(), "600519.SH", "600519", "20261017"
        )
    assert result is not None
    assert result["ann_date"] == ""
    assert result["report_date"] == "20260630"
