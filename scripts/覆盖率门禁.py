# -*- coding: utf-8 -*-
"""覆盖率门禁薄入口（P1，v1.2 §6.2 P1-5）。

用法:
    python scripts/覆盖率门禁.py
    python scripts/覆盖率门禁.py --json outputs/coverage.json --junit test-results.xml
    python scripts/覆盖率门禁.py --report-only   # 报告模式（CI 首轮：基线待冻结）

读 pytest-cov 产物 coverage.json，按 v1.2 §5 判据（C-1~C-7）判定通过/失败；
业务逻辑全部在 myquant.leakage_gate.coverage_gate。
被 CI coverage-gate-check job 调用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 薄入口：把项目根加进 sys.path，再导入业务模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from myquant.leakage_gate.coverage_gate import (  # noqa: E402
    DEFAULT_COVERAGE_JSON,
    evaluate,
)


def main() -> int:
    """CLI 主入口。

    Returns:
        退出码：0=判据通过（或报告模式）；1=判据违反 / 参数错误。
    """
    parser = argparse.ArgumentParser(description="覆盖率门禁（v1.2 §5：分层覆盖率 + 总量棘轮 + 关键文件下限）")
    parser.add_argument("--json", default=DEFAULT_COVERAGE_JSON, help="pytest-cov coverage.json 路径")
    parser.add_argument("--junit", default=None, help="pytest --junitxml 路径（可选，C-7 跳过预算数据源）")
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--report-only", action="store_true",
                        help="报告模式：输出判据预演，不阻断（CI 首轮用，基线冻结后移除）")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    json_path = Path(args.json).resolve()
    junit_path = Path(args.junit).resolve() if args.junit else None

    report = evaluate(
        coverage_json=json_path,
        junit_xml=junit_path,
        project_root=root,
        report_only=args.report_only,
    )
    print(report.summary())
    if not report.ok:
        print(f"  ❌ 覆盖率门禁未通过（{len(report.violations)} 项违反）")
        # 报告模式：输出判据预演但不阻断（CI 首轮自产基线，冻结后转阻断）
        return 0 if args.report_only else 1
    print("  ✓ 覆盖率门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
