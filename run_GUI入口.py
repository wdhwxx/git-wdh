# 文件名：run_GUI入口.py
"""
GUI桌面界面入口脚本（薄入口）
==============================

功能：
    启动 Pythonwdhmm 鲲鹏量化交易系统的桌面图形界面（GUI）。
    本脚本是薄入口，不包含任何GUI业务逻辑。

使用方法：
    # 启动GUI
    python run_GUI入口.py

    # 指定主题
    python run_GUI入口.py --theme dark

    # 启动时自动加载配置
    python run_GUI入口.py --config config/backtest_config.yaml

    # 最大化窗口启动
    python run_GUI入口.py --maximized

技术栈：
    - GUI框架：PySide6（Qt for Python），主对话框 myquant.gui.backtest_config_dialog
    - 旧骨架：myquant.gui.main_window.MainWindow（tkinter，由 myquant.gui 导出）
    - 可选依赖：pip install -e ".[gui]"

设计原则（薄入口，规则2/规则3）：
    - 本脚本只做：参数解析 → 依赖检查 → 调用 myquant.gui 内部函数启动界面
    - 所有GUI业务逻辑在 myquant.gui 包中实现
    - GUI与回测/实盘共用同一套核心代码（策略、指标、风控），通过引擎抽象层调用
    - GUI是可选组件，不安装 PySide6 时回测和实盘仍可通过命令行运行

阶段说明：
    GUI 已实现（L1修复，见回写记录#31）：本入口桥接实际可用的回测配置主对话框
    myquant.gui.backtest_config_dialog.run_dialog（PySide6，与 run_回测配置GUI.py
    同款调用）。原骨架 myquant.gui.main_window.MainWindow（tkinter 主窗口）保留由
    myquant.gui 导出；原引用不存在的 myquant.gui.app.QuantApp 已移除。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    max_depth = 20
    depth = 0
    while current != current.parent and depth < max_depth:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        depth += 1
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def check_gui_dependencies() -> tuple[bool, str]:
    """
    检查GUI依赖是否已安装

    返回：
        (是否可用, 缺失的依赖信息)
    """
    missing = []
    try:
        import PySide6  # noqa: F401
    except ImportError:
        missing.append("PySide6>=6.5.0")

    if missing:
        return False, ", ".join(missing)
    return True, ""


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="run_GUI入口",
        description="Pythonwdhmm 鲲鹏量化交易系统 — GUI桌面界面入口（薄入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python run_GUI入口.py
  python run_GUI入口.py --verbose

安装GUI依赖：
  pip install -e ".[gui]"
        """,
    )

    # 配置文件
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="启动时自动加载的配置文件路径（YAML格式）",
    )

    # 界面设置
    parser.add_argument(
        "--theme",
        type=str,
        default="default",
        choices=["default", "dark", "light"],
        help="界面主题（default=默认 / dark=深色 / light=浅色）",
    )
    parser.add_argument(
        "--maximized",
        action="store_true",
        help="启动时最大化窗口",
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="启动时全屏显示",
    )

    # 调试
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="启用详细日志输出（DEBUG级别）",
    )

    return parser


def launch_gui(args: argparse.Namespace) -> int:
    """
    启动GUI（薄入口：调用 myquant.gui 内部函数）

    返回：
        0  正常退出
        1  依赖缺失
        2  GUI启动失败
    """
    project_root = find_project_root()

    # 检查GUI依赖
    has_deps, missing = check_gui_dependencies()
    if not has_deps:
        print("=" * 60)
        print("Pythonwdhmm 鲲鹏量化交易系统 — GUI入口")
        print("=" * 60)
        print()
        print(f"项目根目录：{project_root}")
        print()
        print("[错误] GUI依赖未安装。")
        print(f"  缺失：{missing}")
        print()
        print("安装命令：")
        print("  pip install -e \".[gui]\"")
        print()
        print("GUI是可选组件，不安装时可通过命令行运行回测和实盘：")
        print("  python run_回测入口.py --help")
        print("  python run_实盘入口.py --help")
        print()
        return 1

    # 旧版主窗口参数兼容提示（当前回测配置窗口版本不生效，不静默忽略）
    legacy_hints = []
    if args.theme != "default":
        legacy_hints.append(f"--theme {args.theme}")
    if args.maximized:
        legacy_hints.append("--maximized")
    if args.fullscreen:
        legacy_hints.append("--fullscreen")
    if args.config:
        legacy_hints.append(f"--config {args.config}")
    if legacy_hints:
        print("[提示] 以下参数由旧版主窗口保留，当前回测配置窗口版本不生效（已忽略）：")
        print("       " + "、".join(legacy_hints))
        print("       --config 可在回测配置窗口内手动加载配置文件。")
        print()

    # 初始化日志（--verbose 时 DEBUG 级别）
    try:
        from myquant.core.logger import setup_logger

        setup_logger(
            level="DEBUG" if args.verbose else "INFO",
            log_dir=project_root / "logs",
        )
    except Exception:
        # 日志初始化失败不阻断GUI（业务逻辑仍在 myquant 内）
        pass

    # 启动实际GUI主体（L1修复：桥接回测配置主对话框，薄入口）
    try:
        from myquant.gui.backtest_config_dialog import run_dialog

        return run_dialog(project_root)
    except Exception as e:
        print("[错误] GUI启动失败：", e)
        return 2


def main() -> int:
    """主入口函数"""
    parser = setup_argument_parser()
    args = parser.parse_args()
    return launch_gui(args)


if __name__ == "__main__":
    sys.exit(main())
