# 文件名：scripts/数据清洗.py
"""
数据清洗.py - 数据清洗入口脚本 (折中档位2 v2 - 统一数据源入口)
================================================================
功能:
  1. 从数据源拉取原始行情数据 (写入 datasets/raw/)
  2. 执行清洗流水线 (校验/去重/异常检测)
  3. 输出清洗后数据到 datasets/processed/
  4. 脏数据归档到 datasets/bad/
  5. 生成质量报告到 outputs/

V5回测性能优化：
  清洗完成后自动执行缓存同步（更新数据指纹 + 失效该标的因子缓存），
  防止使用过期数据的因子计算结果。

★★★ 数据源优先级（v2 统一入口）★★★
  --source auto（默认）: 通达信本地源优先，失败自动降级到akshare网络源
  --source tdx         : 仅使用通达信本地源
  --source akshare     : 仅使用akshare网络源（内部多源降级：新浪优先→东方财富）

用法:
  python scripts/数据清洗.py --symbol 000001.SZ --start-date 2024-01-01 --end-date 2024-12-31
  python scripts/数据清洗.py --symbol 600519.SH --data-type bar --config config/data_clean.yaml
  python scripts/数据清洗.py --symbol 000001.SZ --no-save-bad --no-report
  python scripts/数据清洗.py --symbol 000001.SZ --source tdx          # 仅用通达信
  python scripts/数据清洗.py --symbol 000001.SZ --source akshare      # 仅用akshare
  python scripts/数据清洗.py --symbol 000001.SZ --source auto         # 自动降级（默认）
  python scripts/数据清洗.py --symbol 000001.SZ --no-cache-sync       # 跳过缓存同步

依赖: myquant.data_access.factory, myquant.data_access.cleaner
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from myquant.core.logger import setup_logging, get_logger
from myquant.data_access.cleaner import (
    load_clean_config,
    run_clean_pipeline,
)
# M5修复：清洗元信息统一记录（与全量清洗同口径，写 clean_run_meta/dirty_data_stats）
from myquant.data_access.full_clean_service import record_clean_meta
from myquant.data_access.factory import create_fetcher, detect_tdx_available, get_available_sources
from myquant.utils.file_utils import ensure_dir, get_project_path, write_csv, write_json

logger = get_logger("run_data_clean")


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Pythonwdhmm 数据清洗工具 (折中档位2 v2 - 统一数据源入口)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/数据清洗.py --symbol 000001.SZ --start 2024-01-01 --end 2024-12-31
  python scripts/数据清洗.py --symbol 600519.SH --data-type bar --config config/data_clean.yaml
  python scripts/数据清洗.py --symbol 000001.SZ --no-save-bad --no-report --output-format csv
  python scripts/数据清洗.py --symbol 000001.SZ --source auto     (默认，通达信优先→akshare)
  python scripts/数据清洗.py --symbol 000001.SZ --source tdx      (仅通达信)
  python scripts/数据清洗.py --symbol 000001.SZ --source akshare  (仅akshare)
  python scripts/数据清洗.py --symbol 000001.SZ --no-cache-sync   (跳过缓存同步)
        """,
    )

    parser.add_argument(
        "--symbol", "-s",
        type=str,
        required=True,
        help="标的代码，如 000001.SZ / 600519.SH",
    )
    parser.add_argument(
        "--data-type", "-t",
        type=str,
        default="bar",
        choices=["bar", "tick"],
        help="数据类型: bar (K线) / tick (逐笔)，默认 bar",
    )
    parser.add_argument(
        "--start-date", "--start",
        type=str,
        default=None,
        help="开始日期 YYYY-MM-DD，默认取数据源最早日期",
    )
    parser.add_argument(
        "--end-date", "--end",
        type=str,
        default=None,
        help="截止日期 YYYY-MM-DD，默认今天",
    )
    parser.add_argument(
        "--period", "-p",
        type=str,
        default="daily",
        help="K线周期: daily/weekly/monthly/minute，默认 daily",
    )
    parser.add_argument(
        "--adjust",
        type=str,
        default="raw",
        choices=["raw", "hfq", "qfq", "none"],
        help="复权方式: raw(不复权原始层,默认)/hfq(后复权)/qfq(前复权)/none",
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="清洗配置文件路径，默认使用 config/data_clean.yaml 或内置默认",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="auto",
        choices=["auto", "akshare", "tdx"],
        help="数据源: auto(自动降级，通达信优先→akshare) / tdx(仅通达信) / akshare(仅akshare)，默认 auto",
    )
    parser.add_argument(
        "--output-format",
        type=str,
        default="csv",
        choices=["csv", "json", "parquet"],
        help="清洗后数据输出格式，默认 csv",
    )
    parser.add_argument(
        "--no-save-bad",
        action="store_true",
        help="不保存脏数据到 datasets/bad/",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="不生成质量报告",
    )
    parser.add_argument(
        "--no-cache-sync",
        action="store_true",
        help="跳过清洗后的缓存同步（不更新指纹/失效缓存）",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别，默认 INFO",
    )

    return parser.parse_args()


def fetch_raw_data(
    symbol: str,
    data_type: str,
    start_date: Optional[str],
    end_date: Optional[str],
    period: str,
    adjust: str,
    source_mode: str,
) -> tuple[List[Dict[str, Any]], str]:
    """
    从数据源获取原始数据（统一入口，支持自动降级）

    Parameters
    ----------
    source_mode : str
        数据源模式: auto / tdx / akshare

    Returns
    -------
    tuple[List[Dict], str] : (原始数据列表, 实际使用的数据源名称)
    """
    # 使用工厂函数创建数据获取器（统一入口）
    fetcher = create_fetcher(mode=source_mode, enable_cache=False)

    # 打印可用数据源信息
    available = get_available_sources()
    tdx_ok, tdx_path = detect_tdx_available()
    logger.info(f"可用数据源: {available}")
    if tdx_ok:
        logger.info(f"通达信安装路径: {tdx_path}")
    logger.info(f"数据源模式: {source_mode}")

    logger.info(f"正在获取 {symbol} 的 {data_type} 数据...")

    if data_type == "bar":
        result = fetcher.get_market_data(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            period=period,
            adjust=None if adjust == "none" else adjust,
        )
        if result and result.count > 0:
            actual_source = result.source or "unknown"
            logger.info(f"获取到 {result.count} 条原始数据 ({result.start_date} ~ {result.end_date})，数据源: {actual_source}")
            # 统一格式：给每条记录补上 symbol 字段（不同数据源返回格式可能不同）
            records = []
            for rec in result.data:
                if "symbol" not in rec or not rec.get("symbol"):
                    rec = dict(rec)
                    rec["symbol"] = symbol
                records.append(rec)
            return records, actual_source
        else:
            logger.warning(f"未获取到数据: {symbol}（所有数据源均失败）")
            return [], "none"
    else:
        # tick 数据 (v1 暂通过实时接口获取，批量tick需特定数据源支持)
        logger.warning("tick 批量数据获取依赖具体数据源支持，v1 优先支持 bar")
        return [], "none"


def save_processed_data(
    records: List[Dict[str, Any]],
    symbol: str,
    data_type: str,
    output_format: str,
) -> Optional[str]:
    """
    保存清洗后数据到 datasets/processed/

    Returns
    -------
    str | None : 输出文件路径
    """
    if not records:
        logger.warning("无清洗后数据，跳过保存")
        return None

    output_dir = get_project_path("datasets", "processed", symbol.replace(".", "_"))
    ensure_dir(output_dir)

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{data_type}_{timestamp}.{output_format}"
    filepath = output_dir / filename

    try:
        if output_format == "csv":
            write_csv(str(filepath), records)
        elif output_format == "json":
            write_json(str(filepath), records)
        elif output_format == "parquet":
            import pandas as pd
            df = pd.DataFrame(records)
            df.to_parquet(str(filepath), index=False)
        logger.info(f"清洗后数据已保存: {filepath} ({len(records)} 条)")
        return str(filepath)
    except Exception as e:
        logger.error(f"保存清洗后数据失败: {e}")
        return None


def run_cache_sync_for_symbol(symbol: str, project_root: Path) -> dict:
    """
    数据清洗完成后执行缓存同步：更新指纹 + 失效该标的缓存

    V5回测性能优化：数据变更必须导致缓存失效。

    Args:
        symbol: 标的代码。
        project_root: 项目根目录。

    Returns:
        同步结果字典。
    """
    try:
        from myquant.utils.cache_sync import CacheSyncManager

        sync_manager = CacheSyncManager(project_root=project_root)
        result = sync_manager.sync_symbol(symbol, data_source="clean")

        logger.info(
            "[缓存同步] %s: 变更=%s, 失效缓存=%d条, 耗时=%.0fms",
            symbol,
            "是" if result.changed_symbols else "否",
            result.cache_entries_removed,
            result.duration_ms,
        )
        return result.to_dict()
    except Exception as e:
        logger.warning("[缓存同步] 执行失败（不影响清洗结果）: %s: %s", type(e).__name__, e)
        return {"error": str(e)}


def main() -> int:
    """主函数"""
    args = parse_args()

    # 初始化日志
    import logging
    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    setup_logging(level=log_level, log_dir=get_project_path("logs"))

    logger.info("=" * 60)
    logger.info("  Pythonwdhmm 数据清洗工具 (折中档位2 v2 - 统一数据源入口)")
    logger.info("=" * 60)
    logger.info(f"标的: {args.symbol}")
    logger.info(f"数据类型: {args.data_type}")
    logger.info(f"日期范围: {args.start_date or '最早'} ~ {args.end_date or '今天'}")
    logger.info(f"周期: {args.period}, 复权: {args.adjust}")
    logger.info(f"数据源模式: {args.source} (auto=通达信优先→akshare自动降级)")
    logger.info("-" * 60)

    # 1. 加载清洗配置
    clean_config = load_clean_config(config_path=args.config)
    if not clean_config.get("enabled", True):
        logger.warning("清洗功能已在配置中关闭 (enabled=false)，将直接输出原始数据")
    logger.info(f"清洗配置已加载 (enabled={clean_config.get('enabled', True)})")

    # 2. 获取原始数据（统一入口，支持自动降级）
    raw_data, actual_source = fetch_raw_data(
        symbol=args.symbol,
        data_type=args.data_type,
        start_date=args.start_date,
        end_date=args.end_date,
        period=args.period,
        adjust=args.adjust,
        source_mode=args.source,
    )

    if not raw_data:
        logger.error("未获取到任何原始数据，退出")
        return 1

    # 3. 执行清洗流水线
    if clean_config.get("enabled", True):
        result = run_clean_pipeline(
            records=raw_data,
            data_type=args.data_type,
            symbol=args.symbol,
            config=clean_config,
            save_bad=not args.no_save_bad,
            save_report=not args.no_report,
        )
        cleaned = result["cleaned"]
        stats = result["stats"]
        bad_file = result["bad_file"]
        report_file = result["report_file"]
    else:
        # 清洗关闭，直接使用原始数据
        cleaned = raw_data
        stats = {"total": len(raw_data), "valid": len(raw_data), "bad": 0}
        bad_file = None
        report_file = None

    # 4. 保存清洗后数据
    processed_file = save_processed_data(
        records=cleaned,
        symbol=args.symbol,
        data_type=args.data_type,
        output_format=args.output_format,
    )

    # 4.5 M5修复：记录清洗元信息到数据库（与全量清洗链路能力对齐）
    #     增量清洗此前只落盘不落库，现统一记录；DB不可用时仅告警不阻塞。
    try:
        from myquant.database.connection import get_connection
        from myquant.database.queries import CleanDataQueries
        _conn = get_connection()
        _cdq = CleanDataQueries(_conn)
        _run_id = record_clean_meta(
            _cdq,
            symbol=args.symbol,
            data_type=args.data_type,
            total=stats.get("total", len(raw_data)),
            valid=stats.get("valid", len(cleaned)),
            bad_records=result.get("bad", []) if clean_config.get("enabled", True) else [],
            duration_sec=stats.get("duration_sec", 0.0),
            bad_file=bad_file,
            report_file=report_file,
            source="incremental_cli",
        )
        if _run_id:
            logger.info(f"  清洗元信息已入库: run_id={_run_id}")
    except Exception as e:
        logger.warning(f"清洗元信息入库失败（不影响清洗结果）: {e}")

    # 4.6 V5优化：清洗完成后自动更新指纹 + 失效缓存
    if not args.no_cache_sync and processed_file:
        run_cache_sync_for_symbol(args.symbol, PROJECT_ROOT)

    # 5. 输出摘要
    logger.info("-" * 60)
    logger.info("  清洗结果摘要")
    logger.info("-" * 60)
    logger.info(f"  实际数据源: {actual_source}")
    logger.info(f"  原始数据:   {stats.get('total', 0)} 条")
    logger.info(f"  有效数据:   {stats.get('valid', 0)} 条")
    logger.info(f"  脏数据:     {stats.get('bad', 0)} 条")
    logger.info(f"  去重移除:   {stats.get('removed_duplicates', 0)} 条")
    logger.info(f"  价格跳变:   {stats.get('price_jumps', 0)} 次")
    if "volume_anomalies" in stats:
        logger.info(f"  成交量异常: {stats.get('volume_anomalies', 0)} 次")
    if "time_violations" in stats:
        logger.info(f"  时间违规:   {stats.get('time_violations', 0)} 次")
    logger.info(f"  耗时:       {stats.get('duration_sec', 0)} 秒")
    if processed_file:
        logger.info(f"  清洗数据:   {processed_file}")
    if bad_file:
        logger.info(f"  脏数据归档: {bad_file}")
    if report_file:
        logger.info(f"  质量报告:   {report_file}")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
