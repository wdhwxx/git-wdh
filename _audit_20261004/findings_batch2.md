# 第2批 详细发现（策略与模型、因子、数据、回测与仿真）

## 阅读说明

本文件为审计工作底稿草案，记录审计人员对 D:\Projects\Pythonwdhmm 在 2026-10-04 文件系统快照下的只读取证结果，不构成鉴证结论，不含签字与保证性表述。

**证据等级口径（A–E，依 v3.0 提示词第九章 9.2 全局唯一表）**：A=直接可核验物（代码/配置/日志原文，路径与行号可逐字复核，含 hash 可验）；B=文档/配置/截图等间接材料；C=基于材料的合理推断（含仅目录级观察）；D=材料缺失；E=单方陈述未经佐证（含无真实性声明的 hash）。本批证据主要来自静态代码与配置原文，故以 A 级为主；凡结论需额外假设或仅有项目文档互证者标 B/C，并在「未知」中说明。

**「依赖单方声明」**：指该发现所依据的关键事实仅来自被审计方自身的代码注释、文档声明或口头说明，审计方未能取得可独立验证的外部材料（如监管备案、行情供应商回执、历史版本、运行台账）。凡标注依赖单方声明的事项，其真实性未经独立验证，需在后续批次补充外部证据或由管理层书面确认。

**降级口径提示**：本次审计未取得 commit hash、重要性水平、抽样口径与材料真实性声明，故本批不作整体结论，仅按发现逐条列示事实、推断、未知与整改要求。

---

### KP-P2-01 三因子股票池缺 ST/停牌/新股过滤且取前比例与规格不符

- **维度**：策略与模型 / 因子与选股 / 合规
- **位置**：myquant/strategies/base.py:581-673（581、588-591、609-618、620-655、657-673）；myquant/strategies/ma_strategy.py:88、143；docs/strategy_specs.md:65-78
- **证据等级**：B（代码与规格文档原文均可复核且相互矛盾，但无历史 ST/停牌名单以量化实际影响）
- **优先级 / 风险等级**：P1 高
- **事实**：`StockPoolBuilder.build` 的完整流程为 `filter_by_code → calculate_factors → select_by_intersection`，`filter_by_code` 仅按代码前缀排除 `DEFAULT_EXCLUDE_PREFIXES`（688/300/301/4/8 等）。`select_by_intersection` 取 `top_n = max(1, int(n * self.top_ratio))`。构造器默认 `top_ratio: float = 1.0 / 3.0`，但 `ma_strategy.py:88` 传入 `pool_top_ratio: float = 0.5,  # 各因子取前50%（原1/3太严格导致交集太小）`，并在 143 行 `StockPoolBuilder(top_ratio=pool_top_ratio)`。`docs/strategy_specs.md:65-67` 规定三因子各「取前1/3」，第 78 行明确要求「排除：ST 股、停牌股、上市不足 60 天新股、日均成交量 < 100 万股」。全仓库未检索到 `pool_top_ratio` 的配置项，`config/backtest_kunpeng.yaml` 仅有 `benchmark` 与 `adjust` 两处相关配置。
- **推断**：生产路径最终以 0.5 而非规格 1/3 构建股票池，规格文档与实现存在确定性偏差；ST/停牌/新股/流动性四类可交易性过滤在选股代码中均未实现，规格第 78 行为未落地条款。as_of 截断（696-701 行）只解决未来数据问题，不解决可交易性问题。
- **未知**：datasets/processed 清洗环节的实际排除规则与排除时点；样本期间 ST 股与停牌股在池中的实际占比；规格文档 1/3 与代码 0.5 孰为业绩口径依据（docs/strategy_specs.md 全部业绩表未说明所用比例）；是否另有配置在运行期覆盖该参数。
- **风险影响**：回测池纳入当期不可买/不可持标的，收益率被系统性高估；若以该池上线实盘，可能生成 ST 股与停牌股订单，触发券商拒单、风控拦截或流动性损失；以非规格口径业绩对外披露构成合规风险。可审计性方面，池构建参数来源分散于代码默认值而非配置，无法从产出物反查口径。
- **可能性**：控制完全缺失（四类可交易性过滤在选股路径中不存在，且参数与规格不符）
- **根因**：技术 —— 池构建器仅实现代码前缀过滤与因子交集，未实现规格要求的可交易性校验层，且比例默认值被策略层覆写。
- **整改建议**：在 `myquant/strategies/base.py` 的 `StockPoolBuilder.build` 中新增 `filter_by_tradability(symbols, as_of)`，按当时点校验 ST 状态、停牌状态、上市满 60 天、20 日均量 ≥ 100 万股；将 `myquant/strategies/ma_strategy.py:88` 的默认 `pool_top_ratio` 改回 `1.0/3.0`，并在 `config/backtest_kunpeng.yaml` 显式增加 `pool_top_ratio: 0.3333`，使口径可由配置审计。
- **验证方法（DoD）**：代码走查确认新过滤方法存在且被 `build` 调用；单元测试构造含 ST、停牌、上市 30 天、低量标的的样本，断言其全部落入排除集；以 1/3 重跑回测，核对日志中 `三因子交集` 的 `低波动 ∩ 高价 ∩ 高动量` 数值等于 `int(n/3)`。
- **最坏情形**：实盘按含 ST/停牌股的池下单，出现大面积废单与无法卖出，同时对外披露的历史收益不可复现。
- **是否需法律顾问确认**：是（涉及业绩披露口径与适当性材料的准确性）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-02 event 引擎 SimBroker 完全不校验涨跌停与停牌

- **维度**：交易仿真 / 风控
- **位置**：myquant/execution/sim_broker.py:279-348（292-308、313-314）；config/backtest_kunpeng.yaml:3240-3241
- **证据等级**：B（撮合代码与配置声明原文明确，但无实盘废单记录印证偏差幅度）
- **优先级 / 风险等级**：P1 高
- **事实**：`_match_order` 对 `OrderType.MARKET` 直接取 `fill_price = bar.open`，限价单仅比较 `bar.low`/`bar.high`，随后 `fill_qty = order.remaining_qty`（313-314 行）并直接生成 Trade。整个方法及其调用链 `match_orders` 未出现涨跌停价判定，也未检查停牌或当日无成交。`config/backtest_kunpeng.yaml:3240-3241` 声明 `price_limit_pct: 0.1` 与 `enforce_limit_up_down: true`。对 `sim_broker.py` 检索 `price_limit|enforce_limit|limit_up|涨跌停|停牌` 无任何匹配，即该配置在本文件内无读取点。
- **推断**：配置声明与实际撮合行为不一致，涨跌停/停牌约束为零执行；bar 内数据虽可能隐含涨跌停（open 已等于涨停价），但「无法成交」这一真实约束不会被模拟，导致信号被无条件当作可成交。
- **未知**：event 引擎在何处装配 SimBroker、是否由上游订单生成层代做涨跌停拦截；回测数据是否含停牌标记列；`enforce_limit_up_down` 是否在其他模块被消费；样本期间涨停日买入信号占比及实际废单率。
- **风险影响**：涨停买入、跌停卖出在仿真中全部成交，回测对「买不进、卖不出」风险零暴露，收益与回撤同时失真；策略容量与最坏流动性风险被低估；上线实盘后成交率与滑点显著劣于回测，直接造成策略与实盘预期落差及客户资产损失。
- **可能性**：控制完全缺失（配置项声明开启，但撮合路径无任何执行代码）
- **根因**：技术 —— 撮合逻辑按「简化回测」实现，风险约束配置未接入 broker。
- **整改建议**：在 `myquant/execution/sim_broker.py` 的 `_match_order` 前置检查：以上一交易日收盘价 × (1 ± `price_limit_pct`) 计算涨跌停价，买单在 open ≥ 涨停价、卖单在 open ≤ 跌停价时返回 None（或按可成交量部分成交）；在 `SimBroker.__init__` 注入 `price_limit_pct`、`enforce_limit_up_down`，并新增 `is_suspended` 判定短路路径。
- **验证方法（DoD）**：代码走查确认配置项被读取；单元测试构造 open 等于涨停价的买入订单与停牌 bar，断言 `_match_order` 返回 None 且不产生 Trade；重跑含涨停日样本的回测，核对废单计数由 0 变为非 0 且报告披露该计数。
- **最坏情形**：策略在实盘连续涨停/停牌行情中反复下单无法成交，持仓无法止损退出，出现超出回测预期的实际亏损。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-03 撮合无成交量容量约束、无部分成交、允许负现金透支

- **维度**：交易仿真 / 资金与持仓控制
- **位置**：myquant/execution/sim_broker.py:313-314；myquant/backtest/vectorized_engine.py:544-559
- **证据等级**：B（代码原文可直接复核；配置 `max_total_position_pct: 0.95` 存在，但无资金不足实盘事故记录）
- **优先级 / 风险等级**：P2 中
- **事实**：sim_broker 中 `# 成交数量（默认全量成交）` 后 `fill_qty = order.remaining_qty`，未见任何 `bar.volume` 参与的数量上限或部分成交逻辑。vectorized_engine 中 `cash = sub_initial + np.cumsum(cashflow)`，随后 `# 资金约束检查（负现金视为杠杆，告警但保持计算）` 与 `if min_cash < -1.0: logger.warning("标的 %s 出现负现金 %.2f（策略目标超资金，按杠杆处理）")`，即负现金仅告警、不阻断、不缩减下单量，后续计算照常进行。配置 `max_total_position_pct: 0.95` 限制目标仓位，但引擎未对现金流做硬约束。
- **推断**：成交数量与市场成交量脱钩，成交价按 open/滑点固定，规模越大回测越乐观；负现金被当作杠杆继续计息式展开，等于允许无成本透支建仓，收益与回撤同时偏离真实资金约束下的结果。
- **未知**：策略实际换手率与单标的日均成交量；目标仓位在信号密集期是否超 100%；`cash < 0` 是否已在组合层被 `max_total_position_pct` 间接抑制；是否存在未纳入本次取证的容量校验分支。
- **风险影响**：回测收益在资金与流动性双重约束缺失下被高估，策略可承载规模无法据此测算；若以该结果配置实盘资金，可能出现下单资金不足被拒、被动降杠杆或强平，影响客户资产与交易安全。
- **可能性**：控制完全缺失（无容量约束、无部分成交、负现金不阻断）
- **根因**：技术 —— 撮合与资金模块按简化假设设计，未实现成交量占比上限与资金硬约束。
- **整改建议**：在 `myquant/execution/sim_broker.py` 的 `_match_order` 中按 `bar.volume × 参与率上限` 截断 `fill_qty` 并对剩余量生成未成交或部分成交状态；在 `myquant/backtest/vectorized_engine.py` 计算 `cash` 后对负现金执行按比例缩单或标记该 bar 不成交，并将超限计为可审计事件而非 `logger.warning`。
- **验证方法（DoD）**：代码走查确认 `bar.volume` 参与数量计算；单元测试构造下单量为当日成交量 10 倍的场景，断言成交量为上限值且存在部分成交记录；构造满仓后再买入场景，断言 `np.min(cash) >= 0` 且超限被计数。
- **最坏情形**：小市值或低流动性标的上按回测结果满仓建仓，实盘成交价严重偏离、无法减仓；组合层面出现实际资金透支与强制平仓。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-04 基准 000300.SH 数据缺失，alpha/beta/excess 从未计算

- **维度**：绩效评价 / 数据完整性 / 对外披露
- **位置**：myquant/backtest/engine.py:246-263、468-498；myquant/backtest/metrics.py:300-327；config/backtest_kunpeng.yaml:6
- **证据等级**：B（代码静默降级路径与配置原文明确；缺基准加载失败日志与产出物指标空值证据）
- **优先级 / 风险等级**：P1 高
- **事实**：`config/backtest_kunpeng.yaml:6` 声明 `benchmark: 000300.SH`。`engine.py:247-256` 在基准不在 `self.data` 时调用 `data_provider.get_bars(...)`，但 `257-261` 仅在 `not self._benchmark_data.empty` 时打印「基准数据已加载」，为空时无任何告警。`_calc_benchmark_equity` 开头即 `if self._benchmark_data is None or len(self._benchmark_data) == 0: return 1.0`，异常分支亦 `return 1.0` 并记 `logger.debug`。`metrics.py:304-327` 的 `_calc_benchmark_metrics` 仅在传入有效 `bench_df` 时才计算 `benchmark_return`、`excess_return`、`beta`、`alpha`，且 `len(common_idx) < 2` 时直接 `return`，无缺省告警。
- **推断**：基准净值被静默替换为常数 1.0，其收益恒为 0，超基准比较退化为策略绝对收益自比，alpha/beta/信息比率不可能生成有效值；「基准缺失」在 A 股行情数据中属可检测事实，但代码选择静默降级，使产出物无法区分「基准真实持平」与「基准未加载」。
- **未知**：datasets 或数据库内是否存在 000300.SH 行情；回测产出物（results JSON/报告中）alpha/beta/excess 字段实际是否为空或为 0；此前对外使用的「超额收益 +23.73%」（docs/strategy_specs.md:57）取自何口径；指数数据是否另有加载入口。
- **风险影响**：所有基于超额收益的业绩陈述缺乏计算依据，属于不可验证的对外表述，构成合规与披露风险；同时策略相对基准的选股能力无法评价，无法识别收益是来自个股选择还是市场 Beta。审计上表现为绩效指标不可复核。
- **可能性**：控制存在未执行（基准加载分支存在，实际未生效且无告警）
- **根因**：技术+流程 —— 基准数据源未纳入数据集治理，且缺失时以静默降级代替失败告警。
- **整改建议**：在 `myquant/backtest/engine.py:257` 将空数据分支改为 `logger.error` 并标记回测结果为 `benchmark_missing`；在 `myquant/backtest/metrics.py:308` 的 `return` 前写入缺基准告警与字段置空标记；补齐 000300.SH 日线数据并在 `config/backtest_kunpeng.yaml:6` 下增加 `benchmark_required: true`，缺基准即令回测失败退出。
- **验证方法（DoD）**：运行一次回测，确认日志出现基准加载条数，产出物 `alpha`、`beta`、`excess_return` 均为非空数值；人为移除基准数据重跑，确认回测被标记失败而非输出 1.0 基准。
- **最坏情形**：对外持续披露无计算依据的超额收益，策略实际仅为高 Beta 暴露，市场转向时出现远超预期的回撤。
- **是否需法律顾问确认**：是（涉及对外业绩与超额收益表述的准确性）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-05 参数扫描全样本择优，无样本外/PBO/walk-forward

- **维度**：模型验证 / 参数选择
- **位置**：myquant/backtest/kunpeng_param_scanner.py:50-64、66-67、218-236；myquant/backtest/param_optimizer.py:296-334
- **证据等级**：B（网格、排序与择优代码原文可复核；无独立样本外结果可对照）
- **优先级 / 风险等级**：P1 高
- **事实**：`SIGNAL_GRID` 与 `EXIT_GRID`（50-64 行）合计约 5×3×3×3×2 与 3×3×3×3 组参数；`START_DATE: str = "2024-01-01"` 且 `end_date: None`（212-214 行落盘 `"end_date": None`），即全区间扫描。`run` 在 223-228 行对全部组合在全样本上评估，229-236 行 `stage_rows.sort(key=lambda r: (-(r["n_signal"] >= max(3, len(data)//3)), -(r["avg_sharpe"] ...)))`，即按同一全样本的夏普直接取 Top，随后 `logger.info(...Top5)`。`param_optimizer.py:314-328` 的 `ranked` 亦仅按 `self.objective` 对 `self.trials` 全量排序取前 `top`，`best()` 返回 `ranked(top=1)[0]`。仓库内未见 PBO、walk-forward、样本外保留期的实现或调用。
- **推断**：参数选择与参数评价使用同一数据区间，排序指标（信号数、平均夏普）即为被择优指标，存在典型多重比较与过拟合；全网格中的极端组合必然被选出，其样本内夏普不可外推。
- **未知**：实际选出参数与其样本外表现；扫描是否另设验证区间（如按阶段B参数在阶段A参数结果上再来一次择优）；`avg_sharpe` 的样本量是否足以支撑排序；产出报告中是否提示过拟合风险。
- **风险影响**：以样本内最优参数上线，实盘收益显著低于回测甚至亏损；参数选择过程无可审计记录，无法向客户或合规解释参数来源；后续参数调整缺乏可复现的评价框架。
- **可能性**：已有事件（既成事实：现有扫描与择优逻辑已在全样本上执行）
- **根因**：技术+流程 —— 缺少样本切分与过拟合度量的方法论约束，择优流程未设留出集。
- **整改建议**：在 `myquant/backtest/kunpeng_param_scanner.py` 增加 `IS_END`/`OOS_START` 切分，网格仅在样本内评估，最终 Top 参数在样本外区间复算并同时落盘；在 `myquant/backtest/param_optimizer.py` 增加 PBO 或 deflated Sharpe 计算，并在 `best()` 返回值中附加 `oos_metric` 与 `pbo` 字段。
- **验证方法（DoD）**：代码走查确认样本切分与样本外复算路径；运行扫描，确认 `scan_results.json` 中每条候选同时含样本内与样本外指标，且最终推荐参数的样本外指标一并输出。
- **最坏情形**：按过拟合参数投入实盘资金，出现大幅且持续的回撤，客户资产受损后无法说明参数选择依据。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-06 参数扫描标的池按成交笔数内生挑选，引入选择偏差

- **维度**：模型验证 / 抽样方法
- **位置**：myquant/backtest/kunpeng_param_scanner.py:91-101、196-199
- **证据等级**：B（筛选条件代码原文可复核；无替代口径结果对照以量化偏差）
- **优先级 / 风险等级**：P2 中
- **事实**：`load_active_symbols` 读取 `results/batch_backtest/kunpeng/results.json`，过滤 `v.get("status") == "completed" and (v.get("trade_count") or 0) >= min_trades`，按 `rows.sort(key=lambda x: -x[1])` 取 `top_n=50`；`run` 中 `symbols = self.load_active_symbols(top_n=top_n, min_trades=min_trades)` 后直接 `data = self.load_data(symbols)` 并进入网格评估。即「活跃标的」定义为在**同一历史样本**上成交笔数多者，排名依据正是待评价指标族的一阶观测量。
- **推断**：标的池由样本内表现内生决定，参数在「历史高换手子集」上择优，属条件选择偏差；该子集的信号密度天然更高，参数阈值（如 `vol_ratio_threshold`、`diff_dea_ratio`）会向该子集过度拟合，样本外或全市场应用中信号数骤降。
- **未知**：全池 `results.json` 的生成口径与剔除规则；被排除标的（`trade_count < 3` 或非 completed）的占比；是否存在按行业/市值分层的外生池构建；`top_n=50` 的依据。
- **风险影响**：参数在真实全市场（约 820 只）上的适用性未经验证，可能导致信号稀疏或过度交易；最坏情况下选中参数仅在历史活跃子集有效，全市场部署后业绩不可复现，属可审计性缺陷。
- **可能性**：控制完全缺失（标的池选择完全由内生成交笔数决定，无外生约束）
- **根因**：技术+流程 —— 为降低算力消耗选取活跃标的，未对选择偏差做方法论约束或披露。
- **整改建议**：在 `myquant/backtest/kunpeng_param_scanner.py:91-101` 将标的池改为外生口径（如按扫描起始日前一段区间的流动性/市值/指数成分分层抽样），或在保留内生池的同时强制在同一参数上并行输出全市场子集结果用于对照，并在 `meta` 中记录池构建依据。
- **验证方法（DoD）**：代码走查确认池构建不再依赖当期样本的 `trade_count` 排序；运行扫描后对比活跃池与全市场子集上同一参数组的信号数与平均夏普，差异纳入产出报告。
- **最坏情形**：参数仅在少数活跃股上有效，实盘全市场部署后信号不足或集中持仓少数标的，风险敞口与预期严重偏离。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-07 幸存者偏差有工具无接入，退市覆盖率为零且无折价

- **维度**：数据完整性 / 回测口径 / 对外披露
- **位置**：myquant/backtest/survivorship_bias.py:5-29、31-34、209-218；myquant/data_access/full_clean_service.py:180-217
- **证据等级**：B（模块自述与清洗代码原文可复核；缺退市股数据与折价计算产出物）
- **优先级 / 风险等级**：P2 中
- **事实**：`survivorship_bias.py` 模块注释自述问题链：「数据源头 = 当前存续 A 股列表（akshare/通达信 get_stock_list 快照），退市/已摘牌股票从未入库 → DB 中不存在历史退市股行情」「回测池 = datasets/processed = 清洗后存续池……回测收益系统性偏乐观（幸存者偏差）」，并声明「未来接入退市数据无缝生效」。`audit_data_coverage` 中硬编码 `delisted_coverage = False`（209 行），声明文本为「历史退市/已摘牌股票未纳入（覆盖率=0），存在幸存者偏差——回测收益可能系统性偏乐观」，`estimate_survivorship_discount` 仅在文本中被指向。`full_clean_service.py:186-217` 的 `filter_long_suspended_symbols` 按 `market_data(daily, raw)` 的 `MAX(trade_date)` 距今超过阈值剔除标的，退市股因从未入库而不在候选集内。回测引擎路径未见对上述审计/折价函数的调用。
- **推断**：模块属于「已编写未接入」状态，偏差披露仅存在于注释与一个 `False` 常量，实际回测报告未附折价量与披露文本；停牌近似池无法替代真实退市损失（模块注释亦承认真退市损失通常更极端）。
- **未知**：`survivorship_bias.py` 的调用方与是否进入报告链路；`estimate_survivorship_discount` 的实际输出数值；被剔除的长期停牌标的数量；是否存在退市股历史的替代数据源。
- **风险影响**：回测收益与真实可实现收益之间存在方向确定、幅度未量化的系统性高估，直接削弱策略上线决策的可靠性；对外披露若不含口径声明与折价，属披露不完整。
- **可能性**：已有事件（数据源从未包含退市股，偏差自数据库建立即持续存在）
- **根因**：技术+治理 —— 数据采集口径未覆盖完整历史股票全集，偏差工具完成后未纳入报告流程。
- **整改建议**：在回测报告生成路径（`myquant/backtest/engine.py` 与 `mixed_engine.py` 的结果收尾处）调用 `audit_data_coverage` 与 `estimate_survivorship_discount`，将折价系数与声明文本写入结果 `extra` 并强制输出；数据侧将含退市股的历史行情纳入 `datasets/processed`，使 `build_point_in_time_pool` 覆盖真实退市标的。
- **验证方法（DoD）**：运行一次回测，确认产出物中含 `delisted_coverage`、停牌近似池数量、折价系数与口径声明四项；抽查任一 as_of 时点的池，确认不含该时点之后才退市的标的。
- **最坏情形**：基于系统性高估的历史收益配置实盘资金与客户预期，实际业绩长期不达标，引发客户纠纷与声誉损失。
- **是否需法律顾问确认**：是（涉及业绩口径声明与投资者披露完整性）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-08 防未来校验空转：铁律1/2/4 无有效调用，轻量校验仅时钟倒退

- **维度**：模型验证 / 内部控制 / 防未来函数
- **位置**：myquant/core/lookahead_guard.py:13-21、186-230；myquant/backtest/mixed_engine.py:1008-1049；myquant/core/light_lookahead_guard.py:312-437、441-443
- **证据等级**：B（自述与全仓库调用点检索结果一致可复核；无未来函数实际发生的事故证据）
- **优先级 / 风险等级**：P1 高
- **事实**：`lookahead_guard.py:19-21` 自述「P2-D7-003 整改声明：铁律1由结构……+测试锚点保障；铁律2由止损逻辑顺序保障；guard 运行期记录覆盖铁律3/4（record_indicator/record_stop_loss 无生产调用，蓝图已如实声明）」。全仓库检索确认 `record_indicator` 与 `record_stop_loss` 的定义外无任何调用点；`record_signal` 仅被 `myquant/backtest/engine.py:446`、`mixed_engine.py:833`、`vectorized_engine.py:575`、`myquant/live/engine.py:1443` 调用。`light_lookahead_guard.py` 的 `check_indicator_window`（铁律1）与 `check_signal_execution`（铁律3）除模块自身文档串与 590-667 行自测代码外，无生产调用。`mixed_engine.py:1028-1049` 的 `_run_light_guard_check` 仅以 `lg.on_bar_close(ts)` 沿 `self._bar_timestamps` 推进时钟并 `lg.assert_pass()`，即实际生效的仅为时间线单调（时钟倒退）校验。
- **推断**：防未来函数体系在运行期基本空转——两处 guard 的校验方法未被调用，能触发的违规类型仅为时间戳乱序；「防未来函数校验通过」的日志与 `lookahead_guard_passed=True` 标记会给出高于实际保障程度的控制有效性印象。
- **未知**：`LOOKAHEAD_GUARD_STRICT` 在生产环境的实际取值；`assert_pass` 通过的 `record_signal` 数据是否足以覆盖铁律3；铁律1/2/4 是否另有非 guard 的结构性保障（如 RSRS 闭式回归）；是否有测试在 CI 中强制校验这些方法被调用。
- **风险影响**：未来函数类缺陷（指标含当日、因子用未来数据）无法被自动拦截，回测结果可能建立在不可实现的信息之上；对外与对内声称的「四道防线」与实现不符，属控制设计缺陷与可审计性问题。
- **可能性**：控制存在未执行（校验方法已实现但生产路径无调用，运行期不触发）
- **根因**：技术+流程 —— 校验钩子实现后未接入指标计算、因子与止损各调用点，验收仅依赖文档声明。
- **整改建议**：在指标计算点（`myquant/strategies/ma_strategy.py` 的 RSRS 计算入口与 `myquant/core/lookahead_guard.py:186` 契约处）调用 `record_indicator`；在止损更新处调用 `record_stop_loss`；将 `light_lookahead_guard.check_indicator_window` 与 `check_signal_execution` 接入 `mixed_engine` 的 on_bar 与成交回报路径；在 `mixed_engine.py:1011` 的控制通过日志中同时输出各铁律的实际调用计数。
- **验证方法（DoD）**：代码走查确认四个调用点存在；故意在因子计算中引入一日未来数据并运行回测，断言 guard 报违规且结果被 `mark_failed`；运行正常回测，断言日志中各铁律调用计数 > 0。
- **最坏情形**：核心策略长期依赖未来信息生成信号，实盘无法复现历史收益，客户资产在未知风险敞口下运行。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-09 真实回测用不复权 raw 价（provider 静默降级），配置声明 hfq

- **维度**：数据口径 / 回测真实性 / 配置治理
- **位置**：config/backtest_kunpeng.yaml:3250；myquant/data_access/provider.py:197-201、465-468、482-484、515-517、519-556；myquant/backtest/kunpeng_param_scanner.py:105-119
- **证据等级**：C（配置声明与 provider 降级逻辑原文明确，且扫描器显式传 raw 可印证；尚无回测产出物的复权标记以对照）
- **优先级 / 风险等级**：P2 中
- **事实**：`config/backtest_kunpeng.yaml:3246-3250` 的 `data:` 段声明 `data_dir: ./datasets/processed`、`data_format: parquet`、`adjust: hfq`。`provider.py:465-468` 依 `requested_adjust = adjust or self._default_adjust`，仅当 `requested_adjust in ("hfq", "qfq")` 时才调用 `_apply_factor_adjust`；该函数在 `factors = self._get_factor_lookup(symbol, as_of=end_date)` 为空时执行 `logger.warning(f"{symbol} 无复权因子事件，adjust={adjust} 降级为不复权 raw")` 并 `return df`，即静默以 raw 继续。`_ensure_factor_svc`（482-484）在 DB 不存在时「静默降级，保持 None」；`_get_factor_lookup` 异常分支亦 `return {}`（515-517）。`provider.py:201` 默认 `default_adjust: str = "raw"`，`kunpeng_param_scanner.py:109-119` 显式以 `default_adjust="raw"`、`adjust="raw"` 加载。
- **推断**：`backtest_kunpeng.yaml` 的 `adjust: hfq` 声明对真实回测路径不产生约束力：参数扫描路径显式取 raw，其余路径在因子缺失时回落到 raw；跨除权除息日的价格序列不连续，价格类因子（`close.iloc[-1]`、20 日波动率、60 日动量）与收益计算均受污染。
- **未知**：数据库 `database/quant.db` 复权因子表的覆盖率；`backtest_kunpeng.yaml` 的 `data.adjust` 是否被任何加载器读取；结果产出物是否记录实际生效的复权方式；主回测入口的 provider 构造参数。
- **风险影响**：除权日出现约等于分红送转比例的虚假跳空，触发虚假动量与止损信号，使收益、回撤与信号统计同时失真；同一策略在 raw 与 hfq 口径下结果不可比，配置声明与实际执行不符，削弱审计可追溯性。
- **可能性**：部分执行偶发失效（复权路径存在，仅在有因子事件且因子可得时生效，否则静默回落）
- **根因**：技术+流程 —— 配置声明未与加载器接口绑定，且复权因子缺失以静默回落而非失败告警处理。
- **整改建议**：在数据加载入口（`myquant/data_access/provider.py` 与回测入口的 provider 构造处）读取 `config/backtest_kunpeng.yaml` 的 `data.adjust` 并强制生效；将 `provider.py:534` 的降级告警升级为可配置的硬失败（新增 `adjust_strict` 开关，缺因子即报错）；在结果 `extra` 中写入实际使用的 `adjust` 字段。
- **验证方法（DoD）**：代码走查确认配置项被加载器读取；对含除权日的标的分别以 raw 与 hfq 计算 60 日动量，确认二者在 hfq 下差异符合因子表；运行回测后核对产出物 `adjust` 字段为 `hfq`，且因子缺失时回测被标记失败而非静默继续。
- **最坏情形**：长期回测的收益与回撤因除权跳空而系统性失真，据以制定实盘仓位与止损参数，实际表现与回测严重偏离。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认
