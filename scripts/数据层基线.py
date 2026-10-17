# -*- coding: utf-8 -*-
# 文件名：scripts/数据层基线.py
"""
数据层状态指纹基线入口（薄入口）
==============================
对照 docs/project_layout_gear2.md：scripts/ 只做入口，业务逻辑全部在
myquant/data_access/data_layer_baseline.py（build_baseline / save_baseline_json）。

产出：outputs/数据层基线_<YYYYMMDD>.json —— 7 组只读指纹，施工前后可重跑复现，
作为《数据层结构优化可行性施工方案报告_v1.1》A-00 的验收产物；
加 --dup-listing 额外产出 outputs/重复键定位清单_<YYYYMMDD>.json —— 重复键
全量明细（A-01 验收产物，供 A-13 清理决策使用）。

用法:
  python scripts/数据层基线.py                                   # 默认库+默认 processed+默认日期
  python scripts/数据层基线.py --db database/quant.db --processed datasets/processed
  python scripts/数据层基线.py --out-dir outputs --date 2026-10-17
  python scripts/数据层基线.py --dup-listing                     # 额外输出重复键全量明细
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from myquant.data_access.data_layer_baseline import (  # noqa: E402
    build_baseline,
    build_duplicate_listing,
    save_baseline_json,
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数（薄入口只做参数装配，不写业务逻辑）。"""
    parser = argparse.ArgumentParser(
        description="Pythonwdhmm 数据层状态指纹基线（只读，A-00 验收产物）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=str,
        default=str(PROJECT_ROOT / "database" / "quant.db"),
        help="数据库路径（只读打开，默认 database/quant.db）",
    )
    parser.add_argument(
        "--processed",
        type=str,
        default=str(PROJECT_ROOT / "datasets" / "processed"),
        help="processed parquet 目录（只读扫描，默认 datasets/processed）",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=str(PROJECT_ROOT / "outputs"),
        help="基线 JSON 输出目录（默认 outputs/）",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="基线日期 YYYY-MM-DD（缺省取今天，用于生成_<YYYYMMDD>.json 文件名）",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="额外打印基线 JSON 全文到 stdout（便于人工核对）",
    )
    parser.add_argument(
        "--dup-listing",
        action="store_true",
        help="额外输出重复键全量明细清单（A-01，供 A-13 清理决策使用）",
    )
    return parser.parse_args()


def main() -> int:
    """主入口：构建基线 → 原子落盘 → 打印摘要。"""
    args = parse_args()
    baseline = build_baseline(
        db_path=args.db,
        processed_dir=args.processed,
        baseline_date=args.date,
    )
    path = save_baseline_json(baseline, out_dir=args.out_dir, baseline_date=args.date)

    fp = baseline["fingerprints"]
    print(f"✅ 数据层基线已生成: {path}")
    print(f"   baseline_date      = {baseline['baseline_date']}")
    print(f"   market_data        = {fp['market_data'].get('rows')} 行 / "
          f"{fp['market_data'].get('symbols')} 标的 / pct_chg 非空率 "
          f"{fp['market_data'].get('pct_chg_rate')}")
    print(f"   duplicate_keys     = {fp['duplicate_keys'].get('duplicate_key_total')} 个重复键")
    print(f"   financial_data     = {fp['financial_data'].get('rows')} 行 / "
          f"ann_date 非空 {fp['financial_data'].get('ann_date_notnull')} / "
          f"为空 {fp['financial_data'].get('ann_date_null')}")
    print(f"   processed          = {fp['processed'].get('file_count')} 个 parquet")
    print(f"   aux_tables         = {fp['aux_tables']}")
    print(f"   alembic_version    = {fp['schema_version'].get('alembic_version')}")

    if args.print:
        print(json.dumps(baseline, ensure_ascii=False, indent=2, default=str))

    if args.dup_listing:
        dup_path, listing = build_duplicate_listing(
            db_path=args.db,
            out_dir=args.out_dir,
            baseline_date=args.date,
        )
        print(f"✅ 重复键定位清单已生成: {dup_path}（"
              f"{listing.get('duplicate_key_total')} 个重复键 / "
              f"{listing.get('duplicate_row_total')} 行重复明细）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
