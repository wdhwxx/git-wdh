# 文件名：tests/unit/test_trade_calendar.py
"""
B-1（P2-① #83）真实交易日历单测：
  服务层：sync 落盘（stub akshare）/ load 读回 / 无缓存 None
  TradingCalendar：真实日历优先（节假日按真实日历）/ set_trade_days 注入回退 / next guard 越界
  base.get_trading_dates：真实日历优先（国庆区间空）/ 无缓存回退工作日
  防未来：end_date 截断
"""

from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pandas as pd

from myquant.data_access.sources.base import DataSourceBase
from myquant.utils import time_utils
from myquant.utils.trade_calendar import (
    load_trade_calendar,
    save_trade_calendar,
    sync_trade_calendar,
)


class _DummySource(DataSourceBase):
    """最小具体子类（DataSourceBase 抽象，仅供 get_trading_dates 测试）。"""

    name = "dummy_source"

    def get_market_data(self, *args, **kwargs):
        return []

    def get_stock_list(self, market: str = "A股"):
        return []

    def health_check(self):
        return True


def _fake_akshare(df: pd.DataFrame):
    return types.SimpleNamespace(tool_trade_date_hist_sina=lambda: df)


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame({"trade_date": [date(2025, 10, 9), date(2025, 10, 10), date(2025, 10, 13)]})


class TestTradeCalendarStore:
    def test_sync_and_load(self, tmp_path: Path, monkeypatch):
        """sync 拉取→落盘→load 读回。"""
        monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(_sample_df()))
        n = sync_trade_calendar(tmp_path)
        assert n == 3
        dates = load_trade_calendar(tmp_path)
        assert dates == {date(2025, 10, 9), date(2025, 10, 10), date(2025, 10, 13)}

    def test_sync_reuses_cache(self, tmp_path: Path, monkeypatch):
        """已有缓存且非 force 不触网（直接返回缓存数量）。"""
        save_trade_calendar(tmp_path, {date(2025, 10, 9)})
        hit = {"called": False}

        def boom():
            hit["called"] = True
            raise AssertionError("不应触网")

        monkeypatch.setitem(sys.modules, "akshare", types.SimpleNamespace(tool_trade_date_hist_sina=boom))
        n = sync_trade_calendar(tmp_path)
        assert n == 1
        assert not hit["called"]

    def test_load_no_cache_none(self, tmp_path: Path):
        """无缓存返回 None（调用方回退）。"""
        assert load_trade_calendar(tmp_path) is None


class TestTradingCalendarReal:
    def test_trade_days_priority(self):
        """真实日历优先：周末/节假日按真实集合判定。"""
        days = {date(2025, 10, 9), date(2025, 10, 10), date(2025, 10, 13)}
        cal = time_utils.TradingCalendar(trade_days=days)
        assert cal.is_trading_day(date(2025, 10, 9))  # 周四，在集合
        assert not cal.is_trading_day(date(2025, 10, 11))  # 周六
        assert not cal.is_trading_day(date(2025, 10, 6))  # 国庆（工作日但不在真实日历）

    def test_set_trade_days_and_fallback(self):
        """set_trade_days 注入/清除。"""
        cal = time_utils.TradingCalendar()
        assert cal.is_trading_day(date(2025, 10, 9))  # 无注入：普通周四，工作日近似 True
        cal.set_trade_days({date(2025, 10, 9)})
        assert not cal.is_trading_day(date(2025, 10, 10))  # 周五工作日但不在真实集合
        assert cal.is_trading_day(date(2025, 10, 9))
        cal.set_trade_days(None)
        assert cal.is_trading_day(date(2025, 10, 10))  # 清除后回退工作日近似

    def test_next_guard_out_of_range(self):
        """真实日历越界时 guard 截断返回（不卡死）。"""
        cal = time_utils.TradingCalendar(trade_days={date(2025, 10, 9), date(2025, 10, 10)})
        res = cal.next_trading_day(date(2025, 10, 10), 1)
        assert res > date(2025, 10, 10)  # 越界按日偏移近似返回


class TestBaseGetTradingDates:
    def test_real_calendar_priority(self, monkeypatch):
        """真实日历优先：国庆区间无交易日。"""
        monkeypatch.setattr(
            "myquant.utils.trade_calendar.load_trade_calendar",
            lambda root: {date(2025, 10, 9), date(2025, 10, 10)},
        )
        src = _DummySource()
        dates = src.get_trading_dates("2025-10-01", "2025-10-10")
        assert dates == ["2025-10-09", "2025-10-10"]

    def test_fallback_weekdays(self, monkeypatch):
        """无缓存回退工作日近似（节假日误判保留旧行为+warning）。"""
        monkeypatch.setattr("myquant.utils.trade_calendar.load_trade_calendar", lambda root: None)
        src = _DummySource()
        dates = src.get_trading_dates("2025-10-09", "2025-10-13")
        assert dates == ["2025-10-09", "2025-10-10", "2025-10-13"]

    def test_end_date_cutoff(self, monkeypatch):
        """防未来：end_date 之后的真实交易日不返回。"""
        monkeypatch.setattr(
            "myquant.utils.trade_calendar.load_trade_calendar",
            lambda root: {date(2025, 10, 9), date(2025, 10, 10), date(2025, 12, 31)},
        )
        src = _DummySource()
        dates = src.get_trading_dates("2025-10-01", "2025-10-10")
        assert dates == ["2025-10-09", "2025-10-10"]
