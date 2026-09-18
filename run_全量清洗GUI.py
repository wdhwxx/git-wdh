# -*- coding: utf-8 -*-
# 文件名：run_全量清洗GUI.py
"""
数据全量清洗窗口入口（薄入口）
================================
在 scripts/数据全量清洗.py 之上提供图形化配置窗口：
先弹窗选择清洗范围、指数/基金过滤规则（自定义默认）与清洗参数，
生成 config/data_clean_run.json 后新开控制台运行全量清洗。

使用方法：
    python run_全量清洗GUI.py

依赖：PySide6（quant 环境已安装）
    pip install -e ".[gui]"
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        if current == current.parent:
            break
        current = current.parent
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def main() -> int:
    # 将项目根加入 sys.path，确保能 import myquant
    project_root = find_project_root()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # 检查 PySide6
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("[错误] 未安装 PySide6。请先安装 GUI 依赖：")
        print("       pip install -e \".[gui]\"")
        return 1

    from myquant.gui.data_clean_dialog import run_dialog

    return run_dialog(project_root)


if __name__ == "__main__":
    sys.exit(main())
