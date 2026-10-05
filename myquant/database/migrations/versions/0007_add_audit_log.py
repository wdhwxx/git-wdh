"""0007_add_audit_log.py - 审计只追加表（BASE-7）

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05

审计只追加与归档校验（BASE-7，施工清单 §3.2）：
  - audit_log 主表（kind/action/actor/detail_json/seq/prev_hash/row_hash/
    created_ts，seq+row_hash 双 UNIQUE）——降级哈希链（个人系统版，非完整
    哈希链）：row_hash = SHA-256(kind|action|actor|detail_json|created_ts|seq)，
    prev_hash = 上一条 row_hash（首条 64 个 '0'）；无 UPDATE/DELETE 业务 API，
    常规改写即断链可检出
  - audit_archive 归档表（保留原 seq/prev_hash/row_hash 不改写）
  - audit_archive_manifest 归档清单（row_count/first_seq/last_seq/checksum=
    归档行 row_hash 按序聚合 SHA-256）——verify_archive 重算比对

DDL 与 myquant/database/schema.py 逐字一致（幂等 CREATE IF NOT EXISTS）。
"""
from __future__ import annotations

from alembic import op

from myquant.database.schema import (
    CREATE_AUDIT_ARCHIVE_MANIFEST_SQL,
    CREATE_AUDIT_ARCHIVE_SQL,
    CREATE_AUDIT_LOG_INDEX_SQL,
    CREATE_AUDIT_LOG_SQL,
)

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建审计三表（幂等）。"""
    op.execute(CREATE_AUDIT_LOG_SQL)
    for sql in CREATE_AUDIT_LOG_INDEX_SQL:
        op.execute(sql)
    op.execute(CREATE_AUDIT_ARCHIVE_SQL)
    op.execute(CREATE_AUDIT_ARCHIVE_MANIFEST_SQL)


def downgrade() -> None:
    """回滚：删除审计三表（含索引随表删除）。"""
    op.execute("DROP TABLE IF EXISTS audit_archive_manifest")
    op.execute("DROP TABLE IF EXISTS audit_archive")
    op.execute("DROP TABLE IF EXISTS audit_log")
