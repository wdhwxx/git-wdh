"""统一时点日落点边界测试（M1A-03，v3.0 §6.4.2）。

覆盖：
- ``report_deadline`` 法定披露截止日推定（含 12-31 跨年、异常格式保留）；
- ``eff_disclosure_date`` 优先级（ann_date → report_deadline → report_date）；
- ``match_latest_financial`` 负向口径（7/15 信号日不应看到 8/28 才披露的中报，
  参照 ``tests/unit/test_f15_ann_date_pointin.py:57``）。
"""

from myquant.data_access.time_semantics import (
    eff_disclosure_date,
    match_latest_financial,
    report_deadline,
)


class TestReportDeadline:
    """report_deadline：法定披露截止日推定边界。"""

    def test_q1_report_deadline_apr30(self) -> None:
        assert report_deadline("2025-03-31") == "2025-04-30"

    def test_h1_report_deadline_aug31(self) -> None:
        assert report_deadline("2025-06-30") == "2025-08-31"

    def test_q3_report_deadline_oct31(self) -> None:
        assert report_deadline("2025-09-30") == "2025-10-31"

    def test_annual_report_deadline_next_year_apr30(self) -> None:
        """年报披露截止日跨年到次年 4-30（防未来关键边界）。"""
        assert report_deadline("2025-12-31") == "2026-04-30"

    def test_annual_report_leap_year(self) -> None:
        """跨年落到闰年次年（2024-12-31 → 2026-04-30，非闰日不受影响）。"""
        assert report_deadline("2024-12-31") == "2025-04-30"

    def test_invalid_format_keeps_original(self) -> None:
        """异常报告期格式保留原值，不阻断下游。"""
        assert report_deadline("2025-13-99") == "2025-13-99"
        assert report_deadline("bad-date") == "bad-date"

    def test_empty_and_short_keep_original(self) -> None:
        assert report_deadline("") == ""
        assert report_deadline("2025") == "2025"


class TestEffDisclosureDate:
    """eff_disclosure_date：优先级 ann_date → report_deadline → report_date。"""

    def test_ann_date_priority(self) -> None:
        rec = {"report_date": "2025-06-30", "ann_date": "2025-08-15"}
        assert eff_disclosure_date(rec) == "2025-08-15"

    def test_missing_ann_date_falls_back_to_deadline(self) -> None:
        """ann_date 缺失回退法定披露截止日（保守正确：宁可晚见）。"""
        rec = {"report_date": "2025-12-31", "ann_date": ""}
        assert eff_disclosure_date(rec) == "2026-04-30"

    def test_missing_ann_and_abnormal_report_keeps_report(self) -> None:
        """ann_date 缺失且报告期异常：回退 report_date 本身。"""
        rec = {"report_date": "2025-13-99", "ann_date": None}
        assert eff_disclosure_date(rec) == "2025-13-99"

    def test_missing_report_and_ann_returns_empty(self) -> None:
        rec: dict = {}
        assert eff_disclosure_date(rec) == ""

    def test_ann_date_whitespace_treated_missing(self) -> None:
        rec = {"report_date": "2025-06-30", "ann_date": "   "}
        assert eff_disclosure_date(rec) == "2025-08-31"


class TestMatchLatestFinancial:
    """match_latest_financial：③④ 共用匹配，负向口径必须复现。"""

    # 参照 test_f15_ann_date_pointin.py:57：中报 8/28 才披露，7/15 信号日不可见
    def test_negative_h1_not_visible_before_ann(self) -> None:
        records = [
            {"report_date": "2025-06-30", "ann_date": "2025-08-28", "eps": 0.42},
            {"report_date": "2025-03-31", "ann_date": "2025-04-29", "eps": 0.10},
        ]
        visible = match_latest_financial(records, "2025-07-15")
        assert visible is not None
        assert visible["eps"] == 0.10  # 只可见一季报，中报 8/28 未披露不可见

    def test_negative_annual_not_visible_before_deadline(self) -> None:
        """年报 ann_date 缺失：有效披露日=次年 4-30，年报前交易日不可见。"""
        records = [
            {"report_date": "2025-12-31", "ann_date": "", "eps": 1.20},
            {"report_date": "2025-09-30", "ann_date": "2025-10-30", "eps": 0.30},
        ]
        # 2026-01-15 处于年报披露截止日（2026-04-30）之前 → 只可见三季报
        visible = match_latest_financial(records, "2026-01-15")
        assert visible is not None
        assert visible["eps"] == 0.30

        # 2026-05-01 已过年报截止日 → 年报可见
        visible_later = match_latest_financial(records, "2026-05-01")
        assert visible_later is not None
        assert visible_later["eps"] == 1.20

    def test_latest_visible_on_ann_date_itself(self) -> None:
        """披露日当天即可见（<= 语义）。"""
        records = [
            {"report_date": "2025-06-30", "ann_date": "2025-08-28", "eps": 0.42},
        ]
        visible = match_latest_financial(records, "2025-08-28")
        assert visible is not None
        assert visible["eps"] == 0.42

    def test_no_visible_record_returns_none(self) -> None:
        records = [
            {"report_date": "2025-06-30", "ann_date": "2025-08-28", "eps": 0.42},
        ]
        assert match_latest_financial(records, "2025-08-27") is None

    def test_empty_records_returns_none(self) -> None:
        assert match_latest_financial([], "2025-07-15") is None

    def test_empty_trade_date_returns_none(self) -> None:
        records = [{"report_date": "2025-06-30", "ann_date": "2025-08-28"}]
        assert match_latest_financial(records, "") is None

    def test_descending_eff_takes_latest(self) -> None:
        """多条可见记录取有效披露日最大（最新）一期。"""
        records = [
            {"report_date": "2025-03-31", "ann_date": "2025-04-29", "eps": 0.10},
            {"report_date": "2025-06-30", "ann_date": "2025-08-28", "eps": 0.42},
        ]
        visible = match_latest_financial(records, "2025-09-01")
        assert visible is not None
        assert visible["eps"] == 0.42
