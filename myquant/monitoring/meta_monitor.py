# 文件名：myquant/monitoring/meta_monitor.py
"""
meta_monitor.py - 监控自身可观测性（BASE-5 元监控）
============================================================
功能:
  1. 五类元探针：采集器健康 / 事件总线积压 / 存储写入延迟 /
     告警发送成功率 / 看板可用性
  2. 元指标进入 MetricsCollector（/api/metrics 可观测）
  3. 元指标周期落库 MetricsHistoryStore（趋势/回放，R-02 仅回放不检测）
  4. 监控自身异常走独立告警通道（meta 通道）
  5. R-08 降噪约束：元监控告警走独立冷却窗口（单 key 冷却 + 聚合），
     独立于业务告警去重窗口，避免元监控抖动刷屏掩盖业务告警

防未来函数说明:
  - 所有探针只读取组件"当前已发生"的状态/统计（stats/history/上次写入耗时）
  - 指标时间戳为采集时刻当前时间
  - 无任何预测方法、无硬编码未来日期

使用方式:
    from myquant.monitoring.meta_monitor import MetaMonitor

    meta = MetaMonitor()
    report = meta.run()          # 执行一轮完整采集（探针→指标→落库→独立告警）
    status, detail = meta.probe_status()   # 供 HealthPatrol.add_check 使用
"""

from __future__ import annotations

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.logger import get_logger
from myquant.core.registry import register_component

logger = get_logger("monitoring.meta")

# ═══════════════════════════════════════════════
#  元监控常量（可调，不硬编码魔法数字）
# ═══════════════════════════════════════════════

# 探针状态值（与巡检 PATROL 语义一致：0=healthy 1=degraded 2=down）
META_HEALTHY = "healthy"
META_DEGRADED = "degraded"
META_DOWN = "down"
_META_STATUS_VALUE = {META_HEALTHY: 0.0, META_DEGRADED: 1.0, META_DOWN: 2.0}

# 采集器健康：指标超过该秒数未更新视为 stale（采集链路疑似中断）
DEFAULT_STALE_THRESHOLD_SECONDS = 300.0
# 事件总线积压：历史缓冲占比 >= 该比例视为 degraded
DEFAULT_BACKLOG_RATIO_DEGRADED = 0.8
# 存储写入延迟：单次落库耗时超过该毫秒数视为 degraded
DEFAULT_WRITE_LATENCY_WARN_MS = 1000.0
# 告警发送成功率：错误占比 > 该比例视为 degraded
DEFAULT_ALERT_ERROR_RATIO_WARN = 0.05
# R-08：元监控告警独立冷却窗口（秒），独立于业务告警 dedup（默认 300s）
DEFAULT_META_COOLDOWN_SECONDS = 120.0

# BASE-8 外部兜底心跳：默认参数（可调，不硬编码魔法数字）
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0   # 心跳写入间隔（秒）
DEFAULT_HEARTBEAT_CONSECUTIVE_TIMEOUT = 3   # R-08：连续超时次数才判死（>=2，规避临界网络误报）
DEFAULT_HEARTBEAT_TIMEOUT_FACTOR = 2.0      # 超时窗口 = interval * 该因子
_HEARTBEAT_FILENAME = "heartbeat.json"      # 外部载体心跳文件（FileHeartbeatCarrier）

# 元指标命名（P0-5 命名规范：领域_对象_动作_单位；领域=meta）
META_INDICATORS: Tuple[str, ...] = (
    "meta_status",                    # 元监控汇总（0/1/2）
    "meta_collector_health",          # 采集器健康（0/1/2）
    "meta_event_bus_backlog",         # 事件总线积压（0/1/2）
    "meta_storage_write_latency_ms",  # 存储写入延迟（毫秒）
    "meta_alert_send_success_rate",   # 告警发送成功率（0~1）
    "meta_dashboard_availability",    # 看板可用性（0/1/2）
)


# ═══════════════════════════════════════════════
#  BASE-8 外部兜底心跳（元监控心跳写到外部独立载体）
# ═══════════════════════════════════════════════
#
# 语义：元监控心跳写到外部独立载体（个人系统降级版=另一台低配机器挂载
# 目录/云函数/手机推送），单机故障（进程被杀/主机宕机）时外部看门狗仍能
# 依据心跳超时自报警——"监控自身故障可自报警"闭环。
# R-08 判定优化：外部看门狗采用**连续多次超时**（而非单次）才判
# "主进程死"，规避临界网络抖动误报；单次超时后心跳恢复不误判。

class HeartbeatCarrier(ABC):
    """
    外部心跳载体抽象（BASE-8 预留多载体）。

    send_heartbeat : 本机把存活心跳写入外部载体（幂等覆盖最近一次）
    read_last_heartbeat : 外部看门狗读取最近心跳（本机不可读回也允许）
    """

    @abstractmethod
    def send_heartbeat(self, payload: Dict[str, Any]) -> bool:
        """写入一次存活心跳；True=写入成功。"""

    @abstractmethod
    def read_last_heartbeat(self) -> Optional[Dict[str, Any]]:
        """读取最近一次心跳（无记录返回 None）。"""

    def close(self) -> None:
        """释放外部载体资源（可选实现）。"""


class FileHeartbeatCarrier(HeartbeatCarrier):
    """
    文件心跳载体（个人系统降级版首选：外部目录/另一台低配机器挂载目录）。

    心跳文件写入目标目录的 heartbeat.json（原子写：temp + rename，
    防半写损坏）；外部看门狗读该文件判存活。目录权限收紧由 BASE-9
    监控库文件权限收紧统一处理。
    """

    def __init__(self, directory: str, filename: str = _HEARTBEAT_FILENAME):
        directory_s = (directory or "").strip()
        if not directory_s:
            raise ValueError("心跳载体失败: directory 不能为空")
        self._directory = Path(directory_s)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._file = self._directory / (filename or _HEARTBEAT_FILENAME)

    def send_heartbeat(self, payload: Dict[str, Any]) -> bool:
        try:
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._file)  # 原子替换（防外部读半写）
            return True
        except OSError as e:
            logger.error(f"心跳写入失败（{self._file}）: {e}")
            return False

    def read_last_heartbeat(self) -> Optional[Dict[str, Any]]:
        try:
            if not self._file.exists():
                return None
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error(f"心跳读取失败（{self._file}）: {e}")
            return None


class WebhookHeartbeatCarrier(HeartbeatCarrier):
    """
    Webhook 心跳载体（手机推送/云函数回调：HTTP POST 存活心跳）。

    预留实现：无外部服务时调用即失败（记 warning 不阻断主流程），
    外部服务就绪后同接口可用；读回不支持（推送单向），返回 None。
    """

    def __init__(self, url: str, timeout_seconds: float = 5.0):
        if not (url or "").strip():
            raise ValueError("心跳载体失败: url 不能为空")
        self._url = url
        try:
            self._timeout = float(timeout_seconds)
            if self._timeout <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"timeout_seconds 必须为正数，收到 {timeout_seconds!r}")

    def send_heartbeat(self, payload: Dict[str, Any]) -> bool:
        try:
            import urllib.request

            req = urllib.request.Request(
                self._url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                return resp.status < 400
        except Exception as e:  # noqa: BLE001（网络失败记 warning，不阻断主流程）
            logger.warning(f"Webhook 心跳发送失败: {e}")
            return False

    def read_last_heartbeat(self) -> Optional[Dict[str, Any]]:
        return None  # 推送单向，外部服务侧留存，本机不可读回


_HEARTBEAT_CARRIER_REGISTRY: Dict[str, Callable[..., HeartbeatCarrier]] = {
    "file": FileHeartbeatCarrier,
    "webhook": WebhookHeartbeatCarrier,
}


def get_heartbeat_carrier(name: str, **kwargs: Any) -> HeartbeatCarrier:
    """
    按名称获取心跳载体实例（BASE-8 注册表）。

    Parameters
    ----------
    name : str
        "file"（外部目录心跳文件，个人系统首选）/ "webhook"（手机推送/云函数预留）
    kwargs : dict
        载体构造参数（file: directory；webhook: url）

    Raises
    ------
    ValueError: 未注册载体名或缺少必选参数
    """
    key = (name or "").strip().lower()
    if key not in _HEARTBEAT_CARRIER_REGISTRY:
        raise ValueError(
            f"未注册的心跳载体: {name!r}（可选: {sorted(_HEARTBEAT_CARRIER_REGISTRY)}）"
        )
    return _HEARTBEAT_CARRIER_REGISTRY[key](**kwargs)


class HeartbeatWriter:
    """
    心跳写入器（守护线程周期写外部载体；单机故障=进程死=心跳停）。

    Usage:
        writer = HeartbeatWriter(carrier, interval_seconds=60)
        writer.start(status_callable)   # 每次心跳携带当前元监控状态
        ...
        writer.stop(final_heartbeat=True)
    """

    def __init__(
        self,
        carrier: HeartbeatCarrier,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ):
        if not isinstance(carrier, HeartbeatCarrier):
            raise ValueError(
                f"carrier 必须为 HeartbeatCarrier 实例，收到 {type(carrier).__name__}"
            )
        try:
            interval = float(interval_seconds)
            if interval <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"interval_seconds 必须为正数，收到 {interval_seconds!r}")
        self._carrier = carrier
        self._interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        self._seq = 0
        self._last_written_ts: Optional[float] = None

    def _build_payload(self, status_callable: Optional[Callable[[], Any]]) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            seq = self._seq
        status = "ok"
        if status_callable is not None:
            try:
                st = status_callable()
                if isinstance(st, dict):
                    status = str(st.get("status", "ok"))
                elif st:
                    status = str(st)
            except Exception as e:  # noqa: BLE001
                status = f"error:{type(e).__name__}"
        return {
            "seq": seq,
            "ts": time.time(),          # 心跳时刻（当前时点，防未来）
            "pid": int(os.getpid()),
            "status": status,
            "msg": "meta-heartbeat",
        }

    def start(self, status_callable: Optional[Callable[[], Any]] = None) -> bool:
        """启动心跳守护线程（daemon，幂等；每次心跳携带当前状态）。"""
        with self._lock:
            if self._running:
                return True
            self._stop_event.clear()
            self._seq = 0

            def _loop() -> None:
                while not self._stop_event.wait(self._interval):
                    try:
                        payload = self._build_payload(status_callable)
                        self._carrier.send_heartbeat(payload)
                        self._last_written_ts = payload["ts"]
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"心跳发送异常: {e}")

            self._thread = threading.Thread(
                target=_loop, name="meta-heartbeat-writer", daemon=True
            )
            self._running = True
            self._thread.start()
        logger.info(f"[meta] 外部心跳写入启动（载体={type(self._carrier).__name__}，间隔 {self._interval:.0f}s）")
        return True

    def stop(self, final_heartbeat: bool = True) -> bool:
        """
        停止心跳线程（可选收尾写一次，防收尾前心跳中断误判）。

        Returns
        -------
        bool : True=已停止
        """
        with self._lock:
            if not self._running:
                return True
            self._running = False
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        if final_heartbeat:
            payload = self._build_payload(None)
            self._carrier.send_heartbeat(payload)
        logger.info("[meta] 外部心跳写入已停止")
        return True

    @property
    def last_written_ts(self) -> Optional[float]:
        """最近一次成功写入的心跳时刻（未写过 None）。"""
        return self._last_written_ts


class ExternalHeartbeatGuard:
    """
    外部看门狗判定器（R-08：连续多次超时而非单次，规避临界网络误报）。

    Usage（外部载体侧/演练/单测）:
        guard = ExternalHeartbeatGuard(interval_seconds=60, consecutive_timeout=3)
        alive, reason, missed = guard.check_alive(carrier)

    判定规则：
      - 最近心跳新鲜（now - ts <= interval*2）→ alive，missed=0
      - 最近心跳超时（now - ts > interval*2）→ 记 1 次超时；**连续
        missed >= consecutive_timeout 才判 dead**；单次/少量超时判
        degraded（不误判"主进程死"，规避临界网络抖动）
      - 完全无心跳记录 → dead（外部载体从无心跳=主进程从未存活）
      - 心跳恢复 → missed 清零，重新计数
    """

    def __init__(
        self,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        consecutive_timeout: int = DEFAULT_HEARTBEAT_CONSECUTIVE_TIMEOUT,
        timeout_factor: float = DEFAULT_HEARTBEAT_TIMEOUT_FACTOR,
    ):
        try:
            interval = float(interval_seconds)
            if interval <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"interval_seconds 必须为正数，收到 {interval_seconds!r}")
        if not isinstance(consecutive_timeout, int) or consecutive_timeout < 2:
            raise ValueError(
                f"consecutive_timeout 必须为 >=2 的整数（R-08 连续多次才判死），收到 {consecutive_timeout!r}"
            )
        try:
            factor = float(timeout_factor)
            if factor <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"timeout_factor 必须为正数，收到 {timeout_factor!r}")
        self._interval = interval
        self._consecutive_timeout = consecutive_timeout
        self._timeout_window = interval * factor
        self._missed = 0

    def check_alive(self, carrier: HeartbeatCarrier, now: Optional[float] = None) -> tuple:
        """
        依据最近心跳判定主进程存活。

        Returns
        -------
        tuple : (alive: bool, status: str, missed: int)
            alive=True 存活；status="alive"/"degraded"（超时未达阈值）/
            "dead"（连续超时达阈值或无任何心跳）；missed=当前连续超时次数
        """
        now_ts = time.time() if now is None else float(now)
        hb = carrier.read_last_heartbeat()
        if hb is None:
            self._missed = self._consecutive_timeout  # 从无心跳=判死
            return False, "dead", self._missed
        hb_ts = float(hb.get("ts", 0.0))
        if now_ts - hb_ts <= self._timeout_window:
            self._missed = 0  # 心跳恢复/新鲜，重新计数
            return True, "alive", 0
        self._missed += 1  # 超时（临界网络抖动：单次不判死）
        if self._missed >= self._consecutive_timeout:
            return False, "dead", self._missed
        return True, "degraded", self._missed

    def reset(self) -> None:
        """重置超时计数（外部看门狗周期起始/演练复位）。"""
        self._missed = 0


# ═══════════════════════════════════════════════
#  MetaMonitor
# ═══════════════════════════════════════════════

class MetaMonitor:
    """
    监控自身可观测性（BASE-5 元监控）。

    周期采集五类元指标，写入 MetricsCollector（/api/metrics 数据源）与
    MetricsHistoryStore（趋势回放），异常走独立告警通道且受 R-08
    独立冷却窗口约束（与业务告警去重完全隔离）。

    Parameters
    ----------
    collector : MetricsCollector | None
        指标采集器（None 用全局 get_metrics_collector()）
    metrics_history : MetricsHistoryStore | None
        指标时序存储（None 不落库；R-02 仅回放不检测）
    event_bus : EventBus | None
        事件总线（None 用全局 get_event_bus()）
    business_alert_manager : AlertManager | None
        业务告警管理器（被监控对象，用于告警发送成功率探针；
        None 用全局 get_alert_manager()）
    cooldown_seconds : float
        R-08 元监控告警独立冷却窗口（秒，默认 120，独立于业务 dedup 300）
    stale_threshold_seconds : float
        采集器健康 stale 阈值（秒）
    backlog_ratio_degraded : float
        事件总线积压 degraded 阈值（history 占比）
    write_latency_warn_ms : float
        存储写入延迟 degraded 阈值（毫秒）
    alert_error_ratio_warn : float
        告警发送失败率 degraded 阈值（0~1）
    """

    def __init__(
        self,
        collector: Any = None,
        metrics_history: Any = None,
        event_bus: Any = None,
        business_alert_manager: Any = None,
        cooldown_seconds: float = DEFAULT_META_COOLDOWN_SECONDS,
        stale_threshold_seconds: float = DEFAULT_STALE_THRESHOLD_SECONDS,
        backlog_ratio_degraded: float = DEFAULT_BACKLOG_RATIO_DEGRADED,
        write_latency_warn_ms: float = DEFAULT_WRITE_LATENCY_WARN_MS,
        alert_error_ratio_warn: float = DEFAULT_ALERT_ERROR_RATIO_WARN,
    ) -> None:
        # 组件（延迟导入：避免循环依赖与测试环境构建副作用）
        self._collector: Any = collector
        self._metrics_history: Any = metrics_history
        self._event_bus: Any = event_bus
        self._business_alert_manager: Any = business_alert_manager

        # R-08 独立冷却窗口（元监控告警专用 AlertManager，与业务告警隔离）
        self._meta_manager: Any = None
        self._cooldown_seconds = self._positive(cooldown_seconds, "cooldown_seconds")

        # 阈值
        self._stale_threshold_seconds = self._positive(
            stale_threshold_seconds, "stale_threshold_seconds"
        )
        self._backlog_ratio_degraded = self._nonneg(
            backlog_ratio_degraded, "backlog_ratio_degraded"
        )
        self._write_latency_warn_ms = self._positive(
            write_latency_warn_ms, "write_latency_warn_ms"
        )
        self._alert_error_ratio_warn = self._nonneg(
            alert_error_ratio_warn, "alert_error_ratio_warn"
        )

        # 探针注册表（可覆盖/扩展）
        self._probes: Dict[str, Callable[[], Tuple[str, Dict[str, Any]]]] = {
            "collector_health": self._probe_collector_health,
            "event_bus_backlog": self._probe_event_bus_backlog,
            "storage_write_latency": self._probe_storage_write_latency,
            "alert_send_success": self._probe_alert_send_success,
            "dashboard_availability": self._probe_dashboard_availability,
        }

        # 运行状态
        self._last_run_ts: float = 0.0
        self._last_write_latency_ms: float = 0.0   # 上次元指标落库耗时（ms）
        self._last_report: Dict[str, Any] = {}     # 最近一轮探针结果
        self._dashboard_probe: Optional[Callable[[], Tuple[bool, Dict[str, Any]]]] = None

        # BASE-8 外部兜底心跳（可选绑定：随巡检发心跳，单机故障外部看门狗判死）
        self._heartbeat_carrier: Optional[HeartbeatCarrier] = None
        self._heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
        self._last_heartbeat_ts: float = 0.0
        self._heartbeat_seq: int = 0

        logger.debug("元监控初始化（R-08 独立冷却窗口 %.0fs）", self._cooldown_seconds)

    def attach_heartbeat(
        self,
        carrier: HeartbeatCarrier,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> "MetaMonitor":
        """
        绑定外部心跳载体（BASE-8：随巡检发心跳；单机故障=run 停止=心跳停，
        外部看门狗按 R-08 连续多次超时判死）。

        Parameters
        ----------
        carrier : HeartbeatCarrier
            外部载体（FileHeartbeatCarrier 目录 / WebhookHeartbeatCarrier）
        interval_seconds : float
            心跳间隔（秒，>0；随巡检节奏或独立节拍）

        Returns
        -------
        MetaMonitor : self
        """
        if not isinstance(carrier, HeartbeatCarrier):
            raise ValueError(
                f"carrier 必须为 HeartbeatCarrier 实例，收到 {type(carrier).__name__}"
            )
        try:
            interval = float(interval_seconds)
            if interval <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"interval_seconds 必须为正数，收到 {interval_seconds!r}")
        self._heartbeat_carrier = carrier
        self._heartbeat_interval = interval
        logger.info(
            f"[meta] 外部心跳载体绑定: {type(carrier).__name__}（间隔 {interval:.0f}s）"
        )
        return self

    def _send_heartbeat(self) -> None:
        """
        写一次外部存活心跳（BASE-8；心跳携带最近元监控状态与时刻）。

        防未来：心跳 ts=当前时刻（只报告已发生状态）；写失败记 warning
        不阻断元监控主流程。
        """
        if self._heartbeat_carrier is None:
            return
        self._heartbeat_seq += 1
        payload: Dict[str, Any] = {
            "seq": self._heartbeat_seq,
            "ts": time.time(),
            "pid": int(os.getpid()),
            "status": str(self._last_report.get("status", "ok")),
            "msg": "meta-heartbeat",
        }
        try:
            ok = self._heartbeat_carrier.send_heartbeat(payload)
            if ok:
                self._last_heartbeat_ts = payload["ts"]
            else:
                logger.warning(f"[meta] 外部心跳写入失败（seq={payload['seq']}）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[meta] 外部心跳异常（不阻断元监控）: {e}")

    # ─────────────────────────────────────────
    #  防御式校验工具
    # ─────────────────────────────────────────

    @staticmethod
    def _positive(value: float, name: str) -> float:
        """正数校验：非法回退默认并留痕"""
        try:
            v = float(value)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
        logger.warning("meta_monitor 参数 %s 非法（须 >0）：%r，使用默认", name, value)
        return DEFAULT_META_COOLDOWN_SECONDS if name == "cooldown_seconds" else 0.0

    @staticmethod
    def _nonneg(value: float, name: str) -> float:
        """非负校验：非法回退 0 并留痕"""
        try:
            v = float(value)
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass
        logger.warning("meta_monitor 参数 %s 非法（须 >=0）：%r，回退 0", name, value)
        return 0.0

    # ─────────────────────────────────────────
    #  组件装配（延迟加载，失败降级不阻断）
    # ─────────────────────────────────────────

    def _get_collector(self) -> Any:
        if self._collector is None:
            try:
                from myquant.monitoring.metrics_collector import get_metrics_collector
                self._collector = get_metrics_collector()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"指标采集器加载失败: {e}")
        return self._collector

    def _get_event_bus(self) -> Any:
        if self._event_bus is None:
            try:
                from myquant.core.event_bus import get_event_bus
                self._event_bus = get_event_bus()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"事件总线加载失败: {e}")
        return self._event_bus

    def _get_business_manager(self) -> Any:
        if self._business_alert_manager is None:
            try:
                from myquant.monitoring.alerting import get_alert_manager
                self._business_alert_manager = get_alert_manager()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"业务告警管理器加载失败: {e}")
        return self._business_alert_manager

    def _get_meta_manager(self) -> Any:
        """独立告警通道（R-08：独立冷却窗口，与业务告警完全隔离）。

        C-9（回写#238）增强：元监控 manager 额外装配一个 meta 作用域
        AlertGovernor，使 dedup_key=meta:* 告警**确实流经 C-9 治理框架**的
        元监控独立冷却分支（META_DEDUP_PREFIX 识别 → 独立冷却窗口 → 跳过业务
        依赖抑制/动态基线降噪），与业务 dedup 300s 完全隔离。既有 16 项契约
        不变：首轮回调放行、冷却窗口内抑制、新实例缓存重建可再告警。
        """
        if self._meta_manager is None:
            try:
                from myquant.monitoring.alerting import (
                    AlertGovernor,
                    AlertManager,
                    ConsoleAlertChannel,
                    LogAlertChannel,
                )
                manager = AlertManager(dedup_window_seconds=self._cooldown_seconds)
                manager.add_channel(ConsoleAlertChannel(name="meta-console"))
                manager.add_channel(LogAlertChannel(name="meta-log", logger_name="monitoring.meta"))
                # C-9：装配 meta 作用域治理（独立冷却=本探针 cooldown；
                # 依赖抑制/动态基线降噪关闭——元监控告警不得被业务降噪误吞）
                try:
                    meta_gov = AlertGovernor(
                        dependency_suppression_enabled=False,
                        baseline_noise_reduction_enabled=False,
                        meta_cooldown_seconds=self._cooldown_seconds,
                    )
                    manager.attach_governor(meta_gov)
                except Exception as e:  # noqa: BLE001 —— 治理装配失败不阻断元监控告警
                    logger.warning(f"元监控治理框架装配失败（仅走独立 dedup）: {e}")
                self._meta_manager = manager
            except Exception as e:  # noqa: BLE001
                logger.warning(f"元监控独立告警通道初始化失败: {e}")
        return self._meta_manager

    # ─────────────────────────────────────────
    #  探针注册
    # ─────────────────────────────────────────

    def set_probe(self, name: str, func: Callable[[], Tuple[str, Dict[str, Any]]]) -> None:
        """覆盖/新增探针（防御：name 非空 + func 可调用）"""
        if not name or not callable(func):
            raise ValueError("探针必须提供非空 name 与可调用 func")
        self._probes[name] = func

    def remove_probe(self, name: str) -> bool:
        """移除探针（内置探针亦可移除，移除后该项不再参与采集）"""
        return self._probes.pop(name, None) is not None

    def set_dashboard_probe(self, func: Callable[[], Tuple[bool, Dict[str, Any]]]) -> None:
        """
        注入看板可用性检查（返回 (ok, detail)）。
        默认未注入时看板项为 standby（healthy + 占位说明，不触发告警）。
        """
        if func is not None and not callable(func):
            raise ValueError("dashboard probe 必须可调用")
        self._dashboard_probe = func

    # ─────────────────────────────────────────
    #  五类内置探针（只读组件当前状态，防未来）
    # ─────────────────────────────────────────

    def _probe_collector_health(self) -> Tuple[str, Dict[str, Any]]:
        """采集器健康：指标是否长时间未更新（stale）"""
        collector = self._get_collector()
        if collector is None:
            return META_DOWN, {"reason": "采集器未接入"}
        try:
            stale = collector.get_stale_metrics(
                max_age_seconds=self._stale_threshold_seconds
            )
            names = [m.name for m in stale]
            if names:
                return META_DEGRADED, {
                    "stale_count": len(names),
                    "stale_metrics": names[:10],
                    "threshold_seconds": self._stale_threshold_seconds,
                }
            return META_HEALTHY, {"stale_count": 0}
        except Exception as e:  # noqa: BLE001
            return META_DOWN, {"error": f"{type(e).__name__}: {e}"}

    def _probe_event_bus_backlog(self) -> Tuple[str, Dict[str, Any]]:
        """事件总线积压：历史缓冲占比 + handler 错误（积压/健康代理）"""
        bus = self._get_event_bus()
        if bus is None:
            return META_DOWN, {"reason": "事件总线未接入"}
        try:
            stats = bus.stats
            history_size = int(stats.get("history_size", 0))
            _history = getattr(bus, "_history", None)
            max_history = int(getattr(_history, "maxlen", 1000) or 1000)
            ratio = history_size / max_history if max_history > 0 else 0.0
            handler_errors = 0
            try:
                hs_list = bus.get_handler_stats()
                if isinstance(hs_list, list):
                    handler_errors = sum(int(h.error_count or 0) for h in hs_list)
            except Exception:  # noqa: BLE001
                handler_errors = -1  # 统计不可得，不判 down
            detail: Dict[str, Any] = {
                "history_size": history_size,
                "max_history": max_history,
                "ratio": round(ratio, 3),
                "handler_errors": handler_errors,
            }
            if ratio >= 1.0:
                return META_DOWN, dict(detail, reason="事件历史缓冲已满（截断风险）")
            if ratio >= self._backlog_ratio_degraded or handler_errors > 0:
                return META_DEGRADED, detail
            return META_HEALTHY, detail
        except Exception as e:  # noqa: BLE001
            return META_DOWN, {"error": f"{type(e).__name__}: {e}"}

    def _probe_storage_write_latency(self) -> Tuple[str, Dict[str, Any]]:
        """存储写入延迟：上次元指标落库耗时（毫秒）"""
        if self._metrics_history is None:
            return META_HEALTHY, {"mode": "no_history_store", "note": "未绑定时序存储，不测写入延迟"}
        latency = self._last_write_latency_ms
        detail = {"last_write_latency_ms": round(latency, 3)}
        if latency > self._write_latency_warn_ms:
            return META_DEGRADED, dict(
                detail, warn_threshold_ms=self._write_latency_warn_ms
            )
        return META_HEALTHY, detail

    def _probe_alert_send_success(self) -> Tuple[str, Dict[str, Any]]:
        """告警发送成功率：业务告警通道 sent/error 统计"""
        manager = self._get_business_manager()
        if manager is None:
            return META_DOWN, {"reason": "业务告警管理器未接入"}
        try:
            stats = manager.stats
            channels = stats.get("channels", {})
            sent = 0
            error = 0
            for name, ch_stats in channels.items():
                sent += int(ch_stats.get("sent_count", 0) or 0)
                error += int(ch_stats.get("error_count", 0) or 0)
            total = sent + error
            ratio = 1.0 if total == 0 else sent / total
            detail: Dict[str, Any] = {
                "sent": sent,
                "error": error,
                "success_rate": round(ratio, 4),
                "channels_count": int(stats.get("channels_count", 0)),
            }
            if total == 0:
                return META_HEALTHY, dict(detail, note="尚无告警发送记录")
            if ratio <= 0.0:
                return META_DOWN, dict(detail, reason="告警全部发送失败")
            if (1.0 - ratio) > self._alert_error_ratio_warn:
                return META_DEGRADED, dict(
                    detail, error_ratio_warn=self._alert_error_ratio_warn
                )
            return META_HEALTHY, detail
        except Exception as e:  # noqa: BLE001
            return META_DOWN, {"error": f"{type(e).__name__}: {e}"}

    def _probe_dashboard_availability(self) -> Tuple[str, Dict[str, Any]]:
        """看板可用性：可注入检查；默认 standby 不触发告警（防占位刷屏）"""
        if self._dashboard_probe is not None:
            try:
                ok, detail = self._dashboard_probe()
                return (META_HEALTHY if ok else META_DOWN), detail
            except Exception as e:  # noqa: BLE001
                return META_DOWN, {"error": f"{type(e).__name__}: {e}"}
        return META_HEALTHY, {
            "mode": "standby",
            "note": "看板实时健康未注入（BASE-5 占位，D-6 看板接入后启用）",
        }

    # ─────────────────────────────────────────
    #  采集执行
    # ─────────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        """
        执行一轮完整元监控采集：探针 → 指标（collector）→ 落库（时序）→ 独立告警。

        Returns
        -------
        Dict[str, Any] :
            {
                "timestamp": float,
                "status": "healthy" | "degraded" | "down",
                "checks": {探针名: {"status": ..., "detail": ...}},
            }
        """
        ts = time.time()
        checks: Dict[str, Dict[str, Any]] = {}
        for name, func in self._probes.items():
            try:
                status, detail = func()
            except Exception as e:  # noqa: BLE001
                status, detail = META_DOWN, {"error": f"{type(e).__name__}: {e}"}
            checks[name] = {"status": status, "detail": detail}

        status = self._evaluate_status(checks)
        report = {"timestamp": ts, "status": status, "checks": checks}
        self._last_report = report
        self._last_run_ts = ts

        # 指标 → MetricsCollector（/api/metrics 数据源）
        self._collect_indicators(report)
        # 落库 → MetricsHistoryStore（趋势回放；R-02 仅回放不检测；计时供延迟探针）
        self._persist_indicators(report)
        # 异常 → 独立告警通道（R-08 独立冷却窗口）
        self._alert_if_unhealthy(report)
        # BASE-8：外部兜底心跳随巡检写（单机故障=run 停=心跳停，外部看门狗判死）
        self._send_heartbeat()
        return report

    def _evaluate_status(self, checks: Dict[str, Dict[str, Any]]) -> str:
        """汇总：down 优先，其次 degraded，否则 healthy"""
        if any(c["status"] == META_DOWN for c in checks.values()):
            return META_DOWN
        if any(c["status"] == META_DEGRADED for c in checks.values()):
            return META_DEGRADED
        return META_HEALTHY

    def _collect_indicators(self, report: Dict[str, Any]) -> None:
        """元指标写入 MetricsCollector（meta_* 命名，P0-5 规范）"""
        collector = self._get_collector()
        if collector is None:
            return
        try:
            checks = report["checks"]
            collector.set_gauge("meta_status", _META_STATUS_VALUE[report["status"]])
            collector.set_gauge(
                "meta_collector_health",
                _META_STATUS_VALUE[checks["collector_health"]["status"]],
            )
            collector.set_gauge(
                "meta_event_bus_backlog",
                _META_STATUS_VALUE[checks["event_bus_backlog"]["status"]],
            )
            collector.set_gauge(
                "meta_storage_write_latency_ms",
                self._last_write_latency_ms,
            )
            success_rate = 1.0
            if checks["alert_send_success"]["status"] != META_DOWN:
                detail = checks["alert_send_success"]["detail"]
                success_rate = float(detail.get("success_rate", 1.0))
            collector.set_gauge("meta_alert_send_success_rate", success_rate)
            collector.set_gauge(
                "meta_dashboard_availability",
                _META_STATUS_VALUE[checks["dashboard_availability"]["status"]],
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"元指标写入采集器失败（不阻断）: {e}")

    def _persist_indicators(self, report: Dict[str, Any]) -> None:
        """元指标落库（R-02 仅回放不检测）；实测写入耗时供延迟探针"""
        store = self._metrics_history
        if store is None:
            self._last_write_latency_ms = 0.0
            return
        try:
            ts = report["timestamp"]
            rows: List[Dict[str, Any]] = [
                {
                    "name": "meta_status",
                    "value": _META_STATUS_VALUE[report["status"]],
                    "labels": {"status": report["status"]},
                    "timestamp": ts,
                }
            ]
            for name, check in report["checks"].items():
                rows.append(
                    {
                        "name": f"meta_check.{name}",
                        "value": _META_STATUS_VALUE[check["status"]],
                        "labels": {"status": check["status"]},
                        "timestamp": ts,
                    }
                )
            started = time.time()
            store.save_batch(rows)
            self._last_write_latency_ms = (time.time() - started) * 1000.0
        except Exception as e:  # noqa: BLE001
            self._last_write_latency_ms = 0.0
            logger.error(f"元指标落库失败（不阻断）: {e}")

    def _alert_if_unhealthy(self, report: Dict[str, Any]) -> int:
        """
        异常项走独立告警通道（R-08：独立冷却窗口，单 key 冷却 + 聚合）。

        同一探针在冷却窗口内仅发送一次（复用 AlertManager 内建 dedup，
        dedup_window=cooldown_seconds，与业务告警 300s 窗口完全隔离）。
        """
        manager = self._get_meta_manager()
        if manager is None:
            return 0
        sent = 0
        for name, check in report["checks"].items():
            if check["status"] == META_HEALTHY:
                continue
            try:
                from myquant.core.constants import SeverityLevel
                from myquant.monitoring.alerting import Alert
                level = (
                    SeverityLevel.ERROR
                    if check["status"] == META_DOWN
                    else SeverityLevel.WARNING
                )
                alert = Alert(
                    level=level,
                    message=f"[meta] {name} {check['status']}: {check['detail']}",
                    source="meta_monitor",
                    category="meta",
                    details=dict(check["detail"], probe=name),
                    deduplication_key=f"meta:{name}",
                )
                sent += manager.send_alert(alert)
            except Exception as e:  # noqa: BLE001
                logger.error(f"元监控告警发送失败: {e}")
        if sent:
            logger.info(f"[meta] 元监控异常告警已发送 {sent} 条（独立冷却 {self._cooldown_seconds:.0f}s）")
        return sent

    # ─────────────────────────────────────────
    #  供 HealthPatrol 集成
    # ─────────────────────────────────────────

    def probe_status(self) -> Tuple[str, Dict[str, Any]]:
        """
        巡检检查项接口：返回 (status, detail)。

        未执行过 run() 时先执行一轮（HealthPatrol.add_check 直接使用）。
        """
        if not self._last_report:
            self.run()
        return self._last_report["status"], {
            "checks": {
                name: c["status"] for name, c in self._last_report["checks"].items()
            },
            "last_run_ts": self._last_run_ts,
        }

    def to_dict(self) -> Dict[str, Any]:
        """导出运行状态"""
        return {
            "last_run_ts": self._last_run_ts,
            "last_write_latency_ms": round(self._last_write_latency_ms, 3),
            "cooldown_seconds": self._cooldown_seconds,
            "status": self._last_report.get("status", META_HEALTHY),
            "checks": {
                name: c["status"] for name, c in self._last_report.get("checks", {}).items()
            },
        }

    def __repr__(self) -> str:
        return (
            f"<MetaMonitor status={self._last_report.get('status', 'never_run')} "
            f"cooldown={self._cooldown_seconds:.0f}s>"
        )


# ═══════════════════════════════════════════════
#  __main__ 自测 Demo
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """内置自测：探针状态机 / 独立冷却 / 指标写入 / 落库 / 独立告警"""
    passed = 0
    failures: List[str] = []
    checks: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, info: str = "") -> None:
        nonlocal passed
        checks.append((name, ok, info))
        if ok:
            passed += 1
        else:
            failures.append(f"{name}: {info}")

    import tempfile
    from pathlib import Path

    from myquant.core.event_bus import Event, EventBus
    from myquant.core.metrics_history import MetricsHistoryStore
    from myquant.monitoring.alerting import AlertManager, LogAlertChannel
    from myquant.monitoring.metrics_collector import MetricsCollector

    # 1. 全组件注入（不触全局单例；时序库用临时文件，不碰真实 quant.db）
    bus = EventBus(max_history=100)
    collector = MetricsCollector().register_builtin_metrics()
    _tmp_db = Path(tempfile.mkdtemp(prefix="meta_test_")) / "meta_test.db"
    store = MetricsHistoryStore(db_path=_tmp_db)
    tmp_dir = str(Path(_tmp_db).parent)
    business = AlertManager()
    business.add_channel(LogAlertChannel(name="test-log", logger_name="test.business"))

    meta = MetaMonitor(
        collector=collector,
        metrics_history=store,
        event_bus=bus,
        business_alert_manager=business,
        cooldown_seconds=60.0,
    )
    report = meta.run()
    check("全组件健康时 status=healthy", report["status"] == "healthy", f"{report['status']}")
    check(
        "五类探针全部采集",
        set(report["checks"].keys()) == {
            "collector_health", "event_bus_backlog",
            "storage_write_latency", "alert_send_success", "dashboard_availability",
        },
        f"{set(report['checks'].keys())}",
    )
    check(
        "元指标进入采集器",
        collector.get_value("meta_status") == 0.0,
        f"{collector.get_value('meta_status')}",
    )
    check(
        "元指标落库可查（R-02 仅回放）",
        len(store.query("meta_status")) == 1,
        f"{len(store.query('meta_status'))}",
    )
    check(
        "探针状态接口",
        meta.probe_status()[0] == "healthy",
        f"{meta.probe_status()}",
    )

    # 2. 异常场景：总线历史积压 95/100 → degraded
    for _ in range(95):
        bus.publish(Event("META_FILL", data={"i": 1}))
    report2 = meta.run()
    check(
        "总线积压达 95%（>=80%）→ status=degraded",
        report2["status"] == "degraded",
        f"{report2['checks']['event_bus_backlog']}",
    )

    # 3. R-08 独立冷却：异常项在冷却窗口内只发一次（dedup_key=meta:*）
    meta_hist_size = lambda: meta._get_meta_manager().stats["history_size"]  # noqa: E731
    meta.run()  # 异常状态第一轮 → 发送 meta 告警
    check("异常状态发送独立告警", meta_hist_size() >= 1, f"history={meta_hist_size()}")
    before = meta_hist_size()
    meta.run()  # 同状态（同冷却窗口）第二轮 → dedup 不再发送
    check(
        "独立告警通道 dedup 生效（同 key 冷却窗口内只发一次）",
        meta_hist_size() == before,
        f"before={before}, after={meta_hist_size()}",
    )

    # 4. 独立告警通道隔离：业务 manager 无 meta 告警
    check(
        "业务告警管理器未收到 meta 告警（通道隔离）",
        business.stats["history_size"] == 0,
        f"{business.stats['history_size']}",
    )

    # 5. 新实例（新冷却缓存）可再告警
    meta2 = MetaMonitor(
        collector=collector,
        metrics_history=store,
        event_bus=bus,
        business_alert_manager=business,
        cooldown_seconds=60.0,
    )
    meta2.run()
    check(
        "新实例独立冷却缓存重建后可再告警",
        meta2._get_meta_manager().stats["history_size"] >= 1,
        f"history={meta2._get_meta_manager().stats['history_size']}",
    )

    # ══════════════════════════════════════
    #  BASE-8 自测（外部兜底心跳）
    # ══════════════════════════════════════

    # 载体：文件心跳（外部目录载体，原子写）
    hb_dir = Path(tmp_dir) / "heartbeat"
    carrier = FileHeartbeatCarrier(str(hb_dir))
    ok = carrier.send_heartbeat({"seq": 1, "ts": time.time(), "status": "ok"})
    check("文件载体写入", ok and (hb_dir / "heartbeat.json").exists())
    read_back = carrier.read_last_heartbeat()
    check("文件载体读回", read_back is not None and read_back["seq"] == 1)

    # 载体注册表
    check(
        "载体注册表 file/webhook",
        isinstance(get_heartbeat_carrier("file", directory=str(hb_dir)), FileHeartbeatCarrier),
    )
    try:
        get_heartbeat_carrier("unknown")
        check("未注册载体报错", False)
    except ValueError:
        check("未注册载体报错", True)

    # MetaMonitor 集成：attach_heartbeat → run 后心跳落载体
    meta3 = MetaMonitor(
        collector=collector,
        metrics_history=store,
        event_bus=bus,
        business_alert_manager=business,
    )
    meta3.attach_heartbeat(carrier, interval_seconds=60.0)
    meta3.run()
    hb = carrier.read_last_heartbeat()
    check(
        "元监控随巡检发心跳",
        hb is not None and int(hb.get("seq", 0)) >= 1 and "status" in hb,
    )
    check(
        "心跳带 pid/时刻（防未来=当前时刻）",
        int(hb.get("pid", 0)) > 0 and float(hb.get("ts", 0)) <= time.time(),
    )

    # R-08 外部看门狗：连续多次超时才判死（临界网络抖动不误判）
    guard = ExternalHeartbeatGuard(interval_seconds=60.0, consecutive_timeout=2)
    now = time.time()
    carrier.send_heartbeat({"seq": 99, "ts": now, "status": "ok"})
    alive, st, missed = guard.check_alive(carrier, now=now)
    check("新鲜心跳 alive", alive and st == "alive" and missed == 0)
    alive, st, missed = guard.check_alive(carrier, now=now + 121.0)  # 1 次超时
    check(
        "单次超时不判死（R-08 规避网络抖动）",
        alive and st == "degraded" and missed == 1,
        f"alive={alive}, st={st}, missed={missed}",
    )
    alive, st, missed = guard.check_alive(carrier, now=now + 181.0)  # 连续 2 次
    check(
        "连续 2 次超时判死",
        (not alive) and st == "dead" and missed == 2,
        f"alive={alive}, st={st}, missed={missed}",
    )
    # 心跳恢复 → 重新计数
    carrier.send_heartbeat({"seq": 100, "ts": now + 182.0, "status": "ok"})
    alive, st, missed = guard.check_alive(carrier, now=now + 182.0)
    check("心跳恢复不误判（计数清零）", alive and st == "alive" and missed == 0)

    # 完全无心跳 → dead
    empty_carrier = FileHeartbeatCarrier(str(Path(tmp_dir) / "empty_hb"))
    alive, st, missed = guard.check_alive(empty_carrier, now=time.time())
    check("外部载体从无心跳 → dead", (not alive) and st == "dead")

    # HeartbeatWriter 守护线程周期写
    writer_carrier = FileHeartbeatCarrier(str(Path(tmp_dir) / "writer_hb"))
    writer = HeartbeatWriter(writer_carrier, interval_seconds=0.2)
    writer.start(status_callable=lambda: {"status": "healthy"})
    time.sleep(0.6)
    writer_hb = writer_carrier.read_last_heartbeat()
    check(
        "HeartbeatWriter 周期写心跳",
        writer_hb is not None and int(writer_hb.get("seq", 0)) >= 2,
    )
    writer.stop(final_heartbeat=True)
    writer_hb2 = writer_carrier.read_last_heartbeat()
    check("HeartbeatWriter 收尾心跳", writer_hb2 is not None)

    # 5. 防御：非法冷却参数回退
    meta_bad = MetaMonitor(cooldown_seconds=-5)
    check("非法 cooldown 回退默认", meta_bad._cooldown_seconds == DEFAULT_META_COOLDOWN_SECONDS,
          f"{meta_bad._cooldown_seconds}")

    # 6. 看板占位不触发告警
    meta_dash = MetaMonitor(
        collector=collector, metrics_history=store,
        event_bus=bus, business_alert_manager=business,
    )
    dash_status = meta_dash._probe_dashboard_availability()[0]
    check("看板未注入默认 healthy（standby，防刷屏）", dash_status == "healthy", f"{dash_status}")

    print("=" * 60)
    if failures:
        print(f"自测结果: {passed} 项通过, {len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return len(failures)
    print(f"自测结果: 全部 {passed} 项通过 ✅")
    print("=" * 60)
    return 0


# ═══════════════════════════════════════════════
#  G2-5 阶段二：装配工厂（注册表化）
#  live/scheduler 经 core.registry.get_component("meta_monitor") 取用并调用，
#  消除 scheduler → monitoring.meta_monitor 反向依赖（EX-13B）。
#  模块导入即注册；装配责任上移（run_实盘入口.py setup_monitoring_linkage 触发）。
# ═══════════════════════════════════════════════

def get_meta_monitor(
    collector: Any = None,
    metrics_history: Any = None,
    event_bus: Any = None,
    **kwargs: Any,
) -> MetaMonitor:
    """元监控装配工厂（BASE-5）。

    Parameters
    ----------
    collector : Any | None
        指标采集器实例（None 时由 MetaMonitor 内部延迟取全局采集器）。
    metrics_history : Any | None
        指标时序存储实例（巡检复用同一趋势回放源）。
    event_bus : Any | None
        全局事件总线实例。
    **kwargs : Any
        透传 MetaMonitor 构造参数（cooldown_seconds/stale_threshold_seconds 等）。

    Returns
    -------
    MetaMonitor : 元监控实例（监控自身可观测性）。
    """
    return MetaMonitor(
        collector=collector,
        metrics_history=metrics_history,
        event_bus=event_bus,
        **kwargs,
    )


#: G2-5 阶段二：装配工厂注册（模块导入即注册；scheduler 经 get_component 取用）
register_component("meta_monitor", get_meta_monitor)


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
