# 文件名：myquant/database/migrations/versions/0009_reconciliation_ledger.py
"""
0009：对账账本落地（T1-3，真钱硬前置 RC-07）
================================================================
背景：0004 只建了 orders/fills/positions_daily 三表骨架，无对账相关字段/表。
本迁移幂等扩展：
  1. orders 增列：client_order_id / broker_order_id / broker_id / account_id /
     filled_qty / filled_price / last_update_at / raw_payload
  2. fills 增列：broker_trade_id(唯一) / stamp_tax / transfer_fee / account_id
  3. 新增 order_events（append-only, order_id+seq 唯一）
  4. 新增 reconciliation_runs / reconciliation_diffs / reconciliation_freeze

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


# ─────────────────────────────────────────────
# 1. orders 增列（SQLite 支持 ADD COLUMN，幂等靠检查 PRAGMA）
# ─────────────────────────────────────────────

_ORDERS_ADD_COLUMNS = [
    ("client_order_id", "TEXT"),
    ("broker_order_id", "TEXT"),
    ("broker_id", "TEXT"),
    ("account_id", "TEXT DEFAULT 'default'"),
    ("filled_qty", "REAL DEFAULT 0"),
    ("filled_price", "REAL DEFAULT 0"),
    ("last_update_at", "REAL"),
    ("raw_payload", "TEXT"),
]

# ─────────────────────────────────────────────
# 2. fills 增列
# ─────────────────────────────────────────────

_FILLS_ADD_COLUMNS = [
    ("broker_trade_id", "TEXT"),
    ("stamp_tax", "REAL DEFAULT 0"),
    ("transfer_fee", "REAL DEFAULT 0"),
    ("account_id", "TEXT DEFAULT 'default'"),
]

# ─────────────────────────────────────────────
# 3. 新增三表 DDL
# ─────────────────────────────────────────────

_CREATE_ORDER_EVENTS = """
CREATE TABLE IF NOT EXISTS order_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    event_type   TEXT    NOT NULL,
    payload      TEXT,
    created_at   TEXT    DEFAULT (datetime('now')),
    UNIQUE(order_id, seq)
);
"""

_CREATE_RECONCILIATION_RUNS = """
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT    NOT NULL,
    run_type     TEXT    NOT NULL,
    started_at   REAL    NOT NULL,
    finished_at  REAL,
    consistent   INTEGER,
    frozen       INTEGER DEFAULT 0,
    note         TEXT,
    created_at   TEXT    DEFAULT (datetime('now'))
);
"""

_CREATE_RECONCILIATION_DIFFS = """
CREATE TABLE IF NOT EXISTS reconciliation_diffs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL,
    account_id       TEXT    NOT NULL,
    cash_diff        REAL,
    position_diff    TEXT,
    exceeds_threshold INTEGER DEFAULT 0,
    created_at       TEXT    DEFAULT (datetime('now'))
);
"""

_CREATE_RECONCILIATION_FREEZE = """
CREATE TABLE IF NOT EXISTS reconciliation_freeze (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    reason       TEXT,
    created_at   TEXT    DEFAULT (datetime('now'))
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_order_events_order_id ON order_events(order_id);",
    "CREATE INDEX IF NOT EXISTS idx_recon_runs_account ON reconciliation_runs(account_id);",
    "CREATE INDEX IF NOT EXISTS idx_recon_diffs_run ON reconciliation_diffs(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_recon_freeze_account ON reconciliation_freeze(account_id);",
]


def _column_exists(table: str, column: str) -> bool:
    """检查列是否已存在（SQLite PRAGMA table_info）。"""
    import sqlalchemy as sa
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table)]
    return column in columns


def upgrade() -> None:
    """升级：orders/fills 增列 + 新增三表 + 4 索引，全部幂等。"""
    # 1. orders 增列
    for col_name, col_type in _ORDERS_ADD_COLUMNS:
        if not _column_exists("orders", col_name):
            op.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type};")

    # 2. fills 增列
    for col_name, col_type in _FILLS_ADD_COLUMNS:
        if not _column_exists("fills", col_name):
            op.execute(f"ALTER TABLE fills ADD COLUMN {col_name} {col_type};")

    # 3. 新增三表
    op.execute(_CREATE_ORDER_EVENTS)
    op.execute(_CREATE_RECONCILIATION_RUNS)
    op.execute(_CREATE_RECONCILIATION_DIFFS)
    op.execute(_CREATE_RECONCILIATION_FREEZE)

    # 4. 索引
    for sql in _CREATE_INDEXES:
        op.execute(sql)


def downgrade() -> None:
    """回滚：删 4 索引 + 三表（orders/fills 增列 SQLite 不支持 DROP COLUMN，保留）。"""
    for sql in reversed(_CREATE_INDEXES):
        idx = sql.split()[5]
        op.execute(f"DROP INDEX IF EXISTS {idx};")
    op.execute("DROP TABLE IF EXISTS reconciliation_freeze;")
    op.execute("DROP TABLE IF EXISTS reconciliation_diffs;")
    op.execute("DROP TABLE IF EXISTS reconciliation_runs;")
    op.execute("DROP TABLE IF EXISTS order_events;")
