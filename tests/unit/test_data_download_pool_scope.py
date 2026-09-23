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

    def update_symbols_batch(self, symbols, period="daily", adjust="hfq", progress_callback=None, force_full=False) -> List[Dict[str, Any]]:
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
