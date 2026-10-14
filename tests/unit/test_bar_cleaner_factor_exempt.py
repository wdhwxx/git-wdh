# 文件名：tests/unit/test_bar_cleaner_factor_exempt.py
"""
清洗除权跳变豁免单测（方案B #82）
====================================
覆盖：函数版/类版跳变豁免（除权日 -50% 缺口被因子吸收）、
非除权日跳变仍检测、无因子表回退原行为。

全部合成数据，零网络。
"""

from __future__ import annotations

from myquant.data_access.cleaner.bar_cleaner import (
    BarCleaner,
    _factor_adjusted_jump_pct,
    clean_bar_records,
)

#: 合成场景：10送10 → 除息日 06-19 open 腰斩 -50%（raw 口径超阈值，hfq 无缺口）
_RECORDS = [
    {"symbol": "600519.SH", "trade_date": "2024-06-18", "open": 20.0, "high": 20.5,
     "low": 19.5, "close": 20.0, "volume": 1000, "amount": 20000},
    {"symbol": "600519.SH", "trade_date": "2024-06-19", "open": 10.0, "high": 10.5,
     "low": 9.8, "close": 10.0, "volume": 1500, "amount": 15000},
]
#: 除息日因子翻倍（10送10）
_FACTORS = {"2024-06-18": 2.0, "2024-06-19": 4.0}
_BASE_CFG = {"max_price_jump_pct": 0.22, "price_jump_action": "drop", "detect_price_jump": True}


class TestFunctionCleanBarRecords:
    def test_without_factor_jump_dropped(self):
        cleaned, bad, _ = clean_bar_records([dict(r) for r in _RECORDS], dict(_BASE_CFG))
        assert len(cleaned) == 1 and len(bad) == 1  # 06-19 因 -50% 跳变被剔除

    def test_with_factor_jump_exempted(self):
        cfg = dict(_BASE_CFG, factor_lookup=_FACTORS)
        cleaned, bad, _ = clean_bar_records([dict(r) for r in _RECORDS], cfg)
        assert len(cleaned) == 2 and len(bad) == 0  # 除权缺口被因子吸收 → 豁免

    def test_non_event_day_jump_still_detected(self):
        """非除权日（因子表存在但两日无事件）：跳变仍按 raw 口径检测。"""
        recs = [
            {"symbol": "000001.SZ", "trade_date": "2024-06-17", "open": 10.0, "high": 10.2,
             "low": 9.8, "close": 10.0, "volume": 1000, "amount": 10000},
            {"symbol": "000001.SZ", "trade_date": "2024-06-18", "open": 5.0, "high": 5.2,
             "low": 4.8, "close": 5.0, "volume": 1000, "amount": 5000},
        ]
        cfg = dict(_BASE_CFG, factor_lookup={"2023-01-01": 1.0})
        cleaned, bad, _ = clean_bar_records(recs, cfg)
        assert len(cleaned) == 1 and len(bad) == 1


class TestBarCleanerClass:
    def test_class_exempt(self):
        cl = BarCleaner(config=dict(_BASE_CFG, factor_lookup=_FACTORS))
        cleaned, bad, _ = cl.clean([dict(r) for r in _RECORDS])
        assert len(cleaned) == 2 and len(bad) == 0

    def test_class_without_factor(self):
        cl = BarCleaner(config=dict(_BASE_CFG))
        cleaned, bad, _ = cl.clean([dict(r) for r in _RECORDS])
        assert len(cleaned) == 1 and len(bad) == 1


class TestHelper:
    def test_factor_adjusted_pct_zero_for_split(self):
        pct = _factor_adjusted_jump_pct(_RECORDS[0], _RECORDS[1], _FACTORS)
        assert pct is not None and abs(pct) < 0.01  # hfq 口径无缺口

    def test_no_lookup_returns_none(self):
        assert _factor_adjusted_jump_pct(_RECORDS[0], _RECORDS[1], None) is None
        assert _factor_adjusted_jump_pct(_RECORDS[0], _RECORDS[1], {}) is None

    def test_no_events_returns_none(self):
        assert _factor_adjusted_jump_pct(_RECORDS[0], _RECORDS[1], {"2023-01-01": 1.0}) is None
