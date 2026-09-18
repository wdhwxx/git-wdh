# 文件名：myquant/engine/base.py
"""
engine/base.py - 引擎抽象基类
==============================
回测引擎与实盘引擎的统一抽象基类，定义生命周期钩子与事件驱动接口。

设计原则（规则2：为实盘无缝对接准备）:
  - BaseEngine 定义统一生命周期：on_init → on_market_event(循环) → on_stop
  - 回测引擎(backtest/engine.py)与实盘引擎(live/engine.py)共同继承此基类
  - 驱动事件统一为 MarketEvent（bar/tick/trade），支持多粒度数据驱动
  - 策略层只依赖 BaseEngine 接口，切换回测/实盘零修改

防未来函数（规则4）:
  - on_market_event 接收的 MarketEvent 视为"已发生"的市场数据
  - 引擎不得在事件处理中访问未来数据
  - 回测引擎逐事件推进，实盘引擎由真实行情驱动
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.models.market_data import MarketEvent

logger = get_logger(__name__)


class BaseEngine(ABC):
    """
    引擎抽象基类（Abstract Base Class）

    回测引擎与实盘引擎的共同父类，定义统一的生命周期与事件驱动接口。

    生命周期:
      1. on_init()      — 初始化（加载数据/连接行情/初始化组合与执行器）
      2. on_market_event(event) — 市场事件处理循环（bar/tick/trade驱动）
      3. on_stop()      — 停止（取消挂单/断开连接/生成结果）

    子类必须实现:
      - on_init()
      - on_market_event(event)
      - on_stop()
      - run()  — 主循环入口
    """

    def __init__(self, name: str = "base_engine"):
        """
        初始化引擎基类

        参数:
            name: 引擎名称（用于日志标识）
        """
        self.name = name
        self._initialized = False
        self._running = False
        self._stopped = False
        self._event_count = 0
        logger.info(f"引擎基类初始化: {name}")

    # ------------------------------------------------------------------
    # 生命周期钩子（子类必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    def on_init(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化钩子

        子类在此实现:
          - 加载配置/数据
          - 初始化投资组合
          - 初始化执行器（模拟券商/真实券商）
          - 初始化策略
          - 连接行情源（实盘）

        参数:
            config: 引擎配置字典（回测/实盘共用，字段可扩展）
        """
        pass

    @abstractmethod
    def on_market_event(self, event: MarketEvent) -> None:
        """
        市场事件处理钩子（核心循环）

        每收到一个市场事件（bar/tick/trade）调用一次。
        子类在此实现:
          - 更新组合市场价
          - 撮合待成交订单（回测）/ 接收成交回报（实盘）
          - 调用策略生成信号
          - 提交订单到执行器
          - 记录净值/日志

        参数:
            event: 市场事件（MarketEvent，包含 bar/tick/snapshot 数据）

        防未来函数:
          - event 中的数据视为"已发生"，不得访问未来数据
          - 回测引擎逐事件推进，事件顺序即时间顺序
        """
        pass

    @abstractmethod
    def on_stop(self) -> None:
        """
        停止钩子

        子类在此实现:
          - 取消所有活跃订单
          - 断开行情/券商连接（实盘）
          - 生成回测/实盘结果
          - 清理资源
        """
        pass

    # ------------------------------------------------------------------
    # 主循环入口（子类必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    def run(self) -> Any:
        """
        运行引擎主循环

        回测引擎: 逐bar/逐tick遍历历史数据，调用 on_market_event
        实盘引擎: 订阅实时行情，回调驱动 on_market_event，直到收到停止信号

        返回:
            回测结果(BacktestResult) 或 实盘运行状态
        """
        pass

    # ------------------------------------------------------------------
    # 通用方法
    # ------------------------------------------------------------------

    def start(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        启动引擎（调用 on_init）

        参数:
            config: 引擎配置
        """
        if self._initialized:
            logger.warning(f"引擎 {self.name} 已初始化，跳过重复初始化")
            return
        self.on_init(config)
        self._initialized = True
        logger.info(f"引擎 {self.name} 初始化完成")

    def stop(self) -> None:
        """停止引擎（调用 on_stop）"""
        if self._stopped:
            return
        self._running = False
        self._stopped = True
        self.on_stop()
        logger.info(f"引擎 {self.name} 已停止")

    def dispatch_event(self, event: MarketEvent) -> None:
        """
        分发市场事件（调用 on_market_event）

        参数:
            event: 市场事件
        """
        if not self._initialized:
            raise RuntimeError(f"引擎 {self.name} 未初始化，请先调用 start()")
        if self._stopped:
            logger.warning(f"引擎 {self.name} 已停止，忽略事件")
            return
        self._event_count += 1
        self.on_market_event(event)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def is_initialized(self) -> bool:
        """是否已初始化"""
        return self._initialized

    @property
    def is_running(self) -> bool:
        """是否运行中"""
        return self._running

    @property
    def is_stopped(self) -> bool:
        """是否已停止"""
        return self._stopped

    @property
    def event_count(self) -> int:
        """已处理事件数"""
        return self._event_count

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"name={self.name}, "
            f"initialized={self._initialized}, "
            f"running={self._running}, "
            f"events={self._event_count})"
        )
