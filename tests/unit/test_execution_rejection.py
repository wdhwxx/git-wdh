# 文件名：tests/unit/test_execution_rejection.py
"""
test_execution_rejection.py - 订单拒绝率监控单元测试（B-5，回写#225）
==========================================================================
验收口径（清单 B-5 行 + P0-2 锁定表交易行）：
  - 订单拒绝率 = 窗口内拒单数/报单数；SLO < 0.1%（LOCKED 0.001）；
  - 拒单率飙升（窗口内超锁定倍数 N 与窗口长度）→ P1 告警；
  - 阈值/窗口取自 P0-2；未配置 N/窗口 → 只执行绝对阈值，不触发飙升告警；
  - 防未来：只消费事件自带已发生时间戳。
"""

from __future__ import annotations

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.execution_stats import (
    METRIC_ORDER_REJECT_RATE,
    METRIC_ORDER_REJECTED_TOTAL,
    METRIC_ORDER_SUBMITTED_TOTAL,
    SURGE_MIN_BASE_SAMPLES,
    ExecutionStatsMonitor,
)


class _FakeAlert:
    def __init__(self) -> None:
        self.calls: list = []

    def alert(self, **kw):
        self.calls.append(kw)
        return 1


class _FakeMetrics:
    def __init__(self) -> None:
        self.gauges: dict = {}

    def set_gauge(self, name, value, labels=None):
        self.gauges[name] = float(value)


def _build(**kw) -> ExecutionStatsMonitor:
    kw.setdefault("alert_manager", _FakeAlert())
    kw.setdefault("metrics_collector", _FakeMetrics())
    return ExecutionStatsMonitor(**kw)


class TestAbsoluteRejectRate:
    """绝对 SLO：拒单率 > 0.1% → P2/WARNING。"""

    def test_reject_over_slo_fires_p2(self):
        am = _FakeAlert()
        mon = ExecutionStatsMonitor(
            reject_rate_max=0.001, rate_window_seconds=60.0,
            alert_manager=am, metrics_collector=_FakeMetrics(),
        )
        t = 1000.0
        # 99 报单 + 1 拒单 = 1.0% > 0.1%
        for i in range(99):
            mon.observe_order_submit(f"o{i}", event_ts=t + i * 0.5)
        mon.observe_order_reject("o99", event_ts=t + 49.0)
        slo = [c for c in am.calls if c["deduplication_key"] == "exec:reject_rate_slo"]
        assert len(slo) == 1
        assert slo[0]["level"] == SeverityLevel.WARNING
        assert slo[0]["category"] == "trade"
        assert slo[0]["details"]["reject_rate"] == pytest.approx(0.01, abs=1e-6)

    def test_reject_under_slo_no_alert(self):
        am = _FakeAlert()
        mon = ExecutionStatsMonitor(
            reject_rate_max=0.001, rate_window_seconds=60.0,
            alert_manager=am, metrics_collector=_FakeMetrics(),
        )
        t = 2000.0
        # 200 报单 + 0 拒单 = 0%
        for i in range(200):
            mon.observe_order_submit(f"o{i}", event_ts=t + i * 0.1)
        slo = [c for c in am.calls if c["deduplication_key"] == "exec:reject_rate_slo"]
        assert slo == []

    def test_window_purge(self):
        """窗口滑动后旧事件剔除，拒单率回落。"""
        am = _FakeAlert()
        mon = ExecutionStatsMonitor(
            reject_rate_max=1.0, rate_window_seconds=60.0,  # 绝对阈值关掉，只看窗口
            alert_manager=am, metrics_collector=_FakeMetrics(),
        )
        # 窗口内 10 报单 2 拒单
        for i in range(8):
            mon.observe_order_submit(f"a{i}", event_ts=1000.0 + i)
        mon.observe_order_reject("r0", event_ts=1001.0)
        mon.observe_order_reject("r1", event_ts=1002.0)
        snap = mon.current_reject_rate()
        assert snap["window_rejected"] == 2
        assert snap["window_submitted"] == 10
        assert snap["reject_rate"] == pytest.approx(0.2, abs=1e-6)


class TestSurgeDetection:
    """飙升检测：r_now > N × r_base → P1/ERROR。"""

    def test_surge_fires_p1(self):
        am = _FakeAlert()
        mon = ExecutionStatsMonitor(
            reject_rate_max=1.0,            # 关掉绝对阈值，只验飙升
            surge_multiple=5.0, surge_window_seconds=60.0,
            alert_manager=am, metrics_collector=_FakeMetrics(),
        )
        # 基线窗 [40,100)：20 报单 1 拒单 = 5%
        base_t = 50.0
        for i in range(19):
            mon.observe_order_submit(f"b{i}", event_ts=base_t + i)
        mon.observe_order_reject("b-rej", event_ts=base_t + 10.0)
        # 当前窗 [100,160]：10 报单 6 拒单 = 60% > 5×5%=25%
        cur_t = 130.0
        for i in range(10):
            mon.observe_order_submit(f"c{i}", event_ts=cur_t + i)
        for i in range(6):
            mon.observe_order_reject(f"c-rej{i}", event_ts=cur_t + i)

        surge = [c for c in am.calls if c["deduplication_key"] == "exec:reject_surge"]
        assert len(surge) >= 1
        assert surge[0]["level"] == SeverityLevel.ERROR
        assert surge[0]["category"] == "trade"
        assert surge[0]["details"]["surge_multiple"] == 5.0

    def test_surge_disabled_when_unconfigured(self):
        """未配置 N/窗口（PENDING_CAL）→ 不触发飙升告警。"""
        am = _FakeAlert()
        mon = ExecutionStatsMonitor(
            reject_rate_max=1.0,            # 关掉绝对阈值
            surge_multiple=None, surge_window_seconds=None,
            alert_manager=am, metrics_collector=_FakeMetrics(),
        )
        # 即便拒单率极高，也不应有飙升告警
        for i in range(SURGE_MIN_BASE_SAMPLES + 5):
            mon.observe_order_submit(f"u{i}", event_ts=5000.0 + i)
        for i in range(10):
            mon.observe_order_reject(f"u-rej{i}", event_ts=5000.0 + 40 + i)
        surge = [c for c in am.calls if c["deduplication_key"] == "exec:reject_surge"]
        assert surge == []
        assert mon.summary()["rejection"]["surge_enabled"] is False

    def test_surge_no_fire_when_baseline_too_small(self):
        """基线样本不足 SURGE_MIN_BASE_SAMPLES → 不判飙升（防冷启动误报）。"""
        am = _FakeAlert()
        mon = ExecutionStatsMonitor(
            reject_rate_max=1.0,
            surge_multiple=5.0, surge_window_seconds=60.0,
            alert_manager=am, metrics_collector=_FakeMetrics(),
        )
        # 基线窗仅 5 报单 0 拒单（<20），即使当前窗飙升也不报
        for i in range(5):
            mon.observe_order_submit(f"b{i}", event_ts=50.0 + i)
        for i in range(10):
            mon.observe_order_submit(f"c{i}", event_ts=130.0 + i)
            mon.observe_order_reject(f"cr{i}", event_ts=130.0 + i)
        surge = [c for c in am.calls if c["deduplication_key"] == "exec:reject_surge"]
        assert surge == []


class TestRejectMetrics:
    """拒单率指标可查（collector）。"""

    def test_metrics_written(self):
        am = _FakeAlert()
        met = _FakeMetrics()
        mon = ExecutionStatsMonitor(
            reject_rate_max=0.001, rate_window_seconds=60.0,
            alert_manager=am, metrics_collector=met,
        )
        for i in range(10):
            mon.observe_order_submit(f"o{i}", event_ts=1000.0 + i)
        mon.observe_order_reject("r0", event_ts=1005.0)
        mon.collect_metrics()
        # 10 报单 + 1 拒单 = 11 窗口事件，拒单率 = 1/11
        assert met.gauges[METRIC_ORDER_REJECT_RATE] == pytest.approx(1 / 11, abs=1e-6)
        assert met.gauges[METRIC_ORDER_SUBMITTED_TOTAL] == 10.0
        assert met.gauges[METRIC_ORDER_REJECTED_TOTAL] == 1.0

    def test_summary_structure(self):
        mon = _build(reject_rate_max=0.001, rate_window_seconds=60.0)
        s = mon.summary()["rejection"]
        assert s["reject_rate_max"] == pytest.approx(0.001, abs=1e-9)
        assert s["window_seconds"] == 60.0
        assert s["surge_enabled"] is False
        assert s["surge_multiple"] is None


class TestConfigValidation:
    """防御式配置校验。"""

    def test_bad_reject_rate_raises(self):
        with pytest.raises(ValueError):
            ExecutionStatsMonitor(reject_rate_max=2.0)  # >1

    def test_bad_surge_multiple_raises(self):
        with pytest.raises(ValueError):
            ExecutionStatsMonitor(surge_multiple=1.0, surge_window_seconds=60.0)

    def test_mismatched_surge_config_raises(self):
        with pytest.raises(ValueError):
            ExecutionStatsMonitor(surge_multiple=3.0, surge_window_seconds=None)
