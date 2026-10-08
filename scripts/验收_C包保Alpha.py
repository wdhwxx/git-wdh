# -*- coding: utf-8 -*-
"""C 包保 Alpha 验收薄入口（C-12，回写 #241）。

职责仅：解析参数 → 调用 Runner → 落 JSON。零业务逻辑（业务断言全在
``myquant/monitoring/c_acceptance.py``）。

Usage:
    python scripts/验收_C包保Alpha.py
    python scripts/验收_C包保Alpha.py --out docs/监控档案/C包保Alpha验收_20261006.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许从仓库根直接运行脚本（scripts/ 不在 sys.path）
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from myquant.acceptance.c_acceptance import CAcceptanceRunner  # noqa: E402

DEFAULT_OUT = _REPO_ROOT / "docs" / "监控档案" / "C包保Alpha验收_20261006.json"


def main() -> int:
    """解析参数并执行验收。

    Returns:
        int: overall_pass 为 True 返回 0，否则返回 1。
    """
    parser = argparse.ArgumentParser(description="C 包保 Alpha 验收 Runner 入口")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="验收 JSON 报告输出路径")
    args = parser.parse_args()

    report = CAcceptanceRunner().write_report(args.out)
    print(f"[C-12] overall_pass={report['overall_pass']} "
          f"({report['totals']['pass']}/{report['totals']['total']} pass) -> {args.out}")
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
