# 文件名：tests/unit/test_data_incremental_clean_dual.py
"""增量清洗通道 run_incremental_clean 双写 DB + processed 单测。

验证 M0-M4 方案"仅清洗"模式（不重新拉取）：
  - records 传入 → 清洗后写 quant.db（INSERT OR REPLACE upsert）+ processed 扁平 parquet
  - 无记录 → 返回 no_records
  - 单标的失败隔离（一个标的异常不影响其他标的）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict

import pandas as pd
import pytest

from myquant.data_access import full_clean_service as fcs


def _bar(
    date: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100000.0,
    symbol: str = "TST.SZ",
) -> Dict[str, Any]:
    """构造一条含 symbol 的英文字段增量记录。"""
    return {
        "symbol": symbol, "date": date, "open": open_, "high": high,
        "low": low, "close": close, "volume": volume,
        "amount": round(open_ * volume, 2), "pct_chg": 0.0, "turnover": 0.5,
    }


class _MockFetcher:
    """M1 保证：增量清洗不应重新拉取数据源。"""

    def get_market_data(self, *args: Any, **kwargs: Any):
        raise AssertionError("run_incremental_clean 不应重新拉取数据源")


@pytest.fixture
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """装配：mock fetcher / processed 目录 / 清洗配置；DB 用真实临时文件（内部 migrate 建表）。"""
    import myquant.data_access.manager as mng

    db_path = tmp_path / "quant.db"
    processed = tmp_path / "datasets" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "myquant.data_access.factory.create_default_fetcher", lambda: _MockFetcher()
    )
    monkeypatch.setattr(
        "myquant.data_access.manager.get_project_path",
        lambda *parts: (tmp_path / Path(*parts).as_posix()) if parts else tmp_path,
    )
    monkeypatch.setattr(mng, "load_clean_config", lambda: {"enabled": True, "bar": {}})
    return db_path, processed


def test_双写DB与processed(_env) -> None:
    db_path, processed = _env
    symbol = "TST.SZ"
    records = [
        _bar("2026-09-19", 11, 11.5, 10.8, 11.2, symbol=symbol),
        _bar("2026-09-20", 11.3, 11.8, 11.1, 11.6, symbol=symbol),
    ]

    res = fcs.run_incremental_clean(records=records, db_path=db_path)
    assert res["status"] == "ok"
    assert symbol in res["cleaned_symbols"]
    assert res["db_written"] >= 1
    assert res["processed_written"] >= 1

    # DB 双写验证（upsert 后 2 根）
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM market_data WHERE symbol=? ORDER BY trade_date", (symbol,)
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[-1][1] == 11.6

    # processed 双写验证（清洗后中文字段扁平 parquet）
    df = pd.read_parquet(processed / f"{symbol}.parquet")
    assert int((df["日期"] == 20260919).sum()) == 1
    assert int((df["日期"] == 20260920).sum()) == 1


def test_无记录返回no_records(_env) -> None:
    db_path, _ = _env
    res = fcs.run_incremental_clean(records=[], db_path=db_path)
    assert res["status"] == "no_records"


def test_单标的失败隔离(_env) -> None:
    db_path, processed = _env
    good = _bar("2026-09-19", 11, 11.5, 10.8, 11.2, symbol="GOOD.SZ")
    # 坏标的：缺收盘价的脏记录 → 清洗后无有效增量，不写 DB/processed，且不影响好标
    bad = {"symbol": "BAD.SZ", "date": "2026-09-19", "open": 1, "high": 1,
           "low": 1, "close": None, "volume": 1, "amount": 0, "pct_chg": 0}

    res = fcs.run_incremental_clean(records=[good, bad], db_path=db_path)
    assert "GOOD.SZ" in res["cleaned_symbols"]  # 好标成功
    assert "BAD.SZ" not in res["cleaned_symbols"]  # 坏标被清洗剔除，未入 cleaned
    # 好标 processed 写入
    df = pd.read_parquet(processed / "GOOD.SZ.parquet")
    assert int((df["日期"] == 20260919).sum()) == 1
    # 坏标未写 DB（脏数据未入库）
    conn = sqlite3.connect(str(db_path))
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM market_data WHERE symbol='BAD.SZ'").fetchone()[0]
    finally:
        conn.close()
    assert cnt == 0


def test_读取暂存文件并规范化日期(tmp_path: Path) -> None:
    """暂存文件（date 为 YYYYMMDD 数值）读回后规范化为 YYYY-MM-DD。"""
    db_dir = tmp_path / "database"
    db_dir.mkdir(parents=True, exist_ok=True)
    f = db_dir / "incremental_20260924.parquet"
    pd.DataFrame([
        {"symbol": "TST.SZ", "date": 20260919, "open": 11, "high": 11.5, "low": 10.8,
         "close": 11.2, "volume": 100000, "amount": 1, "pct_chg": 0, "turnover": 0.5},
    ]).to_parquet(f)

    recs = fcs._load_incremental_records(tmp_path, date_tag="20260924")
    assert len(recs) == 1
    assert recs[0]["date"] == "2026-09-19"  # YYYYMMDD → YYYY-MM-DD
