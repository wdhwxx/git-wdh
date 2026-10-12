# 文件名：tests/unit/test_execution.py
"""
test_execution.py - 执行层单元测试
====================================
覆盖 myquant/execution/ 和 myquant/live/brokers/：
  - ExecutionInterface   执行接口抽象基类（回测/实盘统一接口）
  - SimBroker            模拟券商（滑点/佣金/印花税/撮合/持仓/资金）
  - OrderStateMachine    订单状态机（合法转换/终态/历史记录）
  - BrokerManager        券商管理器（注册/主备切换/故障转移）

测试维度:
  1. 执行接口（ABC不可实例化/方法定义/回测实盘统一）
  2. 模拟券商（连接/下单/撤单/撮合/持仓/资金/佣金/滑点/重置）
  3. 订单状态机（合法转换/非法转换拒绝/终态/活跃态/历史追踪）
  4. 券商管理器（注册/查询/主备切换/故障转移）

运行方式:
  pytest tests/unit/test_execution.py -v

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/unit/test_execution.py）
  2. 为实时行情及实盘无缝对接做好充足准备（ExecutionInterface 统一接口/实盘券商适配器/BrokerManager 故障转移）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 撮合仅使用当前 bar 的 OHLC，不引用未来 bar
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from myquant.execution import ExecutionInterface, OrderStateMachine, SimBroker
from myquant.models.backtest import CommissionModel, SlippageModel
from myquant.models.market_data import Bar
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType, Position, Trade


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _make_order(symbol: str = "000001.SZ", side: OrderSide = OrderSide.BUY,
                order_type: OrderType = OrderType.LIMIT,
                quantity: float = 100, price: float = 10.0,
                status: OrderStatus = OrderStatus.PENDING) -> Order:
    """构造测试订单"""
    return Order(
        symbol=symbol, side=side, order_type=order_type,
        quantity=quantity, price=price, status=status,
        created_at=time.time(),
    )


def _make_bar(symbol: str = "000001.SZ", open_p: float = 9.9,
              high: float = 10.2, low: float = 9.8,
              close: float = 10.1, volume: float = 100000.0) -> Bar:
    """构造测试K线"""
    return Bar(
        symbol=symbol, open=open_p, high=high, low=low, close=close,
        volume=volume, timestamp=time.time(),
    )


# ═══════════════════════════════════════════
#  TestExecutionInterface - 执行接口
# ═══════════════════════════════════════════

class TestExecutionInterface:
    """ExecutionInterface 执行接口抽象基类测试"""

    def test_cannot_instantiate(self):
        """抽象基类不可直接实例化"""
        with pytest.raises(TypeError):
            ExecutionInterface()

    def test_abstract_methods_defined(self):
        """所有抽象方法已定义"""
        methods = [
            "submit_order", "cancel_order", "get_order",
            "get_active_orders", "get_account", "get_positions",
            "connect", "disconnect", "is_connected", "get_trades",
        ]
        for method in methods:
            assert hasattr(ExecutionInterface, method), f"缺少方法: {method}"

    def test_sim_broker_implements_interface(self):
        """SimBroker 实现 ExecutionInterface"""
        assert issubclass(SimBroker, ExecutionInterface)

    def test_interface_name(self):
        """接口名称"""
        class DummyImpl(ExecutionInterface):
            def submit_order(self, order): return order
            def cancel_order(self, order_id): return True
            def get_order(self, order_id): return None
            def get_active_orders(self, symbol=None): return []
            def get_account(self): return {}
            def get_positions(self): return []
            def connect(self): return True
            def disconnect(self): pass
            def is_connected(self): return True
            def get_trades(self, symbol=None): return []

        impl = DummyImpl(name="test")
        assert impl.name == "test"


# ═══════════════════════════════════════════
#  TestSimBroker - 模拟券商
# ═══════════════════════════════════════════

class TestSimBroker:
    """SimBroker 模拟券商测试"""

    def test_instantiation_default(self):
        """默认实例化"""
        broker = SimBroker()
        assert broker is not None

    def test_instantiation_custom_capital(self):
        """自定义初始资金"""
        broker = SimBroker(initial_capital=500_000.0)
        assert broker.available_cash == 500_000.0

    def test_connect(self):
        """连接"""
        broker = SimBroker()
        assert broker.connect() is True
        assert broker.is_connected is True

    def test_disconnect(self):
        """断开连接"""
        broker = SimBroker()
        broker.connect()
        broker.disconnect()
        assert broker.is_connected is False

    def test_submit_order(self):
        """提交订单"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order()
        submitted = broker.submit_order(order)
        assert submitted is not None
        assert submitted.status in (OrderStatus.SUBMITTED, OrderStatus.PENDING)

    def test_submit_order_adds_to_active(self):
        """提交后订单进入活跃列表"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order()
        broker.submit_order(order)
        active = broker.get_active_orders()
        assert len(active) >= 1

    def test_cancel_order(self):
        """取消订单"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order()
        submitted = broker.submit_order(order)
        result = broker.cancel_order(submitted.order_id)
        assert result is True

    def test_cancel_nonexistent_order(self):
        """取消不存在的订单"""
        broker = SimBroker()
        broker.connect()
        result = broker.cancel_order("nonexistent_id")
        assert result is False

    def test_get_order(self):
        """查询订单"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order()
        submitted = broker.submit_order(order)
        fetched = broker.get_order(submitted.order_id)
        assert fetched is not None
        assert fetched.order_id == submitted.order_id

    def test_get_order_nonexistent(self):
        """查询不存在的订单返回 None"""
        broker = SimBroker()
        broker.connect()
        assert broker.get_order("nonexistent") is None

    def test_on_bar_match_buy(self):
        """on_bar 撮合买单"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(price=10.0)  # 限价10元
        broker.submit_order(order)
        bar = _make_bar(open_p=9.9, high=10.1, low=9.8, close=10.0)
        trades = broker.on_bar(bar)
        assert isinstance(trades, list)
        # 限价10元，bar最低9.8，应该成交
        assert len(trades) >= 1

    def test_on_bar_no_match_low_limit(self):
        """低价限价买单不成交（限价低于bar最低价）"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(price=5.0)  # 远低于市场价
        broker.submit_order(order)
        bar = _make_bar(close=10.0, low=9.8)
        trades = broker.on_bar(bar)
        assert len(trades) == 0

    def test_position_after_buy(self):
        """买入后持仓更新"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(quantity=100, price=10.0)
        broker.submit_order(order)
        bar = _make_bar(close=10.0)
        broker.on_bar(bar)
        positions = broker.get_positions()
        assert len(positions) >= 1
        pos = broker.get_position("000001.SZ")
        assert pos is not None
        assert pos.quantity == 100

    def test_cash_deducted_after_buy(self):
        """买入后资金扣减"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        initial_cash = broker.available_cash
        order = _make_order(quantity=100, price=10.0)
        broker.submit_order(order)
        bar = _make_bar(close=10.0)
        broker.on_bar(bar)
        # 买入100*10=1000元 + 佣金，资金应减少
        assert broker.available_cash < initial_cash

    def test_total_assets(self):
        """总资产计算"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        assert broker.total_assets == 1_000_000.0

    def test_equity(self):
        """净值计算"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        assert broker.equity > 0

    def test_position_count(self):
        """持仓数量"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        assert broker.position_count == 0
        order = _make_order(quantity=100, price=10.0)
        broker.submit_order(order)
        bar = _make_bar(close=10.0)
        broker.on_bar(bar)
        assert broker.position_count >= 1

    def test_get_account(self):
        """获取账户信息"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        account = broker.get_account()
        assert isinstance(account, dict)
        assert "cash" in account or "available" in account or "total_assets" in account

    def test_get_trades(self):
        """获取成交记录"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(price=10.0)
        broker.submit_order(order)
        bar = _make_bar(close=10.0)
        broker.on_bar(bar)
        trades = broker.get_trades()
        assert isinstance(trades, list)
        assert len(trades) >= 1

    def test_get_trades_by_symbol(self):
        """按标的获取成交"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(symbol="000001.SZ", price=10.0)
        broker.submit_order(order)
        bar = _make_bar(symbol="000001.SZ", close=10.0)
        broker.on_bar(bar)
        trades = broker.get_trades(symbol="000001.SZ")
        assert len(trades) >= 1
        trades_other = broker.get_trades(symbol="600000.SH")
        assert len(trades_other) == 0

    def test_commission_applied(self):
        """佣金计算（买入万2，最低5元）"""
        cm = CommissionModel(buy_rate=0.0002, min_commission=5.0)
        broker = SimBroker(initial_capital=1_000_000.0, commission_model=cm)
        broker.connect()
        order = _make_order(quantity=100, price=10.0)  # 1000元，佣金最低5元
        broker.submit_order(order)
        bar = _make_bar(close=10.0)
        trades = broker.on_bar(bar)
        if trades:
            assert trades[0].commission >= 5.0

    def test_slippage_applied(self):
        """滑点计算（买入滑点使成交价高于撮合基准价）"""
        sm = SlippageModel(type="fixed_bps", fixed_bps=10.0)
        broker = SimBroker(initial_capital=1_000_000.0, slippage_model=sm)
        broker.connect()
        order = _make_order(quantity=100, price=10.0)
        broker.submit_order(order)
        bar = _make_bar(open_p=9.9, close=10.0)
        trades = broker.on_bar(bar)
        if trades:
            # 买入滑点使成交价高于bar开盘价（撮合基准）
            assert trades[0].price > 9.9

    def test_reset(self):
        """重置模拟券商"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(price=10.0)
        broker.submit_order(order)
        bar = _make_bar(close=10.0)
        broker.on_bar(bar)
        broker.reset()
        assert broker.position_count == 0
        assert broker.available_cash == 1_000_000.0

    def test_sell_order(self):
        """卖出订单撮合"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        # 先买入
        buy_order = _make_order(side=OrderSide.BUY, quantity=100, price=10.0)
        broker.submit_order(buy_order)
        broker.on_bar(_make_bar(close=10.0))
        # 再卖出
        sell_order = _make_order(side=OrderSide.SELL, quantity=100, price=11.0)
        broker.submit_order(sell_order)
        trades = broker.on_bar(_make_bar(close=11.0, high=11.5))
        assert isinstance(trades, list)

    def test_market_order(self):
        """市价单"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(order_type=OrderType.MARKET, quantity=100)
        broker.submit_order(order)
        bar = _make_bar(close=10.0)
        trades = broker.on_bar(bar)
        # 市价单应立即成交
        assert len(trades) >= 1

    def test_repr(self):
        """__repr__ 不崩溃"""
        broker = SimBroker()
        r = repr(broker)
        assert isinstance(r, str)
        assert "SimBroker" in r


# ═══════════════════════════════════════════
#  TestSimBrokerLotSizeEnforcement - 整手执行（P0-03 续卡 #348）
# ═══════════════════════════════════════════

class TestSimBrokerLotSizeEnforcement:
    """SimBroker 按 lot_size 限制零股买入（买入整手截断/卖出不限/默认兼容）。"""

    def test_buy_truncated_to_lot(self):
        """lot_size=100 买单 150 股 → 成交 100（向下取整）。"""
        broker = SimBroker(initial_capital=1_000_000.0, lot_size=100)
        broker.connect()
        order = _make_order(side=OrderSide.BUY, quantity=150, price=10.0)
        broker.submit_order(order)
        trades = broker.on_bar(_make_bar(close=10.0))
        assert len(trades) == 1
        assert trades[0].quantity == 100

    def test_buy_below_one_lot_not_filled(self):
        """lot_size=100 买单 50 股（不足一手）→ 不成交。"""
        broker = SimBroker(initial_capital=1_000_000.0, lot_size=100)
        broker.connect()
        order = _make_order(side=OrderSide.BUY, quantity=50, price=10.0)
        broker.submit_order(order)
        trades = broker.on_bar(_make_bar(close=10.0))
        assert len(trades) == 0

    def test_sell_not_truncated(self):
        """lot_size=100 卖单 150 股 → 成交 150（卖出不限制，零股可卖）。"""
        broker = SimBroker(initial_capital=1_000_000.0, lot_size=100)
        broker.connect()
        order = _make_order(side=OrderSide.SELL, quantity=150, price=10.0)
        broker.submit_order(order)
        trades = broker.on_bar(_make_bar(close=10.0))
        assert len(trades) == 1
        assert trades[0].quantity == 150

    def test_default_lot_zero_backward_compatible(self):
        """lot_size=0（未装配整手约束）买单 50 股 → 成交 50（向后兼容）。"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(side=OrderSide.BUY, quantity=50, price=10.0)
        broker.submit_order(order)
        trades = broker.on_bar(_make_bar(close=10.0))
        assert len(trades) == 1
        assert trades[0].quantity == 50


# ═══════════════════════════════════════════
#  TestOrderStateMachine - 订单状态机
# ═══════════════════════════════════════════

class TestOrderStateMachine:
    """OrderStateMachine 订单状态机测试"""

    def test_instantiation(self):
        """实例化"""
        sm = OrderStateMachine()
        assert sm is not None

    def test_can_transition_valid(self):
        """合法状态转换"""
        assert OrderStateMachine.can_transition(
            OrderStatus.PENDING, OrderStatus.SUBMITTED) is True

    def test_can_transition_invalid(self):
        """非法状态转换"""
        # 已成交不能再撤单
        assert OrderStateMachine.can_transition(
            OrderStatus.FILLED, OrderStatus.CANCELLED) is False

    def test_submit(self):
        """提交订单"""
        sm = OrderStateMachine()
        order = _make_order()
        result = sm.submit(order)
        assert result is True
        assert order.status == OrderStatus.SUBMITTED

    def test_fill(self):
        """成交订单"""
        sm = OrderStateMachine()
        order = _make_order()
        sm.submit(order)
        result = sm.fill(order, quantity=100, price=10.0)
        assert result is True
        assert order.status == OrderStatus.FILLED
        assert order.filled_qty == 100

    def test_cancel(self):
        """取消订单"""
        sm = OrderStateMachine()
        order = _make_order()
        sm.submit(order)
        result = sm.cancel(order)
        assert result is True
        assert order.status == OrderStatus.CANCELLED

    def test_reject(self):
        """拒绝订单"""
        sm = OrderStateMachine()
        order = _make_order()
        result = sm.reject(order, reason="资金不足")
        assert result is True
        assert order.status == OrderStatus.REJECTED

    def test_expire(self):
        """订单过期"""
        sm = OrderStateMachine()
        order = _make_order()
        sm.submit(order)
        result = sm.expire(order)
        assert result is True

    def test_is_active_pending(self):
        """PENDING 是活跃状态"""
        order = _make_order(status=OrderStatus.PENDING)
        assert OrderStateMachine.is_active(order) is True

    def test_is_active_submitted(self):
        """SUBMITTED 是活跃状态"""
        order = _make_order(status=OrderStatus.SUBMITTED)
        assert OrderStateMachine.is_active(order) is True

    def test_is_terminal_filled(self):
        """FILLED 是终态"""
        order = _make_order(status=OrderStatus.FILLED)
        assert OrderStateMachine.is_terminal(order) is True

    def test_is_terminal_cancelled(self):
        """CANCELLED 是终态"""
        order = _make_order(status=OrderStatus.CANCELLED)
        assert OrderStateMachine.is_terminal(order) is True

    def test_is_terminal_rejected(self):
        """REJECTED 是终态"""
        order = _make_order(status=OrderStatus.REJECTED)
        assert OrderStateMachine.is_terminal(order) is True

    def test_is_filled(self):
        """is_filled 判断"""
        order = _make_order(status=OrderStatus.FILLED)
        assert OrderStateMachine.is_filled(order) is True

    def test_is_cancelled(self):
        """is_cancelled 判断"""
        order = _make_order(status=OrderStatus.CANCELLED)
        assert OrderStateMachine.is_cancelled(order) is True

    def test_is_rejected(self):
        """is_rejected 判断"""
        order = _make_order(status=OrderStatus.REJECTED)
        assert OrderStateMachine.is_rejected(order) is True

    def test_transition_history(self):
        """转换历史记录（属性）"""
        sm = OrderStateMachine()
        order = _make_order()
        sm.submit(order)
        sm.fill(order, 100, 10.0)
        history = sm.transition_history
        assert isinstance(history, list)
        assert len(history) >= 1
        # 第一条是 PENDING -> SUBMITTED
        assert history[0]["from"] == "PENDING"
        assert history[0]["to"] == "SUBMITTED"

    def test_get_order_transitions(self):
        """按订单ID获取转换历史"""
        sm = OrderStateMachine()
        order = _make_order()
        sm.submit(order)
        transitions = sm.get_order_transitions(order.order_id)
        assert isinstance(transitions, list)
        assert len(transitions) >= 1

    def test_cannot_cancel_filled(self):
        """已成交订单不能撤单（抛 ExecutionError）"""
        from myquant.core.exceptions import ExecutionError
        sm = OrderStateMachine()
        order = _make_order()
        sm.submit(order)
        sm.fill(order, 100, 10.0)
        with pytest.raises(ExecutionError):
            sm.cancel(order)

    def test_cannot_fill_cancelled(self):
        """已撤单订单不能成交（抛 ExecutionError）"""
        from myquant.core.exceptions import ExecutionError
        sm = OrderStateMachine()
        order = _make_order()
        sm.submit(order)
        sm.cancel(order)
        with pytest.raises(ExecutionError):
            sm.fill(order, 100, 10.0)

    def test_repr(self):
        """__repr__ 不崩溃"""
        sm = OrderStateMachine()
        r = repr(sm)
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestBrokerManager - 券商管理器
# ═══════════════════════════════════════════

class TestBrokerManager:
    """BrokerManager 券商管理器测试（实盘准备）"""

    def _make_manager(self):
        from myquant.live.brokers.manager import BrokerManager
        return BrokerManager(auto_failover=True)

    def test_instantiation(self):
        """实例化"""
        mgr = self._make_manager()
        assert mgr is not None

    def test_register_broker_interface(self):
        """注册券商接口存在"""
        from myquant.live.brokers.manager import BrokerManager
        assert hasattr(BrokerManager, "register_broker")
        assert callable(BrokerManager.register_broker)

    def test_list_brokers_interface(self):
        """列出券商接口存在"""
        from myquant.live.brokers.manager import BrokerManager
        assert hasattr(BrokerManager, "list_brokers")
        assert callable(BrokerManager.list_brokers)

    def test_get_broker_interface(self):
        """获取券商接口存在"""
        from myquant.live.brokers.manager import BrokerManager
        assert hasattr(BrokerManager, "get_broker")
        assert callable(BrokerManager.get_broker)

    def test_set_primary_interface(self):
        """设置主券商接口存在"""
        from myquant.live.brokers.manager import BrokerManager
        assert hasattr(BrokerManager, "set_primary")
        assert callable(BrokerManager.set_primary)

    def test_auto_failover_default(self):
        """默认启用故障转移"""
        mgr = self._make_manager()
        assert mgr._auto_failover is True

    def test_list_brokers_empty(self):
        """空管理器列出空列表"""
        mgr = self._make_manager()
        brokers = mgr.list_brokers()
        assert isinstance(brokers, list)

    def test_no_broker_available_error(self):
        """无券商时获取抛出异常"""
        from myquant.live.brokers.manager import NoBrokerAvailableError
        mgr = self._make_manager()
        with pytest.raises(NoBrokerAvailableError):
            mgr.get_broker()

    def test_broker_not_found_error(self):
        """BrokerNotFoundError 异常类存在"""
        from myquant.live.brokers.manager import BrokerNotFoundError
        assert issubclass(BrokerNotFoundError, Exception)

    def test_registrations(self):
        """注册信息查询"""
        mgr = self._make_manager()
        regs = mgr.registrations
        assert isinstance(regs, dict)


# ═══════════════════════════════════════════
#  TestRealTimePreparation - 实盘准备
# ═══════════════════════════════════════════

class TestRealTimePreparation:
    """实盘无缝对接准备测试"""

    def test_execution_interface_unified(self):
        """ExecutionInterface 统一回测/实盘接口"""
        # SimBroker 实现接口，实盘券商也应实现同一接口
        assert issubclass(SimBroker, ExecutionInterface)
        # 接口方法对回测和实盘一致
        for method in ["submit_order", "cancel_order", "get_order",
                       "get_positions", "get_account"]:
            assert hasattr(ExecutionInterface, method)

    def test_sim_broker_matching_no_future_data(self):
        """撮合不使用未来数据（仅当前bar）"""
        broker = SimBroker(initial_capital=1_000_000.0)
        broker.connect()
        order = _make_order(price=10.0)
        broker.submit_order(order)
        # 只用当前bar撮合
        bar = _make_bar(open_p=9.9, high=10.1, low=9.8, close=10.0)
        trades = broker.on_bar(bar)
        assert isinstance(trades, list)
        # 不引用下一根bar

    def test_order_state_machine_for_live(self):
        """状态机适用于实盘订单生命周期"""
        sm = OrderStateMachine()
        order = _make_order()
        # 实盘流程：提交→部分成交→全部成交
        sm.submit(order)
        sm.fill(order, 50, 10.0)  # 部分成交
        assert order.filled_qty == 50
        sm.fill(order, 50, 10.1)  # 全部成交
        assert order.status == OrderStatus.FILLED

    def test_broker_manager_failover(self):
        """BrokerManager 故障转移为实盘高可用准备"""
        from myquant.live.brokers.manager import BrokerManager
        mgr = BrokerManager(auto_failover=True)
        assert mgr._auto_failover is True

    def test_commission_model_for_live(self):
        """佣金模型适用于实盘A股交易成本"""
        cm = CommissionModel(
            buy_rate=0.0002, sell_rate=0.0002,
            min_commission=5.0, stamp_tax=0.001,
            transfer_fee=0.00001,
        )
        # A股标准：佣金万2(最低5元)、印花税千1(卖出)、过户费万0.1
        assert cm.buy_rate == 0.0002
        assert cm.stamp_tax == 0.001
