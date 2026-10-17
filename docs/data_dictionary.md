# 文件名：docs/data_dictionary.md
# 鲲鹏量化交易系统 Pythonwdhmm — 数据字典

> **版本**：v1.1 实测核验版配套修订（2026-10-17，A-06 文档失真修正）
> **修订依据**：代码事实（myquant/database/schema.py、scripts/数据导出_DB到processed.py、myquant/data_access/provider.py）
> **旧版失真说明**：本文档旧版（2026-10-17 前）表名/复权口径/字段清单与代码不符，A-06 已按代码事实逐条修正。

## 1. 数据存储架构

datasets/
├── raw/                    # 原始行情数据（不可变，从数据源直接下载）
│   └── financial/          # 财务 CSV（A-04 起含 ann_date 列，18 列）
├── processed/              # 清洗对齐后的 hfq 日线（回测 / 实盘读取）
│   ├── 000001.parquet      # 每只股票一个文件
│   ├── 000002.parquet
│   └── ...
└── features/               # ⚠️ 【C-09 决策 2026-10-17】保留占位（仅 .gitkeep）：因子**不落库**，
                            # DWS 口径=processed + DB 派生列组合（adjust_factor/financial_data 现算），
                            # 因子在内存现算（features.py FactorEngine/IncrementalFeatureSession）；
                            # factor_data 表=可选能力（代码完备、未启用、无消费方、不设计门禁）

## 2. 行情数据字段说明（processed/*.parquet，扁平式中文字段）

### 2.1 字段定义（15 列，与增量/全量写入方 schema 一致）

| 字段名 | 类型 | 单位 | 说明 | 示例 |
|---|---|---|---|---|
| 日期 | int64 | - | 交易日期（整数 YYYYMMDD） | 20240115 |
| 开盘价 | float64 | 元 | 开盘价（后复权 hfq） | 12.35 |
| 最高价 | float64 | 元 | 最高价（后复权） | 12.58 |
| 最低价 | float64 | 元 | 最低价（后复权） | 12.20 |
| 收盘价 | float64 | 元 | 收盘价（后复权） | 12.45 |
| 成交量 | float64 | 股 | 成交量（A-07 统一口径为股） | 12345678 |
| 成交额 | float64 | 元 | 成交额 | 152345678.50 |
| 涨跌幅 | float64 | % | 涨跌幅（百分比格式） | -0.49（表示-0.49%） |
| 振幅 | float64 | % | (high-low)/pre_close×100 | 3.12 |
| 量比 | float64 | - | 当日成交量/过去 5 日均量 | 1.25 |
| 换手率 | float64 | % | 换手率 | 2.31 |
| 流通股本 | float64 | 股 | 流通股本（近似值） | 1.94e10 |
| 市盈动 | float64 | - | 动态市盈率（年化净利润口径） | 15.2 |
| 每股收益 | float64 | 元 | 按报告期向前匹配最近一期 eps | 1.24 |
| available_time | int64 | - | 财务列可用时点（有效披露日+1，防未来函数） | 20240116 |

> **A-09 说明**：processed 统一不含「前收盘价」派生列（方案二）；读取层由 provider.py:797-799 / mixed_engine.py:1120-1123 按收盘价.shift(1) 兜底派生。

### 2.2 重要注意事项

- **pct_chg 是百分比格式**：-0.49 表示 -0.49%，不是 -0.0049。计算波动率时需除以 100
- **复权口径（修正）**：数据库 market_data 当前存 **raw（不复权原始层）**（schema.py adjust 注释：raw 当前 / hfq/qfq 预留）；**processed 为 hfq 后复权日线**（导出脚本 scripts/数据导出_DB到processed.py 从 quant.db 导出 hfq）。回测读取 processed（hfq）；实盘信号数据源为 DB raw 层（hfq 由复权因子调整，方案B #82：hfq价 = raw价 × factor）
- **数据范围**：约 5,589 只沪深 A 股（实测 2026-10-17，market_data 16,917,877 行），日期从 2018 年至今
- **数据频率**：日线（daily），后续可扩展分钟级（period 列：daily/minute/tick）
- **单位口径（A-07）**：成交量统一为**股**（网易/东财数据源换算路径对齐，schema.py 注释同步）

### 2.3 数据质量要求

- 无缺失值：OHLCV 字段不允许为空
- 无未来数据：每日数据在当日收盘后才可使用；财务列以 available_time（有效披露日+1）为可见时点
- 停牌处理：停牌日数据缺失（is_suspended 标记），回测中跳过
- 涨跌停：保留原始涨跌停价格，撮合层拦截

## 3. 数据库表结构（SQLite，quant.db）

数据库文件：database/quant.db（约 6GB，WAL 模式，通过 myquant/database/ 管理；alembic 迁移链当前 head=0010）

### 3.1 表清单（schema.py TABLE_NAMES，22 张）

| 表名 | 说明 |
|---|---|
| market_data | 行情数据表（核心表，K线） |
| financial_data | 财务数据表（329,735 行；A-02/B-09 后 ann_date 链路补齐） |
| factor_data | 因子数据表（**0 行，可选能力未启用**；C-09 决策=不落库，因子内存现算） |
| adjust_factor | 复权因子表（除权除息事件，方案B #82） |
| share_change | 股本变动表（真实总/流通股本事件） |
| strategy_state | 策略状态表 |
| data_source | 数据源注册表 |
| sync_status | 数据同步状态表 |
| clean_run_meta | 清洗运行元信息表 |
| dirty_data_stats | 脏数据统计表 |
| orders / fills / positions_daily | 订单/成交/持仓日快照关系账（KP-P4-02） |
| order_events / reconciliation_runs / reconciliation_diffs / reconciliation_freeze | 对账账本 4 表（T1-4，**仅 Alembic 管理**，schema.py 未含 DDL，见 A-12） |
| alerts | 告警持久化表 |
| metrics_history | 指标时序存储表（value_encrypted 密文列） |
| audit_log / audit_archive / audit_archive_manifest | 审计日志只追加表 + 归档 + 校验清单（哈希链） |

### 3.2 核心表：market_data（行情）

| 字段名 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| symbol | TEXT NOT NULL | 股票代码（带后缀，如 000001.SZ） |
| market | TEXT DEFAULT 'A股' | 市场：A股/港股/美股 |
| period | TEXT DEFAULT 'daily' | 周期：daily/minute/tick |
| adjust | TEXT | 复权方式：raw（不复权原始层，**当前**）/hfq/qfq（预留） |
| trade_date | TEXT NOT NULL | 交易日期（YYYY-MM-DD） |
| trade_time | TEXT | 交易时间（分钟线/tick 用）HH:MM:SS |
| open / high / low / close | REAL | 开/高/低/收价 |
| volume | REAL | 成交量（**股**，A-07 统一） |
| amount | REAL | 成交额（元） |
| pct_chg | REAL | 涨跌幅（%，-0.49 表示 -0.49%） |
| turnover | REAL | 换手率（%） |
| amplitude | REAL | 振幅（%） |
| volume_ratio | REAL | 量比 |
| float_share | REAL | 流通股本（股，近似值） |
| pe_dynamic | REAL | 市盈(动) |
| is_suspended | INTEGER DEFAULT 0 | 是否停牌（1=是） |
| data_source | TEXT | 数据来源标识 |
| created_at | TEXT | 记录创建时间 |
| 唯一约束 | | UNIQUE(symbol, period, adjust, trade_date, trade_time) + 部分唯一索引 uq_market_data_daily_key（WHERE trade_time IS NULL，B-01） |

### 3.3 核心表：financial_data（财务）

| 字段名 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| symbol | TEXT NOT NULL | 股票代码 |
| report_date | TEXT NOT NULL | 报告期（YYYY-MM-DD） |
| report_type | TEXT | Q1/Q2/Q3/Q4/年报 |
| ann_date | TEXT | 公告日（实际披露日，防未来函数时点；A-02/B-09/A-04 链路补齐） |
| pe / pb / ps | REAL | 市盈率(TTM)/市净率/市销率 |
| total_mv / circ_mv | REAL | 总市值/流通市值（元） |
| revenue / net_profit | REAL | 营业收入/净利润（元） |
| net_profit_yoy / revenue_yoy | REAL | 净利/营收同比（%） |
| roe / gross_margin / debt_ratio | REAL | 净资产收益率/毛利率/资产负债率（%） |
| eps / bps | REAL | 每股收益/每股净资产 |
| data_source / created_at | TEXT | 数据来源/创建时间 |
| 唯一约束 | | UNIQUE(symbol, report_date) |

### 3.4 核心表：factor_data（因子，可选能力未启用——C-09 决策=不落库）

> **C-09 决策（2026-10-17）**：生产实测 0 行、无任何消费方（策略/模型/回测均不读）；因子在内存现算
> （features.py FactorEngine / IncrementalFeatureSession）已完备。本表保留 schema/查询/写入代码作为
> **可选能力**（未启用、不设计门禁），DWS 口径=processed + DB 派生列组合。若未来出现真实消费方，
> 需先登记蓝图再接线 `scripts/财务数据入库.py --mode factors`。

| 字段名 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| symbol | TEXT NOT NULL | 股票代码 |
| trade_date | TEXT NOT NULL | 交易日期 |
| factor_name | TEXT NOT NULL | 因子名称（SMA5/MACD_DIF/PE 等，见 financial_service TECH_FACTOR_SPECS / FUNDAMENTAL_FACTOR_MAP） |
| factor_value / factor_rank | REAL | 因子值/因子排名（百分位 0-1） |
| calc_method / data_source / created_at | TEXT | 计算方法/数据来源/创建时间 |
| 唯一约束 | | UNIQUE(symbol, trade_date, factor_name) |

## 4. 实时行情数据格式（为实盘预留）

### 4.1 Tick 数据格式

| 字段名 | 类型 | 说明 |
|---|---|---|
| ts_code | str | 股票代码 |
| datetime | datetime | 时间戳（精确到毫秒） |
| last_price | float | 最新价 |
| bid_price1-5 | float | 买1-买5价 |
| bid_vol1-5 | int | 买1-买5量 |
| ask_price1-5 | float | 卖1-卖5价 |
| ask_vol1-5 | int | 卖1-卖5量 |
| volume | int | 累计成交量 |
| amount | float | 累计成交额 |
| open / high / low | float | 今日开/高/低价 |
| pre_close | float | 昨日收盘价 |

### 4.2 分钟 Bar 数据格式

| 字段名 | 类型 | 说明 |
|---|---|---|
| ts_code | str | 股票代码 |
| datetime | datetime | Bar 时间（如 10:30 表示 10:25-10:30） |
| frequency | str | 周期（1m/5m/15m/30m/60m） |
| open / high / low / close | float | 开/高/低/收价 |
| volume | int | 成交量 |
| amount | float | 成交额 |

## 5. 数据源规范

### 5.1 数据源适配器接口

所有数据源必须实现 data_access/sources/base_source.py 定义的接口：

| 方法 | 说明 | 返回 |
|---|---|---|
| get_stock_list() | 获取股票列表 | DataFrame |
| get_daily_data(symbol, start, end) | 获取日线数据 | DataFrame |
| get_realtime_quote(symbols) | 获取实时行情（为实盘预留） | DataFrame |
| get_minute_data(symbol, freq, start, end) | 获取分钟数据（为实盘预留） | DataFrame |

### 5.2 当前数据源：AkShare

- 免费开源，无需 token
- 日线数据：ak.stock_zh_a_hist()（东方财富）
- 实时行情：ak.stock_zh_a_spot_em()（为实盘预留）
- 股票列表：ak.stock_info_a_code_name()
- 数据格式：DataFrame，字段名需映射到标准格式
- 实际入库来源（实测 data_source 分布）：tdx / tencent_http / incremental_clean（历史）+ eastmoney（新链路）

### 5.3 数据源优先级

```yaml
data:
  source_priority:
    - "eastmoney"    # 东方财富（主）
    - "tencent"      # 腾讯（备）
    - "sina"         # 新浪（备）
```

## 6. 数据版本管理

### 6.1 版本管理原则

- database/quant.db：核心数据仓库（market_data/financial_data 等，alembic 迁移管理）
- datasets/raw/：原始数据不可变，下载后不修改（财务 CSV 经 A-04 回填含 ann_date 列）
- datasets/processed/：清洗后数据，每次更新覆盖旧文件（增量只追加缺失日期、绝不覆盖存量）
- datasets/features/：⚠️ 【C-09 决策 2026-10-17】保留占位（仅 .gitkeep）：因子**不落库**，DWS 口径=processed + DB 派生列组合 + 因子内存现算（features.py FactorEngine/IncrementalFeatureSession）；factor_data=可选能力未启用、不设计门禁

### 6.2 数据更新流程

1. 增量下载：akshare 增量拉取最新日线 → 清洗管线（bar 清洗）→ 事务写回 quant.db market_data
2. 全量清洗：scripts/数据全量清洗.py → FullCleanService → 事务写回 + 清洗元信息落库 + bad 归档
3. 导出 processed：scripts/数据导出_DB到processed.py（quant.db hfq 日线 → processed 扁平 parquet，逐只导出 + 完整性校验 + 断点续传 + export_manifest.json sidecar）
4. 因子计算：预留（C-09 决策=不落库：因子内存现算；factor_data 可选能力未启用，若未来出现真实消费方须先登记蓝图再接线 scripts/财务数据入库.py --mode factors）

### 6.3 数据校验规则

- 下载后校验：记录数、字段完整性、日期连续性
- 清洗后校验：无缺失值、价格合理性（high>=low 等）；脏数据写入 dirty_data_stats（PRICE_JUMP 等 error 级）
- 防未来函数校验：T 日数据在 T 日收盘后才可用；财务列以 available_time（有效披露日+1）为可见时点（A-02/B-09/A-04 链路保证 ann_date 供给）
- 重复键治理：A-01/A-13/B-01 后重复键 = 0，uq_market_data_daily_key 部分唯一索引守护
- 一致性基线：outputs/数据层基线_<日期>.json（A-00，7 组指纹可重跑复现）
