# 文件名：tests/unit/test_p118_non_cash_fixes.py
"""P1-18 非真钱子项回归：黑名单允许卖出 / 未知规则名硬失败 / ML识别中文日期列。"""

from __future__ import annotations

import pandas as pd

from myquant.models.trade import Order, OrderSide, OrderType
from myquant.risk.rules import BlacklistRule, RiskRuleLibrary


def _order(side: OrderSide) -> Order:
    return Order(order_id="x", symbol="600519.SH", side=side,
                 order_type=OrderType.LIMIT, quantity=100, price=100.0,
                 strategy_id="s")


def test_blacklist_blocks_buy_but_allows_sell() -> None:
    r = BlacklistRule(blacklist={"600519.SH"})
    assert not r.check(_order(OrderSide.BUY), None).passed
    assert r.check(_order(OrderSide.SELL), None).passed


def test_unknown_rule_name_raises() -> None:
    try:
        RiskRuleLibrary.from_config({"不存在的规则xyz": {"enabled": True}})
        raise AssertionError("未知规则名必须硬失败，不得静默跳过")
    except ValueError:
        pass


def test_ml_recognizes_chinese_date_column() -> None:
    from myquant.ml.inference_service import _dates_to_epoch
    out = _dates_to_epoch(pd.Series([20240101, 20240102]))
    assert len(out) == 2 and out[1] > out[0]
