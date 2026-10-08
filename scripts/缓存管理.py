# 文件名：scripts/缓存管理.py
"""
缓存管理运维脚本
================
V5回测性能优化新增：因子缓存与数据指纹的统一运维工具

功能:
    1. 查看缓存状态（L1内存/L2磁盘/指纹仓库/磁盘占用）
    2. 清空所有缓存（L1+L2）
    3. 清理过期缓存文件
    4. 失效指定标的/因子的缓存
    5. 查看指纹仓库（所有标的/单个标的详情）
    6. 手动执行全量缓存同步（更新指纹+失效变更缓存）
    7. 校验指纹与数据一致性

使用方法:
    # 查看缓存状态
    python scripts/缓存管理.py --status

    # 清空所有缓存
    python scripts/缓存管理.py --clear

    # 清理过期缓存
    python scripts/缓存管理.py --cleanup

    # 失效指定标的缓存
    python scripts/缓存管理.py --invalidate 000001.SZ

    # 失效指定因子缓存
    python scripts/缓存管理.py --invalidate-factor rsrs

    # 查看指纹仓库
    python scripts/缓存管理.py --fingerprints

    # 查看指定标的指纹
    python scripts/缓存管理.py --fingerprint 000001.SZ

    # 手动全量同步
    python scripts/缓存管理.py --sync

    # 校验指纹与数据一致性
    python scripts/缓存管理.py --verify

设计原则:
    - 所有操作有明确提示，危险操作（清空）需确认
    - 输出结构化信息，便于运维排查
    - 不修改业务数据，只操作缓存和指纹
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────
# 路径探测
# ─────────────────────────────────────────────

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


def _new_factor_cache_factory(project_root: Path):
    """构造因子缓存工厂（G1 EX-07B：缓存实例由装配层注入，scripts 依赖 L2 合法方向）。"""
    from myquant.indicators.factor_cache import FactorCache

    def _factory():
        return FactorCache(
            cache_dir=str(project_root / "cache" / "factor"),
            enable_l1=True,
        )
    return _factory


# ─────────────────────────────────────────────
# 命令实现
# ─────────────────────────────────────────────

def cmd_status(project_root: Path) -> int:
    """查看缓存状态"""
    from myquant.utils.cache_sync import CacheSyncManager

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    status = manager.get_cache_status()

    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                  鲲鹏量化 — 缓存状态总览                      ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    # 因子缓存
    cache = status["cache"]
    print("┌─ 因子缓存 (FactorCache) ──────────────────────────────────────┐")
    print(f"│  L1 内存缓存:  {'启用' if cache['l1_enabled'] else '禁用':<10s}  "
          f"条目数: {cache['l1_size']:<6d}                     │")
    print(f"│  L2 磁盘缓存:  {'启用' if cache['l2_enabled'] else '禁用':<10s}  "
          f"文件数: {cache['l2_files']:<6d}                     │")
    print(f"│  L1 命中: {cache['l1_hits']:<8d}  L2 命中: {cache['l2_hits']:<8d}  "
          f"L3未命中: {cache['l3_misses']:<8d}      │")
    print(f"│  总请求: {cache['total_requests']:<8d}  "
          f"命中率: {cache['hit_rate']:.2%}  "
          f"失效次数: {cache['invalidate_count']:<6d}  │")
    l2_size_str = f"{status['l2_size_mb']:.2f} MB"
    l2_padding = " " * max(0, 48 - len(l2_size_str))
    print(f"│  L2 磁盘占用: {l2_size_str}{l2_padding}│")
    print(f"│  缓存目录: {cache['cache_dir'][:50]:<50s}│")
    print("└────────────────────────────────────────────────────────────────┘")
    print()

    # 指纹仓库
    fp = status["fingerprint_store"]
    print("┌─ 数据指纹仓库 (FingerprintStore) ────────────────────────────┐")
    print(f"│  仓库路径: {fp['path'][:50]:<50s}│")
    print(f"│  已记录标的: {fp['symbol_count']:<6d}"
          f"{' ' * 42}│")
    if fp["symbols"]:
        preview = ", ".join(fp["symbols"][:8])
        if len(fp["symbols"]) > 8:
            preview += f" ... 等{fp['symbol_count']}个"
        print(f"│  标的列表: {preview[:50]:<50s}│")
    print("└────────────────────────────────────────────────────────────────┘")
    print()

    # 数据目录
    pd_info = status["processed_dir"]
    print("┌─ 数据目录 ────────────────────────────────────────────────────┐")
    print(f"│  路径: {pd_info['path'][:50]:<50s}│")
    print(f"│  存在: {'是' if pd_info['exists'] else '否':<10s}"
          f"{' ' * 42}│")
    print("└────────────────────────────────────────────────────────────────┘")
    print()
    return 0


def cmd_clear(project_root: Path, force: bool = False) -> int:
    """清空所有缓存"""
    from myquant.utils.cache_sync import CacheSyncManager

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    status = manager.get_cache_status()

    print(f"\n  即将清空所有因子缓存:")
    print(f"    L1 内存条目: {status['cache']['l1_size']}")
    print(f"    L2 磁盘文件: {status['cache']['l2_files']} ({status['l2_size_mb']:.2f} MB)")

    if not force:
        confirm = input("\n  确认清空？此操作不可恢复 (y/N): ").strip().lower()
        if confirm != "y":
            print("  已取消")
            return 0

    manager.clear_all_cache()
    print("\n  ✓ 所有缓存已清空")
    return 0


def cmd_cleanup(project_root: Path) -> int:
    """清理过期缓存"""
    from myquant.utils.cache_sync import CacheSyncManager

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    cleaned = manager.cleanup_expired_cache()
    print(f"\n  ✓ 已清理过期缓存文件: {cleaned} 个")
    return 0


def cmd_invalidate(project_root: Path, symbol: str) -> int:
    """失效指定标的缓存"""
    from myquant.utils.cache_sync import CacheSyncManager

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    removed = manager.invalidate_symbol(symbol)
    print(f"\n  ✓ 已失效 {symbol} 的缓存: {removed} 条")
    return 0


def cmd_invalidate_factor(project_root: Path, factor_name: str) -> int:
    """失效指定因子缓存"""
    from myquant.utils.cache_sync import CacheSyncManager

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    removed = manager.invalidate_factor(factor_name)
    print(f"\n  ✓ 已失效因子 {factor_name} 的缓存: {removed} 条")
    return 0


def cmd_fingerprints(project_root: Path) -> int:
    """查看指纹仓库"""
    from myquant.utils.cache_sync import CacheSyncManager

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    status = manager.get_cache_status()
    fp = status["fingerprint_store"]

    print()
    print(f"  指纹仓库: {fp['path']}")
    print(f"  已记录标的: {fp['symbol_count']} 个")
    print()

    if not fp["symbols"]:
        print("  （仓库为空）")
        return 0

    print(f"  {'标的代码':<16s} {'行数':>8s} {'日期范围':<24s} {'数据源':<10s}")
    print("  " + "-" * 62)

    for symbol in fp["symbols"]:
        info = manager.get_fingerprint_info(symbol)
        if info:
            print(f"  {symbol:<16s} {info['row_count']:>8d} "
                  f"{info['date_range']:<24s} {info['data_source']:<10s}")

    print()
    return 0


def cmd_fingerprint(project_root: Path, symbol: str) -> int:
    """查看指定标的指纹"""
    from myquant.utils.cache_sync import CacheSyncManager

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    info = manager.get_fingerprint_info(symbol)

    if info is None:
        print(f"\n  ✗ 未找到 {symbol} 的指纹记录")
        return 1

    print()
    print(f"  标的: {info['symbol']}")
    print(f"  哈希: {info['hash']}")
    print(f"  行数: {info['row_count']}")
    print(f"  日期范围: {info['date_range']}")
    print(f"  列名: {', '.join(info['columns'])}")
    print(f"  数据源: {info['data_source']}")
    print(f"  创建时间: {info['created_at']}")
    print()
    return 0


def cmd_sync(project_root: Path) -> int:
    """手动全量缓存同步"""
    from myquant.utils.cache_sync import CacheSyncManager

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    print("\n  开始全量缓存同步...")
    result = manager.sync_all(data_source="manual")

    print()
    print(result.summary())
    print()
    return 0


def cmd_verify(project_root: Path) -> int:
    """校验指纹与数据一致性"""
    from myquant.utils.cache_sync import CacheSyncManager
    from myquant.utils.data_fingerprint import compute_fingerprint_from_file

    manager = CacheSyncManager(
        project_root=project_root,
        factor_cache_factory=_new_factor_cache_factory(project_root),
    )
    status = manager.get_cache_status()
    symbols = status["fingerprint_store"]["symbols"]

    if not symbols:
        print("\n  指纹仓库为空，无需校验")
        return 0

    print(f"\n  开始校验 {len(symbols)} 个标的的指纹一致性...")
    print()

    matched = 0
    mismatched = 0
    missing = 0

    for symbol in symbols:
        stored_fp = manager.fingerprint_store.get(symbol)
        if stored_fp is None:
            continue

        # 查找数据文件
        filepath = manager._find_symbol_file(symbol)
        if filepath is None:
            print(f"    ⚠ {symbol}: 数据文件不存在")
            missing += 1
            continue

        try:
            current_fp = compute_fingerprint_from_file(
                filepath=filepath,
                symbol=symbol,
                data_source=stored_fp.data_source,
            )
            if stored_fp == current_fp:
                matched += 1
            else:
                mismatched += 1
                print(f"    ✗ {symbol}: 指纹不一致（数据已变更）")
                print(f"        存储: {stored_fp.hash_value[:16]}...")
                print(f"        当前: {current_fp.hash_value[:16]}...")
        except Exception as e:
            print(f"    ⚠ {symbol}: 校验失败 - {e}")
            missing += 1

    print()
    print(f"  校验完成: 一致 {matched}, 不一致 {mismatched}, 缺失/失败 {missing}")
    if mismatched > 0:
        print(f"  建议: 运行 --sync 更新指纹并失效缓存")
    print()
    return 0 if mismatched == 0 else 1


# ─────────────────────────────────────────────
# 命令行参数
# ─────────────────────────────────────────────

def setup_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="缓存管理",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 缓存管理运维工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/缓存管理.py --status              # 查看缓存状态
  python scripts/缓存管理.py --clear               # 清空所有缓存
  python scripts/缓存管理.py --cleanup             # 清理过期缓存
  python scripts/缓存管理.py --invalidate 000001.SZ # 失效指定标的缓存
  python scripts/缓存管理.py --invalidate-factor rsrs  # 失效指定因子缓存
  python scripts/缓存管理.py --fingerprints        # 查看指纹仓库
  python scripts/缓存管理.py --fingerprint 000001.SZ  # 查看指定标的指纹
  python scripts/缓存管理.py --sync                # 手动全量同步
  python scripts/缓存管理.py --verify              # 校验指纹一致性
        """,
    )

    # 状态查询
    parser.add_argument("--status", action="store_true", help="查看缓存状态")

    # 缓存操作
    parser.add_argument("--clear", action="store_true", help="清空所有缓存（需确认）")
    parser.add_argument("--clear-force", action="store_true", help="清空所有缓存（无需确认）")
    parser.add_argument("--cleanup", action="store_true", help="清理过期缓存")
    parser.add_argument("--invalidate", type=str, metavar="SYMBOL",
                        help="失效指定标的的缓存")
    parser.add_argument("--invalidate-factor", type=str, metavar="FACTOR",
                        help="失效指定因子的所有缓存")

    # 指纹操作
    parser.add_argument("--fingerprints", action="store_true", help="查看指纹仓库")
    parser.add_argument("--fingerprint", type=str, metavar="SYMBOL",
                        help="查看指定标的的指纹详情")

    # 同步与校验
    parser.add_argument("--sync", action="store_true", help="手动全量缓存同步")
    parser.add_argument("--verify", action="store_true", help="校验指纹与数据一致性")

    return parser


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────

def main() -> int:
    project_root = find_project_root()
    parser = setup_argument_parser()
    args = parser.parse_args()

    # 如果没有任何参数，显示帮助
    if not any([
        args.status, args.clear, args.clear_force, args.cleanup,
        args.invalidate, args.invalidate_factor,
        args.fingerprints, args.fingerprint,
        args.sync, args.verify,
    ]):
        parser.print_help()
        return 0

    exit_code = 0

    if args.status:
        exit_code = cmd_status(project_root) or exit_code

    if args.clear or args.clear_force:
        exit_code = cmd_clear(project_root, force=args.clear_force) or exit_code

    if args.cleanup:
        exit_code = cmd_cleanup(project_root) or exit_code

    if args.invalidate:
        exit_code = cmd_invalidate(project_root, args.invalidate) or exit_code

    if args.invalidate_factor:
        exit_code = cmd_invalidate_factor(project_root, args.invalidate_factor) or exit_code

    if args.fingerprints:
        exit_code = cmd_fingerprints(project_root) or exit_code

    if args.fingerprint:
        exit_code = cmd_fingerprint(project_root, args.fingerprint) or exit_code

    if args.sync:
        exit_code = cmd_sync(project_root) or exit_code

    if args.verify:
        exit_code = cmd_verify(project_root) or exit_code

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
