# 文件名：myquant/monitoring/watchdog.py
"""
watchdog.py - 交易看门狗（A-4 行情活性看门狗）
==========================================================================
本模块承载"保命"看门狗（《量化系统监控模块施工总清单_V1.2》A-4/A-5/A-6）：

  1. StalenessDetector       —— A-4 行情活性看门狗（EVENT + 秒级 timer 兜底）
       bar 到达即刷新 staleness 时钟（on_bar / 订阅 EventType.BAR）；
       交易时段内超 bar_timeout_seconds 无新 bar → 告警；
       非交易时段（午休/收盘/节假日）自动豁免；
       开盘 grace_minutes 无首根 bar → "数据源未就绪"告警；
       bar 到达间隔 P99 内部统计并经 get_stats() 随巡检报告暴露
       （不触碰 metrics_collector.py）。

  （A-5 引擎心跳看门狗 / A-6 券商连接监控：后续回写增量补入本文件）

设计原则（4 条铁律）:
  1. 阈值唯一真源 R-04：全部数值阈值经 myquant.core.settings.get_monitoring_config()
     读取 config/settings.yaml（watchdog / slo.engine_heartbeat），代码内不硬编码业务阈值。
  2. R-02 触发模式：A-4 由事件/定时器实时触发，周期巡检仅兜底；
     周期落库不参与 P0 检测。
  3. 严格防未来：只消费"已发生"时间戳（bar 到达时刻 / 探测时刻），不预测行情。
  4. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。

依赖: time, threading, collections, myquant.core.{settings,constants,logger}
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from myquant.core.constants import (
    RUNBOOK_BROKER_DISCONNECT,  # G2-5 阶段二下沉 core，re-export 兼容旧引用
    SeverityLevel,
)
from myquant.core.logger import get_logger
from myquant.core.registry import register_component
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.watchdog")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04）
# G2-5 阶段二：A-4/A-6 探测间隔与断连 Runbook 已下沉 myquant/core/constants.py，
# 本模块 re-export（见顶部导入），供旧引用兼容；此处仅保留本模块私有常量。
# ─────────────────────────────────────────────
#: bar 到达间隔 P99 统计保留的样本窗口（最近 N 根 bar 的间隔）
P99_SAMPLE_WINDOW: int = 200
#: A-5 引擎 get_status() 单次探针超时上限（秒；超过视为无响应/僵死）
ENGINE_PROBE_TIMEOUT_SECONDS: float = 3.0


# ═══════════════════════════════════════════════
#  A-4 行情活性看门狗
# ═══════════════════════════════════════════════
class StalenessDetector:
    """
    行情活性（staleness）检测器。

    bar 到达即刷新 staleness 时钟（EVENT 触发）；交易时段内超过
    ``bar_timeout_seconds`` 无新 bar → 判定行情断流并产生 ERROR 告警；
    非交易时段（午休 / 收盘 / 节假日）自动豁免不误报；开盘 ``grace_minutes``
    内无首根 bar → "数据源未就绪"。

    触发模式（R-02）:
      - EVENT：``on_bar()`` 或 EventBus ``EventType.BAR`` 到达即刷新时钟。
      - 秒级 timer 兜底：``evaluate()`` 由快周期任务调用，bars 停止到达时
        无事件产生，由定时器检出超时并告警。

    防未来: 只记录"已发生"的 bar 到达时刻（event_ts / 系统时钟），
    不预测下一根 bar；统计只消费截至当前已到样本。

    使用方式:
        from myquant.monitoring.watchdog import StalenessDetector

        det = StalenessDetector()
        det.attach_event_bus(bus)          # 订阅 market.bar
        # bar 到达：det.on_bar(ts)
        status, detail = det.check()       # 供 HealthPatrol.add_check
        det.evaluate()                     # 供秒级 timer：检出即告警
    """

    def __init__(
        self,
        bar_timeout_seconds: Optional[float] = None,
        session_grace_minutes: Optional[float] = None,
        calendar: Any = None,
        clock: Optional[Callable[[], float]] = None,
        alert_manager: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        bar_timeout_seconds : float | None
            bar 超时判定窗口（秒）。None 读 monitoring.watchdog.bar_timeout_seconds。
        session_grace_minutes : float | None
            开盘宽限期（分钟）。None 读 monitoring.watchdog.session_grace_minutes。
        calendar : TradingCalendar | None
            交易日历（用于交易时段判定）。None 用默认 TradingCalendar。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。

        Raises
        ------
        ValueError : 阈值非正数时抛出（防御式：拒绝脏配置运行）。
        """
        cfg = get_monitoring_config()
        wd = cfg.get("watchdog", {})

        self._bar_timeout: float = (
            float(bar_timeout_seconds)
            if bar_timeout_seconds is not None
            else float(wd.get("bar_timeout_seconds", 300))
        )
        self._grace_seconds: float = (
            float(session_grace_minutes) * 60.0
            if session_grace_minutes is not None
            else float(wd.get("session_grace_minutes", 5)) * 60.0
        )
        if self._bar_timeout <= 0:
            raise ValueError(f"bar_timeout_seconds 必须 > 0，实际 {self._bar_timeout!r}")
        if self._grace_seconds < 0:
            raise ValueError(f"session_grace_minutes 必须 >= 0，实际 {session_grace_minutes!r}")

        # 交易日历（默认 TradingCalendar；测试可注入固定日历）
        if calendar is None:
            from myquant.utils.time_utils import TradingCalendar
            calendar = TradingCalendar()
        self._calendar = calendar

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager

        # 状态（全部为"已发生"事实）
        self._last_bar_ts: Optional[float] = None
        self._bars_received: int = 0
        self._prev_bar_ts: Optional[float] = None
        self._intervals: Deque[float] = deque(maxlen=P99_SAMPLE_WINDOW)
        self._active_alert_key: Optional[str] = None  # 当前未恢复的告警去重键
        self._handler_ids: List[str] = []
        logger.debug(
            "StalenessDetector 初始化: bar_timeout=%.0fs grace=%.0fs",
            self._bar_timeout, self._grace_seconds,
        )

    # ── EVENT 入口：bar 到达即刷新时钟 ──────────────────────
    def on_bar(self, ts: Optional[float] = None) -> None:
        """
        bar 到达事件入口（EVENT 触发）：刷新 staleness 时钟并记录到达间隔。

        Parameters
        ----------
        ts : float | None
            bar 业务发生时刻（epoch 秒）。None 取当前系统时钟。
            防未来：仅记录传入/当前时刻，不引用未来数据。
        """
        now = self._clock() if ts is None else float(ts)
        if self._prev_bar_ts is not None:
            gap = now - self._prev_bar_ts
            if gap >= 0:  # 防未来：仅消费非负间隔（乱序/回拨 bar 不计入 P99）
                self._intervals.append(gap)
            else:
                logger.warning("bar 时间戳回退（%s < %s），该间隔不计入 P99", now, self._prev_bar_ts)
        self._prev_bar_ts = now
        self._last_bar_ts = now
        self._bars_received += 1

    def attach_event_bus(self, bus: Any) -> None:
        """
        订阅 EventBus 的 EventType.BAR 事件，到达即刷新时钟。

        Parameters
        ----------
        bus : EventBus
            事件总线。订阅失败记 warning，不阻断（on_bar 仍可被业务直接调用）。
        """
        if bus is None:
            return
        try:
            from myquant.core.event_bus import EventType

            handler_id = bus.subscribe(EventType.BAR, self._handle_bar_event)
            self._handler_ids.append(handler_id)
            logger.info("StalenessDetector 已订阅 EventType.BAR")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"StalenessDetector 订阅 EventBus BAR 失败（on_bar 仍可用）: {e}")

    def _handle_bar_event(self, event: Any) -> None:
        """EventBus BAR 事件处理：优先取事件业务时刻 event_ts，回退事件创建时刻。"""
        try:
            data = getattr(event, "data", None) or {}
            ts = data.get("event_ts") or getattr(event, "timestamp", None)
            self.on_bar(ts=ts)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"StalenessDetector 处理 BAR 事件异常: {e}")

    # ── 交易时段判定（非交易时段豁免）────────────────────────
    def _session_open_ts(self, now_dt: Any) -> Optional[float]:
        """
        计算当前活跃交易时段的开盘时刻（今日 09:30）。

        MORNING/AFTERNOON 均以 09:30 为会话起点（上午 bar 中断后下午仍应连续）；
        其余时段返回 None。

        防未来：仅由当前时钟推算今日 09:30，不引用未来行情。
        """
        from myquant.utils.time_utils import SessionType

        session = self._calendar.get_session(now_dt)
        if session not in (SessionType.MORNING, SessionType.AFTERNOON):
            return None
        open_dt = now_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        return open_dt.timestamp()

    def _is_active_session(self, now_dt: Any) -> bool:
        """当前是否处于应收到 bar 的活跃交易时段（交易日 且 MORNING/AFTERNOON）。"""
        from myquant.utils.time_utils import SessionType

        if not self._calendar.is_trading_day(now_dt.date()):
            return False
        return self._calendar.get_session(now_dt) in (SessionType.MORNING, SessionType.AFTERNOON)

    # ── 纯判定：供 HealthPatrol.add_check（60s 兜底）─────────
    def check(self, now: Optional[float] = None) -> tuple:
        """
        判定当前行情活性（纯函数，不告警）。

        Parameters
        ----------
        now : float | None
            评估时刻戳（None 取当前时钟）。

        Returns
        -------
        tuple : (status, detail)
            status ∈ {"healthy" | "degraded" | "down"}（对齐 HealthPatrol 语义）。
            非交易时段一律 healthy（豁免，不误报）。

        防未来：本行使用的数据截至 now 时刻（last_bar_ts 为已发生 bar 时刻）。
        """
        from datetime import datetime

        from myquant.utils.time_utils import TZ_SHANGHAI

        now_ts = self._clock() if now is None else float(now)
        # 用上海时区的 wall-clock 判定交易时段（防未来：仅当前时刻）
        now_dt = datetime.fromtimestamp(now_ts, tz=TZ_SHANGHAI)

        if not self._is_active_session(now_dt):
            return "healthy", {"exempt": "非交易时段（午休/收盘/节假日豁免）"}

        open_ts = self._session_open_ts(now_dt)
        # 尚未收到任何 bar
        if self._last_bar_ts is None:
            age_since_open = now_ts - (open_ts if open_ts else now_ts)
            if age_since_open <= self._grace_seconds:
                return "healthy", {
                    "reason": "开盘宽限期内",
                    "grace_remaining_seconds": self._grace_seconds - age_since_open,
                }
            if age_since_open <= self._bar_timeout:
                return "degraded", {
                    "reason": "数据源未就绪",
                    "stale_seconds": age_since_open,
                }
            return "down", {"reason": "数据源未就绪", "stale_seconds": age_since_open}

        bar_age = now_ts - self._last_bar_ts
        if bar_age <= self._bar_timeout:
            return "healthy", {
                "last_bar_age_seconds": round(bar_age, 1),
                "bars_received": self._bars_received,
            }
        return "down", {
            "reason": "行情断流（交易时段无新 bar 超时）",
            "stale_seconds": round(bar_age, 1),
            "bar_timeout_seconds": self._bar_timeout,
        }

    # ── 告警闭环：供秒级 timer / 巡检调用 ────────────────────
    def evaluate(self, now: Optional[float] = None) -> tuple:
        """
        判定并在非健康时经 AlertManager 告警（R-02：事件/定时器实时触发，
        周期巡检仅兜底）。

        - down   → ERROR 告警（行情断流 / 数据源未就绪）
        - degraded → WARNING 告警
        - healthy → 若此前有未恢复告警，发 INFO 恢复并清状态

        Returns
        -------
        tuple : (status, detail) 同 check()。
        """
        status, detail = self.check(now=now)
        try:
            if status == "down":
                self._fire_alert(SeverityLevel.ERROR, "行情活性异常", detail, key="watchdog:staleness:down")
            elif status == "degraded":
                self._fire_alert(SeverityLevel.WARNING, "行情活性降级", detail, key="watchdog:staleness:degraded")
            else:
                self._recover(detail)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"StalenessDetector 告警闭环异常（不阻断检测）: {e}")
        return status, detail

    def _fire_alert(self, level: SeverityLevel, message: str, detail: Dict[str, Any], key: str) -> None:
        """发送告警（带固定去重键，窗口内不刷屏）；失败记 error 不阻断。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = get_alert_manager()
        mgr.alert(
            level=level,
            message=f"[watchdog] {message}: {detail.get('reason', detail)}",
            source="monitoring.watchdog",
            category="watchdog",
            details=detail,
            deduplication_key=key,
        )
        self._active_alert_key = key

    def _recover(self, detail: Dict[str, Any]) -> None:
        """恢复：若此前有未恢复告警，发一条 INFO 恢复（去重键带 recovered: 前缀）。"""
        if self._active_alert_key is None:
            return
        try:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = self._alert_manager or get_alert_manager()
            mgr.alert(
                level=SeverityLevel.INFO,
                message="[watchdog] 行情已恢复",
                source="monitoring.watchdog",
                category="watchdog",
                details=detail,
                deduplication_key=f"recovered:{self._active_alert_key}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"StalenessDetector 恢复告警失败: {e}")
        finally:
            self._active_alert_key = None

    # ── 统计查询（随巡检报告暴露，不触碰 metrics_collector）───
    def get_stats(self) -> Dict[str, Any]:
        """
        返回行情活性统计快照（供巡检报告 / 看板暴露）。

        含 last_bar_age / bar_interval_p99 / bars_received / 最近判定状态。
        P99 仅基于已到 bar 样本（防未来）。
        """
        now_ts = self._clock()
        last_age = (now_ts - self._last_bar_ts) if self._last_bar_ts is not None else None
        return {
            "bars_received": self._bars_received,
            "last_bar_ts": self._last_bar_ts,
            "last_bar_age_seconds": round(last_age, 1) if last_age is not None else None,
            "bar_interval_p99_seconds": self._p99_interval(),
            "sample_count": len(self._intervals),
        }

    def _p99_interval(self) -> Optional[float]:
        """计算 bar 到达间隔 P99（秒）。样本 <2 时返回 None（不伪造统计）。"""
        if len(self._intervals) < 2:
            return None
        ordered = sorted(self._intervals)
        # 本行使用的数据截至 now 时刻（仅已到 bar 样本）
        idx = max(0, int(len(ordered) * 0.99) - 1)
        return round(ordered[max(0, min(idx, len(ordered) - 1))], 3)

    def patrol_check(self) -> tuple:
        """供 HealthPatrol.add_check 的无参闭包：直接返回 check() 结果。"""
        return self.check()


# ═══════════════════════════════════════════════
#  A-5 引擎心跳看门狗（回写#209）
# ═══════════════════════════════════════════════
class EngineHeartbeatWatcher:
    """
    引擎心跳看门狗（POLL，窗口须 <60s，R-02/R-04 硬约束）。

    周期 ``probe()`` 调 ``engine.get_status()``：连续 ``consecutive_misses`` 次
    无响应（抛异常 / 单次探针超时）→ 判定引擎僵死，发 P0/ERROR 告警。
    恢复响应时计数清零并发 INFO 恢复。

    阈值（R-04，全部读 monitoring.slo.engine_heartbeat，禁止代码内自定义）:
      - probe_interval_seconds    : 探测间隔（默认 10s）
      - consecutive_misses        : 连续无响应次数（默认 5）
      - hang_judge_window_seconds : 判定窗口上限（默认 60s）；
        防御式校验 consecutive_misses × probe_interval 须 < window，否则 warning
        （保住 MTTD<1min，R-02 硬约束）。

    防未来：仅探测"当前"引擎是否响应，不预测引擎状态。
    """

    def __init__(
        self,
        engine: Any,
        probe_interval_seconds: Optional[float] = None,
        consecutive_misses: Optional[int] = None,
        hang_judge_window_seconds: Optional[float] = None,
        probe_timeout_seconds: float = ENGINE_PROBE_TIMEOUT_SECONDS,
        alert_manager: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        engine : LiveEngine | None
            实盘引擎（须有 get_status()）。None 时 probe 视为未接入。
        probe_interval_seconds / consecutive_misses / hang_judge_window_seconds :
            None 时读 monitoring.slo.engine_heartbeat（R-04 唯一真源）。
        probe_timeout_seconds : float
            单次 get_status() 超时上限（秒）；超过视为无响应。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。

        Raises
        ------
        ValueError : 阈值非法（非正数）时抛出。
        """
        slo = get_monitoring_config().get("slo", {}).get("engine_heartbeat", {})
        self._probe_interval: float = (
            float(probe_interval_seconds) if probe_interval_seconds is not None
            else float(slo.get("probe_interval_seconds", 10))
        )
        self._miss_limit: int = (
            int(consecutive_misses) if consecutive_misses is not None
            else int(slo.get("consecutive_misses", 5))
        )
        self._window: float = (
            float(hang_judge_window_seconds) if hang_judge_window_seconds is not None
            else float(slo.get("hang_judge_window_seconds", 60))
        )
        if self._probe_interval <= 0 or self._miss_limit <= 0 or self._window <= 0:
            raise ValueError(
                f"引擎心跳阈值非法（须>0）: interval={self._probe_interval}, "
                f"misses={self._miss_limit}, window={self._window}"
            )
        # R-02 硬约束：N×间隔 必须 < 判定窗口（保住 MTTD<1min）
        if self._miss_limit * self._probe_interval >= self._window:
            logger.warning(
                "R-02 约束告警: consecutive_misses(%d)×probe_interval(%.0f)=%.0fs "
                ">= hang_judge_window(%.0fs)，MTTD 将 >1min，请校准 slo.engine_heartbeat",
                self._miss_limit, self._probe_interval,
                self._miss_limit * self._probe_interval, self._window,
            )

        self._engine = engine
        self._probe_timeout: float = float(probe_timeout_seconds)
        self._alert_manager = alert_manager
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._miss_count: int = 0
        self._hang_alerted: bool = False
        logger.debug(
            "EngineHeartbeatWatcher: interval=%.0fs misses=%d window=%.0fs",
            self._probe_interval, self._miss_limit, self._window,
        )

    @property
    def probe_interval_seconds(self) -> float:
        """探测间隔（供调度器注册周期任务用）。"""
        return self._probe_interval

    def _single_probe(self) -> bool:
        """
        单次探针：调 engine.get_status()，返回是否响应。

        - engine 无 get_status → False（未接入，按无响应处理）。
        - get_status() 抛异常 → False。
        - get_status() 在 probe_timeout 内未返回（僵死）→ False。
        """
        if self._engine is None:
            return False
        func = getattr(self._engine, "get_status", None)
        if not callable(func):
            return False

        box: Dict[str, Any] = {}

        def _run() -> None:
            try:
                box["status"] = func()
            except Exception as e:  # noqa: BLE001
                box["error"] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(self._probe_timeout)
        if t.is_alive():
            return False  # 超时未返回 → 僵死
        if "error" in box:
            return False
        return True

    def probe(self) -> Dict[str, Any]:
        """
        执行一次心跳探测并维护连续无响应计数。

        Returns
        -------
        dict : {"responding": bool, "miss_count": int, "hung": bool}
            hung=True 表示已连续 miss 达上限（引擎僵死）。
        """
        ok = self._single_probe()
        if ok:
            self._miss_count = 0
            if self._hang_alerted:
                self._recover()
        else:
            self._miss_count += 1

        hung = self._miss_count >= self._miss_limit
        if hung and not self._hang_alerted:
            self._fire_hang_alert()
        return {"responding": ok, "miss_count": self._miss_count, "hung": hung}

    def _fire_hang_alert(self) -> None:
        """引擎僵死 P0/ERROR 告警（去重键固定，窗口内不刷屏）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = get_alert_manager()
        mgr.alert(
            level=SeverityLevel.ERROR,
            message=(
                f"[watchdog] 引擎僵死：连续 {self._miss_count} 次 get_status() 无响应"
                f"（窗口 {self._miss_limit * self._probe_interval:.0f}s < {self._window:.0f}s）"
            ),
            source="monitoring.watchdog",
            category="engine",
            details={
                "consecutive_misses": self._miss_count,
                "probe_interval_seconds": self._probe_interval,
                "hang_judge_window_seconds": self._window,
            },
            deduplication_key="watchdog:engine_hung",
        )
        self._hang_alerted = True

    def _recover(self) -> None:
        """引擎恢复响应：发 INFO 恢复并清僵死标记。"""
        try:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = self._alert_manager or get_alert_manager()
            mgr.alert(
                level=SeverityLevel.INFO,
                message="[watchdog] 引擎心跳已恢复",
                source="monitoring.watchdog",
                category="engine",
                deduplication_key="recovered:watchdog:engine_hung",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"引擎心跳恢复告警失败: {e}")
        finally:
            self._hang_alerted = False


# ═══════════════════════════════════════════════
#  A-6 券商连接状态监控（回写#210，EVENT 转换路径）
# ═══════════════════════════════════════════════
class BrokerConnectionWatcher:
    """
    券商连接状态监控（EVENT 转换路径，R-02）。

    BaseBroker 无连接状态推送回调，故以"快速轮询 + 健康→不健康转换点"实现
    事件路径：``on_state(connected)`` 在 connected 由 True→False 时立即触发
    P0/CRITICAL 告警（category=broker，details 带 runbook_id）；由 False→True
    时发 INFO 恢复。周期巡检 ``_check_broker`` 仅作兜底。

    防未来：仅依据"当前"连接状态做转换判定，不预测网络。
    """

    def __init__(
        self,
        broker: Any,
        alert_manager: Any = None,
        runbook_id: str = RUNBOOK_BROKER_DISCONNECT,
        on_disconnect: Optional[Callable[[], None]] = None,
        on_recover: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Parameters
        ----------
        broker : BaseBroker | None
            券商对象（须有 is_connected 属性/方法）。None 时不轮询。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        runbook_id : str
            断连 Runbook 标识（随告警 details 携带，触发 Runbook）。
        on_disconnect / on_recover : Callable | None
            0.5（回写#291）断连/恢复联动回调（用例2 断连禁开仓、恢复仅补发未成交）。
            由装配层（live.scheduler）注入 engine.pause / engine.resume —— watchdog 模块
            不反向依赖 engine，回调异常仅告警不拖垮看门狗（F-3）。
        """
        self._broker = broker
        self._alert_manager = alert_manager
        self._runbook_id = runbook_id
        self._last_connected: Optional[bool] = None  # None=尚未采样
        self._disconnect_alerted: bool = False
        self._on_disconnect = on_disconnect
        self._on_recover = on_recover

    def attach_disconnect_callback(self, cb: Optional[Callable[[], None]]) -> None:
        """装配层注入断连联动回调（用例2：断连→禁开仓）。"""
        self._on_disconnect = cb

    def attach_recover_callback(self, cb: Optional[Callable[[], None]]) -> None:
        """装配层注入恢复联动回调（用例2：恢复→解除禁开仓，仅补发未成交）。"""
        self._on_recover = cb

    def _read_connected(self) -> bool:
        """读取券商连接状态（兼容属性与方法两种 duck-typing）。异常视为未连接。"""
        if self._broker is None:
            return False
        try:
            conn = getattr(self._broker, "is_connected", None)
            if callable(conn):
                conn = conn()
            return bool(conn)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取券商连接状态异常（按未连接处理）: {e}")
            return False

    def poll_once(self) -> Dict[str, Any]:
        """快速轮询一次连接状态并驱动转换判定（事件路径入口）。"""
        connected = self._read_connected()
        return self.on_state(connected)

    def on_state(self, connected: bool, now: Optional[float] = None) -> Dict[str, Any]:
        """
        连接状态转换判定（事件触发核心；单测可直接注入 True→False 验证）。

        Parameters
        ----------
        connected : bool
            当前是否已连接。
        now : float | None
            采样时刻（None 取系统时钟；防未来：仅记录当前）。

        Returns
        -------
        dict : {"connected": bool, "transition": str}
            transition ∈ {"none", "connected->disconnected",
                          "disconnected->connected", "initial"}。
        """
        ts = now if now is not None else time.time()
        transition = "none"
        if self._last_connected is None:
            transition = "initial"
        elif connected != self._last_connected:
            transition = "connected->disconnected" if not connected else "disconnected->connected"

        if transition == "connected->disconnected":
            self._fire_disconnect_alert(ts)
        elif transition == "disconnected->connected":
            self._recover()

        self._last_connected = bool(connected)
        return {"connected": bool(connected), "transition": transition}

    def _fire_disconnect_alert(self, ts: float) -> None:
        """券商断连 P0/CRITICAL 告警（带 category=broker + runbook_id）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = get_alert_manager()
        mgr.alert(
            level=SeverityLevel.CRITICAL,
            message="[watchdog] 券商连接断开（交易中断 P0）",
            source="monitoring.watchdog",
            category="broker",
            details={
                "runbook_id": self._runbook_id,
                "connected": False,
                "disconnected_at": ts,
            },
            deduplication_key="watchdog:broker_disconnected",
        )
        self._disconnect_alerted = True
        # 0.5（回写#291）：断连联动（用例2 禁开仓）——回调异常仅告警不拖垮看门狗（F-3）
        if self._on_disconnect is not None:
            try:
                self._on_disconnect()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[watchdog] 断连联动回调异常（禁开仓未生效）: {e}")

    def _recover(self) -> None:
        """券商重连恢复：发 INFO 恢复。"""
        try:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = self._alert_manager or get_alert_manager()
            mgr.alert(
                level=SeverityLevel.INFO,
                message="[watchdog] 券商连接已恢复",
                source="monitoring.watchdog",
                category="broker",
                details={"runbook_id": self._runbook_id, "connected": True},
                deduplication_key="recovered:watchdog:broker_disconnected",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"券商连接恢复告警失败: {e}")
        finally:
            self._disconnect_alerted = False
        # 0.5（回写#291）：恢复联动（用例2 解除禁开仓，仅补发未成交）——异常仅告警（F-3）
        if self._on_recover is not None:
            try:
                self._on_recover()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[watchdog] 恢复联动回调异常: {e}")

    @property
    def last_connected(self) -> Optional[bool]:
        """最近一次采样的连接状态（供巡检兜底比对）。"""
        return self._last_connected


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：验证 StalenessDetector 核心判定（注入假时钟/假告警）。"""
    from datetime import datetime

    from myquant.utils.time_utils import TZ_SHANGHAI, TradingCalendar

    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name} {info}")

    # 固定时钟 + 固定日历（2026-10-13 周二，活跃上午盘；避开国庆假期）
    # 开盘 09:30；grace=5min=300s；bar_timeout=600s（测试值，使降级带 (grace, timeout] 非空）
    fixed_now = [datetime(2026, 10, 13, 9, 32, 0, tzinfo=TZ_SHANGHAI).timestamp()]

    def clock() -> float:
        return fixed_now[0]

    cal = TradingCalendar()
    alerts: List[dict] = []

    class FakeAlert:
        def alert(self, **kw):
            alerts.append(kw)
            return 1

    det = StalenessDetector(
        bar_timeout_seconds=600.0, session_grace_minutes=5.0,
        calendar=cal, clock=clock, alert_manager=FakeAlert(),
    )
    # 1) 09:32（开盘后 2min < grace 5min）、无 bar → healthy 宽限期
    st, d = det.check()
    check("A4 开盘宽限期内 healthy", st == "healthy" and "宽限" in d.get("reason", ""), f"got {st} {d}")
    # 2) 09:40（开盘后 10min=600s > grace 300s，<= bar_timeout 600s）→ degraded 数据源未就绪
    fixed_now[0] = datetime(2026, 10, 13, 9, 40, 0, tzinfo=TZ_SHANGHAI).timestamp()
    st, d = det.check()
    check("A4 开盘超时未就绪 degraded", st == "degraded" and "未就绪" in d.get("reason", ""), f"got {st} {d}")
    # 3) 10:30 收到一根 bar，10:31 检查（bar 新鲜）→ healthy
    det.on_bar(ts=datetime(2026, 10, 13, 10, 30, 0, tzinfo=TZ_SHANGHAI).timestamp())
    st, d = det.check(now=datetime(2026, 10, 13, 10, 31, 0, tzinfo=TZ_SHANGHAI).timestamp())
    check("A4 有 bar 且新鲜 healthy", st == "healthy", f"got {st} {d}")
    # 4) 10:41 检查（距 10:30 bar 已 660s > bar_timeout 600s）→ down 断流
    st, d = det.check(now=datetime(2026, 10, 13, 10, 41, 0, tzinfo=TZ_SHANGHAI).timestamp())
    check("A4 断流 down", st == "down" and "断流" in d.get("reason", ""), f"got {st} {d}")
    # 5) evaluate 在 down 时发 ERROR
    alerts.clear()
    det.evaluate(now=datetime(2026, 10, 13, 10, 41, 0, tzinfo=TZ_SHANGHAI).timestamp())
    check("A4 断流发 ERROR 告警", any(a["level"].name == "ERROR" for a in alerts), f"alerts={alerts}")
    # 6) 午休时段豁免（12:00）
    st, d = det.check(now=datetime(2026, 10, 13, 12, 0, 0, tzinfo=TZ_SHANGHAI).timestamp())
    check("A4 午休豁免 healthy", st == "healthy" and d.get("exempt"), f"got {st} {d}")
    # 7) P99 统计
    for dt in [datetime(2026, 10, 13, 10, i, 0, tzinfo=TZ_SHANGHAI).timestamp() for i in range(31, 40)]:
        det.on_bar(ts=dt)
    p99 = det.get_stats()["bar_interval_p99_seconds"]
    check("A4 P99 统计可计算", p99 is not None and p99 > 0, f"p99={p99}")

    # ── A-5 EngineHeartbeatWatcher ──
    class AliveEngine:
        def get_status(self):
            return {"state": "RUNNING"}

    class HangEngine:
        def get_status(self):
            raise RuntimeError("boom")

    hb = EngineHeartbeatWatcher(
        AliveEngine(), probe_interval_seconds=10, consecutive_misses=5,
        hang_judge_window_seconds=60, alert_manager=FakeAlert(),
    )
    r = hb.probe()
    check("A5 存活引擎响应", r["responding"] and r["miss_count"] == 0, f"r={r}")
    hb2 = EngineHeartbeatWatcher(
        HangEngine(), probe_interval_seconds=10, consecutive_misses=5,
        hang_judge_window_seconds=60, alert_manager=FakeAlert(),
    )
    alerts.clear()
    for _ in range(5):
        r = hb2.probe()
    check("A5 连续5次无响应判 hung", r["hung"] is True and r["miss_count"] == 5, f"r={r}")
    check("A5 僵死发 ERROR 告警", any(a["level"].name == "ERROR" for a in alerts), f"alerts={alerts}")

    # ── A-6 BrokerConnectionWatcher ──
    from myquant.monitoring.watchdog import BrokerConnectionWatcher
    bw = BrokerConnectionWatcher(object(), alert_manager=FakeAlert())
    alerts.clear()
    bw.on_state(True)
    r1 = bw.on_state(False)  # 健康→不健康转换
    check("A6 True->False 转换", r1["transition"] == "connected->disconnected", f"r1={r1}")
    check("A6 断连发 CRITICAL 告警", any(a["level"].name == "CRITICAL" for a in alerts), f"alerts={alerts}")
    check("A6 告警带 runbook/category",
          any(a.get("category") == "broker" and a.get("details", {}).get("runbook_id") for a in alerts),
          f"alerts={alerts}")
    r2 = bw.on_state(True)  # 恢复
    check("A6 恢复转换", r2["transition"] == "disconnected->connected", f"r2={r2}")

    print("=" * 60)
    print(f"watchdog.py 自测: {passed} 项通过")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"失败 {len(failures)} 项")
        return 1
    print("全部通过 ✅")
    print("=" * 60)
    return 0


# ═══════════════════════════════════════════════
#  G2-5 阶段二：装配工厂（注册表化）
#  live/scheduler 经 core.registry.get_component 取用并调用，消除
#  scheduler → monitoring.watchdog 反向依赖（EX-13D）。
#  模块导入即注册；装配责任上移（run_实盘入口.py setup_monitoring_linkage 触发）。
#  工厂函数签名与类构造一致，支持注入可测参数（R-04 阈值由类内读 settings.yaml）。
# ═══════════════════════════════════════════════

def get_staleness_detector(**kwargs: Any) -> StalenessDetector:
    """行情活性看门狗装配工厂（A-4）。

    Parameters
    ----------
    **kwargs : Any
        透传 StalenessDetector 构造参数（bar_timeout_seconds 等，None 读 R-04 真源）。

    Returns
    -------
    StalenessDetector : 行情活性检测器实例。
    """
    return StalenessDetector(**kwargs)


def get_engine_heartbeat_watcher(engine: Any, **kwargs: Any) -> EngineHeartbeatWatcher:
    """引擎心跳看门狗装配工厂（A-5）。

    Parameters
    ----------
    engine : Any
        实盘引擎（须有 get_status()）。
    **kwargs : Any
        透传 EngineHeartbeatWatcher 构造参数（probe_interval_seconds 等）。

    Returns
    -------
    EngineHeartbeatWatcher : 引擎心跳检测器实例。
    """
    return EngineHeartbeatWatcher(engine, **kwargs)


def get_broker_connection_watcher(broker: Any, **kwargs: Any) -> BrokerConnectionWatcher:
    """券商连接看门狗装配工厂（A-6）。

    Parameters
    ----------
    broker : Any
        券商对象（须有 is_connected 属性/方法）。
    **kwargs : Any
        透传 BrokerConnectionWatcher 构造参数（alert_manager/runbook_id 等）。

    Returns
    -------
    BrokerConnectionWatcher : 券商连接状态监控实例。
    """
    return BrokerConnectionWatcher(broker, **kwargs)


#: G2-5 阶段二：装配工厂注册（模块导入即注册；scheduler 经 get_component 取用）
register_component("staleness_detector", get_staleness_detector)
register_component("engine_heartbeat_watcher", get_engine_heartbeat_watcher)
register_component("broker_connection_watcher", get_broker_connection_watcher)


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
