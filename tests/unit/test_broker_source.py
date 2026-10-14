# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_broker_source.py
"""
券商实时数据源基类（myquant/data_access/sources/broker_source.py）单元测试（G0-9 补测）
==========================================================
覆盖基类行为：
  - 构造：默认 config / broker_name 解析 / name 属性
  - 元信息属性：source_type/priority/supported_markets/supported_periods/
    supports_financial/supports_realtime
  - 未连接时的降级：get_market_data 空结果 / get_realtime_quote None /
    subscribe/unsubscribe False / get_account_info None / get_stock_list [] /
    health_check DOWN
  - 子类必须实现的桩方法在连接态抛 NotImplementedError
  - 交易回调注册与分发（含异常容忍）
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from myquant.data_access.sources.base import DataSourceStatus
from myquant.data_access.sources.broker_source import BrokerDataSource


class TestConstruction:
    def test_default_config_uses_unknown_broker(self) -> None:
        src = BrokerDataSource()
        assert src.name == "broker_unknown"
        assert src.is_connected is False

    def test_broker_name_from_config(self) -> None:
        src = BrokerDataSource({"broker_name": "qmt", "account_id": "acc1"})
        assert src.name == "broker_qmt"

    def test_meta_properties(self) -> None:
        src = BrokerDataSource()
        assert src.source_type == "broker"
        assert src.priority == 5
        assert src.supported_markets == {"A股"}
        assert src.supported_periods == {"tick", "minute", "daily"}
        assert src.supports_financial is False
        assert src.supports_realtime is True


class TestNotConnectedDegrade:
    @pytest.fixture()
    def src(self) -> BrokerDataSource:
        return BrokerDataSource()

    def test_get_market_data_returns_empty(self, src: BrokerDataSource) -> None:
        result = src.get_market_data("600519.SH", "2024-01-01", "2024-01-31")
        assert result.symbol == "600519.SH"
        assert result.data == []
        assert result.count == 0

    def test_get_realtime_quote_returns_none(self, src: BrokerDataSource) -> None:
        assert src.get_realtime_quote("600519.SH") is None

    def test_subscribe_realtime_false(self, src: BrokerDataSource) -> None:
        assert src.subscribe_realtime("600519.SH", lambda d: None) is False

    def test_unsubscribe_realtime_false(self, src: BrokerDataSource) -> None:
        assert src.unsubscribe_realtime("600519.SH") is False

    def test_get_account_info_none(self, src: BrokerDataSource) -> None:
        assert src.get_account_info() is None

    def test_get_stock_list_empty(self, src: BrokerDataSource) -> None:
        assert src.get_stock_list() == []

    def test_health_check_down(self, src: BrokerDataSource) -> None:
        assert src.health_check() == DataSourceStatus.DOWN


class TestConnectedNotImplemented:
    @pytest.fixture()
    def src(self) -> BrokerDataSource:
        s = BrokerDataSource()
        s._connected = True  # 模拟已连接，触发子类桩
        return s

    def test_connect_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            BrokerDataSource().connect()

    def test_disconnect_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            BrokerDataSource().disconnect()

    def test_reconnect_not_implemented(self, src: BrokerDataSource) -> None:
        with pytest.raises(NotImplementedError):
            src.reconnect()

    def test_get_market_data_connected_not_implemented(
        self, src: BrokerDataSource,
    ) -> None:
        with pytest.raises(NotImplementedError):
            src.get_market_data("600519.SH")

    def test_get_realtime_quote_connected_not_implemented(
        self, src: BrokerDataSource,
    ) -> None:
        with pytest.raises(NotImplementedError):
            src.get_realtime_quote("600519.SH")

    def test_subscribe_realtime_connected_not_implemented(
        self, src: BrokerDataSource,
    ) -> None:
        with pytest.raises(NotImplementedError):
            src.subscribe_realtime("600519.SH", lambda d: None)

    def test_get_stock_list_connected_not_implemented(
        self, src: BrokerDataSource,
    ) -> None:
        with pytest.raises(NotImplementedError):
            src.get_stock_list()


class TestTradeCallbacks:
    def test_register_and_notify(self) -> None:
        src = BrokerDataSource()
        received: list[Dict[str, Any]] = []

        def cb(data: Dict[str, Any]) -> None:
            received.append(data)

        src.register_trade_callback(cb)
        src._notify_trade_callbacks({"type": "trade", "symbol": "600519"})
        assert len(received) == 1
        assert received[0]["symbol"] == "600519"

    def test_notify_tolerates_callback_error(self) -> None:
        src = BrokerDataSource()

        def bad_cb(data: Dict[str, Any]) -> None:
            raise RuntimeError("boom")

        src.register_trade_callback(bad_cb)
        # 不应向上抛
        src._notify_trade_callbacks({"type": "order"})
