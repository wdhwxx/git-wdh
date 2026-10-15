# 文件名：tests/unit/test_realtime_bridge.py
"""test_realtime_bridge.py - 实时清洗→事件总线联动桥（B项）单元测试（回写#423）

覆盖 _RealtimeEventBridge 全部路径：
  1. 构造（默认/自定义参数）
  2. on_dirty 窗口聚合 + 窗口翻转发布（确定性时间 monkeypatch）
  3. 连续阈值升级（critical / warning / severity_map 覆盖）
  4. on_clean 连续计数清零 + clean streak 累加
  5. flush_all 强制补发
  6. enabled 开关（on_dirty 不发布 / set_enabled 恢复）
  7. 发布失败故障隔离（不阻断行情分发）
  8. _infer_dirty_type（结构化错误码精确匹配 + 旧格式文本回退）
  9. 观测接口（published_count / clean_streak / consecutive_dirty / enabled）
  10. __repr__

零网络：FakeBus 记录 publish_data_quality 调用，不依赖事件总线实现。
"""

from typing import Any, Dict, List

import pytest

from myquant.live.realtime_bridge import _RealtimeEventBridge


class FakeBus:
    """记录 publish_data_quality 调用的假事件总线（可注入故障）。"""

    def __init__(self, fail_publish: bool = False) -> None:
        self.events: List[Dict[str, Any]] = []
        self.fail_publish = fail_publish

    def publish_data_quality(self, **kwargs: Any) -> None:
        if self.fail_publish:
            raise RuntimeError("bus down")
        self.events.append(kwargs)


@pytest.fixture
def bridge() -> _RealtimeEventBridge:
    return _RealtimeEventBridge(event_bus=FakeBus(), aggregate_window=60, critical_after=3)


def test_default_construction(bridge: _RealtimeEventBridge) -> None:
    """默认参数构造：enabled 开、计数归零、source=live。"""
    assert bridge.enabled is True
    assert bridge.published_count == 0
    assert "live" in repr(bridge)


def test_custom_parameters() -> None:
    """自定义参数：window/critical_after 取 max(1, 入参)，severity_map 保留。"""
    b = _RealtimeEventBridge(
        event_bus=FakeBus(),
        source="data_clean",
        severity_map={"price_jump": "critical"},
        aggregate_window=0,
        critical_after=-1,
        enabled=False,
    )
    assert b.enabled is False
    assert "data_clean" in repr(b)
    assert "window=1s" in repr(b)
    assert "critical_after=1" in repr(b)


def test_on_dirty_aggregates_within_window(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """窗口内多次脏数据合并为一条（不发布）。"""
    t0 = 1_000_000.0
    monkeypatch.setattr("myquant.live.realtime_bridge.time.time", lambda: t0)
    bar = {"symbol": "600000", "close": 10.0}
    bridge.on_dirty("600000", bar, ["[PRICE_JUMP] 价格跳变"])
    bridge.on_dirty("600000", bar, ["[PRICE_JUMP] 价格跳变"])
    bus = bridge._bus
    assert isinstance(bus, FakeBus)
    assert bus.events == []


def test_window_flip_publishes_previous(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """窗口到期：旧窗口发布（bad_count 累加），新窗口继续聚合。"""
    t0 = 1_000_000.0
    clock = {"t": t0}
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: clock["t"]
    )
    bar = {"symbol": "600000", "close": 10.0}
    bridge.on_dirty("600000", bar, ["[PRICE_JUMP] 价格跳变"])
    bridge.on_dirty("600000", bar, ["[PRICE_JUMP] 价格跳变"])
    assert bridge._bus.events == []  # type: ignore[union-attr]
    clock["t"] = t0 + 61.0  # 窗口过期（同 key 触发翻转）
    bridge.on_dirty("600000", bar, ["[PRICE_JUMP] 价格跳变"])
    bus = bridge._bus
    assert isinstance(bus, FakeBus)
    assert len(bus.events) == 1
    ev = bus.events[0]
    assert ev["dirty_type"] == "price_jump"
    assert ev["symbol"] == "600000"
    assert ev["bad_count"] == 2
    assert ev["total_count"] == 2
    assert ev["severity"] == "warning"
    assert ev["source"] == "live"
    assert ev["source_step"] == "realtime_feed"
    assert bridge.published_count == 1


def test_critical_escalation(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """严重类型连续 ≥ critical_after 次升级 critical（默认 critical_after=3）。"""
    t0 = 1_000_000.0
    clock = {"t": t0}
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: clock["t"]
    )
    bar = {"symbol": "600000", "close": -1.0}
    # 连续 3 次同窗口同 key（不发布，窗口内聚合）
    for _ in range(3):
        bridge.on_dirty("600000", bar, ["[NEGATIVE_PRICE] 负价"])
    assert bridge._bus.events == []  # type: ignore[union-attr]
    clock["t"] = t0 + 61.0
    bridge.on_dirty("600000", bar, ["[NEGATIVE_PRICE] 负价"])
    bus = bridge._bus
    assert isinstance(bus, FakeBus)
    ev = bus.events[0]
    assert ev["dirty_type"] == "negative_price"
    assert ev["bad_count"] == 3
    assert ev["severity"] == "critical"


def test_soft_type_always_warning(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非严重类型（price_jump）即使连续多次也不升级。"""
    t0 = 1_000_000.0
    clock = {"t": t0}
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: clock["t"]
    )
    bar = {"symbol": "600000", "close": 10.0}
    for _ in range(5):
        bridge.on_dirty("600000", bar, ["[PRICE_JUMP] 价格跳变"])
    clock["t"] = t0 + 61.0
    bridge.on_dirty("600000", bar, ["[PRICE_JUMP] 价格跳变"])
    bus = bridge._bus
    assert isinstance(bus, FakeBus)
    assert bus.events[0]["severity"] == "warning"


def test_severity_map_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """severity_map 自定义覆盖优先于默认判定。"""
    b = _RealtimeEventBridge(
        event_bus=FakeBus(), severity_map={"price_jump": "critical"}
    )
    t0 = 1_000_000.0
    clock = {"t": t0}
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: clock["t"]
    )
    b.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] 价格跳变"])
    clock["t"] = t0 + 61.0
    b.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] 价格跳变"])
    bus = b._bus
    assert isinstance(bus, FakeBus)
    assert bus.events[0]["severity"] == "critical"


def test_on_clean_resets_and_streak(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """干净 bar：clean streak 累加，该标的连续脏计数清零。"""
    t0 = 1_000_000.0
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: t0
    )
    bridge.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] x"])
    assert bridge.consecutive_dirty.get("600000:price_jump", 0) == 1
    bridge.on_clean("600000")
    assert bridge.clean_streak.get("600000", 0) == 1
    assert bridge.consecutive_dirty == {}


def test_flush_all_publishes_pending(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flush_all 强制补发窗口内未发布事件。"""
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: 1_000_000.0
    )
    bridge.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] x"])
    bridge.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] x"])
    assert bridge.published_count == 0
    bridge.flush_all()
    bus = bridge._bus
    assert isinstance(bus, FakeBus)
    assert len(bus.events) == 1
    assert bus.events[0]["bad_count"] == 2
    assert bridge.published_count == 1
    # 清空后再次 flush 无事件
    bridge.flush_all()
    assert len(bus.events) == 1


def test_disabled_blocks_publish(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enabled=False：on_dirty 不计数不发布；set_enabled(True) 恢复。"""
    bridge.set_enabled(False)
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: 1_000_000.0
    )
    bridge.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] x"])
    bridge.flush_all()
    bus = bridge._bus
    assert isinstance(bus, FakeBus)
    assert bus.events == []
    assert bridge.enabled is False
    bridge.set_enabled(True)
    assert bridge.enabled is True
    bridge.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] x"])
    bridge.flush_all()
    assert len(bus.events) == 1


def test_publish_failure_isolated(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布失败（总线异常）不影响行情分发：不抛出、计数不增、恢复后正常。"""
    bus = bridge._bus
    assert isinstance(bus, FakeBus)
    bus.fail_publish = True
    t0 = 1_000_000.0
    clock = {"t": t0}
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: clock["t"]
    )
    bridge.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] x"])  # 不 raise
    assert bridge.published_count == 0
    clock["t"] = t0 + 61.0
    bridge.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] x"])  # 窗口翻转，发布失败
    assert bridge.published_count == 0
    bus.fail_publish = False
    bridge.flush_all()  # 恢复后窗口仍在，可补发
    assert len(bus.events) == 1
    assert bridge.published_count == 1


def test_infer_dirty_type_structured() -> None:
    """结构化错误码精确匹配（含 critical 型）。"""
    assert (
        _RealtimeEventBridge._infer_dirty_type(["[PRICE_JUMP] 价格跳变"], None)
        == "price_jump"
    )
    assert (
        _RealtimeEventBridge._infer_dirty_type(["[NEGATIVE_PRICE] 负价"], None)
        == "negative_price"
    )
    assert (
        _RealtimeEventBridge._infer_dirty_type(["[DUPLICATE_TICK] 重复"], None)
        == "duplicate_tick"
    )


def test_infer_dirty_type_fallback() -> None:
    """旧格式文本回退模糊匹配。"""
    assert (
        _RealtimeEventBridge._infer_dirty_type(["价格跳变"], None)
        == "price_jump"
    )


def test_infer_dirty_type_unknown() -> None:
    """无法识别返回 unknown。"""
    assert _RealtimeEventBridge._infer_dirty_type([], None) == "unknown"
    assert (
        _RealtimeEventBridge._infer_dirty_type(["无法分类的异常"], None)
        == "unknown"
    )


def test_observations_snapshots(
    bridge: _RealtimeEventBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """观测接口返回只读快照（不指向内部可变状态）。"""
    monkeypatch.setattr(
        "myquant.live.realtime_bridge.time.time", lambda: 1_000_000.0
    )
    bridge.on_dirty("600000", {"close": 10.0}, ["[PRICE_JUMP] x"])
    bridge.on_clean("600000")
    streak = bridge.clean_streak
    streak["600000"] = 99  # 修改快照不影响内部
    assert bridge.clean_streak.get("600000", 0) == 1
    cd = bridge.consecutive_dirty
    assert cd == {}


def test_repr_contents(bridge: _RealtimeEventBridge) -> None:
    """__repr__ 包含 source/window/critical_after/published/enabled。"""
    r = repr(bridge)
    assert "live" in r
    assert "window=60s" in r
    assert "critical_after=3" in r
    assert "published=0" in r
    assert "enabled=True" in r
