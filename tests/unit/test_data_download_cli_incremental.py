# 文件名：tests/unit/test_data_download_cli_incremental.py
"""CLI 数据下载增量链路单测（A1 修复，蓝图 #86）。

覆盖：
  - download_daily 标的范围 = 全市场候选 → 清洗规则实时过滤（池外创业板/科创板/北交所不下载）
  - processed 写回走 manager.save_cleaned_incremental_parquet（清洗链路，复用已拉 records，无二次拉取）
  - 清洗写回失败逐只计数、不阻断批次
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # tests/unit → 项目根


def _load_script(name: str) -> Any:
    """加载 scripts 下的中文名脚本模块（scripts 非包，用 importlib 文件加载）。"""
    path = PROJECT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _write_run_json(tmp_path: Path) -> None:
    """写入 config/data_clean_run.json（14 条规则口径测试替身，含 68/90/20/99）。"""
    path = tmp_path / "config" / "data_clean_run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "exclude_rules": [
            "000.SH", "399", "880", "5.SH", "1.SZ", "30.SZ", "688.SH",
            "4", "8", "92", "68", "90", "20", "99",
        ],
        "exclude_st": False,
        "exclude_suspended_days": 0,
    }), encoding="utf-8")


class _FakeFetcher:
    """伪 fetcher：get_stock_list 返回含池内外标的的全市场列表。"""

    def __init__(self, stocks: List[Dict[str, str]]) -> None:
        self._stocks = stocks
        self.get_market_data_called = 0

    def get_stock_list(self, market: str = "A股") -> List[Dict[str, str]]:
        """返回全市场列表。"""
        return self._stocks

    def get_market_data(self, symbol, start_date=None, end_date=None):
        """若被调用则计数（A1 后不应再出现二次拉取）。"""
        self.get_market_data_called += 1
        return None


class _FakeManager:
    """伪 manager：记录 update_symbols_batch 标的与 save_cleaned 调用。"""

    def __init__(self, results: List[Dict[str, Any]]) -> None:
        self._results = results
        self.symbols: List[str] = []
        self.saved: List[Dict[str, Any]] = []

    def update_symbols_batch(self, symbols, period="daily", adjust="raw", progress_callback=None) -> List[Dict[str, Any]]:
        """记录标的并返回预设结果。"""
        self.symbols = list(symbols)
        return self._results

    def save_cleaned_incremental_parquet(self, symbol, start_date=None, end_date=None, records=None) -> Optional[str]:
        """记录调用并返回假路径。"""
        self.saved.append({"symbol": symbol, "records": records})
        return "/tmp/x.parquet"


def _make_stocks() -> List[Dict[str, str]]:
    """全市场候选列表：2 只主板 + 创业板/科创板/北交所各 1 只。"""
    return [
        {"symbol": "000001.SZ"},
        {"symbol": "000002.SZ"},
        {"symbol": "300001.SZ"},    # 创业板 → 排除
        {"symbol": "688001.SH"},    # 科创板 → 排除
        {"symbol": "920001.BJ"},    # 北交所 → 排除
    ]


def test_cli_download_池过滤只进清洗池(monkeypatch, tmp_path: Path) -> None:
    """download_daily 标的范围经 14 条规则过滤：池外市场不下载不写 processed。"""
    _write_run_json(tmp_path)
    mod = _load_script("数据下载")
    fetcher = _FakeFetcher(_make_stocks())
    manager = _FakeManager([])
    res = mod.download_daily(fetcher, manager, tmp_path)
    assert manager.symbols == ["000001.SZ", "000002.SZ"]
    assert res["pool_original"] == 5
    assert res["pool_after"] == 2


def test_cli_download_写回走清洗链路复用记录(monkeypatch, tmp_path: Path) -> None:
    """completed 且 record_count>0 → save_cleaned_incremental_parquet 复用已拉 records，无二次拉取。"""
    _write_run_json(tmp_path)
    mod = _load_script("数据下载")
    fetcher = _FakeFetcher(_make_stocks())
    records = [{"date": "2026-09-30", "open": 1.0, "close": 1.1}]
    manager = _FakeManager([
        {
            "symbol": "000001.SZ", "status": "completed", "record_count": 1,
            "start_date": "2026-09-29", "end_date": "2026-09-30",
            "data": records,
        },
    ])
    res = mod.download_daily(fetcher, manager, tmp_path)
    assert res["parquet_saved"] == 1
    assert res["save_failed"] == 0
    assert len(manager.saved) == 1
    assert manager.saved[0]["symbol"] == "000001.SZ"
    assert manager.saved[0]["records"] == records  # 复用已拉记录
    assert fetcher.get_market_data_called == 0  # 无二次拉取


def test_cli_download_写回失败逐只计数(monkeypatch, tmp_path: Path) -> None:
    """清洗写回异常 → save_failed 计数 + save_failed_symbols 明细，不阻断批次其他标的。"""
    _write_run_json(tmp_path)
    mod = _load_script("数据下载")

    class _FailManager(_FakeManager):
        def save_cleaned_incremental_parquet(self, symbol, start_date=None, end_date=None, records=None) -> Optional[str]:
            if symbol == "000001.SZ":
                raise RuntimeError("清洗失败")
            return "/tmp/x.parquet"

    fetcher = _FakeFetcher(_make_stocks())
    manager = _FailManager([
        {"symbol": "000001.SZ", "status": "completed", "record_count": 1,
         "start_date": "2026-09-29", "end_date": "2026-09-30", "data": [{"date": "2026-09-30"}]},
        {"symbol": "000002.SZ", "status": "completed", "record_count": 1,
         "start_date": "2026-09-29", "end_date": "2026-09-30", "data": [{"date": "2026-09-30"}]},
    ])
    res = mod.download_daily(fetcher, manager, tmp_path)
    assert res["save_failed"] == 1
    assert "000001.SZ" in res["save_failed_symbols"]
    assert res["parquet_saved"] == 1  # 另一只正常写回

