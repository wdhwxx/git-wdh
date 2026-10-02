# 文件名：scripts/同步交易日历.py
"""交易日历同步薄入口（P2-① #83）：拉取 akshare 全历史交易日 → data/trade_calendar.csv。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from myquant.utils.trade_calendar import TradeCalendarError, load_trade_calendar, sync_trade_calendar


def main() -> int:
    parser = argparse.ArgumentParser(description="同步A股真实交易日历到 data/trade_calendar.csv")
    parser.add_argument("--force", action="store_true", help="强制重新拉取（默认有缓存则复用）")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    if not args.force:
        cached = load_trade_calendar(project_root)
        if cached:
            print(f"已有缓存 {len(cached)} 个交易日，使用 --force 强制刷新")
            return 0
    try:
        count = sync_trade_calendar(project_root, force=args.force)
    except TradeCalendarError as exc:
        print(f"交易日历同步失败: {exc}", file=sys.stderr)
        return 1
    print(f"交易日历同步完成: {count} 个交易日 → data/trade_calendar.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
