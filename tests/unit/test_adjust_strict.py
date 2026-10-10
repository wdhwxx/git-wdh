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
    assert scanner._read_data_adjust_config()[0] in ("raw", "hfq", "qfq")
    yaml_path = ROOT / "config" / "backtest_kunpeng.yaml"
    if yaml_path.exists():
        import yaml
        cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert scanner._read_data_adjust_config()[0] == str(cfg["data"]["adjust"])


def test_scanner_meta_adjust_effective_src():
    """扫描器结果 meta 落盘 adjust_effective"""
    src = (ROOT / "myquant" / "backtest" / "kunpeng_param_scanner.py").read_text(encoding="utf-8")
    assert '"adjust_effective": self._active_adjust' in src
    assert "_read_data_adjust_config" in src


# ═══════════════════════════════════════════
# M1A-05（v3.0 §6.4.5/§6.4.7-B/§6.4.6）：qfq 穿越告警 + 降级可观测 + PIT 访问
# ═══════════════════════════════════════════


def test_qfq_without_end_date_warns(caplog, monkeypatch, tmp_path):
    """end_date=None + qfq：显式告警（历史价被未来除权信息缩放，穿越风险）。"""
    import logging
    p = _provider(tmp_path)
    monkeypatch.setattr(p, "_get_factor_lookup", lambda symbol, as_of=None: {"2024-01-01": 1.5})
    with caplog.at_level(logging.WARNING, logger="data_access.provider"):
        p._apply_factor_adjust(_df(), "000001.SZ", "qfq", None)
    assert any("end_date 缺失" in r.message for r in caplog.records)


def test_degradation_observable_set_and_audit(monkeypatch, tmp_path):
    """降级事件：记可观测集合 + 落审计（M1A-05 §6.4.7-B，不再静默）。"""
    from myquant.data_access import provider as provider_mod

    monkeypatch.setattr(provider_mod, "_adjust_degraded_symbols", set())
    calls: list = []

    class _FakeAuditStore:
        def append(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr("myquant.data_access.provider.AuditLogStore", lambda: _FakeAuditStore())
    p = _provider(tmp_path)
    monkeypatch.setattr(p, "_get_factor_lookup", lambda symbol, as_of=None: {})
    out = p._apply_factor_adjust(_df(), "000001.SZ", "hfq", None)
    assert out.attrs.get("adjust_effective") == "raw"
    assert "000001.SZ" in provider_mod._adjust_degraded_symbols
    assert calls, "降级事件必须落 audit_log"


def test_default_adjust_strict_from_constructor(monkeypatch, tmp_path):
    """default_adjust_strict 构造参数生效：无因子时拒绝静默降级。"""
    p = HistoricalDataProvider(
        processed_dir=str(tmp_path), default_adjust="raw", default_adjust_strict=True
    )
    monkeypatch.setattr(p, "_get_factor_lookup", lambda symbol, as_of=None: {})
    with pytest.raises(ValueError, match="adjust_strict"):
        p._apply_factor_adjust(_df(), "000001.SZ", "hfq", "2024-01-05")


def test_load_asof_filters_future_bars(tmp_path, monkeypatch):
    """load_asof：只返回 <= decision_time 的 bar（PIT 防未来）。"""
    p = _provider(tmp_path)
    called = {}

    def fake_get_bars(symbol, start_date=None, end_date=None, period="daily", adjust=None, **kw):
        called["end_date"] = end_date
        return _df()

    monkeypatch.setattr(p, "get_bars", fake_get_bars)
    out = p.load_asof("000001.SZ", "2024-01-03", start_date="2024-01-01")
    assert called["end_date"] == "2024-01-03"
    assert not out.empty


def test_asof_join_backward_pit_semantics():
    """asof_join backward：左表每行取右表 <= 该日期的最近行（防未来）。"""
    left = pd.DataFrame(
        {"日期": pd.to_datetime(["2024-01-02", "2024-01-05"]), "价": [1.0, 2.0]}
    )
    right = pd.DataFrame(
        {"日期": pd.to_datetime(["2024-01-01", "2024-01-04"]), "因子": [10.0, 20.0]}
    )
    out = HistoricalDataProvider.asof_join(left, right)
    assert list(out["因子"].values) == [10.0, 20.0]  # 1/2→1/1 因子10；1/5→1/4 因子20


def test_asof_join_empty_right_returns_left_copy():
    left = pd.DataFrame({"日期": pd.to_datetime(["2024-01-02"]), "价": [1.0]})
    right = pd.DataFrame()
    out = HistoricalDataProvider.asof_join(left, right)
    assert list(out["价"].values) == [1.0]