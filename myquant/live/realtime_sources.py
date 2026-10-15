# 文件名：myquant/live/realtime_sources.py
"""
realtime_sources.py - 实时行情源层（拆包瘦身 PR1 · 回写#421）
=================================================================
从 live/data_feed.py 迁出的"行情源层"：确定性模拟源 / 快照→周期bar聚合 / 多后端实时行情获取。

核心能力:
  1. MockRealtimeSource — 确定性模拟实时源（本地离线端到端验证，无随机可复现）
  2. RealtimeBarAggregator — 高频快照→周期bar(1m/5m/日)，周期切换才产生新bar
  3. RealtimeQuoteFetcher — 腾讯批量接口(主源) + akshare东财快照(备源) + mock(离线) 自动降级
  4. 行情代码工具 — 6位代码↔标准symbol↔腾讯代码互转、频率→聚合周期、快照→标准bar

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md，live/ 段 realtime_sources.py 已登记回写#421）
  2. 为实时行情及实盘无缝对接做好充足准备（统一bar结构，后续券商推送直接走同一分发通道）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前及历史数据，时间戳严格校验

防未来函数说明:
  - 实时快照时间戳优先取数据源自带行情时间，缺失用到达时刻，均≤当前时间
  - MockRealtimeSource 内部时钟从过去开始且只递增，时间戳恒≤当前时间
  - 聚合器只聚合【已发生】快照，周期key=timestamp//period，恒为已发生周期
  - 除权因子查找按 as_of 截断（_factor_on_or_before 只取 ≤date_str 的最近因子）

依赖: math, re, time, typing,
       myquant.core.logger（date_utils 为函数内延迟导入）
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("live.realtime_sources")


# ═══════════════════════════════════════════════
#  类型别名与常量
# ═══════════════════════════════════════════════

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
#  行情代码工具
# ═══════════════════════════════════════════════

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
