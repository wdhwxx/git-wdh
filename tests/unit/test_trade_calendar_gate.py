# 文件名：tests/unit/test_trade_calendar_gate.py
"""
test_trade_calendar_gate.py - 交易日历安全门测试（KP-P3-10 / T-16）
====================================================================
覆盖"交易日历缺失，非交易日可通过安全门"修复：
  1. TradingHoursRule 增交易日维度（非交易日拒绝/交易日放行）。
  2. run_实盘入口._is_trading_day 接入真实交易日历（已知交易日 True / 周日 False）。

依赖: pytest, myquant.risk.rules, myquant.models.trade, run_实盘入口
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from myquant.models.trade import Order, OrderSide, OrderType
from myquant.risk.rules import RiskContext, TradingHoursRule


def _make_order() -> Order:
    return Order(
        symbol="000001.SZ",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=10.0,
    )


class TestTradingHoursRuleTradingDay:
    def test_交易日历_非交易日拒绝(self) -> None:
        """DoD：日历为空集 → 任意日期非交易日 → check 拒绝（含交易日维度）。"""
        rule = TradingHoursRule(trading_calendar=set())
        ctx = RiskContext(extra={"current_time": "10:00:00"})
        res = rule.check(_make_order(), ctx)
        assert res.passed is False
        assert "非交易日" in res.reason

    def test_交易日历_交易日放行(self) -> None:
        """DoD：日历含今日 + 交易时段内 → 放行。"""
        rule = TradingHoursRule(trading_calendar={dt.date.today()})
        ctx = RiskContext(extra={"current_time": "10:00:00"})
        res = rule.check(_make_order(), ctx)
        assert res.passed is True

    def test_is_trading_day_显式日历(self) -> None:
        """DoD：_is_trading_day 按显式日历判定。"""
        rule = TradingHoursRule(trading_calendar={dt.date(2026, 10, 9)})
        assert rule._is_trading_day(dt.date(2026, 10, 9)) is True
        assert rule._is_trading_day(dt.date(2026, 10, 10)) is False

    def test_非交易时段_仍拒绝(self) -> None:
        """DoD：交易日但非交易时段 → 拒绝（保留原时段维度）。"""
        rule = TradingHoursRule(trading_calendar={dt.date.today()})
        ctx = RiskContext(extra={"current_time": "02:00:00"})
        res = rule.check(_make_order(), ctx)
        assert res.passed is False
        assert "非交易时段" in res.reason


class TestEntryTradingDay:
    def test_实盘入口_交易日判定接真实日历(self) -> None:
        """DoD：真实日历已知交易日 True；周日 False。"""
        from myquant.utils.trade_calendar import load_trade_calendar
        from run_实盘入口 import _is_trading_day

        cal = load_trade_calendar(Path(".").resolve())
        if not cal:
            pytest.skip("交易日历缓存为空（CI 无网络/无本地数据）")
        assert cal  # 缓存存在且非空
        trading = sorted(cal)[-1]  # 最近已知交易日
        assert _is_trading_day(trading) is True
        assert _is_trading_day(dt.date(2026, 10, 4)) is False  # 2026-10-04 周日
