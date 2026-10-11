# 文件名：myquant/backtest/engine_selftest.py
"""
backtest/engine_selftest.py - engine.py 三模式调度器自测（P0-00 子步骤 A 第四刀）
================================================================

自 engine.py:1145-1285 整体迁入（v2.2 §5.0 子步骤 A 提取清单 (a)4：
_run_self_test 里非 __main__ 的辅助类 + 自测函数）。

承载：
  - ``run_engine_selftest``：原 ``_run_self_test``，验证 BacktestScheduler 三模式调度
    （event/mixed/vectorized 能力探测、auto 选择、全流程运行、显式模式、异常路径）
  - ``_selftest_broker_factory``：G1 EX-09A main 语境装配的 SimBroker 工厂

分层：myquant.backtest（L3）；仅依赖 models(L2) + strategies(L2) + 本包引擎 +
execution.sim_broker（仅 __main__ 装配语境，分层豁免边界内）。
纯自测脚本，业务运行时不 import；engine.py 仅在 ``__main__`` 薄入口调用。
"""

from __future__ import annotations

from typing import Any, List, Optional

import numpy as np
import pandas as pd

from myquant.backtest.assembly import register_broker_factory
from myquant.backtest.engine import BacktestConfig, BacktestScheduler, run_backtest
from myquant.models.market_data import Bar
from myquant.strategies.base import (
    Signal,
    SignalType,
    StrategyBase,
    StrategyContext,
)


def run_engine_selftest() -> int:
    """运行 engine.py 三模式调度器自测（原 _run_self_test，自 engine.py 迁入）。

    6 组校验：策略能力探测（event/mixed/vectorized modes 识别）/ auto 模式选择 /
    三模式全流程运行 / 显式模式 / 模式不匹配异常 / 非法 mode 防御。

    Returns:
        失败项数量（0 = 全部通过）。
    """
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 60)
    print("engine.py 三模式调度器自测开始")
    print("=" * 60)

    # 合成行情
    n = 40
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 10.0 * 1.02 ** np.arange(n)
    df = pd.DataFrame(
        {"开盘价": close * 0.995, "最高价": close * 1.02, "最低价": close * 0.98,
         "收盘价": close, "成交量": np.full(n, 1e6)},
        index=idx,
    )
    data = {"600519.SH": df}
    config = BacktestConfig(
        symbols=["600519.SH"], start_date="2024-01-01",
        end_date="2024-02-28", initial_capital=1_000_000.0,
    )

    # 测试策略：仅 event / event+mixed / event+mixed+vectorized
    class _EventOnlyStrategy(StrategyBase):
        name = "event_only"

        def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

    class _MixedCapableStrategy(StrategyBase):
        name = "mixed_capable"

        def generate_mixed_loop(self, bar: Bar, context: StrategyContext,
                                position: Optional[Any]) -> float:
            return 1000.0

        def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

    class _VectorCapableStrategy(StrategyBase):
        name = "vector_capable"

        def generate_mixed_loop(self, bar: Bar, context: StrategyContext,
                                position: Optional[Any]) -> float:
            return 1000.0

        def generate_signals_vectorized(self, data_df: pd.DataFrame) -> np.ndarray:
            return np.zeros(len(data_df)) + 1000.0

        def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

    # 1. 能力探测
    eo = _EventOnlyStrategy()
    mc = _MixedCapableStrategy()
    vc = _VectorCapableStrategy()
    check("event-only 策略 modes == [event]", BacktestScheduler.detect_modes(eo) == ["event"], f"{BacktestScheduler.detect_modes(eo)}")
    check("mixed 策略 modes 含 mixed", "mixed" in BacktestScheduler.detect_modes(mc), f"{BacktestScheduler.detect_modes(mc)}")
    check("vector 策略 modes 含 vectorized", "vectorized" in BacktestScheduler.detect_modes(vc), f"{BacktestScheduler.detect_modes(vc)}")

    # 2. auto 选择
    s1 = BacktestScheduler(mode="auto")
    check("auto 选 event", s1.resolve_mode(eo) == "event", f"{s1.resolve_mode(eo)}")
    s2 = BacktestScheduler(mode="auto")
    check("auto 选 mixed", s2.resolve_mode(mc) == "mixed", f"{s2.resolve_mode(mc)}")
    s3 = BacktestScheduler(mode="auto")
    check("auto 选 vectorized", s3.resolve_mode(vc) == "vectorized", f"{s3.resolve_mode(vc)}")

    # 3. 全流程运行（三种模式都跑通）
    r1 = run_backtest(config, eo, data=data, mode="auto")
    check("event 模式回测成功", r1 is not None and r1.is_success and r1.extra.get("engine_mode") == "event", f"status={r1.status}, mode={r1.extra.get('engine_mode')}")
    r2 = run_backtest(config, mc, data=data, mode="auto")
    check("mixed 模式回测成功", r2 is not None and r2.is_success and r2.extra.get("engine_mode") == "mixed", f"status={r2.status}, mode={r2.extra.get('engine_mode')}")
    r3 = run_backtest(config, vc, data=data, mode="auto")
    check("vectorized 模式回测成功", r3 is not None and r3.is_success and r3.extra.get("engine_mode") == "vectorized", f"status={r3.status}, mode={r3.extra.get('engine_mode')}")

    # 4. 显式模式
    r4 = run_backtest(config, vc, data=data, mode="vectorized")
    check("显式 vectorized 模式成功", r4 is not None and r4.is_success, f"status={r4.status}")

    # 5. 显式模式不匹配 → 明确异常
    try:
        run_backtest(config, eo, data=data, mode="vectorized")
        check("显式 vectorized + event-only 策略抛异常", False, "未抛出")
    except NotImplementedError:
        check("显式 vectorized + event-only 策略抛异常", True)

    # 6. 非法 mode
    try:
        BacktestScheduler(mode="bad")
        check("非法 mode 抛 ValueError", False, "未抛出")
    except ValueError:
        check("非法 mode 抛 ValueError", True)

    print("=" * 60)
    if failures:
        print(f"自测结果: {passed} 项通过, {len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return len(failures)
    print(f"自测结果: 全部 {passed} 项通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    # G1 EX-09A：main 语境装配撮合执行器工厂（分层违规豁免边界内）
    from myquant.execution.sim_broker import SimBroker

    def _selftest_broker_factory(config: BacktestConfig) -> Any:
        return SimBroker(
            initial_capital=config.initial_capital,
            commission_model=config.commission,
            slippage_model=config.slippage,
            price_limit_pct=float(config.price_limit_pct),
            enforce_limit=bool(config.enforce_limit),
            is_suspended=None,
        )

    register_broker_factory(_selftest_broker_factory)
    raise SystemExit(run_engine_selftest())
