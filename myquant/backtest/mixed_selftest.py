# 文件名：myquant/backtest/mixed_selftest.py
"""
backtest/mixed_selftest.py - mixed_engine 模块自测（P0-00 子步骤 A 第二刀）
================================================================

自 mixed_engine.py:1137-1495 整体迁入（v2.2 §5.0 子步骤 A 提取清单 (a)4：
_run_self_test 里非 __main__ 的辅助类 + 自测函数）。

承载：
  - ``_MixedTargetStrategy``：固定目标持仓策略（target 模式，验证 T+1 时序）
  - ``_MixedSignalStrategy``：on_bar 信号模式策略（buy_at/sell_at 触发）
  - ``_make_ohlcv``：合成行情 DataFrame 构造工具
  - ``run_mixed_selftest``：原 ``_run_self_test``，跑全部 7 组校验项

分层：myquant.backtest（L3）；仅依赖 models(L2) + strategies(L2) + 本包引擎。
纯自测脚本，业务运行时不 import；mixed_engine.py 仅在 ``__main__`` 薄入口调用。
"""

from __future__ import annotations

from typing import Any, List, Optional

import numpy as np
import pandas as pd

from myquant.backtest.mixed_engine import MixedBacktestEngine
from myquant.models.backtest import BacktestConfig
from myquant.models.market_data import Bar
from myquant.models.trade import OrderSide, OrderType, Position
from myquant.strategies.base import (
    Signal,
    SignalType,
    StrategyBase,
    StrategyContext,
)


class _MixedTargetStrategy(StrategyBase):
    """自测用固定目标持仓策略（target 模式）。

    generate_mixed_loop 按 bar 序号返回预设目标股数，用于精确验证 T+1 时序。
    """

    name = "mixed_target"

    def __init__(self, targets: Optional[List[float]] = None, **params: Any):
        super().__init__(**params)
        self.targets: List[float] = list(targets or [])
        self._bar_idx: int = 0

    def reset(self) -> None:
        """重置 bar 计数（供多次运行使用）。"""
        self._bar_idx = 0

    def generate_mixed_loop(self, bar: Bar, context: StrategyContext, position: Optional[Position]) -> float:
        """返回目标持仓股数（T 日收盘决定）。"""
        i = self._bar_idx
        self._bar_idx += 1
        if i < len(self.targets):
            return self.targets[i]
        return 0.0

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        # 兼容抽象基类：target 模式不使用
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)


class _MixedSignalStrategy(StrategyBase):
    """自测用 on_bar 信号策略（兼容 event 引擎接口）。"""

    name = "mixed_signal"

    def __init__(self, buy_at: Optional[List[int]] = None, sell_at: Optional[List[int]] = None, **params: Any):
        super().__init__(**params)
        self.buy_at: List[int] = list(buy_at or [])
        self.sell_at: List[int] = list(sell_at or [])
        self._bar_idx: int = 0

    def reset(self) -> None:
        """重置 bar 计数。"""
        self._bar_idx = 0

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """按 bar 序号生成买卖信号。"""
        i = self._bar_idx
        self._bar_idx += 1
        signals: List[Signal] = []
        if i in self.buy_at:
            signals.append(
                Signal(
                    symbol=bar.symbol,
                    signal_type=SignalType.BUY,
                    strategy_id=self.name,
                    reason="buy_signal",
                )
            )
        if i in self.sell_at:
            signals.append(
                Signal(
                    symbol=bar.symbol,
                    signal_type=SignalType.SELL,
                    strategy_id=self.name,
                    reason="sell_signal",
                )
            )
        return signals

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)


def _make_ohlcv(n: int, base: float = 10.0, drift: float = 0.02) -> pd.DataFrame:
    """构造合成行情 DataFrame（n 根日线，价格缓慢上涨）。"""
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = base * (1.0 + drift) ** np.arange(n)
    open_px = close * 0.995
    high = close * 1.02
    low = close * 0.98
    volume = np.full(n, 1_000_000.0)
    return pd.DataFrame(
        {"开盘价": open_px, "最高价": high, "最低价": low, "收盘价": close, "成交量": volume},
        index=idx,
    )


def run_mixed_selftest() -> int:
    """运行 mixed_engine 模块自测（原 _run_self_test，自 mixed_engine.py 迁入）。

    7 组校验：目标模式 T+1 / mixed-vs-vectorized 逐点一致 / 止损触发 /
    STOP_LIMIT 条件单 / on_bar 信号模式 / 多标的 / 防御式非法参数。

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
    print("mixed_engine 自测开始")
    print("=" * 60)

    # 构造合成行情（10 根）
    df = _make_ohlcv(10)
    data = {"600519.SH": df}
    config = BacktestConfig(
        symbols=["600519.SH"],
        start_date="2024-01-01",
        end_date="2024-01-12",
        initial_capital=1_000_000.0,
    )
    open_px = df["开盘价"].to_numpy()
    df["收盘价"].to_numpy()

    # 1. 目标持仓模式基本运行（t>=3 目标 1000 股）
    targets = [0.0, 0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0]
    strategy = _MixedTargetStrategy(targets=targets)
    engine = MixedBacktestEngine(config=config, strategy=strategy, data=data)
    result = engine.run()
    check(
        "目标模式回测成功",
        result is not None and result.is_success,
        f"status={result.status}",
    )
    check(
        "净值曲线点数 = 10",
        len(result.equity_curve) == 10,
        f"点数={len(result.equity_curve)}",
    )
    # T+1：target[3]=1000（第3根收盘信号）→ 第4根开盘成交 open[4]
    buy_trades = [t for t in result.trades if t.side == OrderSide.BUY.value]
    check(
        "买入 1 笔（T+1 开盘成交）",
        len(buy_trades) == 1,
        f"买入笔数={len(buy_trades)}",
    )
    if buy_trades:
        check(
            "买入成交价 = open[4]（T+1 次日开盘）",
            abs(buy_trades[0].price - open_px[4]) < 1e-6,
            f"成交价={buy_trades[0].price}, open[4]={open_px[4]:.4f}",
        )
    # 持仓路径：第3根成交后持仓 1000
    check(
        "成交后持仓 = 1000",
        engine.get_position("600519.SH") is not None
        and abs(engine.get_position("600519.SH").quantity - 1000.0) < 1e-9,
        f"持仓={engine.get_position('600519.SH')}",
    )
    # 现金 + 市值 = 总资产守恒（费用扣减后）
    acc = engine.get_account()
    check(
        "账户总资产 = 现金 + 市值",
        abs(acc["total_assets"] - (acc["cash"] + acc["market_value"])) < 1e-9,
        f"total={acc['total_assets']:.2f}, cash+mv={acc['cash'] + acc['market_value']:.2f}",
    )

    # 2. 与 vectorized 引擎单标的结果逐点一致（同一目标序列）
    from myquant.backtest.vectorized_engine import (
        VectorizedBacktestEngine,
    )
    from myquant.backtest.vectorized_engine import (
        _FixedTargetStrategy as _VecFixedTarget,
    )
    vec_targets = [0.0, 0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0]
    vec_strategy = _VecFixedTarget(targets=vec_targets)
    vec_engine = VectorizedBacktestEngine(config=config, strategy=vec_strategy, data=data)
    vec_result = vec_engine.run()
    mixed_eq = [p.equity for p in result.equity_curve]
    vec_eq = [p.equity for p in vec_result.equity_curve]
    max_eq_diff = max(abs(a - b) for a, b in zip(mixed_eq, vec_eq))
    check(
        "mixed 与 vectorized 净值逐点一致",
        len(mixed_eq) == len(vec_eq) and max_eq_diff < 1e-9,
        f"点数 {len(mixed_eq)} vs {len(vec_eq)}, 最大差异={max_eq_diff:.2e}",
    )

    # 3. 止损触发（路径依赖）：价格回落触发止损
    #    构造下跌行情，止损 10%，验证止损卖出即时成交
    n2 = 10
    idx2 = pd.date_range("2024-02-01", periods=n2, freq="B")
    close2 = np.array([10.0, 10.2, 10.4, 10.0, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5])
    open2 = close2 * 0.995
    high2 = np.maximum(open2, close2) * 1.02
    low2 = np.minimum(open2, close2) * 0.98
    vol2 = np.full(n2, 1_000_000.0)
    df2 = pd.DataFrame(
        {"open": open2, "high": high2, "low": low2, "close": close2, "volume": vol2},
        index=idx2,
    )
    data2 = {"000001.SZ": df2}
    config2 = BacktestConfig(
        symbols=["000001.SZ"],
        start_date="2024-02-01",
        end_date="2024-02-14",
        initial_capital=1_000_000.0,
    )
    sl_targets = [1000.0] * n2
    sl_strategy = _MixedTargetStrategy(targets=sl_targets)
    sl_engine = MixedBacktestEngine(
        config=config2, strategy=sl_strategy, data=data2, stop_loss_pct=0.10
    )
    sl_result = sl_engine.run()
    sl_sells = [t for t in sl_result.trades if t.side == OrderSide.SELL.value]
    check(
        "止损触发产生卖出",
        len(sl_sells) >= 1,
        f"卖出笔数={len(sl_sells)}",
    )
    if sl_sells:
        first_buy = [t for t in sl_result.trades if t.side == OrderSide.BUY.value]
        if first_buy:
            cost = first_buy[0].price
            stop_px = cost * 0.90
            expected_fill = min(stop_px, open2[4])
            check(
                "止损卖出成交价 = min(止损价, 触发日开盘)",
                abs(sl_sells[0].price - expected_fill) < 1e-6,
                f"成交价={sl_sells[0].price}, 期望={expected_fill:.4f} (成本={cost:.4f}, 止损={stop_px:.4f})",
            )

    # 4. 条件单（STOP_LIMIT）：突破买入，验证 T+1 撮合触发
    class _ConditionalOrderStrategy(StrategyBase):
        name = "conditional_order"

        def __init__(self, **params: Any):
            super().__init__(**params)
            self._bar_idx = 0
            self.submitted = False

        def generate_mixed_loop(self, bar: Bar, context: StrategyContext, position: Optional[Position]) -> float:
            i = self._bar_idx
            self._bar_idx += 1
            if i == 0 and not self.submitted:
                self.submitted = True
                order = self.create_order(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    quantity=1000.0,
                    order_type=OrderType.STOP_LIMIT,
                    price=10.3,
                    reason="conditional",
                )
                order.stop_price = 10.2
                context.submit_order(order)
            return 0.0

        def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

    cond_strategy = _ConditionalOrderStrategy()
    cond_engine = MixedBacktestEngine(config=config, strategy=cond_strategy, data=data)
    cond_result = cond_engine.run()
    cond_buys = [t for t in cond_result.trades if t.side == OrderSide.BUY.value]
    check(
        "条件单（STOP_LIMIT）触发买入",
        len(cond_buys) == 1,
        f"条件单买入笔数={len(cond_buys)}",
    )
    if cond_buys:
        check(
            "STOP_LIMIT 成交价 = min(limit, max(open, stop))",
            abs(cond_buys[0].price - 10.2) < 1e-6,
            f"成交价={cond_buys[0].price}",
        )

    # 5. on_bar 信号模式（兼容 event 引擎接口）
    sig_strategy = _MixedSignalStrategy(buy_at=[3], sell_at=[7])
    sig_engine = MixedBacktestEngine(config=config, strategy=sig_strategy, data=data)
    sig_result = sig_engine.run()
    check(
        "on_bar 信号模式回测成功",
        sig_result is not None and sig_result.is_success,
        f"status={sig_result.status}",
    )
    sig_buys = [t for t in sig_result.trades if t.side == OrderSide.BUY.value]
    sig_sells = [t for t in sig_result.trades if t.side == OrderSide.SELL.value]
    check(
        "信号模式买入/卖出各 1 笔",
        len(sig_buys) == 1 and len(sig_sells) == 1,
        f"买={len(sig_buys)}, 卖={len(sig_sells)}",
    )

    # 6. 多标的运行
    data_multi = {"600519.SH": df, "000001.SZ": _make_ohlcv(10, base=8.0)}
    multi_strategy = _MixedTargetStrategy(targets=[0.0, 0.0, 0.0, 500.0, 500.0, 500.0, 500.0, 500.0, 500.0, 500.0])
    multi_engine = MixedBacktestEngine(config=config, strategy=multi_strategy, data=data_multi)
    multi_result = multi_engine.run()
    check(
        "多标的同时回测成功",
        multi_result is not None and multi_result.is_success,
        f"status={multi_result.status}",
    )

    # 7. 防御：非法参数 / 无策略接口 / 空数据
    try:
        MixedBacktestEngine(config=config, strategy=strategy, data=data, stop_loss_pct=1.5)
        check("非法 stop_loss_pct 抛 ValueError", False, "未抛出")
    except ValueError:
        check("非法 stop_loss_pct 抛 ValueError", True)

    class _NoInterfaceStrategy(StrategyBase):
        name = "no_interface"

        def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

    noif_strategy = _NoInterfaceStrategy()
    noif_engine = MixedBacktestEngine(config=config, strategy=noif_strategy, data=data)
    noif_result = noif_engine.run()
    check(
        "无 generate_mixed_loop 时回退 on_bar 模式",
        noif_engine._signal_mode == "event" and noif_result.is_success,
        f"signal_mode={noif_engine._signal_mode}, status={noif_result.status}",
    )
    check(
        "有 generate_mixed_loop 时用 target 模式",
        engine._signal_mode == "target",
        f"signal_mode={engine._signal_mode}",
    )

    empty_engine = MixedBacktestEngine(config=config, strategy=strategy, data={})
    empty_result = empty_engine.run()
    check(
        "空数据返回 failed 状态",
        empty_result.status == "failed",
        f"status={empty_result.status}",
    )

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
    raise SystemExit(run_mixed_selftest())
