# 文件名：myquant/acceptance/__init__.py
"""
acceptance - 验收与演练模块
===========================
提供系统验收（保命闭环/保钱包/保 Alpha/自治）与故障演练（混沌演练/灾备演练）能力，
按治理方案 §5.4 结构归位 A 从 monitoring/ 拆出（回写 #262）：
  - m1_acceptance   : A-15 M1 保命验收（四环覆盖率 + FI-01~05 故障注入 + MTTx 计时 + TP/FP/FN 分类）
  - b_acceptance    : B-10 B 包保钱包验收（B-1~B-9 九项检查，阈值引用监控配置真源）
  - c_acceptance    : C-12 C 包保 Alpha 验收（C-1~C-11 验收项，含归因对比/动态基线/信号一致性）
  - d_acceptance    : D-9 D 包自治验收（D-1~D-8，集成混沌演练/灾备演练/配置版本化）
  - chaos_drill     : 混沌演练（FI-01~07 故障注入，隔离环境）
  - disaster_drill  : 灾备演练（备份恢复/切换演练四项）
"""
