# 文件名：myquant/data_access/fetcher.py
"""
fetcher.py - 数据获取器（统一调度+内存缓存）
=============================================
数据层的统一入口，整合多数据源能力，提供内存缓存加速。

缓存架构（M3修复：描述与实现对齐，消除"三级缓存"名实不符）:
  v1 实现层:
    1. 内存缓存（LRU+TTL，进程内，最快）
    2. 原始数据源（通达信/akshare/券商）
  架构分层说明（不在本模块重复实现，避免职责重叠）:
    - 文件缓存层职责由 DataManager 的 datasets/processed/ 承担
      （Parquet 标准数据集 + DatasetVersion 版本管理）
    - 数据库缓存层职责由 database/quant.db 承担
      （market_data 表 + DataQueries 类型安全 CRUD）
  实盘/实时行情预留: 实时行情绕过缓存直接走数据源（subscribe_realtime）

缓存策略:
  - 行情数据：按股票+日期范围缓存，命中则直接返回
  - 财务数据：按股票+报告期缓存
  - 缓存失效：数据更新时自动清除对应缓存（invalidate_cache）
  - 防未来函数：缓存key包含end_date，确保不返回未来数据
  - 跨进程持久化需求：走 DataManager.processed / database 层（见上）

设计原则:
  - 对上层透明：策略层调用get_data，不知道背后有多少缓存和数据源
  - 防未来函数：所有查询严格传递end_date
  - 线程安全：多线程环境下缓存一致
  - 为实盘预留：实时行情绕过缓存，直接走数据源

依赖: typing, threading, datetime, functools, pathlib, myquant.data_access.sources
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .sources.base import (
    DataSourceBase,
    MarketDataResult,
)
from .sources.composite_source import CompositeDataSource

# ─────────────────────────────────────────────
# 常量定义
# ─────────────────────────────────────────────

# 内存缓存最大条目数
DEFAULT_CACHE_SIZE = 1000

# 缓存默认有效期（秒），行情数据当天有效
DEFAULT_CACHE_TTL = 86400  # 24小时


def _eod_expiry_ts() -> float:
    """P2-08：缓存到期对齐交易日收盘（15:30），而非固定24h墙钟。

    盘中未定型 bar 不再以同一 key 缓存满 24h；收盘后缓存到当日15:30即失效，
    避免次日命中收盘前旧值。收盘后调用则对齐到下一个交易日15:30。
    """
    now = datetime.now()
    eod = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now >= eod:
        eod = eod + timedelta(days=1)
    return eod.timestamp()


# ─────────────────────────────────────────────
# 内存缓存实现（LRU + TTL）
# ─────────────────────────────────────────────

class _MemoryCache:
    """
    内存缓存（LRU + TTL）
    =====================
    线程安全的内存缓存，支持LRU淘汰和TTL过期。
    """

    def __init__(self, max_size: int = DEFAULT_CACHE_SIZE, ttl: int = DEFAULT_CACHE_TTL):
        self._cache: Dict[str, Tuple[Any, float]] = {}  # {key: (value, timestamp)}
        self._max_size = max_size
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """获取缓存，过期返回None"""
        with self._lock:
            if key not in self._cache:
                return None
            value, expire_at = self._cache[key]
            # 检查到期（P2-08：expire_at 为交易日收盘对齐时间戳）
            if datetime.now().timestamp() > expire_at:
                del self._cache[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        """设置缓存，超过max_size则淘汰最旧的"""
        with self._lock:
            if len(self._cache) >= self._max_size:
                # 淘汰最旧的（简单实现：删除第一个）
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            # P2-08：默认按交易日收盘(15:30)到期；显式传入自定义ttl则按now+ttl
            if self._ttl == DEFAULT_CACHE_TTL:
                expire_at = _eod_expiry_ts()
            else:
                expire_at = datetime.now().timestamp() + self._ttl
            self._cache[key] = (value, expire_at)

    def invalidate(self, key: str) -> None:
        """使指定缓存失效"""
        with self._lock:
            self._cache.pop(key, None)

    def invalidate_pattern(self, pattern: str) -> int:
        """使匹配模式的缓存失效，返回失效数量"""
        with self._lock:
            keys_to_remove = [k for k in self._cache if pattern in k]
            for k in keys_to_remove:
                del self._cache[k]
            return len(keys_to_remove)

    def clear(self) -> None:
        """清空所有缓存"""
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


# ─────────────────────────────────────────────
# 数据获取器
# ─────────────────────────────────────────────

class DataFetcher:
    """
    数据获取器（统一调度+多级缓存）
    ================================
    数据层的统一入口，提供带缓存的数据获取服务。

    Usage:
        from myquant.data_access import DataFetcher
        from myquant.data_access.sources.tdx_source import TdxDataSource
        from myquant.data_access.sources.akshare_source import AkshareDataSource

        fetcher = DataFetcher()
        fetcher.add_source(TdxDataSource())
        fetcher.add_source(AkshareDataSource())

        # 获取行情数据（自动缓存）
        result = fetcher.get_market_data("000001.SZ", "2024-01-01", "2024-12-31")

        # 获取财务数据
        financial = fetcher.get_financial_data("600519.SH")

        # 获取股票列表
        stocks = fetcher.get_stock_list()
    """

    def __init__(
        self,
        source: Optional[DataSourceBase] = None,
        cache_size: int = DEFAULT_CACHE_SIZE,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        enable_cache: bool = True,
    ):
        """
        Parameters
        ----------
        source : DataSourceBase | None
            数据源，None则创建空的CompositeDataSource
        cache_size : int
            内存缓存大小
        cache_ttl : int
            缓存有效期（秒）
        enable_cache : bool
            是否启用缓存
        """
        self._source = source or CompositeDataSource()
        self._enable_cache = enable_cache
        self._cache = _MemoryCache(max_size=cache_size, ttl=cache_ttl)
        self._lock = threading.Lock()

    # ── 属性 ──

    @property
    def source(self) -> DataSourceBase:
        """底层数据源"""
        return self._source

    @property
    def cache_size(self) -> int:
        """当前缓存条目数"""
        return len(self._cache)

    # ── 数据源管理 ──

    def add_source(self, source: DataSourceBase) -> None:
        """添加数据源（如果底层是CompositeDataSource）"""
        if isinstance(self._source, CompositeDataSource):
            self._source.add_source(source)
        else:
            # 如果不是组合源，自动升级为组合源
            composite = CompositeDataSource()
            composite.add_source(self._source)
            composite.add_source(source)
            self._source = composite

    # ── 缓存key生成 ──

    @staticmethod
    def _make_cache_key(
        data_type: str,
        symbol: str,
        start_date: Optional[str],
        end_date: Optional[str],
        period: str = "daily",
        adjust: Optional[str] = "raw",
        **kwargs,
    ) -> str:
        """
        生成缓存key

        【防未来函数】key包含end_date，确保不同end_date的缓存不混淆
        """
        parts = [data_type, symbol, period, str(adjust)]
        if start_date:
            parts.append(f"start={start_date}")
        if end_date:
            parts.append(f"end={end_date}")
        for k, v in sorted(kwargs.items()):
            parts.append(f"{k}={v}")
        return "|".join(parts)

    # ═══════════════════════════════════════════
    # 行情数据获取（带缓存）
    # ═══════════════════════════════════════════

    def get_market_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
        use_cache: bool = True,
    ) -> MarketDataResult:
        """
        获取行情数据（带内存缓存；持久化层见模块文档说明）

        【防未来函数】end_date 之后的数据严格不返回

        Parameters
        ----------
        symbol : str
            股票代码
        start_date : str | None
            开始日期
        end_date : str | None
            截止日期（防未来函数关键参数）
        period : str
            周期
        adjust : str | None
            复权方式
        use_cache : bool
            是否使用缓存（实时行情设为False）

        Returns
        -------
        MarketDataResult : 行情数据结果
        """
        if not self._enable_cache or not use_cache:
            return self._source.get_market_data(symbol, start_date, end_date, period, adjust)

        cache_key = self._make_cache_key(
            "market", symbol, start_date, end_date, period, adjust
        )
        # 尝试从缓存获取
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # 缓存未命中，从数据源获取
        result = self._source.get_market_data(symbol, start_date, end_date, period, adjust)

        # 写入缓存（只缓存有数据的结果）
        if result and result.count > 0:
            self._cache.set(cache_key, result)

        return result

    def get_market_data_batch(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ) -> Dict[str, MarketDataResult]:
        """
        批量获取行情数据（逐只带缓存）

        【防未来函数】end_date 传递给每只股票
        """
        results = {}
        for symbol in symbols:
            results[symbol] = self.get_market_data(
                symbol, start_date, end_date, period, adjust
            )
        return results

    # ═══════════════════════════════════════════
    # 财务数据获取（带缓存）
    # ═══════════════════════════════════════════

    def get_financial_data(
        self,
        symbol: str,
        report_date: Optional[str] = None,
        end_date: Optional[str] = None,
        use_cache: bool = True,
    ) -> List[Dict]:
        """
        获取财务数据（带缓存）

        【防未来函数】end_date 之后的报告期数据不返回
        """
        if not self._enable_cache or not use_cache:
            return self._source.get_financial_data(symbol, report_date, end_date)

        cache_key = self._make_cache_key(
            "financial", symbol, report_date, end_date, report_type="financial"
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._source.get_financial_data(symbol, report_date, end_date)
        if result:
            self._cache.set(cache_key, result)
        return result

    def get_financial_history(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        use_cache: bool = True,
    ) -> List[Dict]:
        """
        获取历史多期财务数据（带内存缓存，只缓存非空结果）

        【防未来函数】end_date 之后的报告期数据不返回（本行使用的数据截至 end_date 时刻）

        Args:
            symbol: 股票代码，如 "600519.SH"
            start_date: 起始报告期，格式 "YYYYMMDD" 或 "YYYY-MM-DD"
            end_date: 截止报告期，格式同上
            use_cache: 是否使用内存缓存（默认True）

        Returns:
            多期财务记录列表，按报告期倒序排列；空列表表示无数据
        """
        if not self._enable_cache or not use_cache:
            return self._source.get_financial_history(symbol, start_date, end_date)

        cache_key = self._make_cache_key(
            "financial_history", symbol, start_date, end_date,
            report_type="financial_history",
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._source.get_financial_history(symbol, start_date, end_date)
        if result:
            self._cache.set(cache_key, result)
        return result

    # ═══════════════════════════════════════════
    # 基础信息获取
    # ═══════════════════════════════════════════

    def get_stock_list(self, market: str = "A股", use_cache: bool = True) -> List[Dict]:
        """获取股票列表（带缓存，缓存时间较长）"""
        if not self._enable_cache or not use_cache:
            return self._source.get_stock_list(market)

        cache_key = f"stock_list|{market}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._source.get_stock_list(market)
        if result:
            self._cache.set(cache_key, result)
        return result

    def get_index_list(self, market: str = "A股") -> List[Dict]:
        """获取指数列表"""
        if hasattr(self._source, "get_index_list"):
            return self._source.get_index_list(market)
        return []

    def get_trading_dates(
        self,
        start_date: str,
        end_date: str,
        market: str = "A股",
    ) -> List[str]:
        """
        获取交易日列表

        【防未来函数】end_date 之后的日期不返回
        """
        return self._source.get_trading_dates(start_date, end_date, market)

    # ═══════════════════════════════════════════
    # 实时行情（绕过缓存）
    # ═══════════════════════════════════════════

    def subscribe_realtime(
        self,
        symbol: str,
        callback: callable,
        period: str = "tick",
    ) -> bool:
        """
        订阅实时行情（绕过缓存，直接走数据源）

        实盘场景使用，回调函数接收实时行情数据。
        """
        return self._source.subscribe_realtime(symbol, callback, period)

    def unsubscribe_realtime(self, symbol: str, callback: Optional[callable] = None) -> bool:
        """取消实时行情订阅"""
        return self._source.unsubscribe_realtime(symbol, callback)

    # ═══════════════════════════════════════════
    # 缓存管理
    # ═══════════════════════════════════════════

    def invalidate_cache(self, symbol: Optional[str] = None) -> int:
        """
        使缓存失效

        Parameters
        ----------
        symbol : str | None
            指定股票代码，None则清空所有缓存

        Returns
        -------
        int : 失效的缓存条目数
        """
        if symbol is None:
            count = len(self._cache)
            self._cache.clear()
            return count
        return self._cache.invalidate_pattern(symbol)

    def get_cache_stats(self) -> Dict:
        """获取缓存统计信息"""
        return {
            "cache_size": len(self._cache),
            "max_size": self._cache._max_size,
            "ttl": self._cache._ttl,
            "enabled": self._enable_cache,
        }

    # ═══════════════════════════════════════════
    # 健康检查
    # ═══════════════════════════════════════════

    def health_check(self) -> Dict:
        """健康检查，返回所有数据源状态"""
        if isinstance(self._source, CompositeDataSource):
            self._source.health_check()
            return self._source.get_source_status_report()
        else:
            status = self._source.health_check()
            return [{"name": self._source.name, "status": status.value}]

    def __repr__(self) -> str:
        return (
            f"DataFetcher(source={self._source.name}, "
            f"cache_size={len(self._cache)}, enabled={self._enable_cache})"
        )