# 存量 guard 资产重登记（M0B-01 / M0B-02 / M0B-03）

> 归属：未来函数与数据泄漏门禁 · 阶段 0-B（M0-B）
> 依据：《未来函数与数据泄漏门禁施工方案.md（v3.0 定稿）》§3（存量 guard 资产盘点，按实测效力三档登记）
> 登记口径：**从"文件存在"改为"运行期能否拦截"**——✅ 生产实测有效 / ⚠️ 仅自报（依赖被测方如实上报，可被绕过）/ ❌ 未接入（模块存在但运行期零调用）
> 生成日期：2026-10-10

## 一、存量 guard 资产表（§3 全量 14 行，按实测效力三档重登记）

| 层 | 已有门禁 | 位置 | 实测效力 | 运行期是否可达结论（M0B-01） |
|---|---|---|---|---|
| 回测/实盘执行 | 四铁律记录型校验 | `myquant/core/lookahead_guard.py`（`:13-17` 铁律定义） | ⚠️ 仅铁律3/4 且**恒真**（比对引擎自身迭代顺序）；铁律1/2 靠策略自报 | 可达：lookahead_guard 单例在 engine.py create_engine 注入（P2-D7-001），运行期真实调用；但铁律1/2 效力依赖策略自报（G-05：时间线排序自检，不计入门禁效力） |
| 回测/实盘执行 | 轻量逐 bar 时钟校验 | `myquant/core/light_lookahead_guard.py` | ❌ **未接入**：引擎仅升序回放时间戳（构造上不可能触发）；3 个 `check_*` 生产零调用；项目自身承认见 `utils/performance_config.py:31` | **运行期零调用（不可达）**——计为"未接入"，不叠加门禁效力；配置仅同步开关（performance_config.py:31/:168，M0B-03 口径一致）；后续接入需挂载点设计（不属本阶段） |
| 接入面 | 15 文件 240 处接入 | 三引擎、实盘引擎、策略基类、ML 特征 | ⚠️ 调用点性质不等价，须按上两行拆分 | 可达：按 lookahead_guard（可达但恒真）与 light_lookahead_guard（不可达）拆分登记 |
| 策略注册 | 装饰器注册 + `create_strategy` 工厂 + entry_points 插件 + 回测实盘共用 | `myquant/strategies/registry.py` | ✅ 有效（但**有 5 个绕过点**，见 §6.2-8） | 可达：生产实测有效（M0-04 注册强制契约后 7 策略经出口挂载强制校验；5 个绕过点已由 M0-08/09 出口唯一收敛点封堵） |
| ML | 特征截断重算一致性验证 | `myquant/ml/features.py` `lookahead_guard()`（`:841-890`） | ✅ 有效〔采信 v2.2 自述〕 | 可达：ML 特征层运行期调用（采信自述，未独立核验） |
| ML | 防未来切分（绝不随机切）+ `strict_split` horizon 防标签重叠 + walk-forward | `myquant/ml/training_pipeline.py`（`:1284`、`:1549`） | ✅ 有效〔采信 v2.2 自述〕 | 可达：训练管道运行期调用（采信自述） |
| ML | 模型 `train_end_time` 记录 + 推理侧守卫 | `ml/model_registry.py`、`ml/inference_service.py`（`:327-347`） | ✅ 有效（**仅推理侧**；训练侧写入校验缺失） | 可达：推理侧真实拦截（采信自述；训练侧缺口属 M2 范围） |
| 因子 | IC/ICIR 有效性档案 + 经济逻辑强制 | `myquant/indicators/factor_validator.py` | ✅ 有效 | 可达：因子验证器运行期调用 |
| 因子 | 三级缓存 + 数据指纹 | `indicators/factor_cache.py`、`utils/data_fingerprint.py` | ⚠️ `data_fingerprint` 默认 `None` 仅告警（`:461-469`），靠 `factor_engine.py:303-305` `auto_fingerprint` 兜 | 可达但弱：指纹默认 None 仅告警（可被绕过），有短 TTL 兜底 |
| 数据 | 财务公告日 PIT（`ann_date` + test_f15 锚点） | `database/schema.py`、`data_access/financial_service.py:536-551` | ✅ 有效**但仅此一路**；导出/增量两路未采用（§6.4） | 可达：financial_service 运行期调用；导出/增量两路穿越（M1A-01 收敛） |
| 数据（v2.3 上调） | 复权因子 PIT 截断 | `data_access/provider.py:514-524`（`as_of=end_date`）、`:562-564`（`merge_asof backward`）、`:566-568`（qfq `iloc[-1]`） | ✅ **实现正确**；⚠️ 但依赖调用方传对 `end_date`，且 `adjust_strict` 默认 False | 可达：实现正确；调用纪律缺口（M1A-05：adjust_strict 默认值治理）；fail-open 见第二节 |
| 数据 | 复权/股本事件表、幸存者偏差 | `backtest/survivorship_bias.py`、`stock_pool.yaml` | ⚠️ 分析工具与静态配置，**不等于股票池 PIT**；且 RL-03 依赖的成分股历史数据源**不存在**（§6.8） | 可达但弱：分析工具运行期可调用；股票池 PIT 未建设（RL-03 待启用，DEC-03） |
| 配置 | `backtest_config.yaml` 的 `lookahead_guard`(enabled/strict_mode) + `performance.light_lookahead_guard`(enabled/strict) | `config/backtest_config.yaml:3293`、`:3309` | ⚠️ **注意层级**：`light_lookahead_guard` 嵌在 `performance` 下，非顶层段 | 可达：入口消费 lookahead_guard（run_回测入口.py:312-318）；light_lookahead_guard 仅同步开关（未接入引擎，M0B-03 口径） |
| CI | `-m lookahead` 强制 100%、配置断言、架构分层门禁、ruff 基线护栏 | `.github/workflows/ci.yml` | ✅ 有效（但 guard 断言读的是 **`.example`** 文件：`test_config_live_check.py:79`，真实配置无监管，见 §6.2-12） | 可达：CI 运行期强制；guard 断言已改真实配置（M0-17：test_config_live_check 读真实配置，真实配置缺失 CI 跳过 + leakage-gate-check job 强制） |

**结论**：14 行均有"运行期是否可达"结论；无"计为资产但零调用"的模块——唯一零调用模块 `light_lookahead_guard` 已如实登记为 ❌ 未接入（§3 原表亦同口径）。

## 二、fail-open 三处登记（M0B-02，v2.3 新增要求）

| # | 位置 | fail-open 行为 | 处置结论（对应 M1A 任务） |
|---|---|---|---|
| A | `myquant/backtest/engine.py:684-688` | `_bar_timestamp_str` 时间戳异常 → 返回空字符串 → LookaheadGuard **跳过日期校验**（异常路径不可见） | **M1A-04**：异常返回哨兵值 + 计数（fail-open 可观测化）；`core/lookahead_guard.py` 空值"跳过校验"改"记 violation（警告档）"+ `:519-537` 补审计落点 |
| B | `myquant/data_access/provider.py:545-552` | 无复权因子事件且 `adjust_strict=False` → **静默降级 raw**（仅 warning，不可观测） | **M1A-05**：降级写 sidecar；`:535` `adjust_strict` 默认值治理；`end_date=None`+qfq 加告警/断言；新增 `load_asof`/`asof_join` |
| C | `myquant/data_access/manager.py:1298-1299` | 增量 B 类派生量计算异常 → **吞异常仅 warning**（失败不可见） | **M1A-06**：吞异常改可观测（失败计数 + 断言阈值），**与 M1A-01 同批**（§6.4.3 ③④ 原子提交约束） |

三处均已有处置结论并映射到轨道 C（M1A）对应任务，登记完成。

## 三、资产口径更正（M0B-03）

- `myquant/utils/performance_config.py:31`：注释已声明"当前仅同步开关、未接入引擎，属独立可选模块"（口径正确，P2-D7-002 整改遗留说明）；
- `myquant/utils/performance_config.py:168`：`light_guard = perf_cfg.get("light_lookahead_guard", {})` 读取点补注释"未接入引擎（仅配置声明同步开关）"——**文档与代码注释口径一致**（本任务完成）。

## 四、验收对照

- [x] §3 资产表 14 行均有"运行期是否可达"结论（M0B-01）；
- [x] 不存在计为资产但零调用的模块（light_lookahead_guard 如实登记 ❌ 未接入）；
- [x] fail-open 三处均有处置结论并映射 M1A-04/05/06（M0B-02）；
- [x] performance_config.py:31/:168 与文档口径一致（M0B-03）。
