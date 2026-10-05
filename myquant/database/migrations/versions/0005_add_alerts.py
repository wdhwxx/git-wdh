# 文件名：myquant/database/migrations/versions/0005_add_alerts.py
"""
0005：新增告警持久化表 alerts（BASE-1 告警落库）
================================================================
背景：告警只存内存（AlertManager._history deque，maxlen=1000），
重启后历史丢失，/api/alerts 无法回查，P0-8 误报统计无数据源。
本迁移幂等新增 alerts 表（CREATE TABLE IF NOT EXISTS，与 schema.py DDL
逐字一致，供 create_all_tables 与迁移双路一致建表）：

  1. alerts — 告警持久化表（alert_id 唯一；status=open/acked/resolved；
              classification=TP/FP/auto 供 P0-8 统计脚本读取；R-01）

字段与施工清单 BASE-1 对齐：
  id/level/category/source/message/details_json/dedup_key/status/
  classification/created_at/acked_at/resolved_at/escalated/runbook_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-05
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


# ─────────────────────────────────────────────
# alerts 表 DDL（与 schema.py 逐字一致）
# ─────────────────────────────────────────────

_CREATE_ALERTS = """
CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id       TEXT    NOT NULL UNIQUE,
    level          TEXT    NOT NULL,
    category       TEXT    NOT NULL DEFAULT 'system',
    source         TEXT    NOT NULL DEFAULT 'monitoring',
    message        TEXT    NOT NULL,
    details_json   TEXT,
    dedup_key      TEXT,
    status         TEXT    NOT NULL DEFAULT 'open',
    classification TEXT    DEFAULT '',
    created_at     REAL    NOT NULL,
    acked_at       REAL,
    resolved_at    REAL,
    escalated      INTEGER NOT NULL DEFAULT 0,
    runbook_id     TEXT
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_alerts_dedup_key ON alerts(dedup_key);",
    "CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);",
    "CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);",
]


def upgrade() -> None:
    """升级：建 alerts 表 + 3 索引，全部幂等"""
    op.execute(_CREATE_ALERTS)
    for sql in _CREATE_INDEXES:
        op.execute(sql)


def downgrade() -> None:
    """回滚：删 3 索引 + alerts 表"""
    for sql in reversed(_CREATE_INDEXES):
        idx = sql.split()[5]
        op.execute(f"DROP INDEX IF EXISTS {idx};")
    op.execute("DROP TABLE IF EXISTS alerts;")
