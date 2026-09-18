# 文件名：scripts/候选池交叉验证.py
"""
候选池交叉验证 - 中文入口脚本（薄入口）
========================================
对两个策略（rsrs / ma_cross）的批量回测缓存做双强交叉验证：
各自硬过滤（夏普 / 交易数 / 回撤）→ 取交集 → 按综合夏普降序标记 Top N →
导出 CSV + HTML + PDF 一套文档到 results/batch_backtest/双强候选池/双强候选池_<时间戳>.*。

业务逻辑位于 myquant.backtest.candidate_pool（真实模块），本脚本只做：
  参数解析 → 路径拼装 → 调用 build_candidate_pool → 汇总输出。

运行方式:
    # 默认：rsrs × ma_cross，夏普≥0.5、交易≥10、回撤≤10%、Top30
    python scripts/候选池交叉验证.py

    # 自定义阈值 / Top 数量
    python scripts/候选池交叉验证.py --min-sharpe 1.0 --min-trades 15 --top-n 20

    # 自定义缓存目录与输出
    python scripts/候选池交叉验证.py --rsrs-cache results/batch_backtest/rsrs \
        --ma-cache results/batch_backtest/ma_cross --out results/batch_backtest/双强候选池/自定义名.csv
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        if current == current.parent:
            break
    return script_dir.parent


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="候选池交叉验证",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 双策略交叉验证候选池",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/候选池交叉验证.py
  python scripts/候选池交叉验证.py --min-sharpe 1.0 --top-n 20
  python scripts/候选池交叉验证.py --out results/batch_backtest/双强候选池/自定义名.csv""",
    )
    parser.add_argument("--rsrs-cache", default="results/batch_backtest/rsrs",
                        help="rsrs 策略缓存目录（含 results.json）")
    parser.add_argument("--ma-cache", default="results/batch_backtest/ma_cross",
                        help="ma_cross 策略缓存目录（含 results.json）")
    parser.add_argument("--min-sharpe", type=float, default=0.5,
                        help="单策略夏普下限（默认 0.5）")
    parser.add_argument("--min-trades", type=int, default=10,
                        help="单策略交易数下限（默认 10）")
    parser.add_argument("--max-drawdown", type=float, default=0.10,
                        help="单策略最大回撤上限，0.10=10%%（默认 0.10）")
    parser.add_argument("--top-n", type=int, default=30,
                        help="综合夏普降序标记数量（默认 30）")
    parser.add_argument("--out", default=None,
                        help="CSV 输出路径；缺省自动生成 results/batch_backtest/双强候选池/双强候选池_<时间戳>.csv")
    return parser


def main() -> int:
    """主入口：解析参数 → 调用候选池构建 → 汇总输出。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    project_root = find_project_root()
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    from myquant.backtest.candidate_pool import build_candidate_pool

    args = setup_argument_parser().parse_args()
    rsrs_dir = project_root / args.rsrs_cache
    ma_dir = project_root / args.ma_cache
    out_path: Optional[Path] = None
    if args.out:
        out_path = project_root / args.out
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 候选池产物统一收进 results/batch_backtest/双强候选池/ 子文件夹（CSV+HTML+PDF 一套）
        out_path = (
            project_root / "results" / "batch_backtest" / "双强候选池"
            / f"双强候选池_{ts}.csv"
        )

    print("=" * 64)
    print("  双策略交叉验证候选池")
    print(f"  项目根: {project_root}")
    print(f"  rsrs 缓存: {rsrs_dir}")
    print(f"  ma_cross 缓存: {ma_dir}")
    print(f"  过滤: 夏普≥{args.min_sharpe} | 交易≥{args.min_trades} | 回撤≤{args.max_drawdown:.0%}")
    print(f"  Top N: {args.top_n}")
    print("=" * 64)

    try:
        pool = build_candidate_pool(
            rsrs_dir=rsrs_dir,
            ma_cross_dir=ma_dir,
            min_sharpe=args.min_sharpe,
            min_trades=args.min_trades,
            max_drawdown=args.max_drawdown,
            top_n=args.top_n,
            out_path=out_path,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"[错误] {exc}")
        return 2

    # 候选池报告（HTML + PDF，与 CSV 同目录同前缀一套；失败仅告警，不阻断）
    report_files: List[str] = []
    try:
        from myquant.backtest.candidate_report import (
            CandidatePoolReportGenerator,
            CandidateReportMeta,
        )
        report_meta = CandidateReportMeta(
            strategy_pair="rsrs × ma_cross",
            min_sharpe=args.min_sharpe,
            min_trades=args.min_trades,
            max_drawdown=args.max_drawdown,
            top_n=args.top_n,
        )
        report_paths = CandidatePoolReportGenerator().generate_full_report(
            pool, report_meta, out_path,
        )
        report_files = list(report_paths.values())
    except Exception as exc:
        print(f"[警告] 候选池报告生成失败（仅保留 CSV）: {exc}")

    print(f"\n双强候选池: {len(pool)} 只（其中 Top{args.top_n} 标记 {int(pool['is_top30'].sum())} 只）")
    print(f"产物: {out_path}")
    if report_files:
        print("报告文档: " + ", ".join(report_files))
    print("\n综合夏普 Top 10:")
    top = pool[pool["is_top30"]].head(10)
    for _, row in top.iterrows():
        print(
            f"  {row['symbol']}: rsrs {row['rsrs_ret']:+.2%}/夏普{row['rsrs_sharpe']:.2f} | "
            f"ma_cross {row['ma_cross_ret']:+.2%}/夏普{row['ma_cross_sharpe']:.2f} | "
            f"综合夏普 {row['avg_sharpe']:.2f}"
        )
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
