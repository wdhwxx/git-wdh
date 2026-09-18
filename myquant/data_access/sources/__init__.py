# 文件名：myquant/data_access/sources/__init__.py
"""
sources - 数据源实现集合
=======================
包含各种数据源的实现，所有数据源继承自 base.DataSourceBase。

数据源列表:
  - base.py            : 数据源抽象基类（统一接口定义，核心）
  - tdx_source.py      : 通达信本地数据源（.day文件解析，回测主力）
  - akshare_source.py  : akshare网络数据源（财务/补充/校验）
  - broker_source.py   : 券商实时数据源（预留接口，XTP/QMT）
  - composite_source.py: 组合数据源（多源融合+自动降级）

数据源注册机制:
  所有数据源通过 register_data_source() 注册，
  CompositeDataSource 按优先级自动选择和降级。
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from .base import DataSourceBase, DataSourceStatus, MarketDataResult
from .composite_source import CompositeDataSource

__all__ = [
    "DataSourceBase",
    "DataSourceStatus",
    "MarketDataResult",
    "CompositeDataSource",
]
