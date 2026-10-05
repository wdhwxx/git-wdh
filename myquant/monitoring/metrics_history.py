# 文件名：myquant/monitoring/metrics_history.py
"""
metrics_history.py - 指标时序存储层（BASE-2 指标时序存储）
========================================================================
依据《量化系统监控模块施工总清单_V1.2》BASE-2：
  - metrics_history 表（name/labels_json/value/timestamp + 按天分区索引）
  - scheduler 周期落库（默认 60s，巡检/指标快照写入）
  - 保留策略（原始 30 天、降采样 1 年：原始行定期清理，降采样查询
    按时间桶聚合，无需单独建降采样表）
  - 约束（R-02）：本表仅服务趋势/基线/看板回放，不得作为任何 P0 实时
    告警的检测来源；P0 实时告警一律走事件订阅。

本模块职责：
  1. 建表自检（幂等，DDL 从 myquant.database.schema 导入，与迁移 0006 逐字一致）
  2. 指标写入（INSERT OR IGNORE，name+labels_json+timestamp 唯一防重复）
  3. 批量写入（executemany，事务包裹，scheduler 周期落库用）
  4. 查询（name/labels/时间范围/limit 过滤，趋势与基线回放）
  5. 降采样聚合（按 epoch 时间桶 avg/max/min/sum/count，支撑 1 年回放）
  6. 保留清理（purge 超过保留期的原始行）

防未来铁律：timestamp 一律取"采集/落库时刻"（scheduler 巡检时刻或调用方
注入的时间戳），只记录已发生的数据，不预测未来；落库失败不阻断巡检/
告警主流程（记 error 留痕）。

依赖: json, time, logging, typing, myquant.database.connection / schema
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Union

from myquant.database.connection import DatabaseConnection, get_connection
from myquant.database.schema import (
    CREATE_METRICS_HISTORY_INDEX_SQL,
    CREATE_METRICS_HISTORY_SQL,
)

logger = logging.getLogger("myquant.monitoring.metrics_history")


# ═══════════════════════════════════════════════
#  保留策略常量（与施工清单 BASE-2 对齐）
# ═══════════════════════════════════════════════

DEFAULT_RETENTION_DAYS: int = 30       # 原始数据保留天数
DEFAULT_DOWNSAMPLE_DAYS: int = 365     # 降采样查询可回放天数（1 年）
DEFAULT_BUCKET_MINUTES: int = 60       # 降采样默认时间桶（分钟）
MAX_BATCH_SIZE: int = 1000             # 单次批量写入上限（防超大事务）
_AGG_WHITELIST: tuple = ("avg", "max", "min", "sum", "count")


class MetricsHistoryStore:
    """
    指标时序存储（SQLite，线程安全，事务写入；仅服务回放，非实时告警源）。

    Usage:
        store = MetricsHistoryStore()
        store.save_metric("equity_curve", 1_000_000.0, labels={"strategy": "A"})
        rows = store.query("equity_curve", start_ts=..., end_ts=...)
        buckets = store.query_downsampled("equity_curve", bucket_minutes=60)
        purged = store.retention_purge(retention_days=30)

    R-02 约束：本表仅服务趋势/基线/看板回放；任何 P0 实时告警不得依赖
    本表检测（P0 实时告警一律走事件订阅，见施工清单 R-02）。
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Any]] = None,
        connection: Optional[DatabaseConnection] = None,
    ):
        """
        Parameters
        ----------
        db_path : str | Path | None
            数据库路径；None 则用全局默认（项目根/database/quant.db）
        connection : DatabaseConnection | None
            注入连接（测试用临时库）；与 db_path 互斥，优先 connection
        """
        if connection is not None:
            self._conn: DatabaseConnection = connection
        else:
            self._conn = get_connection(db_path)
        self._ensure_schema()

    # ── 建表自检（幂等，与迁移 DDL 逐字一致）──

    def _ensure_schema(self) -> None:
        """确保 metrics_history 表与索引存在（幂等，启动/实例化时自检）。"""
        with self._conn.transaction():
            self._conn.execute_update(CREATE_METRICS_HISTORY_SQL)
            for sql in CREATE_METRICS_HISTORY_INDEX_SQL:
                self._conn.execute_update(sql)
        logger.debug("metrics_history 表自检完成（CREATE IF NOT EXISTS）")

    # ── 写：单条落库 ──

    @staticmethod
    def _dump_labels(labels: Optional[Dict[str, Any]]) -> str:
        """labels → 排序 JSON（key 排序保证同构 labels 唯一键一致）。"""
        return json.dumps(labels or {}, sort_keys=True, ensure_ascii=False)

    def save_metric(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ) -> bool:
        """
        写入一个指标点（INSERT OR IGNORE，name+labels_json+timestamp 唯一）。

        Parameters
        ----------
        name : str
            指标名（P0-5 命名规范：领域_对象_动作_单位，如 equity_curve_total）
        value : float
            指标值（须为有限数值）
        labels : dict | None
            标签（如 {"strategy": "A"}），JSON 序列化后落库
        timestamp : float | None
            采集/落库时刻（epoch 秒）；None 取当前时刻（本行数据截至该时刻，
            不引用未来数据）

        Returns
        -------
        bool : True=新增一行；False=同 key 同秒已存在被忽略（幂等）

        Raises
        ------
        ValueError: name 为空、value 非有限数值、labels 非 dict 时抛出
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("落库失败: 指标名 name 不能为空")
        if labels is not None and not isinstance(labels, dict):
            raise ValueError(f"落库失败: labels 必须为 dict，收到 {type(labels).__name__}")
        try:
            num_value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"落库失败: value 必须为数值，收到 {value!r}")
        if num_value != num_value or num_value in (float("inf"), float("-inf")):
            raise ValueError(f"落库失败: value 必须为有限数值，收到 {value!r}")

        ts = time.time() if timestamp is None else float(timestamp)
        labels_json = self._dump_labels(labels)
        with self._conn.transaction():
            affected = self._conn.execute_update(
                """
                INSERT OR IGNORE INTO metrics_history (name, labels_json, value, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (name, labels_json, num_value, ts),
            )
        return affected > 0

    def save_batch(self, rows: List[Dict[str, Any]]) -> int:
        """
        批量写入指标点（executemany，事务包裹；scheduler 周期落库主入口）。

        Parameters
        ----------
        rows : list[dict]
            每项含 name/value，可选 labels/timestamp，如:
            [{"name": "patrol_healthy_count", "value": 4, "labels": {...}, "timestamp": 1234.5}]

        Returns
        -------
        int : 实际新增行数（重复点被忽略不计入）

        Raises
        ------
        ValueError: rows 为空/超批量上限/含非法项时抛出
        """
        if not rows:
            return 0
        if len(rows) > MAX_BATCH_SIZE:
            raise ValueError(
                f"批量写入超上限: {len(rows)} > MAX_BATCH_SIZE={MAX_BATCH_SIZE}"
            )
        prepared = []
        now = time.time()
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"批量写入失败: 第 {i} 项不是 dict（{type(row).__name__}）")
            name = (row.get("name") or "").strip()
            if not name:
                raise ValueError(f"批量写入失败: 第 {i} 项缺少 name")
            try:
                value = float(row["value"])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"批量写入失败: 第 {i} 项 value 非法（{row.get('value')!r}）")
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"批量写入失败: 第 {i} 项 value 非有限数值")
            labels = row.get("labels")
            if labels is not None and not isinstance(labels, dict):
                raise ValueError(f"批量写入失败: 第 {i} 项 labels 非 dict")
            ts = row.get("timestamp", now)
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                raise ValueError(f"批量写入失败: 第 {i} 项 timestamp 非法")
            prepared.append((name, self._dump_labels(labels), value, ts))

        with self._conn.transaction():
            affected = self._conn.executemany(
                """
                INSERT OR IGNORE INTO metrics_history (name, labels_json, value, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                prepared,
            )
        return affected

    # ── 读：查询 ──

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        """sqlite3.Row → dict（labels_json 反序列化，坏 JSON 记 error 不静默吞）。"""
        try:
            labels = json.loads(row["labels_json"] or "{}")
        except ValueError:
            logger.error("metrics_history 行 labels_json 解析失败: id=%s", row["id"])
            labels = {}
        return {
            "id": row["id"],
            "name": row["name"],
            "labels": labels,
            "value": row["value"],
            "timestamp": row["timestamp"],
        }

    def query(
        self,
        name: Optional[str] = None,
        labels: Optional[Dict[str, Any]] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        limit: int = 1000,
        order: str = "desc",
    ) -> List[Dict[str, Any]]:
        """
        查询指标点（趋势/基线/看板回放；R-02：不用于 P0 实时告警检测）。

        Parameters
        ----------
        name : str | None
            按指标名精确过滤；None=不限
        labels : dict | None
            按标签精确匹配（sort_keys JSON 相等）；None=不限
        start_ts / end_ts : float | None
            时间范围（epoch 秒，闭区间）；None=不限
        limit : int
            返回条数上限（>0，默认 1000）
        order : str
            "desc"=按时间倒序（最新在前，默认）；"asc"=升序

        Returns
        -------
        list[dict] : 每项 {id/name/labels/value/timestamp}
        """
        if limit <= 0:
            raise ValueError(f"limit 必须为正整数，收到 {limit}")
        if order not in ("asc", "desc"):
            raise ValueError(f"order 必须为 asc/desc，收到 {order!r}")

        clauses: List[str] = []
        params: List[Any] = []
        if name:
            clauses.append("name = ?")
            params.append(name)
        if labels is not None:
            if not isinstance(labels, dict):
                raise ValueError("labels 必须为 dict")
            clauses.append("labels_json = ?")
            params.append(self._dump_labels(labels))
        if start_ts is not None:
            clauses.append("timestamp >= ?")
            params.append(float(start_ts))
        if end_ts is not None:
            clauses.append("timestamp <= ?")
            params.append(float(end_ts))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT id, name, labels_json, value, timestamp FROM metrics_history{where}"
            f" ORDER BY timestamp {order} LIMIT ?"
        )
        params.append(limit)
        with self._conn.transaction():
            rows = self._conn.execute(sql, params)
        return [self._row_to_dict(r) for r in rows]

    def query_downsampled(
        self,
        name: str,
        bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        agg: str = "avg",
    ) -> List[Dict[str, Any]]:
        """
        降采样聚合查询（按 epoch 时间桶 GROUP BY，支撑 1 年历史回放）。

        Parameters
        ----------
        name : str
            指标名（必填）
        bucket_minutes : int
            时间桶大小（分钟，>0）
        start_ts / end_ts : float | None
            时间范围（epoch 秒）；None=不限
        agg : str
            聚合函数：avg/max/min/sum/count

        Returns
        -------
        list[dict] : 每项 {bucket: 桶起点 epoch 秒, value: 聚合值, count: 桶内点数}
        """
        if not (name or "").strip():
            raise ValueError("降采样查询失败: name 不能为空")
        if bucket_minutes <= 0:
            raise ValueError(f"bucket_minutes 必须为正整数，收到 {bucket_minutes}")
        if agg not in _AGG_WHITELIST:
            raise ValueError(f"非法聚合函数: {agg!r}（须为 {list(_AGG_WHITELIST)}）")

        bucket_seconds = int(bucket_minutes) * 60
        agg_expr = "COUNT(*)" if agg == "count" else f"{agg}(value)"
        # 参数顺序与 SQL 占位符一一对应：桶宽×2 → name → 时间范围
        params: List[Any] = [bucket_seconds, bucket_seconds, name]
        if start_ts is not None:
            params.append(float(start_ts))
        if end_ts is not None:
            params.append(float(end_ts))
        if start_ts is not None:
            clauses = ["timestamp >= ?"]
        else:
            clauses = []
        if end_ts is not None:
            clauses.append("timestamp <= ?")
        where = " AND ".join(["name = ?"] + clauses)
        sql = (
            "SELECT CAST(timestamp / ? AS INTEGER) * ? AS bucket, "
            f"{agg_expr} AS value, COUNT(*) AS count "
            "FROM metrics_history "
            f"WHERE {where} "
            "GROUP BY bucket ORDER BY bucket ASC"
        )
        with self._conn.transaction():
            rows = self._conn.execute(sql, params)
        return [{"bucket": r["bucket"], "value": r["value"], "count": r["count"]} for r in rows]

    # ── 保留清理 ──

    def purge_before(self, cutoff_ts: float) -> int:
        """删除 timestamp < cutoff_ts 的原始行（保留策略执行）。"""
        cutoff = float(cutoff_ts)
        with self._conn.transaction():
            affected = self._conn.execute_update(
                "DELETE FROM metrics_history WHERE timestamp < ?",
                (cutoff,),
            )
        return affected

    def retention_purge(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        """清理超过保留期的原始行（默认 30 天；降采样查询仍可回放 1 年）。

        防未来：cutoff 按当前时刻回溯计算，只删除已发生的过期数据。
        """
        if retention_days <= 0:
            raise ValueError(f"retention_days 必须为正整数，收到 {retention_days}")
        cutoff = time.time() - retention_days * 86400.0
        return self.purge_before(cutoff)


# ═══════════════════════════════════════════════
#  自测（python -m myquant.monitoring.metrics_history）
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """独立自测：临时库跑通写/读/降采样/清理全链路，0=通过。"""
    import tempfile

    logger.info("MetricsHistoryStore 自测开始")
    passed = 0
    failed = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            logger.info("  ✓ %s", label)
        else:
            failed += 1
            logger.error("  ✗ %s %s", label, detail)

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        conn = DatabaseConnection(str(Path(tmp) / "self_test.db"))
        try:
            store = MetricsHistoryStore(connection=conn)

            # 建表自检
            from myquant.database.schema import TABLE_NAMES, table_exists

            check("建表自检", table_exists(conn, TABLE_NAMES["metrics_history"]))

            # 写入 + 唯一幂等
            base = 1_700_000_000.0
            check("单条写入", store.save_metric("equity_curve_total", 1_000_000.0, timestamp=base))
            check("同 key 同秒忽略", store.save_metric("equity_curve_total", 1_000_000.0, timestamp=base) is False)
            store.save_metric("equity_curve_total", 1_000_100.0, timestamp=base + 60)
            store.save_metric("patrol_status", 0.0, labels={"status": "healthy"}, timestamp=base + 120)

            # 查询
            rows = store.query("equity_curve_total", limit=10)
            check("按名查询", len(rows) == 2)
            check("倒序最新在前", rows[0]["timestamp"] == base + 60)
            rows = store.query("patrol_status", labels={"status": "healthy"})
            check("按标签过滤", len(rows) == 1 and rows[0]["value"] == 0.0)
            rows = store.query(start_ts=base, end_ts=base + 61, limit=10)
            check("时间范围过滤", len(rows) == 2)

            # 降采样聚合：3 点各占一个 60 分钟桶（base, base+1h, base+2h）
            for i in range(3):
                store.save_metric("sine_test", float(i), timestamp=base + 3600 * i)
            buckets = store.query_downsampled("sine_test", bucket_minutes=60, agg="avg")
            check(
                "降采样 60 分钟桶",
                len(buckets) == 3
                and buckets[0]["value"] == 0.0
                and buckets[1]["value"] == 1.0
                and buckets[2]["value"] == 2.0,
            )

            # 批量写入
            batch = [{"name": "batch_m", "value": float(i), "timestamp": base + 100 + i} for i in range(5)]
            check("批量写入", store.save_batch(batch) == 5)

            # 保留清理：timestamp < base+10 的行 = equity(base) + sine_test(base) 共 2 行
            old = store.purge_before(base + 10)
            check("清理旧数据", old == 2)
            check("清理后查询", len(store.query("equity_curve_total", limit=10)) == 1)

            # 防御式校验
            try:
                store.save_metric("", 1.0)
                check("空 name 报错", False)
            except ValueError:
                check("空 name 报错", True)
            try:
                store.query_downsampled("x", agg="median")
                check("非法 agg 报错", False)
            except ValueError:
                check("非法 agg 报错", True)
            try:
                store.query(limit=0)
                check("limit=0 报错", False)
            except ValueError:
                check("limit=0 报错", True)
        finally:
            conn.close()

    logger.info("自测结果: %d 通过, %d 失败", passed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(_run_self_test())
