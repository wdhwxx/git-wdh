# 文件名：tests/unit/test_derived_metrics_report_deadline.py
"""
P1-①（#83）发布日推算：财务数据按披露截止日而非报告期判定。

核心：报告期 03-31 的 Q1 财报披露截止 04-30，在 4/1~4/29 之间
不得使用（防未来函数）；5/10 起可用当期。
"""

from __future__ import annotations

import pytest

from myquant.data_access.cleaner.derived_metrics import _report_deadline, compute_b_class_metrics


def _fin(report_date: str, net_profit: float) -> dict:
    return {
        "report_date": report_date,
        "net_profit": net_profit,
        "circ_mv": 1100000.0,
        "total_mv": 1100000.0,
    }


class TestReportDeadline:
    def test_mapping(self):
        """四个报告期 → 法定披露截止日。"""
        assert _report_deadline("2024-03-31") == "2024-04-30"
        assert _report_deadline("2024-06-30") == "2024-08-31"
        assert _report_deadline("2024-09-30") == "2024-10-31"
        assert _report_deadline("2023-12-31") == "2024-04-30"  # 年报次年 4-30

    def test_abnormal_kept(self):
        """异常格式保留原值（不阻断下游）。"""
        assert _report_deadline("") == ""
        assert _report_deadline("2024-01-15") == "2024-01-15"


class TestBClassPeNoLookahead:
    def test_before_disclosure_no_pe(self):
        """2024-04-20：年报(4/30)与 Q1(4/30)均未到披露截止 → pe 为 None（防未来）。"""
        records = [{"trade_date": "2024-04-20", "close": 100.0, "volume": 100000.0}]
        fins = [_fin("2023-12-31", 40000.0), _fin("2024-03-31", 12000.0)]
        out = compute_b_class_metrics(records, fins)
        assert out[0]["pe_dynamic"] is None

    def test_after_disclosure_uses_current_period(self):
        """2024-05-10：Q1 已披露（截止 4/30）→ 用 Q1 年化 ×4。"""
        records = [{"trade_date": "2024-05-10", "close": 100.0, "volume": 100000.0}]
        fins = [_fin("2023-12-31", 40000.0), _fin("2024-03-31", 12000.0)]
        out = compute_b_class_metrics(records, fins)
        # total_share = total_mv/close = 1100000/100 = 11000
        # pe = close*total_share / (12000*4) = 1100000/48000 = 22.916666…，round(…,4)=22.9167
        assert out[0]["pe_dynamic"] == 22.9167
