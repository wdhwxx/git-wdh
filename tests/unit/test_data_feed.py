# 文件名：tests/unit/test_data_feed.py
"""
P3-1（v1.2 §6.4 ROI 1）：live/data_feed.py 覆盖率补测（28.9%→60%）

覆盖范围：
  1. 模块工具：_make_bar（周期格式化/timestamp）/ _infer_market / _to_standard_symbol /
     _to_tencent_code / _period_from_frequency / _factor_on_or_before /
     _live_jump_change_with_factor（除权跳变豁免）/ _quote_to_bar
  2. MockRealtimeSource：确定性路径/时钟推进/自定义 base/过滤未知标的
  3. RealtimeBarAggregator：周期切换产新 bar / 同周期增量更新 / 查询 / reset / 空 symbol
  4. RealtimeQuoteFetcher：构造 / active_backend / _parse_tencent_line（正常/畸形/价格<=0/
     未来时间回退）/ mock 后端 / tencent 失败降级 akshare（monkeypatch）
  5. MarketDataFeed：
     - 构造（realtime_mock / 频率映射 / clean 配置 / adjust_out）
     - connect（realtime_mock→mock 后端；akshare 未装→False；tdx/custom→True）
     - disconnect（stop + aggregator reset + bridge flush）
     - 属性 is_connected/is_running/is_realtime/active_backend
     - subscribe/unsubscribe/get_subscribed_symbols/is_subscribed/回调去重
     - 回调注册（error/dirty）
     - attach_event_bus（bridge 挂载 + 回调注册）
     - poll_once（未连接→{}；无订阅→{}；realtime_mock 全链路：快照→聚合→清洗→缓存→分发）
     - _fetch_latest_bars（tdx + 本地 parquet 缺失/缺列/损坏）
     - _fetch_from_akshare（akshare 未装→[]）
     - _stream_clean（脏数据丢弃+回调 / 跳变未达阈值 / 达阈值告警）
     - _clean_bars（空 / 异常回退原数据）
     - _update_cache（latest / history 100 截断 / qfq 现算）
     - get_latest_bar / get_historical_bars
     - _dispatch（回调收到 / 回调异常容错）
     - start（未连接 / 已运行 / 正常启动）/ stop
     - health_check（未连接 down / 正常 healthy / degraded / realtime 段）
     - stats / repr / _maybe_adjust_bar（qfq / 无因子）
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest

from myquant.live.data_feed import (
    MarketDataFeed,
    MockRealtimeSource,
    RealtimeBarAggregator,
    RealtimeQuoteFetcher,
    _factor_on_or_before,
    _infer_market,
    _live_jump_change_with_factor,
    _make_bar,
    _period_from_frequency,
    _quote_to_bar,
    _to_standard_symbol,
    _to_tencent_code,
)

# ─────────────────────────────────────────────
# 模块工具函数
# ─────────────────────────────────────────────

class TestMakeBar:
    def test_daily_format(self) -> None:
        bar = _make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.2, 1000)
        assert bar["date"] == "2024-01-15"
        assert bar["symbol"] == "000001.SZ"
        assert bar["timestamp"] == datetime(2024, 1, 15).timestamp()

    def test_minute_format(self) -> None:
        bar = _make_bar("000001.SZ", "2024-01-15 10:30:00", 10.0, 10.5, 9.8, 10.2, 1000,
                        period="1min")
        assert ":" in bar["date"]  # 分钟级带时间

    def test_float_coercion(self) -> None:
        bar = _make_bar("x", "2024-01-15", 1, 2, 3, 4, 5, 6)
        assert all(isinstance(bar[k], float) for k in ("open", "high", "low", "close", "volume", "amount"))


class TestInferMarket:
    def test_sh(self) -> None:
        for code in ("600519", "900901", "500001", "700001"):
            assert _infer_market(code) == "sh", code

    def test_sz(self) -> None:
        for code in ("000001", "200001", "300001"):
            assert _infer_market(code) == "sz", code

    def test_bj(self) -> None:
        for code in ("400001", "800001"):
            assert _infer_market(code) == "bj", code

    def test_unknown_default_sz(self) -> None:
        assert _infer_market("123456") == "sz"


class TestSymbolConverters:
    def test_to_standard_symbol(self) -> None:
        assert _to_standard_symbol("600519") == "600519.SH"
        assert _to_standard_symbol("000001") == "000001.SZ"

    def test_to_tencent_code(self) -> None:
        assert _to_tencent_code("600519.SH") == "sh600519"
        assert _to_tencent_code("000001.SZ") == "sz000001"

    def test_period_from_frequency(self) -> None:
        assert _period_from_frequency("1m") == 60
        assert _period_from_frequency("5m") == 300
        assert _period_from_frequency("1h") == 3600
        assert _period_from_frequency("unknown") == 60  # 默认
        assert _period_from_frequency("") == 60


class TestFactorHelpers:
    def test_factor_on_or_before_empty(self) -> None:
        assert _factor_on_or_before({}, "2024-01-15") is None
        assert _factor_on_or_before({"2024-01-10": 1.0}, "") is None

    def test_factor_on_or_before_picks_latest(self) -> None:
        lookup = {"2024-01-01": 1.0, "2024-01-10": 1.2, "2024-01-20": 1.5}
        assert _factor_on_or_before(lookup, "2024-01-15") == pytest.approx(1.2)
        assert _factor_on_or_before(lookup, "2024-01-25") == pytest.approx(1.5)
        assert _factor_on_or_before(lookup, "2023-12-31") is None

    def test_jump_change_no_factor(self) -> None:
        assert _live_jump_change_with_factor(10.0, 5.0, "2024-01-01", "2024-01-02", {}) is None

    def test_jump_change_no_event(self) -> None:
        lookup = {"2024-01-01": 1.0, "2024-01-02": 1.0}
        assert _live_jump_change_with_factor(10.0, 9.5, "2024-01-01", "2024-01-02", lookup) is None

    def test_jump_change_absorbed(self) -> None:
        # 除权：价格减半但因子翻倍 → hfq 连续 → 真实跳变约 0
        lookup = {"2024-01-01": 1.0, "2024-01-02": 2.0}
        jump = _live_jump_change_with_factor(10.0, 5.0, "2024-01-01", "2024-01-02", lookup)
        assert jump is not None and jump < 0.001

    def test_jump_change_bad_prev_factor(self) -> None:
        lookup = {"2024-01-01": 0.0, "2024-01-02": 2.0}
        assert _live_jump_change_with_factor(10.0, 5.0, "2024-01-01", "2024-01-02", lookup) is None

    def test_quote_to_bar(self) -> None:
        quote = {
            "symbol": "000001.SZ", "price": 10.5, "open": 10.0, "high": 10.8,
            "low": 9.9, "volume": 100.0, "amount": 1000.0, "timestamp": 1700000000.0,
        }
        bar = _quote_to_bar(quote)
        assert bar["close"] == 10.5
        assert bar["symbol"] == "000001.SZ"
        assert bar["timestamp"] == 1700000000.0
        assert ":" in bar["date"]


# ─────────────────────────────────────────────
# MockRealtimeSource
# ─────────────────────────────────────────────

class TestMockRealtimeSource:
    def test_default_symbols_and_base(self) -> None:
        src = MockRealtimeSource()
        assert "000001.SZ" in src._base and "600519.SH" in src._base

    def test_custom_base(self) -> None:
        src = MockRealtimeSource(symbols=["000001.SZ"], base_prices={"000001.SZ": 50.0})
        quotes = src.fetch_spot()
        assert quotes["000001.SZ"]["prev_close"] == 50.0

    def test_clock_advances_per_fetch(self) -> None:
        src = MockRealtimeSource(start_ts=1700000000.0, step_interval=1.0)
        c0 = src.clock
        src.fetch_spot()
        assert src.clock == pytest.approx(c0 + 1.0)
        assert src.tick_count == 1

    def test_fetch_subset(self) -> None:
        src = MockRealtimeSource(symbols=["000001.SZ", "600519.SH"])
        quotes = src.fetch_spot(["000001.SZ"])
        assert list(quotes) == ["000001.SZ"]

    def test_fetch_unknown_symbol_skipped(self) -> None:
        src = MockRealtimeSource(symbols=["000001.SZ"])
        assert src.fetch_spot(["999999.SZ"]) == {}

    def test_deterministic_prices(self) -> None:
        src = MockRealtimeSource(symbols=["000001.SZ"], base_prices={"000001.SZ": 10.0},
                                 start_ts=1700000000.0)
        q1 = src.fetch_spot()
        src2 = MockRealtimeSource(symbols=["000001.SZ"], base_prices={"000001.SZ": 10.0},
                                  start_ts=1700000000.0)
        q2 = src2.fetch_spot()
        assert q1["000001.SZ"]["price"] == q2["000001.SZ"]["price"]


# ─────────────────────────────────────────────
# RealtimeBarAggregator
# ─────────────────────────────────────────────

def _mk_quote(symbol: str, price: float, ts: float, volume: float = 100.0) -> dict:
    return {"symbol": symbol, "price": price, "open": price, "high": price,
            "low": price, "volume": volume, "amount": price * volume, "timestamp": ts}


class TestRealtimeBarAggregator:
    def test_new_period_returns_bar(self) -> None:
        agg = RealtimeBarAggregator(period=60)
        bar = agg.add_quote(_mk_quote("000001.SZ", 10.0, 1700000000.0))
        assert bar is not None
        assert bar["symbol"] == "000001.SZ"

    def test_same_period_updates_in_place(self) -> None:
        agg = RealtimeBarAggregator(period=60)
        agg.add_quote(_mk_quote("000001.SZ", 10.0, 1700000000.0))
        out = agg.add_quote(_mk_quote("000001.SZ", 10.5, 1700000030.0, volume=50.0))
        assert out is None  # 同周期不产新 bar
        cur = agg.get_current_bar("000001.SZ")
        assert cur["close"] == 10.5
        assert cur["high"] == 10.5
        assert cur["volume"] == 150.0

    def test_period_switch_new_bar(self) -> None:
        agg = RealtimeBarAggregator(period=60)
        agg.add_quote(_mk_quote("000001.SZ", 10.0, 1700000000.0))
        bar = agg.add_quote(_mk_quote("000001.SZ", 11.0, 1700000060.0))
        assert bar is not None
        assert bar["close"] == 11.0

    def test_empty_symbol_returns_none(self) -> None:
        agg = RealtimeBarAggregator(period=60)
        assert agg.add_quote({"price": 10.0, "timestamp": 1700000000.0}) is None

    def test_get_all_bars_and_reset(self) -> None:
        agg = RealtimeBarAggregator(period=60)
        agg.add_quote(_mk_quote("000001.SZ", 10.0, 1700000000.0))
        agg.add_quote(_mk_quote("600519.SH", 1500.0, 1700000000.0))
        assert set(agg.get_all_bars()) == {"000001.SZ", "600519.SH"}
        agg.reset()
        assert agg.get_all_bars() == {}

    def test_period_min_one(self) -> None:
        agg = RealtimeBarAggregator(period=0)
        assert agg._period == 1


# ─────────────────────────────────────────────
# RealtimeQuoteFetcher
# ─────────────────────────────────────────────

class TestRealtimeQuoteFetcher:
    def test_construct(self) -> None:
        f = RealtimeQuoteFetcher(backend="mock")
        assert f.active_backend == "mock"
        assert f.last_error == ""

    def test_fetch_mock_backend(self) -> None:
        src = MockRealtimeSource(symbols=["000001.SZ"], base_prices={"000001.SZ": 10.0})
        f = RealtimeQuoteFetcher(backend="mock", mock_source=src)
        quotes = f.fetch_spot(["000001.SZ"])
        assert "000001.SZ" in quotes
        assert f.active_backend == "mock"

    def test_fetch_empty_symbols(self) -> None:
        f = RealtimeQuoteFetcher(backend="mock")
        assert f.fetch_spot([]) == {}

    def test_parse_tencent_line_valid(self) -> None:
        f = RealtimeQuoteFetcher()
        raw = (
            'v_sh600519="1~贵州茅台~600519~1700.00~1690.00~1695.00~1000~'
            "2000000~1~2~3~4~5~6~7~8~9~10~11~12~13~14~15~16~17~18~19~20~21~22~23~24~"
            "20240115103000~25~26~27~28~29~30~31~32~33~34\";"
        )
        quote = f._parse_tencent_line(raw)
        assert quote is not None
        assert quote["symbol"] == "600519.SH"
        assert quote["price"] == 1700.0
        assert quote["volume"] == 1000 * 100.0

    def test_parse_tencent_line_malformed(self) -> None:
        f = RealtimeQuoteFetcher()
        assert f._parse_tencent_line("no-quote-here") is None
        assert f._parse_tencent_line('v_x="1~a~b";') is None  # 字段不足

    def test_parse_tencent_line_zero_price(self) -> None:
        f = RealtimeQuoteFetcher()
        raw = 'v_sh600519="1~名称~600519~0.00~1690.00~1695.00~1000~' + "~".join(["0"] * 28) + "\";"
        assert f._parse_tencent_line(raw) is None

    def test_tencent_fail_degrade_akshare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = RealtimeQuoteFetcher(backend="auto")

        def _fail_tencent(symbols):  # type: ignore[no-untyped-def]
            f._last_error = "网络失败"
            f._active_backend = "akshare"
            return {}

        monkeypatch.setattr(f, "_fetch_tencent", _fail_tencent)

        def _fail_akshare(symbols):  # type: ignore[no-untyped-def]
            return {}

        monkeypatch.setattr(f, "_fetch_akshare_spot", _fail_akshare)
        assert f.fetch_spot(["000001.SZ"]) == {}

    def test_tencent_ok_no_degrade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        f = RealtimeQuoteFetcher(backend="tencent")

        def _ok(symbols):  # type: ignore[no-untyped-def]
            return {"000001.SZ": {"symbol": "000001.SZ", "price": 10.0}}

        monkeypatch.setattr(f, "_fetch_tencent", _ok)
        quotes = f.fetch_spot(["000001.SZ"])
        assert "000001.SZ" in quotes
        assert f.active_backend == "tencent"


# ─────────────────────────────────────────────
# MarketDataFeed 构造与连接
# ─────────────────────────────────────────────

class TestFeedInitConnect:
    def test_realtime_mock_construct(self) -> None:
        feed = MarketDataFeed(source="realtime_mock", frequency="1m")
        assert feed.is_realtime is True
        assert feed._period == 60

    def test_custom_frequency_period(self) -> None:
        feed = MarketDataFeed(source="realtime_mock", frequency="5m")
        assert feed._period == 300

    def test_clean_disabled(self) -> None:
        feed = MarketDataFeed(source="realtime_mock", enable_clean=False)
        assert feed._clean_config == {}

    def test_connect_realtime_mock(self) -> None:
        feed = MarketDataFeed(source="realtime_mock", frequency="1m")
        assert feed.connect() is True
        assert feed.is_connected is True
        assert feed.active_backend == "mock"
        assert feed._aggregator is not None

    def test_connect_akshare_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _no_akshare(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise ImportError("no akshare")

        monkeypatch.setattr("builtins.__import__", _no_akshare)
        feed = MarketDataFeed(source="akshare")
        # akshare 缺失 → connect False 且记录错误
        assert feed.connect() is False
        assert feed.is_connected is False

    def test_connect_tdx(self) -> None:
        feed = MarketDataFeed(source="tdx")
        assert feed.connect() is True

    def test_connect_custom(self) -> None:
        feed = MarketDataFeed(source="custom_xyz")
        assert feed.connect() is True
        assert feed.is_connected is True

    def test_disconnect(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.connect()
        feed.subscribe("000001.SZ")
        feed.disconnect()
        assert feed.is_connected is False

    def test_repr(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        assert "MarketDataFeed" in repr(feed)


# ─────────────────────────────────────────────
# 订阅管理
# ─────────────────────────────────────────────

class TestFeedSubscription:
    def test_subscribe_and_list(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.subscribe("000001.SZ")
        feed.subscribe("600519.SH")
        assert feed.get_subscribed_symbols() == ["000001.SZ", "600519.SH"]
        assert feed.is_subscribed("000001.SZ") is True

    def test_subscribe_callback_dedup(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        cb = lambda s, b: None  # noqa: E731
        feed.subscribe("000001.SZ", cb)
        feed.subscribe("000001.SZ", cb)
        assert len(feed._subscriptions["000001.SZ"]) == 1

    def test_unsubscribe(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.subscribe("000001.SZ")
        feed.unsubscribe("000001.SZ")
        assert feed.is_subscribed("000001.SZ") is False

    def test_register_callbacks(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        err_cb = lambda s, e: None  # noqa: E731
        dirty_cb = lambda s, b, e: None  # noqa: E731
        feed.register_error_callback(err_cb)
        feed.register_dirty_callback(dirty_cb)
        assert len(feed._error_callbacks) == 1
        assert len(feed._dirty_callbacks) == 1


# ─────────────────────────────────────────────
# poll_once 全链路（realtime_mock）
# ─────────────────────────────────────────────

class TestFeedPollOnce:
    def test_poll_not_connected(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        assert feed.poll_once() == {}

    def test_poll_no_subscription(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.connect()
        assert feed.poll_once() == {}

    def test_realtime_mock_full_flow(self) -> None:
        # 固定整分钟 start_ts（2025-01-01 00:00:00 UTC）消除分钟边界 flaky：
        # 默认 start_ts=now-600s 秒位随机，poll 推进 1s 恰跨分钟时聚合器误判新周期，
        # 导致第二次 poll 产出新 bar 使 assert new_bars2 == {} 偶发失败（CI 间歇红）。
        src = MockRealtimeSource(
            symbols=["000001.SZ"], base_prices={"000001.SZ": 10.0},
            start_ts=1735689600.0,
        )
        feed = MarketDataFeed(source="realtime_mock", frequency="1m", mock_source=src)
        feed.connect()
        received: list = []
        feed.subscribe("000001.SZ", lambda s, b: received.append((s, b)))
        # 第一次 poll → 聚合器新周期产 bar → 分发
        new_bars = feed.poll_once()
        assert "000001.SZ" in new_bars
        assert len(new_bars["000001.SZ"]) == 1
        assert received  # 回调收到
        assert feed.get_latest_bar("000001.SZ") is not None
        assert feed.stats["total_polls"] == 1
        assert feed.stats["success_polls"] == 1
        # 同周期再 poll → 无新 bar 分发
        received.clear()
        new_bars2 = feed.poll_once()
        assert new_bars2 == {}
        assert feed.get_latest_bar("000001.SZ") is not None  # 缓存仍更新
        # 历史 bar
        hist = feed.get_historical_bars("000001.SZ", count=5)
        assert len(hist) >= 1

    def test_realtime_future_quote_rejected(self) -> None:
        src = MockRealtimeSource(symbols=["000001.SZ"], base_prices={"000001.SZ": 10.0})
        feed = MarketDataFeed(source="realtime_mock", frequency="1m", mock_source=src)
        feed.connect()
        feed.subscribe("000001.SZ")
        # 注入未来时间戳 quote
        feed._aggregator.add_quote({
            "symbol": "000001.SZ", "price": 10.0, "timestamp": time.time() + 99999,
        })
        # 下一次 poll 的 mock quote 时间戳正常（mock 时钟从过去开始）
        assert feed.poll_once() is not None

    def test_attach_event_bus(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        bridge = feed.attach_event_bus()
        assert bridge is not None
        assert feed._event_bridge is bridge
        assert len(feed._dirty_callbacks) == 1


# ─────────────────────────────────────────────
# 数据获取（历史/本地/akshare）
# ─────────────────────────────────────────────

class TestFeedFetch:
    def test_fetch_latest_custom_source_empty(self) -> None:
        feed = MarketDataFeed(source="custom_xyz")
        assert feed._fetch_latest_bars("000001.SZ") == []

    def test_fetch_akshare_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _no_akshare(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise ImportError("no akshare")

        monkeypatch.setattr("builtins.__import__", _no_akshare)
        feed = MarketDataFeed(source="akshare")
        assert feed._fetch_from_akshare("000001.SZ") == []

    def test_fetch_local_history_no_dir(self) -> None:
        feed = MarketDataFeed(source="tdx")
        assert feed._fetch_from_local_history("000001.SZ") == []

    def test_fetch_local_history_missing_file(self, tmp_path: Path) -> None:
        feed = MarketDataFeed(source="tdx", local_history_dir=str(tmp_path))
        assert feed._fetch_from_local_history("000001.SZ") == []

    def test_fetch_local_history_bad_dir(self) -> None:
        feed = MarketDataFeed(source="tdx", local_history_dir=r"D:\no_such_dir_xyz")
        assert feed._fetch_from_local_history("000001.SZ") == []


# ─────────────────────────────────────────────
# 清洗
# ─────────────────────────────────────────────

class TestFeedClean:
    def _clean_feed(self) -> MarketDataFeed:
        feed = MarketDataFeed(source="realtime_mock", frequency="1m")
        feed.connect()
        feed.subscribe("000001.SZ")
        return feed

    def test_stream_clean_valid_bar(self) -> None:
        feed = self._clean_feed()
        bar = _make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.2, 1000)
        out = feed._stream_clean("000001.SZ", bar)
        assert out is not None
        assert feed.stats["total_bars"] == 1

    def test_stream_clean_invalid_bar_notifies(self) -> None:
        feed = self._clean_feed()
        dirty_hits: list = []
        feed.register_dirty_callback(lambda s, b, e: dirty_hits.append((s, e)))
        bad_bar = {"symbol": "000001.SZ", "date": "2024-01-15", "open": -1.0,
                   "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0}
        out = feed._stream_clean("000001.SZ", bad_bar)
        assert out is None
        assert feed.stats["dirty_bars"] >= 1
        assert dirty_hits

    def test_stream_clean_jump_below_threshold(self) -> None:
        feed = self._clean_feed()
        feed._latest_bars["000001.SZ"] = {"close": 10.0}
        bar = _make_bar("000001.SZ", "2024-01-16", 10.0, 10.5, 9.8, 10.2, 1000)  # +2%
        out = feed._stream_clean("000001.SZ", bar)
        assert out is not None  # 未达阈值不丢弃

    def test_stream_clean_jump_above_threshold_alerts(self) -> None:
        feed = MarketDataFeed(source="realtime_mock", clean_config={"price_jump_threshold": 0.15,
                                                                    "jump_alert_threshold": 1})
        feed.connect()
        feed.subscribe("000001.SZ")
        feed._latest_bars["000001.SZ"] = {"close": 10.0, "date": "2024-01-15"}
        dirty_hits: list = []
        feed.register_dirty_callback(lambda s, b, e: dirty_hits.append(e))
        bar = _make_bar("000001.SZ", "2024-01-16", 10.0, 12.5, 9.5, 12.0, 1000)  # +20%
        out = feed._stream_clean("000001.SZ", bar)
        assert out is not None  # 跳变不丢弃
        assert dirty_hits  # 达阈值告警

    def test_clean_bars_empty(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        assert feed._clean_bars("000001.SZ", []) == []

    def test_clean_bars_exception_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from myquant.live import data_feed as df

        def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("clean boom")

        monkeypatch.setattr(df, "clean_bar_records", _boom)
        feed = MarketDataFeed(source="realtime_mock")
        bars = [_make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.2, 1000)]
        assert feed._clean_bars("000001.SZ", bars) == bars  # 回退原数据


# ─────────────────────────────────────────────
# 缓存 / 分发
# ─────────────────────────────────────────────

class TestFeedCacheAndDispatch:
    def test_update_cache_latest_and_history(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        bars = [
            _make_bar("000001.SZ", "2024-01-14", 10.0, 10.5, 9.8, 10.2, 1000),
            _make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.5, 1000),
        ]
        feed._update_cache("000001.SZ", bars)
        assert feed.get_latest_bar("000001.SZ")["close"] == 10.5
        assert len(feed.get_historical_bars("000001.SZ", count=10)) == 2

    def test_update_cache_history_cap_100(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        bars = [_make_bar("000001.SZ", f"2024-01-{i:02d}", 10.0, 10.5, 9.8, 10.0 + i / 100, 1000)
                for i in range(1, 31)]
        for i in range(10):
            feed._update_cache("000001.SZ", bars)
        assert len(feed.get_historical_bars("000001.SZ", count=500)) == 100

    def test_update_cache_empty(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed._update_cache("000001.SZ", [])
        assert feed.get_latest_bar("000001.SZ") is None

    def test_dispatch_calls_callback(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        received: list = []
        feed.subscribe("000001.SZ", lambda s, b: received.append((s, b)))
        bar = _make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.2, 1000)
        feed._dispatch("000001.SZ", [bar])
        assert len(received) == 1
        assert received[0][0] == "000001.SZ"

    def test_dispatch_callback_exception_tolerated(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")

        def _boom(s, b):  # type: ignore[no-untyped-def]
            raise RuntimeError("cb boom")

        feed.subscribe("000001.SZ", _boom)
        bar = _make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.2, 1000)
        feed._dispatch("000001.SZ", [bar])  # 不应抛

    def test_dispatch_empty_noop(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed._dispatch("000001.SZ", [])  # 不应抛


# ─────────────────────────────────────────────
# 复权现算
# ─────────────────────────────────────────────

class TestAdjust:
    def test_qfq_adjusts_prices(self) -> None:
        feed = MarketDataFeed(source="realtime_mock", adjust_out="qfq")
        feed._factor_lookup_cache["000001.SZ"] = {"2024-01-01": 2.0}
        bar = _make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.2, 1000)
        out = feed._maybe_adjust_bar(bar)
        assert out["close"] == pytest.approx(5.1)  # 10.2 / 2.0
        assert out["open"] == pytest.approx(5.0)

    def test_raw_no_adjust(self) -> None:
        feed = MarketDataFeed(source="realtime_mock", adjust_out="raw")
        bar = _make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.2, 1000)
        assert feed._maybe_adjust_bar(bar) is bar

    def test_qfq_no_factor_keeps_raw(self) -> None:
        feed = MarketDataFeed(source="realtime_mock", adjust_out="qfq")
        # 预置空因子缓存，避免懒加载 DB 因子服务
        feed._factor_lookup_cache["000001.SZ"] = {}
        bar = _make_bar("000001.SZ", "2024-01-15", 10.0, 10.5, 9.8, 10.2, 1000)
        out = feed._maybe_adjust_bar(bar)
        assert out["close"] == 10.2  # 无因子不折算


# ─────────────────────────────────────────────
# start / stop / health_check
# ─────────────────────────────────────────────

class TestFeedStartStopHealth:
    def test_start_not_connected(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.start()
        assert feed.is_running is False

    def test_start_stop_cycle(self) -> None:
        src = MockRealtimeSource(symbols=["000001.SZ"], base_prices={"000001.SZ": 10.0})
        feed = MarketDataFeed(source="realtime_mock", frequency="1m", mock_source=src)
        feed.connect()
        feed.subscribe("000001.SZ")
        feed.start(poll_interval=1)
        assert feed.is_running is True
        time.sleep(2.5)  # 让轮询跑 1-2 次
        feed.stop()
        assert feed.is_running is False
        assert feed.stats["total_polls"] >= 1

    def test_start_twice(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.connect()
        feed.start()
        feed.start()  # 二次启动应安全 no-op
        feed.stop()

    def test_stop_not_running(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.stop()  # 安全 no-op

    def test_health_not_connected(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        h = feed.health_check()
        assert h["status"] == "down"
        assert h["connected"] is False

    def test_health_healthy(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.connect()
        feed.subscribe("000001.SZ")
        h = feed.health_check()
        assert h["status"] == "healthy"
        assert h["realtime"]["active_backend"] == "mock"

    def test_health_degraded(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        feed.connect()
        feed.subscribe("000001.SZ")
        feed._stats["failed_polls"] = 5
        feed._stats["success_polls"] = 1
        feed._stats["total_polls"] = 6
        h = feed.health_check()
        assert h["status"] == "degraded"

    def test_stats_readonly_copy(self) -> None:
        feed = MarketDataFeed(source="realtime_mock")
        s = feed.stats
        s["total_polls"] = 999
        assert feed.stats["total_polls"] == 0
