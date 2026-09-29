# 文件名：tests/unit/test_data_clean_incremental.py
"""DataManager.save_cleaned_incremental_parquet 单测。

验证增量更新日线"走清洗管线"：与全量清洗共用 run_clean_pipeline + config/data_clean.yaml，
按标的按区间清洗（带前一收盘上下文）后合并写回 processed 扁平 parquet。
核心保证：清洗规则生效（脏行剔除）、前收盘上下文用于涨跌跳变判定、
合并保留全历史不丢、重叠日去重 keep=last、日期升序、列对齐存量 schema。
M6 增量派生量：清洗后新行补 涨跌幅/振幅/量比（量比以存量前 5 日均量为基准），
DB 有 financial_data 时补 换手率/流通股本/市盈动/每股收益。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from myquant.data_access.manager import DataManager
from myquant.data_access.sources.base import MarketDataResult


# ── 工具：mock fetcher 与数据构造 ──


def _bar(
    date: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100000.0,
) -> Dict[str, Any]:
    """构造一条英文字段原始 bar（fetcher 原始记录格式）。"""
    return {
        "date": date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": round(open_ * volume, 2),
        "pct_chg": 0.0,
        "turnover": 0.5,
    }


class _MockFetcher:
    """mock 数据获取器：按 symbol 返回预设的 MarketDataResult。"""

    def __init__(self, records: List[Dict[str, Any]]) -> None:
        self._records = records

    def get_market_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "hfq",
        use_cache: bool = True,
    ) -> MarketDataResult:
        """返回预设区间记录（忽略日期过滤，测试聚焦清洗/合并逻辑）。"""
        return MarketDataResult(symbol, self._records, source="mock")


def _make_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, records: List[Dict[str, Any]]
) -> tuple:
    """构造 DataManager，并把 processed 目录重定向到 tmp_path 沙箱。"""
    processed = tmp_path / "datasets" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "myquant.data_access.manager.get_project_path",
        lambda *parts: (tmp_path / Path(*parts).as_posix()) if parts else tmp_path,
    )
    manager = DataManager(_MockFetcher(records))
    return manager, processed


def _write_existing(processed: Path, symbol: str, rows: List[Dict[str, Any]]) -> Path:
    """预置存量扁平 parquet（中文字段）。"""
    path = processed / f"{symbol}.parquet"
    pd.DataFrame(rows).to_parquet(path)
    return path


# ── 用例 ──


def test_清洗后合并保留全历史并追加新日(monkeypatch, tmp_path: Path) -> None:
    """存量全历史 + 增量新日（清洗通过）→ 合并保留最早历史、追加新日、日期升序。"""
    symbol = "TST.SZ"
    manager, processed = _make_manager(monkeypatch, tmp_path, [_bar("20260919", 11, 11.5, 10.8, 11.2), _bar("20260920", 11.3, 11.8, 11.1, 11.6)])
    _write_existing(processed, symbol, [
        {"日期": 19910403, "收盘价": 1.0},
        {"日期": 20000101, "收盘价": 2.0},
        {"日期": 20260918, "收盘价": 3.0},
    ])

    out = manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-19", end_date="2026-09-20")
    assert out is not None
    df = pd.read_parquet(Path(out))
    assert len(df) == 5
    assert df["日期"].is_monotonic_increasing
    assert int(df["日期"].min()) == 19910403  # 历史保留
    assert int(df["日期"].max()) == 20260920  # 新日追加


def test_重叠日去重保留最新值(monkeypatch, tmp_path: Path) -> None:
    """增量区间与存量重叠日 → 去重为1，保留最新（清洗后的新值）。"""
    symbol = "TST.SZ"
    manager, processed = _make_manager(monkeypatch, tmp_path, [
        _bar("20260918", 9, 9.3, 8.9, 9.0),
        _bar("20260921", 9.4, 9.6, 9.2, 9.5),
    ])
    _write_existing(processed, symbol, [
        {"日期": 19910403, "收盘价": 1.0},
        {"日期": 20260918, "收盘价": 3.0},
    ])

    out = manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-18", end_date="2026-09-21")
    df = pd.read_parquet(Path(out))
    assert len(df) == 3
    assert int((df["日期"] == 20260918).sum()) == 1  # 重叠去重
    assert float(df.loc[df["日期"] == 20260918, "收盘价"].iloc[0]) == 9.0  # keep=last 取新值


def test_脏行被清洗剔除(monkeypatch, tmp_path: Path) -> None:
    """区间含结构性脏行（缺收盘价）→ 清洗后该行不入 processed。"""
    symbol = "TST.SZ"
    good = _bar("20260919", 11, 11.5, 10.8, 11.2)
    bad = {"date": "20260920", "open": 12, "high": 12, "low": 12, "close": None, "volume": 100}  # 缺收盘价
    manager, processed = _make_manager(monkeypatch, tmp_path, [good, bad])
    _write_existing(processed, symbol, [{"日期": 19910403, "收盘价": 1.0}])

    out = manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-19", end_date="2026-09-20")
    df = pd.read_parquet(Path(out))
    assert int((df["日期"] == 20260919).sum()) == 1
    assert int((df["日期"] == 20260920).sum()) == 0  # 脏行被剔除


def test_前收盘上下文触发涨跌跳变剔除(monkeypatch, tmp_path: Path) -> None:
    """存量前收盘 10 → 区间首行 open 1000 巨跳，配置 action=drop 时该行被剔除（证明前收盘上下文生效）。"""
    symbol = "TST.SZ"
    records = [_bar("20260919", 1000, 1010, 990, 1005)]  # vs 前收盘10 → 9900%跳变
    manager, processed = _make_manager(monkeypatch, tmp_path, records)
    _write_existing(processed, symbol, [
        {"日期": 20260918, "开盘价": 10, "最高价": 10, "最低价": 9.9, "收盘价": 10, "成交量": 50000},
    ])
    # 覆盖清洗配置为 drop 语义（证明方法确实应用了规则配置）
    monkeypatch.setattr(
        "myquant.data_access.manager.load_clean_config",
        lambda: {
            "enabled": True,
            "bar": {"detect_price_jump": True, "max_price_jump_pct": 0.22, "price_jump_action": "drop"},
        },
    )

    out = manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-19", end_date="2026-09-19")
    # 区间唯一的新行被剔除 → 无有效增量，方法返回 None（不把脏数据写进 processed）
    assert out is None
    df = pd.read_parquet(processed / f"{symbol}.parquet")
    assert int((df["日期"] == 20260919).sum()) == 0  # 跳变行未写入
    assert int((df["日期"] == 20260918).sum()) == 1  # 存量保留


def test_清洗功能关闭返回None(monkeypatch, tmp_path: Path) -> None:
    """配置 enabled=False → 方法返回 None，不做清洗。"""
    symbol = "TST.SZ"
    manager, processed = _make_manager(monkeypatch, tmp_path, [_bar("20260919", 11, 11.5, 10.8, 11.2)])
    monkeypatch.setattr("myquant.data_access.manager.load_clean_config", lambda: {"enabled": False})
    assert manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-19", end_date="2026-09-19") is None


def test_无数据返回None(monkeypatch, tmp_path: Path) -> None:
    """fetcher 无数据 → 返回 None。"""
    symbol = "TST.SZ"
    manager, processed = _make_manager(monkeypatch, tmp_path, [])
    assert manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-19", end_date="2026-09-19") is None


def test_存量损坏仍写入且列对齐(monkeypatch, tmp_path: Path) -> None:
    """存量 parquet 损坏 → 不崩溃，仅写入清洗后的区间；且列对齐存量 schema。"""
    symbol = "TST.SZ"
    manager, processed = _make_manager(monkeypatch, tmp_path, [_bar("20260919", 11, 11.5, 10.8, 11.2)])
    bad = processed / f"{symbol}.parquet"
    bad.write_bytes(b"\x00\x01not-a-valid-parquet")

    out = manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-19", end_date="2026-09-19")
    assert out is not None
    df = pd.read_parquet(Path(out))
    assert len(df) == 1
    assert int(df["日期"].iloc[0]) == 20260919
    assert "收盘价" in df.columns


def test_传入records后不再重新拉取(monkeypatch, tmp_path: Path) -> None:
    """M1修复：save 传入 records 时不再调用 fetcher.get_market_data（消除增量链路双拉取）。"""
    symbol = "TST.SZ"
    records = [_bar("20260919", 11, 11.5, 10.8, 11.2)]

    class _ExplodeFetcher:
        """若被调用则测试失败（M1 后 save 不应再拉取）。"""

        def get_market_data(self, *args: Any, **kwargs: Any):
            raise AssertionError("M1 修复后不应重新拉取数据源")

    manager = DataManager(_ExplodeFetcher())
    processed = tmp_path / "datasets" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "myquant.data_access.manager.get_project_path",
        lambda *parts: (tmp_path / Path(*parts).as_posix()) if parts else tmp_path,
    )
    _write_existing(processed, symbol, [{"日期": 19910403, "收盘价": 1.0}])

    out = manager.save_cleaned_incremental_parquet(
        symbol, start_date="2026-09-19", end_date="2026-09-19", records=records,
    )
    assert out is not None
    df = pd.read_parquet(Path(out))
    assert int((df["日期"] == 20260919).sum()) == 1  # 复用 records 清洗后写入
    assert int(df["日期"].min()) == 19910403  # 存量保留
def test_增量新行补派生量_量比用前5日均量(monkeypatch, tmp_path: Path) -> None:
    """M6：增量清洗后新行含 涨跌幅/振幅/量比；量比以存量前 5 日均量为基准（与全量口径一致）。"""
    symbol = "TST.SZ"
    # 区间：前收盘 10 → 新日 11.2，成交量 120000
    records = [_bar("20260926", 11, 11.5, 10.8, 11.2, volume=120000)]
    manager, processed = _make_manager(monkeypatch, tmp_path, records)
    # 存量前 6 根（< 20260926 取最近 5 根 = 20260921..20260925，成交量均 100000），收盘 10
    rows = []
    for d in ["20260918", "20260921", "20260922", "20260923", "20260924", "20260925"]:
        rows.append({
            "日期": int(d.replace("-", "")), "开盘价": 10, "最高价": 10.2,
            "最低价": 9.9, "收盘价": 10.0, "成交量": 100000, "成交额": 1000000,
            "涨跌幅": None, "振幅": None, "量比": None,
            "流通股本": None, "市盈动": None,
        })
    _write_existing(processed, symbol, rows)

    out = manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-26", end_date="2026-09-26")
    df = pd.read_parquet(Path(out))
    new = df[df["日期"] == 20260926].iloc[0]
    assert float(new["涨跌幅"]) == round((11.2 - 10.0) / 10.0 * 100, 4)
    assert float(new["振幅"]) == round((11.5 - 10.8) / 10.0 * 100, 4)
    assert float(new["量比"]) == round(120000 / 100000, 4)  # 5 日均量 = 100000
    # 存量行不被重算（增量语义：历史不动），派生量列保持空
    old = df[df["日期"] == 20260918].iloc[0]
    assert pd.isna(old.get("涨跌幅"))


def test_增量B类派生量_有财务数据时计算换手率流通股本市盈动与每股收益(monkeypatch, tmp_path: Path) -> None:
    """M6：DB 有 financial_data 时，增量新行补 换手率/流通股本/市盈动/每股收益；无财务则留空不报错。"""
    symbol = "TST.SZ"
    records = [_bar("20260926", 11, 11.5, 10.8, 11.2, volume=100000)]
    manager, processed = _make_manager(monkeypatch, tmp_path, records)

    class _FakeDataQueries:
        """fake DataQueries：DB 无上下文行情（processed 兜底），financial_data 返回单期财报。"""

        def __init__(self, conn: Any) -> None:
            self.conn = conn

        def get_market_data(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
            return []

    class _FakeConn:
        def execute(self, sql: str, params: Any = ()) -> List[Dict[str, Any]]:
            if "financial_data" in sql:
                return [{
                    "report_date": "2026-06-30", "circ_mv": 10_000_000_000.0,
                    "total_mv": 20_000_000_000.0, "net_profit": 1_000_000_000.0,
                    "eps": 0.5,
                }]
            return []

    monkeypatch.setattr("myquant.database.queries.DataQueries", _FakeDataQueries)
    manager._db = _FakeConn()  # 注入非空 db 连接启用 DB 路径

    rows = [{
        "日期": 20260925, "开盘价": 10, "最高价": 10.2,
        "最低价": 9.9, "收盘价": 10.0, "成交量": 100000, "成交额": 1000000,
        "涨跌幅": None, "振幅": None, "量比": None,
        "流通股本": None, "市盈动": None, "换手率": None, "每股收益": None,
    }]
    _write_existing(processed, symbol, rows)

    out = manager.save_cleaned_incremental_parquet(symbol, start_date="2026-09-26", end_date="2026-09-26")
    df = pd.read_parquet(Path(out))
    new = df[df["日期"] == 20260926].iloc[0]
    float_share = 10_000_000_000.0 / 11.2  # circ_mv / 最新收盘
    assert float(new["流通股本"]) == round(float_share, 2)
    assert float(new["换手率"]) == round(100000 / float_share * 100, 4)
    # pe_dynamic = close * total_share / 年化净利润（半年报 ×2）
    assert float(new["市盈动"]) == round(11.2 * (20_000_000_000.0 / 11.2) / (1_000_000_000.0 * 2), 4)
    assert float(new["每股收益"]) == 0.5
    # 存量行不补派生量（增量语义）
    old = df[df["日期"] == 20260925].iloc[0]
    assert pd.isna(old.get("市盈动"))
