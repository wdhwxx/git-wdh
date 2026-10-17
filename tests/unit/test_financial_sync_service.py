# 文件名：tests/unit/test_financial_sync_service.py
"""财务CSV增量同步服务单测（缺口A，蓝图 #74）。

覆盖：
  - find_missing_financial_symbols：DB 定位缺失标的 + 排除规则过滤（与清洗口径一致）
  - merge_and_save_csv：新旧合并去重（symbol+report_date keep=新）+ 17 列头保持
  - run_sync 全流程（fake fetcher）：定位→拉取→合并→统计
  - run_sync 自动入库：auto_import=True 时调用 FinancialImportService（monkeypatch）
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from myquant.data_access.financial_sync_service import (
    FinancialSyncService,
    _merge_and_save_csv,
)
from myquant.database.schema import CREATE_FINANCIAL_DATA_SQL, CREATE_MARKET_DATA_SQL

_MARKET_INSERT = (
    "INSERT INTO market_data (symbol, market, period, adjust, trade_date, open, high, low, close, "
    "volume, amount, pct_chg, turnover, amplitude, volume_ratio, float_share, pe_dynamic, data_source) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _make_db(tmp_path: Path) -> Path:
    """构建临时 DB：market_data 3 只（600000.SH 有财务 / 601091.SH 无 / 688001.SH 无但科创板 / 200011.SZ 无但B股）。"""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(CREATE_MARKET_DATA_SQL)
    conn.executescript(CREATE_FINANCIAL_DATA_SQL)
    for sym in ("600000.SH", "601091.SH", "688001.SH", "200011.SZ"):
        conn.execute(
            _MARKET_INSERT,
            (sym, "A股", "daily", "raw", "2026-09-28", 1.0, 1.0, 1.0, 1.0,
             1.0, 1.0, 0.0, None, None, None, None, None, "test"),
        )
    conn.execute(
        "INSERT INTO financial_data (symbol, market, report_date, report_type, data_source) "
        "VALUES (?,?,?,?,?)",
        ("600000.SH", "A股", "20260630", "财务摘要(历史)", "test"),
    )
    conn.commit()
    conn.close()
    return db


class FakeFetcher:
    """假数据源：按标的返回固定财务记录。"""

    def __init__(self, records_by_symbol: Dict[str, List[Dict[str, Any]]]) -> None:
        self._records = records_by_symbol

    def get_financial_history(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._records.get(symbol, [])

    def get_stock_list(self) -> List[Dict[str, str]]:
        return [{"symbol": s} for s in self._records]


class TestFindMissingFinancialSymbols:
    """待补财务标的定位（缺口A增量范围）。"""

    def test_定位缺失标的并过滤排除规则(self, tmp_path: Path) -> None:
        """market_data 有、financial_data 无 → 仅返回清洗保留标的（科创板/B股被排除）。"""
        db = _make_db(tmp_path)
        from myquant.data_access.financial_sync_service import (
            _find_missing_financial_symbols,
        )

        missing = _find_missing_financial_symbols(tmp_path, db_path=db)
        assert missing == ["601091.SH"]


class TestMergeAndSaveCsv:
    """CSV 合并去重。"""

    def test_新旧合并去重且列头保持(self, tmp_path: Path) -> None:
        """新记录与旧 CSV 按 symbol+report_date 去重 keep=新，17 列头不变。"""
        fin_dir = tmp_path / "datasets" / "raw" / "financial"
        fin_dir.mkdir(parents=True)
        old = fin_dir / "financial_history_20260912.csv"
        with open(old, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["symbol", "market", "report_date", "report_type", "pe", "pb",
                            "total_mv", "circ_mv", "eps", "bps", "revenue", "net_profit",
                            "net_profit_parent", "roe", "gross_margin", "debt_ratio", "data_source"],
            )
            writer.writeheader()
            writer.writerow({"symbol": "600000.SH", "market": "A股", "report_date": "20260630",
                             "report_type": "财务摘要(历史)", "eps": "0.89", "data_source": "old"})
            writer.writerow({"symbol": "600000.SH", "market": "A股", "report_date": "20251231",
                             "report_type": "财务摘要(历史)", "eps": "0.80", "data_source": "old"})

        new_records = [
            {"symbol": "601091.SH", "market": "A股", "report_date": "20260630",
             "report_type": "财务摘要(历史)", "eps": "1.10", "data_source": "new"},
            # 与旧 CSV 重复主键：新值覆盖
            {"symbol": "600000.SH", "market": "A股", "report_date": "20260630",
             "report_type": "财务摘要(历史)", "eps": "0.95", "data_source": "new"},
        ]
        out = _merge_and_save_csv(tmp_path, new_records)
        assert out is not None and out.exists()

        with open(out, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 3  # 旧2 + 新1（重复1条被新覆盖）
        by_key = {(r["symbol"], r["report_date"]): r for r in rows}
        assert by_key[("600000.SH", "20260630")]["eps"] == "0.95"  # 新值生效
        assert by_key[("601091.SH", "20260630")]["eps"] == "1.10"
        assert by_key[("600000.SH", "20251231")]["eps"] == "0.80"  # 旧值保留
        # 列头 18 列顺序不变（A-04 #432 新增 ann_date 后，与 FINANCIAL_CSV_COLUMNS 契约一致）
        with open(out, encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f))
        assert len(header) == 18 and header[0] == "symbol" and header[-1] == "data_source"

    def test_无新记录返回None(self, tmp_path: Path) -> None:
        """new_records 为空 → 不落盘返回 None。"""
        assert _merge_and_save_csv(tmp_path, []) is None


class TestRunSync:
    """同步主流程。"""

    def test_run_sync_增量流程(self, tmp_path: Path) -> None:
        """默认模式：自动定位缺失标的 → 拉取 → 合并落盘 → 统计（不入库）。"""
        db = _make_db(tmp_path)
        fetcher = FakeFetcher({
            "601091.SH": [{"symbol": "601091.SH", "market": "A股", "report_date": "20260630",
                           "report_type": "财务摘要(历史)", "eps": "1.10", "data_source": "new"}],
        })
        svc = FinancialSyncService(tmp_path)
        stats = svc.run_sync(auto_import=False, fetcher=fetcher, db_path=db)
        assert stats.symbols == ["601091.SH"]
        assert stats.fetched_symbols == 1
        assert stats.new_records == 1
        assert stats.csv_path is not None and Path(stats.csv_path).exists()
        assert stats.missing_after == []  # 601091.SH 已拉到
        assert stats.import_stats is None

    def test_run_sync_自动入库(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto_import=True → 合并后调用 FinancialImportService.import_financial_csv。"""
        db = _make_db(tmp_path)
        fetcher = FakeFetcher({
            "601091.SH": [{"symbol": "601091.SH", "market": "A股", "report_date": "20260630",
                           "report_type": "财务摘要(历史)", "eps": "1.10", "data_source": "new"}],
        })
        called: Dict[str, Any] = {}

        class FakeImportStats:
            csv_file = "x.csv"
            total_rows = 1
            success_rows = 1
            skipped_rows = 0
            symbol_count = 1
            min_report_date = "2026-06-30"
            max_report_date = "2026-06-30"

        class FakeImportService:
            def __init__(self, project_root: Path) -> None:
                called["project_root"] = project_root

            def import_financial_csv(self, csv_path: Path, progress_callback: Any = None) -> FakeImportStats:
                called["csv_path"] = str(csv_path)
                return FakeImportStats()

        monkeypatch.setattr(
            "myquant.data_access.financial_service.FinancialImportService",
            FakeImportService,
        )
        svc = FinancialSyncService(tmp_path)
        stats = svc.run_sync(auto_import=True, fetcher=fetcher, db_path=db)
        assert called["csv_path"] == stats.csv_path
        assert stats.import_stats is not None
        assert stats.import_stats["symbol_count"] == 1
        assert stats.import_stats["total_rows"] == 1

    def test_run_sync_无可拉取标的跳过(self, tmp_path: Path) -> None:
        """无缺失标的 → 空统计跳过。"""
        db = tmp_path / "t2.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(CREATE_MARKET_DATA_SQL)
        conn.executescript(CREATE_FINANCIAL_DATA_SQL)
        conn.execute(
            _MARKET_INSERT,
            ("600000.SH", "A股", "daily", "raw", "2026-09-28", 1.0, 1.0, 1.0, 1.0,
             1.0, 1.0, 0.0, None, None, None, None, None, "test"),
        )
        conn.execute(
            "INSERT INTO financial_data (symbol, market, report_date, report_type, data_source) "
            "VALUES (?,?,?,?,?)",
            ("600000.SH", "A股", "20260630", "财务摘要(历史)", "test"),
        )
        conn.commit()
        conn.close()
        svc = FinancialSyncService(tmp_path)
        stats = svc.run_sync(auto_import=False, db_path=db)
        assert stats.symbols == []
        assert stats.csv_path is None
