# tests/unit/test_live_reconciliation_gate.py
"""
test_live_reconciliation_gate.py - T0-8 LIVE 实时对账装配门测试
=================================================================
验收 FI-12/13：live 下 scheduler.enabled=false 或 account_reconciler/alert_manager
未注册 → ValueError 拒启；paper/sim 尊重配置原值（false/未注册可运行）。
"""
from __future__ import annotations

import pytest

from myquant.core.registry import _component_factories
from myquant.live.engine_assembly import _assert_live_reconciliation_gate


@pytest.fixture(autouse=True)
def _reset_registry():
    """每个用例前后复位组件注册表（隔离模块级状态）。"""
    saved = dict(_component_factories)
    yield
    _component_factories.clear()
    _component_factories.update(saved)


def _register_both():
    """模拟 account_reconciler + alert_manager 已注册。"""
    _component_factories["account_reconciler"] = lambda: object()
    _component_factories["alert_manager"] = lambda: object()


class TestLiveReconciliationGate:
    def test_live_scheduler_disabled_raises(self):
        """live + scheduler.enabled=false → ValueError 拒启。"""
        _register_both()
        with pytest.raises(ValueError, match="scheduler.enabled=false"):
            _assert_live_reconciliation_gate("live", {"enabled": False})

    def test_live_account_reconciler_unregistered_raises(self):
        """live + account_reconciler 未注册 → ValueError 拒启。"""
        _component_factories["alert_manager"] = lambda: object()
        with pytest.raises(ValueError, match="account_reconciler 未注册"):
            _assert_live_reconciliation_gate("live", {"enabled": True})

    def test_live_alert_manager_unregistered_raises(self):
        """live + alert_manager 未注册 → ValueError 拒启。"""
        _component_factories["account_reconciler"] = lambda: object()
        with pytest.raises(ValueError, match="alert_manager 未注册"):
            _assert_live_reconciliation_gate("live", {"enabled": True})

    def test_live_all_ok_passes(self):
        """live + 三项全满足 → 不抛。"""
        _register_both()
        _assert_live_reconciliation_gate("live", {"enabled": True})

    def test_paper_scheduler_disabled_passes(self):
        """paper + scheduler.enabled=false → 不抛（paper 尊重配置原值）。"""
        _assert_live_reconciliation_gate("paper", {"enabled": False})

    def test_sim_unregistered_passes(self):
        """sim + 组件未注册 → 不抛。"""
        _assert_live_reconciliation_gate("sim", {"enabled": True})

    def test_multiple_errors_combined(self):
        """多项缺失时错误信息合并。"""
        with pytest.raises(ValueError) as exc_info:
            _assert_live_reconciliation_gate("live", {"enabled": False})
        msg = str(exc_info.value)
        assert "scheduler.enabled=false" in msg
        assert "account_reconciler" in msg
        assert "alert_manager" in msg
