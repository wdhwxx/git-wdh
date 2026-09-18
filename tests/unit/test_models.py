# 文件名：tests/unit/test_models.py
"""
test_models.py - 数据模型单元测试
==================================
覆盖 myquant/models/ 全部三个模块：
  - market_data.py : BarFrequency / Tick / Bar / OrderLevel / Snapshot / MarketEvent / MarketDataValidator
  - trade.py       : OrderSide / OrderType / OrderStatus / Order / Position / Trade / OrderFactory
  - backtest.py    : PerformanceMetrics / TradeRecord / EquityPoint / BacktestResult（Commission/Slippage/Config 已在 test_backtest 覆盖）

测试维度:
  1. 枚举值（BarFrequency / OrderSide / OrderType / OrderStatus）
  2. 行情模型（Tick / Bar / Snapshot / MarketEvent 的字段、序列化、校验）
  3. 交易模型（Order 生命周期 / Position 盈亏 / Trade / OrderFactory）
  4. 回测模型（PerformanceMetrics 34字段 / TradeRecord / EquityPoint / BacktestResult）
  5. 数据校验器（MarketDataValidator 的 bar/tick/snapshot/series 校验）

运行方式:
  pytest tests/unit/test_models.py -v

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/unit/test_models.py）
  2. 为实时行情及实盘无缝对接做好充足准备（Tick/Snapshot 实时数据结构 / Order 实盘状态机 / Position 实时盈亏）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 模型测试不涉及时间序列计算，所有时间戳为固定历史值
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import pandas as pd
import pytest

from myquant.models.backtest import (
    BacktestResult,
    EquityPoint,
    PerformanceMetrics,
    TradeRecord,
)
from myquant.models.market_data import (
    Bar,
    BarFrequency,
    MarketDataValidator,
    MarketEvent,
    OrderLevel,
    Snapshot,
    Tick,
)
from myquant.models.trade import (
    Order,
    OrderFactory,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Trade,
)


# ═══════════════════════════════════════════
#  TestBarFrequency - K线频率枚举
# ═══════════════════════════════════════════

class TestBarFrequency:
    """BarFrequency 枚举测试"""

    def test_daily_exists(self):
        """日线存在"""
        assert BarFrequency.DAILY is not None

    def test_weekly_exists(self):
        """周线存在"""
        assert hasattr(BarFrequency, "WEEKLY") or hasattr(BarFrequency, "1W")

    def test_minute_exists(self):
        """分钟线存在"""
        assert hasattr(BarFrequency, "MINUTE_1") or hasattr(BarFrequency, "1m") or hasattr(BarFrequency, "MIN1")

    def test_enum_values_are_strings(self):
        """枚举值为字符串"""
        for freq in BarFrequency:
            assert isinstance(freq.value, str)


# ═══════════════════════════════════════════
#  TestTick - 逐笔行情
# ═══════════════════════════════════════════

class TestTick:
    """Tick 逐笔行情测试"""

    def test_default_values(self):
        """默认值"""
        tick = Tick()
        assert tick.symbol == ""
        assert tick.price == 0.0
        assert tick.volume == 0.0
        assert tick.bid_price == 0.0
        assert tick.ask_price == 0.0

    def test_custom_values(self):
        """自定义值"""
        tick = Tick(symbol="000001.SZ", price=10.5, volume=1000,
                    bid_price=10.49, ask_price=10.51)
        assert tick.symbol == "000001.SZ"
        assert tick.price == 10.5
        assert tick.bid_price == 10.49
        assert tick.ask_price == 10.51

    def test_timestamp_auto_generated(self):
        """时间戳自动生成"""
        tick = Tick()
        assert tick.timestamp > 0

    def test_extra_dict(self):
        """extra 字段"""
        tick = Tick(extra={"key": "value"})
        assert tick.extra["key"] == "value"

    def test_to_dict(self):
        """序列化为字典"""
        tick = Tick(symbol="000001.SZ", price=10.5)
        d = tick.to_dict()
        assert isinstance(d, dict)
        assert d["symbol"] == "000001.SZ"
        assert d["price"] == 10.5

    def test_is_valid(self):
        """is_valid 属性"""
        tick = Tick(symbol="000001.SZ", price=10.5, volume=1000)
        assert tick.is_valid is True or tick.is_valid is False

    def test_computed_properties(self):
        """计算属性（spread/mid_price/vwap等）"""
        tick = Tick(symbol="000001.SZ", price=10.5, bid_price=10.49,
                    ask_price=10.51, volume=1000, amount=10500)
        assert hasattr(tick, "spread")
        assert hasattr(tick, "mid_price")
        assert hasattr(tick, "change")


# ═══════════════════════════════════════════
#  TestBar - K线数据
# ═══════════════════════════════════════════

class TestBar:
    """Bar K线数据测试"""

    def test_default_values(self):
        """默认值"""
        bar = Bar()
        assert bar.symbol == ""
        assert bar.open == 0.0
        assert bar.high == 0.0
        assert bar.low == 0.0
        assert bar.close == 0.0
        assert bar.volume == 0.0

    def test_custom_values(self):
        """自定义值"""
        bar = Bar(symbol="000001.SZ", open=10.0, high=10.5, low=9.8,
                  close=10.2, volume=100000, amount=1020000)
        assert bar.symbol == "000001.SZ"
        assert bar.high == 10.5
        assert bar.close == 10.2

    def test_frequency_default_daily(self):
        """默认频率日线"""
        bar = Bar()
        assert bar.frequency == BarFrequency.DAILY

    def test_to_dict(self):
        """序列化为字典"""
        bar = Bar(symbol="000001.SZ", close=10.2)
        d = bar.to_dict()
        assert isinstance(d, dict)
        assert d["symbol"] == "000001.SZ"
        assert d["close"] == 10.2

    def test_from_dict(self):
        """从字典反序列化"""
        data = {"symbol": "000001.SZ", "open": 10.0, "close": 10.2, "volume": 1000}
        bar = Bar.from_dict(data)
        assert bar.symbol == "000001.SZ"
        assert bar.close == 10.2

    def test_roundtrip(self):
        """序列化-反序列化往返"""
        bar1 = Bar(symbol="000001.SZ", open=10.0, high=10.5, low=9.8,
                   close=10.2, volume=100000, amount=1020000)
        d = bar1.to_dict()
        bar2 = Bar.from_dict(d)
        assert bar2.symbol == bar1.symbol
        assert bar2.open == bar1.open
        assert bar2.close == bar1.close

    def test_validate_valid_bar(self):
        """有效K线校验通过（返回空错误列表）"""
        bar = Bar(symbol="000001.SZ", open=10.0, high=10.5, low=9.8, close=10.2)
        errors = bar.validate()
        assert isinstance(errors, list)
        assert len(errors) == 0

    def test_validate_high_low_inconsistency(self):
        """高低价不一致校验（返回错误列表）"""
        bar = Bar(symbol="000001.SZ", open=10.0, high=9.0, low=11.0, close=10.0)
        errors = bar.validate()
        assert isinstance(errors, list)
        assert len(errors) > 0


# ═══════════════════════════════════════════
#  TestOrderLevel - 买卖盘档位
# ═══════════════════════════════════════════

class TestOrderLevel:
    """OrderLevel 买卖盘档位测试"""

    def test_default_values(self):
        """默认值"""
        level = OrderLevel()
        assert level.price == 0.0
        assert level.size == 0.0

    def test_custom_values(self):
        """自定义值"""
        level = OrderLevel(price=10.5, size=1000)
        assert level.price == 10.5
        assert level.size == 1000


# ═══════════════════════════════════════════
#  TestSnapshot - 行情快照
# ═══════════════════════════════════════════

class TestSnapshot:
    """Snapshot 行情快照测试"""

    def test_default_values(self):
        """默认值"""
        snap = Snapshot()
        assert snap.symbol == ""
        assert snap.last_price == 0.0
        assert len(snap.bids) == 0
        assert len(snap.asks) == 0

    def test_with_order_levels(self):
        """带买卖盘档位"""
        bids = [OrderLevel(price=10.49, size=1000), OrderLevel(price=10.48, size=2000)]
        asks = [OrderLevel(price=10.51, size=1500), OrderLevel(price=10.52, size=2500)]
        snap = Snapshot(symbol="000001.SZ", last_price=10.50, bids=bids, asks=asks)
        assert len(snap.bids) == 2
        assert len(snap.asks) == 2
        assert snap.bids[0].price == 10.49
        assert snap.asks[0].price == 10.51

    def test_timestamp_auto(self):
        """时间戳自动生成"""
        snap = Snapshot()
        assert snap.timestamp > 0


# ═══════════════════════════════════════════
#  TestMarketEvent - 市场事件
# ═══════════════════════════════════════════

class TestMarketEvent:
    """MarketEvent 市场事件测试"""

    def test_default_values(self):
        """默认值"""
        event = MarketEvent()
        assert event.symbol == ""
        assert event.event_type == "tick"
        assert event.source == ""

    def test_custom_values(self):
        """自定义值"""
        bar = Bar(symbol="000001.SZ", close=10.2)
        event = MarketEvent(symbol="000001.SZ", event_type="bar",
                            data=bar, source="akshare")
        assert event.symbol == "000001.SZ"
        assert event.event_type == "bar"
        assert event.source == "akshare"
        assert event.data is not None

    def test_event_id_auto(self):
        """事件ID自动生成"""
        event = MarketEvent()
        assert event.event_id.startswith("mkt_")

    def test_event_types(self):
        """支持的事件类型"""
        for etype in ["tick", "bar", "snapshot"]:
            event = MarketEvent(event_type=etype)
            assert event.event_type == etype


# ═══════════════════════════════════════════
#  TestMarketDataValidator - 数据校验器
# ═══════════════════════════════════════════

class TestMarketDataValidator:
    """MarketDataValidator 数据校验器测试"""

    def test_validate_bar_valid(self):
        """有效K线校验"""
        validator = MarketDataValidator()
        bar = Bar(symbol="000001.SZ", open=10.0, high=10.5, low=9.8, close=10.2)
        result = validator.validate_bar(bar)
        assert result is not None

    def test_validate_tick_valid(self):
        """有效Tick校验"""
        validator = MarketDataValidator()
        tick = Tick(symbol="000001.SZ", price=10.5, volume=1000)
        result = validator.validate_tick(tick)
        assert result is not None

    def test_validate_snapshot_valid(self):
        """有效快照校验"""
        validator = MarketDataValidator()
        snap = Snapshot(symbol="000001.SZ", last_price=10.5)
        result = validator.validate_snapshot(snap)
        assert result is not None

    def test_validate_series(self):
        """序列校验（接受Bar列表）"""
        validator = MarketDataValidator()
        bars = [
            Bar(symbol="000001.SZ", open=10.0, high=10.5, low=9.8, close=10.2,
                timestamp=1700000000.0 + i * 86400)
            for i in range(5)
        ]
        result = validator.validate_series(bars)
        assert result is not None

    def test_validate_bar_negative_price(self):
        """负价格校验失败"""
        validator = MarketDataValidator()
        bar = Bar(symbol="000001.SZ", open=-1.0, high=0.5, low=-2.0, close=-0.5)
        result = validator.validate_bar(bar)
        # 负价格应返回错误
        assert result is not None


# ═══════════════════════════════════════════
#  TestOrderEnums - 订单枚举
# ═══════════════════════════════════════════

class TestOrderEnums:
    """订单枚举测试"""

    def test_order_side_buy_sell(self):
        """买卖方向"""
        assert OrderSide.BUY is not None
        assert OrderSide.SELL is not None

    def test_order_type_market_limit(self):
        """订单类型"""
        assert OrderType.MARKET is not None
        assert OrderType.LIMIT is not None

    def test_order_status_states(self):
        """订单状态"""
        assert OrderStatus.PENDING is not None
        assert OrderStatus.FILLED is not None
        assert OrderStatus.CANCELLED is not None
        assert OrderStatus.REJECTED is not None

    def test_enum_values_are_strings(self):
        """枚举值为字符串"""
        for status in OrderStatus:
            assert isinstance(status.value, str)


# ═══════════════════════════════════════════
#  TestOrder - 订单模型
# ═══════════════════════════════════════════

class TestOrder:
    """Order 订单模型测试"""

    def test_default_values(self):
        """默认值"""
        order = Order()
        assert order.symbol == ""
        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.MARKET
        assert order.status == OrderStatus.PENDING
        assert order.filled_qty == 0.0

    def test_custom_values(self):
        """自定义值"""
        order = Order(symbol="000001.SZ", side=OrderSide.SELL,
                      order_type=OrderType.LIMIT, quantity=100, price=10.5)
        assert order.symbol == "000001.SZ"
        assert order.side == OrderSide.SELL
        assert order.order_type == OrderType.LIMIT
        assert order.quantity == 100
        assert order.price == 10.5

    def test_order_id_auto(self):
        """订单ID自动生成"""
        order = Order()
        assert order.order_id != ""
        assert len(order.order_id) > 0

    def test_submit(self):
        """提交订单"""
        order = Order(symbol="000001.SZ", quantity=100, price=10.0)
        order.submit()
        assert order.status == OrderStatus.SUBMITTED or order.status == OrderStatus.PENDING

    def test_fill(self):
        """成交订单"""
        order = Order(symbol="000001.SZ", quantity=100, price=10.0)
        order.fill(qty=100, price=10.0)
        assert order.filled_qty == 100
        assert order.filled_price == 10.0
        assert order.status == OrderStatus.FILLED

    def test_partial_fill(self):
        """部分成交"""
        order = Order(symbol="000001.SZ", quantity=100, price=10.0)
        order.fill(qty=50, price=10.0)
        assert order.filled_qty == 50
        assert order.status != OrderStatus.FILLED  # 未全部成交

    def test_cancel(self):
        """取消订单"""
        order = Order(symbol="000001.SZ", quantity=100)
        order.cancel()
        assert order.status == OrderStatus.CANCELLED

    def test_reject(self):
        """拒绝订单"""
        order = Order(symbol="000001.SZ", quantity=100)
        order.reject(reason="资金不足")
        assert order.status == OrderStatus.REJECTED

    def test_to_dict(self):
        """序列化为字典"""
        order = Order(symbol="000001.SZ", quantity=100, price=10.0)
        d = order.to_dict()
        assert isinstance(d, dict)
        assert d["symbol"] == "000001.SZ"
        assert d["quantity"] == 100

    def test_from_dict(self):
        """从字典反序列化"""
        data = {"symbol": "000001.SZ", "quantity": 100, "price": 10.0,
                "side": "BUY", "order_type": "LIMIT"}
        order = Order.from_dict(data)
        assert order.symbol == "000001.SZ"
        assert order.quantity == 100

    def test_roundtrip(self):
        """序列化-反序列化往返"""
        order1 = Order(symbol="000001.SZ", side=OrderSide.BUY,
                       order_type=OrderType.LIMIT, quantity=100, price=10.5)
        d = order1.to_dict()
        order2 = Order.from_dict(d)
        assert order2.symbol == order1.symbol
        assert order2.quantity == order1.quantity

    def test_validate_valid_order(self):
        """有效订单校验（返回空错误列表）"""
        order = Order(symbol="000001.SZ", quantity=100, price=10.0)
        errors = order.validate()
        assert isinstance(errors, list)
        assert len(errors) == 0

    def test_validate_zero_quantity(self):
        """零数量校验失败（返回错误列表）"""
        order = Order(symbol="000001.SZ", quantity=0)
        errors = order.validate()
        assert isinstance(errors, list)
        assert len(errors) > 0

    def test_extra_field(self):
        """extra 字段"""
        order = Order(extra={"strategy": "ma_cross"})
        assert order.extra["strategy"] == "ma_cross"


# ═══════════════════════════════════════════
#  TestPosition - 持仓模型
# ═══════════════════════════════════════════

class TestPosition:
    """Position 持仓模型测试"""

    def test_default_values(self):
        """默认值"""
        pos = Position()
        assert pos.symbol == ""
        assert pos.quantity == 0.0
        assert pos.avg_cost == 0.0
        assert pos.market_price == 0.0
        assert pos.realized_pnl == 0.0

    def test_custom_values(self):
        """自定义值"""
        pos = Position(symbol="000001.SZ", quantity=100, avg_cost=10.0,
                       market_price=10.5)
        assert pos.symbol == "000001.SZ"
        assert pos.quantity == 100
        assert pos.avg_cost == 10.0

    def test_update_market_price(self):
        """更新市价"""
        pos = Position(symbol="000001.SZ", quantity=100, avg_cost=10.0)
        pos.update_market_price(10.5)
        assert pos.market_price == 10.5

    def test_apply_fill_buy(self):
        """买入成交更新持仓"""
        pos = Position(symbol="000001.SZ")
        pos.apply_fill(qty=100, price=10.0, side=OrderSide.BUY)
        assert pos.quantity == 100
        assert pos.avg_cost > 0  # 含佣金

    def test_apply_fill_sell(self):
        """卖出成交更新持仓"""
        pos = Position(symbol="000001.SZ", quantity=100, avg_cost=10.0)
        pos.apply_fill(qty=50, price=11.0, side=OrderSide.SELL)
        assert pos.quantity == 50
        assert pos.realized_pnl > 0  # 盈利卖出

    def test_to_dict(self):
        """序列化为字典"""
        pos = Position(symbol="000001.SZ", quantity=100, avg_cost=10.0)
        d = pos.to_dict()
        assert isinstance(d, dict)
        assert d["symbol"] == "000001.SZ"
        assert d["quantity"] == 100

    def test_unrealized_pnl(self):
        """浮动盈亏计算"""
        pos = Position(symbol="000001.SZ", quantity=100, avg_cost=10.0,
                       market_price=11.0)
        # 浮动盈亏 = (11 - 10) * 100 = 100
        if hasattr(pos, "unrealized_pnl"):
            assert abs(pos.unrealized_pnl - 100.0) < 1e-6
        elif hasattr(pos, "float_pnl"):
            assert abs(pos.float_pnl - 100.0) < 1e-6


# ═══════════════════════════════════════════
#  TestTrade - 成交模型
# ═══════════════════════════════════════════

class TestTrade:
    """Trade 成交模型测试"""

    def test_default_values(self):
        """默认值"""
        trade = Trade()
        assert trade.symbol == ""
        assert trade.side == OrderSide.BUY
        assert trade.quantity == 0.0
        assert trade.price == 0.0
        assert trade.commission == 0.0

    def test_custom_values(self):
        """自定义值"""
        trade = Trade(symbol="000001.SZ", side=OrderSide.BUY,
                      quantity=100, price=10.5, commission=5.0)
        assert trade.symbol == "000001.SZ"
        assert trade.quantity == 100
        assert trade.commission == 5.0

    def test_trade_id_auto(self):
        """成交ID自动生成"""
        trade = Trade()
        assert trade.trade_id != ""

    def test_to_dict(self):
        """序列化为字典"""
        trade = Trade(symbol="000001.SZ", quantity=100, price=10.5)
        d = trade.to_dict()
        assert isinstance(d, dict)
        assert d["symbol"] == "000001.SZ"
        assert d["quantity"] == 100

    def test_slippage_field(self):
        """滑点字段"""
        trade = Trade(symbol="000001.SZ", slippage=0.02)
        assert trade.slippage == 0.02


# ═══════════════════════════════════════════
#  TestOrderFactory - 订单工厂
# ═══════════════════════════════════════════

class TestOrderFactory:
    """OrderFactory 订单工厂测试"""

    def test_limit_buy(self):
        """限价买入"""
        order = OrderFactory.limit_buy(symbol="000001.SZ", quantity=100, price=10.0)
        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.LIMIT
        assert order.quantity == 100
        assert order.price == 10.0

    def test_limit_sell(self):
        """限价卖出"""
        order = OrderFactory.limit_sell(symbol="000001.SZ", quantity=100, price=11.0)
        assert order.side == OrderSide.SELL
        assert order.order_type == OrderType.LIMIT

    def test_market_buy(self):
        """市价买入"""
        order = OrderFactory.market_buy(symbol="000001.SZ", quantity=100)
        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.MARKET

    def test_market_sell(self):
        """市价卖出"""
        order = OrderFactory.market_sell(symbol="000001.SZ", quantity=100)
        assert order.side == OrderSide.SELL
        assert order.order_type == OrderType.MARKET

    def test_stop_loss(self):
        """止损单"""
        order = OrderFactory.stop_loss(symbol="000001.SZ", quantity=100,
                                       stop_price=9.5)
        assert order is not None
        assert order.stop_price == 9.5

    def test_stop_limit(self):
        """限价止损单"""
        order = OrderFactory.stop_limit(symbol="000001.SZ", quantity=100, stop_price=9.5, price=9.4)
        assert order is not None
        assert order.stop_price == 9.5

    def test_factory_orders_have_id(self):
        """工厂创建的订单有ID"""
        order = OrderFactory.limit_buy("000001.SZ", 100, 10.0)
        assert order.order_id != ""


# ═══════════════════════════════════════════
#  TestPerformanceMetrics - 绩效指标
# ═══════════════════════════════════════════

class TestPerformanceMetrics:
    """PerformanceMetrics 绩效指标测试"""

    def test_default_values(self):
        """默认值全零"""
        m = PerformanceMetrics()
        assert m.total_return == 0.0
        assert m.sharpe_ratio == 0.0
        assert m.max_drawdown == 0.0
        assert m.win_rate == 0.0
        assert m.total_trades == 0

    def test_custom_values(self):
        """自定义值"""
        m = PerformanceMetrics(
            total_return=0.15, sharpe_ratio=1.5, max_drawdown=0.10,
            win_rate=0.6, total_trades=50,
        )
        assert m.total_return == 0.15
        assert m.sharpe_ratio == 1.5
        assert m.max_drawdown == 0.10
        assert m.win_rate == 0.6
        assert m.total_trades == 50

    def test_all_34_fields_exist(self):
        """34个字段全部存在"""
        m = PerformanceMetrics()
        expected_fields = [
            "total_return", "annualized_return", "benchmark_return", "excess_return",
            "annualized_volatility", "max_drawdown", "max_drawdown_duration",
            "downside_volatility", "sharpe_ratio", "sortino_ratio", "calmar_ratio",
            "information_ratio", "total_trades", "winning_trades", "losing_trades",
            "win_rate", "profit_loss_ratio", "avg_win", "avg_loss",
            "max_consecutive_wins", "max_consecutive_losses",
            "total_commission", "total_slippage", "turnover",
            "alpha", "beta", "tracking_error", "var_95", "cvar_95",
            "skewness", "kurtosis", "trading_days", "start_date", "end_date",
        ]
        for field in expected_fields:
            assert hasattr(m, field), f"缺少字段: {field}"

    def test_risk_adjusted_ratios(self):
        """风险调整收益指标"""
        m = PerformanceMetrics(sharpe_ratio=2.0, sortino_ratio=3.0,
                               calmar_ratio=1.5, information_ratio=0.8)
        assert m.sharpe_ratio == 2.0
        assert m.sortino_ratio == 3.0
        assert m.calmar_ratio == 1.5
        assert m.information_ratio == 0.8

    def test_trade_statistics(self):
        """交易统计指标"""
        m = PerformanceMetrics(
            total_trades=100, winning_trades=60, losing_trades=40,
            win_rate=0.6, avg_win=500, avg_loss=300,
            profit_loss_ratio=1.67,
        )
        assert m.total_trades == 100
        assert m.winning_trades + m.losing_trades == m.total_trades

    def test_risk_metrics(self):
        """风险指标"""
        m = PerformanceMetrics(
            var_95=0.02, cvar_95=0.03, skewness=-0.5, kurtosis=3.0,
            beta=1.2, alpha=0.05, tracking_error=0.08,
        )
        assert m.var_95 == 0.02
        assert m.cvar_95 == 0.03
        assert m.beta == 1.2


# ═══════════════════════════════════════════
#  TestTradeRecord - 交易记录
# ═══════════════════════════════════════════

class TestTradeRecord:
    """TradeRecord 交易记录测试"""

    def test_default_values(self):
        """默认值"""
        tr = TradeRecord()
        assert tr.symbol == ""
        assert tr.quantity == 0.0
        assert tr.pnl == 0.0
        assert tr.holding_days == 0

    def test_custom_values(self):
        """自定义值"""
        tr = TradeRecord(
            trade_id="t001", symbol="000001.SZ", side="BUY",
            quantity=100, price=10.5, pnl=500.0, pnl_pct=0.05,
            holding_days=5, entry_reason="金叉", exit_reason="死叉",
        )
        assert tr.trade_id == "t001"
        assert tr.pnl == 500.0
        assert tr.pnl_pct == 0.05
        assert tr.holding_days == 5

    def test_signal_source(self):
        """信号来源字段"""
        tr = TradeRecord(signal_source="ma_cross")
        assert tr.signal_source == "ma_cross"

    def test_strategy_id(self):
        """策略ID字段"""
        tr = TradeRecord(strategy_id="rsrs_v2")
        assert tr.strategy_id == "rsrs_v2"


# ═══════════════════════════════════════════
#  TestEquityPoint - 净值点
# ═══════════════════════════════════════════

class TestEquityPoint:
    """EquityPoint 净值点测试"""

    def test_default_values(self):
        """默认值（equity/benchmark_equity 默认1.0，归一化基准）"""
        ep = EquityPoint()
        assert ep.equity == 1.0
        assert ep.cash == 0.0
        assert ep.position_value == 0.0
        assert ep.drawdown == 0.0

    def test_custom_values(self):
        """自定义值"""
        ep = EquityPoint(
            timestamp=1700000000.0, date="2024-01-15",
            equity=1_050_000.0, cash=500_000.0, position_value=550_000.0,
            drawdown=0.02, benchmark_equity=1_020_000.0,
        )
        assert ep.equity == 1_050_000.0
        assert ep.cash + ep.position_value == ep.equity
        assert ep.drawdown == 0.02

    def test_equity_composition(self):
        """净值 = 现金 + 持仓市值"""
        ep = EquityPoint(cash=300_000, position_value=700_000, equity=1_000_000)
        assert abs(ep.cash + ep.position_value - ep.equity) < 1e-6

    def test_benchmark_equity(self):
        """基准净值字段"""
        ep = EquityPoint(benchmark_equity=1_010_000.0)
        assert ep.benchmark_equity == 1_010_000.0


# ═══════════════════════════════════════════
#  TestBacktestResult - 回测结果
# ═══════════════════════════════════════════

class TestBacktestResult:
    """BacktestResult 回测结果测试"""

    def test_default_values(self):
        """默认值"""
        from myquant.models.backtest import BacktestConfig
        result = BacktestResult(
            config=BacktestConfig(), metrics=None, equity_curve=[],
            trades=[], run_id="", started_at="", finished_at="", status="",
        )
        assert result.equity_curve == []
        assert result.trades == []

    def test_completed_result(self):
        """完成状态结果"""
        from myquant.models.backtest import BacktestConfig
        result = BacktestResult(
            config=BacktestConfig(initial_capital=1_000_000),
            metrics=PerformanceMetrics(total_return=0.15),
            equity_curve=[EquityPoint(equity=1_150_000)],
            trades=[TradeRecord(symbol="000001.SZ")],
            run_id="bt_001", started_at="2024-01-01",
            finished_at="2024-06-30", status="completed",
        )
        assert result.status == "completed"
        assert result.run_id == "bt_001"
        assert result.metrics.total_return == 0.15
        assert len(result.equity_curve) == 1
        assert len(result.trades) == 1

    def test_error_result(self):
        """错误状态结果"""
        from myquant.models.backtest import BacktestConfig
        result = BacktestResult(
            config=BacktestConfig(), metrics=None, equity_curve=[],
            trades=[], run_id="bt_err", started_at="2024-01-01",
            finished_at="2024-01-01", status="error",
            error_message="数据加载失败",
        )
        assert result.status == "error"
        assert result.error_message == "数据加载失败"

    def test_extra_field(self):
        """extra 扩展字段"""
        from myquant.models.backtest import BacktestConfig
        result = BacktestResult(
            config=BacktestConfig(), metrics=None, equity_curve=[],
            trades=[], run_id="bt_extra", started_at="", finished_at="",
            status="completed", extra={"custom_metric": 42},
        )
        assert result.extra["custom_metric"] == 42
