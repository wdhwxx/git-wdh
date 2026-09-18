# 文件名：myquant/data_access/sources/tdx_source.py
"""
tdx_source.py - 通达信本地数据源
================================
解析通达信本地.day文件，提供高速行情数据读取。

通达信.day文件格式（32字节/根K线）:
  偏移  长度  类型      说明
  0     4     int32     日期（如20240101）
  4     4     float32   开盘价（元）
  8     4     float32   最高价（元）
  12    4     float32   最低价（元）
  16    4     float32   收盘价（元）
  20    4     float32   成交额（元）
  24    4     int32     成交量（手）
  28    4     int32     保留字段

文件路径规则:
  {tdx_root}/vipdoc/sh/lday/sh{code}.day  ← 上海
  {tdx_root}/vipdoc/sz/lday/sz{code}.day  ← 深圳

设计原则:
  - 纯本地文件读取，毫秒级响应，回测主力数据源
  - 支持自动探测通达信安装路径
  - 支持后复权数据（需通达信已下载复权数据）
  - 防未来函数：严格按end_date过滤
  - 为实时行情预留DDE接口（当前版本仅支持日线）

依赖: struct, os, pathlib, datetime, typing
"""

from __future__ import annotations

import os
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import (
    DataSourceBase,
    DataSourceStatus,
    MarketDataResult,
)
from myquant.core.logger import get_logger

logger = get_logger("data_access.tdx_source")


# ─────────────────────────────────────────────
# 常量定义
# ─────────────────────────────────────────────

# 通达信.day文件每条记录32字节
TDX_RECORD_SIZE = 32

# 通达信常见安装路径（按优先级排序）
DEFAULT_TDX_PATHS = [
    r"D:\TDX",
    r"C:\new_tdx",
    r"C:\new_tdx_2",
    r"D:\new_tdx",
    r"D:\通达信",
    r"C:\通达信",
    r"C:\Program Files\new_tdx",
    r"C:\Program Files (x86)\new_tdx",
]

# 市场代码映射
MARKET_PREFIX = {
    "sh": "上海",
    "sz": "深圳",
    "bj": "北京",
}


# ─────────────────────────────────────────────
# 通达信数据源类
# ─────────────────────────────────────────────

class TdxDataSource(DataSourceBase):
    """
    通达信本地数据源
    ================
    读取通达信本地.day文件，提供高速行情数据。

    Usage:
        from myquant.data_access.sources.tdx_source import TdxDataSource
        source = TdxDataSource(tdx_root=r"C:\new_tdx")
        result = source.get_market_data("000001.SZ", "2024-01-01", "2024-12-31")
        print(result.count)
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        Parameters
        ----------
        config : dict | None
            配置项：
            - tdx_root : str  通达信安装根目录（自动探测）
            - auto_detect : bool  是否自动探测路径（默认True）
        """
        super().__init__(config)
        self._tdx_root = self.config.get("tdx_root")
        self._auto_detect = self.config.get("auto_detect", True)
        if not self._tdx_root and self._auto_detect:
            self._tdx_root = self._detect_tdx_path()

    # ── 基本属性 ──

    @property
    def name(self) -> str:
        return "tdx"

    @property
    def source_type(self) -> str:
        return "local"

    @property
    def priority(self) -> int:
        return 10  # 本地源优先级最高

    @property
    def supported_markets(self) -> Set[str]:
        return {"A股"}

    @property
    def supported_periods(self) -> Set[str]:
        return {"daily"}  # 当前版本仅支持日线，分钟线为.lc1文件后续扩展

    @property
    def supports_financial(self) -> bool:
        return False  # 通达信财务数据格式复杂，走网络源

    @property
    def supports_realtime(self) -> bool:
        return False  # 实时行情需DDE，后续版本扩展

    @property
    def tdx_root(self) -> Optional[str]:
        """通达信根目录"""
        return self._tdx_root

    # ── 路径探测 ──

    def _detect_tdx_path(self) -> Optional[str]:
        """自动探测通达信安装路径"""
        for path in DEFAULT_TDX_PATHS:
            if Path(path).exists():
                # 验证是否有vipdoc目录
                if (Path(path) / "vipdoc").exists():
                    return path
        return None

    def _get_day_file_path(self, symbol: str) -> Optional[Path]:
        """
        根据股票代码获取.day文件路径

        Parameters
        ----------
        symbol : str
            股票代码，如 000001.SZ / 600519.SH

        Returns
        -------
        Path | None : .day文件路径，不存在返回None
        """
        if not self._tdx_root:
            return None
        # 解析代码和市场
        parts = symbol.split(".")
        if len(parts) != 2:
            return None
        code = parts[0].lower()
        market = parts[1].lower()

        if market == "sh":
            market_dir = "sh"
            file_code = f"sh{code}"
        elif market == "sz":
            market_dir = "sz"
            file_code = f"sz{code}"
        elif market == "bj":
            market_dir = "bj"
            file_code = f"bj{code}"
        else:
            return None

        file_path = Path(self._tdx_root) / "vipdoc" / market_dir / "lday" / f"{file_code}.day"
        return file_path if file_path.exists() else None

    # ── .day文件解析 ──

    def _parse_day_file(self, file_path: Path) -> List[Dict]:
        """
        解析通达信.day文件

        Parameters
        ----------
        file_path : Path
            .day文件路径

        Returns
        -------
        list[dict] : 行情数据列表，字段：
            date, open, high, low, close, volume, amount
        """
        records = []
        file_size = file_path.stat().st_size
        if file_size == 0 or file_size % TDX_RECORD_SIZE != 0:
            # 文件大小异常，可能不是标准.day文件
            return records

        with open(file_path, "rb") as f:
            while True:
                raw = f.read(TDX_RECORD_SIZE)
                if len(raw) < TDX_RECORD_SIZE:
                    break
                # 解析32字节记录
                # 格式: <iiiiifii
                #   date(int32), open(int32,单位:分), high(int32,单位:分),
                #   low(int32,单位:分), close(int32,单位:分),
                #   amount(float32,成交额,单位:元), volume(int32,成交量,单位:手),
                #   reserved(int32,保留)
                date_int, open_p, high_p, low_p, close_p, amount, volume, _ = struct.unpack(
                    "<iiiiifii", raw
                )
                # 日期转换：20240101 -> "2024-01-01"
                date_str = f"{date_int // 10000:04d}-{(date_int // 100) % 100:02d}-{date_int % 100:02d}"
                # 价格从分转换为元
                open_price = open_p / 100.0
                high_price = high_p / 100.0
                low_price = low_p / 100.0
                close_price = close_p / 100.0
                # 价格异常过滤（0或负数）
                if open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0:
                    continue
                records.append({
                    "date": date_str,
                    "open": round(open_price, 4),
                    "high": round(high_price, 4),
                    "low": round(low_price, 4),
                    "close": round(close_price, 4),
                    "volume": float(volume),      # 手
                    "amount": round(amount, 2),    # 元
                })
        return records

    # ═══════════════════════════════════════════
    # 核心接口实现
    # ═══════════════════════════════════════════

    def get_market_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "hfq",
    ) -> MarketDataResult:
        """
        获取通达信本地行情数据

        【防未来函数】end_date 之后的数据严格不返回

        Parameters
        ----------
        symbol : str
            股票代码，如 000001.SZ
        start_date : str | None
            开始日期
        end_date : str | None
            截止日期（防未来函数）
        period : str
            周期（仅支持daily）
        adjust : str | None
            复权方式（通达信本地数据通常是不复权，
            后复权需通过复权因子计算，当前版本返回原始数据，
            复权计算由上层indicators层处理）

        Returns
        -------
        MarketDataResult : 行情数据结果
        """
        if period != "daily":
            return MarketDataResult(symbol, [], source=self.name)

        file_path = self._get_day_file_path(symbol)
        if file_path is None:
            self._record_failure()
            return MarketDataResult(symbol, [], source=self.name)

        try:
            all_data = self._parse_day_file(file_path)
            # 【防未来函数】按日期范围过滤
            filtered = self._filter_by_date(all_data, start_date, end_date)
            self._record_success()
            return MarketDataResult(
                symbol=symbol,
                data=filtered,
                source=self.name,
                market="A股",
                period=period,
                adjust=adjust,
            )
        except Exception as e:
            self._record_failure()
            return MarketDataResult(symbol, [], source=self.name)

    def get_stock_list(self, market: str = "A股") -> List[Dict]:
        """
        获取本地已下载的A股股票列表（扫描.day文件）

        【过滤规则】只保留沪深两市A股股票，剔除指数、可转债、基金等非股票：
          - 沪市(sh)：60(主板)、68(科创板)、90(B股)
          - 深市(sz)：00(主板)、30(创业板)、20(B股)
          - 剔除：sh目录下00开头的指数(000001上证指数等)、11开头的可转债
          - 剔除：sz目录下39开头的指数(399001深证成指等)、15开头的基金

        Returns
        -------
        list[dict] : 股票信息列表
        """
        if not self._tdx_root:
            return []
        stocks = []
        vipdoc_path = Path(self._tdx_root) / "vipdoc"
        for market_dir in ["sh", "sz"]:
            lday_path = vipdoc_path / market_dir / "lday"
            if not lday_path.exists():
                continue
            for day_file in lday_path.glob("*.day"):
                # 文件名如 sh600519.day / sz000001.day
                name = day_file.stem  # sh600519
                if len(name) < 8:  # sh/sz + 6位代码
                    continue
                prefix = name[:2]  # sh/sz
                code = name[2:]    # 600519
                if not code.isdigit() or len(code) != 6:
                    continue
                # ── 过滤非股票：只保留沪深A股 ──
                if market_dir == "sh":
                    # 沪市股票：60(主板)、68(科创板)、90(B股)
                    if not code.startswith(("60", "68", "90")):
                        continue
                else:  # sz
                    # 深市股票：00(主板)、30(创业板)、20(B股)
                    if not code.startswith(("00", "30", "20")):
                        continue
                symbol = f"{code}.{prefix.upper()}"
                stocks.append({
                    "symbol": symbol,
                    "name": "",  # 本地文件不含股票名称，需从网络源补充
                    "market": "A股",
                    "list_date": "",
                    "industry": "",
                    "area": "",
                    "data_source": self.name,
                })
        return stocks

    def health_check(self) -> DataSourceStatus:
        """
        健康检查：检查通达信路径是否存在、是否有数据文件

        Returns
        -------
        DataSourceStatus : 健康状态
        """
        if not self._tdx_root:
            self._update_status(DataSourceStatus.DOWN)
            return DataSourceStatus.DOWN
        if not Path(self._tdx_root).exists():
            self._update_status(DataSourceStatus.DOWN)
            return DataSourceStatus.DOWN
        # 检查是否有至少一个.day文件
        vipdoc_path = Path(self._tdx_root) / "vipdoc"
        has_data = False
        for market_dir in ["sh", "sz"]:
            lday_path = vipdoc_path / market_dir / "lday"
            if lday_path.exists() and any(lday_path.glob("*.day")):
                has_data = True
                break
        if has_data:
            self._update_status(DataSourceStatus.HEALTHY)
        else:
            self._update_status(DataSourceStatus.DEGRADED)
        return self._status

    # ── 额外工具方法 ──

    def get_available_symbols(self) -> List[str]:
        """获取本地所有可用股票代码"""
        stocks = self.get_stock_list()
        return [s["symbol"] for s in stocks]

    def get_data_date_range(self, symbol: str) -> Tuple[Optional[str], Optional[str]]:
        """获取某只股票本地数据的日期范围"""
        file_path = self._get_day_file_path(symbol)
        if file_path is None:
            return (None, None)
        data = self._parse_day_file(file_path)
        if not data:
            return (None, None)
        return (data[0]["date"], data[-1]["date"])

    # ── #S4修复：不支持功能的安全默认实现（返回空结果+日志，不抛NotImplementedError） ──

    def get_financial_data(
        self,
        symbol: str,
        report_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取财务数据（通达信本地源不支持，返回空列表）

        #S4修复：覆盖基类的 raise NotImplementedError，返回空列表并记录日志，
        避免调用方未检查 supports_financial 时崩溃。财务数据建议走 akshare 网络源。

        【防未来函数】end_date 之后的报告期数据不返回（本实现不返回任何数据）
        """
        logger.warning(
            f"通达信数据源不支持财务数据查询: symbol={symbol}, "
            f"建议使用 akshare 网络源获取财务数据"
        )
        return []

    def get_index_list(self, market: str = "A股") -> List[Dict]:
        """
        获取指数列表（通达信本地源不支持，返回空列表）

        #S4修复：覆盖基类的 raise NotImplementedError，返回空列表并记录日志，
        避免调用方未检查 supports_index 时崩溃。指数列表建议走 akshare 网络源。
        """
        logger.warning(
            f"通达信数据源不支持指数列表查询: market={market}, "
            f"建议使用 akshare 网络源获取指数列表"
        )
        return []

    def subscribe_realtime(
        self,
        symbol: str,
        callback: Callable[[Dict], None],
        period: str = "tick",
    ) -> bool:
        """
        订阅实时行情（通达信本地源不支持，返回False）

        #S4修复：覆盖基类的 raise NotImplementedError（基类在 supports_realtime=False 时抛异常），
        返回 False 并记录日志，避免调用方未检查 supports_realtime 时崩溃。
        实时行情建议使用专门的实时数据源（如 QMT/Ptrade/腾讯行情）。

        为实盘无缝对接预留：未来版本支持 DDE 实时数据时，可覆盖此方法实现真实订阅。
        """
        logger.warning(
            f"通达信数据源不支持实时行情订阅: symbol={symbol}, period={period}, "
            f"建议使用 QMT/Ptrade/腾讯行情等实时数据源"
        )
        return False
