# 文件名：scripts/同步股本变动.py
"""股本变动同步薄入口（P1-② #83）：拉取 akshare 东财股本变动 → share_change 表。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myquant.data_access import DataFetcher
from myquant.data_access.share_change_sync_service import ShareChangeSyncService
from myquant.data_access.sources.akshare_source import AkshareDataSource
from myquant.data_access.sources.tdx_source import TdxDataSource


def _setup_fetcher(project_root: Path):
    """构造 fetcher（优先通达信本地，备用 akshare 网络；同 数据下载.py 口径）。"""
    fetcher = DataFetcher()
    tdx = TdxDataSource()
    if tdx.health_check().value == "healthy":
        fetcher.add_source(tdx)
    fetcher.add_source(AkshareDataSource())
    return fetcher


def main() -> int:
    parser = argparse.ArgumentParser(description="同步A股真实股本变动到 share_change 表（UPSERT 幂等可续跑）")
    parser.add_argument("--limit", type=int, default=0, help="只同步前 N 只（默认全市场）")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    fetcher = _setup_fetcher(project_root)
    stocks = fetcher.get_stock_list()
    symbols = [s.get("symbol") for s in stocks if s.get("symbol")]
    if args.limit > 0:
        symbols = symbols[: args.limit]
    print(f"待同步标的 {len(symbols)} 只（limit={args.limit or '全市场'}）")

    svc = ShareChangeSyncService(project_root)
    result = svc.sync_market(symbols, sleep=0.3)
    print(
        f"股本同步完成: total={result['total']} ok={result['ok']} "
        f"no_events={result['no_events']} fail={result['fail']} "
        f"事件={result['events_upserted']} 耗时={result['elapsed_sec']}s"
    )
    if result["failed_symbols"]:
        print(f"失败标的({len(result['failed_symbols'])}): {result['failed_symbols'][:20]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
