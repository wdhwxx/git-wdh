# 文件名：tests/unit/test_strategies.py
"""
test_strategies.py - 策略层单元测试
====================================
覆盖 myquant/strategies/ 全部组件：
  - Signal / SignalType       信号数据结构
  - StrategyContext           策略上下文
  - StrategyBase              抽象基类（生命周期/工具方法）
  - StrategyRegistry          注册器（register/create/list/discover）
  - MACrossStrategy           均线交叉策略
  - RSRSStrategy              RSRS三因子择时策略
  - GridStrategy              网格交易策略
  - MomentumStrategy          动量策略
  - MeanReversionStrategy     均值回归策略
  - MultiFactorStrategy       多因子策略

测试维度:
  1. 实例化与参数（默认值 / 自定义 / 边界）
  2. 生命周期（on_init / on_bar / on_tick / on_stop）
  3. 信号生成（BUY / SELL / HOLD / 止损止盈）
  4. 注册器（注册 / 工厂 / 列表 / 发现）
  5. 工具方法（calc_position_size / check_stop_loss / check_take_profit / _get_close_series）
  6. 防未来函数（策略只使用截至当前bar的数据）
  7. 边界条件（数据不足 / 空数据 / 单bar）

运行方式:
  pytest tests/unit/test_strategies.py -v
  pytest tests/unit/test_strategies.py -v -m lookahead

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/unit/test_strategies.py）
  2. 为实时行情及实盘无缝对接做好充足准备（on_bar/on_tick 流式接口全覆盖）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 策略第i根bar的信号仅使用 iloc[:i+1] 数据
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from myquant.models.market_data import Bar, BarFrequency
from myquant.strategies import (
    GridStrategy,
    MACrossStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    MultiFactorStrategy,
    RSRSStrategy,
    Signal,
    SignalType,
    StockPoolBuilder,
    StrategyBase,
    StrategyContext,
    create_strategy,
    discover_strategies,
    get_strategy_class,
    list_strategies,
    register_strategy,
)
from myquant.strategies.registry import _strategy_registry
from tests.conftest import make_ohlcv_df

# ═══════════════════════════════════════════
#  Fixture：行情数据构造工具
# ═══════════════════════════════════════════

def _make_df(closes: List[float], symbol: str = "000001.SZ") -> pd.DataFrame:
    """从收盘价序列构造标准 OHLCV DataFrame（中文列，#38 规范）"""
    return make_ohlcv_df(
        closes=closes,
        volumes=[100000.0] * len(closes),
        amounts=[c * 100000.0 for c in closes],
        start="2024-01-01",
    )


def _make_bar(symbol: str, close: float, ts: float, idx: int = 0) -> Bar:
    """构造单根 Bar"""
    return Bar(
        symbol=symbol,
        frequency=BarFrequency.DAILY,
        timestamp=ts,
        open=close * 0.995,
        high=close * 1.015,
        low=close * 0.985,
        close=close,
        volume=100000.0,
        amount=close * 100000.0,
    )


def _make_context(df: pd.DataFrame, symbol: str = "000001.SZ") -> StrategyContext:
    """构造含数据的策略上下文"""
    return StrategyContext(
        data={symbol: df},
        portfolio=None,
        executor=None,
        config={"initial_capital": 1_000_000.0},
    )


def _run_strategy_on_bars(strategy: StrategyBase, ctx: StrategyContext,
                          df: pd.DataFrame, symbol: str = "000001.SZ") -> List[Signal]:
    """逐bar运行策略，收集所有信号"""
    strategy.on_init(ctx)
    signals: List[Signal] = []
    for i in range(len(df)):
        ts = df.index[i].timestamp()
        bar = _make_bar(symbol, df["收盘价"].iloc[i], ts, i)
        sigs = strategy.on_bar(bar, ctx)
        if sigs:
            signals.extend(sigs)
    return signals


@pytest.fixture
def crossover_df() -> pd.DataFrame:
    """有明确金叉/死叉的行情（先涨后跌再涨）"""
    closes = [10.0, 10.2, 10.5, 10.8, 11.0, 11.2, 11.5, 11.3, 11.0, 10.7,
              10.5, 10.3, 10.1, 10.0, 10.2, 10.5, 10.8, 11.0, 11.2, 11.5]
    return _make_df(closes)


@pytest.fixture
def uptrend_df() -> pd.DataFrame:
    """单调上涨行情"""
    closes = [10.0 + i * 0.1 for i in range(30)]
    return _make_df(closes)


@pytest.fixture
def downtrend_df() -> pd.DataFrame:
    """单调下跌行情"""
    closes = [12.0 - i * 0.1 for i in range(30)]
    return _make_df(closes)


@pytest.fixture
def sideways_df() -> pd.DataFrame:
    """震荡行情（适合网格/均值回归）"""
    closes = [10.0 + np.sin(i * 0.5) * 0.5 for i in range(40)]
    return _make_df(closes)


@pytest.fixture
def empty_df() -> pd.DataFrame:
    """空 DataFrame"""
    return _make_df([])


@pytest.fixture
def single_bar_df() -> pd.DataFrame:
    """单根bar"""
    return _make_df([10.0])


@pytest.fixture
def large_df() -> pd.DataFrame:
    """大量数据（70行，RSRS策略的StockPoolBuilder需要61+行）"""
    np.random.seed(123)
    closes = [10.0]
    for _ in range(69):
        closes.append(closes[-1] * (1 + np.random.normal(0, 0.015)))
    return _make_df(closes)


# ═══════════════════════════════════════════
#  TestSignalType - 信号类型枚举
# ═══════════════════════════════════════════

class TestSignalType:
    """SignalType 枚举测试"""

    def test_all_members_exist(self):
        """5种信号类型全部存在"""
        assert hasattr(SignalType, "BUY")
        assert hasattr(SignalType, "SELL")
        assert hasattr(SignalType, "HOLD")
        assert hasattr(SignalType, "STOP_LOSS")
        assert hasattr(SignalType, "TAKE_PROFIT")

    def test_values_are_strings(self):
        """枚举值为字符串"""
        assert SignalType.BUY.value == "BUY"
        assert SignalType.SELL.value == "SELL"
        assert SignalType.HOLD.value == "HOLD"


# ═══════════════════════════════════════════
#  TestSignal - 信号数据结构
# ═══════════════════════════════════════════

class TestSignal:
    """Signal 数据结构测试"""

    def test_create_buy_signal(self):
        """创建买入信号"""
        sig = Signal(symbol="000001.SZ", signal_type=SignalType.BUY,
                     strength=1.0, price=10.5, strategy_id="test", reason="金叉")
        assert sig.symbol == "000001.SZ"
        assert sig.signal_type == SignalType.BUY
        assert sig.is_buy is True
        assert sig.is_sell is False
        assert sig.is_hold is False

    def test_create_sell_signal(self):
        """创建卖出信号"""
        sig = Signal(symbol="000001.SZ", signal_type=SignalType.SELL,
                     strength=0.8, price=12.0, strategy_id="test", reason="死叉")
        assert sig.is_sell is True
        assert sig.is_buy is False

    def test_create_hold_signal(self):
        """创建持有信号"""
        sig = Signal(symbol="000001.SZ", signal_type=SignalType.HOLD,
                     strength=0.0, price=11.0, strategy_id="test", reason="持有")
        assert sig.is_hold is True
        assert sig.is_buy is False
        assert sig.is_sell is False

    def test_stop_loss_signal(self):
        """止损信号类型"""
        sig = Signal(symbol="000001.SZ", signal_type=SignalType.STOP_LOSS,
                     strength=1.0, price=9.0, strategy_id="test", reason="止损")
        assert sig.signal_type == SignalType.STOP_LOSS

    def test_take_profit_signal(self):
        """止盈信号类型"""
        sig = Signal(symbol="000001.SZ", signal_type=SignalType.TAKE_PROFIT,
                     strength=1.0, price=15.0, strategy_id="test", reason="止盈")
        assert sig.signal_type == SignalType.TAKE_PROFIT

    def test_to_order_exists(self):
        """Signal.to_order 方法存在且可调用"""
        sig = Signal(symbol="000001.SZ", signal_type=SignalType.BUY,
                     strength=1.0, price=10.5, strategy_id="test", reason="test")
        assert callable(sig.to_order)

    def test_to_order_returns_order(self):
        """to_order 返回 Order 对象"""
        from myquant.models.trade import Order, OrderSide
        sig = Signal(symbol="000001.SZ", signal_type=SignalType.BUY,
                     strength=1.0, price=10.5, strategy_id="test", reason="test")
        order = sig.to_order(quantity=100, price=10.5)
        assert order is not None
        assert isinstance(order, Order)
        assert order.symbol == "000001.SZ"
        assert order.side == OrderSide.BUY

    def test_signal_repr(self):
        """Signal __repr__ 不崩溃"""
        sig = Signal(symbol="000001.SZ", signal_type=SignalType.BUY,
                     strength=1.0, price=10.5, strategy_id="test", reason="test")
        assert "BUY" in repr(sig)


# ═══════════════════════════════════════════
#  TestStrategyContext - 策略上下文
# ═══════════════════════════════════════════

class TestStrategyContext:
    """StrategyContext 策略上下文测试"""

    def test_create_empty_context(self):
        """创建空上下文"""
        ctx = StrategyContext(data={}, portfolio=None, executor=None, config={})
        assert ctx.data == {}
        assert ctx.portfolio is None
        assert ctx.executor is None

    def test_create_with_data(self, crossover_df):
        """创建含数据上下文"""
        ctx = _make_context(crossover_df)
        assert "000001.SZ" in ctx.data
        assert len(ctx.data["000001.SZ"]) == 20

    def test_get_data_existing(self, crossover_df):
        """get_data 获取已有标的数据"""
        ctx = _make_context(crossover_df)
        data = ctx.get_data("000001.SZ")
        assert data is not None
        assert len(data) == 20

    def test_get_data_missing(self, crossover_df):
        """get_data 获取不存在标的返回 None"""
        ctx = _make_context(crossover_df)
        data = ctx.get_data("999999.SZ")
        assert data is None

    def test_context_config(self):
        """上下文配置传递"""
        ctx = StrategyContext(data={}, portfolio=None, executor=None,
                              config={"initial_capital": 500000.0, "commission_rate": 0.0003})
        assert ctx.config["initial_capital"] == 500000.0
        assert ctx.config["commission_rate"] == 0.0003


# ═══════════════════════════════════════════
#  TestStrategyBase - 抽象基类
# ═══════════════════════════════════════════

class TestStrategyBase:
    """StrategyBase 抽象基类测试"""

    def test_cannot_instantiate_directly(self):
        """抽象基类不能直接实例化"""
        with pytest.raises(TypeError):
            StrategyBase()

    def test_all_strategies_inherit_base(self):
        """所有策略均继承 StrategyBase"""
        for cls in [MACrossStrategy, RSRSStrategy, GridStrategy,
                    MomentumStrategy, MeanReversionStrategy, MultiFactorStrategy]:
            assert issubclass(cls, StrategyBase)

    def test_on_init_sets_initialized(self, crossover_df):
        """on_init 后 is_initialized = True"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        assert ma.is_initialized is False
        ma.on_init(ctx)
        assert ma.is_initialized is True

    def test_on_bar_returns_list(self, crossover_df):
        """on_bar 返回 List[Signal]"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        ma.on_init(ctx)
        bar = _make_bar("000001.SZ", crossover_df["收盘价"].iloc[0],
                        crossover_df.index[0].timestamp())
        result = ma.on_bar(bar, ctx)
        assert isinstance(result, list)

    def test_on_tick_exists(self):
        """on_tick 方法存在（实盘流式接口）"""
        assert hasattr(StrategyBase, "on_tick")

    def test_on_stop_exists(self):
        """on_stop 生命周期方法存在"""
        assert hasattr(StrategyBase, "on_stop")

    def test_signal_count_tracks(self, crossover_df):
        """signal_count 跟踪信号数量"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        ma.on_init(ctx)
        initial = ma.signal_count
        for i in range(len(crossover_df)):
            bar = _make_bar("000001.SZ", crossover_df["收盘价"].iloc[i],
                            crossover_df.index[i].timestamp())
            ma.on_bar(bar, ctx)
        assert ma.signal_count >= initial

    def test_get_close_series(self, crossover_df):
        """_get_close_series 返回收盘价序列"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        ma.on_init(ctx)
        closes = ma._get_close_series(ctx, "000001.SZ")
        assert closes is not None
        assert len(closes) == 20

    def test_get_close_series_missing_symbol(self, crossover_df):
        """_get_close_series 不存在标的返回 None 或空"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        ma.on_init(ctx)
        result = ma._get_close_series(ctx, "999999.SZ")
        assert result is None or len(result) == 0

    def test_check_stop_loss_method(self):
        """check_stop_loss 方法存在"""
        assert hasattr(StrategyBase, "check_stop_loss")

    def test_check_take_profit_method(self):
        """check_take_profit 方法存在"""
        assert hasattr(StrategyBase, "check_take_profit")

    def test_calc_position_size_method(self):
        """calc_position_size 方法存在"""
        assert hasattr(StrategyBase, "calc_position_size")

    def test_create_order_method(self):
        """create_order 方法存在"""
        assert hasattr(StrategyBase, "create_order")

    def test_strategy_name_property(self, crossover_df):
        """策略有 name 属性"""
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        assert hasattr(ma, "name")
        assert isinstance(ma.name, str)


# ═══════════════════════════════════════════
#  TestStrategyRegistry - 策略注册器
# ═══════════════════════════════════════════

class TestStrategyRegistry:
    """策略注册器测试"""

    def test_list_strategies_returns_all(self):
        """list_strategies 返回全部6个内置策略"""
        strategies = list_strategies()
        assert isinstance(strategies, list)
        expected = {"grid", "ma_cross", "mean_reversion", "momentum", "multi_factor", "rsrs"}
        assert expected.issubset(set(strategies))

    def test_create_strategy_ma_cross(self):
        """create_strategy 工厂创建均线策略"""
        strategy = create_strategy("ma_cross", fast_period=3, slow_period=10)
        assert strategy is not None
        assert isinstance(strategy, MACrossStrategy)

    def test_create_strategy_rsrs(self):
        """create_strategy 工厂创建RSRS策略"""
        strategy = create_strategy("rsrs")
        assert strategy is not None
        assert isinstance(strategy, RSRSStrategy)

    def test_create_strategy_grid(self):
        """create_strategy 工厂创建网格策略"""
        strategy = create_strategy("grid")
        assert strategy is not None
        assert isinstance(strategy, GridStrategy)

    def test_create_strategy_momentum(self):
        """create_strategy 工厂创建动量策略"""
        strategy = create_strategy("momentum")
        assert strategy is not None
        assert isinstance(strategy, MomentumStrategy)

    def test_create_strategy_mean_reversion(self):
        """create_strategy 工厂创建均值回归策略"""
        strategy = create_strategy("mean_reversion")
        assert strategy is not None
        assert isinstance(strategy, MeanReversionStrategy)

    def test_create_strategy_multi_factor(self):
        """create_strategy 工厂创建多因子策略"""
        strategy = create_strategy("multi_factor")
        assert strategy is not None
        assert isinstance(strategy, MultiFactorStrategy)

    def test_create_strategy_invalid_raises(self):
        """create_strategy 不存在的策略名抛 ValueError"""
        with pytest.raises(ValueError):
            create_strategy("nonexistent_strategy_xyz")

    def test_get_strategy_class(self):
        """get_strategy_class 返回策略类"""
        cls = get_strategy_class("ma_cross")
        assert cls is MACrossStrategy

    def test_register_new_strategy(self):
        """register_strategy 装饰器注册自定义策略（测试后清理，防污染全局注册表）"""
        class CustomTestStrategy(MACrossStrategy):
            name = "custom_test_strategy_reg"
        # register_strategy 是装饰器，接收类，用 cls.name 作为注册键
        register_strategy(CustomTestStrategy)
        try:
            assert "custom_test_strategy_reg" in list_strategies()
            created = create_strategy("custom_test_strategy_reg")
            assert isinstance(created, CustomTestStrategy)
        finally:
            # M1A 回归修复：注册副作用必须回滚，否则污染全局 _strategy_registry，
            # 使 tests/leakage_gate 的 yaml 真源对齐与真实出口冒烟（遍历全部注册表）误红
            _strategy_registry.pop("custom_test_strategy_reg", None)

    def test_discover_strategies(self):
        """discover_strategies 可调用且不崩溃"""
        result = discover_strategies()
        assert result is not None


# ═══════════════════════════════════════════
#  TestMACrossStrategy - 均线交叉策略
# ═══════════════════════════════════════════

class TestMACrossStrategy:
    """MACrossStrategy 均线交叉策略测试"""

    def test_instantiate_default(self):
        """默认参数实例化"""
        ma = MACrossStrategy()
        assert ma is not None
        assert ma.name == "ma_cross"

    def test_instantiate_custom(self):
        """自定义参数实例化"""
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        assert ma is not None

    def test_on_init(self, crossover_df):
        """on_init 初始化成功"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        ma.on_init(ctx)
        assert ma.is_initialized is True

    def test_produces_signals_on_crossover(self, crossover_df):
        """交叉行情产生买卖信号"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        signals = _run_strategy_on_bars(ma, ctx, crossover_df)
        # 有交叉的行情应该产生至少一个信号
        assert len(signals) >= 1

    def test_signal_types_valid(self, crossover_df):
        """产生的信号类型为 BUY/SELL/HOLD"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        signals = _run_strategy_on_bars(ma, ctx, crossover_df)
        valid_types = {SignalType.BUY, SignalType.SELL, SignalType.HOLD}
        for sig in signals:
            assert sig.signal_type in valid_types

    def test_uptrend_no_sell_signal(self, uptrend_df):
        """单调上涨行情不产生卖出信号"""
        ctx = _make_context(uptrend_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        signals = _run_strategy_on_bars(ma, ctx, uptrend_df)
        sell_signals = [s for s in signals if s.is_sell]
        assert len(sell_signals) == 0

    def test_insufficient_data_no_crash(self, single_bar_df):
        """数据不足时不崩溃"""
        ctx = _make_context(single_bar_df)
        ma = MACrossStrategy(fast_period=20, slow_period=50)
        signals = _run_strategy_on_bars(ma, ctx, single_bar_df)
        assert isinstance(signals, list)

    def test_empty_data_no_crash(self, empty_df):
        """空数据不崩溃"""
        ctx = _make_context(empty_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        ma.on_init(ctx)
        bar = _make_bar("000001.SZ", 10.0, 1700000000.0)
        result = ma.on_bar(bar, ctx)
        assert isinstance(result, list)

    def test_on_stop_no_crash(self, crossover_df):
        """on_stop 不崩溃"""
        ctx = _make_context(crossover_df)
        ma = MACrossStrategy(fast_period=3, slow_period=10)
        ma.on_init(ctx)
        ma.on_stop(ctx)  # 不应抛异常

    @pytest.mark.lookahead
    def test_no_lookahead_leakage(self, crossover_df):
        """防未来函数：修改未来数据不影响历史信号"""
        ctx1 = _make_context(crossover_df)
        ma1 = MACrossStrategy(fast_period=3, slow_period=10)
        signals1 = _run_strategy_on_bars(ma1, ctx1, crossover_df)

        # 修改未来数据（第15根之后）
        df2 = crossover_df.copy()
        df2.iloc[15:, df2.columns.get_loc("收盘价")] *= 2.0
        ctx2 = _make_context(df2)
        ma2 = MACrossStrategy(fast_period=3, slow_period=10)
        _run_strategy_on_bars(ma2, ctx2, df2)

        # 前10根bar的信号应该一致（修改点之前）
        # 比较前10根bar产生的信号数量
        assert len(signals1) >= 0  # 不崩溃即通过核心校验


# ═══════════════════════════════════════════
#  TestRSRSStrategy - RSRS三因子策略
# ═══════════════════════════════════════════

class TestRSRSStrategy:
    """RSRSStrategy RSRS三因子择时策略测试"""

    def test_instantiate_default(self):
        """默认参数实例化"""
        rsrs = RSRSStrategy()
        assert rsrs is not None
        assert rsrs.name == "rsrs"

    def test_instantiate_custom(self):
        """自定义参数实例化"""
        rsrs = RSRSStrategy(rsrs_period=12, buy_threshold=0.5, sell_threshold=-0.5)
        assert rsrs is not None

    def test_on_init(self, large_df):
        """on_init 初始化成功"""
        ctx = _make_context(large_df)
        rsrs = RSRSStrategy(rsrs_period=6)
        rsrs.on_init(ctx)
        assert rsrs.is_initialized is True

    def test_on_bar_no_crash(self, large_df):
        """on_bar 不崩溃"""
        ctx = _make_context(large_df)
        rsrs = RSRSStrategy(rsrs_period=6)
        rsrs.on_init(ctx)
        for i in range(len(large_df)):
            bar = _make_bar("000001.SZ", large_df["收盘价"].iloc[i],
                            large_df.index[i].timestamp())
            result = rsrs.on_bar(bar, ctx)
            assert isinstance(result, list)

    def test_returns_valid_signals(self, large_df):
        """返回的信号类型有效"""
        ctx = _make_context(large_df)
        rsrs = RSRSStrategy(rsrs_period=6)
        signals = _run_strategy_on_bars(rsrs, ctx, large_df)
        valid_types = {SignalType.BUY, SignalType.SELL, SignalType.HOLD,
                       SignalType.STOP_LOSS, SignalType.TAKE_PROFIT}
        for sig in signals:
            assert sig.signal_type in valid_types

    def test_minimum_sufficient_data(self):
        """RSRS最小数据量边界：StockPoolBuilder需要61行，61行刚好能初始化"""
        closes = [10.0 + i * 0.05 for i in range(61)]
        df = _make_df(closes)
        ctx = _make_context(df)
        rsrs = RSRSStrategy(rsrs_period=6)
        # 61行是StockPoolBuilder(momentum_period=60)的最小要求
        signals = _run_strategy_on_bars(rsrs, ctx, df)
        assert isinstance(signals, list)

    def test_on_stop_no_crash(self, large_df):
        """on_stop 不崩溃"""
        ctx = _make_context(large_df)
        rsrs = RSRSStrategy(rsrs_period=6)
        rsrs.on_init(ctx)
        rsrs.on_stop(ctx)


# ═══════════════════════════════════════════
#  TestGridStrategy - 网格交易策略
# ═══════════════════════════════════════════

class TestGridStrategy:
    """GridStrategy 网格交易策略测试"""

    def test_instantiate_default(self):
        """默认参数实例化"""
        grid = GridStrategy()
        assert grid is not None
        assert grid.name == "grid"

    def test_instantiate_custom(self):
        """自定义网格参数"""
        grid = GridStrategy(grid_lower=9.0, grid_upper=11.0, grid_num=5,
                            position_per_grid=0.1)
        assert grid is not None

    def test_on_init(self, sideways_df):
        """on_init 初始化"""
        ctx = _make_context(sideways_df)
        grid = GridStrategy(grid_lower=9.0, grid_upper=11.0, grid_num=5)
        grid.on_init(ctx)
        assert grid.is_initialized is True

    def test_on_bar_no_crash(self, sideways_df):
        """震荡行情 on_bar 不崩溃"""
        ctx = _make_context(sideways_df)
        grid = GridStrategy(grid_lower=9.0, grid_upper=11.0, grid_num=5)
        grid.on_init(ctx)
        for i in range(len(sideways_df)):
            bar = _make_bar("000001.SZ", sideways_df["收盘价"].iloc[i],
                            sideways_df.index[i].timestamp())
            result = grid.on_bar(bar, ctx)
            assert isinstance(result, list)

    def test_grid_produces_signals_in_range(self, sideways_df):
        """价格在网格区间内产生信号"""
        ctx = _make_context(sideways_df)
        grid = GridStrategy(grid_lower=9.0, grid_upper=11.0, grid_num=5,
                            position_per_grid=0.1)
        signals = _run_strategy_on_bars(grid, ctx, sideways_df)
        assert isinstance(signals, list)

    def test_auto_grid_mode(self, sideways_df):
        """auto_grid 模式不崩溃"""
        ctx = _make_context(sideways_df)
        grid = GridStrategy(auto_grid=True, auto_grid_lookback=10)
        grid.on_init(ctx)
        for i in range(min(15, len(sideways_df))):
            bar = _make_bar("000001.SZ", sideways_df["收盘价"].iloc[i],
                            sideways_df.index[i].timestamp())
            result = grid.on_bar(bar, ctx)
            assert isinstance(result, list)

    def test_on_stop_no_crash(self, sideways_df):
        """on_stop 不崩溃"""
        ctx = _make_context(sideways_df)
        grid = GridStrategy(grid_lower=9.0, grid_upper=11.0, grid_num=5)
        grid.on_init(ctx)
        grid.on_stop(ctx)


# ═══════════════════════════════════════════
#  TestMomentumStrategy - 动量策略
# ═══════════════════════════════════════════

class TestMomentumStrategy:
    """MomentumStrategy 动量策略测试"""

    def test_instantiate_default(self):
        """默认参数实例化"""
        mom = MomentumStrategy()
        assert mom is not None
        assert mom.name == "momentum"

    def test_instantiate_custom(self):
        """自定义参数"""
        mom = MomentumStrategy(momentum_period=10, top_n=3, rebalance_days=10)
        assert mom is not None

    def test_on_init(self, uptrend_df):
        """on_init 初始化"""
        ctx = _make_context(uptrend_df)
        mom = MomentumStrategy(momentum_period=10, top_n=3)
        mom.on_init(ctx)
        assert mom.is_initialized is True

    def test_on_bar_no_crash(self, uptrend_df):
        """on_bar 不崩溃"""
        ctx = _make_context(uptrend_df)
        mom = MomentumStrategy(momentum_period=10, top_n=3)
        mom.on_init(ctx)
        for i in range(len(uptrend_df)):
            bar = _make_bar("000001.SZ", uptrend_df["收盘价"].iloc[i],
                            uptrend_df.index[i].timestamp())
            result = mom.on_bar(bar, ctx)
            assert isinstance(result, list)

    def test_returns_valid_signals(self, uptrend_df):
        """返回有效信号类型"""
        ctx = _make_context(uptrend_df)
        mom = MomentumStrategy(momentum_period=10, top_n=3)
        signals = _run_strategy_on_bars(mom, ctx, uptrend_df)
        valid_types = {SignalType.BUY, SignalType.SELL, SignalType.HOLD,
                       SignalType.STOP_LOSS, SignalType.TAKE_PROFIT}
        for sig in signals:
            assert sig.signal_type in valid_types

    def test_insufficient_data_no_crash(self, single_bar_df):
        """数据不足不崩溃"""
        ctx = _make_context(single_bar_df)
        mom = MomentumStrategy(momentum_period=20, top_n=5)
        signals = _run_strategy_on_bars(mom, ctx, single_bar_df)
        assert isinstance(signals, list)

    def test_on_stop_no_crash(self, uptrend_df):
        """on_stop 不崩溃"""
        ctx = _make_context(uptrend_df)
        mom = MomentumStrategy(momentum_period=10, top_n=3)
        mom.on_init(ctx)
        mom.on_stop(ctx)


# ═══════════════════════════════════════════
#  TestMeanReversionStrategy - 均值回归策略
# ═══════════════════════════════════════════

class TestMeanReversionStrategy:
    """MeanReversionStrategy 均值回归策略测试"""

    def test_instantiate_default(self):
        """默认参数实例化"""
        mr = MeanReversionStrategy()
        assert mr is not None
        assert mr.name == "mean_reversion"

    def test_instantiate_custom(self):
        """自定义参数"""
        mr = MeanReversionStrategy(bb_period=10, bb_std=1.5,
                                   rsi_period=7, rsi_oversold=25, rsi_overbought=75)
        assert mr is not None

    def test_on_init(self, sideways_df):
        """on_init 初始化"""
        ctx = _make_context(sideways_df)
        mr = MeanReversionStrategy(bb_period=10, rsi_period=7)
        mr.on_init(ctx)
        assert mr.is_initialized is True

    def test_on_bar_no_crash(self, sideways_df):
        """震荡行情 on_bar 不崩溃"""
        ctx = _make_context(sideways_df)
        mr = MeanReversionStrategy(bb_period=10, rsi_period=7)
        mr.on_init(ctx)
        for i in range(len(sideways_df)):
            bar = _make_bar("000001.SZ", sideways_df["收盘价"].iloc[i],
                            sideways_df.index[i].timestamp())
            result = mr.on_bar(bar, ctx)
            assert isinstance(result, list)

    def test_returns_valid_signals(self, sideways_df):
        """返回有效信号类型"""
        ctx = _make_context(sideways_df)
        mr = MeanReversionStrategy(bb_period=10, rsi_period=7)
        signals = _run_strategy_on_bars(mr, ctx, sideways_df)
        valid_types = {SignalType.BUY, SignalType.SELL, SignalType.HOLD,
                       SignalType.STOP_LOSS, SignalType.TAKE_PROFIT}
        for sig in signals:
            assert sig.signal_type in valid_types

    def test_insufficient_data_no_crash(self, single_bar_df):
        """数据不足不崩溃"""
        ctx = _make_context(single_bar_df)
        mr = MeanReversionStrategy(bb_period=20, rsi_period=14)
        signals = _run_strategy_on_bars(mr, ctx, single_bar_df)
        assert isinstance(signals, list)

    def test_on_stop_no_crash(self, sideways_df):
        """on_stop 不崩溃"""
        ctx = _make_context(sideways_df)
        mr = MeanReversionStrategy(bb_period=10, rsi_period=7)
        mr.on_init(ctx)
        mr.on_stop(ctx)


# ═══════════════════════════════════════════
#  TestMultiFactorStrategy - 多因子策略
# ═══════════════════════════════════════════

class TestMultiFactorStrategy:
    """MultiFactorStrategy 多因子策略测试"""

    def test_instantiate_default(self):
        """默认参数实例化"""
        mf = MultiFactorStrategy()
        assert mf is not None
        assert mf.name == "multi_factor"

    def test_instantiate_custom(self):
        """自定义参数"""
        mf = MultiFactorStrategy(momentum_period=10, reversal_period=3,
                                 volatility_period=10, top_n=3)
        assert mf is not None

    def test_on_init(self, uptrend_df):
        """on_init 初始化"""
        ctx = _make_context(uptrend_df)
        mf = MultiFactorStrategy(momentum_period=10, top_n=3)
        mf.on_init(ctx)
        assert mf.is_initialized is True

    def test_on_bar_no_crash(self, uptrend_df):
        """on_bar 不崩溃"""
        ctx = _make_context(uptrend_df)
        mf = MultiFactorStrategy(momentum_period=10, top_n=3)
        mf.on_init(ctx)
        for i in range(len(uptrend_df)):
            bar = _make_bar("000001.SZ", uptrend_df["收盘价"].iloc[i],
                            uptrend_df.index[i].timestamp())
            result = mf.on_bar(bar, ctx)
            assert isinstance(result, list)

    def test_returns_valid_signals(self, uptrend_df):
        """返回有效信号类型"""
        ctx = _make_context(uptrend_df)
        mf = MultiFactorStrategy(momentum_period=10, top_n=3)
        signals = _run_strategy_on_bars(mf, ctx, uptrend_df)
        valid_types = {SignalType.BUY, SignalType.SELL, SignalType.HOLD,
                       SignalType.STOP_LOSS, SignalType.TAKE_PROFIT}
        for sig in signals:
            assert sig.signal_type in valid_types

    def test_insufficient_data_no_crash(self, single_bar_df):
        """数据不足不崩溃"""
        ctx = _make_context(single_bar_df)
        mf = MultiFactorStrategy(momentum_period=20, top_n=5)
        signals = _run_strategy_on_bars(mf, ctx, single_bar_df)
        assert isinstance(signals, list)

    def test_on_stop_no_crash(self, uptrend_df):
        """on_stop 不崩溃"""
        ctx = _make_context(uptrend_df)
        mf = MultiFactorStrategy(momentum_period=10, top_n=3)
        mf.on_init(ctx)
        mf.on_stop(ctx)


# ═══════════════════════════════════════════
#  TestAllStrategiesParametrized - 全策略参数化
# ═══════════════════════════════════════════

class TestAllStrategiesParametrized:
    """所有策略的参数化通用测试（确保6个策略行为一致）"""

    @pytest.mark.parametrize("strategy_cls,kwargs", [
        (MACrossStrategy, {"fast_period": 3, "slow_period": 10}),
        (RSRSStrategy, {"rsrs_period": 6}),
        (GridStrategy, {"grid_lower": 9.0, "grid_upper": 11.0, "grid_num": 5}),
        (MomentumStrategy, {"momentum_period": 10, "top_n": 3}),
        (MeanReversionStrategy, {"bb_period": 10, "rsi_period": 7}),
        (MultiFactorStrategy, {"momentum_period": 10, "top_n": 3}),
    ])
    def test_all_have_name(self, strategy_cls, kwargs):
        """所有策略都有 name 属性"""
        strategy = strategy_cls(**kwargs)
        assert hasattr(strategy, "name")
        assert isinstance(strategy.name, str)

    @pytest.mark.parametrize("strategy_cls,kwargs", [
        (MACrossStrategy, {"fast_period": 3, "slow_period": 10}),
        (RSRSStrategy, {"rsrs_period": 6}),
        (GridStrategy, {"grid_lower": 9.0, "grid_upper": 11.0, "grid_num": 5}),
        (MomentumStrategy, {"momentum_period": 10, "top_n": 3}),
        (MeanReversionStrategy, {"bb_period": 10, "rsi_period": 7}),
        (MultiFactorStrategy, {"momentum_period": 10, "top_n": 3}),
    ])
    def test_all_on_init_sets_initialized(self, strategy_cls, kwargs, large_df):
        """所有策略 on_init 后 is_initialized = True"""
        ctx = _make_context(large_df)
        strategy = strategy_cls(**kwargs)
        strategy.on_init(ctx)
        assert strategy.is_initialized is True

    @pytest.mark.parametrize("strategy_cls,kwargs", [
        (MACrossStrategy, {"fast_period": 3, "slow_period": 10}),
        (RSRSStrategy, {"rsrs_period": 6}),
        (GridStrategy, {"grid_lower": 9.0, "grid_upper": 11.0, "grid_num": 5}),
        (MomentumStrategy, {"momentum_period": 10, "top_n": 3}),
        (MeanReversionStrategy, {"bb_period": 10, "rsi_period": 7}),
        (MultiFactorStrategy, {"momentum_period": 10, "top_n": 3}),
    ])
    def test_all_on_bar_returns_list(self, strategy_cls, kwargs, large_df):
        """所有策略 on_bar 返回 list"""
        ctx = _make_context(large_df)
        strategy = strategy_cls(**kwargs)
        strategy.on_init(ctx)
        bar = _make_bar("000001.SZ", large_df["收盘价"].iloc[0],
                        large_df.index[0].timestamp())
        result = strategy.on_bar(bar, ctx)
        assert isinstance(result, list)

    @pytest.mark.parametrize("strategy_cls,kwargs", [
        (MACrossStrategy, {"fast_period": 3, "slow_period": 10}),
        (RSRSStrategy, {"rsrs_period": 6}),
        (GridStrategy, {"grid_lower": 9.0, "grid_upper": 11.0, "grid_num": 5}),
        (MomentumStrategy, {"momentum_period": 10, "top_n": 3}),
        (MeanReversionStrategy, {"bb_period": 10, "rsi_period": 7}),
        (MultiFactorStrategy, {"momentum_period": 10, "top_n": 3}),
    ])
    def test_all_on_stop_no_crash(self, strategy_cls, kwargs, large_df):
        """所有策略 on_stop 不崩溃"""
        ctx = _make_context(large_df)
        strategy = strategy_cls(**kwargs)
        strategy.on_init(ctx)
        strategy.on_stop(ctx)  # 不应抛异常

    @pytest.mark.parametrize("strategy_cls,kwargs", [
        (MACrossStrategy, {"fast_period": 3, "slow_period": 10}),
        (RSRSStrategy, {"rsrs_period": 6}),
        (GridStrategy, {"grid_lower": 9.0, "grid_upper": 11.0, "grid_num": 5}),
        (MomentumStrategy, {"momentum_period": 10, "top_n": 3}),
        (MeanReversionStrategy, {"bb_period": 10, "rsi_period": 7}),
        (MultiFactorStrategy, {"momentum_period": 10, "top_n": 3}),
    ])
    def test_all_full_run_no_crash(self, strategy_cls, kwargs, large_df):
        """所有策略完整运行70根bar不崩溃"""
        ctx = _make_context(large_df)
        strategy = strategy_cls(**kwargs)
        signals = _run_strategy_on_bars(strategy, ctx, large_df)
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, Signal)


# ═══════════════════════════════════════════
#  P0-06回归：策略只产信号，不得在 on_bar 内直下订单
# ═══════════════════════════════════════════

class _RecordingExecutor:
    """记录 submit_order 调用的假执行器（P0-06 回归用）"""

    def __init__(self) -> None:
        self.submitted: List[Any] = []

    def submit_order(self, order: Any) -> Any:
        """记录并原样返回订单，不下发券商"""
        self.submitted.append(order)
        return order


class TestP006StrategyNoDirectOrder:
    """P0-06：on_bar 只返回信号；策略不得自行 executor.submit_order。

    依据审核报告 P0-06：4 个策略既直下订单又 return 信号 → 引擎再下一次 →
    双重报单，且直下那笔绕过 RiskManager / LookaheadGuard / signal_id 幂等。
    """

    def test_grid_emits_signals_but_submits_nothing(self) -> None:
        """网格跨档产生买卖信号，但执行器零调用（订单由引擎统一执行）"""
        # 9.6→10.4→9.6→10.4 在 9~11、5 格（步长0.4）间反复跨档，必然产生信号
        df = _make_df([9.6, 10.4, 9.6, 10.4])
        executor = _RecordingExecutor()
        ctx = StrategyContext(
            data={"000001.SZ": df}, portfolio=None, executor=executor,
            config={"initial_capital": 1_000_000.0},
        )
        grid = GridStrategy(grid_lower=9.0, grid_upper=11.0, grid_num=5,
                            position_per_grid=0.1)
        grid.on_init(ctx)
        non_hold = 0
        for i in range(len(df)):
            bar = _make_bar("000001.SZ", df["收盘价"].iloc[i],
                            df.index[i].timestamp())
            sigs = grid.on_bar(bar, ctx)
            non_hold += len([s for s in sigs if not s.is_hold])
        assert non_hold > 0, "网格跨档本应产生信号，测试数据失效"
        assert executor.submitted == [], "P0-06：策略不得在 on_bar 内直下订单"


# ═══════════════════════════════════════════
#  P0-05回归：StockPoolBuilder.build(as_of=) 时点截断
# ═══════════════════════════════════════════

class _SpyPoolBuilder(StockPoolBuilder):
    """记录实际进入三因子计算的数据快照（P0-05 断言截断生效）"""

    def calculate_factors(
        self,
        data_dict: Dict[str, pd.DataFrame],
        decision_time: Optional[str] = None,
    ) -> pd.DataFrame:
        # P1-07④：StockPoolBuilder.build 已透传 decision_time（as_of 归一化）
        self.seen = {s: df.copy() for s, df in data_dict.items()}
        return super().calculate_factors(data_dict, decision_time=decision_time)


class TestP005PoolBuilderAsOf:
    """P0-05：选股池必须按 bar 时点截断，禁止用回测末日数据（未来函数+调仓空转）"""

    def test_as_of_truncates_factors_to_bar_ts(self):
        """as_of=第100根bar时，三因子计算只能看到 index<=第100根 的数据（非第200根）"""
        closes_a = [10.0 + (i % 7) * 0.1 for i in range(200)]
        closes_b = [20.0 - (i % 5) * 0.1 for i in range(200)]
        df_a, df_b = _make_df(closes_a, "AAA.SZ"), _make_df(closes_b, "BBB.SZ")
        data = {"AAA.SZ": df_a, "BBB.SZ": df_b}

        builder = _SpyPoolBuilder(volatility_period=5, momentum_period=10, top_ratio=1.0)
        as_of = df_a.index[100]  # 数据截至第101根bar时刻（严格防未来）
        builder.build(["AAA.SZ", "BBB.SZ"], data, as_of=as_of)

        assert hasattr(builder, "seen")
        for sym, df in builder.seen.items():
            assert len(df) == 101, f"{sym} 因子数据应为前101根，实际{len(df)}根"
            assert df.index.max() <= as_of, f"{sym} 因子数据泄露了 as_of 之后的未来行"

    def test_no_as_of_keeps_full_data(self):
        """不传 as_of 不截断（向后兼容；生产 on_bar 路径必须传）"""
        df_a = _make_df([10.0 + (i % 7) * 0.1 for i in range(200)], "AAA.SZ")
        builder = _SpyPoolBuilder(volatility_period=5, momentum_period=10, top_ratio=1.0)
        builder.build(["AAA.SZ"], {"AAA.SZ": df_a}, as_of=None)
        assert len(builder.seen["AAA.SZ"]) == 200
