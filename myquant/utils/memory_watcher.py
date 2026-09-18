# 文件名：myquant/utils/memory_watcher.py
"""
鲲鹏量化交易系统 - 内存水位监控器（MemoryWatcher）
====================================================
V5回测性能优化新增：OOM防护核心组件

功能:
    1. 实时监控进程内存占用（RSS）和系统内存水位
    2. 三级水位告警：NORMAL / WARNING / CRITICAL
    3. 阈值可配置，支持回调注册（水位变化时自动触发）
    4. 线程安全，可在多线程/多进程环境下使用
    5. 统计信息：峰值内存、告警次数、回调触发次数
    6. 上下文管理器支持，自动清理

设计原则:
    - 低开销：单次检查 < 1ms，不引入显著性能损耗
    - 防御式：psutil 不可用时降级为 tracemalloc，再降级为禁用
    - 无侵入：不修改被监控代码，仅提供查询和回调机制

典型用法:
    watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
    if watcher.is_critical():
        # 自动降级：减少预取、切换分块加载
        loader.prefetch_count = 0

    with MemoryWatcher(warning_pct=75) as w:
        w.register_callback(MemoryLevel.CRITICAL, lambda level, info: print(f"OOM风险: {info['percent']}%"))
        run_backtest()
"""

from __future__ import annotations

import logging
import threading
import time
import tracemalloc
from collections.abc import Callable
from contextlib import contextmanager
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ============================================================================
# 常量
# ============================================================================

DEFAULT_WARNING_PCT: float = 70.0
"""默认告警阈值（系统内存占用百分比）"""

DEFAULT_CRITICAL_PCT: float = 85.0
"""默认严重阈值（系统内存占用百分比）"""

CHECK_INTERVAL_SECONDS: float = 0.5
"""最小检查间隔（秒），避免频繁调用 psutil 引入开销"""

MB_TO_BYTES: int = 1024 * 1024
"""MB 到字节的转换系数"""


# ============================================================================
# 内存水位枚举
# ============================================================================

class MemoryLevel(Enum):
    """内存水位级别"""

    NORMAL = "normal"
    """正常：内存占用低于告警阈值"""

    WARNING = "warning"
    """告警：内存占用超过告警阈值，建议减少预取/分块加载"""

    CRITICAL = "critical"
    """严重：内存占用超过严重阈值，必须停止预取并强制分块加载"""

    @property
    def severity(self) -> int:
        """水位严重程度（数值越大越严重），用于水位升降判断。

        Enum 的 .value 是字符串（"normal"/"warning"/"critical"），字典序与
        严重程度顺序不一致（"normal" > "critical"），不能直接用字符串比较。
        """
        return _MEMORY_LEVEL_SEVERITY[self.value]


# 水位严重程度映射（normal < warning < critical）
_MEMORY_LEVEL_SEVERITY: dict[str, int] = {
    MemoryLevel.NORMAL.value: 1,
    MemoryLevel.WARNING.value: 2,
    MemoryLevel.CRITICAL.value: 3,
}


# ============================================================================
# 内存监控器
# ============================================================================

class MemoryWatcher:
    """
    内存水位监控器

    监控当前进程的 RSS 内存占用和系统整体内存水位，提供三级告警机制。
    线程安全，支持回调注册和上下文管理器。

    Attributes:
        warning_pct: 告警阈值（系统内存百分比）。
        critical_pct: 严重阈值（系统内存百分比）。
        enabled: 是否启用监控。

    Example:
        >>> watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
        >>> info = watcher.get_memory_info()
        >>> print(info["level"])
        normal
        >>> if watcher.is_critical():
        ...     print("OOM风险，自动降级")
    """

    def __init__(
        self,
        warning_pct: float = DEFAULT_WARNING_PCT,
        critical_pct: float = DEFAULT_CRITICAL_PCT,
        enabled: bool = True,
    ):
        """
        初始化内存监控器

        Args:
            warning_pct: 告警阈值（系统内存百分比），默认 70.0。
            critical_pct: 严重阈值（系统内存百分比），默认 85.0。
            enabled: 是否启用监控，默认 True。禁用时所有检查返回 NORMAL。

        Raises:
            ValueError: 阈值不在 (0, 100] 范围内，或 warning_pct >= critical_pct。
        """
        if not (0 < warning_pct <= 100):
            raise ValueError(f"warning_pct 必须在 (0, 100] 范围内: {warning_pct}")
        if not (0 < critical_pct <= 100):
            raise ValueError(f"critical_pct 必须在 (0, 100] 范围内: {critical_pct}")
        if warning_pct >= critical_pct:
            raise ValueError(
                f"warning_pct ({warning_pct}) 必须小于 critical_pct ({critical_pct})"
            )

        self.warning_pct: float = float(warning_pct)
        self.critical_pct: float = float(critical_pct)
        self.enabled: bool = enabled

        # 线程锁
        self._lock = threading.Lock()

        # 缓存上次检查结果，避免频繁调用 psutil
        self._cached_info: dict[str, Any] | None = None
        self._last_check_time: float = 0.0

        # 回调注册表: {callback_id: (level, callback)}
        self._callbacks: dict[str, tuple[MemoryLevel, Callable[[MemoryLevel, dict[str, Any]], None]]] = {}
        self._callback_counter: int = 0

        # 上次触发回调的水位（避免重复触发）
        self._last_callback_level: MemoryLevel | None = None

        # 统计信息
        self._stats: dict[str, Any] = {
            "check_count": 0,
            "warning_count": 0,
            "critical_count": 0,
            "callback_count": 0,
            "peak_rss_mb": 0.0,
            "peak_percent": 0.0,
            "start_time": time.time(),
        }

        # psutil 可用性检测
        self._psutil_available: bool = False
        self._process = None
        try:
            import psutil  # type: ignore
            self._psutil_available = True
            self._process = psutil.Process()
            logger.debug("MemoryWatcher: psutil 可用，使用系统级内存监控")
        except ImportError:
            logger.warning("MemoryWatcher: psutil 不可用，降级为 tracemalloc")
            try:
                if not tracemalloc.is_tracing():
                    tracemalloc.start()
            except Exception as e:
                logger.warning("MemoryWatcher: tracemalloc 启动失败: %s，监控将被禁用", e)
                self.enabled = False

    # ------------------------------------------------------------------
    # 核心检查方法
    # ------------------------------------------------------------------

    def get_memory_info(self, force_refresh: bool = False) -> dict[str, Any]:
        """
        获取当前内存信息

        Args:
            force_refresh: 是否强制刷新（跳过缓存）。默认 False，使用缓存减少 psutil 调用。

        Returns:
            包含以下字段的字典:
                - rss_mb: 进程 RSS 内存（MB）
                - vms_mb: 进程 VMS 虚拟内存（MB）
                - percent: 系统内存占用百分比
                - system_total_mb: 系统总内存（MB）
                - system_available_mb: 系统可用内存（MB）
                - level: MemoryLevel 枚举值
                - level_name: 水位名称字符串
                - timestamp: 检查时间戳
                - source: 数据来源（"psutil" / "tracemalloc" / "disabled"）
        """
        if not self.enabled:
            return self._disabled_info()

        now = time.time()
        # 使用缓存（避免频繁调用 psutil）
        if (
            not force_refresh
            and self._cached_info is not None
            and (now - self._last_check_time) < CHECK_INTERVAL_SECONDS
        ):
            return self._cached_info.copy()

        info = self._collect_memory_info()
        info["level"] = self._determine_level(info["percent"])
        info["level_name"] = info["level"].value
        info["timestamp"] = now
        info["source"] = "psutil" if self._psutil_available else "tracemalloc"

        # 更新缓存和统计
        with self._lock:
            self._cached_info = info
            self._last_check_time = now
            self._stats["check_count"] += 1
            self._stats["peak_rss_mb"] = max(self._stats["peak_rss_mb"], info["rss_mb"])
            self._stats["peak_percent"] = max(self._stats["peak_percent"], info["percent"])
            if info["level"] == MemoryLevel.WARNING:
                self._stats["warning_count"] += 1
            elif info["level"] == MemoryLevel.CRITICAL:
                self._stats["critical_count"] += 1

        # 检查是否需要触发回调
        self._maybe_trigger_callbacks(info["level"], info)

        return info.copy()

    def check(self) -> MemoryLevel:
        """
        检查当前内存水位

        Returns:
            MemoryLevel 枚举值（NORMAL / WARNING / CRITICAL）。
        """
        info = self.get_memory_info()
        return info["level"]

    def is_normal(self) -> bool:
        """内存是否正常（低于告警阈值）"""
        return self.check() == MemoryLevel.NORMAL

    def is_warning(self) -> bool:
        """内存是否告警（超过告警阈值但低于严重阈值）"""
        return self.check() == MemoryLevel.WARNING

    def is_critical(self) -> bool:
        """内存是否严重（超过严重阈值）"""
        return self.check() == MemoryLevel.CRITICAL

    def is_above_warning(self) -> bool:
        """内存是否超过告警阈值（WARNING 或 CRITICAL）"""
        level = self.check()
        return level in (MemoryLevel.WARNING, MemoryLevel.CRITICAL)

    # ------------------------------------------------------------------
    # 回调机制
    # ------------------------------------------------------------------

    def register_callback(
        self,
        level: MemoryLevel,
        callback: Callable[[MemoryLevel, dict[str, Any]], None],
    ) -> str:
        """
        注册内存水位回调

        当内存水位达到或超过指定级别时，回调函数会被调用。
        同一水位变化只触发一次（从低水位升到高水位时触发，持续高水位不重复触发）。

        Args:
            level: 触发回调的水位级别。
            callback: 回调函数，签名为 callback(level, memory_info) -> None。

        Returns:
            回调 ID，可用于 unregister_callback 注销。

        Raises:
            TypeError: callback 不是可调用对象。
        """
        if not callable(callback):
            raise TypeError(f"callback 必须是可调用对象: {type(callback)}")

        with self._lock:
            self._callback_counter += 1
            callback_id = f"cb_{self._callback_counter}"
            self._callbacks[callback_id] = (level, callback)

        logger.debug("注册内存回调: id=%s, level=%s", callback_id, level.value)
        return callback_id

    def unregister_callback(self, callback_id: str) -> bool:
        """
        注销内存水位回调

        Args:
            callback_id: register_callback 返回的回调 ID。

        Returns:
            True=注销成功，False=回调 ID 不存在。
        """
        with self._lock:
            if callback_id in self._callbacks:
                del self._callbacks[callback_id]
                logger.debug("注销内存回调: id=%s", callback_id)
                return True
        return False

    def clear_callbacks(self) -> None:
        """清除所有已注册的回调"""
        with self._lock:
            self._callbacks.clear()
            self._last_callback_level = None
        logger.debug("已清除所有内存回调")

    def _maybe_trigger_callbacks(self, current_level: MemoryLevel, info: dict[str, Any]) -> None:
        """检查是否需要触发回调（水位上升时触发，持续同一水位不重复触发）"""
        with self._lock:
            # 水位没有变化，不触发
            if current_level == self._last_callback_level:
                return

            # 水位下降，更新记录但不触发回调
            if self._last_callback_level is not None and current_level.severity < self._last_callback_level.severity:
                self._last_callback_level = current_level
                return

            # 水位上升，触发匹配的回调
            self._last_callback_level = current_level
            callbacks_to_trigger: list[tuple[MemoryLevel, Callable]] = []
            for _, (registered_level, cb) in self._callbacks.items():
                # 触发条件：当前水位 >= 注册水位（按严重程度比较）
                if current_level.severity >= registered_level.severity:
                    callbacks_to_trigger.append((registered_level, cb))

        # 在锁外执行回调，避免回调中调用 watcher 方法导致死锁
        for registered_level, cb in callbacks_to_trigger:
            try:
                cb(current_level, info)
                with self._lock:
                    self._stats["callback_count"] += 1
            except Exception as e:
                logger.error("内存回调执行失败: level=%s, error=%s", registered_level.value, e)

    # ------------------------------------------------------------------
    # 统计信息
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """
        获取内存监控统计信息

        Returns:
            包含以下字段的字典:
                - check_count: 总检查次数
                - warning_count: 告警次数
                - critical_count: 严重次数
                - callback_count: 回调触发次数
                - peak_rss_mb: 峰值 RSS 内存（MB）
                - peak_percent: 峰值系统内存百分比
                - uptime_seconds: 运行时长（秒）
                - enabled: 是否启用
                - psutil_available: psutil 是否可用
        """
        with self._lock:
            stats = self._stats.copy()
            stats["uptime_seconds"] = time.time() - stats["start_time"]
            stats["enabled"] = self.enabled
            stats["psutil_available"] = self._psutil_available
            stats["warning_pct"] = self.warning_pct
            stats["critical_pct"] = self.critical_pct
        return stats

    def reset_stats(self) -> None:
        """重置统计信息"""
        with self._lock:
            self._stats = {
                "check_count": 0,
                "warning_count": 0,
                "critical_count": 0,
                "callback_count": 0,
                "peak_rss_mb": 0.0,
                "peak_percent": 0.0,
                "start_time": time.time(),
            }
            self._last_callback_level = None
        logger.debug("内存监控统计已重置")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _collect_memory_info(self) -> dict[str, Any]:
        """收集内存信息（psutil 优先，tracemalloc 降级）"""
        if self._psutil_available and self._process is not None:
            return self._collect_via_psutil()
        return self._collect_via_tracemalloc()

    def _collect_via_psutil(self) -> dict[str, Any]:
        """通过 psutil 收集内存信息"""
        import psutil  # type: ignore

        try:
            mem_info = self._process.memory_info()
            sys_mem = psutil.virtual_memory()

            return {
                "rss_mb": round(mem_info.rss / MB_TO_BYTES, 2),
                "vms_mb": round(mem_info.vms / MB_TO_BYTES, 2),
                "percent": round(sys_mem.percent, 2),
                "system_total_mb": round(sys_mem.total / MB_TO_BYTES, 2),
                "system_available_mb": round(sys_mem.available / MB_TO_BYTES, 2),
            }
        except Exception as e:
            logger.warning("psutil 内存采集失败: %s，降级为 tracemalloc", e)
            return self._collect_via_tracemalloc()

    def _collect_via_tracemalloc(self) -> dict[str, Any]:
        """通过 tracemalloc 收集内存信息（降级方案，仅进程级）"""
        try:
            if tracemalloc.is_tracing():
                current, peak = tracemalloc.get_traced_memory()
                rss_mb = round(current / MB_TO_BYTES, 2)
            else:
                rss_mb = 0.0
        except Exception:
            rss_mb = 0.0

        # tracemalloc 无法获取系统级内存百分比，用进程 RSS 估算
        # 假设系统总内存 16GB（保守估计），实际百分比可能不准确
        estimated_total_mb = 16384.0
        percent = round(min(rss_mb / estimated_total_mb * 100, 100.0), 2)

        return {
            "rss_mb": rss_mb,
            "vms_mb": 0.0,
            "percent": percent,
            "system_total_mb": estimated_total_mb,
            "system_available_mb": max(estimated_total_mb - rss_mb, 0),
        }

    def _determine_level(self, percent: float) -> MemoryLevel:
        """根据内存百分比确定水位级别"""
        if percent >= self.critical_pct:
            return MemoryLevel.CRITICAL
        if percent >= self.warning_pct:
            return MemoryLevel.WARNING
        return MemoryLevel.NORMAL

    def _disabled_info(self) -> dict[str, Any]:
        """监控禁用时返回的信息"""
        return {
            "rss_mb": 0.0,
            "vms_mb": 0.0,
            "percent": 0.0,
            "system_total_mb": 0.0,
            "system_available_mb": 0.0,
            "level": MemoryLevel.NORMAL,
            "level_name": "normal",
            "timestamp": time.time(),
            "source": "disabled",
        }

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    def __enter__(self) -> MemoryWatcher:
        """进入上下文管理器"""
        return self

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> None:
        """退出上下文管理器，清理资源"""
        self.clear_callbacks()
        # 不停止 tracemalloc（可能被其他模块使用）
        logger.debug("MemoryWatcher 上下文退出")

    def __repr__(self) -> str:
        return (
            f"MemoryWatcher(warning={self.warning_pct}%, "
            f"critical={self.critical_pct}%, enabled={self.enabled}, "
            f"psutil={self._psutil_available})"
        )


# ============================================================================
# 全局单例
# ============================================================================

_global_watcher: MemoryWatcher | None = None
_global_watcher_lock = threading.Lock()


def get_memory_watcher(
    warning_pct: float = DEFAULT_WARNING_PCT,
    critical_pct: float = DEFAULT_CRITICAL_PCT,
) -> MemoryWatcher:
    """
    获取全局内存监控器单例

    Args:
        warning_pct: 告警阈值，仅在首次创建时生效。
        critical_pct: 严重阈值，仅在首次创建时生效。

    Returns:
        全局 MemoryWatcher 单例。
    """
    global _global_watcher
    with _global_watcher_lock:
        if _global_watcher is None:
            _global_watcher = MemoryWatcher(warning_pct, critical_pct)
        return _global_watcher


def reset_memory_watcher() -> None:
    """重置全局内存监控器单例（测试用）"""
    global _global_watcher
    with _global_watcher_lock:
        if _global_watcher is not None:
            _global_watcher.clear_callbacks()
        _global_watcher = None


# ============================================================================
# 上下文管理器装饰器
# ============================================================================

@contextmanager
def memory_guard(
    warning_pct: float = DEFAULT_WARNING_PCT,
    critical_pct: float = DEFAULT_CRITICAL_PCT,
    on_critical: Callable[[dict[str, Any]], None] | None = None,
):
    """
    内存防护上下文管理器

    在代码块执行期间监控内存水位，严重时自动触发回调。

    Args:
        warning_pct: 告警阈值。
        critical_pct: 严重阈值。
        on_critical: 严重水位回调，签名为 on_critical(memory_info) -> None。

    Yields:
        MemoryWatcher 实例。

    Example:
        >>> with memory_guard(critical_pct=80) as watcher:
        ...     run_large_backtest()
        ...     if watcher.is_critical():
        ...         print("内存超限，结果可能不完整")
    """
    watcher = MemoryWatcher(warning_pct, critical_pct)

    if on_critical is not None:
        def _critical_callback(level: MemoryLevel, info: dict[str, Any]) -> None:
            on_critical(info)
        watcher.register_callback(MemoryLevel.CRITICAL, _critical_callback)

    try:
        yield watcher
    finally:
        watcher.clear_callbacks()


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  memory_watcher.py — 内存水位监控器 自测 Demo")
    print("=" * 60)

    # ── 测试 1: 基本功能 ──
    print("\n[1] 基本功能测试")
    watcher = MemoryWatcher(warning_pct=70, critical_pct=85)
    info = watcher.get_memory_info(force_refresh=True)
    print(f"    RSS内存: {info['rss_mb']:.2f} MB")
    print(f"    系统内存: {info['percent']:.2f}%")
    print(f"    水位: {info['level_name']}")
    print(f"    数据来源: {info['source']}")
    assert "rss_mb" in info
    assert "percent" in info
    assert "level" in info
    print("    ✓ 基本功能正常")

    # ── 测试 2: 水位判断 ──
    print("\n[2] 水位判断测试")
    level = watcher.check()
    print(f"    当前水位: {level.value}")
    print(f"    is_normal: {watcher.is_normal()}")
    print(f"    is_warning: {watcher.is_warning()}")
    print(f"    is_critical: {watcher.is_critical()}")
    print(f"    is_above_warning: {watcher.is_above_warning()}")
    # 三个互斥判断中应有一个为 True
    assert watcher.is_normal() or watcher.is_warning() or watcher.is_critical()
    print("    ✓ 水位判断正常")

    # ── 测试 3: 阈值校验 ──
    print("\n[3] 阈值校验测试")
    try:
        MemoryWatcher(warning_pct=0)
        assert False, "应抛出 ValueError"
    except ValueError:
        print("    ✓ warning_pct=0 正确抛出 ValueError")

    try:
        MemoryWatcher(warning_pct=90, critical_pct=80)
        assert False, "应抛出 ValueError"
    except ValueError:
        print("    ✓ warning_pct >= critical_pct 正确抛出 ValueError")

    try:
        MemoryWatcher(critical_pct=101)
        assert False, "应抛出 ValueError"
    except ValueError:
        print("    ✓ critical_pct=101 正确抛出 ValueError")

    # ── 测试 4: 回调机制 ──
    print("\n[4] 回调机制测试")
    callback_results: list[str] = []

    def on_warning(level: MemoryLevel, info: dict[str, Any]) -> None:
        callback_results.append(f"warning:{info['percent']}")

    def on_critical(level: MemoryLevel, info: dict[str, Any]) -> None:
        callback_results.append(f"critical:{info['percent']}")

    cb_id_1 = watcher.register_callback(MemoryLevel.WARNING, on_warning)
    cb_id_2 = watcher.register_callback(MemoryLevel.CRITICAL, on_critical)
    print(f"    注册回调: {cb_id_1}, {cb_id_2}")
    assert cb_id_1.startswith("cb_")
    assert cb_id_2.startswith("cb_")

    # 注销回调
    result = watcher.unregister_callback(cb_id_1)
    assert result is True
    print("    ✓ 回调注销正常")

    # 注销不存在的回调
    result = watcher.unregister_callback("nonexistent")
    assert result is False
    print("    ✓ 不存在回调注销返回 False")

    # 回调类型校验
    try:
        watcher.register_callback(MemoryLevel.WARNING, "not_callable")  # type: ignore
        assert False, "应抛出 TypeError"
    except TypeError:
        print("    ✓ 非可调用对象正确抛出 TypeError")

    watcher.clear_callbacks()

    # ── 测试 5: 统计信息 ──
    print("\n[5] 统计信息测试")
    watcher2 = MemoryWatcher()
    for _ in range(5):
        watcher2.get_memory_info(force_refresh=True)
    stats = watcher2.get_stats()
    print(f"    检查次数: {stats['check_count']}")
    print(f"    峰值RSS: {stats['peak_rss_mb']:.2f} MB")
    print(f"    运行时长: {stats['uptime_seconds']:.2f}s")
    assert stats["check_count"] >= 5
    assert stats["peak_rss_mb"] > 0
    print("    ✓ 统计信息正常")

    # 重置统计
    watcher2.reset_stats()
    stats2 = watcher2.get_stats()
    assert stats2["check_count"] == 0
    print("    ✓ 统计重置正常")

    # ── 测试 6: 禁用模式 ──
    print("\n[6] 禁用模式测试")
    disabled_watcher = MemoryWatcher(enabled=False)
    info_disabled = disabled_watcher.get_memory_info()
    assert info_disabled["level"] == MemoryLevel.NORMAL
    assert info_disabled["source"] == "disabled"
    assert disabled_watcher.is_normal() is True
    print("    ✓ 禁用模式返回 NORMAL")

    # ── 测试 7: 上下文管理器 ──
    print("\n[7] 上下文管理器测试")
    with MemoryWatcher(warning_pct=50, critical_pct=90) as w:
        assert isinstance(w, MemoryWatcher)
        info_ctx = w.get_memory_info(force_refresh=True)
        print(f"    上下文中水位: {info_ctx['level_name']}")
    print("    ✓ 上下文管理器正常")

    # ── 测试 8: memory_guard 装饰器 ──
    print("\n[8] memory_guard 上下文管理器测试")
    critical_triggered = []

    def on_crit(info: dict[str, Any]) -> None:
        critical_triggered.append(info["percent"])

    with memory_guard(warning_pct=1, critical_pct=2, on_critical=on_crit) as guard:
        # 阈值设得极低，触发告警
        guard_info = guard.get_memory_info(force_refresh=True)
        print(f"    guard水位: {guard_info['level_name']} ({guard_info['percent']}%)")
    print("    ✓ memory_guard 正常")

    # ── 测试 9: 全局单例 ──
    print("\n[9] 全局单例测试")
    reset_memory_watcher()
    w1 = get_memory_watcher()
    w2 = get_memory_watcher()
    assert w1 is w2
    print("    ✓ 全局单例正常")
    reset_memory_watcher()

    # ── 测试 10: 缓存机制 ──
    print("\n[10] 缓存机制测试")
    watcher3 = MemoryWatcher()
    info1 = watcher3.get_memory_info()
    info2 = watcher3.get_memory_info()  # 应使用缓存
    assert info1["timestamp"] == info2["timestamp"]
    print("    ✓ 缓存机制正常（连续两次调用时间戳相同）")

    # 强制刷新
    import time as _time
    _time.sleep(0.6)  # 超过缓存间隔
    info3 = watcher3.get_memory_info(force_refresh=True)
    assert info3["timestamp"] > info1["timestamp"]
    print("    ✓ 强制刷新正常")

    print("\n" + "=" * 60)
    print("  ✓ memory_watcher.py 全部自测通过")
    print("=" * 60)
