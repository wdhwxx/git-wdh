"""P1-04 引擎直构例外清单（REALISM_EXEMPT_ENTRY 固化登记）。

方案 §4.4 要求：所有回测/实盘入口收敛到唯一出口工厂（回测
``BacktestScheduler.create_engine`` / 实盘 ``create_live_engine``），凡不收敛的
直构点必须显式登记于此清单并附豁免理由，由 AST 扫描测试强制核对——新增
未登记直构点即判红（防"门禁绕过"通道静默敞口）。

本模块为 L0 纯数据注册表：不 import 引擎/策略，只描述"哪些脚本、用什么符号、
为何豁免、由哪道门禁兜底"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ExemptEntry:
    """一条引擎直构豁免登记。

    Attributes:
        script: 脚本相对路径（相对项目根，如 ``scripts/双券商模拟实盘.py``）。
        symbol: 直构符号（正则子串，如 ``LiveEngine(`` / ``BacktestEngine.from_data_provider``）。
        reason: 豁免理由（为何不收敛到出口工厂）。
        covered_by: 兜底门禁（该脚本实际受哪道出口/安全门禁约束）。
    """

    script: str
    symbol: str
    reason: str
    covered_by: str


#: 已登记的引擎直构例外清单（frozen tuple，运行时不可变）。
#:
#: 3 个实盘演示/沙箱脚本直构 ``LiveEngine``：它们不走生产 ``live_config.yaml``，
#: 而是脚本内手动装配 MarketDataFeed / BrokerManager / LivePortfolio / 策略后直构
#: 引擎，属"沙箱演示"语义（paper/回放），不进入生产实盘报备链路。生产实盘唯一
#: 出口仍是 ``myquant.live.engine_assembly.create_live_engine``（回写#422 装配层迁出直连；含 assert_all_gates_pass
#: 出口强制 + 运行模式一致性 + LIVE 报备合规门禁）。
#:
#: 历史直构点 ``scripts/生成回测报告.py BacktestEngine.from_data_provider`` 已于
#: P1-04 收敛到 ``BacktestScheduler.create_engine``（run_backtest），不在此清单。
REALISM_EXEMPT_ENTRIES: Tuple[ExemptEntry, ...] = (
    ExemptEntry(
        script="scripts/双券商模拟实盘.py",
        symbol="LiveEngine(",
        reason="双券商并行沙箱演示：脚本内手动装配 BrokerManager/LivePortfolio/策略，"
               "不走生产 live_config.yaml；直构 LiveEngine 仅用于 paper 回放。",
        covered_by="live_safety_gate（生产唯一出口 create_live_engine 含"
                   "assert_all_gates_pass + 运行模式一致性 + LIVE 报备合规门禁）",
    ),
    ExemptEntry(
        script="scripts/实时行情驱动.py",
        symbol="LiveEngine(",
        reason="实时行情轮询驱动演示：脚本内建 feed/broker/portfolio 后直构，"
               "属沙箱回放语义，不进入生产实盘链路。",
        covered_by="live_safety_gate（生产唯一出口 create_live_engine 含出口强制门禁）",
    ),
    ExemptEntry(
        script="scripts/实盘模拟回测.py",
        symbol="LiveEngine(",
        reason="实盘模拟回测沙箱：脚本内手动装配组件后直构 LiveEngine，"
               "用于策略在实盘同源接口下的回放验证，非生产实盘入口。",
        covered_by="live_safety_gate（生产唯一出口 create_live_engine 含出口强制门禁）",
    ),
)
