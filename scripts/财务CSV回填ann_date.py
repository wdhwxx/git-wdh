# 文件名：scripts/财务CSV回填ann_date.py
"""A-04 薄入口：存量财务 CSV 补 ann_date 公告日列（默认 dry-run 统计）。

用法：
    python scripts/财务CSV回填ann_date.py                  # dry-run 统计待回填
    python scripts/财务CSV回填ann_date.py --run            # 实际回写
    python scripts/财务CSV回填ann_date.py --csv-dir PATH   # 指定目录
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myquant.data_access.financial_sync_service import backfill_ann_date_column  # noqa: E402

_DEFAULT_DIR = Path(__file__).resolve().parents[1] / "datasets" / "raw" / "financial"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="存量财务 CSV 补 ann_date 公告日列（A-04）",
    )
    ap.add_argument("--csv-dir", type=Path, default=_DEFAULT_DIR,
                    help="财务 CSV 目录（默认 datasets/raw/financial）")
    ap.add_argument("--run", action="store_true",
                    help="实际回写；缺省 dry-run 仅统计")
    args = ap.parse_args()

    stats = backfill_ann_date_column(args.csv_dir, dry_run=not args.run)
    mode = "已回填" if args.run else "[dry-run] 待回填"
    print(
        f"A-04 存量回填（{mode}）: "
        f"扫描 {stats['files_scanned']} 个文件, "
        f"回填 {stats['files_backfilled']} 个, "
        f"行数 {stats['rows_updated']}"
    )
    if not args.run and stats["files_backfilled"]:
        print("确认无误后加 --run 执行实际回写。")


if __name__ == "__main__":
    main()
