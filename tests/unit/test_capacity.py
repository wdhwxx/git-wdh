# 文件名：tests/unit/test_capacity.py
"""
test_capacity.py - C-8 容量预测模块测试（回写#237）
========================================================================
覆盖：
  1. 数据量增长预测：最小二乘斜率、N 天外推值、剩余可用天数。
  2. 回测/清洗资源占用估算（单位成本=已观测事实；未观测项 None 不编造）。
  3. 策略容量上限：注册策略数 vs 上限，超限预警。
  4. 容量不足 P2 预警（注入阈值）：存储余量不足 / 策略数超限
     （category=capacity、source=monitoring.capacity、dedup、details 附值）。
  5. 阈值未配置（PENDING_CAL=None）→ 只计算不告警、不编造。
  6. 健康状态（余量充足 + 策略数低于上限）→ 不告警。
  7. 容量指标经 set_gauge 进看板（REGISTERED_METRICS 全部可写）。
  8. 防未来：record_data_size 拒绝 timestamp>now 的落库点。
  9. 防御式校验（负数据量 / 负阈值 / 非正样本量 / 空策略名 / 非法容量）。

依赖: pytest, myquant.monitoring.capacity。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.capacity import (
    CATEGORY_CAPACITY,
    DEDUP_STORAGE_HEADROOM,
    DEDUP_STRATEGY_COUNT,
    DEFAULT_PROJECTION_DAYS,
    METRIC_BACKTEST_PEAK_MEMORY,
    METRIC_DATA_SIZE_CURRENT,
    METRIC_DATA_SIZE_GROWTH,
    METRIC_DAYS_REMAINING,
    METRIC_HEADROOM_PCT,
    METRIC_STRATEGY_COUNT,
    REGISTERED_METRICS,
    CapacityMonitor,
    estimate_backtest_usage,
    estimate_clean_usage,
)

_BASE: float = 1_700_000_000.0
_DAY: float = 86_400.0


class _FakeAlert:
    """捕获 alert() 调用的告警管理器桩。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def alert(self, **kwargs: Any) -> int:
        self.calls.append(dict(kwargs))
        return 1


class _FakeMetrics:
    """捕获 set_gauge 的指标桩（无 labels）。"""

    def __init__(self) -> None:
        self.gauges: Dict[str, float] = {}

    def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
        self.gauges[name] = float(value)


def _make_monitor(
    clock_vals: List[float],
    *,
    headroom_min_pct: Any = None,
    strategy_limit: Any = None,
    projection_days: Any = None,
    storage_total_mb: Any = None,
    alert: Any = None,
    metrics: Any = None,
) -> CapacityMonitor:
    """构造固定时钟的监控器（阈值显式注入，避免依赖 settings.yaml 的 PENDING_CAL）。"""
    return CapacityMonitor(
        growth_projection_days=projection_days,
        storage_headroom_min_pct=headroom_min_pct,
        strategy_count_limit=strategy_limit,
        alert_manager=alert if alert is not None else _FakeAlert(),
        metrics_collector=metrics if metrics is not None else _FakeMetrics(),
        clock=lambda: clock_vals[0],
        storage_total_mb=storage_total_mb,
    )


def _ten_day_growing_sizes() -> List[float]:
    """10 天线性增长落库量：100→190 MB（每天 +10 MB）。"""
    return [100.0 + i * 10.0 for i in range(10)]


@pytest.fixture()
def clock_vals() -> List[float]:
    return [_BASE + 9 * _DAY]  # 第 9 天末（10 个点的最后时刻）


def _feed_linear(mon: CapacityMonitor, sizes: List[float]) -> None:
    for i, sz in enumerate(sizes):
        mon.record_data_size(_BASE + i * _DAY, sz)


class TestGrowthProjection:
    def test_增长斜率与外推值(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals, projection_days=30, storage_total_mb=10_000.0)
        _feed_linear(mon, _ten_day_growing_sizes())
        st = mon.poll()
        proj = st["projection"]
        assert proj["point_count"] == 10
        assert proj["latest_size_mb"] == 190.0
        assert abs(proj["growth_mb_per_day"] - 10.0) < 1e-3
        # 外推 30 天：190 + 10*30 = 490
        assert abs(proj["projected_n_days_mb"] - 490.0) < 1e-2

    def test_剩余可用天数(self, clock_vals: List[float]) -> None:
        # 总容量 200 MB，最新 190 MB，斜率 10 MB/天 → (200-190)/10 = 1 天
        mon = _make_monitor(clock_vals, storage_total_mb=200.0)
        _feed_linear(mon, _ten_day_growing_sizes())
        proj = mon.poll()["projection"]
        assert abs(proj["estimated_days_remaining"] - 1.0) < 1e-2

    def test_样本不足不外推(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals, storage_total_mb=10_000.0)
        mon.record_data_size(_BASE, 100.0)  # 单点 → 无法拟合
        proj = mon.poll()["projection"]
        assert proj["growth_mb_per_day"] is None
        assert proj["projected_n_days_mb"] is None
        assert proj["estimated_days_remaining"] is None

    def test_容量未观测则余量与剩余天数None(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals, storage_total_mb=None)  # 未观测总容量
        _feed_linear(mon, _ten_day_growing_sizes())
        proj = mon.poll()["projection"]
        assert proj["headroom_pct"] is None
        assert proj["estimated_days_remaining"] is None

    def test_外推天数缺省回退(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals, projection_days=None, storage_total_mb=10_000.0)
        _feed_linear(mon, _ten_day_growing_sizes())
        proj = mon.poll()["projection"]
        assert proj["projection_days"] == DEFAULT_PROJECTION_DAYS


class TestResourceEstimation:
    def test_回测资源估算线性缩放(self) -> None:
        est = estimate_backtest_usage(
            1_000,
            peak_mb_per_sample=0.002,
            disk_mb_per_sample=0.001,
            seconds_per_sample=0.0005,
        )
        assert est["task"] == "backtest"
        assert est["sample_count"] == 1_000
        assert abs(est["peak_memory_mb"] - 2.0) < 1e-3
        assert abs(est["disk_mb"] - 1.0) < 1e-3
        assert abs(est["estimated_seconds"] - 0.5) < 1e-3
        # 实测 RSS 锚点（复用 memory_watcher）
        assert est["observed_current_rss_mb"] >= 0.0

    def test_清洗资源估算线性缩放(self) -> None:
        est = estimate_clean_usage(500, peak_mb_per_sample=0.001)
        assert est["task"] == "clean"
        assert abs(est["peak_memory_mb"] - 0.5) < 1e-3

    def test_未观测单位成本不编造(self) -> None:
        est = estimate_backtest_usage(100)  # 全部单位成本缺省
        assert est["peak_memory_mb"] is None
        assert est["disk_mb"] is None
        assert est["estimated_seconds"] is None

    def test_估算非法入参(self) -> None:
        with pytest.raises(ValueError):
            estimate_backtest_usage(0)
        with pytest.raises(ValueError):
            estimate_backtest_usage(-5)
        with pytest.raises(ValueError):
            estimate_backtest_usage(10, peak_mb_per_sample=-0.1)


class TestStrategyCapacity:
    def test_策略计数与注销(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        mon.register_strategy("a")
        mon.register_strategy("b")
        mon.register_strategy("a")  # 幂等
        assert mon.poll()["strategy_count"] == 2
        mon.unregister_strategy("a")
        assert mon.poll()["strategy_count"] == 1

    def test_空策略名拒绝(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        with pytest.raises(ValueError):
            mon.register_strategy("")


class TestCapacityAlerting:
    def test_存储余量不足触发P2预警(self, clock_vals: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(
            clock_vals,
            headroom_min_pct=20.0,
            storage_total_mb=200.0,  # 最新 190 → headroom 5% < 20%
            alert=am,
        )
        _feed_linear(mon, _ten_day_growing_sizes())
        st = mon.poll()
        assert st["alert_triggered"] is True
        alerts = [c for c in am.calls if c["deduplication_key"] == DEDUP_STORAGE_HEADROOM]
        assert len(alerts) == 1
        a = alerts[0]
        assert a["level"] == SeverityLevel.WARNING
        assert a["category"] == CATEGORY_CAPACITY
        assert a["source"] == "monitoring.capacity"
        assert "headroom_pct" in a["details"]
        assert a["details"]["headroom_min_pct"] == 20.0

    def test_策略数超限触发P2预警(self, clock_vals: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(clock_vals, strategy_limit=5, storage_total_mb=10_000.0, alert=am)
        _feed_linear(mon, _ten_day_growing_sizes())
        for i in range(6):
            mon.register_strategy(f"s{i}")
        st = mon.poll()
        assert st["strategy_count"] == 6
        alerts = [c for c in am.calls if c["deduplication_key"] == DEDUP_STRATEGY_COUNT]
        assert len(alerts) == 1
        assert alerts[0]["details"]["strategy_count"] == 6
        assert alerts[0]["details"]["strategy_count_limit"] == 5

    def test_未配置阈值不告警不编造(self, clock_vals: List[float]) -> None:
        am = _FakeAlert()
        # 阈值全 None（PENDING_CAL）：余量极低 + 策略超限也不告警
        mon = _make_monitor(
            clock_vals,
            headroom_min_pct=None,
            strategy_limit=None,
            storage_total_mb=200.0,
            alert=am,
        )
        _feed_linear(mon, _ten_day_growing_sizes())
        for i in range(10):
            mon.register_strategy(f"s{i}")
        st = mon.poll()
        assert am.calls == []
        assert st["alert_triggered"] is False
        # 指标照常计算输出
        assert st["projection"]["growth_mb_per_day"] is not None
        assert st["projection"]["headroom_pct"] is not None

    def test_健康状态不告警(self, clock_vals: List[float]) -> None:
        am = _FakeAlert()
        mon = _make_monitor(
            clock_vals,
            headroom_min_pct=20.0,
            strategy_limit=5,
            storage_total_mb=100_000.0,  # 余量 99.8% >> 20%
            alert=am,
        )
        _feed_linear(mon, _ten_day_growing_sizes())
        mon.register_strategy("a")  # 1 < 5
        st = mon.poll()
        assert am.calls == []
        assert st["alert_triggered"] is False


class TestMetricsRegistration:
    def test_全部容量指标进看板(self, clock_vals: List[float]) -> None:
        met = _FakeMetrics()
        mon = _make_monitor(
            clock_vals,
            headroom_min_pct=20.0,
            strategy_limit=5,
            storage_total_mb=200.0,
            metrics=met,
        )
        _feed_linear(mon, _ten_day_growing_sizes())
        mon.note_backtest_peak_memory_mb(12.5)
        mon.poll()
        for name in REGISTERED_METRICS:
            assert name in met.gauges, f"{name} 未写入看板"
        assert abs(met.gauges[METRIC_DATA_SIZE_GROWTH] - 10.0) < 1e-3
        assert met.gauges[METRIC_STRATEGY_COUNT] == 0.0
        assert met.gauges[METRIC_BACKTEST_PEAK_MEMORY] == 12.5
        assert METRIC_DATA_SIZE_CURRENT in met.gauges
        assert METRIC_DAYS_REMAINING in met.gauges
        assert METRIC_HEADROOM_PCT in met.gauges


class TestAntiFuture:
    def test_拒绝未来落库点(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        with pytest.raises(ValueError, match="未来"):
            mon.record_data_size(clock_vals[0] + 100.0, 200.0)


class TestDefensiveValidation:
    def test_负数据量拒绝(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        with pytest.raises(ValueError):
            mon.record_data_size(_BASE, -1.0)

    def test_负余量阈值拒绝(self, clock_vals: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_monitor(clock_vals, headroom_min_pct=-1.0)

    def test_非正策略上限拒绝(self, clock_vals: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_monitor(clock_vals, strategy_limit=0)

    def test_非法容量拒绝(self, clock_vals: List[float]) -> None:
        with pytest.raises(ValueError):
            _make_monitor(clock_vals, storage_total_mb=-10.0)
        mon = _make_monitor(clock_vals)
        with pytest.raises(ValueError):
            mon.set_storage(used_mb=-1.0)

    def test_落库点幂等去重(self, clock_vals: List[float]) -> None:
        mon = _make_monitor(clock_vals)
        mon.record_data_size(_BASE, 100.0)
        mon.record_data_size(_BASE, 105.0)  # 同时刻覆盖
        assert mon.poll()["projection"]["point_count"] == 1
        assert mon.poll()["projection"]["latest_size_mb"] == 105.0
