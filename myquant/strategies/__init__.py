# 文件名：myquant/strategies/__init__.py
"""
鲲鹏量化交易系统 - 策略层（Strategies Layer）
==============================================
策略抽象基类、注册器、三因子股票池构建器、RSRS核心策略实现。

核心组件:
  - StrategyBase     : 策略抽象基类（生命周期/信号生成/仓位管理）
  - Signal           : 交易信号数据结构
  - SignalType       : 信号类型枚举
  - StrategyContext  : 策略上下文（数据/组合/执行器访问）
  - StockPoolBuilder : 三因子股票池构建器（代码过滤+三因子交集）
  - RSRSStrategy     : RSRS三因子策略（核心策略）
  - MACrossStrategy  : 均线交叉策略示例
  - GridStrategy     : 网格策略（震荡市，区间内分批低买高卖）
  - MomentumStrategy : 动量策略（趋势市，定期选动量top_n）
  - MeanReversionStrategy: 均值回归策略（布林带+RSI超买超卖）
  - MultiFactorStrategy : 多因子策略（动量/反转/低波动/量价，z-score加权选股）
  - KunpengStrategy     : 鲲鹏起爆点策略（量均线量比+均线粘合+MACD多维共振三重过滤）
  - register_strategy: 策略注册装饰器
  - create_strategy  : 策略工厂方法
  - list_strategies  : 列出已注册策略

设计原则:
  - 回测/实盘共用同一策略实例，切换零修改
  - 策略通过上下文访问数据/组合/执行器，不依赖具体引擎
  - 严格防未来函数：T日信号T+1成交，RSRS斜率不含第i根
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from myquant.strategies.base import (
    Signal,
    SignalType,
    StockPoolBuilder,
    StrategyBase,
    StrategyContext,
)
from myquant.strategies.grid_strategy import GridStrategy
from myquant.strategies.kunpeng_strategy import KunpengStrategy

# 导入策略实现（触发 @register_strategy 装饰器）
from myquant.strategies.ma_strategy import MACrossStrategy, RSRSStrategy
from myquant.strategies.mean_reversion_strategy import MeanReversionStrategy
from myquant.strategies.momentum_strategy import MomentumStrategy
from myquant.strategies.multi_factor_strategy import MultiFactorStrategy
from myquant.strategies.registry import (
    create_strategy,
    discover_strategies,
    get_strategy_class,
    list_strategies,
    register_strategy,
)

__all__ = [
    # 核心基础设施
    'StrategyBase',
    'Signal',
    'SignalType',
    'StrategyContext',
    'StockPoolBuilder',
    # 注册器
    'register_strategy',
    'create_strategy',
    'get_strategy_class',
    'list_strategies',
    'discover_strategies',
    # 策略实现
    'RSRSStrategy',
    'MACrossStrategy',
    'GridStrategy',
    'MomentumStrategy',
    'MeanReversionStrategy',
    'MultiFactorStrategy',
    'KunpengStrategy',
]
