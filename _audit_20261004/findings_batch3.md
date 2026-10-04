# 第3批 详细发现（实盘一致性、交易执行、订单、事前风控、异常交易）

## 阅读说明

本文件为审计工作底稿草案，不是鉴证报告，不含签字与保证性结论。审计基准为 2026-10-04 文件系统快照；环境无法执行 git，未取得 commit hash；重要性水平、抽样口径、材料真实性声明均未提供，故本批按降级口径编制，结论仅指向所引路径与行号的代码事实。

**证据等级口径（A–E，依 v3.0 提示词第九章 9.2 全局唯一表）**
- A：直接可核验物（代码/配置/日志原文，路径与行号可逐字复核，含 hash 可验）。
- B：文档/配置/截图等间接材料。
- C：基于材料的合理推断（含仅目录级观察）。
- D：材料缺失。
- E：单方陈述未经佐证（含无真实性声明的 hash）。
本批发现以 A 级（代码原文）为主，凡结论需补充运行时材料才能定量者，在「未知」栏注明。

**「依赖单方声明」的含义**：指结论所依据的信息来自被审计方（管理层、开发方）单方面提供的说明、注释、文档或配置自述，未取得独立第三方证据或系统外部证据印证。本次审计未取得管理层书面声明与配置签署记录，故凡以项目内注释/文档自述支撑的判断，均在本批中标注该属性；此类判断不能替代对运行态系统的验证。

**本批共性未知**：未取得实盘运行日志、券商对账单、调度器运行记录与测试执行报告，故所有「运行期是否发生」的判断仅为静态代码推断，属依赖单方声明的范围。

---

### KP-P3-01 T+1 冻结只增不减，买入次日仍不可卖出

- **维度**：交易执行 / 交易规则一致性
- **位置**：`myquant/live/brokers/paper_broker.py:158,285-286,808-809,825-834`；`myquant/live/brokers/qmt_broker.py:1384`
- **证据等级**：A（代码原文逐字核对，且全项目检索 `settle_t1` 调用点可复核）
- **优先级 / 风险等级**：P0 严重
- **事实**：买入成交时累加冻结：`self._t1_holdings[symbol] = self._t1_holdings.get(symbol, 0) + trade.quantity`（`:809`）；下单前置检查用 `available_qty = pos.quantity - t1_frozen`（`:286`），不足即 `order.reject`（`:288-298`）。释放仅靠 `settle_t1()` 内 `self._t1_holdings.clear()`（`:831-832`）。全项目检索 `settle_t1` 仅命中 `scripts/双券商模拟实盘.py:308,350`、`scripts/实盘模拟回测.py:275`；`myquant/live/engine.py`、`myquant/live/scheduler.py` 无调用点，`attach_engine` 只注册巡检/日报/补单三类任务（`scheduler.py:1613,1632,1648`）。
- **推断**：实盘与仿真多日连续运行时冻结量只增不减，次日卖单被本地前置检查永久拒绝，止损、止盈与强平出口被自锁。
- **未知**：是否有运行时动态调用（getattr/任务表注入）；QMT 实盘端是否由券商 T+1 校验绕过本地冻结。
- **风险影响**：持仓无法退出，风险敞口长时间裸露；仿真期无法验证可平仓行为，仿真→实盘准入证据链失效。
- **可能性**：控制完全缺失
- **根因**：技术——日切结算函数已实现但未接入任何生产调度生命周期。
- **整改建议**：在 `myquant/live/scheduler.py` 的收盘任务（15:30 日报后）注册日切任务，调用 broker_manager 各通道 `settle_t1()`；引擎启动时对存量持仓做一次解冻初始化。
- **验证方法（DoD）**：单测「T 日买入→日切→可卖数量恢复→卖单通过」；集成测试连跑两个交易日，断言无「可卖数量不足」拒单日志。
- **最坏情形**：实盘遇急跌无法卖出，强平流程亦被拒，损失无上限扩大。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-02 日线 bar 时间戳取行情到达时刻，同根 bar 重复推送并按该 bar 开盘价成交

- **维度**：交易执行 / 防未来函数
- **位置**：`myquant/live/data_feed.py:163,1686-1693`；`myquant/live/engine.py:892-899,998-1006`；`myquant/live/brokers/paper_broker.py:531-534`
- **证据等级**：A（构造 bar、去重校验、撮合三处原文已逐字核对）
- **优先级 / 风险等级**：P0 严重
- **事实**：bar 构造时 `"timestamp": time.time()`（`data_feed.py:163`），即取行情到达的墙钟时刻而非 bar 归属时间；推送侧取 `latest_bar = bars[-1]` 后逐回调推送（`:1686-1693`）。引擎校验只做「未来丢弃」与「时间戳单调」：`if bar.timestamp <= last_ts: return False`（`engine.py:1001-1006`）。校验通过后 `self._broker_manager.push_market_data(symbol, bar)`（`:896`）交给券商撮合；PaperBroker 市价单直接以该 bar 开盘价成交：`trade = self._match_market_order(order, open_price, timestamp)`（`paper_broker.py:531-534`）。
- **推断**：同一根日线在轮询周期内被反复推送，而每轮 timestamp 重新取墙钟因而递增，单调去重失效；每次推送都以「当日开盘价」成交，形成同一价格重复成交与事实上的时间穿越。
- **未知**：轮询周期与 bar 缓存窗口的实际配置；是否另有上层按 `bar.date` 去重（本批未检索到）。
- **风险影响**：仿真与实盘信号收益被系统性高估，回测/仿真结论不可用于真钱决策；成交记录与实际行情时序不符，审计轨迹失真。
- **可能性**：控制完全缺失
- **根因**：技术——bar 时间语义用到达时刻替代交易日归属时刻，去重键错配。
- **整改建议**：`data_feed.py` 改为以 bar 的 `date`（交易日）作为 `timestamp` 语义来源；`engine.py:_validate_bar_time` 增加按 `(symbol, date)` 去重；同根 bar 只允许首次推送触发撮合。
- **验证方法（DoD）**：单测同一根 bar 推送两次，断言第二次被丢弃且不产生成交；断言市价成交价时间戳等于 bar 日期而非当前时刻。
- **最坏情形**：以虚高仿真绩效上线，实盘收益与预期背离，客户资产受损。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-03 重启不恢复券商订单与持仓，双账本漂移

- **维度**：订单 / 状态持久化与恢复
- **位置**：`myquant/live/engine.py:1718`；`myquant/live/state_store.py:611,655-671,786-795`；`config/live_config.yaml:3345`
- **证据等级**：A（恢复调用签名、订单采集逻辑、配置项原文已核对，检索确认 broker 无 `restore_state`）
- **优先级 / 风险等级**：P1 高
- **事实**：恢复调用为 `restore_live_state(snapshot, portfolio=self._portfolio)`（`engine.py:1718`），未传 `broker`；快照采集侧确实落了订单 `"orders": _capture_orders(broker)`（`state_store.py:611`，实现见 `:655-671`，依赖 broker 具备 `get_orders`）。而恢复侧 broker 分支要求 `hasattr(broker, "restore_state")`（`:786`），PaperBroker/QmtBroker 均无该方法（brokers 目录检索 `def restore_state` 仅命中 `manager.py:1001`）；broker 缺失时仅记 `notes`「账户以 portfolio 为准」。配置 `auto_recover_on_start: false`（`live_config.yaml:3345`）使启动默认不做任何恢复。
- **推断**：崩溃重启后进程账本由快照 portfolio 重建，券商侧真实在途订单不被撤回也不被认领，未成交挂单成为「孤儿单」；持仓与现金以进程账本为准而与券商不一致，即双账本漂移。
- **未知**：券商端是否支持查询并领回订单；快照落盘频率与实际可用于恢复的时间点。
- **风险影响**：重启后可能重复下单或漏平仓，资金与持仓对不上，对账与审计无法闭环。
- **可能性**：控制存在未执行
- **根因**：技术——快照采集覆盖订单，恢复路径未实现 broker 侧回写与对账。
- **整改建议**：为 PaperBroker/QmtBroker 实现 `restore_state`（含在途订单与冻结资金）；`engine.py:1718` 补传 broker；启动时以券商查询结果为准做一次对账，差异则暂停交易并告警。
- **验证方法（DoD）**：集成测试「提交挂单→落快照→重启→断言挂单被领回或撤销且账本与券商一致」；对账差异路径有告警与 PAUSE 断言。
- **最坏情形**：重启后重复买入，超仓并占用资金，风险指标全部失真。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-04 涨跌停事前硬拦截恒失效（缺少昨收基准）

- **维度**：事前风控 / 交易规则
- **位置**：`myquant/risk/rules.py:403-413`；`myquant/live/engine.py:1541,1618`；`config/live_config.yaml:3325-3326`
- **证据等级**：A（规则实现、上下文构造、配置声明三处原文均已核对）
- **优先级 / 风险等级**：P1 高
- **事实**：`PriceLimitRule.check` 取 `prev_close = context.extra.get("prev_close", {}).get(order.symbol, 0)`，当 `current_price <= 0 or prev_close <= 0` 时直接 `return self._pass(reason="价格信息不足，跳过涨跌停检查")`（`rules.py:404-407`）。引擎构造风控上下文时 `extra={"current_time": datetime.now().strftime("%H:%M:%S")}`（`engine.py:1618`），全程未写入 `prev_close`；默认风控集注释自述 `price_limit(无prev_close自动跳过)`（`engine.py:1541`）。配置却声明 `allow_buy_limit_up: false` / `allow_sell_limit_down: false`（`live_config.yaml:3325-3326`）。
- **推断**：事前涨跌停拦截在任何路径下都恒跳过，配置声明与运行行为不符；唯一兜底是券商适配器侧报价校验（`qmt_broker.py:1341-1348`），该处又依赖同样可能存在偏差的昨收。
- **未知**：行情源是否另有昨收字段可注入；`exclude_st`/`exclude_suspend` 是否在别处生效。
- **风险影响**：涨停追高买入、跌停无法卖出的错误委托直达券商，产生废单与滑点损失，事前风控形同虚设。
- **可能性**：控制完全缺失
- **根因**：技术——规则所需入参从未装配，缺失时 fail-open 放行。
- **整改建议**：`engine.py:_build_risk_context` 从行情快照注入 `extra["prev_close"]`；`PriceLimitRule` 缺昨收时改为 fail-closed（拒绝并告警）而非跳过。
- **验证方法（DoD）**：单测构造涨幅 10% 的买单价，断言被拒；再断言缺 prev_close 时返回拒绝且产生告警而非放行。
- **最坏情形**：连续涨停板追单成交，持仓成本严重偏离策略预期。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-05 熔断强平实际仅 dry-run，返回值却标记为已执行

- **维度**：事前风控 / 应急响应
- **位置**：`myquant/live/engine.py:1797-1800`；`myquant/live/emergency_liquidation.py:62-69`；`myquant/risk/guardian.py:500-514`；`config/live_config.yaml:3324`
- **证据等级**：A（调用参数、服务默认值、回调返回处理三处原文已核对）
- **优先级 / 风险等级**：P1 高
- **事实**：引擎强平回调固定传 `confirm=False`，注释自述「只记录/生成计划，不下真单」（`engine.py:1797-1800`）；`EmergencyLiquidationService.execute` 默认 `confirm: bool = False` 即 dry_run，仅在 `confirm=True` 时才真实下单（`emergency_liquidation.py:62-69`）。守护侧无回调时 `logger.warning("v1仅记录，未实际执行")` 后仍 `return True`（`guardian.py:513-514`）；有回调时把回调返回值赋给 `decision.executed`（`:502-503`）。配置声明最大回撤 15%「触发后清仓停止」（`live_config.yaml:3324`）。
- **推断**：所谓熔断清仓不产生任何委托，而决策记录与告警显示「已执行」，运维与审计将误判持仓已清空，真实敞口继续裸露。
- **未知**：人工 SOP（`scripts/一键平仓.py --confirm`）是否被纳入值班流程并留痕。
- **风险影响**：极端行情下回撤失控且无人接手；留痕与真实状态相反，事后追责与合规举证困难。
- **可能性**：控制存在未执行
- **根因**：技术——安全默认（dry_run）与「已执行」语义未区分，返回值滥用。
- **整改建议**：`_on_force_close` 明确返回执行模式（`mode=dry_run` 时返回 False 或独立状态）；`guardian.py:513` 改为返回 False；在实盘模式装配 `confirm=True` 的人工确认通道并落审计日志。
- **验证方法（DoD）**：单测断言 dry_run 下 `decision.executed is False` 且 CRITICAL 告警文本含「未平仓」；实盘演练验证确认通道能真实下单。
- **最坏情形**：报告称已清仓而实际满仓，回撤击穿清盘线。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-06 异常交易类风控整体缺失（自成交、撤单率、胖手指）

- **维度**：事前风控 / 异常交易
- **位置**：`myquant/risk/rules.py:512-524`；`myquant/risk/risk_manager.py:512-517,583-588`
- **证据等级**：A（注册表全量清点 + 全项目关键词检索零命中，可复核）
- **优先级 / 风险等级**：P1 高
- **事实**：`RULE_REGISTRY` 共 11 条：position_limit、single_order_limit、max_drawdown、daily_loss_limit、stop_loss、take_profit、rate_limit、price_limit、trading_hours、blacklist、cash_availability（`rules.py:512-524`）。全项目检索 `self_trade|撤单率|cancel_rate|fat_finger|胖手指|wash_trade|异常交易` 无任何命中。`risk_manager.py:512` 仅在 PAUSED 状态禁止 BUY；`:583-588` 自述持仓监控「v1只发出警告，不自动平仓」。
- **推断**：自成交（同标的买卖对手单）、高频撤单率、胖手指（异常大额或异常价格偏离）三类控制的判定逻辑、阈值与拦截点均不存在，既无事前拦截也无可疑行为留痕与报告。
- **未知**：券商柜台或 QMT 客户端是否自带异常交易拦截；是否另有风控在策略层实现。
- **风险影响**：可能被交易所认定异常交易行为而受警示、限制交易甚至纪律处分，合规连续性中断；胖手指可造成单笔重大资金损失。
- **可能性**：控制完全缺失
- **根因**：合规——规则库未覆盖异常交易行为监管要求，属设计缺口。
- **整改建议**：在 `myquant/risk/rules.py` 新增 `SelfTradeRule`（同标的反向未成交单互斥）、`CancelRateRule`（当日撤单率阈值）、`FatFingerRule`（单笔金额与价格偏离阈值）并注册进 `RULE_REGISTRY`；在 `risk_manager.py` 增加行为留痕与日报汇总。
- **验证方法（DoD）**：三条规则各有单测覆盖命中/未命中；风控日报输出当日撤单率与超阈值事件清单。
- **最坏情形**：账户被限制交易或处罚，客户资产与牌照声誉同步受损。
- **是否需法律顾问确认**：是（异常交易行为认定标准与报告义务的法规适用性需确认）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-07 回测未执行实盘风控，且撮合规则与实盘不一致

- **维度**：回测与实盘一致性
- **位置**：`myquant/backtest/engine.py:220-225`；`myquant/execution/sim_broker.py:65-71,240-277,292-304`；`myquant/backtest/mixed_engine.py:491-505`
- **证据等级**：A（券商构造参数、撮合分支、另建止损逻辑三处原文已核对，backtest 目录检索风控类零命中）
- **优先级 / 风险等级**：P1 高
- **事实**：回测引擎仅以 `SimBroker(initial_capital=..., commission_model=..., slippage_model=...)` 构造券商并 `connect()`（`engine.py:220-225`），未装配 RiskManager（backtest 目录检索 `RiskManager|risk_manager|RiskRuleLibrary` 零命中）。`SimBroker.__init__` 无 `enable_t1`/`lot_size`/`price_limit_pct` 参数（`sim_broker.py:65-71`）；`on_bar` 仅按市价单取开盘价、限价单比高低价撮合（`:240-277,292-304`），无涨跌停、停牌与 T+1 校验。`mixed_engine.py:491-505` 另有自实现 `_check_stop_loss`，以「最低价触及成本价×(1-stop_loss_pct)」近似盘中成交，与实盘 StopLossRule 口径不同。
- **推断**：回测成交假设宽于实盘，实盘的事前拒单与交易规则限制未计入，回测—实盘一致性与仿真准入结论不成立。
- **未知**：`config_builder` 是否将交易规则透传到实盘侧参数（本批未逐条核对）；是否存在未检索到的回测风控包装。
- **风险影响**：以偏乐观的回测绩效配置资金与参数，实盘表现系统性劣于预期，客户资产承担未披露偏差。
- **可能性**：控制完全缺失
- **根因**：技术——回测与实盘两套执行链路未共享同一规则实现。
- **整改建议**：`backtest/engine.py:220` 复用实盘 `RiskManager`（同配置源）作为下单前置检查；`SimBroker` 增加 `enable_t1/lot_size/price_limit_pct` 并在 `_match_order` 内执行；`mixed_engine` 止损改调同一规则模块。
- **验证方法（DoD）**：同一策略在回测与纸面实盘跑同一区间，断言拒单原因分布与成交价口径可比；单测断言涨停日回测不成交。
- **最坏情形**：以失真回测上线，实盘回撤远超预期，触发客户赎回与投诉。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-08 风控资金与持仓基准非券商权威账本

- **维度**：事前风控 / 账本一致性
- **位置**：`myquant/live/engine.py:1578-1588`；`myquant/live/portfolio.py:478-498`
- **证据等级**：A（上下文取值与同步函数原文已核对；`sync_from_broker` 全项目检索无生产调用者）
- **优先级 / 风险等级**：P1 高
- **事实**：`_build_risk_context` 全部取进程内 `self._portfolio`：`cash = float(self._portfolio.available_cash)`、`total_assets = float(self._portfolio.get_total_assets())`、持仓遍历 `self._portfolio.get_all_positions()`（`engine.py:1579-1588`），position_limit / cash_availability / daily_loss_limit 均以此为基准。`portfolio.sync_from_broker`（`portfolio.py:478-498`）能按 broker 的 `get_account()`/`get_positions()` 覆盖现金与持仓（`:488-499`），但全项目检索仅测试 `tests/unit/test_live_concurrency.py:118` 调用，生产链路无调用者。
- **推断**：风控决策基于进程账本而非券商权威账本；成交回报丢失、部分成交或人工干预后，风控可用资金与持仓和券商漂移，漂移随交易次数累积。
- **未知**：是否存在定时对账任务；券商回报失败率与补偿机制（未取得运行日志）。
- **风险影响**：可能出现超买、超卖或卖空被拒，风控指标失真，对账无法闭环，客户资产与账实不符。
- **可能性**：控制存在未执行
- **根因**：技术——已实现的权威账本同步未接入运行循环。
- **整改建议**：在 `engine` 的定时任务（与快照同频）中周期调用 `portfolio.sync_from_broker(broker)`，并对账差异超阈值时 PAUSE 新开仓并告警。
- **验证方法（DoD）**：单测使 broker 账本与 portfolio 故意不一致，断言同步后一致且超阈值触发 PAUSE；运行日志出现周期对账记录。
- **最坏情形**：风控认为资金充足而实际已透支，或被拒单导致无法及时平仓。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-09 QmtBroker 仿真撮合价与昨收基准错误

- **维度**：交易执行 / 行情驱动
- **位置**：`myquant/live/brokers/qmt_broker.py:1064-1066,1081-1082,1341-1348`；`myquant/live/brokers/manager.py:381-391`
- **证据等级**：A（三处原文已逐字核对）
- **优先级 / 风险等级**：P2 中
- **事实**：`update_market_price` 内「首次行情记录为昨收基准」：`if symbol not in self._prev_close or self._prev_close[symbol] <= 0: self._prev_close[symbol] = price`（`qmt_broker.py:1064-1066`），昨收被写成当日首个行情价；随后对活跃订单以当前 `price` 撮合：`trade = self._match_order(order, price, timestamp)`（`:1081-1082`）。价格限制校验直接使用被污染的 `prev_close`（`:1341-1348`）。券商管理器对无 `on_bar` 的券商取日线收盘价作为成交价：`close = float(bar.get("close", 0) or 0)` → `broker.update_market_price(symbol, close)`（`manager.py:384-391`）。
- **推断**：仿真成交价偏离真实可成交价，涨跌停基准随当日首个行情漂移，涨跌停判定与撮合结果均不可信，仿真结论不能作为实盘准入依据。
- **未知**：QMT 实盘模式是否走同一 `update_market_price` 路径，抑或由柜台回报驱动（未取得实连验证）。
- **风险影响**：仿真绩效失真导致参数与资金配置误判；涨跌停误判产生废单或漏拦截。
- **可能性**：控制存在未执行（有校验函数但取数基准错误）
- **根因**：技术——昨收基准缺独立数据源，用「首笔行情」近似。
- **整改建议**：`qmt_broker.py` 昨收改由行情快照/日线前收字段注入（含跨日重置）；`manager.py:384-391` 改为传入 bar 的开盘价或明确标注「收盘价近似」并在报告中披露；涨跌停校验缺真实昨收时 fail-closed。
- **验证方法（DoD）**：单测喂入两根不同日 bar，断言昨收等于前一日收盘而非当日首笔；断言缺昨收时校验拒绝下单。
- **最坏情形**：以失真的仿真结果放行实盘资金，实盘首日即出现大幅偏差。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P3-10 交易日历缺失，非交易日可通过安全门

- **维度**：事前风控 / 交易时段
- **位置**：`run_实盘入口.py:93-105`；`myquant/risk/rules.py:438-443`；`myquant/utils/trade_calendar.py:37,70`
- **证据等级**：A（入口判定、规则判定与既有日历模块原文均已核对）
- **优先级 / 风险等级**：P2 中
- **事实**：实盘入口 `is_trading_time()` 仅判 `if now.weekday() >= 5: return False` 加两个时段区间（`run_实盘入口.py:99-105`），文档字符串自述「节假日需交易日历判断，此处简化」（`:96`）。`TradingHoursRule._is_trading_time` 同样只比对时段、不含日期维度（`rules.py:438-443`），且引擎默认风控集中 `trading_hours` 为 `{"enabled": False}`（`engine.py:1562`）。项目已具备真实日历 `myquant/utils/trade_calendar.py:37,70`（`load_trade_calendar`/`sync_trade_calendar`）与 `data/trade_calendar.csv`，但实盘入口未引用。
- **推断**：国庆、春节等工作日节假日被判为「可交易」，实盘安全门放行下单与启动流程，委托将被柜台拒回并污染对账；同时存在「工作日近似」与「真实日历」两套交易日语义并存。
- **未知**：是否存在未检索到的其他实盘时段校验；`data/trade_calendar.csv` 的覆盖范围与更新时效。
- **风险影响**：非交易日产生大量废单与告警噪音，掩盖真实异常；交易日语义不一致使日切、日报与结算错位。
- **可能性**：部分执行偶发失效（节假日必现）
- **根因**：技术——真实日历模块已实现但入口与规则层未接线。
- **整改建议**：`run_实盘入口.py:93` 改调 `myquant.utils.trade_calendar.load_trade_calendar` + `utils/time_utils.is_trading_day`；`rules.py:438` 增加交易日维度；实盘风控配置将 `trading_hours` 置为启用。
- **验证方法（DoD）**：单测以国庆调休工作日为输入，断言入口返回 False、规则拒绝下单；配置审查确认 `trading_hours.enabled` 为 true。
- **最坏情形**：节假日无人值守下自动下单被拒并触发误告警，真实故障被噪音淹没。
- **是否需法律顾问确认**：是（非交易日委托与交易所交易时间规则的适用性需确认）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认
