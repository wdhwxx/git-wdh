# -*- coding: utf-8 -*-
"""门禁参数变更审计薄入口（#C-4·5，判据A 对价 + 判据B 蓝图登记）。

用法:
    python scripts/门禁审计.py --before <SHA> --head <SHA>
    python scripts/门禁审计.py --before ${{ github.event.before }} --head ${{ github.sha }}

判据A：commit 范围内 test_architecture.py / coverage_gate.py 的基线/参数常量
       有值变更时，须满足（独立 commit + 工单 #N 或回写 + 蓝图回写）；
判据B：新增 .py/.md 文件须在蓝图 docs/project_layout_gear2.md 全文登记。
业务逻辑全部在 myquant.leakage_gate.gate_audit。
被 CI gate-audit job 调用（before 传 push 前 SHA，head 传本次 SHA）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 薄入口：把项目根加进 sys.path，再导入业务模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from myquant.leakage_gate.gate_audit import (  # noqa: E402
    run_audit,
)


def main() -> int:
    """CLI 主入口。

    Returns:
        退出码：0=判据通过；1=违反 / 参数错误 / git 不可用（fail-closed）。
    """
    parser = argparse.ArgumentParser(
        description="门禁参数变更审计（判据A 对价 + 判据B 蓝图登记，门禁顶红治理方案 §6）"
    )
    parser.add_argument("--before", required=True, help="审计范围起点 SHA（push 前）")
    parser.add_argument("--head", required=True, help="审计范围终点 SHA（本次）")
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    try:
        report = run_audit(before=args.before, head=args.head, project_root=root)
    except RuntimeError as exc:
        print(f"门禁审计失败（fail-closed）: {exc}")
        return 1
    print(report.summary())
    if not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
