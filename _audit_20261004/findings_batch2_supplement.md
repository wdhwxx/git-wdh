# 审计工作底稿 · 补充发现（第2批 承前）

本文件补录在合并前序取证结论时识别到的两条 P0 级发现，作为第 2 批的追加底稿。
本文件为审计工作底稿草案，不是鉴证报告，不含签字与保证性结论。
审计基准：2026-10-04 文件系统快照（环境无法执行 git，未取得 commit hash；重要性水平、抽样口径、材料真实性声明均未提供，按降级口径编制）。

---

### KP-P2-10 生产撮合引擎的涨跌停不可成交约束恒为空转：Bar.pre_close 恒为 0

- **维度**：回测与仿真 / 实盘一致性
- **位置**：`myquant/backtest/mixed_engine.py:948`；`myquant/backtest/order_matcher.py:383-386,370`；`scripts/数据导出_DB到processed.py:109`；`myquant/data_access/provider.py:67,566-567`；`myquant/backtest/vectorized_engine.py:455-459`；`myquant/backtest/engine.py:694-702`
- **证据等级**：A（构造 Bar、撮合校验、导出 SQL 三处原文逐字核对，且全项目检索可复核）
- **优先级 / 风险等级**：P0 严重
- **事实**：生产主力引擎构造 Bar 时 `pre_close=float(row.get("前收盘价", 0))`（`mixed_engine.py:948`）。processed 数据的唯一导出 SQL 为 `SELECT trade_date, open, high, low, close, volume, amount, pct_chg, amplitude, volume_ratio, turnover, float_share, pe_dynamic`（`脚本:109`），**不含前收盘价**；`myquant/backtest/data_loader.py` 与该引擎均不从其它列派生 `前收盘价`（全项目检索 `前收盘价`：仅 provider.py 的映射表第 67 行与 docstring、financial_service 映射、cleaner 注释，**无任何写入点**）。因此 `row.get("前收盘价", 0)` 恒取默认值 0。撮合层校验为 `if self.enable_price_limit and bar.pre_close > 0:`（`order_matcher.py:384`），`pre_close=0` 时整段涨跌停判断被跳过（`order_matcher.py:370` 注释自述「pre_close<=0 时跳过涨跌停检查（数据不足）」）。对照之下 vectorized 引擎自带兜底：`if "pre_close" in df.columns: ... else: pre_closes = np.concatenate([[np.nan], closes[:-1]])`（`vectorized_engine.py:456-459`）；event 引擎构造 Bar 时根本不传 `pre_close`（`engine.py:694-702`）。
- **推断**：mixed（生产主力）与 event（自称真值基准）两引擎在回测中**不会拦截「涨停开盘买入、跌停开盘卖出」**，买单按开盘价成交；只有 vectorized 引擎因前收兜底而近似正确。三引擎在同一策略同一数据上的差异因此包含「可交易性约束是否生效」这一系统性口径差，而 `consistency_check.py` 的三模式对拍会把该差异当作可接受的数值误差。
- **未知**：processed 目录下历史 parquet 是否在早期版本曾含该列（本环境无 python 可执行，未列文件列名）；样本期内涨停开盘样本占比，故本发现未量化对历史绩效的具体影响幅度。
- **风险影响**：回测净值与实盘可执行性出现结构性高估——回测买在实盘买不进的涨停板上，收益与容量结论不可用于真钱决策；且该偏差方向单向乐观，不会自然抵消。
- **可能性**：控制完全缺失（校验条件在生产路径上恒为假，且缺失时静默跳过而非阻断）
- **根因**：技术 —— 撮合层的可交易性校验依赖一个导出管道并不存在的列，缺失时默认值 0 使校验静默失效，且三引擎各自构造 Bar 未统一。
- **整改建议**：①在 `scripts/数据导出_DB到processed.py:109` 的 SELECT 中补 `pre_close` 并重导 processed；②在 `myquant/backtest/data_loader.py` 加载层统一派生 `前收盘价 = close.shift(1)` 作为兜底并写回 DataFrame，使所有引擎共用同一列；③`order_matcher.py:384` 与 event 引擎在 `pre_close<=0` 时改为抛错或记 `warning` 并在 `result.extra` 标记 `price_limit_checked=False`，禁止静默跳过；④CI 增加「processed 列集合必须包含前收盘价」的契约断言。
- **验证方法（DoD）**：构造含涨停开盘日的样本，断言 mixed/vectorized/event 三引擎在该日买单成交数为 0 且撮合日志出现涨跌停拦截记录；对同一区间重跑并对比修正前后净值差异，差异纳入报告披露。
- **最坏情形**：对外披露的绩效包含大量实际不可成交的涨停买入，按该绩效放大资金实盘后无法复现，客户资金按错误预期承担风险。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P2-11 第二道防未来防线的失败结果会被 mark_complete 覆盖为「已完成」

- **维度**：回测与仿真 / 防未来函数
- **位置**：`myquant/backtest/mixed_engine.py:1012-1014,1028-1049,1091-1092`；`myquant/backtest/vectorized_engine.py:738-763`；`myquant/models/backtest.py:445-447`
- **证据等级**：A（返回值丢弃、状态机覆盖、时间线预排序三处原文逐字核对）
- **优先级 / 风险等级**：P0 严重
- **事实**：`_run_lookahead_assert` 执行 `self._run_light_guard_check()` 后**丢弃返回值并 `return True`**（`mixed_engine.py:1012-1014`）；`_run_light_guard_check` 的唯一实质动作是 `lg.reset(); for ts in self._bar_timestamps: lg.on_bar_close(ts); lg.assert_pass()`（`:1038-1041`），而 `self._bar_timestamps` 在构建时已 `all_bars.sort(key=lambda b: (b.timestamp, b.symbol))`（`:953`），**时间线必然单调**，故「时钟倒退」永不触发；`check_data_access` / `check_indicator_window` / `check_signal_execution` 在 `myquant/backtest/` 内无任何生产调用点。收尾阶段无论前述结果如何，均执行 `mark_complete()`（`:1091-1092`），而 `mark_complete` 无条件写入 `status="completed"`（`models/backtest.py:445-447`）。
- **推断**：**即使**防线记录到违规并调用 `mark_failed`，紧随其后的 `mark_complete` 仍会把状态改写为 completed，防线在状态机上不具备阻断能力；「防未来函数校验通过」这一标记因此不能作为回测结论有效性的证据。
- **未知**：`event` 引擎与批量回测路径是否以 `result.extra` 中的其它字段作为准入条件（本批仅在 mixed/vectorized 与 models 内核对状态语义）；`LookaheadGuard`（重校验器）在 event 路径的覆盖面。
- **风险影响**：含前视/乱序问题的回测不会被标记失败，而是以「已完成」进入绩效汇总、候选池与对外报告，污染标的入池与资金配置决策；同时使审计赖以判断回测有效性的状态字段失去证据价值。
- **可能性**：控制完全缺失（校验输入为常量、返回值被丢弃、失败状态被覆盖，三层同时失效）
- **根因**：技术 —— 校验器接线不完整（只接最小检查点）且返回值未与状态机语义串联，失败状态不具备终态性。
- **整改建议**：①`_run_lookahead_assert` 改为 `ok = self._run_light_guard_check(); return ok`；②在 `models/backtest.py:445-447` 的 `mark_complete` 增加 `if self.status == "failed": raise/return` 的终态保护；③在 mixed/vectorized 逐 bar 循环中真实调用 `check_indicator_window(calc_ts, data_end_ts)` 与 `check_signal_execution(signal_ts, exec_ts, price_type)`；④修正 `docs/project_layout_gear2.md:631` 中「无引擎消费方」的过期声明。
- **验证方法（DoD）**：构造故意读取未来数据的策略桩，断言回测以 `status="failed"` 收尾且 `result.extra["light_lookahead_passed"]` 不存在；补充单测覆盖 `mark_failed → mark_complete` 的顺序语义（断言后者不覆盖前者）。
- **最坏情形**：带未来函数的策略被判定为「防未来校验通过」并获得「已完成」绩效，据以投入真钱交易，产生实际亏损且无法向投资者说明绩效形成过程。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认
