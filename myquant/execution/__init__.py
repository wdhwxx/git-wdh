# 文件名：myquant/execution/__init__.py
"""
鲲鹏量化交易系统 - 统一执行抽象层（Execution Layer）
====================================================
回测模拟撮合与实盘券商交易的统一执行接口。

核心组件:
  - ExecutionInterface : 执行接口抽象基类（下单/撤单/查单/资金查询）
  - SimBroker          : 回测模拟券商（滑点/佣金/印花税/部分成交）
  - OrderStateMachine  : 订单生命周期状态机（待报→已报→部成→已成→已撤→废单）

设计原则:
  - 回测模拟券商与实盘券商适配器共同实现 ExecutionInterface
  - 引擎层只依赖 ExecutionInterface，切换回测/实盘零修改
  - 订单状态由 OrderStateMachine 统一管理，确保状态转换合法
  - A股写实交易成本：佣金万2(最低5元)、印花税万5(仅卖出，2023-08-28起减半)、过户费万0.1
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from myquant.execution.base import ExecutionInterface
from myquant.execution.sim_broker import SimBroker
from myquant.execution.state_machine import OrderStateMachine

__all__ = [
    # 执行接口
    'ExecutionInterface',
    # 模拟券商（回测）
    'SimBroker',
    # 订单状态机
    'OrderStateMachine',
]
