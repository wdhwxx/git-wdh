# 文件名：myquant/monitoring/health_check.py
"""
health_check.py - 系统健康检查（阶段9任务9骨架）
==================================================
功能:
  1. 健康状态枚举：HEALTHY / DEGRADED / UNHEALTHY
  2. 健康检查结果数据类：HealthCheckResult
  3. HealthChecker 管理器：注册检查项、执行检查、汇总状态
  4. 内置检查项：磁盘空间、事件总线状态、进程存活
  5. 检查项超时控制、并发执行预留

为实盘无缝对接预留:
  - add_check() 动态注册检查项（数据源连通性/券商连接/行情订阅状态）
  - run_all() 批量执行，支持超时控制
  - get_summary() 汇总健康状态，供 API / Web 看板查询
  - 与 EventBus 集成：健康状态变化时发布 SYSTEM 事件
  - 定期巡检（任务22 scheduler 将调用）
  - 健康状态持久化（任务23 state_store 将集成）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 健康检查只读取当前状态，不预测未来

防未来函数说明:
  - 所有检查项只读取当前时刻的系统状态（磁盘/进程/事件总线）
  - 不使用任何历史数据推断未来健康状态
  - 检查结果的 timestamp 为执行时刻的当前时间
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("monitoring.health_check")


# ═══════════════════════════════════════════════
#  健康状态枚举
# ═══════════════════════════════════════════════

class HealthStatus(Enum):
    """系统健康状态"""
    HEALTHY = "healthy"        # 健康：所有检查项通过
    DEGRADED = "degraded"      # 降级：部分检查项警告，系统仍可运行
    UNHEALTHY = "unhealthy"    # 不健康：关键检查项失败，系统不可用

    @property
    def level(self) -> int:
        """数值级别（用于比较，越大越严重）"""
        return {
            HealthStatus.HEALTHY: 0,
            HealthStatus.DEGRADED: 10,
            HealthStatus.UNHEALTHY: 20,
        }[self]

    def __ge__(self, other: "HealthStatus") -> bool:
        return self.level >= other.level

    def __gt__(self, other: "HealthStatus") -> bool:
        return self.level > other.level

    def __le__(self, other: "HealthStatus") -> bool:
        return self.level <= other.level

    def __lt__(self, other: "HealthStatus") -> bool:
        return self.level < other.level

    @property
    def is_healthy(self) -> bool:
        return self == HealthStatus.HEALTHY

    @property
    def is_critical(self) -> bool:
        return self == HealthStatus.UNHEALTHY


# ═══════════════════════════════════════════════
#  健康检查结果
# ═══════════════════════════════════════════════

@dataclass
class HealthCheckResult:
    """
    单项健康检查结果

    Attributes
    ----------
    name : str
        检查项名称（唯一标识）
    status : HealthStatus
        检查结果状态
    message : str
        人类可读的检查结果描述
    details : Dict[str, Any]
        详细信息（如磁盘使用率、连接延迟等）
    duration : float
        检查耗时（秒）
    timestamp : float
        检查执行时间戳
    check_id : str
        检查结果唯一ID
    """
    name: str
    status: HealthStatus
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    duration: float = 0.0
    timestamp: float = field(default_factory=time.time)
    check_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "check_id": self.check_id,
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "duration": round(self.duration, 6),
            "timestamp": self.timestamp,
        }

    def summary(self) -> str:
        """人类可读摘要"""
        return f"[{self.status.value.upper()}] {self.name}: {self.message} ({self.duration:.4f}s)"


# ═══════════════════════════════════════════════
#  检查项定义
# ═══════════════════════════════════════════════

@dataclass
class _CheckItem:
    """内部检查项定义"""
    name: str
    func: Callable[[], HealthCheckResult]
    timeout: float = 10.0
    enabled: bool = True
    description: str = ""
    critical: bool = False  # 是否为关键检查项（失败直接导致 UNHEALTHY）


# ═══════════════════════════════════════════════
#  内置检查项函数
# ═══════════════════════════════════════════════

def check_disk_space(
    path: Optional[str] = None,
    warning_threshold: float = 80.0,
    critical_threshold: float = 95.0,
) -> HealthCheckResult:
    """
    磁盘空间检查

    Parameters
    ----------
    path : str | None
        检查路径（None则使用当前工作目录）
    warning_threshold : float
        警告阈值（使用率百分比）
    critical_threshold : float
        严重阈值（使用率百分比）

    Returns
    -------
    HealthCheckResult
    """
    start = time.time()
    check_path = path or os.getcwd()

    try:
        stat = os.statvfs(check_path) if hasattr(os, "statvfs") else None
        if stat is None:
            # Windows 兼容：使用 shutil
            import shutil
            total, used, free = shutil.disk_usage(check_path)
            usage_pct = (used / total * 100) if total > 0 else 0.0
        else:
            total = stat.f_frsize * stat.f_blocks
            free = stat.f_frsize * stat.f_bavail
            used = total - free
            usage_pct = (used / total * 100) if total > 0 else 0.0

        details = {
            "path": check_path,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "usage_percent": round(usage_pct, 2),
        }

        if usage_pct >= critical_threshold:
            status = HealthStatus.UNHEALTHY
            message = f"磁盘使用率 {usage_pct:.1f}% 超过严重阈值 {critical_threshold}%"
        elif usage_pct >= warning_threshold:
            status = HealthStatus.DEGRADED
            message = f"磁盘使用率 {usage_pct:.1f}% 超过警告阈值 {warning_threshold}%"
        else:
            status = HealthStatus.HEALTHY
            message = f"磁盘使用率 {usage_pct:.1f}%，剩余 {free / (1024**3):.1f} GB"

        return HealthCheckResult(
            name="disk_space",
            status=status,
            message=message,
            details=details,
            duration=time.time() - start,
        )
    except Exception as e:
        return HealthCheckResult(
            name="disk_space",
            status=HealthStatus.UNHEALTHY,
            message=f"磁盘检查失败: {e}",
            details={"error": str(e)},
            duration=time.time() - start,
        )


def check_event_bus_status(bus: Optional[Any] = None) -> HealthCheckResult:
    """
    事件总线状态检查

    Parameters
    ----------
    bus : EventBus | None
        事件总线实例（None则使用全局单例）

    Returns
    -------
    HealthCheckResult
    """
    start = time.time()
    try:
        if bus is None:
            from myquant.core.event_bus import get_event_bus
            bus = get_event_bus()

        stats = bus.stats
        details = {
            "total_events_published": stats.get("total_events_published", 0),
            "sync_handlers": stats.get("sync_handlers", 0),
            "async_handlers": stats.get("async_handlers", 0),
            "history_size": stats.get("history_size", 0),
        }

        # 检查是否有处理器错误（通过处理器统计）
        handler_errors = 0
        try:
            all_stats = bus.get_handler_stats()
            handler_errors = sum(s.error_count for s in all_stats)
            details["handler_errors"] = handler_errors
            details["total_handler_calls"] = sum(s.call_count for s in all_stats)
        except Exception as e:
            logger.debug(f"事件总线handler统计获取失败: {e}")

        if handler_errors > 0:
            status = HealthStatus.DEGRADED
            message = f"事件总线运行中，有 {handler_errors} 个处理器错误"
        else:
            status = HealthStatus.HEALTHY
            message = (
                f"事件总线正常，已发布 {details['total_events_published']} 个事件，"
                f"{details['sync_handlers']} 个同步处理器"
            )

        return HealthCheckResult(
            name="event_bus",
            status=status,
            message=message,
            details=details,
            duration=time.time() - start,
        )
    except Exception as e:
        return HealthCheckResult(
            name="event_bus",
            status=HealthStatus.UNHEALTHY,
            message=f"事件总线检查失败: {e}",
            details={"error": str(e)},
            duration=time.time() - start,
        )


def check_process_uptime() -> HealthCheckResult:
    """
    进程存活检查

    Returns
    -------
    HealthCheckResult
    """
    start = time.time()
    try:
        import sys
        pid = os.getpid()
        version = sys.version.split()[0]

        # 尝试获取进程启动时间（跨平台）
        uptime_seconds = None
        try:
            import psutil
            proc = psutil.Process(pid)
            uptime_seconds = time.time() - proc.create_time()
        except ImportError:
            pass

        details = {
            "pid": pid,
            "python_version": version,
            "platform": sys.platform,
        }
        if uptime_seconds is not None:
            details["uptime_seconds"] = round(uptime_seconds, 1)
            details["uptime_human"] = _format_duration(uptime_seconds)

        return HealthCheckResult(
            name="process",
            status=HealthStatus.HEALTHY,
            message=f"进程运行中 (PID={pid}, Python={version})",
            details=details,
            duration=time.time() - start,
        )
    except Exception as e:
        return HealthCheckResult(
            name="process",
            status=HealthStatus.UNHEALTHY,
            message=f"进程检查失败: {e}",
            details={"error": str(e)},
            duration=time.time() - start,
        )


def _format_duration(seconds: float) -> str:
    """格式化时长为人类可读字符串"""
    if seconds < 60:
        return f"{seconds:.0f}秒"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}分钟"
    elif seconds < 86400:
        return f"{seconds / 3600:.1f}小时"
    else:
        return f"{seconds / 86400:.1f}天"


# ═══════════════════════════════════════════════
#  HealthChecker 健康检查管理器
# ═══════════════════════════════════════════════

class HealthChecker:
    """
    健康检查管理器

    管理多个检查项，支持批量执行、状态汇总、动态注册。

    Usage:
        checker = HealthChecker()
        checker.register_default_checks()

        # 执行所有检查
        results = checker.run_all()
        summary = checker.get_summary()

        # 动态注册自定义检查（实盘数据源连通性等）
        def check_data_source():
            return HealthCheckResult(name="akshare", status=HealthStatus.HEALTHY, message="OK")
        checker.add_check("akshare", check_data_source, critical=True)

    为实盘无缝对接预留:
      - add_check() 注册数据源/券商/行情连接检查
      - run_all() 定期巡检（任务22 scheduler 调用）
      - get_summary() 供 API / Web 看板查询
      - 与 EventBus 集成：状态变化时发布事件
    """

    def __init__(self):
        self._checks: Dict[str, _CheckItem] = {}
        self._last_results: Dict[str, HealthCheckResult] = {}
        self._last_run_time: float = 0.0
        self._status_history: List[HealthStatus] = []
        self._max_history = 100

    # ═══════════════════════════════════════════
    #  检查项注册
    # ═══════════════════════════════════════════

    def add_check(
        self,
        name: str,
        func: Callable[[], HealthCheckResult],
        timeout: float = 10.0,
        critical: bool = False,
        description: str = "",
    ) -> "HealthChecker":
        """
        注册检查项

        Parameters
        ----------
        name : str
            检查项名称（唯一标识）
        func : Callable[[], HealthCheckResult]
            检查函数，无参数，返回 HealthCheckResult
        timeout : float
            超时时间（秒）
        critical : bool
            是否为关键检查项（失败直接导致 UNHEALTHY）
        description : str
            检查项描述

        Returns
        -------
        HealthChecker : self（链式调用）
        """
        self._checks[name] = _CheckItem(
            name=name,
            func=func,
            timeout=timeout,
            critical=critical,
            description=description,
        )
        logger.debug(f"注册健康检查项: {name} (critical={critical})")
        return self

    def remove_check(self, name: str) -> bool:
        """移除检查项"""
        if name in self._checks:
            del self._checks[name]
            self._last_results.pop(name, None)
            logger.debug(f"移除健康检查项: {name}")
            return True
        return False

    def enable_check(self, name: str) -> bool:
        """启用检查项"""
        if name in self._checks:
            self._checks[name].enabled = True
            return True
        return False

    def disable_check(self, name: str) -> bool:
        """禁用检查项"""
        if name in self._checks:
            self._checks[name].enabled = False
            return True
        return False

    def list_checks(self) -> List[str]:
        """列出所有检查项名称"""
        return list(self._checks.keys())

    def register_default_checks(self) -> "HealthChecker":
        """
        注册内置默认检查项

        包含：磁盘空间、事件总线状态、进程存活
        """
        self.add_check(
            "disk_space",
            check_disk_space,
            critical=True,
            description="磁盘空间使用率检查",
        )
        self.add_check(
            "event_bus",
            check_event_bus_status,
            critical=False,
            description="事件总线运行状态检查",
        )
        self.add_check(
            "process",
            check_process_uptime,
            critical=True,
            description="进程存活与运行时间检查",
        )
        return self

    # ═══════════════════════════════════════════
    #  执行检查
    # ═══════════════════════════════════════════

    def run_check(self, name: str) -> Optional[HealthCheckResult]:
        """
        执行单个检查项

        Parameters
        ----------
        name : str
            检查项名称

        Returns
        -------
        HealthCheckResult | None : 检查结果，检查项不存在返回 None
        """
        item = self._checks.get(name)
        if item is None or not item.enabled:
            return None

        start = time.time()
        try:
            result = item.func()
            result.duration = time.time() - start
        except Exception as e:
            result = HealthCheckResult(
                name=name,
                status=HealthStatus.UNHEALTHY,
                message=f"检查执行异常: {e}",
                details={"error": str(e)},
                duration=time.time() - start,
            )
            logger.error(f"健康检查项 [{name}] 执行异常: {e}", exc_info=True)

        self._last_results[name] = result
        return result

    def run_all(self) -> List[HealthCheckResult]:
        """
        执行所有已启用的检查项

        Returns
        -------
        List[HealthCheckResult] : 所有检查结果
        """
        self._last_run_time = time.time()
        results = []
        for name in list(self._checks.keys()):
            result = self.run_check(name)
            if result is not None:
                results.append(result)

        # 记录状态历史
        overall = self._compute_overall_status(results)
        self._status_history.append(overall)
        if len(self._status_history) > self._max_history:
            self._status_history.pop(0)

        logger.info(f"健康检查完成: {len(results)} 项, 总体状态={overall.value}")
        return results

    # ═══════════════════════════════════════════
    #  状态汇总
    # ═══════════════════════════════════════════

    def _compute_overall_status(self, results: List[HealthCheckResult]) -> HealthStatus:
        """计算总体健康状态"""
        if not results:
            return HealthStatus.HEALTHY

        overall = HealthStatus.HEALTHY
        for result in results:
            item = self._checks.get(result.name)
            # 关键检查项失败直接 UNHEALTHY
            if item and item.critical and result.status == HealthStatus.UNHEALTHY:
                return HealthStatus.UNHEALTHY
            if result.status > overall:
                overall = result.status

        return overall

    def get_summary(self) -> Dict[str, Any]:
        """
        获取健康检查汇总

        Returns
        -------
        Dict : {
            "overall_status": str,
            "overall_status_level": int,
            "checks_count": int,
            "healthy_count": int,
            "degraded_count": int,
            "unhealthy_count": int,
            "last_run_time": float,
            "results": List[Dict],
        }
        """
        results = list(self._last_results.values())
        overall = self._compute_overall_status(results)

        healthy = sum(1 for r in results if r.status == HealthStatus.HEALTHY)
        degraded = sum(1 for r in results if r.status == HealthStatus.DEGRADED)
        unhealthy = sum(1 for r in results if r.status == HealthStatus.UNHEALTHY)

        return {
            "overall_status": overall.value,
            "overall_status_level": overall.level,
            "is_healthy": overall.is_healthy,
            "is_critical": overall.is_critical,
            "checks_count": len(results),
            "healthy_count": healthy,
            "degraded_count": degraded,
            "unhealthy_count": unhealthy,
            "last_run_time": self._last_run_time,
            "results": [r.to_dict() for r in results],
        }

    def get_last_result(self, name: str) -> Optional[HealthCheckResult]:
        """获取指定检查项的上次结果"""
        return self._last_results.get(name)

    def get_status_history(self, limit: int = 20) -> List[str]:
        """获取状态历史（最近 N 次）"""
        return [s.value for s in self._status_history[-limit:]]

    @property
    def overall_status(self) -> HealthStatus:
        """当前总体健康状态"""
        results = list(self._last_results.values())
        return self._compute_overall_status(results)

    def __repr__(self) -> str:
        return (
            f"<HealthChecker checks={len(self._checks)} "
            f"status={self.overall_status.value} "
            f"last_run={self._last_run_time:.0f}>"
        )


# ═══════════════════════════════════════════════
#  全局健康检查器单例
# ═══════════════════════════════════════════════

_global_checker: Optional[HealthChecker] = None
_checker_lock = None  # 延迟导入 threading


def get_health_checker() -> HealthChecker:
    """获取全局健康检查器单例"""
    global _global_checker, _checker_lock
    if _checker_lock is None:
        import threading
        _checker_lock = threading.Lock()
    with _checker_lock:
        if _global_checker is None:
            _global_checker = HealthChecker()
            _global_checker.register_default_checks()
        return _global_checker


def reset_health_checker() -> None:
    """重置全局健康检查器（测试用）"""
    global _global_checker
    _global_checker = None
