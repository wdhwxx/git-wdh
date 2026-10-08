# -*- coding: utf-8 -*-
# 文件名：scripts/数据全量清洗.py
"""
数据全量清洗入口（薄入口）
==========================
对照 docs/project_layout_gear2.md：scripts/ 只做入口，业务逻辑全部在
myquant/data_access/full_clean_service.py（FullCleanService）。

本脚本仅负责：命令行参数解析 → 组装配置 → 调用 FullCleanService.run()。

V5回测性能优化：
    全量清洗完成后自动执行缓存同步（更新数据指纹 + 失效变更标的因子缓存），
    防止使用过期数据的因子计算结果。dry-run 模式不执行缓存同步。

核心流程（在 FullCleanService 内）:
  quant.db → DataQueries.get_market_data → run_clean_pipeline（清洗管线）
  → DataQueries 事务写回（delete + bulk_insert）
  → CleanDataQueries 元信息（clean_run_meta / dirty_data_stats）
  → datasets/bad/*.json（脏数据归档，pipeline 自动落盘）
  → 可选重建 datasets/processed/*.parquet

用法:
  python scripts/数据全量清洗.py                              # 默认全量清洗
  python scripts/数据全量清洗.py --config config/data_clean_run.json
  python scripts/数据全量清洗.py --symbols 000001.SZ,600519.SH  # 只清洗指定标的
  python scripts/数据全量清洗.py --dry-run                      # 只体检不写回
  python scripts/数据全量清洗.py --force                        # 忽略断点续传
  python scripts/数据全量清洗.py --workers 4
  python scripts/数据全量清洗.py --no-cache-sync                # 跳过缓存同步
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from myquant.data_access.full_clean_service import (
    DEFAULT_EXCLUDE_RULES,
    FullCleanService,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pythonwdhmm 全量数据清洗（薄入口，业务逻辑见 myquant/data_access/full_clean_service.py）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/数据全量清洗.py                              # 默认全量清洗（GUI 也会生成该配置）
  python scripts/数据全量清洗.py --config config/data_clean_run.json
  python scripts/数据全量清洗.py --symbols 000001.SZ,600519.SH  # 只清洗指定标的
  python scripts/数据全量清洗.py --dry-run                      # 只体检不写回
  python scripts/数据全量清洗.py --force                        # 忽略断点续传全量重跑
  python scripts/数据全量清洗.py --workers 4
  python scripts/数据全量清洗.py --no-cache-sync                # 跳过缓存同步
        """,
    )
    parser.add_argument("--config", "-c", type=str, default=None,
                        help="清洗运行配置 JSON（GUI 生成）")
    parser.add_argument("--symbols", type=str, default=None,
                        help="只清洗指定标的，逗号分隔")
    parser.add_argument("--exclude-rules", type=str, default=None,
                        help="排除规则，逗号分隔（如 '000.SH,399,880,5.SH,1.SZ'），空字符串表示不过滤")
    parser.add_argument("--workers", "-w", type=int, default=None, help="并行进程数")
    parser.add_argument("--dry-run", action="store_true", help="只体检不写回")
    parser.add_argument("--force", action="store_true", help="忽略断点续传")
    parser.add_argument("--no-rebuild", action="store_true", help="清洗后不重建 processed")
    parser.add_argument("--no-backup", action="store_true", help="清洗前不备份数据库")
    parser.add_argument("--db", type=str, default=None,
                        help="指定要清洗的数据库路径（默认 database/quant.db，测试用副本）")
    parser.add_argument("--no-cache-sync", action="store_true",
                        help="跳过清洗后的缓存同步（不更新指纹/失效缓存）")
    return parser.parse_args()


def _new_factor_cache_factory(project_root: Path):
    """构造因子缓存工厂（G1 EX-07B：缓存实例由装配层注入，scripts 依赖 L2 合法方向）。"""
    from myquant.indicators.factor_cache import FactorCache

    def _factory():
        return FactorCache(
            cache_dir=str(project_root / "cache" / "factor"),
            enable_l1=True,
        )
    return _factory


def run_cache_sync(project_root: Path, symbols: list = None) -> dict:
    """
    全量清洗完成后执行缓存同步：更新指纹 + 失效变更标的缓存

    V5回测性能优化：数据变更必须导致缓存失效。
    全量清洗后所有标的数据可能已变更，使用 sync_all 全量扫描。

    Args:
        project_root: 项目根目录。
        symbols: 指定同步的标的列表，None 则全量扫描。

    Returns:
        同步结果字典。
    """
    try:
        from myquant.utils.cache_sync import CacheSyncManager

        sync_manager = CacheSyncManager(
            project_root=project_root,
            factor_cache_factory=_new_factor_cache_factory(project_root),
        )

        if symbols:
            result = sync_manager.sync_symbols(symbols, data_source="full_clean")
        else:
            result = sync_manager.sync_all(data_source="full_clean")

        print(f"\n[缓存同步] 扫描 {result.total_scanned} 个标的, "
              f"变更 {len(result.changed_symbols)} 个, "
              f"失效缓存 {result.cache_entries_removed} 条, "
              f"耗时 {result.duration_ms:.0f}ms")
        if result.changed_symbols:
            print(f"[缓存同步] 变更标的: {', '.join(result.changed_symbols[:10])}"
                  f"{'...' if len(result.changed_symbols) > 10 else ''}")
        if result.errors:
            print(f"[缓存同步] 警告: {len(result.errors)} 个错误")
        return result.to_dict()
    except Exception as e:
        print(f"\n[缓存同步] 执行失败（不影响清洗结果）: {type(e).__name__}: {e}")
        return {"error": str(e)}


def main() -> int:
    args = parse_args()

    # 读取配置（JSON > 命令行参数 > 默认）
    config: dict = {}
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = PROJECT_ROOT / cfg_path
        if cfg_path.exists():
            config = json.loads(cfg_path.read_text(encoding="utf-8"))
        else:
            print(f"[错误] 配置文件不存在: {cfg_path}")
            return 1

    if args.symbols:
        config["symbols"] = args.symbols
    if args.exclude_rules is not None:
        config["exclude_rules"] = [
            s.strip() for s in args.exclude_rules.split(",") if s.strip()
        ]
    if args.workers:
        config["workers"] = args.workers
    if args.no_rebuild:
        config["rebuild_processed"] = False
    if args.no_backup:
        config["backup"] = False
    if args.dry_run:
        config["dry_run"] = True

    config.setdefault("exclude_rules", DEFAULT_EXCLUDE_RULES)
    config.setdefault("rebuild_processed", True)
    config.setdefault("backup", True)
    config.setdefault("workers", 8)
    from datetime import datetime
    config.setdefault("started_at", datetime.now().isoformat(timespec="seconds"))

    is_dry_run = args.dry_run or bool(config.get("dry_run"))

    service = FullCleanService(
        config=config,
        db_path=Path(args.db) if args.db else None,
        dry_run=is_dry_run,
        force=args.force,
    )
    service.run()
    print("\n全量清洗流程结束。")

    # V5优化：清洗完成后自动更新指纹 + 失效缓存
    # dry-run 模式不写回数据，不需要缓存同步
    if not is_dry_run and not args.no_cache_sync:
        # 如果指定了 symbols，只同步这些标的；否则全量同步
        sync_symbols = None
        if args.symbols:
            sync_symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        run_cache_sync(PROJECT_ROOT, symbols=sync_symbols)
    elif is_dry_run:
        print("\n[缓存同步] dry-run 模式，跳过缓存同步")

    return 0


if __name__ == "__main__":
    sys.exit(main())
