# 文件名：myquant/utils/time_utils.py
"""
time_utils.py - 日期时间处理、时区转换、交易日历
=================================================
功能清单:
  1. 时区感知时间获取 (Asia/Shanghai 为主)
  2. 时间戳 <-> datetime 互转
  3. 交易日历 (A股 SSE/SZSE 节假日管理)
  4. 交易时段判断 (集合竞价/连续竞价/午休/盘后)
  5. 交易日序列生成
  6. 日期偏移 (T+N 交易日)

依赖: datetime, zoneinfo (Py3.9+), functools
"""

from __future__ import annotations

import datetime as dt
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import List, Optional, Set, Union

from myquant.core.logger import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────
# 时区定义（优先 zoneinfo，降级为固定偏移）
# ─────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")
    TZ_UTC = ZoneInfo("UTC")
    TZ_HK = ZoneInfo("Asia/Hong_Kong")
    TZ_US_EAST = ZoneInfo("America/New_York")
except Exception:
    # 降级方案：Windows 未安装 tzdata 时使用固定偏移时区
    # 注意：固定偏移无夏令时，A股时区(UTC+8)全年不变，不影响交易时段判断
    TZ_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
    TZ_UTC = timezone.utc
    TZ_HK = timezone(timedelta(hours=8), name="Asia/Hong_Kong")
    TZ_US_EAST = timezone(timedelta(hours=-5), name="America/New_York")

# A股交易时段
class SessionType(Enum):
    PRE_MARKET = "pre_market"           # 09:15 - 09:25 集合竞价
    MORNING = "morning_session"         # 09:30 - 11:30 上午连续竞价
    LUNCH_BREAK = "lunch_break"         # 11:30 - 13:00 午休
    AFTERNOON = "afternoon_session"     # 13:00 - 15:00 下午连续竞价
    AFTER_HOURS = "after_hours"         # 15:00 之后
    CLOSED = "closed"                   # 非交易日


# ─────────────────────────────────────────────
# 基础时间函数
# ─────────────────────────────────────────────

def now(tz: ZoneInfo = TZ_SHANGHAI) -> datetime:
    """获取当前时区感知时间，默认上海时区"""
    return datetime.now(tz=tz)


def today(tz: ZoneInfo = TZ_SHANGHAI) -> date:
    """获取当前日期"""
    return datetime.now(tz=tz).date()


def to_timestamp(
    d: Union[datetime, date, str],
    tz: ZoneInfo = TZ_SHANGHAI,
    unit: str = "s",
) -> int:
    """
    datetime/date/str -> Unix时间戳

    Parameters
    ----------
    d : datetime | date | str
        输入时间，str格式支持: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS'
    tz : ZoneInfo
        时区（仅对 naive datetime / date 生效）
    unit : str
        's' 秒级 | 'ms' 毫秒级 | 'us' 微秒级

    Returns
    -------
    int : Unix timestamp
    """
    if isinstance(d, str):
        d = parse_datetime(d)
    if isinstance(d, date) and not isinstance(d, datetime):
        d = datetime(d.year, d.month, d.day, tzinfo=tz)
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz)

    ts = d.timestamp()
    if unit == "ms":
        return int(ts * 1000)
    elif unit == "us":
        return int(ts * 1_000_000)
    return int(ts)


def from_timestamp(
    ts: Union[int, float],
    tz: ZoneInfo = TZ_SHANGHAI,
    unit: str = "s",
) -> datetime:
    """Unix时间戳 -> 时区感知datetime"""
    if unit == "ms":
        ts = ts / 1000
    elif unit == "us":
        ts = ts / 1_000_000
    return datetime.fromtimestamp(ts, tz=tz)


def parse_datetime(s: str) -> datetime:
    """
    智能解析日期字符串
    支持: '2024-01-15', '2024-01-15 09:30:00', '20240115',
          '2024/01/15', '2024-01-15T09:30:00'
    """
    s = s.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y%m%d%H%M%S",
        "%Y%m%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析日期字符串: '{s}'")


def tz_convert(
    d: datetime,
    from_tz: ZoneInfo,
    to_tz: ZoneInfo,
) -> datetime:
    """时区转换"""
    if d.tzinfo is None:
        d = d.replace(tzinfo=from_tz)
    return d.astimezone(to_tz)


def fmt_datetime(d: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """datetime -> 格式化字符串"""
    return d.strftime(fmt)


# ─────────────────────────────────────────────
# 交易日历核心类
# ─────────────────────────────────────────────

class TradingCalendar:
    """
    A股交易日历管理器
    ==================
    功能:
      - 判断是否交易日 (排除周末 + 法定节假日)
      - 生成交易日序列
      - T+N 交易日偏移
      - 交易时段判断
      - 支持外部加载/注入节假日表

    默认内置 2024-2026 年A股休市日（可外部覆盖）。
    生产环境建议从交易所API或本地CSV加载完整日历。

    Usage:
        cal = TradingCalendar()
        cal.is_trading_day(date(2025, 1, 2))   # False (元旦)
        cal.next_trading_day(date(2025, 1, 1)) # 2025-01-02
        cal.get_trading_days('2025-01-01', '2025-01-31')
    """

    # 内置节假日 (周末自动排除，此处仅列法定假日中的工作日休市)
    # M6修复: 补齐 2023 年（此前仅 2024-2026，2023 年节假日被误判为交易日，
    # 而演示/回测行情默认从 2023-01 起）。2026 为预排，以国务院/交易所公告为准。
    _DEFAULT_HOLIDAYS: set = {
        # ── 2023 ──
        date(2023, 1, 2),   # 元旦（1/1 周日）
        date(2023, 1, 23), date(2023, 1, 24), date(2023, 1, 25),
        date(2023, 1, 26), date(2023, 1, 27),  # 春节
        date(2023, 4, 5),   # 清明
        date(2023, 5, 1), date(2023, 5, 2), date(2023, 5, 3),  # 劳动节
        date(2023, 6, 22), date(2023, 6, 23),  # 端午
        date(2023, 9, 29),  # 中秋
        date(2023, 10, 2), date(2023, 10, 3), date(2023, 10, 4),
        date(2023, 10, 5), date(2023, 10, 6),  # 国庆
        # ── 2024 ──
        date(2024, 1, 1),   # 元旦
        date(2024, 2, 9), date(2024, 2, 12), date(2024, 2, 13),
        date(2024, 2, 14), date(2024, 2, 15), date(2024, 2, 16),  # 春节
        date(2024, 4, 4), date(2024, 4, 5),  # 清明
        date(2024, 5, 1), date(2024, 5, 2), date(2024, 5, 3),  # 劳动节
        date(2024, 6, 10),  # 端午
        date(2024, 9, 16), date(2024, 9, 17),  # 中秋
        date(2024, 10, 1), date(2024, 10, 2), date(2024, 10, 3),
        date(2024, 10, 4), date(2024, 10, 7),  # 国庆
        # ── 2025 ──
        date(2025, 1, 1),   # 元旦
        date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
        date(2025, 1, 31), date(2025, 2, 3), date(2025, 2, 4),  # 春节
        date(2025, 4, 4),   # 清明
        date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 5),  # 劳动节
        date(2025, 6, 2),   # 端午
        date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3),
        date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8),  # 国庆+中秋
        # ── 2026（预排，以国务院/交易所公告为准，可经 holidays 参数覆盖）──
        date(2026, 1, 1), date(2026, 1, 2),  # 元旦
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
        date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23),  # 春节
        date(2026, 4, 6),   # 清明
        date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),  # 劳动节
        date(2026, 6, 19),  # 端午
        date(2026, 9, 25),  # 中秋
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
        date(2026, 10, 6), date(2026, 10, 7),  # 国庆
    }

    def __init__(
        self,
        holidays: Optional[set] = None,
        trade_days: Optional[Set[date]] = None,
    ):
        """
        Parameters
        ----------
        holidays : set[date] | None
            自定义节假日集合。None 则使用内置默认值。
        trade_days : set[date] | None
            真实交易日集合（P2-① #83）。None 回退 工作日-节假日 近似；
            提供时 is_trading_day/偏移/序列全部按真实日历。
        """
        self._holidays: set = holidays if holidays is not None else set(self._DEFAULT_HOLIDAYS)
        self._trade_days: Optional[Set[date]] = set(trade_days) if trade_days else None

    # ── 属性 ──
    @property
    def holidays(self) -> set:
        return self._holidays

    # ── 核心判断 ──
    def is_weekend(self, d: date) -> bool:
        """是否周末 (周六=5, 周日=6)"""
        return d.weekday() >= 5

    def is_holiday(self, d: date) -> bool:
        """是否法定节假日休市"""
        return d in self._holidays

    def is_trading_day(self, d: Union[date, str]) -> bool:
        """是否交易日（P2-① #83：注入真实交易日时按真实日历，否则 非周末且非节假日）"""
        if isinstance(d, str):
            d = parse_datetime(d).date()
        if self._trade_days is not None:
            return d in self._trade_days
        return not self.is_weekend(d) and not self.is_holiday(d)

    def set_trade_days(self, trade_days: Optional[Set[date]]) -> None:
        """运行时注入/清除真实交易日集合（P2-① #83；None 回退近似）。"""
        self._trade_days = set(trade_days) if trade_days else None

    # ── 交易日偏移 ──
    def next_trading_day(self, d: date, n: int = 1) -> date:
        """T+N 交易日 (n>0 向后, n<0 向前)

        P2-①(#83)：真实交易日模式若越过已知日历边界（guard 3660 天内未找齐），
        按日偏移近似并 warning，避免死循环。
        """
        step = 1 if n > 0 else -1
        count = 0
        current = d
        guard = 0
        while count < abs(n) and guard < 3660:
            current += timedelta(days=step)
            guard += 1
            if self.is_trading_day(current):
                count += 1
        if count < abs(n):
            logger.warning(
                "交易日历越界: %s 在 %d 天内未找到 %d 个交易日，按日偏移近似返回",
                d, guard, abs(n),
            )
        return current

    def prev_trading_day(self, d: date, n: int = 1) -> date:
        """T-N 交易日"""
        return self.next_trading_day(d, -n)

    # ── 交易日序列 ──
    def get_trading_days(
        self,
        start: Union[date, str],
        end: Union[date, str],
    ) -> List[date]:
        """生成 [start, end] 闭区间内的所有交易日"""
        if isinstance(start, str):
            start = parse_datetime(start).date()
        if isinstance(end, str):
            end = parse_datetime(end).date()
        if start > end:
            start, end = end, start

        result = []
        current = start
        while current <= end:
            if self.is_trading_day(current):
                result.append(current)
            current += timedelta(days=1)
        return result

    def count_trading_days(
        self,
        start: Union[date, str],
        end: Union[date, str],
    ) -> int:
        """区间内交易日数量"""
        return len(self.get_trading_days(start, end))

    # ── 交易时段 ──
    @staticmethod
    def get_session(d: datetime) -> SessionType:
        """
        判断给定时刻处于哪个交易时段
        (仅判断时间，不判断是否交易日)
        """
        t = d.time()
        if dt.time(9, 15) <= t < dt.time(9, 25):
            return SessionType.PRE_MARKET
        elif dt.time(9, 30) <= t < dt.time(11, 30):
            return SessionType.MORNING
        elif dt.time(11, 30) <= t < dt.time(13, 0):
            return SessionType.LUNCH_BREAK
        elif dt.time(13, 0) <= t < dt.time(15, 0):
            return SessionType.AFTERNOON
        elif t >= dt.time(15, 0):
            return SessionType.AFTER_HOURS
        else:
            return SessionType.CLOSED

    def is_trading_hours(self, d: Optional[datetime] = None) -> bool:
        """当前是否处于交易时段（含集合竞价）"""
        if d is None:
            d = now()
        if not self.is_trading_day(d.date()):
            return False
        session = self.get_session(d)
        return session in (
            SessionType.PRE_MARKET,
            SessionType.MORNING,
            SessionType.AFTERNOON,
        )

    # ── 辅助 ──
    def get_month_trading_days(self, year: int, month: int) -> List[date]:
        """获取某月全部交易日"""
        first_day = date(year, month, 1)
        if month == 12:
            last_day = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(year, month + 1, 1) - timedelta(days=1)
        return self.get_trading_days(first_day, last_day)

    def load_holidays_from_file(self, filepath: str) -> None:
        """
        从CSV文件加载节假日表 (扩展接口)
        CSV格式: 每行一个日期 'YYYY-MM-DD'
        """
        import csv
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if row and row[0].strip():
                    self._holidays.add(parse_datetime(row[0].strip()).date())

    def __repr__(self) -> str:
        return f"TradingCalendar(holidays_count={len(self._holidays)})"


# ─────────────────────────────────────────────
# 模块级便捷实例与函数
# ─────────────────────────────────────────────

_default_calendar = TradingCalendar()


def is_trading_day(d: Union[date, str]) -> bool:
    """模块级快捷: 判断是否交易日"""
    return _default_calendar.is_trading_day(d)


def get_trading_days(
    start: Union[date, str],
    end: Union[date, str],
) -> List[date]:
    """模块级快捷: 获取交易日列表"""
    return _default_calendar.get_trading_days(start, end)


def next_trading_day(d: date, n: int = 1) -> date:
    """模块级快捷: T+N"""
    return _default_calendar.next_trading_day(d, n)


# ─────────────────────────────────────────────
# 自测
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  time_utils.py 自测")
    print("=" * 60)

    # 1) 当前时间
    print(f"\n[1] 当前上海时间: {now()}")
    print(f"    当前UTC时间:  {now(TZ_UTC)}")
    print(f"    今日日期:     {today()}")

    # 2) 时间戳互转
    ts = to_timestamp("2025-06-15 09:30:00")
    print(f"\n[2] '2025-06-15 09:30:00' -> ts={ts}")
    print(f"    ts -> datetime: {from_timestamp(ts)}")

    # 3) 交易日历
    cal = TradingCalendar()
    test_dates = [
        date(2025, 1, 1),   # 元旦 -> False
        date(2025, 1, 2),   # 周四 -> True
        date(2025, 1, 4),   # 周六 -> False
        date(2025, 1, 29),  # 春节 -> False
        date(2025, 6, 16),  # 周一 -> True
    ]
    print("\n[3] 交易日判断:")
    for d in test_dates:
        flag = cal.is_trading_day(d)
        print(f"    {d} ({d.strftime('%A'):>9s}) -> {'✓ 交易日' if flag else '✗ 休市'}")

    # 4) 交易日序列
    days = cal.get_trading_days("2025-01-01", "2025-01-15")
    print(f"\n[4] 2025-01-01 ~ 01-15 交易日 ({len(days)}天):")
    print(f"    {days}")

    # 5) T+N
    d0 = date(2025, 1, 24)
    print(f"\n[5] T+1: {d0} -> {cal.next_trading_day(d0, 1)}")
    print(f"    T+5: {d0} -> {cal.next_trading_day(d0, 5)}")

    # 6) 交易时段
    test_time = datetime(2025, 6, 16, 10, 30, 0, tzinfo=TZ_SHANGHAI)
    print(f"\n[6] {test_time.strftime('%H:%M')} -> {cal.get_session(test_time).value}")

    print("\n" + "=" * 60)
    print("  ✓ time_utils.py 自测完成")
    print("=" * 60)