# 文件名：scripts/数据库迁移.py
"""
数据库迁移 - 中文入口脚本（薄入口，第6项D）
============================================
封装 Alembic 数据库迁移命令，提供中文友好的操作界面。

功能:
  - upgrade    升级数据库到最新版本（或指定版本）
  - downgrade  回滚数据库到指定版本
  - current    查看当前数据库版本
  - history    查看迁移历史链
  - stamp      标记数据库版本（不执行迁移，用于已有数据库兼容）
  - check      检查数据库状态（表数量、版本、待执行迁移）
  - init       初始化新数据库（等价于 upgrade head）

运行方式:
    # 查看当前版本
    python scripts/数据库迁移.py current

    # 升级到最新版本
    python scripts/数据库迁移.py upgrade

    # 升级到指定版本
    python scripts/数据库迁移.py upgrade --revision 0001

    # 回滚一个版本
    python scripts/数据库迁移.py downgrade --revision -1

    # 回滚到初始（清空所有表）
    python scripts/数据库迁移.py downgrade --revision base

    # 查看迁移历史
    python scripts/数据库迁移.py history

    # 标记已有数据库为最新版本（不执行迁移）
    python scripts/数据库迁移.py stamp --revision head

    # 检查数据库状态
    python scripts/数据库迁移.py check

    # 指定数据库路径（默认 database/quant.db）
    python scripts/数据库迁移.py --db path/to/custom.db current

设计原则（规则2/规则3/规则5）:
  - 薄入口：只做 参数解析→调用 Alembic→输出结果
  - 数据库路径与 myquant/database/connection.py 一致（默认 database/quant.db）
  - 支持 --db 参数和 QUANT_DB_URL 环境变量覆盖（实盘/测试切换）
  - 入口脚本用中文名，与项目其他中文入口一致
  - 为实盘无缝对接预留：生产环境升级前 check 检查、stamp 兼容已有库
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        if current == current.parent:
            break
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="数据库迁移",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 数据库迁移管理（Alembic）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/数据库迁移.py current
  python scripts/数据库迁移.py upgrade
  python scripts/数据库迁移.py upgrade --revision 0001
  python scripts/数据库迁移.py downgrade --revision -1
  python scripts/数据库迁移.py history
  python scripts/数据库迁移.py stamp --revision head
  python scripts/数据库迁移.py check
  python scripts/数据库迁移.py --db custom.db check""",
    )
    parser.add_argument("--db", type=str, default=None,
                        help="数据库文件路径（默认 database/quant.db，也可用环境变量 QUANT_DB_URL）")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出详细日志")

    subparsers = parser.add_subparsers(dest="command", help="操作命令")

    # upgrade
    p_up = subparsers.add_parser("upgrade", help="升级数据库到最新版本（或指定版本）")
    p_up.add_argument("--revision", type=str, default="head",
                      help="目标版本（默认 head=最新，可指定版本号如 0001）")

    # downgrade
    p_down = subparsers.add_parser("downgrade", help="回滚数据库到指定版本")
    p_down.add_argument("--revision", type=str, default="-1",
                        help="目标版本（默认 -1=回滚一个版本，base=清空全部）")

    # current
    subparsers.add_parser("current", help="查看当前数据库版本")

    # history
    subparsers.add_parser("history", help="查看迁移历史链")

    # stamp
    p_stamp = subparsers.add_parser("stamp", help="标记数据库版本（不执行迁移）")
    p_stamp.add_argument("--revision", type=str, default="head",
                         help="标记的目标版本（默认 head）")

    # check
    subparsers.add_parser("check", help="检查数据库状态（表数量、版本、完整性）")

    # init
    subparsers.add_parser("init", help="初始化新数据库（等价于 upgrade head）")

    return parser


def run_alembic(project_root: Path, args: list, db_path: str | None = None, verbose: bool = False) -> int:
    """运行 Alembic 命令"""
    env = {**os.environ, "PYTHONPATH": str(project_root)}
    if db_path:
        # 转为绝对路径
        db_abs = Path(db_path)
        if not db_abs.is_absolute():
            db_abs = project_root / db_path
        env["QUANT_DB_URL"] = f"sqlite:///{db_abs}"

    cmd = [sys.executable, "-m", "alembic"] + args
    if not verbose:
        # 过滤 INFO 日志，只保留关键输出
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_root), env=env)
        # 输出 stderr 中的非 INFO 行
        for line in result.stderr.splitlines():
            if "INFO" not in line and "WARNING" not in line:
                print(line)
        # 输出 stdout
        if result.stdout.strip():
            print(result.stdout.strip())
        return result.returncode
    else:
        result = subprocess.run(cmd, cwd=str(project_root), env=env)
        return result.returncode


def cmd_upgrade(project_root: Path, args, db_path: str | None) -> int:
    """升级数据库"""
    revision = args.revision
    print(f"[迁移] 升级到版本: {revision}")
    rc = run_alembic(project_root, ["upgrade", revision], db_path, args.verbose)
    if rc == 0:
        print("[迁移] 升级完成")
    else:
        print(f"[错误] 升级失败，返回码: {rc}")
    return rc


def cmd_downgrade(project_root: Path, args, db_path: str | None) -> int:
    """回滚数据库"""
    revision = args.revision
    print(f"[迁移] 回滚到版本: {revision}")
    if revision == "base":
        print("  [警告] 回滚到 base 将删除所有表和数据！")
    rc = run_alembic(project_root, ["downgrade", revision], db_path, args.verbose)
    if rc == 0:
        print("[迁移] 回滚完成")
    else:
        print(f"[错误] 回滚失败，返回码: {rc}")
    return rc


def cmd_current(project_root: Path, args, db_path: str | None) -> int:
    """查看当前版本"""
    print("[迁移] 当前数据库版本:")
    return run_alembic(project_root, ["current"], db_path, args.verbose)


def cmd_history(project_root: Path, args, db_path: str | None) -> int:
    """查看迁移历史"""
    print("[迁移] 迁移历史链:")
    return run_alembic(project_root, ["history", "--verbose"], db_path, args.verbose)


def cmd_stamp(project_root: Path, args, db_path: str | None) -> int:
    """标记版本（不执行迁移）"""
    revision = args.revision
    print(f"[迁移] 标记数据库版本为: {revision}（不执行迁移）")
    print("  [说明] 适用于已有数据库，标记后可正常使用 Alembic 管理后续迁移")
    rc = run_alembic(project_root, ["stamp", revision], db_path, args.verbose)
    if rc == 0:
        print("[迁移] 版本标记完成")
    else:
        print(f"[错误] 标记失败，返回码: {rc}")
    return rc


def cmd_check(project_root: Path, db_path: str | None) -> int:
    """检查数据库状态"""
    import sqlite3

    # 解析数据库路径
    if db_path:
        db_abs = Path(db_path)
        if not db_abs.is_absolute():
            db_abs = project_root / db_path
    else:
        env_url = os.environ.get("QUANT_DB_URL", "")
        if env_url.startswith("sqlite:///"):
            db_abs = Path(env_url[len("sqlite:///"):])
        else:
            db_abs = project_root / "database" / "quant.db"

    print("=" * 50)
    print("  数据库状态检查")
    print("=" * 50)
    print(f"  数据库路径: {db_abs}")

    if not db_abs.exists():
        print("  [状态] 数据库文件不存在，需要执行 init 或 upgrade")
        print("=" * 50)
        return 0

    conn = sqlite3.connect(str(db_abs))
    # 表数量
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name"
    ).fetchall()]
    # 索引数量
    indexes = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%' ORDER BY name"
    ).fetchall()]
    # Alembic 版本
    ver = None
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        ver = row[0] if row else None
    except Exception:
        pass

    print(f"  表数量: {len(tables)}")
    for t in tables:
        cnt = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        print(f"    - {t} ({cnt} 条记录)")
    print(f"  索引数量: {len(indexes)}")
    print(f"  Alembic版本: {ver or '未标记（需 stamp）'}")

    # 检查是否有未执行的迁移
    if ver:
        # 获取 heads
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "heads"],
            capture_output=True, text=True, cwd=str(project_root),
            env={**os.environ, "PYTHONPATH": str(project_root),
                 "QUANT_DB_URL": f"sqlite:///{db_abs}"},
        )
        heads = [l.strip().split()[0] for l in result.stdout.splitlines() if l.strip() and "(head)" in l]
        if ver in heads:
            print("  [状态] 已是最新版本，无需迁移")
        else:
            print(f"  [状态] 有新版本可用，建议执行 upgrade")
    else:
        print("  [状态] 未标记 Alembic 版本，建议执行 stamp head（已有库）或 upgrade（新库）")

    conn.close()
    print("=" * 50)
    return 0


def main() -> int:
    """主入口"""
    project_root = find_project_root()
    parser = setup_argument_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    db_path = args.db

    print("=" * 50)
    print("  Pythonwdhmm 鲲鹏量化交易系统 — 数据库迁移")
    print(f"  项目根目录: {project_root}")
    print("=" * 50)

    handlers = {
        "upgrade": lambda: cmd_upgrade(project_root, args, db_path),
        "downgrade": lambda: cmd_downgrade(project_root, args, db_path),
        "current": lambda: cmd_current(project_root, args, db_path),
        "history": lambda: cmd_history(project_root, args, db_path),
        "stamp": lambda: cmd_stamp(project_root, args, db_path),
        "check": lambda: cmd_check(project_root, db_path),
        "init": lambda: cmd_upgrade(project_root, argparse.Namespace(revision="head", verbose=args.verbose), db_path),
    }

    handler = handlers.get(args.command)
    if handler:
        return handler()
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
