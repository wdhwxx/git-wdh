# 文件名：scripts/监控配置管理.py
"""
D-7 监控即代码完善 薄入口（scripts 层，业务逻辑在 myquant）。
==================================================================
管理 config/monitoring/*.yaml（仪表盘/告警规则/Runbook/采集配置）的版本化、
变更关联监控与回滚：仅做命令行编排，不含业务逻辑。

用法：
  python scripts/监控配置管理.py --init            # 现有配置项打初始版本 v1
  python scripts/监控配置管理.py --list            # 盘点配置项与当前版本
  python scripts/监控配置管理.py --history dashboard
  python scripts/监控配置管理.py --apply dashboard --content-file 新配置.yaml --by 操作人 --summary 说明
  python scripts/监控配置管理.py --rollback alert_rules --by 操作人
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# 允许从任意工作目录直接运行（含 scripts/ 目录，对齐 #254 验收_D包自治修复）；
# 业务逻辑全部在 myquant（薄入口）
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from myquant.monitoring.config_as_code import KNOWN_ITEMS, MonitoringConfigRegistry  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="监控配置管理",
        description="D-7 监控即代码：config/monitoring/*.yaml 版本化 + 变更关联监控 + 回滚",
    )
    parser.add_argument("--init", action="store_true", help="现有配置项打初始版本 v1")
    parser.add_argument("--list", action="store_true", help="盘点配置项与当前版本")
    parser.add_argument("--history", metavar="ITEM", help="查看某配置项变更史")
    parser.add_argument("--apply", metavar="ITEM", help="应用新配置（版本+1）")
    parser.add_argument("--content-file", metavar="PATH", help="--apply 时的新配置内容文件")
    parser.add_argument("--rollback", metavar="ITEM", help="回滚到上一版本")
    parser.add_argument("--by", default="operator", help="变更发起方（可追溯）")
    parser.add_argument("--summary", default="", help="变更说明（可追溯）")
    return parser


def _require_content(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"读取配置内容文件失败: {e}", file=sys.stderr)
        raise SystemExit(2)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    registry = MonitoringConfigRegistry()

    if args.init:
        n = registry.initialize(changed_by=args.by)
        print(f"初始化完成：{n} 个配置项已版本化（其余已版本化跳过）。")
        return 0
    if args.list:
        for item, info in registry.discover().items():
            ver = info["version"] if info["exists"] else "（未发现文件）"
            print(f"  {item:14s} version={ver}  sha256={info['sha256'] or '-'}")
        return 0
    if args.history:
        for e in registry.history(args.history):
            print(f"  v{e['version']:3d} {e['action']:8s} "
                  f"by={e['changed_by']} {e['applied_at']:.0f} {e['summary']}")
        return 0
    if args.apply:
        if not args.content_file:
            print("--apply 需要 --content-file 指定新配置内容文件", file=sys.stderr)
            return 2
        res = registry.apply(args.apply, _require_content(args.content_file),
                             changed_by=args.by, summary=args.summary)
        print(f"应用完成：{res['item']} → v{res['version']}（sha256={res['sha256'][:12]}…）")
        return 0
    if args.rollback:
        res = registry.rollback(args.rollback, changed_by=args.by)
        if res.get("rolled_back"):
            print(f"回滚完成：{res['item']} → v{res['version']}（恢复自 v{res['restored_version']}）")
        else:
            print(f"回滚未执行：{res.get('reason')}", file=sys.stderr)
        return 0 if res.get("rolled_back") else 2

    print("未指定操作（--init/--list/--history/--apply/--rollback）。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
