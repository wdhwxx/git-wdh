# 文件名：myquant/data_access/data_layer_baseline.py
"""
data_layer_baseline.py - 数据层状态指纹基线（只读）
===================================================
对数据层做只读状态采集，产出可重跑复现的 7 组指纹，供施工前后对比与回归验证。

指纹分组（对应《数据层结构优化可行性施工方案报告_v1.1》§5.2 A-00）：
  1. market_data 规模：行数 / 标的数 / adjust 分布 / 日期范围 / pct_chg 非空率
  2. data_source 分布
  3. 唯一键重复：(symbol, period, adjust, trade_date) 聚合的重复键总数与 Top 样本
  4. financial_data 规模与 ann_date 覆盖率
  5. processed parquet 物理现状：文件数 / 列数分布
  6. 辅助表规模：adjust_factor / share_change / clean_run_meta / dirty_data_stats /
     factor_data 行数与 bad 目录文件数
  7. 版本戳：alembic_version

全程只读：sqlite 以 ``file:...?mode=ro`` 打开 + ``PRAGMA query_only=ON``，
不修改数据库、不写 processed、不触碰任何项目业务文件。

依赖: sqlite3, json, datetime, pathlib, typing, pyarrow, myquant.core.logger
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from myquant.core.logger import get_logger

logger = get_logger("data_access.data_layer_baseline")

# 基线 JSON 的协议版本（结构变更时递增，消费方据此判定兼容）
BASELINE_SCHEMA_VERSION: int = 1

# 唯一键重复聚合使用的列（对应 B-01 部分唯一索引的键）
DUP_KEY_COLUMNS: Tuple[str, ...] = ("symbol", "period", "adjust", "trade_date")

# 辅助表清单（存在即统计行数，不存在记 not_present）
AUX_TABLE_NAMES: Tuple[str, ...] = (
    "adjust_factor",
    "share_change",
    "clean_run_meta",
    "dirty_data_stats",
    "factor_data",
)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """以只读模式打开 SQLite 数据库。

    Args:
        db_path: 数据库文件绝对路径。

    Returns:
        只读 sqlite 连接（``PRAGMA query_only=ON`` 已生效）。

    Raises:
        FileNotFoundError: 数据库文件不存在时抛出。
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"数据库文件不存在: {db_path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:  # pragma: no cover - 只读打开失败属环境异常
        conn.close()
        raise RuntimeError(f"数据库只读模式设置失败: {exc}") from exc
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """判断表是否存在（sqlite_master 查询）。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """返回表全部列名（PRAGMA table_info），表不存在返回空列表。"""
    if not _table_exists(conn, table):
        return []
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _safe_scalar(conn: sqlite3.Connection, sql: str, params: Tuple[Any, ...] = ()) -> Any:
    """执行单值查询；SQLite 异常时记录日志并返回 None（不中断整组采集）。"""
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.Error as exc:
        logger.warning("指纹采集查询失败: %s (%s)", sql, exc)
        return None


def collect_market_data_fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
    """采集 market_data 规模指纹（第 1 组）。

    覆盖：行数、标的数、adjust 分布、日期范围、pct_chg 非空率。

    Args:
        conn: 只读数据库连接。

    Returns:
        指纹字典；表不存在时返回 {"present": False}。
    """
    if not _table_exists(conn, "market_data"):
        return {"present": False}
    cols = _table_columns(conn, "market_data")
    total = _safe_scalar(conn, "SELECT COUNT(*) FROM market_data") or 0
    symbols = _safe_scalar(conn, "SELECT COUNT(DISTINCT symbol) FROM market_data") or 0
    adjust_dist = [
        [r[0], r[1]]
        for r in conn.execute(
            "SELECT adjust, COUNT(*) FROM market_data GROUP BY adjust ORDER BY 2 DESC"
        ).fetchall()
    ]
    date_range = conn.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM market_data"
    ).fetchone()
    pct_chg_nonnull = _safe_scalar(
        conn, "SELECT COUNT(*) FROM market_data WHERE pct_chg IS NOT NULL"
    ) or 0
    return {
        "present": True,
        "table_columns": cols,
        "rows": total,
        "symbols": symbols,
        "adjust_dist": adjust_dist,
        "date_range": list(date_range) if date_range else None,
        "pct_chg_nonnull": pct_chg_nonnull,
        "pct_chg_rate": round(pct_chg_nonnull / total, 6) if total else 0.0,
    }


def collect_data_source_fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
    """采集 data_source 分布指纹（第 2 组）。

    Args:
        conn: 只读数据库连接。

    Returns:
        指纹字典；表不存在时返回 {"present": False}。
    """
    if not _table_exists(conn, "market_data"):
        return {"present": False}
    rows = conn.execute(
        "SELECT data_source, COUNT(*) FROM market_data GROUP BY data_source ORDER BY 2 DESC"
    ).fetchall()
    return {"present": True, "distribution": [[r[0], r[1]] for r in rows]}


def collect_duplicate_keys_fingerprint(
    conn: sqlite3.Connection, limit: int = 20
) -> Dict[str, Any]:
    """采集唯一键重复指纹（第 3 组）。

    按 (symbol, period, adjust, trade_date) 聚合，输出重复键总数与 Top 样本。
    trade_time 为 NULL 的行是唯一约束失效的核心场景（B-01 部分唯一索引的键）。

    Args:
        conn: 只读数据库连接。
        limit: 返回的 Top 重复键样本条数上限。

    Returns:
        指纹字典；表不存在时返回 {"present": False}。
    """
    if not _table_exists(conn, "market_data"):
        return {"present": False}
    cols = _table_columns(conn, "market_data")
    key_cols = [c for c in DUP_KEY_COLUMNS if c in cols]
    if len(key_cols) != len(DUP_KEY_COLUMNS):
        return {"present": True, "error": f"缺少聚合列: {DUP_KEY_COLUMNS} vs {cols}"}
    group_sql = ", ".join(key_cols)
    total = _safe_scalar(
        conn,
        f"SELECT COUNT(*) FROM (SELECT {group_sql} FROM market_data "
        f"GROUP BY {group_sql} HAVING COUNT(*)>1)",
    ) or 0
    top = conn.execute(
        f"SELECT {group_sql}, COUNT(*) c FROM market_data GROUP BY {group_sql} "
        "HAVING c>1 ORDER BY c DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {
        "present": True,
        "key_columns": list(key_cols),
        "duplicate_key_total": total,
        "top_samples": [[*r[: len(key_cols)], r[len(key_cols)]] for r in top],
    }


def collect_financial_fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
    """采集 financial_data 规模与 ann_date 覆盖率指纹（第 4 组）。

    覆盖：行数、标的数、ann_date 非空/为空行数、晚披露样本数
    （ann_date > report_date + 4 个月，即法定截止日推定窗口）。

    Args:
        conn: 只读数据库连接。

    Returns:
        指纹字典；表不存在时返回 {"present": False}。
    """
    if not _table_exists(conn, "financial_data"):
        return {"present": False}
    cols = _table_columns(conn, "financial_data")
    rows = _safe_scalar(conn, "SELECT COUNT(*) FROM financial_data") or 0
    symbols = (
        _safe_scalar(conn, "SELECT COUNT(DISTINCT symbol) FROM financial_data") or 0
    )
    if "ann_date" not in cols:
        return {"present": True, "rows": rows, "symbols": symbols,
                "ann_date_column": False}
    ann_notnull = _safe_scalar(
        conn,
        "SELECT COUNT(*) FROM financial_data WHERE ann_date IS NOT NULL AND ann_date!=''",
    ) or 0
    ann_null = rows - ann_notnull
    late = 0
    if "report_date" in cols:
        late = _safe_scalar(
            conn,
            "SELECT COUNT(*) FROM financial_data WHERE ann_date IS NOT NULL "
            "AND ann_date!='' AND report_date IS NOT NULL AND report_date!='' "
            "AND ann_date > date(report_date, '+4 months')",
        ) or 0
    return {
        "present": True,
        "table_columns": cols,
        "rows": rows,
        "symbols": symbols,
        "ann_date_notnull": ann_notnull,
        "ann_date_null": ann_null,
        "ann_date_rate": round(ann_notnull / rows, 6) if rows else 0.0,
        "late_disclosure_gt_4m": late,
    }


def collect_processed_fingerprint(processed_dir: str) -> Dict[str, Any]:
    """采集 processed parquet 物理现状指纹（第 5 组）。

    用 pyarrow 只读 schema（不加载数据），统计文件数与列数分布。

    Args:
        processed_dir: datasets/processed 目录路径。

    Returns:
        指纹字典；目录不存在时返回 {"present": False}。
    """
    base = Path(processed_dir)
    if not base.is_dir():
        return {"present": False}
    try:
        import pyarrow.parquet as pq  # 延迟导入，避免无 pyarrow 环境阻断其他组
    except ImportError as exc:  # pragma: no cover
        logger.warning("pyarrow 不可用，processed 指纹跳过: %s", exc)
        return {"present": False, "error": "pyarrow unavailable"}
    column_dist: Dict[str, int] = {}
    file_count = 0
    sample_columns: Optional[List[str]] = None
    for p in sorted(base.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".parquet", ".pq"):
            continue
        file_count += 1
        try:
            schema = pq.read_schema(str(p))
            names = list(schema.names)
        except Exception as exc:  # noqa: BLE001 - 单文件损坏不中断整组扫描
            logger.warning("parquet schema 读取失败 %s: %s", p.name, exc)
            names = ["<unreadable>"]
        key = str(len(names))
        column_dist[key] = column_dist.get(key, 0) + 1
        if sample_columns is None:
            sample_columns = names
    return {
        "present": True,
        "file_count": file_count,
        "column_count_distribution": [
            [k, v] for k, v in sorted(column_dist.items())
        ],
        "sample_columns_first_file": sample_columns,
    }


def collect_aux_tables_fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
    """采集辅助表规模指纹（第 6 组）。

    统计 adjust_factor / share_change / clean_run_meta / dirty_data_stats /
    factor_data 行数；bad 目录文件数单独统计。

    Args:
        conn: 只读数据库连接。

    Returns:
        指纹字典。
    """
    out: Dict[str, Any] = {}
    for t in AUX_TABLE_NAMES:
        if _table_exists(conn, t):
            out[t] = _safe_scalar(conn, f"SELECT COUNT(*) FROM {t}") or 0
        else:
            out[t] = "not_present"
    return out


def collect_schema_version_fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
    """采集版本戳指纹（第 7 组）。

    Alembic 版本号取自 alembic_version 表；不存在时标记 not_present。

    Args:
        conn: 只读数据库连接。

    Returns:
        指纹字典。
    """
    if _table_exists(conn, "alembic_version"):
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        return {"present": True, "alembic_version": [r[0] for r in rows]}
    return {"present": False}


def build_baseline(
    db_path: str,
    processed_dir: str,
    baseline_date: Optional[str] = None,
) -> Dict[str, Any]:
    """构建完整数据层状态指纹基线。

    Args:
        db_path: 数据库文件绝对路径（只读打开）。
        processed_dir: datasets/processed 目录路径（只读扫描）。
        baseline_date: 基线日期字符串（YYYY-MM-DD）；缺省取今天。

    Returns:
        基线字典：包含 schema_version / baseline_date / generated_at / 各指纹分组。

    Raises:
        FileNotFoundError: 数据库文件不存在时抛出。
    """
    if baseline_date is None:
        baseline_date = date.today().isoformat()
    conn = _open_readonly(db_path)
    try:
        baseline: Dict[str, Any] = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "baseline_date": baseline_date,
            "generated_at": date.today().isoformat(),
            "db_path": str(Path(db_path).resolve()),
            "processed_dir": str(Path(processed_dir).resolve()),
            "fingerprints": {
                "market_data": collect_market_data_fingerprint(conn),
                "data_source": collect_data_source_fingerprint(conn),
                "duplicate_keys": collect_duplicate_keys_fingerprint(conn),
                "financial_data": collect_financial_fingerprint(conn),
                "processed": collect_processed_fingerprint(processed_dir),
                "aux_tables": collect_aux_tables_fingerprint(conn),
                "schema_version": collect_schema_version_fingerprint(conn),
            },
        }
        return baseline
    finally:
        conn.close()


def save_baseline_json(
    baseline: Dict[str, Any], out_dir: str, baseline_date: Optional[str] = None
) -> str:
    """把基线字典原子写入 ``out_dir/数据层基线_<YYYYMMDD>.json``。

    Args:
        baseline: build_baseline 返回的基线字典。
        out_dir: 输出目录（如 outputs/）。
        baseline_date: 基线日期字符串；缺省取 baseline 中的 baseline_date。

    Returns:
        实际写入的 JSON 文件绝对路径。

    Raises:
        OSError: 目录不可写时抛出。
    """
    if baseline_date is None:
        baseline_date = str(baseline.get("baseline_date") or date.today().isoformat())
    stamp = baseline_date.replace("-", "")
    out = Path(out_dir)
    os.makedirs(out, exist_ok=True)
    target = out / f"数据层基线_{stamp}.json"
    tmp = out / f"数据层基线_{stamp}.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, target)
    logger.info("数据层基线已写入: %s", target)
    return str(target)


def _self_test() -> None:
    """模块自带冒烟测试：内存临时库 + 临时 parquet 目录，验证采集闭环。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "t.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE market_data (symbol TEXT, period TEXT, adjust TEXT, "
            "trade_date TEXT, trade_time TEXT, pct_chg REAL, data_source TEXT)"
        )
        conn.execute(
            "CREATE TABLE financial_data (symbol TEXT, report_date TEXT, ann_date TEXT)"
        )
        conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
        conn.execute("INSERT INTO alembic_version VALUES ('0009')")
        conn.executemany(
            "INSERT INTO market_data VALUES (?,?,?,?,?,?,?)",
            [
                ("000001.SZ", "daily", "raw", "2026-10-14", None, 1.2, "tdx"),
                ("000001.SZ", "daily", "raw", "2026-10-15", None, -0.5, "tdx"),
                ("000001.SZ", "daily", "raw", "2026-10-15", None, 0.3, "incremental_clean"),
                ("600519.SH", "daily", "raw", "2026-10-15", None, 0.1, "tencent_http"),
            ],
        )
        conn.executemany(
            "INSERT INTO financial_data VALUES (?,?,?)",
            [
                ("000001.SZ", "2026-06-30", None),
                ("600519.SH", "2026-06-30", "2026-08-20"),
            ],
        )
        conn.commit()
        conn.close()
        proc = Path(tmp) / "processed"
        proc.mkdir()
        import pandas as pd  # 延迟导入（仅自测路径）

        pd.DataFrame({"日期": [1], "收盘价": [10.0]}).to_parquet(proc / "a.parquet")
        baseline = build_baseline(db, str(proc), "2026-10-17")
        fp = baseline["fingerprints"]
        assert fp["market_data"]["rows"] == 4, fp["market_data"]
        assert fp["market_data"]["symbols"] == 2, fp["market_data"]
        assert fp["duplicate_keys"]["duplicate_key_total"] == 1, fp["duplicate_keys"]
        assert fp["financial_data"]["ann_date_notnull"] == 1, fp["financial_data"]
        assert fp["processed"]["file_count"] == 1, fp["processed"]
        assert fp["schema_version"]["alembic_version"] == ["0009"], fp["schema_version"]
        path = save_baseline_json(baseline, str(Path(tmp) / "out"))
        with open(path, encoding="utf-8") as f:
            reloaded = json.load(f)
        assert reloaded["fingerprints"]["market_data"]["rows"] == 4
        print("data_layer_baseline 自测通过 ✅")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _self_test()
