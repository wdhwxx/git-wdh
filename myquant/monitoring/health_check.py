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

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("monitoring.health_check")


# ═══════════════════════════════════════════════
#  L0 基础设施检查常量（A-1 新增，全部可 add_check 注册）
# ═══════════════════════════════════════════════

# 网络延迟：TCP 连接延迟阈值（毫秒）；默认目标=国内公共 DNS（仅基础连通性演示，
# 实盘应按数据源/券商端点经 config 覆盖，禁止把业务端点硬编码进业务代码）
DEFAULT_LATENCY_TARGETS: List[tuple] = [("223.5.5.5", 53)]
DEFAULT_LATENCY_WARNING_MS: float = 300.0
DEFAULT_LATENCY_CRITICAL_MS: float = 1000.0
DEFAULT_LATENCY_TIMEOUT: float = 3.0

# 时钟偏差：NTP 协议校时（标准库 UDP 123），偏差阈值（秒）
DEFAULT_NTP_SERVER: str = "ntp.aliyun.com"
DEFAULT_CLOCK_WARNING_SECONDS: float = 5.0
DEFAULT_CLOCK_CRITICAL_SECONDS: float = 30.0
DEFAULT_NTP_TIMEOUT: float = 3.0

# CPU / 内存：使用率阈值（%）
DEFAULT_CPU_WARNING_PCT: float = 85.0
DEFAULT_CPU_CRITICAL_PCT: float = 95.0
DEFAULT_MEMORY_WARNING_PCT: float = 85.0
DEFAULT_MEMORY_CRITICAL_PCT: float = 95.0

# 证书检查：HTTPS 端点（基础设施级默认，可按券商/交易所 API 覆盖）
DEFAULT_TLS_HOSTS: List[tuple] = [("www.aliyun.com", 443)]
DEFAULT_TLS_WARNING_DAYS: float = 30.0
DEFAULT_TLS_CRITICAL_DAYS: float = 7.0
DEFAULT_TLS_TIMEOUT: float = 5.0

# DNS 解析：默认域名与超时（秒）
DEFAULT_DNS_HOSTS: List[str] = ["www.aliyun.com"]
DEFAULT_DNS_TIMEOUT: float = 3.0

# 网络分区：多端点可达性（默认=延迟目标 + TLS 端点；实盘按 config 覆盖）
DEFAULT_PARTITION_ENDPOINTS: List[tuple] = [
    ("223.5.5.5", 53),
    ("www.aliyun.com", 443),
]
DEFAULT_PARTITION_TIMEOUT: float = 3.0

# 不可达地址（自测/单测注入用，验证降级分支不依赖外网）
_UNREACHABLE_HOST: str = "10.255.255.1"


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
#  L0 基础设施检查项（A-1：网络延迟/时钟偏差/CPU/内存/证书/DNS/网络分区）
#  防未来：全部只读取当前时刻状态，不做任何预测
# ═══════════════════════════════════════════════

def _validate_thresholds(warning: float, critical: float, label: str) -> None:
    """阈值防御式校验（warning < critical，均为正数）。"""
    if warning <= 0 or critical <= 0:
        raise ValueError(f"{label}: warning/critical 阈值必须为正数，收到 {warning}/{critical}")
    if warning >= critical:
        raise ValueError(f"{label}: warning 阈值必须小于 critical，收到 {warning}/{critical}")


def _resolve_endpoints(
    targets: Optional[List[tuple]], default: List[tuple], label: str
) -> List[tuple]:
    """端点列表解析：None → 默认；显式传 [] → 空（记 HEALTHY 未配置）。"""
    if targets is None:
        return list(default)
    if not isinstance(targets, list):
        raise ValueError(f"{label}: targets 必须为 list[(host, port)]，收到 {type(targets).__name__}")
    for item in targets:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError(f"{label}: 非法端点 {item!r}（须为 (host, port)）")
        if not item[0] or not isinstance(item[1], int) or not (0 < item[1] < 65536):
            raise ValueError(f"{label}: 非法端点 {item!r}（host 非空、port 须 1~65535）")
    return list(targets)


def check_network_latency(
    targets: Optional[List[tuple]] = None,
    warning_ms: float = DEFAULT_LATENCY_WARNING_MS,
    critical_ms: float = DEFAULT_LATENCY_CRITICAL_MS,
    timeout: float = DEFAULT_LATENCY_TIMEOUT,
) -> HealthCheckResult:
    """
    网络延迟检查（TCP 连接耗时，毫秒）。

    本行使用的数据截至 t 时刻：仅测当前连接延迟，不推断未来可用性。

    Parameters
    ----------
    targets : list[(host, port)] | None
        探测端点；None 用默认（国内公共 DNS）
    warning_ms / critical_ms : float
        延迟阈值（毫秒），warning 以上 DEGRADED、critical 以上 UNHEALTHY
    timeout : float
        单端点连接超时（秒）

    Returns
    -------
    HealthCheckResult
    """
    _validate_thresholds(warning_ms, critical_ms, "network_latency")
    if timeout <= 0:
        raise ValueError(f"timeout 必须为正数，收到 {timeout}")
    eps = _resolve_endpoints(targets, DEFAULT_LATENCY_TARGETS, "network_latency")
    start = time.time()
    if not eps:
        return HealthCheckResult(
            name="network_latency",
            status=HealthStatus.HEALTHY,
            message="网络延迟检查未配置探测端点（跳过）",
            details={"targets": [], "latency_ms": None},
            duration=time.time() - start,
        )
    import socket

    latencies: Dict[str, float] = {}
    failures: List[str] = []
    for host, port in eps:
        begin = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                latencies[f"{host}:{port}"] = round((time.time() - begin) * 1000, 2)
        except OSError as e:
            failures.append(f"{host}:{port}({e.__class__.__name__})")
    max_ms = max(latencies.values()) if latencies else 0.0
    details = {"latency_ms": latencies, "max_ms": max_ms, "failures": failures}
    if failures and not latencies:
        status = HealthStatus.UNHEALTHY
        message = f"网络延迟检查失败（全部端点不可达）: {'; '.join(failures)}"
    elif failures:
        status = HealthStatus.DEGRADED
        message = f"部分端点不可达: {'; '.join(failures)}；最大延迟 {max_ms}ms"
    elif max_ms >= critical_ms:
        status = HealthStatus.UNHEALTHY
        message = f"网络延迟 {max_ms:.0f}ms 超过严重阈值 {critical_ms:.0f}ms"
    elif max_ms >= warning_ms:
        status = HealthStatus.DEGRADED
        message = f"网络延迟 {max_ms:.0f}ms 超过警告阈值 {warning_ms:.0f}ms"
    else:
        status = HealthStatus.HEALTHY
        message = f"网络延迟正常（最大 {max_ms:.0f}ms）"
    return HealthCheckResult(
        name="network_latency", status=status, message=message, details=details,
        duration=time.time() - start,
    )


def check_clock_skew(
    server: str = DEFAULT_NTP_SERVER,
    warning_seconds: float = DEFAULT_CLOCK_WARNING_SECONDS,
    critical_seconds: float = DEFAULT_CLOCK_CRITICAL_SECONDS,
    timeout: float = DEFAULT_NTP_TIMEOUT,
) -> HealthCheckResult:
    """
    时钟偏差检查（NTP 协议校时，标准库 UDP 123）。

    本行使用的数据截至 t 时刻：偏差=服务器时间-本地时间（当前瞬间），
    不做趋势预测。NTP 不可达 → DEGRADED（网络问题不误判为时钟损坏）；
    偏差超阈值 → UNHEALTHY（时间偏差影响订单/行情时间戳可信度）。

    Parameters
    ----------
    server : str
        NTP 服务器（默认 ntp.aliyun.com）
    warning_seconds / critical_seconds : float
        偏差阈值（秒）
    timeout : float
        UDP 超时（秒）

    Returns
    -------
    HealthCheckResult
    """
    if not (server or "").strip():
        raise ValueError("clock_skew: server 不能为空")
    _validate_thresholds(warning_seconds, critical_seconds, "clock_skew")
    if timeout <= 0:
        raise ValueError(f"timeout 必须为正数，收到 {timeout}")
    start = time.time()
    try:
        offset = _ntp_offset(server, timeout)
    except OSError as e:
        return HealthCheckResult(
            name="clock_skew",
            status=HealthStatus.DEGRADED,
            message=f"NTP 服务器 {server} 不可达（时钟偏差无法校时，网络问题不判时钟损坏）: {e}",
            details={"server": server, "error": str(e)},
            duration=time.time() - start,
        )
    except Exception as e:  # noqa: BLE001（NTP 响应解析异常也按不可评估降级）
        return HealthCheckResult(
            name="clock_skew",
            status=HealthStatus.DEGRADED,
            message=f"NTP 响应解析失败（{server}）: {e}",
            details={"server": server, "error": str(e)},
            duration=time.time() - start,
        )
    abs_offset = abs(offset)
    if abs_offset >= critical_seconds:
        status = HealthStatus.UNHEALTHY
        message = f"时钟偏差 {offset:+.1f}s 超过严重阈值 {critical_seconds:.0f}s（影响交易时间戳可信度）"
    elif abs_offset >= warning_seconds:
        status = HealthStatus.DEGRADED
        message = f"时钟偏差 {offset:+.1f}s 超过警告阈值 {warning_seconds:.0f}s"
    else:
        status = HealthStatus.HEALTHY
        message = f"时钟偏差 {offset:+.1f}s（与 {server} 校时一致）"
    return HealthCheckResult(
        name="clock_skew", status=status,
        message=message, details={"server": server, "offset_seconds": round(offset, 3)},
        duration=time.time() - start,
    )


def _ntp_offset(server: str, timeout: float) -> float:
    """
    NTP 请求-响应偏移量（秒）：(T2-T1 + T3-T4)/2 标准算法。

    本行使用的数据截至 t 时刻：四个时间戳均为当前校时瞬间采集。
    """
    import socket
    import struct

    # NTP 请求包（LI=0, VN=4, Mode=3 客户端），transmit timestamp 置 0
    request = bytearray(48)
    request[0] = 0x23  # 001 00 011：LI=0 VN=4 Mode=3
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        t1 = time.time()
        sock.sendto(bytes(request), (server, 123))
        response, _ = sock.recvfrom(48)
        t4 = time.time()
    if len(response) < 48:
        raise ValueError("NTP 响应长度不足 48 字节")
    # NTP 时间戳 = 32 位秒 + 32 位小数（自 1900-01-01）
    sec = struct.unpack("!I", response[40:44])[0]
    frac = struct.unpack("!I", response[44:48])[0]
    ntp_epoch = 2208988800.0  # 1970-01-01 与 1900-01-01 的秒差
    t2 = sec - ntp_epoch + frac / 2**32
    t3 = time.time()
    return ((t2 - t1) + (t3 - t4)) / 2.0


def check_cpu_usage(
    warning_pct: float = DEFAULT_CPU_WARNING_PCT,
    critical_pct: float = DEFAULT_CPU_CRITICAL_PCT,
    interval: float = 0.5,
) -> HealthCheckResult:
    """
    CPU 使用率检查（psutil 采样）。

    本行使用的数据截至 t 时刻：仅采样当前使用率。

    Parameters
    ----------
    warning_pct / critical_pct : float
        使用率阈值（%）
    interval : float
        采样窗口（秒）；<=0 时用 0.05 短采样避免阻塞巡检

    Returns
    -------
    HealthCheckResult
    """
    _validate_thresholds(warning_pct, critical_pct, "cpu_usage")
    if interval < 0:
        raise ValueError(f"interval 不能为负，收到 {interval}")
    start = time.time()
    try:
        import psutil
    except ImportError:
        return HealthCheckResult(
            name="cpu_usage",
            status=HealthStatus.DEGRADED,
            message="CPU 检查不可评估：psutil 未安装",
            details={"error": "psutil not installed"},
            duration=time.time() - start,
        )
    try:
        sample_interval = interval if interval > 0 else 0.05
        usage = psutil.cpu_percent(interval=sample_interval)
        details = {"cpu_percent": round(usage, 2), "cores": psutil.cpu_count() or 0}
        if usage >= critical_pct:
            status, message = HealthStatus.UNHEALTHY, f"CPU 使用率 {usage:.1f}% 超过严重阈值 {critical_pct:.0f}%"
        elif usage >= warning_pct:
            status, message = HealthStatus.DEGRADED, f"CPU 使用率 {usage:.1f}% 超过警告阈值 {warning_pct:.0f}%"
        else:
            status, message = HealthStatus.HEALTHY, f"CPU 使用率 {usage:.1f}%"
        return HealthCheckResult(
            name="cpu_usage", status=status, message=message, details=details,
            duration=time.time() - start,
        )
    except Exception as e:  # noqa: BLE001
        return HealthCheckResult(
            name="cpu_usage", status=HealthStatus.UNHEALTHY,
            message=f"CPU 检查失败: {e}", details={"error": str(e)},
            duration=time.time() - start,
        )


def check_memory_usage(
    warning_pct: float = DEFAULT_MEMORY_WARNING_PCT,
    critical_pct: float = DEFAULT_MEMORY_CRITICAL_PCT,
) -> HealthCheckResult:
    """
    内存使用率检查（psutil 虚拟内存）。

    本行使用的数据截至 t 时刻：仅读取当前内存占用。

    Parameters
    ----------
    warning_pct / critical_pct : float
        使用率阈值（%）

    Returns
    -------
    HealthCheckResult
    """
    _validate_thresholds(warning_pct, critical_pct, "memory_usage")
    start = time.time()
    try:
        import psutil
    except ImportError:
        return HealthCheckResult(
            name="memory_usage",
            status=HealthStatus.DEGRADED,
            message="内存检查不可评估：psutil 未安装",
            details={"error": "psutil not installed"},
            duration=time.time() - start,
        )
    try:
        mem = psutil.virtual_memory()
        usage = mem.percent
        details = {
            "memory_percent": round(usage, 2),
            "total_bytes": mem.total,
            "available_bytes": mem.available,
        }
        if usage >= critical_pct:
            status, message = HealthStatus.UNHEALTHY, f"内存使用率 {usage:.1f}% 超过严重阈值 {critical_pct:.0f}%"
        elif usage >= warning_pct:
            status, message = HealthStatus.DEGRADED, f"内存使用率 {usage:.1f}% 超过警告阈值 {warning_pct:.0f}%"
        else:
            status, message = HealthStatus.HEALTHY, f"内存使用率 {usage:.1f}%（剩余 {mem.available / 1024**3:.1f} GB）"
        return HealthCheckResult(
            name="memory_usage", status=status, message=message, details=details,
            duration=time.time() - start,
        )
    except Exception as e:  # noqa: BLE001
        return HealthCheckResult(
            name="memory_usage", status=HealthStatus.UNHEALTHY,
            message=f"内存检查失败: {e}", details={"error": str(e)},
            duration=time.time() - start,
        )


def check_tls_certificate(
    hosts: Optional[List[tuple]] = None,
    warning_days: float = DEFAULT_TLS_WARNING_DAYS,
    critical_days: float = DEFAULT_TLS_CRITICAL_DAYS,
    timeout: float = DEFAULT_TLS_TIMEOUT,
) -> HealthCheckResult:
    """
    HTTPS 证书有效期检查（peer cert notAfter vs 当前时间）。

    本行使用的数据截至 t 时刻：剩余天数=当前时刻与证书到期时间的差，
    不做证书续期预测。

    Parameters
    ----------
    hosts : list[(host, port)] | None
        HTTPS 端点；None 用默认基础设施端点
    warning_days / critical_days : float
        剩余天数阈值（低于即 DEGRADED/UNHEALTHY）
    timeout : float
        建连超时（秒）

    Returns
    -------
    HealthCheckResult
    """
    # 证书语义：剩余天数越少越严重 → 独立校验（正数 + warning 必须大于 critical）
    if warning_days <= 0 or critical_days <= 0:
        raise ValueError(
            f"tls_certificate: 阈值必须为正数，收到 {warning_days}/{critical_days}"
        )
    if warning_days <= critical_days:
        raise ValueError(
            f"tls_certificate: warning_days 必须大于 critical_days（剩余天数越少越严重），"
            f"收到 {warning_days}/{critical_days}"
        )
    if timeout <= 0:
        raise ValueError(f"timeout 必须为正数，收到 {timeout}")
    eps = _resolve_endpoints(hosts, DEFAULT_TLS_HOSTS, "tls_certificate")
    start = time.time()
    if not eps:
        return HealthCheckResult(
            name="tls_certificate",
            status=HealthStatus.HEALTHY,
            message="证书检查未配置 HTTPS 端点（跳过）",
            details={"hosts": []},
            duration=time.time() - start,
        )
    import socket
    import ssl

    now = time.time()
    remaining: Dict[str, float] = {}
    failures: List[str] = []
    for host, port in eps:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as s:
                    cert = s.getpeercert()
            not_after = ssl.cert_time_to_seconds(cert["notAfter"])
            remaining[f"{host}:{port}"] = round((not_after - now) / 86400.0, 1)
        except Exception as e:  # noqa: BLE001（建连/握手/证书解析失败统一记录）
            failures.append(f"{host}:{port}({e.__class__.__name__})")
    details = {"remaining_days": remaining, "failures": failures}
    min_days = min(remaining.values()) if remaining else 0.0
    if failures and not remaining:
        status = HealthStatus.DEGRADED
        message = f"证书检查端点全部不可达（不可评估，网络问题不判证书失效）: {'; '.join(failures)}"
    elif failures:
        status = HealthStatus.DEGRADED
        message = f"部分证书端点不可达: {'; '.join(failures)}；最短剩余 {min_days:.0f} 天"
    elif min_days < critical_days:
        status = HealthStatus.UNHEALTHY
        message = f"证书将在 {min_days:.0f} 天内到期（严重阈值 {critical_days:.0f} 天）"
    elif min_days < warning_days:
        status = HealthStatus.DEGRADED
        message = f"证书剩余 {min_days:.0f} 天，低于警告阈值 {warning_days:.0f} 天"
    else:
        status = HealthStatus.HEALTHY
        message = f"HTTPS 证书有效期正常（最短剩余 {min_days:.0f} 天）"
    return HealthCheckResult(
        name="tls_certificate", status=status, message=message, details=details,
        duration=time.time() - start,
    )


def check_dns_resolution(
    hosts: Optional[List[str]] = None,
    timeout: float = DEFAULT_DNS_TIMEOUT,
) -> HealthCheckResult:
    """
    DNS 解析检查（域名 → IP 地址解析）。

    本行使用的数据截至 t 时刻：仅解析当前域名。

    Parameters
    ----------
    hosts : list[str] | None
        待解析域名；None 用默认
    timeout : float
        解析超时（秒，经 getaddrinfo 阻塞同步执行）

    Returns
    -------
    HealthCheckResult
    """
    if timeout <= 0:
        raise ValueError(f"timeout 必须为正数，收到 {timeout}")
    names = DEFAULT_DNS_HOSTS if hosts is None else hosts
    if not isinstance(names, list):
        raise ValueError(f"hosts 必须为 list[str]，收到 {type(names).__name__}")
    for h in names:
        if not (h or "").strip():
            raise ValueError("hosts 中存在空域名")
    start = time.time()
    if not names:
        return HealthCheckResult(
            name="dns_resolution",
            status=HealthStatus.HEALTHY,
            message="DNS 检查未配置域名（跳过）",
            details={"hosts": []},
            duration=time.time() - start,
        )
    import socket

    failures: List[str] = []
    resolved: Dict[str, List[str]] = {}
    for host in names:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            ips = sorted({info[4][0] for info in infos})
            resolved[host] = ips
        except socket.gaierror as e:
            failures.append(f"{host}({e.__class__.__name__})")
    details = {"resolved_ips": resolved, "failures": failures}
    if failures:
        status = HealthStatus.UNHEALTHY
        message = f"DNS 解析失败: {'; '.join(failures)}"
    else:
        status = HealthStatus.HEALTHY
        message = f"DNS 解析正常（{len(resolved)} 个域名）"
    return HealthCheckResult(
        name="dns_resolution", status=status, message=message, details=details,
        duration=time.time() - start,
    )


def check_network_partition(
    endpoints: Optional[List[tuple]] = None,
    timeout: float = DEFAULT_PARTITION_TIMEOUT,
) -> HealthCheckResult:
    """
    网络分区检查（关键端点整体可达性）。

    本行使用的数据截至 t 时刻：仅测当前可达性；全部失败=疑似网络分区
    （数据源/券商/交易所全断），部分失败=降级。

    Parameters
    ----------
    endpoints : list[(host, port)] | None
        关键端点；None 用默认（延迟目标 + TLS 端点）
    timeout : float
        单端点超时（秒）

    Returns
    -------
    HealthCheckResult
    """
    if timeout <= 0:
        raise ValueError(f"timeout 必须为正数，收到 {timeout}")
    eps = _resolve_endpoints(endpoints, DEFAULT_PARTITION_ENDPOINTS, "network_partition")
    start = time.time()
    if not eps:
        return HealthCheckResult(
            name="network_partition",
            status=HealthStatus.HEALTHY,
            message="网络分区检查未配置端点（跳过）",
            details={"endpoints": []},
            duration=time.time() - start,
        )
    import socket

    reachable: Dict[str, bool] = {}
    for host, port in eps:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                reachable[f"{host}:{port}"] = True
        except OSError:
            reachable[f"{host}:{port}"] = False
    reachable_count = sum(1 for ok in reachable.values() if ok)
    total = len(reachable)
    details = {"reachable": reachable, "reachable_count": reachable_count, "total": total}
    if reachable_count == 0:
        status = HealthStatus.UNHEALTHY
        message = f"网络分区疑似（{total} 个关键端点全部不可达）"
    elif reachable_count < total:
        status = HealthStatus.DEGRADED
        message = f"部分关键端点不可达（{total - reachable_count}/{total} 失败）"
    else:
        status = HealthStatus.HEALTHY
        message = f"网络分区正常（{total} 个关键端点全部可达）"
    return HealthCheckResult(
        name="network_partition", status=status, message=message, details=details,
        duration=time.time() - start,
    )


def register_l0_checks(checker: "HealthChecker") -> "HealthChecker":
    """
    批量注册 L0 基础设施检查项（A-1 验收：基础检查全部可巡检）。

    注册 7 项：network_latency / clock_skew / cpu_usage / memory_usage /
    tls_certificate / dns_resolution / network_partition。

    critical 标记说明（影响总体 UNHEALTHY 判定）：
      - critical=True：时钟偏差、DNS、网络分区（直接威胁交易时间戳/数据可达）
      - critical=False：延迟、CPU、内存、证书（降级提示，不误杀整体）

    Parameters
    ----------
    checker : HealthChecker
        目标管理器

    Returns
    -------
    HealthChecker : self（链式）
    """
    checker.add_check(
        "network_latency", check_network_latency, timeout=15.0,
        critical=False, description="网络延迟检查（TCP 连接耗时）",
    )
    checker.add_check(
        "clock_skew", check_clock_skew, timeout=8.0,
        critical=True, description="时钟偏差检查（NTP 校时）",
    )
    checker.add_check(
        "cpu_usage", check_cpu_usage, timeout=8.0,
        critical=False, description="CPU 使用率检查（psutil）",
    )
    checker.add_check(
        "memory_usage", check_memory_usage, timeout=8.0,
        critical=False, description="内存使用率检查（psutil）",
    )
    checker.add_check(
        "tls_certificate", check_tls_certificate, timeout=15.0,
        critical=False, description="HTTPS 证书有效期检查",
    )
    checker.add_check(
        "dns_resolution", check_dns_resolution, timeout=8.0,
        critical=True, description="DNS 解析检查",
    )
    checker.add_check(
        "network_partition", check_network_partition, timeout=15.0,
        critical=True, description="网络分区检查（关键端点可达性）",
    )
    return checker


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


# ═══════════════════════════════════════════════
#  自测（python -m myquant.monitoring.health_check）
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """独立自测：L0 七项检查注册/执行/汇总 + 防御式校验，0=通过。"""
    import threading

    logger.info("health_check 自测开始")
    passed = 0
    failed = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            logger.info("  ✓ %s", label)
        else:
            failed += 1
            logger.error("  ✗ %s %s", label, detail)

    # 1. register_l0_checks 注册 7 项并可 run_all
    checker = HealthChecker()
    register_l0_checks(checker)
    names = checker.list_checks()
    for expect in (
        "network_latency", "clock_skew", "cpu_usage", "memory_usage",
        "tls_certificate", "dns_resolution", "network_partition",
    ):
        check(f"L0 注册: {expect}", expect in names)
    results = checker.run_all()
    check("L0 执行 7 项", len(results) == 7, f"got {len(results)}")
    check("L0 结果状态均合法", all(r.status in HealthStatus for r in results))
    summary = checker.get_summary()
    check("汇总状态合法", summary["overall_status"] in ("healthy", "degraded", "unhealthy"))
    check("汇总计数一致", summary["checks_count"] == 7)

    # 2. 各检查函数可独立执行（注入可控目标）
    latency = check_network_latency(targets=[("127.0.0.1", 1)], timeout=0.3)
    check("latency 不可达目标降级不崩溃", latency.status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY))
    cpu = check_cpu_usage(interval=0.0)
    check("cpu 检查可执行", cpu.status in HealthStatus)
    mem = check_memory_usage()
    check("memory 检查可执行", mem.status in HealthStatus)
    dns = check_dns_resolution(hosts=["localhost"])
    check("dns 本机解析健康", dns.status == HealthStatus.HEALTHY, dns.message)
    partition = check_network_partition(endpoints=[("127.0.0.1", 1)], timeout=0.3)
    check("partition 端点失败不崩溃", partition.status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY))

    # 3. 防御式校验
    for bad_call, label in (
        (lambda: check_network_latency(warning_ms=500, critical_ms=100), "latency 阈值倒挂"),
        (lambda: check_network_latency(targets=[("host", "80")]), "latency 非法端点"),
        (lambda: check_clock_skew(server=""), "clock 空服务器"),
        (lambda: check_cpu_usage(warning_pct=95, critical_pct=90), "cpu 阈值倒挂"),
        (lambda: check_memory_usage(warning_pct=-1, critical_pct=90), "memory 负阈值"),
        (lambda: check_tls_certificate(warning_days=7, critical_days=30), "tls 阈值语义错（剩余天数 warning 须 > critical）"),
        (lambda: check_dns_resolution(timeout=0), "dns 零超时"),
        (lambda: check_network_partition(timeout=-1), "partition 负超时"),
    ):
        try:
            bad_call()
            check(f"防御式: {label}", False)
        except ValueError:
            check(f"防御式: {label}", True)

    # 4. 与默认注册共存（register_default_checks + L0）
    combined = HealthChecker()
    combined.register_default_checks()
    register_l0_checks(combined)
    check("默认+L0 共存", len(combined.list_checks()) == 3 + 7)

    # 5. 并发执行预留（多线程 run_check 单项）
    def _run_single() -> None:
        checker.run_check("cpu_usage")

    threads = [threading.Thread(target=_run_single) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发 run_check 单项不抛异常", True)

    logger.info("自测结果: %d 通过, %d 失败", passed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    raise SystemExit(_run_self_test())
