# -*- coding: utf-8 -*-
# 文件名：鲲鹏仿真运测.py
"""
鲲鹏仿真运测 - Paper 仿真运行入口（#62）
==========================================
启动后先弹出「仿真运行模式」选择窗：
  - A · 定时托管：展示豆包侧 cron 状态（只读），确认后进入回测配置主界面。
  - B · GUI 常驻：拉起 paper 引擎常驻进程，实时显示 bar/信号/成交/盈亏，关窗即停。

薄入口原则：本脚本只做依赖检查 → 调起 paper_run_dialog，不含业务逻辑。

使用方法：
    python 鲲鹏仿真运测.py

依赖: PySide6>=6.5.0
"""

from __future__ import annotations

import sys
from pathlib import Path


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        if current == current.parent:
            break
        current = current.parent
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def check_deps() -> tuple[bool, str]:
    """检查 PySide6 是否安装。"""
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False, "PySide6>=6.5.0"
    return True, ""


def main() -> int:
    """主入口函数。"""
    project_root = find_project_root()

    ok, missing = check_deps()
    if not ok:
        print("=" * 60)
        print("鲲鹏仿真运测 - Paper 仿真运行入口")
        print("=" * 60)
        print(f"项目根目录：{project_root}")
        print(f"[错误] 缺少依赖：{missing}")
        print("安装命令：pip install \"pyside6>=6.5.0\"")
        return 1

    # 日志初始化
    try:
        from myquant.core.logger import setup_logger
        setup_logger(level="INFO", log_dir=project_root / "logs")
    except Exception:
        pass

    # 启动仿真运行模式选择弹窗
    try:
        from myquant.gui.paper_run_dialog import run_paper_dialog
        ret = run_paper_dialog(project_root)
        # 仿真弹窗关闭即结束，不再自动拉起回测配置窗口
        return 0
    except Exception as e:
        print(f"[错误] 仿真运行弹窗启动失败：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
