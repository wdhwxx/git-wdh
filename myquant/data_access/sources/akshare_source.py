# 文件名：myquant/data_access/sources/akshare_source.py
"""
akshare_source.py - akshare网络数据源（多源自动降级，V2 稳定链）
==============================================================
通过akshare库 + 原生HTTP接口获取网络行情和财务数据，作为通达信本地源的补充。

★★★ 数据源优先级（V2 修复版，2026-09-05）★★★
  行情数据（日线）降级链：
    1. 东方财富 stock_zh_a_hist   —— 主源，akshare官方主力，数据最全含后复权
    2. 腾讯财经 原生HTTP(fqkline)  —— 备源1，web.ifzq.gtimg.cn，稳定含前/后复权
    3. 网易财经 原生HTTP(chddata)  —— 备源2，quotes.money.163.com，最稳定但仅不复权
    4. 新浪 stock_zh_a_daily       —— 兜底，已被akshare废弃，多数环境不可用
  分钟线：
    仅东方财富 stock_zh_a_hist_min_em
  财务数据（稳定链）:
    1. 东方财富 stock_individual_info_em —— PE/PB/市值/每股收益，会话级封锁跳过
    2. 新浪 stock_financial_abstract     —— 财报摘要（归母净利润/营业总收入/ROE等）
    3. 百度 stock_zh_valuation_baidu     —— 东财不可用时补 PE(TTM)/市净率
    4. 全源失败时整体重试1次

V2 修复说明（2026-09-05）：
  - 根因：原代码新浪 stock_zh_a_daily 优先，但该接口已被 akshare 1.11+ 废弃；
    健康检查误用 stock_zh_a_spot_em（股票列表）判活，导致源被误判为 DEGRADED
    后选中，但实际行情接口因网络封锁返回空，GUI 显示每只 0 条。
  - 修复：东财行情接口优先；新增腾讯/网易原生HTTP源（不依赖akshare封装，抗接口变更）；
    健康检查改用实际行情接口 stock_zh_a_hist；每个源增加会话级封锁标志。

核心能力:
  1. 行情数据：A股日线/分钟线，支持后复权
  2. 财务数据：PE/PB/市值/财报指标（通达信不支持，走此源）
  3. 股票列表：全市场A股列表，含名称/行业/地区
  4. 指数数据：上证指数/深证成指/创业板指等
  5. 实时行情：盘中实时报价（延迟约3秒）

设计原则:
  - 延迟导入akshare，未安装时不报错（健康检查返回DOWN）
  - 原生HTTP源不依赖akshare版本，抗接口变更
  - 所有网络请求加超时和异常处理，禁止裸except静默吞掉
  - 防未来函数：严格按end_date过滤
  - 会话级封锁：ConnectionError后本会话跳过该源，避免每只浪费重试
  - 接口变更时自动降级，不影响其他数据源

依赖: akshare（可选，延迟导入）, pandas, requests, datetime, typing, csv, io
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from myquant.core.logger import get_logger

from .base import (
    DataSourceBase,
    DataSourceStatus,
    MarketDataResult,
)

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 常量定义
# ─────────────────────────────────────────────

# 请求超时时间（秒）
DEFAULT_TIMEOUT = 30

# 连续失败阈值（超过此数标记为DOWN）
MAX_FAIL_COUNT = 5

# 腾讯财经K线接口（原生HTTP，含复权）
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

# 东方财富数据中心API（方案A·2026-09-12：push2行情接口被IP级风控时，
# 估值/财务改走 datacenter-web 官方数据中心，RPT_VALUEANALYSIS_DET 估值分析
# + RPT_F10_FINANCE_MAINFINADATA 财务主数据，同一家东财、独立子域独立风控）
EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_DATACENTER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

# 网易财经历史数据接口（原生HTTP，CSV，仅不复权）
NETEASE_HISTORY_URL = "http://quotes.money.163.com/service/chddata.html"

# 网易接口字段清单（收盘价/最高/最低/开盘/前收盘/涨跌额/涨跌幅/换手率/成交量/成交金额）
NETEASE_FIELDS = "TCLOSE;HIGH;LOW;TOPEN;LCLOSE;CHG;PCHG;TURNOVER;VOTURNOVER;VATURNOVER"


# ─────────────────────────────────────────────
# akshare数据源类
# ─────────────────────────────────────────────

class AkshareDataSource(DataSourceBase):
    """
    akshare网络数据源（多源自动降级，V2 稳定链）
    ============================================
    通过akshare + 原生HTTP获取网络数据，作为本地源的补充。
    内部自动降级：东财优先，失败切腾讯/网易，新浪兜底。

    Usage:
        from myquant.data_access.sources.akshare_source import AkshareDataSource
        source = AkshareDataSource()
        result = source.get_market_data("000001.SZ", "2024-01-01", "2024-12-31")
        financial = source.get_financial_data("600519.SH")
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        Parameters
        ----------
        config : dict | None
            配置项：
            - timeout : int  请求超时时间（默认30秒）
            - retry_count : int  失败重试次数（默认2）
            - prefer_source : str  优先数据源 "eastmoney"/"tencent"/"netease"/"sina"
              （默认"eastmoney"，V2修复：新浪已废弃不再优先）
        """
        super().__init__(config)
        self._timeout = self.config.get("timeout", DEFAULT_TIMEOUT)
        self._retry_count = self.config.get("retry_count", 2)
        self._prefer_source = self.config.get("prefer_source", "eastmoney")
        self._akshare_available = None  # None=未检测, True/False=检测结果

        # 会话级封锁标志：源被网络封锁（ConnectionError）后本会话跳过，
        # 避免每只股票浪费重试等待（V2 提速）
        self._em_blocked = False          # 东财财务接口封锁
        self._em_hist_blocked = False     # 东财行情接口封锁
        self._tencent_blocked = False     # 腾讯行情接口封锁
        self._netease_blocked = False     # 网易行情接口封锁

    # ── 基本属性 ──

    @property
    def name(self) -> str:
        return "akshare"

    @property
    def source_type(self) -> str:
        return "network"

    @property
    def priority(self) -> int:
        return 50  # 网络源优先级低于本地源（通达信priority=10）

    @property
    def supported_markets(self) -> Set[str]:
        return {"A股"}

    @property
    def supported_periods(self) -> Set[str]:
        return {"daily", "minute"}

    @property
    def supports_financial(self) -> bool:
        return True  # akshare支持财务数据

    @property
    def supports_realtime(self) -> bool:
        return True  # akshare支持实时行情（轮询方式）

    @property
    def supports_index(self) -> bool:
        return True

    # ── akshare延迟导入 ──

    def _get_akshare(self):
        """延迟导入akshare，未安装返回None"""
        if self._akshare_available is False:
            return None
        try:
            import akshare as ak
            self._akshare_available = True
            return ak
        except ImportError:
            self._akshare_available = False
            return None

    def _check_akshare(self) -> bool:
        """检查akshare是否可用"""
        return self._get_akshare() is not None

    # ── 工具方法：代码格式转换 ──

    def _symbol_to_akshare(self, symbol: str) -> str:
        """
        股票代码转换（东方财富源格式）：000001.SZ -> 000001
        akshare东方财富接口通常只用6位数字代码
        """
        return symbol.split(".")[0]

    def _symbol_to_sina(self, symbol: str) -> str:
        """
        股票代码转换（新浪源格式）：000001.SZ -> sz000001, 600519.SH -> sh600519
        akshare新浪接口需要带市场前缀
        """
        if "." not in symbol:
            code = symbol
            if code.startswith(("60", "68", "90")):
                return f"sh{code}"
            elif code.startswith(("00", "30", "20")):
                return f"sz{code}"
            elif code.startswith(("43", "83", "87", "88", "89", "92")):
                return f"bj{code}"
            return f"sz{code}"
        code, market = symbol.split(".")
        market = market.lower()
        if market in ("sh", "sz", "bj"):
            return f"{market}{code}"
        return f"sz{code}"

    def _symbol_to_tencent(self, symbol: str) -> str:
        """
        股票代码转换（腾讯源格式）：600519.SH -> sh600519, 000001.SZ -> sz000001
        腾讯原生HTTP接口需要带市场前缀（小写）
        """
        return self._symbol_to_sina(symbol)  # 腾讯与新浪格式一致

    def _symbol_to_netease(self, symbol: str) -> str:
        """
        股票代码转换（网易源格式）：600519.SH -> 0600519, 000001.SZ -> 1000001
        网易接口 code 参数：沪市前缀0，深市前缀1，后跟6位数字
        """
        code = symbol.split(".")[0]
        if "." in symbol:
            market = symbol.split(".")[1].upper()
        else:
            market = "SH" if code.startswith(("60", "68", "90")) else "SZ"
        prefix = "0" if market == "SH" else "1"
        return f"{prefix}{code}"

    # ── 工具方法：DataFrame转标准记录 ──

    def _df_to_records(self, df, date_col: str = "日期") -> List[Dict]:
        """
        将akshare返回的DataFrame转换为标准字典列表

        标准字段：date, open, high, low, close, volume, amount, pct_chg
        同时支持东方财富源（中文列名）和新浪源（英文列名）
        """
        if df is None or df.empty:
            return []
        records = []
        col_map = {
            "日期": "date", "date": "date",
            "开盘": "open", "open": "open",
            "最高": "high", "high": "high",
            "最低": "low", "low": "low",
            "收盘": "close", "close": "close",
            "成交量": "volume", "volume": "volume",
            "成交额": "amount", "amount": "amount",
            "涨跌幅": "pct_chg", "pct_chg": "pct_chg",
            "换手率": "turnover", "turnover": "turnover",
        }
        for _, row in df.iterrows():
            record = {}
            for col, std_col in col_map.items():
                if col in row.index:
                    val = row[col]
                    if std_col == "date":
                        if hasattr(val, "strftime"):
                            record[std_col] = val.strftime("%Y-%m-%d")
                        else:
                            record[std_col] = str(val)[:10]
                    else:
                        try:
                            record[std_col] = float(val)
                        except (ValueError, TypeError):
                            record[std_col] = None
            if "date" in record and record["date"]:
                records.append(record)
        return records

    def _retry_request(self, func, *args, **kwargs):
        """带重试的请求封装"""
        last_error = None
        for attempt in range(self._retry_count + 1):
            try:
                result = func(*args, **kwargs)
                self._record_success()
                return result
            except Exception as e:
                last_error = e
                if attempt < self._retry_count:
                    import time
                    time.sleep(1 * (attempt + 1))  # 递增等待
        self._record_failure()  # 最终失败才记1次（重试是正常容错，不累积）
        raise last_error

    def _is_block_error(self, e: Exception) -> bool:
        """判断是否为确定性网络封锁错误（连接被拒/远端关闭/超时），此类失败重试无意义"""
        try:
            import requests
            if isinstance(e, (
                requests.exceptions.ConnectionError,
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
            )):
                return True
        except ImportError:
            pass
        return type(e).__name__ in (
            "ConnectionError", "RemoteDisconnected", "TimeoutError",
            "ConnectTimeoutError",
        )

    # ═══════════════════════════════════════════
    # 行情数据：多源降级（V2 稳定链）
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
        通过多源获取行情数据（V2 自动降级链）

        ★★★ 数据源优先级（V2 修复版）★★★
          日线：
            1. 东方财富 stock_zh_a_hist   —— 主源，含后复权
            2. 腾讯财经 原生HTTP(fqkline)  —— 备源1，含前/后复权
            3. 网易财经 原生HTTP(chddata)  —— 备源2，最稳定但仅不复权
            4. 新浪 stock_zh_a_daily       —— 兜底（已废弃）
          分钟线：仅东方财富 stock_zh_a_hist_min_em

        【防未来函数】end_date 之后的数据严格不返回

        Parameters
        ----------
        symbol : str
            股票代码，如 000001.SZ
        start_date : str | None
            开始日期 YYYY-MM-DD
        end_date : str | None
            截止日期 YYYY-MM-DD
        period : str
            周期 daily/minute
        adjust : str | None
            复权方式 hfq/qfq/None
        """
        ak = self._get_akshare()

        # akshare日期格式：YYYYMMDD
        ak_start = start_date.replace("-", "") if start_date else "19900101"
        ak_end = end_date.replace("-", "") if end_date else datetime.now().strftime("%Y%m%d")

        # 复权方式映射
        adjust_map = {"raw": "", "hfq": "hfq", "qfq": "qfq", None: "", "": ""}
        adjust_param = adjust_map.get(adjust, "")

        records: List[Dict] = []
        used_source = ""

        if period == "daily":
            # ── 源1：东方财富（主源，V2 优先）──
            if not self._em_hist_blocked:
                records, used_source = self._try_eastmoney_hist(
                    ak, symbol, ak_start, ak_end, adjust_param,
                )

            # ── 源2：腾讯财经原生HTTP（备源1）──
            if not records and not self._tencent_blocked:
                records, used_source = self._try_tencent_hist(
                    symbol, start_date, end_date, adjust,
                )

            # ── 源3：网易财经原生HTTP（备源2，最稳定兜底）──
            if not records and not self._netease_blocked:
                records, used_source = self._try_netease_hist(
                    symbol, ak_start, ak_end,
                )

            # ── 源4：新浪（兜底，已废弃）──
            if not records and self._prefer_source == "sina":
                records, used_source = self._try_sina_hist(
                    ak, symbol, ak_start, ak_end, adjust_param,
                )
        elif period == "minute":
            # ── 分钟线：仅东方财富源支持 ──
            if not self._em_hist_blocked and ak is not None:
                try:
                    code = self._symbol_to_akshare(symbol)
                    df_min = self._retry_request(
                        ak.stock_zh_a_hist_min_em,
                        symbol=code,
                        period="1",
                        adjust=adjust or "",
                    )
                    if df_min is not None and not df_min.empty:
                        records = self._df_to_records(df_min)
                        used_source = "akshare_eastmoney_minute"
                except Exception as e:
                    logger.error(
                        f"分钟线获取失败 [{symbol}]: {type(e).__name__}: {e}"
                    )
                    if self._is_block_error(e):
                        self._em_hist_blocked = True
                        logger.info("东财分钟线接口已被网络封锁，本会话后续跳过")

        if not records:
            logger.warning(
                f"所有行情源均失败 [{symbol}] period={period} adjust={adjust} "
                f"date_range={ak_start}~{ak_end}"
            )
            return MarketDataResult(symbol, [], source=self.name)

        # 【防未来函数】二次过滤（确保不返回未来数据）
        filtered = self._filter_by_date(records, start_date, end_date)
        return MarketDataResult(
            symbol=symbol,
            data=filtered,
            source=used_source or self.name,
            market="A股",
            period=period,
            adjust=adjust,
        )

    # ── 行情子源：东方财富 ──

    def _try_eastmoney_hist(
        self,
        ak,
        symbol: str,
        ak_start: str,
        ak_end: str,
        adjust_param: str,
    ) -> tuple[List[Dict], str]:
        """
        东方财富行情源（主源）：ak.stock_zh_a_hist

        Parameters
        ----------
        ak : module
            akshare 模块实例（None 时直接返回空）
        symbol : str
            股票代码 600519.SH
        ak_start : str
            开始日期 YYYYMMDD
        ak_end : str
            截止日期 YYYYMMDD
        adjust_param : str
            复权参数 hfq/qfq/""

        Returns
        -------
        tuple[list[dict], str] : (记录列表, 使用的源名)
        """
        if ak is None:
            return [], ""
        try:
            code = self._symbol_to_akshare(symbol)
            df_em = self._retry_request(
                ak.stock_zh_a_hist,
                symbol=code,
                period="daily",
                start_date=ak_start,
                end_date=ak_end,
                adjust=adjust_param,
            )
            if df_em is not None and not df_em.empty:
                records = self._df_to_records(df_em)
                logger.debug(f"东方财富源获取成功 [{symbol}]: {len(records)} 条")
                return records, "akshare_eastmoney"
        except Exception as e:
            logger.warning(
                f"东方财富行情失败 [{symbol}]: {type(e).__name__}: {e}"
            )
            if self._is_block_error(e):
                self._em_hist_blocked = True
                logger.info("东财行情接口已被网络封锁，本会话后续跳过东财行情")
        return [], ""

    # ── 行情子源：腾讯财经原生HTTP ──

    def _try_tencent_hist(
        self,
        symbol: str,
        start_date: Optional[str],
        end_date: Optional[str],
        adjust: Optional[str],
    ) -> tuple[List[Dict], str]:
        """
        腾讯财经行情源（备源1）：原生HTTP fqkline 接口

        不依赖 akshare 封装，直接调用 web.ifzq.gtimg.cn，抗接口变更。
        支持前复权(qfq)/后复权(hfq)/不复权。

        Parameters
        ----------
        symbol : str
            股票代码 600519.SH
        start_date : str | None
            开始日期 YYYY-MM-DD
        end_date : str | None
            截止日期 YYYY-MM-DD
        adjust : str | None
            复权方式 hfq/qfq/None

        Returns
        -------
        tuple[list[dict], str] : (记录列表, 使用的源名)
        """
        try:
            import requests
        except ImportError:
            logger.debug("requests未安装，跳过腾讯源")
            return [], ""

        tencent_symbol = self._symbol_to_tencent(symbol)
        # 腾讯复权参数：qfq=前复权, hfq=后复权, 空=不复权
        tx_adjust = adjust if adjust in ("qfq", "hfq") else ""
        tx_start = start_date or "1990-01-01"
        tx_end = end_date or datetime.now().strftime("%Y-%m-%d")

        url = (
            f"{TENCENT_KLINE_URL}?param={tencent_symbol},day,"
            f"{tx_start},{tx_end},640,{tx_adjust}"
        )

        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
            payload = resp.json()

            # 腾讯返回结构：data -> {symbol} -> qfqday/hfqday/day
            data_node = payload.get("data", {}).get(tencent_symbol, {})
            kline_key = "qfqday" if tx_adjust == "qfq" else (
                "hfqday" if tx_adjust == "hfq" else "day"
            )
            kline = data_node.get(kline_key) or data_node.get("day") or []

            if not kline:
                return [], ""

            records = []
            for item in kline:
                # 腾讯K线每条格式: [date, open, close, high, low, volume, ...]
                if len(item) < 6:
                    continue
                records.append({
                    "date": str(item[0])[:10],
                    "open": self._safe_float(item[1]),
                    "close": self._safe_float(item[2]),
                    "high": self._safe_float(item[3]),
                    "low": self._safe_float(item[4]),
                    "volume": self._safe_float(item[5]) * 100.0,  # 手→股（P1-④ #83，与腾讯实时/全库口径一致）
                    "amount": None,  # 腾讯接口不直接返回成交额
                    "pct_chg": None,
                })
            logger.debug(f"腾讯源获取成功 [{symbol}]: {len(records)} 条")
            return records, "tencent_http"
        except Exception as e:
            logger.warning(
                f"腾讯行情失败 [{symbol}]: {type(e).__name__}: {e}"
            )
            if self._is_block_error(e):
                self._tencent_blocked = True
                logger.info("腾讯行情接口已被网络封锁，本会话后续跳过腾讯")
            return [], ""

    # ── 行情子源：网易财经原生HTTP ──

    def _try_netease_hist(
        self,
        symbol: str,
        ak_start: str,
        ak_end: str,
    ) -> tuple[List[Dict], str]:
        """
        网易财经行情源（备源2，最稳定兜底）：原生HTTP CSV 接口

        不依赖 akshare 封装，直接调用 quotes.money.163.com，稳定性极高。
        注意：网易仅提供不复权数据，返回时 adjust 会被标记为 None。

        Parameters
        ----------
        symbol : str
            股票代码 600519.SH
        ak_start : str
            开始日期 YYYYMMDD
        ak_end : str
            截止日期 YYYYMMDD

        Returns
        -------
        tuple[list[dict], str] : (记录列表, 使用的源名)
        """
        try:
            import requests
        except ImportError:
            logger.debug("requests未安装，跳过网易源")
            return [], ""

        netease_code = self._symbol_to_netease(symbol)
        url = (
            f"{NETEASE_HISTORY_URL}?code={netease_code}"
            f"&start={ak_start}&end={ak_end}&fields={NETEASE_FIELDS}"
        )

        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
            # 网易CSV为GBK编码
            resp.encoding = "gbk"
            text = resp.text

            if not text or len(text.strip()) < 50:
                return [], ""

            reader = csv.DictReader(io.StringIO(text))
            records = []
            for row in reader:
                date_str = str(row.get("日期", "")).strip()
                if not date_str:
                    continue
                records.append({
                    "date": date_str,
                    "open": self._safe_float(row.get("开盘价")),
                    "high": self._safe_float(row.get("最高价")),
                    "low": self._safe_float(row.get("最低价")),
                    "close": self._safe_float(row.get("收盘价")),
                    "volume": self._safe_float(row.get("成交量")),
                    "amount": self._safe_float(row.get("成交金额")),
                    "pct_chg": self._safe_float(row.get("涨跌幅")),
                    "turnover": self._safe_float(row.get("换手率")),
                })
            if records:
                # 网易按日期倒序返回，转为升序
                records.sort(key=lambda r: r["date"])
                logger.debug(f"网易源获取成功 [{symbol}]: {len(records)} 条（不复权）")
                return records, "netease_http"
        except Exception as e:
            logger.warning(
                f"网易行情失败 [{symbol}]: {type(e).__name__}: {e}"
            )
            if self._is_block_error(e):
                self._netease_blocked = True
                logger.info("网易行情接口已被网络封锁，本会话后续跳过网易")
        return [], ""

    # ── 行情子源：新浪（兜底，已废弃）──

    def _try_sina_hist(
        self,
        ak,
        symbol: str,
        ak_start: str,
        ak_end: str,
        adjust_param: str,
    ) -> tuple[List[Dict], str]:
        """
        新浪行情源（兜底，已被 akshare 1.11+ 废弃）：ak.stock_zh_a_daily

        仅在 prefer_source="sina" 时尝试，多数环境不可用。
        """
        if ak is None:
            return [], ""
        try:
            sina_symbol = self._symbol_to_sina(symbol)
            df_sina = self._retry_request(
                ak.stock_zh_a_daily,
                symbol=sina_symbol,
                start_date=ak_start,
                end_date=ak_end,
                adjust=adjust_param,
            )
            if df_sina is not None and not df_sina.empty:
                records = self._df_to_records(df_sina)
                logger.debug(f"新浪源获取成功 [{symbol}]: {len(records)} 条")
                return records, "akshare_sina"
        except Exception as e:
            logger.warning(
                f"新浪行情失败 [{symbol}]: {type(e).__name__}: {e}（新浪接口已废弃，属正常）"
            )
        return [], ""

    # ═══════════════════════════════════════════
    # 财务数据：多源降级（稳定链，保持原逻辑）
    # ═══════════════════════════════════════════

    def get_financial_data(
        self,
        symbol: str,
        report_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        通过akshare获取财务数据（多源自动降级，稳定链）

        【防未来函数】end_date 之后的报告期数据不返回

        数据源优先级:
          1. 东方财富 stock_individual_info_em —— PE/PB/市值/每股收益 完整记录
             ★ 会话级跳过：本会话确认东财被网络封锁（ConnectionError）后，
               后续股票直接跳过东财，避免每只浪费约3秒重试
          2. 新浪 stock_financial_abstract —— 财报摘要（归母净利润/营业总收入/
             基本每股收益/每股净资产/ROE/毛利率等）
          3. 百度 stock_zh_valuation_baidu —— PE(TTM)/市净率 补估值
             ★ V2修复：百度从"新浪成功后补全"升级为"独立兜底源"——
               东财+新浪都失败时，直接用百度PE/PB生成最小财务记录，
               避免全链路返回空（原bug：新浪失败时百度不被调用）
          4. 整体兜底：全部源失败时整体重试1次，应对瞬时双源故障
        """
        ak = self._get_akshare()
        if ak is None:
            logger.warning(f"akshare未安装，无法获取财务数据: {symbol}")
            return []
        code = self._symbol_to_akshare(symbol)

        # 整体兜底：最多2轮（应对瞬时双源故障）
        for chain_attempt in range(2):
            # ── ① 东方财富财务（方案A双通道：push2优先，封锁/失败降级datacenter-web）──
            em_result = self._try_eastmoney_finance(ak, symbol, code, end_date)
            if em_result is not None:
                return [em_result]

            # ── ② 新浪财务摘要（报表指标）──
            sina_result = self._try_sina_finance(ak, symbol, code, end_date)
            if sina_result is not None:
                # ── ③ 东财不可用时新浪缺 PE/PB → 百度补估值 ──
                if sina_result.get("pe") is None or sina_result.get("pb") is None:
                    val = self._fetch_baidu_valuation(ak, code)
                    if val:
                        if sina_result.get("pe") is None:
                            sina_result["pe"] = val.get("pe")
                        if sina_result.get("pb") is None:
                            sina_result["pb"] = val.get("pb")
                return [sina_result]
            # ── ④ 百度独立兜底（东财+新浪都失败时，用PE/PB生成最小记录）──
            baidu_val = self._fetch_baidu_valuation(ak, code)
            if baidu_val and (baidu_val.get("pe") is not None or baidu_val.get("pb") is not None):
                # P2-②(#83)：百度估值为实时快照，无真实报告期；report_date=今天 伪造报告期
                #   （回测因子回溯可用到"未来"快照=未来函数），丢弃 + 留痕，不写库
                logger.warning(
                    f"丢弃百度估值兜底记录 [{symbol}]（无真实报告期，防未来函数 P2-② #83）"
                )
                return []

            if chain_attempt == 0:
                import time
                time.sleep(1.5)  # 瞬时故障等待后整体重试

        logger.warning(f"财务数据所有源均失败 [{symbol}]: 东财封锁={self._em_blocked}, 已尝试新浪+百度兜底")
        return []

    def _try_eastmoney_finance(self, ak, symbol: str, code: str, end_date: Optional[str]):
        """
        东方财富财务数据（方案A双通道：push2 + datacenter-web）

        push2 行情接口（PE/PB/市值/每股收益）优先；接口被IP级风控
        （ConnectionError/RemoteDisconnected）或解析失败时，自动降级到
        东财官方数据中心接口 datacenter-web（RPT_VALUEANALYSIS_DET 估值分析
        + RPT_F10_FINANCE_MAINFINADATA 财务主数据）。

        成功返回结果 dict；全部通道失败返回 None 并记录会话级封锁标志。

        Returns
        -------
        dict | None : 财务记录；失败返回 None
        """
        # ── 通道① push2（会话级封锁后跳过，不再浪费重试等待）──
        if not self._em_blocked:
            try:
                df = self._retry_request(ak.stock_individual_info_em, symbol=code)
                if df is None or df.empty:
                    return None
                info_dict = {}
                for _, row in df.iterrows():
                    info_dict[str(row.iloc[0])] = row.iloc[1]
                # P2-②(#83)：东财 push2 为实时估值快照，无真实报告期；
                #   report_date=今天 伪造报告期（回测可用"未来"快照=未来函数），
                #   丢弃 + 留痕，不写库（真实报表走通道② datacenter-web）
                logger.warning(
                    f"丢弃东财实时估值快照 [{symbol}]（无真实报告期，防未来函数 P2-② #83）"
                )
                # P0-02修复：丢弃无报告期快照后必须返回 None（原 return [] 被 composite
                # 误判为"成功"并短路全部降级源，通道②/新浪/百度兜底全部不可达）；
                # 删除永不可达的 return result 死代码。
                return None
            except Exception as e:
                logger.warning(
                    f"东方财富push2财务接口失败 [{symbol}]: {type(e).__name__}: {e}"
                )
                if self._is_block_error(e):
                    self._em_blocked = True
                    logger.info("东财push2财务接口已被网络封锁，本会话降级数据中心接口")

        # ── 通道② datacenter-web（push2 封锁/失败时兜底）──
        dc_result = self._fetch_eastmoney_datacenter(symbol, code, end_date)
        if dc_result is not None:
            self._record_success()  # 降级成功视为源可用，不累积失败计数
            return dc_result
        return None

    def _fetch_eastmoney_datacenter(self, symbol: str, code: str, end_date: Optional[str]):
        """
        东财数据中心接口（datacenter-web，方案A新增兜底通道）

        估值分析 RPT_VALUEANALYSIS_DET 提供 PE_TTM/PB_MRQ/总市值/流通市值；
        财务主数据 RPT_F10_FINANCE_MAINFINADATA 提供 EPSJB/BPS。
        两个接口均为东财官方数据中心报表，与东财网页/App 同源，字段口径一致。

        Returns
        -------
        dict | None : 财务记录；请求失败/无数据返回 None
        """
        try:
            import requests
        except ImportError:
            logger.debug("requests未安装，跳过东财数据中心接口")
            return None

        params_val = {
            "reportName": "RPT_VALUEANALYSIS_DET",
            "columns": "ALL",
            "filter": f'(SECUCODE="{symbol}")',
            "pageNumber": "1",
            "pageSize": "1",
            "sortTypes": "-1",
            "sortColumns": "TRADE_DATE",
            "source": "WEB",
            "client": "WEB",
        }
        params_fin = {
            "reportName": "RPT_F10_FINANCE_MAINFINADATA",
            "columns": "ALL",
            "filter": f'(SECUCODE="{symbol}")',
            "pageNumber": "1",
            "pageSize": "1",
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
            "source": "WEB",
            "client": "WEB",
        }
        try:
            resp = requests.get(
                EASTMONEY_DATACENTER_URL, params=params_val,
                headers=EASTMONEY_DATACENTER_HEADERS, timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            rows = (payload.get("result") or {}).get("data") or []
            if not rows:
                logger.warning(f"东财数据中心估值无数据 [{symbol}]")
                return None
            row = rows[0]
            # P2-②(#83)：TRADE_DATE 缺失时不兜底今天（伪造报告期=未来函数），丢弃+留痕
            report_date = str(row.get("TRADE_DATE") or "")[:10]
            if not report_date:
                logger.warning(
                    f"丢弃东财数据中心估值记录 [{symbol}]（TRADE_DATE 缺失，防未来函数 P2-② #83）"
                )
                return None
            if end_date and report_date > end_date:
                return None  # 防未来函数：交易日晚于截止日

            result = {
                "symbol": symbol,
                "market": "A股",
                "report_date": report_date,
                "report_type": "实时",
                "pe": self._safe_float(row.get("PE_TTM")),
                "pb": self._safe_float(row.get("PB_MRQ")),
                "total_mv": self._safe_float(row.get("TOTAL_MARKET_CAP")),
                "circ_mv": self._safe_float(row.get("NOTLIMITED_MARKETCAP_A")),
                "eps": None,
                "bps": None,
                "data_source": f"{self.name}_eastmoney_datacenter",
            }

            # 财务主数据补 EPS/BPS（单独接口，失败不影响估值结果）
            try:
                resp2 = requests.get(
                    EASTMONEY_DATACENTER_URL, params=params_fin,
                    headers=EASTMONEY_DATACENTER_HEADERS, timeout=self._timeout,
                )
                resp2.raise_for_status()
                payload2 = resp2.json()
                rows2 = (payload2.get("result") or {}).get("data") or []
                if rows2:
                    fin_row = rows2[0]
                    result["eps"] = self._safe_float(fin_row.get("EPSJB"))
                    result["bps"] = self._safe_float(fin_row.get("BPS"))
            except Exception as e:
                logger.debug(f"东财数据中心财务主数据失败 [{symbol}]: {type(e).__name__}: {e}")
            return result
        except Exception as e:
            logger.warning(
                f"东财数据中心财务接口失败 [{symbol}]: {type(e).__name__}: {e}"
            )
            return None

    def _try_sina_finance(self, ak, symbol: str, code: str, end_date: Optional[str]):
        """
        新浪财务摘要源（财报报表指标）
        成功返回结果 dict；失败返回 None。
        """
        try:
            df2 = self._retry_request(ak.stock_financial_abstract, symbol=code)
            if df2 is None or df2.empty:
                return None
            # 最新报告期为第一列（akshare 按时间倒序排列）
            report_cols = [c for c in df2.columns if c not in ("选项", "指标")]
            latest_period = report_cols[0] if report_cols else ""
            fin_dict = {}
            for _, row in df2.iterrows():
                indicator = str(row.get("指标", ""))
                value = row.get(latest_period, None)
                if indicator and value is not None:
                    fin_dict[indicator] = value
            result = {
                "symbol": symbol,
                "market": "A股",
                "report_date": latest_period,
                "report_type": "财务摘要",
                "pe": self._safe_float(fin_dict.get("市盈率")),
                "pb": self._safe_float(fin_dict.get("市净率")),
                "total_mv": self._safe_float(fin_dict.get("总市值")),
                "circ_mv": self._safe_float(fin_dict.get("流通市值")),
                "eps": self._safe_float(fin_dict.get("基本每股收益")),
                "bps": self._safe_float(fin_dict.get("每股净资产")),
                "revenue": self._safe_float(fin_dict.get("营业总收入")),
                "net_profit": self._safe_float(fin_dict.get("净利润")),
                "net_profit_parent": self._safe_float(fin_dict.get("归母净利润")),
                "roe": self._safe_float(fin_dict.get("净资产收益率(ROE)")),
                "gross_margin": self._safe_float(fin_dict.get("毛利率")),
                "debt_ratio": self._safe_float(fin_dict.get("资产负债率")),
                "data_source": f"{self.name}_sina",
            }
            if (end_date and result["report_date"]
                    and result["report_date"] > end_date.replace("-", "")):
                return None  # 防未来函数：报告期晚于截止日
            return result
        except Exception as e2:
            logger.warning(f"新浪财务接口失败 [{symbol}]: {type(e2).__name__}: {e2}")
            return None

    def _fetch_baidu_valuation(self, ak, code: str) -> Optional[Dict]:
        """
        百度估值源：补 PE(TTM)/市净率（东财不可用时）
        每次约0.3秒；失败不抛异常，返回 None。
        """
        pe = pb = None
        try:
            df = self._retry_request(
                ak.stock_zh_valuation_baidu,
                symbol=code, indicator="市盈率(TTM)", period="近一年",
            )
            if df is not None and not df.empty:
                pe = self._safe_float(df.iloc[-1].get("value"))
        except Exception as e:
            logger.warning(f"百度市盈率获取失败 [{code}]: {type(e).__name__}: {e}")
        try:
            df = self._retry_request(
                ak.stock_zh_valuation_baidu,
                symbol=code, indicator="市净率", period="近一年",
            )
            if df is not None and not df.empty:
                pb = self._safe_float(df.iloc[-1].get("value"))
        except Exception as e:
            logger.warning(f"百度市净率获取失败 [{code}]: {type(e).__name__}: {e}")
        if pe is None and pb is None:
            return None
        return {"pe": pe, "pb": pb}

    # ═══════════════════════════════════════════
    # 历史多期财务数据
    # ═══════════════════════════════════════════

    def _fetch_eastmoney_financial_history(
        self,
        symbol: str,
        code: str,
        start_norm: Optional[str],
        end_norm: Optional[str],
    ) -> List[Dict]:
        """
        东财数据中心历史财务（方案A扩展，替代已失效的新浪历史接口）

        数据源：datacenter-web RPT_F10_FINANCE_MAINFINADATA（财务主数据，
        按报告期多期返回：EPS/BPS/营业总收入/归母净利润/ROE/毛利率/资产负债率）。
        报告期倒序排列（最新在前），report_date 格式 YYYYMMDD（与新浪历史通道一致）。

        Args:
            symbol: 股票代码（带后缀，如 600519.SH）
            code: 6位数字代码
            start_norm: 起始报告期 YYYYMMDD，None=不限制
            end_norm: 截止报告期 YYYYMMDD，None=不限制

        Returns:
            多期财务记录列表（字段与 get_financial_data 一致，pe/pb 留 None
            由调用方补最新估值）；无数据或请求失败返回空列表
        """
        try:
            import requests
        except ImportError:
            logger.debug("requests未安装，跳过东财数据中心历史财务")
            return []

        params = {
            "reportName": "RPT_F10_FINANCE_MAINFINADATA",
            "columns": "ALL",
            "filter": f'(SECUCODE="{symbol}")',
            "pageNumber": "1",
            "pageSize": "200",
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
            "source": "WEB",
            "client": "WEB",
        }
        try:
            resp = requests.get(
                EASTMONEY_DATACENTER_URL, params=params,
                headers=EASTMONEY_DATACENTER_HEADERS, timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            rows = (payload.get("result") or {}).get("data") or []
        except Exception as e:
            logger.warning(
                f"东财数据中心历史财务失败 [{symbol}]: {type(e).__name__}: {e}"
            )
            return []

        results: List[Dict] = []
        for row in rows:
            raw_date = str(row.get("REPORT_DATE") or "")[:10].replace("-", "")
            if len(raw_date) != 8 or not raw_date.isdigit():
                continue
            if start_norm and raw_date < start_norm:
                continue  # 早于起始报告期，跳过
            if end_norm and raw_date > end_norm:
                continue  # 防未来函数：晚于截止日的报告期不返回
            record = {
                "symbol": symbol,
                "market": "A股",
                "report_date": raw_date,
                "report_type": "财务摘要(历史)",
                "pe": None,
                "pb": None,
                "total_mv": None,
                "circ_mv": None,
                "eps": self._safe_float(row.get("EPSJB")),
                "bps": self._safe_float(row.get("BPS")),
                "revenue": self._safe_float(row.get("TOTALOPERATEREVE")),
                "net_profit": None,  # 东财主数据表无含少数股东权益的净利润，仅归母口径
                "net_profit_parent": self._safe_float(row.get("PARENTNETPROFIT")),
                "roe": self._safe_float(row.get("ROEJQ")),
                "gross_margin": self._safe_float(row.get("XSMLL")),
                "debt_ratio": self._safe_float(row.get("ZCFZL")),
                "data_source": f"{self.name}_eastmoney_datacenter_history",
            }
            results.append(record)

        if results:
            logger.info(
                f"东财数据中心历史财务 [{symbol}]: {len(results)}期 "
                f"({results[-1]['report_date']}~{results[0]['report_date']})"
            )
        return results

    def get_financial_history(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        获取历史多期财务数据（遍历新浪财报所有报告期，生成多条记录）

        【防未来函数】end_date 之后的报告期数据不返回（本行使用的数据截至 end_date 时刻）

        与 get_financial_data 的区别：
          - get_financial_data：只取最新1期，返回 List[Dict] 长度=1
          - get_financial_history：遍历所有报告期，返回 List[Dict] 长度=N期

        数据源：新浪 stock_financial_abstract（80行指标 × 约100列报告期，1998~最新）
        估值补全：百度 stock_zh_valuation_baidu 仅补最新一期的 PE/PB（百度只有近一年历史）

        Args:
            symbol: 股票代码，如 "600519.SH"
            start_date: 起始报告期，格式 "YYYYMMDD" 或 "YYYY-MM-DD"，None=不限制起始
            end_date: 截止报告期，格式同上，None=截止当前日期

        Returns:
            多期财务记录列表，每条字段与 get_financial_data 完全一致，
            按报告期倒序排列（最新在前），空列表表示无数据或全部被日期过滤

        Raises:
            ValueError: symbol 为空时抛出
        """
        if not symbol or not symbol.strip():
            raise ValueError("get_financial_history: symbol 不能为空")

        ak = self._get_akshare()
        if ak is None:
            logger.warning(f"akshare未安装，无法获取历史财务数据: {symbol}")
            return []
        code = self._symbol_to_akshare(symbol)

        # 归一化日期格式为 YYYYMMDD（统一比较口径）
        start_norm: Optional[str] = start_date.replace("-", "") if start_date else None
        end_norm: Optional[str] = end_date.replace("-", "") if end_date else None

        # ── 方案A扩展：东财数据中心历史财务优先 ──
        # 新浪 stock_financial_abstract 在 akshare 1.18+ 已失效（quotes.sina.cn
        # 接口改版返回空响应），东财 datacenter 为当前主力；新浪作为兜底保留。
        em_history: List[Dict] = self._fetch_eastmoney_financial_history(
            symbol, code, start_norm, end_norm
        )
        if em_history:
            # 最新一期补 PE/PB（datacenter 估值接口，与"百度仅补最新一期"语义一致）
            latest_valuation = self._fetch_eastmoney_datacenter(symbol, code, end_date)
            if latest_valuation is not None and latest_valuation.get("pe") is not None:
                em_history[0]["pe"] = latest_valuation["pe"]
                em_history[0]["pb"] = latest_valuation["pb"]
                em_history[0]["total_mv"] = latest_valuation["total_mv"]
                em_history[0]["circ_mv"] = latest_valuation["circ_mv"]
                em_history[0]["data_source"] = (
                    f"{self.name}_eastmoney_datacenter_history+valuation"
                )
            return em_history

        # 新浪财报全量表兜底（80行指标 × 约100列报告期，环境恢复后自动回退）
        try:
            df = self._retry_request(ak.stock_financial_abstract, symbol=code)
        except Exception as e:
            logger.warning(f"新浪历史财务接口失败 [{symbol}]: {type(e).__name__}: {e}")
            return []

        if df is None or df.empty:
            return []

        # 报告期列（排除"选项"和"指标"两列），akshare按时间倒序排列
        report_cols: List[str] = [c for c in df.columns if c not in ("选项", "指标")]
        if not report_cols:
            logger.debug(f"新浪财务数据无报告期列 [{symbol}]")
            return []

        # 构建指标字典：{指标名: {报告期: 值}}，便于按列取值
        indicator_map: Dict[str, Dict[str, object]] = {}
        for _, row in df.iterrows():
            indicator = str(row.get("指标", "")).strip()
            if not indicator:
                continue
            period_values: Dict[str, object] = {}
            for col in report_cols:
                val = row.get(col, None)
                if val is not None and str(val).strip() != "":
                    period_values[col] = val
            if period_values:
                indicator_map[indicator] = period_values

        # 筛选报告期范围【防未来函数：end_date 之后的报告期不返回】
        filtered_periods: List[str] = []
        for period in report_cols:
            if start_norm and period < start_norm:
                continue  # 早于起始日期，跳过
            if end_norm and period > end_norm:
                continue  # 晚于截止日期（未来数据），跳过
            filtered_periods.append(period)

        if not filtered_periods:
            logger.debug(
                f"历史财务数据无匹配报告期 [{symbol}]: "
                f"start={start_norm}, end={end_norm}, 可用={report_cols[-1]}~{report_cols[0]}"
            )
            return []

        # 百度PE/PB仅补最新一期（百度只有近一年历史估值，不补多年前）
        latest_period = filtered_periods[0]  # 倒序第一个=最新
        latest_pe = indicator_map.get("市盈率", {}).get(latest_period)
        latest_pb = indicator_map.get("市净率", {}).get(latest_period)
        baidu_val: Optional[Dict] = None
        if latest_pe is None or latest_pb is None:
            baidu_val = self._fetch_baidu_valuation(ak, code)

        # 逐期生成记录（字段与 get_financial_data 完全一致）
        results: List[Dict] = []
        for period in filtered_periods:
            record = {
                "symbol": symbol,
                "market": "A股",
                "report_date": period,
                "report_type": "财务摘要(历史)",
                "pe": self._safe_float(indicator_map.get("市盈率", {}).get(period)),
                "pb": self._safe_float(indicator_map.get("市净率", {}).get(period)),
                "total_mv": self._safe_float(indicator_map.get("总市值", {}).get(period)),
                "circ_mv": self._safe_float(indicator_map.get("流通市值", {}).get(period)),
                "eps": self._safe_float(indicator_map.get("基本每股收益", {}).get(period)),
                "bps": self._safe_float(indicator_map.get("每股净资产", {}).get(period)),
                "revenue": self._safe_float(indicator_map.get("营业总收入", {}).get(period)),
                "net_profit": self._safe_float(indicator_map.get("净利润", {}).get(period)),
                "net_profit_parent": self._safe_float(indicator_map.get("归母净利润", {}).get(period)),
                "roe": self._safe_float(indicator_map.get("净资产收益率(ROE)", {}).get(period)),
                "gross_margin": self._safe_float(indicator_map.get("毛利率", {}).get(period)),
                "debt_ratio": self._safe_float(indicator_map.get("资产负债率", {}).get(period)),
                "data_source": f"{self.name}_sina_history",
            }
            # 最新一期补百度PE/PB（新浪缺估值时）
            if period == latest_period and baidu_val:
                if record["pe"] is None and baidu_val.get("pe") is not None:
                    record["pe"] = baidu_val["pe"]
                if record["pb"] is None and baidu_val.get("pb") is not None:
                    record["pb"] = baidu_val["pb"]
                if baidu_val.get("pe") is not None or baidu_val.get("pb") is not None:
                    record["data_source"] = f"{self.name}_sina_history+baidu"
            results.append(record)

        logger.info(
            f"历史财务数据 [{symbol}]: {len(results)}期 "
            f"({filtered_periods[-1]}~{filtered_periods[0]})"
        )
        return results

    # ═══════════════════════════════════════════
    # 股票列表 / 指数 / 实时行情
    # ═══════════════════════════════════════════

    def get_stock_list(self, market: str = "A股") -> List[Dict]:
        """
        通过akshare获取A股股票列表

        Returns
        -------
        list[dict] : 股票信息列表，含名称/行业/地区
        """
        ak = self._get_akshare()
        if ak is None:
            return []
        try:
            df = self._retry_request(ak.stock_zh_a_spot_em)
            if df is None or df.empty:
                return []
            stocks = []
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                name = str(row.get("名称", ""))
                if not code or not code.isdigit():
                    continue
                if code.startswith(("60", "68")):
                    symbol = f"{code}.SH"
                elif code.startswith(("00", "30")):
                    symbol = f"{code}.SZ"
                elif code.startswith(("43", "83", "87", "88")):
                    symbol = f"{code}.BJ"
                else:
                    continue
                stocks.append({
                    "symbol": symbol,
                    "name": name,
                    "market": "A股",
                    "list_date": "",
                    "industry": str(row.get("所属行业", "")),
                    "area": str(row.get("所属地区", "")),
                    "data_source": self.name,
                })
            return stocks
        except Exception as e:
            logger.warning(f"股票列表获取失败: {type(e).__name__}: {e}")
            return []

    def get_index_list(self, market: str = "A股") -> List[Dict]:
        """获取主要指数列表"""
        return [
            {"symbol": "000001.SH", "name": "上证指数", "market": "A股"},
            {"symbol": "399001.SZ", "name": "深证成指", "market": "A股"},
            {"symbol": "399006.SZ", "name": "创业板指", "market": "A股"},
            {"symbol": "000300.SH", "name": "沪深300", "market": "A股"},
            {"symbol": "000905.SH", "name": "中证500", "market": "A股"},
            {"symbol": "000852.SH", "name": "中证1000", "market": "A股"},
        ]

    def subscribe_realtime(
        self,
        symbol: str,
        callback: callable,
        period: str = "tick",
    ) -> bool:
        """
        订阅实时行情（akshare通过轮询实现，非真正推送）

        注意：akshare实时行情是轮询方式，延迟约3秒，
        实盘高频场景建议使用券商Level-2接口。
        """
        if not self.supports_realtime:
            return False
        return super().subscribe_realtime(symbol, callback, period)

    # ═══════════════════════════════════════════
    # 健康检查（V2 修复：用实际行情接口判活）
    # ═══════════════════════════════════════════

    def health_check(self) -> DataSourceStatus:
        """
        健康检查：检查akshare是否安装、行情接口是否实际可用

        V2 修复：原代码误用 stock_zh_a_spot_em（股票列表）判活，
        导致股票列表通但行情接口被封锁时误判为 DEGRADED。
        现改用实际行情接口 stock_zh_a_hist 判活。

        检查顺序：
          1. akshare 是否安装
          2. 东方财富行情接口 stock_zh_a_hist（主源）
          3. 腾讯原生HTTP接口（备源1，无需akshare）
          4. 网易原生HTTP接口（备源2，无需akshare）

        Returns
        -------
        DataSourceStatus : 健康状态
            HEALTHY  — 主源可用
            DEGRADED — 主源不可用但备源可用
            DOWN     — 所有源均不可用
        """
        # 1. akshare 是否安装
        if not self._check_akshare():
            # akshare 未安装，但腾讯/网易原生HTTP源仍可能可用
            if self._health_check_tencent() or self._health_check_netease():
                self._update_status(DataSourceStatus.DEGRADED)
                return DataSourceStatus.DEGRADED
            self._update_status(DataSourceStatus.DOWN)
            return DataSourceStatus.DOWN

        ak = self._get_akshare()

        # 2. 东方财富行情接口（主源，实际调用判活）
        try:
            df = ak.stock_zh_a_hist(
                symbol="000001",
                period="daily",
                start_date="20240101",
                end_date="20240110",
                adjust="",
            )
            if df is not None and not df.empty:
                self._update_status(DataSourceStatus.HEALTHY)
                return DataSourceStatus.HEALTHY
        except Exception as e:
            logger.warning(f"东财行情健康检查失败: {type(e).__name__}: {e}")
            if self._is_block_error(e):
                self._em_hist_blocked = True

        # 3. 腾讯原生HTTP（备源1）
        if self._health_check_tencent():
            self._update_status(DataSourceStatus.DEGRADED)
            return DataSourceStatus.DEGRADED

        # 4. 网易原生HTTP（备源2）
        if self._health_check_netease():
            self._update_status(DataSourceStatus.DEGRADED)
            return DataSourceStatus.DEGRADED

        self._update_status(DataSourceStatus.DOWN)
        return DataSourceStatus.DOWN

    def _health_check_tencent(self) -> bool:
        """腾讯原生HTTP健康检查（用平安银行000001.SZ测试）"""
        records, _ = self._try_tencent_hist(
            "000001.SZ", "2024-01-01", "2024-01-10", None,
        )
        return len(records) > 0

    def _health_check_netease(self) -> bool:
        """网易原生HTTP健康检查（用平安银行000001.SZ测试）"""
        records, _ = self._try_netease_hist(
            "000001.SZ", "20240101", "20240110",
        )
        return len(records) > 0

    # ── 工具方法 ──

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        """安全转换为float"""
        if value is None:
            return None
        try:
            # 处理带单位的字符串，如 "1.2万亿"
            s = str(value).replace(",", "").replace("%", "")
            if "万亿" in s:
                return float(s.replace("万亿", "")) * 1e12
            if "亿" in s:
                return float(s.replace("亿", "")) * 1e8
            if "万" in s:
                return float(s.replace("万", "")) * 1e4
            return float(s)
        except (ValueError, TypeError):
            return None