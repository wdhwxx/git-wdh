# 文件名：myquant/monitoring/metrics_history.py
"""
metrics_history.py - 指标时序存储层（BASE-2 指标时序存储 + BASE-6 存储优化）
========================================================================
依据《量化系统监控模块施工总清单_V1.2》BASE-2 / BASE-6：
  - metrics_history 表（name/labels_json/value/timestamp + 按天分区索引）
  - scheduler 周期落库（默认 60s，巡检/指标快照写入）
  - 保留策略（原始 30 天、降采样 1 年：原始行定期清理，降采样查询
    按时间桶聚合，无需单独建降采样表）
  - 约束（R-02）：本表仅服务趋势/基线/看板回放，不得作为任何 P0 实时
    告警的检测来源；P0 实时告警一律走事件订阅。

BASE-6 存储优化新增职责：
  7. 异步批量写入（enqueue/flush/后台守护线程周期落库，避免热路径直写）
  8. 降采样物化（materialize_downsampled：旧数据按桶聚合物化落库，可回放
     1 年；DownsamplingTask 独立调度，interval 可配，默认每日）
  9. 迁移接口预留（MetricExporter ABC + 注册表 vm/clickhouse 占位，
     不立即换库；导出副本失败不阻断落库）
  10. WAL 模式 + busy_timeout 显式化（database/connection.py，高写入
     下写等待而非锁表）

防未来铁律：timestamp 一律取"采集/落库时刻"（scheduler 巡检时刻或调用方
注入的时间戳），只记录已发生的数据，不预测未来；落库失败不阻断巡检/
告警主流程（记 error 留痕）；降采样物化只聚合当前时刻之前已发生的数据。

依赖: json, time, logging, threading, collections, abc, typing,
      myquant.database.connection / schema
"""

from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Union

from myquant.database.connection import DatabaseConnection, get_connection
from myquant.database.schema import (
    CREATE_METRICS_HISTORY_INDEX_SQL,
    CREATE_METRICS_HISTORY_SQL,
)
from myquant.core.field_crypto import (
    _SENSITIVE_NAME_PATTERNS as _SENSITIVE_NAME_PATTERNS_DEFAULT,
    is_sensitive_name,
    mask_value,
)

logger = logging.getLogger("myquant.core.metrics_history")


# ═══════════════════════════════════════════════
#  保留策略常量（与施工清单 BASE-2/BASE-6 对齐）
# ═══════════════════════════════════════════════

DEFAULT_RETENTION_DAYS: int = 30       # 原始数据保留天数
DEFAULT_DOWNSAMPLE_DAYS: int = 365     # 降采样查询可回放天数（1 年）
DEFAULT_BUCKET_MINUTES: int = 60       # 降采样默认时间桶（分钟）
MAX_BATCH_SIZE: int = 1000             # 单次批量写入上限（防超大事务）
_AGG_WHITELIST: tuple = ("avg", "max", "min", "sum", "count")

# BASE-6：异步批量写入默认参数
DEFAULT_FLUSH_INTERVAL_SECONDS: float = 5.0   # 后台 flush 间隔（秒）
DEFAULT_QUEUE_LIMIT: int = 10_000             # 内存队列上限（防无界增长）
# BASE-6：降采样独立调度默认参数
DEFAULT_DOWNSAMPLE_INTERVAL_SECONDS: float = 86400.0  # 独立调度默认每日一次
DEFAULT_MATERIALIZE_OLDER_THAN_DAYS: int = 30         # 物化门槛：>=30 天前的旧数据
DEFAULT_DOWNSAMPLE_AGG: str = "avg"                   # 物化聚合函数


# ═══════════════════════════════════════════════
#  迁移接口预留（BASE-6：VictoriaMetrics/ClickHouse，不立即换库）
# ═══════════════════════════════════════════════

class MetricExporter(ABC):
    """
    指标迁移导出接口（BASE-6 预留）。

    预留语义：现系统 SQLite 为主存储；未来迁往 VictoriaMetrics/ClickHouse
    时，实现本接口并在 __init__/set_exporter 绑定，save 路径同步导出副本，
    平滑切换（不立即换库）。

    实现要求（防未来）：
      - export_batch 只接收已落库的数据（导出副本，不影响主存储事务）
      - 导出失败不得阻断主落库（调用方在 except 中记 warning）
    """

    @abstractmethod
    def export_batch(self, rows: List[Dict[str, Any]]) -> int:
        """导出一批指标点（返回导出成功条数）。"""

    def connect(self) -> None:
        """建立外部存储连接（可选实现）。"""

    def close(self) -> None:
        """关闭外部存储连接（可选实现）。"""


class NullMetricExporter(MetricExporter):
    """空导出器（默认）：导出即成功计数，不写外部存储（纯 SQLite 模式）。"""

    def export_batch(self, rows: List[Dict[str, Any]]) -> int:
        return len(rows)


class _UnavailableExporter(MetricExporter):
    """占位导出器（vm/clickhouse 预留）：调用时明确报错，说明未接入。"""

    def __init__(self, target: str):
        self._target = target

    def export_batch(self, rows: List[Dict[str, Any]]) -> int:
        raise NotImplementedError(
            f"{self._target} 导出器为 BASE-6 预留占位，尚未接入；"
            f"如需迁移请实现 MetricExporter 接口并注册"
        )


_EXPORTER_REGISTRY: Dict[str, Any] = {
    "null": NullMetricExporter,
    "vm": lambda: _UnavailableExporter("vm"),
    "clickhouse": lambda: _UnavailableExporter("clickhouse"),
}


def get_exporter(name: str) -> MetricExporter:
    """
    按名称获取导出器实例（BASE-6 迁移接口注册表）。

    Parameters
    ----------
    name : str
        "null"（默认，纯 SQLite）/"vm"（VictoriaMetrics 预留）/"clickhouse"（预留）

    Raises
    ------
    ValueError: 未注册的导出器名称
    """
    key = (name or "null").strip().lower()
    if key not in _EXPORTER_REGISTRY:
        raise ValueError(
            f"未注册的导出器: {name!r}（可选: {sorted(_EXPORTER_REGISTRY)}）"
        )
    factory = _EXPORTER_REGISTRY[key]
    return factory()


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
        # BASE-6：迁移导出器（默认空导出，纯 SQLite）
        self._exporter: MetricExporter = NullMetricExporter()
        # BASE-9：敏感字段加密（命中敏感名 → 密文落库 value_encrypted，
        # value 列存占位 0.0；未绑定且写入敏感名 → 拒绝明文落盘）
        self._field_crypto: Optional[Any] = None
        self._sensitive_patterns: tuple = _SENSITIVE_NAME_PATTERNS_DEFAULT
        # BASE-9：访问审计（读取/导出留痕，kind=access）
        self._access_audit: Optional[Any] = None
        # BASE-6：异步批量写入队列（内存缓冲，后台线程周期 flush，避免热路径直写）
        self._queue: Deque[Dict[str, Any]] = deque()
        self._queue_limit: int = DEFAULT_QUEUE_LIMIT
        self._queue_lock = threading.Lock()
        self._flush_thread: Optional[threading.Thread] = None
        self._flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS
        self._flush_stop = threading.Event()
        self._flush_running = False

    def attach_field_crypto(
        self,
        crypto: Any,
        sensitive_patterns: Optional[tuple] = None,
    ) -> "MetricsHistoryStore":
        """
        绑定敏感字段加密器（BASE-9：关键字段密文落盘，防泄露）。

        Parameters
        ----------
        crypto : FieldCrypto
            加密器（Fernet；密钥见 field_crypto 模块）
        sensitive_patterns : tuple | None
            敏感关键字集合覆盖；None 用默认（equity/balance/position/...）

        Returns
        -------
        MetricsHistoryStore : self
        """
        if crypto is None or not hasattr(crypto, "encrypt") or not hasattr(crypto, "try_decrypt"):
            raise ValueError("crypto 必须为 FieldCrypto 实例（含 encrypt/try_decrypt）")
        self._field_crypto = crypto
        if sensitive_patterns is not None:
            self._sensitive_patterns = tuple(sensitive_patterns)
        logger.info("[metrics] 敏感字段加密已绑定（关键字段密文落盘）")
        return self

    def attach_access_audit(self, audit: Any) -> "MetricsHistoryStore":
        """
        绑定访问审计（BASE-9：监控库读取/导出留痕，kind=access）。

        Parameters
        ----------
        audit : AuditLogStore
            BASE-7 审计只追加存储

        Returns
        -------
        MetricsHistoryStore : self
        """
        if audit is None or not hasattr(audit, "append"):
            raise ValueError("audit 必须为 AuditLogStore 实例（含 append）")
        self._access_audit = audit
        return self

    def _audit_access(self, action: str, detail: Dict[str, Any]) -> None:
        """监控库读取/导出访问留痕（kind=access；失败记 warning 不阻断）。"""
        if self._access_audit is None:
            return
        try:
            self._access_audit.append("access", action, actor="metrics_query", detail=detail)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"访问审计留痕失败（不阻断查询）: {e}")

    def _is_sensitive(self, name: str) -> bool:
        """敏感指标判定（可配置 patterns）。"""
        return is_sensitive_name(name, patterns=self._sensitive_patterns)

    def _encrypt_sensitive_value(self, name: str, value: float) -> Optional[str]:
        """
        敏感值加密（命中敏感名时）：未绑定加密器 → 拒绝明文落盘。

        Returns
        -------
        str | None : 密文（非敏感名为 None）；敏感名但无加密器时抛 ValueError
        """
        if not self._is_sensitive(name):
            return None
        if self._field_crypto is None:
            raise ValueError(
                f"拒绝明文落盘: 指标 {name!r} 命中敏感名但未绑定 FieldCrypto（BASE-9）"
            )
        return self._field_crypto.encrypt(value)

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
        # BASE-9：敏感指标密文落盘（value 列存占位 0.0，密文存 value_encrypted；
        # 未绑定加密器时拒绝明文落盘）
        encrypted = self._encrypt_sensitive_value(name, num_value)
        store_value = 0.0 if encrypted is not None else num_value
        with self._conn.transaction():
            affected = self._conn.execute_update(
                """
                INSERT OR IGNORE INTO metrics_history (name, labels_json, value, timestamp, value_encrypted)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, labels_json, store_value, ts, encrypted),
            )
        if affected > 0:
            self._export_rows(
                [{"name": name, "labels": labels or {}, "value": num_value, "timestamp": ts}]
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
            # BASE-9：敏感指标密文落盘（未绑定加密器时拒绝明文）
            encrypted = self._encrypt_sensitive_value(name, value)
            store_value = 0.0 if encrypted is not None else value
            prepared.append((name, self._dump_labels(labels), store_value, ts, encrypted))

        with self._conn.transaction():
            affected = self._conn.executemany(
                """
                INSERT OR IGNORE INTO metrics_history (name, labels_json, value, timestamp, value_encrypted)
                VALUES (?, ?, ?, ?, ?)
                """,
                prepared,
            )
        if affected > 0:
            self._export_rows(
                [
                    {
                        "name": n,
                        "labels": json.loads(lj or "{}"),
                        "value": v if e is None else float(self._field_crypto.decrypt(e)),
                        "timestamp": t,
                    }
                    for (n, lj, v, t, e) in prepared
                ]
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

    def _row_to_dict_decrypted(self, row: Any) -> Dict[str, Any]:
        """sqlite3.Row → dict；敏感行（value_encrypted 非空）解密回读真实值。"""
        out = self._row_to_dict(row)
        token = row["value_encrypted"]
        if token:
            if self._field_crypto is not None:
                plain = self._field_crypto.try_decrypt(token)
                if plain is not None:
                    try:
                        out["value"] = float(plain)
                    except (TypeError, ValueError):
                        out["value"] = plain
                else:
                    out["value"] = mask_value(out["value"])  # 解密失败脱敏
            else:
                # 库里有密文但未绑定加密器：脱敏（不泄露，不崩溃）
                out["value"] = mask_value(out["value"])
        return out

    def query(
        self,
        name: Optional[str] = None,
        labels: Optional[Dict[str, Any]] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        limit: int = 1000,
        order: str = "desc",
        record_access: bool = False,
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
        record_access : bool
            BASE-9：True 时本次读取写入访问审计（kind=access，绑定
            AuditLogStore 后生效；失败记 warning 不阻断查询）

        Returns
        -------
        list[dict] : 每项 {id/name/labels/value/timestamp}；敏感行 value
        为解密回读真实值（解密失败脱敏 "****"）
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
            f"SELECT id, name, labels_json, value, timestamp, value_encrypted "
            f"FROM metrics_history{where}"
            f" ORDER BY timestamp {order} LIMIT ?"
        )
        params.append(limit)
        with self._conn.transaction():
            rows = self._conn.execute(sql, params)
        if record_access:
            self._audit_access(
                "read",
                {
                    "resource": "metrics_history",
                    "filter": {
                        "name": name,
                        "labels": labels,
                        "start_ts": start_ts,
                        "end_ts": end_ts,
                    },
                    "rows": len(rows),
                },
            )
        return [self._row_to_dict_decrypted(r) for r in rows]

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
        # BASE-9：敏感指标密文不可聚合（value 列为占位 0.0），拒绝并提示用 query 回读
        if self._is_sensitive(name):
            raise ValueError(
                f"降采样拒绝聚合敏感指标 {name!r}（密文落盘不可聚合）；"
                f"请用 query(name=..., record_access=True) 解密回读"
            )

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

    # ── BASE-9：导出（留痕）──

    def export_csv(self, path: str, name: Optional[str] = None, limit: int = 100_000) -> int:
        """
        导出指标点到 CSV（BASE-9：导出纳入访问审计，kind=access/export）。

        Parameters
        ----------
        path : str
            导出文件路径（父目录不存在则创建）
        name : str | None
            只导出该指标；None=全部
        limit : int
            导出条数上限（>0）

        Returns
        -------
        int : 导出行数

        Raises
        ------
        ValueError: path 为空/limit 非法时抛出
        """
        if not (path or "").strip():
            raise ValueError("导出失败: path 不能为空")
        if limit <= 0:
            raise ValueError(f"limit 必须为正整数，收到 {limit}")
        rows = self.query(name=name, limit=limit, order="asc", record_access=False)
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        import csv

        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["id", "name", "labels", "value", "timestamp"]
            )
            writer.writeheader()
            for r in rows:
                writer.writerow(
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "labels": json.dumps(r["labels"], ensure_ascii=False, sort_keys=True),
                        "value": r["value"],
                        "timestamp": r["timestamp"],
                    }
                )
        # 导出留痕（强制；失败记 warning 不阻断导出）
        self._audit_access(
            "export",
            {"resource": "metrics_history", "name": name, "rows": len(rows), "path": str(out_path)},
        )
        logger.info(f"[metrics] 导出完成: {len(rows)} 行 → {out_path}")
        return len(rows)

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
    #  BASE-6：迁移导出接口
    # ═══════════════════════════════════════════════

    def set_exporter(self, exporter: Optional[MetricExporter]) -> "MetricsHistoryStore":
        """
        绑定迁移导出器（BASE-6 预留；默认 NullMetricExporter 纯 SQLite）。

        Parameters
        ----------
        exporter : MetricExporter | None
            导出器实例（或 get_exporter("vm"/"clickhouse") 占位）；None 重置为空导出

        Returns
        -------
        MetricsHistoryStore : self
        """
        if exporter is not None and not isinstance(exporter, MetricExporter):
            raise ValueError(
                f"exporter 必须为 MetricExporter 实例，收到 {type(exporter).__name__}"
            )
        self._exporter = exporter if exporter is not None else NullMetricExporter()
        logger.info(f"[metrics] 导出器绑定: {type(self._exporter).__name__}")
        return self

    def _export_rows(self, rows: List[Dict[str, Any]]) -> None:
        """导出已落库副本（迁移预留；导出失败记 warning 不阻断主落库）。"""
        if not rows:
            return
        try:
            self._exporter.export_batch(rows)
        except NotImplementedError:
            logger.debug("导出器为预留占位（未接入），跳过导出")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"指标导出失败（不阻断落库）: {e}")

    # ═══════════════════════════════════════════════
    #  BASE-6：异步批量写入（避免热路径直写）
    # ═══════════════════════════════════════════════

    def enqueue(self, row: Dict[str, Any]) -> int:
        """
        入队一个指标点（内存缓冲，后台线程周期批量落库；热路径用）。

        队列超上限时丢弃最旧并记 warning（防无界增长，宁可丢最旧快照
        也不阻塞主流程）。

        Parameters
        ----------
        row : dict
            与 save_batch 行结构一致（name/value，可选 labels/timestamp）

        Returns
        -------
        int : 当前队列长度
        """
        with self._queue_lock:
            if len(self._queue) >= self._queue_limit:
                self._queue.popleft()
                logger.warning(
                    f"指标队列超上限 {self._queue_limit}，丢弃最旧 1 条（防内存无界）"
                )
            self._queue.append(dict(row))
            return len(self._queue)

    def queue_size(self) -> int:
        """当前内存队列长度。"""
        with self._queue_lock:
            return len(self._queue)

    def flush(self) -> int:
        """
        把内存队列批量落库（executemany 事务包裹）。

        Returns
        -------
        int : 本次实际新增行数（重复点忽略不计入）
        """
        with self._queue_lock:
            if not self._queue:
                return 0
            pending = list(self._queue)
            self._queue.clear()
        if not pending:
            return 0
        # 分批（防单事务超上限）
        inserted = 0
        for i in range(0, len(pending), MAX_BATCH_SIZE):
            chunk = pending[i : i + MAX_BATCH_SIZE]
            try:
                inserted += self.save_batch(chunk)
            except Exception as e:  # noqa: BLE001
                logger.error(f"异步批量落库失败（{len(chunk)} 条丢弃）: {e}")
                # 落库失败的批次不再重试（快照类数据，防阻塞热路径）
        return inserted

    def start_background_flush(self, interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS) -> bool:
        """
        启动后台 flush 守护线程（BASE-6：避免热路径直写）。

        线程为 daemon，随进程退出自动终止；重复启动幂等。

        Parameters
        ----------
        interval_seconds : float
            flush 间隔（秒，>0）

        Returns
        -------
        bool : True=已启动（或已在运行）
        """
        try:
            interval = float(interval_seconds)
            if interval <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"interval_seconds 必须为正数，收到 {interval_seconds!r}")
        with self._queue_lock:
            if self._flush_running:
                return True
            self._flush_stop.clear()
            self._flush_interval = interval

            def _loop() -> None:
                while not self._flush_stop.wait(self._flush_interval):
                    try:
                        self.flush()
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"后台 flush 异常: {e}")

            self._flush_thread = threading.Thread(
                target=_loop, name="metrics-history-flush", daemon=True
            )
            self._flush_running = True
            self._flush_thread.start()
        logger.info(f"[metrics] 后台批量落库线程启动（间隔 {interval:.0f}s）")
        return True

    def stop_background_flush(self, final_flush: bool = True) -> int:
        """
        停止后台 flush 线程（可选收尾落库）。

        Parameters
        ----------
        final_flush : bool
            停止前是否把队列剩余数据落库（默认 True）

        Returns
        -------
        int : 收尾落库条数
        """
        with self._queue_lock:
            if not self._flush_running:
                return 0 if not final_flush else self.flush()
            self._flush_running = False
        self._flush_stop.set()
        if self._flush_thread is not None and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)
        self._flush_thread = None
        inserted = 0
        if final_flush:
            inserted = self.flush()
        logger.info(f"[metrics] 后台批量落库线程已停止（收尾落库 {inserted} 条）")
        return inserted

    # ═══════════════════════════════════════════════
    #  BASE-6：降采样物化（原始 30 天 → 1min 聚合 → 1 年）
    # ═══════════════════════════════════════════════

    def materialize_downsampled(
        self,
        name: str,
        bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
        older_than_days: int = DEFAULT_MATERIALIZE_OLDER_THAN_DAYS,
        agg: str = DEFAULT_DOWNSAMPLE_AGG,
        target_name: Optional[str] = None,
    ) -> int:
        """
        把过期原始数据按时间桶聚合物化落库（降采样 1 年可回放）。

        物化目标写入同表（target_name 默认 ``<name>:ds:<bucket_minutes>m``），
        与原始行共存；保留策略为：原始 30 天（retention_purge 清理）+ 物化
        1 年（downsample_retention_purge 清理）。

        防未来：物化只聚合 ``当前时刻 - older_than_days`` 之前已发生的数据，
        不触碰近期（仍为原始高精度）数据；同桶幂等（INSERT OR IGNORE，
        重复执行不产生重复行）。

        Parameters
        ----------
        name : str
            源指标名
        bucket_minutes : int
            时间桶（分钟，>0）
        older_than_days : int
            只物化 >= 该天数之前的旧数据（>0）
        agg : str
            聚合函数（avg/max/min/sum/count）
        target_name : str | None
            物化目标指标名；None 用默认命名

        Returns
        -------
        int : 物化写入行数

        Raises
        ------
        ValueError: 参数非法时抛出
        """
        src = (name or "").strip()
        if not src:
            raise ValueError("物化失败: 源指标名 name 不能为空")
        if bucket_minutes <= 0:
            raise ValueError(f"bucket_minutes 必须为正整数，收到 {bucket_minutes}")
        if older_than_days <= 0:
            raise ValueError(f"older_than_days 必须为正整数，收到 {older_than_days}")
        if agg not in _AGG_WHITELIST:
            raise ValueError(f"非法聚合函数: {agg!r}（须为 {list(_AGG_WHITELIST)}）")
        target = (target_name or f"{src}:ds:{int(bucket_minutes)}m").strip()
        if not target:
            raise ValueError("物化失败: target_name 不能为空")

        cutoff = time.time() - older_than_days * 86400.0
        bucket_seconds = int(bucket_minutes) * 60
        agg_expr = "COUNT(*)" if agg == "count" else f"{agg}(value)"
        sql = (
            "SELECT CAST(timestamp / ? AS INTEGER) * ? AS bucket, "
            f"{agg_expr} AS value, COUNT(*) AS cnt "
            "FROM metrics_history "
            "WHERE name = ? AND timestamp < ? "
            "GROUP BY bucket"
        )
        with self._conn.transaction():
            buckets = self._conn.execute(
                sql, (bucket_seconds, bucket_seconds, src, cutoff)
            )
            if not buckets:
                return 0
            prepared = [
                (
                    target,
                    self._dump_labels(
                        {
                            "source": src,
                            "bucket_minutes": int(bucket_minutes),
                            "agg": agg,
                        }
                    ),
                    float(r["value"]),
                    float(r["bucket"]),
                )
                for r in buckets
            ]
            affected = self._conn.executemany(
                """
                INSERT OR IGNORE INTO metrics_history (name, labels_json, value, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                prepared,
            )
        if affected > 0:
            self._export_rows(
                [
                    {"name": target, "labels": {}, "value": v, "timestamp": t}
                    for (_n, _lj, v, t) in prepared
                ]
            )
        logger.info(
            f"[metrics] 降采样物化完成: {src} → {target} "
            f"({agg}/{bucket_minutes}m, >= {older_than_days}天前)，{affected} 行"
        )
        return affected

    def downsample_retention_purge(
        self,
        downsample_days: int = DEFAULT_DOWNSAMPLE_DAYS,
        name_prefix: Optional[str] = None,
    ) -> int:
        """
        清理超过保留期的降采样物化行（默认 1 年；保留原始行策略不受影响）。

        Parameters
        ----------
        downsample_days : int
            物化数据保留天数（>0）
        name_prefix : str | None
            只清理该前缀的物化行（如 "patrol_status:ds:"）；None 清理全部
            含 ":ds:" 命名标记的物化行

        Returns
        -------
        int : 删除行数
        """
        if downsample_days <= 0:
            raise ValueError(f"downsample_days 必须为正整数，收到 {downsample_days}")
        cutoff = time.time() - downsample_days * 86400.0
        if name_prefix:
            where = "name LIKE ? AND timestamp < ?"
            params: List[Any] = [f"{name_prefix}%", cutoff]
        else:
            where = "name LIKE ? AND timestamp < ?"
            params = ["%:ds:%", cutoff]
        with self._conn.transaction():
            affected = self._conn.execute_update(
                f"DELETE FROM metrics_history WHERE {where}",
                params,
            )
        logger.info(f"[metrics] 降采样物化清理完成（>= {downsample_days} 天前）: {affected} 行")
        return affected


# ═══════════════════════════════════════════════
#  BASE-6：降采样独立调度器（原始 30 天 → 1min 聚合 → 1 年）
# ═══════════════════════════════════════════════

class DownsamplingTask:
    """
    降采样独立调度（守护线程，与巡检/告警调度完全隔离）。

    BASE-6 语义：原始数据保留 30 天（高精度），>=30 天前的旧数据按
    时间桶聚合物化为降采样行（默认 1 分钟桶 → 可回放 1 年），随后原始
    过期行由 retention_purge 清理；物化行由 downsample_retention_purge
    清理（默认 365 天）。整个周期独立于巡检 60s 节奏，默认每日一次，
    interval 可配（降采样"可配置可回放"）。

    Usage:
        task = DownsamplingTask(store)
        task.start(interval_seconds=86400)   # 独立守护线程
        ...
        task.stop()                          # 收尾并清理

    防未来：run() 只处理当前时刻之前已发生的数据；失败不中断下次调度。
    """

    def __init__(
        self,
        store: MetricsHistoryStore,
        bucket_minutes: int = DEFAULT_BUCKET_MINUTES,
        older_than_days: int = DEFAULT_MATERIALIZE_OLDER_THAN_DAYS,
        agg: str = DEFAULT_DOWNSAMPLE_AGG,
        names: Optional[List[str]] = None,
        enable_purge: bool = True,
    ):
        if bucket_minutes <= 0:
            raise ValueError(f"bucket_minutes 必须为正整数，收到 {bucket_minutes}")
        if older_than_days <= 0:
            raise ValueError(f"older_than_days 必须为正整数，收到 {older_than_days}")
        if agg not in _AGG_WHITELIST:
            raise ValueError(f"非法聚合函数: {agg!r}（须为 {list(_AGG_WHITELIST)}）")
        self._store = store
        self._bucket_minutes = bucket_minutes
        self._older_than_days = older_than_days
        self._agg = agg
        self._names: List[str] = names or []  # 空=全部指标物化（按名聚合）
        self._enable_purge = enable_purge
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._interval: float = DEFAULT_DOWNSAMPLE_INTERVAL_SECONDS
        self._running = False
        self._lock = threading.Lock()
        self._last_run: Optional[float] = None
        self._last_result: Optional[Dict[str, int]] = None

    # ── 调度控制 ──

    def start(
        self,
        interval_seconds: float = DEFAULT_DOWNSAMPLE_INTERVAL_SECONDS,
    ) -> bool:
        """
        启动独立调度守护线程（daemon，进程退出自动终止；幂等）。

        Parameters
        ----------
        interval_seconds : float
            调度间隔（秒，>0；默认 86400=每日一次）

        Returns
        -------
        bool : True=已启动（或已在运行）
        """
        try:
            interval = float(interval_seconds)
            if interval <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"interval_seconds 必须为正数，收到 {interval_seconds!r}")
        with self._lock:
            if self._running:
                return True
            self._interval = interval
            self._stop_event.clear()

            def _loop() -> None:
                while not self._stop_event.wait(self._interval):
                    self.run()

            self._thread = threading.Thread(
                target=_loop, name="metrics-downsample-task", daemon=True
            )
            self._running = True
            self._thread.start()
        logger.info(f"[metrics] 降采样调度线程启动（间隔 {interval:.0f}s）")
        return True

    def stop(self, run_once: bool = True) -> bool:
        """
        停止调度线程。

        Parameters
        ----------
        run_once : bool
            停止前是否补跑一轮（默认 True，防收尾遗留旧数据未物化）

        Returns
        -------
        bool : True=已停止
        """
        with self._lock:
            if not self._running:
                return True
            self._running = False
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        if run_once:
            self.run()
        logger.info("[metrics] 降采样调度线程已停止")
        return True

    # ── 执行 ──

    def run(self) -> Dict[str, int]:
        """
        执行一轮降采样：物化旧数据 → （可选）清理过期原始行与物化行。

        Returns
        -------
        dict : {"materialized": 物化行数, "purged_raw": 清理原始行数,
                "purged_downsample": 清理物化行数}

        失败不抛异常：单指标物化失败记 error，不影响其余指标与下次调度。
        """
        result: Dict[str, int] = {"materialized": 0, "purged_raw": 0, "purged_downsample": 0}
        if self._names:
            targets = self._names
        else:
            # 全量模式：物化所有原始指标（排除已有 :ds: 物化行）
            try:
                with self._store._conn.transaction():
                    rows = self._store._conn.execute(
                        "SELECT DISTINCT name FROM metrics_history WHERE name NOT LIKE ?",
                        ("%:ds:%",),
                    )
                targets = [r["name"] for r in rows]
            except Exception as e:  # noqa: BLE001
                logger.error(f"降采样枚举指标失败: {e}")
                targets = []
        for src in targets:
            try:
                result["materialized"] += self._store.materialize_downsampled(
                    src,
                    bucket_minutes=self._bucket_minutes,
                    older_than_days=self._older_than_days,
                    agg=self._agg,
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"降采样物化失败（{src}）: {e}")
        if self._enable_purge:
            try:
                result["purged_raw"] = self._store.retention_purge(
                    DEFAULT_RETENTION_DAYS
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"原始数据清理失败: {e}")
            try:
                result["purged_downsample"] = self._store.downsample_retention_purge(
                    DEFAULT_DOWNSAMPLE_DAYS
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"物化数据清理失败: {e}")
        self._last_run = time.time()
        self._last_result = result
        return result

    @property
    def last_result(self) -> Optional[Dict[str, int]]:
        """最近一轮执行结果（未跑过为 None）。"""
        return self._last_result


# ═══════════════════════════════════════════════
#  自测（python -m myquant.core.metrics_history）
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

            # 写入 + 唯一幂等（BASE-2 用例用非敏感名；敏感名加密见 BASE-9 段）
            base = 1_700_000_000.0
            check("单条写入", store.save_metric("curve_total_sample", 1_000_000.0, timestamp=base))
            check("同 key 同秒忽略", store.save_metric("curve_total_sample", 1_000_000.0, timestamp=base) is False)
            store.save_metric("curve_total_sample", 1_000_100.0, timestamp=base + 60)
            store.save_metric("patrol_status", 0.0, labels={"status": "healthy"}, timestamp=base + 120)

            # 查询
            rows = store.query("curve_total_sample", limit=10)
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
            check("清理后查询", len(store.query("curve_total_sample", limit=10)) == 1)

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

            # ══════════════════════════════════
            #  BASE-6 自测
            # ══════════════════════════════════

            # 迁移导出器：默认空导出 + 占位注册表
            check("默认导出器为空导出", isinstance(store._exporter, NullMetricExporter))
            store.set_exporter(get_exporter("null"))
            check("set_exporter(null) 重绑定", isinstance(store._exporter, NullMetricExporter))
            try:
                get_exporter("vm").export_batch([{"name": "x", "value": 1.0}])
                check("vm 占位导出器未接入报错", False)
            except NotImplementedError:
                check("vm 占位导出器未接入报错", True)
            try:
                get_exporter("unknown")
                check("未注册导出器报错", False)
            except ValueError:
                check("未注册导出器报错", True)

            # 异步批量写入（enqueue/flush）
            for i in range(3):
                store.enqueue({"name": "async_q", "value": float(i), "timestamp": base + 300 + i})
            check("enqueue 队列长度", store.queue_size() == 3)
            inserted = store.flush()
            check("flush 落库", inserted == 3 and len(store.query("async_q", limit=10)) == 3)
            check("flush 后队列清空", store.queue_size() == 0)

            # 后台 flush 守护线程（避免热路径直写）
            store.start_background_flush(interval_seconds=0.2)
            store.enqueue({"name": "async_bg", "value": 7.0, "timestamp": base + 400})
            time.sleep(0.6)  # 等后台线程落库
            check("后台 flush 落库", len(store.query("async_bg", limit=5)) == 1)
            store.enqueue({"name": "async_bg2", "value": 8.0, "timestamp": base + 401})
            tail = store.stop_background_flush(final_flush=True)
            check("后台线程收尾落库", tail >= 1 and len(store.query("async_bg2", limit=5)) == 1)

            # 降采样物化（>=30 天前的旧数据 → 1min 桶 → 可回放 1 年）
            old_ts = time.time() - 31 * 86400.0
            for i in range(3):  # 31 天前的三个 1min 桶
                store.save_metric("ds_source", float(i), timestamp=old_ts + 60 * i)
            mat = store.materialize_downsampled(
                "ds_source", bucket_minutes=1, older_than_days=30, agg="avg"
            )
            check("物化 3 桶", mat == 3)
            ds_rows = store.query("ds_source:ds:1m", limit=10)
            check("物化可查询", len(ds_rows) == 3 and ds_rows[0]["value"] in (0.0, 1.0, 2.0))
            mat2 = store.materialize_downsampled(
                "ds_source", bucket_minutes=1, older_than_days=30, agg="avg"
            )
            check("物化幂等（同桶不重复）", mat2 == 0 and len(store.query("ds_source:ds:1m", limit=10)) == 3)
            store.save_metric("ds_old_material:ds:1m", 1.0, timestamp=time.time() - 366 * 86400.0)
            purged_ds = store.downsample_retention_purge(downsample_days=365)
            check("物化 365 天清理", purged_ds >= 1)

            # 降采样独立调度（DownsamplingTask）
            task = DownsamplingTask(store, bucket_minutes=1, older_than_days=30)
            res = task.run()
            check(
                "调度器一轮执行（物化+双清理）",
                res["materialized"] >= 0 and "purged_raw" in res and "purged_downsample" in res,
            )
            task.start(interval_seconds=0.2)
            time.sleep(0.5)
            task.stop(run_once=False)
            check("调度器 start/stop 周期执行", task.last_result is not None)

            # ══════════════════════════════════
            #  BASE-9 自测（敏感字段加密 + 访问审计）
            # ══════════════════════════════════

            from myquant.core.field_crypto import FieldCrypto

            crypto = FieldCrypto(key_file=str(Path(tmp) / ".test_key"))
            store.attach_field_crypto(crypto)

            # 敏感指标密文落盘 + 解密回读
            store.save_metric("equity_curve_total", 1_000_000.0, timestamp=base + 500)
            rows = store.query("equity_curve_total", limit=5)
            check(
                "敏感指标解密回读真实值",
                len(rows) == 1 and rows[0]["value"] == 1_000_000.0,
                f"value={rows[0]['value']}",
            )
            # 落盘真实性：库内 value 应为占位 0.0 + value_encrypted 非空密文
            raw = conn.execute(
                "SELECT value, value_encrypted FROM metrics_history WHERE name = ?",
                ("equity_curve_total",),
            )
            check(
                "敏感字段密文落盘（value 占位 + 密文非空）",
                float(raw[0]["value"]) == 0.0 and bool(raw[0]["value_encrypted"]),
            )
            check("密文非明文", "1000000.0" not in raw[0]["value_encrypted"])

            # 普通指标不受影响
            store.save_metric("patrol_status", 0.0, timestamp=base + 501)
            raw2 = conn.execute(
                "SELECT value, value_encrypted FROM metrics_history WHERE name = ?",
                ("patrol_status",),
            )
            check("普通指标明文落库（无密文列）", raw2[0]["value_encrypted"] is None)

            # 未绑定加密器写入敏感名 → 拒绝明文落盘
            store_bare = MetricsHistoryStore(connection=conn)
            try:
                store_bare.save_metric("equity_curve_total", 5.0, timestamp=base + 502)
                check("未绑定加密器拒绝敏感明文落盘", False)
            except ValueError:
                check("未绑定加密器拒绝敏感明文落盘", True)

            # 敏感指标拒绝降采样聚合
            try:
                store.query_downsampled("equity_curve_total", bucket_minutes=60)
                check("敏感指标降采样拒绝", False)
            except ValueError:
                check("敏感指标降采样拒绝", True)

            # 访问审计：读取留痕 + 导出留痕
            from myquant.core.audit_log import AuditLogStore

            audit = AuditLogStore(connection=conn)
            store.attach_access_audit(audit)
            store.query("patrol_status", limit=5, record_access=True)
            export_path = Path(tmp) / "export.csv"
            store.export_csv(str(export_path), name="patrol_status", limit=10)
            check("导出文件生成", export_path.exists())
            access_rows = audit.query(kind="access", order="asc")
            check(
                "读取/导出访问审计留痕",
                len(access_rows) == 2
                and access_rows[0]["detail"]["resource"] == "metrics_history"
                and access_rows[1]["action"] == "export",
                f"count={len(access_rows)}",
            )
            check("访问审计链完整", audit.verify_chain()[0] is True)
        finally:
            # BASE-6：后台 flush/降采样线程可能持有独立线程连接，
            # 须 close_all 跨线程关闭（close() 只关主线程连接）
            conn.close_all()

    logger.info("自测结果: %d 通过, %d 失败", passed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(_run_self_test())
