# 文件名：tests/unit/test_trade_ledger.py
"""
test_trade_ledger.py - 订单/成交/持仓关系账测试（KP-P4-02 / T-08）
==================================================================
覆盖"无订单/成交/持仓关系账（仅 10 张表）"修复：
  1. Alembic 0004 upgrade head 建出 orders/fills/positions_daily 三表
     （DoD：alembic upgrade head + 查询单测）。
  2. TradeLedger 逐笔订单落库 → 查询与委托一致。
  3. 逐笔成交落库 → 与 broker 逐笔记录一致。
  4. 幂等 —— 同一 order_id/trade_id 重复上报不重复入库。
  5. 持仓日快照 —— 删除当前快照不影响历史查询（DoD 核心）。
  6. schema.create_all_tables 亦含三表（DDL 路径一致）。
  7. 引擎接线 —— LiveEngine 注入 trade_ledger 后，on_order_update 逐笔
     订单落库、on_trade 逐笔成交落库（成交回调同步落库）。

依赖: pytest, myquant.database（DatabaseConnection/create_all_tables/TABLE_NAMES/
      schema.table_exists）、myquant.database.trade_log（TradeLedger）、
      myquant.live.engine（LiveEngine/RiskConfig）、myquant.models.trade
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict

import pytest

from myquant.database import (
    TABLE_NAMES,
    DatabaseConnection,
    create_all_tables,
)
from myquant.database.schema import table_exists
from myquant.database.trade_log import TradeLedger
from myquant.live.engine import LiveEngine, RiskConfig
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType, Trade

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_LEDGER_TABLES = ("orders", "fills", "positions_daily")


@pytest.fixture
def conn(tmp_path: Path) -> DatabaseConnection:
    """临时文件库 + create_all_tables（schema DDL 路径）。"""
    db = DatabaseConnection(db_path=str(tmp_path / "ledger.db"))
    db.connect()
    create_all_tables(db)
    yield db
    db.close()


def _order(**kw: Dict) -> Order:
    defaults: Dict = {
        "symbol": "000001.SZ",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "quantity": 100.0,
        "status": OrderStatus.SUBMITTED,
        "strategy_id": "ma",
    }
    defaults.update(kw)
    return Order(**defaults)


def _trade(order_id: str = "ord-1", **kw: Dict) -> Trade:
    defaults: Dict = {
        "order_id": order_id,
        "symbol": "000001.SZ",
        "side": OrderSide.BUY,
        "quantity": 100.0,
        "price": 10.0,
        "commission": 1.0,
        "strategy_id": "ma",
    }
    defaults.update(kw)
    return Trade(**defaults)


# ═══════════════════════════════════════════════
#  1. Alembic 0004 upgrade head 建三表（DoD）
# ═══════════════════════════════════════════════

class TestAlembicUpgrade:
    def test_upgrade_head_建出三表(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic_ledger.db"
        env = {
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT),
            "QUANT_DB_URL": f"sqlite:///{db_path}",
        }
        r = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0, f"alembic upgrade 失败: {r.stderr}"
        db = DatabaseConnection(db_path=str(db_path))
        db.connect()
        try:
            for name in _LEDGER_TABLES:
                assert table_exists(db, TABLE_NAMES[name]), f"缺少表 {name}"
        finally:
            db.close()


# ═══════════════════════════════════════════════
#  2/3/4. TradeLedger 写入与幂等
# ═══════════════════════════════════════════════

class TestTradeLedger:
    def test_逐笔订单落库查询一致(self, conn: DatabaseConnection) -> None:
        ledger = TradeLedger(conn)
        order = _order()
        ledger.record_order(order, engine_id="e1")
        rows = ledger.query_orders(symbol="000001.SZ")
        assert len(rows) == 1
        assert rows[0]["order_id"] == order.order_id
        assert rows[0]["symbol"] == "000001.SZ"
        assert rows[0]["side"] == "BUY"
        assert rows[0]["status"] == "SUBMITTED"
        assert rows[0]["strategy_id"] == "ma"

    def test_逐笔成交落库与broker一致(self, conn: DatabaseConnection) -> None:
        ledger = TradeLedger(conn)
        trade = _trade(order_id="ord-9")
        ledger.record_fill(trade, engine_id="e1")
        rows = ledger.query_fills(symbol="000001.SZ")
        assert len(rows) == 1
        assert rows[0]["trade_id"] == trade.trade_id
        assert rows[0]["order_id"] == "ord-9"
        assert rows[0]["quantity"] == 100.0
        assert rows[0]["price"] == 10.0
        assert rows[0]["commission"] == 1.0

    def test_重复上报幂等不重复(self, conn: DatabaseConnection) -> None:
        ledger = TradeLedger(conn)
        order = _order()
        trade = _trade(order_id=order.order_id)
        ledger.record_order(order, engine_id="e1")
        ledger.record_order(order, engine_id="e1")   # 重复
        ledger.record_fill(trade, engine_id="e1")
        ledger.record_fill(trade, engine_id="e1")    # 重复
        assert len(ledger.query_orders()) == 1
        assert len(ledger.query_fills()) == 1

    def test_按状态与订单过滤(self, conn: DatabaseConnection) -> None:
        ledger = TradeLedger(conn)
        ledger.record_order(_order(order_id="a1", status=OrderStatus.FILLED), engine_id="e1")
        ledger.record_order(_order(order_id="a2", status=OrderStatus.REJECTED), engine_id="e1")
        filled = ledger.query_orders(status="FILLED")
        assert [r["order_id"] for r in filled] == ["a1"]


# ═══════════════════════════════════════════════
#  5. 持仓日快照：删除当前不影响历史（DoD 核心）
# ═══════════════════════════════════════════════

class TestPositionSnapshot:
    def test_删除当前快照不影响历史查询(self, conn: DatabaseConnection) -> None:
        ledger = TradeLedger(conn)
        pos_d1 = [{"symbol": "000001.SZ", "quantity": 100.0, "avg_cost": 10.0,
                   "market_price": 10.0, "market_value": 1000.0}]
        pos_d2 = [{"symbol": "000001.SZ", "quantity": 200.0, "avg_cost": 10.0,
                   "market_price": 11.0, "market_value": 2200.0}]
        ledger.record_position_snapshot("2026-10-02", pos_d1, cash=100_000.0)
        ledger.record_position_snapshot("2026-10-03", pos_d2, cash=99_000.0)
        # 删除"当前"最新快照（10-03）
        with conn.transaction():
            conn.execute_update(
                f"DELETE FROM {TABLE_NAMES['positions_daily']} "
                "WHERE snapshot_date = '2026-10-03'"
            )
        # 历史（10-02）仍可查询
        hist = ledger.query_positions_on("2026-10-02")
        assert len(hist) == 1
        assert hist[0]["quantity"] == 100.0
        # 删除日期已查不到（被删的当前快照）
        assert ledger.query_positions_on("2026-10-03") == []

    def test_同日重复覆盖不堆积(self, conn: DatabaseConnection) -> None:
        ledger = TradeLedger(conn)
        pos = [{"symbol": "000001.SZ", "quantity": 100.0}]
        ledger.record_position_snapshot("2026-10-02", pos, cash=100_000.0)
        pos2 = [{"symbol": "000001.SZ", "quantity": 150.0}]
        ledger.record_position_snapshot("2026-10-02", pos2, cash=100_000.0)
        rows = ledger.query_positions_on("2026-10-02")
        assert len(rows) == 1
        assert rows[0]["quantity"] == 150.0


# ═══════════════════════════════════════════════
#  6. schema.create_all_tables 含三表
# ═══════════════════════════════════════════════

class TestSchemaDDL:
    def test_create_all_tables含三表(self, conn: DatabaseConnection) -> None:
        for name in _LEDGER_TABLES:
            assert table_exists(conn, TABLE_NAMES[name]), f"缺少表 {name}"


# ═══════════════════════════════════════════════
#  7. 引擎接线：成交回调同步落库
# ═══════════════════════════════════════════════

class TestEngineWiring:
    def _make_engine(self, tmp_path: Path, ledger: TradeLedger) -> LiveEngine:
        from myquant.live.state_store import StateStore
        engine = LiveEngine(
            engine_id="ledger_test",
            risk_config=RiskConfig(),
            state_store=StateStore(root=str(tmp_path), store_id="ledger_test"),
            trade_ledger=ledger,
            enable_risk_manager=False,
            enable_guardian=False,
            enable_state_store=False,
            enable_scheduler=False,
            enable_event_bus=False,
            enable_lookahead_guard=False,
        )
        return engine

    def test_on_order_update逐笔订单落库(self, conn: DatabaseConnection, tmp_path: Path) -> None:
        ledger = TradeLedger(conn)
        engine = self._make_engine(tmp_path, ledger)
        order = _order()
        engine.on_order_update(order)
        rows = ledger.query_orders(symbol="000001.SZ")
        assert len(rows) == 1
        assert rows[0]["order_id"] == order.order_id

    def test_on_trade逐笔成交落库(self, conn: DatabaseConnection, tmp_path: Path) -> None:
        ledger = TradeLedger(conn)
        engine = self._make_engine(tmp_path, ledger)
        trade = _trade(order_id="ord-w1")
        engine.on_trade(trade)
        rows = ledger.query_fills(symbol="000001.SZ")
        assert len(rows) == 1
        assert rows[0]["trade_id"] == trade.trade_id
        assert rows[0]["order_id"] == "ord-w1"
