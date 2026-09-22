# -*- coding: utf-8 -*-
"""
鲲鹏策略参数网格扫描（薄入口，P3-6 业务逻辑已下沉 myquant/backtest/kunpeng_param_scanner.py）
=============================================================================================
真实数据，vectorized 模式，两阶段：
  阶段A：信号参数扫描（出场 fixed=standard，仓位 fixed=默认）
  阶段B：出场+仓位参数扫描（信号参数 fixed=当前默认）
本脚本仅做 argparse 参数解析 + 调用 KunpengParamScanner，不含任何业务逻辑。

用法（quant 环境）:
  D:\\ProgramData\\anaconda3\\envs\\quant\\python.exe scripts\\kunpeng_param_scan.py [--stage signal|exit|both] [--top N] [--min-trades N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myquant.backtest.kunpeng_param_scanner import KunpengParamScanner  # noqa: E402


def parse_args(argv: list) -> argparse.Namespace:
    """参数解析（薄入口职责）"""
    parser = argparse.ArgumentParser(description="鲲鹏策略参数网格扫描（薄入口）")
    parser.add_argument("--stage", choices=("signal", "exit", "both"), default="both")
    parser.add_argument("--top", type=int, default=50, help="活跃标的数上限")
    parser.add_argument("--min-trades", type=int, default=3, help="活跃标的成交笔数下限")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args(sys.argv[1:])
    scanner = KunpengParamScanner()
    return scanner.run(stage=args.stage, top_n=args.top, min_trades=args.min_trades)


if __name__ == "__main__":
    sys.exit(main())
