# 文件名：myquant/core/transports/rabbitmq_bus.py
"""
rabbitmq_bus.py - RabbitMQ 传输实现
=====================================
基于 RabbitMQ (AMQP) 实现可靠消息传输。

依赖: pika (pip install pika)

特点:
  - 消息持久化
  - 确认机制 (ACK)
  - 支持 Exchange 路由
  - 适合生产环境跨服务通信

可靠性保证（C3 加固）:
  - Publisher Confirm: publish 同步等待 broker 确认，未确认即抛错，不静默丢失
  - 有限重试: 消费失败按 x-retry-count 计数重投（默认 3 次），避免无限循环
  - 死信队列: 超过重试上限的消息进入 {prefix}.dlx / {queue}.dlq，可人工恢复

Usage:
    from myquant.core.transports import get_rabbitmq_transport

    transport = get_rabbitmq_transport(host="localhost", port=5672)
    transport.connect()
    transport.publish("order.filled", {"symbol": "600519"})
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

# C3: 模块级可选导入（_handle_message 重投路径使用 pika.BasicProperties）
try:
    import pika
except ImportError:  # pragma: no cover - 无 pika 环境允许导入模块，连接时再报错
    pika = None

logger = logging.getLogger("myquant.transport.rabbitmq")


class RabbitMQTransport(Transport):
    """
    RabbitMQ 传输适配器

    拓扑:
      Exchange (topic) → Queue (per consumer) → Handler

    Exchange 命名: {queue_prefix}.exchange
    Queue 命名: {queue_prefix}.{topic}
    """

    def __init__(self, config: Optional[TransportConfig] = None, **kwargs):
        if config is None:
            config = TransportConfig(**kwargs)
        super().__init__(config)
        self._connection = None
        self._channel = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._running = False
        self._handlers: Dict[str, list] = {}
        self._exchange_name = f"{config.queue_prefix}.exchange"
        # C3: 消息消费失败最大重试次数（达上限进入死信队列）
        self.max_retry = config.max_retry
        # C3: 死信交换机/队列命名
        self._dlx_name = f"{config.queue_prefix}.dlx"

    def connect(self) -> None:
        try:
            import pika
        except ImportError:
            raise ImportError("需要安装 pika: pip install pika")

        self._state = TransportState.CONNECTING
        try:
            credentials = pika.PlainCredentials(
                self.config.username or "guest",
                self.config.password or "guest",
            )
            params = pika.ConnectionParameters(
                host=self.config.host,
                port=self.config.port,
                virtual_host=self.config.vhost,
                credentials=credentials,
                heartbeat=0,
            )
            self._connection = pika.BlockingConnection(params)
            self._channel = self._connection.channel()

            # 声明 Exchange
            self._channel.exchange_declare(
                exchange=self._exchange_name,
                exchange_type="topic",
                durable=True,
            )

            # C3: 启用 publisher confirm——basic_publish 同步等待 broker 确认，
            # 消息被拒/不可达时抛出 UnroutableError/NackError，不再静默丢失
            self._channel.confirm_delivery()

            self._state = TransportState.CONNECTED
            logger.info(
                f"RabbitMQ connected: {self.config.host}:{self.config.port}"
                f" exchange={self._exchange_name}"
            )
        except Exception as e:
            self._state = TransportState.ERROR
            raise TransportConnectionError(f"RabbitMQ连接失败: {e}") from e

    def disconnect(self) -> None:
        self._running = False
        if self._consumer_thread and self._consumer_thread.is_alive():
            self._consumer_thread.join(timeout=3.0)
        if self._connection and self._connection.is_open:
            self._connection.close()
        self._state = TransportState.CLOSED
        logger.info("RabbitMQ transport disconnected")

    def publish(self, topic: str, payload: Dict[str, Any], **kwargs) -> None:
        if not self.is_connected:
            raise TransportError("RabbitMQ未连接")

        msg = Message(topic=topic, payload=payload)
        try:
            # C3: mandatory=True——无匹配队列时 broker 拒绝并触发 confirm 失败
            self._channel.basic_publish(
                exchange=self._exchange_name,
                routing_key=topic,
                body=msg.serialize(),
                mandatory=True,
                properties=pika.BasicProperties(
                    delivery_mode=2,  # 持久化
                    content_type="application/json",
                ),
            )
        except (pika.exceptions.UnroutableError, pika.exceptions.NackError) as e:
            # C3: broker 未确认（无路由/被拒绝）——显式抛出，由上层决定重试或告警
            raise TransportError(f"RabbitMQ发布未获确认: {e}") from e

    def subscribe(
        self,
        topic: str,
        handler: Callable[[Message], None],
        **kwargs,
    ) -> None:
        if not self.is_connected:
            raise TransportError("RabbitMQ未连接")

        queue_name = f"{self.config.queue_prefix}.{topic.replace('.', '_')}"

        # C3: 声明死信交换机（DLX）——消费失败且超过最大重试次数的消息进入此处
        self._channel.exchange_declare(
            exchange=self._dlx_name,
            exchange_type="topic",
            durable=True,
        )
        # 声明主队列，绑定 DLX（超时/拒绝/超重试的消息自动投递到 DLX）
        self._channel.queue_declare(
            queue=queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": self._dlx_name,
                "x-dead-letter-routing-key": queue_name,
            },
        )
        self._channel.queue_bind(
            queue=queue_name,
            exchange=self._exchange_name,
            routing_key=topic,
        )
        # C3: 声明死信队列（DLQ）并绑定 DLX——运维可从 DLQ 人工恢复消息
        dlq_name = f"{queue_name}.dlq"
        self._channel.queue_declare(queue=dlq_name, durable=True)
        self._channel.queue_bind(
            queue=dlq_name,
            exchange=self._dlx_name,
            routing_key=queue_name,
        )
        logger.info(
            f"RabbitMQ queue={queue_name} 已绑定 DLX={self._dlx_name}, DLQ={dlq_name}"
        )

        if topic not in self._handlers:
            self._handlers[topic] = []
        self._handlers[topic].append(handler)

        # M7修复: 运行中新增订阅——原 _consume 仅在启动时遍历一次 handlers，
        # 新队列只声明绑定不消费，消息积压。此处把 basic_consume 调度到
        # 消费线程的 IO loop（pika add_callback_threadsafe），线程安全且立即生效。
        if self._running and self._connection and self._connection.is_open:
            self._connection.add_callback_threadsafe(
                lambda: self._channel.basic_consume(
                    queue=queue_name,
                    on_message_callback=self._handle_message,
                )
            )
            logger.info(
                f"RabbitMQ 运行中新增订阅 topic={topic}，已调度消费 "
                f"(queue={queue_name})"
            )
        # 启动消费线程（仅首次订阅）
        elif not self._running:
            self._running = True
            self._consumer_thread = threading.Thread(
                target=self._consume,
                name="rabbitmq-consumer",
                daemon=True,
            )
            self._consumer_thread.start()

    def unsubscribe(self, topic: str) -> None:
        self._handlers.pop(topic, None)

    def _consume(self) -> None:
        """后台消费线程"""
        # 对所有已订阅的队列设置消费
        for topic in self._handlers:
            queue_name = f"{self.config.queue_prefix}.{topic.replace('.', '_')}"
            self._channel.basic_consume(
                queue=queue_name,
                on_message_callback=self._handle_message,
            )

        try:
            self._channel.start_consuming()
        except Exception as e:
            if self._running:
                logger.error(f"RabbitMQ consume error: {e}")

    def _handle_message(self, ch, method, properties, body) -> None:
        """单条消息处理：成功 ack；失败有限重试；达上限/重投失败进死信队列（C3）"""
        # C3: 读取重试计数（x-retry-count header），达到 max_retry 后不再重试
        headers = dict(properties.headers or {}) if properties else {}
        try:
            retry_count = int(headers.get("x-retry-count", 0) or 0)
        except (TypeError, ValueError):
            retry_count = 0

        # C3: 反序列化失败（消息损坏）也走有限重试路径，topic 回退到 routing_key
        msg = None
        topic = getattr(method, "routing_key", None) or ""
        try:
            msg = Message.deserialize(body)
            topic = msg.topic
            for handler in self._handlers.get(topic, []):
                handler(msg)
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            # 队列名用于日志/死信标识
            queue_name = (
                f"{self.config.queue_prefix}."
                f"{str(topic or method.routing_key or '').replace('.', '_')}"
            )
            if retry_count < self.max_retry:
                # 有限重试：ack 当前投递，携带计数重投原队列（避免 requeue 无限循环）
                try:
                    self._channel.basic_publish(
                        exchange=self._exchange_name,
                        routing_key=method.routing_key or topic,
                        body=body,
                        mandatory=True,
                        properties=pika.BasicProperties(
                            delivery_mode=2,  # 持久化
                            content_type="application/json",
                            headers={**headers, "x-retry-count": retry_count + 1},
                        ),
                    )
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    logger.warning(
                        f"RabbitMQ handler error: {e}；"
                        f"重试 {retry_count + 1}/{self.max_retry} 次"
                    )
                except Exception as repub_e:
                    # 重投失败：nack 入死信队列，避免消息滞留
                    logger.error(
                        f"RabbitMQ 重投失败: {repub_e}；消息进入死信队列 {queue_name}.dlq"
                    )
                    ch.basic_nack(
                        delivery_tag=method.delivery_tag,
                        requeue=False,
                    )
            else:
                logger.error(
                    f"RabbitMQ handler error: {e}；"
                    f"已达最大重试 {self.max_retry} 次，"
                    f"消息进入死信队列 {queue_name}.dlq"
                )
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def __repr__(self) -> str:
        return (
            f"RabbitMQTransport({self.config.host}:{self.config.port}, "
            f"exchange={self._exchange_name}, state={self._state.value})"
        )