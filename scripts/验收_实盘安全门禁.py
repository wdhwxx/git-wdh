# 文件名：scripts/验收_实盘安全门禁.py
"""
验收_实盘安全门禁.py - 实盘安全门禁第0阶段验收薄入口（回写#287）
==========================================================================
薄入口纪律：仅做 参数解析 + 路径探测 + 调用 myquant/acceptance/live_safety_gate.py，
不写入任何业务逻辑。

用法示例:
    python scripts/验收_实盘安全门禁.py                       # 全量验收
    python scripts/验收_实盘安全门禁.py --select 0.1          # 只跑 0.1 施工项用例
    python scripts/验收_实盘安全门禁.py --out docs/监控档案/实盘安全门禁验收.json
    python scripts/验收_实盘安全门禁.py --list                # 列出已登记验收项
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# ── 路径探测：支持从任意目录启动（scripts/ 目录启动时顶层 import 需要项目根入 sys.path）──
_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from myquant.acceptance.live_safety_gate import (  # noqa: E402  # 薄入口：sys.path 注入后导入
    GATE_ITEMS,
    LiveSafetyGateRunner,
)


def _default_out_path() -> str:
    """默认报告路径：项目根/docs/监控档案/实盘安全门禁验收_<日期>.json（根治 scripts/ 启动落档分叉）。"""
    import datetime

    today = datetime.date.today().strftime("%Y%m%d")
    return str(_REPO_ROOT / "docs" / "监控档案" / f"实盘安全门禁验收_{today}.json")


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析命令行参数（纯解析，无业务逻辑）。"""
    parser = argparse.ArgumentParser(
        description="实盘安全门禁第0阶段验收（V3 §九 用例映射）",
    )
    parser.add_argument(
        "--out", default=_default_out_path(),
        help="JSON 验收报告输出路径（默认 docs/监控档案/实盘安全门禁验收_<日期>.json）",
    )
    parser.add_argument(
        "--mode", default="full", choices=("full",),
        help="验收档位（当前仅 full）",
    )
    parser.add_argument(
        "--select", default="",
        help="只展示/断言 item 前缀匹配的验收项（如 0.1 / 0.4 / LIVE）；空=全部",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="列出已登记验收项后退出（不执行验收）",
    )
    return parser.parse_args(argv)


def _main(argv: Optional[List[str]] = None) -> int:
    """验收入口（薄：解析→调用→落盘→摘要）。"""
    args = _parse_args(argv)

    if args.list:
        print("已登记验收项（施工清单 §12 映射）：")
        for item_id in GATE_ITEMS:
            print(f"  - {item_id}")
        return 0

    runner = LiveSafetyGateRunner()
    try:
        report = runner.run(mode=args.mode)
    finally:
        runner.close()

    # 展示层过滤（--select 前缀），报告本体保留全量
    select = args.select.strip()
    items = report["items"]
    if select:
        items = [it for it in items if it["item"].startswith(select)]

    print(f"实盘安全门禁第0阶段验收（{args.mode} 档）:")
    for it in items:
        status = "PASS" if it.get("pass") else "FAIL"
        print(f"  [{status}] {it['item']} - {it.get('detail', '')}")

    runner.write_report(report, args.out)
    print(f"\nsummary: passed={report['summary']['passed']} "
          f"failed={report['summary']['failed']} total={report['summary']['total']}")
    print(f"报告已落盘: {args.out}")

    # 未选择过滤时以整体结果定退出码；选择过滤时按被过滤子集定退出码
    shown_failed = sum(1 for it in items if not it.get("pass"))
    return 1 if shown_failed > 0 else 0


if __name__ == "__main__":
    raise SystemExit(_main())
