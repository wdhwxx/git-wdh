# 文件名：myquant/monitoring/dynamic_baseline.py
"""
dynamic_baseline.py - 动态基线 + 异常检测（C-6，回写#235）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》C-6：
  滚动基线（均值±k×std 或分位数带）替代静态阈值；同环比变化率；
  多条件组合判定（AND/OR 可配，条件结构可注入）；结构化 anomaly 事件；
  冷启动静态阈值兜底；regime 自适应（为 D-3 阈值自动校准打底）。

触发模式（R-02）：事件驱动（record() 每摄入一个"已发生"点即判定一次）
  + 可选 replay() 注入历史数组批量回放。

计算口径（全部基于"已发生"的历史点，严格防未来）：
  - 数据点 = (timestamp, value)，按 (series, regime) 归组；只消费
    timestamp<=now 的点（record() 拒绝未来点）。
  - 滚动基线：新点入账后，取"该点之前"的 trailing window 个历史点
    （严格过去，不含新点自身，避免新点污染自身基线）：
      · zscore 法（默认）：baseline_low/high = mean ∓ k×std（样本标准差）；
      · quantile 法：baseline_low/high = 经验分位带（默认 P5/P95）。
    样本 < min_samples → 冷启动，回退静态阈值判定（见下）。
  - 同环比（变化率，以小数计，0.3=30%）：
      · 环比 mom = (当前值 / 上一已发生值 - 1)；
      · 同比 yoy = (当前值 / 前 yoy_lag 个周期值 - 1)（"上周同期/上一周期"，
        lag 为样本数，算法参数可注入）。分母为 0 → None（不编造）。
  - 多条件组合判定：conditions = {"combine": "AND"|"OR", "rules": [...]}，
    规则类型：
      · {"type": "out_of_baseline"}          —— 当前值落在基线带外（冷启动时
                                              为落在静态阈值带外）；
      · {"type": "mom_ratio", "side": "up"|"down"|"both",
         "min_ratio": float}                 —— 环比变化率超阈；
      · {"type": "yoy_ratio", "side": ..., "min_ratio": float}。
    AND=全部规则命中才判异常；OR=任一命中即判异常。
    缺省 conditions = 仅 out_of_baseline（最可解释的默认）。

阈值纪律（R-04）：
  - 窗口 window / 倍数 k / 分位 / yoy_lag / min_ratio 等属"算法参数"
    （模块常量 + 构造可注入），不是 SLO 数值阈值。
  - 冷启动静态阈值为"业务阈值"：构造注入优先；否则读
    monitoring.slo.dynamic_baseline.static_low/static_high 真源；
    二者皆 None（PENDING_CAL 未配置）→ 冷启动期只记录不告警、不编造数值。

regime 自适应（为 D-3 打底）：
  - set_regime(label) 切换市场状态标签；按 (series, regime) 分组维护
    独立历史缓冲 → 不同 regime 各自独立学习基线，切换即"换用"对应 regime
    的基线（新 regime 冷启动 → 静态阈值兜底）。
  - 每次切换留痕（old→new、时刻），regime_history() 可查。

告警契约（可选接入）：
  - 判定异常时经 get_alert_manager().alert() 发 WARNING
    （category=dynamic_baseline、source=monitoring.dynamic_baseline、
    deduplication_key=dynamic_baseline:<series>:<regime> 防刷屏、
    details 附整条 anomaly 事件 JSON）。

防未来：record() 拒绝 timestamp>now 的点；基线/同环比只用"该点之前"的历史。
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.dynamic_baseline")

# ─────────────────────────────────────────────
# 常量（算法参数 = 模块缺省值，构造可注入；非 SLO 业务阈值）
# ─────────────────────────────────────────────
#: 告警分类（决定默认 Runbook 兜底，A-10）
CATEGORY_BASELINE: str = "dynamic_baseline"

#: 告警来源
ALERT_SOURCE: str = "monitoring.dynamic_baseline"

#: 缺省 regime 标签（未显式 set_regime 时）
DEFAULT_REGIME: str = "default"

#: 滚动窗口样本数（trailing，不含当前点）
DEFAULT_WINDOW: int = 30

#: zscore 法基线倍数 k（mean ± k×std）
DEFAULT_K: float = 3.0

#: 冷启动最小样本数占窗口比例（min_samples = ceil(window × ratio)，下限 2）
MIN_SAMPLES_RATIO: float = 0.5

#: 冷启动最小样本数绝对下限
MIN_SAMPLES_FLOOR: int = 2

#: 基线方法："zscore"（mean±k×std）或 "quantile"（经验分位带）
DEFAULT_METHOD: str = "zscore"

#: quantile 法缺省低/高分位
DEFAULT_QUANTILE_LOW: float = 0.05
DEFAULT_QUANTILE_HIGH: float = 0.95

#: 同比 lag（样本数；"上周同期"按采样密度换算，算法参数）
DEFAULT_YOY_LAG: int = 24

#: 同环比规则缺省最小变化率（小数，0.3=30%）
DEFAULT_RATIO_MIN: float = 0.3

#: 每 (series, regime) 点缓存上限（超出丢最旧，防无界增长）
MAX_POINTS_PER_KEY: int = 5000

#: 未来点判定容差（秒）
_FUTURE_EPS: float = 1.0

#: 告警去重键前缀（后缀拼 series:regime，固定窗口内不刷屏）
DEDUP_PREFIX: str = "dynamic_baseline:"

# ── 指标名（set_gauge 进 collector；labels={"series","regime"}）─────────────
METRIC_BASELINE_MEAN: str = "dynamic_baseline_baseline_mean"
METRIC_BASELINE_LOW: str = "dynamic_baseline_baseline_low"
METRIC_BASELINE_HIGH: str = "dynamic_baseline_baseline_high"
METRIC_MOM_RATIO: str = "dynamic_baseline_mom_ratio"
METRIC_YOY_RATIO: str = "dynamic_baseline_yoy_ratio"
METRIC_ANOMALY: str = "dynamic_baseline_anomaly"
METRIC_POINT_COUNT: str = "dynamic_baseline_point_count"

#: 本模块注册的全部指标名（供主控/驾驶舱查询）
REGISTERED_METRICS: List[str] = [
    METRIC_BASELINE_MEAN,
    METRIC_BASELINE_LOW,
    METRIC_BASELINE_HIGH,
    METRIC_MOM_RATIO,
    METRIC_YOY_RATIO,
    METRIC_ANOMALY,
    METRIC_POINT_COUNT,
]


class DynamicBaseline:
    """
    动态基线异常检测器（事件驱动 + 历史回放；严格防未来）。

    用法:
        from myquant.monitoring.dynamic_baseline import DynamicBaseline

        db = DynamicBaseline()                       # 算法参数用缺省
        ev = db.record("latency_ms", ts, 42.0)      # 喂已发生点；返回 anomaly 事件或 None
        db.set_regime("volatile")                    # 切换 regime（独立基线）
        events = db.replay("latency_ms", [(ts0, v0), ...])
    """

    def __init__(
        self,
        window: int = DEFAULT_WINDOW,
        k: float = DEFAULT_K,
        method: str = DEFAULT_METHOD,
        quantile_low: float = DEFAULT_QUANTILE_LOW,
        quantile_high: float = DEFAULT_QUANTILE_HIGH,
        yoy_lag: int = DEFAULT_YOY_LAG,
        min_samples: Optional[int] = None,
        conditions: Optional[Dict[str, Any]] = None,
        static_thresholds: Optional[Dict[str, Tuple[float, float]]] = None,
        static_default: Optional[Tuple[float, float]] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
        max_points_per_key: int = MAX_POINTS_PER_KEY,
    ) -> None:
        """
        Parameters
        ----------
        window : int
            滚动基线窗口样本数（trailing，>0）。
        k : float
            zscore 法倍数（mean ± k×std），>=0。
        method : str
            "zscore" 或 "quantile"。
        quantile_low / quantile_high : float
            quantile 法低/高分位（0<low<high<1）。
        yoy_lag : int
            同比回看样本数（>=1）。
        min_samples : int | None
            冷启动最小样本数；None 自动 ceil(window×ratio)，下限 2。
        conditions : dict | None
            多条件组合结构 {"combine":"AND|OR","rules":[...]}；
            None 缺省为仅 out_of_baseline。
        static_thresholds : dict | None
            冷启动静态阈值兜底 {series: (low, high)}，构造注入优先。
        static_default : tuple | None
            冷启动静态阈值缺省 (low, high)（无 per-series 命中时用）。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        max_points_per_key : int
            每 (series, regime) 缓存上限（正整数）。
        """
        # ── 防御式校验算法参数 ──
        if not isinstance(window, int) or window <= 0:
            raise ValueError(f"window 必须为正整数，收到 {window!r}")
        try:
            kf = float(k)
        except (TypeError, ValueError):
            raise ValueError(f"k 必须为数值，收到 {k!r}") from None
        if kf < 0:
            raise ValueError(f"k 不能为负，收到 {kf!r}")
        if method not in ("zscore", "quantile"):
            raise ValueError(f"method 必须为 zscore/quantile，收到 {method!r}")
        try:
            ql = float(quantile_low)
            qh = float(quantile_high)
        except (TypeError, ValueError):
            raise ValueError("quantile_low/high 必须为数值") from None
        if not (0.0 < ql < qh < 1.0):
            raise ValueError(f"需满足 0<quantile_low<quantile_high<1，收到 {ql}/{qh}")
        if not isinstance(yoy_lag, int) or yoy_lag < 1:
            raise ValueError(f"yoy_lag 必须为 >=1 的整数，收到 {yoy_lag!r}")
        if max_points_per_key <= 0:
            raise ValueError(f"max_points_per_key 必须为正整数，收到 {max_points_per_key}")

        self._window: int = window
        self._k: float = kf
        self._method: str = method
        self._q_low: float = ql
        self._q_high: float = qh
        self._yoy_lag: int = yoy_lag
        self._min_samples: int = (
            int(min_samples)
            if min_samples is not None
            else max(MIN_SAMPLES_FLOOR, math.ceil(window * MIN_SAMPLES_RATIO))
        )
        if self._min_samples < 1:
            raise ValueError(f"min_samples 必须 >=1，收到 {self._min_samples}")

        # ── 多条件组合结构（注入校验；缺省=仅 out_of_baseline）──
        self._conditions: Dict[str, Any] = self._normalize_conditions(conditions)

        # ── 冷启动静态阈值兜底（R-04：注入 > SLO 真源；None=PENDING_CAL 不告警）──
        self._static_thresholds: Dict[str, Tuple[float, float]] = (
            dict(static_thresholds) if static_thresholds else {}
        )
        self._static_default: Optional[Tuple[float, float]] = self._validate_static_tuple(
            static_default, "static_default"
        )
        self._slo_static: Optional[Tuple[float, float]] = self._read_slo_static()

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector
        self._max_points = int(max_points_per_key)

        # 缓冲：{(series, regime): [(ts, value), ...]}（按 ts 升序）
        self._buffers: Dict[Tuple[str, str], List[Tuple[float, float]]] = {}

        # regime 状态与切换留痕
        self._regime: str = DEFAULT_REGIME
        self._regime_history: List[Dict[str, Any]] = [
            {"time": self._clock(), "old": None, "new": DEFAULT_REGIME}
        ]

        # 最近一次评估快照（供 to_dict 观测）
        self._last_event: Optional[Dict[str, Any]] = None
        self._anomaly_count: int = 0

        logger.debug(
            "DynamicBaseline 初始化: window=%d k=%.2f method=%s min_samples=%d "
            "yoy_lag=%d conditions=%s",
            self._window, self._k, self._method, self._min_samples,
            self._yoy_lag, self._conditions,
        )

    # ── 条件结构归一化与校验 ───────────────────────────────────────────
    @staticmethod
    def _normalize_conditions(conditions: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        归一化多条件组合结构；None → 缺省仅 out_of_baseline。

        Parameters
        ----------
        conditions : dict | None
            {"combine": "AND"|"OR", "rules": [{"type": ..., ...}, ...]}。

        Returns
        -------
        dict : 归一化后的条件结构。

        Raises
        ------
        ValueError : 结构非法（combine/rules/规则 type）时抛出。
        """
        if conditions is None:
            return {"combine": "AND", "rules": [{"type": "out_of_baseline"}]}
        if not isinstance(conditions, dict):
            raise ValueError(f"conditions 必须为 dict，收到 {type(conditions).__name__}")
        combine = str(conditions.get("combine", "AND")).upper()
        if combine not in ("AND", "OR"):
            raise ValueError(f"conditions.combine 必须为 AND/OR，收到 {combine!r}")
        rules = conditions.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ValueError("conditions.rules 必须为非空 list")
        valid_types = ("out_of_baseline", "mom_ratio", "yoy_ratio")
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ValueError(f"conditions.rules[{i}] 必须为 dict")
            rtype = rule.get("type")
            if rtype not in valid_types:
                raise ValueError(
                    f"conditions.rules[{i}].type 非法: {rtype!r}（可选 {valid_types}）"
                )
            if rtype in ("mom_ratio", "yoy_ratio"):
                side = str(rule.get("side", "both")).lower()
                if side not in ("up", "down", "both"):
                    raise ValueError(
                        f"conditions.rules[{i}].side 必须为 up/down/both，收到 {side!r}"
                    )
                try:
                    mr = float(rule.get("min_ratio", DEFAULT_RATIO_MIN))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"conditions.rules[{i}].min_ratio 必须为数值"
                    ) from None
                if mr < 0:
                    raise ValueError(
                        f"conditions.rules[{i}].min_ratio 不能为负，收到 {mr}"
                    )
        return {"combine": combine, "rules": list(rules)}

    @staticmethod
    def _validate_static_tuple(
        t: Any, name: str
    ) -> Optional[Tuple[float, float]]:
        """把静态阈值二元组校验解析为 (low, high)；None 透传。"""
        if t is None:
            return None
        if not (isinstance(t, (tuple, list)) and len(t) == 2):
            raise ValueError(f"{name} 必须为 (low, high) 二元组，收到 {t!r}")
        try:
            low = float(t[0])
            high = float(t[1])
        except (TypeError, ValueError):
            raise ValueError(f"{name} 含非数值，收到 {t!r}") from None
        if not (math.isfinite(low) and math.isfinite(high)):
            raise ValueError(f"{name} 必须为有限数，收到 {t!r}")
        if low > high:
            raise ValueError(f"{name} 需 low<=high，收到 ({low}, {high})")
        return (low, high)

    @staticmethod
    def _read_slo_static() -> Optional[Tuple[float, float]]:
        """
        读 monitoring.slo.dynamic_baseline.static_low/static_high（R-04 真源）。

        Returns
        -------
        tuple | None : (static_low, static_high)；缺任一项或非法 → None（PENDING_CAL）。
        """
        try:
            slo = get_monitoring_config().get("slo", {}).get("dynamic_baseline", {}) or {}
            low = slo.get("static_low")
            high = slo.get("static_high")
            if low is None or high is None:
                return None
            low_f = float(low)
            high_f = float(high)
            if not (math.isfinite(low_f) and math.isfinite(high_f)) or low_f > high_f:
                logger.warning("slo.dynamic_baseline.static_low/high 非法，按未配置处理")
                return None
            return (low_f, high_f)
        except Exception as e:  # noqa: BLE001 —— 配置读取失败不阻断监控
            logger.warning("读取 slo.dynamic_baseline 失败，按未配置处理: %s", e)
            return None

    def _resolve_static(self, series: str) -> Optional[Tuple[float, float]]:
        """解析冷启动静态阈值：per-series 注入 > 缺省注入 > SLO 真源。"""
        if series in self._static_thresholds:
            return self._static_thresholds[series]
        if self._static_default is not None:
            return self._static_default
        return self._slo_static

    # ── regime 管理 ───────────────────────────────────────────────────
    def set_regime(self, label: str) -> None:
        """
        切换 regime 标签（按 (series, regime) 独立维护基线；切换留痕）。

        Parameters
        ----------
        label : str
            新 regime 标签（非空字符串）。

        Raises
        ------
        ValueError : label 非法时抛出。
        """
        if not label or not isinstance(label, str):
            raise ValueError(f"regime 标签必须为非空字符串，收到 {label!r}")
        if label == self._regime:
            return
        old = self._regime
        self._regime = label
        rec = {"time": self._clock(), "old": old, "new": label}
        self._regime_history.append(rec)
        logger.info("regime 切换: %s → %s", old, label)

    def regime_history(self) -> List[Dict[str, Any]]:
        """返回 regime 切换留痕（按时间升序，含初始 default）。"""
        return list(self._regime_history)

    @property
    def regime(self) -> str:
        """当前 regime 标签。"""
        return self._regime

    # ── 数据摄入（事件驱动；严格防未来）───────────────────────────────
    def record(self, series: str, timestamp: float, value: float) -> Optional[Dict[str, Any]]:
        """
        摄入一个"已发生"数据点并立即做异常判定。

        防未来：timestamp 超过 now（容差 1s）即拒绝抛 ValueError；
        同 (series, regime, ts) 幂等（覆盖旧值）。

        Parameters
        ----------
        series : str
            指标/序列名（非空）。
        timestamp : float
            数据点时间戳（epoch 秒，必须 <= now）。
        value : float
            该时刻观测值（有限数）。

        Returns
        -------
        dict | None : 判定为异常时返回结构化 anomaly 事件（JSON 可序列化）；
            健康/冷启动无静态阈值/未达条件 → None。

        Raises
        ------
        ValueError : series 为空 / timestamp 未来 / value 非有限数时抛出。
        """
        if not series or not isinstance(series, str):
            raise ValueError(f"series 必须为非空字符串，收到 {series!r}")
        try:
            ts = float(timestamp)
            v = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"timestamp/value 必须可转 float，收到 ts={timestamp!r} value={value!r}"
            ) from e
        if not math.isfinite(ts) or not math.isfinite(v):
            raise ValueError(f"timestamp/value 必须为有限数，收到 ts={ts!r} value={v!r}")
        now = self._clock()
        if ts > now + _FUTURE_EPS:
            raise ValueError(
                f"拒绝未来数据点：timestamp={ts:.3f} 晚于当前 {now:.3f}（防未来红线）"
            )

        key = (series, self._regime)
        buf = self._buffers.setdefault(key, [])
        # 幂等去重（同 ts 覆盖，保持升序）
        replaced = False
        for i, (old_ts, _) in enumerate(buf):
            if abs(old_ts - ts) <= 1e-9:
                buf[i] = (ts, v)
                replaced = True
                break
        if not replaced:
            buf.append((ts, v))
            buf.sort(key=lambda p: p[0])
        if len(buf) > self._max_points:
            del buf[: len(buf) - self._max_points]

        # 立即判定（基线只用该点之前的历史，严格防未来）
        try:
            event = self._evaluate(series, ts, v)
        except Exception as e:  # noqa: BLE001 —— 单点判定异常不阻断摄入
            logger.warning("series=%s 点判定异常（已摄入但跳过告警）: %s", series, e)
            event = None

        # 指标落盘（labels=series+regime）
        self._publish_gauges(series, v, event)
        return event

    def replay(
        self,
        series: str,
        points: List[Tuple[float, float]],
    ) -> List[Dict[str, Any]]:
        """
        注入历史数组批量回放（按时间升序逐点 record，收集 anomaly 事件）。

        Parameters
        ----------
        series : str
            序列名。
        points : list[(timestamp, value)]
            历史点数组（须全部 <= 当前 clock，否则 record 拒未来）。

        Returns
        -------
        list[dict] : 回放过程中产生的 anomaly 事件列表（可能为空）。

        Raises
        ------
        ValueError : series 为空 / points 非 list 时抛出。
        """
        if not series or not isinstance(series, str):
            raise ValueError(f"series 必须为非空字符串，收到 {series!r}")
        if not isinstance(points, list):
            raise ValueError(f"points 必须为 list，收到 {type(points).__name__}")
        events: List[Dict[str, Any]] = []
        for ts, v in points:
            ev = self.record(series, float(ts), float(v))
            if ev is not None:
                events.append(ev)
        logger.info("dynamic_baseline 回放: series=%s 点=%d 异常=%d", series, len(points), len(events))
        return events

    # ── 核心判定 ─────────────────────────────────────────────────────
    def _evaluate(
        self, series: str, ts: float, value: float
    ) -> Optional[Dict[str, Any]]:
        """
        基于"该点之前"的历史计算基线/同环比并执行多条件组合判定。

        本行使用的数据截至 ts 时刻（不含未来）。
        """
        key = (series, self._regime)
        buf = self._buffers[key]
        # hist = 当前点之前的全部历史（buf[-1] 即刚入账的当前点）
        hist = buf[:-1]
        window = hist[-self._window:]  # trailing window（严格过去）

        cold_start = len(window) < self._min_samples
        baseline_mean: Optional[float] = None
        baseline_low: Optional[float] = None
        baseline_high: Optional[float] = None
        out_of_band = False

        if cold_start:
            # 冷启动：回退静态阈值（未注入/SLO None → 不告警）
            static = self._resolve_static(series)
            if static is not None:
                out_of_band = value < static[0] or value > static[1]
        else:
            if self._method == "zscore":
                vals = [v for _, v in window]
                mean = sum(vals) / len(vals)
                if len(vals) > 1:
                    var = sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)
                    std = math.sqrt(var)
                else:
                    std = 0.0
                baseline_mean = mean
                baseline_low = mean - self._k * std
                baseline_high = mean + self._k * std
            else:  # quantile
                vals = sorted(v for _, v in window)
                baseline_low = self._quantile(vals, self._q_low)
                baseline_high = self._quantile(vals, self._q_high)
                baseline_mean = sum(vals) / len(vals)
            out_of_band = value < baseline_low or value > baseline_high

        # ── 同环比（变化率，小数）──
        mom_ratio: Optional[float] = None
        if len(hist) >= 1:
            prev_v = hist[-1][1]
            if prev_v != 0:
                mom_ratio = (value - prev_v) / abs(prev_v)
        yoy_ratio: Optional[float] = None
        if len(hist) >= self._yoy_lag:
            yoy_v = hist[-self._yoy_lag][1]
            if yoy_v != 0:
                yoy_ratio = (value - yoy_v) / abs(yoy_v)

        # ── 多条件组合 ──
        conditions_hit = self._eval_rules(out_of_band, mom_ratio, yoy_ratio)
        combine = self._conditions["combine"]
        rules = self._conditions["rules"]
        if combine == "OR":
            is_anomaly = len(conditions_hit) > 0
        else:  # AND
            is_anomaly = len(conditions_hit) == len(rules)

        if not is_anomaly:
            return None

        event: Dict[str, Any] = {
            "time": ts,
            "series": series,
            "value": round(value, 6),
            "baseline_low": self._rnd(baseline_low),
            "baseline_high": self._rnd(baseline_high),
            "baseline_mean": self._rnd(baseline_mean),
            "ratio": {
                "mom": self._rnd(mom_ratio),
                "yoy": self._rnd(yoy_ratio),
            },
            "conditions_hit": conditions_hit,
            "regime": self._regime,
            "cold_start": cold_start,
        }
        self._last_event = event
        self._anomaly_count += 1
        self._fire_alert(series, event)
        return event

    def _eval_rules(
        self,
        out_of_band: bool,
        mom_ratio: Optional[float],
        yoy_ratio: Optional[float],
    ) -> List[str]:
        """逐条评估规则，返回命中的规则 type 列表。"""
        hit: List[str] = []
        for rule in self._conditions["rules"]:
            rtype = rule["type"]
            if rtype == "out_of_baseline":
                if out_of_band:
                    hit.append("out_of_baseline")
            elif rtype == "mom_ratio":
                if mom_ratio is not None and self._ratio_breach(mom_ratio, rule):
                    hit.append("mom_ratio")
            elif rtype == "yoy_ratio":
                if yoy_ratio is not None and self._ratio_breach(yoy_ratio, rule):
                    hit.append("yoy_ratio")
        return hit

    @staticmethod
    def _ratio_breach(ratio: float, rule: Dict[str, Any]) -> bool:
        """判断同环比变化率是否超阈（side=up/down/both）。"""
        min_ratio = abs(float(rule.get("min_ratio", DEFAULT_RATIO_MIN)))
        side = str(rule.get("side", "both")).lower()
        if side == "up":
            return ratio > min_ratio
        if side == "down":
            return ratio < -min_ratio
        return abs(ratio) > min_ratio

    @staticmethod
    def _quantile(sorted_vals: List[float], q: float) -> float:
        """线性插值经验分位（sorted_vals 已升序；q∈[0,1]）。"""
        n = len(sorted_vals)
        if n == 1:
            return sorted_vals[0]
        idx = q * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return sorted_vals[lo]
        frac = idx - lo
        return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac

    @staticmethod
    def _rnd(x: Optional[float]) -> Optional[float]:
        """四舍五入到 6 位小数（None 透传），保证 JSON 干净。"""
        return round(x, 6) if x is not None else None

    # ── 告警契约 ─────────────────────────────────────────────────────
    def _fire_alert(self, series: str, event: Dict[str, Any]) -> bool:
        """发送一条 WARNING 告警（category=dynamic_baseline；失败记 warning 不阻断）。"""
        try:
            self._alert_mgr().alert(
                level=SeverityLevel.WARNING,
                message=(
                    f"[dynamic_baseline] series={series} 脱离动态基线 "
                    f"(value={event['value']}, regime={event['regime']}, "
                    f"hit={event['conditions_hit']})"
                ),
                source=ALERT_SOURCE,
                category=CATEGORY_BASELINE,
                details={"anomaly": event},
                deduplication_key=f"{DEDUP_PREFIX}{series}:{event['regime']}",
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("series=%s 异常告警发送失败（不阻断检测）: %s", series, e)
            return False

    def _alert_mgr(self) -> Any:
        """延迟解析告警管理器（None 时取全局单例）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager

            mgr = get_alert_manager()
            self._alert_manager = mgr
        return mgr

    # ── 指标落盘（懒加载 collector；set_gauge 自动建指标）──────────────
    def _publish_gauges(
        self,
        series: str,
        value: float,
        event: Optional[Dict[str, Any]],
    ) -> None:
        """把最新基线/同环比/异常标记写 set_gauge（labels={series,regime}）。"""
        labels: Dict[str, str] = {"series": series, "regime": self._regime}
        is_anomaly = event is not None
        gauges: Dict[str, float] = {
            METRIC_BASELINE_MEAN: (event or {}).get("baseline_mean"),
            METRIC_BASELINE_LOW: (event or {}).get("baseline_low"),
            METRIC_BASELINE_HIGH: (event or {}).get("baseline_high"),
            METRIC_MOM_RATIO: ((event or {}).get("ratio") or {}).get("mom"),
            METRIC_YOY_RATIO: ((event or {}).get("ratio") or {}).get("yoy"),
        }
        for name, v in gauges.items():
            self._set_gauge(name, v, labels)
        # 异常标记：1.0=本点异常，0.0=健康（恒落盘，便于面板看突变）
        self._set_gauge(METRIC_ANOMALY, 1.0 if is_anomaly else 0.0, labels)
        # 样本数（当前 regime 缓冲长度）
        n = len(self._buffers.get((series, self._regime), []))
        self._set_gauge(METRIC_POINT_COUNT, float(n), labels)

    def _set_gauge(self, name: str, value: Optional[float], labels: Dict[str, str]) -> None:
        """set_gauge 一个仪表盘指标；None（样本不足）落 0.0 占位。"""
        col = self._metrics_collector
        if col is None:
            try:
                from myquant.monitoring.metrics_collector import get_metrics_collector

                col = get_metrics_collector()
                self._metrics_collector = col
            except Exception as e:  # noqa: BLE001
                logger.warning("获取 metrics_collector 失败（指标未写入）: %s", e)
                return
        try:
            col.set_gauge(name, 0.0 if value is None else float(value), labels=labels)
        except Exception as e:  # noqa: BLE001
            logger.warning("指标 %s 写入失败（不阻断检测）: %s", name, e)

    # ── 状态可观测 ────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        """返回检测器快照（最近 anomaly 事件 + regime + 累计异常数 + 参数）。"""
        return {
            "regime": self._regime,
            "last_event": self._last_event,
            "anomaly_count": self._anomaly_count,
            "window": self._window,
            "k": self._k,
            "method": self._method,
            "min_samples": self._min_samples,
            "yoy_lag": self._yoy_lag,
            "regime_history": self.regime_history(),
        }


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：固定时钟 + 假告警/假指标，演示正常段→偏离段→anomaly + regime 切换。"""
    import json

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

        def alert(self, **kw: Any) -> int:
            self.calls.append(kw)
            return 1

    class _FakeMetrics:
        def __init__(self) -> None:
            self.gauges: Dict[str, float] = {}

        def set_gauge(self, name: str, value: float, labels: Any = None) -> None:
            lbl = labels or {}
            self.gauges[f"{name}:{lbl.get('series','?')}:{lbl.get('regime','?')}"] = float(value)

    base = 1_700_000_000.0
    step = 60.0  # 1 分钟一个点
    # 固定"现在"：远大于所有喂入点的 ts（全部历史点 <= now，严格防未来）
    clock_now = [base + 1_000_000.0]

    def clock() -> float:
        return clock_now[0]

    def ts(k: int) -> float:
        """第 k 个采样点的时间戳（k 从 0 起，全部 <= clock_now）。"""
        return base + k * step

    # ── 场景 1：正常段（围绕 mean=100, std≈1 的平稳序列）──
    am = _FakeAlert()
    met = _FakeMetrics()
    db = DynamicBaseline(
        window=20, k=3.0, min_samples=10, yoy_lag=5,
        alert_manager=am, metrics_collector=met, clock=clock,
    )
    normal = [100.0, 101.0, 99.0, 100.5, 99.5, 100.2, 99.8, 100.1, 99.9, 100.0,
              100.3, 99.7, 100.1, 99.9, 100.0, 100.2, 99.8, 100.0, 100.1, 99.9]
    for i, v in enumerate(normal):
        ev = db.record("latency_ms", ts(i), v)
        check(f"1) 正常段第{i}点不告警", ev is None and am.calls == [], f"ev={ev}")

    # ── 场景 2：偏离段（k=30 突跳到 120，远超 mean±3σ≈100±3）──
    ev = db.record("latency_ms", ts(30), 120.0)
    check("2) 偏离段产出 anomaly 事件", ev is not None, f"ev={ev}")
    check("2) anomaly 含必备字段", ev is not None and all(
        k in ev for k in ("time", "series", "value", "baseline_low",
                          "baseline_high", "ratio", "conditions_hit")
    ), f"keys={list(ev.keys()) if ev else None}")
    check("2) 基线带覆盖 100", ev is not None and ev["baseline_mean"] is not None
          and abs(ev["baseline_mean"] - 100.0) < 1.0, f"ev={ev}")
    check("2) 触发 out_of_baseline", ev is not None and "out_of_baseline" in ev["conditions_hit"])
    check("2) anomaly JSON 可序列化", ev is not None and bool(json.dumps(ev, ensure_ascii=False)))
    check("2) 发 WARNING 告警", len(am.calls) == 1 and am.calls[0]["level"].name == "WARNING",
          f"calls={am.calls}")
    check("2) 告警 category=dynamic_baseline", am.calls[0]["category"] == "dynamic_baseline")
    check("2) 告警 details 附 anomaly", "anomaly" in am.calls[0]["details"])
    check("2) dedup 键防刷屏", am.calls[0]["deduplication_key"] == "dynamic_baseline:latency_ms:default")

    # ── 场景 3：regime 切换到 "volatile"（均值抬到 200；旧 regime 基线不再适用）──
    db.set_regime("volatile")
    check("3) regime 已切换", db.regime == "volatile")
    check("3) 切换留痕", len(db.regime_history()) == 2
          and db.regime_history()[-1]["old"] == "default"
          and db.regime_history()[-1]["new"] == "volatile")
    # 新 regime 冷启动（缓冲空）→ 无静态阈值 → 即使跳到 999 也不告警（仅记录）
    am.calls.clear()
    ev_cold = db.record("latency_ms", ts(31), 999.0)
    check("3) 新 regime 冷启动无静态阈值→不告警不编造", ev_cold is None and am.calls == [],
          f"ev={ev_cold}")
    # 喂满新 regime 的正常段（mean≈200，k=32..51 共 20 点，window=20 把 k=31 的 999 排出窗）
    vol = [200.0, 201.0, 199.0, 200.5, 199.5, 200.2, 199.8, 200.1, 199.9, 200.0,
           200.1, 199.9, 200.3, 199.7, 200.0, 200.2, 199.8, 200.0, 200.1, 199.9]
    for i, v in enumerate(vol):
        db.record("latency_ms", ts(32 + i), v)
    # 新 regime 下突跳到 260（k=60，相对 mean≈200 的 ~1σ 带外）→ 新基线命中
    ev2 = db.record("latency_ms", ts(60), 260.0)
    check("3) regime 切换后基线自适应（新 regime 突跳再次命中 anomaly）",
          ev2 is not None and ev2["regime"] == "volatile", f"ev2={ev2}")
    check("3) 新基线均值≈200", ev2 is not None and ev2["baseline_mean"] is not None
          and abs(ev2["baseline_mean"] - 200.0) < 2.0, f"ev2={ev2}")

    # ── 场景 4：冷启动静态阈值兜底（注入 static_default）──
    am2 = _FakeAlert()
    db2 = DynamicBaseline(
        window=20, min_samples=10, static_default=(50.0, 150.0),
        alert_manager=am2, metrics_collector=_FakeMetrics(), clock=clock,
    )
    # 只喂 3 个点（<min_samples=10），仍冷启动；值 200 超静态上限 150 → 告警
    for i in range(3):
        db2.record("qps", base + i * step, 100.0 + i)
    ev3 = db2.record("qps", base + 5 * step, 200.0)
    check("4) 冷启动静态阈值兜底告警", ev3 is not None and ev3["cold_start"] is True, f"ev3={ev3}")

    # ── 场景 5：None 配置（PENDING_CAL）不告警不编造 ──
    am3 = _FakeAlert()
    db3 = DynamicBaseline(
        window=20, min_samples=10,
        alert_manager=am3, metrics_collector=_FakeMetrics(), clock=clock,
    )
    for i in range(3):
        db3.record("latency_ms", base + i * step, 100.0)
    ev4 = db3.record("latency_ms", base + 5 * step, 9999.0)
    check("5) 冷启动无静态阈值（SLO=None）不告警不编造", ev4 is None and am3.calls == [], f"ev4={ev4}")

    # ── 场景 6：防未来拒绝未来点 ──
    rejected = False
    try:
        db3.record("latency_ms", clock_now[0] + 100.0, 1.0)
    except ValueError:
        rejected = True
    check("6) 拒绝未来点", rejected)

    # ── 场景 7：多条件 AND 组合（out_of_baseline AND mom_ratio 才判异常）──
    am4 = _FakeAlert()
    cond = {
        "combine": "AND",
        "rules": [
            {"type": "out_of_baseline"},
            {"type": "mom_ratio", "side": "both", "min_ratio": 0.5},
        ],
    }
    db4 = DynamicBaseline(
        window=10, k=3.0, min_samples=5, yoy_lag=3,
        conditions=cond, alert_manager=am4, metrics_collector=_FakeMetrics(), clock=clock,
    )
    # 喂平稳段 mean=100
    for i in range(10):
        db4.record("x", base + i * step, 100.0 + (i % 3) * 0.1)
    # 单点小幅上跳 105（在基线带内/边际，且 mom≈5%<50%）→ AND 不命中
    ev5 = db4.record("x", base + 11 * step, 105.0)
    check("7) AND 组合：仅基线边际但环比未达50% → 不告警", ev5 is None, f"ev5={ev5}")
    # 巨跳 200（既出基线带，mom≈+100%>50%）→ AND 命中
    ev6 = db4.record("x", base + 12 * step, 200.0)
    check("7) AND 组合：出带且环比>50% → 异常", ev6 is not None
          and set(ev6["conditions_hit"]) == {"out_of_baseline", "mom_ratio"}, f"ev6={ev6}")

    # ── 场景 8：防御式校验 ──
    bad = False
    try:
        db4.record("", 1.0, 1.0)
    except ValueError:
        bad = True
    check("8) 空 series 拒绝", bad)
    bad2 = False
    try:
        DynamicBaseline(window=0)
    except ValueError:
        bad2 = True
    check("8) window<=0 拒绝", bad2)
    bad3 = False
    try:
        DynamicBaseline(conditions={"combine": "X", "rules": [{"type": "out_of_baseline"}]})
    except ValueError:
        bad3 = True
    check("8) 非法 combine 拒绝", bad3)

    print("=" * 60)
    print(f"dynamic_baseline.py 自测: {passed} 项通过")
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
