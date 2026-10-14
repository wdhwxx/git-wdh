# 文件名：myquant/database/queries.py
"""
queries.py - 统一数据查询接口
==============================
功能清单:
  1. 行情数据CRUD（查询/批量插入/更新/删除）
  2. 财务数据CRUD
  3. 因子数据CRUD
  4. 策略状态读写（持久化/崩溃恢复）
  5. 数据源管理（注册/查询/健康状态更新）
  6. 同步状态管理（更新/查询/统计）
  7. 防未来函数：查询时严格按截止日期过滤，不返回未来数据

设计原则:
  - 所有查询方法支持 end_date 参数，严格不返回截止日期之后的数据（防未来函数）
  - 批量插入使用 executemany，性能最优
  - 查询结果支持返回字典列表或pandas DataFrame
  - 所有写入操作在事务中执行，保证原子性
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 任务13：脏数据类型/修复策略自动推断（与 core/constants.py 对接）
from myquant.core.constants import DIRTY_TYPE_REPAIR_MAP, DirtyDataType

from .connection import DatabaseConnection
from .schema import (
    TABLE_NAMES,
    CleanRunMetaSchema,
    DirtyDataStatsSchema,
    FactorDataSchema,
    FinancialDataSchema,
)


class DataQueries:
    """
    统一数据查询接口
    ================
    封装所有数据库操作，上层（data_access/strategies）只调用此接口，
    不直接写SQL，保证数据访问层统一。

    Usage:
        from myquant.database import get_connection, DataQueries
        conn = get_connection()
        queries = DataQueries(conn)

        # 查询行情数据
        df = queries.get_market_data("000001.SZ", "2024-01-01", "2024-12-31")

        # 批量插入行情数据
        queries.bulk_insert_market_data([...])
    """

    def __init__(self, connection: DatabaseConnection):
        self.conn = connection

    # ═══════════════════════════════════════════
    # 行情数据操作
    # ═══════════════════════════════════════════

    def get_market_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        adjust: Optional[str] = "raw",
        market: str = "A股",
    ) -> List[Dict]:
        """
        查询单只股票行情数据

        【防未来函数】end_date 之后的数据绝对不返回

        Parameters
        ----------
        symbol : str
            股票代码，如 000001.SZ
        start_date : str | None
            开始日期 YYYY-MM-DD，None则不限制
        end_date : str | None
            截止日期 YYYY-MM-DD，None则不限制（防未来函数关键参数）
        period : str
            周期 daily/minute/tick
        adjust : str | None
            复权方式 hfq/qfq/None
        market : str
            市场 A股/港股/美股

        Returns
        -------
        list[dict] : 行情数据列表，按日期升序
        """
        conditions = ["symbol = ?", "period = ?", "market = ?"]
        params: List[Any] = [symbol, period, market]

        if adjust is not None:
            conditions.append("adjust = ?")
            params.append(adjust)
        else:
            conditions.append("adjust IS NULL")

        if start_date:
            conditions.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            # 【防未来函数】严格过滤截止日期
            conditions.append("trade_date <= ?")
            params.append(end_date)

        # P1-③(#83)：过滤停牌占位行（open/close 为 NULL），防污染回测；
        # is_suspended=1 事实行保留在库，由专用查询/统计消费
        sql = f"""
            SELECT * FROM {TABLE_NAMES['market_data']}
            WHERE {' AND '.join(conditions)}
              AND open IS NOT NULL AND close IS NOT NULL
            ORDER BY trade_date ASC, trade_time ASC
        """
        rows = self.conn.execute(sql, tuple(params))
        return [dict(row) for row in rows]

    def get_market_data_batch(
        self,
        symbols: List[str],
        trade_date: str,
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ) -> Dict[str, Dict]:
        """
        批量查询多只股票某一天的行情数据

        【防未来函数】只返回指定日期的数据，不返回之后的

        Returns
        -------
        dict[str, dict] : {symbol: 行情数据字典}
        """
        if not symbols:
            return {}
        placeholders = ",".join(["?"] * len(symbols))
        adjust_clause = "adjust = ?" if adjust else "adjust IS NULL"
        params = list(symbols) + [trade_date, period]
        if adjust:
            params.append(adjust)

        # P1-③(#83)：过滤停牌占位行（open/close 为 NULL），防污染回测
        sql = f"""
            SELECT * FROM {TABLE_NAMES['market_data']}
            WHERE symbol IN ({placeholders})
              AND trade_date = ?
              AND period = ?
              AND {adjust_clause}
              AND open IS NOT NULL AND close IS NOT NULL
        """
        rows = self.conn.execute(sql, tuple(params))
        return {row["symbol"]: dict(row) for row in rows}

    def get_latest_market_date(
        self,
        symbol: str,
        period: str = "daily",
        adjust: Optional[str] = "raw",
    ) -> Optional[str]:
        """查询某只股票最新的行情日期（用于增量更新判断）"""
        adjust_clause = "adjust = ?" if adjust else "adjust IS NULL"
        params = [symbol, period]
        if adjust:
            params.append(adjust)
        sql = f"""
            SELECT MAX(trade_date) as max_date FROM {TABLE_NAMES['market_data']}
            WHERE symbol = ? AND period = ? AND {adjust_clause}
        """
        row = self.conn.execute_one(sql, tuple(params))
        return row["max_date"] if row and row["max_date"] else None

    def bulk_insert_market_data(
        self,
        data_list: List[Dict],
        replace: bool = True,
    ) -> int:
        """
        批量插入行情数据

        Parameters
        ----------
        data_list : list[dict]
            行情数据列表，每个dict包含字段：
            symbol, market, period, adjust, trade_date, trade_time,
            open, high, low, close, volume, amount, pct_chg, turnover,
            is_suspended, data_source
        replace : bool
            True=冲突时替换（INSERT OR REPLACE），False=冲突时忽略

        Returns
        -------
        int : 插入/替换的记录数
        """
        if not data_list:
            return 0
        columns = [
            "symbol", "market", "period", "adjust", "trade_date", "trade_time",
            "open", "high", "low", "close", "volume", "amount",
            "pct_chg", "turnover", "amplitude", "volume_ratio", "float_share", "pe_dynamic", "is_suspended", "data_source",
        ]
        placeholders = ",".join(["?"] * len(columns))
        action = "REPLACE" if replace else "IGNORE"
        sql = f"INSERT OR {action} INTO {TABLE_NAMES['market_data']} ({','.join(columns)}) VALUES ({placeholders})"

        params_list = []
        for item in data_list:
            params_list.append(tuple(item.get(col) for col in columns))

        with self.conn.transaction():
            return self.conn.executemany(sql, params_list)

    def delete_market_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> int:
        """删除指定股票的行情数据（用于数据修复）"""
        conditions = ["symbol = ?"]
        params = [symbol]
        if start_date:
            conditions.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("trade_date <= ?")
            params.append(end_date)
        sql = f"DELETE FROM {TABLE_NAMES['market_data']} WHERE {' AND '.join(conditions)}"
        with self.conn.transaction():
            return self.conn.execute_update(sql, tuple(params))

    # ═══════════════════════════════════════════
    # 财务数据操作
    # ═══════════════════════════════════════════

    def get_financial_data(
        self,
        symbol: str,
        report_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        查询财务数据

        【防未来函数】end_date 之后的报告期数据不返回
        """
        conditions = ["symbol = ?"]
        params = [symbol]
        if report_date:
            conditions.append("report_date = ?")
            params.append(report_date)
        if end_date:
            conditions.append("report_date <= ?")
            params.append(end_date)
        sql = f"""
            SELECT * FROM {TABLE_NAMES['financial_data']}
            WHERE {' AND '.join(conditions)}
            ORDER BY report_date DESC
        """
        rows = self.conn.execute(sql, tuple(params))
        return [dict(row) for row in rows]

    def bulk_insert_financial_data(
        self,
        data_list: List[Dict],
        replace: bool = True,
    ) -> int:
        """批量插入财务数据"""
        if not data_list:
            return 0
        columns = FinancialDataSchema.COLUMNS[1:]  # 排除自增id
        placeholders = ",".join(["?"] * len(columns))
        action = "REPLACE" if replace else "IGNORE"
        sql = f"INSERT OR {action} INTO {TABLE_NAMES['financial_data']} ({','.join(columns)}) VALUES ({placeholders})"
        params_list = [tuple(item.get(col) for col in columns) for item in data_list]
        with self.conn.transaction():
            return self.conn.executemany(sql, params_list)

    # ═══════════════════════════════════════════
    # 因子数据操作
    # ═══════════════════════════════════════════

    def get_factor_data(
        self,
        factor_name: str,
        trade_date: Optional[str] = None,
        symbol: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        查询因子数据

        【防未来函数】end_date 之后的因子数据不返回
        """
        conditions = ["factor_name = ?"]
        params = [factor_name]
        if trade_date:
            conditions.append("trade_date = ?")
            params.append(trade_date)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if end_date:
            conditions.append("trade_date <= ?")
            params.append(end_date)
        sql = f"""
            SELECT * FROM {TABLE_NAMES['factor_data']}
            WHERE {' AND '.join(conditions)}
            ORDER BY trade_date ASC, symbol ASC
        """
        rows = self.conn.execute(sql, tuple(params))
        return [dict(row) for row in rows]

    def bulk_insert_factor_data(
        self,
        data_list: List[Dict],
        replace: bool = True,
    ) -> int:
        """批量插入因子数据"""
        if not data_list:
            return 0
        columns = FactorDataSchema.COLUMNS[1:]
        placeholders = ",".join(["?"] * len(columns))
        action = "REPLACE" if replace else "IGNORE"
        sql = f"INSERT OR {action} INTO {TABLE_NAMES['factor_data']} ({','.join(columns)}) VALUES ({placeholders})"
        params_list = [tuple(item.get(col) for col in columns) for item in data_list]
        with self.conn.transaction():
            return self.conn.executemany(sql, params_list)

    # ═══════════════════════════════════════════
    # 策略状态操作（持久化/崩溃恢复）
    # ═══════════════════════════════════════════

    def save_strategy_state(
        self,
        strategy_name: str,
        run_id: str,
        state_key: str,
        state_value: Any,
        value_type: str = "json",
    ) -> None:
        """保存策略状态（用于崩溃恢复）"""
        if value_type == "json":
            stored_value = json.dumps(state_value, ensure_ascii=False, default=str)
        else:
            stored_value = str(state_value)
        sql = f"""
            INSERT OR REPLACE INTO {TABLE_NAMES['strategy_state']}
            (strategy_name, run_id, state_key, state_value, value_type)
            VALUES (?, ?, ?, ?, ?)
        """
        with self.conn.transaction():
            self.conn.execute_update(sql, (strategy_name, run_id, state_key, stored_value, value_type))

    def load_strategy_state(
        self,
        strategy_name: str,
        run_id: str,
        state_key: str,
        default: Any = None,
    ) -> Any:
        """加载策略状态（崩溃恢复时调用）"""
        sql = f"""
            SELECT state_value, value_type FROM {TABLE_NAMES['strategy_state']}
            WHERE strategy_name = ? AND run_id = ? AND state_key = ?
        """
        row = self.conn.execute_one(sql, (strategy_name, run_id, state_key))
        if not row:
            return default
        if row["value_type"] == "json":
            return json.loads(row["state_value"])
        return row["state_value"]

    def get_all_strategy_states(
        self,
        strategy_name: str,
        run_id: str,
    ) -> Dict[str, Any]:
        """获取某次运行的所有状态"""
        sql = f"""
            SELECT state_key, state_value, value_type FROM {TABLE_NAMES['strategy_state']}
            WHERE strategy_name = ? AND run_id = ?
        """
        rows = self.conn.execute(sql, (strategy_name, run_id))
        result = {}
        for row in rows:
            if row["value_type"] == "json":
                result[row["state_key"]] = json.loads(row["state_value"])
            else:
                result[row["state_key"]] = row["state_value"]
        return result

    # ═══════════════════════════════════════════
    # 数据源管理
    # ═══════════════════════════════════════════

    def register_data_source(
        self,
        source_name: str,
        source_type: str,
        priority: int = 100,
        config: Optional[Dict] = None,
        description: str = "",
    ) -> None:
        """注册数据源"""
        config_json = json.dumps(config, ensure_ascii=False) if config else None
        sql = f"""
            INSERT OR REPLACE INTO {TABLE_NAMES['data_source']}
            (source_name, source_type, priority, is_enabled, config_json, description)
            VALUES (?, ?, ?, 1, ?, ?)
        """
        with self.conn.transaction():
            self.conn.execute_update(sql, (source_name, source_type, priority, config_json, description))

    def get_enabled_data_sources(self) -> List[Dict]:
        """获取所有启用的数据源，按优先级排序"""
        sql = f"""
            SELECT * FROM {TABLE_NAMES['data_source']}
            WHERE is_enabled = 1
            ORDER BY priority ASC
        """
        rows = self.conn.execute(sql)
        return [dict(row) for row in rows]

    def update_data_source_health(
        self,
        source_name: str,
        health_status: str,
        fail_count: Optional[int] = None,
    ) -> None:
        """更新数据源健康状态"""
        if fail_count is None:
            sql = f"""
                UPDATE {TABLE_NAMES['data_source']}
                SET health_status = ?, last_check_time = datetime('now')
                WHERE source_name = ?
            """
            params = (health_status, source_name)
        else:
            sql = f"""
                UPDATE {TABLE_NAMES['data_source']}
                SET health_status = ?, fail_count = ?, last_check_time = datetime('now')
                WHERE source_name = ?
            """
            params = (health_status, fail_count, source_name)
        with self.conn.transaction():
            self.conn.execute_update(sql, params)

    # ═══════════════════════════════════════════
    # 同步状态管理
    # ═══════════════════════════════════════════

    def update_sync_status(
        self,
        symbol: str,
        period: str,
        data_type: str,
        last_sync_date: str,
        record_count: int = 0,
        sync_status: str = "completed",
        data_source: str = "",
        error_message: str = "",
    ) -> None:
        """更新数据同步状态"""
        sql = f"""
            INSERT OR REPLACE INTO {TABLE_NAMES['sync_status']}
            (symbol, period, data_type, last_sync_date, last_sync_time,
             record_count, sync_status, data_source, error_message)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
        """
        with self.conn.transaction():
            self.conn.execute_update(sql, (
                symbol, period, data_type, last_sync_date,
                record_count, sync_status, data_source, error_message,
            ))

    def get_sync_status(
        self,
        symbol: Optional[str] = None,
        sync_status: Optional[str] = None,
    ) -> List[Dict]:
        """查询同步状态"""
        conditions = []
        params = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if sync_status:
            conditions.append("sync_status = ?")
            params.append(sync_status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM {TABLE_NAMES['sync_status']} {where} ORDER BY symbol"
        rows = self.conn.execute(sql, tuple(params))
        return [dict(row) for row in rows]

    def get_pending_symbols(
        self,
        all_symbols: List[str],
        period: str = "daily",
        data_type: str = "market",
    ) -> List[str]:
        """
        获取需要同步的股票列表（对比全量股票和已同步状态）

        返回：尚未同步或同步失败的股票代码列表
        """
        if not all_symbols:
            return []
        placeholders = ",".join(["?"] * len(all_symbols))
        sql = f"""
            SELECT symbol FROM {TABLE_NAMES['sync_status']}
            WHERE symbol IN ({placeholders})
              AND period = ?
              AND data_type = ?
              AND sync_status = 'completed'
        """
        rows = self.conn.execute(sql, tuple(all_symbols) + (period, data_type))
        synced = {row["symbol"] for row in rows}
        return [s for s in all_symbols if s not in synced]

    # ═══════════════════════════════════════════
    # 统计查询
    # ═══════════════════════════════════════════

    def get_market_data_count(self, symbol: Optional[str] = None) -> int:
        """获取行情数据记录数"""
        if symbol:
            row = self.conn.execute_one(
                f"SELECT COUNT(*) as cnt FROM {TABLE_NAMES['market_data']} WHERE symbol = ?",
                (symbol,),
            )
        else:
            row = self.conn.execute_one(
                f"SELECT COUNT(*) as cnt FROM {TABLE_NAMES['market_data']}"
            )
        return row["cnt"] if row else 0

    def get_all_symbols(self, period: str = "daily") -> List[str]:
        """获取数据库中所有有行情数据的股票代码"""
        sql = f"""
            SELECT DISTINCT symbol FROM {TABLE_NAMES['market_data']}
            WHERE period = ? ORDER BY symbol
        """
        rows = self.conn.execute(sql, (period,))
        return [row["symbol"] for row in rows]


# ═══════════════════════════════════════════════
# 清洗数据查询接口（任务13新增）
# ═══════════════════════════════════════════════

class CleanDataQueries:
    """
    清洗数据查询接口（任务13）
    =========================
    封装清洗运行元信息表(clean_run_meta)与脏数据统计表(dirty_data_stats)的完整 CRUD。

    设计原则:
      - 与 PipelineResult / PipelineCompleteEvent / DataQualityEvent 结构对接
      - 写入在事务中执行；批量插入使用 executemany，性能最优
      - 【防未来函数】查询支持截止时间过滤，绝不返回"未来"已记录的运行
      - 为实盘无缝对接：source 字段区分 offline/live，支持流式逐条累加写入

    Usage:
        from myquant.database import get_connection, CleanDataQueries
        conn = get_connection()
        cq = CleanDataQueries(conn)

        # 离线批量：一次写入运行元信息 + 脏数据统计
        run_id = cq.insert_clean_run(data_type="bar", symbol="000001.SZ", status="completed", ...)
        cq.insert_dirty_stats(run_id=run_id, symbol="000001.SZ", data_type="bar",
                              dirty_type="ohlc_logic_error", bad_count=3)

        # 实盘流式：逐条事件累加
        cq.accumulate_dirty_stats(run_id=run_id, symbol="000001.SZ", data_type="bar",
                                  dirty_type="price_jump", bad_count=1)

        # 查询与汇总
        runs = cq.get_clean_runs(symbol="000001.SZ")
        summary = cq.get_dirty_stats_summary(run_id=run_id)
    """

    def __init__(self, connection: DatabaseConnection):
        self.conn = connection

    # ─────────────────────────────────────────────
    # 内部工具
    # ─────────────────────────────────────────────

    @staticmethod
    def _now_utc() -> str:
        """当前UTC时间（YYYY-MM-DD HH:MM:SS）"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _generate_run_id(data_type: str, symbol: str) -> str:
        """自动生成运行ID（时间戳+微秒，保证唯一）"""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        return f"clean_{data_type or 'x'}_{symbol or 'x'}_{ts}"

    @staticmethod
    def _resolve_severity(dirty_type: str, severity: Optional[str] = None) -> str:
        """脏数据严重级别：显式传入优先，否则从 DirtyDataType 推断"""
        if severity:
            return severity
        try:
            return DirtyDataType(dirty_type).severity
        except (ValueError, KeyError):
            return "error"

    @staticmethod
    def _resolve_repair_strategy(dirty_type: str, repair_strategy: Optional[str] = None) -> str:
        """修复策略：显式传入优先，否则从 DIRTY_TYPE_REPAIR_MAP 推断"""
        if repair_strategy:
            return repair_strategy
        try:
            return DIRTY_TYPE_REPAIR_MAP[DirtyDataType(dirty_type)].value
        except (ValueError, KeyError):
            return ""

    @staticmethod
    def _json_dumps(value: Any) -> Optional[str]:
        """JSON序列化（None返回None）"""
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _json_loads(value: Optional[str], default: Any = None) -> Any:
        """JSON反序列化（空值返回默认）"""
        if not value:
            return default
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default

    # ─────────────────────────────────────────────
    # 清洗运行元信息 clean_run_meta
    # ─────────────────────────────────────────────

    def insert_clean_run(
        self,
        *,
        data_type: str,
        symbol: str,
        status: str = CleanRunMetaSchema.STATUS_RUNNING,
        total: int = 0,
        valid: int = 0,
        bad: int = 0,
        valid_rate: Optional[float] = None,
        quality_grade: Optional[str] = None,
        severity: Optional[str] = None,
        steps_executed: Optional[List[str]] = None,
        step_durations: Optional[Dict[str, float]] = None,
        total_duration: Optional[float] = None,
        bad_file: Optional[str] = None,
        report_file: Optional[str] = None,
        processed_file: Optional[str] = None,
        source: str = CleanRunMetaSchema.SOURCE_OFFLINE,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        error: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """
        插入一条清洗运行元信息，返回 run_id

        Parameters
        ----------
        run_id : str | None
            运行ID，None 则自动生成（clean_{data_type}_{symbol}_{时间戳}）
        data_type / symbol / status : str
            数据类型 / 标的 / 状态
        total / valid / bad : int
            原始总数 / 有效数 / 脏数据数
        valid_rate : float | None
            合格率，None 且 total>0 时自动计算 valid/total
        quality_grade : str | None
            质量等级（QualityGrade.value）
        severity : str | None
            严重级别（SeverityLevel.value）
        steps_executed : list[str] | None
            已执行步骤（自动JSON序列化）
        step_durations : dict[str,float] | None
            步骤耗时（自动JSON序列化）
        total_duration : float | None
            总耗时（秒）
        bad_file / report_file / processed_file : str | None
            输出文件路径
        source : str
            触发来源 offline/live
        started_at / finished_at : str | None
            起止时间（UTC），None 则自动取当前时间
        error : str | None
            错误信息（失败时）

        Returns
        -------
        str : run_id
        """
        if run_id is None:
            run_id = self._generate_run_id(data_type, symbol)
        if valid_rate is None and total > 0:
            valid_rate = round(valid / total, 6)
        if started_at is None:
            started_at = self._now_utc()

        columns = CleanRunMetaSchema.WRITE_COLUMNS
        placeholders = ",".join(["?"] * len(columns))
        params = (
            run_id, data_type, symbol, status,
            total, valid, bad, valid_rate, quality_grade,
            severity,
            self._json_dumps(steps_executed),
            self._json_dumps(step_durations),
            total_duration,
            bad_file, report_file, processed_file, source,
            started_at, finished_at, error,
        )
        sql = (
            f"INSERT OR REPLACE INTO {CleanRunMetaSchema.TABLE_NAME} "
            f"({','.join(columns)}) VALUES ({placeholders})"
        )
        with self.conn.transaction():
            self.conn.execute_update(sql, params)
        return run_id

    def update_clean_run_status(
        self,
        run_id: str,
        status: str,
        *,
        total_duration: Optional[float] = None,
        error: Optional[str] = None,
        finished_at: Optional[str] = None,
        quality_grade: Optional[str] = None,
        severity: Optional[str] = None,
        steps_executed: Optional[List[str]] = None,
        step_durations: Optional[Dict[str, float]] = None,
        bad_file: Optional[str] = None,
        report_file: Optional[str] = None,
        processed_file: Optional[str] = None,
    ) -> bool:
        """
        更新清洗运行状态（running → completed/failed）

        用于离线流水线先写 running 后写终态，或实盘会话状态更新。
        返回是否更新成功（run_id 不存在返回 False）。
        """
        sets = ["status = ?"]
        params: List[Any] = [status]
        if total_duration is not None:
            sets.append("total_duration = ?")
            params.append(total_duration)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if finished_at is not None:
            sets.append("finished_at = ?")
            params.append(finished_at)
        elif status in (CleanRunMetaSchema.STATUS_COMPLETED, CleanRunMetaSchema.STATUS_FAILED):
            # 进入终态时自动记录结束时间
            sets.append("finished_at = ?")
            params.append(self._now_utc())
        if quality_grade is not None:
            sets.append("quality_grade = ?")
            params.append(quality_grade)
        if severity is not None:
            sets.append("severity = ?")
            params.append(severity)
        if steps_executed is not None:
            sets.append("steps_executed = ?")
            params.append(self._json_dumps(steps_executed))
        if step_durations is not None:
            sets.append("step_durations = ?")
            params.append(self._json_dumps(step_durations))
        if bad_file is not None:
            sets.append("bad_file = ?")
            params.append(bad_file)
        if report_file is not None:
            sets.append("report_file = ?")
            params.append(report_file)
        if processed_file is not None:
            sets.append("processed_file = ?")
            params.append(processed_file)

        params.append(run_id)
        sql = (
            f"UPDATE {CleanRunMetaSchema.TABLE_NAME} SET {', '.join(sets)} "
            f"WHERE run_id = ?"
        )
        with self.conn.transaction():
            affected = self.conn.execute_update(sql, tuple(params))
        return affected > 0

    def get_clean_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """按 run_id 查询单条清洗运行元信息（steps/耗时自动反序列化）"""
        sql = f"SELECT * FROM {CleanRunMetaSchema.TABLE_NAME} WHERE run_id = ?"
        row = self.conn.execute_one(sql, (run_id,))
        if not row:
            return None
        return self._deserialize_clean_run(dict(row))

    def get_clean_runs(
        self,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        条件查询清洗运行元信息（按 started_at 倒序）

        【防未来函数】end_time 提供时严格只返回 started_at <= end_time 的记录，
        绝不返回"未来"运行。
        """
        conditions: List[str] = []
        params: List[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if data_type:
            conditions.append("data_type = ?")
            params.append(data_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if start_time:
            conditions.append("started_at >= ?")
            params.append(start_time)
        if end_time:
            # 【防未来函数】严格截止
            conditions.append("started_at <= ?")
            params.append(end_time)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT * FROM {CleanRunMetaSchema.TABLE_NAME} {where} "
            f"ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        rows = self.conn.execute(sql, tuple(params) + (limit, offset))
        return [self._deserialize_clean_run(dict(row)) for row in rows]

    def get_latest_clean_run(
        self,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        获取最近一次清洗运行（按 started_at 最大）

        用于增量清洗判断 / 实盘断点续跑。只返回已发生的最近运行，不含未来数据。
        """
        conditions: List[str] = []
        params: List[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if data_type:
            conditions.append("data_type = ?")
            params.append(data_type)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT * FROM {CleanRunMetaSchema.TABLE_NAME} {where} "
            f"ORDER BY started_at DESC, id DESC LIMIT 1"
        )
        row = self.conn.execute_one(sql, tuple(params))
        if not row:
            return None
        return self._deserialize_clean_run(dict(row))

    def count_clean_runs(
        self,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
    ) -> int:
        """统计清洗运行次数"""
        conditions: List[str] = []
        params: List[Any] = []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if data_type:
            conditions.append("data_type = ?")
            params.append(data_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if source:
            conditions.append("source = ?")
            params.append(source)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT COUNT(*) as cnt FROM {CleanRunMetaSchema.TABLE_NAME} {where}"
        row = self.conn.execute_one(sql, tuple(params))
        return row["cnt"] if row else 0

    def delete_clean_run(self, run_id: str) -> int:
        """
        删除一条清洗运行及其关联的脏数据统计（事务原子操作）

        Returns
        -------
        int : 删除的元信息行数（0 表示不存在）
        """
        with self.conn.transaction():
            # 先删子表（脏数据统计），再删主表
            self.conn.execute_update(
                f"DELETE FROM {DirtyDataStatsSchema.TABLE_NAME} WHERE run_id = ?",
                (run_id,),
            )
            affected = self.conn.execute_update(
                f"DELETE FROM {CleanRunMetaSchema.TABLE_NAME} WHERE run_id = ?",
                (run_id,),
            )
        return affected

    def _deserialize_clean_run(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """清洗运行行：steps_executed/step_durations 反序列化"""
        row["steps_executed"] = self._json_loads(row.get("steps_executed"), [])
        row["step_durations"] = self._json_loads(row.get("step_durations"), {})
        return row

    # ─────────────────────────────────────────────
    # 从流水线对象/事件写入（与 cleaner/event_bus 无缝对接）
    # ─────────────────────────────────────────────

    def insert_clean_run_from_pipeline_result(
        self,
        result: Any,
        *,
        source: str = CleanRunMetaSchema.SOURCE_OFFLINE,
        run_id: Optional[str] = None,
    ) -> str:
        """
        从 PipelineResult 对象插入清洗运行元信息（零拷贝对接）

        result 需具备字段：status/data_type/symbol/cleaned/bad/stats/
        step_durations/total_duration/bad_file/report_file/processed_file/error
        （即 myquant.data_access.cleaner.pipeline.PipelineResult 结构）
        """
        stats = getattr(result, "stats", None) or {}
        total = stats.get("total", 0)
        valid = stats.get("valid", len(getattr(result, "cleaned", []) or []))
        bad = stats.get("bad", len(getattr(result, "bad", []) or []))
        started_at = self._now_utc()
        finished_at = self._now_utc()
        return self.insert_clean_run(
            data_type=getattr(result, "data_type", "bar"),
            symbol=getattr(result, "symbol", "unknown"),
            status=getattr(result, "status", "completed"),
            total=total,
            valid=valid,
            bad=bad,
            quality_grade=stats.get("quality_grade"),
            severity=stats.get("severity"),
            steps_executed=list(getattr(result, "steps_executed", []) or []),
            step_durations=dict(getattr(result, "step_durations", {}) or {}),
            total_duration=getattr(result, "total_duration", None),
            bad_file=getattr(result, "bad_file", None),
            report_file=getattr(result, "report_file", None),
            processed_file=getattr(result, "processed_file", None),
            source=source,
            started_at=started_at,
            finished_at=finished_at,
            error=getattr(result, "error", None),
            run_id=run_id,
        )

    def insert_clean_run_from_pipeline_event(
        self,
        event: Any,
        *,
        source: str = CleanRunMetaSchema.SOURCE_LIVE,
        run_id: Optional[str] = None,
    ) -> str:
        """
        从 PipelineCompleteEvent 对象插入清洗运行元信息（实盘事件对接）

        event 需具备字段：symbol/data_type/status/cleaned_count/bad_count/
        total_count/quality_grade/duration/steps_executed/bad_file/report_file/error
        （即 myquant.core.event_bus.PipelineCompleteEvent 结构）
        """
        total = getattr(event, "total_count", 0)
        valid = getattr(event, "cleaned_count", 0)
        bad = getattr(event, "bad_count", 0)
        started_at = self._now_utc()
        finished_at = self._now_utc()
        return self.insert_clean_run(
            data_type=getattr(event, "data_type", "bar"),
            symbol=getattr(event, "symbol", "unknown"),
            status=getattr(event, "status", "completed"),
            total=total,
            valid=valid,
            bad=bad,
            valid_rate=round(valid / total, 6) if total > 0 else None,
            quality_grade=getattr(event, "quality_grade", None),
            severity=None,
            steps_executed=list(getattr(event, "steps_executed", []) or []),
            step_durations=None,
            total_duration=getattr(event, "duration", None),
            bad_file=getattr(event, "bad_file", None),
            report_file=getattr(event, "report_file", None),
            processed_file=None,
            source=source,
            started_at=started_at,
            finished_at=finished_at,
            error=getattr(event, "error", None),
            run_id=run_id,
        )

    # ─────────────────────────────────────────────
    # 脏数据统计 dirty_data_stats
    # ─────────────────────────────────────────────

    def insert_dirty_stats(
        self,
        run_id: str,
        symbol: str,
        data_type: str,
        dirty_type: str,
        bad_count: int = 1,
        severity: Optional[str] = None,
        repair_strategy: Optional[str] = None,
        sample_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        插入/覆盖一条脏数据统计（离线批量场景）

        同 (run_id, symbol, data_type, dirty_type) 冲突时覆盖为最新值。
        severity / repair_strategy 未传时自动从 DirtyDataType 推断。
        """
        severity = self._resolve_severity(dirty_type, severity)
        repair_strategy = self._resolve_repair_strategy(dirty_type, repair_strategy)
        columns = DirtyDataStatsSchema.WRITE_COLUMNS
        placeholders = ",".join(["?"] * len(columns))
        params = (
            run_id, symbol, data_type, dirty_type,
            severity, bad_count, repair_strategy,
            self._json_dumps(sample_data),
        )
        sql = (
            f"INSERT OR REPLACE INTO {DirtyDataStatsSchema.TABLE_NAME} "
            f"({','.join(columns)}) VALUES ({placeholders})"
        )
        with self.conn.transaction():
            self.conn.execute_update(sql, params)

    def accumulate_dirty_stats(
        self,
        run_id: str,
        symbol: str,
        data_type: str,
        dirty_type: str,
        bad_count: int = 1,
        severity: Optional[str] = None,
        repair_strategy: Optional[str] = None,
        sample_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        累加一条脏数据统计（实盘流式场景）

        同 (run_id, symbol, data_type, dirty_type) 已存在时 bad_count 累加，
        适用于实盘逐条 DataQualityEvent 实时更新。
        """
        severity = self._resolve_severity(dirty_type, severity)
        repair_strategy = self._resolve_repair_strategy(dirty_type, repair_strategy)
        sql = f"""
            INSERT INTO {DirtyDataStatsSchema.TABLE_NAME}
                (run_id, symbol, data_type, dirty_type, severity, bad_count, repair_strategy, sample_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, symbol, data_type, dirty_type)
            DO UPDATE SET
                bad_count = bad_count + excluded.bad_count,
                severity = excluded.severity,
                repair_strategy = excluded.repair_strategy
        """
        with self.conn.transaction():
            self.conn.execute_update(sql, (
                run_id, symbol, data_type, dirty_type,
                severity, bad_count, repair_strategy,
                self._json_dumps(sample_data),
            ))

    def bulk_insert_dirty_stats(self, entries: List[Dict[str, Any]]) -> int:
        """
        批量插入脏数据统计（executemany，性能最优）

        entries 每项需含：run_id/symbol/data_type/dirty_type/bad_count
        可选：severity/repair_strategy/sample_data（缺失自动推断）
        """
        if not entries:
            return 0
        columns = DirtyDataStatsSchema.WRITE_COLUMNS
        placeholders = ",".join(["?"] * len(columns))
        sql = (
            f"INSERT OR REPLACE INTO {DirtyDataStatsSchema.TABLE_NAME} "
            f"({','.join(columns)}) VALUES ({placeholders})"
        )
        params_list = []
        for item in entries:
            dirty_type = item.get("dirty_type", "unknown")
            params_list.append((
                item.get("run_id", ""),
                item.get("symbol", ""),
                item.get("data_type", "bar"),
                dirty_type,
                self._resolve_severity(dirty_type, item.get("severity")),
                item.get("bad_count", 0),
                self._resolve_repair_strategy(dirty_type, item.get("repair_strategy")),
                self._json_dumps(item.get("sample_data")),
            ))
        with self.conn.transaction():
            return self.conn.executemany(sql, params_list)

    def insert_dirty_stats_from_event(
        self,
        event: Any,
        *,
        run_id: Optional[str] = None,
        accumulate: bool = True,
    ) -> None:
        """
        从 DataQualityEvent 写入脏数据统计（实盘事件对接）

        event 需具备字段：dirty_type/symbol/data_type/severity/bad_count/
        repair_strategy/record（即 myquant.core.event_bus.DataQualityEvent 结构）
        accumulate=True 时逐条累加（实盘流式），False 时覆盖。
        """
        dirty_type = getattr(event, "dirty_type", "unknown") or "unknown"
        symbol = getattr(event, "symbol", "unknown")
        data_type = getattr(event, "data_type", "bar")
        severity = getattr(event, "severity", None) or None
        repair_strategy = getattr(event, "repair_strategy", None) or None
        bad_count = getattr(event, "bad_count", 1) or 1
        sample_data = getattr(event, "record", None)
        if run_id is None:
            run_id = f"live_{symbol}_{data_type}"
        if accumulate:
            self.accumulate_dirty_stats(
                run_id, symbol, data_type, dirty_type,
                bad_count=bad_count, severity=severity,
                repair_strategy=repair_strategy, sample_data=sample_data,
            )
        else:
            self.insert_dirty_stats(
                run_id, symbol, data_type, dirty_type,
                bad_count=bad_count, severity=severity,
                repair_strategy=repair_strategy, sample_data=sample_data,
            )

    def get_dirty_stats(
        self,
        run_id: Optional[str] = None,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
        dirty_type: Optional[str] = None,
        severity: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """条件查询脏数据统计（sample_data 自动反序列化）"""
        conditions: List[str] = []
        params: List[Any] = []
        if run_id:
            conditions.append("run_id = ?")
            params.append(run_id)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if data_type:
            conditions.append("data_type = ?")
            params.append(data_type)
        if dirty_type:
            conditions.append("dirty_type = ?")
            params.append(dirty_type)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM {DirtyDataStatsSchema.TABLE_NAME} {where} ORDER BY bad_count DESC, dirty_type ASC"
        rows = self.conn.execute(sql, tuple(params))
        result = []
        for row in rows:
            item = dict(row)
            item["sample_data"] = self._json_loads(item.get("sample_data"))
            result.append(item)
        return result

    def get_dirty_stats_summary(
        self,
        run_id: Optional[str] = None,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        脏数据统计汇总

        Returns
        -------
        dict : {
            "total_bad": int,          # 脏数据总条数
            "total_types": int,        # 脏数据类型数
            "error_count": int,        # error 级条数
            "warning_count": int,      # warning 级条数
            "by_type": {dirty_type: bad_count},   # 按类型分布
            "by_severity": {severity: bad_count}, # 按级别分布
            "top_dirty_types": [str],  # 高发类型TOP（按条数降序）
        }
        """
        conditions: List[str] = []
        params: List[Any] = []
        if run_id:
            conditions.append("run_id = ?")
            params.append(run_id)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if data_type:
            conditions.append("data_type = ?")
            params.append(data_type)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        summary: Dict[str, Any] = {
            "total_bad": 0,
            "total_types": 0,
            "error_count": 0,
            "warning_count": 0,
            "by_type": {},
            "by_severity": {},
            "top_dirty_types": [],
        }

        sql = f"SELECT dirty_type, severity, bad_count FROM {DirtyDataStatsSchema.TABLE_NAME} {where}"
        rows = self.conn.execute(sql, tuple(params))
        for row in rows:
            summary["total_bad"] += row["bad_count"]
            summary["by_type"][row["dirty_type"]] = summary["by_type"].get(row["dirty_type"], 0) + row["bad_count"]
            sev = row["severity"] or "unknown"
            summary["by_severity"][sev] = summary["by_severity"].get(sev, 0) + row["bad_count"]
            if sev == "error":
                summary["error_count"] += row["bad_count"]
            elif sev == "warning":
                summary["warning_count"] += row["bad_count"]

        summary["total_types"] = len(summary["by_type"])
        summary["top_dirty_types"] = [
            t for t, _ in sorted(summary["by_type"].items(), key=lambda kv: kv[1], reverse=True)
        ]
        return summary

    def get_top_dirty_types(
        self,
        run_id: Optional[str] = None,
        symbol: Optional[str] = None,
        data_type: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """获取脏数据类型排行（按 bad_count 降序）"""
        conditions: List[str] = []
        params: List[Any] = []
        if run_id:
            conditions.append("run_id = ?")
            params.append(run_id)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if data_type:
            conditions.append("data_type = ?")
            params.append(data_type)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT dirty_type, severity, SUM(bad_count) as total_bad, COUNT(*) as batch_count "
            f"FROM {DirtyDataStatsSchema.TABLE_NAME} {where} "
            f"GROUP BY dirty_type ORDER BY total_bad DESC LIMIT ?"
        )
        rows = self.conn.execute(sql, tuple(params) + (limit,))
        return [dict(row) for row in rows]

    def delete_dirty_stats(
        self,
        run_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> int:
        """删除脏数据统计（run_id 或 symbol 条件）"""
        conditions: List[str] = []
        params: List[Any] = []
        if run_id:
            conditions.append("run_id = ?")
            params.append(run_id)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if not conditions:
            return 0
        where = " AND ".join(conditions)
        sql = f"DELETE FROM {DirtyDataStatsSchema.TABLE_NAME} WHERE {where}"
        with self.conn.transaction():
            return self.conn.execute_update(sql, tuple(params))

    def count_dirty_stats(self, run_id: Optional[str] = None) -> int:
        """统计脏数据统计记录数"""
        if run_id:
            row = self.conn.execute_one(
                f"SELECT COUNT(*) as cnt FROM {DirtyDataStatsSchema.TABLE_NAME} WHERE run_id = ?",
                (run_id,),
            )
        else:
            row = self.conn.execute_one(
                f"SELECT COUNT(*) as cnt FROM {DirtyDataStatsSchema.TABLE_NAME}"
            )
        return row["cnt"] if row else 0
