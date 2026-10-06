# 文件名：myquant/monitoring/margin_monitor.py
"""
margin_monitor.py - 保证金率/资金费率监控（B-9，回写#229）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》B-9：
  保证金占用率、维持保证金、强平价距离、资金费率（持仓隔夜成本）监控；
  保证金占用超阈值 / 接近强平 → P0 强平预警（防止爆仓）。

触发模式（R-02）：EVENT + POLL 兜底
  - EVENT：成交（ORDER_FILLED）/ 持仓变动（POSITION_UPDATE）/ 标记价变动
    （TICK 行情事件，可开关）触发占用率重算（attach_event_bus 订阅）；
    标记价变动亦可由调用方直接调 refresh() 注入。
  - POLL：poll() 供周期巡检兜底重算（事件链路异常时仍能周期性复核）。

计算口径（全部基于"已发生"的持仓与账户/标记价快照，严格防未来）：
  - 权益（equity）         = portfolio.get_account()["total_assets"]（= 总资产口径）。
  - 保证金占用率（%）       = 占用保证金 / 权益 × 100；
                             占用保证金取 account["used_margin"]，缺省回退
                             account["frozen_cash"]（冻结/已用保证金口径）。
  - 维持保证金              = 持仓总市值 × 维持保证金率（maintenance_margin_rate，
                             配置/注入；None=未启用 → 该项不计算、距离不判定）。
  - 强平价距离百分比（%）    = (权益 - 维持保证金) / 权益 × 100；
                             含义：权益相对维持保证金的安全垫百分比，数值越小
                             越接近强平。低于配置阈值 → P0 强平预警。
  - 资金费率（持仓隔夜成本） = Σ 持仓市值 × 资金费率；费率 funding_rate 由配置/
                             注入（本监控不向行情侧索取费率真源，避免硬编码）。

阈值真源（R-04）：config/settings.yaml 的 monitoring.slo.margin 块，
  经 myquant.core.settings.get_monitoring_config() 读取：
    - strong_flat_distance_pct   ：强平价距离百分比阈值（接近强平→P0），PENDING_CAL
    - funding_rate_threshold     ：资金费率阈值（持仓隔夜成本告警），PENDING_CAL
  另：margin_usage_ratio_threshold（保证金占用率→P0 强平预警阈值）同为 PENDING_CAL，
  配置驱动；配置无该字段时回退构造参数注入，缺省 None=未启用。
  **未配置（None）→ 不触发 P0/费率告警、不编造数值**；指标照常计算输出。
  维持保证金率 maintenance_margin_rate 配置块暂无字段，作为构造参数注入，
  默认 None=未启用（docstring 注明 PENDING_CAL，待实盘校准）。

设计原则（铁律）:
  1. 阈值唯一真源 R-04：业务阈值一律经 get_monitoring_config() 读取或构造注入，
     代码内不硬编码业务阈值（PENDING_CAL 项为 None 时即"不启用告警"，绝不编造）。
  2. 严格防未来：占用率/距离只基于已发生的持仓与标记价快照（portfolio 公开 getter
     返回的当前事实），last_calc_time 仅记录计算发生时刻，不预测未来价格。
  3. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  4. LivePortfolio（myquant/live/portfolio.py）正被 B-1 并行修改——本模块**不在顶层
     import portfolio**，只经注入的鸭子类型 portfolio 调用其公开 getter；缺省实例
     在方法内惰性获取，避免顶层 import 绑定导致测试耦合失败。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.margin")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04 或构造注入）
# ─────────────────────────────────────────────
#: 告警分类（决定默认 Runbook 兜底，A-10）
CATEGORY_MARGIN: str = "margin"

#: P0 强平距离告警去重键（固定，窗口内不刷屏）
DEDUP_STRONG_FLAT: str = "margin:strong_flat"
#: 保证金占用超阈 P0 强平预警去重键
DEDUP_USAGE: str = "margin:usage"
#: 资金费率超阈告警去重键
DEDUP_FUNDING: str = "margin:funding_rate"

#: 指标名清单（set_gauge 进 collector；BuiltinMetrics 登记由主控 Wave2 统一做）
METRIC_MARGIN_USAGE_RATIO: str = "margin_usage_ratio"
METRIC_MAINTENANCE_MARGIN: str = "maintenance_margin"
METRIC_LIQUIDATION_DISTANCE_PCT: str = "liquidation_distance_pct"
METRIC_FUNDING_RATE: str = "funding_rate"
METRIC_FUNDING_COST: str = "funding_cost_estimate"

#: 本模块注册的全部指标名（供主控/驾驶舱查询）
REGISTERED_METRICS: List[str] = [
    METRIC_MARGIN_USAGE_RATIO,
    METRIC_MAINTENANCE_MARGIN,
    METRIC_LIQUIDATION_DISTANCE_PCT,
    METRIC_FUNDING_RATE,
    METRIC_FUNDING_COST,
]


class MarginMonitor:
    """
    保证金率/资金费率监控器（EVENT 实时重算 + POLL 兜底 + 接近强平 P0 预警）。

    计算口径见模块 docstring。告警分级（阈值 R-04，读 slo.margin，PENDING_CAL）:
      - liquidation_distance_pct < strong_flat_distance_pct（配置后）
          → P0/CRITICAL 强平预警（category=margin，dedup_key=margin:strong_flat）。
      - margin_usage_ratio > margin_usage_ratio_threshold（配置后）
          → P0/CRITICAL 强平预警（dedup_key=margin:usage，防止爆仓）。
      - funding_rate > funding_rate_threshold（配置后）
          → WARNING 持仓隔夜成本告警（dedup_key=margin:funding_rate）。

    防未来：只消费"已发生"的账户/持仓/标记价快照；last_calc_time 仅记录计算发生
    时刻。本行使用的数据截至计算时刻。

    使用方式:
        from myquant.monitoring.margin_monitor import MarginMonitor

        mon = MarginMonitor(portfolio=live_portfolio, maintenance_margin_rate=0.005)
        mon.attach_event_bus(bus)          # 订阅成交/持仓/标记价事件
        mon.poll()                          # 周期巡检兜底
        state = mon.to_dict()               # 供 B-8 驾驶舱 / B-10 消费
    """

    def __init__(
        self,
        portfolio: Any = None,
        strong_flat_distance_pct: Optional[float] = None,
        margin_usage_ratio_threshold: Optional[float] = None,
        funding_rate_threshold: Optional[float] = None,
        funding_rate: Optional[float] = None,
        maintenance_margin_rate: Optional[float] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        portfolio : LivePortfolio | None
            组合对象（鸭子类型：get_account()->dict / get_market_value()->float /
            get_total_assets()->float，全部只读公开 getter）。None 时在方法内惰性
            获取默认实例（避免顶层 import portfolio 与 B-1 并行修改耦合）。
        strong_flat_distance_pct : float | None
            强平价距离百分比阈值（接近强平→P0）。None 读 slo.margin.strong_flat_distance_pct；
            仍为 None（PENDING_CAL 未配置）→ 不触发强平距离告警，不编造数值。
        margin_usage_ratio_threshold : float | None
            保证金占用率阈值（占用超阈→P0 强平预警）。None 读 slo.margin.
            margin_usage_ratio_threshold；仍为 None（PENDING_CAL）→ 不触发该告警。
        funding_rate_threshold : float | None
            资金费率阈值。None 读 slo.margin.funding_rate_threshold；仍为 None
            （PENDING_CAL）→ 不触发费率告警。
        funding_rate : float | None
            当前资金费率（持仓隔夜成本=Σ市值×费率）。费率真源由配置/调用方注入；
            None=未启用（不计算隔夜成本、不做费率告警）。
        maintenance_margin_rate : float | None
            维持保证金率（维持保证金=市值×此率）。配置块暂无该字段，作为构造参数
            注入；默认 None=未启用（不计算维持保证金、不判定强平距离），PENDING_CAL
            待实盘校准。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（进 /api/metrics）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        """
        slo = get_monitoring_config().get("slo", {}).get("margin", {})

        # ── 阈值（R-04；构造注入优先，回退配置；None=PENDING_CAL 未配置=不启用告警）──
        self._strong_flat_distance_pct: Optional[float] = (
            self._as_optional_float(strong_flat_distance_pct, "strong_flat_distance_pct")
            if strong_flat_distance_pct is not None
            else self._as_optional_float(slo.get("strong_flat_distance_pct"), "strong_flat_distance_pct")
        )
        self._margin_usage_threshold: Optional[float] = (
            self._as_optional_float(
                margin_usage_ratio_threshold, "margin_usage_ratio_threshold"
            )
            if margin_usage_ratio_threshold is not None
            else self._as_optional_float(
                slo.get("margin_usage_ratio_threshold"), "margin_usage_ratio_threshold"
            )
        )
        self._funding_rate_threshold: Optional[float] = (
            self._as_optional_float(funding_rate_threshold, "funding_rate_threshold")
            if funding_rate_threshold is not None
            else self._as_optional_float(slo.get("funding_rate_threshold"), "funding_rate_threshold")
        )

        # ── 计算参数（费率/维持保证金率；配置块暂无字段 → 构造注入，None=未启用）──
        self._funding_rate: Optional[float] = self._as_optional_float(
            funding_rate, "funding_rate"
        )
        self._maintenance_margin_rate: Optional[float] = self._as_optional_float(
            maintenance_margin_rate, "maintenance_margin_rate"
        )

        self._portfolio = portfolio
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector

        # 可观测状态（refresh()/poll() 写入；初始未计算）
        self._equity: Optional[float] = None
        self._occupied_margin: Optional[float] = None
        self._market_value: Optional[float] = None
        self._margin_usage_ratio: Optional[float] = None
        self._maintenance_margin: Optional[float] = None
        self._liquidation_distance_pct: Optional[float] = None
        self._funding_cost: Optional[float] = None
        self._last_calc_time: Optional[float] = None
        self._alert_triggered: bool = False
        self._degraded: bool = False

        self._handler_ids: List[str] = []
        logger.debug(
            "MarginMonitor 初始化: strong_flat_dist=%s usage_thr=%s funding_thr=%s "
            "funding_rate=%s mm_rate=%s",
            self._strong_flat_distance_pct, self._margin_usage_threshold,
            self._funding_rate_threshold, self._funding_rate, self._maintenance_margin_rate,
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
            logger.warning("margin.%s 非法值 %r，按未配置（None=未启用）处理", name, value)
            return None
        if f < 0:
            raise ValueError(f"margin.{name} 不能为负，实际 {f!r}")
        return f

    # ── 组合实例解析（方法内惰性 import，避免顶层绑定 B-1 并行修改的 portfolio）──
    def _resolve_portfolio(self) -> Any:
        """
        解析组合数据源：优先注入实例；否则惰性 import 默认 LivePortfolio 单例。

        惰性 import 位于方法体内（不在模块顶层），与正被 B-1 修改的 portfolio.py
        解耦；获取失败返回 None（refresh 走降级路径，不抛）。

        Returns
        -------
        LivePortfolio | None : 组合对象；不可用时 None。
        """
        if self._portfolio is not None:
            return self._portfolio
        try:  # 方法内惰性 import（禁止顶层绑定）
            from myquant.live.portfolio import LivePortfolio  # noqa: WPS433

            self._portfolio = LivePortfolio()
        except Exception as e:  # 组合不可用不阻断监控主流程（降级）
            logger.warning("惰性获取 LivePortfolio 失败（本次按降级处理）: %s", e)
            self._portfolio = None
        return self._portfolio

    # ── 事件链路：订阅成交/持仓/标记价事件 → 触发重算 ─────────────────────
    def attach_event_bus(self, bus: Any, subscribe_market_events: bool = True) -> None:
        """
        订阅 EventBus 的成交/持仓/标记价事件，到达即触发占用率重算（EVENT 路径）。

        Parameters
        ----------
        bus : EventBus
            事件总线。订阅失败记 warning，不阻断（poll() 兜底仍可用）。
        subscribe_market_events : bool
            是否订阅 TICK 行情事件作为"标记价变动"来源（默认 True）；标记价变动
            亦可由调用方直接调 refresh() 注入。
        """
        if bus is None:
            return
        try:
            from myquant.core.event_bus import EventType  # noqa: WPS433

            self._handler_ids.append(
                bus.subscribe(EventType.ORDER_FILLED, self._on_trade_event)
            )
            self._handler_ids.append(
                bus.subscribe(EventType.POSITION_UPDATE, self._on_trade_event)
            )
            if subscribe_market_events:
                self._handler_ids.append(
                    bus.subscribe(EventType.TICK, self._on_trade_event)
                )
            logger.info(
                "MarginMonitor 已订阅 EventBus ORDER_FILLED/POSITION_UPDATE"
                "%s", (" / TICK" if subscribe_market_events else "")
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MarginMonitor 订阅 EventBus 失败（poll 兜底仍可用）: {e}")

    def _on_trade_event(self, event: Any) -> None:
        """事件总线成交/持仓/标记价事件：触发一次占用率重算（防御，不抛出）。"""
        try:
            self.refresh()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"处理交易/行情事件触发重算异常: {e}")

    # ── POLL 兜底入口 ────────────────────────────────────────────────────
    def poll(self) -> Dict[str, Any]:
        """
        周期巡检兜底：重算全部监控项并复核告警（事件链路异常时仍能周期复核）。

        Returns
        -------
        dict : 当前监控状态快照（同 to_dict）。
        """
        return self.refresh()

    # ── 核心重算（EVENT 与 POLL 共用）────────────────────────────────────
    def refresh(self) -> Dict[str, Any]:
        """
        基于"已发生"的账户/持仓/标记价快照重算保证金/强平距离/资金费率并复核告警。

        防未来：只消费 portfolio 公开 getter 返回的当前事实（截至 now 时刻），
        不预测未来价格；last_calc_time 记录本次计算发生时刻。

        Returns
        -------
        dict : 当前监控状态快照（同 to_dict）。数据缺失时降级（置 None/0、不抛）。
        """
        now = self._clock()
        portfolio = self._resolve_portfolio()

        # 降级：组合不可用 → 指标归零/置 None，不抛异常
        if portfolio is None:
            self._degraded = True
            self._equity = None
            self._occupied_margin = None
            self._market_value = None
            self._margin_usage_ratio = None
            self._maintenance_margin = None
            self._liquidation_distance_pct = None
            self._funding_cost = None
            self._last_calc_time = now
            logger.warning("MarginMonitor 无可用组合数据源，本次降级输出（不告警）")
            return self.to_dict()

        account: Dict[str, Any] = {}
        market_value: float = 0.0
        try:
            account = dict(portfolio.get_account() or {})
            market_value = float(portfolio.get_market_value() or 0.0)
        except Exception as e:  # 读快照失败 → 降级，不阻断
            self._degraded = True
            logger.warning("读取组合快照失败，本次降级（不告警）: %s", e)
            self._last_calc_time = now
            return self.to_dict()

        # ── 权益与占用保证金（截至 now 的已发生快照）──
        equity = self._pick_float(account, "total_assets", None)
        if equity is None:
            try:
                equity = float(portfolio.get_total_assets() or 0.0)
            except Exception:  # noqa: BLE001
                equity = 0.0
        # 占用保证金：优先 used_margin，缺省回退 frozen_cash（冻结/已用保证金口径）
        occupied = self._pick_float(account, "used_margin", None)
        if occupied is None:
            occupied = self._pick_float(account, "frozen_cash", 0.0)

        # ── 保证金占用率 = 占用保证金 / 权益 × 100（权益为 0 时不计算）──
        if equity is not None and equity > 0:
            margin_usage_ratio = (occupied / equity) * 100.0
        else:
            margin_usage_ratio = None

        # ── 维持保证金 = 市值 × 维持保证金率（率 None=未启用）──
        if self._maintenance_margin_rate is not None:
            maintenance_margin = market_value * self._maintenance_margin_rate
        else:
            maintenance_margin = None

        # ── 强平价距离百分比 = (权益 - 维持保证金) / 权益 × 100（安全垫）──
        if (
            maintenance_margin is not None
            and equity is not None
            and equity > 0
        ):
            liquidation_distance_pct = (
                (equity - maintenance_margin) / equity
            ) * 100.0
        else:
            liquidation_distance_pct = None

        # ── 资金费率持仓隔夜成本 = Σ市值 × 费率（费率 None=未启用）──
        if self._funding_rate is not None:
            funding_cost = market_value * self._funding_rate
        else:
            funding_cost = None

        # 写入状态
        self._degraded = False
        self._equity = equity
        self._occupied_margin = occupied
        self._market_value = market_value
        self._margin_usage_ratio = margin_usage_ratio
        self._maintenance_margin = maintenance_margin
        self._liquidation_distance_pct = liquidation_distance_pct
        self._funding_cost = funding_cost
        self._last_calc_time = now
        self._alert_triggered = False

        # 指标落盘（set_gauge；采集器不可用仅记 warning 不阻断）
        self._set_gauge(METRIC_MARGIN_USAGE_RATIO, margin_usage_ratio)
        self._set_gauge(METRIC_MAINTENANCE_MARGIN, maintenance_margin)
        self._set_gauge(METRIC_LIQUIDATION_DISTANCE_PCT, liquidation_distance_pct)
        self._set_gauge(METRIC_FUNDING_RATE, self._funding_rate)
        self._set_gauge(METRIC_FUNDING_COST, funding_cost)

        # 告警复核（阈值未配置=None → 不告警、不编造）
        self._evaluate_alerts()
        return self.to_dict()

    @staticmethod
    def _pick_float(account: Dict[str, Any], key: str, default: Optional[float]) -> Optional[float]:
        """
        从账户字典安全取 float 字段；缺失/非法返回 default（防御，不抛）。

        Parameters
        ----------
        account : dict
            get_account() 返回的账户字典。
        key : str
            字段名。
        default : float | None
            字段缺失时的回退值。

        Returns
        -------
        float | None : 解析后的浮点值或 default。
        """
        val = account.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    # ── 告警判定（阈值 None=PENDING_CAL → 不告警）────────────────────────
    def _evaluate_alerts(self) -> None:
        """基于最新快照复核强平距离/占用率/资金费率告警（阈值未配置则跳过）。"""
        # 1) 接近强平：强平距离 < 阈值 → P0/CRITICAL
        if (
            self._strong_flat_distance_pct is not None
            and self._liquidation_distance_pct is not None
            and self._liquidation_distance_pct < self._strong_flat_distance_pct
        ):
            self._alert_triggered = True
            self._fire_strong_flat_alert()

        # 2) 保证金占用超阈 → P0/CRITICAL 强平预警（防爆仓）
        if (
            self._margin_usage_threshold is not None
            and self._margin_usage_ratio is not None
            and self._margin_usage_ratio > self._margin_usage_threshold
        ):
            self._alert_triggered = True
            self._fire_usage_alert()

        # 3) 资金费率超阈 → WARNING 持仓隔夜成本告警（级别按严重度）
        if (
            self._funding_rate_threshold is not None
            and self._funding_rate is not None
            and self._funding_rate > self._funding_rate_threshold
        ):
            self._alert_triggered = True
            self._fire_funding_alert()

    def _alert_mgr(self) -> Any:
        """延迟解析告警管理器（None 时取全局单例）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager

            mgr = get_alert_manager()
            self._alert_manager = mgr
        return mgr

    def _fire_strong_flat_alert(self) -> None:
        """接近强平 P0/CRITICAL 预警（message 含距离与阈值；固定去重键不刷屏）。"""
        try:
            self._alert_mgr().alert(
                level=SeverityLevel.CRITICAL,
                message=(
                    f"[margin] 接近强平：强平价距离 {self._liquidation_distance_pct:.2f}% "
                    f"< 阈值 {self._strong_flat_distance_pct:.2f}%，存在爆仓风险"
                ),
                source="monitoring.margin",
                category=CATEGORY_MARGIN,
                details={
                    "liquidation_distance_pct": round(self._liquidation_distance_pct, 3),
                    "strong_flat_distance_pct": self._strong_flat_distance_pct,
                    "equity": self._equity,
                    "maintenance_margin": self._maintenance_margin,
                },
                deduplication_key=DEDUP_STRONG_FLAT,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"强平距离 P0 告警发送失败（不阻断监控）: {e}")

    def _fire_usage_alert(self) -> None:
        """保证金占用超阈 P0/CRITICAL 强平预警（防爆仓）。"""
        try:
            self._alert_mgr().alert(
                level=SeverityLevel.CRITICAL,
                message=(
                    f"[margin] 保证金占用率 {self._margin_usage_ratio:.2f}% "
                    f"> 阈值 {self._margin_usage_threshold:.2f}%，存在爆仓风险"
                ),
                source="monitoring.margin",
                category=CATEGORY_MARGIN,
                details={
                    "margin_usage_ratio": round(self._margin_usage_ratio, 3),
                    "margin_usage_ratio_threshold": self._margin_usage_threshold,
                    "occupied_margin": self._occupied_margin,
                    "equity": self._equity,
                },
                deduplication_key=DEDUP_USAGE,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"保证金占用 P0 告警发送失败（不阻断监控）: {e}")

    def _fire_funding_alert(self) -> None:
        """资金费率超阈 WARNING 持仓隔夜成本告警。"""
        try:
            self._alert_mgr().alert(
                level=SeverityLevel.WARNING,
                message=(
                    f"[margin] 资金费率 {self._funding_rate:.5f} > 阈值 "
                    f"{self._funding_rate_threshold:.5f}，持仓隔夜成本偏高"
                ),
                source="monitoring.margin",
                category=CATEGORY_MARGIN,
                details={
                    "funding_rate": self._funding_rate,
                    "funding_rate_threshold": self._funding_rate_threshold,
                    "funding_cost_estimate": self._funding_cost,
                    "market_value": self._market_value,
                },
                deduplication_key=DEDUP_FUNDING,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"资金费率告警发送失败（不阻断监控）: {e}")

    # ── 指标落盘（懒加载 collector；set_gauge 自动建指标）──────────────────
    def _set_gauge(self, name: str, value: Optional[float]) -> None:
        """
        set_gauge 一个仪表盘指标（进 /api/metrics）；None（未启用）落 0.0 占位。

        采集器不可用仅记 warning，不阻断检测。
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
            col.set_gauge(name, 0.0 if value is None else float(value))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"指标 {name} 写入失败（不阻断检测）: {e}")

    # ── 状态可观测（供 B-8 驾驶舱 / B-10 消费）───────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        """
        返回监控状态快照（占用率/维持保证金/强平距离/资金费率/最近计算时间/是否触发告警）。

        防未来：仅截至最近一次计算时刻（last_calc_time）的已发生快照。
        """
        return {
            "margin_usage_ratio": self._margin_usage_ratio,
            "maintenance_margin": self._maintenance_margin,
            "liquidation_distance_pct": self._liquidation_distance_pct,
            "funding_rate": self._funding_rate,
            "funding_cost_estimate": self._funding_cost,
            "occupied_margin": self._occupied_margin,
            "equity": self._equity,
            "market_value": self._market_value,
            "maintenance_margin_rate": self._maintenance_margin_rate,
            "strong_flat_distance_pct": self._strong_flat_distance_pct,
            "margin_usage_ratio_threshold": self._margin_usage_threshold,
            "funding_rate_threshold": self._funding_rate_threshold,
            "last_calc_time": self._last_calc_time,
            "alert_triggered": self._alert_triggered,
            "degraded": self._degraded,
        }


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：注入桩组合/假告警/假指标/固定时钟，验证 EVENT→重算→P0 闭环。"""
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
            self.calls: list = []

        def alert(self, **kw):
            self.calls.append(kw)
            return 1

    class _FakeMetrics:
        def __init__(self) -> None:
            self.gauges: dict = {}

        def set_gauge(self, name, value, labels=None):
            self.gauges[name] = float(value)

    class _StubPortfolio:
        """鸭子类型组合：get_account/get_market_value/get_total_assets。"""

        def __init__(self, equity: float, frozen: float, mv: float) -> None:
            self._equity = equity
            self._frozen = frozen
            self._mv = mv

        def get_account(self):
            return {
                "total_assets": self._equity,
                "frozen_cash": self._frozen,
                "market_value": self._mv,
            }

        def get_market_value(self):
            return self._mv

        def get_total_assets(self):
            return self._equity

    clock_now = [1000.0]

    def clock() -> float:
        return clock_now[0]

    # 1) 强平距离阈值 5%，维持保证金率 0.5%
    #    权益=100000，市值=100000 → 维持保证金=500 → 距离=(100000-500)/100000=99.5% >5% → 不告警
    am = _FakeAlert()
    met = _FakeMetrics()
    pf = _StubPortfolio(equity=100000.0, frozen=10000.0, mv=100000.0)
    mon = MarginMonitor(
        portfolio=pf, strong_flat_distance_pct=5.0, maintenance_margin_rate=0.005,
        alert_manager=am, metrics_collector=met, clock=clock,
    )
    st = mon.refresh()
    check("1) 距离=99.5%>5% 不告警", am.calls == [] and st["liquidation_distance_pct"] == 99.5, f"st={st}")
    check("1) 占用率=10%", st["margin_usage_ratio"] == 10.0, f"st={st['margin_usage_ratio']}")

    # 2) 权益恶化到 510（维持保证金 500 → 距离=(510-500)/510≈1.96% <5% → P0）
    pf._equity = 510.0
    st = mon.refresh()
    p0 = [c for c in am.calls if c["deduplication_key"] == "margin:strong_flat"]
    check("2) 距离<5% 触发 P0/CRITICAL", len(p0) == 1 and p0[0]["level"].name == "CRITICAL", f"calls={am.calls}")
    check("2) to_dict.alert_triggered=True", mon.to_dict()["alert_triggered"] is True, "")

    # 3) 指标可见
    check("3) margin_usage_ratio 指标已写", "margin_usage_ratio" in met.gauges, f"gauges={met.gauges}")
    check("3) liquidation_distance_pct 指标已写", "liquidation_distance_pct" in met.gauges, "")

    # 4) 未配置阈值（None）→ 距离再小也不告警、不编造
    am2 = _FakeAlert()
    pf2 = _StubPortfolio(equity=510.0, frozen=10.0, mv=100000.0)
    mon2 = MarginMonitor(
        portfolio=pf2, strong_flat_distance_pct=None, maintenance_margin_rate=0.005,
        alert_manager=am2, metrics_collector=_FakeMetrics(), clock=clock,
    )
    st2 = mon2.refresh()
    check("4) 未配置阈值不告警", am2.calls == [], f"calls={am2.calls}")
    check("4) 指标照常计算输出", st2["liquidation_distance_pct"] is not None, f"st={st2}")

    # 5) 保证金占用超阈 P0（占用率阈值 15%，当前 frozen=50000/equity=100000=50%）
    am3 = _FakeAlert()
    pf3 = _StubPortfolio(equity=100000.0, frozen=50000.0, mv=100000.0)
    mon3 = MarginMonitor(
        portfolio=pf3, margin_usage_ratio_threshold=15.0, maintenance_margin_rate=None,
        alert_manager=am3, metrics_collector=_FakeMetrics(), clock=clock,
    )
    mon3.refresh()
    usage = [c for c in am3.calls if c["deduplication_key"] == "margin:usage"]
    check("5) 占用率超阈触发 P0", len(usage) == 1 and usage[0]["level"].name == "CRITICAL", f"calls={am3.calls}")

    # 6) 资金费率超阈 WARNING（费率 0.001，阈值 0.0005；市值 100000 → 隔夜成本=100）
    am4 = _FakeAlert()
    mon4 = MarginMonitor(
        portfolio=pf3, funding_rate=0.001, funding_rate_threshold=0.0005,
        alert_manager=am4, metrics_collector=_FakeMetrics(), clock=clock,
    )
    st4 = mon4.refresh()
    fr = [c for c in am4.calls if c["deduplication_key"] == "margin:funding_rate"]
    check("6) 资金费率超阈触发 WARNING", len(fr) == 1 and fr[0]["level"].name == "WARNING", f"calls={am4.calls}")
    check("6) 隔夜成本=100", st4["funding_cost_estimate"] == 100.0, f"st={st4['funding_cost_estimate']}")

    # 7) 防御：组合 get_account 抛错 → 降级不抛
    class _BadPortfolio:
        def get_account(self):
            raise RuntimeError("boom")

    mon5 = MarginMonitor(portfolio=_BadPortfolio(), alert_manager=_FakeAlert(), clock=clock)
    raised = False
    try:
        mon5.refresh()
    except Exception:
        raised = True
    check("7) 快照失败降级不抛", not raised and mon5.to_dict()["degraded"] is True, f"raised={raised}")

    print("=" * 60)
    print(f"margin_monitor.py 自测: {passed} 项通过")
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
