# 文件名：tests/unit/test_data_download_pool_scope.py
"""GUI 数据下载"股票池范围"单测。

验证方案 A：日线全量/增量下载的标的来源由"全市场 get_stock_list()"改为
"配置股票池（backtest_kunpeng.yaml 的 3226 三因子池）"，避免把 processed 扩成全市场。
核心保证：读池代码、池文件缺失/解析失败/空池时返回空列表（不回退全市场）、
run_daily 以池列表为下载标的范围。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from myquant.gui.data_download_dialog import _load_pool_codes, run_daily

# ── _load_pool_codes ──


def _write_pool(tmp_path: Path, rel: str, content: str) -> Path:
    """写入池配置文件并返回路径。"""
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_读取配置股票池代码(tmp_path: Path) -> None:
    """正常配置文件 → 返回 codes 列表。"""
    _write_pool(tmp_path, "config/backtest_kunpeng.yaml",
                "stock_pool:\n  codes:\n    - 000001.SZ\n    - 600519.SH\n")
    assert _load_pool_codes(tmp_path) == ["000001.SZ", "600519.SH"]


def test_文件缺失返回空列表(tmp_path: Path) -> None:
    """配置文件不存在 → 返回空列表（不回退全市场）。"""
    assert _load_pool_codes(tmp_path) == []


def test_解析失败返回空列表(tmp_path: Path) -> None:
    """YAML 解析失败 → 返回空列表。"""
    _write_pool(tmp_path, "config/backtest_kunpeng.yaml", ": : : bad yaml\n")
    assert _load_pool_codes(tmp_path) == []


def test_空池返回空列表(tmp_path: Path) -> None:
    """codes 为空 → 返回空列表。"""
    _write_pool(tmp_path, "config/backtest_kunpeng.yaml", "stock_pool:\n  codes: []\n")
    assert _load_pool_codes(tmp_path) == []


def test_自定义池配置文件(tmp_path: Path) -> None:
    """传入 pool_config 相对路径 → 从该文件读取。"""
    _write_pool(tmp_path, "config/mypool.yaml", "stock_pool:\n  codes:\n    - 000001.SZ\n")
    assert _load_pool_codes(tmp_path, "config/mypool.yaml") == ["000001.SZ"]


# ── run_daily 标的范围 ──


class _FakeManager:
    """伪 manager：记录 update_symbols_batch 收到的标的列表。"""

    def __init__(self) -> None:
        self.symbols: List[str] = []

    def update_symbols_batch(self, symbols, period="daily", adjust="raw", progress_callback=None, force_full=False) -> List[Dict[str, Any]]:
        """记录标的并返回空结果。"""
        self.symbols = list(symbols)
        return []

    def save_cleaned_incremental_parquet(self, symbol, start_date=None, end_date=None) -> Optional[str]:
        """不做实际写入。"""
        return None


def test_run_daily_用池列表而非全市场(monkeypatch, tmp_path: Path) -> None:
    """run_daily 的标的范围取自配置股票池（非 get_stock_list 全市场）。"""
    import myquant.gui.data_download_dialog as d

    def fake_load(project_root: Path, pool_config: Optional[str] = None) -> List[str]:
        return ["000001.SZ", "600519.SH"]

    monkeypatch.setattr(d, "_load_pool_codes", fake_load)
    manager = _FakeManager()
    # fetcher=None：若 run_daily 仍调 get_stock_list() 会 AttributeError，从而证明改用了池
    res = run_daily(fetcher=None, manager=manager, project_root=tmp_path)
    assert manager.symbols == ["000001.SZ", "600519.SH"]
    assert res["total"] == 0  # update_symbols_batch 返回空
# ── run_daily 全市场范围（scope="full_market"，通达信全量数据）──


class _FakeFetcher:
    """伪 fetcher：get_stock_list 返回全市场列表。"""

    def get_stock_list(self, market: str = "A股") -> List[Dict[str, Any]]:
        """返回全市场 A 股列表（全量入库标的范围）。"""
        return [
            {"symbol": "000001.SZ"},
            {"symbol": "600519.SH"},
            {"symbol": "601398.SH"},
        ]


def test_run_daily_全市场范围_用get_stock_list(tmp_path: Path) -> None:
    """scope='full_market'：标的取全市场 get_stock_list()，不写 processed。"""

    manager = _FakeManager()
    fetcher = _FakeFetcher()
    res = run_daily(
        fetcher=fetcher, manager=manager, project_root=tmp_path,
        force_full=True, scope="full_market",
    )
    assert manager.symbols == ["000001.SZ", "600519.SH", "601398.SH"]
    assert res["mode"] == "full_market"
    assert "parquet_saved" not in res  # 全市场入库不写 processed


def test_run_daily_全市场_缺fetcher抛错(tmp_path: Path) -> None:
    """scope='full_market' 但 fetcher=None → 明确报错（防静默空转）。"""

    manager = _FakeManager()
    with pytest.raises(ValueError, match="full_market"):
        run_daily(fetcher=None, manager=manager, project_root=tmp_path, scope="full_market")


# ── 增量范围实时过滤（resolve_incremental_symbols 接入）与缺口可见 ──


def _write_run_json(tmp_path: Path) -> None:
    """写入 config/data_clean_run.json（14 条规则口径测试替身，含 68/000.SH）。"""
    path = tmp_path / "config" / "data_clean_run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps({
        "exclude_rules": [
            "000.SH", "399", "880", "5.SH", "1.SZ", "30.SZ", "688.SH",
            "4", "8", "92", "68", "90", "20", "99",
        ],
        "exclude_st": False,
    }), encoding="utf-8")


def test_run_daily_池内规则实时过滤(monkeypatch, tmp_path: Path) -> None:
    """池内标的经清洗规则实时过滤后才是下载范围（规则外不进 processed）。"""
    import myquant.gui.data_download_dialog as d

    def fake_load(project_root: Path, pool_config: Optional[str] = None) -> List[str]:
        return ["000001.SZ", "000300.SH", "689009.SH", "600519.SH"]

    monkeypatch.setattr(d, "_load_pool_codes", fake_load)
    _write_run_json(tmp_path)
    manager = _FakeManager()
    res = run_daily(fetcher=None, manager=manager, project_root=tmp_path)
    # 000300.SH(000.SH) / 689009.SH(68) 被剔除 → 只下载规则内标的
    assert manager.symbols == ["000001.SZ", "600519.SH"]
    assert res["pool_original"] == 4
    assert res["pool_after"] == 2


class _FakeManagerWithResults(_FakeManager):
    """伪 manager：update_symbols_batch 返回预设结果（no_data/failed 缺口可见）。"""

    def __init__(self, results: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._results = results

    def update_symbols_batch(self, symbols, period="daily", adjust="raw", progress_callback=None, force_full=False) -> List[Dict[str, Any]]:
        self.symbols = list(symbols)
        return self._results


def test_run_daily_no_data缺口进汇总(monkeypatch, tmp_path: Path) -> None:
    """下载区间无数据（no_data）→ 逐只携带请求区间进汇总（缺口可见）。"""
    import myquant.gui.data_download_dialog as d

    def fake_load(project_root: Path, pool_config: Optional[str] = None) -> List[str]:
        return ["000001.SZ", "600519.SH"]

    monkeypatch.setattr(d, "_load_pool_codes", fake_load)
    _write_run_json(tmp_path)
    manager = _FakeManagerWithResults([
        {
            "symbol": "000001.SZ", "status": "no_data", "record_count": 0,
            "start_date": "2026-09-29", "end_date": "2026-10-01",
            "data": None, "error_message": "未获取到数据",
        },
        {
            "symbol": "600519.SH", "status": "completed", "record_count": 1,
            "start_date": "2026-09-29", "end_date": "2026-09-29",
            "data": [{"date": "2026-09-29"}],
        },
    ])
    res = run_daily(fetcher=None, manager=manager, project_root=tmp_path)
    assert len(res["no_data_symbols"]) == 1
    nd = res["no_data_symbols"][0]
    assert nd["symbol"] == "000001.SZ"
    assert nd["start_date"] == "2026-09-29"
    assert nd["end_date"] == "2026-10-01"


def test_run_daily_失败明细进汇总(monkeypatch, tmp_path: Path) -> None:
    """拉取异常（failed）→ 错误明细进汇总（缺口可见）。"""
    import myquant.gui.data_download_dialog as d

    def fake_load(project_root: Path, pool_config: Optional[str] = None) -> List[str]:
        return ["000001.SZ"]

    monkeypatch.setattr(d, "_load_pool_codes", fake_load)
    _write_run_json(tmp_path)
    manager = _FakeManagerWithResults([
        {
            "symbol": "000001.SZ", "status": "failed", "record_count": 0,
            "start_date": None, "end_date": None,
            "data": None, "error_message": "磁盘 IO 异常",
        },
    ])
    res = run_daily(fetcher=None, manager=manager, project_root=tmp_path)
    assert len(res["failed_symbols"]) == 1
    assert res["failed_symbols"][0]["symbol"] == "000001.SZ"
    assert "磁盘 IO 异常" in res["failed_symbols"][0]["error"]
