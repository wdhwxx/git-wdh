# -*- coding: utf-8 -*-
"""配置死键扫描薄入口（P2-07）。

用法:
    python scripts/配置死键扫描.py
    python scripts/配置死键扫描.py --config config/backtest_config.yaml --root .

报告配置里"写了但 myquant 源码零消费"的死键；新增死键（超出已知基线）会高亮。
业务逻辑全部在 myquant.leakage_gate.config_deadkey。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 薄入口：把项目根加进 sys.path，再导入业务模块
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from myquant.leakage_gate.config_deadkey import (  # noqa: E402
    DEFAULT_CONFIG_YAML,
    scan_dead_keys,
)


def main() -> int:
    """CLI 主入口。

    Returns:
        退出码：0=无新增死键；1=有新增死键（可作为 CI 门禁信号）。
    """
    parser = argparse.ArgumentParser(description="扫描配置死键（配置有但代码零消费）")
    parser.add_argument("--config", default=DEFAULT_CONFIG_YAML, help="配置 yaml 路径")
    parser.add_argument("--root", default=".", help="项目根目录")
    args = parser.parse_args()

    report = scan_dead_keys(config_path=args.config, project_root=args.root)
    print(report.summary())
    if report.whitelisted_segments:
        print(f"  白名单段（跳过）: {', '.join(report.whitelisted_segments)}")
    if report.dead_keys:
        print(f"  死键清单({len(report.dead_keys)}): {', '.join(report.dead_keys)}")
    if report.new_dead_keys:
        print(f"  ⚠ 新增死键({len(report.new_dead_keys)}): {', '.join(report.new_dead_keys)}")
        return 1
    print("  ✓ 无新增死键")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
