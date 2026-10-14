# 文件名：tests/unit/test_factor_sync_service.py
"""
复权因子同步服务单测（方案B #82）
====================================
覆盖：符号转换 / 事件表清洗（1900脏行/NaN/排序去重）/ UPSERT 幂等 /
因子查询（as_of 防未来截断）/ 拉取失败重试。

全部使用 stub akshare 与临时数据库，零网络、零真实行情。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from myquant.data_access.factor_sync_service import FactorSyncService
from myquant.database import get_connection
from myquant.database.schema import create_all_tables


class StubAK:
    """stub akshare：固定返回含脏行的东财 hfq-factor 事件表。"""

    def __init__(self, df: pd.DataFrame | None = None, fail_times: int = 0):
        self.df = df
        self.fail_times = fail_times
        self.calls: list[str] = []

    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        self.calls.append(symbol)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("stub network fail")
        if self.df is None:
            return pd.DataFrame(columns=["date", "hfq_factor"])
        return self.df.copy()


@pytest.fixture
def svc(temp_db_path: str) -> FactorSyncService:
    """基于临时数据库 + stub akshare 的因子服务（teardown 释放连接与文件锁）。"""
    conn = get_connection(temp_db_path)
    create_all_tables(conn)
    svc = FactorSyncService(Path(temp_db_path).parent, db_path=Path(temp_db_path), akshare_module=StubAK())
    yield svc
    svc._conn.close()  # 关闭全局缓存连接，释放临时库文件锁


def _events_df(dates, factors):
    return pd.DataFrame({"date": dates, "hfq_factor": factors})


class TestSymbolConversion:
    def test_to_ak_symbol(self):
        assert FactorSyncService.to_ak_symbol("600519.SH") == "sh600519"
        assert FactorSyncService.to_ak_symbol("000001.SZ") == "sz000001"


class TestCleanEvents:
    def test_remove_dirty_rows_and_sort(self):
        # _clean_events 是静态方法，无需实例化（实例化会要求 database/quant.db 存在）
        df = _events_df(
            ["2024-06-19", "1900-01-01", "2023-12-20", "2024-06-19"],
            [8.239032, 1.0, 8.071836, 8.24],  # 同日重复（后者胜）
        )
        events = FactorSyncService._clean_events(df, "600519.SH")
        assert [(d, round(f, 6)) for d, f in events] == [
            ("2023-12-20", 8.071836),
            ("2024-06-19", 8.24),
        ]

    def test_nan_and_invalid_factor_dropped(self):
        df = _events_df(["2024-06-19", "2024-06-20", "bad-date"], [8.239032, None, 1.0])
        events = FactorSyncService._clean_events(df, "600519.SH")
        assert events == [("2024-06-19", 8.239032)]

    def test_empty_and_missing_cols(self):
        assert FactorSyncService._clean_events(pd.DataFrame(), "x") == []
        assert FactorSyncService._clean_events(pd.DataFrame({"a": [1]}), "x") == []


class TestFetchAndUpsert:
    def test_fetch_upsert_lookup(self, svc):
        svc._ak = StubAK(df=_events_df(
            ["2024-06-19", "2023-12-20", "1900-01-01"],
            [8.239032, 8.071836, 1.0],
        ))
        n, status = svc.sync_symbol("600519.SH")
        assert (n, status) == (2, "ok")
        lookup = svc.get_factor_lookup("600519.SH")
        assert lookup == {"2023-12-20": 8.071836, "2024-06-19": 8.239032}
        assert svc.get_latest_factor("600519.SH") == 8.239032

    def test_as_of_truncation_future_function_guard(self, svc):
        """防未来函数：as_of 截断，查询日之后的事件不可见。"""
        svc._ak = StubAK(df=_events_df(
            ["2024-06-19", "2025-06-26"],
            [8.239032, 8.531877],
        ))
        svc.sync_symbol("600519.SH")
        assert svc.get_latest_factor("600519.SH", as_of="2024-12-31") == 8.239032
        assert len(svc.get_factor_lookup("600519.SH", as_of="2024-06-18")) == 0

    def test_upsert_idempotent(self, svc):
        svc._ak = StubAK(df=_events_df(["2024-06-19"], [8.239032]))
        svc.sync_symbol("600519.SH")
        svc.sync_symbol("600519.SH")  # 重复同步不产生重复行
        rows = svc._conn.execute(
            "SELECT COUNT(*) AS n FROM adjust_factor WHERE symbol='600519.SH'"
        )
        assert rows[0]["n"] == 1

    def test_fetch_failure_returns_none(self, svc):
        svc._ak = StubAK(df=_events_df(["2024-06-19"], [8.239032]), fail_times=3)
        n, status = svc.sync_symbol("600519.SH")
        assert n is None and status.startswith("fail")


class TestSyncMarket:
    def test_sync_market_stats(self, svc):
        svc._ak = StubAK(df=_events_df(["2024-06-19"], [8.239032]))
        stats = svc.sync_market(["600519.SH", "000001.SZ"], sleep=0)
        assert stats["total"] == 2 and stats["ok"] == 2 and stats["events_upserted"] == 2
        assert stats["failed_symbols"] == []
