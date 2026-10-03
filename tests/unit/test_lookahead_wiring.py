# -*- coding: utf-8 -*-
"""
防未来函数校验接线回归测试（审计 H2）
======================================
覆盖:
  1. MixedBacktestEngine 默认开启 guard，run 后信号/成交已记录且无违规
  2. VectorizedBacktestEngine 默认开启 guard，run 后信号/成交已记录且无违规
  3. enable_lookahead_guard=False 可显式关闭
  4. BacktestScheduler 创建引擎时注入全局 guard 单例
  5. 违规暴露路径：guard 有违规时结果 extra 写入 lookahead_guard_passed=False

回归依据: 审计问题 H2（mixed/vectorized 引擎未接防未来校验）
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.backtest.engine import ENGINE_MODE_MIXED, BacktestScheduler  # noqa: E402
from myquant.backtest.mixed_engine import MixedBacktestEngine  # noqa: E402
from myquant.backtest.vectorized_engine import VectorizedBacktestEngine  # noqa: E402
from myquant.core.lookahead_guard import (  # noqa: E402
    LookaheadGuard,
    get_lookahead_guard,
)
from myquant.models.backtest import BacktestConfig  # noqa: E402
from tests.unit.test_backtest_consistency_v5 import (  # noqa: E402
    _make_config,
    _make_strategy,
    make_demo_data,
)


@pytest.mark.lookahead
class TestMixedWiring:
    """H2: MixedBacktestEngine 接线"""

    def test_默认开启且run记录(self) -> None:
        guard = LookaheadGuard()
        eng = MixedBacktestEngine(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=40),
            lookahead_guard=guard,
        )
        result = eng.run()
        stats = guard.stats
        assert stats["signals_recorded"] > 0
        assert stats["trades_recorded"] > 0
        assert stats["violations"] == 0
        assert result.extra.get("lookahead_guard_passed") is None  # 无违规不写标记

    def test_可显式关闭(self) -> None:
        eng = MixedBacktestEngine(
            config=BacktestConfig(),
            enable_lookahead_guard=False,
        )
        assert eng._lookahead_guard is None

    def test_默认开启(self) -> None:
        eng = MixedBacktestEngine(config=BacktestConfig())
        assert eng._lookahead_guard is not None

    def test_违规写入结果extra(self) -> None:
        """预置违规（T 日信号 T 日成交），run 后 extra 标记未通过且含违规明细"""
        guard = LookaheadGuard()
        guard.record_trade(
            symbol="000001.SZ", trade_date="2023-01-02",
            signal_date="2023-01-02", price_type="OPEN", side="BUY",
        )
        eng = MixedBacktestEngine(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=40),
            lookahead_guard=guard,
        )
        result = eng.run()
        # run 开始 reset 会清空预置违规——用 reset 后的独立构造验证暴露路径：
        # 直接调用 _run_lookahead_assert 前先注入违规
        guard.reset()
        guard.record_trade(
            symbol="000001.SZ", trade_date="2023-01-02",
            signal_date="2023-01-02", price_type="OPEN", side="BUY",
        )
        passed = eng._run_lookahead_assert()
        assert passed is False
        assert result.extra.get("lookahead_guard_passed") is False
        assert result.extra.get("lookahead_guard_violations")
        # P0-01回归：违规回测必须标记失败，禁止 status=completed / is_success=True
        assert result.status == "failed"
        assert result.is_success is False
        assert "防未来函数校验未通过" in result.error_message


@pytest.mark.lookahead
class TestVectorizedWiring:
    """H2: VectorizedBacktestEngine 接线"""

    def test_默认开启且run记录(self) -> None:
        guard = LookaheadGuard()
        eng = VectorizedBacktestEngine(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=40),
            lookahead_guard=guard,
        )
        result = eng.run()
        stats = guard.stats
        assert stats["signals_recorded"] > 0
        assert stats["trades_recorded"] > 0
        assert stats["violations"] == 0
        assert result.extra.get("lookahead_guard_passed") is None

    def test_可显式关闭(self) -> None:
        eng = VectorizedBacktestEngine(
            config=BacktestConfig(),
            enable_lookahead_guard=False,
        )
        assert eng._lookahead_guard is None

    def test_默认开启(self) -> None:
        eng = VectorizedBacktestEngine(config=BacktestConfig())
        assert eng._lookahead_guard is not None

    def test_t_plus_1语义无违规(self) -> None:
        """向量化 position[t]=target[t-1] 的 T+1 语义应通过铁律3（成交日>信号日）"""
        guard = LookaheadGuard()
        eng = VectorizedBacktestEngine(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=30),
            lookahead_guard=guard,
        )
        eng.run()
        # 违规为 0 说明所有成交 trade_date > signal_date
        assert guard.stats["violations"] == 0


@pytest.mark.lookahead
class TestSchedulerWiring:
    """H2: BacktestScheduler 统一注入全局单例"""

    def test_scheduler注入单例(self) -> None:
        sched = BacktestScheduler(mode=ENGINE_MODE_MIXED)
        strategy = _make_strategy()
        eng = sched.create_engine(
            config=_make_config(),
            strategy=strategy,
            data=make_demo_data(n_bars=30),
        )
        assert eng._lookahead_guard is get_lookahead_guard()
