# 鲲鹏量化交易系统审计报告

> 依据文本：《鲲鹏量化交易系统独立审计、审查与审核提示词》v3.0（生效日 2026-10-04）
> 审计对象：D:\Projects\Pythonwdhmm（鲲鹏量化交易系统，沪深 A 股日线级别量化回测与实盘）
> 审计基准：2026-10-04 文件系统快照｜审计类型：独立审计 ＋ 全面审查 ＋ 合规审核（合并为一层）
> 报告性质：**审计差距分析与工作底稿草案，不是鉴证报告**

---

## 使用限制（务必先读）

1. 本报告为**审计差距分析与工作底稿草案**，不是鉴证报告。全部结论须由持牌审计机构、合规官、风控负责人复核并签字担责。
2. 若由被审计方自行使用，使用页面必须载明：**「本文件为非独立自查报告，不得对外宣称已通过独立审计」**。
3. 本报告结论绑定 **2026-10-04 文件系统快照**；快照之后发生的任何变更均可能使结论失效。
4. 本报告不构成法律、税务、会计意见或投资建议。涉及监管适用性的全部结论须由持牌律师/合规官确认；数字型控制标准（阈值、时限、保存年限）以证监会、交易所、期货业协会官网最新原文为准，本报告不引用自媒体或聚合知识库摘要。

---

## 0. 审计启动与资料清单

### 0.1 三参数填写表（请在进入分维度审查前确认并签署）

| 参数 | 本次取值 | 需由谁确认 |
|---|---|---|
| ① 重要性水平 | **未提供**（NAV __%、单日最大可容忍亏损 __、监管处罚暴露等级 __） | 审计委托方 ＋ 风控负责人 |
| ② 抽样口径 | **未提供**（回测起止日 __、实盘交易日数 __、策略/账户覆盖率 __、排除项 __） | 审计委托方 |
| ③ 审计基准 | 基准日期 2026-10-04；仓库路径 D:\Projects\Pythonwdhmm；**commit hash 未取得**（仓库根无 `.git`，本次执行环境无法运行 git）；配置版本＝快照内 `config/*.yaml` 内容；环境对应关系（开发/仿真/生产）**未取得** | 提供方签署声明 |

> 三参数缺失的直接后果：无法执行提示词第 4 章的重要性水平过滤，且未取得提供方签署的《材料真实性声明》。按第 5.4 条，**无此声明时证据等级最高计为 E**。

### 0.2 首轮澄清问题（按阻塞性 / 重要性排序，共 10 个）

**阻塞性（答不出则无法出具有效结论）**

1. 是否存在 `.git` 仓库与可提供的 commit hash？2026-10-04 快照是否等同生产运行版本？请提供部署清单（各环境对应的版本、配置文件名与位置）。
2. 是否愿意签署《材料真实性声明》：基准日期、hash 真实性、配置即生产当前版本、未作选择性删减？
3. 三项重要性水平取值（NAV 百分比、单日最大可容忍亏损、监管处罚暴露等级）分别是多少？低于门槛的发现是否降为「信息」级？
4. 抽样口径：回测起止日、实盘（或仿真）交易日数、策略与账户覆盖率、排除项及理由分别是什么？
5. 程序化交易报备的实际状态：是否已在券商/交易所完成报备？请提供报备编号或回执；报备的账户标识、策略标识、软件版本。
6. 交易相关资料与日志目前的实际保存位置、保存方式与保留期限？是否存在仓库外的备份或集中日志系统？（本系统内 `LOG_KEEP_DAYS = 7`）

**重要性（影响风险定级与整改优先级）**

7. 本系统是否涉及真实资金？若有：资金规模、托管安排、账户主体（个人/机构）、份额与申赎安排。
8. 组织与治理：岗位设置、职责分离矩阵、权限矩阵与账号清单、三道防线安排、关键人识别、投委会/董事会监督记录。
9. 运行期证据可否提供：实盘/仿真运行日志、成交回报、券商对账单、监控告警记录、调度器执行记录、CI 与验收测试执行报告、灾备切换演练记录、最近一次内审/外审/渗透测试/监管检查结果。
10. 数据与供应商：数据源授权合同要点与期限、数据血缘说明、经纪与交易所接口说明、供应商尽调与退出安排；是否存在跨境/数据出境场景。

### 0.3 必交材料清单（缺任一项则只出具《审计启动与资料清单》，不硬写报告）

| # | 材料 | 本次状态 |
|---|---|---|
| 1 | 系统架构图、部署拓扑、数据流图、网络图 | **未提供**（仅有 `docs/architecture.md` 的文本示意与 ASCII 图） |
| 2 | 策略说明书（逻辑描述、输入输出契约、参数范围与变更审批记录；不提供源码全文，源码仅现场抽查式访问） | **部分提供**（`docs/strategy_specs.md` 存在，但参数变更审批记录未提供；且已发现规格与实现不一致） |
| 3 | 回测报告、实盘报告、归因报告、压力测试报告 | **未提供**（仓库内仅有历史产物目录与自述文档） |
| 4 | 数据源清单、授权合同要点、数据字典、血缘说明 | **部分提供**（`docs/data_dictionary.md` 存在；授权合同要点与血缘说明未提供） |
| 5 | 风控规则、限额、止损、应急预案、Kill Switch 说明 | **部分提供**（规则与限额在代码与 `config/live_config.yaml` 中可读；应急预案与 Kill Switch 说明文档未提供） |
| 6 | 权限矩阵、账号清单（脱敏）、密钥管理策略、变更记录 | **未提供**（代码层仅有单一 API 密钥机制） |

### 0.4 选交材料清单（有则加分，无则标「未验证」）

| # | 材料 | 本次状态 |
|---|---|---|
| 7 | 代码仓库关键模块、配置、脚本、SQL、API 文档（现场抽查式、只读） | **已提供**（全仓库只读访问；无 commit 绑定） |
| 8 | 交易日志、订单日志、成交回报、对账记录、监控日志（抽样区间内） | **未提供**（且日志保留期仅 7 天） |
| 9 | 合规文件、程序化交易报备与变更报告、内部制度、审批记录 | **未提供**（仅有 `docs/仿真模拟盘运行制度.md` 等内部文本） |
| 10 | 运维手册、SLA、灾备方案、演练记录、事故报告 | **未提供** |
| 11 | 供应商、托管、经纪、交易所接口说明 | **未提供** |
| 12 | 最近一次内审/外审/渗透测试/监管检查结果及整改闭环 | **未提供**（仓库内自述整改记录之间互相矛盾） |

### 0.5 脱敏规则（最小必要字段）

- 账号保留后 4 位；IP 保留前两段；金额分桶；身份证/手机号/姓名全掩码。
- **密钥一律不提供**，仅提供管理策略与轮换记录。
- 可用合成数据替代生产数据。
- 本次审计已遵守：未读取 `.env`、私钥、证书等敏感文件的整份内容（仅做路径与键名探测）。

### 0.6 审计限制（必须随报告一并披露）

| 限制 | 说明 |
|---|---|
| 执行环境限制 | 本次会话无法启动任何子进程（`pwsh` 返回 `0xC0000142`），故**无法执行 git、pytest、python 或任何脚本**；全部取证依赖文件读取与内容检索。 |
| 运行期材料缺失 | 未取得运行日志、成交回报、对账单、告警记录、测试报告、报备回执。 |
| 单方声明依赖 | 仓库内既有审计文档（`docs/系统性审核/`、`docs/审计档案/`、蓝图回写、交接摘要）均为被审计方自述，且已发现自述之间及自述与代码之间的矛盾。 |
| 无法验真 | 无 commit hash，结论仅绑定快照时点。 |
| 未做动态测试 | 提示词第十二章红队用例**全部未执行**。 |

---

## 1. 仓库画像（只读审查启动输出）

### 1.1 技术栈与形态

- 语言/运行时：Python ≥ 3.10（CI 实测 3.12）；包管理 `pyproject.toml` 单文件声明，**无锁文件与哈希校验**。
- 核心包 `myquant`：`api`、`core`、`database`、`backtest`、`live`、`data_access`、`strategies`、`risk`、`monitoring`、`allocation`、`execution`、`indicators`、`models`、`utils`、`ml`、`gui`、`web`。
- 存储：单一 SQLite `database/quant.db`（与代码同盘），由 SQLAlchemy ＋ Alembic 管理。
- 部署：`docker/`（Dockerfile、compose、nginx Basic Auth 网关）、`一键部署.bat`、`.github/workflows/ci.yml`（7 个 job）。
- 界面：PySide6 桌面 GUI ＋ Streamlit Web 看板 ＋ FastAPI REST 接口。

### 1.2 模块划分与关键入口

| 环节 | 位置 |
|---|---|
| 回测入口 | `run_回测入口.py`、`run_批量回测.py`、`run_回测配置GUI.py` |
| 实盘入口 | `run_实盘入口.py`（`--mode monitor/paper/sim/live`，`--confirm-live` 门禁） |
| 数据入口 | `run_数据下载GUI.py`、`run_全量清洗GUI.py`、`scripts/数据全量清洗.py` |
| 运维工具 | `run_清理GUI.py`、`scripts/结果备份.py`、`scripts/缓存管理.py` |
| 策略层 | `myquant/strategies/`（`StockPoolBuilder`、`RSRSStrategy`、`KunpengStrategy` 等） |
| 引擎层 | `myquant/backtest/`（event / vectorized / mixed 三引擎）＋ `myquant/live/engine.py` |

### 1.3 关键文件清单（按审计维度）

- **策略与因子**：`myquant/strategies/base.py`、`ma_strategy.py`、`kunpeng_strategy.py`、`registry.py`、`myquant/indicators/rsrs.py`、`docs/strategy_specs.md`
- **数据**：`myquant/data_access/provider.py`、`loader.py`、`manager.py`、`financial_service.py`、`cleaner/derived_metrics.py`、`full_clean_service.py`、`scripts/数据导出_DB到processed.py`
- **回测**：`myquant/backtest/mixed_engine.py`、`vectorized_engine.py`、`engine.py`、`order_matcher.py`、`fee_common.py`、`metrics.py`、`param_optimizer.py`、`kunpeng_param_scanner.py`、`consistency_check.py`、`survivorship_bias.py`
- **防未来**：`myquant/core/lookahead_guard.py`、`light_lookahead_guard.py`
- **实盘**：`myquant/live/engine.py`、`data_feed.py`、`scheduler.py`、`state_store.py`、`portfolio.py`、`emergency_liquidation.py`、`brokers/paper_broker.py`、`brokers/qmt_broker.py`
- **风控**：`myquant/risk/rules.py`（`RULE_REGISTRY` 11 条）、`risk_manager.py`、`guardian.py`
- **资金与账务**：`myquant/models/trade.py`、`myquant/database/schema.py`、`config/live_config.yaml`
- **权限与安全**：`myquant/api/middleware/auth.py`、`rate_limit.py`、`myquant/api/routes.py`、`docker/nginx/dashboard_auth.conf`、`myquant/core/transports/rabbitmq_bus.py`
- **运维**：`myquant/monitoring/*`、`myquant/utils/cleanup_service.py`、`cache_sync.py`、`scripts/结果备份.py`
- **合规与治理**：`docs/仿真模拟盘运行制度.md`、`docs/工程待办/*`、`docs/审计档案/*`、`docs/系统性审核/*`

### 1.4 构建、测试与 CI/CD

- 构建/安装：`pip install -e ".[dev,live,web,benchmark]"`；`一键部署.bat` v3.0 负责建环境、初始化目录、初始化数据库、验证核心模块导入。
- 测试：`tests/unit/`（实测 91 个 `test_*.py`，蓝图自述 44 个，**计数失真**）、`tests/benchmarks/`。本次无法执行。
- CI：`.github/workflows/ci.yml` 共 7 个 job（lint / test / consistency-check / backtest-smoke / docker-build / live-config-check / data-clean）；**无依赖漏洞扫描、无密钥扫描、无 SBOM**。

### 1.5 缺失材料清单（详见 §0.3、§0.4）

实盘日志与成交回报、资金对账记录、合规报备与变更报告、运维手册与 SLA、灾备与演练记录、事故报告、外部审计/渗透测试结果、供应商与接口说明、权限矩阵与账号清单、密钥管理策略与轮换记录。

### 1.6 敏感信息风险提示（只列路径，不列内容）

- 未发现磁盘上的 `.env` 文件（`**/*.env` 无命中），但存在 `config/live_config.yaml`（含 `${FEISHU_SECRET}`/`${SMTP_PASSWORD}` 等环境变量占位，以占位符形式出现，未发现明文口令）。
- 已确认的风险点：`docker/Dockerfile` 无 `.dockerignore` 且第 50 行 `COPY . .`，构建上下文可能包含 `.env` 与 `database/quant.db`；`docker-compose.yml:32,76` 将 `../.env` 以只读方式挂入 app/live 容器。
- 未发现硬编码明文凭证（`rabbitmq_bus.py:87-90` 存在 `guest/guest` 缺省回退，属缺省凭据而非硬编码真实口令）。

### 1.7 审计分批计划（本次实际执行）

| 批次 | 范围 | 状态 |
|---|---|---|
| 第 1 批 | 仓库画像与架构、代码质量、依赖与 CI/CD、安全基线 | 已完成 |
| 第 2 批 | 策略、因子、数据、回测与仿真 | 已完成 |
| 第 3 批 | 实盘一致性、执行、订单、事前风控、异常交易 | 已完成 |
| 第 4 批 | 资金、对账、权限、安全、密钥、供应链 | 已完成 |
| 第 5 批 | 运维、灾备、监控、变更、容量与应急 | 已完成 |
| 第 6 批 | 合规、治理、文档、程序化交易报备、模型治理 | 已完成 |
| 第 7 批 | 汇总、风险矩阵、整改路线图 | 已完成（见主报告 §3–§9） |

---

## 2. 本轮结论摘要

- 共形成 **50 项** 发现：**严重（P0）10 项、高（P1）27 项、中（P2）12 项、低（P3）1 项**。
- 总体成熟度 **1.8 / 5**（介于「有制度无执行证据」与「局部执行」之间）。
- 放行建议：**Go with conditions（仅限 paper/sim 仿真运行）**；**真实资金实盘 No-Go**，直到 10 项 P0 全部关闭且合规四项（KP-P6-01、KP-M05、KP-P3-06、KP-M02）关闭。
- 最紧迫 5 项：KP-P3-01（T+1 冻结致无法卖出）、KP-P2-10（涨跌停约束恒空转）、KP-P3-02（bar 时间穿越）、KP-P6-01（程序化报备与异常交易控制点全缺失）、KP-P4-01（账实无对账）。
- 详细发现、汇总表、风险矩阵、合规差距分析、整改路线图、红队用例执行情况与三个必要清单，见《鲲鹏量化交易系统审计报告（含工作底稿附录）》。

---

## 3. 下一步（需人类确认后方可继续）

1. 回答 §0.2 的 10 个澄清问题（阻塞性 1–6 优先）。
2. 确认 §0.1 三参数取值。
3. 提供 §0.3 必交材料（至少第 1、2、3、5 项）。
4. 若确认走降级路径（材料无法全部提供），请书面确认接受「相关维度结论一律标注未覆盖/无法验证」。
