# 文件名：scripts/财务CSV更新.py
"""
财务CSV更新 - 中文入口脚本（薄入口）
==================================
自动补齐财务数据：定位 financial_data 缺失的标的（如新上市未进入历史 CSV 的
601091.SH）→ 拉取多期财务历史 → 与本地最新 CSV 合并去重 → 落盘 →（默认）
自动入库 financial_data。

业务逻辑全部位于 ``myquant.data_access.financial_sync_service.FinancialSyncService``，
本脚本仅做：参数解析 → 路径探测 → 调用服务 → 汇总输出。

运行方式:
    # 默认：增量同步（只拉缺失标的）+ 自动入库
    python scripts/财务CSV更新.py

    # 指定标的（逗号分隔）
    python scripts/财务CSV更新.py --symbols 601091.SH,301686.SZ

    # 全市场重拉 + 自动入库（约 25 分钟+，与 GUI 行为一致）
    python scripts/财务CSV更新.py --all

    # 只拉取不入库（后续手动执行 scripts/财务数据入库.py）
    python scripts/财务CSV更新.py --no-import

    # 限定报告期范围（防未来函数：只取该区间已披露数据）
    python scripts/财务CSV更新.py --start-date 20230101 --end-date 20260630
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        if current == current.parent:
            break
        current = current.parent
    return script_dir.parent


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="财务CSV更新",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 财务CSV增量同步与自动入库（缺口A）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/财务CSV更新.py                    # 增量同步（缺失标的）+ 自动入库
  python scripts/财务CSV更新.py --symbols 601091.SH
  python scripts/财务CSV更新.py --all              # 全市场重拉
  python scripts/财务CSV更新.py --no-import        # 只拉取不入库""",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="全市场重拉财务历史（缺省只拉 financial_data 缺失的清洗保留标的，增量优先）",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="指定待拉取标的，逗号分隔（如 601091.SH,301686.SZ）",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="起始报告期（YYYYMMDD 或 YYYY-MM-DD），None=不限",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="截止报告期（YYYYMMDD 或 YYYY-MM-DD），None=截止今天（防未来函数）",
    )
    parser.add_argument(
        "--no-import",
        action="store_true",
        help="拉取合并后不自动入库（后续手动执行 scripts/财务数据入库.py）",
    )
    return parser


def main() -> int:
    """主入口：解析参数 → 调用服务 → 汇总输出。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    project_root = find_project_root()
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    from myquant.data_access.financial_sync_service import FinancialSyncService

    args = setup_argument_parser().parse_args()

    symbols: Optional[list] = None
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 64)
    print("  财务CSV更新（缺口A：增量同步 + 自动入库）")
    print(f"  项目根: {project_root}")
    print(f"  模式: {'全市场重拉' if args.all else ('指定标的 ' + ','.join(symbols) if symbols else '增量（缺失标的）')}")
    print(f"  报告期: {args.start_date or '不限'} ~ {args.end_date or '截止今天'}")
    print(f"  自动入库: {'否' if args.no_import else '是'}")
    print("=" * 64)

    svc = FinancialSyncService(project_root)
    stats = svc.run_sync(
        symbols=symbols,
        all_market=args.all,
        start_date=args.start_date,
        end_date=args.end_date,
        auto_import=not args.no_import,
    )

    print("\n[同步结果]")
    print(f"  待拉标的: {len(stats.symbols)} 只")
    print(f"  拉取成功: {stats.fetched_symbols} 只, 失败/无数据: {stats.skipped_symbols} 只")
    print(f"  新增记录: {stats.new_records} 条")
    if stats.csv_path:
        print(f"  合并CSV : {stats.csv_path}")
    else:
        print("  合并CSV : 无新记录（未落盘）")
    if stats.import_stats:
        imp = stats.import_stats
        if "error" in imp:
            print(f"  自动入库 : 失败 - {imp['error']}")
        else:
            print(
                f"  自动入库 : 总行={imp['total_rows']} 成功={imp['success_rows']} "
                f"跳过={imp['skipped_rows']} 标的={imp['symbol_count']} "
                f"区间={imp['min_report_date']}~{imp['max_report_date']}"
            )
    else:
        print("  自动入库 : 跳过（--no-import 或无新CSV）")
    if stats.missing_after:
        print(f"  仍缺财务 : {len(stats.missing_after)} 只 - {stats.missing_after[:10]}")

    print("\n[下一步建议]")
    if stats.missing_after:
        print("  以上标的数据源无财务记录（可能未披露），待下期数据源更新后重跑本脚本。")
    if stats.csv_path and not args.no_import:
        print("  如需 B 类派生量（换手率/流通股本/市盈动/每股收益）落库：")
        print("    执行: python scripts/数据全量清洗.py --config config/data_clean_run.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
