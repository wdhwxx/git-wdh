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
        # T1-7：AuditLogStore 哈希链（订单/成交/对账/冻结四类写入同步审计）
        self._audit = None
        try:
            from myquant.core.audit_log import AuditLogStore
            self._audit = AuditLogStore()
        except Exception:
            self._audit = None  # 审计存储不可用不阻断交易

    # ── 写入：订单 ──

    def record_order(
        self, order: Order, engine_id: str = "default",
    ) -> None:
        """逐笔订单落库（order_id 唯一，T1-4：ON CONFLICT DO UPDATE 更新状态/残量）。"""
        # T1-4：submitted_at 恒 NULL 修复——用 order.created_at 落库（created_at 是订单创建时刻）
        submitted_ts = getattr(order, "submitted_at", None) or getattr(order, "created_at", None)
        # T1-4：新增列（0009 迁移）——filled_qty / filled_price / last_update_at / broker_id 等
        sql = (
            f"INSERT INTO {TABLE_NAMES['orders']} "
            "(order_id, symbol, side, order_type, quantity, price, status, "
            " strategy_id, engine_id, submitted_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(order_id) DO UPDATE SET "
            "  status=excluded.status, "
            "  filled_qty=excluded.filled_qty, "
            "  filled_price=excluded.filled_price, "
            "  last_update_at=excluded.last_update_at"
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
                    submitted_ts,
                ),
            )
        # T1-7：同步审计哈希链（order 类）
        if self._audit is not None:
            try:
                self._audit.append(
                    kind="order",
                    action=_enum_value(order.status),
                    actor="trade_ledger",
                    detail={"order_id": order.order_id, "symbol": order.symbol},
                )
            except Exception:
                pass  # 审计写失败不阻断交易

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
        # T1-7：同步审计哈希链（fill 类）
        if self._audit is not None:
            try:
                self._audit.append(
                    kind="fill",
                    action="create",
                    actor="trade_ledger",
                    detail={"trade_id": trade.trade_id, "order_id": trade.order_id},
                )
            except Exception:
                pass

    # ── 写入：订单事件（T1-4，append-only）──

    def record_order_event(
        self, order_id: str, seq: int, event_type: str, payload: str = "",
    ) -> None:
        """T1-4：订单事件落库（append-only, order_id+seq 唯一）。"""
        sql = (
            "INSERT OR IGNORE INTO order_events "
            "(order_id, seq, event_type, payload) "
            "VALUES (?, ?, ?, ?)"
        )
        with self._db.transaction():
            self._db.execute_update(sql, (order_id, seq, event_type, payload))

    # ── 写入：对账记录（T1-4）──

    def record_reconciliation_run(
        self, account_id: str, run_type: str, started_at: float,
        finished_at: float = None, consistent: int = None, frozen: int = 0,
        note: str = "",
    ) -> int:
        """T1-4：对账批次落库，返回 run_id。"""
        sql = (
            "INSERT INTO reconciliation_runs "
            "(account_id, run_type, started_at, finished_at, consistent, frozen, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        with self._db.transaction():
            self._db.execute_update(
                sql, (account_id, run_type, started_at, finished_at, consistent, frozen, note),
            )
            # execute_update 返回 rowcount(int)，用 SELECT last_insert_rowid() 取 run_id
            rows = self._db.execute("SELECT last_insert_rowid()")
            return rows[0][0] if rows else 0
        # T1-7：同步审计哈希链（reconciliation 类）
        if self._audit is not None:
            try:
                self._audit.append(
                    kind="reconciliation",
                    action=run_type,
                    actor="trade_ledger",
                    detail={"account_id": account_id, "consistent": consistent, "frozen": frozen},
                )
            except Exception:
                pass

    def record_reconciliation_diff(
        self, run_id: int, account_id: str, cash_diff: float = None,
        position_diff: str = "", exceeds_threshold: int = 0,
    ) -> None:
        """T1-4：对账差异落库。"""
        sql = (
            "INSERT INTO reconciliation_diffs "
            "(run_id, account_id, cash_diff, position_diff, exceeds_threshold) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        with self._db.transaction():
            self._db.execute_update(
                sql, (run_id, account_id, cash_diff, position_diff, exceeds_threshold),
            )

    def record_reconciliation_freeze(
        self, account_id: str, action: str, actor: str, reason: str = "",
    ) -> None:
        """T1-4：冻结/解冻记录落库。"""
        sql = (
            "INSERT INTO reconciliation_freeze "
            "(account_id, action, actor, reason) "
            "VALUES (?, ?, ?, ?)"
        )
        with self._db.transaction():
            self._db.execute_update(sql, (account_id, action, actor, reason))
        # T1-7：同步审计哈希链（freeze 类）
        if self._audit is not None:
            try:
                self._audit.append(
                    kind="freeze",
                    action=action,
                    actor=actor,
                    detail={"account_id": account_id, "reason": reason},
                )
            except Exception:
                pass

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

    # ── 查询：T2-2 订单账本查询接口 ──

    def get_order_by_id(self, order_id: str) -> Optional[Dict[str, Any]]:
        """T2-2：按 order_id 查订单状态（用于对账/排障）。"""
        rows = self._db.execute(
            f"SELECT * FROM {TABLE_NAMES['orders']} WHERE order_id = ?",
            (order_id,),
        )
        if not rows:
            return None
        row = rows[0]
        # sqlite3.Row 支持 keys()
        return dict(zip(row.keys(), row))

    def get_order_events(self, order_id: str) -> List[Dict[str, Any]]:
        """T2-2：按 order_id 查事件链（intent/ack/error/fill，按 seq 升序）。"""
        rows = self._db.execute(
            f"SELECT * FROM {TABLE_NAMES['order_events']} WHERE order_id = ? ORDER BY seq ASC",
            (order_id,),
        )
        if not rows:
            return []
        return [dict(zip(r.keys(), r)) for r in rows]

    # ── 查询：T2-4 对账历史查询 ──

    def query_reconciliation_runs(
        self, start_date: str = "", end_date: str = "", limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """T2-4：按日期范围查对账运行记录（按时间倒序）。"""
        where: List[str] = []
        params: List[Any] = []
        if start_date:
            where.append("run_ts >= ?")
            params.append(start_date)
        if end_date:
            where.append("run_ts <= ?")
            params.append(end_date)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._db.execute(
            f"SELECT * FROM reconciliation_runs{where_sql} ORDER BY run_ts DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._db.execute(
            "SELECT * FROM reconciliation_runs LIMIT 0"
        ).description]
        return [dict(zip(cols, r)) for r in rows]

    def query_reconciliation_diffs(
        self, run_id: Optional[int] = None, limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """T2-4：按 run_id 查对账差异明细。"""
        where: str = ""
        params: List[Any] = []
        if run_id is not None:
            where = " WHERE run_id = ?"
            params.append(run_id)
        rows = self._db.execute(
            f"SELECT * FROM reconciliation_diffs{where} ORDER BY id DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._db.execute(
            "SELECT * FROM reconciliation_diffs LIMIT 0"
        ).description]
        return [dict(zip(cols, r)) for r in rows]

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
