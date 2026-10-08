# 文件名：tests/unit/test_weekly_review.py
"""
test_weekly_review.py - §7 周告警复盘报告生成器测试（R-01）

覆盖：
  1. generate：TP/FP/FN 统计（复用 weekly_alert_stats 口径）+ 准确率/误报率边界达标判定
  2. Top 告警：窗口过滤/分组计数/降序/防未来跳过
  3. 容量趋势：注入 MetricsHistoryStore 有数据出趋势；无数据 no_data；无指标库返回空
  4. 防未来：week_end 钳制、未来告警行跳过
  5. 防御：alert_store=None / top_n<=0 / save 空 report / save 空 prefix 拒绝
  6. 输出：Markdown 关键节、save 双格式落盘
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from myquant.core.constants import SeverityLevel
from myquant.database.connection import DatabaseConnection
from myquant.core.alert_store import AlertStore
from myquant.monitoring.alerting import Alert
from myquant.core.metrics_history import MetricsHistoryStore
from myquant.monitoring.weekly_review import (
    WEEK_SECONDS,
    WeeklyReviewReport,
)


def _make_store(tmp: Path) -> AlertStore:
    """临时告警库（带 alerts 表结构）。"""
    conn = DatabaseConnection(tmp / "wr_test.db")
    return AlertStore(connection=conn)


def _save_alert(
    store: AlertStore,
    message: str,
    *,
    level: str = SeverityLevel.WARNING,
    category: str = "system",
    alert_id: str,
    ts: float,
    classification: str = "",
) -> None:
    """落一条告警并可选打 classification。"""
    alert = Alert(
        level=level,
        message=message,
        category=category,
        alert_id=alert_id,
        timestamp=ts,
        classification=classification,
    )
    store.save_alert(alert)
    if classification:
        store.set_classification(alert_id, classification)


class TestGenerateStats:
    """TP/FP/FN 统计与达标判定（复用 weekly_alert_stats 口径）。"""

    def test_tp_fp_统计与边界达标判定(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        now = time.time()
        for i in range(19):
            _save_alert(store, f"tp-{i}", alert_id=f"wr-tp-{i}", ts=now - 3600.0, classification="TP")
        _save_alert(
            store, "fp-one", category="data_quality", alert_id="wr-fp-1",
            ts=now - 1800.0, classification="FP",
        )
        report = WeeklyReviewReport(store).generate(week_end=now)
        assert report["tp"] == 19
        assert report["fp"] == 1
        assert report["fn"] == 0
        # 19/20=0.95：准确率=0.95 边界（目标 >0.95）不达标；误报率=0.05 边界（目标 <0.05）不达标
        assert report["accuracy"] == 0.95
        assert report["false_positive_rate"] == 0.05
        assert report["passed"] is False

    def test_达标判定_20tp_1fp(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        now = time.time()
        for i in range(20):
            _save_alert(store, f"tp-{i}", alert_id=f"wr-tp-{i}", ts=now - 3600.0, classification="TP")
        _save_alert(
            store, "fp-one", alert_id="wr-fp-1", ts=now - 1800.0, classification="FP",
        )
        report = WeeklyReviewReport(store).generate(week_end=now)
        # 20/21≈0.9524>0.95 且 1/21≈0.0476<0.05 → 达标
        assert report["accuracy"] > 0.95
        assert report["false_positive_rate"] < 0.05
        assert report["passed"] is True

    def test_窗口外告警不计入(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        now = time.time()
        _save_alert(store, "in-week", alert_id="wr-in", ts=now - 3600.0, classification="TP")
        _save_alert(
            store, "out-week", alert_id="wr-out", ts=now - WEEK_SECONDS - 100.0,
            classification="TP",
        )
        report = WeeklyReviewReport(store).generate(week_end=now)
        assert report["tp"] == 1  # 窗口外 TP 不计入


class TestTopAlerts:
    """Top 告警分组/排序/防未来。"""

    def test_分组计数与降序(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        now = time.time()
        for i in range(5):
            _save_alert(store, "repeated-msg", category="data_quality", alert_id=f"r-{i}", ts=now - 300.0)
        _save_alert(store, "single-msg", category="system", alert_id="s-1", ts=now - 300.0)
        report = WeeklyReviewReport(store).generate(week_end=now, top_n=10)
        top = report["top_alerts"]
        assert any(t["category"] == "data_quality" and t["count"] == 5 for t in top)
        assert any(t["category"] == "system" and t["count"] == 1 for t in top)
        # 降序
        for i in range(len(top) - 1):
            assert top[i]["count"] >= top[i + 1]["count"]

    def test_未来告警跳过(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        now = time.time()
        _save_alert(store, "future-msg", alert_id="wr-fut", ts=now + 10000.0)
        report = WeeklyReviewReport(store).generate(week_end=now)
        assert all(t["message"] != "future-msg" for t in report["top_alerts"])

    def test_top_n裁剪(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        now = time.time()
        for i in range(6):
            _save_alert(store, f"msg-{i}", alert_id=f"m-{i}", ts=now - 300.0)
        report = WeeklyReviewReport(store).generate(week_end=now, top_n=3)
        assert len(report["top_alerts"]) == 3


class TestCapacityTrends:
    """容量趋势：有数据/无数据/未注入。"""

    def test_注入指标库_有数据出趋势(self, tmp_path: Path) -> None:
        alert_store = _make_store(tmp_path)
        conn = DatabaseConnection(tmp_path / "wr_metrics.db")
        mstore = MetricsHistoryStore(connection=conn)
        now = time.time()
        mstore.save_batch(
            [
                {"name": "capacity_storage_headroom_pct", "value": 20.0, "timestamp": now - 7200.0},
                {"name": "capacity_storage_headroom_pct", "value": 15.0, "timestamp": now - 3600.0},
            ]
        )
        report = WeeklyReviewReport(alert_store, metrics_store=mstore).generate(week_end=now)
        caps = {c["name"]: c for c in report["capacity_trends"]}
        assert "capacity_storage_headroom_pct" in caps
        entry = caps["capacity_storage_headroom_pct"]
        assert entry["samples"] == 2
        assert entry["first_value"] == 20.0
        assert entry["last_value"] == 15.0
        assert entry["trend"] == "down"

    def test_注入指标库_无数据no_data(self, tmp_path: Path) -> None:
        alert_store = _make_store(tmp_path)
        conn = DatabaseConnection(tmp_path / "wr_metrics2.db")
        mstore = MetricsHistoryStore(connection=conn)
        report = WeeklyReviewReport(alert_store, metrics_store=mstore).generate(week_end=time.time())
        assert report["capacity_trends"] != []
        assert all(c["samples"] == 0 and c["note"] == "no_data" for c in report["capacity_trends"])

    def test_未注入指标库_容量趋势为空(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        report = WeeklyReviewReport(store).generate(week_end=time.time())
        assert report["capacity_trends"] == []

    def test_空指标名列表_不统计(self, tmp_path: Path) -> None:
        alert_store = _make_store(tmp_path)
        conn = DatabaseConnection(tmp_path / "wr_metrics3.db")
        mstore = MetricsHistoryStore(connection=conn)
        report = WeeklyReviewReport(alert_store, metrics_store=mstore).generate(
            week_end=time.time(), capacity_metric_names=[]
        )
        assert report["capacity_trends"] == []


class TestAntiFuture:
    """防未来：week_end 钳制。"""

    def test_week_end钳制到now(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        now = time.time()
        report = WeeklyReviewReport(store).generate(week_end=now + 99999.0)
        # 钳制语义：周右端远小于未钳制的入参
        assert report["week_end"] < now + 9999.0
        assert report["week_start"] < report["week_end"]


class TestDefensive:
    """防御式校验。"""

    def test_alert_store为None拒绝(self) -> None:
        with pytest.raises(ValueError):
            WeeklyReviewReport(None)  # type: ignore[arg-type]

    def test_top_n非法拒绝(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        with pytest.raises(ValueError):
            WeeklyReviewReport(store).generate(top_n=0)

    def test_save空报告拒绝(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        review = WeeklyReviewReport(store)
        with pytest.raises(ValueError):
            review.save({}, output_dir=tmp_path)

    def test_save空前缀拒绝(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        review = WeeklyReviewReport(store)
        with pytest.raises(ValueError):
            review.save({"generated_at": time.time()}, output_dir=tmp_path, prefix="")


class TestOutput:
    """Markdown 与落盘。"""

    def test_markdown关键节(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _save_alert(store, "hi", alert_id="wr-1", ts=time.time() - 300.0)
        report = WeeklyReviewReport(store).generate()
        md = WeeklyReviewReport(store).to_markdown(report)
        assert "## 一、告警准确率统计" in md
        assert "## 二、Top 告警" in md
        assert "## 三、容量趋势" in md
        assert "P0-8 口径" in md

    def test_save双格式落盘(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        review = WeeklyReviewReport(store)
        report = review.generate()
        out = tmp_path / "reports"
        md_path = review.save(report, output_dir=out)
        assert md_path.exists()
        assert md_path.suffix == ".md"
        assert (out / (md_path.stem + ".json")).exists()
