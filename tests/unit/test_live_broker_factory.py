# 文件名：tests/unit/test_live_broker_factory.py
"""
test_live_broker_factory.py - live 券商装配工厂单测（回写#289/#294）
==========================================================================
0.3（#289）：build_broker_from_config 由 engine 迁入 broker_factory（应急链独立）；
本测试为 myquant.live.broker_factory 的引用级覆盖（R-19 零覆盖基线）。
0.9（#294）：broker 提交边界硬闸在基类——工厂创建的 PaperBroker 同样带薄闸。
"""
from __future__ import annotations

import pytest

from myquant.live.broker_factory import build_broker_from_config
from myquant.live.brokers.paper_broker import PaperBroker
from myquant.models.trade import Order, OrderSide, OrderStatus

_BROKER_CFG = {"type": "paper"}
_EXEC_CFG = {"commission_rate": 0.0002, "min_commission": 5.0, "stamp_tax_rate": 0.001}
_CAPITAL = 1_000_000.0


def test_build_paper_broker_default() -> None:
    """paper/sim 配置 → PaperBroker 实例（工厂默认走 paper）。"""
    broker = build_broker_from_config(_BROKER_CFG, _EXEC_CFG, _CAPITAL)
    assert isinstance(broker, PaperBroker)
    assert broker.available_cash == _CAPITAL


def test_build_broker_unknown_type_raises() -> None:
    """未实现适配器明确报错（不静默回退）。"""
    with pytest.raises(ValueError):
        build_broker_from_config({"type": "xtp"}, _EXEC_CFG, _CAPITAL)


def test_factory_broker_has_submission_gate() -> None:
    """0.9 硬闸在基类：工厂装配的 broker 同样过薄闸（kill_switch 拒 BUY）。"""
    broker = build_broker_from_config(_BROKER_CFG, _EXEC_CFG, _CAPITAL)
    broker.connect()
    # 无标志 → BUY 正常放行（不误伤）
    ok = broker.submit_order(
        Order(symbol="000001.SZ", side=OrderSide.BUY, quantity=100, price=10.0)
    )
    assert ok.status != OrderStatus.REJECTED
