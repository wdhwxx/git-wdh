# 文件名：tests/unit/test_emergency_liquidation.py
"""
test_emergency_liquidation.py - 紧急一键平仓服务测试（任务47 / P2）
================================================================
覆盖 P2「一键平仓脚本」核心验收：
  1. 默认预览（dry_run）—— 只生成计划，不真实下单
  2. 确认模式真实平仓 —— 逐仓市价全仓卖出（SELL/MARKET/数量正确）
  3. 顺序保证 —— 先撤全部挂单，再卖持仓
  4. 可卖数量 —— 优先取 extra.available_qty（QMT can_use_volume）；
     无可用数量 → 跳过；无 available_qty → 回退全仓
  5. 防御式 —— 连接失败安全返回 / 单标的异常不阻断其余 / 拒单记失败
  6. 指定标的 —— symbol 过滤只平该标的
  7. 空仓 / 报告结构完整

依赖: pytest, myquant.live.emergency_liquidation（EmergencyLiquidationService）
      myquant.models.trade（Order/Position/OrderSide/OrderType/OrderStatus）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from myquant.live.emergency_liquidation import EmergencyLiquidationService
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType, Position


# ═══════════════════════════════════════════════
#  FakeBroker - 可编程平仓测试券商
# ═══════════════════════════════════════════════

class FakeBroker:
    """可编程测试券商（紧急平仓服务用，不继承 BaseBroker 以保持轻量）"""

    broker_name = "fake_liquidation"

    def __init__(
        self,
        connected: bool = True,
        positions: Optional[List[Position]] = None,
        active_orders: Optional[List[Order]] = None,
        submit_behavior: str = "ok",     # ok / reject / raise
        fail_symbol: Optional[str] = None,
        connect_behavior: str = "ok",    # ok / fail
    ) -> None:
        self._connected = bool(connected)
        self._positions = list(positions or [])
        self._active_orders = list(active_orders or [])
        self._submit_behavior = submit_behavior
        self._fail_symbol = fail_symbol
        self._connect_behavior = connect_behavior
        self.submitted: List[Order] = []
        self.cancelled: List[str] = []
        self.connect_calls: int = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        self.connect_calls += 1
        if self._connect_behavior == "fail":
            self._connected = False
        else:
            self._connected = True
        return self._connected

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        if symbol:
            return [o for o in self._active_orders if o.symbol == symbol]
        return list(self._active_orders)

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    def get_positions(self) -> List[Position]:
        return list(self._positions)

    def submit_order(self, order: Order) -> Order:
        self.submitted.append(order)
        if self._fail_symbol and order.symbol == self._fail_symbol:
            raise RuntimeError(f"模拟提交异常: {order.symbol}")
        if self._submit_behavior == "reject":
            order.reject("模拟拒单: 可卖数量不足(T+1冻结)")
            return order
        if self._submit_behavior == "raise":
            raise RuntimeError("模拟提交异常")
        order.submit()
        return order


# ═══════════════════════════════════════════════
#  测试辅助
# ═══════════════════════════════════════════════

def _pos(
    symbol: str = "600519.SH",
    quantity: float = 100,
    price: float = 1500.0,
    available_qty: Optional[float] = None,
) -> Position:
    pos = Position(
        symbol=symbol,
        quantity=quantity,
        avg_cost=1450.0,
        market_price=price,
    )
    if available_qty is not None:
        pos.extra["available_qty"] = available_qty
    return pos


def _active_order(order_id: str = "o1", symbol: str = "600519.SH") -> Order:
    return Order(
        order_id=order_id,
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=10.0,
        strategy_id="test",
    )


def _make_service(broker: FakeBroker) -> EmergencyLiquidationService:
    return EmergencyLiquidationService(broker)


# ═══════════════════════════════════════════════
#  1. 默认预览（dry_run）
# ═══════════════════════════════════════════════

def test_预览模式_不下单只生成计划() -> None:
    broker = FakeBroker(positions=[_pos()])
    report = _make_service(broker).execute()

    assert report["mode"] == "DRY_RUN"
    assert report["ok"] is True
    assert broker.submitted == []  # 未真实下单
    assert report["liquidation"]["total_symbols"] == 1
    assert report["liquidation"]["items"][0]["status"] == "PLANNED"
    assert "未真实下单" in report["liquidation"]["items"][0]["reason"]


def test_预览模式_列出挂单撤销计划() -> None:
    broker = FakeBroker(positions=[_pos()], active_orders=[_active_order("o1")])
    report = _make_service(broker).execute()

    assert len(report["cancelled_orders"]) == 1
    assert report["cancelled_orders"][0]["dry_run"] is True
    assert broker.cancelled == []  # 预览不真实撤单


# ═══════════════════════════════════════════════
#  2. 确认模式真实平仓
# ═══════════════════════════════════════════════

def test_确认模式_全仓市价卖出() -> None:
    broker = FakeBroker(positions=[_pos(quantity=100, price=1500.0)])
    report = _make_service(broker).execute(confirm=True)

    assert report["mode"] == "LIVE_EXEC"
    assert report["ok"] is True
    assert len(broker.submitted) == 1
    sell = broker.submitted[0]
    assert sell.side is OrderSide.SELL
    assert sell.order_type is OrderType.MARKET
    assert sell.quantity == 100
    assert sell.price == 0.0  # 市价单：不假设成交价
    assert sell.strategy_id == "emergency_liquidation"
    assert report["liquidation"]["success"] == 1
    assert report["liquidation"]["failed"] == 0
    assert report["liquidation"]["total_quantity"] == 100
    assert report["liquidation"]["estimated_value"] == pytest.approx(150_000.0)


# ═══════════════════════════════════════════════
#  3. 顺序保证：先撤挂单再卖
# ═══════════════════════════════════════════════

def test_先撤挂单再卖出() -> None:
    broker = FakeBroker(
        positions=[_pos()],
        active_orders=[_active_order("o1"), _active_order("o2", "000001.SZ")],
    )
    report = _make_service(broker).execute(confirm=True)

    assert set(broker.cancelled) == {"o1", "o2"}
    assert len(broker.submitted) == 1  # 撤单完成后才卖出
    assert report["ok"] is True


# ═══════════════════════════════════════════════
#  4. 可卖数量
# ═══════════════════════════════════════════════

def test_优先使用可卖数量() -> None:
    """QMT 映射的 extra.available_qty（can_use_volume）优先于全仓"""
    broker = FakeBroker(positions=[_pos(quantity=100, available_qty=40)])
    report = _make_service(broker).execute(confirm=True)

    assert broker.submitted[0].quantity == 40
    assert report["liquidation"]["total_quantity"] == 40


def test_可卖数量为零_跳过() -> None:
    broker = FakeBroker(positions=[_pos(quantity=100, available_qty=0)])
    report = _make_service(broker).execute(confirm=True)

    assert report["liquidation"]["skipped"] == 1
    assert broker.submitted == []
    assert report["liquidation"]["items"][0]["status"] == "SKIPPED"


def test_无可卖字段_回退全仓() -> None:
    """Position 无 extra.available_qty → 按全仓卖出（券商侧 T+1 校验兜底）"""
    broker = FakeBroker(positions=[_pos(quantity=100)])
    report = _make_service(broker).execute(confirm=True)

    assert broker.submitted[0].quantity == 100


# ═══════════════════════════════════════════════
#  5. 防御式
# ═══════════════════════════════════════════════

def test_连接失败_安全返回不抛异常() -> None:
    broker = FakeBroker(connected=False, connect_behavior="fail", positions=[_pos()])
    report = _make_service(broker).execute(confirm=True)

    assert report["ok"] is False
    assert report["connect"]["ok"] is False
    assert report["connect"]["error"] != ""
    assert report["errors"]
    assert broker.submitted == []


def test_连接后恢复_正常平仓() -> None:
    broker = FakeBroker(connected=False, positions=[_pos()])
    report = _make_service(broker).execute(confirm=True)

    assert broker.connect_calls == 1
    assert report["ok"] is True
    assert len(broker.submitted) == 1


def test_单标异常_不阻断其余() -> None:
    broker = FakeBroker(
        positions=[_pos("600519.SH"), _pos("000001.SZ", quantity=200, price=20.0)],
        fail_symbol="000001.SZ",
    )
    report = _make_service(broker).execute(confirm=True)

    assert report["liquidation"]["success"] == 1
    assert report["liquidation"]["failed"] == 1
    assert any("000001.SZ" in e for e in report["errors"])
    assert broker.submitted[0].symbol == "600519.SH"


def test_拒单记录失败() -> None:
    broker = FakeBroker(positions=[_pos()], submit_behavior="reject")
    report = _make_service(broker).execute(confirm=True)

    assert report["liquidation"]["failed"] == 1
    assert report["liquidation"]["success"] == 0
    assert report["ok"] is False
    item = report["liquidation"]["items"][0]
    assert item["status"] == "REJECTED"
    assert "拒单" in item["reason"]


# ═══════════════════════════════════════════════
#  6. 指定标的
# ═══════════════════════════════════════════════

def test_指定标的_只平该标的() -> None:
    broker = FakeBroker(
        positions=[_pos("600519.SH"), _pos("000001.SZ", quantity=200, price=20.0)]
    )
    report = _make_service(broker).execute(confirm=True, symbol="600519.SH")

    assert report["liquidation"]["total_symbols"] == 1
    assert report["liquidation"]["items"][0]["symbol"] == "600519.SH"
    assert len(broker.submitted) == 1


def test_指定标的_无匹配持仓() -> None:
    broker = FakeBroker(positions=[_pos("600519.SH")])
    report = _make_service(broker).execute(confirm=True, symbol="000001.SZ")

    assert report["liquidation"]["total_symbols"] == 0
    assert report["ok"] is True  # 空仓视为平仓成功


# ═══════════════════════════════════════════════
#  7. 空仓与报告结构
# ═══════════════════════════════════════════════

def test_空仓_平仓成功() -> None:
    broker = FakeBroker(positions=[])
    report = _make_service(broker).execute(confirm=True)

    assert report["ok"] is True
    assert report["liquidation"]["total_symbols"] == 0
    assert report["liquidation"]["success"] == 0


def test_报告结构完整() -> None:
    broker = FakeBroker(positions=[_pos()])
    report = _make_service(broker).execute()

    assert set(report.keys()) >= {
        "ok", "mode", "started_at", "finished_at", "elapsed_ms",
        "connect", "cancelled_orders", "liquidation", "errors",
    }
    assert set(report["liquidation"].keys()) >= {
        "total_symbols", "success", "failed", "skipped",
        "total_quantity", "estimated_value", "items",
    }
    assert report["finished_at"] >= report["started_at"]
