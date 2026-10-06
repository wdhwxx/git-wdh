# 文件名：tests/unit/test_signal_decay.py
"""
test_signal_decay.py - C-4 信号质量退化检测测试（回写#233）
========================================================================
覆盖：
  1. 三指标计算：后收益滚动 IC（Spearman 秩相关）、信号密度、无效信号占比。
  2. 衰减触发分级预警（显式注入阈值）：
       - IC < ic_threshold → P1/ERROR；
       - 无效占比 > invalid_signal_ratio_max → P1/ERROR；
       - 密度 < density_min → P2/WARNING；
       category=signal_decay、source=monitoring.signal_decay、dedup、details 附值与阈值。
  3. 阈值未配置（PENDING_CAL=None）→ 只计算不告警、不编造数值。
  4. 健康序列（高 IC、低无效、密度达标）不告警。
  5. 防未来：record_pair 拒绝未来 signal_ts 与未来 realized_at（未兑现收益）。
  6. 防御式校验（非有限收益 / 无效占比阈值越界 / IC 窗口<2 / 密度窗<=0）。
  7. 衰减曲线输出：evaluate() 返回含 IC/密度/无效占比三序列 + 阈值，可 JSON 序列化。
  8. set_gauge 动态注册 4 项指标（labels={"group": default}）。

依赖: pytest, myquant.monitoring.signal_decay。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.signal_decay import (
    CATEGORY_SIGNAL_DECAY,
    METRIC_SIGNAL_DECAY_DENSITY,
    METRIC_SIGNAL_DECAY_IC,
    METRIC_SIGNAL_DECAY_INVALID_RATIO,
    METRIC_SIGNAL_DECAY_PAIRS,
    REGISTERED_METRICS,
    SignalDecayMonitor,
)

_BASE: float = 1_700_000_000.0


class _FakeAlert:
    """捕获 alert() 调用的告警管理器桩。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def alert(self, **kwargs: Any) -> int:
        self.calls.append(dict(kwargs))
        return 1


class _FakeMetrics:
    """捕获 set_gauge 的指标桩（labels 并入 key）。"""

    def __init__(self) -> None:
        self.gauges: Dict[str, float] = {}

    def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
        self.gauges[f"{name}:{(labels or {}).get('group', '?')}"] = float(value)


def _make_monitor(
    clock_now: List[float],
    alert: Any = None,
    metrics: Any = None,
    ic_threshold: Any = None,
    density_min: Any = None,
    invalid_max: Any = None,
    ic_window: int = 20,
    density_window: float = 1_000.0,
) -> SignalDecayMonitor:
    """构造固定时钟、阈值可注入的监控器。"""
    return SignalDecayMonitor(
        ic_rolling_window=ic_window,
        ic_threshold=ic_threshold,
        density_min=density_min,
        invalid_signal_ratio_max=invalid_max,
        density_window_seconds=density_window,
        alert_manager=alert if alert is not None else _FakeAlert(),
        metrics_collector=metrics if metrics is not None else _FakeMetrics(),
        clock=lambda: clock_now[0],
    )


def _feed_aligned(mon: SignalDecayMonitor, n: int, base: float, step: float) -> None:
    """喂 n 条 signal_value 与 realized_return 同向（高 IC、低无效）的对。"""
    for i in range(n):
        s = float(i) - (n - 1) / 2.0
        r = 0.5 * s + 0.1
        ts = base + i * step
        mon.record_pair(ts, s, r, realized_at=ts + 1.0)


def _feed_opposite(mon: SignalDecayMonitor, n: int, base: float, step: float) -> None:
    """喂 n 条 signal_value 与 realized_return 反向（IC 转负、高无效）的对。"""
    for i in range(n):
        s = float(i) - (n - 1) / 2.0
        r = -0.5 * s - 0.1
        ts = base + i * step
        mon.record_pair(ts, s, r, realized_at=ts + 1.0)


@pytest.fixture()
def clock_now() -> List[float]:
    return [_BASE + 5_000.0]


class TestMetricsCompute:
    def test_滚动IC_高相关为正(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        _feed_aligned(mon, 20, _BASE, 20.0)
        st = mon.evaluate()
        assert st["ic"] is not None
        assert st["ic"] > 0.9           # 强正相关 → IC≈1
        assert st["ic_window"] == 20

    def test_滚动IC_反向为负(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        _feed_opposite(mon, 20, _BASE, 20.0)
        st = mon.evaluate()
        assert st["ic"] is not None
        assert st["ic"] < -0.9          # 强负相关 → IC≈-1

    def test_样本不足IC为None(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        mon.record_pair(_BASE, 1.0, 0.1, realized_at=_BASE + 1.0)
        st = mon.evaluate()
        assert st["ic"] is None         # 仅 1 条，<2 不出 IC
        assert st["completed_pairs"] == 1

    def test_密度计算(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now, density_window=1_000.0)
        # now=_BASE+5000；窗 (now-1000, now] = (4000,5000]（左开右闭）
        for i in range(10):
            ts = _BASE + 4_100.0 + i * 100.0   # 4100..5000 全在窗内（>4000，<=5000）
            mon.record_pair(ts, float(i), 0.01 * i, realized_at=ts + 1.0)
        st = mon.evaluate()
        assert st["density_count"] == 10
        assert abs(st["density"] - 10.0 / 1_000.0) < 1e-9

    def test_无效信号占比(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        # 10 条看多（s>0）：5 条涨（有效）5 条跌（无效）→ 占比 0.5
        for i in range(10):
            s = 1.0
            r = 0.1 if i < 5 else -0.1   # 后 5 条看多却跌
            ts = _BASE + i * 20.0
            mon.record_pair(ts, s, r, realized_at=ts + 1.0)
        st = mon.evaluate()
        assert st["invalid_ratio"] is not None
        assert abs(st["invalid_ratio"] - 0.5) < 1e-9


class TestDecayAlerts:
    def test_IC低于阈值_P1_ERROR(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_now, alert=am, ic_threshold=0.15)
        _feed_opposite(mon, 20, _BASE, 20.0)   # IC≈-1 < 0.15
        st = mon.evaluate()
        ic_alerts = [c for c in am.calls if c["deduplication_key"] == "signal_decay:ic:default"]
        assert len(ic_alerts) == 1
        assert ic_alerts[0]["level"] == SeverityLevel.ERROR
        assert ic_alerts[0]["category"] == CATEGORY_SIGNAL_DECAY
        assert ic_alerts[0]["source"] == "monitoring.signal_decay"
        assert "ic" in ic_alerts[0]["details"]
        assert "ic_threshold" in ic_alerts[0]["details"]
        assert st["alerts"]["ic_p1"] is True
        assert st["alert_triggered"] is True

    def test_无效占比超限_P1_ERROR(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_now, alert=am, ic_threshold=None,
                            invalid_max=0.4)
        for i in range(10):
            ts = _BASE + i * 20.0
            mon.record_pair(ts, 1.0, -0.1, realized_at=ts + 1.0)  # 全看多却跌
        st = mon.evaluate()
        inv_alerts = [c for c in am.calls if c["deduplication_key"] == "signal_decay:invalid_ratio:default"]
        assert len(inv_alerts) == 1
        assert inv_alerts[0]["level"] == SeverityLevel.ERROR
        assert inv_alerts[0]["details"]["invalid_signal_ratio_max"] == 0.4
        assert st["alerts"]["invalid_p1"] is True

    def test_密度低于下限_P2_WARNING(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_now, alert=am, density_min=0.02,
                            density_window=1_000.0)
        # now=_BASE+5000；窗内仅 5 条 → density=0.005 < 0.02
        for i in range(5):
            ts = _BASE + 4_000.0 + i * 100.0
            mon.record_pair(ts, float(i), 0.01 * i, realized_at=ts + 1.0)
        st = mon.evaluate()
        d_alerts = [c for c in am.calls if c["deduplication_key"] == "signal_decay:density:default"]
        assert len(d_alerts) == 1
        assert d_alerts[0]["level"] == SeverityLevel.WARNING
        assert st["alerts"]["density_p2"] is True


class TestNoAlertWhenHealthyOrPending:
    def test_健康序列不告警(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_now, alert=am, ic_threshold=0.0,
                            density_min=0.001, invalid_max=0.5)
        clock_now[0] = _BASE + 400.0          # 时钟移到数据末尾，使密度窗覆盖健康段
        _feed_aligned(mon, 20, _BASE, 20.0)   # ts=base..base+380，高 IC、零无效、密度达标
        st = mon.evaluate()
        assert am.calls == []
        assert st["alert_triggered"] is False

    def test_阈值未配置不告警不编造(self, clock_now: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_now, alert=am, ic_threshold=None,
                            density_min=None, invalid_max=None)
        _feed_opposite(mon, 20, _BASE, 20.0)   # 严重衰减序列
        st = mon.evaluate()
        assert am.calls == []                   # 不告警
        assert st["ic"] is not None             # 指标照常计算
        assert st["invalid_ratio"] is not None
        assert st["ic_threshold"] is None
        assert st["alert_triggered"] is False


class TestAntiFuture:
    def test_拒绝未来信号时刻(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        with pytest.raises(ValueError):
            mon.record_pair(clock_now[0] + 100.0, 1.0, 0.1,
                            realized_at=clock_now[0] + 101.0)

    def test_拒绝未兑现收益(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        # signal_ts 已发生，但 realized_at 在未来（收益未兑现）→ 拒绝
        with pytest.raises(ValueError):
            mon.record_pair(_BASE, 1.0, 0.1, realized_at=clock_now[0] + 100.0)


class TestDefensive:
    def test_非有限收益拒绝(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        with pytest.raises(ValueError):
            mon.record_pair(_BASE, 1.0, float("nan"))

    def test_无效占比阈值越界拒绝(self, clock_now: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_monitor(clock_now, invalid_max=1.5)
        with pytest.raises(ValueError):
            _make_monitor(clock_now, invalid_max=-0.1)

    def test_IC窗口过小拒绝(self, clock_now: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_monitor(clock_now, ic_window=1)

    def test_密度窗非法拒绝(self, clock_now: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_monitor(clock_now, density_window=0.0)

    def test_非数值入参拒绝(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        with pytest.raises(ValueError):
            mon.record_pair("not-a-ts", 1.0, 0.1)  # type: ignore[arg-type]


class TestDecayCurve:
    def test_曲线三序列且JSON可序列化(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now, ic_threshold=0.5)
        _feed_aligned(mon, 20, _BASE, 20.0)
        st1 = mon.evaluate()
        _feed_opposite(mon, 20, _BASE + 500.0, 100.0)
        clock_now[0] = _BASE + 2_500.0
        st2 = mon.evaluate()
        curve = st2["curve"]
        assert set(curve.keys()) == {"ic", "density", "invalid_ratio"}
        assert len(curve["ic"]) >= 2
        assert len(curve["density"]) >= 2
        assert len(curve["invalid_ratio"]) >= 2
        # 阈值随状态一并输出
        assert st2["thresholds"]["ic_threshold"] == 0.5
        # 可 JSON 序列化
        s = json.dumps(st2)
        assert isinstance(s, str)
        # 两帧 IC 应递减（正常段高 → 衰减段低）
        ics = [fr["ic"] for fr in curve["ic"]]
        assert ics[-1] < ics[0]

    def test_summary未评估返回空曲线结构(self, clock_now: List[float]) -> None:
        mon = _make_monitor(clock_now)
        s = mon.summary()
        assert s["curve"]["ic"] == []
        assert s["curve"]["density"] == []
        assert s["alert_triggered"] is False


class TestMetricsGauges:
    def test_set_gauge动态注册(self, clock_now: List[float]) -> None:
        met = _FakeMetrics()
        mon = _make_monitor(clock_now, metrics=met)
        _feed_aligned(mon, 20, _BASE, 20.0)
        mon.evaluate()
        assert f"{METRIC_SIGNAL_DECAY_IC}:default" in met.gauges
        assert f"{METRIC_SIGNAL_DECAY_DENSITY}:default" in met.gauges
        assert f"{METRIC_SIGNAL_DECAY_INVALID_RATIO}:default" in met.gauges
        assert f"{METRIC_SIGNAL_DECAY_PAIRS}:default" in met.gauges
        # IC 高相关 → gauge 值 > 0.9
        assert met.gauges[f"{METRIC_SIGNAL_DECAY_IC}:default"] > 0.9

    def test_REGISTERED_METRICS声明(self) -> None:
        assert REGISTERED_METRICS == [
            METRIC_SIGNAL_DECAY_IC,
            METRIC_SIGNAL_DECAY_DENSITY,
            METRIC_SIGNAL_DECAY_INVALID_RATIO,
            METRIC_SIGNAL_DECAY_PAIRS,
        ]
