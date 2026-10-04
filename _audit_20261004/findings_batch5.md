# 第5批 详细发现（运维与灾备、监控告警、变更管理、容量与应急）

## 阅读说明

本批为独立审计工作底稿草案，证据基于 2026-10-04 文件系统快照的只读取证结论（未取得 commit hash，重要性水平、抽样口径、材料真实性声明均未提供，属降级口径）。证据等级口径（A–E，依 v3.0 提示词第九章 9.2 全局唯一表）：**A**=直接可核验物（代码/配置/日志原文，路径与行号可逐字复核，含 hash 可验）；**B**=文档/配置/截图等间接材料；**C**=基于材料的合理推断（含仅目录级观察）；**D**=材料缺失；**E**=单方陈述未经佐证（含无真实性声明的 hash）。本批发现以 A 级（代码/配置原文）为主。「依赖单方声明」指结论只能依托项目自身文档、注释、docstring 或文件名声明，缺少外部或运行时独立佐证，不能作为鉴证依据。本文件不构成鉴证结论，涉及中国境内程序化交易监管条款的条目，法规适用性均需持牌律师/合规官确认，条文以证监会/交易所官网最新原文为准。

### KP-P5-01 部署与运行态：镜像无 .dockerignore、容器以 root 运行、实盘容器无 HTTP 级健康探针

- **维度**：运维与灾备 / 部署与运行态
- **位置**：`docker/Dockerfile:7`、`docker/Dockerfile:40`、`docker/Dockerfile:50`、`docker/docker-compose.yml:98-105`
- **证据等级**：B（Dockerfile 与 compose 全文可逐字复核；仓库内 glob 未发现任何 `.dockerignore`）
- **优先级 / 风险等级**：P1 高
- **事实**：镜像基础为 `FROM python:3.12-slim`（`Dockerfile:7`），全文无 `USER` 指令，`COPY . .`（`Dockerfile:50`）把项目根整体纳入构建上下文；仓库内无 `.dockerignore`。`docker-compose.yml` 中 `app` 服务探针为 `test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]`（`:51`），`live` 服务探针为 `test: ["CMD-SHELL", "pgrep -f run_实盘入口.py || exit 1"]`（`:101`），且 `live` 环境变量设置 `AUTO_RECOVER_ON_START=true`（`:89`）。
- **推断**：容器默认以 root 身份运行，且构建上下文无忽略清单，`.env`、`database/quant.db`、`backups/`、`datasets/` 等大体积/敏感内容会进入构建上下文与镜像层；实盘容器探针仅判断进程存在，进程假活（线程死锁、行情源断连、风控停摆）仍判健康。
- **未知**：宿主 Docker/Compose 版本与 `restart` 策略的实际生效情况；生产是否另用 k8s/进程守护替代；镜像是否实际推送至私有仓库；`AUTO_RECOVER_ON_START` 是否被任何代码消费（本批未在代码中检索到该变量读取点）。
- **风险影响**：密钥与账户数据库进入镜像层造成泄露面扩大；进程假活导致故障期间无任何自动重启或摘除，问题持续存在直至人工发现；实盘状态恢复被误认为已开启。
- **可能性**：控制完全缺失（无 `.dockerignore`、无 `USER`、无业务级探针）
- **根因**：技术 — 容器化仅做到"能跑起来"，未按最小权限与最小上下文原则收敛。
- **整改建议**：在 `docker/Dockerfile` 增加非 root 用户（`useradd -m quant` 后 `USER quant` 并调整目录属主）；新增仓库根 `.dockerignore` 排除 `.env`、`database/`、`datasets/`、`outputs/`、`logs/`、`backups/`、`__pycache__`、`.git`；把 `docker/docker-compose.yml` 的 `live` 探针改为调用真实业务健康接口或新增 `run_实盘入口.py --health-probe`（覆盖行情源连通、券商连接、风控状态）。
- **验证方法（DoD）**：`docker history`/`docker run --rm <image> id` 显示非 root；`docker build` 日志中不出现 `.env`、`quant.db`；`docker inspect --format '{{.State.Health.Status}}'` 在行情源被切断后 3 个探针周期内变为 `unhealthy`。
- **最坏情形**：密钥随镜像外泄；实盘进程假死数小时无人知，持仓裸奔至收盘或隔夜跳空。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-02 健康探针内容：`/api/health` 检查器注册表为空，接口恒报 unknown 但 HTTP 200

- **维度**：监控告警 / 探针有效性
- **位置**：`myquant/api/routes.py:319-326`、`myquant/api/routes.py:382-411`、`docker/Dockerfile:60-61`
- **证据等级**：B（路由与初始化代码可逐字复核；未检索到任何生产调用 `register_health_checker`）
- **优先级 / 风险等级**：P1 高
- **事实**：`app.state.health_checkers: Dict[str, Any] = {}`（`routes.py:319`）仅由 `register_health_checker`（`:321-326`）填充；`/api/health` 遍历该字典，未注入模块统一置 `unknown`（`:403-405`），且只要没有显式 `down` 就返回 `status="healthy"`（`:406-411`）。全仓检索 `register_health_checker` 无生产调用点（仅定义处）。`Dockerfile:61` 探针为 `CMD curl -f http://localhost:8000/api/health || exit 1`。
- **推断**：容器探针实际只验证"HTTP 服务在监听且未抛异常"，`data/strategy/backtest/live/risk/allocation` 六个模块恒为 `unknown` 仍返回 200 与 `healthy`；数据源断连、风控暂停、资金异常均不会改变探针结果。
- **未知**：是否有容器外的独立拨测（Zabbix/Prometheus blackbox/云监控）；`myquant.monitoring.health_check` 的 `HealthChecker` 是否在项目外被装配（仓内仅测试引用）。
- **风险影响**：编排层与值班人员把 `healthy` 当作业务可用信号，故障被"绿色"掩盖，MTTD 被拉长到人工察觉为止。
- **可能性**：控制存在未执行（接口存在、探针存在，但内容为空壳）
- **根因**：技术 — 健康检查仅在 API 层预留注册表，未在实盘装配路径接线。
- **整改建议**：在 `run_实盘入口.py` 的装配段（`setup_monitoring_linkage` 附近）调用 `app.register_health_checker("live", engine.health_check)`、`("data", data_feed.health_check)`、`("risk", lambda: risk_manager.get_summary().get("state"))`；并在 `routes.py:406` 把 `unknown` 视为 `degraded`，避免"未注入=健康"。
- **验证方法（DoD）**：注入后请求 `/api/health` 返回各模块真实状态；断开行情源后接口 `status=degraded` 且 `docker inspect` 探针转为 `unhealthy`；新增单测覆盖"未注册模块不算健康"。
- **最坏情形**：行情源/券商链路中断而看板与编排系统全绿，人工在数小时后才发现。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-03 监控告警：`myquant/monitoring` 只采集与查询，健康/指标无阈值告警闭环

- **维度**：监控告警 / 闭环缺失
- **位置**：`myquant/monitoring/health_check.py:634-644`、`myquant/monitoring/metrics_collector.py:1192-1271`、`myquant/monitoring/__init__.py:26`
- **证据等级**：B（源码引用点可复核；全仓无生产调用）
- **优先级 / 风险等级**：P0 严重
- **事实**：`get_health_checker()`（`health_check.py:634`）返回全局 `HealthChecker` 单例，但全仓 grep 中该函数只出现在 `health_check.py` 自身与 `monitoring/__init__.py:36,69`（导出），无生产模块调用，也无定时巡检调用；`MetricsCollector.collect_from_health_checker`（`metrics_collector.py:1192`）同样无生产调用方。`monitoring/__init__.py:26` 的 docstring 声明"任务22: live/scheduler.py 定期巡检调用 HealthChecker + check_escalations()"，而 `myquant/live/scheduler.py` 实际使用自建 `HealthPatrol`，未引用 `monitoring` 包。
- **推断**：健康检查与指标采集具备"采集/查询/Prometheus 导出"能力，但没有任何把阈值越界转成告警的调用链；监控包在实盘运行路径上是装饰性组件。
- **未知**：是否有项目外的 Prometheus + Alertmanager 抓取 `/metrics` 并自带告警规则（仓内未见相关配置或部署清单）；容器化部署下是否有外部监控系统。
- **风险影响**：无自动告警闭环，故障发现完全依赖人工盯盘与日志检索；夜间/无人值守时段失效不可知。
- **可能性**：控制完全缺失（采集能力存在，触发链路为零）
- **根因**：治理 — 文档声明了"定期巡检调用"，实现侧未落地，两处口径漂移未被复核。
- **整改建议**：在 `myquant/live/scheduler.py` 的 `attach_engine` 中额外注册一项定时任务，调用 `myquant.monitoring.health_check.get_health_checker().run_all()` 并把非 `HEALTHY` 结果经 `get_alert_manager().alert(...)` 发出；或补 `myquant/monitoring/health_check.py` 到告警器的适配函数并在 `scheduler` 注册。
- **验证方法（DoD）**：新增集成测试：注入磁盘 95% 或事件总线异常后，`attach_engine` 注册的巡检任务在 1 个周期内产生一条 `AlertManager` 历史记录；`get_alert_manager().stats["history_size"]` 增长可断言。
- **最坏情形**：磁盘写满、事件总线异常等可预防故障在无人值守时段长期累积，最终以数据损坏或进程中止形式暴露。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-04 告警升级检查无生产调用方，告警可能被静默吞掉且无留痕兜底

- **维度**：监控告警 / 告警生命周期
- **位置**：`myquant/monitoring/alerting.py:1835-1883`、`myquant/monitoring/alerting.py:1329-1343`、`myquant/monitoring/alerting.py:1894-1908`、`tests/unit/test_monitoring.py:451-456`
- **证据等级**：B（调用点与函数体可逐字复核）
- **优先级 / 风险等级**：P1 高
- **事实**：`check_escalations()` 实现"WARNING 超 `escalation_time` 未确认自动升级为 ERROR"（`alerting.py:1859-1879`），但仓内调用点仅 `tests/unit/test_monitoring.py:455` 与 `alerting.py` 自身 docstring（`:1155`）与 `monitoring/__init__.py:26` 的声明；`scheduler.py` 未调用。静默相关代码：`send_alert` 在静默期内直接 `return 0`（`:1329-1332`），去重命中同样 `return 0`（`:1338-1343`）；`silence()` 可无上限时长调用（`:1894-1898`）。
- **推断**：告警升级闭环在实盘从未运行，WARNING 永不升级；静默/去重导致的丢弃只写 DEBUG 日志、不计入发送统计，事后难以从告警历史中区分"未发生"与"被抑制"。
- **未知**：是否存在外部工单/值班系统承担升级职责；是否有组织制度规定静默审批人。
- **风险影响**：维护窗口外的持续 WARNING 无升级与催办，重要告警可能长期沉睡；抑制行为缺乏可审计记录。
- **可能性**：控制存在未执行（升级与静默代码完整，调度未接线）
- **根因**：流程 — 告警治理规则写在模块文档，未纳入实盘调度与值班制度。
- **整改建议**：在 `myquant/live/scheduler.py:1595` 起的 `attach_engine` 中增加 `alert_manager.check_escalations` 间隔任务（建议 60s）；在 `alerting.py:1330-1343` 为静默/去重抑制增加计数指标与 `logger.warning` 记录，使抑制可见。
- **验证方法（DoD）**：构造一条 `auto_acknowledge_info=False` 的 WARNING，等待超过 `escalation_time` 后断言 `mgr.get_escalated()` 非空；静默期内断言产生"被抑制"计数日志。
- **最坏情形**：风控降级类 WARNING 被静默吞掉，演变为未被察觉的持仓/资金损失。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-05 告警通道：邮件/Webhook 从未装配，通道失败静默降级为本地日志

- **维度**：监控告警 / 通道与兜底
- **位置**：`myquant/monitoring/alerting.py:2011-2023`、`config/live_config.yaml:3376-3400`、`myquant/monitoring/alerting.py:650-653`、`myquant/monitoring/alerting.py:909-923`
- **证据等级**：B（单例装配与配置原文可逐字复核）
- **优先级 / 风险等级**：P1 高
- **事实**：`get_alert_manager()` 创建单例后只注册 `ConsoleAlertChannel()` 与 `LogAlertChannel()`（`alerting.py:2020-2022`），全仓无生产调用 `EmailAlertChannel`/`WebhookAlertChannel` 的构造点。`config/live_config.yaml` 的 `notification` 段被显式标注「【预留·未消费】多渠道推送属后续迭代，v1仅写本地日志」（`:3377`），`enabled: false`（`:3379`），飞书/企业微信/邮件全部 `enabled: false`（`:3382,3386,3389`）。通道未配置时 `EmailAlertChannel.send` 仅 `logger.warning` 后返回 False（`:650-653`），`WebhookAlertChannel.send` 队列满时 `logger.warning` 后丢队列尔返回 False（`:920-923`）。
- **推断**：当前告警全部落在控制台与本地日志；外部通道（飞书/企业微信/邮件）无一装配，"多通道并行"仅存在于文档与单测；主通道失败时无备用通道，无重发/二次确认机制。
- **未知**：是否有外部监控平台代偿推送；值班联系人清单与告警接收人是否存在（未见任何接收人配置）。
- **风险影响**：所有告警依赖有人打开终端或翻日志，无人值守时告警实际不可达；告警丢失无任何计数或补偿。
- **可能性**：控制完全缺失（通道代码存在，生产零装配、零接收人）
- **根因**：流程 — 消息推送被规划为"后续迭代"，上线前未做最小可用通道配置。
- **整改建议**：在 `run_实盘入口.py:360-361` 装配段按 `config/live_config.yaml` 的 `notification` 段读取并 `add_channel(WebhookAlertChannel(...))`；至少启用一条外部通道，并把通道注册失败视为启动期 ERROR（而非静默继续）；为 `AlertManager` 增加"全部通道失败"回退逻辑（如写入独立告警文件 `logs/alerts_undelivered.log`）。
- **验证方法（DoD）**：断开外网后触发一条 CRITICAL，断言 `logs/` 下收到 webhook 失败记录且存在未投递告警文件；恢复网络后重发成功；`get_alert_manager().list_channels()` 含外部通道。
- **最坏情形**：风控暂停、断连等 CRITICAL 事件仅存于本地日志，无人知晓直至盯盘者发现异常持仓。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-06 灾备：备份仅覆盖 outputs/logs（默认）且无恢复验证、无不可变性、无分级 RTO/RPO

- **维度**：灾备 / 备份与恢复
- **位置**：`scripts/结果备份.py:204-229`、`scripts/结果备份.py:300-315`、`scripts/结果备份.py:166-188`、`docker/docker-compose.yml:114-136`
- **证据等级**：B（脚本与 compose 可逐字复核；`backups/` 实际仅含 `.gitkeep` 与一个 `tencent_hfq_orphans_20261001_225848.csv`）
- **优先级 / 风险等级**：P0 严重
- **事实**：`collect_backup_sources`（`结果备份.py:204-229`）默认只收 `outputs` 与 `logs`，`database/quant.db`、`config/` 仅在 `--include-db`/`--include-config`/`--all` 时纳入；`run_backup` 调用 `backup_files` 后直接打印结果（`:300-315`），无恢复校验、无 checksum、无副本异机；`cleanup_old_backups`（`:166-188`）按 mtime 只保留最近 N 个，超出的目录 `shutil.rmtree`，删前无审批、无不可变保留；实盘状态目录 `实盘状态`、`实盘信号`、`datasets/live_state` 均不在备份源内；`docker-compose.yml:118-120` 的 `db` 卷 `pgdata` 仅 `driver: local`，无异地副本。全仓未检索到 RTO/RPO 或灾备切换演练记录（`docs/` 下 grep 无命中）。
- **推断**：备份是"手工、可选、同盘、可删"的脚本，未构成可恢复性保障；关键恢复对象（数据库、实盘快照、配置）默认不在备份范围；从未验证过恢复，备份有效性未知。
- **未知**：宿主是否有独立磁盘快照/云备份；`database/init` 初始化脚本是否足以重建库（`compose:120`）；实际 RTO/RPO 业务要求由谁定。
- **风险影响**：磁盘故障、误删或勒索事件同时损失生产数据与同盘备份，且无可用的恢复演练依据。
- **可能性**：控制完全缺失（默认不含关键对象、无验证、无不可变、无指标）
- **根因**：技术 — 备份脚本定位为"结果归档"而非灾备，未做恢复目标与分级定义。
- **整改建议**：把 `scripts/结果备份.py:204-229` 的默认源扩展为 `database/quant.db` + `config/*.yaml` + `datasets/live_state`，并默认启用 `--zip` 与校验；在 `myquant/utils/file_utils.backup_files` 输出后增加 sha256 清单与"随机抽取一份还原到临时目录并计数比对"的自检；为备份目录启用只追加/异地副本（对象存储或异机），并在 `docs/` 形成分级 RTO/RPO 表。
- **验证方法（DoD）**：执行一次备份 → 从备份还原 `quant.db` 与 `live_state` 到隔离目录 → 校验行数/快照 checksum 一致并在报告中留存记录；备份目录存在保留锁定或异机副本的取证截图。
- **最坏情形**：生产库或实盘快照全损，仅能依赖无法验证的同盘备份，恢复失败导致业务无法重启。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-07 变更管理：无配置漂移检测、无版本化发布与回滚、无变更审批留痕

- **维度**：变更管理 / 发布与配置治理
- **位置**：`.github/workflows/ci.yml:237-300`、`myquant/live/engine.py:2559-2585`、`docker/docker-compose.yml:31-32`
- **证据等级**：B（流水线与装配代码可逐字复核；`docs/` 下 grep「变更管理/配置漂移」无命中）
- **优先级 / 风险等级**：P1 高
- **事实**：CI（`ci.yml:43-338`）仅做 lint 基线护栏、单测、一致性/指纹专项、Docker 构建与配置**文件格式**校验（`:254-300` 校验 example 配置键存在性与策略名枚举），无环境实际配置与模板的漂移比对，也无版本化产物/镜像签名/发布记录。运行时配置经挂载直接进入容器：`docker-compose.yml:31-32` 将宿主 `config/` 与 `.env` 只读挂载，改动 `config/live_config.yaml` 无需任何审批或发布流程；`engine.py:2559-2585` 从该文件直读 `state_store`/`scheduler`/`lookahead_guard` 等关键开关。
- **推断**：配置变更与代码变更均无审批留痕、无版本号绑定、无回滚脚本化路径；生产中"改一个 yaml 即改行为"不可追溯，事故后无法还原当时生效的配置快照。
- **未知**：是否有 git 分支保护/必需评审（仓内无 CODEOWNERS、无分支保护可证材料）；`.env` 由谁保管与轮换。
- **风险影响**：变更引发的事故无法定界（当时配置不可考），回滚靠人工记忆，重复故障概率高。
- **可能性**：控制完全缺失（无漂移检测、无发布版本、无审批留痕）
- **根因**：流程 — 单人开发模式下配置即代码的治理未建立。
- **整改建议**：在 `.github/workflows/ci.yml` 增加 job：将 `config/*.example.yaml` 与生产配置做键级 diff 并在差异未说明时失败；为 `config/live_config.yaml` 引入 `config_version` 字段并由 `engine.py:2502` 起装配时写入日志与快照 metadata；发布时对镜像打 git 短哈希 tag 并在 `docs/` 登记变更审批（变更人/时间/内容/回滚方式）。
- **验证方法（DoD）**：人为改一个未声明的生产配置键 → CI 漂移 job 失败；发布记录中每次变更有版本号、审批人与回滚命令；快照 metadata 可回读当时 config_version。
- **最坏情形**：一次未经记录的参数修改（如放大仓位上限）造成损失且无法举证当时配置，事后复盘与责任界定失效。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-08 崩溃恢复：快照含订单但恢复只回写持仓与现金，在途订单丢失且默认不自动恢复

- **维度**：运维与灾备 / 崩溃恢复一致性
- **位置**：`myquant/live/state_store.py:606-616`、`myquant/live/state_store.py:655-671`、`myquant/live/state_store.py:760-809`、`config/live_config.yaml:3337-3346`、`myquant/live/engine.py:1699-1725`
- **证据等级**：B（采集/恢复代码与配置可逐字复核）
- **优先级 / 风险等级**：P0 严重
- **事实**：`capture_live_state` 采集 `engine/account/positions/orders/scheduler`（`state_store.py:606-616`），`_capture_orders` 从 `broker.get_orders()` 取单（`:655-671`），配置亦声明 `save_pending_orders: true`（`live_config.yaml:3341`）；但 `restore_live_state` 只做 `portfolio` 重建（`_restore_portfolio`：reset → 按快照 BUY 重建 → `update_cash` 回写现金，`:812-866`）、可选 `broker.restore_state`、`scheduler` 仅记录（`:797-802`），**无任何 `orders` 消费路径**。配置 `auto_recover_on_start: false`（`live_config.yaml:3345`），装配处 `auto_recover=bool(state_cfg.get("auto_recover_on_start", False))`（`engine.py:2583`），故默认不恢复；`_recover_state` 只传 `portfolio=`（`:1718`），快照损坏/加载失败一律"全新开始"（`:1714-1716`）。
- **推断**：重启后在途订单既不被恢复也不被显式对账，可能形成"券商侧已成交、本地无持仓"或"重复下单"的双向不一致；快照版本兼容性无守卫（`SnapshotVersionError` 仅校验版本号 ≤0），旧格式快照会被按当前字段尽力还原，字段缺失静默跳过。
- **未知**：`engine.resend_pending_orders()`（`engine.py` 断线补单）是否在崩溃重启路径上被调用；快照与券商实际持仓的日终对账是否人工执行。
- **风险影响**：崩溃恢复产生账实不符，风控基于错误持仓计算敞口，可能重复开仓或漏平仓。
- **可能性**：控制存在未执行（有采集与恢复框架，关键字段与默认开关未接线）
- **根因**：技术 — 恢复路径未覆盖订单维度的对账与幂等去重。
- **整改建议**：在 `myquant/live/state_store.py:760-809` 的 `restore_live_state` 增加订单分支：把快照 `orders` 与券商实时 `get_orders()` 做差集，输出待对账清单并对未知状态订单触发 `resend_pending_orders`；在 `capture_live_state`/`save` 写入 `schema_version`，`load_version` 对不兼容版本抛错并拒绝恢复；在 `config/live_config.yaml:3345` 将 `auto_recover_on_start` 改为显式必填并有启动前对账校验。
- **验证方法（DoD）**：构造"快照含 PENDING 订单 + 券商侧已成交"场景，恢复后断言产生对账告警且本地持仓与券商一致；构造 schema_version 不匹配快照，断言拒绝加载并有 CRITICAL 告警。
- **最坏情形**：重启后按旧持仓再次买入同一标的，形成超仓与资金超支，隔夜风险失控。
- **是否需法律顾问确认**：是（涉及程序化交易在途订单与交易留痕的监管要求，且本条须在整改建议中经持牌律师/合规官确认适用性，以证监会/交易所官网最新原文为准）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-09 缓存与指纹：增量回测复用仅比对数据指纹，参数变更可复用旧结果冒充新结果

- **维度**：容量与运维 / 缓存与结果正确性
- **位置**：`myquant/backtest/incremental_backtest.py:321-338`、`myquant/backtest/incremental_backtest.py:393-395`、`myquant/backtest/batch_backtest.py:452-478`、`myquant/backtest/batch_backtest.py:558-567`、`myquant/core/constants.py:152-155`
- **证据等级**：B（两处复用判定逻辑与常量注释可逐字复核）
- **优先级 / 风险等级**：P0 严重
- **事实**：增量调度 `plan()` 以 `self._store.get_changed_symbols(fps)` 判变更，兜底条件仅为 `cached.get("fingerprint_hash") != fp.hash_value`（`incremental_backtest.py:321-333`）；写入缓存时只写 `fingerprint_hash` 与 `computed_at`（`:393-395`），而 `compute_fingerprint` 覆盖的是数据内容/行数/日期区间/列/频率/来源（`data_fingerprint.py:89-98`），不含策略名、策略参数、成本模型、区间与初始资金。批量回测则有 `_config_signature()`（`batch_backtest.py:452-478`，含 `strategy_name/params/commission/slippage/benchmark/risk_free_rate/start_date/end_date/initial_capital/frequency`），并在 `plan()` 中 `config_changed` 即全量重算（`:558-567`）。`core/constants.py:153` 注释亦自认："incremental 缓存按 fingerprint_hash 匹配（无 config_signature）"。
- **推断**：增量回测在"同一份数据、改了策略参数或成本假设"时会复用旧缓存结果，输出物与请求参数不匹配，即旧结果冒充新结果；该风险已被批量路径修复但未同步到增量路径。
- **未知**：上层脚本是否在改参数时强制 `--force-full`（若存在则风险被流程补偿）；增量结果是否用于对外报告或资金决策。
- **风险影响**：绩效评估与参数寻优结论失真，可能据此放大仓位或上线无效策略。
- **可能性**：控制部分执行偶发失效（批量路径有签名校验，增量路径缺失）
- **根因**：技术 — 双源缓存格式收敛时只统一了版本常量，未统一签名口径。
- **整改建议**：把 `batch_backtest.py:452-478` 的 `_config_signature()` 抽到共享工具（如 `myquant/utils/cache_sync.py` 或 `data_fingerprint.py`），在 `incremental_backtest.py:265-295` 的 `_load_results/_save_results` 写入并比对 `config_signature`，与 `constants.py:155` 的 `RESULTS_FORMAT_VERSION` 一并升版触发一次全量重算。
- **验证方法（DoD）**：同一数据、仅改 `params` 后运行增量回测，断言 `recomputed_count == len(symbols)`；缓存文件含 `config_signature` 字段；新增回归测试覆盖"签名不一致 → 全量重算"。
- **最坏情形**：以旧参数结果冒充新参数结果出具绩效报告，误导资金配置决策。
- **是否需法律顾问确认**：否
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-10 证据保留：日志 7 天保留与回测产物默认只留最近 1 期，存在删除审计证据的可能

- **维度**：容量与应急 / 证据保留（与合规交叉）
- **位置**：`myquant/utils/cleanup_service.py:695`、`myquant/utils/cleanup_service.py:698-728`、`myquant/utils/cleanup_service.py:84-93`、`myquant/utils/cleanup_service.py:135-136`、`myquant/utils/cleanup_service.py:429-440`、`myquant/core/logger.py:174-175`
- **证据等级**：B（清理规则与保留常量可逐字复核）
- **优先级 / 风险等级**：P0 严重
- **事实**：`LOG_KEEP_DAYS: int = 7`（`cleanup_service.py:695`），`scan_old_logs` 删除 `logs/` 下 `myquant_`/`myquant_error_`/`myquant_trade_`/`kunpeng_` 前缀且 mtime 早于 cutoff 的文件（`:688-728`），其中 `myquant_trade_` 为交易日志前缀（`logger.py:255-262` 实际写入 `{app_name}_trade_{日期}.log`）；`scan_rotated_logs` 无条件把 `logs/*.log.1` 列为 D 类待删（`:439-440`），而轮转为 10MB×5 份（`logger.py:174-175`）。回测产物：`DEFAULT_KEEP = 1`（`:136`），`plan_outputs_cleanup` 按期时间戳只留最近 1 期（`:811-825`），F 类可一键删除 `report_/equity_/trades_/summary_/result_` 与看板（`:84-93`）。保护清单保护 `quant.db`、`processed/*.parquet`、保留期内报告与批量增量缓存（`:26-34`），但**不含交易日志、告警记录、委托/成交明细的长期留存要求**。
- **推断**：交易与告警证据默认存活期约 7 天且可被一条命令或 GUI 勾选删除，历史上限仅由容量而非合规保留期决定；轮转文件被列为常规垃圾，可能先于 7 天被清掉。
- **未知**：监管要求的留痕期限与范围（须以证监会/交易所官网最新原文为准）；`outputs/` 与 `logs/` 是否有仓库外副本；GUI 勾选 K 类的实际操作人与审批。
- **风险影响**：事故或监管核查时无法提供交易与告警期间证据，构成举证不能。
- **可能性**：控制存在未执行（保留策略完整实现，但口径按运维便利而非合规期限设定）
- **根因**：合规 — 保留期未按监管留痕要求分级设定。
- **整改建议**：在 `myquant/utils/cleanup_service.py:695` 将日志保留改为分类分级常量（交易/告警/错误日志独立保留期，且默认值不小于合规期限），并把 `myquant_trade_` 与告警日志移出可清理集合（`:688-728`）；为 `scan_rotated_logs`（`:429-440`）增加保留期判断而非无条件删除；将清理产物同步至不可变归档目录（如 `backups/immutable_logs/<YYYYMM>/`）后再删除本地产物。**需持牌律师/合规官确认**留存期限与范围，条文以证监会/交易所官网最新原文为准。
- **验证方法（DoD）**：以 8 天前的 `myquant_trade_*.log` 与 `*.log.1` 构造样本运行 dry-run，确认交易/告警日志不在待删清单；归档目录中存在对应副本；保留期常量与合规意见书一致。
- **最坏情形**：监管问询或纠纷发生时交易与告警证据已被清理，无法举证。
- **是否需法律顾问确认**：是（涉及程序化交易日志/交易记录留存期限的法规适用性，需持牌律师/合规官确认，以证监会/交易所官网最新原文为准）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

### KP-P5-11 事故响应与无责复盘：未见事故分级/通报/升级预案，复盘记录与演练留痕缺失

- **维度**：应急与容量 / 事故响应与复盘
- **位置**：`docs/掘金量化仿真模拟盘_任务实施细则摘要.md:46`、`docs/工程待办/未完成事项摘要_实盘前待办.md:197`、`config/live_config.yaml:3355-3357`
- **证据等级**：D（仅文档/配置声明；`docs/` 下 grep「事故响应/应急响应/无责/postmortem/值班/on-call」零命中）
- **优先级 / 风险等级**：P1 高
- **事实**：应急预案仅以计划表形式存在：T12「场景化异常预案演练」验收标准为"各场景处置闭环、留痕"、责任人"风控合规/IT"、优先级 P1（`掘金量化仿真模拟盘_任务实施细则摘要.md:46`），以及"保留所有实盘日志，便于事后复盘"（`未完成事项摘要_实盘前待办.md:197`）的单方声明。`config/live_config.yaml:3355-3357` 仅注册了名为 `health_check` 的定时任务条目（无对应处置动作）。全仓未检索到事故分级标准、通报路径、值班交接、无责复盘模板，也未检索到任何已完成的演练/复盘留痕文件（`docs/` 下无演练记录）。
- **推断**：事故响应依赖个人经验，无分级与时限；即便发生故障，也没有强制的无责复盘与整改跟踪机制，"演练留痕"停留在待办项。
- **未知**：T12 是否已实际开展并留痕于项目外（如邮件/会议记录）；是否有事故登记台账；组织是否为单人运维。
- **风险影响**：同类故障重复发生且无闭环整改；事故处置与责任界定无书面依据。
- **可能性**：控制完全缺失（无预案文本、无分级、无复盘留痕）
- **根因**：治理 — 应急与复盘机制未随实盘功能一并建设。
- **整改建议**：在 `docs/` 新增《实盘事故响应与复盘制度》，定义事故分级（P0~P3）、MTTD/MTTR 目标、通报路径与升级时限、以及 5 个工作日内出具无责复盘（含时间线、根因、整改项与责任人）的要求；将 `config/live_config.yaml:3355-3357` 的 `health_check` 任务替换为可执行处置动作（如触发告警 + 降级/暂停开仓）；在 `docs/` 建立演练与复盘台账并逐次留痕。
- **验证方法（DoD）**：制度文件评审通过并存档；至少完成 1 次断网/行情中断演练且在台账中记录场景、时间、处置、耗时与整改项；无责复盘记录可检。
- **最坏情形**：连续故障无人复盘，同类问题反复导致资金损失累积，且无任何体系化处置依据。
- **是否需法律顾问确认**：是（事故处置留痕与程序化交易异常报告义务的法规适用性需持牌律师/合规官确认，以证监会/交易所官网最新原文为准）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认
