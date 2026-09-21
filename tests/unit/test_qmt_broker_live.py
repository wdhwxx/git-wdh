# 文件名：tests/unit/test_qmt_broker_live.py
"""
test_qmt_broker_live.py - QMT 券商适配器 LIVE 模式单元测试
===========================================================
覆盖 myquant/live/brokers/qmt_broker.py 的真实 SDK 通道（LIVE 模式）：
  - SIM 模式不导入 xtquant（环境隔离）
  - LIVE 模式缺配置时 connect 明确失败
  - _call_qmt_sdk 仅 LIVE 可用（SIM 调用抛错）
  - QMT 委托状态码 → OrderStatus 映射
  - QMT 持仓 → 本地 Position 映射
  - 未连接时下单拒绝（风控前移）
  - 完整 LIVE 流程（fake SDK）：connect → submit → 成交回报 → 撤单 → 资金/持仓查询

测试方式: 使用 fake 对象模拟 xtquant，不实际连接 QMT 客户端。

运行方式:
  pytest tests/unit/test_qmt_broker_live.py -v

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/unit/test_qmt_broker_live.py）
  2. 为实时行情及实盘无缝对接做好充足准备（LIVE 通道全分支覆盖）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 成交价格/数量只来自券商回报，测试不假设未来价格
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from myquant.live.brokers.qmt_broker import QmtBroker
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType


# ═══════════════════════════════════════════
#  fake SDK（模拟 xtquant，不实际连接 QMT）
# ═══════════════════════════════════════════


class _FakeXtConstant:
    """fake xtconstant：LIVE 下单所需的 QMT 常量"""

    STOCK_BUY = 23
    STOCK_SELL = 24
    FIX_PRICE = 11
    LATEST_PRICE = 5
    MARKET_PEER_PRICE_FIRST = 44


class _FakeXtQuantTraderCallback:
    """fake 回调基类（模拟 XtQuantTraderCallback 的空实现）"""

    pass


class _FakeStockAccount:
    """fake StockAccount（模拟资金账号对象）"""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id


class _FakeXtQuantTrader:
    """fake XtQuantTrader：记录调用、返回预设结果"""

    def __init__(self, path: str = "", session: int = 0) -> None:
        self.path = path
        self.session = session
        self.start_called = 0
        self.stop_called = 0
        self.connect_called = 0
        self.subscribe_called = 0
        self.order_calls: List[tuple] = []
        self.cancel_calls: List[tuple] = []
        self.callback = None
        self.connect_result = 0
        self.subscribe_result = 0
        self.next_order_id = 100001
        self.account_status_ok = True
        self.fake_asset: Optional[Any] = None
        self.fake_positions: List[Any] = []

    def register_callback(self, callback: Any) -> None:
        self.callback = callback

    def start(self) -> None:
        self.start_called += 1

    def stop(self) -> None:
        self.stop_called += 1

    def connect(self) -> int:
        self.connect_called += 1
        return self.connect_result

    def subscribe(self, account: Any) -> int:
        self.subscribe_called += 1
        return self.subscribe_result

    def order_stock(
        self,
        account: Any,
        symbol: str,
        order_type: int,
        order_volume: int,
        price_type: int,
        price: float,
        strategy_name: str = "",
        order_remark: str = "",
    ) -> int:
        self.order_calls.append(
            (account, symbol, order_type, order_volume, price_type, price,
             strategy_name, order_remark)
        )
        qmt_id = self.next_order_id
        self.next_order_id += 1
        return qmt_id

    def cancel_order_stock(self, account: Any, order_id: int) -> int:
        self.cancel_calls.append((account, order_id))
        return 0

    def query_account_status(self) -> List[Any]:
        class _St:
            status: int = 0 if self.account_status_ok else -1
        return [_St()]

    def query_stock_asset(self, account: Any) -> Any:
        if self.fake_asset is not None:
            return self.fake_asset
        class _A:
            cash: float = 1_000_000.0
            frozen_cash: float = 0.0
            market_value: float = 0.0
            total_asset: float = 1_000_000.0
        return _A()

    def query_stock_positions(self, account: Any) -> List[Any]:
        return list(self.fake_positions)


class _FakeXtAsset:
    """fake XtAsset（query_stock_asset 返回）"""

    cash: float = 1_000_000.0
    frozen_cash: float = 0.0
    market_value: float = 0.0
    total_asset: float = 1_000_000.0


class _FakeXtPosition:
    """fake XtPosition（query_stock_positions 返回）"""

    stock_code: str = "600000.SH"
    volume: float = 1000.0
    can_use_volume: float = 800.0
    open_price: float = 10.0
    market_value: float = 10200.0


class _FakeXtTradeResponse:
    """fake 成交回报（on_stock_trade 参数）"""

    order_id: int
    traded_price: float
    traded_volume: float
    trade_id: str = "T12345"

    def __init__(self, order_id: int, price: float, volume: float) -> None:
        self.order_id = order_id
        self.traded_price = price
        self.traded_volume = volume


class _FakeXtOrderResponse:
    """fake 委托回报（on_stock_order 参数）"""

    order_id: int
    status: int
    status_msg: str = ""
    traded_volume: float = 0.0
    traded_price: float = 0.0

    def __init__(self, order_id: int, status: int, traded_volume: float = 0.0,
                 traded_price: float = 0.0, status_msg: str = "") -> None:
        self.order_id = order_id
        self.status = status
        self.traded_volume = traded_volume
        self.traded_price = traded_price
        self.status_msg = status_msg


# ═══════════════════════════════════════════
#  fixture：LIVE broker（注入 fake SDK）
# ═══════════════════════════════════════════


@pytest.fixture
def live_broker(monkeypatch) -> tuple:
    """构造 LIVE 模式 QmtBroker 并注入 fake xtquant SDK"""
    broker = QmtBroker(
        qmt_path="D:\\fake\\qmt\\userdata_mini",
        account_id="1000001",
        mode="LIVE",
        initial_capital=1_000_000.0,
    )
    fake = _FakeXtQuantTrader()
    fake_asset = _FakeXtAsset()
    fake_asset.cash = 1_000_000.0
    fake_asset.frozen_cash = 0.0
    fake.fake_asset = fake_asset

    def _make_trader_cls():
        """返回 fake 类：每次构造都返回同一个 fake 实例（便于断言调用）"""

        class _Cls(_FakeXtQuantTrader):
            def __new__(cls, path: str = "", session: int = 0) -> _FakeXtQuantTrader:
                return fake

        return _Cls

    def _fake_import():
        return (_FakeXtConstant(), _make_trader_cls(), _FakeXtQuantTraderCallback,
                _FakeStockAccount)

    monkeypatch.setattr(broker, "_import_xtquant", _fake_import)
    return broker, fake


def _make_order(side: OrderSide = OrderSide.BUY, quantity: float = 100.0,
                price: float = 10.0, symbol: str = "600000.SH") -> Order:
    return Order(
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        price=price,
        strategy_id="test_strategy",
    )


# ═══════════════════════════════════════════
#  1. 模式与环境隔离
# ═══════════════════════════════════════════


class TestModeIsolation:
    def test_sim_mode_default(self) -> None:
        """默认 mode=SIM，不导入 xtquant 即可正常构造"""
        broker = QmtBroker()
        assert broker._mode == "SIM"
        assert broker._xt_trader is None

    def test_live_mode_requires_config(self) -> None:
        """LIVE 模式缺 qmt_path/account_id → connect 明确失败"""
        broker = QmtBroker(mode="LIVE")
        assert broker.connect() is False
        assert broker._connected is False

    def test_call_qmt_sdk_sim_mode_raises(self) -> None:
        """SIM 模式调用 _call_qmt_sdk → RuntimeError（上层应已按 mode 分支）"""
        broker = QmtBroker(mode="SIM")
        with pytest.raises(RuntimeError):
            broker._call_qmt_sdk("heartbeat")

    def test_live_mode_unknown_method(self, live_broker) -> None:
        """未知 method → ValueError"""
        broker, _ = live_broker
        with pytest.raises(ValueError):
            broker._call_qmt_sdk("no_such_method")


# ═══════════════════════════════════════════
#  2. 连接流程
# ═══════════════════════════════════════════


class TestConnect:
    def test_connect_success(self, live_broker) -> None:
        """connect 成功：start/connect/subscribe 全部调用，SDK ready"""
        broker, fake = live_broker
        assert broker.connect() is True
        assert fake.start_called == 1
        assert fake.connect_called == 1
        assert fake.subscribe_called == 1
        assert broker._qmt_sdk_ready is True
        assert broker._qmt_login_ok is True
        assert broker._connected is True

    def test_connect_failure(self, live_broker) -> None:
        """connect 失败（fake 返回非0）→ False 且 SDK 未就绪"""
        broker, fake = live_broker
        fake.connect_result = -1
        assert broker.connect() is False
        assert broker._connected is False
        assert broker._qmt_sdk_ready is False

    def test_disconnect(self, live_broker) -> None:
        """disconnect：stop 被调用，状态清理"""
        broker, fake = live_broker
        broker.connect()
        broker.disconnect()
        assert fake.stop_called == 1
        assert broker._connected is False
        assert broker._qmt_sdk_ready is False

    def test_heartbeat_ok(self, live_broker) -> None:
        """心跳正常：连接保持"""
        broker, fake = live_broker
        broker.connect()
        assert broker.heartbeat() is True

    def test_heartbeat_fail_marks_disconnected(self, live_broker) -> None:
        """心跳失败 → 连接置断线"""
        broker, fake = live_broker
        broker.connect()
        fake.account_status_ok = False
        assert broker.heartbeat() is False
        assert broker._connected is False


# ═══════════════════════════════════════════
#  3. 委托/撤单/成交回报
# ═══════════════════════════════════════════


class TestOrderFlow:
    def test_submit_unconnected_rejected(self, live_broker) -> None:
        """未连接时下单 → 订单拒绝（风控前移）"""
        broker, _ = live_broker
        order = _make_order()
        result = broker.submit_order(order)
        assert result.status is OrderStatus.REJECTED
        assert "未连接" in result.extra.get("reject_reason", "")

    def test_submit_order_live_success(self, live_broker) -> None:
        """LIVE 下单成功：建立 QMT 委托编号 ↔ 本地 order_id 映射"""
        broker, fake = live_broker
        broker.connect()
        order = _make_order(quantity=100, price=10.0)
        result = broker.submit_order(order)
        assert result.status is OrderStatus.SUBMITTED
        assert len(fake.order_calls) == 1
        _, symbol, otype, volume, ptype, price, sname, remark = fake.order_calls[0]
        assert symbol == "600000.SH"
        assert otype == _FakeXtConstant.STOCK_BUY
        assert volume == 100
        assert ptype == _FakeXtConstant.FIX_PRICE
        assert price == 10.0
        assert remark == order.order_id[:24]
        # 双向映射建立
        assert broker._local_to_qmt[order.order_id] == 100001
        assert broker._qmt_to_local[100001] == order.order_id
        assert order.extra["qmt_order_id"] == 100001

    def test_submit_sell_uses_stock_sell(self, live_broker) -> None:
        """卖出单：QMT 委托类型为 STOCK_SELL"""
        broker, fake = live_broker
        broker.connect()
        # fake 持仓 600000.SH 1000 股（可用 800）
        fake.fake_positions = [_FakeXtPosition()]
        order = _make_order(side=OrderSide.SELL, quantity=100, price=10.5)
        result = broker.submit_order(order)
        assert result.status is OrderStatus.SUBMITTED
        assert fake.order_calls[0][2] == _FakeXtConstant.STOCK_SELL

    def test_submit_sell_insufficient_qty_rejected(self, live_broker) -> None:
        """卖出量 > 可用持仓 → 拒绝（QMT 权威校验）"""
        broker, fake = live_broker
        broker.connect()
        fake.fake_positions = [_FakeXtPosition()]  # 可用 800
        order = _make_order(side=OrderSide.SELL, quantity=1000, price=10.5)
        result = broker.submit_order(order)
        assert result.status is OrderStatus.REJECTED
        assert "可卖数量不足" in result.extra.get("reject_reason", "")

    def test_submit_buy_insufficient_cash_rejected(self, live_broker) -> None:
        """买入金额 > 可用资金 → 拒绝（QMT 权威校验）"""
        broker, fake = live_broker
        broker.connect()
        fake.fake_asset.cash = 500.0
        order = _make_order(quantity=100, price=10.0)  # 需要 1000 > 500
        result = broker.submit_order(order)
        assert result.status is OrderStatus.REJECTED
        assert "资金不足" in result.extra.get("reject_reason", "")

    def test_on_stock_trade_executes_fill(self, live_broker) -> None:
        """成交回报：订单 FILLED，成交记录落地（LIVE 不更新本地资金）"""
        broker, fake = live_broker
        broker.connect()
        order = _make_order(quantity=100, price=10.0)
        broker.submit_order(order)
        # 模拟 QMT 成交回报
        resp = _FakeXtTradeResponse(100001, 10.05, 100.0)
        broker._on_qmt_trade(resp)
        assert order.status is OrderStatus.FILLED
        assert len(broker.get_trades()) == 1
        trade = broker.get_trades()[0]
        assert trade.price == 10.05
        assert trade.quantity == 100
        assert trade.extra.get("qmt_trade_id") == "T12345"
        # LIVE 不本地扣资金（QMT 账本权威）
        assert broker.cash == 1_000_000.0

    def test_on_stock_order_rejected(self, live_broker) -> None:
        """废单回报：订单 REJECTED"""
        broker, fake = live_broker
        broker.connect()
        order = _make_order()
        broker.submit_order(order)
        resp = _FakeXtOrderResponse(100001, 57, status_msg="价格超出范围")
        broker._on_qmt_order(resp)
        assert order.status is OrderStatus.REJECTED
        assert "价格超出范围" in order.extra.get("reject_reason", "")

    def test_on_stock_order_cancelled(self, live_broker) -> None:
        """已撤回报：订单 CANCELLED"""
        broker, fake = live_broker
        broker.connect()
        order = _make_order()
        broker.submit_order(order)
        resp = _FakeXtOrderResponse(100001, 54)
        broker._on_qmt_order(resp)
        assert order.status is OrderStatus.CANCELLED

    def test_cancel_order_live(self, live_broker) -> None:
        """撤单：cancel_order_stock 按映射的 QMT 委托编号发起"""
        broker, fake = live_broker
        broker.connect()
        order = _make_order()
        broker.submit_order(order)
        assert broker.cancel_order(order.order_id) is True
        assert len(fake.cancel_calls) == 1
        assert fake.cancel_calls[0][1] == 100001

    def test_cancel_order_live_sdk_fail(self, live_broker, monkeypatch) -> None:
        """撤单 SDK 返回非0 → False（订单保持活跃）"""
        broker, fake = live_broker
        broker.connect()
        order = _make_order()
        broker.submit_order(order)

        def _fail_cancel(account: Any, order_id: int) -> int:
            return -1

        monkeypatch.setattr(fake, "cancel_order_stock", _fail_cancel)
        assert broker.cancel_order(order.order_id) is False
        assert order.status is OrderStatus.SUBMITTED


# ═══════════════════════════════════════════
#  4. 账户/持仓查询
# ═══════════════════════════════════════════


class TestAccountAndPositions:
    def test_get_account_live(self, live_broker) -> None:
        """资金查询：QMT 权威数据返回"""
        broker, fake = live_broker
        broker.connect()
        fake.fake_asset.cash = 888_888.0
        fake.fake_asset.frozen_cash = 1000.0
        fake.fake_asset.market_value = 50_000.0
        fake.fake_asset.total_asset = 938_888.0
        account = broker.get_account()
        assert account["cash"] == 888_888.0
        assert account["available_cash"] == 887_888.0
        assert account["market_value"] == 50_000.0
        assert account["account_id"] == "1000001"
        assert account["mode"] == "LIVE"

    def test_get_positions_live(self, live_broker) -> None:
        """持仓查询：QMT XtPosition → 本地 Position"""
        broker, fake = live_broker
        broker.connect()
        fake.fake_positions = [_FakeXtPosition()]
        positions = broker.get_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos.symbol == "600000.SH"
        assert pos.quantity == 1000.0
        assert pos.avg_cost == 10.0
        assert pos.market_price == pytest.approx(10.2)  # 10200 / 1000
        assert pos.extra["available_qty"] == 800.0


# ═══════════════════════════════════════════
#  5. 状态映射
# ═══════════════════════════════════════════


class TestStatusMapping:
    def test_map_qmt_order_status(self) -> None:
        """QMT 委托状态码 → OrderStatus 映射"""
        broker = QmtBroker(mode="LIVE")
        assert broker._map_qmt_order_status(48) is OrderStatus.SUBMITTED  # 未报
        assert broker._map_qmt_order_status(49) is OrderStatus.SUBMITTED  # 待报
        assert broker._map_qmt_order_status(50) is OrderStatus.SUBMITTED  # 已报
        assert broker._map_qmt_order_status(51) is OrderStatus.SUBMITTED  # 已报待撤
        assert broker._map_qmt_order_status(52) is OrderStatus.SUBMITTED  # 部成待撤
        assert broker._map_qmt_order_status(53) is OrderStatus.CANCELLED  # 部撤
        assert broker._map_qmt_order_status(54) is OrderStatus.CANCELLED  # 已撤
        assert broker._map_qmt_order_status(55) is OrderStatus.PARTIAL_FILLED  # 部成
        assert broker._map_qmt_order_status(56) is OrderStatus.FILLED      # 已成
        assert broker._map_qmt_order_status(57) is OrderStatus.REJECTED    # 废单
        assert broker._map_qmt_order_status(255) is OrderStatus.SUBMITTED  # 未知

    def test_map_qmt_position_empty(self) -> None:
        """空/非法持仓 → None"""
        broker = QmtBroker(mode="LIVE")
        assert broker._map_qmt_position(None) is None

        class _Zero:
            stock_code: str = "600000.SH"
            volume: float = 0.0

        assert broker._map_qmt_position(_Zero()) is None
