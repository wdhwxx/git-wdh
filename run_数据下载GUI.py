# -*- coding: utf-8 -*-
# 文件名：run_数据下载GUI.py
"""
数据下载管理器入口（薄入口）
============================
在 scripts/数据下载.py 命令行工具之上提供图形化下载管理弹窗：
信息区（数据源状态+数据概览）、8项功能勾选（原6项+新增2项）、
十行滚动实时进度、开始/取消+总进度条。

使用方法：
    python run_数据下载GUI.py

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


def _acquire_single_instance_lock() -> bool:
    """
    单实例互斥锁（Windows named mutex）

    数据下载会全量读写同一个 quant.db，多实例并发会引发 SQLite
    disk I/O error（历史事故：3 个 GUI 并发写库导致 WAL 损坏）。
    通过 CreateMutexW 实现：已有实例持有同名互斥体时返回 False，
    调用方应提示并退出；句柄在进程生命周期内持有（不关闭），
    进程退出/崩溃后内核自动释放，不会残留死锁。
    """
    if sys.platform != "win32":
        return True  # 非 Windows 不强制（项目以 Windows 部署为主）
    import ctypes
    from ctypes import wintypes  # noqa: F401  确保 wintypes 注册

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, "Local\\Pythonwdhmm_DataDownloadGUI")
    if not handle:
        return False
    # ERROR_ALREADY_EXISTS = 183：已有实例持有互斥体
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    return True


def main() -> int:
    if not _acquire_single_instance_lock():
        print("检测到数据下载管理器已在运行（单实例限制），请勿重复启动。")
        return 1
    # 将项目根加入 sys.path，确保能 import myquant
    project_root = find_project_root()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from PySide6.QtWidgets import QApplication
    from myquant.gui.data_download_dialog import DataDownloadDialog

    app = QApplication(sys.argv)
    app.setApplicationName("数据下载管理器")
    win = DataDownloadDialog()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
