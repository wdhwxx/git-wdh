# 文件名：tests/unit/test_channel_assembly.py
"""
test_channel_assembly.py - 告警通道从未装配测试（KP-P5-05 / T-28）
========================================================================
覆盖修复：
  1. configure_notification_channels 按 notification.channels 装配 Email/Webhook。
  2. notification 未启用时返回 0（不装配）。
  3. 告警未投递时回退写 logs/alerts_undelivered.log。

依赖: pytest, myquant.monitoring.alerting
"""

from __future__ import annotations

from pathlib import Path

from myquant.core.constants import SeverityLevel
from myquant.monitoring.alerting import (
    Alert,
    AlertManager,
    configure_notification_channels,
)


class _FakeSettings:
    def __init__(self, notification, root: Path):
        self._notification = notification
        self._root = root

    def get(self, key: str, default=None):
        if key == "notification":
            return self._notification
        return default

    @property
    def project_root(self) -> Path:
        return self._root


class TestChannelAssembly:
    def test_按channels装配(self, tmp_path, monkeypatch) -> None:
        """DoD：enabled 时按 channels 装配 email/feishu/wecom 通道。"""
        notif = {
            "enabled": True,
            "channels": {
                "email": {
                    "enabled": True,
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 465,
                    "smtp_user": "u",
                    "smtp_password": "p",
                    "smtp_to": "a@b.com",
                },
                "feishu": {"webhook": "https://feishu/hook"},
                "wecom": {"webhook": "https://wecom/hook"},
            },
        }
        monkeypatch.setattr("myquant.core.settings.get_settings", lambda: _FakeSettings(notif, tmp_path))
        m = AlertManager()
        added = configure_notification_channels(m)
        assert added == 3
        assert m.get_channel("email") is not None
        assert m.get_channel("feishu") is not None
        assert m.get_channel("wecom") is not None

    def test_未启用不装配(self, tmp_path, monkeypatch) -> None:
        """DoD：notification.enabled=False 时不装配任何通道。"""
        monkeypatch.setattr(
            "myquant.core.settings.get_settings",
            lambda: _FakeSettings({"enabled": False, "channels": {}}, tmp_path),
        )
        m = AlertManager()
        added = configure_notification_channels(m)
        assert added == 0

    def test_未投递回退写日志(self, tmp_path, monkeypatch) -> None:
        """DoD：告警无通道投递时回退写 logs/alerts_undelivered.log。"""
        monkeypatch.setattr(
            "myquant.core.settings.get_settings",
            lambda: _FakeSettings({"enabled": False, "channels": {}}, tmp_path),
        )
        m = AlertManager()
        alert = Alert(
            level=SeverityLevel.WARNING,
            message="undelivered-test",
            source="test",
            category="health",
        )
        m.send_alert(alert)
        log_path = tmp_path / "logs" / "alerts_undelivered.log"
        assert log_path.exists()
        text = log_path.read_text(encoding="utf-8")
        assert "undelivered-test" in text
