# 文件名：myquant/monitoring/data_quality_explorer.py
"""
data_quality_explorer.py - 数据质量探查器（纯Python数据层）
==============================================================
任务15新增：数据质量探查数据逻辑，纯 Python 数据层，不依赖 streamlit，可独立单元测试。

【#S2修复】从 myquant/web/dashboard.py 迁移到 myquant/monitoring/，消除 API 层依赖 Web 层的架构倒置。
迁移后：
  - API 层（api/routes.py）直接从 monitoring 导入，不再依赖 web
  - Web 层（web/dashboard.py）从 monitoring 导入，保持向后兼容
  - 本文件为唯一权威定义位置

数据来源:
  - CleanDataQueries（任务13）：清洗运行元信息（clean_run_meta）/ 脏数据统计（dirty_data_stats）
  - MetricsCollector（任务11）：数据质量实时指标（dirty_ratio/dirty_count/pipeline_runs 等）

防未来函数:
  - 只读取数据库/指标采集器中【已发生】的数据
  - get_clean_runs 的 end_time 严格过滤由 CleanDataQueries（任务13）保证
  - 本类无任何预测方法/属性

为实盘对接:
  - source 过滤区分 offline/live（实盘清洗运行 source="live"）
  - 实时指标展示实盘数据质量状态
  - get_latest_clean_run 支持实盘断点续跑监控
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config

logger = get_logger("monitoring.data_quality_explorer")


class DataQualityExplorer:
    """
    数据质量探查数据逻辑（任务15新增）

    纯 Python 数据层，不依赖 streamlit，可独立单元测试。

    数据来源:
      - CleanDataQueries（任务13）：清洗运行元信息（clean_run_meta）/ 脏数据统计（dirty_data_stats）
      - MetricsCollector（任务11）：数据质量实时指标（dirty_ratio/dirty_count/pipeline_runs 等）

    防未来函数:
      - 只读取数据库/指标采集器中【已发生】的数据
      - get_clean_runs 的 end_time 严格过滤由 CleanDataQueries（任务13）保证
      - 本类无任何预测方法/属性

    为实盘对接:
      - source 过滤区分 offline/live（实盘清洗运行 source="live"）
      - 实时指标展示实盘数据质量状态
      - get_latest_clean_run 支持实盘断点续跑监控
    """

    def __init__(
        self,
        connection: Any = None,
        metrics_collector: Any = None,
    ):
        """
        Parameters
        ----------
        connection : DatabaseConnection | None
            数据库连接（None 用 get_connection() 全局单例，默认 quant.db）
        metrics_collector : MetricsCollector | None
            指标采集器（None 用 get_metrics_collector() 全局单例）
        """
        from myquant.database import CleanDataQueries, get_connection
        from myquant.monitoring.metrics_collector import get_metrics_collector
        self._connection = connection if connection is not None else get_connection()
        self._queries = CleanDataQueries(self._connection)
        self._collector = metrics_collector if metrics_collector is not None else get_metrics_collector()
        logger.info("数据质量探查器初始化")

    # ─────────────────────────────────────────────
    #  中文描述映射辅助
    # ─────────────────────────────────────────────

    @staticmethod
    def grade_desc(grade: Optional[str]) -> str:
        """质量等级中文描述"""
        if not grade:
            return "-"
        from myquant.core.constants import QualityGrade, QUALITY_GRADE_DESC_MAP
        try:
            return QUALITY_GRADE_DESC_MAP.get(QualityGrade(grade), grade)
        except (ValueError, KeyError):
            return str(grade)

    @staticmethod
    def dirty_type_desc(dirty_type: Optional[str]) -> str:
        """脏数据类型中文描述"""
        if not dirty_type:
            return "-"
        from myquant.core.constants import DirtyDataType, DIRTY_TYPE_DESC_MAP
        try:
            return DIRTY_TYPE_DESC_MAP.get(DirtyDataType(dirty_type), dirty_type)
        except (ValueError, KeyError):
            return str(dirty_type)

    # ─────────────────────────────────────────────
    #  实时质量指标（MetricsCollector 任务11）
    # ─────────────────────────────────────────────

    def get_metrics(self) -> Dict[str, Any]:
        """
        读取数据质量实时指标（无数据返回 None）

        Returns
        -------
        dict : {
            total_records, dirty_count, clean_count, dirty_ratio,
            quality_grade, pipeline_runs, pipeline_failures, repair_count
        }
        """
        from myquant.monitoring.metrics_collector import BuiltinMetrics
        names = {
            "total_records": BuiltinMetrics.DATA_QUALITY_TOTAL_RECORDS,
            "dirty_count": BuiltinMetrics.DATA_QUALITY_DIRTY_COUNT,
            "clean_count": BuiltinMetrics.DATA_QUALITY_CLEAN_COUNT,
            "dirty_ratio": BuiltinMetrics.DATA_QUALITY_DIRTY_RATIO,
            "quality_grade": BuiltinMetrics.DATA_QUALITY_QUALITY_GRADE,
            "pipeline_runs": BuiltinMetrics.DATA_QUALITY_PIPELINE_RUNS,
            "pipeline_failures": BuiltinMetrics.DATA_QUALITY_PIPELINE_FAILURES,
            "repair_count": BuiltinMetrics.DATA_QUALITY_REPAIR_COUNT,
        }
        metrics: Dict[str, Any] = {}
        for key, name in names.items():
            m = self._collector.get(name)
            if m is None:
                # 任务16增强（向后兼容）：无标签读取不到时，回退读取该指标名下
                # 最新的带标签数据点。真实 pipeline 用 collect_data_quality 写入
                # 带 symbol/data_type 标签的指标（实盘场景），无标签 get() 取不到。
                # 无标签指标场景（get() 直接命中）与原逻辑完全一致；无任何数据时
                # 仍返回 None，不影响既有行为。
                points = self._collector.get_all(name)
                if points:
                    m = points[-1]  # dict 保序，最后插入=最新
            metrics[key] = m.value if m is not None else None
        return metrics

    # ─────────────────────────────────────────────
    #  清洗运行查询（CleanDataQueries 任务13）
    # ─────────────────────────────────────────────

    def get_clean_runs(
        self,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        清洗运行列表（按 started_at 倒序）

        【防未来函数】仅返回已发生的清洗运行（任务13 end_time 严格过滤）。
        """
        return self._queries.get_clean_runs(
            symbol=symbol,
            data_type=data_type,
            status=status,
            source=source,
            limit=limit,
        )

    def count_clean_runs(
        self,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
    ) -> int:
        """清洗运行次数统计"""
        return self._queries.count_clean_runs(
            symbol=symbol, data_type=data_type, status=status, source=source,
        )

    def get_run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        """单次清洗运行完整详情"""
        return self._queries.get_clean_run(run_id)

    def get_latest_run(
        self,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """最近一次清洗运行（实盘断点续跑监控）"""
        return self._queries.get_latest_clean_run(symbol=symbol, data_type=data_type)

    # ─────────────────────────────────────────────
    #  质量等级分布 / 脏数据统计
    # ─────────────────────────────────────────────

    def get_grade_distribution(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        按质量等级聚合最近 N 次清洗运行

        Returns
        -------
        list[dict] : [{grade, grade_desc, count, ratio}, ...]（按 count 降序）
        """
        runs = self._queries.get_clean_runs(limit=limit)
        dist: Dict[str, int] = {}
        for r in runs:
            g = r.get("quality_grade") or "unknown"
            dist[g] = dist.get(g, 0) + 1
        total = len(runs)
        result = []
        for grade, cnt in sorted(dist.items(), key=lambda kv: kv[1], reverse=True):
            result.append({
                "grade": grade,
                "grade_desc": self.grade_desc(grade),
                "count": cnt,
                "ratio": round(cnt / total, 4) if total else 0,
            })
        return result

    def get_dirty_stats(
        self,
        run_id: Optional[str] = None,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
        dirty_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """脏数据统计明细（补充中文描述字段 dirty_type_desc）"""
        stats = self._queries.get_dirty_stats(
            run_id=run_id, symbol=symbol, data_type=data_type, dirty_type=dirty_type,
        )
        for s in stats:
            s["dirty_type_desc"] = self.dirty_type_desc(s.get("dirty_type"))
        return stats

    def get_dirty_summary(
        self,
        run_id: Optional[str] = None,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        脏数据统计汇总（任务13 get_dirty_stats_summary + 中文描述）

        Returns
        -------
        dict : {
            total_bad, total_types, error_count, warning_count,
            by_type, by_severity, top_dirty_types, by_type_desc
        }
        """
        summary = self._queries.get_dirty_stats_summary(
            run_id=run_id, symbol=symbol, data_type=data_type,
        )
        summary["by_type_desc"] = {
            dt: {"bad_count": cnt, "desc": self.dirty_type_desc(dt)}
            for dt, cnt in summary.get("by_type", {}).items()
        }
        return summary

    # ─────────────────────────────────────────────
    #  总览
    # ─────────────────────────────────────────────

    def get_overview(self, limit: int = 20) -> Dict[str, Any]:
        """
        数据质量总览（页面一次调用即可渲染）

        Returns
        -------
        dict : {
            metrics, total_runs, latest_run, grade_distribution, dirty_summary
        }
        """
        return {
            "metrics": self.get_metrics(),
            "total_runs": self.count_clean_runs(),
            "latest_run": self.get_latest_run(),
            "grade_distribution": self.get_grade_distribution(limit=limit),
            "dirty_summary": self.get_dirty_summary(),
        }

    def __repr__(self) -> str:
        return (
            f"DataQualityExplorer(connection={self._connection!r}, "
            f"collector={self._collector!r})"
        )


# ═══════════════════════════════════════════════════════════════
#  B-7：数据新鲜度 SLI（L2 增强，事件/周期）（回写#227）
# ═══════════════════════════════════════════════════════════════
#
# 验收口径（施工总清单 B-7 行）：
#   - 数据新鲜度引用 P0-2 锁定清单（<1min、缺口=0，LOCKED）；每日数据缺口统计；
#     因子/财务同步任务延迟监控（分别设值）。
#   - 验收 = 数据新鲜度指标可查，缺口/延迟超阈值告警；阈值取自 P0-2（R-04 唯一真源）。
#
# 阈值真源（R-04）：config/settings.yaml monitoring.slo.data 块，
#   读取入口 myquant.core.settings.get_monitoring_config()：
#     freshness_max_seconds = 60   （LOCKED 硬约束：新鲜度 <1min，必须生效）
#     gap_allowed           = 0   （LOCKED 硬约束：缺口 =0，缺口>0 即告警）
#     factor_sync_delay_max_seconds   = ~（None，PENDING_CAL 待实盘校准 → 不告警、不编造数值）
#     financial_sync_delay_max_seconds = ~（None，PENDING_CAL 待实盘校准 → 不告警、不编造数值）
#
# 缺口口径：应到数据点缺失的统计。本类不臆造调度计划；缺口数量由注入的 gap_provider
#   （只读外部已统计的缺口快照）或 record_gap() 显式登记提供，按日聚合（date 键取
#   ISO "YYYY-MM-DD"，字典序即时间序）。当前评估缺口 = 最新一个日期键的缺口数量。
#
# 防未来函数：
#   - 新鲜度滞后 = max(0, now - 最新已到达数据时间戳)，只消费"已发生"数据；
#     未来时间戳（时钟偏差）被截断为 0，不计负滞后、不误告警。
#   - 缺口/同步延迟只统计"已缺失/已完成"事实，不预测下一笔到达。

# ── 常量（非业务阈值；业务阈值一律来自 settings.yaml R-04）──
#: 告警分类 / 来源
SLI_CATEGORY_DATA_QUALITY: str = "data_quality"
SLI_SOURCE_DATA_FRESHNESS: str = "monitoring.data_quality"

#: 指标名（set_gauge 自动建指标；BuiltinMetrics 登记由主控 Wave2 统一做）
METRIC_FRESHNESS_LAG_SECONDS: str = "data_freshness_lag_seconds"
METRIC_DATA_GAP_COUNT: str = "data_gap_count"
METRIC_FACTOR_SYNC_DELAY_SECONDS: str = "factor_sync_delay_seconds"
METRIC_FINANCIAL_SYNC_DELAY_SECONDS: str = "financial_sync_delay_seconds"

#: 新鲜度超阈严重度分级系数：lag > freshness_max × 该系数 → ERROR(P1)，否则 WARNING(P2)
FRESHNESS_ERROR_MULTIPLE: float = 2.0


def _none_to_float(value: Any) -> Optional[float]:
    """
    把配置值安全转 float；None 或非法值统一回退 None（PENDING_CAL 维度=不启用）。

    Parameters
    ----------
    value : Any
        配置原始值（yaml 的 ~ → None，或数值）。

    Returns
    -------
    float | None : 转成功返回 float；None/非法返回 None。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("同步延迟阈值非法（%r），按未配置处理（不告警）", value)
        return None


class DataFreshnessSli:
    """
    数据新鲜度 SLI（B-7，事件/周期触发）。

    度量三件事（阈值全部读 monitoring.slo.data，R-04）：
      1. 新鲜度滞后：按标的/数据类统计"最新已到达数据时间戳 vs 当前时间"的滞后秒数；
         滞后 > freshness_max_seconds(60s, LOCKED) → 告警（超 2× 升 ERROR/P1，否则 WARNING/P2）。
      2. 每日数据缺口：按日聚合应到而缺失的数据点数量；缺口 > gap_allowed(0, LOCKED)
         → ERROR/P1 告警（缺口=0 是硬约束，任何缺口即 P1）。
      3. 因子/财务同步任务延迟：分别设值；阈值未配置(None)=PENDING_CAL → 该维度不告警、
         不编造数值；已配置且延迟超限 → WARNING(P2) 告警。

    指标（经 get_metrics_collector().set_gauge 写入，进 /api/metrics）：
      data_freshness_lag_seconds / data_gap_count /
      factor_sync_delay_seconds / financial_sync_delay_seconds

    触发：poll()/collect() 供周期巡检；attach_event_bus() 订阅数据到达事件
      （EventType.DATA_LOADED）到达即触发一次 poll（事件驱动兜底周期）。

    防未来：只消费"已发生"的最新时间戳/缺口/同步完成时刻，滞后截断为非负，不预测。
    """

    def __init__(
        self,
        freshness_max_seconds: Optional[float] = None,
        gap_allowed: Optional[int] = None,
        factor_sync_delay_max_seconds: Optional[float] = None,
        financial_sync_delay_max_seconds: Optional[float] = None,
        latest_timestamp_provider: Optional[Callable[[], Dict[str, float]]] = None,
        gap_provider: Optional[Callable[[], Dict[str, int]]] = None,
        alert_manager: Any = None,
        metrics_collector: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        freshness_max_seconds : float | None
            新鲜度滞后上限（秒，LOCKED=60）。None 读 slo.data.freshness_max_seconds。
        gap_allowed : int | None
            允许缺口数（LOCKED=0）。None 读 slo.data.gap_allowed。
        factor_sync_delay_max_seconds : float | None
            因子同步延迟上限（秒）。None 读 slo.data.factor_sync_delay_max_seconds；
            读到 None=PENDING_CAL → 该维度不告警、不编造数值。
        financial_sync_delay_max_seconds : float | None
            财务同步延迟上限（秒）。同上。
        latest_timestamp_provider : Callable[[], dict[str, float]] | None
            "最新数据时间戳提供者"回调：返回 {data_key: 最新到达 epoch 秒}。
            data_key 一般为 "标的:数据类"（如 "SH600000:1d"）。None 缺省返回空快照
            （无数据场景防御：不编造时间戳、不误告警）。
        gap_provider : Callable[[], dict[str, int]] | None
            "每日缺口统计"回调：返回 {date(YYYY-MM-DD): 当日缺口数量}。None 缺省空。
        alert_manager : AlertManager | None
            告警管理器；None 延迟取 get_alert_manager()。
        metrics_collector : MetricsCollector | None
            指标采集器；None 延迟取 get_metrics_collector()。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。

        Raises
        ------
        ValueError : freshness_max_seconds<=0 / gap_allowed<0 / 已配置的同步延迟<=0 时抛出
            （拒绝脏配置运行；LOCKED 硬约束必须生效）。
        """
        slo_data = get_monitoring_config().get("slo", {}).get("data", {}) or {}

        self._freshness_max: float = (
            float(freshness_max_seconds) if freshness_max_seconds is not None
            else float(slo_data.get("freshness_max_seconds", 60))
        )
        self._gap_allowed: int = (
            int(gap_allowed) if gap_allowed is not None
            else int(slo_data.get("gap_allowed", 0))
        )
        # 因子/财务同步延迟：PENDING_CAL 未配置(None) → 维度关闭，不告警不编造
        self._factor_delay_max: Optional[float] = (
            _none_to_float(factor_sync_delay_max_seconds)
            if factor_sync_delay_max_seconds is not None
            else _none_to_float(slo_data.get("factor_sync_delay_max_seconds"))
        )
        self._financial_delay_max: Optional[float] = (
            _none_to_float(financial_sync_delay_max_seconds)
            if financial_sync_delay_max_seconds is not None
            else _none_to_float(slo_data.get("financial_sync_delay_max_seconds"))
        )

        # 防御式校验（LOCKED 硬约束：freshness 60s / gap 0 必须生效）
        if self._freshness_max <= 0:
            raise ValueError(
                f"freshness_max_seconds 必须 > 0，实际 {self._freshness_max!r}"
            )
        if self._gap_allowed < 0:
            raise ValueError(
                f"gap_allowed 必须 >= 0，实际 {self._gap_allowed!r}"
            )
        for _name, _v in (
            ("factor_sync_delay_max_seconds", self._factor_delay_max),
            ("financial_sync_delay_max_seconds", self._financial_delay_max),
        ):
            if _v is not None and _v <= 0:
                raise ValueError(f"{_name} 必须 > 0 或 None，实际 {_v!r}")

        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager = alert_manager
        self._metrics_collector = metrics_collector

        # 数据源可注入；缺省=空快照（无数据场景防御）
        self._latest_provider: Callable[[], Dict[str, float]] = (
            latest_timestamp_provider if latest_timestamp_provider is not None
            else lambda: {}
        )
        self._gap_provider: Callable[[], Dict[str, int]] = (
            gap_provider if gap_provider is not None else lambda: {}
        )

        # 内部状态（全部为"已发生"事实）
        self._daily_gaps: Dict[str, int] = {}
        self._factor_sync_last_ts: Optional[float] = None
        self._financial_sync_last_ts: Optional[float] = None
        self._handler_ids: List[str] = []
        logger.debug(
            "DataFreshnessSli 初始化: freshness_max=%.0fs gap_allowed=%d "
            "factor_delay_max=%r financial_delay_max=%r",
            self._freshness_max, self._gap_allowed,
            self._factor_delay_max, self._financial_delay_max,
        )

    # ── 事件/周期登记入口 ─────────────────────────────────────
    def record_gap(self, date: str, count: int) -> None:
        """
        登记某一交易日的数据缺口数量（缺口口径=应到数据点缺失）。

        Parameters
        ----------
        date : str
            交易日，ISO "YYYY-MM-DD"（字典序即时间序）。
        count : int
            当日缺口数量（>=0）。

        Raises
        ------
        ValueError : date 为空或 count<0 时抛出。
        """
        if not date or not isinstance(date, str):
            raise ValueError(f"gap date 必须是非空字符串，实际 {date!r}")
        c = int(count)
        if c < 0:
            raise ValueError(f"gap count 必须 >= 0，实际 {c!r}")
        self._daily_gaps[date] = c

    def record_gaps(self, daily: Dict[str, int]) -> None:
        """批量登记每日缺口（覆盖同日已有值）。"""
        if not isinstance(daily, dict):
            raise ValueError(f"daily gaps 必须是 dict，实际 {type(daily)!r}")
        for d, c in daily.items():
            self.record_gap(str(d), int(c))

    def record_factor_sync(self, ts: Optional[float] = None) -> None:
        """登记一次因子同步任务完成时刻（事件入口；防未来：仅记录已发生时刻）。"""
        self._factor_sync_last_ts = float(self._clock() if ts is None else ts)

    def record_financial_sync(self, ts: Optional[float] = None) -> None:
        """登记一次财务同步任务完成时刻（事件入口；防未来：仅记录已发生时刻）。"""
        self._financial_sync_last_ts = float(self._clock() if ts is None else ts)

    # ── 事件总线：数据到达即触发一次评估 ──────────────────────
    def attach_event_bus(self, bus: Any) -> None:
        """
        订阅 EventBus 数据到达事件（DATA_LOADED），到达即 poll() 一次（事件驱动）。

        Parameters
        ----------
        bus : EventBus
            事件总线。订阅失败记 warning，不阻断（poll() 仍可被周期巡检调用）。
        """
        if bus is None:
            return
        try:
            from myquant.core.event_bus import EventType

            self._handler_ids.append(
                bus.subscribe(EventType.DATA_LOADED, self._on_data_arrived)
            )
            logger.info("DataFreshnessSli 已订阅 EventBus DATA_LOADED")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DataFreshnessSli 订阅 EventBus 失败（poll 仍可用）: {e}")

    def _on_data_arrived(self, event: Any) -> None:
        """数据到达事件回调：触发一次新鲜度/缺口/延迟评估（异常不向上炸断总线）。"""
        try:
            self.poll()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"数据到达事件触发 poll 异常: {e}")

    # ── 纯查询（不告警、不写指标）─────────────────────────────
    def latest_lag_seconds(self) -> Dict[str, float]:
        """返回各 data_key 当前新鲜度滞后秒数（纯查询；防未来：仅截至 now 时刻）。"""
        return self._compute_lag_map(self._clock())

    def daily_gap_stats(self) -> Dict[str, int]:
        """返回每日缺口聚合统计快照（登记值 + gap_provider 当日值合并）。"""
        return self._merged_daily_gaps()

    def current_gap_count(self) -> int:
        """当前评估缺口数 = 最新一个日期键（YYYY-MM-DD 字典序最大）的缺口数量。"""
        return self._latest_gap_count(self._merged_daily_gaps())

    # ── 主入口：周期巡检 / 事件驱动 ───────────────────────────
    def poll(self) -> Dict[str, Any]:
        """
        一轮采集：新鲜度滞后 + 每日缺口 + 因子/财务同步延迟 → 写指标 + 分级告警。

        Returns
        -------
        dict : 当前 SLI 状态快照（各 key 滞后、每日缺口、当前缺口数、各延迟维度值）。
        """
        now = self._clock()

        # 1) 新鲜度滞后（按标的/数据类分别）
        lag_map = self._compute_lag_map(now)
        max_lag = 0.0
        for key, lag in lag_map.items():
            self._write_gauge(
                METRIC_FRESHNESS_LAG_SECONDS, lag, labels={"data_key": key}
            )
            max_lag = max(max_lag, lag)
            self._evaluate_freshness(key, lag)

        # 2) 每日数据缺口
        daily = self._merged_daily_gaps()
        gap_count = self._latest_gap_count(daily)
        self._write_gauge(METRIC_DATA_GAP_COUNT, float(gap_count))
        self._evaluate_gap(gap_count)

        # 3) 因子/财务同步延迟（未配置维度跳过，不告警不编造）
        factor_delay = self._evaluate_sync(
            "factor", now, self._factor_delay_max, self._factor_sync_last_ts
        )
        financial_delay = self._evaluate_sync(
            "financial", now, self._financial_delay_max, self._financial_sync_last_ts
        )

        return {
            "freshness_max_seconds": self._freshness_max,
            "gap_allowed": self._gap_allowed,
            "latest_lag_seconds": lag_map,
            "max_lag_seconds": round(max_lag, 3),
            "daily_gap_stats": daily,
            "current_gap_count": gap_count,
            "factor_sync_delay_seconds": factor_delay,
            "financial_sync_delay_seconds": financial_delay,
        }

    #: collect() 与 poll() 等价（事件/周期两种触发语义共用一轮采集）
    collect = poll

    def get_status(self) -> Dict[str, Any]:
        """返回监控状态快照（供巡检报告/看板暴露；纯查询不告警）。"""
        now = self._clock()
        daily = self._merged_daily_gaps()
        return {
            "freshness_max_seconds": self._freshness_max,
            "gap_allowed": self._gap_allowed,
            "factor_sync_delay_max_seconds": self._factor_delay_max,
            "financial_sync_delay_max_seconds": self._financial_delay_max,
            "latest_lag_seconds": self._compute_lag_map(now),
            "daily_gap_stats": daily,
            "current_gap_count": self._latest_gap_count(daily),
        }

    # ── 内部计算 ─────────────────────────────────────────────
    def _compute_lag_map(self, now: float) -> Dict[str, float]:
        """
        计算各 data_key 新鲜度滞后。

        防未来：滞后 = max(0, now - 最新已到达时间戳)；未来时间戳（时钟偏差）截断为 0，
        本行使用的数据截至 now 时刻（只消费已到达数据）。
        """
        try:
            raw = self._latest_provider() or {}
        except Exception as e:  # noqa: BLE001
            logger.warning("latest_timestamp_provider 读取失败（按无数据处理）: %s", e)
            return {}
        result: Dict[str, float] = {}
        for key, ts in raw.items():
            try:
                t = float(ts)
            except (TypeError, ValueError):
                logger.warning("跳过非法时间戳 data_key=%r ts=%r", key, ts)
                continue
            lag = now - t
            if lag < 0:  # 防未来：未来时间戳不计负滞后
                lag = 0.0
            result[str(key)] = lag
        return result

    def _merged_daily_gaps(self) -> Dict[str, int]:
        """合并登记缺口与 gap_provider 当日缺口（同日期相加）。"""
        merged: Dict[str, int] = dict(self._daily_gaps)
        try:
            for d, c in (self._gap_provider() or {}).items():
                k = str(d)
                merged[k] = merged.get(k, 0) + int(c)
        except Exception as e:  # noqa: BLE001
            logger.warning("gap_provider 读取失败（仅用已登记缺口）: %s", e)
        return merged

    @staticmethod
    def _latest_gap_count(daily: Dict[str, int]) -> int:
        """当前评估缺口 = 最新日期键（YYYY-MM-DD 字典序最大）的缺口数量；无数据=0。"""
        if not daily:
            return 0
        latest_date = max(daily.keys())
        return int(daily[latest_date])

    # ── 评估与告警 ───────────────────────────────────────────
    def _evaluate_freshness(self, key: str, lag: float) -> None:
        """新鲜度超阈分级告警：>2× 上限 ERROR(P1)，否则 WARNING(P2)。"""
        if lag <= self._freshness_max:
            return
        level = (
            SeverityLevel.ERROR
            if lag > self._freshness_max * FRESHNESS_ERROR_MULTIPLE
            else SeverityLevel.WARNING
        )
        self._alert(
            level=level,
            message=(
                f"[data-freshness] {key} 数据滞后 {lag:.0f}s 超过上限 "
                f"{self._freshness_max:.0f}s"
            ),
            details={
                "data_key": key,
                "lag_seconds": round(lag, 3),
                "freshness_max_seconds": self._freshness_max,
            },
            dedup_key=f"freshness:{key}",
        )

    def _evaluate_gap(self, gap_count: int) -> None:
        """缺口 > gap_allowed（LOCKED=0）→ ERROR(P1)：任何缺口即硬约束被破坏。"""
        if gap_count <= self._gap_allowed:
            return
        self._alert(
            level=SeverityLevel.ERROR,
            message=(
                f"[data-gap] 数据缺口 {gap_count} 超过允许值 {self._gap_allowed}"
                f"（缺口=0 为 LOCKED 硬约束）"
            ),
            details={
                "gap_count": gap_count,
                "gap_allowed": self._gap_allowed,
            },
            dedup_key="data_gap:exceeded",
        )

    def _evaluate_sync(
        self,
        kind: str,
        now: float,
        max_delay: Optional[float],
        last_ts: Optional[float],
    ) -> Optional[float]:
        """
        因子/财务同步延迟评估。

        max_delay=None（PENDING_CAL 未配置）→ 维度关闭，不告警、不编造数值。
        last_ts=None（无同步记录）→ 防御跳过。延迟超配置值 → WARNING(P2) 告警。
        """
        if max_delay is None:
            return None  # PENDING_CAL：待实盘校准，不告警不编造
        if last_ts is None:
            return None  # 无同步记录，防御不编造
        delay = now - last_ts
        if delay < 0:  # 防未来
            delay = 0.0
        metric = (
            METRIC_FACTOR_SYNC_DELAY_SECONDS if kind == "factor"
            else METRIC_FINANCIAL_SYNC_DELAY_SECONDS
        )
        self._write_gauge(metric, delay)
        if delay > max_delay:
            self._alert(
                level=SeverityLevel.WARNING,
                message=(
                    f"[data-sync] {kind} 同步延迟 {delay:.0f}s 超过上限 {max_delay:.0f}s"
                ),
                details={
                    "kind": kind,
                    "delay_seconds": round(delay, 3),
                    "max_delay_seconds": max_delay,
                },
                dedup_key=f"{kind}_sync:delay",
            )
        return delay

    # ── 指标/告警基础设施（懒加载，采集器/管理器不可用仅记 warning 不阻断）──
    def _write_gauge(
        self, name: str, value: float, labels: Optional[Dict[str, str]] = None
    ) -> None:
        """set_gauge 写指标（进 /api/metrics）；失败记 warning 不阻断检测。"""
        col = self._metrics_collector
        if col is None:
            try:
                from myquant.monitoring.metrics_collector import get_metrics_collector
                col = get_metrics_collector()
                self._metrics_collector = col
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取 metrics_collector 失败（指标 {name} 未写入）: {e}")
                return
        try:
            col.set_gauge(name, float(value), labels=labels)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"指标 {name} 写入失败（不阻断检测）: {e}")

    def _alert(
        self,
        level: SeverityLevel,
        message: str,
        details: Dict[str, Any],
        dedup_key: str,
    ) -> None:
        """经 get_alert_manager().alert 发告警（category=data_quality）；失败记 warning 不阻断。"""
        mgr = self._alert_manager
        if mgr is None:
            try:
                from myquant.monitoring.alerting import get_alert_manager
                mgr = get_alert_manager()
                self._alert_manager = mgr
            except Exception as e:  # noqa: BLE001
                logger.warning(f"获取 alert_manager 失败（告警未发送: {message}）: {e}")
                return
        try:
            mgr.alert(
                level=level,
                message=message,
                source=SLI_SOURCE_DATA_FRESHNESS,
                category=SLI_CATEGORY_DATA_QUALITY,
                details=details,
                deduplication_key=dedup_key,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"告警发送失败（不阻断检测）: {e}")

    def __repr__(self) -> str:
        return (
            f"DataFreshnessSli(freshness_max={self._freshness_max!r}, "
            f"gap_allowed={self._gap_allowed!r}, "
            f"factor_delay_max={self._factor_delay_max!r}, "
            f"financial_delay_max={self._financial_delay_max!r})"
        )


# ═══════════════════════════════════════════════
#  __main__ 自测 demo（注入固定时钟/假告警/假采集器）
# ═══════════════════════════════════════════════
def _run_freshness_self_test() -> int:
    """模块自测 demo：验证新鲜度分级告警/缺口硬约束/同步延迟分维/防未来。"""
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
            self.gauges[(name, tuple(sorted((labels or {}).items())))] = value

    clock_now = [1000.0]

    def clock() -> float:
        return clock_now[0]

    am = _FakeAlert()
    met = _FakeMetrics()

    # 1) 新鲜数据（lag=10 < 60）→ 不告警，指标=10
    sli = DataFreshnessSli(
        latest_timestamp_provider=lambda: {"SH600000:1d": 990.0},
        alert_manager=am, metrics_collector=met, clock=clock,
    )
    st = sli.poll()
    check("1) 新鲜数据不告警", am.calls == [], f"calls={am.calls}")
    check("1) lag 指标=10", st["latest_lag_seconds"]["SH600000:1d"] == 10.0, f"st={st}")

    # 2) 过期数据（lag=100，介于 60~120）→ WARNING
    am.calls.clear()
    sli2 = DataFreshnessSli(
        latest_timestamp_provider=lambda: {"SH600000:1d": 900.0},
        alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
    )
    sli2.poll()
    w = [c for c in am.calls if c["deduplication_key"] == "freshness:SH600000:1d"]
    check("2) 过期触发 WARNING", len(w) == 1 and w[0]["level"].name == "WARNING", f"calls={am.calls}")

    # 3) 严重过期（lag=200 > 120=2×60）→ ERROR
    am.calls.clear()
    sli3 = DataFreshnessSli(
        latest_timestamp_provider=lambda: {"SH600000:1d": 800.0},
        alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
    )
    sli3.poll()
    e = [c for c in am.calls if c["deduplication_key"] == "freshness:SH600000:1d"]
    check("3) 严重过期触发 ERROR", len(e) == 1 and e[0]["level"].name == "ERROR", f"calls={am.calls}")

    # 4) 缺口>0（gap_allowed=0 LOCKED）→ ERROR(P1)
    am.calls.clear()
    sli4 = DataFreshnessSli(
        alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
    )
    sli4.record_gap("2026-10-06", 2)
    sli4.poll()
    g = [c for c in am.calls if c["deduplication_key"] == "data_gap:exceeded"]
    check("4) 缺口>0 触发 ERROR(P1)", len(g) == 1 and g[0]["level"].name == "ERROR", f"calls={am.calls}")

    # 5) 每日缺口统计正确（最新日期键=2026-10-06 → 当前缺口=2）
    check("5) 每日缺口聚合正确", sli4.daily_gap_stats() == {"2026-10-06": 2}, f"st={sli4.daily_gap_stats()}")
    check("5) 当前缺口=2", sli4.current_gap_count() == 2, "")

    # 6) 因子/财务延迟未配置(None) → 不告警不编造
    am.calls.clear()
    sli6 = DataFreshnessSli(
        alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
    )
    sli6.record_factor_sync(ts=0.0)       # 很久之前，但阈值未配置
    sli6.record_financial_sync(ts=0.0)
    st6 = sli6.poll()
    check("6) 未配置延迟维度不告警", am.calls == [], f"calls={am.calls}")
    check("6) 未配置维度不编造数值", st6["factor_sync_delay_seconds"] is None, f"st={st6}")

    # 7) 注入配置 factor=10s，延迟=100s → WARNING 告警 + 指标写入
    am.calls.clear()
    met7 = _FakeMetrics()
    sli7 = DataFreshnessSli(
        factor_sync_delay_max_seconds=10.0,
        alert_manager=am, metrics_collector=met7, clock=clock,
    )
    sli7.record_factor_sync(ts=900.0)   # clock=1000 → delay=100 > 10
    st7 = sli7.poll()
    fs = [c for c in am.calls if c["deduplication_key"] == "factor_sync:delay"]
    check("7) 因子延迟超阈触发 WARNING", len(fs) == 1, f"calls={am.calls}")
    check("7) 因子延迟指标=100", st7["factor_sync_delay_seconds"] == 100.0, f"st={st7}")

    # 8) 防未来：最新时间戳在未来（ts=1100 > now=1000）→ lag 截断 0，不告警
    am.calls.clear()
    sli8 = DataFreshnessSli(
        latest_timestamp_provider=lambda: {"SH600000:1d": 1100.0},
        alert_manager=am, metrics_collector=_FakeMetrics(), clock=clock,
    )
    st8 = sli8.poll()
    check("8) 未来时间戳 lag 截断 0", st8["latest_lag_seconds"]["SH600000:1d"] == 0.0, f"st={st8}")
    check("8) 未来时间戳不误告警", am.calls == [], f"calls={am.calls}")

    # 9) 防御式非法阈值抛错
    raised = False
    try:
        DataFreshnessSli(freshness_max_seconds=0, alert_manager=_FakeAlert())
    except ValueError:
        raised = True
    check("9) 非法 freshness_max 抛 ValueError", raised, "")

    print("=" * 60)
    print(f"data_quality_explorer.DataFreshnessSli 自测: {passed} 项通过")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"失败 {len(failures)} 项")
        return 1
    print("全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_freshness_self_test())
