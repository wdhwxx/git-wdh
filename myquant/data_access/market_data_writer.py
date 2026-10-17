# 文件名：myquant/data_access/market_data_writer.py
"""
统一写入口 MarketDataWriter（数据层施工 C-03 #452）
======================================================
依据《数据层结构优化可行性施工方案报告_v1.1》§4.3(a)：
  - write_raw：过门禁①结构校验 + 打 ingest 三列，写入原始事实行（承接 C-01 #450 接线逻辑）
  - write_upsert：INSERT OR REPLACE 按 (symbol, trade_date) 覆盖（承接 full_clean 两处写回语义；
    UPSERT + 旧值 correction_ledger 台账由 C-02 #453 升级，本模块先收敛不改语义）
  - 写失败可观测：不静默 return 0（报告点名 manager 异常吞噬），记 error 日志并返回失败计数。

所有 market_data 表写入必须经本入口（3 条路径收敛）；门禁/计数/元数据随行。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from myquant.data_access.admission import AdmissionChecker

logger = logging.getLogger(__name__)


class MarketDataWriteError(Exception):
    """MarketDataWriter 写失败（调用方可捕获做状态降级/告警）。"""


class MarketDataWriter:
    """market_data 表唯一写入口（薄封装 DataQueries，写失败可观测）。"""

    def __init__(
        self,
        queries: Any,
        admission_checker: Optional[AdmissionChecker] = None,
    ) -> None:
        """初始化写入口。

        Args:
            queries: DataQueries 实例（已绑定 DatabaseConnection）
            admission_checker: 门禁①判定器；None 时惰性构造（默认关闭=直通）
        """
        self._queries = queries
        self._admission_checker = admission_checker

    # ── 门禁①（C-01 #450 逻辑迁入，行为等价）────────────────
    def _check_admission(
        self,
        row: Dict[str, Any],
        symbol: str,
        data_source: str,
        prev_row: Optional[Dict[str, Any]],
    ) -> str:
        """单行门禁判定：返回 BLOCK / FLAG 标记串 / PASS。"""
        if self._admission_checker is None:
            from myquant.data_access.admission import AdmissionChecker
            self._admission_checker = AdmissionChecker()
        if not self._admission_checker.is_enabled():
            return "PASS"
        # 字段映射：原始行 date 键 → 门禁① trade_date 键（B-05 #445 同款修正）
        admission_row = dict(row)
        admission_row.setdefault("trade_date", row.get("date", ""))
        result = self._admission_checker.check(
            admission_row, symbol, data_source, prev_row=prev_row,
        )
        if result.decision == "BLOCK":
            self._queries.record_admission_reject(
                symbol=symbol,
                rule=result.reasons[0].split(":")[0] if result.reasons else "admission_block",
                detail="|".join(result.reasons),
                source=data_source,
                trade_date=str(row.get("date", "") or None),
                level="BLOCK",
            )
            return "BLOCK"
        if result.decision == "FLAG":
            return "|".join(result.flags)
        return "PASS"

    # ── 原始行写入（增量采集，含门禁 + ingest 三列）──────────
    def write_raw(
        self,
        rows: List[Dict[str, Any]],
        symbol: str,
        market: str,
        period: str,
        adjust: Optional[str],
        data_source: str,
        ingest_time: str,
        ingest_batch: str,
        prev_row: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        """批量写入原始事实行（过门禁①：BLOCK 拒收留痕 / FLAG 落 anomaly_flags）。

        Args:
            rows: 原始行情行（date/open/high/low/close/volume/amount/pct_chg/turnover）
            symbol / market / period / adjust: 标的与标签
            data_source: 数据源标识
            ingest_time / ingest_batch: B-02 采集元数据
            prev_row: 首行判定上下文（DB 末根），None 时无上下文

        Returns:
            {"written": 实际写入行数, "blocked": 拒收行数, "flagged": 标记行数}

        Raises:
            MarketDataWriteError: 数据库写失败（不静默，记 error 日志）
        """
        db_data: List[Dict[str, Any]] = []
        blocked = flagged = 0
        cur_prev = prev_row
        for row in rows:
            entry = {
                "symbol": symbol,
                "market": market,
                "period": period,
                "adjust": adjust,
                "trade_date": row.get("date", ""),
                "trade_time": row.get("time"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "amount": row.get("amount"),
                "pct_chg": row.get("pct_chg"),
                "turnover": row.get("turnover"),
                "is_suspended": 1 if row.get("volume", 0) == 0 else 0,
                "data_source": data_source,
                "ingest_time": ingest_time,
                "ingest_batch": ingest_batch,
                "anomaly_flags": None,
            }
            verdict = self._check_admission(row, symbol, data_source, cur_prev)
            if verdict == "BLOCK":
                blocked += 1
                continue
            if verdict != "PASS":
                flagged += 1
                entry["anomaly_flags"] = verdict
            db_data.append(entry)
            cur_prev = row

        written = 0
        if db_data:
            try:
                written = self._queries.bulk_insert_market_data(db_data)
            except Exception as exc:
                # C-03 #452：写失败可观测——记 error 日志并显式抛出，禁止静默 return 0
                logger.error("MarketDataWriter.write_raw 写失败 [%s]: %s", symbol, exc)
                raise MarketDataWriteError(f"write_raw 失败 [{symbol}]: {exc}") from exc
        return {"written": written, "blocked": blocked, "flagged": flagged}

    # ── 清洗写回（INSERT OR REPLACE，C-02 升级 UPSERT+台账）──
    def write_upsert(
        self,
        rows: List[Dict[str, Any]],
        symbol: str,
    ) -> int:
        """批量 upsert 写入（INSERT OR REPLACE，保留 C-02 前语义）。

        Args:
            rows: 已清洗/构造好的 DB 行（含 symbol/trade_date 等完整字段）
            symbol: 标的（日志定位用）

        Returns:
            写入行数

        Raises:
            MarketDataWriteError: 数据库写失败（不静默）
        """
        if not rows:
            return 0
        try:
            return self._queries.bulk_insert_market_data(rows, replace=True)
        except Exception as exc:
            logger.error("MarketDataWriter.write_upsert 写失败 [%s]: %s", symbol, exc)
            raise MarketDataWriteError(f"write_upsert 失败 [{symbol}]: {exc}") from exc
