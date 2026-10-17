# 文件名：tests/unit/test_market_data_writer.py
"""C-03 MarketDataWriter 统一写入口单测（V-13 写入口收敛，蓝图 #452）。
覆盖：write_raw 默认关直通+ingest三列；开启后 BLOCK 拒收留痕/FLAG 落列/计数；
write_upsert INSERT OR REPLACE 覆盖语义；写失败可观测（抛 MarketDataWriteError 不静默）；
manager._save_to_database 委托 writer 门禁三态等价。全部离线确定性（:memory: 库）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from myquant.data_access.admission import AdmissionChecker
from myquant.data_access.market_data_writer import MarketDataWriteError, MarketDataWriter
from myquant.data_access.manager import DataManager
from myquant.data_access.sources.base import MarketDataResult
from myquant.database import DatabaseConnection, create_all_tables
from myquant.database.queries import DataQueries


class _StubFetcher:
    def get_market_data(self, *args: Any, **kwargs: Any) -> MarketDataResult:
        raise AssertionError("测试不应触发真实拉取")

    def get_market_data_batch(self, *args: Any, **kwargs: Any) -> dict:
        raise AssertionError("测试不应触发真实拉取")


def _make_conn() -> DatabaseConnection:
    conn = DatabaseConnection(db_path=":memory:")
    create_all_tables(conn)
    return conn


def _checker(enabled: bool) -> AdmissionChecker:
    checker = AdmissionChecker(config_path=Path("not_exists.yaml"))
    checker._enabled = enabled
    return checker


def _rows(n: int) -> List[dict]:
    return [
        {"date": f"2024-01-{i:02d}", "open": 10.0 + i, "high": 10.6 + i,
         "low": 9.9 + i, "close": 10.2 + i, "volume": 1e6,
         "amount": 1e7, "pct_chg": 1.0, "turnover": 0.1}
        for i in range(1, n + 1)
    ]


# ── 1. write_raw 默认关直通 ──────────────────────────────────
def test_write_raw_disabled_passthrough() -> None:
    conn = _make_conn()
    writer = MarketDataWriter(DataQueries(conn), admission_checker=_checker(False))
    stats = writer.write_raw(
        _rows(2), "000001.SZ", "A股", "daily", "raw", "tdx",
        "2024-01-01T09:00:00", "20240101-0001",
    )
    assert stats == {"written": 2, "blocked": 0, "flagged": 0}
    dq = DataQueries(conn)
    rows = dq.get_market_data("000001.SZ", adjust="raw")
    assert len(rows) == 2
    assert all(r["ingest_time"] == "2024-01-01T09:00:00" for r in rows)
    assert all(r["anomaly_flags"] is None for r in rows)


# ── 2. write_raw 开启后 BLOCK ────────────────────────────────
def test_write_raw_block_rejects() -> None:
    conn = _make_conn()
    writer = MarketDataWriter(DataQueries(conn), admission_checker=_checker(True))
    bad_row = {"date": "2024-01-02", "open": 10.0, "high": 5.0, "low": 9.8,
               "close": 10.2, "volume": 1e6}
    stats = writer.write_raw(
        [bad_row], "000001.SZ", "A股", "daily", "raw", "tdx",
        "2024-01-01T09:00:00", "20240101-0001",
    )
    assert stats == {"written": 0, "blocked": 1, "flagged": 0}
    dq = DataQueries(conn)
    assert dq.get_market_data("000001.SZ", adjust="raw") == []
    rejects = dq.query_admission_rejects(symbol="000001.SZ")
    assert len(rejects) == 1 and rejects[0]["rule"] == "high_low_conflict"


# ── 3. write_raw 开启后 FLAG ────────────────────────────────
def test_write_raw_flag_marks() -> None:
    conn = _make_conn()
    writer = MarketDataWriter(DataQueries(conn), admission_checker=_checker(True))
    rows = [
        {"date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8,
         "close": 10.2, "volume": 1e6, "amount": 1e7, "pct_chg": 1.0, "turnover": 0.1},
        {"date": "2024-01-03", "open": 29.0, "high": 31.0, "low": 28.5,
         "close": 30.0, "volume": 2e8, "amount": 6e9, "pct_chg": 194.0, "turnover": 0.5},
    ]
    stats = writer.write_raw(
        rows, "000001.SZ", "A股", "daily", "raw", "tdx",
        "2024-01-01T09:00:00", "20240101-0001",
    )
    assert stats == {"written": 2, "blocked": 0, "flagged": 1}
    dq = DataQueries(conn)
    db_row = dq.get_market_data("000001.SZ", start_date="2024-01-03",
                                end_date="2024-01-03", adjust="raw")[0]
    assert "price_jump" in (db_row["anomaly_flags"] or "")


# ── 4. prev_row 上下文 ──────────────────────────────────────
def test_write_raw_prev_row_context() -> None:
    conn = _make_conn()
    writer = MarketDataWriter(DataQueries(conn), admission_checker=_checker(True))
    prev = {"date": "2024-01-02", "close": 10.2, "volume": 1e6, "data_source": "tdx"}
    jump = {"date": "2024-01-04", "open": 39.0, "high": 41.0, "low": 38.5,
            "close": 40.0, "volume": 1.5e5, "amount": 6e6, "pct_chg": 292.0,
            "turnover": 0.3}
    stats = writer.write_raw(
        [jump], "000001.SZ", "A股", "daily", "raw", "tdx",
        "2024-01-01T09:00:00", "20240101-0001", prev_row=prev,
    )
    assert stats["flagged"] == 1
    dq = DataQueries(conn)
    db_row = dq.get_market_data("000001.SZ", start_date="2024-01-04",
                                end_date="2024-01-04", adjust="raw")[0]
    assert "price_jump" in (db_row["anomaly_flags"] or "")


# ── 5. write_upsert 覆盖语义 ────────────────────────────────
def test_write_upsert_replace_semantics() -> None:
    conn = _make_conn()
    # B-01 部分唯一索引（生产库已迁移；内存库需手动补建以复现 INSERT OR REPLACE 语义）
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_data_daily_key "
        "ON market_data(symbol, period, adjust, trade_date) "
        "WHERE trade_time IS NULL"
    )
    writer = MarketDataWriter(DataQueries(conn))
    first = [{"symbol": "000001.SZ", "market": "A股", "period": "daily",
              "adjust": "raw", "trade_date": "2024-01-02", "open": 10.0,
              "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1e6,
              "amount": 1e7, "data_source": "incremental_clean"}]
    assert writer.write_upsert(first, "000001.SZ") == 1
    second = [{"symbol": "000001.SZ", "market": "A股", "period": "daily",
               "adjust": "raw", "trade_date": "2024-01-02", "open": 10.1,
               "high": 10.6, "low": 9.9, "close": 10.3, "volume": 1.1e6,
               "amount": 1.1e7, "data_source": "incremental_clean"}]
    assert writer.write_upsert(second, "000001.SZ") == 1
    dq = DataQueries(conn)
    rows = dq.get_market_data("000001.SZ", adjust="raw")
    assert len(rows) == 1, "INSERT OR REPLACE 应按键覆盖不新增"
    assert rows[0]["close"] == 10.3
    assert writer.write_upsert([], "000001.SZ") == 0


# ── 6. 写失败可观测 ─────────────────────────────────────────
def test_write_failure_is_observable() -> None:
    """坏 DB（无表）注入：write_raw 抛 MarketDataWriteError（不静默 return 0）。"""
    conn = DatabaseConnection(db_path=":memory:")  # 未建表
    writer = MarketDataWriter(DataQueries(conn), admission_checker=_checker(False))
    with pytest.raises(MarketDataWriteError):
        writer.write_raw(
            _rows(1), "000001.SZ", "A股", "daily", "raw", "tdx",
            "2024-01-01T09:00:00", "20240101-0001",
        )


def test_write_upsert_failure_is_observable() -> None:
    conn = DatabaseConnection(db_path=":memory:")
    writer = MarketDataWriter(DataQueries(conn))
    with pytest.raises(MarketDataWriteError):
        writer.write_upsert(
            [{"symbol": "000001.SZ", "trade_date": "2024-01-02"}], "000001.SZ",
        )


# ── 7. 收敛接线：manager 委托（门禁三态等价）─────────────────
def test_manager_delegates_to_writer() -> None:
    """manager._save_to_database 委托 writer：BLOCK 拒收 + 计数 + 留痕等价。"""
    conn = _make_conn()
    manager = DataManager(fetcher=_StubFetcher(), db_connection=conn)
    manager._admission_checker = _checker(True)
    bad_row = {"date": "2024-01-02", "open": 10.0, "high": 5.0, "low": 9.8,
               "close": 10.2, "volume": 1e6}
    result = MarketDataResult(
        symbol="000001.SZ", data=[bad_row], source="tdx",
        market="A股", period="daily", adjust="raw",
    )
    n = manager._save_to_database("000001.SZ", result, "daily", "raw")
    assert n == 0
    assert manager._admission_stats == {"blocked": 1, "flagged": 0}
    dq = DataQueries(conn)
    assert dq.query_admission_rejects(symbol="000001.SZ") != []

