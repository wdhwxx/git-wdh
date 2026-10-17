# 文件名：tests/unit/test_admission.py
"""B-04 门禁①准入单测。

覆盖：
  1. BLOCK：缺字段 / 类型不可转 / high<low / 负价 / 空 symbol / 时间戳无效；
  2. FLAG：价格跳变（分品种阈值，北交所 30% 不误杀）/ 量异动 / 源切换；
  3. PASS：合法行放行；
  4. 白名单：生效窗内放行、窗外失效、disabled 不生效。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from myquant.data_access.admission import (
    _DEFAULT_MAX_PRICE_JUMP_PCT,
    AdmissionChecker,
    AdmissionResult,
)


@pytest.fixture
def checker(tmp_path: Path) -> AdmissionChecker:
    """默认阈值判定器（不依赖真实 config，走默认值兜底）。"""
    return AdmissionChecker(
        config_path=tmp_path / "no_data_clean.yaml",
        whitelist_path=tmp_path / "no_whitelist.yaml",
    )


def _row(**overrides) -> dict:
    """构造合法行情行。"""
    row = {
        "symbol": "000001.SZ",
        "trade_date": "2026-10-15",
        "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
        "volume": 100000.0, "data_source": "tdx",
    }
    row.update(overrides)
    return row


# ── BLOCK 类 ─────────────────────────────────────────
def test_block_missing_field(checker: AdmissionChecker) -> None:
    """必需字段缺失 → BLOCK。"""
    res = checker.check(_row(close=None), "000001.SZ", "tdx")
    assert res.decision == "BLOCK"
    assert any("missing_field:close" in r for r in res.reasons)


def test_block_unconvertible(checker: AdmissionChecker) -> None:
    """类型不可转（非数值）→ BLOCK。"""
    res = checker.check(_row(open="abc"), "000001.SZ", "tdx")
    assert res.decision == "BLOCK"
    assert any("unconvertible:open" in r for r in res.reasons)


def test_block_high_low_conflict(checker: AdmissionChecker) -> None:
    """high < low → BLOCK。"""
    res = checker.check(_row(high=9.8, low=10.2), "000001.SZ", "tdx")
    assert res.decision == "BLOCK"
    assert any("high_low_conflict" in r for r in res.reasons)


def test_block_negative_price(checker: AdmissionChecker) -> None:
    """负价 → BLOCK。"""
    res = checker.check(_row(close=-1.0), "000001.SZ", "tdx")
    assert res.decision == "BLOCK"
    assert any("negative_price:close" in r for r in res.reasons)


def test_block_empty_symbol(checker: AdmissionChecker) -> None:
    """空 symbol → BLOCK。"""
    res = checker.check(_row(), "", "tdx")
    assert res.decision == "BLOCK"
    assert "empty_symbol" in res.reasons


def test_block_invalid_timestamp(checker: AdmissionChecker) -> None:
    """时间戳无效 → BLOCK。"""
    res = checker.check(_row(trade_date="2026/10/15"), "000001.SZ", "tdx")
    assert res.decision == "BLOCK"
    assert any("invalid_timestamp" in r for r in res.reasons)


# ── PASS 类 ─────────────────────────────────────────
def test_pass_valid_row(checker: AdmissionChecker) -> None:
    """合法行 → PASS（无 prev 上下文）。"""
    res = checker.check(_row(), "000001.SZ", "tdx")
    assert res.decision == "PASS"
    assert res.passed


# ── FLAG 类 ─────────────────────────────────────────
def test_flag_price_jump_exceeds_default(checker: AdmissionChecker) -> None:
    """价格跳变超默认阈值（无分品种配置时 0.22）→ FLAG。"""
    prev = _row(close=10.0)
    res = checker.check(_row(close=13.0), "000001.SZ", "tdx", prev_row=prev)
    assert res.decision == "FLAG"
    assert any("price_jump" in f for f in res.flags)


def test_flag_not_triggered_within_threshold(checker: AdmissionChecker) -> None:
    """跳变在阈值内 → PASS（无标记）。"""
    prev = _row(close=10.0)
    res = checker.check(_row(close=10.5), "000001.SZ", "tdx", prev_row=prev)
    assert res.decision == "PASS"


def test_flag_volume_anomaly(checker: AdmissionChecker) -> None:
    """量异动（>100 倍）→ FLAG。"""
    prev = _row(volume=1000.0)
    res = checker.check(_row(volume=500000.0), "000001.SZ", "tdx", prev_row=prev)
    assert res.decision == "FLAG"
    assert any("volume_anomaly" in f for f in res.flags)


def test_flag_source_switch(checker: AdmissionChecker) -> None:
    """源切换 → FLAG。"""
    prev = _row(data_source="tdx")
    res = checker.check(_row(data_source="akshare"), "000001.SZ", "akshare",
                        prev_row=prev)
    assert res.decision == "FLAG"
    assert any("source_switch" in f for f in res.flags)


# ── 分品种阈值 ─────────────────────────────────────────
def test_price_jump_threshold_by_board(tmp_path: Path) -> None:
    """北交所 30% 涨停（0.25 跳变）不误杀；主板 0.25 跳变触发 FLAG。"""
    cfg = tmp_path / "data_clean.yaml"
    cfg.write_text(
        "bar:\n"
        "  max_price_jump_pct: 0.22\n"
        "  max_price_jump_pct_by_market:\n"
        "    主板: 0.11\n"
        "    创业板: 0.21\n"
        "    科创板: 0.21\n"
        "    北交所: 0.31\n"
        "  max_price_jump_pct_by_symbol: {}\n",
        encoding="utf-8",
    )
    c = AdmissionChecker(config_path=cfg, whitelist_path=tmp_path / "w.yaml")
    prev = _row(close=10.0)
    # 北交所（8 开头）：+25% < 31% 阈值 → PASS
    res_bj = c.check(_row(symbol="830001.BJ", close=12.5), "830001.BJ", "tdx",
                     prev_row=prev)
    assert res_bj.decision == "PASS"
    # 主板：+25% > 11% 阈值 → FLAG
    res_main = c.check(_row(close=12.5), "000001.SZ", "tdx", prev_row=prev)
    assert res_main.decision == "FLAG"


def test_price_jump_threshold_by_symbol(tmp_path: Path) -> None:
    """by_symbol 单标的覆盖（0.10 更严）→ +15% 触发 FLAG。"""
    cfg = tmp_path / "data_clean.yaml"
    cfg.write_text(
        "bar:\n"
        "  max_price_jump_pct: 0.22\n"
        "  max_price_jump_pct_by_market:\n"
        "    主板: 0.11\n"
        "  max_price_jump_pct_by_symbol:\n"
        "    000001.SZ: 0.10\n",
        encoding="utf-8",
    )
    c = AdmissionChecker(config_path=cfg, whitelist_path=tmp_path / "w.yaml")
    prev = _row(close=10.0)
    res = c.check(_row(close=11.5), "000001.SZ", "tdx", prev_row=prev)
    assert res.decision == "FLAG"
    assert any("price_jump" in f for f in res.flags)


# ── 白名单 ─────────────────────────────────────────
def test_whitelist_disabled_by_default(tmp_path: Path) -> None:
    """enabled=false 白名单不生效（默认安全态）。"""
    wl = tmp_path / "wl.yaml"
    wl.write_text(
        "enabled: false\n"
        "entries:\n"
        "  - symbol: '000001.SZ'\n"
        "    rule: ohlc\n"
        "    valid_from: '2020-01-01'\n"
        "    valid_until: '2030-12-31'\n"
        "    approved_by: owner\n",
        encoding="utf-8",
    )
    c = AdmissionChecker(
        config_path=tmp_path / "no.yaml", whitelist_path=wl
    )
    # 缺 close → BLOCK（白名单 disabled 不拦截 BLOCK 判定）
    res = c.check(_row(close=None), "000001.SZ", "tdx")
    assert res.decision == "BLOCK"


def test_whitelist_in_window_passes(tmp_path: Path) -> None:
    """生效窗内命中 → 直接 PASS（跳过 BLOCK 规则）。"""
    wl = tmp_path / "wl.yaml"
    wl.write_text(
        "enabled: true\n"
        "entries:\n"
        "  - symbol: '000001.SZ'\n"
        "    rule: ohlc\n"
        "    valid_from: '2020-01-01'\n"
        "    valid_until: '2030-12-31'\n"
        "    approved_by: owner\n",
        encoding="utf-8",
    )
    c = AdmissionChecker(config_path=tmp_path / "no.yaml", whitelist_path=wl)
    res = c.check(_row(close=None), "000001.SZ", "tdx")  # 缺字段但白名单放行
    assert res.decision == "PASS"
    assert "whitelisted" in res.reasons


def test_whitelist_out_of_window_fails(tmp_path: Path) -> None:
    """窗外（valid_until 已过）→ 白名单失效，BLOCK 生效。"""
    wl = tmp_path / "wl.yaml"
    wl.write_text(
        "enabled: true\n"
        "entries:\n"
        "  - symbol: '000001.SZ'\n"
        "    rule: ohlc\n"
        "    valid_from: '2020-01-01'\n"
        "    valid_until: '2020-12-31'\n"
        "    approved_by: owner\n",
        encoding="utf-8",
    )
    c = AdmissionChecker(config_path=tmp_path / "no.yaml", whitelist_path=wl)
    res = c.check(_row(close=None), "000001.SZ", "tdx")
    assert res.decision == "BLOCK"


def test_result_passed_semantics() -> None:
    """FLAG 放行、BLOCK 拒收。"""
    assert AdmissionResult(decision="PASS").passed
    assert AdmissionResult(decision="FLAG", flags=["x"]).passed
    assert not AdmissionResult(decision="BLOCK", reasons=["x"]).passed


def test_default_threshold_constant() -> None:
    """默认阈值常量可读（兼容现有单一 0.22 口径）。"""
    assert _DEFAULT_MAX_PRICE_JUMP_PCT == 0.22
