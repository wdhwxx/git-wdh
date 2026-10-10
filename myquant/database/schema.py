# 文件名：myquant/database/schema.py
"""
schema.py - 数据库表结构定义
============================
功能清单:
  1. 行情数据表（日线/分钟线/tick，多市场）
  2. 财务数据表（PE/PB/市值/财报指标）
  3. 因子数据表（技术因子/基本面因子/自定义因子）
  4. 策略状态表（持仓/订单/回测状态持久化）
  5. 数据源表（数据源注册/优先级/健康状态）
  6. 数据同步状态表（每只股票最后更新时间）
  7. 建表/删表/表存在检查
  8. 复权因子表（adjust_factor：除权除息事件后复权因子，方案B #82）

设计原则:
  - 所有表带 market 字段（A股/港股/美股），为多市场扩展预留
  - 所有表带 period 字段（daily/minute/tick），为多周期预留
  - 行情表字段与交接文档5.2节约定一致
  - 行情表 adjust 字段：当前库内为不复权原始层(raw)，预留复权(hfq/qfq)扩展
  - 所有时间戳用UTC存储，展示时转上海时区
"""

from __future__ import annotations

from typing import Dict, List, Tuple


# ─────────────────────────────────────────────
# 表名常量
# ─────────────────────────────────────────────

TABLE_NAMES = {
    "market_data": "market_data",           # 行情数据表
    "financial_data": "financial_data",     # 财务数据表
    "factor_data": "factor_data",           # 因子数据表
    "adjust_factor": "adjust_factor",       # 复权因子表（除权除息事件，方案B #82）
    "share_change": "share_change",         # 股本变动表（真实总/流通股本事件，P1-② #83）
    "strategy_state": "strategy_state",     # 策略状态表
    "data_source": "data_source",           # 数据源注册表
    "sync_status": "sync_status",           # 数据同步状态表
    "clean_run_meta": "clean_run_meta",     # 清洗运行元信息表（任务13新增）
    "dirty_data_stats": "dirty_data_stats", # 脏数据统计表（任务13新增）
    "orders": "orders",                     # 订单关系账（KP-P4-02）
    "fills": "fills",                       # 成交关系账（KP-P4-02）
    "positions_daily": "positions_daily",   # 持仓日快照账（KP-P4-02）
    "alerts": "alerts",                     # 告警持久化表（BASE-1 告警落库）
    "metrics_history": "metrics_history",   # 指标时序存储表（BASE-2 周期落库）
    "audit_log": "audit_log",               # 审计日志只追加表（BASE-7 哈希链）
    "audit_archive": "audit_archive",       # 审计归档表（BASE-7 定期归档）
    "audit_archive_manifest": "audit_archive_manifest",  # 审计归档校验清单（BASE-7）
}


# ─────────────────────────────────────────────
# 建表SQL定义
# ─────────────────────────────────────────────

# 行情数据表（核心表，存储K线数据）
CREATE_MARKET_DATA_SQL = """
CREATE TABLE IF NOT EXISTS market_data (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,           -- 股票代码，带后缀如 000001.SZ
    market      TEXT    NOT NULL DEFAULT 'A股',  -- 市场：A股/港股/美股
    period      TEXT    NOT NULL DEFAULT 'daily', -- 周期：daily/minute/tick
    adjust      TEXT,                        -- 复权方式：raw(不复权原始层，当前)/hfq/qfq(预留)
    trade_date  TEXT    NOT NULL,           -- 交易日期 YYYY-MM-DD
    trade_time  TEXT,                        -- 交易时间（分钟线/tick用）HH:MM:SS
    open        REAL,                        -- 开盘价
    high        REAL,                        -- 最高价
    low         REAL,                        -- 最低价
    close       REAL,                        -- 收盘价
    volume      REAL,                        -- 成交量（手）
    amount      REAL,                        -- 成交额（元）
    pct_chg     REAL,                        -- 涨跌幅（百分比格式，-0.49表示-0.49%）
    turnover    REAL,                        -- 换手率（%）
    amplitude   REAL,                        -- 振幅（%，(high-low)/pre_close*100）
    volume_ratio REAL,                       -- 量比（当日成交量/过去5日均量）
    float_share  REAL,                        -- 流通股本（股，近似值）
    pe_dynamic   REAL,                        -- 市盈(动)（动态市盈率，年化净利润口径）
    is_suspended INTEGER DEFAULT 0,         -- 是否停牌 1=是 0=否
    data_source TEXT,                        -- 数据来源标识
    created_at  TEXT    DEFAULT (datetime('now')),  -- 记录创建时间
    UNIQUE(symbol, period, adjust, trade_date, trade_time)
);
"""

# 行情表索引（加速查询）
# P0-07 修正：本索引经 Alembic 0002 对齐；Alembic 链与 schema.py 的其余分叉
# （adjust_factor/share_change 两表 + 5 列 + 5 索引）由 Alembic 0003 补齐，
# 双主最终一致为 27 索引（原注释"双主一致为22索引"与事实不符）。
CREATE_MARKET_DATA_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_market_symbol ON market_data(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_market_date ON market_data(trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_market_symbol_date ON market_data(symbol, trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_market_market_period ON market_data(market, period);",
    "CREATE INDEX IF NOT EXISTS idx_market_symbol_period_date ON market_data(symbol, period, trade_date);",
]

# 财务数据表
CREATE_FINANCIAL_DATA_SQL = """
CREATE TABLE IF NOT EXISTS financial_data (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,       -- 股票代码
    market          TEXT    NOT NULL DEFAULT 'A股',
    report_date     TEXT    NOT NULL,       -- 报告期 YYYY-MM-DD（如2025-03-31一季报）
    report_type     TEXT,                    -- 报告类型：Q1/Q2/Q3/Q4/年报
    ann_date        TEXT,                    -- 公告日 YYYY-MM-DD（实际披露日，防未来函数时点）
    pe              REAL,                    -- 市盈率(TTM)
    pb              REAL,                    -- 市净率
    ps              REAL,                    -- 市销率
    total_mv        REAL,                    -- 总市值（元）
    circ_mv         REAL,                    -- 流通市值（元）
    revenue         REAL,                    -- 营业收入（元）
    net_profit      REAL,                    -- 净利润（元）
    net_profit_yoy  REAL,                    -- 净利润同比增长率（%）
    revenue_yoy     REAL,                    -- 营收同比增长率（%）
    roe             REAL,                    -- 净资产收益率（%）
    gross_margin    REAL,                    -- 毛利率（%）
    debt_ratio      REAL,                    -- 资产负债率（%）
    eps             REAL,                    -- 每股收益
    bps             REAL,                    -- 每股净资产
    data_source     TEXT,                    -- 数据来源
    created_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(symbol, report_date)
);
"""

CREATE_FINANCIAL_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_fin_symbol ON financial_data(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_fin_report_date ON financial_data(report_date);",
    "CREATE INDEX IF NOT EXISTS idx_fin_ann_date ON financial_data(ann_date);",
]

# 因子数据表（存储指标计算结果，避免重复计算）
CREATE_FACTOR_DATA_SQL = """
CREATE TABLE IF NOT EXISTS factor_data (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,           -- 股票代码
    market      TEXT    NOT NULL DEFAULT 'A股',
    trade_date  TEXT    NOT NULL,           -- 交易日期
    factor_name TEXT    NOT NULL,           -- 因子名称，如 volatility_20/momentum_60/rsrs_score
    factor_value REAL,                       -- 因子值
    factor_rank REAL,                        -- 因子排名（百分位0-1）
    calc_method TEXT,                        -- 计算方法标识
    data_source TEXT,                        -- 数据来源
    created_at  TEXT    DEFAULT (datetime('now')),
    UNIQUE(symbol, trade_date, factor_name)
);
"""

CREATE_FACTOR_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_factor_symbol_date ON factor_data(symbol, trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_factor_name_date ON factor_data(factor_name, trade_date);",
]

# 复权因子表（除权除息事件后复权因子，方案B #82：hfq价 = raw价 × factor，事件日及之后生效）
CREATE_ADJUST_FACTOR_SQL = """
CREATE TABLE IF NOT EXISTS adjust_factor (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,           -- 股票代码
    event_date  TEXT    NOT NULL,           -- 除权除息事件日 YYYY-MM-DD（该日及之后因子生效）
    factor      REAL    NOT NULL,           -- 后复权因子（hfq价 = raw价 × factor）
    data_source TEXT,                        -- 数据来源（akshare_em）
    created_at  TEXT    DEFAULT (datetime('now')),
    UNIQUE(symbol, event_date)
);
"""

CREATE_ADJUST_FACTOR_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_adj_factor_symbol ON adjust_factor(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_adj_factor_symbol_date ON adjust_factor(symbol, event_date);",
]

# 股本变动表（真实总股本/流通股本事件，P1-② #83：变更日及之后生效，消除解禁/增发/回购偏差）
CREATE_SHARE_CHANGE_SQL = """
CREATE TABLE IF NOT EXISTS share_change (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT    NOT NULL,           -- 股票代码（600519.SH）
    change_date   TEXT    NOT NULL,           -- 股本变更生效日期 YYYY-MM-DD（该日及之后生效）
    total_share   REAL,                        -- 总股本（股）
    float_share   REAL,                        -- 已上市流通A股（股）
    change_reason TEXT,                        -- 变动原因（首发/送股/回购等）
    data_source   TEXT,                        -- 数据来源（akshare_em）
    created_at    TEXT    DEFAULT (datetime('now')),
    UNIQUE(symbol, change_date)
);
"""

CREATE_SHARE_CHANGE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_share_change_symbol ON share_change(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_share_change_symbol_date ON share_change(symbol, change_date);",
]

# 策略状态表（回测/实盘状态持久化，崩溃恢复用）
CREATE_STRATEGY_STATE_SQL = """
CREATE TABLE IF NOT EXISTS strategy_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name   TEXT    NOT NULL,       -- 策略名称
    run_id          TEXT    NOT NULL,       -- 运行实例ID（每次回测/实盘唯一）
    state_key       TEXT    NOT NULL,       -- 状态键，如 position/order/stop_loss_price
    state_value     TEXT,                    -- 状态值（JSON格式）
    value_type      TEXT,                    -- 值类型：json/float/int/string
    updated_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(strategy_name, run_id, state_key)
);
"""

CREATE_STRATEGY_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_state_strategy ON strategy_state(strategy_name);",
    "CREATE INDEX IF NOT EXISTS idx_state_run_id ON strategy_state(run_id);",
]

# 数据源注册表（多源管理）
CREATE_DATA_SOURCE_SQL = """
CREATE TABLE IF NOT EXISTS data_source (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name     TEXT    NOT NULL UNIQUE, -- 数据源名称：tdx/akshare/tushare/broker
    source_type     TEXT    NOT NULL,        -- 类型：local/network/broker
    priority        INTEGER DEFAULT 100,     -- 优先级（数字越小优先级越高）
    is_enabled      INTEGER DEFAULT 1,       -- 是否启用 1=是 0=否
    health_status   TEXT    DEFAULT 'unknown', -- 健康状态：healthy/degraded/down/unknown
    last_check_time TEXT,                     -- 最后健康检查时间
    fail_count      INTEGER DEFAULT 0,       -- 连续失败次数
    config_json     TEXT,                     -- 配置信息（JSON）
    description     TEXT,                     -- 描述
    updated_at      TEXT    DEFAULT (datetime('now'))
);
"""

# 数据同步状态表（记录每只股票最后更新时间）
CREATE_SYNC_STATUS_SQL = """
CREATE TABLE IF NOT EXISTS sync_status (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,       -- 股票代码
    market          TEXT    NOT NULL DEFAULT 'A股',
    period          TEXT    NOT NULL DEFAULT 'daily',
    data_type       TEXT    NOT NULL DEFAULT 'market', -- 数据类型：market/financial/factor
    last_sync_date  TEXT,                    -- 最后同步日期 YYYY-MM-DD
    last_sync_time  TEXT,                    -- 最后同步时间
    record_count    INTEGER DEFAULT 0,       -- 已同步记录数
    sync_status     TEXT    DEFAULT 'pending', -- 状态：pending/syncing/completed/failed
    error_message   TEXT,                    -- 错误信息
    data_source     TEXT,                    -- 使用的数据源
    updated_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(symbol, period, data_type)
);
"""

CREATE_SYNC_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_sync_status ON sync_status(sync_status);",
    "CREATE INDEX IF NOT EXISTS idx_sync_symbol ON sync_status(symbol);",
]

# 清洗运行元信息表（任务13新增：记录每次清洗运行的完整元信息）
# 对应 PipelineResult / PipelineCompleteEvent 结构，供数据集版本管理与实盘追溯
CREATE_CLEAN_RUN_META_SQL = """
CREATE TABLE IF NOT EXISTS clean_run_meta (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL UNIQUE,       -- 运行ID（离线流水线/实盘会话，全局唯一）
    data_type       TEXT    NOT NULL,              -- 数据类型：bar/tick（对应 DataType.value）
    symbol          TEXT    NOT NULL,              -- 标的代码
    status          TEXT    NOT NULL,              -- 状态：completed/failed/running（对应 PipelineResult.status）
    total           INTEGER DEFAULT 0,             -- 原始数据总数
    valid           INTEGER DEFAULT 0,             -- 清洗后有效数据数
    bad             INTEGER DEFAULT 0,             -- 脏数据条数
    valid_rate      REAL,                           -- 合格率 valid/total（0.0~1.0）
    quality_grade   TEXT,                           -- 数据质量等级（对应 QualityGrade.value）
    severity        TEXT,                           -- 严重级别（对应 SeverityLevel.value）
    steps_executed  TEXT,                           -- 已执行步骤（JSON数组，对应 CleanStep.value）
    step_durations  TEXT,                           -- 各步骤耗时（JSON对象）
    total_duration  REAL,                           -- 总耗时（秒）
    bad_file        TEXT,                           -- 脏数据归档文件路径
    report_file     TEXT,                           -- 质量报告文件路径
    processed_file  TEXT,                           -- processed数据文件路径
    source          TEXT    DEFAULT 'offline',     -- 触发来源：offline/live（实盘预留）
    started_at      TEXT,                           -- 开始时间（UTC，YYYY-MM-DD HH:MM:SS）
    finished_at     TEXT,                           -- 结束时间（UTC）
    error           TEXT,                           -- 错误信息（失败时）
    created_at      TEXT    DEFAULT (datetime('now'))  -- 记录创建时间（UTC）
);
"""

CREATE_CLEAN_RUN_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_clean_run_symbol ON clean_run_meta(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_data_type ON clean_run_meta(data_type);",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_status ON clean_run_meta(status);",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_started_at ON clean_run_meta(started_at);",
    "CREATE INDEX IF NOT EXISTS idx_clean_run_symbol_type ON clean_run_meta(symbol, data_type);",
]

# 脏数据统计表（任务13新增：按脏数据类型逐类统计，供质量分析/告警/风控追溯）
CREATE_DIRTY_DATA_STATS_SQL = """
CREATE TABLE IF NOT EXISTS dirty_data_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,              -- 关联 clean_run_meta.run_id
    symbol          TEXT    NOT NULL,              -- 标的代码
    data_type       TEXT    NOT NULL,              -- 数据类型：bar/tick
    dirty_type      TEXT    NOT NULL,              -- 脏数据类型（对应 DirtyDataType.value，14种）
    severity        TEXT,                           -- 严重级别：error/warning（对应 DirtyDataType.severity）
    bad_count       INTEGER DEFAULT 0,             -- 该类脏数据条数
    repair_strategy TEXT,                           -- 修复策略（对应 RepairStrategy.value）
    sample_data     TEXT,                           -- 样本数据（JSON，首条脏数据样本，便于人工复核）
    created_at      TEXT    DEFAULT (datetime('now')),
    UNIQUE(run_id, symbol, data_type, dirty_type)
);
"""

CREATE_DIRTY_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_dirty_run ON dirty_data_stats(run_id);",
    "CREATE INDEX IF NOT EXISTS idx_dirty_symbol ON dirty_data_stats(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_dirty_type ON dirty_data_stats(dirty_type);",
    "CREATE INDEX IF NOT EXISTS idx_dirty_severity ON dirty_data_stats(severity);",
]

# 订单关系账表（KP-P4-02：逐笔订单持久化，长期归档）
CREATE_ORDERS_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT    NOT NULL UNIQUE,   -- 订单ID（券商端可去重的确定性ID）
    symbol       TEXT    NOT NULL,          -- 标的代码
    side         TEXT    NOT NULL,          -- 方向：buy/sell
    order_type   TEXT,                      -- 订单类型：market/limit
    quantity     REAL,                       -- 委托数量
    price        REAL,                       -- 委托价格（市价单为0）
    status       TEXT    NOT NULL,          -- 状态：submitted/filled/rejected/cancelled
    strategy_id  TEXT,                       -- 策略ID
    engine_id    TEXT,                       -- 引擎ID
    submitted_at REAL,                       -- 提交时间（epoch秒）
    created_at   TEXT    DEFAULT (datetime('now'))  -- 记录创建时间（UTC）
);
"""

CREATE_ORDERS_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);",
    "CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy_id);",
]

# 成交关系账表（KP-P4-02：逐笔成交持久化，与broker逐笔一致）
CREATE_FILLS_SQL = """
CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     TEXT    NOT NULL UNIQUE,   -- 成交ID
    order_id     TEXT    NOT NULL,          -- 关联订单ID
    symbol       TEXT    NOT NULL,          -- 标的代码
    side         TEXT    NOT NULL,          -- 方向：buy/sell
    quantity     REAL,                       -- 成交数量
    price        REAL,                       -- 成交价格
    commission   REAL    DEFAULT 0,         -- 手续费
    slippage     REAL    DEFAULT 0,         -- 滑点
    trade_time   REAL,                       -- 成交时间（epoch秒）
    strategy_id  TEXT,                       -- 策略ID
    engine_id    TEXT,                       -- 引擎ID
    created_at   TEXT    DEFAULT (datetime('now'))  -- 记录创建时间（UTC）
);
"""

CREATE_FILLS_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_fills_order_id ON fills(order_id);",
    "CREATE INDEX IF NOT EXISTS idx_fills_symbol ON fills(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_fills_trade_time ON fills(trade_time);",
]

# 持仓日快照账表（KP-P4-02：逐日持仓快照，删除当前快照不影响历史查询）
CREATE_POSITIONS_DAILY_SQL = """
CREATE TABLE IF NOT EXISTS positions_daily (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date  TEXT    NOT NULL,        -- 快照日期 YYYY-MM-DD
    symbol         TEXT    NOT NULL,        -- 标的代码
    quantity       REAL,                     -- 持仓数量
    avg_cost       REAL,                     -- 持仓均价
    market_price   REAL,                     -- 最新市场价
    market_value   REAL,                     -- 市值
    realized_pnl   REAL,                     -- 已实现盈亏
    unrealized_pnl REAL,                     -- 未实现盈亏
    cash           REAL,                     -- 当日现金
    account_id     TEXT    DEFAULT 'default', -- 账户ID
    created_at     TEXT    DEFAULT (datetime('now')),
    UNIQUE(snapshot_date, symbol, account_id)
);
"""

CREATE_POSITIONS_DAILY_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_positions_date ON positions_daily(snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions_daily(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_positions_date_symbol ON positions_daily(snapshot_date, symbol);",
]


# ─────────────────────────────────────────────
# 告警持久化表（BASE-1：与 0005_add_alerts.py 迁移 DDL 逐字一致）
# ─────────────────────────────────────────────

CREATE_ALERTS_SQL = """
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

CREATE_ALERTS_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_alerts_dedup_key ON alerts(dedup_key);",
    "CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);",
    "CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);",
]


# ─────────────────────────────────────────────
# 指标时序存储表（BASE-2：与 0006_add_metrics_history.py 迁移 DDL 逐字一致）
# ─────────────────────────────────────────────

CREATE_METRICS_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS metrics_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    labels_json     TEXT    NOT NULL DEFAULT '{}',
    value           REAL    NOT NULL,
    timestamp       REAL    NOT NULL,
    value_encrypted TEXT             DEFAULT NULL,   -- BASE-9 敏感字段密文（命中敏感名时非空）
    UNIQUE (name, labels_json, timestamp)
);
"""

# 按天分区索引：date(timestamp,'unixepoch') 表达式索引（UTC 日，SQLite 索引
# 表达式须确定性，不可用 localtime 修饰符），支撑"按天分区查询/清理"
CREATE_METRICS_HISTORY_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_metrics_history_name_ts ON metrics_history(name, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_metrics_history_day ON metrics_history(date(timestamp, 'unixepoch'));",
    "CREATE INDEX IF NOT EXISTS idx_metrics_history_ts ON metrics_history(timestamp);",
]


# ─────────────────────────────────────────────
# 审计只追加表（BASE-7 审计只追加与归档校验）
# ─────────────────────────────────────────────

# 降级哈希链设计（个人系统版，非完整哈希链）：
#   - audit_log 每行含 seq（链序号，从 1 递增，UNIQUE）
#   - row_hash = SHA-256(kind|action|actor|detail_json|created_ts|seq)（规范化序列）
#   - prev_hash = 上一条 row_hash（首条为 64 个 '0'）
#   - verify_chain() 按 seq 顺序重算并比对 → 常规 UPDATE/DELETE 或中间篡改
#     立即断链可检出
# 归档（archive）：旧行按原 seq/prev_hash/row_hash 整体 COPY 到 audit_archive
#   （哈希不重算，保持原链不可变），主表删除；manifest 记录归档批 row_count/
#   first_seq/last_seq/checksum（= 归档行 row_hash 按序聚合 SHA-256），
#   verify_archive() 重算聚合校验和比对 → 归档校验通过
CREATE_AUDIT_LOG_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,           -- 审计类别：alert/order/fill/metric/config
    action      TEXT    NOT NULL,           -- 动作：create/update/ack/resolve/archive 等
    actor       TEXT    NOT NULL DEFAULT 'system',  -- 操作者（系统/用户标识）
    detail_json TEXT    NOT NULL DEFAULT '{}',      -- 明细（dict 序列化，排序 key）
    seq         INTEGER NOT NULL,           -- 链序号（从 1 递增，UNIQUE）
    prev_hash   TEXT    NOT NULL,           -- 上一条 row_hash（首条 64 个 '0'）
    row_hash    TEXT    NOT NULL,           -- 本条规范化序列 SHA-256
    created_ts  REAL    NOT NULL,           -- 记录时刻（epoch 秒，当前时点）
    UNIQUE (seq),
    UNIQUE (row_hash)
);
"""

CREATE_AUDIT_LOG_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_audit_log_kind_ts ON audit_log(kind, created_ts);",
    "CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(created_ts);",
]

CREATE_AUDIT_ARCHIVE_SQL = """
CREATE TABLE IF NOT EXISTS audit_archive (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    actor       TEXT    NOT NULL DEFAULT 'system',
    detail_json TEXT    NOT NULL DEFAULT '{}',
    seq         INTEGER NOT NULL,           -- 原链序号（保持与主表一致）
    prev_hash   TEXT    NOT NULL,           -- 原 prev_hash（不改写）
    row_hash    TEXT    NOT NULL,           -- 原 row_hash（不改写）
    created_ts  REAL    NOT NULL,
    archived_ts REAL    NOT NULL,           -- 归档时刻（当前时点）
    UNIQUE (seq),
    UNIQUE (row_hash)
);
"""

CREATE_AUDIT_ARCHIVE_MANIFEST_SQL = """
CREATE TABLE IF NOT EXISTS audit_archive_manifest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_cutoff_ts REAL  NOT NULL,       -- 归档截止（< 该时间戳的行）
    row_count       INTEGER NOT NULL,       -- 归档行数
    first_seq       INTEGER NOT NULL,       -- 归档批起始 seq
    last_seq        INTEGER NOT NULL,       -- 归档批结束 seq
    checksum        TEXT    NOT NULL,       -- 归档行 row_hash 按序聚合 SHA-256
    created_ts      REAL    NOT NULL        -- 归档时刻（当前时点）
);
"""


# ─────────────────────────────────────────────
# 所有建表SQL汇总
# ─────────────────────────────────────────────

ALL_CREATE_TABLE_SQL = [
    CREATE_MARKET_DATA_SQL,
    CREATE_FINANCIAL_DATA_SQL,
    CREATE_FACTOR_DATA_SQL,
    CREATE_ADJUST_FACTOR_SQL,
    CREATE_SHARE_CHANGE_SQL,
    CREATE_STRATEGY_STATE_SQL,
    CREATE_DATA_SOURCE_SQL,
    CREATE_SYNC_STATUS_SQL,
    CREATE_CLEAN_RUN_META_SQL,
    CREATE_DIRTY_DATA_STATS_SQL,
    CREATE_ORDERS_SQL,
    CREATE_FILLS_SQL,
    CREATE_POSITIONS_DAILY_SQL,
    CREATE_ALERTS_SQL,
    CREATE_METRICS_HISTORY_SQL,
    CREATE_AUDIT_LOG_SQL,
    CREATE_AUDIT_ARCHIVE_SQL,
    CREATE_AUDIT_ARCHIVE_MANIFEST_SQL,
]

ALL_CREATE_INDEX_SQL = (
    CREATE_MARKET_DATA_INDEX_SQL
    + CREATE_FINANCIAL_INDEX_SQL
    + CREATE_FACTOR_INDEX_SQL
    + CREATE_ADJUST_FACTOR_INDEX_SQL
    + CREATE_SHARE_CHANGE_INDEX_SQL
    + CREATE_STRATEGY_INDEX_SQL
    + CREATE_SYNC_INDEX_SQL
    + CREATE_CLEAN_RUN_INDEX_SQL
    + CREATE_DIRTY_INDEX_SQL
    + CREATE_ORDERS_INDEX_SQL
    + CREATE_FILLS_INDEX_SQL
    + CREATE_POSITIONS_DAILY_INDEX_SQL
    + CREATE_ALERTS_INDEX_SQL
    + CREATE_METRICS_HISTORY_INDEX_SQL
    + CREATE_AUDIT_LOG_INDEX_SQL
)


# ─────────────────────────────────────────────
# Schema 类定义（结构化描述，便于代码引用）
# ─────────────────────────────────────────────

class MarketDataSchema:
    """行情数据表结构定义"""
    TABLE_NAME = TABLE_NAMES["market_data"]
    COLUMNS = [
        "id", "symbol", "market", "period", "adjust",
        "trade_date", "trade_time", "open", "high", "low",
        "close", "volume", "amount", "pct_chg", "turnover",
        "is_suspended", "data_source", "created_at",
    ]
    # 与交接文档5.2节约定一致的核心字段
    CORE_FIELDS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]


class FinancialDataSchema:
    """财务数据表结构定义"""
    TABLE_NAME = TABLE_NAMES["financial_data"]
    COLUMNS = [
        "id", "symbol", "market", "report_date", "report_type", "ann_date",
        "pe", "pb", "ps", "total_mv", "circ_mv",
        "revenue", "net_profit", "net_profit_yoy", "revenue_yoy",
        "roe", "gross_margin", "debt_ratio", "eps", "bps",
        "data_source", "created_at",
    ]


class FactorDataSchema:
    """因子数据表结构定义"""
    TABLE_NAME = TABLE_NAMES["factor_data"]
    COLUMNS = [
        "id", "symbol", "market", "trade_date",
        "factor_name", "factor_value", "factor_rank",
        "calc_method", "data_source", "created_at",
    ]


class StrategyStateSchema:
    """策略状态表结构定义"""
    TABLE_NAME = TABLE_NAMES["strategy_state"]
    COLUMNS = [
        "id", "strategy_name", "run_id", "state_key",
        "state_value", "value_type", "updated_at",
    ]


class DataSourceSchema:
    """数据源表结构定义"""
    TABLE_NAME = TABLE_NAMES["data_source"]
    COLUMNS = [
        "id", "source_name", "source_type", "priority",
        "is_enabled", "health_status", "last_check_time",
        "fail_count", "config_json", "description", "updated_at",
    ]


class CleanRunMetaSchema:
    """
    清洗运行元信息表结构定义（任务13新增）

    与 PipelineResult / PipelineCompleteEvent 结构对应，
    存储每次清洗运行的完整元信息，供数据集版本管理与实盘追溯。
    """
    TABLE_NAME = TABLE_NAMES["clean_run_meta"]
    COLUMNS = [
        "id", "run_id", "data_type", "symbol", "status",
        "total", "valid", "bad", "valid_rate", "quality_grade",
        "severity", "steps_executed", "step_durations", "total_duration",
        "bad_file", "report_file", "processed_file", "source",
        "started_at", "finished_at", "error", "created_at",
    ]
    # 写入列（排除自增id与自动生成的时间戳）
    WRITE_COLUMNS = [
        "run_id", "data_type", "symbol", "status",
        "total", "valid", "bad", "valid_rate", "quality_grade",
        "severity", "steps_executed", "step_durations", "total_duration",
        "bad_file", "report_file", "processed_file", "source",
        "started_at", "finished_at", "error",
    ]
    # 运行状态常量（与 PipelineResult.status 一致）
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    # 触发来源
    SOURCE_OFFLINE = "offline"
    SOURCE_LIVE = "live"


class DirtyDataStatsSchema:
    """
    脏数据统计表结构定义（任务13新增）

    与 DirtyDataType / DIRTY_TYPE_DESC_MAP 对应，
    按脏数据类型逐类统计，供质量分析/告警/风控追溯。
    """
    TABLE_NAME = TABLE_NAMES["dirty_data_stats"]
    COLUMNS = [
        "id", "run_id", "symbol", "data_type", "dirty_type",
        "severity", "bad_count", "repair_strategy", "sample_data", "created_at",
    ]
    # 写入列（排除自增id与自动生成的时间戳）
    WRITE_COLUMNS = [
        "run_id", "symbol", "data_type", "dirty_type",
        "severity", "bad_count", "repair_strategy", "sample_data",
    ]


# ─────────────────────────────────────────────
# 建表函数
# ─────────────────────────────────────────────

def _migrate_market_data_columns(connection) -> None:
    """
    迁移 market_data 表：补加派生量列（振幅 amplitude、量比 volume_ratio）。

    对已存在的表幂等执行 ALTER TABLE ADD COLUMN。SQLite 的 ADD COLUMN
    要求列不存在时才执行，先查 PRAGMA table_info 判断，避免重复加列报错。
    """
    existing_rows = connection.execute(
        "PRAGMA table_info(market_data)"
    )
    existing_cols = {row["name"] for row in existing_rows}
    new_columns = {
        "amplitude": "REAL",
        "volume_ratio": "REAL",
        "float_share": "REAL",
        "pe_dynamic": "REAL",
    }
    for col_name, col_type in new_columns.items():
        if col_name not in existing_cols:
            connection.execute_update(
                f"ALTER TABLE market_data ADD COLUMN {col_name} {col_type}"
            )


def _migrate_financial_data_columns(connection) -> None:
    """
    迁移 financial_data 表：补加公告日列 ann_date（F-15）。

    公告日（实际披露日）区别于报告期（report_date）：Q2 报告期 6/30
    实际披露常在 8 月底，用 report_date <= trade_date 会在披露前
    错误引用未来财报。ann_date 为 NULL 的存量行在查询时回退统一时点日落点
    （v3.0 §6.4.2：法定披露截止日推定 report_deadline，M1A-02 收敛）。
    幂等：先 PRAGMA 判断列是否存在。
    """
    existing_rows = connection.execute(
        "PRAGMA table_info(financial_data)"
    )
    existing_cols = {row["name"] for row in existing_rows}
    if "ann_date" not in existing_cols:
        connection.execute_update(
            "ALTER TABLE financial_data ADD COLUMN ann_date TEXT"
        )


def create_all_tables(connection) -> None:
    """
    创建所有表和索引

    Parameters
    ----------
    connection : DatabaseConnection
        数据库连接实例
    """
    with connection.transaction():
        # 建表
        for sql in ALL_CREATE_TABLE_SQL:
            connection.execute_update(sql)
        # 迁移：对已存在的表补加列（幂等，须在建索引之前，否则新列索引报错）
        _migrate_market_data_columns(connection)
        _migrate_financial_data_columns(connection)
        # 建索引
        for sql in ALL_CREATE_INDEX_SQL:
            connection.execute_update(sql)


def drop_all_tables(connection) -> None:
    """删除所有表（危险操作，仅测试用）"""
    table_order = [
        TABLE_NAMES["dirty_data_stats"],   # 先删子表（外键关联 clean_run_meta）
        TABLE_NAMES["clean_run_meta"],
        TABLE_NAMES["sync_status"],
        TABLE_NAMES["data_source"],
        TABLE_NAMES["strategy_state"],
        TABLE_NAMES["factor_data"],
        TABLE_NAMES["adjust_factor"],
        TABLE_NAMES["share_change"],
        TABLE_NAMES["financial_data"],
        TABLE_NAMES["positions_daily"],
        TABLE_NAMES["fills"],
        TABLE_NAMES["orders"],
        TABLE_NAMES["market_data"],
        TABLE_NAMES["alerts"],
        TABLE_NAMES["metrics_history"],
        TABLE_NAMES["audit_archive_manifest"],  # BASE-7 审计三表（无外键，最后删）
        TABLE_NAMES["audit_archive"],
        TABLE_NAMES["audit_log"],
    ]
    with connection.transaction():
        for table in table_order:
            connection.execute_update(f"DROP TABLE IF EXISTS {table}")


def table_exists(connection, table_name: str) -> bool:
    """检查表是否存在"""
    row = connection.execute_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return row is not None


def get_table_count(connection, table_name: str) -> int:
    """获取表记录数"""
    row = connection.execute_one(f"SELECT COUNT(*) as cnt FROM {table_name}")
    return row["cnt"] if row else 0
