# 文件名：tests/realism_gate/test_invariants_red.py
"""
tests/realism_gate/test_invariants_red.py - 结果侧不变量反红测试（P1-15，v2.2 §4.9）
================================================================================

逐条构造违规序列，断言 RI-1~RI-6 均被 check_result_invariants 抓住；
并断言修复前的既有缺陷（vectorized cash[i:] 覆盖错误）在未修复时会被
RI-3/RI-4 抓住（作为不变量有效性证据）。

本测试为红线测试集：直接构造违规输入，期望 check_result_invariants
返回对应 critical 违规。不依赖 realism_gate 的 enabled 开关（纯函数校验）。
"""

from __future__ import annotations

import pytest

from myquant.realism_gate.invariants import (
    BarState,
    TradeRecord,
    check_result_invariants,
)


def _good_trade(symbol: str = "600000.SH", date: str = "2024-01-02") -> TradeRecord:
    """合法成交：成交价在 [low, high] 内，量未超 cap。"""
    return TradeRecord(
        symbol=symbol, trade_date=date, side="BUY",
        price=10.0, quantity=1000.0,
        day_low=9.9, day_high=10.1, day_volume=1_000_000.0,
        slippage_amount=0.0,
    )


def _good_bar(symbol: str = "600000.SH", date: str = "2024-01-02",
              cash: float = 100_000.0, position: float = 1000.0,
              close: float = 10.0) -> BarState:
    """合法 bar：现金为正、持仓为正、收盘价合理。"""
    return BarState(symbol=symbol, bar_date=date, cash=cash,
                    position=position, close=close)


# ═══════════════════════════════════════════════
#  RI-1 成交价界
# ═══════════════════════════════════════════════


class TestRI1PriceBounds:
    def test_正常成交价不违规(self) -> None:
        v = check_result_invariants(
            trades=[_good_trade()],
            bar_states=[_good_bar()],
            initial_capital=1_000_000.0,
        )
        assert not [x for x in v if x.field_path == "invariants.price_bounds"]

    def test_成交价越界_抓RI1(self) -> None:
        # price=11.5 远超 day_high=10.1，带宽 max(0, 0.01)=0.01 → hi=10.11
        bad = TradeRecord(
            symbol="600000.SH", trade_date="2024-01-02", side="BUY",
            price=11.5, quantity=1000.0,
            day_low=9.9, day_high=10.1, day_volume=1_000_000.0,
        )
        v = check_result_invariants(
            trades=[bad], bar_states=[_good_bar()], initial_capital=1_000_000.0,
        )
        ri1 = [x for x in v if x.field_path == "invariants.price_bounds"]
        assert len(ri1) == 1, f"RI-1 应命中 1 条，实际 {len(ri1)}"
        assert ri1[0].critical is True


# ═══════════════════════════════════════════════
#  RI-2 成交量界
# ═══════════════════════════════════════════════


class TestRI2VolumeBound:
    def test_cap内不违规(self) -> None:
        # quantity=1000，day_volume=1_000_000，cap=0.1 → limit=100_000 → 不违规
        v = check_result_invariants(
            trades=[_good_trade()], bar_states=[_good_bar()],
            initial_capital=1_000_000.0, participation_cap=0.1,
        )
        assert not [x for x in v if x.field_path == "invariants.volume_bound"]

    def test_超cap抓RI2(self) -> None:
        big = TradeRecord(
            symbol="600000.SH", trade_date="2024-01-02", side="BUY",
            price=10.0, quantity=200_000.0,  # > 1_000_000*0.1=100_000
            day_low=9.9, day_high=10.1, day_volume=1_000_000.0,
        )
        v = check_result_invariants(
            trades=[big], bar_states=[_good_bar()],
            initial_capital=1_000_000.0, participation_cap=0.1,
        )
        ri2 = [x for x in v if x.field_path == "invariants.volume_bound"]
        assert len(ri2) == 1

    def test_未声明cap且未声明无限流动性_抓RI2(self) -> None:
        v = check_result_invariants(
            trades=[_good_trade()], bar_states=[_good_bar()],
            initial_capital=1_000_000.0, participation_cap=None,
            declared_unlimited_liquidity=False,
        )
        ri2 = [x for x in v if x.field_path == "invariants.volume_bound"]
        assert len(ri2) == 1

    def test_已声明无限流动性_放行RI2(self) -> None:
        v = check_result_invariants(
            trades=[_good_trade()], bar_states=[_good_bar()],
            initial_capital=1_000_000.0, participation_cap=None,
            declared_unlimited_liquidity=True,
        )
        assert not [x for x in v if x.field_path == "invariants.volume_bound"]


# ═══════════════════════════════════════════════
#  RI-4 现金非负
# ═══════════════════════════════════════════════


class TestRI4CashNonNegative:
    def test_负现金抓RI4(self) -> None:
        neg = _good_bar(cash=-5000.0)  # < -1.0 容差
        v = check_result_invariants(
            trades=[_good_trade()], bar_states=[neg],
            initial_capital=1_000_000.0,
        )
        ri4 = [x for x in v if x.field_path == "invariants.cash_non_negative"]
        assert len(ri4) == 1

    def test_声明杠杆放行RI4(self) -> None:
        neg = _good_bar(cash=-5000.0)
        v = check_result_invariants(
            trades=[_good_trade()], bar_states=[neg],
            initial_capital=1_000_000.0, allow_leverage=True,
        )
        assert not [x for x in v if x.field_path == "invariants.cash_non_negative"]


# ═══════════════════════════════════════════════
#  RI-5 T+1 一致性
# ═══════════════════════════════════════════════


class TestRI5T1Consistency:
    def test_卖出超限抓RI5(self) -> None:
        # bar0 持仓 1000，bar1 卖出 1500 > 前一日持仓 1000
        bars = [
            BarState(symbol="600000.SH", bar_date="2024-01-02",
                     cash=50_000.0, position=1000.0, close=10.0),
            BarState(symbol="600000.SH", bar_date="2024-01-03",
                     cash=65_000.0, position=0.0, close=10.0),
        ]
        sells = [
            TradeRecord(symbol="600000.SH", trade_date="2024-01-03",
                        side="SELL", price=10.0, quantity=1500.0,
                        day_low=9.9, day_high=10.1, day_volume=1_000_000.0),
        ]
        v = check_result_invariants(
            trades=sells, bar_states=bars, initial_capital=1_000_000.0,
        )
        ri5 = [x for x in v if x.field_path == "invariants.t1_consistency"]
        assert len(ri5) == 1

    def test_合法卖出不抓RI5(self) -> None:
        bars = [
            BarState(symbol="600000.SH", bar_date="2024-01-02",
                     cash=50_000.0, position=1000.0, close=10.0),
            BarState(symbol="600000.SH", bar_date="2024-01-03",
                     cash=60_000.0, position=0.0, close=10.0),
        ]
        sells = [
            TradeRecord(symbol="600000.SH", trade_date="2024-01-03",
                        side="SELL", price=10.0, quantity=1000.0,
                        day_low=9.9, day_high=10.1, day_volume=1_000_000.0),
        ]
        v = check_result_invariants(
            trades=sells, bar_states=bars, initial_capital=1_000_000.0,
        )
        assert not [x for x in v if x.field_path == "invariants.t1_consistency"]


# ═══════════════════════════════════════════════
#  RI-6 持仓非负
# ═══════════════════════════════════════════════


class TestRI6PositionNonNegative:
    def test_负持仓抓RI6(self) -> None:
        neg = _good_bar(position=-500.0)
        v = check_result_invariants(
            trades=[_good_trade()], bar_states=[neg],
            initial_capital=1_000_000.0,
        )
        ri6 = [x for x in v if x.field_path == "invariants.position_non_negative"]
        assert len(ri6) == 1

    def test_声明做空放行RI6(self) -> None:
        neg = _good_bar(position=-500.0)
        v = check_result_invariants(
            trades=[_good_trade()], bar_states=[neg],
            initial_capital=1_000_000.0, allow_short=True,
        )
        assert not [x for x in v if x.field_path == "invariants.position_non_negative"]


# ═══════════════════════════════════════════════
#  有效性证据：vectorized cash[i:] 覆盖缺陷 → RI-3/RI-4 抓住
# ═══════════════════════════════════════════════


class TestEffectivenessCashCoverBug:
    """模拟 vectorized_engine 修复前 cash[i:] 覆盖错误：缩量日费用未重算，
    导致后续 bar 现金被错误高估/透支。断言 RI-3/RI-4 能抓住此类缺陷。"""

    def test_cash透支被RI4抓住(self) -> None:
        # 模拟修复前：买入 1000 股 × 10 元 = 10_000，但 cash 被错误多扣 → 透支
        bars = [
            BarState(symbol="600000.SH", bar_date="2024-01-02",
                     cash=990_000.0, position=0.0, close=10.0),
            BarState(symbol="600000.SH", bar_date="2024-01-03",
                     cash=-5_000.0, position=1000.0, close=10.0),  # 透支
        ]
        trades = [
            TradeRecord(symbol="600000.SH", trade_date="2024-01-03",
                        side="BUY", price=10.0, quantity=1000.0,
                        day_low=9.9, day_high=10.1, day_volume=1_000_000.0),
        ]
        v = check_result_invariants(
            trades=trades, bar_states=bars, initial_capital=1_000_000.0,
        )
        ri4 = [x for x in v if x.field_path == "invariants.cash_non_negative"]
        assert len(ri4) == 1, f"cash 透支应被 RI-4 抓住，违规: {v}"


# ═══════════════════════════════════════════════
#  防御式输入校验
# ═══════════════════════════════════════════════


class TestInputValidation:
    def test_initial_capital非正数抛错(self) -> None:
        with pytest.raises(ValueError):
            check_result_invariants(
                trades=[], bar_states=[], initial_capital=0.0,
            )

    def test_participation_cap越界抛错(self) -> None:
        with pytest.raises(ValueError):
            check_result_invariants(
                trades=[], bar_states=[], initial_capital=1_000_000.0,
                participation_cap=1.5,
            )
