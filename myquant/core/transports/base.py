# 文件名：myquant/core/transports/base.py
"""
base.py - 传输抽象基类
=======================
定义所有消息中间件适配器的统一接口。
"""

from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class TransportState(Enum):
    """传输层状态"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    CLOSED = "closed"


@dataclass
class TransportConfig:
    """传输层配置"""
    host: str = "localhost"
    port: int = 6379
    username: str = ""
    password: str = ""
    database: int = 0
    vhost: str = "/"
    exchange: str = ""
    queue_prefix: str = "myquant"
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 10
    heartbeat_interval: float = 30.0
    # 消息消费失败最大重试次数（RabbitMQ 有限重试，达上限进入死信队列）
    max_retry: int = 3
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """传输消息"""
    topic: str
    payload: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    msg_id: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    def serialize(self) -> bytes:
        return json.dumps({
            "topic": self.topic,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "msg_id": self.msg_id,
            "headers": self.headers,
        }, default=str).encode("utf-8")

    @classmethod
    def deserialize(cls, raw: bytes) -> "Message":
        data = json.loads(raw.decode("utf-8"))
        return cls(
            topic=data.get("topic", ""),
            payload=data.get("payload", {}),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", ""),
            headers=data.get("headers", {}),
        )


class Transport(abc.ABC):
    """
    消息传输抽象基类

    所有中间件适配器必须实现以下接口:
      - connect / disconnect
      - publish
      - subscribe / unsubscribe
      - 状态管理
    """

    def __init__(self, config: TransportConfig):
        self.config = config
        self._state = TransportState.DISCONNECTED
        self._subscriptions: Dict[str, list] = {}

    # ── 抽象方法 ──
    @abc.abstractmethod
    def connect(self) -> None:
        """建立连接"""
        ...

    @abc.abstractmethod
    def disconnect(self) -> None:
        """断开连接"""
        ...

    @abc.abstractmethod
    def publish(self, topic: str, payload: Dict[str, Any], **kwargs) -> None:
        """发布消息"""
        ...

    @abc.abstractmethod
    def subscribe(
        self,
        topic: str,
        handler: Callable[[Message], None],
        **kwargs,
    ) -> None:
        """订阅主题"""
        ...

    @abc.abstractmethod
    def unsubscribe(self, topic: str) -> None:
        """取消订阅"""
        ...

    # ── 通用方法 ──
    @property
    def state(self) -> TransportState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == TransportState.CONNECTED

    def publish_event(self, topic: str, event_data: Dict[str, Any]) -> None:
        """发布事件 (便捷方法)"""
        self.publish(topic, event_data)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(state={self._state.value})"