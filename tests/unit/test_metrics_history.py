# 文件名：tests/unit/test_metrics_history.py
"""
test_metrics_history.py - BASE-2 指标时序存储单元测试
======================================================
覆盖：
  1. MetricsHistoryStore 建表自检（表存在/幂等/迁移 DDL 与 schema.py 逐字一致）
  2. 写入：save_metric 唯一幂等 / 标签 JSON / 防御式校验（空 name/非有限值/非 dict labels）
  3. 批量写入：save_batch（executemany 事务/上限/非法项）
  4. 查询：query（name/labels/时间范围/limit/order/防御）
  5. 降采样：query_downsampled（时间桶聚合/agg 白名单/防御）
  6. 保留清理：purge_before / retention_purge
  7. scheduler 集成（BASE-2 验收）：
     - HealthPatrol 绑定 MetricsHistoryStore → build_report 后落库可查
       （patrol_status + 各检查项状态计数，R-02 仅回放不检测）
     - store 未绑定时巡检不落库、不报错（降级）

运行方式:
  pytest tests/unit/test_metrics_history.py -v

设计原则（防未来）:
  所有测试使用历史固定时间戳与临时 sqlite，不依赖系统当前时间，不碰真实 quant.db。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.database.schema import (
    CREATE_METRICS_HISTORY_INDEX_SQL,
    CREATE_METRICS_HISTORY_SQL,
    TABLE_NAMES,
    table_exists,
)
from myquant.monitoring.metrics_history import (
    MAX_BATCH_SIZE,
    MetricsHistoryStore,
)
from myquant.live.scheduler import HealthPatrol

# 历史固定基准时间戳（防未来：不依赖系统当前时间）
BASE_TS = 1_700_000_000.0  # 2023-11-14 前后


# ═══════════════════════════════════════════
#  Fixture：临时库连接 + Store
# ═══════════════════════════════════════════

@pytest.fixture()
def tmp_conn(tmp_path: Path) -> DatabaseConnection:
    """临时 sqlite 连接（不碰真实 quant.db）"""
    return DatabaseConnection(tmp_path / "test_metrics.db")


@pytest.fixture()
def store(tmp_conn: DatabaseConnection) -> MetricsHistoryStore:
    """临时库 MetricsHistoryStore 实例"""
    return MetricsHistoryStore(connection=tmp_conn)


# ═══════════════════════════════════════════
#  1. 建表自检 + 迁移 DDL 一致性
# ═══════════════════════════════════════════

class TestSchemaSelfCheck:
    def test_table_created(self, store: MetricsHistoryStore, tmp_conn: DatabaseConnection) -> None:
        assert table_exists(tmp_conn, TABLE_NAMES["metrics_history"])

    def test_ensure_schema_idempotent(self, tmp_conn: DatabaseConnection) -> None:
        MetricsHistoryStore(connection=tmp_conn)
        MetricsHistoryStore(connection=tmp_conn)

    def test_migration_ddl_executable(self, tmp_path: Path) -> None:
        """0006 迁移 DDL 在临时 sqlite 可执行（upgrade SQL 正确性）"""
        import importlib
        conn = sqlite3.connect(str(tmp_path / "migrate_test.db"))
        try:
            m0006 = importlib.import_module(
                "myquant.database.migrations.versions.0006_add_metrics_history"
            )
            cur = conn.cursor()
            cur.execute(m0006._CREATE_METRICS_HISTORY)
            for sql in m0006._CREATE_INDEXES:
                cur.execute(sql)
            conn.commit()
            tables = {
                row[0] for row in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "metrics_history" in tables
            # 迁移 DDL 与 schema.py 逐字一致（BASE-2 要求）
            assert m0006._CREATE_METRICS_HISTORY == CREATE_METRICS_HISTORY_SQL
            assert m0006._CREATE_INDEXES == CREATE_METRICS_HISTORY_INDEX_SQL
        finally:
            conn.close()


# ═══════════════════════════════════════════
#  2. 写入
# ═══════════════════════════════════════════

class TestSaveMetric:
    def test_save_and_query_back(self, store: MetricsHistoryStore) -> None:
        assert store.save_metric("equity_curve_total", 1_000_000.0, timestamp=BASE_TS)
        rows = store.query("equity_curve_total", limit=10)
        assert len(rows) == 1
        assert rows[0]["value"] == 1_000_000.0
        assert rows[0]["labels"] == {}

    def test_unique_idempotent(self, store: MetricsHistoryStore) -> None:
        assert store.save_metric("m", 1.0, labels={"a": "1"}, timestamp=BASE_TS)
        # 同 name+labels+timestamp 二次写入被忽略（幂等）
        assert store.save_metric("m", 1.0, labels={"a": "1"}, timestamp=BASE_TS) is False
        assert len(store.query("m", limit=10)) == 1
        # 标签不同 → 新行
        assert store.save_metric("m", 2.0, labels={"a": "2"}, timestamp=BASE_TS)
        assert len(store.query("m", limit=10)) == 2

    def test_labels_json_keyorder_normalized(self, store: MetricsHistoryStore) -> None:
        """标签 dict 键顺序不同 → 同一行（sort_keys JSON 归一）"""
        store.save_metric("m", 1.0, labels={"a": "1", "b": "2"}, timestamp=BASE_TS)
        assert store.save_metric("m", 1.0, labels={"b": "2", "a": "1"}, timestamp=BASE_TS) is False

    def test_missing_name_rejected(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.save_metric("", 1.0)

    def test_non_finite_value_rejected(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.save_metric("m", float("nan"), timestamp=BASE_TS)

    def test_non_dict_labels_rejected(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.save_metric("m", 1.0, labels="bad", timestamp=BASE_TS)  # type: ignore


# ═══════════════════════════════════════════
#  3. 批量写入
# ═══════════════════════════════════════════

class TestSaveBatch:
    def test_batch_insert(self, store: MetricsHistoryStore) -> None:
        rows = [
            {"name": "m", "value": float(i), "timestamp": BASE_TS + i} for i in range(5)
        ]
        assert store.save_batch(rows) == 5
        assert len(store.query("m", limit=10)) == 5

    def test_batch_ignores_duplicates(self, store: MetricsHistoryStore) -> None:
        rows = [
            {"name": "m", "value": 1.0, "timestamp": BASE_TS},
            {"name": "m", "value": 1.0, "timestamp": BASE_TS},  # 重复
        ]
        assert store.save_batch(rows) == 1

    def test_batch_empty_returns_zero(self, store: MetricsHistoryStore) -> None:
        assert store.save_batch([]) == 0

    def test_batch_too_large_rejected(self, store: MetricsHistoryStore) -> None:
        rows = [{"name": "m", "value": 1.0} for _ in range(MAX_BATCH_SIZE + 1)]
        with pytest.raises(ValueError):
            store.save_batch(rows)

    def test_batch_invalid_row_rejected(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.save_batch([{"name": "m", "value": "x"}])  # type: ignore


# ═══════════════════════════════════════════
#  4. 查询
# ═══════════════════════════════════════════

class TestQuery:
    def _seed(self, store: MetricsHistoryStore) -> None:
        store.save_metric("equity", 100.0, timestamp=BASE_TS)
        store.save_metric("equity", 101.0, timestamp=BASE_TS + 60)
        store.save_metric("equity", 102.0, timestamp=BASE_TS + 120)
        store.save_metric("patrol_status", 0.0, labels={"status": "healthy"}, timestamp=BASE_TS)

    def test_order_desc_default(self, store: MetricsHistoryStore) -> None:
        self._seed(store)
        rows = store.query("equity", limit=10)
        assert [r["timestamp"] for r in rows] == [BASE_TS + 120, BASE_TS + 60, BASE_TS]

    def test_order_asc(self, store: MetricsHistoryStore) -> None:
        self._seed(store)
        rows = store.query("equity", limit=10, order="asc")
        assert [r["timestamp"] for r in rows] == [BASE_TS, BASE_TS + 60, BASE_TS + 120]

    def test_time_range_filter(self, store: MetricsHistoryStore) -> None:
        self._seed(store)
        rows = store.query("equity", start_ts=BASE_TS + 1, end_ts=BASE_TS + 119, limit=10)
        assert len(rows) == 1 and rows[0]["timestamp"] == BASE_TS + 60

    def test_labels_filter(self, store: MetricsHistoryStore) -> None:
        self._seed(store)
        rows = store.query("patrol_status", labels={"status": "healthy"}, limit=10)
        assert len(rows) == 1

    def test_limit(self, store: MetricsHistoryStore) -> None:
        self._seed(store)
        assert len(store.query("equity", limit=2)) == 2

    def test_invalid_limit_rejected(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.query(limit=0)


# ═══════════════════════════════════════════
#  5. 降采样聚合
# ═══════════════════════════════════════════

class TestDownsample:
    def test_bucket_avg(self, store: MetricsHistoryStore) -> None:
        # 3 点各占一个 60 分钟桶（base, base+1h, base+2h）→ 每桶 avg=各自值
        for i in range(3):
            store.save_metric("m", float(i), timestamp=BASE_TS + 3600 * i)
        buckets = store.query_downsampled("m", bucket_minutes=60, agg="avg")
        assert len(buckets) == 3
        assert [b["value"] for b in buckets] == [0.0, 1.0, 2.0]

    def test_bucket_count(self, store: MetricsHistoryStore) -> None:
        store.save_metric("m", 1.0, timestamp=BASE_TS)
        store.save_metric("m", 2.0, timestamp=BASE_TS + 30)
        store.save_metric("m", 3.0, timestamp=BASE_TS + 3600)
        buckets = store.query_downsampled("m", bucket_minutes=60, agg="count")
        assert buckets[0]["count"] == 2 and buckets[1]["count"] == 1

    def test_invalid_agg_rejected(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.query_downsampled("m", agg="median")

    def test_invalid_bucket_rejected(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.query_downsampled("m", bucket_minutes=0)


# ═══════════════════════════════════════════
#  6. 保留清理
# ═══════════════════════════════════════════

class TestRetention:
    def test_purge_before(self, store: MetricsHistoryStore) -> None:
        store.save_metric("m", 1.0, timestamp=BASE_TS)
        store.save_metric("m", 2.0, timestamp=BASE_TS + 100)
        assert store.purge_before(BASE_TS + 50) == 1
        assert len(store.query("m", limit=10)) == 1

    def test_retention_purge_invalid_days(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.retention_purge(retention_days=0)


# ═══════════════════════════════════════════
#  7. scheduler 集成（BASE-2 验收）
# ═══════════════════════════════════════════

class TestSchedulerIntegration:
    def test_patrol_records_metrics(self, tmp_path: Path) -> None:
        """验收：巡检完成 → 指标落库可查（patrol_status + 各检查项计数）"""
        conn = DatabaseConnection(tmp_path / "patrol_test.db")
        store = MetricsHistoryStore(connection=conn)
        patrol = HealthPatrol(metrics_history=store)

        # 组件全 None → 全部 down，汇总 status=down
        report = patrol.build_report(engine=None, data_feed=None, broker=None,
                                     portfolio=None, risk_manager=None)
        assert report["status"] == "down"

        # 落库校验
        rows = store.query("patrol_status", labels={"status": "down"}, limit=10)
        assert len(rows) == 1 and rows[0]["value"] == 2.0
        assert store.query("patrol_down_count", limit=10)[0]["value"] == 5.0
        check_rows = store.query("patrol_check.engine", limit=10)
        assert len(check_rows) == 1 and check_rows[0]["value"] == 2.0

    def test_patrol_healthy_recorded(self, tmp_path: Path) -> None:
        """全部组件健康 → patrol_status=healthy(0)"""
        conn = DatabaseConnection(tmp_path / "patrol_ok.db")
        store = MetricsHistoryStore(connection=conn)
        patrol = HealthPatrol(metrics_history=store)

        class _HealthyEngine:
            """引擎：get_status() 返回 RUNNING"""
            @staticmethod
            def get_status() -> Dict[str, Any]:
                return {"state": "RUNNING", "stats": {}}

        class _HealthyFeed:
            """行情源：方法式 is_connected/is_running/get_subscribed_symbols"""
            def is_connected(self) -> bool:
                return True
            def is_running(self) -> bool:
                return True
            def get_subscribed_symbols(self) -> list:
                return []

        class _HealthyBroker:
            """券商：属性式 is_connected=True"""
            @property
            def is_connected(self) -> bool:
                return True

        class _HealthyPortfolio:
            """持仓：get_account() 返回账户总览"""
            @staticmethod
            def get_account() -> Dict[str, Any]:
                return {"available_cash": 500_000.0}

        class _HealthyRisk:
            """风控：get_summary() 返回 NORMAL"""
            @staticmethod
            def get_summary() -> Dict[str, Any]:
                return {"state": "NORMAL"}

        report = patrol.build_report(
            engine=_HealthyEngine(),
            data_feed=_HealthyFeed(),
            broker=_HealthyBroker(),
            portfolio=_HealthyPortfolio(),
            risk_manager=_HealthyRisk(),
        )
        assert report["status"] == "healthy"
        rows = store.query("patrol_status", labels={"status": "healthy"}, limit=10)
        assert len(rows) == 1 and rows[0]["value"] == 0.0

    def test_patrol_without_store_no_error(self, tmp_path: Path) -> None:
        """未绑定 store → 巡检正常完成，不落库不报错（降级）"""
        patrol = HealthPatrol(metrics_history=None)
        report = patrol.build_report()
        assert report["status"] == "down"  # 全 None → down

    def test_patrol_store_failure_does_not_block(self, tmp_path: Path) -> None:
        """store 落库异常 → 巡检结果仍返回（落库失败不阻断巡检）"""
        class _BrokenStore:
            def save_batch(self, rows: Any) -> int:
                raise RuntimeError("boom")

        patrol = HealthPatrol(metrics_history=_BrokenStore())
        report = patrol.build_report()
        assert report["status"] == "down"
