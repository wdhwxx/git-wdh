# 文件名：myquant/monitoring/rolling_performance.py
"""
rolling_performance.py - 滚动绩效指标监控（C-2，回写#232）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》C-2：
  滚动窗口（日/周/月）收益、夏普、回撤、胜率；日内回撤/亏损超策略阈值告警；
  基于 metrics_history 回放。

触发模式（R-02）：周期（POLL）
  - C-2 触发列为"周期"，本模块以 poll() 周期巡检复核告警 + 调用方按需
    record()/replay_from_history() 喂"已发生"绩效点后计算滚动绩效面板。

计算口径（全部基于"已发生"的绩效点，严格防未来）：
  - 绩效点 = (timestamp, equity)，按策略归组；只消费 timestamp<=now 的点
    （record() 拒绝未来点）。
  - 日序列：把绩效点按自然日聚合，取每日最后一个 equity 为该日日末权益；
    日收益 = (当日权益 / 前一交易日权益 - 1) × 100。
  - 滚动窗口（日/周/月 = 1/7/30 个自然日，trailing 至最新日）：
      - 收益 return_pct   = (窗口末权益 / 窗口首权益 - 1) × 100；
      - 夏普 Sharpe        = mean(日收益)/std(日收益) × sqrt(252)（样本<2 或 std=0 → None）；
      - 回撤 drawdown_pct  = 窗口内权益路径的最大峰谷回撤（%）；
      - 胜率 win_rate      = 窗口内日收益>0 的天数占比 × 100（无日收益样本 → None）。
  - 日内回撤 = 当日（today）权益样本内的最大峰谷回撤；
    日内亏损 = 当日首末权益收益（负值=亏）。超阈值即预警（阈值未配置则只计算不告警）。

阈值真源（R-04）：config/settings.yaml 的 monitoring.slo.strategy.intraday_drawdown_limit，
  经 myquant.core.settings.get_monitoring_config() 读取：
    - intraday_drawdown_limit ：日内回撤/亏损阈值（按策略锁定），PENDING_CAL（None）
      → 不触发日内告警、不编造数值；指标照常计算输出。

设计原则（铁律）:
  1. 阈值唯一真源 R-04：业务阈值一律经 get_monitoring_config() 读取或构造注入，
     代码内不硬编码业务阈值（PENDING_CAL 项为 None 时即"不启用告警"，绝不编造）。
  2. 严格防未来：滚动/日内计算只消费"已发生"（timestamp<=now）的绩效点，
     last_poll_time 仅记录计算发生时刻，不预测未来收益。
  3. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  4. 基于 metrics_history 回放：replay_from_history() 从 MetricsHistoryStore.query()
     拉取指标点（name/labels/时间范围，order=asc），按策略注入本监控，供滚动面板回放。
"""

from __future__ import annotations

import math
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.rolling_performance")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04 或构造注入）
# ─────────────────────────────────────────────
#: 告警分类（决定默认 Runbook 兜底，A-10）
CATEGORY_STRATEGY: str = "strategy"

#: 缺省策略名（replay 无 strategy 标签时归组用）
DEFAULT_STRATEGY: str = "default"

#: 滚动窗口自然日数（日/周/月）
WINDOW_DAYS: Tuple[int, int, int] = (1, 7, 30)

#: 年化天数（夏普年化，交易日近似）
TRADING_DAYS_PER_YEAR: float = 252.0

#: 日内回撤告警去重键前缀（后缀拼策略名，固定窗口内不刷屏）
DEDUP_INTRADAY_DRAWDOWN_PREFIX: str = "strategy:intraday_drawdown:"
#: 日内亏损告警去重键前缀
DEDUP_INTRADAY_LOSS_PREFIX: str = "strategy:intraday_loss:"

#: 每策略绩效点缓存上限（超出丢最旧，避免无界增长）
MAX_POINTS_PER_STRATEGY: int = 5000

#: 指标名（set_gauge 进 collector，labels={"strategy": 策略名}）
METRIC_RETURN_1D: str = "rolling_performance_return_1d"
METRIC_RETURN_7D: str = "rolling_performance_return_7d"
METRIC_RETURN_30D: str = "rolling_performance_return_30d"
METRIC_SHARPE_30D: str = "rolling_performance_sharpe_30d"
METRIC_DRAWDOWN_30D: str = "rolling_performance_drawdown_30d"
METRIC_WINRATE_30D: str = "rolling_performance_winrate_30d"
METRIC_INTRADAY_DRAWDOWN: str = "strategy_intraday_drawdown"
METRIC_INTRADAY_RETURN: str = "strategy_intraday_return"

#: 本模块注册的全部指标名（供主控/驾驶舱查询）
REGISTERED_METRICS: List[str] = [
    METRIC_RETURN_1D,
    METRIC_RETURN_7D,
    METRIC_RETURN_30D,
    METRIC_SHARPE_30D,
    METRIC_DRAWDOWN_30D,
    METRIC_WINRATE_30D,
    METRIC_INTRADAY_DRAWDOWN,
    METRIC_INTRADAY_RETURN,
]

#: 未来点判定容差（秒）：timestamp 超过 now 的绝对上限
_FUTURE_EPS: float = 1.0


class RollingPerformanceMonitor:
    """
    滚动绩效指标监控器（周期巡检 + 基于已发生绩效点的滚动/日内绩效面板）。

    计算口径见模块 docstring。告警分级（阈值 R-04，读 slo.strategy，PENDING_CAL）:
      - 日内回撤 > intraday_drawdown_limit（配置后）
          → WARNING 预警（category=strategy，dedup=strategy:intraday_drawdown:<name>）。
      - 日内亏损（当日收益为负且 |收益| > 阈值，配置后）
          → WARNING 预警（dedup=strategy:intraday_loss:<name>）。
      阈值未配置（None=PENDING_CAL）→ 只计算滚动/日内绩效输出，不触发告警、不编造。

    防未来：record() 拒绝 timestamp>now 的绩效点；滚动/日内计算只消费"已发生"
    的点。本行使用的数据截至各绩效点时间戳。

    使用方式:
        from myquant.monitoring.rolling_performance import RollingPerformanceMonitor

        mon = RollingPerformanceMonitor()                  # 阈值读 slo.strategy
        mon.record("trend_a", ts, equity)                  # 喂已发生绩效点
        mon.replay_from_history(store, metric_name)        # 基于 metrics_history 回放
        state = mon.poll()                                 # 周期巡检：计算+复核告警
        panel = mon.to_dict()                              # 供驾驶舱消费
    """

    def __init__(
        self,
        intraday_drawdown_limit: Optional[float] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
        max_points_per_strategy: int = MAX_POINTS_PER_STRATEGY,
    ) -> None:
        """
        Parameters
        ----------
        intraday_drawdown_limit : float | None
            日内回撤/亏损阈值。None 读 slo.strategy.intraday_drawdown_limit；
            仍为 None（PENDING_CAL 未配置）→ 不触发日内告警，不编造数值。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（进 /api/metrics）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        max_points_per_strategy : int
            每策略绩效点缓存上限（超出丢最旧），必须为正整数。
        """
        if max_points_per_strategy <= 0:
            raise ValueError(f"max_points_per_strategy 必须为正整数，收到 {max_points_per_strategy}")

        slo = get_monitoring_config().get("slo", {}).get("strategy", {})

        # ── 阈值（R-04；构造注入优先，回退配置；None=PENDING_CAL 未配置=不启用告警）──
        self._intraday_drawdown_limit: Optional[float] = self._as_optional_float(
            intraday_drawdown_limit, "intraday_drawdown_limit"
        ) if intraday_drawdown_limit is not None else self._as_optional_float(
            slo.get("intraday_drawdown_limit"), "intraday_drawdown_limit"
        )

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector
        self._max_points = int(max_points_per_strategy)

        # 每策略绩效点缓存：{strategy: [(timestamp, equity), ...]}（按时间升序）
        self._points: Dict[str, List[Tuple[float, float]]] = {}

        # 可观测状态（poll() 写入）
        self._last_poll_time: Optional[float] = None
        self._alert_triggered: bool = False
        self._degraded: bool = False
        self._last_state: Dict[str, Any] = {}

        logger.debug(
            "RollingPerformanceMonitor 初始化: intraday_drawdown_limit=%s max_points=%s",
            self._intraday_drawdown_limit, self._max_points,
        )

    # ── 防御式数值解析（None/非数 → None=未启用；拒绝负阈值）──────────────
    @staticmethod
    def _as_optional_float(value: Any, name: str) -> Optional[float]:
        """
        把配置/注入值解析为可选 float；None 或 YAML 的 ~ 视为"未配置=未启用"。

        Parameters
        ----------
        value : Any
            原始配置值（可能为 None / 数字 / 字符串）。
        name : str
            参数名（日志/异常用）。

        Returns
        -------
        float | None : 解析后的正值；未配置返回 None。

        Raises
        ------
        ValueError : 阈值为负数时抛出（拒绝脏配置运行；业务阈值不允许为负）。
        """
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            logger.warning("strategy.%s 非法值 %r，按未配置（None=未启用）处理", name, value)
            return None
        if f < 0:
            raise ValueError(f"strategy.{name} 不能为负，实际 {f!r}")
        return f

    # ── 绩效点摄入（EVENT/回放共用入口；严格防未来）───────────────────────
    def record(self, strategy: str, timestamp: float, equity: float) -> None:
        """
        摄入一个"已发生"绩效点（timestamp, equity），按策略归组。

        防未来：timestamp 超过当前时刻 now（容差 1s）即拒绝抛 ValueError；
        等值重复点幂等去重（同策略同时间戳只保留一条）。

        Parameters
        ----------
        strategy : str
            策略名（非空）。
        timestamp : float
            绩效点时间戳（epoch 秒，必须 <= now）。
        equity : float
            该时刻策略权益/账户权益（正有限数）。

        Raises
        ------
        ValueError : strategy 为空 / timestamp 非法或为未来 / equity 非法时抛出。
        """
        if not strategy or not isinstance(strategy, str):
            raise ValueError(f"strategy 必须为非空字符串，收到 {strategy!r}")
        try:
            ts = float(timestamp)
            eq = float(equity)
        except (TypeError, ValueError) as e:
            raise ValueError(f"timestamp/equity 必须为可转 float，收到 ts={timestamp!r} eq={equity!r}") from e
        if not math.isfinite(ts) or not math.isfinite(eq):
            raise ValueError(f"timestamp/equity 必须为有限数，收到 ts={ts!r} eq={eq!r}")
        now = self._clock()
        if ts > now + _FUTURE_EPS:
            raise ValueError(
                f"拒绝未来绩效点：timestamp={ts:.3f} 晚于当前 {now:.3f}（防未来红线）"
            )

        series = self._points.setdefault(strategy, [])
        # 幂等去重（同策略同时间戳保留已存在的最新一条，避免重复入账）
        for i, (old_ts, _) in enumerate(series):
            if abs(old_ts - ts) <= 1e-9:
                series[i] = (ts, eq)
                return
        series.append((ts, eq))
        series.sort(key=lambda p: p[0])
        # 缓存上限：超出丢最旧
        if len(series) > self._max_points:
            del series[: len(series) - self._max_points]

    # ── 基于 metrics_history 回放（C-2 验收口径）────────────────────────
    def replay_from_history(
        self,
        store: Any,
        metric_name: str,
        strategy: Optional[str] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        limit: int = 10000,
    ) -> int:
        """
        从 MetricsHistoryStore 回放指标点（name/labels/时间范围）注入本监控。

        供"滚动绩效面板基于 metrics_history 回放"验收：把已落库的绩效/权益序列
        拉回并按策略归组，之后 poll() 即可基于回放序列计算滚动绩效。

        Parameters
        ----------
        store : MetricsHistoryStore
            指标时序库（须有 query(name, labels, start_ts, end_ts, limit, order)）。
        metric_name : str
            回放的指标名（如 B-2 的权益/资金指标）。
        strategy : str | None
            目标策略名；给出时按 labels={"strategy": strategy} 精确过滤，并把每行的
            strategy 标签归为该值。None 时每行按其 labels["strategy"] 归组，缺失归
            DEFAULT_STRATEGY。
        start_ts / end_ts : float | None
            时间范围（闭区间）；None=不限。
        limit : int
            查询条数上限。

        Returns
        -------
        int : 回放并成功摄入的绩效点数量。

        Raises
        ------
        ValueError : store 缺 query 或 metric_name 为空时抛出。
        """
        if store is None or not callable(getattr(store, "query", None)):
            raise ValueError("store 必须为提供 query() 的 MetricsHistoryStore")
        if not metric_name or not isinstance(metric_name, str):
            raise ValueError(f"metric_name 必须为非空字符串，收到 {metric_name!r}")

        labels: Optional[Dict[str, str]] = None
        if strategy is not None:
            if not isinstance(strategy, str) or not strategy:
                raise ValueError(f"strategy 必须为非空字符串，收到 {strategy!r}")
            labels = {"strategy": strategy}

        rows: List[Dict[str, Any]] = store.query(
            name=metric_name, labels=labels, start_ts=start_ts,
            end_ts=end_ts, limit=int(limit), order="asc",
        )
        ingested = 0
        for r in rows:
            row_strategy: str = strategy or DEFAULT_STRATEGY
            rlabels = r.get("labels")
            if strategy is None and isinstance(rlabels, dict):
                row_strategy = rlabels.get("strategy") or DEFAULT_STRATEGY
            try:
                self.record(row_strategy, float(r["timestamp"]), float(r["value"]))
                ingested += 1
            except (ValueError, TypeError, KeyError) as e:  # 非法行跳过不阻断
                logger.warning("回放跳过非法绩效行（不阻断）: %s", e)
        logger.info(
            "rolling_performance 回放完成: metric=%s strategy=%s 摄入 %d/%d 点",
            metric_name, strategy, ingested, len(rows),
        )
        return ingested

    # ── 周期巡检入口（POLL，R-02：C-2 触发列为周期）─────────────────────
    def poll(self) -> Dict[str, Any]:
        """
        周期巡检：基于已摄入绩效点计算滚动/日内绩效并复核告警。

        Returns
        -------
        dict : 滚动绩效面板快照（同 to_dict）。
        """
        now = self._clock()
        self._last_poll_time = now
        self._alert_triggered = False
        self._degraded = False

        state: Dict[str, Any] = {}
        for strategy in sorted(self._points.keys()):
            if not self._points[strategy]:
                continue
            try:
                perf = self._compute_strategy(strategy, now)
                state[strategy] = perf
            except Exception as e:  # noqa: BLE001
                self._degraded = True
                logger.warning("策略 %s 绩效计算异常（本次降级该策略）: %s", strategy, e)
                state[strategy] = {
                    "degraded": True,
                    "error": str(e),
                    "intraday_drawdown": None,
                    "intraday_return": None,
                }

        # 指标落盘（进 /api/metrics；采集器不可用仅记 warning 不阻断）
        self._publish_gauges({"strategies": state})

        self._last_state = {
            "strategies": state,
            "intraday_drawdown_limit": self._intraday_drawdown_limit,
            "alert_triggered": self._alert_triggered,
            "degraded": self._degraded,
            "last_poll_time": now,
        }
        return self._last_state

    # ── 单策略滚动绩效计算 + 告警复核 ────────────────────────────────────
    def _compute_strategy(self, strategy: str, now: float) -> Dict[str, Any]:
        """
        计算单策略滚动/日内绩效并复核告警。

        Parameters
        ----------
        strategy : str
            策略名。
        now : float
            当前时刻（用于日内窗口判定）。

        Returns
        -------
        dict : 该策略绩效面板（含 rolling_1d/7d/30d、日内回撤/收益、告警标记）。
        """
        series = self._points[strategy]

        # ── 日序列：按自然日聚合，取每日最后一个 equity ──
        daily = self._build_daily_series(series)  # [(date, equity), ...] 升序

        # ── 滚动窗口（日/周/月）──
        rolling: Dict[str, Any] = {}
        for n in WINDOW_DAYS:
            wp = self._window_points(daily, n)  # [(date, equity)]
            rolling[f"{n}d"] = self._rolling_stats(wp)

        # ── 日内（today）回撤/亏损 ──
        today = date.fromtimestamp(now)
        today_points = [p for p in series if date.fromtimestamp(p[0]) == today]
        intraday_drawdown: Optional[float] = None
        intraday_return: Optional[float] = None
        if len(today_points) >= 2:
            intraday_drawdown = self._max_drawdown_pct([p[1] for p in today_points])
            first_eq = today_points[0][1]
            if first_eq > 0:
                intraday_return = (today_points[-1][1] / first_eq - 1.0) * 100.0

        # ── 告警复核（阈值未配置=None → 不告警、不编造）──
        drawdown_alerted, loss_alerted = self._evaluate_alerts(
            strategy, intraday_drawdown, intraday_return
        )

        return {
            "point_count": len(series),
            "last_ts": series[-1][0] if series else None,
            "rolling": rolling,
            "intraday_drawdown": intraday_drawdown,
            "intraday_return": intraday_return,
            "intraday_drawdown_alerted": drawdown_alerted,
            "intraday_loss_alerted": loss_alerted,
            "degraded": False,
        }

    @staticmethod
    def _build_daily_series(series: List[Tuple[float, float]]) -> List[Tuple[date, float]]:
        """
        把绩效点聚合为日末权益序列（每自然日取最后一个权益），按日期升序。

        Parameters
        ----------
        series : list[(timestamp, equity)]
            按时间升序的绩效点。

        Returns
        -------
        list[(date, equity)] : 每自然日一条日末权益，按日期升序。
        """
        daily: Dict[date, float] = {}
        for ts, eq in series:
            daily[date.fromtimestamp(ts)] = eq  # 同日后覆盖=取最后一条（已升序）
        return sorted(daily.items(), key=lambda kv: kv[0])

    @staticmethod
    def _window_points(
        daily: List[Tuple[date, float]], n_days: int
    ) -> List[Tuple[date, float]]:
        """
        截取 trailing n_days 个自然日内的日末权益序列（截至最新日）。

        Parameters
        ----------
        daily : list[(date, equity)]
            日末权益序列（升序）。
        n_days : int
            窗口自然日数。

        Returns
        -------
        list[(date, equity)] : 窗口内日末权益，按日期升序。
        """
        if not daily:
            return []
        last_date = daily[-1][0]
        start = last_date - timedelta(days=n_days - 1)
        return [(d, e) for (d, e) in daily if d >= start]

    @classmethod
    def _rolling_stats(cls, wp: List[Tuple[date, float]]) -> Dict[str, Any]:
        """
        计算单窗口滚动绩效（收益/夏普/回撤/胜率）。

        Parameters
        ----------
        wp : list[(date, equity)]
            窗口内日末权益（升序）。

        Returns
        -------
        dict : {"return_pct", "sharpe", "drawdown_pct", "win_rate", "days"}；
            样本不足项为 None。
        """
        if len(wp) < 1:
            return {"return_pct": None, "sharpe": None, "drawdown_pct": None,
                    "win_rate": None, "days": 0}
        eqs = [e for _, e in wp]
        # 收益（首末权益，需 >=2 日）
        ret: Optional[float] = None
        if len(wp) >= 2 and eqs[0] > 0:
            ret = (eqs[-1] / eqs[0] - 1.0) * 100.0
        # 日收益序列（用于夏普/胜率）
        returns: List[float] = []
        for i in range(len(eqs) - 1):
            if eqs[i] > 0:
                returns.append((eqs[i + 1] / eqs[i] - 1.0) * 100.0)
        # 夏普（年化）
        sharpe: Optional[float] = None
        if len(returns) >= 2:
            mean = sum(returns) / len(returns)
            var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            std = math.sqrt(var)
            if std > 0:
                sharpe = (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR)
        # 回撤（窗口内权益路径最大峰谷）
        drawdown = cls._max_drawdown_pct(eqs)
        # 胜率（日收益>0 占比）
        win_rate: Optional[float] = None
        if returns:
            win_rate = (sum(1 for r in returns if r > 0) / len(returns)) * 100.0
        return {
            "return_pct": round(ret, 4) if ret is not None else None,
            "sharpe": round(sharpe, 4) if sharpe is not None else None,
            "drawdown_pct": round(drawdown, 4),
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "days": len(wp),
        }

    @staticmethod
    def _max_drawdown_pct(eqs: List[float]) -> float:
        """
        权益路径最大峰谷回撤（%）：峰值后的回落深度最大值。

        Parameters
        ----------
        eqs : list[float]
            按时间升序的权益序列。

        Returns
        -------
        float : 最大回撤百分比（>=0，无回撤为 0）。
        """
        peak = eqs[0]
        max_dd = 0.0
        for e in eqs[1:]:
            if e > peak:
                peak = e
            elif peak > 0:
                dd = (peak - e) / peak * 100.0
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    # ── 告警判定（阈值 None=PENDING_CAL → 不告警）────────────────────────
    def _evaluate_alerts(
        self,
        strategy: str,
        intraday_drawdown: Optional[float],
        intraday_return: Optional[float],
    ) -> Tuple[bool, bool]:
        """
        复核日内回撤/亏损告警（阈值未配置则跳过；按策略去重）。

        Parameters
        ----------
        strategy : str
            策略名（拼入去重键）。
        intraday_drawdown : float | None
            当日最大峰谷回撤（%）。
        intraday_return : float | None
            当日收益（%），负值=亏损。

        Returns
        -------
        (bool, bool) : (日内回撤告警是否触发, 日内亏损告警是否触发)。
        """
        drawdown_alerted = False
        loss_alerted = False
        limit = self._intraday_drawdown_limit

        # 日内回撤超阈值 → WARNING（category=strategy）
        if limit is not None and intraday_drawdown is not None and intraday_drawdown > limit:
            drawdown_alerted = self._fire_alert(
                strategy=strategy,
                kind="intraday_drawdown",
                dedup_key=f"{DEDUP_INTRADAY_DRAWDOWN_PREFIX}{strategy}",
                message=(
                    f"[strategy] 策略 {strategy} 日内回撤 {intraday_drawdown:.2f}% "
                    f"> 阈值 {limit:.2f}%"
                ),
                details={
                    "intraday_drawdown": round(intraday_drawdown, 4),
                    "intraday_drawdown_limit": limit,
                    "strategy": strategy,
                },
            )

        # 日内亏损超阈值（当日收益为负且 |收益|>阈值）→ WARNING
        if (
            limit is not None
            and intraday_return is not None
            and intraday_return < 0
            and abs(intraday_return) > limit
        ):
            loss_alerted = self._fire_alert(
                strategy=strategy,
                kind="intraday_loss",
                dedup_key=f"{DEDUP_INTRADAY_LOSS_PREFIX}{strategy}",
                message=(
                    f"[strategy] 策略 {strategy} 日内亏损 {intraday_return:.2f}% "
                    f"（|亏损|>阈值 {limit:.2f}%）"
                ),
                details={
                    "intraday_return": round(intraday_return, 4),
                    "intraday_drawdown_limit": limit,
                    "strategy": strategy,
                },
            )

        if drawdown_alerted or loss_alerted:
            self._alert_triggered = True
        return drawdown_alerted, loss_alerted

    def _fire_alert(
        self,
        strategy: str,
        kind: str,
        dedup_key: str,
        message: str,
        details: Dict[str, Any],
    ) -> bool:
        """
        发送一条 WARNING 告警（category=strategy）；失败记 warning 不阻断。

        Returns
        -------
        bool : 是否成功发出（告警管理器可用且不抛）。
        """
        try:
            self._alert_mgr().alert(
                level=SeverityLevel.WARNING,
                message=message,
                source="monitoring.rolling_performance",
                category=CATEGORY_STRATEGY,
                details=details,
                deduplication_key=dedup_key,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("策略 %s %s 告警发送失败（不阻断监控）: %s", strategy, kind, e)
            return False

    def _alert_mgr(self) -> Any:
        """延迟解析告警管理器（None 时取全局单例）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager

            mgr = get_alert_manager()
            self._alert_manager = mgr
        return mgr

    # ── 指标落盘（懒加载 collector；set_gauge 自动建指标）──────────────────
    def _publish_gauges(self, state: Dict[str, Any]) -> None:
        """
        把最新滚动/日内绩效写入 set_gauge（labels={"strategy": 策略名}）。

        采集器不可用仅记 warning，不阻断检测。
        """
        for strategy, perf in (state.get("strategies") or {}).items():
            if not isinstance(perf, dict):
                continue
            labels: Dict[str, str] = {"strategy": strategy}
            rolling = perf.get("rolling") or {}
            gauges: Dict[str, Optional[float]] = {
                METRIC_RETURN_1D: self._pick(rolling.get("1d"), "return_pct"),
                METRIC_RETURN_7D: self._pick(rolling.get("7d"), "return_pct"),
                METRIC_RETURN_30D: self._pick(rolling.get("30d"), "return_pct"),
                METRIC_SHARPE_30D: self._pick(rolling.get("30d"), "sharpe"),
                METRIC_DRAWDOWN_30D: self._pick(rolling.get("30d"), "drawdown_pct"),
                METRIC_WINRATE_30D: self._pick(rolling.get("30d"), "win_rate"),
                METRIC_INTRADAY_DRAWDOWN: perf.get("intraday_drawdown"),
                METRIC_INTRADAY_RETURN: perf.get("intraday_return"),
            }
            for name, value in gauges.items():
                self._set_gauge(name, value, labels)

    @staticmethod
    def _pick(window: Optional[Dict[str, Any]], key: str) -> Optional[float]:
        """从窗口字典安全取绩效值（缺省 None）。"""
        if not isinstance(window, dict):
            return None
        return window.get(key)

    def _set_gauge(self, name: str, value: Optional[float], labels: Dict[str, str]) -> None:
        """
        set_gauge 一个仪表盘指标；None（未启用/样本不足）落 0.0 占位。
        """
        col = self._metrics_collector
        if col is None:
            try:
                from myquant.monitoring.metrics_collector import get_metrics_collector

                col = get_metrics_collector()
                self._metrics_collector = col
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取 metrics_collector 失败（指标未写入）: {e}")
                return
        try:
            col.set_gauge(name, 0.0 if value is None else float(value), labels=labels)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"指标 {name} 写入失败（不阻断检测）: {e}")

    # ── 状态可观测（供驾驶舱/验收消费）───────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        """
        返回滚动绩效面板快照（各策略滚动窗口/日内绩效 + 阈值 + 告警标记）。

        防未来：仅截至最近一次 poll 时刻（last_poll_time）已消费的绩效点。
        """
        return dict(self._last_state)

    def summary(self, strategy: Optional[str] = None) -> Dict[str, Any]:
        """
        返回策略（或全部）的最新滚动绩效摘要（驾驶舱/面板消费）。

        Parameters
        ----------
        strategy : str | None
            目标策略；None 返回全部策略摘要。

        Returns
        -------
        dict : {策略名: 绩效摘要} 或指定策略的绩效摘要。
        """
        strategies = (self._last_state.get("strategies") or {})
        if strategy is not None:
            return strategies.get(strategy, {})
        return strategies


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：注入假告警/假指标/固定时钟，验证摄入→滚动→日内告警闭环。"""
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name} {info}")

    class _FakeAlert:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def alert(self, **kw):
            self.calls.append(kw)
            return 1

    class _FakeMetrics:
        def __init__(self) -> None:
            self.gauges: Dict[str, float] = {}

        def set_gauge(self, name, value, labels=None):
            key = f"{name}:{(labels or {}).get('strategy', '?')}"
            self.gauges[key] = float(value)

    # 固定时钟：now = 10 天内
    base = 1_700_000_000.0
    day = 86400.0
    clock_now = [base + 10 * day + 3600.0]  # 第 10 天 1 小时后

    def clock() -> float:
        return clock_now[0]

    # 造 10 天日末权益：100 → 102 → 99 → 103 → 105 → 101 → 106 → 108 → 107 → 110
    eqs = [100.0, 102.0, 99.0, 103.0, 105.0, 101.0, 106.0, 108.0, 107.0, 110.0]
    am = _FakeAlert()
    met = _FakeMetrics()
    mon = RollingPerformanceMonitor(
        intraday_drawdown_limit=10.0, alert_manager=am, metrics_collector=met, clock=clock,
    )
    for i, eq in enumerate(eqs):
        mon.record("trend_a", base + i * day, eq)

    st = mon.poll()
    ra = st["strategies"]["trend_a"]
    check("1) 日序列 10 点/30d 窗口 10 日", ra["point_count"] == 10 and ra["rolling"]["30d"]["days"] == 10, f"{ra}")
    # 30d 窗口：10 日全在窗口内；收益 = (110/100-1)*100 = 10%
    check("2) 30d 收益=10%", ra["rolling"]["30d"]["return_pct"] == 10.0, f"{ra['rolling']['30d']}")
    # 7d 窗口：最后 7 日 = eqs[3..9] = 103→110 → 收益≈6.8%
    check("3) 7d 收益>5%", ra["rolling"]["7d"]["return_pct"] is not None and ra["rolling"]["7d"]["return_pct"] > 5.0, f"{ra['rolling']['7d']}")
    # 30d 最大回撤：100→102(peak102)→99(dd=2.94)→103→105→101(peak105→dd=3.81)→106→108→107→110
    check("4) 30d 回撤>0", ra["rolling"]["30d"]["drawdown_pct"] > 0, f"{ra['rolling']['30d']['drawdown_pct']}")
    check("5) 日内（当天仅 110 一点）回撤 None/不告警", ra["intraday_drawdown"] is None and am.calls == [], f"{ra}")

    # 6) 日内回撤超阈：当天再喂一根大跌 → 峰值 110 → 88 → 回撤=20% >10% → WARNING
    am.calls.clear()
    mon.record("trend_a", clock_now[0] - 1800.0, 110.0)
    mon.record("trend_a", clock_now[0] - 900.0, 88.0)
    st2 = mon.poll()
    ra2 = st2["strategies"]["trend_a"]
    dd_alerts = [c for c in am.calls if c["deduplication_key"] == "strategy:intraday_drawdown:trend_a"]
    check("6) 日内回撤超阈触发 WARNING", len(dd_alerts) == 1 and dd_alerts[0]["level"].name == "WARNING", f"calls={am.calls}")
    check("6) 回撤附值≈20%", ra2["intraday_drawdown"] is not None and abs(ra2["intraday_drawdown"] - 20.0) < 0.01, f"{ra2['intraday_drawdown']}")
    check("6) 告警标记", st2["alert_triggered"] is True, "")

    # 7) 指标已写（labels 分策略）
    check("7) 滚动收益指标已写", f"{METRIC_RETURN_30D}:trend_a" in met.gauges, f"gauges={list(met.gauges)}")
    check("7) 日内回撤指标已写", f"{METRIC_INTRADAY_DRAWDOWN}:trend_a" in met.gauges, "")

    # 8) 阈值未配置（None，PENDING_CAL）→ 再大跌也不告警、不编造
    am2 = _FakeAlert()
    mon2 = RollingPerformanceMonitor(
        intraday_drawdown_limit=None, alert_manager=am2, metrics_collector=_FakeMetrics(), clock=clock,
    )
    mon2.record("trend_a", clock_now[0] - 1800.0, 110.0)
    mon2.record("trend_a", clock_now[0] - 900.0, 50.0)
    st3 = mon2.poll()
    check("8) 未配置阈值不告警", am2.calls == [], f"calls={am2.calls}")
    check("8) 指标照常计算输出", st3["strategies"]["trend_a"]["intraday_drawdown"] is not None, f"{st3}")

    # 9) 防未来：拒绝未来点
    mon2_clock = [base + 10 * day + 3600.0]
    def clock2() -> float:
        return mon2_clock[0]
    mon3 = RollingPerformanceMonitor(clock=clock2)
    rejected = False
    try:
        mon3.record("trend_a", mon2_clock[0] + 100.0, 120.0)
    except ValueError:
        rejected = True
    check("9) 拒绝未来绩效点", rejected, "")

    # 10) 防御：非法入参（空策略/非数权益/负阈值）
    bad = False
    try:
        mon3.record("", 1.0, 100.0)
    except ValueError:
        bad = True
    check("10) 空策略拒绝", bad, "")
    bad2 = False
    try:
        RollingPerformanceMonitor(intraday_drawdown_limit=-1.0)
    except ValueError:
        bad2 = True
    check("10) 负阈值拒绝", bad2, "")

    print("=" * 60)
    print(f"rolling_performance.py 自测: {passed} 项通过")
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
