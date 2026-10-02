# 文件名：tests/unit/test_web_source_volume_unit.py
"""
P1-④（#83）网源 volume 单位统一：全库口径=股。

腾讯 K 线源 volume 为手（×100 转股，与 data_feed 腾讯实时一致）；
网易源 volume 已是股（保持不动）；TDX .day 实为股（见 test_tdx_source_parse）。
"""

from __future__ import annotations

import requests
import pytest

from myquant.data_access.sources.akshare_source import AkshareDataSource


class _FakeResp:
    def __init__(self, payload: dict | None = None, text: str = ""):
        self._payload = payload or {}
        self._text = text
        self.text = text  # 网易源读取 resp.text（CSV）

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class TestTencentKlineVolume:
    def test_hand_to_share_x100(self, monkeypatch):
        """腾讯 K 线 volume（手）×100 → 股。"""
        payload = {"data": {"sh600519": {"day": [["2024-06-19", 1501, 1501, 1501, 1501, 12345, 1]]}}}
        monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResp(payload=payload))
        src = AkshareDataSource()
        records, source = src._try_tencent_hist("600519.SH", "2024-06-19", "2024-06-19", None)
        assert source == "tencent_http"
        assert records[0]["volume"] == 12345.0 * 100.0


class TestNeteaseVolumeKept:
    def test_share_unchanged(self, monkeypatch):
        """网易 volume 已是股，保持不动。"""
        csv_text = (
            "日期,股票代码,名称,开盘价,最高价,最低价,收盘价,成交量,成交金额,涨跌幅,换手率\n"
            "2024-06-19,600519,贵州茅台,1500,1510,1495,1501,12345,185000000,0.5,0.1"
        )
        monkeypatch.setattr(requests, "get", lambda url, timeout: _FakeResp(text=csv_text))
        src = AkshareDataSource()
        records, source = src._try_netease_hist("600519.SH", "20240619", "20240619")
        assert source == "netease_http"
        assert records[0]["volume"] == 12345.0
