# 文件名：myquant/core/transports/__init__.py
"""
transports - 外部消息中间件适配层
==================================
提供统一的消息传输抽象，支持:
  - Redis Pub/Sub
  - RabbitMQ (AMQP)
  - 未来可扩展: Kafka, ZeroMQ, NATS 等

所有实现遵循 base.Transport 接口。
"""

from typing import Any

from myquant.core.transports.base import (
    Message,
    Transport,
    TransportConfig,
    TransportState,
)

__all__ = [
    "Transport",
    "TransportConfig",
    "Message",
    "TransportState",
    "get_redis_transport",
    "get_rabbitmq_transport",
]

# 延迟导入，避免未安装依赖时报错
def get_redis_transport(**kwargs: Any) -> "RedisTransport":  # noqa: F821  # 延迟导入模式，注解字符串不求值（#380 复核：ruff 0.16.3 对字符串注解仍查 F821，noqa 非冗余，保留）
    from myquant.core.transports.redis_bus import RedisTransport
    return RedisTransport(**kwargs)

def get_rabbitmq_transport(**kwargs: Any) -> "RabbitMQTransport":  # noqa: F821  # 延迟导入模式，注解字符串不求值（#380 复核：noqa 非冗余，保留）
    from myquant.core.transports.rabbitmq_bus import RabbitMQTransport
    return RabbitMQTransport(**kwargs)