# 文件名：tests/unit/test_data_export_columns.py
"""全量导出列集合统一（换手率补列）单测（#72）。

验证：
  - CN_COLUMN_MAP 含 turnover→换手率
  - SELECT 语句包含 turnover（防映射与查询失同步）
  - load_symbol_data 对临时 DB 输出 13 列中文集合（含换手率），与增量 _en_to_cn 一致
  - financial_data 无记录 → 每股收益列仍存在（全 None），列集合不受影响
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from typing import Any, List

import pandas as pd
import pytest

from myquant.database.schema import CREATE_FINANCIAL_DATA_SQL, CREATE_MARKET_DATA_SQL

_EXPORT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "数据导出_DB到processed.py"

#: 目标中文列集合（与增量 _en_to_cn 列集合一致）
EXPECTED_CN_COLUMNS: List[str] = [
    "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额",
    "涨跌幅", "振幅", "量比", "换手率", "流通股本", "市盈动", "每股收益",
]

_INSERT_MARKET_SQL: str = (
    "INSERT INTO market_data (symbol, market, period, adjust, trade_date, open, high, low, close, "
    "volume, amount, pct_chg, turnover, amplitude, volume_ratio, float_share, pe_dynamic, data_source) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _load_export_module() -> Any:
    """importlib 加载导出脚本（薄入口模块）。"""
    spec = importlib.util.spec_from_file_location("export_processed", _EXPORT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_db(tmp_path: Path, symbol: str, market_row: List[Any]) -> Path:
    """构建临时 DB：market_data + financial_data 表 + 1 行行情。"""
    db = tmp_path / f"{symbol}.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(CREATE_MARKET_DATA_SQL)
    conn.executescript(CREATE_FINANCIAL_DATA_SQL)
    conn.execute(_INSERT_MARKET_SQL, market_row)
    conn.commit()
    conn.close()
    return db


class TestExportColumnSet:
    """全量导出列集合（#72 换手率补列）。"""

    def test_cn_column_map_含换手率(self) -> None:
        """CN_COLUMN_MAP 含 turnover→换手率。"""
        mod = _load_export_module()
        assert mod.CN_COLUMN_MAP["turnover"] == "换手率"

    def test_select语句含turnover(self) -> None:
        """load_symbol_data 的 SELECT 包含 turnover 列（防映射与查询失同步）。"""
        src = _EXPORT_SCRIPT.read_text(encoding="utf-8")
        body = src.split("def load_symbol_data")[1].split("def verify_parquet")[0]
        assert "turnover" in body
        assert "float_share" in body
        assert "pe_dynamic" in body

    def test_load_symbol_data_输出完整列集合(self, tmp_path: Path) -> None:
        """load_symbol_data 输出 13 列中文集合（含换手率），与增量 _en_to_cn 一致。"""
        row: List[Any] = [
            "000001.SZ", "A股", "daily", "raw", "2026-09-28", 11.28, 11.41, 11.27, 11.3,
            71534144.0, 810575808.0, 0.0, 1.23, 1.2389, 0.8165, 1.94e10, 5.0, "test",
        ]
        db = _make_db(tmp_path, "000001.SZ", row)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO financial_data (symbol, market, report_date, report_type, pe, total_mv, "
            "circ_mv, net_profit, eps, data_source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("000001.SZ", "A股", "2026-06-30", "财务摘要(历史)", 5.2, 2.2e11, 2.0e11, 2.5e10, 1.24, "test"),
        )
        conn.commit()
        conn.close()

        mod = _load_export_module()
        df = mod.load_symbol_data(sqlite3.connect(str(db)), "000001.SZ")
        # 扁平式：#72 13 列业务列 + "日期"列（整数 YYYYMMDD），index 为 RangeIndex
        assert list(df.columns) == ["日期"] + EXPECTED_CN_COLUMNS
        assert isinstance(df.index, pd.RangeIndex)
        assert int(df["日期"].iloc[0]) == 20260928
        # 换手率列值透传（DB 有值则导出有值）
        assert df["换手率"].iloc[0] == 1.23
        # 每股收益按报告期向前匹配
        assert df["每股收益"].iloc[0] == 1.24

    def test_load_symbol_data_无财务记录每股收益为None(self, tmp_path: Path) -> None:
        """financial_data 无记录 → 每股收益列仍存在（全 None），列集合不受影响。"""
        row: List[Any] = [
            "601091.SH", "A股", "daily", "raw", "2026-09-17", 13.0, 20.8, 11.73, 20.8,
            168125152.0, 2.18e9, None, None, None, None, None, None, "test",
        ]
        db = _make_db(tmp_path, "601091.SH", row)
        mod = _load_export_module()
        df = mod.load_symbol_data(sqlite3.connect(str(db)), "601091.SH")
        assert list(df.columns) == ["日期"] + EXPECTED_CN_COLUMNS
        assert isinstance(df.index, pd.RangeIndex)
        assert int(df["日期"].iloc[0]) == 20260917
        assert df["每股收益"].isna().all()
        assert df["换手率"].isna().all()

    def test_verify_parquet_扁平式通过(self, tmp_path: Path) -> None:
        """verify_parquet 从「日期」列读起止日期（扁平式），与 DB 统计一致 → 通过。"""
        mod = _load_export_module()
        # 构造扁平式 parquet（整数日期列 + 两行）
        df = pd.DataFrame({
            "日期": [20260917, 20260918],
            "收盘价": [13.0, 13.5],
            "开盘价": [12.9, 13.1],
        })
        path = tmp_path / "601091.SH.parquet"
        df.to_parquet(path, index=False)
        ok, msg = mod.verify_parquet(path, db_rows=2, db_start="2026-09-17", db_end="2026-09-18")
        assert ok, msg
        # 与 DB 统计不一致 → 失败
        ok2, _ = mod.verify_parquet(path, db_rows=2, db_start="2026-09-16", db_end="2026-09-18")
        assert not ok2

    def test_verify_parquet_旧索引式判失败触发重建(self, tmp_path: Path) -> None:
        """旧索引式文件（无「日期」列）→ verify 判失败（触发重建，不抛异常中断）。"""
        mod = _load_export_module()
        df = pd.DataFrame({
            "收盘价": [13.0, 13.5],
        }, index=pd.to_datetime(["2026-09-17", "2026-09-18"]))
        df.index.name = "日期"
        path = tmp_path / "old.parquet"
        df.to_parquet(path, index=True)
        ok, msg = mod.verify_parquet(path, db_rows=2, db_start="2026-09-17", db_end="2026-09-18")
        assert not ok
        assert "重建" in msg
