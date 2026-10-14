# 文件名：myquant/monitoring/signal_consistency.py
"""
signal_consistency.py - 回测实盘逐笔信号一致性监控（C-10，回写#239）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》C-10：
  同参数同时间窗内，回测信号序列与实盘信号序列逐笔对齐对比，统计偏差率并在
  偏差率超 SLO 阈值时发 WARNING 告警（附偏差明细）。

触发模式（R-02 / agent-hint C）：批任务（非 MTTD 类）
  - 模块提供 run_comparison(backtest_signals, live_signals) 供批任务在每个时间窗
    结束后调用：传入本窗内回测侧与实盘侧两条信号序列，本模块完成对齐→比对→
    偏差率统计→告警判定→指标落盘，返回结构化面板。不接 scheduler。

对齐算法（逐笔对齐，三键对齐 signal_id / 时间 / 标的）：
  1. 主对齐键 = signal_id：两侧 signal_id 交集 → 逐笔配对（align_by="signal_id"）。
  2. 模糊兜底 = (symbol, 时间邻近)：回测侧未被 signal_id 命中的信号，在实盘侧
     未使用信号中找 同 symbol 且 |Δts| <= time_tolerance 的最近一笔配对
     （align_by="time_symbol"）；用于 signal_id 因链路透传缺失时的容错对齐。
  3. 两侧都无法配对的信号记为 one-sided（回测有实盘无 / 实盘有回测无），
     计入明细供运维核查，但不进入"对齐样本总数"分母（见下口径）。

错配判定（已对齐配对 unit；任一命中即判为错配样本，容差可注入）：
  - direction_mismatch ：回测方向(side) != 实盘方向(side)（归一化小写后比较）；
  - price_diff_exceeds_tolerance ：|回测价 - 实盘价| > price_tolerance；
    （一侧缺价而另一侧有价，亦判为价格错配）；
  - time_drift_exceeds_tolerance ：|回测ts - 实盘ts| > time_tolerance。
    （signal_id 配对但两侧时间戳漂移超容差 = 时序不一致；模糊配对本身已在容差内，
     不会再触发本项。）

偏差率（窗口内）：
  - aligned        = 成功配对的对比 unit 数（signal_id + time_symbol）；
  - mismatched     = aligned 中命中任一错配判定的 unit 数；
  - mismatch_rate  = mismatched / aligned（aligned=0 时为 0.0，不除零、不编造）；
  - total          = aligned + one-sided 数（本窗对比单元总数，含单侧信号）；
  - 明细 details   = 全部错配 unit（含 one-sided），字段：
      signal_id / ts / symbol / backtest_side / live_side /
      backtest_price / live_price / deviation_types / align_by。

阈值真源（R-04/R-05，P0-2）：config/settings.yaml 的
  monitoring.slo.backtest_consistency.signal_deviation_rate_max
  monitoring.slo.backtest_consistency.deviation_window
  经 myquant.core.settings.get_monitoring_config() 读取：
    - signal_deviation_rate_max：偏差率上限（如 0.05=5%），PENDING_CAL（~=None）
      → 只计算输出偏差率与明细，不发告警、不编造数值；
    - deviation_window：偏差率计算窗口（秒），配置后只保留 ts >= now - window 的
      信号；PENDING_CAL（None）→ 不过滤窗口，按传入序列全量对比（批任务已自行圈窗）。
  演示/测试可经构造参数显式注入阈值触发告警（R-05：P0-1 策略风格=中低频因子，
  决策不预置数值）。

设计原则（铁律）:
  1. 阈值唯一真源 R-04：业务阈值经 get_monitoring_config().slo.backtest_consistency
     读取或构造注入，代码内不硬编码业务阈值；PENDING_CAL（None）项=不启用告警，
     绝不编造。
  2. 严格防未来：run_comparison 拒绝 ts > now(+eps) 的信号记录；窗口过滤只保留
     已发生点；本行使用的数据截至 now 时刻。
  3. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  4. 指标经 get_metrics_collector().set_gauge 动态注册（不改 metrics_collector.py）。

依赖: math, time, typing, myquant.core.{constants,logger,settings}
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.signal_consistency")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04 或构造注入）
# ─────────────────────────────────────────────
#: 告警分类（决定默认 Runbook 兜底，A-10）
CATEGORY_SIGNAL_CONSISTENCY: str = "signal_consistency"

#: 告警来源标识（与 C-2/C-4 等模块保持 source=monitoring.<module> 约定）
SOURCE_MODULE: str = "monitoring.signal_consistency"

#: 偏差率告警去重键（固定窗口内不刷屏）
DEDUP_DEVIATION_RATE: str = "signal_consistency:deviation_rate"

#: 方向 token 归一化映射（buy/b→buy，sell/s→sell；其余原样小写保留）
_SIDE_MAP: Dict[str, str] = {
    "buy": "buy", "b": "buy", "long": "buy", "1": "buy",
    "sell": "sell", "s": "sell", "short": "sell", "-1": "sell",
}

#: 错配类型枚举（明细 deviation_types 取值）
DEV_DIRECTION: str = "direction_mismatch"
DEV_PRICE: str = "price_diff_exceeds_tolerance"
DEV_TIME: str = "time_drift_exceeds_tolerance"
DEV_MISSING_LIVE: str = "missing_in_live"
DEV_MISSING_BACKTEST: str = "missing_in_backtest"

#: 告警 details 内附偏差明细条数上限（避免 details 体积膨胀）
MAX_DETAILS_IN_ALERT: int = 20

#: 未来点判定容差（秒）：timestamp 超过 now 的绝对上限
_FUTURE_EPS: float = 1.0

#: 缺省比对容差（非业务阈值；可注入）。缺省 0.0 = 价格/时间须精确一致，
#   真实部署经构造参数按标的波动注入合理容差。
DEFAULT_PRICE_TOLERANCE: float = 0.0
DEFAULT_TIME_TOLERANCE: float = 0.0

#: 指标名（set_gauge 进 collector，自动建指标）
METRIC_TOTAL: str = "signal_consistency_total"
METRIC_ALIGNED: str = "signal_consistency_aligned"
METRIC_MISMATCHED: str = "signal_consistency_mismatched"
METRIC_MISMATCH_RATE: str = "signal_consistency_mismatch_rate"

#: 本模块注册的全部指标名（供主控/驾驶舱查询）
REGISTERED_METRICS: List[str] = [
    METRIC_TOTAL,
    METRIC_ALIGNED,
    METRIC_MISMATCHED,
    METRIC_MISMATCH_RATE,
]


class SignalConsistencyChecker:
    """
    回测实盘逐笔信号一致性校验器（批任务调用）。

    用法:
        from myquant.monitoring.signal_consistency import SignalConsistencyChecker

        checker = SignalConsistencyChecker()   # 阈值读 slo.backtest_consistency（PENDING_CAL）
        panel = checker.run_comparison(backtest_signals, live_signals)
        # panel: {total, aligned, mismatched, mismatch_rate, details, ...}
    """

    def __init__(
        self,
        signal_deviation_rate_max: Optional[float] = None,
        deviation_window: Optional[float] = None,
        price_tolerance: Optional[float] = None,
        time_tolerance: Optional[float] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        signal_deviation_rate_max : float | None
            偏差率告警上限（0~1，如 0.05=5%）。None 读
            slo.backtest_consistency.signal_deviation_rate_max；仍为 None
            （PENDING_CAL 未配置）→ 不发告警，不编造数值。
        deviation_window : float | None
            偏差率计算窗口（秒）。None 读 slo.backtest_consistency.deviation_window；
            仍为 None → 不过滤窗口，按传入序列全量对比。
        price_tolerance : float | None
            价格比对容差（价格单位，>=0）。None 用 DEFAULT_PRICE_TOLERANCE。
        time_tolerance : float | None
            时间比对/模糊对齐容差（秒，>=0）。None 用 DEFAULT_TIME_TOLERANCE。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。

        Raises
        ------
        ValueError : 阈值越界（rate 不在 [0,1]、window<=0、容差<0）时抛出。
        """
        slo = get_monitoring_config().get("slo", {}).get("backtest_consistency", {})

        # ── 阈值（R-04/R-05；构造注入优先，回退配置；None=PENDING_CAL 未配置）──
        self._rate_max: Optional[float] = self._parse_optional_rate(
            signal_deviation_rate_max
            if signal_deviation_rate_max is not None
            else slo.get("signal_deviation_rate_max"),
        )
        self._deviation_window: Optional[float] = self._parse_optional_window(
            deviation_window if deviation_window is not None else slo.get("deviation_window"),
        )

        # ── 比对容差（非业务阈值，可注入；缺省精确一致）──
        self._price_tolerance: float = self._as_nonneg_float(
            price_tolerance if price_tolerance is not None else DEFAULT_PRICE_TOLERANCE,
            "price_tolerance",
        )
        self._time_tolerance: float = self._as_nonneg_float(
            time_tolerance if time_tolerance is not None else DEFAULT_TIME_TOLERANCE,
            "time_tolerance",
        )

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector

        # 最近一次对比面板（run_comparison 写入）
        self._last_panel: Dict[str, Any] = {}
        self._alert_triggered: bool = False

        logger.debug(
            "SignalConsistencyChecker 初始化: rate_max=%s deviation_window=%s "
            "price_tol=%s time_tol=%s",
            self._rate_max, self._deviation_window,
            self._price_tolerance, self._time_tolerance,
        )

    # ── 防御式数值解析 ─────────────────────────────────────────────
    @staticmethod
    def _parse_optional_rate(value: Any) -> Optional[float]:
        """
        解析偏差率上限（0~1）；None / YAML ~ 视为未配置=不启用告警。

        Raises
        ------
        ValueError : 解析后不在 [0,1] 时抛出（拒绝脏配置运行）。
        """
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            logger.warning("signal_deviation_rate_max 非法值 %r，按未配置（None）处理", value)
            return None
        if not (0.0 <= f <= 1.0):
            raise ValueError(f"signal_deviation_rate_max 必须在 [0,1]，实际 {f!r}")
        return f

    @staticmethod
    def _parse_optional_window(value: Any) -> Optional[float]:
        """解析偏差率窗口（秒）；None/YAML ~ 视为未配置=不过滤窗口。"""
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            logger.warning("deviation_window 非法值 %r，按未配置（None）处理", value)
            return None
        if f <= 0:
            raise ValueError(f"deviation_window 必须 > 0，实际 {f!r}")
        return f

    @staticmethod
    def _as_nonneg_float(value: Any, name: str) -> float:
        """把比对容差解析为非负 float；非法/负值抛 ValueError。"""
        try:
            f = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{name} 必须为数字，收到 {value!r}") from e
        if math.isnan(f) or math.isinf(f) or f < 0:
            raise ValueError(f"{name} 必须为非负有限数，收到 {f!r}")
        return f

    # ── 信号归一化（防御式取字段；缺字段按 None/空串）────────────────
    @staticmethod
    def _normalize_signal(raw: Any, side_tag: str) -> Dict[str, Any]:
        """
        把一条信号记录（dict 或对象）归一化为内部结构。

        Parameters
        ----------
        raw : dict | object
            信号记录。dict 时取 signal_id/timestamp(或 ts)/symbol/side(或 direction)/
            price(或 signal_price)；对象时按同名属性取。
        side_tag : str
            "backtest" / "live"（异常信息定位用）。

        Returns
        -------
        dict : {signal_id: str, ts: float, symbol: str, side: str, price: float|None}

        Raises
        ------
        ValueError : 缺 symbol 或时间戳非法/非有限时抛出。
        """
        if isinstance(raw, dict):
            sid = raw.get("signal_id", raw.get("id", ""))
            ts = raw.get("timestamp", raw.get("ts", raw.get("event_ts")))
            symbol = raw.get("symbol", raw.get("code", ""))
            side = raw.get("side", raw.get("direction", ""))
            price = raw.get("price", raw.get("signal_price", raw.get("ref_price")))
        else:
            sid = getattr(raw, "signal_id", getattr(raw, "id", ""))
            ts = getattr(raw, "timestamp", getattr(raw, "ts", getattr(raw, "event_ts", None)))
            symbol = getattr(raw, "symbol", getattr(raw, "code", ""))
            side = getattr(raw, "side", getattr(raw, "direction", ""))
            price = getattr(raw, "price", getattr(raw, "signal_price",
                                                getattr(raw, "ref_price", None)))

        sid = "" if sid is None else str(sid)
        symbol = "" if symbol is None else str(symbol).strip()
        if not symbol:
            raise ValueError(f"[{side_tag}] 信号缺 symbol，记录={raw!r}")

        try:
            ts_f = float(ts)
        except (TypeError, ValueError) as e:
            raise ValueError(f"[{side_tag}] 信号时间戳非法：{ts!r}") from e
        if not math.isfinite(ts_f):
            raise ValueError(f"[{side_tag}] 信号时间戳非有限数：{ts_f!r}")

        # 方向归一化（buy/b/long/1→buy；sell/s/short/-1→sell；其余原样小写）
        side_s = "" if side is None else str(side).strip().lower()
        side_norm = _SIDE_MAP.get(side_s, side_s)

        price_f: Optional[float]
        if price is None:
            price_f = None
        else:
            try:
                price_f = float(price)
            except (TypeError, ValueError):
                price_f = None
            else:
                if not math.isfinite(price_f):
                    price_f = None

        return {
            "signal_id": sid,
            "ts": ts_f,
            "symbol": symbol,
            "side": side_norm,
            "price": price_f,
        }

    # ── 批任务主入口：逐笔对齐对比 + 偏差率 + 告警 ───────────────────
    def run_comparison(
        self,
        backtest_signals: List[Any],
        live_signals: List[Any],
    ) -> Dict[str, Any]:
        """
        在一个时间窗内逐笔对比回测侧与实盘侧信号序列。

        防未来：任一信号 ts > now(+_FUTURE_EPS) 即拒绝（抛 ValueError）；
        配置了 deviation_window 时只保留 ts >= now - window 的已发生点。

        Parameters
        ----------
        backtest_signals : list[dict|object]
            本窗内回测侧信号序列。
        live_signals : list[dict|object]
            本窗内实盘侧信号序列。

        Returns
        -------
        dict : 对比面板
            {
              "total": int, "aligned": int, "mismatched": int,
              "mismatch_rate": float, "details": list[dict],
              "unmatched_backtest": int, "unmatched_live": int,
              "rate_max": float|None, "deviation_window": float|None,
              "alert_triggered": bool, "now": float,
            }
        """
        now = self._clock()
        self._alert_triggered = False

        # ── 归一化 + 防未来 + 窗口过滤（本行数据截至 now）──
        bt: List[Dict[str, Any]] = []
        live: List[Dict[str, Any]] = []
        cutoff: Optional[float] = None
        if self._deviation_window is not None:
            cutoff = now - self._deviation_window

        for raw in backtest_signals or []:
            s = self._normalize_signal(raw, "backtest")
            if s["ts"] > now + _FUTURE_EPS:
                raise ValueError(
                    f"拒绝未来回测信号：ts={s['ts']:.3f} 晚于 now={now:.3f}（防未来红线）"
                )
            if cutoff is not None and s["ts"] < cutoff:
                continue  # 窗口外，丢弃
            bt.append(s)
        for raw in live_signals or []:
            s = self._normalize_signal(raw, "live")
            if s["ts"] > now + _FUTURE_EPS:
                raise ValueError(
                    f"拒绝未来实盘信号：ts={s['ts']:.3f} 晚于 now={now:.3f}（防未来红线）"
                )
            if cutoff is not None and s["ts"] < cutoff:
                continue
            live.append(s)

        # ── 对齐：signal_id 主对齐 + (symbol,时间邻近) 模糊兜底 ──
        units, unmatched_bt, unmatched_live = self._align(bt, live)

        # ── 逐 unit 错配判定 ──
        details: List[Dict[str, Any]] = []
        mismatched = 0
        for b, lv, align_by in units:
            devs: List[str] = []
            if b["side"] != lv["side"]:
                devs.append(DEV_DIRECTION)
            # 价格：两侧都有价才比绝对差；一侧有价一侧缺价也判价格错配
            if (b["price"] is None) != (lv["price"] is None):
                devs.append(DEV_PRICE)
            elif b["price"] is not None and lv["price"] is not None:
                if abs(b["price"] - lv["price"]) > self._price_tolerance:
                    devs.append(DEV_PRICE)
            # 时间漂移（signal_id 配对但两侧 ts 漂移超容差）
            if abs(b["ts"] - lv["ts"]) > self._time_tolerance:
                devs.append(DEV_TIME)

            if devs:
                mismatched += 1
                details.append(self._make_detail(
                    signal_id=b["signal_id"] or lv["signal_id"],
                    ts=b["ts"], symbol=b["symbol"],
                    bt_side=b["side"], live_side=lv["side"],
                    bt_price=b["price"], live_price=lv["price"],
                    dev_types=devs, align_by=align_by,
                ))

        # ── one-sided（单侧信号）入明细，供运维核查；不计入 aligned 分母 ──
        for b in unmatched_bt:
            details.append(self._make_detail(
                signal_id=b["signal_id"], ts=b["ts"], symbol=b["symbol"],
                bt_side=b["side"], live_side="", bt_price=b["price"], live_price=None,
                dev_types=[DEV_MISSING_LIVE], align_by="unmatched",
            ))
        for lv in unmatched_live:
            details.append(self._make_detail(
                signal_id=lv["signal_id"], ts=lv["ts"], symbol=lv["symbol"],
                bt_side="", live_side=lv["side"], bt_price=None, live_price=lv["price"],
                dev_types=[DEV_MISSING_BACKTEST], align_by="unmatched",
            ))

        aligned = len(units)
        total = aligned + len(unmatched_bt) + len(unmatched_live)
        mismatch_rate: float = (mismatched / aligned) if aligned > 0 else 0.0

        # ── 指标落盘（进 /api/metrics；采集器不可用仅记 warning 不阻断）──
        self._publish_gauges(total, aligned, mismatched, mismatch_rate)

        # ── 告警判定（阈值 None=PENDING_CAL → 不告警、不编造）──
        alert_fired = False
        if self._rate_max is not None and aligned > 0 and mismatch_rate > self._rate_max:
            alert_fired = self._fire_alert(
                total=total, aligned=aligned, mismatched=mismatched,
                mismatch_rate=mismatch_rate, details=details,
            )
            self._alert_triggered = alert_fired

        panel: Dict[str, Any] = {
            "total": total,
            "aligned": aligned,
            "mismatched": mismatched,
            "mismatch_rate": round(mismatch_rate, 6),
            "details": details,
            "unmatched_backtest": len(unmatched_bt),
            "unmatched_live": len(unmatched_live),
            "rate_max": self._rate_max,
            "deviation_window": self._deviation_window,
            "price_tolerance": self._price_tolerance,
            "time_tolerance": self._time_tolerance,
            "alert_triggered": self._alert_triggered,
            "now": now,
        }
        self._last_panel = panel
        logger.info(
            "signal_consistency 窗口对比: total=%d aligned=%d mismatched=%d rate=%.4f"
            "（上限=%s）alert=%s",
            total, aligned, mismatched, mismatch_rate, self._rate_max, alert_fired,
        )
        return panel

    # ── 对齐算法（signal_id 主对齐 + symbol/时间模糊兜底）─────────────
    def _align(
        self,
        bt: List[Dict[str, Any]],
        live: List[Dict[str, Any]],
    ) -> Tuple[
        List[Tuple[Dict[str, Any], Dict[str, Any], str]],
        List[Dict[str, Any]],
        List[Dict[str, Any]],
    ]:
        """
        逐笔对齐回测/实盘信号序列。

        Returns
        -------
        (units, unmatched_bt, unmatched_live) :
            units = [(bt_signal, live_signal, align_by), ...]；
            unmatched_bt = 回测侧未配对信号；unmatched_live = 实盘侧未配对信号。
        """
        units: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
        used_bt = [False] * len(bt)
        used_live = [False] * len(live)

        # 1) signal_id 主对齐
        live_id_idx: Dict[str, int] = {}
        for j, s in enumerate(live):
            if s["signal_id"]:
                live_id_idx.setdefault(s["signal_id"], j)
        for i, b in enumerate(bt):
            sid = b["signal_id"]
            if sid and sid in live_id_idx:
                j = live_id_idx[sid]
                if not used_live[j]:
                    units.append((b, live[j], "signal_id"))
                    used_bt[i] = True
                    used_live[j] = True

        # 2) 回测侧剩余 → (symbol, 时间邻近) 模糊兜底
        for i, b in enumerate(bt):
            if used_bt[i]:
                continue
            best_j = -1
            best_dt = math.inf
            for j, lv in enumerate(live):
                if used_live[j]:
                    continue
                if lv["symbol"] != b["symbol"]:
                    continue
                dt = abs(lv["ts"] - b["ts"])
                if dt <= self._time_tolerance and dt < best_dt:
                    best_dt = dt
                    best_j = j
            if best_j >= 0:
                units.append((b, live[best_j], "time_symbol"))
                used_bt[i] = True
                used_live[best_j] = True

        unmatched_bt = [b for i, b in enumerate(bt) if not used_bt[i]]
        unmatched_live = [lv for j, lv in enumerate(live) if not used_live[j]]
        return units, unmatched_bt, unmatched_live

    # ── 明细构造 ──────────────────────────────────────────────────
    @staticmethod
    def _make_detail(
        signal_id: str,
        ts: float,
        symbol: str,
        bt_side: str,
        live_side: str,
        bt_price: Optional[float],
        live_price: Optional[float],
        dev_types: List[str],
        align_by: str,
    ) -> Dict[str, Any]:
        """构造一条错配样本明细（JSON 可序列化，供告警 details 附带）。"""
        return {
            "signal_id": signal_id,
            "ts": round(ts, 3),
            "symbol": symbol,
            "backtest_side": bt_side,
            "live_side": live_side,
            "backtest_price": bt_price,
            "live_price": live_price,
            "deviation_types": list(dev_types),
            "align_by": align_by,
        }

    # ── 告警 ─────────────────────────────────────────────────────
    def _fire_alert(
        self,
        total: int,
        aligned: int,
        mismatched: int,
        mismatch_rate: float,
        details: List[Dict[str, Any]],
    ) -> bool:
        """
        偏差率超阈值 → WARNING（category=signal_consistency，dedup 防刷屏）。

        details 附偏差率/阈值 + 偏差明细摘要（前 MAX_DETAILS_IN_ALERT 条）。
        发送失败记 warning 不阻断监控。
        """
        mgr = self._alert_mgr()
        if mgr is None:
            return False
        # 明细摘要：截断前 N 条，避免 details 体积膨胀
        summary = [
            {
                "signal_id": d["signal_id"],
                "symbol": d["symbol"],
                "ts": d["ts"],
                "deviation_types": d["deviation_types"],
                "backtest_side": d["backtest_side"],
                "live_side": d["live_side"],
            }
            for d in details[:MAX_DETAILS_IN_ALERT]
        ]
        try:
            mgr.alert(
                level=SeverityLevel.WARNING,
                message=(
                    f"[signal_consistency] 回测实盘信号偏差率 {mismatch_rate:.2%} "
                    f"（{mismatched}/{aligned}）超阈值 {self._rate_max:.2%}，"
                    f"窗口内 total={total}"
                ),
                source=SOURCE_MODULE,
                category=CATEGORY_SIGNAL_CONSISTENCY,
                details={
                    "mismatch_rate": round(mismatch_rate, 6),
                    "rate_max": self._rate_max,
                    "total": total,
                    "aligned": aligned,
                    "mismatched": mismatched,
                    "detail_count": len(details),
                    "details_summary": summary,
                },
                deduplication_key=DEDUP_DEVIATION_RATE,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("信号一致性偏差率告警发送失败（不阻断监控）: %s", e)
            return False

    def _alert_mgr(self) -> Any:
        """延迟解析告警管理器（None 时取全局单例）。"""
        mgr = self._alert_manager
        if mgr is None:
            try:
                from myquant.monitoring.alerting import get_alert_manager

                mgr = get_alert_manager()
                self._alert_manager = mgr
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取 alert_manager 失败（告警未发送）: {e}")
                return None
        return mgr

    # ── 指标落盘（懒加载 collector；set_gauge 自动建指标）──────────
    def _publish_gauges(
        self, total: int, aligned: int, mismatched: int, mismatch_rate: float
    ) -> None:
        """把窗口对比统计写入 set_gauge（进 /api/metrics）。"""
        self._set_gauge(METRIC_TOTAL, float(total))
        self._set_gauge(METRIC_ALIGNED, float(aligned))
        self._set_gauge(METRIC_MISMATCHED, float(mismatched))
        self._set_gauge(METRIC_MISMATCH_RATE, round(mismatch_rate, 6))

    def _set_gauge(self, name: str, value: float) -> None:
        """set_gauge 一个仪表盘指标；采集器懒加载，失败不阻断。"""
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
            col.set_gauge(name, float(value))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"set_gauge {name} 失败（不阻断统计）: {e}")

    # ── 状态可观测（供驾驶舱/验收消费）─────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        """返回最近一次 run_comparison 面板快照（防未来：截至 now 时刻）。"""
        return dict(self._last_panel)


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：正常序列→注入偏差→告警附明细；含 None 配置不告警、防未来、防御校验。"""
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
            self.gauges[name] = float(value)

    base = 1_700_000_000.0
    clock_now = [base + 3600.0]

    def clock() -> float:
        return clock_now[0]

    def sig(i: int, side: str, price: float, ts: Optional[float] = None,
            symbol: str = "000001.SZ", sid: Optional[str] = None) -> Dict[str, Any]:
        return {
            "signal_id": sid or f"s{i}",
            "timestamp": base + i * 60.0 if ts is None else ts,
            "symbol": symbol,
            "side": side,
            "price": price,
        }

    # ── 用例1：正常序列（10 笔完全一致）→ aligned=10 mismatched=0 rate=0 不告警 ──
    am1 = _FakeAlert()
    met1 = _FakeMetrics()
    c1 = SignalConsistencyChecker(
        signal_deviation_rate_max=0.10, price_tolerance=0.01, time_tolerance=5.0,
        alert_manager=am1, metrics_collector=met1, clock=clock,
    )
    bt = [sig(i, "buy", 10.0 + i * 0.01) for i in range(10)]
    live = [sig(i, "buy", 10.0 + i * 0.01) for i in range(10)]
    p1 = c1.run_comparison(bt, live)
    check("1) 正常序列 aligned=10", p1["aligned"] == 10, f"{p1['aligned']}")
    check("1) 正常序列 mismatched=0", p1["mismatched"] == 0, f"{p1['mismatched']}")
    check("1) 正常序列 rate=0", p1["mismatch_rate"] == 0.0, f"{p1['mismatch_rate']}")
    check("1) 正常序列不告警", am1.calls == [], f"{am1.calls}")

    # ── 用例2：注入偏差（1 笔方向翻转 + 1 笔价格差超容差）→ rate=2/10=20% > 10% → WARNING ──
    am2 = _FakeAlert()
    met2 = _FakeMetrics()
    c2 = SignalConsistencyChecker(
        signal_deviation_rate_max=0.10, price_tolerance=0.01, time_tolerance=5.0,
        alert_manager=am2, metrics_collector=met2, clock=clock,
    )
    bt2 = [sig(i, "buy", 10.0 + i * 0.01) for i in range(10)]
    live2 = [sig(i, "buy", 10.0 + i * 0.01) for i in range(10)]
    live2[3] = sig(3, "sell", 10.03)                 # 方向翻转
    live2[7] = sig(7, "buy", 12.50)                  # 价格差巨大
    p2 = c2.run_comparison(bt2, live2)
    check("2) 注入偏差 mismatched=2", p2["mismatched"] == 2, f"{p2['mismatched']}")
    check("2) 偏差率=20%", abs(p2["mismatch_rate"] - 0.2) < 1e-6, f"{p2['mismatch_rate']}")
    check("2) 触发 WARNING", len(am2.calls) == 1 and am2.calls[0]["level"].name == "WARNING",
          f"calls={[c['deduplication_key'] for c in am2.calls]}")
    check("2) 告警 category=signal_consistency",
          am2.calls and am2.calls[0]["category"] == "signal_consistency", "")
    check("2) 告警 source=monitoring.signal_consistency",
          am2.calls and am2.calls[0]["source"] == "monitoring.signal_consistency", "")
    check("2) dedup 键防刷屏", am2.calls and am2.calls[0]["deduplication_key"] == DEDUP_DEVIATION_RATE, "")
    check("2) details 附偏差率/阈值",
          am2.calls and am2.calls[0]["details"]["mismatch_rate"] == 0.2
          and am2.calls[0]["details"]["rate_max"] == 0.10,
          f"{am2.calls[0]['details'] if am2.calls else None}")
    check("2) details 附偏差明细摘要",
          am2.calls and len(am2.calls[0]["details"]["details_summary"]) == 2, "")
    dev_types = {tuple(d["deviation_types"]) for d in p2["details"]}
    check("2) 明细含 direction 与 price 偏差",
          (DEV_DIRECTION,) in dev_types and (DEV_PRICE,) in dev_types, f"{dev_types}")

    # ── 用例3：时间漂移超容差 → 判 time_drift ──
    am3 = _FakeAlert()
    c3 = SignalConsistencyChecker(
        signal_deviation_rate_max=0.0, price_tolerance=0.01, time_tolerance=5.0,
        alert_manager=am3, metrics_collector=_FakeMetrics(), clock=clock,
    )
    bt3 = [sig(0, "buy", 10.0), sig(1, "buy", 10.0)]
    live3 = [sig(0, "buy", 10.0), sig(1, "buy", 10.0)]
    live3[1] = sig(1, "buy", 10.0, ts=base + 1 * 60.0 + 60.0)  # ts 漂移 60s > 5s
    p3 = c3.run_comparison(bt3, live3)
    check("3) 时间漂移超容差判 time_drift",
          any(DEV_TIME in d["deviation_types"] for d in p3["details"]), f"{p3['details']}")
    check("3) rate_max=0 时 1/2=50%>0 触发告警", len(am3.calls) == 1, f"{len(am3.calls)}")

    # ── 用例4：阈值 None（PENDING_CAL）→ 只计算不告警不编造 ──
    am4 = _FakeAlert()
    c4 = SignalConsistencyChecker(
        signal_deviation_rate_max=None, price_tolerance=0.01, time_tolerance=5.0,
        alert_manager=am4, metrics_collector=_FakeMetrics(), clock=clock,
    )
    bt4 = [sig(i, "buy", 10.0) for i in range(5)]
    live4 = [sig(i, "sell", 99.0) for i in range(5)]   # 全错
    p4 = c4.run_comparison(bt4, live4)
    check("4) None 阈值不告警", am4.calls == [], f"{am4.calls}")
    check("4) 指标照常计算输出", p4["mismatched"] == 5 and p4["aligned"] == 5,
          f"{p4['mismatched']},{p4['aligned']}")

    # ── 用例5：健康不告警（rate=0 即使阈值低）──
    am5 = _FakeAlert()
    c5 = SignalConsistencyChecker(
        signal_deviation_rate_max=0.01, price_tolerance=0.01, time_tolerance=5.0,
        alert_manager=am5, metrics_collector=_FakeMetrics(), clock=clock,
    )
    p5 = c5.run_comparison([sig(i, "buy", 10.0) for i in range(5)],
                           [sig(i, "buy", 10.0) for i in range(5)])
    check("5) 健康序列不告警", am5.calls == [] and p5["alert_triggered"] is False,
          f"{am5.calls}")

    # ── 用例6：防未来拒绝未来点 ──
    am6 = _FakeAlert()
    c6 = SignalConsistencyChecker(alert_manager=am6, clock=clock)
    rejected = False
    try:
        c6.run_comparison([sig(0, "buy", 10.0, ts=clock_now[0] + 100.0)], [])
    except ValueError:
        rejected = True
    check("6) 拒绝未来回测信号", rejected, "")
    rejected2 = False
    try:
        c6.run_comparison([], [sig(0, "buy", 10.0, ts=clock_now[0] + 100.0)])
    except ValueError:
        rejected2 = True
    check("6) 拒绝未来实盘信号", rejected2, "")

    # ── 用例7：防御校验（缺 symbol / 非数 ts / 越界阈值 / 负容差）──
    bad = False
    try:
        c6.run_comparison([{"timestamp": 1.0, "side": "buy", "price": 1.0}], [])
    except ValueError:
        bad = True
    check("7) 缺 symbol 拒绝", bad, "")
    bad2 = False
    try:
        SignalConsistencyChecker(signal_deviation_rate_max=-0.1)
    except ValueError:
        bad2 = True
    check("7) 负偏差率阈值拒绝", bad2, "")
    bad3 = False
    try:
        SignalConsistencyChecker(price_tolerance=-1.0)
    except ValueError:
        bad3 = True
    check("7) 负价格容差拒绝", bad3, "")

    # ── 用例8：one-sided 信号入明细（不进 aligned 分母）──
    am8 = _FakeAlert()
    c8 = SignalConsistencyChecker(
        signal_deviation_rate_max=None, alert_manager=am8,
        metrics_collector=_FakeMetrics(), clock=clock,
    )
    bt8 = [sig(0, "buy", 10.0), sig(1, "buy", 10.0)]
    live8 = [sig(0, "buy", 10.0)]   # 缺 s1
    p8 = c8.run_comparison(bt8, live8)
    check("8) one-sided 计入 total 不计 aligned",
          p8["aligned"] == 1 and p8["total"] == 2 and p8["unmatched_backtest"] == 1,
          f"{p8}")
    check("8) one-sided 明细 missing_in_live",
          any(DEV_MISSING_LIVE in d["deviation_types"] for d in p8["details"]),
          f"{p8['details']}")

    # ── 用例9：指标已注册落盘 ──
    check("9) total/aligned/mismatched/rate 指标已写",
          met2.gauges.get(METRIC_TOTAL) == 10.0
          and met2.gauges.get(METRIC_ALIGNED) == 10.0
          and met2.gauges.get(METRIC_MISMATCHED) == 2.0
          and abs(met2.gauges.get(METRIC_MISMATCH_RATE, -1) - 0.2) < 1e-6,
          f"{met2.gauges}")
    check("9) REGISTERED_METRICS 含 4 项",
          set(REGISTERED_METRICS) == {METRIC_TOTAL, METRIC_ALIGNED,
                                      METRIC_MISMATCHED, METRIC_MISMATCH_RATE},
          f"{REGISTERED_METRICS}")

    # ── 用例10：空序列（aligned=0）不除零、不告警 ──
    am10 = _FakeAlert()
    c10 = SignalConsistencyChecker(
        signal_deviation_rate_max=0.1, alert_manager=am10,
        metrics_collector=_FakeMetrics(), clock=clock,
    )
    p10 = c10.run_comparison([], [])
    check("10) 空序列 rate=0 不告警", p10["mismatch_rate"] == 0.0 and am10.calls == [],
          f"{p10}")

    print("=" * 60)
    print(f"signal_consistency.py 自测: {passed} 项通过")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"失败 {len(failures)} 项")
        return 1
    print("全部通过 ✅")
    print("=" * 60)

    # ── 偏差告警演示输出（验收口径：人为注入偏差→告警附明细）──
    print("[演示] 偏差率超阈告警详情：")
    if am2.calls:
        d = am2.calls[0]
        print(f"  level   = {d['level'].name}")
        print(f"  category= {d['category']}")
        print(f"  message = {d['message']}")
        print(f"  details = rate={d['details']['mismatch_rate']} "
              f"rate_max={d['details']['rate_max']} "
              f"mismatched={d['details']['mismatched']}/{d['details']['aligned']}")
        for s in d["details"]["details_summary"]:
            print(f"    - {s['signal_id']} {s['symbol']} ts={s['ts']} "
                  f"bt_side={s['backtest_side']} live_side={s['live_side']} "
                  f"dev={s['deviation_types']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
