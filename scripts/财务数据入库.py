# 文件名：scripts/财务数据入库.py
"""
财务数据入库 - 中文入口脚本（薄入口）
====================================
将财务 CSV 原始数据导入 SQLite ``financial_data`` 表，并批量计算技术/基本面因子
写入 ``factor_data`` 表，最后生成全中文表头的 HTML + CSV 入库报告。

业务逻辑全部位于 ``myquant.data_access.financial_service.FinancialImportService``，
本脚本仅做：参数解析 → 路径拼装 → 调用服务 → 汇总输出。

运行方式:
    # 默认：自动定位最新财务CSV，导入 + 计算最近1日因子 + 生成报告
    python scripts/财务数据入库.py

    # 仅导入财务CSV
    python scripts/财务数据入库.py --mode import

    # 仅计算因子（回看最近5个交易日）
    python scripts/财务数据入库.py --mode factors --lookback-days 5

    # 指定CSV文件
    python scripts/财务数据入库.py --csv datasets/raw/financial/financial_history_20260912.csv
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


def _new_factor_engine_factory(project_root: Path):
    """构造因子引擎工厂（G1 EX-08A：引擎由装配层注入，scripts 依赖 L2 合法方向）。

    与 myquant/data_access/financial_service.py 原延迟导入参数保持一致
    （cache_dir/use_cache/auto_fingerprint）。
    """
    from myquant.indicators.factor_engine import FactorEngine

    def _factory():
        return FactorEngine(
            cache_dir=str(project_root / "cache" / "factor"),
            use_cache=True,
            auto_fingerprint=True,
        )
    return _factory


def locate_latest_financial_csv(project_root: Path) -> Optional[Path]:
    """在 datasets/raw/financial/ 下定位最新的 financial_history_*.csv。"""
    fin_dir = project_root / "datasets" / "raw" / "financial"
    if not fin_dir.exists():
        return None
    candidates = sorted(fin_dir.glob("financial_history_*.csv"))
    if candidates:
        return candidates[-1]
    candidates = sorted(fin_dir.glob("financial_*.csv"))
    return candidates[-1] if candidates else None


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="财务数据入库",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 财务数据入库与因子计算",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/财务数据入库.py
  python scripts/财务数据入库.py --mode import
  python scripts/财务数据入库.py --mode factors --lookback-days 5""",
    )
    parser.add_argument(
        "--mode",
        choices=["import", "factors", "all"],
        default="all",
        help="执行模式：import=仅导入财务CSV, factors=仅计算因子, all=两者都做（默认）",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="财务CSV路径；缺省自动定位 datasets/raw/financial/ 下最新 financial_history_*.csv",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=1,
        help="因子计算写入最近 N 个交易日截面（默认 1，避免长表爆炸）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="报告输出目录；缺省 outputs/财务数据入库报告/",
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

    from myquant.data_access.financial_service import FinancialImportService

    args = setup_argument_parser().parse_args()

    # 定位 CSV
    csv_path: Optional[Path] = None
    if args.mode in ("import", "all"):
        if args.csv:
            csv_path = project_root / args.csv
        else:
            csv_path = locate_latest_financial_csv(project_root)
        if csv_path is None or not csv_path.exists():
            print(f"[错误] 未找到财务CSV文件: {args.csv or '（自动定位失败）'}")
            return 2

    output_dir = Path(args.output_dir) if args.output_dir else None

    print("=" * 64)
    print("  财务数据入库与因子计算")
    print(f"  项目根: {project_root}")
    print(f"  执行模式: {args.mode}")
    if csv_path:
        print(f"  财务CSV: {csv_path}")
    print(f"  因子回看天数: {args.lookback_days}")
    print("=" * 64)

    svc = FinancialImportService(
        project_root,
        factor_engine_factory=_new_factor_engine_factory(project_root),
    )
    try:
        import_stats = None
        factor_stats = None

        if args.mode in ("import", "all") and csv_path is not None:
            print("\n[阶段1] 财务CSV导入数据库 ...")
            import_stats = svc.import_financial_csv(csv_path)
            print(
                f"  完成: 总行={import_stats.total_rows} 成功={import_stats.success_rows} "
                f"跳过={import_stats.skipped_rows} 标的={import_stats.symbol_count} "
                f"区间={import_stats.min_report_date}~{import_stats.max_report_date}"
            )

        if args.mode in ("factors", "all"):
            print("\n[阶段2] 因子批量计算 ...")
            factor_stats = svc.compute_factors(lookback_days=args.lookback_days)
            print(
                f"  完成: 标的={factor_stats.symbol_count} 因子值={factor_stats.total_factor_values} "
                f"交易日={factor_stats.trade_days} 最新={factor_stats.latest_trade_date} "
                f"失败={len(factor_stats.failed_symbols)}"
            )

        # 生成报告（仅在有任一阶段结果时）
        if import_stats is not None or factor_stats is not None:
            print("\n[阶段3] 生成全中文入库报告 ...")
            from myquant.data_access.financial_service import FactorStats, ImportStats
            imp = import_stats or ImportStats()
            fac = factor_stats or FactorStats()
            csv_out, html_out = svc.generate_report(imp, fac, output_dir)
            print(f"  CSV 报告: {csv_out}")
            print(f"  HTML报告: {html_out}")

        print("\n" + "=" * 64)
        print("  全部完成")
        print("=" * 64)
        return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"\n[错误] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        logging.exception("财务数据入库执行异常")
        print(f"\n[异常] {exc}")
        return 1
    finally:
        svc.close()


if __name__ == "__main__":
    sys.exit(main())
