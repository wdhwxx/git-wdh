# 文件名：tests/unit/test_survivorship_bias.py
"""
test_survivorship_bias.py - F-04 幸存者偏差评估器测试
==================================================
覆盖：
  1. 数据覆盖审计（字段齐全/退市覆盖=0/口径声明/停牌近似池/processed 计数）
  2. 偏差折价估计（合成 DB：停牌拖累→折价为负、区间非法/样本不足防御）
  3. 逐期存续池构建（as_of 过滤、非法日期防御）
  4. CoverageAudit 序列化往返 + 非法输入
  5. batch_report 口径声明（None 零变化 / 渲染 / HTML 转义防注入）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from myquant.backtest.batch_report import (
    BatchReportMeta,
    BatchSummaryReportGenerator,
)
from myquant.backtest.survivorship_bias import (
    CoverageAudit,
    audit_data_coverage,
    build_point_in_time_pool,
    estimate_survivorship_discount,
)

# 合成交易日序列（每月一个点，满足 MIN_TRADING_DAYS=5）
DATES: List[str] = [
    "2026-01-05", "2026-02-05", "2026-03-05", "2026-04-05",
    "2026-05-05", "2026-06-05", "2026-07-05", "2026-08-05", "2026-09-05",
]
REF_DATE: str = "2026-09-30"
SUSPEND_DAYS: int = 60


# ═══════════════════════════════════════════════
#  测试辅助
# ═══════════════════════════════════════════════

def _make_db(
    tmp_path: Path,
    n_survivor: int = 30,
    n_suspended: int = 5,
    survivor_close: Sequence[float] = (10.0, 10.6, 11.2, 11.8, 12.4,
                                        13.0, 13.6, 14.2, 15.0),
    suspended_close: Sequence[float] = (10.0, 9.5, 9.0, 8.5, 8.0),
) -> Path:
    """构造合成 quant.db：存续池（最后交易日 09-05）+ 停牌近似池（03-05）。"""
    db = tmp_path / "quant.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE market_data (symbol TEXT, trade_date TEXT, "
        "period TEXT, adjust TEXT, close REAL)"
    )
    for i in range(n_survivor):
        sym = f"SV{i:04d}.SZ"
        for j, d in enumerate(DATES):
            conn.execute(
                "INSERT INTO market_data VALUES (?,?,?,?,?)",
                (sym, d, "daily", "raw", survivor_close[j]),
            )
    for i in range(n_suspended):
        sym = f"SP{i:04d}.SZ"
        for j, d in enumerate(DATES[:5]):  # 数据截至 2026-03-05
            conn.execute(
                "INSERT INTO market_data VALUES (?,?,?,?,?)",
                (sym, d, "daily", "raw", suspended_close[j]),
            )
    conn.commit()
    conn.close()
    return db


# ═══════════════════════════════════════════════
#  1. 数据覆盖审计
# ═══════════════════════════════════════════════

def test_audit_basic_fields(tmp_path: Path) -> None:
    """审计返回字段齐全，退市覆盖=0（当前数据源无历史退市股）"""
    db = _make_db(tmp_path)
    audit = audit_data_coverage(
        db_path=db, processed_dir=tmp_path / "processed",
        suspend_days=SUSPEND_DAYS, ref_date=REF_DATE,
    )
    assert audit.db_symbol_count == 35
    assert audit.delisted_coverage is False
    assert audit.latest_trade_date == "2026-09-05"
    assert audit.suspended_count == 5
    assert len(audit.suspended_pool) == 5
    assert "幸存者偏差" in audit.statement
    assert audit.to_dict()["db_symbol_count"] == 35


def test_audit_suspended_pool_membership(tmp_path: Path) -> None:
    """最后交易日早于 ref-suspend_days 的标的进入停牌近似池"""
    db = _make_db(tmp_path, n_survivor=2, n_suspended=3)
    audit = audit_data_coverage(
        db_path=db, processed_dir=tmp_path / "processed",
        suspend_days=SUSPEND_DAYS, ref_date=REF_DATE,
    )
    assert all(s.startswith("SP") for s in audit.suspended_pool)
    assert all(s.startswith("SV") for s in audit.suspended_pool
               ) is False or audit.suspended_count == 3


def test_audit_processed_count(tmp_path: Path) -> None:
    """processed 目录 parquet 计数（含停牌标的时计入）"""
    db = _make_db(tmp_path)
    pdir = tmp_path / "processed"
    pdir.mkdir()
    (pdir / "SV0000.SZ.parquet").write_bytes(b"x")
    (pdir / "SP0000.SZ.parquet").write_bytes(b"x")
    (pdir / ".hidden").write_bytes(b"x")
    audit = audit_data_coverage(
        db_path=db, processed_dir=pdir,
        suspend_days=SUSPEND_DAYS, ref_date=REF_DATE,
    )
    assert audit.processed_symbol_count == 2  # 隐藏文件不计


def test_audit_db_missing(tmp_path: Path) -> None:
    """quant.db 不存在 → FileNotFoundError"""
    with pytest.raises(FileNotFoundError):
        audit_data_coverage(
            db_path=tmp_path / "nope.db",
            processed_dir=tmp_path / "processed",
            ref_date=REF_DATE,
        )


# ═══════════════════════════════════════════════
#  2. 偏差折价估计
# ═══════════════════════════════════════════════

def test_estimate_discount_suspended_drags(tmp_path: Path) -> None:
    """停牌近似池区间收益更低 → 全样本低于存续池（折价为负）"""
    db = _make_db(tmp_path)
    result = estimate_survivorship_discount(
        db_path=db,
        start_date="2026-01-05", end_date="2026-09-05",
        suspend_days=SUSPEND_DAYS, ref_date=REF_DATE,
    )
    # 存续池 ≈ +50%，停牌池 ≈ -20%，35 只等权 → 全样本 ≈ +40%
    assert result["survivor_pool_return"] == pytest.approx(0.5, abs=1e-6)
    assert result["full_pool_return"] == pytest.approx(
        (30 * 0.5 + 5 * (-0.2)) / 35, abs=1e-6)
    assert result["discount"] < 0.0
    assert result["n_survivor"] == 30
    assert result["n_suspended_used"] == 5
    assert "口径" in result["note"]
    # 年化折算字段存在
    assert "discount_annualized" in result


def test_estimate_discount_no_suspended(tmp_path: Path) -> None:
    """无停牌标的时折价≈0（两池同收益）"""
    db = _make_db(tmp_path, n_survivor=20, n_suspended=0)
    result = estimate_survivorship_discount(
        db_path=db,
        start_date="2026-01-05", end_date="2026-09-05",
        suspend_days=SUSPEND_DAYS, ref_date=REF_DATE,
    )
    assert result["discount"] == pytest.approx(0.0, abs=1e-9)
    assert result["n_suspended_used"] == 0


def test_estimate_discount_invalid_range(tmp_path: Path) -> None:
    """区间 start > end → ValueError"""
    db = _make_db(tmp_path)
    with pytest.raises(ValueError):
        estimate_survivorship_discount(
            db_path=db,
            start_date="2026-09-05", end_date="2026-01-05",
            suspend_days=SUSPEND_DAYS, ref_date=REF_DATE,
        )


def test_estimate_discount_insufficient(tmp_path: Path) -> None:
    """样本不足（每池 <3）→ ValueError"""
    db = _make_db(tmp_path, n_survivor=2, n_suspended=1)
    with pytest.raises(ValueError):
        estimate_survivorship_discount(
            db_path=db,
            start_date="2026-01-05", end_date="2026-09-05",
            suspend_days=SUSPEND_DAYS, ref_date=REF_DATE,
        )


# ═══════════════════════════════════════════════
#  3. 逐期存续池构建
# ═══════════════════════════════════════════════

def test_build_point_in_time_pool_basic() -> None:
    """as_of 过滤：最后交易日 >= as_of 的标的保留"""
    last_map = {
        "A.SZ": "2026-09-30",
        "B.SZ": "2026-06-30",
        "C.SZ": "2025-12-31",
        "D.SZ": None,  # 无数据 → 跳过
    }
    pool = build_point_in_time_pool(last_map, "2026-07-01")
    assert pool == ["A.SZ"]
    pool2 = build_point_in_time_pool(last_map, "2026-01-01")
    assert pool2 == ["A.SZ", "B.SZ"]


def test_build_point_in_time_pool_invalid_date() -> None:
    """非法 as_of → ValueError"""
    with pytest.raises(ValueError):
        build_point_in_time_pool({"A.SZ": "2026-09-30"}, "not-a-date")


# ═══════════════════════════════════════════════
#  4. CoverageAudit 序列化
# ═══════════════════════════════════════════════

def test_coverage_audit_roundtrip() -> None:
    """to_dict/from_dict 往返等价"""
    audit = CoverageAudit(
        db_symbol_count=35,
        processed_symbol_count=31,
        latest_trade_date="2026-09-30",
        suspended_pool=["SP0000.SZ"],
        suspended_count=1,
        delisted_coverage=False,
        statement="口径声明",
    )
    audit2 = CoverageAudit.from_dict(audit.to_dict())
    assert audit2 == audit


def test_coverage_audit_from_dict_invalid() -> None:
    """非法输入 → ValueError"""
    with pytest.raises(ValueError):
        CoverageAudit.from_dict({})
    with pytest.raises(ValueError):
        CoverageAudit.from_dict("bad")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════
#  5. batch_report 口径声明集成
# ═══════════════════════════════════════════════

def _make_rows() -> List[Dict[str, Any]]:
    return [
        {"symbol": "A.SZ", "status": "completed", "total_return": 0.1,
         "sharpe_ratio": 1.2, "max_drawdown": -0.05, "trade_count": 10,
         "equity_length": 100, "mode": "mixed", "source": "重算", "error": ""},
        {"symbol": "B.SZ", "status": "completed", "total_return": -0.03,
         "sharpe_ratio": 0.4, "max_drawdown": -0.08, "trade_count": 6,
         "equity_length": 100, "mode": "mixed", "source": "复用", "error": ""},
    ]


def _make_meta() -> BatchReportMeta:
    return BatchReportMeta(
        strategy_name="ma_cross",
        start_date="2026-01-01",
        end_date="2026-09-30",
        initial_capital=1_000_000.0,
        base_mode="mixed",
        force_full=False,
        total=2,
        recomputed=1,
        reused=1,
    )


def test_report_data_coverage_none_zero_change(tmp_path: Path) -> None:
    """无 data_coverage → 报告不含声明节（默认零变化）"""
    gen = BatchSummaryReportGenerator(title="测试")
    out = tmp_path / "report.html"
    gen.generate_html(_make_rows(), _make_meta(), out)
    html = out.read_text(encoding="utf-8")
    assert "数据口径声明" not in html
    assert "幸存者偏差" not in html


def test_report_data_coverage_rendered(tmp_path: Path) -> None:
    """data_coverage 注入 → 渲染声明节（含折价系数）"""
    gen = BatchSummaryReportGenerator(title="测试")
    out = tmp_path / "report.html"
    coverage = {
        "statement": "口径声明文本：存在幸存者偏差",
        "db_symbol_count": 35,
        "processed_symbol_count": 31,
        "suspended_count": 5,
        "delisted_coverage": False,
        "discount": -0.1,
        "discount_annualized": -0.3,
    }
    gen.generate_html(_make_rows(), _make_meta(), out, data_coverage=coverage)
    html = out.read_text(encoding="utf-8")
    assert "数据口径声明" in html
    assert "口径声明文本" in html
    assert "不含历史退市股" in html
    assert "-10.00%" in html  # 折价 -0.1 → -10.00%
    assert "幸存者偏差" in html


def test_report_data_coverage_html_escaped(tmp_path: Path) -> None:
    """statement 含 HTML 标签 → 转义防注入"""
    gen = BatchSummaryReportGenerator(title="测试")
    out = tmp_path / "report.html"
    coverage = {
        "statement": "<script>alert(1)</script>声明",
        "db_symbol_count": 1,
        "processed_symbol_count": 1,
        "suspended_count": 0,
        "delisted_coverage": False,
    }
    gen.generate_html(_make_rows(), _make_meta(), out, data_coverage=coverage)
    html = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


__all__: List[str] = []
