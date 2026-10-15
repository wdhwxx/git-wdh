# 文件名：myquant/live/data_feed.py
"""
data_feed.py - 实时行情接入（真实行情源 / 订阅 / 重连 / 分发 / 流式清洗）
=========================================================================
实盘行情数据接入层，负责从数据源获取实时行情、流式清洗、聚合成 bar、分发给策略。

核心能力:
  1. 多数据源支持 — akshare轮询 / 真实实时行情源（腾讯批量 / akshare快照）/ 券商推送（预留）
  2. 实时行情获取 — RealtimeQuoteFetcher：腾讯批量接口(主源) + akshare东财快照(备源) 自动降级
  3. 周期聚合 — RealtimeBarAggregator：高频快照→周期bar(1m/5m/日)，周期切换才产生新bar
  4. 离线模拟 — MockRealtimeSource：确定性模拟实时源，本地离线端到端验证
  5. 订阅管理 — subscribe/unsubscribe/get_subscribed
  6. 流式清洗 — 单条流式校验(_validate_single_bar) + 跳变缓存检测 + 脏数据钩子(_on_dirty)
  7. 行情分发 — 回调机制，将聚合后的行情分发给策略
  8. 断线重连 — 网络异常自动重连，指数退避（防数据源限流）
  9. 行情缓存 — 最新行情内存缓存，策略可查询

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md，live/ 仅 data_feed.py 单文件）
  2. 为实时行情及实盘无缝对接做好充足准备（统一bar结构，后续券商推送直接走同一分发通道）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前及历史数据，时间戳严格校验

防未来函数说明:
  - 行情数据时间戳严格校验，不接受未来时间（>now+5s 直接拒绝）
  - 实时快照时间戳优先取数据源自带行情时间，缺失用到达时刻，均≤当前时间
  - 聚合器只聚合【已发生】快照，同周期复用更新、周期切换才产新bar
  - 策略只收到当前bar及历史bar，不包含未来数据
  - 轮询间隔内只使用已确认的行情，不预判下一根bar
  - 清洗函数确保数据完整性和时间顺序

v1实现范围:
  - 原有 akshare 日线轮询路径（向后兼容，零修改）
  - 新增 realtime 模式：腾讯批量主源 + akshare快照备源 + 周期聚合 + 流式清洗 + 指数退避
  - 新增 MockRealtimeSource 离线模拟实时源
  - 预留事件发布钩子 register_dirty_callback（脏数据联动，B项接事件总线零改动）
  - 为WebSocket/券商推送预留接口（_on_ws_message等）

依赖: typing, time, threading, datetime, requests(可选), akshare(可选),
       myquant.data_access.cleaner, myquant.core.logger
"""

from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path as _Path
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.data_access.cleaner import clean_bar_records, load_clean_config
from myquant.data_access.cleaner.bar_cleaner import _validate_single_bar
from myquant.data_access.cleaner.error_codes import (
    infer_dirty_type_from_errors,
)

logger = get_logger("live.data_feed")


# ═══════════════════════════════════════════════
#  类型别名与常量
# ═══════════════════════════════════════════════

# 行情回调: (symbol: str, bar: Dict[str, Any]) -> None
MarketDataCallback = Callable[[str, Dict[str, Any]], None]

# 异常回调: (symbol: str, error: Exception) -> None
FeedErrorCallback = Callable[[str, Exception], None]

# 脏数据回调: (symbol: str, bar: Dict[str, Any], errors: List[str]) -> None
DirtyDataCallback = Callable[[str, Dict[str, Any], List[str]], None]

# 实时快照字段：symbol/name/price/open/high/low/prev_close/volume/amount/timestamp
QUOTE_FIELDS = ("symbol", "name", "price", "open", "high", "low",
                "prev_close", "volume", "amount", "timestamp")

# 频率 → 聚合周期（秒）
FREQUENCY_PERIOD_MAP = {
    "realtime": 60,
    "1m": 60, "1min": 60,
    "5m": 300, "5min": 300,
    "15m": 900, "15min": 900,
    "30m": 1800, "30min": 1800,
    "60m": 3600, "1h": 3600, "hour": 3600,
    "daily": 86400, "day": 86400, "1d": 86400,
}

# 未来时间容忍（秒）：数据源时钟与本机时钟允许偏差
FUTURE_TOLERANCE_SEC = 5.0

# 腾讯行情字段索引（qt.gtimg.cn 返回 v_sh600519="1~名称~代码~...~";）
_TENCENT_IDX_NAME = 1
_TENCENT_IDX_CODE = 2
_TENCENT_IDX_PRICE = 3
_TENCENT_IDX_PREV_CLOSE = 4
_TENCENT_IDX_OPEN = 5
_TENCENT_IDX_VOLUME_HAND = 6
_TENCENT_IDX_TIME = 30
_TENCENT_IDX_HIGH = 33
_TENCENT_IDX_LOW = 34
_TENCENT_IDX_AMOUNT_WAN = 37


# ═══════════════════════════════════════════════
#  行情数据结构（标准Bar）
# ═══════════════════════════════════════════════

def _make_bar(
    symbol: str,
    date: str,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    amount: float = 0.0,
    period: Optional[str] = None,
) -> Dict[str, Any]:
    """
    创建标准Bar数据结构

    #E1修复：date 字段按周期统一格式化：
      - 日线/周线/月线: "YYYY-MM-DD"
      - 分钟线/小时线/实时: "YYYY-MM-DD HH:MM:SS"

    Parameters
    ----------
    symbol : str
        标的代码
    date : str
        日期字符串（支持 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM:SS"）
    open, high, low, close : float
        OHLC价格
    volume : float
        成交量
    amount : float
        成交额
    period : str | None
        数据周期，决定 date 格式。None 默认按日线处理。

    Returns
    -------
    Dict[str, Any] : 标准Bar字典
    """
    # #E1修复：按周期统一格式化 date 字段
    from myquant.core.date_utils import bar_date_to_epoch, format_bar_date
    formatted_date = format_bar_date(date, period) or str(date)
    return {
        "symbol": symbol,
        "date": formatted_date,
        "open": float(open),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
        "amount": float(amount),
        # #T-04(KP-P3-02)：timestamp 用 bar 周期归属时间（日线归属日 00:00:00），
        # 不用墙钟 time.time()——避免同一根 bar 因墙钟时刻不同被判为不同 bar
        "timestamp": bar_date_to_epoch(formatted_date),
    }


def _infer_market(code: str) -> str:
    """
    根据6位股票代码推断市场（沪深/北）

    Returns
    -------
    str : "sh" | "sz" | "bj"
    """
    code = str(code).strip()
    if code.startswith(("6", "9", "5", "7")):
        return "sh"
    if code.startswith(("0", "2", "3")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    return "sz"


def _to_standard_symbol(code: str) -> str:
    """
    6位代码 → 标准symbol（600519 → 600519.SH）

    Returns
    -------
    str : 标准symbol
    """
    code = str(code).strip()
    market = _infer_market(code)
    return f"{code}.{market.upper()}"


def _to_tencent_code(symbol: str) -> str:
    """
    标准symbol → 腾讯行情代码（600519.SH → sh600519）

    Returns
    -------
    str : 腾讯行情代码
    """
    code, _, market = symbol.partition(".")
    return f"{market.lower()}{code}"


def _period_from_frequency(frequency: str) -> int:
    """频率字符串 → 聚合周期（秒）"""
    return FREQUENCY_PERIOD_MAP.get(str(frequency).lower(), 60)


def _factor_on_or_before(lookup: Dict[str, float], date_str: str) -> Optional[float]:
    """因子表中 <= date_str 的最近因子（as_of 截断，防未来函数）。"""
    if not lookup or not date_str:
        return None
    best: Optional[float] = None
    best_date = ""
    for d, f in lookup.items():
        if d <= date_str:
            if not best_date or d > best_date:
                best_date = d
                best = f
    return best


def _live_jump_change_with_factor(
    prev_close: float,
    cur_close: float,
    prev_date: str,
    cur_date: str,
    factor_lookup: Dict[str, float],
) -> Optional[float]:
    """按复权因子吸收除权缺口后的真实跳变百分比（S2 #83）。

    raw 价在除权事件日会跳空（如高送转 -50%），但 hfq 价连续；
    本函数用事件日前后因子把两条价折算到 hfq 口径后求真实变化率。
    无因子表 / 非事件日（因子未变）返回 None（不做豁免判定）。
    """
    if not factor_lookup or not prev_date or not cur_date:
        return None
    prev_factor = _factor_on_or_before(factor_lookup, prev_date)
    cur_factor = _factor_on_or_before(factor_lookup, cur_date)
    if prev_factor is None or cur_factor is None or prev_factor <= 0:
        return None
    if abs(cur_factor - prev_factor) < 1e-12:
        return None  # 无除权事件
    hfq_prev = prev_close * prev_factor
    hfq_cur = cur_close * cur_factor
    if hfq_prev <= 0:
        return None
    return abs(hfq_cur - hfq_prev) / hfq_prev


def _quote_to_bar(quote: Dict[str, Any]) -> Dict[str, Any]:
    """
    实时快照 → 标准bar（单条，作为聚合周期首根或同周期更新基础）

    #E1修复：date 字段统一为 "YYYY-MM-DD HH:MM:SS"（实时数据天然有完整时间）。

    Returns
    -------
    Dict[str, Any] : 标准bar字典
    """
    price = float(quote.get("price", 0.0))
    ts = float(quote.get("timestamp", time.time()))
    # #E1修复：使用标准化工具格式化 date
    from myquant.core.date_utils import format_bar_date
    return {
        "symbol": quote.get("symbol", ""),
        "date": format_bar_date(ts, "1min"),
        "open": float(quote.get("open", price)),
        "high": float(quote.get("high", price)),
        "low": float(quote.get("low", price)),
        "close": price,
        "volume": float(quote.get("volume", 0.0)),
        "amount": float(quote.get("amount", 0.0)),
        "timestamp": ts,
    }


# ═══════════════════════════════════════════════
#  MockRealtimeSource - 确定性模拟实时行情源
# ═══════════════════════════════════════════════

class MockRealtimeSource:
    """
    确定性模拟实时行情源（本地离线验证用）

    内部时钟从过去某时刻开始逐步推进，每次 fetch_spot 前进 step_interval 秒，
    生成确定性 tick/快照序列（无随机，完全可复现）。

    【防未来函数】内部时钟从过去开始且只递增，时间戳恒≤当前时间。

    使用方式:
        mock = MockRealtimeSource(symbols=["000001.SZ", "600519.SH"])
        quotes = mock.fetch_spot(["000001.SZ"])     # 每次调用推进一个tick
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        start_ts: Optional[float] = None,
        step_interval: float = 1.0,
        base_prices: Optional[Dict[str, float]] = None,
    ):
        """
        初始化模拟实时源

        Parameters
        ----------
        symbols : List[str] | None
            标的列表（默认 000001.SZ / 600519.SH）
        start_ts : float | None
            起始时间戳（默认=当前时间-600秒，即从过去开始）
        step_interval : float
            每次 fetch_spot 推进的秒数（默认1秒，模拟每秒一个tick）
        base_prices : Dict[str, float] | None
            各标的基准价（默认 000001.SZ=10.0 / 600519.SH=1500.0）
        """
        self._symbols = list(symbols or ["000001.SZ", "600519.SH"])
        self._step = float(step_interval)
        self._clock = start_ts if start_ts is not None else (time.time() - 600.0)
        self._count = 0
        self._base = dict(base_prices or {})
        for s in self._symbols:
            if s not in self._base:
                self._base[s] = 1500.0 if s.startswith("600") else 10.0

    def fetch_spot(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        """
        获取实时快照（确定性），每次调用内部时钟前进 step_interval 秒

        Parameters
        ----------
        symbols : List[str] | None
            请求的标的（默认全部标的）

        Returns
        -------
        Dict[str, Dict] : symbol → 快照
        """
        targets = [s for s in (symbols or self._symbols) if s in self._base]
        self._count += 1
        self._clock += self._step
        t = self._count

        quotes: Dict[str, Dict[str, Any]] = {}
        for i, symbol in enumerate(targets):
            base = self._base[symbol]
            # 确定性价格路径：先跌后涨 + 正弦震荡（可复现）
            wave = 0.03 * math.sin(t * 0.3 + i * 1.7)
            drift = 0.001 * t * (0.5 if i % 2 == 0 else 1.0)
            price = base * (1.0 + wave + drift)
            if price <= 0:
                price = base * 0.9

            quotes[symbol] = {
                "symbol": symbol,
                "name": f"模拟{symbol}",
                "price": round(price, 4),
                "open": round(price * (1 - 0.002), 4),
                "high": round(price * (1 + 0.005), 4),
                "low": round(price * (1 - 0.005), 4),
                "prev_close": base,
                "volume": 100000.0,
                "amount": round(price * 100000, 2),
                "timestamp": self._clock,
            }
        return quotes

    @property
    def clock(self) -> float:
        """当前内部时钟（时间戳）"""
        return self._clock

    @property
    def tick_count(self) -> int:
        """已生成的tick数"""
        return self._count


# ═══════════════════════════════════════════════
#  RealtimeBarAggregator - 快照→周期bar聚合器
# ═══════════════════════════════════════════════

class RealtimeBarAggregator:
    """
    高频快照 → 周期bar 聚合器

    同一周期内复用同一根bar并增量更新（high/low/volume/amount），
    周期切换才产生新bar返回（策略只在周期切换时收到新bar，避免重复触发）。

    【防未来函数】只聚合已发生快照；周期key=timestamp//period，恒为已发生周期。

    使用方式:
        agg = RealtimeBarAggregator(period=60)
        new_bar = agg.add_quote(quote)      # 新周期切换→返回新bar；同周期→None
        cur = agg.get_current_bar(symbol)   # 获取当前周期最新聚合bar
    """

    def __init__(self, period: int = 60):
        self._period = max(1, int(period))
        self._bars: Dict[str, Dict[str, Any]] = {}
        self._keys: Dict[str, int] = {}

    def add_quote(self, quote: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        聚合一条实时快照

        Parameters
        ----------
        quote : Dict
            实时快照（含 symbol/price/timestamp/...）

        Returns
        -------
        Optional[Dict] : 周期切换产生的新bar；同周期更新返回None
        """
        symbol = quote.get("symbol", "")
        if not symbol:
            return None
        price = float(quote.get("price", 0.0))
        ts = float(quote.get("timestamp", time.time()))
        period_key = int(ts // self._period)

        # 新周期 → 产新bar
        if symbol not in self._keys or period_key != self._keys[symbol]:
            bar = _quote_to_bar(quote)
            bar["timestamp"] = float(period_key * self._period)  # 周期起始时间戳
            # #E1修复：使用标准化工具格式化 date（聚合器为日内级别，格式 "YYYY-MM-DD HH:MM:SS"）
            from myquant.core.date_utils import format_bar_date
            bar["date"] = format_bar_date(bar["timestamp"], "1min")
            self._bars[symbol] = bar
            self._keys[symbol] = period_key
            return dict(bar)

        # 同周期 → 增量更新当前bar（复用，不产新bar）
        bar = self._bars[symbol]
        bar["close"] = price
        bar["high"] = max(float(bar["high"]), price)
        bar["low"] = min(float(bar["low"]), price)
        bar["volume"] = float(bar.get("volume", 0.0)) + float(quote.get("volume", 0.0))
        bar["amount"] = float(bar.get("amount", 0.0)) + float(quote.get("amount", 0.0))
        return None

    def get_current_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取指定标的最新聚合bar（同周期内的最新状态）"""
        bar = self._bars.get(symbol)
        return dict(bar) if bar else None

    def get_all_bars(self) -> Dict[str, Dict[str, Any]]:
        """获取全部标的最新聚合bar"""
        return {s: dict(b) for s, b in self._bars.items()}

    def reset(self) -> None:
        """清空聚合状态（重连/切换周期时）"""
        self._bars.clear()
        self._keys.clear()


# ═══════════════════════════════════════════════
#  RealtimeQuoteFetcher - 实时行情获取器
# ═══════════════════════════════════════════════

class RealtimeQuoteFetcher:
    """
    实时行情获取器（多后端自动降级）

    后端（免费免注册，符合本地离线偏好）:
      - tencent : 腾讯 qt.gtimg.cn 批量接口（主源，只精确请求订阅标的，轻量）
      - akshare : 东财全量快照 spot_em 过滤（备源，需要全市场/主源失败时）
      - mock    : 内置确定性模拟源（离线验证）

    统一输出快照结构（QUOTE_FIELDS）:
      symbol/name/price/open/high/low/prev_close/volume/amount/timestamp

    【防未来函数】快照时间戳优先取数据源自带行情时间，缺失用到达时刻；
    均为已发生行情，不含任何预测。
    """

    def __init__(
        self,
        backend: str = "tencent",
        timeout: float = 5.0,
        mock_source: Optional[MockRealtimeSource] = None,
    ):
        self._backend = str(backend).lower()
        self._timeout = float(timeout)
        self._mock_source = mock_source or MockRealtimeSource()
        self._active_backend = self._backend
        self._last_error = ""

    @property
    def active_backend(self) -> str:
        """当前生效的后端"""
        return self._active_backend

    @property
    def last_error(self) -> str:
        """最近一次错误"""
        return self._last_error

    def fetch_spot(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取实时快照（自动降级）

        Parameters
        ----------
        symbols : List[str]
            请求的标的列表

        Returns
        -------
        Dict[str, Dict] : symbol → 快照（失败的标的自动跳过）
        """
        if not symbols:
            return {}
        targets = [s for s in symbols if s]

        if self._backend == "mock":
            return self._fetch_mock(targets)

        # 主源：腾讯批量
        if self._backend in ("tencent", "auto", "realtime"):
            quotes = self._fetch_tencent(targets)
            if quotes or self._backend != "auto":
                return quotes

        # 备源：akshare 东财快照
        quotes = self._fetch_akshare_spot(targets)
        return quotes

    # ── 腾讯批量接口（主源） ──

    def _fetch_tencent(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        腾讯 qt.gtimg.cn 批量实时快照

        #E4修复：支持超过50只标的的分批请求，每批50只，合并结果。
        部分批次失败不影响其他批次；全部失败才降级到 akshare。
        为实盘大量标的订阅场景做好准备。
        """
        try:
            import requests
        except ImportError:
            self._last_error = "requests 未安装"
            logger.error("[realtime] requests 未安装，无法使用腾讯源")
            return {}

        if not symbols:
            return {}

        # #E4修复：分批请求，每批最多50只（腾讯接口限制）
        BATCH_SIZE = 50
        total = len(symbols)
        if total > BATCH_SIZE:
            logger.info(f"[realtime] 订阅标的{total}只，超过腾讯单批限制{BATCH_SIZE}只，分批请求")

        quotes: Dict[str, Dict[str, Any]] = {}
        batch_success = 0
        batch_failed = 0

        for batch_idx in range(0, total, BATCH_SIZE):
            batch = symbols[batch_idx:batch_idx + BATCH_SIZE]
            batch_num = batch_idx // BATCH_SIZE + 1
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

            try:
                codes = ",".join(_to_tencent_code(s) for s in batch)
                url = f"https://qt.gtimg.cn/q={codes}"
                resp = requests.get(url, timeout=self._timeout)
                resp.encoding = "gbk"  # 腾讯返回GBK编码
                text = resp.text
            except Exception as e:
                batch_failed += 1
                err = f"腾讯行情第{batch_num}/{total_batches}批请求失败: {type(e).__name__}: {e}"
                logger.warning(f"[realtime] {err}")
                self._last_error = err
                continue

            # 解析本批结果
            batch_count = 0
            for raw in text.strip().splitlines():
                quote = self._parse_tencent_line(raw)
                if quote:
                    quotes[quote["symbol"]] = quote
                    batch_count += 1

            if batch_count > 0:
                batch_success += 1
            else:
                batch_failed += 1
                logger.warning(f"[realtime] 腾讯第{batch_num}/{total_batches}批返回0条有效数据")

        # 全部批次失败才降级到 akshare；部分失败保留已获取数据
        if batch_success == 0 and batch_failed > 0:
            self._last_error = f"腾讯行情全部{batch_failed}批请求失败，降级到akshare"
            logger.warning(f"[realtime] {self._last_error}")
            self._active_backend = "akshare"
            return {}

        if quotes:
            self._active_backend = "tencent"
            if total > BATCH_SIZE:
                logger.info(f"[realtime] 腾讯分批请求完成: {total}只标的，成功{len(quotes)}只，"
                            f"批次成功{batch_success}/失败{batch_failed}")
        return quotes

    def _parse_tencent_line(self, raw: str) -> Optional[Dict[str, Any]]:
        """解析腾讯行情单行：v_sh600519="1~名称~代码~最新~昨收~今开~量~...~时间~...~最高~最低~...~额(万)~...";"""
        m = re.search(r'="(.*)";', raw)
        if not m:
            return None
        f = m.group(1).split("~")
        if len(f) < 35:
            return None
        try:
            code = f[_TENCENT_IDX_CODE]
            name = f[_TENCENT_IDX_NAME]
            price = float(f[_TENCENT_IDX_PRICE])
            prev_close = float(f[_TENCENT_IDX_PREV_CLOSE])
            open_ = float(f[_TENCENT_IDX_OPEN])
            high = float(f[_TENCENT_IDX_HIGH])
            low = float(f[_TENCENT_IDX_LOW])
            volume_hand = float(f[_TENCENT_IDX_VOLUME_HAND])  # 手
            amount_wan = float(f[_TENCENT_IDX_AMOUNT_WAN]) if len(f) > _TENCENT_IDX_AMOUNT_WAN else 0.0
            # 时间 yyyyMMddHHmmss
            raw_ts = f[_TENCENT_IDX_TIME].strip() if len(f) > _TENCENT_IDX_TIME else ""
        except (ValueError, IndexError):
            return None

        if price <= 0:
            return None

        symbol = _to_standard_symbol(code)
        # 数据源自带行情时间（真实行情时刻）；解析失败用到达时刻
        ts = time.time()
        if len(raw_ts) == 14:
            try:
                ts = datetime.strptime(raw_ts, "%Y%m%d%H%M%S").timestamp()
            except ValueError:
                pass
        # 防未来：若数据源时间超前于本机（时钟偏差），回退为到达时刻
        if ts > time.time() + FUTURE_TOLERANCE_SEC:
            ts = time.time()

        return {
            "symbol": symbol,
            "name": name,
            "price": price,
            "open": open_,
            "high": high,
            "low": low,
            "prev_close": prev_close,
            "volume": volume_hand * 100.0,  # 手 → 股
            "amount": amount_wan * 10000.0,  # 万元 → 元
            "timestamp": ts,
        }

    # ── akshare 东财快照（备源） ──

    def _fetch_akshare_spot(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        akshare 东财全量快照过滤（备源）

        需要全市场数据或主源失败时使用；仅提取订阅标的。
        """
        try:
            import akshare as ak
        except ImportError:
            self._last_error = "akshare 未安装，无法使用备源"
            logger.error("[realtime] akshare 未安装，无法使用备源")
            self._active_backend = "mock"
            return {}

        try:
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return {}
            codes = {s.split(".")[0] for s in symbols}
            sub = df[df["代码"].astype(str).isin(codes)]
        except Exception as e:
            self._last_error = f"akshare快照失败: {type(e).__name__}: {e}"
            logger.warning(f"[realtime] {self._last_error}")
            self._active_backend = "mock"
            return {}

        now = time.time()
        quotes: Dict[str, Dict[str, Any]] = {}
        for _, row in sub.iterrows():
            code = str(row.get("代码", ""))
            symbol = _to_standard_symbol(code)
            if symbol not in set(symbols):
                continue
            try:
                price = float(row.get("最新价", 0) or 0)
                if price <= 0:
                    continue
                quotes[symbol] = {
                    "symbol": symbol,
                    "name": str(row.get("名称", "")),
                    "price": price,
                    "open": float(row.get("今开", price) or price),
                    "high": float(row.get("最高", price) or price),
                    "low": float(row.get("最低", price) or price),
                    "prev_close": float(row.get("昨收", price) or price),
                    "volume": float(row.get("成交量", 0) or 0),
                    "amount": float(row.get("成交额", 0) or 0),
                    "timestamp": now,
                }
            except (ValueError, TypeError):
                continue

        if quotes:
            self._active_backend = "akshare"
        return quotes

    # ── mock 模拟源 ──

    def _fetch_mock(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """内置确定性模拟源（离线验证）"""
        self._active_backend = "mock"
        return self._mock_source.fetch_spot(symbols)


# ═══════════════════════════════════════════════
#  MarketDataFeed - 实时行情接入
# ═══════════════════════════════════════════════

class MarketDataFeed:
    """
    实时行情接入（订阅/重连/分发/流式清洗）

    支持两种模式（向后兼容）:
      - 原有模式: source="akshare" 日线轮询（v1，零修改保留）
      - 实时模式: source="realtime"/"realtime_tencent"/"realtime_akshare"/"realtime_mock"
                  真实行情源 + 周期聚合 + 流式清洗 + 指数退避

    使用方式:
        # 实时模式（腾讯主源 + akshare备源，60秒周期bar）
        feed = MarketDataFeed(source="realtime", frequency="1m")
        feed.connect()

        def on_bar(symbol, bar):
            print(f"收到行情: {symbol} close={bar['close']}")

        feed.subscribe("000001.SZ", on_bar)
        feed.subscribe("600519.SH", on_bar)
        feed.register_dirty_callback(lambda s, b, e: print(f"脏数据: {s} {e}"))

        # 启动轮询（后台线程，失败指数退避）
        feed.start(poll_interval=3)

        # 或手动轮询（测试用）
        feed.poll_once()

        # 停止
        feed.stop()
        feed.disconnect()

        # 离线模式（确定性模拟实时源）
        feed = MarketDataFeed(source="realtime_mock", frequency="1m")
        feed.connect()
        feed.subscribe("000001.SZ", on_bar)
        feed.poll_once()  # 每次推进一个tick，聚合器按周期产bar
    """

    def __init__(
        self,
        source: str = "akshare",
        frequency: str = "daily",
        enable_clean: bool = True,
        clean_config: Optional[Dict[str, Any]] = None,
        max_retry: int = 5,
        retry_interval: int = 10,
        realtime_backend: str = "auto",
        mock_source: Optional[MockRealtimeSource] = None,
        bar_period: Optional[int] = None,
        backoff_cap: int = 60,
        local_history_dir: Optional[str] = None,
        factor_svc: Optional[Any] = None,
        adjust_out: str = "hfq",
    ):
        """
        初始化实时行情接入

        Parameters
        ----------
        source : str
            行情数据源：akshare(日线轮询,默认) / realtime / realtime_tencent /
            realtime_akshare / realtime_mock / tdx / custom
        frequency : str
            行情频率：daily / 1m / 5m / 15m / 30m / 60m / realtime（聚合周期）
        enable_clean : bool
            是否启用数据清洗（默认True）
        clean_config : Dict | None
            清洗配置（None则使用默认配置；含 price_jump_threshold 跳变阈值，默认0.15）
        max_retry : int
            最大重试次数（网络异常时）
        retry_interval : int
            重试间隔（秒）
        realtime_backend : str
            实时后端：auto(腾讯主+akshare备) / tencent / akshare / mock
        mock_source : MockRealtimeSource | None
            离线模拟源实例（source=realtime_mock 时使用）
        bar_period : int | None
            聚合周期（秒），None则按 frequency 自动映射
        backoff_cap : int
            指数退避上限（秒），默认60
        factor_svc : Any | None
            复权因子服务（方案B #82），hfq/qfq 现算与除权跳变豁免用；None 懒加载默认 DB 服务
        adjust_out : str
            对外输出口径："hfq"（默认，后复权×因子，与回测一致，除权日不跳变）/
            "qfq"（前复权÷因子）/ "raw"（事实价原样，仅下单层用，指标层禁止静默落 raw）
        """
        self._source = source
        self._local_history_dir = local_history_dir
        self._frequency = frequency
        self._enable_clean = enable_clean
        self._max_retry = max_retry
        self._retry_interval = retry_interval
        self._backoff_cap = int(backoff_cap)
        # S2(#83)：复权现算——factor_svc 可选（None 懒加载默认 DB 服务）；
        #   adjust_out="qfq" 时对分发 bar 按最新因子现算前复权，"raw" 原样透传
        self._factor_svc = factor_svc
        self._adjust_out = str(adjust_out or "hfq").lower()
        self._factor_lookup_cache: Dict[str, Dict[str, float]] = {}

        # 实时模式判定
        self._is_realtime_mode = str(source).lower().startswith("realtime")

        # 聚合周期
        self._period = bar_period or _period_from_frequency(frequency)

        # 清洗配置
        if enable_clean:
            self._clean_config = dict(clean_config or load_clean_config(use_settings=False))
            if "price_jump_threshold" not in self._clean_config:
                self._clean_config["price_jump_threshold"] = 0.15
            # #O1修复：连续跳变告警阈值（连续N次跳变才告警，默认3次；抑制瞬时跳变误报）
            self._jump_alert_threshold = int(self._clean_config.get("jump_alert_threshold", 3))
        else:
            self._clean_config = {}
            self._jump_alert_threshold = 3

        # 订阅管理
        self._subscriptions: Dict[str, List[MarketDataCallback]] = {}
        self._error_callbacks: List[FeedErrorCallback] = []
        self._dirty_callbacks: List[DirtyDataCallback] = []

        # 行情缓存（最新bar）
        self._latest_bars: Dict[str, Dict[str, Any]] = {}
        self._historical_bars: Dict[str, List[Dict[str, Any]]] = {}  # 最近N条bar

        # 运行状态
        self._connected = False
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # 实时组件
        self._fetcher: Optional[RealtimeQuoteFetcher] = None
        self._aggregator: Optional[RealtimeBarAggregator] = None
        self._mock_source = mock_source

        # B项：事件总线联动桥（attach_event_bus 后创建；None=未挂载，原行为不变）
        self._event_bridge: Optional["_RealtimeEventBridge"] = None

        # 退避状态（连续失败次数 + 当前退避间隔）
        self._backoff_count = 0
        self._backoff_interval = 0.0
        # M5修复: 轮询循环异常退出标记（线程意外死亡时供 health_check 判定 down）
        self._poll_error: Optional[str] = None

        # 统计
        self._stats = {
            "total_polls": 0,
            "success_polls": 0,
            "failed_polls": 0,
            "total_bars": 0,
            "dirty_bars": 0,
            "quote_count": 0,
            "last_poll_time": 0.0,
            "last_quote_time": 0.0,
            "last_error": "",
        }

        # #O1修复：跳变连续计数（按symbol记录连续跳变次数，达到阈值才告警）
        self._jump_streak: Dict[str, int] = {}

        logger.info(
            f"实时行情接入初始化: source={source}, frequency={frequency}, "
            f"period={self._period}s, clean={'启用' if enable_clean else '禁用'}"
        )

    # ═══════════════════════════════════════════
    #  连接管理
    # ═══════════════════════════════════════════

    def connect(self) -> bool:
        """
        连接行情数据源

        实时模式：初始化实时获取器（按后端）+ 周期聚合器。
        v1 akshare: 只做可用性检测。

        Returns
        -------
        bool : True=连接成功
        """
        if self._is_realtime_mode:
            backend = "auto"
            if self._source == "realtime_tencent":
                backend = "tencent"
            elif self._source == "realtime_akshare":
                backend = "akshare"
            elif self._source == "realtime_mock":
                backend = "mock"
            else:
                backend = self._source.replace("realtime", "auto")
            if backend == "auto":
                backend = "tencent" if self._source in ("realtime",) else "auto"

            self._fetcher = RealtimeQuoteFetcher(
                backend=backend,
                mock_source=self._mock_source,
            )
            self._aggregator = RealtimeBarAggregator(period=self._period)
            self._connected = True
            logger.info(f"[data_feed] 实时行情源已连接: source={self._source}, "
                        f"backend={self._fetcher.active_backend}, period={self._period}s")
            return True

        if self._source == "akshare":
            try:
                import akshare as ak  # noqa: F401
                self._connected = True
                logger.info("[data_feed] akshare行情源已连接（轮询模式）")
                return True
            except ImportError as e:
                self._stats["last_error"] = f"akshare未安装: {e}"
                logger.error(f"[data_feed] akshare未安装: {e}")
                return False
        elif self._source == "tdx":
            # TODO: 后续迭代实现通达信实时行情连接
            self._connected = True
            logger.info("[data_feed] 通达信行情源已连接（预留）")
            return True
        else:
            self._connected = True
            logger.info(f"[data_feed] 自定义行情源已连接: {self._source}")
            return True

    def disconnect(self) -> None:
        """断开行情数据源"""
        self.stop()
        self._connected = False
        if self._aggregator is not None:
            self._aggregator.reset()
        # B项：断开时 flush 剩余聚合窗口（未发布的脏数据事件补发）
        if self._event_bridge is not None:
            try:
                self._event_bridge.flush_all()
            except Exception as e:
                logger.error(f"[data_feed] 事件桥flush异常: {e}")
        logger.info("[data_feed] 行情源已断开")

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected

    @property
    def is_running(self) -> bool:
        """是否正在运行（轮询中）"""
        return self._running

    @property
    def is_realtime(self) -> bool:
        """是否实时模式"""
        return self._is_realtime_mode

    @property
    def active_backend(self) -> str:
        """当前生效的实时行情后端"""
        if self._fetcher is not None:
            return self._fetcher.active_backend
        return ""

    # ═══════════════════════════════════════════
    #  订阅管理
    # ═══════════════════════════════════════════

    def subscribe(
        self,
        symbol: str,
        callback: Optional[MarketDataCallback] = None,
    ) -> bool:
        """
        订阅标的行情

        Parameters
        ----------
        symbol : str
            标的代码，如 "000001.SZ"
        callback : MarketDataCallback | None
            行情回调函数 (symbol, bar) -> None

        Returns
        -------
        bool : True=订阅成功
        """
        with self._lock:
            if symbol not in self._subscriptions:
                self._subscriptions[symbol] = []
            if callback is not None and callback not in self._subscriptions[symbol]:
                self._subscriptions[symbol].append(callback)
        logger.info(f"[data_feed] 已订阅: {symbol}")
        return True

    def unsubscribe(self, symbol: str) -> bool:
        """
        取消订阅标的行情

        Parameters
        ----------
        symbol : str
            标的代码

        Returns
        -------
        bool : True=取消成功
        """
        with self._lock:
            if symbol in self._subscriptions:
                del self._subscriptions[symbol]
        logger.info(f"[data_feed] 已取消订阅: {symbol}")
        return True

    def get_subscribed_symbols(self) -> List[str]:
        """获取当前订阅的标的列表"""
        with self._lock:
            return sorted(self._subscriptions.keys())

    def is_subscribed(self, symbol: str) -> bool:
        """是否已订阅某标的"""
        with self._lock:
            return symbol in self._subscriptions

    def register_error_callback(self, callback: FeedErrorCallback) -> None:
        """注册异常回调"""
        if callback not in self._error_callbacks:
            self._error_callbacks.append(callback)

    def register_dirty_callback(self, callback: DirtyDataCallback) -> None:
        """
        注册脏数据回调（事件发布预留钩子）

        实时流式清洗检出脏数据时调用 (symbol, bar, errors) -> None。
        【B项】事件总线（DataQualityEvent发布）在此钩子挂载，零改动接入。
        """
        if callback not in self._dirty_callbacks:
            self._dirty_callbacks.append(callback)

    def attach_event_bus(
        self,
        event_bus: Optional[Any] = None,
        source: str = "live",
        severity_map: Optional[Dict[str, str]] = None,
        aggregate_window: int = 60,
        critical_after: int = 3,
        enabled: bool = True,
    ) -> "_RealtimeEventBridge":
        """
        挂载事件总线（B项：流式清洗 → DataQualityEvent 发布）

        复用 register_dirty_callback 钩子，经【窗口聚合 + 连续阈值升级】后
        发布 DataQualityEvent，自动联动：
          - 任务10 AlertManager：分级告警（critical→CRITICAL通道）
          - 任务11 MetricsCollector：脏数据占比指标自动采集
          - 任务12 RiskManager：严重脏数据自动暂停策略

        【防未来函数】事件时间戳=窗口检出时刻，恒≤当前时间。
        【向后兼容】不调用本方法时 _event_bridge=None，原行为完全不变。

        Parameters
        ----------
        event_bus : EventBus | None
            事件总线实例；None 时自动使用全局单例 get_event_bus()（一行挂载）
        source : str
            事件来源标识（默认 "live"，区分离线清洗 "data_clean"）
        severity_map : Dict[str, str] | None
            自定义 dirty_type → severity 覆盖映射（如 {"price_jump": "critical"}）
        aggregate_window : int
            聚合窗口秒数（同 symbol+dirty_type 窗口内合并为一条事件，避免事件风暴）
        critical_after : int
            严重类型连续发生次数达到此值才升级为 critical（过滤瞬时抖动，
            避免单 tick 误报触发风控暂停）；单次仅 warning
        enabled : bool
            是否启用发布（False 时只内部计数不发布，便于调试/灰度）

        Returns
        -------
        _RealtimeEventBridge : 联动桥实例（可查询 published_count / clean_streak）
        """
        if event_bus is None:
            from myquant.core.event_bus import get_event_bus
            event_bus = get_event_bus()
        bridge = _RealtimeEventBridge(
            event_bus=event_bus,
            source=source,
            severity_map=severity_map,
            aggregate_window=aggregate_window,
            critical_after=critical_after,
            enabled=enabled,
        )
        self._event_bridge = bridge
        self.register_dirty_callback(bridge.on_dirty)
        logger.info(
            f"[data_feed] 事件总线已挂载: source={source}, "
            f"window={aggregate_window}s, critical_after={critical_after}, "
            f"enabled={enabled}"
        )
        return bridge

    # ═══════════════════════════════════════════
    #  行情获取（核心）
    # ═══════════════════════════════════════════

    def poll_once(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        单次轮询所有订阅标的的行情

        实时模式：批量快照 → 周期聚合 → 流式清洗 → 分发。
        原有模式：逐标的获取 → 清洗 → 分发。

        Returns
        -------
        Dict[str, List[Dict]] : {symbol: [bar, ...]} 本次新产生的bar
        """
        if not self._connected:
            logger.warning("[data_feed] 未连接，无法轮询")
            return {}

        self._stats["total_polls"] += 1
        self._stats["last_poll_time"] = time.time()

        if self._is_realtime_mode:
            return self._poll_realtime_once()

        symbols = self.get_subscribed_symbols()
        if not symbols:
            logger.debug("[data_feed] 无订阅标的，跳过轮询")
            return {}

        all_bars: Dict[str, List[Dict[str, Any]]] = {}

        for symbol in symbols:
            try:
                bars = self._fetch_latest_bars(symbol)
                if bars:
                    # 清洗
                    if self._enable_clean:
                        bars = self._clean_bars(symbol, bars)

                    # 更新缓存
                    self._update_cache(symbol, bars)

                    # 分发
                    self._dispatch(symbol, bars)

                    all_bars[symbol] = bars
                    self._stats["success_polls"] += 1
                else:
                    logger.debug(f"[data_feed] {symbol} 无新行情")

            except Exception as e:
                self._stats["failed_polls"] += 1
                self._stats["last_error"] = str(e)
                logger.error(f"[data_feed] 获取 {symbol} 行情失败: {type(e).__name__}: {e}")
                # 触发异常回调
                for cb in self._error_callbacks:
                    try:
                        cb(symbol, e)
                    except Exception as cb_e:
                        logger.debug(f"[data_feed] 错误回调异常: {cb_e}")

        return all_bars

    def _poll_realtime_once(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        实时模式单次轮询（核心）

        流程:
          1. 批量获取订阅标的实时快照（一次请求，按后端自动降级）
          2. 每标的：防未来校验 → 周期聚合（周期切换产新bar）
          3. 新bar流式清洗（单条校验+跳变缓存检测+脏数据钩子）
          4. 更新缓存 + 分发（只分发周期切换的新bar，避免重复触发策略）
          5. 失败指数退避（防数据源限流）
        """
        symbols = self.get_subscribed_symbols()
        if not symbols:
            logger.debug("[data_feed] 无订阅标的，跳过轮询")
            return {}

        if self._fetcher is None or self._aggregator is None:
            self._stats["last_error"] = "实时组件未初始化"
            return {}

        quotes = self._fetcher.fetch_spot(symbols)
        if not quotes:
            # 失败退避
            self._backoff_count += 1
            self._backoff_interval = min(
                self._retry_interval * (2 ** (self._backoff_count - 1)),
                self._backoff_cap,
            )
            self._stats["failed_polls"] += 1
            self._stats["last_error"] = f"实时行情获取为空 (backend={self._fetcher.active_backend})"
            logger.warning(f"[data_feed] {self._stats['last_error']}")
            return {}

        # 成功 → 重置退避
        self._backoff_count = 0
        self._backoff_interval = 0.0
        self._stats["success_polls"] += 1

        now = time.time()
        new_bars: Dict[str, List[Dict[str, Any]]] = {}

        for symbol, quote in quotes.items():
            try:
                # 防未来：时间戳超过当前时间+容忍度 → 拒绝
                ts = float(quote.get("timestamp", now))
                if ts > now + FUTURE_TOLERANCE_SEC:
                    logger.warning(f"[data_feed] 拒绝未来时间行情: {symbol} ts={ts} > now={now}",
                                   extra={"source": "live", "symbol": symbol})
                    self._stats["dirty_bars"] += 1
                    continue

                self._stats["quote_count"] += 1
                self._stats["last_quote_time"] = ts

                # 周期聚合：周期切换产新bar，同周期更新缓存
                bar = self._aggregator.add_quote(quote)

                if bar is not None:
                    # 新周期bar → 流式清洗
                    if self._enable_clean:
                        cleaned = self._stream_clean(symbol, bar)
                        if cleaned is None:
                            continue
                        bar = cleaned

                    # 更新缓存 + 分发（周期切换才分发）
                    self._update_cache(symbol, [bar])
                    self._dispatch(symbol, [bar])
                    # B项：干净bar → 事件桥 clean streak 计数（该标的连续脏数据计数清零）
                    if self._event_bridge is not None:
                        try:
                            self._event_bridge.on_clean(symbol)
                        except Exception as e:
                            logger.error(f"[data_feed] 事件桥on_clean异常: {e}")
                    if symbol not in new_bars:
                        new_bars[symbol] = []
                    new_bars[symbol].append(bar)
                else:
                    # 同周期更新 → 只更新缓存（不重复分发）
                    cur = self._aggregator.get_current_bar(symbol)
                    if cur is not None:
                        self._update_cache(symbol, [cur])

            except Exception as e:
                self._stats["last_error"] = str(e)
                logger.error(f"[data_feed] 实时处理 {symbol} 失败: {type(e).__name__}: {e}")

        return new_bars

    def _fetch_from_local_history(self, symbol: str) -> List[Dict[str, Any]]:
        """
        从本地日线 parquet 读取最新 bar（tdx 本地源，离线秒级，零网络零限流）

        processed/<symbol>.parquet 由增量清洗通道（tdx）补齐到最新交易日，
        中文列：开盘价/最高价/最低价/收盘价/成交量/成交额/日期。
        文件缺失/损坏/缺列返回空列表，不阻断轮询。
        """
        if not self._local_history_dir:
            return []
        parquet_path = _Path(self._local_history_dir) / f"{symbol}.parquet"
        try:
            import pandas as pd
            df = pd.read_parquet(parquet_path)
        except Exception as e:
            logger.debug(f"[data_feed] 本地无parquet {symbol}: {type(e).__name__}: {e}")
            return []
        if df is None or df.empty or "日期" not in df.columns:
            return []
        required = {"开盘价", "最高价", "最低价", "收盘价"}
        if not required.issubset(df.columns):
            logger.warning(f"[data_feed] 本地parquet缺列 {symbol}: 需{required}")
            return []
        try:
            df = df.sort_values("日期").tail(1)
        except Exception as e:
            logger.warning(f"[data_feed] 本地parquet排序失败 {symbol}: {e}")
            return []
        bars: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                date_val = row.get("日期")
                if isinstance(date_val, (int, float)):
                    d = int(date_val)
                    date_str = (f"{d // 10000:04d}-{(d % 10000) // 100:02d}-{d % 100:02d}"
                                if d >= 10000000 else "")
                else:
                    date_str = str(date_val)[:10] if date_val is not None else ""
                ts = (datetime.strptime(date_str, "%Y-%m-%d").timestamp()
                      if date_str else time.time())
                bars.append({
                    "symbol": symbol,
                    "date": date_str,
                    "open": float(row["开盘价"]),
                    "high": float(row["最高价"]),
                    "low": float(row["最低价"]),
                    "close": float(row["收盘价"]),
                    "volume": float(row.get("成交量", 0)),
                    "amount": float(row.get("成交额", 0)),
                    "timestamp": ts,
                })
            except (ValueError, TypeError, KeyError):
                continue
        return bars

    def _fetch_latest_bars(self, symbol: str) -> List[Dict[str, Any]]:
        """
        从数据源获取最新行情（原有模式: akshare轮询）

        Parameters
        ----------
        symbol : str
            标的代码

        Returns
        -------
        List[Dict] : 最新bar列表（通常1条，日线频率）
        """
        if self._source == "akshare":
            return self._fetch_from_akshare(symbol)
        elif self._source == "tdx":
            # 本地通达信/processed：离线秒级读取最新bar，零网络零限流
            return self._fetch_from_local_history(symbol)
        else:
            logger.debug(f"[data_feed] 自定义行情源: {symbol}")
            return []

    def _fetch_from_akshare(self, symbol: str) -> List[Dict[str, Any]]:
        """
        从akshare获取最新行情（新浪源优先，原有日线轮询路径）

        v1: 获取最近5个交易日的数据，返回最新1条。
        获取多条是为了清洗时能检测异常（如价格跳变）。
        """
        try:
            import akshare as ak
        except ImportError:
            logger.error("[data_feed] akshare未安装")
            return []

        # 计算日期范围（最近10个自然日，覆盖5个交易日）
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")

        # 新浪源symbol格式：sz000001 / sh600519
        code = symbol.split(".")[0]
        market = symbol.split(".")[1].lower() if "." in symbol else "sz"
        sina_symbol = f"{market}{code}"

        try:
            # S2(#83)：统一不复权 raw 事实（adjust=""），与腾讯快照/本地历史同口径；
            #   复权价由消费端按 adjust_out 现算（_maybe_adjust_bar），消除量纲错位
            df = ak.stock_zh_a_daily(
                symbol=sina_symbol,
                start_date=start_date,
                end_date=end_date,
                adjust="",
            )
            if df is None or df.empty:
                return []

            # 转换为标准bar格式
            bars = []
            for _, row in df.iterrows():
                bar = _make_bar(
                    symbol=symbol,
                    date=str(row.get("date", ""))[:10],
                    open=float(row.get("open", 0)),
                    high=float(row.get("high", 0)),
                    low=float(row.get("low", 0)),
                    close=float(row.get("close", 0)),
                    volume=float(row.get("volume", 0)),
                    amount=float(row.get("amount", 0)),
                )
                bars.append(bar)

            # 只返回最新的1条（实时行情），但保留历史用于清洗
            return bars[-5:] if len(bars) >= 5 else bars

        except Exception as e:
            logger.warning(f"[data_feed] akshare获取 {symbol} 失败: {e}")
            return []

    # ═══════════════════════════════════════════
    #  数据清洗
    # ═══════════════════════════════════════════

    def _stream_clean(self, symbol: str, bar: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        实时单条流式清洗

        复用 _validate_single_bar 做单条基础校验（OHLC/非负/symbol/时间戳）；
        非法 → 丢弃 + 脏数据计数 + 告警 + _on_dirty 钩子（返回None）。
        再基于缓存做跨bar价格跳变检测；跳变 → 告警 + _on_dirty（不丢弃，真实行情可能跳变）。

        Parameters
        ----------
        symbol : str
            标的代码
        bar : Dict
            待清洗的单条bar

        Returns
        -------
        Optional[Dict] : 清洗后的有效bar；非法则返回None（丢弃）
        """
        try:
            errors = _validate_single_bar(bar, self._clean_config)
        except Exception as e:
            errors = [f"清洗异常: {e}"]

        if errors:
            self._stats["dirty_bars"] += 1
            logger.warning(f"[data_feed] 脏数据告警: {symbol} "
                           f"date={bar.get('date', '?')} errors={errors}",
                           extra={"source": "live", "symbol": symbol})
            self._notify_dirty(symbol, bar, errors)
            return None

        # 跨bar价格跳变检测（基于最新缓存close）
        # #O1修复：连续跳变次数阈值，抑制瞬时跳变误报和告警风暴
        jump_threshold = float(self._clean_config.get("price_jump_threshold", 0.15))
        prev_close = None
        prev_bar = None
        with self._lock:
            prev = self._latest_bars.get(symbol)
            if prev:
                prev_close = prev.get("close")
                prev_bar = prev
        if prev_close and prev_close > 0 and bar.get("close", 0) > 0:
            change = abs(bar["close"] - prev_close) / prev_close
            if change > jump_threshold:
                # S2(#83)：除权事件日 raw 缺口被因子吸收 → 视为正常行情，重置连续计数
                exempt = _live_jump_change_with_factor(
                    prev_close,
                    float(bar.get("close", 0)),
                    str(prev_bar.get("date", ""))[:10] if prev_bar else "",
                    str(bar.get("date", ""))[:10],
                    self._get_factor_lookup_cached(symbol),
                )
                if exempt is not None and exempt <= jump_threshold:
                    if self._jump_streak.get(symbol, 0) > 0:
                        self._jump_streak[symbol] = 0
                else:
                    # 连续跳变计数 +1
                    streak = self._jump_streak.get(symbol, 0) + 1
                    self._jump_streak[symbol] = streak
                    msg = f"价格跳变 {change*100:.1f}% > {jump_threshold*100:.0f}%"
                    if streak >= self._jump_alert_threshold:
                        # 达到连续阈值才告警
                        self._stats["dirty_bars"] += 1
                        logger.warning(f"[data_feed] 跳变告警(连续{streak}次): {symbol} {msg} "
                                       f"prev={prev_close} now={bar['close']}",
                                       extra={"source": "live", "symbol": symbol})
                        self._notify_dirty(symbol, bar, [msg])
                    else:
                        # 未达阈值只记录debug，不告警（抑制瞬时跳变误报）
                        logger.debug(f"[data_feed] 跳变检测(连续{streak}/{self._jump_alert_threshold}次): {symbol} {msg} "
                                    f"prev={prev_close} now={bar['close']}")
                # 跳变是真实行情可能，不丢弃
            else:
                # 无跳变，重置连续计数
                if self._jump_streak.get(symbol, 0) > 0:
                    self._jump_streak[symbol] = 0

        self._stats["total_bars"] += 1
        return bar

    def _notify_dirty(self, symbol: str, bar: Dict[str, Any], errors: List[str]) -> None:
        """触发脏数据回调（事件发布预留钩子，B项挂 DataQualityEvent）"""
        callbacks = list(self._dirty_callbacks)
        for cb in callbacks:
            try:
                cb(symbol, bar, errors)
            except Exception as e:
                logger.error(f"[data_feed] 脏数据回调异常: {e}")

    def _clean_bars(
        self,
        symbol: str,
        bars: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        清洗bar数据（原有批量路径）

        调用 clean_bar_records 清洗，脏数据记录告警，不返回脏数据。
        S2(#83)：有因子时注入 factor_lookup（config 副本，不污染共享配置），
        除权缺口跳变被因子吸收不误剔。
        """
        if not bars:
            return []

        cfg = self._clean_config
        lookup = self._get_factor_lookup_cached(symbol)
        if lookup:
            cfg = dict(self._clean_config)
            cfg["factor_lookup"] = lookup

        try:
            result = clean_bar_records(
                records=bars,
                config=cfg,
            )
            # clean_bar_records 返回 (cleaned, bad, stats) 三元组
            if isinstance(result, tuple) and len(result) >= 2:
                cleaned = result[0]
                bad = result[1]
            else:
                # 兼容dict返回格式
                cleaned = result.get("cleaned", []) if isinstance(result, dict) else bars
                bad = result.get("bad", []) if isinstance(result, dict) else []

            if bad:
                self._stats["dirty_bars"] += len(bad)
                for b in bad:
                    errors = b.get("_errors", []) if isinstance(b, dict) else []
                    logger.warning(
                        f"[data_feed] 脏数据告警: {symbol} "
                        f"date={b.get('date', '?') if isinstance(b, dict) else '?'} errors={errors}"
                    )

            self._stats["total_bars"] += len(bars)
            return cleaned

        except Exception as e:
            logger.error(f"[data_feed] 清洗 {symbol} 数据失败: {e}")
            # 清洗失败时返回原始数据（不阻塞行情分发）
            return bars

    # ═══════════════════════════════════════════
    #  复权现算与除权跳变豁免（S2 #83）
    # ═══════════════════════════════════════════

    def _ensure_factor_svc(self) -> None:
        """懒加载复权因子服务（DB 不存在时静默降级为 None）。"""
        if self._factor_svc is not None:
            return
        from myquant.data_access.factor_sync_service import FactorSyncService

        project_root = _Path(__file__).resolve().parents[2]
        db_path = project_root / "database" / "quant.db"
        if db_path.exists():
            try:
                self._factor_svc = FactorSyncService(project_root, db_path=db_path)
            except Exception as e:
                logger.error(f"初始化复权因子服务失败: {type(e).__name__}: {e}")
                self._factor_svc = None

    def _get_factor_lookup_cached(self, symbol: str) -> Dict[str, float]:
        """按 symbol 缓存的因子事件表 {event_date: factor}（as_of=今天，防未来函数）。"""
        cached = self._factor_lookup_cache.get(symbol)
        if cached is not None:
            return cached
        self._ensure_factor_svc()
        if self._factor_svc is None:
            return {}
        try:
            lookup = self._factor_svc.get_factor_lookup(symbol)
        except Exception as e:
            logger.error(f"读取复权因子失败[{symbol}]: {type(e).__name__}: {e}")
            return {}
        self._factor_lookup_cache[symbol] = lookup
        return lookup

    def _maybe_adjust_bar(self, bar: Dict[str, Any]) -> Dict[str, Any]:
        """adjust_out 时按最新因子现算复权（qfq=raw÷因子，hfq=raw×因子）。

        仅当该标的有因子事件时折算；无因子/因子无效保持原值（降级 raw）。
        T3-1：指标层统一 hfq（与回测 backtest_config.adjust=hfq 一致），
        除权日不跳变；下单层仍用 broker 真实 raw 价（券商认的就是这个）。
        """
        if self._adjust_out not in ("qfq", "hfq") or not bar:
            return bar
        factors = self._get_factor_lookup_cached(str(bar.get("symbol", "")))
        if not factors:
            return bar
        latest = max(factors.values())
        if not latest or latest <= 0:
            return bar
        out = dict(bar)
        for key in ("open", "high", "low", "close"):
            if key in out:
                try:
                    val = float(out[key])
                    if self._adjust_out == "qfq":
                        out[key] = val / latest
                    else:  # hfq
                        out[key] = val * latest
                except (ValueError, TypeError):
                    pass
        return out

    # ═══════════════════════════════════════════
    #  行情缓存
    # ═══════════════════════════════════════════

    def _update_cache(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        """更新行情缓存（S2 #83：adjust_out 非 raw 时先现算复权；T3-1 hfq 纳入）"""
        if not bars:
            return
        if self._adjust_out in ("qfq", "hfq"):
            bars = [self._maybe_adjust_bar(b) for b in bars]

        with self._lock:
            # 更新最新bar
            self._latest_bars[symbol] = bars[-1]

            # 更新历史bar（保留最近100条）
            if symbol not in self._historical_bars:
                self._historical_bars[symbol] = []
            self._historical_bars[symbol].extend(bars)
            # 只保留最近100条
            if len(self._historical_bars[symbol]) > 100:
                self._historical_bars[symbol] = self._historical_bars[symbol][-100:]

    def get_latest_bar(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取指定标的的最新行情"""
        with self._lock:
            return self._latest_bars.get(symbol)

    def get_historical_bars(self, symbol: str, count: int = 10) -> List[Dict[str, Any]]:
        """
        获取指定标的的历史行情（最近N条）

        Parameters
        ----------
        symbol : str
            标的代码
        count : int
            返回条数

        Returns
        -------
        List[Dict] : 历史bar列表（按时间升序）
        """
        with self._lock:
            bars = self._historical_bars.get(symbol, [])
            return bars[-count:] if len(bars) >= count else bars

    # ═══════════════════════════════════════════
    #  行情分发
    # ═══════════════════════════════════════════

    def _dispatch(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        """
        分发行情给订阅回调

        只分发最新的1条bar（实时行情）。
        历史bar通过 get_historical_bars() 查询。

        Parameters
        ----------
        symbol : str
            标的代码
        bars : List[Dict]
            bar列表
        """
        if not bars:
            return

        latest_bar = bars[-1]

        with self._lock:
            callbacks = list(self._subscriptions.get(symbol, []))

        for callback in callbacks:
            try:
                callback(symbol, latest_bar)
            except Exception as e:
                logger.error(f"[data_feed] 行情回调异常 ({symbol}): {e}")

    # ═══════════════════════════════════════════
    #  轮询循环（后台线程，含指数退避）
    # ═══════════════════════════════════════════

    def start(self, poll_interval: int = 60) -> None:
        """
        启动后台轮询线程

        实时模式建议 poll_interval=3（秒），配合聚合周期产bar。
        失败自动指数退避（防数据源限流）。

        Parameters
        ----------
        poll_interval : int
            轮询间隔（秒），默认60秒
        """
        if self._running:
            logger.warning("[data_feed] 已在运行中")
            return

        if not self._connected:
            logger.error("[data_feed] 未连接，无法启动")
            return

        self._stop_event.clear()
        self._poll_error = None
        self._running = True

        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            args=(poll_interval,),
            daemon=True,
            name="MarketDataFeed",
        )
        self._poll_thread.start()

        logger.info(f"[data_feed] 轮询已启动，间隔={poll_interval}秒")

    def _poll_loop(self, poll_interval: int) -> None:
        """轮询循环（后台线程，失败指数退避；M5: 异常退出兜底复位）"""
        try:
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                except Exception as e:
                    logger.error(f"[data_feed] 轮询异常: {e}")

                # 等待间隔或停止信号（失败时使用退避间隔）
                wait = self._backoff_interval if self._backoff_interval > 0 else float(poll_interval)
                self._stop_event.wait(timeout=wait)
        except Exception as e:
            # M5修复: 循环意外退出兜底——记录错误供 health_check 判定，
            # 防 running 标志悬挂导致监控误判引擎在运行
            logger.error(f"[data_feed] 轮询循环异常退出: {e}")
            self._poll_error = f"{type(e).__name__}: {e}"
            raise
        finally:
            self._running = False
            logger.info("[data_feed] 轮询已停止")

    def stop(self) -> None:
        """停止后台轮询线程"""
        if not self._running:
            return

        self._stop_event.set()

        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=10)

        self._running = False
        logger.info("[data_feed] 已请求停止")

    # ═══════════════════════════════════════════
    #  WebSocket推送预留接口（后续迭代）
    # ═══════════════════════════════════════════

    def _on_ws_message(self, message: str) -> None:
        """
        WebSocket消息处理（预留，后续迭代实现）

        券商WebSocket推送行情时调用此方法，
        解析消息后调用 _dispatch 分发给策略。
        """
        # TODO: 后续迭代实现WebSocket消息解析和分发
        logger.debug(f"[data_feed] WS消息（预留）: {message[:100]}")

    def _on_ws_connect(self) -> None:
        """WebSocket连接成功回调（预留）"""
        logger.info("[data_feed] WebSocket已连接（预留）")

    def _on_ws_disconnect(self) -> None:
        """WebSocket断开回调（预留）"""
        logger.warning("[data_feed] WebSocket已断开（预留）")

    # ═══════════════════════════════════════════
    #  统计与健康检查
    # ═══════════════════════════════════════════

    @property
    def stats(self) -> Dict[str, Any]:
        """行情接入统计（只读）"""
        return dict(self._stats)

    def health_check(self) -> Dict[str, Any]:
        """
        健康检查

        实时模式额外返回：active_backend / last_quote_time / quote_count / period

        Returns
        -------
        Dict[str, Any] :
            {
                "source": str,
                "frequency": str,
                "connected": bool,
                "running": bool,
                "subscribed_count": int,
                "stats": Dict,
                "status": "healthy" | "degraded" | "down",
                "realtime": {...},   # 仅实时模式
            }
        """
        subscribed_count = len(self.get_subscribed_symbols())

        # M5修复: 轮询线程存活探针——running 标志与线程实际状态分离，
        # 线程意外死亡（非 Exception 退出/外部终止）时不再误报健康
        thread_alive = (
            self._poll_thread is not None and self._poll_thread.is_alive()
        )

        if not self._connected:
            status = "down"
        elif self._poll_error:
            status = "down"
            logger.error(
                f"[data_feed] 轮询循环异常已退出（兜底标记 down）: {self._poll_error}"
            )
        elif self._running and not thread_alive:
            status = "down"
            logger.error("[data_feed] 轮询线程已退出但运行标志未复位（兜底标记 down）")
        elif self._stats["failed_polls"] > self._stats["success_polls"] and self._stats["total_polls"] > 0:
            status = "degraded"
        else:
            status = "healthy"

        result: Dict[str, Any] = {
            "source": self._source,
            "frequency": self._frequency,
            "connected": self._connected,
            "running": self._running,
            "thread_alive": thread_alive,
            "poll_error": self._poll_error,
            "subscribed_count": subscribed_count,
            "subscribed_symbols": self.get_subscribed_symbols(),
            "enable_clean": self._enable_clean,
            "stats": dict(self._stats),
            "status": status,
        }

        if self._is_realtime_mode:
            result["realtime"] = {
                "active_backend": self.active_backend,
                "period": self._period,
                "last_quote_time": self._stats.get("last_quote_time", 0.0),
                "quote_count": self._stats.get("quote_count", 0),
                "backoff_interval": self._backoff_interval,
                "backoff_count": self._backoff_count,
            }

        return result

    def __repr__(self) -> str:
        return (
            f"MarketDataFeed(source={self._source}, "
            f"frequency={self._frequency}, "
            f"realtime={self._is_realtime_mode}, "
            f"connected={self._connected}, "
            f"running={self._running}, "
            f"subscribed={len(self.get_subscribed_symbols())})"
        )


# ═══════════════════════════════════════════════
#  _RealtimeEventBridge - 实时清洗→事件总线联动桥
# ═══════════════════════════════════════════════

class _RealtimeEventBridge:
    """
    实时流式清洗 → 事件总线联动桥（B项核心）

    职责:
      1. 窗口聚合：同 (symbol, dirty_type) 在 aggregate_window 秒内合并为一条
         DataQualityEvent（bad_count/total_count 累加），窗口翻转才发布
         —— 避免每 tick 刷事件导致事件风暴/告警风暴。
      2. 连续阈值升级：严重类型（OHLC非法/负价/负量/时间戳无效/缺symbol）
         连续发生 ≥ critical_after 次才上报 critical；单次仅 warning
         —— 过滤瞬时抖动，避免单 tick 误报触发风控暂停。
      3. 线程安全：轮询在后台线程，聚合/计数状态加锁；发布在锁外执行。
      4. clean streak：连续干净 bar 计数（供观测；恢复仍由任务12机制负责）。
      5. 故障隔离：事件发布失败不影响行情分发（try/except 包裹）。

    【防未来函数】事件时间戳=窗口检出时刻（publish_data_quality 内部取当前时间），
    恒≤当前时间；不含任何未来数据。

    使用方式（通常通过 MarketDataFeed.attach_event_bus 创建）:
        bridge = _RealtimeEventBridge(event_bus=bus)
        feed.register_dirty_callback(bridge.on_dirty)   # 脏数据入口
        bridge.on_clean(symbol)                          # 干净bar计数
        bridge.flush_all()                               # 停止时补发
    """

    # 严重错误类型（连续达到 critical_after 才升级为 critical）
    _CRITICAL_TYPES = frozenset({
        "ohlc_logic_error",
        "negative_price",
        "negative_volume",
        "invalid_timestamp",
        "missing_symbol",
    })
    # 直接 warning 类型（不升级，真实行情可能发生）
    _WARNING_TYPES = frozenset({
        "price_jump",
        "volume_anomaly",
        "zero_price",
        "zero_volume",
        "time_monotonic_error",
        "duplicate_timestamp",
        "duplicate_tick",
        "bid_ask_cross",
    })

    def __init__(
        self,
        event_bus: Any,
        source: str = "live",
        severity_map: Optional[Dict[str, str]] = None,
        aggregate_window: int = 60,
        critical_after: int = 3,
        enabled: bool = True,
    ):
        """
        初始化联动桥

        Parameters
        ----------
        event_bus : EventBus
            事件总线实例（需有 publish_data_quality 方法）
        source : str
            事件来源标识（默认 "live"）
        severity_map : Dict[str, str] | None
            自定义 dirty_type → severity 覆盖
        aggregate_window : int
            聚合窗口秒数（≥1）
        critical_after : int
            严重类型连续次数阈值（≥1）
        enabled : bool
            是否启用发布
        """
        self._bus = event_bus
        self._source = str(source)
        self._severity_map: Dict[str, str] = dict(severity_map or {})
        self._window = max(1, int(aggregate_window))
        self._critical_after = max(1, int(critical_after))
        self._enabled = bool(enabled)
        self._lock = threading.Lock()

        # 聚合槽: (symbol, dirty_type) -> {window_start, bad_count, total_count, errors, record}
        self._buckets: Dict[tuple, Dict[str, Any]] = {}
        # 连续脏数据计数: (symbol, dirty_type) -> int（跨窗口累计，干净bar清零）
        self._consecutive: Dict[tuple, int] = {}
        # 连续干净bar计数: symbol -> int（供观测）
        self._clean_streak: Dict[str, int] = {}
        # 已发布事件数
        self._published_count = 0

    # ═══════════════════════════════════════════
    #  入口
    # ═══════════════════════════════════════════

    def on_dirty(self, symbol: str, bar: Dict[str, Any], errors: List[str]) -> None:
        """
        脏数据入口（被 register_dirty_callback 调用）

        推断 dirty_type → 窗口聚合 → 窗口到期则 flush 发布。
        发布在锁外执行，避免持锁调用外部总线。
        """
        if not self._enabled:
            return
        dirty_type = self._infer_dirty_type(errors, bar)
        now = time.time()
        key = (symbol, dirty_type)
        to_flush: Optional[tuple] = None

        with self._lock:
            # 干净 streak 清零
            self._clean_streak[symbol] = 0
            # 连续计数（跨窗口累计）
            self._consecutive[key] = self._consecutive.get(key, 0) + 1

            bucket = self._buckets.get(key)
            if bucket is None or (now - bucket["window_start"]) >= self._window:
                # 旧窗口到期 → 取出待发布（锁外发布）
                if bucket is not None and bucket.get("bad_count", 0) > 0:
                    to_flush = (key, bucket)
                # 开新窗口
                bucket = {
                    "window_start": now,
                    "bad_count": 0,
                    "total_count": 0,
                    "errors": [],
                    "record": None,
                }
                self._buckets[key] = bucket

            bucket["bad_count"] += 1
            bucket["total_count"] += 1
            bucket["errors"] = list(errors)
            bucket["record"] = dict(bar) if bar else None

        # 锁外发布旧窗口
        if to_flush is not None:
            self._flush_bucket(to_flush[0], to_flush[1])

    def on_clean(self, symbol: str) -> None:
        """干净 bar 入口（连续脏数据计数清零 + clean streak 累加）"""
        if not self._enabled:
            return
        with self._lock:
            self._clean_streak[symbol] = self._clean_streak.get(symbol, 0) + 1
            # 该标的所有 dirty_type 连续计数清零
            for key in list(self._consecutive.keys()):
                if key[0] == symbol:
                    self._consecutive[key] = 0

    def flush_all(self) -> None:
        """强制 flush 所有窗口（停止/断开时补发未发布事件）"""
        if not self._enabled:
            return
        pending: list = []
        with self._lock:
            for key, bucket in list(self._buckets.items()):
                if bucket.get("bad_count", 0) > 0:
                    pending.append((key, bucket))
            self._buckets.clear()
        for key, bucket in pending:
            self._flush_bucket(key, bucket)

    # ═══════════════════════════════════════════
    #  发布
    # ═══════════════════════════════════════════

    def _flush_bucket(self, key: tuple, bucket: Dict[str, Any]) -> None:
        """
        发布一个聚合窗口为 DataQualityEvent

        严重度判定：
          - severity_map 自定义覆盖优先
          - 严重类型且连续计数 ≥ critical_after → critical
          - 其余 → warning
        """
        symbol, dirty_type = key
        with self._lock:
            consecutive = self._consecutive.get(key, 0)

        severity = self._severity_map.get(dirty_type)
        if severity is None:
            if dirty_type in self._CRITICAL_TYPES and consecutive >= self._critical_after:
                severity = "critical"
            else:
                severity = "warning"

        try:
            self._bus.publish_data_quality(
                dirty_type=dirty_type,
                symbol=symbol,
                data_type="bar",
                severity=severity,
                errors=list(bucket.get("errors", [])),
                record=dict(bucket["record"]) if bucket.get("record") else None,
                bad_count=int(bucket.get("bad_count", 0)),
                total_count=int(bucket.get("total_count", 0)),
                source=self._source,
                source_step="realtime_feed",
            )
            self._published_count += 1
            logger.debug(
                f"[realtime_event] 已发布: {symbol} {dirty_type} "
                f"severity={severity} bad={bucket.get('bad_count')} "
                f"consecutive={consecutive}"
            )
        except Exception as e:
            logger.error(f"[realtime_event] 发布DataQualityEvent失败: {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════
    #  dirty_type 推断
    # ═══════════════════════════════════════════

    @staticmethod
    @staticmethod
    def _infer_dirty_type(errors: List[str], bar: Optional[Dict[str, Any]]) -> str:
        """
        从清洗错误推断 dirty_type（对齐任务6 DirtyDataType 枚举）

        #E3修复：优先解析结构化错误码前缀 "[ERROR_CODE] message" 精确匹配；
        若无错误码前缀（旧格式/跳变检测等动态生成的错误），回退到文本模糊匹配。

        匹配优先级：OHLC > 跳变 > 负价/负量 > 零价/零量 > 缺symbol >
                   时间戳 > 单调 > 重复 > 量异常 > unknown
        """
        # #E3修复：使用统一的错误码推断函数（结构化优先 + 模糊匹配回退）
        return infer_dirty_type_from_errors(errors)

    # ═══════════════════════════════════════════
    #  观测接口
    # ═══════════════════════════════════════════

    @property
    def published_count(self) -> int:
        """已发布事件数"""
        return self._published_count

    @property
    def clean_streak(self) -> Dict[str, int]:
        """各标的连续干净 bar 计数（快照）"""
        with self._lock:
            return dict(self._clean_streak)

    @property
    def consecutive_dirty(self) -> Dict[str, int]:
        """各 (symbol, dirty_type) 连续脏数据计数（快照）"""
        with self._lock:
            return {f"{k[0]}:{k[1]}": v for k, v in self._consecutive.items() if v > 0}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """运行时开关发布（灰度/调试）"""
        self._enabled = bool(enabled)

    def __repr__(self) -> str:
        return (
            f"_RealtimeEventBridge(source={self._source}, "
            f"window={self._window}s, critical_after={self._critical_after}, "
            f"published={self._published_count}, enabled={self._enabled})"
        )
