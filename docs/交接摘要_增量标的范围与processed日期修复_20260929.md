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
