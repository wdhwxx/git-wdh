# 文件名：tests/unit/test_live_fee.py
"""
test_live_fee.py - LIVE 本地账本费用计提测试（KP-P4-03 / T-09）
=================================================================
覆盖"LIVE 本地账本不记费用（commission/slippage 恒 0）"修复：
原 qmt_broker LIVE 成交 trade.commission=0.0/slippage=0.0，本地账本
（LivePortfolio）按 0 费扣现金，导致本地现金高于券商实际。

修复：LIVE 成交也走 fee_common 唯一入口计提（佣金万2.5最低5元 + 过户费万0.1
买卖双向 + 印花税千1仅卖出），与 SIM/回测同构，本地账本如实扣费。
  1. LIVE 买单 fee>0 —— 10 万买单佣金+过户费计提（DoD）。
  2. LIVE 卖单含印花税 —— 千1 印花税计入。
  3. 佣金最低 5 元 —— 小单按 min_commission 兜底。
  4. 现金守恒 —— 本地账本 = 初始 − 成交金额 − 累计费用（DoD）。
  5. 口径与回测 FeeCalculator 同构 —— 同一模型同一参数。

依赖: pytest, myquant.live.brokers.qmt_broker（QmtBroker）、
      myquant.live.portfolio（LivePortfolio）、myquant.models.trade、
      myquant.backtest.fee_common（FeeCalculator）、myquant.models.backtest
"""

from __future__ import annotations

import time

import pytest

from myquant.backtest.fee_common import FeeCalculator
from myquant.live.brokers.qmt_broker import QmtBroker
from myquant.live.portfolio import LivePortfolio
from myquant.models.backtest import CommissionModel
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType

SYMBOL = "000001.SZ"


def _make_broker(**kw) -> QmtBroker:
    defaults = {
        "mode": "LIVE",
        "initial_capital": 1_000_000.0,
        "commission_rate": 0.00025,   # 万2.5
        "min_commission": 5.0,
        "stamp_tax_rate": 0.001,       # 千1 仅卖出
        "transfer_fee_rate": 0.00001,  # 万0.1
    }
    defaults.update(kw)
    return QmtBroker(**defaults)


def _order(side: OrderSide, qty: float = 10000.0, price: float = 10.0) -> Order:
    return Order(
        symbol=SYMBOL, side=side, order_type=OrderType.MARKET,
        quantity=qty, price=price, status=OrderStatus.SUBMITTED,
    )


def _fill(broker: QmtBroker, order: Order) -> None:
    """驱动 LIVE 成交（_execute_fill 统一成交回报入口，无需 QMT 连接）。"""
    trade = broker._execute_fill(order, fill_price=10.0, timestamp=time.time())
    assert trade is not None, "成交应成功"
    return trade


# ═══════════════════════════════════════════════
#  1/2/3. LIVE 费用计提
# ═══════════════════════════════════════════════

class TestLiveFee:
    def test_LIVE买单fee大于0(self) -> None:
        """DoD：10 万买单 fee>0（佣金 25 + 过户费 1 = 26）。"""
        broker = _make_broker()
        trade = _fill(broker, _order(OrderSide.BUY, qty=10000.0, price=10.0))
        assert trade.commission > 0.0
        assert trade.commission == 26.0
        assert trade.slippage == 20.0  # notional 10万 × 滑点 ratio 万2

    def test_LIVE卖单含印花税(self) -> None:
        """卖单计提印花税千1：佣金 25 + 过户费 1 + 印花税 100 = 126。"""
        broker = _make_broker()
        trade = _fill(broker, _order(OrderSide.SELL, qty=10000.0, price=10.0))
        assert trade.commission == 126.0

    def test_佣金最低5元兜底(self) -> None:
        """小额单佣金按 min_commission=5 兜底。"""
        broker = _make_broker()
        trade = _fill(broker, _order(OrderSide.BUY, qty=100.0, price=10.0))
        # notional=1000 → 佣金 max(0.25, 5)=5 + 过户费 0.01 → 5.01
        assert trade.commission == 5.01


# ═══════════════════════════════════════════════
#  4. 现金守恒（DoD）
# ═══════════════════════════════════════════════

class TestCashConservation:
    def test_本地账本现金守恒_扣费(self) -> None:
        """本地账本 = 初始 − 成交金额 − 累计费用。"""
        broker = _make_broker()
        trade = _fill(broker, _order(OrderSide.BUY, qty=10000.0, price=10.0))
        portfolio = LivePortfolio(initial_capital=1_000_000.0)
        portfolio.apply_trade(trade)
        expected = 1_000_000.0 - 10000.0 * 10.0 - trade.commission
        assert portfolio._cash == pytest.approx(expected, abs=0.01)
        assert portfolio._stats["total_commission"] == pytest.approx(
            trade.commission, abs=0.01)

    def test_卖单现金守恒_回笼并扣费(self) -> None:
        broker = _make_broker()
        trade = _fill(broker, _order(OrderSide.SELL, qty=10000.0, price=10.0))
        portfolio = LivePortfolio(initial_capital=1_000_000.0)
        portfolio.apply_trade(trade)
        expected = 1_000_000.0 + 10000.0 * 10.0 - trade.commission
        assert portfolio._cash == pytest.approx(expected, abs=0.01)


# ═══════════════════════════════════════════════
#  5. 与回测 FeeCalculator 同构
# ═══════════════════════════════════════════════

class TestFeeConsistency:
    def test_费用与回测FeeCalculator同构(self) -> None:
        """broker._calc_commission 与回测 FeeCalculator 同一模型同参。"""
        broker = _make_broker()
        trade = _fill(broker, _order(OrderSide.BUY, qty=10000.0, price=10.0))
        calc = FeeCalculator(commission_model=CommissionModel(
            buy_rate=broker.commission_rate,
            sell_rate=broker.commission_rate,
            min_commission=broker.min_commission,
            stamp_tax=broker.stamp_tax_rate,
            transfer_fee=broker.transfer_fee_rate,
        ))
        comm, stamp, transfer = calc.calc_commission_breakdown(
            trade.side, trade.notional)
        assert trade.commission == pytest.approx(comm + stamp + transfer, abs=0.01)
