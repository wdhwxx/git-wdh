"""test_measurability.py - P0-8 可度量性行为测试（#386 P2-4 C-6 补行为级测试）

覆盖 myquant/core/measurability.py 的 TP/FP/FN 分类、MTTx 计时、FI 用例库——
全部为纯计算/纯数据行为，测试即行为契约。
"""

from __future__ import annotations

import pytest

from myquant.core.measurability import (
    CLASSIFICATION_FP,
    CLASSIFICATION_TP,
    CLASSIFICATION_UNKNOWN,
    FI_CASES,
    ClassificationStats,
    MttxCalculator,
    list_fi_cases,
)


class TestClassificationStats:
    """P0-8 ① 分类口径：TP/FP/FN/UNCLASSIFIED 判定与统计。"""

    def test_classify_tp_when_acked_requires_action(self) -> None:
        assert (
            ClassificationStats.classify_alert({"acked_disposition": "需处置"})
            == CLASSIFICATION_TP
        )

    def test_classify_tp_when_root_cause_real(self) -> None:
        assert ClassificationStats.classify_alert({"root_cause_real": True}) == CLASSIFICATION_TP

    def test_classify_fp_when_acked_noise(self) -> None:
        assert (
            ClassificationStats.classify_alert({"acked_disposition": "噪声"})
            == CLASSIFICATION_FP
        )

    def test_classify_fp_when_auto_resolved_and_no_event(self) -> None:
        assert (
            ClassificationStats.classify_alert(
                {"auto_resolved": True, "meta_confirmed_no_event": True}
            )
            == CLASSIFICATION_FP
        )

    def test_classify_unknown_when_no_evidence(self) -> None:
        assert ClassificationStats.classify_alert({}) == CLASSIFICATION_UNKNOWN

    def test_classify_raises_on_non_dict(self) -> None:
        with pytest.raises(TypeError):
            ClassificationStats.classify_alert("not-a-dict")  # type: ignore[arg-type]

    def test_count_fn_only_uncaptured(self) -> None:
        records = [
            {"case_id": "FI-01", "captured": True},
            {"case_id": "FI-02", "captured": False},
            {"case_id": "FI-03", "captured": False},
        ]
        assert ClassificationStats.count_fn(records) == 2

    def test_count_fn_rejects_non_dict(self) -> None:
        with pytest.raises(TypeError):
            ClassificationStats.count_fn(["bad"])  # type: ignore[list-item]

    def test_compute_metrics(self) -> None:
        alerts = [
            {"acked_disposition": "需处置"},  # TP
            {"acked_disposition": "误报"},    # FP
            {},                               # UNCLASSIFIED
        ]
        stats = ClassificationStats.compute(
            alerts, fi_records=[{"captured": False}, {"captured": True}]
        )
        assert stats["tp"] == 1
        assert stats["fp"] == 1
        assert stats["fn"] == 1
        assert stats["unclassified"] == 1
        assert stats["accuracy"] == 0.5
        assert stats["fp_rate"] == 0.5
        assert stats["detection_rate"] == 0.5
        assert stats["formula"]["accuracy"] == "TP/(TP+FP)"

    def test_compute_zero_denominator_honest_none(self) -> None:
        stats = ClassificationStats.compute([])
        assert stats["tp"] == 0
        assert stats["fp"] == 0
        assert stats["accuracy"] is None
        assert stats["fp_rate"] is None
        assert stats["detection_rate"] is None


class TestMttxCalculator:
    """P0-8 ② MTTx 计时口径（系统时间戳；非法序列拒绝）。"""

    def test_elapsed_seconds(self) -> None:
        assert MttxCalculator.elapsed_seconds(100.0, 105.0) == 5.0

    def test_elapsed_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            MttxCalculator.elapsed_seconds(-1.0, 0.0)

    def test_elapsed_rejects_end_before_start(self) -> None:
        with pytest.raises(ValueError):
            MttxCalculator.elapsed_seconds(5.0, 3.0)

    def test_mttd_mtta_mttr(self) -> None:
        assert MttxCalculator.mttd(0.0, 2.5) == 2.5
        assert MttxCalculator.mtta(2.5, 4.0) == 1.5
        assert MttxCalculator.mttr(0.0, 10.0) == 10.0

    def test_average(self) -> None:
        assert MttxCalculator.average([1.0, 2.0, 3.0]) == 2.0

    def test_average_empty_returns_none(self) -> None:
        assert MttxCalculator.average([]) is None

    def test_summarize(self) -> None:
        result = MttxCalculator.summarize(
            [{"seconds": 1.0, "case_id": "FI-01"}, {"seconds": 3.0}], kind="mttd"
        )
        assert result["kind"] == "mttd"
        assert result["samples"] == 2
        assert result["avg_seconds"] == 2.0
        assert result["max_seconds"] == 3.0

    def test_summarize_empty(self) -> None:
        result = MttxCalculator.summarize([], kind="mttr")
        assert result["samples"] == 0
        assert result["avg_seconds"] is None
        assert result["max_seconds"] is None

    def test_summarize_rejects_sample_without_seconds(self) -> None:
        with pytest.raises(TypeError):
            MttxCalculator.summarize([{"case_id": "FI-01"}])  # type: ignore[list-item]


class TestFICases:
    """P0-8 ③ FI-01~07 用例库完整性（R-01：唯一权威用例库）。"""

    def test_fi_cases_unique_and_ordered(self) -> None:
        ids = [c.case_id for c in FI_CASES]
        assert len(ids) == len(set(ids))
        assert ids == sorted(ids)

    def test_fi_cases_complete_fields(self) -> None:
        for case in FI_CASES:
            assert case.case_id.startswith("FI-")
            assert case.fault_desc
            assert case.expected_capture
            assert case.kpi
            assert case.timer_start
            assert case.timer_end

    def test_list_fi_cases_returns_dicts(self) -> None:
        items = list_fi_cases()
        assert len(items) >= 7
        assert all(isinstance(i, dict) and i.get("case_id") for i in items)
