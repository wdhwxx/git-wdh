# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_redis_bus.py
"""
Redis Pub/Sub 传输（myquant/core/transports/redis_bus.py）单元测试（G0-9 补测）
==========================================================
环境无真实 redis，测试注入假 redis 模块（monkeypatch sys.modules）验证：
  - connect 成功 / 连接失败抛 TransportConnectionError
  - publish：未连接抛 TransportError / 已连接经 client.publish 发送序列化消息
  - subscribe：未连接抛 TransportError / 已连接注册 handler + pubsub.subscribe
  - unsubscribe 移除 handler
  - disconnect 关闭 pubsub/client 并置 CLOSED
  - Message 序列化/反序列化往返
"""
from __future__ import annotations

import json
import sys
import types
from typing import Any, Dict, Optional

import pytest

from myquant.core.exceptions import TransportConnectionError, TransportError
from myquant.core.transports.base import Message
from myquant.core.transports.redis_bus import RedisTransport


class _FakePubSub:
    """假 pubsub 对象。"""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.closed = False

    def subscribe(self, *channels: Any) -> None:
        self.subscribed.extend(channels)

    def unsubscribe(self, *channels: Any) -> None:
        for c in channels:
            if c in self.subscribed:
                self.subscribed.remove(c)

    def get_message(self, ignore_subscribe_messages: bool = True,
                    timeout: float = 0.0) -> Any:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """假 redis 客户端。"""

    def __init__(self, fail_ping: bool = False) -> None:
        self.fail_ping = fail_ping
        self.published: list[tuple[str, str]] = []
        self._pubsub = _FakePubSub()
        self.closed = False

    def ping(self) -> bool:
        if self.fail_ping:
            raise ConnectionError("redis down")
        return True

    def pubsub(self) -> _FakePubSub:
        return self._pubsub

    def publish(self, channel: str, message: Any) -> int:
        self.published.append((channel, message))
        return 1

    def close(self) -> None:
        self.closed = True


def _install_fake_redis(fail_ping: bool = False) -> _FakeClient:
    """向 sys.modules 注入假 redis 模块，返回假客户端。"""
    client = _FakeClient(fail_ping=fail_ping)
    fake_redis = types.ModuleType("redis")
    fake_redis.Redis = lambda *a, **k: client
    sys.modules["redis"] = fake_redis
    return client


@pytest.fixture()
def _no_real_redis():
    """记录原 redis 模块并卸载，测试结束后恢复。"""
    had = "redis" in sys.modules
    original = sys.modules.pop("redis", None)
    yield
    if had and original is not None:
        sys.modules["redis"] = original
    else:
        sys.modules.pop("redis", None)


class TestMessageSerialization:
    def test_roundtrip(self) -> None:
        msg = Message(topic="order.filled", payload={"symbol": "600519", "qty": 100})
        raw = msg.serialize()
        restored = Message.deserialize(raw)
        assert restored.topic == "order.filled"
        assert restored.payload == {"symbol": "600519", "qty": 100}


class TestRedisTransport:
    def test_connect_success_sets_connected(self, _no_real_redis) -> None:
        client = _install_fake_redis()
        t = RedisTransport(host="127.0.0.1", port=6379)
        t.connect()
        assert t.is_connected
        assert client._pubsub is not None
        t.disconnect()
        assert client.closed

    def test_connect_failure_raises(self, _no_real_redis) -> None:
        _install_fake_redis(fail_ping=True)
        t = RedisTransport(host="127.0.0.1", port=6379)
        with pytest.raises(TransportConnectionError):
            t.connect()

    def test_publish_before_connect_raises(self, _no_real_redis) -> None:
        t = RedisTransport(host="127.0.0.1", port=6379)
        with pytest.raises(TransportError):
            t.publish("order.filled", {"symbol": "600519"})

    def test_publish_after_connect_serializes(self, _no_real_redis) -> None:
        client = _install_fake_redis()
        t = RedisTransport(host="127.0.0.1", port=6379)
        t.connect()
        t.publish("order.filled", {"symbol": "600519", "price": 1800})
        assert client.published
        channel, raw = client.published[-1]
        assert channel == "order.filled"
        restored = Message.deserialize(raw.encode() if isinstance(raw, str) else raw)
        assert restored.payload["symbol"] == "600519"

    def test_subscribe_before_connect_raises(self, _no_real_redis) -> None:
        t = RedisTransport(host="127.0.0.1", port=6379)
        with pytest.raises(TransportError):
            t.subscribe("order.filled", lambda m: None)

    def test_subscribe_registers_handler_and_channel(self, _no_real_redis) -> None:
        client = _install_fake_redis()
        t = RedisTransport(host="127.0.0.1", port=6379)
        t.connect()
        handler = lambda m: None  # noqa: E731
        t.subscribe("order.filled", handler)
        assert "order.filled" in client._pubsub.subscribed
        assert len(t._handlers["order.filled"]) == 1
        # 再次订阅不重复注册 channel
        t.subscribe("order.filled", handler)
        assert client._pubsub.subscribed.count("order.filled") == 1
        t.disconnect()

    def test_unsubscribe_removes_handler(self, _no_real_redis) -> None:
        client = _install_fake_redis()
        t = RedisTransport(host="127.0.0.1", port=6379)
        t.connect()
        t.subscribe("a", lambda m: None)
        t.unsubscribe("a")
        assert "a" not in t._handlers
        assert "a" not in client._pubsub.subscribed
        t.disconnect()

    def test_disconnect_closes_and_sets_closed(self, _no_real_redis) -> None:
        client = _install_fake_redis()
        t = RedisTransport(host="127.0.0.1", port=6379)
        t.connect()
        t.disconnect()
        assert t._state.value == "closed"
        assert client.closed
