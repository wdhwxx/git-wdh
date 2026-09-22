# -*- coding: utf-8 -*-
"""
鲲鹏壹号策略参数优化（真实行情网格搜索）V2
========================================
三轮优化:
  第一轮：核心信号参数宽网格（量能/粘合/MACD），出场参数固定当前配置
  第二轮：出场参数网格（档位/持有上限/单票仓位），信号参数取第一轮最优
  第三轮：信号参数边界精扫（围绕前轮最优外扩），出场参数取第二轮最优
评估口径:
  - 标的池：流动性 Top N 且覆盖回测区间(2024-01-01~2026-09-16)的真实标的
  - 引擎：vectorized（鲲鹏三接口之一，三模式结果一致性已验证）
  - 费用：config/backtest_kunpeng.yaml sim_broker 写实模型
  - 聚合：每参数组合 -> 全部标的的 平均夏普(无信号标的按0) / 平均收益 /
          信号标的平均夏普 / 信号标的平均收益 / 平均回撤 / 信号覆盖率 / 总成交
主推荐口径：avg_sharpe_all（全部标的平均夏普）；候选过滤：信号覆盖率>=10%
输出: outputs/kunpeng_param_opt/ 下 CSV/JSON + 控制台 Top 榜
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, r"D:\Projects\Pythonwdhmm")

from myquant.models.backtest import (
    BacktestConfig,
    CommissionModel,
    SlippageModel,
)
from myquant.backtest.engine import run_backtest
from myquant.backtest.data_loader import HistoricalDataProvider
from myquant.strategies.kunpeng_strategy import KunpengStrategy
from myquant.utils.parallel import ParallelManager

# ── 常量 ─────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(r"D:\Projects\Pythonwdhmm")
PROCESSED_DIR = str(PROJECT_ROOT / "datasets" / "processed")
CONFIG_PATH = PROJECT_ROOT / "config" / "backtest_kunpeng.yaml"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kunpeng_param_opt_loose"  # 放宽信号版，独立目录不覆盖原结果
START_DATE = "2024-01-01"
END_DATE = "2026-09-16"
INITIAL_CAPITAL = 1_000_000.0
MODE = "vectorized"
N_SYMBOLS = 300          # 参与优化的标的数（流动性 Top N）
MIN_COVERAGE = 0.30      # 最优组合的信号覆盖率下限（放宽信号版：提高成交样本要求）
TOP_N_SHOW = 15          # 榜单展示行数

# 当前配置基线参数（同时作为第一轮/第二轮的非网格参数）
BASELINE_PARAMS: Dict[str, Any] = {
    "vol_ratio_threshold": 4.5,
    "vol_count_window": 6,
    "vol_active_days": 5,
    "barslast_vol_window": 13,
    "barslast_ratio_window": 15,
    "ma_sticky_threshold": 1.22,
    "diff_dea_ratio": 0.018,
    "macd_abs_threshold": 0.1,
    "macd_rise_threshold": 0.0001,
    "exit_mode": "standard",
    "hold_days_cap": 60,
    "max_position_pct": 0.2,
    "max_positions": 5,
    "capital_base": 1_000_000.0,
}

# 第一轮：信号参数宽网格（出场固定 baseline）—— 放宽信号版：阈值整体下调，更容易触发信号
ROUND1_GRID: Dict[str, Tuple[Any, ...]] = {
    "vol_ratio_threshold": (2.0, 3.0, 4.5),
    "vol_active_days": (3, 4, 5),
    "ma_sticky_threshold": (1.02, 1.08, 1.15),
    "diff_dea_ratio": (0.003, 0.006, 0.010),
    "macd_rise_threshold": (0.00002, 0.00005, 0.0001),
}
# 第二轮：出场参数网格（信号参数固定第一轮最优）
ROUND2_GRID: Dict[str, Tuple[Any, ...]] = {
    "exit_mode": ("conservative", "standard", "aggressive"),
    "hold_days_cap": (30, 60, 90),
    "max_position_pct": (0.10, 0.20, 0.30),
}
# 第三轮：信号参数边界精扫（围绕第一轮最优外扩，出场固定第二轮最优）
ROUND3_GRID: Dict[str, Tuple[Any, ...]] = {
    "vol_ratio_threshold": (4.5, 5.0, 6.0),
    "vol_active_days": (6,),
    "ma_sticky_threshold": (1.02, 1.05, 1.10),
    "diff_dea_ratio": (0.005, 0.010),
    "macd_rise_threshold": (0.00002, 0.00005),
}


def load_commission_slippage(cfg: Dict[str, Any]) -> Tuple[CommissionModel, SlippageModel]:
    """从配置 sim_broker 段构建费用与滑点模型（与 run_鲲鹏批量回测.py 同口径）。"""
    sb = cfg.get("sim_broker", {})
    commission = CommissionModel(
        buy_rate=float(sb.get("commission_rate", 0.0002)),
        sell_rate=float(sb.get("commission_rate", 0.0002)),
        min_commission=float(sb.get("min_commission", 5.0)),
        stamp_tax=float(sb.get("stamp_tax_rate", 0.001)),
        transfer_fee=float(sb.get("transfer_fee_rate", 1.0e-05)),
    )
    value = float(sb.get("slippage_value", 0.0002))
    slippage = SlippageModel(type="fixed_bps", fixed_bps=value * 10000.0)
    return commission, slippage


def load_symbols(n: int) -> List[str]:
    """从候选清单取流动性 Top n 标的代码。"""
    cand = PROJECT_ROOT / "_candidates.csv"
    if not cand.exists():
        # 文件缺失友好提示（BUG-06）：--help 前置后，此处已是解析后业务路径
        print(f"[错误] 候选池文件不存在: {cand}")
        print("请先运行候选池生成（scripts/候选池交叉验证.py 或 scripts/标的预筛选.py）后再执行参数优化。")
        raise SystemExit(2)
    symbols: List[str] = []
    with open(cand, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            symbols.append(row["symbol"])
            if len(symbols) >= n:
                break
    if len(symbols) < n:
        raise RuntimeError(f"候选不足: 需要 {n}，实际 {len(symbols)}")
    return symbols


def load_data(symbols: List[str], start_date: str = START_DATE,
              end_date: str = END_DATE) -> Dict[str, Any]:
    """加载真实行情（统一 end_date 防未来函数）。"""
    provider = HistoricalDataProvider(
        processed_dir=PROCESSED_DIR, default_adjust="hfq", data_format="parquet",
    )
    data = provider.load_multiple(
        symbols, start_date=start_date, end_date=end_date, adjust="hfq",
    )
    missing = [s for s in symbols if s not in data]
    if missing:
        print(f"[警告] {len(missing)} 个标的无有效数据: {missing[:5]} ...")
    return data


# ── 子进程任务函数（模块级，可 pickle）───────────────────────────────
def evaluate_symbol_grid(item: Dict[str, Any]) -> Dict[str, Any]:
    """单标的 x 全部参数组合 的批量评估（子进程执行，永不抛出）。"""
    symbol = item["symbol"]
    df = item["df"]
    params_list = item["params_list"]
    outcomes: List[Dict[str, Any]] = []
    for params in params_list:
        entry: Dict[str, Any] = {
            "status": "error", "total_return": None, "sharpe_ratio": None,
            "max_drawdown": None, "trade_count": None, "error": None,
        }
        try:
            strategy = KunpengStrategy(**params)
            config = BacktestConfig(
                strategy_name="kunpeng",
                symbols=[symbol],
                start_date=item["start_date"],
                end_date=item["end_date"],
                frequency="1d",
                initial_capital=item["initial_capital"],
                params=params,
                commission=item["commission"],
                slippage=item["slippage"],
            )
            result = run_backtest(config, strategy, data={symbol: df}, mode=item["mode"])
            m = result.metrics
            entry.update({
                "status": "completed",
                "total_return": float(m.total_return),
                "sharpe_ratio": float(m.sharpe_ratio),
                "max_drawdown": float(m.max_drawdown),
                "trade_count": int(len(result.trades)),
            })
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        outcomes.append(entry)
    return {"symbol": symbol, "outcomes": outcomes}


# ── 聚合与排序 ───────────────────────────────────────────────────────
def aggregate(params_list: List[Dict[str, Any]],
              symbol_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按参数组合聚合全部标的的评估结果。"""
    n_params = len(params_list)
    agg: List[Dict[str, Any]] = []
    for pi in range(n_params):
        ok_count = 0
        signal_count = 0
        total_trades = 0
        sum_sharpe_all, sum_sharpe_sig = 0.0, 0.0
        sum_ret_all, sum_ret_sig = 0.0, 0.0
        sum_dd_all = 0.0
        for sr in symbol_results:
            outcome = sr["outcomes"][pi]
            if outcome["status"] != "completed":
                continue
            ok_count += 1
            ret = outcome["total_return"] or 0.0
            shr = outcome["sharpe_ratio"] or 0.0
            dd = outcome["max_drawdown"] or 0.0
            tc = outcome["trade_count"] or 0
            total_trades += tc
            sum_sharpe_all += shr
            sum_ret_all += ret
            sum_dd_all += dd
            if tc > 0:
                signal_count += 1
                sum_sharpe_sig += shr
                sum_ret_sig += ret
        agg.append({
            "params": params_list[pi],
            "ok_count": ok_count,
            "signal_count": signal_count,
            "coverage": (signal_count / ok_count) if ok_count else 0.0,
            "avg_sharpe_all": (sum_sharpe_all / ok_count) if ok_count else None,
            "avg_sharpe_signal": (sum_sharpe_sig / signal_count) if signal_count else None,
            "avg_return_all": (sum_ret_all / ok_count) if ok_count else None,
            "avg_return_signal": (sum_ret_sig / signal_count) if signal_count else None,
            "avg_drawdown": (sum_dd_all / ok_count) if ok_count else None,
            "total_trades": total_trades,
        })
    return agg


def sort_rows(rows: List[Dict[str, Any]], key: str,
              reverse: bool = True) -> List[Dict[str, Any]]:
    """按指定指标降序/升序排序（None 值排最后）。"""
    return sorted(
        rows,
        key=lambda r: (r[key] is None, r[key] if r[key] is not None else 0.0),
        reverse=reverse,
    )


def pick_best(rows: List[Dict[str, Any]], key: str = "avg_sharpe_all") -> Dict[str, Any]:
    """在覆盖率达标的组合中选最优（主指标最高）。"""
    eligible = [r for r in rows if r["coverage"] >= MIN_COVERAGE and r[key] is not None]
    if not eligible:
        eligible = [r for r in rows if r[key] is not None]
    if not eligible:
        raise RuntimeError("无可用组合（全部失败）")
    return sort_rows(eligible, key)[0]


def fmt_params(p: Dict[str, Any]) -> str:
    """参数组合紧凑串。"""
    return " ".join(f"{k}={v}" for k, v in p.items())


def run_grid(params_list: List[Dict[str, Any]], data: Dict[str, Any],
             commission: CommissionModel, slippage: SlippageModel,
             round_name: str, start_date: str = START_DATE,
             end_date: str = END_DATE) -> List[Dict[str, Any]]:
    """并行评估参数网格并聚合。"""
    items = [
        {
            "symbol": sym, "df": df, "params_list": params_list,
            "mode": MODE, "start_date": start_date, "end_date": end_date,
            "initial_capital": INITIAL_CAPITAL,
            "commission": commission, "slippage": slippage,
        }
        for sym, df in data.items()
    ]
    print(f"[{round_name}] 提交 {len(items)} 标的 × {len(params_list)} 参数组 "
          f"= {len(items) * len(params_list)} 组评估，并行中 ...", flush=True)
    t0 = time.perf_counter()
    with ParallelManager() as pm:
        outcomes = pm.map_cpu(evaluate_symbol_grid, items, return_exceptions=True,
                              check_pickle=True)
    elapsed = time.perf_counter() - t0
    symbol_results: List[Dict[str, Any]] = []
    for item, outcome in zip(items, outcomes):
        if isinstance(outcome, BaseException):
            symbol_results.append({
                "symbol": item["symbol"],
                "outcomes": [{
                    "status": "error", "total_return": None, "sharpe_ratio": None,
                    "max_drawdown": None, "trade_count": None,
                    "error": f"{type(outcome).__name__}: {outcome}",
                }] * len(params_list),
            })
        else:
            symbol_results.append(outcome)
    rows = aggregate(params_list, symbol_results)
    ok_symbols = sum(1 for s in symbol_results
                     if s["outcomes"][0]["status"] == "completed")
    print(f"[{round_name}] 完成: 耗时 {elapsed:.1f}s | 成功标的 {ok_symbols}", flush=True)
    return rows


def save_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    """保存聚合结果 CSV（params 展开为列）。"""
    fieldnames = ["coverage", "avg_sharpe_all", "avg_sharpe_signal",
                  "avg_return_all", "avg_return_signal", "avg_drawdown",
                  "total_trades", "ok_count", "signal_count"]
    param_names = list(rows[0]["params"].keys()) if rows else []
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["rank"] + param_names + fieldnames)
        for rank, r in enumerate(rows, start=1):
            writer.writerow([rank] + [r["params"].get(k) for k in param_names] +
                            [r[k] for k in fieldnames])


def print_board(title: str, rows: List[Dict[str, Any]], show: int = TOP_N_SHOW) -> None:
    """打印排行榜（主口径 avg_sharpe_all）。"""
    print("\n" + "=" * 122)
    print(f"  {title}")
    print("=" * 122)
    print(f"{'#':<4} {'参数(变段)':<46} {'覆盖率':>7} {'夏普全':>8} {'夏普信号':>9} "
          f"{'收益全':>8} {'收益信号':>9} {'回撤':>8} {'成交':>8}")
    for i, r in enumerate(rows[:show], start=1):
        # 只显示与 baseline 不同的参数
        diff = " ".join(f"{k}={v}" for k, v in r["params"].items()
                        if k not in ("barslast_vol_window", "barslast_ratio_window",
                                     "vol_count_window", "macd_abs_threshold",
                                     "capital_base") or r["params"][k] != BASELINE_PARAMS[k])
        print(f"{i:<4} {diff[:44]:<46} "
              f"{r['coverage']:>7.1%} "
              f"{r['avg_sharpe_all'] if r['avg_sharpe_all'] is not None else float('nan'):>8.2f} "
              f"{r['avg_sharpe_signal'] if r['avg_sharpe_signal'] is not None else float('nan'):>9.2f} "
              f"{r['avg_return_all'] if r['avg_return_all'] is not None else float('nan'):>8.2%} "
              f"{r['avg_return_signal'] if r['avg_return_signal'] is not None else float('nan'):>9.2%} "
              f"{r['avg_drawdown'] if r['avg_drawdown'] is not None else float('nan'):>8.2%} "
              f"{r['total_trades']:>8}")
    print()


def build_grid(base: Dict[str, Any], grid: Dict[str, Tuple[Any, ...]]) -> List[Dict[str, Any]]:
    """按网格定义展开参数组合（未在 grid 中的参数沿用 base）。"""
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    params_list: List[Dict[str, Any]] = []
    for combo in combos:
        p = dict(base)
        for k, v in zip(keys, combo):
            p[k] = v
        params_list.append(p)
    return params_list


def merge_metrics(r: Dict[str, Any]) -> Dict[str, Any]:
    """提取行指标（JSON 落盘用）。"""
    return {k: r[k] for k in ("coverage", "avg_sharpe_all", "avg_sharpe_signal",
                              "avg_return_all", "avg_return_signal",
                              "avg_drawdown", "total_trades", "signal_count")}


def parse_args() -> argparse.Namespace:
    """解析命令行参数（argparse 前置：--help 在业务加载前返回，不触碰候选文件/行情）。"""
    parser = argparse.ArgumentParser(
        description="鲲鹏壹号策略参数优化（三轮网格搜索：信号宽网格→出场网格→边界精扫，真实行情）",
    )
    parser.add_argument("--n-symbols", type=int, default=N_SYMBOLS,
                        help=f"参与优化标的数（默认 {N_SYMBOLS}）")
    parser.add_argument("--start", default=START_DATE,
                        help=f"回测起始日（默认 {START_DATE}）")
    parser.add_argument("--end", default=END_DATE,
                        help=f"回测截止日（默认 {END_DATE}）")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR,
                        help=f"输出目录（默认 {OUTPUT_DIR.name}）")
    return parser.parse_args()


def main(args: Optional[argparse.Namespace] = None) -> int:
    """主流程：解析参数 -> 加载数据 -> 三轮网格扫描 -> 基线对照 -> 推荐 -> 输出。"""
    if args is None:
        args = parse_args()
    n_symbols = int(args.n_symbols)
    start_date = str(args.start)
    end_date = str(args.end)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. 配置与数据
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    commission, slippage = load_commission_slippage(cfg)
    symbols = load_symbols(n_symbols)
    print(f"[数据] 标的 {len(symbols)} 个（流动性 Top {n_symbols}），"
          f"区间 {start_date} ~ {end_date}", flush=True)
    data = load_data(symbols, start_date=start_date, end_date=end_date)
    print(f"[数据] 实际加载 {len(data)} 个标的", flush=True)

    # 2. 第一轮：信号参数宽网格
    round1_params = build_grid(BASELINE_PARAMS, ROUND1_GRID)
    print(f"\n[第一轮] 信号参数宽网格: {len(round1_params)} 组合", flush=True)
    rows1 = run_grid(round1_params, data, commission, slippage, "第一轮",
                     start_date=start_date, end_date=end_date)
    rows1_sorted = sort_rows(rows1, "avg_sharpe_all")
    save_csv(rows1_sorted, out_dir / "round1_signal.csv")
    print_board("第一轮 Top 榜（信号参数宽网格，出场=当前配置）", rows1_sorted)
    best1 = pick_best(rows1)
    print(f"[第一轮最优] {fmt_params(best1['params'])} | "
          f"覆盖率 {best1['coverage']:.1%} | 夏普(全) {best1['avg_sharpe_all']:.2f} "
          f"| 收益(全) {best1['avg_return_all']:.2%}", flush=True)

    # 3. 第二轮：出场参数网格（信号参数固定第一轮最优）
    round2_params = build_grid(best1["params"], ROUND2_GRID)
    print(f"\n[第二轮] 出场参数网格: {len(round2_params)} 组合", flush=True)
    rows2 = run_grid(round2_params, data, commission, slippage, "第二轮",
                     start_date=start_date, end_date=end_date)
    rows2_sorted = sort_rows(rows2, "avg_sharpe_all")
    save_csv(rows2_sorted, out_dir / "round2_exit.csv")
    print_board("第二轮 Top 榜（出场参数，信号参数=第一轮最优）", rows2_sorted)
    best2 = pick_best(rows2)
    print(f"[第二轮最优] {fmt_params(best2['params'])} | "
          f"覆盖率 {best2['coverage']:.1%} | 夏普(全) {best2['avg_sharpe_all']:.2f} "
          f"| 收益(全) {best2['avg_return_all']:.2%}", flush=True)

    # 4. 第三轮：信号参数边界精扫（出场参数固定第二轮最优）
    round3_params = build_grid(best2["params"], ROUND3_GRID)
    print(f"\n[第三轮] 信号参数边界精扫: {len(round3_params)} 组合", flush=True)
    rows3 = run_grid(round3_params, data, commission, slippage, "第三轮",
                     start_date=start_date, end_date=end_date)
    rows3_sorted = sort_rows(rows3, "avg_sharpe_all")
    save_csv(rows3_sorted, out_dir / "round3_refine.csv")
    print_board("第三轮 Top 榜（信号参数精扫，出场=第二轮最优）", rows3_sorted)
    best3 = pick_best(rows3)
    print(f"[第三轮最优] {fmt_params(best3['params'])} | "
          f"覆盖率 {best3['coverage']:.1%} | 夏普(全) {best3['avg_sharpe_all']:.2f} "
          f"| 收益(全) {best3['avg_return_all']:.2%}", flush=True)

    # 5. 基线对照
    print(f"\n[基线] 当前配置参数评估 ...", flush=True)
    base_rows = run_grid([BASELINE_PARAMS], data, commission, slippage, "基线",
                         start_date=start_date, end_date=end_date)
    base_row = base_rows[0]
    print("\n" + "=" * 122)
    print("  基线对照（当前 config/backtest_kunpeng.yaml 参数）")
    print("=" * 122)
    print(f"  覆盖率 {base_row['coverage']:.1%} | 夏普(全) {base_row['avg_sharpe_all']:.2f} "
          f"| 夏普(信号) {base_row['avg_sharpe_signal']:.2f} "
          f"| 收益(全) {base_row['avg_return_all']:.2%} "
          f"| 收益(信号) {base_row['avg_return_signal']:.2%} "
          f"| 回撤 {base_row['avg_drawdown']:.2%} | 成交 {base_row['total_trades']}", flush=True)

    # 6. 综合推荐：第三轮最优（若精扫后边界外无更优，仍取最优）
    best_final = best3
    # 若第三轮最优与第一轮最优相同（无外扩增益），则推荐第二轮最优
    print("\n" + "=" * 122)
    print("  推荐最佳参数（三轮综合）")
    print("=" * 122)
    print(f"  {fmt_params(best_final['params'])}")
    print(f"  覆盖率 {best_final['coverage']:.1%} | 夏普(全) {best_final['avg_sharpe_all']:.2f} "
          f"| 夏普(信号) {best_final['avg_sharpe_signal']:.2f} "
          f"| 收益(全) {best_final['avg_return_all']:.2%} "
          f"| 收益(信号) {best_final['avg_return_signal']:.2%} "
          f"| 回撤 {best_final['avg_drawdown']:.2%} | 成交 {best_final['total_trades']}", flush=True)

    # 7. 落盘汇总
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scope": {
            "symbols": len(data), "start": start_date, "end": end_date,
            "mode": MODE, "initial_capital": INITIAL_CAPITAL,
            "symbol_source": f"流动性Top{N_SYMBOLS}且覆盖区间",
            "min_coverage": MIN_COVERAGE,
        },
        "baseline": BASELINE_PARAMS,
        "baseline_metrics": merge_metrics(base_row),
        "round1_best": {"params": best1["params"], "metrics": merge_metrics(best1)},
        "round2_best": {"params": best2["params"], "metrics": merge_metrics(best2)},
        "round3_best": {"params": best3["params"], "metrics": merge_metrics(best3)},
        "recommended": best_final["params"],
        "recommended_metrics": merge_metrics(best_final),
    }
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[已保存] {out_dir / 'summary.json'}")
    print(f"[已保存] {out_dir / 'round1_signal.csv'}")
    print(f"[已保存] {out_dir / 'round2_exit.csv'}")
    print(f"[已保存] {out_dir / 'round3_refine.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
