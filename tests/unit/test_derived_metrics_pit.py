# 文件名：tests/unit/test_derived_metrics_pit.py
"""B-07 被消费列 PIT 口径收敛单测。

覆盖：
  1. late-disclosure：ann_date > report_deadline 时，ann_date 前该期财务不可见
     （流通股本反推/市盈动不引用），ann_date 当日/之后可见（V-09 负向用例）；
  2. 正常披露：ann_date <= report_deadline，按真实披露日可见（与原 report_deadline 口径一致）；
  3. ann_date 缺失：回退 report_deadline 推定（历史数据兼容，行为不变）；
  4. 同截止日多报告期：年报+一季报同日截止 → 一季报优先（match_latest_financial 次键语义）。
"""

from __future__ import annotations

from myquant.data_access.cleaner.derived_metrics import compute_b_class_metrics


def _bar(trade_date: str, close: float = 10.0, volume: float = 1e6) -> dict:
    return {
        "trade_date": trade_date,
        "close": close,
        "volume": volume,
        "open": 10.0, "high": 10.5, "low": 9.8,
    }


def _fin(
    report_date: str,
    ann_date: str,
    circ_mv: float = 1e11,
    total_mv: float = 1.2e11,
    net_profit: float = 1e9,
) -> dict:
    return {
        "report_date": report_date,
        "ann_date": ann_date,
        "circ_mv": circ_mv,
        "total_mv": total_mv,
        "net_profit": net_profit,
    }


# ── late-disclosure 负向（V-09）─────────────────────────
def test_late_disclosure_not_visible_before_ann_date() -> None:
    """ann_date=2026-09-05 > report_deadline(2026-08-31)：
    2026-09-01 时该期不可见（不反推股本/不引用净利）。"""
    fin = _fin(report_date="2026-06-30", ann_date="2026-09-05")
    records = [
        _bar("2026-08-31"),
        _bar("2026-09-01"),
        _bar("2026-09-07"),
    ]
    out = compute_b_class_metrics(records, [fin])
    # 8-31：ann_date(9-05) 之前 → 不可见（推定截止日口径会误判可见，本项验证收敛）
    assert out[0]["float_share"] is None
    assert out[0]["pe_dynamic"] is None
    assert out[0]["turnover"] is None
    # 9-01：仍 < ann_date → 不可见
    assert out[1]["float_share"] is None
    # 9-07：>= ann_date → 可见（反推股本 = circ_mv / close）
    assert out[2]["float_share"] is not None
    assert abs(out[2]["float_share"] - 1e11 / 10.0) < 0.5
    assert out[2]["pe_dynamic"] is not None


def test_late_disclosure_visible_on_ann_date() -> None:
    """ann_date 当日即可见。"""
    fin = _fin(report_date="2026-06-30", ann_date="2026-09-05")
    out = compute_b_class_metrics([_bar("2026-09-05")], [fin])
    assert out[0]["float_share"] is not None


# ── 正常披露（ann_date <= deadline）─────────────────────────
def test_normal_disclosure_visible_at_deadline() -> None:
    """ann_date=2026-08-20 <= deadline(2026-08-31) → 8-31 可见（与原口径一致）。"""
    fin = _fin(report_date="2026-06-30", ann_date="2026-08-20")
    out = compute_b_class_metrics([_bar("2026-08-31")], [fin])
    assert out[0]["float_share"] is not None


def test_not_visible_before_ann_date_normal() -> None:
    """正常披露下 ann_date 前同样不可见（无泄漏）。"""
    fin = _fin(report_date="2026-06-30", ann_date="2026-08-20")
    out = compute_b_class_metrics([_bar("2026-08-19")], [fin])
    assert out[0]["float_share"] is None


# ── ann_date 缺失回退 ─────────────────────────────────────────
def test_missing_ann_date_falls_back_to_deadline() -> None:
    """ann_date 缺失 → report_deadline 推定（历史数据兼容，行为不变）。"""
    fin = {
        "report_date": "2026-06-30",
        "ann_date": None,  # 历史行无公告日期
        "circ_mv": 1e11, "total_mv": 1.2e11, "net_profit": 1e9,
    }
    out = compute_b_class_metrics(
        [_bar("2026-08-30"), _bar("2026-08-31")], [fin]
    )
    # 8-30 < deadline(8-31) → 不可见；8-31 >= deadline → 可见
    assert out[0]["float_share"] is None
    assert out[1]["float_share"] is not None


# ── 同截止日多报告期 ─────────────────────────────────────────
def test_same_deadline_prefers_latest_report() -> None:
    """年报(12-31)与一季报(03-31)同于 4-30 截止：
    4-30 之后取最新报告期=一季报（match_latest_financial 次键语义）。"""
    annual = {
        "report_date": "2025-12-31", "ann_date": "2026-04-30",
        "circ_mv": 1e11, "total_mv": 1.2e11, "net_profit": 4e9,
    }
    q1 = {
        "report_date": "2026-03-31", "ann_date": "2026-04-30",
        "circ_mv": 1e11, "total_mv": 1.2e11, "net_profit": 1e9,
    }
    out = compute_b_class_metrics([_bar("2026-05-05")], [annual, q1])
    # 一季报优先：net=1e9 × 4 年化 = 4e9；cur_total=1.2e11/close(10)=1.2e10
    # pe_dynamic = close(10) × 1.2e10 / 4e9 = 30
    assert abs(out[0]["pe_dynamic"] - 30.0) < 0.5
