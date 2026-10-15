# 文件名：tests/unit/test_risk_manager.py
"""
P3-4（v1.2 §6.4 ROI 4）：risk/risk_manager.py 覆盖率补测（56.3%→75%）

覆盖范围：
  1. RiskStatus / RiskEventType / RiskEvent（构造与 repr）
  2. DataQualityRiskConfig（默认/自定义 to_dict）
  3. SymbolDataQualityStatus（record_event / mark_paused / reset_pause / to_dict）
  4. RiskManager 构造（默认规则库 / 自定义 config / max_events）
  5. 状态机：get_status / get_status_reason / pause / resume / is_paused / can_open_position
  6. check_order：正常通过 / PAUSED 买入拒绝 / 数据质量暂停拒绝 / 规则拒绝（含事件记录）
  7. check_positions：止损 / 止盈 / 回撤自动暂停 / 非法持仓跳过
  8. 熔断与引擎暂停：pause_for_circuit_breaker / resume_from_circuit_breaker /
     pause_for_engine / resume_engine_holds / circuit_paused / engine_paused
  9. 事件总线：subscribe_to_event_bus / unsubscribe_from_event_bus
  10. 数据质量联动：handle_quality_alert_event（CRITICAL/ERROR 分级）/
      handle_pipeline_complete_event（bad_ratio 暂停 + 自动恢复）/
      handle_data_quality_event / 状态查询 / reset_data_quality_status
  11. 规则库管理：add / remove / get / enable / disable / rule_names / enabled_rules
  12. 异常交易：set_pending_orders / record_cancel_order / get_daily_report / reset_daily_counts
  13. 事件：get_events / get_recent_events / event_count / stats / get_summary / reset / repr
  14. build_risk_manager_from_config：默认 / anomaly_rules 子集 / 未知名报错
"""

from __future__ import annotations

from datetime import datetime

import pytest

from myquant.core.event_bus import (
    DataQualityEvent,
    PipelineCompleteEvent,
    QualityAlertEvent,
)
from myquant.models.trade import Order, OrderSide
from myquant.risk.risk_manager import (
    DataQualityRiskConfig,
    RiskEvent,
    RiskEventType,
    RiskManager,
    RiskStatus,
    SymbolDataQualityStatus,
    build_risk_manager_from_config,
)
from myquant.risk.rules import RiskContext


# 固定 rules.py 内部墙钟（2026-10-15 周四 10:30 盘中）→ 测试时间无关
# （交易时段规则 datetime.now() 依赖墙钟，CI 不同时区/时段运行会 REJECT，见 #398 CI windows 腿 5 fail）
@pytest.fixture(autouse=True)
def _freeze_rules_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FixedDateTime(datetime):  # type: ignore[misc]
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return cls(2026, 10, 15, 10, 30, 0)

    monkeypatch.setattr("myquant.risk.rules.datetime", _FixedDateTime)

# ─────────────────────────────────────────────
# 基础数据类
# ─────────────────────────────────────────────

class TestRiskEnumsAndEvent:
    def test_risk_status_members(self) -> None:
        assert RiskStatus.NORMAL.value == "NORMAL"
        assert RiskStatus.PAUSED.value == "PAUSED"

    def test_risk_event_type_members(self) -> None:
        assert RiskEventType.ORDER_REJECTED in RiskEventType
        assert RiskEventType.STATUS_CHANGE in RiskEventType

    def test_risk_event_repr(self) -> None:
        e = RiskEvent(
            event_type=RiskEventType.ORDER_REJECTED,
            rule_name="r",
            symbol="000001.SZ",
            reason="拒绝",
        )
        assert "order_rejected" in repr(e)


class TestDataQualityRiskConfig:
    def test_defaults(self) -> None:
        cfg = DataQualityRiskConfig()
        assert cfg.enabled is True

    def test_custom(self) -> None:
        cfg = DataQualityRiskConfig(
            enabled=True,
            auto_pause_on_critical=True,
            auto_pause_on_pipeline_critical=False,
            pause_bad_ratio_threshold=0.3,
            warning_bad_ratio_threshold=0.1,
            min_bad_count_for_pause=50,
            auto_resume_enabled=False,
            resume_bad_ratio_threshold=0.02,
            consecutive_good_required=3,
        )
        assert cfg.pause_bad_ratio_threshold == 0.3
        assert cfg.auto_pause_on_pipeline_critical is False
        assert cfg.min_bad_count_for_pause == 50
        assert cfg.consecutive_good_required == 3

    def test_to_dict(self) -> None:
        cfg = DataQualityRiskConfig()
        d = cfg.to_dict()
        assert d["enabled"] is True
        assert "pause_bad_ratio_threshold" in d
        assert "auto_resume_enabled" in d


class TestSymbolDataQualityStatus:
    def test_record_event_and_pause(self) -> None:
        st = SymbolDataQualityStatus(symbol="000001.SZ")
        st.record_event(
            severity="error", bad_ratio=0.5, bad_count=50, total_count=100,
            dirty_type="duplicate", grade="poor",
        )
        assert st.event_count == 1
        assert st.error_count == 1
        assert st.current_grade == "poor"
        st.mark_paused("质量告警")
        assert st.is_paused is True
        assert st.pause_reason == "质量告警"

    def test_reset_pause(self) -> None:
        st = SymbolDataQualityStatus(symbol="000001.SZ")
        st.mark_paused("x")
        st.reset_pause()
        assert st.is_paused is False

    def test_to_dict(self) -> None:
        st = SymbolDataQualityStatus(symbol="000001.SZ")
        d = st.to_dict()
        assert d["symbol"] == "000001.SZ"


# ─────────────────────────────────────────────
# 构造
# ─────────────────────────────────────────────

class TestRiskManagerInit:
    def test_default_construct(self) -> None:
        rm = RiskManager()
        assert rm.get_status() == RiskStatus.NORMAL
        assert len(rm.rule_names) >= 10  # 默认 10 类规则 + 数据质量

    def test_custom_config(self) -> None:
        rm = RiskManager(config={"rate_limit": {"max_orders_per_day": 5}})
        assert "rate_limit" in rm.rule_names

    def test_max_events_bound(self) -> None:
        rm = RiskManager(max_events=3)
        for i in range(5):
            rm.pause(f"reason-{i}")
            rm.resume()
        # 事件数不超过 max_events
        assert rm.event_count <= 3

    def test_repr(self) -> None:
        assert "RiskManager" in repr(RiskManager())


# ─────────────────────────────────────────────
# 状态机
# ─────────────────────────────────────────────

class TestStateMachine:
    def test_initial_state(self) -> None:
        rm = RiskManager()
        assert rm.get_status() == RiskStatus.NORMAL
        assert rm.get_status_reason() == ""
        assert rm.is_paused() is False
        assert rm.can_open_position() is True

    def test_pause_resume(self) -> None:
        rm = RiskManager()
        rm.pause("测试暂停")
        assert rm.get_status() == RiskStatus.PAUSED
        assert "测试暂停" in rm.get_status_reason()
        assert rm.is_paused() is True
        assert rm.can_open_position() is False
        rm.resume()
        assert rm.get_status() == RiskStatus.NORMAL
        assert rm.is_paused() is False

    def test_resume_clears_data_quality_pauses(self) -> None:
        rm = RiskManager()
        rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="critical", symbol="000001.SZ", bad_ratio=0.9)
        )
        assert rm.is_paused() is True
        rm.resume()
        assert rm.is_paused() is False
        assert rm.get_paused_symbols() == []


# ─────────────────────────────────────────────
# check_order
# ─────────────────────────────────────────────

def _buy_order(symbol: str = "000001.SZ", quantity: float = 100.0, price: float = 10.0) -> Order:
    return Order(symbol=symbol, side=OrderSide.BUY, quantity=quantity, price=price)


def _normal_context() -> RiskContext:
    return RiskContext(
        cash=100000.0,
        total_assets=100000.0,
        initial_capital=100000.0,
        peak_assets=100000.0,
        day_start_assets=100000.0,
        current_price={"000001.SZ": 10.0, "600519.SH": 100.0},
        extra={
            "prev_close": {"000001.SZ": 10.0, "600519.SH": 100.0},
            # 显式盘中时刻（交易时段规则支持注入，避免依赖墙钟）
            "current_time": "10:30:00",
        },
    )


class TestCheckOrder:
    def test_pass_increments_stats(self) -> None:
        rm = RiskManager()
        result = rm.check_order(_buy_order(), _normal_context())
        assert result.passed is True
        assert rm.stats["total_orders_checked"] == 1
        assert rm.stats["orders_passed"] == 1
        assert rm.stats["new_orders_today"] == 1

    def test_paused_buy_rejected(self) -> None:
        rm = RiskManager()
        rm.pause("全局暂停")
        result = rm.check_order(_buy_order(), _normal_context())
        assert result.passed is False
        assert result.rule_name == "risk_status_paused"
        assert rm.stats["orders_rejected"] == 1

    def test_paused_sell_allowed(self) -> None:
        rm = RiskManager()
        rm.pause("全局暂停")
        sell = Order(symbol="000001.SZ", side=OrderSide.SELL, quantity=100.0, price=10.0)
        result = rm.check_order(sell, _normal_context())
        assert result.passed is True  # 平仓/卖出不受 PAUSED 限制

    def test_rate_limit_rejected(self) -> None:
        rm = RiskManager(config={"rate_limit": {"max_orders_per_day": 1}})
        ctx = _normal_context()
        ctx.order_count_today = 1  # 已达上限
        result = rm.check_order(_buy_order(), ctx)
        assert result.passed is False
        assert result.rule_name == "rate_limit"

    def test_cash_insufficient_rejected(self) -> None:
        rm = RiskManager()
        ctx = _normal_context()
        ctx.cash = 0.0
        big = Order(symbol="000001.SZ", side=OrderSide.BUY, quantity=100000.0, price=100.0)
        result = rm.check_order(big, ctx)
        # 金额超限或现金不足至少一类规则拒绝
        assert result.passed is False

    def test_rejected_records_event(self) -> None:
        rm = RiskManager()
        rm.pause("暂停")
        rm.check_order(_buy_order(), _normal_context())
        assert rm.event_count >= 1
        # 按类型过滤断言（事件按 timestamp 倒序，同秒内排序不稳，不依赖 [0]）
        rejected = rm.get_events(event_type=RiskEventType.ORDER_REJECTED)
        assert rejected
        assert rejected[0].rule_name == "risk_status_paused"


# ─────────────────────────────────────────────
# check_positions
# ─────────────────────────────────────────────

class TestCheckPositions:
    def _ctx_with_pos(self, quantity: float, avg_cost: float, market: float) -> RiskContext:
        ctx = _normal_context()
        ctx.positions = [
            {"symbol": "000001.SZ", "quantity": quantity, "avg_cost": avg_cost,
             "market_price": market},
        ]
        return ctx

    def test_no_warnings_when_profitable(self) -> None:
        rm = RiskManager()
        warnings = rm.check_positions(self._ctx_with_pos(100.0, 10.0, 12.0))
        assert warnings == []

    def test_stop_loss_warning(self) -> None:
        rm = RiskManager()
        warnings = rm.check_positions(self._ctx_with_pos(100.0, 10.0, 8.0))
        assert len(warnings) >= 1
        assert warnings[0].passed is False

    def test_take_profit_warning(self) -> None:
        rm = RiskManager(config={
            "stop_loss": {"enabled": False, "max_loss_pct": 0.10},
            "take_profit": {"enabled": True, "max_profit_pct": 0.20},
        })
        warnings = rm.check_positions(self._ctx_with_pos(100.0, 10.0, 14.0))
        assert len(warnings) >= 1

    def test_invalid_position_skipped(self) -> None:
        rm = RiskManager()
        ctx = _normal_context()
        ctx.positions = [{"symbol": "X", "quantity": 0, "avg_cost": 0, "market_price": 0}]
        assert rm.check_positions(ctx) == []

    def test_drawdown_auto_pause(self) -> None:
        rm = RiskManager(config={"max_drawdown": {"max_drawdown_pct": 0.05}})
        ctx = _normal_context()
        ctx.peak_assets = 100000.0
        ctx.total_assets = 90000.0  # 回撤 10% > 5%
        ctx.positions = []
        rm.check_positions(ctx)
        assert rm.get_status() == RiskStatus.PAUSED

    def test_position_checks_stat(self) -> None:
        rm = RiskManager()
        rm.check_positions(_normal_context())
        assert rm.stats["position_checks"] == 1


# ─────────────────────────────────────────────
# 熔断 / 引擎暂停
# ─────────────────────────────────────────────

class TestCircuitBreakerAndEngine:
    def test_circuit_breaker(self) -> None:
        rm = RiskManager()
        assert rm.pause_for_circuit_breaker("熔断: 连续亏损") is True
        assert rm.circuit_paused is True
        assert rm.is_paused() is True
        assert rm.resume_from_circuit_breaker("复位") is True
        assert rm.circuit_paused is False

    def test_engine_holds(self) -> None:
        rm = RiskManager()
        assert rm.pause_for_engine("引擎停机") is True
        assert rm.engine_paused is True
        assert rm.is_paused() is True
        assert rm.resume_engine_holds("引擎恢复") is True
        assert rm.engine_paused is False

    def test_resume_clears_all(self) -> None:
        # 熔断/引擎各自可回滚复位（resume() 只清全局状态，不回滚独立标记）
        rm = RiskManager()
        rm.pause_for_circuit_breaker("x")
        rm.pause_for_engine("y")
        rm.resume()
        assert rm.is_paused() is False
        # 独立标记须各自复位
        rm.resume_from_circuit_breaker("复位")
        rm.resume_engine_holds("恢复")
        assert rm.circuit_paused is False and rm.engine_paused is False


# ─────────────────────────────────────────────
# 数据质量联动
# ─────────────────────────────────────────────

class TestDataQualityLinkage:
    def test_critical_alert_global_pause(self) -> None:
        rm = RiskManager()
        status = rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="critical", symbol="000001.SZ", bad_ratio=0.9)
        )
        assert status == RiskStatus.PAUSED
        assert rm.is_paused() is True
        assert rm.stats["data_quality_pauses"] >= 1

    def test_error_alert_symbol_pause(self) -> None:
        rm = RiskManager()
        status = rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="error", symbol="000001.SZ", bad_ratio=0.6)
        )
        # 实现：error+高占比返回 PAUSED，但全局状态只置 WARNING（单标暂停）
        assert status == RiskStatus.PAUSED
        assert rm.get_status() == RiskStatus.WARNING
        assert rm.get_paused_symbols() == ["000001.SZ"]
        assert rm.is_symbol_data_quality_paused("000001.SZ") is True
        assert rm.data_quality_global_paused is False

    def test_error_alert_low_ratio_only_warn(self) -> None:
        rm = RiskManager()
        status = rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="error", symbol="000001.SZ", bad_ratio=0.1)
        )
        # 低脏数据占比 → 仅置 WARNING，不暂停
        assert rm.get_paused_symbols() == []
        assert rm.get_status() == RiskStatus.WARNING
        assert status == RiskStatus.WARNING

    def test_symbol_paused_blocks_buy(self) -> None:
        rm = RiskManager()
        rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="error", symbol="000001.SZ", bad_ratio=0.6)
        )
        result = rm.check_order(_buy_order("000001.SZ"), _normal_context())
        assert result.passed is False
        assert result.rule_name == "data_quality_symbol_paused"
        # 其他标的不受影响
        other = rm.check_order(_buy_order("600519.SH"), _normal_context())
        assert other.passed is True

    def test_pipeline_complete_high_bad_ratio_pauses(self) -> None:
        rm = RiskManager()
        rm.handle_pipeline_complete_event(
            PipelineCompleteEvent(symbol="000001.SZ", bad_count=800, total_count=1000,
                                  quality_grade="poor", bad_ratio=0.8)
        )
        assert rm.is_symbol_data_quality_paused("000001.SZ") is True

    def test_pipeline_complete_clean_no_pause(self) -> None:
        rm = RiskManager()
        rm.handle_pipeline_complete_event(
            PipelineCompleteEvent(symbol="000001.SZ", bad_count=1, total_count=1000,
                                  quality_grade="good", bad_ratio=0.001)
        )
        assert rm.get_paused_symbols() == []

    def test_pipeline_failed_warns(self) -> None:
        rm = RiskManager()
        status = rm.handle_pipeline_complete_event(
            PipelineCompleteEvent(symbol="000001.SZ", status="failed", error="boom")
        )
        assert rm.get_status() == RiskStatus.WARNING
        assert status == RiskStatus.WARNING

    def test_data_quality_event(self) -> None:
        rm = RiskManager()
        rm.handle_data_quality_event(
            DataQualityEvent(symbol="000001.SZ", dirty_type="duplicate", bad_count=10)
        )
        assert rm.stats["data_quality_events"] >= 1

    def test_get_all_status(self) -> None:
        rm = RiskManager()
        rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="error", symbol="000001.SZ", bad_ratio=0.6)
        )
        all_status = rm.get_all_data_quality_status()
        assert "000001.SZ" in all_status

    def test_get_status_unknown_symbol(self) -> None:
        rm = RiskManager()
        assert rm.get_data_quality_status("999999.SZ") is None

    def test_reset_data_quality_status(self) -> None:
        rm = RiskManager()
        rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="error", symbol="000001.SZ", bad_ratio=0.6)
        )
        rm.reset_data_quality_status("000001.SZ")
        assert rm.get_paused_symbols() == []

    def test_pause_reason_property(self) -> None:
        rm = RiskManager()
        rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="error", symbol="000001.SZ", bad_ratio=0.6)
        )
        assert isinstance(rm.data_quality_pause_reason, str)

    def test_data_quality_config_property(self) -> None:
        rm = RiskManager()
        assert isinstance(rm.data_quality_config, DataQualityRiskConfig)

    def test_auto_resume_after_improvement(self) -> None:
        rm = RiskManager()
        rm.handle_quality_alert_event(
            QualityAlertEvent(alert_level="error", symbol="000001.SZ", bad_ratio=0.6)
        )
        assert rm.is_symbol_data_quality_paused("000001.SZ") is True
        # 后续干净数据 → 自动恢复
        rm.handle_pipeline_complete_event(
            PipelineCompleteEvent(symbol="000001.SZ", bad_count=1, total_count=1000,
                                  quality_grade="good", bad_ratio=0.001)
        )
        assert rm.is_symbol_data_quality_paused("000001.SZ") is False


# ─────────────────────────────────────────────
# 事件总线订阅
# ─────────────────────────────────────────────

class TestEventBus:
    def test_subscribe_and_unsubscribe(self) -> None:
        from myquant.core.event_bus import get_event_bus

        bus = get_event_bus()
        rm = RiskManager()
        keys = rm.subscribe_to_event_bus(bus)
        assert len(keys) >= 3  # 至少 3 类事件订阅
        n = rm.unsubscribe_from_event_bus()
        assert n == len(keys)


# ─────────────────────────────────────────────
# 规则库管理
# ─────────────────────────────────────────────

class TestRuleLibraryManagement:
    def test_add_and_get_rule(self) -> None:
        from myquant.risk.rules import RateLimitRule

        rm = RiskManager()
        rule = RateLimitRule(max_orders_per_day=7)
        rm.add_rule(rule)
        assert rm.get_rule("rate_limit") is not None

    def test_remove_rule(self) -> None:
        rm = RiskManager()
        assert rm.remove_rule("blacklist") is True
        assert rm.get_rule("blacklist") is None
        assert rm.remove_rule("not_exists") is False

    def test_enable_disable(self) -> None:
        rm = RiskManager()
        assert rm.disable_rule("rate_limit") is True
        assert "rate_limit" not in rm.enabled_rules
        assert rm.enable_rule("rate_limit") is True
        assert "rate_limit" in rm.enabled_rules

    def test_enable_unknown_rule_false(self) -> None:
        rm = RiskManager()
        assert rm.enable_rule("bogus") is False


# ─────────────────────────────────────────────
# 异常交易 / 日报 / 事件
# ─────────────────────────────────────────────

class TestAnomalyAndDaily:
    def test_set_pending_orders(self) -> None:
        rm = RiskManager()
        rm.set_pending_orders([Order(symbol="000001.SZ", side=OrderSide.BUY, quantity=10.0)])
        ctx = _normal_context()
        rm.check_order(_buy_order(), ctx)
        assert len(ctx.pending_orders) == 1
        assert ctx.pending_orders[0]["symbol"] == "000001.SZ"
        assert ctx.pending_orders[0]["side"] == OrderSide.BUY

    def test_record_cancel_order(self) -> None:
        rm = RiskManager()
        rm.record_cancel_order(_buy_order(), reason="手动撤单")
        assert rm.stats["cancel_orders_today"] == 1
        assert rm._cancel_count_today == 1

    def test_get_daily_report(self) -> None:
        rm = RiskManager()
        rm.check_order(_buy_order(), _normal_context())
        rm.record_cancel_order(_buy_order(), reason="撤")
        report = rm.get_daily_report()
        assert report["new_orders_today"] == 1
        assert report["cancel_orders_today"] == 1

    def test_reset_daily_counts(self) -> None:
        rm = RiskManager()
        rm.check_order(_buy_order(), _normal_context())
        rm.record_cancel_order(_buy_order(), reason="撤")
        rm.reset_daily_counts()
        report = rm.get_daily_report()
        assert report["new_orders_today"] == 0
        assert report["cancel_orders_today"] == 0

    def test_get_events_filter(self) -> None:
        rm = RiskManager()
        rm.pause("x")
        # pause 记录 STATUS_CHANGE 事件
        status_events = rm.get_events(event_type=RiskEventType.STATUS_CHANGE)
        assert len(status_events) >= 1
        symbol_events = rm.get_events(symbol="ANY")
        assert symbol_events == []

    def test_get_summary(self) -> None:
        rm = RiskManager()
        s = rm.get_summary()
        assert "status" in s and "stats" in s

    def test_reset(self) -> None:
        rm = RiskManager()
        rm.pause("x")
        rm.reset()
        assert rm.get_status() == RiskStatus.NORMAL
        assert rm.event_count == 0


# ─────────────────────────────────────────────
# build_risk_manager_from_config
# ─────────────────────────────────────────────

class TestBuildFromConfig:
    def test_default_rules(self) -> None:
        rm = build_risk_manager_from_config(
            risk_cfg={"max_drawdown_pct": 0.2},
            position_cfg={"max_single_position": 0.15, "max_total_positions": 8},
        )
        assert "max_drawdown" in rm.rule_names
        assert "self_trade" in rm.rule_names  # anomaly_rules=None → 全部启用
        assert "fat_finger" in rm.rule_names
        assert "trading_hours" in rm.rule_names

    def test_anomaly_rules_disabled(self) -> None:
        rm = build_risk_manager_from_config(
            risk_cfg={}, position_cfg={}, anomaly_rules=[]
        )
        assert "self_trade" not in rm.rule_names

    def test_anomaly_rules_subset(self) -> None:
        rm = build_risk_manager_from_config(
            risk_cfg={}, position_cfg={}, anomaly_rules=["cancel_rate"]
        )
        assert "cancel_rate" in rm.rule_names
        assert "self_trade" not in rm.rule_names

    def test_unknown_anomaly_rule_raises(self) -> None:
        with pytest.raises(ValueError, match="未知异常交易规则名"):
            build_risk_manager_from_config(
                risk_cfg={}, position_cfg={}, anomaly_rules=["bogus"]
            )

    def test_trading_hours_disabled(self) -> None:
        rm = build_risk_manager_from_config(
            risk_cfg={}, position_cfg={}, enable_trading_hours=False
        )
        rule = rm.get_rule("trading_hours")
        assert rule is not None and rule.enabled is False

    def test_stop_loss_enabled_from_strategy(self) -> None:
        rm = build_risk_manager_from_config(
            risk_cfg={},
            position_cfg={},
            strategy_cfg={"params": {"stop_loss_pct": 0.08, "take_profit_pct": 0.3}},
        )
        assert rm.get_rule("stop_loss").enabled is True
        assert rm.get_rule("take_profit").enabled is True
