# 文件名：myquant/backtest/survivorship_bias.py
"""
survivorship_bias.py - 幸存者偏差评估器（F-04）
================================================
背景（问题链，审查确认）:
  - 数据源头 = 当前存续 A 股列表（akshare/通达信 get_stock_list 快照），
    退市/已摘牌股票从未入库 → DB 中不存在历史退市股行情；
  - 回测池 = datasets/processed = 清洗后存续池（排除规则 / ST /
    长期停牌>阈值天剔除），天然排除退市股 → 回测收益系统性偏乐观
    （幸存者偏差）；
  - 任务卡验收（P0 整改任务清单 F-04）：回测报告声明口径（是否含退市股）
    + 含偏差折价或完整退市数据，二者必居其一。

本模块职责（偏差折价披露路径）:
  1. audit_data_coverage：数据覆盖审计——DB/processed 标的数、最后交易日
     分布、停牌近似池（最后交易日 < ref_date - 阈值 的标的）、退市覆盖
     声明（当前数据源无历史退市股）
  2. estimate_survivorship_discount：偏差折价估计——区间等权收益对比
     「存续池 vs 存续池 ∪ 停牌近似池」，输出折价系数（停牌风险下界，
     真退市股损失通常更极端，披露注明），年化折算口径一并输出
  3. build_point_in_time_pool：逐期存续池构建——给定时点 as_of，
     池 = 最后交易日 >= as_of 的标的（任务卡整改内容 1 逻辑显式化，
     未来接入退市数据无缝生效）

防未来函数（py 文件代码规则·铁律 11）:
  - 停牌/存续判定均基于"该时点已可得数据"（最后交易日为数据事实，
    as_of/ref_date 可注入，测试可固定日期）；
  - 严禁用"今日是否存续"回溯历史池（逐期池构建天然规避）；
  - 本模块只读 quant.db market_data 表，绝不写库。

实现:
  - DB 只读连接（sqlite3 mode=ro）+ pandas 纯函数
  - 阈值常量集中定义，无魔法数字
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from myquant.core.logger import get_logger

logger = get_logger("backtest.survivorship_bias")

# ── 常量（集中定义，禁止魔法数字）──────────────────────────────
# 长期停牌判定阈值（与 config/data_clean_run.json exclude_suspended_days=60 对齐）
DEFAULT_SUSPEND_DAYS: int = 60
# 区间收益计算最少有效交易日（防御：数据不足的标的跳过）
MIN_TRADING_DAYS: int = 5
# 年化折算交易日基数（A 股年度交易日近似）
TRADING_DAYS_PER_YEAR: int = 252


def _default_ref_date() -> str:
    """默认参考日期（当天），格式 YYYY-MM-DD。"""
    return datetime.now().strftime("%Y-%m-%d")


def _open_ro_db(db_path: Path):
    """只读打开 quant.db（mode=ro，绝不写库）。

    Args:
        db_path: quant.db 路径。

    Returns:
        sqlite3.Connection（只读模式）。

    Raises:
        FileNotFoundError: db_path 不存在时抛出。
    """
    import sqlite3

    if not Path(db_path).is_file():
        raise FileNotFoundError(f"quant.db 不存在: {db_path}")
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


# ═══════════════════════════════════════════════
#  1. 数据覆盖审计
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class CoverageAudit:
    """数据覆盖审计结果（可序列化入档）。

    Attributes:
        db_symbol_count: DB market_data 标的数。
        processed_symbol_count: processed 目录 parquet 标的数。
        latest_trade_date: DB 全市场最后交易日（YYYY-MM-DD）。
        suspended_pool: 停牌近似池标的列表（最后交易日 < ref_date - 阈值）。
        suspended_count: 停牌近似池数量。
        delisted_coverage: 是否含历史退市股（当前数据源=False）。
        statement: 口径声明文本（回测报告直接引用）。
        created_at: 审计时间。
    """

    db_symbol_count: int
    processed_symbol_count: int
    latest_trade_date: Optional[str]
    suspended_pool: List[str]
    suspended_count: int
    delisted_coverage: bool
    statement: str
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 JSON 安全 dict（报告口径声明注入）。"""
        return {
            "db_symbol_count": self.db_symbol_count,
            "processed_symbol_count": self.processed_symbol_count,
            "latest_trade_date": self.latest_trade_date,
            "suspended_pool": list(self.suspended_pool),
            "suspended_count": self.suspended_count,
            "delisted_coverage": self.delisted_coverage,
            "statement": self.statement,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CoverageAudit":
        """从 dict 反序列化（防御式校验）。"""
        if not isinstance(data, dict):
            raise ValueError(f"CoverageAudit 数据必须是 dict，got: {type(data).__name__}")
        if "db_symbol_count" not in data or "statement" not in data:
            raise ValueError("CoverageAudit 缺少必填字段(db_symbol_count/statement)")
        suspended = data.get("suspended_pool") or []
        return cls(
            db_symbol_count=int(data.get("db_symbol_count", 0)),
            processed_symbol_count=int(data.get("processed_symbol_count", 0)),
            latest_trade_date=data.get("latest_trade_date"),
            suspended_pool=[str(s) for s in suspended],
            suspended_count=int(data.get("suspended_count", len(suspended))),
            delisted_coverage=bool(data.get("delisted_coverage", False)),
            statement=str(data.get("statement", "")),
            created_at=str(data.get("created_at", "")),
        )


def audit_data_coverage(
    project_root: Optional[Path] = None,
    db_path: Optional[Path] = None,
    processed_dir: Optional[Path] = None,
    suspend_days: int = DEFAULT_SUSPEND_DAYS,
    ref_date: Optional[str] = None,
) -> CoverageAudit:
    """数据覆盖审计：量化退市股覆盖缺口与停牌近似池规模。

    Args:
        project_root: 项目根（默认由 __file__ 推导 parents[2]）。
        db_path: quant.db 路径（默认 project_root/database/quant.db）。
        processed_dir: processed 目录（默认 project_root/datasets/processed）。
        suspend_days: 长期停牌判定阈值天数（<=0 时不启用停牌池）。
        ref_date: 参考日期 YYYY-MM-DD；None 用当天（测试可注入）。

    Returns:
        CoverageAudit（含口径声明 statement）。

    Raises:
        FileNotFoundError: DB 或 processed 目录缺失时抛出。
    """
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
    db = Path(db_path) if db_path else root / "database" / "quant.db"
    pdir = Path(processed_dir) if processed_dir else root / "datasets" / "processed"
    ref = ref_date or _default_ref_date()

    conn = _open_ro_db(db)
    try:
        rows = conn.execute(
            "SELECT symbol, MAX(trade_date) FROM market_data "
            "WHERE period='daily' AND adjust='raw' GROUP BY symbol"
        ).fetchall()
    finally:
        conn.close()

    last_map: Dict[str, str] = {}
    for sym, mx in rows:
        if mx is not None:
            last_map[str(sym)] = str(mx)
    db_count = len(last_map)
    latest = max(last_map.values()) if last_map else None

    processed_count = 0
    if pdir.is_dir():
        processed_count = len([
            f for f in pdir.iterdir()
            if f.is_file() and f.suffix.lower() in (".parquet", ".csv")
            and not f.name.startswith(".")
        ])

    suspended: List[str] = []
    if suspend_days > 0 and latest is not None:
        try:
            ref_dt = date.fromisoformat(ref)
        except ValueError:
            ref_dt = date.today()
        for sym in sorted(last_map):
            try:
                gap = (ref_dt - date.fromisoformat(last_map[sym])).days
            except ValueError:
                continue
            if gap > suspend_days:
                suspended.append(sym)

    delisted_coverage = False
    statement = (
        "【数据口径声明】回测标的池源自当前存续 A 股列表快照"
        "（akshare/通达信 get_stock_list），已剔除指数/基金/ST/长期停牌"
        f">={suspend_days} 天标的；历史退市/已摘牌股票未纳入（覆盖率=0），"
        "存在幸存者偏差——回测收益可能系统性偏乐观。"
        f"当前 DB 标的 {db_count} 只（最新交易日 {latest}），"
        f"processed {processed_count} 只，停牌近似池 {len(suspended)} 只。"
        "偏差折价量化见 estimate_survivorship_discount。"
    )
    return CoverageAudit(
        db_symbol_count=db_count,
        processed_symbol_count=processed_count,
        latest_trade_date=latest,
        suspended_pool=suspended,
        suspended_count=len(suspended),
        delisted_coverage=delisted_coverage,
        statement=statement,
    )


# ═══════════════════════════════════════════════
#  2. 偏差折价估计
# ═══════════════════════════════════════════════

def _interval_return(
    conn: Any,
    symbol: str,
    start_date: str,
    end_date: str,
) -> Optional[float]:
    """区间等权买入持有收益（首日收盘买入 → 末日收盘卖出）。

    数据截至 min(end_date, 该标的最后交易日)（停牌标的用停牌前收盘，
    近似"纳入停牌/退市风险标的"的真实可得收益）。

    Args:
        conn: 只读 DB 连接。
        symbol: 标的代码。
        start_date: 区间起始 YYYY-MM-DD。
        end_date: 区间结束 YYYY-MM-DD。

    Returns:
        区间收益（小数，如 0.12）；数据不足/价格非法返回 None。
    """
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM market_data "
            "WHERE period='daily' AND adjust='raw' AND symbol=? "
            "AND trade_date>=? AND trade_date<=? ORDER BY trade_date",
            (symbol, start_date, end_date),
        ).fetchall()
    except Exception as exc:  # 防御：单标的查询失败跳过，不阻断
        logger.warning("区间收益查询失败[%s]: %s", symbol, exc)
        return None
    if len(rows) < MIN_TRADING_DAYS:
        return None
    closes: List[float] = []
    for _, close in rows:
        try:
            c = float(close)
        except (TypeError, ValueError):
            c = float("nan")
        if c > 0:
            closes.append(c)
    if len(closes) < 2:
        return None
    if closes[0] <= 0:
        return None
    return closes[-1] / closes[0] - 1.0


def estimate_survivorship_discount(
    project_root: Optional[Path] = None,
    db_path: Optional[Path] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    suspend_days: int = DEFAULT_SUSPEND_DAYS,
    ref_date: Optional[str] = None,
) -> Dict[str, Any]:
    """偏差折价估计：区间等权收益对比「存续池 vs 存续池∪停牌近似池」。

    存续池     = 最后交易日 >= ref_date - suspend_days 的标的（现有回测口径）
    全样本近似池 = 存续池 ∪ 停牌近似池（最后交易日更早、仍有区间内数据的标的，
                 其收益按数据截至停牌前收盘计算）
    折价系数   = 全样本近似池平均收益 - 存续池平均收益
              （正数 = 存续池虚高；停牌风险下界——真退市股损失通常更极端）

    Args:
        project_root: 项目根（默认 __file__ 推导）。
        db_path: quant.db 路径。
        start_date: 区间起始 YYYY-MM-DD；None 用 DB 最早交易日。
        end_date: 区间结束 YYYY-MM-DD；None 用 DB 最新交易日。
        suspend_days: 停牌近似池判定阈值天数。
        ref_date: 参考日期；None 用当天（测试可注入）。

    Returns:
        结果 dict（口径/存续池收益/全样本近似池收益/折价系数/年化折算/
        样本数/区间信息），供报告口径声明注入。

    Raises:
        ValueError: 区间非法或两池样本不足时抛出（折价无统计意义）。
    """
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
    db = Path(db_path) if db_path else root / "database" / "quant.db"
    ref = ref_date or _default_ref_date()
    conn = _open_ro_db(db)
    try:
        bounds = conn.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM market_data "
            "WHERE period='daily' AND adjust='raw'"
        ).fetchone()
        db_min, db_max = (str(bounds[0]) if bounds else None,
                          str(bounds[1]) if bounds else None)
        start = start_date or db_min
        end = end_date or db_max
        if not start or not end:
            raise ValueError("DB 无行情数据，无法估计偏差折价")
        if start > end:
            raise ValueError(f"区间非法: start({start}) > end({end})")

        try:
            ref_dt = date.fromisoformat(ref)
        except ValueError:
            ref_dt = date.today()

        rows = conn.execute(
            "SELECT symbol, MAX(trade_date) FROM market_data "
            "WHERE period='daily' AND adjust='raw' GROUP BY symbol"
        ).fetchall()
        survivor: List[str] = []
        suspended: List[str] = []
        for sym, mx in rows:
            if mx is None:
                continue
            try:
                gap = (ref_dt - date.fromisoformat(str(mx))).days
            except ValueError:
                continue
            if gap > suspend_days:
                suspended.append(str(sym))
            else:
                survivor.append(str(sym))
    finally:
        conn.close()

    # 重新打开连接用于区间收益查询（上一连接已关）
    conn = _open_ro_db(db)
    try:
        surv_rets: List[float] = []
        for sym in survivor:
            r = _interval_return(conn, sym, start, end)
            if r is not None:
                surv_rets.append(r)
        full_rets: List[float] = list(surv_rets)
        for sym in suspended:
            r = _interval_return(conn, sym, start, end)
            if r is not None:
                full_rets.append(r)
    finally:
        conn.close()

    if len(surv_rets) < 3 or len(full_rets) < 3:
        raise ValueError(
            f"偏差折价样本不足: 存续池 {len(surv_rets)} / 全样本近似 {len(full_rets)}"
        )

    surv_ret = float(pd.Series(surv_rets).mean())
    full_ret = float(pd.Series(full_rets).mean())
    discount = full_ret - surv_ret
    # 年化折算（区间交易日基数近似）
    try:
        n_days = max((date.fromisoformat(end) - date.fromisoformat(start)).days, 1)
        discount_ann = (1.0 + discount) ** (TRADING_DAYS_PER_YEAR / n_days) - 1.0
    except ValueError:
        discount_ann = float("nan")

    return {
        "start_date": start,
        "end_date": end,
        "ref_date": ref,
        "suspend_days": suspend_days,
        "n_survivor": len(surv_rets),
        "n_suspended_used": len(full_rets) - len(surv_rets),
        "survivor_pool_return": round(surv_ret, 6),
        "full_pool_return": round(full_ret, 6),
        "discount": round(discount, 6),
        "discount_annualized": round(discount_ann, 6) if discount_ann == discount_ann else None,
        "note": (
            "口径：区间等权买入持有（首日收盘买入→末日收盘卖出）；"
            "停牌近似池收益按数据截至停牌前收盘计算；"
            "折价系数=全样本近似池收益-存续池收益（正=存续池虚高）；"
            "该值为停牌风险下界，真退市股损失通常更极端。"
        ),
    }


# ═══════════════════════════════════════════════
#  3. 逐期存续池构建
# ═══════════════════════════════════════════════

def build_point_in_time_pool(
    symbols_with_last_date: Dict[str, str],
    as_of_date: str,
) -> List[str]:
    """逐期存续池构建：给定时点，池 = 最后交易日 >= as_of 的标的。

    任务卡整改内容 1（股票池按"当时时点是否存续"逐期构建）的逻辑显式化：
    当前数据源无退市股时，结果等效于 processed 池；未来接入退市/停牌
    标的完整数据后，本函数自动按 as_of 过滤，杜绝"今日是否存续"回溯。

    Args:
        symbols_with_last_date: {symbol: 最后交易日 YYYY-MM-DD}。
        as_of_date: 判定时点 YYYY-MM-DD（可注入，防未来）。

    Returns:
        该时点存续的标的列表（升序）。

    Raises:
        ValueError: as_of_date 格式非法时抛出。
    """
    try:
        as_of = date.fromisoformat(as_of_date)
    except ValueError as exc:
        raise ValueError(f"as_of_date 格式非法: {as_of_date}") from exc
    out: List[str] = []
    for sym, last in symbols_with_last_date.items():
        if not last:
            continue
        try:
            last_dt = date.fromisoformat(str(last))
        except ValueError:
            continue
        if last_dt >= as_of:
            out.append(str(sym))
    return sorted(out)


__all__ = [
    "DEFAULT_SUSPEND_DAYS",
    "MIN_TRADING_DAYS",
    "TRADING_DAYS_PER_YEAR",
    "CoverageAudit",
    "audit_data_coverage",
    "estimate_survivorship_discount",
    "build_point_in_time_pool",
]
