# 文件名：myquant/database/migrations/versions/0002_add_market_data_composite_index.py
"""
增量迁移：market_data 新增 (symbol, period, trade_date) 复合索引
============================================================
背景：
  实盘逐根推送与回测中，常见查询为「按标的+周期+日期范围」取K线：
    SELECT * FROM market_data WHERE symbol=? AND period=? AND trade_date BETWEEN ? AND ?
  原有索引 idx_market_symbol_date(symbol, trade_date) 不包含 period，
  多周期（daily/minute/tick）混合存储时无法充分利用索引。

变更：
  - 新增复合索引 idx_market_symbol_period_date(symbol, period, trade_date)
  - 该索引覆盖实盘最常用的查询模式，提升多周期场景查询性能

注意：
  - 不删除原有索引 idx_market_symbol_date，保持向后兼容
  - SQLite CREATE INDEX IF NOT EXISTS 幂等
  - downgrade 删除本迁移新增的索引

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# 本迁移新增的索引名
_NEW_INDEX_NAME = "idx_market_symbol_period_date"

# 建索引 SQL
_CREATE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {_NEW_INDEX_NAME} "
    f"ON market_data(symbol, period, trade_date);"
)

# 删索引 SQL
_DROP_INDEX_SQL = f"DROP INDEX IF EXISTS {_NEW_INDEX_NAME};"


def upgrade() -> None:
    """升级：新增 market_data 复合索引"""
    op.execute(_CREATE_INDEX_SQL)


def downgrade() -> None:
    """回滚：删除新增的复合索引"""
    op.execute(_DROP_INDEX_SQL)
