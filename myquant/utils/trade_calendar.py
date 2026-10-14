# 文件名：myquant/utils/trade_calendar.py
"""
trade_calendar.py - A股真实交易日历（P2-① #83）
=============================================
基于 akshare tool_trade_date_hist_sina 全历史交易日（1990-12-19 ~ 最新排期），
落盘 data/trade_calendar.csv 缓存，供交易日判定/序列查询使用。

防未来函数：本模块不消费未来日期——查询方（get_trading_dates 等）按 end_date
截断；同步接口一次性拉全历史落盘，未来排期仅由消费端 end_date 约束。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Optional, Set

from myquant.core.logger import get_logger

logger = get_logger(__name__)

#: 交易日历缓存目录/文件名（相对 project_root）
TRADE_CALENDAR_DIR: str = "data"
TRADE_CALENDAR_FILE: str = "trade_calendar.csv"


class TradeCalendarError(RuntimeError):
    """交易日历拉取/落盘失败。"""


def _cache_path(project_root: Path) -> Path:
    return project_root / TRADE_CALENDAR_DIR / TRADE_CALENDAR_FILE


def load_trade_calendar(project_root: Path) -> Optional[Set[date]]:
    """读取缓存中的真实交易日集合；无缓存/解析失败返回 None（调用方回退工作日近似）。"""
    path = _cache_path(project_root)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8", newline="") as f:
            dates: Set[date] = set()
            for row in csv.DictReader(f):
                d = str(row.get("trade_date", "")).strip()
                if len(d) >= 10:
                    dates.add(date.fromisoformat(d[:10]))
        if not dates:
            return None
        return dates
    except (OSError, ValueError) as exc:
        logger.warning("交易日历缓存读取失败，回退工作日近似: %s (%s)", path, exc)
        return None


def save_trade_calendar(project_root: Path, dates: Set[date]) -> Path:
    """落盘交易日集合为 CSV（升序，UTF-8）。"""
    path = _cache_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["trade_date"])
        for d in sorted(dates):
            writer.writerow([d.isoformat()])
    logger.info("交易日历缓存已落盘: %s（%d 个交易日）", path, len(dates))
    return path


def sync_trade_calendar(project_root: Path, force: bool = False) -> int:
    """从 akshare 拉取全历史交易日并落盘；返回交易日数量。

    已有缓存且非 force 时直接返回缓存数量（不触网）；force=True 强制刷新。
    拉取失败抛 TradeCalendarError（由调用方留痕/降级）。
    """
    if not force:
        cached = load_trade_calendar(project_root)
        if cached:
            logger.info("交易日历已有缓存（%d 个），--force 强制刷新", len(cached))
            return len(cached)
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
    except Exception as exc:
        raise TradeCalendarError(
            f"akshare 交易日历拉取失败: {type(exc).__name__}: {exc}"
        ) from exc
    if df is None or df.empty or "trade_date" not in df.columns:
        raise TradeCalendarError("akshare 交易日历返回空或缺少 trade_date 列")
    dates: Set[date] = set()
    for raw in df["trade_date"].tolist():
        try:
            d = raw.date() if hasattr(raw, "date") else date.fromisoformat(str(raw)[:10])
            dates.add(d)
        except (ValueError, TypeError):
            continue  # 脏行跳过
    save_trade_calendar(project_root, dates)
    return len(dates)
