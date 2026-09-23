# 文件名：scripts/仿真准入评估.py
"""薄入口：仿真模拟盘绩效准入评估（任务 T10，对照表5 阈值）。

只做参数解析 + 路径探测 + 调用 myquant.live.acceptance（业务逻辑全在 myquant 内）。

用法：
  python scripts/仿真准入评估.py --metrics outputs/live/仿真绩效_20260923.json
  python scripts/仿真准入评估.py --metrics ... --backtest results/xxx/backtest_metrics.json
  python scripts/仿真准入评估.py --metrics ... --thresholds config/acceptance_thresholds.json --output outputs/仿真准入评估_20260923.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def find_project_root() -> Path:
    """向上查找项目根（含 pyproject.toml 的目录）。"""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    return current.parent


def _load_json(path: str, project_root: Path) -> Dict[str, Any]:
    """加载 JSON 对象文件（相对路径以项目根为基准）。"""
    p = Path(path)
    if not p.is_absolute():
        p = project_root / p
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"文件不是 JSON 对象: {p}")
    return data


def _save_report(report: Any, output: str, project_root: Path) -> str:
    """保存评估报告（.csv 用表格，其余用文本）。"""
    out = Path(output)
    if not out.is_absolute():
        out = project_root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["key", "name", "status", "actual", "threshold", "detail"])
        for item in report.items:
            writer.writerow(
                [item.key, item.name, item.status.value, item.actual,
                 item.threshold, item.detail]
            )
        writer.writerow(
            ["overall", "综合结论", "pass" if report.overall else "fail",
             f"通过={report.passed} 未达标={report.failed} 待补充={report.pending}", "", ""]
        )
        out.write_text(buf.getvalue(), encoding="utf-8-sig")
    else:
        out.write_text(report.to_text(), encoding="utf-8")
    return str(out)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="仿真准入评估",
        description="仿真模拟盘绩效准入评估（任务 T10，表5 阈值判定）",
    )
    parser.add_argument("--metrics", required=True, help="仿真运行指标 JSON 路径")
    parser.add_argument("--thresholds", default=None, help="阈值覆盖 JSON（可选）")
    parser.add_argument("--backtest", default=None, help="回测基线指标 JSON（可选）")
    parser.add_argument("--run-mode", default="paper", help="运行模式标识（paper/sim）")
    parser.add_argument("--output", default=None, help="评估报告输出路径（.txt/.csv）")
    args = parser.parse_args()

    project_root = find_project_root()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from myquant.live.acceptance import SimulationAcceptanceEvaluator

    try:
        metrics = _load_json(args.metrics, project_root)
        thresholds = _load_json(args.thresholds, project_root) if args.thresholds else None
        backtest = _load_json(args.backtest, project_root) if args.backtest else None
    except Exception as e:
        print(f"[错误] 输入加载失败: {e}")
        return 2

    evaluator = SimulationAcceptanceEvaluator(
        metrics, thresholds=thresholds, backtest=backtest, run_mode=args.run_mode
    )
    report = evaluator.evaluate()
    print(report.to_text())

    if args.output:
        try:
            path = _save_report(report, args.output, project_root)
            print(f"\n[报告] 已保存: {path}")
        except Exception as e:
            print(f"[警告] 报告保存失败: {e}")
            return 2
    return 0 if report.overall else 2


if __name__ == "__main__":
    sys.exit(main())
