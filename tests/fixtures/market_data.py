# 文件名：tests/fixtures/market_data.py
"""
market_data.py - 行情数据测试 Fixture
=======================================
提供正常数据和各类脏数据的 fixture，供数据清洗单元测试使用。

覆盖场景:
  - 正常 Bar 序列
  - OHLC 逻辑错误 (high < low, high < open)
  - 价格为负
  - 成交量为负
  - 重复时间戳
  - 时间回退
  - 价格跳变
  - 成交量异常放大
  - 空数据
  - 正常 Tick 序列
  - Tick bid>=ask 异常
  - Tick 重复
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

# ═══════════════════════════════════════════
#  Bar Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def sample_bar_records() -> List[Dict[str, Any]]:
    """正常的 Bar 数据序列 (5条，按时间升序)"""
    return [
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000, "amount": 1020000},
        {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.2, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 120000, "amount": 1272000},
        {"symbol": "000001.SZ", "date": "2024-01-04", "open": 10.6, "high": 11.0, "low": 10.5, "close": 10.9, "volume": 110000, "amount": 1199000},
        {"symbol": "000001.SZ", "date": "2024-01-05", "open": 10.9, "high": 11.2, "low": 10.8, "close": 11.1, "volume": 95000, "amount": 1054500},
        {"symbol": "000001.SZ", "date": "2024-01-08", "open": 11.1, "high": 11.5, "low": 11.0, "close": 11.3, "volume": 105000, "amount": 1186500},
    ]


@pytest.fixture
def bar_with_ohlc_error() -> List[Dict[str, Any]]:
    """包含 OHLC 逻辑错误的 Bar (high < low)"""
    return [
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 9.5, "low": 10.5, "close": 10.2, "volume": 100000, "amount": 1020000},
    ]


@pytest.fixture
def bar_with_negative_price() -> List[Dict[str, Any]]:
    """包含负价格的 Bar"""
    return [
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": -1.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000, "amount": 1020000},
    ]


@pytest.fixture
def bar_with_negative_volume() -> List[Dict[str, Any]]:
    """包含负成交量的 Bar"""
    return [
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": -100, "amount": 1020000},
    ]


@pytest.fixture
def bar_with_duplicate_timestamp() -> List[Dict[str, Any]]:
    """包含重复时间戳的 Bar 序列"""
    return [
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000, "amount": 1020000},
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.1, "high": 10.6, "low": 9.9, "close": 10.3, "volume": 110000, "amount": 1133000},
        {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.3, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 120000, "amount": 1272000},
    ]


@pytest.fixture
def bar_with_price_jump() -> List[Dict[str, Any]]:
    """包含价格跳变的 Bar 序列 (第2根开盘价相对前收盘涨幅>22%)"""
    return [
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0, "volume": 100000, "amount": 1000000},
        {"symbol": "000001.SZ", "date": "2024-01-03", "open": 15.0, "high": 15.5, "low": 14.8, "close": 15.2, "volume": 200000, "amount": 3040000},
        {"symbol": "000001.SZ", "date": "2024-01-04", "open": 15.2, "high": 15.8, "low": 15.0, "close": 15.5, "volume": 180000, "amount": 2790000},
    ]


@pytest.fixture
def bar_with_volume_anomaly() -> List[Dict[str, Any]]:
    """包含成交量异常放大的 Bar 序列"""
    return [
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000, "amount": 1020000},
        {"symbol": "000001.SZ", "date": "2024-01-03", "open": 10.2, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 50000000, "amount": 530000000},
        {"symbol": "000001.SZ", "date": "2024-01-04", "open": 10.6, "high": 11.0, "low": 10.5, "close": 10.9, "volume": 110000, "amount": 1199000},
    ]


@pytest.fixture
def bar_missing_symbol() -> List[Dict[str, Any]]:
    """缺少 symbol 的 Bar"""
    return [
        {"date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000, "amount": 1020000},
    ]


@pytest.fixture
def empty_bar_records() -> List[Dict[str, Any]]:
    """空 Bar 数据"""
    return []


@pytest.fixture
def bar_mixed_dirty() -> List[Dict[str, Any]]:
    """混合脏数据：正常 + OHLC错误 + 负价格 + 重复时间戳"""
    return [
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 100000, "amount": 1020000},
        {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.1, "high": 10.6, "low": 9.9, "close": 10.3, "volume": 110000, "amount": 1133000},
        {"symbol": "000001.SZ", "date": "2024-01-03", "open": -5.0, "high": 10.8, "low": 10.1, "close": 10.6, "volume": 120000, "amount": 1272000},
        {"symbol": "000001.SZ", "date": "2024-01-04", "open": 10.6, "high": 9.0, "low": 11.0, "close": 10.9, "volume": 110000, "amount": 1199000},
        {"symbol": "000001.SZ", "date": "2024-01-05", "open": 10.9, "high": 11.2, "low": 10.8, "close": 11.1, "volume": 95000, "amount": 1054500},
    ]


# ═══════════════════════════════════════════
#  Tick Fixtures
# ═══════════════════════════════════════════

@pytest.fixture
def sample_tick_records() -> List[Dict[str, Any]]:
    """正常的 Tick 数据序列"""
    return [
        {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 9.99, "ask_price": 10.01},
        {"symbol": "000001.SZ", "timestamp": 1704153601.0, "price": 10.01, "volume": 200, "amount": 2002, "bid_price": 10.00, "ask_price": 10.02},
        {"symbol": "000001.SZ", "timestamp": 1704153602.0, "price": 10.02, "volume": 150, "amount": 1503, "bid_price": 10.01, "ask_price": 10.03},
    ]


@pytest.fixture
def tick_with_bid_ask_cross() -> List[Dict[str, Any]]:
    """包含 bid >= ask 异常的 Tick"""
    return [
        {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 10.05, "ask_price": 10.01},
    ]


@pytest.fixture
def tick_with_negative_price() -> List[Dict[str, Any]]:
    """包含负价格的 Tick"""
    return [
        {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": -1.0, "volume": 100, "amount": -100, "bid_price": 0, "ask_price": 0},
    ]


@pytest.fixture
def tick_duplicate() -> List[Dict[str, Any]]:
    """重复 Tick (同时间同价格同量)"""
    return [
        {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 9.99, "ask_price": 10.01},
        {"symbol": "000001.SZ", "timestamp": 1704153600.0, "price": 10.0, "volume": 100, "amount": 1000, "bid_price": 9.99, "ask_price": 10.01},
        {"symbol": "000001.SZ", "timestamp": 1704153601.0, "price": 10.01, "volume": 200, "amount": 2002, "bid_price": 10.00, "ask_price": 10.02},
    ]


@pytest.fixture
def empty_tick_records() -> List[Dict[str, Any]]:
    """空 Tick 数据"""
    return []
