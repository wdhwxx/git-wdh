# tests/unit/test_t0_11_pause_gate.py
"""
test_t0_11_pause_gate.py - T0-11 pause() 非 RUNNING 行为专项测试
=================================================================
验收 FI-19：
  1. 非 RUNNING 时 pause() 不再静默返回（force=False → warning 留痕，状态不变）
  2. force=True 强制暂停（不管当前状态）
  3. INIT 时 force=True pause → _startup_blocked=True，start() 保持 PAUSED
"""
from __future__ import annotations

import logging

from myquant.live.engine import EngineState, LiveEngine


def _make_minimal_engine() -> LiveEngine:
    """构造一个未 start 的引擎（INIT 状态），避免真实依赖。"""
    return LiveEngine(engine_id="test_t0_11")


class TestPauseNonRunning:
    """T0-11：非 RUNNING 时 pause() 行为。"""

    def test_pause_when_init_force_false_does_nothing(self, caplog):
        """INIT 状态 + force=False → 状态保持 INIT，warning 留痕。"""
        eng = _make_minimal_engine()
        assert eng._state == EngineState.INIT
        with caplog.at_level(logging.WARNING):
            eng.pause(reason="test")
        assert eng._state == EngineState.INIT  # 状态不变
        # 有 warning 留痕（不再静默）
        assert any("pause 被忽略" in r.message for r in caplog.records)

    def test_pause_when_stopped_force_false_does_nothing(self, caplog):
        """STOPPED 状态 + force=False → 状态不变，warning 留痕。"""
        eng = _make_minimal_engine()
        eng._state = EngineState.STOPPED
        with caplog.at_level(logging.WARNING):
            eng.pause(reason="test")
        assert eng._state == EngineState.STOPPED
        assert any("pause 被忽略" in r.message for r in caplog.records)

    def test_pause_force_true_when_init_sets_startup_blocked(self):
        """INIT 状态 + force=True → 置 PAUSED + _startup_blocked=True。"""
        eng = _make_minimal_engine()
        eng.pause(reason="启动前暂停", force=True)
        assert eng._state == EngineState.PAUSED
        assert eng._startup_blocked is True

    def test_pause_force_true_when_stopped_sets_paused(self):
        """STOPPED 状态 + force=True → 强制置 PAUSED（不进 RUNNING）。"""
        eng = _make_minimal_engine()
        eng._state = EngineState.STOPPED
        eng.pause(reason="强制", force=True)
        assert eng._state == EngineState.PAUSED

    def test_pause_when_running_works_normally(self):
        """RUNNING 状态 + force=False → 正常置 PAUSED（旧行为保留）。"""
        eng = _make_minimal_engine()
        eng._state = EngineState.RUNNING
        eng.pause(reason="正常暂停")
        assert eng._state == EngineState.PAUSED
        assert eng._pause_source == "正常暂停"
