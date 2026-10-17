# -*- coding: utf-8 -*-
# 文件名：scripts/数据一致性校验.py
"""
数据层一致性校验入口（薄入口）
==============================
对照 docs/project_layout_gear2.md：scripts/ 只做入口，业务逻辑全部在
myquant/data_access/data_layer_baseline.py（build_baseline / compare_baseline）。

作用：现算数据层 7 组状态指纹，与历史基线 JSON（outputs/数据层基线_<YYYYMMDD>.json）
比对，验证施工前后数据层一致性；--rebuild 先重建基线再比对（基线一键复现，
《数据层结构优化可行性施工方案报告_v1.1》A-11 验收）。

用法:
  python scripts/数据一致性校验.py                                          # 默认库+默认 processed，自动找最新基线比对
  python scripts/数据一致性校验.py --db database/quant.db --processed datasets/processed
  python scripts/数据一致性校验.py --baseline outputs/数据层基线_20261017.json
  python scripts/数据一致性校验.py --rebuild --date 2026-10-17             # 重建基线后再比对（一键复现）

退出码: 0=与基线一致；1=存在差异；2=参数/IO 错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from myquant.data_access.data_layer_baseline import (  # noqa: E402
    build_baseline,
    compare_baseline,
    save_baseline_json,
)


def _find_latest_baseline(out_dir: str) -> Path:
    """在输出目录中查找最新的数据层基线 JSON。

    Args:
        out_dir: 输出目录。

    Returns:
        最新基线文件路径；不存在时抛出 FileNotFoundError。
    """
    matches = sorted(Path(out_dir).glob("数据层基线_*.json"))
    if not matches:
        raise FileNotFoundError(f"{out_dir} 下未找到数据层基线 JSON（可用 --rebuild 重建）")
    return matches[-1]


def parse_args() -> argparse.Namespace:
    """解析命令行参数（薄入口只做参数装配，不写业务逻辑）。"""
    parser = argparse.ArgumentParser(
        description="Pythonwdhmm 数据层一致性校验（现算指纹 vs 基线比对，A-11 验收产物）",
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
        "--baseline",
        type=str,
        default=None,
        help="基线 JSON 路径（缺省自动找 outputs/ 下最新基线）",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=str(PROJECT_ROOT / "outputs"),
        help="基线 JSON 输出目录（默认 outputs/；--rebuild 时写新基线）",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="基线日期 YYYY-MM-DD（--rebuild 时用于生成_<YYYYMMDD>.json 文件名）",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="先重建基线（save_baseline_json 落盘新快照）再比对——基线一键复现",
    )
    return parser.parse_args()


def main() -> int:
    """主入口：现算指纹 → 加载基线 → 比对 → 汇总输出。"""
    args = parse_args()

    # 1. 现算当前指纹（只读）
    current = build_baseline(
        db_path=args.db,
        processed_dir=args.processed,
        baseline_date=args.date,
    )

    # 2. 重建基线（可选，一键复现）
    baseline_path: Path
    if args.rebuild:
        saved = save_baseline_json(
            current, out_dir=args.out_dir, baseline_date=args.date
        )
        baseline_path = Path(saved)
        print(f"✅ 基线已重建: {baseline_path}")
    else:
        if args.baseline:
            baseline_path = Path(args.baseline)
        else:
            baseline_path = _find_latest_baseline(args.out_dir)
        if not baseline_path.is_file():
            print(f"[错误] 基线文件不存在: {baseline_path}")
            return 2

    # 3. 加载基线并比对
    try:
        with open(baseline_path, encoding="utf-8") as f:
            baseline = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[错误] 基线加载失败 {baseline_path}: {exc}")
        return 2

    diffs: List[str] = compare_baseline(current, baseline)
    fp = current["fingerprints"]

    print("=" * 64)
    print("  数据层一致性校验")
    print(f"  基线: {baseline_path}")
    print(f"  现算 baseline_date: {current.get('baseline_date')}")
    print(f"  market_data        = {fp['market_data'].get('rows')} 行 / "
          f"{fp['market_data'].get('symbols')} 标的")
    print(f"  duplicate_keys     = {fp['duplicate_keys'].get('duplicate_key_total')} 个重复键")
    print(f"  financial_data     = {fp['financial_data'].get('rows')} 行 / "
          f"ann_date 非空 {fp['financial_data'].get('ann_date_notnull')}")
    print(f"  processed          = {fp['processed'].get('file_count')} 个 parquet")
    print(f"  alembic_version    = {fp['schema_version'].get('alembic_version')}")
    print("=" * 64)

    if not diffs:
        print("✅ 数据层与基线一致（7 组指纹全部匹配）")
        return 0

    print(f"⚠️  发现 {len(diffs)} 处差异：")
    for d in diffs:
        print(f"  - {d}")
    print("（预期差异=施工变更项；请核对后决定是否 --rebuild 更新基线）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
