# 文件名：tests/unit/test_execution_stats.py
"""
test_execution_stats.py - 执行链路延迟/滑点统计单元测试（B-4，回写#224）
==========================================================================
验收口径（清单 B-4 行）：
  - 信号→下单→成交三段延迟 P99 统计；滑点=成交价 vs 信号价（买/卖分列）；
  - 订单发送→ACK P99 < 20ms（真源）；超 SLI → P2 告警；
  - POLL 提供 collect_metrics()/summary() 供看板；事件驱动记录样本；
  - 样本不足语义：<P99_MIN_SAMPLES 不出 P99（返回 None）。
本测试注入伪造事件时间戳样本验证统计正确性，不连真实 broker/event_bus。
"""

from __future__ import annotations

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.execution_stats import (
    METRIC_ACK_P99,
    METRIC_SIGNAL_TO_FILL_P99,
    METRIC_SLIPPAGE_BUY_PCT,
    METRIC_SLIPPAGE_SELL_PCT,
    P99_MIN_SAMPLES,
    SIDE_BUY,
    SIDE_SELL,
    ExecutionStatsMonitor,
)


# ─────────────────────────────────────────────
#  夹具
# ─────────────────────────────────────────────
class _FakeAlert:
    """记录 alert() 调用的假告警管理器（不去重，便于断言触发次数）。"""

    def __init__(self) -> None:
        self.calls: list = []

    def alert(self, **kw):
        self.calls.append(kw)
        return 1


class _FakeMetrics:
    """记录 set_gauge 调用的假指标采集器。"""

    def __init__(self) -> None:
        self.gauges: dict = {}

    def set_gauge(self, name, value, labels=None):
        self.gauges[name] = float(value)


def _make_monitor(**kw) -> ExecutionStatsMonitor:
    """构造默认监控器：注入假告警/假指标，关闭飙升（避免干扰）。"""
    kw.setdefault("alert_manager", _FakeAlert())
    kw.setdefault("metrics_collector", _FakeMetrics())
    return ExecutionStatsMonitor(**kw)


def _feed_roundtrip(mon: ExecutionStatsMonitor, n: int, ack_ms: float = 5.0,
                    slip_buy_pct: float = 0.1, t0: float = 1000.0) -> None:
    """
    注入 n 笔完整 signal→submit→fill 往返样本。

    ACK 延迟恒为 ack_ms 毫秒；买单滑点恒为 slip_buy_pct 百分比（买贵）。
    """
    for i in range(n):
        sid = f"sig-{i}"
        oid = f"ord-{i}"
        signal_ts = t0 + i * 0.1
        send_ts = signal_ts
        ack_ts = signal_ts + ack_ms / 1000.0
        fill_ts = signal_ts + 0.5
        mon.observe_signal(sid, event_ts=signal_ts)
        mon.observe_order_submit(
            oid, event_ts=ack_ts, signal_id=sid, send_ts=send_ts,
            side=SIDE_BUY, ref_price=10.0,
        )
        mon.observe_order_fill(
            oid, trade_id=f"trd-{i}", event_ts=fill_ts,
            side=SIDE_BUY, fill_price=10.0 * (1 + slip_buy_pct / 100.0), ref_price=10.0,
        )


# ─────────────────────────────────────────────
#  测试用例
# ─────────────────────────────────────────────
class TestLatencyP99:
    """三段延迟 P99 统计。"""

    def test_ack_p99_constant(self):
        """ACK 延迟恒为 5ms（>=30 样本），P99=5ms。"""
        mon = _make_monitor()
        _feed_roundtrip(mon, n=P99_MIN_SAMPLES, ack_ms=5.0)
        lat = mon.summary()["latency"]["send_to_ack_ms"]
        assert lat["count"] == P99_MIN_SAMPLES
        assert lat["p99"] == pytest.approx(5.0, abs=1e-6)
        assert lat["mean"] == pytest.approx(5.0, abs=1e-6)

    def test_signal_to_submit_segment(self):
        """信号→下单段：ack_ts - signal_ts = ack_ms（发送=信号时刻）。"""
        mon = _make_monitor()
        _feed_roundtrip(mon, n=P99_MIN_SAMPLES, ack_ms=7.0)
        seg = mon.summary()["latency"]["signal_to_submit_ms"]
        # ack_ts = signal_ts + 7ms
        assert seg["p99"] == pytest.approx(7.0, abs=1e-6)

    def test_submit_to_fill_segment(self):
        """下单→成交段：fill_ts - ack_ts = 500ms - ack_ms。"""
        mon = _make_monitor()
        _feed_roundtrip(mon, n=P99_MIN_SAMPLES, ack_ms=20.0)
        seg = mon.summary()["latency"]["submit_to_fill_ms"]
        # fill_ts=signal+0.5s, ack_ts=signal+0.02s → 480ms
        assert seg["p99"] == pytest.approx(480.0, abs=1e-6)

    def test_signal_to_fill_end_to_end(self):
        """端到端 信号→成交 = 500ms。"""
        mon = _make_monitor()
        _feed_roundtrip(mon, n=P99_MIN_SAMPLES, ack_ms=10.0)
        seg = mon.summary()["latency"]["signal_to_fill_ms"]
        assert seg["p99"] == pytest.approx(500.0, abs=1e-6)

    def test_p99_nearest_rank(self):
        """P99 最近秩法：构造 100 个递增样本（1..100ms），P99=第 99 小值=99ms。"""
        mon = _make_monitor()
        for i in range(100):
            mon.observe_order_submit(
                f"p{i}", event_ts=1000.0 + i * 0.1,
                send_ts=1000.0 + i * 0.1 - (i + 1) / 1000.0,  # ack-send = i+1 ms
            )
        p99 = mon.summary()["latency"]["send_to_ack_ms"]["p99"]
        assert p99 == pytest.approx(99.0, abs=1e-6)


class TestSmallSampleSemantics:
    """样本不足语义：<P99_MIN_SAMPLES 不出 P99，均值仍出。"""

    def test_p99_none_when_below_min(self):
        mon = _make_monitor()
        for i in range(P99_MIN_SAMPLES - 1):
            mon.observe_order_submit(
                f"s{i}", event_ts=2000.0 + i, send_ts=2000.0 + i - 0.01,
            )
        lat = mon.summary()["latency"]["send_to_ack_ms"]
        assert lat["p99"] is None
        assert lat["count"] == P99_MIN_SAMPLES - 1
        assert lat["mean"] == pytest.approx(10.0, abs=1e-6)

    def test_exact_boundary_includes_min(self):
        """恰好 P99_MIN_SAMPLES 个样本 → 出 P99。"""
        mon = _make_monitor()
        for i in range(P99_MIN_SAMPLES):
            mon.observe_order_submit(
                f"b{i}", event_ts=3000.0 + i, send_ts=3000.0 + i - 0.02,
            )
        lat = mon.summary()["latency"]["send_to_ack_ms"]
        assert lat["p99"] == pytest.approx(20.0, abs=1e-6)


class TestSlippage:
    """滑点：买/卖分列，符号口径。"""

    def test_buy_slippage_sign(self):
        """买单：成交价 10.1 vs 参考 10.0 → +1%（买贵=正）。"""
        mon = _make_monitor()
        for i in range(5):
            mon.observe_order_fill(
                f"f{i}", trade_id=f"t{i}", event_ts=1000.0 + i,
                side=SIDE_BUY, fill_price=10.1, ref_price=10.0,
            )
        slip = mon.summary()["slippage"]["buy_pct"]
        assert slip["count"] == 5
        assert slip["mean"] == pytest.approx(1.0, abs=1e-6)

    def test_sell_slippage_sign(self):
        """卖单：成交价 9.9 vs 参考 10.0 → -1%（卖低=负）。"""
        mon = _make_monitor()
        for i in range(5):
            mon.observe_order_fill(
                f"f{i}", trade_id=f"t{i}", event_ts=1000.0 + i,
                side=SIDE_SELL, fill_price=9.9, ref_price=10.0,
            )
        slip = mon.summary()["slippage"]["sell_pct"]
        assert slip["count"] == 5
        assert slip["mean"] == pytest.approx(-1.0, abs=1e-6)

    def test_buy_sell_separated(self):
        """买/卖样本互不串列。"""
        mon = _make_monitor()
        for i in range(5):
            mon.observe_order_fill(
                f"b{i}", trade_id=f"tb{i}", event_ts=1000.0 + i,
                side=SIDE_BUY, fill_price=10.2, ref_price=10.0,
            )
            mon.observe_order_fill(
                f"s{i}", trade_id=f"ts{i}", event_ts=1000.0 + i,
                side=SIDE_SELL, fill_price=9.8, ref_price=10.0,
            )
        slip = mon.summary()["slippage"]
        assert slip["buy_pct"]["mean"] == pytest.approx(2.0, abs=1e-6)
        assert slip["sell_pct"]["mean"] == pytest.approx(-2.0, abs=1e-6)

    def test_missing_price_skipped(self):
        """缺成交价或参考价 → 防御式跳过该滑点样本。"""
        mon = _make_monitor()
        mon.observe_order_fill("x", trade_id="tx", event_ts=1000.0,
                               side=SIDE_BUY, fill_price=None, ref_price=10.0)
        mon.observe_order_fill("y", trade_id="ty", event_ts=1001.0,
                               side=SIDE_BUY, fill_price=10.0, ref_price=0.0)
        slip = mon.summary()["slippage"]["buy_pct"]
        assert slip["count"] == 0


class TestAckAlert:
    """ACK P99 超 20ms（真源）→ P2/WARNING。"""

    def test_ack_over_limit_fires_p2(self):
        am = _FakeAlert()
        met = _FakeMetrics()
        mon = ExecutionStatsMonitor(
            ack_limit_ms=20.0, alert_manager=am, metrics_collector=met,
        )
        # 30 笔，ACK 延迟恒 30ms（>20）
        for i in range(P99_MIN_SAMPLES):
            mon.observe_order_submit(
                f"a{i}", event_ts=1000.0 + i * 0.1,
                send_ts=1000.0 + i * 0.1 - 0.03,
            )
        out = mon.collect_metrics()
        p2 = [c for c in am.calls if c["deduplication_key"] == "exec:ack_delay_p99"]
        assert len(p2) == 1
        assert p2[0]["level"] == SeverityLevel.WARNING
        assert p2[0]["category"] == "trade"
        assert out["ack_alert_fired"] is True
        assert met.gauges[METRIC_ACK_P99] == pytest.approx(30.0, abs=1e-6)

    def test_ack_under_limit_no_alert(self):
        am = _FakeAlert()
        mon = ExecutionStatsMonitor(ack_limit_ms=20.0, alert_manager=am,
                                   metrics_collector=_FakeMetrics())
        for i in range(P99_MIN_SAMPLES):
            mon.observe_order_submit(
                f"a{i}", event_ts=1000.0 + i * 0.1,
                send_ts=1000.0 + i * 0.1 - 0.005,  # 5ms < 20
            )
        mon.collect_metrics()
        p2 = [c for c in am.calls if c["deduplication_key"] == "exec:ack_delay_p99"]
        assert p2 == []


class TestCollectMetrics:
    """POLL 采集：指标进 collector。"""

    def test_collect_writes_gauges(self):
        am = _FakeAlert()
        met = _FakeMetrics()
        mon = ExecutionStatsMonitor(ack_limit_ms=20.0, alert_manager=am, metrics_collector=met)
        _feed_roundtrip(mon, n=P99_MIN_SAMPLES, ack_ms=5.0, slip_buy_pct=0.2)
        out = mon.collect_metrics()
        assert METRIC_ACK_P99 in met.gauges
        assert METRIC_SIGNAL_TO_FILL_P99 in met.gauges
        assert METRIC_SLIPPAGE_BUY_PCT in met.gauges
        # 无卖单样本时 sell_pct 为 None，按设计不下发（指标缺省）
        assert METRIC_SLIPPAGE_SELL_PCT not in met.gauges
        assert out["slippage_buy_pct"] == pytest.approx(0.2, abs=1e-6)
        assert out["slippage_sell_pct"] is None


class TestAntiFuture:
    """防未来：时序异常样本被丢弃，只消费已发生时间戳。"""

    def test_ack_before_send_dropped(self):
        mon = _make_monitor()
        mon.observe_order_submit("fut1", event_ts=4000.0, send_ts=4001.0)  # ack<send
        lat = mon.summary()["latency"]["send_to_ack_ms"]
        assert lat["count"] == 0

    def test_fill_before_ack_dropped_segment(self):
        """成交早于 ACK → 不出下单→成交段样本。"""
        mon = _make_monitor()
        mon.observe_order_submit("o1", event_ts=5000.0)
        mon.observe_order_fill("o1", trade_id="t1", event_ts=4999.0)  # fill<ack
        seg = mon.summary()["latency"]["submit_to_fill_ms"]
        assert seg["count"] == 0
