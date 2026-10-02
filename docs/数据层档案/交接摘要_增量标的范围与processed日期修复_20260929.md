# 交接摘要：增量标的范围"焊死 3226"排查与 processed 日期修复

- 交接时间：2026-09-29 20:40
- 项目：D:\Projects\Pythonwdhmm（鲲鹏量化交易系统）
- 前序对话主题：恢复通达信全量下载并改名「通达信全量数据」+ 增量旁路（已提交 #68）→ 增量清洗补派生量计算（已提交 #69）→ 增量标的范围"焊死 3226"排查
- 当前阶段：**用户正在执行「重新下载所有数据 + 重做一遍全量清洗」，完成后需按本文档第 5 节核实清单逐项核对**。全程未改动任何代码（用户明确要求：未经确认不动代码）。

---

## 1. 任务背景

用户报告：今天重做全量数据，清洗进库 3208 只；但增量补 29 日数据时补的是 3226 只。用户认为**增量补充应以 processed 干净数据为准（"数据里有什么，增量就补什么"），增量标的范围被焊死为配置池 3226 只是设计缺陷**。

排查结论（均已取证，见第 3 节）：用户方向成立——**主责在增量管道**；但"18 只 ST/退市票被专项整理正常挡掉"的前提不成立（数据管道内无专项整理机制，18 只是执行时点异常 + 静默跳过）。

---

## 2. 当前数据实况（2026-09-29 实测）

| 项 | 数量/状态 | 来源 |
|---|---|---|
| DB market_data distinct symbol（daily/hfq） | **5335** | quant.db（11:27-11:29 通达信全量下载写入，created_at 全部 2026-09-29）|
| 配置池（backtest_kunpeng.yaml stock_pool.codes） | **3226** | 三因子预设过滤池；池 ⊂ DB；池 ∩ processed = 3226 |
| 全量清洗本次成功（clean_run_meta source='full_clean:full_clean_20260929_113551'） | **3208** | completed；另 18 只池内标的 status≠ok 被静默跳过（无元信息记录）|
| 689009.SH | 命中排除规则"68"被排除 | 14 条规则实证 |
| processed parquet 总数 | **3227** | 3208 只"索引式"（日期=索引，无"日期"列）+ 19 只"扁平式"（带"日期"列）|
| processed 日期状态 | **3208 只日期信息已损坏**（RangeIndex int，无日期）| 19:29-19:31 增量更新批量改写 3206 个文件所致 |
| sync_status | 5335 completed，最新 2026-09-29 | 11:27 全量下载写入 |
| datasets/bad | 0 文件（11:35 前后无归档）；dry-run 复现后新增 16 个（20:37）| 详见第 3 节 |
| outputs 清洗报告 | 无 `clean_all_full_clean_20260929_113551.json`（**被清理脚本误删**，见第 4 节）| 本次 dry-run 已生成 2 份新报告（203656/203739）|

---

## 3. 完整证据链与结论

### 3.1 增量管道 = 主责（结构性确认）
- 增量入口 `myquant/gui/data_download_dialog.py` `run_daily(scope="pool")`：标的 = `_load_pool_codes()` → `config/backtest_kunpeng.yaml` 的 stock_pool.codes = **3226 只**（不看 DB/processed 实际存量）。
- 增量**未应用排除规则**（无 filter_excluded_symbols 调用）；注释只说"清洗规则同全量"（指 bar 清洗参数，不含标的过滤）。
- 实证：689009.SH 被"68"规则排除，但增量照样把其 20260929 数据写进 processed 扁平文件。
- 增量写 processed 走 `myquant/data_access/manager.py` `save_cleaned_incremental_parquet`（约 998–1169 行，含 `_en_to_cn` 12+2 字段映射）。
- 对比：
  - 全量管道：DB 全量 → resolve_symbols（14 条排除规则）→ worker_clean → 写回 DB → export_all 重建 processed（keep_symbols 白名单）
  - 增量管道：配置池 3226 → update_symbols_batch（写 DB）→ save_cleaned_incremental_parquet（无排除规则）→ 写 processed

### 3.2 18 只"池内但未清洗"标的 = 时点性执行异常（非规则/非专项/非数据不可洗）
18 只清单：000584.SZ 000622.SZ 000627.SZ 000851.SZ 002231.SZ 002336.SZ 002750.SZ 600190.SH 600200.SH 600387.SH 600462.SH 600705.SH 600804.SH 601028.SH 601989.SH 603003.SH 603056.SH 603388.SH（多为基础面/市场认知中的 ST、退市整理、长期停牌股，但**代码内无此类专项机制**）。

四层证据：
1. 模拟 14 条排除规则（`simulate_resolve.py`）：这 18 只 **0 命中**，不在任何排除规则内；模拟保留集 3226 = 配置池 3226（池即"DB 全量 - 排除规则"）。
2. 两次 dry-run 复现性清洗（20:36/20:37，`--dry-run --symbols`）：**18 只全部 ok，0 error/0 empty/0 failed**（4 秒完成）；14 条规则下 689009.SH 被"68"排除、其余全过。
3. dry-run 为 18 只产出 **16 个 bad 归档**（`datasets/bad/bad_bar_600190.SH_20260929_203742.json` 等）：证明清洗管线对其**完整正常运行**（检测巨幅跳变 22%-75% → drop → 归档 → ok）。若为管线异常则不会有 bad 归档。
4. 这些跳变正是 `reset_base_gap_days=30`（bar_cleaner.py）应对停牌/重组复牌跳变的设计场景——数据本身可洗。

结论：11:35 的失败是**清洗执行时点的异常**（很可能与 11:27-11:29 刚写入 DB 的原始数据状态有关），19:29 增量重拉数据后已自愈。**当时的具体报错已无法恢复**（见第 4 节）。

### 3.3 processed 日期破坏 = 增量合并逻辑（决定性复现）
- 全量清洗 12:0x rebuild processed 产物为**索引式** parquet（index=日期 datetime64，无"日期"列；`scripts/数据导出_DB到processed.py` 的 `load_symbol_data` set_index('trade_date') + `export_all` to_parquet(index=True)）。
- 增量 `save_cleaned_incremental_parquet` 合并：`cleaned_df.reindex(columns=existing_df.columns)`（把"日期"列丢掉）→ `pd.concat([existing_df, cleaned_df], ignore_index=True)`（存量日期索引被重置为 RangeIndex）→ `drop_duplicates(subset=["日期"])` 因无"日期"列而不生效 → **写出无日期文件**。
- 实证：`repro_export_roundtrip.py`——`load_symbol_data`+`to_parquet(index=True)` 往返均带日期（index.name='日期'），但现存 processed/000001.SZ.parquet 无日期（RangeIndex，8344 行）；mtime 显示 19:29-19:31 有 3206 个文件被改写。
- 下游影响：回测/实盘读 processed 时 3208 只时间序列断裂。

### 3.4 数据实况关键数字
- 全量清洗 resolve = DB 5335 − 排除 2109（300/301/302 创业板、688/68 科创板、200/201 深B、900 沪B、000.SH 指数、399/880/5.SH/1.SZ/4/8/92/99）= **3226** → 实际 completed 3208（18 只静默跳过）。
- processed 19 只扁平文件最新日期（证明增量绕过清洗、补了旧数据）：000584.SZ=20250710、000622.SZ=20250715、000627.SZ=20250813、000851.SZ=20250926、002231.SZ=20260129、002336.SZ=20250703、002750.SZ=20250626、600190.SH=20250718、600200.SH=20251229、600387.SH=20250704、600462.SH=20250714、600705.SH=20250402、600804.SH=20250630、601028.SH=20250326、601989.SH=20250812、603003.SH=20250630、603056.SH=20260120、603388.SH=20251110、689009.SH=20260929。

---

## 4. 已修正的认知（重要）

**"清洗报告未落盘"是错误推断，已撤回**。真实机制：
- 11:35 报告正常生成（`clean_all_full_clean_20260929_113551.json`，GUI 全量清洗 batch_id 前缀 `full_clean_*`）。
- 用户运行过 `scripts/垃圾数据清理.py --execute` → **A 类"测试残留"规则误删**：`myquant/utils/cleanup_service.py:77` `TEST_CLEAN_REPORT_PATTERN = "clean_all_full_clean_*.json"`。
- **规则缺陷**：`full_clean` 是生产 batch_id 前缀（非测试），生产全量清洗报告每次都会被清理脚本误删 → failed_symbols 随报告丢失，审计痕迹不可追溯。
- 另注意：B 类规则会删 `backups/backup_*_quant_before_clean.db`（真实清洗备份同名，同样有误伤风险）；A 类"孤儿 bad"规则固定模式 `bad_bar_*_20260830_225*.json`（本次 16 个新 bad 不匹配，暂安全）。

---

## 5. 待办事项

### 5.1 用户当前行动（进行中）
用户正在：**重新下载所有数据（建议走「通达信全量数据」full_market 入口重建 DB 原始层）→ 重做一遍全量清洗（GUI「数据清洗」或 `scripts/数据全量清洗.py`）**。完成后需执行核实清单：

| # | 核实项 | 判定 |
|---|---|---|
| 1 | DB 标的数（full_market≈5335；仅池内=3226）+ sync_status | 记录实际值 |
| 2 | 本次 clean_run_meta completed 数量 → **18 只成功还是再次失败** | 关键判据 |
| 3 | 排除规则生效：689009.SH 被"68"排除 | 14 条规则下应排除 |
| 4 | 报告文件 `outputs/clean_all_full_clean_*.json` 是否生成 | 验证 save_report 正常（**清洗后先别跑清理脚本**）|
| 5 | processed 重建：文件数、日期是否恢复（索引式带日期）、最新交易日、派生量列 | 3208/3226？日期必须恢复 |
| 6 | processed vs 清洗成功集 vs 配置池 3226 三方对比 + 18 只去向 | 明确记录 |
| 7 | 若 18 只仍失败 → 从报告抓 failed_symbols 与报错（本次必须留痕）| 上次丢失的根因 |

### 5.2 四条修复方案（未动代码，等用户拍板）
1. **增量标的源 = 最近一次全量清洗成功集**（从 clean_run_meta 最新 full_clean run 的 completed symbols 取）→ 18 只失败票、689009 自动不补；贴合"数据里有什么就补什么"。
2. **全量清洗失败落元信息/报告**：failed_symbols 进 clean_run_meta（独立于报告文件）+ 修复清理规则误删（TEST_CLEAN_REPORT_PATTERN 改为只匹配测试报告前缀，如 clean_all_TEST_*；或把 clean_all_full_clean_*.json 列入保护清单）。
3. **重建 processed 修复日期**（用户正在做的重做全量清洗即此步；若仅想修日期可只重跑 export_all）。
4. **schema 统一**：全量导出改为扁平式（带"日期"列）与增量合并天然兼容（reindex/concat/drop_duplicates 正常）；或增量合并适配索引式存量（治标）。**必须与 3 搭配，否则下次增量还会抹日期**。

### 5.3 所有代码改动须遵守
- `D:\Projects\Pythonwdhmm\docs\代码变更治理规则.md`（蓝图即宪法：业务逻辑归 myquant、追加回写记录、同步目录树、补测试）
- `D:\Projects\Pythonwdhmm\docs\py文件代码规则.md`
- 蓝图文件 `docs/project_layout_gear2.md`：UTF-8 BOM + CRLF；文末回写格式 `N. ✨【回写记录·YYYY-MM-DD·标题】…`；目录树位置 tests≈476 行、manager.py≈662、derived_metrics.py≈690；最新回写 #69。下一次新里程碑编号建议 #70。
- Edit 工具对 D 盘文件曾 Permission denied → 用"补丁脚本（精确字符串替换+断言）+ Python 执行"模式；写回显式 `write_bytes(b'\xef\xbb\xbf'+body.encode('utf-8'))`。
- 测试文件：tests/unit/test_data_clean_incremental.py（10 项）、test_update_symbol_incremental_start.py（11）、test_data_incremental_clean_dual.py（4）、test_data_download_pool_scope.py（8）。

---

## 6. Git 与环境

- 已提交：commit `8699b95`（#69+#68，5 文件：manager.py、data_download_dialog.py、project_layout_gear2.md、test_data_clean_incremental.py、test_data_download_pool_scope.py）。
- 提交身份：仓库级 .git/config（Pythonwdhmm Dev / dev@pythonwdhmm.local）——随 .git 走，项目移到另一台电脑仍有效（远程登录凭据不随项目走）；仓库无远程。
- 环境：用户机器为 wdh52；Python 解释器 **`D:\ProgramData\anaconda3\envs\conda_wdh\python.exe`**（原 C:\Users\dgd\anaconda3 路径已失效）；**Bash 不可用，用 PowerShell 工具**；沙箱按需授权、无 sudo。
- 工作区：新 `C:\Users\wdh52\AppData\Local\Doubao\User Data\Default\.doubao\agent_mode\workspace\.sessions\38444628425934594\agents\m_0cwpuVcG5ZG\`（旧 dgd 工作区不可写）。审计脚本（simulate_resolve.py / repro_worker_clean.py / check_created_at.py / repro_export_roundtrip.py / check_diff_pool_clean.py / check_processed_format.py / check_symbol_scope.py 等）在该工作区，可复用。
- 用户偏好（画像提示）：重视结构化文档交付、量化系统工程化、A股量化；要求事实可追溯、数字有来源；所有命令行操作"始终询问"。

---

## 7. 下一步建议（给新对话）

1. 先读本文档（自包含）确认上下文，再核实用户重做后的数据（第 5.1 清单）。
2. 核实完成 → 与用户确认四条修复方案（5.2）→ 按代码规则实现 → 补测试 → 回写蓝图 #70 → 全量 tests/unit 验证（历史基线：1587 passed/2 skipped/0 failed）。
3. 交付说明需列验证方式与覆盖范围；避免在用户未确认前改动任何代码。
---

## 8. 重做后核实结论（2026-09-29 晚追加）

> 本节由用户按第 5.1 核实清单重做后，经现场核查追加（事实来源：本次会话实测；与第 2-3 节"交接时点"状态对照，差异已在各条注明）。

### 8.1 核实清单逐项结果（用户重做：重新下载全量数据 + 重做全量清洗，迁移至家里电脑 wdh52 后增量补充时发现问题）

| # | 核实项 | 结果 | 与交接时点对照 |
|---|---|---|---|
| 1 | DB 标的数 + sync_status | 全量下载后 DB 存量、sync_status 均正常 | 同第 2 节（5335 写入） |
| 2 | 本次 clean_run_meta completed | **3208 只成功；18 只池内标的仍缺失**（000584.SZ 等 18 只不在 processed） | 第 2 节"18 只静默跳过"在重做后**复现**，非偶然 |
| 3 | 排除规则生效（689009.SH） | 14 条规则下 689009.SH 仍被"68"排除 | 一致 |
| 4 | 报告文件生成 | 正常生成（本次未跑清理脚本） | 第 4 节误删认知已修正 |
| 5 | processed 重建：文件数、日期 | **日期已恢复完好**（重建后为索引式 datetime64 带日期），3208 只时间序列可用 | 第 3.3 节"3208 只日期损坏"已随重做修复 |
| 6 | processed vs 成功集 vs 配置池 | 3208 成功集正常写入；18 只缺失与 ST/退市属性无代码层关系（见 8.2） | 第 3.2 节结论复核成立 |
| 7 | 18 只 failed_symbols | 缺失标的在下载层即未入库（见 8.2 决定性实测），清洗层无 failed 报错可抓 | 与第 3.2 节"时点异常已自愈"表述对齐 |

### 8.2 ST/*ST/S/SST 模块核查（用户询问"项目结构是否无 ST 剔除模块"后只读核查，已定性）

- **代码层无任何 ST 剔除实现**：全 myquant 代码库无名称/风险警示过滤逻辑。
- `config/live_config.yaml:3328` 风控段有 `risk_control.exclude_st: true`（注释"排除ST股票"），但**全库 0 消费点**（预留未接线）。
- 通达信 `tdx_source.get_stock_list`（tdx_source.py:310-360）只按代码前缀过滤（沪 60/68/90、深 00/30/20），不识别 ST。
- `full_clean_service.DEFAULT_EXCLUDE_RULES` 为 14 条代码前缀规则（000.SH/399/880/5.SH/1.SZ/30.SZ/688.SH/4/8/92/68/90/20/99），与 ST 无关。
- **决定性实测**：`TdxDataSource().get_stock_list()` 返回 **5344 只**，18 只缺失标的**全部在列表内**；结合 sync_status 无记录（进过下载列表无论成败都会写）→ **定性：18 只缺失 = 下载层未入库（ST/停牌类标的通达信停更所致），与有无 ST 模块无关**。

### 8.3 后续进展与本任务衔接

- **ST 剔除能力已实施**（蓝图 #70，2026-09-29 晚完成）：`full_clean_service.py`（ST 名单族 load/match/filter/refresh + resolve_symbols 串联）、`data_clean_dialog.py`（排除规则框内「剔除 ST / *ST / S*ST / SST」选项组 + 刷新/编辑名单按钮）、新增 `config/st_blacklist.example.yaml`（模板）与 `tests/unit/test_st_blacklist_clean.py`（29 项）；验证：py_compile 通过、新单测 29 passed、清洗链路回归 126 passed、全量 tests/unit = **1616 passed / 2 skipped / 0 failed**、GUI 离屏冒烟 + 真实 DB 端到端剔除生效。
- 第 5.2 四条修复方案未动代码（除上述 #70 独立任务）；**18 只缺失的修复（下载层补数据）用户明确"暂时不动手"**。
- 工作区 git 含本任务 3 改 + 2 新文件，提交与否由用户拍板。

---

## 9. 增量端到端亲测 + 列模板修复 + 规则化根治待办（2026-10-01 晚追加）

> 本节为增量 F 清单落地（#77，执行A）后的端到端真实验证结论，与 8.x 衔接（用户 10-01 在 GUI「增量更新日线」亲测，从 3208→3226）。

### 9.1 核实结论（已入库）

- **缺口属实**：10-01 前 processed 全部 ≤09-28（3208 只，3185 只=09-28）；DB 09-29/09-30 均 0 行；交易所公告证实 09-29/09-30 为交易日（中秋 9-25~27 休市、国庆 10-01~07 休市）。本地 TDX .day 已含 29/30 → 无需等开市即可补。
- **增量后实测**：processed 3208→**3226**（3186 只到 09-30、3 只到 09-29、37 只停牌 <09-29）；DB 全量 5317→5335（+18 只池内标的首次入库）。
- **范围规则确实引入增量（F4，防焊死）**：processed 3226 = `resolve_incremental_symbols` 对 DB 全量 5335 实时推导的规则内集合（排除规则剔 2109、ST 剔 0）——**精确相等、零剔除零多余**；池=3226 与规则内=3226 恰好同数。
- **18 只新增票溯源（trace_18.py）**：000584.SZ/000622.SZ/000627.SZ/000851.SZ/002231.SZ/002336.SZ/002750.SZ/600190.SH/600200.SH/600387.SH/600462.SH/600705.SH/600804.SH/601028.SH/601989.SH/603003.SH/603056.SH/603388.SH——18/18 在池配置内、18/18 经范围规则保留（正常 A 股代码非 ST）、DB 行情各行=640（2022 年~2025/2026-01 停牌）、financial_data=0、TDX 本地名单无名称。**即：池内规则内老停牌标的首次入库（此前下载层未入库），非规则漏网**。
- **整库无变化（问题2）**：存量 3208 只结构全检通过；DB 逐值抽样对比 20 只 81282 行 **0 不一致**（存量与新增 09-29/09-30 均与 quant.db 权威源一致）。

### 9.2 列模板缺陷与修复（蓝图 #78，已提交 0ec99a4）

- **缺陷**：18 只新建 parquet 仅 10 列（缺 换手率/流通股本/市盈动/每股收益）。根因：增量"无存量新建"分支直接写清洗后现存列，B 类派生量依赖 financial_data（#69 缺失跳过）→ 列不建；存量追加靠 reindex 对齐故不暴露。全量导出为固定 14 列模板。
- **修复**：`manager.py` 模块级 `PROCESSED_COLUMN_TEMPLATE`（14 列，与全量导出 load_symbol_data 一致）+ 无存量新建分支 reindex 对齐；18 只已有文件重写对齐（只补列不改行，逐值校验）；单测 +1 项（现 14 项）。
- **验证**：py_compile PASS；增量链路相关 53 passed；全量 tests/unit **1653 passed / 2 skipped / 0 failed**；端到端复验 3226 文件结构异常=0、DB 逐值 81282 行 0 不一致。

### 9.3 全池盘点（只读）与待决策项

- 池 3226 只按 DB 最后交易日分类：A 类·无行情 0；**B 类·长期停牌/退市（最后<2026-06-01）=20**（18 只本次新建 + 000638.SZ 最后04-13、600355.SH 最后04-03 全量已导出 14 列正常）；C 类·2026 有数据但未到 09-29 =17（2026-06~09 停牌，如 000004.SZ 07-13、600293.SH 09-28）；活跃 3189。
- **用户拍板方向（10-01 晚）**：清除 18 只停牌/退市老标的（processed 18 个 parquet 已确认）；**"长期停牌/退市不应进干净库"规则化根治 = 方案 A（待办，用户下班回家继续）**。

### 9.4 ⏳ 待办：规则化根治（方案 A，用户已选，回家后继续）

1. `config/data_clean_run.json` 排除规则新增「长期停牌/退市」项（默认阈值：DB 最后交易日距今 > 90 天 → 排除；阈值可配，动态推导、防焊死）。
2. `full_clean_service.resolve_incremental_symbols` 串联该规则（需接入 DB 最后交易日查询；与全量 resolve_symbols 同源，注意全量导出也受影响——B 类 20 只将全部被规则排除）。
3. 三处同步清理：processed 18 只移备份（不硬删，可追溯）；quant.db market_data 18 只行情；`backtest_kunpeng.yaml` stock_pool.codes 剔除（**必须同步，否则增量从 TDX 复发**）。
4. 单测（新规则项：阈值内保留/超阈值排除/边界日/无 DB 数据回退）+ 蓝图回写 #79 + 全量回归。
5. 完成后补记交接摘要（本项闭环）。

### 9.5 Git 与环境（10-01 提交后状态）

- 已提交：`84d68a3`（#71-76，含 #70 ST 主代码）→ `4b2af10`（#70 补齐：test_st_blacklist_clean.py 29 项 + st_blacklist.example.yaml）→ `0ec99a4`（#77 增量 F 清单 + #78 列模板，13 文件 938+/86-）。**工作区 CLEAN**。
- 环境（本次会话=dgd 机器）：Python `C:\Users\dgd\anaconda3\envs\conda_wdh\python.exe`（3.11.16）；Bash 不可用，一律 PowerShell；项目根 `D:\Projects\Pythonwdhmm`（与交接 8.x 的 wdh52 机器为同一项目迁移，机器不同解释器路径不同）。
- 一次性验证/修复脚本存于会话工作区（verify_gap_2930 / verify_inc_after / verify_inc_rules_and_integrity / verify_18_bad_cols / trace_18 / pool_audit / fix_18_cols），可复用；fix_18_cols.py 为 18 只 14 列重写脚本（已执行，18/18 PASS）。

### 9.6 ✅ 规则化根治完成（蓝图 #79，2026-10-01 wdh52 机器，用户拍板"规则外全部"）

- **阈值确定**：60 天（自然日，≈42 交易日；正常停牌 ≤10 交易日≈15 天，余量充足，覆盖暂停上市/长期重组停滞/退市整理）。⚠️ 与 9.4 草拟的"90 天"不同，以 60 天为准。
- **规则实现**：`full_clean_service.py` 新增 `filter_long_suspended_symbols`（market_data(daily,hfq) MAX(trade_date) gap > 阈值剔除、等于保留；DB 无记录/解析失败/查询异常/阈值≤0 均保留不阻断）；`resolve_incremental_symbols` 串联（ref_date 可注入、stats 增 excluded_suspended、config 缺省 0 不启用）；`resolve_symbols` 全量侧用 dq.conn 同口径串联；`data_clean_dialog.py` 新增「剔除长期停牌/退市」QSpinBox（默认 60/0=不启用）+ 实时预览 + `_collect_config` 写 `exclude_suspended_days`（防 GUI 重写 config 丢字段）；`config/data_clean_run.json` 加 `exclude_st:true` + `exclude_suspended_days:60`。
- **实测影响面**：DB 全量 5335 → 规则内 **3196**（长期停牌排除 **30 只** = B 类 20 + C 类停牌>60 天 10 只；用户点名 18 只 100% 命中）。
- **三处同步清理（DB 不删——修正 9.4 第 3 步）**：① processed 移备份 30 只 → `backups/processed_excluded_20261001/`，processed=3196=规则内（差集空）✅；② 池 `backtest_kunpeng.yaml` 剔除 31 只（30 长期停牌 + 689009.SH"68"前缀规则外历史遗留）→ 池 3195（⚠️ 缺 601091.SH：沪市新股历史遗漏，非本次范围，披露）；③ **quant.db 行情不删**——规则判定依赖最后交易日，删库后"无记录=保留"会使拦截失效（复发机制）；DB 原始层保留历史无副作用（全量/增量均已被规则挡住）。
- **验证**：py_compile PASS；test_resolve_incremental_symbols.py 12 passed（+5 项：60 天启用剔除/边界==阈值保留/DB无记录保留/未配置不启用/DB缺失不报错）；清理 dry_run 清单与实测一致（30/31）；全量 pytest tests/unit 以当日回归日志为准。
- **Git**：未提交改动 = full_clean_service.py / data_clean_dialog.py / config/data_clean_run.json / test_resolve_incremental_symbols.py / project_layout_gear2.md（#79 回写）/ 本摘要 9.6 节。提交方式（单 commit 或分拆）由用户拍板。


### 9.7 ⏳ 数据层深度审查（#80，2026-10-01 wdh52 机器，只读审查，结论待新对话拍板修复）

**背景**：用户要求"严格审查数据层：数据拉取、财务数据拉取、本地及网络数据抓取，看是否还有无重大 BUG"。本审查全程只读，**未改任何代码**。

**审查范围**（11 文件精读 + 5 个决定性实测脚本）：
- 精读：`tdx_source.py` / `base.py` / `web_source.py` / `composite_source.py` / `fetcher.py` / `factory.py` / `manager.py`（update_symbols 385-448 + _save_to_database 800-848）/ `derived_metrics.py` 全文 / `financial_sync_service.py` 全文 / `akshare_source.py`（财务段）/ `data_download_dialog.py`（入口段）
- 实测脚本（agent workspace m_0cwpjmv7D5g/，可复用）：audit_adjust.py（复权口径）/ audit_units.py（TDX 原始字段）/ audit_src.py+audit_src2.py（数据源分布）/ audit_tencent.py+audit_tencent2.py（腾讯混入核验），输出 $env:TEMP 下 a80/u80/s80/t80 日志

**🔴 P0 级发现（正确性，必须处理）**：
1. **P0-1 全库复权标签失真**：market_data **16,753,492 行 adjust='hfq'，实际全为 TDX 不复权价**。证据：600519.SH 除息日 2024-06-19（10派308.76）DB close=1501.00 == TDX 原始 1501.00（真实跳空 -20.5 元未复权）；000001.SZ 2023-07-21 全等；3 只抽样近 10 日与 TDX **0 不一致**。根因：`tdx_source` 返回原始价（注释自认"复权由上层处理"），`manager._save_to_database` **无转换直通入库**，adjust='hfq' 原样写。影响：① 除权日涨跌幅/振幅假跳变（600519 2024-06-19 被算成 -3.5% 虚假跌幅）；② 高送转除权（-50%）会触发清洗 `max_price_jump_pct` 跳变检测**误判剔除**；③ 回测按 hfq 语义消费实为不复权序列；④ derived_metrics 注释"库内为 hfq"与事实不符。
2. **P0-2 腾讯 hfq 孤岛混入 DB（#77"清除18只"未真正落库）**：全库数据源分布 = tdx **16,741,972** + tencent_http **11,520**（18 只 × 640 行，即 #77 用户点名 18 只）。实证：000584.SZ 2022-09-06 DB close=**34.89**（真后复权价，不复权应 ~4-5 元）→ **跨源量纲混合**。现状：processed 已清、池已剔、TDX 无数据、financial_data=0 → **孤岛**，最新日期停在 2025-2026 初不再更新。用户以为已清，实际 DB 原始层仍在。

**🟠 P1 级发现（数据质量，建议处理）**：
3. **P1-3 B 类派生量防未来违规**：`derived_metrics.py` L203 `fin.report_date <= trade_date` 用**报告期**而非**发布日**——一季报（报告期 03-31）4 月底才披露，4 月初交易提前使用；中报/三季报同理提前 1-2 个月。financial_sync_service 仅拉取端按 end_date 过滤报告期，未解决使用端时点。
4. **P1-4 股本反推近似**：`float_share = 最新财报 circ_mv / 最新行情 close`（财报 circ_mv 对应财报日市值，高送转/增发后与最新价不匹配 → 换手率系统性偏差；注释已承认近似，误差可达数倍）。
5. **P1-5 is_suspended 判定失效**：manager L842 `is_suspended = volume==0`，TDX .day 停牌日**不写记录**（无行）→ 几乎永不触发。
6. **P1-6 网源 volume 单位风险（种子）**：TDX .day volume 字段**实测单位=股**（5951416=594.5 万股，与 91.8 亿成交额/均价 1543 吻合），tdx_source 注释"手"是**错误注释**（数值链路自洽）；但网源（东财/新浪/腾讯）volume 单位=**手**，若标的走网源入库 → 换手率/量比差 **100 倍**（当前 18 只孤岛未消费未爆雷，网源入口存在即风险）。
7. **P1-7 成交额 float32 精度**：amount 用 float32（91.8 亿精度误差 ~64 元，实测 DB 值 9177636864.0 已非整数），建议 double/INTEGER(分)。

**🟡 P2 级发现（健壮性，择机）**：8. 交易日历=工作日近似（base.get_trading_dates 无节假日调休）；9. akshare 财务兜底 report_date=今天（L745/798/878，入库后与历史交易日不匹配）；10. FINANCIAL_EXCLUDE_RULES 14 条硬编码（与 config 同口径未外置）；11. tdx get_stock_list 缺北交所（与排除规则一致无害）；12. 上证 B 股 900xxx 归 A 股清单（依赖规则兜底）；13. 量比 5 日均量含停牌 0 量日（TDX 无 0 行，网源可能有）→ 停牌后首日量比失真。

**建议修复方向（待新对话拍板）**：
- **P0-1 复权口径二选一**：A) 明确 DB="不复权原始层"，adjust 标签改 raw/none + 网源拉取强制 adjust="" 对齐（**推荐**，改动最小）；B) 建复权因子表做真 hfq。
- **P0-2 18 只孤岛**：按 #77 原意补执行 DB 移备份清理（不硬删）或 is_deleted 标记。
- **P1-3** financial_data 增公告日列改按公告日判定；或保守滞后窗口（Q1+1月/中报+2月/Q3+1月）。
- **P1-4** 优先用财务源股本字段，否则除权事件修正；**P1-5** is_suspended 改交易日历缺失判定；**P1-6** 网源 volume ×100 统一为股；**P1-7** amount 改 double。
- **优先级建议**：P0-1 口径决策 → P0-2 孤岛清理 → P1-3 PE 未来函数 → 其余。

**遗留衔接**：本审查未动代码；**#79 git 提交（5 文件，full_clean_service.py / data_clean_dialog.py / test_resolve_incremental_symbols.py / project_layout_gear2.md / 本摘要 9.6 节）仍未执行**，提交方式（单 commit 或分拆）由用户拍板；601091.SH 池缺失已披露未处理。新对话续做时默认遵守代码治理规则（查蓝图/业务逻辑归 myquant/补单测/py_compile+pytest/回写蓝图 #80/交接摘要，先出改动清单等拍板）。
## 9.8 【2026-10-01 晚】P0-1 复权口径修复已完成（蓝图回写 #81）

用户拍板方案 A（明确 DB=不复权原始层，adjust 标签 'raw'）+ 网源强制不复权对齐，7 组全部执行完毕：

1. **DB 迁移（一次）**：market_data 16,753,492 行 adjust 'hfq'→'raw'（UPDATE 全命中）；迁移前备份 backups/quant_db_bak_before_p0_1_20261001_222410.db（5.55GB 可回滚）；迁移后 hfq=0、raw=16,753,492、全市场 raw 标的 5335
2. **默认值改造（9 文件）**：sources 六源 + queries + fetcher + manager 默认 adjust→"raw"；akshare/web adjust_map 加 "raw": ""（东财 adjust="" / 腾讯 day，跨源量纲混入源头切断）
3. **读库口径（4 文件）**：full_clean_service / scripts/数据导出_DB到processed.py / data_download_dialog / financial_service 全部 'hfq'→'raw'；端到端冒烟通过（默认查询命中 600519、raw 5335、hfq 残留 0）
4. **注释/config/UI（12 文件）**：schema/derived_metrics（"库内为后复权"错误注释修正）/tdx_source docstring/config example/data_loader/kunpeng_param_scanner/batch_backtest_tab（复权下拉 3 项→1 项"不复权原始层 (raw)"）/backtest_config_dialog/data_clean_dialog（补漏）等
5. **测试**：8 测试文件 15 处 hfq→raw 同步 + 新增 tests/unit/test_adjust_raw_semantics.py（4 项：默认值/入库标签透传/查询隔离/网源 raw→不复权映射，零网络）
6. **验证**：py_compile 全项目（myquant/scripts/tests）PASS；新增单测 4 passed；全量 pytest tests/unit = **1662 passed / 2 skipped / 0 failed**
7. **蓝图**：回写 #81 + 目录树登记 test_adjust_raw_semantics.py

**遗留（披露，勿删）**：
- 除权日假跳变（600519 除息 -3.5% 虚假跌幅 / 高送转触发 max_price_jump_pct 误剔）**仍在**——方案 A 只诚实化标签，真复权需方案 B（复权因子表，另立任务拍板）
- P0-2 腾讯 hfq 孤岛 18 只 / 11,520 行仍在 DB（网源改不复权后不再新增，存量清理待 P0-2 任务）
- live 实时链路 qfq 独立于 DB 未动（本地历史 raw 预热 + 实时 qfq 量纲错位为既有遗留）
- #79 的 git 提交（5 文件）仍未执行，待用户拍板；本次 #81 改动（含 DB 迁移）一并待提交

## 9.9 方案B复权因子表落地（2026-10-01，P0-1 配套）
用户 4 项拍板：全市场 5317 / B1 消费落点（processed 保持 raw，回测现算）/ 清洗跳变豁免 / hfq+qfq。已落地：adjust_factor 表（9 张表）+ factor_sync_service.py + scripts/复权因子同步.py；全市场拉取 5316/5317 成功、63,374 事件（689009.SH CDR 无因子，降级 raw）；data_loader hfq/qfq 内存现算（防未来按 end_date 截断）+ bar_cleaner 除权跳变豁免；新增 3 测试文件 23 项单测；全量 pytest tests/unit = 1685 passed / 2 skipped / 0 failed；蓝图 #82 已回写。遗留：派生量复权口径重建为二期；git 提交延后待用户拍板。
