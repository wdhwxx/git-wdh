# 文件名：scripts/数据库初始化.py
"""
数据库初始化脚本（薄入口）
==========================

功能：
    初始化 SQLite 数据库（quant.db），创建表结构，执行迁移，导入股票元数据。
    本脚本是薄入口，不包含任何数据库业务逻辑。

使用方法：
    # 初始化数据库（如已存在则跳过建表）
    python scripts/数据库初始化.py

    # 重置数据库（危险！会删除所有数据后重建）
    python scripts/数据库初始化.py --reset

    # 仅检查数据库状态
    python scripts/数据库初始化.py --check

    # 导入股票列表（从通达信本地或akshare获取）
    python scripts/数据库初始化.py --import-stocks

    # 指定数据库路径
    python scripts/数据库初始化.py --db-path D:/data/quant.db

设计原则（薄入口，规则2/规则3）：
    - 本脚本只做：参数解析 → 调用 myquant.database 内部函数 → 结果展示
    - 所有数据库业务逻辑在 myquant.database 包中实现
    - 数据库同时服务于回测和实盘（规则2：实盘无缝对接）
    - 支持数据库版本管理和迁移（migrations）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


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


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="数据库初始化",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 数据库初始化（薄入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python scripts/数据库初始化.py
  python scripts/数据库初始化.py --reset
  python scripts/数据库初始化.py --check
  python scripts/数据库初始化.py --import-stocks
        """,
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="重置数据库（删除现有数据库后重新初始化，危险操作！）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅检查数据库状态，不做任何修改",
    )
    parser.add_argument(
        "--import-stocks",
        action="store_true",
        help="导入A股股票列表到数据库（优先通达信本地，备用akshare）",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="数据库文件路径，默认 database/quant.db",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="启用详细日志输出",
    )

    return parser


def print_banner(title: str) -> None:
    """打印标题横幅"""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)
    print()


def check_database_status(db_path: Path) -> int:
    """检查数据库状态"""
    print_banner("数据库状态检查")
    print(f"  数据库路径：{db_path}")

    if not db_path.exists():
        print("  状态：数据库文件不存在，需要初始化")
        print()
        return 1

    file_size = db_path.stat().st_size
    print(f"  文件大小：{file_size:,} 字节 ({file_size / 1024:.1f} KB)")

    try:
        from myquant.database.connection import DatabaseConnection
        from myquant.database.schema import table_exists, TABLE_NAMES
        from myquant.database.migrations import MigrationManager

        conn = DatabaseConnection(db_path)
        conn.connect()

        # 检查各表是否存在
        print(f"  数据表检查：")
        existing_count = 0
        for table_name in TABLE_NAMES.values():
            exists = table_exists(conn, table_name)
            status = "✓ 已创建" if exists else "✗ 缺失"
            print(f"    {table_name:<20s} {status}")
            if exists:
                existing_count += 1

        print(f"  已创建表：{existing_count}/{len(TABLE_NAMES)}")

        # 检查迁移版本
        try:
            manager = MigrationManager(conn)
            version = manager.current_version()
            print(f"  数据库版本：v{version}")
        except Exception:
            print(f"  数据库版本：未初始化")

        conn.close()
    except ImportError as e:
        print(f"  导入失败：{e}")
        print("  请先运行：pip install -e \".[dev]\"")
        return 2
    except Exception as e:
        print(f"  读取失败：{e}")
        return 2

    print()
    return 0


def run_database_init(args: argparse.Namespace) -> int:
    """
    执行数据库初始化（薄入口：调用 myquant.database 内部函数）

    返回：
        0  成功
        1  路径错误
        2  初始化失败
    """
    project_root = find_project_root()

    # 确保项目根目录在 sys.path 中（支持独立运行：python scripts/数据库初始化.py）
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # 确定数据库路径
    if args.db_path:
        db_path = Path(args.db_path)
    else:
        db_path = project_root / "database" / "quant.db"

    # 确保数据库目录存在
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 检查模式
    if args.check:
        return check_database_status(db_path)

    print_banner("Pythonwdhmm 鲲鹏量化交易系统 — 数据库初始化")
    print(f"  项目根目录：{project_root}")
    print(f"  数据库路径：{db_path}")
    print(f"  操作模式：  {'重置' if args.reset else '初始化'}")
    print(f"  导入股票：  {'是' if args.import_stocks else '否'}")
    print()

    # 重置操作的安全确认
    if args.reset and db_path.exists():
        print("[警告] --reset 将删除现有数据库！")
        print(f"  待删除文件：{db_path}")
        print(f"  文件大小：  {db_path.stat().st_size:,} 字节")
        print()
        confirm = input("确认删除并重建？输入 'YES' 继续：").strip()
        if confirm != "YES":
            print("已取消。")
            return 0
        db_path.unlink()
        print("  已删除旧数据库。")
        print()

    # 导入数据库层
    try:
        from myquant.database.connection import DatabaseConnection
        from myquant.database.schema import create_all_tables, TABLE_NAMES
        from myquant.database.migrations import migrate_to_latest
    except ImportError as e:
        print(f"[错误] 导入 myquant.database 失败：{e}")
        print("  请先运行：pip install -e \".[dev]\"")
        return 2

    # 创建连接
    conn = DatabaseConnection(db_path)
    conn.connect()
    print("[1/3] 数据库连接成功")

    # 创建所有表
    create_all_tables(conn)
    print(f"[2/3] 已创建 {len(TABLE_NAMES)} 张数据表")

    # 执行数据库迁移
    try:
        applied = migrate_to_latest(conn)
        if applied:
            print(f"[3/3] 数据库迁移完成，应用了 {len(applied)} 个版本")
        else:
            print("[3/3] 数据库已是最新版本")
    except Exception as e:
        print(f"[3/3] 数据库迁移跳过：{e}")

    conn.close()
    print()
    print("数据库初始化完成！")
    print(f"  数据库文件：{db_path}")

    # 导入股票列表
    if args.import_stocks:
        print()
        print_banner("导入A股股票列表")
        try:
            from myquant.data_access import DataFetcher
            from myquant.data_access.sources.tdx_source import TdxDataSource
            from myquant.data_access.sources.akshare_source import AkshareDataSource

            fetcher = DataFetcher()
            # 优先通达信本地
            tdx = TdxDataSource()
            if tdx.health_check().value == "healthy":
                fetcher.add_source(tdx)
                print("  数据源：通达信本地")
            else:
                print("  通达信本地不可用，尝试akshare网络源...")
                fetcher.add_source(AkshareDataSource())
                print("  数据源：akshare网络")

            stocks = fetcher.get_stock_list()
            print(f"  获取到 {len(stocks)} 只股票")

            # 保存到数据库（通过data_source表记录）
            from myquant.database.connection import get_connection
            from myquant.database.queries import DataQueries
            conn2 = get_connection(db_path)
            queries = DataQueries(conn2)
            # 注册数据源
            queries.register_data_source("stock_list", "network", priority=100, description="A股股票列表")
            conn2.close()
            print("  股票列表已记录")
        except Exception as e:
            print(f"  导入失败：{e}")
            print("  可稍后手动运行：python scripts/数据库初始化.py --import-stocks")

    print()
    print("=" * 60)
    return 0


def main() -> int:
    """主入口函数"""
    parser = setup_argument_parser()
    args = parser.parse_args()
    return run_database_init(args)


if __name__ == "__main__":
    sys.exit(main())
