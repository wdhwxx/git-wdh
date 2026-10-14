# 文件名：tests/unit/test_live_data_feed_adjust.py
"""
S2（#83）live 链路复权口径统一单测
=====================================
覆盖：akshare 轮询改拉 raw（不复权事实）/ 除权跳变因子豁免 / adjust_out qfq 现算。

零网络：stub akshare 模块与因子服务。
"""

from __future__ import annotations

import sys
import types
from datetime import datetime

import pandas as pd

from myquant.live.data_feed import MarketDataFeed, _live_jump_change_with_factor


class StubFactorSvc:
    """stub 因子服务：返回预置因子事件表。"""

    def __init__(self, lookup: dict):
        self._lookup = lookup

    def get_factor_lookup(self, symbol: str, as_of: str | None = None) -> dict:
        return dict(self._lookup)


def _make_bar(symbol: str = "600519.SH", date: str = "2024-06-19", close: float = 1501.0) -> dict:
    """构造标准 bar（英文表头，与 data_feed 内部一致）。"""
    return {
        "symbol": symbol,
        "date": date,
        "open": close * 1.005,
        "high": close * 1.01,
        "low": close * 0.995,
        "close": close,
        "volume": 1000.0,
        "amount": 1500000.0,
        "timestamp": datetime(2024, 6, 19, 15, 0).timestamp(),
    }


class TestAkshareRaw:
    def test_fetch_akshare_uses_raw(self, monkeypatch):
        """S2：新浪轮询改拉不复权 raw（adjust=''），不再直接 qfq。"""
        calls: dict = {}

        def fake_daily(symbol, start_date, end_date, adjust):
            calls["adjust"] = adjust
            return pd.DataFrame({
                "date": ["2024-06-19"],
                "open": [1501.0],
                "high": [1510.0],
                "low": [1495.0],
                "close": [1501.0],
                "volume": [1000],
                "amount": [1500000],
            })

        monkeypatch.setitem(sys.modules, "akshare", types.SimpleNamespace(stock_zh_a_daily=fake_daily))
        feed = MarketDataFeed(source="akshare")
        feed._connected = True
        bars = feed._fetch_from_akshare("600519.SH")
        assert calls["adjust"] == ""
        assert bars and bars[-1]["close"] == 1501.0


class TestJumpExempt:
    def test_exempt_on_factor_event(self):
        """S2：除权日 raw 跳变但 hfq 连续 → 不告警（重置 streak）。"""
        lookup = {"2024-06-18": 8.071836, "2024-06-19": 8.239032}
        feed = MarketDataFeed(
            source="realtime_mock",
            clean_config={"price_jump_threshold": 0.15},
            factor_svc=StubFactorSvc(lookup),
        )
        feed._latest_bars["600519.SH"] = _make_bar(date="2024-06-18", close=1521.5)
        feed._jump_streak["600519.SH"] = 2
        cur = _make_bar(date="2024-06-19", close=1501.0)
        cleaned = feed._stream_clean("600519.SH", cur)
        assert cleaned is cur
        assert feed._jump_streak["600519.SH"] == 0
        assert feed._stats["dirty_bars"] == 0

    def test_detect_non_factor_jump(self):
        """S2：非除权日跳变仍告警（达连续阈值）。"""
        lookup = {"2024-06-18": 8.071836}
        feed = MarketDataFeed(
            source="realtime_mock",
            clean_config={"price_jump_threshold": 0.15},
            factor_svc=StubFactorSvc(lookup),
        )
        feed._latest_bars["600519.SH"] = _make_bar(date="2024-06-17", close=1500.0)
        feed._jump_streak["600519.SH"] = 2
        feed._stream_clean("600519.SH", _make_bar(date="2024-06-18", close=1200.0))
        assert feed._stats["dirty_bars"] >= 1

    def test_helper_no_factor_returns_none(self):
        """辅助函数：无因子表返回 None（不豁免）。"""
        assert _live_jump_change_with_factor(1500.0, 1200.0, "2024-06-17", "2024-06-18", {}) is None


class TestQfqAdjustOut:
    def test_qfq_divide_latest_factor(self):
        """S2：adjust_out='qfq' 时 bar 价 ÷ 最新因子。"""
        lookup = {"2024-06-18": 8.071836, "2024-06-19": 8.239032}
        feed = MarketDataFeed(source="akshare", adjust_out="qfq", factor_svc=StubFactorSvc(lookup))
        out = feed._maybe_adjust_bar(_make_bar(date="2024-06-19", close=1501.0))
        assert abs(out["close"] - 1501.0 / 8.239032) < 1e-6

    def test_raw_adjust_out_unchanged(self):
        """S2：默认 adjust_out='raw' 原样透传。"""
        feed = MarketDataFeed(source="akshare", factor_svc=StubFactorSvc({"2024-06-19": 8.239032}))
        out = feed._maybe_adjust_bar(_make_bar(date="2024-06-19", close=1501.0))
        assert out["close"] == 1501.0
