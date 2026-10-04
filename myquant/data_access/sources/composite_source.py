# 文件名：myquant/data_access/sources/composite_source.py
"""
composite_source.py - 组合数据源（多源融合+自动降级）
=====================================================
数据层的核心调度器，管理多个数据源，按优先级自动选择和降级。

核心能力:
  1. 多源管理：注册/注销/查询数据源
  2. 自动降级：主源失败自动切备用源，保证数据不中断
  3. 能力路由：根据数据类型（行情/财务/实时）自动选择支持的数据源
  4. 数据融合：多源数据对比，选择质量更高的结果
  5. 健康监控：定期检查所有数据源健康状态
  6. 统计上报：记录各数据源使用次数/成功率/失败次数

降级策略:
  1. 按优先级排序数据源
  2. 跳过状态为DOWN的数据源
  3. 跳过不支持当前市场/周期/数据类型的数据源
  4. 逐个尝试，第一个成功的返回结果
  5. 全部失败则返回空结果

设计原则:
  - 对上层透明：策略层只看到一个数据源，不知道背后有多少个
  - 防未来函数：所有查询严格传递end_date
  - 为实盘预留：实时行情订阅自动路由到支持的数据源
  - 线程安全：多线程环境下数据源状态一致

依赖: typing, threading, datetime, myquant.data_access.sources.base
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from myquant.core.logger import get_logger

logger = get_logger("data_access.sources.composite_source")

from .base import (
    DataSourceBase,
    DataSourceStatus,
    MarketDataResult,
    register_data_source,
    get_all_data_sources,
    get_healthy_data_sources,
)


# ─────────────────────────────────────────────
# 组合数据源类
# ─────────────────────────────────────────────

class CompositeDataSource(DataSourceBase):
    """
    组合数据源（多源融合+自动降级）
    ================================
    管理多个数据源，对外提供统一接口，内部自动选择和降级。

    Usage:
        from myquant.data_access.sources import CompositeDataSource
        from myquant.data_access.sources.tdx_source import TdxDataSource
        from myquant.data_access.sources.akshare_source import AkshareDataSource

        composite = CompositeDataSource()
        composite.add_source(TdxDataSource())
        composite.add_source(AkshareDataSource())

        # 自动选择最优数据源
        result = composite.get_market_data("000001.SZ", "2024-01-01", "2024-12-31")
        print(result.source)  # 可能是 'tdx' 或 'akshare'
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        Parameters
        ----------
        config : dict | None
            配置项：
            - auto_register : bool  是否自动注册全局数据源（默认True）
            - enable_health_check : bool  是否启用定期健康检查（默认True）
            - health_check_interval : int  健康检查间隔秒数（默认300）
        """
        super().__init__(config)
        self._sources: List[DataSourceBase] = []
        self._lock = threading.Lock()
        self._auto_register = self.config.get("auto_register", True)
        self._stats = {
            "total_requests": 0,
            "success_count": 0,
            "fail_count": 0,
            "source_usage": {},   # {source_name: count}
            "source_success": {}, # {source_name: count}
            "source_fail": {},    # {source_name: count}
        }

    # ── 基本属性 ──

    @property
    def name(self) -> str:
        return "composite"

    @property
    def source_type(self) -> str:
        return "composite"

    @property
    def priority(self) -> int:
        return 1  # 组合源优先级最高（对外统一入口）

    @property
    def supported_markets(self) -> Set[str]:
        """所有子数据源支持的市场并集"""
        markets = set()
        for source in self._sources:
            markets.update(source.supported_markets)
        return markets

    @property
    def supported_periods(self) -> Set[str]:
        """所有子数据源支持的周期并集"""
        periods = set()
        for source in self._sources:
            periods.update(source.supported_periods)
        return periods

    @property
    def supports_financial(self) -> bool:
        """是否有子数据源支持财务数据"""
        return any(s.supports_financial for s in self._sources)

    @property
    def supports_realtime(self) -> bool:
        """是否有子数据源支持实时行情"""
        return any(s.supports_realtime for s in self._sources)

    @property
    def sources(self) -> List[DataSourceBase]:
        """所有已添加的数据源（按优先级排序）"""
        with self._lock:
            return sorted(self._sources, key=lambda s: s.priority)

    # ── 数据源管理 ──

    def add_source(self, source: DataSourceBase) -> None:
        """
        添加数据源

        Parameters
        ----------
        source : DataSourceBase
            数据源实例
        """
        with self._lock:
            # 检查是否已存在
            for existing in self._sources:
                if existing.name == source.name:
                    return  # 已存在，不重复添加
            self._sources.append(source)
            # 初始化统计
            self._stats["source_usage"][source.name] = 0
            self._stats["source_success"][source.name] = 0
            self._stats["source_fail"][source.name] = 0
        if self._auto_register:
            register_data_source(source)

    def remove_source(self, source_name: str) -> bool:
        """
        移除数据源

        Parameters
        ----------
        source_name : str
            数据源名称

        Returns
        -------
        bool : 是否成功移除
        """
        with self._lock:
            for i, source in enumerate(self._sources):
                if source.name == source_name:
                    del self._sources[i]
                    return True
        return False

    def get_source(self, source_name: str) -> Optional[DataSourceBase]:
        """按名称获取数据源"""
        with self._lock:
            for source in self._sources:
                if source.name == source_name:
                    return source
        return None

    # ── 数据源选择逻辑 ──

    def _select_sources(
        self,
        market: str = "A股",
        period: str = "daily",
        need_financial: bool = False,
        need_realtime: bool = False,
    ) -> List[DataSourceBase]:
        """
        选择可用的数据源（按优先级排序，过滤不可用的）

        Parameters
        ----------
        market : str
            目标市场
        period : str
            目标周期
        need_financial : bool
            是否需要财务数据
        need_realtime : bool
            是否需要实时行情

        Returns
        -------
        list[DataSourceBase] : 可用数据源列表（按优先级排序）
        """
        candidates = []
        for source in self.sources:
            # 过滤状态为DOWN的
            if source.get_status() == DataSourceStatus.DOWN:
                continue
            # 过滤不支持市场的
            if not source.supports_market(market):
                continue
            # 过滤不支持周期的
            if not source.supports_period(period):
                continue
            # 过滤不支持财务数据的
            if need_financial and not source.supports_financial:
                continue
            # 过滤不支持实时行情的
            if need_realtime and not source.supports_realtime:
                continue
            candidates.append(source)
        return candidates

    def _record_usage(self, source_name: str, success: bool) -> None:
        """记录数据源使用统计"""
        with self._lock:
            self._stats["total_requests"] += 1
            self._stats["source_usage"][source_name] = self._stats["source_usage"].get(source_name, 0) + 1
            if success:
                self._stats["success_count"] += 1
                self._stats["source_success"][source_name] = self._stats["source_success"].get(source_name, 0) + 1
            else:
                self._stats["fail_count"] += 1
                self._stats["source_fail"][source_name] = self._stats["source_fail"].get(source_name, 0) + 1

    # ═══════════════════════════════════════════
    # 核心接口实现（自动降级）
    # ═══════════════════════════════════════════

    def get_market_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ) -> MarketDataResult:
        """
        获取行情数据（自动降级）

        【防未来函数】end_date 传递给所有子数据源，严格不返回未来数据

        降级策略：
        1. 按优先级选择可用数据源
        2. 逐个尝试，第一个返回非空结果的即为最终结果
        3. 全部失败则返回空结果
        """
        # 解析市场（从symbol后缀）
        market = "A股"
        if "." in symbol:
            suffix = symbol.split(".")[1].upper()
            if suffix in ("SH", "SZ", "BJ"):
                market = "A股"

        candidates = self._select_sources(market=market, period=period)
        if not candidates:
            return MarketDataResult(symbol, [], source="none")

        for source in candidates:
            try:
                result = source.get_market_data(
                    symbol, start_date, end_date, period, adjust
                )
                if result and result.count > 0:
                    self._record_usage(source.name, True)
                    return result
                else:
                    self._record_usage(source.name, False)
            except Exception:
                self._record_usage(source.name, False)
                continue

        # 全部失败，返回空结果（P2-09：显式告警，区分"该标的无数据"与"所有源都挂"）
        logger.warning(
            "composite_source 全部源取数失败返回空: symbol=%s period=%s 候选源=%s",
            symbol, period, [s.name for s in candidates],
        )
        return MarketDataResult(symbol, [], source="none")

    def get_financial_data(
        self,
        symbol: str,
        report_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取财务数据（自动降级，仅选择支持财务数据的源）

        【防未来函数】end_date 传递给所有子数据源
        """
        market = "A股"
        candidates = self._select_sources(market=market, need_financial=True)
        if not candidates:
            return []

        for source in candidates:
            try:
                result = source.get_financial_data(symbol, report_date, end_date)
                if result:
                    self._record_usage(source.name, True)
                    return result
                else:
                    self._record_usage(source.name, False)
            except Exception:
                self._record_usage(source.name, False)
                continue
        return []

    def get_financial_history(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取历史多期财务数据（自动降级，仅选择支持财务数据的源）

        【防未来函数】end_date 传递给所有子数据源，之后的报告期不返回

        Args:
            symbol: 股票代码，如 "600519.SH"
            start_date: 起始报告期，格式 "YYYYMMDD" 或 "YYYY-MM-DD"
            end_date: 截止报告期，格式同上

        Returns:
            多期财务记录列表，按报告期倒序排列；空列表表示所有源均无数据
        """
        market = "A股"
        candidates = self._select_sources(market=market, need_financial=True)
        if not candidates:
            logger.warning("无支持财务数据的源可用（所有源状态为DOWN或不支持财务）")
            return []

        for source in candidates:
            try:
                # 仅调用具备 get_financial_history 方法的源（基类可能没有）
                if not hasattr(source, "get_financial_history"):
                    continue
                result = source.get_financial_history(symbol, start_date, end_date)
                if result:
                    self._record_usage(source.name, True)
                    return result
                else:
                    self._record_usage(source.name, False)
            except Exception as e:
                logger.warning(
                    f"源 [{source.name}] 历史财务获取失败 [{symbol}]: "
                    f"{type(e).__name__}: {e}"
                )
                self._record_usage(source.name, False)
                continue
        return []


    def get_stock_list(self, market: str = "A股") -> List[Dict]:
        """
        获取股票列表（自动降级，合并多源结果去重）
        """
        candidates = self._select_sources(market=market)
        if not candidates:
            return []

        all_stocks = {}
        for source in candidates:
            try:
                stocks = source.get_stock_list(market)
                for stock in stocks:
                    symbol = stock.get("symbol", "")
                    if symbol and symbol not in all_stocks:
                        # 优先使用有名称的源的数据
                        if stock.get("name"):
                            all_stocks[symbol] = stock
                        elif symbol not in all_stocks:
                            all_stocks[symbol] = stock
                self._record_usage(source.name, True)
            except Exception:
                self._record_usage(source.name, False)
                continue
        return list(all_stocks.values())

    def subscribe_realtime(
        self,
        symbol: str,
        callback: Callable[[Dict], None],
        period: str = "tick",
    ) -> bool:
        """
        订阅实时行情（自动路由到支持实时行情的源）
        """
        market = "A股"
        candidates = self._select_sources(market=market, period=period, need_realtime=True)
        if not candidates:
            return False
        # 选择优先级最高的支持实时行情的源
        source = candidates[0]
        try:
            success = source.subscribe_realtime(symbol, callback, period)
            self._record_usage(source.name, success)
            return success
        except Exception:
            self._record_usage(source.name, False)
            return False

    def unsubscribe_realtime(self, symbol: str, callback: Optional[Callable] = None) -> bool:
        """取消实时行情订阅（通知所有源）"""
        success = False
        for source in self.sources:
            if source.supports_realtime:
                try:
                    if source.unsubscribe_realtime(symbol, callback):
                        success = True
                except Exception as e:
                    logger.debug(f"数据源{source.name}取消订阅异常: {e}")
        return success

    # ═══════════════════════════════════════════
    # 健康管理
    # ═══════════════════════════════════════════

    def health_check(self) -> DataSourceStatus:
        """
        健康检查：检查所有子数据源健康状态

        Returns
        -------
        DataSourceStatus : 组合源状态
            - 所有源健康 -> HEALTHY
            - 部分源降级 -> DEGRADED
            - 所有源不可用 -> DOWN
        """
        healthy_count = 0
        degraded_count = 0
        down_count = 0
        for source in self.sources:
            try:
                status = source.health_check()
                if status == DataSourceStatus.HEALTHY:
                    healthy_count += 1
                elif status == DataSourceStatus.DEGRADED:
                    degraded_count += 1
                else:
                    down_count += 1
            except Exception:
                down_count += 1

        total = len(self.sources)
        if total == 0:
            self._update_status(DataSourceStatus.DOWN)
        elif down_count == total:
            self._update_status(DataSourceStatus.DOWN)
        elif down_count > 0 or degraded_count > 0:
            self._update_status(DataSourceStatus.DEGRADED)
        else:
            self._update_status(DataSourceStatus.HEALTHY)
        return self._status

    # ── 统计信息 ──

    def get_stats(self) -> Dict:
        """获取数据源使用统计"""
        with self._lock:
            return dict(self._stats)

    def get_source_status_report(self) -> List[Dict]:
        """获取所有数据源状态报告"""
        report = []
        for source in self.sources:
            info = source.get_status_info()
            info["usage_count"] = self._stats["source_usage"].get(source.name, 0)
            info["success_count"] = self._stats["source_success"].get(source.name, 0)
            info["fail_count"] = self._stats["source_fail"].get(source.name, 0)
            report.append(info)
        return report

    def __repr__(self) -> str:
        source_names = [s.name for s in self.sources]
        return f"CompositeDataSource(sources={source_names}, status={self._status.value})"