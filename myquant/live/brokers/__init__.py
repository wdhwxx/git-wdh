# 文件名：myquant/live/brokers/__init__.py
"""
brokers - 券商适配层
=====================
实盘券商适配器，只负责对接券商原始接口，不做清洗和策略逻辑。

适配器列表:
  - base_broker.py     券商抽象基类（继承ExecutionInterface，增加连接/订阅/回调）
  - paper_broker.py    本地模拟券商（Paper Trading，无券商环境测试用）
  - qmt_broker.py      迅投QMT适配器（SIM模拟撮合 + LIVE SDK占位，第1项实盘对接）
  - ptrade_broker.py   恒生Ptrade适配器（SIM模拟撮合 + LIVE API占位，第1项实盘对接）
  - manager.py         多券商管理器（双券商并行：路由/主备切换/合并视图/健康监控）
  - xtp_broker.py      中泰XTP适配器（TODO: 后续迭代）

设计原则:
  - 只做接口对接，不做业务清洗
  - 继承BaseBroker，自动获得连接管理/行情订阅/回调注册能力
  - 引擎层只依赖ExecutionInterface，切换券商零修改
  - 多券商并行：BrokerManager 统一编排（主备Failover/多账户/仿真+实盘）
  - 为实时行情和实盘无缝对接预留完整接口
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from .base_broker import BaseBroker, BrokerType
from .manager import (
    BrokerManager,
    BrokerManagerError,
    BrokerNotFoundError,
    BrokerRegistration,
    NoBrokerAvailableError,
)
from .paper_broker import PaperBroker
from .ptrade_broker import PtradeBroker
from .qmt_broker import QmtBroker

__all__ = [
    "BaseBroker",
    "BrokerType",
    "PaperBroker",
    "QmtBroker",
    "PtradeBroker",
    "BrokerManager",
    "BrokerRegistration",
    "BrokerManagerError",
    "BrokerNotFoundError",
    "NoBrokerAvailableError",
]
