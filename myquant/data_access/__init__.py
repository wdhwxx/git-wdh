# 文件名：myquant/data_access/__init__.py
"""
data_access - 数据访问层
========================
统一数据访问入口，包含数据获取、管理、清洗、优化加载等能力。

模块:
  - fetcher   : DataFetcher 多数据源获取器（缓存+降级）
  - manager   : DataManager 数据管理器（版本管理+增量更新）
  - factory   : create_default_fetcher / create_fetcher 工厂函数
  - sources   : 数据源实现集合（tdx/akshare/composite）
  - loader    : OptimizedDataLoader 优化数据加载器（V5新增：预取+分块+流水线+MemoryWatcher）
  - full_clean_service : FullCleanService 全量清洗服务
"""

__version__ = "0.2.0"
__author__ = "Pythonwdhmm"

# ── 原有核心导出（保持向后兼容）──
from .fetcher import DataFetcher
from .manager import (
    DataManager,
    DatasetVersion,
    DatasetVersionError,
    DatasetVersionNotFoundError,
    DataVersionIntegrityError,
)
from .factory import (
    create_default_fetcher,
    create_fetcher,
    get_available_sources,
    detect_tdx_available,
)
from .sources.base import (
    DataSourceBase,
    DataSourceStatus,
    MarketDataResult,
)
from .sources import CompositeDataSource

# ── V5回测性能优化新增 ──
from .loader import (
    OptimizedDataLoader,
    LoadStats,
    DEFAULT_PREFETCH_COUNT,
    DEFAULT_CHUNK_DAYS,
    DEFAULT_WARNING_PCT,
    DEFAULT_CRITICAL_PCT,
)

__all__ = [
    # 原有核心
    "DataFetcher",
    "DataManager",
    "DatasetVersion",
    "DatasetVersionError",
    "DatasetVersionNotFoundError",
    "DataVersionIntegrityError",
    "create_default_fetcher",
    "create_fetcher",
    "get_available_sources",
    "detect_tdx_available",
    "DataSourceBase",
    "DataSourceStatus",
    "MarketDataResult",
    "CompositeDataSource",
    # V5新增
    "OptimizedDataLoader",
    "LoadStats",
    "DEFAULT_PREFETCH_COUNT",
    "DEFAULT_CHUNK_DAYS",
    "DEFAULT_WARNING_PCT",
    "DEFAULT_CRITICAL_PCT",
]
