# 文件名：tests/unit/test_admission_wiring.py
"""
C-01 门禁①接入 update_symbol 单测（V-11 接线语义）
====================================================
覆盖（蓝图 #450 配套）：
  1. 默认关：AdmissionChecker.is_enabled()=False；_save_to_database 直通（BLOCK 行也写入，生产零变化）
  2. 开启后 BLOCK：拒收不写库 + admission_reject 表留痕（rule/detail/source/trade_date）
  3. 开启后 FLAG：仍写库 + anomaly_flags 列落标记（price_jump/volume_anomaly/...）
  4. 开启后 PASS：原样写入（anomaly_flags=None）
  5. prev_row 上下文：DB 末根作为首行判定基准（缺字段 BLOCK 仍独立触发）
  6. 计数暴露：_admission_stats {blocked, flagged}；update_symbol result 追加字段
设计原则：全部离线确定性（:memory: 库 + 临时 yaml + stub fetcher，零网络）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

from myquant.data_access.admission import AdmissionChecker
from myquant.data_access.manager import DataManager
from myquant.data_access.sources.base import MarketDataResult
from myquant.database import DatabaseConnection, create_all_tables
from myquant.database.queries import DataQueries


class _StubFetcher:
    """最小 fetcher stub：仅满足 DataManager 构造。"""

    def get_market_data(self, *args: Any, **kwargs: Any) -> MarketDataResult:
        raise AssertionError("测试不应触发真实拉取")

    def get_market_data_batch(self, *args: Any, **kwargs: Any) -> dict:
        raise AssertionError("测试不应触发真实拉取")


def _make_conn() -> DatabaseConnection:
    conn = DatabaseConnection(db_path=":memory:")
    create_all_tables(conn)
    return conn


def _make_manager(conn: DatabaseConnection, enabled: bool) -> DataManager:
    manager = DataManager(fetcher=_StubFetcher(), db_connection=conn)
    # 注入显式 enabled 的判定器（绕过真实 config 文件，保持确定性）
    checker = AdmissionChecker(config_path=Path("not_exists.yaml"))
    checker._enabled = enabled
    manager._admission_checker = checker
    return manager


def _result(rows: List[dict], source: str = "tdx") -> MarketDataResult:
    return MarketDataResult(
        symbol="000001.SZ", data=rows, source=source,
        market="A股", period="daily", adjust="raw",
    )


_GOOD = [
    {"date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
     "volume": 100000.0, "amount": 1020000.0, "pct_chg": 1.0, "turnover": 0.1},
    {"date": "2024-01-03", "open": 10.2, "high": 10.8, "low": 10.0, "close": 10.7,
     "volume": 120000.0, "amount": 1270000.0, "pct_chg": 4.9, "turnover": 0.12},
]


# ── 1. 默认关（直通，生产零变化）──────────────────────────────
def test_default_disabled_passthrough() -> None:
    """默认关闭：is_enabled()=False；含 BLOCK 缺陷行也照常写入（现状保持）。"""
    checker = AdmissionChecker(config_path=Path("not_exists.yaml"))
    assert checker.is_enabled() is False

    conn = _make_conn()
    manager = _make_manager(conn, enabled=False)
    bad_row = {"date": "2024-01-02", "open": 10.0, "high": 5.0, "low": 9.8,
               "close": 10.2, "volume": 100000.0}  # high<low → BLOCK 规则
    n = manager._save_to_database("000001.SZ", _result([bad_row]), "daily", "raw")
    assert n == 1, "关闭时应直通写入 1 行"
    dq = DataQueries(conn)
    rows = dq.get_market_data("000001.SZ", adjust="raw")
    assert len(rows) == 1
    assert rows[0]["anomaly_flags"] is None
    assert manager._admission_stats == {"blocked": 0, "flagged": 0}
    # 无拒收留痕
    assert dq.query_admission_rejects(symbol="000001.SZ") == []


# ── 2. 开启后 BLOCK：拒收 + 留痕 + 不写库 ─────────────────────
def test_enabled_block_rejects_and_records() -> None:
    """开启后：BLOCK 行拒收不写库，admission_reject 留痕（rule/detail/trade_date）。"""
    conn = _make_conn()
    manager = _make_manager(conn, enabled=True)
    bad_row = {"date": "2024-01-02", "open": 10.0, "high": 5.0, "low": 9.8,
               "close": 10.2, "volume": 100000.0}  # high<low
    n = manager._save_to_database("000001.SZ", _result([bad_row]), "daily", "raw")
    assert n == 0, "BLOCK 行应拒收，写入 0 行"
    dq = DataQueries(conn)
    assert dq.get_market_data("000001.SZ", adjust="raw") == []
    rejects = dq.query_admission_rejects(symbol="000001.SZ")
    assert len(rejects) == 1
    assert rejects[0]["rule"] == "high_low_conflict"
    assert "high_low_conflict" in rejects[0]["detail"]
    assert rejects[0]["level"] == "BLOCK"
    assert rejects[0]["trade_date"] == "2024-01-02"
    assert rejects[0]["source"] == "tdx"
    assert manager._admission_stats["blocked"] == 1


# ── 3. 开启后 FLAG：写库 + anomaly_flags 落列 ─────────────────
def test_enabled_flag_marks_anomaly_flags() -> None:
    """开启后：FLAG 行仍写库，anomaly_flags 落标记（价格跳变+量异动）。"""
    conn = _make_conn()
    manager = _make_manager(conn, enabled=True)
    rows = [
        {"date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
         "volume": 100000.0, "amount": 1020000.0, "pct_chg": 1.0, "turnover": 0.1},
        # 相对前根 close=10.2 跳至 30.0（>主板 0.11 阈值）+ 量 200 倍（>100）
        {"date": "2024-01-03", "open": 29.0, "high": 31.0, "low": 28.5, "close": 30.0,
         "volume": 20000000.0, "amount": 6e8, "pct_chg": 194.0, "turnover": 0.5},
    ]
    n = manager._save_to_database("000001.SZ", _result(rows), "daily", "raw")
    assert n == 2, "FLAG 行应放行写入"
    dq = DataQueries(conn)
    db_rows = dq.get_market_data("000001.SZ", start_date="2024-01-03", end_date="2024-01-03",
                                 adjust="raw")
    assert len(db_rows) == 1
    flags = db_rows[0]["anomaly_flags"] or ""
    assert "price_jump" in flags
    assert "volume_anomaly" in flags
    assert manager._admission_stats["flagged"] == 1


# ── 4. 开启后 PASS：原样写入 ──────────────────────────────────
def test_enabled_pass_writes_plain() -> None:
    """开启后：PASS 行原样写入，anomaly_flags=None，无拒收留痕。"""
    conn = _make_conn()
    manager = _make_manager(conn, enabled=True)
    n = manager._save_to_database("000001.SZ", _result(_GOOD), "daily", "raw")
    assert n == 2
    dq = DataQueries(conn)
    rows = dq.get_market_data("000001.SZ", adjust="raw")
    assert len(rows) == 2
    assert all(r["anomaly_flags"] is None for r in rows)
    assert dq.query_admission_rejects(symbol="000001.SZ") == []
    assert manager._admission_stats == {"blocked": 0, "flagged": 0}


# ── 5. prev_row 上下文（DB 末根）──────────────────────────────
def test_prev_row_context_from_db() -> None:
    """开启后：首行判定以 DB 末根为上下文（量异动相对 DB 前值触发）。"""
    conn = _make_conn()
    manager = _make_manager(conn, enabled=False)
    # 先写一根基线（关闭状态下）
    manager._save_to_database("000001.SZ", _result(_GOOD[:1]), "daily", "raw")
    # 再开开关增量写入：新首根相对 DB 末根 close=10.2 跳至 40（>阈值）→ FLAG
    manager._admission_checker._enabled = True
    jump_row = {"date": "2024-01-04", "open": 39.0, "high": 41.0, "low": 38.5,
                "close": 40.0, "volume": 150000.0, "amount": 6e6, "pct_chg": 292.0,
                "turnover": 0.3}
    n = manager._save_to_database("000001.SZ", _result([jump_row]), "daily", "raw")
    assert n == 1
    dq = DataQueries(conn)
    db_row = dq.get_market_data("000001.SZ", start_date="2024-01-04", end_date="2024-01-04",
                                adjust="raw")[0]
    assert "price_jump" in (db_row["anomaly_flags"] or "")
    assert manager._admission_stats["flagged"] == 1


# ── 6. 计数暴露（update_symbol 结果字段）─────────────────────
def test_update_symbol_result_exposes_counts() -> None:
    """update_symbol 结果追加 admission_blocked / admission_flagged（默认关=0）。"""
    conn = _make_conn()
    manager = _make_manager(conn, enabled=False)

    class _Fetcher:
        def get_market_data(self, *args: Any, **kwargs: Any) -> MarketDataResult:
            return _result(_GOOD)

        def get_market_data_batch(self, *args: Any, **kwargs: Any) -> dict:
            raise AssertionError

        def invalidate_cache(self, *args: Any, **kwargs: Any) -> None:
            return None

    manager._fetcher = _Fetcher()
    res = manager.update_symbol("000001.SZ")
    assert res["status"] == "completed"
    assert res["admission_blocked"] == 0
    assert res["admission_flagged"] == 0
