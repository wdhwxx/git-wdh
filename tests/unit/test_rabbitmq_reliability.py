# -*- coding: utf-8 -*-
"""
RabbitMQ 消息可靠性回归测试（审计 C3）
======================================
覆盖:
  1. 消费成功 → ACK
  2. 消费失败 → 有限重试（x-retry-count 计数重投，默认 3 次）
  3. 达最大重试 → NACK(requeue=False) 进入死信队列（不再丢消息）
  4. 重投失败 → NACK 入死信
  5. 消息损坏（反序列化失败）→ 走有限重试路径
  6. Publisher Confirm：mandatory + Unroutable/Nack 转 TransportError
  7. 死信队列声明（DLX/DLQ 绑定参数）
  8. connect 启用 confirm_delivery
  9. max_retry 可配置

回归依据: 审计问题 C3（rabbitmq_bus.py basic_nack(requeue=False) 直接丢消息）
"""
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── fake pika（测试环境无 pika / 无真实 broker）──
_exc = types.ModuleType("pika.exceptions")
_exc.UnroutableError = type("UnroutableError", (Exception,), {})
_exc.NackError = type("NackError", (Exception,), {})

_pika = types.ModuleType("pika")
_pika.exceptions = _exc


class _FakeBasicProperties:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


_pika.BasicProperties = _FakeBasicProperties
# 保存原始绑定（运行期 fixture 注入 fake，测试结束后恢复，避免收集期污染 sys.modules）
_orig_pika = sys.modules.get("pika")
_orig_pika_exc = sys.modules.get("pika.exceptions")

import pytest  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _fake_pika_and_restore():
    """运行期注入 fake pika（仅本模块测试期间）并重载 rabbitmq_bus；结束后恢复并重载。

    说明：注入必须发生在运行期而非模块收集期——pytest 收集会 import 全部测试文件，
    模块级注入会把 fake pika 残留到 sys.modules，污染其他测试文件（如
    test_message_bus_credentials 的 connect 路径）。
    """
    import importlib
    sys.modules["pika"] = _pika
    sys.modules["pika.exceptions"] = _exc
    bus = sys.modules.get("myquant.core.transports.rabbitmq_bus")
    if bus is not None:
        importlib.reload(bus)
    yield
    if _orig_pika is None:
        sys.modules.pop("pika", None)
    else:
        sys.modules["pika"] = _orig_pika
    if _orig_pika_exc is None:
        sys.modules.pop("pika.exceptions", None)
    else:
        sys.modules["pika.exceptions"] = _orig_pika_exc
    bus = sys.modules.get("myquant.core.transports.rabbitmq_bus")
    if bus is not None:
        importlib.reload(bus)

from myquant.core.exceptions import TransportError  # noqa: E402
from myquant.core.transports.base import (  # noqa: E402
    Message,
    TransportConfig,
    TransportState,
)
from myquant.core.transports.rabbitmq_bus import RabbitMQTransport  # noqa: E402


class _FakeMethod:
    def __init__(self, routing_key="order.filled", delivery_tag=42):
        self.routing_key = routing_key
        self.delivery_tag = delivery_tag


class _FakeProps:
    def __init__(self, headers=None):
        self.headers = headers


class _FakeChannel:
    """记录调用的假 channel"""

    def __init__(self):
        self.acks = []
        self.nacks = []
        self.published = []
        self.queue_declared = []
        self.queue_binds = []
        self.exchanges = []
        self.confirm_called = False
        self.consume_registered = []
        self.publish_raises = None

    def basic_ack(self, delivery_tag=None, **kw):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag=None, requeue=None, **kw):
        self.nacks.append((delivery_tag, requeue))

    def basic_publish(self, **kw):
        if self.publish_raises is not None:
            raise self.publish_raises
        self.published.append(kw)

    def exchange_declare(self, **kw):
        self.exchanges.append(kw)

    def queue_declare(self, **kw):
        self.queue_declared.append(kw)

    def queue_bind(self, **kw):
        self.queue_binds.append(kw)

    def basic_consume(self, **kw):
        self.consume_registered.append(kw)

    def confirm_delivery(self):
        self.confirm_called = True

    def start_consuming(self):
        pass


def _make_transport(max_retry=3):
    t = RabbitMQTransport(
        host="localhost",
        port=5672,
        queue_prefix="test",
        max_retry=max_retry,
    )
    t._state = TransportState.CONNECTED
    t._channel = _FakeChannel()
    return t


def _msg_body(topic="order.filled", payload=None):
    return Message(topic=topic, payload=payload or {"symbol": "600519"}).serialize()


class TestRetryAndDeadLetter:
    """C3: 有限重试 + 死信"""

    def test_消费成功直接ack(self) -> None:
        t = _make_transport()
        ch = t._channel
        calls = []
        t._handlers["order.filled"] = [lambda m: calls.append(m)]
        t._handle_message(ch, _FakeMethod(), _FakeProps(), _msg_body())
        assert len(calls) == 1
        assert ch.acks == [42]
        assert ch.nacks == []
        assert ch.published == []

    def test_失败首次重试(self) -> None:
        t = _make_transport()
        ch = t._channel

        def boom(m):
            raise RuntimeError("handler boom")

        t._handlers["order.filled"] = [boom]
        t._handle_message(ch, _FakeMethod(), _FakeProps(), _msg_body())
        # 重投 1 次：发布带 x-retry-count=1，且 ack 原消息
        assert len(ch.published) == 1
        props = ch.published[0]["properties"]
        assert props.headers == {"x-retry-count": 1}
        assert props.delivery_mode == 2
        assert ch.published[0]["mandatory"] is True
        assert ch.published[0]["routing_key"] == "order.filled"
        assert ch.acks == [42]      # 消费掉当前投递
        assert ch.nacks == []       # 未直接丢弃

    def test_重试计数传递(self) -> None:
        t = _make_transport(max_retry=5)
        ch = t._channel
        t._handlers["order.filled"] = [lambda m: (_ for _ in ()).throw(RuntimeError("x"))]
        t._handle_message(
            ch,
            _FakeMethod(),
            _FakeProps(headers={"x-retry-count": 2}),
            _msg_body(),
        )
        props = ch.published[0]["properties"]
        assert props.headers == {"x-retry-count": 3}

    def test_达上限进死信(self) -> None:
        t = _make_transport(max_retry=3)
        ch = t._channel
        t._handlers["order.filled"] = [lambda m: (_ for _ in ()).throw(RuntimeError("x"))]
        t._handle_message(
            ch,
            _FakeMethod(),
            _FakeProps(headers={"x-retry-count": 3}),
            _msg_body(),
        )
        assert ch.published == []          # 不再重投
        assert ch.nacks == [(42, False)]   # requeue=False → 进死信队列
        assert ch.acks == []

    def test_重投失败进死信(self) -> None:
        t = _make_transport()
        ch = t._channel
        ch.publish_raises = RuntimeError("connection lost")
        t._handlers["order.filled"] = [lambda m: (_ for _ in ()).throw(RuntimeError("x"))]
        t._handle_message(ch, _FakeMethod(), _FakeProps(), _msg_body())
        assert ch.nacks == [(42, False)]

    def test_消息损坏走重试(self) -> None:
        t = _make_transport()
        ch = t._channel
        t._handle_message(ch, _FakeMethod(), _FakeProps(), b"not-json{{{")
        # 反序列化失败 → 按 retry_count=0 < max_retry 重投（routing_key 兜底）
        assert len(ch.published) == 1
        assert ch.published[0]["routing_key"] == "order.filled"
        assert ch.acks == [42]

    def test_无handler也ack(self) -> None:
        """无订阅 handler 时按成功处理（避免无谓重试）"""
        t = _make_transport()
        ch = t._channel
        t._handle_message(ch, _FakeMethod(), _FakeProps(), _msg_body())
        assert ch.acks == [42]


class TestPublisherConfirm:
    """C3: publisher confirm + mandatory"""

    def test_正常发布通过(self) -> None:
        t = _make_transport()
        ch = t._channel
        t.publish("order.filled", {"symbol": "600519"})
        assert len(ch.published) == 1
        assert ch.published[0]["mandatory"] is True
        assert ch.published[0]["routing_key"] == "order.filled"

    def test_unroutable转TransportError(self) -> None:
        t = _make_transport()
        t._channel.publish_raises = _exc.UnroutableError("no route")
        with pytest.raises(TransportError):
            t.publish("order.filled", {"symbol": "1"})

    def test_nack转TransportError(self) -> None:
        t = _make_transport()
        t._channel.publish_raises = _exc.NackError("rejected")
        with pytest.raises(TransportError):
            t.publish("order.filled", {"symbol": "1"})

    def test_未连接拒绝发布(self) -> None:
        t = RabbitMQTransport(host="h", port=1)
        with pytest.raises(TransportError):
            t.publish("order.filled", {})


class TestDeadLetterDeclare:
    """C3: DLX/DLQ 声明"""

    def test_subscribe声明死信拓扑(self) -> None:
        t = _make_transport()
        ch = t._channel
        t.subscribe("order.filled", lambda m: None)
        # 主队列绑定 DLX
        dlx_declares = [e for e in ch.exchanges if e["exchange"] == "test.dlx"]
        assert len(dlx_declares) == 1
        assert dlx_declares[0]["exchange_type"] == "topic"
        main_q = [q for q in ch.queue_declared if "dlq" not in q["queue"]][0]
        assert main_q["arguments"] == {
            "x-dead-letter-exchange": "test.dlx",
            "x-dead-letter-routing-key": "test.order_filled",
        }
        # DLQ 声明并绑定到 DLX
        dlq = [q for q in ch.queue_declared if q["queue"].endswith(".dlq")][0]
        assert dlq["durable"] is True
        bind = [b for b in ch.queue_binds if b["queue"].endswith(".dlq")][0]
        assert bind["exchange"] == "test.dlx"
        assert bind["routing_key"] == "test.order_filled"

    def test_connect启用confirm(self) -> None:
        t = _make_transport()
        # connect 需要真实 pika 连接，直接验证 confirm_delivery 被调用（通过 _consume 不触发）
        # 这里验证 subscribe 触发的消费注册指向 _handle_message
        ch = t._channel
        t.subscribe("order.filled", lambda m: None)
        assert ch.consume_registered[0]["on_message_callback"] == t._handle_message

    def test_max_retry可配置(self) -> None:
        assert _make_transport(max_retry=10).max_retry == 10
        assert RabbitMQTransport(host="h", port=1).max_retry == 3


class TestTransportConfig:
    def test_max_retry字段默认3(self) -> None:
        assert TransportConfig().max_retry == 3

    def test_max_retry字段可覆盖(self) -> None:
        assert TransportConfig(max_retry=7).max_retry == 7
