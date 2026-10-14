# 文件名：tests/unit/test_p212_factor_annualize.py
"""P2-12回归：h日重叠收益年化必须 mean*252/h，不得 mean*252。"""

from __future__ import annotations

from myquant.indicators.factor_validator import TRADING_DAYS_PER_YEAR, _annualized_mean


def test_annualize_divides_by_horizon() -> None:
    vals = [0.01] * 10
    # 正确：日频均值=0.01/5，年化=*252
    expected = 0.01 / 5 * TRADING_DAYS_PER_YEAR
    got = _annualized_mean(vals, horizon=5)
    assert abs(got - expected) < 1e-9
    # 旧错算法 mean*252=2.52，新结果应为其 1/5=0.504
    assert abs(got - 0.504) < 1e-6


def test_horizon_one_is_daily_annualization() -> None:
    vals = [0.001] * 5
    assert abs(_annualized_mean(vals, horizon=1) - 0.001 * TRADING_DAYS_PER_YEAR) < 1e-9
