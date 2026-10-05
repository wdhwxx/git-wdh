# 文件名：tests/unit/test_alert_store.py
"""
test_alert_store.py - BASE-1 告警持久化单元测试
=================================================
覆盖：
  1. AlertStore 建表自检（表存在/幂等/迁移 DDL 与 schema.py 逐字一致）
  2. 落库：save_alert → get / details 反序列化 / status 推导
  3. 状态流转：update_status（open/acked/resolved 时间戳语义）
  4. 分类打标：set_classification（P0-8 统计脚本读取源，R-01）
  5. 升级标记 / Runbook 绑定
  6. 查询：load_history 过滤 / load_unacknowledged / load_escalated /
     get_open_by_dedup_key（恢复匹配）
  7. 防御式校验：非法 status / limit<=0 / 缺 alert_id / 缺 level / 缺 message
  8. AlertManager 集成（BASE-1 验收）：
     - send_alert 落库（重启后新 manager + 同 store 可查 → 历史可查）
     - acknowledge / resolve 同步库
     - 恢复通知：pipeline_complete(completed) → "告警→恢复"成对记录 + 置 resolved
  9. 迁移 SQL 可执行性：0005 upgrade/downgrade DDL 在临时 sqlite 可执行

运行方式:
  pytest tests/unit/test_alert_store.py -v

设计原则（防未来）:
  所有测试使用历史固定时间戳与临时 sqlite，不依赖系统当前时间，不碰真实 quant.db。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.database.schema import (
    CREATE_ALERTS_INDEX_SQL,
    CREATE_ALERTS_SQL,
    TABLE_NAMES,
    table_exists,
)
from myquant.monitoring.alert_store import (
    STATUS_ACKED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    AlertStore,
)
from myquant.monitoring.alerting import (
    Alert,
    AlertManager,
    SeverityLevel,
)


# ═══════════════════════════════════════════
#  Fixture：临时库连接 + AlertStore
# ═══════════════════════════════════════════

@pytest.fixture()
def tmp_conn(tmp_path: Path) -> DatabaseConnection:
    """临时 sqlite 连接（不碰真实 quant.db）"""
    conn = DatabaseConnection(tmp_path / "test_alerts.db")
    return conn


@pytest.fixture()
def store(tmp_conn: DatabaseConnection) -> AlertStore:
    """临时库 AlertStore 实例"""
    return AlertStore(connection=tmp_conn)


def _make_alert(
    alert_id: str = "a1",
    level: SeverityLevel = SeverityLevel.WARNING,
    message: str = "数据质量告警: 价格跳变",
    dedup_key: str = "agg:000001.SZ:daily:price_jump",
    category: str = "data_quality",
    timestamp: float = 1000.0,
    acknowledged: bool = False,
    resolved: bool = False,
) -> Alert:
    """构造测试 Alert（历史固定时间戳，防未来）"""
    return Alert(
        level=level,
        message=message,
        source="event_bus.data_quality.aggregated",
        category=category,
        details={"dirty_type": "price_jump", "symbol": "000001.SZ"},
        deduplication_key=dedup_key,
        timestamp=timestamp,
        alert_id=alert_id,
        acknowledged=acknowledged,
        resolved=resolved,
    )


# ═══════════════════════════════════════════
#  1. 建表自检 + 迁移 DDL 一致性
# ═══════════════════════════════════════════

class TestSchemaSelfCheck:
    def test_table_created(self, store: AlertStore, tmp_conn: DatabaseConnection) -> None:
        assert table_exists(tmp_conn, TABLE_NAMES["alerts"])

    def test_ensure_schema_idempotent(self, tmp_conn: DatabaseConnection) -> None:
        # 重复实例化不报错（CREATE IF NOT EXISTS）
        AlertStore(connection=tmp_conn)
        AlertStore(connection=tmp_conn)

    def test_migration_ddl_executable(self, tmp_path: Path) -> None:
        """0005 迁移 DDL 在临时 sqlite 可执行（upgrade SQL 正确性）"""
        import importlib
        conn = sqlite3.connect(str(tmp_path / "migrate_test.db"))
        try:
            # 模拟 0005 upgrade：执行迁移模块内 DDL
            # 模块名以数字开头，不能 import 语句，用 importlib 按路径导入
            m0005 = importlib.import_module(
                "myquant.database.migrations.versions.0005_add_alerts"
            )
            cur = conn.cursor()
            cur.execute(m0005._CREATE_ALERTS)
            for sql in m0005._CREATE_INDEXES:
                cur.execute(sql)
            conn.commit()
            tables = {
                row[0] for row in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "alerts" in tables
            # 迁移 DDL 与 schema.py 逐字一致（BASE-1 要求）
            assert m0005._CREATE_ALERTS == CREATE_ALERTS_SQL
            assert m0005._CREATE_INDEXES == CREATE_ALERTS_INDEX_SQL
        finally:
            conn.close()


# ═══════════════════════════════════════════
#  2. 落库
# ═══════════════════════════════════════════

class TestSaveAlert:
    def test_save_and_get(self, store: AlertStore) -> None:
        alert = _make_alert()
        store.save_alert(alert)
        row = store.get("a1")
        assert row is not None
        # SeverityLevel.value 为小写（warning/error/...），落库一致
        assert row["level"] == SeverityLevel.WARNING.value
        assert row["category"] == "data_quality"
        assert row["status"] == "open"
        assert row["deduplication_key"] == "agg:000001.SZ:daily:price_jump"

    def test_details_roundtrip(self, store: AlertStore) -> None:
        alert = _make_alert()
        store.save_alert(alert)
        assert store.get("a1")["details"]["symbol"] == "000001.SZ"

    def test_upsert_same_id_idempotent(self, store: AlertStore) -> None:
        store.save_alert(_make_alert(message="v1"))
        store.save_alert(_make_alert(message="v2"))
        row = store.get("a1")
        assert row is not None and row["message"] == "v2"

    def test_status_from_flags(self, store: AlertStore) -> None:
        acked = _make_alert(alert_id="b1", acknowledged=True)
        resolved = _make_alert(alert_id="b2", resolved=True)
        store.save_alert(acked)
        store.save_alert(resolved)
        assert store.get("b1")["status"] == "acked"
        assert store.get("b2")["status"] == "resolved"

    def test_missing_alert_id_rejected(self, store: AlertStore) -> None:
        alert = _make_alert()
        alert.alert_id = ""
        with pytest.raises(ValueError):
            store.save_alert(alert)

    def test_missing_message_rejected(self, store: AlertStore) -> None:
        alert = _make_alert(message="")
        with pytest.raises(ValueError):
            store.save_alert(alert)


# ═══════════════════════════════════════════
#  3. 状态流转
# ═══════════════════════════════════════════

class TestStatusFlow:
    def test_ack_sets_acked_at(self, store: AlertStore) -> None:
        store.save_alert(_make_alert())
        assert store.update_status("a1", STATUS_ACKED)
        row = store.get("a1")
        assert row is not None
        assert row["status"] == "acked"
        assert row["acked_at"] is not None
        assert row["resolved_at"] is None

    def test_resolve_sets_both_ts(self, store: AlertStore) -> None:
        store.save_alert(_make_alert())
        assert store.update_status("a1", STATUS_RESOLVED)
        row = store.get("a1")
        assert row is not None
        assert row["status"] == "resolved"
        assert row["resolved_at"] is not None
        # 未确认过也补 acked_at（COALESCE 语义）
        assert row["acked_at"] is not None

    def test_reopen_clears_ts(self, store: AlertStore) -> None:
        store.save_alert(_make_alert())
        store.update_status("a1", STATUS_RESOLVED)
        assert store.update_status("a1", STATUS_OPEN)
        row = store.get("a1")
        assert row is not None
        assert row["acked_at"] is None and row["resolved_at"] is None

    def test_unknown_status_rejected(self, store: AlertStore) -> None:
        store.save_alert(_make_alert())
        with pytest.raises(ValueError):
            store.update_status("a1", "weird")


# ═══════════════════════════════════════════
#  4. 分类打标（P0-8 读取源）
# ═══════════════════════════════════════════

class TestClassification:
    def test_set_and_read(self, store: AlertStore) -> None:
        store.save_alert(_make_alert())
        assert store.set_classification("a1", "FP")
        assert store.get("a1")["classification"] == "FP"

    def test_default_empty(self, store: AlertStore) -> None:
        store.save_alert(_make_alert())
        assert store.get("a1")["classification"] == ""

    def test_save_with_classification(self, store: AlertStore) -> None:
        store.save_alert(_make_alert(), classification="TP")
        assert store.get("a1")["classification"] == "TP"


# ═══════════════════════════════════════════
#  5. 升级标记 / Runbook 绑定
# ═══════════════════════════════════════════

class TestEscalationAndRunbook:
    def test_mark_escalated(self, store: AlertStore) -> None:
        store.save_alert(_make_alert())
        assert store.mark_escalated("a1")
        assert store.get("a1")["escalated"] is True

    def test_set_runbook_id(self, store: AlertStore) -> None:
        store.save_alert(_make_alert())
        assert store.set_runbook_id("a1", "rb-market-quote-stall")
        assert store.get("a1")["runbook_id"] == "rb-market-quote-stall"


# ═══════════════════════════════════════════
#  6. 查询
# ═══════════════════════════════════════════

class TestQuery:
    def test_load_history_order_and_limit(self, store: AlertStore) -> None:
        store.save_alert(_make_alert(alert_id="a1", timestamp=1000.0))
        store.save_alert(_make_alert(alert_id="a2", timestamp=2000.0))
        rows = store.load_history(limit=10)
        assert [r["alert_id"] for r in rows] == ["a2", "a1"]  # created_at 倒序

    def test_load_history_filters(self, store: AlertStore) -> None:
        store.save_alert(_make_alert(alert_id="a1", level=SeverityLevel.WARNING))
        store.save_alert(_make_alert(alert_id="a2", level=SeverityLevel.ERROR))
        # level 过滤值须与落库值一致（SeverityLevel.value 小写）
        rows = store.load_history(limit=10, level=SeverityLevel.ERROR.value)
        assert [r["alert_id"] for r in rows] == ["a2"]
        rows = store.load_history(limit=10, status="open")
        assert len(rows) == 2

    def test_load_unacknowledged(self, store: AlertStore) -> None:
        store.save_alert(_make_alert(alert_id="a1"))
        store.save_alert(_make_alert(alert_id="a2", acknowledged=True))
        rows = store.load_unacknowledged(limit=10)
        assert [r["alert_id"] for r in rows] == ["a1"]

    def test_load_escalated(self, store: AlertStore) -> None:
        store.save_alert(_make_alert(alert_id="a1"))
        store.save_alert(_make_alert(alert_id="a2"))
        store.mark_escalated("a2")
        rows = store.load_escalated(limit=10)
        assert [r["alert_id"] for r in rows] == ["a2"]

    def test_get_open_by_dedup_key(self, store: AlertStore) -> None:
        store.save_alert(_make_alert(alert_id="a1"))
        store.save_alert(_make_alert(alert_id="a2", resolved=True))
        rows = store.get_open_by_dedup_key("agg:000001.SZ:daily:price_jump")
        # 只返回未解决的同 key 告警（恢复通知匹配）
        assert [r["alert_id"] for r in rows] == ["a1"]

    def test_load_history_limit_invalid(self, store: AlertStore) -> None:
        with pytest.raises(ValueError):
            store.load_history(limit=0)


# ═══════════════════════════════════════════
#  7. AlertManager 集成（BASE-1 验收）
# ═══════════════════════════════════════════

class TestAlertManagerIntegration:
    def _manager_with_store(self, tmp_path: Path) -> tuple:
        conn = DatabaseConnection(tmp_path / "am_test.db")
        store = AlertStore(connection=conn)
        manager = AlertManager(store=store)
        return manager, store

    def test_send_alert_persists(self, tmp_path: Path) -> None:
        manager, store = self._manager_with_store(tmp_path)
        alert = _make_alert()
        manager.send_alert(alert, dedup_enabled=False)
        assert store.get("a1") is not None
        assert manager.stats["store_enabled"] is True

    def test_history_readable_after_restart(self, tmp_path: Path) -> None:
        """验收：重启后 /api/alerts 历史可查（新 manager + 同 store → 库内存双读）"""
        conn = DatabaseConnection(tmp_path / "restart_test.db")
        store1 = AlertStore(connection=conn)
        m1 = AlertManager(store=store1)
        m1.send_alert(_make_alert(alert_id="a1"), dedup_enabled=False)

        # 模拟重启：同一库、新 manager（内存空）
        store2 = AlertStore(connection=conn)
        m2 = AlertManager(store=store2)
        history = m2.get_history(limit=50)
        assert any(a.alert_id == "a1" for a in history)

    def test_acknowledge_syncs_store(self, tmp_path: Path) -> None:
        manager, store = self._manager_with_store(tmp_path)
        manager.send_alert(_make_alert(), dedup_enabled=False)
        assert manager.acknowledge("a1")
        assert store.get("a1")["status"] == "acked"

    def test_resolve_syncs_store(self, tmp_path: Path) -> None:
        manager, store = self._manager_with_store(tmp_path)
        manager.send_alert(_make_alert(), dedup_enabled=False)
        assert manager.resolve("a1")
        assert store.get("a1")["status"] == "resolved"

    def test_acknowledge_after_restart_hits_store(self, tmp_path: Path) -> None:
        """重启后内存无该告警，acknowledge 直接落库（库内存双读）"""
        conn = DatabaseConnection(tmp_path / "restart_ack.db")
        store1 = AlertStore(connection=conn)
        AlertManager(store=store1).send_alert(_make_alert(alert_id="a1"), dedup_enabled=False)
        store2 = AlertStore(connection=conn)
        m2 = AlertManager(store=store2)
        assert m2.acknowledge("a1")
        assert store2.get("a1")["status"] == "acked"

    def test_recovery_notice_creates_paired_records(self, tmp_path: Path) -> None:
        """验收：断流→恢复产生'告警→恢复'成对记录 + 原告警置 resolved"""
        manager, store = self._manager_with_store(tmp_path)
        # 先产生未解决的 data_quality 告警（断流）
        manager.send_alert(_make_alert(alert_id="a1"), dedup_enabled=False)
        assert store.get("a1")["status"] == "open"

        # 模拟健康恢复：pipeline_complete(status=completed)
        event = SimpleNamespace(
            status="completed",
            quality_grade="good",
            bad_ratio=0.0,
            symbol="000001.SZ",
            data_type="daily",
            cleaned_count=100,
            bad_count=0,
            total_count=100,
            duration=1.5,
            error="",
            steps_executed=[],
        )
        manager.handle_pipeline_complete_event(event)

        # 原告警已置 resolved（内存 + 库）
        assert store.get("a1")["status"] == "resolved"
        # 产生恢复通知（INFO，成对记录）
        recovered = [a for a in manager.get_history(limit=50)
                     if a.message.startswith("[恢复]")]
        assert len(recovered) == 1
        assert recovered[0].level == SeverityLevel.INFO
        assert recovered[0].deduplication_key == "recovered:agg:000001.SZ:daily:price_jump"
        assert recovered[0].parent_alert_id == "a1"

    def test_no_recovery_when_pipeline_failed(self, tmp_path: Path) -> None:
        """流水线失败不是恢复信号，不触发恢复通知"""
        manager, store = self._manager_with_store(tmp_path)
        manager.send_alert(_make_alert(alert_id="a1"), dedup_enabled=False)
        event = SimpleNamespace(
            status="failed",
            symbol="000001.SZ",
            data_type="daily",
            error="boom",
            duration=1.0,
            cleaned_count=0,
            bad_count=0,
            total_count=0,
            quality_grade="",
            bad_ratio=0.0,
            steps_executed=[],
        )
        manager.handle_pipeline_complete_event(event)
        assert store.get("a1")["status"] == "open"  # 未置 resolved
        assert not [a for a in manager.get_history(limit=50) if a.message.startswith("[恢复]")]
