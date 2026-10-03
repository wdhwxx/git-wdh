# 文件名：myquant/data_access/financial_service.py
"""
财务数据入库服务
================
将财务 CSV 原始数据导入 SQLite ``financial_data`` 表，并基于行情数据与财务数据
批量计算因子写入 ``factor_data`` 表，最后生成全中文表头的 HTML + CSV 入库报告。

业务职责（按治理规则归 myquant，scripts/ 仅做薄入口）：
  1. 读取财务 CSV → 字段映射 → 日期格式转换 → 分批批量入库
  2. 行情数据加载 → FactorEngine 向量化计算技术因子 → 长表展开入库
  3. 财务数据按报告期映射最新值 → 基本面因子入库
  4. 生成全中文 HTML + CSV 入库报告

【防未来函数】
  - 财务因子仅取「公告日 ann_date <= trade_date」的最新一期（F-15 修正：
    旧版误用报告期 report_date，会在财报实际披露前引用未来数据）
  - 技术因子全部基于历史窗口 rolling/ewm，第 t 行仅使用 ``iloc[:t+1]`` 数据
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from myquant.core.logger import get_logger
from myquant.database import get_connection
from myquant.database.queries import DataQueries

logger = get_logger(__name__)

# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

#: 每批批量入库行数（控制事务大小，避免 SQLite 事务过大）
BATCH_SIZE: int = 10000

#: 因子计算分批（每批加载的标的数，控制内存）
FACTOR_BATCH_SYMBOLS: int = 100

#: 技术因子配置（传给 FactorEngine）
#: 每项：{"engine": FactorEngine 配置, "cols": {输出列名: factor_data 表中的因子名}}
TECH_FACTOR_SPECS: List[Dict[str, Any]] = [
    {
        "engine": {"name": "sma", "params": {"period": 5}, "prefix": "sma5_"},
        "cols": {"sma": "SMA5"},
    },
    {
        "engine": {"name": "sma", "params": {"period": 10}, "prefix": "sma10_"},
        "cols": {"sma": "SMA10"},
    },
    {
        "engine": {"name": "sma", "params": {"period": 20}, "prefix": "sma20_"},
        "cols": {"sma": "SMA20"},
    },
    {
        "engine": {"name": "sma", "params": {"period": 60}, "prefix": "sma60_"},
        "cols": {"sma": "SMA60"},
    },
    {
        "engine": {"name": "ema", "params": {"period": 12}, "prefix": "ema12_"},
        "cols": {"ema": "EMA12"},
    },
    {
        "engine": {"name": "ema", "params": {"period": 26}, "prefix": "ema26_"},
        "cols": {"ema": "EMA26"},
    },
    {
        "engine": {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}, "prefix": "macd_"},
        "cols": {"dif": "MACD_DIF", "dea": "MACD_DEA", "macd_hist": "MACD_HIST"},
    },
    {
        "engine": {"name": "rsi", "params": {"period": 14}, "prefix": "rsi14_"},
        "cols": {"rsi": "RSI14"},
    },
    {
        "engine": {"name": "bollinger_bands", "params": {"period": 20, "std_dev": 2.0}, "prefix": "boll_"},
        "cols": {"upper": "BOLL_UP", "mid": "BOLL_MID", "lower": "BOLL_LOW"},
    },
    {
        "engine": {"name": "atr", "params": {"period": 14}, "prefix": "atr14_"},
        "cols": {"atr": "ATR14"},
    },
]

#: 基本面因子：财务字段 → factor_data 表中的因子名
FUNDAMENTAL_FACTOR_MAP: Dict[str, str] = {
    "pe": "市盈率PE",
    "pb": "市净率PB",
    "roe": "净资产收益率ROE",
    "gross_margin": "毛利率",
    "debt_ratio": "资产负债率",
}

#: factor_data 表写入列（与 FactorDataSchema.COLUMNS[1:] 对齐，排除自增 id）
FACTOR_WRITE_COLUMNS: Tuple[str, ...] = (
    "symbol", "market", "trade_date", "factor_name",
    "factor_value", "factor_rank", "calc_method", "data_source", "created_at",
)

#: financial_data 表写入列（与 FinancialDataSchema.COLUMNS[1:] 对齐，排除自增 id）
FIN_WRITE_COLUMNS: Tuple[str, ...] = (
    "symbol", "market", "report_date", "report_type", "ann_date",
    "pe", "pb", "ps", "total_mv", "circ_mv",
    "revenue", "net_profit", "net_profit_yoy", "revenue_yoy",
    "roe", "gross_margin", "debt_ratio", "eps", "bps",
    "data_source", "created_at",
)


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class ImportStats:
    """财务 CSV 导入统计结果。"""

    csv_file: str = ""
    total_rows: int = 0
    success_rows: int = 0
    skipped_rows: int = 0
    symbol_count: int = 0
    min_report_date: str = ""
    max_report_date: str = ""
    error_rows: int = 0


@dataclass
class FactorStats:
    """因子计算统计结果。"""

    symbol_count: int = 0
    tech_factor_count: int = 0
    fundamental_factor_count: int = 0
    total_factor_values: int = 0
    trade_days: int = 0
    latest_trade_date: str = ""
    failed_symbols: List[str] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════
# 主服务类
# ════════════════════════════════════════════════════════════════


class FinancialImportService:
    """
    财务数据入库服务。

    Args:
        project_root: 项目根目录（用于定位数据库与输出目录）。
        db_path: 数据库路径；None 时使用 ``project_root/database/quant.db``。
    """

    def __init__(self, project_root: Path, db_path: Optional[Path] = None) -> None:
        if not project_root or not project_root.exists():
            raise ValueError(f"项目根目录不存在: {project_root!r}")
        self.project_root = Path(project_root)
        self.db_path = db_path or (self.project_root / "database" / "quant.db")
        if not self.db_path.exists():
            raise FileNotFoundError(f"数据库文件不存在: {self.db_path}")

        self._conn = get_connection(self.db_path)
        self._queries = DataQueries(self._conn)

    # ─────────────────────────────────────────────
    # 第一阶段：财务 CSV 导入
    # ─────────────────────────────────────────────

    def import_financial_csv(
        self,
        csv_path: Path,
        progress_callback: Optional[Any] = None,
    ) -> ImportStats:
        """
        读取财务 CSV 并批量导入 ``financial_data`` 表。

        Args:
            csv_path: 财务 CSV 文件路径。
            progress_callback: 可选回调 ``(current, total, message)``。

        Returns:
            ImportStats 导入统计。

        Raises:
            FileNotFoundError: CSV 文件不存在。
            ValueError: CSV 为空或列结构不符预期。
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"财务 CSV 不存在: {csv_path}")

        stats = ImportStats(csv_file=csv_path.name)

        # 逐行流式读取（53.7MB 一次性 read 内存可接受，流式更稳妥）
        rows_buffer: List[Dict[str, Any]] = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        symbols: set = set()

        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            expected = {"symbol", "market", "report_date", "report_type", "net_profit_parent"}
            if not expected.issubset(set(reader.fieldnames or [])):
                raise ValueError(
                    f"CSV 列结构不符预期，缺少列: {expected - set(reader.fieldnames or [])}"
                )

            for raw in reader:
                stats.total_rows += 1
                record = self._map_csv_row(raw, now_str)
                if record is None:
                    stats.skipped_rows += 1
                    continue
                rows_buffer.append(record)
                symbols.add(record["symbol"])

                if len(rows_buffer) >= BATCH_SIZE:
                    self._flush_financial_batch(rows_buffer, stats)
                    if progress_callback:
                        progress_callback(stats.success_rows, stats.total_rows, "财务CSV导入中")
                    rows_buffer.clear()

            # 收尾
            if rows_buffer:
                self._flush_financial_batch(rows_buffer, stats)
                if progress_callback:
                    progress_callback(stats.success_rows, stats.total_rows, "财务CSV导入中")

        stats.symbol_count = len(symbols)
        # 日期范围
        dates = self._query_fin_date_range()
        stats.min_report_date, stats.max_report_date = dates
        stats.success_rows = stats.success_rows or self._count_fin_rows()

        logger.info(
            "财务CSV导入完成: 文件=%s 总行=%d 成功=%d 跳过=%d 标的=%d 区间=%s~%s",
            csv_path.name, stats.total_rows, stats.success_rows,
            stats.skipped_rows, stats.symbol_count,
            stats.min_report_date, stats.max_report_date,
        )
        return stats

    def _map_csv_row(self, raw: Dict[str, str], now_str: str) -> Optional[Dict[str, Any]]:
        """
        将 CSV 一行映射为 financial_data 表写入记录。

        映射规则（依交接摘要）：
          - ``net_profit_parent`` → ``net_profit``
          - CSV 无 ``ps/net_profit_yoy/revenue_yoy`` → 置 None
          - ``report_date`` 由 YYYYMMDD 转 YYYY-MM-DD
          - 空字符串 → None；数值字段转 float

        Returns:
            写入记录 dict；symbol 为空或 report_date 非法时返回 None。
        """
        symbol = (raw.get("symbol") or "").strip()
        if not symbol:
            return None

        report_date_raw = (raw.get("report_date") or "").strip()
        report_date = self._normalize_report_date(report_date_raw)
        if not report_date:
            return None

        # F-15：公告日（实际披露日）。CSV 若提供 ann_date 列则解析；
        # 否则置 None，时点查询时回退 report_date（向后兼容存量数据）。
        ann_date_raw = (raw.get("ann_date") or "").strip()
        ann_date = self._normalize_report_date(ann_date_raw) if ann_date_raw else None

        def _f(key: str) -> Optional[float]:
            val = (raw.get(key) or "").strip()
            if val == "":
                return None
            try:
                return float(val)
            except ValueError:
                return None

        return {
            "symbol": symbol,
            "market": (raw.get("market") or "A股").strip() or "A股",
            "report_date": report_date,
            "report_type": (raw.get("report_type") or "").strip(),
            "ann_date": ann_date,
            "pe": _f("pe"),
            "pb": _f("pb"),
            "ps": None,
            "total_mv": _f("total_mv"),
            "circ_mv": _f("circ_mv"),
            "revenue": _f("revenue"),
            "net_profit": _f("net_profit_parent"),
            "net_profit_yoy": None,
            "revenue_yoy": None,
            "roe": _f("roe"),
            "gross_margin": _f("gross_margin"),
            "debt_ratio": _f("debt_ratio"),
            "eps": _f("eps"),
            "bps": _f("bps"),
            "data_source": (raw.get("data_source") or "").strip() or "csv_import",
            "created_at": now_str,
        }

    @staticmethod
    def _normalize_report_date(raw: str) -> str:
        """将 YYYYMMDD 数字串规范化为 YYYY-MM-DD。"""
        raw = raw.strip()
        if not raw:
            return ""
        digits = raw.replace("-", "").replace("/", "")
        if len(digits) == 8 and digits.isdigit():
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        return raw[:10]

    def _flush_financial_batch(self, batch: List[Dict[str, Any]], stats: ImportStats) -> None:
        """将一批记录写入 financial_data 表（REPLACE 模式）。"""
        try:
            inserted = self._queries.bulk_insert_financial_data(batch, replace=True)
            stats.success_rows += inserted
        except sqlite3.Error as e:
            logger.error("财务数据批量写入失败: %s", e)
            stats.error_rows += len(batch)

    def _count_fin_rows(self) -> int:
        try:
            row = self._conn.execute_one("SELECT COUNT(*) AS c FROM financial_data")
            return int(row["c"]) if row else 0
        except sqlite3.Error:
            return 0

    def _query_fin_date_range(self) -> Tuple[str, str]:
        try:
            row = self._conn.execute_one(
                "SELECT MIN(report_date) AS mn, MAX(report_date) AS mx FROM financial_data"
            )
            if row:
                return row["mn"] or "", row["mx"] or ""
        except sqlite3.Error:
            pass
        return "", ""

    # ─────────────────────────────────────────────
    # 第二阶段：因子批量计算
    # ─────────────────────────────────────────────

    def compute_factors(
        self,
        lookback_days: int = 1,
        progress_callback: Optional[Any] = None,
    ) -> FactorStats:
        """
        批量计算技术因子与基本面因子并写入 factor_data 表。

        【防未来函数】
          - 技术因子：FactorEngine 内部 rolling/ewm 仅用历史窗口
          - 基本面因子：仅取 ``report_date <= trade_date`` 的最新一期

        Args:
            lookback_days: 写入最近 N 个交易日的截面（默认 1，避免长表爆炸）。
            progress_callback: 可选回调 ``(current, total, message)``。

        Returns:
            FactorStats 因子计算统计。
        """
        if lookback_days < 1:
            raise ValueError(f"lookback_days 必须 >= 1，got: {lookback_days}")

        stats = FactorStats()

        # 延迟导入（FactorEngine 依赖较重，按需加载）
        from myquant.indicators.factor_engine import FactorEngine

        engine = FactorEngine(
            cache_dir=str(self.project_root / "cache" / "factor"),
            use_cache=True,
            auto_fingerprint=True,
        )
        tech_configs = [spec["engine"] for spec in TECH_FACTOR_SPECS]
        stats.tech_factor_count = sum(
            len(spec["cols"]) for spec in TECH_FACTOR_SPECS
        )
        stats.fundamental_factor_count = len(FUNDAMENTAL_FACTOR_MAP)

        # 获取全部标的列表（从 market_data 去重）
        symbols = self._list_symbols()
        total = len(symbols)
        stats.symbol_count = total
        logger.info("因子计算开始: 标的=%d 回看天数=%d", total, lookback_days)

        # 分批处理技术因子
        for batch_start in range(0, total, FACTOR_BATCH_SYMBOLS):
            batch_symbols = symbols[batch_start:batch_start + FACTOR_BATCH_SYMBOLS]
            data_dict: Dict[str, pd.DataFrame] = {}
            for sym in batch_symbols:
                df = self._load_market_df(sym)
                if df is not None and len(df) >= 60:
                    data_dict[sym] = df

            if not data_dict:
                continue

            results = engine.calculate_batch(data_dict, tech_configs, raise_on_error=False)

            factor_rows: List[Dict[str, Any]] = []
            for sym, result_df in results.items():
                if result_df is None or result_df.empty:
                    stats.failed_symbols.append(sym)
                    continue
                market_df = data_dict[sym]
                tail = min(lookback_days, len(market_df))
                if tail <= 0:
                    continue
                # 对齐索引：取最近 N 个交易日
                tail_dates = list(market_df.index[-tail:])
                tail_result = result_df.tail(tail)
                for i, trade_date in enumerate(tail_dates):
                    if i >= len(tail_result):
                        break
                    row_vals = tail_result.iloc[i]
                    for spec in TECH_FACTOR_SPECS:
                        for out_col, factor_name in spec["cols"].items():
                            col_key = f"{spec['engine'].get('prefix', '')}{out_col}"
                            if col_key not in row_vals.index:
                                continue
                            val = row_vals[col_key]
                            if pd.isna(val):
                                continue
                            factor_rows.append(self._make_factor_row(
                                sym, str(trade_date), factor_name,
                                float(val), "技术因子(FactorEngine)",
                            ))

            # 基本面因子：从 financial_data 映射最新一期
            fund_rows = self._build_fundamental_rows(batch_symbols, lookback_days)
            factor_rows.extend(fund_rows)

            if factor_rows:
                self._flush_factor_batch(factor_rows)
                stats.total_factor_values += len(factor_rows)

            if progress_callback:
                done = min(batch_start + FACTOR_BATCH_SYMBOLS, total)
                progress_callback(done, total, "因子计算中")

            logger.debug("因子计算批次: %d/%d", min(batch_start + FACTOR_BATCH_SYMBOLS, total), total)

        stats.latest_trade_date = self._latest_market_date()
        # 统计实际写入交易日数
        stats.trade_days = self._count_factor_trade_days()

        logger.info(
            "因子计算完成: 标的=%d 因子值=%d 最新交易日=%s 失败=%d",
            stats.symbol_count, stats.total_factor_values,
            stats.latest_trade_date, len(stats.failed_symbols),
        )
        return stats

    def _list_symbols(self) -> List[str]:
        """从 market_data 表获取全部去重标的。"""
        try:
            rows = self._conn.execute("SELECT DISTINCT symbol FROM market_data ORDER BY symbol")
            return [r["symbol"] for r in rows]
        except sqlite3.Error as e:
            logger.error("查询标的列表失败: %s", e)
            return []

    def _load_market_df(self, symbol: str) -> Optional[pd.DataFrame]:
        """加载单只标的行情为 DataFrame（按 trade_date 排序，日期索引）。"""
        try:
            rows = self._queries.get_market_data(symbol=symbol, period="daily", adjust="raw")
        except sqlite3.Error as e:
            logger.debug("行情查询失败 [%s]: %s", symbol, e)
            return None
        if not rows:
            return None
        df = pd.DataFrame(rows)
        # #38中文列改造：数据库 market_data 为英文表头，此处统一转中文，
        # 与 FactorEngine 及其内部指标（要求中文列）及全链路中文口径一致
        _rename = {
            "open": "开盘价", "high": "最高价", "low": "最低价",
            "close": "收盘价", "volume": "成交量",
            "amount": "成交额", "pct_chg": "涨跌幅", "pre_close": "前收盘价",
        }
        df = df.rename(columns={en: cn for en, cn in _rename.items() if en in df.columns})
        # 确保数值列
        for col in ("开盘价", "最高价", "最低价", "收盘价", "成交量"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("trade_date").set_index("trade_date")
        # 去掉全空行
        df = df.dropna(subset=["开盘价", "最高价", "最低价", "收盘价"], how="all")
        return df

    def _build_fundamental_rows(
        self, symbols: List[str], lookback_days: int,
    ) -> List[Dict[str, Any]]:
        """
        为批次标的构建基本面因子行。

        【防未来函数 F-15 修正】每只标的对每个信号交易日 td，仅选取
        「公告日 <= td」的最新一期财报。公告日取 ``ann_date``（实际披露日），
        若该字段为 NULL（存量数据）则回退 ``report_date``。

        时序语义：本行使用的数据截至 t 时刻——仅引用在 t 日开盘前已公开披露
        的财报，杜绝用报告期（report_date，如 6/30）提前于披露日（如 8/28）
        引用未来财报的未来函数。
        """
        rows: List[Dict[str, Any]] = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for sym in symbols:
            try:
                fin = self._queries.get_financial_data(symbol=sym)
            except sqlite3.Error:
                continue
            if not fin:
                continue
            # 有效披露日 = ann_date 优先，缺失回退 report_date
            def _eff_date(r: Dict[str, Any]) -> str:
                return str(r.get("ann_date") or r.get("report_date") or "")

            # 按有效披露日降序排序（覆盖原 report_date 排序假设）
            fin_sorted = sorted(fin, key=_eff_date, reverse=True)
            # 取该标的最近行情交易日
            trade_dates = self._latest_trade_dates(sym, lookback_days)
            if not trade_dates:
                continue
            for td in trade_dates:
                # 在 td 时点可见的最新一期：有效披露日 <= td
                visible = next(
                    (r for r in fin_sorted if _eff_date(r) and _eff_date(r) <= str(td)),
                    None,
                )
                if visible is None:
                    continue
                for field_key, factor_name in FUNDAMENTAL_FACTOR_MAP.items():
                    val = visible.get(field_key)
                    if val is None:
                        continue
                    rows.append({
                        "symbol": sym,
                        "market": visible.get("market", "A股"),
                        "trade_date": td,
                        "factor_name": factor_name,
                        "factor_value": float(val),
                        "factor_rank": None,
                        "calc_method": "基本面因子(公告日时点映射)",
                        "data_source": visible.get("data_source", "financial_import"),
                        "created_at": now_str,
                    })
        return rows

    def _latest_trade_dates(self, symbol: str, n: int) -> List[str]:
        """取标的最近 n 个交易日（从 market_data）。"""
        try:
            rows = self._conn.execute(
                "SELECT trade_date FROM market_data WHERE symbol=? AND period='daily' "
                "ORDER BY trade_date DESC LIMIT ?",
                (symbol, n),
            )
            return [r["trade_date"] for r in rows]
        except sqlite3.Error:
            return []

    def _make_factor_row(
        self, symbol: str, trade_date: str, factor_name: str,
        value: float, calc_method: str,
    ) -> Dict[str, Any]:
        """构造 factor_data 表写入行。"""
        return {
            "symbol": symbol,
            "market": "A股",
            "trade_date": trade_date,
            "factor_name": factor_name,
            "factor_value": float(value),
            "factor_rank": None,
            "calc_method": calc_method,
            "data_source": "factor_engine",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _flush_factor_batch(self, batch: List[Dict[str, Any]]) -> None:
        """将一批因子行写入 factor_data 表（REPLACE 模式）。"""
        try:
            self._queries.bulk_insert_factor_data(batch, replace=True)
        except sqlite3.Error as e:
            logger.error("因子数据批量写入失败: %s", e)

    def _latest_market_date(self) -> str:
        try:
            row = self._conn.execute_one("SELECT MAX(trade_date) AS d FROM market_data")
            return row["d"] if row and row["d"] else ""
        except sqlite3.Error:
            return ""

    def _count_factor_trade_days(self) -> int:
        try:
            row = self._conn.execute_one("SELECT COUNT(DISTINCT trade_date) AS c FROM factor_data")
            return int(row["c"]) if row else 0
        except sqlite3.Error:
            return 0

    # ─────────────────────────────────────────────
    # 第三阶段：生成全中文 HTML + CSV 报告
    # ─────────────────────────────────────────────

    def generate_report(
        self,
        import_stats: ImportStats,
        factor_stats: FactorStats,
        output_dir: Optional[Path] = None,
    ) -> Tuple[Path, Path]:
        """
        生成全中文表头的 HTML + CSV 入库报告。

        Args:
            import_stats: 财务导入统计。
            factor_stats: 因子计算统计。
            output_dir: 输出目录；默认 ``outputs/财务数据入库报告/``。

        Returns:
            (csv_path, html_path) 两个报告文件路径。
        """
        if output_dir is None:
            output_dir = self.project_root / "outputs" / "财务数据入库报告"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = output_dir / f"财务入库报告_{ts}.csv"
        html_path = output_dir / f"财务入库报告_{ts}.html"

        self._write_report_csv(csv_path, import_stats, factor_stats)
        self._write_report_html(html_path, import_stats, factor_stats)

        logger.info("入库报告已生成: CSV=%s HTML=%s", csv_path, html_path)
        return csv_path, html_path

    def _write_report_csv(
        self, path: Path, imp: ImportStats, fac: FactorStats,
    ) -> None:
        """写 CSV 报告（全中文表头：指标, 数值）。"""
        rows = [
            ("任务名称", "财务数据入库与因子计算"),
            ("执行时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("—— 财务数据导入 ——", ""),
            ("财务CSV文件", imp.csv_file),
            ("财务原始总行数", imp.total_rows),
            ("成功导入行数", imp.success_rows),
            ("跳过行数", imp.skipped_rows),
            ("错误行数", imp.error_rows),
            ("覆盖标的数", imp.symbol_count),
            ("最早报告期", imp.min_report_date),
            ("最新报告期", imp.max_report_date),
            ("—— 因子计算 ——", ""),
            ("参与计算标的数", fac.symbol_count),
            ("技术因子数量", fac.tech_factor_count),
            ("基本面因子数量", fac.fundamental_factor_count),
            ("因子值总行数", fac.total_factor_values),
            ("覆盖交易日数", fac.trade_days),
            ("最新行情交易日", fac.latest_trade_date),
            ("失败标的数", len(fac.failed_symbols)),
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["指标", "数值"])
            writer.writerows(rows)

    def _write_report_html(
        self, path: Path, imp: ImportStats, fac: FactorStats,
    ) -> None:
        """写 HTML 报告（深色主题，全中文）。"""
        tech_names: List[str] = []
        for spec in TECH_FACTOR_SPECS:
            tech_names.extend(spec["cols"].values())
        fund_names = list(FUNDAMENTAL_FACTOR_MAP.values())

        def card(title: str, value: str, color: str = "#2fd4a7") -> str:
            return (
                f'<div class="card"><div class="card-val" style="color:{color}">{value}</div>'
                f'<div class="card-title">{title}</div></div>'
            )

        tech_table = "".join(
            f"<tr><td>{i + 1}</td><td>{n}</td><td>技术因子</td></tr>"
            for i, n in enumerate(tech_names)
        )
        fund_table = "".join(
            f"<tr><td>{len(tech_names) + i + 1}</td><td>{n}</td><td>基本面因子</td></tr>"
            for i, n in enumerate(fund_names)
        )

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>财务数据入库报告</title>
<style>
  body {{ background:#0f1420; color:#cdd6e6; font-family:"Microsoft YaHei",sans-serif; margin:0; padding:24px; }}
  h1 {{ color:#2fd4a7; font-size:22px; }}
  h2 {{ color:#818cf8; font-size:16px; margin-top:28px; border-left:3px solid #2fd4a7; padding-left:10px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin-top:16px; }}
  .card {{ background:#1a2130; border:1px solid #232a3a; border-radius:8px; padding:16px; }}
  .card-val {{ font-size:22px; font-weight:700; font-family:Consolas; }}
  .card-title {{ font-size:12px; color:#7b8498; margin-top:6px; }}
  table {{ width:100%; border-collapse:collapse; margin-top:12px; font-size:13px; }}
  th,td {{ padding:8px 10px; border:1px solid #232a3a; text-align:left; }}
  th {{ background:#1a2130; color:#2fd4a7; }}
  .meta {{ color:#7b8498; font-size:12px; margin-top:8px; }}
</style>
</head>
<body>
  <h1>财务数据入库报告</h1>
  <div class="meta">生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}　|　数据库：database/quant.db</div>

  <h2>财务数据导入概览</h2>
  <div class="cards">
    {card("财务CSV文件", imp.csv_file, "#e8edf7")}
    {card("原始总行数", imp.total_rows)}
    {card("成功导入行数", imp.success_rows)}
    {card("跳过行数", imp.skipped_rows, "#ffb84d")}
    {card("覆盖标的数", imp.symbol_count)}
    {card("报告期区间", f"{imp.min_report_date} ~ {imp.max_report_date}", "#e8edf7")}
  </div>

  <h2>因子计算概览</h2>
  <div class="cards">
    {card("参与标的数", fac.symbol_count)}
    {card("技术因子数", fac.tech_factor_count)}
    {card("基本面因子数", fac.fundamental_factor_count)}
    {card("因子值总行数", fac.total_factor_values)}
    {card("覆盖交易日", fac.trade_days)}
    {card("最新交易日", fac.latest_trade_date, "#e8edf7")}
  </div>

  <h2>因子清单</h2>
  <table>
    <tr><th>序号</th><th>因子名称</th><th>类别</th></tr>
    {tech_table}
    {fund_table}
  </table>
</body>
</html>"""
        path.write_text(html, encoding="utf-8")

    # ─────────────────────────────────────────────
    # 收尾
    # ─────────────────────────────────────────────

    def close(self) -> None:
        """关闭数据库连接。"""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    # 自测 Demo：仅打印服务可用性（实际导入需指定 CSV）
    import sys
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[2]
    svc = FinancialImportService(root)
    print("FinancialImportService 初始化成功，数据库:", svc.db_path)
    syms = svc._list_symbols()
    print("market_data 标的数:", len(syms))
    svc.close()
    sys.exit(0)
