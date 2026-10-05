"""0008_add_value_encrypted.py - metrics_history 敏感字段加密列（BASE-9）

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05

监控数据加密与访问审计（BASE-9，施工清单 R-06）：
  - metrics_history 新增 value_encrypted TEXT 列（敏感指标密文落盘，
    命中 FieldCrypto.is_sensitive_name 时 value 列存占位 0.0 + 密文
    存本列；查询时解密回读）
  - 新库：CREATE TABLE IF NOT EXISTS 含新列（与 schema.py 逐字一致）
  - 旧库：ALTER TABLE ADD COLUMN（幂等，SQLite 不支持 IF NOT EXISTS
    加列 → 先查 PRAGMA table_info 再决定）

与 schema.py 逐字一致（幂等 CREATE IF NOT EXISTS 建新表路径）。
"""

from alembic import op
from sqlalchemy import text

from myquant.database.schema import CREATE_METRICS_HISTORY_SQL

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建新表（含新列）或对旧表幂等补列。"""
    op.execute(CREATE_METRICS_HISTORY_SQL)
    # 旧表补列（幂等：SQLite ALTER 无 IF NOT EXISTS，先探测列）
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(metrics_history)"))}
    if "value_encrypted" not in cols:
        op.execute(
            "ALTER TABLE metrics_history ADD COLUMN value_encrypted TEXT DEFAULT NULL"
        )


def downgrade() -> None:
    """回滚：删除加密列（SQLite 3.35+ 支持 DROP COLUMN，失败则提示重建表）。"""
    conn = op.get_bind()
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(metrics_history)"))}
    if "value_encrypted" in cols:
        try:
            op.execute("ALTER TABLE metrics_history DROP COLUMN value_encrypted")
        except Exception:  # noqa: BLE001（旧版 SQLite 不支持 DROP COLUMN）
            # 不静默：明确提示降级路径（数据量小可整表重建）
            raise RuntimeError(
                "当前 SQLite 不支持 DROP COLUMN；如需回滚请整表重建 metrics_history"
            )
