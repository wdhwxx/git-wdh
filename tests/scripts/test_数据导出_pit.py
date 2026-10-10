# -*- coding: utf-8 -*-
"""
test_数据导出_pit.py - 导出路径 PIT 负向口径（M1A-09，v3.0 §6.4.7 验收）
======================================================================

DEC-05 结论：pytest 不直接收集 ``scripts/``（脚本不在零覆盖门禁扫描范围
MYQUANT.rglob），施工动作 = 单独建测试 + 确认收集路径覆盖——本文件放
``tests/scripts/``（已加入 pyproject testpaths），以 importlib 方式加载
``scripts/数据导出_DB到processed.py``，在内存 SQLite 上复现导出路径的
每股收益时点匹配（参照 ``tests/unit/test_f15_ann_date_pointin.py:57`` 负向口径）：

- 负向：7/15 信号日不应看到 8/28 才披露的中报 eps；
- 正向：披露后（9/1）可见中报 eps；
- 装配校验：load_symbol_data 的财务匹配已收敛到
  ``myquant.data_access.time_semantics.match_latest_financial``（③ 修复）。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "数据导出_DB到processed.py"

_spec = importlib.util.spec_from_file_location("数据导出_DB到processed", SCRIPT_PATH)
assert _spec and _spec.loader
EXPORT_MOD = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(EXPORT_MOD)


def _make_db(symbol: str = "000001.SZ") -> sqlite3.Connection:
    """内存 SQLite：market_data（raw 日线）+ financial_data（含 ann_date）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE market_data (
            symbol TEXT, period TEXT, adjust TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            amount REAL, pct_chg REAL, amplitude REAL, volume_ratio REAL,
            turnover REAL, float_share REAL, pe_dynamic REAL
        );
        CREATE TABLE financial_data (
            symbol TEXT, report_date TEXT, ann_date TEXT, eps REAL
        );
        """
    )
    # 两个交易日：7/15（信号日）、9/1（中报披露后）
    for td in ("2025-07-15", "2025-09-01"):
        conn.execute(
            "INSERT INTO market_data VALUES (?, 'daily', 'raw', ?, "
            "10.0, 10.5, 9.5, 10.2, 1000.0, 10200.0, 1.0, 5.0, 1.2, "
            "0.5, 100000000.0, 15.0)",
            (symbol, td),
        )
    # 一季报 4/29 披露 eps=0.10；中报 8/28 披露 eps=0.42
    conn.execute(
        "INSERT INTO financial_data VALUES (?, '2025-03-31', '2025-04-29', 0.10)",
        (symbol,),
    )
    conn.execute(
        "INSERT INTO financial_data VALUES (?, '2025-06-30', '2025-08-28', 0.42)",
        (symbol,),
    )
    conn.commit()
    return conn


def test_negative_mid_report_not_visible_on_7_15() -> None:
    """负向：7/15 信号日不应看到 8/28 才披露的中报 eps（每股收益=一季报 0.10）。"""
    conn = _make_db()
    df = EXPORT_MOD.load_symbol_data(conn, "000001.SZ")
    row_715 = df.loc[df["日期"] == 20250715, "每股收益"].iloc[0]
    assert row_715 == 0.10, f"7/15 不应看到中报 eps=0.42，实际={row_715}"


def test_positive_mid_report_visible_after_ann() -> None:
    """正向：9/1（中报 8/28 已披露）可见中报 eps=0.42。"""
    conn = _make_db()
    df = EXPORT_MOD.load_symbol_data(conn, "000001.SZ")
    row_901 = df.loc[df["日期"] == 20250901, "每股收益"].iloc[0]
    assert row_901 == 0.42


def test_load_symbol_data_uses_unified_matcher() -> None:
    """装配校验：③ 修复后导出脚本收敛到 match_latest_financial（不再按 report_date 穿越）。"""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "match_latest_financial" in src
    assert "SELECT report_date, ann_date, eps FROM financial_data" in src


def test_eps_column_present_without_financial_rows() -> None:
    """无财务记录时每股收益列置 None（不阻断导出）。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE market_data (
            symbol TEXT, period TEXT, adjust TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            amount REAL, pct_chg REAL, amplitude REAL, volume_ratio REAL,
            turnover REAL, float_share REAL, pe_dynamic REAL
        );
        CREATE TABLE financial_data (
            symbol TEXT, report_date TEXT, ann_date TEXT, eps REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO market_data VALUES ('000001.SZ', 'daily', 'raw', '2025-07-15', "
        "10.0, 10.5, 9.5, 10.2, 1000.0, 10200.0, 1.0, 5.0, 1.2, "
        "0.5, 100000000.0, 15.0)"
    )
    conn.commit()
    df = EXPORT_MOD.load_symbol_data(conn, "000001.SZ")
    assert "每股收益" in df.columns
    assert df["每股收益"].iloc[0] is None


# ═══════════════════════════════════════════
# M1A-10/11（v3.0 §6.4.4/§6.4.3）：available_time 派生 + rev 联动
# ═══════════════════════════════════════════


def test_available_time_after_eff_plus_one() -> None:
    """财务列 available_time = 有效披露日 + 1（不等于 trade_date，v3.0 §6.4.4 禁止混同）。"""
    conn = _make_db()
    df = EXPORT_MOD.load_symbol_data(conn, "000001.SZ")
    # 7/15 行：一季报 eff=2025-04-29 → available=2025-04-30（≠ 2025-07-15）
    row_715 = df.loc[df["日期"] == 20250715]
    assert row_715["每股收益"].iloc[0] == 0.10
    assert row_715["available_time"].iloc[0] == "2025-04-30"
    assert row_715["available_time"].iloc[0] != str(row_715["日期"].iloc[0])
    # 9/1 行：中报 eff=2025-08-28 → available=2025-08-29（≠ 2025-09-01）
    row_901 = df.loc[df["日期"] == 20250901]
    assert row_901["每股收益"].iloc[0] == 0.42
    assert row_901["available_time"].iloc[0] == "2025-08-29"


def test_available_time_none_when_eps_unavailable() -> None:
    """每股收益不可用（无匹配财务记录）→ available_time 为 None（无可用时点）。"""
    conn = _make_db()
    # 加一个早于任何披露日的交易日（2025-01-02）：无可见财务记录
    conn.execute(
        "INSERT INTO market_data VALUES ('000001.SZ', 'daily', 'raw', '2025-01-02', "
        "10.0, 10.5, 9.5, 10.2, 1000.0, 10200.0, 1.0, 5.0, 1.2, "
        "0.5, 100000000.0, 15.0)"
    )
    conn.commit()
    df = EXPORT_MOD.load_symbol_data(conn, "000001.SZ")
    row = df.loc[df["日期"] == 20250102]
    assert pd.isna(row["每股收益"].iloc[0])  # pandas float 列 None→NaN
    assert pd.isna(row["available_time"].iloc[0])  # StringDtype 下 None→NaN


def test_export_all_writes_manifest_with_rev(tmp_path) -> None:
    """export_all 写批次 sidecar：schema_revision 单一真源（financial_data=3）。"""
    from myquant.utils.data_versioner import DATASET_SCHEMA_REVISIONS

    db_path = tmp_path / "quant.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE market_data (
            symbol TEXT, period TEXT, adjust TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            amount REAL, pct_chg REAL, amplitude REAL, volume_ratio REAL,
            turnover REAL, float_share REAL, pe_dynamic REAL
        );
        CREATE TABLE financial_data (
            symbol TEXT, report_date TEXT, ann_date TEXT, eps REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO market_data VALUES ('000001.SZ', 'daily', 'raw', '2025-07-15', "
        "10.0, 10.5, 9.5, 10.2, 1000.0, 10200.0, 1.0, 5.0, 1.2, "
        "0.5, 100000000.0, 15.0)"
    )
    conn.execute(
        "INSERT INTO financial_data VALUES ('000001.SZ', '2025-03-31', '2025-04-29', 0.10)"
    )
    conn.commit()
    conn.close()

    out = tmp_path / "processed"
    out.mkdir()
    stats = EXPORT_MOD.export_all(db_path, out, force=True)
    assert stats["exported"] >= 1

    import json as _json

    manifest = _json.loads((out / "export_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_revision"]["financial_data"] == DATASET_SCHEMA_REVISIONS["financial_data"] == 3
    assert "每股收益" in manifest["available_time_rule"]
    assert "eff_disclosure_date + 1" in manifest["available_time_rule"]["每股收益"]


def test_export_all_blocks_old_rev_mismatch(tmp_path) -> None:
    """M1A-11：旧批次 rev=2 与新批次 rev=3 混用 → 兼容断言阻断（跨口径比较守卫）。"""
    import json as _json

    from myquant.utils.data_versioner import DataVersionMismatchError

    db_path = tmp_path / "quant.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE market_data (
            symbol TEXT, period TEXT, adjust TEXT, trade_date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            amount REAL, pct_chg REAL, amplitude REAL, volume_ratio REAL,
            turnover REAL, float_share REAL, pe_dynamic REAL
        );
        CREATE TABLE financial_data (
            symbol TEXT, report_date TEXT, ann_date TEXT, eps REAL
        );
        """
    )
    conn.execute(
        "INSERT INTO market_data VALUES ('000001.SZ', 'daily', 'raw', '2025-07-15', "
        "10.0, 10.5, 9.5, 10.2, 1000.0, 10200.0, 1.0, 5.0, 1.2, "
        "0.5, 100000000.0, 15.0)"
    )
    conn.commit()
    conn.close()

    out = tmp_path / "processed"
    out.mkdir()
    # 预写旧批次 manifest（financial_data rev=2，旧口径）
    (out / "export_manifest.json").write_text(
        _json.dumps(
            {"schema_revision": {"market_data": 2, "financial_data": 2, "factor_data": 1}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(DataVersionMismatchError):
        EXPORT_MOD.export_all(db_path, out, force=True)
