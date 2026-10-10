# 文件名：tests/unit/test_t1_settle.py
"""
test_t1_settle.py - T+1 日终结算接线测试（KP-P3-01 / T-01）
============================================================
覆盖"T+1 冻结只增不减，日终结算无生产调用"修复：
  1. BrokerManager.settle_t1_all —— 遍历全部注册券商逐个调用 settle_t1，
     无实现券商标记 skipped，单券商异常不阻断其余。
  2. T+1 日切闭环（DoD）—— T 日买入冻结 → 当日卖单被拒（可卖数量不足）→
     日切结算 → 结算后卖单通过。
  3. attach_engine 日终 cron —— broker 有 settle_t1_all/settle_t1 时注册
     "T+1日切结算" cron 任务；两者皆无则不注册。
  4. engine 启动解冻 —— start() 对存量持仓做一次 settle_t1_all 解冻初始化。

依赖: pytest, myquant.live.brokers.manager（BrokerManager）
      myquant.live.brokers.paper_broker（PaperBroker）
      myquant.live.brokers.base_broker（BaseBroker/BrokerType）
      myquant.live.scheduler（attach_engine/LiveScheduler）
      myquant.live.engine（LiveEngine/RiskConfig）
      myquant.live.portfolio（LivePortfolio）
      myquant.live.state_store（StateStore）
      myquant.strategies.ma_strategy（MACrossStrategy）
      myquant.models.trade（Order/OrderSide/OrderType/OrderStatus）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from myquant.live.brokers.base_broker import BaseBroker, BrokerType
from myquant.live.brokers.manager import BrokerManager
from myquant.live.brokers.paper_broker import PaperBroker
from myquant.live.engine import LiveEngine, RiskConfig
from myquant.live.portfolio import LivePortfolio
from myquant.live.scheduler import LiveScheduler, attach_engine
from myquant.live.state_store import StateStore
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType
from myquant.strategies.ma_strategy import MACrossStrategy

# ═══════════════════════════════════════════════
#  测试辅助
# ═══════════════════════════════════════════════

SYMBOL = "000001.SZ"


class _NoSettleBroker(BaseBroker):
    """无 settle_t1 实现的测试券商（验证 settle_t1_all 标记 skipped）"""

    broker_name = "nosettle"
    broker_type = BrokerType.CUSTOM

    def __init__(self) -> None:
        super().__init__(name="nosettle")

    def _submit_order_impl(self, order: Order) -> Order:
        return order

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_order(self, order_id: str) -> Optional[Order]:
        return None

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        return []

    def get_account(self) -> Dict[str, Any]:
        return {"cash": 0.0, "available_cash": 0.0, "frozen_cash": 0.0,
                "market_value": 0.0, "total_assets": 0.0}

    def get_positions(self) -> List[Any]:
        return []


class _RecordingManager(BrokerManager):
    """记录 settle_t1_all 调用次数的 BrokerManager（引擎启动解冻测试用）"""

    def __init__(self) -> None:
        super().__init__()
        self.settle_calls: int = 0

    def settle_t1_all(self) -> Dict[str, Any]:
        self.settle_calls += 1
        return super().settle_t1_all()


def _make_manager() -> BrokerManager:
    """注册一个已连接的 PaperBroker 主券商的 BrokerManager"""
    mgr = BrokerManager()
    mgr.register_broker(
        PaperBroker(initial_capital=1_000_000.0), alias="paper", role="primary"
    )
    mgr.get_broker("paper").connect()
    return mgr


def _make_startable_engine(tmp_path, manager: BrokerManager) -> LiveEngine:
    """最小可启动引擎（复用 test_live_guard_failover 装配模式，swap 进 broker_manager）"""
    engine = LiveEngine(
        risk_config=RiskConfig(),
        state_store=StateStore(root=str(tmp_path), store_id="t1_settle_test"),
        enable_warmup=False,
    )
    engine.set_broker_manager(manager)
    engine.set_portfolio(LivePortfolio(initial_capital=1_000_000.0))
    engine.add_strategy(MACrossStrategy())
    return engine


# ═══════════════════════════════════════════════
#  1. BrokerManager.settle_t1_all
# ═══════════════════════════════════════════════

class TestSettleT1All:
    """KP-P3-01：settle_t1_all 遍历各券商结算"""

    def test_结算已注册券商并返回settled(self) -> None:
        mgr = _make_manager()
        res = mgr.settle_t1_all()
        assert res["settled"] == ["paper"]
        assert res["skipped"] == []
        assert res["errors"] == {}

    def test_无settle_t1实现券商标记skipped(self) -> None:
        mgr = _make_manager()
        mgr.register_broker(_NoSettleBroker(), alias="nosettle", role="secondary")
        res = mgr.settle_t1_all()
        assert "paper" in res["settled"]
        assert "nosettle" in res["skipped"]
        assert res["errors"] == {}

    def test_空管理器零动作(self) -> None:
        mgr = BrokerManager()
        res = mgr.settle_t1_all()
        assert res["settled"] == []
        assert res["skipped"] == []
        assert res["errors"] == {}


# ═══════════════════════════════════════════════
#  2. T+1 日切闭环（DoD 核心）
# ═══════════════════════════════════════════════

class TestT1Cycle:
    """T 日买入冻结 → 当日卖单被拒 → 日切结算 → 结算后卖单通过"""

    def _buy_fill(self, broker: PaperBroker, qty: int = 100) -> Order:
        broker.update_market_price(SYMBOL, 10.0)
        buy = Order(symbol=SYMBOL, side=OrderSide.BUY,
                    order_type=OrderType.MARKET, quantity=qty)
        broker.submit_order(buy)
        broker.update_market_price(SYMBOL, 10.0)  # 驱动撮合成交
        assert buy.status is OrderStatus.FILLED
        return buy

    def test_T日买入冻结_卖单被拒_结算后可卖(self) -> None:
        mgr = _make_manager()
        broker: PaperBroker = mgr.get_broker("paper")
        self._buy_fill(broker, qty=100)

        # T 日卖出 100 股 → 因 T+1 冻结被拒（可卖数量不足）
        sell = Order(symbol=SYMBOL, side=OrderSide.SELL,
                     order_type=OrderType.MARKET, quantity=100)
        mgr.submit_order(sell)
        assert sell.status is OrderStatus.REJECTED
        assert "可卖数量不足" in sell.extra.get("reject_reason", "")

        # 日切结算：释放当日买入冻结
        res = mgr.settle_t1_all()
        assert res["settled"] == ["paper"]

        # 结算后再次卖出 → 通过并成交
        sell2 = Order(symbol=SYMBOL, side=OrderSide.SELL,
                      order_type=OrderType.MARKET, quantity=100)
        mgr.submit_order(sell2)
        broker.update_market_price(SYMBOL, 10.0)  # 驱动撮合成交
        assert sell2.status is OrderStatus.FILLED

    def test_禁用T1时当日即可卖(self) -> None:
        """对照：enable_t1=False 时当日买入即可卖，无需日切"""
        broker = PaperBroker(initial_capital=1_000_000.0, enable_t1=False)
        broker.connect()
        broker.update_market_price(SYMBOL, 10.0)
        broker.submit_order(Order(symbol=SYMBOL, side=OrderSide.BUY,
                                  order_type=OrderType.MARKET, quantity=100))
        broker.update_market_price(SYMBOL, 10.0)
        sell = Order(symbol=SYMBOL, side=OrderSide.SELL,
                     order_type=OrderType.MARKET, quantity=100)
        broker.submit_order(sell)
        broker.update_market_price(SYMBOL, 10.0)
        assert sell.status is OrderStatus.FILLED


# ═══════════════════════════════════════════════
#  3. attach_engine 日终 cron 接线
# ═══════════════════════════════════════════════

class TestAttachEngineT1Settle:
    """KP-P3-01：attach_engine 注册日终 T+1 日切结算 cron 任务"""

    def test_broker_manager注册T1日切任务(self) -> None:
        sched = LiveScheduler(tick_interval=0.5)
        ids = attach_engine(sched, broker=_make_manager(), enable_patrol=False,
                            enable_daily_report=False)
        assert "t1_settle" in ids
        task = sched.get_task(ids["t1_settle"])
        assert task is not None
        assert task.name == "T+1日切结算"
        assert task.schedule_type == "cron"
        assert task.cron_expr == "30 15 * * 1-5"

    def test_单券商settle_t1也注册(self) -> None:
        sched = LiveScheduler(tick_interval=0.5)
        broker = PaperBroker(initial_capital=1_000_000.0)
        ids = attach_engine(sched, broker=broker, enable_patrol=False,
                            enable_daily_report=False)
        assert "t1_settle" in ids

    def test_无日切实现不注册(self) -> None:
        sched = LiveScheduler(tick_interval=0.5)
        ids = attach_engine(sched, broker=_NoSettleBroker(), enable_patrol=False,
                            enable_daily_report=False)
        assert "t1_settle" not in ids

    def test_显式关闭不注册(self) -> None:
        sched = LiveScheduler(tick_interval=0.5)
        ids = attach_engine(sched, broker=_make_manager(), enable_patrol=False,
                            enable_daily_report=False, enable_t1_settle=False)
        assert "t1_settle" not in ids


# ═══════════════════════════════════════════════
#  4. engine 启动解冻
# ═══════════════════════════════════════════════

class TestEngineStartT1Unfreeze:
    """KP-P3-01：engine.start() 对存量持仓做一次 T+1 解冻初始化"""

    def test_start调用settle_t1_all解冻(self, tmp_path) -> None:
        mgr = _RecordingManager()
        mgr.register_broker(
            PaperBroker(initial_capital=1_000_000.0), alias="paper", role="primary"
        )
        engine = _make_startable_engine(tmp_path, mgr)
        try:
            assert engine.start() is True
            assert mgr.settle_calls >= 1
        finally:
            engine.stop()

    def test_无broker_manager启动不报错(self, tmp_path) -> None:
        """对照：未装配 broker_manager 时 start 因组件缺失返回 False（不抛异常）"""
        engine = LiveEngine(
            risk_config=RiskConfig(),
            state_store=StateStore(root=str(tmp_path), store_id="t1_no_mgr"),
            enable_warmup=False,
        )
        engine.set_portfolio(LivePortfolio(initial_capital=1_000_000.0))
        engine.add_strategy(MACrossStrategy())
        assert engine.start() is False


# ═══════════════════════════════════════════════
#  5. 双交易日集成（DoD：连跑两日，无"可卖数量不足"拒单）
# ═══════════════════════════════════════════════

class TestTwoDaySettlement:
    """两个交易日连续运行：T 日买入冻结，日切结算后 T+1 日可正常卖出"""

    def test_两交易日_无可卖不足拒单(self) -> None:
        mgr = _make_manager()
        broker: PaperBroker = mgr.get_broker("paper")

        # T 日：买入并成交（当日冻结）
        broker.update_market_price(SYMBOL, 10.0)
        buy = Order(symbol=SYMBOL, side=OrderSide.BUY,
                    order_type=OrderType.MARKET, quantity=100)
        mgr.submit_order(buy)
        broker.update_market_price(SYMBOL, 10.0)
        assert buy.status is OrderStatus.FILLED

        # 日终结算（等价调度器 15:30 日切任务）
        mgr.settle_t1_all()

        # T+1 日：卖出成交，无拒单
        broker.update_market_price(SYMBOL, 10.5)
        sell = Order(symbol=SYMBOL, side=OrderSide.SELL,
                     order_type=OrderType.MARKET, quantity=100)
        mgr.submit_order(sell)
        broker.update_market_price(SYMBOL, 10.5)
        assert sell.status is OrderStatus.FILLED, \
            f"T+1 卖出应成交，实际: {sell.status} {sell.extra.get('reject_reason', '')}"
