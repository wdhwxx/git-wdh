# 文件名：myquant/backtest/data_loader.py
"""
data_loader.py - 回测数据加载兼容层（薄壳 re-export）
=====================================================

⚠️ 2026-10-03 架构治理：本文件原含 DataProviderBase + HistoricalDataProvider 真实实现，
为打断 data_access <-> backtest 循环依赖，真实实现已下沉至
``myquant/data_access/provider.py``。

历史循环依赖：
  - data_access/loader.py 顶层 import backtest.data_loader.HistoricalDataProvider
  - backtest/data_loader.py 懒加载 data_access/factor_sync_service.FactorSyncService
  形成 data_access -> backtest -> data_access 环。

本文件现在仅做 re-export，保持向后兼容：
  - 所有历史调用方（run_*.py / scripts/ / gui/ / backtest/ 内部 / tests/，共 18 处）
    ``from myquant.backtest.data_loader import ...`` 零改动继续工作。
  - 新代码应直接 ``from myquant.data_access.provider import ...``。

真实实现、防未来函数逻辑、复权现算逻辑全部在 ``myquant/data_access/provider.py``。
"""

from __future__ import annotations

from myquant.core.logger import get_logger
from myquant.data_access.provider import (
    CN_INDEX_NAME,
    EN_TO_CN_COLUMN_MAP,
    REQUIRED_CN_OHLCV_COLUMNS,
    REQUIRED_EN_OHLCV_COLUMNS,
    DataProviderBase,
    HistoricalDataProvider,
    require_ohlcv,
)

# 保留原 logger 名，避免历史日志配置（logging.getLogger("backtest.data_loader")）失效
logger = get_logger("backtest.data_loader")


__all__ = [
    "CN_INDEX_NAME",
    "EN_TO_CN_COLUMN_MAP",
    "REQUIRED_CN_OHLCV_COLUMNS",
    "REQUIRED_EN_OHLCV_COLUMNS",
    "DataProviderBase",
    "HistoricalDataProvider",
    "require_ohlcv",
]
