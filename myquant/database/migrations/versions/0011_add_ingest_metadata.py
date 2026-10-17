# 文件名：myquant/database/migrations/versions/0011_add_ingest_metadata.py
"""
0011：market_data 采集元数据三列（B-02，数据层施工）
====================================================
背景（《数据层结构优化可行性施工方案报告_v1.1》§5.2 B-02，目标结构 §4.2(c)）：
  为 ODS 层补"三时间戳采集元数据"，解决：
    - P0-1 门禁标记丢失（anomaly_flags 列预留，B-04 门禁① FLAG 落列）
    - P0-6 溯源断裂（ingest_time / ingest_batch 记录采集时刻与批次）
    - P1-1 无 system_time（回测延迟模拟 D-01 的数据基础）
  生产库 quant.db 约 6GB、16.9M 行，不做物理重构（设计总则第 1 条）：
  用 SQLite 逐列 ALTER TABLE ADD COLUMN（不支持 ADD COLUMN IF NOT EXISTS，
  用 sqlite_master 检查列存在性保证幂等）。

变更：
  - market_data 新增三列：
      ingest_time   TEXT  -- 采集入库时间（ISO 8601）
      ingest_batch  TEXT  -- 采集批次号（YYYYMMDD-序号）
      anomaly_flags TEXT  -- 异常标记（门禁① FLAG 落列，B-04 后写入）
  - 存量行三列保持 NULL（尊重新账与旧账：不追溯重写历史行）；
    新写入行由 manager._save_to_database 填充（验收：非空率 100%）。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# 新增列定义：(列名, DDL 片段)
_NEW_COLUMNS = (
    ("ingest_time", "TEXT"),
    ("ingest_batch", "TEXT"),
    ("anomaly_flags", "TEXT"),
)


def _column_exists(conn, column: str) -> bool:
    """检查 market_data 是否已存在指定列（SQLite 无 ADD COLUMN IF NOT EXISTS）。"""
    rows = conn.exec_driver_sql(
        "SELECT name FROM pragma_table_info('market_data') WHERE name = ?",
        (column,),
    )
    return rows.first() is not None


def _apply_upgrade(conn) -> None:
    """逐列幂等 ADD（独立纯逻辑，供 upgrade 与单测直接注入连接调用）。"""
    for col_name, col_type in _NEW_COLUMNS:
        if not _column_exists(conn, col_name):
            conn.exec_driver_sql(
                f"ALTER TABLE market_data ADD COLUMN {col_name} {col_type}"
            )


def _apply_downgrade(conn) -> None:
    """逐列 DROP（SQLite 3.35+ 支持 ALTER TABLE DROP COLUMN）。"""
    for col_name, _col_type in _NEW_COLUMNS:
        if _column_exists(conn, col_name):
            conn.exec_driver_sql(
                f"ALTER TABLE market_data DROP COLUMN {col_name}"
            )


def upgrade() -> None:
    """升级：market_data 新增采集元数据三列（逐列幂等 ADD）。"""
    _apply_upgrade(op.get_bind())


def downgrade() -> None:
    """回滚：逐列 DROP（SQLite 支持 ALTER TABLE DROP COLUMN，3.35+）。"""
    _apply_downgrade(op.get_bind())
