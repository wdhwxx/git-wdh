# -*- coding: utf-8 -*-
"""
KP-P2-11 防未来失败被 mark_complete 覆盖（T-03）
=================================================
覆盖:
  1. BacktestResult 终态保护：mark_failed 后 mark_complete 抛 RuntimeError（禁止覆盖失败态）
  2. MixedBacktestEngine._run_lookahead_assert 保留 light guard 返回值：
     轻量时间线校验违规 → 返回 False 且 result 已 mark_failed（不再无条件 return True）
  3. on_stop 端到端 DoD：light guard 违规时回测以 status="failed" 收尾，不覆盖为 completed

回归依据: 审计 KP-P2-11 —— mixed_engine._run_light_guard_check 返回值被丢弃后 return True，
on_stop 对 True 无条件 mark_complete，覆盖 mark_failed（models/backtest.py:445-447）。
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.backtest.mixed_engine import MixedBacktestEngine  # noqa: E402
from myquant.core.light_lookahead_guard import (  # noqa: E402
    get_light_lookahead_guard,
    reset_light_lookahead_guard,
)
from myquant.models.backtest import BacktestConfig, BacktestResult  # noqa: E402
from tests.unit.test_backtest_consistency_v5 import (  # noqa: E402
    _make_config,
    _make_strategy,
    make_demo_data,
)


@pytest.mark.lookahead
class TestMarkCompleteTerminalState:
    """models/backtest.py mark_complete 终态保护（KP-P2-11）"""

    def test_pending可直接mark_complete(self) -> None:
        result = BacktestResult(config=BacktestConfig())
        assert result.status == "pending"
        result.mark_complete()
        assert result.status == "completed"

    def test_mark_failed后mark_complete抛错(self) -> None:
        result = BacktestResult(config=BacktestConfig())
        result.mark_failed("防未来函数校验未通过")
        assert result.status == "failed"
        with pytest.raises(RuntimeError, match="failed 态禁止覆盖为 completed"):
            result.mark_complete()
        # 失败态为终态，未被覆盖为 completed
        assert result.status == "failed"

    def test_mark_complete后mark_failed_失败态生效(self) -> None:
        result = BacktestResult(config=BacktestConfig())
        result.mark_complete()
        assert result.status == "completed"
        result.mark_failed("后续发现违规")
        # 失败态是终态，可覆盖 completed
        assert result.status == "failed"


@pytest.mark.lookahead
class TestMixedLightGuardReturn:
    """mixed_engine._run_lookahead_assert 保留 light guard 返回值（KP-P2-11 核心缺陷）"""

    @pytest.fixture(autouse=True)
    def _ensure_light_guard(self) -> None:
        reset_light_lookahead_guard()
        lg = get_light_lookahead_guard()
        lg.enabled = True
        lg.strict = False

    def _engine(self, run: bool = True) -> MixedBacktestEngine:
        eng = MixedBacktestEngine(
            config=_make_config(),
            strategy=_make_strategy(),
            data=make_demo_data(n_bars=40),
        )
        if run:
            eng.run()
        return eng

    def _inject_regression(self, eng: MixedBacktestEngine) -> None:
        """把最新（最后）时间戳置于最早（最前）之前 → 触发轻量防线规则4时间线倒退。"""
        ts = list(eng._bar_timestamps)
        eng._bar_timestamps = [ts[-1], ts[0]] + ts[1:-1]

    def test_light_guard通过_返回True且completed(self) -> None:
        eng = self._engine()
        passed = eng._run_lookahead_assert()
        assert passed is True
        assert eng.result.status == "completed"

    def test_light_guard违规_返回False且结果failed(self) -> None:
        eng = self._engine()
        self._inject_regression(eng)
        passed = eng._run_lookahead_assert()
        assert passed is False
        assert eng.result.status == "failed"
        assert eng.result.is_success is False
        assert "轻量防未来防线校验未通过" in eng.result.error_message

    def test_light_guard违规_on_stop不覆盖failed(self) -> None:
        """端到端 DoD：light guard 违规时回测以 failed 收尾，而非被 mark_complete 覆盖。"""
        eng = self._engine(run=False)
        eng._build_unified_bars()
        self._inject_regression(eng)
        eng.on_stop()
        assert eng.result.status == "failed"
        assert eng.result.is_success is False
        assert "轻量防未来防线校验未通过" in eng.result.error_message
