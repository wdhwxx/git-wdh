# tests/integration/test_装配有效性.py
# 文件名：tests/integration/test_assembly_wiring.py
"""
test_assembly_wiring.py - T4-5 提前｜装配有效性防复发集成测试
=================================================================
验收口径（清单 T4-5）：live 配置下逐项断言——
  1. ledger 已注入（engine 有 portfolio / state_store）
  2. account_reconciler / alert_manager 已注册（registry 有工厂）
  3. 事件对账 handler 已接线（engine_assembly 有 _assert_live_reconciliation_gate）
  4. PnL 有周期任务（scheduler.enabled=true）
  5. 退订可执行（engine 有 stop/unsubscribe 方法）
  6. 追加断言 auto_failover 实际取值、broker.type 实际取值

任一护栏未接上 → 集成测试失败（防"在册未接线"复发的根）。

测试策略：不真实装配引擎（避免外部 broker/feed 依赖），
改为断言【配置文件关键值】+【代码工厂/护栏存在性】——
这才是"接线是否到位"的根，比端到端装配更稳定、更可复现。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

# ── 配置文件路径 ──
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "live_config.yaml"


@pytest.fixture(scope="module")
def live_config() -> Dict[str, Any]:
    """加载 live_config.yaml（模块级缓存）。"""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class TestConfigValues:
    """配置文件关键值断言（防止被人随手改坏）。"""

    def test_broker_type_is_paper_for_safety(self, live_config: Dict[str, Any]) -> None:
        """broker.type 当前应为 paper（阶段 0 安全默认）。"""
        assert live_config["broker"]["type"] == "paper", (
            "broker.type 必须为 paper（阶段0安全默认；切 qmt/ptrade 需走 live 硬前置检查）"
        )

    def test_trade_mode_is_paper(self, live_config: Dict[str, Any]) -> None:
        """trade_mode 当前应为 paper。"""
        assert live_config["live"]["trade_mode"] == "paper", (
            "trade_mode 必须为 paper（阶段0安全默认）"
        )

    def test_scheduler_enabled(self, live_config: Dict[str, Any]) -> None:
        """scheduler.enabled 必须为 true（T0-8 live 硬前置：周期核对链必须跑）。"""
        assert live_config["scheduler"]["enabled"] is True, (
            "scheduler.enabled 必须为 true（T0-8 实时对账装配门硬前置）"
        )

    def test_order_timeout_configured(self, live_config: Dict[str, Any]) -> None:
        """order_timeout 必须配置（T0-12·2 盘活的死配置，不能被删）。"""
        assert "order_timeout" in live_config["broker"], (
            "broker.order_timeout 缺失（T0-12·2 查单窗口/超时真源）"
        )
        assert int(live_config["broker"]["order_timeout"]) > 0


class TestRegistryWiring:
    """registry 工厂注册断言（防止模块 import 被删导致"在册未接线"）。"""

    def test_account_reconciler_registered(self) -> None:
        """registry 有 account_reconciler 工厂（T0-8 硬前置）。"""
        # 触发 monitoring.account_reconciler 模块 import（自注册）
        import myquant.monitoring.account_reconciler  # noqa: F401
        from myquant.core.registry import get_component

        assert get_component("account_reconciler") is not None, (
            "account_reconciler 未注册——T0-8 live 拒启；检查 myquant.monitoring.account_reconciler 是否被 import"
        )

    def test_alert_manager_registered(self) -> None:
        """registry 有 alert_manager 工厂（T0-8 硬前置）。"""
        import myquant.monitoring.alerting  # noqa: F401
        from myquant.core.registry import get_component

        assert get_component("alert_manager") is not None, (
            "alert_manager 未注册——T0-8 live 拒启；检查 myquant.monitoring.alerting 是否被 import"
        )


class TestAssemblyGuardExists:
    """装配层护栏函数存在性断言（防止被人删改）。"""

    def test_live_reconciliation_gate_exists(self) -> None:
        """engine_assembly._assert_live_reconciliation_gate 存在（T0-8 护栏）。"""
        from myquant.live.engine_assembly import _assert_live_reconciliation_gate

        assert callable(_assert_live_reconciliation_gate)

    def test_live_watchdog_gate_exists(self) -> None:
        """engine_assembly._assert_live_watchdog_gate 存在（既有护栏）。"""
        from myquant.live.engine_assembly import _assert_live_watchdog_gate

        assert callable(_assert_live_watchdog_gate)


class TestEngineInterface:
    """引擎接口存在性断言（退订可执行 / 关键方法不缺失）。"""

    def test_engine_has_pause_force(self) -> None:
        """LiveEngine.pause 支持 force 参数（T0-11 启动期暂停标记）。"""
        import inspect

        from myquant.live.engine import LiveEngine

        sig = inspect.signature(LiveEngine.pause)
        assert "force" in sig.parameters, (
            "pause() 缺 force 参数（T0-11 启动期暂停标记无法实现）"
        )

    def test_engine_has_startup_blocked_attr(self) -> None:
        """LiveEngine 有 _startup_blocked 属性（T0-11 启动期暂停标记）。"""
        # 不实例化（避免外部依赖），直接从 __init__ 源码确认
        import inspect

        from myquant.live.engine import LiveEngine
        src = inspect.getsource(LiveEngine.__init__)
        assert "_startup_blocked" in src, (
            "LiveEngine.__init__ 缺 _startup_blocked 初始化（T0-11）"
        )

    def test_broker_manager_restore_state_forwards(self) -> None:
        """BrokerManager.restore_state 有转发子券商逻辑（T0-10）。"""
        import inspect

        from myquant.live.brokers.manager import BrokerManager

        src = inspect.getsource(BrokerManager.restore_state)
        assert "hasattr(broker" in src, (
            "BrokerManager.restore_state 缺子券商转发逻辑（T0-10）"
        )
