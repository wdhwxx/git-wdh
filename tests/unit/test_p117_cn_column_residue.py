# 文件名：tests/unit/test_p117_cn_column_residue.py
"""P1-17回归：策略层中文列改造残留——恒假英文列判定导致取价/ATR错误。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from myquant.strategies.grid_strategy import GridStrategy


def _cn_df(n: int = 25) -> pd.DataFrame:
    """收盘价恒定(close.std=0)、最高最低差恒为4的中文列数据。"""
    return pd.DataFrame({
        "开盘价": [100.0] * n,
        "收盘价": [100.0] * n,
        "最高价": [102.0] * n,
        "最低价": [98.0] * n,
        "成交量": [1000.0] * n,
    })


def test_auto_grid_uses_high_low_atr_not_close_std() -> None:
    """中文列下 ATR 必须取 (high-low) 均值（=4），而非 close.std()（=0）。"""
    strat = GridStrategy(auto_grid=True, auto_grid_lookback=20, auto_grid_atr_mult=2.0)
    ctx = SimpleNamespace(data={"000001.SZ": _cn_df()},
                          get_data=lambda symbol: _cn_df())
    strat._calc_auto_grid(ctx)
    # mid=100, atr=4 → upper=100+4*2=108, lower=100-4*2=92
    assert strat.grid_upper == 108.0, f"upper={strat.grid_upper}（应为108，旧错走close.std=0分支）"
    assert strat.grid_lower == 92.0
