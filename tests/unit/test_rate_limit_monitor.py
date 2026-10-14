# 文件名：tests/unit/test_rate_limit_monitor.py
"""
test_rate_limit_monitor.py - 交易所限流/频控监控单元测试（A-12，回写#216）
==========================================================================
验收口径（清单 A-12 行）：高频报单触发预警；到阈值前自动暂停开仓。
本测试用注入动作序列 + 固定时钟验证：滑动窗口计数 → P1 预警 → 暂停调用发生；
窗口滑动后自动恢复；查询/撤单按动作分别统计；密钥无关，不碰 quant.db。
"""

from __future__ import annotations

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.rate_limit_monitor import (
    ACTION_CANCEL,
    ACTION_ORDER,
    ACTION_QUERY,
    ExchangeRateLimitMonitor,
)


# ─────────────────────────────────────────────
#  夹具
# ─────────────────────────────────────────────
class _FakeAlert:
    """记录 alert() 调用的假告警管理器。"""

    def __init__(self) -> None:
        self.calls: list = []

    def alert(self, **kw):
        self.calls.append(kw)
        return 1


class _FakeMetrics:
    """记录 inc_counter 调用的假指标采集器。"""

    def __init__(self) -> None:
        self.counts: dict = {}

    def inc_counter(self, name, amount=1.0, labels=None):
        self.counts[name] = self.counts.get(name, 0) + int(amount)


class _Clock:
    """可控时钟。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _make_monitor(clock: _Clock, am: _FakeAlert, met: _FakeMetrics, **kw) -> ExchangeRateLimitMonitor:
    """构造一个测试用监控器：order 上限 5/窗口，warn_ratio 0.8。"""
    defaults = dict(
        window_seconds=60.0, order_per_minute=5, query_per_minute=100,
        cancel_per_minute=100, warn_ratio=0.8,
        alert_manager=am, metrics_collector=met, clock=clock,
    )
    defaults.update(kw)
    return ExchangeRateLimitMonitor(**defaults)


class TestSlidingWindowCount:
    """滑动窗口计数（按动作类型分别统计）。"""

    def test_窗口内计数正确(self) -> None:
        clock = _Clock(1000.0)
        mon = _make_monitor(clock, _FakeAlert(), _FakeMetrics())
        for i in range(3):
            mon.observe_order(ts=1000.0 + i)
        counts = mon.window_counts(now=1002.0)
        assert counts[ACTION_ORDER] == 3

    def test_窗口外样本被剔除(self) -> None:
        clock = _Clock(1000.0)
        mon = _make_monitor(clock, _FakeAlert(), _FakeMetrics())
        for i in range(3):
            mon.observe_order(ts=1000.0 + i)   # 样本落在 1000~1002
        # 评估时刻推进到 1100.0（> 1000+60 窗口）→ 老样本全部剔除
        counts = mon.window_counts(now=1100.0)
        assert counts[ACTION_ORDER] == 0

    def test_查询与撤单窗口相互隔离(self) -> None:
        clock = _Clock(1000.0)
        mon = _make_monitor(clock, _FakeAlert(), _FakeMetrics())
        for i in range(2):
            mon.observe_query(ts=1000.0 + i)
        mon.observe_cancel(ts=1001.0)
        counts = mon.window_counts(now=1002.0)
        assert counts[ACTION_QUERY] == 2
        assert counts[ACTION_CANCEL] == 1
        assert counts[ACTION_ORDER] == 0


class TestWarnAndPause:
    """接近阈值 → P1 预警 + 到阈值前自动暂停开仓（暂停调用发生）。"""

    def test_未达预警线不告警不暂停(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        pause_calls: list = []
        mon = _make_monitor(clock, am, _FakeMetrics())
        mon.attach_pause_hook(lambda reason: pause_calls.append(reason))
        for i in range(3):  # order 上限 5，0.8×5=4；3 < 4
            mon.observe_order(ts=1000.0 + i)
        assert am.calls == []
        assert not mon.paused
        assert pause_calls == []

    def test_达预警线触发P1预警且暂停调用发生(self) -> None:
        """注入动作序列到第 4 笔（0.8×5=4）→ P1 预警 + pause_hook 被调用。"""
        clock = _Clock(1000.0)
        am = _FakeAlert()
        met = _FakeMetrics()
        pause_calls: list = []
        mon = _make_monitor(clock, am, met)
        mon.attach_pause_hook(lambda reason: pause_calls.append(reason))
        for i in range(4):  # 4 笔 = 80%
            mon.observe_order(ts=1000.0 + i)
        warn = [c for c in am.calls if c["deduplication_key"] == "ratelimit:order:warn"]
        assert len(warn) == 1
        assert warn[0]["level"] == SeverityLevel.WARNING
        assert warn[0]["category"] == "rate_limit"
        # 到阈值前自动暂停开仓：暂停钩子被调用且留痕
        assert len(pause_calls) == 1
        assert mon.paused is True
        # 告警 details 携带留痕信息（paused=True，不含任何密钥类敏感信息）
        assert warn[0]["details"]["paused"] is True
        assert warn[0]["details"]["window_count"] == 4

    def test_达上限触发ERROR告警(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        mon = _make_monitor(clock, am, _FakeMetrics())
        for i in range(5):  # 5 笔 = 100% 上限
            mon.observe_order(ts=1000.0 + i)
        limit = [c for c in am.calls if c["deduplication_key"] == "ratelimit:order:limit"]
        assert len(limit) == 1
        assert limit[0]["level"] == SeverityLevel.ERROR

    def test_窗口回落自动恢复开仓(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        pause_calls: list = []
        resume_calls: list = []
        mon = _make_monitor(clock, am, _FakeMetrics())
        mon.attach_pause_hook(lambda r: pause_calls.append(r))
        mon.attach_resume_hook(lambda: resume_calls.append(1))
        for i in range(4):  # 触发暂停
            mon.observe_order(ts=1000.0 + i)
        assert mon.paused is True
        # 时间推进超过窗口 → 老样本剔除，压力回落
        clock.now = 1100.0
        mon.evaluate(now=1100.0)
        assert mon.paused is False
        assert len(resume_calls) == 1
        # 恢复时发 INFO
        assert any(c["level"] == SeverityLevel.INFO for c in am.calls)


class TestMetrics:
    """限流累计指标进 /api/metrics。"""

    def test_动作累计计数指标递增(self) -> None:
        clock = _Clock(1000.0)
        met = _FakeMetrics()
        mon = _make_monitor(clock, _FakeAlert(), met)
        for _ in range(4):
            mon.observe_order()
        mon.observe_query()
        mon.observe_cancel()
        assert met.counts["rate_limit_orders_total"] == 4
        assert met.counts["rate_limit_queries_total"] == 1
        assert met.counts["rate_limit_cancels_total"] == 1


class TestDefensive:
    """防御式校验。"""

    def test_非法窗口抛错(self) -> None:
        with pytest.raises(ValueError):
            ExchangeRateLimitMonitor(window_seconds=0, alert_manager=_FakeAlert())

    def test_非法上限抛错(self) -> None:
        with pytest.raises(ValueError):
            ExchangeRateLimitMonitor(order_per_minute=-1, alert_manager=_FakeAlert())

    def test_非法warn_ratio抛错(self) -> None:
        with pytest.raises(ValueError):
            ExchangeRateLimitMonitor(warn_ratio=1.5, alert_manager=_FakeAlert())

    def test_未知动作类型抛错(self) -> None:
        mon = _make_monitor(_Clock(), _FakeAlert(), _FakeMetrics())
        with pytest.raises(ValueError):
            mon._observe("bogus", ts=1000.0)

    def test_pause_hook必须可调用(self) -> None:
        mon = _make_monitor(_Clock(), _FakeAlert(), _FakeMetrics())
        with pytest.raises(ValueError):
            mon.attach_pause_hook(123)  # type: ignore[arg-type]

    def test_pause_hook异常不阻断(self) -> None:
        """pause_hook 抛异常时已留痕告警，不向上炸断检测。"""
        clock = _Clock(1000.0)
        am = _FakeAlert()
        mon = _make_monitor(clock, am, _FakeMetrics())

        def boom(reason):
            raise RuntimeError("hook boom")

        mon.attach_pause_hook(boom)
        for i in range(4):
            mon.observe_order(ts=1000.0 + i)  # 不应抛异常
        assert mon.paused is True
        assert any(c["deduplication_key"] == "ratelimit:order:warn" for c in am.calls)

    def test_阈值从slo读取(self) -> None:
        """未显式传阈值时读 monitoring.slo.exchange_rate_limit（R-04）。"""
        mon = ExchangeRateLimitMonitor(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        assert mon._window == 60.0
        assert mon._limits[ACTION_ORDER] == 300
        assert mon._warn_ratio == 0.8
