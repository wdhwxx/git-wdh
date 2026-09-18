# -*- coding: utf-8 -*-
"""
鲲鹏策略参数网格扫描（真实数据，vectorized 模式，两阶段）
====================================================
阶段A：信号参数扫描（出场 fixed=standard，仓位 fixed=默认）
阶段B：出场+仓位参数扫描（信号参数 fixed=当前默认）

评估口径：多标的批量回测 → 聚合平均夏普 / 平均收益 / 平均回撤 / 信号标的数。
数据：datasets/processed 真实行情（hfq），区间 2024-01-01 起至数据末端。
标的池：此前全池回测中成交 >= 3 笔的活跃标的 top N。

用法（quant 环境）:
  D:\\ProgramData\\anaconda3\\envs\\quant\\python.exe scripts\\kunpeng_param_scan.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(r"D:\Projects\Pythonwdhmm")
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.backtest.batch_backtest import BatchBacktester  # noqa: E402

PROCESSED = r"D:\Projects\Pythonwdhmm\datasets\processed"
RESULTS_JSON = PROJECT_ROOT / "results" / "batch_backtest" / "kunpeng" / "results.json"
SCAN_CACHE = PROJECT_ROOT / "cache" / "param_scan"
OUT_JSON = PROJECT_ROOT / "results" / "param_scan" / "scan_results.json"

START_DATE = "2024-01-01"
END_DATE = None  # 与批量回测一致：至数据末端
INITIAL_CAPITAL = 1_000_000.0

# ── 默认参数（与 config/backtest_kunpeng.yaml strategy.params 一致）──
BASE_PARAMS = {
    "vol_ratio_threshold": 4.5,
    "vol_count_window": 6,
    "vol_active_days": 5,
    "barslast_vol_window": 13,
    "barslast_ratio_window": 15,
    "ma_sticky_threshold": 1.22,
    "diff_dea_ratio": 0.018,
    "macd_abs_threshold": 0.10,
    "macd_rise_threshold": 0.0001,
    "exit_mode": "standard",
    "hold_days_cap": 60,
    "max_position_pct": 0.20,
    "max_positions": 5,
}

# ── 阶段A：信号参数网格 ──
SIGNAL_GRID = {
    "vol_ratio_threshold": (3.5, 4.5, 5.5),
    "vol_active_days": (4, 5, 6),
    "ma_sticky_threshold": (1.15, 1.22, 1.30),
    "diff_dea_ratio": (0.012, 0.018, 0.025),
    "macd_rise_threshold": (0.0001, 0.005),
}

# ── 阶段B：出场+仓位参数网格 ──
EXIT_GRID = {
    "exit_mode": ("conservative", "standard", "aggressive"),
    "hold_days_cap": (30, 60, 90),
    "max_position_pct": (0.10, 0.20, 0.30),
    "max_positions": (3, 5, 8),
}


def load_active_symbols(top_n: int = 50, min_trades: int = 3) -> list[str]:
    """从已有全池结果中挑选活跃标的（成交笔数 >= min_trades，取 top_n）。"""
    with open(RESULTS_JSON, "r", encoding="utf-8") as f:
        d = json.load(f)
    rows = [
        (s, v.get("trade_count") or 0)
        for s, v in d["results"].items()
        if v.get("status") == "completed" and (v.get("trade_count") or 0) >= min_trades
    ]
    rows.sort(key=lambda x: -x[1])
    return [s for s, _ in rows[:top_n]]


def load_data(symbols: list[str]) -> dict:
    """加载真实行情（HistoricalDataProvider，hfq）。"""
    from myquant.backtest.data_loader import HistoricalDataProvider

    provider = HistoricalDataProvider(processed_dir=PROCESSED, default_adjust="hfq", data_format="parquet")
    return provider.load_multiple(symbols, start_date=START_DATE, end_date=END_DATE, adjust="hfq")


def run_one(combo: dict, data: dict, tag: str) -> dict:
    """跑一个参数组合的批量回测（vectorized），返回聚合指标。"""
    params = dict(BASE_PARAMS)
    params.update(combo)
    bt = BatchBacktester(
        strategy_name="kunpeng",
        params=params,
        data=data,
        fingerprint_path=str(SCAN_CACHE / f"fp_{tag}.json"),
        results_path=str(SCAN_CACHE / f"res_{tag}.json"),
        mode="vectorized",
        cpu_workers=8,
        start_date=START_DATE,
        end_date=END_DATE,
        initial_capital=INITIAL_CAPITAL,
        frequency="1d",
    )
    result = bt.run(force_full=True)

    rows = []
    for symbol, r in result.per_symbol.items():
        if r.get("status") != "completed":
            continue
        m = r.get("metrics") or {}
        rows.append(
            {
                "symbol": symbol,
                "trade_count": int(r.get("trade_count") or 0),
                "total_return": float(m.get("total_return") or 0.0),
                "sharpe_ratio": float(m.get("sharpe_ratio") or 0.0),
                "max_drawdown": float(m.get("max_drawdown") or 0.0),
            }
        )
    signal_rows = [r for r in rows if r["trade_count"] > 0]
    n = len(signal_rows)
    agg = {
        "params": combo,
        "n_symbols": len(rows),
        "n_signal": n,
        "total_trades": sum(r["trade_count"] for r in signal_rows),
        "avg_sharpe": sum(r["sharpe_ratio"] for r in signal_rows) / n if n else None,
        "avg_return": sum(r["total_return"] for r in signal_rows) / n if n else None,
        "avg_drawdown": sum(r["max_drawdown"] for r in signal_rows) / n if n else None,
        "med_sharpe": sorted(r["sharpe_ratio"] for r in signal_rows)[n // 2] if n else None,
        "win_rate": sum(1 for r in signal_rows if r["total_return"] > 0) / n if n else None,
    }
    return agg


def expand_grid(grid: dict) -> list[dict]:
    """笛卡尔积展开参数网格。"""
    import itertools

    keys = list(grid.keys())
    combos = []
    for values in itertools.product(*[grid[k] for k in keys]):
        combos.append(dict(zip(keys, values)))
    return combos


def main() -> int:
    parser = argparse.ArgumentParser(description="鲲鹏策略参数网格扫描")
    parser.add_argument("--stage", choices=("signal", "exit", "both"), default="both")
    parser.add_argument("--top", type=int, default=50, help="活跃标的数上限")
    parser.add_argument("--min-trades", type=int, default=3, help="活跃标的成交笔数下限")
    args = parser.parse_args()

    print("=" * 72)
    print("  鲲鹏策略参数网格扫描（真实数据 / vectorized）")
    print("=" * 72)

    symbols = load_active_symbols(top_n=args.top, min_trades=args.min_trades)
    print(f"[标的池] 成交>={args.min_trades} 的活跃标的 top {len(symbols)}")
    data = load_data(symbols)
    print(f"[数据] 实际加载 {len(data)} 个标的（部分过滤后无数据被跳过）")
    if not data:
        print("[错误] 无有效数据")
        return 2

    stages = []
    if args.stage in ("signal", "both"):
        stages.append(("A_signal", SIGNAL_GRID))
    if args.stage in ("exit", "both"):
        stages.append(("B_exit", EXIT_GRID))

    all_results = {"meta": {"start_date": START_DATE, "end_date": str(END_DATE), "n_symbols": len(data)}, "stages": {}}
    for tag, grid in stages:
        combos = expand_grid(grid)
        print(f"\n[阶段 {tag}] {len(combos)} 个参数组合 × {len(data)} 标的")
        t0 = time.perf_counter()
        stage_rows = []
        for i, combo in enumerate(combos, 1):
            agg = run_one(combo, data, f"{tag}_{i}")
            stage_rows.append(agg)
            if i % 20 == 0 or i == len(combos):
                print(f"  进度 {i}/{len(combos)}（耗时 {time.perf_counter() - t0:.0f}s）")
        elapsed = time.perf_counter() - t0
        # 排序：优先信号数（>=1/3 标的），再按平均夏普降序
        stage_rows.sort(
            key=lambda r: (
                -(r["n_signal"] >= max(3, len(data) // 3)),
                -(r["avg_sharpe"] if r["avg_sharpe"] is not None else -999),
            )
        )
        all_results["stages"][tag] = {"elapsed_seconds": round(elapsed, 1), "rows": stage_rows}
        print(f"[阶段 {tag} 完成] 耗时 {elapsed:.0f}s | Top5:")
        for r in stage_rows[:5]:
            print(
                f"  夏普 {r['avg_sharpe'] if r['avg_sharpe'] is not None else float('nan'):.3f} | "
                f"收益 {r['avg_return'] if r['avg_return'] is not None else float('nan'):.2%} | "
                f"回撤 {r['avg_drawdown'] if r['avg_drawdown'] is not None else float('nan'):.2%} | "
                f"信号 {r['n_signal']}/{r['n_symbols']} 笔{r['total_trades']} | {r['params']}"
            )

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n[已保存] {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
