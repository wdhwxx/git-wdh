# 文件名：myquant/backtest/vectorized_selftest.py
"""
backtest/vectorized_selftest.py - vectorized_engine 模块自测（P0-00 子步骤 A 第三刀）
================================================================

自 vectorized_engine.py:888-1202 整体迁入（v2.2 §5.0 子步骤 A 提取清单 (a)4：
_run_self_test 里非 __main__ 的辅助类 + 自测函数）。

承载：
  - ``_FixedTargetStrategy``：固定目标持仓序列策略（精确控制 T+1 成交时点）
  - ``_DummyVectorStrategy``：双均线向量化策略（满仓/空仓切换）
  - ``run_vectorized_selftest``：原 ``_run_self_test``，跑全部 11 组校验项

分层：myquant.backtest（L3）；仅依赖 models(L2) + strategies(L2) + 本包引擎。
纯自测脚本，业务运行时不 import；vectorized_engine.py 仅在 ``__main__`` 薄入口调用。
"""

from __future__ import annotations

from typing import Any, List, Optional

import numpy as np
import pandas as pd

from myquant.backtest.vectorized_engine import VectorizedBacktestEngine
from myquant.models.backtest import BacktestConfig
from myquant.strategies.base import StrategyBase, StrategyContext


class _FixedTargetStrategy(StrategyBase):
    """自测用固定目标持仓序列策略（精确控制 T+1 成交时点）。

    generate_signals_vectorized 直接返回预设的 target 股数序列。
    """

    name = "fixed_target_vector"

    def __init__(self, targets: Optional[List[float]] = None, **params: Any):
        super().__init__(**params)
        self.targets: List[float] = list(targets or [])

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        n = len(data)
        out = np.zeros(n, dtype=np.float64)
        t = np.asarray(self.targets, dtype=np.float64)
        m = min(n, t.size)
        out[:m] = t[:m]
        return out

    def generate_signal(self, bar: Any, context: StrategyContext):
        # 兼容抽象基类：向量化模式不使用
        return None


class _DummyVectorStrategy(StrategyBase):
    """自测用向量化策略：双均线，满仓/空仓（1000 股固定）切换。

    generate_signals_vectorized 输出目标持仓股数（T 日收盘决定）。
    """

    name = "dummy_vector"

    def __init__(self, fast: int = 3, slow: int = 5, target_shares: float = 1000.0, **params: Any):
        super().__init__(**params)
        self.fast = int(fast)
        self.slow = int(slow)
        self.target_shares = float(target_shares)

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        close = data["收盘价"].to_numpy(dtype=np.float64)
        n = len(close)
        target = np.zeros(n, dtype=np.float64)
        if n < self.slow + 1:
            return target
        # 本行使用的数据截至 t 时刻：仅用截至 t 的 close 计算均线
        fast_ma = pd.Series(close).rolling(self.fast).mean().to_numpy()
        slow_ma = pd.Series(close).rolling(self.slow).mean().to_numpy()
        # t 时刻信号：fast_ma[t] > slow_ma[t]（均线已用截至 t 收盘数据，无未来函数）
        cross_up = (fast_ma > slow_ma) & ~np.isnan(fast_ma) & ~np.isnan(slow_ma)
        target[cross_up] = self.target_shares
        return target

    def generate_signal(self, bar: Any, context: StrategyContext):
        # 兼容抽象基类：向量化模式不使用
        return None


def run_vectorized_selftest() -> int:
    """运行 vectorized_engine 模块自测（原 _run_self_test，自 vectorized_engine.py 迁入）。

    11 组校验：基本运行/T+1 持仓时序/成交价=开盘价/费用与滑点/期末净值/
    多标的聚合/无交易恒 1/非法 capital_mode 防御/未实现接口报错/空数据 failed/
    手算净值一致/买卖闭环 FIFO 盈亏。

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
    print("vectorized_engine 自测开始")
    print("=" * 60)

    # 构造合成行情（10 根日线，价格平稳上涨）
    dates = pd.date_range("2024-01-01", periods=10, freq="D")
    close = np.array([10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 11.6, 11.8])
    open_px = close * 0.995
    high = close * 1.02
    low = close * 0.98
    volume = np.full(10, 1_000_000.0)
    df = pd.DataFrame(
        {"open": open_px, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )
    data = {"600519.SH": df}
    config = BacktestConfig(
        symbols=["600519.SH"],
        start_date="2024-01-01",
        end_date="2024-01-10",
        initial_capital=1_000_000.0,
    )

    # 1. 基本运行：固定 target（t>=3 满仓 1000 股 → position 自 t=4 起 1000）
    targets = [0.0, 0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0]
    strategy = _FixedTargetStrategy(targets=targets)
    engine = VectorizedBacktestEngine(config=config, strategy=strategy, data=data)
    result = engine.run()
    check(
        "基本回测运行成功且 completed",
        result is not None and result.is_success,
        f"status={result.status}",
    )
    check(
        "净值曲线点数 = 10",
        len(result.equity_curve) == 10,
        f"点数={len(result.equity_curve)}",
    )

    # 2. T+1 成交验证：target[t] 在 t+1 日开盘建仓
    #    target[0..2]=0, target[3]=1000 → position[0..3]=0, position[4]=1000
    sr = engine._symbol_results[0]
    check(
        "持仓第 0-3 根为 0",
        all(sr.position[i] == 0 for i in range(4)),
        f"position[:4]={sr.position[:4]}",
    )
    check(
        "持仓第 4 根起为 1000（T+1 成交）",
        abs(sr.position[4] - 1000.0) < 1e-9,
        f"position[4]={sr.position[4]}",
    )

    # 3. 成交价 = T 日开盘价（t=3 信号 → t=4 开盘成交）
    buy_trades = [t for t in sr.trades if t.side == "BUY"]
    check(
        "买入成交价 = 对应日开盘价",
        len(buy_trades) == 1 and abs(buy_trades[0].price - open_px[4]) < 1e-6,
        f"成交价={buy_trades[0].price if buy_trades else None}, open[4]={open_px[4]:.4f}",
    )

    # 4. 费用正确性（买入总手续费=佣金+过户费+滑点，无印花税）
    if buy_trades:
        bt = buy_trades[0]
        notional = 1000.0 * bt.price
        expect_comm = max(notional * 0.0002, 5.0) + notional * 0.00001
        expect_slip = bt.price * 2.0 / 10000.0 * 1000.0
        check(
            "买入总手续费正确（佣金+过户费）",
            abs(bt.commission - expect_comm) < 1e-6,
            f"手续费={bt.commission}, 期望={expect_comm}",
        )
        check(
            "买入滑点正确",
            abs(bt.slippage - expect_slip) < 1e-6,
            f"滑点={bt.slippage}, 期望={expect_slip}",
        )

    # 5. 期末净值 > 1（持有上涨，资金含初始本金）
    check(
        "期末净值 > 1",
        result.final_equity > 1.0,
        f"期末净值={result.final_equity:.4f}",
    )

    # 6. 多标的聚合
    df2 = df.copy()
    data2 = {"600519.SH": df, "000001.SZ": df2}
    config2 = BacktestConfig(
        symbols=["600519.SH", "000001.SZ"],
        start_date="2024-01-01",
        end_date="2024-01-10",
        initial_capital=1_000_000.0,
    )
    engine2 = VectorizedBacktestEngine(config=config2, strategy=strategy, data=data2)
    result2 = engine2.run()
    check(
        "多标的回测成功",
        result2 is not None and result2.is_success,
        f"status={result2.status if result2 else None}",
    )
    check(
        "多标的组合净值点数 = 10",
        len(result2.equity_curve) == 10,
        f"点数={len(result2.equity_curve)}",
    )

    # 7. 无交易场景：target 全 0 → 净值恒为 1
    flat_strategy = _FixedTargetStrategy(targets=[0.0] * 10)
    engine_flat = VectorizedBacktestEngine(config=config, strategy=flat_strategy, data=data)
    result_flat = engine_flat.run()
    check(
        "无交易净值恒为 1",
        abs(result_flat.final_equity - 1.0) < 1e-9,
        f"期末净值={result_flat.final_equity:.6f}",
    )

    # 8. 非法参数防御
    try:
        VectorizedBacktestEngine(config=config, strategy=strategy, data=data, capital_mode="bad")
        check("非法 capital_mode 抛 ValueError", False, "未抛出")
    except ValueError:
        check("非法 capital_mode 抛 ValueError", True)

    class _NoVectorStrategy(_FixedTargetStrategy):
        # 删除向量化接口，模拟未实现
        def generate_signals_vectorized(self, data):  # type: ignore[override]
            raise NotImplementedError

    try:
        bad_engine = VectorizedBacktestEngine(config=config, strategy=_NoVectorStrategy(), data=data)
        bad_engine.run()
        check("策略未实现向量化接口抛异常", False, "未抛出")
    except NotImplementedError:
        check("策略未实现向量化接口抛异常", True)

    # 9. 空数据场景
    empty_engine = VectorizedBacktestEngine(config=config, strategy=strategy, data={})
    empty_result = empty_engine.run()
    check(
        "空数据返回 failed 状态",
        empty_result.status == "failed",
        f"status={empty_result.status}",
    )

    # 10. 手算净值验证（单标的，买入后持仓市值 + 现金）
    #    t=4 买入 1000 股 @ open[4]；现金 = 1e6 - (成交额 + 总手续费 + 滑点)
    #    期末净值 = (期末现金 + 1000×close[9]) / 1e6
    if buy_trades:
        bt = buy_trades[0]
        notional_buy = 1000.0 * bt.price
        buy_cost = notional_buy + bt.commission + bt.slippage
        final_cash = 1_000_000.0 - buy_cost
        final_asset = final_cash + 1000.0 * close[9]
        expect_equity = final_asset / 1_000_000.0
        check(
            "手算期末净值一致",
            abs(result.final_equity - expect_equity) < 1e-6,
            f"引擎={result.final_equity:.6f}, 手算={expect_equity:.6f}",
        )

    # 11. 买卖完整闭环：买入→卖出，验证印花税/FIFO盈亏/卖出现金
    #     target[3]=1000 → t=4 开盘买入；target[6]=0 → t=7 开盘卖出
    round_trip_targets = [0.0, 0.0, 0.0, 1000.0, 1000.0, 1000.0, 0.0, 0.0, 0.0, 0.0]
    rt_strategy = _FixedTargetStrategy(targets=round_trip_targets)
    rt_engine = VectorizedBacktestEngine(config=config, strategy=rt_strategy, data=data)
    rt_result = rt_engine.run()
    rt_sr = rt_engine._symbol_results[0]
    sell_trades = [t for t in rt_sr.trades if t.side == "SELL"]
    check(
        "卖出成交 1 笔",
        len(sell_trades) == 1,
        f"卖出笔数={len(sell_trades)}",
    )
    if sell_trades:
        st = sell_trades[0]
        sell_notional = 1000.0 * open_px[7]
        expect_sell_comm = (
            max(sell_notional * 0.0002, 5.0)
            + sell_notional * 0.001  # 印花税仅卖出
            + sell_notional * 0.00001  # 过户费
        )
        expect_sell_slip = open_px[7] * 2.0 / 10000.0 * 1000.0
        check(
            "卖出成交价 = open[7]（T+1 开盘）",
            abs(st.price - open_px[7]) < 1e-6,
            f"成交价={st.price}, open[7]={open_px[7]:.4f}",
        )
        check(
            "卖出总手续费含印花税",
            abs(st.commission - expect_sell_comm) < 1e-6,
            f"手续费={st.commission}, 期望={expect_sell_comm}",
        )
        check(
            "卖出滑点正确（卖减收）",
            abs(st.slippage - expect_sell_slip) < 1e-6,
            f"滑点={st.slippage}, 期望={expect_sell_slip}",
        )
        # FIFO 配对盈亏：(open[7]-open[4])*1000 - 总手续费
        expect_pnl = (open_px[7] - open_px[4]) * 1000.0 - expect_sell_comm
        check(
            "FIFO 平仓盈亏正确",
            abs(st.pnl - expect_pnl) < 1e-6,
            f"pnl={st.pnl:.4f}, 期望={expect_pnl:.4f}",
        )
        # 期末现金手算：1e6 - 买入总支出 + 卖出净收入（含滑点减收）
        expect_sell_proceeds = sell_notional - expect_sell_comm - expect_sell_slip
        expect_final_asset = 1_000_000.0 - (1000.0 * open_px[4] + bt.commission + bt.slippage) + expect_sell_proceeds
        expect_equity_rt = expect_final_asset / 1_000_000.0
        check(
            "买卖闭环期末净值手算一致",
            abs(rt_result.final_equity - expect_equity_rt) < 1e-6,
            f"引擎={rt_result.final_equity:.8f}, 手算={expect_equity_rt:.8f}",
        )
    # 卖出后持仓归零
    check(
        "卖出后持仓归零",
        abs(rt_sr.position[8]) < 1e-9,
        f"position[8]={rt_sr.position[8]}",
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
    raise SystemExit(run_vectorized_selftest())
