# 文件名：myquant/core/light_lookahead_guard.py
"""
light_lookahead_guard.py - 轻量防未来函数校验器（回测专用）
=============================================================
定位:
  与 myquant/core/lookahead_guard.py（记录型完整校验器）互补的轻量运行时防护层。
  记录型版本需要调用方在每个环节手动 record_*，回测结束 assert_pass() 统一校验；
  本轻量版由回测引擎逐 bar 推进"当前时刻"，自动实时校验数据访问/指标窗口/信号时序，
  违规立即拦截（strict）或记录告警（warn），性能开销 O(1)，专为回测引擎自动接入设计。

检测的四类未来函数（对照规则4铁律）:
  1. 未来数据访问 : 在 t 时刻访问了 > t 时刻的数据
  2. 指标含当日   : 指标计算日使用了 >= 计算日 的数据（未排除当日，对应铁律1）
  3. 信号同日成交 : 信号 T 日收盘产生却在 T 日成交（对应铁律3：信号T收盘→T+1开盘）
  4. 时钟倒退     : 回测时间线非递增推进（数据乱序）

环境变量:
  LIGHT_LOOKAHEAD_GUARD_ENABLED=true   强制开启校验（默认开启）
  LIGHT_LOOKAHEAD_GUARD_STRICT=true    严格模式，违规立即抛异常（默认仅记录）

使用方法（回测引擎接入）:
    from myquant.core.light_lookahead_guard import get_light_lookahead_guard

    guard = get_light_lookahead_guard()
    guard.reset()

    # 每处理完一根 bar 推进当前时刻
    guard.on_bar_close("2024-01-15")

    # 回测引擎内部自动校验（返回 False 表示违规）
    ok = guard.check_data_access(data_ts="2024-01-16", source="strategy")   # 未来数据 → False
    ok = guard.check_indicator_window(calc_ts="2024-01-15", data_end_ts="2024-01-14")  # 合法 → True
    ok = guard.check_signal_execution(signal_ts="2024-01-15", exec_ts="2024-01-16")    # T+1 合法 → True

    # 回测结束统一校验
    guard.assert_pass()  # 存在违规则抛 LookaheadError

依赖: os, threading, logging, dataclasses, datetime, typing
本地: myquant.core.exceptions.LookaheadError
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Union

from myquant.core.exceptions import LookaheadError

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 轻量校验器环境变量开关
ENV_ENABLED = "LIGHT_LOOKAHEAD_GUARD_ENABLED"
ENV_STRICT = "LIGHT_LOOKAHEAD_GUARD_STRICT"

# 违规规则标识（对照规则4铁律）
RULE_FUTURE_DATA = "轻量1-未来数据访问"
RULE_INDICATOR_WINDOW = "轻量2-指标计算含当日"
RULE_SIGNAL_EXECUTION = "轻量3-信号T日收盘后成交"
RULE_CLOCK_REGRESS = "轻量4-时间线时钟倒退"

# 时间戳类型：int/float（unix秒）、date/datetime、ISO 日期字符串 "YYYY-MM-DD"
TimeLike = Union[int, float, date, datetime, str]


# ─────────────────────────────────────────────
# 违规记录
# ─────────────────────────────────────────────

@dataclass
class LightLookaheadViolation:
    """轻量防未来函数校验器违规记录。"""

    rule: str                       # 违反的规则标识
    message: str                    # 人类可读违规说明
    symbol: str = ""                # 关联标的（可选）
    ts: Optional[float] = None      # 违规发生时的当前时刻（秒）
    source: str = ""                # 违规来源（如 strategy/factor）
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "rule": self.rule,
            "message": self.message,
            "symbol": self.symbol,
            "ts": self.ts,
            "source": self.source,
        }

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


# ─────────────────────────────────────────────
# 时间归一化辅助
# ─────────────────────────────────────────────

def _to_seconds(ts: Optional[TimeLike]) -> Optional[float]:
    """将时间戳归一化为秒（unix 秒）。

    支持 int/float（原样）、date/datetime（转 timestamp）、
    ISO 日期字符串 "YYYY-MM-DD"（转当天 0 点）。

    Args:
        ts: 任意受支持的时间表示。

    Returns:
        归一化后的秒数；None 输入返回 None。

    Raises:
        ValueError: 字符串无法解析为日期时抛出。
    """
    if ts is None:
        return None
    if isinstance(ts, bool):
        raise ValueError(f"不支持布尔值作为时间戳: {ts!r}")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return ts.timestamp()
    if isinstance(ts, date):
        return datetime(ts.year, ts.month, ts.day).timestamp()
    if isinstance(ts, str):
        text = ts.strip()
        if not text:
            raise ValueError("时间字符串为空")
        # 先尝试完整 datetime 格式（支持 "YYYY-MM-DD HH:MM:SS"），再退化为日期格式
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            try:
                d = date.fromisoformat(text)
                return datetime(d.year, d.month, d.day).timestamp()
            except ValueError:
                raise ValueError(
                    f"无法解析时间字符串: {ts!r}（仅支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）"
                )
    raise TypeError(f"不支持的时间类型: {type(ts).__name__}")


def is_future_data(data_ts: Optional[TimeLike], current_ts: Optional[TimeLike]) -> bool:
    """判断 data_ts 是否属于 current_ts 之后的未来数据。

    Args:
        data_ts: 被访问数据的时刻。
        current_ts: 当前允许访问的最大时刻（当前 bar 收盘）。

    Returns:
        True=未来数据；任一时刻未知时返回 False（无法判定）。
    """
    d = _to_seconds(data_ts)
    c = _to_seconds(current_ts)
    if d is None or c is None:
        return False
    return d > c


def is_lookahead_execution(
    signal_ts: Optional[TimeLike],
    exec_ts: Optional[TimeLike],
    allow_same_day: bool = False,
) -> bool:
    """判断成交时序是否构成未来函数（信号尚未产生即成交）。

    规则: 成交时刻必须晚于信号产生时刻（T日收盘信号→T+1开盘成交）。
    - exec < signal                → 成交早于信号，未来函数
    - exec == signal 且 !allow_same_day → 同日成交（默认视为未来函数）
    - exec > signal                → 合法（T+1 及以后）

    Args:
        signal_ts: 信号产生时刻（T日收盘）。
        exec_ts: 实际成交时刻。
        allow_same_day: 是否允许同日成交（部分实盘模型当日成交时置 True）。

    Returns:
        True=存在未来函数；任一时刻未知时返回 False。
    """
    s = _to_seconds(signal_ts)
    e = _to_seconds(exec_ts)
    if s is None or e is None:
        return False
    if e < s:
        return True
    if e == s and not allow_same_day:
        return True
    return False


# ─────────────────────────────────────────────
# 轻量防未来函数校验器
# ─────────────────────────────────────────────

class LightLookaheadGuard:
    """轻量防未来函数校验器（回测专用，自动时钟驱动）。

    由回测引擎逐 bar 推进当前时刻（on_bar_close），
    在数据访问/指标计算/信号成交等关键点调用 O(1) 校验方法，
    实时检测未来函数。strict 模式违规立即抛 LookaheadError，
    否则仅记录违规并告警，回测结束 assert_pass() 统一拦截。

    Attributes:
        current_ts: 当前已推进到的时刻（秒）；None 表示尚未推进。
        violations: 已记录的违规列表。
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        strict: Optional[bool] = None,
        allow_same_day: bool = False,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._lock = threading.RLock()

        env_enabled = os.environ.get(ENV_ENABLED, "true").lower()
        self._enabled = enabled if enabled is not None else env_enabled in ("true", "1", "yes", "on")

        env_strict = os.environ.get(ENV_STRICT, "false").lower()
        self._strict = strict if strict is not None else env_strict in ("true", "1", "yes", "on")

        self._allow_same_day: bool = bool(allow_same_day)
        self._logger: logging.Logger = logger or logging.getLogger(__name__)

        self._current_ts: Optional[float] = None
        self._violations: List[LightLookaheadViolation] = []

    # ── 开关/属性 ──

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    @property
    def strict(self) -> bool:
        return self._strict

    @strict.setter
    def strict(self, value: bool) -> None:
        self._strict = bool(value)

    @property
    def current_ts(self) -> Optional[float]:
        """当前已推进到的时刻（秒）。"""
        with self._lock:
            return self._current_ts

    @property
    def violations(self) -> List[LightLookaheadViolation]:
        """全部违规记录（副本）。"""
        with self._lock:
            return list(self._violations)

    @property
    def violation_count(self) -> int:
        """违规总数。"""
        with self._lock:
            return len(self._violations)

    @property
    def stats(self) -> Dict[str, Any]:
        """状态统计。"""
        with self._lock:
            return {
                "enabled": self._enabled,
                "strict": self._strict,
                "allow_same_day": self._allow_same_day,
                "current_ts": self._current_ts,
                "violations": len(self._violations),
                "rules": [v.rule for v in self._violations],
            }

    # ── 时间线推进 ──

    def on_bar_close(self, ts: TimeLike) -> None:
        """推进回测时间线：当前 bar 收盘，更新当前允许访问的最大时刻。

        Args:
            ts: 当前 bar 收盘时刻。

        Raises:
            ValueError: ts 无法解析。
        """
        if not self._enabled:
            return
        sec = _to_seconds(ts)
        if sec is None:
            return
        with self._lock:
            if self._current_ts is not None and sec < self._current_ts:
                self._report(
                    rule=RULE_CLOCK_REGRESS,
                    message=(
                        f"回测时间线倒退: 当前已推进到 "
                        f"{_fmt_ts(self._current_ts)}，又收到更早的 bar 收盘 {_fmt_ts(sec)}"
                    ),
                    ts=self._current_ts,
                )
            self._current_ts = sec

    # ── 校验方法 ──

    def check_data_access(self, data_ts: TimeLike, source: str = "", symbol: str = "") -> bool:
        """校验数据访问是否使用了未来数据。

        Args:
            data_ts: 被访问数据的时刻。
            source: 数据来源（strategy/factor/risk 等，用于定位）。
            symbol: 关联标的（可选）。

        Returns:
            True=合法；False=访问了未来数据（已记录违规）。
        """
        if not self._enabled:
            return True
        d = _to_seconds(data_ts)
        with self._lock:
            c = self._current_ts
        if d is None or c is None:
            return True  # 未知时刻或时间线未推进，无法判定
        if d > c:
            self._report(
                rule=RULE_FUTURE_DATA,
                message=(
                    f"在 {_fmt_ts(c)} 访问了 {_fmt_ts(d)} 的数据（未来数据）"
                    f"{f'，来源: {source}' if source else ''}"
                ),
                symbol=symbol,
                ts=c,
                source=source,
            )
            return False
        return True

    def check_indicator_window(
        self,
        calc_ts: TimeLike,
        data_end_ts: TimeLike,
        indicator: str = "",
        symbol: str = "",
    ) -> bool:
        """校验指标计算窗口是否含当日数据（铁律1：指标计算不含当日）。

        Args:
            calc_ts: 指标计算日（第 i 根 K 线）。
            data_end_ts: 指标使用的数据截止日（必须 < calc_ts）。
            indicator: 指标名（可选）。
            symbol: 关联标的（可选）。

        Returns:
            True=合法；False=指标使用了含计算日当天/未来的数据。
        """
        if not self._enabled:
            return True
        c = _to_seconds(calc_ts)
        d = _to_seconds(data_end_ts)
        if c is None or d is None:
            return True
        with self._lock:
            cur = self._current_ts
        # 指标计算发生在未来时刻（当前时间线未到 calc_ts）
        if cur is not None and c > cur:
            self._report(
                rule=RULE_INDICATOR_WINDOW,
                message=(
                    f"指标{f'[{indicator}]' if indicator else ''}计算日 {_fmt_ts(c)} "
                    f"晚于当前时间线 {_fmt_ts(cur)}（未来时刻计算）"
                ),
                symbol=symbol,
                ts=cur,
            )
            return False
        # 指标数据截止日 >= 计算日：使用了含当天数据
        if d >= c:
            self._report(
                rule=RULE_INDICATOR_WINDOW,
                message=(
                    f"指标{f'[{indicator}]' if indicator else ''}计算日 {_fmt_ts(c)} "
                    f"使用了截止日 {_fmt_ts(d)} 的数据（data_end 必须 < calc_ts，不含当日）"
                ),
                symbol=symbol,
                ts=cur,
            )
            return False
        return True

    def check_signal_execution(
        self,
        signal_ts: TimeLike,
        exec_ts: TimeLike,
        price_type: str = "OPEN",
        symbol: str = "",
    ) -> bool:
        """校验信号→成交时序（铁律3：信号T日收盘→T+1开盘成交）。

        Args:
            signal_ts: 信号产生时刻（T日收盘）。
            exec_ts: 实际成交时刻。
            price_type: 成交价类型（OPEN/CLOSE/VWAP，用于说明）。
            symbol: 关联标的（可选）。

        Returns:
            True=时序合法；False=构成未来函数（成交早于/同于信号产生时刻）。
        """
        if not self._enabled:
            return True
        s = _to_seconds(signal_ts)
        e = _to_seconds(exec_ts)
        if s is None or e is None:
            return True
        if is_lookahead_execution(s, e, allow_same_day=self._allow_same_day):
            with self._lock:
                cur = self._current_ts
            if e < s:
                detail = f"成交时刻 {_fmt_ts(e)} 早于信号产生时刻 {_fmt_ts(s)}"
            else:
                detail = (
                    f"信号 {_fmt_ts(s)} 与成交 {_fmt_ts(e)} 同日"
                    f"（成交价类型 {price_type}，应 T+1 执行）"
                )
            self._report(
                rule=RULE_SIGNAL_EXECUTION,
                message=f"信号→成交时序违规: {detail}",
                symbol=symbol,
                ts=cur,
            )
            return False
        return True

    # ── 统一校验 ──

    def assert_pass(self) -> bool:
        """回测结束时统一校验：存在违规则抛 LookaheadError。

        Returns:
            True=校验通过（无违规）。

        Raises:
            LookaheadError: 存在未来函数违规时抛出，details 汇总全部违规。
        """
        if not self._enabled:
            return True
        with self._lock:
            violations = list(self._violations)
        if violations:
            lines = [f"轻量防未来函数校验未通过！共发现 {len(violations)} 处违规："]
            for i, v in enumerate(violations, 1):
                lines.append(f"  [{i}] {v.rule}: {v.message}")
            raise LookaheadError(
                rule_name="轻量防未来函数统一校验",
                details="\n".join(lines),
            )
        return True

    # ── 报告/重置 ──

    def report(self) -> str:
        """生成违规汇总报告文本并通过 logger 输出（warning 级）。"""
        with self._lock:
            violations = list(self._violations)
            current = self._current_ts
        lines = ["轻量防未来函数校验报告"]
        lines.append("=" * 72)
        lines.append(f"启用: {self._enabled} | 严格模式: {self._strict} | "
                     f"当前时刻: {_fmt_ts(current) if current is not None else '未推进'}")
        lines.append(f"违规总数: {len(violations)}")
        if violations:
            lines.append("-" * 72)
            for i, v in enumerate(violations, 1):
                lines.append(f"[{i}] {v.rule}")
                lines.append(f"    {v.message}")
        else:
            lines.append("（无违规，通过）")
        text = "\n".join(lines)
        self._logger.warning("%s", text)
        return text

    def reset(self) -> None:
        """清空时间线与违规记录（每次回测开始前调用）。"""
        with self._lock:
            self._current_ts = None
            self._violations.clear()

    def __repr__(self) -> str:
        s = self.stats
        return (
            f"LightLookaheadGuard(enabled={s['enabled']}, strict={s['strict']}, "
            f"current_ts={s['current_ts']}, violations={s['violations']})"
        )

    # ── 内部 ──

    def _report(
        self,
        rule: str,
        message: str,
        symbol: str = "",
        ts: Optional[float] = None,
        source: str = "",
    ) -> None:
        violation = LightLookaheadViolation(
            rule=rule, message=message, symbol=symbol, ts=ts, source=source,
        )
        with self._lock:
            self._violations.append(violation)
        if self._strict:
            raise LookaheadError(
                rule_name=rule,
                details=message,
                symbol=symbol,
            )
        self._logger.warning("轻量防未来函数违规: %s", violation)


def _fmt_ts(sec: float) -> str:
    """将秒级时间戳格式化为 'YYYY-MM-DD HH:MM:SS'。"""
    try:
        return datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return f"{sec:.3f}s"


# ─────────────────────────────────────────────
# 全局单例
# ─────────────────────────────────────────────

_global_light_guard: Optional[LightLookaheadGuard] = None
_light_guard_lock = threading.Lock()


def get_light_lookahead_guard() -> LightLookaheadGuard:
    """获取轻量防未来函数校验器全局单例。

    Usage:
        guard = get_light_lookahead_guard()
        guard.reset()
        # ... 回测引擎逐 bar: guard.on_bar_close(ts) ...
        guard.assert_pass()
    """
    global _global_light_guard
    with _light_guard_lock:
        if _global_light_guard is None:
            _global_light_guard = LightLookaheadGuard()
        return _global_light_guard


def reset_light_lookahead_guard() -> None:
    """重置轻量防未来函数校验器单例（测试/进程重建时使用）。"""
    global _global_light_guard
    with _light_guard_lock:
        _global_light_guard = None


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover — 仅 python -m 自测路径，不计入门禁覆盖
    from datetime import datetime as _dt

    print("=" * 60)
    print("  light_lookahead_guard.py — 轻量防未来函数校验器 自测 Demo")
    print("=" * 60)

    ok_count = [0]
    total_count = [0]

    def _ok(cond: bool, label: str) -> None:
        total_count[0] += 1
        mark = "✓" if cond else "✗"
        print(f"    {mark} {label}")
        if cond:
            ok_count[0] += 1

    # ── 1) 时间线推进 + 数据访问校验 ──
    print("\n[1] 时间线推进 + 数据访问校验")
    guard = LightLookaheadGuard(enabled=True, strict=False, logger=None)
    _ok(guard.current_ts is None, "初始未推进时间线")
    guard.on_bar_close("2024-01-15")
    _ok(guard.current_ts is not None, "on_bar_close 后已推进")
    _ok(guard.check_data_access("2024-01-14", source="strategy"), "访问历史数据合法")
    _ok(guard.check_data_access("2024-01-15", source="strategy"), "访问当日数据合法")
    _ok(guard.check_data_access("2024-01-16", source="strategy") is False, "访问未来数据被拦截")
    _ok(guard.violation_count == 1, "未来数据违规已记录 1 条")
    _ok(guard.violations[0].rule == RULE_FUTURE_DATA, "违规规则标识正确")

    # ── 2) 指标窗口校验 ──
    print("\n[2] 指标窗口校验")
    _ok(guard.check_indicator_window("2024-01-15", "2024-01-14", indicator="RSRS"), "指标不含当日合法")
    _ok(guard.check_indicator_window("2024-01-15", "2024-01-15", indicator="RSRS") is False,
        "指标含当日被拦截")
    _ok(guard.check_indicator_window("2024-01-15", "2024-01-16", indicator="RSRS") is False,
        "指标用未来数据被拦截")
    _ok(guard.check_indicator_window("2024-01-16", "2024-01-15", indicator="RSRS") is False,
        "指标在未来时刻计算被拦截")

    # ── 3) 信号→成交时序校验 ──
    print("\n[3] 信号→成交时序校验")
    _ok(guard.check_signal_execution("2024-01-15", "2024-01-16", price_type="OPEN"), "T+1 成交合法")
    _ok(guard.check_signal_execution("2024-01-15", "2024-01-15", price_type="CLOSE") is False,
        "同日收盘成交被拦截")
    _ok(guard.check_signal_execution("2024-01-16", "2024-01-15", price_type="OPEN") is False,
        "成交早于信号被拦截")

    # ── 4) 时钟倒退 ──
    print("\n[4] 时钟倒退")
    guard.reset()
    guard.on_bar_close("2024-01-15")
    guard.on_bar_close("2024-01-14")
    _ok(guard.violation_count == 1, "时间线倒退被记录违规")

    # ── 5) strict 模式立即抛异常 ──
    print("\n[5] strict 模式")
    strict_guard = LightLookaheadGuard(enabled=True, strict=True)
    strict_guard.on_bar_close("2024-01-15")
    try:
        strict_guard.check_data_access("2024-01-16")
        _ok(False, "strict 模式未来数据应抛 LookaheadError")
    except LookaheadError:
        _ok(True, "strict 模式未来数据立即抛 LookaheadError")

    # ── 6) assert_pass 统一校验 ──
    print("\n[6] assert_pass 统一校验")
    guard.reset()
    _ok(guard.assert_pass(), "无违规时 assert_pass 通过")
    guard.on_bar_close("2024-01-15")
    guard.check_signal_execution("2024-01-15", "2024-01-15")
    try:
        guard.assert_pass()
        _ok(False, "有违规时 assert_pass 应抛 LookaheadError")
    except LookaheadError:
        _ok(True, "有违规时 assert_pass 正确抛 LookaheadError")

    # ── 7) allow_same_day ──
    print("\n[7] allow_same_day 配置")
    lax_guard = LightLookaheadGuard(enabled=True, strict=False, allow_same_day=True)
    lax_guard.on_bar_close("2024-01-15")
    _ok(lax_guard.check_signal_execution("2024-01-15", "2024-01-15", price_type="CLOSE"),
        "allow_same_day=True 时同日成交放行")

    # ── 8) 报告 / 统计 ──
    print("\n[8] 报告与统计")
    guard.reset()
    guard.on_bar_close("2024-01-15")
    guard.check_data_access("2024-01-16", source="factor")
    report_text = guard.report()
    _ok("轻量防未来函数校验报告" in report_text, "报告包含标题")
    _ok(RULE_FUTURE_DATA in report_text, "报告包含违规规则")
    s = guard.stats
    _ok(s["violations"] == 1, "stats 违规数正确")
    _ok(guard.violation_count == 1, "violation_count 正确")

    # ── 9) 禁用模式 ──
    print("\n[9] 禁用模式")
    off_guard = LightLookaheadGuard(enabled=False)
    off_guard.on_bar_close("2024-01-15")
    _ok(off_guard.current_ts is None, "禁用时不推进时间线")
    _ok(off_guard.check_data_access("2099-01-01"), "禁用时未来数据放行")
    _ok(off_guard.assert_pass(), "禁用时 assert_pass 通过")

    # ── 10) 模块级工具函数 ──
    print("\n[10] 模块级工具函数")
    _ok(is_future_data("2024-01-16", "2024-01-15") is True, "is_future_data 判定未来")
    _ok(is_future_data("2024-01-15", "2024-01-15") is False, "is_future_data 当日非未来")
    _ok(is_lookahead_execution("2024-01-15", "2024-01-15") is True, "同日成交判定为前视")
    _ok(is_lookahead_execution("2024-01-15", "2024-01-15", allow_same_day=True) is False,
        "allow_same_day 后同日放行")
    _ok(is_lookahead_execution("2024-01-15", "2024-01-16") is False, "T+1 非前视")

    # ── 11) 时间归一化 ──
    print("\n[11] 时间归一化")
    _ok(is_future_data(_dt(2024, 1, 16, 10, 0), _dt(2024, 1, 15, 15, 0)) is True,
        "datetime 类型判定未来")
    from datetime import date as _date
    _ok(is_future_data(_date(2024, 1, 16), _date(2024, 1, 15)) is True, "date 类型判定未来")
    _ok(is_future_data(1705305600, 1705219200) is True, "unix 秒判定未来")
    try:
        _to_seconds("not-a-date")
        _ok(False, "非法时间字符串应抛 ValueError")
    except ValueError:
        _ok(True, "非法时间字符串正确抛 ValueError")

    # ── 12) 单例与重置 ──
    print("\n[12] 单例与重置")
    g1 = get_light_lookahead_guard()
    g2 = get_light_lookahead_guard()
    _ok(g1 is g2, "同名单例返回同一实例")
    reset_light_lookahead_guard()
    g3 = get_light_lookahead_guard()
    _ok(g3 is not g1, "reset 后重建新实例")

    print("\n" + "=" * 60)
    print(f"  自测结果: {ok_count[0]}/{total_count[0]} 项通过")
    if ok_count[0] == total_count[0]:
        print("  ✓ light_lookahead_guard.py 轻量防未来函数校验器 全部自测通过")
    else:
        print(f"  ✗ 有 {total_count[0] - ok_count[0]} 项未通过，请检查！")
    print("=" * 60)
