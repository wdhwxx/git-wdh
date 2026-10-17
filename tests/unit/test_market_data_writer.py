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


# ── 5. write_upsert UPSERT 语义（C-02 #453）──────────────────
def test_write_upsert_replace_semantics() -> None:
    conn = _make_conn()
    writer = MarketDataWriter(DataQueries(conn))
    first = [{"symbol": "000001.SZ", "market": "A股", "period": "daily",
              "adjust": "raw", "trade_date": "2024-01-02", "open": 10.0,
              "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1e6,
              "amount": 1e7, "data_source": "incremental_clean"}]
    assert writer.write_upsert(first, "000001.SZ", corrected_by="incremental_clean") == 1
    second = [{"symbol": "000001.SZ", "market": "A股", "period": "daily",
               "adjust": "raw", "trade_date": "2024-01-02", "open": 10.1,
               "high": 10.6, "low": 9.9, "close": 10.3, "volume": 1.1e6,
               "amount": 1.1e7, "data_source": "incremental_clean"}]
    assert writer.write_upsert(second, "000001.SZ", corrected_by="incremental_clean") == 1
    dq = DataQueries(conn)
    rows = dq.get_market_data("000001.SZ", adjust="raw")
    assert len(rows) == 1, "UPSERT 按键更新不新增"
    assert rows[0]["close"] == 10.3
    # 变化字段写台账（C-02 可审计回滚依据）
    ledger = dq.conn.execute("SELECT * FROM correction_ledger")
    assert len(ledger) >= 1
    by_field = {r["field"]: r for r in ledger}
    assert by_field["close"]["old_value"] == "10.2"  # 台账文本化存储
    assert by_field["close"]["new_value"] == "10.3"
    assert writer.write_upsert([], "000001.SZ") == 0


def test_write_upsert_no_change_skips_ledger() -> None:
    """同键同值：不更新、不写台账（防御 needs_write 之外的重复调用）。"""
    conn = _make_conn()
    writer = MarketDataWriter(DataQueries(conn))
    row = {"symbol": "000001.SZ", "market": "A股", "period": "daily",
           "adjust": "raw", "trade_date": "2024-01-02", "open": 10.0,
           "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1e6,
           "amount": 1e7, "data_source": "incremental_clean"}
    assert writer.write_upsert([row], "000001.SZ") == 1
    assert writer.write_upsert([row], "000001.SZ") == 0  # 无变化跳过
    dq = DataQueries(conn)
    assert len(dq.get_market_data("000001.SZ", adjust="raw")) == 1
    assert dq.conn.execute_one("SELECT COUNT(*) AS c FROM correction_ledger")["c"] == 0


def test_write_upsert_rows_monotonic() -> None:
    """行数单调不减：写回含 1 新 + 1 改，总行数不减少（绝不删除）。"""
    conn = _make_conn()
    writer = MarketDataWriter(DataQueries(conn))
    base = [
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-02", "open": 10.0,
         "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1e6,
         "amount": 1e7, "data_source": "incremental_clean"},
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-03", "open": 10.3,
         "high": 10.8, "low": 10.1, "close": 10.6, "volume": 1.2e6,
         "amount": 1.3e7, "data_source": "incremental_clean"},
    ]
    assert writer.write_upsert(base, "000001.SZ") == 2
    # 全量清洗后：1 行修正 + 1 行新增（01-04）
    after = [
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-02", "open": 10.1,
         "high": 10.6, "low": 9.9, "close": 10.3, "volume": 1.1e6,
         "amount": 1.1e7, "data_source": "incremental_clean"},
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-03", "open": 10.3,
         "high": 10.8, "low": 10.1, "close": 10.6, "volume": 1.2e6,
         "amount": 1.3e7, "data_source": "incremental_clean"},
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-04", "open": 10.4,
         "high": 10.9, "low": 10.2, "close": 10.7, "volume": 1.3e6,
         "amount": 1.4e7, "data_source": "incremental_clean"},
    ]
    n = writer.write_upsert(after, "000001.SZ")
    dq = DataQueries(conn)
    rows = dq.get_market_data("000001.SZ", adjust="raw")
    assert len(rows) == 3, "UPSERT 绝不删除：行数单调不减"
    assert n == 2  # 1 修正 + 1 新增
    ledger = dq.conn.execute_one(
        "SELECT COUNT(*) AS c FROM correction_ledger"
    )["c"]
    assert ledger == 6  # 01-02 行 open/high/low/close/volume/amount 六字段各记一条；01-03 无变化不记


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


# ── 8. write_back_symbol 废止 delete（C-02）──────────────────
def test_write_back_symbol_no_delete() -> None:
    """write_back_symbol 不再整标的删除：UPSERT 保留既有行 + 台账留痕。"""
    from myquant.data_access.full_clean_service import FullCleanService

    conn = _make_conn()
    # 预置 2 行旧数据
    dq = DataQueries(conn)
    dq.bulk_insert_market_data([
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-02", "open": 10.0,
         "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1e6,
         "amount": 1e7, "data_source": "tdx"},
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-03", "open": 10.3,
         "high": 10.8, "low": 10.1, "close": 10.6, "volume": 1.2e6,
         "amount": 1.3e7, "data_source": "tdx"},
    ], replace=False)
    svc = FullCleanService(config={"batch_id": "test"}, dry_run=False)
    # 清洗后：01-02 修正 + 01-04 新增（01-03 无变化）
    cleaned = [
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-02", "open": 10.1,
         "high": 10.6, "low": 9.9, "close": 10.3, "volume": 1.1e6,
         "amount": 1.1e7, "data_source": "incremental_clean"},
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-03", "open": 10.3,
         "high": 10.8, "low": 10.1, "close": 10.6, "volume": 1.2e6,
         "amount": 1.3e7, "data_source": "incremental_clean"},
        {"symbol": "000001.SZ", "market": "A股", "period": "daily",
         "adjust": "raw", "trade_date": "2024-01-04", "open": 10.4,
         "high": 10.9, "low": 10.2, "close": 10.7, "volume": 1.3e6,
         "amount": 1.4e7, "data_source": "incremental_clean"},
    ]
    rc = svc.write_back_symbol(dq, "000001.SZ", cleaned)
    assert rc == "written"
    rows = dq.get_market_data("000001.SZ", adjust="raw")
    assert len(rows) == 3, "绝不删除：3 行全保留"
    by_date = {r["trade_date"]: r for r in rows}
    assert by_date["2024-01-02"]["close"] == 10.3
    ledger = dq.conn.execute_one("SELECT COUNT(*) AS c FROM correction_ledger")["c"]
    assert ledger == 8  # 01-02 六值字段+data_source 共 7 条；01-03 仅 data_source(tdx→incremental_clean) 1 条

