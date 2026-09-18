# 文件名：myquant/engine/__init__.py
"""
鲲鹏量化交易系统 - 引擎抽象层（Engine Layer）
==============================================
回测与实盘共用的引擎生命周期与事件驱动骨架。

核心组件:
  - BaseEngine : 引擎抽象基类，定义 on_init/on_market_event/on_stop 生命周期钩子

设计原则:
  - 回测引擎(backtest/engine.py)与实盘引擎(live/engine.py)共同继承 BaseEngine
  - 驱动事件统一为 MarketEvent（bar/tick/trade），支持多粒度数据驱动
  - 策略层只依赖 BaseEngine 接口，切换回测/实盘零修改
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from myquant.engine.base import BaseEngine

__all__ = [
    'BaseEngine',
]
