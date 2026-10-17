# 文件名：tests/unit/test_derived_parity_incremental_full.py
"""B-08 全量/增量派生量口径统一单测（V-10 两路径逐字段等价）。

覆盖：
  1. fin_b 补 ann_date 后：同一财务列表（含 ann_date），全量路径（fin 完整）
     与增量路径（fin_b 同构）过 compute_b_class_metrics → 逐字段等价；
  2. 晚披露样本：两路径 ann_date 前均不可见（float_share/pe_dynamic/turnover 同 None）；
  3. turnover 口径：两路径逐值相等；
  4. _with_factor_lookup：config 副本注入 factor_lookup，原 config 不被污染（子进程池安全）。
"""

from __future__ import annotations

import pytest

from myquant.data_access.cleaner.derived_metrics import compute_b_class_metrics
from myquant.data_access.full_clean_service import _with_factor_lookup


def _bars() -> list:
    """与增量/全量同构的行情记录（trade_date 归一后）。"""
    return [
        {"trade_date": "2026-08-31", "close": 10.0, "volume": 1e6},
        {"trade_date": "2026-09-07", "close": 10.5, "volume": 1.2e6},
    ]


def _fin_records(with_ann: bool = True) -> list:
    """财务记录：ann_date 晚于 report_deadline 的晚披露样本（2026-09-05 > 8-31）。"""
    f = {
        "report_date": "2026-06-30",
        "circ_mv": 1e11,
        "total_mv": 1.2e11,
        "net_profit": 1e9,
    }
    if with_ann:
        f["ann_date"] = "2026-09-05"
    return [f]


def _fin_b(fin_records: list) -> list:
    """模拟 manager.py 增量 fin_b 构造（B-08 修复后：含 ann_date）。"""
    return [
        {
            "report_date": f.get("report_date"),
            "ann_date": f.get("ann_date"),
            "circ_mv": f.get("circ_mv"),
            "total_mv": f.get("total_mv"),
            "net_profit": f.get("net_profit"),
        }
        for f in fin_records
    ]


# ── 两路径逐字段等价（V-10）──────────────────────────
def test_full_vs_incremental_parity() -> None:
    """同一财务输入（含 ann_date）：全量 vs 增量输出逐字段等价。"""
    fin = _fin_records(with_ann=True)
    bars = _bars()
    full_out = compute_b_class_metrics(bars, fin)
    incr_out = compute_b_class_metrics(bars, _fin_b(fin))
    for full_rec, incr_rec in zip(full_out, incr_out):
        for key in ("turnover", "float_share", "pe_dynamic"):
            assert full_rec[key] == incr_rec[key], f"{key} 分叉: {full_rec[key]} != {incr_rec[key]}"


def test_late_disclosure_parity_both_paths() -> None:
    """晚披露（ann_date=09-05 > deadline=08-31）：两路径 8-31 均不可见、9-07 均可见。"""
    fin = _fin_records(with_ann=True)
    bars = _bars()
    full_out = compute_b_class_metrics(bars, fin)
    incr_out = compute_b_class_metrics(bars, _fin_b(fin))
    for out in (full_out, incr_out):
        assert out[0]["float_share"] is None      # 8-31 < ann_date
        assert out[0]["pe_dynamic"] is None
        assert out[0]["turnover"] is None
        assert out[1]["float_share"] is not None  # 9-07 >= ann_date
        assert out[1]["pe_dynamic"] is not None


def test_ann_date_missing_fallback_parity() -> None:
    """ann_date 缺失：两路径均回退 report_deadline（历史数据兼容一致）。"""
    fin = _fin_records(with_ann=False)
    bars = [
        {"trade_date": "2026-08-30", "close": 10.0, "volume": 1e6},
        {"trade_date": "2026-08-31", "close": 10.1, "volume": 1.1e6},
    ]
    full_out = compute_b_class_metrics(bars, fin)
    incr_out = compute_b_class_metrics(bars, _fin_b(fin))
    assert full_out[0]["float_share"] is None      # 8-30 < deadline(8-31)
    assert full_out[1]["float_share"] is not None  # 8-31 >= deadline
    for full_rec, incr_rec in zip(full_out, incr_out):
        assert full_rec["float_share"] == incr_rec["float_share"]
        assert full_rec["pe_dynamic"] == incr_rec["pe_dynamic"]


def test_turnover_parity() -> None:
    """turnover 口径两路径逐值相等。"""
    fin = _fin_records(with_ann=True)
    bars = _bars()
    full_out = compute_b_class_metrics(bars, fin)
    incr_out = compute_b_class_metrics(bars, _fin_b(fin))
    assert full_out[1]["turnover"] == incr_out[1]["turnover"]
    assert full_out[1]["turnover"] is not None
    # 手动复算：volume / float_share × 100
    fs = full_out[1]["float_share"]
    assert abs(full_out[1]["turnover"] - 1.2e6 / fs * 100.0) < 0.01


# ── factor_lookup 注入 ─────────────────────────────────────────
def test_with_factor_lookup_copy_no_pollution() -> None:
    """config 副本注入 factor_lookup；原 config 不被污染（子进程池安全）。"""
    clean_config = {"bar": {"max_price_jump_pct": 0.22}}
    factor_lookup = {"2026-06-30": 1.5}
    injected = _with_factor_lookup(clean_config, factor_lookup)
    # 副本生效
    assert injected["bar"]["factor_lookup"] == factor_lookup
    assert injected["bar"]["max_price_jump_pct"] == 0.22
    # 原 config 无污染
    assert "factor_lookup" not in clean_config["bar"]
    assert clean_config["bar"]["max_price_jump_pct"] == 0.22


def test_factor_lookup_empty_keeps_original() -> None:
    """factor_lookup 为空时不动 config（增量路径 factor_lookup 空 → 原配置直通）。"""
    clean_config = {"bar": {"detect_price_jump": True}}
    assert _with_factor_lookup(clean_config, {})["bar"].get("factor_lookup") == {}
    # 空注入仍给副本加空因子（清洗器幂等）；原配置不变
    assert "factor_lookup" not in clean_config["bar"]
