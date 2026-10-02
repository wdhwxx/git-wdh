# 交接摘要 · processed 清理（创业板/科创板/北交所 6 列直写残留） · 2026-10-02

## 背景
阶段 A 审计确认：`scripts/数据下载.py` CLI 链路无标的池过滤 + processed 原始直写（6 列英文），导致创业板/科创板/北交所等池外标的以半成品形式写入 processed（2,376 个 6 列 parquet，其中 2,025 只创业板仅 2 行 2026-09-29/09-30）。全量清洗口径仅含沪深主板 3,192 只（14 列中文+派生量），池外市场"暂时不做、保持排除"（用户拍板）。

## 处置（C1 方案 1：删除，备份可回滚）
- 执行时间：2026-10-02 晚（阶段 B 前导数据操作）
- 动作：**移动**（非删除）2,376 个 6 列直写 parquet 至 `backups\processed_6col_20261002\`（移动=备份+移除，一步可回滚）
- 结果验证（回读扫描）：
  - 移入备份：2376 / 2376 ✅
  - processed 剩余：**3,195 个**，全部清洗口径（14 列等），无 6 列残留 ✅
  - 样本：000001.SZ.parquet 8,323 行 ✅
- 回滚方式：`Move-Item backups\processed_6col_20261002\*.parquet datasets\processed\` 即可还原

## 现状：processed 与全量清洗口径一致
- processed 仅含主板清洗池标的（14 列中文扁平，日期整数 YYYYMMDD，含派生量）
- 创业板/科创板/北交所数据仍在 DB raw 层（数据权威），processed 不包含（与 D1 决策一致）

## ✅ 再污染风险已根治（A1 已完成，2026-10-02）
- **已执行**：`scripts/数据下载.py download_daily` 改造——候选池经 `resolve_incremental_symbols`（14 条规则）过滤，只下载主板清洗池内标的；processed 写回改 `manager.save_cleaned_incremental_parquet`（与 GUI 增量更新同一清洗链路，复用已拉 records 无二次拉取）；`save_market_data_parquet` 原始直写已废弃标注。
- **验证**：py_compile 通过；新增单测 3 项 + 回归 = 14 passed；全量 pytest **1755 passed / 2 skipped**（含新增 3 项，零破坏）；蓝图 #86 已回写；git 已提交。
- **效果**：下次 `python.exe scripts\数据下载.py --incremental` 只补主板清洗池（3,192 只），创业板/科创板/北交所不会再进入 processed。

## ✅ 留后三项已全部落地（A2 + B1 + P3-2，蓝图 #87，2026-10-02）
- **A2 排除规则内置兜底补齐 14 条**：`full_clean_service.DEFAULT_EXCLUDE_RULES` 由 10 条补 `68/90/20/99` 为 14 条（复核确认 `financial_sync_service.FINANCIAL_EXCLUDE_RULES` 原已 14 条）；两处内置兜底与 `config/data_clean_run.json` + `financial_exclude_rules.yaml` 完全一致——config 缺失/损坏回退时**不再把 B 股 90·20 / 科创板 68 / 老三板 99 段纳入清洗池**（B 股永久剔除口径防复活）；financial_sync 过时注释同步；`test_resolve_incremental_symbols` 兜底回退断言更新为 14 条口径。
- **B1 schema.py 清理**：`drop_all_tables` 顺序中 share_change 重复两行 → 单行（DROP TABLE IF EXISTS 幂等，行为不变）。
- **P3-2 `数据下载.py` sys.path 注入**：顶部注入项目根（与 `同步股本变动.py` 对齐）——**以后 CLI 直跑免设 PYTHONPATH**：`D:\ProgramData\anaconda3\envs\conda_wdh\python.exe scripts\数据下载.py --incremental`（冒烟验证 INJECT_OK）。
- **验证**：py_compile 5 文件通过；相关单测（resolve/p2_anti_future/database/cli_incremental）**87 passed / 1 skipped**；全量 pytest **1755 passed / 2 skipped**（与 #86 基线一致，零破坏）；git 分三笔提交（6289969 A2 / 9113de4 B1 / 92c65e3 P3-2+蓝图#87）。

## 与三期各文档关系
- 审计依据：`docs\数据层全链路审计与修复方案_20261002.md`（A1/A2/B1/C1 问题清单，A1/A2/B1/C1/C2 均已完成）
- 用户口径（2026-10-02）：只做主板；创业板/科创板/北交所保持排除、暂不放开（后续 GUI 放开时再补派生量缺口）
- 未决待办：GUI `run_daily` 写回失败"异常/未产出"双计（与 A1 同款，已披露待统一）；北交所 float_share 股本源扩展（远期）；`财务CSV更新.py` 等其余脚本是否补 sys.path 注入（未点名）
