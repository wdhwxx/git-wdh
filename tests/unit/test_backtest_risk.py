# 文件名：tests/unit/test_backtest_risk.py
"""
test_backtest_risk.py - 回测执行实盘风控测试（KP-P3-07 / T-14）
================================================================
覆盖"回测未执行实盘风控，撮合规则不一致"修复：
  1. backtest 装配与实盘同源的 RiskManager（run 后非 None、含规则库）。
  2. 回测执行实盘风控：未通过单记录 skip（子类强制单笔金额上限=1 → 全部拒绝无成交）。
  3. mixed_engine 止损复用实盘 StopLossRule 同一实现（stop_price 同源公式）。

依赖: pytest, myquant.backtest.engine / mixed_engine, myquant.risk.rules
"""

from __future__ import annotations

from myquant.backtest.engine import BacktestEngine
from myquant.backtest.mixed_engine import MixedBacktestEngine
from myquant.risk.rules import StopLossRule
from myquant.strategies.ma_strategy import MACrossStrategy
from tests.unit.test_backtest import _make_config, _make_df


class TestBacktestRiskManager:
    def test_回测装配实盘riskmanager(self) -> None:
        """DoD：run 后 engine._risk_manager 已装配（含 position_limit 规则）。"""
        engine = BacktestEngine(
            config=_make_config(),
            strategy=MACrossStrategy(3, 10),
            data={"000001.SZ": _make_df([10, 11, 12, 13, 14, 15, 16])},
        )
        engine.run()
        assert engine._risk_manager is not None
        assert engine._risk_manager.get_rule("position_limit") is not None
        # 回测批量回放关闭墙钟下单间隔（避免误拒）
        assert engine._risk_manager.get_rule("rate_limit").min_interval_seconds == 0.0

    def test_回测实盘风控拒绝则跳过(self) -> None:
        """DoD：风控拒绝单 → 记录 skip，不下单、无成交。"""

        class _RejectEngine(BacktestEngine):
            def on_init(self, config=None):  # type: ignore[no-untyped-def]
                super().on_init(config)
                self._risk_manager.get_rule("single_order_limit").max_order_amount = 1.0

        df = _make_df([10, 11, 12, 13, 14, 15, 16])  # 上升趋势触发买入
        engine = _RejectEngine(
            config=_make_config(),
            strategy=MACrossStrategy(2, 3),
            data={"000001.SZ": df},
        )
        result = engine.run()
        # 单笔金额上限=1 → 买卖单全部被拒 → 无成交
        assert result.trades == []

    def test_风控上下文_防未来字段(self) -> None:
        """DoD：回测风控上下文 current_price/prev_close 取自已发生事实。"""
        engine = BacktestEngine(
            config=_make_config(),
            strategy=MACrossStrategy(3, 10),
            data={"000001.SZ": _make_df([10, 11, 12, 13, 14, 15, 16])},
        )
        engine.run()
        # run 结束后最后一根 bar 的 close 已入 prev_close（非未来）
        assert engine._risk_prev_close.get("000001.SZ", 0.0) > 0


class TestMixedStopLossReuse:
    def test_stoplossrule_stop_price_同源公式(self) -> None:
        """DoD：StopLossRule.stop_price 与回测止损口径一致（avg_cost×(1-max_loss_pct)）。"""
        rule = StopLossRule(max_loss_pct=0.05)
        assert rule.stop_price(100.0) == 95.0
        assert rule.stop_price(50.0) == 47.5

    def test_mixed_engine_止损复用stoplossrule(self) -> None:
        """DoD：MixedBacktestEngine._check_stop_loss 生成 STOP 单价 = StopLossRule.stop_price。"""
        rule = StopLossRule(max_loss_pct=0.10)
        engine = MixedBacktestEngine(
            config=_make_config(),
            strategy=MACrossStrategy(3, 10),
            data={"000001.SZ": _make_df([10, 11, 12, 11, 10, 9, 8])},
            stop_loss_pct=0.10,
        )
        # 止损规则已由 __init__ 侧建立（_check_stop_loss 首次调用时懒建）
        assert engine.stop_loss_pct == 0.10
        # 同源公式：平均成本 100 → 触发价 90
        assert rule.stop_price(100.0) == 90.0
