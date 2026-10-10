# 文件名：myquant/leakage_gate/__init__.py
"""
leakage_gate - 防泄漏门禁中心（L4 治理层落点，模块分层 L0）
=============================================================
统一防未来函数 / 数据泄漏门禁的入口、违规记录与出口断言。

组件（v3.0 §5.4/§6.2）：
  - gate.py            门禁中心：GateConfig 开关 / Violation / LeakageGateError /
                       register_gate_check 注册 / assert_all_gates_pass 出口断言
  - contract.py        契约完整性校验（M0-06）
  - consumption.py     消费侧列门禁 L0.5（M0-07）
  - static_scan.py     静态扫描（阶段 1-B）
  - cross_sectional.py 横截面 as-of 校验（阶段 1-B）
  - sanity.py          shuffle-label 探测（阶段 2）
  - report.py          泄漏审计报告产物（阶段 3）
  - exemption.py       豁免流实现（阶段 3）

出口强制点（唯一）：run_回测入口.py create_engine / run_实盘入口.py create_live_engine
调用 assert_all_gates_pass（v3.0 §5.4，不在注册口校验）。

防未来函数：本包只记录已发生的校验结果，不预测未来。
"""

from myquant.leakage_gate.gate import (
    GateConfig,
    LeakageGateError,
    Violation,
    assert_all_gates_pass,
    get_config,
    register_gate_check,
    reset_gate_checks,
    set_config,
)

__all__ = [
    "GateConfig",
    "Violation",
    "LeakageGateError",
    "register_gate_check",
    "reset_gate_checks",
    "get_config",
    "set_config",
    "assert_all_gates_pass",
]
