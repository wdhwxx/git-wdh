# 文件名：scripts/运行API.py
"""
运行API.py - 量化交易系统API服务入口
======================================
启动REST API服务（基于FastAPI + Uvicorn）。

使用方式:
    python scripts/运行API.py                          # 默认 0.0.0.0:8000
    python scripts/运行API.py --host 127.0.0.1       # 指定地址
    python scripts/运行API.py --port 9000             # 指定端口
    python scripts/运行API.py --reload                 # 开发模式热重载
    python scripts/运行API.py --help                   # 查看帮助

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数

防未来函数说明:
  - 入口脚本只启动当前API服务，不预测未来
  - 不包含任何未来数据或预测逻辑
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 将项目根目录加入sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.core.logger import get_logger

logger = get_logger("scripts.run_api")


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
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
        default="0.0.0.0",
        help="监听地址（默认: 0.0.0.0，允许外部访问）",
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
    return parser.parse_args()


def check_dependencies() -> bool:
    """
    检查依赖是否安装

    Returns
    -------
    bool : 依赖是否齐全
    """
    missing = []

    try:
        import fastapi  # noqa: F401
    except ImportError:
        missing.append("fastapi")

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        missing.append("uvicorn")

    if missing:
        print("=" * 60)
        print("  ⚠️ 缺少必要依赖")
        print("=" * 60)
        print(f"\n  缺少: {', '.join(missing)}")
        print("\n  请运行以下命令安装:")
        print(f"\n    pip install {' '.join(missing)}")
        print("\n  安装后重新运行:")
        print("\n    python scripts/运行API.py")
        print("\n" + "=" * 60)
        return False

    return True


def print_banner(args: argparse.Namespace) -> None:
    """打印启动横幅"""
    print()
    print("=" * 60)
    print("  Pythonwdhmm 量化交易系统 - API服务")
    print("=" * 60)
    print(f"  标题:     {args.title}")
    print(f"  版本:     {args.version}")
    print(f"  监听地址: http://{args.host}:{args.port}")
    print(f"  热重载:   {'开启' if args.reload else '关闭'}")
    print(f"  工作进程: {args.workers}")
    print(f"  认证:     {'开启 (X-API-Key)' if not args.disable_auth else '禁用 (仅开发)'}")
    print(f"  限流:     {'开启 (100次/60秒/IP)' if not args.disable_rate_limit else '禁用 (仅开发)'}")
    print("-" * 60)
    print("  API文档:")
    print(f"    Swagger UI:  http://{args.host}:{args.port}/docs")
    print(f"    ReDoc:       http://{args.host}:{args.port}/redoc")
    print(f"    OpenAPI:     http://{args.host}:{args.port}/openapi.json")
    print("-" * 60)
    print("  按 Ctrl+C 停止服务")
    print("=" * 60)
    print()


def main() -> None:
    """主函数"""
    args = parse_args()

    # 检查依赖
    if not check_dependencies():
        sys.exit(1)

    # 打印启动信息
    print_banner(args)

    # 导入并创建应用（C1修复：认证/限流默认开启，可用 --disable-* 显式关闭）
    try:
        import os as _os

        from myquant.api.routes import create_app
        app = create_app(
            title=args.title,
            version=args.version,
            enable_auth=not args.disable_auth,
            enable_rate_limit=not args.disable_rate_limit,
        )
    except Exception as e:
        logger.error(f"创建API应用失败: {e}", exc_info=True)
        print(f"\n❌ 创建API应用失败: {e}\n")
        sys.exit(1)

    # 认证开启但未配置密钥 → 醒目提示（安全默认：拒绝所有请求）
    if not args.disable_auth and not _os.environ.get("QUANT_API_KEYS"):
        print()
        print("  ⚠️  认证已开启，但未配置环境变量 QUANT_API_KEYS")
        print("      所有 API 请求将被拒绝（安全默认）。")
        print("      配置示例（PowerShell）:")
        print("        $env:QUANT_API_KEYS='your-key-1,your-key-2'")
        print("      Python 启动后所有请求需携带请求头 X-API-Key: <密钥>")

    # 启动服务
    try:
        import uvicorn
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            reload=args.reload,
            workers=args.workers if not args.reload else 1,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n\n👋 API服务已停止\n")
    except Exception as e:
        logger.error(f"API服务运行异常: {e}", exc_info=True)
        print(f"\n❌ API服务运行异常: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
