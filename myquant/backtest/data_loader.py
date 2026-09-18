# 文件名：myquant/backtest/data_loader.py
"""
data_loader.py - 回测数据加载层（为实盘无缝对接设计）
=====================================================

设计思想（规则2：为实时行情及实盘无缝对接做准备）:
  抽象出 DataProviderBase 统一数据接口，回测和实盘共用同一套引擎代码：
    - HistoricalDataProvider : 回测用，从 datasets/processed/ 加载历史CSV
    - RealtimeDataProvider   : 实盘用，从实时行情feed获取（阶段6实现，TODO）
  BacktestEngine 只依赖 DataProviderBase 抽象接口，不关心数据来源。
  实盘时替换为 RealtimeDataProvider，引擎代码零修改。

防未来函数（规则4）:
  - get_bars() 严格按 end_date 过滤，绝不返回未来数据
  - 加载时校验数据日期范围，超出 end_date 的数据直接丢弃
  - 数据按日期升序排列，确保时序正确

数据格式:
  输入: datasets/processed/<symbol>/bar_*.csv (清洗后标准数据)
  输出: pd.DataFrame，index为日期(datetime)，列为 open/high/low/close/volume/amount

依赖: pandas, pathlib, typing, myquant.core.logger, myquant.utils.time_utils
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd

from myquant.core.logger import get_logger
from myquant.utils.file_utils import get_project_path
from myquant.utils.time_utils import parse_datetime

logger = get_logger("backtest.data_loader")


# ═══════════════════════════════════════════
#  DataProviderBase - 数据提供抽象基类
# ═══════════════════════════════════════════

class DataProviderBase(ABC):
    """
    数据提供抽象基类

    回测和实盘共用此接口，引擎只依赖此抽象，不关心具体数据来源。

    子类必须实现:
      - get_bars(symbol, start_date, end_date, period): 获取K线数据
      - get_available_symbols(): 获取可用标的列表
      - get_date_range(symbol): 获取某标的的数据日期范围

    TODO(阶段6): RealtimeDataProvider 继承此类，从实时行情feed获取数据
    """

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
    ) -> pd.DataFrame:
        """
        获取K线数据

        ★★★ 防未来函数 ★★★
        end_date 之后的数据绝对不返回。

        Parameters
        ----------
        symbol : str
            标的代码，如 000001.SZ
        start_date : str | None
            开始日期 YYYY-MM-DD，None则取数据最早日期
        end_date : str | None
            截止日期 YYYY-MM-DD，None则取数据最新日期
            （防未来函数：严格不返回此日期之后的数据）
        period : str
            K线周期 daily/weekly/minute

        Returns
        -------
        pd.DataFrame : index为日期(datetime)，列为 open/high/low/close/volume/amount
                       无数据时返回空DataFrame
        """
        ...

    @abstractmethod
    def get_available_symbols(self) -> List[str]:
        """获取可用标的列表"""
        ...

    @abstractmethod
    def get_date_range(self, symbol: str) -> Optional[tuple]:
        """
        获取某标的的数据日期范围

        Returns
        -------
        tuple | None : (最早日期, 最晚日期)，无数据返回None
        """
        ...


# ═══════════════════════════════════════════
#  HistoricalDataProvider - 历史数据提供（回测用）
# ═══════════════════════════════════════════

class HistoricalDataProvider(DataProviderBase):
    """
    历史数据提供器（回测用）

    从 datasets/processed/ 目录加载清洗后的CSV数据。

    目录结构:
      datasets/processed/<symbol_safe>/bar_<period>_<adjust>_<timestamp>.csv

    symbol_safe: 标的代码中的 . 替换为 _，如 000001.SZ -> 000001_SZ

    Usage:
        provider = HistoricalDataProvider()
        df = provider.get_bars("000001.SZ", "2024-01-01", "2024-06-30")
        engine = BacktestEngine.from_data_provider(provider, config, strategy)
    """

    def __init__(
        self,
        processed_dir: Optional[Union[str, Path]] = None,
        default_period: str = "daily",
        default_adjust: str = "hfq",
        data_format: str = "parquet",
    ):
        """
        Parameters
        ----------
        processed_dir : str | Path | None
            processed数据目录，None则使用默认 datasets/processed/
        default_period : str
            默认K线周期
        default_adjust : str
            默认复权方式
        data_format : str
            数据格式："parquet"（默认，直接 {symbol}.parquet 文件）
                     或 "csv"（子目录 {symbol_safe}/bar_*.csv 文件，旧格式兼容）
        """
        if processed_dir is None:
            self._processed_dir = get_project_path("datasets", "processed")
        else:
            self._processed_dir = Path(processed_dir)
        self._default_period = default_period
        self._default_adjust = default_adjust
        self._data_format = data_format.lower()

        # 标的数据文件缓存: {symbol: {period: {adjust: filepath}}}
        self._file_cache: Dict[str, Dict[str, Dict[str, Path]]] = {}
        self._scan_directory()

        logger.info(
            f"HistoricalDataProvider 初始化: dir={self._processed_dir}, "
            f"format={self._data_format}, 标的数={len(self._file_cache)}"
        )

    # ── 目录扫描 ──

    def _symbol_to_dir(self, symbol: str) -> str:
        """标的代码转目录名: 000001.SZ -> 000001_SZ"""
        return symbol.replace(".", "_")

    def _scan_directory(self) -> None:
        """
        扫描processed目录，建立标的->文件映射。

        支持两种数据格式：
        1. parquet格式（默认）：直接 {symbol}.parquet 文件，如 000001.SZ.parquet
        2. csv格式（旧格式兼容）：子目录 {symbol_safe}/bar_<period>_<adjust>_<timestamp>.csv
        """
        self._file_cache.clear()
        if not self._processed_dir.exists():
            logger.warning(f"processed目录不存在: {self._processed_dir}")
            return

        # ★ 格式1：直接 parquet/csv 文件（如 000001.SZ.parquet）
        for data_file in sorted(self._processed_dir.iterdir()):
            # 跳过目录和隐藏文件
            if data_file.is_dir() or data_file.name.startswith("."):
                continue
            # 只处理 parquet 和 csv 文件
            suffix = data_file.suffix.lower()
            if suffix not in (".parquet", ".csv"):
                continue

            # 文件名即 symbol（去掉后缀），如 000001.SZ.parquet -> 000001.SZ
            symbol = data_file.stem

            if symbol not in self._file_cache:
                self._file_cache[symbol] = {}
            if self._default_period not in self._file_cache[symbol]:
                self._file_cache[symbol][self._default_period] = {}

            # 直接文件的 adjust 用默认值
            existing = self._file_cache[symbol][self._default_period].get(self._default_adjust)
            if existing is None or data_file.name > existing.name:
                self._file_cache[symbol][self._default_period][self._default_adjust] = data_file

        # ★ 格式2：子目录 + CSV 文件（旧格式兼容，如 000001_SZ/bar_daily_hfq_xxx.csv）
        for symbol_dir in sorted(self._processed_dir.iterdir()):
            # 只处理目录，跳过隐藏目录
            if not symbol_dir.is_dir() or symbol_dir.name.startswith("."):
                continue
            symbol = symbol_dir.name.replace("_", ".")

            if symbol not in self._file_cache:
                self._file_cache[symbol] = {}

            for csv_file in sorted(symbol_dir.glob("bar_*.csv")):
                # 文件名格式: bar_<period>_<adjust>_<timestamp>.csv
                parts = csv_file.stem.split("_")
                if len(parts) >= 3:
                    period = parts[1] if len(parts) > 1 else self._default_period
                    adjust = parts[2] if len(parts) > 2 else self._default_adjust
                else:
                    period = self._default_period
                    adjust = self._default_adjust

                if period not in self._file_cache[symbol]:
                    self._file_cache[symbol][period] = {}
                # 同一period+adjust可能有多个时间戳文件，取最新的
                existing = self._file_cache[symbol][period].get(adjust)
                if existing is None or csv_file.name > existing.name:
                    self._file_cache[symbol][period][adjust] = csv_file

        logger.info(f"扫描完成: {len(self._file_cache)} 个标的 (format={self._data_format})")

    def _find_data_file(
        self,
        symbol: str,
        period: str,
        adjust: Optional[str] = None,
    ) -> Optional[Path]:
        """
        查找标的对应的数据文件

        优先匹配指定period+adjust，找不到则降级匹配。
        """
        if symbol not in self._file_cache:
            return None

        period_files = self._file_cache[symbol].get(period, {})
        if not period_files:
            # 降级：尝试任意period
            for p, pf in self._file_cache[symbol].items():
                if pf:
                    period_files = pf
                    logger.debug(f"{symbol} 未找到period={period}，降级使用period={p}")
                    break

        if not period_files:
            return None

        # 优先指定adjust
        if adjust and adjust in period_files:
            return period_files[adjust]
        # 降级：取第一个可用adjust
        if period_files:
            first_adjust = next(iter(period_files))
            logger.debug(f"{symbol} 未找到adjust={adjust}，降级使用adjust={first_adjust}")
            return period_files[first_adjust]

        return None

    # ── DataProviderBase 接口实现 ──

    def get_bars(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        从processed目录加载K线数据

        ★★★ 防未来函数 ★★★
        - end_date 之后的数据严格过滤掉
        - start_date 之前的数据过滤掉
        - 返回数据按日期升序排列

        Parameters
        ----------
        symbol : str
            标的代码
        start_date : str | None
            开始日期 YYYY-MM-DD
        end_date : str | None
            截止日期 YYYY-MM-DD（防未来函数关键参数）
        period : str
            K线周期
        adjust : str | None
            复权方式（hfq/qfq/none）；None 使用构造默认；找不到对应文件时自动降级。

        Returns
        -------
        pd.DataFrame : index=日期(datetime), columns=[open, high, low, close, volume, amount]
        """
        filepath = self._find_data_file(symbol, period, adjust or self._default_adjust)
        if filepath is None:
            logger.warning(f"未找到 {symbol} 的processed数据文件")
            return pd.DataFrame()

        try:
            # ★ 根据文件后缀选择读取方式（parquet 或 csv）
            if filepath.suffix.lower() == ".parquet":
                df = pd.read_parquet(filepath)
            else:
                df = pd.read_csv(filepath)
        except Exception as e:
            logger.error(f"读取 {symbol} 数据文件失败: {filepath}, 错误: {e}")
            return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()

        # ★ 日期处理：parquet 文件的 index 已经是日期（DatetimeIndex），
        #   csv 文件需要找日期列并设为 index
        if isinstance(df.index, pd.DatetimeIndex):
            # parquet 格式：index 已经是日期，确保排序
            df = df.sort_index()
        else:
            # csv 格式：确定日期列名（兼容 date / trade_date / datetime / timestamp）
            date_col = None
            for col in ("date", "trade_date", "datetime", "timestamp"):
                if col in df.columns:
                    date_col = col
                    break

            if date_col is None:
                logger.error(f"{symbol} 数据文件缺少日期列: {list(df.columns)}")
                return pd.DataFrame()

            # 解析日期并设为index
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
            df = df.dropna(subset=[date_col])
            df.set_index(date_col, inplace=True)
            df.sort_index(inplace=True)

        # ★★★ 防未来函数：严格按日期范围过滤 ★★★
        if start_date:
            try:
                start_dt = parse_datetime(start_date)
                df = df[df.index >= pd.Timestamp(start_dt)]
            except (ValueError, TypeError):
                logger.warning(f"start_date格式无效: {start_date}，不过滤开始日期")

        if end_date:
            try:
                end_dt = parse_datetime(end_date)
                # 包含end_date当天，严格不返回之后的数据
                df = df[df.index <= pd.Timestamp(end_dt)]
            except (ValueError, TypeError):
                logger.warning(f"end_date格式无效: {end_date}，不过滤截止日期")

        # 标准化列名（确保包含核心OHLCV列）
        required_cols = ["open", "high", "low", "close", "volume"]
        for col in required_cols:
            if col not in df.columns:
                logger.warning(f"{symbol} 数据缺少列: {col}")

        # 数值类型转换
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 去除内部标记列（清洗时添加的_warnings等）
        internal_cols = [c for c in df.columns if c.startswith("_")]
        if internal_cols:
            df = df.drop(columns=internal_cols)

        if not df.empty:
            logger.info(
                f"加载 {symbol}: {len(df)} 根K线, "
                f"范围={df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}"
            )
        else:
            logger.warning(f"{symbol} 过滤后无数据")

        return df

    def get_available_symbols(self) -> List[str]:
        """获取所有可用标的列表"""
        return sorted(self._file_cache.keys())

    def get_date_range(self, symbol: str) -> Optional[tuple]:
        """
        获取某标的的数据日期范围（不过滤，返回完整范围）

        Returns
        -------
        tuple | None : (最早日期str, 最晚日期str)
        """
        df = self.get_bars(symbol)  # 不传日期范围，获取全部
        if df.empty:
            return None
        return (
            df.index[0].strftime("%Y-%m-%d"),
            df.index[-1].strftime("%Y-%m-%d"),
        )

    def load_multiple(
        self,
        symbols: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = None,
    ) -> Dict[str, pd.DataFrame]:
        """
        批量加载多标的数据

        ★★★ 防未来函数 ★★★
        所有标的统一使用相同的 end_date，确保不返回未来数据。

        Parameters
        ----------
        symbols : list[str]
            标的代码列表
        start_date : str | None
            开始日期
        end_date : str | None
            截止日期（防未来函数）
        period : str
            K线周期
        adjust : str | None
            复权方式（hfq/qfq/none）；None 使用构造默认；找不到对应文件时自动降级。

        Returns
        -------
        dict : {symbol: DataFrame}，无数据的标的不包含在结果中
        """
        result = {}
        for symbol in symbols:
            df = self.get_bars(symbol, start_date, end_date, period, adjust)
            if not df.empty:
                result[symbol] = df
            else:
                logger.warning(f"跳过 {symbol}: 无有效数据")
        return result

    def refresh(self) -> None:
        """重新扫描目录（数据更新后调用）"""
        self._scan_directory()
        logger.info("HistoricalDataProvider 已刷新")

    def __repr__(self) -> str:
        return (
            f"HistoricalDataProvider(dir={self._processed_dir}, "
            f"symbols={len(self._file_cache)})"
        )


# ═══════════════════════════════════════════
#  RealtimeDataProvider - 实时数据提供（实盘用，TODO阶段6）
# ═══════════════════════════════════════════

# TODO(阶段6): 实现 RealtimeDataProvider，继承 DataProviderBase
#   - 从 live/data_feed.py 订阅实时行情
#   - get_bars() 返回已收盘的历史bar + 当前正在形成的bar
#   - 实盘时替换 HistoricalDataProvider，BacktestEngine 零修改
#
# class RealtimeDataProvider(DataProviderBase):
#     """实时数据提供器（实盘用，阶段6实现）"""
#     def __init__(self, data_feed):
#         self._data_feed = data_feed
#         self._history: Dict[str, pd.DataFrame] = {}
#
#     def get_bars(self, symbol, start_date=None, end_date=None, period="daily"):
#         # 从实时feed获取已收盘bar + 当前bar
#         ...
#
#     def get_available_symbols(self):
#         return list(self._data_feed.subscribed_symbols)
#
#     def get_date_range(self, symbol):
#         ...
