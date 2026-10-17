# -*- coding: utf-8 -*-
# 文件名：scripts/数据重复清理.py
"""
存量重复行清理入口（薄入口，数据层施工 A-13）
==============================================
对照 docs/project_layout_gear2.md：scripts/ 只做入口，业务逻辑全部在
myquant/data_access/duplicate_cleaner.py（build_clean_report）。

按 (symbol,period,adjust,trade_date) 去重，保留规则：
  原始源行（tdx/tencent_http）> incremental_clean；同源保留最早 created_at；
  tdx 与 tencent_http 不同原始源时按 created_at 最早；兜底 rowid 最小。

默认 dry-run（只统计不删除）；加 --run 才执行（先在线备份再事务删除）。

用法:
  python scripts/数据重复清理.py                                  # dry-run
  python scripts/数据重复清理.py --run                            # 备份+删除+报告
  python scripts/数据重复清理.py --db database/quant.db --backup-dir backups
  python scripts/数据重复清理.py --report-dir outputs --date 2026-10-17
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from myquant.data_access.duplicate_cleaner import build_clean_report  # noqa: E402


def parse_args() -> argparse.Namespace:
    """解析命令行参数（薄入口只做参数装配）。"""
    parser = argparse.ArgumentParser(
        description="Pythonwdhmm 存量重复行清理（A-13：默认 dry-run，--run 才执行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=str,
        default=str(PROJECT_ROOT / "database" / "quant.db"),
        help="数据库路径（默认 database/quant.db）",
    )
    parser.add_argument(
        "--backup-dir",
        type=str,
        default=str(PROJECT_ROOT / "backups"),
        help="备份目录（默认 backups/，gitignore 不入库）",
    )
    parser.add_argument(
        "--report-dir",
        type=str,
        default=str(PROJECT_ROOT / "outputs"),
        help="清理报告输出目录（默认 outputs/）",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="报告日期 YYYY-MM-DD（缺省取今天）",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="执行模式：先在线备份（合并 WAL）再事务删除；不加则仅 dry-run 统计",
    )
    return parser.parse_args()


def main() -> int:
    """主入口：统计 →（--run 时 备份+删除）→ 报告落盘。"""
    args = parse_args()
    result = build_clean_report(
        db_path=args.db,
        backup_dir=args.backup_dir,
        report_dir=args.report_dir,
        dry_run=not args.run,
        report_date=args.date,
    )
    mode = "DRY-RUN（未删除）" if result["dry_run"] else "已执行"
    print(f"[{mode}] 重复键 {result['duplicate_key_total']} / "
          f"重复行 {result['duplicate_row_total']} / 待删 {result['to_delete']}")
    if not result["dry_run"]:
        print(f"  已删除 {result.get('deleted_rows')} 行 → 剩余重复键 "
              f"{result.get('duplicate_key_after')} / 清理后行数 "
              f"{result.get('rows_after')}")
        print(f"  备份: {result.get('backup_path')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
