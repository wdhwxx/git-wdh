# -*- coding: utf-8 -*-
"""
test_live_bar_dedup.py - KP-P3-02 日线 bar 去重（台账 T-04）
============================================================
覆盖 myquant/live/data_feed.py + myquant/live/engine.py + myquant/core/date_utils.py：
  1. make_bar timestamp 用 bar 周期归属时间（不用墙钟 time.time()），确定性且可被
     format_bar_date 往返还原为同一日期
  2. bar_date_to_epoch 与 normalize_to_datetime 严格互逆（解析失败返回 0.0 防御式）
  3. 引擎 on_market_data 以 (symbol, date) 去重：同一根 bar 二次推送直接丢弃，
     不进入 broker 撮合（不产生成交）；不同日期均正常处理

回归依据: 审计 KP-P3-02 —— data_feed.py:163 timestamp=time.time()（墙钟），
         去重仅单调校验（墙钟递增→恒过），同一根 bar 每轮重复推送并以当日开盘价成交。
运行方式: pytest tests/unit/test_live_bar_dedup.py -v
"""
from __future__ import annotations

from datetime import datetime

from myquant.core.date_utils import (
    bar_date_to_epoch,
    format_bar_date,
    normalize_to_datetime,
)
from myquant.live.data_feed import _make_bar
from myquant.live.engine import EngineState, LiveEngine, RiskConfig
from myquant.strategies.base import StrategyContext


def _make_engine() -> LiveEngine:
    """最小引擎（关闭全部保障组件，仅驱动 on_market_data 去重路径）。"""
    engine = LiveEngine(
        engine_id="bar_dedup_test",
        risk_config=RiskConfig(max_positions=10, max_position_pct=0.5),
        enable_risk_manager=False,
        enable_guardian=False,
        enable_state_store=False,
        enable_scheduler=False,
        enable_event_bus=False,
    )
    engine._context = StrategyContext()
    engine._data_feed = None  # 无行情源时 freq 默认 'daily'
    engine._state = EngineState.RUNNING
    return engine


def _bar(date: str = "2025-06-15") -> dict:
    """构造一根标准日线 bar dict（data_feed make_bar 产物等价物）。"""
    return {
        "symbol": "000001.SZ",
        "date": date,
        "open": 10.0,
        "high": 10.5,
        "low": 9.9,
        "close": 10.2,
        "volume": 1000.0,
        "amount": 10000.0,
        "timestamp": bar_date_to_epoch(date),
    }


class _RecordingBroker:
    """记录 push_market_data 调用次数的桩 broker，用于断言二次推送不达撮合。"""

    def __init__(self) -> None:
        self.push_count: int = 0

    def push_market_data(self, symbol: str, bar: dict) -> None:
        self.push_count += 1


class TestBarDateToEpoch:
    """date_utils 新增 bar_date_to_epoch：与 normalize_to_datetime 严格互逆。"""

    def test_往返还原同一日期(self) -> None:
        for s in ("2025-06-15", "2025-06-15 10:30:00"):
            ts = bar_date_to_epoch(s)
            assert isinstance(ts, float) and ts > 0
            dt = normalize_to_datetime(ts)
            assert dt is not None
            # 日线格式还原为日期
            assert format_bar_date(ts, "daily") == "2025-06-15"

    def test_解析失败返回0防御式(self) -> None:
        assert bar_date_to_epoch(None) == 0.0
        assert bar_date_to_epoch("不是日期") == 0.0


class TestMakeBarTimestamp:
    """data_feed.make_bar：timestamp 用周期归属时间，非墙钟。"""

    def test_时间戳确定性且可还原日期(self) -> None:
        bar1 = _make_bar(
            "000001.SZ", "2025-06-15", 10.0, 10.5, 9.9, 10.2, 1000.0,
            amount=10000.0, period="daily",
        )
        bar2 = _make_bar(
            "000001.SZ", "2025-06-15", 10.0, 10.5, 9.9, 10.2, 1000.0,
            amount=10000.0, period="daily",
        )
        assert bar1["date"] == "2025-06-15"
        # 同一日期确定性：两次调用时间戳一致（墙钟做不到）
        assert bar1["timestamp"] == bar2["timestamp"]
        # 可往返还原为同一交易日
        assert format_bar_date(bar1["timestamp"], "daily") == "2025-06-15"
        assert normalize_to_datetime(bar1["timestamp"]) == datetime(2025, 6, 15)

    def test_不同日期时间戳不同(self) -> None:
        t1 = _make_bar("000001.SZ", "2025-06-15", 1, 2, 0.5, 1.5, 1, period="daily")["timestamp"]
        t2 = _make_bar("000001.SZ", "2025-06-16", 1, 2, 0.5, 1.5, 1, period="daily")["timestamp"]
        assert t2 > t1


class TestEngineBarDedup:
    """engine.on_market_data：(symbol, date) 去重，同根 bar 二次推送丢弃且不成交。"""

    def test_同一根bar二次推送丢弃且不达撮合(self) -> None:
        engine = _make_engine()
        broker = _RecordingBroker()
        engine._broker_manager = broker
        bar = _bar("2025-06-15")
        engine.on_market_data("000001.SZ", bar)
        # 首根处理：history 1 根 + broker 撮合 1 次
        assert len(engine._bar_history["000001.SZ"]) == 1
        assert broker.push_count == 1
        # 二次推送：去重丢弃，不进入撮合、不产生成交
        engine.on_market_data("000001.SZ", bar)
        assert len(engine._bar_history["000001.SZ"]) == 1
        assert broker.push_count == 1
        assert engine._stats["bar_dedup_skipped"] == 1
        assert engine._stats["filled_orders"] == 0

    def test_不同日期均正常处理(self) -> None:
        engine = _make_engine()
        broker = _RecordingBroker()
        engine._broker_manager = broker
        engine.on_market_data("000001.SZ", _bar("2025-06-15"))
        engine.on_market_data("000001.SZ", _bar("2025-06-16"))
        assert len(engine._bar_history["000001.SZ"]) == 2
        assert broker.push_count == 2
        assert engine._stats["bar_dedup_skipped"] == 0

    def test_不同标的同日不误删(self) -> None:
        engine = _make_engine()
        broker = _RecordingBroker()
        engine._broker_manager = broker
        bar_a = _bar("2025-06-15")
        bar_b = dict(_bar("2025-06-15"))
        bar_b["symbol"] = "600519.SH"
        engine.on_market_data("000001.SZ", bar_a)
        engine.on_market_data("600519.SH", bar_b)
        # 两标的同时日 bar 均应处理（去重键含 symbol）
        assert broker.push_count == 2
        assert engine._stats["bar_dedup_skipped"] == 0
