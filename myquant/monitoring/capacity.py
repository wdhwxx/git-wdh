# 文件名：myquant/monitoring/capacity.py
"""
capacity.py - 容量预测模块（C-8，回写#237）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》C-8：
  1. 数据量增长预测：摄入历史数据量落库点 (timestamp, data_size_mb)（已发生序列，
     如 processed / 时序库容量），按最小二乘线性趋势外推 N 天（N 读配置），
     输出预测值、剩余可用天数。
  2. 回测/清洗资源占用估算：estimate_backtest_usage / estimate_clean_usage
     基于已观测标定（单位成本）的线性缩放模型；峰值内存测量直接复用
     myquant.utils.memory_watcher.MemoryWatcher。
  3. 策略容量上限：注册策略数 vs 上限，超限预警。
  4. 容量不足判定：存储余量（headroom）低于阈值 / 策略数超上限
     → P2 预警（WARNING，category=capacity、source=monitoring.capacity、
     dedup 防刷屏、details 附数值）。
  5. 容量指标进看板：REGISTERED_METRICS 声明 + get_metrics_collector().set_gauge
     动态注册（不修改 metrics_collector.py）。
  6. 阈值真源 R-04：阈值读 get_monitoring_config() 的 monitoring.slo.capacity
     （growth_projection_days / storage_headroom_min_pct / strategy_count_limit
     均为 ~ = None = PENDING_CAL）。None 时只计算输出、不告警、不编造；
     演示/测试用构造注入阈值触发预警。

触发模式（R-02）：周期（POLL）
  - C-8 触发列为"周期"，本模块以 poll() 周期巡检复核告警；调用方按需
    record_data_size() / set_storage() / register_strategy() 喂"已发生"事实。

计算口径（全部基于"已发生"事实，严格防未来）：
  - 数据量点 = (timestamp, size_mb)，只消费 timestamp<=now 的点
    （record_data_size() 拒绝未来点）。本行使用的数据截至最近一个已发生点。
  - 增长趋势：对已发生点做最小二乘线性拟合（斜率 per_day）；样本 <2 或时间
    跨度为 0 → 无法外推，输出 None（不编造）。
  - 外推 N 天预测值 = 最新已观测 size + slope_per_day × N；
    剩余可用天数 = (storage_total_mb - 最新已观测 size) / slope_per_day
    （斜率<=0 或总容量未观测 → None，不编造）。
  - 存储余量 headroom_pct = (storage_total_mb - 已用 size) / storage_total_mb × 100。

设计原则（铁律）:
  1. 阈值唯一真源 R-04：业务阈值一律经 get_monitoring_config() 读取或构造注入，
     代码内不硬编码业务阈值（PENDING_CAL 项为 None 时即"不启用告警"，绝不编造）。
  2. 严格防未来：增长外推只消费"已发生"（timestamp<=now）的落库点；剩余天数是
     基于已观测趋势的外推，不消费任何未来点。
  3. 业务逻辑归 myquant；告警只调 get_alert_manager().alert()，不改 alerting.py。
  4. 资源估算的单位成本必须由调用方按"已观测事实"传入；未观测（None）的输出项
     返回 None，绝不硬编码"每样本 xx MB"之类的编造参数。
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config
from myquant.utils.memory_watcher import MemoryWatcher, get_memory_watcher

logger = get_logger("monitoring.capacity")

# ─────────────────────────────────────────────
# 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04 或构造注入）
# ─────────────────────────────────────────────
#: 告警分类（决定默认 Runbook 兜底，A-10）
CATEGORY_CAPACITY: str = "capacity"

#: 告警 source（模块名）
SOURCE_NAME: str = "monitoring.capacity"

#: 每日秒数（时间戳→天换算）
SECONDS_PER_DAY: float = 86_400.0

#: 缺省外推天数（N 未配置时的计算窗口；属计算参数非业务阈值，类比滚动窗口 1/7/30）
DEFAULT_PROJECTION_DAYS: int = 30

#: 存储余量告警去重键（固定窗口内不刷屏）
DEDUP_STORAGE_HEADROOM: str = "capacity:storage_headroom"
#: 策略数超限告警去重键
DEDUP_STRATEGY_COUNT: str = "capacity:strategy_count"

#: 每类数据点缓存上限（超出丢最旧，避免无界增长）
MAX_DATA_POINTS: int = 5000

#: 未来点判定容差（秒）：timestamp 超过 now 的绝对上限
_FUTURE_EPS: float = 1.0

#: 指标名（set_gauge 进 collector，无 labels）
METRIC_DATA_SIZE_GROWTH: str = "capacity_data_size_growth"
METRIC_DAYS_REMAINING: str = "capacity_estimated_days_remaining"
METRIC_STRATEGY_COUNT: str = "capacity_strategy_count"
METRIC_BACKTEST_PEAK_MEMORY: str = "capacity_backtest_peak_memory_mb"
METRIC_DATA_SIZE_CURRENT: str = "capacity_data_size_current_mb"
METRIC_HEADROOM_PCT: str = "capacity_storage_headroom_pct"

#: 本模块注册的全部指标名（供主控/驾驶舱查询）
REGISTERED_METRICS: List[str] = [
    METRIC_DATA_SIZE_GROWTH,
    METRIC_DAYS_REMAINING,
    METRIC_STRATEGY_COUNT,
    METRIC_BACKTEST_PEAK_MEMORY,
    METRIC_DATA_SIZE_CURRENT,
    METRIC_HEADROOM_PCT,
]


# ─────────────────────────────────────────────
# 资源占用估算（模块级纯函数；单位成本必须为已观测事实）
# ─────────────────────────────────────────────
def measure_current_rss_mb() -> float:
    """
    实测当前进程 RSS 内存（MB）——直接复用 MemoryWatcher，作为已观测锚点。

    Returns
    -------
    float : 当前进程 RSS（MB，保留 2 位小数）。MemoryWatcher 不可用/降级时
        仍返回其测量值（可能为 tracemalloc 近似），绝不编造。
    """
    try:
        watcher: MemoryWatcher = get_memory_watcher()
        info = watcher.get_memory_info(force_refresh=True)
        return float(info.get("rss_mb", 0.0))
    except Exception as e:  # noqa: BLE001
        logger.warning("MemoryWatcher 读取 RSS 失败（返回 0.0，不阻断）: %s", e)
        return 0.0


def estimate_backtest_usage(
    sample_count: int,
    *,
    peak_mb_per_sample: Optional[float] = None,
    disk_mb_per_sample: Optional[float] = None,
    seconds_per_sample: Optional[float] = None,
) -> Dict[str, Any]:
    """
    估算回测任务的资源占用（峰值内存 / 磁盘 / 耗时）。

    线性缩放模型：output = 单位成本 × sample_count。单位成本必须是调用方按
    "已观测事实"传入（如某次小规模回测实测得到的 MB/样本）；未观测（None）
    的输出项返回 None，绝不硬编码编造。

    Parameters
    ----------
    sample_count : int
        回测样本量（正整数）。
    peak_mb_per_sample : float | None
        已观测峰值内存单位成本（MB/样本）；None → peak_memory_mb 不编造。
    disk_mb_per_sample : float | None
        已观测落盘单位成本（MB/样本）；None → disk_mb 不编造。
    seconds_per_sample : float | None
        已观测耗时单位成本（秒/样本）；None → estimated_seconds 不编造。

    Returns
    -------
    dict :
        {"sample_count", "peak_memory_mb", "disk_mb", "estimated_seconds",
         "observed_current_rss_mb"}；未观测项为 None。

    Raises
    ------
    ValueError : sample_count 非正整数或单位成本为负时抛出。
    """
    if not isinstance(sample_count, int) or sample_count <= 0:
        raise ValueError(f"sample_count 必须为正整数，收到 {sample_count!r}")
    for name, v in (
        ("peak_mb_per_sample", peak_mb_per_sample),
        ("disk_mb_per_sample", disk_mb_per_sample),
        ("seconds_per_sample", seconds_per_sample),
    ):
        if v is not None and float(v) < 0:
            raise ValueError(f"{name} 不能为负，收到 {v!r}")

    peak_mb: Optional[float] = (
        round(sample_count * float(peak_mb_per_sample), 4)
        if peak_mb_per_sample is not None else None
    )
    disk_mb: Optional[float] = (
        round(sample_count * float(disk_mb_per_sample), 4)
        if disk_mb_per_sample is not None else None
    )
    est_seconds: Optional[float] = (
        round(sample_count * float(seconds_per_sample), 4)
        if seconds_per_sample is not None else None
    )
    return {
        "task": "backtest",
        "sample_count": sample_count,
        "peak_memory_mb": peak_mb,
        "disk_mb": disk_mb,
        "estimated_seconds": est_seconds,
        "observed_current_rss_mb": measure_current_rss_mb(),
    }


def estimate_clean_usage(
    sample_count: int,
    *,
    peak_mb_per_sample: Optional[float] = None,
    disk_mb_per_sample: Optional[float] = None,
    seconds_per_sample: Optional[float] = None,
) -> Dict[str, Any]:
    """
    估算数据清洗任务的资源占用（峰值内存 / 磁盘 / 耗时）。

    与 estimate_backtest_usage 同模型（线性缩放、单位成本须为已观测事实）。
    清洗任务的单位成本通常与回测不同，调用方应分别标定。

    Parameters
    ----------
    sample_count : int
        待清洗样本量（正整数）。
    peak_mb_per_sample / disk_mb_per_sample / seconds_per_sample : float | None
        已观测单位成本；None → 对应输出不编造（None）。

    Returns
    -------
    dict : {"task": "clean", "sample_count", "peak_memory_mb", "disk_mb",
        "estimated_seconds", "observed_current_rss_mb"}。

    Raises
    ------
    ValueError : sample_count 非正整数或单位成本为负时抛出。
    """
    result = estimate_backtest_usage(
        sample_count,
        peak_mb_per_sample=peak_mb_per_sample,
        disk_mb_per_sample=disk_mb_per_sample,
        seconds_per_sample=seconds_per_sample,
    )
    result["task"] = "clean"
    return result


class CapacityMonitor:
    """
    容量预测监控器（周期巡检 + 基于已发生落库点的增长外推与容量预警）。

    计算口径见模块 docstring。告警分级（阈值 R-04，读 slo.capacity，PENDING_CAL）:
      - 存储余量 headroom_pct < storage_headroom_min_pct（配置后）
          → WARNING 预警（category=capacity，dedup=capacity:storage_headroom）。
      - 注册策略数 > strategy_count_limit（配置后）
          → WARNING 预警（dedup=capacity:strategy_count）。
      阈值未配置（None=PENDING_CAL）→ 只计算增长外推/容量输出，不触发告警、不编造。

    防未来：record_data_size() 拒绝 timestamp>now 的落库点；增长外推/剩余天数
    只基于"已发生"点。本行使用的数据截至最近一个已发生落库点。

    使用方式:
        from myquant.monitoring.capacity import CapacityMonitor

        mon = CapacityMonitor()                       # 阈值读 slo.capacity
        mon.record_data_size(ts, size_mb)             # 喂已发生落库点
        mon.set_storage(total_mb=102400.0)            # 观测到的总容量
        mon.register_strategy("trend_a")              # 注册策略
        st = mon.poll()                              # 周期巡检：计算+复核告警
        panel = mon.to_dict()                        # 供驾驶舱消费
    """

    def __init__(
        self,
        growth_projection_days: Optional[int] = None,
        storage_headroom_min_pct: Optional[float] = None,
        strategy_count_limit: Optional[int] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
        storage_total_mb: Optional[float] = None,
        max_data_points: int = MAX_DATA_POINTS,
    ) -> None:
        """
        Parameters
        ----------
        growth_projection_days : int | None
            外推天数 N。None 读 slo.capacity.growth_projection_days；仍为 None
            → 用计算缺省 DEFAULT_PROJECTION_DAYS（属计算参数非告警阈值）。
        storage_headroom_min_pct : float | None
            存储余量下限（%）。None 读 slo.capacity.storage_headroom_min_pct；
            仍为 None（PENDING_CAL）→ 不触发存储告警，不编造。
        strategy_count_limit : int | None
            策略数上限。None 读 slo.capacity.strategy_count_limit；
            仍为 None（PENDING_CAL）→ 不触发策略数告警，不编造。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()（进 /api/metrics）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        storage_total_mb : float | None
            已观测到的存储总容量（MB）；None 时余量/剩余天数不编造（None）。
        max_data_points : int
            落库点缓存上限（超出丢最旧），必须为正整数。
        """
        if max_data_points <= 0:
            raise ValueError(f"max_data_points 必须为正整数，收到 {max_data_points}")

        slo = get_monitoring_config().get("slo", {}).get("capacity", {})

        # ── 外推天数 N（构造注入优先，回退配置，回退计算缺省）──
        self._projection_days: int = self._resolve_projection_days(
            growth_projection_days, slo.get("growth_projection_days")
        )

        # ── 告警阈值（R-04；构造注入优先，回退配置；None=PENDING_CAL 未配置=不启用）──
        self._headroom_min_pct: Optional[float] = self._as_optional_float(
            storage_headroom_min_pct
            if storage_headroom_min_pct is not None
            else slo.get("storage_headroom_min_pct"),
            "storage_headroom_min_pct",
        )
        self._strategy_limit: Optional[int] = self._as_optional_int(
            strategy_count_limit
            if strategy_count_limit is not None
            else slo.get("strategy_count_limit"),
            "strategy_count_limit",
        )

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector
        self._max_points = int(max_data_points)

        # 已观测事实
        self._storage_total_mb: Optional[float] = self._as_optional_positive(
            storage_total_mb, "storage_total_mb"
        )
        self._storage_used_override_mb: Optional[float] = None
        self._data_points: List[Tuple[float, float]] = []  # (ts, size_mb) 升序
        self._strategies: set = set()
        self._last_backtest_peak_mb: Optional[float] = None

        # 可观测状态（poll() 写入）
        self._last_poll_time: Optional[float] = None
        self._alert_triggered: bool = False
        self._degraded: bool = False
        self._last_state: Dict[str, Any] = {}

        logger.debug(
            "CapacityMonitor 初始化: projection_days=%s headroom_min_pct=%s "
            "strategy_limit=%s storage_total_mb=%s",
            self._projection_days, self._headroom_min_pct,
            self._strategy_limit, self._storage_total_mb,
        )

    # ── 防御式数值解析 ─────────────────────────────────────────────────
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
        float | None : 解析后的非负值；未配置返回 None。

        Raises
        ------
        ValueError : 阈值为负数时抛出（拒绝脏配置运行；业务阈值不允许为负）。
        """
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            logger.warning("capacity.%s 非法值 %r，按未配置（None=未启用）处理", name, value)
            return None
        if f < 0:
            raise ValueError(f"capacity.{name} 不能为负，实际 {f!r}")
        return f

    @staticmethod
    def _as_optional_int(value: Any, name: str) -> Optional[int]:
        """
        把配置/注入值解析为可选 int（策略数上限）；None 视为未配置。

        Parameters
        ----------
        value : Any
            原始配置值。
        name : str
            参数名。

        Returns
        -------
        int | None : 解析后的正整数上限；未配置返回 None。

        Raises
        ------
        ValueError : 上限非正整数时抛出。
        """
        if value is None:
            return None
        try:
            iv = int(value)
        except (TypeError, ValueError):
            logger.warning("capacity.%s 非法值 %r，按未配置（None=未启用）处理", name, value)
            return None
        if iv <= 0:
            raise ValueError(f"capacity.{name} 必须为正整数，实际 {iv!r}")
        return iv

    @staticmethod
    def _as_optional_positive(value: Any, name: str) -> Optional[float]:
        """解析可选正浮点数（已观测容量）；None 透传，非正值拒绝。"""
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须为可转 float，收到 {value!r}")
        if not math.isfinite(f) or f <= 0:
            raise ValueError(f"{name} 必须为有限正数，收到 {f!r}")
        return f

    @staticmethod
    def _resolve_projection_days(injected: Any, configured: Any) -> int:
        """
        解析外推天数 N：构造注入 > 配置 > 计算缺省 DEFAULT_PROJECTION_DAYS。

        Parameters
        ----------
        injected : Any
            构造注入值。
        configured : Any
            settings.yaml 配置值。

        Returns
        -------
        int : 最终使用的外推天数（正整数）。
        """
        for raw in (injected, configured):
            if raw is None:
                continue
            try:
                iv = int(raw)
            except (TypeError, ValueError):
                logger.warning("growth_projection_days 非法值 %r，回退缺省", raw)
                continue
            if iv > 0:
                return iv
            logger.warning("growth_projection_days 非正 %r，回退缺省", raw)
        return DEFAULT_PROJECTION_DAYS

    # ── 已发生事实摄入 ─────────────────────────────────────────────────
    def record_data_size(self, timestamp: float, size_mb: float) -> None:
        """
        摄入一个"已发生"的数据量落库点 (timestamp, size_mb)。

        防未来：timestamp 超过当前时刻 now（容差 1s）即拒绝抛 ValueError；
        等值重复点幂等去重（同时间戳只保留一条）。

        Parameters
        ----------
        timestamp : float
            落库点时间戳（epoch 秒，必须 <= now）。
        size_mb : float
            该时刻已落库的数据量（MB，非负有限数）。

        Raises
        ------
        ValueError : timestamp/size_mb 非法或为未来点时抛出。
        """
        try:
            ts = float(timestamp)
            size = float(size_mb)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"timestamp/size_mb 必须为可转 float，收到 ts={timestamp!r} size={size_mb!r}"
            ) from e
        if not math.isfinite(ts) or not math.isfinite(size) or size < 0:
            raise ValueError(
                f"timestamp/size_mb 必须为非负有限数，收到 ts={ts!r} size={size!r}"
            )
        now = self._clock()
        if ts > now + _FUTURE_EPS:
            raise ValueError(
                f"拒绝未来落库点：timestamp={ts:.3f} 晚于当前 {now:.3f}（防未来红线）"
            )

        for i, (old_ts, _) in enumerate(self._data_points):
            if abs(old_ts - ts) <= 1e-9:
                self._data_points[i] = (ts, size)
                return
        self._data_points.append((ts, size))
        self._data_points.sort(key=lambda p: p[0])
        if len(self._data_points) > self._max_points:
            del self._data_points[: len(self._data_points) - self._max_points]

    def set_storage(
        self,
        total_mb: Optional[float] = None,
        used_mb: Optional[float] = None,
    ) -> None:
        """
        登记已观测到的存储容量事实。

        Parameters
        ----------
        total_mb : float | None
            存储总容量（MB，正有限数）；None 表示不更新总容量。
        used_mb : float | None
            已用容量（MB，非负有限数）；None 表示用最新落库点推算已用量。

        Raises
        ------
        ValueError : total_mb / used_mb 非法时抛出。
        """
        if total_mb is not None:
            self._storage_total_mb = self._as_optional_positive(total_mb, "total_mb")
        if used_mb is not None:
            try:
                u = float(used_mb)
            except (TypeError, ValueError) as e:
                raise ValueError(f"used_mb 必须为可转 float，收到 {used_mb!r}") from e
            if not math.isfinite(u) or u < 0:
                raise ValueError(f"used_mb 必须为非负有限数，收到 {u!r}")
            self._storage_used_override_mb = u
        elif used_mb is None:
            # 显式传 None（且不传 total）时清空 override，回到落库点推算
            if total_mb is None:
                self._storage_used_override_mb = None

    def register_strategy(self, name: str) -> None:
        """
        注册一个策略（计入策略容量）。

        Parameters
        ----------
        name : str
            策略名（非空字符串）。

        Raises
        ------
        ValueError : name 为空/非字符串时抛出。
        """
        if not name or not isinstance(name, str):
            raise ValueError(f"策略名必须为非空字符串，收到 {name!r}")
        self._strategies.add(name)

    def unregister_strategy(self, name: str) -> None:
        """
        注销一个策略（从计数中移除）。

        Parameters
        ----------
        name : str
            策略名。
        """
        if name in self._strategies:
            self._strategies.discard(name)

    def note_backtest_peak_memory_mb(self, peak_mb: float) -> None:
        """
        登记一次回测实测峰值内存（MB），供 capacity_backtest_peak_memory_mb 指标。

        Parameters
        ----------
        peak_mb : float
            实测峰值内存（非负有限数）。
        """
        try:
            v = float(peak_mb)
        except (TypeError, ValueError) as e:
            raise ValueError(f"peak_mb 必须为可转 float，收到 {peak_mb!r}") from e
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"peak_mb 必须为非负有限数，收到 {v!r}")
        self._last_backtest_peak_mb = v

    # ── 增长外推（最小二乘线性趋势；严格防未来）────────────────────────
    def _fit_growth_per_day(self) -> Optional[float]:
        """
        对已发生落库点做最小二乘线性拟合，返回每日增长斜率（MB/天）。

        本行使用的数据截至最近一个已发生落库点（防未来）。

        Returns
        -------
        float | None : 斜率（MB/天，可为负=下降）；样本<2 或时间跨度为 0 → None。
        """
        pts = self._data_points
        n = len(pts)
        if n < 2:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        denom = sum((x - x_mean) ** 2 for x in xs)
        if denom <= 0:
            return None
        numer = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        slope_per_sec = numer / denom
        return slope_per_sec * SECONDS_PER_DAY

    def _compute_projection(self) -> Dict[str, Any]:
        """
        基于已发生落库点计算增长外推面板。

        Returns
        -------
        dict :
            {"point_count", "latest_size_mb", "growth_mb_per_day",
             "projected_n_days_mb", "projection_days", "estimated_days_remaining",
             "headroom_pct", "storage_total_mb", "used_mb"}；样本/容量不足项为 None。
        """
        pts = self._data_points
        latest_size: Optional[float] = pts[-1][1] if pts else None
        slope_per_day: Optional[float] = self._fit_growth_per_day()

        n = self._projection_days
        projected: Optional[float] = None
        if slope_per_day is not None and latest_size is not None:
            projected = round(latest_size + slope_per_day * n, 4)

        total = self._storage_total_mb
        used: Optional[float] = (
            self._storage_used_override_mb
            if self._storage_used_override_mb is not None
            else latest_size
        )

        headroom_pct: Optional[float] = None
        days_remaining: Optional[float] = None
        if total is not None and used is not None and total > 0:
            headroom_pct = round((total - used) / total * 100.0, 4)
            if slope_per_day is not None and slope_per_day > 0 and latest_size is not None:
                days_remaining = round((total - latest_size) / slope_per_day, 4)

        return {
            "point_count": len(pts),
            "latest_size_mb": round(latest_size, 4) if latest_size is not None else None,
            "growth_mb_per_day": round(slope_per_day, 6) if slope_per_day is not None else None,
            "projected_n_days_mb": projected,
            "projection_days": n,
            "estimated_days_remaining": days_remaining,
            "headroom_pct": headroom_pct,
            "storage_total_mb": total,
            "used_mb": round(used, 4) if used is not None else None,
        }

    # ── 周期巡检入口（POLL，R-02：C-8 触发列为周期）────────────────────
    def poll(self) -> Dict[str, Any]:
        """
        周期巡检：基于已发生落库点计算增长外推/容量面板并复核告警。

        Returns
        -------
        dict : 容量面板快照（同 to_dict）。
        """
        now = self._clock()
        self._last_poll_time = now
        self._alert_triggered = False
        self._degraded = False

        try:
            projection = self._compute_projection()
        except Exception as e:  # noqa: BLE001
            self._degraded = True
            logger.warning("容量增长计算异常（本次降级）: %s", e)
            projection = {
                "point_count": len(self._data_points),
                "latest_size_mb": None,
                "growth_mb_per_day": None,
                "projected_n_days_mb": None,
                "projection_days": self._projection_days,
                "estimated_days_remaining": None,
                "headroom_pct": None,
                "storage_total_mb": self._storage_total_mb,
                "used_mb": None,
                "degraded": True,
                "error": str(e),
            }

        strategy_count = len(self._strategies)

        # ── 告警复核（阈值未配置=None → 不告警、不编造）──
        headroom_alerted = self._evaluate_storage_alert(projection.get("headroom_pct"))
        strategy_alerted = self._evaluate_strategy_alert(strategy_count)
        if headroom_alerted or strategy_alerted:
            self._alert_triggered = True

        # 指标落盘（进 /api/metrics；采集器不可用仅记 warning 不阻断）
        self._publish_gauges(projection, strategy_count)

        self._last_state = {
            "projection": projection,
            "strategy_count": strategy_count,
            "headroom_min_pct": self._headroom_min_pct,
            "strategy_count_limit": self._strategy_limit,
            "backtest_peak_memory_mb": self._last_backtest_peak_mb,
            "alert_triggered": self._alert_triggered,
            "degraded": self._degraded,
            "last_poll_time": now,
        }
        return self._last_state

    # ── 告警判定（阈值 None=PENDING_CAL → 不告警）──────────────────────
    def _evaluate_storage_alert(self, headroom_pct: Optional[float]) -> bool:
        """
        复核存储余量告警：headroom_pct < 配置下限（配置后）→ WARNING。

        Parameters
        ----------
        headroom_pct : float | None
            当前存储余量百分比；None（总容量未观测）→ 不告警、不编造。

        Returns
        -------
        bool : 是否触发存储余量告警。
        """
        limit = self._headroom_min_pct
        if limit is None or headroom_pct is None:
            return False
        if headroom_pct >= limit:
            return False
        return self._fire_alert(
            dedup_key=DEDUP_STORAGE_HEADROOM,
            message=(
                f"[capacity] 存储余量 {headroom_pct:.2f}% < 阈值 {limit:.2f}%"
            ),
            details={
                "headroom_pct": headroom_pct,
                "headroom_min_pct": limit,
                "storage_total_mb": self._storage_total_mb,
                "used_mb": self._last_state.get("projection", {}).get("used_mb")
                if self._last_state else None,
            },
        )

    def _evaluate_strategy_alert(self, strategy_count: int) -> bool:
        """
        复核策略数超限告警：注册策略数 > 上限（配置后）→ WARNING。

        Parameters
        ----------
        strategy_count : int
            当前注册策略数。

        Returns
        -------
        bool : 是否触发策略数超限告警。
        """
        limit = self._strategy_limit
        if limit is None:
            return False
        if strategy_count <= limit:
            return False
        return self._fire_alert(
            dedup_key=DEDUP_STRATEGY_COUNT,
            message=(
                f"[capacity] 注册策略数 {strategy_count} > 上限 {limit}"
            ),
            details={
                "strategy_count": strategy_count,
                "strategy_count_limit": limit,
            },
        )

    def _fire_alert(
        self,
        dedup_key: str,
        message: str,
        details: Dict[str, Any],
    ) -> bool:
        """
        发送一条 WARNING 告警（category=capacity）；失败记 warning 不阻断。

        Returns
        -------
        bool : 是否成功发出（告警管理器可用且不抛）。
        """
        try:
            self._alert_mgr().alert(
                level=SeverityLevel.WARNING,
                message=message,
                source=SOURCE_NAME,
                category=CATEGORY_CAPACITY,
                details=details,
                deduplication_key=dedup_key,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("容量告警发送失败（不阻断监控）: %s", e)
            return False

    def _alert_mgr(self) -> Any:
        """延迟解析告警管理器（None 时取全局单例）。"""
        mgr = self._alert_manager
        if mgr is None:
            from myquant.monitoring.alerting import get_alert_manager

            mgr = get_alert_manager()
            self._alert_manager = mgr
        return mgr

    # ── 指标落盘（懒加载 collector；set_gauge 自动建指标）────────────────
    def _publish_gauges(self, projection: Dict[str, Any], strategy_count: int) -> None:
        """
        把最新容量面板写入 set_gauge（无 labels）。采集器不可用仅记 warning。
        """
        gauges: Dict[str, Optional[float]] = {
            METRIC_DATA_SIZE_GROWTH: projection.get("growth_mb_per_day"),
            METRIC_DAYS_REMAINING: projection.get("estimated_days_remaining"),
            METRIC_STRATEGY_COUNT: float(strategy_count),
            METRIC_BACKTEST_PEAK_MEMORY: self._last_backtest_peak_mb,
            METRIC_DATA_SIZE_CURRENT: projection.get("latest_size_mb"),
            METRIC_HEADROOM_PCT: projection.get("headroom_pct"),
        }
        for name, value in gauges.items():
            self._set_gauge(name, value)

    def _set_gauge(self, name: str, value: Optional[float]) -> None:
        """set_gauge 一个仪表盘指标；None（未启用/样本不足）落 0.0 占位。"""
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

    # ── 状态可观测（供驾驶舱/验收消费）─────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        """
        返回容量面板快照（增长外推 + 策略数 + 阈值 + 告警标记）。

        防未来：仅截至最近一次 poll 时刻（last_poll_time）已消费的落库点。
        """
        return dict(self._last_state)


# ═══════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：注入假告警/假指标/固定时钟，验证增长外推→容量预警闭环。"""
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
            self.gauges[name] = float(value)

    base = 1_700_000_000.0
    day = SECONDS_PER_DAY
    clock_now = [base + 9 * day]  # 第 9 天末

    def clock() -> float:
        return clock_now[0]

    # ── 1) 增长外推：10 天落库量线性 100→190 MB（每天 +10 MB）──────────
    am = _FakeAlert()
    met = _FakeMetrics()
    mon = CapacityMonitor(
        growth_projection_days=30,
        storage_headroom_min_pct=20.0,
        strategy_count_limit=5,
        alert_manager=am,
        metrics_collector=met,
        clock=clock,
        storage_total_mb=200.0,  # 总容量 200 MB，第9天已用 190 MB → headroom=5%
    )
    sizes = [100.0 + i * 10.0 for i in range(10)]  # 100,110,...,190
    for i, sz in enumerate(sizes):
        mon.record_data_size(base + i * day, sz)

    st = mon.poll()
    proj = st["projection"]
    check("1) 落库点 10 个", proj["point_count"] == 10, f"{proj}")
    check("1) 最新已观测 190 MB", proj["latest_size_mb"] == 190.0, f"{proj}")
    check("1) 增长斜率≈10 MB/天", abs(proj["growth_mb_per_day"] - 10.0) < 1e-3, f"{proj}")
    # 外推 30 天：190 + 10*30 = 490 MB
    check("1) 30 天预测≈490 MB", abs(proj["projected_n_days_mb"] - 490.0) < 1e-2, f"{proj}")
    # headroom = (200-190)/200*100 = 5% < 20% 阈值 → 应触发 WARNING
    check("1) headroom≈5%", abs(proj["headroom_pct"] - 5.0) < 1e-2, f"{proj}")
    head_alerts = [c for c in am.calls if c["deduplication_key"] == DEDUP_STORAGE_HEADROOM]
    check("1) 存储余量不足触发 WARNING", len(head_alerts) == 1 and head_alerts[0]["level"].name == "WARNING", f"calls={am.calls}")
    check("1) 告警 category=capacity/source=monitoring.capacity", head_alerts and head_alerts[0]["category"] == CATEGORY_CAPACITY and head_alerts[0]["source"] == SOURCE_NAME, f"{head_alerts}")

    # ── 2) 剩余可用天数：(200-190)/10 = 1 天 ──
    check("2) 剩余可用≈1 天", abs(proj["estimated_days_remaining"] - 1.0) < 1e-2, f"{proj}")

    # ── 3) 策略数超限：注册 6 个 > 上限 5 ──
    am.calls.clear()
    for i in range(6):
        mon.register_strategy(f"s{i}")
    st2 = mon.poll()
    check("3) 策略数=6", st2["strategy_count"] == 6, f"{st2['strategy_count']}")
    strat_alerts = [c for c in am.calls if c["deduplication_key"] == DEDUP_STRATEGY_COUNT]
    check("3) 策略数超限触发 WARNING", len(strat_alerts) == 1, f"calls={am.calls}")
    check("3) 告警标记", st2["alert_triggered"] is True, "")

    # ── 4) 指标已进看板 ──
    check("4) 增长指标已写", METRIC_DATA_SIZE_GROWTH in met.gauges, f"gauges={list(met.gauges)}")
    check("4) 剩余天数指标已写", METRIC_DAYS_REMAINING in met.gauges, "")
    check("4) 策略数指标=6", met.gauges.get(METRIC_STRATEGY_COUNT) == 6.0, f"{met.gauges.get(METRIC_STRATEGY_COUNT)}")
    check("4) 余量指标已写", METRIC_HEADROOM_PCT in met.gauges, "")

    # ── 5) 资源估算（单位成本为已观测事实）──
    est = estimate_backtest_usage(1_000, peak_mb_per_sample=0.002, disk_mb_per_sample=0.001, seconds_per_sample=0.0005)
    check("5) 回测峰值估算≈2 MB", est["peak_memory_mb"] is not None and abs(est["peak_memory_mb"] - 2.0) < 1e-3, f"{est}")
    check("5) 回测耗时估算≈0.5s", est["estimated_seconds"] is not None and abs(est["estimated_seconds"] - 0.5) < 1e-3, f"{est}")
    mon.note_backtest_peak_memory_mb(est["peak_memory_mb"])
    mon.poll()  # 登记峰值后下次巡检发布进快照/看板
    check("5) 回测峰值指标登记", mon.to_dict().get("backtest_peak_memory_mb") == 2.0, "")

    est_clean = estimate_clean_usage(500, peak_mb_per_sample=0.001)
    check("5) 清洗峰值估算≈0.5 MB", est_clean["peak_memory_mb"] is not None and abs(est_clean["peak_memory_mb"] - 0.5) < 1e-3, f"{est_clean}")
    check("5) 清洗未观测项=None（不编造）", est_clean["disk_mb"] is None and est_clean["estimated_seconds"] is None, f"{est_clean}")
    check("5) 当前实测 RSS 复用 memory_watcher", est["observed_current_rss_mb"] >= 0.0, f"{est}")

    # ── 6) 阈值未配置（None，PENDING_CAL）→ 只计算不告警、不编造 ──
    am2 = _FakeAlert()
    mon2 = CapacityMonitor(
        growth_projection_days=None,  # 走配置（None=PENDING_CAL）→ 计算缺省 30
        storage_headroom_min_pct=None,
        strategy_count_limit=None,
        alert_manager=am2,
        metrics_collector=_FakeMetrics(),
        clock=clock,
        storage_total_mb=200.0,
    )
    for i, sz in enumerate(sizes):
        mon2.record_data_size(base + i * day, sz)
    mon2.register_strategy("s0")
    st3 = mon2.poll()
    check("6) 未配置阈值不告警", am2.calls == [], f"calls={am2.calls}")
    check("6) 增长照常计算输出", st3["projection"]["growth_mb_per_day"] is not None, f"{st3['projection']}")
    check("6) 外推天数走缺省 30", st3["projection"]["projection_days"] == DEFAULT_PROJECTION_DAYS, f"{st3['projection']}")

    # ── 7) 健康状态不告警：余量充足 + 策略数低于上限 ──
    am3 = _FakeAlert()
    mon3 = CapacityMonitor(
        storage_headroom_min_pct=20.0,
        strategy_count_limit=5,
        alert_manager=am3,
        metrics_collector=_FakeMetrics(),
        clock=clock,
        storage_total_mb=10_000.0,  # 余量巨大
    )
    for i, sz in enumerate(sizes):
        mon3.record_data_size(base + i * day, sz)
    st4 = mon3.poll()
    check("7) 健康余量>阈值不告警", am3.calls == [], f"calls={am3.calls}")
    check("7) 健康告警标记=False", st4["alert_triggered"] is False, "")

    # ── 8) 防未来：拒绝未来落库点 ──
    rejected = False
    try:
        mon3.record_data_size(clock_now[0] + 100.0, 200.0)
    except ValueError:
        rejected = True
    check("8) 拒绝未来落库点", rejected, "")

    # ── 9) 防御式校验 ──
    bad = False
    try:
        mon3.record_data_size(base, -5.0)
    except ValueError:
        bad = True
    check("9) 负数据量拒绝", bad, "")
    bad2 = False
    try:
        CapacityMonitor(storage_headroom_min_pct=-1.0)
    except ValueError:
        bad2 = True
    check("9) 负余量阈值拒绝", bad2, "")
    bad3 = False
    try:
        estimate_backtest_usage(0)
    except ValueError:
        bad3 = True
    check("9) 非正样本量拒绝", bad3, "")
    bad4 = False
    try:
        mon3.register_strategy("")
    except ValueError:
        bad4 = True
    check("9) 空策略名拒绝", bad4, "")

    print("=" * 60)
    print(f"capacity.py 自测: {passed} 项通过")
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
