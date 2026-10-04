# 文件名：tests/unit/test_abnormal_trade_rules.py
"""
test_abnormal_trade_rules.py - 异常交易类风控测试（KP-P3-06 / T-19）
========================================================================
覆盖"异常交易类风控整体缺失"修复：
  1. SelfTradeRule —— 同标的反向未成交单互斥拒绝。
  2. CancelRateRule —— 撤单率超阈值拒绝。
  3. FatFingerRule —— 单笔金额超限/价格偏离拒绝。
  4. RULE_REGISTRY 已注册三条规则。
  5. RiskManager 撤单计数 + 日报 + 日内新单计数 + 重置。

依赖: pytest, myquant.risk.rules, myquant.risk.risk_manager,
      myquant.models.trade
"""

from __future__ import annotations

import pytest

from myquant.models.trade import Order, OrderSide, OrderType
from myquant.risk.risk_manager import RiskEventType, RiskManager
from myquant.risk.rules import (
    RULE_REGISTRY,
    CancelRateRule,
    FatFingerRule,
    RiskContext,
    SelfTradeRule,
)


def _order(side: OrderSide = OrderSide.BUY, price: float = 10.0, quantity: float = 100,
           symbol: str = "000001.SZ") -> Order:
    return Order(
        symbol=symbol,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=price,
    )


class TestSelfTradeRule:
    def test_同标反向未成交单拒绝(self) -> None:
        """DoD：pending_orders 含同标的 SELL 未成交 → BUY 拒绝。"""
        rule = SelfTradeRule()
        ctx = RiskContext(pending_orders=[{"symbol": "000001.SZ", "side": OrderSide.SELL, "order_id": "o1"}])
        assert rule.check(_order(OrderSide.BUY), ctx).passed is False

    def test_同向未成交单放行(self) -> None:
        """DoD：pending_orders 含同标的 BUY 未成交 → BUY 放行（非互斥）。"""
        rule = SelfTradeRule()
        ctx = RiskContext(pending_orders=[{"symbol": "000001.SZ", "side": OrderSide.BUY, "order_id": "o1"}])
        assert rule.check(_order(OrderSide.BUY), ctx).passed is True

    def test_无未成交单放行(self) -> None:
        """DoD：无 pending_orders → 放行。"""
        rule = SelfTradeRule()
        assert rule.check(_order(OrderSide.BUY), RiskContext()).passed is True


class TestCancelRateRule:
    def test_撤单率超阈拒绝(self) -> None:
        """DoD：cancel/(new+cancel)=6/10=0.6 > 0.5 → 拒绝。"""
        rule = CancelRateRule(max_cancel_rate=0.5)
        ctx = RiskContext(cancel_count_today=6, new_order_count_today=4)
        assert rule.check(_order(), ctx).passed is False

    def test_撤单率低于阈放行(self) -> None:
        """DoD：cancel=3, new=7 → rate=0.3 < 0.5 → 放行。"""
        rule = CancelRateRule(max_cancel_rate=0.5)
        ctx = RiskContext(cancel_count_today=3, new_order_count_today=7)
        assert rule.check(_order(), ctx).passed is True


class TestFatFingerRule:
    def test_单笔金额超限拒绝(self) -> None:
        """DoD：100万股×10元=1千万 > 100万上限 → 拒绝。"""
        rule = FatFingerRule(max_order_amount=1_000_000, max_price_deviation_pct=0.20)
        ctx = RiskContext(current_price={"000001.SZ": 10.0})
        order = _order(price=10.0, quantity=1_000_000)
        assert rule.check(order, ctx).passed is False

    def test_价格偏离超限拒绝(self) -> None:
        """DoD：现价10，申报15（偏离50%>20%）→ 拒绝。"""
        rule = FatFingerRule(max_order_amount=1_000_000, max_price_deviation_pct=0.20)
        ctx = RiskContext(current_price={"000001.SZ": 10.0})
        order = _order(price=15.0, quantity=100)
        assert rule.check(order, ctx).passed is False

    def test_正常放行(self) -> None:
        """DoD：金额与价格偏离均在限内 → 放行。"""
        rule = FatFingerRule(max_order_amount=1_000_000, max_price_deviation_pct=0.20)
        ctx = RiskContext(current_price={"000001.SZ": 10.0})
        order = _order(price=10.5, quantity=100)
        assert rule.check(order, ctx).passed is True


class TestRegistry:
    def test_三条规则已注册(self) -> None:
        """DoD：RULE_REGISTRY 含 self_trade/cancel_rate/fat_finger。"""
        assert "self_trade" in RULE_REGISTRY
        assert "cancel_rate" in RULE_REGISTRY
        assert "fat_finger" in RULE_REGISTRY
        assert RULE_REGISTRY["self_trade"] is SelfTradeRule
        assert RULE_REGISTRY["cancel_rate"] is CancelRateRule
        assert RULE_REGISTRY["fat_finger"] is FatFingerRule


class TestRiskManagerCancelCounting:
    def test_check_order_通过计数新单(self) -> None:
        """DoD：check_order 通过 → new_orders_today 增。"""
        rm = RiskManager()
        rm.disable_rule("trading_hours")  # 仅聚焦新单计数，避开交易日维度
        result = rm.check_order(
            _order(),
            RiskContext(
                cash=1e6,
                total_assets=1e6,
                current_price={"000001.SZ": 10.0},
                extra={"prev_close": {"000001.SZ": 10.0}},
            ),
        )
        assert result.passed
        assert rm._new_order_count_today == 1
        assert rm.get_daily_report()["new_orders_today"] == 1

    def test_record_cancel_order_计数与事件(self) -> None:
        """DoD：record_cancel_order → cancel 计数 +1 且记录 CANCEL_ORDER 事件。"""
        rm = RiskManager()
        rm.record_cancel_order(_order(symbol="000001.SZ"))
        assert rm._cancel_count_today == 1
        assert rm._stats["cancel_orders_today"] == 1
        events = rm.get_events(event_type=RiskEventType.CANCEL_ORDER)
        assert len(events) == 1
        assert events[0].symbol == "000001.SZ"

    def test_daily_report_撤单率(self) -> None:
        """DoD：日报 cancel_rate 正确计算。"""
        rm = RiskManager()
        rm._new_order_count_today = 7
        rm._cancel_count_today = 3
        rep = rm.get_daily_report()
        assert rep["new_orders_today"] == 7
        assert rep["cancel_orders_today"] == 3
        assert rep["cancel_rate"] == pytest.approx(0.3)

    def test_reset_daily_counts(self) -> None:
        """DoD：reset_daily_counts 清空计数。"""
        rm = RiskManager()
        rm._new_order_count_today = 5
        rm._cancel_count_today = 2
        rm.reset_daily_counts()
        assert rm.get_daily_report()["new_orders_today"] == 0
        assert rm.get_daily_report()["cancel_orders_today"] == 0
