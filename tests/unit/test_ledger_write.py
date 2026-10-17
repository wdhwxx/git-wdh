"""T1-2/T1-8 单元测试：验证 ledger 写入逻辑（intent/ack/error）。"""
import os
import sqlite3
import tempfile

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.database.trade_log import TradeLedger
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType


@pytest.fixture
def temp_ledger():
    """临时数据库 + TradeLedger（每个测试独立）。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        db = DatabaseConnection(db_path)
        # 建表（简化：直接 CREATE TABLE）
        db.execute_update("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                order_type TEXT,
                quantity REAL,
                price REAL,
                status TEXT,
                strategy_id TEXT,
                engine_id TEXT,
                submitted_at REAL,
                created_at TEXT,
                client_order_id TEXT,
                broker_order_id TEXT,
                broker_id TEXT,
                account_id TEXT,
                filled_qty REAL,
                filled_price REAL,
                last_update_at REAL,
                raw_payload TEXT
            )
        """)
        db.execute_update("""
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                seq INTEGER,
                event_type TEXT,
                payload TEXT,
                created_at TEXT
            )
        """)
        db.execute_update("""
            CREATE TABLE IF NOT EXISTS reconciliation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT,
                run_type TEXT,
                started_at REAL,
                finished_at REAL,
                consistent INTEGER,
                frozen INTEGER,
                note TEXT
            )
        """)
        db.execute_update("""
            CREATE TABLE IF NOT EXISTS reconciliation_diffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                account_id TEXT,
                cash_diff REAL,
                position_diff TEXT,
                exceeds_threshold INTEGER
            )
        """)
        db.execute_update("""
            CREATE TABLE IF NOT EXISTS reconciliation_freeze (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT,
                action TEXT,
                actor TEXT,
                reason TEXT,
                created_at TEXT
            )
        """)
        ledger = TradeLedger(db)
        yield ledger, db_path
    finally:
        os.unlink(db_path)


class TestLedgerWrite:
    """验证 ledger 写入逻辑。"""

    def test_record_order_intent(self, temp_ledger):
        """T1-2：intent 事件写入 orders + order_events。"""
        ledger, db_path = temp_ledger
        order = Order(
            order_id="test_intent_001",
            symbol="600519.SH",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=1800.0,
            status=OrderStatus.SUBMITTED,
            strategy_id="ma_cross",
        )
        # 模拟 record_order_intent
        ledger.record_order(order, engine_id="test")
        ledger.record_order_event(
            order_id="test_intent_001", seq=1, event_type="intent",
            payload="status=submitted",
        )

        # 验证
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders WHERE order_id='test_intent_001'")
        assert cur.fetchone()[0] == 1, "orders 表应有 1 条"
        cur.execute("SELECT COUNT(*) FROM order_events WHERE order_id='test_intent_001'")
        assert cur.fetchone()[0] == 1, "order_events 表应有 1 条 intent"
        conn.close()

    def test_record_order_ack(self, temp_ledger):
        """T1-2：ack 事件更新订单状态 + 写事件。"""
        ledger, db_path = temp_ledger
        order = Order(
            order_id="test_ack_001",
            symbol="600519.SH",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=1800.0,
            status=OrderStatus.SUBMITTED,
            strategy_id="ma_cross",
        )
        ledger.record_order(order, engine_id="test")
        ledger.record_order_event(
            order_id="test_ack_001", seq=1, event_type="intent", payload="",
        )
        # ack：更新状态为 FILLED
        order.status = OrderStatus.FILLED
        ledger.record_order(order, engine_id="test")  # ON CONFLICT DO UPDATE
        ledger.record_order_event(
            order_id="test_ack_001", seq=2, event_type="ack", payload="status=filled",
        )

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders WHERE order_id='test_ack_001'")
        assert cur.fetchone()[0] == 1, "orders 表应只有 1 条（幂等）"
        cur.execute("SELECT COUNT(*) FROM order_events WHERE order_id='test_ack_001'")
        assert cur.fetchone()[0] == 2, "order_events 应有 2 条（intent + ack）"
        conn.close()

    def test_reconciliation_run(self, temp_ledger):
        """T2-5/T2-6：对账运行落库。"""
        import time
        ledger, db_path = temp_ledger
        run_id = ledger.record_reconciliation_run(
            account_id="test_account",
            run_type="realtime",
            started_at=time.time(),
            finished_at=time.time(),
            consistent=1,
            frozen=0,
            note="test",
        )
        assert run_id > 0, "run_id 应大于 0"

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM reconciliation_runs WHERE account_id='test_account'")
        assert cur.fetchone()[0] == 1, "reconciliation_runs 应有 1 条"
        conn.close()

    def test_reconciliation_freeze(self, temp_ledger):
        """T2-7/T2-8：冻结/解冻记录落库。"""
        ledger, db_path = temp_ledger
        ledger.record_reconciliation_freeze(
            account_id="test_account",
            action="freeze",
            actor="system",
            reason="cash_diff=100.00",
        )
        ledger.record_reconciliation_freeze(
            account_id="test_account",
            action="unfreeze",
            actor="admin",
            reason="人工处置完成",
        )

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM reconciliation_freeze WHERE account_id='test_account'")
        assert cur.fetchone()[0] == 2, "reconciliation_freeze 应有 2 条（freeze + unfreeze）"
        conn.close()

    def test_get_order_by_id(self, temp_ledger):
        """T2-2：按 order_id 查订单。"""
        ledger, db_path = temp_ledger
        order = Order(
            order_id="test_query_001",
            symbol="600519.SH",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=1800.0,
            status=OrderStatus.SUBMITTED,
        )
        ledger.record_order(order, engine_id="test")

        result = ledger.get_order_by_id("test_query_001")
        assert result is not None, "应查到订单"
        assert result["order_id"] == "test_query_001", "order_id 匹配"
        assert result["symbol"] == "600519.SH", "symbol 匹配"

    def test_get_order_events(self, temp_ledger):
        """T2-2：按 order_id 查事件链。"""
        ledger, db_path = temp_ledger
        order_id = "test_events_001"
        ledger.record_order_event(order_id=order_id, seq=1, event_type="intent", payload="")
        ledger.record_order_event(order_id=order_id, seq=2, event_type="ack", payload="")
        ledger.record_order_event(order_id=order_id, seq=3, event_type="fill", payload="")

        events = ledger.get_order_events(order_id)
        assert len(events) == 3, "应有 3 条事件"
        assert events[0]["event_type"] == "intent", "第 1 条是 intent"
        assert events[1]["event_type"] == "ack", "第 2 条是 ack"
        assert events[2]["event_type"] == "fill", "第 3 条是 fill"
