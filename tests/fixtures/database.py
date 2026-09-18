# 文件名：tests/fixtures/database.py
"""
database.py - 数据库测试 Fixture
====================================
提供临时数据库、初始化schema、查询接口、事务清理等 fixture，
供数据库模块、数据清洗入库、数据集版本管理等测试使用。

覆盖场景:
  - 临时 SQLite 数据库（自动创建+清理）
  - 已初始化全部表结构的数据库
  - CleanDataQueries 查询接口实例
  - 含测试数据的数据库（clean_run + dirty_stats）
  - 数据库连接上下文（自动关闭）

设计原则:
  1. 全部使用临时文件，测试结束自动删除，不污染项目
  2. 为生产数据库对接预留：fixture 接口与生产 DatabaseConnection 一致
  3. 严格禁用未来函数，全部使用历史日期
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Generator

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.database.queries import CleanDataQueries
from myquant.database.schema import create_all_tables


# ═══════════════════════════════════════════
#  临时数据库 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def temp_db_path() -> Generator[str, None, None]:
    """临时数据库文件路径（测试结束自动删除）"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # 清理
    p = Path(db_path)
    if p.exists():
        p.unlink(missing_ok=True)


@pytest.fixture
def db_connection(temp_db_path: str) -> Generator[DatabaseConnection, None, None]:
    """数据库连接实例（测试结束自动关闭）"""
    conn = DatabaseConnection(db_path=temp_db_path, pool_size=1)
    yield conn
    conn.close()


@pytest.fixture
def initialized_db(db_connection: DatabaseConnection) -> DatabaseConnection:
    """已初始化全部表结构的数据库连接"""
    create_all_tables(db_connection)
    return db_connection


@pytest.fixture
def clean_data_queries(initialized_db: DatabaseConnection) -> CleanDataQueries:
    """CleanDataQueries 查询接口实例（已建表）"""
    return CleanDataQueries(connection=initialized_db)


# ═══════════════════════════════════════════
#  含测试数据的数据库 Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def db_with_clean_run(clean_data_queries: CleanDataQueries) -> CleanDataQueries:
    """含一条清洗运行记录的数据库"""
    clean_data_queries.insert_clean_run(
        run_id="test_run_001",
        data_type="bar",
        symbol="000001.SZ",
        status="completed",
        total=1000,
        valid=980,
        bad=20,
        valid_rate=0.98,
        quality_grade="good",
        severity="warning",
        source="offline",
        started_at="2024-01-15 09:30:00",
        finished_at="2024-01-15 09:30:05",
        total_duration=5.0,
    )
    return clean_data_queries


@pytest.fixture
def db_with_dirty_stats(db_with_clean_run: CleanDataQueries) -> CleanDataQueries:
    """含清洗运行+脏数据统计的数据库"""
    db_with_clean_run.insert_dirty_stats(
        run_id="test_run_001",
        symbol="000001.SZ",
        data_type="bar",
        dirty_type="negative_price",
        severity="error",
        bad_count=10,
        repair_strategy="drop",
    )
    db_with_clean_run.insert_dirty_stats(
        run_id="test_run_001",
        symbol="000001.SZ",
        data_type="bar",
        dirty_type="ohlc_error",
        severity="warning",
        bad_count=10,
        repair_strategy="drop",
    )
    return db_with_clean_run


@pytest.fixture
def db_with_multiple_runs(clean_data_queries: CleanDataQueries) -> CleanDataQueries:
    """含多条清洗运行记录的数据库（用于列表/分页测试）"""
    for i in range(5):
        clean_data_queries.insert_clean_run(
            run_id=f"test_run_{i+1:03d}",
            data_type="bar",
            symbol="000001.SZ",
            status="completed",
            total=1000 + i * 100,
            valid=980 + i * 10,
            bad=20 - i,
            valid_rate=0.98 + i * 0.002,
            quality_grade="good",
            severity="info",
            source="offline",
            started_at=f"2024-01-{15+i:02d} 09:30:00",
            finished_at=f"2024-01-{15+i:02d} 09:30:05",
            total_duration=5.0,
        )
    return clean_data_queries


# ═══════════════════════════════════════════
#  完整数据库环境 Fixture
# ═══════════════════════════════════════════

@pytest.fixture
def database_environment(initialized_db: DatabaseConnection,
                          clean_data_queries: CleanDataQueries) -> Dict[str, Any]:
    """
    完整数据库环境（connection + queries）
    返回字典，测试中可直接解包使用。
    """
    return {
        "connection": initialized_db,
        "queries": clean_data_queries,
    }
