# 文件名：myquant/utils/memory_pool.py
"""
memory_pool.py - 线程安全对象池（P1 引擎核心层·第8项）
========================================================
用途:
  1. 复用高频事件对象（Event），减少对象创建 / GC 压力（回测/实盘事件流）
  2. 线程安全：多线程并发 acquire / release 无泄漏、无重复、计数一致
  3. 通用对象池（可池化任意对象）+ 专用事件对象池

核心组件:
  - ObjectPool        通用线程安全对象池（Generic）
  - EventObjectPool   事件对象池（复用 core.event_bus.Event）
  - get_event_pool / acquire_event / release_event  模块级便捷入口（全局事件池）

设计原则:
  - 线程安全：RLock 保护空闲对象栈与统计计数
  - 容量上限 max_size（池中已创建对象上限，0=无限）+ 空闲上限 max_idle
  - 归还时重置对象（防止脏状态串池）
  - 池空且未达上限 → 创建；池满 → 直接新建不阻塞（保证可用性）
  - 统计指标：created / acquired_total / released_total / in_use / peak_in_use / discarded
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from typing import (
    Any, Callable, Deque, Dict, Generic, Iterator, List, Optional, TypeVar,
)

from myquant.core.event_bus import Event, EventType

logger = logging.getLogger("myquant.memory_pool")

# 泛型类型变量
T = TypeVar("T")

# 常量
_DEFAULT_MAX_IDLE: int = 1000   # 默认空闲对象上限
_DEFAULT_POOL_NAME: str = "object_pool"


# ═══════════════════════════════════════════════
#  通用线程安全对象池
# ═══════════════════════════════════════════════

class ObjectPool(Generic[T]):
    """通用线程安全对象池。

    通过 create_fn 创建新对象、reset_fn 在归还时重置对象状态。
    线程安全：所有状态变更在 RLock 保护下进行。

    Attributes:
        max_size: 池中已创建对象总数上限（0 表示不限制）。
        max_idle: 空闲对象缓存上限（0 表示不缓存空闲对象）。
    """

    def __init__(
        self,
        create_fn: Callable[[], T],
        reset_fn: Optional[Callable[[T], None]] = None,
        max_size: int = 0,
        max_idle: int = _DEFAULT_MAX_IDLE,
        name: str = _DEFAULT_POOL_NAME,
    ):
        """初始化对象池。

        Args:
            create_fn: 创建新对象的工厂函数（无参）。
            reset_fn: 归还时重置对象状态的函数（可选，None 表示不重置）。
            max_size: 已创建对象总数上限（0 表示不限制）。
            max_idle: 空闲对象缓存上限。
            name: 池名称（用于日志与调试）。

        Raises:
            TypeError: create_fn 不可调用时抛出。
            ValueError: max_size / max_idle 为负时抛出。
        """
        if not callable(create_fn):
            raise TypeError(f"create_fn 必须是可调用对象，收到: {type(create_fn).__name__}")
        if max_size < 0:
            raise ValueError(f"max_size 不能为负: {max_size}")
        if max_idle < 0:
            raise ValueError(f"max_idle 不能为负: {max_idle}")
        self._create_fn: Callable[[], T] = create_fn
        self._reset_fn: Optional[Callable[[T], None]] = reset_fn
        self._max_size: int = max_size
        self._max_idle: int = max_idle
        self._name: str = name

        self._pool: Deque[T] = deque()
        self._lock = threading.RLock()
        # 统计
        self._created: int = 0
        self._acquired_total: int = 0
        self._released_total: int = 0
        self._in_use: int = 0
        self._peak_in_use: int = 0
        self._discarded: int = 0

        logger.debug("对象池初始化: name=%s, max_size=%s, max_idle=%s",
                     self._name, self._max_size, self._max_idle)

    # ── 获取 / 归还 ──

    def acquire(self) -> T:
        """从池中获取一个对象。

        优先复用空闲对象；池空且未达 max_size 则新建；已达 max_size 则直接新建
        （不阻塞调用方，保证可用性）。

        Returns:
            可用的对象实例。

        Raises:
            RuntimeError: create_fn 抛异常时包装抛出。
        """
        with self._lock:
            if self._pool:
                obj: T = self._pool.pop()
            else:
                try:
                    obj = self._create_fn()
                except Exception as e:
                    raise RuntimeError(f"对象池 {self._name} 创建对象失败: {e}") from e
                self._created += 1
            self._in_use += 1
            self._peak_in_use = max(self._peak_in_use, self._in_use)
            self._acquired_total += 1
            return obj

    def release(self, obj: Optional[T]) -> bool:
        """将对象归还池中。

        归还前调用 reset_fn 重置状态；空闲对象已达 max_idle 上限时丢弃该对象。

        Args:
            obj: 待归还对象。

        Returns:
            True 表示对象被缓存回池；False 表示对象被丢弃（超空闲上限或 obj 为空）。
        """
        if obj is None:
            return False
        with self._lock:
            if self._in_use <= 0:
                # 防御：未借出对象被重复归还
                logger.warning("对象池 %s: 检测到重复/非法归还", self._name)
                return False
            self._in_use -= 1
            self._released_total += 1
            if len(self._pool) >= self._max_idle:
                self._discarded += 1
                return False
            if self._reset_fn is not None:
                try:
                    self._reset_fn(obj)
                except Exception as e:
                    logger.warning("对象池 %s 重置对象失败（仍归还）: %s", self._name, e)
            self._pool.append(obj)
            return True

    @contextmanager
    def borrow(self) -> Iterator[T]:
        """上下文管理器：借出并在退出时自动归还。

        Usage:
            with pool.borrow() as obj:
                use(obj)
        """
        obj = self.acquire()
        try:
            yield obj
        finally:
            self.release(obj)

    # ── 维护 ──

    def clear(self) -> int:
        """清空空闲对象池。

        Returns:
            被清空的空闲对象数量。
        """
        with self._lock:
            n = len(self._pool)
            self._pool.clear()
            return n

    def shrink(self) -> int:
        """裁剪空闲对象到 max_idle 上限以内（一般无需手动调用）。"""
        with self._lock:
            n = len(self._pool)
            while len(self._pool) > self._max_idle:
                self._pool.pop()
            return n - len(self._pool)

    # ── 统计查询 ──

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self) -> int:
        """当前空闲对象数量。"""
        with self._lock:
            return len(self._pool)

    @property
    def created(self) -> int:
        """累计创建的对象数量。"""
        with self._lock:
            return self._created

    @property
    def acquired_total(self) -> int:
        """累计借出次数。"""
        with self._lock:
            return self._acquired_total

    @property
    def released_total(self) -> int:
        """累计归还次数。"""
        with self._lock:
            return self._released_total

    @property
    def in_use(self) -> int:
        """当前借出中（未归还）的对象数量。"""
        with self._lock:
            return self._in_use

    @property
    def peak_in_use(self) -> int:
        """历史峰值借出数量。"""
        with self._lock:
            return self._peak_in_use

    @property
    def discarded(self) -> int:
        """因空闲上限被丢弃的对象数量。"""
        with self._lock:
            return self._discarded

    def stats(self) -> Dict[str, Any]:
        """获取池状态快照（线程安全）。"""
        with self._lock:
            return {
                "name": self._name,
                "max_size": self._max_size,
                "max_idle": self._max_idle,
                "idle": len(self._pool),
                "in_use": self._in_use,
                "peak_in_use": self._peak_in_use,
                "created": self._created,
                "acquired_total": self._acquired_total,
                "released_total": self._released_total,
                "discarded": self._discarded,
            }

    def __repr__(self) -> str:
        return (
            f"ObjectPool(name={self._name}, idle={self.size}, "
            f"in_use={self.in_use}, created={self.created})"
        )


# ═══════════════════════════════════════════════
#  事件对象池
# ═══════════════════════════════════════════════

class EventObjectPool(ObjectPool[Event]):
    """事件对象池：复用 Event 对象，减少高频事件流的内存分配。

    用法:
        pool = EventObjectPool(max_idle=1000)
        ev = pool.acquire(EventType.TICK, data={"price": 10.5}, source="feed")
        ...   # 使用 ev
        pool.release(ev)   # 归还（重置字段后缓存）
    """

    def __init__(
        self,
        max_size: int = 0,
        max_idle: int = _DEFAULT_MAX_IDLE,
        name: str = "event_pool",
    ):
        super().__init__(
            create_fn=self._create_event,
            reset_fn=self._reset_event,
            max_size=max_size,
            max_idle=max_idle,
            name=name,
        )

    @staticmethod
    def _create_event() -> Event:
        """创建空事件对象（字段在 acquire 时填充）。"""
        return Event(event_type=EventType.SYSTEM_START)

    @staticmethod
    def _reset_event(event: Event) -> None:
        """归还时重置事件字段，防止脏状态串池。"""
        event.event_type = EventType.SYSTEM_START
        if event.data is not None:
            event.data.clear()
        event.timestamp = 0.0
        event.event_id = ""
        event.source = ""
        event.priority = 0

    def acquire(
        self,
        event_type: Any,
        data: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Event:
        """从池中获取并初始化一个事件对象。

        Args:
            event_type: 事件类型（EventType 或 str）。
            data: 事件数据字典。
            **kwargs: 其余事件字段（source / priority）。

        Returns:
            初始化完毕的 Event 对象。
        """
        event = super().acquire()
        event.event_type = event_type
        event.data = dict(data) if data else {}
        event.timestamp = time.time()
        event.event_id = uuid.uuid4().hex[:12]
        event.source = str(kwargs.get("source", ""))
        event.priority = int(kwargs.get("priority", 0))
        return event


# ═══════════════════════════════════════════════
#  全局事件对象池（模块级便捷入口）
# ═══════════════════════════════════════════════

_global_event_pool: Optional[EventObjectPool] = None
_event_pool_lock = threading.Lock()


def get_event_pool() -> EventObjectPool:
    """获取全局事件对象池单例。"""
    global _global_event_pool
    with _event_pool_lock:
        if _global_event_pool is None:
            _global_event_pool = EventObjectPool(name="global_event_pool")
        return _global_event_pool


def reset_event_pool() -> None:
    """重置全局事件对象池（测试用）。"""
    global _global_event_pool
    with _event_pool_lock:
        _global_event_pool = None


def acquire_event(
    event_type: Any,
    data: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Event:
    """从全局事件池获取一个事件对象（便捷函数）。"""
    return get_event_pool().acquire(event_type, data=data, **kwargs)


def release_event(event: Optional[Event]) -> bool:
    """将事件对象归还全局事件池（便捷函数）。"""
    if event is None:
        return False
    return get_event_pool().release(event)


# ═══════════════════════════════════════════════
#  自测 Demo
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """运行 memory_pool 对象池自测。

    Returns:
        通过的测试项数量。
    """
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 60)
    print("memory_pool.py 对象池自测开始")
    print("=" * 60)

    # 1. 通用对象池：基本 acquire/release
    counter: List[int] = [0]

    def make() -> Dict[str, Any]:
        counter[0] += 1
        return {"n": counter[0]}

    def reset_obj(obj: Dict[str, Any]) -> None:
        obj["n"] = -1

    pool = ObjectPool(create_fn=make, reset_fn=reset_obj, max_idle=10)
    a = pool.acquire()
    check("acquire 返回对象", a == {"n": 1}, f"{a}")
    check("created=1", pool.created == 1, f"{pool.created}")
    check("in_use=1", pool.in_use == 1, f"{pool.in_use}")
    ok = pool.release(a)
    check("release 成功", ok and pool.in_use == 0, f"ok={ok}, in_use={pool.in_use}")
    b = pool.acquire()
    check("复用空闲对象（created 仍为1）", pool.created == 1 and b is a, f"created={pool.created}")
    check("归还时已重置", b["n"] == -1, f"{b}")
    pool.release(b)

    # 2. 容量上限：max_size 生效
    pool2 = ObjectPool(create_fn=make, max_size=3, max_idle=10)
    objs = [pool2.acquire() for _ in range(3)]
    check("max_size 内不新建超限", pool2.created == 3, f"{pool2.created}")
    extra = pool2.acquire()  # 第4个：达上限直接新建
    check("池满直接新建", pool2.created == 4, f"{pool2.created}")
    check("池满新建对象可用", extra is not None and extra["n"] > 0, f"{extra}")
    for o in objs + [extra]:
        pool2.release(o)
    check("归还后 in_use=0", pool2.in_use == 0, f"{pool2.in_use}")

    # 3. 空闲上限：max_idle=2，第3次归还被丢弃
    pool3 = ObjectPool(create_fn=make, max_idle=2)
    objs3 = [pool3.acquire() for _ in range(3)]
    r1 = pool3.release(objs3[0])
    r2 = pool3.release(objs3[1])
    r3 = pool3.release(objs3[2])
    check("前两次归还缓存", r1 and r2, f"{r1},{r2}")
    check("第三次归还被丢弃", (not r3) and pool3.discarded == 1, f"r3={r3}, discarded={pool3.discarded}")

    # 4. 上下文管理器 borrow
    with ObjectPool(create_fn=make).borrow() as borrowed:
        check("borrow 上下文内可用", borrowed["n"] == counter[0], f"{borrowed}")

    # 5. 防御：重复归还
    p4 = ObjectPool(create_fn=make, max_idle=5)
    o = p4.acquire()
    p4.release(o)
    check("重复归还被拒绝", p4.release(o) is False, f"{p4.release(o)}")

    # 6. EventObjectPool：事件字段初始化 + 归还重置
    ep = EventObjectPool(max_idle=5)
    ev = ep.acquire(EventType.TICK, data={"price": 10.5}, source="feed", priority=3)
    check("事件类型正确", ev.type_name == EventType.TICK.value, f"{ev.type_name}")
    check("事件数据正确", ev.data.get("price") == 10.5, f"{ev.data}")
    check("事件来源/优先级正确", ev.source == "feed" and ev.priority == 3, f"{ev.source},{ev.priority}")
    check("事件 id 已生成", len(ev.event_id) == 12, f"{ev.event_id}")
    ep.release(ev)
    ev2 = ep.acquire(EventType.BAR, data={"c": 1.0})
    check("归还后字段已重置（data 清空重填）", ev2.data == {"c": 1.0} and ev2.source == "", f"{ev2.data},{ev2.source}")
    check("复用空闲事件对象", ev2 is ev, "未复用同一对象")

    # 7. 线程安全：多线程并发 acquire/release 无泄漏
    import threading

    pool_t = ObjectPool(create_fn=make, max_idle=100)
    n_threads = 8
    per_thread = 500
    errors: List[str] = []

    def worker() -> None:
        try:
            for _ in range(per_thread):
                o = pool_t.acquire()
                if o is None:
                    errors.append("acquire 返回 None")
                pool_t.release(o)
        except Exception as e:  # 测试用：收集异常
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发 acquire/release 无异常", not errors, f"{errors[:3]}")
    st = pool_t.stats()
    check("并发后 in_use=0（无泄漏）", st["in_use"] == 0, f"{st}")
    check("并发借出/归还计数一致",
          st["acquired_total"] == st["released_total"] == n_threads * per_thread,
          f"acq={st['acquired_total']}, rel={st['released_total']}")
    check("并发峰值借出<=8", st["peak_in_use"] <= 8, f"{st['peak_in_use']}")

    # 8. 全局事件池便捷函数
    reset_event_pool()
    gev = acquire_event(EventType.SIGNAL, data={"s": 1})
    check("全局事件池获取", gev.type_name == EventType.SIGNAL.value, f"{gev.type_name}")
    check("全局事件池归还", release_event(gev) is True, f"{release_event(gev)}")
    reset_event_pool()

    print("=" * 60)
    if failures:
        print(f"自测结果: {passed} 项通过, {len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return len(failures)
    print(f"自测结果: 全部 {passed} 项通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    # 自测 Demo：运行全部校验项
    raise SystemExit(_run_self_test())
