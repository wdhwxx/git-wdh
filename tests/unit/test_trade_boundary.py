# -*- coding: utf-8 -*-
"""
交易模型边界修复回归测试（审计 H3 + H4）
=========================================
H4: Order.fill 防御式校验 —— 非法数量/超额成交（重复回报）拒绝入账
H3: Position.apply_fill —— 仓位反转时以本次成交价重置新方向成本价

回归依据: 审计问题 H3/H4（myquant/models/trade.py 交易模型边界缺陷）
"""
import pytest

from myquant.models.trade import Order, OrderSide, OrderStatus, Position

# ═══════════════════════════════════════════════
# H4: Order.fill 防御式校验
# ═══════════════════════════════════════════════

class TestOrderFillGuard:
    """H4: 订单成交回报边界校验"""

    def test_正常全量成交(self) -> None:
        o = Order(symbol="000001.SZ", quantity=100, price=10.0)
        r = o.fill(qty=100, price=10.0)
        assert r is True
        assert o.status == OrderStatus.FILLED
        assert o.filled_qty == 100
        assert o.filled_price == 10.0

    def test_部分成交后全部成交(self) -> None:
        o = Order(symbol="000001.SZ", quantity=100, price=10.0)
        r1 = o.fill(qty=30, price=10.0)
        assert r1 is False and o.status == OrderStatus.PARTIAL_FILLED
        r2 = o.fill(qty=70, price=11.0)
        assert r2 is True and o.status == OrderStatus.FILLED
        # 加权均价 = (30*10 + 70*11)/100
        assert abs(o.filled_price - 10.7) < 1e-9

    def test_终态订单重复成交拒绝(self) -> None:
        """已全部成交的订单再次收到回报 → 拒绝，账目不更新"""
        o = Order(symbol="000001.SZ", quantity=100, price=10.0)
        o.fill(qty=100, price=10.0)
        r = o.fill(qty=100, price=10.0)
        assert r is False
        assert o.filled_qty == 100  # 未虚增
        assert o.status == OrderStatus.FILLED

    def test_部分成交后超额回报拒绝(self) -> None:
        """部分成交（剩 40）后收到 50 的超额回报 → 拒绝并记录诊断"""
        o = Order(symbol="000001.SZ", quantity=100, price=10.0)
        o.fill(qty=60, price=10.0)
        r = o.fill(qty=50, price=10.0)  # 50 > remaining 40
        assert r is False
        assert o.filled_qty == 60      # 未入账
        assert o.status == OrderStatus.PARTIAL_FILLED
        assert o.extra.get("fill_rejected") is True
        assert "超过剩余可成交量" in o.extra.get("fill_reject_reason", "")

    def test_非法数量拒绝(self) -> None:
        o = Order(symbol="000001.SZ", quantity=100, price=10.0)
        assert o.fill(qty=0, price=10.0) is False
        assert o.filled_qty == 0 and o.extra.get("fill_rejected") is True
        assert o.fill(qty=-5, price=10.0) is False
        assert o.filled_qty == 0

    def test_合法部分成交正常推进(self) -> None:
        """非拒绝路径不受影响：合法部分成交持续累计"""
        o = Order(symbol="000001.SZ", quantity=100, price=10.0)
        o.fill(qty=40, price=10.0)
        r = o.fill(qty=40, price=10.0)  # 剩 60，合法
        assert r is False and o.filled_qty == 80


# ═══════════════════════════════════════════════
# H3: Position.apply_fill 反转成本价
# ═══════════════════════════════════════════════

class TestPositionReverseCost:
    """H3: 仓位反转成本价修复"""

    def test_新建仓(self) -> None:
        p = Position(symbol="000001.SZ")
        p.apply_fill(qty=100, price=10.0, side=OrderSide.BUY)
        assert p.quantity == 100 and p.avg_cost == 10.0

    def test_加仓均价(self) -> None:
        p = Position(symbol="000001.SZ")
        p.apply_fill(qty=100, price=10.0, side=OrderSide.BUY)
        p.apply_fill(qty=100, price=20.0, side=OrderSide.BUY)
        assert p.quantity == 200 and abs(p.avg_cost - 15.0) < 1e-9

    def test_减仓未反转(self) -> None:
        p = Position(symbol="000001.SZ", quantity=100, avg_cost=10.0)
        p.apply_fill(qty=50, price=30.0, side=OrderSide.SELL)
        assert p.quantity == 50 and p.avg_cost == 10.0
        assert abs(p.realized_pnl - (30 - 10) * 50) < 1e-9

    def test_完全平仓归零(self) -> None:
        p = Position(symbol="000001.SZ", quantity=100, avg_cost=10.0)
        p.apply_fill(qty=100, price=30.0, side=OrderSide.SELL)
        assert p.quantity == 0 and p.avg_cost == 0.0

    def test_多头反转空头成本价(self) -> None:
        """核心修复：多 100 超卖 150 → 空 -50，成本=本次成交价"""
        p = Position(symbol="000001.SZ", quantity=100, avg_cost=10.0)
        p.apply_fill(qty=150, price=12.0, side=OrderSide.SELL)
        assert p.quantity == -50
        assert p.avg_cost == 12.0  # 修复前错误保留旧多头成本 10.0
        assert abs(p.realized_pnl - (12 - 10) * 100) < 1e-9

    def test_空头反转多头成本价(self) -> None:
        """核心修复：空 -100 反买 150 → 多 +50，成本=本次成交价"""
        p = Position(symbol="000001.SZ", quantity=-100, avg_cost=8.0)
        p.apply_fill(qty=150, price=9.0, side=OrderSide.BUY)
        assert p.quantity == 50
        assert p.avg_cost == 9.0  # 修复前错误保留旧空头成本 8.0
        assert abs(p.realized_pnl - (8 - 9) * 100) < 1e-9

    def test_反转空头浮盈方向(self) -> None:
        """反转后空头盈亏按新成本价计算（市场价 < 成本 → 浮盈为正）"""
        p = Position(symbol="000001.SZ", quantity=-50, avg_cost=12.0)
        p.update_market_price(11.0)
        assert abs(p.unrealized_pnl - 50.0) < 1e-9

    def test_非法数量抛错(self) -> None:
        p = Position(symbol="000001.SZ")
        with pytest.raises(ValueError):
            p.apply_fill(qty=0, price=10.0, side=OrderSide.BUY)
        with pytest.raises(ValueError):
            p.apply_fill(qty=-5, price=10.0, side=OrderSide.BUY)
