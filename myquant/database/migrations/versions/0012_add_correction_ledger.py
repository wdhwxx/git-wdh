# 文件名：myquant/database/migrations/versions/0012_add_correction_ledger.py
"""
0012：修正台账 + 门禁拒收记录两表（B-03，数据层施工）
====================================================
背景（《数据层结构优化可行性施工方案报告_v1.1》§5.2 B-03，目标结构 §4.3(b)）：
  - correction_ledger 修正台账（落地 v2.0 §7.2）：UPSERT/清洗修正时旧值留痕，
    支撑 C-02（废止 delete+insert → UPSERT + 留痕）与可审计回滚。
  - admission_reject 门禁拒收记录：门禁①（B-04）BLOCK 拒收可举证（否决可追溯）。
  CREATE TABLE IF NOT EXISTS 幂等；两表均可 DROP（可逆性）。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-17
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# 修正台账表（symbol/trade_date/field/old_value/new_value/corrected_at/corrected_by/reason）
_CREATE_CORRECTION_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS correction_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,            -- 标的代码
    trade_date   TEXT    NOT NULL,            -- 交易日 YYYY-MM-DD
    field        TEXT    NOT NULL,            -- 被修正字段名
    old_value    TEXT,                         -- 旧值（文本化，兼容数值/NULL）
    new_value    TEXT,                         -- 新值
    corrected_at TEXT    DEFAULT (datetime('now')),  -- 修正时间
    corrected_by TEXT    NOT NULL,            -- 修正者（模块/脚本/人）
    reason       TEXT                          -- 修正原因
);
"""

# 门禁①拒收记录表（源契约校验 BLOCK 落痕）
_CREATE_ADMISSION_REJECT_SQL = """
CREATE TABLE IF NOT EXISTS admission_reject (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,            -- 标的代码
    trade_date   TEXT,                         -- 交易日（可为空=非行情类拒收）
    source       TEXT,                         -- 数据源标识
    rule         TEXT    NOT NULL,            -- 触发规则名（如 high_low_conflict）
    level        TEXT    NOT NULL DEFAULT 'BLOCK',  -- 拒收级别（BLOCK）
    detail       TEXT,                         -- 拒收详情（字段/值）
    rejected_at  TEXT    DEFAULT (datetime('now'))   -- 拒收时间
);
"""

_DROP_CORRECTION_LEDGER_SQL = "DROP TABLE IF EXISTS correction_ledger;"
_DROP_ADMISSION_REJECT_SQL = "DROP TABLE IF EXISTS admission_reject;"


def _table_exists(conn, table: str) -> bool:
    """检查表是否存在。"""
    rows = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return rows.first() is not None


def _apply_upgrade(conn) -> None:
    """建两表（幂等，独立纯逻辑供单测注入连接直调）。"""
    if not _table_exists(conn, "correction_ledger"):
        conn.exec_driver_sql(_CREATE_CORRECTION_LEDGER_SQL)
    if not _table_exists(conn, "admission_reject"):
        conn.exec_driver_sql(_CREATE_ADMISSION_REJECT_SQL)


def _apply_downgrade(conn) -> None:
    """删两表。"""
    conn.exec_driver_sql(_DROP_CORRECTION_LEDGER_SQL)
    conn.exec_driver_sql(_DROP_ADMISSION_REJECT_SQL)


def upgrade() -> None:
    """升级：新增 correction_ledger + admission_reject 两表。"""
    _apply_upgrade(op.get_bind())


def downgrade() -> None:
    """回滚：删除两表。"""
    _apply_downgrade(op.get_bind())
