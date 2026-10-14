# 文件名：scripts/数据下载.py
"""
数据下载脚本（薄入口）
======================

功能：
    批量下载沪深A股日线行情和财务数据，清洗后存入本地。
    本脚本是薄入口，不包含业务逻辑，只做参数解析、调用myquant内部函数、结果展示。

V5回测性能优化：
    下载完成后自动执行缓存同步（更新数据指纹 + 失效变更标的因子缓存），
    防止使用过期数据的因子计算结果。

使用方法：
    # 交互式菜单（推荐）
    python scripts/数据下载.py

    # 全量下载日线
    python scripts/数据下载.py --daily

    # 增量更新日线（盘后每日运行，约1分钟）
    python scripts/数据下载.py --incremental

    # 下载财务数据
    python scripts/数据下载.py --finance

    # 全部更新（日线增量 + 财务）
    python scripts/数据下载.py --all

    # 数据质量校验
    python scripts/数据下载.py --validate

    # 查看同步状态
    python scripts/数据下载.py --status

    # 跳过缓存同步（仅下载，不更新指纹/缓存）
    python scripts/数据下载.py --daily --no-cache-sync

数据存储结构（严格遵循项目结构图）：
    datasets/
    ├── raw/                    # 原始数据（不可变）
    │   ├── daily/              # 日线原始CSV
    │   └── financial/          # 财务原始CSV
    ├── processed/              # 清洗后数据（Parquet，回测/实盘读取）
    └── features/               # 因子数据（Parquet）
    database/
    └── quant.db                # SQLite数据库（索引和同步状态）

设计原则（4条铁律）：
    1. 以项目结构总图为核心
    2. 为实时行情及实盘无缝对接做好充足准备
    3. 输出完整代码，文件开头标注文件名
    4. 严格禁用未来函数（所有下载严格截止到当前日期）
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# CLI 直跑免设 PYTHONPATH（与 scripts/同步股本变动.py 对齐，#87）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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


# ─────────────────────────────────────────────
# 数据源初始化
# ─────────────────────────────────────────────

def setup_fetcher(project_root: Path):
    """
    创建数据获取器，添加数据源（优先通达信本地，备用akshare网络）

    薄入口：调用 myquant.data_access 内部函数
    """
    from myquant.data_access import DataFetcher
    from myquant.data_access.sources.akshare_source import AkshareDataSource
    from myquant.data_access.sources.tdx_source import TdxDataSource

    fetcher = DataFetcher()

    # 通达信本地源（回测主力，优先级最高）
    tdx = TdxDataSource()
    tdx_status = tdx.health_check()
    if tdx_status.value == "healthy":
        fetcher.add_source(tdx)

    # akshare网络源（财务数据/补充/校验）
    ak = AkshareDataSource()
    fetcher.add_source(ak)

    return fetcher, tdx, ak


def setup_data_manager(fetcher, project_root: Path):
    """
    创建数据管理器

    薄入口：调用 myquant.data_access.DataManager
    """
    from myquant.data_access import DataManager
    from myquant.database import get_connection
    from myquant.database.migrations import migrate_to_latest
    from myquant.database.schema import TABLE_NAMES, table_exists

    db_path = project_root / "database" / "quant.db"
    conn = get_connection(db_path)
    # 防御：数据库为空/被删时自动建表，避免 "no such table: sync_status"
    if not table_exists(conn, TABLE_NAMES["sync_status"]):
        migrate_to_latest(conn)
    manager = DataManager(fetcher, db_connection=conn)
    return manager, conn


# ─────────────────────────────────────────────
# 数据保存（调用 myquant.utils.file_utils）
# ─────────────────────────────────────────────

def save_market_data_parquet(result, project_root: Path) -> Path:
    """
    保存行情数据到 processed 目录（Parquet格式）

    薄入口：调用 myquant.utils.file_utils.write_parquet
    ⚠️ 已废弃（见回写记录#86）：日线 processed 写回改由
    manager.save_cleaned_incremental_parquet 统一清洗写回（14列中文+派生量+合并），
    本函数为原始直写（6列英文、覆盖式），自 #86 起不再被调用。
    """
    from myquant.utils.file_utils import ensure_dir, write_parquet

    processed_dir = ensure_dir(project_root / "datasets" / "processed")
    output_path = processed_dir / f"{result.symbol}.parquet"
    df = result.to_dataframe()
    if not df.empty:
        write_parquet(output_path, df)
    return output_path


def save_financial_data_csv(data_list: List[dict], project_root: Path) -> Path:
    """
    保存财务数据到 raw/financial 目录（CSV格式）

    薄入口：调用 myquant.utils.file_utils.write_csv
    """
    from myquant.utils.file_utils import ensure_dir, write_csv

    fin_dir = ensure_dir(project_root / "datasets" / "raw" / "financial")
    output_path = fin_dir / f"financial_{datetime.now().strftime('%Y%m%d')}.csv"
    if data_list:
        write_csv(output_path, data_list)
    return output_path


# ─────────────────────────────────────────────
# 核心功能函数（薄入口，调用内部函数）
# ─────────────────────────────────────────────

def download_daily(
    fetcher,
    manager,
    project_root: Path,
    incremental: bool = False,
    progress_callback=None,
) -> dict:
    """
    下载日线数据（池内增量定位 + 清洗合并写回 processed）

    薄入口：调用 DataManager.update_symbols_batch 入库 raw 事实版本；
    processed 走 manager.save_cleaned_incremental_parquet（与 GUI 增量更新同一
    清洗链路：14列中文扁平 + 派生量 + 合并写回），根治 CLI 原始直写弄脏数据。
    【防未来函数】DataManager 内部严格截止到当前日期。

    Args:
        fetcher: 数据获取器。
        manager: 数据管理器（写 quant.db + sync_status + processed）。
        project_root: 项目根目录。
        incremental: 兼容保留参数；现统一池内增量定位（force_full=False，
            由 DataManager 依据 sync_status 自动补缺失区间；全量重下走 GUI
            通达信全量/全量清洗）。
        progress_callback: 进度回调 callback(current, total, result)。

    Returns:
        包含 total/success/failed/skipped/parquet_saved/save_failed/
        save_failed_symbols/pool_original/pool_after 的字典。
    """
    from myquant.data_access.full_clean_service import resolve_incremental_symbols
    from myquant.utils.file_utils import ensure_dir

    ensure_dir(project_root / "datasets" / "raw" / "daily")
    ensure_dir(project_root / "datasets" / "processed")

    # 候选池 = 下载源全市场列表（#85 已剔 B 股），经清洗规则实时过滤
    # （14条排除规则 + ST 名单 + 长期停牌，与全量清洗同一规则源；防焊死：
    # 每次运行实时推导，绝不读取历史标的数字）——只下载主板清洗池内标的，
    # 杜绝创业板/科创板/北交所等池外标的进入 processed
    stocks = fetcher.get_stock_list()
    candidates = [s["symbol"] for s in stocks if s.get("symbol")]
    if not candidates:
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                "parquet_saved": 0, "save_failed": 0, "save_failed_symbols": [],
                "pool_original": 0, "pool_after": 0}
    symbols, scope_stats = resolve_incremental_symbols(candidates, project_root)
    if not symbols:
        print("  候选池全被清洗规则排除，空转")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0,
                "parquet_saved": 0, "save_failed": 0, "save_failed_symbols": [],
                "pool_original": len(candidates), "pool_after": 0}
    print(f"  下载范围(实时推导): 候选{len(candidates)} → 规则内{len(symbols)}"
          f"(排除规则{len(scope_stats.get('excluded_prefix', []))}/"
          f"ST{len(scope_stats.get('excluded_st', []))}/"
          f"停牌{len(scope_stats.get('excluded_suspended', []))})")

    # 批量更新（入库 raw 事实版本，复权在消费端现算 S1/S2，严禁 hfq 标签入库）
    results = manager.update_symbols_batch(
        symbols,
        period="daily",
        adjust="raw",
        progress_callback=progress_callback,
    )

    # processed 写回：清洗管线 + 派生量 + 中文列扁平（对齐 GUI 增量更新链路，
    # 根治增量直写弄脏数据）；单标的"拉取→清洗→写 processed"原子隔离，
    # 失败只影响该标的，不影响批次
    success_count = 0
    save_failed = 0
    save_failed_symbols: List[str] = []
    for r in results:
        if r["status"] == "completed" and r.get("record_count", 0) > 0:
            try:
                processed_file = manager.save_cleaned_incremental_parquet(
                    r["symbol"],
                    start_date=r.get("start_date"),
                    end_date=r.get("end_date"),
                    # 复用 update_symbol 已拉记录，消除 save 阶段二次网络请求
                    records=r.get("data"),
                )
            except Exception as e:
                # 异常视为写回失败：单标的计数一次（与"未产出文件"互斥，不双计），不阻断批次
                save_failed += 1
                save_failed_symbols.append(r["symbol"])
                print(f"    增量清洗写 processed 失败 [{r['symbol']}]: {type(e).__name__}: {e}")
                continue
            if processed_file:
                success_count += 1
            else:
                # 清洗写 processed 未产出文件（None）同样视为写入失败并告警，
                # 避免"拉取成功但 processed 未更新"被静默吞掉
                save_failed += 1
                save_failed_symbols.append(r["symbol"])
                print(f"    增量清洗写 processed 未产出文件 [{r['symbol']}]")

    summary = {
        "total": len(results),
        "success": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "parquet_saved": success_count,
        "save_failed": save_failed,
        "save_failed_symbols": save_failed_symbols,
        "pool_original": len(candidates),
        "pool_after": len(symbols),
    }
    return summary


def download_finance(fetcher, project_root: Path, limit: int = None) -> dict:
    """
    下载财务数据（全量A股）

    薄入口：调用 DataFetcher 获取财务数据
    【防未来函数】只获取当前已发布的财报数据

    Parameters
    ----------
    fetcher : DataFetcher
        数据获取器
    project_root : Path
        项目根目录
    limit : int | None
        限制下载数量（None=全量），仅用于测试
    """
    print("  正在获取股票列表...")
    stocks = fetcher.get_stock_list()
    if limit:
        stocks = stocks[:limit]
    symbols = [s["symbol"] for s in stocks if s.get("symbol")]
    print(f"  共 {len(symbols)} 只A股股票，开始下载财务数据...")

    all_financial = []
    success = 0
    failed = 0
    skipped = 0

    for i, symbol in enumerate(symbols, 1):
        try:
            data = fetcher.get_financial_data(symbol)
            if data:
                all_financial.extend(data)
                success += 1
                print(f"    [{i}/{len(symbols)}] {symbol} ✓ ({len(data)}条)")
            else:
                skipped += 1
                print(f"    [{i}/{len(symbols)}] {symbol} ⏭ 无数据")
        except Exception as e:
            failed += 1
            print(f"    [{i}/{len(symbols)}] {symbol} ✗ ({type(e).__name__})")
        time.sleep(0.3)

    if all_financial:
        save_financial_data_csv(all_financial, project_root)

    return {"total": len(symbols), "success": success, "failed": failed, "skipped": skipped, "records": len(all_financial)}


def validate_data(fetcher, manager, project_root: Path) -> List[dict]:
    """
    数据质量校验

    薄入口：调用 DataManager.validate_data
    """

    processed_dir = project_root / "datasets" / "processed"
    if not processed_dir.exists():
        print("  无已下载数据，跳过校验")
        return []

    # 扫描已下载的股票
    parquet_files = list(processed_dir.glob("*.parquet"))
    symbols = [f.stem for f in parquet_files[:20]]  # 抽查20只

    print(f"  抽查 {len(symbols)} 只股票的数据质量...")
    reports = []
    for symbol in symbols:
        report = manager.validate_data(symbol)
        reports.append(report)
        status_icon = "✓" if report["overall_status"] == "healthy" else "⚠"
        print(f"    {status_icon} {symbol}: {report['overall_status']} "
              f"(异常{len(report['price_anomalies']) + len(report['volume_anomalies'])}项)")

    return reports


def show_sync_status(manager) -> dict:
    """
    显示同步状态报告

    薄入口：调用 DataManager.get_sync_report
    """
    return manager.get_sync_report()


def _new_factor_cache_factory(project_root: Path):
    """构造因子缓存工厂（G1 EX-07B：缓存实例由装配层注入，scripts 依赖 L2 合法方向）。"""
    from myquant.indicators.factor_cache import FactorCache

    def _factory():
        return FactorCache(
            cache_dir=str(project_root / "cache" / "factor"),
            enable_l1=True,
        )
    return _factory


def run_cache_sync(project_root: Path, symbols: Optional[List[str]] = None) -> dict:
    """
    数据下载完成后执行缓存同步：更新指纹 + 失效变更标的缓存

    V5回测性能优化：数据变更必须导致缓存失效，防止使用过期因子计算结果。

    Args:
        project_root: 项目根目录。
        symbols: 指定同步的标的列表，None 则全量扫描 processed 目录。

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
            result = sync_manager.sync_symbols(symbols, data_source="download")
        else:
            result = sync_manager.sync_all(data_source="download")

        print(f"\n  [缓存同步] 扫描 {result.total_scanned} 个标的, "
              f"变更 {len(result.changed_symbols)} 个, "
              f"失效缓存 {result.cache_entries_removed} 条, "
              f"耗时 {result.duration_ms:.0f}ms")
        if result.changed_symbols:
            print(f"  [缓存同步] 变更标的: {', '.join(result.changed_symbols[:10])}"
                  f"{'...' if len(result.changed_symbols) > 10 else ''}")
        if result.errors:
            print(f"  [缓存同步] 警告: {len(result.errors)} 个错误")
        return result.to_dict()
    except Exception as e:
        print(f"\n  [缓存同步] 执行失败（不影响下载结果）: {type(e).__name__}: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────
# 精美菜单展示
# ─────────────────────────────────────────────

def show_header():
    """显示顶部标题栏"""
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║           Pythonwdhmm 鲲鹏量化交易系统 — 数据管理中心          ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()


def show_data_source_status(tdx, ak):
    """显示数据源状态"""
    print("┌─ 数据源状态 ──────────────────────────────────────────────────┐")
    # 通达信
    tdx_icon = "✅" if tdx.health_check().value == "healthy" else "❌"
    tdx_path = tdx.tdx_root or "未检测到"
    print(f"│  [通达信本地] {tdx_icon}  路径: {tdx_path:<30s} │")
    # akshare
    ak_icon = "✅" if ak._check_akshare() else "❌"
    print(f"│  [akshare网络] {ak_icon}  财务数据: 支持                        │")
    print("│  [券商实时]    ⏸  未配置（实盘时接入XTP/QMT）                  │")
    print("└────────────────────────────────────────────────────────────────┘")
    print()


def show_data_overview(project_root: Path):
    """显示数据概览"""
    processed_dir = project_root / "datasets" / "processed"
    raw_daily_dir = project_root / "datasets" / "raw" / "daily"
    db_path = project_root / "database" / "quant.db"

    stock_count = len(list(processed_dir.glob("*.parquet"))) if processed_dir.exists() else 0
    raw_count = len(list(raw_daily_dir.glob("*.csv"))) if raw_daily_dir.exists() else 0
    db_size = db_path.stat().st_size if db_path.exists() else 0

    print("┌─ 数据概览 ────────────────────────────────────────────────────┐")
    print(f"│  已下载股票(Parquet): {stock_count:<5d} 只   原始CSV: {raw_count:<5d} 只    │")
    print(f"│  数据库: quant.db  ({db_size / 1024:.1f} KB)                           │")
    print("└────────────────────────────────────────────────────────────────┘")
    print()


def show_menu():
    """显示主菜单"""
    print("┌──────┬─────────────────────────────────────────────────────────┐")
    print("│ 选项 │  功能说明                                               │")
    print("├──────┼─────────────────────────────────────────────────────────┤")
    print("│ [1]  │  全量下载日线数据（首次使用，约30-60分钟）              │")
    print("│ [2]  │  增量更新日线（盘后每日运行，约1-5分钟）                │")
    print("│ [3]  │  下载财务数据（PE/PB/业绩报表）                         │")
    print("│ [4]  │  全部更新（日线增量 + 财务）                            │")
    print("│ [5]  │  数据质量校验（完整性/异常检测）                        │")
    print("│ [6]  │  查看同步状态报告                                      │")
    print("│ [0]  │  退出                                                  │")
    print("└──────┴─────────────────────────────────────────────────────────┘")
    print()
    print("  防未来函数：所有下载严格截止到当前日期，不获取未来数据")
    print("  V5优化：下载完成后自动更新数据指纹并失效变更标的缓存")
    print()


def show_progress(current: int, total: int, result: dict):
    """进度回调函数"""
    if current % 50 == 0 or current == total:
        status = result.get("status", "unknown")
        icon = "✓" if status == "completed" else ("⏭" if status == "skipped" else "✗")
        print(f"    [{current}/{total}] {result.get('symbol', '')} {icon} "
              f"({result.get('record_count', 0)}条)")


# ─────────────────────────────────────────────
# 命令行参数
# ─────────────────────────────────────────────

def setup_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="数据下载",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 数据下载工具（薄入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python scripts/数据下载.py              # 交互式菜单
  python scripts/数据下载.py --daily      # 全量下载日线
  python scripts/数据下载.py --incremental # 增量更新（实盘每日运行）
  python scripts/数据下载.py --finance    # 下载财务数据
  python scripts/数据下载.py --all        # 全部更新
  python scripts/数据下载.py --validate   # 数据质量校验
  python scripts/数据下载.py --status     # 查看同步状态
  python scripts/数据下载.py --daily --no-cache-sync  # 跳过缓存同步
        """,
    )
    parser.add_argument("--daily", action="store_true", help="全量下载日线数据")
    parser.add_argument("--incremental", action="store_true", help="增量更新日线（最近数据）")
    parser.add_argument("--finance", action="store_true", help="下载财务数据")
    parser.add_argument("--all", action="store_true", help="日线增量 + 财务全部更新")
    parser.add_argument("--validate", action="store_true", help="数据质量校验")
    parser.add_argument("--status", action="store_true", help="查看同步状态")
    parser.add_argument("--no-cache-sync", action="store_true",
                        help="跳过下载后的缓存同步（不更新指纹/失效缓存）")
    return parser


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────

def main() -> int:
    project_root = find_project_root()
    parser = setup_argument_parser()
    args = parser.parse_args()

    # 初始化数据源
    show_header()
    print("  正在初始化数据源...")
    fetcher, tdx, ak = setup_fetcher(project_root)
    manager, conn = setup_data_manager(fetcher, project_root)
    print("  数据源初始化完成！")
    print()

    # 命令行模式
    if args.daily or args.incremental or args.finance or args.all or args.validate or args.status:
        if args.daily:
            print("  [任务] 全量下载日线数据")
            result = download_daily(fetcher, manager, project_root, incremental=False,
                                    progress_callback=show_progress)
            print(f"\n  完成：成功{result['success']} | 失败{result['failed']} | 跳过{result['skipped']}"
                  f" | 清洗写回{result['parquet_saved']} | 写回失败{result['save_failed']}")
            if not args.no_cache_sync:
                run_cache_sync(project_root)
        if args.incremental:
            print("  [任务] 增量更新日线数据")
            result = download_daily(fetcher, manager, project_root, incremental=True,
                                    progress_callback=show_progress)
            print(f"\n  完成：成功{result['success']} | 失败{result['failed']} | 跳过{result['skipped']}"
                  f" | 清洗写回{result['parquet_saved']} | 写回失败{result['save_failed']}")
            if not args.no_cache_sync:
                run_cache_sync(project_root)
        if args.finance or args.all:
            print("  [任务] 下载财务数据")
            result = download_finance(fetcher, project_root)
            print(f"\n  完成：成功{result['success']} | 失败{result['failed']} | 跳过{result['skipped']} | 共{result['records']}条")
        if args.validate:
            print("  [任务] 数据质量校验")
            validate_data(fetcher, manager, project_root)
        if args.status:
            print("  [任务] 同步状态报告")
            report = show_sync_status(manager)
            print(f"  总股票数: {report['total_symbols']}")
            print(f"  已完成: {report['completed']} | 失败: {report['failed']} | 待处理: {report['pending']}")
        conn.close()
        return 0

    # 交互式菜单
    while True:
        show_data_source_status(tdx, ak)
        show_data_overview(project_root)
        show_menu()

        try:
            choice = input("  请输入选项 [0-6]：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  已退出")
            conn.close()
            return 0

        print()

        if choice == "0":
            print("  再见！")
            conn.close()
            return 0
        elif choice == "1":
            print("  [任务] 全量下载日线数据")
            print("  这将下载全市场约5000只股票，预计30-60分钟...")
            confirm = input("  确认开始？(y/n): ").strip().lower()
            if confirm == "y":
                result = download_daily(fetcher, manager, project_root, incremental=False,
                                        progress_callback=show_progress)
                print(f"\n  完成：成功{result['success']} | 失败{result['failed']} | 跳过{result['skipped']}"
                      f" | 清洗写回{result['parquet_saved']} | 写回失败{result['save_failed']}")
                run_cache_sync(project_root)
        elif choice == "2":
            print("  [任务] 增量更新日线数据")
            result = download_daily(fetcher, manager, project_root, incremental=True,
                                    progress_callback=show_progress)
            print(f"\n  完成：成功{result['success']} | 失败{result['failed']} | 跳过{result['skipped']}"
                  f" | 清洗写回{result['parquet_saved']} | 写回失败{result['save_failed']}")
            run_cache_sync(project_root)
        elif choice == "3":
            print("  [任务] 下载财务数据")
            result = download_finance(fetcher, project_root)
            print(f"\n  完成：成功{result['success']} | 失败{result['failed']} | 跳过{result['skipped']} | 共{result['records']}条")
        elif choice == "4":
            print("  [任务] 全部更新（日线增量 + 财务）")
            result1 = download_daily(fetcher, manager, project_root, incremental=True,
                                     progress_callback=show_progress)
            print(f"\n  日线完成：成功{result1['success']} | 失败{result1['failed']}"
                  f" | 清洗写回{result1['parquet_saved']} | 写回失败{result1['save_failed']}")
            run_cache_sync(project_root)
            print("\n  开始下载财务数据...")
            result2 = download_finance(fetcher, project_root)
            print(f"\n  财务完成：成功{result2['success']} | 失败{result2['failed']} | 跳过{result2['skipped']} | 共{result2['records']}条")
        elif choice == "5":
            print("  [任务] 数据质量校验")
            validate_data(fetcher, manager, project_root)
        elif choice == "6":
            print("  [任务] 同步状态报告")
            report = show_sync_status(manager)
            print(f"  总股票数: {report['total_symbols']}")
            print(f"  已完成: {report['completed']} | 失败: {report['failed']} | 待处理: {report['pending']}")
        else:
            print("  [错误] 无效选项，请输入 0-6")

        print()
        input("  按回车键返回菜单...")
        print()


if __name__ == "__main__":
    sys.exit(main())
