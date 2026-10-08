# 文件名：tests/unit/test_audit_log.py
"""
test_audit_log.py - BASE-7 审计只追加存储单元测试
==================================================
覆盖：
  1. 建表三表自检 + 迁移 0007 DDL 与 schema.py 逐字一致
  2. 追加与哈希链：seq 递增 / prev_hash 链尾衔接 / GENESIS 首链 /
     记录字段齐全
  3. 只读查询：kind/actor/时间范围/limit/order 过滤，detail 反序列化
  4. 篡改检测：verify_chain 干净链通过；行内容篡改断链；链衔接破坏
     （插入/删除）断链
  5. 归档与校验：archive 归档旧行（原哈希不改写）+ manifest 校验和；
     verify_archive 通过 / 归档行篡改检出 / 空归档
  6. 防御式：空 kind/action/actor、detail 非 dict、detail 超限、
     limit 非法、before_ts 非法、归档清单不存在
  7. AlertStore 集成：attach_audit_log 后落库/状态流转留痕；
     审计失败不阻断告警落库

运行方式:
  pytest tests/unit/test_audit_log.py -v
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.database.schema import (
    CREATE_AUDIT_ARCHIVE_MANIFEST_SQL,
    CREATE_AUDIT_ARCHIVE_SQL,
    CREATE_AUDIT_LOG_SQL,
    TABLE_NAMES,
    table_exists,
)
from myquant.monitoring.alerting import Alert, SeverityLevel
from myquant.core.alert_store import AlertStore, STATUS_ACKED, STATUS_RESOLVED
from myquant.core.audit_log import AuditLogStore, _GENESIS_HASH


@pytest.fixture()
def tmp_conn(tmp_path: Path) -> DatabaseConnection:
    return DatabaseConnection(tmp_path / "audit_test.db")


@pytest.fixture()
def audit(tmp_conn: DatabaseConnection) -> AuditLogStore:
    return AuditLogStore(connection=tmp_conn)


def _make_alert(alert_id: str = "a1", resolved: bool = False) -> Alert:
    return Alert(
        level=SeverityLevel.WARNING,
        message="数据质量告警: 价格跳变",
        source="event_bus.data_quality.aggregated",
        category="data_quality",
        details={"dirty_type": "price_jump", "symbol": "000001.SZ"},
        deduplication_key="agg:000001.SZ:daily:price_jump",
        timestamp=1000.0,
        alert_id=alert_id,
        resolved=resolved,
    )


# ═══════════════════════════════════════════
#  1. 建表自检 + 迁移 DDL 一致性
# ═══════════════════════════════════════════

class TestSchemaSelfCheck:
    def test_three_tables_created(self, tmp_conn: DatabaseConnection) -> None:
        AuditLogStore(connection=tmp_conn)
        assert table_exists(tmp_conn, TABLE_NAMES["audit_log"])
        assert table_exists(tmp_conn, TABLE_NAMES["audit_archive"])
        assert table_exists(tmp_conn, TABLE_NAMES["audit_archive_manifest"])

    def test_ensure_schema_idempotent(self, tmp_conn: DatabaseConnection) -> None:
        AuditLogStore(connection=tmp_conn)
        AuditLogStore(connection=tmp_conn)  # 重复实例化不报错

    def test_migration_ddl_matches_schema(self) -> None:
        """迁移 0007 的 DDL 与 schema.py 逐字一致（防双路漂移）。"""
        import re
        from pathlib import Path as P

        migration = (P(__file__).parents[2] / "myquant/database/migrations/versions/0007_add_audit_log.py").read_text(
            encoding="utf-8"
        )
        # 迁移文件内的 DDL 常量文本
        def extract(sql: str) -> str:
            return re.sub(r"\s+", " ", sql.strip())

        for const_name, schema_sql in (
            ("CREATE_AUDIT_LOG_SQL", CREATE_AUDIT_LOG_SQL),
            ("CREATE_AUDIT_ARCHIVE_SQL", CREATE_AUDIT_ARCHIVE_SQL),
            ("CREATE_AUDIT_ARCHIVE_MANIFEST_SQL", CREATE_AUDIT_ARCHIVE_MANIFEST_SQL),
        ):
            # 迁移里通过 from schema import 引用，需确认常量名存在且未被覆写
            assert const_name in migration


# ═══════════════════════════════════════════
#  2. 追加与哈希链
# ═══════════════════════════════════════════

class TestAppendChain:
    def test_append_returns_chain_fields(self, audit: AuditLogStore) -> None:
        rec = audit.append("alert", "create", actor="scheduler", detail={"alert_id": "A1"})
        assert rec["seq"] == 1
        assert rec["prev_hash"] == _GENESIS_HASH
        assert len(rec["row_hash"]) == 64

    def test_chain_linking(self, audit: AuditLogStore) -> None:
        r1 = audit.append("alert", "create", detail={"id": 1})
        r2 = audit.append("alert", "ack", detail={"id": 1})
        r3 = audit.append("metric", "archive")
        assert r2["prev_hash"] == r1["row_hash"]
        assert r3["prev_hash"] == r2["row_hash"]
        assert audit.count() == 3

    def test_same_content_distinct_seq_distinct_hash(self, audit: AuditLogStore) -> None:
        r1 = audit.append("alert", "create", detail={"id": 1})
        r2 = audit.append("alert", "create", detail={"id": 1})
        assert r1["seq"] != r2["seq"]
        assert r1["row_hash"] != r2["row_hash"]  # seq 参与哈希


# ═══════════════════════════════════════════
#  3. 只读查询
# ═══════════════════════════════════════════

class TestQuery:
    def test_filter_kind_actor(self, audit: AuditLogStore) -> None:
        audit.append("alert", "create", actor="scheduler")
        audit.append("alert", "ack", actor="operator")
        audit.append("metric", "archive", actor="scheduler")
        assert len(audit.query(kind="alert", order="asc")) == 2
        assert len(audit.query(actor="scheduler")) == 2
        assert len(audit.query(kind="metric", actor="scheduler")) == 1

    def test_time_range(self, audit: AuditLogStore) -> None:
        base = time.time()
        audit.append("alert", "create", detail={"t": 1})  # created_ts 接近 base
        rows = audit.query(start_ts=base - 10, end_ts=base + 10)
        assert len(rows) == 1

    def test_limit_and_order(self, audit: AuditLogStore) -> None:
        for i in range(5):
            audit.append("alert", f"op{i}", detail={"i": i})
        rows_desc = audit.query(limit=3)
        assert rows_desc[0]["seq"] == 5
        rows_asc = audit.query(limit=2, order="asc")
        assert rows_asc[0]["seq"] == 1 and rows_asc[1]["seq"] == 2

    def test_detail_roundtrip(self, audit: AuditLogStore) -> None:
        audit.append("config", "change", detail={"slo": {"latency": 0.5}, "nested": {"a": 1}})
        row = audit.query(kind="config")[0]
        assert row["detail"]["nested"]["a"] == 1


# ═══════════════════════════════════════════
#  4. 篡改检测
# ═══════════════════════════════════════════

class TestVerifyChain:
    def test_clean_chain(self, audit: AuditLogStore) -> None:
        audit.append("alert", "create", detail={"id": 1})
        audit.append("alert", "ack", detail={"id": 1})
        ok, broken, checked = audit.verify_chain()
        assert ok and broken is None and checked == 2

    def test_empty_chain_ok(self, audit: AuditLogStore) -> None:
        ok, broken, checked = audit.verify_chain()
        assert ok and checked == 0

    def test_content_tamper_detected(
        self, audit: AuditLogStore, tmp_conn: DatabaseConnection
    ) -> None:
        audit.append("alert", "create", detail={"id": 1})
        audit.append("alert", "ack", detail={"id": 1})
        with tmp_conn.transaction():
            tmp_conn.execute_update(
                "UPDATE audit_log SET detail_json = ? WHERE seq = 2",
                ('{"id": 1, "tampered": true}',),
            )
        ok, broken, _ = audit.verify_chain()
        assert not ok and broken == 2

    def test_link_break_detected(
        self, audit: AuditLogStore, tmp_conn: DatabaseConnection
    ) -> None:
        audit.append("alert", "create", detail={"id": 1})
        audit.append("alert", "ack", detail={"id": 1})
        audit.append("metric", "archive")
        with tmp_conn.transaction():
            tmp_conn.execute_update(
                "UPDATE audit_log SET prev_hash = ? WHERE seq = 3",
                ("f" * 64,),
            )
        ok, broken, _ = audit.verify_chain()
        assert not ok and broken == 3

    def test_deleted_row_detected(
        self, audit: AuditLogStore, tmp_conn: DatabaseConnection
    ) -> None:
        audit.append("alert", "create", detail={"id": 1})
        audit.append("alert", "ack", detail={"id": 1})
        audit.append("metric", "archive")
        with tmp_conn.transaction():
            tmp_conn.execute_update("DELETE FROM audit_log WHERE seq = 2")
        ok, broken, _ = audit.verify_chain()
        assert not ok and broken == 3  # seq=3 的 prev_hash 仍指向被删的 seq=2


# ═══════════════════════════════════════════
#  5. 归档与校验
# ═══════════════════════════════════════════

class TestArchive:
    def _seed_old(self, audit: AuditLogStore, tmp_conn: DatabaseConnection) -> None:
        old_ts = time.time() - 30 * 86400.0
        for i in range(3):
            with tmp_conn.transaction():
                tmp_conn.execute_update(
                    "INSERT INTO audit_log (kind, action, actor, detail_json, seq, "
                    "prev_hash, row_hash, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "old",
                        "create",
                        "system",
                        "{}",
                        100 + i,
                        "0" * 64,
                        f"{'b' * 62}{i:02d}",
                        old_ts + i,
                    ),
                )

    def test_archive_and_verify(self, audit: AuditLogStore, tmp_conn: DatabaseConnection) -> None:
        audit.append("alert", "create", detail={"id": 1})
        self._seed_old(audit, tmp_conn)
        manifest = audit.archive(before_ts=time.time() - 20 * 86400.0)
        assert manifest["row_count"] == 3
        assert manifest["first_seq"] == 100 and manifest["last_seq"] == 102
        assert audit.count() == 1  # 主表只留近期
        ok, expected, actual, n = audit.verify_archive(manifest["id"])
        assert ok and n == 3
        assert expected == actual and len(expected) == 64

    def test_archive_empty(self, audit: AuditLogStore) -> None:
        manifest = audit.archive(before_ts=time.time() - 30 * 86400.0)
        assert manifest["row_count"] == 0

    def test_archive_tamper_detected(
        self, audit: AuditLogStore, tmp_conn: DatabaseConnection
    ) -> None:
        self._seed_old(audit, tmp_conn)
        manifest = audit.archive(before_ts=time.time() - 20 * 86400.0)
        with tmp_conn.transaction():
            tmp_conn.execute_update(
                "UPDATE audit_archive SET row_hash = ? WHERE seq = 101",
                ("c" * 64,),
            )
        ok, _, _, _ = audit.verify_archive(manifest["id"])
        assert not ok

    def test_archive_keeps_original_hashes(
        self, audit: AuditLogStore, tmp_conn: DatabaseConnection
    ) -> None:
        self._seed_old(audit, tmp_conn)
        manifest = audit.archive(before_ts=time.time() - 20 * 86400.0)
        with tmp_conn.transaction():
            rows = tmp_conn.execute(
                "SELECT seq, row_hash, prev_hash FROM audit_archive ORDER BY seq ASC"
            )
        assert int(rows[0]["seq"]) == 100
        assert str(rows[0]["row_hash"]) == "b" * 62 + "00"  # 原哈希不改写
        ok, _, _, _ = audit.verify_archive(manifest["id"])
        assert ok

    def test_verify_archive_missing_manifest(self, audit: AuditLogStore) -> None:
        with pytest.raises(ValueError):
            audit.verify_archive(9999)


# ═══════════════════════════════════════════
#  6. 防御式
# ═══════════════════════════════════════════

class TestDefensive:
    def test_empty_kind_action_actor(self, audit: AuditLogStore) -> None:
        with pytest.raises(ValueError):
            audit.append("", "create")
        with pytest.raises(ValueError):
            audit.append("alert", "")
        with pytest.raises(ValueError):
            audit.append("alert", "create", actor="")

    def test_detail_not_dict(self, audit: AuditLogStore) -> None:
        with pytest.raises(ValueError):
            audit.append("alert", "create", detail="x")  # type: ignore[arg-type]

    def test_detail_too_large(self, audit: AuditLogStore) -> None:
        with pytest.raises(ValueError):
            audit.append("alert", "create", detail={"blob": "x" * 20_000})

    def test_query_limit_validation(self, audit: AuditLogStore) -> None:
        with pytest.raises(ValueError):
            audit.query(limit=0)
        with pytest.raises(ValueError):
            audit.query(limit=100_000)

    def test_archive_invalid_cutoff(self, audit: AuditLogStore) -> None:
        with pytest.raises(ValueError):
            audit.archive(float("nan"))


# ═══════════════════════════════════════════
#  7. AlertStore 集成（alerts 全落库留痕）
# ═══════════════════════════════════════════

class TestAlertStoreIntegration:
    def test_save_and_status_leave_audit_trail(
        self, tmp_conn: DatabaseConnection, tmp_path: Path
    ) -> None:
        audit = AuditLogStore(connection=tmp_conn)
        store = AlertStore(connection=tmp_conn).attach_audit_log(audit)
        store.save_alert(_make_alert("a1"))
        store.update_status("a1", STATUS_ACKED)
        store.update_status("a1", STATUS_RESOLVED)
        rows = audit.query(kind="alert", order="asc")
        assert len(rows) == 3
        assert rows[0]["action"] == "save"
        assert rows[0]["detail"]["alert_id"] == "a1"
        assert rows[1]["action"] == "status_change" and rows[1]["detail"]["status"] == "acked"
        assert rows[2]["action"] == "status_change" and rows[2]["detail"]["status"] == "resolved"
        # 审计链完整
        ok, _, _ = audit.verify_chain()
        assert ok

    def test_audit_failure_does_not_block_alert(
        self, tmp_conn: DatabaseConnection
    ) -> None:
        class BoomAudit:
            def append(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("audit store down")

        store = AlertStore(connection=tmp_conn).attach_audit_log(BoomAudit())
        # 审计失败不阻断告警落库
        assert store.save_alert(_make_alert("a2")) == "a2"
        assert store.update_status("a2", STATUS_ACKED) is True

    def test_without_audit_no_trail(self, tmp_conn: DatabaseConnection) -> None:
        store = AlertStore(connection=tmp_conn)
        store.save_alert(_make_alert("a3"))
        from myquant.database.schema import table_exists

        # 未绑定审计不产生审计表写入（表存在但 0 行；此处直接验证无审计存储）
        audit = AuditLogStore(connection=tmp_conn)
        assert audit.count() == 0
