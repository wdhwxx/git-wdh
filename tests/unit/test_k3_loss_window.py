# 文件名：tests/unit/test_k3_loss_window.py
"""A-03 K3 丢失窗口临时库复现（只读，不碰生产库）。

K3 根因（代码级）：DataQueries.bulk_insert_market_data（queries.py:218）
与 delete_market_data（queries.py:237）各自 `with self.conn.transaction():`，
而 full_clean_service.write_back_symbol（full_clean_service.py:735）外层又包一层
`with dq.conn.transaction():` —— SQLite 单连接上三层嵌套事务且无 SAVEPOINT。

丢失窗口机制：
  1. 外层 write_back_symbol 事务开启（隐式 BEGIN）；
  2. delete_market_data 内层 commit 提交整个活跃事务 → DELETE 已生效（旧行已删且已提交）；
  3. bulk_insert_market_data 开新事务，若中途异常 → 该事务 rollback → INSERT 未生效；
  4. 外层捕获异常返回 error，但 DELETE 无法回滚（事务早已提交）
     ⇒ 标的被删空且不可回滚（数据丢失窗口）。

本文件即 A-03 的"临时库复现脚本"：复现成功 = K3 机制确认（delete 已生效、
insert 未生效、外层 rollback 救不回）。对照用例验证"若 DELETE+INSERT 处于同一
原子事务（SAVEPOINT 语义），失败时数据不丢失" —— 该对照是 A-05 的验收基准。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.database.queries import DataQueries

_TABLE = "market_data"
_COLS = ["symbol", "period", "adjust", "trade_date", "trade_time", "close",
         "data_source"]


def _make_dq(tmp_path: Path) -> DataQueries:
    db = tmp_path / "k3.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        f"CREATE TABLE {_TABLE} ("
        " symbol TEXT, period TEXT, adjust TEXT, trade_date TEXT, trade_time TEXT,"
        " close REAL, data_source TEXT"
        ")"
    )
    conn.commit()
    conn.close()
    dc = DatabaseConnection(str(db))
    return DataQueries(dc)


def _seed(dq: DataQueries, symbol: str, n: int) -> None:
    rows = [
        {"symbol": symbol, "period": "daily", "adjust": "none",
         "trade_date": f"2024-01-{i + 1:02d}", "trade_time": None,
         "close": 10.0 + i, "data_source": "tdx"}
        for i in range(n)
    ]
    with dq.conn.transaction():
        dq.conn.executemany(
            f"INSERT INTO {_TABLE} ({','.join(_COLS)}) VALUES "
            f"({','.join(['?'] * len(_COLS))})",
            [tuple(r[c] for c in _COLS) for r in rows],
        )


def _count(dq: DataQueries, symbol: str) -> int:
    row = dq.conn.execute_one(
        f"SELECT COUNT(*) AS c FROM {_TABLE} WHERE symbol = ?", (symbol,)
    )
    return int(row["c"]) if row else 0


def _simulate_nested_write_back(
    dq: DataQueries, symbol: str, new_rows: List[dict],
    fail_insert: bool,
) -> str:
    """按 full_clean_service.write_back_symbol 现状语义逐层模拟（非调真实服务，
    聚焦嵌套事务机制本身）：外层事务 → delete 内层事务 → insert 内层事务（可注入异常）。"""
    outer = dq.conn.transaction()
    outer.__enter__()
    try:
        # 内层 1：delete_market_data 的 with transaction()（commit 提交整个事务）
        with dq.conn.transaction():
            dq.conn.execute_update(
                f"DELETE FROM {_TABLE} WHERE symbol = ?", (symbol,)
            )
        # 内层 2：bulk_insert_market_data 的 with transaction()（注入异常验证 DELETE 已生效）
        if fail_insert:
            raise RuntimeError("injected bulk_insert failure (A-03)")
        if new_rows:
            with dq.conn.transaction():
                dq.conn.executemany(
                    f"INSERT INTO {_TABLE} ({','.join(_COLS)}) VALUES "
                    f"({','.join(['?'] * len(_COLS))})",
                    [tuple(r[c] for c in _COLS) for r in new_rows],
                )
        outer.__exit__(None, None, None)
        return "written"
    except Exception as exc:
        outer.__exit__(type(exc), exc, exc.__traceback__)
        return "error"


def _simulate_atomic_write_back(
    dq: DataQueries, symbol: str, new_rows: List[dict], fail_insert: bool,
) -> str:
    """A-05 预期语义（SAVEPOINT/单一事务）：DELETE+INSERT 同一原子事务，
    失败则整体回滚 → 数据不丢失。作为对照验收基准。"""
    try:
        with dq.conn.transaction():
            dq.conn.execute_update(
                f"DELETE FROM {_TABLE} WHERE symbol = ?", (symbol,)
            )
            if fail_insert:
                raise RuntimeError("injected failure (atomic control)")
            if new_rows:
                dq.conn.executemany(
                    f"INSERT INTO {_TABLE} ({','.join(_COLS)}) VALUES "
                    f"({','.join(['?'] * len(_COLS))})",
                    [tuple(r[c] for c in _COLS) for r in new_rows],
                )
        return "written"
    except Exception as exc:
        return "error"


def _new_rows(symbol: str, n: int) -> List[dict]:
    return [
        {"symbol": symbol, "period": "daily", "adjust": "none",
         "trade_date": f"2025-06-{i + 1:02d}", "trade_time": None,
         "close": 20.0 + i, "data_source": "cleaned"}
        for i in range(n)
    ]


# ═══════════════════════════════════════════
# A-03 复现：DELETE 已生效（标的被删空且不可回滚）
# ═══════════════════════════════════════════


def test_k3_nested_delete_committed_insert_failed(tmp_path: Path) -> None:
    """【K3 复现=确认】嵌套模式下 bulk_insert 注入异常：
    DELETE 已生效（行数 0=标的被删空），外层事务无法回滚恢复（旧数据丢失）。"""
    dq = _make_dq(tmp_path)
    _seed(dq, "000001.SZ", 10)
    assert _count(dq, "000001.SZ") == 10

    status = _simulate_nested_write_back(dq, "000001.SZ", _new_rows("000001.SZ", 10),
                                         fail_insert=True)
    assert status == "error"            # 写回报错（insert 失败）
    assert _count(dq, "000001.SZ") == 0  # DELETE 已生效且不可回滚 → 丢失窗口成立


def test_k3_nested_normal_path_ok(tmp_path: Path) -> None:
    """嵌套模式正常路径（无异常）：delete+insert 均各自提交，行数正确。"""
    dq = _make_dq(tmp_path)
    _seed(dq, "600519.SH", 5)
    status = _simulate_nested_write_back(dq, "600519.SH", _new_rows("600519.SH", 3),
                                         fail_insert=False)
    assert status == "written"
    assert _count(dq, "600519.SH") == 3  # 旧 5 行被删、新 3 行写入


# ═══════════════════════════════════════════
# 对照：原子事务语义（A-05 验收基准）
# ═══════════════════════════════════════════


def test_atomic_control_insert_failed_keeps_old(tmp_path: Path) -> None:
    """【对照=A-05 验收基准】DELETE+INSERT 同一事务失败 → 整体回滚，旧数据保留。"""
    dq = _make_dq(tmp_path)
    _seed(dq, "000001.SZ", 10)
    status = _simulate_atomic_write_back(dq, "000001.SZ", _new_rows("000001.SZ", 10),
                                         fail_insert=True)
    assert status == "error"
    assert _count(dq, "000001.SZ") == 10  # 数据不丢失（A-05 目标行为）
