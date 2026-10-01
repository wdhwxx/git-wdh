# 文件名：myquant/data_access/sources/web_source.py
"""
web_source.py - 网源数据源（东财→新浪→腾讯 三级降级）
=====================================================
专为"数据下载管理器"新增的"网源数据"选项提供的数据源适配器，
通过 akshare 库从网页抓取 A 股日线行情。

★★★ 降级链（用户指定）★★★
  1. 东方财富源 stock_zh_a_hist  —— 优先，东财 3 秒不通自动降级
  2. 新浪源 stock_zh_a_daily     —— 备用，新浪不通再降级
  3. 腾讯源 stock_zh_a_hist_tx   —— 兜底

★★★ 股票列表降级链（项2 增强）★★★
  1. 东财实时行情 stock_zh_a_spot_em（3 秒超时）→ 完整列表含名称/行业
  2. 新浪实时行情 stock_zh_a_spot（10 秒超时，分页抓取较慢）→ 完整列表含名称
  3. 本地通达信清单兜底（扫描 D:\\TDX 的 .day 文件，约 5333 只，零联网）→
     保证任何网络环境下网页抓取都不会因"股票列表为空"而 0 只。
     说明：股票清单只是标的代码集合，行情数据仍走上方网络降级链，
     因此不改变"网页抓取=网源数据"的语义，且与项目"通达信为回测主力
     本地清单最完整"的定位一致，为实时行情/实盘提供统一标的池。

设计原则:
  - 独立于 akshare_source（其内部为"新浪→东财"降级且无腾讯源），
    本模块不改动任何既有数据源，保护原有功能
  - 行情每源 3 秒超时（ThreadPoolExecutor + future.result(timeout=3)）
  - 延迟导入 akshare，未安装不报错
  - 防未来函数：严格按 end_date 过滤，不获取未来数据
  - health_check 探测真实行情路径（而非仅东财列表接口），
    使 GUI 状态卡如实反映"能否抓到数据"

依赖: akshare（可选，延迟导入）, pandas, concurrent.futures
"""

from __future__ import annotations

import contextlib
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from myquant.core.logger import get_logger

from .base import (
    DataSourceBase,
    DataSourceStatus,
    MarketDataResult,
)

logger = get_logger(__name__)


# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 单源请求超时（秒）：东财3秒不通→新浪→腾讯
SOURCE_TIMEOUT = 3

# 股票列表抓取超时（秒）：东财快、新浪分页慢且部分网络不稳定
# （实测 13~18s 波动），取 10s 平衡：快速成功时拿到含名称/北交所完整清单，
# 慢/挂时迅速落到 TDX 本地兜底，保证"网页抓取"永远不会因清单为空而 0 只。
LIST_TIMEOUT_EM = 3
LIST_TIMEOUT_SINA = 10

# 源名称（用于返回 source 标识）
SOURCE_EM = "东财"
SOURCE_SINA = "新浪"
SOURCE_TX = "腾讯"
SOURCE_TDX_FALLBACK = "tdx"  # 股票清单本地兜底源标识


# ─────────────────────────────────────────────
# 网源数据源类
# ─────────────────────────────────────────────

class WebSourceDataSource(DataSourceBase):
    """
    网源数据源（东财→新浪→腾讯 三级降级）

    Usage:
        from myquant.data_access.sources.web_source import WebSourceDataSource
        source = WebSourceDataSource()
        result = source.get_market_data("000001.SZ", "2024-01-01", "2024-12-31")
        stocks = source.get_stock_list()
    """

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config)
        self._timeout = float(self.config.get("timeout", SOURCE_TIMEOUT))
        self._akshare_available: Optional[bool] = None
        # 降级链运行统计（用于 UI 显示"源: X · 已降级N次"）
        self._degrade_count: int = 0
        self._last_source: Optional[str] = None
        # 股票清单来源（东财/新浪/tdx），供 UI 展示
        self._list_source: Optional[str] = None

    # ── 降级链运行统计 ──

    @property
    def degrade_count(self) -> int:
        """本次会话累计降级次数（东财失败→新浪、新浪失败→腾讯 各计一次）"""
        return self._degrade_count

    @property
    def last_source(self) -> Optional[str]:
        """最近一次成功获取行情数据所用的源名（东财/新浪/腾讯），未成功为 None"""
        return self._last_source

    @property
    def list_source(self) -> Optional[str]:
        """最近一次成功获取股票清单所用的源（东财/新浪/tdx），未成功为 None"""
        return self._list_source

    # ── 基本属性 ──

    @property
    def name(self) -> str:
        return "web"

    @property
    def source_type(self) -> str:
        return "network"

    @property
    def priority(self) -> int:
        # 略低于 akshare(50)，独立使用时无影响
        return 55

    @property
    def supported_markets(self) -> Set[str]:
        return {"A股"}

    @property
    def supported_periods(self) -> Set[str]:
        return {"daily"}

    # ── akshare 延迟导入 ──

    def _get_akshare(self):
        """延迟导入 akshare，未安装返回 None"""
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
        return self._get_akshare() is not None

    # ── 工具方法 ──

    @staticmethod
    def _symbol_to_code(symbol: str) -> str:
        """000001.SZ -> 000001（东财/腾讯数字代码）"""
        return symbol.split(".")[0]

    @staticmethod
    def _symbol_to_prefix(symbol: str) -> str:
        """000001.SZ -> sz000001（新浪/腾讯带市场前缀）"""
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

    def _df_to_records(self, df) -> List[Dict]:
        """
        将 akshare 返回的 DataFrame 转换为标准字典列表

        兼容中文列名（东财）与英文列名（新浪/腾讯）
        标准字段：date, open, high, low, close, volume, amount, pct_chg
        """
        if df is None or df.empty:
            return []
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
        records = []
        for _, row in df.iterrows():
            record = {}
            for col, std_col in col_map.items():
                if col not in row.index:
                    continue
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
            if record.get("date"):
                records.append(record)
        return records

    def _call_with_timeout(self, func, *args, timeout: Optional[float] = None, **kwargs):
        """
        带超时的请求封装（线程池 + future.result(timeout)）

        超时抛出 TimeoutError，由上层触发降级。

        ★ 修复要点：不使用 `with ThreadPoolExecutor` 上下文管理器——
        其退出时会 shutdown(wait=True) 阻塞直到慢调用真正结束，
        导致"3秒超时"名存实亡。这里改为手动管理，超时后立即返回，
        后台线程让其自然结束（wait=False），降级链马上进入下一源。
        """
        t = timeout if timeout is not None else self._timeout
        ex = ThreadPoolExecutor(max_workers=1)
        future = None
        try:
            future = ex.submit(func, *args, **kwargs)
            return future.result(timeout=t)
        finally:
            # 超时后立刻释放调用线程，不等待后台线程结束
            if future is not None:
                future.cancel()
            ex.shutdown(wait=False, cancel_futures=True)

    # ═══════════════════════════════════════════
    # 单源抓取函数（各源独立，异常向上抛由降级链处理）
    # ═══════════════════════════════════════════

    def _fetch_eastmoney(self, ak, symbol: str, start: str, end: str, adjust: str) -> List[Dict]:
        """东方财富源：stock_zh_a_hist（3秒超时）"""
        code = self._symbol_to_code(symbol)
        df = self._call_with_timeout(
            ak.stock_zh_a_hist,
            symbol=code, period="daily",
            start_date=start, end_date=end, adjust=adjust,
        )
        return self._df_to_records(df)

    def _fetch_sina(self, ak, symbol: str, start: str, end: str, adjust: str) -> List[Dict]:
        """新浪源：stock_zh_a_daily"""
        sina_symbol = self._symbol_to_prefix(symbol)
        df = self._call_with_timeout(
            ak.stock_zh_a_daily,
            symbol=sina_symbol, start_date=start, end_date=end, adjust=adjust,
        )
        return self._df_to_records(df)

    def _fetch_tencent(self, ak, symbol: str, start: str, end: str, adjust: str) -> List[Dict]:
        """腾讯源：stock_zh_a_hist_tx（接口不存在时返回空，由降级链兜底）"""
        fn = getattr(ak, "stock_zh_a_hist_tx", None)
        if fn is None:
            logger.warning("akshare 无腾讯接口 stock_zh_a_hist_tx，腾讯源不可用")
            return []
        tx_symbol = self._symbol_to_prefix(symbol)
        df = self._call_with_timeout(
            fn, symbol=tx_symbol, start_date=start, end_date=end, adjust=adjust,
        )
        return self._df_to_records(df)

    def _build_chain(self, ak) -> List[Tuple[str, callable]]:
        """构建行情降级链：[(源名, 抓取函数)]，供 get_market_data / health_check 复用"""
        return [
            (SOURCE_EM, self._fetch_eastmoney),
            (SOURCE_SINA, self._fetch_sina),
            (SOURCE_TX, self._fetch_tencent),
        ]

    def _filter_by_date(self, records: List[Dict],
                        start_date: Optional[str],
                        end_date: Optional[str]) -> List[Dict]:
        """【防未来函数】严格按请求日期区间过滤，end_date 之后的数据不返回"""
        if not records:
            return []
        filtered = records
        if end_date:
            filtered = [r for r in filtered if str(r.get("date", ""))[:10] <= str(end_date)[:10]]
        if start_date:
            filtered = [r for r in filtered if str(r.get("date", ""))[:10] >= str(start_date)[:10]]
        return filtered

    # ═══════════════════════════════════════════
    # 股票清单解析
    # ═══════════════════════════════════════════

    def _parse_list_df(self, df) -> List[Dict]:
        """
        将 akshare 股票列表 DataFrame 解析为标准清单

        兼容东财（stock_zh_a_spot_em）与新浪（stock_zh_a_spot）列名：
          - 代码/名称 两源均有
          - 所属行业/所属地区 仅东财有，新浪缺省填空
        """
        if df is None or df.empty:
            return []
        stocks = []
        for _, row in df.iterrows():
            code = str(row.get("代码", "")).strip()
            if not code or not code.isdigit():
                continue
            if code.startswith(("60", "68", "90")):
                symbol = f"{code}.SH"
            elif code.startswith(("00", "30", "20")):
                symbol = f"{code}.SZ"
            elif code.startswith(("43", "83", "87", "88")):
                symbol = f"{code}.BJ"
            else:
                continue
            stocks.append({
                "symbol": symbol,
                "name": str(row.get("名称", "") or ""),
                "market": "A股",
                "list_date": "",
                "industry": str(row.get("所属行业", "") or ""),
                "area": str(row.get("所属地区", "") or ""),
                "data_source": self.name,
            })
        return stocks

    def _fetch_list_em(self, ak) -> List[Dict]:
        """东财股票列表：stock_zh_a_spot_em（3秒超时）"""
        df = self._call_with_timeout(ak.stock_zh_a_spot_em, timeout=LIST_TIMEOUT_EM)
        return self._parse_list_df(df)

    def _fetch_list_sina(self, ak) -> List[Dict]:
        """新浪股票列表：stock_zh_a_spot（10秒超时，分页抓取慢）"""
        # 新浪 spot 内部逐页抓取会打印 tqdm 进度条（写到 stderr），
        # 同时重定向 stdout/stderr 避免污染 GUI/控制台日志
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            df = self._call_with_timeout(ak.stock_zh_a_spot, timeout=LIST_TIMEOUT_SINA)
        return self._parse_list_df(df)

    def _fetch_list_tdx(self) -> List[Dict]:
        """本地通达信清单兜底：扫描 D:\\TDX 的 .day 文件（零联网）"""
        try:
            from .tdx_source import TdxDataSource
            tdx = TdxDataSource()
            stocks = tdx.get_stock_list()
            # 统一 data_source 标识为 web（本源）+ 记录实际来自 tdx 兜底
            for s in stocks:
                s["data_source"] = f"{self.name}_{SOURCE_TDX_FALLBACK}"
            return stocks
        except Exception as e:
            logger.warning(f"本地通达信清单兜底失败: {type(e).__name__}: {e}")
            return []

    # ═══════════════════════════════════════════
    # 核心接口实现
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
        获取日线行情（东财→新浪→腾讯 三级降级，每源3秒超时）

        【防未来函数】end_date 之后的数据严格不返回
        """
        if period != "daily":
            return MarketDataResult(symbol, [], source=self.name)
        ak = self._get_akshare()
        if ak is None:
            logger.warning("akshare 未安装，网源数据不可用")
            return MarketDataResult(symbol, [], source=self.name)

        ak_start = start_date.replace("-", "") if start_date else "19900101"
        ak_end = end_date.replace("-", "") if end_date else datetime.now().strftime("%Y%m%d")
        adjust_map = {"raw": "", "hfq": "hfq", "qfq": "qfq", None: "", "": ""}
        adjust_param = adjust_map.get(adjust, "")

        # 降级链：东财 → 新浪 → 腾讯
        chain = self._build_chain(ak)
        for idx, (source_name, fetch_fn) in enumerate(chain):
            try:
                records = fetch_fn(ak, symbol, ak_start, ak_end, adjust_param)
                if records:
                    # 【防未来函数】二次过滤
                    filtered = self._filter_by_date(records, start_date, end_date)
                    if filtered:
                        self._record_success()
                        self._last_source = source_name
                        logger.debug(f"网源[{source_name}]获取成功 [{symbol}]: {len(filtered)} 条")
                        return MarketDataResult(
                            symbol=symbol,
                            data=filtered,
                            source=f"web_{source_name}",
                            market="A股",
                            period=period,
                            adjust=adjust,
                        )
                # 返回空 → 触发降级（若还有下一源）
                if idx < len(chain) - 1:
                    self._degrade_count += 1
                logger.debug(f"网源[{source_name}]返回空 [{symbol}]，尝试下一源")
            except Exception as e:
                self._record_failure()
                if idx < len(chain) - 1:
                    self._degrade_count += 1
                logger.warning(
                    f"网源[{source_name}]获取失败 [{symbol}]: {type(e).__name__}: {e}，降级下一源"
                )

        return MarketDataResult(symbol, [], source=self.name)

    def get_stock_list(self, market: str = "A股") -> List[Dict]:
        """
        获取 A 股股票列表（三层降级，保证不返回空）

        ① 东财实时行情 stock_zh_a_spot_em（3秒超时）→ 完整列表含名称/行业
        ② 新浪实时行情 stock_zh_a_spot（10秒超时，分页较慢）→ 完整列表含名称
        ③ 本地通达信清单兜底（D:\\TDX 扫描，约5333只，零联网）→ 保证非空

        任一源成功后记录 self._list_source 供 UI 展示。
        """
        ak = self._get_akshare()
        if ak is not None:
            # ① 东财
            try:
                stocks = self._fetch_list_em(ak)
                if stocks:
                    self._list_source = SOURCE_EM
                    return stocks
                logger.debug("东财股票列表返回空，尝试下一源")
            except Exception as e:
                logger.warning(f"东财股票列表获取失败: {type(e).__name__}: {e}，尝试下一源")

            # ② 新浪
            try:
                stocks = self._fetch_list_sina(ak)
                if stocks:
                    self._list_source = SOURCE_SINA
                    return stocks
                logger.debug("新浪股票列表返回空，尝试下一源")
            except Exception as e:
                logger.warning(f"新浪股票列表获取失败: {type(e).__name__}: {e}，尝试下一源")

        # ③ 本地通达信清单兜底（零联网，保证非空）
        stocks = self._fetch_list_tdx()
        if stocks:
            self._list_source = SOURCE_TDX_FALLBACK
            logger.info(f"股票列表使用本地通达信清单兜底，共 {len(stocks)} 只")
            return stocks

        logger.error("股票列表所有来源均失败，返回空")
        return []

    def health_check(self) -> DataSourceStatus:
        """
        健康检查：探测真实行情抓取路径（东财→新浪→腾讯）

        不再只探测东财列表接口（该接口可能被网络封锁），
        而是尝试用降级链抓取单只近 7 个交易日行情，
        任一源成功即判定 HEALTHY，如实反映"能否抓到数据"。
        探测过程不污染 degrade_count / last_source 统计。
        """
        if not self._check_akshare():
            self._update_status(DataSourceStatus.DOWN)
            return DataSourceStatus.DOWN
        ak = self._get_akshare()
        end = datetime.now()
        start = end - timedelta(days=7)
        ak_start = start.strftime("%Y%m%d")
        ak_end = end.strftime("%Y%m%d")
        for _, fetch_fn in self._build_chain(ak):
            try:
                records = fetch_fn(ak, "600519.SH", ak_start, ak_end, "")
                if records:
                    self._update_status(DataSourceStatus.HEALTHY)
                    return DataSourceStatus.HEALTHY
            except Exception:
                # 单个源失败继续尝试下一源，不记降级统计
                continue
        self._update_status(DataSourceStatus.DOWN)
        return DataSourceStatus.DOWN
