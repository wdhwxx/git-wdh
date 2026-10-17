# 文件名：myquant/data_access/financial_sync_service.py
"""
财务 CSV 增量同步服务
======================
为"财务数据自动补齐"提供业务能力：定位 financial_data 缺失的标的（如新上市
未进入历史 CSV 的 601091.SH），复用 akshare 网络源拉取多期财务历史，与本地
最新财务 CSV 按 ``(symbol, report_date)`` 合并去重后落盘，并可选衔接
``FinancialImportService`` 自动入库，形成"拉取 → 合并 → 入库"闭环。

业务职责（按治理规则归 myquant，scripts/ 仅做薄入口）：
  1. ``find_missing_financial_symbols`` —— 查询 quant.db，找出 market_data 有行情
     但 financial_data 无记录的 A 股标的（与全量清洗 #71 no_financial 留痕同口径，
     排除规则复用 full_clean_service.filter_excluded_symbols）
  2. ``fetch_financial_history`` —— 逐标的调用数据源 ``get_financial_history``
     拉取多期财务（防未来函数：end_date 之后的报告期不获取）
  3. ``merge_financial_csv`` —— 新拉记录与本地最新 CSV 合并（symbol+report_date
     去重 keep=新），落盘 ``financial_history_YYYYMMDD.csv``（与 GUI 下载同命名）
  4. ``run_sync`` —— 编排主流程（定位→拉取→合并→可选自动入库）

【防未来函数】
  - 拉取阶段将 end_date 原样传给数据源，报告期 > end_date 的数据不返回
  - 合并去重以报告期为主键，不做任何向前引用
"""

from __future__ import annotations

import csv
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

from myquant.core.logger import get_logger
from myquant.data_access.factory import create_default_fetcher
from myquant.data_access.full_clean_service import filter_excluded_symbols

logger = get_logger(__name__)

# ════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════

#: 财务原始数据目录（相对 project_root）
FINANCIAL_RAW_DIR: str = "datasets/raw/financial"

#: 财务历史 CSV 文件名前缀（与 GUI data_download_dialog._save_financial_csv 一致）
FINANCIAL_HISTORY_PREFIX: str = "financial_history"

#: 每次拉取间隔秒数（限速，与 GUI run_finance 一致）
FETCH_SLEEP_SEC: float = 0.3

#: CSV 列顺序（与既有 financial_history_*.csv 完全一致；
#: A-04 起含 ann_date 公告日列，位置与 financial_service 入库映射同序 report_type 之后）
FINANCIAL_CSV_COLUMNS: List[str] = [
    "symbol", "market", "report_date", "report_type", "ann_date", "pe", "pb",
    "total_mv", "circ_mv", "eps", "bps", "revenue", "net_profit",
    "net_profit_parent", "roe", "gross_margin", "debt_ratio", "data_source",
]

#: 合并去重主键列
DEDUP_KEYS: Tuple[str, str] = ("symbol", "report_date")

#: 增量定位默认排除规则（P2-③ #83 config 化后为内置回退）——
#: 对齐全量清洗实际口径（config/data_clean_run.json 14 条，
#: 含科创板688/北交所4·8·92·68/创业板30·90/B股20·99等；full_clean_service 的
#: DEFAULT_EXCLUDE_RULES 同为本 14 条，#87 已补齐，两处内置兜底一致）。
#: 优先读取 config/financial_exclude_rules.yaml（见 _load_financial_exclude_rules）。
FINANCIAL_EXCLUDE_RULES: List[str] = [
    "000.SH", "399", "880", "5.SH", "1.SZ", "30.SZ", "688.SH",
    "4", "8", "92", "68", "90", "20", "99",
]


def _load_financial_exclude_rules(project_root: Path) -> List[str]:
    """读取 config/financial_exclude_rules.yaml（P2-③ #83）。

    缺文件/格式异常 → 回退内置 14 条 + warning（不阻断增量定位）。
    """
    cfg_path = project_root / "config" / "financial_exclude_rules.yaml"
    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            rules = data.get("exclude_rules")
            if isinstance(rules, list) and rules:
                return [str(r) for r in rules]
        except Exception as exc:
            logger.warning(
                "财务排除规则配置读取失败，回退内置 14 条: %s (%s)", cfg_path, exc
            )
    else:
        logger.warning("财务排除规则配置缺失，使用内置 14 条: %s", cfg_path)
    return list(FINANCIAL_EXCLUDE_RULES)


# ════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════

@dataclass
class SyncStats:
    """财务 CSV 同步统计结果。"""

    #: 本次同步的标的列表
    symbols: List[str] = field(default_factory=list)
    #: 成功拉取的标的数
    fetched_symbols: int = 0
    #: 拉取失败/无数据的标的数
    skipped_symbols: int = 0
    #: 新增财务记录条数
    new_records: int = 0
    #: 合并后 CSV 路径；无新记录时为 None
    csv_path: Optional[str] = None
    #: 自动入库统计（未入库为 None）
    import_stats: Optional[Dict[str, Any]] = None
    #: 同步后仍缺财务的标的（入库后复查）
    missing_after: List[str] = field(default_factory=list)


# ════════════════════════════════════════════════════════════
# 业务实现
# ════════════════════════════════════════════════════════════

def _find_missing_financial_symbols(
    project_root: Path,
    exclude_rules: Optional[List[str]] = None,
    db_path: Optional[Path] = None,
) -> List[str]:
    """定位 market_data 有行情、financial_data 无记录的 A 股标的。

    口径与全量清洗 #71 no_financial 留痕一致：只考虑清洗保留范围内的标的
    （排除指数/基金/科创板/北交所/B股等），避免对无财务的正常标的反复拉取。

    Args:
        project_root: 项目根目录。
        exclude_rules: 排除规则；None 使用与全量清洗一致的口径（14 条）。
        db_path: 数据库路径；None 使用 ``database/quant.db``。

    Returns:
        List[str]: 待补财务的标的列表（升序）。
    """
    rules = list(exclude_rules) if exclude_rules is not None else _load_financial_exclude_rules(project_root)
    db = db_path or (project_root / "database" / "quant.db")
    sql = (
        "SELECT DISTINCT m.symbol FROM market_data m "
        "WHERE m.market = 'A股' "
        "AND m.symbol NOT IN (SELECT DISTINCT symbol FROM financial_data) "
        "ORDER BY m.symbol"
    )
    try:
        conn = sqlite3.connect(str(db))
        try:
            all_missing = [row[0] for row in conn.execute(sql)]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.error("待补财务标的查询失败: %s (%s)", db, exc)
        return []
    keep, _excluded = filter_excluded_symbols(all_missing, rules)
    logger.info(
        "待补财务标的: 原始 %d 只, 排除规则过滤后 %d 只（样例 %s）",
        len(all_missing), len(keep), keep[:5],
    )
    return keep


def _fetch_financial_history(
    fetcher: Any,
    symbols: Sequence[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    sleep_sec: float = FETCH_SLEEP_SEC,
    progress_callback: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """逐标的拉取多期财务历史。

    只调用数据源公开的 ``get_financial_history`` 接口；end_date 之后的报告期
    由数据源过滤（防未来函数，本行使用的数据截至 end_date 时刻）。

    Args:
        fetcher: 数据获取器（须提供 get_financial_history）。
        symbols: 待拉取标的列表。
        start_date: 起始报告期（YYYYMMDD 或 YYYY-MM-DD），None=不限。
        end_date: 截止报告期，None=截止当前日期。
        sleep_sec: 每次请求间隔秒数。
        progress_callback: 可选回调 ``(current, total, message_dict)``。

    Returns:
        Tuple[List[Dict], List[str]]: (成功记录列表, 失败/无数据的标的列表)。
    """
    records: List[Dict[str, Any]] = []
    failed: List[str] = []
    total = len(symbols)
    for i, symbol in enumerate(symbols, 1):
        try:
            data = fetcher.get_financial_history(symbol, start_date, end_date)
        except Exception as exc:  # noqa: BLE001 —— 单标的失败不阻断整体
            logger.warning("财务历史拉取失败 [%s]: %s", symbol, exc)
            failed.append(symbol)
            if progress_callback:
                progress_callback(i, total, {"symbol": symbol, "status": "failed", "count": 0})
            continue
        if data:
            records.extend(data)
            if progress_callback:
                progress_callback(i, total, {"symbol": symbol, "status": "completed", "count": len(data)})
        else:
            failed.append(symbol)
            if progress_callback:
                progress_callback(i, total, {"symbol": symbol, "status": "skipped", "count": 0})
        if i < total:
            time.sleep(sleep_sec)
    return records, failed


def _merge_and_save_csv(
    project_root: Path,
    new_records: Sequence[Dict[str, Any]],
) -> Optional[Path]:
    """新拉记录与本地最新财务 CSV 合并后落盘。

    读取 ``datasets/raw/financial/`` 下最新的 ``financial_history_*.csv``，
    按 ``(symbol, report_date)`` 去重 keep=新记录，输出
    ``financial_history_YYYYMMDD.csv``（UTF-8 BOM，列顺序与既有 CSV 一致）。

    Args:
        project_root: 项目根目录。
        new_records: 本次拉取的新财务记录（dict 列表）。

    Returns:
        Optional[Path]: 合并后 CSV 路径；new_records 为空时返回 None。
    """
    if not new_records:
        logger.info("无新财务记录，跳过合并落盘")
        return None

    fin_dir = project_root / FINANCIAL_RAW_DIR
    fin_dir.mkdir(parents=True, exist_ok=True)

    # 读取旧 CSV（取最新一个）
    old_rows: List[Dict[str, Any]] = []
    old_candidates = sorted(fin_dir.glob(f"{FINANCIAL_HISTORY_PREFIX}_*.csv"))
    if old_candidates:
        old_path = old_candidates[-1]
        try:
            with open(old_path, encoding="utf-8-sig", newline="") as f:
                old_rows = list(csv.DictReader(f))
            logger.info("合并基准 CSV: %s（%d 行）", old_path.name, len(old_rows))
        except (OSError, csv.Error) as exc:
            # P2-11：旧CSV读取失败时绝不截断历史（old_rows留空会把raw覆盖为仅本次标的），
            # 改为中止本次落盘，保留旧文件，待人工/重试恢复。
            logger.error(
                "旧财务CSV读取失败，中止本次落盘以避免截断历史: %s (%s)", old_path, exc,
            )
            return None

    # 新记录优先：symbol+report_date 去重 keep=新
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in old_rows:
        key = (str(row.get("symbol", "")), str(row.get("report_date", "")))
        merged[key] = row
    for rec in new_records:
        key = (str(rec.get("symbol", "")), str(rec.get("report_date", "")))
        merged[key] = dict(rec)

    output_path = fin_dir / (
        f"{FINANCIAL_HISTORY_PREFIX}_{datetime.now().strftime('%Y%m%d')}.csv"
    )
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FINANCIAL_CSV_COLUMNS)
        writer.writeheader()
        for _key, row in sorted(merged.items()):
            writer.writerow({c: row.get(c, "") for c in FINANCIAL_CSV_COLUMNS})

    logger.info("合并落盘: %s（%d 行，新增 %d 条）", output_path.name, len(merged), len(new_records))
    return output_path


def _count_financial_symbols(project_root: Path, db_path: Optional[Path] = None) -> int:
    """统计 financial_data 中已有记录的标的数（入库后复查用）。"""
    db = db_path or (project_root / "database" / "quant.db")
    try:
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute("SELECT COUNT(DISTINCT symbol) FROM financial_data").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.error("financial_data 标的数统计失败: %s", exc)
        return 0


# ════════════════════════════════════════════════════════════
# 服务类
# ════════════════════════════════════════════════════════════

class FinancialSyncService:
    """财务 CSV 增量同步服务（缺口A自动机制业务核心）。"""

    def __init__(self, project_root: Path) -> None:
        """初始化服务。

        Args:
            project_root: 项目根目录（相对路径均基于此解析）。
        """
        self.project_root = Path(project_root)

    def run_sync(
        self,
        symbols: Optional[Sequence[str]] = None,
        all_market: bool = False,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        auto_import: bool = True,
        exclude_rules: Optional[List[str]] = None,
        fetcher: Any = None,
        db_path: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
    ) -> SyncStats:
        """执行财务 CSV 同步主流程：定位 → 拉取 → 合并 →（可选）自动入库。

        标的确定规则（优先级从高到低）：
          1. ``all_market=True``：全市场（fetcher.get_stock_list()）
          2. 显式传入 ``symbols``：仅这些标的
          3. 缺省：自动定位 financial_data 缺失的清洗保留标的（增量优先）

        Args:
            symbols: 显式指定待拉取标的；None 且非全市场时自动定位缺失标的。
            all_market: True 时拉取全市场财务（与 GUI 行为一致）。
            start_date: 起始报告期；None 不限。
            end_date: 截止报告期；None 截止今天（YYYY-MM-DD）。
            auto_import: True 时合并后自动调用 FinancialImportService 入库。
            exclude_rules: 自动定位缺失标的时的排除规则；None 用默认。
            fetcher: 数据获取器；None 用 create_default_fetcher()。
            db_path: 数据库路径；None 用 ``database/quant.db``。
            progress_callback: 可选回调 ``(current, total, message_dict)``。

        Returns:
            SyncStats: 同步统计结果。
        """
        f = fetcher or create_default_fetcher()
        effective_end = end_date or date.today().strftime("%Y-%m-%d")

        # 1. 确定标的
        target_symbols: List[str] = []
        if all_market:
            stocks = f.get_stock_list()
            target_symbols = [s["symbol"] for s in stocks if s.get("symbol")]
            logger.info("全市场模式: 待拉取 %d 只", len(target_symbols))
        elif symbols:
            target_symbols = [str(s).strip() for s in symbols if str(s).strip()]
            logger.info("指定标的模式: 待拉取 %d 只", len(target_symbols))
        else:
            target_symbols = _find_missing_financial_symbols(
                self.project_root, exclude_rules=exclude_rules, db_path=db_path
            )
        if not target_symbols:
            logger.info("无可拉取标的，本次同步跳过")
            return SyncStats()

        # 2. 拉取
        records, failed = _fetch_financial_history(
            f, target_symbols, start_date, effective_end, progress_callback=progress_callback
        )

        # 3. 合并落盘
        csv_path = _merge_and_save_csv(self.project_root, records)

        # 4. 自动入库
        import_stats: Optional[Dict[str, Any]] = None
        if auto_import and csv_path is not None:
            from myquant.data_access.financial_service import FinancialImportService

            svc = FinancialImportService(self.project_root)
            try:
                stats = svc.import_financial_csv(csv_path)
                import_stats = {
                    "csv_file": stats.csv_file,
                    "total_rows": stats.total_rows,
                    "success_rows": stats.success_rows,
                    "skipped_rows": stats.skipped_rows,
                    "symbol_count": stats.symbol_count,
                    "min_report_date": stats.min_report_date,
                    "max_report_date": stats.max_report_date,
                }
                logger.info(
                    "自动入库完成: 总行=%d 成功=%d 跳过=%d 标的=%d",
                    stats.total_rows, stats.success_rows, stats.skipped_rows, stats.symbol_count,
                )
            except Exception as exc:  # noqa: BLE001 —— 入库失败记录但不掩盖同步结果
                logger.error("自动入库失败: %s", exc)
                import_stats = {"error": str(exc)}

        # 5. 复查缺失
        before_count = _count_financial_symbols(self.project_root, db_path=db_path)
        missing_after: List[str] = []
        if records:
            missing_after = [
                s for s in target_symbols
                if s in failed or s not in {r.get("symbol") for r in records}
            ]
        else:
            missing_after = list(target_symbols)
        after_count = _count_financial_symbols(self.project_root, db_path=db_path)

        stats = SyncStats(
            symbols=target_symbols,
            fetched_symbols=len(target_symbols) - len(failed),
            skipped_symbols=len(failed),
            new_records=len(records),
            csv_path=str(csv_path) if csv_path else None,
            import_stats=import_stats,
            missing_after=missing_after,
        )
        logger.info(
            "同步完成: 标的=%d 拉取成功=%d 失败=%d 新记录=%d 财务标的 %d→%d",
            len(target_symbols), stats.fetched_symbols, stats.skipped_symbols,
            len(records), before_count, after_count,
        )
        return stats


def backfill_ann_date_column(fin_dir: Path, dry_run: bool = True) -> Dict[str, Any]:
    """A-04：为存量 ``financial_history_*.csv`` 补 ``ann_date`` 列（默认 dry-run）。

    B-09 之前的存量 CSV 无 ann_date 列；本函数按 FINANCIAL_CSV_COLUMNS 校验表头，
    缺列则插入空列回写（UTF-8 BOM，保持其余列值/行序不变）。已有 ann_date 列的文件
    跳过（不重写）。公告日真实值由下一次东财拉取经 B-09 链路补入新行，存量行的
    ann_date 空串在入库时被 financial_service 视为无公告日（时点回退 report_date，
    向后兼容）。

    Parameters
    ----------
    fin_dir : Path
        财务 CSV 目录（默认 datasets/raw/financial/）。
    dry_run : bool
        True（默认）只扫描统计不写盘；False 实际回写。

    Returns
    -------
    dict : {files_scanned, files_backfilled, rows_updated}
    """
    stats: Dict[str, Any] = {"files_scanned": 0, "files_backfilled": 0, "rows_updated": 0}
    if not fin_dir.exists():
        logger.warning("财务 CSV 目录不存在: %s", fin_dir)
        return stats

    for csv_path in sorted(fin_dir.glob(f"{FINANCIAL_HISTORY_PREFIX}_*.csv")):
        stats["files_scanned"] += 1
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = list(reader.fieldnames or [])
            rows = [dict(r) for r in reader]
        if "ann_date" in fields:
            logger.info("跳过（已含 ann_date 列）: %s", csv_path.name)
            continue

        # 插入位置：report_type 之后（与 FINANCIAL_CSV_COLUMNS 同序）
        pos = fields.index("report_type") + 1 if "report_type" in fields else len(fields)
        new_fields = fields[:pos] + ["ann_date"] + fields[pos:]
        if new_fields != FINANCIAL_CSV_COLUMNS:
            logger.warning(
                "%s 表头顺序与 FINANCIAL_CSV_COLUMNS 不一致（现 %d 列→将 %d 列），"
                "按现有列序插入 ann_date",
                csv_path.name, len(fields), len(new_fields),
            )

        if dry_run:
            logger.info("[dry-run] 待回填: %s（%d 行，%d 列→%d 列）",
                        csv_path.name, len(rows), len(fields), len(new_fields))
            stats["files_backfilled"] += 1
            stats["rows_updated"] += len(rows)
            continue

        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=new_fields)
            writer.writeheader()
            for r in rows:
                r["ann_date"] = ""
                writer.writerow({c: r.get(c, "") for c in new_fields})
        stats["files_backfilled"] += 1
        stats["rows_updated"] += len(rows)
        logger.info("已回填: %s（%d 行）", csv_path.name, len(rows))
    return stats
