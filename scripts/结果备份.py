# 文件名：scripts/结果备份.py
"""
结果备份脚本（薄入口）
======================

功能：
    备份回测结果、日志文件、数据库到指定目录，支持压缩和时间戳命名。
    本脚本是薄入口，不包含任何备份业务逻辑。

使用方法：
    # 默认备份（回测结果 + 日志，到 backups/ 目录）
    python scripts/结果备份.py

    # 指定备份输出目录
    python scripts/结果备份.py --output D:/backup/

    # 同时备份数据库
    python scripts/结果备份.py --include-db

    # 备份全部（结果 + 日志 + 数据库 + 配置）
    python scripts/结果备份.py --all

    # 压缩为 zip
    python scripts/结果备份.py --zip

    # 保留最近N个备份（自动清理旧备份）
    python scripts/结果备份.py --keep 5

    # 查看备份列表
    python scripts/结果备份.py --list

备份内容说明：
    - outputs/       回测结果（净值曲线、交易记录、绩效报告）
    - logs/          运行日志
    - database/      SQLite数据库（可选，--include-db）
    - config/        实际配置文件（可选，--include-config，注意含敏感信息）

设计原则（薄入口，规则2/规则3）：
    - 本脚本只做：参数解析 → 调用 myquant 内部工具函数 → 结果展示
    - 所有备份业务逻辑在 myquant.utils.file_utils 中实现
    - 备份机制同时服务于回测和实盘（规则2：实盘无缝对接）
    - 实盘状态备份通过 myquant.live.state_store 实现

阶段说明（P2-06更新）：
    已接入 myquant.utils.file_utils.backup_files，本脚本为可用入口；删除了 return 后不可达死代码。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional


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
        prog="结果备份",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 结果备份（薄入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python scripts/结果备份.py
  python scripts/结果备份.py --output D:/backup/ --zip
  python scripts/结果备份.py --all --keep 5
  python scripts/结果备份.py --list
        """,
    )

    # 备份内容
    parser.add_argument(
        "--include-db",
        action="store_true",
        help="同时备份数据库（database/quant.db）",
    )
    parser.add_argument(
        "--include-config",
        action="store_true",
        help="同时备份实际配置文件（注意：可能含敏感信息，如API密钥）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="备份全部内容（结果 + 日志 + 数据库 + 配置）",
    )

    # 输出设置
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="备份输出目录，默认 backups/",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="将备份压缩为 zip 文件",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help="保留最近N个备份，自动删除更早的备份",
    )

    # 查看模式
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出所有备份并退出",
    )
    parser.add_argument(
        "--verify",
        metavar="PATH",
        nargs="?",
        const="latest",
        help="校验备份 MANIFEST.sha256 完整性（缺省校验最近备份）",
    )
    parser.add_argument(
        "--restore",
        metavar="PATH",
        nargs="?",
        const="latest",
        help="按 MANIFEST 还原备份到项目根（缺省还原最近备份）",
    )

    # 其他
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="启用详细日志输出",
    )

    return parser


def list_backups(backup_dir: Path) -> None:
    """列出所有备份"""
    print("备份列表：")
    print("-" * 70)

    if not backup_dir.exists():
        print("  （备份目录不存在，尚无备份）")
        return

    items = sorted(backup_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)

    if not items:
        print("  （备份目录为空）")
        return

    for i, item in enumerate(items, 1):
        stat = item.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if item.is_dir():
            size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
            print(f"  {i:2d}. [目录] {item.name:<40s} {mtime}  {size / 1024:.1f} KB")
        else:
            print(f"  {i:2d}. [文件] {item.name:<40s} {mtime}  {stat.st_size / 1024:.1f} KB")


def cleanup_old_backups(backup_dir: Path, keep: int) -> int:
    """
    清理旧备份，保留最近 keep 个

    返回：删除的备份数量
    """
    if not backup_dir.exists():
        return 0

    items = sorted(backup_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)

    if len(items) <= keep:
        return 0

    deleted = 0
    for item in items[keep:]:
        # KP-P5-06: 白名单/锁定标记——含 .keep 标记的备份不清理（避免误删受保护备份）
        if _is_locked_backup(item):
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
        deleted += 1

    return deleted


def _is_locked_backup(item: Path) -> bool:
    """KP-P5-06: 判断备份是否带锁定/白名单标记（.keep），带标记不参与自动清理。"""
    if item.name.endswith(".keep"):
        return True
    if item.is_dir() and (item / ".keep").exists():
        return True
    return False


def _resolve_backup(backup_dir: Path, spec: str) -> Optional[Path]:
    """解析备份路径：'latest' 取最近备份，否则按给定路径。"""
    if spec and spec != "latest":
        p = Path(spec)
        return p if p.exists() else None
    if not backup_dir.exists():
        return None
    items = sorted(backup_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return items[0] if items else None


def collect_backup_sources(
    project_root: Path,
    include_db: bool,
    include_config: bool,
    all_mode: bool,
) -> List[tuple[Path, str]]:
    """
    收集需要备份的源路径

    返回：[(源路径, 备份内相对路径), ...]
    """
    sources: List[tuple[Path, str]] = []

    # 回测结果（默认备份）
    outputs_dir = project_root / "outputs"
    if outputs_dir.exists():
        sources.append((outputs_dir, "outputs"))

    # 日志（默认备份）
    logs_dir = project_root / "logs"
    if logs_dir.exists():
        sources.append((logs_dir, "logs"))

    # 数据库（KP-P5-06：默认纳入，不再可选 gate）
    db_path = project_root / "database" / "quant.db"
    if db_path.exists():
        sources.append((db_path, "database/quant.db"))

    # 配置文件（KP-P5-06：默认纳入实际配置，跳过 example 模板）
    config_dir = project_root / "config"
    if config_dir.exists():
        for yaml_file in config_dir.glob("*.yaml"):
            if not yaml_file.name.endswith(".example.yaml"):
                sources.append((yaml_file, f"config/{yaml_file.name}"))

    # 实盘状态/信号（KP-P5-06：默认纳入 state，供崩溃恢复还原）
    for state_dir, rel in (
        ("实盘状态", "实盘状态"),
        ("实盘信号", "实盘信号"),
        ("datasets/live_state", "datasets/live_state"),
    ):
        sd = project_root / state_dir
        if sd.exists():
            sources.append((sd, rel))

    return sources


def run_backup(args: argparse.Namespace) -> int:
    """
    执行备份（薄入口：调用 myquant.utils.file_utils 内部函数）

    返回：
        0  成功
        1  没有可备份的内容
        2  备份失败
    """
    project_root = find_project_root()
    # 项目根加入 sys.path（scripts 薄入口惯例，保证 python scripts/xxx.py 可导入 myquant）
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # 确定备份输出目录
    if args.output:
        backup_dir = Path(args.output)
    else:
        backup_dir = project_root / "backups"

    # 列出备份模式
    if args.list:
        list_backups(backup_dir)
        return 0

    # 校验备份模式（KP-P5-06）
    if args.verify is not None:
        target = _resolve_backup(backup_dir, args.verify)
        if target is None:
            print(f"[错误] 未找到待校验备份: {args.verify}")
            return 1
        from myquant.utils.file_utils import verify_backup as _vb
        res = _vb(target)
        print(f"[校验] {target.name}: 核对 {res['checked']} 项, ok={res['ok']}")
        for m in res["mismatches"]:
            print(f"  ✗ {m['path']}: {m['reason']}")
        return 0 if res["ok"] else 2

    # 还原备份模式（KP-P5-06）
    if args.restore is not None:
        target = _resolve_backup(backup_dir, args.restore)
        if target is None:
            print(f"[错误] 未找到待还原备份: {args.restore}")
            return 1
        from myquant.utils.file_utils import restore_backup as _rb
        res = _rb(target, project_root)
        print(f"[还原] {target.name}: 还原 {res['restored']} 项, ok={res['ok']}")
        for e in res["errors"]:
            print(f"  ✗ {e}")
        return 0 if res["ok"] else 2

    # 收集备份源
    sources = collect_backup_sources(
        project_root,
        include_db=args.include_db,
        include_config=args.include_config,
        all_mode=args.all,
    )

    if not sources:
        print("[提示] 没有可备份的内容。")
        print("  outputs/ 和 logs/ 目录均为空或不存在。")
        print("  可使用 --include-db 备份数据库，--all 备份全部。")
        return 1

    # 创建带时间戳的备份目录名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{timestamp}"

    # L2修复：backup_files 已实现于 myquant.utils.file_utils（阶段0内置逻辑移除，薄入口）
    from myquant.utils.file_utils import backup_files

    # 备份内容清单
    total_size = 0
    print(f"备份内容（{len(sources)} 项）：")
    for src, rel_path in sources:
        if src.is_dir():
            size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        else:
            size = src.stat().st_size
        total_size += size
        print(f"  - {rel_path:<30s} ({size / 1024:.1f} KB)")
    print(f"  合计：{total_size / 1024:.1f} KB")
    print()

    log_level = "DEBUG" if args.verbose else "INFO"
    logger = None
    try:
        from myquant.core.logger import setup_logger
        logger = setup_logger(level=log_level, log_dir=project_root / "logs")
    except Exception:
        pass

    backup_dir.mkdir(parents=True, exist_ok=True)
    result = backup_files(
        sources=sources,
        output_dir=backup_dir,
        backup_name=backup_name,
        compress=args.zip,
    )

    if args.keep:
        deleted = cleanup_old_backups(backup_dir, args.keep)
        if deleted > 0:
            print(f"已清理 {deleted} 个旧备份（保留最近 {args.keep} 个）")

    print(f"备份完成：{result['path']}（{result['size'] / 1024:.1f} KB，模式={result['mode']}）")
    if logger is not None:
        logger.info(f"备份完成：{result}")
    return 0


def main() -> int:
    """主入口函数"""
    parser = setup_argument_parser()
    args = parser.parse_args()
    return run_backup(args)


if __name__ == "__main__":
    sys.exit(main())
