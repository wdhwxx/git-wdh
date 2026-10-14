# 文件名：tests/unit/test_incident_context.py
"""
test_incident_context.py - C-7 根因分析（单机版）测试（回写#236）
========================================================================
覆盖：
  1. 同窗聚合：同窗告警聚合为一次 incident（非同窗开新 incident）。
  2. 依赖抑制：底层（watchdog 行情断流）命中时，同窗上层告警
     （data_quality/strategy）只作关联上下文（related+suppressed），不再单开根因。
  3. EVENT 事件路径：真实 EventBus + 真实 AlertManager(临时库) 断流级联 →
     单条根因 incident（验收口径：一次行情断流只产生一条根因告警）。
  4. incident 结构：incident_id/start_ts/end_ts/root_alert/related_alerts/
     metrics/changes/logs/status 齐全且 JSON 可序列化。
  5. 窗口边界：start_ts+window 内归并，超出 window 开新 incident。
  6. 防未来：拒绝未来时间戳。
  7. 防御式校验（空 category/source/dedup、非法 level、非法 window）。
  8. 指标注册：incident_total/incident_suppressed_total 经 collector 注册+递增。

依赖: pytest, myquant.monitoring.incident_context, myquant.core.event_bus,
      myquant.core.alert_store/alerting。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from myquant.core.alert_store import AlertStore
from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import Event, EventBus, EventType
from myquant.monitoring.alerting import AlertManager
from myquant.monitoring.incident_context import (
    CATEGORY_INCIDENT,
    DEDUP_INCIDENT_PREFIX,
    DEFAULT_DEPENDENCY_MAP,
    METRIC_INCIDENT_SUPPRESSED_TOTAL,
    METRIC_INCIDENT_TOTAL,
    REGISTERED_METRICS,
    SOURCE_INCIDENT,
    IncidentContextEngine,
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
    """捕获 register/inc_counter 的指标采集器桩（labels 并入 key）。"""

    def __init__(self) -> None:
        self.registered: List[str] = []
        self.counters: Dict[str, float] = {}

    def register(self, name: str, metric_type: Any, help: str = "",
                 label_names: Any = None) -> "_FakeMetrics":
        self.registered.append(name)
        return self

    def inc_counter(self, name: str, amount: float = 1.0,
                    labels: Any = None) -> None:
        key = f"{name}:{(labels or {}).get('root_category', '?')}"
        self.counters[key] = self.counters.get(key, 0.0) + amount


def _make_engine(
    clock_now: float,
    alert: Any = None,
    metrics: Any = None,
    window: float = 60.0,
    dep_map: Any = None,
) -> IncidentContextEngine:
    """构造固定时钟的引擎（now=clock_now，所有注入 ts 须 <= now）。"""
    return IncidentContextEngine(
        window_seconds=window,
        dependency_map=dep_map,
        alert_manager=alert if alert is not None else _FakeAlert(),
        metrics_collector=metrics if metrics is not None else _FakeMetrics(),
        clock=lambda: clock_now,
    )


class TestSameWindowAggregation:
    def test_同窗告警聚合为单incident(self) -> None:
        now = _BASE + 10_000.0
        am = _FakeAlert()
        eng = _make_engine(now, alert=am)
        eng.handle_alert(SeverityLevel.ERROR, "watchdog", "monitoring.watchdog",
                         "watchdog:staleness:down", "行情断流", ts=_BASE + 10.0)
        # 同窗内再来一条 broker 告警（非上层依赖）→ related，不抑制
        eng.handle_alert(SeverityLevel.WARNING, "broker", "monitoring.broker",
                         "broker:latency", "券商延迟", ts=_BASE + 20.0)
        incidents = eng.get_incidents()
        assert len(incidents) == 1
        assert incidents[0]["root_alert"]["category"] == "watchdog"
        assert len(incidents[0]["related_alerts"]) == 1
        assert incidents[0]["suppressed_count"] == 0
        # 根因告警只发一条
        inc_calls = [c for c in am.calls if c["category"] == CATEGORY_INCIDENT]
        assert len(inc_calls) == 1

    def test_窗口外开新incident(self) -> None:
        now = _BASE + 10_000.0
        am = _FakeAlert()
        eng = _make_engine(now, alert=am, window=60.0)
        eng.handle_alert(SeverityLevel.ERROR, "watchdog", "monitoring.watchdog",
                         "watchdog:staleness:down", "断流", ts=_BASE + 10.0)
        # 远超窗口（>60s）再来一条 → 关闭旧 incident，开新根因
        eng.handle_alert(SeverityLevel.ERROR, "broker", "monitoring.broker",
                         "broker:disconnect", "券商断连", ts=_BASE + 200.0)
        incidents = eng.get_incidents()
        assert len(incidents) == 2
        # get_incidents 顺序 = [当前 open, ...已关闭]：open=broker，closed=watchdog
        assert incidents[0]["root_alert"]["category"] == "broker"
        assert incidents[0]["status"] == "open"
        assert incidents[1]["root_alert"]["category"] == "watchdog"
        assert incidents[1]["status"] == "closed"
        # 两次根因 → 两条 incident 告警
        inc_calls = [c for c in am.calls if c["category"] == CATEGORY_INCIDENT]
        assert len(inc_calls) == 2


class TestDependencySuppression:
    def test_底层断流抑制上层数据质量与策略告警(self) -> None:
        now = _BASE + 10_000.0
        am = _FakeAlert()
        met = _FakeMetrics()
        eng = _make_engine(now, alert=am, metrics=met)
        # 根因：行情断流（watchdog 底层）
        eng.handle_alert(SeverityLevel.ERROR, "watchdog", "monitoring.watchdog",
                         "watchdog:staleness:down", "断流", ts=_BASE + 10.0)
        # 级联上层：数据质量（应被抑制）
        eng.handle_alert(SeverityLevel.WARNING, "data_quality", "monitoring.dq",
                         "dq:gap", "行情缺口", ts=_BASE + 15.0)
        # 级联上层：策略漂移（应被抑制）
        eng.handle_alert(SeverityLevel.WARNING, "strategy", "monitoring.strategy",
                         "strategy:drift", "信号漂移", ts=_BASE + 20.0)
        inc = eng.get_incidents()[0]
        assert inc["root_alert"]["category"] == "watchdog"
        assert len(inc["related_alerts"]) == 2
        assert inc["suppressed_count"] == 2
        assert all(a["suppressed"] is True for a in inc["related_alerts"])
        # 根因告警契约：category=incident / source / dedup 前缀
        inc_calls = [c for c in am.calls if c["category"] == CATEGORY_INCIDENT]
        assert len(inc_calls) == 1
        assert inc_calls[0]["source"] == SOURCE_INCIDENT
        assert inc_calls[0]["deduplication_key"].startswith(DEDUP_INCIDENT_PREFIX)
        # 抑制的上层告警不单独触发新 incident 告警
        assert len(am.calls) == 1

    def test_独立上层告警无根因时自开incident(self) -> None:
        now = _BASE + 10_000.0
        am = _FakeAlert()
        eng = _make_engine(now, alert=am)
        eng.handle_alert(SeverityLevel.WARNING, "data_quality", "monitoring.dq",
                         "dq:dirty", "脏数据", ts=_BASE + 10.0)
        inc = eng.get_incidents()[0]
        # 无底层根因 → 该告警自为根因，不抑制
        assert inc["root_alert"]["category"] == "data_quality"
        assert inc["suppressed_count"] == 0

    def test_可注入扩展依赖映射(self) -> None:
        now = _BASE + 10_000.0
        am = _FakeAlert()
        eng = _make_engine(now, alert=am, dep_map={"broker": ["trade"]})
        eng.handle_alert(SeverityLevel.CRITICAL, "broker", "monitoring.watchdog",
                         "broker:disconnect", "券商断连", ts=_BASE + 10.0)
        eng.handle_alert(SeverityLevel.ERROR, "trade", "monitoring.trade",
                         "trade:reject", "拒单", ts=_BASE + 12.0)
        inc = eng.get_incidents()[0]
        assert inc["root_alert"]["category"] == "broker"
        assert inc["suppressed_count"] == 1  # trade 是 broker 上层 → 抑制


class TestEventPath:
    def test_真实EventBus断流级联单根因incident(self) -> None:
        """EVENT 路径（R-02）：真实 EventBus 发布断流→级联，引擎实时聚合为单条根因。"""
        tmpdir = tempfile.mkdtemp(prefix="incident_test_")
        try:
            store = AlertStore(db_path=str(Path(tmpdir) / "t.db"))
            manager = AlertManager(store=store, store_enabled=True)
            bus = EventBus()
            met = _FakeMetrics()
            eng = IncidentContextEngine(
                window_seconds=60.0, alert_manager=manager,
                metrics_collector=met, clock=time.time,
            )
            eng.subscribe_to_event_bus(bus)

            bus.publish(Event(
                EventType.SYSTEM_ERROR, source="monitoring.watchdog",
                data={"level": "error", "category": "watchdog",
                      "source": "monitoring.watchdog", "dedup_key": "watchdog:staleness:down",
                      "message": "行情断流 bar 超时", "ts": time.time()},
            ))
            bus.publish(Event(
                EventType.DATA_QUALITY_ALERT, source="monitoring.dq",
                data={"level": "warning", "category": "data_quality",
                      "source": "monitoring.dq", "dedup_key": "dq:gap",
                      "message": "行情缺口", "ts": time.time()},
            ))
            bus.publish(Event(
                EventType.RISK_ALERT, source="monitoring.strategy",
                data={"level": "warning", "category": "strategy",
                      "source": "monitoring.strategy", "dedup_key": "strategy:drift",
                      "message": "信号漂移", "ts": time.time()},
            ))

            incidents = eng.get_incidents()
            assert len(incidents) == 1                      # 非告警风暴
            inc = incidents[0]
            assert inc["root_alert"]["category"] == "watchdog"
            assert len(inc["related_alerts"]) == 2
            assert inc["suppressed_count"] == 2
            # AlertManager 只落 1 条 incident 根因告警
            inc_alerts = [a for a in manager._history if a.category == CATEGORY_INCIDENT]
            assert len(inc_alerts) == 1
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestIncidentStructure:
    def test_incident字段齐全且JSON可序列化(self) -> None:
        now = _BASE + 10_000.0
        eng = _make_engine(now)
        eng.handle_alert(SeverityLevel.ERROR, "watchdog", "monitoring.watchdog",
                         "watchdog:staleness:down", "断流", ts=_BASE + 10.0)
        # 注入上下文（metrics/changes/logs 挂入打开 incident）
        eng.handle_metric("market.bar_rate", 0.0, ts=_BASE + 11.0, labels={"symbol": "600000.SH"})
        eng.handle_change("config", "调参", ts=_BASE + 11.0, actor="ops")
        eng.handle_log("error", "bar timeout", ts=_BASE + 11.0, source="watchdog")
        inc = eng.get_incidents()[0]
        for key in ("incident_id", "start_ts", "end_ts", "root_alert",
                    "related_alerts", "metrics", "changes", "logs", "status"):
            assert key in inc, f"缺字段 {key}"
        assert len(inc["metrics"]) == 1
        assert len(inc["changes"]) == 1
        assert len(inc["logs"]) == 1
        json.dumps(inc)  # 不抛异常即 JSON 可序列化

    def test_root_ts前pending上下文折叠入incident(self) -> None:
        now = _BASE + 10_000.0
        eng = _make_engine(now)
        # 根因前 window 内的变更记录 → pending
        eng.handle_change("deploy", "上线新版本", ts=_BASE + 5.0, actor="ci")
        # 根因到达
        eng.handle_alert(SeverityLevel.ERROR, "watchdog", "monitoring.watchdog",
                         "watchdog:staleness:down", "断流", ts=_BASE + 10.0)
        inc = eng.get_incidents()[0]
        # root_ts 前 window 内的 change 应折叠进 incident 上下文
        assert any(c["change_type"] == "deploy" for c in inc["changes"])


class TestWindowBoundary:
    def test_窗口上界内归并_超出开新incident(self) -> None:
        now = _BASE + 10_000.0
        eng = _make_engine(now, window=60.0)
        eng.handle_alert(SeverityLevel.ERROR, "watchdog", "monitoring.watchdog",
                         "watchdog:staleness:down", "断流", ts=_BASE + 10.0)
        # 恰好在上界 start+60 内 → 归并 related
        eng.handle_alert(SeverityLevel.WARNING, "data_quality", "monitoring.dq",
                         "dq:gap", "缺口", ts=_BASE + 70.0)
        assert len(eng.get_incidents()) == 1
        assert len(eng.get_incidents()[0]["related_alerts"]) == 1
        # 超过上界（start+60=70，取 71.1）→ 开新 incident
        eng.handle_alert(SeverityLevel.ERROR, "broker", "monitoring.broker",
                         "broker:dc", "断连", ts=_BASE + 71.1)
        assert len(eng.get_incidents()) == 2


class TestFutureProof:
    def test_拒绝未来时间戳(self) -> None:
        now = _BASE + 10_000.0
        eng = _make_engine(now)
        with pytest.raises(ValueError, match="未来"):
            eng.handle_alert(SeverityLevel.ERROR, "watchdog", "s", "k", "m",
                             ts=now + 100.0)
        with pytest.raises(ValueError, match="未来"):
            eng.handle_metric("x", 1.0, ts=now + 100.0)


class TestDefensiveValidation:
    def test_空category_source_dedup拒绝(self) -> None:
        now = _BASE + 10_000.0
        eng = _make_engine(now)
        with pytest.raises(ValueError):
            eng.handle_alert(SeverityLevel.ERROR, "", "s", "k", "m", ts=now)
        with pytest.raises(ValueError):
            eng.handle_alert(SeverityLevel.ERROR, "c", "", "k", "m", ts=now)
        with pytest.raises(ValueError):
            eng.handle_alert(SeverityLevel.ERROR, "c", "s", "", "m", ts=now)

    def test_非法level拒绝(self) -> None:
        now = _BASE + 10_000.0
        eng = _make_engine(now)
        with pytest.raises(ValueError):
            eng.handle_alert("bogus_level", "c", "s", "k", "m", ts=now)

    def test_非法window拒绝(self) -> None:
        with pytest.raises(ValueError):
            IncidentContextEngine(window_seconds=0, clock=lambda: _BASE)
        with pytest.raises(ValueError):
            IncidentContextEngine(window_seconds=-5, clock=lambda: _BASE)

    def test_非法依赖映射拒绝(self) -> None:
        with pytest.raises(ValueError):
            IncidentContextEngine(window_seconds=60.0, dependency_map=["not", "a", "dict"],
                                  clock=lambda: _BASE)


class TestMetricsRegistration:
    def test_指标注册与计数器递增(self) -> None:
        now = _BASE + 10_000.0
        met = _FakeMetrics()
        eng = _make_engine(now, metrics=met)
        # 两个计数器指标均注册
        for name in REGISTERED_METRICS:
            assert name in met.registered
        # 根因 incident +1；一条被抑制上层 +1
        eng.handle_alert(SeverityLevel.ERROR, "watchdog", "monitoring.watchdog",
                         "watchdog:staleness:down", "断流", ts=_BASE + 10.0)
        eng.handle_alert(SeverityLevel.WARNING, "data_quality", "monitoring.dq",
                         "dq:gap", "缺口", ts=_BASE + 15.0)
        assert met.counters.get(f"{METRIC_INCIDENT_TOTAL}:watchdog", 0.0) == 1.0
        assert met.counters.get(f"{METRIC_INCIDENT_SUPPRESSED_TOTAL}:watchdog", 0.0) == 1.0

    def test_默认依赖映射已登记(self) -> None:
        assert "watchdog" in DEFAULT_DEPENDENCY_MAP
        assert "data_quality" in DEFAULT_DEPENDENCY_MAP["watchdog"]


class TestEventBusLifecycle:
    def test_subscribe_unsubscribe(self) -> None:
        now = _BASE + 10_000.0
        eng = _make_engine(now)
        bus = EventBus()
        ids = eng.subscribe_to_event_bus(bus)
        assert len(ids) == len(eng._alert_event_types)  # noqa: SLF001
        n = eng.unsubscribe_from_event_bus(bus)
        assert n == len(ids)
