# 文件名：tests/unit/test_qmt_prev_close.py
"""
test_qmt_prev_close.py - QmtBroker 昨收基准/撮合价口径测试（KP-P3-09 / T-11）
======================================================================
覆盖"撮合价与昨收基准错误（两处首笔=昨收）"修复：
  1. 昨收注入正确性 —— update_market_price 传 prev_close 注入；首笔行情不再覆盖。
  2. set_market_price 注入 prev_close。
  3. set_prev_close 显式注入。
  4. 跨交易日重置 —— roll_prev_close_to_trading_day 清空昨收。
  5. 缺昨收 fail-closed —— _check_price_limit 拒绝（DoD）。
  6. 有昨收正常判定 —— 涨停/跌停边界。
  7. manager 撮合价口径 —— push_market_data 用 open 撮合并注入 prev_close（DoD）。

依赖: pytest, myquant.live.brokers.qmt_broker, myquant.live.brokers.manager
"""

from __future__ import annotations

from myquant.live.brokers import BrokerManager, QmtBroker
from myquant.models.trade import OrderSide

SYMBOL = "000001.SZ"


def _broker(**kw) -> QmtBroker:
    defaults = {
        "mode": "SIM",
        "initial_capital": 1_000_000.0,
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "stamp_tax_rate": 0.001,
        "transfer_fee_rate": 0.00001,
        "price_limit_pct": 0.10,
    }
    defaults.update(kw)
    return QmtBroker(**defaults)


# ═══════════════════════════════════════════════
#  1/2/3/4. 昨收注入正确性
# ═══════════════════════════════════════════════

class TestPrevCloseInjection:
    def test_update注入昨收_且首笔行情不覆盖(self) -> None:
        """DoD：update_market_price 传 prev_close 注入；不带 prev_close 的行情不覆盖昨收。"""
        broker = _broker()
        broker.update_market_price(SYMBOL, price=10.0, prev_close=9.5)
        assert broker._prev_close[SYMBOL] == 9.5
        # 后续首笔行情不带 prev_close：不得用现价覆盖昨收（原缺陷）
        broker.update_market_price(SYMBOL, price=10.8)
        assert broker._prev_close[SYMBOL] == 9.5

    def test_set_market_price_注入prev_close(self) -> None:
        broker = _broker()
        broker.set_market_price(SYMBOL, price=10.0, prev_close=9.5)
        assert broker._prev_close[SYMBOL] == 9.5
        broker.set_market_price(SYMBOL, price=10.8)  # 不覆盖
        assert broker._prev_close[SYMBOL] == 9.5

    def test_set_prev_close_显式注入(self) -> None:
        broker = _broker()
        broker.set_prev_close(SYMBOL, 9.5)
        assert broker._prev_close[SYMBOL] == 9.5
        broker.set_prev_close(SYMBOL, 0.0)  # 非正不注入
        assert broker._prev_close[SYMBOL] == 9.5

    def test_跨交易日重置昨收(self) -> None:
        broker = _broker()
        broker.set_prev_close(SYMBOL, 9.5)
        broker.roll_prev_close_to_trading_day("2026-10-05")
        assert broker._prev_close[SYMBOL] == 9.5  # 同日不清
        broker.roll_prev_close_to_trading_day("2026-10-06")  # 跨日清空
        assert SYMBOL not in broker._prev_close


# ═══════════════════════════════════════════════
#  5/6. 涨跌停 fail-closed
# ═══════════════════════════════════════════════

class TestPriceLimitFailClosed:
    def test_缺昨收_拒绝成交(self) -> None:
        """DoD：无昨收时 _check_price_limit → False（fail-closed）。"""
        broker = _broker()
        assert broker._check_price_limit(SYMBOL, 10.0, OrderSide.BUY) is False
        assert broker._check_price_limit(SYMBOL, 10.0, OrderSide.SELL) is False

    def test_有昨收_正常判定(self) -> None:
        broker = _broker()
        broker.set_prev_close(SYMBOL, 10.0)  # 涨停 11.0 / 跌停 9.0
        assert broker._check_price_limit(SYMBOL, 10.5, OrderSide.BUY) is True
        assert broker._check_price_limit(SYMBOL, 11.0, OrderSide.BUY) is True
        assert broker._check_price_limit(SYMBOL, 11.1, OrderSide.BUY) is False
        assert broker._check_price_limit(SYMBOL, 9.0, OrderSide.SELL) is True
        assert broker._check_price_limit(SYMBOL, 8.9, OrderSide.SELL) is False


# ═══════════════════════════════════════════════
#  7. manager 撮合价口径
# ═══════════════════════════════════════════════

class TestManagerFillPrice:
    def test_push_market_data_用open撮合并注入昨收(self, monkeypatch) -> None:
        """DoD：manager 撮合传 open（防未来），并注入 prev_close。"""
        broker = _broker()
        captured: dict = {}

        def spy_update(symbol: str, price: float, timestamp=None, prev_close=None) -> list:
            captured.update({"symbol": symbol, "price": price})
            return []

        monkeypatch.setattr(broker, "update_market_price", spy_update)
        mgr = BrokerManager()
        mgr.register_broker(broker, alias="qmt")
        bar = {"open": 10.2, "close": 10.9, "prev_close": 9.5}
        mgr.push_market_data(SYMBOL, bar)
        assert captured["price"] == 10.2  # 用 open 而非 close
        assert broker._prev_close[SYMBOL] == 9.5  # 昨收经 set_prev_close 注入
