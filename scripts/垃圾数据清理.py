# -*- coding: utf-8 -*-
# 文件名：scripts/垃圾数据清理.py
"""
项目垃圾数据清理工具（薄入口，P2-D6-001 整改：业务逻辑委托 myquant.utils.cleanup_service）
============================================================================================
扫描 Pythonwdhmm 项目数据存储目录，识别并清理测试残留、孤儿文件、旧格式冗余、
缓存和轮转日志。默认 dry-run（只报告不删除），加 --execute 才真正删除。

垃圾分类与共享服务（myquant.utils.cleanup_service）对齐：
  A. 明确测试残留  — 文件名含 TEST/TEST_EXEC/TICK_TEST/EMPTY_TEST/HOOK_TEST/
                     unknown/_verify/_入口/test_，以及孤儿 bad 文件
  B. 测试备份      — backups/backup_*_quant_before_clean.db（全量清洗测试产生）
  C. Python 缓存   — **/__pycache__/（删除后 Python 自动重建）
  D. 轮转日志      — logs/*.log.1
  E. 旧格式冗余    — datasets/processed/*/ 子目录（需 --include-legacy 才删）
  F. 回测产物输出  — outputs 下 report/equity/trades/summary/result/回测看板（需 --purge-results 才删）

P2-D6-001 整改说明（语义变更，显式声明）：
  - 原版本 F 类仅删 outputs/result_*.json 且保护报告族；现统一为共享服务 F 类规则——
    报告族与 result 同属回测产物输出，用户显式 --purge-results 时一并清理（与 GUI 清理面板同规则）。
  - 本工具不执行 outputs 归期（保留最近 N 期），归期请走 scripts/清理历史回测输出.py（--keep）。

保护清单（与共享服务一致，永不删除）：
  - database/quant.db（主数据库）
  - datasets/processed/*.parquet（回测主数据）
  - outputs/ 归期保留期内的真实回测报告
  - datasets/bad/ 中被 clean_run_meta 引用的真实脏数据归档
  - backups/ 用户原有备份（backup_20260829_083316）与代码备份（*.bak_*.py）
  - 所有 .gitkeep

用法：
  python scripts/垃圾数据清理.py                    # dry-run，只扫描报告
  python scripts/垃圾数据清理.py --execute          # 执行清理（A+B+C+D）
  python scripts/垃圾数据清理.py --execute --include-legacy   # 额外删旧格式 CSV（E）
  python scripts/垃圾数据清理.py --execute --purge-results    # 额外删 outputs 回测产物（F）
  python scripts/垃圾数据清理.py --execute --all              # 全部清理（A-F）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from myquant.utils.cleanup_service import (  # noqa: E402  # P2-D6-001 整改：共享服务单源
    CATEGORY_NAMES,
    CleanupOptions,
    execute_plan,
    format_plan_summary,
    run_cleanup_scan,
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Pythonwdhmm 项目垃圾数据清理（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--execute", action="store_true", help="真正执行删除（默认只扫描报告）")
    p.add_argument("--include-legacy", action="store_true", help="包含旧格式 processed CSV 子目录（E 类）")
    p.add_argument("--purge-results", action="store_true", help="包含 outputs 回测产物输出（F 类：report/equity/trades/summary/result/回测看板）")
    p.add_argument("--all", action="store_true", help="清理全部类别（A+B+C+D+E+F）")
    p.add_argument("--json", action="store_true", help="输出 JSON 格式报告")
    return p.parse_args()


def main():
    args = parse_args()
    options = CleanupOptions(
        include_legacy=args.include_legacy or args.all,
        clean_artifacts=args.purge_results or args.all,
        clean_outputs=False,  # 归期（保留最近 N 期）走 清理历史回测输出.py，本工具不做
        execute=args.execute,
    )
    try:
        plan = run_cleanup_scan(PROJECT_ROOT, options)
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 1

    if args.json:
        report = {
            "scan_time": plan.scanned_at.isoformat(timespec="seconds"),
            "execute": args.execute,
            "total_items": len(plan.junk.items),
            "total_size_mb": round(plan.delete_total_size() / 1024 / 1024, 2),
            "items": [
                {"path": str(i.path.relative_to(PROJECT_ROOT)),
                 "category": i.category,
                 "category_name": CATEGORY_NAMES.get(i.category, i.category),
                 "reason": i.reason,
                 "size_mb": round(i.size_mb, 4)}
                for i in plan.junk.items
            ],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("\n".join(format_plan_summary(plan)))

    if args.execute and plan.junk.items:
        print(f"\n开始删除 {len(plan.junk.items)} 项...")
        deleted, failed, freed = execute_plan(plan)
        print(f"\n清理完成: 成功 {deleted}, 失败 {failed}, 释放 {freed / 1024 / 1024:.1f} MB")
    elif args.execute:
        print("\n没有可清理的垃圾数据。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
