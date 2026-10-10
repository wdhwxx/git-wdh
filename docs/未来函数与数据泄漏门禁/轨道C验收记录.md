# 轨道 C（M1-A 数据层收口）验收记录

- 验收日期：2026-10-10
- 验收依据：`docs/未来函数与数据泄漏门禁/未来函数与数据泄漏门禁施工方案.md` v3.0 §6.4.7 验收清单 + §12.4 G-01/G-03
- 提交：**#281（06807f0）** + **#282（96f1e9e）**（工作区干净，蓝图与任务清单已回写）
- 全量回归：**3721 passed, 4 skipped**（tests/unit + tests/leakage_gate + tests/scripts）

## 一、§6.4.7 验收清单逐项核对

| # | 验收标准 | 证据 | 结论 |
|---|---|---|---|
| 1 | ③④ 同一提交修复，两条导出路径 `每股收益` 结果一致 | #281 单次提交同时改 `scripts/数据导出_DB到processed.py:157`（③）与 `myquant/data_access/manager.py:1309`（④），两路径均调 `time_semantics.match_latest_financial` 单一实现；M1A-08 重导实证两路结果一致 | ✅ |
| 2 | 修复效果以"写入结果的 eps 非空率 + 抽样比对 ann_date"验证，不以 diff 验证 | M1A-08：抽样 5 标的（000001/000002/000006/000007/000008.SZ）P1 全 OK（7/1~8/31 取一季报 0.67/-0.5/0.0056/-0.0063/-0.0112，不穿越中报）+ P2 全 OK（9/1 截止日推定生效切中报 1.24/-1.25/-0.0709/-0.008/-0.0197）+ P3 全量非空率 87.64%（11337694/12936745，3197 parquet）；旧 parquet 6-30 当天即见中报 eps 的穿越值全部清除 | ✅ |
| 3 | 导出路径有等价测试（负向口径复现） | `tests/scripts/test_数据导出_pit.py` 8 passed（7/15 只见一季报 eps=0.10、9/1 见中报 0.42、装配断言、无财务记录）；pyproject testpaths 已含 tests/scripts | ✅ |
| 4 | `available_time` 逐列正确，财务列 ≠ trade_date | M1A-10：财务列 available_time = 有效披露日 + 1（`time_semantics.next_day`），全量（导出脚本）与增量（manager）两路径派生一致；PROCESSED_COLUMN_TEMPLATE 15 列对齐防分叉；export_manifest.json sidecar 记录 available_time_rule/schema_revision/stats；测试：test_data_export_columns（15 列断言）+ test_data_clean_incremental（15 列） | ✅ |
| 5 | 复权两项治理：end_date=None+qfq 有告警/断言；adjust_strict 真实配置显式声明 | M1A-05：`provider.py` qfq + end_date=None → 显式 warning；`config/backtest_config.yaml:3274-3275` 与 `config/backtest_kunpeng.yaml:3250-3251` 均 `adjust: hfq` + `adjust_strict: true`；strategy_registry.yaml 全部 hfq | ✅ |
| 6 | 三处 fail-open 均有可观测输出，无静默跳过 | A：engine 哨兵 "" + `_bar_date_failures` ClassVar 计数 + guard 记警告 violation（test_guard_mounting TestFailOpenObservability 6 用例）；B：`_adjust_degraded_symbols` 集合 + `_append_adjust_degradation` 落审计（test_adjust_strict 追加 6 用例）；C：manager `_b_class_derivation_failures` 计数 + 阈值 5 日志升级 error | ✅ |
| 7 | `data_versioner` rev 已联动 | M1A-11：financial_data rev 2→3（注释：rev3 = M1A-01 时点日基收敛）；导出脚本从 `DATASET_SCHEMA_REVISIONS` 单一真源写 manifest；旧 rev 不兼容抛 DataVersionMismatchError，旧 manifest 缺失/异常仅告警不阻断；test_data_versioner rev 断言已同步 | ✅ |

## 二、§12.4 施工检查项（轨道 C 范围内）

| 项 | 要求 | 状态 |
|---|---|---|
| G-01 | 修复 ③④ 后重导 parquet 纠数据本身（"无需重导"≠"不需要正确数据"） | ✅ #282 全量重导 3197/3197（0 失败），穿越值清除 |
| G-03 | severity 翻转须显式验收 + 审计记录；翻转前 R11 保持报红 | ✅ 翻转依据 M1A-08 数据验证；台账 `audit` 字段记录（2026-10-10 施工、依据验证项）；翻转前报红由 #281 阶段测试锚定；test_ledger_truth_source 断言已改为翻转验收锚点 |

## 三、实证发现与修复（M1A-08 过程中）

- **match_latest_financial 同 eff 排序缺陷**：年报与一季报均截止次年 4-30，原排序（仅 eff 为 key）不稳定，7/1 时点取到过期年报（000001 取 2.07 而非一季报 0.67）。修复：排序次键补 `report_date`（同 eff 取最新报告期），新增边界测试 `test_same_eff_takes_newer_report_period`，二次重导后 P1 全 OK。
- **数据环境事实**：quant.db 位于 `database/quant.db`（5589 行情标的 / 5315 财务标的）；financial_data 表 **ann_date 全空**（0 行非空，329735 行 NULL）→ 有效披露日全走 report_deadline 法定截止日推定（保守正确路径，P2 实证 9/1 切中报）。

## 四、蓝图一致性

- 新增文件 `myquant/data_access/time_semantics.py`、`tests/scripts/test_数据导出_pit.py`、`tests/unit/test_time_semantics.py` 均已在 `docs/project_layout_gear2.md` 目录树登记（#281/#282 回写）。
- 全部改动文件（manager/provider/engine/lookahead_guard/financial_service/derived_metrics/data_versioner/导出脚本等 24+6 文件）蓝图注释已回写 #281/#282。
- R-23 入口白名单：无新增入口（临时探查脚本已清理）；R-24 薄化冻结：manager.py 基线 2186→2200 已同步（M1A-10 增量派生所致，属预期增长，注释已说明）。

## 五、结论

**轨道 C 验收通过**。12/12 任务全部完成（M1A-01~12），§6.4.7 验收清单 7/7 达成，G-01/G-03 合规，全量回归 3721 passed 全绿。

**遗留衔接（不阻塞验收）**：
- 导出脚本 `verify_all` 按 DB 全量（5589）校验、导出范围按规则内（3197）——2392 个排除规则标的报"文件不存在"致脚本 exit 1（既有口径，非 M1A 引入）。建议后续（轨道 F 审计收口）将校验范围与导出范围对齐。
- G-02（R11 间接引用）/ G-04（R03 里程碑）/ G-05（措辞统一）属轨道 D/E 范围，尚未涉及。
