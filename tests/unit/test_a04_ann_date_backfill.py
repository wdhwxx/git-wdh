# 文件名：tests/unit/test_a04_ann_date_backfill.py
"""A-04 CSV 链路补 ann_date 单测。

覆盖：
  1. FINANCIAL_CSV_COLUMNS 已含 ann_date 且位于 report_type 之后（列序与入库映射一致）；
  2. backfill dry-run：缺列文件统计待回填、不写盘；
  3. backfill 实际回写：表头补列、原行列值不变、ann_date 空串；
  4. 已含 ann_date 列的文件跳过（不重写）；
  5. late-disclosure 用例：含 ann_date 的 CSV 经 import_financial_csv 入库后
     financial_data.ann_date 非空（临时库验证完整链路）。
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from myquant.data_access import financial_sync_service as fss
from myquant.data_access.financial_service import FinancialImportService
from myquant.database.schema import create_all_tables
from myquant.database.connection import get_connection

# ═══════════════════════════════════════════
# 1. 常量列序
# ═══════════════════════════════════════════


def test_csv_columns_contains_ann_date_after_report_type() -> None:
    """ann_date 在 FINANCIAL_CSV_COLUMNS 中且紧跟 report_type。"""
    assert "ann_date" in fss.FINANCIAL_CSV_COLUMNS
    assert fss.FINANCIAL_CSV_COLUMNS.index("ann_date") == (
        fss.FINANCIAL_CSV_COLUMNS.index("report_type") + 1
    )


# ═══════════════════════════════════════════
# 2. backfill：dry-run / 实际回写 / 跳过
# ═══════════════════════════════════════════

_OLD_HEADER = [
    "symbol", "market", "report_date", "report_type", "pe", "pb", "total_mv",
    "circ_mv", "eps", "bps", "revenue", "net_profit", "net_profit_parent",
    "roe", "gross_margin", "debt_ratio", "data_source",
]
_ROWS = [
    ["000001.SZ", "A股", "19901231", "财务摘要(历史)", "", "", "", "",
     "2.627", "", "", "70875000.0", "", "", "1.9279320633", "akshare_eastmoney_datacenter_history"],
    ["600519.SH", "A股", "20260630", "财务摘要(历史)", "30.5", "8.2", "1.2万亿",
     "1.1万亿", "55.0", "380.0", "1.2e11", "5.5e10", "5.5e10", "18.5", "90.0",
     "20.0", "akshare_eastmoney_datacenter_history"],
]


@pytest.fixture
def old_csv(tmp_path: Path) -> Path:
    p = tmp_path / "financial_history_20260930.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(_OLD_HEADER)
        w.writerows(_ROWS)
    return p


def _read_csv(p: Path):
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), [dict(row) for row in r]


def test_backfill_dry_run_does_not_write(old_csv: Path, tmp_path: Path) -> None:
    """dry-run：统计待回填 1 文件/2 行，文件不被修改。"""
    stats = fss.backfill_ann_date_column(tmp_path, dry_run=True)
    assert stats["files_scanned"] == 1
    assert stats["files_backfilled"] == 1
    assert stats["rows_updated"] == 2
    header, _ = _read_csv(old_csv)
    assert "ann_date" not in header  # 未写盘


def test_backfill_run_adds_column_keeps_values(old_csv: Path, tmp_path: Path) -> None:
    """实际回写：补 ann_date 空列，原值/行序不变，列序=新常量。"""
    stats = fss.backfill_ann_date_column(tmp_path, dry_run=False)
    assert stats["files_backfilled"] == 1
    header, rows = _read_csv(old_csv)
    assert header == fss.FINANCIAL_CSV_COLUMNS
    assert rows[0]["symbol"] == "000001.SZ"
    assert rows[0]["report_date"] == "19901231"
    assert rows[0]["ann_date"] == ""
    assert rows[1]["pe"] == "30.5"
    assert rows[1]["data_source"] == "akshare_eastmoney_datacenter_history"
    assert len(rows) == 2  # 行序与数量不变


def test_backfill_skips_csv_with_ann_date(old_csv: Path, tmp_path: Path) -> None:
    """已含 ann_date 列的文件跳过（不重写、统计 0 回填）。"""
    fss.backfill_ann_date_column(tmp_path, dry_run=False)
    stats = fss.backfill_ann_date_column(tmp_path, dry_run=False)
    assert stats["files_scanned"] == 1
    assert stats["files_backfilled"] == 0
    assert stats["rows_updated"] == 0


# ═══════════════════════════════════════════
# 3. late-disclosure：CSV ann_date 入库后非空
# ═══════════════════════════════════════════

def _touch_db(db_path: Path) -> None:
    """创建空 sqlite 文件并初始化全部表结构。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()
    conn = get_connection(db_path)
    create_all_tables(conn)
    conn.close()


def test_late_disclosure_ann_date_imported(tmp_path: Path) -> None:
    """含 ann_date 列（公告日晚于报告期）的 CSV 入库后 ann_date 非空。"""
    db_path = tmp_path / "fin.db"
    _touch_db(db_path)
    csv_path = tmp_path / "financial_history_late.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(fss.FINANCIAL_CSV_COLUMNS)
        w.writerow([
            "600519.SH", "A股", "20260630", "财务摘要(历史)", "2026-08-15",
            "30.5", "8.2", "1.2万亿", "1.1万亿", "55.0", "380.0",
            "1.2e11", "5.5e10", "5.5e10", "18.5", "90.0", "20.0",
            "akshare_eastmoney_datacenter_history",
        ])

    svc = FinancialImportService(project_root=tmp_path, db_path=db_path)
    stats = svc.import_financial_csv(csv_path)
    assert stats.success_rows == 1

    row = svc._conn.execute_one(
        "SELECT report_date, ann_date FROM financial_data WHERE symbol='600519.SH'"
    )
    assert row is not None
    assert row["report_date"] == "2026-06-30"
    assert row["ann_date"] == "2026-08-15"  # late-disclosure：公告日晚于报告期被保留


def test_late_disclosure_empty_ann_date_null(tmp_path: Path) -> None:
    """ann_date 空列入库 → ann_date 为 NULL（时点回退 report_date，向后兼容）。"""
    db_path = tmp_path / "fin2.db"
    _touch_db(db_path)
    csv_path = tmp_path / "financial_history_noann.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(fss.FINANCIAL_CSV_COLUMNS)
        w.writerow([
            "000001.SZ", "A股", "19901231", "财务摘要(历史)", "",
            "", "", "", "", "2.627", "", "", "70875000.0", "", "",
            "1.9279320633", "akshare_eastmoney_datacenter_history",
        ])

    svc = FinancialImportService(project_root=tmp_path, db_path=db_path)
    stats = svc.import_financial_csv(csv_path)
    assert stats.success_rows == 1
    row = svc._conn.execute_one(
        "SELECT ann_date FROM financial_data WHERE symbol='000001.SZ'"
    )
    assert row is not None
    assert row["ann_date"] is None
