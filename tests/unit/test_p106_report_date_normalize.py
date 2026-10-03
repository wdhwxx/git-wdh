# 文件名：tests/unit/test_p106_report_date_normalize.py
"""P1-06回归：新浪财务摘要通道 report_date 归一化后再做防未来比较。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from myquant.data_access.sources.akshare_source import AkshareDataSource

_FAKE_AK = SimpleNamespace(stock_financial_abstract=lambda **k: None)


def _abstract_df(latest_period: str) -> pd.DataFrame:
    """模拟 stock_financial_abstract：列名含报告期（可能带分隔符）。"""
    return pd.DataFrame({
        "选项": ["按报告期", "按报告期"],
        "指标": ["市盈率", "市净率"],
        latest_period: [5.0, 0.8],
    })


def _mk(monkeypatch: pytest.MonkeyPatch, df: pd.DataFrame) -> AkshareDataSource:
    src = object.__new__(AkshareDataSource)
    monkeypatch.setattr(src, "_retry_request", lambda *a, **k: df)
    return src


def test_dashed_period_normalized_not_future(monkeypatch: pytest.MonkeyPatch) -> None:
    """列名 '2024-03-31' 归一化为 '20240331'，早于 end_date → 返回且归一。"""
    src = _mk(monkeypatch, _abstract_df("2024-03-31"))
    out = src._try_sina_finance(_FAKE_AK, "000001", "000001", "20240630")
    assert out is not None
    assert out["report_date"] == "20240331"


def test_future_period_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """归一后 20240930 > end_date 20240630 → None（防未来）。"""
    src = _mk(monkeypatch, _abstract_df("2024-09-30"))
    out = src._try_sina_finance(_FAKE_AK, "000001", "000001", "20240630")
    assert out is None


def test_unparseable_period_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """无法解析的报告期列名 → 告警拒绝（不静默放行）。"""
    src = _mk(monkeypatch, _abstract_df("最新"))
    out = src._try_sina_finance(_FAKE_AK, "000001", "000001", "20240630")
    assert out is None
