# 文件名：myquant/backtest/assembly.py
"""
backtest/assembly.py - 回测引擎装配模块（P0-00 子步骤 A 第一刀）
================================================================

承载回测引擎的跨层装配胶水，自 ``engine.py`` 模块级代码原样迁入
（v2.2 §5.0 子步骤 A 提取清单 (a)1：broker 工厂与解析）。

当前内容：
  - ``_broker_factory`` 模块级全局：进程内撮合执行器工厂注册表
    （G1 EX-09A：BacktestEngine 不顶层 import execution 层 L3→L4 反向，
     撮合执行器 SimBroker 等由合法层 scripts/tests/入口/批量回测装配方注入）。
  - ``register_broker_factory(factory)``：注册/清除全局工厂。

分层：myquant.backtest 登记 MODULE_LAYER L3（test_architecture.py）；
仅依赖 myquant.models.backtest（L2）与 myquant.core.logger（L0），
**不反向 import engine**（避免循环依赖），纯装配胶水。

防未来函数：工厂只消费装配方已传入的 config，不引用任何未来数据。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from myquant.core.logger import get_logger
from myquant.models.backtest import BacktestConfig

logger = get_logger(__name__)

# ── 撮合执行器工厂注册表（G1 EX-09A，自 engine.py 迁入）─────────────────
# BacktestEngine 不再顶层 import execution 层（L3→L4 反向），撮合执行器
# （SimBroker 等）由合法层（scripts/tests/入口/批量回测装配方）经
# register_broker_factory 装配，或构造时传 broker_factory 参数。
# 未装配时 BacktestEngine._resolve_broker 显式报错（不静默降级）。
# 防未来：工厂只消费已传入 config，不引用任何未来数据。
_broker_factory: Optional[Callable[[BacktestConfig], Any]] = None


def get_broker_factory() -> Optional[Callable[[BacktestConfig], Any]]:
    """返回当前注册的全局撮合执行器工厂（实时读模块级全局）。

    engine.py 经此访问，避免 ``from myquant.backtest import assembly`` 产生
    engine→backtest 包级导入边（会闭合 backtest/__init__→batch_backtest→engine 环）。

    Returns:
        当前注册的工厂函数；未注册返回 None。
    """
    return _broker_factory


def register_broker_factory(
    factory: Optional[Callable[[BacktestConfig], Any]],
) -> None:
    """注册全局撮合执行器工厂（装配层注入，进程内生效）。

    Args:
        factory: 工厂函数，签名 factory(config: BacktestConfig) -> broker
            （如 SimBroker 实例）；传 None 清除注册（回退为仅实例级注入）。

    Raises:
        TypeError: factory 非 None 且不可调用时抛出。
    """
    global _broker_factory
    if factory is not None and not callable(factory):
        raise TypeError(f"broker_factory 必须可调用，收到: {type(factory).__name__}")
    _broker_factory = factory
    logger.info(
        "[backtest] 撮合执行器工厂%s", "已装配" if factory is not None else "已清除"
    )
