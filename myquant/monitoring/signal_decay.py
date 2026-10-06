# 文件名：myquant/monitoring/signal_decay.py
"""
signal_decay.py - 信号质量退化检测（C-4，回写#233）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》C-4 信号衰减监控项：
  信号质量退化检测——后收益滚动 IC、信号密度下降、连续无效信号占比，
  任一指标越限即分级预警（P1/P2），并输出衰减曲线供演示/看板。

数据模型（严格防未来）：
  一条"信号-已实现收益对" = (signal_ts, signal_value, realized_return, realized_at)
    - signal_ts      ：信号产生时刻（epoch 秒，已发生）。
    - signal_value   ：信号强度/方向打分（>0 看多/向上预期，<0 看空/向下预期）。
    - realized_return：信号时刻之后已实现的前向收益（只有在 realized_at 时刻才可知）。
    - realized_at    ：该前向收益"变成已知"的时刻（≈ signal_ts + 持有 horizon）。
  防未来红线：record_pair() 拒绝 signal_ts > now 或 realized_at > now 的对——
    本行只用"信号时刻已过、且前向收益已兑现"的对；未兑现收益绝不参与 IC/无效判定。

三项指标（全部只消费已兑现的对）：
  1. 后收益滚动 IC：trailing N 条已兑现对（N=ic_rolling_window）的
       Spearman 秩相关（signal_value vs realized_return）。样本<2 或秩方差为 0 → None。
  2. 信号密度下降：时间窗 (now-W, now] 内 signal_ts 落入的对条数 / W（条/秒），
       与绝对下限 density_min 比较；低于下限即密度下降。
  3. 连续无效信号占比：与 IC 同窗口的 trailing N 条对中"无效信号"占比。
       无效定义：信号方向与已实现收益不匹配——
         signal_value>0（看多）但 realized_return<=0，或
         signal_value<0（看空）但 realized_return>=0；
         signal_value==0 视为中性，不计入分子也不计入分母。

衰减判定与分级（与 P0-3 告警分级语义一致，P1 比 P2 更严重）：
  - P1（SeverityLevel.ERROR）：滚动 IC < ic_threshold（信号预测力显著衰减），
      或 无效信号占比 > invalid_signal_ratio_max（信号大面积反向）。
  - P2（SeverityLevel.WARNING）：信号密度 < density_min（出单量骤降）。
  阈值未配置（None=PENDING_CAL）→ 三项指标照常计算输出，但不触发任何告警、不编造数值。

阈值真源（R-04）：config/settings.yaml 的 monitoring.slo.signal_decay，
  经 myquant.core.settings.get_monitoring_config() 读取：
    - ic_rolling_window           ：滚动 IC 窗口（trailing 对数），PENDING_CAL。
    - ic_threshold                ：滚动 IC 衰减下限，PENDING_CAL。
    - density_min                 ：信号密度下限（条/秒），PENDING_CAL。
    - invalid_signal_ratio_max     ：无效信号占比上限，PENDING_CAL。
  构造注入优先级高于配置；窗口长度未配置时用模块常量兜底（纯算法窗，非业务阈值）。

设计原则（铁律）:
  1. 阈值唯一真源 R-04：业务阈值一律经 get_monitoring_config() 读取或构造注入，
     代码内不硬编码业务阈值；PENDING_CAL（None）即"不启用告警"，绝不编造。
  2. 严格防未来：只消费已发生信号时刻 + 该时刻后已兑现收益；record_pair 拒绝未来点。
  3. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  4. 指标经 get_metrics_collector().set_gauge 动态注册（不改 metrics_collector.py 本体）。
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.signal_decay")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04 或构造注入）
# ─────────────────────────────────────────────

#: 告警分类（category 固定值，契约要求 category=signal_decay）
CATEGORY_SIGNAL_DECAY: str = "signal_decay"

#: 告警来源（契约要求 source=monitoring.signal_decay）
ALERT_SOURCE: str = "monitoring.signal_decay"

#: 未来点判定容差（秒）：signal_ts/realized_at 超过 now 的绝对上限
_FUTURE_EPS: float = 1.0

#: 滚动 IC 窗口兜底（条）：ic_rolling_window 未配置（PENDING_CAL）时的算法窗长度。
#: 纯统计窗，非业务告警阈值；业务告警仍以 ic_threshold 是否配置为准。
DEFAULT_IC_ROLLING_WINDOW: int = 20

#: 信号密度评估时间窗（秒）：密度 = (now-W, now] 内信号数 / W。纯技术窗。
DEFAULT_DENSITY_WINDOW_SECONDS: float = 3600.0

#: 历史曲线保留条数（衰减曲线输出；超出丢最旧，有界内存）
MAX_CURRENT_POINTS: int = 500
MAX_CURVE_POINTS: int = 200

#: 告警去重键前缀（固定窗口内不刷屏）
DEDUP_IC_PREFIX: str = "signal_decay:ic:"
DEDUP_DENSITY_PREFIX: str = "signal_decay:density:"
DEDUP_INVALID_PREFIX: str = "signal_decay:invalid_ratio:"

#: 默认策略/分组名（未指定 group 时归组）
DEFAULT_GROUP: str = "default"

#: 指标名（set_gauge 进 collector，labels={"group": 分组名}）
METRIC_SIGNAL_DECAY_IC: str = "signal_decay_rolling_ic"
METRIC_SIGNAL_DECAY_DENSITY: str = "signal_decay_density"
METRIC_SIGNAL_DECAY_INVALID_RATIO: str = "signal_decay_invalid_ratio"
METRIC_SIGNAL_DECAY_PAIRS: str = "signal_decay_completed_pairs"

#: 本模块注册的全部指标名（供主控/驾驶舱查询）
REGISTERED_METRICS: List[str] = [
    METRIC_SIGNAL_DECAY_IC,
    METRIC_SIGNAL_DECAY_DENSITY,
    METRIC_SIGNAL_DECAY_INVALID_RATIO,
    METRIC_SIGNAL_DECAY_PAIRS,
]


class SignalDecayMonitor:
    """
    信号质量退化检测器（后收益滚动 IC + 信号密度 + 无效信号占比 → P1/P2 预警）。

    生命周期：
      - record_pair() 逐笔喂"已兑现"的信号-收益对（防未来：拒绝未来 signal_ts/realized_at）。
      - evaluate() 计算三项指标、复核分级告警、写 gauge，并把快照追加进衰减曲线序列。
      - summary() / to_dict() 返回最新状态 + 完整衰减曲线（可 JSON 序列化）。

    告警分级（阈值 R-04，读 slo.signal_decay，PENDING_CAL=None 不告警）:
      - 滚动 IC < ic_threshold            → P1/ERROR（category=signal_decay）。
      - 无效信号占比 > invalid_signal_ratio_max → P1/ERROR。
      - 信号密度 < density_min            → P2/WARNING。
    """

    def __init__(
        self,
        ic_rolling_window: Optional[int] = None,
        ic_threshold: Optional[float] = None,
        density_min: Optional[float] = None,
        invalid_signal_ratio_max: Optional[float] = None,
        density_window_seconds: Optional[float] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        ic_rolling_window : int | None
            滚动 IC 窗口（trailing 已兑现对数）。None 读 slo.signal_decay.ic_rolling_window；
            仍为 None 时用 DEFAULT_IC_ROLLING_WINDOW（纯算法窗，非业务阈值）。
        ic_threshold : float | None
            滚动 IC 衰减下限（低于即 P1）。None 读 slo.signal_decay.ic_threshold；
            None（PENDING_CAL）→ 不触发 IC 告警，不编造数值。
        density_min : float | None
            信号密度下限（条/秒，低于即 P2）。None 读 slo.signal_decay.density_min；
            None → 不触发密度告警。
        invalid_signal_ratio_max : float | None
            无效信号占比上限（超过即 P1）。None 读
            slo.signal_decay.invalid_signal_ratio_max；None → 不触发无效占比告警。
        density_window_seconds : float | None
            密度评估时间窗（秒）。None 用 DEFAULT_DENSITY_WINDOW_SECONDS。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。

        Raises
        ------
        ValueError : 阈值非法（窗口<=0、密度窗<=0、无效占比不在 [0,1]、阈值符号非法）时抛出。
        """
        slo = get_monitoring_config().get("slo", {}).get("signal_decay", {})

        # ── 滚动 IC 窗口（算法窗；构造注入 > 配置 > 模块常量兜底）──
        if ic_rolling_window is not None:
            self._ic_window: int = int(ic_rolling_window)
        elif slo.get("ic_rolling_window") is not None:
            self._ic_window = int(float(slo.get("ic_rolling_window")))
        else:
            self._ic_window = DEFAULT_IC_ROLLING_WINDOW
        if self._ic_window <= 1:
            raise ValueError(f"ic_rolling_window 必须 >= 2，实际 {self._ic_window!r}")

        # ── 业务阈值（R-04；构造注入优先，回退配置；None=PENDING_CAL=不告警）──
        self._ic_threshold: Optional[float] = self._opt_float(
            ic_threshold if ic_threshold is not None else slo.get("ic_threshold"),
            "ic_threshold",
            allow_negative=True,  # IC 可正可负，阈值允许为负
        )
        self._density_min: Optional[float] = self._opt_float(
            density_min if density_min is not None else slo.get("density_min"),
            "density_min",
        )
        raw_max = invalid_signal_ratio_max if invalid_signal_ratio_max is not None \
            else slo.get("invalid_signal_ratio_max")
        self._invalid_ratio_max: Optional[float] = self._opt_float(
            raw_max, "invalid_signal_ratio_max", zero_one=True
        )

        # 密度评估窗（技术窗，非业务阈值）
        if density_window_seconds is not None:
            self._density_window: float = float(density_window_seconds)
        else:
            self._density_window = DEFAULT_DENSITY_WINDOW_SECONDS
        if self._density_window <= 0:
            raise ValueError(
                f"density_window_seconds 必须 > 0，实际 {self._density_window!r}"
            )

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector

        # 已兑现信号-收益对：[(signal_ts, signal_value, realized_return, realized_at), ...]
        # 按 realized_at 升序（兑现时刻）；只含已发生、已兑现的对。
        self._pairs: List[Tuple[float, float, float, float]] = []

        # 衰减曲线序列（每次 evaluate() 追加一帧；有界）
        self._ic_curve: Deque[Dict[str, Any]] = deque(maxlen=MAX_CURVE_POINTS)
        self._density_curve: Deque[Dict[str, Any]] = deque(maxlen=MAX_CURVE_POINTS)
        self._invalid_curve: Deque[Dict[str, Any]] = deque(maxlen=MAX_CURVE_POINTS)

        # 可观测状态（evaluate() 写入）
        self._last_eval_time: Optional[float] = None
        self._alert_triggered: bool = False
        self._last_state: Dict[str, Any] = {}

        logger.debug(
            "SignalDecayMonitor 初始化: ic_window=%d ic_threshold=%s density_min=%s "
            "invalid_ratio_max=%s density_window=%.0fs",
            self._ic_window, self._ic_threshold, self._density_min,
            self._invalid_ratio_max, self._density_window,
        )

    # ── 防御式数值解析 ─────────────────────────────────────────────
    @staticmethod
    def _opt_float(
        value: Any,
        name: str,
        zero_one: bool = False,
        allow_negative: bool = False,
    ) -> Optional[float]:
        """
        把配置/注入值解析为可选 float；None / YAML 的 ~ 视为"未配置=不启用告警"。

        Parameters
        ----------
        value : Any
            原始配置值（可能为 None / 数字 / 字符串）。
        name : str
            参数名（日志/异常用）。
        zero_one : bool
            True 时要求取值在 [0,1]（占比类阈值）。
        allow_negative : bool
            True 时允许负值（IC 阈值可为负）。

        Returns
        -------
        float | None : 解析后的阈值；未配置返回 None。

        Raises
        ------
        ValueError : 阈值超出合法区间时抛出（拒绝脏配置运行）。
        """
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            logger.warning("signal_decay.%s 非法值 %r，按未配置（None=不告警）处理", name, value)
            return None
        if math.isnan(f) or math.isinf(f):
            logger.warning("signal_decay.%s 非有限值 %r，按未配置（None=不告警）处理", name, value)
            return None
        if zero_one and not (0.0 <= f <= 1.0):
            raise ValueError(f"signal_decay.{name} 必须在 [0,1]，实际 {f!r}")
        if not allow_negative and f < 0:
            raise ValueError(f"signal_decay.{name} 不能为负，实际 {f!r}")
        return f

    # ── 信号-收益对摄入（严格防未来）─────────────────────────────────
    def record_pair(
        self,
        signal_ts: float,
        signal_value: float,
        realized_return: float,
        realized_at: Optional[float] = None,
    ) -> None:
        """
        摄入一条"已兑现"的信号-已实现收益对。

        防未来：signal_ts（信号时刻）与 realized_at（前向收益兑现时刻）都必须 <= now
        （容差 1s）；任一为未来即拒绝抛 ValueError。realized_at 缺省视为 signal_ts
        （收益在信号时刻即已知，适用于同步已兑现场景）。

        Parameters
        ----------
        signal_ts : float
            信号产生时刻（epoch 秒，必须 <= now）。
        signal_value : float
            信号强度/方向打分（>0 看多，<0 看空，0 中性）。
        realized_return : float
            信号时刻之后已实现的前向收益（必须在 realized_at 时刻才可知）。
        realized_at : float | None
            前向收益兑现时刻（epoch 秒）；None 用 signal_ts。必须 <= now。

        Raises
        ------
        ValueError : 时间戳/数值非法、为未来点、非有限数时抛出。
        """
        try:
            sts = float(signal_ts)
            sval = float(signal_value)
            rret = float(realized_return)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"signal_ts/signal_value/realized_return 必须可转 float，"
                f"收到 sts={signal_ts!r} sval={signal_value!r} rret={realized_return!r}"
            ) from e
        if not (math.isfinite(sts) and math.isfinite(sval) and math.isfinite(rret)):
            raise ValueError(
                f"信号-收益对必须为有限数，收到 sts={sts!r} sval={sval!r} rret={rret!r}"
            )
        rat = float(realized_at) if realized_at is not None else sts
        if not math.isfinite(rat):
            raise ValueError(f"realized_at 必须为有限数，收到 {realized_at!r}")

        now = self._clock()
        # 防未来红线：信号尚未发生 / 收益尚未兑现 一律拒绝
        if sts > now + _FUTURE_EPS:
            raise ValueError(
                f"拒绝未来信号：signal_ts={sts:.3f} 晚于当前 {now:.3f}（防未来红线）"
            )
        if rat > now + _FUTURE_EPS:
            raise ValueError(
                f"拒绝未兑现收益：realized_at={rat:.3f} 晚于当前 {now:.3f}"
                f"（只消费信号时刻后已实现的收益，防未来）"
            )

        self._pairs.append((sts, sval, rret, rat))
        self._pairs.sort(key=lambda p: p[3])  # 按兑现时刻升序
        # 有界缓存：超出丢最旧（已兑现最早的对）
        if len(self._pairs) > MAX_CURRENT_POINTS:
            del self._pairs[: len(self._pairs) - MAX_CURRENT_POINTS]

    # ── Spearman 秩相关（手写，免 scipy 依赖；平均秩处理并列）─────────
    @staticmethod
    def _rankdata(values: List[float]) -> List[float]:
        """
        1-based 平均秩（并列取平均秩）。

        Parameters
        ----------
        values : list[float]
            原始数值序列。

        Returns
        -------
        list[float] : 与 values 等长的秩序列。
        """
        n = len(values)
        order = sorted(range(n), key=lambda i: values[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0  # 1-based 平均秩
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    @classmethod
    def _spearman(cls, xs: List[float], ys: List[float]) -> Optional[float]:
        """
        两组序列的 Spearman 秩相关系数。

        Parameters
        ----------
        xs : list[float]
            信号强度序列。
        ys : list[float]
            已实现前向收益序列。

        Returns
        -------
        float | None : 秩相关系数 ∈ [-1, 1]；样本<2 或任一秩方差为 0（常数序列）返回 None。
        """
        n = len(xs)
        if n < 2 or len(ys) != n:
            return None
        rx = cls._rankdata(xs)
        ry = cls._rankdata(ys)
        mx = sum(rx) / n
        my = sum(ry) / n
        sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        sxx = sum((a - mx) ** 2 for a in rx)
        syy = sum((b - my) ** 2 for b in ry)
        if sxx <= 0.0 or syy <= 0.0:
            return None  # 任一序列无区分度（秩方差为 0），IC 不可定义
        return sxy / math.sqrt(sxx * syy)

    @classmethod
    def _invalid_ratio(cls, window: List[Tuple[float, float, float, float]]) -> Optional[float]:
        """
        计算窗口内无效信号占比。

        无效定义：信号方向与已实现收益不匹配（看多却跌 / 看空却涨）；
        signal_value==0 的中性信号不计入分子也不计入分母。

        Parameters
        ----------
        window : list[tuple]
            trailing 窗口内的已兑现对 (signal_ts, signal_value, realized_return, realized_at)。

        Returns
        -------
        float | None : 无效占比 ∈ [0,1]；窗口内无方向性信号（全中性）返回 None。
        """
        invalid = 0
        total = 0
        for _, sval, rret, _ in window:
            if sval > 0:                      # 看多 → 收益应 > 0
                total += 1
                if rret <= 0.0:
                    invalid += 1
            elif sval < 0:                    # 看空 → 收益应 < 0
                total += 1
                if rret >= 0.0:
                    invalid += 1
            # sval == 0：中性，不计入
        if total == 0:
            return None
        return invalid / total

    # ── 周期评估入口（POLL）────────────────────────────────────────
    def evaluate(self) -> Dict[str, Any]:
        """
        周期评估：计算三项指标、复核分级告警、写 gauge，并追加衰减曲线帧。

        Returns
        -------
        dict : 最新状态快照（同 summary/to_dict），含 ic/density/invalid_ratio 现值、
            阈值、告警标记与完整衰减曲线（可 JSON 序列化）。
        """
        now = self._clock()
        self._last_eval_time = now
        self._alert_triggered = False

        # trailing N 条已兑现对（按兑现时刻升序取最后 N 条）
        win = self._pairs[-self._ic_window:] if self._pairs else []
        ic: Optional[float] = None
        invalid_ratio: Optional[float] = None
        if len(win) >= 2:
            ic = self._spearman([p[1] for p in win], [p[2] for p in win])
        invalid_ratio = self._invalid_ratio(win)

        # 信号密度：(now-W, now] 内 signal_ts 落入的对条数 / W
        cutoff = now - self._density_window
        density_count = sum(1 for p in self._pairs if cutoff < p[0] <= now)
        density: float = density_count / self._density_window

        # ── 告警复核（阈值 None=PENDING_CAL → 不告警、不编造）──
        ic_alerted = self._evaluate_ic_alert(ic)
        density_alerted = self._evaluate_density_alert(density, density_count)
        invalid_alerted = self._evaluate_invalid_alert(invalid_ratio)
        if ic_alerted or density_alerted or invalid_alerted:
            self._alert_triggered = True

        # ── 衰减曲线追加（有界；None 帧也记录，展示 IC 不可定义时刻）──
        self._ic_curve.append({"t": round(now, 3), "ic": round(ic, 6) if ic is not None else None})
        self._density_curve.append({
            "t": round(now, 3),
            "density": round(density, 8),
            "count": density_count,
        })
        self._invalid_curve.append({
            "t": round(now, 3),
            "invalid_ratio": round(invalid_ratio, 6) if invalid_ratio is not None else None,
        })

        # ── 指标落盘（进 /api/metrics；失败仅 warning 不阻断）──
        self._publish_gauges(ic, density, invalid_ratio)

        self._last_state = {
            "ic": round(ic, 6) if ic is not None else None,
            "ic_threshold": self._ic_threshold,
            "ic_window": self._ic_window,
            "density": round(density, 8),
            "density_min": self._density_min,
            "density_window_seconds": self._density_window,
            "density_count": density_count,
            "invalid_ratio": round(invalid_ratio, 6) if invalid_ratio is not None else None,
            "invalid_signal_ratio_max": self._invalid_ratio_max,
            "completed_pairs": len(self._pairs),
            "alerts": {
                "ic_p1": ic_alerted,
                "density_p2": density_alerted,
                "invalid_p1": invalid_alerted,
            },
            "alert_triggered": self._alert_triggered,
            "last_eval_time": now,
            "curve": {
                "ic": list(self._ic_curve),
                "density": list(self._density_curve),
                "invalid_ratio": list(self._invalid_curve),
            },
            "thresholds": {
                "ic_threshold": self._ic_threshold,
                "density_min": self._density_min,
                "invalid_signal_ratio_max": self._invalid_ratio_max,
            },
        }
        return self._last_state

    # ── 告警判定（阈值 None=PENDING_CAL → 不告警）───────────────────
    def _evaluate_ic_alert(self, ic: Optional[float]) -> bool:
        """滚动 IC < ic_threshold → P1/ERROR。"""
        if self._ic_threshold is None or ic is None:
            return False
        if ic < self._ic_threshold:
            return self._fire_alert(
                level=SeverityLevel.ERROR,
                dedup_key=f"{DEDUP_IC_PREFIX}{DEFAULT_GROUP}",
                message=(
                    f"[signal_decay] 信号后收益滚动 IC={ic:.4f} "
                    f"< 阈值 {self._ic_threshold:.4f}（信号预测力显著衰减）"
                ),
                details={
                    "ic": round(ic, 6),
                    "ic_threshold": self._ic_threshold,
                    "ic_window": self._ic_window,
                },
            )
        return False

    def _evaluate_density_alert(self, density: float, count: int) -> bool:
        """信号密度 < density_min → P2/WARNING。"""
        if self._density_min is None:
            return False
        if density < self._density_min:
            return self._fire_alert(
                level=SeverityLevel.WARNING,
                dedup_key=f"{DEDUP_DENSITY_PREFIX}{DEFAULT_GROUP}",
                message=(
                    f"[signal_decay] 信号密度 {density:.6f} 条/秒 "
                    f"< 下限 {self._density_min:.6f}（窗口内 {count} 条 / "
                    f"{self._density_window:.0f}s，出单量骤降）"
                ),
                details={
                    "density": round(density, 8),
                    "density_min": self._density_min,
                    "density_count": count,
                    "density_window_seconds": self._density_window,
                },
            )
        return False

    def _evaluate_invalid_alert(self, invalid_ratio: Optional[float]) -> bool:
        """无效信号占比 > invalid_signal_ratio_max → P1/ERROR。"""
        if self._invalid_ratio_max is None or invalid_ratio is None:
            return False
        if invalid_ratio > self._invalid_ratio_max:
            return self._fire_alert(
                level=SeverityLevel.ERROR,
                dedup_key=f"{DEDUP_INVALID_PREFIX}{DEFAULT_GROUP}",
                message=(
                    f"[signal_decay] 无效信号占比 {invalid_ratio:.2%} "
                    f"> 上限 {self._invalid_ratio_max:.2%}（信号方向与已实现收益大面积背离）"
                ),
                details={
                    "invalid_ratio": round(invalid_ratio, 6),
                    "invalid_signal_ratio_max": self._invalid_ratio_max,
                    "ic_window": self._ic_window,
                },
            )
        return False

    def _fire_alert(
        self,
        level: SeverityLevel,
        dedup_key: str,
        message: str,
        details: Dict[str, Any],
    ) -> bool:
        """
        发一条告警（category=signal_decay, source=monitoring.signal_decay）；失败记 warning 不阻断。

        Returns
        -------
        bool : 是否成功发出（告警管理器可用且不抛）。
        """
        try:
            self._alert_mgr().alert(
                level=level,
                message=message,
                source=ALERT_SOURCE,
                category=CATEGORY_SIGNAL_DECAY,
                details=details,
                deduplication_key=dedup_key,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("信号衰减告警发送失败（不阻断监控）: %s", e)
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
        ic: Optional[float],
        density: float,
        invalid_ratio: Optional[float],
    ) -> None:
        """把最新三项指标写入 set_gauge（labels={"group": default}）。采集器不可用仅记 warning。"""
        labels: Dict[str, str] = {"group": DEFAULT_GROUP}
        self._set_gauge(METRIC_SIGNAL_DECAY_IC, ic, labels)
        self._set_gauge(METRIC_SIGNAL_DECAY_DENSITY, density, labels)
        self._set_gauge(METRIC_SIGNAL_DECAY_INVALID_RATIO, invalid_ratio, labels)
        self._set_gauge(METRIC_SIGNAL_DECAY_PAIRS, float(len(self._pairs)), labels)

    def _set_gauge(self, name: str, value: Optional[float], labels: Dict[str, str]) -> None:
        """set_gauge 一个仪表盘指标；None（样本不足）落 0.0 占位。"""
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

    # ── 状态可观测（供驾驶舱/验收消费；可 JSON 序列化）─────────────────
    def to_dict(self) -> Dict[str, Any]:
        """返回最新评估快照（含三项指标现值、阈值、告警标记与衰减曲线）。"""
        return dict(self._last_state)

    def summary(self) -> Dict[str, Any]:
        """
        返回监控结构化快照（驾驶舱/报告消费）。等同 evaluate() 的最新结果；
        未评估时返回含空曲线的初始结构。
        """
        if not self._last_state:
            return {
                "ic": None,
                "ic_threshold": self._ic_threshold,
                "density": None,
                "density_min": self._density_min,
                "invalid_ratio": None,
                "invalid_signal_ratio_max": self._invalid_ratio_max,
                "alert_triggered": False,
                "curve": {
                    "ic": [],
                    "density": [],
                    "invalid_ratio": [],
                },
                "thresholds": {
                    "ic_threshold": self._ic_threshold,
                    "density_min": self._density_min,
                    "invalid_signal_ratio_max": self._invalid_ratio_max,
                },
            }
        return dict(self._last_state)


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：人为制造"正常段→衰减段"序列，验证三指标、分级告警与衰减曲线。"""
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
            self.gauges[f"{name}:{(labels or {}).get('group', '?')}"] = float(value)

    # 固定时钟：now 推进到序列末尾
    base = 1_700_000_000.0
    clock_now = [base + 5_000.0]

    def clock() -> float:
        return clock_now[0]

    am = _FakeAlert()
    met = _FakeMetrics()
    # 显式注入阈值（演示/测试用，绕开 PENDING_CAL）：
    #   IC<0.15 → P1；无效占比>0.4 → P1；密度<0.01 条/秒 → P2
    mon = SignalDecayMonitor(
        ic_rolling_window=20,
        ic_threshold=0.15,
        density_min=0.01,
        invalid_signal_ratio_max=0.40,
        density_window_seconds=1_000.0,
        alert_manager=am,
        metrics_collector=met,
        clock=clock,
    )

    # ── 正常段：20 条，signal_value 与 realized_return 强正相关（高 IC、低无效）──
    # 信号每 20s 一条，20 条跨 380s；收益 = 0.5*signal_value + 小噪声（方向一致）。
    for i in range(20):
        s = float(i) - 9.5          # -9.5 .. 9.5，线性
        r = 0.5 * s + 0.2           # 与 s 同向
        ts = base + i * 20.0
        mon.record_pair(ts, s, r, realized_at=ts + 1.0)
    clock_now[0] = base + 400.0
    st_healthy = mon.evaluate()
    check("1) 正常段 IC 显著为正", st_healthy["ic"] is not None and st_healthy["ic"] > 0.5,
          f"ic={st_healthy['ic']}")
    check("1) 正常段无效占比≈0", st_healthy["invalid_ratio"] is not None
          and st_healthy["invalid_ratio"] <= 0.1, f"inv={st_healthy['invalid_ratio']}")
    # 正常段密度：窗 1000s 内 20 条 → 0.02 条/秒 > 0.01 → 不告警
    check("1) 正常段不触发任何告警", am.calls == [] and st_healthy["alert_triggered"] is False,
          f"calls={[c['deduplication_key'] for c in am.calls]}")

    # ── 衰减段：再喂 20 条，signal_value 与 realized_return 反向（IC 转负、无效占比高）──
    # 且信号更稀疏（每 100s 一条），拉低密度。先推进时钟到序列末尾（防未来：喂数时 now 须 >= 各 ts）。
    clock_now[0] = base + 2_500.0
    for i in range(20):
        s = float(i) - 9.5
        r = -0.5 * s - 0.2          # 方向反了
        ts = base + 500.0 + i * 100.0
        mon.record_pair(ts, s, r, realized_at=ts + 1.0)
    st_decay = mon.evaluate()
    check("2) 衰减段 IC 转负/低于阈值", st_decay["ic"] is not None
          and st_decay["ic"] < 0.15, f"ic={st_decay['ic']}")
    check("2) 衰减段无效占比高", st_decay["invalid_ratio"] is not None
          and st_decay["invalid_ratio"] > 0.4, f"inv={st_decay['invalid_ratio']}")
    # 密度：窗 (now-1000, now] = (base+1500, base+2500]。衰减段 ts=base+500+i*100：
    #   i=11..19（base+1600..base+2400）在窗内共 9 条 → density=0.009 < 0.01 → P2 也会触发；
    #   本节断言只校验 P1（IC/无效占比），P2 触发不影响这些断言。
    p1_ic = [c for c in am.calls if c["deduplication_key"] == "signal_decay:ic:default"]
    p1_inv = [c for c in am.calls if c["deduplication_key"] == "signal_decay:invalid_ratio:default"]
    check("2) IC 衰减触发 P1/ERROR", len(p1_ic) == 1 and p1_ic[0]["level"].name == "ERROR",
          f"calls={[c['deduplication_key'] for c in am.calls]}")
    check("2) 无效占比超限触发 P1/ERROR", len(p1_inv) == 1
          and p1_inv[0]["level"] == p1_ic[0]["level"], f"calls={am.calls}")
    check("2) 告警 category=signal_decay", all(c["category"] == "signal_decay" for c in am.calls),
          f"{[c.get('category') for c in am.calls]}")
    check("2) 告警 source=monitoring.signal_decay",
          all(c["source"] == "monitoring.signal_decay" for c in am.calls), "")
    check("2) details 附指标值与阈值", p1_ic and "ic_threshold" in p1_ic[0]["details"]
          and "ic" in p1_ic[0]["details"], f"{p1_ic[0]['details'] if p1_ic else None}")

    # ── 衰减曲线：含 IC/密度/无效占比三序列，可 JSON 序列化 ──
    import json
    curve = st_decay["curve"]
    check("3) 衰减曲线含三序列", set(curve.keys()) == {"ic", "density", "invalid_ratio"},
          f"{list(curve.keys())}")
    check("3) IC 曲线至少 2 帧", len(curve["ic"]) >= 2, f"{len(curve['ic'])}")
    try:
        json.dumps(st_decay)
        json_ok = True
    except (TypeError, ValueError):
        json_ok = False
    check("3) 状态可 JSON 序列化", json_ok, "")

    # ── P2 密度下降：把密度下限抬高，使当前密度跌破 → WARNING ──
    am2 = _FakeAlert()
    mon2 = SignalDecayMonitor(
        ic_rolling_window=20, ic_threshold=None, invalid_signal_ratio_max=None,
        density_min=0.02, density_window_seconds=1000.0,
        alert_manager=am2, metrics_collector=_FakeMetrics(), clock=clock,
    )
    for i in range(5):  # 5 条跨 400s，密度=5/1000=0.005 < 0.02
        ts = base + 100.0 + i * 100.0
        mon2.record_pair(ts, float(i), 0.1 * i, realized_at=ts + 1.0)
    st2 = mon2.evaluate()
    p2 = [c for c in am2.calls if c["deduplication_key"] == "signal_decay:density:default"]
    check("4) 密度低于下限触发 P2/WARNING", len(p2) == 1 and p2[0]["level"].name == "WARNING",
          f"calls={[c['deduplication_key'] for c in am2.calls]}")

    # ── None 配置（PENDING_CAL）→ 只计算不告警、不编造 ──
    am3 = _FakeAlert()
    mon3 = SignalDecayMonitor(
        ic_rolling_window=20, ic_threshold=None, density_min=None,
        invalid_signal_ratio_max=None, density_window_seconds=1000.0,
        alert_manager=am3, metrics_collector=_FakeMetrics(), clock=clock,
    )
    for i in range(20):  # 反向衰减序列
        s = float(i) - 9.5
        ts = base + i * 20.0
        mon3.record_pair(ts, s, -0.5 * s, realized_at=ts + 1.0)
    st3 = mon3.evaluate()
    check("5) None 配置不告警", am3.calls == [], f"calls={am3.calls}")
    check("5) 指标照常计算输出", st3["ic"] is not None and st3["invalid_ratio"] is not None,
          f"{st3['ic']} {st3['invalid_ratio']}")

    # ── 防未来：拒绝未来点 ──
    rejected_sig = False
    rejected_fut = False
    try:
        mon3.record_pair(clock_now[0] + 100.0, 1.0, 0.1, realized_at=clock_now[0] + 101.0)
    except ValueError:
        rejected_sig = True
    try:
        mon3.record_pair(base, 1.0, 0.1, realized_at=clock_now[0] + 100.0)  # 收益未兑现
    except ValueError:
        rejected_fut = True
    check("6) 拒绝未来信号时刻", rejected_sig, "")
    check("6) 拒绝未兑现收益（未来 realized_at）", rejected_fut, "")

    # ── 防御校验：非法入参 ──
    bad1 = False
    try:
        mon3.record_pair("nan" if False else 1.0, 1.0, float("nan"))
    except ValueError:
        bad1 = True
    check("7) 非有限收益拒绝", bad1, "")
    bad2 = False
    try:
        SignalDecayMonitor(invalid_signal_ratio_max=1.5)  # 越出 [0,1]
    except ValueError:
        bad2 = True
    check("7) 无效占比阈值越界拒绝", bad2, "")
    bad3 = False
    try:
        SignalDecayMonitor(ic_rolling_window=1)  # 窗口 <2
    except ValueError:
        bad3 = True
    check("7) IC 窗口<2 拒绝", bad3, "")

    # ── set_gauge 动态注册 ──
    check("8) IC/密度/无效占比/对数指标已写",
          f"{METRIC_SIGNAL_DECAY_IC}:default" in met.gauges
          and f"{METRIC_SIGNAL_DECAY_DENSITY}:default" in met.gauges
          and f"{METRIC_SIGNAL_DECAY_INVALID_RATIO}:default" in met.gauges
          and f"{METRIC_SIGNAL_DECAY_PAIRS}:default" in met.gauges,
          f"gauges={list(met.gauges)}")
    check("8) REGISTERED_METRICS 含 4 指标", len(REGISTERED_METRICS) == 4,
          f"{REGISTERED_METRICS}")

    print("=" * 60)
    print(f"signal_decay.py 自测: {passed} 项通过")
    if failures:
        for f in failures:
            print(f"  x {f}")
        print(f"失败 {len(failures)} 项")
        return 1
    # 衰减曲线摘要演示
    print("衰减曲线摘要（IC 帧）:")
    for fr in st_decay["curve"]["ic"]:
        print(f"   t={fr['t']}  ic={fr['ic']}")
    print("全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
