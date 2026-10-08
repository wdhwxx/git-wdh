# 文件名：tests/unit/test_alert_governance.py
"""
test_alert_governance.py - C-9 告警治理与降噪（回写#238）单元测试
===========================================================
覆盖（施工清单 C-9）：
  1. 聚合分组：同根因链路相似告警在聚合窗口内归并（summary.aggregated_total）
  2. 依赖抑制集成（引用 C-7 IncidentContextEngine.DEFAULT_DEPENDENCY_MAP）：
     同窗底层根因命中 → 上层告警 suppressed（不单独告警）
  3. 动态基线降噪（引用 C-6 DynamicBaseline）：值落在动态基线内 → 抑制静态阈值误报；
     冷启动样本不足不抑制（不编造）；确证异常放行
  4. 元监控独立冷却（R-08）：dedup_key=meta:* 走独立冷却窗口，跳过业务依赖/基线降噪
  5. TP/FP/FN 周统计口径与达标判定（P0-8：准确率>0.95 且误报率<0.05）
  6. 防未来：拒绝未来时间戳告警
  7. 防御校验：非法窗口抛错 / attach 非 governor 抛错 / store=None 抛错
  8. 既有契约不破坏：AlertManager 默认无 governor 时行为不变（A-8/A-9/A-10 契约）

运行方式:
  pytest tests/unit/test_alert_governance.py -q
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.core.alert_store import AlertStore
from myquant.monitoring.alerting import (
    GOVERNANCE_SUPPRESSED_BASELINE,
    GOVERNANCE_SUPPRESSED_DEPENDENCY,
    GOVERNANCE_SUPPRESSED_META,
    WEEK_SECONDS,
    Alert,
    AlertGovernor,
    AlertManager,
    ConsoleAlertChannel,
    SeverityLevel,
    weekly_alert_stats,
)
from myquant.monitoring.dynamic_baseline import DynamicBaseline


# ═══════════════════════════════════════════════
#  夹具与工具
# ═══════════════════════════════════════════════

class _FixedClock:
    """固定时钟（防未来：所有喂入 ts 远小于 now）。"""

    def __init__(self, now: float = 1_700_000_000.0 + 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _make_alert(
    *,
    level: SeverityLevel = SeverityLevel.WARNING,
    message: str = "t",
    category: str = "system",
    source: str = "test",
    dedup: str = "",
    details: Any = None,
    ts: float = 0.0,
    alert_id: str = "",
) -> Alert:
    return Alert(
        level=level,
        message=message,
        category=category,
        source=source,
        deduplication_key=dedup,
        details=details or {},
        timestamp=ts,
        alert_id=alert_id,
    )


@pytest.fixture()
def tmp_store():
    """临时 AlertStore（不碰真实 quant.db）。"""
    tmp = tempfile.mkdtemp(prefix="c9_gov_")
    conn = DatabaseConnection(Path(tmp) / "gov.db")
    store = AlertStore(connection=conn)
    yield store
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


# ═══════════════════════════════════════════════
#  2. 依赖抑制（集成 C-7 DEFAULT_DEPENDENCY_MAP）
# ═══════════════════════════════════════════════

class TestDependencySuppression:
    """集成 C-7：底层根因命中 → 同窗上层告警 suppressed（引用 C-7 依赖映射）。"""

    def test_root_cause_allows_upper_suppressed(self) -> None:
        clock = _FixedClock()
        gov = AlertGovernor(
            dependency_suppression_enabled=True,
            suppression_window_seconds=60.0,
            aggregation_window_seconds=60.0,
            meta_cooldown_seconds=120.0,
            clock=clock,
        )
        # C-7 DEFAULT_DEPENDENCY_MAP: watchdog -> [data_quality, strategy]
        root = _make_alert(category="watchdog", dedup="watchdog:staleness", ts=clock.now)
        d_root = gov.evaluate(root)
        assert d_root.allowed is True  # 根因放行

        upper = _make_alert(category="data_quality", dedup="dq:gap", ts=clock.now)
        d_up = gov.evaluate(upper)
        assert d_up.allowed is False
        assert d_up.reason == GOVERNANCE_SUPPRESSED_DEPENDENCY

    def test_non_dependency_category_allowed(self) -> None:
        clock = _FixedClock()
        gov = AlertGovernor(dependency_suppression_enabled=True, clock=clock)
        gov.evaluate(_make_alert(category="watchdog", dedup="w:1", ts=clock.now))
        # broker 不在 watchdog 的上层映射里 → 放行
        other = _make_alert(category="broker", dedup="b:lat", ts=clock.now)
        assert gov.evaluate(other).allowed is True

    def test_out_of_window_root_no_suppress(self) -> None:
        clock = _FixedClock()
        gov = AlertGovernor(
            dependency_suppression_enabled=True,
            suppression_window_seconds=10.0,
            clock=clock,
        )
        # 根因在 now 命中
        gov.evaluate(_make_alert(category="watchdog", dedup="w:1", ts=clock.now))
        # 时钟推进 20s（>窗口）后上层告警 → 不再抑制
        clock.now += 20.0
        upper = _make_alert(category="data_quality", dedup="dq:late", ts=clock.now)
        assert gov.evaluate(upper).allowed is True

    def test_uses_c7_default_dependency_map(self) -> None:
        """未注入 dependency_map 时复用 C-7 DEFAULT_DEPENDENCY_MAP。"""
        from myquant.monitoring.incident_context import DEFAULT_DEPENDENCY_MAP
        gov = AlertGovernor(clock=_FixedClock())
        assert gov._dep_map == {k: list(v) for k, v in DEFAULT_DEPENDENCY_MAP.items()}


# ═══════════════════════════════════════════════
#  3. 动态基线降噪（集成 C-6 DynamicBaseline）
# ═══════════════════════════════════════════════

class TestDynamicBaselineNoiseReduction:
    """集成 C-6：值落在动态基线带内 → 抑制静态阈值误报；冷启动不抑制。"""

    def _fed_normal(self, gov: AlertGovernor, db: DynamicBaseline, n: int,
                    base: float = 1_700_000_000.0) -> None:
        values = [100.0, 101.0, 99.0, 100.5, 99.5, 100.2, 99.8, 100.1, 99.9, 100.0]
        for i in range(n):
            a = _make_alert(
                category="system", dedup=f"static:x:{i}", ts=base + i * 60.0,
                details={"governance": {"series": "latency_ms", "value": values[i % len(values)]}},
            )
            gov.evaluate(a)

    def test_within_baseline_suppresses_after_warmup(self) -> None:
        clock = _FixedClock()
        db = DynamicBaseline(window=20, k=3.0, min_samples=10, clock=clock)
        gov = AlertGovernor(
            baseline_noise_reduction_enabled=True, dynamic_baseline=db, clock=clock,
        )
        self._fed_normal(gov, db, 9)  # 9 点冷启动
        # 第 10 点（暖机）值落在基线带内 → 抑制静态阈值误报
        a = _make_alert(
            category="system", dedup="static:fp", ts=clock.now - 100.0,
            details={"governance": {"series": "latency_ms", "value": 100.2}},
        )
        d = gov.evaluate(a)
        assert d.allowed is False
        assert d.reason == GOVERNANCE_SUPPRESSED_BASELINE

    def test_cold_start_does_not_suppress(self) -> None:
        clock = _FixedClock()
        db = DynamicBaseline(window=20, k=3.0, min_samples=10, clock=clock)
        gov = AlertGovernor(
            baseline_noise_reduction_enabled=True, dynamic_baseline=db, clock=clock,
        )
        a = _make_alert(
            category="system", dedup="static:cold", ts=clock.now - 100.0,
            details={"governance": {"series": "latency_ms", "value": 100.0}},
        )
        # 冷启动样本不足 → 不抑制（不编造判断）
        assert gov.evaluate(a).allowed is True

    def test_no_governance_details_no_suppress(self) -> None:
        clock = _FixedClock()
        db = DynamicBaseline(window=20, k=3.0, min_samples=2, clock=clock)
        gov = AlertGovernor(
            baseline_noise_reduction_enabled=True, dynamic_baseline=db, clock=clock,
        )
        a = _make_alert(category="system", dedup="plain", ts=clock.now)  # 无 governance details
        assert gov.evaluate(a).allowed is True

    def test_baseline_anomaly_allows(self) -> None:
        """C-6 确证异常（record 返回事件）→ 放行，不抑制。"""
        clock = _FixedClock()
        db = DynamicBaseline(window=20, k=3.0, min_samples=10, clock=clock)
        gov = AlertGovernor(
            baseline_noise_reduction_enabled=True, dynamic_baseline=db, clock=clock,
        )
        self._fed_normal(gov, db, 10)  # 暖机，mean≈100
        # 极端值 500 → C-6 判异常 → 放行（真异常，不抑制）
        a = _make_alert(
            category="system", dedup="static:anom", ts=clock.now - 50.0,
            details={"governance": {"series": "latency_ms", "value": 500.0}},
        )
        d = gov.evaluate(a)
        assert d.allowed is True  # 异常确认，放行


# ═══════════════════════════════════════════════
#  4. 元监控独立冷却（R-08）
# ═══════════════════════════════════════════════

class TestMetaIndependentCooldown:
    """dedup_key=meta:* 走独立冷却窗口，跳过业务依赖/基线降噪。"""

    def test_meta_first_allowed_second_suppressed(self) -> None:
        clock = _FixedClock()
        gov = AlertGovernor(
            dependency_suppression_enabled=True,
            meta_cooldown_seconds=120.0, clock=clock,
        )
        m1 = _make_alert(category="meta", dedup="meta:collector", ts=clock.now)
        assert gov.evaluate(m1).allowed is True
        m2 = _make_alert(category="meta", dedup="meta:collector", ts=clock.now)
        d = gov.evaluate(m2)
        assert d.allowed is False
        assert d.reason == GOVERNANCE_SUPPRESSED_META

    def test_meta_skips_dependency_suppression(self) -> None:
        """meta:* 即使在根因链路窗口内也不走依赖抑制（独立通道）。"""
        clock = _FixedClock()
        gov = AlertGovernor(
            dependency_suppression_enabled=True,
            suppression_window_seconds=60.0, clock=clock,
        )
        # 先有一个 watchdog 根因（业务）
        gov.evaluate(_make_alert(category="watchdog", dedup="w:1", ts=clock.now))
        # meta 告警即使 category=meta 也不被依赖抑制（R-08 独立）
        m = _make_alert(category="meta", dedup="meta:x", ts=clock.now)
        d = gov.evaluate(m)
        assert d.allowed is True
        assert d.reason != GOVERNANCE_SUPPRESSED_DEPENDENCY


# ═══════════════════════════════════════════════
#  1. 聚合分组
# ═══════════════════════════════════════════════

class TestAggregationGrouping:
    """同根因链路相似告警在聚合窗口内归并（summary.aggregated_total）。"""

    def test_same_group_counted_aggregated(self) -> None:
        clock = _FixedClock()
        gov = AlertGovernor(aggregation_window_seconds=60.0, clock=clock)
        gov.evaluate(_make_alert(category="data_quality", dedup="dq:gap:one", ts=clock.now))
        gov.evaluate(_make_alert(category="data_quality", dedup="dq:gap:two", ts=clock.now))
        s = gov.summary()
        assert s["aggregated_total"] == 1

    def test_out_of_group_window_not_aggregated(self) -> None:
        clock = _FixedClock()
        gov = AlertGovernor(aggregation_window_seconds=10.0, clock=clock)
        gov.evaluate(_make_alert(category="data_quality", dedup="dq:gap:one", ts=clock.now))
        clock.now += 20.0
        gov.evaluate(_make_alert(category="data_quality", dedup="dq:gap:two", ts=clock.now))
        assert gov.summary()["aggregated_total"] == 0


# ═══════════════════════════════════════════════
#  5. TP/FP/FN 周统计口径与达标判定（P0-8）
# ═══════════════════════════════════════════════

class TestWeeklyAlertStats:
    """weekly_alert_stats：准确率=TP/(TP+FP)、误报率=FP/(TP+FP)、检出率=TP/(TP+FN)。"""

    def _seed(self, store: AlertStore, now: float, tp: int, fp: int) -> None:
        for i in range(tp):
            a = Alert(level=SeverityLevel.WARNING, message=f"tp{i}",
                      category="system", alert_id=f"w-tp-{i}", timestamp=now - 3600.0)
            store.save_alert(a)
            store.set_classification(f"w-tp-{i}", "TP")
        for i in range(fp):
            a = Alert(level=SeverityLevel.WARNING, message=f"fp{i}",
                      category="system", alert_id=f"w-fp-{i}", timestamp=now - 3600.0)
            store.save_alert(a)
            store.set_classification(f"w-fp-{i}", "FP")

    def test_accuracy_fp_rate_pass_judgement(self, tmp_store: AlertStore) -> None:
        clock = _FixedClock()
        self._seed(tmp_store, clock.now, tp=19, fp=1)  # 19/20=0.95（边界不达标）
        s = weekly_alert_stats(tmp_store, clock=clock)
        assert s["tp"] == 19 and s["fp"] == 1
        assert s["accuracy"] == 0.95
        assert s["false_positive_rate"] == 0.05
        assert s["passed"] is False  # >0.95 且 <0.05 才达标；边界不算

    def test_pass_when_accuracy_above_target(self, tmp_store: AlertStore) -> None:
        clock = _FixedClock()
        self._seed(tmp_store, clock.now, tp=20, fp=1)  # 20/21≈0.9524>0.95, 1/21≈0.0476<0.05
        s = weekly_alert_stats(tmp_store, clock=clock)
        assert s["passed"] is True
        assert s["accuracy"] > 0.95
        assert s["false_positive_rate"] < 0.05

    def test_detection_rate_with_fn(self, tmp_store: AlertStore) -> None:
        clock = _FixedClock()
        self._seed(tmp_store, clock.now, tp=10, fp=0)
        fi = [{"captured": True}, {"captured": False}, {"captured": False}]
        s = weekly_alert_stats(tmp_store, fi_records=fi, clock=clock)
        assert s["fn"] == 2  # count_fn = 未捕获数
        assert s["detection_rate"] == round(10 / 12, 6)

    def test_empty_window_rates_none_not_zero(self, tmp_store: AlertStore) -> None:
        clock = _FixedClock()
        s = weekly_alert_stats(tmp_store, clock=clock)
        # 样本不足：比率为 None（诚实不造数），passed=False
        assert s["accuracy"] is None
        assert s["false_positive_rate"] is None
        assert s["passed"] is False

    def test_note_contains_4_week_rule(self, tmp_store: AlertStore) -> None:
        clock = _FixedClock()
        s = weekly_alert_stats(tmp_store, clock=clock)
        assert "4 周" in s["note"]

    def test_week_window_span(self, tmp_store: AlertStore) -> None:
        clock = _FixedClock()
        s = weekly_alert_stats(tmp_store, clock=clock)
        assert abs((s["week_end"] - s["week_start"]) - WEEK_SECONDS) < 1e-6

    def test_store_none_raises(self) -> None:
        with pytest.raises(ValueError):
            weekly_alert_stats(None)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════
#  6. 防未来
# ═══════════════════════════════════════════════

class TestFutureProof:
    def test_future_alert_rejected(self) -> None:
        clock = _FixedClock()
        gov = AlertGovernor(clock=clock)
        future = _make_alert(category="system", ts=clock.now + 1000.0)
        with pytest.raises(ValueError):
            gov.evaluate(future)

    def test_future_rows_excluded_from_weekly_stats(self, tmp_store: AlertStore) -> None:
        clock = _FixedClock()
        # 落一条未来时间戳告警（classification=TP）
        a = Alert(level=SeverityLevel.WARNING, message="future",
                  category="system", alert_id="future-1", timestamp=clock.now + 3600.0)
        tmp_store.save_alert(a)
        tmp_store.set_classification("future-1", "TP")
        s = weekly_alert_stats(tmp_store, clock=clock)
        # 未来行被排除 → tp=0
        assert s["tp"] == 0


# ═══════════════════════════════════════════════
#  7. 防御校验
# ═══════════════════════════════════════════════

class TestDefensive:
    def test_negative_window_raises(self) -> None:
        with pytest.raises(ValueError):
            AlertGovernor(suppression_window_seconds=-1)

    def test_non_positive_meta_cooldown_raises(self) -> None:
        with pytest.raises(ValueError):
            AlertGovernor(meta_cooldown_seconds=0)

    def test_invalid_dependency_map_raises(self) -> None:
        with pytest.raises(ValueError):
            AlertGovernor(dependency_map="not-a-dict")  # type: ignore[arg-type]

    def test_evaluate_none_raises(self) -> None:
        with pytest.raises(ValueError):
            AlertGovernor().evaluate(None)  # type: ignore[arg-type]

    def test_attach_non_governor_raises(self) -> None:
        mgr = AlertManager()
        with pytest.raises(ValueError):
            mgr.attach_governor("nope")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════
#  8. 既有契约不破坏（governor=None 时行为不变）
# ═══════════════════════════════════════════════

class TestExistingContractUnbroken:
    """AlertManager 默认无 governor：send_alert/dedup/stats 既有契约不变。"""

    def test_default_manager_no_governor(self) -> None:
        mgr = AlertManager()
        assert mgr._governor is None
        assert mgr.stats["governance"] is None

    def test_send_alert_works_without_governor(self, tmp_store: AlertStore) -> None:
        mgr = AlertManager(store=tmp_store)
        mgr.add_channel(ConsoleAlertChannel("c"))
        a = _make_alert(category="broker", dedup="broker:lat", ts=time.time())
        n = mgr.send_alert(a)
        assert n >= 1  # 无 governor 时正常投递

    def test_dedup_still_works_without_governor(self, tmp_store: AlertStore) -> None:
        mgr = AlertManager(store=tmp_store)
        mgr.add_channel(ConsoleAlertChannel("c"))
        a = _make_alert(category="broker", dedup="broker:dup", ts=time.time())
        mgr.send_alert(a, dedup_enabled=True)
        n2 = mgr.send_alert(a, dedup_enabled=True)
        assert n2 == 0  # 既有 dedup 契约不变

    def test_governor_summary_structure(self) -> None:
        gov = AlertGovernor(clock=_FixedClock())
        s = gov.summary()
        for k in ("allowed_total", "suppressed_total", "aggregated_total",
                  "suppressed_by_dependency", "suppressed_by_baseline",
                  "suppressed_by_meta_cooldown"):
            assert k in s
