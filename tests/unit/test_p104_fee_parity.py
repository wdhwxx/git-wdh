# 文件名：tests/unit/test_p104_fee_parity.py
"""
P1-04 跨层费用对拍：live broker 与回测 FeeCalculator 逐点一致
========================================================================
live brokers（paper_broker / qmt_broker）的 _calc_commission 已改走
fee_common.FeeCalculator（同一 CommissionModel 参数）。本测试在最低佣金
临界带（amount×费率 ≈ min_commission=5，即 notional≈25000）附近逐点对拍，
杜绝 live/backtest 费用口径漂移（破坏"回测实盘同构"）。
"""
from __future__ import annotations

import pytest

from myquant.models.trade import OrderSide


class _StubTrade:
    """最小 Trade 替身：_calc_commission 只依赖 notional 与 side"""

    def __init__(self, notional: float, side: OrderSide) -> None:
        self.notional = notional
        self.side = side


# A股写实费率（live broker 默认值）
_RATE = 0.0002
_MIN = 5.0
_STAMP = 0.001
_TRANSFER = 0.00001

# 临界带采样：notional 从千元到十万，覆盖 amount*rate 跨越 min_commission 的点
_NOTIONALS = [1_000.0, 5_000.0, 10_000.0, 24_000.0, 25_000.0, 26_000.0, 50_000.0, 100_000.0]


def _backtest_fee(side: OrderSide, notional: float) -> float:
    """回测基准：FeeCalculator breakdown 求和（round 2）"""
    from myquant.backtest.fee_common import FeeCalculator
    from myquant.models.backtest import CommissionModel

    calc = FeeCalculator(
        commission_model=CommissionModel(
            buy_rate=_RATE, sell_rate=_RATE, min_commission=_MIN,
            stamp_tax=_STAMP, transfer_fee=_TRANSFER,
        )
    )
    c, s, t = calc.calc_commission_breakdown(side, notional)
    return round(c + s + t, 2)


class TestP104LiveBacktestFeeParity:
    """P1-04：live broker 费用 == 回测 FeeCalculator 费用（逐点）"""

    @pytest.mark.parametrize("notional", _NOTIONALS)
    def test_paper_broker_buy(self, notional: float) -> None:
        from myquant.live.brokers.paper_broker import PaperBroker

        br = PaperBroker(
            commission_rate=_RATE, min_commission=_MIN,
            stamp_tax_rate=_STAMP, transfer_fee_rate=_TRANSFER,
        )
        live_fee = br._calc_commission(_StubTrade(notional, OrderSide.BUY))
        assert live_fee == pytest.approx(_backtest_fee(OrderSide.BUY, notional), abs=1e-6)

    @pytest.mark.parametrize("notional", _NOTIONALS)
    def test_paper_broker_sell(self, notional: float) -> None:
        from myquant.live.brokers.paper_broker import PaperBroker

        br = PaperBroker(
            commission_rate=_RATE, min_commission=_MIN,
            stamp_tax_rate=_STAMP, transfer_fee_rate=_TRANSFER,
        )
        live_fee = br._calc_commission(_StubTrade(notional, OrderSide.SELL))
        assert live_fee == pytest.approx(_backtest_fee(OrderSide.SELL, notional), abs=1e-6)
