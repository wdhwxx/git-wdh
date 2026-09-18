# 文件名：myquant/core/transports/redis_bus.py
"""
redis_bus.py - Redis Pub/Sub 传输实现
======================================
基于 Redis Pub/Sub 实现跨进程消息通信。

依赖: redis (pip install redis)

Usage:
    from myquant.core.transports import get_redis_transport

    transport = get_redis_transport(host="localhost", port=6379)
    transport.connect()
    transport.subscribe("order.filled", lambda msg: print(msg.payload))
    transport.publish("order.filled", {"symbol": "600519", "price": 1800})
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from myquant.core.transports.base import (
    Message,
    Transport,
    TransportConfig,
    TransportState,
)
from myquant.core.exceptions import TransportConnectionError, TransportError

logger = logging.getLogger("myquant.transport.redis")


class RedisTransport(Transport):
    """
    Redis Pub/Sub 传输适配器

    特点:
      - 轻量级，适合单机多进程通信
      - 消息不持久化 (fire-and-forget)
      - 支持模式匹配订阅 (psubscribe)
    """

    def __init__(self, config: Optional[TransportConfig] = None, **kwargs):
        if config is None:
            config = TransportConfig(**kwargs)
        super().__init__(config)
        self._client = None
        self._pubsub = None
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._handlers: Dict[str, list] = {}

    def connect(self) -> None:
        try:
            import redis
        except ImportError:
            raise ImportError("需要安装 redis: pip install redis")

        self._state = TransportState.CONNECTING
        try:
            self._client = redis.Redis(
                host=self.config.host,
                port=self.config.port,
                db=self.config.database,
                password=self.config.password or None,
                decode_responses=False,
            )
            self._client.ping()
            self._pubsub = self._client.pubsub()
            self._state = TransportState.CONNECTED
            logger.info(f"Redis transport connected: {self.config.host}:{self.config.port}")
        except Exception as e:
            self._state = TransportState.ERROR
            raise TransportConnectionError(f"Redis连接失败: {e}") from e

    def disconnect(self) -> None:
        self._running = False
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=3.0)
        if self._pubsub:
            self._pubsub.close()
        if self._client:
            self._client.close()
        self._state = TransportState.CLOSED
        logger.info("Redis transport disconnected")

    def publish(self, topic: str, payload: Dict[str, Any], **kwargs) -> None:
        if not self.is_connected:
            raise TransportError("Redis未连接")
        msg = Message(topic=topic, payload=payload)
        self._client.publish(topic, msg.serialize())

    def subscribe(
        self,
        topic: str,
        handler: Callable[[Message], None],
        **kwargs,
    ) -> None:
        if not self.is_connected:
            raise TransportError("Redis未连接")

        if topic not in self._handlers:
            self._handlers[topic] = []
            self._pubsub.subscribe(topic)

        self._handlers[topic].append(handler)

        # 启动监听线程 (仅一次)
        if not self._running:
            self._running = True
            self._listener_thread = threading.Thread(
                target=self._listen,
                name="redis-listener",
                daemon=True,
            )
            self._listener_thread.start()

    def unsubscribe(self, topic: str) -> None:
        if self._pubsub:
            self._pubsub.unsubscribe(topic)
        self._handlers.pop(topic, None)

    def _listen(self) -> None:
        """后台监听线程"""
        while self._running:
            try:
                message = self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message["type"] == "message":
                    topic = message["channel"].decode("utf-8")
                    msg = Message.deserialize(message["data"])
                    for handler in self._handlers.get(topic, []):
                        try:
                            handler(msg)
                        except Exception as e:
                            logger.error(f"Redis handler error: {e}")
            except Exception as e:
                if self._running:
                    logger.error(f"Redis listen error: {e}")
                    time.sleep(self.config.reconnect_interval)

    def __repr__(self) -> str:
        return f"RedisTransport({self.config.host}:{self.config.port}, state={self._state.value})"