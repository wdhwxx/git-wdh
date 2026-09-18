> v1 落地边界小结
> ✅ **v1 要做**

1. 完整目录骨架全部保留，`TODO`标记待扩展点。
2. raw 只读约束，processed 输出、datasets/bad 脏数据落盘。
3. `data_access/cleaner`目录占位，内部实现**普通函数**（不写抽象基类）：`clean_bar_records`、`clean_tick_records`、简单质量报告函数。
4. `config/data_clean.example.yaml`配置，直接字典读取，不做复杂规则对象。
5. `run_data_clean.py`脚本入口；离线批量清洗；实盘`data_feed`复用同一批清洗函数。
6. 独立`data_quality`日志文件，脏数据写日志、写 bad 目录。
7. `test_data_clean.py`单元测试，覆盖高频问题：时间重复、OHLC 值域、异常价格、空值。

❌ **v1 不做，全部标记 TODO 留给后续迭代**

1. cleaner 模块各类抽象基类、面向对象清洗器。
2. 事件总线发布`DataQualityEvent`；监控、风控自动响应脏数据事件。
3. 数据集完整版本元管理、清洗元信息入库。
4. Web 看板数据质量页面、API 数据质量查询接口。
5. 全套极端 case 单元测试、脏数据指标采集。
---

## 文件2：E:\Projects\Pythonwdhmm\docs\stage_completion.md
```markdown
# Pythonwdhmm 阶段逻辑完成度总表
> 文件路径：docs/stage_completion.md
> 参考架构蓝图：docs/project_layout_gear2.md
> 当前项目物理路径：E:\Projects\Pythonwdhmm（注：交接文档中的D盘路径已失效，实际项目在E盘）
> 当前进度：**阶段0-8全部完成；阶段8 GUI/Web/API可选组件基础已于2026-08-27全部完成；阶段9任务1-25已全部完成（2026-08-28）；阶段9任务26双券商并行三件套（BrokerManager/QmtBroker/PtradeBroker）已于2026-08-29完成；阶段9任务27实盘引擎接入多券商管理器（BrokerManager统一执行通道+双券商模拟实盘入口）已于2026-08-29完成；阶段9任务28实时行情全链路（A实时行情源+B事件总线联动+C引擎实时驱动+D中文入口脚本）已于2026-08-29完成；项目结构蓝图对齐补全（benchmarks性能基准 + fixtures测试夹具3模块）已于2026-08-28完成；【第3项 策略库扩展】A网格/B动量/C均值回归/D多因子/E注册导出/F统一验证/G文档更新七子项已于2026-08-29全部完成。阶段9累计验证3276项 + 任务28(214) = 3490项，+ benchmarks(20) = 项目总基线3510项全通过，0失败；策略库扩展统一验证66项全通过，0失败，项目总基线更新为3576项；【第4项 回测报告增强】A蒙特卡洛分析+B HTML报告增强+C PDF报告生成+D统一验证与中文入口四子项已于2026-08-29全部完成，统一验证62项全通过，0失败，项目总基线更新为3638项；【第5项 性能优化】A WMA全量向量化+增量O(1) + B SMA增量O(1) + C ATR全量向量化+增量一致性 + D Bollinger/Volatility增量O(1) + E KDJ环形缓冲区+一致性 + F统一验证与中文入口 六子项已于2026-08-29全部完成，统一验证164项全通过，0失败，项目总基线更新为3802项；【第6项 Alembic数据库迁移】A初始化框架+B初始迁移0001+C增量迁移0002+stamp兼容+D中文入口与统一验证 四子项已于2026-08-29全部完成，统一验证57项全通过，0失败，项目总基线更新为3859项。**
> 规则：
> 1. v1实现内容放在阶段0‑8；蓝图中所有`TODO:后续迭代实现`全部放在【阶段9 高级增强迭代】
> 2. 每个阶段输出：阶段目标、状态、关键交付文件、验收标准。

## 阶段0：项目脚手架 & 基础基础设施（core、models、目录结构、配置加载、日志、异常）
- 状态：✅已完成
- 目标：搭建项目目录骨架，pyproject.toml，.gitignore，core基础组件，models基础数据模型
- 交付：core/，models/基础dataclass/pydantic模型，config_loader，logger，settings，exceptions
- 验收：可导入myquant包，配置加载正常，日志输出正常，基础数据模型可实例化校验。

## 阶段1：数据层基础：数据源fetcher、raw数据读写、基础数据管理器
- 状态：✅已完成
- 目标：多数据源适配器fetcher，datasets/raw目录读写，data_access.sources，基础manager骨架
- 交付：myquant/data_access/fetcher.py；sources各个适配器；manager.py基础骨架；scripts/download_data.py
- 验收：能够拉取原始行情写入datasets/raw；可以从raw读取原始bar/tick。

## 阶段2：数据清洗v1【折中档位2】
- 状态：✅已完成 (2026-08-27)
- 目标：实现折中档位2数据清洗v1；raw只读，输出processed；脏数据落bad目录；yaml配置；基础单元测试；不做抽象基类、事件联动等高阶特性。
- 交付：
  1. datasets/raw, datasets/processed, datasets/bad, datasets/features 目录骨架（含.gitkeep）
  2. config/data_clean.example.yaml 清洗规则配置模板
  3. myquant/data_access/cleaner/ 完整子包（7个文件）：
     - __init__.py：导出核心函数
     - base_cleaner.py：TODO占位（阶段9实现抽象基类）
     - bar_cleaner.py：clean_bar_records() 核心函数（单条校验+序列校验+去重+异常检测+排序）
     - tick_cleaner.py：clean_tick_records() 核心函数
     - pipeline.py：run_clean_pipeline() 端到端编排（含脏数据落盘+报告生成）
     - rules_loader.py：load_clean_config() 配置加载（默认配置+Settings+yaml文件三级优先级）
     - quality_reporter.py：generate_quality_report() JSON质量报告
  4. scripts/数据清洗.py 命令行清洗入口脚本（支持--symbol/--data-type/--start-date/--end-date等参数）
  5. tests/test_data_clean.py 单元测试（6个测试类，覆盖bar/tick/pipeline/config/reporter全场景）
  6. tests/fixtures/market_data.py 脏数据fixture（12种场景）
  7. DataManager 新增方法：clean_and_save_symbol()、load_processed_data()、_save_processed_data()
- 验收标准：
  1. ✅ 19项核心功能验证全部通过（tests/_verify_stage2.py）
  2. ✅ 支持OHLC逻辑校验、负价格/负成交量检测、重复时间戳去重、价格跳变/成交量异常标记、缺symbol检测
  3. ✅ Tick支持bid>=ask检测、重复去重、价格跳变检测
  4. ✅ 脏数据自动归档到datasets/bad/，质量报告输出到outputs/
  5. ✅ 配置驱动，支持yaml配置文件和全局Settings
  6. ✅ DataManager一键清洗并保存到processed，可加载processed数据

## 阶段3：indicators 技术指标模块
- 状态：✅已完成
- 目标：MA/EMA、RSI/MACD/KDJ、布林带ATR等指标，无状态函数，输入必须是processed清洗后bar。
- 交付：myquant/indicators/*
- 验收：输入标准Bar列表，输出正确指标值。

## 阶段4：策略基类+注册器，简单示例策略
- 状态：✅已完成
- 目标：策略基类BaseStrategy，注册器registry，ma_strategy示例策略，支持入口点插件注册。
- 交付：myquant/strategies/*
- 验收：可注册策略，能够接收market_event产生交易信号。

## 阶段5：回测引擎、sim_broker模拟撮合、绩效指标、回测报告
- 状态：✅已完成（含processed真实数据链路打通，2026-08-27）
- 目标：BaseEngine回测子类，sim_broker模拟撮合，metrics绩效指标，recorder记录，report报告。
- 交付：myquant/backtest/*；myquant/execution/sim_broker.py；run_回测入口.py入口。
- 验收：
  1. ✅ 回测引擎可以跑通，支持从内存构造bar输入
  2. ✅ **processed真实数据链路已打通（2026-08-27）**：
     - 新增 `myquant/backtest/data_loader.py`：DataProviderBase抽象基类 + HistoricalDataProvider实现，从datasets/processed/加载CSV，严格end_date过滤防未来函数
     - 修改 `myquant/backtest/engine.py`：新增data_provider参数 + from_data_provider()类方法，on_init()自动从provider加载数据，_build_unified_bars()双重日期过滤
     - 修改 `run_回测入口.py`：完整实现配置加载→策略实例化→HistoricalDataProvider→引擎创建→运行→LookaheadGuard校验→报告生成全链路
     - 修复 MACrossStrategy 未来函数bug：根据bar.timestamp截取数据到当前bar，禁止访问未来数据
  3. ✅ 端到端验证通过（模拟数据120交易日，3笔交易，总收益2.56%，夏普0.7155）
  4. ✅ T+1成交时序验证：3笔成交价全部=当日开盘价（含滑点）
  5. ✅ 防未来函数验证：数据最后日期≤end_date，LookaheadGuard.assert_pass()通过
  6. ✅ 实盘无缝对接预留：RealtimeDataProvider TODO（阶段6继承DataProviderBase即可，引擎零修改）

## 阶段6：实盘基础骨架 live模块（不做完整券商对接）
- 状态：✅已完成 (2026-08-27)
- 目标：live模块骨架，data_feed、portfolio、state_store；broker抽象基类与模拟券商。v1复用阶段2的清洗普通函数做流式清洗。
- 交付：
  1. config/live_config.example.yaml 实盘配置模板（券商连接、风控阈值、订阅合约）
  2. myquant/live/brokers/base_broker.py 券商抽象基类（统一接口、回调注册、订单管理）
  3. myquant/live/brokers/paper_broker.py 模拟券商（本地模拟撮合、T+1、涨跌停、滑点、手续费）
  4. myquant/live/data_feed.py 实时行情接入（订阅/重连/分发/清洗，akshare轮询，WebSocket预留）
  5. myquant/live/portfolio.py 实时持仓管理（持仓/市值/资金/盈亏，与broker/data_feed集成）
  6. myquant/live/engine.py 实盘引擎（协调data_feed/strategy/broker/portfolio，T+1撮合，风控检查）
  7. scripts/实盘模拟回测.py 端到端验证入口（真实历史数据+双均线策略+完整链路）
- 验收标准：
  1. ✅ 6个核心模块全部实现，11项引擎验证通过
  2. ✅ 端到端链路验证通过：akshare数据→engine→策略→风控→broker撮合→portfolio更新
  3. ✅ 实盘模拟回测：117个交易日，7个信号，1笔成交，收益率+1.22%，0错误
  4. ✅ T+1成交时序正确，涨跌停/滑点/手续费模拟正确
  5. ✅ 严格禁用未来函数：时间戳校验+T+1成交
  6. ✅ 为实盘无缝对接预留：WebSocket推送、多策略、多账户、券商回调全部预留接口

## 阶段7：风控risk、资金分配allocation模块基础实现
- 状态：✅已完成 (2026-08-27)
- 目标：risk_manager基础规则；allocation等权、风险平价基础实现；v1风控不和清洗事件联动。
- 交付：
  **【风控risk部分 ✅已完成】**
  1. myquant/risk/__init__.py 模块导出（10类规则+管理器+守护）
  2. myquant/risk/rules.py 风控规则库（10类核心规则+规则库统一管理）
     - PositionLimitRule 仓位限制（单票最大仓位/最大持仓数/单票最大股数）
     - SingleOrderLimitRule 单笔限额（防误操作）
     - MaxDrawdownRule 最大回撤（超阈值禁止新开仓）
     - StopLossRule 个股止损（亏损超阈值禁止加仓）
     - TakeProfitRule 个股止盈（盈利超阈值禁止加仓，防追高）
     - RateLimitRule 流控（每日最大下单数/最小间隔）
     - PriceLimitRule 涨跌停（涨停不买/跌停不卖）
     - TradingHoursRule 交易时段（A股时段外禁止下单）
     - BlacklistRule 黑名单（禁止交易指定标的，可动态添加）
     - CashAvailabilityRule 资金可用性（买入金额+手续费+保留金≤可用资金）
     - RiskRuleLibrary 规则库（添加/删除/开关/批量检查/配置加载）
  3. myquant/risk/risk_manager.py 风控管理器（与实盘引擎零修改集成）
     - check_order() 订单前置检查（PAUSED直接拒买+规则库批量检查+事件记录）
     - check_positions() 持仓监控（止损/止盈/回撤警告，v1不自动平仓）
     - 三级风控状态 NORMAL/WARNING/PAUSED
     - 回撤自动暂停（auto_pause_on_drawdown）
     - 风控事件审计（ORDER_REJECTED/POSITION_WARNING/STATUS_CHANGE）
     - 规则管理（委托给RiskRuleLibrary）
     - get_summary() 风控摘要
  4. myquant/risk/guardian.py 风控守护（v1简化骨架）
     - check_health() 健康检查（风控状态/账户资金/持仓数据/连续拒绝/最近事件，6项检查）
     - evaluate() 守护决策（强制平仓>熔断暂停>PAUSED告警>WARNING告警>DEGRADED告警>无操作）
     - force_close_all()/force_close_symbol() 强制平仓（v1只记录，set_force_close_callback对接broker）
     - alert() 风控告警（INFO/WARNING/CRITICAL三级，v1记录日志）
     - 连续拒绝计数（熔断机制，record_rejection/record_acceptance）
     - 三级健康状态 HEALTHY/DEGRADED/UNHEALTHY
     - get_summary() 守护摘要
  5. tests/_verify_risk_rules.py 规则库验证（15项全通过）
  6. tests/_verify_risk_manager.py 管理器验证（13项全通过）
  7. tests/_verify_risk_guardian.py 守护验证（16项全通过）
  - 风控验收标准：
    1. ✅ 10类风控规则全部实现，15项规则库验证通过
    2. ✅ 风控管理器与实盘引擎零修改集成，13项验证通过
    3. ✅ 风控守护v1骨架完整，16项验证通过
    4. ✅ 三级风控状态（NORMAL/WARNING/PAUSED）+ 三级健康状态（HEALTHY/DEGRADED/UNHEALTHY）
    5. ✅ 熔断机制（连续拒绝超阈值自动暂停）
    6. ✅ 回撤自动暂停（超阈值禁止新开仓）
    7. ✅ 严格禁用未来函数（只使用当前已知的账户/持仓/行情数据）
    8. ✅ 为实盘无缝对接预留：强制平仓回调、多渠道告警、守护循环、DataQualityEvent联动、动态阈值、多账户隔离

  **【资金分配allocation部分 ✅已完成】**
  1. myquant/allocation/__init__.py 模块导出（基类+等权+风险平价+目标波动率）
  2. myquant/allocation/base.py 分配器抽象基类
     - AllocationResult 分配结果数据类（权重/金额/校验/summary）
     - AllocationConstraints 分配约束（全局/单独上下限、排除资产）
     - AllocatorBase 抽象基类（allocate抽象方法、validate校验、normalize_weights归一化、apply_constraints应用约束、rebalance再平衡）
  3. myquant/allocation/equal_weight.py 等权分配器
     - 每个资产权重=1/N，自动应用约束，结果校验
     - 空资产/单资产边界处理
  4. myquant/allocation/risk_parity.py 风险平价分配器（两种模式）
     - inverse_volatility：逆波动率加权 w_i=(1/σ_i)/Σ(1/σ_j)
     - risk_parity：完整迭代优化（考虑相关性，使风险贡献相等，342次迭代收敛）
     - 回退机制：无协方差→逆波动率，无波动率→等权
     - 风险贡献计算（metadata输出）
  5. myquant/allocation/target_volatility.py 目标波动率分配器（TODO占位，回退等权）
  6. tests/_verify_allocation_base.py 基类验证（11项全通过）
  7. tests/_verify_equal_weight.py 等权验证（12项全通过）
  8. tests/_verify_risk_parity.py 风险平价验证（14项全通过）
  9. tests/_verify_allocation_integration.py 完整集成验证（59项全通过）
  - 资金分配验收标准：
    1. ✅ 抽象基类完整（AllocationResult/AllocationConstraints/AllocatorBase），11项验证通过
    2. ✅ 等权分配器，12项验证通过
    3. ✅ 风险平价分配器（逆波动率+完整迭代），14项验证通过
    4. ✅ 完整集成测试59项全通过
    5. ✅ 约束条件支持（单资产上下限、排除资产）
    6. ✅ 再平衡接口（rebalance计算调仓量，阈值过滤）
    7. ✅ 回退机制（无协方差→逆波动率，无波动率→等权）
    8. ✅ 严格禁用未来函数（只使用当前已知的历史波动率/协方差）
    9. ✅ 为实盘无缝对接预留：动态波动率更新、协方差矩阵支持、目标波动率、风险预算、因子风险平价、动态再平衡调度

## 阶段8：GUI / Web / API 可选组件基础
- 状态：✅已完成 (2026-08-27)
- 目标：GUI基础窗口；web dashboard基础页面；api基础路由骨架。v1实现Web + API，GUI已有基础模块。
- 交付：
  **【Web模块 ✅已完成】**
  1. myquant/web/__init__.py 模块导出
  2. myquant/web/dashboard.py Streamlit Web看板（7个页面 + 核心模块注入接口）
     - 🏠 首页：系统概览 + 模块状态卡片 + 快速入口
     - 📊 策略：策略列表 + 参数配置（占位）
     - 📈 回测：回测配置表单 + 绩效指标 + 净值曲线（占位）
     - 💹 实盘：连接状态 + 账户概览 + 持仓列表 + 手动下单（占位）
     - 🛡️ 风控：风控状态 + 10类规则开关 + 事件日志（占位）
     - 💾 数据：数据源管理 + 数据质量报告 + 数据操作（占位）
     - ℹ️ 关于：已完成模块 + TODO + 技术栈
     - 5个核心模块注入接口（set_strategy_manager/set_backtest_engine/set_live_engine/set_risk_manager/set_data_manager）
     - Streamlit未安装友好提示
  3. tests/_verify_web.py Web验证（43项全通过）

  **【API模块 ✅已完成】**
  1. myquant/api/__init__.py 模块导出
  2. myquant/api/routes.py 路由聚合（15+ API端点 + create_app + run_api）
     - 系统：GET /api/health
     - 策略：GET /api/strategies, GET /api/strategies/{name}
     - 回测：POST /api/backtest/run, GET /api/backtest/{id}
     - 实盘：GET /api/live/status, POST /api/live/start, POST /api/live/stop, GET /api/live/positions
     - 风控：GET /api/risk/status, POST /api/risk/pause, POST /api/risk/resume
     - 数据：GET /api/data/quality, POST /api/data/clean
     - 订单：POST /api/order, POST /api/order/{id}/cancel
     - 资金分配：POST /api/allocation/calculate
     - CORS配置 + 全局异常处理 + FastAPI未安装降级
  3. myquant/api/middleware/auth.py API Key认证（Header/Query/常量时间比较/多Key管理）
  4. myquant/api/middleware/rate_limit.py 限流（固定窗口 + 令牌桶）
  5. myquant/api/models/schemas.py 14个Pydantic响应模型（通用/健康/策略/回测/实盘/风控/数据质量/订单）
  6. myquant/api/dependencies/get_services.py 服务容器（单例）+ 8个依赖注入函数
  7. scripts/运行API.py API服务入口脚本（中文名，支持--host/--port/--reload/--workers等参数）
  8. tests/_verify_api.py API验证（67项全通过）

  **【阶段8完整集成验证 ✅已完成】**
  9. tests/_verify_stage8_integration.py 完整集成验证（79项全通过）
     - 9个核心模块全部导入
     - 服务容器注册8个核心服务
     - Web模块与核心服务集成
     - API模块与核心服务集成
     - Web + API双入口协同（共享服务容器）
     - 阶段8完整性检查（15个文件全部存在）
     - 全模块防未来函数验证
     - 入口脚本参数解析验证
- 验收标准：
  1. ✅ Web模块43项验证全通过（7个页面 + 5个模块注入接口）
  2. ✅ API模块67项验证全通过（15+端点 + 认证 + 限流 + 14个模型 + 服务容器）
  3. ✅ 阶段8完整集成验证79项全通过
  4. ✅ 入口脚本中文名：scripts/运行API.py，支持--host/--port/--reload/--workers等参数
  5. ✅ Streamlit/FastAPI未安装时友好降级，不影响其他模块
  6. ✅ 严格禁用未来函数（Web/API全模块无预测方法/属性）
  7. ✅ 为实盘无缝对接预留：实盘控制API、订单API、风控API、数据API、WebSocket实时行情、JWT/OAuth2认证、分布式限流、策略参数动态配置、用户管理、系统监控
- 运行方式：
  - Web看板：pip install streamlit && streamlit run run_web.py
  - API服务：pip install fastapi uvicorn && python scripts/运行API.py
  - API文档：http://localhost:8000/docs (Swagger UI)

## 阶段9：高级增强迭代（蓝图中全部TODO项）
- 状态：✅已完成 (2026-08-27 开始，任务1-25全部完成，2026-08-28)
- 目标：将v1中标记为TODO的高阶特性全部实现，包括面向对象清洗器、规则对象化、事件总线联动、数据集版本管理、监控告警、数据质量探查页面、ML模块等。
- 任务清单（25项，分7组）：
  **【第一组：数据清洗面向对象重构 ✅任务1-5已完成】**
  - ✅ 任务1：cleaner/base_cleaner.py — 清洗器抽象基类（CleanerBase + CleanRule + CleanResult + CleanerRegistry），91项验证通过
  - ✅ 任务2：cleaner/bar_cleaner.py — 重构为BarCleaner类，继承CleanerBase，兼容旧函数，83项验证通过，新旧结果100%一致
  - ✅ 任务3：cleaner/tick_cleaner.py — 重构为TickCleaner类，继承CleanerBase，兼容旧函数，82项验证通过，新旧结果100%一致
  - ✅ 任务4：cleaner/rules_loader.py — 规则对象映射（CleanRuleLibrary + build_bar_rules + build_tick_rules + 全局单例+热重载），104项验证通过
  - ✅ 任务5：cleaner/pipeline.py — 完整流水线编排对象（CleanPipeline类 + PipelineStep + PipelineResult，链式调用+事件钩子+自定义步骤+中间结果缓存），131项验证通过

  **【第二组：事件总线 + 监控告警联动 ✅任务6-12已完成】**
  - ✅ 任务6：core/constants.py — 数据清洗枚举（DataType + DirtyDataType(14种) + RepairStrategy(5种) + QualityGrade(5级) + CleanStep(8步) + SeverityLevel(4级) + 4个映射表），168项验证通过
  - ✅ 任务7：core/exceptions.py — 数据清洗异常（DataQualityError + DataQualityCriticalError + DataQualityWarning + CleaningPipelineError + DataRepairError），138项验证通过
  - ✅ 任务8：core/event_bus.py — DataQualityEvent事件定义 + 发布订阅增强（3个强类型事件类 + EventFilter过滤器 + HandlerStats统计 + 告警去重 + wait_for_event + DataQualityEventPublisher发布器），243项验证通过
  - ✅ 任务9：monitoring/ 模块骨架 — health_check.py（HealthChecker+HealthStatus+内置检查项）+ alerting.py（AlertManager+AlertChannel多通道+分级去重+事件总线订阅预留）+ metrics_collector.py（MetricsCollector+Prometheus导出+数据质量指标预留），219项验证通过
  - ✅ 任务10：monitoring/alerting.py — 接收DataQualityEvent自动告警（AlertRule规则配置+AlertAggregator流式聚合+Email/Webhook实际发送+智能分级+告警升级+发送回调），195项验证通过
  - ✅ 任务11：monitoring/metrics_collector.py — 采集脏数据占比/清洗质量指标（collect_from_data_quality_event实时采集+collect_from_pipeline_event+collect_from_pipeline_result增强+create_alert_callback告警回调+subscribe_to_event_bus自动订阅+get_stale_metrics过期检测+Prometheus导出增强），129项验证通过
  - ✅ 任务12：risk/risk_manager.py — 对接DataQualityEvent，脏数据严重时自动暂停策略（A→E全链路：事件发布+AlertManager/MetricsCollector联动+数据质量恢复自动恢复策略，268项验证通过，2026-08-28）

  **【第三组：数据集版本元管理 + 数据库入库 ✅任务13-14已完成】**
  - ✅ 任务13：database/schema.py — 清洗元信息表（clean_run_meta）+ 脏数据统计表（dirty_data_stats）DDL + CleanDataQueries 完整 CRUD（queries.py），142项验证通过（2026-08-28）
  - ✅ 任务14：data_access/manager.py — 数据集完整版本管理（版本号/元数据/快照/回滚），74项验证通过（2026-08-28）

  **【第四组：Web/API数据质量增强 ✅任务15-17已完成】**
  - ✅ 任务15：web/dashboard.py — 数据质量探查页面（DataQualityExplorer 数据逻辑类 + 独立「🧹 数据质量」页面），65项验证通过（2026-08-28）
  - ✅ 任务16：api/routes.py — 数据质量查询接口（含/metrics Prometheus端点 + /alerts告警端点），83项验证通过（2026-08-28）
  - ✅ 任务17：api/models/schemas.py — 数据质量详细Pydantic响应schema，80项验证通过（2026-08-28）

  **【第五组：ML模块完整MLOps链路 ✅任务18-21已完成】**
  - ✅ 任务18：ml/__init__.py + ml/features.py — 特征工程（FeatureConfig训练/推理共用配置 + FeatureEngine引擎 + FeatureDataset数据集，71项验证通过（2026-08-28））
  - ✅ 任务19：ml/model_registry.py — 模型注册版本管理（版本号/元数据/快照/回滚/active热切换/checksum校验，74项验证通过（2026-08-28））
  - ✅ 任务20：ml/training_pipeline.py — 训练流水线（内置numpy岭回归/逻辑回归 + 可插拔model_factory + time_series_split时间序列切分strict_split防标签重叠 + 评估指标numpy手写 + 注册闭环），99项验证通过（2026-08-28）
  - ✅ 任务21：ml/inference_service.py — 批量/实时预测服务（批量/实时预测 + 特征对齐校验 + train_end_time守卫防未来 + 注册闭环），63项验证通过（2026-08-28）

  **【第六组：实盘增强 ✅任务22-23已完成】**
  - ✅ 任务22：live/scheduler.py — 定时任务/巡检/日报（CronExpression 5字段cron解析 + ScheduledTask/LiveScheduler后台守护线程调度 + HealthPatrol健康巡检 + DailyReport每日报告Markdown/JSON落盘 + attach_engine实盘引擎便捷挂载），127项验证通过（2026-08-28）
  - ✅ 任务23：live/state_store.py — 状态持久化/崩溃恢复（StateStore版本管理+原子写+checksum校验+保留策略 + StateSnapshot快照 + capture_live_state采集 + restore_live_state持仓/资金重建 + attach_snapshot定时快照与任务22调度器联动），88项验证通过（2026-08-28）

  **【第七组：测试补充 ✅任务24-25已完成】**
  - ✅ 任务24：tests/test_data_clean.py — 补充全部极端边界测试case（92项测试，含55项极端边界；过程中修复rules_loader.py浅拷贝污染全局默认配置的真实缺陷）
  - ✅ 任务25：阶段9完整集成验证脚本（tests/数据清洗集成验证.py，8条集成链路62项断言全通过；覆盖清洗→事件→风控→监控→数据库→ML→实盘→防未来全链路）

  **【第八组：双券商并行 + 实时行情全链路 ✅任务26-28已完成】**
  - ✅ 任务26：双券商并行三件套（brokers/manager.py BrokerManager多券商管理器 + qmt_broker.py 迅投QMT适配器 + ptrade_broker.py 恒生Ptrade适配器），224项验证通过（2026-08-29）
  - ✅ 任务27：live/engine.py 接入 BrokerManager 多券商管理器 + scripts/双券商模拟实盘.py 入口，81项验证通过（2026-08-29）
  - ✅ 任务28：实时行情全链路（A.data_feed.py 实时行情源腾讯主+akshare备+mock离线 / B.事件总线联动窗口聚合+连续阈值升级 / C.engine.py 实时自动轮询+频率自适应+防未来+单调+预热 / D.scripts/实时行情驱动.py 中文入口），214项验证通过（2026-08-29）

---
# 当前下一步行动
1. ✅ 两份文档已保存到 D:\Projects\Pythonwdhmm\docs\
2. ✅ 阶段2（数据清洗v1折中档位2）已开发完成，19项核心验证通过
3. ✅ 阶段5回测引擎读取processed真实数据链路已打通（2026-08-27）
   - 新增 backtest/data_loader.py（DataProviderBase + HistoricalDataProvider）
   - 引擎接入 from_data_provider()，自动加载processed数据
   - run_回测入口.py 完整实现全链路
   - 端到端验证通过：3笔交易，T+1时序正确，防未来函数校验通过
4. ✅ 阶段6（实盘基础骨架 live模块）已完成（2026-08-27）
   - 6个核心模块全部实现，端到端链路验证通过
   - 实盘模拟回测：117个交易日，7个信号，1笔成交，收益率+1.22%
5. ✅ 阶段7风控risk部分已完成（2026-08-27）
   - rules.py：10类风控规则+规则库，15项验证通过
   - risk_manager.py：风控管理器，13项验证通过
   - guardian.py：风控守护v1骨架，16项验证通过
   - 三级风控状态+三级健康状态+熔断机制+回撤自动暂停
6. ✅ 阶段7资金分配allocation部分已完成（2026-08-27）
   - base.py：分配器抽象基类+分配结果+约束条件，11项验证通过
   - equal_weight.py：等权分配器，12项验证通过
   - risk_parity.py：风险平价分配器（逆波动率+完整迭代），14项验证通过
   - target_volatility.py：目标波动率TODO占位（回退等权）
   - 完整集成测试59项全通过
7. ✅ 阶段8（GUI/Web/API可选组件基础）已完成（2026-08-27）
   - Web模块：dashboard.py Streamlit看板（7个页面 + 5个模块注入接口），43项验证通过
   - API模块：routes.py 15+端点 + auth认证 + rate_limit限流 + 14个Pydantic模型 + 服务容器，67项验证通过
   - 入口脚本：scripts/运行API.py（中文名，支持--host/--port/--reload等参数）
   - 阶段8完整集成验证79项全通过
8. ✅ 阶段9任务1-7已完成（2026-08-27，交接点）
   - 第一组（数据清洗面向对象重构）：
     - 任务1：base_cleaner.py 抽象基类（CleanerBase+CleanRule+CleanResult+CleanerRegistry），91项验证通过
     - 任务2：bar_cleaner.py 重构为BarCleaner类，83项验证通过，新旧结果100%一致
     - 任务3：tick_cleaner.py 重构为TickCleaner类，82项验证通过，新旧结果100%一致
     - 任务4：rules_loader.py 规则对象化（CleanRuleLibrary+全局单例+热重载），104项验证通过
     - 任务5：pipeline.py 完整流水线编排对象（CleanPipeline+PipelineStep+PipelineResult），131项验证通过
   - 第二组（事件总线+监控告警联动）基础设施：
     - 任务6：constants.py 数据清洗枚举（6个枚举类+4个映射表），168项验证通过
     - 任务7：exceptions.py 数据清洗异常（5个异常类），138项验证通过
   - 阶段9任务1-7累计验证：797项全通过，0失败
9. ✅ 阶段9任务8已完成（2026-08-27，event_bus.py 数据质量事件 + 发布订阅增强）
   - 3个强类型事件类：DataQualityEvent（脏数据事件）+ PipelineCompleteEvent（流水线完成事件）+ QualityAlertEvent（质量告警事件）
   - EventType 新增4个数据质量事件类型：DATA_QUALITY_DIRTY / PIPELINE_COMPLETE / ALERT / CRITICAL
   - EventFilter 事件过滤器：按 symbol / data_type / severity / event_type 过滤（AND关系）
   - HandlerStats 处理器统计：调用次数 / 错误次数 / 耗时 / 最后错误 / 错误率
   - 告警去重：时间窗口内相同 deduplication_key 的告警只发布一次
   - 类型化发布方法：publish_data_quality / publish_pipeline_complete / publish_quality_alert / publish_critical
   - 便捷订阅方法：subscribe_data_quality / subscribe_pipeline_complete / subscribe_quality_alert
   - wait_for_event 阻塞等待事件（支持超时和过滤器）
   - DataQualityEventPublisher 便捷发布器（封装EventBus，供清洗流水线/实盘数据流调用）
   - 243项验证全通过，0失败
   - 阶段9累计验证：1040项全通过，0失败
10. ✅ 阶段9任务9已完成（2026-08-27，monitoring/ 模块骨架）
    - health_check.py：HealthStatus枚举 + HealthCheckResult + HealthChecker管理器 + 内置检查项（磁盘空间/事件总线/进程存活）+ 关键检查项失败直接UNHEALTHY + 全局单例
    - alerting.py：Alert数据类 + AlertChannel抽象基类 + ConsoleAlertChannel/LogAlertChannel实现 + EmailAlertChannel/WebhookAlertChannel预留骨架 + AlertManager管理器（分级路由/去重/多通道/静默模式/告警历史/确认解决）+ subscribe_to_event_bus()事件总线订阅预留（任务10完善）+ handle_data_quality_event/handle_quality_alert_event/handle_pipeline_complete_event处理方法
    - metrics_collector.py：MetricType枚举（COUNTER/GAUGE/HISTOGRAM）+ Metric数据类 + BuiltinMetrics常量（事件总线/数据质量/系统/实盘指标）+ MetricsCollector采集器（注册/set_gauge/inc_counter/observe_histogram/get/to_prometheus/to_dict）+ collect_from_event_bus/collect_data_quality/collect_from_pipeline_result/collect_from_health_checker/collect_from_alert_manager采集方法 + 全局单例
    - 219项验证全通过，0失败
    - 阶段9累计验证：1259项全通过，0失败
11. ✅ 阶段9任务10已完成（2026-08-27，monitoring/alerting.py 接收DataQualityEvent自动告警）
    - AlertRule 告警规则配置：脏数据类型→级别映射（14种）+ 占比阈值→级别（3档）+ 级别→通道映射 + 聚合窗口 + 升级时间 + min_bad_count + default_rule()工厂方法
    - AlertAggregator 流式聚合器：同一标的+脏数据类型在窗口内聚合，支持add_event/flush/cleanup_expired，聚合桶含count/total_bad/total_total/max_level/errors
    - EmailAlertChannel 实际SMTP发送：smtplib + HTML/纯文本模板 + TLS/SSL + 收件人列表 + 主题模板 + 超时配置
    - WebhookAlertChannel 实际HTTP发送：urllib + 飞书/钉钉/企业微信/通用4种payload + 自定义headers/template + 重试机制（指数退避）+ 超时
    - AlertManager 增强：智能分级（determine_level取最高）+ 通道路由（_route_channels按规则）+ 告警聚合（handle_data_quality_event→aggregator→_send_aggregated_alert）+ 告警升级（check_escalations，WARNING超时未确认→ERROR）+ 发送回调（on_alert_callbacks，metrics联动）+ flush_aggregator强制刷新
    - handle_data_quality_event()：should_alert判断→determine_level分级→aggregator聚合→过期桶发送聚合告警
    - handle_pipeline_complete_event()：先flush聚合器→失败→ERROR→质量等级分级（critical→CRITICAL/poor→ERROR/fair→WARNING/good→不告警）
    - handle_quality_alert_event()：直接转发为告警（按alert_level映射）
    - 195项验证全通过，0失败
    - 向后兼容：任务9 monitoring验证219项继续全通过
    - 阶段9累计验证：1454项全通过，0失败
12. ✅ 阶段9任务11已完成（2026-08-27，monitoring/metrics_collector.py 采集脏数据占比/清洗质量指标）
    - collect_from_data_quality_event() 实时事件采集：从DataQualityEvent提取dirty_type/symbol/data_type/bad_count/total_count，更新dirty_count/dirty_type_count/total_records/dirty_ratio，流式支持实盘逐条更新
    - collect_from_pipeline_event() 流水线事件采集：从PipelineCompleteEvent提取完整统计，更新pipeline_runs/pipeline_failures/数据质量全套指标
    - collect_from_pipeline_result() 增强：支持多种结果对象结构（PipelineResult/CleanPipelineResult），自动提取stats/step_results/quality_report/repair_results，失败状态自动采集pipeline_failures
    - collect_data_quality() 增强：新增repair_counts参数（修复策略计数）、步骤名规范化（Validate→validate, Detect Anomalies→detect_anomalies）、质量等级映射使用BuiltinMetrics.QUALITY_GRADE_MAP、空标签不添加
    - collect_from_alert_manager() 增强：按级别+分类统计（而非仅按级别）、采集升级告警数、从告警历史聚合level_category_counts
    - create_alert_callback() 告警回调：创建AlertManager回调函数，告警发送时自动更新system_alerts_total（按level+category标签），升级告警额外更新system_alerts_escalated
    - subscribe_to_event_bus() 事件总线自动订阅：订阅DATA_QUALITY_DIRTY→collect_from_data_quality_event，DATA_QUALITY_PIPELINE_COMPLETE→collect_from_pipeline_event，无需手动调用collect方法
    - 新增内置指标：data_quality_total_records/data_quality_repair_count/data_quality_pipeline_runs/data_quality_pipeline_failures/system_alerts_escalated（共5个）
    - get_stale_metrics() 指标过期检测：长时间未更新的指标标记为stale，用于实盘断连检测
    - Metric新属性：age_seconds/is_stale()/to_dict含age_seconds
    - to_prometheus() 增强：可选时间戳（include_timestamp）、标签值转义（双引号/反斜杠/换行符）、NaN/Inf处理（NaN/+Inf/-Inf）、空指标跳过
    - 129项验证全通过，0失败
    - 向后兼容：任务9 monitoring验证219项 + 任务10 alerting验证195项继续全通过
    - 阶段9累计验证：1583项全通过，0失败
13. ✅ 阶段9任务12已完成（2026-08-28，risk/risk_manager.py 对接DataQualityEvent，脏数据严重时自动暂停策略）
    - A项：修复验证脚本 `tests/_verify_risk_dq.py`（重写make_context匹配真实RiskContext、补全测试12/13、新增汇总输出）→ 179项通过
    - B项：发布策略暂停/恢复事件（EventType新增RISK_PAUSED/RISK_RESUMED + StrategyPauseEvent事件类 + risk_manager._publish_pause_event()，暂停/恢复/重置均发布事件）→ 207项通过
    - C项：AlertManager联动（STRATEGY_PAUSE_ALERT_LEVEL_MAP全局暂停→CRITICAL/单标的→ERROR/恢复→INFO + handle_strategy_pause_event() + 订阅2个事件，3→5 handler）→ 225项通过
    - D项：MetricsCollector联动（RISK_PAUSE_TOTAL/RISK_RESUME_TOTAL/RISK_PAUSED指标 + collect_from_strategy_pause_event()全局暂停symbol归一为ALL + 订阅2个事件，2→4 handler）→ 243项通过
    - E项：数据质量恢复后自动恢复策略（可配置恢复阈值：auto_resume_enabled/resume_quality_grades/resume_bad_ratio_threshold/consecutive_good_required + _check_auto_resume()仅基于已发生事件判定 + 恢复时联动发布事件）→ 268项通过
    - 任务12全量验证：`tests/_verify_risk_dq.py` **268项通过，0失败**；risk_manager/risk_rules/event_bus/monitoring/alerting/metrics 全部回归通过
    - 运行环境：`E:\ProgramData\anaconda3\envs\quant\python.exe`（依赖已恢复完整，24项全就绪）
14. ✅ 阶段9任务13已完成（2026-08-28，database/schema.py 清洗元信息表 + 脏数据统计表DDL）
    - schema.py 新增两表DDL：
      - CleanRunMetaSchema（clean_run_meta 表）：run_id/data_type/symbol/status/total/valid/bad/valid_rate/quality_grade/severity/steps_executed/step_durations/total_duration/bad_file/report_file/processed_file/source/started_at/finished_at/error，与 PipelineResult/PipelineCompleteEvent 结构对应
      - DirtyDataStatsSchema（dirty_data_stats 表）：run_id/symbol/data_type/dirty_type/severity/bad_count/repair_strategy/sample_data，与 DirtyDataType 对应
      - 状态常量（STATUS_RUNNING/COMPLETED/FAILED + SOURCE_OFFLINE/LIVE）+ 外键关联 + 建表/建索引/删表/表检查/计数工具
    - queries.py 新增 CleanDataQueries 完整 CRUD：insert_clean_run/update_clean_run_status/get_clean_run/get_clean_runs + insert_dirty_data_stats/get_dirty_stats/get_dirty_stats_summary 等
    - 任务13全量验证：`tests/_verify_database_schema.py` **142项通过，0失败**
    - 为实盘对接：source 字段区分 offline/live；processed_file 衔接数据集版本管理（任务14可直接从清洗运行建版本）
15. ✅ 阶段9任务14已完成（2026-08-28，data_access/manager.py 数据集完整版本管理：版本号/元数据/快照/回滚）
    - 新增 DatasetVersion 版本元数据结构 + 3个版本异常类（DatasetVersionError/NotFoundError/IntegrityError）
    - 存储布局：datasets/processed/{symbol}/versions/{index.json + v{n}/data.csv + metadata.json}，与阶段2时间戳文件完全兼容
    - 新增12个方法：create_dataset_version（自动递增版本号+快照+checksum+索引双写）/ create_dataset_version_from_clean_run（衔接任务13 clean_run_meta）/ list/get/get_latest / load_dataset_version（checksum校验+end_date严格过滤防未来函数）/ rollback_dataset_version（写回processed当前文件+index记录回滚事件）/ delete（head自动回退）/ verify / get_dataset_versions_metadata
    - 并发安全：线程锁+跨进程文件锁（实盘多进程预留）；source区分offline/live；parent_version派生链
    - 任务14全量验证：`tests/_verify_dataset_version.py` **74项通过，0失败**；任务12(268)/任务13(142)/stage8(79) 全部回归通过
    - 阶段9累计验证：1583 + 任务12(268) + 任务13(142) + 任务14(74) = **2067项全通过，0失败**
16. ✅ 阶段9任务15已完成（2026-08-28，web/dashboard.py 数据质量探查页面）
    - 新增 DataQualityExplorer 数据逻辑类（纯Python、不依赖streamlit、可独立测试）：
      - get_clean_runs/count_clean_runs/get_run_detail/get_latest_run：清洗运行查询（复用任务13 CleanDataQueries）
      - get_grade_distribution：质量等级分布（中文描述+占比，复用 QualityGrade 枚举）
      - get_dirty_stats/get_dirty_summary：脏数据统计（中文描述+severity，复用 DirtyDataType/DIRTY_TYPE_DESC_MAP）
      - get_metrics：实时质量指标（复用任务11 MetricsCollector 8个指标）
      - get_overview：总览（一次调用全量渲染数据）
    - Dashboard 新增独立「🧹 数据质量」页面（PAGES 7→8）+ 注入接口 set_data_quality_explorer/set_database_connection（未注入自动fallback全局单例）
    - 页面5区块：实时质量指标→清洗运行总览（过滤）→质量等级分布→脏数据统计→运行详情
    - 任务15全量验证：`tests/_verify_web_data_quality.py` **65项通过，0失败**；`_verify_web.py`(46)/`_verify_stage8_integration.py`(79)/`_verify_database_schema.py`(142) 全部回归通过
    - 阶段9累计验证：2067 + 任务15(65) = **2132项全通过，0失败**
    - 为实盘对接：source过滤区分offline/live、实时指标展示实盘数据质量、get_latest_run支撑断点续跑监控
17. ✅ 阶段9任务16已完成（2026-08-28，api/routes.py 数据质量查询接口）
    - 新增9个端点（create_app 新增 data_quality_explorer/metrics_collector/alert_manager 注入参数，注入优先、懒加载全局单例）：
      - GET /api/data/quality/overview：数据质量总览（实时指标+运行总数+最近运行+等级分布+脏数据统计）
      - GET /api/data/quality/runs：清洗运行列表（symbol/data_type/status/source/limit 过滤，按 started_at 倒序）
      - GET /api/data/quality/runs/{run_id}：运行详情（不存在→404）
      - GET /api/data/quality/dirty：脏数据统计（汇总+中文描述明细，include_detail 开关）
      - GET /api/data/quality/metrics：实时质量指标（MetricsCollector 8指标）
      - GET /api/data/quality：v1占位→真实总览（DataQualityResponse 兼容结构，quality_grade 数字→名称映射）
      - GET /api/metrics：Prometheus 文本导出（text/plain，供 Prometheus/Grafana 抓取）
      - GET /api/alerts：告警列表（scope=all/unacknowledged/escalated + level/category 过滤 + stats 统计）
      - POST /api/alerts/{alert_id}/ack + /resolve：告警确认/解决（不存在→404，运维闭环）
    - 修复真实缺陷：DataQualityExplorer.get_metrics() 只读无标签指标，但真实 pipeline 用 collect_data_quality 写入带 symbol/data_type 标签指标（实盘常态）→ 恒读 None。已做向后兼容增强（无标签读不到时回退最新带标签数据点；无标签场景原逻辑不变；空数据仍返回 None），任务15的65项验证零破坏
    - 防未来函数：全部端点只返回已发生数据；/api/metrics 只导出已采集指标；/api/alerts 只返回历史告警；清洗运行 end_time 过滤由任务13保证
    - 为实盘对接：source=live 过滤、/api/metrics 供 Grafana 抓取、/api/alerts 运维确认闭环
    - 任务16全量验证：`tests/_verify_api_data_quality.py` **83项通过，0失败**；`_verify_web_data_quality.py`(65)/`_verify_web.py`(46)/`_verify_api.py`(67)/`_verify_stage8_integration.py`(79) 全部回归通过
    - 阶段9累计验证：2132 + 任务16(83) = **2215项全通过，0失败**
18. ✅ 阶段9任务17已完成（2026-08-28，api/models/schemas.py 数据质量详细Pydantic响应schema）
    - ApiResponse 泛型化：`ApiResponse[T]`（data: Optional[T]），无参用法向后兼容阶段8全部 response_model=ApiResponse
    - 新增13个数据质量/监控详细模型（全部对齐任务16端点响应结构）：
      - QualityMetricsInfo 实时质量指标（quality_grade 支持数值1.0/字符串双形态）
      - CleanRunInfo / CleanRunsData 清洗运行信息/列表（与 clean_run_meta 全字段对齐，steps 反序列化）
      - GradeDistributionItem / DirtyStatInfo / DirtySummaryInfo / DirtyData 等级分布/脏数据明细/汇总（含中文描述）
      - QualityOverviewData 总览5大块（metrics/total_runs/latest_run/grade_distribution/dirty_summary）
      - AlertInfo / AlertSummaryInfo / AlertListData / AlertAckData / AlertResolveData 告警模型（与 Alert.to_dict / AlertManager.stats 逐字段对齐）
    - routes.py 接入：9个端点全部加 `response_model=ApiResponse[XXX]` + `response_model_exclude_none=True`
    - 兼容性保障：所有模型 `model_config={"extra":"allow"}` 保留额外字段；response_model_exclude_none 排除None字段（避免破坏任务16"dirty?include_detail=false 无 details"断言）
    - 防未来函数：所有模型字段只承载已发生事实（清洗/脏数据/告警），无预测方法/字段
    - 为实盘对接：source 区分offline/live、告警summary承载实盘统计、泛型响应为后续实盘/行情模型预留统一包装
    - 任务17全量验证：`tests/_verify_api_data_quality_schemas.py` **80项通过，0失败**；任务16(83)硬门槛 + `_verify_api.py`(67)/`_verify_web_data_quality.py`(65)/`_verify_web.py`(46)/`_verify_stage8_integration.py`(79)/`_verify_database_schema.py`(142) 全部回归通过
    - 阶段9累计验证：2215 + 任务17(80) = **2295项全通过，0失败**
19. ✅ 阶段9任务18已完成（2026-08-28，ml/__init__.py + ml/features.py 特征工程，ML模块完整MLOps链路第一环）
    - 新增 FeatureConfig 特征配置（训练/推理共用，JSON可序列化 to_dict/from_dict/to_json/from_json，杜绝训练-推理偏移；max_lookback 最大回看窗口）
    - 新增 FeatureEngine 特征引擎：
      - compute_features() 批量特征计算（40个特征列，严格因果 trailing 滚动窗口，复用 indicators 10项技术指标 + 价格衍生 + 派生特征）
      - generate_labels() 标签生成（forward_return 未来N期收益 + direction 涨跌二分类，仅训练集使用）
      - build_dataset() 特征+标签对齐为 FeatureDataset（丢弃无标签尾部，X/y/timestamps JSON安全）
      - compute_incremental() 实盘增量特征（默认全量从起点计算，与训练最后一行逐列精确一致；修正递推指标EMA/RSI/MACD/KDJ截断偏移）
      - lookahead_guard() 防未来断言（截断重算一致性验证，可检出未来泄露）
    - 新增 FeatureDataset 特征数据集（X/y/timestamps/feature_columns/label_column，to_json 序列化供训练/推理）
    - 新增 bars_to_df() Bar列表→标准化DataFrame（输入仅限 processed 清洗后数据，禁读 raw）
    - 特征因子：价格衍生（return_1/5/10/20 + log_return_1 + rolling_mean/std/max/min(5/20/60) + momentum_5/20）+ 技术指标（SMA20/60/EMA12/26/RSI14/MACD/KDJ/ATR14/Bollinger/Vol20）+ 派生（sma_gap/atr_norm/bb_width）
    - 防未来函数（铁律）：特征全 trailing 窗口只含 ≤t；标签 shift(-horizon) 引用未来仅存在于训练目标列，build_dataset 丢弃无标签尾部；lookahead_guard 显式检出未来泄露；篡改未来 close 不影响当前特征（6列验证）；源码扫描无预测方法/无硬编码未来日期
    - 为实盘对接：compute_incremental 逐 bar 增量特征与训练精确一致；FeatureConfig JSON 序列化训练/推理共用；特征列名固定
    - 任务18全量验证：`tests/_verify_ml_features.py` **71项通过，0失败**；stage8_integration(79)/api(67) 回归通过
    - 阶段9累计验证：2295 + 任务18(71) = **2366项全通过，0失败**
20. ✅ 阶段9任务19已完成（2026-08-28，ml/model_registry.py 模型注册版本管理，ML模块完整MLOps链路第二环）
    - 新增 ModelRegistry 模型注册表 + ModelVersion 版本元数据 + 4个异常类（ModelRegistryError/ModelNotFoundError/ModelVersionNotFoundError/ModelIntegrityError）
    - 存储布局：datasets/models/{model_name}/versions/{index.json + v{n}/model.pkl + metadata.json}（对齐任务14 数据集版本管理模式）
    - ModelVersion 元数据：model_type/checksum(sha256)/created_at/source(offline/live)/feature_config_hash/feature_columns/label_column/train_start_time/train_end_time(防未来)/metrics/hyperparameters/status/parent_version/description
    - 核心方法：register_model（版本自增+工件落盘+checksum+索引双写）/ load_model（checksum完整性校验，篡改检出ModelIntegrityError）/ activate_version（实盘热切换，旧active自动归档）/ rollback（回退前一版本+last_rollback顶层记录）/ list_models/list_versions/get_model_info/get_model_metadata/get_active_version/get_latest_version/verify_model/delete_version/delete_model/get_summary
    - 复用任务18：feature_config_hash（FeatureConfig JSON hash）+ feature_columns（FeatureEngine预演40列）→ 模型与训练特征绑定；模型工件 pickle 序列化不绑定任何 ML 框架
    - 并发安全：线程锁 + 跨进程文件锁（实盘多进程预留）
    - 防未来函数（铁律）：train_end_time 记录训练数据截止（推理侧只使用<=截止数据）；无预测方法；无硬编码未来日期；load_model/verify_model checksum 校验
    - 为实盘对接：active 热切换（模型不停机切换）+ 多版本并存（A/B测试/回滚）+ source区分offline/live
    - 修复2个真实缺陷：feature_columns 解析（FeatureEngine属性非FeatureConfig，改为引擎预演）；last_rollback 写错层级（head内→index顶层）
    - 任务19全量验证：`tests/_verify_model_registry.py` **74项通过，0失败**；ml_features(71)/stage8_integration(79)/api(67) 全部回归通过
    - 阶段9累计验证：2366 + 任务19(74) = **2440项全通过，0失败**
20. ✅ 阶段9任务20已完成（2026-08-28，ml/training_pipeline.py 训练流水线，ML模块完整MLOps链路第三环）
    - 内置 numpy 岭回归（LinearRegressor）+ 逻辑回归（LogisticRegressor），sklearn/lightgbm/xgboost 均不可用 → 模型必须 numpy 手写
    - 可插拔 model_factory：内置两类 + 自定义模型（fit/predict 鸭子类型）注册
    - time_series_split 时间序列切分：严格递增 argsort（绝不用随机切分），strict_split 防标签重叠（horizon 间隔防未来泄露）
    - 评估指标 numpy 手写：regression_metrics（R²/MAE/RMSE/MAPE）+ classification_metrics（准确率/精确率/召回率/F1/AUC）
    - TrainingPipeline.run()/retrain()：训练→评估→ModelRegistry 注册闭环（版本自增/active热切换/train_end_time记录数据截止防未来）
    - 任务20全量验证：`tests/_verify_training_pipeline.py` **99项通过，0失败**；ml_features(71)/model_registry(74)/stage8(79)/api(67) 全部回归通过
    - 阶段9累计验证：2440 + 任务20(99) = **2539项全通过，0失败**
21. ✅ 阶段9任务21已完成（2026-08-28，ml/inference_service.py 批量/实时预测服务，ML模块完整MLOps链路第四环）
    - InferenceService：predict_batch（批量预测）/ predict_realtime（实时单条预测）
    - 特征对齐校验：推理特征列与训练特征列逐列校验，不一致抛 FeatureMisalignmentError
    - train_end_time 守卫：实时/批量预测前校验当前时间 ≤ 模型 train_end_time，防止用未来数据预测（违规抛 TrainTimeGuardError）
    - 复用任务18 FeatureEngine.compute_incremental + 任务19 ModelRegistry.load_model，预测结果 JSON 安全
    - 任务21全量验证：`tests/_verify_inference_service.py` **63项通过，0失败**；training_pipeline(99)/ml_features(71)/model_registry(74)/stage8(79) 全部回归通过
    - 阶段9累计验证：2539 + 任务21(63) = **2602项全通过，0失败**
22. ✅ 阶段9任务22已完成（2026-08-28，live/scheduler.py 定时任务/巡检/日报，实盘增强第一项）
    - CronExpression 轻量 5 字段 cron 解析（分/时/日/月/周），支持 * / */n / a,b,c / a-b / a-b/n，POSIX 日周 OR 语义，周7归一；match()/next_after()/next_timestamp()
    - ScheduledTask 任务数据类 + LiveScheduler 调度器：interval/cron 两种调度，后台守护线程逐 tick 派发，异常隔离（单任务失败不中断），超时保护，认领防重派发，pause/resume/run_task_now/run_once
    - HealthPatrol 健康巡检器：引擎/行情源/券商/持仓/风控 5 项内置检查 + 自定义检查项，输出每项 healthy/degraded/down + 汇总；引擎 RUNNING+0错误→healthy、未运行→degraded、ERROR→down
    - DailyReport 每日报告生成器：汇总引擎统计/账户/持仓明细/巡检健康/汇总指标，输出 dict + Markdown + JSON 落盘 outputs/daily_reports/；持仓字段对齐真实 Position（avg_cost/cost_value/market_value）
    - attach_engine() 实盘便捷挂载：一行注册巡检（默认60s interval）+ 收盘日报（默认 "30 15 * * 1-5" cron）
    - 防未来函数（铁律）：调度仅基于当前时钟；巡检/日报只读已发生状态；无预测方法/无硬编码未来日期
    - 修复3个真实缺陷：任务错误未累计 total_errors；自定义检查项调用约定（无参）；日报持仓字段未对齐真实 Position
    - 任务22全量验证：`tests/_verify_live_scheduler.py` **127项通过，0失败**；engine/data_feed/portfolio/paper_broker/base_broker/task2_engine/live_config 全部回归通过
    - 阶段9累计验证：2602 + 任务22(127) = **2729项全通过，0失败**
23. ✅ 阶段9任务23已完成（2026-08-28，live/state_store.py 状态持久化/崩溃恢复，实盘增强第二项）
    - StateStore 状态存储核心：
      - 存储布局 datasets/live_state/{store_id}/versions/ + index.json（对齐任务14/19 版本管理模式）
      - 原子写（临时文件 + os.replace 防写一半崩溃损坏）+ 索引双写 + 线程锁
      - save() 版本自增 / load_version() / load_latest() / recover_latest() / list_snapshots() / delete_version()（head自动回退）/ delete_old(keep) 保留策略 / verify_snapshot() / get_summary()
      - 读时 checksum(sha256) 校验，篡改/损坏抛 SnapshotIntegrityError
    - StateSnapshot 快照数据类：version/timestamp/tag/checksum/metadata/state + verify() + to_dict/from_dict
    - capture_live_state() 采集：engine + account + positions + orders + scheduler（duck-typing，组件缺省安全跳过）+ extra 扩展
    - restore_live_state() 回写：portfolio reset → BUY 重建持仓 → update_cash 精确回写现金 → 回写市值（真实 Position 全字段对齐）；broker 支持 restore_state 时回写；scheduler v1 仅记录；strict 模式缺失组件抛错
    - attach_snapshot()：与任务22 调度器联动，注册定时快照任务（默认300s interval）
    - 4个异常类：StateStoreError / SnapshotNotFoundError / SnapshotIntegrityError / SnapshotVersionError
    - 防未来函数（铁律）：快照只存已发生状态；恢复只回写已知数据、不生成新交易信号；无预测方法/无硬编码未来日期
    - 为实盘对接：崩溃后 recover_latest 一键恢复持仓/资金；定时快照防丢单；store_id 支持多账户/多策略隔离；broker restore_state 钩子预留真实券商状态同步
    - 修复2个真实缺陷：delete_old(keep=0) 被 falsy 判断吞掉校验（改为 None 判断）；get_summary latest_snapshot 表达式写坏（改为清晰实现）
    - 任务23全量验证：`tests/_verify_state_store.py` **88项通过，0失败**；live_scheduler(127)/engine/data_feed/portfolio/paper_broker/task2_engine 全部回归通过
    - 阶段9累计验证：2729 + 任务23(88) = **2817项全通过，0失败**
24. ✅ 阶段9任务24已完成（2026-08-28，tests/test_data_clean.py 补充全部极端边界测试case）
    - test_data_clean.py 完整重写：37个原有测试 + 55个新增极端边界测试 = 92个测试全通过
    - 覆盖：OHLC变体/全零停牌/零成交量/非数值/缺失字段/无效时间戳/数值timestamp/时间回退排序/sort=False/drop_all/多重复/阈值边界(严格>)/单条/NaN/Inf/超大数值/空白symbol/额外字段保留/OO三元组/新旧函数100%一致/clean_single流式/CleanerRegistry注册/配置覆盖/tick极端边界/空数据除零保护/excellent边界(需bad==0)/99%excellent/95%good/80%fair/extra_info/缺失字段默认/无效yaml优雅降级/部分覆盖保持默认/不存在路径返回默认
    - 过程中发现并修复1个真实缺陷：rules_loader.py浅拷贝污染全局默认配置（dict(DEFAULT) → copy.deepcopy(DEFAULT)，嵌套dict被_patch_config原地修改永久污染默认值）
    - 任务24验证：pytest tests/test_data_clean.py -v → **92 passed, 0 failed**（0.24s）；stage2/rules_loader/bar_cleaner/tick_cleaner 全部回归通过
    - 阶段9累计验证：2817 + 任务24(92) = **2909项全通过，0失败**
25. ✅ 阶段9任务25已完成（2026-08-28，阶段9完整集成验证脚本 tests/数据清洗集成验证.py）
    - 8条集成链路，62个验证断言，全通过（EXIT=0）
    - 链路1 数据清洗全链路（8项）：BarCleaner → CleanPipeline → quality_reporter → bad落盘 → 脏数据文件内容校验 → 质量报告内容/等级判定 → 空数据除零保护
    - 链路2 事件总线联动（7项）：EventBus → AlertManager(flush聚合器) → MetricsCollector(脏数据指标+Prometheus) → RiskManager(critical自动暂停) → 事件历史
    - 链路3 监控三件套（9项）：HealthChecker(run_all) → AlertManager(分级/确认/未确认) → MetricsCollector(从HealthChecker采集+Prometheus+过期检测)
    - 链路4 数据库元信息（6项）：create_all_tables建表 → CleanDataQueries插入/查询/脏数据统计/汇总/列表
    - 链路5 ML全链路（9项）：FeatureEngine(40特征列) → TrainingPipeline(numpy岭回归+评估指标) → ModelRegistry(注册+加载checksum) → InferenceService(批量预测+train_end_time守卫+特征对齐)
    - 链路6 实盘调度+状态（10项）：LiveScheduler(interval+手动执行) → HealthPatrol(持仓检查) → DailyReport(构建+Markdown落盘) → StateStore(快照+checksum+崩溃恢复+持仓/资金精确重建cash=90000.0)
    - 链路7 清洗→风控→恢复闭环（6项）：清洗检出脏数据 → critical事件→风控symbol级暂停 → 暂停原因记录 → 状态查询 → 重置恢复 → 事件历史完整
    - 链路8 全链路防未来函数（7项）：特征时间戳对齐 → build_dataset丢弃无标签尾部 → 源码扫描(推理/实盘/清洗模块无shift(-)) → train_end_time守卫 → StateStore只存已发生状态 → BarCleaner只比较前一条 → 全部历史日期
    - 设计：纯Python无外部依赖，临时目录隔离，可独立运行也可pytest收集，全部使用历史数据(2024-01)
    - 任务25验证：python tests/数据清洗集成验证.py → **62 PASS / 0 FAIL**，EXIT=0
    - 阶段9累计验证：2909 + 任务25(62) = **2971项全通过，0失败**
26. ✅ 阶段9任务26已完成（2026-08-29，双券商并行三件套：BrokerManager多券商管理器 + QmtBroker迅投QMT适配器 + PtradeBroker恒生Ptrade适配器）
    - A. BrokerManager 多券商管理器（myquant/live/brokers/manager.py，~22KB）：
      - 双券商并行核心：register_broker(broker, alias, role) 支持 primary/secondary 角色，alias 自动生成，非法角色/重复别名/非BaseBroker 均拦截
      - 订单路由：默认路由主券商，submit_order(order, broker_id=...) 指定券商，Order.extra 写入 broker_id 路由标记
      - 主备 Failover 双路径：① 连接断开 → failover() 自动切健康备券商；② 主券商拒单 → _failover_submit 逐备券商重试 + 成功后自动切主（修复真实缺陷：原实现误用连接健康判断，拒单场景主连接健康导致不切换）
      - 合并视图：get_account(merged=True) 跨券商累加5项资金字段；get_positions(merged=True) 按symbol聚合数量+加权均价，extra 记录 broker_ids 溯源
      - 状态持久化：get_state / restore_state 衔接 StateStore；回调透传（order/trade/account 自动注册到全部券商）
      - 4个异常类 + get_summary / get_state / restore_state / health_check_all
    - B. QmtBroker 迅投QMT适配器（myquant/live/brokers/qmt_broker.py，~28KB）：
      - 双模式：mode="SIM" 本地完整模拟撮合引擎；mode="LIVE" 真实 xtquant SDK 通道统一占位（_call_qmt_sdk 明确抛 NotImplementedError，不静默降级）
      - 行情驱动撮合：update_market_price(symbol, price) 与 PaperBroker 完全同接口，data_feed 收到行情推送统一调用
      - 成交回报统一入口 _execute_fill：与真实 QMT on_trade 回报同构，模拟与实盘逻辑一致，接入 SDK 后上层零修改
      - A股规则：T+1 当日买入冻结、整手100股、涨跌停10%校验、佣金万2.5+最低5元+印花税千1+过户费
      - load_config 支持 qmt: 段配置；account_id 多账户预留；模块头部完整「真实QMT SDK对接说明」
    - C. PtradeBroker 恒生Ptrade适配器（myquant/live/brokers/ptrade_broker.py，~11KB）：
      - 模拟引擎继承 QmtBroker（同构共享，避免20KB重复代码）；LIVE 分支覆写 _call_qmt_sdk → _call_ptrade_api 占位（零重复）
      - Ptrade 策略语义接口：order_shares(按股数) / order_value(按金额整手换算) / order_target_percent(目标仓位比例)，统一收敛到 submit_order(Order)
      - load_config 支持 ptrade: 段；health_check 独立语义（ptrade_api_ready/ptrade_path）；模块头部完整「真实Ptrade API对接说明」
    - 防未来函数（铁律）：成交只依据【已发生】行情（update_market_price 传入当前价）；不预测价格/不假设成交价；T+1 冻结只累计已成交买入；主备切换只依据已发生健康检查；三文件均无预测方法/无硬编码未来日期
    - 任务26全量验证：`tests/_verify_broker_manager.py` **76项** + `tests/_verify_qmt_broker.py` **84项** + `tests/_verify_ptrade_broker.py` **64项** = **224项全通过，0失败**；paper_broker/base_broker/stage8_integration(79) 全部回归通过
    - 阶段9累计验证：2971 + 任务26(224) = **3195项全通过，0失败**
27. ✅ 阶段9任务27已完成（2026-08-29，live/engine.py 接入 BrokerManager 多券商管理器 + 双券商模拟实盘入口）
    - A. engine.py 接入多券商管理器（myquant/live/engine.py，10处改造）：
      - `set_broker(单券商)` 自动包装为 BrokerManager（向后兼容，重复调用整体替换防残留）；新增 `set_broker_manager(manager)` 显式多券商并行入口（非 BrokerManager 抛 TypeError）
      - 唯一执行通道 = broker_manager：`start()` 连接全部券商 + 回调透传（order/trade/account 到全部券商）+ `StrategyContext.executor=broker_manager`；`stop()` 断开全部
      - `on_market_data` 行情经 `BrokerManager.push_market_data` 广播全部券商（manager.py 新增方法，duck-typing：PaperBroker.on_bar T+1 / QmtBroker.update_market_price 行情驱动，各券商只撮合自己账上订单→主备天然无重复成交）
      - `_submit_order` 走 broker_manager 自动路由主券商 + 拒单/断线自动 failover；`_get_watch_symbols` 用合并持仓；`get_status` 新增 broker 摘要（mode/primary/broker_count/brokers）；`health_check` broker 段统一 schema（primary + brokers 全量 + connected，单/多券商同构）
      - ★ 修复实盘引擎 v1 缺口：`StrategyContext.data` 此前恒为空导致策略"数据不足"无信号——新增 `_sync_context_data` 将 bar_history 增量同步为 DataFrame（按 bar.timestamp 索引、含 close 列、只含当前及历史 bar），与回测引擎同构，防未来
    - B. 双券商模拟实盘入口（scripts/双券商模拟实盘.py，中文名入口）：
      - QMT 主 + Ptrade 备（SIM 模式）BrokerManager 并行；akshare 历史数据优先 + 确定性模拟 K 线兜底
      - 端到端：行情广播 → 策略信号 → 风控 → 路由主券商 → 行情驱动撮合 → 成交回报 → portfolio 合并账本；每日 settle_t1 模拟 T+1 日切
      - 主备 failover 演示：第60%交易日断开主券商 → 自动切换备券商 → 继续交易（切换后健康校验）；双券商合并视图对账
      - 运行结果：8 信号 8 订单 8 成交（买卖闭环）0 错误，failover 后 ptrade 继续成交，10 项链路验证全过
    - 防未来函数（铁律）：context.data 只含当前及历史 bar；成交只依据已发生行情驱动；主备切换只依据已发生健康/拒单；时间戳校验拒绝未来 bar；文件无预测方法/无硬编码未来日期
    - 真实 QMT/Ptrade SDK 接入（LIVE 通道）待券商终端环境（见交接摘要）；SIM 模拟撮合与真实回报同构，接入 SDK 后上层零修改
    - 任务27全量验证：`tests/_verify_engine_multi_broker.py` **81项全通过，0失败**；回归零破坏：_verify_engine / _verify_stage8_integration(79) / _verify_live_scheduler(127) / 数据清洗集成验证(62) / broker 三件套(76+84+ptrade) / scripts/实盘模拟回测.py（单券商向后兼容，1.22% 与基线一致）
    - 阶段9累计验证：3195 + 任务27(81) = **3276项全通过，0失败**
28. ✅ 阶段9任务28已完成（2026-08-29，实时行情全链路：A实时行情源+B事件总线联动+C引擎实时驱动+D中文入口脚本）
    - A. 实时行情源（myquant/live/data_feed.py 全量重写，向后兼容）：
      - RealtimeQuoteFetcher 多后端自动降级：腾讯 qt.gtimg.cn 批量接口（主源，只精确请求订阅标的，轻量）+ akshare 东财快照（备源）+ mock 确定性模拟源（离线验证）
      - 腾讯行解析：手→股、万→元换算、数据源自带行情时间戳、防未来回退（时间戳超前→到达时刻）
      - RealtimeBarAggregator 周期聚合器：同周期复用增量更新，周期切换才产新bar（防策略高频空转）
      - MockRealtimeSource 确定性模拟源：内部时钟从过去推进，确定性价格路径（完全可复现）
      - MarketDataFeed 增强：source="realtime/realtime_tencent/realtime_akshare/realtime_mock" 实时模式；单条流式清洗（复用 _validate_single_bar + 跨bar跳变缓存检测）+ 脏数据钩子 register_dirty_callback + 指数退避节流
      - 真实源联网探测通过：腾讯实时快照成功（贵州茅台，数据源自带时间戳，防未来校验通过）
    - B. 流式清洗联动（data_feed.py 新增 attach_event_bus + _RealtimeEventBridge）：
      - 窗口聚合：同 (symbol, dirty_type) 在 aggregate_window 内合并为一条 DataQualityEvent（bad_count/total_count 累加），窗口翻转才发布——避免事件风暴
      - 连续阈值升级：严重类型（OHLC非法/负价/负量/时间戳无效/缺symbol）连续 ≥critical_after 次才 critical，单次仅 warning——过滤瞬时抖动，避免误触发风控暂停
      - 全局单例默认挂载：attach_event_bus(event_bus=None) 自动用 get_event_bus()，一行接入
      - 线程安全：聚合/计数加锁，发布在锁外；on_clean 清零连续计数 + clean streak 观测；disconnect 时 flush_all 补发
      - dirty_type 推断对齐任务6 14项 DirtyDataType 枚举；事件字段 source="live" / source_step="realtime_feed"
      - 联动自动生效（零改动三处订阅方）：任务10 AlertManager 分级告警 + 任务11 MetricsCollector 脏数据指标 + 任务12 RiskManager 严重脏数据自动暂停
    - C. 引擎实时驱动（myquant/live/engine.py 7处改造）：
      - start() 实时模式自动启动后台轮询（feed.start(poll_interval)）；日线模式保持不自动启动（兼容 LiveScheduler 手动驱动，零破坏）
      - _dict_to_bar 频率自适应（_infer_bar_frequency 映射 MINUTE_1/5/15/30、HOUR_1、DAILY，不硬编码）
      - _validate_bar_time 双重保护：实时 ≤now+5s（与data_feed容忍度对齐）+ 时间单调（bar.timestamp > 最后一根，防旧快照晚到/重复）
      - _warmup_history 历史预热：频率自适应（1m/5m等优先 akshare 分钟级 stock_zh_a_hist_min_em，失败降级日线）+ clean_bar_records 清洗过滤；策略启动即有足够窗口（如MA20），不会恒"数据不足"
      - pause()→feed.stop()（节能）、resume()→feed.start()（恢复）；stop() 补 feed.disconnect()
      - 构造参数：realtime_poll_interval=3 / warmup_bars=60 / enable_warmup=True
    - D. 中文入口脚本（scripts/实时行情驱动.py，中文名入口）：
      - 串起 A+B+C 完整闭环：实时行情源 → 流式清洗+事件总线 → 引擎自动轮询 → 策略信号 → 风控 → 券商模拟撮合 → 成交回报
      - 12项命令行参数：--source / --frequency / --symbols / --poll-interval / --mock-step / --warmup-bars / --no-warmup / --event-bus/--no-event-bus / --duration / --capital / --broker
      - 默认 realtime_mock 离线可演示（mock step=10s + poll=1s → 约7秒产第一根1m bar）；--source realtime 切腾讯真实源；--broker paper/qmt/ptrade/dual（qmt/ptrade/dual 需券商终端环境，当前SIM模拟桩，标注清楚）
      - 短周期双均线策略（short=2/long=4，适配实时bar数量少）；信号/成交实时打印 + 每秒状态行 + 最终账户/交易/事件总线/行情源健康报告 + 链路验证断言
      - --duration 运行时长（默认30秒，0=无限）+ Ctrl+C 优雅停止
    - 防未来函数（铁律）：mock源从过去开始推进；真实源时间戳≤now+5s；引擎时间单调校验；事件时间戳=检出时刻；context.data只含当前及历史bar；四文件均无预测方法/无硬编码未来日期
    - 任务28全量验证：`tests/_verify_realtime_quote.py` **86项** + `tests/_verify_realtime_event.py` **72项** + `tests/_verify_realtime_engine.py` **56项** = **214项全通过，0失败**；入口脚本短时长运行测试通过（5秒产3根bar，链路验证6/6）；--help参数解析正常；--broker dual双券商模拟桩正常
    - 回归零破坏：data_feed(10) / stage8_integration(79) / live_scheduler(127) / 数据清洗集成验证(62) / A项(86) / B项(72) / C项(56) 全过
    - 阶段9累计验证：3276 + 任务28(214) = **3490项全通过，0失败**

## 第3项：策略库扩展 ✅（2026-08-29，A-G七子项全部完成）
- 状态：✅已完成 (2026-08-29)
- 目标：在既有 ma_strategy（ma_cross/rsrs）基础上扩展策略库：网格/动量/均值回归/多因子4个新策略 + 注册器导出更新 + 统一验证脚本 + 文档更新；全部为回测/实盘共用（on_bar逐根驱动 + StrategyContext 访问数据，配置即参数，context.data 只含当前及历史 bar 防未来）
- 交付：
  - **A项 网格策略** `myquant/strategies/grid_strategy.py`（name="grid"）：
    - 固定上下界网格 + auto_grid（基于历史ATR自动算区间）；跌破一格买一份（LIMIT单挂网格线下沿）、涨破一格卖一份（LIMIT单挂网格线上沿）
    - 区间下破止损 / 上破止盈 → 市价清仓；每格仓位 position_per_grid，A股100股整数倍；多标的独立网格状态
    - `get_grid_status()` 供监控/Web看板
  - **B项 动量策略** `myquant/strategies/momentum_strategy.py`（name="momentum"）：
    - 动量=close[-1]/close[-period-1]-1（trailing窗口）；定期调仓（_should_rebalance）选top_n，卖出不在池+买入新入池
    - 等权或动量加权；单票仓位上限；min_momentum阈值；单票止损/止盈（复用base.py工具）
    - ★ F项修复防未来缺陷：`_calc_momentum_scores`/`_get_latest_price` 原读全量最后一行 → 新增 `_get_close_upto(context, symbol, ts)` 按 bar.timestamp 截断，回测不再读未来价
    - `get_momentum_status()` 供监控
  - **C项 均值回归策略** `myquant/strategies/mean_reversion_strategy.py`（name="mean_reversion"）：
    - 复用 indicators：BollingerBands + RSI（已内置防未来）；买入：价<=下轨 AND RSI<=超卖；卖出：价>=上轨+RSI超买 / 回归中轨(exit_on_mid) / 止损 / 止盈
    - 带宽保护 min_bandwidth_pct=0.5%（防零波动频繁信号）；指标按symbol缓存（实盘性能优化）
    - ★ F项修复防未来缺陷：`_calc_indicators` 原读全量最后一行 → 加 ts 参数按 bar.timestamp 截断
    - `get_mean_reversion_status()` 供监控
  - **D项 多因子策略** `myquant/strategies/multi_factor_strategy.py`（name="multi_factor"）：
    - 纯价格因子：动量(20日)+反转(5日)+低波动(20日)+量价趋势；z-score滚动标准化（严格trailing），加权打分选top_n
    - 定期调仓；等权或因子加权；单票仓位上限；量能恒定标的剔除；extra_factors 预留基本面因子插拔
    - 严格防未来：`_get_data_upto` 四参数签名按当前bar截断，全部因子trailing计算
    - `get_multi_factor_status()` 供监控
  - **E项 注册器与导出更新**：
    - `myquant/strategies/registry.py`：自动导入从1个（ma_strategy）扩展为5个策略模块，导入即注册
    - `myquant/strategies/__init__.py`：导出4个新策略类 + 更新__all__（共6个类）
    - `config/strategy_params.example.yaml`：补充4个新策略参数块，参数名与类签名逐一对应，**配置即参数**（yaml块直接作kwargs创建策略，回测/实盘config零桥接）
  - **F项 统一验证脚本** `tests/_verify_strategy_library.py`（独立运行，python tests/_verify_strategy_library.py，失败退出码1）：
    - A节 base.py 新方法（check_stop_loss/check_take_profit/_should_rebalance/_get_close_series）11项
    - B节 注册与工厂（6策略注册+create_strategy+参数透传+discover无重复）10项
    - C1-C4 四个新策略：信号逻辑（三种形态合成K线：上涨/下跌/震荡，确定性不依赖网络）+ 防未来（篡改未来价格/成交量不影响当前信号与调仓池订单）+ 参数校验
    - D节 现有策略回归：ma_cross金叉+金叉日防未来、rsrs注册/创建/on_init/数据不足HOLD，零破坏
    - 全部66项通过，0失败
  - **G项 文档更新**：
    - `docs/project_layout_gear2.md`：strategies/ 目录树展开为8文件（4新策略+__init__/registry/base/ma_strategy）+ strategy_params注释 + tests补充 _verify_strategy_library.py
    - `docs/stage_completion.md`：本section标记第3项完成
    - `docs/交接摘要_策略库扩展已完成.md`：新建完成态交接摘要，记录A-G全部成果与进入第4项
- 防未来函数（铁律）：
  - 6个策略全部验证通过「篡改未来价格/成交量 → 当前bar信号与调仓结果不变」（grid全程信号序列 / momentum池+订单 / mean_reversion信号 / multi_factor池+订单 / ma_cross金叉日信号）
  - F项统一脚本揪出并修复2个历史防未来缺陷（momentum/mean_reversion 原读context全量最后一行），修复后与multi_factor同款截断方案对齐
  - 源码扫描无预测方法/无硬编码未来日期；T日信号T+1成交由引擎层保证
- 验证基线：
  - 策略库扩展统一验证：`tests/_verify_strategy_library.py` **66项全通过，0失败**（含6策略全量 + base.py新方法 + 回归）
  - 回归零破坏：ma_cross/rsrs 注册/创建/on_init/信号 全过；D项临时脚本 `_tmp_verify_multi_factor.py` 已删除（并入统一脚本）
  - **项目总验证基线：3510 + 66 = 3576项全通过，0失败**

## 第4项：回测报告增强 ✅（2026-08-29，A-D四子项全部完成）
- 状态：✅已完成 (2026-08-29)
- 目标：在基础回测输出之上增强专业报告能力：蒙特卡洛模拟（收益分布/破产概率/置信区间）、HTML交互式报告（Plotly图表+蒙特卡洛区块）、PDF专业排版报告（可打印存档）、统一验证脚本与中文入口；全部为回测/实盘共用（报告模板与实盘日报统一风格，回测vs实盘偏差分析预留）
- 交付：
  - **A项 蒙特卡洛分析模块** `myquant/backtest/monte_carlo.py`（新增）：
    - `MonteCarloAnalyzer(n_simulations=2000, horizon_days=252, ruin_threshold=0.5, seed=42)` + `analyze(equity_curve) -> MonteCarloResult`
    - 方法：仅用**已实现历史收益**（pct_change）有放回重采样引导 → 累积乘积（cumprod）模拟收益路径；回撤路径用 cummax 滚动最大值；无偏标度逐期波动率按 sqrt 折算
    - `MonteCarloResult` 字段：final_returns / max_drawdowns / p5~p95收益分位 / 回撤95%CI / 中位回撤 / quantile_equity(5×253分位带) / prob_negative / prob_ruin / n_simulations / seed
    - 参数校验（n_simulations>0、horizon_days>0、0<ruin_threshold<=1）；固定seed可复现；空输入/净值<30点返回空结果（调用方自动跳过）
  - **B项 HTML报告增强** `myquant/backtest/report.py`（增强，零破坏）：
    - `generate_html(result, path, include_monte_carlo=True, mc_n_simulations=2000, mc_horizon_days=252, mc_seed=42)`
    - 新增蒙特卡洛区块：统计卡（中位收益/中位回撤/收益95%CI/回撤95%CI/负收益概率/破产概率）+ 收益直方图(mcReturnChart) + 回撤直方图(mcDrawdownChart) + 收益路径分位带(mcQuantileChart, P5/P25/P50/P75/P95)
    - 风险提示语："蒙特卡洛模拟基于历史收益的统计推断，不代表未来实际表现"
    - 关闭开关 include_monte_carlo=False 回归安全；原净值/回撤图与交易明细保留
    - `generate_full_report` 新增PDF输出（5件套：html/pdf/text/trades_csv/equity_csv）
  - **C项 PDF报告生成** `myquant/backtest/pdf_report.py`（新增）：
    - `PdfReportGenerator().generate_pdf(result, output_path, include_monte_carlo=True, ...)`
    - 依赖：**fpdf2 2.8.8**（纯Python、免费、本地离线）
    - 中文字体自动探测：simhei.ttf黑体 → 等线 → 雅黑 → 宋体（系统字体，离线），失败静默回退英文
    - 结构（示例8页）：标题区 / 核心绩效表 / 交易统计表 / 净值曲线图 / 回撤曲线图 / 蒙特卡洛（统计表+收益直方图+回撤直方图+分位带+风险提示）/ 交易明细（自动分页表头重复、盈亏红绿）
    - 固定seed可复现；净值<30点自动跳过蒙特卡洛
  - **D项 统一验证脚本 + 中文入口**：
    - `tests/_verify_backtest_report.py`（新增，独立运行，失败退出码1）：A蒙特卡洛16项 + B HTML 13项 + C PDF 33项 = **62项全过**
    - 合成确定性回测结果（120日净值+40笔成交），不依赖网络；含防未来源码扫描（无 shift(-) / 无 future / 无硬编码未来日期）
    - `scripts/生成回测报告.py`（新增，中文名入口，薄入口）：复用 `run_回测入口.py` 的 build_backtest_config；参数 --config/--strategy/--symbols/--start/--end/--capital/--output/--no-monte-carlo/--mc-simulations/--mc-horizon/--mc-seed/--list-strategies/--verbose
    - 全流程：配置→HistoricalDataProvider→策略注册器→BacktestEngine→LookaheadGuard强制校验→完整报告5件套
    - 端到端实证：平安银行000001.SZ 2024全年242根K线 + ma_cross，退出码0，5件套齐全（HTML含蒙特卡洛区块；PDF 8页/184中文字符）
- 防未来函数（铁律）：
  - 蒙特卡洛仅用**已实现**历史收益重采样，不引用未来数据；净值<30点时自动跳过避免统计偏差
  - HTML/PDF报告只渲染回测已产生的净值/交易/指标，无预测方法
  - 统一验证脚本含源码扫描：monte_carlo.py/report.py/pdf_report.py 均无 shift(-) / future / 硬编码未来日期
  - 入口端到端链路经 LookaheadGuard 强制校验通过
- 验证基线：
  - 回测报告增强统一验证：`tests/_verify_backtest_report.py` **62项全通过，0失败**（A蒙特卡洛16 + B HTML13 + C PDF33）
  - 回归零破坏：原 report.py 生成HTML/交易明细功能不变；generate_full_report 新增PDF为增量能力
  - **项目总验证基线：3576 + 62 = 3638项全通过，0失败**

## 第5项：性能优化 ✅（2026-08-29，A-F六子项全部完成）
- 状态：✅已完成 (2026-08-29)
- 目标：基于 benchmarks 实测优化热点指标全量计算与增量更新性能，为实时行情/实盘逐根推送场景做准备；全量计算向量化消除Python循环，增量更新O(period)→O(1)，同时修复多处增量与全量不一致的原有bug
- 交付：
  - **A项 WMA** `myquant/indicators/moving_average.py`：
    - 全量计算：Python for循环逐窗口np.dot → numpy sliding_window_view零拷贝窗口 + tensordot矩阵乘，10000根提速26.4倍（19.75ms→0.75ms）
    - 增量更新：list(deque)切片+np.dot → 维护加权和+窗口和O(1)递推，0.00043ms/根
  - **B项 SMA** `myquant/indicators/moving_average.py`：
    - 增量更新：list(deque)切片+np.mean → 维护滚动总和O(1)递推，提速8.9倍（0.00315ms→0.00035ms/根）
    - 全量计算不变（pandas rolling.mean已向量化）
  - **C项 ATR** `myquant/indicators/volatility.py`：
    - 全量计算：pd.concat([tr1,tr2,tr3],axis=1).max → np.maximum链式向量化，零临时对象，提速2.7倍（1.38ms→0.52ms）
    - 修复原有bug：wilder模式增量匹配pandas ewm(adjust=False,min_periods)从第1根持续递推；sma模式第一根TR入历史队列
  - **D项 Bollinger/Volatility** `myquant/indicators/volatility.py`：
    - 增量更新：np.array(list(...))+mean+std → 维护滚动总和+平方和O(1)，ddof=1与全量一致
    - Bollinger 0.00118ms/根，Volatility 0.00081ms/根
    - 修复原有bug：Volatility pct_chg模式第一根收益率入历史并累积，对齐全量首个有效值位置
  - **E项 KDJ** `myquant/indicators/momentum.py`：
    - 增量更新：list(deque)[-n:]+dict生成器max/min → Python list环形缓冲区+内置max/min，提速1.9倍（0.00175ms→0.00095ms/根）
    - 修复原有bug：K/D初始值从k_prev=50递推 → 第一个有效值=RSV本身，匹配全量ewm(adjust=False)
  - **F项 统一验证+中文入口**：
    - `tests/_verify_perf_optimization.py`：A-E五项整合，一键运行164项验证
    - `scripts/性能基准测试.py`：中文入口，支持--benchmark-only/--verify-only/--quick
    - 五个子项独立验证脚本：_verify_perf_item_a_wma.py(23) / _b_sma.py(20) / _c_atr.py(25) / _d_bollinger_volatility.py(62) / _e_kdj.py(34)
- 防未来函数（铁律）：
  - 所有优化仅改变计算方式，不改变数据依赖窗口；sliding_window_view第i行仅含iloc[i-period+1:i+1]
  - 增量更新严格按bar顺序递推，无未来数据引用
  - 统一验证含防未来函数检查（前period-1行NaN）
  - 修复的增量一致性bug使回测（全量）与实盘（增量）结果完全一致，为实盘无缝对接奠定基础
- 验证基线：
  - 性能优化统一验证：`tests/_verify_perf_optimization.py` **164项全通过，0失败**（A23+B20+C25+D62+E34）
  - benchmarks指标基准：10项全PASS（全量<200ms，增量<5ms）
  - 回归零破坏：A-E各项交叉验证全过
  - **项目总验证基线：3638 + 164 = 3802项全通过，0失败**

## 第6项：Alembic 数据库迁移 ✅（2026-08-29，A-D四子项全部完成）
- 状态：✅已完成 (2026-08-29)
- 目标：引入生产级 Alembic 数据库迁移框架，替代/补充自研 MigrationManager，支持版本管理、增量升级、回滚、已有数据库兼容
- 交付：
  - **A项 初始化框架**：
    - `alembic.ini`（项目根目录，英文注释避免Windows GBK解码问题）
    - `myquant/database/migrations/env.py`（自动探测项目根、SQLite路径解析、offline/online双模式、事务迁移）
    - `myquant/database/migrations/script.py.mako`（迁移脚本模板）
    - `myquant/database/migrations/versions/`（版本目录）
    - `pyproject.toml` 添加 alembic>=1.13.0 依赖
  - **B项 初始迁移 0001**：`versions/0001_initial_schema.py`，8张业务表+21个索引，从schema.py完整转换，列名与schema.py逐列一致
  - **C项 增量迁移 0002 + stamp兼容**：
    - `versions/0002_add_market_data_composite_index.py`，新增market_data(symbol,period,trade_date)复合索引（实盘多周期查询优化）
    - 已有数据库兼容：`alembic stamp head` 标记版本不重复建表，旧MigrationManager建的库可无缝接入
  - **D项 中文入口 + 统一验证**：
    - `scripts/数据库迁移.py`（中文入口，支持upgrade/downgrade/current/history/stamp/check/init）
    - `tests/_verify_db_migration.py`（统一验证，A框架10+B初始17+C增量16+D入口14=57项）
- 设计决策：
  - Alembic 作为主迁移框架（生产级标准），现有自研 MigrationManager 保留为兼容层（不破坏 数据库初始化.py 和现有测试）
  - 迁移脚本用 op.execute() 执行原始SQL，与项目 raw sqlite3 风格一致，无需引入SQLAlchemy ORM
  - alembic.ini 用英文注释（Alembic用locale编码读取ini，中文注释在Windows GBK环境会解码失败）
  - 数据库路径支持 --db 参数和 QUANT_DB_URL 环境变量覆盖（实盘/测试切换）
- 防未来函数（铁律）：数据库迁移与未来函数无关，所有迁移脚本仅操作表结构，不涉及行情数据计算
- 验证基线：
  - 数据库迁移统一验证：`tests/_verify_db_migration.py` **57项全通过，0失败**
  - 0001迁移：8表+21索引，upgrade/downgrade/往返全过，列名与schema.py逐列一致
  - 0002迁移：复合索引新增/删除，版本0001↔0002切换
  - stamp兼容：旧方式建表后stamp head，不重复建表、不丢数据
  - 中文入口：7个子命令全部可用
  - **项目总验证基线：3802 + 57 = 3859项全通过，0失败**

---

## 项目结构蓝图对齐补全（2026-08-28）

### benchmarks 性能基准测试目录 ✅
- 新增 tests/benchmarks/ 目录（6个文件，20个基准测试全通过）
- 文件清单：
  1. `__init__.py` — 包说明
  2. `conftest.py` — 阈值配置（INDICATOR_THRESHOLD_MS=200 / ENGINE_THROUGHPUT_MIN=500 / EXECUTION_LATENCY_MS=5）+ 合成K线fixture（perf_bars_1k/5k/10k）
  3. `_benchmark_utils.py` — 纯Python轻量级基准框架（BenchmarkRunner + BenchmarkResult，统计均值/中位数/P95/P99/min/max/ops/sec，阈值断言，格式化报告），无pytest-benchmark外部依赖
  4. `test_indicator_perf.py` — 10个测试：SMA/EMA/WMA/MACD/RSI/KDJ/Bollinger/ATR全量计算(10000根) + SMA/RSI增量更新(实盘逐根推送)
  5. `test_engine_throughput.py` — 3个测试：1000根/5000根/3标的x2000根 引擎吞吐量（均线策略+回测引擎）
  6. `test_execution_latency.py` — 7个测试：市价单/限价单下单 + 撤单 + 查单 + 查持仓 + 查账户 + 查活动订单
- 关键性能数据：SMA(20)全量1万根=0.48ms；引擎吞吐量=1727-2532 bars/sec；市价单下单=0.006ms(16万ops/sec)；查单=0.0001ms(790万ops/sec)
- 全部远低于阈值，pytest收集正常（20 tests collected）

### tests/fixtures/ 补全3个模块 ✅
- 新增3个fixture文件，fixture总数从15增至43
- 文件清单：
  1. `strategies.py`（10个fixture）：ma_strategy/ma_strategy_fast/registered_ma_strategy/multiple_strategies/buy_signal/sell_signal/hold_signal/sample_signals/strategy_context/strategy_context_with_position
  2. `live_env.py`（10个fixture）：mock_broker/mock_broker_small/broker_with_position/mock_event_bus/event_bus_with_subscribers/live_portfolio/live_portfolio_small/portfolio_with_position/market_data_feed/live_environment（broker+portfolio+event_bus完整环境）
  3. `database.py`（8个fixture）：temp_db_path/db_connection/initialized_db/clean_data_queries/db_with_clean_run/db_with_dirty_stats/db_with_multiple_runs/database_environment
- tests/conftest.py 已更新 pytest_plugins 加载全部4个fixture模块（market_data + strategies + live_env + database）
- pytest启动验证通过，4个fixture模块全部正常加载

### 验证基线更新
- 阶段9任务1-27累计：3276项
- 阶段9任务28 实时行情全链路（A实时源+B事件总线+C引擎驱动+D入口）：214项
- benchmarks性能基准：20项
- 第3项 策略库扩展统一验证（A-G七子项，tests/_verify_strategy_library.py）：66项
- 第4项 回测报告增强统一验证（A-D四子项，tests/_verify_backtest_report.py）：62项
- 第5项 性能优化统一验证（A-F六子项，tests/_verify_perf_optimization.py）：164项
- 第6项 数据库迁移统一验证（A-D四子项，tests/_verify_db_migration.py）：57项
- **项目总验证基线：3510 + 66 + 62 + 164 + 57 = 3859项全通过，0失败**
- 项目结构与蓝图 project_layout_gear2.md 100%对齐（benchmarks目录 + 4个fixture模块全齐；live/brokers 双券商并行与总图同步；实时行情全链路 data_feed/engine/scripts 与总图同步；strategies/ 8文件策略库与总图同步；backtest/ monte_carlo.py+pdf_report.py+report.py增强+data_loader.py 与总图同步；scripts/生成回测报告.py+性能基准测试.py+数据库迁移.py 与总图同步；tests/ 5个性能优化验证脚本+统一验证脚本+数据库迁移验证脚本 与总图同步；alembic.ini + myquant/database/migrations/env.py+versions/ 与总图同步）
