# 文件名：myquant/monitoring/rate_limit_monitor.py
"""
rate_limit_monitor.py - 交易所限流/频控监控（A-12，回写#216）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》A-12：
  统计单位时间窗口内的报单/查询/撤单频率（滑动窗口计数，按动作类型分别统计）；
  接近交易所频控阈值 → P1 预警（触发封禁即交易中断）；
  到阈值前自动暂停开仓（配合熔断暂停策略，可注入 pause/resume hook，动作留痕，禁止静默）。

触发模式（R-02）：POLL + 动作事件实时驱动
  - observe_order / observe_query / observe_cancel 由报单/查询/撤单动作实时调用
    （或经 attach_event_bus 订阅 EventType.ORDER_SUBMIT / ORDER_CANCELLED）；
  - evaluate() 供快周期巡检兜底（窗口滑动后复核预警/暂停是否解除）。

设计原则（铁律）:
  1. 阈值唯一真源 R-04：全部数值阈值经 myquant.core.settings.get_monitoring_config()
     读取 config/settings.yaml 的 monitoring.slo.exchange_rate_limit，代码内不硬编码业务阈值。
  2. 严格防未来：只消费"已发生"动作的时间戳（event_ts / 探测时刻），滑动窗口仅剔除
     now - window 之前的历史样本，不预测下一笔报单。
  3. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  4. 暂停开仓只做可回滚、留痕的动作：经注入的 pause_hook/resume_hook 执行，
     每次暂停/恢复均发告警 + 记日志，绝不静默。

依赖: time, collections, myquant.core.{settings,constants,logger}
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict, List, Optional

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.rate_limit")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04）
# ─────────────────────────────────────────────
#: 受监控的动作类型（报单 / 查询 / 撤单）
ACTION_ORDER: str = "order"
ACTION_QUERY: str = "query"
ACTION_CANCEL: str = "cancel"
_ALL_ACTIONS: tuple = (ACTION_ORDER, ACTION_QUERY, ACTION_CANCEL)

#: 恢复迟滞系数：占比回落到 warn_ratio × (1-此系数) 以下才解除暂停（防边界抖动反复暂停/恢复）
RESUME_HYSTERESIS: float = 0.5

#: 各动作类型对应的累计计数指标名（与 BuiltinMetrics 注册一致）
_METRIC_BY_ACTION: Dict[str, str] = {
    ACTION_ORDER: "rate_limit_orders_total",
    ACTION_QUERY: "rate_limit_queries_total",
    ACTION_CANCEL: "rate_limit_cancels_total",
}


class ExchangeRateLimitMonitor:
    """
    交易所限流/频控监控器（滑动窗口计数 + 接近阈值 P1 预警 + 到阈值前自动暂停开仓）。

    对报单/查询/撤单三类动作分别维护一个长度 = window_seconds 的滑动时间窗；
    每次 observe_* 记录该动作的发生时刻（event_ts），剔除窗口外历史样本后得到窗口内计数。

    告警与制动分级（阈值 R-04，读 slo.exchange_rate_limit）:
      - warn_ratio（默认 0.8）: 窗口计数 ≥ 上限 × warn_ratio
          → P1/WARNING 预警（接近频控阈值，面临封禁交易中断风险）
          → 同时自动暂停开仓（pause_hook，可回滚，全程留痕）。
      - 1.0: 窗口计数 ≥ 上限
          → ERROR 告警（已达频控阈值，随时可能被交易所封禁）。
      - 全部动作占比回落到 warn_ratio × (1 - RESUME_HYSTERESIS) 以下
          → 解除暂停（resume_hook）+ INFO 恢复。

    防未来：只记录/统计"已发生"动作时间戳；滑动窗口剔除仅依据 now - window 之前的样本，
    本行使用的数据截至 now 时刻。

    使用方式:
        from myquant.monitoring.rate_limit_monitor import ExchangeRateLimitMonitor

        mon = ExchangeRateLimitMonitor()
        mon.attach_pause_hook(lambda reason: engine.pause(reason=reason))
        mon.attach_event_bus(bus)          # 订阅报单/撤单事件
        # 每次动作：mon.observe_order(); mon.observe_query(); mon.observe_cancel()
        status = mon.get_status()
    """

    def __init__(
        self,
        window_seconds: Optional[float] = None,
        order_per_minute: Optional[int] = None,
        query_per_minute: Optional[int] = None,
        cancel_per_minute: Optional[int] = None,
        warn_ratio: Optional[float] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        window_seconds : float | None
            滑动窗口长度（秒）。None 读 slo.exchange_rate_limit.window_seconds。
        order_per_minute / query_per_minute / cancel_per_minute : int | None
            各动作每分钟上限。None 读对应 slo.exchange_rate_limit.*_per_minute。
        warn_ratio : float | None
            预警/暂停触发比例（0,1]。None 读 slo.exchange_rate_limit.warn_ratio。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（进 /api/metrics）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。

        Raises
        ------
        ValueError : 阈值非法（窗口/上限 <=0、warn_ratio 不在 (0,1]）时抛出（拒绝脏配置运行）。
        """
        slo = get_monitoring_config().get("slo", {}).get("exchange_rate_limit", {})

        self._window: float = (
            float(window_seconds) if window_seconds is not None
            else float(slo.get("window_seconds", 60))
        )
        self._limits: Dict[str, int] = {
            ACTION_ORDER: (
                int(order_per_minute) if order_per_minute is not None
                else int(slo.get("order_per_minute", 300))
            ),
            ACTION_QUERY: (
                int(query_per_minute) if query_per_minute is not None
                else int(slo.get("query_per_minute", 600))
            ),
            ACTION_CANCEL: (
                int(cancel_per_minute) if cancel_per_minute is not None
                else int(slo.get("cancel_per_minute", 300))
            ),
        }
        self._warn_ratio: float = (
            float(warn_ratio) if warn_ratio is not None
            else float(slo.get("warn_ratio", 0.8))
        )

        # 防御式校验（拒绝脏配置运行）
        if self._window <= 0:
            raise ValueError(f"window_seconds 必须 > 0，实际 {self._window!r}")
        for action, limit in self._limits.items():
            if limit <= 0:
                raise ValueError(f"{action}_per_minute 必须 > 0，实际 {limit!r}")
        if not (0.0 < self._warn_ratio <= 1.0):
            raise ValueError(
                f"warn_ratio 必须在 (0,1]，实际 {self._warn_ratio!r}"
            )

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector

        # 滑动窗口状态：每个动作类型一个时间戳队列（全部为"已发生"事实）
        self._windows: Dict[str, Deque[float]] = {
            a: deque() for a in _ALL_ACTIONS
        }
        # 当前处于"超预警线"的动作集合（暂停/恢复判定用）
        self._warn_actions: set = set()
        self._paused: bool = False
        # 可注入的暂停/恢复钩子（配合熔断/引擎 pause；默认 None=仅可观测留痕）
        self._pause_hook: Optional[Callable[[str], None]] = None
        self._resume_hook: Optional[Callable[[], None]] = None
        self._handler_ids: List[str] = []
        logger.debug(
            "ExchangeRateLimitMonitor 初始化: window=%.0fs limits=%s warn_ratio=%.2f",
            self._window, self._limits, self._warn_ratio,
        )

    # ── 钩子装配（可回滚的暂停/恢复动作；禁止静默）──────────────
    def attach_pause_hook(self, hook: Callable[[str], None]) -> None:
        """
        装配"暂停开仓"动作钩子（到达预警线时调用，参数为原因字符串）。

        Parameters
        ----------
        hook : Callable[[str], None]
            如 lambda reason: engine.pause(reason=reason)；None 等价于不装配（仅观测告警）。
        """
        if hook is not None and not callable(hook):
            raise ValueError("pause_hook 必须是可调用对象")
        self._pause_hook = hook

    def attach_resume_hook(self, hook: Callable[[], None]) -> None:
        """装配"恢复开仓"动作钩子（窗口压力回落时调用）。"""
        if hook is not None and not callable(hook):
            raise ValueError("resume_hook 必须是可调用对象")
        self._resume_hook = hook

    # ── 事件链路：订阅报单/撤单事件 ───────────────────────────
    def attach_event_bus(self, bus: Any) -> None:
        """
        订阅 EventBus 的报单/撤单事件，到达即计入对应滑动窗口。

        Parameters
        ----------
        bus : EventBus
            事件总线。订阅失败记 warning，不阻断（observe_* 仍可被业务直接调用）。
        """
        if bus is None:
            return
        try:
            from myquant.core.event_bus import EventType

            self._handler_ids.append(
                bus.subscribe(EventType.ORDER_SUBMIT, self._handle_order_submit)
            )
            self._handler_ids.append(
                bus.subscribe(EventType.ORDER_CANCELLED, self._handle_order_cancelled)
            )
            logger.info("ExchangeRateLimitMonitor 已订阅 EventBus ORDER_SUBMIT/ORDER_CANCELLED")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ExchangeRateLimitMonitor 订阅 EventBus 失败（observe_* 仍可用）: {e}")

    def _handle_order_submit(self, event: Any) -> None:
        """事件总线报单事件：优先取业务时刻 event_ts，回退事件创建时刻。"""
        try:
            data = getattr(event, "data", None) or {}
            ts = data.get("event_ts") or getattr(event, "timestamp", None)
            self.observe_order(ts=ts)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"处理报单事件异常: {e}")

    def _handle_order_cancelled(self, event: Any) -> None:
        """事件总线撤单事件：优先取业务时刻 event_ts。"""
        try:
            data = getattr(event, "data", None) or {}
            ts = data.get("event_ts") or getattr(event, "timestamp", None)
            self.observe_cancel(ts=ts)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"处理撤单事件异常: {e}")

    # ── 动作观测入口（实时驱动）──────────────────────────────
    def observe_order(self, ts: Optional[float] = None) -> None:
        """记录一次报单动作（事件/实时驱动入口）。"""
        self._observe(ACTION_ORDER, ts=ts)

    def observe_query(self, ts: Optional[float] = None) -> None:
        """记录一次查询/行情拉取动作。"""
        self._observe(ACTION_QUERY, ts=ts)

    def observe_cancel(self, ts: Optional[float] = None) -> None:
        """记录一次撤单动作。"""
        self._observe(ACTION_CANCEL, ts=ts)

    def _observe(self, action: str, ts: Optional[float]) -> None:
        """
        记录某动作一次发生并驱动窗口计数 + 预警/暂停判定。

        Parameters
        ----------
        action : str
            ACTION_ORDER / ACTION_QUERY / ACTION_CANCEL。
        ts : float | None
            动作业务发生时刻（epoch 秒）。None 取当前系统时钟。
            防未来：仅记录传入/当前时刻，不引用未来数据。
        """
        if action not in self._windows:
            raise ValueError(f"未知动作类型: {action!r}")
        now = self._clock() if ts is None else float(ts)
        if now < 0:
            logger.warning("动作时间戳为负（%s），忽略本次观测", now)
            return
        # 防未来：本行使用的数据截至 now 时刻（仅追加 now 之前/等于 now 的样本）
        self._windows[action].append(now)
        self._inc_metric(action)
        self._evaluate(now)

    def _inc_metric(self, action: str) -> None:
        """累计计数指标 +1（进 /api/metrics）；采集器不可用仅记 warning 不阻断。"""
        col = self._metrics_collector
        if col is None:
            try:
                from myquant.monitoring.metrics_collector import get_metrics_collector
                col = get_metrics_collector()
                self._metrics_collector = col
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取 metrics_collector 失败（指标未计数）: {e}")
                return
        try:
            col.inc_counter(_METRIC_BY_ACTION[action], 1.0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"限流指标 {action} 计数失败（不阻断检测）: {e}")

    # ── 核心判定：滑动窗口计数 → P1 预警 → 暂停开仓 ──────────
    def _purge(self, dq: Deque[float], now: float) -> None:
        """
        剔除滑动窗口之外的历史样本。

        防未来：仅保留 [now - window, now] 内的样本；本行使用的数据截至 now 时刻。
        """
        cutoff = now - self._window
        while dq and dq[0] < cutoff:
            dq.popleft()

    def window_counts(self, now: Optional[float] = None) -> Dict[str, int]:
        """
        返回各动作当前滑动窗口内的计数（纯查询，不告警）。

        Parameters
        ----------
        now : float | None
            评估时刻（None 取当前时钟）。

        Returns
        -------
        dict : {action: count}
        """
        t = self._clock() if now is None else float(now)
        result: Dict[str, int] = {}
        for action, dq in self._windows.items():
            self._purge(dq, t)  # 防未来：仅截至 t 时刻的样本
            result[action] = len(dq)
        return result

    def evaluate(self, now: Optional[float] = None) -> Dict[str, Any]:
        """
        评估各动作窗口压力并驱动告警/暂停/恢复（POLL 兜底入口）。

        Returns
        -------
        dict : 各动作窗口计数、占比、当前是否暂停。
        """
        t = self._clock() if now is None else float(now)
        return self._evaluate(t)

    def _evaluate(self, now: float) -> Dict[str, Any]:
        """内部评估：逐动作算占比，分级告警并维护暂停/恢复状态。"""
        counts = self.window_counts(now)
        over_warn: set = set()
        result: Dict[str, Any] = {"counts": counts, "ratios": {}, "paused": self._paused}

        for action, count in counts.items():
            limit = self._limits[action]
            ratio = count / limit if limit > 0 else 0.0
            result["ratios"][action] = round(ratio, 3)
            if ratio >= 1.0:
                self._fire_limit_alert(action, count, limit, ratio)
                over_warn.add(action)
            elif ratio >= self._warn_ratio:
                self._fire_warn_alert(action, count, limit, ratio)
                over_warn.add(action)

        # 暂停/恢复闭环（可回滚、全程留痕）
        if over_warn and not self._paused:
            self._do_pause(over_warn)
        elif not over_warn and self._paused:
            # 迟滞：仅当全部动作占比回落到 warn_ratio×(1-hyst) 以下才恢复
            resume_below = self._warn_ratio * (1.0 - RESUME_HYSTERESIS)
            all_below = all(
                (counts[a] / self._limits[a]) < resume_below for a in _ALL_ACTIONS
            )
            if all_below:
                self._do_resume()

        self._warn_actions = over_warn
        result["paused"] = self._paused
        return result

    # ── 告警 ────────────────────────────────────────────────
    def _fire_warn_alert(self, action: str, count: int, limit: int, ratio: float) -> None:
        """接近频控阈值 P1/WARNING 预警（固定去重键，窗口内不刷屏）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = get_alert_manager()
        mgr.alert(
            level=SeverityLevel.WARNING,
            message=(
                f"[rate-limit] {action} 频率接近交易所频控阈值：窗口内 {count}/{limit}"
                f"（{ratio:.0%}），已自动暂停开仓"
            ),
            source="monitoring.rate_limit",
            category="rate_limit",
            details={
                "action": action,
                "window_count": count,
                "limit_per_window": limit,
                "ratio": round(ratio, 3),
                "window_seconds": self._window,
                "paused": True,
            },
            deduplication_key=f"ratelimit:{action}:warn",
        )

    def _fire_limit_alert(self, action: str, count: int, limit: int, ratio: float) -> None:
        """已达频控阈值 ERROR 告警（面临交易所封禁、交易中断风险）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager
            mgr = get_alert_manager()
        mgr.alert(
            level=SeverityLevel.ERROR,
            message=(
                f"[rate-limit] {action} 已达交易所频控阈值：窗口内 {count}/{limit}"
                f"（{ratio:.0%}），面临封禁/交易中断"
            ),
            source="monitoring.rate_limit",
            category="rate_limit",
            details={
                "action": action,
                "window_count": count,
                "limit_per_window": limit,
                "ratio": round(ratio, 3),
                "window_seconds": self._window,
                "paused": True,
            },
            deduplication_key=f"ratelimit:{action}:limit",
        )

    # ── 暂停/恢复（可回滚、留痕，禁止静默）────────────────────
    def _do_pause(self, over_warn: set) -> None:
        """执行暂停开仓：记日志留痕 + 调 pause_hook（若装配）。"""
        self._paused = True
        reason = (
            f"exchange rate-limit near threshold: actions={sorted(over_warn)} "
            f"warn_ratio={self._warn_ratio:.2f}"
        )
        logger.warning("自动暂停开仓（到频控阈值前制动）: %s", reason)
        if self._pause_hook is not None:
            try:
                self._pause_hook(reason)
            except Exception as e:  # noqa: BLE001
                logger.error(f"调用 pause_hook 暂停开仓失败（已留痕告警）: {e}")

    def _do_resume(self) -> None:
        """解除暂停开仓：调 resume_hook（若装配）+ INFO 恢复。"""
        self._paused = False
        logger.info("频控窗口压力回落，恢复开仓")
        if self._resume_hook is not None:
            try:
                self._resume_hook()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"调用 resume_hook 恢复开仓失败: {e}")
        try:
            mgr = self._alert_manager
            if mgr is None:
                from myquant.monitoring.alerting import get_alert_manager
                mgr = get_alert_manager()
            mgr.alert(
                level=SeverityLevel.INFO,
                message="[rate-limit] 频控窗口压力回落，已恢复开仓",
                source="monitoring.rate_limit",
                category="rate_limit",
                details={"paused": False},
                deduplication_key="recovered:ratelimit:pause",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"频控恢复告警失败: {e}")

    # ── 状态查询（可观测）────────────────────────────────────
    def get_status(self) -> Dict[str, Any]:
        """
        返回监控状态快照（供巡检报告 / 看板暴露）。

        含各动作窗口计数、占比、当前是否暂停。防未来：仅截至当前时刻样本。
        """
        now = self._clock()
        counts = self.window_counts(now)
        ratios = {
            a: round(counts[a] / self._limits[a], 3) if self._limits[a] > 0 else 0.0
            for a in _ALL_ACTIONS
        }
        return {
            "window_seconds": self._window,
            "limits": dict(self._limits),
            "warn_ratio": self._warn_ratio,
            "window_counts": counts,
            "ratios": ratios,
            "paused": self._paused,
            "warn_actions": sorted(self._warn_actions),
        }

    @property
    def paused(self) -> bool:
        """当前是否因频控接近阈值而暂停开仓（只读状态）。"""
        return self._paused


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：注入固定时钟/假告警/假钩子，验证滑动窗口→P1预警→暂停闭环。"""
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name} {info}")

    class FakeAlert:
        def __init__(self):
            self.calls = []

        def alert(self, **kw):
            self.calls.append(kw)
            return 1

    class FakeMetrics:
        def __init__(self):
            self.counts = defaultdict(int)

        def inc_counter(self, name, amount=1.0, labels=None):
            self.counts[name] += int(amount)

    # 固定时钟：从 t0=1000.0 起手动推进
    clock_now = [1000.0]

    def clock() -> float:
        return clock_now[0]

    am = FakeAlert()
    met = FakeMetrics()
    pause_calls: List[str] = []
    resume_calls: List[int] = []

    # 上限 order=5/窗口, warn_ratio=0.8 → 4 笔即 P1 预警+暂停
    mon = ExchangeRateLimitMonitor(
        window_seconds=60.0, order_per_minute=5, query_per_minute=100,
        cancel_per_minute=100, warn_ratio=0.8,
        alert_manager=am, metrics_collector=met, clock=clock,
    )
    mon.attach_pause_hook(lambda reason: pause_calls.append(reason))
    mon.attach_resume_hook(lambda: resume_calls.append(1))

    # 1) 3 笔报单（< 4 预警线）→ 不告警不暂停
    for i in range(3):
        mon.observe_order(ts=1000.0 + i)
    check("1) 未达预警线不告警不暂停", am.calls == [] and not mon.paused, f"calls={am.calls}")
    # 2) 第 4 笔（达 0.8×5=4）→ P1 预警 + 暂停调用发生
    mon.observe_order(ts=1000.0 + 3)
    warn = [c for c in am.calls if c["deduplication_key"] == "ratelimit:order:warn"]
    check("2) 第4笔触发P1预警", len(warn) == 1 and warn[0]["level"].name == "WARNING", f"calls={am.calls}")
    check("2) 暂停开仓钩子被调用", len(pause_calls) == 1 and mon.paused, f"pause={pause_calls}")
    # 3) 累计计数指标已写入
    check("3) rate_limit_orders_total 累计=4", met.counts["rate_limit_orders_total"] == 4, f"met={met.counts}")
    # 4) 再到第 5 笔（达上限 1.0）→ ERROR 告警
    am.calls.clear()
    mon.observe_order(ts=1000.0 + 4)
    lim = [c for c in am.calls if c["deduplication_key"] == "ratelimit:order:limit"]
    check("4) 达上限触发ERROR告警", len(lim) == 1 and lim[0]["level"].name == "ERROR", f"calls={am.calls}")
    # 5) 窗口滑动 60s 后老样本剔除，计数回落 → 自动恢复
    clock_now[0] = 1100.0  # 已超 60s 窗口
    am.calls.clear()
    mon.evaluate(now=1100.0)
    check("5) 窗口回落自动恢复开仓", (not mon.paused) and len(resume_calls) == 1, f"paused={mon.paused} resume={resume_calls}")
    # 6) 查询/撤单独立窗口分别计数
    mon2 = ExchangeRateLimitMonitor(
        window_seconds=60.0, order_per_minute=100, query_per_minute=2,
        cancel_per_minute=100, warn_ratio=0.8, alert_manager=FakeAlert(),
        metrics_collector=FakeMetrics(), clock=clock,
    )
    clock_now[0] = 1001.5  # 复位时钟到样本窗口内（步骤5曾推进到1100）
    for i in range(2):  # query 达上限
        mon2.observe_query(ts=1000.0 + i)
    st = mon2.get_status()
    check("6) 查询/撤单按动作分别统计", st["window_counts"]["query"] == 2 and st["window_counts"]["order"] == 0, f"st={st['window_counts']}")
    # 7) 防御式非法阈值抛错
    raised = False
    try:
        ExchangeRateLimitMonitor(order_per_minute=0, alert_manager=FakeAlert())
    except ValueError:
        raised = True
    check("7) 非法上限抛 ValueError", raised, "")
    # 8) 未知动作类型抛错
    raised2 = False
    try:
        mon._observe("bogus", ts=1000.0)
    except ValueError:
        raised2 = True
    check("8) 未知动作类型抛 ValueError", raised2, "")

    print("=" * 60)
    print(f"rate_limit_monitor.py 自测: {passed} 项通过")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"失败 {len(failures)} 项")
        return 1
    print("全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
