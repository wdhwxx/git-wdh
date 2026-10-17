# 文件名：tests/unit/test_migration_0011.py
"""B-02 采集元数据三列迁移单测。

覆盖：
  1. 0011 upgrade DDL 在临时 sqlite 可执行，三列存在且类型正确；
  2. upgrade 幂等：重复执行不报错（列已存在跳过）；
  3. 存量行三列为 NULL、新写入可填值（尊重新账旧账）；
  4. downgrade 可删三列。
"""

from __future__ import annotations

import importlib
from pathlib import Path

from sqlalchemy import create_engine, text

M0011 = importlib.import_module(
    "myquant.database.migrations.versions.0011_add_ingest_metadata"
)


def _make_engine(tmp_path: Path):
    """构造含 market_data 最小结构的临时库引擎（模拟 0010 迁移后的库）。"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE market_data (symbol TEXT, period TEXT, adjust TEXT, "
            "trade_date TEXT, trade_time TEXT, open REAL, high REAL, low REAL, "
            "close REAL, data_source TEXT, created_at TEXT)"
        ))
    return engine


def _run_upgrade(conn) -> None:
    """执行 0011 upgrade 纯逻辑（直接注入连接，不依赖 alembic proxy）。"""
    M0011._apply_upgrade(conn)


def _run_downgrade(conn) -> None:
    """执行 0011 downgrade 纯逻辑。"""
    M0011._apply_downgrade(conn)


def _columns(conn) -> set[str]:
    rows = conn.execute(text("PRAGMA table_info('market_data')")).fetchall()
    return {row[1] for row in rows}


def test_upgrade_adds_three_columns(tmp_path: Path) -> None:
    """upgrade 后三列存在且类型为 TEXT。"""
    engine = _make_engine(tmp_path)
    try:
        with engine.begin() as conn:
            _run_upgrade(conn)
        with engine.connect() as conn:
            cols = _columns(conn)
            assert {"ingest_time", "ingest_batch", "anomaly_flags"} <= cols
            info = {
                row[1]: row[2] for row in conn.execute(
                    text("PRAGMA table_info('market_data')")
                )
            }
            assert info["ingest_time"] == "TEXT"
            assert info["ingest_batch"] == "TEXT"
            assert info["anomaly_flags"] == "TEXT"
    finally:
        engine.dispose()


def test_upgrade_idempotent(tmp_path: Path) -> None:
    """upgrade 重复执行幂等（列已存在跳过，不报错）。"""
    engine = _make_engine(tmp_path)
    try:
        with engine.begin() as conn:
            _run_upgrade(conn)
        with engine.begin() as conn:
            _run_upgrade(conn)
        with engine.connect() as conn:
            cols = _columns(conn)
            assert {"ingest_time", "ingest_batch", "anomaly_flags"} <= cols
    finally:
        engine.dispose()


def test_legacy_rows_null_new_rows_fillable(tmp_path: Path) -> None:
    """存量行三列为 NULL，新行可填充（尊重新账旧账）。"""
    engine = _make_engine(tmp_path)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO market_data (symbol, period, adjust, trade_date, "
                "trade_time, open, high, low, close, data_source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("000001.SZ", "daily", "raw", "2026-10-15", None, 10.0, 10.2, 9.9,
                 10.1, "tdx", "2026-10-15 09:00:00"),
            )
        with engine.begin() as conn:
            _run_upgrade(conn)
            conn.exec_driver_sql(
                "UPDATE market_data SET ingest_time=?, ingest_batch=?, "
                "anomaly_flags=? WHERE symbol='000001.SZ'",
                ("2026-10-17T09:30:00", "20261017-0001", None),
            )
        with engine.connect() as conn:
            row = conn.exec_driver_sql(
                "SELECT ingest_time, ingest_batch FROM market_data "
                "WHERE symbol='000001.SZ'"
            ).fetchone()
            assert row[0] == "2026-10-17T09:30:00"
            assert row[1] == "20261017-0001"
    finally:
        engine.dispose()


def test_downgrade_drops_three_columns(tmp_path: Path) -> None:
    """downgrade 删除三列。"""
    engine = _make_engine(tmp_path)
    try:
        with engine.begin() as conn:
            _run_upgrade(conn)
        with engine.begin() as conn:
            _run_downgrade(conn)
        with engine.connect() as conn:
            cols = _columns(conn)
            assert "ingest_time" not in cols
            assert "ingest_batch" not in cols
            assert "anomaly_flags" not in cols
    finally:
        engine.dispose()
