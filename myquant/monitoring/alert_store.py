# 文件名：myquant/monitoring/alert_store.py
"""
alert_store.py - 告警持久化存储层（BASE-1 告警落库）
========================================================================
依据《量化系统监控模块施工总清单_V1.2》BASE-1：
  - alerts 表（id/level/category/source/message/details_json/dedup_key/
    status/classification(TP/FP/auto，供 P0-8 统计打标 R-01)/created_at/
    acked_at/resolved_at/escalated/runbook_id）
  - AlertManager 发送落库、确认/解决/升级同步库、库内存双读
  - 恢复通知（同 key 未解决时健康恢复自动发 INFO 并置 resolved）

本模块职责：
  1. 建表自检（幂等，DDL 从 myquant.database.schema 导入，与迁移 0005 逐字一致）
  2. 告警写入（INSERT OR REPLACE，alert_id 唯一）
  3. 状态流转（open/acked/resolved，时间戳由系统时钟写入）
  4. 分类打标（classification，P0-8 统计脚本读取源）
  5. 历史/未确认/升级查询（库内存双读的"库"侧）

防未来铁律：只写/读已发生的时间戳与告警记录，不引用未来数据；
落库失败不阻断告警投递（记 error 留痕，元监控计数），告警投递优先。

依赖: json, time, logging, typing, myquant.database.connection / schema
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Union

from myquant.database.connection import DatabaseConnection, get_connection
from myquant.database.schema import CREATE_ALERTS_INDEX_SQL, CREATE_ALERTS_SQL

logger = logging.getLogger("myquant.monitoring.alert_store")


# ═══════════════════════════════════════════════
#  状态常量（与施工清单 BASE-1 对齐）
# ═══════════════════════════════════════════════

STATUS_OPEN: str = "open"            # 未确认
STATUS_ACKED: str = "acked"          # 已确认
STATUS_RESOLVED: str = "resolved"    # 已解决


class AlertStore:
    """
    告警持久化存储（SQLite，线程安全，事务写入）

    Usage:
        store = AlertStore()
        store.save_alert(alert)
        store.update_status(alert_id, STATUS_RESOLVED)
        rows = store.load_history(limit=50)
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
        """确保 alerts 表与索引存在（幂等，启动/实例化时自检）。"""
        with self._conn.transaction():
            self._conn.execute_update(CREATE_ALERTS_SQL)
            for sql in CREATE_ALERTS_INDEX_SQL:
                self._conn.execute_update(sql)
        logger.debug("alerts 表自检完成（CREATE IF NOT EXISTS）")

    # ── 写：落库 ──

    @staticmethod
    def _status_from_alert(alert: Any) -> str:
        """由 Alert 对象推导库状态（resolved > acked > open）。"""
        if getattr(alert, "resolved", False):
            return STATUS_RESOLVED
        if getattr(alert, "acknowledged", False):
            return STATUS_ACKED
        return STATUS_OPEN

    def save_alert(self, alert: Any, classification: str = "") -> str:
        """
        落库一条告警（INSERT OR REPLACE，alert_id 唯一幂等）。

        Parameters
        ----------
        alert : Alert
            告警对象（需含 alert_id/level/category/source/message/details/
            deduplication_key/timestamp/acknowledged/resolved/escalated_from）
        classification : str
            处置分类（TP/FP/auto/空），P0-8 统计打标；首次落库默认空

        Returns
        -------
        str : alert_id

        Raises
        ------
        ValueError: alert 缺关键字段时抛出（防御式，不静默写脏数据）
        """
        alert_id = getattr(alert, "alert_id", "")
        if not alert_id:
            raise ValueError("落库失败: alert 缺少 alert_id")
        level = getattr(alert, "level", None)
        if level is None or not getattr(level, "value", ""):
            raise ValueError("落库失败: alert 缺少 level")
        message = getattr(alert, "message", "")
        if not message:
            raise ValueError("落库失败: alert 缺少 message")

        now = time.time()
        details_json = json.dumps(
            getattr(alert, "details", {}) or {},
            ensure_ascii=False,
        )
        escalated = 1 if getattr(alert, "escalated_from", None) else 0

        with self._conn.transaction():
            self._conn.execute_update(
                """
                INSERT OR REPLACE INTO alerts (
                    alert_id, level, category, source, message,
                    details_json, dedup_key, status, classification,
                    created_at, acked_at, resolved_at, escalated, runbook_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    level.value,
                    getattr(alert, "category", "system"),
                    getattr(alert, "source", "monitoring"),
                    message,
                    details_json,
                    getattr(alert, "deduplication_key", "") or "",
                    AlertStore._status_from_alert(alert),
                    classification,
                    getattr(alert, "timestamp", now),
                    now if getattr(alert, "acknowledged", False) else None,
                    now if getattr(alert, "resolved", False) else None,
                    escalated,
                    None,
                ),
            )
        return alert_id

    # ── 写：状态流转（确认/解决，同步库）──

    def update_status(self, alert_id: str, status: str) -> bool:
        """
        更新告警状态（open/acked/resolved），同步对应时间戳。

        Parameters
        ----------
        alert_id : str
            告警 ID
        status : str
            open / acked / resolved

        Returns
        -------
        bool : 是否更新了 1 行

        Raises
        ------
        ValueError: status 非法时抛出
        """
        if status not in (STATUS_OPEN, STATUS_ACKED, STATUS_RESOLVED):
            raise ValueError(f"非法告警状态: {status!r}（须为 open/acked/resolved）")

        now = time.time()
        if status == STATUS_RESOLVED:
            # 解决：置 resolved_at=now，并补 acked_at（若未确认过）
            sql = """
                UPDATE alerts
                   SET status = ?, resolved_at = ?,
                       acked_at = COALESCE(acked_at, ?)
                 WHERE alert_id = ?
            """
            params: tuple = (status, now, now, alert_id)
        elif status == STATUS_ACKED:
            sql = "UPDATE alerts SET status = ?, acked_at = ? WHERE alert_id = ?"
            params = (status, now, alert_id)
        else:
            sql = (
                "UPDATE alerts SET status = ?, acked_at = NULL, "
                "resolved_at = NULL WHERE alert_id = ?"
            )
            params = (status, alert_id)

        with self._conn.transaction():
            affected = self._conn.execute_update(sql, params)
        return affected > 0

    def set_classification(self, alert_id: str, classification: str) -> bool:
        """打标处置分类（TP/FP/auto），P0-8 统计脚本读取源。

        Args:
            alert_id: 告警 ID
            classification: TP / FP / auto

        Returns:
            bool: 是否更新了 1 行
        """
        with self._conn.transaction():
            affected = self._conn.execute_update(
                "UPDATE alerts SET classification = ? WHERE alert_id = ?",
                (classification, alert_id),
            )
        return affected > 0

    def mark_escalated(self, alert_id: str) -> bool:
        """标记告警已升级（check_escalations 同步库）。"""
        with self._conn.transaction():
            affected = self._conn.execute_update(
                "UPDATE alerts SET escalated = 1 WHERE alert_id = ?",
                (alert_id,),
            )
        return affected > 0

    def set_runbook_id(self, alert_id: str, runbook_id: str) -> bool:
        """绑定 Runbook（P0-4 关联，可选）。"""
        with self._conn.transaction():
            affected = self._conn.execute_update(
                "UPDATE alerts SET runbook_id = ? WHERE alert_id = ?",
                (runbook_id, alert_id),
            )
        return affected > 0

    # ── 读：库内存双读的"库"侧 ──

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        """sqlite3.Row → dict（details_json 反序列化，坏 JSON 不静默吞）。"""
        data = {
            "alert_id": row["alert_id"],
            "level": row["level"],
            "category": row["category"],
            "source": row["source"],
            "message": row["message"],
            "deduplication_key": row["dedup_key"] or "",
            "status": row["status"],
            "classification": row["classification"] or "",
            "created_at": row["created_at"],
            "acked_at": row["acked_at"],
            "resolved_at": row["resolved_at"],
            "escalated": bool(row["escalated"]),
            "runbook_id": row["runbook_id"],
        }
        try:
            data["details"] = json.loads(row["details_json"]) if row["details_json"] else {}
        except json.JSONDecodeError as exc:
            logger.error(f"告警 details_json 解析失败 alert_id={row['alert_id']}: {exc}")
            data["details"] = {"_parse_error": str(exc)}
        return data

    def load_history(
        self,
        limit: int = 50,
        level: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        查询告警历史（created_at 倒序）。

        Parameters
        ----------
        limit : int
            最大返回条数（默认 50，>0）
        level / category / status : str | None
            过滤条件（可选）

        Returns
        -------
        List[dict] : 告警记录列表（详情反序列化）

        Raises
        ------
        ValueError: limit 非法时抛出
        """
        if limit <= 0:
            raise ValueError(f"limit 必须 >0，收到 {limit}")

        sql = "SELECT * FROM alerts WHERE 1=1"
        params: List[Any] = []
        if level:
            sql += " AND level = ?"
            params.append(level)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, tuple(params))
        return [AlertStore._row_to_dict(r) for r in rows]

    def load_unacknowledged(self, limit: int = 50) -> List[Dict[str, Any]]:
        """查询未确认告警（status='open'）。"""
        return self.load_history(limit=limit, status=STATUS_OPEN)

    def load_escalated(self, limit: int = 50) -> List[Dict[str, Any]]:
        """查询升级告警（escalated=1）。"""
        if limit <= 0:
            raise ValueError(f"limit 必须 >0，收到 {limit}")
        rows = self._conn.execute(
            "SELECT * FROM alerts WHERE escalated = 1 "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [AlertStore._row_to_dict(r) for r in rows]

    def get_open_by_dedup_key(self, dedup_key: str) -> List[Dict[str, Any]]:
        """查询同去重键且未解决的告警（恢复通知匹配，BASE-1）。"""
        if not dedup_key:
            return []
        rows = self._conn.execute(
            "SELECT * FROM alerts WHERE dedup_key = ? AND status != ? "
            "ORDER BY created_at DESC",
            (dedup_key, STATUS_RESOLVED),
        )
        return [AlertStore._row_to_dict(r) for r in rows]

    def get(self, alert_id: str) -> Optional[Dict[str, Any]]:
        """按 alert_id 查询单条（不存在返回 None）。"""
        row = self._conn.execute_one(
            "SELECT * FROM alerts WHERE alert_id = ?",
            (alert_id,),
        )
        return AlertStore._row_to_dict(row) if row else None


# ═══════════════════════════════════════════════
#  自测 Demo（__main__ 入口，可运行；临时库不碰真实 quant.db）
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """自测：落库/状态流转/分类打标/恢复匹配，全部走临时 sqlite 文件。"""
    import tempfile
    from pathlib import Path

    from myquant.database.connection import DatabaseConnection

    passed: int = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name}: {detail}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "test_alerts.db"
        conn = DatabaseConnection(tmp_db)
        store = AlertStore(connection=conn)

        # 构造最小告警（无外部依赖，模拟 Alert 关键字段）
        class _FakeAlert:
            alert_id = "a1"
            level = type("L", (), {"value": "WARNING"})()
            category = "data_quality"
            source = "event_bus.data_quality.aggregated"
            message = "数据质量告警: 价格跳变"
            details = {"dirty_type": "price_jump", "symbol": "000001.SZ"}
            deduplication_key = "agg:000001.SZ:daily:price_jump"
            timestamp = 1000.0
            acknowledged = False
            resolved = False
            escalated_from = None

        store.save_alert(_FakeAlert())
        check("落库可查", store.get("a1") is not None)
        check("状态=open", store.get("a1")["status"] == "open")
        check("details 反序列化", store.get("a1")["details"].get("symbol") == "000001.SZ")

        # 分类打标（P0-8 读取源）
        check("分类打标", store.set_classification("a1", "FP"))
        check("分类可读", store.get("a1")["classification"] == "FP")

        # 确认 → 解决
        check("确认同步", store.update_status("a1", STATUS_ACKED))
        check("acked 时间戳", store.get("a1")["acked_at"] is not None)
        check("解决同步", store.update_status("a1", STATUS_RESOLVED))
        check("resolved 时间戳", store.get("a1")["resolved_at"] is not None)

        # 升级标记
        check("升级标记", store.mark_escalated("a1"))
        check("escalated 可读", store.get("a1")["escalated"] is True)

        # 恢复匹配（同 key 未解决）
        class _FakeAlert2:
            alert_id = "a2"
            level = type("L", (), {"value": "ERROR"})()
            category = "data_quality"
            source = "event_bus.pipeline"
            message = "清洗质量告警"
            details = {"symbol": "000001.SZ"}
            deduplication_key = "agg:000001.SZ:daily:price_jump"
            timestamp = 2000.0
            acknowledged = False
            resolved = False
            escalated_from = None

        store.save_alert(_FakeAlert2())
        check("同key未解决可查", len(store.get_open_by_dedup_key("agg:000001.SZ:daily:price_jump")) == 1)

        try:
            # 非法输入防御
            try:
                store.update_status("a1", "weird")
                check("非法状态报错", False, "未抛 ValueError")
            except ValueError:
                check("非法状态报错", True)
            try:
                store.load_history(limit=0)
                check("limit=0 报错", False, "未抛 ValueError")
            except ValueError:
                check("limit=0 报错", True)
        finally:
            # Windows：先关闭连接释放 sqlite 文件锁，再允许临时目录清理
            conn.close()

    print(f"AlertStore 自测: {passed} 项通过, {len(failures)} 项失败")
    for fail in failures:
        print(f"  - {fail}")
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
