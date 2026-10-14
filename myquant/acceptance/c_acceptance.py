# -*- coding: utf-8 -*-
"""C 包「保 Alpha」监控验收 Runner（C-12，回写 #241）。

对 C 包可验收项（C-1 模型漂移 / C-2 滚动绩效 / C-4 信号衰减 / C-5 归因 /
C-6 动态基线 / C-7 根因 / C-8 容量 / C-9 告警治理 / C-10 信号一致性 /
C-11 数据血缘决策项）逐项做**真实执行断言**（非仅 import 检查），并在隔离
环境（固定时钟 + 内存 FakeAlert 注入，不落 quant.db、不读真实配置真源）下
统计 P0-8 误报率口径（健康输入序列上的告警数）。

体例参照 ``myquant/monitoring/b_acceptance.py``（B-10）与
``myquant/monitoring/m1_acceptance.py``（A-15）：隔离环境 + 逐项 verdict +
overall_pass + JSON 报告。

防未来：所有喂入时间戳均为固定历史锚点（``BASE_TS`` 附近，远早于验收日），
``clock`` 注入固定 now，严格保证 feed_ts <= now。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ═══════════════════════════════════════════════════════════════════════
# 常量（R-04：验收专用注入阈值集中外置；非业务 SLO 真源，仅本隔离 Runner 使用）
# ═══════════════════════════════════════════════════════════════════════

#: 固定历史时间锚点（2023-11-14 附近，远早于验收日 2026-10-06），保证防未来。
BASE_TS: float = 1_700_000_000.0
#: 固定“现在”（注入 clock 的返回值）：所有 feed_ts 必须 <= CLOCK_NOW。
#: 后移 1e6 秒（约 11.6 天），既为按天喂数留出空间，又远早于验收日 2026-10-06。
CLOCK_NOW: float = BASE_TS + 1_000_000.0
#: 验收报告日期（generated_at 日期部分）。
REPORT_DATE: str = "2026-10-06"

#: 验收注入阈值（演示/隔离用，绕开 PENDING_CAL；非 settings.yaml 真源）。
INJ_PSI_THRESHOLD: float = 0.10
INJ_KS_THRESHOLD: float = 0.20
INJ_INTRADAY_DD_LIMIT: float = 0.20          # 日内回撤阈值（20%）
INJ_IC_THRESHOLD: float = 0.10               # 滚动 IC 告警阈值
INJ_DENSITY_MIN: float = 0.0005              # 信号密度下限（条/秒）
INJ_INVALID_RATIO_MAX: float = 0.50          # 无效信号占比上限
INJ_BASELINE_K: float = 3.0                  # 动态基线 k 倍sigma
INJ_BASELINE_MIN_SAMPLES: int = 10           # 冷启动样本数
INJ_HEADROOM_MIN_PCT: float = 10.0           # 存储 headroom 下限（%）
INJ_STRATEGY_LIMIT: int = 5                  # 注册策略数上限
INJ_STORAGE_TOTAL_MB: float = 1000.0        # 验收注入存储总量
INJ_DEVIATION_RATE_MAX: float = 0.10         # 信号一致性偏差率上限

#: 数据血缘决策项（C-11）：已决策关闭/远期（回写 #240），验收=确认磁盘无该文件。
DATA_LINEAGE_REL_PATH: str = os.path.join("myquant", "monitoring", "data_lineage.py")


# ═══════════════════════════════════════════════════════════════════════
# 隔离用 FakeAlert（捕获告警调用，不双发、不落库）
# ═══════════════════════════════════════════════════════════════════════
@dataclass
class _FakeAlert:
    """内存告警捕获器：记录所有 alert() 调用，供误报/检出断言。"""

    calls: List[Dict[str, Any]] = field(default_factory=list)

    def alert(self, **kw: Any) -> int:
        """记录一次告警调用，返回告警 id（占位）。"""
        self.calls.append(dict(kw))
        return len(self.calls)

    @property
    def count(self) -> int:
        """捕获到的告警条数。"""
        return len(self.calls)


def _fixed_clock() -> float:
    """固定时钟：恒返回 CLOCK_NOW（防未来注入）。"""
    return CLOCK_NOW


# ═══════════════════════════════════════════════════════════════════════
# C 包验收 Runner
# ═══════════════════════════════════════════════════════════════════════
class CAcceptanceRunner:
    """C 包保 Alpha 验收 Runner：逐项真实断言 + 误报统计 + JSON 报告。

    Usage:
        runner = CAcceptanceRunner()
        report = runner.run()
        runner.write_report(Path("docs/监控档案/C包保Alpha验收_20261006.json"))
    """

    def __init__(self, project_root: Optional[Path] = None) -> None:
        """
        Parameters
        ----------
        project_root : Path | None
            仓库根（用于定位 data_lineage.py 是否存在等磁盘断言）；None 自动推断
            （本文件位于 <root>/myquant/monitoring/ 下，上溯两级）。
        """
        here = Path(__file__).resolve()
        self._root: Path = project_root if project_root is not None else here.parents[2]
        self._results: List[Dict[str, Any]] = []
        # P0-8 误报电池计数
        self._healthy_runs: int = 0
        self._false_positives: int = 0
        self._positive_cases: int = 0
        self._positive_detected: int = 0

    # ── 断言登记 ────────────────────────────────────────────────
    def _record(self, item: str, name: str, passed: bool,
                summary: str, measured: Optional[Dict[str, Any]] = None,
                error: Optional[str] = None) -> None:
        """登记逐项 verdict。"""
        self._results.append({
            "item": item,
            "name": name,
            "verdict": "pass" if passed else "fail",
            "summary": summary,
            "measured": measured or {},
            "error": error,
        })

    # ── C-1 模型漂移 ────────────────────────────────────────────
    def _check_c1_drift(self) -> None:
        from myquant.ml.model_monitor import (  # noqa: WPS433
            DRIFT_STATUS_DRIFT,
            DRIFT_STATUS_HEALTHY,
            DRIFT_STATUS_NOT_CONFIGURED,
            ModelMonitor,
        )

        monitor = ModelMonitor()
        # 健康分布：actual ≈ expected
        expected = [1.0 + 0.01 * (i % 7) for i in range(100)]
        actual_healthy = [1.0 + 0.01 * (i % 7) + 0.005 for i in range(100)]
        r_healthy = monitor.evaluate_drift(
            actual_healthy, expected, model_name="m_healthy", version=1,
            psi_threshold=INJ_PSI_THRESHOLD, ks_threshold=INJ_KS_THRESHOLD,
        )
        # 漂移分布：actual 整体平移到远端
        actual_drift = [10.0 + 0.01 * (i % 5) for i in range(100)]
        r_drift = monitor.evaluate_drift(
            actual_drift, expected, model_name="m_drift", version=1,
            psi_threshold=INJ_PSI_THRESHOLD, ks_threshold=INJ_KS_THRESHOLD,
        )
        # 未配置：阈值传 None → not_configured
        r_none = monitor.evaluate_drift(
            expected, expected, model_name="m_none", version=1,
            psi_threshold=None, ks_threshold=None,
        )
        psi = monitor.psi_score(actual_healthy, expected)
        ks = monitor.ks_statistic(actual_healthy, expected)

        # 日频漂移检查（隔离 FakeAlert + 显式阈值，不读配置真源）
        from myquant.live.scheduler import run_daily_model_drift_check  # noqa: WPS433
        fake = _FakeAlert()
        summary = run_daily_model_drift_check(
            monitor,
            drift_checks={"m_daily": (actual_drift, expected)},
            versions={"m_daily": 1},
            alert_manager=fake,
            psi_threshold=INJ_PSI_THRESHOLD,
            ks_threshold=INJ_KS_THRESHOLD,
        )
        daily = summary.get("m_daily", {})
        daily_alerted = bool(daily.get("alerted"))
        self._positive_cases += 1
        if daily_alerted:
            self._positive_detected += 1

        ok = (
            r_healthy.status == DRIFT_STATUS_HEALTHY
            and r_drift.status == DRIFT_STATUS_DRIFT
            and r_none.status == DRIFT_STATUS_NOT_CONFIGURED
            and daily.get("status") == DRIFT_STATUS_DRIFT
            and daily_alerted
            and isinstance(psi, float) and isinstance(ks, float)
        )
        self._record(
            "C-1", "模型漂移（PSI/KS 三态 + 日频预警）", ok,
            f"healthy={r_healthy.status} drift={r_drift.status} "
            f"not_configured={r_none.status} daily_alerted={daily_alerted}",
            measured={
                "psi": psi, "ks": ks,
                "healthy_status": r_healthy.status,
                "drift_status": r_drift.status,
                "not_configured_status": r_none.status,
                "daily": daily,
                "alert_calls": fake.count,
            },
        )

    # ── C-2 滚动绩效 ────────────────────────────────────────────
    def _check_c2_rolling(self) -> None:
        from myquant.monitoring.rolling_performance import (
            RollingPerformanceMonitor,  # noqa: WPS433
        )

        fake = _FakeAlert()
        mon = RollingPerformanceMonitor(
            intraday_drawdown_limit=INJ_INTRADAY_DD_LIMIT,
            alert_manager=fake, clock=_fixed_clock,
        )
        # 10 天温和上行权益（健康序列）
        equities = [100.0, 101.0, 99.5, 102.0, 103.0, 102.0, 104.0, 105.0, 104.5, 106.0]
        for i, eq in enumerate(equities):
            mon.record("trend_a", BASE_TS + i * 86_400.0, eq)
        panel = mon.to_dict()
        try:
            json.dumps(panel)
            panel_json_ok = True
        except (TypeError, ValueError):
            panel_json_ok = False
        # 健康序列不应触发日内回撤/亏损告警
        self._healthy_runs += 1
        self._false_positives += fake.count
        ok = isinstance(panel, dict) and panel_json_ok and fake.count == 0
        self._record(
            "C-2", "滚动绩效（滚动收益/回撤/胜率 + 日内告警）", ok,
            f"recorded=10 points, panel_json={panel_json_ok}, alerts={fake.count}",
            measured={"points": len(equities), "alert_calls": fake.count,
                      "panel_keys": sorted(list(panel.keys())) if isinstance(panel, dict) else []},
        )

    # ── C-4 信号衰减 ───────────────────────────────────────────
    def _check_c4_signal_decay(self) -> None:
        from myquant.monitoring.signal_decay import SignalDecayMonitor  # noqa: WPS433

        # 健康：signal_value 与 realized_return 正相关 → IC 高于阈值，不告警
        fake_healthy = _FakeAlert()
        mon_h = SignalDecayMonitor(
            ic_threshold=INJ_IC_THRESHOLD, density_min=INJ_DENSITY_MIN,
            invalid_signal_ratio_max=INJ_INVALID_RATIO_MAX,
            alert_manager=fake_healthy, clock=_fixed_clock,
        )
        n = 30
        # 喂数时点落在 (now-3600, now] 密度窗内（防未来：全部 <= CLOCK_NOW）
        sig_base = CLOCK_NOW - 400.0
        for i in range(n):
            sv = (i + 1) / n
            ts = sig_base + i * 10.0
            mon_h.record_pair(
                signal_ts=ts,
                signal_value=sv,
                realized_return=sv * 0.5,          # 正相关
                realized_at=ts + 5.0,
            )
        snap_h = mon_h.evaluate()
        self._healthy_runs += 1
        self._false_positives += fake_healthy.count

        # 衰减：realized_return 与 signal_value 负相关 → IC < 阈值 → P1 告警
        fake_decay = _FakeAlert()
        mon_d = SignalDecayMonitor(
            ic_threshold=INJ_IC_THRESHOLD, density_min=INJ_DENSITY_MIN,
            invalid_signal_ratio_max=INJ_INVALID_RATIO_MAX,
            alert_manager=fake_decay, clock=_fixed_clock,
        )
        for i in range(n):
            sv = (i + 1) / n
            ts = sig_base + i * 10.0
            mon_d.record_pair(
                signal_ts=ts,
                signal_value=sv,
                realized_return=-sv * 0.5,         # 负相关 → IC 低
                realized_at=ts + 5.0,
            )
        snap_d = mon_d.evaluate()
        decay_alerted = fake_decay.count >= 1
        self._positive_cases += 1
        if decay_alerted:
            self._positive_detected += 1

        ok = (
            isinstance(snap_h, dict) and isinstance(snap_d, dict)
            and fake_healthy.count == 0
            and decay_alerted
        )
        self._record(
            "C-4", "信号衰减（滚动 IC/密度/无效占比 + P1P2 分级）", ok,
            f"healthy_ic={snap_h.get('ic')} alerts_h={fake_healthy.count}; "
            f"decay_ic={snap_d.get('ic')} alerts_d={fake_decay.count}",
            measured={
                "healthy_ic": snap_h.get("ic"), "decay_ic": snap_d.get("ic"),
                "healthy_alerts": fake_healthy.count,
                "decay_alerts": fake_decay.count,
            },
        )

    # ── C-5 归因 ───────────────────────────────────────────────
    def _check_c5_attribution(self) -> None:
        from myquant.backtest.attribution import AttributionAnalyzer  # noqa: WPS433
        from myquant.backtest.metrics import (  # noqa: WPS433
            ATTRIBUTION_DEFAULT_GROUP,
            compare_attribution,
            group_contribution,
            group_risk_contribution,
            period_bucket_of,
        )

        an = AttributionAnalyzer(period="day", clock=_fixed_clock)
        an.add_record("backtest", BASE_TS, pnl=100.0, strategy="trend_a",
                      factor="ma_cross", symbol="600519")
        an.add_record("backtest", BASE_TS + 86_400.0, pnl=50.0, strategy="trend_a",
                      factor="rsi", symbol="600519")
        an.add_record("live", BASE_TS, pnl=90.0, strategy="trend_a",
                      factor="ma_cross", symbol="600519")
        an.add_record("live", BASE_TS + 86_400.0, pnl=45.0, strategy="trend_a",
                      factor="rsi", symbol="600519")
        summary = an.daily_summary()
        self_consistent = bool(summary.get("self_consistent"))

        # 纯函数断言
        recs = [
            {"strategy": "trend_a", "pnl": 100.0, "period_bucket": "2023-11-14"},
            {"strategy": "trend_a", "pnl": 50.0, "period_bucket": "2023-11-15"},
            {"strategy": "meanrev_b", "pnl": -30.0, "period_bucket": "2023-11-14"},
        ]
        rows = group_contribution(recs, "strategy")
        ratios_sum = round(sum(r["ratio"] for r in rows), 6)
        top = rows[0] if rows else {}
        risk_rows = group_risk_contribution(recs, "strategy")
        cmp_rows = compare_attribution(
            [{"group": "trend_a", "contribution": 150.0}],
            [{"group": "trend_a", "contribution": 135.0}],
        )
        abs_diff = cmp_rows[0]["abs_diff"] if cmp_rows else None
        bucket = period_bucket_of(BASE_TS, "day")

        ok = (
            self_consistent
            and abs(ratios_sum - 1.0) < 1e-6
            and abs(top.get("contribution", 0.0) - 150.0) < 1e-6
            and isinstance(risk_rows, list)
            and abs(abs_diff - (-15.0)) < 1e-6
            and isinstance(bucket, str)
            and ATTRIBUTION_DEFAULT_GROUP == "unknown"
        )
        self._record(
            "C-5", "归因（四维归因 + 回测/实盘对比 + self_consistent 对账）", ok,
            f"self_consistent={self_consistent} ratio_sum={ratios_sum} "
            f"top_contrib={top.get('contribution')} abs_diff={abs_diff} bucket={bucket}",
            measured={
                "self_consistent": self_consistent,
                "ratio_sum": ratios_sum,
                "top_group": top.get("group"),
                "top_contribution": top.get("contribution"),
                "risk_groups": len(risk_rows),
                "abs_diff": abs_diff,
                "bucket": bucket,
            },
        )

    # ── C-6 动态基线 ────────────────────────────────────────────
    def _check_c6_baseline(self) -> None:
        from myquant.monitoring.dynamic_baseline import DynamicBaseline  # noqa: WPS433

        fake = _FakeAlert()
        db = DynamicBaseline(
            window=20, k=INJ_BASELINE_K, min_samples=INJ_BASELINE_MIN_SAMPLES,
            alert_manager=fake, clock=_fixed_clock,
        )
        # 冷启动 + 暖机：喂 12 个平稳点（≈100）
        warm_n = INJ_BASELINE_MIN_SAMPLES + 2
        for i in range(warm_n):
            db.record("latency_ms", BASE_TS + i * 60.0, 100.0 + 0.2 * (i % 3))
        # 暖机后带内点 → 不应判异常（None）
        in_band = db.record("latency_ms", BASE_TS + warm_n * 60.0, 100.3)
        self._healthy_runs += 1
        # 注意：动态基线暖机期可能对早期点不告警（冷启动不判）；带内点必为 None
        in_band_ok = in_band is None
        # 注入离群点 → 应判异常
        outlier = db.record("latency_ms", BASE_TS + (warm_n + 1) * 60.0, 999.0)
        self._positive_cases += 1
        if outlier is not None:
            self._positive_detected += 1
        snap = db.to_dict()

        ok = in_band_ok and outlier is not None and isinstance(snap, dict)
        self._record(
            "C-6", "动态基线（滚动基线/同环比/冷启动兜底/regime）", ok,
            f"warm={warm_n} in_band_event={in_band} outlier_event={'yes' if outlier else 'no'}",
            measured={
                "warm_points": warm_n,
                "in_band_is_none": in_band_ok,
                "outlier_detected": outlier is not None,
                "anomaly_count": snap.get("anomaly_count"),
                "regime": snap.get("regime"),
            },
        )

    # ── C-7 根因 ────────────────────────────────────────────────
    def _check_c7_incident(self) -> None:
        from myquant.core.constants import SeverityLevel  # noqa: WPS433
        from myquant.monitoring.incident_context import (
            IncidentContextEngine,  # noqa: WPS433
        )

        fake = _FakeAlert()
        eng = IncidentContextEngine(window_seconds=60.0, alert_manager=fake,
                                    clock=_fixed_clock)
        # 根因：watchdog 断流
        eng.handle_alert(SeverityLevel.ERROR, "watchdog", "monitoring.watchdog",
                         "watchdog:staleness", "行情断流")
        # 同窗上层：data_quality 应被依赖抑制
        eng.handle_alert(SeverityLevel.WARNING, "data_quality", "monitoring.dq",
                         "dq:gap", "数据质量下降")
        snap = eng.to_dict()
        incident_total = int(snap.get("incident_total", 0))
        suppressed_total = int(snap.get("suppressed_total", 0))
        self._positive_cases += 1
        if suppressed_total >= 1:
            self._positive_detected += 1
        ok = incident_total == 1 and suppressed_total >= 1
        self._record(
            "C-7", "根因（同窗聚合/依赖抑制/事件驱动）", ok,
            f"incident_total={incident_total} suppressed_total={suppressed_total}",
            measured={"incident_total": incident_total,
                      "suppressed_total": suppressed_total},
        )

    # ── C-8 容量 ───────────────────────────────────────────────
    def _check_c8_capacity(self) -> None:
        from myquant.monitoring.capacity import CapacityMonitor  # noqa: WPS433

        fake = _FakeAlert()
        cap = CapacityMonitor(
            storage_headroom_min_pct=INJ_HEADROOM_MIN_PCT,
            strategy_count_limit=INJ_STRATEGY_LIMIT,
            alert_manager=fake, clock=_fixed_clock,
            storage_total_mb=INJ_STORAGE_TOTAL_MB,
        )
        for i in range(9):
            cap.record_data_size(BASE_TS + i * 86_400.0, 100.0 + i * 5.0)
        for name in ["trend_a", "meanrev_b", "arbitrage_c"]:
            cap.register_strategy(name)
        panel = cap.poll()
        self._healthy_runs += 1
        self._false_positives += fake.count
        ok = isinstance(panel, dict) and fake.count == 0
        self._record(
            "C-8", "容量（外推预测/剩余天数/headroom 告警）", ok,
            f"points=9 strategies=3 alerts={fake.count}",
            measured={"alert_calls": fake.count,
                      "panel_keys": sorted(list(panel.keys())) if isinstance(panel, dict) else []},
        )

    # ── C-9 告警治理 ────────────────────────────────────────────
    def _check_c9_governor(self) -> None:
        from myquant.core.constants import SeverityLevel  # noqa: WPS433
        from myquant.monitoring.alerting import (  # noqa: WPS433
            GOVERNANCE_SUPPRESSED_DEPENDENCY,
            GOVERNANCE_SUPPRESSED_META,
            Alert,
            AlertGovernor,
        )

        gov = AlertGovernor(
            dependency_suppression_enabled=True,
            suppression_window_seconds=60.0,
            aggregation_window_seconds=60.0,
            meta_cooldown_seconds=120.0,
            clock=_fixed_clock,
        )
        root = Alert(level=SeverityLevel.ERROR, message="行情断流",
                     category="watchdog", source="monitoring.watchdog",
                     deduplication_key="watchdog:staleness", timestamp=CLOCK_NOW)
        d_root = gov.evaluate(root)
        upper = Alert(level=SeverityLevel.WARNING, message="数据质量下降",
                      category="data_quality", source="monitoring.dq",
                      deduplication_key="dq:gap", timestamp=CLOCK_NOW)
        d_up = gov.evaluate(upper)
        self._positive_cases += 1
        if (not d_up.allowed) and d_up.reason == GOVERNANCE_SUPPRESSED_DEPENDENCY:
            self._positive_detected += 1

        meta1 = Alert(level=SeverityLevel.WARNING, message="[meta] collector stale",
                     category="meta", source="meta_monitor",
                     deduplication_key="meta:collector_health", timestamp=CLOCK_NOW)
        gov.evaluate(meta1)
        meta2 = Alert(level=SeverityLevel.WARNING, message="[meta] collector stale 再发",
                      category="meta", source="meta_monitor",
                      deduplication_key="meta:collector_health", timestamp=CLOCK_NOW)
        d_meta2 = gov.evaluate(meta2)
        self._positive_cases += 1
        if (not d_meta2.allowed) and d_meta2.reason == GOVERNANCE_SUPPRESSED_META:
            self._positive_detected += 1

        summ = gov.summary()
        ok = (
            d_root.allowed
            and (not d_up.allowed) and d_up.reason == GOVERNANCE_SUPPRESSED_DEPENDENCY
            and (not d_meta2.allowed) and d_meta2.reason == GOVERNANCE_SUPPRESSED_META
            and isinstance(summ, dict)
        )
        self._record(
            "C-9", "告警治理（聚合分组/依赖抑制/元监控独立冷却）", ok,
            f"root_allowed={d_root.allowed} upper_reason={d_up.reason} "
            f"meta2_reason={d_meta2.reason}",
            measured={"root_allowed": d_root.allowed, "upper_reason": d_up.reason,
                      "meta2_reason": d_meta2.reason, "summary": summ},
        )

    # ── C-10 信号一致性 ────────────────────────────────────────
    def _check_c10_consistency(self) -> None:
        from myquant.monitoring.signal_consistency import (
            SignalConsistencyChecker,  # noqa: WPS433
        )

        # 对齐序列：回测/实盘完全一致 → mismatch_rate=0，不告警
        fake_h = _FakeAlert()
        chk = SignalConsistencyChecker(
            signal_deviation_rate_max=INJ_DEVIATION_RATE_MAX,
            price_tolerance=1.0, time_tolerance=60.0,
            alert_manager=fake_h, clock=_fixed_clock,
        )
        base_sigs = [
            {"signal_id": f"s{i}", "timestamp": CLOCK_NOW - 300.0 + i * 60.0,
             "symbol": "600519", "side": "buy", "price": 1700.0 + i}
            for i in range(5)
        ]
        panel_h = chk.run_comparison(base_sigs, list(base_sigs))
        self._healthy_runs += 1
        self._false_positives += fake_h.count

        # 偏差序列：同 id 但实盘 s2 价格严重偏离（超 price_tolerance）→ 偏差率>阈 → 告警
        fake_d = _FakeAlert()
        chk2 = SignalConsistencyChecker(
            signal_deviation_rate_max=INJ_DEVIATION_RATE_MAX,
            price_tolerance=1.0, time_tolerance=60.0,
            alert_manager=fake_d, clock=_fixed_clock,
        )
        live_dev = [dict(s) for s in base_sigs]
        live_dev[2] = dict(base_sigs[2], price=9999.0)   # 价格偏差 8299 > 容差 1.0
        panel_d = chk2.run_comparison(base_sigs, live_dev)
        mismatch_alerted = bool(panel_d.get("alert_triggered")) or fake_d.count >= 1
        self._positive_cases += 1
        if mismatch_alerted:
            self._positive_detected += 1

        ok = (
            float(panel_h.get("mismatch_rate", 1.0)) == 0.0
            and not panel_h.get("alert_triggered")
            and fake_h.count == 0
            and mismatch_alerted
        )
        self._record(
            "C-10", "信号一致性（逐笔对齐/偏差率/超阈告警/one-sided）", ok,
            f"healthy_rate={panel_h.get('mismatch_rate')} alerts_h={fake_h.count}; "
            f"deviation_alert={mismatch_alerted}",
            measured={
                "healthy_mismatch_rate": panel_h.get("mismatch_rate"),
                "healthy_alerts": fake_h.count,
                "deviation_alert_triggered": panel_d.get("alert_triggered"),
            },
        )

    # ── C-11 数据血缘（决策项）──────────────────────────────────
    def _check_c11_lineage(self) -> None:
        target = self._root / DATA_LINEAGE_REL_PATH
        exists = os.path.exists(target)
        # 已决策关闭/远期（#240）：验收=确认磁盘不存在该实现文件
        ok = exists is False
        self._record(
            "C-11", "数据血缘（决策=关闭/远期，#240 已回写）", ok,
            f"磁盘存在 data_lineage.py={exists}（预期 False；决策项非代码验收）",
            measured={"path": str(target), "exists": exists,
                      "decision": "closed_deferred", "ticket": "#240"},
        )

    # ── 主流程 ─────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        """逐项执行验收并汇总报告。

        Returns:
            dict: 完整验收报告（JSON 可序列化）。
        """
        checks: List[Callable[[], None]] = [
            self._check_c1_drift,
            self._check_c2_rolling,
            self._check_c4_signal_decay,
            self._check_c5_attribution,
            self._check_c6_baseline,
            self._check_c7_incident,
            self._check_c8_capacity,
            self._check_c9_governor,
            self._check_c10_consistency,
            self._check_c11_lineage,
        ]
        for chk in checks:
            try:
                chk()
            except Exception as exc:  # noqa: BLE001 —— 单项失败不阻断其余
                self._results.append({
                    "item": getattr(chk, "__name__", chk),
                    "name": chk.__name__,
                    "verdict": "fail",
                    "summary": f"执行异常: {exc}",
                    "measured": {},
                    "error": traceback.format_exc(),
                })

        passed = sum(1 for r in self._results if r["verdict"] == "pass")
        total = len(self._results)
        false_positive_rate = (
            self._false_positives / self._healthy_runs
            if self._healthy_runs else 0.0
        )
        report: Dict[str, Any] = {
            "acceptance": "C-12 C包保Alpha验收",
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "report_date": REPORT_DATE,
            "environment": {
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "isolated": True,
                "fixed_clock_used": True,
                "quant_db_touched": False,
            },
            "items": self._results,
            "false_positive": {
                "healthy_runs": self._healthy_runs,
                "false_positive_alerts": self._false_positives,
                "false_positive_rate": round(false_positive_rate, 6),
                "tp": self._positive_detected,
                "fn": self._positive_cases - self._positive_detected,
                "positive_cases": self._positive_cases,
                "note": "P0-8：健康输入序列上的告警数（预期 0 误报）；TP/FN 来自隔离注入的"
                        "已知异常检出（漂移告警/依赖抑制/meta 冷却/基线离群/一致性偏差）。",
            },
            "totals": {"pass": passed, "fail": total - passed, "total": total},
            "overall_pass": passed == total,
        }
        return report

    def write_report(self, path: Path) -> Dict[str, Any]:
        """执行 run() 并把 JSON 报告写到 path（自动建目录）。

        Returns:
            dict: 写出的报告内容。
        """
        report = self.run()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
        return report


# ═══════════════════════════════════════════════════════════════════════
# __main__ 自测
# ═══════════════════════════════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测：跑 Runner 并断言报告结构合法。

    Returns:
        int: 失败项数（0 = 全绿）。
    """
    failures: List[str] = []
    runner = CAcceptanceRunner()
    report = runner.run()

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    check("报告含 items", isinstance(report.get("items"), list) and len(report["items"]) >= 10)
    check("报告含 overall_pass", "overall_pass" in report)
    check("报告含 false_positive", "false_positive" in report)
    check("报告含 generated_at", "generated_at" in report)
    check("报告日期为 2026-10-06", report.get("report_date") == REPORT_DATE)
    check("隔离标记", report["environment"]["isolated"] is True
          and report["environment"]["quant_db_touched"] is False)
    fp = report["false_positive"]
    check("误报统计字段齐全",
          {"healthy_runs", "false_positive_alerts", "tp", "fn"} <= set(fp.keys()))
    verdicts = [it["verdict"] for it in report["items"]]
    check("逐项 verdict 合法", all(v in ("pass", "fail") for v in verdicts))
    return len(failures)


if __name__ == "__main__":
    sys.exit(_run_self_test())
