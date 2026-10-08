# 文件名：scripts/监控周复盘.py
"""
监控周复盘.py - §7 每周告警复盘薄入口（R-01 自动统计）

用法
====
python scripts/监控周复盘.py                    # 本周窗口，输出到 项目根/outputs/weekly_reports/
python scripts/监控周复盘.py --out D:/tmp/review # 指定输出目录
python scripts/监控周复盘.py --top-n 5           # Top 告警取 5 条
python scripts/监控周复盘.py --week-end 1791273600  # 指定周窗口右端（epoch 秒）
python scripts/监控周复盘.py --no-capacity       # 不统计容量趋势

纪律
====
- 薄入口：仅做参数解析 + 路径探测 + 调用 myquant/monitoring/weekly_review.py
  （WeeklyReviewReport），业务逻辑零写入脚本。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def find_project_root() -> Path:
    """自脚本位置向上定位项目根（含 scripts/ 或 scripts/故障演练 两级深度）。

    Returns:
        Path: 项目根目录（含 config/settings.yaml 或 myquant/ 包的目录）。

    Raises:
        RuntimeError: 无法定位时抛出。
    """
    here = Path(__file__).resolve().parent
    for cand in (here.parent, here.parent.parent):
        if (cand / "myquant").is_dir() and (cand / "config").is_dir():
            return cand
    raise RuntimeError("无法定位项目根目录（未找到 myquant/ + config/）")


def main() -> int:
    """周复盘薄入口：解析参数 → 装配 store → 生成报告 → 落盘 → 控制台摘要。"""
    parser = argparse.ArgumentParser(
        description="监控周复盘（§7 · R-01）：TP/FP/FN + 准确率/误报率/检出率 + Top 告警 + 容量趋势"
    )
    parser.add_argument("--db", default=None, help="quant.db 路径（默认 项目根/database/quant.db）")
    parser.add_argument("--out", default=None, help="输出目录（默认 项目根/outputs/weekly_reports/）")
    parser.add_argument("--week-end", type=float, default=None, help="周窗口右端 epoch 秒（默认当前时间）")
    parser.add_argument("--top-n", type=int, default=10, help="Top 告警条数（默认 10）")
    parser.add_argument(
        "--no-capacity",
        action="store_true",
        help="不统计容量趋势（默认统计 C-8 容量指标）",
    )
    args = parser.parse_args()

    root = find_project_root()
    sys.path.insert(0, str(root))

    from myquant.core.alert_store import AlertStore
    from myquant.monitoring.weekly_review import WeeklyReviewReport

    db_path = Path(args.db) if args.db else root / "database" / "quant.db"
    if not db_path.exists():
        print(f"[监控周复盘] 数据库不存在: {db_path}")
        return 2

    # 业务逻辑全部在 myquant（薄入口只做装配与调用）
    alert_store = AlertStore(db_path=str(db_path))
    metrics_store = None
    if not args.no_capacity:
        try:
            from myquant.core.metrics_history import MetricsHistoryStore

            metrics_store = MetricsHistoryStore(db_path=str(db_path))
        except Exception as exc:  # noqa: BLE001
            print(f"[监控周复盘] 指标库初始化失败（容量趋势跳过）: {exc}")
            metrics_store = None

    report = WeeklyReviewReport(alert_store, metrics_store=metrics_store).generate(
        week_end=args.week_end,
        top_n=args.top_n,
    )
    out_dir = Path(args.out) if args.out else None
    md_path = WeeklyReviewReport(alert_store, metrics_store=metrics_store).save(
        report, output_dir=out_dir
    )

    print(
        f"[监控周复盘] 窗口 {report['week_start']}~{report['week_end']}: "
        f"TP={report['tp']} FP={report['fp']} FN={report['fn']} "
        f"准确率={report['accuracy']} 误报率={report['false_positive_rate']} "
        f"达标={report['passed']} Top告警={len(report['top_alerts'])} 条"
    )
    print(f"[监控周复盘] 报告已落盘: {md_path}")

    # 控制台摘要：Top 告警 Top3（便于快速巡检）
    for item in report["top_alerts"][:3]:
        print(
            f"  Top{item['rank']}: [{item['category']}] x{item['count']} "
            f"{item['message'][:60]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
