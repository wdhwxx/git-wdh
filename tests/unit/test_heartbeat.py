# 文件名：tests/unit/test_heartbeat.py
"""
test_heartbeat.py - BASE-8 元监控外部兜底心跳单元测试
=====================================================
覆盖：
  1. 载体：FileHeartbeatCarrier 写/读/原子写；WebhookHeartbeatCarrier
     参数校验；get_heartbeat_carrier 注册表（file/webhook/非法名）
  2. HeartbeatWriter 守护线程：周期写心跳（seq 递增）、收尾心跳、幂等
  3. ExternalHeartbeatGuard（R-08 连续多次超时判定）：新鲜心跳 alive；
     单次超时 degraded 不判死（临界网络抖动不误判）；连续超时判 dead；
     心跳恢复计数清零；外部载体从无心跳 dead；reset
  4. MetaMonitor 集成：attach_heartbeat 后 run 随巡检发心跳（seq/pid/ts
     防未来=当前时刻）；未绑定不发心跳
  5. 防御式：非法载体名/非法间隔/非法连续次数/载体类型校验

运行方式:
  pytest tests/unit/test_heartbeat.py -v
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

import pytest

from myquant.core.event_bus import EventBus
from myquant.core.metrics_history import MetricsHistoryStore
from myquant.monitoring.alerting import AlertManager, LogAlertChannel
from myquant.monitoring.meta_monitor import (
    DEFAULT_HEARTBEAT_CONSECUTIVE_TIMEOUT,
    ExternalHeartbeatGuard,
    FileHeartbeatCarrier,
    HeartbeatWriter,
    MetaMonitor,
    WebhookHeartbeatCarrier,
    get_heartbeat_carrier,
)
from myquant.monitoring.metrics_collector import MetricsCollector


@pytest.fixture()
def hb_dir(tmp_path: Path) -> Path:
    return tmp_path / "heartbeat"


@pytest.fixture()
def carrier(hb_dir: Path) -> FileHeartbeatCarrier:
    return FileHeartbeatCarrier(str(hb_dir))


@pytest.fixture()
def components(tmp_path: Path) -> Dict[str, Any]:
    bus = EventBus(max_history=100)
    collector = MetricsCollector().register_builtin_metrics()
    store = MetricsHistoryStore(db_path=tmp_path / "meta_test.db")
    business = AlertManager()
    business.add_channel(LogAlertChannel(name="test-log", logger_name="test.business"))
    return {"bus": bus, "collector": collector, "store": store, "business": business}


# ═══════════════════════════════════════════
#  1. 载体
# ═══════════════════════════════════════════

class TestCarrier:
    def test_file_write_read(self, carrier: FileHeartbeatCarrier) -> None:
        assert carrier.send_heartbeat({"seq": 1, "ts": time.time(), "status": "ok"}) is True
        hb = carrier.read_last_heartbeat()
        assert hb is not None and hb["seq"] == 1 and hb["status"] == "ok"

    def test_file_no_heartbeat_returns_none(self, hb_dir: Path) -> None:
        empty = FileHeartbeatCarrier(str(hb_dir / "empty"))
        assert empty.read_last_heartbeat() is None

    def test_file_atomic_overwrite(self, carrier: FileHeartbeatCarrier) -> None:
        carrier.send_heartbeat({"seq": 1, "ts": 1.0})
        carrier.send_heartbeat({"seq": 2, "ts": 2.0})
        hb = carrier.read_last_heartbeat()
        assert hb["seq"] == 2 and hb["ts"] == 2.0  # 覆盖最近一次，无残留 tmp

    def test_file_empty_directory_raises(self) -> None:
        with pytest.raises(ValueError):
            FileHeartbeatCarrier("  ")

    def test_registry(self, hb_dir: Path) -> None:
        assert isinstance(
            get_heartbeat_carrier("file", directory=str(hb_dir)), FileHeartbeatCarrier
        )
        assert isinstance(
            get_heartbeat_carrier("webhook", url="http://localhost:9/hb"),
            WebhookHeartbeatCarrier,
        )
        with pytest.raises(ValueError):
            get_heartbeat_carrier("nope")

    def test_webhook_validation(self) -> None:
        with pytest.raises(ValueError):
            WebhookHeartbeatCarrier("")
        with pytest.raises(ValueError):
            WebhookHeartbeatCarrier("http://x", timeout_seconds=0)

    def test_webhook_send_failure_not_raising(self) -> None:
        # 无外部服务 → 发送失败记 warning 不抛（不阻断主流程）
        w = WebhookHeartbeatCarrier("http://127.0.0.1:9/hb", timeout_seconds=1.0)
        assert w.send_heartbeat({"seq": 1, "ts": time.time()}) is False
        assert w.read_last_heartbeat() is None  # 推送单向


# ═══════════════════════════════════════════
#  2. HeartbeatWriter
# ═══════════════════════════════════════════

class TestHeartbeatWriter:
    def test_periodic_write(self, hb_dir: Path) -> None:
        c = FileHeartbeatCarrier(str(hb_dir / "w1"))
        w = HeartbeatWriter(c, interval_seconds=0.2)
        assert w.start() is True
        time.sleep(0.6)
        hb = c.read_last_heartbeat()
        assert hb is not None and int(hb["seq"]) >= 2
        assert w.last_written_ts is not None
        w.stop(final_heartbeat=True)

    def test_start_idempotent(self, hb_dir: Path) -> None:
        c = FileHeartbeatCarrier(str(hb_dir / "w2"))
        w = HeartbeatWriter(c, interval_seconds=0.2)
        w.start()
        assert w.start() is True  # 幂等
        w.stop(final_heartbeat=False)

    def test_final_heartbeat_on_stop(self, hb_dir: Path) -> None:
        c = FileHeartbeatCarrier(str(hb_dir / "w3"))
        w = HeartbeatWriter(c, interval_seconds=0.2)
        w.start()
        time.sleep(0.25)
        w.stop(final_heartbeat=True)
        hb = c.read_last_heartbeat()
        assert hb is not None  # 收尾再写一次，防收尾前误判

    def test_payload_carries_status(self, hb_dir: Path) -> None:
        c = FileHeartbeatCarrier(str(hb_dir / "w4"))
        w = HeartbeatWriter(c, interval_seconds=0.2)
        w.start(status_callable=lambda: {"status": "healthy"})
        time.sleep(0.5)
        hb = c.read_last_heartbeat()
        assert hb["status"] == "healthy"
        assert int(hb["pid"]) > 0
        w.stop(final_heartbeat=False)

    def test_validation(self, hb_dir: Path) -> None:
        with pytest.raises(ValueError):
            HeartbeatWriter("not-a-carrier")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            HeartbeatWriter(FileHeartbeatCarrier(str(hb_dir)), interval_seconds=0)


# ═══════════════════════════════════════════
#  3. ExternalHeartbeatGuard（R-08 连续多次超时）
# ═══════════════════════════════════════════

class TestExternalGuard:
    def test_fresh_alive(self, carrier: FileHeartbeatCarrier) -> None:
        now = time.time()
        carrier.send_heartbeat({"seq": 1, "ts": now})
        g = ExternalHeartbeatGuard(interval_seconds=60.0, consecutive_timeout=2)
        alive, st, missed = g.check_alive(carrier, now=now)
        assert alive and st == "alive" and missed == 0

    def test_single_timeout_not_dead(self, carrier: FileHeartbeatCarrier) -> None:
        """R-08：临界网络抖动单次超时不误判"主进程死"。"""
        now = time.time()
        carrier.send_heartbeat({"seq": 1, "ts": now})
        g = ExternalHeartbeatGuard(interval_seconds=60.0, consecutive_timeout=2)
        alive, st, missed = g.check_alive(carrier, now=now + 121.0)
        assert alive and st == "degraded" and missed == 1

    def test_consecutive_timeout_dead(self, carrier: FileHeartbeatCarrier) -> None:
        now = time.time()
        carrier.send_heartbeat({"seq": 1, "ts": now})
        g = ExternalHeartbeatGuard(interval_seconds=60.0, consecutive_timeout=2)
        g.check_alive(carrier, now=now + 121.0)   # 1 次超时
        alive, st, missed = g.check_alive(carrier, now=now + 181.0)  # 连续 2 次
        assert not alive and st == "dead" and missed == 2

    def test_recovery_resets_count(self, carrier: FileHeartbeatCarrier) -> None:
        now = time.time()
        carrier.send_heartbeat({"seq": 1, "ts": now})
        g = ExternalHeartbeatGuard(interval_seconds=60.0, consecutive_timeout=2)
        g.check_alive(carrier, now=now + 121.0)   # 1 次超时
        carrier.send_heartbeat({"seq": 2, "ts": now + 122.0})  # 心跳恢复
        alive, st, missed = g.check_alive(carrier, now=now + 122.0)
        assert alive and st == "alive" and missed == 0

    def test_no_heartbeat_dead(self, hb_dir: Path) -> None:
        c = FileHeartbeatCarrier(str(hb_dir / "never"))
        g = ExternalHeartbeatGuard(interval_seconds=60.0, consecutive_timeout=2)
        alive, st, missed = g.check_alive(c, now=time.time())
        assert not alive and st == "dead" and missed == 2

    def test_reset(self, carrier: FileHeartbeatCarrier) -> None:
        now = time.time()
        carrier.send_heartbeat({"seq": 1, "ts": now})
        g = ExternalHeartbeatGuard(interval_seconds=60.0, consecutive_timeout=2)
        g.check_alive(carrier, now=now + 121.0)
        g.reset()
        alive, st, missed = g.check_alive(carrier, now=now + 121.0)
        assert alive and st == "degraded" and missed == 1  # 从 0 重新计数

    def test_default_threshold(self) -> None:
        assert DEFAULT_HEARTBEAT_CONSECUTIVE_TIMEOUT >= 2

    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            ExternalHeartbeatGuard(interval_seconds=0)
        with pytest.raises(ValueError):
            ExternalHeartbeatGuard(consecutive_timeout=1)  # R-08 须 >=2
        with pytest.raises(ValueError):
            ExternalHeartbeatGuard(consecutive_timeout=2.5)  # type: ignore[arg-type]


# ═══════════════════════════════════════════
#  4. MetaMonitor 集成
# ═══════════════════════════════════════════

class TestMetaMonitorIntegration:
    def make_meta(self, components: Dict[str, Any], **kw: Any) -> MetaMonitor:
        return MetaMonitor(
            collector=components["collector"],
            metrics_history=components["store"],
            event_bus=components["bus"],
            business_alert_manager=components["business"],
            **kw,
        )

    def test_run_sends_heartbeat(self, components: Dict[str, Any], hb_dir: Path) -> None:
        c = FileHeartbeatCarrier(str(hb_dir / "meta"))
        meta = self.make_meta(components)
        meta.attach_heartbeat(c, interval_seconds=60.0)
        meta.run()
        hb = c.read_last_heartbeat()
        assert hb is not None and int(hb["seq"]) == 1
        assert int(hb["pid"]) > 0
        assert hb["ts"] <= time.time()  # 防未来：心跳时刻=当前
        assert "status" in hb  # 携带最近元监控状态

    def test_attach_heartbeat_validation(self, components: Dict[str, Any]) -> None:
        meta = self.make_meta(components)
        with pytest.raises(ValueError):
            meta.attach_heartbeat("not-carrier")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            meta.attach_heartbeat(FileHeartbeatCarrier("."), interval_seconds=0)

    def test_without_heartbeat_no_write(self, components: Dict[str, Any], hb_dir: Path) -> None:
        meta = self.make_meta(components)
        meta.run()  # 未绑定不发心跳
        assert not (hb_dir / "meta2" / "heartbeat.json").exists()
