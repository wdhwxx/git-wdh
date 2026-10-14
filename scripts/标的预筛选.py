# -*- coding: utf-8 -*-
# 文件名：scripts/标的预筛选.py
"""
标的预筛选脚本
================
从 quant.db 全市场标的中，按基础条件筛选出"可交易标的池"，
生成标的清单 yaml，供回测配置直接引用。

筛选维度（v1）:
  1. 代码前缀过滤（剔除创业板/科创板/北交所/老三板等）
  2. 上市时间过滤（上市满 N 年，剔除次新股）
  3. 流动性过滤 — 日均成交额 > 阈值（剔除小盘僵尸股）
  4. 流动性过滤 — 日均换手率 > 阈值（剔除不活跃股）

设计原则（规则2：为实盘无缝对接准备）:
  - 筛选逻辑独立于回测引擎，回测/实盘共用同一套筛选条件
  - 筛选结果带条件快照，可追溯（知道当时用了什么条件筛的）
  - 只读取 quant.db，不修改原始数据
  - 筛选只算一次，结果可复用（多次回测/参数优化用同一个池子）
  - 批量 SQL 查询，全市场 9343 只只需 3 次查询，秒级完成

防未来函数（规则4）:
  - 流动性指标（日均成交额/换手率）按回测开始日前的历史窗口计算
    默认用回测开始日前 365 天的数据，避免使用回测期内的未来信息
  - 上市时间用数据中最早交易日判断，不依赖外部不确定的上市日期

使用方式:
  python scripts/标的预筛选.py --start 2020-01-01 --end 2025-12-31 \
      --min-amount 50000000 --min-turnover 1.0 --min-list-years 1 \
      --exclude-prefixes 30,688,4,8,92 \
      --output config/stock_pool.yaml
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

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
# 批量数据读取（全市场一次查询，秒级完成）
# ─────────────────────────────────────────────

def load_all_symbols_with_dates(
    conn: sqlite3.Connection,
) -> Dict[str, Tuple[str, str, int]]:
    """
    批量读取所有有日线后复权数据的标的及其日期范围。
    返回 {symbol: (最早日期, 最晚日期, 行数)}
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, MIN(trade_date), MAX(trade_date), COUNT(*)
        FROM market_data
        WHERE period='daily' AND adjust='raw'
        GROUP BY symbol
        ORDER BY symbol
    """)
    result = {}
    for row in cur.fetchall():
        if row[1] is not None:
            result[row[0]] = (row[1], row[2], row[3])
    return result


def load_liquidity_batch(
    conn: sqlite3.Connection,
    calc_start: str,
    calc_end: str,
) -> Tuple[Dict[str, Tuple[float, Optional[float], int]], bool]:
    """
    批量计算所有标的在指定时间段内的流动性统计。
    返回 ({symbol: (日均成交额, 日均换手率或None, 有效交易日数)}, turnover是否可用)

    只计算 calc_start ~ calc_end 之间的数据（回测开始前的历史窗口），
    严格防未来函数：不使用回测期内的数据。

    注意：若数据库 turnover 字段全为空，则换手率返回 None，
    调用方应跳过换手率过滤并给出警告。
    """
    cur = conn.cursor()
    # 先检查 turnover 字段是否有数据
    cur.execute("""
        SELECT COUNT(*) FROM market_data
        WHERE period='daily' AND adjust='raw'
          AND trade_date >= ? AND trade_date <= ?
          AND turnover IS NOT NULL AND turnover >= 0
    """, (calc_start, calc_end))
    turnover_available = cur.fetchone()[0] > 0

    if turnover_available:
        cur.execute("""
            SELECT symbol, AVG(amount), AVG(turnover), COUNT(*)
            FROM market_data
            WHERE period='daily' AND adjust='raw'
              AND trade_date >= ? AND trade_date <= ?
              AND amount > 0 AND turnover >= 0
            GROUP BY symbol
        """, (calc_start, calc_end))
    else:
        # turnover 不可用时，只按 amount 过滤
        cur.execute("""
            SELECT symbol, AVG(amount), COUNT(*)
            FROM market_data
            WHERE period='daily' AND adjust='raw'
              AND trade_date >= ? AND trade_date <= ?
              AND amount > 0
            GROUP BY symbol
        """, (calc_start, calc_end))

    result = {}
    for row in cur.fetchall():
        if row[1] is not None and row[-1] > 0:
            if turnover_available:
                result[row[0]] = (float(row[1]), float(row[2]), int(row[3]))
            else:
                result[row[0]] = (float(row[1]), None, int(row[2]))
    return result, turnover_available


# ─────────────────────────────────────────────
# 筛选逻辑
# ─────────────────────────────────────────────

def filter_by_prefix(
    symbols: List[str],
    exclude_prefixes: List[str],
) -> List[str]:
    """按代码前缀过滤"""
    if not exclude_prefixes:
        return symbols
    result = []
    for sym in symbols:
        code = sym.split('.')[0] if '.' in sym else sym
        if any(code.startswith(p) for p in exclude_prefixes):
            continue
        result.append(sym)
    return result


def filter_by_listing_age(
    symbols: List[str],
    date_map: Dict[str, Tuple[str, str, int]],
    min_list_years: float,
    backtest_start: str,
) -> Tuple[List[str], Dict[str, str]]:
    """
    按上市时间过滤：上市满 min_list_years 年。
    用数据中最早交易日近似上市日期，判断到回测开始日是否满 N 年。

    返回 (通过的标的列表, 被剔除的原因字典)
    """
    if min_list_years <= 0:
        return symbols, {}

    passed = []
    rejected = {}
    start_dt = datetime.strptime(backtest_start, "%Y-%m-%d")
    min_days = int(min_list_years * 365)

    for sym in symbols:
        info = date_map.get(sym)
        if info is None:
            rejected[sym] = "无日线数据"
            continue
        earliest = info[0]  # YYYY-MM-DD
        earliest_dt = datetime.strptime(earliest, "%Y-%m-%d")
        days_listed = (start_dt - earliest_dt).days
        if days_listed >= min_days:
            passed.append(sym)
        else:
            rejected[sym] = f"上市仅 {days_listed} 天 (< {min_days} 天)"

    return passed, rejected


def filter_by_liquidity(
    symbols: List[str],
    liquidity_map: Dict[str, Tuple[float, Optional[float], int]],
    min_avg_amount: float,
    min_avg_turnover: float,
    min_valid_days: int = 60,
    turnover_available: bool = True,
) -> Tuple[List[str], Dict[str, str]]:
    """
    按流动性过滤：日均成交额 >= min_avg_amount 且 日均换手率 >= min_avg_turnover。

    用回测开始前的历史窗口计算，严格防未来函数。
    有效交易日数不足 min_valid_days 的标的也剔除（数据不充分）。

    若 turnover_available=False（数据库 turnover 字段全为空），
    则跳过换手率过滤，只按日均成交额过滤。

    返回 (通过的标的列表, 被剔除的原因字典)
    """
    if min_avg_amount <= 0 and (min_avg_turnover <= 0 or not turnover_available):
        return symbols, {}

    passed = []
    rejected = {}

    for sym in symbols:
        stats = liquidity_map.get(sym)
        if stats is None:
            rejected[sym] = "流动性窗口内无有效数据"
            continue
        avg_amount, avg_turnover, valid_days = stats
        if valid_days < min_valid_days:
            rejected[sym] = f"有效交易日仅 {valid_days} 天 (< {min_valid_days})"
            continue
        if avg_amount < min_avg_amount:
            rejected[sym] = f"日均成交额 {avg_amount/1e4:.0f}万 (< {min_avg_amount/1e4:.0f}万)"
            continue
        if turnover_available and min_avg_turnover > 0:
            if avg_turnover is None or avg_turnover < min_avg_turnover:
                to_str = f"{avg_turnover:.2f}%" if avg_turnover is not None else "无数据"
                rejected[sym] = f"日均换手率 {to_str} (< {min_avg_turnover:.2f}%)"
                continue
        passed.append(sym)

    return passed, rejected


# ─────────────────────────────────────────────
# 结果输出
# ─────────────────────────────────────────────

def build_filter_result(
    *,
    total_symbols: int,
    after_prefix: int,
    after_listing: int,
    after_liquidity: int,
    final_codes: List[str],
    conditions: Dict,
    rejected_listing: Dict[str, str],
    rejected_liquidity: Dict[str, str],
) -> Dict:
    """构建筛选结果字典（含条件快照，可追溯）"""
    return {
        "filter_meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_in_db": total_symbols,
            "after_prefix_filter": after_prefix,
            "after_listing_filter": after_listing,
            "after_liquidity_filter": after_liquidity,
            "final_count": len(final_codes),
        },
        "filter_conditions": conditions,
        "stock_pool": {
            "type": "preset_filtered",
            "description": "由 scripts/标的预筛选.py 生成的可交易标的池",
            "codes": final_codes,
        },
        "rejected_detail": {
            "listing_age": rejected_listing,
            "liquidity": rejected_liquidity,
        },
    }


def save_result(result: Dict, output_path: Path) -> None:
    """保存筛选结果为 yaml"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(result, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def run_filter(args: argparse.Namespace) -> Dict:
    """执行完整筛选流程"""
    project_root = find_project_root()
    db_path = project_root / "database" / "quant.db"

    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    # 解析排除前缀
    exclude_prefixes = [
        p.strip() for p in args.exclude_prefixes.split(",") if p.strip()
    ] if args.exclude_prefixes else []

    # 计算流动性统计窗口：回测开始日前 lookback_days 天
    start_dt = datetime.strptime(args.start, "%Y-%m-%d")
    calc_end = (start_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    calc_start = (start_dt - timedelta(days=args.liquidity_lookback_days)).strftime("%Y-%m-%d")

    conditions = {
        "backtest_start": args.start,
        "backtest_end": args.end,
        "exclude_prefixes": exclude_prefixes,
        "min_list_years": args.min_list_years,
        "min_avg_amount": args.min_avg_amount,
        "min_avg_turnover": args.min_avg_turnover,
        "turnover_data_available": None,  # 运行时填充
        "liquidity_window": {
            "start": calc_start,
            "end": calc_end,
            "lookback_days": args.liquidity_lookback_days,
            "min_valid_days": args.min_valid_days,
        },
    }

    print("=" * 60)
    print("  标的预筛选")
    print("=" * 60)
    print(f"  数据库: {db_path}")
    print(f"  回测期间: {args.start} ~ {args.end}")
    print(f"  流动性统计窗口: {calc_start} ~ {calc_end} (回测前 {args.liquidity_lookback_days} 天)")
    print(f"  排除前缀: {exclude_prefixes or '无'}")
    print(f"  上市满 {args.min_list_years} 年")
    print(f"  日均成交额 >= {args.min_avg_amount/1e4:.0f} 万")
    print(f"  日均换手率 >= {args.min_avg_turnover:.2f}%")
    print()

    # 建立单一数据库连接，所有查询共用
    conn = sqlite3.connect(str(db_path))
    try:
        # 步骤1：批量加载全量标的及日期范围（1次查询）
        print("[1/4] 批量加载全市场标的及日期范围...")
        date_map = load_all_symbols_with_dates(conn)
        all_symbols = sorted(date_map.keys())
        print(f"       全市场标的数: {len(all_symbols)}")

        # 步骤2：代码前缀过滤（纯内存，无查询）
        after_prefix = filter_by_prefix(all_symbols, exclude_prefixes)
        print(f"[2/4] 前缀过滤后: {len(after_prefix)} (剔除 {len(all_symbols) - len(after_prefix)})")

        # 步骤3：上市时间过滤（纯内存，用已加载的 date_map）
        after_listing, rejected_listing = filter_by_listing_age(
            after_prefix, date_map, args.min_list_years, args.start,
        )
        print(f"[3/4] 上市时间过滤后: {len(after_listing)} (剔除 {len(after_prefix) - len(after_listing)})")

        # 步骤4：批量加载流动性统计（1次查询）+ 过滤
        print(f"[4/4] 批量计算流动性统计（窗口 {calc_start} ~ {calc_end}）...")
        liquidity_map, turnover_available = load_liquidity_batch(conn, calc_start, calc_end)
        conditions["turnover_data_available"] = turnover_available
        print(f"       有流动性数据的标的: {len(liquidity_map)}")
        if not turnover_available:
            print("       ⚠️  警告: 数据库 turnover 字段全为空，换手率过滤已自动跳过")
            print("           （如需换手率过滤，请先运行数据下载脚本补全 turnover 字段）")
        after_liquidity, rejected_liquidity = filter_by_liquidity(
            after_listing, liquidity_map,
            args.min_avg_amount, args.min_avg_turnover, args.min_valid_days,
            turnover_available=turnover_available,
        )
        print(f"       流动性过滤后: {len(after_liquidity)} (剔除 {len(after_listing) - len(after_liquidity)})")
    finally:
        conn.close()

    # 构建结果
    result = build_filter_result(
        total_symbols=len(all_symbols),
        after_prefix=len(after_prefix),
        after_listing=len(after_listing),
        after_liquidity=len(after_liquidity),
        final_codes=sorted(after_liquidity),
        conditions=conditions,
        rejected_listing=rejected_listing,
        rejected_liquidity=rejected_liquidity,
    )

    # 保存
    output_path = Path(args.output) if args.output else project_root / "config" / "stock_pool.yaml"
    if not output_path.is_absolute():
        output_path = project_root / output_path
    save_result(result, output_path)

    print()
    print("=" * 60)
    print(f"  筛选完成: 最终标的 {len(after_liquidity)} 只")
    print(f"  结果已保存: {output_path}")
    print("=" * 60)

    return result


# ─────────────────────────────────────────────
# 命令行入口
# ─────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="标的预筛选：从 quant.db 全市场中筛选可交易标的池",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start", required=True, help="回测开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="回测结束日期 (YYYY-MM-DD)")
    parser.add_argument(
        "--exclude-prefixes", default="30,688,4,8,92",
        help="要排除的代码前缀，逗号分隔 (默认: 30,688,4,8,92)",
    )
    parser.add_argument(
        "--min-list-years", type=float, default=1.0,
        help="上市满多少年 (默认: 1.0)",
    )
    parser.add_argument(
        "--min-avg-amount", type=float, default=50_000_000,
        help="日均成交额下限（元，默认: 50000000 = 5000万）",
    )
    parser.add_argument(
        "--min-avg-turnover", type=float, default=1.0,
        help="日均换手率下限（%%，默认: 1.0）",
    )
    parser.add_argument(
        "--liquidity-lookback-days", type=int, default=365,
        help="流动性统计回看天数（回测开始前，默认: 365）",
    )
    parser.add_argument(
        "--min-valid-days", type=int, default=60,
        help="流动性窗口内最少有效交易日数 (默认: 60)",
    )
    parser.add_argument(
        "--output", default="config/stock_pool.yaml",
        help="输出文件路径 (默认: config/stock_pool.yaml)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        run_filter(args)
    except Exception as e:
        print(f"\n❌ 筛选失败: {e}", file=sys.stderr)
        sys.exit(1)
