# -*- coding: utf-8 -*-
# 文件名：myquant/api/cli.py
"""
cli.py - API 服务命令行参数解析（P3-7 自 scripts/运行API.py 下沉）
==================================================================
纯参数解析逻辑下沉 myquant（消除 tests/unit/test_api_middleware.py 反向导入 scripts 的
分层违规）；scripts/运行API.py 只做薄入口调用本模块。

防未来函数：仅解析静态参数，不涉及任何行情/时间数据。
"""

from __future__ import annotations

import argparse
from typing import List, Optional


def build_api_parser() -> argparse.ArgumentParser:
    """构建 API 服务命令行解析器（纯解析，无业务逻辑）"""
    parser = argparse.ArgumentParser(
        description="Pythonwdhmm 量化交易系统 - API服务入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/运行API.py                          # 默认 0.0.0.0:8000
  python scripts/运行API.py --host 127.0.0.1       # 本地访问
  python scripts/运行API.py --port 9000             # 指定端口
  python scripts/运行API.py --reload                 # 开发模式热重载

安装依赖:
  pip install fastapi uvicorn
        """,
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="监听地址（KP-P1-01：默认仅回环 127.0.0.1，外部访问需显式指定）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="监听端口（默认: 8000）",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="启用热重载（开发模式，代码修改自动重启）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="工作进程数（默认: 1，生产环境建议设为CPU核心数）",
    )
    parser.add_argument(
        "--title",
        type=str,
        default="Pythonwdhmm 量化交易系统 API",
        help="API标题（默认: Pythonwdhmm 量化交易系统 API）",
    )
    parser.add_argument(
        "--version",
        type=str,
        default="v1.0.0",
        help="API版本（默认: v1.0.0）",
    )
    parser.add_argument(
        "--disable-auth",
        action="store_true",
        help="禁用API Key认证（仅开发环境使用；默认开启，密钥经环境变量 QUANT_API_KEYS 配置）",
    )
    parser.add_argument(
        "--disable-rate-limit",
        action="store_true",
        help="禁用IP限流（仅开发环境使用；默认开启）",
    )
    return parser


def parse_api_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """解析 API 服务命令行参数（argv=None 时取 sys.argv[1:]）"""
    return build_api_parser().parse_args(argv)


if __name__ == "__main__":
    # 自测：解析器组装与默认值契约
    _args = parse_api_args([])
    assert _args.host == "0.0.0.0"
    assert _args.port == 8000
    assert _args.disable_auth is False
    assert _args.disable_rate_limit is False
    _args2 = parse_api_args(["--disable-auth", "--disable-rate-limit"])
    assert _args2.disable_auth is True and _args2.disable_rate_limit is True
    print("myquant/api/cli.py 自测通过")
