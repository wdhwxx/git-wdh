# 文件名：tests/unit/test_update_symbol_incremental_start.py
"""增量更新日线"缺失区间死锁"修复（M5）单测。

背景：sync_status 只代表"曾拉取成功"，processed 写回失败时 DB 领先，
若增量起点仅以 sync_status 为准，会永久跳过缺失区间（如 1117 只卡在
09-21 的脱节死锁）。M5 将增量起点改为 processed 实际最新交易日（权威），
processed 缺失时回退 DB；GUI 侧把"拉取成功"与"processed 实际写入"分开统计。

覆盖：
- _get_processed_last_date：解析/缺失/NaN/无日期列/非日线周期
- update_symbol 增量起点：脱节重拉/回退 DB/全量/已最新/正常增量
- run_daily：save 返回 None 记入 save_failed（不再静默吞掉）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

import myquant.data_access.manager as manager_mod
from myquant.data_access.manager import DataManager
from myquant.data_access.sources.base import MarketDataResult


def _fake_project_path(tmp_path: Path):
    def _resolve(*parts: str) -> Path:
        p = tmp_path
        for part in parts:
            p = p / part
        return p
    return _resolve


def _write_processed(tmp_path: Path, symbol: str, dates: List[Any]) -> Path:
    """写入最小 processed parquet（仅日期列），返回文件路径。"""
    out_dir = tmp_path / "datasets" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{symbol}.parquet"
    pd.DataFrame({"日期": dates}).to_parquet(out, index=False)
    return out


class _FakeFetcher:
    """伪 fetcher：记录增量起点并返回单条当日数据。"""

    def __init__(self) -> None:
        self.last_start_date: Optional[str] = None
        self.calls: List[Dict[str, Any]] = []

    def get_market_data(self, symbol, start_date=None, end_date=None, period="daily",
                        adjust="raw", use_cache=False) -> MarketDataResult:
        self.last_start_date = start_date
        self.calls.append({"start_date": start_date, "end_date": end_date})
        today = datetime.now().strftime("%Y-%m-%d")
        return MarketDataResult(
            symbol=symbol,
            data=[{"date": today, "open": 1.0, "high": 1.0, "low": 1.0,
                   "close": 1.0, "volume": 100, "amount": 100.0,
                   "pct_chg": 0.0, "turnover": 0.0, "is_suspended": 0}],
            source="fake",
        )

    def invalidate_cache(self, symbol) -> None:  # noqa: ARG002
        return None


# ── _get_processed_last_date ──


def test_processed_最新日期解析(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """int64 日期列 → YYYY-MM-DD。"""
    _write_processed(tmp_path, "000001.SZ", [20260921, 20260928])
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    m = DataManager(_FakeFetcher())
    assert m._get_processed_last_date("000001.SZ") == "2026-09-28"


def test_processed_文件缺失返回None(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 parquet 文件 → None（调用方回退 sync_status 或全量）。"""
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    m = DataManager(_FakeFetcher())
    assert m._get_processed_last_date("000001.SZ") is None


def test_processed_浮点日期含NaN取最大(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """float 日期列含 NaN → 取有效最大值。"""
    _write_processed(tmp_path, "000001.SZ", [20260921.0, float("nan"), 20260928.0])
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    m = DataManager(_FakeFetcher())
    assert m._get_processed_last_date("000001.SZ") == "2026-09-28"


def test_processed_无日期列返回None(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺"日期"列 → None（防御式校验）。"""
    out_dir = tmp_path / "datasets" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"收盘价": [1.0]}).to_parquet(out_dir / "000001.SZ.parquet", index=False)
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    m = DataManager(_FakeFetcher())
    assert m._get_processed_last_date("000001.SZ") is None


def test_processed_非日线周期返回None(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """period != daily → None（当前仅 daily 扁平 parquet 支持）。"""
    _write_processed(tmp_path, "000001.SZ", [20260928])
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    m = DataManager(_FakeFetcher())
    assert m._get_processed_last_date("000001.SZ", period="minute") is None


# ── update_symbol 增量起点 ──


def test_update_symbol_脱节重拉缺失区间(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M5核心：processed=09-21 而 DB sync=09-28（脱节）→ 增量起点以 processed 为准重拉。"""
    _write_processed(tmp_path, "000001.SZ", [20260921])  # 落后于 DB
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    monkeypatch.setattr(
        DataManager, "_get_last_sync_date",
        lambda self, symbol, period="daily", data_type="market": "2026-09-28",  # noqa: ARG005
    )
    fetcher = _FakeFetcher()
    m = DataManager(fetcher)
    m.update_symbol("000001.SZ")
    # processed 权威：从 processed+1 重拉缺失区间（09-22 起），而非 DB 的 09-29
    assert fetcher.last_start_date == "2026-09-22"


def test_update_symbol_processed缺失回退DB(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """processed 无文件 → 回退 DB sync 日期（昨天→今天增量）。"""
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(
        DataManager, "_get_last_sync_date",
        lambda self, symbol, period="daily", data_type="market": yesterday,  # noqa: ARG005
    )
    fetcher = _FakeFetcher()
    m = DataManager(fetcher)
    m.update_symbol("000001.SZ")
    today = datetime.now().strftime("%Y-%m-%d")
    assert fetcher.last_start_date == today


def test_update_symbol_无任何记录则全量(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """processed 与 DB 均无记录 → start=None（全量拉取）。"""
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    monkeypatch.setattr(
        DataManager, "_get_last_sync_date",
        lambda self, symbol, period="daily", data_type="market": None,  # noqa: ARG005
    )
    fetcher = _FakeFetcher()
    m = DataManager(fetcher)
    m.update_symbol("000001.SZ")
    assert fetcher.last_start_date is None


def test_update_symbol_已最新则跳过(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """processed=today → 下一交易日>今天 → skipped，不触发拉取。"""
    today = datetime.now().strftime("%Y-%m-%d")
    _write_processed(tmp_path, "000001.SZ", [int(today.replace("-", ""))])
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    fetcher = _FakeFetcher()
    m = DataManager(fetcher)
    res = m.update_symbol("000001.SZ")
    assert res["status"] == "skipped"
    assert fetcher.last_start_date is None


def test_update_symbol_正常增量起点为今天(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """processed=昨天 → 增量起点=今天（不跳过，拉当日）。"""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _write_processed(tmp_path, "000001.SZ", [int(yesterday.replace("-", ""))])
    monkeypatch.setattr(manager_mod, "get_project_path", _fake_project_path(tmp_path))
    fetcher = _FakeFetcher()
    m = DataManager(fetcher)
    res = m.update_symbol("000001.SZ")
    today = datetime.now().strftime("%Y-%m-%d")
    assert res["status"] == "completed"
    assert fetcher.last_start_date == today


# ── run_daily：save 返回 None 计入 save_failed ──


class _FakeSaveNoneManager:
    """伪 manager：拉取"成功"，但清洗写 processed 未产出文件（返回 None）。"""

    def update_symbols_batch(self, symbols, period="daily", adjust="raw",
                             progress_callback=None, force_full=False) -> List[Dict[str, Any]]:
        return [{
            "symbol": "000001.SZ",
            "status": "completed",
            "record_count": 1,
            "start_date": "2026-09-22",
            "end_date": "2026-09-28",
            "data": [{"date": "2026-09-28", "open": 1.0, "high": 1.0, "low": 1.0,
                      "close": 1.0, "volume": 100}],
        }]

    def save_cleaned_incremental_parquet(self, symbol, start_date=None,
                                         end_date=None, records=None) -> Optional[str]:
        # M5 场景：清洗未产出文件（原逻辑静默吞掉）
        return None


def test_run_daily_save返回None计入save_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """拉取 completed 但 processed 未更新 → save_failed=1 且留痕，不再虚报成功。"""
    import myquant.gui.data_download_dialog as d

    monkeypatch.setattr(d, "_load_pool_codes", lambda root, pool_config=None: ["000001.SZ"])
    res = d.run_daily(fetcher=None, manager=_FakeSaveNoneManager(), project_root=tmp_path)
    assert res["parquet_saved"] == 0
    assert res["save_failed"] == 1
    assert res["save_failed_symbols"] == ["000001.SZ"]
