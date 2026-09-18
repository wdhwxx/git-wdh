# 文件名：myquant/core/async_runtime.py
"""
async_runtime.py - 异步运行时管理
==================================
功能:
  1. 事件循环创建与复用
  2. 异步任务封装 (fire-and-forget / await)
  3. 优雅关闭与信号处理 (SIGINT/SIGTERM)
  4. 任务组管理
  5. 同步/异步桥接

依赖: asyncio, signal, threading, logging
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from typing import Any, Callable, Coroutine, List, Optional, Set

logger = logging.getLogger("myquant.async_runtime")


class AsyncRuntime:
    """
    异步运行时管理器

    功能:
      - 管理事件循环生命周期
      - 支持从同步代码提交异步任务
      - 优雅关闭: 取消所有pending任务 → 等待完成 → 关闭循环
      - 信号处理: Ctrl+C / kill 自动触发关闭

    Usage:
        runtime = AsyncRuntime()
        runtime.start()

        # 提交任务
        future = runtime.submit(some_coroutine())

        # 优雅关闭
        runtime.shutdown()

    或作为上下文管理器:
        with AsyncRuntime() as rt:
            rt.submit(my_task())
    """

    def __init__(self, name: str = "myquant-runtime"):
        self.name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._tasks: Set[asyncio.Task] = set()
        self._running = False
        self._shutdown_event = threading.Event()

    # ── 生命周期 ──
    def start(self) -> None:
        """启动异步运行时 (后台线程)"""
        if self._running:
            return

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"{self.name}-loop",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        logger.info(f"AsyncRuntime '{self.name}' started")

    def _run_loop(self) -> None:
        """后台线程运行事件循环"""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except asyncio.CancelledError:
            pass
        finally:
            self._loop.close()

    async def _main(self) -> None:
        """主协程: 等待关闭信号"""
        while self._running:
            await asyncio.sleep(0.1)

    def stop(self) -> None:
        """停止运行时"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info(f"AsyncRuntime '{self.name}' stopped")

    # ── 任务提交 ──
    def submit(self, coro: Coroutine) -> "asyncio.Future":
        """
        提交协程到事件循环 (线程安全)
        返回 concurrent.futures.Future
        """
        if not self._loop or not self._running:
            raise RuntimeError("AsyncRuntime 未启动")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run_until_complete(self, coro: Coroutine, timeout: Optional[float] = None) -> Any:
        """同步等待协程完成"""
        future = self.submit(coro)
        return future.result(timeout=timeout)

    def create_task(self, coro: Coroutine) -> None:
        """创建后台任务 (fire-and-forget)"""
        if not self._loop:
            raise RuntimeError("AsyncRuntime 未启动")

        async def _wrapper():
            task = asyncio.ensure_future(coro)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            try:
                return await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Task error: {e}")

        asyncio.run_coroutine_threadsafe(_wrapper(), self._loop)

    # ── 优雅关闭 ──
    def shutdown(self, timeout: float = 10.0) -> None:
        """
        优雅关闭:
          1. 取消所有未完成任务
          2. 等待清理
          3. 关闭事件循环
        """
        logger.info(f"AsyncRuntime '{self.name}' shutting down...")
        self._running = False

        if self._loop and self._loop.is_running():
            # 取消所有任务
            async def _cancel_all():
                tasks = [t for t in self._tasks if not t.done()]
                for t in tasks:
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            try:
                asyncio.run_coroutine_threadsafe(_cancel_all(), self._loop).result(timeout=timeout)
            except Exception as e:
                # #R1修复：取消任务时的异常不影响shutdown主流程，但记录debug日志便于排查
                logger.debug(f"AsyncRuntime '{self.name}' cancel tasks exception: {e}")

        self.stop()
        logger.info(f"AsyncRuntime '{self.name}' shutdown complete")

    # ── 信号处理 ──
    def install_signal_handlers(self) -> None:
        """安装 SIGINT/SIGTERM 信号处理器"""
        def _handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.warning(f"Received {sig_name}, initiating graceful shutdown...")
            self.shutdown()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    # ── 上下文管理器 ──
    def __enter__(self) -> "AsyncRuntime":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.shutdown()

    # ── 属性 ──
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def pending_tasks(self) -> int:
        return len([t for t in self._tasks if not t.done()])

    def __repr__(self) -> str:
        status = "running" if self._running else "stopped"
        return f"AsyncRuntime(name='{self.name}', status={status}, tasks={len(self._tasks)})"


# ═══════════════════════════════════════════════
#  同步/异步桥接工具
# ═══════════════════════════════════════════════

def run_sync(coro: Coroutine) -> Any:
    """
    在同步上下文中运行协程
    如果已有事件循环则在新线程中运行
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # 已在异步上下文中，用线程桥接
        result = [None]
        exception = [None]

        def _run():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result[0] = new_loop.run_until_complete(coro)
            except Exception as e:
                exception[0] = e
            finally:
                new_loop.close()

        t = threading.Thread(target=_run)
        t.start()
        t.join()
        if exception[0]:
            raise exception[0]
        return result[0]
    else:
        return asyncio.run(coro)


def async_to_sync(func: Callable[..., Coroutine]) -> Callable:
    """
    将异步函数包装为同步函数

    @async_to_sync
    async def fetch_data():
        ...

    result = fetch_data()  # 同步调用
    """
    def wrapper(*args, **kwargs):
        return run_sync(func(*args, **kwargs))
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper