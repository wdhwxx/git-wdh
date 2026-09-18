# 文件名：myquant/api/dependencies/get_services.py
"""
get_services.py - API服务依赖注入
==================================
FastAPI依赖注入，获取各核心服务实例。

v1实现范围（基础骨架）:
  - ServiceContainer 服务容器（单例模式，统一管理所有服务实例）
  - get_service_container() 获取服务容器（FastAPI依赖）
  - get_strategy_manager() 获取策略管理器
  - get_backtest_engine() 获取回测引擎
  - get_live_engine() 获取实盘引擎
  - get_risk_manager() 获取风控管理器
  - get_data_manager() 获取数据管理器
  - get_allocator() 获取资金分配器

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（所有核心服务注入接口预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 依赖注入只提供当前服务实例，不预测未来

防未来函数说明:
  - 依赖注入只提供当前已初始化的服务实例
  - 不创建任何未来数据或预测
  - 服务状态基于当前已知信息，不包含未来数据

TODO(后续迭代):
  - 数据库会话依赖注入
  - 缓存服务依赖注入
  - 消息队列依赖注入
  - 配置服务依赖注入
  - 日志服务依赖注入
  - 多环境服务切换（开发/测试/生产）
  - 服务健康检查依赖

依赖: typing, myquant.core.logger
"""

from __future__ import annotations

from typing import Any, Optional

from myquant.core.logger import get_logger

logger = get_logger("api.dependencies.get_services")


# ═══════════════════════════════════════════════
#  服务容器（单例）
# ═══════════════════════════════════════════════

class ServiceContainer:
    """
    服务容器（单例模式）

    统一管理所有核心服务实例，API通过依赖注入获取服务。

    使用方式:
        from myquant.api.dependencies.get_services import ServiceContainer

        container = ServiceContainer()
        container.register("strategy_manager", strategy_mgr)
        strategy_mgr = container.get("strategy_manager")

    与FastAPI集成（后续迭代）:
        from fastapi import Depends

        async def get_strategy_mgr(container: ServiceContainer = Depends(get_service_container)):
            return container.get_strategy_manager()
    """

    _instance: Optional["ServiceContainer"] = None

    def __new__(cls) -> "ServiceContainer":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._services: dict[str, Any] = {}
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """初始化（只执行一次）"""
        if not self._initialized:
            self._services = {}
            self._initialized = True
            logger.info("[deps] 服务容器初始化（单例）")

    def register(self, name: str, service: Any) -> None:
        """
        注册服务

        Parameters
        ----------
        name : str
            服务名称
        service : Any
            服务实例
        """
        self._services[name] = service
        logger.info(f"[deps] 注册服务: {name} = {type(service).__name__}")

    def get(self, name: str) -> Optional[Any]:
        """
        获取服务

        Parameters
        ----------
        name : str
            服务名称

        Returns
        -------
        Any | None : 服务实例（未注册返回None）
        """
        service = self._services.get(name)
        if service is None:
            logger.warning(f"[deps] 服务未注册: {name}")
        return service

    def has(self, name: str) -> bool:
        """检查服务是否已注册"""
        return name in self._services

    def unregister(self, name: str) -> None:
        """注销服务"""
        if name in self._services:
            del self._services[name]
            logger.info(f"[deps] 注销服务: {name}")

    @property
    def service_names(self) -> list[str]:
        """已注册的服务名称列表"""
        return list(self._services.keys())

    # ═══════════════════════════════════════════
    #  常用服务快捷访问
    # ═══════════════════════════════════════════

    def get_strategy_manager(self) -> Optional[Any]:
        """获取策略管理器"""
        return self.get("strategy_manager")

    def get_backtest_engine(self) -> Optional[Any]:
        """获取回测引擎"""
        return self.get("backtest_engine")

    def get_live_engine(self) -> Optional[Any]:
        """获取实盘引擎"""
        return self.get("live_engine")

    def get_risk_manager(self) -> Optional[Any]:
        """获取风控管理器"""
        return self.get("risk_manager")

    def get_data_manager(self) -> Optional[Any]:
        """获取数据管理器"""
        return self.get("data_manager")

    def get_allocator(self) -> Optional[Any]:
        """获取资金分配器"""
        return self.get("allocator")

    def get_auth(self) -> Optional[Any]:
        """获取认证服务"""
        return self.get("auth")

    def get_rate_limiter(self) -> Optional[Any]:
        """获取限流器"""
        return self.get("rate_limiter")

    # ═══════════════════════════════════════════
    #  监控类服务快捷访问（M4修复：routes.py 依赖注入接线）
    # ═══════════════════════════════════════════

    def get_data_quality_explorer(self) -> Optional[Any]:
        """获取数据质量探查器（monitoring底层，#S2修复后位置）"""
        return self.get("data_quality_explorer")

    def get_metrics_collector_service(self) -> Optional[Any]:
        """获取指标采集器"""
        return self.get("metrics_collector")

    def get_alert_manager_service(self) -> Optional[Any]:
        """获取告警管理器"""
        return self.get("alert_manager")


# ═══════════════════════════════════════════════
#  FastAPI依赖注入函数（后续迭代集成FastAPI时使用）
# ═══════════════════════════════════════════════

def get_service_container() -> ServiceContainer:
    """
    获取服务容器（FastAPI依赖注入）

    Returns
    -------
    ServiceContainer : 服务容器单例
    """
    return ServiceContainer()


def get_strategy_manager(container: ServiceContainer = None) -> Optional[Any]:
    """获取策略管理器（FastAPI依赖注入）"""
    if container is None:
        container = get_service_container()
    return container.get_strategy_manager()


def get_backtest_engine(container: ServiceContainer = None) -> Optional[Any]:
    """获取回测引擎（FastAPI依赖注入）"""
    if container is None:
        container = get_service_container()
    return container.get_backtest_engine()


def get_live_engine(container: ServiceContainer = None) -> Optional[Any]:
    """获取实盘引擎（FastAPI依赖注入）"""
    if container is None:
        container = get_service_container()
    return container.get_live_engine()


def get_risk_manager(container: ServiceContainer = None) -> Optional[Any]:
    """获取风控管理器（FastAPI依赖注入）"""
    if container is None:
        container = get_service_container()
    return container.get_risk_manager()


def get_data_manager(container: ServiceContainer = None) -> Optional[Any]:
    """获取数据管理器（FastAPI依赖注入）"""
    if container is None:
        container = get_service_container()
    return container.get_data_manager()


def get_allocator(container: ServiceContainer = None) -> Optional[Any]:
    """获取资金分配器（FastAPI依赖注入）"""
    if container is None:
        container = get_service_container()
    return container.get_allocator()


# ═══════════════════════════════════════════════
#  监控类服务依赖注入（M4修复：routes.py 实际接线）
# ═══════════════════════════════════════════════

def get_data_quality_explorer(container: ServiceContainer = None) -> Optional[Any]:
    """
    获取数据质量探查器（FastAPI依赖注入）

    查找顺序:
      1. 服务容器已注册的 "data_quality_explorer"（create_app 外部注入）
      2. 未注册时自动构建（monitoring 底层 DataQualityExplorer，#S2修复后位置）

    Returns
    -------
    Any : DataQualityExplorer 实例
    """
    if container is None:
        container = get_service_container()
    explorer = container.get_data_quality_explorer()
    if explorer is None:
        from myquant.monitoring.data_quality_explorer import DataQualityExplorer
        explorer = DataQualityExplorer()
        container.register("data_quality_explorer", explorer)
    return explorer


def get_metrics_collector(container: ServiceContainer = None) -> Optional[Any]:
    """
    获取指标采集器（FastAPI依赖注入）

    查找顺序: 服务容器注册实例 → monitoring 全局单例（与引擎联动同一实例）。

    Returns
    -------
    Any : MetricsCollector 实例
    """
    if container is None:
        container = get_service_container()
    collector = container.get_metrics_collector_service()
    if collector is None:
        from myquant.monitoring import get_metrics_collector as _get_global
        collector = _get_global()
        container.register("metrics_collector", collector)
    return collector


def get_alert_manager(container: ServiceContainer = None) -> Optional[Any]:
    """
    获取告警管理器（FastAPI依赖注入）

    查找顺序: 服务容器注册实例 → monitoring 全局单例（默认控制台+日志双通道）。

    Returns
    -------
    Any : AlertManager 实例
    """
    if container is None:
        container = get_service_container()
    alert_mgr = container.get_alert_manager_service()
    if alert_mgr is None:
        from myquant.monitoring import get_alert_manager as _get_global
        alert_mgr = _get_global()
        container.register("alert_manager", alert_mgr)
    return alert_mgr
