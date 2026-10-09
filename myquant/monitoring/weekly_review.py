# 文件名：myquant/monitoring/weekly_review.py
"""
weekly_review.py - 周告警复盘报告生成（§7 全局治理与运营机制 · R-01）

功能
====
- 按周窗口从 AlertStore 统计 TP/FP/FN 与准确率/误报率/检出率（复用
  alerting.weekly_alert_stats，与 P0-8 同口径，R-01 唯一测量依据）。
- Top 告警：周窗口内按 (category, message) 分组计数，输出 Top N 高频告警。
- 容量趋势：周窗口内读取 C-8 容量类指标（metrics_history），输出样本数/
  最新值/首末趋势；无数据时诚实标注 no_data，不编造。
- 报告落盘：Markdown + JSON 双格式，输出目录默认 项目根/outputs/weekly_reports/。

纪律
====
1. 阈值/口径单一真源 R-04/R-01：达标判定与统计公式全部复用 weekly_alert_stats，
   本模块不重新定义阈值。
2. 严格防未来：week_end 钳制到 <= now；只消费 created_at <= now 的告警与
   timestamp <= now 的指标点。
3. 样本不足诚实标注：比率无法计算时为 None（不造数），达标判定 False。
4. 业务逻辑全在本模块，scripts/ 只做薄入口。

设计原则（对齐既有 monitoring 模块）：
- 全量类型注解 + Google 风格 docstring + 防御式校验 + logging。
- 异常处理：只捕获可合理处理的异常，失败记日志不阻断（报告生成尽力而为）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from myquant.core.alert_store import AlertStore
from myquant.core.registry import register_component
from myquant.monitoring.alerting import weekly_alert_stats

logger = logging.getLogger("monitoring.weekly_review")

#: 周窗口长度（秒）：7 天
WEEK_SECONDS: float = 7.0 * 24.0 * 3600.0

#: 周复盘告警历史最大加载条数（Top 告警统计用，倒序取最近）
WEEKLY_LOAD_LIMIT: int = 20000

#: Top 告警默认条数
DEFAULT_TOP_N: int = 10

#: 防未来容差（秒）：时间戳晚于 now+此值视为未来数据，跳过
_FUTURE_EPS: float = 1.0

#: 报告输出目录（相对项目根）
DEFAULT_REPORT_SUBDIR: Tuple[str, ...] = ("outputs", "weekly_reports")

#: 报告前缀
DEFAULT_REPORT_PREFIX: str = "监控周复盘"

#: C-8 容量类指标名（与 monitoring/capacity.py METRIC_* 常量一致；缺失时标注 no_data）
DEFAULT_CAPACITY_METRIC_NAMES: Tuple[str, ...] = (
    "capacity_data_size_growth",
    "capacity_estimated_days_remaining",
    "capacity_strategy_count",
    "capacity_backtest_peak_memory_mb",
    "capacity_data_size_current_mb",
    "capacity_storage_headroom_pct",
)


def _now(clock: Optional[Callable[[], float]]) -> float:
    """取当前时间戳（可注入 clock 便于测试）。"""
    return (clock if clock is not None else time.time)()


class WeeklyReviewReport:
    """周告警复盘报告生成器（§7 每周动作 · R-01 自动统计）。

    输入：AlertStore（告警持久化，必填）；MetricsHistoryStore（容量趋势，可选）。
    输出：结构化统计 dict + Markdown 文本 + 双格式落盘。
    """

    def __init__(
        self,
        alert_store: Any,
        metrics_store: Any = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """初始化周复盘报告生成器。

        Parameters
        ----------
        alert_store : AlertStore | Any
            告警持久化存储（需具备 load_history(limit) 接口；为 None 抛 ValueError）。
        metrics_store : MetricsHistoryStore | Any | None
            指标时序存储（可选；需具备 query(name, start_ts, end_ts) 接口）。
            为 None 时容量趋势输出 no_data（诚实标注，不编造）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。

        Raises
        ------
        ValueError : alert_store 为 None 时抛出。
        """
        if alert_store is None:
            raise ValueError("weekly_review: alert_store 不能为 None")
        self._alert_store: Any = alert_store
        self._metrics_store: Any = metrics_store
        self._clock: Callable[[], float] = (
            clock if clock is not None else time.time
        )

    # ─────────────────────────────────────────────
    # 生成
    # ─────────────────────────────────────────────
    def generate(
        self,
        week_end: Optional[float] = None,
        top_n: int = DEFAULT_TOP_N,
        capacity_metric_names: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """生成周复盘统计报告。

        Parameters
        ----------
        week_end : float | None
            周窗口右端（epoch 秒）；None 取当前时钟。防未来：钳制到 <= now。
        top_n : int
            Top 告警条数（>0，默认 10）。
        capacity_metric_names : list[str] | None
            容量类指标名清单；None 用默认 C-8 指标名。为空列表表示不统计容量。

        Returns
        -------
        dict : 周复盘报告（week_start/week_end/tp/fp/fn/auto/unclassified/
            准确率/误报率/检出率/达标/top_alerts/capacity_trends/口径注释）。

        Raises
        ------
        ValueError : top_n 非法时抛出。
        """
        if top_n <= 0:
            raise ValueError(f"top_n 必须 >0，收到 {top_n}")

        now = self._clock()
        end = float(week_end) if week_end is not None else now
        # 防未来：周右端不得晚于当前时钟
        end = min(end, now)
        start = end - WEEK_SECONDS

        # 1) TP/FP/FN + 达标判定：复用 P0-8 同口径（R-01，不重新定义公式/阈值）
        try:
            stats: Dict[str, Any] = weekly_alert_stats(
                self._alert_store,
                week_end=end,
                fi_records=None,
                clock=self._clock,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("周统计失败（按空统计兜底，不阻断报告）: %s", exc)
            stats = {
                "week_start": round(start, 3),
                "week_end": round(end, 3),
                "tp": 0,
                "fp": 0,
                "fn": 0,
                "auto": 0,
                "unclassified": 0,
                "accuracy": None,
                "false_positive_rate": None,
                "detection_rate": None,
                "passed": False,
                "note": "周统计计算失败，已兜底为空统计",
            }

        # 2) Top 告警：周窗口内按 (category, message) 分组计数
        top_alerts: List[Dict[str, Any]] = self._build_top_alerts(start, end, top_n)

        # 3) 容量趋势：周窗口内容量类指标（可选；缺失标注 no_data 不编造）
        capacity_trends: List[Dict[str, Any]] = self._build_capacity_trends(
            start, end, capacity_metric_names
        )

        report: Dict[str, Any] = {
            "report_type": "weekly_review",
            "generated_at": round(now, 3),
            "week_start": round(start, 3),
            "week_end": round(end, 3),
            "tp": stats.get("tp", 0),
            "fp": stats.get("fp", 0),
            "fn": stats.get("fn", 0),
            "auto": stats.get("auto", 0),
            "unclassified": stats.get("unclassified", 0),
            "accuracy": stats.get("accuracy"),
            "false_positive_rate": stats.get("false_positive_rate"),
            "detection_rate": stats.get("detection_rate"),
            "accuracy_target": stats.get("accuracy_target"),
            "false_positive_rate_target": stats.get("false_positive_rate_target"),
            "passed": stats.get("passed", False),
            "top_alerts": top_alerts,
            "capacity_trends": capacity_trends,
            "note": stats.get("note", ""),
            "formula": {
                "accuracy": "TP/(TP+FP)",
                "false_positive_rate": "FP/(TP+FP)",
                "detection_rate": "TP/(TP+FN)",
            },
        }
        return report

    # ─────────────────────────────────────────────
    # 子统计
    # ─────────────────────────────────────────────
    def _build_top_alerts(
        self, start: float, end: float, top_n: int
    ) -> List[Dict[str, Any]]:
        """周窗口内按 (category, message) 分组统计 Top N 高频告警。

        防未来：只消费 created_at <= now + 容差的告警行；窗口外行跳过。

        Parameters
        ----------
        start / end : float
            周窗口闭区间（epoch 秒）。
        top_n : int
            返回条数上限。

        Returns
        -------
        list[dict] : [{rank/category/message/count/levels}] 按计数降序。
        """
        try:
            rows: List[Dict[str, Any]] = self._alert_store.load_history(
                limit=WEEKLY_LOAD_LIMIT
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Top 告警加载失败（返回空）: %s", exc)
            return []

        now = self._clock()
        groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in rows:
            created = row.get("created_at")
            if created is None:
                continue
            try:
                ts = float(created)
            except (TypeError, ValueError):
                continue
            if ts > now + _FUTURE_EPS:
                continue  # 防未来
            if ts < start or ts > end:
                continue
            category = str(row.get("category") or "unknown")
            message = str(row.get("message") or "")
            key = (category, message)
            entry = groups.get(key)
            if entry is None:
                entry = {"category": category, "message": message, "count": 0, "levels": set()}
                groups[key] = entry
            entry["count"] += 1
            level = str(row.get("level") or "")
            if level:
                entry["levels"].add(level)

        ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["category"]))
        result: List[Dict[str, Any]] = []
        for idx, g in enumerate(ordered[:top_n], start=1):
            result.append(
                {
                    "rank": idx,
                    "category": g["category"],
                    "message": g["message"][:200],
                    "count": g["count"],
                    "levels": sorted(g["levels"]),
                }
            )
        return result

    def _build_capacity_trends(
        self,
        start: float,
        end: float,
        capacity_metric_names: Optional[Sequence[str]],
    ) -> List[Dict[str, Any]]:
        """周窗口内容量类指标趋势（可选能力；无数据源/无数据时诚实标注）。

        Parameters
        ----------
        start / end : float
            周窗口闭区间（epoch 秒）。
        capacity_metric_names : list[str] | None
            指标名清单；None 用默认 C-8 指标名；空列表表示不统计。

        Returns
        -------
        list[dict] : [{name/samples/first_value/last_value/trend}]；
            无数据时 {name, samples:0, note:"no_data"}。
        """
        if self._metrics_store is None:
            return []
        names = (
            list(DEFAULT_CAPACITY_METRIC_NAMES)
            if capacity_metric_names is None
            else list(capacity_metric_names)
        )
        if not names:
            return []

        now = self._clock()
        trends: List[Dict[str, Any]] = []
        for name in names:
            try:
                points: List[Dict[str, Any]] = self._metrics_store.query(
                    name=name,
                    start_ts=start,
                    end_ts=end,
                    limit=WEEKLY_LOAD_LIMIT,
                    order="asc",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("容量指标 %s 查询失败（标注 no_data）: %s", name, exc)
                points = []
            # 防未来：只保留已发生时间戳的指标点
            valid = [p for p in points if float(p.get("timestamp", 0.0)) <= now + _FUTURE_EPS]
            if not valid:
                trends.append({"name": name, "samples": 0, "note": "no_data"})
                continue
            first = float(valid[0].get("value", 0.0))
            last = float(valid[-1].get("value", 0.0))
            trend: str = "flat"
            if last > first:
                trend = "up"
            elif last < first:
                trend = "down"
            trends.append(
                {
                    "name": name,
                    "samples": len(valid),
                    "first_value": round(first, 6),
                    "last_value": round(last, 6),
                    "trend": trend,
                }
            )
        return trends

    # ─────────────────────────────────────────────
    # 输出
    # ─────────────────────────────────────────────
    def to_markdown(self, report: Dict[str, Any]) -> str:
        """将周复盘报告转成可读 Markdown（含口径注释与达标判定）。"""
        def _fmt(value: Any) -> str:
            if value is None:
                return "—（样本不足）"
            if isinstance(value, float):
                return f"{value:.4f}"
            return str(value)

        lines: List[str] = [
            "# 监控周复盘报告（§7 · R-01 自动统计）",
            "",
            f"- 统计窗口：{_fmt(report.get('week_start'))} ~ {_fmt(report.get('week_end'))}（epoch 秒）",
            f"- 生成时间：{_fmt(report.get('generated_at'))}",
            "",
            "## 一、告警准确率统计（P0-8 口径）",
            "",
            "| 指标 | 值 | 目标 | 达标 |",
            "|---|---|---|---|",
            f"| TP（真告警） | {report.get('tp', 0)} | — | — |",
            f"| FP（误报） | {report.get('fp', 0)} | — | — |",
            f"| FN（漏报，FI 主动测量） | {report.get('fn', 0)} | — | — |",
            f"| AUTO（自动分类） | {report.get('auto', 0)} | — | — |",
            f"| 未分类 | {report.get('unclassified', 0)} | — | — |",
            f"| 准确率 = TP/(TP+FP) | {_fmt(report.get('accuracy'))} | >95% | {'✅' if report.get('passed') else '❌/待判定'} |",
            f"| 误报率 = FP/(TP+FP) | {_fmt(report.get('false_positive_rate'))} | <5% | — |",
            f"| 检出率 = TP/(TP+FN) | {_fmt(report.get('detection_rate'))} | — | — |",
            "",
            f"> 达标判定：准确率 >95% 且误报率 <5%（样本不足时诚实判 False）。{report.get('note', '')}",
            "",
        ]

        top = report.get("top_alerts") or []
        lines.append("## 二、Top 告警（按 category+message 分组）")
        lines.append("")
        if not top:
            lines.append("（本周窗口内无告警或无数据）")
        else:
            lines.append("| # | 类别 | 计数 | 级别 | 告警内容 |")
            lines.append("|---|---|---|---|---|")
            for item in top:
                lines.append(
                    f"| {item['rank']} | {item['category']} | {item['count']} | "
                    f"{','.join(item['levels']) or '—'} | {item['message'][:80]} |"
                )
        lines.append("")

        caps = report.get("capacity_trends") or []
        lines.append("## 三、容量趋势（C-8 指标，可选）")
        lines.append("")
        if not caps:
            lines.append("（未配置容量数据源或未统计）")
        else:
            lines.append("| 指标 | 样本数 | 首值 | 末值 | 趋势 |")
            lines.append("|---|---|---|---|---|")
            for item in caps:
                if item.get("samples", 0) == 0:
                    lines.append(f"| {item['name']} | 0 | — | — | no_data |")
                else:
                    lines.append(
                        f"| {item['name']} | {item['samples']} | {item['first_value']} | "
                        f"{item['last_value']} | {item['trend']} |"
                    )
        lines.append("")
        lines.append("---")
        lines.append("> 口径：R-01 唯一测量依据，公式见 report['formula']；严格防未来，只消费已发生时间戳。")
        return "\n".join(lines)

    def save(
        self,
        report: Dict[str, Any],
        output_dir: Optional[Path] = None,
        prefix: str = DEFAULT_REPORT_PREFIX,
    ) -> Path:
        """周复盘报告落盘（Markdown + JSON 双格式，同前缀同时间戳）。

        Parameters
        ----------
        report : dict
            generate() 返回的周复盘报告。
        output_dir : Path | None
            输出目录；None 默认 项目根/outputs/weekly_reports/（自动创建）。
        prefix : str
            文件名前缀（默认"监控周复盘"）。

        Returns
        -------
        Path : 落盘的 Markdown 文件路径（JSON 同目录同前缀）。

        Raises
        ------
        ValueError : report 为空或 prefix 为空时抛出。
        """
        if not report:
            raise ValueError("weekly_review: report 不能为空")
        if not prefix:
            raise ValueError("weekly_review: prefix 不能为空")
        if output_dir is None:
            output_dir = (
                Path(__file__).resolve().parent.parent.parent / Path(*DEFAULT_REPORT_SUBDIR)
            )
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("输出目录创建失败（报告不落盘）: %s", exc)
            raise

        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(report.get("generated_at", time.time())))
        md_path = output_dir / f"{prefix}_{stamp}.md"
        json_path = output_dir / f"{prefix}_{stamp}.json"
        md_path.write_text(self.to_markdown(report), encoding="utf-8")
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("周复盘报告已落盘: %s / %s", md_path, json_path)
        return md_path


def _run_self_test() -> int:
    """__main__ 自测：临时库造数据 → 生成 → 校验关键断言。"""
    import tempfile
    from myquant.core.constants import SeverityLevel
    from myquant.database.connection import DatabaseConnection
    from myquant.monitoring.alerting import Alert

    checks: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    with tempfile.TemporaryDirectory() as tmp:
        conn = DatabaseConnection(Path(tmp) / "weekly_review.db")
        store = AlertStore(connection=conn)
        now = time.time()
        # 本周窗口内：19 TP + 1 FP（classification 打标），另 1 条窗口外
        for i in range(19):
            a = Alert(
                level=SeverityLevel.WARNING,
                message=f"tp-{i}",
                category="system",
                alert_id=f"wr-tp-{i}",
                timestamp=now - 3600.0,
                classification="TP",
            )
            store.save_alert(a)
            store.set_classification(f"wr-tp-{i}", "TP")
        fp_alert = Alert(
            level=SeverityLevel.WARNING,
            message="fp-one",
            category="data_quality",
            alert_id="wr-fp-1",
            timestamp=now - 1800.0,
            classification="FP",
        )
        store.save_alert(fp_alert)
        store.set_classification("wr-fp-1", "FP")
        outside = Alert(
            level=SeverityLevel.INFO,
            message="outside-week",
            category="system",
            alert_id="wr-out",
            timestamp=now - WEEK_SECONDS - 100.0,
        )
        store.save_alert(outside)

        report = WeeklyReviewReport(store).generate(week_end=now)
        check("TP=19 / FP=1 / 窗口外不计入", report["tp"] == 19 and report["fp"] == 1 and report["unclassified"] == 0)
        check("准确率=19/20=0.95（边界>0.95 不达标）", report["accuracy"] == 0.95 and report["passed"] is False)
        check("Top 告警含 data_quality 分组", any(t["category"] == "data_quality" for t in report["top_alerts"]))
        check("Top 告警按计数降序", all(
            report["top_alerts"][i]["count"] >= report["top_alerts"][i + 1]["count"]
            for i in range(len(report["top_alerts"]) - 1)
        ))
        md = WeeklyReviewReport(store).to_markdown(report)
        check("Markdown 含关键节", "## 一、告警准确率统计" in md and "## 二、Top 告警" in md)

        # 容量趋势：无 metrics_store → 空；metrics_store 无数据 → no_data
        report2 = WeeklyReviewReport(store).generate(week_end=now)
        check("无指标库时容量趋势为空", report2["capacity_trends"] == [])

        # 防未来：week_end 晚于 now 被钳制（钳制语义=远小于入参；round 进位毫秒级不计）
        report3 = WeeklyReviewReport(store).generate(week_end=now + 99999.0)
        check("防未来：week_end 钳制（远小于入参）", report3["week_end"] < now + 9999.0)

        # 防御：alert_store=None 拒绝
        bad = False
        try:
            WeeklyReviewReport(None)  # type: ignore[arg-type]
        except ValueError:
            bad = True
        check("防御：alert_store=None 拒绝", bad)
        # 防御：top_n 非法拒绝
        bad2 = False
        try:
            WeeklyReviewReport(store).generate(top_n=0)
        except ValueError:
            bad2 = True
        check("防御：top_n=0 拒绝", bad2)
        # save 落盘
        out = Path(tmp) / "reports"
        md_path = WeeklyReviewReport(store).save(report, output_dir=out)
        check("save 落盘 Markdown", md_path.exists() and md_path.suffix == ".md")
        json_path = out / (md_path.stem + ".json")
        check("save 落盘 JSON 同前缀", json_path.exists())
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    failed = 0
    for name, ok, detail in checks:
        flag = "✅" if ok else "❌"
        print(f"{flag} {name} {detail}")
        if not ok:
            failed += 1
    print(f"weekly_review __main__ 自测: {len(checks) - failed}/{len(checks)} 项全绿")
    return 1 if failed else 0


# ═══════════════════════════════════════════════
#  G2-5 阶段二：装配工厂（注册表化）
#  live/scheduler 经 core.registry.get_component("weekly_review_report") 取用并调用，
#  消除 scheduler → monitoring.weekly_review 反向依赖（EX-13H）。
#  模块导入即注册；装配责任上移（run_实盘入口.py setup_monitoring_linkage 触发）。
# ═══════════════════════════════════════════════

def get_weekly_review_report(
    alert_store: Any,
    metrics_store: Any = None,
    **kwargs: Any,
) -> WeeklyReviewReport:
    """每周告警复盘报告生成器装配工厂（§7 每周动作）。

    Parameters
    ----------
    alert_store : Any
        告警持久化存储（须具备 load_history(limit) 接口；None 抛 ValueError）。
    metrics_store : Any | None
        指标时序存储（可选；None 时容量趋势输出 no_data，诚实标注）。
    **kwargs : Any
        透传 WeeklyReviewReport 构造参数（clock 等）。

    Returns
    -------
    WeeklyReviewReport : 周复盘报告生成器实例。

    Raises
    ------
    ValueError : alert_store 为 None 时抛出。
    """
    return WeeklyReviewReport(alert_store, metrics_store=metrics_store, **kwargs)


#: G2-5 阶段二：装配工厂注册（模块导入即注册；scheduler 经 get_component 取用）
register_component("weekly_review_report", get_weekly_review_report)


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
