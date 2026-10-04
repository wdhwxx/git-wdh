# -*- coding: utf-8 -*-
"""T-42 KP-P2-06 参数扫描外生池：不依赖回测成交笔数，分层流动性池 + selection_bias_note。"""
import os

import pandas as pd

from myquant.backtest.kunpeng_param_scanner import KunpengParamScanner


def _make(tmp_path, name, dates, amount):
    df = pd.DataFrame({"日期": dates, "成交额": amount, "收盘价": [10.0] * len(dates), "成交量": [1000] * len(dates)})
    df.to_parquet(tmp_path / name)


def _scanner(tmp_path):
    return KunpengParamScanner(processed_dir=str(tmp_path))


def test_exogenous_pool_stratified(tmp_path):
    _make(tmp_path, "HI.SZ.parquet", [20231001 + i for i in range(30)], [10000.0] * 30)
    _make(tmp_path, "MID.SZ.parquet", [20231001 + i for i in range(30)], [100.0] * 30)
    _make(tmp_path, "LO.SZ.parquet", [20231001 + i for i in range(30)], [1.0] * 30)
    _make(tmp_path, "LATE.SZ.parquet", [20240201 + i for i in range(30)], [999.0] * 30)
    syms, meta = _scanner(tmp_path).build_exogenous_pool(top_n=2, liquidity_window_days=60)
    assert "HI.SZ" in syms
    assert "LATE.SZ" not in syms
    assert meta["method"] == "exogenous_liquidity_stratified"
    assert meta["n_scored"] == 3
    assert meta["n_pool"] == 2
    assert meta["window_days"] == 60


def test_meta_selection_bias_note(tmp_path):
    _make(tmp_path, "A.SZ.parquet", [20231001 + i for i in range(30)], [50.0] * 30)
    _, meta = _scanner(tmp_path).build_exogenous_pool(top_n=1)
    assert "外生池" in meta["selection_bias_note"]
    assert "内生挑选" in meta["selection_bias_note"]
    assert "KP-P2-07" in meta["selection_bias_note"]


def test_empty_dir_returns_empty(tmp_path):
    syms, meta = _scanner(tmp_path).build_exogenous_pool(top_n=5)
    assert syms == []
    assert meta["n_scored"] == 0
    assert meta["n_pool"] == 0


def test_no_hist_before_start_skipped(tmp_path):
    _make(tmp_path, "X.SZ.parquet", [20240301 + i for i in range(10)], [5.0] * 10)
    syms, meta = _scanner(tmp_path).build_exogenous_pool(top_n=5)
    assert syms == []
    assert meta["n_scored"] == 0


def test_load_active_symbols_compat_exogenous(tmp_path):
    _make(tmp_path, "A.SZ.parquet", [20231001 + i for i in range(30)], [50.0] * 30)
    _make(tmp_path, "B.SZ.parquet", [20231001 + i for i in range(30)], [5.0] * 30)
    syms = _scanner(tmp_path).load_active_symbols(top_n=1)
    assert syms == ["A.SZ"]


def test_run_meta_records_pool():
    """run 的 meta 含 pool 段（外生池构建依据 + selection_bias_note）。"""
    src = os.path.join(os.path.dirname(__file__), "..", "..", "myquant", "backtest", "kunpeng_param_scanner.py")
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "build_exogenous_pool(top_n=top_n)" in text
    assert '"pool": pool_meta' in text
    assert "selection_bias_note" in text