# 文件名：tests/unit/test_security_monitor.py
"""
test_security_monitor.py - 安全监控单元测试（A-13，回写#217；P0-7 密钥零明文）
==========================================================================
验收口径（清单 A-13 行）：伪造密钥访问/异常登录即告警；密钥零明文落盘。
本测试注入安全事件 → 断言告警发出，且告警 details/message/状态快照均不含密钥原文。
"""

from __future__ import annotations

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.security_monitor import (
    SecurityMonitor,
    mask_secret,
    sha256_fingerprint,
    EVT_LOGIN_FAILED,
    EVT_UNAUTHORIZED_ACCESS,
    EVT_CREDENTIAL_CHANGE,
    EVT_SECRET_EXPOSURE,
)

#: 测试用伪造密钥（绝不可出现在任何告警 details/日志/落盘）
FAKE_SECRET: str = "sk-FAKESECRETKEY-xyz9876543210abcd"


class _FakeAlert:
    """记录 alert() 调用的假告警管理器。"""

    def __init__(self) -> None:
        self.calls: list = []

    def alert(self, **kw):
        self.calls.append(kw)
        return 1


class _FakeMetrics:
    """记录 security_events_total 计数的假指标采集器。"""

    def __init__(self) -> None:
        self.bucket: dict = {}

    def inc_counter(self, name, amount=1.0, labels=None):
        self.bucket.setdefault((labels or {}).get("event_type", ""), 0)
        self.bucket[(labels or {}).get("event_type", "")] += int(amount)


class TestLogin:
    """异常登录检测。"""

    def test_异常登录发P1告警(self) -> None:
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        mon.observe_login("trader01", ok=False)
        assert len(am.calls) == 1
        assert am.calls[0]["level"] == SeverityLevel.ERROR
        assert am.calls[0]["details"]["event_type"] == EVT_LOGIN_FAILED

    def test_正常登录不告警(self) -> None:
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        mon.observe_login("trader01", ok=True)
        assert am.calls == []


class TestUnauthorizedAccess:
    """未授权 API 访问（401/403）。"""

    def test_403未授权访问即告警(self) -> None:
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        mon.observe_unauthorized_access("anon", "/api/admin", status_code=403)
        assert len(am.calls) == 1
        assert am.calls[0]["level"] == SeverityLevel.ERROR
        assert am.calls[0]["details"]["status_code"] == 403
        assert am.calls[0]["details"]["endpoint"] == "/api/admin"

    def test_401未授权访问即告警(self) -> None:
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        mon.observe_unauthorized_access("unknown", "/api/secret", status_code=401)
        assert am.calls[0]["details"]["event_type"] == EVT_UNAUTHORIZED_ACCESS


class TestCredentialChange:
    """凭证变更 → P0/CRITICAL。"""

    def test_凭证变更发P0告警(self) -> None:
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        mon.observe_credential_change("ops", "fp_001122")
        assert am.calls[0]["level"] == SeverityLevel.CRITICAL
        assert am.calls[0]["details"]["event_type"] == EVT_CREDENTIAL_CHANGE
        assert am.calls[0]["details"]["credential_fingerprint"] == "fp_001122"


class TestSecretExposure:
    """疑似密钥泄漏检测：告警即发，但 details 零明文。"""

    def test_伪造密钥进日志即告警(self) -> None:
        """注入含伪造密钥的日志文本 → 立即 P1 告警。"""
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        fp = mon.observe_secret_exposure("log", f"connect failed api_key={FAKE_SECRET} timeout")
        assert fp is not None
        assert len(am.calls) == 1
        assert am.calls[0]["level"] == SeverityLevel.ERROR
        assert am.calls[0]["details"]["event_type"] == EVT_SECRET_EXPOSURE

    def test_告警details不含密钥原文(self) -> None:
        """P0-7：告警 details/message 不得含密钥原文。"""
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        mon.observe_secret_exposure("alert", f"token={FAKE_SECRET} bad")
        for call in am.calls:
            assert FAKE_SECRET not in call["message"]
            assert FAKE_SECRET not in str(call["details"])

    def test_告警只带sha256指纹(self) -> None:
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        fp = mon.observe_secret_exposure("exception", f"secret={FAKE_SECRET}")
        detail_fp = am.calls[0]["details"]["fingerprint"]
        assert detail_fp == fp
        # 指纹本身不是密钥原文，长度 16 位十六进制
        assert detail_fp != FAKE_SECRET
        assert len(detail_fp) == 16

    def test_scan_text只返回指纹列表(self) -> None:
        mon = SecurityMonitor(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        fps = mon.scan_text(f"api_key={FAKE_SECRET}")
        assert len(fps) >= 1
        assert FAKE_SECRET not in fps[0]

    def test_无密钥文本不告警(self) -> None:
        am = _FakeAlert()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=_FakeMetrics())
        fp = mon.observe_secret_exposure("log", "normal request handled ok")
        assert fp is None
        assert am.calls == []


class TestMetrics:
    """security_events_total 按事件类型计数。"""

    def test_安全事件按类型计数(self) -> None:
        met = _FakeMetrics()
        mon = SecurityMonitor(alert_manager=_FakeAlert(), metrics_collector=met)
        mon.observe_login("a", ok=False)
        mon.observe_unauthorized_access("a", "/x", 403)
        mon.observe_credential_change("a", "fp")
        mon.observe_secret_exposure("log", f"api_key={FAKE_SECRET}")
        assert met.bucket[EVT_LOGIN_FAILED] == 1
        assert met.bucket[EVT_UNAUTHORIZED_ACCESS] == 1
        assert met.bucket[EVT_CREDENTIAL_CHANGE] == 1
        assert met.bucket[EVT_SECRET_EXPOSURE] == 1


class TestZeroPlaintext:
    """密钥零明文落盘（断言告警 details / 状态快照均不含密钥原文）。"""

    def test_get_status不含密钥原文(self) -> None:
        am = _FakeAlert()
        met = _FakeMetrics()
        mon = SecurityMonitor(alert_manager=am, metrics_collector=met)
        mon.observe_secret_exposure("log", f"api_key={FAKE_SECRET}")
        status = mon.get_status()
        assert FAKE_SECRET not in str(status)

    def test_mask_secret不泄露完整原文(self) -> None:
        masked = mask_secret(FAKE_SECRET)
        assert FAKE_SECRET not in masked
        assert "****" in masked

    def test_fingerprint不等于原文(self) -> None:
        fp = sha256_fingerprint(FAKE_SECRET)
        assert fp != FAKE_SECRET
        assert FAKE_SECRET not in fp


class TestDefensive:
    """防御式校验。"""

    def test_空actor抛错(self) -> None:
        mon = SecurityMonitor(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        with pytest.raises(ValueError):
            mon.observe_login("", ok=False)

    def test_非bool登录结果抛错(self) -> None:
        mon = SecurityMonitor(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        with pytest.raises(ValueError):
            mon.observe_login("a", ok="yes")  # type: ignore[arg-type]

    def test_空endpoint抛错(self) -> None:
        mon = SecurityMonitor(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        with pytest.raises(ValueError):
            mon.observe_unauthorized_access("a", "", 403)

    def test_空credential_fingerprint抛错(self) -> None:
        mon = SecurityMonitor(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        with pytest.raises(ValueError):
            mon.observe_credential_change("a", "  ")

    def test_scan_text空输入安全(self) -> None:
        mon = SecurityMonitor(alert_manager=_FakeAlert(), metrics_collector=_FakeMetrics())
        assert mon.scan_text(None) == []
        assert mon.scan_text("") == []
