# 文件名：scripts/故障演练/季频故障演练.py
"""
D-6 混沌工程与故障演练 薄入口（scripts 层，业务逻辑在 myquant）。
==================================================================
季度故障演练：复用 P0-8 FI-01~07 用例库，逐 FI 用例注入→计时→分类→记录，
产出复盘报告（JSON）落盘；仅做命令行编排，不含业务逻辑。

用法：
  python scripts/故障演练/季频故障演练.py                     # 全量 7 用例
  python scripts/故障演练/季频故障演练.py --select FI-01 FI-06 # 指定用例
  python scripts/故障演练/季频故障演练.py --out 报告.json       # 复盘报告落盘
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# 允许从任意工作目录直接运行（含 scripts/故障演练/ 目录，对齐 #254 验收_D包自治修复）；
# 业务逻辑全部在 myquant（薄入口）
_REPO_ROOT = Path(__file__).resolve().parents[2]  # scripts/故障演练/ → 项目根
while not (_REPO_ROOT / "myquant").is_dir() and _REPO_ROOT != _REPO_ROOT.parent:
    _REPO_ROOT = _REPO_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from myquant.acceptance.chaos_drill import ChaosDrillRunner, DEFAULT_DRILL_HEADER  # noqa: E402

# 默认输出为项目根绝对路径（修复：原相对路径从 scripts/ 子目录启动时
# 会把报告写到 scripts/docs/监控档案/，与 docs/监控档案/ 正式落档分叉）
DEFAULT_REPORT_PATH = str(_REPO_ROOT / "docs" / "监控档案" / "混沌演练" / "季度故障演练报告.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="季频故障演练",
        description=DEFAULT_DRILL_HEADER,
    )
    parser.add_argument(
        "--select", nargs="+", default=None,
        help="指定要演练的 FI 用例（如 FI-01 FI-06）；缺省全量 FI-01~07",
    )
    parser.add_argument(
        "--out", default=DEFAULT_REPORT_PATH,
        help=f"复盘报告落盘路径（缺省 {DEFAULT_REPORT_PATH}）",
    )
    parser.add_argument(
        "--db", default=None,
        help="告警落库的临时数据库路径；缺省自动创建并在结束清理",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="仅打印可用 FI 用例清单（P0-8 用例库）后退出",
    )
    return parser


def list_cases() -> None:
    from myquant.monitoring.measurability import list_fi_cases
    print("P0-8 故障注入用例库（演练/验收同源，R-01）：")
    for row in list_fi_cases():
        print(f"  {row['case_id']}: {row['fault_desc']} | {row['kpi']}")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        list_cases()
        return 0

    runner = ChaosDrillRunner(store_db_path=args.db)
    try:
        runner.register_builtin_cases(select=args.select)
        if not runner.case_ids:
            print("无可用演练用例（select 过滤为空），退出。", file=sys.stderr)
            return 2
        report = runner.run(select=args.select)
        runner.write_report(args.out)
        summary = report["summary"]
        print(f"演练完成：共 {summary['cases_total']} 项，检出 {summary['cases_detected']} "
              f"项（检出率 {summary['detect_rate']:.1%}），TP={summary['tp']} "
              f"FP={summary['fp']} FN={summary['fn']}")
        print(f"复盘报告已落盘：{args.out}")
        return 0
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
