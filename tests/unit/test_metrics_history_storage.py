# 文件名：tests/unit/test_metrics_history_storage.py
"""
test_metrics_history_storage.py - BASE-6 存储优化单元测试
==========================================================
覆盖：
  1. 迁移接口预留：默认空导出 / 注册表（null/vm/clickhouse）/ 非法名 /
     set_exporter 校验 / 导出钩子（落库后副本导出，失败不阻断）
  2. 异步批量写入：enqueue 队列 / flush 落库 / 超限丢弃 / 后台守护线程
     flush 与收尾落库（避免热路径直写）
  3. 降采样物化：>=30 天前旧数据按桶聚合物化 / 幂等 / 物化行可查询 /
     标签带 source/bucket/agg；物化 365 天清理（不碰原始行）
  4. DownsamplingTask 独立调度：run 全量+指定名 / start/stop / 参数校验
  5. database/connection：默认 pragma 含 WAL + busy_timeout（高写入写等待
     而非锁表）

运行方式:
  pytest tests/unit/test_metrics_history_storage.py -v
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from myquant.database.connection import DatabaseConnection, DEFAULT_PRAGMAS
from myquant.core.metrics_history import (
    DEFAULT_DOWNSAMPLE_INTERVAL_SECONDS,
    DEFAULT_QUEUE_LIMIT,
    DownsamplingTask,
    MAX_BATCH_SIZE,
    MetricExporter,
    MetricsHistoryStore,
    NullMetricExporter,
    _EXPORTER_REGISTRY,
    get_exporter,
)


@pytest.fixture()
def tmp_conn(tmp_path: Path) -> DatabaseConnection:
    return DatabaseConnection(tmp_path / "storage_test.db")


@pytest.fixture()
def store(tmp_conn: DatabaseConnection) -> MetricsHistoryStore:
    return MetricsHistoryStore(connection=tmp_conn)


# ═══════════════════════════════════════════
#  1. 迁移接口预留
# ═══════════════════════════════════════════

class TestExporter:
    def test_default_null_exporter(self, store: MetricsHistoryStore) -> None:
        assert isinstance(store._exporter, NullMetricExporter)

    def test_registry_names(self) -> None:
        assert set(_EXPORTER_REGISTRY.keys()) == {"null", "vm", "clickhouse"}
        assert isinstance(get_exporter("null"), NullMetricExporter)
        assert isinstance(get_exporter("VM"), MetricExporter)  # 大小写归一

    def test_placeholder_raises(self) -> None:
        for name in ("vm", "clickhouse"):
            with pytest.raises(NotImplementedError):
                get_exporter(name).export_batch([{"name": "x", "value": 1.0}])

    def test_unknown_exporter_raises(self) -> None:
        with pytest.raises(ValueError):
            get_exporter("influxdb")

    def test_set_exporter_validation(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.set_exporter("not-an-exporter")  # type: ignore[arg-type]
        store.set_exporter(None)  # 重置回空导出
        assert isinstance(store._exporter, NullMetricExporter)

    def test_export_hook_receives_rows(self, store: MetricsHistoryStore) -> None:
        seen: List[Dict[str, Any]] = []

        class FakeExporter(MetricExporter):
            def export_batch(self, rows: List[Dict[str, Any]]) -> int:
                seen.extend(rows)
                return len(rows)

        store.set_exporter(FakeExporter())
        store.save_metric("eq", 100.0, labels={"s": "a"})
        store.save_batch([{"name": "eq2", "value": 1.0}])
        assert len(seen) == 2
        assert seen[0]["name"] == "eq" and seen[0]["labels"] == {"s": "a"}

    def test_export_failure_does_not_block_save(self, store: MetricsHistoryStore) -> None:
        class BoomExporter(MetricExporter):
            def export_batch(self, rows: List[Dict[str, Any]]) -> int:
                raise RuntimeError("external down")

        store.set_exporter(BoomExporter())
        assert store.save_metric("eq", 1.0) is True  # 落库成功，导出失败仅记 warning


# ═══════════════════════════════════════════
#  2. 异步批量写入
# ═══════════════════════════════════════════

class TestAsyncWrite:
    def test_enqueue_and_flush(self, store: MetricsHistoryStore) -> None:
        for i in range(3):
            store.enqueue({"name": "q", "value": float(i), "timestamp": 1_700_000_000.0 + i})
        assert store.queue_size() == 3
        assert store.flush() == 3
        assert store.queue_size() == 0
        assert len(store.query("q", limit=10)) == 3

    def test_flush_empty(self, store: MetricsHistoryStore) -> None:
        assert store.flush() == 0

    def test_queue_limit_drops_oldest(self, store: MetricsHistoryStore) -> None:
        for i in range(DEFAULT_QUEUE_LIMIT + 5):
            store.enqueue({"name": "q", "value": float(i), "timestamp": float(i)})
        assert store.queue_size() == DEFAULT_QUEUE_LIMIT

    def test_background_flush_thread(self, store: MetricsHistoryStore) -> None:
        assert store.start_background_flush(interval_seconds=0.2) is True
        store.enqueue({"name": "bg", "value": 1.0, "timestamp": 1_700_000_000.0})
        time.sleep(0.6)
        assert len(store.query("bg", limit=5)) == 1
        # 幂等启动
        assert store.start_background_flush(0.2) is True
        assert store.stop_background_flush(final_flush=True) >= 0

    def test_background_flush_final_drain(self, store: MetricsHistoryStore) -> None:
        store.start_background_flush(interval_seconds=0.2)
        store.enqueue({"name": "bg2", "value": 2.0, "timestamp": 1_700_000_000.0})
        tail = store.stop_background_flush(final_flush=True)  # 立即停+收尾
        assert tail == 1
        assert len(store.query("bg2", limit=5)) == 1

    def test_invalid_interval(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.start_background_flush(0)


# ═══════════════════════════════════════════
#  3. 降采样物化
# ═══════════════════════════════════════════

class TestMaterialize:
    def _seed_old(self, store: MetricsHistoryStore, n: int = 3) -> float:
        old_ts = time.time() - 31 * 86400.0
        for i in range(n):
            store.save_metric("ds_src", float(i), timestamp=old_ts + 60 * i)
        return old_ts

    def test_materialize_buckets(self, store: MetricsHistoryStore) -> None:
        self._seed_old(store, n=3)
        assert store.materialize_downsampled("ds_src", bucket_minutes=1, older_than_days=30) == 3
        rows = store.query("ds_src:ds:1m", limit=10)
        assert len(rows) == 3
        assert {r["value"] for r in rows} == {0.0, 1.0, 2.0}
        # 物化行标签带溯源
        assert rows[0]["labels"]["source"] == "ds_src"
        assert rows[0]["labels"]["bucket_minutes"] == 1
        assert rows[0]["labels"]["agg"] == "avg"

    def test_materialize_idempotent(self, store: MetricsHistoryStore) -> None:
        self._seed_old(store, n=3)
        store.materialize_downsampled("ds_src", bucket_minutes=1, older_than_days=30)
        assert store.materialize_downsampled("ds_src", bucket_minutes=1, older_than_days=30) == 0
        assert len(store.query("ds_src:ds:1m", limit=10)) == 3

    def test_recent_data_not_materialized(self, store: MetricsHistoryStore) -> None:
        store.save_metric("ds_src", 1.0, timestamp=time.time())  # 今天的数据
        assert store.materialize_downsampled("ds_src", bucket_minutes=1, older_than_days=30) == 0

    def test_downsample_retention_purge(self, store: MetricsHistoryStore) -> None:
        self._seed_old(store, n=3)
        store.materialize_downsampled("ds_src", bucket_minutes=1, older_than_days=30)
        # 366 天前的物化行 + 366 天前的原始行（原始行不受物化清理影响）
        store.save_metric("ds_src:ds:1m", 9.9, timestamp=time.time() - 366 * 86400.0)
        store.save_metric("ds_raw_old", 8.8, timestamp=time.time() - 366 * 86400.0)
        purged = store.downsample_retention_purge(downsample_days=365)
        assert purged == 1  # 只清物化行
        assert len(store.query("ds_raw_old", limit=5)) == 1  # 原始行保留

    def test_downsample_purge_prefix(self, store: MetricsHistoryStore) -> None:
        store.save_metric("patrol_status:ds:1m", 1.0, timestamp=time.time() - 400 * 86400.0)
        store.save_metric("other:ds:1m", 2.0, timestamp=time.time() - 400 * 86400.0)
        purged = store.downsample_retention_purge(downsample_days=365, name_prefix="patrol_status")
        assert purged == 1
        assert len(store.query("other:ds:1m", limit=5)) == 1

    def test_materialize_validation(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            store.materialize_downsampled("", bucket_minutes=1)
        with pytest.raises(ValueError):
            store.materialize_downsampled("x", bucket_minutes=0)
        with pytest.raises(ValueError):
            store.materialize_downsampled("x", bucket_minutes=1, agg="median")


# ═══════════════════════════════════════════
#  4. DownsamplingTask 独立调度
# ═══════════════════════════════════════════

class TestDownsamplingTask:
    def test_run_returns_result(self, store: MetricsHistoryStore) -> None:
        old_ts = time.time() - 31 * 86400.0
        store.save_metric("t_src", 1.0, timestamp=old_ts)
        task = DownsamplingTask(store, bucket_minutes=1, older_than_days=30)
        res = task.run()
        assert res["materialized"] >= 1
        assert "purged_raw" in res and "purged_downsample" in res
        assert task.last_result is res

    def test_run_with_names_subset(self, store: MetricsHistoryStore) -> None:
        old_ts = time.time() - 31 * 86400.0
        store.save_metric("t_a", 1.0, timestamp=old_ts)
        store.save_metric("t_b", 2.0, timestamp=old_ts)
        task = DownsamplingTask(store, bucket_minutes=1, older_than_days=30, names=["t_a"])
        res = task.run()
        assert res["materialized"] == 1
        assert len(store.query("t_b:ds:1m", limit=5)) == 0

    def test_start_stop_idempotent(self, store: MetricsHistoryStore) -> None:
        task = DownsamplingTask(store, bucket_minutes=1, older_than_days=30)
        assert task.start(interval_seconds=0.2) is True
        assert task.start(0.2) is True  # 幂等
        time.sleep(0.5)
        assert task.stop(run_once=False) is True
        assert task.stop() is True  # 已停止幂等

    def test_task_validation(self, store: MetricsHistoryStore) -> None:
        with pytest.raises(ValueError):
            DownsamplingTask(store, bucket_minutes=0)
        with pytest.raises(ValueError):
            DownsamplingTask(store, older_than_days=0)
        with pytest.raises(ValueError):
            DownsamplingTask(store, agg="median")


# ═══════════════════════════════════════════
#  5. connection 默认 pragma（WAL + busy_timeout）
# ═══════════════════════════════════════════

class TestConnectionPragmas:
    def test_default_pragmas_include_wal_and_busy(self) -> None:
        assert DEFAULT_PRAGMAS["journal_mode"] == "WAL"
        assert int(DEFAULT_PRAGMAS["busy_timeout"]) >= 30000  # 30s 写等待

    def test_connection_applies_busy_timeout(self, tmp_conn: DatabaseConnection) -> None:
        tmp_conn.connect()
        try:
            row = tmp_conn.execute("PRAGMA busy_timeout")
            assert int(row[0][0]) >= 30000
        finally:
            tmp_conn.close()
