# 文件名：myquant/database/migrations/versions/0006_add_metrics_history.py
"""
0006：新增指标时序存储表 metrics_history（BASE-2 指标时序存储）
================================================================
背景：监控指标只存内存（MetricsCollector 每指标每标签组合最多 1000 个
点，进程重启即丢），权益曲线/滚动绩效/动态基线（§5.3）无法回放历史。
本迁移幂等新增 metrics_history 表（CREATE TABLE IF NOT EXISTS，与
schema.py DDL 逐字一致，供 create_all_tables 与迁移双路一致建表）：

  1. metrics_history — 指标时序表（name/labels_json/value/timestamp，
     name+labels_json+timestamp 唯一防重复落库）

字段与施工清单 BASE-2 对齐：
  name/labels_json/value/timestamp + 按天分区索引（日期表达式索引）
  + name+timestamp 复合索引

约束（R-02）：本表仅服务趋势/基线/看板回放，不得作为任何 P0 实时告警
的检测来源；P0 实时告警一律走事件订阅。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-05
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


# ─────────────────────────────────────────────
# metrics_history 表 DDL（与 schema.py 逐字一致）
# ─────────────────────────────────────────────

_CREATE_METRICS_HISTORY = """
CREATE TABLE IF NOT EXISTS metrics_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    labels_json TEXT    NOT NULL DEFAULT '{}',
    value       REAL    NOT NULL,
    timestamp   REAL    NOT NULL,
    UNIQUE (name, labels_json, timestamp)
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_metrics_history_name_ts ON metrics_history(name, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_metrics_history_day ON metrics_history(date(timestamp, 'unixepoch'));",
    "CREATE INDEX IF NOT EXISTS idx_metrics_history_ts ON metrics_history(timestamp);",
]


def upgrade() -> None:
    """升级：建 metrics_history 表 + 3 索引，全部幂等"""
    op.execute(_CREATE_METRICS_HISTORY)
    for sql in _CREATE_INDEXES:
        op.execute(sql)


def downgrade() -> None:
    """回滚：删 3 索引 + metrics_history 表"""
    for sql in reversed(_CREATE_INDEXES):
        idx = sql.split()[5]
        op.execute(f"DROP INDEX IF EXISTS {idx};")
    op.execute("DROP TABLE IF EXISTS metrics_history;")
