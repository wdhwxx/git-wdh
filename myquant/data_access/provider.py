# 文件名：myquant/data_access/provider.py
"""
provider.py - 历史数据提供层（数据访问层归属，为回测/实盘共用）
================================================================

本模块原位于 myquant/backtest/data_loader.py，2026-10-03 架构治理下沉至
myquant/data_access/provider.py，以打断 data_access <-> backtest 循环依赖：
  - 搬移前：data_access/loader.py 顶层 import backtest.data_loader.HistoricalDataProvider
            且 backtest/data_loader.py 懒加载 data_access/factor_sync_service.FactorSyncService
            形成 data_access -> backtest -> data_access 环。
  - 搬移后：HistoricalDataProvider 归位数据层，backtest/data_loader.py 变为 re-export
            兼容薄壳，原有 18 处 from myquant.backtest.data_loader import ... 零改动。

设计思想（规则2：为实时行情及实盘无缝对接做准备）:
  抽象出 DataProviderBase 统一数据接口，回测和实盘共用同一套引擎代码：
    - HistoricalDataProvider : 回测用，从 datasets/processed/ 加载历史 parquet/csv
    - RealtimeDataProvider   : 实盘用，从实时行情feed获取（阶段6实现，TODO）
  BacktestEngine / LiveEngine 只依赖 DataProviderBase 抽象接口，不关心数据来源。
  实盘时替换为 RealtimeDataProvider，引擎代码零修改。

防未来函数（规则4）:
  - get_bars() 严格按 end_date 过滤，绝不返回未来数据
  - 加载时校验数据日期范围，超出 end_date 的数据直接丢弃
  - 数据按日期升序排列，确保时序正确

数据格式:
  输入: datasets/processed/<symbol>.parquet （默认扁平 parquet）
        或 datasets/processed/<symbol_safe>/bar_*.csv（旧格式兼容）
  输出: pd.DataFrame，index为日期(datetime，名为"日期")，列为中文表头
        （开盘价/最高价/最低价/收盘价/成交量/成交额 等）

依赖: pandas, pathlib, typing, myquant.core.logger,
      myquant.utils.file_utils, myquant.utils.time_utils,
      myquant.data_access.factor_sync_service（复权因子服务，同包懒加载）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd

from myquant.core.logger import get_logger
from myquant.utils.file_utils import get_project_path
from myquant.utils.time_utils import parse_datetime

logger = get_logger("data_access.provider")


#: 英文列名 → 中文列名映射（引擎内部统一使用中文表头）
#: processed 导出脚本已将表头改为全中文；此处对来自数据库/旧英文表头的数据统一兜底转中文
EN_TO_CN_COLUMN_MAP: Dict[str, str] = {
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
    "amount": "成交额",
    "pct_chg": "涨跌幅",
    "amplitude": "振幅",
    "volume_ratio": "量比",
    "float_share": "流通股本",
    "pe_dynamic": "市盈动",
    "turnover": "换手率",
    "pre_close": "前收盘价",
}

#: 中文日期索引名（导出脚本与内部统一使用）
CN_INDEX_NAME: str = "日期"

#: 核心 OHLCV 中文列（#38 中文列改造后的入口校验主口径）
REQUIRED_CN_OHLCV_COLUMNS: Tuple[str, ...] = (
    "开盘价", "最高价", "最低价", "收盘价", "成交量",
)
#: 对应英文列（兼容旧缓存 parquet / 旧英文表头数据源，引擎侧会做 EN→CN 转换）
REQUIRED_EN_OHLCV_COLUMNS: Tuple[str, ...] = (
    "open", "high", "low", "close", "volume",
)


def require_ohlcv(df: pd.DataFrame, symbol: str = "") -> None:
    """校验 DataFrame 包含核心 OHLCV 列（中文列优先，兼容英文列）。

    #38 中文列改造后，回测入口统一以中文列为主校验口径；
    英文列作为旧缓存/旧数据源的兼容形态保留（引擎 get_bars 已做 EN→CN 转换）。

    Args:
        df: 待校验的行情 DataFrame。
        symbol: 标的代码（仅用于错误信息定位，可空）。

    Raises:
        ValueError: 中英文 OHLCV 列均不完整时抛出。
    """
    cols = list(df.columns)
    if all(c in cols for c in REQUIRED_CN_OHLCV_COLUMNS):
        return
    if all(c in cols for c in REQUIRED_EN_OHLCV_COLUMNS):
        return
    prefix = f"标的 {symbol} " if symbol else ""
    raise ValueError(
        f"{prefix}行情缺少 OHLCV 列（需中文列 {REQUIRED_CN_OHLCV_COLUMNS} "
        f"或英文列 {REQUIRED_EN_OHLCV_COLUMNS}），实际列: {cols}"
    )


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
        获取某标的的数据日期范围（不过滤，返回完整范围）

        Returns
        -------
        tuple | None : (最早日期str, 最晚日期str)，无数据返回None
        """
        ...


# ═══════════════════════════════════════════
#  HistoricalDataProvider - 历史数据提供（回测用）
# ═══════════════════════════════════════════

class HistoricalDataProvider(DataProviderBase):
    """
    历史数据提供器（回测用）

    从 datasets/processed/ 目录加载清洗后的 parquet/csv 数据。

    目录结构:
      扁平 parquet（默认）: datasets/processed/<symbol>.parquet，如 000001.SZ.parquet
      旧 CSV 子目录:      datasets/processed/<symbol_safe>/bar_<period>_<adjust>_<timestamp>.csv
                          symbol_safe: 标的代码中的 . 替换为 _，如 000001.SZ -> 000001_SZ

    Usage:
        >>> provider = HistoricalDataProvider()
        >>> df = provider.get_bars("000001.SZ", "2024-01-01", "2024-06-30")
        >>> engine = BacktestEngine.from_data_provider(provider, config, strategy)
    """

    def __init__(
        self,
        processed_dir: Optional[Union[str, Path]] = None,
        default_period: str = "daily",
        default_adjust: str = "raw",
        data_format: str = "parquet",
        factor_svc: Optional[Any] = None,
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
        factor_svc : FactorSyncService | None
            复权因子服务（方案B #82，hfq/qfq 现算用）；None 则懒加载默认 DB 服务
        """
        if processed_dir is None:
            self._processed_dir = get_project_path("datasets", "processed")
        else:
            self._processed_dir = Path(processed_dir)
        self._default_period = default_period
        self._default_adjust = default_adjust
        self._data_format = data_format.lower()
        self._factor_svc = factor_svc

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
                    adjust = self._default_period

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
        adjust_strict: bool = False,
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
        pd.DataFrame : index=日期(datetime), columns=[开盘价,最高价,最低价,收盘价,成交量,成交额]
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

        # ★★★ 列名统一为中文：回测引擎内部全链路使用中文表头 ★★★
        # 若存在英文列名（数据库/旧英文表头），统一转为中文；中文表头文件不受影响
        rename_map = {en: cn for en, cn in EN_TO_CN_COLUMN_MAP.items() if en in df.columns}
        if rename_map:
            df = df.rename(columns=rename_map)
        if df.index.name in (None, "date", "trade_date"):
            df.index.name = CN_INDEX_NAME

        # ★ 日期处理：parquet 文件的 index 已经是日期（DatetimeIndex），
        #   csv 文件需要找日期列并设为 index
        if isinstance(df.index, pd.DatetimeIndex):
            # parquet 格式：index 已经是日期，确保排序
            df = df.sort_index()
        else:
            # csv 格式：确定日期列名（兼容 date / trade_date / datetime / timestamp）
            date_col = None
            for col in ("日期", "date", "trade_date", "datetime", "timestamp"):
                if col in df.columns:
                    date_col = col
                    break

            if date_col is None:
                logger.error(f"{symbol} 数据文件缺少日期列: {list(df.columns)}")
                return pd.DataFrame()

            # 解析日期并设为index（兼容整数YYYYMMDD和字符串两种格式）
            if pd.api.types.is_integer_dtype(df[date_col]):
                df[date_col] = pd.to_datetime(df[date_col].astype(str), format='%Y%m%d', errors="coerce")
            else:
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

        # 标准化列名（确保包含核心OHLCV列，中文表头）
        required_cols = ["开盘价", "最高价", "最低价", "收盘价", "成交量"]
        for col in required_cols:
            if col not in df.columns:
                logger.warning(f"{symbol} 数据缺少列: {col}")

        # 数值类型转换（中文表头）
        for col in ("开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # KP-P2-10/T-02：前收盘价兜底派生——processed/旧缓存未含该列时按收盘价.shift(1) 派生，
        # 防混合/事件引擎涨跌停校验因 pre_close 恒 0 而空转（首行无前驱为 NaN，由撮合层跳过）。
        if "前收盘价" not in df.columns and "收盘价" in df.columns:
            df["前收盘价"] = pd.to_numeric(df["收盘价"], errors="coerce").shift(1)

        # 去除内部标记列（清洗时添加的_warnings等）
        internal_cols = [c for c in df.columns if c.startswith("_")]
        if internal_cols:
            df = df.drop(columns=internal_cols)

        # ★ 复权现算（方案B #82）：processed 只存 raw，hfq/qfq 按因子表内存变换
        # KP-P2-09：adjust_strict 时因子缺失即 raise（不静默降级）；df.attrs 记实际生效复权
        requested_adjust = adjust or self._default_adjust
        if requested_adjust in ("hfq", "qfq") and not df.empty:
            df = self._apply_factor_adjust(
                df, symbol, requested_adjust, end_date, adjust_strict=adjust_strict
            )
        df.attrs.setdefault("adjust_effective", requested_adjust)

        if not df.empty:
            logger.info(
                f"加载 {symbol}: {len(df)} 根K线, "
                f"范围={df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}"
            )
        else:
            logger.warning(f"{symbol} 过滤后无数据")

        return df

    # ── 复权现算（方案B #82） ──

    def _ensure_factor_svc(self) -> None:
        """懒加载复权因子服务（DB 不存在时静默降级，保持 None）。

        项目根用 __file__ 推导（parents[2] = 项目根），不依赖 PROJECT_ROOT 探测
        （marker 缺失时可能解析到错误目录，2026-10-01 实测）。
        注：本文件原位于 myquant/backtest/data_loader.py，2026-10-03 下沉至
        myquant/data_access/provider.py，两层级深度一致（均为 myquant/<sub>/file.py），
        parents[2] 仍指向项目根，路径推导不变。
        """
        if self._factor_svc is not None:
            return
        from myquant.data_access.factor_sync_service import FactorSyncService

        project_root = Path(__file__).resolve().parents[2]
        db_path = project_root / "database" / "quant.db"
        if db_path.exists():
            try:
                self._factor_svc = FactorSyncService(project_root, db_path=db_path)
            except Exception as e:
                logger.error(f"初始化复权因子服务失败: {type(e).__name__}: {e}")
                self._factor_svc = None

    def _get_factor_lookup(
        self,
        symbol: str,
        as_of: Optional[str] = None,
    ) -> Dict[str, float]:
        """读取标的后复权因子 {event_date: factor}（<= as_of；防未来函数）。"""
        self._ensure_factor_svc()
        if self._factor_svc is None:
            return {}
        try:
            return self._factor_svc.get_factor_lookup(symbol, as_of=as_of)
        except Exception as e:
            logger.error(f"读取复权因子失败[{symbol}]: {type(e).__name__}: {e}")
            return {}

    def _apply_factor_adjust(
        self,
        df: pd.DataFrame,
        symbol: str,
        adjust: str,
        end_date: Optional[str],
        adjust_strict: bool = False,
    ) -> pd.DataFrame:
        """按复权因子表现算 hfq/qfq 价（processed 只存 raw，消费端内存变换）。

        - hfq价 = raw价 × factor（事件日及之后生效）
        - qfq价 = hfq价 / 最新因子（最新因子按 end_date 截断，防未来函数）
        - 标的无因子事件时降级返回 raw 并告警（不阻断回测）；KP-P2-09：adjust_strict 时 raise
        """
        factors = self._get_factor_lookup(symbol, as_of=end_date)
        if not factors:
            if adjust_strict:
                raise ValueError(
                    f"{symbol} 无复权因子事件，adjust={adjust} 且 adjust_strict=True，拒绝静默降级 raw"
                )
            logger.warning(f"{symbol} 无复权因子事件，adjust={adjust} 降级为不复权 raw")
            df = df.copy()
            df.attrs["adjust_effective"] = "raw"
            return df
        df = df.copy()
        df.attrs["adjust_effective"] = adjust

        factor_series = pd.Series(factors)  # index 为 str 日期
        factor_series.index = pd.to_datetime(factor_series.index)  # 转 Timestamp（避免 dict+index 错配致 NaN）
        factor_series = factor_series.sort_index()
        left = df.reset_index().sort_values(CN_INDEX_NAME)
        right = pd.DataFrame({"因子日": factor_series.index, "_factor": factor_series.values})
        right = right.sort_values("因子日")
        merged = pd.merge_asof(
            left, right, left_on=CN_INDEX_NAME, right_on="因子日", direction="backward"
        )
        merged["_factor"] = merged["_factor"].fillna(1.0)
        if adjust == "qfq":
            latest = float(factor_series.iloc[-1])
            merged["_factor"] = merged["_factor"] / latest
        for col in ("开盘价", "最高价", "最低价", "收盘价"):
            if col in merged.columns:
                merged[col] = pd.to_numeric(merged[col], errors="coerce") * merged["_factor"]
        out = merged.drop(columns=["_factor"]).set_index(CN_INDEX_NAME)
        out.index.name = CN_INDEX_NAME
        out = out.sort_index()
        out = self._recompute_price_derived_metrics(out)
        out.attrs["adjust_effective"] = adjust  # recompute 返回新 df，重设复权标识
        return out

    def _recompute_price_derived_metrics(
        self,
        df: pd.DataFrame,
    ) -> pd.DataFrame:
        """复权现算后重算价格类派生量（涨跌幅/振幅），消除除权日 raw 假跳变。

        S1（#83）：processed 派生量列仍为 raw 口径（除权日真实跳变）；消费端
        hfq/qfq 现算后按复权价重算，口径与清洗端 compute_derived_metrics 一致：
        pct_chg=(close_t-close_{t-1})/close_{t-1}*100、amplitude=(high_t-low_t)/
        close_{t-1}*100，首行无前收盘价置 None；列缺失时跳过不创建。
        volume_ratio/turnover/float_share/pe_dynamic 非价格派生量保持不动。

        Parameters
        ----------
        df : pd.DataFrame
            复权现算后的行情 DataFrame（中文表头，含 收盘价 列）。

        Returns
        -------
        pd.DataFrame
            重算 涨跌幅/振幅 后的同一 DataFrame（原文件 raw 派生量被覆盖）。
        """
        if df.empty or "收盘价" not in df.columns:
            return df
        close = pd.to_numeric(df["收盘价"], errors="coerce")
        prev_close = close.shift(1)
        # KP-P2-10/T-02：复权现算后同步刷新前收盘价（涨跌停校验基准需与复权价同口径，
        # 首行无前驱为 NaN）
        df["前收盘价"] = prev_close
        denom_valid = prev_close.notna() & (prev_close != 0)
        if "涨跌幅" in df.columns:
            pct = ((close - prev_close) / prev_close * 100.0).round(4)
            df["涨跌幅"] = pct.where(denom_valid)
        if "振幅" in df.columns and "最高价" in df.columns and "最低价" in df.columns:
            high = pd.to_numeric(df["最高价"], errors="coerce")
            low = pd.to_numeric(df["最低价"], errors="coerce")
            amp = ((high - low) / prev_close * 100.0).round(4)
            df["振幅"] = amp.where(denom_valid)
        return df

    def get_available_symbols(self) -> List[str]:
        """获取所有可用标的列表"""
        return sorted(self._file_cache.keys())

    def get_date_range(self, symbol: str) -> Optional[tuple]:
        """
        获取某标的的数据日期范围（不过滤，返回完整范围）

        Returns
        -------
        tuple | None : (最早日期str, 最晚日期str)，无数据返回None
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
        dict : {symbol: DataFrame}，无数据的标的不包含在结果中。
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

# TODO(阶段6): 实现 RealtimeDataProvider，继承此类，从实时行情feed获取数据
#   - 从 live/data_feed.py 订阅实时行情
#   - get_bars() 返回已收盘的历史bar + 当前正在形成的bar
#   - 实盘时替换 HistoricalDataProvider，引擎零修改
