# 文件名：myquant/core/date_utils.py
"""
date_utils.py - 标准化日期处理工具
====================================
#E1修复：统一 bar 数据的 date 字段格式，解决日线/分钟线/实时数据格式不一致问题。

设计原则:
  1. 按数据粒度统一格式：
     - 日线/周线/月线/季线/年线: "YYYY-MM-DD"
     - 分钟线/小时线/实时聚合: "YYYY-MM-DD HH:MM:SS"
  2. 向下兼容：normalize_to_datetime() 可解析两种格式及时间戳
  3. 为实盘准备：支持交易日判断、交易时段校验等扩展
  4. 严格禁用未来函数：所有时间处理基于输入参数，不隐式获取当前时间

核心函数:
  - normalize_to_datetime(date_str)  : 灵活解析日期字符串 → datetime
  - format_bar_date(dt, period)      : 按周期格式化 date 字段
  - get_date_part(date_str)          : 获取日期部分 "YYYY-MM-DD"（兼容两种格式）
  - is_daily_period(period)          : 判断是否为日线级别周期
  - get_period_seconds(period)       : 获取周期秒数（用于聚合判断）

依赖: datetime, re, typing
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Union

logger = logging.getLogger("myquant.date_utils")


# ═══════════════════════════════════════════
#  周期分类常量
# ═══════════════════════════════════════════

# 日线级别周期（date 格式: "YYYY-MM-DD"）
DAILY_PERIODS = frozenset({
    "daily", "day", "1d", "d",
    "weekly", "week", "1w", "w",
    "monthly", "month", "1M", "M",
    "quarterly", "quarter", "3M", "Q",
    "yearly", "year", "1y", "Y", "annual",
})

# 日内级别周期（date 格式: "YYYY-MM-DD HH:MM:SS"）
INTRADAY_PERIODS = frozenset({
    "1min", "1m", "minute",
    "5min", "5m",
    "15min", "15m",
    "30min", "30m",
    "60min", "60m", "1h", "hour",
    "tick", "realtime", "rt",
})

# 周期秒数映射（用于聚合判断）
_PERIOD_SECONDS = {
    "1min": 60, "1m": 60, "minute": 60,
    "5min": 300, "5m": 300,
    "15min": 900, "15m": 900,
    "30min": 1800, "30m": 1800,
    "60min": 3600, "60m": 3600, "1h": 3600, "hour": 3600,
    "daily": 86400, "day": 86400, "1d": 86400, "d": 86400,
    "weekly": 604800, "week": 604800, "1w": 604800, "w": 604800,
    "monthly": 2592000, "month": 2592000, "1M": 2592000, "M": 2592000,
}

# 日期格式正则
_RE_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_RE_DATE_TIME_MS = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\.\d+")
_RE_TENCENT_TS = re.compile(r"^\d{14}$")  # yyyyMMddHHmmss


# ═══════════════════════════════════════════
#  核心函数
# ═══════════════════════════════════════════

def is_daily_period(period: Optional[str]) -> bool:
    """
    判断是否为日线级别周期。

    Parameters
    ----------
    period : str | None
        周期标识，如 "daily"、"5min"、"1h" 等。

    Returns
    -------
    bool : True 表示日线级别（date 格式为 "YYYY-MM-DD"）
    """
    if not period:
        return True  # 默认按日线处理
    p = str(period).lower().strip()
    return p in DAILY_PERIODS


def get_period_seconds(period: Optional[str]) -> Optional[int]:
    """
    获取周期对应的秒数（用于聚合判断）。

    Parameters
    ----------
    period : str | None
        周期标识。

    Returns
    -------
    int | None : 周期秒数，未知周期返回 None
    """
    if not period:
        return None
    p = str(period).lower().strip()
    return _PERIOD_SECONDS.get(p)


def normalize_to_datetime(
    date_value: Union[str, float, int, datetime, None],
) -> Optional[datetime]:
    """
    灵活解析日期值为 datetime 对象。

    支持格式:
      - "YYYY-MM-DD"
      - "YYYY-MM-DD HH:MM:SS"
      - "YYYY-MM-DDTHH:MM:SS"（ISO 格式）
      - "YYYY-MM-DD HH:MM:SS.ffffff"（带毫秒）
      - "yyyyMMddHHmmss"（腾讯行情时间戳格式）
      - float/int 时间戳（秒级）
      - datetime 对象（直接返回）

    Parameters
    ----------
    date_value : str | float | int | datetime | None
        待解析的日期值。

    Returns
    -------
    datetime | None : 解析后的 datetime 对象，解析失败返回 None
    """
    if date_value is None:
        return None

    if isinstance(date_value, datetime):
        return date_value

    if isinstance(date_value, (int, float)):
        try:
            return datetime.fromtimestamp(float(date_value))
        except (OSError, ValueError, OverflowError):
            return None

    if not isinstance(date_value, str):
        return None

    s = date_value.strip()
    if not s:
        return None

    # 带毫秒的日期时间
    if _RE_DATE_TIME_MS.match(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
            except ValueError:
                pass

    # 标准日期时间
    if _RE_DATE_TIME.match(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass

    # 仅日期
    if _RE_DATE_ONLY.match(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            pass

    # 腾讯行情时间戳 yyyyMMddHHmmss
    if _RE_TENCENT_TS.match(s):
        try:
            return datetime.strptime(s, "%Y%m%d%H%M%S")
        except ValueError:
            pass

    # 尝试 pandas 风格解析（如果可用）
    try:
        import pandas as pd
        result = pd.to_datetime(s)
        if pd.notna(result):
            return result.to_pydatetime()
    except Exception as e:
        # #R1修复：pandas解析失败是正常降级（前面已尝试多种格式），记录debug日志便于排查
        logger.debug(f"parse_datetime pandas parse failed for '{s}': {e}")

    return None


def format_bar_date(
    date_value: Union[str, float, int, datetime, None],
    period: Optional[str] = None,
) -> str:
    """
    按数据周期格式化 bar 的 date 字段。

    格式规则:
      - 日线/周线/月线等: "YYYY-MM-DD"
      - 分钟线/小时线/实时: "YYYY-MM-DD HH:MM:SS"

    Parameters
    ----------
    date_value : str | float | int | datetime | None
        待格式化的日期值。
    period : str | None
        数据周期，决定输出格式。None 默认按日线处理。

    Returns
    -------
    str : 格式化后的日期字符串。解析失败时返回空字符串。
    """
    dt = normalize_to_datetime(date_value)
    if dt is None:
        return ""

    if is_daily_period(period):
        return dt.strftime("%Y-%m-%d")
    else:
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def bar_date_to_epoch(
    date_value: Union[str, float, int, datetime, None],
) -> float:
    """
    将 bar 的日期值转换为周期归属 epoch 秒（时间戳）。

    与 normalize_to_datetime 严格互逆：bar_date_to_epoch 的产出可被
    normalize_to_datetime 还原为同一日期（日线归属日 00:00:00 的本地时间戳）。
    用于 live/data_feed 生成 bar.timestamp——用 bar 周期归属时间而非墙钟
    time.time()，避免同一根 bar 因墙钟时刻不同被判为不同 bar（KP-P3-02）。

    Parameters
    ----------
    date_value : str | float | int | datetime | None
        bar 的日期值（"YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM:SS"）。

    Returns
    -------
    float : 周期归属 epoch 秒；解析失败返回 0.0 并告警（防御式，调用方据此丢弃/降级）。
    """
    dt = normalize_to_datetime(date_value)
    if dt is None:
        logger.warning("bar_date_to_epoch 解析失败，返回 0.0: %r", date_value)
        return 0.0
    return dt.timestamp()


def get_date_part(date_value: Union[str, float, int, datetime, None]) -> str:
    """
    获取日期部分 "YYYY-MM-DD"（兼容两种格式）。

    这是向下兼容的工具函数，用于下游需要纯日期的场景。

    Parameters
    ----------
    date_value : str | float | int | datetime | None
        待处理的日期值。

    Returns
    -------
    str : "YYYY-MM-DD" 格式的日期字符串。
    """
    dt = normalize_to_datetime(date_value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d")


def get_time_part(date_value: Union[str, float, int, datetime, None]) -> str:
    """
    获取时间部分 "HH:MM:SS"（仅日内级别数据有意义）。

    Parameters
    ----------
    date_value : str | float | int | datetime | None
        待处理的日期值。

    Returns
    -------
    str : "HH:MM:SS" 格式的时间字符串，日线数据返回 "00:00:00"。
    """
    dt = normalize_to_datetime(date_value)
    if dt is None:
        return ""
    return dt.strftime("%H:%M:%S")


def is_same_trading_day(
    date1: Union[str, float, int, datetime, None],
    date2: Union[str, float, int, datetime, None],
) -> bool:
    """
    判断两个日期是否属于同一个交易日。

    Parameters
    ----------
    date1, date2 : str | float | int | datetime | None
        待比较的日期值。

    Returns
    -------
    bool : True 表示同一个交易日。
    """
    d1 = get_date_part(date1)
    d2 = get_date_part(date2)
    if not d1 or not d2:
        return False
    return d1 == d2


def get_period_start(
    date_value: Union[str, float, int, datetime, None],
    period: Optional[str] = None,
) -> Optional[datetime]:
    """
    获取给定日期所在周期的起始时间（用于聚合判断）。

    Parameters
    ----------
    date_value : str | float | int | datetime | None
        待处理的日期值。
    period : str | None
        数据周期。

    Returns
    -------
    datetime | None : 周期起始时间。
    """
    dt = normalize_to_datetime(date_value)
    if dt is None:
        return None

    if is_daily_period(period):
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    seconds = get_period_seconds(period)
    if seconds is None or seconds <= 0:
        return dt

    # 按周期对齐到周期起始
    epoch = datetime(1970, 1, 1)
    delta = dt - epoch
    period_num = int(delta.total_seconds()) // seconds
    return epoch + timedelta(seconds=period_num * seconds)


# ═══════════════════════════════════════════
#  模块自检
# ═══════════════════════════════════════════

if __name__ == "__main__":
    # 格式化解锁
    print("=== date_utils 自检 ===")

    # 日线格式
    assert format_bar_date("2025-06-15", "daily") == "2025-06-15"
    assert format_bar_date("2025-06-15 10:30:00", "daily") == "2025-06-15"

    # 分钟线格式
    assert format_bar_date("2025-06-15 10:30:00", "5min") == "2025-06-15 10:30:00"
    assert format_bar_date("2025-06-15", "5min") == "2025-06-15 00:00:00"

    # 灵活解析
    assert normalize_to_datetime("2025-06-15") is not None
    assert normalize_to_datetime("2025-06-15 10:30:00") is not None
    assert normalize_to_datetime("20250615103000") is not None  # 腾讯格式
    assert normalize_to_datetime(1718409600) is not None  # 时间戳

    # 日期部分提取
    assert get_date_part("2025-06-15 10:30:00") == "2025-06-15"
    assert get_date_part("2025-06-15") == "2025-06-15"

    # 周期判断
    assert is_daily_period("daily") is True
    assert is_daily_period("5min") is False
    assert is_daily_period("1h") is False
    assert is_daily_period(None) is True  # 默认日线

    # 同一交易日判断
    assert is_same_trading_day("2025-06-15 10:30:00", "2025-06-15 14:00:00") is True
    assert is_same_trading_day("2025-06-15", "2025-06-16") is False

    print("所有自检通过 ✅")
