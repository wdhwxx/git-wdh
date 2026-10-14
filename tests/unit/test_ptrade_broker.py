# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_ptrade_broker.py
"""
Ptrade 券商适配（myquant/live/brokers/ptrade_broker.py）单元测试（G0-9 补测）
==========================================================
SIM 模式（复用 QmtBroker 模拟引擎）覆盖：
  - 构造：ptrade_path/account_id/mode 语义字段映射、api_ready=False
  - load_config：空配置 / ptrade 段 / 扁平结构 / ptrade_path 透传
  - order_shares：LIMIT 缺 price 抛 ValueError / 委托收敛到 submit_order
  - order_value：无价抛 ValueError / 金额过小无法凑整手抛 ValueError /
    金额→整手股数换算（向下取整）
  - order_target_percent：percent 越界抛 ValueError / 委托收敛 order_value
  - health_check：含 ptrade_api_ready / ptrade_path 字段
  - _call_ptrade_api / _call_qmt_sdk：真实 API 未接入明确抛 NotImplementedError
"""
from __future__ import annotations

import pytest

from myquant.live.brokers.ptrade_broker import PtradeBroker
from myquant.models.trade import OrderSide, OrderType


@pytest.fixture()
def sim() -> PtradeBroker:
    """SIM 模式 PtradeBroker（无需真实 SDK）。"""
    return PtradeBroker(ptrade_path="C:/ptrade", account_id="SIM001", mode="SIM")


class TestConstruction:
    def test_semantic_fields_mapped(self, sim: PtradeBroker) -> None:
        assert sim._ptrade_path == "C:/ptrade"
        assert sim._ptrade_api_ready is False

    def test_default_path_empty(self) -> None:
        b = PtradeBroker()
        assert b._ptrade_path == ""
        assert b._qmt_path == ""


class TestLoadConfig:
    def test_empty_config_is_noop(self, sim: PtradeBroker) -> None:
        sim.load_config({})
        assert sim._ptrade_path == "C:/ptrade"

    def test_ptrade_section(self, sim: PtradeBroker) -> None:
        sim.load_config({"ptrade": {"ptrade_path": "D:/ptrade_new", "mode": "LIVE"}})
        assert sim._ptrade_path == "D:/ptrade_new"
        assert sim._qmt_path == "D:/ptrade_new"

    def test_flat_config(self, sim: PtradeBroker) -> None:
        sim.load_config({"ptrade_path": "E:/flat", "account_id": "ACC"})
        assert sim._ptrade_path == "E:/flat"


class TestOrderShares:
    def test_limit_without_price_raises(self, sim: PtradeBroker) -> None:
        with pytest.raises(ValueError, match="LIMIT 委托必须指定 price"):
            sim.order_shares("600519.SH", 100, OrderSide.BUY, price=None,
                             order_type=OrderType.LIMIT)

    def test_limit_order_delegates(self, sim: PtradeBroker) -> None:
        order = sim.order_shares("600519.SH", 200, OrderSide.BUY,
                                 price=1800.0, order_type=OrderType.LIMIT)
        assert order.symbol == "600519.SH"
        assert float(order.quantity) == 200.0
        assert order.side == OrderSide.BUY


class TestOrderValue:
    def test_no_price_raises(self, sim: PtradeBroker) -> None:
        with pytest.raises(ValueError, match="无成交价可用"):
            sim.order_value("600519.SH", 10000, OrderSide.BUY, price=None)

    def test_value_to_lot_shares_floor(self, sim: PtradeBroker) -> None:
        order = sim.order_value("600519.SH", 10000, OrderSide.BUY, price=10.0)
        # int(10000/10/100)*100 = 1000
        assert float(order.quantity) == 1000.0

    def test_value_too_small_raises(self, sim: PtradeBroker) -> None:
        with pytest.raises(ValueError, match="委托金额过小"):
            sim.order_value("600519.SH", 50, OrderSide.BUY, price=10.0)


class TestOrderTargetPercent:
    def test_invalid_percent_raises(self, sim: PtradeBroker) -> None:
        for bad in (0.0, -0.1, 1.5):
            with pytest.raises(ValueError, match="percent 必须在"):
                sim.order_target_percent("600519.SH", bad)

    def test_valid_percent_delegates(self, sim: PtradeBroker) -> None:
        order = sim.order_target_percent("600519.SH", 0.1, price=10.0)
        # 目标金额 = total_assets * 0.1；正常收敛到 order_value
        assert order.symbol == "600519.SH"
        assert float(order.quantity) > 0


class TestHealthCheck:
    def test_contains_ptrade_fields(self, sim: PtradeBroker) -> None:
        info = sim.health_check()
        assert "ptrade_api_ready" in info
        assert info["ptrade_api_ready"] is False
        assert info.get("ptrade_path") == "C:/ptrade"


class TestRealApiPlaceholder:
    def test_call_ptrade_api_raises(self, sim: PtradeBroker) -> None:
        with pytest.raises(NotImplementedError, match="真实Ptrade API 通道未接入"):
            sim._call_ptrade_api("connect")

    def test_call_qmt_sdk_redirects(self, sim: PtradeBroker) -> None:
        with pytest.raises(NotImplementedError, match="真实Ptrade API 通道未接入"):
            sim._call_qmt_sdk("submit_order")
