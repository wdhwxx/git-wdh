# -*- coding: utf-8 -*-
"""T-47 KP-P2-09：复权因子缺失不再静默降级（adjust_strict）+ 结果 adjust_effective 落盘。"""
from pathlib import Path

import pandas as pd
import pytest

from myquant.backtest.kunpeng_param_scanner import KunpengParamScanner
from myquant.data_access.provider import HistoricalDataProvider

ROOT = Path(__file__).resolve().parents[2]


def _df() -> pd.DataFrame:
    df = pd.DataFrame(
        {"开盘价": [10.0], "最高价": [10.5], "最低价": [9.5], "收盘价": [10.0]},
        index=[pd.Timestamp("2024-01-02")],
    )
    df.index.name = "日期"
    return df


def _provider(tmp_path) -> HistoricalDataProvider:
    return HistoricalDataProvider(processed_dir=str(tmp_path), default_adjust="raw")


def test_adjust_strict_raises_when_no_factor(monkeypatch, tmp_path):
    p = _provider(tmp_path)
    monkeypatch.setattr(p, "_get_factor_lookup", lambda symbol, as_of=None: {})
    with pytest.raises(ValueError, match="adjust_strict"):
        p._apply_factor_adjust(_df(), "000001.SZ", "hfq", "2024-01-05", adjust_strict=True)


def test_degrade_sets_attrs_raw(monkeypatch, tmp_path):
    p = _provider(tmp_path)
    monkeypatch.setattr(p, "_get_factor_lookup", lambda symbol, as_of=None: {})
    out = p._apply_factor_adjust(_df(), "000001.SZ", "hfq", None)
    assert out.attrs.get("adjust_effective") == "raw"


def test_applied_sets_attrs_hfq(monkeypatch, tmp_path):
    p = _provider(tmp_path)
    monkeypatch.setattr(p, "_get_factor_lookup", lambda symbol, as_of=None: {"2024-01-01": 1.5})
    out = p._apply_factor_adjust(_df(), "000001.SZ", "hfq", None)
    assert out.attrs.get("adjust_effective") == "hfq"


def test_scanner_reads_yaml_adjust():
    """扫描器 load_data 复权改读 config/backtest_kunpeng.yaml data.adjust"""
    scanner = KunpengParamScanner()
    assert scanner._read_data_adjust() in ("raw", "hfq", "qfq")
    yaml_path = ROOT / "config" / "backtest_kunpeng.yaml"
    if yaml_path.exists():
        import yaml
        cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert scanner._read_data_adjust() == str(cfg["data"]["adjust"])


def test_scanner_meta_adjust_effective_src():
    """扫描器结果 meta 落盘 adjust_effective"""
    src = (ROOT / "myquant" / "backtest" / "kunpeng_param_scanner.py").read_text(encoding="utf-8")
    assert '"adjust_effective": self._active_adjust' in src
    assert "_read_data_adjust" in src