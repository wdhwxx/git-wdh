# -*- coding: utf-8 -*-
"""T-39 KP-P4-07 消息总线凭据：空凭据拒绝 guest/guest 静默回退（防御性）。"""
from pathlib import Path

import pytest

from myquant.core.exceptions import TransportConnectionError
from myquant.core.transports.base import TransportConfig
from myquant.core.transports.rabbitmq_bus import RabbitMQTransport

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "myquant" / "core" / "transports" / "base.py"
RABBIT = ROOT / "myquant" / "core" / "transports" / "rabbitmq_bus.py"


def test_transport_config_username_password_default_none():
    """TransportConfig 默认 username/password 为 None（不再默认空串）。"""
    cfg = TransportConfig()
    assert cfg.username is None
    assert cfg.password is None


def test_source_default_none_not_empty_string():
    """base.py 源码中 username/password 默认值改为 None。"""
    src = BASE.read_text(encoding="utf-8")
    assert "username: Optional[str] = None" in src
    assert "password: Optional[str] = None" in src
    assert 'username: str = ""' not in src


def test_empty_credentials_raise():
    """空凭据（None/空）调用 connect 抛 TransportConnectionError。"""
    cfg = TransportConfig()  # username/password = None
    bus = RabbitMQTransport(cfg)
    with pytest.raises(TransportConnectionError):
        bus.connect()


def test_partial_credentials_raise():
    """仅用户名无口令同样拒绝。"""
    cfg = TransportConfig(username="admin", password=None)
    bus = RabbitMQTransport(cfg)
    with pytest.raises(TransportConnectionError):
        bus.connect()


def test_source_no_guest_fallback():
    """rabbitmq_bus 不再出现 `or "guest"` 静默回退。"""
    src = RABBIT.read_text(encoding="utf-8")
    assert 'or "guest"' not in src
    assert "guest/guest" in src  # 注释说明拒绝回退


def test_credentials_present_not_raise():
    """凭据齐全不抛凭据缺失错误（未连真实 broker，仅验证不触发缺凭据分支）。"""
    cfg = TransportConfig(username="admin", password="secret")
    bus = RabbitMQTransport(cfg)
    # 无 pika 环境时抛 ImportError（说明通过了凭据校验），而非 TransportConnectionError
    try:
        bus.connect()
    except TransportConnectionError:
        pytest.fail("凭据齐全却报凭据缺失")
    except ImportError:
        pass  # 环境无 pika，凭据校验已通过
