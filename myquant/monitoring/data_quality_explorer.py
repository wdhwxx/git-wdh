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

from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger

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
