# 文件名：myquant/database/trade_log.py
"""
trade_log.py - 订单/成交/持仓关系账（KP-P4-02）
============================================
逐笔订单、成交、持仓日快照的持久化与查询层。解决审计缺陷"无订单/成交/
持仓关系账"：成交不再只存内存（broker._trades），成交回调同步落库，长期归档，
可追溯与 broker 逐笔一致的记录；持仓日快照按日追加，删除当前快照不影响历史查询。

表（schema.py / Alembic 0004）：
  - orders          — 逐笔订单（order_id 唯一）
  - fills           — 逐笔成交（trade_id 唯一）
  - positions_daily — 持仓日快照（snapshot_date+symbol+account_id 唯一）

依赖: myquant.database.connection（DatabaseConnection），myquant.models.trade
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from myquant.database.connection import DatabaseConnection
from myquant.database.schema import TABLE_NAMES
from myquant.models.trade import Order, Trade


def _enum_value(value: Any) -> str:
    """枚举取 .value，非枚举直接 str()"""
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


class TradeLedger:
    """订单/成交/持仓关系账持久化与查询。

    Attributes:
        db : DatabaseConnection — 已建好 orders/fills/positions_daily 三表的连接。
    """

    def __init__(self, db: DatabaseConnection) -> None:
        self._db = db

    # ── 写入：订单 ──

    def record_order(
        self, order: Order, engine_id: str = "default",
    ) -> None:
        """逐笔订单落库（order_id 唯一，重复上报幂等跳过）。"""
        sql = (
            f"INSERT OR IGNORE INTO {TABLE_NAMES['orders']} "
            "(order_id, symbol, side, order_type, quantity, price, status, "
            " strategy_id, engine_id, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._db.transaction():
            self._db.execute_update(
                sql,
                (
                    order.order_id,
                    order.symbol,
                    _enum_value(order.side),
                    _enum_value(order.order_type),
                    float(order.quantity),
                    float(order.price if order.price is not None else 0.0),
                    _enum_value(order.status),
                    order.strategy_id,
                    engine_id,
                    getattr(order, "submitted_at", None),
                ),
            )

    # ── 写入：成交 ──

    def record_fill(self, trade: Trade, engine_id: str = "default") -> None:
        """逐笔成交落库（trade_id 唯一，重复上报幂等跳过）。"""
        sql = (
            f"INSERT OR IGNORE INTO {TABLE_NAMES['fills']} "
            "(trade_id, order_id, symbol, side, quantity, price, commission, "
            " slippage, trade_time, strategy_id, engine_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._db.transaction():
            self._db.execute_update(
                sql,
                (
                    trade.trade_id,
                    trade.order_id,
                    trade.symbol,
                    _enum_value(trade.side),
                    float(trade.quantity),
                    float(trade.price),
                    float(trade.commission),
                    float(trade.slippage),
                    float(trade.timestamp),
                    trade.strategy_id,
                    engine_id,
                ),
            )

    # ── 写入：持仓日快照 ──

    def record_position_snapshot(
        self,
        snapshot_date: str,
        positions: List[Dict[str, Any]],
        *,
        cash: float = 0.0,
        account_id: str = "default",
    ) -> None:
        """持仓日快照落库（snapshot_date+symbol+account 唯一，同日重复覆盖）。"""
        sql = (
            f"INSERT OR REPLACE INTO {TABLE_NAMES['positions_daily']} "
            "(snapshot_date, symbol, quantity, avg_cost, market_price, market_value, "
            " realized_pnl, unrealized_pnl, cash, account_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        with self._db.transaction():
            for pos in positions:
                self._db.execute_update(
                    sql,
                    (
                        snapshot_date,
                        str(pos.get("symbol", "")),
                        float(pos.get("quantity", 0.0)),
                        float(pos.get("avg_cost", 0.0)),
                        float(pos.get("market_price", 0.0)),
                        float(pos.get("market_value", 0.0)),
                        float(pos.get("realized_pnl", 0.0)),
                        float(pos.get("unrealized_pnl", 0.0)),
                        float(cash),
                        account_id,
                    ),
                )

    # ── 查询 ──

    def query_orders(
        self, symbol: Optional[str] = None, status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询订单（可按标的/状态过滤），按提交时间倒序。"""
        where: List[str] = []
        params: List[Any] = []
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        if status:
            where.append("status = ?")
            params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._db.execute(
            f"SELECT * FROM {TABLE_NAMES['orders']}{where_sql} "
            "ORDER BY submitted_at DESC",
            tuple(params) or None,
        )
        return [dict(r) for r in rows]

    def query_fills(
        self, symbol: Optional[str] = None, order_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询成交（可按标的/订单过滤），按成交时间倒序。"""
        where: List[str] = []
        params: List[Any] = []
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        if order_id:
            where.append("order_id = ?")
            params.append(order_id)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._db.execute(
            f"SELECT * FROM {TABLE_NAMES['fills']}{where_sql} "
            "ORDER BY trade_time DESC",
            tuple(params) or None,
        )
        return [dict(r) for r in rows]

    def query_positions_on(self, snapshot_date: str) -> List[Dict[str, Any]]:
        """查询指定日期的持仓快照（历史可追溯）。"""
        rows = self._db.execute(
            f"SELECT * FROM {TABLE_NAMES['positions_daily']} "
            "WHERE snapshot_date = ? ORDER BY symbol",
            (snapshot_date,),
        )
        return [dict(r) for r in rows]
