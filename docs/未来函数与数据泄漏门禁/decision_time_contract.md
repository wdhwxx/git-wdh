# 决策时点契约模板（L-1 契约层）

> 项目：Pythonwdhmm（鲲鹏量化交易系统）
> 归属：未来函数与数据泄漏门禁 · 阶段 0-A（M0-01）
> 依据：《未来函数与数据泄漏门禁施工方案.md（v3.0 定稿）》§5.2、§11 附录 B
> 生成日期：2026-10-10

## 一、契约的作用

本契约模板是**所有策略/因子/特征/标签必须声明的"时间语义元数据"**。契约只做声明，不做校验——
校验由 `myquant/leakage_gate/contract.py` 负责（比对 decision_time / available_time 声明完整性与列级台账）。

**契约不能自证 PIT**：`available_time` 是策略侧声明，其与数据层实际日基是否一致，须由列级时点台账
（`config/data_time_semantics.yaml`，阶段 0-P）校验。

## 二、契约主键

**契约主键 = `name`（策略类属性），`strategy_id` 仅作展示别名**（DEC-01 拍板，见 v3.0 §5.2 修正 2）。

理由：registry.py 现有实现即以 `name` 为键（`registry.py:40`/`:72-79`），定死 `name` 为唯一主键，
避免"name / strategy_id 两套真相"导致的 `assert_all_gates_pass(strategy_id, …)` 取不到契约。

## 三、契约模板（YAML）

```yaml
strategy_id: base_strategy_001        # 展示别名（非主键）
name: 基础策略                          # ← 唯一主键（v3.0 §5.2 修正 2 / DEC-01）
type: daily_equity
decision_time: "T日收盘后 15:00"        # 决策时点：信号生成时刻
execution_time: "T+1开盘 09:30"         # 执行时点：下单成交时刻
holding_period: "5-20交易日"
universe:
  type: "static"                        # 当前实为静态配置，非 point-in-time
  source: "config/stock_pool.yaml"      # index_constituents_history 数据源尚不存在（§6.8 RL-03）
  pit_status: "未建设"                   # 显式声明，避免契约自证 PIT（注4）
data_availability:
  market_data:
    available_time: "T日15:00"           # 行情列可用时点
    adjust: "hfq"                        # 真实配置为 hfq（非 raw），消费期现算（v3.0 §2.3）
    adjust_asof: "end_date 必传"          # 复权因子按 as_of=end_date 截断（§6.4.5）
    fields: [开盘价, 最高价, 最低价, 收盘价, 成交量, 成交额]
  financial_data:
    available_time: "ann_date"           # 复用现有 ann_date 字段
    basis: "eff_disclosure_date"         # 统一时点日落点函数（§6.4.2）
    fields: [revenue, net_profit, ...]
consumed_columns:                        # R11 消费侧门禁的声明侧输入（v3.0 §6.1.3）
  - 开盘价
  - 最高价
  - 最低价
  - 收盘价
  - 成交量
  - 成交额
ruleset: daily_close_decision            # 映射到 config/strategy_registry.yaml 的 ruleset
owner: xxx
```

## 四、字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 唯一主键；策略类属性，registry 以 name 为键 |
| `strategy_id` | ✅ | 展示别名；不参与主键判定 |
| `decision_time` | ✅ | 信号/决策生成时刻；契约校验比对对象 |
| `execution_time` | ✅ | 下单成交时刻；应晚于 decision_time（T+1 执行） |
| `holding_period` | ✅ | 持仓周期（策略自声明） |
| `universe.pit_status` | ✅ | 显式声明 universe 是否 point-in-time；**禁止引用不存在的数据源** |
| `data_availability.market_data` | ✅ | 行情数据可用时点 + 复权口径（hfq + as_of=end_date） |
| `data_availability.financial_data` | ✅ | 财务数据可用时点（ann_date）+ 统一时点日基（eff_disclosure_date） |
| `consumed_columns` | ✅ | 本策略引用的数据列清单（R11 声明侧输入） |

## 五、注记

> 注 1：不引入项目当前没有的数据类型（如分析师评级），契约模板只覆盖行情 + 财务 + 因子。

> 注 2：模板中的 `available_time` 是**策略侧声明**；其与数据层实际日基是否一致，
> 须由阶段 0-P 的列级时点台账校验（`leakage_gate/contract.py` 负责比对）。**契约不能自证 PIT。**

> 注 3：`consumed_columns` 同样**不能自证**——声明可漏报。实际判定以 AST 实测引用列为准，
> 声明仅作补充与文档用途（R11，v3.0 §10.8）。消费侧门禁以「AST 实测引用列 ∪ 契约声明列」为准。

> 注 4：`universe.pit_status` 必须显式声明。契约模板**不得引用不存在的数据源**
> （`index_constituents_history` 项目内不存在，§6.8 RL-03 待数据源建设后启用）。

> 注 5（G-02 间接引用）：`consumed_columns` 无法覆盖**间接引用**（`col="每股收益"; df[col]` 等）。
> 该绕过路径由消费侧门禁的"阻断列名出现次数审计"（低成本字面量检索）补充兜底（v3.0 §12.4-G-02）。
