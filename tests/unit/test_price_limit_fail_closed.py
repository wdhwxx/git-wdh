# 文件名：tests/unit/test_price_limit_fail_closed.py
"""
test_price_limit_fail_closed.py - 涨跌停硬拦截 fail-closed 测试（KP-P3-04 / T-12）
======================================================================
覆盖"涨跌停事前硬拦截恒失效（fail-open）"修复：
  1. PriceLimitRule 缺 prev_close → 拒绝（fail-closed，DoD）。
  2. 有 prev_close → 涨停拒买 / 跌停拒卖 / 正常放行。
  3. engine._build_risk_context 从 prev_close_map 注入 prev_close（DoD）。
  （qmt_broker._check_price_limit 缺昨收 return False 已在 test_qmt_prev_close 覆盖）

依赖: pytest, myquant.risk.rules, myquant.live.engine, myquant.models.trade
"""

from __future__ import annotations

from myquant.live.engine_assembly import (
    create_live_engine,  # 回写#422：装配层迁出，调用点直连
)
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType
from myquant.risk.rules import PriceLimitRule, RiskContext

SYMBOL = "000001.SZ"


def _order(side: OrderSide, symbol: str = SYMBOL, price: float = 10.0) -> Order:
    return Order(symbol=symbol, side=side, order_type=OrderType.MARKET,
                 quantity=100.0, price=price, status=OrderStatus.SUBMITTED)


def _ctx(current_price: float, prev_close: float) -> RiskContext:
    return RiskContext(
        current_price={SYMBOL: current_price},
        extra={"prev_close": {SYMBOL: prev_close}},
    )


# ═══════════════════════════════════════════════
#  1/2. PriceLimitRule fail-closed
# ═══════════════════════════════════════════════

class TestPriceLimitFailClosed:
    def test_缺prev_close_拒绝(self) -> None:
        """DoD：current_price 或 prev_close 缺失 → reject（不再跳过放行）。"""
        rule = PriceLimitRule(limit_pct=0.10)
        res = rule.check(_order(OrderSide.BUY), _ctx(10.0, 0.0))
        assert not res.passed
        assert "fail-closed" in res.reason
        res2 = rule.check(_order(OrderSide.BUY), _ctx(0.0, 10.0))
        assert not res2.passed

    def test_涨停拒买(self) -> None:
        rule = PriceLimitRule(limit_pct=0.10)
        res = rule.check(_order(OrderSide.BUY), _ctx(11.0, 10.0))
        assert not res.passed
        assert "涨停不能买入" in res.reason

    def test_跌停拒卖(self) -> None:
        rule = PriceLimitRule(limit_pct=0.10)
        res = rule.check(_order(OrderSide.SELL), _ctx(9.0, 10.0))
        assert not res.passed
        assert "跌停不能卖出" in res.reason

    def test_正常放行(self) -> None:
        rule = PriceLimitRule(limit_pct=0.10)
        res = rule.check(_order(OrderSide.BUY), _ctx(10.5, 10.0))
        assert res.passed


# ═══════════════════════════════════════════════
#  3. engine 注入 prev_close
# ═══════════════════════════════════════════════

class TestEnginePrevCloseInjection:
    def _make_engine(self):
        config = {
            "live": {"trade_mode": "paper", "name": "t12", "initial_capital": 100000},
            "broker": {"type": "paper"},
            "execution": {},
            "market_data": {},
            "strategy": {},
            "position": {},
            "risk_control": {},
            "state_store": {"auto_recover_on_start": False},  # KP-P5-08 显式必填对齐
            "scheduler": {},
            "lookahead_guard": {},
        }
        return create_live_engine(config)

    def test_build_risk_context_注入prev_close(self) -> None:
        """DoD：风控上下文 extra 含 prev_close（引擎从行情快照捕获）。"""
        engine = self._make_engine()
        engine._prev_close_map[SYMBOL] = 9.5
        ctx = engine._build_risk_context()
        assert ctx.extra.get("prev_close", {}).get(SYMBOL) == 9.5
