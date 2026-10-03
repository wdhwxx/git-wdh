# 文件名：tests/unit/test_p113_webhook_nonblock.py
"""P1-13回归：WebhookAlertChannel.send 必须入队立即返回，不阻塞调用线程。"""

from __future__ import annotations

import time

from myquant.core.constants import SeverityLevel
from myquant.monitoring.alerting import Alert, WebhookAlertChannel


def test_send_returns_immediately_enqueues() -> None:
    ch = WebhookAlertChannel(webhook_url="http://127.0.0.1:1/none", timeout=10, retries=3)
    alert = Alert(level=SeverityLevel.CRITICAL, message="t", source="test", category="c")
    t0 = time.time()
    ok = ch.send(alert)
    elapsed = time.time() - t0
    assert ok is True
    assert elapsed < 1.0, f"send 阻塞了 {elapsed:.1f}s，未走后台队列"
    assert ch._send_q.qsize() >= 1, "应已入队由后台线程消费"
