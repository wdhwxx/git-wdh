# -*- coding: utf-8 -*-
# 文件名：run_回测配置GUI.py
"""
回测配置窗口入口（薄入口）
==========================
在回测入口脚本之上提供一个图形化配置窗口：
运行前先弹窗选择过滤条件（排除股票前缀等）与基础回测设置，
生成 backtest_config.yaml 后自动启动回测。

使用方法：
    python run_回测配置GUI.py

依赖：PySide6（可选依赖，quant 环境已安装）
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

    from myquant.gui.backtest_config_dialog import run_dialog

    return run_dialog(project_root)


if __name__ == "__main__":
    sys.exit(main())
