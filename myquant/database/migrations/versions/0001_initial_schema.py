# 文件名：myquant/database/migrations/versions/0001_initial_schema.py
"""
初始建表：8张业务表 + 全部索引
============================================
从 myquant/database/schema.py 的 DDL 完整转换，确保与现有建表逻辑完全一致。

包含表：
  1. market_data        行情数据表（日线/分钟线/tick，多市场）
  2. financial_data     财务数据表（PE/PB/市值/财报指标）
  3. factor_data        因子数据表（技术因子/基本面因子）
  4. strategy_state     策略状态表（持仓/订单/回测状态持久化）
  5. data_source        数据源注册表（多源管理）
  6. sync_status        数据同步状态表（每只股票最后更新时间）
  7. clean_run_meta     清洗运行元信息表（任务13新增）
  8. dirty_data_stats   脏数据统计表（任务13新增，外键关联 clean_run_meta）

Revision ID: 0001
Revises: None（首个版本）
Create Date: 2026-08-29

设计原则：
  - 使用 op.execute() 执行原始SQL，与项目 raw sqlite3 风格一致
  - 建表顺序：被依赖表先建；删表顺序相反
  - 所有 CREATE TABLE IF NOT EXISTS 幂等，兼容已有数据库
  - 为实盘无缝对接预留：多市场字段(market)、多周期字段(period)、实盘来源字段(source)
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# ─────────────────────────────────────────────
# 建表 SQL（与 schema.py 完全一致）
# ─────────────────────────────────────────────

_CREATE_TABLES = [
    # 1. 行情数据表
    """
    CREATE TABLE IF NOT EXISTS market_data (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT    NOT NULL,
        market      TEXT    NOT NULL DEFAULT 'A股',
        period      TEXT    NOT NULL DEFAULT 'daily',
        adjust      TEXT,
        trade_date  TEXT    NOT NULL,
        trade_time  TEXT,
        open        REAL,
        high        REAL,
        low         REAL,
        close       REAL,
        volume      REAL,
        amount      REAL,
        pct_chg     REAL,
        turnover    REAL,
        is_suspended INTEGER DEFAULT 0,
        data_source TEXT,
        created_at  TEXT    DEFAULT (datetime('now')),
        UNIQUE(symbol, period, adjust, trade_date, trade_time)
    );
    """,
    # 2. 财务数据表
    """
    CREATE TABLE IF NOT EXISTS financial_data (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT    NOT NULL,
        market          TEXT    NOT NULL DEFAULT 'A股',
        report_date     TEXT    NOT NULL,
        report_type     TEXT,
        pe              REAL,
        pb              REAL,
        ps              REAL,
        total_mv        REAL,
        circ_mv         REAL,
        revenue         REAL,
        net_profit      REAL,
        net_profit_yoy  REAL,
        revenue_yoy     REAL,
        roe             REAL,
        gross_margin    REAL,
        debt_ratio      REAL,
        eps             REAL,
        bps             REAL,
        data_source     TEXT,
        created_at      TEXT    DEFAULT (datetime('now')),
        UNIQUE(symbol, report_date)
    );
    """,
    # 3. 因子数据表
    """
    CREATE TABLE IF NOT EXISTS factor_data (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT    NOT NULL,
        market      TEXT    NOT NULL DEFAULT 'A股',
        trade_date  TEXT    NOT NULL,
        factor_name TEXT    NOT NULL,
        factor_value REAL,
        factor_rank REAL,
        calc_method TEXT,
        data_source TEXT,
        created_at  TEXT    DEFAULT (datetime('now')),
        UNIQUE(symbol, trade_date, factor_name)
    );
    """,
    # 4. 策略状态表
    """
    CREATE TABLE IF NOT EXISTS strategy_state (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_name   TEXT    NOT NULL,
        run_id          TEXT    NOT NULL,
        state_key       TEXT    NOT NULL,
        state_value     TEXT,
        value_type      TEXT,
        updated_at      TEXT    DEFAULT (datetime('now')),
        UNIQUE(strategy_name, run_id, state_key)
    );
    """,
    # 5. 数据源注册表
    """
    CREATE TABLE IF NOT EXISTS data_source (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name     TEXT    NOT NULL UNIQUE,
        source_type     TEXT    NOT NULL,
        priority        INTEGER DEFAULT 100,
        is_enabled      INTEGER DEFAULT 1,
        health_status   TEXT    DEFAULT 'unknown',
        last_check_time TEXT,
        fail_count      INTEGER DEFAULT 0,
        config_json     TEXT,
        description     TEXT,
        updated_at      TEXT    DEFAULT (datetime('now'))
    );
    """,
    # 6. 数据同步状态表
    """
    CREATE TABLE IF NOT EXISTS sync_status (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT    NOT NULL,
        market          TEXT    NOT NULL DEFAULT 'A股',
        period          TEXT    NOT NULL DEFAULT 'daily',
        data_type       TEXT    NOT NULL DEFAULT 'market',
        last_sync_date  TEXT,
        last_sync_time  TEXT,
        record_count    INTEGER DEFAULT 0,
        sync_status     TEXT    DEFAULT 'pending',
        error_message   TEXT,
        data_source     TEXT,
        updated_at      TEXT    DEFAULT (datetime('now')),
        UNIQUE(symbol, period, data_type)
    );
    """,
    # 7. 清洗运行元信息表
    """
    CREATE TABLE IF NOT EXISTS clean_run_meta (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT    NOT NULL UNIQUE,
        data_type       TEXT    NOT NULL,
        symbol          TEXT    NOT NULL,
        status          TEXT    NOT NULL,
        total           INTEGER DEFAULT 0,
        valid           INTEGER DEFAULT 0,
        bad             INTEGER DEFAULT 0,
        valid_rate      REAL,
        quality_grade   TEXT,
        severity        TEXT,
        steps_executed  TEXT,
        step_durations  TEXT,
        total_duration  REAL,
        bad_file        TEXT,
        report_file     TEXT,
        processed_file  TEXT,
        source          TEXT    DEFAULT 'offline',
        started_at      TEXT,
        finished_at     TEXT,
        error           TEXT,
        created_at      TEXT    DEFAULT (datetime('now'))
    );
    """,
    # 8. 脏数据统计表（外键关联 clean_run_meta.run_id）
    """
    CREATE TABLE IF NOT EXISTS dirty_data_stats (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT    NOT NULL,
        symbol          TEXT    NOT NULL,
        data_type       TEXT    NOT NULL,
        dirty_type      TEXT    NOT NULL,
        severity        TEXT,
        bad_count       INTEGER DEFAULT 0,
        repair_strategy TEXT,
        sample_data     TEXT,
        created_at      TEXT    DEFAULT (datetime('now')),
        UNIQUE(run_id, symbol, data_type, dirty_type)
    );
    """,
]

# ─────────────────────────────────────────────
# 建索引 SQL（与 schema.py 完全一致）
# ─────────────────────────────────────────────

_CREATE_INDEXES = [
    # market_data 索引
    "CREATE INDEX IF NOT EXISTS idx_market_symbol ON market_data(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_market_date ON market_data(trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_market_symbol_date ON market_data(symbol, trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_market_market_period ON market_data(market, period);",
    # financial_data 索引
    "CREATE INDEX IF NOT EXISTS idx_fin_symbol ON financial_data(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_fin_report_date ON financial_data(report_date);",
    # factor_data 索引
    "CREATE INDEX IF NOT EXISTS idx_factor_symbol_date ON factor_data(symbol, trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_factor_name_date ON factor_data(factor_name, trade_date);",
    # strategy_state 索引
    "CREATE INDEX IF NOT EXISTS idx_state_strategy ON strategy_state(strategy_name);",
    "CREATE INDEX IF NOT EXISTS idx_state_run_id ON strategy_state(run_id);",
    # sync_status 索引
    "CREATE INDEX IF NOT EXISTS idx_sync_status ON sync_status(sync_status);",
    "CREATE INDEX IF NOT EXISTS idx_sync_symbol ON sync_status(symbol);",
    # clean_run_meta 索引
    "CREATE INDEX IF NOT EXISTS idx_clean_run_symbol ON clean_run_meta(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_data_type ON clean_run_meta(data_type);",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_status ON clean_run_meta(status);",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_started_at ON clean_run_meta(started_at);",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_symbol_type ON clean_run_meta(symbol, data_type);",
    # dirty_data_stats 索引
    "CREATE INDEX IF NOT EXISTS idx_dirty_run ON dirty_data_stats(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_dirty_symbol ON dirty_data_stats(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_dirty_type ON dirty_data_stats(dirty_type);",
    "CREATE INDEX IF NOT EXISTS idx_dirty_severity ON dirty_data_stats(severity);",
]

# ─────────────────────────────────────────────
# 删表顺序（与建表相反，先删子表）
# ─────────────────────────────────────────────

_DROP_TABLES = [
    "dirty_data_stats",   # 先删子表（关联 clean_run_meta）
    "clean_run_meta",
    "sync_status",
    "data_source",
    "strategy_state",
    "factor_data",
    "financial_data",
    "market_data",
]


def upgrade() -> None:
    """升级：创建所有8张表 + 全部索引"""
    # 建表
    for sql in _CREATE_TABLES:
        op.execute(sql)
    # 建索引
    for sql in _CREATE_INDEXES:
        op.execute(sql)


def downgrade() -> None:
    """回滚：删除所有索引 + 8张表（按依赖逆序）"""
    # 删索引（SQLite 删表会自动删索引，但显式删除更清晰）
    for sql in reversed(_CREATE_INDEXES):
        # 从 "CREATE INDEX IF NOT EXISTS idx_name ON table(col);" 提取索引名
        import re
        m = re.search(r"CREATE INDEX IF NOT EXISTS (\w+)", sql)
        if m:
            op.execute(f"DROP INDEX IF EXISTS {m.group(1)};")
    # 删表（逆序）
    for table in _DROP_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table};")
