# 文件名：myquant/monitoring/execution_stats.py
"""
execution_stats.py - 执行链路延迟/滑点统计 + 订单拒绝率监控（B-4 + B-5）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》两项：

B-4 信号→下单→成交三段延迟 P99 + 滑点（回写 #224）
  - 三段延迟（每段 start/end 均取自事件自带 event_ts，业务发生时刻）：
      1. 信号→下单：signal_ts  → ack_ts   （SIGNAL_GENERATED → ORDER_SUBMIT）
      2. 发送→ACK： send_ts   → ack_ts   （订单发送本地打戳 → 券商 ACK 确认）
      3. 下单→成交：ack_ts    → fill_ts   （ORDER_SUBMIT → ORDER_FILLED）
    另出端到端 信号→成交 signal_ts → fill_ts。
  - ACK 延迟（发送→ACK）P99 超过真源阈值（默认 20ms，LOCKED）→ P2 告警。
  - 滑点 = 成交价 vs 信号价/参考价（买/卖分别统计）：
      slippage_pct = (fill_price - ref_price) / ref_price * 100
      符号口径（docstring 固定，看板/报告引用本定义）：
        * 买单：slippage_pct > 0 表示买贵了（成交价高于参考价）；
        * 卖单：slippage_pct < 0 表示卖低了（成交价低于参考价）。
      即统一 (fill-ref)/ref 符号语义，买单为正=不利、卖单为负=不利。

B-5 订单拒绝率监控（回写 #225）
  - 拒单率 = 窗口内拒单数 / 窗口内报单数（事件驱动滑动窗口，事件时间 event_ts）。
  - 绝对阈值 SLO：拒单率 > order_reject_rate_max（0.1%，P0-2 LOCKED）→ P2 告警。
  - 飙升检测（R-04，PENDING_CAL）：仅当配置了 reject_rate_surge_multiple（N）
    与 reject_rate_surge_window_seconds（W）才启用；口径（本模块按真源语义定义）：
      * 当前窗 [t-W, t] 拒单率 r_now；对照基线窗 [t-2W, t-W] 拒单率 r_base；
      * 触发条件：r_now > N × r_base，且 r_base 窗内报单数 >= SURGE_MIN_BASE_SAMPLES
        （基线样本不足时不算基线飙升，避免冷启动误报）；
      * 触发 → P1 告警。
    未配置 N/窗口 → 只执行绝对阈值 0.1%，不编造默认倍数/窗口（PENDING_CAL 待实盘校准）。

触发模式（R-02）：POLL + 事件实时驱动
  - observe_signal / observe_order_submit / observe_order_fill / observe_order_reject
    为实时事件驱动入口（attach_event_bus 后由 OrderEvent/SignalEvent 自动驱动）；
  - collect_metrics() / summary() 供周期采集（B-8 驾驶舱）与看板消费（POLL 统计类，
    非 MTTD<1min 类硬实时）。

设计原则（铁律）:
  1. 阈值唯一真源 R-04：业务阈值经 get_monitoring_config().slo.trading 读取
     （order_ack_delay_p99_ms / order_reject_rate_max /
      reject_rate_surge_multiple / reject_rate_surge_window_seconds），
     代码内不硬编码业务阈值；未配置的飙升项视为禁用（None），不编造默认。
  2. 严格防未来：延迟段/滑点/拒单率全部只消费事件自带、已发生的 event_ts；
     滑动窗口以“已观测到的最大 event_ts”为水位线 t 向前剔除，不引用未来数据。
  3. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  4. 样本不足语义明确：延迟/滑点 P99 在样本数 < P99_MIN_SAMPLES 时不出 P99（返回 None），
     不下发 None 到指标、不据此告警；均值/样本数不受此限。

事件数据契约（消费 event.data，发布方按需填写；缺字段则防御式跳过该样本，不报错）:
  - SignalEvent(SIGNAL_GENERATED)：signal_id=链路信号 id；event.event_ts=信号时刻；
      data 可选 side("buy"/"sell")、signal_price(信号价)。
  - OrderEvent(ORDER_SUBMIT)：order_id=链路订单 id；event.event_ts=ACK 确认时刻；
      data 可选 signal_id、side、order_send_ts(订单发送本地打戳,算发送→ACK)、
      ref_price/signal_price(参考价)。
  - OrderEvent(ORDER_FILLED)：order_id + trade_id；event.event_ts=成交时刻；
      data 可选 side、fill_price(成交价)、ref_price/signal_price(参考价)。
  - OrderEvent(ORDER_REJECTED)：order_id；event.event_ts=拒单时刻；reject_reason。

依赖: math, time, collections, statistics(无), myquant.core.{settings,constants,logger}
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.execution_stats")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04）
# ─────────────────────────────────────────────

#: P99 最小样本数：样本数 < 此值不出 P99（返回 None）。非阈值，纯统计可靠性下限。
P99_MIN_SAMPLES: int = 30

#: 方向常量（与事件 data["side"] 取值对齐）
SIDE_BUY: str = "buy"
SIDE_SELL: str = "sell"

#: 各类样本缓冲上限（有界内存；超出丢弃最旧样本，保证常驻不膨胀）
MAX_LATENCY_SAMPLES: int = 10000
MAX_SLIPPAGE_SAMPLES: int = 10000
MAX_REJECT_EVENTS: int = 10000

#: 绝对拒单率评估窗口（秒）：真源未给绝对阈值窗口时的默认滑动窗长度。
#: 飙升窗口配置后自动复用为评估窗；此为兜底常量（非业务阈值）。
DEFAULT_RATE_WINDOW_SECONDS: float = 60.0

#: 飙升判定：基线窗最少报单数（不足则基线不可信，不判飙升，防冷启动误报）
SURGE_MIN_BASE_SAMPLES: int = 20

#: 指标名（与 BuiltinMetrics 注册一致；set_gauge 自动建指标，主控 Wave2 统一登记）
METRIC_SIGNAL_TO_FILL_P99: str = "execution_signal_to_fill_p99_ms"
METRIC_ACK_P99: str = "execution_ack_p99_ms"
METRIC_SIGNAL_TO_SUBMIT_P99: str = "execution_signal_to_submit_p99_ms"
METRIC_SUBMIT_TO_FILL_P99: str = "execution_submit_to_fill_p99_ms"
METRIC_SLIPPAGE_BUY_PCT: str = "slippage_buy_pct"
METRIC_SLIPPAGE_SELL_PCT: str = "slippage_sell_pct"
METRIC_SLIPPAGE_BUY_P99_PCT: str = "slippage_buy_p99_pct"
METRIC_SLIPPAGE_SELL_P99_PCT: str = "slippage_sell_p99_pct"
METRIC_ORDER_REJECT_RATE: str = "order_reject_rate"
METRIC_ORDER_SUBMITTED_TOTAL: str = "orders_submitted_total"
METRIC_ORDER_REJECTED_TOTAL: str = "orders_rejected_total"


class ExecutionStatsMonitor:
    """
    执行链路延迟/滑点统计（B-4）+ 订单拒绝率监控（B-5）。

    生命周期：
      - 事件驱动：attach_event_bus(bus) 后，SIGNAL_GENERATED/ORDER_SUBMIT/
        ORDER_FILLED/ORDER_REJECTED 到达即记录样本；也可手工调 observe_* 注入。
      - POLL 采集：collect_metrics() 周期把 P99/均值/拒单率写进 metrics_collector
        （进 /api/metrics 与 B-8 驾驶舱），并驱动 ACK P99(P2)/拒单率(P2)/飙升(P1) 告警。
      - summary() 返回结构化快照供看板/报告消费。

    防未来：所有延迟段与滑点都用事件自带 event_ts 两两相减；拒单率滑动窗口以
    “已观测最大 event_ts”为水位线 t，仅剔除 t-window 之前的事件，本行使用的数据截至 t 时刻。
    """

    def __init__(
        self,
        ack_limit_ms: Optional[float] = None,
        reject_rate_max: Optional[float] = None,
        surge_multiple: Optional[float] = None,
        surge_window_seconds: Optional[float] = None,
        rate_window_seconds: Optional[float] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        ack_limit_ms : float | None
            订单发送→ACK P99 告警阈值（毫秒）。None 读 slo.trading.order_ack_delay_p99_ms。
        reject_rate_max : float | None
            订单拒绝率绝对 SLO（如 0.001=0.1%）。None 读 slo.trading.order_reject_rate_max。
        surge_multiple : float | None
            拒单率飙升倍数 N（r_now > N × r_base）。None/配置为 "~" → 禁用飙升检测。
        surge_window_seconds : float | None
            飙升窗口长度 W（秒）。None/配置为 "~" → 禁用飙升检测。
        rate_window_seconds : float | None
            拒单率滑动评估窗长度（秒）。None：配置了飙升窗则复用飙升窗，否则用
            DEFAULT_RATE_WINDOW_SECONDS。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（懒加载，先例 rate_limit_monitor）。
        clock : Callable[[], float] | None
            当前时间戳函数（仅作事件时间戳缺失时的兜底；正常路径用事件自带 event_ts）。

        Raises
        ------
        ValueError : 阈值非法（ACK<=0、拒绝率不在 (0,1]、飙升倍数<=0、窗口<=0）时抛出。
        """
        slo = get_monitoring_config().get("slo", {}).get("trading", {})

        self._ack_limit_ms: float = (
            float(ack_limit_ms) if ack_limit_ms is not None
            else float(slo.get("order_ack_delay_p99_ms", 20))
        )
        self._reject_rate_max: float = (
            float(reject_rate_max) if reject_rate_max is not None
            else float(slo.get("order_reject_rate_max", 0.001))
        )

        # 飙升倍数/窗口：配置为 "~"（YAML null）或缺失 → PENDING_CAL，禁用飙升检测
        raw_mult = slo.get("reject_rate_surge_multiple", None)
        raw_win = slo.get("reject_rate_surge_window_seconds", None)
        self._surge_multiple: Optional[float] = (
            float(surge_multiple) if surge_multiple is not None
            else self._parse_optional_float(raw_mult)
        )
        self._surge_window: Optional[float] = (
            float(surge_window_seconds) if surge_window_seconds is not None
            else self._parse_optional_float(raw_win)
        )

        # 拒单率评估窗：显式 > 飙升窗 > 默认常量
        if rate_window_seconds is not None:
            self._rate_window: float = float(rate_window_seconds)
        elif self._surge_window is not None:
            self._rate_window = self._surge_window
        else:
            self._rate_window = DEFAULT_RATE_WINDOW_SECONDS

        # 防御式校验（拒绝脏配置运行）
        if self._ack_limit_ms <= 0:
            raise ValueError(f"ack_limit_ms 必须 > 0，实际 {self._ack_limit_ms!r}")
        if not (0.0 < self._reject_rate_max <= 1.0):
            raise ValueError(
                f"reject_rate_max 必须在 (0,1]，实际 {self._reject_rate_max!r}"
            )
        if self._rate_window <= 0:
            raise ValueError(f"rate_window_seconds 必须 > 0，实际 {self._rate_window!r}")
        if self._surge_multiple is not None and self._surge_multiple <= 1.0:
            raise ValueError(
                f"surge_multiple 必须 > 1.0（否则无飙升意义），实际 {self._surge_multiple!r}"
            )
        if (self._surge_multiple is None) != (self._surge_window is None):
            raise ValueError(
                "surge_multiple 与 surge_window_seconds 必须同时配置或同时留空（PENDING_CAL）"
            )

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector

        # 链路订单记录：order_id -> 已观测到的时间戳/价格事实
        self._orders: Dict[str, Dict[str, Any]] = {}
        # signal_id -> signal_ts（信号时刻，供信号→下单段配对）
        self._signals: Dict[str, float] = {}

        # 延迟样本缓冲（毫秒）
        self._lat_signal_to_submit: Deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)
        self._lat_ack: Deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)
        self._lat_submit_to_fill: Deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)
        self._lat_signal_to_fill: Deque[float] = deque(maxlen=MAX_LATENCY_SAMPLES)
        # 滑点样本缓冲（百分比，买/卖分列）
        self._slip_buy: Deque[float] = deque(maxlen=MAX_SLIPPAGE_SAMPLES)
        self._slip_sell: Deque[float] = deque(maxlen=MAX_SLIPPAGE_SAMPLES)

        # 拒单率滑动窗口：元素为 (event_ts, is_rejected)；水位线 = 已观测最大 event_ts
        self._reject_events: Deque = deque(maxlen=MAX_REJECT_EVENTS)
        self._watermark: float = 0.0
        self._submitted_total: int = 0
        self._rejected_total: int = 0

        self._lock = threading.Lock()
        self._handler_ids: List[str] = []
        logger.debug(
            "ExecutionStatsMonitor 初始化: ack_limit=%.1fms reject_max=%.4f "
            "surge=%s/%s rate_window=%.0fs",
            self._ack_limit_ms, self._reject_rate_max,
            self._surge_multiple, self._surge_window, self._rate_window,
        )

    # ── 配置辅助 ─────────────────────────────────────────────
    @staticmethod
    def _parse_optional_float(raw: Any) -> Optional[float]:
        """把 YAML 中的数值/"~"(null)/缺失 解析为 float 或 None（PENDING_CAL 视为禁用）。"""
        if raw is None:
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        if math.isnan(v) or math.isinf(v):
            return None
        return v

    # ── 事件链路装配 ────────────────────────────────────────
    def attach_event_bus(self, bus: Any) -> None:
        """
        订阅 EventBus：SIGNAL_GENERATED/ORDER_SUBMIT/ORDER_FILLED/ORDER_REJECTED。

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
                bus.subscribe(EventType.SIGNAL_GENERATED, self._handle_signal)
            )
            self._handler_ids.append(
                bus.subscribe(EventType.ORDER_SUBMIT, self._handle_order_submit)
            )
            self._handler_ids.append(
                bus.subscribe(EventType.ORDER_FILLED, self._handle_order_fill)
            )
            self._handler_ids.append(
                bus.subscribe(EventType.ORDER_REJECTED, self._handle_order_reject)
            )
            logger.info("ExecutionStatsMonitor 已订阅 EventBus SIGNAL/SUBMIT/FILLED/REJECTED")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ExecutionStatsMonitor 订阅 EventBus 失败（observe_* 仍可用）: {e}")

    # ── 事件处理器（防御式取字段，缺字段跳过）─────────────────
    @staticmethod
    def _get_event_ts(event: Any) -> Optional[float]:
        """取事件业务时刻 event_ts（ChainEvent 自带）；缺失回退事件创建 timestamp。"""
        ts = getattr(event, "event_ts", None)
        if ts is None:
            data = getattr(event, "data", None) or {}
            ts = data.get("event_ts")
        if ts is None:
            ts = getattr(event, "timestamp", None)
        if ts is None:
            return None
        try:
            return float(ts)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _get_data(event: Any, key: str, default: Any = None) -> Any:
        """从事件 data dict 防御式取业务字段（side/价格/order_send_ts 等）。"""
        data = getattr(event, "data", None) or {}
        if not isinstance(data, dict):
            return default
        return data.get(key, default)

    def _handle_signal(self, event: Any) -> None:
        """SIGNAL_GENERATED：记录信号时刻（按 signal_id 配对后续订单）。"""
        try:
            sid = getattr(event, "signal_id", "") or self._get_data(event, "signal_id", "")
            ts = self._get_event_ts(event)
            if sid and ts is not None:
                self.observe_signal(signal_id=str(sid), event_ts=ts)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"处理信号事件异常: {e}")

    def _handle_order_submit(self, event: Any) -> None:
        """ORDER_SUBMIT：记录 ACK 时刻；data 取 send_ts/side/ref_price/signal_id。"""
        try:
            oid = getattr(event, "order_id", "") or self._get_data(event, "order_id", "")
            ts = self._get_event_ts(event)
            if oid and ts is not None:
                self.observe_order_submit(
                    order_id=str(oid),
                    event_ts=ts,
                    signal_id=str(getattr(event, "signal_id", "") or self._get_data(event, "signal_id", "")),
                    send_ts=self._to_float(self._get_data(event, "order_send_ts")),
                    side=str(self._get_data(event, "side", "") or ""),
                    ref_price=self._to_float(
                        self._get_data(event, "ref_price", self._get_data(event, "signal_price"))
                    ),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"处理下单事件异常: {e}")

    def _handle_order_fill(self, event: Any) -> None:
        """ORDER_FILLED：记录成交时刻；data 取 side/fill_price/ref_price。"""
        try:
            oid = getattr(event, "order_id", "") or self._get_data(event, "order_id", "")
            ts = self._get_event_ts(event)
            if oid and ts is not None:
                self.observe_order_fill(
                    order_id=str(oid),
                    trade_id=str(getattr(event, "trade_id", "") or self._get_data(event, "trade_id", "")),
                    event_ts=ts,
                    side=str(self._get_data(event, "side", "") or ""),
                    fill_price=self._to_float(self._get_data(event, "fill_price")),
                    ref_price=self._to_float(
                        self._get_data(event, "ref_price", self._get_data(event, "signal_price"))
                    ),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"处理成交事件异常: {e}")

    def _handle_order_reject(self, event: Any) -> None:
        """ORDER_REJECTED：计入拒单窗口。"""
        try:
            oid = getattr(event, "order_id", "") or self._get_data(event, "order_id", "")
            ts = self._get_event_ts(event)
            reason = getattr(event, "reject_reason", "") or self._get_data(event, "reject_reason", "")
            if oid and ts is not None:
                self.observe_order_reject(order_id=str(oid), event_ts=ts, reject_reason=str(reason))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"处理拒单事件异常: {e}")

    @staticmethod
    def _to_float(v: Any) -> Optional[float]:
        """安全转 float；None/非法 → None（防御式跳过该字段）。"""
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return f

    # ── 实时观测入口（事件驱动；防未来：仅记录传入 event_ts）────
    def observe_signal(self, signal_id: str, event_ts: float) -> None:
        """
        记录一次信号产生（信号→下单段起点）。

        Parameters
        ----------
        signal_id : str
            链路信号 id（同一信号 signal→order→fill 保持一致）。
        event_ts : float
            信号业务发生时刻（epoch 秒）。防未来：只记录该时刻，不预测。
        """
        if not signal_id:
            return
        ts = self._to_float(event_ts)
        if ts is None:
            logger.warning("信号 event_ts 非法，忽略该信号样本")
            return
        with self._lock:
            # 防未来：本行使用的数据截至 ts 时刻（仅记录 ts 之前/等于 ts 的事实）
            self._signals[signal_id] = ts
            if ts > self._watermark:
                self._watermark = ts

    def observe_order_submit(
        self,
        order_id: str,
        event_ts: float,
        signal_id: str = "",
        send_ts: Optional[float] = None,
        side: str = "",
        ref_price: Optional[float] = None,
    ) -> None:
        """
        记录一次订单 ACK/确认（ORDER_SUBMIT 时刻），并计报单数。

        Parameters
        ----------
        order_id : str
            链路订单 id。
        event_ts : float
            订单 ACK/确认业务时刻（epoch 秒）。
        signal_id : str
            关联信号 id（用于信号→下单段配对）。
        send_ts : float | None
            订单发送到券商前的本地打戳时刻；提供时才出“发送→ACK”延迟样本。
        side : str
            "buy"/"sell"（滑点买卖分列用）。
        ref_price : float | None
            信号价/参考价（滑点分母用）。
        """
        if not order_id:
            return
        ack_ts = self._to_float(event_ts)
        if ack_ts is None:
            logger.warning("下单 event_ts 非法，忽略该订单样本")
            return
        s_ts = self._to_float(send_ts)
        ref = self._to_float(ref_price)
        with self._lock:
            rec = self._orders.setdefault(order_id, {})
            rec.update(
                signal_id=signal_id or rec.get("signal_id", ""),
                ack_ts=ack_ts,
                side=side or rec.get("side", ""),
            )
            if s_ts is not None:
                rec["send_ts"] = s_ts
            if ref is not None:
                rec["ref_price"] = ref

            # B-4：发送→ACK 延迟样本（毫秒）
            if s_ts is not None and ack_ts >= s_ts:
                self._lat_ack.append((ack_ts - s_ts) * 1000.0)
            elif s_ts is not None and ack_ts < s_ts:
                logger.warning("ACK 时刻早于发送时刻（event_ts 时序异常），跳过该 ACK 延迟样本")

            # B-4：信号→下单段（信号时刻已到）
            sid = rec.get("signal_id") or signal_id
            if sid and sid in self._signals:
                s_signal = self._signals[sid]
                if ack_ts >= s_signal:
                    self._lat_signal_to_submit.append((ack_ts - s_signal) * 1000.0)
                else:
                    logger.warning("ACK 时刻早于信号时刻（时序异常），跳过信号→下单段样本")

            # B-5：报单数 +1，写入拒单率窗口
            self._submitted_total += 1
            self._reject_events.append((ack_ts, False))
            if ack_ts > self._watermark:
                self._watermark = ack_ts
            self._purge_reject_events()
            self._evaluate_reject_rate()

    def observe_order_fill(
        self,
        order_id: str,
        trade_id: str,
        event_ts: float,
        side: str = "",
        fill_price: Optional[float] = None,
        ref_price: Optional[float] = None,
    ) -> None:
        """
        记录一次成交（ORDER_FILLED），计算下单→成交段、端到端、滑点。

        Parameters
        ----------
        order_id : str
            链路订单 id（与 ORDER_SUBMIT 配对）。
        trade_id : str
            成交 id（仅留痕，不强制）。
        event_ts : float
            成交业务时刻（epoch 秒）。
        side : str
            "buy"/"sell"。
        fill_price : float | None
            成交价（滑点分子用）。
        ref_price : float | None
            信号价/参考价（滑点分母用）。
        """
        if not order_id:
            return
        fill_ts = self._to_float(event_ts)
        if fill_ts is None:
            logger.warning("成交 event_ts 非法，忽略该成交样本")
            return
        fp = self._to_float(fill_price)
        ref = self._to_float(ref_price)
        with self._lock:
            rec = self._orders.setdefault(order_id, {})
            rec["fill_ts"] = fill_ts
            rec["trade_id"] = trade_id
            if side:
                rec["side"] = side
            if fp is not None:
                rec["fill_price"] = fp
            if ref is not None:
                rec["ref_price"] = ref

            ack_ts = rec.get("ack_ts")
            sid = rec.get("signal_id", "")
            # 下单→成交段
            if ack_ts is not None and fill_ts >= ack_ts:
                self._lat_submit_to_fill.append((fill_ts - ack_ts) * 1000.0)
            elif ack_ts is not None:
                logger.warning("成交时刻早于 ACK 时刻（时序异常），跳过下单→成交段样本")
            # 端到端 信号→成交
            if sid and sid in self._signals:
                s_signal = self._signals[sid]
                if fill_ts >= s_signal:
                    self._lat_signal_to_fill.append((fill_ts - s_signal) * 1000.0)

            # 滑点（买/卖分列；需要 side + 成交价 + 参考价）
            final_side = rec.get("side", "")
            final_fp = rec.get("fill_price")
            final_ref = rec.get("ref_price")
            if final_side in (SIDE_BUY, SIDE_SELL) and final_fp is not None and final_ref:
                slip_pct = (final_fp - final_ref) / final_ref * 100.0
                if final_side == SIDE_BUY:
                    self._slip_buy.append(slip_pct)
                else:
                    self._slip_sell.append(slip_pct)

            if fill_ts > self._watermark:
                self._watermark = fill_ts

    def observe_order_reject(self, order_id: str, event_ts: float, reject_reason: str = "") -> None:
        """
        记录一次订单拒绝（ORDER_REJECTED），计入拒单窗口并驱动 P2/P1 判定。

        Parameters
        ----------
        order_id : str
            链路订单 id。
        event_ts : float
            拒单业务发生时刻（epoch 秒）。
        reject_reason : str
            拒单原因（留痕进告警 details）。
        """
        if not order_id:
            return
        rej_ts = self._to_float(event_ts)
        if rej_ts is None:
            logger.warning("拒单 event_ts 非法，忽略该拒单样本")
            return
        with self._lock:
            self._rejected_total += 1
            self._reject_events.append((rej_ts, True))
            if rej_ts > self._watermark:
                self._watermark = rej_ts
            self._purge_reject_events()
            self._evaluate_reject_rate()

    # ── 拒单率窗口与判定（B-5）──────────────────────────────
    def _purge_reject_events(self) -> None:
        """
        剔除水位线之前的旧事件。

        防未来：以已观测最大 event_ts 为 t，仅保留 [t - max(rate_window, surge_window*2), t]；
        本行使用的数据截至 t 时刻。保留 2×飙升窗是为对照基线窗留足样本。
        """
        t = self._watermark
        keep_span = self._rate_window
        if self._surge_window is not None:
            keep_span = max(keep_span, self._surge_window * 2.0)
        cutoff = t - keep_span
        dq = self._reject_events
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def _window_rate(self, lo: float, hi: float, upper_inclusive: bool = False) -> Dict[str, float]:
        """
        返回时间窗内的报单数/拒单数/拒单率（纯计算，不告警）。

        Parameters
        ----------
        lo : float
            窗左界（含）。
        hi : float
            窗右界；upper_inclusive=False 时为开区间 [lo, hi)，True 时为闭区间 [lo, hi]。
            当前窗用闭区间（水位线 t 即最新事件时刻，须纳入）；基线窗用半开避免重复计数。
        upper_inclusive : bool
            右界是否含端点。
        """
        submitted = 0
        rejected = 0
        for ts, is_rej in self._reject_events:
            if not (lo <= ts):
                continue
            if upper_inclusive:
                if ts <= hi:
                    submitted += 1
                    if is_rej:
                        rejected += 1
            else:
                if ts < hi:
                    submitted += 1
                    if is_rej:
                        rejected += 1
        rate = (rejected / submitted) if submitted > 0 else 0.0
        return {"submitted": submitted, "rejected": rejected, "rate": rate}

    def current_reject_rate(self) -> Dict[str, float]:
        """
        返回当前滑动窗内拒单率快照（纯查询）。

        Returns
        -------
        dict : {window_submitted, window_rejected, reject_rate}
        """
        with self._lock:
            t = self._watermark
            win = self._window_snapshot(t)
            return {
                "window_submitted": float(win["submitted"]),
                "window_rejected": float(win["rejected"]),
                "reject_rate": round(win["rate"], 6),
            }

    def _window_snapshot(self, t: float) -> Dict[str, float]:
        """[t-rate_window, t] 当前窗拒单率（闭区间，含水位线最新事件）。"""
        return self._window_rate(t - self._rate_window, t, upper_inclusive=True)

    def _evaluate_reject_rate(self) -> None:
        """B-5 核心判定：绝对 SLO(P2) + 飙升(P1)。调用方需持有 _lock。"""
        t = self._watermark
        if t <= 0:
            return
        cur = self._window_snapshot(t)

        # 1) 绝对 SLO：窗口拒单率 > 0.1%（LOCKED）→ P2/WARNING
        if cur["submitted"] > 0 and cur["rate"] > self._reject_rate_max:
            self._fire_reject_slo_alert(cur)

        # 2) 飙升检测：仅当 N/W 已配置（PENDING_CAL 之外）才启用
        if self._surge_multiple is not None and self._surge_window is not None:
            w = self._surge_window
            cur_win = self._window_rate(t - w, t, upper_inclusive=True)    # 当前窗（闭）
            base_win = self._window_rate(t - 2.0 * w, t - w)               # 对照基线窗（半开）
            if (
                base_win["submitted"] >= SURGE_MIN_BASE_SAMPLES
                and base_win["rate"] > 0.0
                and cur_win["rate"] > self._surge_multiple * base_win["rate"]
            ):
                self._fire_reject_surge_alert(cur_win, base_win)

    # ── 告警 ────────────────────────────────────────────────
    def _get_alert_manager(self) -> Any:
        """取告警管理器（懒加载，失败返回 None 并记 warning）。"""
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

    def _fire_reject_slo_alert(self, cur: Dict[str, float]) -> None:
        """窗口拒单率超绝对 SLO（0.1%）→ P2/WARNING（category=trade）。"""
        mgr = self._get_alert_manager()
        if mgr is None:
            return
        mgr.alert(
            level=SeverityLevel.WARNING,
            message=(
                f"[execution] 订单拒绝率超 SLO：窗口内 {cur['rejected']:.0f}/{cur['submitted']:.0f}"
                f" = {cur['rate']:.3%}（上限 {self._reject_rate_max:.3%}）"
            ),
            source="monitoring.execution_stats",
            category="trade",
            details={
                "window_submitted": cur["submitted"],
                "window_rejected": cur["rejected"],
                "reject_rate": round(cur["rate"], 6),
                "slo_max": self._reject_rate_max,
                "window_seconds": self._rate_window,
            },
            deduplication_key="exec:reject_rate_slo",
        )

    def _fire_reject_surge_alert(self, cur: Dict[str, float], base: Dict[str, float]) -> None:
        """拒单率相对基线飙升超 N 倍 → P1/ERROR（category=trade）。"""
        mgr = self._get_alert_manager()
        if mgr is None:
            return
        mgr.alert(
            level=SeverityLevel.ERROR,
            message=(
                f"[execution] 拒单率飙升：当前窗 {cur['rate']:.3%} "
                f"({cur['rejected']:.0f}/{cur['submitted']:.0f}) "
                f"达基线窗 {base['rate']:.3%} 的 "
                f"{(cur['rate'] / base['rate'] if base['rate'] > 0 else 0):.1f} 倍"
                f"（阈值 N={self._surge_multiple:.1f}）"
            ),
            source="monitoring.execution_stats",
            category="trade",
            details={
                "current_window": {"submitted": cur["submitted"], "rejected": cur["rejected"],
                                   "rate": round(cur["rate"], 6)},
                "baseline_window": {"submitted": base["submitted"], "rejected": base["rejected"],
                                    "rate": round(base["rate"], 6)},
                "surge_multiple": self._surge_multiple,
                "surge_window_seconds": self._surge_window,
            },
            deduplication_key="exec:reject_surge",
        )

    def _fire_ack_alert(self, ack_p99: float) -> None:
        """ACK P99 超阈值（20ms LOCKED）→ P2/WARNING（category=trade）。"""
        mgr = self._get_alert_manager()
        if mgr is None:
            return
        mgr.alert(
            level=SeverityLevel.WARNING,
            message=(
                f"[execution] 订单发送→ACK P99 = {ack_p99:.1f}ms，"
                f"超 SLI 阈值 {self._ack_limit_ms:.0f}ms"
            ),
            source="monitoring.execution_stats",
            category="trade",
            details={
                "ack_p99_ms": round(ack_p99, 2),
                "limit_ms": self._ack_limit_ms,
                "sample_size": len(self._lat_ack),
            },
            deduplication_key="exec:ack_delay_p99",
        )

    # ── 统计计算 ─────────────────────────────────────────────
    @staticmethod
    def _p99(samples: Deque[float]) -> Optional[float]:
        """
        计算 P99（最近秩法）。样本数 < P99_MIN_SAMPLES 返回 None（语义明确：不出 P99）。

        Parameters
        ----------
        samples : Deque[float]
            样本集合（数值型）。

        Returns
        -------
        float | None : P99 值（毫秒/百分比）；样本不足返回 None。
        """
        n = len(samples)
        if n < P99_MIN_SAMPLES:
            return None
        ordered = sorted(samples)
        rank = math.ceil(0.99 * n)          # 最近秩法
        rank = max(1, min(rank, n))
        return float(ordered[rank - 1])

    @staticmethod
    def _mean(samples: Deque[float]) -> Optional[float]:
        """均值；空样本返回 None。"""
        if not samples:
            return None
        return sum(samples) / len(samples)

    # ── POLL 采集入口 ────────────────────────────────────────
    def collect_metrics(self) -> Dict[str, Any]:
        """
        周期采集：把 P99/均值/拒单率写进 metrics_collector，并驱动 ACK P99 P2 告警。

        由 metrics_collector/B-8 驾驶舱周期调用（POLL 统计类）。set_gauge 自动建指标。

        Returns
        -------
        dict : 本次采集写入的指标值（与 summary 一致的核心子集）。
        """
        with self._lock:
            self._purge_reject_events()
            ack_p99 = self._p99(self._lat_ack)
            s2f_p99 = self._p99(self._lat_signal_to_fill)
            s2s_p99 = self._p99(self._lat_signal_to_submit)
            s2fill_p99 = self._p99(self._lat_submit_to_fill)
            slip_buy_mean = self._mean(self._slip_buy)
            slip_sell_mean = self._mean(self._slip_sell)
            slip_buy_p99 = self._p99(self._slip_buy)
            slip_sell_p99 = self._p99(self._slip_sell)
            rej = self._window_snapshot(self._watermark)
            reject_rate = rej["rate"]

            # ACK P99 告警（样本足够且超阈值才发；dedup 固定键窗口内不刷屏）
            ack_alert_fired = False
            if ack_p99 is not None and ack_p99 > self._ack_limit_ms:
                self._fire_ack_alert(ack_p99)
                ack_alert_fired = True

        # 写指标（锁外调用 collector，避免慢 IO 持锁）
        self._set_gauge(METRIC_ACK_P99, ack_p99)
        self._set_gauge(METRIC_SIGNAL_TO_FILL_P99, s2f_p99)
        self._set_gauge(METRIC_SIGNAL_TO_SUBMIT_P99, s2s_p99)
        self._set_gauge(METRIC_SUBMIT_TO_FILL_P99, s2fill_p99)
        self._set_gauge(METRIC_SLIPPAGE_BUY_PCT, slip_buy_mean)
        self._set_gauge(METRIC_SLIPPAGE_SELL_PCT, slip_sell_mean)
        self._set_gauge(METRIC_SLIPPAGE_BUY_P99_PCT, slip_buy_p99)
        self._set_gauge(METRIC_SLIPPAGE_SELL_P99_PCT, slip_sell_p99)
        self._set_gauge(METRIC_ORDER_REJECT_RATE, reject_rate)
        self._inc_counter(METRIC_ORDER_SUBMITTED_TOTAL, self._submitted_total)
        self._inc_counter(METRIC_ORDER_REJECTED_TOTAL, self._rejected_total)

        return {
            "ack_p99_ms": ack_p99,
            "signal_to_fill_p99_ms": s2f_p99,
            "signal_to_submit_p99_ms": s2s_p99,
            "submit_to_fill_p99_ms": s2fill_p99,
            "slippage_buy_pct": slip_buy_mean,
            "slippage_sell_pct": slip_sell_mean,
            "reject_rate": round(reject_rate, 6),
            "ack_alert_fired": ack_alert_fired,
        }

    def summary(self) -> Dict[str, Any]:
        """
        返回监控结构化快照（供 B-2 报告 / B-8 驾驶舱 / B-10 看板消费）。

        Returns
        -------
        dict
            结构如下（数值为 None 表示样本不足/无样本）：
            {
              "latency": {
                  "signal_to_submit_ms": {"p99": float|None, "mean": float|None, "count": int},
                  "send_to_ack_ms":      {"p99": float|None, "mean": float|None, "count": int},
                  "submit_to_fill_ms":   {"p99": float|None, "mean": float|None, "count": int},
                  "signal_to_fill_ms":   {"p99": float|None, "mean": float|None, "count": int},
                  "ack_limit_ms": float,
              },
              "slippage": {
                  "buy_pct":  {"mean": float|None, "p99": float|None, "count": int},
                  "sell_pct": {"mean": float|None, "p99": float|None, "count": int},
                  "note": "买>0=买贵; 卖<0=卖低",
              },
              "rejection": {
                  "window_seconds": float,
                  "window_submitted": int,
                  "window_rejected": int,
                  "reject_rate": float,
                  "reject_rate_max": float,
                  "surge_enabled": bool,
                  "surge_multiple": float|None,
                  "surge_window_seconds": float|None,
                  "submitted_total": int,
                  "rejected_total": int,
              },
            }
        """
        with self._lock:
            self._purge_reject_events()
            rej = self._window_snapshot(self._watermark)
            return {
                "latency": {
                    "signal_to_submit_ms": {
                        "p99": self._p99(self._lat_signal_to_submit),
                        "mean": self._mean(self._lat_signal_to_submit),
                        "count": len(self._lat_signal_to_submit),
                    },
                    "send_to_ack_ms": {
                        "p99": self._p99(self._lat_ack),
                        "mean": self._mean(self._lat_ack),
                        "count": len(self._lat_ack),
                    },
                    "submit_to_fill_ms": {
                        "p99": self._p99(self._lat_submit_to_fill),
                        "mean": self._mean(self._lat_submit_to_fill),
                        "count": len(self._lat_submit_to_fill),
                    },
                    "signal_to_fill_ms": {
                        "p99": self._p99(self._lat_signal_to_fill),
                        "mean": self._mean(self._lat_signal_to_fill),
                        "count": len(self._lat_signal_to_fill),
                    },
                    "ack_limit_ms": self._ack_limit_ms,
                },
                "slippage": {
                    "buy_pct": {
                        "mean": self._mean(self._slip_buy),
                        "p99": self._p99(self._slip_buy),
                        "count": len(self._slip_buy),
                    },
                    "sell_pct": {
                        "mean": self._mean(self._slip_sell),
                        "p99": self._p99(self._slip_sell),
                        "count": len(self._slip_sell),
                    },
                    "note": "slippage=(fill-ref)/ref*100; 买>0=买贵, 卖<0=卖低",
                },
                "rejection": {
                    "window_seconds": self._rate_window,
                    "window_submitted": rej["submitted"],
                    "window_rejected": rej["rejected"],
                    "reject_rate": round(rej["rate"], 6),
                    "reject_rate_max": self._reject_rate_max,
                    "surge_enabled": self._surge_multiple is not None,
                    "surge_multiple": self._surge_multiple,
                    "surge_window_seconds": self._surge_window,
                    "submitted_total": self._submitted_total,
                    "rejected_total": self._rejected_total,
                },
            }

    # ── 指标写入（懒加载 collector；失败不阻断）──────────────
    def _set_gauge(self, name: str, value: Optional[float]) -> None:
        """set_gauge 写指标；None（样本不足）不下发；采集器懒加载。"""
        if value is None:
            return
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

    def _inc_counter(self, name: str, value: int) -> None:
        """写累计计数指标（用 set_gauge 直接写累计值，避免重复 inc 漂移）。"""
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


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：注入固定事件时间戳样本，验证三段延迟/滑点 P99、ACK P2、拒单率与飙升 P1。"""
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name} {info}")

    class FakeAlert:
        def __init__(self) -> None:
            self.calls: list = []

        def alert(self, **kw):
            self.calls.append(kw)
            return 1

    class FakeMetrics:
        def __init__(self) -> None:
            self.gauges: dict = {}

        def set_gauge(self, name, value, labels=None):
            self.gauges[name] = value

    am = FakeAlert()
    met = FakeMetrics()

    # 用例1：30 笔买单，发送→ACK 延迟恒 30ms（>20ms 阈值），滑点买贵 0.1%
    mon = ExecutionStatsMonitor(
        ack_limit_ms=20.0, reject_rate_max=0.001,
        alert_manager=am, metrics_collector=met,
    )
    t0 = 1000.0
    for i in range(30):
        sid = f"sig-{i}"
        oid = f"ord-{i}"
        mon.observe_signal(sid, event_ts=t0 + i * 0.1)
        mon.observe_order_submit(
            oid, event_ts=t0 + i * 0.1 + 0.03, signal_id=sid,
            send_ts=t0 + i * 0.1, side="buy", ref_price=10.0,
        )
        mon.observe_order_fill(
            oid, trade_id=f"trd-{i}", event_ts=t0 + i * 0.1 + 0.5,
            side="buy", fill_price=10.01, ref_price=10.0,
        )
    s = mon.summary()
    check("1) ACK P99=30ms（30ms 恒定）", abs(s["latency"]["send_to_ack_ms"]["p99"] - 30.0) < 1e-6,
          f"{s['latency']['send_to_ack_ms']}")
    check("1) 买单滑点均值=0.1%", abs(s["slippage"]["buy_pct"]["mean"] - 0.1) < 1e-6,
          f"{s['slippage']['buy_pct']}")
    check("1) 卖单无样本 count=0", s["slippage"]["sell_pct"]["count"] == 0, "")

    # collect_metrics 应触发 ACK P99>20ms → P2/WARNING
    out = mon.collect_metrics()
    p2 = [c for c in am.calls if c["deduplication_key"] == "exec:ack_delay_p99"]
    check("2) ACK P99>20ms 触发 P2/WARNING", len(p2) == 1 and p2[0]["level"].name == "WARNING",
          f"calls={[c['deduplication_key'] for c in am.calls]}")
    check("2) 指标 ack_p99_ms=30", abs(met.gauges.get(METRIC_ACK_P99, -1) - 30.0) < 1e-6,
          f"{met.gauges.get(METRIC_ACK_P99)}")
    check("2) collect 返回 ack_alert_fired=True", out["ack_alert_fired"] is True, "")

    # 用例2：小样本语义 —— 2 笔不出 P99
    am2 = FakeAlert()
    mon2 = ExecutionStatsMonitor(alert_manager=am2, metrics_collector=FakeMetrics())
    for i in range(2):
        mon2.observe_order_submit(f"o{i}", event_ts=2000.0 + i, send_ts=2000.0 + i - 0.01)
    s2 = mon2.summary()
    check("3) 小样本(<30) ACK P99=None", s2["latency"]["send_to_ack_ms"]["p99"] is None,
          f"{s2['latency']['send_to_ack_ms']}")
    check("3) 小样本均值仍出", abs(s2["latency"]["send_to_ack_ms"]["mean"] - 10.0) < 1e-6,
          f"{s2['latency']['send_to_ack_ms']}")
    mon2.collect_metrics()
    check("3) 小样本不触发 ACK 告警", am2.calls == [], f"{am2.calls}")

    # 用例3：拒单率 >0.1% → P2/WARNING
    am3 = FakeAlert()
    mon3 = ExecutionStatsMonitor(
        reject_rate_max=0.001, rate_window_seconds=60.0,
        alert_manager=am3, metrics_collector=FakeMetrics(),
    )
    t = 3000.0
    for i in range(99):
        mon3.observe_order_submit(f"r{i}", event_ts=t + i * 0.5)
    mon3.observe_order_reject("r99", event_ts=t + 50.0)  # 1/100=1% > 0.1%
    slo = [c for c in am3.calls if c["deduplication_key"] == "exec:reject_rate_slo"]
    check("4) 拒单率>0.1% 触发 P2/WARNING", len(slo) == 1 and slo[0]["level"].name == "WARNING",
          f"{[c['deduplication_key'] for c in am3.calls]}")

    # 用例4：飙升（配置 multiple=5, window=60s）→ P1/ERROR
    am4 = FakeAlert()
    mon4 = ExecutionStatsMonitor(
        reject_rate_max=1.0, surge_multiple=5.0, surge_window_seconds=60.0,
        alert_manager=am4, metrics_collector=FakeMetrics(),
    )
    # 基线窗 [0,60)：20 报单 0 拒单 → rate=0，不满足 base.rate>0，需造一个有基线率的场景
    # 基线窗 [40,100)：20 报单 1 拒单 =5%；当前窗 [100,160)：10 报单 6 拒单=60% > 5×5%
    base_t = 50.0
    for i in range(19):
        mon4.observe_order_submit(f"b{i}", event_ts=base_t + i)
    mon4.observe_order_reject("b-rej", event_ts=base_t + 10.0)  # 1/20=5%
    # 推进水位到当前窗，造飙升
    cur_t = 130.0
    for i in range(10):
        mon4.observe_order_submit(f"c{i}", event_ts=cur_t + i)
    for i in range(6):
        mon4.observe_order_reject(f"c-rej{i}", event_ts=cur_t + i)
    surge = [c for c in am4.calls if c["deduplication_key"] == "exec:reject_surge"]
    check("5) 拒单率飙升触发 P1/ERROR（真实 AlertManager 按 dedup_key 去重）",
          len(surge) >= 1 and surge[0]["level"].name == "ERROR",
          f"{[c['deduplication_key'] for c in am4.calls]}")

    # 用例5：未配置 N/窗口 → 不触发飙升告警
    am5 = FakeAlert()
    mon5 = ExecutionStatsMonitor(
        reject_rate_max=1.0,  # 绝对阈值关掉，避免干扰
        surge_multiple=None, surge_window_seconds=None,
        alert_manager=am5, metrics_collector=FakeMetrics(),
    )
    for i in range(20):
        mon5.observe_order_submit(f"u{i}", event_ts=5000.0 + i)
    for i in range(6):
        mon5.observe_order_reject(f"u-rej{i}", event_ts=5000.0 + 20 + i)
    no_surge = [c for c in am5.calls if c["deduplication_key"] == "exec:reject_surge"]
    check("6) 未配置飙升参数不触发飙升告警", no_surge == [], f"{am5.calls}")

    # 用例6：防未来 —— 时序异常（ack<send）样本被丢弃
    am6 = FakeAlert()
    mon6 = ExecutionStatsMonitor(alert_manager=am6, metrics_collector=FakeMetrics())
    mon6.observe_order_submit("future1", event_ts=4000.0, send_ts=4001.0)  # ack 早于 send
    s6 = mon6.summary()
    check("7) ack<send 时序异常不出 ACK 样本", s6["latency"]["send_to_ack_ms"]["count"] == 0,
          f"{s6['latency']['send_to_ack_ms']}")

    print("=" * 60)
    print(f"execution_stats.py 自测: {passed} 项通过")
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
