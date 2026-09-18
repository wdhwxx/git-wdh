# 文件名：docs/architecture.md
# Pythonwdhmm 鲲鹏量化交易系统 — 架构设计文档

## 1. 系统架构总览

┌─────────────────────────────────────────────────────────────────┐
│                        入口层（Entry Points）                     │
│  run_回测入口.py    run_实盘入口.py    run_GUI 入口.py            │
└──────────┬──────────────────┬──────────────────┬────────────────┘
│                  │                  │
┌──────────▼──────────────────▼──────────────────▼────────────────┐
│                      配置层（Configuration）                      │
│  .env + config/*.yaml + myquant/core/settings.py                 │
│  优先级：环境变量 > yaml 配置 > 代码默认值                          │
└──────────┬──────────────────────────────────────────────────────┘
│
┌──────────▼──────────────────────────────────────────────────────┐
│                   核心基础设施层（Core）                           │
│  settings (配置)  logger (日志)  exceptions (异常)                   │
│  event_bus (事件总线)  cache (缓存)  lookahead_guard (防未来函数)    │
└──────────┬──────────────────────────────────────────────────────┘
│
┌──────────▼──────────────────────────────────────────────────────┐
│                     数据模型层（Models）                           │
│  trade.py (订单 / 持仓 / 成交)  market_data.py (Bar/Tick/Snapshot)      │
│  backtest.py (回测配置 / 结果)                                       │
└──────────┬──────────────────────────────────────────────────────┘
│
┌──────────▼──────────┬───────────────────┬───────────────────────┐
│   数据访问层         │   指标层           │   工具层               │
│   data_access/      │   indicators/     │   utils/              │
│   fetcher (行情拉取)  │   moving_average  │   time_utils (交易日历) │
│   manager (数据管理)  │   momentum        │   file_utils (文件)    │
│   sources (多数据源)  │   volatility      │   math_utils (统计)    │
│   akshare_source    │   rsrs (核心指标)   │                       │
└──────────┬──────────┴────────┬──────────┴───────────────────────┘
│                   │
┌──────────▼───────────────────▼───────────────────────────────────┐
│                    引擎抽象层（Engine Base）                       │
│  BaseEngine: on_init /on_market_event/on_stop                 │
│  MarketEvent: bar /tick/trade（多粒度数据驱动）                 │
└──────────┬──────────────────────────────────────────────────────┘
│
┌──────────▼──────────────────┬────────────────────────────────────┐
│   回测引擎（Backtest）       │   实盘引擎（Live）                  │
│   backtest/engine.py        │   live/engine.py                   │
│   同步时间序列事件循环        │   异步事件循环                      │
│   SimBroker 模拟撮合          │   Broker 真实 / 模拟下单               │
│   LookaheadGuard 校验         │   RiskManager 实时风控               │
│   Metrics 绩效计算            │   StateStore 状态持久化              │
│   Recorder 交易记录           │   Scheduler 定时任务                 │
│   Report 报告生成             │   DataFeed 实时行情                  │
└──────────┬──────────────────┴────────────┬───────────────────────┘
│                               │
┌──────────▼───────────────────────────────▼───────────────────────┐
│                    执行层（Execution）                             │
│  ExecutionInterface (下单 / 撤单 / 查单 / 资金查询)                       │
│  SimBroker (回测模拟撮合)  StateMachine (订单状态机)                 │
│  live/brokers/(券商适配器：xtp/qmt/ptrade/custom)                 │
└──────────┬──────────────────────────────────────────────────────┘
│
┌──────────▼──────────────────────────────────────────────────────┐
│              策略层（Strategies）+ 风控层（Risk）                  │
│  BaseStrategy + Registry (注册器)                                 │
│  RsrsStrategy (核心)  MaStrategy (示例)                             │
│  RiskManager (仓位 / 止损 / 流控，独立线程硬隔离)                       │
│  Allocation (资金分配：等权 / 风险平价 / 目标波动率)                    │
└──────────┬──────────────────────────────────────────────────────┘
│
┌──────────▼──────────────────────────────────────────────────────┐
│                    监控告警层（Monitoring）                        │
│  Notifier (飞书 / 企业微信 / 邮件)  HealthCheck (健康检查)              │
│  MetricsCollector (运行指标采集)                                   │
└─────────────────────────────────────────────────────────────────┘

## 2. 模块依赖关系

### 2.1 依赖方向（严格单向，禁止循环依赖）

入口层 → 配置层 → 核心层 → 模型层
↓
数据访问层 ← 指标层 ← 工具层
↓
引擎抽象层
↙        ↘
回测引擎        实盘引擎
↘        ↙
执行层
↓
策略层 + 风控层 + 资金分配
↓
监控告警层

### 2.2 模块职责与依赖表

| 模块 | 职责 | 依赖的模块 | 被谁依赖 |
|---|---|---|---|
| core | 配置/日志/异常/事件总线/防未来函数 | 无（最底层） | 所有模块 |
| models | 数据模型定义 | core | 所有模块 |
| utils | 通用工具函数 | 无（最底层） | 所有模块 |
| data_access | 数据获取/清洗/管理 | core, models, utils | indicators, backtest, live |
| indicators | 技术指标计算（无状态） | core, utils | strategies, backtest, live |
| engine | 引擎抽象基类 | core, models | backtest, live |
| execution | 执行接口与模拟撮合 | core, models | backtest, live |
| strategies | 策略定义与注册 | core, models, indicators | backtest, live |
| backtest | 回测引擎 | engine, execution, strategies, data_access, indicators | 入口层 |
| risk | 风控规则与拦截 | core, models | backtest, live |
| allocation | 资金分配 | core, models | backtest, live |
| live | 实盘引擎 | engine, execution, strategies, data_access, risk, monitoring | 入口层 |
| monitoring | 消息推送与健康检查 | core, utils | live, backtest |

## 3. 数据流设计

### 3.1 回测数据流（同步）

config/*.yaml
│
▼
DataManager.load_stock_data()  ← datasets/processed/*.parquet
│
▼
DataFrame (OHLCV)
│
├──→ indicators.calc_by_name ("rsrs", df)  ← 指标计算（无未来函数）
│
▼
BacktestEngine.run ()
│
├──→ 逐日遍历：on_bar (bar_data)
│       │
│       ├──→ Strategy.on_bar () → Signal (买入 / 卖出)
│       ├──→ RiskManager.check () → 通过 / 拦截
│       ├──→ SimBroker.execute () → T+1 开盘价成交
│       │       ├──→ 佣金 / 印花税 / 过户费 / 滑点扣除
│       │       ├──→ 涨跌停 / 停牌拦截
│       │       └──→ Recorder 记录成交
│       └──→ LookaheadGuard.check () ← 防未来函数校验
│
▼
MetricsCalculator → 夏普 / 回撤 / 胜率 / 盈亏比
│
▼
ReportGenerator → outputs/*.html

### 3.2 实盘数据流（异步，为实时行情预留）

定时任务（Scheduler）
│
├── 08:30 DataFetcher.update_data ()  ← akshare 增量更新日线数据
├── 15:05 LiveSignalGenerator.run ()
│       ├──→ DataManager.get_all_symbols () → 820 只股票
│       ├──→ DataManager.select_three_factor_pool () → 三因子交集
│       ├──→ indicators.calc_by_name ("rsrs") → RSRS 信号
│       ├──→ PositionManager.check_take_profit () → 止盈检查
│       └──→ Notifier.push () → 飞书 / 企业微信 / 邮件推送
└── 16:00 ReportGenerator → 实盘信号 /*.html

实时行情（未来扩展，分钟级 / Tick 级）
│
▼
DataFeed.subscribe (symbols, frequency)
│
▼
MarketEvent (tick/bar) → EventBus.publish ()
│
├──→ LiveEngine.on_market_event ()
│       ├──→ Strategy.on_tick ()/on_bar () → Signal
│       ├──→ RiskManager.check_realtime () → 实时风控拦截
│       ├──→ Broker.place_order () → 真实下单
│       └──→ StateStore.save () → 状态持久化（崩溃恢复）
│
└──→ IntradayMonitor → 盘中止盈 / 止损提醒

## 4. 同步/异步边界

### 4.1 同步区域（回测）

- 回测引擎使用同步时间序列事件循环
- 数据从本地parquet文件加载，无网络IO
- 指标计算同步完成
- 模拟撮合同步返回结果

### 4.2 异步区域（实盘，为实时行情预留）

- 实盘引擎使用异步事件循环（asyncio）
- 行情订阅通过WebSocket异步接收
- 券商下单通过异步HTTP/WebSocket
- 风控检查在独立线程运行（硬隔离）
- 状态持久化异步写入（不阻塞主循环）
- 消息推送异步发送（不阻塞交易）

### 4.3 同步/异步桥接

- 策略层（Strategy）保持同步接口：on_bar(bar_data)
- 回测和实盘都调用同一个策略方法，确保逻辑一致
- 实盘引擎将异步MarketEvent转换为同步bar数据后调用策略
- 策略代码只需写一份，回测和实盘共用

## 5. 回测与实盘一致性保障

### 5.1 共用模块（回测和实盘调用同一份代码）

| 共用模块 | 说明 |
|---|---|
| indicators | 指标计算完全一致（RSRS/ADX等） |
| strategies | 策略信号生成逻辑完全一致 |
| data_access | 数据加载和因子计算完全一致 |
| risk | 风控规则完全一致 |
| models | 数据模型完全一致 |

### 5.2 差异模块

| 模块 | 回测 | 实盘 |
|---|---|---|
| 数据源 | 本地parquet文件 | akshare实时/历史API |
| 撮合 | SimBroker模拟（T+1开盘价） | 券商真实下单 |
| 风控 | 回测后校验 | 实时独立线程拦截 |
| 状态 | 内存中，回测结束即销毁 | StateStore持久化，崩溃可恢复 |
| 推送 | 不推送 | Notifier多渠道推送 |

### 5.3 一致性校验机制

- 回测参数和实盘参数从同一份yaml配置读取
- 策略注册器确保回测和实盘加载同一个策略类
- 防未来函数校验在回测和实盘中都强制开启
- 每日实盘信号可与回测信号对比验证

## 6. 扩展性设计

### 6.1 策略扩展（插件化）

- 通过 @register_strategy("name") 装饰器注册
- 支持外部策略包通过 pyproject.toml 入口点注册
- 新增策略无需修改引擎代码

### 6.2 券商扩展（插件化）

- 券商适配器继承 live/brokers/base_broker.py
- 通过 pyproject.toml 的 myquant.brokers 入口点注册
- 新增券商只需实现接口，无需修改引擎代码
- 预留：中泰XTP、迅投QMT、CTP期货等

### 6.3 数据源扩展（插件化）

- 数据源继承 data_access/sources/base_source.py
- 通过 pyproject.toml 的 myquant.data_sources 入口点注册
- 新增数据源只需实现接口，支持Tushare、聚宽、Wind等

### 6.4 多周期扩展

- MarketEvent支持 tick/1m/5m/15m/30m/60m/daily 多粒度
- 策略可订阅多个周期，on_bar接收不同周期数据
- 当前使用日线，后续可平滑升级为分钟级/Tick级
