# -*- coding: utf-8 -*-
# 文件名：run_清理GUI.py
"""
项目清理窗口入口（薄入口）
==========================
启动图形化清理弹窗：勾选清理项目、设置回测输出保留期数、
选择执行模式，下半部滚动区实时展示 dry-run 预览 / 执行结果。
业务逻辑全部在 myquant/utils/cleanup_service.py（蓝图纪律：脚本只做薄入口）。

使用方法：
    python run_清理GUI.py

依赖：PySide6（可选依赖，quant 环境已安装）
    pip install -e ".[gui]"
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。

    Returns:
        项目根目录绝对路径。

    Raises:
        RuntimeError: 向上搜索达到最大深度仍未找到 pyproject.toml。
    """
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
    """主入口：探测项目根 → 校验 PySide6 → 启动清理对话框。

    Returns:
        应用退出码：0 正常退出，1 缺依赖。
    """
    project_root = find_project_root()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # 检查 PySide6（GUI 可选依赖）
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("[错误] 未安装 PySide6。请先安装 GUI 依赖：")
        print("       pip install -e \".[gui]\"")
        return 1

    from myquant.gui.cleanup_dialog import run_cleanup_dialog

    return run_cleanup_dialog(project_root)


if __name__ == "__main__":
    sys.exit(main())
