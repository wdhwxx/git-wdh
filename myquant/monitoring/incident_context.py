# 文件名：myquant/monitoring/incident_context.py
"""
incident_context.py - 根因分析（单机版，C-7，回写#236）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》C-7：告警关联上下文聚合 +
依赖抑制 + 事件驱动根因判定。

触发模式（R-02）：事件驱动（EVENT）
  - 告警类事件到达即经 subscribe_to_event_bus() 实时聚合判定；
    handle_alert()/handle_metric()/handle_change()/handle_log() 注入入口供周期兜底。

核心口径（全部基于"已发生"时间戳，严格防未来）：
  1. 同窗聚合：同一时间窗（默认 60s，可配）内的告警按时间窗聚合为一次 incident。
     窗口由"已观测水位线"界定——只消费 ts<=now 的告警/事件，拒绝未来时间点。
     incident 结构（JSON 可序列化）：
       incident_id / start_ts / end_ts / root_alert / related_alerts /
       metrics / changes / logs / status / suppressed_count / related_count。
  2. 依赖抑制：定义"底层→上层"依赖映射（底层故障引发上层告警）。同窗内底层
     （根因）告警命中时，由其引发的上层告警只作为关联上下文记录（related，
     suppressed=True），不再单独触发新的根因 incident——同根因链路只保留根因
     incident，避免告警风暴。依赖映射可注入可扩展（dependency_map）。
     缺省映射：watchdog（行情断流 A-4）→ data_quality / strategy；
               data_quality → strategy。
  3. 根因判定：时间窗内首个告警即根因（root_alert）；同窗后续告警为关联上下文。
     （单机版按到达顺序定根因；真实世界根因先于级联告警到达，符合行情断流→级联
     数据质量/策略告警的传播时序。）

告警契约（参照 C-2 rolling_performance 模式）：
  - 根因 incident 判定后经 get_alert_manager().alert() 发 WARNING/ERROR；
  - category=incident、source=monitoring.incident_context；
  - deduplication_key="incident:<incident_id>" 防重复（同一 incident 只发一次）；
  - details 附 incident 上下文摘要（root/related/suppressed 计数）。

指标（REGISTERED_METRICS 声明 + 经 get_metrics_collector() 动态注册，
禁止修改 metrics_collector.py 本体）：
  - incident_total             （COUNTER）每开一次根因 incident +1；
  - incident_suppressed_total   （COUNTER）每抑制一条上层告警 +1。

设计原则（铁律）:
  1. 严格防未来：只聚合已发生时间戳（告警 created_at / 事件 ts）；窗口按已观测
     水位线界定；拒绝未来时间点（容差 _FUTURE_EPS）。
  2. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  3. 防御式校验 + 全量类型注解 + Google docstring；logging 不用 print。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import Event, EventBus, EventType
from myquant.core.logger import get_logger

logger = get_logger("monitoring.incident_context")

# ─────────────────────────────────────────────
# 常量（非业务阈值；窗口/依赖映射均可构造注入覆盖）
# ─────────────────────────────────────────────
#: 告警分类（根因 incident 告警落此 category，决定 Runbook 兜底）
CATEGORY_INCIDENT: str = "incident"

#: 告警来源（source 字段固定值）
SOURCE_INCIDENT: str = "monitoring.incident_context"

#: 缺省聚合时间窗（秒）：同窗告警聚合为一次 incident
DEFAULT_WINDOW_SECONDS: float = 60.0

#: 根因 incident 告警去重键前缀（后缀拼 incident_id，固定窗口内不刷屏）
DEDUP_INCIDENT_PREFIX: str = "incident:"

#: 未来点判定容差（秒）：ts 超过 now 的绝对上限
_FUTURE_EPS: float = 1.0

#: 待折叠上下文缓冲保留倍数（超出 window*此倍数的 pending 项被淘汰，防无界增长）
_PENDING_RETENTION_FACTOR: float = 3.0

#: 缺省"底层→上层"依赖映射：key=底层（根因候选）category，value=其可引发的上层 category。
#: 行情断流（watchdog A-4）→ 数据质量告警 / 策略漂移告警；数据质量 → 策略告警。
#: 可构造注入 dependency_map 覆盖/扩展。
DEFAULT_DEPENDENCY_MAP: Dict[str, List[str]] = {
    "watchdog": ["data_quality", "strategy"],
    "data_quality": ["strategy"],
}

#: 缺省订阅的"告警类事件"类型（EventBus 事件驱动入口；可构造注入 alert_event_types 扩展）。
#: 任一此类事件到达即按 event.data 归一化为一条告警观察并实时聚合判定。
DEFAULT_ALERT_EVENT_TYPES: Tuple[EventType, ...] = (
    EventType.SYSTEM_ERROR,
    EventType.DATA_QUALITY_ALERT,
    EventType.DATA_QUALITY_CRITICAL,
    EventType.DATA_QUALITY_DIRTY,
    EventType.RISK_ALERT,
    EventType.RISK_VIOLATION,
)

#: 指标名（COUNTER，经 get_metrics_collector() 动态注册）
METRIC_INCIDENT_TOTAL: str = "incident_total"
METRIC_INCIDENT_SUPPRESSED_TOTAL: str = "incident_suppressed_total"

#: 本模块注册的全部指标名（供主控/驾驶舱查询）
REGISTERED_METRICS: List[str] = [
    METRIC_INCIDENT_TOTAL,
    METRIC_INCIDENT_SUPPRESSED_TOTAL,
]

#: 上下文缓冲分桶名（metrics / changes / logs）
_BUCKET_METRICS: str = "metrics"
_BUCKET_CHANGES: str = "changes"
_BUCKET_LOGS: str = "logs"
_BUCKETS: Tuple[str, ...] = (_BUCKET_METRICS, _BUCKET_CHANGES, _BUCKET_LOGS)


class IncidentContextEngine:
    """
    根因分析引擎（单机版）：同窗告警聚合 + 底层→上层依赖抑制 + 事件驱动。

    算法（流式、水位线、严格防未来）：
      - 告警观察到达（handle_alert / EventBus 事件）：
        a. 拒绝未来时间戳（>now+_FUTURE_EPS）。
        b. 若落入当前未关闭 incident 的窗口（start_ts <= ts <= start_ts+window）：
           追加为 related_alerts；若其 category 是该 incident 根因 category 的
           上层依赖（dependency_map）→ suppressed=True 且累计抑制计数（不再单开
           新根因 incident）。
        c. 否则：关闭当前未关闭 incident（status=closed），以本告警为根因开新
           incident（root_alert），incident_total+1，并经 AlertManager 发一条
           category=incident 的根因告警。

    上下文（metrics/changes/logs）：落入打开 incident 窗口的直接挂入；否则进
      pending 缓冲；新 incident 开启时折叠 root_ts 前 window 内的 pending 项。

    使用方式:
        from myquant.monitoring.incident_context import IncidentContextEngine

        eng = IncidentContextEngine(window_seconds=60.0)
        eng.subscribe_to_event_bus(bus)            # 真实 EventBus 事件驱动
        eng.handle_alert(level, category, source, dedup_key, ts=...)  # 兜底注入
        incidents = eng.get_incidents()            # 全部 incident（含根因摘要）
    """

    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        dependency_map: Optional[Dict[str, List[str]]] = None,
        alert_event_types: Optional[Tuple[EventType, ...]] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        window_seconds : float
            聚合时间窗（秒，>0）；同窗告警聚合为一次 incident。默认 60s。
        dependency_map : dict | None
            "底层→上层"依赖映射 {底层category: [上层category,...]}；None 用
            DEFAULT_DEPENDENCY_MAP。可注入扩展（如 broker→trade）。
        alert_event_types : tuple(EventType) | None
            订阅的告警类事件类型；None 用 DEFAULT_ALERT_EVENT_TYPES。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（动态注册计数器）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        """
        try:
            win = float(window_seconds)
        except (TypeError, ValueError) as e:
            raise ValueError(f"window_seconds 必须可转 float，收到 {window_seconds!r}") from e
        if win <= 0:
            raise ValueError(f"window_seconds 必须为正数，收到 {win!r}")

        self._window: float = win
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector

        # 依赖映射归一化为 {底层: set(上层)}（防御：值可迭代字符串列表）
        raw_map = dependency_map if dependency_map is not None else DEFAULT_DEPENDENCY_MAP
        if not isinstance(raw_map, dict):
            raise ValueError(f"dependency_map 必须为 dict，收到 {type(raw_map)!r}")
        self._dep_map: Dict[str, List[str]] = {}
        for low, ups in raw_map.items():
            if not isinstance(low, str) or not low:
                raise ValueError(f"dependency_map 的 key 必须为非空字符串，收到 {low!r}")
            if not isinstance(ups, (list, tuple)):
                raise ValueError(f"dependency_map[{low!r}] 必须为 list/tuple，收到 {type(ups)!r}")
            self._dep_map[low] = [str(u) for u in ups if isinstance(u, str) and u]

        self._alert_event_types: Tuple[EventType, ...] = (
            alert_event_types if alert_event_types is not None else DEFAULT_ALERT_EVENT_TYPES
        )

        # 状态：至多一个"打开中"的 incident；其余落 closed 列表
        self._open_incident: Optional[Dict[str, Any]] = None
        self._closed_incidents: List[Dict[str, Any]] = []
        # 上下文缓冲（未挂入打开 incident 的 metrics/changes/logs）
        self._pending: Dict[str, List[Dict[str, Any]]] = {
            _BUCKET_METRICS: [], _BUCKET_CHANGES: [], _BUCKET_LOGS: [],
        }
        self._seq: int = 0  # incident_id 单调序号

        # EventBus 订阅句柄（unsubscribe 用）
        self._bus: Optional[EventBus] = None
        self._handler_ids: List[str] = []

        # 已观测水位线（所有 handle_* 时间戳的最大值；pending 淘汰按水位线而非墙钟，
        # 保证回放历史事件时 window 内的上下文不被误删——窗口按已观测水位线界定）
        self._watermark: float = 0.0

        # 计数器（内存镜像，供 to_dict/测试断言；同时落 collector）
        self._incident_total: int = 0
        self._suppressed_total: int = 0

        self._register_metrics()
        logger.debug(
            "IncidentContextEngine 初始化: window=%ss dep_map=%s event_types=%s",
            self._window, self._dep_map, [t.value for t in self._alert_event_types],
        )

    # ═══════════════════════════════════════════════
    #  事件驱动（EVENT，R-02）：订阅真实 EventBus
    # ═══════════════════════════════════════════════
    def subscribe_to_event_bus(self, bus: EventBus) -> List[str]:
        """
        订阅事件总线：告警类事件到达即实时聚合判定（参照 alerting.subscribe_to_event_bus）。

        Parameters
        ----------
        bus : EventBus
            真实事件总线实例。

        Returns
        -------
        list[str] : 订阅的 handler_id 列表。

        Raises
        ------
        ValueError : bus 无 subscribe 方法时抛出。
        """
        if bus is None or not callable(getattr(bus, "subscribe", None)):
            raise ValueError("bus 必须为提供 subscribe() 的 EventBus")
        self._bus = bus
        handler_ids: List[str] = []
        for etype in self._alert_event_types:
            hid = bus.subscribe(etype, lambda e: self._on_event(e))
            handler_ids.append(hid)
        self._handler_ids = handler_ids
        logger.info("IncidentContextEngine 已订阅事件总线，handler_ids=%s", handler_ids)
        return handler_ids

    def unsubscribe_from_event_bus(self, bus: Optional[EventBus] = None) -> int:
        """取消事件总线订阅，返回成功取消的处理器数。"""
        target = bus if bus is not None else self._bus
        count = 0
        if target is not None and callable(getattr(target, "unsubscribe", None)):
            for hid in self._handler_ids:
                try:
                    if target.unsubscribe(hid):
                        count += 1
                except Exception as e:  # noqa: BLE001 —— 单个取消失败不阻断其余
                    logger.warning("取消订阅 %s 失败（继续其余）: %s", hid, e)
        self._handler_ids = []
        return count

    def _on_event(self, event: Event) -> None:
        """
        EventBus 告警类事件回调：把事件 data 归一化为告警观察并 handle_alert。

        防御：任一事件格式非法仅记 warning 并跳过，绝不向总线抛出异常。
        """
        try:
            data = getattr(event, "data", None) or {}
            if not isinstance(data, dict):
                logger.warning("事件 data 非 dict，跳过聚合: %r", data)
                return
            level = data.get("level", data.get("severity", SeverityLevel.WARNING))
            category = data.get("category", "system")
            source = data.get("source", getattr(event, "source", "") or "event")
            dedup_key = data.get(
                "dedup_key", data.get("deduplication_key", getattr(event, "event_id", ""))
            )
            message = data.get("message", getattr(event, "type_name", "alert"))
            ts = data.get("ts", data.get("created_at", getattr(event, "timestamp", None)))
            self.handle_alert(
                level=level, category=category, source=source,
                dedup_key=dedup_key, message=str(message), ts=ts,
            )
        except Exception as e:  # noqa: BLE001 —— 事件回调禁止冒泡到总线
            logger.warning("IncidentContextEngine 处理事件失败（不阻断总线）: %s", e)

    # ═══════════════════════════════════════════════
    #  注入入口（周期兜底）
    # ═══════════════════════════════════════════════
    def handle_alert(
        self,
        level: Any,
        category: str,
        source: str,
        dedup_key: str,
        message: str = "",
        ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        摄入一条"已发生"告警观察（EVENT 路径与周期兜底共用入口）。

        防未来：ts 超过当前时刻 now（容差 1s）即拒绝抛 ValueError。
        聚合/抑制语义见类 docstring。

        Parameters
        ----------
        level : SeverityLevel | str
            告警级别（SeverityLevel 或其 value 字符串）。
        category : str
            告警分类（data_quality / strategy / watchdog / broker ...）。
        source : str
            告警来源（模块名）。
        dedup_key : str
            告警去重键（同根因链路关联依据）。
        message : str
            告警摘要消息。
        ts : float | None
            告警时间戳（epoch 秒，必须 <= now）；None 取当前时钟。

        Returns
        -------
        dict : 该告警所归入（或新开启）的 incident 快照。

        Raises
        ------
        ValueError : category/source/dedup_key 为空 / ts 非法或为未来时抛出。
        """
        norm_level = self._normalize_level(level)
        if not category or not isinstance(category, str):
            raise ValueError(f"category 必须为非空字符串，收到 {category!r}")
        if not source or not isinstance(source, str):
            raise ValueError(f"source 必须为非空字符串，收到 {source!r}")
        if not isinstance(dedup_key, str) or not dedup_key:
            raise ValueError(f"dedup_key 必须为非空字符串，收到 {dedup_key!r}")

        obs_ts = self._resolve_ts(ts)
        self._watermark = max(self._watermark, obs_ts)
        alert_rec: Dict[str, Any] = {
            "level": norm_level.value,
            "category": category,
            "source": source,
            "dedup_key": dedup_key,
            "message": str(message),
            "ts": obs_ts,
        }

        # ── 落入打开中 incident 的窗口？→ 关联上下文（可能被依赖抑制）──
        inc = self._open_incident
        if inc is not None and inc["start_ts"] <= obs_ts <= inc["start_ts"] + self._window:
            suppressed = self._is_suppressed(inc["root_alert"]["category"], category)
            alert_rec["suppressed"] = suppressed
            inc["related_alerts"].append(alert_rec)
            inc["end_ts"] = max(inc["end_ts"], obs_ts)
            if suppressed:
                inc["suppressed_count"] += 1
                self._suppressed_total += 1
                self._inc_counter(METRIC_INCIDENT_SUPPRESSED_TOTAL,
                                  labels={"root_category": inc["root_alert"]["category"]})
                logger.info(
                    "上层告警被根因抑制: incident=%s upper=%s(by %s)（仅作关联上下文）",
                    inc["incident_id"], category, inc["root_alert"]["category"],
                )
            return self._snapshot(inc)

        # ── 否则：关闭旧打开 incident，以本告警为根因开新 incident ──
        self._close_open_incident()
        new_inc = self._open_new_incident(alert_rec)
        return self._snapshot(new_inc)

    def handle_metric(
        self,
        name: str,
        value: float,
        ts: Optional[float] = None,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """注入一条指标快照（挂入打开 incident 或 pending 缓冲）。

        Parameters
        ----------
        name : str
            指标名。
        value : float
            指标值。
        ts : float | None
            采样时间戳（<=now）；None 取当前时钟。
        labels : dict | None
            指标标签维度。
        """
        if not name or not isinstance(name, str):
            raise ValueError(f"metric name 必须为非空字符串，收到 {name!r}")
        try:
            val = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"metric value 必须可转 float，收到 {value!r}") from e
        rec: Dict[str, Any] = {
            "name": name, "value": val, "ts": self._resolve_ts(ts),
            "labels": dict(labels or {}),
        }
        self._attach_or_buffer(_BUCKET_METRICS, rec)

    def handle_change(
        self,
        change_type: str,
        detail: str = "",
        ts: Optional[float] = None,
        actor: str = "",
    ) -> None:
        """注入一条最近变更记录（配置/策略/参数变更，可注入）。"""
        if not change_type or not isinstance(change_type, str):
            raise ValueError(f"change_type 必须为非空字符串，收到 {change_type!r}")
        rec: Dict[str, Any] = {
            "change_type": change_type, "detail": str(detail),
            "actor": str(actor), "ts": self._resolve_ts(ts),
        }
        self._attach_or_buffer(_BUCKET_CHANGES, rec)

    def handle_log(
        self,
        level: str,
        message: str,
        ts: Optional[float] = None,
        source: str = "",
    ) -> None:
        """注入一条日志摘要（可注入）。"""
        if not message:
            raise ValueError(f"log message 不能为空")
        rec: Dict[str, Any] = {
            "level": str(level), "message": str(message),
            "source": str(source), "ts": self._resolve_ts(ts),
        }
        self._attach_or_buffer(_BUCKET_LOGS, rec)

    # ═══════════════════════════════════════════════
    #  周期兜底：关闭已过期的打开 incident（水位线=now）
    # ═══════════════════════════════════════════════
    def poll(self) -> Dict[str, Any]:
        """周期兜底巡检：关闭窗口已过的打开 incident（按 now 水位线）。

        Returns
        -------
        dict : 当前 incident 面板快照（open + closed 计数）。
        """
        now = self._clock()
        inc = self._open_incident
        if inc is not None and now > inc["start_ts"] + self._window:
            self._close_open_incident()
        self._evict_pending()
        return self.to_dict()

    # ═══════════════════════════════════════════════
    #  内部：incident 生命周期
    # ═══════════════════════════════════════════════
    def _open_new_incident(self, root_alert: Dict[str, Any]) -> Dict[str, Any]:
        """以 root_alert 为根因开启新 incident，折叠 pending 上下文，发根因告警。"""
        self._seq += 1
        root_ts: float = root_alert["ts"]
        incident_id: str = f"incd_{int(root_ts)}_{self._seq:04d}"

        inc: Dict[str, Any] = {
            "incident_id": incident_id,
            "start_ts": root_ts,
            "end_ts": root_ts,
            "root_alert": root_alert,
            "related_alerts": [],
            "metrics": [],
            "changes": [],
            "logs": [],
            "status": "open",
            "suppressed_count": 0,
            "related_count": 0,
        }

        # 折叠 root_ts 前 window 内的 pending 上下文（最近变更/日志/指标作根因前上下文）
        lo = root_ts - self._window
        for bucket in _BUCKETS:
            folded = [r for r in self._pending[bucket] if lo <= r["ts"] <= root_ts]
            inc[bucket] = folded
            self._pending[bucket] = [r for r in self._pending[bucket] if r["ts"] > root_ts]

        self._open_incident = inc
        self._incident_total += 1
        self._inc_counter(METRIC_INCIDENT_TOTAL, labels={"root_category": root_alert["category"]})

        self._fire_incident_alert(inc)
        logger.info(
            "开启根因 incident %s: root=%s/%s（start_ts=%.3f）",
            incident_id, root_alert["category"], root_alert["source"], root_ts,
        )
        return inc

    def _close_open_incident(self) -> None:
        """把打开中的 incident 标记 closed 移入 closed 列表。"""
        inc = self._open_incident
        if inc is None:
            return
        inc["status"] = "closed"
        inc["related_count"] = len(inc["related_alerts"])
        self._closed_incidents.append(inc)
        self._open_incident = None

    def _is_suppressed(self, root_category: str, upper_category: str) -> bool:
        """判定 upper_category 是否为 root_category 底层可引发的上层告警（应抑制）。"""
        return upper_category in self._dep_map.get(root_category, [])

    # ═══════════════════════════════════════════════
    #  告警契约：根因判定后经 AlertManager 发 WARNING/ERROR
    # ═══════════════════════════════════════════════
    def _fire_incident_alert(self, inc: Dict[str, Any]) -> None:
        """发一条 category=incident 根因告警（dedup=incident:<id>）；失败记日志不阻断。"""
        root = inc["root_alert"]
        try:
            level = self._normalize_level(root["level"])
        except ValueError:
            level = SeverityLevel.ERROR
        message = (
            f"[incident] 根因事件 {inc['incident_id']}: "
            f"{root['source']}/{root['category']}「{root['message']}」"
            f"（关联 {len(inc['related_alerts'])} 条，抑制上层 {inc['suppressed_count']} 条）"
        )
        details: Dict[str, Any] = {
            "incident_id": inc["incident_id"],
            "start_ts": inc["start_ts"],
            "end_ts": inc["end_ts"],
            "root_alert": root,
            "related_count": len(inc["related_alerts"]),
            "suppressed_count": inc["suppressed_count"],
            "window_seconds": self._window,
        }
        try:
            self._alert_mgr().alert(
                level=level,
                message=message,
                source=SOURCE_INCIDENT,
                category=CATEGORY_INCIDENT,
                details=details,
                deduplication_key=f"{DEDUP_INCIDENT_PREFIX}{inc['incident_id']}",
            )
        except Exception as e:  # noqa: BLE001 —— 告警失败不阻断聚合
            logger.warning("根因 incident 告警发送失败（不阻断监控）: %s", e)

    def _alert_mgr(self) -> Any:
        """延迟解析告警管理器（None 时取全局单例）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager

            mgr = get_alert_manager()
            self._alert_manager = mgr
        return mgr

    # ═══════════════════════════════════════════════
    #  指标：REGISTERED_METRICS 动态注册 + COUNTER 递增
    # ═══════════════════════════════════════════════
    def _register_metrics(self) -> None:
        """经 get_metrics_collector() 动态注册本模块计数器（不改 metrics_collector.py）。"""
        col = self._resolve_collector()
        if col is None:
            return
        try:
            from myquant.monitoring.metrics_collector import MetricType
        except Exception as e:  # noqa: BLE001
            logger.warning("导入 MetricType 失败（指标未注册）: %s", e)
            return
        helps = {
            METRIC_INCIDENT_TOTAL: "Total number of root-cause incidents opened",
            METRIC_INCIDENT_SUPPRESSED_TOTAL: "Total number of upper-layer alerts suppressed by dependency",
        }
        for name in REGISTERED_METRICS:
            try:
                register = getattr(col, "register", None)
                if callable(register):
                    register(name, MetricType.COUNTER, help=helps.get(name, name), label_names=[])
            except Exception as e:  # noqa: BLE001 —— 单个注册失败不阻断其余
                logger.warning("指标 %s 注册失败（继续其余）: %s", name, e)

    def _resolve_collector(self) -> Any:
        """延迟解析指标采集器；不可用返回 None（仅记 warning）。"""
        col = self._metrics_collector
        if col is not None:
            return col
        try:
            from myquant.monitoring.metrics_collector import get_metrics_collector

            col = get_metrics_collector()
            self._metrics_collector = col
            return col
        except Exception as e:  # noqa: BLE001
            logger.warning("获取 metrics_collector 失败（指标未写入）: %s", e)
            return None

    def _inc_counter(self, name: str, amount: float = 1.0,
                     labels: Optional[Dict[str, str]] = None) -> None:
        """inc_counter 一个计数器；采集器不可用仅记 warning 不阻断。"""
        col = self._resolve_collector()
        if col is None:
            return
        try:
            col.inc_counter(name, amount=amount, labels=labels or {})
        except Exception as e:  # noqa: BLE001
            logger.warning("指标 %s 递增失败（不阻断检测）: %s", name, e)

    # ═══════════════════════════════════════════════
    #  上下文挂入 / pending 缓冲 / 淘汰
    # ═══════════════════════════════════════════════
    def _attach_or_buffer(self, bucket: str, rec: Dict[str, Any]) -> None:
        """把上下文记录挂入打开 incident 窗口内，否则进 pending 缓冲。"""
        self._watermark = max(self._watermark, rec["ts"])
        inc = self._open_incident
        if inc is not None and inc["start_ts"] <= rec["ts"] <= inc["start_ts"] + self._window:
            inc[bucket].append(rec)
            inc["end_ts"] = max(inc["end_ts"], rec["ts"])
        else:
            self._pending[bucket].append(rec)
        self._evict_pending()

    def _evict_pending(self) -> None:
        """按已观测水位线淘汰 pending 中过旧记录（> watermark - window*倍数），防无界增长。

        用水位线而非墙钟 now：回放历史事件时 now 可能远晚于事件时间，
        按墙钟会误删 window 内待折叠的上下文。
        """
        if self._watermark <= 0:
            return
        cutoff = self._watermark - self._window * _PENDING_RETENTION_FACTOR
        for bucket in _BUCKETS:
            self._pending[bucket] = [r for r in self._pending[bucket] if r["ts"] >= cutoff]

    # ═══════════════════════════════════════════════
    #  防御式归一化
    # ═══════════════════════════════════════════════
    def _resolve_ts(self, ts: Optional[float]) -> float:
        """解析并校验时间戳；None 取当前时钟；拒绝未来时间点（防未来红线）。"""
        now = self._clock()
        if ts is None:
            return now
        try:
            t = float(ts)
        except (TypeError, ValueError) as e:
            raise ValueError(f"ts 必须可转 float，收到 {ts!r}") from e
        if t > now + _FUTURE_EPS:
            raise ValueError(
                f"拒绝未来时间点：ts={t:.3f} 晚于当前 {now:.3f}（防未来红线）"
            )
        return t

    @staticmethod
    def _normalize_level(level: Any) -> SeverityLevel:
        """把 level（SeverityLevel / value 字符串）归一化为 SeverityLevel。"""
        if isinstance(level, SeverityLevel):
            return level
        if isinstance(level, str):
            try:
                return SeverityLevel(level.lower())
            except ValueError:
                pass
        raise ValueError(f"非法告警级别: {level!r}（应为 SeverityLevel 或其 value 字符串）")

    # ═══════════════════════════════════════════════
    #  状态可观测（JSON 可序列化）
    # ═══════════════════════════════════════════════
    @staticmethod
    def _snapshot(inc: Dict[str, Any]) -> Dict[str, Any]:
        """返回 incident 的深拷贝快照（related_count 即时刷新；JSON 可序列化）。"""
        snap = dict(inc)
        snap["related_count"] = len(inc["related_alerts"])
        return snap

    def get_incidents(self) -> List[Dict[str, Any]]:
        """返回全部 incident（打开中 + 已关闭），按开启顺序；均为 JSON 可序列化快照。"""
        out: List[Dict[str, Any]] = []
        if self._open_incident is not None:
            out.append(self._snapshot(self._open_incident))
        out.extend(self._closed_incidents)
        return out

    def to_dict(self) -> Dict[str, Any]:
        """返回引擎面板快照（incident 列表 + 内存计数器镜像）。"""
        return {
            "window_seconds": self._window,
            "incident_total": self._incident_total,
            "suppressed_total": self._suppressed_total,
            "open_incident": self._snapshot(self._open_incident) if self._open_incident else None,
            "closed_count": len(self._closed_incidents),
            "incidents": self.get_incidents(),
        }


# ═══════════════════════════════════════════════
#  __main__ 自测 Demo（可运行）
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测：真实 EventBus + 真实 AlertManager(临时库) 演示行情断流级联→单条根因 incident。"""
    import tempfile
    from pathlib import Path

    from myquant.monitoring.alert_store import AlertStore
    from myquant.monitoring.alerting import AlertManager, ConsoleAlertChannel

    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name} {info}")

    # ── 隔离环境：真实 EventBus + 真实 AlertManager(临时 AlertStore) ──
    tmpdir = Path(tempfile.mkdtemp(prefix="incident_ctx_demo_"))
    try:
        store = AlertStore(db_path=str(tmpdir / "incident.db"))
        manager = AlertManager(store=store, store_enabled=True)
        manager.add_channel(ConsoleAlertChannel("incident-demo-console"))
        bus = EventBus()

        eng = IncidentContextEngine(
            window_seconds=60.0, alert_manager=manager,
            metrics_collector=None, clock=time.time,
        )
        eng.subscribe_to_event_bus(bus)

        # ── 模拟行情断流级联事件流（真实 EventBus 发布，事件驱动 R-02）──
        # 1) 根因：A-4 行情断流（bar 超时）→ watchdog 断流告警（底层）
        bus.publish(Event(
            EventType.SYSTEM_ERROR, source="monitoring.watchdog",
            data={"level": SeverityLevel.ERROR.value, "category": "watchdog",
                  "source": "monitoring.watchdog", "dedup_key": "watchdog:staleness:down",
                  "message": "行情断流：bar 超时（A-4）", "ts": time.time()},
        ))
        time.sleep(0.01)
        # 2) 级联上层：数据质量告警（同窗内，应被根因抑制）
        bus.publish(Event(
            EventType.DATA_QUALITY_ALERT, source="monitoring.data_quality",
            data={"level": SeverityLevel.WARNING.value, "category": "data_quality",
                  "source": "monitoring.data_quality", "dedup_key": "dq:gap_detected",
                  "message": "断流导致行情缺口，数据质量下降", "ts": time.time()},
        ))
        time.sleep(0.01)
        # 3) 级联上层：策略告警（同窗内，应被根因抑制）
        bus.publish(Event(
            EventType.RISK_ALERT, source="monitoring.strategy",
            data={"level": SeverityLevel.WARNING.value, "category": "strategy",
                  "source": "monitoring.strategy", "dedup_key": "strategy:drift",
                  "message": "断流导致策略信号漂移", "ts": time.time()},
        ))

        incidents = eng.get_incidents()
        # ── 验收口径：一次行情断流只产生一条根因 incident，非告警风暴 ──
        check("1) 仅一条根因 incident", len(incidents) == 1, f"incidents={len(incidents)}")
        inc = incidents[0] if incidents else {}
        check("2) 根因=watchdog 断流", inc.get("root_alert", {}).get("category") == "watchdog",
              f"root={inc.get('root_alert')}")
        check("3) 关联告警=2（data_quality+strategy）", len(inc.get("related_alerts", [])) == 2,
              f"related={inc.get('related_alerts')}")
        check("4) 抑制计数=2（两条上层均被抑制）", inc.get("suppressed_count") == 2,
              f"suppressed={inc.get('suppressed_count')}")
        check("5) 抑制标记正确",
              all(a.get("suppressed") is True for a in inc.get("related_alerts", [])),
              f"related={inc.get('related_alerts')}")

        # 根因告警契约：AlertManager 只应收到 1 条 category=incident 告警
        incident_alerts = [a for a in manager._history if getattr(a, "category", "") == CATEGORY_INCIDENT]
        check("6) 仅一条 incident 根因告警（非告警风暴）", len(incident_alerts) == 1,
              f"incident_alerts={len(incident_alerts)}")
        if incident_alerts:
            ia = incident_alerts[0]
            check("7) 告警契约 source/category/dedup",
                  ia.source == SOURCE_INCIDENT and ia.category == CATEGORY_INCIDENT
                  and ia.deduplication_key.startswith(DEDUP_INCIDENT_PREFIX),
                  f"source={ia.source} cat={ia.category} dedup={ia.deduplication_key}")

        # 面板计数器镜像
        panel = eng.to_dict()
        check("8) incident_total=1 / suppressed_total=2",
              panel["incident_total"] == 1 and panel["suppressed_total"] == 2,
              f"panel={panel['incident_total']}/{panel['suppressed_total']}")

        # ── 同窗再喂一条 category=broker 告警：与 watchdog 根因无依赖关系，
        #    应并入同一 incident 作 related，但不被记为"上层抑制" ──
        eng.handle_alert(
            level=SeverityLevel.WARNING, category="broker", source="monitoring.broker",
            dedup_key="broker:latency", message="券商延迟升高",
        )
        incidents2 = eng.get_incidents()
        check("9) 同窗 broker 告警并入同一 incident（related 不抑制）",
              len(incidents2) == 1 and len(incidents2[0]["related_alerts"]) == 3,
              f"related={len(incidents2[0]['related_alerts']) if incidents2 else 0}")
        check("10) broker 非上层依赖→不额外抑制",
              incidents2[0]["suppressed_count"] == 2 if incidents2 else False,
              f"suppressed={incidents2[0]['suppressed_count'] if incidents2 else '?'}")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("=" * 60)
    print(f"incident_context.py 自测: {passed} 项通过, {len(failures)} 项失败")
    for f in failures:
        print(f"  ✗ {f}")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
