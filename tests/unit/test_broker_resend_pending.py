# 文件名：tests/unit/test_broker_resend_pending.py
"""
test_broker_resend_pending.py - 断线补单对账测试（任务45 / T3）
================================================================
覆盖 M1 断线补单机制核心验收（T3：QMT 断线→重连后仅补发未成交订单，
已成交零重发，无重复报单）：
  1. 重连后仅补发未成交订单 —— 本地 SUBMITTED + 券商端无此单 → 补发，
     使用同一 order_id 的 PENDING 干净副本（幂等键不变）。
  2. 已成交/已撤/废单/部成 零重发 —— 券商端快照存在终态或部成痕迹 →
     绝不补发（terminal 计数，本地状态待回报同步）。
  3. 券商端在途不补 —— 快照存在 SUBMITTED → in_flight 计数，不补。
  4. 查询失败不补发 —— query_broker_orders 返回 None → 保守跳过
     （宁可漏单不重复报单，防补单风暴）。
  5. 券商未连接跳过 —— 本轮不补，重连后再触发。
  6. 幂等去重 —— 补发走 submit_order，券商端重复 order_id 直接返回
     已有订单，不生成新委托（dedup 命中计数验证）。
  7. 统计可观测 —— checked/in_flight/terminal/resent/failed/skipped
     与 per_broker 逐券商统计；get_summary 暴露 last_resend。
  8. 引擎委托 —— LiveEngine.resend_pending_orders() 转发 BrokerManager；
     无 broker_manager 时返回错误码不抛出。

依赖: pytest, myquant.live.brokers.manager（BrokerManager）
      myquant.live.brokers.base_broker（BaseBroker/BrokerType）
      myquant.live.engine（LiveEngine/RiskConfig）
      myquant.models.trade（Order/OrderSide/OrderType/OrderStatus）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from myquant.live.brokers.base_broker import BaseBroker, BrokerType
from myquant.live.brokers.manager import BrokerManager
from myquant.live.engine import LiveEngine, RiskConfig
from myquant.live.portfolio import LivePortfolio
from myquant.live.state_store import StateStore
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType


# ═══════════════════════════════════════════════
#  FakeBroker - 可编程测试券商（带券商端委托簿）
# ═══════════════════════════════════════════════

class FakeBroker(BaseBroker):
    """
    可编程测试券商（继承 BaseBroker，任务45 补单对账用）

    - 本地订单簿复用基类 self._orders（get_orders 由基类提供）
    - submit_order 具备幂等去重（重复 order_id 返回已有订单，dedup_hits+1）
    - 券商端当日委托快照由 _remote_orders 注入（dict 列表）
    - query_broker_orders 行为由 query_mode 控制:
        "ok"    → 返回 _remote_orders 快照
        "fail"  → 返回 None（查询失败：调用方不补发）
        "raise" → 抛 RuntimeError（异常：调用方按跳过处理）
    """

    broker_name = "fake_broker"
    broker_type = BrokerType.CUSTOM

    def __init__(
        self,
        name: str = "fake",
        connected: bool = True,
        remote_orders: Optional[List[Dict[str, Any]]] = None,
        query_mode: str = "ok",
    ) -> None:
        super().__init__(name=name)
        self._fake_connected = bool(connected)
        self._remote_orders = [dict(o) for o in (remote_orders or [])]
        self._query_mode = query_mode
        self.submit_calls: List[Order] = []   # 券商收到的全部委托（含补发副本）
        self.dedup_hits: int = 0              # 幂等命中次数（重复 order_id）

    # ── 连接状态 ──
    @property
    def is_connected(self) -> bool:
        return self._fake_connected

    def connect(self) -> bool:
        self._fake_connected = True
        return True

    def disconnect(self) -> None:
        self._fake_connected = False

    # ── 券商端当日委托快照（对账数据源）──
    def query_broker_orders(
        self, symbol: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        if self._query_mode == "fail":
            return None
        if self._query_mode == "raise":
            raise RuntimeError("模拟查询异常")
        return [dict(o) for o in self._remote_orders]

    # ── 下单（幂等去重 + 状态机）──
    def _submit_order_impl(self, order: Order) -> Order:
        self.submit_calls.append(order)
        existing = self._orders.get(order.order_id)
        if existing is not None:
            self.dedup_hits += 1
            return existing
        if order.status is not OrderStatus.PENDING:
            order.reject("订单状态无法提交")
            self._orders[order.order_id] = order
            return order
        order.submit()
        self._orders[order.order_id] = order
        return order

    # ── 断线补单强制重发（任务45：跳过幂等，模拟委托送达券商端）──
    def resubmit_order(self, order: Order) -> Order:
        self.submit_calls.append(order)
        if order.status is not OrderStatus.PENDING:
            order.reject("订单状态无法提交")
            self._orders[order.order_id] = order
            return order
        order.submit()
        self._orders[order.order_id] = order
        # 模拟真实券商：补发送达后券商端委托队列出现该单（下次对账可查到）
        self._remote_orders = [
            o for o in self._remote_orders
            if str(o.get("order_id") or "") != str(order.order_id)[:24]
        ]
        self._remote_orders.append(_remote(order.order_id, OrderStatus.SUBMITTED))
        return order

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        return [o for o in self._orders.values() if o.status.is_active]

    def get_account(self) -> Dict[str, Any]:
        return {"cash": 1_000_000.0, "available_cash": 1_000_000.0}

    def get_positions(self) -> List[Any]:
        return []

    def get_trades(self, symbol: Optional[str] = None) -> List[Any]:
        return []

    def health_check(self) -> Dict[str, Any]:
        return {
            "broker_name": self.broker_name,
            "broker_type": str(self.broker_type),
            "connected": self._fake_connected,
            "status": "healthy" if self._fake_connected else "down",
        }


# ═══════════════════════════════════════════════
#  测试辅助
# ═══════════════════════════════════════════════

def _make_order(order_id: str = "sig_resend_0001") -> Order:
    return Order(
        order_id=order_id,
        symbol="600519.SH",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=1500.0,
        strategy_id="test_strategy",
    )


def _remote(order_id: str, status: OrderStatus, qmt_id: int = 100) -> Dict[str, Any]:
    """构造券商端委托快照条目（order_id 与本地前 24 位一致）"""
    return {
        "order_id": order_id[:24],
        "qmt_order_id": qmt_id,
        "status": status,
        "symbol": "600519.SH",
        "quantity": 100.0,
    }


def _submit_and_manage(broker: FakeBroker, order: Order) -> BrokerManager:
    """本地提交订单并注册到单券商管理器（主券商）"""
    broker.submit_order(order)
    mgr = BrokerManager()
    mgr.register_broker(broker, alias="qmt", role="primary")
    mgr.connect_all()
    return mgr


# ═══════════════════════════════════════════════
#  1. 仅补发未成交订单
# ═══════════════════════════════════════════════

def test_重连后仅补发未成交订单() -> None:
    broker = FakeBroker(name="qmt", remote_orders=[])  # 券商端无任何委托
    order = _make_order()
    mgr = _submit_and_manage(broker, order)
    assert order.status is OrderStatus.SUBMITTED

    result = mgr.resend_pending_orders()

    assert result["resent"] == 1
    assert result["failed"] == 0
    assert result["checked"] == 1
    assert result["per_broker"]["qmt"]["resent"] == 1
    # 补发用同一 order_id 的 PENDING 干净副本（提交后流转为 SUBMITTED）
    resent_order = broker.submit_calls[-1]
    assert resent_order.order_id == order.order_id
    assert resent_order.status is OrderStatus.SUBMITTED
    assert resent_order.extra.get("resend") is True
    # 券商端最终持有该订单（SUBMITTED）
    assert broker.get_order(order.order_id) is not None


def test_多笔未成交订单全部补发() -> None:
    broker = FakeBroker(name="qmt", remote_orders=[])
    orders = [_make_order(f"sig_resend_{i:04d}") for i in range(1, 4)]
    for o in orders:
        broker.submit_order(o)
    mgr = BrokerManager()
    mgr.register_broker(broker, alias="qmt", role="primary")

    result = mgr.resend_pending_orders()

    assert result["resent"] == 3
    assert result["checked"] == 3
    assert len(broker.submit_calls) == 6  # 3 次原始委托 + 3 次补发


# ═══════════════════════════════════════════════
#  2. 已成交/已撤/废单/部成 零重发
# ═══════════════════════════════════════════════

@pytest.mark.parametrize(
    "remote_status",
    [
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.PARTIAL_FILLED,
    ],
)
def test_券商端已有终态痕迹_绝不重发(remote_status: OrderStatus) -> None:
    order_id = "sig_resend_filled01"
    broker = FakeBroker(
        name="qmt",
        remote_orders=[_remote(order_id, remote_status, qmt_id=200)],
    )
    order = _make_order(order_id)
    mgr = _submit_and_manage(broker, order)

    result = mgr.resend_pending_orders()

    assert result["terminal"] == 1
    assert result["resent"] == 0
    # 未产生任何新委托（原始提交后 submit_calls 不再增长）
    assert len(broker.submit_calls) == 1


def test_已成交订单不重发_本地状态不伪造() -> None:
    """券商端 FILLED → 不补发；本地订单保持 SUBMITTED（等待回报同步），不伪造成交"""
    order_id = "sig_resend_filled02"
    broker = FakeBroker(
        name="qmt",
        remote_orders=[_remote(order_id, OrderStatus.FILLED, qmt_id=201)],
    )
    order = _make_order(order_id)
    mgr = _submit_and_manage(broker, order)

    result = mgr.resend_pending_orders()

    assert result["resent"] == 0
    assert result["terminal"] == 1
    assert order.status is OrderStatus.SUBMITTED  # 不伪造状态


# ═══════════════════════════════════════════════
#  3. 券商端在途不补
# ═══════════════════════════════════════════════

def test_券商端在途不补发() -> None:
    order_id = "sig_resend_inflight1"
    broker = FakeBroker(
        name="qmt",
        remote_orders=[_remote(order_id, OrderStatus.SUBMITTED, qmt_id=300)],
    )
    order = _make_order(order_id)
    mgr = _submit_and_manage(broker, order)

    result = mgr.resend_pending_orders()

    assert result["in_flight"] == 1
    assert result["resent"] == 0
    assert len(broker.submit_calls) == 1


# ═══════════════════════════════════════════════
#  4. 查询失败不补发（保守）
# ═══════════════════════════════════════════════

def test_券商端查询失败_不补发() -> None:
    broker = FakeBroker(name="qmt", query_mode="fail")  # 查询返回 None
    order = _make_order()
    mgr = _submit_and_manage(broker, order)

    result = mgr.resend_pending_orders()

    assert result["skipped"] == 1
    assert result["resent"] == 0
    assert len(broker.submit_calls) == 1  # 无补发委托


def test_券商端查询异常_按跳过处理不补发() -> None:
    broker = FakeBroker(name="qmt", query_mode="raise")
    order = _make_order()
    mgr = _submit_and_manage(broker, order)

    result = mgr.resend_pending_orders()

    assert result["skipped"] == 1
    assert result["resent"] == 0


# ═══════════════════════════════════════════════
#  5. 未连接跳过
# ═══════════════════════════════════════════════

def test_券商未连接_跳过不补发() -> None:
    broker = FakeBroker(name="qmt", connected=False, remote_orders=[])
    order = _make_order()
    # 未连接时本地订单仍可能在簿（断线前已提交）；直接走对账
    broker._orders[order.order_id] = order
    order.submit()
    mgr = BrokerManager()
    mgr.register_broker(broker, alias="qmt", role="primary")

    result = mgr.resend_pending_orders()

    assert result["skipped"] == 1
    assert result["resent"] == 0


# ═══════════════════════════════════════════════
#  6. 幂等去重（补发不重复报单）
# ═══════════════════════════════════════════════

def test_重复补发_不产生重复报单() -> None:
    """网络波动导致对账重复触发：第一次补发真正委托；
    第二次对账时券商端已能看到该单 → 在途不补，无重复报单"""
    broker = FakeBroker(name="qmt", remote_orders=[])
    order = _make_order()
    mgr = _submit_and_manage(broker, order)

    first = mgr.resend_pending_orders()
    assert first["resent"] == 1
    assert len(broker.submit_calls) == 2  # 原始委托 + 补发委托

    # 第二次对账：券商端已存在该单（在途）→ 不补发
    second = mgr.resend_pending_orders()
    assert second["resent"] == 0
    assert second["in_flight"] == 1
    assert len(broker.submit_calls) == 2  # 无新增委托 → 无重复报单
    assert broker.dedup_hits == 0  # 补发走 resubmit 通道，不经过 submit 幂等


# ═══════════════════════════════════════════════
#  7. 统计与 per_broker
# ═══════════════════════════════════════════════

def test_多券商独立对账_统计合并() -> None:
    order_id = "sig_resend_multi01"
    # 主券商：券商端无此单 → 补发；备券商：券商端已成交 → 不补
    primary = FakeBroker(name="qmt", remote_orders=[])
    backup = FakeBroker(
        name="ptrade",
        remote_orders=[_remote(order_id, OrderStatus.FILLED, qmt_id=400)],
    )
    # 双券商并行：各自持有独立订单对象（同 order_id，避免共享状态被互相改写）
    primary.submit_order(_make_order(order_id))
    backup.submit_order(_make_order(order_id))
    mgr = BrokerManager()
    mgr.register_broker(primary, alias="qmt", role="primary")
    mgr.register_broker(backup, alias="ptrade", role="secondary")

    result = mgr.resend_pending_orders()

    assert result["resent"] == 1
    assert result["terminal"] == 1
    assert result["checked"] == 2
    assert result["per_broker"]["qmt"]["resent"] == 1
    assert result["per_broker"]["ptrade"]["terminal"] == 1
    # get_summary 暴露最近一次对账结果
    assert mgr.get_summary()["last_resend"]["checked"] == 2


def test_空订单簿_对账零动作() -> None:
    broker = FakeBroker(name="qmt", remote_orders=[])
    mgr = BrokerManager()
    mgr.register_broker(broker, alias="qmt", role="primary")

    result = mgr.resend_pending_orders()

    assert result["checked"] == 0
    assert result["resent"] == 0
    assert result["per_broker"]["qmt"]["checked"] == 0


# ═══════════════════════════════════════════════
#  8. 引擎委托方法
# ═══════════════════════════════════════════════

def _make_engine(tmp_path, broker: FakeBroker) -> LiveEngine:
    engine = LiveEngine(
        risk_config=RiskConfig(),
        state_store=StateStore(root=str(tmp_path), store_id="resend_test"),
    )
    engine.set_broker(broker)
    engine.set_portfolio(LivePortfolio(initial_capital=1_000_000.0))
    return engine


def test_引擎断线补单委托(tmp_path) -> None:
    broker = FakeBroker(name="qmt", remote_orders=[])
    order = _make_order()
    broker.submit_order(order)
    engine = _make_engine(tmp_path, broker)

    result = engine.resend_pending_orders()

    assert result["resent"] == 1
    # 引擎 set_broker 包装的单券商 manager，alias = broker.broker_name
    assert result["per_broker"]["fake_broker"]["resent"] == 1


def test_引擎无broker_manager_返回错误码(tmp_path) -> None:
    engine = LiveEngine(
        risk_config=RiskConfig(),
        state_store=StateStore(root=str(tmp_path), store_id="resend_test_none"),
    )
    result = engine.resend_pending_orders()

    assert result == {"error": "no_broker_manager"}


def test_clone_order_deep_copies_extra_nested() -> None:
    """P1-18：补发副本 extra 必须真深拷贝，改嵌套对象不得污染原单。"""
    original = Order(
        order_id="X1", symbol="600519.SH", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=100, price=1500.0,
        strategy_id="s", extra={"nested": {"a": 1}},
    )
    retry = BrokerManager._clone_order_for_retry(original)
    retry.extra["nested"]["a"] = 999
    assert original.extra["nested"]["a"] == 1, "副本嵌套对象污染了原单"
    assert retry.status == OrderStatus.PENDING