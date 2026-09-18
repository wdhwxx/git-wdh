# -*- coding: utf-8 -*-
# 文件名：scripts/数据导出_DB到processed.py
"""
quant.db → processed 完整导出脚本
====================================
从 quant.db 导出所有标的的 hfq 后复权日线数据到 datasets/processed/ 目录。

功能:
  1. 从 quant.db 读取所有有 hfq 日线数据的标的
  2. 逐只读取完整日线数据（trade_date, open, high, low, close, volume, amount, pct_chg）
  3. 按日期升序排序，设置日期为索引
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
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


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
    """从 quant.db 读取所有有 hfq 日线数据的标的代码"""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT symbol FROM market_data
        WHERE period='daily' AND adjust='hfq'
        ORDER BY symbol
    """)
    return [r[0] for r in cur.fetchall()]


def get_symbol_stats(conn: sqlite3.Connection, symbol: str) -> Tuple[int, str, str]:
    """获取单只标的在 DB 中的统计：行数, 最早日期, 最晚日期"""
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
        FROM market_data
        WHERE symbol=? AND period='daily' AND adjust='hfq'
    """, (symbol,))
    row = cur.fetchone()
    return row[0], row[1], row[2]


def load_symbol_data(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    """从 quant.db 读取单只标的的完整 hfq 日线数据"""
    query = """
        SELECT trade_date, open, high, low, close, volume, amount, pct_chg
        FROM market_data
        WHERE symbol=? AND period='daily' AND adjust='hfq'
        ORDER BY trade_date ASC
    """
    df = pd.read_sql_query(query, conn, params=(symbol,))
    # 转换日期为 datetime，设置为索引
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df.set_index('trade_date')
    df.index.name = 'date'
    # 确保数值列为 float
    for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
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

    pq_start = df.index[0].strftime("%Y-%m-%d") if hasattr(df.index[0], "strftime") else str(df.index[0])
    pq_end = df.index[-1].strftime("%Y-%m-%d") if hasattr(df.index[-1], "strftime") else str(df.index[-1])

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
        if symbols_filter:
            all_symbols = [s for s in all_symbols if s in symbols_filter]

        total = len(all_symbols)
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
                df.to_parquet(parquet_path, index=True)
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
