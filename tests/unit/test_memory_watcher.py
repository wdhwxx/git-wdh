# 文件名：tests/unit/test_memory_watcher.py
"""
test_memory_watcher.py - P3-2 内存水位监控回归单元测试
======================================================
覆盖 myquant/utils/memory_watcher.py：
  - MemoryLevel      三级水位枚举
  - MemoryWatcher    内存水位监控器（阈值 / 水位判断 / 回调 / 统计 / 上下文管理器）
  - memory_guard     内存防护上下文管理器

测试维度:
  1. 初始化：默认阈值、阈值合法性防御校验
  2. 禁用态：enabled=False 时恒为 NORMAL / disabled 信息
  3. 水位判断：get_memory_info 字段、check、is_* 互斥判断
  4. 水位映射：_determine_level 边界（<warning / warning~critical / >=critical）
  5. 回调状态机：水位上升触发、同水位不重复、下降不触发、注册/注销
  6. 统计与上下文：get_stats / reset_stats / memory_guard / with 协议

运行方式:
  pytest tests/unit/test_memory_watcher.py -v
"""

from __future__ import annotations

from typing import Any

import pytest

from myquant.utils.memory_watcher import (
    MemoryLevel,
    MemoryWatcher,
    memory_guard,
)

# ═════════════════════════════════════════════
#  1. 初始化与阈值防御
# ═════════════════════════════════════════════

class TestInit:
    """初始化与阈值校验测试。"""

    def test_defaults(self) -> None:
        """默认阈值 warning=70, critical=85, enabled=True。"""
        watcher = MemoryWatcher()
        assert watcher.warning_pct == 70.0
        assert watcher.critical_pct == 85.0
        assert watcher.enabled is True

    def test_custom_thresholds(self) -> None:
        """自定义阈值应生效。"""
        watcher = MemoryWatcher(warning_pct=50, critical_pct=90)
        assert watcher.warning_pct == 50.0
        assert watcher.critical_pct == 90.0

    def test_warning_zero_raises(self) -> None:
        """warning_pct=0 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            MemoryWatcher(warning_pct=0)

    def test_critical_over_100_raises(self) -> None:
        """critical_pct=101 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            MemoryWatcher(critical_pct=101)

    def test_warning_gte_critical_raises(self) -> None:
        """warning_pct >= critical_pct 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            MemoryWatcher(warning_pct=90, critical_pct=80)


# ═════════════════════════════════════════════
#  2. 禁用态
# ═════════════════════════════════════════════

class TestDisabled:
    """监控禁用行为测试。"""

    def test_disabled_always_normal(self) -> None:
        """禁用时 check 恒为 NORMAL。"""
        watcher = MemoryWatcher(enabled=False)
        assert watcher.check() == MemoryLevel.NORMAL
        assert watcher.is_normal() is True
        assert watcher.is_warning() is False
        assert watcher.is_critical() is False
        assert watcher.is_above_warning() is False

    def test_disabled_info_fields(self) -> None:
        """禁用时信息应标记 source=disabled 且 level=NORMAL。"""
        watcher = MemoryWatcher(enabled=False)
        info = watcher.get_memory_info()
        assert info["source"] == "disabled"
        assert info["level"] == MemoryLevel.NORMAL
        assert info["percent"] == 0.0


# ═════════════════════════════════════════════
#  3. 水位判断
# ═════════════════════════════════════════════

class TestWatermark:
    """水位判断与信息字段测试。"""

    def test_get_memory_info_fields(self) -> None:
        """get_memory_info 应返回完整字段。"""
        watcher = MemoryWatcher()
        info = watcher.get_memory_info(force_refresh=True)
        for key in (
            "rss_mb", "vms_mb", "percent", "system_total_mb",
            "system_available_mb", "level", "level_name", "timestamp", "source",
        ):
            assert key in info, f"缺少字段: {key}"
        assert info["source"] in ("psutil", "tracemalloc", "disabled")

    def test_flags_mutually_exclusive(self) -> None:
        """is_normal / is_warning / is_critical 三者应有且仅有一个为 True。"""
        watcher = MemoryWatcher()
        flags = [watcher.is_normal(), watcher.is_warning(), watcher.is_critical()]
        assert sum(flags) == 1
        # is_above_warning 与 is_normal 互斥
        assert watcher.is_above_warning() != watcher.is_normal()

    def test_check_returns_level_enum(self) -> None:
        """check() 应返回 MemoryLevel 枚举。"""
        watcher = MemoryWatcher()
        level = watcher.check()
        assert isinstance(level, MemoryLevel)
        assert level in (MemoryLevel.NORMAL, MemoryLevel.WARNING, MemoryLevel.CRITICAL)

    def test_determine_level_boundaries(self) -> None:
        """水位映射边界：<warning→NORMAL；warning≤x<critical→WARNING；≥critical→CRITICAL。"""
        watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
        assert watcher._determine_level(10.0) == MemoryLevel.NORMAL
        assert watcher._determine_level(69.99) == MemoryLevel.NORMAL
        assert watcher._determine_level(70.0) == MemoryLevel.WARNING
        assert watcher._determine_level(84.99) == MemoryLevel.WARNING
        assert watcher._determine_level(85.0) == MemoryLevel.CRITICAL
        assert watcher._determine_level(99.0) == MemoryLevel.CRITICAL


# ═════════════════════════════════════════════
#  4. 回调机制（状态机）
# ═════════════════════════════════════════════

class TestCallbacks:
    """水位回调状态机测试。"""

    def test_register_and_unregister(self) -> None:
        """注册回调返回 cb_ 前缀 ID；注销成功/失败。"""
        watcher = MemoryWatcher()

        def _cb(level: MemoryLevel, info: dict[str, Any]) -> None:
            pass

        cb_id = watcher.register_callback(MemoryLevel.WARNING, _cb)
        assert cb_id.startswith("cb_")
        assert watcher.unregister_callback(cb_id) is True
        assert watcher.unregister_callback(cb_id) is False

    def test_register_non_callable_raises(self) -> None:
        """注册非可调用对象应抛出 TypeError。"""
        watcher = MemoryWatcher()
        with pytest.raises(TypeError):
            watcher.register_callback(MemoryLevel.WARNING, "not_callable")  # type: ignore[arg-type]

    def test_warning_rise_triggers_once(self) -> None:
        """水位上升触发一次；同水位持续不重复触发。"""
        watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
        fired: list[str] = []

        def _on_warning(level: MemoryLevel, info: dict[str, Any]) -> None:
            fired.append("warning")

        watcher.register_callback(MemoryLevel.WARNING, _on_warning)
        # 水位升至 WARNING → 触发
        watcher._maybe_trigger_callbacks(MemoryLevel.WARNING, {"percent": 75.0})
        assert fired == ["warning"]
        # 持续 WARNING → 不重复触发
        watcher._maybe_trigger_callbacks(MemoryLevel.WARNING, {"percent": 78.0})
        assert fired == ["warning"]

    def test_rise_to_critical_triggers_critical_callback(self) -> None:
        """水位升至 CRITICAL 应触发 critical 回调（且 >= 注册水位都触发）。"""
        watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
        fired: list[str] = []

        def _on_warning(level: MemoryLevel, info: dict[str, Any]) -> None:
            fired.append("warning")

        def _on_critical(level: MemoryLevel, info: dict[str, Any]) -> None:
            fired.append("critical")

        watcher.register_callback(MemoryLevel.WARNING, _on_warning)
        watcher.register_callback(MemoryLevel.CRITICAL, _on_critical)
        watcher._maybe_trigger_callbacks(MemoryLevel.CRITICAL, {"percent": 90.0})
        # 当前水位 >= 注册水位 的回调都会触发
        assert "warning" in fired
        assert "critical" in fired

    def test_sequential_rise_warning_to_critical(self) -> None:
        """连续上升 WARNING→CRITICAL 应触发对应回调（修复字符串比较导致上升被误判为下降的缺陷）。

        设计语义：每次水位升档，所有"注册水位 <= 当前水位"的回调都会触发，
        故 WARNING 注册回调在升至 CRITICAL 时会再次触发。
        """
        watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
        fired: list[str] = []

        def _on_warning(level: MemoryLevel, info: dict[str, Any]) -> None:
            fired.append("warning")

        def _on_critical(level: MemoryLevel, info: dict[str, Any]) -> None:
            fired.append("critical")

        watcher.register_callback(MemoryLevel.WARNING, _on_warning)
        watcher.register_callback(MemoryLevel.CRITICAL, _on_critical)
        # 先升至 WARNING（触发 warning）
        watcher._maybe_trigger_callbacks(MemoryLevel.WARNING, {"percent": 75.0})
        assert fired == ["warning"]
        # 再升至 CRITICAL：warning 注册回调（2<=3）与 critical 注册回调（3<=3）都触发
        watcher._maybe_trigger_callbacks(MemoryLevel.CRITICAL, {"percent": 90.0})
        assert fired.count("warning") == 2
        assert fired.count("critical") == 1

    def test_registered_level_filter(self) -> None:
        """当前水位低于注册水位时不触发（如 WARNING 不应触发 CRITICAL 注册回调）。"""
        watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
        fired: list[str] = []

        def _on_critical(level: MemoryLevel, info: dict[str, Any]) -> None:
            fired.append("critical")

        watcher.register_callback(MemoryLevel.CRITICAL, _on_critical)
        watcher._maybe_trigger_callbacks(MemoryLevel.WARNING, {"percent": 75.0})
        assert fired == []  # 未达 CRITICAL 注册水位，不触发

    def test_drop_level_does_not_trigger(self) -> None:
        """水位下降不应触发回调。"""
        watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
        fired: list[str] = []

        def _on_critical(level: MemoryLevel, info: dict[str, Any]) -> None:
            fired.append("critical")

        watcher.register_callback(MemoryLevel.CRITICAL, _on_critical)
        watcher._maybe_trigger_callbacks(MemoryLevel.CRITICAL, {"percent": 90.0})
        assert fired == ["critical"]
        # 水位下降 → 不触发，且后续再升会重新触发
        watcher._maybe_trigger_callbacks(MemoryLevel.NORMAL, {"percent": 10.0})
        assert fired == ["critical"]
        watcher._maybe_trigger_callbacks(MemoryLevel.CRITICAL, {"percent": 91.0})
        assert len(fired) == 2

    def test_clear_callbacks(self) -> None:
        """clear_callbacks 应清空全部回调并重置水位记录。"""
        watcher = MemoryWatcher()
        watcher.register_callback(
            MemoryLevel.WARNING, lambda l, i: None
        )
        watcher.clear_callbacks()
        watcher._maybe_trigger_callbacks(MemoryLevel.WARNING, {"percent": 75.0})
        assert watcher.get_stats()["callback_count"] == 0


# ═════════════════════════════════════════════
#  5. 统计与上下文管理器
# ═════════════════════════════════════════════

class TestStatsAndContext:
    """统计与上下文管理器测试。"""

    def test_get_stats_and_reset(self) -> None:
        """get_stats 应含核心计数；reset_stats 应清零。"""
        watcher = MemoryWatcher()
        stats = watcher.get_stats()
        for key in (
            "check_count", "warning_count", "critical_count",
            "callback_count", "peak_rss_mb", "peak_percent",
        ):
            assert key in stats, f"缺少统计字段: {key}"
        watcher.check()
        watcher.reset_stats()
        assert watcher.get_stats()["check_count"] == 0

    def test_context_manager_protocol(self) -> None:
        """with MemoryWatcher(...) 应支持上下文协议并清理回调。"""
        with MemoryWatcher(warning_pct=70, critical_pct=85) as watcher:
            assert isinstance(watcher, MemoryWatcher)
            watcher.register_callback(
                MemoryLevel.WARNING, lambda l, i: None
            )
        # 退出后回调已清理
        assert watcher._callbacks == {}

    def test_memory_guard_yields_watcher(self) -> None:
        """memory_guard 应产出 watcher 且正常退出。"""
        with memory_guard(warning_pct=70, critical_pct=85) as watcher:
            assert isinstance(watcher, MemoryWatcher)
            info = watcher.get_memory_info(force_refresh=True)
            assert "level" in info

    def test_memory_guard_on_critical_registered(self) -> None:
        """memory_guard 传入 on_critical 应注册 critical 回调。"""
        fired: list[str] = []

        def _on_crit(info: dict[str, Any]) -> None:
            fired.append("critical")

        with memory_guard(warning_pct=70, critical_pct=85, on_critical=_on_crit) as watcher:
            # 手动抬升水位触发（绕过真实内存）
            watcher._maybe_trigger_callbacks(MemoryLevel.CRITICAL, {"percent": 90.0})
        assert fired == ["critical"]
