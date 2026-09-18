# 文件名：myquant/utils/pickle_checker.py
"""
pickle_checker.py - pickle 序列化预检工具
==========================================
V5回测性能优化 P2 新增：多进程任务提交前的序列化预检，提前抛出友好错误。

问题背景:
    多进程（ProcessPoolExecutor / multiprocessing.Pool）任务提交时，若任务函数或参数
    中存在不可 pickle 序列化的对象（lambda、局部函数、生成器、线程锁、Logger、文件句柄等），
    失败通常发生在子进程反序列化阶段，报错信息晦涩且难以定位是"哪个参数、哪一层嵌套"出了问题。

功能:
    1. scan_unpicklable   : 静态启发式深度扫描，递归定位不可 pickle 对象的路径
    2. check_picklable    : 主入口，静态扫描 + 真实往返测试（pickle.dumps/loads）双保险
    3. is_picklable       : 布尔判断
    4. assert_picklable   : 预检断言，失败抛出 PickleCheckError（携带完整路径/原因）
    5. pickle_guard       : 上下文管理器，进入代码块前批量预检
    6. precheck_task      : 针对进程池任务（函数 + args + kwargs）的专项预检
    7. assert_task_picklable : 便捷断言入口

设计原则:
    - 快：静态扫描不做实际序列化，O(节点数)，用于抓取绝大多数结构性不可 pickle 对象；
    - 准：对小中型对象再做真实 pickle 往返测试兜底，捕获运行时序列化失败
          （如 __reduce__ 抛异常、绑定方法绑定不可序列化实例等）；
    - 稳：超大对象（超过 max_test_bytes）跳过往返测试并明确标注 skipped，避免拖慢主流程；
    - 安全：循环引用有 seen 去重、深度/问题数有上限，绝不因扫描自身而崩溃。

典型用法:
    from myquant.utils.pickle_checker import assert_picklable, pickle_guard, precheck_task

    # 场景1：提交任务前预检参数
    assert_picklable({"symbols": ["600519.SH"], "strategy": "ma"})

    # 场景2：批量预检多个对象
    with pickle_guard(config, strategy_params, name="参数扫描任务"):
        pool.submit(run_scan, config, strategy_params)

    # 场景3：预检整个进程池任务（函数 + 参数）
    result = precheck_task(run_scan, args=(config,), kwargs={"fast": True})
    if not result.ok:
        raise PickleCheckError(result)

依赖: 仅标准库（pickle, types, inspect, io, logging, threading, multiprocessing, functools）
"""
from __future__ import annotations

import _thread
import dataclasses
import enum
import functools
import io
import logging
import multiprocessing
import multiprocessing.connection
import multiprocessing.queues
import pickle
import socket
import sys
import threading
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# 常量
# ============================================================================

PROTOCOL: int = pickle.HIGHEST_PROTOCOL
"""pickle 协议版本（最高，兼容性最好）"""

DEFAULT_MAX_DEPTH: int = 32
"""静态扫描最大递归深度，防止深嵌套对象导致递归过深"""

DEFAULT_MAX_ITEMS: int = 50
"""最多报告多少个不可 pickle 问题，超出后截断并提示"""

DEFAULT_MAX_TEST_BYTES: int = 64 * 1024 * 1024
"""真实往返测试的对象大小上限（64MB）。超过则跳过往返测试，仅做静态扫描"""

MAX_PATH_LENGTH: int = 200
"""问题路径的最大展示长度，超长路径截断避免刷屏"""

_PRIMITIVE_TYPES: Tuple[type, ...] = (
    type(None), bool, int, float, complex, str, bytes, bytearray,
)
"""基本标量类型（必然可 pickle，直接作为叶子节点）"""

_UNPICKLABLE_THREADING_TYPES: Tuple[type, ...] = (
    threading.Event,
    threading.Condition,
    threading.Semaphore,
    threading.BoundedSemaphore,
    threading.Barrier,
)
"""线程同步对象（Event/Condition/Semaphore 等）：与进程内状态绑定，跨进程必然不可序列化。

注意: threading.Lock / RLock 在 Python 3.12 中为工厂函数而非类型，其实例类型为
_thread.lock / _thread.RLock，单独在 _detect_unpicklable 中判断。
"""

_UNPICKLABLE_MULTIPROCESSING_TYPES: Tuple[type, ...] = (
    multiprocessing.queues.Queue,
    multiprocessing.queues.SimpleQueue,
    multiprocessing.connection.Connection,
    multiprocessing.connection.PipeConnection,
)
"""multiprocessing 通信对象：不可跨进程序列化（需重建）

注意: multiprocessing.Queue 在 Python 3.12 中为 context 绑定方法而非类型，
实际实例类型为 multiprocessing.queues.Queue，因此使用 queues 下的真实类型判断。
"""

# numpy / pandas 的模块名前缀（用于识别大数组/表格对象，作为叶子处理避免海量元素遍历）
_LEAF_MODULE_PREFIXES: Tuple[str, ...] = ("numpy", "pandas")


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class UnpicklableItem:
    """单个不可 pickle 问题的定位信息。

    Attributes:
        path: 对象内的访问路径（如 root.config.handler / root.args[2]）。
        object_type: 问题对象的类型全名（module.QualName）。
        reason: 不可 pickle 的原因说明。
        detail: 补充细节（如往返测试抛出的异常原文），可为空串。
    """

    path: str
    object_type: str
    reason: str
    detail: str = ""


@dataclass
class PickleCheckResult:
    """pickle 预检结果。

    Attributes:
        ok: 是否通过预检。
        name: 被检查对象的名称（用于错误提示）。
        items: 所有发现的不可 pickle 问题（ok=True 时为空列表）。
        roundtrip_skipped: 是否跳过了真实往返测试（静态扫描通过但对象过大/非严格模式）。
        roundtrip_ok: 往返测试结果（None=未执行）。
        size_bytes: 对象估算大小（字节），None=未估算。
    """

    ok: bool
    name: str
    items: List[UnpicklableItem] = field(default_factory=list)
    roundtrip_skipped: bool = False
    roundtrip_ok: Optional[bool] = None
    size_bytes: Optional[int] = None

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------

    @property
    def message(self) -> str:
        """生成友好的人类可读摘要（含全部问题的路径与原因）。"""
        if self.ok:
            head = f"[通过] 对象 '{self.name}' 通过 pickle 序列化预检"
            if self.roundtrip_ok:
                head += "（已通过真实序列化往返测试）"
            elif self.roundtrip_skipped:
                head += "（静态扫描通过，未做真实序列化测试）"
            return head

        lines: List[str] = [
            f"[失败] 对象 '{self.name}' 未通过 pickle 序列化预检，"
            f"发现 {len(self.items)} 处问题：",
        ]
        for item in self.items:
            path_display = item.path
            if len(path_display) > MAX_PATH_LENGTH:
                path_display = path_display[:MAX_PATH_LENGTH] + "...(截断)"
            lines.append(f"  - 路径 {path_display} [{item.object_type}]: {item.reason}")
            if item.detail:
                detail_display = item.detail
                if len(detail_display) > 300:
                    detail_display = detail_display[:300] + "...(截断)"
                lines.append(f"      详情: {detail_display}")
        return "\n".join(lines)

    def summary(self) -> str:
        """与 message 等价，供打印摘要使用。"""
        return self.message

    def to_dict(self) -> Dict[str, Any]:
        """转为字典（供日志/报告使用）。"""
        return {
            "ok": self.ok,
            "name": self.name,
            "roundtrip_skipped": self.roundtrip_skipped,
            "roundtrip_ok": self.roundtrip_ok,
            "size_bytes": self.size_bytes,
            "items": [
                {
                    "path": i.path,
                    "object_type": i.object_type,
                    "reason": i.reason,
                    "detail": i.detail,
                }
                for i in self.items
            ],
        }

    def __repr__(self) -> str:
        return (
            f"PickleCheckResult(ok={self.ok}, name={self.name!r}, "
            f"issues={len(self.items)}, "
            f"roundtrip_skipped={self.roundtrip_skipped}, "
            f"roundtrip_ok={self.roundtrip_ok})"
        )


class PickleCheckError(Exception):
    """pickle 预检未通过时抛出的异常。

    Attributes:
        result: 关联的 PickleCheckResult，含完整问题定位。
    """

    def __init__(self, result: PickleCheckResult) -> None:
        self.result: PickleCheckResult = result
        super().__init__(result.message)

    def to_dict(self) -> Dict[str, Any]:
        """转字典，便于上层序列化记录。"""
        return self.result.to_dict()


# ============================================================================
# 叶子节点判断（不深入遍历的对象）
# ============================================================================

def _is_local_qualified(obj: Any) -> bool:
    """判断函数/类的 __qualname__ 是否位于局部作用域（不可 pickle 的特征）。"""
    qualname: str = getattr(obj, "__qualname__", "") or ""
    return "<locals>" in qualname


def _is_leaf(obj: Any) -> bool:
    """判断对象是否应作为叶子节点（可 pickle 且无需深入检查其内部）。

    覆盖：基本标量、numpy/pandas 数据对象、日期/路径/枚举、
    模块级顶层函数（按名称引用即可 pickle，无需深入函数体）。
    """
    if isinstance(obj, _PRIMITIVE_TYPES):
        return True
    obj_type = type(obj)
    # numpy 数组/标量、pandas 表格：内部元素海量，且自身可 pickle，作叶子处理
    if obj_type.__module__.startswith(_LEAF_MODULE_PREFIXES):
        return True
    if isinstance(obj, (datetime, date, time, timedelta, Decimal, Path, enum.Enum)):
        return True
    # 模块级顶层函数（非 lambda、非局部定义）：按模块名+限定名引用，可 pickle
    # 注意：types.LambdaType 与 types.FunctionType 是同一类型，判断 lambda
    # 必须依据函数对象的 __name__ == "<lambda>"，不能用 isinstance。
    if (
        isinstance(obj, types.FunctionType)
        and getattr(obj, "__name__", "") != "<lambda>"
        and not _is_local_qualified(obj)
    ):
        return True
    return False


# ============================================================================
# 静态启发式检测
# ============================================================================

def _detect_unpicklable(obj: Any) -> Optional[str]:
    """启发式检测对象是否结构性不可 pickle。

    Args:
        obj: 待检测对象。

    Returns:
        不可 pickle 的原因说明；None 表示结构上可接受，需进一步遍历或依赖往返测试。
    """
    obj_type = type(obj)

    # lambda 匿名函数：无名称可引用，永远不可 pickle。
    # 注意：types.LambdaType 与 types.FunctionType 是同一类型，必须依据
    # __name__ == "<lambda>" 判断，不能用 isinstance(obj, types.LambdaType)。
    if (
        isinstance(obj, types.FunctionType)
        and getattr(obj, "__name__", "") == "<lambda>"
    ):
        return "lambda 匿名函数无法被 pickle 序列化，请改用模块级具名函数"

    # 局部/嵌套函数：定义于函数体内，无法按名称引用
    if isinstance(obj, types.FunctionType) and _is_local_qualified(obj):
        return (
            f"局部/嵌套函数 '{getattr(obj, '__name__', '?')}' 无法被 pickle 序列化，"
            "请将其提升为模块级函数"
        )

    # 内置函数/方法（如 len、print 等）
    if isinstance(obj, (types.BuiltinFunctionType, types.BuiltinMethodType)):
        return "内置函数/方法通常无法被 pickle 序列化，请用模块级函数包装"

    # 生成器 / 协程 / 异步生成器：已开始执行的状态无法序列化
    if isinstance(
        obj,
        (types.GeneratorType, types.CoroutineType, types.AsyncGeneratorType),
    ):
        return "生成器/协程对象无法被 pickle 序列化（已持有执行状态），请改为传递可重放的数据"

    # 模块对象
    if isinstance(obj, types.ModuleType):
        return "模块对象无法被 pickle 序列化，请传递模块内的数据或模块级函数名"

    # 线程同步对象
    if isinstance(obj, (_thread.LockType, _thread.RLock)):
        return f"线程锁对象 {obj_type.__name__} 无法被 pickle 序列化，跨进程需重建"
    if isinstance(obj, _UNPICKLABLE_THREADING_TYPES):
        return f"线程同步对象 {obj_type.__name__} 无法被 pickle 序列化，跨进程需重建"
    if isinstance(obj, threading.local):
        return "threading.local 线程局部存储对象无法被 pickle 序列化"

    # 文件句柄 / 套接字
    if isinstance(obj, io.IOBase):
        return f"文件对象 {obj_type.__name__} 无法被 pickle 序列化，请传递文件路径而非句柄"
    if isinstance(obj, socket.socket):
        return "socket 套接字对象无法被 pickle 序列化"

    # 日志记录器
    if isinstance(obj, (logging.Logger, logging.LoggerAdapter)):
        return "logging.Logger 无法被 pickle 序列化，子进程内请用 logging.getLogger 重建"

    # multiprocessing 通信对象
    if isinstance(obj, _UNPICKLABLE_MULTIPROCESSING_TYPES):
        return f"multiprocessing 对象 {obj_type.__name__} 无法被 pickle 序列化，跨进程需重建"

    # 局部作用域内定义的类
    if isinstance(obj, type) and _is_local_qualified(obj):
        return (
            f"局部作用域内定义的类 '{getattr(obj, '__qualname__', '?')}' "
            "无法被 pickle 序列化，请提升为模块级类"
        )

    # 实例所属类定义于局部作用域
    if _is_local_qualified(obj_type):
        return (
            f"类 '{obj_type.__qualname__}' 定义于局部作用域，其实例无法被 pickle 序列化"
        )

    return None


# ============================================================================
# 子节点遍历
# ============================================================================

def _instance_children(obj: Any, path: str) -> Iterator[Tuple[str, Any]]:
    """遍历普通对象实例的属性（__dict__ + __slots__）。"""
    if hasattr(obj, "__dict__"):
        for name, value in vars(obj).items():
            yield f"{path}.{name}", value

    slots = getattr(type(obj), "__slots__", None)
    if isinstance(slots, (str, tuple, list)):
        slot_names: Tuple[Any, ...] = (slots,) if isinstance(slots, str) else tuple(slots)
        for name in slot_names:
            if not isinstance(name, str):
                continue
            try:
                value = getattr(obj, name, None)
                yield f"{path}.{name}", value
            except Exception as exc:  # 属性读取可能抛异常（描述符），跳过并记录
                logger.debug("读取对象属性失败: %s.%s -> %s", path, name, exc)


def _iter_children(obj: Any, path: str) -> Iterator[Tuple[str, Any]]:
    """迭代对象的所有直接子节点（带访问路径），用于递归扫描。

    覆盖 dict / list / tuple / set / frozenset / functools.partial /
    绑定方法（method）/ 普通实例（__dict__ + __slots__）。
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_path = f"{path}[{key!r}]"
            yield key_path, key
            yield key_path, value
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            yield f"{path}[{index}]", value
    elif isinstance(obj, (set, frozenset)):
        for index, value in enumerate(obj):
            yield f"{path}[{index}]", value
    elif isinstance(obj, functools.partial):
        # partial 自身无 __dict__，需显式遍历 func / args / keywords
        yield f"{path}.func", obj.func
        for index, arg in enumerate(obj.args):
            yield f"{path}.args[{index}]", arg
        if obj.keywords:
            for key, value in obj.keywords.items():
                yield f"{path}.keywords[{key!r}]", value
    elif isinstance(obj, types.MethodType):
        # 绑定方法：可序列化性取决于绑定实例
        yield f"{path}.__self__", obj.__self__
    else:
        yield from _instance_children(obj, path)


# ============================================================================
# 静态深度扫描
# ============================================================================

def _type_name(obj: Any) -> str:
    """返回对象的类型全名（module.QualName）。"""
    obj_type = type(obj)
    return f"{obj_type.__module__}.{obj_type.__qualname__}"


def _scan(
    obj: Any,
    path: str,
    depth: int,
    max_depth: int,
    max_items: int,
    seen: Set[int],
    results: List[UnpicklableItem],
) -> None:
    """递归扫描函数（内部使用）。

    Args:
        obj: 当前扫描的对象。
        path: 当前对象在整体中的访问路径。
        depth: 当前递归深度。
        max_depth: 最大深度。
        max_items: 最大问题报告数。
        seen: 已访问对象 id 集合（防循环引用死循环）。
        results: 收集结果列表。
    """
    # 达到问题数上限，提前终止
    if len(results) >= max_items:
        return

    # 深度保护
    if depth > max_depth:
        results.append(
            UnpicklableItem(
                path,
                _type_name(obj),
                f"超出最大扫描深度 {max_depth}，无法继续深入（可能存在过深嵌套）",
            )
        )
        return

    # 叶子节点直接放行（可 pickle，无需深入）
    if _is_leaf(obj):
        return

    # 循环引用保护
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)

    # 结构性不可 pickle 检测
    reason = _detect_unpicklable(obj)
    if reason is not None:
        results.append(UnpicklableItem(path, _type_name(obj), reason))
        return

    # 递归遍历子节点
    for child_path, child in _iter_children(obj, path):
        _scan(child, child_path, depth + 1, max_depth, max_items, seen, results)
        if len(results) >= max_items:
            results.append(
                UnpicklableItem(
                    "…",
                    "截断",
                    f"问题数量超过上限 {max_items}，剩余内容已截断",
                )
            )
            break


def scan_unpicklable(
    obj: Any,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> List[UnpicklableItem]:
    """静态深度扫描，定位对象中所有结构性不可 pickle 的问题。

    仅做启发式检测（不实际序列化），速度 O(节点数)，适合对大型对象做快速筛查。

    Args:
        obj: 待扫描对象。
        max_depth: 最大递归深度，默认 32。
        max_items: 最大问题报告数，默认 50，超出截断。

    Returns:
        不可 pickle 问题列表；为空表示静态扫描未发现结构性问题。

    Raises:
        ValueError: max_depth 或 max_items 非法。
    """
    if max_depth < 1:
        raise ValueError(f"max_depth 必须 >= 1，收到: {max_depth}")
    if max_items < 1:
        raise ValueError(f"max_items 必须 >= 1，收到: {max_items}")

    results: List[UnpicklableItem] = []
    seen: Set[int] = set()
    _scan(obj, "root", 0, max_depth, max_items, seen, results)
    return results


# ============================================================================
# 对象大小估算（用于决定是否执行真实往返测试）
# ============================================================================

def _leaf_sizeof(obj: Any) -> int:
    """估算叶子节点的大小（字节）。

    numpy 数组用 nbytes、pandas 对象用 memory_usage，其余用 sys.getsizeof。
    """
    if isinstance(obj, bytes):
        return len(obj)
    if hasattr(obj, "nbytes"):  # numpy.ndarray / numpy 标量
        try:
            return int(obj.nbytes)
        except Exception:
            pass
    if hasattr(obj, "memory_usage"):  # pandas.DataFrame / Series / Index
        try:
            return int(obj.memory_usage(deep=True).sum())
        except Exception:
            pass
    return int(sys.getsizeof(obj, 0))


def _total_sizeof(obj: Any, seen: Optional[Set[int]] = None) -> int:
    """递归估算对象的总大小（字节），含所有子节点（防循环引用）。

    Args:
        obj: 目标对象。
        seen: 已访问对象 id 集合（内部使用）。

    Returns:
        估算总字节数（近似值，仅用于大小阈值判断）。
    """
    if seen is None:
        seen = set()
    if _is_leaf(obj):
        return _leaf_sizeof(obj)

    obj_id = id(obj)
    if obj_id in seen:
        return 0
    seen.add(obj_id)

    total = int(sys.getsizeof(obj, 0))
    for _, child in _iter_children(obj, "root"):
        total += _total_sizeof(child, seen)
    return total


# ============================================================================
# 主入口
# ============================================================================

def check_picklable(
    obj: Any,
    *,
    name: str = "对象",
    strict: bool = True,
    max_test_bytes: int = DEFAULT_MAX_TEST_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> PickleCheckResult:
    """pickle 序列化预检主入口。

    两阶段检测：
      1. 静态扫描（快）：定位结构性不可 pickle 对象（lambda/局部函数/锁/Logger 等）；
      2. 真实往返测试（准）：静态通过后，对估算大小 <= max_test_bytes 的对象执行
         pickle.dumps + loads，捕获运行时序列化失败；超限对象明确标注 skipped。

    Args:
        obj: 待预检对象。
        name: 对象名称（用于错误提示），默认 "对象"。
        strict: 是否执行真实往返测试，默认 True。False 时仅做静态扫描。
        max_test_bytes: 往返测试的大小上限（字节），默认 64MB。
        max_depth: 静态扫描最大深度。
        max_items: 最大问题报告数。

    Returns:
        PickleCheckResult，包含 ok / items / roundtrip 信息。

    Raises:
        ValueError: 参数非法。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name 必须是非空字符串")
    if max_test_bytes <= 0:
        raise ValueError(f"max_test_bytes 必须 > 0，收到: {max_test_bytes}")
    if max_depth < 1:
        raise ValueError(f"max_depth 必须 >= 1，收到: {max_depth}")
    if max_items < 1:
        raise ValueError(f"max_items 必须 >= 1，收到: {max_items}")

    # ── 阶段1：静态扫描 ──
    items = scan_unpicklable(obj, max_depth=max_depth, max_items=max_items)
    if items:
        return PickleCheckResult(
            ok=False,
            name=name,
            items=items,
            roundtrip_skipped=False,
            roundtrip_ok=False,
        )

    # ── 阶段2：真实往返测试 ──
    if not strict:
        return PickleCheckResult(
            ok=True,
            name=name,
            items=[],
            roundtrip_skipped=True,
            roundtrip_ok=None,
        )

    size_bytes = _total_sizeof(obj)
    if size_bytes > max_test_bytes:
        logger.debug(
            "对象 '%s' 估算大小 %d 字节，超过往返测试上限 %d，跳过真实序列化",
            name, size_bytes, max_test_bytes,
        )
        return PickleCheckResult(
            ok=True,
            name=name,
            items=[],
            roundtrip_skipped=True,
            roundtrip_ok=None,
            size_bytes=size_bytes,
        )

    try:
        # 完整往返：dumps 验证可编码，loads 验证可恢复
        data = pickle.dumps(obj, protocol=PROTOCOL)
        pickle.loads(data)
        return PickleCheckResult(
            ok=True,
            name=name,
            items=[],
            roundtrip_skipped=False,
            roundtrip_ok=True,
            size_bytes=size_bytes,
        )
    except Exception as exc:
        logger.debug("对象 '%s' 真实序列化失败: %s", name, exc)
        return PickleCheckResult(
            ok=False,
            name=name,
            items=[
                UnpicklableItem(
                    "root",
                    _type_name(obj),
                    "pickle 真实序列化往返测试失败",
                    str(exc),
                )
            ],
            roundtrip_skipped=False,
            roundtrip_ok=False,
            size_bytes=size_bytes,
        )


def is_picklable(obj: Any, *, strict: bool = True) -> bool:
    """判断对象是否可被 pickle 序列化（布尔版）。

    Args:
        obj: 待判断对象。
        strict: 是否执行真实往返测试，默认 True。

    Returns:
        True=可序列化；False=不可序列化。
    """
    return check_picklable(obj, strict=strict).ok


def assert_picklable(
    obj: Any,
    *,
    name: str = "对象",
    strict: bool = True,
    max_test_bytes: int = DEFAULT_MAX_TEST_BYTES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> None:
    """pickle 预检断言：不通过时抛出 PickleCheckError。

    Args:
        obj: 待预检对象。
        name: 对象名称（用于错误提示）。
        strict: 是否执行真实往返测试。
        max_test_bytes: 往返测试大小上限。
        max_depth: 静态扫描最大深度。
        max_items: 最大问题报告数。

    Raises:
        PickleCheckError: 预检未通过时抛出，携带完整问题定位。
    """
    result = check_picklable(
        obj,
        name=name,
        strict=strict,
        max_test_bytes=max_test_bytes,
        max_depth=max_depth,
        max_items=max_items,
    )
    if not result.ok:
        logger.error("pickle 预检失败: %s", result.message)
        raise PickleCheckError(result)


@contextmanager
def pickle_guard(
    *objs: Any,
    name: str = "任务参数",
    strict: bool = True,
    max_test_bytes: int = DEFAULT_MAX_TEST_BYTES,
):
    """pickle 预检上下文管理器：进入代码块前批量预检所有对象。

    Args:
        *objs: 需要预检的对象（至少一个）。
        name: 这批对象的名称前缀，用于错误提示。
        strict: 是否执行真实往返测试。
        max_test_bytes: 往返测试大小上限。

    Yields:
        原对象元组（预检通过后）。

    Raises:
        PickleCheckError: 任一对象预检未通过。
        ValueError: 未传入任何对象。

    Example:
        >>> with pickle_guard(config, params, name="批量扫描任务"):
        ...     pool.submit(run_scan, config, params)
    """
    if not objs:
        raise ValueError("pickle_guard 至少需要一个预检对象")

    for index, obj in enumerate(objs):
        assert_picklable(
            obj,
            name=f"{name}[{index}]",
            strict=strict,
            max_test_bytes=max_test_bytes,
        )
    yield objs


# ============================================================================
# 进程池任务专项预检
# ============================================================================

def precheck_task(
    fn: Any,
    args: Any = (),
    kwargs: Optional[Dict[str, Any]] = None,
    *,
    name: str = "任务",
) -> PickleCheckResult:
    """对进程池任务（函数 + 位置参数 + 关键字参数）做整体 pickle 预检。

    Args:
        fn: 任务函数（模块级顶层函数可 pickle；lambda/局部函数不可）。
        args: 位置参数，list 或 tuple。
        kwargs: 关键字参数，dict 或 None。
        name: 任务名称（用于错误提示）。

    Returns:
        PickleCheckResult，聚合了函数与全部参数的检测结果。

    Raises:
        TypeError: fn 不可调用 / args 类型非法 / kwargs 类型非法。
    """
    if not callable(fn):
        raise TypeError(f"任务函数 fn 必须可调用，收到: {type(fn).__name__}")
    if not isinstance(args, (list, tuple)):
        raise TypeError(f"args 必须为 list 或 tuple，收到: {type(args).__name__}")
    if kwargs is None:
        kwargs = {}
    if not isinstance(kwargs, dict):
        raise TypeError(f"kwargs 必须为 dict 或 None，收到: {type(kwargs).__name__}")

    # 组装待检对象：函数 + 每个位置参数 + 每个关键字参数
    parts: List[Tuple[str, Any]] = [("任务函数(fn)", fn)]
    parts.extend((f"位置参数 args[{index}]", arg) for index, arg in enumerate(args))
    parts.extend((f"关键字参数 {key!r}", value) for key, value in kwargs.items())

    all_ok = True
    all_items: List[UnpicklableItem] = []
    any_roundtrip = False
    for label, part_obj in parts:
        result = check_picklable(part_obj, name=label)
        all_ok = all_ok and result.ok
        # 把每个组成部分的路径前缀 "root" 替换为组成部分标签，
        # 使定位信息保留"是哪个参数、哪一层嵌套"的上下文
        for item in result.items:
            if item.path == "root":
                item.path = label
            elif item.path.startswith("root"):
                item.path = label + item.path[len("root"):]
        all_items.extend(result.items)
        if not result.roundtrip_skipped:
            any_roundtrip = True

    return PickleCheckResult(
        ok=all_ok,
        name=name,
        items=all_items,
        roundtrip_skipped=not any_roundtrip,
        roundtrip_ok=(all_ok if any_roundtrip else None),
    )


def assert_task_picklable(
    fn: Any,
    args: Any = (),
    kwargs: Optional[Dict[str, Any]] = None,
    *,
    name: str = "任务",
) -> None:
    """对进程池任务做 pickle 预检断言，不通过时抛出 PickleCheckError。

    Args:
        fn: 任务函数。
        args: 位置参数。
        kwargs: 关键字参数。
        name: 任务名称。

    Raises:
        PickleCheckError: 预检未通过时抛出。
        TypeError: 参数类型非法。
    """
    result = precheck_task(fn, args=args, kwargs=kwargs, name=name)
    if not result.ok:
        logger.error("任务 pickle 预检失败: %s", result.message)
        raise PickleCheckError(result)


# ============================================================================
# 自测 Demo
# ============================================================================

def _demo_top_level_task(a: int, b: int = 0) -> int:
    """自测用模块级顶层函数（应可被 pickle 按名引用）。"""
    return a + b


def _demo_nested_task() -> int:
    """自测用模块级函数，内部定义局部函数。"""
    def inner() -> int:
        return 1
    return inner()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print("=" * 64)
    print("  pickle_checker.py — pickle 序列化预检工具 自测 Demo")
    print("=" * 64)

    # 自测用临时可导入模块：直接以脚本方式运行时本模块名为 __main__，
    # 而 pickle 往返测试需要对模块级函数按"模块名+限定名"import，
    # 因此构造一个注册到 sys.modules 的临时模块来承载待测函数。
    _test_mod = types.ModuleType("myquant.utils._pickle_test_mod")

    def _test_task(a: int, b: int = 0) -> int:
        """临时模块内的顶层函数，用于自测 pickle 往返。"""
        return a + b

    _test_task.__module__ = "myquant.utils._pickle_test_mod"
    _test_mod._test_task = _test_task
    sys.modules["myquant.utils._pickle_test_mod"] = _test_mod

    passed = 0
    total = 0

    def _expect_ok(actual: bool, label: str) -> None:
        global passed, total
        total += 1
        status = "✓" if actual else "✗"
        print(f"    {status} {label}")
        if actual:
            passed += 1

    def _expect_fail(actual: bool, label: str) -> None:
        global passed, total
        total += 1
        status = "✓" if not actual else "✗"
        print(f"    {status} {label}")
        if not actual:
            passed += 1

    # ── 测试 1: 基本类型全部可 pickle ──
    print("\n[1] 基本类型预检")
    basic_objs = [
        None, True, 42, 3.14, "hello", b"bytes",
        [1, 2, 3], {"a": 1, "b": 2}, (1, 2), {1, 2, 3},
        {"nested": {"list": [1, 2, {"deep": True}]}},
    ]
    for obj in basic_objs:
        _expect_ok(is_picklable(obj), f"可 pickle: {type(obj).__name__}")

    # ── 测试 2: lambda 不可 pickle，且能定位路径 ──
    print("\n[2] lambda 检测")
    result = check_picklable(lambda x: x + 1, name="lambda_test")
    _expect_fail(result.ok, "lambda 不可 pickle")
    assert any("lambda" in i.reason for i in result.items), "应识别为 lambda"
    print(f"      原因: {result.items[0].reason}")

    # ── 测试 3: 嵌套容器中的 lambda 定位到具体路径 ──
    print("\n[3] 嵌套定位")
    nested_obj = {"config": {"handler": None}, "callbacks": [lambda x: x, _test_task]}
    result = check_picklable(nested_obj, name="嵌套对象")
    _expect_fail(result.ok, "嵌套 lambda 不可 pickle")
    paths = [i.path for i in result.items]
    _expect_ok(any("callbacks'][0]" in p for p in paths), "路径定位到 root['callbacks'][0]")
    print(f"      路径: {paths}")

    # ── 测试 4: 局部函数不可 pickle，模块级函数可 ──
    print("\n[4] 函数检测")
    _expect_fail(is_picklable(lambda: None), "lambda 不可 pickle")
    # 局部函数（定义在另一个函数体内部）
    def outer() -> Any:
        def inner() -> int:
            return 1
        return inner
    _expect_fail(is_picklable(outer()), "局部函数不可 pickle")
    _expect_ok(is_picklable(_test_task), "模块级顶层函数可 pickle")

    # ── 测试 5: 线程同步对象 / Logger / 模块 / 生成器 ──
    print("\n[5] 常见不可 pickle 对象")
    _expect_fail(is_picklable(threading.Lock()), "threading.Lock 不可 pickle")
    _expect_fail(is_picklable(threading.RLock()), "threading.RLock 不可 pickle")
    _expect_fail(is_picklable(threading.Event()), "threading.Event 不可 pickle")
    _expect_fail(is_picklable(logging.getLogger("demo")), "logging.Logger 不可 pickle")
    _expect_fail(is_picklable((i for i in range(3))), "生成器不可 pickle")
    _expect_fail(is_picklable(sys), "模块对象不可 pickle")

    # ── 测试 6: 自定义类实例 ──
    print("\n[6] 自定义类实例")
    @dataclasses.dataclass
    class _SimpleConfig:
        symbols: List[str]
        threshold: float = 0.5

    _expect_ok(is_picklable(_SimpleConfig(["600519.SH"], 0.6)), "简单 dataclass 实例可 pickle")

    class _WithLock:
        def __init__(self, name: str) -> None:
            self.name = name
            self.lock = threading.Lock()

    result = check_picklable(_WithLock("x"), name="含锁实例")
    _expect_fail(result.ok, "含 Lock 属性的实例不可 pickle")
    _expect_ok(any("lock" in i.path for i in result.items), "路径定位到 root.lock")
    print(f"      路径: {[i.path for i in result.items]}")

    # ── 测试 7: 局部类实例 ──
    print("\n[7] 局部类检测")
    def make_local_instance() -> Any:
        class _Local:
            pass
        return _Local()
    _expect_fail(is_picklable(make_local_instance()), "局部类实例不可 pickle")

    # ── 测试 8: functools.partial ──
    print("\n[8] functools.partial 检测")
    _expect_ok(is_picklable(functools.partial(_test_task, 1)), "partial 包顶层函数可 pickle")
    _expect_fail(is_picklable(functools.partial(lambda x: x, 1)), "partial 包 lambda 不可 pickle")

    # ── 测试 9: 循环引用不卡死 ──
    print("\n[9] 循环引用防护")
    cycle: List[Any] = []
    cycle.append(cycle)
    _expect_ok(is_picklable(cycle), "循环引用列表可 pickle 且不卡死")

    # ── 测试 10: numpy / pandas（若可用） ──
    print("\n[10] numpy / pandas 大对象")
    try:
        import numpy as np
        arr = np.zeros((1000, 10), dtype=np.float64)
        _expect_ok(is_picklable(arr), "numpy 数组可 pickle（作为叶子快速通过）")
    except ImportError:
        print("    (numpy 未安装，跳过)")
    try:
        import pandas as pd
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})
        _expect_ok(is_picklable(df), "pandas DataFrame 可 pickle（作为叶子快速通过）")
    except ImportError:
        print("    (pandas 未安装，跳过)")

    # ── 测试 11: strict=False 仅静态扫描 ──
    print("\n[11] 非严格模式")
    result = check_picklable({"x": [1, 2, 3]}, name="非严格", strict=False)
    _expect_ok(result.ok, "非严格模式静态扫描通过")
    _expect_ok(result.roundtrip_skipped, "非严格模式跳过往返测试")

    # ── 测试 12: assert_picklable 抛异常 ──
    print("\n[12] 断言与异常")
    try:
        assert_picklable({"fn": lambda x: x}, name="异常测试")
        _expect_fail(True, "assert_picklable 应抛出 PickleCheckError")
    except PickleCheckError as exc:
        _expect_ok(True, "assert_picklable 正确抛出 PickleCheckError")
        _expect_ok("异常测试" in str(exc), "异常信息包含对象名称")
        _expect_ok(isinstance(exc.result, PickleCheckResult), "异常携带结果对象")
        print(f"      异常摘要首行: {str(exc).splitlines()[0]}")

    # assert_picklable 通过场景
    assert_picklable({"symbols": ["600519.SH"]}, name="通过场景")
    _expect_ok(True, "assert_picklable 正常对象不抛异常")

    # ── 测试 13: pickle_guard 上下文 ──
    print("\n[13] pickle_guard 上下文管理器")
    with pickle_guard({"a": 1}, [1, 2, 3], name="批量参数"):
        _expect_ok(True, "pickle_guard 通过场景正常")
    try:
        with pickle_guard({"bad": lambda: None}, name="批量参数"):
            _expect_fail(True, "pickle_guard 应拦截不可 pickle 对象")
    except PickleCheckError:
        _expect_ok(True, "pickle_guard 正确拦截")
    try:
        with pickle_guard(name="空参数"):
            _expect_fail(True, "空参数应抛 ValueError")
    except ValueError:
        _expect_ok(True, "pickle_guard 空参数正确抛 ValueError")

    # ── 测试 14: precheck_task / assert_task_picklable ──
    print("\n[14] 进程池任务专项预检")
    result = precheck_task(_test_task, args=(1, 2), kwargs={"b": 3}, name="任务1")
    _expect_ok(result.ok, "纯函数任务预检通过")

    result = precheck_task(_test_task, args=(1, lambda x: x), name="任务2")
    _expect_fail(result.ok, "含 lambda 参数的任务预检失败")
    _expect_ok(any("args[1]" in i.path for i in result.items), "路径定位到 args[1]")
    print(f"      失败路径: {[i.path for i in result.items]}")

    try:
        assert_task_picklable(lambda x: x, args=(), name="非法任务")
        _expect_fail(True, "assert_task_picklable 应抛异常")
    except PickleCheckError:
        _expect_ok(True, "assert_task_picklable 正确抛异常")

    # ── 测试 15: 参数校验 ──
    print("\n[15] 参数校验")
    try:
        check_picklable([1], name="", )
        _expect_fail(True, "空 name 应抛 ValueError")
    except ValueError:
        _expect_ok(True, "空 name 正确抛 ValueError")
    try:
        precheck_task("not_callable", args=())
        _expect_fail(True, "不可调用 fn 应抛 TypeError")
    except TypeError:
        _expect_ok(True, "不可调用 fn 正确抛 TypeError")
    try:
        precheck_task(_test_task, args="not_a_list")
        _expect_fail(True, "非法 args 应抛 TypeError")
    except TypeError:
        _expect_ok(True, "非法 args 正确抛 TypeError")

    # ── 测试 16: 结果对象 to_dict / message ──
    print("\n[16] 结果对象")
    result = check_picklable({"cb": lambda: 1}, name="报告")
    d = result.to_dict()
    _expect_ok(d["ok"] is False and len(d["items"]) == 1, "to_dict 结构正确")
    msg = result.message
    _expect_ok("报告" in msg and "root['cb']" in msg, "message 含对象名与路径")
    print(f"      摘要: {msg.splitlines()[0]}")

    print("\n" + "=" * 64)
    print(f"  自测结果: {passed}/{total} 项通过")
    if passed == total:
        print("  ✓ pickle_checker.py 全部自测通过")
    else:
        print(f"  ✗ 有 {total - passed} 项未通过，请检查！")
    print("=" * 64)
