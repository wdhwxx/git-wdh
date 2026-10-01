# 文件名：myquant/data_access/factor_sync_service.py
"""
复权因子同步服务（方案B #82）
================================
拉取 A 股全市场除权除息事件的后复权因子，写入 ``adjust_factor`` 表。

背景（P0-1 复权口径修复的配套）：
  - quant.db 行情统一为不复权原始价（adjust='raw'），不再存储 hfq/qfq 序列
  - 消费端按需变换：hfq价 = raw价 × factor（事件日及之后生效）
  - qfq价 = hfq价 / 最新因子（最新因子按查询日截断，防未来函数）

数据源（2026-10-01 实证）：
  - akshare ``stock_zh_a_daily(symbol, adjust="hfq-factor")``：东财复权因子事件表
    返回全历史除权事件（date + hfq_factor，含 1900-01-01 脏行需清洗）
  - 唯一可用路径：stock_zh_a_xdxr 不存在 / stock_zh_a_hist 被网络拒绝

业务职责（按治理规则归 myquant，scripts/ 仅做薄入口）：
  1. 逐标的拉取东财 hfq-factor 事件表
  2. 清洗：去 1900-01-01 脏行 / NaN / 非法因子，日期升序去重
  3. UPSERT 写入 adjust_factor(symbol, event_date, factor)
  4. 全市场同步：UPSERT 幂等（可断点续跑）、限速、失败留痕
  5. 消费端查询：get_factor_lookup / get_latest_factor（供清洗跳变豁免与回测复权现算）

【防未来函数】
  - 因子为历史已发生除权事件的公告数据，事件日确定、入库即事实
  - 消费端仅使用 <= 查询日的最新因子（get_factor_lookup/get_latest_factor 按 as_of 截断）
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from myquant.core.logger import get_logger
from myquant.database import get_connection

logger = get_logger(__name__)

#: 东财 hfq-factor 事件表的脏行阈值（A 股最早 1990-12-19 开业，之前的事件为脏数据）
MIN_EVENT_DATE: str = "1990-12-19"

#: 单标的拉取失败重试次数
DEFAULT_RETRIES: int = 2

#: 默认拉取间隔（秒，限速保护，全市场 5317 只 ≈ 26 分钟）
DEFAULT_SLEEP: float = 0.3


def _import_akshare():
    """懒加载 akshare（测试可注入 stub 避免网络）"""
    import akshare  # noqa: PLC0415

    return akshare


class FactorSyncService:
    """复权因子同步服务。

    用法::

        svc = FactorSyncService(project_root)
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
        """DB 符号转 akshare 符号：'600519.SH' -> 'sh600519'、'000001.SZ' -> 'sz000001'。"""
        code, market = symbol.split(".", 1)
        return market.lower() + code

    # ─────────────────────────────────────────
    # 拉取与清洗
    # ─────────────────────────────────────────

    def fetch_factor_events(
        self,
        symbol: str,
        retries: int = DEFAULT_RETRIES,
    ) -> Optional[List[Tuple[str, float]]]:
        """拉取单标的全历史除权因子事件，返回 [(event_date, factor), ...]（升序）。

        失败重试（指数退避），重试仍失败返回 None（调用方留痕，不中断全市场同步）。
        """
        ak_symbol = self.to_ak_symbol(symbol)
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                df = self._ak.stock_zh_a_daily(
                    symbol=ak_symbol,
                    start_date="19900101",
                    end_date=time.strftime("%Y%m%d"),
                    adjust="hfq-factor",
                )
            except Exception as e:  # 网络/接口异常
                last_err = e
                logger.warning(f"拉取因子失败[{symbol}] 第{attempt + 1}次: {type(e).__name__}: {e}")
                if attempt < retries:
                    time.sleep(0.5 * (2**attempt))
                continue
            return self._clean_events(df, symbol)
        logger.error(f"拉取因子重试耗尽[{symbol}]: {last_err}")
        return None

    @staticmethod
    def _clean_events(df: pd.DataFrame, symbol: str) -> List[Tuple[str, float]]:
        """清洗东财 hfq-factor 事件表：去 1900 脏行 / NaN / 非法因子，升序去重。"""
        if df is None or df.empty:
            return []
        if "date" not in df.columns or "hfq_factor" not in df.columns:
            logger.warning(f"因子表缺列[{symbol}]: {list(df.columns)}")
            return []
        d = pd.DataFrame({"date": pd.to_datetime(df["date"], errors="coerce"),
                          "factor": pd.to_numeric(df["hfq_factor"], errors="coerce")})
        # 去解析失败行
        d = d.dropna(subset=["date", "factor"])
        # 去 1900 脏行（A 股开业之前）
        d = d[d["date"] >= pd.Timestamp(MIN_EVENT_DATE)]
        # 去非法因子（<=0）
        d = d[d["factor"] > 0]
        if d.empty:
            return []
        # 同日去重（保留最后一次出现），按日期升序
        d = d.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        return [(r.date.strftime("%Y-%m-%d"), float(r.factor)) for r in d.itertuples()]

    # ─────────────────────────────────────────
    # 入库（UPSERT 幂等）
    # ─────────────────────────────────────────

    def upsert_events(
        self,
        symbol: str,
        events: List[Tuple[str, float]],
        data_source: str = "akshare_em",
    ) -> int:
        """批量 UPSERT 因子事件，返回写入行数（幂等，可重复执行）。"""
        if not events:
            return 0
        sql = (
            "INSERT INTO adjust_factor(symbol, event_date, factor, data_source, created_at) "
            "VALUES(?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(symbol, event_date) DO UPDATE SET "
            "  factor = excluded.factor, data_source = excluded.data_source"
        )
        params = [(symbol, ev_date, factor, data_source) for ev_date, factor in events]
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
        events = self.fetch_factor_events(symbol)
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
        """全市场同步（单线程逐标的 + 限速，UPSERT 幂等可断点续跑）。

        Parameters
        ----------
        symbols : 标的列表
        sleep : 每次拉取间隔（秒）
        batch_commit : 每 N 只提交一次事务
        on_progress : 可选回调（done, total, ok, fail）

        Returns
        -------
        {"total", "ok", "no_events", "fail", "events_upserted", "failed_symbols", "elapsed_sec"}
        """
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
                logger.info(f"因子同步进度 {i}/{total} ok={ok} no_events={no_events} fail={fail}")

        elapsed = round(time.time() - t0, 1)
        logger.info(
            f"因子同步完成 {total} 只: ok={ok} no_events={no_events} fail={fail} "
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
    # 消费端查询（供清洗豁免 / data_loader 复权现算）
    # ─────────────────────────────────────────

    def get_factor_lookup(self, symbol: str, as_of: Optional[str] = None) -> Dict[str, float]:
        """返回 {event_date: factor}（<= as_of 的事件；as_of=None 返回全部）。

        防未来函数：消费端应传查询日（如回测 end_date），仅使用当日已知因子。
        """
        if as_of:
            rows = self._conn.execute(
                "SELECT event_date, factor FROM adjust_factor "
                "WHERE symbol=? AND event_date<=? ORDER BY event_date",
                (symbol, as_of),
            )
        else:
            rows = self._conn.execute(
                "SELECT event_date, factor FROM adjust_factor "
                "WHERE symbol=? ORDER BY event_date",
                (symbol,),
            )
        return {r["event_date"]: r["factor"] for r in rows}

    def get_latest_factor(self, symbol: str, as_of: Optional[str] = None) -> Optional[float]:
        """返回 <= as_of 的最新因子（None 表示该标的无因子事件）。"""
        lookup = self.get_factor_lookup(symbol, as_of=as_of)
        if not lookup:
            return None
        # 字典按日期升序插入，取最后
        return lookup[max(lookup)]
