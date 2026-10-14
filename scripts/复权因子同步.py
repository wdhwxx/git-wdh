# 文件名：scripts/复权因子同步.py
"""
复权因子同步 - 薄入口（方案B #82）
====================================
业务逻辑在 myquant/data_access/factor_sync_service.py（按治理规则归 myquant）。

用法::

  python scripts/复权因子同步.py --all --sleep 0.3            # 全市场 5317 只（≈26 分钟）
  python scripts/复权因子同步.py --symbols 600519.SH,000001.SZ # 指定标的
  python scripts/复权因子同步.py --all --limit 3 --dry-run     # 只拉不写（连通性测试）
  python scripts/复权因子同步.py --all --limit 5               # 限速测试（前 N 只入库）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myquant.data_access.factor_sync_service import (  # noqa: E402
    DEFAULT_SLEEP,
    FactorSyncService,
)
from myquant.database import get_connection  # noqa: E402


def _load_all_symbols(db_path: Path) -> list[str]:
    conn = get_connection(db_path)
    rows = conn.execute("SELECT DISTINCT symbol FROM market_data ORDER BY symbol")
    return [r["symbol"] for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="复权因子同步（方案B #82）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="全市场标的（DB DISTINCT symbol）")
    group.add_argument("--symbols", help="逗号分隔标的，如 600519.SH,000001.SZ")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help=f"拉取间隔秒（默认 {DEFAULT_SLEEP}）")
    parser.add_argument("--limit", type=int, default=0, help="限速测试：只处理前 N 只（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只拉取清洗不写库")
    args = parser.parse_args()

    project_root = Path(r"D:\Projects\Pythonwdhmm")
    db_path = project_root / "database" / "quant.db"
    svc = FactorSyncService(project_root, db_path=db_path)

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else _load_all_symbols(db_path))
    if args.limit > 0:
        symbols = symbols[: args.limit]
    print(f"待同步标的: {len(symbols)} 只（sleep={args.sleep}s dry_run={args.dry_run}）")

    if args.dry_run:
        ok = no_events = fail = 0
        for i, sym in enumerate(symbols, 1):
            events = svc.fetch_factor_events(sym)
            if events is None:
                fail += 1
                print(f"  [{i}] {sym}: FAIL")
            elif not events:
                no_events += 1
                print(f"  [{i}] {sym}: no_events")
            else:
                ok += 1
                print(f"  [{i}] {sym}: {len(events)} 事件, 最近={events[-1][0]} factor={events[-1][1]:.6f}")
        print(f"DRY_RUN 汇总: ok={ok} no_events={no_events} fail={fail}")
        return 0 if fail == 0 else 1

    stats = svc.sync_market(symbols, sleep=args.sleep)
    print("同步汇总:")
    for k in ("total", "ok", "no_events", "fail", "events_upserted", "elapsed_sec"):
        print(f"  {k}: {stats[k]}")
    if stats["failed_symbols"]:
        print(f"  失败标的 ({len(stats['failed_symbols'])}):")
        for s in stats["failed_symbols"]:
            print(f"    {s}")
    return 0 if stats["fail"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
