# 文件名：scripts/静态扫描.py
"""
静态扫描薄入口（R-18 脚本薄入口纪律）
====================================
委托 myquant/leakage_gate/static_scan.py 的 main_cli 执行静态 AST 扫描，
不在此新增业务逻辑（禁止新增业务类定义）。

用法：
  python scripts/静态扫描.py [paths...]    # 只扫指定变更文件
  python scripts/静态扫描.py --all          # 全量扫描 myquant + scripts
  python scripts/静态扫描.py --json         # JSON 输出（CI/工具对接）

返回码：0=无阻断命中（可含警告）；1=存在阻断命中或参数错误。
被 pre-commit hook（.pre-commit-config.yaml）与 CI job（static-scan-check）调用。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 保证项目根可导入 myquant（独立运行 / pre-commit / CI 均可用）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    """薄入口：参数与路径探针在此，扫描业务逻辑委托 static_scan.main_cli。"""
    from myquant.leakage_gate.static_scan import main_cli

    return main_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
