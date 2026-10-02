# 文件名：myquant/data_access/sources/base.py
"""
base.py - 数据源抽象基类（数据层核心）
======================================
定义所有数据源的统一接口，任何新增数据源必须继承此类并实现抽象方法。

接口设计原则:
  1. 统一返回格式：所有数据源返回相同结构的数据，上层不用关心数据来源
  2. 防未来函数：所有方法支持 end_date 参数，严格不返回未来数据
  3. 为实盘预留：订阅/推送/回调接口，实时行情无缝对接
  4. 健康检查：每个数据源可检测自身健康状态，支持自动降级
  5. 能力声明：每个数据源声明支持的市场/周期/数据类型，组合源按需调度

数据源能力矩阵:
  ┌──────────────┬──────┬────────┬────────┬──────┬────────┐
  │ 数据源       │ 行情 │ 财务   │ 股票池 │ 指数 │ 实时   │
  ├──────────────┼──────┼────────┼────────┼──────┼────────┤
  │ tdx_source   │  ✓   │  ✗    │  ✓    │  ✓   │  部分  │
  │ akshare      │  ✓   │  ✓    │  ✓    │  ✓   │  ✓    │
  │ broker       │  ✓   │  ✗    │  ✗    │  ✗   │  ✓    │
  └──────────────┴──────┴────────┴────────┴──────┴────────┘

依赖: abc, enum, typing, datetime
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from myquant.core.logger import get_logger

logger = get_logger("data_access.sources.base")


# ─────────────────────────────────────────────
# 枚举定义
# ─────────────────────────────────────────────

class DataSourceStatus(Enum):
    """数据源健康状态"""
    UNKNOWN = "unknown"       # 未知（未检查）
    HEALTHY = "healthy"       # 健康（正常可用）
    DEGRADED = "degraded"     # 降级（部分功能可用，如延迟高）
    DOWN = "down"             # 不可用（连接失败/接口异常）


class MarketType(Enum):
    """市场类型"""
    A_STOCK = "A股"           # 沪深A股
    HK_STOCK = "港股"          # 港股
    US_STOCK = "美股"          # 美股
    INDEX = "指数"             # 指数
    FUTURE = "期货"            # 期货


class PeriodType(Enum):
    """数据周期类型"""
    TICK = "tick"              # 逐笔成交
    MINUTE = "minute"          # 分钟线
    DAILY = "daily"            # 日线
    WEEKLY = "weekly"          # 周线
    MONTHLY = "monthly"        # 月线


class AdjustType(Enum):
    """复权类型"""
    NONE = None                # 不复权
    QFQ = "qfq"                # 前复权
    HFQ = "hfq"                # 后复权（预留；当前库内为 raw 不复权原始层，见回写#80）


# ─────────────────────────────────────────────
# 数据结果类
# ─────────────────────────────────────────────

class MarketDataResult:
    """
    行情数据查询结果
    ================
    统一封装行情数据查询结果，包含数据元信息和数据本身。

    Attributes:
        symbol : 股票代码
        market : 市场
        period : 周期
        adjust : 复权方式
        data : 行情数据列表（字典格式，字段与交接文档5.2节一致）
        source : 数据来源标识
        count : 数据条数
        start_date : 起始日期
        end_date : 截止日期
    """

    def __init__(
        self,
        symbol: str,
        data: List[Dict],
        source: str = "",
        market: str = "A股",
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ):
        self.symbol = symbol
        self.market = market
        self.period = period
        self.adjust = adjust
        self.data = data
        self.source = source
        self.count = len(data)
        self.start_date = data[0]["date"] if data else None
        self.end_date = data[-1]["date"] if data else None

    def __repr__(self) -> str:
        return (
            f"MarketDataResult(symbol='{self.symbol}', count={self.count}, "
            f"range={self.start_date}~{self.end_date}, source='{self.source}')"
        )

    def __len__(self) -> int:
        return self.count

    def __bool__(self) -> bool:
        return self.count > 0

    def to_dataframe(self):
        """转换为pandas DataFrame（需要pandas）"""
        import pandas as pd
        if not self.data:
            return pd.DataFrame()
        df = pd.DataFrame(self.data)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)
        return df


# ─────────────────────────────────────────────
# 数据源抽象基类
# ─────────────────────────────────────────────

class DataSourceBase(ABC):
    """
    数据源抽象基类
    ==============
    所有数据源必须继承此类并实现抽象方法。

    【核心接口分类】
    1. 行情数据：get_market_data / get_market_data_batch
    2. 财务数据：get_financial_data
    3. 基础信息：get_stock_list / get_index_list / get_trading_dates
    4. 实时行情：subscribe_realtime / unsubscribe_realtime（实盘用）
    5. 健康管理：health_check / get_status
    6. 能力声明：supported_markets / supported_periods / supports_financial

    【防未来函数约定】
    所有查询方法的 end_date 参数必须严格生效，
    不得返回 end_date 之后的任何数据。子类实现时必须遵守。

    Usage（自定义数据源）:
        class MyDataSource(DataSourceBase):
            @property
            def name(self): return "my_source"
            def get_market_data(self, symbol, ...): ...
            def health_check(self): ...
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        Parameters
        ----------
        config : dict | None
            数据源配置（如API密钥、本地路径、超时时间等）
        """
        self.config = config or {}
        self._status = DataSourceStatus.UNKNOWN
        self._fail_count = 0
        self._last_check_time: Optional[datetime] = None
        self._lock = threading.Lock()
        # 实时行情订阅回调注册表：{symbol: [callback1, callback2]}
        self._realtime_subscribers: Dict[str, List[Callable]] = {}

    # ═══════════════════════════════════════════
    # 基本属性（子类必须实现）
    # ═══════════════════════════════════════════

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称（唯一标识，如 'tdx' / 'akshare' / 'broker_xtp'）"""
        ...

    @property
    def source_type(self) -> str:
        """数据源类型：local / network / broker"""
        return "network"

    @property
    def priority(self) -> int:
        """优先级（数字越小优先级越高，默认100）"""
        return 100

    # ═══════════════════════════════════════════
    # 能力声明（子类可覆盖）
    # ═══════════════════════════════════════════

    @property
    def supported_markets(self) -> Set[str]:
        """支持的市场集合，默认支持A股"""
        return {"A股"}

    @property
    def supported_periods(self) -> Set[str]:
        """支持的周期集合，默认支持日线"""
        return {"daily"}

    @property
    def supports_financial(self) -> bool:
        """是否支持财务数据，默认不支持"""
        return False

    @property
    def supports_realtime(self) -> bool:
        """是否支持实时行情订阅，默认不支持"""
        return False

    @property
    def supports_index(self) -> bool:
        """是否支持指数数据，默认支持"""
        return True

    def supports_market(self, market: str) -> bool:
        """检查是否支持指定市场"""
        return market in self.supported_markets

    def supports_period(self, period: str) -> bool:
        """检查是否支持指定周期"""
        return period in self.supported_periods

    # ═══════════════════════════════════════════
    # 行情数据接口（子类必须实现）
    # ═══════════════════════════════════════════

    @abstractmethod
    def get_market_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ) -> MarketDataResult:
        """
        获取单只股票行情数据

        【防未来函数】end_date 之后的数据绝对不返回

        Parameters
        ----------
        symbol : str
            股票代码，带后缀如 000001.SZ
        start_date : str | None
            开始日期 YYYY-MM-DD
        end_date : str | None
            截止日期 YYYY-MM-DD（防未来函数关键参数）
        period : str
            周期 daily/minute/tick
        adjust : str | None
            复权方式 hfq/qfq/None

        Returns
        -------
        MarketDataResult : 行情数据结果
        """
        ...

    def get_market_data_batch(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ) -> Dict[str, MarketDataResult]:
        """
        批量获取多只股票行情数据（默认逐只调用，子类可优化为批量接口）

        【防未来函数】end_date 之后的数据绝对不返回
        """
        results = {}
        for symbol in symbols:
            try:
                results[symbol] = self.get_market_data(
                    symbol, start_date, end_date, period, adjust
                )
            except Exception as e:
                # 单只失败不影响其他
                results[symbol] = MarketDataResult(symbol, [], source=self.name)
        return results

    # ═══════════════════════════════════════════
    # 财务数据接口（子类可选实现）
    # ═══════════════════════════════════════════

    def get_financial_data(
        self,
        symbol: str,
        report_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取财务数据

        【防未来函数】end_date 之后的报告期数据不返回
        默认不支持，子类覆盖此方法。
        """
        raise NotImplementedError(f"数据源 {self.name} 不支持财务数据")

    # ═══════════════════════════════════════════
    # 基础信息接口
    # ═══════════════════════════════════════════

    @abstractmethod
    def get_stock_list(self, market: str = "A股") -> List[Dict]:
        """
        获取股票列表

        Returns
        -------
        list[dict] : 股票信息列表，每个dict包含：
            symbol, name, market, list_date, industry, area
        """
        ...

    def get_index_list(self, market: str = "A股") -> List[Dict]:
        """
        获取指数列表（默认不支持，子类可覆盖）

        Returns
        -------
        list[dict] : 指数信息列表
        """
        raise NotImplementedError(f"数据源 {self.name} 不支持指数列表")

    def get_trading_dates(
        self,
        start_date: str,
        end_date: str,
        market: str = "A股",
    ) -> List[str]:
        """
        获取交易日列表（P2-① #83：优先查真实交易日历缓存 data/trade_calendar.csv，
        缺失回退区间内工作日近似；子类可覆盖为各自真实日历）

        【防未来函数】end_date 之后的日期不返回
        """
        from datetime import date, timedelta
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

        # 真实交易日历（缓存由 scripts/同步交易日历.py 维护）
        try:
            from pathlib import Path as _Path
            from myquant.utils.trade_calendar import load_trade_calendar

            project_root = _Path(__file__).resolve().parents[3]
            trade_days = load_trade_calendar(project_root)
        except Exception:
            trade_days = None
        if trade_days:
            return [d.isoformat() for d in sorted(trade_days) if start <= d <= end]

        # 回退：区间内工作日近似（原实现）
        dates = []
        current = start
        while current <= end:
            if current.weekday() < 5:  # 周一到周五
                dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        return dates

    # ═══════════════════════════════════════════
    # 实时行情接口（实盘用，子类可选实现）
    # ═══════════════════════════════════════════

    def subscribe_realtime(
        self,
        symbol: str,
        callback: Callable[[Dict], None],
        period: str = "tick",
    ) -> bool:
        """
        订阅实时行情（实盘用）

        Parameters
        ----------
        symbol : str
            股票代码
        callback : callable
            行情回调函数，接收一个dict参数：
            {symbol, price, volume, amount, time, ...}
        period : str
            订阅周期 tick/minute

        Returns
        -------
        bool : 订阅是否成功
        """
        if not self.supports_realtime:
            raise NotImplementedError(f"数据源 {self.name} 不支持实时行情")
        with self._lock:
            if symbol not in self._realtime_subscribers:
                self._realtime_subscribers[symbol] = []
            self._realtime_subscribers[symbol].append(callback)
        return True

    def unsubscribe_realtime(self, symbol: str, callback: Optional[Callable] = None) -> bool:
        """
        取消实时行情订阅

        Parameters
        ----------
        symbol : str
            股票代码
        callback : callable | None
            指定取消的回调函数，None则取消该股票所有订阅

        Returns
        -------
        bool : 取消是否成功
        """
        with self._lock:
            if symbol not in self._realtime_subscribers:
                return False
            if callback is None:
                del self._realtime_subscribers[symbol]
            else:
                self._realtime_subscribers[symbol] = [
                    cb for cb in self._realtime_subscribers[symbol] if cb != callback
                ]
                if not self._realtime_subscribers[symbol]:
                    del self._realtime_subscribers[symbol]
        return True

    def _notify_realtime_subscribers(self, symbol: str, data: Dict) -> None:
        """通知所有订阅者（子类收到实时行情时调用）"""
        with self._lock:
            callbacks = list(self._realtime_subscribers.get(symbol, []))
        for callback in callbacks:
            try:
                callback(data)
            except Exception as e:
                # 回调异常不影响其他订阅者
                logger.debug(f"实时行情回调异常: {e}")

    # ═══════════════════════════════════════════
    # 健康管理接口
    # ═══════════════════════════════════════════

    @abstractmethod
    def health_check(self) -> DataSourceStatus:
        """
        健康检查（检测数据源是否可用）

        Returns
        -------
        DataSourceStatus : 健康状态
        """
        ...

    def get_status(self) -> DataSourceStatus:
        """获取当前状态（不执行检查，返回上次检查结果）"""
        return self._status

    def get_status_info(self) -> Dict:
        """获取详细状态信息"""
        return {
            "name": self.name,
            "type": self.source_type,
            "status": self._status.value,
            "priority": self.priority,
            "fail_count": self._fail_count,
            "last_check_time": self._last_check_time.isoformat() if self._last_check_time else None,
            "supported_markets": list(self.supported_markets),
            "supported_periods": list(self.supported_periods),
            "supports_financial": self.supports_financial,
            "supports_realtime": self.supports_realtime,
        }

    def _update_status(self, status: DataSourceStatus) -> None:
        """更新状态（内部方法）"""
        with self._lock:
            self._status = status
            self._last_check_time = datetime.now()
            if status == DataSourceStatus.HEALTHY:
                self._fail_count = 0
            elif status == DataSourceStatus.DOWN:
                self._fail_count += 1

    def _record_failure(self) -> None:
        """记录一次失败（内部方法，子类请求失败时调用）"""
        with self._lock:
            self._fail_count += 1
            if self._fail_count >= 3:
                self._status = DataSourceStatus.DOWN

    def _record_success(self) -> None:
        """记录一次成功（内部方法，子类请求成功时调用）"""
        with self._lock:
            self._fail_count = 0
            if self._status != DataSourceStatus.HEALTHY:
                self._status = DataSourceStatus.HEALTHY

    # ═══════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════

    def _filter_by_date(
        self,
        data: List[Dict],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        按日期范围过滤数据（防未来函数通用工具）

        【防未来函数】end_date 之后的数据严格过滤掉
        """
        if not data:
            return data
        result = data
        if start_date:
            result = [row for row in result if row.get("date", "") >= start_date]
        if end_date:
            result = [row for row in result if row.get("date", "") <= end_date]
        return result

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name='{self.name}', status={self._status.value})"


# ─────────────────────────────────────────────
# 数据源注册表
# ─────────────────────────────────────────────

# 全局数据源注册表：{name: DataSourceBase实例}
_data_source_registry: Dict[str, DataSourceBase] = {}
_registry_lock = threading.Lock()


def register_data_source(source: DataSourceBase) -> None:
    """
    注册数据源到全局注册表

    Parameters
    ----------
    source : DataSourceBase
        数据源实例
    """
    with _registry_lock:
        _data_source_registry[source.name] = source


def unregister_data_source(name: str) -> None:
    """注销数据源"""
    with _registry_lock:
        _data_source_registry.pop(name, None)


def get_data_source(name: str) -> Optional[DataSourceBase]:
    """按名称获取数据源"""
    with _registry_lock:
        return _data_source_registry.get(name)


def get_all_data_sources() -> List[DataSourceBase]:
    """获取所有已注册的数据源，按优先级排序"""
    with _registry_lock:
        return sorted(_data_source_registry.values(), key=lambda s: s.priority)


def get_healthy_data_sources() -> List[DataSourceBase]:
    """获取所有健康状态的数据源，按优先级排序"""
    return [
        s for s in get_all_data_sources()
        if s.get_status() in (DataSourceStatus.HEALTHY, DataSourceStatus.UNKNOWN)
    ]
