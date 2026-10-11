# 文件名：myquant/realism_gate/__init__.py
"""
realism_gate - 回测真实性门禁（L0 治理层，与 leakage_gate 同构同层）
=====================================================================

把"回测真实性"从引擎参数提升为门禁对象，产出 RealismManifest 并 fail-closed。
（v2.2 §4.1 架构位置 + §4.2 数据契约 + §4.3 出口断言语义）

组件（P0-01 逐步落地）：
  - manifest.py   RealismManifest 数据契约（字段、取值域、完整性校验、签名、序列化）
  - gate.py       门禁中心：配置、清单聚合、出口断言、审计落链、阻断产物落盘（后续刀）
  - invariants.py  结果侧真实性不变量 RI-1~RI-6（P1-15，后续刀）
  - checks/       各真实性维度检查器（market_rules/cost_model/data_pit/sample_pool，后续刀）
  - report.py     RealismManifest 落盘（results.json + 阻断产物 + 报告页 + audit_log，后续刀）

设计红线（v2.2 §4.3 第三处不同）：
  - 不使用 severity，只保留 critical 布尔（唯一阻断判据，无歧义）
  - 配置缺失 = 阻断（与 leakage_gate 段缺失落默认 True 不同）
  - 必须落盘（清单强制写入 results.json 与报告页）

分层：myquant.realism_gate 登记 MODULE_LAYER L0（test_architecture.py）；
仅依赖 myquant.core（L0 合法），禁止 import myquant.backtest / myquant.execution /
myquant.data_access（R-02/R-16 跨层红线）。运行时事实由 L3/L4 以数据契约注入。
"""

from myquant.realism_gate.manifest import (
    CRITICAL_FIELDS,
    ENUM_DOMAINS,
    WARNING_FIELDS,
    ManifestIncompleteError,
    RealismGateError,
    RealismManifest,
    RealismViolation,
)

__all__ = [
    "RealismManifest",
    "RealismViolation",
    "RealismGateError",
    "ManifestIncompleteError",
    "CRITICAL_FIELDS",
    "WARNING_FIELDS",
    "ENUM_DOMAINS",
]
