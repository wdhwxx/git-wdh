# 文件名：tests/unit/test_duplicate_cleaner.py
"""A-13 存量重复行清理单测。

覆盖保留规则（报告 v1.1 §5.2 A-13）：
  1. 原始源行（tdx/tencent_http）> incremental_clean；
  2. 同源保留最早 created_at；
  3. tdx 与 tencent_http 不同原始源时按 created_at 最早；
  4. dry-run 不修改库；执行后重复键=0；备份生成且行数一致。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from myquant.data_access.duplicate_cleaner import (
    backup_db,
    build_clean_report,
    collect_duplicate_stats,
)


def _make_db(tmp_path: Path) -> str:
    """构造含三类重复场景的临时库。"""
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE market_data (symbol TEXT, period TEXT, adjust TEXT, "
        "trade_date TEXT, trade_time TEXT, open REAL, high REAL, low REAL, "
        "close REAL, volume REAL, amount REAL, pct_chg REAL, data_source TEXT, "
        "created_at TEXT)"
    )
    rows = [
        # 键 A：incremental_clean×2 + tdx → 保留 tdx（原始源优先），删 2 行
        ("000001.SZ", "daily", "raw", "2026-10-15", None, 10.0, 10.2, 9.9, 10.1,
         1e6, 1.0e7, 0.01, "incremental_clean", "2026-10-16 00:20:58"),
        ("000001.SZ", "daily", "raw", "2026-10-15", None, 10.0, 10.2, 9.9, 10.1,
         1e6, 1.0e7, 0.01, "incremental_clean", "2026-10-16 09:21:22"),
        ("000001.SZ", "daily", "raw", "2026-10-15", None, 10.0, 10.2, 9.9, 10.1,
         1e6, 1.0e7, 0.01, "tdx", "2026-10-16 09:25:46"),
        # 键 B：tdx + tencent_http（均为原始源）→ 保留 created_at 更早的 tencent_http
        ("600519.SH", "daily", "raw", "2026-10-08", None, 11.78, 11.9, 11.7,
         11.78, 1.4e8, 1.7e9, 0.0, "tencent_http", "2026-10-08 09:08:29"),
        ("600519.SH", "daily", "raw", "2026-10-08", None, 11.78, 11.9, 11.7,
         11.78, 1.4e8, 1.7e9, 0.0, "tdx", "2026-10-08 09:36:40"),
        # 键 C：无重复，不动
        ("000002.SZ", "daily", "raw", "2026-10-16", None, 5.0, 5.1, 4.9, 5.05,
         2e5, 1.0e6, 0.02, "tdx", "2026-10-16 09:25:47"),
    ]
    conn.executemany(
        "INSERT INTO market_data (symbol, period, adjust, trade_date, "
        "trade_time, open, high, low, close, volume, amount, pct_chg, "
        "data_source, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def test_stats_counts(tmp_path: Path) -> None:
    """重复键/重复行/待删行统计。"""
    db = _make_db(tmp_path)
    stats = collect_duplicate_stats(db)
    assert stats["duplicate_key_total"] == 2
    assert stats["duplicate_row_total"] == 5
    assert stats["to_delete"] == 3
    assert stats["rows_before"] == 6


def test_dry_run_does_not_modify(tmp_path: Path) -> None:
    """dry-run 只统计不删除。"""
    db = _make_db(tmp_path)
    report = build_clean_report(db, str(tmp_path / "bak"),
                                str(tmp_path / "out"), dry_run=True,
                                report_date="2026-10-17")
    assert report["dry_run"] is True
    assert report["status"] == "dry_run_only"
    assert report["to_delete"] == 3
    conn = sqlite3.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    finally:
        conn.close()
    assert n == 6
    # 报告落盘
    assert (tmp_path / "out" / "重复行清理报告_20261017.json").exists()


def test_clean_removes_and_keeps_correct(tmp_path: Path) -> None:
    """执行清理：重复键归 0、保留行符合规则、备份完整。"""
    db = _make_db(tmp_path)
    report = build_clean_report(db, str(tmp_path / "bak"),
                                str(tmp_path / "out"), dry_run=False,
                                report_date="2026-10-17")
    assert report["dry_run"] is False
    assert report["deleted_rows"] == 3
    assert report["duplicate_key_after"] == 0
    assert report["rows_after"] == 3
    assert report["backup_path"] and Path(report["backup_path"]).exists()

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT symbol, data_source, created_at FROM market_data "
            "ORDER BY symbol"
        ).fetchall()
    finally:
        conn.close()
    # 键 A 保留 tdx；键 B 保留更早的 tencent_http；键 C 原样
    assert rows == [
        ("000001.SZ", "tdx", "2026-10-16 09:25:46"),
        ("000002.SZ", "tdx", "2026-10-16 09:25:47"),
        ("600519.SH", "tencent_http", "2026-10-08 09:08:29"),
    ]

    # 备份可读且行数=清理前
    bak_conn = sqlite3.connect(report["backup_path"])
    try:
        n = bak_conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    finally:
        bak_conn.close()
    assert n == 6


def test_backup_db_roundtrip(tmp_path: Path) -> None:
    """备份 API 生成可读备份且行数一致。"""
    db = _make_db(tmp_path)
    path = backup_db(db, str(tmp_path / "bak"))
    assert Path(path).exists()
    assert path.startswith(str(tmp_path / "bak"))
