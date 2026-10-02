# 文件名：tests/unit/test_akshare_source_list_filter.py
"""
#85 akshare_source.get_stock_list B 股映射加固单测：
  get_stock_list 不再把沪 B（90）/ 深 B（20）代码段映射为 A 股 symbol，
  与 tdx_source 的 B-4 过滤口径对齐（下载层彻底无 B 股出口）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from myquant.data_access.sources.akshare_source import AkshareDataSource


def _mk_source(monkeypatch: pytest.MonkeyPatch, df: pd.DataFrame) -> AkshareDataSource:
    """构造绕过 __init__ 的 AkshareDataSource，替换列表源与重试器。"""
    src = object.__new__(AkshareDataSource)
    fake_ak = SimpleNamespace(stock_zh_a_spot_em=None)
    monkeypatch.setattr(src, "_get_akshare", lambda: fake_ak)
    monkeypatch.setattr(src, "_retry_request", lambda *args, **kwargs: df)
    return src


def _spot_df() -> pd.DataFrame:
    """模拟东财实时行情 DataFrame（代码/名称/所属行业/所属地区）。"""
    rows = [
        ("000001", "平安银行", "银行", "深圳"),
        ("300001", "特锐德", "电气设备", "青岛"),
        ("600519", "贵州茅台", "白酒", "遵义"),
        ("688001", "华兴源创", "专用设备", "苏州"),
        ("832001", "北交所示例", "通用设备", "北京"),
        ("900901", "沪B示例", "机械", "上海"),
        ("200002", "深B示例", "食品", "深圳"),
        ("400001", "老三板示例", "其他", "北京"),
        ("999999", "非法段", "其他", "北京"),
    ]
    return pd.DataFrame(rows, columns=["代码", "名称", "所属行业", "所属地区"])


class TestAkshareGetStockListFilter:
    def test_b_share_not_mapped(self, monkeypatch: pytest.MonkeyPatch):
        """B 股（900/200）与老三板/非法段被过滤，A 股与北交所保留。"""
        src = _mk_source(monkeypatch, _spot_df())
        stocks = src.get_stock_list()
        symbols = sorted(s["symbol"] for s in stocks)
        assert symbols == [
            "000001.SZ", "300001.SZ", "600519.SH", "688001.SH", "832001.BJ",
        ]
        # 无 B 股 / 无老三板 / 无非法段
        assert all(not s.startswith(("900", "200", "400", "999")) for s in symbols)
        # 北交所已纳入
        assert any(s.endswith(".BJ") for s in symbols)

    def test_empty_spot_returns_empty(self, monkeypatch: pytest.MonkeyPatch):
        """列表源返回空 → 返回空列表（不崩溃）。"""
        src = _mk_source(monkeypatch, pd.DataFrame(columns=["代码", "名称", "所属行业", "所属地区"]))
        assert src.get_stock_list() == []
