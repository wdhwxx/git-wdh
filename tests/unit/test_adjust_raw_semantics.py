# 文件名：tests/unit/test_adjust_raw_semantics.py
"""
test_adjust_raw_semantics.py - P0-1 复权口径语义单测（raw 不复权原始层）
========================================================================
覆盖（蓝图回写 #80 配套，2026-10-01）：
  1. 各层默认 adjust 均为 "raw"（SyncConfig 字段 / HistoricalDataProvider 构造 / DataQueries 三查询签名）
  2. manager._save_to_database 标签透传：adjust="raw" 原样写入 DB
  3. 查询隔离：默认查询只命中 raw 行，不命中 hfq 行（杜绝跨源量纲混读）
  4. 网源映射：WebSourceDataSource 将 "raw" 映射为不复权参数 ""（东财 adjust="" / 腾讯 day）

设计原则：
  - 全部离线确定性（:memory: 库 + monkeypatch，零网络）
  - 严格禁用未来函数：所有日期为历史固定值
"""
from __future__ import annotations

import dataclasses
import inspect
from typing import Any, List

from myquant.backtest.data_loader import HistoricalDataProvider
from myquant.data_access.manager import DataManager, DatasetVersion
from myquant.data_access.sources.base import MarketDataResult
from myquant.data_access.sources.web_source import WebSourceDataSource
from myquant.database import DatabaseConnection, create_all_tables
from myquant.database.queries import DataQueries

_INSERT_SQL = (
    "INSERT INTO market_data (symbol, trade_date, period, market, adjust, "
    "open, high, low, close, volume, amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class _StubFetcher:
    """最小 fetcher stub：仅满足 DataManager 构造，任何拉取调用都应为失败。"""

    def get_market_data(self, *args: Any, **kwargs: Any) -> MarketDataResult:
        raise AssertionError("测试不应触发真实拉取")

    def get_market_data_batch(self, *args: Any, **kwargs: Any) -> dict:
        raise AssertionError("测试不应触发真实拉取")


# ── 1. 各层默认值 = raw ──────────────────────────────────────────────

def test_defaults_are_raw() -> None:
    """manager/回测加载器/查询层 默认 adjust 均为 'raw'。"""
    f = next(f for f in dataclasses.fields(DatasetVersion) if f.name == "adjust")
    assert f.default == "raw", f"DatasetVersion.adjust 默认应为 raw，实际 {f.default!r}"

    params = inspect.signature(HistoricalDataProvider.__init__).parameters
    assert params["default_adjust"].default == "raw", "HistoricalDataProvider 默认应为 raw"

    for method_name in ("get_market_data", "get_market_data_batch", "get_latest_market_date"):
        sig = inspect.signature(getattr(DataQueries, method_name))
        assert sig.parameters["adjust"].default == "raw", f"DataQueries.{method_name} 默认应为 raw"


# ── 2. 入库标签透传 ──────────────────────────────────────────────────

def test_save_to_database_writes_raw_label() -> None:
    """manager._save_to_database 以 adjust='raw' 保存时，DB 中原样写入 raw 标签。"""
    conn = DatabaseConnection(db_path=":memory:")
    create_all_tables(conn)
    manager = DataManager(fetcher=_StubFetcher(), db_connection=conn)

    data: List[dict] = [
        {"date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
         "volume": 100000.0, "amount": 1020000.0, "pct_chg": 1.0, "turnover": 0.1},
        {"date": "2024-01-03", "open": 10.2, "high": 10.8, "low": 10.0, "close": 10.7,
         "volume": 120000.0, "amount": 1270000.0, "pct_chg": 4.9, "turnover": 0.12},
    ]
    result = MarketDataResult(
        symbol="000001.SZ", data=data, source="tdx",
        market="A股", period="daily", adjust="raw",
    )
    n = manager._save_to_database("000001.SZ", result, "daily", "raw")
    assert n == 2, f"应写入 2 行，实际 {n}"

    dq = DataQueries(conn)
    rows = dq.get_market_data("000001.SZ", start_date="2024-01-02", end_date="2024-01-03",
                              adjust="raw")
    assert len(rows) == 2
    assert all(r["adjust"] == "raw" for r in rows)


# ── 3. 查询隔离：raw 与 hfq 不混读 ───────────────────────────────────

def test_query_default_hits_raw_only() -> None:
    """默认查询只命中 raw 行；hfq 行（历史遗留/未来方案B）不得混入结果。"""
    conn = DatabaseConnection(db_path=":memory:")
    create_all_tables(conn)
    conn.execute_update(_INSERT_SQL, [
        "000001.SZ", "2024-01-02", "daily", "A股", "raw",
        10.0, 10.5, 9.8, 10.2, 100000.0, 1020000.0,
    ])
    conn.execute_update(_INSERT_SQL, [
        "000001.SZ", "2024-01-02", "daily", "A股", "hfq",
        20.0, 21.0, 19.5, 20.4, 100000.0, 2040000.0,
    ])

    dq = DataQueries(conn)
    rows = dq.get_market_data("000001.SZ", start_date="2024-01-02", end_date="2024-01-02")
    assert len(rows) == 1, f"默认查询应只命中 raw 1 行，实际 {len(rows)}"
    assert rows[0]["adjust"] == "raw"
    assert rows[0]["close"] == 10.2

    # 显式查 hfq 也不得混读（语义隔离）
    rows_hfq = dq.get_market_data("000001.SZ", start_date="2024-01-02", end_date="2024-01-02",
                                  adjust="hfq")
    assert len(rows_hfq) == 1 and rows_hfq[0]["adjust"] == "hfq"


# ── 4. 网源 raw 映射为不复权参数 ─────────────────────────────────────

def test_web_source_raw_maps_to_no_adjust(monkeypatch: Any) -> None:
    """WebSourceDataSource(adjust='raw') 传给各源 fetch_fn 的复权参数必须为 ''（不复权）。"""
    src = WebSourceDataSource()
    captured: dict = {}

    def fake_fetch(ak: Any, symbol: str, start: str, end: str, adjust_param: str) -> List[dict]:
        captured["adjust_param"] = adjust_param
        return []

    monkeypatch.setattr(src, "_get_akshare", lambda: object())
    monkeypatch.setattr(src, "_build_chain", lambda ak: [("fake", fake_fetch)])

    res = src.get_market_data("000001.SZ", "2024-01-01", "2024-01-02", "daily", "raw")
    assert captured["adjust_param"] == "", f"raw 应映射为 ''，实际 {captured['adjust_param']!r}"
    assert res.adjust == "raw"
    assert res.count == 0  # fake 空结果 → 空 MarketDataResult
