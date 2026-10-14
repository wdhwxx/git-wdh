# 文件名：tests/unit/test_p2_anti_future_config.py
"""
P2（#83）批1纯修复单测：
  P2-② 财务兜底 report_date=今天 丢弃+留痕（防未来函数，akshare_source 三处）
  P2-③ 财务排除规则 config 化（_load_financial_exclude_rules）
  P2-⑤ 量比窗口跳过 0 量日（compute_derived_metrics）
"""

from __future__ import annotations

import types
from pathlib import Path

import pandas as pd

from myquant.data_access.cleaner.derived_metrics import compute_derived_metrics
from myquant.data_access.financial_sync_service import _load_financial_exclude_rules
from myquant.data_access.sources.akshare_source import AkshareDataSource


class _FakeResp:
    def __init__(self, payload: dict | None = None, text: str = ""):
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class TestP2FinanceNoFakeReportDate:
    def test_eastmoney_push2_dropped(self, monkeypatch):
        """P2-②：东财 push2 实时估值（report_date=今天）→ 丢弃，不产记录。"""

        def fake_em(symbol):
            return pd.DataFrame(
                [["市盈率-动态", 12.5], ["市净率", 1.2], ["总市值", 100000000.0]],
                columns=["item", "value"],
            )

        def fake_get(url, params=None, headers=None, timeout=10):
            return _FakeResp(payload={"result": {"data": []}})

        monkeypatch.setattr("requests.get", fake_get)
        ak = types.SimpleNamespace(stock_individual_info_em=fake_em)
        src = AkshareDataSource()
        res = src._try_eastmoney_finance(ak, "600519.SH", "600519", None)
        # P0-02修复：push2快照丢弃后必须返回 None（原 return [] 被 composite 误判为"成功"，
        # 短路掉 datacenter-web/新浪/百度全部降级源）；None 才会让链路继续尝试备源。
        assert res is None


class TestP2ExcludeRulesConfig:
    def test_load_from_yaml(self, tmp_path: Path):
        """P2-③：读 config/financial_exclude_rules.yaml。"""
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "financial_exclude_rules.yaml").write_text(
            "exclude_rules:\n  - \"688.SH\"\n  - \"99\"\n", encoding="utf-8"
        )
        rules = _load_financial_exclude_rules(tmp_path)
        assert rules == ["688.SH", "99"]

    def test_fallback_builtin(self, tmp_path: Path):
        """P2-③：config 缺失 → 回退内置 14 条（不阻断）。"""
        from myquant.data_access.financial_sync_service import FINANCIAL_EXCLUDE_RULES

        rules = _load_financial_exclude_rules(tmp_path)
        assert rules == list(FINANCIAL_EXCLUDE_RULES)
        assert len(rules) == 14


class TestP2VolumeRatioSkipZeroDays:
    def test_zero_volume_day_excluded_from_avg(self):
        """P2-⑤：窗口含 0 量日时均量跳过 0 量日（量比不虚高）。"""
        records = [
            {"date": "2024-06-13", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1000.0},
            {"date": "2024-06-14", "open": 10.2, "high": 10.3, "low": 9.9, "close": 10.0, "volume": 0.0},
            {"date": "2024-06-17", "open": 10.0, "high": 10.8, "low": 9.9, "close": 10.7, "volume": 2000.0},
            {"date": "2024-06-18", "open": 10.7, "high": 11.0, "low": 10.5, "close": 10.9, "volume": 3000.0},
            {"date": "2024-06-19", "open": 10.9, "high": 11.2, "low": 10.8, "close": 11.0, "volume": 4000.0},
            {"date": "2024-06-20", "open": 11.0, "high": 11.5, "low": 10.9, "close": 11.3, "volume": 5000.0},
        ]
        out = compute_derived_metrics(records)
        # 06-20 窗口=06-13..06-19，0 量日被跳过 → 均量=(1000+2000+3000+4000)/4=2500
        assert out[-1]["volume_ratio"] == 2.0  # 5000/2500

    def test_all_zero_window_ratio_none(self):
        """P2-⑤：窗口全 0 量 → 量比 None（避免除零）。"""
        records = [
            {"date": "2024-06-13", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 0.0},
            {"date": "2024-06-14", "open": 10.2, "high": 10.3, "low": 9.9, "close": 10.0, "volume": 0.0},
            {"date": "2024-06-17", "open": 10.0, "high": 10.8, "low": 9.9, "close": 10.7, "volume": 0.0},
            {"date": "2024-06-18", "open": 10.7, "high": 11.0, "low": 10.5, "close": 10.9, "volume": 0.0},
            {"date": "2024-06-19", "open": 10.9, "high": 11.2, "low": 10.8, "close": 11.0, "volume": 0.0},
            {"date": "2024-06-20", "open": 11.0, "high": 11.5, "low": 10.9, "close": 11.3, "volume": 5000.0},
        ]
        out = compute_derived_metrics(records)
        assert out[-1]["volume_ratio"] is None
