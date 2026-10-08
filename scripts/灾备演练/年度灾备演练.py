# 文件名：scripts/灾备演练/年度灾备演练.py
"""
D-8 元监控与灾备 薄入口（scripts 层，业务逻辑在 myquant）。
==================================================================
年度灾备演练：验证 SQLite 备份恢复、RabbitMQ 持久化恢复（持久化载体复刻
队列语义）、告警通道切换、FI-07 临界网络抖动不误判（R-08）；产出 JSON 报告。
仅做命令行编排，不含业务逻辑。

用法：
  python scripts/灾备演练/年度灾备演练.py                  # 全量 4 项演练
  python scripts/灾备演练/年度灾备演练.py --out 报告.json   # 指定报告落盘
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# 允许从任意工作目录直接运行（含 scripts/灾备演练/ 目录，对齐 #254 验收_D包自治修复）；
# 业务逻辑全部在 myquant（薄入口）
_REPO_ROOT = Path(__file__).resolve().parents[2]  # scripts/灾备演练/ → 项目根
while not (_REPO_ROOT / "myquant").is_dir() and _REPO_ROOT != _REPO_ROOT.parent:
    _REPO_ROOT = _REPO_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from myquant.acceptance.disaster_drill import DEFAULT_DRILL_HEADER, DisasterDrillRunner  # noqa: E402

# 默认输出为项目根绝对路径（修复：原相对路径从 scripts/ 子目录启动时
# 会把报告写到 scripts/docs/监控档案/，与 docs/监控档案/ 正式落档分叉）
DEFAULT_REPORT_PATH = str(_REPO_ROOT / "docs" / "监控档案" / "灾备演练" / "年度灾备演练报告.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="年度灾备演练",
        description=DEFAULT_DRILL_HEADER,
    )
    parser.add_argument(
        "--out", default=DEFAULT_REPORT_PATH,
        help=f"灾备演练报告落盘路径（缺省 {DEFAULT_REPORT_PATH}）",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    runner = DisasterDrillRunner()
    try:
        report = runner.run()
        runner.write_report(args.out)
        summary = report["summary"]
        print(f"灾备演练完成：{summary['items_total']} 项全通过"
              f"{'✅' if summary['all_passed'] else '❌'}")
        for item in report["items"]:
            mark = "PASS" if item["passed"] else "FAIL"
            print(f"  [{mark}] {item['item']}: {item['detail']}")
        print(f"演练报告已落盘：{args.out}")
        return 0 if summary["all_passed"] else 1
    finally:
        runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
