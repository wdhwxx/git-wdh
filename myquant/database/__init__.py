# 文件名：myquant/database/__init__.py
"""
database - 数据库层
==================
量化交易系统的本地数据存储层，基于SQLite实现。

功能清单:
  - connection : 数据库连接管理（同步/异步、连接池、事务）
  - schema     : 表结构定义（行情/财务/因子/状态/数据源）
  - queries    : 统一查询接口（CRUD、批量操作、条件查询）
  - migrations : 数据库迁移框架（版本管理、升级/回滚）

设计原则:
  - 回测和实盘共用同一套数据库结构
  - 支持增量更新和断点续传
  - 所有写入操作支持事务，防止数据损坏
  - 为多市场（A股/港股/美股）预留扩展字段
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from .connection import DatabaseConnection, get_connection, close_all_connections
from .schema import (
    create_all_tables,
    TABLE_NAMES,
    MarketDataSchema,
    FinancialDataSchema,
    FactorDataSchema,
    StrategyStateSchema,
    DataSourceSchema,
    CleanRunMetaSchema,
    DirtyDataStatsSchema,
)
from .queries import DataQueries, CleanDataQueries

__all__ = [
    "DatabaseConnection",
    "get_connection",
    "close_all_connections",
    "create_all_tables",
    "TABLE_NAMES",
    "MarketDataSchema",
    "FinancialDataSchema",
    "FactorDataSchema",
    "StrategyStateSchema",
    "DataSourceSchema",
    "CleanRunMetaSchema",
    "DirtyDataStatsSchema",
    "DataQueries",
    "CleanDataQueries",
]
