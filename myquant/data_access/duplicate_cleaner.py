# -*- coding: utf-8 -*-
"""
存量重复行清理服务（数据层施工 A-13）
======================================
按 (symbol, period, adjust, trade_date) 去重，保留规则（报告 v1.1 §5.2 A-13）：
  1. 原始源行（tdx / tencent_http）优先于 incremental_clean；
  2. 同一源内保留 created_at 最早的行；
  3. 边界（规则未覆盖）：键内同时含 tdx 与 tencent_http 两个不同原始源时，
     按 created_at 最早（先写入者）保留，与"同源保留最早"的时间优先精神一致；
  4. 最终兜底 rowid 最小（稳定可复现）。

备份：6GB 级 SQLite（WAL 模式）不可直接复制文件，必须用在线备份 API
（sqlite3.Connection.backup）合并 WAL 生成完整备份。

交付：一次性清理 + 清理报告 outputs/重复行清理报告_<YYYYMMDD>.json。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DUP_KEY_COLUMNS = ("symbol", "period", "adjust", "trade_date")
# 原始源（A-00 实测 data_source 分布：tdx / tencent_http / incremental_clean）
ORIGINAL_SOURCES = ("tdx", "tencent_http")

# 保留排序 SQL：原始源优先 → created_at 最早 → rowid 最小
_RETAIN_ORDER_SQL = (
    "CASE WHEN data_source IN ('tdx','tencent_http') THEN 0 ELSE 1 END, "
    "created_at ASC, rowid ASC"
)


def _open_ro(db_path: str) -> sqlite3.Connection:
    """只读打开 SQLite（file:...?mode=ro + PRAGMA query_only=ON）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _count_market_data(db_path: str) -> int:
    conn = _open_ro(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    finally:
        conn.close()


def collect_duplicate_stats(db_path: str) -> Dict[str, Any]:
    """统计重复键/重复行/待删行（dry-run 与执行前置检查共用，只读）。"""
    conn = _open_ro(db_path)
    try:
        dup_keys = conn.execute(
            "SELECT COUNT(*) FROM (SELECT symbol, period, adjust, trade_date "
            "FROM market_data GROUP BY symbol, period, adjust, trade_date "
            "HAVING COUNT(*)>1)"
        ).fetchone()[0]
        # 重复行总数 = 全部重复键内的行数之和（与 A-01 明细口径一致）
        dup_rows = conn.execute(
            "SELECT COALESCE(SUM(cnt),0) FROM (SELECT COUNT(*) cnt FROM "
            "market_data GROUP BY symbol, period, adjust, trade_date "
            "HAVING COUNT(*)>1)"
        ).fetchone()[0]
        # 待删行 = 重复行总数 - 重复键数（每键保留 1 行）
        to_delete = dup_rows - dup_keys
        return {
            "duplicate_key_total": dup_keys,
            "duplicate_row_total": dup_rows,
            "to_delete": to_delete,
            "rows_before": _count_market_data(db_path),
        }
    finally:
        conn.close()


def backup_db(db_path: str, backup_dir: str) -> str:
    """在线备份 API 生成完整备份（合并 WAL），并校验备份可读且行数一致。"""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = Path(backup_dir) / f"quant.db.bak_{stamp}"
    rows_before = _count_market_data(db_path)
    logger.info("开始备份 %s → %s（行数 %s）", db_path, target, rows_before)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            done = {"n": 0, "total": 0}

            def _progress(completed: int, total: int, remaining: int) -> None:
                done["n"], done["total"] = completed, total
                if completed % 200 == 0:
                    logger.info("备份进度 %s/%s（剩余 %s）",
                                completed, total, remaining)

            src.backup(dst, progress=_progress)
        finally:
            dst.close()
    finally:
        src.close()
    chk = _count_market_data(str(target))
    if chk != rows_before:
        raise RuntimeError(
            f"备份校验失败: 主库 {rows_before} 行 vs 备份 {chk} 行"
        )
    logger.info("备份完成并校验通过: %s（%s 行）", target, chk)
    return str(target)


def clean_duplicates(
    db_path: str,
    backup_dir: str,
    report_dir: str,
    dry_run: bool = True,
    report_date: Optional[str] = None,
) -> Dict[str, Any]:
    """执行 A-13 存量重复行清理（dry_run 只统计不删除）。"""
    if report_date is None:
        report_date = datetime.now().strftime("%Y-%m-%d")

    stats = collect_duplicate_stats(db_path)
    report: Dict[str, Any] = {
        "task": "A-13",
        "report_date": report_date,
        "key_columns": list(DUP_KEY_COLUMNS),
        "retain_rule": (
            "原始源行(tdx/tencent_http)>incremental_clean；同源保留最早 created_at；"
            "tdx 与 tencent_http 不同原始源时按 created_at 最早；兜底 rowid 最小"
        ),
        "dry_run": dry_run,
        "backup_path": None,
        **stats,
    }

    if dry_run:
        report["status"] = "dry_run_only"
        logger.info("dry-run: 重复键 %s / 重复行 %s / 待删 %s",
                    stats["duplicate_key_total"], stats["duplicate_row_total"],
                    stats["to_delete"])
        _write_report(report, report_dir, report_date)
        return report

    # 非 dry-run：先备份，再事务删除，再验证
    backup_path = backup_db(db_path, backup_dir)
    report["backup_path"] = backup_path

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "DELETE FROM market_data WHERE rowid IN ("
            "SELECT rowid FROM (SELECT rowid, ROW_NUMBER() OVER ("
            "PARTITION BY symbol, period, adjust, trade_date "
            f"ORDER BY {_RETAIN_ORDER_SQL}) rn FROM market_data) WHERE rn>1)"
        )
        deleted = cur.rowcount
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise
    finally:
        conn.close()

    after = collect_duplicate_stats(db_path)
    report["deleted_rows"] = deleted
    report["rows_after"] = _count_market_data(db_path)
    report["duplicate_key_after"] = after["duplicate_key_total"]
    report["status"] = "done"
    logger.info("清理完成: 删除 %s 行，剩余重复键 %s", deleted,
                after["duplicate_key_total"])
    _write_report(report, report_dir, report_date)
    return report


def _write_report(report: Dict[str, Any], report_dir: str,
                  report_date: str) -> str:
    os.makedirs(report_dir, exist_ok=True)
    stamp = report_date.replace("-", "")
    target = Path(report_dir) / f"重复行清理报告_{stamp}.json"
    tmp = Path(report_dir) / f"重复行清理报告_{stamp}.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, target)
    return str(target)


def build_clean_report(
    db_path: str, backup_dir: str, report_dir: str,
    dry_run: bool = True, report_date: Optional[str] = None,
) -> Dict[str, Any]:
    """一站式入口：统计 →（可选备份+删除）→ 报告落盘。"""
    return clean_duplicates(
        db_path=db_path,
        backup_dir=backup_dir,
        report_dir=report_dir,
        dry_run=dry_run,
        report_date=report_date,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import sys

    if len(sys.argv) < 2:
        print("用法: python duplicate_cleaner.py <db_path> [backup_dir] [report_dir] [--dry-run|--run]")
        sys.exit(2)
    db = sys.argv[1]
    bdir = sys.argv[2] if len(sys.argv) > 2 else "backups"
    rdir = sys.argv[3] if len(sys.argv) > 3 else "outputs"
    dry = "--run" not in sys.argv
    result = build_clean_report(db, bdir, rdir, dry_run=dry)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
