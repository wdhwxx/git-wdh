# 文件名：tests/unit/test_data_versioner.py
"""
F-07 落地 / F-15 配套：data_versioner 数据集口径版本登记测试。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from myquant.utils.data_versioner import (
    DATASET_SCHEMA_REVISIONS,
    DatasetVersion,
    DataVersionMismatchError,
    assert_versions_compatible,
    get_all_dataset_versions,
    get_dataset_version,
    load_result_version_sidecar,
    write_result_version_sidecar,
)


class _FakeConn:
    """轻量 fake 连接：直接代理 sqlite3 连接，execute 返回行迭代器。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._c = conn

    def execute(self, sql: str, params: tuple = ()):  # type: ignore[override]
        return self._c.execute(sql, params).fetchall()

    def execute_one(self, sql: str, params: tuple = ()):  # type: ignore[override]
        return self._c.execute(sql, params).fetchone()


@pytest.fixture()
def db() -> _FakeConn:
    raw = sqlite3.connect(":memory:")
    raw.executescript(
        """
        CREATE TABLE market_data (
            symbol TEXT, trade_date TEXT, close REAL
        );
        INSERT INTO market_data VALUES ('000001','2024-01-02',10.0);
        INSERT INTO market_data VALUES ('000001','2024-01-03',10.5);
        CREATE TABLE financial_data (
            symbol TEXT, report_date TEXT, ann_date TEXT, pe REAL
        );
        INSERT INTO financial_data VALUES ('000001','2024-06-30','2024-08-28',20.0);
        CREATE TABLE factor_data (
            symbol TEXT, trade_date TEXT, factor_name TEXT, factor_value REAL
        );
        """
    )
    return _FakeConn(raw)


class TestDatasetVersionSnapshot:
    def test_financial_data_revision_is_3(self) -> None:
        """F-15 ann_date + M1A-01 每股收益时点日基收敛后，financial_data 必须登记为 rev=3。"""
        assert DATASET_SCHEMA_REVISIONS["financial_data"] == 3

    def test_snapshot_fields(self, db: _FakeConn) -> None:
        v = get_dataset_version(db, "financial_data")
        assert v.dataset == "financial_data"
        assert v.schema_revision == 3
        assert v.row_count == 1
        assert v.min_date == "2024-06-30"
        assert v.max_date == "2024-06-30"
        # columns_hash 非空短串
        assert len(v.columns_hash) == 12

    def test_market_data_row_count_and_range(self, db: _FakeConn) -> None:
        v = get_dataset_version(db, "market_data")
        assert v.row_count == 2
        assert v.min_date == "2024-01-02"
        assert v.max_date == "2024-01-03"

    def test_unknown_dataset_raises(self, db: _FakeConn) -> None:
        with pytest.raises(KeyError):
            get_dataset_version(db, "not_a_table")

    def test_all_versions(self, db: _FakeConn) -> None:
        versions = get_all_dataset_versions(db)
        assert set(versions.keys()) == set(DATASET_SCHEMA_REVISIONS)
        assert versions["financial_data"].schema_revision == 3


class TestSidecar:
    def test_write_and_load_roundtrip(self, db: _FakeConn, tmp_path: Path) -> None:
        versions = get_all_dataset_versions(db)
        result = tmp_path / "ic_report.csv"
        result.write_text("factor,ic\n", encoding="utf-8")
        sidecar = write_result_version_sidecar(result, versions, extra={"run_id": "r1"})
        assert sidecar.exists()
        loaded = load_result_version_sidecar(result)
        assert loaded is not None
        assert loaded["extra"]["run_id"] == "r1"
        assert loaded["datasets"]["financial_data"]["schema_revision"] == 3
        assert "code_git_short" in loaded

    def test_load_missing_sidecar_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "no_such.csv"
        assert load_result_version_sidecar(p) is None


class TestCompatibilityGuard:
    def test_same_revisions_pass(self) -> None:
        old = {
            "financial_data": DatasetVersion(
                dataset="financial_data", schema_revision=2, row_count=10,
                min_date="", max_date="", columns_hash="abcdef123456",
            )
        }
        new = {
            "financial_data": DatasetVersion(
                dataset="financial_data", schema_revision=2, row_count=20,
                min_date="", max_date="", columns_hash="abcdef123456",
            )
        }
        # 同 rev 不同 row_count 不抛错（数据更新口径未变）
        assert_versions_compatible(old, new)

    def test_revision_bump_raises(self) -> None:
        old = {
            "financial_data": DatasetVersion(
                dataset="financial_data", schema_revision=1, row_count=10,
                min_date="", max_date="", columns_hash="oldhash",
            )
        }
        new = {
            "financial_data": DatasetVersion(
                dataset="financial_data", schema_revision=2, row_count=10,
                min_date="", max_date="", columns_hash="newhash",
            )
        }
        with pytest.raises(DataVersionMismatchError, match="rev 1 → 2"):
            assert_versions_compatible(old, new)
