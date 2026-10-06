# 文件名：tests/unit/test_notification_redundancy.py
"""
test_notification_redundancy.py - A-9 告警通道冗余单元测试
========================================================================
覆盖（FI-06 主通道故障时告警仍可达：冗余通道或落盘兜底）：
  1. 主通道故障（send 返回 False）→ 备用通道仍送达，且按配置顺序降级（主先备后）
  2. P0（CRITICAL）告警冗余：两通道均可达 → sent_count >= 2
  3. 所有通道均失败 → 落盘 logs/alerts_undelivered.log 含完整内容
     （message + details JSON，含 symbol/order_id）
  4. 元监控计数：undelivered_count 递增；失败通道 error_count 记录
  5. 通道 send 抛异常 → 捕获并计失败，不阻断其余通道
  6. configure_notification_channels 装配钉钉 webhook（feishu/wecom/dingtalk）

运行方式:
  pytest tests/unit/test_notification_redundancy.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from myquant.core.constants import SeverityLevel
from myquant.monitoring.alerting import (
    Alert,
    AlertChannel,
    AlertManager,
    AlertRule,
    configure_notification_channels,
)


# ═══════════════════════════════════════════
#  Fake 通道
# ═══════════════════════════════════════════

class _WorkingChannel(AlertChannel):
    """工作正常通道：捕获告警，send 恒成功。"""

    def __init__(self, name: str,
                 min_level: SeverityLevel = SeverityLevel.INFO):
        super().__init__(name=name, min_level=min_level)
        self.alerts: List[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        self._record_success()
        return True


class _FailingChannel(AlertChannel):
    """主通道故障：send 返回 False 并计失败（模拟 webhook/邮件不可达）。"""

    def __init__(self, name: str = "primary",
                 min_level: SeverityLevel = SeverityLevel.INFO,
                 raise_exc: bool = False):
        super().__init__(name=name, min_level=min_level)
        self._raise_exc = raise_exc

    def send(self, alert: Alert) -> bool:
        if self._raise_exc:
            self._record_failure("network down")
            raise RuntimeError("simulated channel network failure")
        self._record_failure("connection refused")
        return False


class _FakeSettings:
    def __init__(self, notification: Dict[str, Any], root: Path):
        self._notification = notification
        self._root = root

    def get(self, key: str, default=None):
        if key == "notification":
            return self._notification
        return default

    @property
    def project_root(self) -> Path:
        return self._root


def _manager_with_rule() -> AlertManager:
    """构造纯内存 AlertManager，按 [primary, backup] 顺序路由 CRITICAL/ERROR。"""
    manager = AlertManager(store=None, store_enabled=False)
    rule = AlertRule.default_rule()
    rule.level_channels = {
        SeverityLevel.CRITICAL: ["primary", "backup"],
        SeverityLevel.ERROR: ["primary", "backup"],
        SeverityLevel.WARNING: ["backup"],
        SeverityLevel.INFO: ["backup"],
    }
    manager.set_alert_rule(rule)
    return manager


def _critical_alert() -> Alert:
    return Alert(
        level=SeverityLevel.CRITICAL,
        message="[P0] 触发紧急强平 600519.SH",
        source="live_engine",
        category="trade",
        details={"symbol": "600519.SH", "order_id": "O_EL_9",
                 "reason": "风险敞口超限"},
        deduplication_key="trade:emergency.liquidation:O_EL_9",
    )


# ═══════════════════════════════════════════
#  1. 主通道故障 → 备用通道送达，按配置顺序降级
# ═══════════════════════════════════════════

class TestFailover:
    def test_primary_fails_backup_delivers(self) -> None:
        """FI-06：主通道故障，备用通道仍送达；降级顺序=主先试、后转备用。"""
        manager = _manager_with_rule()
        primary = _FailingChannel(name="primary")
        backup = _WorkingChannel(name="backup")
        manager.add_channel(primary)
        manager.add_channel(backup)

        sent = manager.send_alert(_critical_alert())

        # 备用通道送达（冗余兜底），主通道失败不阻断
        assert sent == 1
        assert len(backup.alerts) == 1
        # 降级顺序留痕：primary 先试（失败），backup 后试（成功）
        attempts = manager.stats["last_channel_attempts"]
        assert [a["name"] for a in attempts] == ["primary", "backup"]
        assert attempts[0]["success"] is False
        assert attempts[1]["success"] is True

    def test_primary_error_count_recorded(self) -> None:
        """元监控：失败通道 error_count 递增。"""
        manager = _manager_with_rule()
        primary = _FailingChannel(name="primary")
        manager.add_channel(primary)
        manager.add_channel(_WorkingChannel(name="backup"))

        manager.send_alert(_critical_alert())

        assert primary.stats["error_count"] == 1

    def test_raise_exception_does_not_break_others(self) -> None:
        """通道 send 抛异常 → 捕获计失败，不阻断其余通道。"""
        manager = _manager_with_rule()
        boomer = _FailingChannel(name="primary", raise_exc=True)
        backup = _WorkingChannel(name="backup")
        manager.add_channel(boomer)
        manager.add_channel(backup)

        sent = manager.send_alert(_critical_alert())

        assert sent == 1               # 异常通道不影响备用通道送达
        assert backup.stats["sent_count"] == 1


# ═══════════════════════════════════════════
#  2. P0 冗余：CRITICAL 至少 2 通道可达
# ═══════════════════════════════════════════

class TestCriticalRedundancy:
    def test_critical_reaches_two_channels(self) -> None:
        manager = _manager_with_rule()
        manager.add_channel(_WorkingChannel(name="primary"))
        manager.add_channel(_WorkingChannel(name="backup"))

        sent = manager.send_alert(_critical_alert())

        assert sent == 2               # P0 至少 2 通道可达


# ═══════════════════════════════════════════
#  3. 全部失败 → 落盘含完整内容 + 元监控计数
# ═══════════════════════════════════════════

class TestUndeliveredFallback:
    def test_all_fail_writes_full_content(self, tmp_path, monkeypatch) -> None:
        """所有通道失败 → 落盘含 message + details（symbol/order_id）。"""
        monkeypatch.setattr(
            "myquant.core.settings.get_settings",
            lambda: _FakeSettings({"enabled": False, "channels": {}}, tmp_path),
        )
        manager = _manager_with_rule()
        manager.add_channel(_FailingChannel(name="primary"))   # 唯一通道，必失败

        sent = manager.send_alert(_critical_alert())

        assert sent == 0
        # 元监控：未投递计数递增
        assert manager.stats["undelivered_count"] == 1
        # 落盘文件存在且含完整内容
        log_path = tmp_path / "logs" / "alerts_undelivered.log"
        assert log_path.exists()
        text = log_path.read_text(encoding="utf-8")
        assert "触发紧急强平" in text          # message
        assert "600519.SH" in text            # details.symbol
        assert "O_EL_9" in text               # details.order_id
        assert "risk" not in text or "reason" in text

    def test_undelivered_increments_per_alert(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "myquant.core.settings.get_settings",
            lambda: _FakeSettings({"enabled": False, "channels": {}}, tmp_path),
        )
        manager = _manager_with_rule()
        manager.add_channel(_FailingChannel(name="primary"))

        manager.send_alert(_critical_alert())
        manager.send_alert(_critical_alert())   # dedup 抑制第二次（同 dedup_key）

        # 去重窗口内第二条被抑制，不计未投递；undelivered_count=1
        assert manager.stats["undelivered_count"] == 1


# ═══════════════════════════════════════════
#  6. configure 装配钉钉 webhook
# ═══════════════════════════════════════════

class TestDingtalkAssembly:
    def test_dingtalk_webhook_assembled(self, tmp_path, monkeypatch) -> None:
        notif = {
            "enabled": True,
            "channels": {
                "dingtalk": {"webhook": "https://dingtalk/robot"},
                "feishu": {"webhook": "https://feishu/hook"},
            },
        }
        monkeypatch.setattr(
            "myquant.core.settings.get_settings",
            lambda: _FakeSettings(notif, tmp_path),
        )
        manager = AlertManager(store=None, store_enabled=False)
        added = configure_notification_channels(manager)

        assert added == 2
        assert manager.get_channel("dingtalk") is not None
        assert manager.get_channel("feishu") is not None
