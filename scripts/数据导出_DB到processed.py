# -*- coding: utf-8 -*-
# 文件名：scripts/数据导出_DB到processed.py
"""
quant.db → processed 完整导出脚本
====================================
从 quant.db 导出所有标的的 raw 不复权日线数据到 datasets/processed/ 目录。

功能:
  1. 从 quant.db 读取所有有 raw 日线数据的标的
  2. 逐只读取完整日线数据（trade_date, open, high, low, close, volume, amount, pct_chg,
     amplitude, volume_ratio, turnover, float_share, pe_dynamic）
  3. 按日期升序排序，日期保留为「日期」列（扁平式，整数 YYYYMMDD）
  4. 保存为 parquet 文件到 processed 目录
  5. 导出后做完整性校验：每只股票的行数/日期范围对比 DB

设计原则:
  - 逐只查询导出，避免一次性加载2040万行导致内存溢出
  - 导出后自动校验，确保数据完整
  - 支持断点续传（已存在且校验通过的文件跳过）
  - 为实盘无缝对接准备：processed 数据格式与实盘数据提供器一致

使用方式:
  python scripts/数据导出_DB到processed.py
  python scripts/数据导出_DB到processed.py --force  # 强制重新导出所有
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from myquant.data_access.full_clean_service import resolve_incremental_symbols
from myquant.data_access.time_semantics import (
    eff_disclosure_date,
    match_latest_financial,
    next_day,
)
from myquant.utils.data_versioner import (
    DATASET_SCHEMA_REVISIONS,
    DatasetVersion,
    assert_versions_compatible,
)


#: 英文列名 → 中文显示名（导出到 processed 时表头全中文）
#: 下游回测加载器 myquant/backtest/data_loader.py 读取时会自动映射回英文列名，不影响回测
CN_COLUMN_MAP: Dict[str, str] = {
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
    "amount": "成交额",
    "pct_chg": "涨跌幅",
    "amplitude": "振幅",
    "volume_ratio": "量比",
    "float_share": "流通股本",
    "pe_dynamic": "市盈动",
    "turnover": "换手率",
}

def _int_date_to_iso(value: int) -> str:
    """整数日期 YYYYMMDD → ISO 字符串 YYYY-MM-DD（扁平式日期列口径）。

    Args:
        value: 整数日期，如 20260928。

    Returns:
        ISO 格式日期字符串，如 "2026-09-28"。
    """
    return f"{value // 10000:04d}-{value % 10000 // 100:02d}-{value % 100:02d}"


def find_project_root() -> Path:
    """自动探测项目根目录"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        if current == current.parent:
            break
        current = current.parent
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def get_all_symbols(conn: sqlite3.Connection) -> List[str]:
    """从 quant.db 读取所有有 raw 日线数据的标的代码"""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT symbol FROM market_data
        WHERE period='daily' AND adjust='raw'
        ORDER BY symbol
    """)
    return [r[0] for r in cur.fetchall()]


def get_symbol_stats(conn: sqlite3.Connection, symbol: str) -> Tuple[int, str, str]:
    """获取单只标的在 DB 中的统计：行数, 最早日期, 最晚日期"""
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
        FROM market_data
        WHERE symbol=? AND period='daily' AND adjust='raw'
    """, (symbol,))
    row = cur.fetchone()
    return row[0], row[1], row[2]


def load_symbol_data(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    """从 quant.db 读取单只标的的完整 raw 日线数据"""
    query = """
        SELECT trade_date, open, high, low, close, volume, amount, pct_chg, amplitude, volume_ratio, turnover, float_share, pe_dynamic
        FROM market_data
        WHERE symbol=? AND period='daily' AND adjust='raw'
        ORDER BY trade_date ASC
    """
    df = pd.read_sql_query(query, conn, params=(symbol,))
    # 转换日期为 datetime（扁平式：日期以普通列保存，不 set_index）
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    # 确保数值列为 float
    for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'amplitude', 'volume_ratio', 'turnover', 'float_share', 'pe_dynamic']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    # 表头全中文显示（下游加载器自动映射回英文列名，不影响回测）；
    # trade_date 一并映射为「日期」列（扁平式）
    df = df.rename(columns={**CN_COLUMN_MAP, "trade_date": "日期"})
    # KP-P2-10/T-02：前收盘价 = 前一日收盘价（导出 SQL 无该列，统一派生；
    # 防混合/事件引擎涨跌停校验因 pre_close 恒 0 而空转——首行无前驱为 NaN）；
    # 用 insert 紧跟收盘价列，保证列顺序稳定（CI 契约断言依赖）。
    _pre_close = pd.to_numeric(df["收盘价"], errors="coerce").shift(1)
    df.insert(int(df.columns.get_loc("收盘价")) + 1, "前收盘价", _pre_close)
    # 每股收益 = 按交易日匹配最近一期 financial_data.eps。
    # 统一时点日落点（v3.0 §6.4.2，M1A-01 修 ③）：有效披露日 = ann_date 优先
    # → report_deadline 推定 → report_date 兜底，仅取有效披露日 <= 交易日的最
    # 近一期；旧实现按 report_date 匹配会把"披露晚于交易日"的财报提前可见（穿越）。
    fin_cur = conn.cursor()
    fin_cur.execute(
        "SELECT report_date, ann_date, eps FROM financial_data WHERE symbol=? ORDER BY report_date",
        (symbol,),
    )
    fin_rows = [
        dict(zip(("report_date", "ann_date", "eps"), row))
        for row in fin_cur.fetchall()
    ]
    if fin_rows:
        eps_list: List[Optional[float]] = []
        avail_list: List[Optional[str]] = []
        for td in df["日期"].dt.strftime("%Y-%m-%d"):
            matched = match_latest_financial(fin_rows, td)
            eps_list.append((matched or {}).get("eps"))
            # M1A-10（v3.0 §6.4.4）：财务列 available_time = 有效披露日 + 1
            # （披露次日方可消费，禁止与 trade_date 混同）；无可匹配记录时
            # 每股收益不可用 → available_time 置 None（无可用时点）。
            eff = eff_disclosure_date(matched) if matched else ""
            avail_list.append(next_day(eff) if eff else None)
        df["每股收益"] = eps_list
        df["available_time"] = avail_list
    else:
        df["每股收益"] = None
        # 无财务记录：每股收益恒 None，无可用时点
        df["available_time"] = None
    # 扁平式统一：日期列转整数 YYYYMMDD（与增量链路/manager._get_processed_last_date 读取口径一致）
    df["日期"] = df["日期"].dt.strftime("%Y%m%d").astype(int)
    return df


def verify_parquet(parquet_path: Path, db_rows: int, db_start: str, db_end: str) -> Tuple[bool, str]:
    """
    校验 parquet 文件与 DB 数据是否一致。
    返回 (是否通过, 错误信息)
    """
    if not parquet_path.exists():
        return False, "文件不存在"
    try:
        df = pd.read_parquet(parquet_path)
    except Exception as e:
        return False, f"读取失败: {e}"

    if len(df) != db_rows:
        return False, f"行数不匹配: parquet={len(df)}, db={db_rows}"

    # 扁平式：日期从「日期」列读取（整数 YYYYMMDD → YYYY-MM-DD）；
    # 旧索引式文件无「日期」列 → 视为校验失败（触发重建）
    try:
        pq_start = _int_date_to_iso(int(df["日期"].iloc[0]))
        pq_end = _int_date_to_iso(int(df["日期"].iloc[-1]))
    except (KeyError, TypeError, ValueError):
        return False, "日期列缺失或格式异常（旧索引式文件，需重建）"

    if pq_start != db_start:
        return False, f"起始日期不匹配: parquet={pq_start}, db={db_start}"
    if pq_end != db_end:
        return False, f"结束日期不匹配: parquet={pq_end}, db={db_end}"

    return True, "OK"


def export_all(
    db_path: Path,
    output_dir: Path,
    force: bool = False,
    symbols_filter: Optional[List[str]] = None,
) -> Dict:
    """
    完整导出所有标的数据。

    返回统计字典: {total, exported, skipped, failed, duration}
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        all_symbols = get_all_symbols(conn)
        # 清洗规则把关（与全量清洗同一规则源）：processed 只进规则内标的，
        # 范围每次运行时实时推导（防焊死），绝不添加规则外任何一只股票
        all_symbols, scope_stats = resolve_incremental_symbols(all_symbols, find_project_root())
        if symbols_filter:
            all_symbols = [s for s in all_symbols if s in symbols_filter]

        total = len(all_symbols)
        print(f"  DB 全量: {scope_stats['original']} → 规则内: {scope_stats['after']}")
        print(f"=" * 60)
        print(f"  quant.db → processed 完整导出")
        print(f"=" * 60)
        print(f"  数据库: {db_path}")
        print(f"  输出目录: {output_dir}")
        print(f"  标的总数: {total}")
        print(f"  强制重导: {force}")
        print()

        stats = {"total": total, "exported": 0, "skipped": 0, "failed": 0, "errors": []}
        start_time = time.time()

        for i, symbol in enumerate(all_symbols, 1):
            parquet_path = output_dir / f"{symbol}.parquet"

            # 获取 DB 统计
            db_rows, db_start, db_end = get_symbol_stats(conn, symbol)

            # 断点续传：已存在且校验通过则跳过
            if not force and parquet_path.exists():
                ok, msg = verify_parquet(parquet_path, db_rows, db_start, db_end)
                if ok:
                    stats["skipped"] += 1
                    if i % 500 == 0 or i == total:
                        elapsed = time.time() - start_time
                        print(f"  [{i}/{total}] 进度 {i/total*100:.1f}% | "
                              f"已导出={stats['exported']} 跳过={stats['skipped']} "
                              f"失败={stats['failed']} | 耗时={elapsed:.0f}秒")
                    continue

            # 读取并导出
            try:
                df = load_symbol_data(conn, symbol)
                df.to_parquet(parquet_path, index=False)
                stats["exported"] += 1
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append((symbol, str(e)))
                print(f"  ❌ [{i}/{total}] {symbol} 导出失败: {e}")

            # 进度输出
            if i % 500 == 0 or i == total:
                elapsed = time.time() - start_time
                print(f"  [{i}/{total}] 进度 {i/total*100:.1f}% | "
                      f"已导出={stats['exported']} 跳过={stats['skipped']} "
                      f"失败={stats['failed']} | 耗时={elapsed:.0f}秒")

        stats["duration"] = time.time() - start_time

        # M1A-10（v3.0 §6.4.4）：导出批次 sidecar——available_time 逐列规则 +
        # 时点日基口径留痕（财务列=eff_disclosure_date+1，行情列=trade_date）。
        # M1A-11（DEC-06）：schema_revision 从 data_versioner 单一真源读取；
        # 若已存在旧批次 manifest 且 financial_data rev 不同（口径变更后混用
        # 旧批次），由 assert_versions_compatible 阻断（跨口径比较守卫生产接入）。
        manifest = {
            "export_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "available_time_rule": {
                "每股收益": "eff_disclosure_date + 1（披露次日方可消费；v3.0 §6.4.4）",
                "其他行情列": "trade_date（T 日可得）",
            },
            "schema_revision": dict(DATASET_SCHEMA_REVISIONS),
            "stats": {
                "total": stats["total"],
                "exported": stats["exported"],
                "skipped": stats["skipped"],
                "failed": stats["failed"],
            },
        }
        manifest_path = output_dir / "export_manifest.json"
        if manifest_path.exists():
            try:
                old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                old_rev = int((old_manifest.get("schema_revision") or {}).get("financial_data"))
                # 构造快照仅供兼容断言（只看 schema_revision；行数/日期/指纹非导出场景关注点）
                stored = {
                    "financial_data": DatasetVersion(
                        dataset="financial_data", schema_revision=old_rev,
                        row_count=0, min_date="", max_date="", columns_hash="",
                    )
                }
                current = {
                    "financial_data": DatasetVersion(
                        dataset="financial_data",
                        schema_revision=manifest["schema_revision"]["financial_data"],
                        row_count=0, min_date="", max_date="", columns_hash="",
                    )
                }
                assert_versions_compatible(stored, current)
            except (ValueError, KeyError, TypeError) as e:
                # M1A-11：旧 manifest 缺失/异常时仅告警不阻断（首次接入或损坏留痕）
                print(f"  ⚠️ 旧批次 manifest 兼容断言跳过（{e}）")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  批次 sidecar 已写入: {manifest_path}")

        return stats

    finally:
        conn.close()


def verify_all(db_path: Path, output_dir: Path) -> Dict:
    """对所有已导出的 parquet 文件做完整性校验"""
    conn = sqlite3.connect(str(db_path))
    try:
        all_symbols = get_all_symbols(conn)
        total = len(all_symbols)
        passed = 0
        failed = 0
        errors = []

        print(f"\n{'=' * 60}")
        print(f"  完整性校验")
        print(f"{'=' * 60}")

        for i, symbol in enumerate(all_symbols, 1):
            parquet_path = output_dir / f"{symbol}.parquet"
            db_rows, db_start, db_end = get_symbol_stats(conn, symbol)
            ok, msg = verify_parquet(parquet_path, db_rows, db_start, db_end)
            if ok:
                passed += 1
            else:
                failed += 1
                errors.append((symbol, msg))

            if i % 1000 == 0 or i == total:
                print(f"  [{i}/{total}] 通过={passed} 失败={failed}")

        if errors:
            print(f"\n  ❌ 校验失败的标的（前20个）:")
            for symbol, msg in errors[:20]:
                print(f"    {symbol}: {msg}")

        return {"total": total, "passed": passed, "failed": failed, "errors": errors}

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="quant.db → processed 完整导出")
    parser.add_argument("--force", action="store_true", help="强制重新导出所有（不跳过已存在的）")
    parser.add_argument("--verify-only", action="store_true", help="只做完整性校验，不导出")
    args = parser.parse_args()

    project_root = find_project_root()
    db_path = project_root / "database" / "quant.db"
    output_dir = project_root / "datasets" / "processed"

    if not db_path.exists():
        print(f"❌ 数据库不存在: {db_path}")
        sys.exit(1)

    if args.verify_only:
        result = verify_all(db_path, output_dir)
        print(f"\n校验完成: 通过={result['passed']}/{result['total']}, 失败={result['failed']}")
        sys.exit(0 if result['failed'] == 0 else 1)

    # 导出
    stats = export_all(db_path, output_dir, force=args.force)

    print(f"\n{'=' * 60}")
    print(f"  导出完成")
    print(f"{'=' * 60}")
    print(f"  总数: {stats['total']}")
    print(f"  新导出: {stats['exported']}")
    print(f"  跳过(已存在): {stats['skipped']}")
    print(f"  失败: {stats['failed']}")
    print(f"  总耗时: {stats['duration']:.0f} 秒 ({stats['duration']/60:.1f} 分钟)")

    if stats['errors']:
        print(f"\n  ❌ 失败的标的（前10个）:")
        for symbol, err in stats['errors'][:10]:
            print(f"    {symbol}: {err}")

    # 自动校验
    print(f"\n开始完整性校验...")
    verify_result = verify_all(db_path, output_dir)
    print(f"\n校验结果: 通过={verify_result['passed']}/{verify_result['total']}, 失败={verify_result['failed']}")

    if verify_result['failed'] > 0:
        print("\n⚠️  部分标的校验失败，请检查上方错误信息")
        sys.exit(1)
    else:
        print("\n✅ 全部校验通过！")


if __name__ == "__main__":
    main()
