# 文件名：tests/unit/test_p116_rsrs_validate.py
"""P1-16回归：RSRS.validate_no_lookahead 在中文列输入下必须真正可执行并通过。

此前该方法残留英文列 high/low，与 calculate() 已切换的中文列（最高价/最低价）
不一致，任何输入都会抛 KeyError/ValueError，导致"防未来证据链"断裂。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from myquant.indicators.rsrs import RSRS


def _make_cn_data(n: int = 200, seed: int = 7) -> pd.DataFrame:
    """构造中文列行情数据（与实盘/批量计算入口一致）。"""
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    high = close + np.abs(rng.normal(0.1, 0.05, n))
    low = close - np.abs(rng.normal(0.1, 0.05, n))
    return pd.DataFrame({
        "开盘价": close,
        "最高价": high,
        "最低价": low,
        "收盘价": close,
    })


def test_validate_no_lookahead_runs_and_passes() -> None:
    """中文列输入：方法可执行且返回 True（RSRS本体不含当日）。"""
    ind = RSRS(period=18, norm_window=60)
    data = _make_cn_data()
    ok = ind.validate_no_lookahead(data)
    assert ok is True, "RSRS 防未来自校验必须通过（β_i 等价于截断重算）"


def test_calculate_outputs_rsrs_raw() -> None:
    """冒烟：calculate 输出 rsrs_raw 列且非全空。"""
    ind = RSRS(period=18, norm_window=60)
    out = ind.calculate(_make_cn_data())
    assert "rsrs_raw" in out.columns
    assert out["rsrs_raw"].notna().sum() > 0
