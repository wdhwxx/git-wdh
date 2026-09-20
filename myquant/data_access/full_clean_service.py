# -*- coding: utf-8 -*-
# 文件名：myquant/data_access/full_clean_service.py
"""
全量数据清洗服务（业务逻辑层）
================================
对照 docs/project_layout_gear2.md 架构规范：
  - scripts/ 只做入口，业务逻辑全部在 myquant/ 内（本模块）
  - 用 myquant.database.queries.DataQueries / CleanDataQueries 类型安全 CRUD，不写裸 SQL
  - 每只标的一个 run_id（CleanDataQueries 自动生成 clean_bar_{symbol}_{ts}），
    clean_run_meta.run_id UNIQUE 天然满足，无需改表结构
  - bad 脏数据复用 pipeline save_bad 落盘到 datasets/bad/
  - processed 重建调用 scripts/数据导出_DB到processed.export_all（统一 parquet）
  - DatasetVersion 完整版本管理服务当前项目未实现（仅数据模型类），
    重建 processed 暂用直接导出，标注 TODO 待版本服务落地后接入

数据流:
  quant.db(market_data) → DataQueries.get_market_data → run_clean_pipeline
  → DataQueries.delete_market_data + bulk_insert_market_data（事务写回）
  → CleanDataQueries.insert_clean_run / accumulate_dirty_stats（元信息）
  → datasets/bad/*.json（脏数据归档，pipeline 自动落盘）
  → 可选重建 datasets/processed/*.parquet
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing as mp
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from myquant.core.logger import get_logger
from myquant.database.connection import get_connection
from myquant.database.queries import CleanDataQueries, DataQueries
from myquant.data_access.cleaner import load_clean_config, run_clean_pipeline
from myquant.data_access.cleaner.derived_metrics import compute_derived_metrics, compute_b_class_metrics

logger = get_logger("full_clean_service")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "database" / "quant.db"
PROCESSED_DIR = PROJECT_ROOT / "datasets" / "processed"
BAD_DIR = PROJECT_ROOT / "datasets" / "bad"
OUTPUT_DIR = PROJECT_ROOT / "outputs"
BACKUP_DIR = PROJECT_ROOT / "backups"

# 默认排除规则（指数/基金/板块，可被 GUI 自定义覆盖）:
#   000.SH = 上证指数(000001.SH上证指数, 000300.SH沪深300等)
#   399    = 深证/中证指数(399001.SZ深证成指等)
#   880    = 通达信板块指数
#   5.SH   = 沪市基金/ETF(511910.SH等)
#   1.SZ   = 深市基金/LOF(160137.SZ等)
#   30.SZ  = 创业板(300/301开头.SZ)
#   688.SH = 科创板(688开头.SH)
#   4/8/92 = 北交所/老三板(4开头老三板、8开头北交所82/83/87/88、92北交所920新段)
DEFAULT_EXCLUDE_RULES: List[str] = ["000.SH", "399", "880", "5.SH", "1.SZ", "30.SZ", "688.SH", "4", "8", "92"]


# ═══════════════════════════════════════════════
#  排除规则匹配（指数/基金过滤）
# ═══════════════════════════════════════════════

def match_rule(symbol: str, rule: str) -> bool:
    """
    判断 symbol 是否命中排除规则。
    规则支持两种形式:
      "000.SH" -> 代码以 000 开头 且 市场为 SH（后缀感知，不会误伤 000001.SZ 平安银行）
      "399"    -> 代码以 399 开头（不限市场）
    """
    rule = rule.strip()
    if not rule:
        return False
    code, _, market = symbol.partition(".")
    if "." in rule:
        prefix, suffix = rule.split(".", 1)
        return code.startswith(prefix) and market.upper() == suffix.strip().upper()
    return code.startswith(rule)


def filter_excluded_symbols(
    symbols: List[str], exclude_rules: List[str]
) -> Tuple[List[str], List[str]]:
    """按排除规则过滤。Returns: (保留标的, 被排除标的)"""
    if not exclude_rules:
        return symbols, []
    keep, excluded = [], []
    for sym in symbols:
        if any(match_rule(sym, r) for r in exclude_rules):
            excluded.append(sym)
        else:
            keep.append(sym)
    return keep, excluded


# ═══════════════════════════════════════════════
#  质量分级 & 错误码提取
# ═══════════════════════════════════════════════

def grade_by_rate(valid_rate: float) -> Tuple[str, str]:
    """(quality_grade, severity)"""
    if valid_rate >= 0.999:
        return "A", "info"
    if valid_rate >= 0.99:
        return "B", "warning"
    if valid_rate >= 0.95:
        return "C", "error"
    return "D", "critical"


def extract_error_codes(errors: List[str]) -> List[str]:
    """从 _errors 中提取错误码，如 '[NEGATIVE_VOLUME] volume 为负' -> NEGATIVE_VOLUME"""
    codes = []
    for e in errors or []:
        if isinstance(e, str) and e.startswith("["):
            end = e.find("]")
            if end > 0:
                codes.append(e[1:end])
    return codes


# ═══════════════════════════════════════════════
#  清洗元信息公共记录函数（M5修复：增量/全量双入口统一）
# ═══════════════════════════════════════════════

def record_clean_meta(
    cdq: CleanDataQueries,
    *,
    symbol: str,
    data_type: str = "bar",
    total: int = 0,
    valid: int = 0,
    bad_records: Optional[List[Dict[str, Any]]] = None,
    duration_sec: float = 0.0,
    bad_file: Optional[str] = None,
    report_file: Optional[str] = None,
    source: str = "offline",
    status: str = "completed",
    error: Optional[str] = None,
    bad_by_type_sink: Optional[Dict[str, int]] = None,
) -> Optional[str]:
    """
    清洗运行元信息统一记录（M5修复：增量清洗与全量清洗共用）

    写 clean_run_meta + dirty_data_stats 两表，与全量清洗同口径：
      - 质量等级由合格率经 grade_by_rate 分级（A/B/C/D）
      - 脏数据按错误码分类累加（extract_error_codes）

    统一后增量清洗（scripts/数据清洗.py → run_clean_pipeline）与全量清洗
    （FullCleanService）的元信息能力对齐，避免只维护一条链路导致另一条
    静默失效。实盘流式清洗走 insert_clean_run_from_pipeline_event（同一
    CleanDataQueries 接口族），三者口径一致。

    Parameters
    ----------
    cdq : CleanDataQueries
        清洗数据查询接口（已建表）
    symbol / data_type : str
        标的 / 数据类型
    total / valid : int
        原始总数 / 有效数
    bad_records : List[Dict] | None
        脏数据记录（含 _errors 字段，用于错误码分类统计）
    duration_sec : float
        清洗耗时（秒）
    bad_file / report_file : str | None
        脏数据归档 / 质量报告文件路径
    source : str
        来源标识（全量=full_clean / 增量=incremental_cli / 实盘=live）
    status : str
        运行状态（completed/failed）
    error : str | None
        错误信息（失败时）
    bad_by_type_sink : Dict | None
        可选的错误码累计容器（全量清洗用于跨标的聚合；增量传None）

    Returns
    -------
    str | None : run_id（DB写入失败返回None，不抛异常）
    """
    bad_records = bad_records or []
    bad = len(bad_records)
    valid_rate = (valid / total) if total else 0.0
    grade, severity = grade_by_rate(valid_rate)
    now = datetime.now().isoformat(timespec="seconds")

    try:
        run_id = cdq.insert_clean_run(
            data_type=data_type,
            symbol=symbol,
            status=status,
            total=total, valid=valid, bad=bad,
            valid_rate=round(valid_rate, 6),
            quality_grade=grade, severity=severity,
            steps_executed=["validate", "deduplicate", "detect_anomalies", "sort"],
            total_duration=duration_sec,
            bad_file=bad_file,
            report_file=report_file,
            source=source,
            started_at=now, finished_at=now,
            error=error,
        )

        # 脏数据统计（按错误码累加，与全量清洗同口径）
        bad_by_type_local: Dict[str, int] = {}
        sample_by_type: Dict[str, Dict] = {}
        for b in bad_records:
            # 字段兼容：全量清洗 bad_records 用 "errors"（worker 转换），
            # 增量清洗 pipeline 原始 bad 记录用 "_errors"（清洗器输出）
            errs = b.get("_errors") or b.get("errors") or []
            for code in extract_error_codes(errs):
                bad_by_type_local[code] = bad_by_type_local.get(code, 0) + 1
                if bad_by_type_sink is not None:
                    bad_by_type_sink[code] = bad_by_type_sink.get(code, 0) + 1
                if code not in sample_by_type:
                    sample_by_type[code] = {
                        "date": b.get("date"), "volume": b.get("volume"),
                        "errors": errs,
                    }
        for code, count in bad_by_type_local.items():
            cdq.accumulate_dirty_stats(
                run_id, symbol, data_type, code, count,
                sample_data=sample_by_type.get(code),
            )
        return run_id
    except Exception as e:
        logger.warning(f"[meta] 清洗元信息记录失败（不影响清洗结果）: {symbol}: {e}")
        return None


# ═══════════════════════════════════════════════
#  Worker：单只标的清洗（多进程入口，模块顶层可 pickle）
# ═══════════════════════════════════════════════

def worker_clean(task: Tuple[str, str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    单只标的清洗。由子进程调用。
    task = (symbol, db_path, clean_config)
    Returns:
      {symbol, status, db_original_count, cleaned_count, bad_count,
       bad_records(摘要), needs_write, cleaned_data(List[Dict]|None),
       stats, bad_file}
    """
    symbol, db_path, clean_config = task
    result: Dict[str, Any] = {"symbol": symbol, "status": "error"}
    try:
        conn = get_connection(db_path)
        dq = DataQueries(conn)
        # DataQueries.get_market_data 返回 List[Dict]，字段含 trade_date/open/...，
        # 清洗器 _get_timestamp 原生支持 trade_date
        rows = dq.get_market_data(symbol, period="daily", adjust="hfq")
        if not rows:
            result.update(
                status="empty", db_original_count=0,
                cleaned_count=0, bad_count=0, stats={},
            )
            return result

        # save_bad=True：pipeline 自动把脏数据落盘到 datasets/bad/bad_bar_{symbol}_{ts}.json
        pipe_result = run_clean_pipeline(
            records=rows,
            data_type="bar",
            symbol=symbol,
            config=clean_config,
            save_bad=True,
            save_report=False,
            publish_events=False,  # 批量场景避免污染监控告警
        )
        cleaned = pipe_result["cleaned"]
        # 派生量计算：涨跌幅/振幅/量比（清洗后按日排序数据上批量计算）
        if cleaned:
            compute_derived_metrics(cleaned)
            # B类：换手率/流通股本/市盈(动)（需读financial_data）
            try:
                fin_rows = dq.conn.execute("SELECT report_date, circ_mv, total_mv, net_profit FROM financial_data WHERE symbol=? ORDER BY report_date", (symbol,))
                if fin_rows:
                    compute_b_class_metrics(cleaned, [dict(r) for r in fin_rows])
            except Exception as _e:
                logger.warning(f"B类派生量计算失败 {symbol}: {_e}")
        bad = pipe_result["bad"]
        stats = pipe_result["stats"]
        bad_file = pipe_result.get("bad_file")

        # 判断是否需要写回：派生量已写入（强制写回），或有脏数据剔除/去重/行数变化
        needs_write = True or (
            len(bad) > 0
            or (stats.get("removed_duplicates", 0) or 0) > 0
            or len(cleaned) != len(rows)
        )

        # bad 记录摘要（不回传整条超大 payload，只保留定位+错误码）
        bad_records = []
        for b in bad:
            bad_records.append({
                "symbol": symbol,
                "date": b.get("date") or b.get("trade_date"),
                "open": b.get("open"), "high": b.get("high"),
                "low": b.get("low"), "close": b.get("close"),
                "volume": b.get("volume"), "amount": b.get("amount"),
                "errors": b.get("_errors", []),
            })

        result.update(
            status="ok",
            db_original_count=len(rows),
            cleaned_count=len(cleaned),
            bad_count=len(bad),
            bad_records=bad_records,
            needs_write=needs_write,
            # 仅当需要写回时回传完整清洗数据（省内存/传输）
            cleaned_data=cleaned if needs_write else None,
            stats=stats,
            bad_file=bad_file,
        )
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.exception(f"worker 清洗失败 {symbol}: {e}")
    return result


# ═══════════════════════════════════════════════
#  全量清洗服务
# ═══════════════════════════════════════════════

class FullCleanService:
    """全量数据清洗服务（主进程负责调度、写回、元信息记录）"""

    def __init__(
        self,
        config: Dict[str, Any],
        db_path: Optional[Path] = None,
        dry_run: bool = False,
        force: bool = False,
    ):
        self.config = config
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.dry_run = dry_run
        self.force = force

        self.batch_id = config.get("batch_id") or f"full_clean_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.batch_source = f"full_clean:{self.batch_id}"
        self.workers = int(config.get("workers", 8))
        self.exclude_rules = config.get("exclude_rules") or []
        self.rebuild_processed = bool(config.get("rebuild_processed", True))
        self.keep_symbols: Optional[List[str]] = None  # 清洗保留标的，重建 processed 白名单
        self.do_backup = bool(config.get("backup", True))

        # 合并清洗参数（GUI 覆盖默认，如 max_price_jump_pct）
        self.clean_config = load_clean_config()
        overrides = config.get("clean_overrides") or {}
        if overrides:
            bar_cfg = self.clean_config.setdefault("bar", {})
            for k, v in overrides.items():
                if k in ("max_price_jump_pct", "max_volume_ratio", "allow_zero_volume",
                         "check_ohlc_logic", "check_non_negative", "duplicate_strategy",
                         "price_jump_action", "volume_anomaly_action", "reset_base_gap_days"):
                    bar_cfg[k] = v

        self.stats_summary: Dict[str, Any] = {}
        self.failed_symbols: List[str] = []
        self.bad_by_type: Dict[str, int] = {}

    # ── 标的解析 ──
    def resolve_symbols(self, dq: DataQueries) -> Tuple[List[str], List[str]]:
        all_symbols = dq.get_all_symbols(period="daily")
        if self.config.get("symbols"):
            filter_set = {s.strip() for s in str(self.config["symbols"]).split(",") if s.strip()}
            all_symbols = [s for s in all_symbols if s in filter_set]
        return filter_excluded_symbols(all_symbols, self.exclude_rules)

    # ── 断点续传：本次 batch 已 completed 的标的 ──
    def load_completed_symbols(self, cdq: CleanDataQueries) -> set:
        if self.force or self.dry_run:
            return set()
        runs = cdq.get_clean_runs(
            source=self.batch_source, status="completed", limit=200000,
        )
        return {r["symbol"] for r in runs}

    # ── 备份 ──
    def backup_db(self) -> Optional[Path]:
        if self.dry_run or not self.do_backup:
            return None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = BACKUP_DIR / f"backup_{ts}_quant_before_clean.db"
        shutil.copy2(self.db_path, dst)
        print(f"  [备份] quant.db → {dst} ({dst.stat().st_size/1024/1024:.1f} MB)")
        return dst

    # ── 单只写回（DataQueries 事务） ──
    def write_back_symbol(
        self, dq: DataQueries, symbol: str, cleaned_data: Optional[List[Dict]]
    ) -> str:
        """
        事务内写回：删该标的原数据 + 批量插入清洗后数据。
        注意：delete_market_data 按 symbol 删除（当前 DB 仅 daily/hfq，安全）。
        Returns: 'written' / 'unchanged' / 'error'
        """
        if cleaned_data is None:
            return "unchanged"
        if self.dry_run:
            return "written"
        try:
            with dq.conn.transaction():
                dq.delete_market_data(symbol)
                if cleaned_data:
                    dq.bulk_insert_market_data(cleaned_data, replace=True)
            return "written"
        except Exception as e:
            logger.error(f"写回失败 {symbol}: {e}")
            return "error"

    # ── 记录元信息（CleanDataQueries） ──
    def record_meta(
        self, cdq: CleanDataQueries, symbol: str,
        result: Dict[str, Any], write_status: str,
    ) -> Optional[str]:
        """写 clean_run_meta + dirty_data_stats，返回 run_id（M5修复：委托公共函数）"""
        stats = result.get("stats") or {}
        return record_clean_meta(
            cdq,
            symbol=symbol,
            data_type="bar",
            total=result.get("db_original_count", 0),
            valid=result.get("cleaned_count", 0),
            bad_records=result.get("bad_records", []),
            duration_sec=stats.get("duration_sec", 0.0),
            bad_file=result.get("bad_file"),
            source=self.batch_source,
            status="completed" if write_status != "error" else "failed",
            error=result.get("error"),
            bad_by_type_sink=self.bad_by_type,
        )

    # ── 主流程 ──
    def run(self) -> Dict[str, Any]:
        t0 = time.time()
        print("=" * 70)
        print("  Pythonwdhmm 全量数据清洗（FullCleanService）")
        print("=" * 70)
        print(f"  batch_id: {self.batch_id}")
        print(f"  DB: {self.db_path}")
        print(f"  进程数: {self.workers}  dry_run: {self.dry_run}  force: {self.force}")
        print(f"  排除规则: {self.exclude_rules or '无'}")
        print(f"  清洗后重建 processed: {self.rebuild_processed}")
        print(f"  清洗配置: bar.enabled={self.clean_config.get('enabled')}, "
              f"max_price_jump_pct={self.clean_config.get('bar', {}).get('max_price_jump_pct')}")
        print("=" * 70)

        conn = get_connection(str(self.db_path))
        dq = DataQueries(conn)
        cdq = CleanDataQueries(conn)
        try:
            keep_symbols, excluded = self.resolve_symbols(dq)
            self.keep_symbols = keep_symbols
            print(f"\n[1/6] 标的解析完成: 全量={len(keep_symbols)+len(excluded)}, "
                  f"排除指数/基金={len(excluded)}, 实际清洗={len(keep_symbols)}")
            if excluded:
                print(f"       排除示例: {excluded[:5]} ...")

            completed = self.load_completed_symbols(cdq)
            todo = [s for s in keep_symbols if s not in completed]
            print(f"[2/6] 断点续传: 已完成={len(completed)}, 待清洗={len(todo)}")
            if not todo:
                print("       全部已完成，无需清洗。")
                return {"status": "done", "reason": "all_completed"}

            backup_path = self.backup_db()
            print(f"[3/6] 备份完成" + (f": {backup_path}" if backup_path else "（dry-run 跳过）"))

            print(f"[4/6] 开始并行清洗（{self.workers} 进程）...")
            tasks = [(s, str(self.db_path), self.clean_config) for s in todo]
            counts = {"ok": 0, "written": 0, "unchanged": 0, "error": 0, "empty": 0}
            stats_agg = {"bad_rows": 0, "removed_duplicates": 0, "price_jumps": 0,
                         "volume_anomalies": 0, "time_violations": 0, "total_rows": 0}

            ctx = mp.get_context("spawn")
            with ctx.Pool(processes=self.workers) as pool:
                for i, result in enumerate(
                    pool.imap_unordered(worker_clean, tasks, chunksize=1), 1
                ):
                    symbol = result["symbol"]
                    st = result.get("status", "error")
                    counts[st if st in counts else "error"] += 1

                    if st == "ok":
                        write_status = self.write_back_symbol(
                            dq, symbol, result.get("cleaned_data"),
                        )
                        counts[write_status if write_status in counts else "error"] += 1
                        if not self.dry_run:
                            self.record_meta(cdq, symbol, result, write_status)
                        s = result.get("stats") or {}
                        stats_agg["total_rows"] += result.get("db_original_count", 0)
                        stats_agg["bad_rows"] += result.get("bad_count", 0)
                        stats_agg["removed_duplicates"] += s.get("removed_duplicates", 0)
                        stats_agg["price_jumps"] += s.get("price_jumps", 0)
                        stats_agg["volume_anomalies"] += s.get("volume_anomalies", 0)
                        stats_agg["time_violations"] += s.get("time_violations", 0)
                    else:
                        self.failed_symbols.append(symbol)

                    if i % 100 == 0 or i == len(todo):
                        el = time.time() - t0
                        print(f"  [{i}/{len(todo)}] {i/len(todo)*100:.1f}% | "
                              f"ok={counts['ok']} written={counts['written']} "
                              f"unchanged={counts['unchanged']} err={counts['error']} "
                              f"| 耗时={el:.0f}s", flush=True)

            print(f"[5/6] 清洗完成: 统计 {counts}")
            if self.failed_symbols:
                print(f"       失败标的({len(self.failed_symbols)}): {self.failed_symbols[:10]}")

            # 重建 processed（集成 parquet 导出）
            if self.rebuild_processed and not self.dry_run:
                self.rebuild_processed_data()

            # 报告
            report = self.build_report(counts, stats_agg, t0, backup_path)
            self.save_report(report)
            print(f"[6/6] 全量清洗报告: {OUTPUT_DIR / f'clean_all_{self.batch_id}.json'}")
            return report
        finally:
            conn.close()

    # ── 重建 processed（集成 parquet 导出） ──
    def rebuild_processed_data(self) -> None:
        """调用 scripts/数据导出_DB到processed.py 的 export_all，重建扁平 parquet。"""
        script = PROJECT_ROOT / "scripts" / "数据导出_DB到processed.py"
        if not script.exists():
            print("  [警告] 未找到 数据导出_DB到processed.py，跳过重建 processed")
            return
        spec = importlib.util.spec_from_file_location("export_processed", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print("\n  [重建] 开始导出 quant.db → processed/*.parquet ...")
        n_keep = len(self.keep_symbols or [])
        print(f"  [重建] 仅导出清洗保留标的（白名单 {n_keep} 只，已剔除板块/指数/基金）")
        stats = mod.export_all(
            self.db_path, PROCESSED_DIR, force=True, symbols_filter=self.keep_symbols,
        )
        print(f"  [重建] 导出完成: 总数={stats['total']} 新导出={stats['exported']} "
              f"跳过={stats['skipped']} 失败={stats['failed']} 耗时={stats['duration']:.0f}s")
        # 清理白名单外的旧 parquet（确保 processed 只含清洗保留标的）
        keep_set = set(self.keep_symbols or [])
        if keep_set:
            removed_stale = 0
            for f in PROCESSED_DIR.glob("*.parquet"):
                if f.stem not in keep_set:
                    try:
                        f.unlink()
                        removed_stale += 1
                    except OSError:
                        pass
            if removed_stale:
                print(f"  [清理] 移除白名单外的旧 parquet {removed_stale} 个")
        # 清理旧格式空壳子目录（仅空目录）
        removed = 0
        for d in sorted(PROCESSED_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                if not any(d.iterdir()):
                    d.rmdir()
                    removed += 1
            except OSError:
                pass
        if removed:
            print(f"  [清理] 移除 {removed} 个旧格式空壳子目录")

    # ── 报告 ──
    def build_report(
        self, counts: Dict[str, int], stats_agg: Dict[str, Any],
        t0: float, backup_path: Optional[Path],
    ) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "batch_source": self.batch_source,
            "started_at": self.config.get("started_at"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "duration_sec": round(time.time() - t0, 2),
            "exclude_rules": self.exclude_rules,
            "dry_run": self.dry_run,
            "counts": counts,
            "summary": stats_agg,
            "bad_by_type": self.bad_by_type,
            "failed_symbols": self.failed_symbols,
            "backup": str(backup_path) if backup_path else None,
        }

    def save_report(self, report: Dict[str, Any]) -> Path:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUTPUT_DIR / f"clean_all_{self.batch_id}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
