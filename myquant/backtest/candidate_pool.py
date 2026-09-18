# 文件名：myquant/backtest/candidate_pool.py
"""
双策略交叉验证候选池（批量回测结果分析）
============================================
从两个策略的批量回测缓存 results.json 提取绩效，按硬性过滤条件
（交易数下限 / 回撤上限 / 夏普下限）各自筛选后取交集，得到"双强"候选池，
再按综合夏普（两策略均值）降序标记 Top N，导出 CSV（utf-8-sig，表头中文）。

数据源约定：
  - 读取 results/batch_backtest/<strategy>/results.json（批量回测缓存权威，
    含 status / metrics / trade_count / equity_length；CSV 只是导出视图，
    可能滞后于缓存修复，故不直接读 CSV）
  - 仅使用 status == "completed" 的标的，error 标的自动排除

设计原则（docs/py文件代码规则.md）：
  - 全量类型注解 + Google docstring + 防御式校验
  - logging 记录过程，不 print 运行信息
  - 业务逻辑归 myquant，入口脚本只做薄入口（scripts/候选池交叉验证.py）
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── 常量（模块顶部，禁止魔法数字）──────────────────────────────────────
DEFAULT_MIN_SHARPE: float = 0.5      # 单策略夏普下限
DEFAULT_MIN_TRADES: int = 10         # 单策略交易数下限
DEFAULT_MAX_DRAWDOWN: float = 0.10   # 单策略最大回撤上限（0.10 = 10%）
DEFAULT_TOP_N: int = 30              # 综合夏普降序标记的 Top N
RESULTS_FILENAME: str = "results.json"

# 输出列（固定顺序，与既有 dual_strong_pool 产物保持一致）
OUTPUT_COLUMNS: list[str] = [
    "symbol",
    "rsrs_ret", "rsrs_sharpe", "rsrs_max_drawdown", "rsrs_trades",
    "ma_cross_ret", "ma_cross_sharpe", "ma_cross_max_drawdown", "ma_cross_trades",
    "avg_sharpe", "is_top30",
]

# CSV 表头中文化映射：内部 DataFrame 列名保持英文（供代码访问），
# 仅在导出 CSV 时替换为中文表头（顺序与 OUTPUT_COLUMNS 一一对应）。
CSV_HEADERS_ZH: dict[str, str] = {
    "symbol": "股票代码",
    "rsrs_ret": "rsrs收益率",
    "rsrs_sharpe": "rsrs夏普比率",
    "rsrs_max_drawdown": "rsrs最大回撤",
    "rsrs_trades": "rsrs交易次数",
    "ma_cross_ret": "ma_cross收益率",
    "ma_cross_sharpe": "ma_cross夏普比率",
    "ma_cross_max_drawdown": "ma_cross最大回撤",
    "ma_cross_trades": "ma_cross交易次数",
    "avg_sharpe": "综合夏普比率",
    "is_top30": "是否Top30",
}


def _load_completed_entries(cache_dir: Path) -> Dict[str, Dict[str, Any]]:
    """读取策略缓存 results.json，返回 completed 标的的绩效字典。

    Args:
        cache_dir: 策略缓存目录（含 results.json）。

    Returns:
        {symbol: {ret, sharpe, max_drawdown, trades}}，仅包含 completed 标的。

    Raises:
        FileNotFoundError: results.json 不存在时抛出。
        ValueError: results.json 结构非法（缺 results 键或 metrics 字段）时抛出。
    """
    if not cache_dir.is_dir():
        raise FileNotFoundError(f"策略缓存目录不存在: {cache_dir}")
    results_path = cache_dir / RESULTS_FILENAME
    if not results_path.is_file():
        raise FileNotFoundError(f"结果缓存不存在: {results_path}")

    raw = json.loads(results_path.read_text(encoding="utf-8"))
    results: Dict[str, Any] = raw.get("results") if isinstance(raw, dict) else None
    if not isinstance(results, dict):
        raise ValueError(f"结果缓存缺少 results 键: {results_path}")

    entries: Dict[str, Dict[str, Any]] = {}
    for symbol, entry in results.items():
        if not isinstance(entry, dict) or entry.get("status") != "completed":
            continue
        metrics: Dict[str, Any] = entry.get("metrics") or {}
        try:
            entries[symbol] = {
                "ret": float(metrics["total_return"]),
                "sharpe": float(metrics["sharpe_ratio"]),
                "max_drawdown": float(metrics["max_drawdown"]),
                "trades": int(entry.get("trade_count") or 0),
            }
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("标的 %s 绩效字段缺失/非法，跳过: %s", symbol, exc)
    return entries


def _filter_entries(
    entries: Dict[str, Dict[str, Any]],
    min_sharpe: float,
    min_trades: int,
    max_drawdown: float,
) -> set[str]:
    """按硬性过滤条件筛选通过标的。

    Args:
        entries: _load_completed_entries 返回的绩效字典。
        min_sharpe: 夏普下限（≥）。
        min_trades: 交易数下限（≥）。
        max_drawdown: 最大回撤上限（≤）。

    Returns:
        通过过滤的标的代码集合。

    Raises:
        ValueError: 阈值参数非法时抛出。
    """
    if min_sharpe <= 0:
        raise ValueError(f"min_sharpe 必须为正数，收到: {min_sharpe}")
    if min_trades <= 0:
        raise ValueError(f"min_trades 必须为正整数，收到: {min_trades}")
    if not 0 < max_drawdown < 1:
        raise ValueError(f"max_drawdown 必须在 (0, 1) 区间，收到: {max_drawdown}")

    passed: set[str] = set()
    for symbol, m in entries.items():
        if m["sharpe"] >= min_sharpe and m["trades"] >= min_trades and m["max_drawdown"] <= max_drawdown:
            passed.add(symbol)
    return passed


def build_candidate_pool(
    rsrs_dir: Path,
    ma_cross_dir: Path,
    min_sharpe: float = DEFAULT_MIN_SHARPE,
    min_trades: int = DEFAULT_MIN_TRADES,
    max_drawdown: float = DEFAULT_MAX_DRAWDOWN,
    top_n: int = DEFAULT_TOP_N,
    out_path: Optional[Path] = None,
) -> pd.DataFrame:
    """构建双策略交叉验证候选池。

    流程：各自加载 completed 绩效 → 各自硬过滤 → 取交集（双强） →
    按综合夏普（(rsrs+ma_cross)/2）降序 → 标记 Top N → 导出 CSV。

    Args:
        rsrs_dir: rsrs 策略缓存目录。
        ma_cross_dir: ma_cross 策略缓存目录。
        min_sharpe: 单策略夏普下限，默认 0.5。
        min_trades: 单策略交易数下限，默认 10。
        max_drawdown: 单策略最大回撤上限，默认 0.10。
        top_n: 综合夏普降序标记数量，默认 30。
        out_path: CSV 输出路径；None 时不落盘。

    Returns:
        候选池 DataFrame（列见 OUTPUT_COLUMNS，按 avg_sharpe 降序）。

    Raises:
        ValueError: top_n 非法或双强池为空时抛出。
    """
    if top_n <= 0:
        raise ValueError(f"top_n 必须为正整数，收到: {top_n}")

    rsrs_entries = _load_completed_entries(rsrs_dir)
    ma_entries = _load_completed_entries(ma_cross_dir)
    logger.info(
        "加载完成: rsrs completed=%d, ma_cross completed=%d",
        len(rsrs_entries), len(ma_entries),
    )

    rsrs_pass = _filter_entries(rsrs_entries, min_sharpe, min_trades, max_drawdown)
    ma_pass = _filter_entries(ma_entries, min_sharpe, min_trades, max_drawdown)
    dual = rsrs_pass & ma_pass
    logger.info(
        "硬过滤: rsrs=%d 只通过, ma_cross=%d 只通过, 双强交集=%d 只",
        len(rsrs_pass), len(ma_pass), len(dual),
    )
    if not dual:
        raise ValueError(
            "双强池为空：请检查过滤阈值（夏普≥%g、交易≥%d、回撤≤%g）",
            min_sharpe, min_trades, max_drawdown,
        )

    rows: list[dict[str, Any]] = []
    for symbol in sorted(dual):
        r, m = rsrs_entries[symbol], ma_entries[symbol]
        avg_sharpe = (r["sharpe"] + m["sharpe"]) / 2.0
        rows.append({
            "symbol": symbol,
            "rsrs_ret": round(r["ret"], 6),
            "rsrs_sharpe": round(r["sharpe"], 6),
            "rsrs_max_drawdown": round(r["max_drawdown"], 6),
            "rsrs_trades": r["trades"],
            "ma_cross_ret": round(m["ret"], 6),
            "ma_cross_sharpe": round(m["sharpe"], 6),
            "ma_cross_max_drawdown": round(m["max_drawdown"], 6),
            "ma_cross_trades": m["trades"],
            "avg_sharpe": round(avg_sharpe, 6),
            "is_top30": False,
        })

    pool = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    pool = pool.sort_values("avg_sharpe", ascending=False, kind="stable").reset_index(drop=True)
    pool.loc[: top_n - 1, "is_top30"] = True
    logger.info("候选池 %d 只，标记 Top%d %d 只", len(pool), top_n, int(pool["is_top30"].sum()))

    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 导出时仅替换表头为中文（内部 DataFrame 列名保持英文，代码访问不受影响）
        pool.rename(columns=CSV_HEADERS_ZH).to_csv(
            out_path, index=False, encoding="utf-8-sig",
        )
        logger.info("候选池已导出: %s", out_path)
    return pool
