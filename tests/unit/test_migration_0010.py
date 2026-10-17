# 文件名：tests/unit/test_migration_0010.py
"""B-01 部分唯一索引迁移单测。

覆盖：
  1. 0010 upgrade DDL 在临时 sqlite 可执行，索引创建成功；
  2. 部分唯一索引生效：重复键（trade_time IS NULL）插入被拒；
  3. 分钟线不受影响：同键但 trade_time 非空可插入；
  4. downgrade 可删索引，约束解除。
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

M0010 = importlib.import_module(
    "myquant.database.migrations.versions.0010_partial_unique_daily"
)


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    """构造含 market_data 最小结构的临时库。"""
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE market_data (symbol TEXT, period TEXT, adjust TEXT, "
        "trade_date TEXT, trade_time TEXT, open REAL, high REAL, low REAL, "
        "close REAL, data_source TEXT, created_at TEXT)"
    )
    return conn


def _insert(conn: sqlite3.Connection, symbol: str, trade_time: str | None,
            created_at: str = "2026-10-17 09:00:00") -> None:
    conn.execute(
        "INSERT INTO market_data (symbol, period, adjust, trade_date, "
        "trade_time, open, high, low, close, data_source, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, "daily", "raw", "2026-10-15", trade_time, 10.0, 10.2, 9.9,
         10.1, "tdx", created_at),
    )


def test_upgrade_creates_partial_unique_index(tmp_path: Path) -> None:
    """upgrade DDL 可执行且索引存在。"""
    conn = _make_db(tmp_path)
    try:
        _insert(conn, "000001.SZ", None)
        conn.commit()
        conn.execute(M0010._CREATE_UNIQUE_INDEX_SQL)
        conn.commit()
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (M0010._NEW_INDEX_NAME,),
        ).fetchone()
        assert row is not None
        assert "WHERE trade_time IS NULL" in row[0]
    finally:
        conn.close()


def test_unique_enforced_for_daily_duplicates(tmp_path: Path) -> None:
    """日线重复键（trade_time IS NULL）插入被唯一索引拒绝。"""
    conn = _make_db(tmp_path)
    try:
        _insert(conn, "000001.SZ", None)
        conn.commit()
        conn.execute(M0010._CREATE_UNIQUE_INDEX_SQL)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, "000001.SZ", None)
    finally:
        conn.close()


def test_minute_rows_unaffected(tmp_path: Path) -> None:
    """同键但 trade_time 非空（分钟线）不受部分唯一索引约束。"""
    conn = _make_db(tmp_path)
    try:
        _insert(conn, "000001.SZ", None)
        _insert(conn, "000001.SZ", "09:30:00")
        conn.commit()
        conn.execute(M0010._CREATE_UNIQUE_INDEX_SQL)
        conn.commit()
        # 分钟线重复插入同一键不同 trade_time 可成功
        _insert(conn, "000001.SZ", "09:31:00")
        conn.commit()
    finally:
        conn.close()


def test_downgrade_drops_index(tmp_path: Path) -> None:
    """downgrade 删除索引后约束解除。"""
    conn = _make_db(tmp_path)
    try:
        _insert(conn, "000001.SZ", None)
        conn.commit()
        conn.execute(M0010._CREATE_UNIQUE_INDEX_SQL)
        conn.commit()
        conn.execute(M0010._DROP_INDEX_SQL)
        conn.commit()
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (M0010._NEW_INDEX_NAME,),
        ).fetchone()
        assert row is None
        _insert(conn, "000001.SZ", None)
        conn.commit()
    finally:
        conn.close()
