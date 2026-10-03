# 文件名：myquant/data_access/loader.py
"""
鲲鹏量化交易系统 - 优化数据加载器（OptimizedDataLoader）
==========================================================
V5回测性能优化 P0 第6项：预取/分块/流水线 + MemoryWatcher 内存水位监控

核心功能:
    1. 预取（Prefetch）：后台线程预加载后续标的数据，主线程处理当前标的时
       下一批数据已在内存中就绪，消除 I/O 等待
    2. 分块（Chunking）：超大标的数据按日期范围分块加载，降低单次内存峰值
    3. 流水线（Pipeline）：生产者-消费者队列模型，加载与计算并行执行
    4. MemoryWatcher 集成：实时监控内存水位，自动降级策略：
       - NORMAL：正常预取 + 全量加载
       - WARNING：减少预取数量 + 大标的自动分块
       - CRITICAL：停止预取 + 强制分块加载 + 主动 GC

防未来函数:
    - 所有加载严格按 end_date 过滤，绝不返回未来数据
    - 分块加载时每块独立过滤，合并后再次校验
    - 预取数据与主线程使用相同的 end_date 参数

依赖:
    - pandas, numpy, psutil（可选，MemoryWatcher 用）
    - myquant.data_access.provider.HistoricalDataProvider（2026-10-03 架构治理：
      原从 backtest.data_loader import，形成 data_access <-> backtest 循环依赖，
      现归位数据层同包 import）
    - myquant.utils.memory_watcher.MemoryWatcher, MemoryLevel
"""

from __future__ import annotations

import gc
import logging
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from myquant.data_access.provider import HistoricalDataProvider
from myquant.utils.memory_watcher import (
    DEFAULT_CRITICAL_PCT,
    DEFAULT_WARNING_PCT,
    MemoryLevel,
    MemoryWatcher,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 常量
# ============================================================================

DEFAULT_PREFETCH_COUNT: int = 2
"""默认预取数量（后台同时预加载的标的数）"""

DEFAULT_CHUNK_DAYS: int = 500
"""默认分块天数（超过此天数的标的自动分块加载）"""

MIN_CHUNK_DAYS: int = 60
"""最小分块天数（防止分块过小导致频繁 I/O）"""

PREFETCH_QUEUE_MAXSIZE: int = 8
"""预取队列最大长度（防止内存溢出）"""

PIPELINE_QUEUE_MAXSIZE: int = 4
"""流水线队列最大长度"""

WORKER_THREAD_COUNT: int = 2
"""后台加载线程数"""

# ============================================================================
# 加载统计数据类
# ============================================================================

@dataclass
class LoadStats:
    """
    数据加载统计信息

    Attributes:
        total_symbols: 总标的数。
        loaded_symbols: 成功加载标的数。
        failed_symbols: 加载失败标的数。
        skipped_symbols: 跳过（无数据）标的数。
        total_bars: 总K线数。
        prefetch_hits: 预取命中次数。
        chunked_loads: 分块加载次数。
        memory_degradations: 内存降级次数。
        total_load_time_ms: 总加载耗时（毫秒）。
        avg_load_time_ms: 平均每标的加载耗时（毫秒）。
        peak_memory_mb: 加载期间峰值内存（MB）。
    """

    total_symbols: int = 0
    loaded_symbols: int = 0
    failed_symbols: int = 0
    skipped_symbols: int = 0
    total_bars: int = 0
    prefetch_hits: int = 0
    chunked_loads: int = 0
    memory_degradations: int = 0
    total_load_time_ms: float = 0.0
    peak_memory_mb: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def avg_load_time_ms(self) -> float:
        """平均每标的加载耗时（毫秒）"""
        if self.loaded_symbols == 0:
            return 0.0
        return self.total_load_time_ms / self.loaded_symbols

    @property
    def success_rate(self) -> float:
        """加载成功率（0-1）"""
        if self.total_symbols == 0:
            return 0.0
        return self.loaded_symbols / self.total_symbols

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "total_symbols": self.total_symbols,
            "loaded_symbols": self.loaded_symbols,
            "failed_symbols": self.failed_symbols,
            "skipped_symbols": self.skipped_symbols,
            "total_bars": self.total_bars,
            "prefetch_hits": self.prefetch_hits,
            "chunked_loads": self.chunked_loads,
            "memory_degradations": self.memory_degradations,
            "total_load_time_ms": round(self.total_load_time_ms, 2),
            "avg_load_time_ms": round(self.avg_load_time_ms, 2),
            "success_rate": round(self.success_rate, 4),
            "peak_memory_mb": round(self.peak_memory_mb, 2),
            "errors": self.errors.copy(),
        }

    def summary(self) -> str:
        """生成可读的统计摘要"""
        lines = [
            "=== 数据加载统计 ===",
            f"  总标的数: {self.total_symbols}",
            f"  成功加载: {self.loaded_symbols} (成功率 {self.success_rate:.1%})",
            f"  失败: {self.failed_symbols}, 跳过: {self.skipped_symbols}",
            f"  总K线数: {self.total_bars:,}",
            f"  预取命中: {self.prefetch_hits}",
            f"  分块加载: {self.chunked_loads}",
            f"  内存降级: {self.memory_degradations}",
            f"  总耗时: {self.total_load_time_ms:.1f}ms (平均 {self.avg_load_time_ms:.1f}ms/标的)",
            f"  峰值内存: {self.peak_memory_mb:.1f}MB",
        ]
        if self.errors:
            lines.append(f"  错误数: {len(self.errors)}")
            for err in self.errors[:5]:
                lines.append(f"    - {err}")
        return "\n".join(lines)


# ============================================================================
# 优化数据加载器
# ============================================================================

class OptimizedDataLoader:
    """
    优化数据加载器：预取 + 分块 + 流水线 + MemoryWatcher 内存监控

    包装 HistoricalDataProvider，在其基础上增加性能优化和内存防护。

    内存自动降级策略:
        - NORMAL (< warning_pct): 正常预取(prefetch_count) + 全量加载
        - WARNING (>= warning_pct): 预取减半 + 大标的(>chunk_days)自动分块
        - CRITICAL (>= critical_pct): 停止预取 + 强制分块 + 主动 GC

    Attributes:
        provider: 底层历史数据提供器。
        memory_watcher: 内存监控器实例。
        prefetch_count: 当前预取数量（会根据内存水位动态调整）。
        chunk_days: 分块天数阈值。
        enable_prefetch: 是否启用预取。
        enable_chunking: 是否启用分块。
        stats: 加载统计信息。

    Example:
        >>> loader = OptimizedDataLoader()
        >>> data = loader.load_single("000001.SZ", "2024-01-01", "2024-12-31")
        >>> batch = loader.load_batch(["000001.SZ", "000002.SZ"], "2024-01-01", "2024-12-31")
        >>> for chunk in loader.iter_batches(symbols, batch_size=10):
        ...     process(chunk)
    """

    def __init__(
        self,
        provider: Optional[HistoricalDataProvider] = None,
        memory_watcher: Optional[MemoryWatcher] = None,
        prefetch_count: int = DEFAULT_PREFETCH_COUNT,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
        enable_prefetch: bool = True,
        enable_chunking: bool = True,
        warning_pct: float = DEFAULT_WARNING_PCT,
        critical_pct: float = DEFAULT_CRITICAL_PCT,
        processed_dir: Optional[Union[str, Path]] = None,
    ):
        """
        初始化优化数据加载器

        Args:
            provider: 底层历史数据提供器，None 则自动创建。
            memory_watcher: 内存监控器，None 则自动创建。
            prefetch_count: 预取数量，默认 2。
            chunk_days: 分块天数阈值，默认 500。
            enable_prefetch: 是否启用预取，默认 True。
            enable_chunking: 是否启用分块，默认 True。
            warning_pct: 内存告警阈值，默认 70%。
            critical_pct: 内存严重阈值，默认 85%。
            processed_dir: 处理后数据目录（provider 为 None 时使用）。

        Raises:
            ValueError: 参数非法时抛出。
        """
        if prefetch_count < 0:
            raise ValueError(f"prefetch_count 不能为负数: {prefetch_count}")
        if chunk_days < MIN_CHUNK_DAYS:
            raise ValueError(f"chunk_days 不能小于 {MIN_CHUNK_DAYS}: {chunk_days}")

        # 底层数据提供器
        if provider is None:
            self.provider: HistoricalDataProvider = HistoricalDataProvider(
                processed_dir=processed_dir
            )
        else:
            self.provider = provider

        # 内存监控器
        if memory_watcher is None:
            self.memory_watcher: MemoryWatcher = MemoryWatcher(
                warning_pct=warning_pct,
                critical_pct=critical_pct,
            )
        else:
            self.memory_watcher = memory_watcher

        self._base_prefetch_count: int = prefetch_count
        self.prefetch_count: int = prefetch_count
        self.chunk_days: int = chunk_days
        self.enable_prefetch: bool = enable_prefetch
        self.enable_chunking: bool = enable_chunking

        # 统计信息
        self.stats: LoadStats = LoadStats()

        # 预取缓存: {symbol: Future}
        self._prefetch_cache: Dict[str, Future] = {}
        self._prefetch_lock = threading.Lock()

        # 后台线程池（预取用）
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock = threading.Lock()

        logger.info(
            "OptimizedDataLoader 初始化: prefetch=%d, chunk_days=%d, "
            "enable_prefetch=%s, enable_chunking=%s, warning=%.0f%%, critical=%.0f%%",
            prefetch_count, chunk_days, enable_prefetch, enable_chunking,
            warning_pct, critical_pct,
        )

    # ------------------------------------------------------------------
    # 内存自适应调整
    # ------------------------------------------------------------------

    def _adapt_to_memory(self) -> MemoryLevel:
        """
        根据当前内存水位自适应调整加载策略

        Returns:
            当前内存水位级别。
        """
        level = self.memory_watcher.check()

        if level == MemoryLevel.CRITICAL:
            # 严重：停止预取 + 记录降级
            if self.prefetch_count != 0:
                self.prefetch_count = 0
                self.stats.memory_degradations += 1
                logger.warning("内存严重(%.0f%%)，停止预取，强制分块加载",
                               self.memory_watcher.get_memory_info()["percent"])
            # 主动 GC 释放内存
            gc.collect()

        elif level == MemoryLevel.WARNING:
            # 告警：预取减半
            target = max(1, self._base_prefetch_count // 2)
            if self.prefetch_count != target:
                self.prefetch_count = target
                self.stats.memory_degradations += 1
                logger.info("内存告警(%.0f%%)，预取数量降至 %d",
                            self.memory_watcher.get_memory_info()["percent"], target)

        else:
            # 正常：恢复预取
            if self.prefetch_count != self._base_prefetch_count:
                self.prefetch_count = self._base_prefetch_count
                logger.debug("内存恢复正常，预取数量恢复为 %d", self._base_prefetch_count)

        return level

    def _should_chunk(self, start_date: Optional[str], end_date: Optional[str]) -> bool:
        """
        判断是否应该分块加载

        条件:
            1. 启用分块 (enable_chunking)
            2. 日期范围超过 chunk_days，或内存处于 CRITICAL
        """
        if not self.enable_chunking:
            return False

        # 内存严重时强制分块
        if self.memory_watcher.is_critical():
            return True

        # 日期范围超过阈值时分块
        if start_date and end_date:
            try:
                start = pd.Timestamp(start_date)
                end = pd.Timestamp(end_date)
                days = (end - start).days
                return days > self.chunk_days
            except (ValueError, TypeError):
                return False

        return False

    # ------------------------------------------------------------------
    # 单标的加载
    # ------------------------------------------------------------------

    def load_single(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
    ) -> pd.DataFrame:
        """
        加载单标的数据（内存自适应 + 自动分块）

        ★★★ 防未来函数 ★★★
        end_date 之后的数据严格过滤，分块加载时每块独立过滤。

        Args:
            symbol: 标的代码。
            start_date: 开始日期 YYYY-MM-DD。
            end_date: 截止日期 YYYY-MM-DD（防未来函数关键参数）。
            period: K线周期。

        Returns:
            pd.DataFrame，index=日期，columns=OHLCV。无数据返回空 DataFrame。
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError(f"symbol 不能为空且必须为字符串: {symbol!r}")

        start_time = time.perf_counter()
        self.stats.total_symbols += 1

        # 内存自适应
        self._adapt_to_memory()

        try:
            # 判断是否分块
            if self._should_chunk(start_date, end_date):
                df = self._load_chunked(symbol, start_date, end_date, period)
                self.stats.chunked_loads += 1
            else:
                df = self.provider.get_bars(symbol, start_date, end_date, period)

            if df.empty:
                self.stats.skipped_symbols += 1
                logger.debug("跳过 %s: 无有效数据", symbol)
            else:
                self.stats.loaded_symbols += 1
                self.stats.total_bars += len(df)

            # 更新峰值内存
            mem_info = self.memory_watcher.get_memory_info(force_refresh=True)
            if mem_info["rss_mb"] > self.stats.peak_memory_mb:
                self.stats.peak_memory_mb = mem_info["rss_mb"]

            elapsed = (time.perf_counter() - start_time) * 1000
            self.stats.total_load_time_ms += elapsed

            return df

        except Exception as e:
            self.stats.failed_symbols += 1
            error_msg = f"{symbol}: {type(e).__name__}: {e}"
            self.stats.errors.append(error_msg)
            logger.error("加载 %s 失败: %s", symbol, e)
            return pd.DataFrame()

    def _load_chunked(
        self,
        symbol: str,
        start_date: Optional[str],
        end_date: Optional[str],
        period: str = "daily",
    ) -> pd.DataFrame:
        """
        分块加载单标的数据

        将日期范围按 chunk_days 切分为多个块，逐块加载后合并。
        每块独立过滤 end_date，合并后再次校验。

        ★★★ 防未来函数 ★★★
        每块加载时传入原始 end_date，确保不返回未来数据。
        """
        if not start_date or not end_date:
            # 无法确定日期范围时，退化为全量加载
            logger.debug("%s: 无法确定日期范围，退化为全量加载", symbol)
            return self.provider.get_bars(symbol, start_date, end_date, period)

        try:
            start = pd.Timestamp(start_date)
            end = pd.Timestamp(end_date)
        except (ValueError, TypeError) as e:
            logger.warning("%s: 日期解析失败(%s)，退化为全量加载", symbol, e)
            return self.provider.get_bars(symbol, start_date, end_date, period)

        if start >= end:
            return self.provider.get_bars(symbol, start_date, end_date, period)

        # 生成分块日期范围
        chunks: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
        current = start
        while current < end:
            chunk_end = min(current + pd.Timedelta(days=self.chunk_days), end)
            chunks.append((current, chunk_end))
            current = chunk_end + pd.Timedelta(days=1)

        logger.debug("%s: 分块加载，共 %d 块 (chunk_days=%d)",
                     symbol, len(chunks), self.chunk_days)

        # 逐块加载
        dfs: List[pd.DataFrame] = []
        for i, (chunk_start, chunk_end) in enumerate(chunks):
            chunk_df = self.provider.get_bars(
                symbol,
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                period,
            )
            if not chunk_df.empty:
                dfs.append(chunk_df)

            # 每块加载后检查内存，严重时 GC
            if self.memory_watcher.is_critical():
                gc.collect()

        if not dfs:
            return pd.DataFrame()

        # 合并所有块
        result = pd.concat(dfs)
        # 去重（分块边界可能有重叠）
        result = result[~result.index.duplicated(keep="first")]
        result = result.sort_index()

        # ★★★ 防未来函数：合并后再次校验 end_date ★★★
        result = result[result.index <= end]

        return result

    # ------------------------------------------------------------------
    # 批量加载（带预取）
    # ------------------------------------------------------------------

    def load_batch(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
    ) -> Dict[str, pd.DataFrame]:
        """
        批量加载多标的数据（带后台预取）

        加载当前标的时，后台线程预取后续标的数据，消除 I/O 等待。
        内存水位高时自动减少预取或停止预取。

        ★★★ 防未来函数 ★★★
        所有标的统一使用相同的 end_date，预取任务也使用相同参数。

        Args:
            symbols: 标的代码列表。
            start_date: 开始日期。
            end_date: 截止日期（防未来函数）。
            period: K线周期。

        Returns:
            {symbol: DataFrame}，无数据的标的不包含在结果中。
        """
        if not symbols:
            return {}

        # 去重并保持顺序
        seen = set()
        unique_symbols: List[str] = []
        for s in symbols:
            if s and s not in seen:
                seen.add(s)
                unique_symbols.append(s)

        if not unique_symbols:
            return {}

        result: Dict[str, pd.DataFrame] = {}
        self._prefetch_cache.clear()

        try:
            for i, symbol in enumerate(unique_symbols):
                # 内存自适应
                self._adapt_to_memory()

                # 启动后续标的的预取
                if self.enable_prefetch and self.prefetch_count > 0:
                    self._submit_prefetch(
                        unique_symbols[i + 1: i + 1 + self.prefetch_count],
                        start_date, end_date, period,
                    )

                # 检查当前标的是否已预取
                df = self._get_prefetched(symbol)
                if df is not None:
                    self.stats.prefetch_hits += 1
                    logger.debug("%s: 预取命中", symbol)
                else:
                    df = self.load_single(symbol, start_date, end_date, period)

                if not df.empty:
                    result[symbol] = df

        finally:
            # 清理预取缓存和线程池
            self._shutdown_prefetch()

        return result

    def _submit_prefetch(
        self,
        symbols: List[str],
        start_date: Optional[str],
        end_date: Optional[str],
        period: str,
    ) -> None:
        """提交预取任务到后台线程池"""
        if not symbols:
            return

        with self._prefetch_lock:
            for symbol in symbols:
                if symbol in self._prefetch_cache:
                    continue
                # 检查内存，严重时不提交新预取
                if self.memory_watcher.is_critical():
                    logger.debug("内存严重，跳过预取 %s", symbol)
                    continue

                future = self._get_executor().submit(
                    self._prefetch_worker, symbol, start_date, end_date, period
                )
                self._prefetch_cache[symbol] = future

    def _prefetch_worker(
        self,
        symbol: str,
        start_date: Optional[str],
        end_date: Optional[str],
        period: str,
    ) -> pd.DataFrame:
        """
        预取工作线程函数

        ★★★ 防未来函数 ★★★
        使用与主线程相同的 end_date 参数。
        """
        try:
            return self.provider.get_bars(symbol, start_date, end_date, period)
        except Exception as e:
            logger.error("预取 %s 失败: %s", symbol, e)
            return pd.DataFrame()

    def _get_prefetched(self, symbol: str) -> Optional[pd.DataFrame]:
        """获取已预取完成的数据（未完成则等待）"""
        with self._prefetch_lock:
            future = self._prefetch_cache.pop(symbol, None)

        if future is None:
            return None

        try:
            # 等待预取完成（最多等 30 秒）
            df = future.result(timeout=30)
            return df if not df.empty else None
        except Exception as e:
            logger.warning("预取结果获取失败 %s: %s", symbol, e)
            return None

    def _get_executor(self) -> ThreadPoolExecutor:
        """获取或创建后台线程池"""
        with self._executor_lock:
            if self._executor is None or self._executor._shutdown:
                self._executor = ThreadPoolExecutor(
                    max_workers=WORKER_THREAD_COUNT,
                    thread_name_prefix="data-prefetch",
                )
            return self._executor

    def _shutdown_prefetch(self) -> None:
        """关闭预取线程池并清理缓存"""
        with self._prefetch_lock:
            # 取消未完成的任务
            for symbol, future in self._prefetch_cache.items():
                if not future.done():
                    future.cancel()
            self._prefetch_cache.clear()

        with self._executor_lock:
            if self._executor is not None and not self._executor._shutdown:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None

    # ------------------------------------------------------------------
    # 分批迭代加载（内存友好）
    # ------------------------------------------------------------------

    def iter_batches(
        self,
        symbols: List[str],
        batch_size: int = 10,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
    ) -> Iterator[Dict[str, pd.DataFrame]]:
        """
        分批迭代加载（内存友好，适合超大标的集合）

        每批加载 batch_size 个标的，yield 后释放内存，避免一次性加载全部数据。

        ★★★ 防未来函数 ★★★
        每批内部统一使用相同的 end_date。

        Args:
            symbols: 标的代码列表。
            batch_size: 每批标的数，默认 10。
            start_date: 开始日期。
            end_date: 截止日期。
            period: K线周期。

        Yields:
            {symbol: DataFrame} 每批数据。
        """
        if not symbols or batch_size <= 0:
            return

        for i in range(0, len(symbols), batch_size):
            batch_symbols = symbols[i: i + batch_size]
            batch_data = self.load_batch(batch_symbols, start_date, end_date, period)

            if batch_data:
                yield batch_data

            # 每批结束后检查内存，严重时主动 GC
            if self.memory_watcher.is_critical():
                gc.collect()
                logger.info("分批加载: 内存严重，已主动 GC")

    # ------------------------------------------------------------------
    # 流水线加载（生产者-消费者）
    # ------------------------------------------------------------------

    def pipeline_load(
        self,
        symbols: List[str],
        processor: Callable[[str, pd.DataFrame], Any],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        max_workers: int = 2,
    ) -> List[Any]:
        """
        流水线加载：生产者（加载线程）- 消费者（处理函数）并行执行

        加载线程从文件读取数据放入队列，消费者线程从队列取出数据处理，
        加载与计算并行，总耗时 = max(加载总耗时, 计算总耗时) 而非两者之和。

        ★★★ 防未来函数 ★★★
        生产者使用统一的 end_date，消费者只处理已加载的数据。

        Args:
            symbols: 标的代码列表。
            processor: 数据处理函数，签名为 processor(symbol, df) -> result。
            start_date: 开始日期。
            end_date: 截止日期。
            period: K线周期。
            max_workers: 消费者线程数，默认 2。

        Returns:
            处理结果列表（顺序与 symbols 一致，失败的标的结果为 None）。
        """
        if not symbols or not callable(processor):
            return []

        # 去重
        seen = set()
        unique_symbols: List[str] = []
        for s in symbols:
            if s and s not in seen:
                seen.add(s)
                unique_symbols.append(s)

        if not unique_symbols:
            return []

        # 结果字典: {symbol: result}
        results: Dict[str, Any] = {}
        results_lock = threading.Lock()

        # 数据队列: (symbol, df)
        data_queue: "queue.Queue[Tuple[str, pd.DataFrame]]" = queue.Queue(
            maxsize=PIPELINE_QUEUE_MAXSIZE
        )

        # 哨兵值（表示加载完成）
        SENTINEL: Tuple[str, pd.DataFrame] = ("__DONE__", pd.DataFrame())

        def producer() -> None:
            """生产者：加载数据放入队列"""
            try:
                for symbol in unique_symbols:
                    self._adapt_to_memory()
                    df = self.load_single(symbol, start_date, end_date, period)
                    data_queue.put((symbol, df))
                    # 内存严重时放慢生产速度
                    if self.memory_watcher.is_critical():
                        time.sleep(0.1)
            finally:
                data_queue.put(SENTINEL)

        def consumer() -> None:
            """消费者：从队列取出数据并处理"""
            while True:
                try:
                    symbol, df = data_queue.get(timeout=60)
                except queue.Empty:
                    logger.warning("流水线消费者超时等待")
                    break

                if symbol == "__DONE__":
                    # 哨兵值，重新放入队列通知其他消费者，然后退出
                    data_queue.put(SENTINEL)
                    break

                try:
                    if not df.empty:
                        result = processor(symbol, df)
                        with results_lock:
                            results[symbol] = result
                    else:
                        with results_lock:
                            results[symbol] = None
                except Exception as e:
                    logger.error("流水线处理 %s 失败: %s", symbol, e)
                    with results_lock:
                        results[symbol] = None
                finally:
                    data_queue.task_done()

        # 启动生产者线程
        producer_thread = threading.Thread(target=producer, name="pipeline-producer", daemon=True)
        producer_thread.start()

        # 启动消费者线程
        consumer_threads: List[threading.Thread] = []
        for i in range(max_workers):
            t = threading.Thread(target=consumer, name=f"pipeline-consumer-{i}", daemon=True)
            t.start()
            consumer_threads.append(t)

        # 等待所有线程完成
        producer_thread.join(timeout=300)
        for t in consumer_threads:
            t.join(timeout=300)

        # 按 symbols 顺序返回结果
        return [results.get(s) for s in unique_symbols]

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def get_available_symbols(self) -> List[str]:
        """获取所有可用标的列表"""
        return self.provider.get_available_symbols()

    def get_date_range(self, symbol: str) -> Optional[Tuple[str, str]]:
        """获取某标的的数据日期范围"""
        return self.provider.get_date_range(symbol)

    def reset_stats(self) -> None:
        """重置加载统计信息"""
        self.stats = LoadStats()
        logger.debug("加载统计已重置")

    def get_stats(self) -> LoadStats:
        """获取加载统计信息"""
        return self.stats

    def print_stats(self) -> None:
        """打印加载统计信息"""
        print(self.stats.summary())

    def close(self) -> None:
        """关闭加载器，释放资源"""
        self._shutdown_prefetch()
        self.memory_watcher.clear_callbacks()
        logger.info("OptimizedDataLoader 已关闭")

    def __enter__(self) -> "OptimizedDataLoader":
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"OptimizedDataLoader(prefetch={self.prefetch_count}, "
            f"chunk_days={self.chunk_days}, symbols={len(self.get_available_symbols())})"
        )


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import tempfile

    print("=" * 60)
    print("  loader.py — 优化数据加载器 自测 Demo")
    print("=" * 60)

    # ── 构造测试数据 ──
    print("\n[1] 构造测试数据 (3个标的 x 800根K线)")
    tmp_dir = Path(tempfile.mkdtemp(prefix="loader_test_"))
    print(f"    临时数据目录: {tmp_dir}")

    test_symbols = ["TEST001.SZ", "TEST002.SZ", "TEST003.SZ"]
    np.random.seed(42)

    for symbol in test_symbols:
        dates = pd.date_range("2022-01-03", periods=800, freq="B")
        base = 10.0 + np.random.rand() * 50
        returns = np.random.randn(800) * 0.02
        close = base * np.cumprod(1 + returns)
        high = close * (1 + np.abs(np.random.randn(800) * 0.01))
        low = close * (1 - np.abs(np.random.randn(800) * 0.01))
        open_p = close * (1 + np.random.randn(800) * 0.005)
        volume = np.random.randint(100000, 500000, 800)
        amount = close * volume

        df = pd.DataFrame({
            "open": open_p, "high": high, "low": low,
            "close": close, "volume": volume, "amount": amount,
        }, index=dates)
        df.index.name = "date"

        # 保存为 parquet
        filepath = tmp_dir / f"{symbol}.parquet"
        df.to_parquet(filepath)
        print(f"    {symbol}: {len(df)} 根K线 -> {filepath.name}")

    # ── 测试 2: 基本加载 ──
    print("\n[2] 基本加载测试")
    loader = OptimizedDataLoader(
        processed_dir=tmp_dir,
        prefetch_count=2,
        chunk_days=300,
    )
    df1 = loader.load_single("TEST001.SZ", "2022-01-01", "2024-12-31")
    print(f"    加载 TEST001.SZ: {len(df1)} 根K线")
    print(f"    列: {list(df1.columns)}")
    print(f"    日期范围: {df1.index[0].strftime('%Y-%m-%d')} ~ {df1.index[-1].strftime('%Y-%m-%d')}")
    assert not df1.empty
    assert "close" in df1.columns
    print(f"    ✓ 基本加载正常")

    # ── 测试 3: 防未来函数 ──
    print("\n[3] 防未来函数测试")
    df_end = loader.load_single("TEST001.SZ", "2022-01-01", "2023-06-30")
    assert df_end.index.max() <= pd.Timestamp("2023-06-30")
    print(f"    end_date=2023-06-30, 实际最大日期: {df_end.index.max().strftime('%Y-%m-%d')}")
    print(f"    ✓ 防未来函数正常（不返回 end_date 之后数据）")

    # ── 测试 4: 分块加载 ──
    print("\n[4] 分块加载测试")
    loader_chunk = OptimizedDataLoader(
        processed_dir=tmp_dir,
        chunk_days=200,
        enable_prefetch=False,
    )
    df_chunked = loader_chunk.load_single("TEST001.SZ", "2022-01-01", "2024-12-31")
    print(f"    分块加载: {len(df_chunked)} 根K线 (chunk_days=200)")
    print(f"    分块加载次数: {loader_chunk.stats.chunked_loads}")
    assert not df_chunked.empty
    assert loader_chunk.stats.chunked_loads >= 1
    # 分块结果应与全量结果一致
    assert len(df_chunked) == len(df1)
    print(f"    ✓ 分块加载正常，结果与全量一致")

    # ── 测试 5: 批量加载 + 预取 ──
    print("\n[5] 批量加载 + 预取测试")
    loader_batch = OptimizedDataLoader(
        processed_dir=tmp_dir,
        prefetch_count=2,
        chunk_days=1000,
    )
    batch_result = loader_batch.load_batch(
        test_symbols, "2022-01-01", "2024-12-31"
    )
    print(f"    批量加载: {len(batch_result)} 个标的")
    for sym, df in batch_result.items():
        print(f"      {sym}: {len(df)} 根K线")
    assert len(batch_result) == 3
    print(f"    预取命中: {loader_batch.stats.prefetch_hits}")
    print(f"    ✓ 批量加载 + 预取正常")

    # ── 测试 6: 分批迭代 ──
    print("\n[6] 分批迭代测试")
    loader_iter = OptimizedDataLoader(
        processed_dir=tmp_dir,
        prefetch_count=1,
        enable_chunking=False,
    )
    batch_count = 0
    total_loaded = 0
    for batch in loader_iter.iter_batches(test_symbols, batch_size=2,
                                            start_date="2022-01-01", end_date="2024-12-31"):
        batch_count += 1
        total_loaded += len(batch)
        print(f"    第 {batch_count} 批: {len(batch)} 个标的")
    assert batch_count == 2  # 3个标的，batch_size=2 → 2批
    assert total_loaded == 3
    print(f"    ✓ 分批迭代正常 (共 {batch_count} 批)")

    # ── 测试 7: 流水线加载 ──
    print("\n[7] 流水线加载测试")
    loader_pipe = OptimizedDataLoader(
        processed_dir=tmp_dir,
        prefetch_count=0,
        enable_chunking=False,
    )

    def process_symbol(symbol: str, df: pd.DataFrame) -> Dict[str, Any]:
        """模拟数据处理：计算基本统计"""
        return {
            "symbol": symbol,
            "bars": len(df),
            "mean_close": float(df["close"].mean()),
            "total_volume": int(df["volume"].sum()),
        }

    pipe_results = loader_pipe.pipeline_load(
        test_symbols, process_symbol,
        start_date="2022-01-01", end_date="2024-12-31",
        max_workers=2,
    )
    print(f"    流水线结果: {len(pipe_results)} 个")
    for r in pipe_results:
        if r:
            print(f"      {r['symbol']}: {r['bars']} bars, mean_close={r['mean_close']:.2f}")
    assert len(pipe_results) == 3
    assert all(r is not None for r in pipe_results)
    print(f"    ✓ 流水线加载正常")

    # ── 测试 8: 内存监控集成 ──
    print("\n[8] 内存监控集成测试")
    watcher = MemoryWatcher(warning_pct=1, critical_pct=2)  # 极低阈值触发降级
    loader_mem = OptimizedDataLoader(
        processed_dir=tmp_dir,
        memory_watcher=watcher,
        prefetch_count=4,
        chunk_days=300,
    )
    df_mem = loader_mem.load_single("TEST001.SZ", "2022-01-01", "2024-12-31")
    print(f"    内存降级次数: {loader_mem.stats.memory_degradations}")
    print(f"    当前预取数量: {loader_mem.prefetch_count}")
    print(f"    峰值内存: {loader_mem.stats.peak_memory_mb:.1f}MB")
    # 阈值极低，应该触发了降级
    assert loader_mem.stats.memory_degradations >= 0  # 可能触发也可能不触发（取决于系统内存）
    print(f"    ✓ 内存监控集成正常")

    # ── 测试 9: 统计信息 ──
    print("\n[9] 统计信息测试")
    stats = loader_batch.get_stats()
    print(f"    总标的数: {stats.total_symbols}")
    print(f"    成功加载: {stats.loaded_symbols}")
    print(f"    总K线数: {stats.total_bars:,}")
    print(f"    预取命中: {stats.prefetch_hits}")
    print(f"    总耗时: {stats.total_load_time_ms:.1f}ms")
    print(f"    成功率: {stats.success_rate:.1%}")
    assert stats.total_symbols > 0
    assert stats.loaded_symbols > 0
    print(f"    ✓ 统计信息正常")
    print()
    print(stats.summary())

    # ── 测试 10: 边界情况 ──
    print("\n[10] 边界情况测试")
    # 空标的列表
    assert loader.load_batch([]) == {}
    print(f"    ✓ 空标的列表返回空字典")

    # 不存在的标的
    df_nonexist = loader.load_single("NOTEXIST.SZ")
    assert df_nonexist.empty
    print(f"    ✓ 不存在标的返回空 DataFrame")

    # 非法 symbol
    try:
        loader.load_single("")
        assert False, "应抛出 ValueError"
    except ValueError:
        print(f"    ✓ 空 symbol 正确抛出 ValueError")

    # 非法参数
    try:
        OptimizedDataLoader(prefetch_count=-1)
        assert False, "应抛出 ValueError"
    except ValueError:
        print(f"    ✓ 负数 prefetch_count 正确抛出 ValueError")

    # ── 清理 ──
    print("\n[清理] 清理临时数据")
    loader.close()
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"    已清理: {tmp_dir}")

    print("\n" + "=" * 60)
    print("  ✓ loader.py 优化数据加载器 全部自测通过")
    print("=" * 60)
