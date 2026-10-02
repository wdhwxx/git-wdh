# 文件名：myquant/data_access/share_change_sync_service.py
"""
share_change_sync_service.py - 真实股本变动同步服务（P1-② #83）
===============================================================
拉取 A 股全市场股本变动事件（东财 stock_zh_a_gbjg_em：总股本/已上市流通A股/变更日期），
写入 ``share_change`` 表，供清洗派生量（换手率/流通股本/市盈动）按交易日对齐真实股本。

架构：存储一个事实版本（股本变动事件）+ 消费端变换（清洗按 change_date<=trade_date 对齐）
防未来函数：消费端传查询日，仅使用当日已知股本（change_date<=as_of）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from myquant.core.logger import get_logger
from myquant.database.connection import get_connection

logger = get_logger(__name__)

DEFAULT_RETRIES: int = 2
DEFAULT_SLEEP: float = 0.3
MIN_CHANGE_DATE: str = "1990-12-19"  # A 股开业（日历最早）


def _import_akshare():
    import akshare as ak

    return ak


class ShareChangeSyncService:
    """真实股本变动同步服务。

    用法::

        svc = ShareChangeSyncService(project_root)
        svc.sync_symbol("600519.SH")          # 单标的
        svc.sync_market(symbols, sleep=0.3)   # 全市场（UPSERT 幂等可续跑）
    """

    def __init__(
        self,
        project_root: Path | str,
        db_path: Optional[Path] = None,
        akshare_module: Any = None,
    ) -> None:
        if not project_root:
            raise ValueError(f"项目根目录不存在: {project_root!r}")
        self.project_root = Path(project_root)
        self.db_path = Path(db_path) if db_path else (self.project_root / "database" / "quant.db")
        if not self.db_path.exists():
            raise FileNotFoundError(f"数据库文件不存在: {self.db_path}")
        self._conn = get_connection(self.db_path)
        self._ak = akshare_module or _import_akshare()

    # ─────────────────────────────────────────
    # 符号转换
    # ─────────────────────────────────────────

    @staticmethod
    def to_ak_symbol(symbol: str) -> str:
        """DB 符号转 akshare 六位代码：'600519.SH' -> '600519'（东财 _em 接口用六位）。"""
        code, _market = symbol.split(".", 1)
        return code

    # ─────────────────────────────────────────
    # 拉取与清洗
    # ─────────────────────────────────────────

    def fetch_share_change(
        self,
        symbol: str,
        retries: int = DEFAULT_RETRIES,
    ) -> Optional[List[Tuple[str, float, float, str]]]:
        """拉取单标的全历史股本变动事件，返回
        [(change_date, total_share, float_share, reason), ...]（升序）。

        失败重试（指数退避），重试仍失败返回 None（调用方留痕，不中断全市场同步）。
        """
        ak_code = self.to_ak_symbol(symbol)
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                df = self._ak.stock_zh_a_gbjg_em(symbol=ak_code)
            except Exception as e:  # 网络/接口异常
                last_err = e
                logger.warning(f"拉取股本变动失败[{symbol}] 第{attempt + 1}次: {type(e).__name__}: {e}")
                if attempt < retries:
                    time.sleep(0.5 * (2**attempt))
                continue
            return self._clean_records(df, symbol)
        logger.error(f"拉取股本变动重试耗尽[{symbol}]: {last_err}")
        return None

    @staticmethod
    def _clean_records(
        df: pd.DataFrame, symbol: str
    ) -> List[Tuple[str, float, float, str]]:
        """清洗东财股本变动表：变更日期/总股本/已上市流通A股/变动原因，升序去重。"""
        if df is None or df.empty:
            return []
        need = {"变更日期", "总股本", "已上市流通A股"}
        if not need.issubset(df.columns):
            logger.warning(f"股本变动表缺列[{symbol}]: {list(df.columns)}")
            return []
        d = pd.DataFrame({
            "change_date": pd.to_datetime(df["变更日期"], errors="coerce"),
            "total_share": pd.to_numeric(df["总股本"], errors="coerce"),
            "float_share": pd.to_numeric(df["已上市流通A股"], errors="coerce"),
            "reason": df["变动原因"].fillna("").astype(str) if "变动原因" in df.columns else "",
        })
        d = d.dropna(subset=["change_date"])
        d = d[d["change_date"] >= pd.Timestamp(MIN_CHANGE_DATE)]
        d = d.dropna(subset=["total_share", "float_share"])
        d = d[d["total_share"] > 0]
        if d.empty:
            return []
        d = d.drop_duplicates(subset=["change_date"], keep="last").sort_values("change_date")
        out = []
        for r in d.itertuples():
            f_sh = float(r.float_share) if r.float_share and r.float_share > 0 else 0.0
            out.append((r.change_date.strftime("%Y-%m-%d"), float(r.total_share), f_sh, str(r.reason)))
        return out

    # ─────────────────────────────────────────
    # 入库（UPSERT 幂等）
    # ─────────────────────────────────────────

    def upsert_events(
        self,
        symbol: str,
        events: List[Tuple[str, float, float, str]],
        data_source: str = "akshare_em",
    ) -> int:
        """批量 UPSERT 股本变动事件，返回写入行数（幂等，可重复执行）。"""
        if not events:
            return 0
        sql = (
            "INSERT INTO share_change(symbol, change_date, total_share, float_share, change_reason, data_source, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(symbol, change_date) DO UPDATE SET "
            "  total_share = excluded.total_share, float_share = excluded.float_share,"
            "  change_reason = excluded.change_reason, data_source = excluded.data_source"
        )
        params = [(symbol, cd, ts, fs, rs, data_source) for cd, ts, fs, rs in events]
        with self._conn.transaction():
            n = self._conn.executemany(sql, params)
        return n

    # ─────────────────────────────────────────
    # 同步
    # ─────────────────────────────────────────

    def sync_symbol(self, symbol: str) -> Tuple[Optional[int], str]:
        """同步单标的：拉取 → 清洗 → UPSERT。

        Returns
        -------
        (行数, 状态) : 状态 'ok' / 'no_events' / 'fail: <原因>'
        """
        events = self.fetch_share_change(symbol)
        if events is None:
            return (None, f"fail: 拉取失败({symbol})")
        if not events:
            return (0, "no_events")
        try:
            n = self.upsert_events(symbol, events)
            return (n, "ok")
        except Exception as e:
            return (None, f"fail: 入库异常 {type(e).__name__}: {e}")

    def sync_market(
        self,
        symbols: List[str],
        sleep: float = DEFAULT_SLEEP,
        batch_commit: int = 50,
        on_progress=None,
    ) -> Dict[str, Any]:
        """全市场同步（单线程逐标的 + 限速，UPSERT 幂等可断点续跑）。"""
        total = len(symbols)
        ok = no_events = fail = 0
        failed_symbols: List[str] = []
        events_upserted = 0
        t0 = time.time()

        for i, symbol in enumerate(symbols, start=1):
            n, status = self.sync_symbol(symbol)
            if status == "ok":
                ok += 1
                events_upserted += n or 0
            elif status == "no_events":
                no_events += 1
            else:
                fail += 1
                failed_symbols.append(symbol)
            if batch_commit > 0 and i % batch_commit == 0:
                self._conn.commit()
            if sleep > 0 and i < total:
                time.sleep(sleep)
            if on_progress and (i % 50 == 0 or i == total):
                on_progress(i, total, ok, fail)
            if i % 100 == 0:
                logger.info(f"股本同步进度 {i}/{total} ok={ok} no_events={no_events} fail={fail}")

        elapsed = round(time.time() - t0, 1)
        logger.info(
            f"股本同步完成 {total} 只: ok={ok} no_events={no_events} fail={fail} "
            f"事件={events_upserted} 耗时={elapsed}s"
        )
        return {
            "total": total,
            "ok": ok,
            "no_events": no_events,
            "fail": fail,
            "events_upserted": events_upserted,
            "failed_symbols": failed_symbols,
            "elapsed_sec": elapsed,
        }

    # ─────────────────────────────────────────
    # 消费端查询（供清洗派生量按日对齐）
    # ─────────────────────────────────────────

    def get_share_lookup(
        self, symbol: str, as_of: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """返回该标的股本变动事件（<= as_of；None=全部），按 change_date 升序。

        防未来函数：消费端应传查询日（如清洗日 trade_date），仅使用当日已知股本。
        """
        if as_of:
            rows = self._conn.execute(
                "SELECT change_date, total_share, float_share FROM share_change "
                "WHERE symbol=? AND change_date<=? ORDER BY change_date",
                (symbol, as_of),
            )
        else:
            rows = self._conn.execute(
                "SELECT change_date, total_share, float_share FROM share_change "
                "WHERE symbol=? ORDER BY change_date",
                (symbol,),
            )
        return [dict(r) for r in rows]
