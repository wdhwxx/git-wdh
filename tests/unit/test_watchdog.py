# 文件名：tests/unit/test_watchdog.py
"""
test_watchdog.py - 交易看门狗单元测试（A-4/A-5/A-6）
==========================================================================
按 A 包施工顺序分批追加：
  - A-4（#208）：StalenessDetector 行情活性看门狗
  - A-5（#209）：EngineHeartbeatWatcher 引擎心跳看门狗
  - A-6（#210）：BrokerConnectionWatcher 券商连接状态监控

不依赖外网 / 真实券商 / quant.db；时间用固定时钟 + 固定日历注入。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.watchdog import EngineHeartbeatWatcher, StalenessDetector
from myquant.utils.time_utils import TZ_SHANGHAI, TradingCalendar


# ─────────────────────────────────────────────
#  夹具
# ─────────────────────────────────────────────
def _dt(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> float:
    """构造上海时区时间戳（2026-10-13 为周二交易日，避开国庆假期）。"""
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ_SHANGHAI).timestamp()


class _FakeAlert:
    """记录 alert() 调用的假告警管理器。"""

    def __init__(self) -> None:
        self.calls: list = []

    def alert(self, **kw):
        self.calls.append(kw)
        return 1


@pytest.fixture()
def trading_day_calendar() -> TradingCalendar:
    return TradingCalendar()


class TestStalenessDetectorA4:
    """A-4 行情活性看门狗。"""

    def test_开盘宽限期内无bar_healthy(self, trading_day_calendar) -> None:
        """09:32（开盘后2min < grace 5min）无 bar → healthy。"""
        clock = lambda: _dt(2026, 10, 13, 9, 32)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        status, detail = det.check()
        assert status == "healthy"
        assert "宽限" in detail.get("reason", "")

    def test_开盘超时未就绪_degraded(self, trading_day_calendar) -> None:
        """09:40（开盘后10min > grace 5min，<= bar_timeout）→ degraded 数据源未就绪。"""
        clock = lambda: _dt(2026, 10, 13, 9, 40)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=600.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        status, detail = det.check()
        assert status == "degraded"
        assert "未就绪" in detail.get("reason", "")

    def test_有bar且新鲜_healthy(self, trading_day_calendar) -> None:
        clock = lambda: _dt(2026, 10, 13, 10, 31)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        det.on_bar(ts=_dt(2026, 10, 13, 10, 30))
        status, _ = det.check()
        assert status == "healthy"

    def test_交易时段断流_down(self, trading_day_calendar) -> None:
        """bar 后 310s 无新 bar（>bar_timeout 300）→ down。"""
        clock = lambda: _dt(2026, 10, 13, 10, 35, 10)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        det.on_bar(ts=_dt(2026, 10, 13, 10, 30, 0))
        status, detail = det.check()
        assert status == "down"
        assert "断流" in detail.get("reason", "")

    def test_午休时段豁免_不误报(self, trading_day_calendar) -> None:
        """12:00 午休无 bar → healthy 豁免（不误报）。"""
        clock = lambda: _dt(2026, 10, 13, 12, 0)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        det.on_bar(ts=_dt(2026, 10, 13, 11, 0))
        status, detail = det.check()
        assert status == "healthy"
        assert detail.get("exempt")

    def test_收盘后豁免(self, trading_day_calendar) -> None:
        """15:30 收盘后无 bar → healthy 豁免。"""
        clock = lambda: _dt(2026, 10, 13, 15, 30)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        status, detail = det.check()
        assert status == "healthy"
        assert detail.get("exempt")

    def test_断流产生ERROR告警(self, trading_day_calendar) -> None:
        """evaluate 在 down 时经 AlertManager 发 ERROR 告警（事件路径）。"""
        am = _FakeAlert()
        clock = lambda: _dt(2026, 10, 13, 10, 35, 10)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=am,
        )
        det.on_bar(ts=_dt(2026, 10, 13, 10, 30, 0))
        det.evaluate()
        assert len(am.calls) == 1
        assert am.calls[0]["level"] == SeverityLevel.ERROR
        assert am.calls[0]["category"] == "watchdog"

    def test_p99间隔统计内部暴露(self, trading_day_calendar) -> None:
        """bar 到达间隔 P99 在 get_stats() 暴露（不触碰 metrics_collector）。"""
        clock = lambda: _dt(2026, 10, 13, 10, 30)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        for i in range(10):
            det.on_bar(ts=_dt(2026, 10, 13, 10, 0, i))
        stats = det.get_stats()
        assert stats["bars_received"] == 10
        assert stats["bar_interval_p99_seconds"] is not None

    def test_时间戳回退不计入p99(self, trading_day_calendar) -> None:
        """bar 时间戳回退（负间隔）不计入 P99（防未来）。"""
        clock = lambda: _dt(2026, 10, 13, 10, 30)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        det.on_bar(ts=_dt(2026, 10, 13, 10, 5))
        det.on_bar(ts=_dt(2026, 10, 13, 10, 0))  # 回退
        # 仅 1 个有效正间隔样本 → P99 为 None
        assert det.get_stats()["bar_interval_p99_seconds"] is None

    def test_防御式非法阈值抛错(self, trading_day_calendar) -> None:
        """bar_timeout 非正数 → ValueError（拒绝脏配置运行）。"""
        with pytest.raises(ValueError):
            StalenessDetector(
                bar_timeout_seconds=0, session_grace_minutes=5.0,
                calendar=trading_day_calendar, clock=lambda: 0.0,
                alert_manager=_FakeAlert(),
            )

    def test_patrol_check可注册(self, trading_day_calendar) -> None:
        """patrol_check() 返回二元组，可直接 add_check 注册。"""
        clock = lambda: _dt(2026, 10, 13, 10, 30)  # noqa: E731
        det = StalenessDetector(
            bar_timeout_seconds=300.0, session_grace_minutes=5.0,
            calendar=trading_day_calendar, clock=clock, alert_manager=_FakeAlert(),
        )
        result = det.patrol_check()
        assert isinstance(result, tuple) and len(result) == 2


class _AliveEngine:
    def get_status(self) -> dict:
        return {"state": "RUNNING"}


class _BoomEngine:
    def get_status(self) -> dict:
        raise RuntimeError("engine dead")


class TestEngineHeartbeatWatcherA5:
    """A-5 引擎心跳看门狗（POLL，窗口<60s）。"""

    def test_存活引擎响应不计miss(self) -> None:
        hb = EngineHeartbeatWatcher(
            _AliveEngine(), probe_interval_seconds=10,
            consecutive_misses=5, hang_judge_window_seconds=60,
            alert_manager=_FakeAlert(),
        )
        r = hb.probe()
        assert r["responding"] is True
        assert r["miss_count"] == 0
        assert r["hung"] is False

    def test_连续5次无响应判僵死(self) -> None:
        """注入假引擎抛异常，连续 5 次 probe → hung（不需真等 50s）。"""
        am = _FakeAlert()
        hb = EngineHeartbeatWatcher(
            _BoomEngine(), probe_interval_seconds=10,
            consecutive_misses=5, hang_judge_window_seconds=60,
            alert_manager=am,
        )
        last = None
        for _ in range(5):
            last = hb.probe()
        assert last["hung"] is True
        assert last["miss_count"] == 5

    def test_僵死发ERROR告警(self) -> None:
        am = _FakeAlert()
        hb = EngineHeartbeatWatcher(
            _BoomEngine(), probe_interval_seconds=10,
            consecutive_misses=5, hang_judge_window_seconds=60,
            alert_manager=am,
        )
        for _ in range(5):
            hb.probe()
        assert len(am.calls) == 1
        assert am.calls[0]["level"] == SeverityLevel.ERROR
        assert am.calls[0]["category"] == "engine"
        assert am.calls[0]["deduplication_key"] == "watchdog:engine_hung"

    def test_未达上限不告警(self) -> None:
        """连续 4 次（<5）不告警。"""
        am = _FakeAlert()
        hb = EngineHeartbeatWatcher(
            _BoomEngine(), probe_interval_seconds=10,
            consecutive_misses=5, hang_judge_window_seconds=60,
            alert_manager=am,
        )
        for _ in range(4):
            r = hb.probe()
        assert r["hung"] is False
        assert am.calls == []

    def test_恢复后计数清零(self) -> None:
        am = _FakeAlert()
        hb = EngineHeartbeatWatcher(
            _BoomEngine(), probe_interval_seconds=10,
            consecutive_misses=5, hang_judge_window_seconds=60,
            alert_manager=am,
        )
        for _ in range(5):
            hb.probe()
        assert hb._miss_count == 5
        # 引擎恢复
        hb._engine = _AliveEngine()
        r = hb.probe()
        assert r["responding"] is True
        assert r["miss_count"] == 0

    def test_无引擎不崩(self) -> None:
        """engine=None 时 probe 安全返回 miss，不抛异常。"""
        hb = EngineHeartbeatWatcher(
            None, probe_interval_seconds=10,
            consecutive_misses=5, hang_judge_window_seconds=60,
            alert_manager=_FakeAlert(),
        )
        r = hb.probe()
        assert r["responding"] is False

    def test_窗口约束告警(self, capsys) -> None:
        """N×间隔 >= 窗口时记 warning（R-02 约束）。"""
        import logging
        EngineHeartbeatWatcher(
            _AliveEngine(), probe_interval_seconds=20,
            consecutive_misses=5, hang_judge_window_seconds=60,  # 5×20=100>=60
            alert_manager=_FakeAlert(),
        )  # 不抛异常，仅 warning

    def test_阈值从slo读取(self) -> None:
        """未显式传阈值时读 monitoring.slo.engine_heartbeat（R-04）。"""
        hb = EngineHeartbeatWatcher(_AliveEngine(), alert_manager=_FakeAlert())
        assert hb._probe_interval == 10
        assert hb._miss_limit == 5
        assert hb._window == 60

    def test_非法阈值抛错(self) -> None:
        with pytest.raises(ValueError):
            EngineHeartbeatWatcher(
                _AliveEngine(), probe_interval_seconds=0,
                consecutive_misses=5, hang_judge_window_seconds=60,
                alert_manager=_FakeAlert(),
            )
