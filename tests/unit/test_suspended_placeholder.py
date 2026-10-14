# 文件名：tests/unit/test_suspended_placeholder.py
"""
B-2（P1-③ #83）停牌判定单测：
  TDX 源：真实日历对齐补停牌占位行（volume=0/价 NULL）/ 无日历回退旧行为 /
          占位只补历史缺口不补未来排期
  queries：get_market_data / batch 过滤 NULL 价占位行（防污染回测）
"""

from __future__ import annotations

import sqlite3
import struct
from datetime import date
from pathlib import Path

from myquant.data_access.sources.tdx_source import TdxDataSource
from myquant.database.queries import DataQueries


def _day_bytes(date_int: int, close_p: int = 1020) -> bytes:
    """构造 32 字节 .day 记录（open=10.00 high=10.50 low=9.90 close=close_p 分）。"""
    return struct.pack("<iiiiifii", date_int, 1000, 1050, 990, close_p, 12345678.5, 5000000, 0)


def _mk_tdx(tmp_path: Path):
    """构造带 sh600519.day（两个交易日：10-09 / 10-13）的临时 TDX 根。"""
    day_file = tmp_path / "vipdoc" / "sh" / "lday" / "sh600519.day"
    day_file.parent.mkdir(parents=True, exist_ok=True)
    day_file.write_bytes(_day_bytes(20251009) + _day_bytes(20251013))
    return TdxDataSource(config={"tdx_root": str(tmp_path), "auto_detect": False})


class TestTdxPlaceholder:
    def test_fill_suspended_placeholder(self, tmp_path: Path, monkeypatch):
        """日历含 10-10（实际无行）→ 补占位行 volume=0、价 NULL，序列升序。"""
        monkeypatch.setattr(
            "myquant.utils.trade_calendar.load_trade_calendar",
            lambda root: {date(2025, 10, 9), date(2025, 10, 10), date(2025, 10, 13)},
        )
        src = _mk_tdx(tmp_path)
        res = src.get_market_data("600519.SH", start_date="2025-10-09", end_date="2025-10-13")
        assert res.count == 3
        dates = [r["date"] for r in res.data]
        assert dates == ["2025-10-09", "2025-10-10", "2025-10-13"]
        ph = res.data[1]
        assert ph["volume"] == 0.0
        assert ph["open"] is None and ph["close"] is None
        # 正常行不受影响
        assert res.data[0]["close"] == 10.2
        assert res.data[0]["volume"] == 5000000.0

    def test_no_calendar_fallback(self, tmp_path: Path, monkeypatch):
        """无日历缓存回退旧行为（不补占位行）。"""
        monkeypatch.setattr("myquant.utils.trade_calendar.load_trade_calendar", lambda root: None)
        src = _mk_tdx(tmp_path)
        res = src.get_market_data("600519.SH", start_date="2025-10-09", end_date="2025-10-13")
        assert res.count == 2
        assert [r["date"] for r in res.data] == ["2025-10-09", "2025-10-13"]

    def test_no_future_fill(self, tmp_path: Path, monkeypatch):
        """end_date 缺省时不补未来排期（占位只补到已解析最大日期）。"""
        monkeypatch.setattr(
            "myquant.utils.trade_calendar.load_trade_calendar",
            lambda root: {date(2025, 10, 9), date(2025, 10, 10), date(2025, 10, 13), date(2025, 12, 31)},
        )
        src = _mk_tdx(tmp_path)
        res = src.get_market_data("600519.SH")
        assert [r["date"] for r in res.data] == ["2025-10-09", "2025-10-10", "2025-10-13"]
        assert all(r["date"] != "2025-12-31" for r in res.data)


class _FakeConn:
    """轻封装 sqlite3 内存连接，满足 DatabaseConnection.execute 接口。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()):
        cur = self._conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _mk_db():
    """内存库：建 market_data 表 + 正常行 + 占位行（open/close NULL）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE market_data ("
        "symbol TEXT, market TEXT, period TEXT, adjust TEXT, trade_date TEXT, trade_time TEXT,"
        "open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,"
        "pct_chg REAL, turnover REAL, amplitude REAL, volume_ratio REAL, float_share REAL,"
        "pe_dynamic REAL, is_suspended INTEGER, data_source TEXT)"
    )
    rows = [
        ("600519.SH", "A股", "daily", "raw", "2025-10-09", None, 10.0, 10.5, 9.9, 10.2, 5000000, 1e7, 0.0, 1.0, 2.0, 3.0, 1e10, 20.0, 0, "tdx"),
        ("600519.SH", "A股", "daily", "raw", "2025-10-10", None, None, None, None, None, 0.0, 0.0, None, None, None, None, None, None, 1, "tdx"),
    ]
    conn.executemany(
        "INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    return DataQueries(_FakeConn(conn))


class TestQueriesNullPriceFilter:
    def test_get_market_data_filters_placeholder(self):
        """get_market_data 不返回 NULL 价占位行。"""
        q = _mk_db()
        rows = q.get_market_data("600519.SH", start_date="2025-10-09", end_date="2025-10-10")
        assert len(rows) == 1
        assert rows[0]["trade_date"] == "2025-10-09"
        assert rows[0]["is_suspended"] == 0

    def test_get_market_data_batch_filters_placeholder(self):
        """get_market_data_batch 不返回 NULL 价占位行。"""
        q = _mk_db()
        rows = q.get_market_data_batch(["600519.SH"], "2025-10-10")
        assert "600519.SH" not in rows
