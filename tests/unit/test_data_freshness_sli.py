# 文件名：tests/unit/test_data_freshness_sli.py
"""
test_data_freshness_sli.py - 数据新鲜度 SLI 单元测试（B-7，回写#227）
==========================================================================
验收口径（施工总清单 B-7 行）：数据新鲜度指标可查，缺口/延迟超阈值告警；
阈值取自 P0-2 锁定清单（freshness<1min=60s LOCKED、缺口=0 LOCKED）。

本测试用注入时间戳快照 + 固定时钟 + 假告警/假采集器验证：
  - 注入新鲜/过期数据 → 指标正确 + 过期按 60s 阈值分级告警；
  - 缺口 > 0 → ERROR(P1) 告警；每日缺口按日聚合正确；
  - 因子/财务同步延迟未配置(None) → 不告警不编造；注入配置超阈 → WARNING 告警；
  - 阈值引用真源：构造不同 slo.data 配置 → 行为跟随；
  - 防未来：最新时间戳在未来 → 滞后截断 0、不误告警。
不碰 quant.db。
"""

from __future__ import annotations

import pytest

from myquant.core.constants import SeverityLevel
import myquant.monitoring.data_quality_explorer as dqe
from myquant.monitoring.data_quality_explorer import (
    DataFreshnessSli,
    METRIC_FRESHNESS_LAG_SECONDS,
    METRIC_DATA_GAP_COUNT,
    METRIC_FACTOR_SYNC_DELAY_SECONDS,
    METRIC_FINANCIAL_SYNC_DELAY_SECONDS,
    FRESHNESS_ERROR_MULTIPLE,
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
    """记录 set_gauge 调用的假指标采集器。"""

    def __init__(self) -> None:
        self.gauges: dict = {}

    def set_gauge(self, name, value, labels=None):
        key = (name, tuple(sorted((labels or {}).items())))
        self.gauges[key] = float(value)


class _Clock:
    """可控时钟。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _monkeypatch_slo_data(monkeypatch, data: dict) -> None:
    """让 get_monitoring_config() 返回指定的 slo.data 块（R-04 真源行为跟随）。"""
    monkeypatch.setattr(
        dqe, "get_monitoring_config", lambda: {"slo": {"data": dict(data)}}
    )


# ─────────────────────────────────────────────
#  1. 新鲜度度量：注入新鲜/过期数据 → 指标 + 告警(60s)
# ─────────────────────────────────────────────
class TestFreshnessLag:
    def test_新鲜数据指标正确且不告警(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        met = _FakeMetrics()
        # 最新数据 ts=990 → lag=10 < 60
        sli = DataFreshnessSli(
            latest_timestamp_provider=lambda: {"SH600000:1d": 990.0},
            alert_manager=am, metrics_collector=met, clock=clock,
        )
        st = sli.poll()
        assert st["latest_lag_seconds"]["SH600000:1d"] == 10.0
        assert am.calls == []  # 新鲜，不告警
        # 指标写入（带 data_key 标签）
        assert met.gauges[(METRIC_FRESHNESS_LAG_SECONDS, (("data_key", "SH600000:1d"),))] == 10.0

    def test_过期数据超阈触发WARNING(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        # ts=900 → lag=100，介于 60~120(2×60) → WARNING
        sli = DataFreshnessSli(
            latest_timestamp_provider=lambda: {"SH600000:1d": 900.0},
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.poll()
        hits = [c for c in am.calls if c["deduplication_key"] == "freshness:SH600000:1d"]
        assert len(hits) == 1
        assert hits[0]["level"] == SeverityLevel.WARNING
        assert hits[0]["category"] == "data_quality"
        assert hits[0]["details"]["lag_seconds"] == 100.0

    def test_严重过期触发ERROR(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        # ts=800 → lag=200 > 120(2×60) → ERROR(P1)
        sli = DataFreshnessSli(
            latest_timestamp_provider=lambda: {"SH600000:1d": 800.0},
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.poll()
        hits = [c for c in am.calls if c["deduplication_key"] == "freshness:SH600000:1d"]
        assert len(hits) == 1
        assert hits[0]["level"] == SeverityLevel.ERROR

    def test_按标的数据类分别统计(self) -> None:
        clock = _Clock(1000.0)
        sli = DataFreshnessSli(
            latest_timestamp_provider=lambda: {
                "SH600000:1d": 995.0,   # lag=5  新鲜
                "000001.SZ:tick": 900.0,  # lag=100 过期
            },
            alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics(), clock=clock,
        )
        lag = sli.latest_lag_seconds()
        assert lag["SH600000:1d"] == 5.0
        assert lag["000001.SZ:tick"] == 100.0

    def test_无数据场景防御不告警不编造(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        sli = DataFreshnessSli(
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )  # 未注入 provider → 空快照
        st = sli.poll()
        assert st["latest_lag_seconds"] == {}
        assert am.calls == []


# ─────────────────────────────────────────────
#  2/3. 缺口统计 + 缺口>0 → ERROR(P1)
# ─────────────────────────────────────────────
class TestDataGap:
    def test_缺口大于0触发ERROR告警(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        sli = DataFreshnessSli(
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.record_gap("2026-10-06", 1)  # gap_allowed=0(LOCKED) → 任何缺口即 P1
        sli.poll()
        hits = [c for c in am.calls if c["deduplication_key"] == "data_gap:exceeded"]
        assert len(hits) == 1
        assert hits[0]["level"] == SeverityLevel.ERROR
        assert hits[0]["details"]["gap_count"] == 1

    def test_每日缺口聚合正确(self) -> None:
        clock = _Clock(1000.0)
        sli = DataFreshnessSli(
            alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.record_gap("2026-10-05", 2)
        sli.record_gap("2026-10-06", 3)
        stats = sli.daily_gap_stats()
        assert stats == {"2026-10-05": 2, "2026-10-06": 3}
        # 当前评估缺口 = 最新日期键（字典序最大）= 3
        assert sli.current_gap_count() == 3

    def test_gap_provider与登记值合并(self) -> None:
        clock = _Clock(1000.0)
        sli = DataFreshnessSli(
            gap_provider=lambda: {"2026-10-06": 1},
            alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.record_gap("2026-10-06", 2)  # 同日相加 → 3
        assert sli.current_gap_count() == 3

    def test_gap_count指标写入(self) -> None:
        clock = _Clock(1000.0)
        met = _FakeMetrics()
        sli = DataFreshnessSli(
            alert_manager=_FakeAlert(), metrics_collector=met, clock=clock,
        )
        sli.record_gap("2026-10-06", 4)
        sli.poll()
        assert met.gauges[(METRIC_DATA_GAP_COUNT, ())] == 4.0

    def test_无缺口不告警(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        sli = DataFreshnessSli(
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.poll()  # 无缺口登记
        assert not any(c["deduplication_key"] == "data_gap:exceeded" for c in am.calls)


# ─────────────────────────────────────────────
#  4. 因子/财务同步延迟：未配置不告警 / 注入配置超阈告警
# ─────────────────────────────────────────────
class TestSyncDelay:
    def test_未配置延迟维度不告警不编造(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        met = _FakeMetrics()
        # 默认 factor/financial 阈值来自真源 = None(PENDING_CAL)
        sli = DataFreshnessSli(
            alert_manager=am, metrics_collector=met, clock=clock,
        )
        sli.record_factor_sync(ts=0.0)       # 很久之前，但阈值未配置
        sli.record_financial_sync(ts=0.0)
        st = sli.poll()
        assert am.calls == []  # 未配置 → 不告警
        assert st["factor_sync_delay_seconds"] is None  # 不编造数值
        assert st["financial_sync_delay_seconds"] is None
        # 未配置维度不写延迟指标
        assert not any(k[0] == METRIC_FACTOR_SYNC_DELAY_SECONDS for k in met.gauges)

    def test_因子延迟注入配置超阈触发WARNING(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        met = _FakeMetrics()
        sli = DataFreshnessSli(
            factor_sync_delay_max_seconds=10.0,
            alert_manager=am, metrics_collector=met, clock=clock,
        )
        sli.record_factor_sync(ts=900.0)  # delay=100 > 10
        st = sli.poll()
        hits = [c for c in am.calls if c["deduplication_key"] == "factor_sync:delay"]
        assert len(hits) == 1
        assert hits[0]["level"] == SeverityLevel.WARNING
        assert st["factor_sync_delay_seconds"] == 100.0
        assert met.gauges[(METRIC_FACTOR_SYNC_DELAY_SECONDS, ())] == 100.0

    def test_财务延迟注入配置超阈触发WARNING(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        sli = DataFreshnessSli(
            financial_sync_delay_max_seconds=50.0,
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.record_financial_sync(ts=940.0)  # delay=60 > 50
        st = sli.poll()
        hits = [c for c in am.calls if c["deduplication_key"] == "financial_sync:delay"]
        assert len(hits) == 1
        assert st["financial_sync_delay_seconds"] == 60.0

    def test_延迟未超阈不告警(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        sli = DataFreshnessSli(
            factor_sync_delay_max_seconds=1000.0,
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.record_factor_sync(ts=990.0)  # delay=10 < 1000
        sli.poll()
        assert not any(c["deduplication_key"] == "factor_sync:delay" for c in am.calls)


# ─────────────────────────────────────────────
#  5. 阈值引用真源：构造不同 slo.data 配置 → 行为跟随
# ─────────────────────────────────────────────
class TestThresholdFromSource:
    def test_默认阈值读真源LOCKED(self) -> None:
        """未显式传阈值时读 monitoring.slo.data（freshness=60/gap=0 LOCKED；同步=None）。"""
        sli = DataFreshnessSli(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        assert sli._freshness_max == 60.0
        assert sli._gap_allowed == 0
        assert sli._factor_delay_max is None
        assert sli._financial_delay_max is None

    def test_不同slo配置行为跟随(self, monkeypatch) -> None:
        """构造不同 slo.data → 阈值跟随，且告警行为随之改变。"""
        _monkeypatch_slo_data(monkeypatch, {
            "freshness_max_seconds": 30,   # 收紧到 30s
            "gap_allowed": 1,              # 放宽到允许 1 个缺口
            "factor_sync_delay_max_seconds": 100,
            "financial_sync_delay_max_seconds": None,
        })
        clock = _Clock(1000.0)
        am = _FakeAlert()
        sli = DataFreshnessSli(
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        assert sli._freshness_max == 30.0
        assert sli._gap_allowed == 1
        assert sli._factor_delay_max == 100.0
        assert sli._financial_delay_max is None

        # 行为跟随：lag=40 > 30 → 告警（旧 60s 阈值本不告警）
        sli._latest_provider = lambda: {"K:1d": 960.0}  # lag=40
        sli.poll()
        assert any(c["deduplication_key"] == "freshness:K:1d" for c in am.calls)

        # 缺口行为跟随：gap=1 <= gap_allowed=1 → 不告警；gap=2 才告警
        am.calls.clear()
        sli._daily_gaps.clear()
        sli.record_gap("2026-10-06", 1)
        sli._latest_provider = lambda: {}
        sli.poll()
        assert not any(c["deduplication_key"] == "data_gap:exceeded" for c in am.calls)
        sli.record_gap("2026-10-06", 2)
        sli.poll()
        assert any(c["deduplication_key"] == "data_gap:exceeded" for c in am.calls)


# ─────────────────────────────────────────────
#  6. 防未来
# ─────────────────────────────────────────────
class TestAntiFuture:
    def test_未来时间戳滞后截断为零不告警(self) -> None:
        clock = _Clock(1000.0)
        am = _FakeAlert()
        sli = DataFreshnessSli(
            latest_timestamp_provider=lambda: {"K:1d": 1100.0},  # 未来 ts
            alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
        )
        st = sli.poll()
        assert st["latest_lag_seconds"]["K:1d"] == 0.0
        assert am.calls == []  # 未来数据不误告警

    def test_未来同步完成时间不产生负延迟(self) -> None:
        clock = _Clock(1000.0)
        sli = DataFreshnessSli(
            factor_sync_delay_max_seconds=10.0,
            alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics(), clock=clock,
        )
        sli.record_factor_sync(ts=1100.0)  # 未来 → delay 截断 0，不超阈
        st = sli.poll()
        assert st["factor_sync_delay_seconds"] == 0.0


# ─────────────────────────────────────────────
#  防御式校验
# ─────────────────────────────────────────────
class TestDefensive:
    def test_非法freshness_max抛错(self) -> None:
        with pytest.raises(ValueError):
            DataFreshnessSli(freshness_max_seconds=0, alert_manager=_FakeAlert())

    def test_非法gap_allowed抛错(self) -> None:
        with pytest.raises(ValueError):
            DataFreshnessSli(gap_allowed=-1, alert_manager=_FakeAlert())

    def test_非法同步延迟阈值抛错(self) -> None:
        with pytest.raises(ValueError):
            DataFreshnessSli(
                factor_sync_delay_max_seconds=-5, alert_manager=_FakeAlert()
            )

    def test_登记缺口非法输入抛错(self) -> None:
        sli = DataFreshnessSli(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        with pytest.raises(ValueError):
            sli.record_gap("", 1)
        with pytest.raises(ValueError):
            sli.record_gap("2026-10-06", -1)

    def test_provider异常不阻断(self) -> None:
        """provider 抛异常时按空数据处理，不向上炸断 poll。"""
        clock = _Clock(1000.0)

        def boom():
            raise RuntimeError("db down")

        sli = DataFreshnessSli(
            latest_timestamp_provider=boom,
            gap_provider=boom,
            alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics(), clock=clock,
        )
        st = sli.poll()  # 不应抛异常
        assert st["latest_lag_seconds"] == {}
        assert st["current_gap_count"] == 0

    def test_严重过期系数常量(self) -> None:
        """严重过期 ERROR 阈值 = freshness_max × 2。"""
        assert FRESHNESS_ERROR_MULTIPLE == 2.0
