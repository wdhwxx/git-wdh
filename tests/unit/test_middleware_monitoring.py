# 文件名：tests/unit/test_middleware_monitoring.py
"""
test_middleware_monitoring.py - A-2 L1 中间件监控单元测试
===========================================================
覆盖（不依赖真实 RabbitMQ：Management HTTP API 用 fake urlopen 注入）：
  1. 内置指标注册：middleware_queue_depth / middleware_dlq_count /
     middleware_event_bus_backlog / middleware_event_bus_backlog_ratio
  2. collect_from_event_bus 增补：积压条数 + 积压率（容量可得时）；无容量不崩
  3. collect_from_rabbitmq_bus：.dlq 后缀进 DLQ 计数、普通队列进队列深度；
     采集不可用（None）返回 0 不伪造指标
  4. check_middleware_backlog 判定：无数据 fail-closed→DEGRADED；正常→HEALTHY；
     队列深度超阈值→DEGRADED；DLQ 超阈值→UNHEALTHY；总线积压率超阈值→DEGRADED
  5. RabbitMQTransport.get_queue_stats：未配置 management_url→None；
     配置后经 HTTP API 拉取深度（ready+unacked）；任一队列失败→None（fail-closed）
  6. subscribe 登记已声明队列（主队列 + DLQ）

运行方式:
  pytest tests/unit/test_middleware_monitoring.py -v
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

import pytest

from myquant.core.transports.base import TransportConfig, TransportState
from myquant.core.transports.rabbitmq_bus import RabbitMQTransport
from myquant.core.health_check import HealthStatus
from myquant.monitoring.metrics_collector import BuiltinMetrics, MetricsCollector


# ═══════════════════════════════════════════
#  通用 fake
# ═══════════════════════════════════════════

class _FakeChannel:
    """RabbitMQ channel fake（subscribe 登记队列用，不触真实 pika）"""

    def exchange_declare(self, **kwargs: Any) -> None:
        pass

    def queue_declare(self, queue: str, **kwargs: Any) -> None:
        pass

    def queue_bind(self, **kwargs: Any) -> None:
        pass


class _FakeEventBus:
    """EventBus fake：stats + _history 容量（供 collect_from_event_bus 积压采集）"""

    def __init__(self, history_size: int, maxlen: Optional[int] = None):
        self._history_size = history_size
        if maxlen is not None:
            self._history = SimpleNamespace(maxlen=maxlen)
        else:
            self._history = None
        self._stats = {
            "total_events_published": 10,
            "sync_handlers": 2,
            "async_handlers": 1,
            "history_size": history_size,
        }

    @property
    def stats(self) -> Dict[str, Any]:
        return self._stats

    def get_handler_stats(self) -> list:
        return []


class _FakeTransport:
    """RabbitMQTransport fake：get_queue_stats 注入"""

    def __init__(self, stats: Optional[Dict[str, int]]):
        self._stats = stats

    def get_queue_stats(self) -> Optional[Dict[str, int]]:
        return self._stats


def _new_collector() -> MetricsCollector:
    return MetricsCollector().register_builtin_metrics()


# ═══════════════════════════════════════════
#  1. 内置指标注册
# ═══════════════════════════════════════════

class TestMiddlewareMetricRegistration:
    def test_middleware_metrics_registered(self) -> None:
        c = _new_collector()
        for name in (
            BuiltinMetrics.MIDDLEWARE_QUEUE_DEPTH,
            BuiltinMetrics.MIDDLEWARE_DLQ_COUNT,
            BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG,
            BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG_RATIO,
        ):
            assert c.is_registered(name), f"{name} 未注册"

    def test_queue_depth_metric_label_queue(self) -> None:
        c = _new_collector()
        assert c.get_definition(BuiltinMetrics.MIDDLEWARE_QUEUE_DEPTH).label_names == ["queue"]
        assert c.get_definition(BuiltinMetrics.MIDDLEWARE_DLQ_COUNT).label_names == ["queue"]


# ═══════════════════════════════════════════
#  2. collect_from_event_bus 积压采集
# ═══════════════════════════════════════════

class TestCollectEventBusBacklog:
    def test_backlog_and_ratio_collected(self) -> None:
        c = _new_collector()
        bus = _FakeEventBus(history_size=250, maxlen=1000)
        count = c.collect_from_event_bus(bus)
        assert c.get_value(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG) == 250.0
        assert c.get_value(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG_RATIO) == pytest.approx(0.25)
        assert count >= 4  # 3 基础 + 积压条数 + 积压率

    def test_backlog_ratio_capped_at_one(self) -> None:
        c = _new_collector()
        bus = _FakeEventBus(history_size=2000, maxlen=1000)
        c.collect_from_event_bus(bus)
        assert c.get_value(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG_RATIO) == 1.0

    def test_backlog_without_capacity_does_not_crash(self) -> None:
        c = _new_collector()
        bus = _FakeEventBus(history_size=5, maxlen=None)
        count = c.collect_from_event_bus(bus)
        assert c.get_value(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG) == 5.0
        # 容量不可得：积压率不更新但不阻断主链路
        assert c.get_all(BuiltinMetrics.MIDDLEWARE_EVENT_BUS_BACKLOG_RATIO) == []
        assert count >= 4

    def test_bus_stats_error_returns_zero(self) -> None:
        c = _new_collector()

        class _BrokenBus:
            @property
            def stats(self) -> Dict[str, Any]:
                raise RuntimeError("bus down")

        assert c.collect_from_event_bus(_BrokenBus()) == 0


# ═══════════════════════════════════════════
#  3. collect_from_rabbitmq_bus
# ═══════════════════════════════════════════

class TestCollectRabbitmq:
    def test_queue_depth_and_dlq_split(self) -> None:
        c = _new_collector()
        t = _FakeTransport({"mq.order_filled": 7, "mq.order_filled.dlq": 2})
        count = c.collect_from_rabbitmq_bus(t)
        assert count == 2
        assert c.get_value(BuiltinMetrics.MIDDLEWARE_QUEUE_DEPTH,
                           labels={"queue": "mq.order_filled"}) == 7.0
        assert c.get_value(BuiltinMetrics.MIDDLEWARE_DLQ_COUNT,
                           labels={"queue": "mq.order_filled.dlq"}) == 2.0
        # 普通队列不入 DLQ 计数
        assert c.get_value(BuiltinMetrics.MIDDLEWARE_DLQ_COUNT,
                           labels={"queue": "mq.order_filled"}, default=None) is None

    def test_unavailable_stats_returns_zero_no_fake(self) -> None:
        c = _new_collector()
        t = _FakeTransport(None)
        assert c.collect_from_rabbitmq_bus(t) == 0
        assert c.get_all(BuiltinMetrics.MIDDLEWARE_QUEUE_DEPTH) == []
        assert c.get_all(BuiltinMetrics.MIDDLEWARE_DLQ_COUNT) == []

    def test_transport_error_returns_zero(self) -> None:
        c = _new_collector()

        class _BrokenTransport:
            def get_queue_stats(self) -> Optional[Dict[str, int]]:
                raise RuntimeError("management down")

        assert c.collect_from_rabbitmq_bus(_BrokenTransport()) == 0


# ═══════════════════════════════════════════
#  4. check_middleware_backlog 积压判定
# ═══════════════════════════════════════════

class TestCheckMiddlewareBacklog:
    def test_no_data_fail_closed_degraded(self) -> None:
        c = _new_collector()
        result = c.check_middleware_backlog()
        assert result.name == "middleware_backlog"
        assert result.status == HealthStatus.DEGRADED

    def test_healthy_when_below_thresholds(self) -> None:
        c = _new_collector()
        c.collect_from_rabbitmq_bus(_FakeTransport({"mq.a": 5, "mq.a.dlq": 0}))
        c.collect_from_event_bus(_FakeEventBus(history_size=100, maxlen=1000))
        result = c.check_middleware_backlog()
        assert result.status == HealthStatus.HEALTHY

    def test_queue_depth_exceeded_degraded(self) -> None:
        c = _new_collector()
        c.collect_from_rabbitmq_bus(_FakeTransport({"mq.a": 1500, "mq.a.dlq": 0}))
        result = c.check_middleware_backlog(queue_depth_threshold=1000.0)
        assert result.status == HealthStatus.DEGRADED
        assert result.details["queues"] == ["mq.a"]

    def test_dlq_exceeded_unhealthy(self) -> None:
        c = _new_collector()
        c.collect_from_rabbitmq_bus(_FakeTransport({"mq.a": 1, "mq.a.dlq": 200}))
        result = c.check_middleware_backlog(dlq_threshold=100.0)
        assert result.status == HealthStatus.UNHEALTHY
        assert result.details["dlq_over"] == ["mq.a.dlq"]

    def test_backlog_ratio_exceeded_degraded(self) -> None:
        c = _new_collector()
        c.collect_from_event_bus(_FakeEventBus(history_size=950, maxlen=1000))
        result = c.check_middleware_backlog(backlog_ratio_threshold=0.9)
        assert result.status == HealthStatus.DEGRADED
        assert result.details["backlog_ratio_over"] == [pytest.approx(0.95)]


# ═══════════════════════════════════════════
#  5. RabbitMQTransport.get_queue_stats
# ═══════════════════════════════════════════

class TestGetQueueStats:
    def _transport(self, extra: Optional[Dict[str, Any]] = None) -> RabbitMQTransport:
        config = TransportConfig(
            host="localhost",
            port=5672,
            username="ops",
            password="secret",
            queue_prefix="mq",
            extra=extra or {},
        )
        return RabbitMQTransport(config)

    def test_empty_declared_queues_returns_empty(self) -> None:
        t = self._transport(extra={"management_url": "http://mq:15672"})
        assert t.get_queue_stats() == {}

    def test_without_management_url_returns_none(self) -> None:
        t = self._transport()
        t._declared_queues = {"mq.a", "mq.a.dlq"}
        assert t.get_queue_stats() is None

    def test_fetch_queue_depth_from_management_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        t = self._transport(extra={"management_url": "http://mq:15672"})
        t._declared_queues = {"mq.a", "mq.a.dlq"}
        captured_urls: list = []

        class _FakeResp:
            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *args: Any) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"messages_ready": 3, "messages_unacknowledged": 2}'

        def fake_urlopen(req: Any, timeout: Optional[float] = None) -> _FakeResp:
            captured_urls.append(req.full_url)
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        stats = t.get_queue_stats()
        assert stats == {"mq.a": 5, "mq.a.dlq": 5}
        assert len(captured_urls) == 2
        assert "/api/queues/" in captured_urls[0]

    def test_any_queue_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        t = self._transport(extra={"management_url": "http://mq:15672"})
        t._declared_queues = {"mq.a", "mq.a.dlq"}

        def fake_urlopen(req: Any, timeout: Optional[float] = None) -> Any:
            raise OSError("management unreachable")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert t.get_queue_stats() is None


# ═══════════════════════════════════════════
#  6. subscribe 登记已声明队列
# ═══════════════════════════════════════════

class TestSubscribeDeclaresQueues:
    def test_subscribe_records_queue_and_dlq(self) -> None:
        t = RabbitMQTransport(TransportConfig(
            host="localhost", port=5672, username="u", password="p",
            queue_prefix="mq", extra={"management_url": "http://mq:15672"},
        ))
        t._state = TransportState.CONNECTED
        t._running = True
        t._channel = _FakeChannel()
        t._connection = SimpleNamespace(
            is_open=True,
            add_callback_threadsafe=lambda f: None,
        )
        t.subscribe("order.filled", lambda m: None)
        assert "mq.order_filled" in t._declared_queues
        assert "mq.order_filled.dlq" in t._declared_queues
