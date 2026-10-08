# 文件名：scripts/验收_D包自治.py
"""
D-9 D 包验收 薄入口（scripts 层，业务逻辑在 myquant）。
==================================================================
D-1~D-8 验收清单逐项断言（D 包自治）：D-1 编排 / D-2 动作库 / D-3 阈值校准 /
D-4 风险再分配 / D-5 参数调整（各自专项单测全绿）+ D-6 混沌演练 / D-7 配置
版本化 / D-8 灾备演练（Runner 集成）；产出 JSON 验收报告。
仅做命令行编排，不含业务逻辑。

用法：
  python scripts/验收_D包自治.py                     # 全量 8 项验收
  python scripts/验收_D包自治.py --out 报告.json     # 指定报告落盘
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# 允许从任意工作目录直接运行（含 scripts/ 目录，对齐验收_B包保钱包）；
# 业务逻辑全部在 myquant（薄入口）
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from myquant.acceptance.d_acceptance import DEFAULT_REPORT_TITLE, DAcceptanceRunner  # noqa: E402

# 默认输出为项目根绝对路径（修复：原相对路径从 scripts/ 目录启动时
# 会把报告写到 scripts/docs/监控档案/，与 docs/监控档案/ 正式落档分叉）
DEFAULT_REPORT_PATH = str(_REPO_ROOT / "docs" / "监控档案" / "D包自治验收_20261007.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="验收_D包自治",
        description=DEFAULT_REPORT_TITLE,
    )
    parser.add_argument(
        "--out", default=DEFAULT_REPORT_PATH,
        help=f"验收报告落盘路径（缺省 {DEFAULT_REPORT_PATH}）",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    runner = DAcceptanceRunner()
    report = runner.run()
    runner.write_report(args.out)
    summary = report["summary"]
    print(f"D 包验收完成：{summary['items_passed']}/{summary['items_total']} 项通过"
          f"{'✅' if summary['all_passed'] else '❌'}")
    for item in report["items"]:
        mark = "PASS" if item["passed"] else "FAIL"
        print(f"  [{mark}] {item['item']} {item['criterion']}: {item['detail']}")
    print(f"验收报告已落盘：{args.out}")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
