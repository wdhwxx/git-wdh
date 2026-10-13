# -*- coding: utf-8 -*-
"""T-40 KP-P2-01 三因子股票池可交易性过滤：ST/停牌/次新/20日均量剔除 + 比例参数化。

P1-07④：三因子截面合成处 as-of 兜底（validate_asof 接线，含未来行 → 阻断）。
"""
from typing import Dict

import pandas as pd
import pytest

from myquant.leakage_gate.cross_sectional import CrossSectionalAsOfError
from myquant.strategies.base import StockPoolBuilder
from myquant.strategies.ma_strategy import RSRSStrategy
from tests.conftest import make_ohlcv_df


def _good_df(n: int = 120) -> object:
    """健康标的：120 根 K 线、成交量正常。"""
    closes = [10.0 + i * 0.01 for i in range(n)]
    return make_ohlcv_df(closes, volumes=1_000_000.0)


def _st_df() -> object:
    df = _good_df()
    df["名称"] = "ST某某"
    return df


def test_filter_removes_st():
    """含 ST 名称的标的被剔除。"""
    data: Dict = {"000001.SZ": _st_df(), "000002.SZ": _good_df()}
    kept = StockPoolBuilder().filter_by_tradability(list(data), data)
    assert "000001.SZ" not in kept
    assert "000002.SZ" in kept


def test_filter_removes_newly_listed():
    """上市不足 60 天的次新股被剔除。"""
    data: Dict = {
        "000001.SZ": _good_df(n=40),   # 不足 60 根
        "000002.SZ": _good_df(n=120),
    }
    kept = StockPoolBuilder().filter_by_tradability(list(data), data)
    assert "000001.SZ" not in kept
    assert "000002.SZ" in kept


def test_filter_removes_suspended_zero_volume():
    """近 20 日均量为 0（停牌/无成交）被剔除。"""
    data: Dict = {
        "000001.SZ": _good_df().assign(成交量=0.0),   # 20日均量=0
        "000002.SZ": _good_df(),
    }
    kept = StockPoolBuilder().filter_by_tradability(list(data), data)
    assert "000001.SZ" not in kept
    assert "000002.SZ" in kept


def test_filter_keeps_tradable_without_name_col():
    """无名称列的正常标的通过（不因缺名称列误杀）。"""
    data: Dict = {"000002.SZ": _good_df()}
    kept = StockPoolBuilder().filter_by_tradability(list(data), data)
    assert kept == ["000002.SZ"]


def test_build_includes_tradability_filter():
    """build 流程插入可交易性过滤：ST/停牌/次新剔除，健康标的入选。"""
    builder = StockPoolBuilder()
    data: Dict = {
        "000001.SZ": _st_df(),
        "000003.SZ": _good_df(n=50),      # 次新
        "000004.SZ": _good_df().assign(成交量=0.0),  # 停牌
        "000002.SZ": _good_df(),
    }
    pool = builder.build(list(data), data)
    assert "000002.SZ" in pool
    assert "000001.SZ" not in pool
    assert "000003.SZ" not in pool
    assert "000004.SZ" not in pool


def test_ma_strategy_pool_top_ratio_default_one_third():
    """ma_strategy 池比例默认回 1/3（不再 0.5 覆写规格），且可参数化。"""
    s = RSRSStrategy()
    assert s.pool_top_ratio == 1.0 / 3.0
    s2 = RSRSStrategy(pool_top_ratio=0.4)
    assert s2.pool_top_ratio == 0.4


# ═══════════════════════════════════════════════
#  P1-07④ 横截面 as-of 兜底（validate_asof 接线）
# ═══════════════════════════════════════════════

def test_calculate_factors_asof_future_blocked():
    """截面含未来日期行 → calculate_factors 抛 CrossSectionalAsOfError 阻断该截面。"""
    data: Dict = {"000001.SZ": _good_df(), "000002.SZ": _good_df()}
    # 给 000002.SZ 追加晚于决策时点（2026-10-10）的未来行
    future = make_ohlcv_df([99.0], start="2026-12-01")
    data["000002.SZ"] = pd.concat([data["000002.SZ"], future])
    with pytest.raises(CrossSectionalAsOfError, match="as-of 校验失败"):
        StockPoolBuilder().calculate_factors(data, decision_time="2026-10-10")


def test_calculate_factors_asof_clean():
    """全部标的索引 <= 决策日 → calculate_factors 正常合成截面。"""
    data: Dict = {"000001.SZ": _good_df(), "000002.SZ": _good_df()}
    factor_df = StockPoolBuilder().calculate_factors(data, decision_time="2026-10-10")
    assert "000001.SZ" in factor_df.index
    assert {"volatility", "price", "momentum"}.issubset(factor_df.columns)


def test_build_asof_truncation_and_check_ok():
    """build(as_of=决策日)：截断 + as-of 校验正常路径不抛、正常选股。"""
    data: Dict = {"000001.SZ": _good_df(), "000002.SZ": _good_df()}
    pool = StockPoolBuilder().build(
        list(data), data, as_of=pd.Timestamp("2026-10-10").timestamp()
    )
    assert isinstance(pool, list)
    assert set(pool).issubset({"000001.SZ", "000002.SZ"})
