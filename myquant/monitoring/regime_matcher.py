# 文件名：myquant/monitoring/regime_matcher.py
"""
regime_matcher.py - 环境匹配度评分（C-3，回写#242）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》C-3 环境匹配度评分项：
  基于 RSRS 等 regime 指标识别市场环境；策略-环境匹配评分；
  低匹配 → 降频/降仓建议告警（P2）。

数据模型（严格防未来）：
  - 本模块不自行计算 RSRS（复用 myquant/indicators/rsrs.py 的输出），
    由调用方把"已发生"的 RSRS 值喂入：observe_rsrs(ts, rsrs_value, r2_value)。
  - ts 为 RSRS 值对应的时间戳（该值在此刻已可得）；observe 拒绝 ts>now 的未来点。
  - 当前环境档位 = 最近一个已发生 RSRS 值经 classify_regime 判定的档位
    （最新已发生事实 = 当前可得的最新环境，无任何未来引用）。

环境档位（来自 indicators.rsrs.classify_regime，5 档）:
  - trend_up / trend_down ：RSRS 超买卖阈值 且 R²>=趋势置信下限（强势趋势）
  - mild_up / mild_down   ：RSRS 同向但未超阈（温和方向）
  - range                 ：|RSRS|<=0、信号强但结构弱（R² 不足）、或数据不足

策略-环境匹配评分（0~100，算法矩阵，模块常量定义）：
  - trend 风格策略：trend_up=100 / trend_down=90 / mild_up=80 / mild_down=70 / range=40
  - range 风格策略：range=100 / mild_up=70 / mild_down=70 / trend_up=40 / trend_down=40

低匹配告警（R-04 阈值真源 slo.regime_matcher.match_score_min）：
  - 策略匹配分 < match_score_min → P2/WARNING（category=regime，
    dedup=regime:match:<strategy>，details 附匹配分/环境/降频降仓建议）。
  - 阈值未配置（None=PENDING_CAL）→ 只计算评分输出，不触发告警、不编造。

阈值真源（R-04）：config/settings.yaml 的 monitoring.slo.regime_matcher，
  经 myquant.core.settings.get_monitoring_config() 读取：
    - match_score_min ：策略-环境匹配评分下限（低于即 P2 预警），PENDING_CAL。
    - trend_r2_min    ：趋势置信 R² 下限（覆盖算法默认 0.30；None 用算法默认，
                        属算法参数回退，非业务阈值硬编码）。
  构造注入优先级高于配置。

设计原则（铁律）:
  1. 阈值唯一真源 R-04：业务阈值（match_score_min）一律经 get_monitoring_config()
     读取或构造注入，代码内不硬编码业务阈值；PENDING_CAL（None）即"不启用告警"，
     绝不编造。
  2. 严格防未来：只消费已发生 RSRS 值；observe_rsrs 拒绝未来时间戳点；
     当前环境只由最新已发生值判定。
  3. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  4. 指标经 get_metrics_collector().set_gauge 动态注册（不改 metrics_collector.py 本体）。
  5. 匹配评分矩阵为算法定义（模块常量，非业务阈值）；RSRS buy/sell 阈值沿用
     指标标准参数 0.7/-0.7（可注入覆盖），不做监控业务阈值。
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config
from myquant.indicators.rsrs import classify_regime

logger = get_logger("monitoring.regime_matcher")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04 或构造注入）
# ─────────────────────────────────────────────

#: 告警分类（category 固定值，契约要求 category=regime）
CATEGORY_REGIME: str = "regime"

#: 告警来源（契约要求 source=monitoring.regime_matcher）
ALERT_SOURCE: str = "monitoring.regime_matcher"

#: 未来点判定容差（秒）：ts 超过 now 的绝对上限
_FUTURE_EPS: float = 1.0

#: RSRS 买入/卖出阈值默认值（沿用指标标准参数 0.7/-0.7，可注入覆盖）
DEFAULT_BUY_THRESHOLD: float = 0.7
DEFAULT_SELL_THRESHOLD: float = -0.7

#: 趋势置信 R² 算法默认下限（与 indicators/rsrs.REGIME_TREND_R2_MIN 一致；
#: 非业务告警阈值——业务判定以 match_score_min 为准）
DEFAULT_TREND_R2_MIN: float = 0.30

#: 支持的策略风格档位
VALID_STRATEGY_STYLES: Tuple[str, ...] = ("trend", "range")

#: 环境档位数值编码（set_gauge 用；to_dict 同时给出字符串档位）
REGIME_STATE_CODE: Dict[str, float] = {
    "trend_up": 3.0,
    "mild_up": 2.0,
    "range": 0.0,
    "mild_down": -2.0,
    "trend_down": -3.0,
}

#: 策略风格 × 环境档位 → 匹配评分（0~100）算法矩阵（模块常量，非业务阈值）
MATCH_SCORE_BY_STYLE: Dict[str, Dict[str, int]] = {
    "trend": {
        "trend_up": 100,
        "trend_down": 90,
        "mild_up": 80,
        "mild_down": 70,
        "range": 40,
    },
    "range": {
        "range": 100,
        "mild_up": 70,
        "mild_down": 70,
        "trend_up": 40,
        "trend_down": 40,
    },
}

#: 低匹配告警去重键前缀（后缀拼策略名，固定窗口内不刷屏）
DEDUP_MATCH_PREFIX: str = "regime:match:"

#: 未注册风格的策略的兜底风格（防御：未知风格保守视为 trend）
FALLBACK_STYLE: str = "trend"

#: RSRS 观测缓存上限（超出丢最旧，有界内存）
MAX_POINTS: int = 500

#: 指标名（set_gauge 进 collector，labels={"strategy": 策略名} 或全局）
METRIC_REGIME_STATE: str = "regime_state"
METRIC_REGIME_MATCH_SCORE: str = "regime_match_score"

#: 本模块注册的全部指标名（供主控/驾驶舱查询）
REGISTERED_METRICS: List[str] = [
    METRIC_REGIME_STATE,
    METRIC_REGIME_MATCH_SCORE,
]


class RegimeMatcher:
    """
    环境匹配度评分器（周期巡检，非 MTTD 类）。

    生命周期：
      - register_strategy_style() 注册策略风格画像（trend/range）；
      - observe_rsrs() 逐点喂"已发生"的 RSRS 值（防未来：拒绝未来时间戳）；
      - poll()/evaluate() 判定当前环境 → 逐策略算匹配分 → 低匹配复核告警 → 写 gauge；
      - summary()/to_dict() 返回最新状态（可 JSON 序列化，供看板/验收消费）。

    告警（阈值 R-04，读 slo.regime_matcher，PENDING_CAL=None 不告警）:
      - 策略匹配分 < match_score_min → P2/WARNING（category=regime，
        dedup=regime:match:<strategy>，details 附匹配分/当前环境/降频降仓建议）。
    """

    def __init__(
        self,
        strategy_styles: Optional[Dict[str, str]] = None,
        match_score_min: Optional[float] = None,
        trend_r2_min: Optional[float] = None,
        buy_threshold: float = DEFAULT_BUY_THRESHOLD,
        sell_threshold: float = DEFAULT_SELL_THRESHOLD,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
        max_points: int = MAX_POINTS,
    ) -> None:
        """
        Parameters
        ----------
        strategy_styles : dict[str, str] | None
            策略风格画像 {策略名: style}，style ∈ {"trend","range"}。
            None 初始为空，随后用 register_strategy_style() 逐个注册。
        match_score_min : float | None
            低匹配判定下限。None 读 slo.regime_matcher.match_score_min；
            仍为 None（PENDING_CAL 未配置）→ 只计算评分不触发告警，不编造。
        trend_r2_min : float | None
            趋势置信 R² 下限。None 读 slo.regime_matcher.trend_r2_min；
            仍为 None → 用算法默认 DEFAULT_TREND_R2_MIN（算法参数，非业务阈值）。
        buy_threshold / sell_threshold : float
            RSRS 买卖阈值（沿用指标标准参数，可注入覆盖；非法组合显式报错）。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（进 /api/metrics）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        max_points : int
            RSRS 观测缓存上限（超出丢最旧），必须为正整数。
        """
        if max_points <= 0:
            raise ValueError(f"max_points 必须为正整数，收到 {max_points}")
        if buy_threshold <= sell_threshold:
            raise ValueError(
                f"buy_threshold 必须大于 sell_threshold，收到 buy={buy_threshold} sell={sell_threshold}"
            )

        slo = get_monitoring_config().get("slo", {}).get("regime_matcher", {})

        # ── 阈值（R-04；构造注入优先，回退配置；None=PENDING_CAL 未配置=不启用告警）──
        self._match_score_min: Optional[float] = self._as_optional_float(
            match_score_min if match_score_min is not None else slo.get("match_score_min"),
            "match_score_min",
        )
        if self._match_score_min is not None and self._match_score_min < 0.0:
            raise ValueError(
                f"match_score_min 必须 >= 0（评分体系 0~100），收到 {self._match_score_min}"
            )
        trend_r2_cfg = trend_r2_min if trend_r2_min is not None else slo.get("trend_r2_min")
        self._trend_r2_min: float = self._as_optional_float(
            trend_r2_cfg, "trend_r2_min"
        ) if trend_r2_cfg is not None else DEFAULT_TREND_R2_MIN
        if not 0.0 <= self._trend_r2_min <= 1.0:
            raise ValueError(f"trend_r2_min 必须在 [0,1]，收到 {self._trend_r2_min}")

        self._buy_threshold: float = float(buy_threshold)
        self._sell_threshold: float = float(sell_threshold)
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector
        self._max_points: int = int(max_points)

        # 策略风格画像：{strategy: style}
        self._strategy_styles: Dict[str, str] = {}
        if strategy_styles:
            for name, style in strategy_styles.items():
                self.register_strategy_style(name, style)

        # RSRS 观测缓存：[(ts, rsrs_value, r2_value), ...]（按时间升序，仅已发生）
        self._points: Deque[Tuple[float, float, float]] = deque(maxlen=max_points)

        # 可观测状态（poll()/evaluate() 写入）
        self._last_poll_time: Optional[float] = None
        self._last_regime: str = "range"
        self._last_scores: Dict[str, int] = {}
        self._alert_triggered: bool = False
        self._degraded: bool = False
        self._last_state: Dict[str, Any] = {}

        logger.debug(
            "RegimeMatcher 初始化: match_score_min=%s trend_r2_min=%s buy/sell=%s/%s",
            self._match_score_min, self._trend_r2_min,
            self._buy_threshold, self._sell_threshold,
        )

    # ── 防御式工具 ─────────────────────────────────────────────

    @staticmethod
    def _as_optional_float(value: Any, name: str) -> Optional[float]:
        """把配置值转 float；None 保持；非数值/负阈值显式报错（防御式）。"""
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{name} 必须为数值或 None，收到 {value!r}") from e
        if not math.isfinite(f):
            raise ValueError(f"{name} 必须为有限数值，收到 {value!r}")
        return f

    # ── 策略风格画像 ───────────────────────────────────────────

    def register_strategy_style(self, name: str, style: str) -> None:
        """
        注册/更新一个策略的风格画像。

        Args:
            name: 策略名（非空字符串）。
            style: 风格档位，∈ {"trend","range"}。

        Raises:
            ValueError: 策略名为空或风格不在合法档位时抛出。
        """
        if not name or not isinstance(name, str):
            raise ValueError(f"策略名必须为非空字符串，收到 {name!r}")
        if style not in VALID_STRATEGY_STYLES:
            raise ValueError(
                f"策略 {name!r} 的风格 {style!r} 非法，仅支持 {VALID_STRATEGY_STYLES}"
            )
        self._strategy_styles[name] = style

    def get_strategy_styles(self) -> Dict[str, str]:
        """返回策略风格画像副本（只读快照）。"""
        return dict(self._strategy_styles)

    # ── 数据摄入（严格防未来）──────────────────────────────────

    def observe_rsrs(self, ts: float, rsrs_value: float, r2_value: float) -> None:
        """
        摄入一条"已发生"的 RSRS 观测（RSRS 值在该 ts 时刻已可得）。

        防未来红线：ts 超过当前时刻（容差 1s）→ 拒绝并抛 ValueError；
        本行只消费 ts<=now 的已发生 RSRS 值。

        Args:
            ts: RSRS 值对应时间戳（epoch 秒，已发生）。
            rsrs_value: RSRS z-score（NaN=数据不足，按 range 处理）。
            r2_value: 对应回归 R²（NaN=数据不足，按 range 处理）。

        Raises:
            ValueError: ts 为未来时间戳，或 rsrs/r2 为 inf 非有限值时抛出。
        """
        now = self._clock()
        if not math.isfinite(ts):
            raise ValueError(f"ts 必须为有限数值，收到 {ts!r}")
        if ts > now + _FUTURE_EPS:
            raise ValueError(
                f"拒绝未来 RSRS 观测：ts={ts:.3f} > now={now:.3f}（防未来铁律）"
            )
        if not math.isfinite(rsrs_value) or not math.isfinite(r2_value):
            raise ValueError(
                f"rsrs_value/r2_value 必须为有限数值（NaN 属数据不足请用 None 语义），"
                f"收到 rsrs={rsrs_value!r} r2={r2_value!r}"
            )
        self._points.append((float(ts), float(rsrs_value), float(r2_value)))

    # ── 核心计算与巡检 ─────────────────────────────────────────

    def _current_regime(self) -> str:
        """
        当前市场环境档位 = 最近一个已发生 RSRS 值判定的档位。
        无观测（冷启动）→ "range"（未知环境保守不误判方向）。
        """
        if not self._points:
            return "range"
        _, rsrs_v, r2_v = self._points[-1]
        return classify_regime(
            rsrs_v,
            r2_v,
            buy_threshold=self._buy_threshold,
            sell_threshold=self._sell_threshold,
            trend_r2_min=self._trend_r2_min,
        )

    def _match_score(self, style: str, regime: str) -> int:
        """按风格矩阵计算匹配分（0~100）；矩阵缺档保守取 range 分。"""
        matrix = MATCH_SCORE_BY_STYLE.get(style, MATCH_SCORE_BY_STYLE[FALLBACK_STYLE])
        return matrix.get(regime, matrix["range"])

    def evaluate(self) -> Dict[str, Any]:
        """
        计算并复核：判定当前环境 → 逐策略算匹配分 → 低匹配告警 → 写 gauge。
        返回最新状态快照（同 to_dict()）。
        """
        now = self._clock()
        self._last_poll_time = now

        regime = self._current_regime()
        self._last_regime = regime

        scores: Dict[str, int] = {}
        triggered: bool = False
        details_list: List[Dict[str, Any]] = []

        for name, style in sorted(self._strategy_styles.items()):
            score = self._match_score(style, regime)
            scores[name] = score
            if self._match_score_min is not None and score < self._match_score_min:
                # 低匹配 → P2/WARNING：降频/降仓建议告警（阈值已配置才触发，R-04）
                triggered = True
                dedup_key = f"{DEDUP_MATCH_PREFIX}{name}"
                details: Dict[str, Any] = {
                    "strategy": name,
                    "style": style,
                    "regime": regime,
                    "match_score": score,
                    "match_score_min": self._match_score_min,
                    "suggestion": "策略与当前市场环境匹配度低，建议降频/降仓",
                }
                details_list.append(details)
                try:
                    self._alert_mgr().alert(
                        level=SeverityLevel.WARNING,
                        message=(
                            f"[regime] 策略 {name} 与当前环境 {regime} 匹配度低"
                            f"（{score}<{self._match_score_min}），建议降频/降仓"
                        ),
                        category=CATEGORY_REGIME,
                        source=ALERT_SOURCE,
                        dedup_key=dedup_key,
                        details=details,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("低匹配告警发送失败（不阻断评估）: %s", e)

        self._alert_triggered = triggered
        self._publish_gauges(regime, scores)

        self._last_state = {
            "regime": regime,
            "regime_code": REGIME_STATE_CODE.get(regime, 0.0),
            "match_scores": scores,
            "match_score_min": self._match_score_min,
            "trend_r2_min": self._trend_r2_min,
            "strategy_styles": self.get_strategy_styles(),
            "low_match_alerts": details_list,
            "alert_triggered": triggered,
            "degraded": self._degraded,
            "last_poll_time": now,
            "observed_points": len(self._points),
        }
        return dict(self._last_state)

    def poll(self) -> Dict[str, Any]:
        """周期巡检入口（C-3 触发模式=周期，R-02 非 MTTD 类）。等同 evaluate()。"""
        return self.evaluate()

    # ── 告警管理器（懒加载）────────────────────────────────────

    def _alert_mgr(self) -> Any:
        """延迟解析告警管理器（None 时取全局单例）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager

            mgr = get_alert_manager()
            self._alert_manager = mgr
        return mgr

    # ── 指标落盘（懒加载 collector；set_gauge 自动建指标）────────

    def _publish_gauges(self, regime: str, scores: Dict[str, int]) -> None:
        """写当前环境编码与逐策略匹配分 gauge。采集器不可用仅记 warning。"""
        global_labels: Dict[str, str] = {}
        self._set_gauge(
            METRIC_REGIME_STATE, REGIME_STATE_CODE.get(regime, 0.0), global_labels
        )
        for name, score in scores.items():
            self._set_gauge(
                METRIC_REGIME_MATCH_SCORE,
                float(score),
                {"strategy": name},
            )

    def _set_gauge(self, name: str, value: Optional[float], labels: Dict[str, str]) -> None:
        """set_gauge 一个仪表盘指标；None 落 0.0 占位。"""
        col = self._metrics_collector
        if col is None:
            try:
                from myquant.monitoring.metrics_collector import get_metrics_collector

                col = get_metrics_collector()
                self._metrics_collector = col
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取 metrics_collector 失败（指标 {name} 未写）: {e}")
                return
        try:
            col.set_gauge(name, 0.0 if value is None else float(value), labels=labels)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"指标 {name} 写入失败（不阻断检测）: {e}")

    # ── 状态可观测（供驾驶舱/验收消费；可 JSON 序列化）────────────

    def to_dict(self) -> Dict[str, Any]:
        """返回最新评估快照。未评估时返回初始结构。"""
        if not self._last_state:
            return {
                "regime": "range",
                "regime_code": 0.0,
                "match_scores": {},
                "match_score_min": self._match_score_min,
                "trend_r2_min": self._trend_r2_min,
                "strategy_styles": self.get_strategy_styles(),
                "low_match_alerts": [],
                "alert_triggered": False,
                "degraded": self._degraded,
                "last_poll_time": None,
                "observed_points": 0,
            }
        return dict(self._last_state)

    def summary(self) -> Dict[str, Any]:
        """返回监控结构化快照（驾驶舱/报告消费）。等同 to_dict()。"""
        return self.to_dict()


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：环境识别/匹配评分/低匹配告警/防未来/防御校验。失败抛异常。"""

    class _FakeMetrics:
        """内存桩采集器（不依赖真实 MetricsCollector）。"""

        def __init__(self) -> None:
            self.gauges: Dict[Tuple[str, tuple], float] = {}

        def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
            key = (name, tuple(sorted((labels or {}).items())))
            self.gauges[key] = float(value)

    class _FakeAlerts:
        """内存桩告警管理器（记录告警，不依赖真实 AlertManager）。"""

        def __init__(self) -> None:
            self.alerts: List[Dict[str, Any]] = []

        def alert(self, **kwargs: Any) -> None:
            self.alerts.append(kwargs)

    import numpy as np
    import pandas as pd

    fake_metrics = _FakeMetrics()
    fake_alerts = _FakeAlerts()

    # [1] classify_regime 环境档位（直接验证 indicators 追加能力）
    assert classify_regime(0.9, 0.6) == "trend_up", "RSRS>buy 且 R²足 → trend_up"
    assert classify_regime(-0.9, 0.6) == "trend_down", "RSRS<sell 且 R²足 → trend_down"
    assert classify_regime(0.9, 0.1) == "range", "信号强但结构弱 → 保守震荡"
    assert classify_regime(0.4, 0.5) == "mild_up", "0<rsrs<=buy → mild_up"
    assert classify_regime(-0.4, 0.5) == "mild_down", "sell<=rsrs<0 → mild_down"
    assert classify_regime(0.0, 0.5) == "range", "rsrs=0 → range"
    assert classify_regime(float("nan"), 0.5) == "range", "NaN → range"
    print("[1] classify_regime 环境识别 7 项通过")

    # [2] 匹配评分矩阵
    matcher = RegimeMatcher(
        strategy_styles={"trend_a": "trend", "range_b": "range"},
        match_score_min=50.0,
        trend_r2_min=0.3,
        alert_manager=fake_alerts,
        metrics_collector=fake_metrics,
        clock=lambda: 1_800_000.0,
    )
    matcher.observe_rsrs(1_799_990.0, 0.9, 0.6)  # trend_up 环境
    state = matcher.evaluate()
    assert state["regime"] == "trend_up", f"当前环境应为 trend_up，实际 {state['regime']}"
    assert state["match_scores"]["trend_a"] == 100, "trend 策略 × trend_up = 100"
    assert state["match_scores"]["range_b"] == 40, "range 策略 × trend_up = 40（低匹配）"
    assert state["alert_triggered"] is True, "range_b 低匹配（40<50）应触发告警"
    assert fake_alerts.alerts and fake_alerts.alerts[0]["category"] == "regime", \
        "告警 category 应为 regime"
    assert fake_alerts.alerts[0]["dedup_key"] == "regime:match:range_b", "dedup 键契约"
    assert fake_alerts.alerts[0]["level"] == SeverityLevel.WARNING, "低匹配=P2/WARNING"
    assert "suggestion" in fake_alerts.alerts[0]["details"], "建议（降频/降仓）应附于 details"
    print("[2] 环境判定+匹配评分+低匹配 P2 告警契约 6 项通过")

    # [3] 阈值未配置（PENDING_CAL）不告警不编造
    matcher2 = RegimeMatcher(
        strategy_styles={"trend_a": "trend", "range_b": "range"},
        match_score_min=None,  # PENDING_CAL
        alert_manager=_FakeAlerts(),
        metrics_collector=_FakeMetrics(),
        clock=lambda: 1_800_000.0,
    )
    matcher2.observe_rsrs(1_799_990.0, 0.9, 0.6)
    st2 = matcher2.evaluate()
    assert st2["match_scores"]["range_b"] == 40, "评分照常计算"
    assert st2["alert_triggered"] is False, "阈值未配置不得触发告警"
    print("[3] PENDING_CAL 不告警不编造 2 项通过")

    # [4] 防未来：拒绝未来 RSRS 观测
    matcher3 = RegimeMatcher(
        alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
        clock=lambda: 1_800_000.0,
    )
    try:
        matcher3.observe_rsrs(1_900_000.0, 0.9, 0.6)
        raise AssertionError("未来 RSRS 观测应被拒绝")
    except ValueError:
        pass
    print("[4] 防未来拒绝未来点 1 项通过")

    # [5] 防御校验：非法阈值/空策略名/非法风格/inf
    for bad_kwargs in (
        {"match_score_min": -1.0},
        {"trend_r2_min": 1.5},
        {"buy_threshold": -0.7, "sell_threshold": 0.7},
    ):
        try:
            RegimeMatcher(**bad_kwargs, alert_manager=_FakeAlerts(),
                          metrics_collector=_FakeMetrics())
            raise AssertionError(f"非法参数应报错: {bad_kwargs}")
        except ValueError:
            pass
    try:
        RegimeMatcher().register_strategy_style("", "trend")
        raise AssertionError("空策略名应报错")
    except ValueError:
        pass
    try:
        RegimeMatcher().register_strategy_style("s", "mean_reversion")
        raise AssertionError("非法风格应报错")
    except ValueError:
        pass
    try:
        matcher3.observe_rsrs(1_700_000.0, float("inf"), 0.6)
        raise AssertionError("inf 应报错")
    except ValueError:
        pass
    print("[5] 防御校验 6 项通过")

    # [6] 指标注册（set_gauge 动态注册，labels 正确）
    gauge_state = fake_metrics.gauges.get((METRIC_REGIME_STATE, ()))
    assert gauge_state == 3.0, "regime_state gauge 应为 trend_up 编码 3.0"
    gauge_trend = fake_metrics.gauges.get(
        (METRIC_REGIME_MATCH_SCORE, (("strategy", "trend_a"),))
    )
    assert gauge_trend == 100.0, "regime_match_score gauge(trend_a) 应为 100"
    gauge_range = fake_metrics.gauges.get(
        (METRIC_REGIME_MATCH_SCORE, (("strategy", "range_b"),))
    )
    assert gauge_range == 40.0, "regime_match_score gauge(range_b) 应为 40"
    assert REGISTERED_METRICS == [METRIC_REGIME_STATE, METRIC_REGIME_MATCH_SCORE]
    print("[6] 指标 set_gauge 注册 4 项通过")

    # [7] 冷启动：无观测保守归 range，不误判方向；range 风格策略不误告警
    matcher4 = RegimeMatcher(
        strategy_styles={"range_b": "range"}, match_score_min=80.0,
        alert_manager=_FakeAlerts(), metrics_collector=_FakeMetrics(),
        clock=lambda: 1_800_000.0,
    )
    st4 = matcher4.evaluate()
    assert st4["regime"] == "range", "冷启动无观测保守归 range，不猜方向"
    assert st4["match_scores"]["range_b"] == 100, "range 风格 × range 环境 = 100（高匹配）"
    assert st4["alert_triggered"] is False, "冷启动高匹配不应误告警"
    print("[7] 冷启动保守 3 项通过")

    print("\n✓ regime_matcher.py 自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
