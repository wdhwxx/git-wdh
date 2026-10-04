# 文件名：docs/data_dictionary.md
# 鲲鹏量化交易系统 Pythonwdhmm — 数据字典

## 1. 数据存储架构

datasets/
├── raw/                    # 原始行情数据（不可变，从数据源直接下载）
│   └── YYYYMMDD/           # 按日期组织
├── processed/              # 清洗对齐后的数据（回测 / 实盘读取）
│   ├── 000001.parquet      # 每只股票一个文件
│   ├── 000002.parquet
│   └── ...
└── features/               # 特征数据（因子计算中间结果，供策略使用）
└── stock_factors.parquet

## 2. 行情数据字段说明（processed/*.parquet）

### 2.1 字段定义

| 字段名 | 类型 | 单位 | 说明 | 示例 |
|---|---|---|---|---|
| date | datetime64 | - | 交易日期 | 2024-01-15 |
| open | float64 | 元 | 开盘价（后复权） | 12.35 |
| high | float64 | 元 | 最高价（后复权） | 12.58 |
| low | float64 | 元 | 最低价（后复权） | 12.20 |
| close | float64 | 元 | 收盘价（后复权） | 12.45 |
| volume | float64 | 股 | 成交量 | 12345678 |
| amount | float64 | 元 | 成交额 | 152345678.50 |
| pct_chg | float64 | % | 涨跌幅（百分比格式） | -0.49（表示-0.49%） |

### 2.2 重要注意事项

- **pct_chg 是百分比格式**：-0.49 表示 -0.49%，不是 -0.0049。计算波动率时需除以100
- **价格为后复权**：回测使用后复权价格（hfq），实盘信号生成也使用后复权
- **数据范围**：约820只沪深A股，日期从2018年至今
- **数据频率**：日线（daily），后续可扩展分钟级

### 2.3 数据质量要求

- 无缺失值：OHLCV字段不允许为空
- 无未来数据：每日数据在当日收盘后才可使用
- 停牌处理：停牌日数据缺失，回测中跳过
- 涨跌停：保留原始涨跌停价格，撮合层拦截

## 3. 数据库表结构（SQLite）

数据库文件：database/quant.db（通过 myquant/database/ 管理）

### 3.1 股票基础信息表（stock_basic）

| 字段名 | 类型 | 说明 |
|---|---|---|
| ts_code | TEXT PRIMARY KEY | 股票代码（带后缀，如000001.SZ） |
| symbol | TEXT | 股票代码（不带后缀，如000001） |
| name | TEXT | 股票名称 |
| area | TEXT | 地域 |
| industry | TEXT | 所属行业 |
| market | TEXT | 市场类型（主板/创业板/科创板） |
| list_date | TEXT | 上市日期 |
| delist_date | TEXT | 退市日期（NULL表示在市） |
| is_st | INTEGER | 是否ST（0=否，1=是） |
| updated_at | TEXT | 最后更新时间 |

### 3.2 日线行情表（daily_bar）

| 字段名 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| ts_code | TEXT | 股票代码（外键关联stock_basic） |
| trade_date | TEXT | 交易日期（YYYY-MM-DD） |
| open | REAL | 开盘价（后复权） |
| high | REAL | 最高价（后复权） |
| low | REAL | 最低价（后复权） |
| close | REAL | 收盘价（后复权） |
| vol | REAL | 成交量（股） |
| amount | REAL | 成交额（元） |
| pct_chg | REAL | 涨跌幅（%） |
| UNIQUE(ts_code, trade_date) | | 联合唯一约束 |

### 3.3 因子数据表（stock_factor）

| 字段名 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| ts_code | TEXT | 股票代码 |
| trade_date | TEXT | 交易日期 |
| volatility_20 | REAL | 20日波动率（年化） |
| momentum_60 | REAL | 60日动量（收益率） |
| price_rank | REAL | 价格分位数 |
| factor_score | REAL | 综合因子得分 |
| UNIQUE(ts_code, trade_date) | | 联合唯一约束 |

### 3.4 交易记录表（trade_record）

| 字段名 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| trade_date | TEXT | 交易日期 |
| ts_code | TEXT | 股票代码 |
| direction | TEXT | 方向（buy/sell） |
| price | REAL | 成交价格 |
| volume | INTEGER | 成交数量（股） |
| amount | REAL | 成交金额 |
| commission | REAL | 佣金 |
| stamp_tax | REAL | 印花税 |
| transfer_fee | REAL | 过户费 |
| slippage_cost | REAL | 滑点成本 |
| strategy_name | TEXT | 策略名称 |
| signal_date | TEXT | 信号产生日期（T日） |

### 3.5 持仓状态表（position_state）

| 字段名 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| ts_code | TEXT | 股票代码 |
| buy_date | TEXT | 买入日期 |
| buy_price | REAL | 买入价格 |
| volume | INTEGER | 持仓数量 |
| available_volume | INTEGER | 可用数量（T+1限制） |
| cost_amount | REAL | 持仓成本 |
| current_price | REAL | 当前价格 |
| market_value | REAL | 当前市值 |
| profit_pct | REAL | 盈亏比例 |
| stop_loss_price | REAL | 止损价 |
| take_profit_price | REAL | 止盈价 |
| status | TEXT | 状态（holding/closed） |
| updated_at | TEXT | 最后更新时间 |

### 3.6 实盘运行日志表（live_run_log）

| 字段名 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 自增主键 |
| run_date | TEXT | 运行日期 |
| run_mode | TEXT | 运行模式（paper/live） |
| start_time | TEXT | 开始时间 |
| end_time | TEXT | 结束时间 |
| status | TEXT | 状态（success/failed/running） |
| buy_count | INTEGER | 买入信号数 |
| sell_count | INTEGER | 卖出信号数 |
| error_message | TEXT | 错误信息（NULL表示无错误） |

## 4. 实时行情数据格式（为实盘预留）

### 4.1 Tick数据格式

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
| open | float | 今日开盘价 |
| high | float | 今日最高价 |
| low | float | 今日最低价 |
| pre_close | float | 昨日收盘价 |

### 4.2 分钟Bar数据格式

| 字段名 | 类型 | 说明 |
|---|---|---|
| ts_code | str | 股票代码 |
| datetime | datetime | Bar时间（如10:30表示10:25-10:30） |
| frequency | str | 周期（1m/5m/15m/30m/60m） |
| open | float | 开盘价 |
| high | float | 最高价 |
| low | float | 最低价 |
| close | float | 收盘价 |
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

- 免费开源，无需token
- 日线数据：ak.stock_zh_a_hist()（东方财富）
- 实时行情：ak.stock_zh_a_spot_em()（为实盘预留）
- 股票列表：ak.stock_info_a_code_name()
- 数据格式：DataFrame，字段名需映射到标准格式

### 5.3 数据源优先级

```yaml
data:
  source_priority:
    - "eastmoney"    # 东方财富（主）
    - "tencent"      # 腾讯（备）
    - "sina"         # 新浪（备）
## 6. 数据版本管理

### 6.1 版本管理原则

- datasets/raw/：原始数据不可变，下载后不修改
- datasets/processed/：清洗后数据，每次更新覆盖旧文件
- datasets/features/：因子数据，按日期版本化存储

### 6.2 数据更新流程
1. akshare增量下载最新日线数据 → datasets/raw/YYYYMMDD/
2. 数据清洗（去重、补全、复权处理）→ datasets/processed/
3. 因子计算（波动率、动量、价格分位）→ datasets/features/
4. 更新数据库索引（daily_bar表）
5. 记录更新日志（live_run_log表）
### 6.3 数据校验规则

- 下载后校验：记录数、字段完整性、日期连续性
- 清洗后校验：无缺失值、价格合理性（high>=low 等）
- 因子计算校验：波动率 > 0、动量范围合理
- 防未来函数校验：T 日数据在 T 日收盘后才可用

---

把上面框里的**全部内容**复制到 `docs/data_dictionary.md` 里保存。

完成后回复我"第2个完成"，我再给你第3个文件 `strategy_specs.md` 的内容。
