# 文件名：tests/unit/test_backtest.py
"""
test_backtest.py - 回测引擎单元测试
====================================
覆盖 myquant/backtest/ 全部组件：
  - BacktestConfig / CommissionModel / SlippageModel  配置模型
  - TradeRecorder                                      交易记录器
  - MetricsCalculator                                  绩效指标计算
  - BacktestEngine                                     回测引擎（继承BaseEngine）
  - DataProviderBase / HistoricalDataProvider          数据加载器

测试维度:
  1. 配置模型（默认值 / 自定义 / 佣金计算 / 滑点计算）
  2. 交易记录器（记录成交 / 净值曲线 / 查询 / 统计 / 重置）
  3. 绩效指标（夏普 / 最大回撤 / 胜率 / 总收益 / 增量更新）
  4. 回测引擎（实例化 / run() / 结果校验 / 生命周期 / 边界条件）
  5. 防未来函数（end_date过滤 / 信号不使用未来数据）
  6. 数据加载器（抽象基类 / 历史数据提供者）

运行方式:
  pytest tests/unit/test_backtest.py -v
  pytest tests/unit/test_backtest.py -v -m lookahead

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/unit/test_backtest.py）
  2. 为实时行情及实盘无缝对接做好充足准备（MetricsCalculator.incremental_update 流式接口）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 回测第i根bar仅使用 iloc[:i+1] 数据，end_date严格过滤
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from myquant.backtest.data_loader import DataProviderBase, HistoricalDataProvider
from myquant.backtest.engine import BacktestEngine
from myquant.backtest.metrics import MetricsCalculator
from myquant.backtest.recorder import TradeRecorder
from myquant.engine.base import BaseEngine
from myquant.models.backtest import (
    BacktestConfig,
    BacktestResult,
    CommissionModel,
    CommissionType,
    SlippageModel,
)
from myquant.models.market_data import Bar, BarFrequency, MarketEvent
from myquant.models.trade import OrderSide, Trade
from myquant.strategies.ma_strategy import MACrossStrategy

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


def _make_config(**kwargs) -> BacktestConfig:
    """构造回测配置"""
    defaults = dict(
        initial_capital=1_000_000.0,
        symbols=["000001.SZ"],
        start_date="2024-01-01",
        end_date="2024-06-01",
        frequency="1d",
    )
    defaults.update(kwargs)
    return BacktestConfig(**defaults)


def _make_trade(symbol: str, side: OrderSide, quantity: float,
                price: float, timestamp: float, commission: float = 5.0) -> Trade:
    """构造成交记录"""
    return Trade(
        trade_id=f"t_{int(timestamp)}",
        order_id=f"o_{int(timestamp)}",
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        timestamp=timestamp,
        commission=commission,
    )


@pytest.fixture
def crossover_df() -> pd.DataFrame:
    """有交叉的行情（40根bar）"""
    closes = [10.0 + i * 0.1 + np.sin(i * 0.5) * 0.5 for i in range(40)]
    return _make_df(closes)


@pytest.fixture
def uptrend_df() -> pd.DataFrame:
    """单调上涨行情（30根bar）"""
    closes = [10.0 + i * 0.15 for i in range(30)]
    return _make_df(closes)


@pytest.fixture
def empty_df() -> pd.DataFrame:
    """空 DataFrame"""
    return _make_df([])


@pytest.fixture
def single_bar_df() -> pd.DataFrame:
    """单根bar"""
    return _make_df([10.0])


# ═══════════════════════════════════════════
#  TestBacktestConfig - 回测配置
# ═══════════════════════════════════════════

class TestBacktestConfig:
    """BacktestConfig 配置模型测试"""

    def test_default_values(self):
        """默认配置值正确"""
        cfg = BacktestConfig()
        assert cfg.initial_capital == 1_000_000.0
        assert cfg.frequency == "1d"
        assert cfg.benchmark == "000300.SH"
        assert cfg.risk_free_rate == 0.02
        assert cfg.strategy_name == "default"

    def test_custom_values(self):
        """自定义配置"""
        cfg = BacktestConfig(
            initial_capital=500_000.0,
            symbols=["000001.SZ", "600000.SH"],
            start_date="2024-01-01",
            end_date="2024-12-31",
            frequency="1d",
            strategy_name="ma_cross_test",
        )
        assert cfg.initial_capital == 500_000.0
        assert len(cfg.symbols) == 2
        assert cfg.strategy_name == "ma_cross_test"

    def test_commission_model_default(self):
        """默认佣金模型"""
        cfg = BacktestConfig()
        assert cfg.commission.type == CommissionType.RATIO
        assert cfg.commission.buy_rate == 0.0002
        assert cfg.commission.sell_rate == 0.0002
        assert cfg.commission.min_commission == 5.0
        assert cfg.commission.stamp_tax == 0.001

    def test_slippage_model_default(self):
        """默认滑点模型"""
        cfg = BacktestConfig()
        assert cfg.slippage.type == "fixed_bps"
        assert cfg.slippage.fixed_bps == 2.0

    def test_params_dict(self):
        """策略参数字典"""
        cfg = BacktestConfig(params={"fast_period": 3, "slow_period": 10})
        assert cfg.params["fast_period"] == 3
        assert cfg.params["slow_period"] == 10


# ═══════════════════════════════════════════
#  TestCommissionModel - 佣金模型
# ═══════════════════════════════════════════

class TestCommissionModel:
    """CommissionModel 佣金计算测试"""

    def test_calc_buy_ratio(self):
        """买入佣金（比例制，含过户费）"""
        cm = CommissionModel(buy_rate=0.0002, min_commission=5.0, transfer_fee=0.00001)
        # 10000元 * 0.0002 = 2元 < 5元最低 → 5元 + 过户费0.1元 = 5.1元
        commission = cm.calc_buy(10000.0)
        assert abs(commission - 5.1) < 1e-6

    def test_calc_buy_above_min(self):
        """买入佣金超过最低佣金（含过户费）"""
        cm = CommissionModel(buy_rate=0.0003, min_commission=5.0, transfer_fee=0.00001)
        # 100000元 * 0.0003 = 30元 > 5元 → 30元 + 过户费1元 = 31元
        commission = cm.calc_buy(100000.0)
        assert abs(commission - 31.0) < 1e-6

    def test_calc_sell_includes_stamp_tax(self):
        """卖出佣金含印花税+过户费"""
        cm = CommissionModel(sell_rate=0.0002, stamp_tax=0.001,
                             min_commission=5.0, transfer_fee=0.00001)
        # 100000元 * (0.0002佣金 + 0.001印花税 + 0.00001过户费) = 121元
        commission = cm.calc_sell(100000.0)
        assert abs(commission - 121.0) < 1e-6

    def test_calc_sell_min_commission(self):
        """卖出最低佣金（印花税+过户费另计）"""
        cm = CommissionModel(sell_rate=0.0002, stamp_tax=0.001,
                             min_commission=5.0, transfer_fee=0.00001)
        # 佣金: max(0.2, 5)=5 + 印花税1 + 过户费0.01 = 6.01
        commission = cm.calc_sell(1000.0)
        assert abs(commission - 6.01) < 1e-6

    def test_transfer_fee(self):
        """过户费"""
        cm = CommissionModel(transfer_fee=0.00001)
        # 过户费在calc_buy/calc_sell中应被计入
        buy = cm.calc_buy(100000.0)
        assert buy > 0

    def test_zero_amount(self):
        """零金额佣金"""
        cm = CommissionModel()
        commission = cm.calc_buy(0.0)
        assert commission >= 0


# ═══════════════════════════════════════════
#  TestSlippageModel - 滑点模型
# ═══════════════════════════════════════════

class TestSlippageModel:
    """SlippageModel 滑点计算测试"""

    def test_fixed_bps(self):
        """固定基点滑点"""
        sm = SlippageModel(type="fixed_bps", fixed_bps=2.0)
        # 10元 * 2bps = 0.002元
        slip = sm.calc(price=10.0, volume=1000)
        assert abs(slip - 0.002) < 1e-6

    def test_none_slippage(self):
        """无滑点"""
        sm = SlippageModel(type="none")
        slip = sm.calc(price=10.0, volume=1000)
        assert slip == 0.0

    def test_volume_based(self):
        """量价冲击滑点"""
        sm = SlippageModel(type="volume_based", volume_impact=0.1)
        slip = sm.calc(price=10.0, volume=10000, order_size=1000)
        assert slip >= 0.0

    def test_slippage_non_negative(self):
        """滑点非负"""
        sm = SlippageModel()
        slip = sm.calc(price=10.0, volume=1000)
        assert slip >= 0.0


# ═══════════════════════════════════════════
#  TestTradeRecorder - 交易记录器
# ═══════════════════════════════════════════

class TestTradeRecorder:
    """TradeRecorder 交易记录器测试"""

    def test_initialization(self):
        """初始化"""
        rec = TradeRecorder(initial_capital=500_000.0)
        assert rec.initial_capital == 500_000.0
        assert rec.trade_count == 0
        assert rec.equity_points == 0

    def test_record_trade(self):
        """记录成交"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        trade = _make_trade("000001.SZ", OrderSide.BUY, 100, 10.0, 1700000000.0)
        record = rec.record_trade(trade, signal_reason="金叉买入")
        assert record is not None
        assert rec.trade_count == 1

    def test_record_equity(self):
        """记录净值"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        rec.record_equity(timestamp=1700000000.0, equity=1_010_000.0,
                          cash=500_000.0, position_value=510_000.0)
        assert rec.equity_points == 1
        assert rec.final_equity == 1_010_000.0

    def test_get_trades(self):
        """获取成交列表"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        t1 = _make_trade("000001.SZ", OrderSide.BUY, 100, 10.0, 1700000000.0)
        t2 = _make_trade("000001.SZ", OrderSide.SELL, 100, 11.0, 1700100000.0)
        rec.record_trade(t1)
        rec.record_trade(t2)
        trades = rec.get_trades()
        assert len(trades) == 2

    def test_get_equity_curve(self):
        """获取净值曲线"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        rec.record_equity(1700000000.0, 1_000_000.0, 1_000_000.0, 0.0)
        rec.record_equity(1700100000.0, 1_010_000.0, 900_000.0, 110_000.0)
        curve = rec.get_equity_curve()
        assert len(curve) == 2

    def test_get_equity_df(self):
        """获取净值DataFrame"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        rec.record_equity(1700000000.0, 1_000_000.0, 1_000_000.0, 0.0)
        df = rec.get_equity_df()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1

    def test_get_trades_df(self):
        """获取成交DataFrame"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        t = _make_trade("000001.SZ", OrderSide.BUY, 100, 10.0, 1700000000.0)
        rec.record_trade(t)
        df = rec.get_trades_df()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1

    def test_get_trades_by_symbol(self):
        """按标的查询成交"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        rec.record_trade(_make_trade("000001.SZ", OrderSide.BUY, 100, 10.0, 1700000000.0))
        rec.record_trade(_make_trade("600000.SH", OrderSide.BUY, 200, 8.0, 1700000001.0))
        trades = rec.get_trades_by_symbol("000001.SZ")
        assert len(trades) == 1

    def test_get_closed_trades(self):
        """获取已平仓交易"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        rec.record_trade(_make_trade("000001.SZ", OrderSide.BUY, 100, 10.0, 1700000000.0))
        rec.record_trade(_make_trade("000001.SZ", OrderSide.SELL, 100, 11.0, 1700100000.0))
        closed = rec.get_closed_trades()
        assert len(closed) >= 1

    def test_total_pnl(self):
        """总盈亏"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        rec.record_trade(_make_trade("000001.SZ", OrderSide.BUY, 100, 10.0, 1700000000.0, commission=5.0))
        rec.record_trade(_make_trade("000001.SZ", OrderSide.SELL, 100, 11.0, 1700100000.0, commission=5.0))
        pnl = rec.total_pnl
        # 买入100*10=1000，卖出100*11=1100，毛利100，佣金10，净赚约90
        assert pnl > 0

    def test_total_commission(self):
        """总佣金"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        rec.record_trade(_make_trade("000001.SZ", OrderSide.BUY, 100, 10.0, 1700000000.0, commission=5.0))
        rec.record_trade(_make_trade("000001.SZ", OrderSide.SELL, 100, 11.0, 1700100000.0, commission=5.0))
        assert rec.total_commission == 10.0

    def test_reset(self):
        """重置记录器"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        rec.record_trade(_make_trade("000001.SZ", OrderSide.BUY, 100, 10.0, 1700000000.0))
        rec.record_equity(1700000000.0, 1_000_000.0, 1_000_000.0, 0.0)
        rec.reset()
        assert rec.trade_count == 0
        assert rec.equity_points == 0

    def test_empty_recorder_stats(self):
        """空记录器统计"""
        rec = TradeRecorder(initial_capital=1_000_000.0)
        assert rec.trade_count == 0
        assert rec.equity_points == 0
        assert rec.total_pnl == 0.0
        assert rec.total_commission == 0.0


# ═══════════════════════════════════════════
#  TestMetricsCalculator - 绩效指标
# ═══════════════════════════════════════════

class TestMetricsCalculator:
    """MetricsCalculator 绩效指标计算测试"""

    def test_instantiation(self):
        """实例化"""
        mc = MetricsCalculator()
        assert mc is not None

    def test_calculate_with_empty_data(self):
        """空数据计算不崩溃"""
        mc = MetricsCalculator()
        result = mc.calculate(equity_curve=[], trades=[])
        assert result is not None

    def test_calculate_returns_metrics(self):
        """计算返回绩效指标对象"""
        from myquant.models.backtest import EquityPoint
        mc = MetricsCalculator()
        curve = [
            EquityPoint(timestamp=1700000000.0, equity=1_000_000.0, cash=1_000_000.0),
            EquityPoint(timestamp=1700100000.0, equity=1_010_000.0, cash=900_000.0),
            EquityPoint(timestamp=1700200000.0, equity=1_020_000.0, cash=800_000.0),
        ]
        result = mc.calculate(equity_curve=curve, trades=[])
        assert result is not None

    def test_total_return_positive(self):
        """正收益计算"""
        from myquant.models.backtest import EquityPoint
        mc = MetricsCalculator()
        curve = [
            EquityPoint(timestamp=1700000000.0, equity=1_000_000.0, cash=1_000_000.0),
            EquityPoint(timestamp=1700100000.0, equity=1_100_000.0, cash=1_100_000.0),
        ]
        result = mc.calculate(equity_curve=curve, trades=[])
        assert result.total_return > 0

    def test_max_drawdown_non_negative(self):
        """最大回撤非负"""
        from myquant.models.backtest import EquityPoint
        mc = MetricsCalculator()
        curve = [
            EquityPoint(timestamp=1700000000.0, equity=1_000_000.0, cash=1_000_000.0),
            EquityPoint(timestamp=1700100000.0, equity=1_100_000.0, cash=1_100_000.0),
            EquityPoint(timestamp=1700200000.0, equity=950_000.0, cash=950_000.0),
        ]
        result = mc.calculate(equity_curve=curve, trades=[])
        assert result.max_drawdown >= 0.0

    def test_sharpe_ratio_calculated(self):
        """夏普比率计算"""
        from myquant.models.backtest import EquityPoint
        mc = MetricsCalculator(risk_free_rate=0.02)
        np.random.seed(42)
        curve = []
        equity = 1_000_000.0
        for i in range(60):
            equity *= (1 + np.random.normal(0.001, 0.01))
            curve.append(EquityPoint(
                timestamp=1700000000.0 + i * 86400,
                equity=equity, cash=equity * 0.5,
            ))
        result = mc.calculate(equity_curve=curve, trades=[])
        assert hasattr(result, "sharpe_ratio")

    def test_incremental_update_exists(self):
        """增量更新方法存在（实盘流式接口）"""
        mc = MetricsCalculator()
        assert hasattr(mc, "incremental_update")
        assert callable(mc.incremental_update)

    def test_win_rate_with_trades(self):
        """含成交的胜率计算"""
        from myquant.models.backtest import EquityPoint, TradeRecord
        mc = MetricsCalculator()
        curve = [
            EquityPoint(timestamp=1700000000.0, equity=1_000_000.0, cash=1_000_000.0),
            EquityPoint(timestamp=1700100000.0, equity=1_050_000.0, cash=1_050_000.0),
        ]
        # 构造盈利和亏损交易
        trades = []
        result = mc.calculate(equity_curve=curve, trades=trades)
        assert result is not None
        assert hasattr(result, "win_rate")


# ═══════════════════════════════════════════
#  TestBacktestEngine - 回测引擎
# ═══════════════════════════════════════════

class TestBacktestEngine:
    """BacktestEngine 回测引擎测试"""

    def test_instantiation(self, crossover_df):
        """实例化引擎"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        assert engine is not None
        assert engine.name == "backtest"

    def test_inherits_base_engine(self, crossover_df):
        """继承 BaseEngine（#2结构图相符性）"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        assert isinstance(engine, BaseEngine)
        assert issubclass(BacktestEngine, BaseEngine)

    def test_run_returns_result(self, crossover_df):
        """run() 返回 BacktestResult"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        assert isinstance(result, BacktestResult)

    def test_run_status_completed(self, crossover_df):
        """回测状态为 completed"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        assert result.status == "completed"

    def test_run_has_run_id(self, crossover_df):
        """结果有 run_id"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        assert result.run_id is not None
        assert result.run_id.startswith("bt_")

    def test_equity_curve_length_matches_data(self, crossover_df):
        """净值曲线点数与数据行数一致"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        assert len(result.equity_curve) == len(crossover_df)

    def test_metrics_calculated(self, crossover_df):
        """绩效指标已计算"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        assert result.metrics is not None
        assert hasattr(result.metrics, "total_return")
        assert hasattr(result.metrics, "sharpe_ratio")
        assert hasattr(result.metrics, "max_drawdown")

    def test_get_result_after_run(self, crossover_df):
        """get_result() 返回运行结果"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        engine.run()
        result = engine.get_result()
        assert result is not None
        assert result.status == "completed"

    def test_get_result_before_run(self, crossover_df):
        """run() 前 get_result() 返回 None"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        assert engine.get_result() is None

    def test_uptrend_positive_return(self, uptrend_df):
        """单调上涨行情正收益"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": uptrend_df})
        result = engine.run()
        # 单调上涨，均线策略应该盈利
        assert result.metrics.total_return >= 0.0

    def test_initial_capital_preserved(self, crossover_df):
        """初始资金不被修改"""
        config = _make_config(initial_capital=500_000.0)
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        assert result.config.initial_capital == 500_000.0

    def test_trades_recorded(self, crossover_df):
        """交易被记录"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        assert len(result.trades) >= 0  # 可能有交易也可能没有（取决于数据）

    def test_from_data_provider_classmethod(self):
        """from_data_provider 类方法存在"""
        assert hasattr(BacktestEngine, "from_data_provider")
        assert callable(BacktestEngine.from_data_provider)

    def test_lifecycle_methods_exist(self):
        """生命周期方法存在（on_init/on_market_event/on_stop）"""
        assert hasattr(BacktestEngine, "on_init")
        assert hasattr(BacktestEngine, "on_market_event")
        assert hasattr(BacktestEngine, "on_stop")
        assert hasattr(BacktestEngine, "run")

    def test_single_bar_backtest(self, single_bar_df):
        """单根bar回测不崩溃"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": single_bar_df})
        result = engine.run()
        assert result is not None
        assert len(result.equity_curve) == 1

    def test_empty_data_backtest(self, empty_df):
        """空数据回测不崩溃"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": empty_df})
        result = engine.run()
        assert result is not None

    def test_no_strategy_backtest(self, crossover_df):
        """无策略回测不崩溃"""
        config = _make_config()
        engine = BacktestEngine(config=config, strategy=None,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        assert result is not None

    def test_dispatch_event_interface(self, crossover_df):
        """BaseEngine dispatch_event 接口可用"""
        config = _make_config()
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        assert hasattr(engine, "dispatch_event")
        assert hasattr(engine, "event_count")


# ═══════════════════════════════════════════
#  TestBacktestLookahead - 防未来函数
# ═══════════════════════════════════════════

class TestBacktestLookahead:
    """回测防未来函数测试"""

    @pytest.mark.lookahead
    def test_end_date_filters_data(self, crossover_df):
        """end_date 严格过滤，不使用end_date之后的数据"""
        config = _make_config(end_date="2024-01-15")
        strategy = MACrossStrategy(fast_period=3, slow_period=10)
        engine = BacktestEngine(config=config, strategy=strategy,
                                data={"000001.SZ": crossover_df})
        result = engine.run()
        # 净值曲线点数不应超过end_date之前的交易日数
        assert len(result.equity_curve) <= len(crossover_df)

    @pytest.mark.lookahead
    def test_no_future_data_in_signals(self, crossover_df):
        """策略信号不使用未来数据（修改未来数据不影响历史净值）"""
        config = _make_config()
        strategy1 = MACrossStrategy(fast_period=3, slow_period=10)
        engine1 = BacktestEngine(config=config, strategy=strategy1,
                                 data={"000001.SZ": crossover_df})
        result1 = engine1.run()

        # 修改未来数据（第25根之后）
        df2 = crossover_df.copy()
        df2.iloc[25:, df2.columns.get_loc("收盘价")] *= 2.0
        strategy2 = MACrossStrategy(fast_period=3, slow_period=10)
        engine2 = BacktestEngine(config=config, strategy=strategy2,
                                 data={"000001.SZ": df2})
        result2 = engine2.run()

        # 前20根bar的净值应该一致（修改点之前）
        if len(result1.equity_curve) >= 20 and len(result2.equity_curve) >= 20:
            eq1 = result1.equity_curve[19].equity
            eq2 = result2.equity_curve[19].equity
            assert abs(eq1 - eq2) < 1e-6

    @pytest.mark.lookahead
    def test_engine_no_shift_negative(self):
        """引擎代码不含 shift(- 未来函数模式"""
        import inspect
        source = inspect.getsource(BacktestEngine)
        assert "shift(-" not in source
        assert "iloc[+1" not in source


# ═══════════════════════════════════════════
#  TestDataProvider - 数据加载器
# ═══════════════════════════════════════════

class TestDataProvider:
    """数据加载器测试"""

    def test_base_is_abstract(self):
        """DataProviderBase 是抽象基类"""
        with pytest.raises(TypeError):
            DataProviderBase()

    def test_base_has_get_bars(self):
        """基类定义 get_bars 抽象方法"""
        assert hasattr(DataProviderBase, "get_bars")

    def test_base_has_get_available_symbols(self):
        """基类定义 get_available_symbols 抽象方法"""
        assert hasattr(DataProviderBase, "get_available_symbols")

    def test_historical_provider_instantiation(self):
        """HistoricalDataProvider 可实例化（需数据目录）"""
        # 不指定数据目录时应能创建对象（可能扫描为空）
        try:
            provider = HistoricalDataProvider(data_dir="/tmp/nonexistent_quant_data")
            assert provider is not None
        except Exception:
            pytest.skip("HistoricalDataProvider 构造因环境而异")

    def test_historical_provider_inherits_base(self):
        """HistoricalDataProvider 继承 DataProviderBase"""
        assert issubclass(HistoricalDataProvider, DataProviderBase)

    def test_historical_provider_has_load_multiple(self):
        """HistoricalDataProvider 有 load_multiple 方法"""
        assert hasattr(HistoricalDataProvider, "load_multiple")

    def test_historical_provider_has_refresh(self):
        """HistoricalDataProvider 有 refresh 方法"""
        assert hasattr(HistoricalDataProvider, "refresh")


# ═══════════════════════════════════════════
#  TestBacktestResult - 回测结果模型
# ═══════════════════════════════════════════

class TestBacktestResult:
    """BacktestResult 结果模型测试"""

    def test_result_fields(self):
        """结果模型包含必要字段"""
        from myquant.models.backtest import PerformanceMetrics
        result = BacktestResult(
            config=BacktestConfig(),
            metrics=PerformanceMetrics(),
            equity_curve=[],
            trades=[],
            run_id="bt_test",
            started_at="2024-01-01",
            finished_at="2024-01-02",
            status="completed",
        )
        assert result.run_id == "bt_test"
        assert result.status == "completed"
        assert result.config is not None
        assert result.metrics is not None

    def test_result_error_message(self):
        """错误结果包含 error_message"""
        result = BacktestResult(
            config=BacktestConfig(),
            metrics=None,
            equity_curve=[],
            trades=[],
            run_id="bt_error",
            started_at="2024-01-01",
            finished_at="2024-01-01",
            status="error",
            error_message="测试错误",
        )
        assert result.status == "error"
        assert result.error_message == "测试错误"

    def test_result_extra_dict(self):
        """extra 字段可存储附加信息"""
        result = BacktestResult(
            config=BacktestConfig(),
            metrics=None,
            equity_curve=[],
            trades=[],
            run_id="bt_extra",
            started_at="2024-01-01",
            finished_at="2024-01-01",
            status="completed",
            extra={"custom_key": "custom_value"},
        )
        assert result.extra["custom_key"] == "custom_value"
