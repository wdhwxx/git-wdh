# 文件名：myquant/database/migrations/versions/0003_align_schema_divergence.py
"""
0003：补齐 Alembic 链与 schema.py 的结构性分叉（P0-07 修复）
========================================================================
背景：0001 自称"从 schema.py DDL 完整转换"与实测不符——Alembic 路径
缺 adjust_factor / share_change 两表、market_data 4 个派生列、
financial_data.ann_date 列、以及 5 个索引。凡走官方入口
`scripts/数据库迁移.py upgrade` 建库，后续 bulk_insert 直接
`no such column / no such table`。

本迁移幂等补齐（空库或已自建库均可重复执行）：
  1. 建 adjust_factor、share_change 两表（CREATE TABLE IF NOT EXISTS）
  2. market_data 补 amplitude/volume_ratio/float_share/pe_dynamic 四列
     （PRAGMA table_info 判定，幂等 ALTER）
  3. financial_data 补 ann_date 列（F-15 公告日，幂等 ALTER）
  4. 补 5 个索引（CREATE INDEX IF NOT EXISTS）：
       idx_fin_ann_date / idx_adj_factor_symbol / idx_adj_factor_symbol_date /
       idx_share_change_symbol / idx_share_change_symbol_date

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


# ─────────────────────────────────────────────
# 两表 DDL（与 schema.py:149/167 逐字一致）
# ─────────────────────────────────────────────

_CREATE_ADJUST_FACTOR = """
CREATE TABLE IF NOT EXISTS adjust_factor (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    event_date  TEXT    NOT NULL,
    factor      REAL    NOT NULL,
    data_source TEXT,
    created_at  TEXT    DEFAULT (datetime('now')),
    UNIQUE(symbol, event_date)
);
"""

_CREATE_SHARE_CHANGE = """
CREATE TABLE IF NOT EXISTS share_change (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,
    change_date   TEXT    NOT NULL,
    total_share   REAL,
    float_share   REAL,
    change_reason TEXT,
    data_source   TEXT,
    created_at    TEXT    DEFAULT (datetime('now')),
    UNIQUE(symbol, change_date)
);
"""

# market_data 补列（与 schema.py::_migrate_market_data_columns 一致）
_MARKET_DATA_NEW_COLUMNS = {
    "amplitude": "REAL",
    "volume_ratio": "REAL",
    "float_share": "REAL",
    "pe_dynamic": "REAL",
}

# 5 个缺失索引（与 schema.py 一致）
_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_fin_ann_date ON financial_data(ann_date);",
    "CREATE INDEX IF NOT EXISTS idx_adj_factor_symbol ON adjust_factor(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_adj_factor_symbol_date ON adjust_factor(symbol, event_date);",
    "CREATE INDEX IF NOT EXISTS idx_share_change_symbol ON share_change(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_share_change_symbol_date ON share_change(symbol, change_date);",
]


def _existing_columns(table: str) -> set:
    """PRAGMA table_info → 现有列名集合（幂等加列用）"""
    rows = op.get_bind().exec_driver_sql(f"PRAGMA table_info({table})")
    return {row[1] for row in rows}


def upgrade() -> None:
    """升级：补两表 + 5 列 + 5 索引，全部幂等"""
    # 1. 两表
    op.execute(_CREATE_ADJUST_FACTOR)
    op.execute(_CREATE_SHARE_CHANGE)
    # 2. market_data 4 列
    md_cols = _existing_columns("market_data")
    for col, col_type in _MARKET_DATA_NEW_COLUMNS.items():
        if col not in md_cols:
            op.execute(f"ALTER TABLE market_data ADD COLUMN {col} {col_type}")
    # 3. financial_data ann_date
    fin_cols = _existing_columns("financial_data")
    if "ann_date" not in fin_cols:
        op.execute("ALTER TABLE financial_data ADD COLUMN ann_date TEXT")
    # 4. 索引
    for sql in _CREATE_INDEXES:
        op.execute(sql)


def downgrade() -> None:
    """回滚：删 5 索引 + 两表（新增列 SQLite 无法 DROP COLUMN，
    残留空列无害；如需彻底回滚须走整表重建，不在本迁移范围）"""
    for sql in _CREATE_INDEXES:
        # 从 CREATE INDEX 语句提取索引名
        idx = sql.split()[5]
        op.execute(f"DROP INDEX IF EXISTS {idx};")
    op.execute("DROP TABLE IF EXISTS share_change;")
    op.execute("DROP TABLE IF EXISTS adjust_factor;")
