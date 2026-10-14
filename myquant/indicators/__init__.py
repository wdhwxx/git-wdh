# 文件名：myquant/indicators/__init__.py
"""
鲲鹏量化交易系统 - 指标层（Indicators Layer）
==============================================
统一指标基类、防未来函数校验、指标注册表、流式计算接口
为实时行情及实盘无缝对接预留完整的增量计算能力

设计原则：
1. 所有指标继承 IndicatorBase，统一批量计算 + 流式更新双模式
2. 严格防未来函数：第i根K线的指标值仅使用 iloc[:i] 或 iloc[:i+1] 数据
3. 回测与实盘共用同一套指标计算逻辑，仅数据输入方式不同
4. 指标结果可持久化到 datasets/features/，支持增量更新
5. 三级因子缓存（V5新增）：L1内存 / L2磁盘 / L3回源计算，数据指纹参与缓存Key
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from abc import ABC, abstractmethod
from collections import deque
from functools import wraps
from typing import Any, Deque, Dict, List

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger

logger = get_logger(__name__)


# ============================================================================
# 防未来函数校验工具
# ============================================================================

def lookahead_check(indicator_name: str):
    """
    防未来函数装饰器
    校验指标计算结果中是否存在NaN前置（即前N行必须为NaN，因为数据不足）
    用于批量计算模式的自动校验
    """
    def decorator(func):
        @wraps(func)
        def wrapper(self, data: pd.DataFrame, **kwargs) -> pd.DataFrame:
            result = func(self, data, **kwargs)
            # 校验：结果的前 min_periods-1 行必须为NaN（数据不足无法计算）
            min_periods = getattr(self, '_min_periods', None)
            if min_periods is not None and len(result) > min_periods:
                first_valid = result.iloc[:, 0].first_valid_index()
                if first_valid is not None:
                    first_valid_pos = result.index.get_loc(first_valid)
                    if first_valid_pos < min_periods - 1:
                        logger.warning(
                            f"[防未来函数] 指标 {indicator_name} 第{first_valid_pos}行"
                            f"即产生有效值，预期至少第{min_periods-1}行，"
                            f"可能存在未来函数风险！"
                        )
            return result
        return wrapper
    return decorator


# ============================================================================
# 统一指标基类
# ============================================================================

class IndicatorBase(ABC):
    """
    指标统一抽象基类（Abstract Base Class）

    双模式设计：
    -----------
    1. 批量计算模式（calculate）：回测使用，输入完整DataFrame，输出对齐的指标DataFrame
    2. 流式更新模式（update）：实盘使用，逐根K线输入，维护内部状态，返回最新值

    防未来函数约定：
    ----------------
    - 批量模式：第i行指标值仅使用 iloc[:i+1] 的数据（pandas rolling 默认行为）
    - RSRS等特殊指标：第i行使用 iloc[i-period:i]（不含第i根），详见各指标实现
    - 流式模式：update(bar) 传入的bar视为"已收盘"的K线，计算结果在该bar收盘后可用
    """

    # 指标唯一名称（子类必须覆盖）
    name: str = "base_indicator"

    # 指标输出列名列表（子类必须覆盖）
    output_columns: List[str] = []

    # 最少需要多少根K线才能产生第一个有效值（用于防未来函数校验）
    _min_periods: int = 1

    def __init__(self, **params: Any):
        """
        初始化指标

        参数:
            **params: 指标参数，如 period=18, norm_window=60 等
        """
        self.params: Dict[str, Any] = params
        # 流式计算状态字典
        self._state: Dict[str, Any] = {}
        # 流式模式下的历史K线环形缓冲区（Ring Buffer）
        # 注意: maxlen使用足够大的默认值，避免子类period未初始化时报错
        # 子类如需自定义大小，可在__init__中重新创建: self._history = deque(maxlen=...)
        self._history: Deque[Dict[str, Any]] = deque(maxlen=10000)
        # 流式模式下的指标值历史（用于需要历史指标值的计算，如EMA递推、RSRS标准化）
        self._indicator_history: Deque[float] = deque(maxlen=10000)
        logger.debug(f"指标初始化: {self.name}, 参数: {params}")

    # ------------------------------------------------------------------
    # 抽象方法（子类必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """
        批量计算指标（回测模式）

        参数:
            data: 行情数据DataFrame，必须包含 open/high/low/close/volume 列
                  索引为日期或整数位置
            **kwargs: 额外参数

        返回:
            pd.DataFrame: 指标结果，索引与输入data对齐，列为 output_columns
                          前 N-1 行为NaN（数据不足）
        """
        pass

    # ------------------------------------------------------------------
    # 流式更新（实盘模式，子类按需覆盖）
    # ------------------------------------------------------------------

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新指标（实盘模式）

        传入一根已收盘的K线，更新内部状态并返回最新指标值。
        为实时行情及实盘无缝对接预留核心接口。

        参数:
            bar: 单根K线字典，必须包含 date/open/high/low/close/volume
                 date 可为 datetime 或字符串

        返回:
            Dict[str, float]: 最新指标值字典，key为 output_columns 中的列名
                              数据不足时返回 NaN
        """
        raise NotImplementedError(
            f"指标 {self.name} 暂未实现流式更新接口(update)，"
            f"实盘对接前请实现此方法。"
        )

    def reset(self) -> None:
        """重置指标状态（流式模式下切换标的或重新开始时调用）"""
        self._state.clear()
        self._history.clear()
        self._indicator_history.clear()
        logger.debug(f"指标状态已重置: {self.name}")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def get_params(self) -> Dict[str, Any]:
        """获取指标参数副本"""
        return self.params.copy()

    def get_output_columns(self) -> List[str]:
        """获取输出列名"""
        return self.output_columns.copy()

    def _validate_data(self, data: pd.DataFrame) -> None:
        """校验输入数据是否包含必要列"""
        required = ['开盘价', '最高价', '最低价', '收盘价']
        missing = [col for col in required if col not in data.columns]
        if missing:
            raise ValueError(
                f"指标 [{self.name}] 输入数据缺少必要列: {missing}，"
                f"当前列: {list(data.columns)}"
            )

    def _make_result_frame(self, data: pd.DataFrame, values: Dict[str, pd.Series]) -> pd.DataFrame:
        """
        构建结果DataFrame，确保索引与输入对齐

        参数:
            data: 原始输入数据（用于对齐索引）
            values: {列名: Series} 字典

        返回:
            对齐后的DataFrame
        """
        df = pd.DataFrame(index=data.index)
        for col, series in values.items():
            df[col] = series.values if len(series) == len(df) else np.nan
        return df

    def _bar_to_array(self, bar: Dict[str, Any], fields: List[str]) -> np.ndarray:
        """将单根K线字典转为指定字段的数组（流式更新用）"""
        return np.array([bar.get(f, np.nan) for f in fields], dtype=np.float64)

    # ------------------------------------------------------------------
    # 子类可覆盖的配置方法
    # ------------------------------------------------------------------

    def _get_history_size(self) -> int:
        """流式模式下K线历史缓冲区大小，子类按需覆盖"""
        return 500

    def _get_indicator_history_size(self) -> int:
        """流式模式下指标值历史缓冲区大小，子类按需覆盖"""
        return 500


# ============================================================================
# 指标注册表（Indicator Registry）
# ============================================================================

_indicator_registry: Dict[str, type] = {}


def register_indicator(cls: type) -> type:
    """
    指标注册装饰器
    将指标类注册到全局注册表，支持按名称动态创建指标实例

    用法:
        @register_indicator
        class RSRS(IndicatorBase):
            name = "rsrs"
            ...
    """
    if not issubclass(cls, IndicatorBase):
        raise TypeError(f"只有 IndicatorBase 子类才能注册，{cls} 不是")
    if not hasattr(cls, 'name') or cls.name == "base_indicator":
        raise AttributeError(f"指标类 {cls} 必须设置唯一的 name 属性")
    if cls.name in _indicator_registry:
        logger.warning(f"指标 [{cls.name}] 已存在，将被覆盖")
    _indicator_registry[cls.name] = cls
    logger.debug(f"指标已注册: {cls.name} -> {cls.__name__}")
    return cls


def get_indicator(name: str, **params: Any) -> IndicatorBase:
    """
    根据名称获取指标实例

    参数:
        name: 指标名称（如 "rsrs", "sma", "atr"）
        **params: 指标参数

    返回:
        IndicatorBase 实例

    异常:
        ValueError: 指标未注册
    """
    if name not in _indicator_registry:
        available = list(_indicator_registry.keys())
        raise ValueError(
            f"未注册的指标: [{name}]，可用指标: {available}"
        )
    return _indicator_registry[name](**params)


def list_indicators() -> List[str]:
    """列出所有已注册的指标名称"""
    return sorted(list(_indicator_registry.keys()))


# ============================================================================
# 批量指标计算工具（为因子计算、特征工程提供统一入口）
# ============================================================================

def calculate_indicators(
    data: pd.DataFrame,
    indicator_configs: List[Dict[str, Any]],
) -> pd.DataFrame:
    """
    批量计算多个指标，返回合并后的DataFrame

    为三因子股票池筛选、特征工程提供统一入口。

    参数:
        data: 行情数据DataFrame
        indicator_configs: 指标配置列表，每个元素为:
            {
                "name": "rsrs",           # 指标名称
                "params": {"period": 18}, # 指标参数
                "prefix": "rsrs_18_"      # 可选，列名前缀，避免重名
            }

    返回:
        pd.DataFrame: 所有指标结果合并，列名为 前缀+原始列名
    """
    results = []
    for config in indicator_configs:
        ind_name = config["name"]
        params = config.get("params", {})
        prefix = config.get("prefix", "")

        indicator = get_indicator(ind_name, **params)
        ind_result = indicator.calculate(data)

        if prefix:
            ind_result.columns = [prefix + col for col in ind_result.columns]
        results.append(ind_result)

    if not results:
        return pd.DataFrame(index=data.index)

    return pd.concat(results, axis=1)


def save_indicators_to_features(
    result: pd.DataFrame,
    symbol: str,
    output_dir: str = "datasets/features",
) -> str:
    """
    将指标计算结果保存到 features 目录（Parquet格式）

    为因子持久化、增量更新预留接口。

    参数:
        result: 指标结果DataFrame，index为日期
        symbol: 股票代码（如 000001.SZ）
        output_dir: 输出目录

    返回:
        保存的文件路径
    """
    import os
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{symbol}_features.parquet")
    result.to_parquet(filepath)
    logger.info(f"指标特征已保存: {filepath} ({len(result)} 行)")
    return filepath


# ============================================================================
# 三级因子缓存（V5回测性能优化新增，独立于指标注册表）
# ============================================================================

from .factor_cache import (  # noqa: E402
    FactorCache,
    get_factor_cache,
    is_parallel_mode,
    reset_factor_cache,
    set_parallel_mode,
)

# ============================================================================
# 向量化因子引擎（V5回测性能优化新增，批量计算+缓存+指纹联动）
# ============================================================================
from .factor_engine import (  # noqa: E402
    FactorEngine,
    get_factor_engine,
    reset_factor_engine,
)
from .momentum import KDJ, MOM, ROC, RSI  # noqa: E402

# ============================================================================
# 导入各指标模块（必须在基类和注册表定义之后，避免循环导入）
# ============================================================================
from .moving_average import EMA, MACD, SMA, WMA  # noqa: E402
from .rsrs import RSRS  # noqa: E402
from .volatility import ATR, BollingerBands, ExtremeRegime, Volatility  # noqa: E402

__all__ = [
    # 核心基础设施
    'IndicatorBase',
    'register_indicator',
    'get_indicator',
    'list_indicators',
    'lookahead_check',
    'calculate_indicators',
    'save_indicators_to_features',
    # 三级因子缓存（V5新增）
    'FactorCache',
    'get_factor_cache',
    'reset_factor_cache',
    'set_parallel_mode',
    'is_parallel_mode',
    # 向量化因子引擎（V5新增）
    'FactorEngine',
    'get_factor_engine',
    'reset_factor_engine',
    # 移动平均类指标
    'SMA', 'EMA', 'WMA', 'MACD',
    # 动量类指标
    'ROC', 'MOM', 'RSI', 'KDJ',
    # 波动率类指标
    'ATR', 'Volatility', 'BollingerBands', 'ExtremeRegime',
    # RSRS核心指标
    'RSRS',
]
