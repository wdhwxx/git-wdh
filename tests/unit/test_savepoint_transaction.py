# 文件名：tests/unit/test_savepoint_transaction.py
"""A-05 SAVEPOINT 嵌套事务单测。

验证 connection.transaction() 的 SAVEPOINT 支持修复 K3 丢失窗口：
  1. 嵌套 delete+insert 失败 → 整体回滚、旧数据保留（A-03 场景不再丢失，验收基准）；
  2. 嵌套正常路径 → 内层 RELEASE 不提前提交外层，行数正确；
  3. 单层（无活跃事务）语义不变：正常 commit / 异常 rollback；
  4. 深层嵌套（三层）异常回滚至最内层，外层后续写入仍可提交。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.database.queries import DataQueries

_TABLE = "market_data"
_COLS = [
    "symbol", "market", "period", "adjust", "trade_date", "trade_time",
    "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover",
    "amplitude", "volume_ratio", "float_share", "pe_dynamic", "is_suspended",
    "data_source",
]
_COL_DDL = ",".join(
    f"{c} TEXT" if c in ("trade_time", "data_source") else f"{c} REAL"
    for c in _COLS
)


@pytest.fixture
def dq(tmp_path: Path) -> DataQueries:
    db = tmp_path / "a05.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE {_TABLE} ({_COL_DDL})")
    conn.commit()
    conn.close()
    return DataQueries(DatabaseConnection(str(db)))


def _seed(dq: DataQueries, symbol: str, n: int) -> None:
    rows = [
        {"symbol": symbol, "market": "A股", "period": "daily", "adjust": "none",
         "trade_date": f"2024-01-{i + 1:02d}", "trade_time": None,
         "close": 10.0 + i, "data_source": "tdx"}
        for i in range(n)
    ]
    with dq.conn.transaction():
        dq.conn.executemany(
            f"INSERT INTO {_TABLE} ({','.join(_COLS)}) VALUES "
            f"({','.join(['?'] * len(_COLS))})",
            [tuple(r.get(c) for c in _COLS) for r in rows],
        )


def _new_rows(symbol: str, n: int) -> List[dict]:
    return [
        {"symbol": symbol, "market": "A股", "period": "daily", "adjust": "none",
         "trade_date": f"2025-06-{i + 1:02d}", "trade_time": None,
         "close": 20.0 + i, "data_source": "cleaned"}
        for i in range(n)
    ]


def _count(dq: DataQueries, symbol: str) -> int:
    row = dq.conn.execute_one(
        f"SELECT COUNT(*) AS c FROM {_TABLE} WHERE symbol = ?", (symbol,)
    )
    return int(row["c"]) if row else 0


# ═══════════════════════════════════════════
# 1. A-05 核心验收：A-03 嵌套场景数据不再丢失
# ═══════════════════════════════════════════


def test_nested_delete_insert_failure_keeps_old(dq: DataQueries) -> None:
    """write_back_symbol 语义：外层事务 + delete 内层 + insert 前异常 →
    整体回滚，旧 10 行保留（K3 丢失窗口已封堵）。"""
    _seed(dq, "000001.SZ", 10)
    with pytest.raises(RuntimeError):
        with dq.conn.transaction():
            dq.delete_market_data("000001.SZ")   # 内层 SAVEPOINT，RELEASE 不提交外层
            raise RuntimeError("injected insert failure (A-05)")
    assert _count(dq, "000001.SZ") == 10  # 数据不丢失


def test_nested_delete_insert_success(dq: DataQueries) -> None:
    """嵌套正常路径：delete+bulk_insert 各自内层 SAVEPOINT → 外层单次提交，行数正确。"""
    _seed(dq, "600519.SH", 5)
    with dq.conn.transaction():
        dq.delete_market_data("600519.SH")
        dq.bulk_insert_market_data(_new_rows("600519.SH", 3), replace=True)
    assert _count(dq, "600519.SH") == 3  # 旧 5 删、新 3 写，同事务提交


def test_nested_bulk_insert_failure_inner_only_rollback(dq: DataQueries) -> None:
    """内层失败被调用方捕获（不冒泡到外层）：内层改动回滚，外层后续写入仍提交。"""
    _seed(dq, "000001.SZ", 2)
    with dq.conn.transaction():
        dq.conn.execute_update(
            f"UPDATE {_TABLE} SET close = 99 WHERE symbol = '000001.SZ'"
        )
        try:
            with dq.conn.transaction():
                dq.delete_market_data("000001.SZ")
                raise RuntimeError("inner failure caught by caller")
        except RuntimeError:
            pass  # 内层 SAVEPOINT 已回滚（delete 撤销）
        dq.conn.execute_update(
            f"UPDATE {_TABLE} SET close = 88 WHERE symbol = '000001.SZ'"
        )
    # 外层提交：close 88 生效；内层 delete 已回滚（行数仍 2）
    assert _count(dq, "000001.SZ") == 2
    row = dq.conn.execute_one(
        f"SELECT close FROM {_TABLE} WHERE symbol = '000001.SZ' LIMIT 1"
    )
    assert row["close"] == 88


# ═══════════════════════════════════════════
# 2. 单层语义不变（回归：无活跃事务走原 BEGIN/COMMIT）
# ═══════════════════════════════════════════


def test_single_transaction_commit(dq: DataQueries) -> None:
    """单层 transaction 正常退出自动提交。"""
    with dq.conn.transaction():
        dq.conn.execute_update(
            f"INSERT INTO {_TABLE} ({','.join(_COLS)}) VALUES "
            f"('600000.SH','A股','daily','none','2025-01-01',NULL,"
            f"NULL,NULL,NULL,30.0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'tdx')"
        )
    assert _count(dq, "600000.SH") == 1


def test_single_transaction_rollback(dq: DataQueries) -> None:
    """单层 transaction 异常自动回滚。"""
    with pytest.raises(ValueError):
        with dq.conn.transaction():
            dq.conn.execute_update(
                f"INSERT INTO {_TABLE} ({','.join(_COLS)}) VALUES "
                f"('600000.SH','A股','daily','none','2025-01-01',NULL,"
                f"NULL,NULL,NULL,30.0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'tdx')"
            )
            raise ValueError("boom")
    assert _count(dq, "600000.SH") == 0


# ═══════════════════════════════════════════
# 3. 三层嵌套（write_back_symbol + queries 双内层）
# ═══════════════════════════════════════════


def test_three_level_nested_failure_keeps_old(dq: DataQueries) -> None:
    """三层嵌套最内层失败 → 回滚至最内层，外层 delete 未生效，旧数据保留。"""
    _seed(dq, "300750.SZ", 4)
    with pytest.raises(RuntimeError):
        with dq.conn.transaction():          # L1 外层（write_back_symbol）
            with dq.conn.transaction():      # L2 内层（delete_market_data 语义）
                dq.delete_market_data("300750.SZ")
                with dq.conn.transaction():  # L3 内层（bulk_insert 语义，注入异常）
                    dq.conn.executemany(
                        f"INSERT INTO {_TABLE} ({','.join(_COLS)}) VALUES "
                        f"({','.join(['?'] * len(_COLS))})",
                        [tuple(r.get(c) for c in _COLS)
                         for r in _new_rows("300750.SZ", 2)],
                    )
                    raise RuntimeError("L3 injected failure")
    assert _count(dq, "300750.SZ") == 4  # 全部回滚，旧数据保留
