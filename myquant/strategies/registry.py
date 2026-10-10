# 文件名：myquant/strategies/registry.py
"""
strategies/registry.py - 策略注册器
====================================
策略类的注册、发现、实例化工厂。

设计原则（规则2：为实盘无缝对接准备）:
  - 装饰器注册：@register_strategy 装饰策略类
  - 动态发现：扫描指定模块，自动发现策略类
  - 外部插件：支持 pyproject.toml entry_points 注册的外部策略包
  - 工厂方法：create_strategy(name, **params) 按名称创建策略实例
  - 回测/实盘共用同一注册器，策略可在两种模式间无缝切换

使用方式:
    @register_strategy
    class RSRSStrategy(StrategyBase):
        name = "rsrs"
        ...

    strategy = create_strategy("rsrs", period=18, norm_window=60)
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

import importlib
import pkgutil
from typing import Any, Dict, List, Optional, Tuple, Type

from myquant.core.logger import get_logger
from myquant.strategies.base import StrategyBase

logger = get_logger(__name__)


# ═══════════════════════════════════════════════
#  全局策略注册表
# ═══════════════════════════════════════════════

_strategy_registry: Dict[str, Type[StrategyBase]] = {}


# ═══════════════════════════════════════════════
#  防泄漏门禁契约强制（M0-04，v3.0 §5.2/§6.2-8）
#  ═══════════════════════════════════════════════

# 契约必填字段（策略类必须显式声明的契约元数据，主键 = name，DEC-01）
_CONTRACT_REQUIRED_FIELDS: Tuple[str, ...] = (
    "decision_time",
    "execution_time",
    "holding_period",
    "universe",
    "data_availability",
    "consumed_columns",
    "ruleset",
)


def _validate_contract_metadata(cls: Type[StrategyBase]) -> None:
    """
    校验策略类契约元数据完整性（注册强制，M0-04）

    未带契约元数据的策略禁止注册：必填契约字段任一缺失（值为空/None）
    即抛 AttributeError 阻断注册（写表前校验）。

    Args:
        cls: 待注册的策略类（已确认是 StrategyBase 子类）。

    Raises:
        AttributeError: 契约必填字段缺失。
    """
    missing = [f for f in _CONTRACT_REQUIRED_FIELDS if not getattr(cls, f, None)]
    if missing:
        raise AttributeError(
            f"策略类 {cls.__name__} 未带契约元数据，缺少必填字段: {missing}；"
            f"未带契约元数据的策略禁止注册（防泄漏门禁 M0-04，v3.0 §5.2）"
        )


# ═══════════════════════════════════════════════
#  注册装饰器
# ═══════════════════════════════════════════════

def register_strategy(cls: Type[StrategyBase]) -> Type[StrategyBase]:
    """
    策略注册装饰器

    将策略类注册到全局注册表，支持按名称动态创建实例。

    用法:
        @register_strategy
        class RSRSStrategy(StrategyBase):
            name = "rsrs"
            ...

    参数:
        cls: 策略类（必须继承 StrategyBase）

    返回:
        原始类（不修改）

    异常:
        TypeError: 不是 StrategyBase 子类
        AttributeError: 未设置 name 属性
    """
    if not isinstance(cls, type) or not issubclass(cls, StrategyBase):
        raise TypeError(f"只有 StrategyBase 子类才能注册，{cls} 不是")

    name = getattr(cls, 'name', None)
    if not name or name == "base_strategy":
        raise AttributeError(f"策略类 {cls.__name__} 必须设置唯一的 name 属性")

    # ★ M0-04 注册强制契约元数据（写表前校验，未带契约即阻断注册）
    _validate_contract_metadata(cls)

    if name in _strategy_registry:
        logger.warning(f"策略 [{name}] 已存在，将被覆盖: {_strategy_registry[name].__name__} → {cls.__name__}")

    _strategy_registry[name] = cls
    logger.debug(f"策略已注册: {name} -> {cls.__name__}")
    return cls


# ═══════════════════════════════════════════════
#  工厂方法
# ═══════════════════════════════════════════════

def create_strategy(name: str, **params: Any) -> StrategyBase:
    """
    按名称创建策略实例

    参数:
        name: 策略名称（如 "rsrs", "ma_cross"）
        **params: 策略参数

    返回:
        StrategyBase: 策略实例

    异常:
        ValueError: 策略未注册
    """
    if name not in _strategy_registry:
        available = list_strategies()
        raise ValueError(
            f"未注册的策略: [{name}]，可用策略: {available}"
        )
    strategy = _strategy_registry[name](**params)
    logger.info(f"策略实例已创建: {name} -> {strategy.__class__.__name__}, params={params}")
    return strategy


def get_strategy_class(name: str) -> Optional[Type[StrategyBase]]:
    """获取策略类（不实例化）"""
    return _strategy_registry.get(name)


def list_strategies() -> List[str]:
    """列出所有已注册的策略名称"""
    return sorted(list(_strategy_registry.keys()))


# ═══════════════════════════════════════════════
#  动态发现
# ═══════════════════════════════════════════════

def discover_strategies(package_name: str = "myquant.strategies") -> int:
    """
    动态发现指定包中的所有策略类

    遍历包中的所有模块，导入后自动注册带有 @register_strategy 装饰器的策略类。

    参数:
        package_name: 包名（默认 myquant.strategies）

    返回:
        新注册的策略数量
    """
    before = set(_strategy_registry.keys())
    try:
        package = importlib.import_module(package_name)
        package_path = getattr(package, '__path__', None)
        if package_path is None:
            logger.warning(f"包 {package_name} 不是可遍历的包")
            return 0

        for _, module_name, _ in pkgutil.iter_modules(package_path):
            full_module = f"{package_name}.{module_name}"
            try:
                importlib.import_module(full_module)
                logger.debug(f"已导入策略模块: {full_module}")
            except Exception as e:
                logger.warning(f"导入策略模块失败: {full_module}, error={e}")
    except Exception as e:
        logger.warning(f"动态发现策略失败: {package_name}, error={e}")

    after = set(_strategy_registry.keys())
    new_count = len(after - before)
    logger.info(f"动态发现完成: 新注册 {new_count} 个策略，总计 {len(_strategy_registry)} 个")
    return new_count


def discover_external_strategies(entry_point_group: str = "myquant.strategies") -> int:
    """
    发现外部插件包注册的策略

    通过 pyproject.toml 的 entry_points 机制发现外部策略包。
    支持用户安装第三方策略包后自动加载。

    参数:
        entry_point_group: entry_points 组名（默认 myquant.strategies）

    返回:
        新注册的策略数量
    """
    before = set(_strategy_registry.keys())
    try:
        # Python 3.9+ 使用 importlib.metadata
        from importlib.metadata import entry_points

        eps = entry_points()
        # 兼容不同Python版本的API
        if hasattr(eps, 'select'):
            group_eps = eps.select(group=entry_point_group)
        else:
            group_eps = eps.get(entry_point_group, [])

        for ep in group_eps:
            try:
                strategy_cls = ep.load()
                if issubclass(strategy_cls, StrategyBase):
                    name = getattr(strategy_cls, 'name', ep.name)
                    if name not in _strategy_registry:
                        _strategy_registry[name] = strategy_cls
                        logger.info(f"外部策略已注册: {name} -> {strategy_cls.__name__} (from {ep.dist})")
            except Exception as e:
                logger.warning(f"加载外部策略失败: {ep.name}, error={e}")
    except Exception as e:
        logger.debug(f"外部策略发现跳过（可能无外部包）: {e}")

    after = set(_strategy_registry.keys())
    return len(after - before)


# ═══════════════════════════════════════════════
#  自动发现（导入时执行）
# ═══════════════════════════════════════════════

# 导入本包内的策略模块（触发 @register_strategy 装饰器）
# 显式列出全部策略模块：回测/实盘共用同一注册器，导入即注册，切换零修改
try:
    from myquant.strategies import ma_strategy  # noqa: F401
except ImportError:
    pass

try:
    from myquant.strategies import grid_strategy  # noqa: F401
except ImportError:
    pass

try:
    from myquant.strategies import momentum_strategy  # noqa: F401
except ImportError:
    pass

try:
    from myquant.strategies import mean_reversion_strategy  # noqa: F401
except ImportError:
    pass

try:
    from myquant.strategies import multi_factor_strategy  # noqa: F401
except ImportError:
    pass

try:
    from myquant.strategies import kunpeng_strategy  # noqa: F401
except ImportError:
    pass

# 尝试发现外部策略
discover_external_strategies()
