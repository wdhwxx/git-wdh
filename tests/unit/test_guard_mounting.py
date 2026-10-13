# -*- coding: utf-8 -*-
"""
test_guard_mounting.py - KP-P2-08 防未来铁律 1/2/4 有效挂载（台账 T-06）
======================================================================
覆盖 myquant/strategies/base.py（守卫钩子）+ backtest/engine.py（挂载）+ ma_strategy.py（指标/止损点）：
  1. 真实回测后 guard.stats["indicators_recorded"] > 0（record_indicator 不再零挂载）
  2. record_indicator_guard 默认 data_end 因果口径（< calc_date），assert_pass 不误伤
  3. 读取 T+1 数据的指标桩 → assert_pass 抛错（铁律1）
  4. 读取 T+1 数据的信号桩（引擎 record_signal 用策略真实上报 data_end）→ assert_pass 抛错（铁律4）
  5. record_stop_loss_guard 挂载（铁律2 先检查后更新：UPDATE 早于 CHECK 抛错）
  6. record_data_access_guard 挂载（通用校验：访问日后数据抛错）
  7. 无 guard 注入时钩子退化为无操作（实盘兼容，不抛）

回归依据: 审计 KP-P2-08 —— record_indicator/record_stop_loss/record_data_access 无生产调用，
         engine.py:450 data_end=signal_date 自证常量致铁律4 恒假。
运行方式: pytest tests/unit/test_guard_mounting.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from myquant.backtest.engine import BacktestEngine, run_backtest
from myquant.core.exceptions import LookaheadError
from myquant.core.lookahead_guard import LookaheadGuard
from myquant.models.backtest import BacktestConfig
from myquant.strategies.base import Signal, SignalType, StrategyBase, StrategyContext
from myquant.strategies.ma_strategy import MACrossStrategy, RSRSStrategy


def _make_df(n: int = 140, seed: int = 7) -> pd.DataFrame:
    """确定性中文列日线数据（与回测引擎路径同构）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = 10.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.008, n)))
    return pd.DataFrame(
        {
            "开盘价": close * 0.995,
            "最高价": close * 1.02,
            "最低价": close * 0.98,
            "收盘价": close,
            "成交量": np.full(n, 1e6),
            "成交额": close * 1e6,
        },
        index=dates,
    )


class _FutureSignalStrategy(StrategyBase):
    """桩策略：生成 BUY 信号，但谎报信号用了未来一天的数据（读取 T+1）。"""

    name = "future_reader"

    def generate_signal(self, bar, context) -> Signal:
        return Signal(
            symbol=bar.symbol, signal_type=SignalType.BUY,
            strength=0.5, price=bar.close, reason="future-stub",
        )

    def get_signal_data_end(self, symbol: str, signal_date: str, context) -> str:
        """谎称用了 signal_date 之后一天的数据（铁律4 应拦截）。"""
        try:
            dt = pd.Timestamp(signal_date) + pd.Timedelta(days=1)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return signal_date


@pytest.mark.lookahead
class TestIndicatorMounting:
    """record_indicator 真实挂载（铁律1）。"""

    def test_真实回测_indicators_recorded大于0(self) -> None:
        """DoD：回测后 indicators_recorded > 0 且无违规（因果口径不误伤）。"""
        df = _make_df()
        config = BacktestConfig(
            symbols=["000001.SZ"],
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=1_000_000.0,
        )
        guard = LookaheadGuard()
        result = run_backtest(
            config, MACrossStrategy(), data={"000001.SZ": df},
            mode="event", lookahead_guard=guard,
        )
        assert result.is_success
        assert guard.stats["indicators_recorded"] > 0
        guard.assert_pass()  # 因果上报，不抛

    def test_钩子默认因果口径(self) -> None:
        guard = LookaheadGuard()
        ctx = StrategyContext(data={"000001.SZ": _make_df()}, extra={"lookahead_guard": guard})
        st = MACrossStrategy()
        st.record_indicator_guard("000001.SZ", "2025-03-20", "MA", ctx)
        assert guard.stats["indicators_recorded"] == 1
        rec = guard._indicators[0]
        assert rec.data_end < rec.calc_date  # 铁律1：data_end < calc_date
        guard.assert_pass()

    def test_读取T1数据指标桩_assert_pass抛错(self) -> None:
        """DoD：写读取 T+1 数据的指标桩 → assert_pass 抛错。"""
        guard = LookaheadGuard()
        ctx = StrategyContext(extra={"lookahead_guard": guard})
        st = MACrossStrategy()
        # 桩如实上报使用了 T+1 数据（calc_date=2025-06-16，data_end=2025-06-17）
        st.record_indicator_guard(
            "000001.SZ", "2025-06-16", "MA", ctx, data_end="2025-06-17",
        )
        assert guard.stats["indicators_recorded"] == 1
        with pytest.raises(LookaheadError):
            guard.assert_pass()

    def test_无guard注入钩子无操作(self) -> None:
        """context 无 guard 时钩子退化为无操作（实盘/未接线引擎兼容）。"""
        ctx = StrategyContext(data={"000001.SZ": _make_df()})  # extra 无 guard
        st = MACrossStrategy()
        st.record_indicator_guard("000001.SZ", "2025-03-20", "MA", ctx)  # 不抛
        assert True


@pytest.mark.lookahead
class TestSignalDataEndNoSelfProving:
    """引擎 record_signal 用策略真实数据截止日（铁律4，去自证常量）。"""

    def test_读取T1数据信号桩_assert_pass抛错(self) -> None:
        """DoD：信号读取 T+1 数据 → 铁律4 拦截（data_end > signal_date）。"""
        df = _make_df()
        config = BacktestConfig(
            symbols=["000001.SZ"],
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=1_000_000.0,
        )
        guard = LookaheadGuard()
        result = run_backtest(
            config, _FutureSignalStrategy(), data={"000001.SZ": df},
            mode="event", lookahead_guard=guard,
        )
        # 引擎不再用自证常量：检测到策略上报未来数据截止日 → 回测标记失败
        assert result.is_success is False
        with pytest.raises(LookaheadError):
            guard.assert_pass()

    def test_正常策略data_end等于信号日(self) -> None:
        """正常策略默认上报 data_end=signal_date（<= 满足铁律4），不误伤。"""
        df = _make_df()
        config = BacktestConfig(
            symbols=["000001.SZ"],
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=1_000_000.0,
        )
        guard = LookaheadGuard()
        result = run_backtest(
            config, MACrossStrategy(), data={"000001.SZ": df},
            mode="event", lookahead_guard=guard,
        )
        assert result.is_success
        guard.assert_pass()  # 正常策略无违规


@pytest.mark.lookahead
class TestStopLossAndDataAccessMounting:
    """record_stop_loss_guard / record_data_access_guard 挂载（铁律2/通用）。"""

    def test_止损钩子记录(self) -> None:
        guard = LookaheadGuard()
        ctx = StrategyContext(extra={"lookahead_guard": guard})
        st = RSRSStrategy()
        st.record_stop_loss_guard("000001.SZ", "2025-06-16", "CHECK", context=ctx, stop_price=10.0)
        assert guard.stats["stop_loss_ops"] == 1
        guard.assert_pass()

    def test_止损UPDATE早于CHECK_抛错(self) -> None:
        """铁律2：同一天 UPDATE 在 CHECK 之前（连续两次 UPDATE 无 CHECK）→ assert_pass 抛错。"""
        guard = LookaheadGuard()
        guard.record_stop_loss("000001.SZ", "2025-06-16", "UPDATE", stop_price=10.0)
        guard._stop_losses[0].timestamp = 1000.0  # 强制首条 UPDATE 时间戳更早（确定性）
        guard.record_stop_loss("000001.SZ", "2025-06-16", "UPDATE", stop_price=10.2)
        with pytest.raises(LookaheadError):
            guard.assert_pass()

    def test_数据访问钩子记录(self) -> None:
        guard = LookaheadGuard()
        ctx = StrategyContext(extra={"lookahead_guard": guard})
        st = RSRSStrategy()
        st.record_data_access_guard(
            "000001.SZ", "2025-06-16", "2025-06-15", context=ctx, source="test",
        )
        assert guard.stats["data_accesses"] == 1
        guard.assert_pass()

    def test_数据访问未来数据_抛错(self) -> None:
        """通用校验：访问日后数据 → assert_pass 抛错。"""
        guard = LookaheadGuard()
        guard.record_data_access("000001.SZ", "2025-06-16", "2025-06-17", source="test")
        with pytest.raises(LookaheadError):
            guard.assert_pass()


@pytest.mark.lookahead
class TestFailClosedObservability:
    """P1-07③（G13）：空值 fail-closed 阻断档校正。

    - engine._bar_date_str 时间戳异常返回哨兵并计数（不再静默）；
    - guard 空日期（信号日/成交日/data_end 哨兵）记**阻断档** violation——
      assert_pass 抛错（fail-closed：宁可阻断，不可静默放行）；
    - 显式豁免（skip_t1_check=True）是唯一合法跳过通道（实盘执行纪律差异）；
    - 阻断档违规抛错并落 audit_log（审计落点）。
    """

    def test_bar_date_str_abnormal_counts(self) -> None:
        """时间戳异常：返回哨兵 "" 并计数 +1（可观测化）。"""
        BacktestEngine._bar_date_failures = 0

        class _FakeBar:
            timestamp = 10**300  # datetime.fromtimestamp 溢出

        out = BacktestEngine._bar_date_str(_FakeBar())
        assert out == ""
        assert BacktestEngine._bar_date_failures == 1

    def test_bar_date_str_normal_no_count(self) -> None:
        """正常时间戳不计数。"""
        BacktestEngine._bar_date_failures = 0

        class _FakeBar:
            timestamp = 1_752_537_600.0  # 2025-07-15

        out = BacktestEngine._bar_date_str(_FakeBar())
        assert out == "2025-07-15"
        assert BacktestEngine._bar_date_failures == 0

    def test_record_signal_empty_signal_date_blocks(self) -> None:
        """空信号日（哨兵）：记阻断档 violation，assert_pass 抛错。"""
        guard = LookaheadGuard()
        guard.record_signal("000001.SZ", "", "BUY", data_end="2025-07-10")
        assert len(guard.violations) == 1
        assert guard.violations[0].get("severity") != "警告"  # 阻断档
        with pytest.raises(LookaheadError):
            guard.assert_pass()

    def test_record_signal_empty_data_end_blocks(self) -> None:
        """G13：signal_date 非空但 data_end 为空 → 阻断档（无法证明因子仅用历史数据）。"""
        guard = LookaheadGuard()
        guard.record_signal("000001.SZ", "2025-07-15", "BUY", data_end="")
        assert len(guard.violations) == 1
        assert guard.violations[0].get("severity") != "警告"
        with pytest.raises(LookaheadError):
            guard.assert_pass()

    def test_record_trade_empty_trade_date_blocks(self) -> None:
        """空成交日（哨兵）：记阻断档 violation，assert_pass 抛错。"""
        guard = LookaheadGuard()
        guard.record_trade("000001.SZ", "", signal_date="2025-07-15")
        assert len(guard.violations) == 1
        assert guard.violations[0].get("severity") != "警告"
        with pytest.raises(LookaheadError):
            guard.assert_pass()

    def test_record_trade_empty_signal_date_blocks(self) -> None:
        """G13：signal_date 空、trade_date 非空（订单信号映射失配）→ 阻断档。"""
        guard = LookaheadGuard()
        guard.record_trade("000001.SZ", "2025-07-16", signal_date="")
        assert len(guard.violations) == 1
        assert guard.violations[0].get("severity") != "警告"
        with pytest.raises(LookaheadError):
            guard.assert_pass()

    def test_record_indicator_empty_data_end_blocks(self) -> None:
        """G13：record_indicator data_end 为空 → 阻断档（无法证明指标计算不含当日）。"""
        guard = LookaheadGuard()
        guard.record_indicator("MA", "000001.SZ", "2025-07-15", data_end="")
        assert len(guard.violations) == 1
        assert guard.violations[0].get("severity") != "警告"
        with pytest.raises(LookaheadError):
            guard.assert_pass()

    def test_strict_mode_empty_value_raises(self) -> None:
        """严格模式下空值违规立即抛错（阻断档，fail-closed）。"""
        guard = LookaheadGuard()
        guard.strict = True
        with pytest.raises(LookaheadError):
            guard.record_signal("000001.SZ", "", "BUY")
        assert len(guard.violations) == 1
        assert guard.violations[0].get("severity") != "警告"

    def test_skip_t1_check_explicit_exemption(self) -> None:
        """P1-07③：显式豁免（skip_t1_check=True）不记违规（实盘执行纪律差异）。"""
        guard = LookaheadGuard()
        guard.record_trade(
            "000001.SZ", "2025-07-16", signal_date="",
            price_type="OPEN", side="BUY", skip_t1_check=True,
        )
        assert guard.stats["trades_recorded"] == 1
        assert guard.stats["violations"] == 0
        assert guard.assert_pass() is True

    def test_blocking_violation_raises_with_audit(self, monkeypatch) -> None:
        """阻断档违规仍抛错，且审计落点被调用（M1A-04 审计可观测）。"""
        calls: list = []

        class _FakeAuditStore:
            def append(self, *args, **kwargs):
                calls.append((args, kwargs))

        monkeypatch.setattr(
            "myquant.core.audit_log.AuditLogStore", lambda: _FakeAuditStore()
        )
        guard = LookaheadGuard()
        guard.record_signal("000001.SZ", "2025-07-15", "BUY", data_end="2025-07-16")
        with pytest.raises(LookaheadError):
            guard.assert_pass()
        assert calls, "阻断档违规必须落 audit_log（审计落点）"
