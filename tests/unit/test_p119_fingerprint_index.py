# 文件名：tests/unit/test_p119_fingerprint_index.py
"""P1-19回归：数据指纹必须纳入 index 值，避免同数值不同日期区间得同指纹。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from myquant.utils.data_fingerprint import _compute_content_hash


def _frame(index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=index)


def test_same_values_different_index_timing() -> None:
    """OHLCV 数值完全相同、日期区间不同 → 指纹必须不同。"""
    h1 = _compute_content_hash(_frame(pd.date_range("2024-01-01", periods=3, freq="D")))
    h2 = _compute_content_hash(_frame(pd.date_range("2024-06-01", periods=3, freq="D")))
    assert h1 != h2, "同数值不同日期区间不得得同指纹（会导致因子与行情错位）"


def test_same_frame_stable() -> None:
    """同一份数据两次哈希一致（稳定性）。"""
    df = _frame(pd.date_range("2024-01-01", periods=3, freq="D"))
    assert _compute_content_hash(df) == _compute_content_hash(df)
