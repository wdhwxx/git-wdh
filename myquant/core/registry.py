# 文件名：myquant/core/registry.py
"""
registry.py - 组件注册表（L0，纯状态，无 myquant 业务依赖）
====================================================================
供 monitoring 各模块经低层注册表获取装配方注册的全局实例，避免同层
模块互相延迟导入造成真环（G2-1 打断 monitoring 内部环）。

- metrics_collector.get_metrics_collector() 懒创建单例后自注册；
- 使用方（如 alerting._resolve_collector）经 get_collector() 获取；
- 未注册返回 None，使用方降级为不写指标（仅记 warning），不阻断业务。
"""

from __future__ import annotations

from typing import Any, Optional

#: 全局指标采集器（由 metrics_collector 懒创建时自注册；装配方保证先于使用方调用）
_collector: Optional[Any] = None

#: 通用组件工厂注册表（name -> 可调用工厂；monitoring 等模块导入时自注册，装配方经 get_component 取用）
_component_factories: dict[str, Any] = {}


def register_component(name: str, factory: Any) -> None:
    """注册通用组件工厂。

    Parameters
    ----------
    name : str
        组件注册名（如 "alert_manager"、"account_reconciler"、"metrics_collector"）。
    factory : Any
        可调用工厂，调用后返回组件实例（monitoring 模块导入时注册 get_xxx 单例函数）。
    """
    _component_factories[name] = factory


def get_component(name: str) -> Optional[Any]:
    """获取组件工厂；未注册返回 None（使用方降级为跳过装配，不阻断业务）。"""
    return _component_factories.get(name)


def register_collector(collector: Any) -> None:
    """注册全局指标采集器实例。

    Parameters
    ----------
    collector : Any
        MetricsCollector 实例（metrics_collector 懒创建单例时自注册）。
    """
    global _collector
    _collector = collector


def get_collector() -> Optional[Any]:
    """获取全局指标采集器；未注册返回 None（使用方降级为不写指标）。"""
    return _collector
