# -*- coding: utf-8 -*-
"""
A-07 volume 单位契约统一 —— 回归样例单测（5 项，离线可复现，不依赖网络/生产库）

契约：全库 volume 统一为「股」。
- 东财历史/分钟（akshare stock_zh_a_hist / stock_zh_a_hist_min_em）原生=手 → _df_to_records 换算 ×100（A-07 #438）
- 腾讯历史（akshare_source._try_tencent_hist）/ 腾讯实时（live/realtime_sources）原生=手 → 已 ×100
- 网易原生 HTTP(chddata) 原生=股 → 直取不换算
- 新浪（英文 volume 列）原生=股 → 直取不换算
- schema.py market_data.volume 注释=股

实测依据（2026-10-17，600519.SH）：tdx 库 5,767,722（股） vs akshare 东财 57,677（手），ratio=99.9986~100.0004。
"""
import os
import pandas as pd

import pytest

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
SCHEMA_PATH = os.path.join(PROJECT_ROOT, "myquant", "database", "schema.py")
AKSHARE_PATH = os.path.join(
    PROJECT_ROOT, "myquant", "data_access", "sources", "akshare_source.py"
)
REALTIME_PATH = os.path.join(
    PROJECT_ROOT, "myquant", "live", "realtime_sources.py"
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def source():
    from myquant.data_access.sources.akshare_source import AkshareDataSource
    return AkshareDataSource(config={"timeout": 5})


# ── 1. schema 契约：volume 注释=股 ──────────────────────────────
def test_schema_volume_comment_is_shares():
    src = _read(SCHEMA_PATH)
    assert "-- 成交量（股" in src, "schema.py market_data.volume 注释应统一为（股）"
    assert "-- 成交量（手）" not in src, "schema.py 不应残留（手）注释"


# ── 2. 东财路径：中文「成交量」列（手）→ 股（×100） ─────────────
def test_eastmoney_volume_hand_to_shares(source):
    df = pd.DataFrame({
        "日期": ["2026-10-15", "2026-10-14"],
        "开盘": [1310.0, 1280.0],
        "收盘": [1312.97, 1286.0],
        "最高": [1315.0, 1290.0],
        "最低": [1305.0, 1275.0],
        "成交量": [57677, 23773],   # 东财单位=手（实测 ratio≈100）
        "成交额": [7.57e9, 3.06e9],
        "涨跌幅": [2.08, 0.89],
    })
    records = source._df_to_records(df)
    assert len(records) == 2
    assert records[0]["volume"] == 57677 * 100.0, "东财成交量（手）应 ×100 换算为股"
    assert records[1]["volume"] == 23773 * 100.0
    assert records[0]["date"] == "2026-10-15"
    assert records[0]["close"] == 1312.97, "非 volume 字段不应被换算"


# ── 3. 新浪路径：英文 volume 列（原生=股）直取不换算 ─────────────
def test_sina_volume_raw_no_scale(source):
    df = pd.DataFrame({
        "date": ["2026-10-15"],
        "open": [1310.0],
        "high": [1315.0],
        "low": [1305.0],
        "close": [1312.97],
        "volume": [5767700],        # 新浪单位=股，直取
        "amount": [7.57e9],
        "pct_chg": [2.08],
    })
    records = source._df_to_records(df)
    assert len(records) == 1
    assert records[0]["volume"] == 5767700.0, "新浪 volume（股）不应被换算"


# ── 4. 腾讯历史 + 腾讯实时：手→股 ×100（静态回归） ──────────────
def test_tencent_paths_scale_100():
    ak_src = _read(AKSHARE_PATH)
    rt_src = _read(REALTIME_PATH)
    # 腾讯历史（akshare_source _try_tencent_hist）
    assert "* 100.0" in ak_src and "手→股" in ak_src, "腾讯历史路径应含 ×100 手→股"
    # 腾讯实时（realtime_sources）
    assert "volume_hand * 100.0" in rt_src and "手 → 股" in rt_src, "腾讯实时路径应含 ×100 手→股"


# ── 5. 网易路径：原生=股，直取不换算（静态回归） ─────────────────
def test_netease_volume_raw_no_scale():
    ak_src = _read(AKSHARE_PATH)
    # 网易解析块（_try_netease_hist records 构造）应无 *100 换算
    block_start = ak_src.find('def _try_netease_hist')
    assert block_start != -1, "找不到 _try_netease_hist"
    block = ak_src[block_start: ak_src.find("def _try_sina_hist", block_start)]
    assert '"volume": self._safe_float(row.get("成交量"))' in block, "网易 volume 应直取原生（股）"
    assert "* 100.0" not in block, "网易路径不应做 ×100 换算（原生=股）"
