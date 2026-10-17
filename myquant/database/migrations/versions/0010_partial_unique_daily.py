# 文件名：myquant/database/migrations/versions/0010_partial_unique_daily.py
"""
0010：market_data 部分唯一索引（B-01，数据层施工）
====================================================
背景（《数据层结构优化可行性施工方案报告_v1.1》§5.2 B-01，前置 A-13）：
  market_data 的日线行（trade_time IS NULL）以 (symbol, period, adjust,
  trade_date) 为自然唯一键，但历史上缺少唯一约束，已发生 4,377 个重复键
  （A-01 实测定位、A-13 已清理归零）。为防止重复再次发生，建部分唯一索引：
    CREATE UNIQUE INDEX ... ON market_data(symbol, period, adjust, trade_date)
    WHERE trade_time IS NULL;
  部分索引只约束日线（trade_time NULL）行；分钟/秒级行（trade_time 非空）
  仍可按 (trade_time) 区分多根，不受影响。

变更：
  - 新增部分唯一索引 uq_market_data_daily_key
  - CREATE UNIQUE INDEX IF NOT EXISTS 幂等；若库中仍存在重复键则建索引会失败
    （A-13 已确保清理后重复键=0，可安全执行）

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-17
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


# 本迁移新增的部分唯一索引名
_NEW_INDEX_NAME = "uq_market_data_daily_key"

# 建部分唯一索引 SQL（WHERE trade_time IS NULL 只约束日线行）
_CREATE_UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {_NEW_INDEX_NAME} "
    f"ON market_data(symbol, period, adjust, trade_date) "
    f"WHERE trade_time IS NULL;"
)

# 删索引 SQL（downgrade）
_DROP_INDEX_SQL = f"DROP INDEX IF EXISTS {_NEW_INDEX_NAME};"


def upgrade() -> None:
    """升级：新增 market_data 日线部分唯一索引。"""
    op.execute(_CREATE_UNIQUE_INDEX_SQL)


def downgrade() -> None:
    """回滚：删除本迁移新增的部分唯一索引。"""
    op.execute(_DROP_INDEX_SQL)
