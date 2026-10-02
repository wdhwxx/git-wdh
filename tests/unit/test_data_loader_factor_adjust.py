# 文件名：tests/unit/test_data_loader_factor_adjust.py
"""
data_loader 复权现算单测（方案B #82）
========================================
覆盖：raw 原样 / hfq=raw×factor（事件日及之后生效）/ qfq=hfq/最新因子 /
as_of 防未来截断 / 无因子降级 raw。

processed 数据用合成 parquet（中文表头），因子用 stub，零网络。
"""

from __future__ import annotations

import pandas as pd
import pytest

from myquant.backtest.data_loader import HistoricalDataProvider


class StubFactorSvc:
    """stub 因子服务：记录 as_of 并返回预置因子表。"""

    def __init__(self, lookup: dict, symbol: str = "600519.SH"):
        self._lookup = lookup
        self._symbol = symbol
        self.as_of_calls: list[str | None] = []

    def get_factor_lookup(self, symbol: str, as_of: str | None = None) -> dict:
        self.as_of_calls.append(as_of)
        return self._lookup if symbol == self._symbol else {}


def _make_provider(tmp_path, close_prices, factor_svc=None):
    """构造临时 processed 目录 + 合成 raw parquet（2024-06-18~19）。"""
    dates = pd.to_datetime(["2024-06-18", "2024-06-19"])
    df = pd.DataFrame(
        {
            "开盘价": close_prices,
            "最高价": [p * 1.01 for p in close_prices],
            "最低价": [p * 0.99 for p in close_prices],
            "收盘价": close_prices,
            "成交量": [1000, 1200],
            "成交额": [20000, 24000],
            "涨跌幅": [
                None,
                round((close_prices[1] - close_prices[0]) / close_prices[0] * 100.0, 4),
            ],
            "振幅": [2.0, 2.0],
        },
        index=dates,
    )
    df.index.name = "日期"
    processed = tmp_path / "processed"
    processed.mkdir(parents=True)
    df.to_parquet(processed / "600519.SH.parquet")
    return HistoricalDataProvider(processed_dir=processed, factor_svc=factor_svc)


class TestRawPassThrough:
    def test_raw_unchanged(self, tmp_path):
        provider = _make_provider(tmp_path, [1521.5, 1501.0])
        df = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="raw")
        assert list(df["收盘价"]) == [1521.5, 1501.0]

    def test_default_adjust_raw(self, tmp_path):
        provider = _make_provider(tmp_path, [1521.5, 1501.0])
        df = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19")
        assert list(df["收盘价"]) == [1521.5, 1501.0]


class TestFactorAdjust:
    def test_hfq_apply_factor_at_event(self, tmp_path):
        """hfq = raw × factor；事件日（06-19）启用新因子。"""
        svc = StubFactorSvc({"2024-06-18": 8.071836, "2024-06-19": 8.239032})
        provider = _make_provider(tmp_path, [1521.5, 1501.0], factor_svc=svc)
        df = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="hfq")
        assert abs(df.loc["2024-06-18", "收盘价"] - 1521.5 * 8.071836) < 0.01
        assert abs(df.loc["2024-06-19", "收盘价"] - 1501.0 * 8.239032) < 0.01

    def test_qfq_divide_by_latest(self, tmp_path):
        """qfq = hfq / 最新因子：事件日后 qfq==raw，事件前按 factor/latest 折算。"""
        svc = StubFactorSvc({"2024-06-18": 8.071836, "2024-06-19": 8.239032})
        provider = _make_provider(tmp_path, [1521.5, 1501.0], factor_svc=svc)
        df = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="qfq")
        assert abs(df.loc["2024-06-19", "收盘价"] - 1501.0) < 0.01  # 最新事件后 qfq==raw
        expected_prev = 1521.5 * 8.071836 / 8.239032
        assert abs(df.loc["2024-06-18", "收盘价"] - expected_prev) < 0.01

    def test_as_of_truncation_passes_to_service(self, tmp_path):
        """防未来：get_bars 的 end_date 透传给因子服务 as_of。"""
        svc = StubFactorSvc({"2024-06-18": 8.071836, "2024-06-19": 8.239032})
        provider = _make_provider(tmp_path, [1521.5, 1501.0], factor_svc=svc)
        provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="hfq")
        assert svc.as_of_calls[-1] == "2024-06-19"

    def test_no_factor_degrades_to_raw(self, tmp_path):
        """无因子事件：降级返回 raw 并告警，不阻断回测。"""
        svc = StubFactorSvc({})
        provider = _make_provider(tmp_path, [1521.5, 1501.0], factor_svc=svc)
        df = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="hfq")
        assert list(df["收盘价"]) == [1521.5, 1501.0]


class TestDerivedRecompute:
    """S1（#83）：hfq/qfq 现算后重算 涨跌幅/振幅，消除除权日 raw 假跳变。"""

    def test_hfq_pct_chg_no_fake_jump(self, tmp_path):
        """hfq 下除权日涨跌幅按复权价重算，不再是 raw 假跳变值。"""
        svc = StubFactorSvc({"2024-06-18": 8.071836, "2024-06-19": 8.239032})
        provider = _make_provider(tmp_path, [1521.5, 1501.0], factor_svc=svc)
        df = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="hfq")
        c0 = 1521.5 * 8.071836
        c1 = 1501.0 * 8.239032
        expected = (c1 - c0) / c0 * 100.0
        assert abs(df.loc["2024-06-19", "涨跌幅"] - expected) < 0.01
        assert df.loc["2024-06-19", "涨跌幅"] != -1.3474  # raw 假跳变被替换

    def test_qfq_pct_chg_same_ratio(self, tmp_path):
        """qfq 涨跌幅与 hfq 一致（差常数倍，比例不变）。"""
        svc = StubFactorSvc({"2024-06-18": 8.071836, "2024-06-19": 8.239032})
        provider = _make_provider(tmp_path, [1521.5, 1501.0], factor_svc=svc)
        df_qfq = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="qfq")
        df_hfq = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="hfq")
        assert abs(df_qfq.loc["2024-06-19", "涨跌幅"] - df_hfq.loc["2024-06-19", "涨跌幅"]) < 0.0001

    def test_raw_derived_unchanged(self, tmp_path):
        """raw 下派生量保持原文件值（不重算）。"""
        provider = _make_provider(tmp_path, [1521.5, 1501.0])
        df = provider.get_bars("600519.SH", start_date="2024-06-18", end_date="2024-06-19", adjust="raw")
        assert df.loc["2024-06-19", "涨跌幅"] == -1.3474
        assert df.loc["2024-06-19", "振幅"] == 2.0
        assert pd.isna(df.loc["2024-06-18", "涨跌幅"])  # 首行无前收盘价 None
