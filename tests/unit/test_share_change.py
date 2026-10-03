# 文件名：tests/unit/test_share_change.py
"""
B-3（P1-② #83）真实股本单测：
  清洗：东财股本变动表 → 事件元组（缺列/NaN/零值过滤，升序去重）
  对齐：compute_b_class_metrics 按 change_date<=trade_date 取真实股本（解禁前/后变化）
  回退：无股本记录 → circ_mv/close 反推近似
  防未来：变更日期 > trade_date 不生效
"""

from __future__ import annotations

import pandas as pd
import pytest

from myquant.data_access.cleaner.derived_metrics import compute_b_class_metrics
from myquant.data_access.share_change_sync_service import ShareChangeSyncService


def _events_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "变更日期": ["2023-06-15", "2024-12-10", "2025-03-20"],
            "总股本": [1000000000.0, 1200000000.0, 1200000000.0],
            "流通受限股份": [500000000.0, 300000000.0, 0.0],
            "已上市流通A股": [500000000.0, 900000000.0, 1200000000.0],
            "变动原因": ["送股上市", "解禁", "回购"],
        }
    )


class TestCleanRecords:
    def test_clean_events(self):
        """清洗：升序、浮点转换、原因保留。"""
        out = ShareChangeSyncService._clean_records(_events_df(), "600519.SH")
        assert out == [
            ("2023-06-15", 1000000000.0, 500000000.0, "送股上市"),
            ("2024-12-10", 1200000000.0, 900000000.0, "解禁"),
            ("2025-03-20", 1200000000.0, 1200000000.0, "回购"),
        ]

    def test_missing_columns_empty(self):
        """缺列返回空（不崩溃）。"""
        out = ShareChangeSyncService._clean_records(pd.DataFrame({"x": [1]}), "600519.SH")
        assert out == []

    def test_nan_zero_filtered(self):
        """NaN 股本与 1900 脏行被过滤。"""
        df = pd.DataFrame(
            {
                "变更日期": ["1900-01-01", "2024-01-01", "2024-02-01"],
                "总股本": [100.0, float("nan"), 200.0],
                "已上市流通A股": [50.0, 100.0, 0.0],
                "变动原因": ["", "", ""],
            }
        )
        out = ShareChangeSyncService._clean_records(df, "600519.SH")
        # 1900 脏行与 NaN 总股本被过滤；float_share=0（上市前股本事实）保留，total_share>0 合法
        assert out == [("2024-02-01", 200.0, 0.0, "")]


def _records():
    return [
        {"trade_date": "2023-01-10", "close": 10.0, "volume": 100000.0},
        {"trade_date": "2024-06-10", "close": 12.0, "volume": 100000.0},
        {"trade_date": "2025-01-10", "close": 15.0, "volume": 100000.0},
    ]


def _fin():
    return [{"report_date": "2024-06-30", "circ_mv": 10800000000.0, "total_mv": 12000000000.0, "net_profit": 100000000.0}]


class TestAlignment:
    def test_real_share_priority(self):
        """真实股本优先：解禁前 float_share=5 亿、解禁后=9 亿。"""
        share = [
            {"change_date": "2023-06-15", "total_share": 1000000000.0, "float_share": 500000000.0},
            {"change_date": "2024-12-10", "total_share": 1200000000.0, "float_share": 900000000.0},
        ]
        out = compute_b_class_metrics(_records(), _fin(), share_records=share)
        # P0-03修复后：2023-01-10 该日前无股本事件，且财报(截止2024-08-31)尚未披露
        #   → 无市值快照可反推 → None（原错误口径：最新市值÷末日收盘=10800000000/15，未来函数）
        assert out[0]["float_share"] is None
        # 2024-06-10：用 2023-06-15 事件真实股本 5 亿
        assert out[1]["float_share"] == pytest.approx(500000000.0, rel=1e-6)
        assert out[1]["turnover"] == pytest.approx(100000.0 / 500000000.0 * 100.0, rel=1e-6)
        # 2025-01-10：用 2024-12-10 事件真实股本 9 亿
        assert out[2]["float_share"] == pytest.approx(900000000.0, rel=1e-6)

    def test_no_share_fallback(self):
        """无股本记录：仅当财报市值快照已披露后才用当日市值÷当日收盘反推，早期行置 None。"""
        out = compute_b_class_metrics(_records(), _fin())
        # 2023-01-10 / 2024-06-10：财报(截止2024-08-31)尚未披露 → None
        assert out[0]["float_share"] is None
        assert out[1]["float_share"] is None
        # 2025-01-10：快照已披露，用当日收盘15.0反推
        assert out[2]["float_share"] == pytest.approx(10800000000.0 / 15.0, rel=1e-6)

    def test_future_change_not_applied(self):
        """防未来：变更日期 > trade_date 不生效。"""
        share = [{"change_date": "2025-12-31", "total_share": 999.0, "float_share": 999.0}]
        out = compute_b_class_metrics(_records(), _fin(), share_records=share)
        # 2023-01-10：未来事件不生效，财报快照也未披露 → None
        assert out[0]["float_share"] is None
        # 2025-01-10：未来股本事件不生效；财报快照已披露 → 当日收盘反推
        assert out[2]["float_share"] == pytest.approx(10800000000.0 / 15.0, rel=1e-6)


class TestP003NoLookaheadShareReconstruct:
    """P0-03回归：兜底反推股本不得用整段末日收盘价反推后套用历史bar（未来函数）"""

    def test_historical_row_not_reconstructed_from_latest_price(self):
        """2020-01-02 close=10、2026-09-30 close=100、circ_mv=1000：
        2020-01-02 行 float_share 不得等于 1000/100=10（用未来最新价反推）。"""
        records = [
            {"trade_date": "2020-01-02", "close": 10.0, "volume": 1000.0},
            {"trade_date": "2026-09-30", "close": 100.0, "volume": 1000.0},
        ]
        fin = [{"report_date": "2026-06-30", "circ_mv": 1000.0,
                "total_mv": 1000.0, "net_profit": 100.0}]
        out = compute_b_class_metrics(records, fin)
        # 2020-01-02：报告截止日2026-08-31尚未披露 → 无市值快照 → None（不得等于10）
        assert out[0]["float_share"] is None
        assert out[0]["float_share"] != 1000.0 / 100.0
        # 2026-09-30：快照已披露，用当日自身收盘100反推 = 10
        assert out[1]["float_share"] == pytest.approx(1000.0 / 100.0, rel=1e-6)
