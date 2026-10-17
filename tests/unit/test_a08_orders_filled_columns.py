# 文件名：tests/unit/test_a08_orders_filled_columns.py
"""A-08 orders UPSERT 补 3 列单测。

验收：状态流转后 filled_qty / filled_price 不被重置（存量红 test_trade_ledger
"[excluded.filled_qty 列缺失]" 的目标修复）。
覆盖：
  1. INSERT 含 3 列：新订单落库 filled_qty/filled_price 有值；
  2. 状态流转（部分成交→全部成交）ON CONFLICT UPDATE 后 filled_qty 递增不被重置；
  3. 部分成交→撤单：filled_qty 保留（不被重置为 0）；
  4. schema.py orders DDL 与迁移列双路一致（含 filled_qty/filled_price/last_update_at）；
  5. create_all_tables 建的新库 record_order 可执行（无 no such column）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.database.schema import create_all_tables
from myquant.database.trade_log import TradeLedger
from myquant.models.trade import Order, OrderStatus


@pytest.fixture
def store(tmp_path: Path) -> TradeLedger:
    db = tmp_path / "orders.db"
    db.touch()
    dc = DatabaseConnection(str(db))
    create_all_tables(dc)
    return TradeLedger(dc)


def _order(order_id: str, status: OrderStatus, filled_qty: float = 0.0,
           filled_price: float = 0.0) -> Order:
    o = Order(
        order_id=order_id,
        symbol="600519.SH",
        side="buy",
        order_type="limit",
        quantity=100.0,
        price=1700.0,
        status=status,
        filled_qty=filled_qty,
        filled_price=filled_price,
        strategy_id="s1",
    )
    return o


def _order_row(store: TradeLedger, order_id: str):
    return store._db.execute_one(
        "SELECT order_id, status, filled_qty, filled_price, last_update_at "
        "FROM orders WHERE order_id = ?", (order_id,)
    )


# ═══════════════════════════════════════════
# 1. INSERT 含 3 列
# ═══════════════════════════════════════════


def test_insert_persists_filled_columns(store: TradeLogStore) -> None:
    """新订单 INSERT 落库：filled_qty/filled_price 有值、last_update_at 非空。"""
    store.record_order(_order("o1", OrderStatus.SUBMITTED))
    row = _order_row(store, "o1")
    assert row is not None
    assert row["filled_qty"] == 0.0
    assert row["filled_price"] == 0.0
    assert row["last_update_at"] is not None


# ═══════════════════════════════════════════
# 2. 状态流转 filled_qty 不被重置（A-08 核心验收）
# ═══════════════════════════════════════════


def test_status_flow_keeps_filled_qty(store: TradeLogStore) -> None:
    """部分成交→全部成交：filled_qty 递增保留（不被重置为缺省 0）。"""
    store.record_order(_order("o2", OrderStatus.SUBMITTED))
    store.record_order(_order("o2", OrderStatus.PARTIAL_FILLED,
                              filled_qty=40.0, filled_price=1701.5))
    row = _order_row(store, "o2")
    assert row["status"] == OrderStatus.PARTIAL_FILLED.value
    assert row["filled_qty"] == 40.0   # 不被重置
    assert row["filled_price"] == 1701.5

    store.record_order(_order("o2", OrderStatus.FILLED,
                              filled_qty=100.0, filled_price=1702.0))
    row = _order_row(store, "o2")
    assert row["status"] == OrderStatus.FILLED.value
    assert row["filled_qty"] == 100.0  # 全部成交量保留
    assert row["filled_price"] == 1702.0


def test_partial_then_cancel_keeps_filled_qty(store: TradeLedger) -> None:
    """部分成交后撤单（业务携带成交均价重报）：filled_qty/filled_price 保留。"""
    store.record_order(_order("o3", OrderStatus.SUBMITTED))
    store.record_order(_order("o3", OrderStatus.PARTIAL_FILLED,
                              filled_qty=30.0, filled_price=1699.0))
    store.record_order(_order("o3", OrderStatus.CANCELLED,
                              filled_qty=30.0, filled_price=1699.0))
    row = _order_row(store, "o3")
    assert row["status"] == OrderStatus.CANCELLED.value
    assert row["filled_qty"] == 30.0  # 残量不被重置为 0
    assert row["filled_price"] == 1699.0


# ═══════════════════════════════════════════
# 3. schema 双路一致 + create_all_tables 新库可用
# ═══════════════════════════════════════════


def test_schema_orders_has_filled_columns(store: TradeLogStore) -> None:
    """create_all_tables 建的 orders 表含 3 列（与 0009 迁移一致）。"""
    cols = {r["name"] for r in store._db.execute("PRAGMA table_info(orders)")}
    assert {"filled_qty", "filled_price", "last_update_at"} <= cols
