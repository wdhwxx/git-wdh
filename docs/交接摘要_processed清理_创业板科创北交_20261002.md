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

## 与三期各文档关系
- 审计依据：`docs\数据层全链路审计与修复方案_20261002.md`（A1/A2/B1/C1 问题清单）
- 用户口径（2026-10-02）：只做主板；创业板/科创板/北交所保持排除、暂不放开；批次 1 其他项（A2 兜底 14 条 / B1 schema 清理 / P3-2 sys.path）留后处理
