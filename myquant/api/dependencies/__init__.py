# 文件名：myquant/api/dependencies/__init__.py
"""
dependencies - API依赖注入
===========================
FastAPI依赖注入，获取各核心服务实例。

v1实现:
  - get_services.py: 服务容器（单例）+ 各服务依赖注入函数

TODO(后续迭代):
  - 数据库会话依赖
  - 缓存服务依赖
  - 消息队列依赖
  - 多环境服务切换
"""

from myquant.api.dependencies.get_services import (
    ServiceContainer,
    get_allocator,
    get_backtest_engine,
    get_data_manager,
    get_live_engine,
    get_risk_manager,
    get_service_container,
    get_strategy_manager,
)

__all__ = [
    "ServiceContainer",
    "get_service_container",
    "get_strategy_manager",
    "get_backtest_engine",
    "get_live_engine",
    "get_risk_manager",
    "get_data_manager",
    "get_allocator",
]
