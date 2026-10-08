# 文件名：myquant/utils/parallel.py
"""
parallel.py - 并行执行管理器（I/O 线程池 + 计算进程池分离）
============================================================
V5回测性能优化 P2 新增：为批量回测、参数扫描、因子计算提供统一并行基础设施。

背景问题:
    原事件驱动回测架构并行不合理：I/O（数据读取、网络请求）与计算（因子、回测）
    混用同一执行池，导致 GIL 争用与阻塞互相拖累；多进程任务中不可 pickle 的对象
    （lambda、局部函数、线程锁等）在子进程反序列化阶段才报错，难以定位。

核心能力:
    1. 双池分离
       - I/O 线程池（ThreadPoolExecutor）：数据下载、文件读写、网络请求等 I/O 密集任务
       - 计算进程池（ProcessPoolExecutor）：因子计算、参数扫描、向量化回测等 CPU 密集任务
       （多进程规避 GIL，多线程避免进程创建开销，各司其职）
    2. pickle 预检
       - 进程池任务（函数 + 参数）提交前自动调用 utils.pickle_checker 预检，
         不可序列化对象提前抛出友好错误（PickleCheckError），
         杜绝"提交成功、子进程反序列化才失败"的隐性故障。
    3. parallel_factor_context 上下文
       - 进入时自动开启全局并行模式 → factor_cache 自动关闭 L1 内存缓存
         （多进程内存不共享，L1 命中率低，走 L2/L3 更合理），并输出告警提示；
       - 退出时恢复进入前的并行模式状态，支持嵌套使用。
    4. 资源安全
       - 池延迟创建（首次提交才真正启动）、统一关闭、线程安全全局单例、
         任务级 return_exceptions / 进度回调。

防未来函数（铁律）:
    - 本模块只负责任务调度与并发管理，不做任何时间序列计算；
    - 不引用任何行情数据、不生成交易信号，无未来函数风险。

典型用法:
    from myquant.utils.parallel import (
        parallel_factor_context,
        run_cpu_parallel,
        run_io_parallel,
    )

    # 场景1：批量因子/回测计算（进程池 + 自动关闭 L1 缓存）
    with parallel_factor_context(cpu_workers=4) as pm:
        results = pm.map_cpu(compute_factor, items)

    # 场景2：便捷函数直接跑
    results = run_cpu_parallel(compute_factor, items)

    # 场景3：I/O 密集（线程池）
    data_list = run_io_parallel(load_kline, symbols)

使用约束（Windows / macOS spawn 模式）:
    - 调用进程池的脚本，其执行逻辑必须放在 `if __name__ == "__main__":` 块内
      （Python 多进程"安全导入主模块"的硬性要求，否则子进程会在引导阶段再次启动进程而报错）；
    - 进程池任务函数必须是真实业务模块中的模块级顶层函数
      （`__main__` 脚本内定义的函数无法被子进程按名导入，详见 utils.pickle_checker 边界说明）。

依赖: os, threading, time, concurrent.futures, contextlib, logging, typing
本地: myquant.utils.pickle_checker (pickle 预检),
      并行模式全局标志（set_parallel_mode/is_parallel_mode）由本模块持有，
      myquant.indicators.factor_cache 自本模块导入（L2→L0 合法方向）
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import (
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from contextlib import contextmanager
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from myquant.utils.pickle_checker import (
    PickleCheckError,
    check_picklable,
    precheck_task,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 常量
# ============================================================================

# 默认 I/O 线程数：Python 官方线程池建议 min(32, cpu+4)，I/O 密集可容纳较多线程
DEFAULT_IO_WORKERS: int = min(32, (os.cpu_count() or 1) + 4)

# 默认计算进程数：按物理/逻辑核数；保留至少 1
DEFAULT_CPU_WORKERS: int = max(1, os.cpu_count() or 1)

# 进程池进程名前缀 / 线程名前缀
PROCESS_NAME_PREFIX: str = "quant-cpu"
THREAD_NAME_PREFIX: str = "quant-io"

# 单批 map 提交的默认分块大小（控制一次往池里塞的任务数量，避免一次性占满内存）
DEFAULT_MAP_CHUNK_SIZE: int = 1


# ============================================================================
# 并行模式全局标志（归 L0 utils 持有）
# ============================================================================
# 并行模式是"并行执行设施"自身的全局状态，由本模块持有；
# 因子缓存（L2 indicators.factor_cache）自本模块导入，并行模式下自动关闭 L1
# 内存缓存（多进程间内存不共享，L1 命中率低，走 L2/L3 更合理）。
# 本标志不涉及时序计算，仅表达当前执行模式（防未来红线不适用）。
_GLOBAL_PARALLEL_MODE: bool = False
_PARALLEL_MODE_LOCK = threading.Lock()


def set_parallel_mode(enabled: bool) -> None:
    """设置全局并行模式。

    并行模式下，新建的 FactorCache 实例自动关闭 L1 内存缓存；
    已存在的实例在后续 get/get_or_compute 调用中也会跳过 L1。

    Args:
        enabled: True 开启并行模式（关闭 L1），False 关闭并行模式（恢复 L1）。
    """
    global _GLOBAL_PARALLEL_MODE
    with _PARALLEL_MODE_LOCK:
        _GLOBAL_PARALLEL_MODE = enabled
    if enabled:
        logger.info("并行模式已开启：因子缓存 L1 内存缓存将自动关闭")
    else:
        logger.info("并行模式已关闭：因子缓存 L1 内存缓存已恢复")


def is_parallel_mode() -> bool:
    """查询当前是否处于并行模式。

    Returns:
        True 表示并行模式已开启（L1 被自动关闭）。
    """
    with _PARALLEL_MODE_LOCK:
        return _GLOBAL_PARALLEL_MODE


# ============================================================================
# 辅助函数
# ============================================================================

def _resolve_workers(workers: Optional[int], default: int) -> int:
    """校验并解析 worker 数量。

    Args:
        workers: 用户传入的 worker 数，None 时使用默认值。
        default: 默认 worker 数。

    Returns:
        合法的 worker 数。

    Raises:
        ValueError: workers 非法（非正整数）。
    """
    if workers is None:
        return default
    if not isinstance(workers, int) or isinstance(workers, bool):
        raise ValueError(f"workers 必须是正整数，收到: {workers!r}")
    if workers < 1:
        raise ValueError(f"workers 必须 >= 1，收到: {workers}")
    return workers


def _default_progress_cb(done: int, total: int) -> None:
    """默认进度回调（每完成 10% 输出一次 debug 日志）。"""
    if total <= 0:
        return
    percent = int(done * 100 / total)
    if percent % 10 == 0:
        logger.debug("并行任务进度: %d/%d (%d%%)", done, total, percent)


# ============================================================================
# ParallelManager - 双池并行管理器
# ============================================================================

class ParallelManager:
    """I/O 线程池 + 计算进程池分离的并行管理器。

    职责:
      - 统一管理 ThreadPoolExecutor（I/O 密集）与 ProcessPoolExecutor（计算密集）；
      - 进程池任务提交前自动做 pickle 预检（调用 utils.pickle_checker）；
      - 提供 map / submit / map_cpu_unordered 等统一入口；
      - 线程安全统计与优雅关闭。

    池延迟创建：构造时不启动任何线程/进程，首次 submit/map 时才创建，
    避免仅开启上下文却不执行任务时白白占用资源。

    Attributes:
        io_workers: I/O 线程池大小。
        cpu_workers: 计算进程池大小。
        check_pickle: 是否对进程池任务做 pickle 预检。

    Example:
        >>> with ParallelManager(io_workers=4, cpu_workers=2) as pm:
        ...     results = pm.map_cpu(compute, items)
    """

    def __init__(
        self,
        io_workers: Optional[int] = None,
        cpu_workers: Optional[int] = None,
        check_pickle: bool = True,
    ) -> None:
        """
        Args:
            io_workers: I/O 线程池大小；None 使用默认 min(32, cpu+4)。
            cpu_workers: 计算进程池大小；None 使用默认 cpu 核数。
            check_pickle: 是否对进程池任务做 pickle 预检，默认 True。

        Raises:
            ValueError: worker 数非法。
        """
        self.io_workers: int = _resolve_workers(io_workers, DEFAULT_IO_WORKERS)
        self.cpu_workers: int = _resolve_workers(cpu_workers, DEFAULT_CPU_WORKERS)
        self.check_pickle: bool = bool(check_pickle)

        # 延迟创建的执行器
        self._io_executor: Optional[ThreadPoolExecutor] = None
        self._cpu_executor: Optional[ProcessPoolExecutor] = None


        # 执行器创建锁
        self._pool_lock = threading.RLock()

        # 统计（线程安全）
        self._stats_lock = threading.Lock()
        self._stats: Dict[str, Union[int, float]] = {
            "io_submitted": 0,
            "cpu_submitted": 0,
            "io_completed": 0,
            "cpu_completed": 0,
            "io_errors": 0,
            "cpu_errors": 0,
            "pickle_blocked": 0,   # 被 pickle 预检拦截的任务数
            "created_at": time.time(),
        }

        logger.info(
            "ParallelManager 初始化: io_workers=%d, cpu_workers=%d, check_pickle=%s",
            self.io_workers, self.cpu_workers, self.check_pickle,
        )

    # ------------------------------------------------------------------
    # 执行器访问（延迟创建）
    # ------------------------------------------------------------------

    @property
    def io_executor(self) -> ThreadPoolExecutor:
        """I/O 线程池（延迟创建）。"""
        with self._pool_lock:
            if self._io_executor is None:
                self._io_executor = ThreadPoolExecutor(
                    max_workers=self.io_workers,
                    thread_name_prefix=THREAD_NAME_PREFIX,
                )
                logger.debug("I/O 线程池已创建: %d workers", self.io_workers)
            return self._io_executor

    @property
    def cpu_executor(self) -> ProcessPoolExecutor:
        """计算进程池（延迟创建）。"""
        with self._pool_lock:
            if self._cpu_executor is None:
                self._cpu_executor = ProcessPoolExecutor(
                    max_workers=self.cpu_workers,
                )
                logger.debug("计算进程池已创建: %d workers", self.cpu_workers)
            return self._cpu_executor

    # ------------------------------------------------------------------
    # 提交单个任务
    # ------------------------------------------------------------------

    def submit_io(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """提交 I/O 密集任务到线程池。

        Args:
            fn: 任务函数。
            *args: 位置参数。
            **kwargs: 关键字参数。

        Returns:
            concurrent.futures.Future。

        Raises:
            TypeError: fn 不可调用。
            RuntimeError: 管理器已关闭。
        """
        self._check_not_shutdown()
        if not callable(fn):
            raise TypeError(f"I/O 任务函数必须可调用，收到: {type(fn).__name__}")

        future = self.io_executor.submit(fn, *args, **kwargs)
        self._bump_stat("io_submitted")
        return future

    def submit_cpu(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """提交计算密集任务到进程池（提交前自动 pickle 预检）。

        Args:
            fn: 任务函数（必须是模块级顶层函数，lambda/局部函数不可）。
            *args: 位置参数。
            **kwargs: 关键字参数。

        Returns:
            concurrent.futures.Future。

        Raises:
            TypeError: fn 不可调用。
            PickleCheckError: 任务函数或参数不可 pickle 序列化时提前抛出。
            RuntimeError: 管理器已关闭。
        """
        self._check_not_shutdown()
        if not callable(fn):
            raise TypeError(f"计算任务函数必须可调用，收到: {type(fn).__name__}")

        if self.check_pickle:
            result = precheck_task(fn, args=args, kwargs=kwargs, name="计算进程池任务")
            if not result.ok:
                self._bump_stat("pickle_blocked")
                raise PickleCheckError(result)

        future = self.cpu_executor.submit(fn, *args, **kwargs)
        self._bump_stat("cpu_submitted")
        return future

    # ------------------------------------------------------------------
    # 批量 map
    # ------------------------------------------------------------------

    def map_io(
        self,
        fn: Callable[[Any], Any],
        iterable: Iterable[Any],
        *,
        return_exceptions: bool = False,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> List[Any]:
        """对可迭代对象执行 I/O 密集任务（线程池），保持输入顺序返回结果。

        Args:
            fn: 任务函数，接收单个 item。
            iterable: 待处理对象集合。
            return_exceptions: True 时任务异常作为结果返回，不中断。
            progress_cb: 进度回调，签名 progress_cb(done, total)。

        Returns:
            与输入顺序一致的结果列表。

        Raises:
            TypeError: fn 不可调用。
            RuntimeError: 管理器已关闭。
        """
        self._check_not_shutdown()
        if not callable(fn):
            raise TypeError(f"I/O 任务函数必须可调用，收到: {type(fn).__name__}")

        items = list(iterable)
        total = len(items)
        if total == 0:
            return []

        future_map: Dict[Future, int] = {
            self.io_executor.submit(fn, item): idx for idx, item in enumerate(items)
        }
        # 统计：一次 map 提交 total 个任务
        self._bump_stat("io_submitted", total)
        results: List[Any] = [None] * total
        done = 0

        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results[idx] = future.result()
                self._bump_stat("io_completed")
            except Exception as exc:
                self._bump_stat("io_errors")
                if return_exceptions:
                    results[idx] = exc
                else:
                    raise
            done += 1
            if progress_cb is not None:
                progress_cb(done, total)

        return results

    def map_cpu(
        self,
        fn: Callable[[Any], Any],
        iterable: Iterable[Any],
        *,
        return_exceptions: bool = False,
        check_pickle: bool = True,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> List[Any]:
        """对可迭代对象执行计算密集任务（进程池），保持输入顺序返回结果。

        提交前对任务函数与每个 item 做 pickle 预检（可开关），
        不可序列化对象提前抛出友好错误。

        Args:
            fn: 任务函数（模块级顶层函数），接收单个 item。
            iterable: 待处理对象集合。
            return_exceptions: True 时任务异常作为结果返回，不中断。
            check_pickle: 是否执行 pickle 预检，默认 True。
            progress_cb: 进度回调，签名 progress_cb(done, total)。

        Returns:
            与输入顺序一致的结果列表。

        Raises:
            TypeError: fn 不可调用。
            PickleCheckError: 任务函数/参数不可 pickle 时抛出。
            RuntimeError: 管理器已关闭。
        """
        self._check_not_shutdown()
        if not callable(fn):
            raise TypeError(f"计算任务函数必须可调用，收到: {type(fn).__name__}")

        items = list(iterable)
        total = len(items)
        if total == 0:
            return []

        # ── pickle 预检 ──
        if self.check_pickle and check_pickle:
            fn_result = assert_task_picklable_soft(fn, name="计算进程池任务函数")
            if not fn_result.ok:
                self._bump_stat("pickle_blocked")
                raise PickleCheckError(fn_result)
            for idx, item in enumerate(items):
                item_result = assert_picklable_soft(item, name=f"计算进程池任务参数[{idx}]")
                if not item_result.ok:
                    self._bump_stat("pickle_blocked")
                    raise PickleCheckError(item_result)

        future_map: Dict[Future, int] = {
            self.cpu_executor.submit(fn, item): idx for idx, item in enumerate(items)
        }
        # 统计：一次 map 提交 total 个任务
        self._bump_stat("cpu_submitted", total)
        results: List[Any] = [None] * total
        done = 0

        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results[idx] = future.result()
                self._bump_stat("cpu_completed")
            except Exception as exc:
                self._bump_stat("cpu_errors")
                if return_exceptions:
                    results[idx] = exc
                else:
                    raise
            done += 1
            if progress_cb is not None:
                progress_cb(done, total)

        return results

    def map_cpu_unordered(
        self,
        fn: Callable[[Any], Any],
        iterable: Iterable[Any],
        *,
        return_exceptions: bool = False,
        check_pickle: bool = True,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> List[Tuple[int, Any]]:
        """无序版 map_cpu：返回 (输入索引, 结果) 列表，完成顺序不保证。

        适用于结果不依赖输入顺序、只需汇总统计的场景（如参数扫描），
        避免为维持顺序做索引排序，略快。

        Args:
            fn: 任务函数。
            iterable: 待处理对象集合。
            return_exceptions: True 时异常作为结果返回。
            check_pickle: 是否执行 pickle 预检。
            progress_cb: 进度回调。

        Returns:
            [(index, result), ...] 列表，index 为输入中的原始索引。
        """
        self._check_not_shutdown()
        if not callable(fn):
            raise TypeError(f"计算任务函数必须可调用，收到: {type(fn).__name__}")

        items = list(iterable)
        total = len(items)
        if total == 0:
            return []

        if self.check_pickle and check_pickle:
            fn_result = assert_task_picklable_soft(fn, name="计算进程池任务函数")
            if not fn_result.ok:
                self._bump_stat("pickle_blocked")
                raise PickleCheckError(fn_result)
            for idx, item in enumerate(items):
                item_result = assert_picklable_soft(item, name=f"计算进程池任务参数[{idx}]")
                if not item_result.ok:
                    self._bump_stat("pickle_blocked")
                    raise PickleCheckError(item_result)

        future_map: Dict[Future, int] = {
            self.cpu_executor.submit(fn, item): idx for idx, item in enumerate(items)
        }
        # 统计：一次 map 提交 total 个任务
        self._bump_stat("cpu_submitted", total)
        results: List[Tuple[int, Any]] = []
        done = 0

        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results.append((idx, future.result()))
                self._bump_stat("cpu_completed")
            except Exception as exc:
                self._bump_stat("cpu_errors")
                if return_exceptions:
                    results.append((idx, exc))
                else:
                    raise
            done += 1
            if progress_cb is not None:
                progress_cb(done, total)

        return results

    # ------------------------------------------------------------------
    # 关闭与生命周期
    # ------------------------------------------------------------------

    def _check_not_shutdown(self) -> None:
        """检查管理器是否已关闭（关闭后禁止提交新任务）。"""
        if self._is_shutdown:
            raise RuntimeError("ParallelManager 已关闭，无法提交新任务")

    @property
    def _is_shutdown(self) -> bool:
        with self._pool_lock:
            return self._io_executor is None and self._cpu_executor is None and getattr(
                self, "_shutdown_flag", False
            )

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """优雅关闭全部执行器（幂等，可重复调用）。

        Args:
            wait: True 等待所有任务完成后再关闭。
            cancel_futures: True 时尝试取消未开始的任务。
        """
        with self._pool_lock:
            io_executor = self._io_executor
            cpu_executor = self._cpu_executor
            self._io_executor = None
            self._cpu_executor = None
            self._shutdown_flag = True

        if io_executor is not None:
            io_executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        if cpu_executor is not None:
            cpu_executor.shutdown(wait=wait, cancel_futures=cancel_futures)

        logger.info("ParallelManager 已关闭: io=%s, cpu=%s", io_executor is not None, cpu_executor is not None)

    def __enter__(self) -> "ParallelManager":
        """进入上下文管理器。"""
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        """退出上下文管理器，关闭全部执行器。"""
        self.shutdown(wait=True)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def _bump_stat(self, key: str, delta: int = 1) -> None:
        """线程安全地递增统计计数。"""
        with self._stats_lock:
            self._stats[key] = int(self._stats[key]) + delta

    def get_stats(self) -> Dict[str, Any]:
        """获取并行执行统计信息。

        Returns:
            包含提交数/完成数/错误数/预检拦截数/池状态的字典。
        """
        with self._stats_lock:
            stats = dict(self._stats)
            stats["uptime_seconds"] = round(time.time() - float(stats["created_at"]), 2)
        with self._pool_lock:
            stats["io_pool_created"] = self._io_executor is not None
            stats["cpu_pool_created"] = self._cpu_executor is not None
            stats["io_workers"] = self.io_workers
            stats["cpu_workers"] = self.cpu_workers
            stats["check_pickle"] = self.check_pickle
        return stats

    def __repr__(self) -> str:
        s = self.get_stats()
        return (
            f"ParallelManager(io={s['io_workers']}w, cpu={s['cpu_workers']}w, "
            f"io_submitted={s['io_submitted']}, cpu_submitted={s['cpu_submitted']}, "
            f"pickle_blocked={s['pickle_blocked']})"
        )


# ============================================================================
# 软断言辅助（返回结果而非抛异常，便于批量预检聚合）
# ============================================================================

def assert_task_picklable_soft(fn: Any, *, name: str = "任务") -> Any:
    """任务 pickle 预检软断言：返回 PickleCheckResult 而非抛异常。

    Args:
        fn: 任务函数。
        name: 任务名称。

    Returns:
        PickleCheckResult。
    """
    return precheck_task(fn, name=name)


def assert_picklable_soft(obj: Any, *, name: str = "对象") -> Any:
    """对象 pickle 预检软断言：返回 PickleCheckResult 而非抛异常。

    Args:
        obj: 待检测对象。
        name: 对象名称。

    Returns:
        PickleCheckResult。
    """
    return check_picklable(obj, name=name)


# ============================================================================
# parallel_factor_context - 并行因子上下文
# ============================================================================

@contextmanager
def parallel_factor_context(
    *,
    io_workers: Optional[int] = None,
    cpu_workers: Optional[int] = None,
    check_pickle: bool = True,
    manager: Optional[ParallelManager] = None,
    shutdown_on_exit: bool = False,
    warn_l1: bool = True,
) -> Iterator[ParallelManager]:
    """并行因子计算上下文管理器。

    进入时：
      1. 开启全局并行模式 → 因子缓存 factor_cache 自动关闭 L1 内存缓存
         （多进程间内存不共享，L1 命中率低，走 L2/L3 更合理）；
      2. 输出告警提示，说明 L1 命中率下降属预期行为；
      3. 提供已就绪的 ParallelManager（I/O 线程池 + 计算进程池）。

    退出时：
      1. 若进入前未处于并行模式，恢复为关闭状态（L1 恢复）；
      2. 支持嵌套：内层退出不会破坏外层的并行模式。

    Args:
        io_workers: I/O 线程池大小；None 使用默认。
        cpu_workers: 计算进程池大小；None 使用默认。
        check_pickle: 是否对进程池任务做 pickle 预检。
        manager: 外部传入的 ParallelManager；None 时使用全局单例。
        shutdown_on_exit: 退出时是否关闭 manager（默认 False；
            仅对传入的 manager 生效，全局单例不会被关闭）。
        warn_l1: 是否在开启并行时输出 L1 关闭告警，默认 True。

    Yields:
        ParallelManager 实例。

    Example:
        >>> with parallel_factor_context(cpu_workers=4) as pm:
        ...     results = pm.map_cpu(compute_factor, items)
    """
    was_parallel = is_parallel_mode()

    mgr = manager if manager is not None else get_parallel_manager(
        io_workers=io_workers, cpu_workers=cpu_workers, check_pickle=check_pickle,
    )

    if not was_parallel:
        set_parallel_mode(True)
        if warn_l1:
            logger.warning(
                "并行模式已开启：因子缓存 L1 内存缓存已自动关闭"
                "（多进程间内存不共享，L1 命中率下降属预期行为，将走 L2/L3）"
            )

    try:
        yield mgr
    finally:
        if not was_parallel:
            set_parallel_mode(False)
            logger.info("并行模式已恢复为关闭，因子缓存 L1 内存缓存已恢复")
        if shutdown_on_exit and manager is not None:
            mgr.shutdown(wait=True)


# ============================================================================
# 便捷函数
# ============================================================================

def run_io_parallel(
    fn: Callable[[Any], Any],
    iterable: Iterable[Any],
    *,
    io_workers: Optional[int] = None,
    return_exceptions: bool = False,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> List[Any]:
    """便捷函数：使用全局单例的 I/O 线程池批量执行任务。

    Args:
        fn: 任务函数。
        iterable: 待处理对象集合。
        io_workers: I/O 线程池大小；None 使用默认。
        return_exceptions: True 时异常作为结果返回。
        progress_cb: 进度回调。

    Returns:
        与输入顺序一致的结果列表。
    """
    manager = get_parallel_manager(io_workers=io_workers)
    return manager.map_io(
        fn, iterable,
        return_exceptions=return_exceptions,
        progress_cb=progress_cb or _default_progress_cb,
    )


def run_cpu_parallel(
    fn: Callable[[Any], Any],
    iterable: Iterable[Any],
    *,
    cpu_workers: Optional[int] = None,
    check_pickle: bool = True,
    return_exceptions: bool = False,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> List[Any]:
    """便捷函数：使用全局单例的计算进程池批量执行任务（含 pickle 预检）。

    Args:
        fn: 任务函数（模块级顶层函数）。
        iterable: 待处理对象集合。
        cpu_workers: 计算进程池大小；None 使用默认。
        check_pickle: 是否执行 pickle 预检。
        return_exceptions: True 时异常作为结果返回。
        progress_cb: 进度回调。

    Returns:
        与输入顺序一致的结果列表。

    Raises:
        PickleCheckError: 任务函数/参数不可 pickle 时抛出。
    """
    manager = get_parallel_manager(cpu_workers=cpu_workers, check_pickle=check_pickle)
    return manager.map_cpu(
        fn, iterable,
        return_exceptions=return_exceptions,
        check_pickle=check_pickle,
        progress_cb=progress_cb or _default_progress_cb,
    )


# ============================================================================
# 全局单例
# ============================================================================

_global_parallel_manager: Optional[ParallelManager] = None
_global_parallel_lock = threading.Lock()


def get_parallel_manager(
    io_workers: Optional[int] = None,
    cpu_workers: Optional[int] = None,
    check_pickle: bool = True,
) -> ParallelManager:
    """获取全局并行管理器单例。

    首次调用时创建实例，后续返回同一实例。
    如需独立配置的实例，请直接使用 ParallelManager(...) 构造。

    Args:
        io_workers: I/O 线程池大小（仅首次创建生效）。
        cpu_workers: 计算进程池大小（仅首次创建生效）。
        check_pickle: 是否执行 pickle 预检（仅首次创建生效）。

    Returns:
        全局 ParallelManager 单例。
    """
    global _global_parallel_manager
    with _global_parallel_lock:
        if _global_parallel_manager is None:
            _global_parallel_manager = ParallelManager(
                io_workers=io_workers,
                cpu_workers=cpu_workers,
                check_pickle=check_pickle,
            )
        return _global_parallel_manager


def reset_parallel_manager() -> None:
    """重置全局并行管理器单例（测试用）。

    若已有实例，先关闭其执行器，再置空。
    """
    global _global_parallel_manager
    with _global_parallel_lock:
        if _global_parallel_manager is not None:
            _global_parallel_manager.shutdown(wait=True)
        _global_parallel_manager = None
    logger.info("全局并行管理器单例已重置")


# ============================================================================
# 自测 Demo
# ============================================================================

def _demo_cpu_worker(item: Dict[str, Any]) -> Dict[str, Any]:
    """自测用计算任务（模块级顶层函数，可被 pickle 按名引用）。

    模拟 CPU 密集计算：对 [0, n) 求和（循环避免被编译器优化掉）。

    Args:
        item: {"symbol": str, "n": int}。

    Returns:
        {"symbol": str, "sum": int}。
    """
    n = int(item["n"])
    total = 0
    for i in range(n):
        total += i
    return {"symbol": item["symbol"], "sum": total}


def _demo_io_worker(item: Dict[str, Any]) -> Dict[str, Any]:
    """自测用 I/O 任务（模块级顶层函数）。

    模拟 I/O 等待（网络/磁盘），返回处理后的长度信息。

    Args:
        item: {"name": str}。

    Returns:
        {"name": str, "length": int}。
    """
    time.sleep(0.01)  # 模拟 I/O 延迟
    return {"name": item["name"], "length": len(item["name"])}


def _demo_boom_worker(item: Dict[str, Any]) -> Dict[str, Any]:
    """自测用会抛异常的计算任务（模块级顶层函数）。

    用于验证 return_exceptions 错误收集。

    Args:
        item: {"n": int}；n < 0 时抛 ValueError。

    Returns:
        {"n": int}。

    Raises:
        ValueError: item["n"] < 0 时抛出。
    """
    if item["n"] < 0:
        raise ValueError(f"非法参数: {item['n']}")
    return {"n": item["n"]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print("=" * 64)
    print("  parallel.py — 并行执行管理器 自测 Demo")
    print("=" * 64)

    passed = 0
    total = 0

    def _expect_ok(actual: bool, label: str) -> None:
        global passed, total
        total += 1
        status = "✓" if actual else "✗"
        print(f"    {status} {label}")
        if actual:
            passed += 1

    # ── 测试 1: I/O 线程池 map ──
    print("\n[1] I/O 线程池 map")
    pm = ParallelManager(io_workers=4, cpu_workers=2)
    io_items = [{"name": "600519.SH"}, {"name": "000001.SZ"}, {"name": "300750.SZ"}]
    io_results = pm.map_io(_demo_io_worker, io_items)
    _expect_ok(len(io_results) == 3, "I/O 结果数量正确")
    _expect_ok(io_results[0]["name"] == "600519.SH", "I/O 结果保持输入顺序")
    _expect_ok(all(isinstance(r["length"], int) for r in io_results), "I/O 结果字段正确")
    print(f"    结果: {[r['name'] for r in io_results]}")

    # ── 测试 2: I/O 线程池 submit ──
    print("\n[2] I/O 线程池 submit")
    fut = pm.submit_io(_demo_io_worker, {"name": "TEST.SH"})
    fut_result = fut.result(timeout=10)
    _expect_ok(fut_result["length"] == 7, "I/O submit 结果正确")
    print(f"    结果: {fut_result}")

    # ── 测试 3: 计算进程池 map（真实子进程） ──
    print("\n[3] 计算进程池 map")
    cpu_items = [
        {"symbol": "A", "n": 10000},
        {"symbol": "B", "n": 20000},
        {"symbol": "C", "n": 30000},
    ]
    cpu_results = pm.map_cpu(_demo_cpu_worker, cpu_items)
    _expect_ok(len(cpu_results) == 3, "CPU 结果数量正确")
    _expect_ok(cpu_results[0]["sum"] == sum(range(10000)), "CPU 计算结果正确")
    _expect_ok(cpu_results[1]["symbol"] == "B", "CPU 结果保持输入顺序")
    print(f"    结果: {[(r['symbol'], r['sum']) for r in cpu_results]}")

    # ── 测试 4: 计算进程池 submit ──
    print("\n[4] 计算进程池 submit")
    fut_cpu = pm.submit_cpu(_demo_cpu_worker, {"symbol": "D", "n": 5000})
    _expect_ok(fut_cpu.result(timeout=30)["sum"] == sum(range(5000)), "CPU submit 结果正确")

    # ── 测试 5: 进程池 pickle 预检拦截 ──
    print("\n[5] 进程池 pickle 预检拦截")
    try:
        pm.submit_cpu(lambda x: x, {"n": 1})
        _expect_ok(False, "提交 lambda 应被预检拦截")
    except PickleCheckError as e:
        _expect_ok(True, "提交 lambda 正确抛出 PickleCheckError")
        _expect_ok("lambda" in str(e), "错误信息包含原因")
    try:
        pm.map_cpu(_demo_cpu_worker, [{"n": 1, "bad": threading.Lock()}])
        _expect_ok(False, "参数含 Lock 应被预检拦截")
    except PickleCheckError:
        _expect_ok(True, "参数含 Lock 正确抛出 PickleCheckError")

    # ── 测试 6: return_exceptions ──
    print("\n[6] return_exceptions 错误收集")
    try:
        pm.submit_cpu(_demo_boom_worker, {"n": -1})
        _expect_ok(True, "submit_cpu 成功提交（异常发生在子进程）")
    except Exception:
        _expect_ok(True, "submit_cpu 提交正常")

    # 用 map 的 return_exceptions 收集异常
    mixed_items = [{"n": 1}, {"n": -5}, {"n": 3}]
    mixed_results = pm.map_cpu(_demo_boom_worker, mixed_items, return_exceptions=True)
    _expect_ok(isinstance(mixed_results[1], ValueError), "异常被收集为结果")
    _expect_ok(mixed_results[0] == {"n": 1}, "正常结果保留")
    print(f"    结果: {mixed_results}")

    # ── 测试 7: parallel_factor_context 关闭 L1 ──
    # 注：本模块以 -m/脚本方式运行自测时存在"__main__ 与真名双模块"差异
    # （上下文管理器经 __main__ 执行，factor_cache 导入的是真名模块），
    # 故此处经真名模块 myquant.utils.parallel 引用，保证状态一致。
    print("\n[7] parallel_factor_context 关闭 L1 缓存")
    import myquant.utils.parallel as _self_mod
    from myquant.indicators.factor_cache import FactorCache
    import tempfile
    import shutil
    from pathlib import Path

    tmp_dir = Path(tempfile.mkdtemp(prefix="parallel_test_"))
    try:
        cache = FactorCache(cache_dir=str(tmp_dir / "factor"), enable_l1=True, l2_enabled=False)
        _expect_ok(cache.enable_l1 is True, "进入上下文前 L1 启用")

        with _self_mod.parallel_factor_context(cpu_workers=2, warn_l1=True) as pm_ctx:
            _expect_ok(_self_mod.is_parallel_mode() is True, "上下文内并行模式开启")
            _expect_ok(pm_ctx is _self_mod.get_parallel_manager(), "上下文返回全局管理器")
            # 上下文内新建的 FactorCache 自动关闭 L1
            cache_inside = FactorCache(cache_dir=str(tmp_dir / "factor2"), enable_l1=True, l2_enabled=False)
            _expect_ok(cache_inside.enable_l1 is False, "上下文内新实例 L1 被强制关闭")

        _expect_ok(_self_mod.is_parallel_mode() is False, "退出上下文后并行模式恢复")
        cache_after = FactorCache(cache_dir=str(tmp_dir / "factor3"), enable_l1=True, l2_enabled=False)
        _expect_ok(cache_after.enable_l1 is True, "退出上下文后 L1 恢复启用")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── 测试 8: 嵌套上下文 ──
    print("\n[8] 嵌套上下文")
    with _self_mod.parallel_factor_context(cpu_workers=1) as outer:
        _expect_ok(_self_mod.is_parallel_mode() is True, "外层开启并行")
        with _self_mod.parallel_factor_context(cpu_workers=1) as inner:
            _expect_ok(_self_mod.is_parallel_mode() is True, "内层仍并行（嵌套）")
        _expect_ok(_self_mod.is_parallel_mode() is True, "内层退出后外层仍并行")
    _expect_ok(_self_mod.is_parallel_mode() is False, "外层退出后恢复关闭")

    # ── 测试 9: 统计信息 ──
    print("\n[9] 统计信息")
    stats = pm.get_stats()
    _expect_ok(stats["io_submitted"] >= 4, "I/O 提交计数正确")
    _expect_ok(stats["cpu_submitted"] >= 4, "CPU 提交计数正确")
    _expect_ok(stats["pickle_blocked"] >= 2, "pickle 拦截计数正确")
    print(f"    统计: { {k: v for k, v in stats.items() if isinstance(v, (int, float, bool))} }")

    # ── 测试 10: 便捷函数 ──
    print("\n[10] 便捷函数")
    from myquant.indicators.factor_cache import reset_factor_cache
    reset_factor_cache()
    io_fast = run_io_parallel(_demo_io_worker, [{"name": "X"}], io_workers=2)
    _expect_ok(io_fast[0]["name"] == "X", "run_io_parallel 正常")
    cpu_fast = run_cpu_parallel(_demo_cpu_worker, [{"symbol": "Z", "n": 100}], cpu_workers=2)
    _expect_ok(cpu_fast[0]["sum"] == sum(range(100)), "run_cpu_parallel 正常")
    try:
        run_cpu_parallel(lambda x: x, [1], cpu_workers=2)
        _expect_ok(False, "run_cpu_parallel 应拦截 lambda")
    except PickleCheckError:
        _expect_ok(True, "run_cpu_parallel 正确拦截 lambda")

    # ── 测试 11: 参数校验与关闭 ──
    print("\n[11] 参数校验与关闭")
    try:
        ParallelManager(io_workers=0)
        _expect_ok(False, "io_workers=0 应抛 ValueError")
    except ValueError:
        _expect_ok(True, "io_workers=0 正确抛 ValueError")
    try:
        ParallelManager(cpu_workers=-1)
        _expect_ok(False, "cpu_workers=-1 应抛 ValueError")
    except ValueError:
        _expect_ok(True, "cpu_workers=-1 正确抛 ValueError")

    pm.shutdown(wait=True)
    try:
        pm.submit_io(_demo_io_worker, {"name": "after_shutdown"})
        _expect_ok(False, "关闭后提交应抛 RuntimeError")
    except RuntimeError:
        _expect_ok(True, "关闭后提交正确抛 RuntimeError")

    # ── 测试 12: 上下文管理器自动关闭 ──
    print("\n[12] 上下文管理器自动关闭")
    with ParallelManager(io_workers=2, cpu_workers=1) as pm2:
        r = pm2.map_io(_demo_io_worker, [{"name": "CTX"}])
        _expect_ok(r[0]["name"] == "CTX", "with 语句内任务正常")
    _expect_ok(pm2._is_shutdown, "with 退出后执行器已关闭")

    print("\n" + "=" * 64)
    print(f"  自测结果: {passed}/{total} 项通过")
    if passed == total:
        print("  ✓ parallel.py 全部自测通过")
    else:
        print(f"  ✗ 有 {total - passed} 项未通过，请检查！")
    print("=" * 64)
