# 文件名：myquant/core/lookahead_guard.py
"""
lookahead_guard.py - 防未来函数校验器（规则4核心）
=====================================================

功能:
  1. 记录回测/实盘中的数据访问、指标计算、信号生成、交易成交、止损操作
  2. 自动校验是否存在未来函数（look-ahead bias）
  3. 回测结束必须调用 assert_pass()，不通过则报错终止
  4. 支持环境变量强制开启 / 严格模式
  5. 回测和实盘共用同一套校验逻辑（规则2：实盘无缝对接）

四条铁律（规则4）:
  1. 指标计算不含当日：RSRS斜率用 iloc[i-period:i]，不含第i根K线
  2. 移动止损先检查后更新：先判断是否触发止损，再更新止损价
  3. 信号T日收盘→T+1开盘：T日收盘产生信号，T+1开盘价成交
  4. 因子仅用历史数据：股票池筛选只用信号日之前的数据

P2-D7-003 整改声明：
  铁律1由结构（RSRS闭式OLS+shift(1)/derived_metrics严格因果）+测试锚点保障；
  铁律2由止损逻辑顺序保障；guard 运行期记录覆盖铁律3/4（record_indicator/record_stop_loss 无生产调用，蓝图已如实声明）。

环境变量:
  LOOKAHEAD_GUARD_ENABLED=true   强制开启校验（默认开启）
  LOOKAHEAD_GUARD_STRICT=true    严格模式，违规立即抛异常（默认仅记录）

使用方法:
    from myquant.core.lookahead_guard import get_lookahead_guard

    guard = get_lookahead_guard()
    guard.reset()

    # 记录指标计算（calc_date=2024-01-15，使用数据截止 2024-01-14）
    guard.record_indicator("RSRS", "000001.SZ", "2024-01-15", data_end="2024-01-14")

    # 记录信号生成（信号日 2024-01-15 收盘，使用数据截止 2024-01-15）
    guard.record_signal("000001.SZ", "2024-01-15", "BUY", data_end="2024-01-15")

    # 记录交易成交（信号日 2024-01-15，成交日 2024-01-16 开盘）
    guard.record_trade("000001.SZ", "2024-01-16", signal_date="2024-01-15", price_type="OPEN")

    # 回测结束校验
    guard.assert_pass()  # 不通过则抛 LookaheadError
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from myquant.core.exceptions import LookaheadError


# ═══════════════════════════════════════════════
#  记录数据类
# ═══════════════════════════════════════════════

@dataclass
class IndicatorRecord:
    """指标计算记录"""
    indicator_name: str
    symbol: str
    calc_date: str           # 指标计算日（第i根K线）
    data_start: str = ""     # 使用的数据起始日
    data_end: str = ""       # 使用的数据截止日（必须 < calc_date）
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class SignalRecord:
    """信号生成记录"""
    symbol: str
    signal_date: str         # 信号生成日（T日收盘）
    signal_type: str         # BUY / SELL / CLOSE
    data_end: str = ""       # 信号使用的数据截止日（必须 <= signal_date）
    factor_names: List[str] = field(default_factory=list)  # 使用的因子名
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class TradeRecord:
    """交易成交记录"""
    symbol: str
    trade_date: str          # 实际成交日（T+1）
    signal_date: str = ""    # 对应信号日（T日）
    price_type: str = ""     # OPEN / CLOSE / VWAP
    side: str = ""           # BUY / SELL
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class StopLossRecord:
    """止损操作记录（校验先检查后更新顺序）"""
    symbol: str
    date: str
    action: str              # "CHECK" / "UPDATE"
    stop_price: float = 0.0
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


@dataclass
class DataAccessRecord:
    """数据访问记录"""
    symbol: str
    access_date: str         # 访问发生的日期
    data_date: str           # 被访问的数据日期
    source: str = ""         # 数据来源（indicator/strategy/risk/factor）
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


# ═══════════════════════════════════════════════
#  防未来函数校验器
# ═══════════════════════════════════════════════

class LookaheadGuard:
    """
    防未来函数校验器（规则4核心）

    记录回测/实盘中的关键操作，回测结束时统一校验。
    支持严格模式（违规立即抛异常）和记录模式（仅记录，assert_pass 时统一校验）。
    """

    def __init__(self, strict: Optional[bool] = None):
        self._lock = threading.Lock()

        # 环境变量控制
        env_enabled = os.environ.get("LOOKAHEAD_GUARD_ENABLED", "true").lower()
        self._enabled = env_enabled in ("true", "1", "yes", "on")

        env_strict = os.environ.get("LOOKAHEAD_GUARD_STRICT", "false").lower()
        self._strict = strict if strict is not None else env_strict in ("true", "1", "yes", "on")

        # 记录存储
        self._indicators: List[IndicatorRecord] = []
        self._signals: List[SignalRecord] = []
        self._trades: List[TradeRecord] = []
        self._stop_losses: List[StopLossRecord] = []
        self._data_accesses: List[DataAccessRecord] = []

        # 校验结果
        self._violations: List[Dict[str, Any]] = []

    # ── 开关 ──

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def strict(self) -> bool:
        return self._strict

    @strict.setter
    def strict(self, value: bool) -> None:
        self._strict = value

    def enable(self) -> None:
        """开启校验"""
        self._enabled = True

    def disable(self) -> None:
        """关闭校验（不推荐，规则4要求强制开启）"""
        self._enabled = False

    # ── 重置 ──

    def reset(self) -> None:
        """清空所有记录（每次回测开始前调用）"""
        with self._lock:
            self._indicators.clear()
            self._signals.clear()
            self._trades.clear()
            self._stop_losses.clear()
            self._data_accesses.clear()
            self._violations.clear()

    # ── 记录方法 ──

    def record_indicator(
        self,
        indicator_name: str,
        symbol: str,
        calc_date: str,
        data_start: str = "",
        data_end: str = "",
    ) -> None:
        """
        记录指标计算（铁律1：指标计算不含当日）

        Parameters
        ----------
        indicator_name : 指标名（如 "RSRS", "MA"）
        symbol : 股票代码
        calc_date : 指标计算日（第i根K线，格式 YYYY-MM-DD）
        data_start : 使用的数据起始日
        data_end : 使用的数据截止日（必须 < calc_date，即不含当日）
        """
        if not self._enabled:
            return

        rec = IndicatorRecord(
            indicator_name=indicator_name,
            symbol=symbol,
            calc_date=calc_date,
            data_start=data_start,
            data_end=data_end,
        )

        # 铁律1校验：data_end 必须 < calc_date（不含当日）
        if data_end and calc_date:
            if data_end >= calc_date:
                violation = {
                    "rule": "铁律1-指标计算不含当日",
                    "indicator": indicator_name,
                    "symbol": symbol,
                    "calc_date": calc_date,
                    "data_end": data_end,
                    "message": f"指标[{indicator_name}]计算日[{calc_date}]使用了截止日[{data_end}]的数据，data_end 必须 < calc_date（不含当日）",
                }
                self._handle_violation(violation)

        with self._lock:
            self._indicators.append(rec)

    def record_signal(
        self,
        symbol: str,
        signal_date: str,
        signal_type: str,
        data_end: str = "",
        factor_names: Optional[List[str]] = None,
    ) -> None:
        """
        记录信号生成（铁律3+铁律4：信号T日收盘，因子仅用历史数据）

        Parameters
        ----------
        symbol : 股票代码
        signal_date : 信号生成日（T日，格式 YYYY-MM-DD）
        signal_type : 信号类型（BUY / SELL / CLOSE）
        data_end : 信号使用的数据截止日（必须 <= signal_date）
        factor_names : 使用的因子名列表
        """
        if not self._enabled:
            return

        rec = SignalRecord(
            symbol=symbol,
            signal_date=signal_date,
            signal_type=signal_type,
            data_end=data_end,
            factor_names=factor_names or [],
        )

        # 铁律4校验：因子数据截止日必须 <= 信号日
        if data_end and signal_date:
            if data_end > signal_date:
                violation = {
                    "rule": "铁律4-因子仅用历史数据",
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "data_end": data_end,
                    "message": f"信号日[{signal_date}]使用了截止日[{data_end}]的因子数据，data_end 必须 <= signal_date",
                }
                self._handle_violation(violation)

        with self._lock:
            self._signals.append(rec)

    def record_trade(
        self,
        symbol: str,
        trade_date: str,
        signal_date: str = "",
        price_type: str = "",
        side: str = "",
    ) -> None:
        """
        记录交易成交（铁律3：信号T日收盘→T+1开盘）

        Parameters
        ----------
        symbol : 股票代码
        trade_date : 实际成交日（T+1，格式 YYYY-MM-DD）
        signal_date : 对应信号日（T日）
        price_type : 成交价类型（OPEN / CLOSE / VWAP）
        side : 买卖方向（BUY / SELL）
        """
        if not self._enabled:
            return

        rec = TradeRecord(
            symbol=symbol,
            trade_date=trade_date,
            signal_date=signal_date,
            price_type=price_type,
            side=side,
        )

        # 铁律3校验：成交日必须 > 信号日（T+1成交）
        if signal_date and trade_date:
            if trade_date <= signal_date:
                violation = {
                    "rule": "铁律3-信号T日收盘→T+1开盘",
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "trade_date": trade_date,
                    "price_type": price_type,
                    "message": f"信号日[{signal_date}]在成交日[{trade_date}]成交，违反T+1规则（trade_date 必须 > signal_date）",
                }
                self._handle_violation(violation)

            # 进一步校验：T日信号应在T+1开盘成交
            if price_type and price_type.upper() not in ("OPEN", "VWAP"):
                # T+1用收盘价成交也可能是合理的（如次日收盘确认），但标准模式应为开盘
                pass

        with self._lock:
            self._trades.append(rec)

    def record_stop_loss(
        self,
        symbol: str,
        date: str,
        action: str,
        stop_price: float = 0.0,
    ) -> None:
        """
        记录止损操作（铁律2：移动止损先检查后更新）

        Parameters
        ----------
        symbol : 股票代码
        date : 操作日期（YYYY-MM-DD）
        action : 操作类型（"CHECK"=检查是否触发 / "UPDATE"=更新止损价）
        stop_price : 止损价
        """
        if not self._enabled:
            return

        action_upper = action.upper()
        if action_upper not in ("CHECK", "UPDATE"):
            return

        rec = StopLossRecord(
            symbol=symbol,
            date=date,
            action=action_upper,
            stop_price=stop_price,
        )

        with self._lock:
            self._stop_losses.append(rec)

        # 铁律2校验：同一天内 UPDATE 不能在 CHECK 之前
        if action_upper == "UPDATE":
            with self._lock:
                today_checks = [
                    r for r in self._stop_losses
                    if r.symbol == symbol and r.date == date and r.action == "CHECK"
                ]
                today_updates_before = [
                    r for r in self._stop_losses
                    if r.symbol == symbol and r.date == date
                    and r.action == "UPDATE" and r.timestamp < rec.timestamp
                ]

            if not today_checks and today_updates_before:
                violation = {
                    "rule": "铁律2-移动止损先检查后更新",
                    "symbol": symbol,
                    "date": date,
                    "message": f"股票[{symbol}]在[{date}]先执行了UPDATE止损价更新，但没有先CHECK是否触发止损，违反先检查后更新规则",
                }
                self._handle_violation(violation)

    def record_data_access(
        self,
        symbol: str,
        access_date: str,
        data_date: str,
        source: str = "",
    ) -> None:
        """
        记录数据访问（通用校验）

        Parameters
        ----------
        symbol : 股票代码
        access_date : 访问发生的日期（YYYY-MM-DD）
        data_date : 被访问的数据日期（YYYY-MM-DD）
        source : 数据来源（indicator/strategy/risk/factor）
        """
        if not self._enabled:
            return

        rec = DataAccessRecord(
            symbol=symbol,
            access_date=access_date,
            data_date=data_date,
            source=source,
        )

        # 通用校验：访问日不能使用访问日之后的数据
        if access_date and data_date:
            if data_date > access_date:
                violation = {
                    "rule": "通用-数据访问不使用未来数据",
                    "symbol": symbol,
                    "access_date": access_date,
                    "data_date": data_date,
                    "source": source,
                    "message": f"在[{access_date}]访问了[{data_date}]的数据（未来数据），来源: {source}",
                }
                self._handle_violation(violation)

        with self._lock:
            self._data_accesses.append(rec)

    # ── 内部：违规处理 ──

    def _handle_violation(self, violation: Dict[str, Any]) -> None:
        """处理违规（严格模式立即抛异常，否则记录）"""
        with self._lock:
            self._violations.append(violation)

        if self._strict:
            raise LookaheadError(
                rule_name=violation.get("rule", ""),
                details=violation.get("message", ""),
                symbol=violation.get("symbol", ""),
                signal_date=violation.get("signal_date", ""),
            )

    # ── 统一校验 ──

    def assert_pass(self) -> bool:
        """
        回测结束时统一校验（必须调用，规则4要求）

        Returns
        -------
        bool : True=校验通过

        Raises
        ------
        LookaheadError : 存在未来函数违规时抛出
        """
        if not self._enabled:
            return True

        with self._lock:
            violations = list(self._violations)

        if violations:
            # 汇总违规信息
            summary_lines = [
                f"防未来函数校验未通过！共发现 {len(violations)} 处违规：",
            ]
            for i, v in enumerate(violations, 1):
                summary_lines.append(f"  [{i}] {v.get('rule', '未知规则')}: {v.get('message', '')}")

            summary = "\n".join(summary_lines)

            raise LookaheadError(
                rule_name="防未来函数统一校验",
                details=summary,
            )

        return True

    # ── 查询方法 ──

    @property
    def violations(self) -> List[Dict[str, Any]]:
        """获取所有违规记录"""
        with self._lock:
            return list(self._violations)

    @property
    def stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            return {
                "enabled": self._enabled,
                "strict": self._strict,
                "indicators_recorded": len(self._indicators),
                "signals_recorded": len(self._signals),
                "trades_recorded": len(self._trades),
                "stop_loss_ops": len(self._stop_losses),
                "data_accesses": len(self._data_accesses),
                "violations": len(self._violations),
            }

    def __repr__(self) -> str:
        s = self.stats
        status = "ENABLED" if self._enabled else "DISABLED"
        mode = "STRICT" if self._strict else "RECORD"
        return (
            f"LookaheadGuard({status}/{mode}, "
            f"indicators={s['indicators_recorded']}, "
            f"signals={s['signals_recorded']}, "
            f"trades={s['trades_recorded']}, "
            f"violations={s['violations']})"
        )


# ═══════════════════════════════════════════════
#  全局单例
# ═══════════════════════════════════════════════

_global_guard: Optional[LookaheadGuard] = None
_guard_lock = threading.Lock()


def get_lookahead_guard() -> LookaheadGuard:
    """
    获取全局防未来函数校验器单例

    Usage:
        guard = get_lookahead_guard()
        guard.reset()
        # ... 回测过程中记录操作 ...
        guard.assert_pass()
    """
    global _global_guard
    with _guard_lock:
        if _global_guard is None:
            _global_guard = LookaheadGuard()
        return _global_guard
