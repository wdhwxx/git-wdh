# 文件名：myquant/realism_gate/checks/__init__.py
"""
realism_gate/checks/ - 真实性门禁检查器子模块（P0-01 第三刀）
================================================================

各维度检查器：纯函数，接收已 build 的 RealismManifest，返回 List[RealismViolation]。
导入即注册（register_realism_check），本包 __init__ 导入即触发注册。

子模块：
  - market_rules.py  涨跌停/停牌/T+1/整手 配置真实性（v2.2 §4.2 行情规则分组）
  - cost_model.py    滑点/四费/费用基数口径（v2.2 §4.2 成本分组）
  - data_pit.py      复权降级/快照指纹/数据质量（v2.2 §4.2 数据分组）
  - sample_pool.py   样本池口径/幸存者偏差（v2.2 §4.2 样本池分组）
  - causality.py     因果切片真实性（P0-08 子项⑤：context_sliced=false 且未声明全量 → critical）

分层：L0；仅消费 manifest 注入的字段值，不 import L3/L4（R-02/R-16）。
"""

from myquant.realism_gate.checks import causality as _causality  # noqa: F401
from myquant.realism_gate.checks import cost_model as _cost_model  # noqa: F401
from myquant.realism_gate.checks import data_pit as _data_pit  # noqa: F401
from myquant.realism_gate.checks import market_rules as _market_rules  # noqa: F401
from myquant.realism_gate.checks import sample_pool as _sample_pool  # noqa: F401
