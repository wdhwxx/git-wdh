# 文件名：tests/unit/test_correction_ledger.py
"""B-03 修正台账 + 门禁拒收记录读写单测。

覆盖：
  1. correction_ledger 写入 + 按 symbol/日期过滤查询；
  2. 旧值/新值文本化（含 None）；
  3. admission_reject 写入 + 查询（BLOCK 落痕）；
  4. 0012 迁移在临时库可执行且幂等、可降级。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from myquant.database.queries import DataQueries
from myquant.database.schema import (
    TABLE_NAMES,
    create_all_tables,
)

M0012 = importlib.import_module(
    "myquant.database.migrations.versions.0012_add_correction_ledger"
)


@pytest.fixture
def conn():
    """内存库 + 建全部表。"""
    from myquant.database.connection import DatabaseConnection

    db = DatabaseConnection(":memory:")
    create_all_tables(db)
    yield db
    db.close()


def test_correction_ledger_write_and_query(conn) -> None:
    """写入修正台账 + 按 symbol/日期过滤查询闭环。"""
    dq = DataQueries(conn)
    dq.record_correction(
        "000001.SZ", "2026-10-15", "close", 10.0, 10.5,
        corrected_by="full_clean_service", reason="复权修正",
    )
    dq.record_correction(
        "600519.SH", "2026-10-15", "volume", "57767", "5776700",
        corrected_by="unit_test", reason="单位换算",
    )
    rows = dq.query_corrections("000001.SZ")
    assert len(rows) == 1
    assert rows[0]["field"] == "close"
    assert rows[0]["old_value"] == "10.0"
    assert rows[0]["new_value"] == "10.5"
    # 按日期范围过滤全部命中
    assert len(dq.query_corrections(start_date="2026-10-15", end_date="2026-10-15")) == 2


def test_correction_old_value_none_text(conn) -> None:
    """old_value=None 存 NULL 不报错。"""
    dq = DataQueries(conn)
    dq.record_correction(
        "000001.SZ", "2026-10-15", "volume_ratio", None, "1.2",
        corrected_by="unit_test", reason="补值",
    )
    rows = dq.query_corrections("000001.SZ")
    assert len(rows) == 1
    assert rows[0]["old_value"] is None


def test_admission_reject_write_and_query(conn) -> None:
    """门禁拒收记录写入 + 查询闭环。"""
    dq = DataQueries(conn)
    dq.record_admission_reject(
        "000001.SZ", rule="high_low_conflict", detail="high=9.8 low=10.2",
        source="tdx", trade_date="2026-10-15",
    )
    dq.record_admission_reject(
        "600519.SH", rule="negative_price", detail="open=-1.0",
        source="akshare", trade_date="2026-10-14",
    )
    rows = dq.query_admission_rejects("000001.SZ")
    assert len(rows) == 1
    assert rows[0]["rule"] == "high_low_conflict"
    assert rows[0]["level"] == "BLOCK"
    assert len(dq.query_admission_rejects()) == 2


def test_migration_0012_upgrade_downgrade(tmp_path: Path) -> None:
    """0012 迁移在临时库可执行、幂等、可降级。"""
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    try:
        with engine.begin() as c:
            M0012._apply_upgrade(c)
        with engine.begin() as c:
            M0012._apply_upgrade(c)  # 幂等
        with engine.connect() as c:
            tables = {
                r[0] for r in c.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ))
            }
            assert "correction_ledger" in tables
            assert "admission_reject" in tables
        with engine.begin() as c:
            M0012._apply_downgrade(c)
        with engine.connect() as c:
            tables = {
                r[0] for r in c.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ))
            }
            assert "correction_ledger" not in tables
            assert "admission_reject" not in tables
    finally:
        engine.dispose()


def test_table_names_contains_ledger() -> None:
    """TABLE_NAMES 含两表（B-03 #443）。"""
    assert "correction_ledger" in TABLE_NAMES
    assert "admission_reject" in TABLE_NAMES
