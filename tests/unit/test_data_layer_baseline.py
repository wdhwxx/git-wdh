# 文件名：tests/unit/test_data_layer_baseline.py
"""data_layer_baseline 状态指纹基线单测。

验证 7 组指纹的只读采集与 JSON 落盘闭环：
  1. market_data 规模（行数/标的/adjust 分布/日期范围/pct_chg 非空率）
  2. data_source 分布
  3. 唯一键重复总数与 Top 样本
  4. financial_data 规模与 ann_date 覆盖率
  5. processed parquet 文件数与列数分布
  6. 辅助表行数
  7. alembic_version 版本戳
核心保证：只读打开（不写库）、重复键口径正确、可重跑复现（两次构建一致）、
缺表/缺库防御（not_present / FileNotFoundError）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from myquant.data_access.data_layer_baseline import (
    build_baseline,
    build_duplicate_listing,
    collect_aux_tables_fingerprint,
    collect_data_source_fingerprint,
    collect_duplicate_keys_fingerprint,
    collect_financial_fingerprint,
    collect_market_data_fingerprint,
    collect_processed_fingerprint,
    collect_schema_version_fingerprint,
    save_baseline_json,
)


def _make_db(tmp_path: Path) -> str:
    """构造含样本数据的临时 SQLite 库，返回库路径。"""
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE market_data (symbol TEXT, period TEXT, adjust TEXT, "
        "trade_date TEXT, trade_time TEXT, open REAL, high REAL, low REAL, "
        "close REAL, volume REAL, amount REAL, pct_chg REAL, data_source TEXT, "
        "created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE financial_data (symbol TEXT, report_date TEXT, ann_date TEXT)"
    )
    conn.execute("CREATE TABLE adjust_factor (symbol TEXT, event_date TEXT, factor REAL)")
    conn.execute("CREATE TABLE share_change (symbol TEXT, change_date TEXT)")
    conn.execute("CREATE TABLE clean_run_meta (id INTEGER PRIMARY KEY, run_date TEXT)")
    conn.execute("CREATE TABLE dirty_data_stats (id INTEGER PRIMARY KEY, dirty_type TEXT)")
    conn.execute("CREATE TABLE factor_data (symbol TEXT, trade_date TEXT)")
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES ('0009')")
    conn.executemany(
        "INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # 000001.SZ 2026-10-15 重复 2 行（tdx + incremental_clean）
            ("000001.SZ", "daily", "raw", "2026-10-14", None, 10.0, 10.5, 9.8, 10.2,
             1000.0, 10200.0, 1.2, "tdx", "2026-10-15 09:00:00"),
            ("000001.SZ", "daily", "raw", "2026-10-15", None, 10.2, 10.8, 10.0, 10.6,
             1200.0, 12720.0, 3.9, "tdx", "2026-10-15 09:00:00"),
            ("000001.SZ", "daily", "raw", "2026-10-15", None, 10.2, 10.8, 10.0, 10.6,
             1200.0, 12720.0, 3.9, "incremental_clean", "2026-10-15 12:00:00"),
            ("600519.SH", "daily", "raw", "2026-10-15", None, 1500.0, 1520.0, 1490.0,
             1510.0, 800.0, 1208000.0, 0.5, "tencent_http", "2026-10-15 09:00:00"),
            # pct_chg 为空行（验证非空率分母）
            ("600519.SH", "daily", "raw", "2026-10-16", None, 1510.0, 1530.0, 1500.0,
             1520.0, 900.0, 1368000.0, None, "tdx", "2026-10-16 09:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO financial_data VALUES (?,?,?)",
        [
            ("000001.SZ", "2026-06-30", None),            # ann_date 为空
            ("600519.SH", "2026-06-30", "2026-08-20"),    # ann_date 非空
            ("600519.SH", "2025-12-31", "2026-06-01"),    # 晚披露（> 法定截止日推定）
        ],
    )
    conn.execute("INSERT INTO adjust_factor VALUES ('000001.SZ', '2026-10-15', 1.0)")
    conn.execute("INSERT INTO share_change VALUES ('000001.SZ', '2026-10-15')")
    conn.execute("INSERT INTO clean_run_meta VALUES (1, '2026-10-17')")
    conn.execute("INSERT INTO dirty_data_stats VALUES (1, 'PRICE_JUMP')")
    conn.execute("INSERT INTO factor_data VALUES ('000001.SZ', '2026-10-15')")
    conn.commit()
    conn.close()
    return db


def _make_processed(tmp_path: Path) -> Path:
    """构造含 2 个 parquet（15 列 / 14 列）的临时 processed 目录。"""
    proc = tmp_path / "processed"
    proc.mkdir()
    cols15 = ["日期", "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额",
              "涨跌幅", "振幅", "量比", "换手率", "流通股本", "市盈动", "每股收益",
              "available_time"]
    cols14 = cols15[:14]
    pd.DataFrame({c: [1] for c in cols15}).to_parquet(proc / "a.parquet")
    pd.DataFrame({c: [1] for c in cols14}).to_parquet(proc / "b.parquet")
    return proc


@pytest.fixture()
def baseline_db(tmp_path: Path) -> str:
    """临时库 fixture。"""
    return _make_db(tmp_path)


@pytest.fixture()
def processed_dir(tmp_path: Path) -> Path:
    """临时 processed 目录 fixture。"""
    return _make_processed(tmp_path)


def test_market_data_fingerprint(baseline_db: str) -> None:
    """第 1 组：行数/标的/adjust 分布/日期范围/pct_chg 非空率。"""
    from myquant.data_access.data_layer_baseline import _open_readonly

    conn = _open_readonly(baseline_db)
    try:
        fp = collect_market_data_fingerprint(conn)
    finally:
        conn.close()
    assert fp["present"] is True
    assert fp["rows"] == 5
    assert fp["symbols"] == 2
    assert fp["adjust_dist"] == [["raw", 5]]
    assert fp["date_range"] == ["2026-10-14", "2026-10-16"]
    assert fp["pct_chg_nonnull"] == 4
    assert fp["pct_chg_rate"] == 0.8


def test_data_source_fingerprint(baseline_db: str) -> None:
    """第 2 组：data_source 分布。"""
    from myquant.data_access.data_layer_baseline import _open_readonly

    conn = _open_readonly(baseline_db)
    try:
        fp = collect_data_source_fingerprint(conn)
    finally:
        conn.close()
    assert fp["present"] is True
    dist = dict(fp["distribution"])
    assert dist["tdx"] == 3
    assert dist["tencent_http"] == 1
    assert dist["incremental_clean"] == 1


def test_duplicate_keys_fingerprint(baseline_db: str) -> None:
    """第 3 组：唯一键重复总数与 Top 样本。"""
    from myquant.data_access.data_layer_baseline import _open_readonly

    conn = _open_readonly(baseline_db)
    try:
        fp = collect_duplicate_keys_fingerprint(conn)
    finally:
        conn.close()
    assert fp["present"] is True
    assert fp["duplicate_key_total"] == 1
    assert fp["top_samples"] == [["000001.SZ", "daily", "raw", "2026-10-15", 2]]


def test_financial_fingerprint(baseline_db: str) -> None:
    """第 4 组：financial_data 规模与 ann_date 覆盖率。"""
    from myquant.data_access.data_layer_baseline import _open_readonly

    conn = _open_readonly(baseline_db)
    try:
        fp = collect_financial_fingerprint(conn)
    finally:
        conn.close()
    assert fp["present"] is True
    assert fp["rows"] == 3
    assert fp["symbols"] == 2
    assert fp["ann_date_notnull"] == 2
    assert fp["ann_date_null"] == 1
    assert fp["ann_date_rate"] == pytest.approx(2 / 3)
    assert fp["late_disclosure_gt_4m"] == 1  # 2026-08-20 > 2026-06-30 + 4 个月


def test_processed_fingerprint(processed_dir: Path) -> None:
    """第 5 组：parquet 文件数与列数分布。"""
    fp = collect_processed_fingerprint(str(processed_dir))
    assert fp["present"] is True
    assert fp["file_count"] == 2
    dist = dict(fp["column_count_distribution"])
    assert dist["15"] == 1
    assert dist["14"] == 1


def test_aux_and_version_fingerprint(baseline_db: str) -> None:
    """第 6/7 组：辅助表行数与 alembic_version。"""
    from myquant.data_access.data_layer_baseline import _open_readonly

    conn = _open_readonly(baseline_db)
    try:
        aux = collect_aux_tables_fingerprint(conn)
        ver = collect_schema_version_fingerprint(conn)
    finally:
        conn.close()
    assert aux["adjust_factor"] == 1
    assert aux["share_change"] == 1
    assert aux["clean_run_meta"] == 1
    assert aux["dirty_data_stats"] == 1
    assert aux["factor_data"] == 1
    assert ver["present"] is True
    assert ver["alembic_version"] == ["0009"]


def test_build_save_reload_and_reproducible(
    baseline_db: str, processed_dir: Path, tmp_path: Path
) -> None:
    """完整闭环：构建 → 落盘 → 回读一致；两次构建可重跑复现。"""
    b1 = build_baseline(baseline_db, str(processed_dir), "2026-10-17")
    b2 = build_baseline(baseline_db, str(processed_dir), "2026-10-17")
    # 可重跑复现：排除 generated_at 后两次构建完全一致
    assert b1["fingerprints"] == b2["fingerprints"]
    out = str(tmp_path / "out")
    path = save_baseline_json(b1, out, "2026-10-17")
    assert path.endswith("数据层基线_20261017.json")
    with open(path, encoding="utf-8") as f:
        reloaded = json.load(f)
    assert reloaded["schema_version"] == 1
    assert reloaded["baseline_date"] == "2026-10-17"
    assert reloaded["fingerprints"] == b1["fingerprints"]


def test_duplicate_details_and_listing(
    baseline_db: str, tmp_path: Path
) -> None:
    """A-01：重复键全量明细采集与清单落盘。"""
    out = str(tmp_path / "out")
    path, listing = build_duplicate_listing(baseline_db, out, "2026-10-17")
    assert listing["present"] is True
    assert listing["duplicate_key_total"] == 1
    assert listing["duplicate_row_total"] == 2
    assert listing["key_columns"] == ["symbol", "period", "adjust", "trade_date"]
    detail = listing["details"][0]
    assert detail["key"] == {
        "symbol": "000001.SZ", "period": "daily", "adjust": "raw",
        "trade_date": "2026-10-15",
    }
    rows = detail["rows"]
    assert len(rows) == 2
    assert {r["data_source"] for r in rows} == {"tdx", "incremental_clean"}
    assert all("rowid" in r and "created_at" in r and "close" in r for r in rows)
    assert path.endswith("重复键定位清单_20261017.json")
    with open(path, encoding="utf-8") as f:
        reloaded = json.load(f)
    assert reloaded["duplicate_key_total"] == 1
    assert reloaded["duplicate_row_total"] == 2


def test_missing_db_raises(tmp_path: Path) -> None:
    """防御：数据库不存在时抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        build_baseline(str(tmp_path / "no.db"), str(tmp_path / "processed"))


def test_empty_db_fingerprints_present_false(tmp_path: Path) -> None:
    """防御：空库（无任何表）时各组指纹 present=False 不抛异常。"""
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    proc = tmp_path / "processed"
    proc.mkdir()
    baseline = build_baseline(db, str(proc), "2026-10-17")
    fp = baseline["fingerprints"]
    assert fp["market_data"]["present"] is False
    assert fp["data_source"]["present"] is False
    assert fp["duplicate_keys"]["present"] is False
    assert fp["financial_data"]["present"] is False
    assert fp["schema_version"]["present"] is False


# ── C-4 补测（#459，2026-10-17）：compare_baseline 全分支 + 防御分支 ──────
# 覆盖 compare_baseline 一致性比对（含数值容差/类型/缺失键/列表长度/嵌套）、
# _safe_scalar 查询异常、缺聚合列、processed 目录缺失/坏文件、无 ann_date 列、
# 辅助表缺表、落盘缺省日期等此前未覆盖分支，抬升 data_layer_baseline 覆盖率。

from myquant.data_access.data_layer_baseline import (
    _safe_scalar,
    _open_readonly,
    collect_duplicate_details,
    collect_financial_fingerprint,
    compare_baseline,
    save_duplicate_listing,
)


def test_compare_baseline_identical(baseline_db: str, processed_dir: Path) -> None:
    """compare_baseline：完全一致时返回空差异。"""
    b1 = build_baseline(baseline_db, str(processed_dir), "2026-10-17")
    b2 = build_baseline(baseline_db, str(processed_dir), "2026-10-17")
    assert compare_baseline(b1, b2) == []


def test_compare_baseline_numeric_tolerance() -> None:
    """compare_baseline：数值相对容差内不报差异，超差报差异。"""
    cur = {"fingerprints": {"a": {"v": 100.0, "rows": 1}}}
    base = {"fingerprints": {"a": {"v": 100.00000005, "rows": 1}}}
    assert compare_baseline(cur, base) == []  # 5e-8 < 1e-9*100 相对容差内
    base2 = {"fingerprints": {"a": {"v": 99.0, "rows": 1}}}
    diffs = compare_baseline(cur, base2)
    assert any("超相对容差" in d for d in diffs)


def test_compare_baseline_type_and_key_mismatch() -> None:
    """compare_baseline：类型变化/缺失键/现算缺失均报差异。"""
    diffs = compare_baseline(
        {"fingerprints": {"a": {"v": 1}}},
        {"fingerprints": {"a": {"v": "x"}}},
    )
    assert any("!=" in d for d in diffs)  # int vs str 类型不等
    diffs2 = compare_baseline(
        {"fingerprints": {"a": {"v": 1}, "b": {"w": 2}}},
        {"fingerprints": {"a": {"v": 1}}},
    )
    assert any("基线缺失" in d for d in diffs2)
    diffs3 = compare_baseline(
        {"fingerprints": {"a": {"v": 1}}},
        {"fingerprints": {"a": {"v": 1}, "b": {"w": 2}}},
    )
    assert any("现算缺失" in d for d in diffs3)


def test_compare_baseline_list_length_and_nested() -> None:
    """compare_baseline：列表长度不等/嵌套列表元素差异报差异。"""
    cur = {"fingerprints": {"a": {"dist": [[1, 2], [3, 4]]}}}
    base = {"fingerprints": {"a": {"dist": [[1, 2], [3, 5]]}}}
    diffs = compare_baseline(cur, base)
    assert any("dist[1][1]" in d for d in diffs)
    diffs2 = compare_baseline(
        {"fingerprints": {"a": {"dist": [1, 2]}}},
        {"fingerprints": {"a": {"dist": [1, 2, 3]}}},
    )
    assert any("长度" in d for d in diffs2)


def test_compare_baseline_bool_and_missing_fp() -> None:
    """compare_baseline：布尔严格比较；指纹段缺失返回固定差异。"""
    diffs = compare_baseline(
        {"fingerprints": {"a": {"flag": True}}},
        {"fingerprints": {"a": {"flag": 2}}},
    )
    assert any("flag" in d for d in diffs)  # bool vs int(2) 视为不等
    assert compare_baseline({"x": 1}, {"fingerprints": {}}) == [
        "fingerprints 缺失（current 或 baseline 无指纹段）"
    ]


def test_safe_scalar_query_error(baseline_db: str) -> None:
    """_safe_scalar：SQLite 查询异常时返回 None 不抛。"""
    conn = _open_readonly(baseline_db)
    try:
        assert _safe_scalar(conn, "SELECT COUNT(*) FROM no_such_table_xyz") is None
    finally:
        conn.close()


def test_duplicate_fingerprint_missing_key_columns(tmp_path: Path) -> None:
    """防御：market_data 缺重复键聚合列时返回 error 标记。"""
    db = str(tmp_path / "missing_cols.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE market_data (symbol TEXT, trade_date TEXT)"  # 缺 period/adjust
    )
    conn.commit()
    conn.close()
    conn = _open_readonly(db)
    try:
        fp = collect_duplicate_keys_fingerprint(conn)
        assert fp["present"] is True and "error" in fp
        det = collect_duplicate_details(conn)
        assert det["present"] is False and "error" in det
    finally:
        conn.close()


def test_financial_fingerprint_without_ann_date(tmp_path: Path) -> None:
    """防御：financial_data 无 ann_date 列时 ann_date_column=False。"""
    db = str(tmp_path / "no_ann.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE financial_data (symbol TEXT, report_date TEXT)")
    conn.commit()
    conn.close()
    conn = _open_readonly(db)
    try:
        fp = collect_financial_fingerprint(conn)
        assert fp["present"] is True
        assert fp["ann_date_column"] is False
    finally:
        conn.close()


def test_processed_fingerprint_missing_and_corrupt(tmp_path: Path) -> None:
    """防御：processed 目录缺失返回 present=False；坏 parquet 记 <unreadable>。"""
    assert collect_processed_fingerprint(str(tmp_path / "nope"))["present"] is False
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "bad.parquet").write_bytes(b"not a real parquet file at all")
    (proc / "note.txt").write_text("skip me")
    fp = collect_processed_fingerprint(str(proc))
    assert fp["present"] is True
    assert fp["file_count"] == 1  # txt 被过滤
    assert fp["column_count_distribution"] == [["1", 1]]  # ["<unreadable>"] 长度 1


def test_aux_tables_not_present(tmp_path: Path) -> None:
    """防御：辅助表缺失记 not_present。"""
    db = str(tmp_path / "no_aux.db")
    sqlite3.connect(db).close()
    conn = _open_readonly(db)
    try:
        aux = collect_aux_tables_fingerprint(conn)
        assert all(v == "not_present" for v in aux.values())
    finally:
        conn.close()


def test_save_listing_default_date(
    baseline_db: str, tmp_path: Path, monkeypatch
) -> None:
    """save_duplicate_listing：缺省日期用今天；原子落盘可回读。"""
    import datetime

    monkeypatch.setattr(
        "myquant.data_access.data_layer_baseline.date",
        datetime.date(2026, 10, 17),
    )
    conn = _open_readonly(baseline_db)
    try:
        listing = collect_duplicate_details(conn)
    finally:
        conn.close()
    out = str(tmp_path / "out")
    path = save_duplicate_listing(listing, out)
    assert path.endswith("重复键定位清单_20261017.json")
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["duplicate_key_total"] == 1
