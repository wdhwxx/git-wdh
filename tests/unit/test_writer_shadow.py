# 文件名：tests/unit/test_writer_shadow.py
"""B-05 统一写入口影子模式单测。

覆盖：
  1. 正常行：现网 vs 影子 豁免 ingest 三列后逐字段零 diff（等价）；
  2. BLOCK 行（缺 close）：影子拒收 → blocked 清单，不产生写入行；
  3. FLAG 行（价格跳变）：影子 anomaly_flags 落列（现网 None）；
  4. enabled=False：影子关闭不产出比对差异（生产零影响）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from myquant.data_access.admission import AdmissionChecker
from myquant.data_access.writer_shadow import (
    _SHADOW_EXEMPT_COLUMNS,
    WriteShadowComparer,
)


@pytest.fixture
def checker(tmp_path: Path) -> AdmissionChecker:
    """默认阈值判定器（不依赖真实 config）。"""
    return AdmissionChecker(
        config_path=tmp_path / "no_data_clean.yaml",
        whitelist_path=tmp_path / "no_whitelist.yaml",
    )


def _raw_rows(**overrides) -> list:
    """构造原始行情行（与现网 _save_to_database 输入同构：date/time/open/...）。"""
    rows = [
        {
            "date": "2026-10-15",
            "time": None,
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000.0, "amount": 1020000.0,
            "pct_chg": 2.0, "turnover": 1.5,
        },
        {
            "date": "2026-10-16",
            "time": None,
            "open": 10.2, "high": 10.6, "low": 10.0, "close": 10.4,
            "volume": 90000.0, "amount": 936000.0,
            "pct_chg": 1.96, "turnover": 1.4,
        },
    ]
    for row in rows:
        row.update(overrides)
    return rows


def _legacy_rows(raw_rows: list) -> list:
    """模拟现网写路径产物（anomaly_flags 恒 None，与 manager._save_to_database 一致）。"""
    return [
        {
            "symbol": "000001.SZ",
            "market": "A股",
            "period": "daily",
            "adjust": "raw",
            "trade_date": r["date"],
            "trade_time": r["time"],
            "open": r["open"], "high": r["high"], "low": r["low"],
            "close": r["close"], "volume": r["volume"], "amount": r["amount"],
            "pct_chg": r["pct_chg"], "turnover": r["turnover"],
            "is_suspended": 1 if r.get("volume", 0) == 0 else 0,
            "data_source": "tdx",
            "ingest_time": "2026-10-17T00:00:00",
            "ingest_batch": "20261017-0001",
            "anomaly_flags": None,
        }
        for r in raw_rows
    ]


# ── 等价性 ─────────────────────────────────────────
def test_equivalent_zero_diff(checker: AdmissionChecker) -> None:
    """正常行：豁免 ingest 三列后现网 vs 影子零 diff。"""
    raw = _raw_rows()
    legacy = _legacy_rows(raw)
    comp = WriteShadowComparer(checker=checker, enabled=True)
    report = comp.run_compare(
        legacy, raw, "000001.SZ", "A股", "daily", "raw", "tdx"
    )
    assert report.checked_rows == 2
    assert report.consistent
    assert report.diffs == []
    assert report.blocked == []
    assert report.flagged == []


def test_shadow_rows_fill_ingest_columns(checker: AdmissionChecker) -> None:
    """影子行补 ingest 三列（B-02 采集元数据）。"""
    raw = _raw_rows()
    comp = WriteShadowComparer(checker=checker, enabled=True)
    shadow, _, _ = comp.shadow_rows(
        raw, "000001.SZ", "A股", "daily", "raw", "tdx"
    )
    assert len(shadow) == 2
    for row in shadow:
        assert row["ingest_time"]
        assert row["ingest_batch"]
        assert row["anomaly_flags"] is None  # 正常行无标记


# ── BLOCK ─────────────────────────────────────────
def test_blocked_row_not_written(checker: AdmissionChecker) -> None:
    """缺 close → 影子拒收：blocked 清单有记录、不产生该行写入。"""
    raw = _raw_rows()
    raw[0]["close"] = None  # 第一行缺必需字段
    legacy = _legacy_rows(raw)
    comp = WriteShadowComparer(checker=checker, enabled=True)
    report = comp.run_compare(
        legacy, raw, "000001.SZ", "A股", "daily", "raw", "tdx"
    )
    assert report.blocked and report.blocked[0][0] == 0
    assert any("missing_field:close" in b[1] for b in report.blocked)
    # 影子侧第一行被拒 → 影子只有 1 行，现网 2 行 → 比对上现网行 0 未对齐
    shadow, _, _ = comp.shadow_rows(raw, "000001.SZ", "A股", "daily", "raw", "tdx")
    assert len(shadow) == 1
    assert shadow[0]["trade_date"] == "2026-10-16"


# ── FLAG ─────────────────────────────────────────
def test_flagged_row_gets_anomaly_flags(checker: AdmissionChecker) -> None:
    """价格跳变 → 影子 anomaly_flags 落列（现网 None，豁免列内差异）。"""
    raw = _raw_rows()
    raw[1]["close"] = 13.0  # 相对前一行 close=10.2 跳变 +27% > 0.22
    legacy = _legacy_rows(raw)
    comp = WriteShadowComparer(checker=checker, enabled=True)
    report = comp.run_compare(
        legacy, raw, "000001.SZ", "A股", "daily", "raw", "tdx"
    )
    assert report.flagged and report.flagged[0][0] == 1
    assert any("price_jump" in f for f in report.flagged[0][1].split(","))
    # 豁免列差异不算 inconsistent
    assert report.consistent


def test_exempt_columns_defined() -> None:
    """豁免列集合固定为 ingest 三列。"""
    assert _SHADOW_EXEMPT_COLUMNS == {"ingest_time", "ingest_batch", "anomaly_flags"}


# ── 影子开关 ─────────────────────────────────────────
def test_disabled_no_effect(checker: AdmissionChecker) -> None:
    """enabled=False：compare 不产出差异（生产零影响）。"""
    raw = _raw_rows()
    legacy = _legacy_rows(raw)
    comp = WriteShadowComparer(checker=checker, enabled=False)
    report = comp.run_compare(
        legacy, raw, "000001.SZ", "A股", "daily", "raw", "tdx"
    )
    assert report.consistent
    assert report.diffs == []
    assert not comp.enabled
