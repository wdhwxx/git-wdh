# 文件名：tests/unit/test_tradability_fail_closed.py
"""B-06 停牌过滤接线 + rules_skipped 结构化告警单测。

覆盖：
  1. 默认 fail-open 行为不变（列缺失 → 跳过 + 字符串摘要，不拒单）；
  2. fail_closed_on_skipped=True + 列缺失 → 整列拒单（total_rejected 计入）；
  3. rules_skipped_detail 结构化三元组（rule/code/reason）；
  4. 接线路径：回测配置读 suspended_symbols → make_suspended_lookup → SimBroker 拒单。
"""

from __future__ import annotations

import pandas as pd
import pytest

from myquant.execution.sim_broker import SimBroker, make_suspended_lookup
from myquant.indicators.tradability import (
    COL_CLOSE,
    COL_HIGH,
    COL_LOW,
    COL_OPEN,
    TradabilityConfig,
    filter_tradable,
)


def _df(suspend_col: bool = True) -> pd.DataFrame:
    """构造 DataFrame（可选停牌标记列，模拟 processed 未纳入 is_suspended）。"""
    data = {
        COL_OPEN: [10.0, 10.2],
        COL_HIGH: [10.5, 10.6],
        COL_LOW: [9.8, 10.0],
        COL_CLOSE: [10.2, 10.4],
        "成交额": [1e7, 9e6],
        "涨跌幅": [2.0, 1.96],
    }
    if suspend_col:
        data["is_suspended"] = [0, 0]
    return pd.DataFrame(data)


# ── 默认 fail-open ─────────────────────────────────────────
def test_default_fail_open_skip_no_reject() -> None:
    """默认：停牌列缺失 → 跳过不拒（行为不变），字符串摘要保留。"""
    df = _df(suspend_col=False)
    mask, stats = filter_tradable(df, TradabilityConfig())
    assert mask.all()  # 无拒
    assert stats["total_rejected"] == 0
    assert "suspend(列缺失，跳过)" in stats["rules_skipped"]


# ── fail-closed ─────────────────────────────────────────
def test_fail_closed_column_missing_rejects_all() -> None:
    """fail_closed_on_skipped=True + 停牌列缺失 → 整列拒单。"""
    df = _df(suspend_col=False)
    cfg = TradabilityConfig(fail_closed_on_skipped=True)
    mask, stats = filter_tradable(df, cfg)
    assert not mask.any()  # 全部不可交易
    assert stats["total_rejected"] == len(df)
    assert stats["suspended_rejected"] == len(df)


def test_fail_closed_rule_off_rejects_all() -> None:
    """fail-closed + 停牌规则关闭 → 整列拒单。"""
    df = _df(suspend_col=True)
    cfg = TradabilityConfig(filter_suspend=False, fail_closed_on_skipped=True)
    mask, stats = filter_tradable(df, cfg)
    assert not mask.any()
    assert stats["total_rejected"] == len(df)


# ── 结构化告警 ─────────────────────────────────────────
def test_rules_skipped_detail_structured() -> None:
    """rules_skipped_detail 为 rule/code/reason 三元组。"""
    df = _df(suspend_col=False)
    _, stats = filter_tradable(df, TradabilityConfig())
    detail = stats["rules_skipped_detail"]
    assert detail, "应有结构化明细"
    entry = detail[0]
    assert entry["rule"] == "suspend"
    assert entry["code"] == "SUSPEND_COL_MISSING"
    assert entry["reason"] == "列缺失，跳过"


def test_rules_skipped_string_backward_compat() -> None:
    """字符串摘要保持既有格式（列缺失，跳过）。"""
    df = _df(suspend_col=False)
    _, stats = filter_tradable(df, TradabilityConfig())
    assert "suspend(列缺失，跳过)" in stats["rules_skipped"]


def test_liquidity_skip_structured() -> None:
    """流动性列缺失 → detail 三元组。"""
    df = _df(suspend_col=True).drop(columns=["成交额"])
    _, stats = filter_tradable(df, TradabilityConfig())
    assert any(
        e["rule"] == "liquidity" and e["code"] == "LIQUIDITY_COL_MISSING"
        for e in stats["rules_skipped_detail"]
    )


# ── 接线路径 ─────────────────────────────────────────
def test_make_suspended_lookup_wiring() -> None:
    """make_suspended_lookup → 停牌判定；空集/None 向后兼容。"""
    lookup = make_suspended_lookup({"000001.SZ"})
    assert lookup("000001.SZ")
    assert not lookup("600519.SH")
    # 空集/None → 不拦截（向后兼容）
    assert not make_suspended_lookup()("000001.SZ")
    assert not make_suspended_lookup(set())("000001.SZ")


def test_broker_rejects_suspended_symbol() -> None:
    """SimBroker 装配停牌 lookup：停牌股订单被拒（返回拒因 停牌）。"""
    lookup = make_suspended_lookup({"000001.SZ"})
    broker = SimBroker(
        initial_capital=1_000_000.0,
        enforce_limit=True,
        is_suspended=lookup,
    )
    from myquant.models.trade import Order, OrderSide

    order = Order(
        symbol="000001.SZ",
        side=OrderSide.BUY,
        quantity=100,
        price=10.0,
    )
    reason = broker._price_limit_check(order, bar=None)
    assert reason == "停牌"


def test_backtest_config_suspended_symbols_branch() -> None:
    """回测配置 suspended_symbols=list → lookup 生效（等价入口分支逻辑）。"""
    sus_raw = ["000001.SZ", "600519.SH"]
    sus = set(sus_raw) if isinstance(sus_raw, list) else None
    lookup = make_suspended_lookup(sus)
    assert lookup("000001.SZ") and lookup("600519.SH")
    assert not lookup("300750.SZ")
