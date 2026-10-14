# R-26 前置调研 · on_init 全量依赖是否已被测试冻结

| 项 | 值 |
|---|---|
| 编号 | R-26（P0-08 唯一剩余前置） |
| 日期 | 2026-10-14 |
| 类型 | 只读摸底（不改任何代码） |
| 结论 | **已冻结 → P0-08 窗口隔离"只补文档契约，不用补测试"** |

## 1. 要回答的问题

P0-08（因果性收口，唯一阻断级卡）的窗口隔离设计是：**on_init 阶段放行全量历史访问，on_bar 阶段封死 `context.data` 的 value 直取**。

开工前必须先确认："on_init 读全量历史"这一行为是否已被现有测试冻结——
- 若已冻结 → P0-08 只需把契约写成正式文档（docstring / §5.5 表补字段）；
- 若未冻结 → P0-08 还要补一组"on_init 放行全量"的测试。

## 2. 勘察事实（逐条回代码核验）

### 2.1 on_init 真正依赖全量的点

| 位置 | 读法 | 是否全量依赖 |
|---|---|---|
| `myquant/strategies/ma_strategy.py:195` | `for symbol, df in context.data.items()` | **是**——on_init 用全量历史一次性预计算 RSRS 完整序列（L199 `rsrs.calculate(df)`），后续 on_bar 按日期取值 |
| `myquant/strategies/grid_strategy.py:218` | `list(context.data.keys())` | keys 枚举（合法保留），非 value 依赖 |
| `myquant/strategies/grid_strategy.py:228` | `context.get_data(symbols[0])` | 走切片访问器 `get_data()`，合规路径 |

**结论**：全仓唯一真正依赖 on_init 全量的，是 `ma_strategy.py:195` 的 RSRS 预热预计算。其余 on_init 触点要么是 keys 枚举，要么走 `get_data()` 切片访问器。

### 2.2 切片开关的时机（窗口隔离机制本身）

引擎只在 **on_bar 逐 bar 推进**时才置 `_slicing_enabled=True`：
- `myquant/backtest/engine.py:492`
- `myquant/backtest/mixed_engine.py:744`

on_init 阶段该开关保持默认 `False` → `get_data()` 与 `context.data["x"]` 均返回全量。这正是窗口隔离的机制基础：on_init 天然放行，on_bar 才切片。

### 2.3 现有测试冻结（37 passed 验证）

`tests/unit/test_causality_slicing.py` 已冻结 on_init 全量契约：

| 测试 | 冻结内容 |
|---|---|
| `TestOnInitStageFullAccess.test_on_init_stage_returns_full`（L119） | on_init 阶段 `_slicing_enabled=False`，`get_data()` 返回全量（5 行） |
| `test_on_init_getitem_returns_full`（L226） | on_init 阶段 `context.data["x"]` 直取返回全量（**保 ma_strategy 预计算**） |
| `test_engine_on_bar_data_is_scoped`（L260） | 真实 mixed 引擎 on_bar 阶段 `data[symbol]` 为切片 |
| `test_fail_closed_when_no_timestamp`（L241） | 切片开启但时点未设 → 返回空（fail-closed，不静默全量） |
| `test_get_returns_full_for_builder_power`（L248） | `data.get()` 恒返回全量（供 pool_builder 自行 as_of 截断） |

`tests/unit/test_checks_causality.py:289 test_accounting_no_violation_on_init`：on_init 阶段记账但**不判违规**（不误伤预热）。

`TestReusedContextNoLeftover`（L127，R-25 处置）：context 跨 run 复用时 `_slicing_enabled/current_bar_ts/访问账目` 三态复位，无残留。

**复跑结果**：`test_causality_slicing.py + test_checks_causality.py` = **37 passed in 2.86s**，冻结有效。

## 3. R-26 结论

**on_init 全量依赖已被测试冻结**。

- `test_on_init_getitem_returns_full` 注释直接写明"保 ma 预计算"，正是为 `ma_strategy.py:195` 的 on_init 全量访问兜底；一旦未来误把 on_init 也切片，该测试即红。
- 窗口隔离两侧（on_init 放行 / on_bar 封死）的行为语义均有测试锁定。

→ **P0-08 窗口隔离的"测试"部分已就位，P0-08 无需新增"on_init 放行"测试**。

## 4. 对 P0-08 工作量的影响（收缩）

P0-08 原估 3~5 人日、16 条验收用例、新增 3 个测试文件（test_manifest_truthfulness.py / test_slicing_failopen_guard.py / test_checks_causality.py）。R-26 确认后：

- `test_checks_causality.py` / `test_slicing_failopen_guard.py` **已存在且 37 passed** → 测试侧大半已落地。
- P0-08 剩余本体工作量收缩为三块：
  1. **manifest 真实值采集**（总清单偏差 5：7 组硬编码乐观值改为实际值）；
  2. **fail-open 加固**（总清单偏差 6：引擎静默 `except: pass` 改为 fail-closed）；
  3. **文档契约显式化**（§5.5 表补字段：on_init 放行全量 / on_bar 封死 value 直取，本 R-26 即为该契约的事实底稿）。

## 5. 纪律遵守

- 全程只读：仅 `Read` / `Grep` / 一次 pytest 复跑验证冻结有效，**未改任何代码与配置**。
- 结论可复现：冻结测试 37 passed，关键触点行号均回源码核对。
