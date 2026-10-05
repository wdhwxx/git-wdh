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

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.logger import get_logger

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

        logger.debug("元监控初始化（R-08 独立冷却窗口 %.0fs）", self._cooldown_seconds)

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
        """独立告警通道（R-08：独立冷却窗口，与业务告警完全隔离）"""
        if self._meta_manager is None:
            try:
                from myquant.monitoring.alerting import (
                    AlertManager,
                    ConsoleAlertChannel,
                    LogAlertChannel,
                )
                manager = AlertManager(dedup_window_seconds=self._cooldown_seconds)
                manager.add_channel(ConsoleAlertChannel(name="meta-console"))
                manager.add_channel(LogAlertChannel(name="meta-log", logger_name="monitoring.meta"))
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

    from myquant.core.event_bus import Event, EventBus
    from myquant.monitoring.alerting import AlertManager, LogAlertChannel
    from myquant.monitoring.metrics_collector import MetricsCollector
    from myquant.monitoring.metrics_history import MetricsHistoryStore

    import tempfile
    from pathlib import Path

    # 1. 全组件注入（不触全局单例；时序库用临时文件，不碰真实 quant.db）
    bus = EventBus(max_history=100)
    collector = MetricsCollector().register_builtin_metrics()
    _tmp_db = Path(tempfile.mkdtemp(prefix="meta_test_")) / "meta_test.db"
    store = MetricsHistoryStore(db_path=_tmp_db)
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


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
