# P0-2 SLI/SLO 初稿 + 数值阈值锁定清单

**编号**：P0-2
**版本**：V1.0（定稿版）
**日期**：2026-10-05
**依据**：《量化系统监控模块施工总清单_V1.2.md》P0-2 项（对应报告 §5.1）+ §10 数值阈值锁定表（R-04 唯一真源）
**事实来源**：本次落地。阈值取值以施工清单 §10 锁定表为准；**待实盘校准项保留显式占位，不编造数值**（对齐「真实数据、禁止杜撰」纪律）。
**定位**：监控施工前置规范第二项——SLI/SLO 初稿定稿 + 全部数值阈值一次性锁定，写入 `monitoring.slo` 配置块（唯一真源），各包实现只引用不自定义。

---

## 1. SLI / SLO 初稿表

| # | 领域 | SLI | SLO 目标 | 测量口径（P0-8 衔接） |
|---|------|-----|----------|----------------------|
| 1 | 行情 | 行情延迟 P99 | <5ms（本地），交易所时间戳偏差可控 | 行情时间戳→引擎接收时间差，系统时钟对齐 |
| 2 | 数据 | 数据新鲜度 | <1min，缺口=0 | 最新数据时间戳 vs 当前时间；按标的/数据类统计 |
| 3 | 数据 | 因子/财务同步延迟 | 分别设值（待校准） | 同步任务完成时间 vs 计划时间 |
| 4 | 信号 | 信号生成延迟 P99 | <50ms | 数据到达→信号产出，链路 id 计时（BASE-4） |
| 5 | 交易 | 订单发送到 ACK P99 | <20ms | 下单提交→券商 ACK 回包 |
| 6 | 交易 | 订单拒绝率 | <0.1%（窗口内） | 拒单数/报单数，事件驱动统计 |
| 7 | 风控 | 限额使用率 | <80% 预警、>95% 强告警 | 实际使用/限额，分集中度/杠杆/单票 |
| 8 | 对账 | 订单-成交-持仓-资金差异 | 实时=0、T+1=0 | reconcile_vs_broker 事件驱动核对，金额精确到分 |
| 9 | 策略 | 日内回撤/亏损 | 不超过策略阈值（待校准） | 滚动日内绩效，metrics_history 回放 |
| 10 | 模型 | PSI/KS/IC 漂移 | 超过基线触发预警（待校准） | model_monitor 滚动序列（已实现 PSI/KS 度量） |
| 11 | 回测一致性 | 回测实盘信号偏差率 | 上限 X%（按策略风格，待校准） | 同参数同时间窗逐笔对比 |
| 12 | 引擎心跳 | 无响应判定窗口 | N×间隔 <60s（硬约束） | get_status 连续无响应计时 |
| 13 | 资金 | 保证金占用/强平价距离 | 接近强平 → P0 告警（待校准） | 保证金占用率、强平价距离百分比 |
| 14 | 资金 | 资金费率 | 阈值待校准 | 持仓隔夜成本 |

---

## 2. 数值阈值锁定清单（§10 升格定稿 · R-04 唯一真源）

> 本表与 `config/monitoring.example.yaml` 的 `monitoring.slo` 块**同源**。状态：`LOCKED`=已锁定（实盘后不得改动）；`PENDING_CAL`=待实盘校准后填死。

| 领域 | SLI | 锁定值 | 锁定语义 | 状态 |
|------|-----|--------|----------|------|
| 行情 | 行情延迟 P99 | 5ms | 本地行情延迟，实盘校准复核 | LOCKED（初值） |
| 数据 | 数据新鲜度 | 60s、缺口 0 | 因子/财务同步延迟分别设值 | LOCKED |
| 数据 | 因子同步延迟 | — | 分别设值 | PENDING_CAL |
| 数据 | 财务同步延迟 | — | 分别设值 | PENDING_CAL |
| 信号 | 信号生成延迟 P99 | 50ms | — | LOCKED（初值） |
| 交易 | 订单 ACK P99 | 20ms | — | LOCKED（初值） |
| 交易 | 订单拒绝率 | 0.1% | 飙升倍数 N 与窗口长度一并锁定 | LOCKED（初值），N/窗口 PENDING_CAL |
| 风控 | 限额使用率 | 80% 预警 / 95% 强告警 | 集中度/杠杆/单票分别设值 | LOCKED；细分项 PENDING_CAL |
| 对账 | 实时差异 | 0（金额精确到分） | **A-7/B-1 统一取 0，禁止两处分别定义** | **LOCKED（硬约束）** |
| 对账 | T+1 差异 | 0（金额精确到分） | 同上 | **LOCKED（硬约束）** |
| 策略 | 日内回撤/亏损 | — | 按策略锁定 | PENDING_CAL |
| 模型 | PSI/KS/IC 漂移 | — | 锁定基线 | PENDING_CAL |
| 回测一致性 | 回测实盘偏差率 | — | 上限 X% + 计算窗口（按策略风格） | PENDING_CAL |
| 引擎心跳 | 判定窗口 | <60s | **N×心跳间隔 <60s 保 MTTD<1min**（R-02） | **LOCKED（硬约束）** |
| 资金 | 保证金占用率 | — | 强平距离百分比 | PENDING_CAL |
| 资金 | 强平价距离 | — | 接近强平 → P0 告警 | PENDING_CAL |
| 资金 | 资金费率 | — | 持仓隔夜成本 | PENDING_CAL |

**锁定语义要点**：
- **对账容差 = 0**：A-7 与 B-1 完全对齐，容差统一从 `monitoring.slo.reconciliation` 取，禁止 A-7/B-1 各自定义（R-04 直接落地）。
- **引擎心跳窗口 <60s**：判定窗口必须小于 1 分钟才能满足 P0 故障 MTTD<1min（R-02）。建议组态：间隔 10s × 连续 5 次 = 50s < 60s。
- **阈值唯一真源纪律**：后续 A/B/C/D 各包涉及阈值一律从 `monitoring.slo` 引用；验收（§9 KPI-12）核对配置与代码引用阈值 == 本表。

---

## 3. 配置块映射（monitoring.slo ↔ 锁定表）

`config/monitoring.example.yaml` → 复制为 `config/monitoring.yaml`（真实配置不入库）：

```yaml
monitoring:
  slo:
    market:        { quote_delay_p99_ms: 5, exchange_ts_bias: ~ }
    data:          { freshness_max_seconds: 60, gap_allowed: 0, factor_sync_delay_max_seconds: ~, financial_sync_delay_max_seconds: ~ }
    signal:        { generation_delay_p99_ms: 50 }
    trading:       { order_ack_delay_p99_ms: 20, order_reject_rate_max: 0.001, reject_rate_surge_multiple: ~, reject_rate_surge_window_seconds: ~ }
    risk:          { position_limit_usage_warn: 0.80, position_limit_usage_alert: 0.95, concentration_limit: ~, leverage_limit: ~, single_stock_position_limit: ~ }
    reconciliation:{ realtime_diff_tolerance: 0, t1_diff_tolerance: 0 }
    strategy:      { intraday_drawdown_limit: ~ }
    model:         { psi_threshold: ~, ks_threshold: ~, ic_threshold: ~ }
    backtest_consistency: { signal_deviation_rate_max: ~, deviation_window: ~ }
    signal_decay:  { ic_rolling_window: ~, ic_threshold: ~, density_min: ~, invalid_signal_ratio_max: ~ }   # C-4 施工期新增（回写#233）
    capacity:      { growth_projection_days: ~, storage_headroom_min_pct: ~, strategy_count_limit: ~ }          # C-8 施工期新增（回写#237）
    dynamic_baseline: { static_low: ~, static_high: ~ }                                                        # C-6 施工期新增（回写#235，另有 calibrator 算法参数块）
    regime_matcher:{ match_score_min: ~, trend_r2_min: ~ }                                                     # C-3 施工期新增（回写#242）
    engine_heartbeat: { hang_judge_window_seconds: 60, probe_interval_seconds: 10, consecutive_misses: 5 }
    margin:        { strong_flat_distance_pct: ~, funding_rate_threshold: ~, margin_usage_ratio_threshold: ~ }  # B-9 施工期补字段（回写#229）
    exchange_rate_limit: { window_seconds: 60, order_per_minute: 300, query_per_minute: 600, cancel_per_minute: 300, warn_ratio: 0.8 }  # A-12 施工期新增（回写#216）
```

> 同步注记（#252 审计）：本节映射已与 `config/settings.yaml` 实际 `monitoring.slo` 对齐——补齐施工期新增子块（signal_decay/capacity/dynamic_baseline/regime_matcher/exchange_rate_limit）与 margin.margin_usage_ratio_threshold 字段；§2 锁定表语义不变，各子块阈值均为 PENDING_CAL（待实盘校准后填死，禁止编造）。

---

## 4. 待实盘校准项（诚实占位，不编造数值）

以下项在实盘数据校准前**保持空占位（PENDING_CAL）**，校准方法同步锁定：

| 项 | 校准方法（建议） | 触发项 |
|----|------------------|--------|
| 行情延迟 P99 复核 | 实盘盘口压测取 P99 后复核 5ms 初值 | A-4/B-4 |
| 因子/财务同步延迟 | 记录同步任务实际耗时一周取 P95 | B-7 |
| 拒单率飙升倍数 N/窗口 | 历史拒单基线 + 波动分析 | B-5 |
| 集中度/杠杆/单票限额 | 按账户风控策略与资金规模设定 | B-3 |
| 日内回撤阈值 | 按策略回测历史回撤分布设定 | C-2 |
| PSI/KS/IC 基线 | 模型训练期分布 + 漂移观测（model_monitor 已有度量） | C-1/C-6 |
| 回测实盘偏差率上限 X%/窗口 | 按策略风格画像（P0-1）设定 | C-10 |
| 强平距离/资金费率 | 按券商保证金规则 + 合约费率 | B-9 |

---

## 5. 负责人与下一步

- **负责人**：单人系统（本人）。
- **下一步（硬顺序）**：**P0-8**（可测量性与故障注入规范，定义 MTTx 计时与 FI 用例集）→ BASE-1/3 → M1-Min。P0-8 的 MTTx 计时起点/终点口径与本表 SLI 测量口径对接。

---

## 附：核验记录

- 蓝图登记：config/ 段 `monitoring.example.yaml` + docs/ 段 `SLO与阈值锁定清单_P0-2.md`（回写 **#188**）。
- 本表与 `config/monitoring.example.yaml` 逐字段一致（同源）。
- 无业务代码改动；严格防未来（阈值仅锁定已确认约束，待校准项显式占位）。
