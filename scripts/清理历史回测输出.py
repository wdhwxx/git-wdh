# -*- coding: utf-8 -*-
# 文件名：scripts/清理历史回测输出.py
"""
清理 outputs 历史回测输出（薄入口，P2-D6-001 整改：业务逻辑委托 myquant.utils.cleanup_service）
================================================================================================
扫描 outputs/ 根目录下的回测产物文件，按回测期（文件名中的 YYYYMMDD_HHMMSS 时间戳）
归组，仅保留时间戳最近的一期（或 --keep 指定期数），删除其余期的全部产物。
默认 dry-run（只报告不删除），加 --execute 才真正删除。

回测产物识别与共享服务（myquant.utils.cleanup_service）对齐：
  - report_<YYYYMMDD_HHMMSS>.pdf / .html
  - equity_<YYYYMMDD_HHMMSS>.csv
  - trades_<YYYYMMDD_HHMMSS>.csv
  - summary_<YYYYMMDD_HHMMSS>.txt
  - result_<unix秒>.json   （run 结果，文件名数字≈回测完成时间，单独排序保留）

保护范围（与共享服务一致，永不删除）：
  - 所有子目录（daily_reports/、report_fixed_*/、backups/ 等）及 .gitkeep
  - 非上述命名模式的任意文件（人工产物、其他脚本输出）
  - result_*.json 中无法解析时间戳的文件（保守保留）

用法：
  python scripts/清理历史回测输出.py                    # dry-run，只报告
  python scripts/清理历史回测输出.py --execute          # 执行：只保留最近 1 期
  python scripts/清理历史回测输出.py --execute --keep 2 # 保留最近 2 期
  python scripts/清理历史回测输出.py --json             # 输出 JSON 报告

设计原则：
  - 薄入口工具：无业务逻辑，扫描/计划/执行/报告全部委托共享服务，符合项目蓝图脚本层定位
  - 本工具仅做 outputs 归期（clean_outputs），不扫项目垃圾 A-D 类（那走 垃圾数据清理.py）
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
    DEFAULT_KEEP,
    CleanupOptions,
    CleanupPlan,
    ScanResult,
    execute_plan,
    format_plan_summary,
    run_cleanup_scan,
)


def main():
    parser = argparse.ArgumentParser(
        prog="清理历史回测输出",
        description="Pythonwdhmm 鲲鹏量化交易系统 — outputs 历史回测清理（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/清理历史回测输出.py              # dry-run 预览\n"
            "  python scripts/清理历史回测输出.py --execute    # 只保留最近 1 期\n"
            "  python scripts/清理历史回测输出.py --execute --keep 2  # 保留最近 2 期\n"
        ),
    )
    parser.add_argument("--execute", action="store_true", help="真正执行删除（默认只预览）")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"保留的最近回测期数（默认 {DEFAULT_KEEP}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式报告")
    args = parser.parse_args()

    try:
        plan = run_cleanup_scan(
            PROJECT_ROOT,
            CleanupOptions(clean_outputs=True, keep=args.keep, execute=args.execute),
        )
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 1

    # 仅保留 outputs 归期部分：清空 junk，保证 execute_plan 只删 outputs 过期产物
    # （不连带项目垃圾 A-D 类——那是 垃圾数据清理.py 的职责）
    cleanup_plan = CleanupPlan(
        project_root=PROJECT_ROOT,
        junk=ScanResult(),
        outputs_keep=plan.outputs_keep,
        outputs_delete=plan.outputs_delete,
        scanned_at=plan.scanned_at,
        options=CleanupOptions(clean_outputs=True, keep=args.keep, execute=args.execute),
    )

    if args.json:
        report = {
            "scan_time": cleanup_plan.scanned_at.isoformat(timespec="seconds"),
            "execute": args.execute,
            "keep": args.keep,
            "keep_count": len(cleanup_plan.outputs_keep),
            "delete_count": len(cleanup_plan.outputs_delete),
            "delete_size_mb": round(
                sum(p.stat().st_size for p in cleanup_plan.outputs_delete) / 1024 / 1024, 2),
            "kept": [str(p.relative_to(PROJECT_ROOT)) for p in sorted(cleanup_plan.outputs_keep)],
            "deleted": [str(p.relative_to(PROJECT_ROOT)) for p in sorted(cleanup_plan.outputs_delete)],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("\n".join(format_plan_summary(cleanup_plan)))

    if args.execute and cleanup_plan.outputs_delete:
        print(f"\n开始删除 {len(cleanup_plan.outputs_delete)} 个文件...")
        deleted, failed, freed = execute_plan(cleanup_plan)
        print(f"\n清理完成: 成功 {deleted}, 失败 {failed}, 释放 {freed / 1024 / 1024:.1f} MB")
        return 0 if failed == 0 else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
