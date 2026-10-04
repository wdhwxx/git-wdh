# 文件名：tests/unit/test_signal_idempotency.py
"""
test_signal_idempotency.py - 信号/订单幂等去重测试（任务43）
====================================================================
覆盖三层幂等去重（防桥接网络波动导致信号重发触发频繁报撤单）：
  1. Signal 确定性信号 ID — make_signal_id 由 strategy_id/symbol/日期/方向
     派生（SHA-256 前24位，适配 QMT order_remark 上限），同参数恒同 ID
  2. 引擎侧信号去重      — _process_signal 对同一 bar 重发信号直接丢弃
     （dedup_skipped 统计，不重复生成订单/走风控/走券商）
  3. QMT 端订单去重      — qmt_broker 对同一 order_id 重复提交直接返回
     已有订单（SIM 不重复撮合，LIVE 不重复 order_stock 报单）

回归依据: 审计问题 43（桥接重发无去重，QMT 端可能频繁报撤单）
运行方式: pytest tests/unit/test_signal_idempotency.py -v
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from myquant.live.engine import LiveEngine, RiskConfig
from myquant.live.brokers.qmt_broker import QmtBroker
from myquant.models.market_data import Bar, BarFrequency
from myquant.models.trade import Order, OrderSide, OrderType
from myquant.strategies.base import Signal, SignalType


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _ts(year: int, month: int, day: int, hour: int = 15, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute).timestamp()


def _make_bar(symbol: str = "000001.SZ", ts: float | None = None,
              close: float = 10.0, freq: BarFrequency = BarFrequency.DAILY) -> Bar:
    return Bar(
        symbol=symbol,
        frequency=freq,
        timestamp=ts if ts is not None else _ts(2024, 1, 15),
        close=close,
    )


def _make_signal(symbol: str = "000001.SZ", st: SignalType = SignalType.BUY,
                 strategy_id: str = "ma_cross") -> Signal:
    return Signal(symbol=symbol, signal_type=st, strategy_id=strategy_id, reason="test")


def _make_min_engine() -> LiveEngine:
    """最小引擎（关闭全部保障组件，直接驱动 _process_signal）"""
    return LiveEngine(
        engine_id="test_idem",
        risk_config=RiskConfig(max_positions=10, max_position_pct=0.5),
        enable_risk_manager=False,
        enable_guardian=False,
        enable_state_store=False,
        enable_scheduler=False,
        enable_event_bus=False,
    )


# ═══════════════════════════════════════════
#  1. Signal 确定性信号 ID
# ═══════════════════════════════════════════

class TestSignalId:
    """任务43：Signal 确定性信号 ID"""

    def test_同参数恒同ID(self) -> None:
        s1 = _make_signal()
        s2 = _make_signal()
        assert s1.make_signal_id(as_of_date="2024-01-15") == \
            s2.make_signal_id(as_of_date="2024-01-15")

    def test_不同日期不同ID(self) -> None:
        s = _make_signal()
        assert s.make_signal_id(as_of_date="2024-01-15") != \
            s.make_signal_id(as_of_date="2024-01-16")

    def test_不同方向不同ID(self) -> None:
        assert _make_signal(st=SignalType.BUY).make_signal_id(as_of_date="2024-01-15") != \
            _make_signal(st=SignalType.SELL).make_signal_id(as_of_date="2024-01-15")

    def test_不同策略不同ID(self) -> None:
        assert _make_signal(strategy_id="ma_cross").make_signal_id(as_of_date="2024-01-15") != \
            _make_signal(strategy_id="rsrs").make_signal_id(as_of_date="2024-01-15")

    def test_不同标的不同ID(self) -> None:
        assert _make_signal(symbol="000001.SZ").make_signal_id(as_of_date="2024-01-15") != \
            _make_signal(symbol="600000.SH").make_signal_id(as_of_date="2024-01-15")

    def test_ID为24字符(self) -> None:
        """24 位 hex，适配 QMT order_remark 上限（24 英文字符）"""
        sid = _make_signal().make_signal_id(as_of_date="2024-01-15")
        assert len(sid) == 24
        assert all(c in "0123456789abcdef" for c in sid)

    def test_无日期参数回退信号时间戳日期(self) -> None:
        s = _make_signal()
        s.timestamp = _ts(2024, 1, 15)
        assert s.make_signal_id() == s.make_signal_id(as_of_date="2024-01-15")


class TestSignalToOrder:
    """任务43：to_order 用 signal_id 派生确定性 order_id"""

    def test_有signal_id时order_id确定(self) -> None:
        s = _make_signal()
        s.signal_id = s.make_signal_id(as_of_date="2024-01-15")
        o1 = s.to_order(100, price=10.0)
        o2 = s.to_order(100, price=10.0)
        assert o1 is not None and o2 is not None
        assert o1.order_id == o2.order_id == s.signal_id

    def test_无signal_id时回退默认uuid(self) -> None:
        s = _make_signal()
        o = s.to_order(100, price=10.0)
        assert o is not None
        assert o.order_id != ""  # dataclass 默认 uuid4（16位hex）


# ═══════════════════════════════════════════
#  2. 引擎侧信号去重（_process_signal）
# ═══════════════════════════════════════════

class TestEngineDedup:
    """任务43：引擎对重复信号直接丢弃"""

    def test_同一bar重发信号丢弃(self) -> None:
        engine = _make_min_engine()
        bar = _make_bar(ts=_ts(2024, 1, 15))
        sig = _make_signal()
        # 第一次：正常处理（生成订单并登记去重）
        engine._process_signal(sig, bar)
        # 第二次：同一信号实例（桥接重发场景）→ 丢弃
        engine._process_signal(sig, bar)
        assert engine._stats["total_signals"] == 2   # 引擎收到2次
        assert engine._stats["dedup_skipped"] == 1   # 丢弃1次
        # 防未来校验器只记录实际处理的信号（第二次被去重，未进 record_signal）
        assert engine._lookahead_guard.stats["signals_recorded"] == 1

    def test_重建同bar信号同样丢弃(self) -> None:
        """桥接重发可能构造新 Signal 实例（timestamp不同），依赖 bar 日期归一化去重"""
        engine = _make_min_engine()
        bar = _make_bar(ts=_ts(2024, 1, 15))
        engine._process_signal(_make_signal(), bar)
        engine._process_signal(_make_signal(), bar)  # 新实例，同 bar
        assert engine._stats["dedup_skipped"] == 1
        assert engine._lookahead_guard.stats["signals_recorded"] == 1

    def test_不同bar日期不误去重(self) -> None:
        engine = _make_min_engine()
        engine._process_signal(_make_signal(), _make_bar(ts=_ts(2024, 1, 15)))
        engine._process_signal(_make_signal(), _make_bar(ts=_ts(2024, 1, 16)))
        assert engine._stats["dedup_skipped"] == 0
        assert engine._lookahead_guard.stats["signals_recorded"] == 2

    def test_不同方向不误去重(self) -> None:
        engine = _make_min_engine()
        bar = _make_bar(ts=_ts(2024, 1, 15))
        engine._process_signal(_make_signal(st=SignalType.BUY), bar)
        engine._process_signal(_make_signal(st=SignalType.SELL), bar)
        assert engine._stats["dedup_skipped"] == 0
        assert engine._lookahead_guard.stats["signals_recorded"] == 2

    def test_分钟线粒度区分同日内不同bar(self) -> None:
        """分钟线去重粒度精确到分钟：同一日不同分钟 bar 不误去重"""
        engine = _make_min_engine()
        engine._process_signal(
            _make_signal(), _make_bar(ts=_ts(2024, 1, 15, 10, 30), freq=BarFrequency.MINUTE_5)
        )
        engine._process_signal(
            _make_signal(), _make_bar(ts=_ts(2024, 1, 15, 10, 35), freq=BarFrequency.MINUTE_5)
        )
        assert engine._stats["dedup_skipped"] == 0
        assert engine._lookahead_guard.stats["signals_recorded"] == 2

    def test_start重置去重集合(self) -> None:
        """每次启动全新会话：去重集合清空"""
        engine = _make_min_engine()
        engine._process_signal(_make_signal(), _make_bar(ts=_ts(2024, 1, 15)))
        assert len(engine._processed_signal_ids) == 1
        # 模拟新会话：清空后同 bar 可再次处理
        engine._processed_signal_ids.clear()
        engine._process_signal(_make_signal(), _make_bar(ts=_ts(2024, 1, 15)))
        assert engine._lookahead_guard.stats["signals_recorded"] == 2


# ═══════════════════════════════════════════
#  3. QMT 端订单去重（qmt_broker）
# ═══════════════════════════════════════════

class _FakeXtConstant:
    STOCK_BUY = 23
    STOCK_SELL = 24
    FIX_PRICE = 11
    MARKET_PEER_PRICE_FIRST = 44


class _FakeXtQuantTraderCallback:
    pass


class _FakeStockAccount:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id


class _FakeXtQuantTrader:
    """fake XtQuantTrader：记录 order_stock 调用次数"""

    def __init__(self, path: str = "", session: int = 0) -> None:
        self.path = path
        self.session = session
        self.start_called = 0
        self.stop_called = 0
        self.connect_called = 0
        self.subscribe_called = 0
        self.order_calls: list = []
        self.cancel_calls: list = []
        self.callback = None
        self.connect_result = 0
        self.subscribe_result = 0
        self.next_order_id = 100001
        self.fake_asset = None

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

    def order_stock(self, account: Any, symbol: str, order_type: int,
                    order_volume: int, price_type: int, price: float,
                    strategy_name: str = "", order_remark: str = "") -> int:
        self.order_calls.append((symbol, order_volume, price_type, price, order_remark))
        qmt_id = self.next_order_id
        self.next_order_id += 1
        return qmt_id

    def cancel_order_stock(self, account: Any, order_id: int) -> int:
        self.cancel_calls.append((account, order_id))
        return 0

    def query_account_status(self) -> list:
        class _St:
            status: int = 0
        return [_St()]

    def query_stock_asset(self, account: Any) -> Any:
        class _Asset:
            cash: float = 1_000_000.0
            frozen_cash: float = 0.0
            market_value: float = 0.0
            total_asset: float = 1_000_000.0
        return _Asset()

    def query_stock_positions(self, account: Any) -> list:
        return []


class TestQmtBrokerIdempotency:
    """任务43：QMT 端按订单 ID 去重（防频繁报撤单）"""

    def _make_live_broker(self, monkeypatch) -> tuple:
        broker = QmtBroker(
            qmt_path="D:\\fake\\qmt\\userdata_mini",
            account_id="1000001",
            mode="LIVE",
            initial_capital=1_000_000.0,
        )
        fake = _FakeXtQuantTrader()

        def _make_trader_cls():
            class _Cls(_FakeXtQuantTrader):
                def __new__(cls, path: str = "", session: int = 0) -> _FakeXtQuantTrader:
                    return fake
            return _Cls

        def _fake_import():
            return (_FakeXtConstant(), _make_trader_cls(),
                    _FakeXtQuantTraderCallback, _FakeStockAccount)

        monkeypatch.setattr(broker, "_import_xtquant", _fake_import)
        return broker, fake

    def test_SIM重复order_id幂等(self) -> None:
        broker = QmtBroker(mode="SIM", initial_capital=1_000_000.0)
        broker.connect()
        order = Order(
            order_id="fixed_order_001",
            symbol="600000.SH", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=100, price=10.0,
        )
        first = broker.submit_order(order)
        second = broker.submit_order(order)  # 同一 order_id 重复提交
        assert second is first                      # 返回已有订单
        assert broker._stats["dedup_skipped"] == 1
        assert broker._stats["total_orders"] == 1   # 不重复注册

    def test_LIVE重复order_id不重复报单(self, monkeypatch) -> None:
        broker, fake = self._make_live_broker(monkeypatch)
        assert broker.connect() is True
        order = Order(
            order_id="fixed_order_live_001",
            symbol="600000.SH", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=100, price=10.0,
        )
        first = broker.submit_order(order)
        assert first.status.value == "SUBMITTED"
        assert len(fake.order_calls) == 1

        second = broker.submit_order(order)  # 桥接重发同一订单
        assert second is first                      # 返回已有订单
        assert len(fake.order_calls) == 1           # QMT 未重复报单
        assert broker._stats["dedup_skipped"] == 1

    def test_LIVE不同order_id正常报单(self, monkeypatch) -> None:
        broker, fake = self._make_live_broker(monkeypatch)
        assert broker.connect() is True
        o1 = Order(order_id="ord_a", symbol="600000.SH", side=OrderSide.BUY,
                   order_type=OrderType.LIMIT, quantity=100, price=10.0)
        o2 = Order(order_id="ord_b", symbol="600000.SH", side=OrderSide.BUY,
                   order_type=OrderType.LIMIT, quantity=100, price=10.0)
        broker.submit_order(o1)
        broker.submit_order(o2)
        assert len(fake.order_calls) == 2
        assert broker._stats.get("dedup_skipped", 0) == 0

    def test_SIM已成交订单重复提交返回终态(self) -> None:
        """已成交订单再收到重复提交 → 返回终态订单，不重复撮合"""
        broker = QmtBroker(mode="SIM", initial_capital=1_000_000.0)
        broker.connect()
        order = Order(order_id="filled_001", symbol="600000.SH", side=OrderSide.BUY,
                      order_type=OrderType.MARKET, quantity=100)
        first = broker.submit_order(order)
        # 行情驱动成交
        broker.update_market_price("600000.SH", 10.0, prev_close=9.5)
        assert first.is_filled
        second = broker.submit_order(order)
        assert second is first
        assert broker._stats["dedup_skipped"] == 1
        assert broker.get_trades("600000.SH")  # 只有一笔成交（不重复撮合）
        assert len(broker.get_trades("600000.SH")) == 1
