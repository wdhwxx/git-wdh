# 审计工作底稿暂存目录 `_audit_20261004/`

本目录是《鲲鹏量化交易系统审计报告》的中间产物暂存区，**不属于项目交付物**。
完成交付后可由用户决定是否删除；删除本目录不影响 `myquant/`、`config/`、`tests/` 等任何项目源码。

## 目录内容

| 文件 | 说明 |
|---|---|
| `report_src.md` | 审计报告正文源文件（Markdown，最终交付件的单一真相源） |
| `phase1_启动与资料清单.md` | 阶段一产出物：《审计启动与资料清单》＋≤10 个澄清问题＋仓库画像 |
| `findings_batch1.md` | 详细发现底稿（KP-P1-01 ~ KP-P1-08，架构/代码/CI/安全基线） |
| `findings_batch2.md` | 详细发现底稿（KP-P2-01 ~ KP-P2-09，策略/数据/回测） |
| `findings_batch2_supplement.md` | 追加底稿（KP-P2-10 ~ KP-P2-11，两项 P0） |
| `findings_batch3.md` | 详细发现底稿（KP-P3-01 ~ KP-P3-10，实盘/执行/风控） |
| `findings_batch4.md` | 详细发现底稿（KP-P4-01 ~ KP-P4-09，资金/权限/安全） |
| `findings_batch5.md` | 详细发现底稿（KP-P5-01 ~ KP-P5-11，运维/灾备/监控） |
| `findings_batch6.md` | 详细发现底稿（KP-P6-01 ~ KP-P6-07，合规/治理/模型） |
| `build_docx.py` | Markdown → DOCX 渲染脚本 |
| `make_deliverables.py` | 合并正文与附录、生成最终 DOCX |
| `build/` | 构建产物（最终交付件的定稿位置） |

## 交付物去向

最终交付文件写入 `I:\思维导图\`，**不回写项目结构**。
