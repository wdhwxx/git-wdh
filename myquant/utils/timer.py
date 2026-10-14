# 文件名：myquant/utils/timer.py
"""
timer.py - 分阶段计时器
=========================
功能:
  1. 分阶段计时: 记录多个命名阶段的累计耗时、执行次数、平均/最小/最大单次耗时
  2. 三种用法: 阶段上下文(phase) / 手动(start/stop) / 分段(split)
  3. 耗时报告: 输出各阶段耗时、耗时占比、汇总，用于回测/参数优化等性能定位
  4. 可禁用: enabled=False 时全部操作 O(1) 空转，生产环境零开销
  5. 线程安全: 内部 RLock，支持多线程/多进程并行记录同一计时器
  6. 模块级注册表: get_stage_timer 跨模块共享同一计时器

依赖: time, threading, math, dataclasses, contextlib, logging, typing
"""

from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

DEFAULT_DECIMAL: int = 3
"""时长文本默认小数位数"""
NANOSECONDS_PER_SECOND: float = 1e9
MICROSECONDS_PER_SECOND: float = 1e6
MILLISECONDS_PER_SECOND: float = 1e3
SECONDS_PER_MINUTE: float = 60.0


# ─────────────────────────────────────────────
# 时长格式化
# ─────────────────────────────────────────────

def format_duration(seconds: float, decimal: int = DEFAULT_DECIMAL) -> str:
    """将秒数格式化为人类可读时长（自动选择 ns/µs/ms/s/min）。

    Args:
        seconds: 时长（秒），必须 >= 0。
        decimal: 小数位数。

    Returns:
        格式化后的时长字符串，如 "12.500 ms"。

    Raises:
        ValueError: seconds 为负数或 decimal 非法时抛出。
    """
    if seconds < 0:
        raise ValueError(f"seconds 必须 >= 0，收到: {seconds}")
    if decimal < 0:
        raise ValueError(f"decimal 必须 >= 0，收到: {decimal}")
    if seconds == 0.0:
        return f"0.{''.join('0' for _ in range(decimal))} s"
    if seconds < 1e-6:
        return f"{seconds * NANOSECONDS_PER_SECOND:.{decimal}f} ns"
    if seconds < 1e-3:
        return f"{seconds * MICROSECONDS_PER_SECOND:.{decimal}f} µs"
    if seconds < 1.0:
        return f"{seconds * MILLISECONDS_PER_SECOND:.{decimal}f} ms"
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds:.{decimal}f} s"
    return f"{seconds / SECONDS_PER_MINUTE:.{decimal}f} min"


# ─────────────────────────────────────────────
# 阶段统计
# ─────────────────────────────────────────────

@dataclass
class _StageStats:
    """单个阶段的计时统计（内部数据结构）。"""

    count: int = 0                        # 进入/结算次数
    total: float = 0.0                    # 累计耗时（秒）
    last: float = 0.0                     # 最近一次耗时（秒）
    minimum: float = math.inf             # 最小单次耗时（秒）
    maximum: float = 0.0                  # 最大单次耗时（秒）
    start_ts: Optional[float] = None      # 正在计时的开始时刻；None=未在计时


# ─────────────────────────────────────────────
# 分阶段计时器
# ─────────────────────────────────────────────

class StageTimer:
    """分阶段计时器：记录命名阶段的耗时并输出性能定位报告。

    Usage:
        >>> timer = StageTimer("回测")
        >>> with timer.phase("数据加载"):
        ...     load_data()
        >>> with timer.phase("因子计算"):
        ...     compute_factors()
        >>> print(timer.report())

    Attributes:
        name: 计时器名称（用于报告标题）。
        enabled: 是否启用计时；False 时所有操作空转（O(1)）。
    """

    def __init__(
        self,
        name: str = "timer",
        enabled: bool = True,
        logger: Optional[logging.Logger] = None,
        decimal: int = DEFAULT_DECIMAL,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ValueError(f"name 必须为非空字符串，收到: {name!r}")
        if decimal < 0:
            raise ValueError(f"decimal 必须 >= 0，收到: {decimal}")
        self.name: str = name
        self.enabled: bool = bool(enabled)
        self.decimal: int = decimal
        self._logger: logging.Logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._stats: Dict[str, _StageStats] = {}
        self._splits: List[Tuple[str, float]] = []
        self._split_mark: Optional[float] = None  # 最近一个计时结束时刻（split 起点）

    # ── 阶段上下文（推荐用法） ──

    @contextmanager
    def phase(self, stage: str) -> Iterator[None]:
        """阶段计时上下文管理器。

        进入时开始计时，退出时结算到对应阶段统计；同一阶段可多次进入（累加）。
        支持嵌套：内层阶段结算后，外层阶段继续计时，互不干扰。

        Args:
            stage: 阶段名称（非空字符串）。

        Raises:
            ValueError: stage 非法。
            RuntimeError: 该阶段已在计时中（重入）时抛出。
        """
        self._validate_stage_name(stage)
        if not self.enabled:
            yield
            return
        with self._lock:
            stats = self._stats.setdefault(stage, _StageStats())
            if stats.start_ts is not None:
                raise RuntimeError(f"阶段 {stage!r} 已在计时中（不支持重入）")
            stats.start_ts = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                stats = self._stats.get(stage)
                if stats is None or stats.start_ts is None:
                    return  # 已被手动 stop() 结算，避免重复累计
                dt = time.perf_counter() - stats.start_ts
                stats.start_ts = None
                self._record(stats, dt)
                self._split_mark = time.perf_counter()

    # ── 手动计时 ──

    def start(self, stage: str) -> "StageTimer":
        """手动开始指定阶段的计时。

        Args:
            stage: 阶段名称（非空字符串）。

        Returns:
            self（支持链式调用）。

        Raises:
            ValueError: stage 非法。
            RuntimeError: 该阶段已在计时中时抛出。
        """
        self._validate_stage_name(stage)
        if not self.enabled:
            return self
        with self._lock:
            stats = self._stats.setdefault(stage, _StageStats())
            if stats.start_ts is not None:
                raise RuntimeError(f"阶段 {stage!r} 已在计时中")
            stats.start_ts = time.perf_counter()
        return self

    def stop(self, stage: Optional[str] = None) -> float:
        """手动结束计时并返回该阶段本次耗时（秒）。

        Args:
            stage: 阶段名称；None 时自动定位唯一正在计时的阶段
                （多个阶段同时计时时必须显式指定）。

        Returns:
            本次耗时（秒）。

        Raises:
            RuntimeError: 无正在计时的阶段、多个阶段同时计时但未指定、
                指定阶段未在计时时抛出。
        """
        if not self.enabled:
            return 0.0
        with self._lock:
            if stage is None:
                running = [s for s, st in self._stats.items() if st.start_ts is not None]
                if len(running) == 0:
                    raise RuntimeError("没有正在计时的阶段，无法自动 stop")
                if len(running) > 1:
                    raise RuntimeError(f"多个阶段正在计时 {running}，请显式指定 stage")
                stage = running[0]
            stats = self._stats.get(stage)
            if stats is None or stats.start_ts is None:
                raise RuntimeError(f"阶段 {stage!r} 未在计时")
            dt = time.perf_counter() - stats.start_ts
            stats.start_ts = None
            self._record(stats, dt)
            self._split_mark = time.perf_counter()
            return dt

    # ── 分段（两个计时点之间的片段） ──

    def split(self, name: str) -> float:
        """记录自上一个计时点（phase 结束 / stop / 上次 split）到现在的耗时。

        用于统计不在 phase 块内的片段（如循环间开销、序列化间隙）。

        Args:
            name: 分段名称（非空字符串）。

        Returns:
            该分段耗时（秒）。

        Raises:
            ValueError: name 非法。
            RuntimeError: 尚无任何计时点作为起始点时抛出。
        """
        self._validate_stage_name(name)
        if not self.enabled:
            return 0.0
        now = time.perf_counter()
        with self._lock:
            if self._split_mark is None:
                raise RuntimeError("split 需要先有 phase/start 作为起始计时点")
            dt = now - self._split_mark
            self._splits.append((name, dt))
            self._split_mark = now
            return dt

    # ── 查询 ──

    def elapsed(self, stage: str) -> float:
        """返回指定阶段已结算的累计耗时（秒）；未登记阶段返回 0.0。"""
        with self._lock:
            stats = self._stats.get(stage)
            return stats.total if stats is not None else 0.0

    def count(self, stage: str) -> int:
        """返回指定阶段的执行（结算）次数；未登记阶段返回 0。"""
        with self._lock:
            stats = self._stats.get(stage)
            return stats.count if stats is not None else 0

    def has_stage(self, stage: str) -> bool:
        """判断指定阶段是否已被登记。"""
        with self._lock:
            return stage in self._stats

    def stage_names(self) -> List[str]:
        """返回已登记的全部阶段名。"""
        with self._lock:
            return list(self._stats.keys())

    def total(self) -> float:
        """返回所有阶段已结算累计耗时之和（不含正在计时的阶段）。"""
        with self._lock:
            return sum(st.total for st in self._stats.values())

    def is_running(self, stage: str) -> bool:
        """判断指定阶段是否正在计时中。"""
        with self._lock:
            stats = self._stats.get(stage)
            return stats is not None and stats.start_ts is not None

    # ── 报告 ──

    def to_dict(self) -> Dict[str, Any]:
        """将计时结果导出为结构化字典（供持久化/序列化/测试断言）。"""
        with self._lock:
            stages: Dict[str, Dict[str, Any]] = {}
            for name, st in self._stats.items():
                stages[name] = {
                    "count": st.count,
                    "total_seconds": st.total,
                    "last_seconds": st.last,
                    "min_seconds": None if st.minimum == math.inf else st.minimum,
                    "max_seconds": st.maximum,
                    "running": st.start_ts is not None,
                }
            return {
                "name": self.name,
                "enabled": self.enabled,
                "total_seconds": sum(st.total for st in self._stats.values()),
                "stages": stages,
                "splits": [(n, dt) for n, dt in self._splits],
            }

    def report(self) -> str:
        """生成阶段耗时报告文本，并通过内部 logger 输出（info 级）。

        Returns:
            报告文本（含表头、各阶段按耗时降序排列、占比与汇总、分段）。
        """
        text = self._build_report_text()
        self._logger.info("阶段计时报告 [%s]:\n%s", self.name, text)
        return text

    def summary(self) -> str:
        """生成单行摘要文本（便于打印在进度条/命令行）。"""
        with self._lock:
            top = sorted(self._stats.items(), key=lambda kv: kv[1].total, reverse=True)
            parts = [
                f"{name}={format_duration(st.total, self.decimal)}"
                for name, st in top[:3]
            ]
            total = format_duration(self.total(), self.decimal)
            return f"[{self.name}] total={total} | " + " | ".join(parts)

    # ── 管理 ──

    def clear(self, stage: Optional[str] = None) -> "StageTimer":
        """清空指定阶段（或全部）的统计。

        Args:
            stage: 阶段名；None 时清空全部阶段与分段。

        Returns:
            self。
        """
        with self._lock:
            if stage is None:
                self._stats.clear()
                self._splits.clear()
                self._split_mark = None
            else:
                self._stats.pop(stage, None)
        return self

    def reset(self) -> "StageTimer":
        """清空全部统计并复位计时点（等价于 clear()）。"""
        return self.clear()

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"StageTimer(name={self.name!r}, enabled={self.enabled}, "
                f"stages={len(self._stats)}, total={format_duration(self.total(), self.decimal)})"
            )

    # ── 内部 ──

    @staticmethod
    def _validate_stage_name(stage: str) -> None:
        if not stage or not isinstance(stage, str):
            raise ValueError(f"阶段名称必须为非空字符串，收到: {stage!r}")

    @staticmethod
    def _record(stats: _StageStats, dt: float) -> None:
        stats.count += 1
        stats.total += dt
        stats.last = dt
        stats.minimum = min(stats.minimum, dt)
        stats.maximum = max(stats.maximum, dt)

    def _build_report_text(self) -> str:
        with self._lock:
            total = sum(st.total for st in self._stats.values())
            lines: List[str] = []
            lines.append("阶段耗时报告")
            lines.append("=" * 72)
            lines.append(
                f"{'阶段':<24} {'次数':>5} {'累计耗时':>14} {'占比':>7} "
                f"{'平均':>12} {'最小':>12} {'最大':>12}"
            )
            lines.append("-" * 72)
            ordered = sorted(
                self._stats.items(), key=lambda kv: kv[1].total, reverse=True
            )
            for name, st in ordered:
                pct = (st.total / total * 100.0) if total > 0 else 0.0
                avg = st.total / st.count if st.count > 0 else 0.0
                mn = format_duration(st.minimum, self.decimal) if st.minimum != math.inf else "-"
                lines.append(
                    f"{name:<24} {st.count:>5} {format_duration(st.total, self.decimal):>14} "
                    f"{pct:>6.1f}% {format_duration(avg, self.decimal):>12} "
                    f"{mn:>12} {format_duration(st.maximum, self.decimal):>12}"
                )
            lines.append("-" * 72)
            lines.append(f"{'合计':<24} {'':>5} {format_duration(total, self.decimal):>14} {'100.0%':>7}")
            if self._splits:
                lines.append("")
                lines.append("分段记录（计时点之间）:")
                for name, dt in self._splits:
                    lines.append(f"  - {name}: {format_duration(dt, self.decimal)}")
            if total == 0:
                lines.append("（暂无已结算的计时数据）")
            return "\n".join(lines)


# ─────────────────────────────────────────────
# 模块级注册表（跨模块共享计时器）
# ─────────────────────────────────────────────

_timers: Dict[str, StageTimer] = {}
_timers_lock = threading.Lock()


def get_stage_timer(
    name: str = "default",
    enabled: bool = True,
    logger: Optional[logging.Logger] = None,
    decimal: int = DEFAULT_DECIMAL,
) -> StageTimer:
    """获取（或创建并缓存）同名计时器，跨模块共享同一实例。

    Args:
        name: 计时器名称；同名调用返回同一实例。
        enabled: 首次创建时的启用开关（后续调用忽略）。
        logger: 首次创建时的 logger（后续调用忽略）。
        decimal: 首次创建时的时长小数位（后续调用忽略）。

    Returns:
        缓存的 StageTimer 实例。
    """
    with _timers_lock:
        timer = _timers.get(name)
        if timer is None:
            timer = StageTimer(name=name, enabled=enabled, logger=logger, decimal=decimal)
            _timers[name] = timer
        return timer


def reset_stage_timers() -> None:
    """清空模块级计时器注册表（测试/进程重建时使用）。"""
    with _timers_lock:
        _timers.clear()


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import time as _time

    print("=" * 60)
    print("  timer.py — 分阶段计时器 自测 Demo")
    print("=" * 60)

    ok_count = [0]
    total_count = [0]

    def _ok(cond: bool, label: str) -> None:
        total_count[0] += 1
        mark = "✓" if cond else "✗"
        print(f"    {mark} {label}")
        if cond:
            ok_count[0] += 1

    # ── 1) 阶段上下文 + 报告 ──
    print("\n[1] 阶段上下文计时")
    timer = StageTimer("回测")
    with timer.phase("数据加载"):
        _time.sleep(0.02)
    with timer.phase("因子计算"):
        _time.sleep(0.01)
    with timer.phase("回测执行"):
        _time.sleep(0.03)
    _ok(timer.has_stage("数据加载") and timer.has_stage("回测执行"), "三个阶段均已登记")
    _ok(timer.count("数据加载") == 1, "数据加载执行 1 次")
    _ok(timer.elapsed("数据加载") >= 0.02, "数据加载累计耗时 >= 20ms")
    _ok(timer.total() > 0.05, "总耗时 > 50ms")
    report = timer.report()
    _ok("数据加载" in report and "因子计算" in report and "回测执行" in report, "报告包含全部阶段")
    _ok("占比" in report and "合计" in report, "报告含占比与合计")
    print(report)

    # ── 2) 同阶段多次累加 + 统计 ──
    print("\n[2] 同阶段多次累加")
    with timer.phase("回测执行"):
        _time.sleep(0.005)
    with timer.phase("回测执行"):
        _time.sleep(0.005)
    _ok(timer.count("回测执行") == 3, "回测执行累计 3 次")
    stats = timer.to_dict()["stages"]["回测执行"]
    _ok(stats["min_seconds"] <= stats["max_seconds"], "min <= max")
    _ok(stats["max_seconds"] >= 0.005, "max >= 5ms")

    # ── 3) 手动 start/stop ──
    print("\n[3] 手动 start/stop")
    timer2 = StageTimer("手动")
    timer2.start("保存结果")
    _time.sleep(0.008)
    dt = timer2.stop()
    _ok(dt >= 0.008, f"手动 stop 返回耗时 >= 8ms（实际 {dt*1000:.1f}ms）")
    _ok(timer2.count("保存结果") == 1, "手动阶段计数 1")

    # ── 4) 嵌套 phase ──
    print("\n[4] 嵌套阶段")
    with timer2.phase("外层"):
        _time.sleep(0.005)
        with timer2.phase("内层"):
            _time.sleep(0.004)
        _time.sleep(0.003)
    _ok(timer2.count("外层") == 1 and timer2.count("内层") == 1, "嵌套阶段各自计数 1")
    _ok(timer2.elapsed("外层") >= timer2.elapsed("内层"), "外层耗时 >= 内层耗时")

    # ── 5) split 分段 ──
    print("\n[5] split 分段")
    timer2.start("阶段A")
    _time.sleep(0.003)
    timer2.stop()
    _time.sleep(0.002)  # 间隙
    gap = timer2.split("A后间隙")
    _ok(gap >= 0.002, f"split 捕获间隙耗时 >= 2ms（实际 {gap*1000:.1f}ms）")

    # ── 6) 禁用模式 ──
    print("\n[6] 禁用模式")
    off_timer = StageTimer("关闭", enabled=False)
    with off_timer.phase("任何阶段"):
        _time.sleep(0.005)
    _ok(off_timer.elapsed("任何阶段") == 0.0, "禁用时 elapsed 恒为 0")
    _ok(off_timer.count("任何阶段") == 0, "禁用时不累计次数")
    _ok(off_timer.stop() == 0.0, "禁用时 stop 返回 0")

    # ── 7) 参数校验 / 异常 ──
    print("\n[7] 参数校验与异常")
    try:
        StageTimer(name="")
        _ok(False, "空 name 应抛 ValueError")
    except ValueError:
        _ok(True, "空 name 正确抛 ValueError")
    try:
        timer.start("未结束")
        timer.start("未结束")
        _ok(False, "重复 start 应抛 RuntimeError")
    except RuntimeError:
        _ok(True, "重复 start 正确抛 RuntimeError")
    try:
        StageTimer("x").stop("不存在的阶段")
        _ok(False, "stop 未计时阶段应抛 RuntimeError")
    except RuntimeError:
        _ok(True, "stop 未计时阶段正确抛 RuntimeError")
    try:
        StageTimer("x").split("孤立分段")
        _ok(False, "无计时点的 split 应抛 RuntimeError")
    except RuntimeError:
        _ok(True, "无计时点的 split 正确抛 RuntimeError")

    # ── 8) clear / reset ──
    print("\n[8] clear / reset")
    timer2.clear("阶段A")
    _ok(not timer2.has_stage("阶段A"), "clear 移除指定阶段")
    timer2.reset()
    _ok(timer2.total() == 0.0 and len(timer2.stage_names()) == 0, "reset 清空全部统计")

    # ── 9) 模块级注册表 ──
    print("\n[9] 模块级注册表")
    t1 = get_stage_timer("共享")
    t2 = get_stage_timer("共享")
    _ok(t1 is t2, "同名 get_stage_timer 返回同一实例")
    reset_stage_timers()
    t3 = get_stage_timer("共享")
    _ok(t3 is not t1, "reset 后重建新实例")

    # ── 10) 时长格式化 ──
    print("\n[10] format_duration")
    _ok(format_duration(0.0) == "0.000 s", "0 秒格式")
    _ok(format_duration(0.0005) == "500.000 µs", "微秒格式")
    _ok(format_duration(0.5) == "500.000 ms", "半秒转毫秒")
    _ok(format_duration(90.0) == "1.500 min", "分钟格式")
    try:
        format_duration(-1.0)
        _ok(False, "负数应抛 ValueError")
    except ValueError:
        _ok(True, "负数正确抛 ValueError")

    print("\n" + "=" * 60)
    print(f"  自测结果: {ok_count[0]}/{total_count[0]} 项通过")
    if ok_count[0] == total_count[0]:
        print("  ✓ timer.py 分阶段计时器 全部自测通过")
    else:
        print(f"  ✗ 有 {total_count[0] - ok_count[0]} 项未通过，请检查！")
    print("=" * 60)
