# 文件名：myquant/database/migrations/versions/0004_add_trade_ledger.py
"""
0004：新增订单/成交/持仓关系账三表（KP-P4-02）
================================================================
背景：schema.py TABLE_NAMES 仅 10 张行情/因子/状态表，无 order/fill/position
关系账。成交只存内存（broker._trades），无长期归档，审计无法按笔追查。
本迁移幂等新增三表（CREATE TABLE IF NOT EXISTS，与 schema.py DDL 逐字一致）：
  1. orders          — 逐笔订单关系账（order_id 唯一，长期归档）
  2. fills           — 逐笔成交关系账（trade_id 唯一，与 broker 逐笔一致）
  3. positions_daily — 持仓日快照账（snapshot_date+symbol+account 唯一，
                       删除当前快照不影响历史查询）

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


# ─────────────────────────────────────────────
# 三表 DDL（与 schema.py 逐字一致）
# ─────────────────────────────────────────────

_CREATE_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT    NOT NULL UNIQUE,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    order_type   TEXT,
    quantity     REAL,
    price        REAL,
    status       TEXT    NOT NULL,
    strategy_id  TEXT,
    engine_id    TEXT,
    submitted_at REAL,
    created_at   TEXT    DEFAULT (datetime('now'))
);
"""

_CREATE_FILLS = """
CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     TEXT    NOT NULL UNIQUE,
    order_id     TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    quantity     REAL,
    price        REAL,
    commission   REAL    DEFAULT 0,
    slippage     REAL    DEFAULT 0,
    trade_time   REAL,
    strategy_id  TEXT,
    engine_id    TEXT,
    created_at   TEXT    DEFAULT (datetime('now'))
);
"""

_CREATE_POSITIONS_DAILY = """
CREATE TABLE IF NOT EXISTS positions_daily (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date  TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    quantity       REAL,
    avg_cost       REAL,
    market_price   REAL,
    market_value   REAL,
    realized_pnl   REAL,
    unrealized_pnl REAL,
    cash           REAL,
    account_id     TEXT    DEFAULT 'default',
    created_at     TEXT    DEFAULT (datetime('now')),
    UNIQUE(snapshot_date, symbol, account_id)
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);",
    "CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id);",
    "CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);",
    "CREATE INDEX IF NOT EXISTS idx_fills_symbol ON fills(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_fills_trade_time ON fills(trade_time);",
    "CREATE INDEX IF NOT EXISTS idx_positions_date ON positions_daily(snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions_daily(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_positions_date_symbol ON positions_daily(snapshot_date, symbol);",
]


def upgrade() -> None:
    """升级：建三表 + 9 索引，全部幂等"""
    op.execute(_CREATE_ORDERS)
    op.execute(_CREATE_FILLS)
    op.execute(_CREATE_POSITIONS_DAILY)
    for sql in _CREATE_INDEXES:
        op.execute(sql)


def downgrade() -> None:
    """回滚：删 9 索引 + 三表"""
    for sql in reversed(_CREATE_INDEXES):
        idx = sql.split()[5]
        op.execute(f"DROP INDEX IF EXISTS {idx};")
    op.execute("DROP TABLE IF EXISTS positions_daily;")
    op.execute("DROP TABLE IF EXISTS fills;")
    op.execute("DROP TABLE IF EXISTS orders;")
