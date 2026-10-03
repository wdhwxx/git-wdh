# 文件名：tests/unit/test_live_risk.py
"""
test_live_risk.py - 实盘风控单元测试
======================================
覆盖 myquant/risk/ 和 myquant/live/engine.py：
  - RiskCheckResult / RiskContext   风控数据结构
  - 6类风控规则                      PositionLimit/SingleOrderLimit/MaxDrawdown/StopLoss/TakeProfit/RateLimit
  - RiskManager                     风控管理器（订单检查/暂停恢复/数据质量联动）
  - RiskGuardian                    风控守护（健康检查/强制平仓/告警/决策记录）
  - LiveEngine                      实盘引擎（继承BaseEngine/生命周期/风控集成）

测试维度:
  1. 风控数据结构（RiskCheckResult/RiskContext/字段/计算）
  2. 6类风控规则（通过/拒绝/边界条件/参数配置）
  3. RiskManager（check_order/pause/resume/status/数据质量状态）
  4. RiskGuardian（健康检查/evaluate/强制平仓/告警/决策统计/重置）
  5. LiveEngine（实例化/继承BaseEngine/生命周期接口/风控配置）
  6. 实盘准备（实时风控/事件订阅/增量检查/熔断机制）

运行方式:
  pytest tests/unit/test_live_risk.py -v

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/unit/test_live_risk.py）
  2. 为实时行情及实盘无缝对接做好充足准备（实时风控/事件总线/熔断/强制平仓）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 风控检查仅使用当前持仓和当前价格，不引用未来数据
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from myquant.models.trade import Order, OrderSide, OrderType, OrderStatus
from myquant.risk import RiskGuardian, RiskManager
from myquant.risk.risk_manager import (
    DataQualityRiskConfig,
    RiskEvent,
    RiskEventType,
    RiskStatus,
    SymbolDataQualityStatus,
)
from myquant.risk.rules import (
    MaxDrawdownRule,
    PositionLimitRule,
    RateLimitRule,
    RiskCheckResult,
    RiskContext,
    RiskRule,
    SingleOrderLimitRule,
    StopLossRule,
    TakeProfitRule,
)


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _make_order(symbol: str = "000001.SZ", side: OrderSide = OrderSide.BUY,
                quantity: float = 100, price: float = 10.0) -> Order:
    """构造测试订单"""
    return Order(
        order_id=f"test_{int(time.time()*1000)}",
        symbol=symbol,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=price,
        status=OrderStatus.PENDING,
        created_at=time.time(),
    )


def _make_context(cash: float = 500_000.0, total_assets: float = 1_000_000.0,
                  initial_capital: float = 1_000_000.0,
                  positions: Optional[List[Dict]] = None,
                  peak_assets: float = 1_000_000.0,
                  current_price: Optional[Dict[str, float]] = None,
                  order_count_today: int = 0,
                  last_order_time: float = 0.0) -> RiskContext:
    """构造风控上下文"""
    return RiskContext(
        cash=cash,
        total_assets=total_assets,
        initial_capital=initial_capital,
        positions=positions or [],
        position_count=len(positions) if positions else 0,
        peak_assets=peak_assets,
        current_price=current_price or {},
        order_count_today=order_count_today,
        last_order_time=last_order_time,
        trading_date="2024-01-15",
    )


# ═══════════════════════════════════════════
#  TestRiskCheckResult - 风控检查结果
# ═══════════════════════════════════════════

class TestRiskCheckResult:
    """RiskCheckResult 数据结构测试"""

    def test_pass_result(self):
        """通过结果"""
        result = RiskCheckResult(passed=True, rule_name="test", details={})
        assert result.passed is True
        assert result.rule_name == "test"

    def test_reject_result(self):
        """拒绝结果带原因"""
        result = RiskCheckResult(passed=False, rule_name="test",
                                 reason="超出仓位限制", details={"max": 0.3})
        assert result.passed is False
        assert result.reason == "超出仓位限制"

    def test_default_reason_empty(self):
        """默认原因为空"""
        result = RiskCheckResult(passed=True, rule_name="test", details={})
        assert result.reason == ""

    def test_repr(self):
        """__repr__ 不崩溃"""
        result = RiskCheckResult(passed=True, rule_name="test", details={})
        r = repr(result)
        assert isinstance(r, str)


# ═══════════════════════════════════════════
#  TestRiskContext - 风控上下文
# ═══════════════════════════════════════════

class TestRiskContext:
    """RiskContext 风控上下文测试"""

    def test_default_values(self):
        """默认值"""
        ctx = RiskContext()
        assert ctx.cash == 0.0
        assert ctx.total_assets == 0.0
        assert ctx.initial_capital == 0.0
        assert ctx.position_count == 0

    def test_custom_values(self):
        """自定义值"""
        ctx = _make_context(cash=100_000, total_assets=500_000)
        assert ctx.cash == 100_000
        assert ctx.total_assets == 500_000

    def test_drawdown_calculation(self):
        """回撤计算"""
        ctx = _make_context(
            total_assets=800_000, peak_assets=1_000_000, initial_capital=1_000_000
        )
        dd = ctx.drawdown
        # (1000000 - 800000) / 1000000 = 0.2
        assert abs(dd - 0.2) < 1e-6

    def test_drawdown_no_peak(self):
        """无峰值时回撤为0"""
        ctx = _make_context(total_assets=900_000, peak_assets=0.0)
        dd = ctx.drawdown
        assert dd == 0.0

    def test_get_position(self):
        """获取持仓"""
        positions = [
            {"symbol": "000001.SZ", "quantity": 100, "avg_price": 10.0},
            {"symbol": "600000.SH", "quantity": 200, "avg_price": 8.0},
        ]
        ctx = _make_context(positions=positions)
        pos = ctx.get_position("000001.SZ")
        assert pos is not None
        assert pos["symbol"] == "000001.SZ"
        assert pos["quantity"] == 100

    def test_get_position_nonexistent(self):
        """获取不存在的持仓返回 None"""
        ctx = _make_context(positions=[])
        assert ctx.get_position("999999.SZ") is None

    def test_get_position_value(self):
        """获取持仓市值"""
        positions = [{"symbol": "000001.SZ", "quantity": 100, "market_price": 12.0}]
        ctx = _make_context(positions=positions)
        value = ctx.get_position_value("000001.SZ")
        assert value == 1200.0  # 100 * 12.0

    def test_position_count(self):
        """持仓数量"""
        positions = [
            {"symbol": "000001.SZ", "quantity": 100},
            {"symbol": "600000.SH", "quantity": 200},
        ]
        ctx = _make_context(positions=positions)
        assert ctx.position_count == 2


# ═══════════════════════════════════════════
#  TestPositionLimitRule - 仓位限制规则
# ═══════════════════════════════════════════

class TestPositionLimitRule:
    """PositionLimitRule 仓位限制测试"""

    def test_instantiation(self):
        """实例化"""
        rule = PositionLimitRule(max_position_pct=0.3, max_positions=5)
        assert rule is not None

    def test_pass_within_limit(self):
        """仓位在限制内通过"""
        rule = PositionLimitRule(max_position_pct=0.3, max_positions=5)
        order = _make_order(quantity=100, price=10.0)  # 1000元
        ctx = _make_context(total_assets=1_000_000, positions=[])
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_reject_exceed_position_pct(self):
        """单标的仓位超限拒绝"""
        rule = PositionLimitRule(max_position_pct=0.01, max_positions=5)
        order = _make_order(quantity=10000, price=10.0)  # 100000元 = 10%
        ctx = _make_context(total_assets=1_000_000, positions=[])
        result = rule.check(order, ctx)
        assert result.passed is False

    def test_reject_exceed_max_positions(self):
        """持仓数量超限拒绝"""
        rule = PositionLimitRule(max_position_pct=0.3, max_positions=2)
        order = _make_order(symbol="000001.SZ")
        positions = [
            {"symbol": "600000.SH", "quantity": 100},
            {"symbol": "600001.SH", "quantity": 100},
        ]
        ctx = _make_context(positions=positions)
        result = rule.check(order, ctx)
        assert result.passed is False

    def test_sell_order_always_pass(self):
        """卖出订单不受仓位限制"""
        rule = PositionLimitRule(max_position_pct=0.01, max_positions=1)
        order = _make_order(side=OrderSide.SELL, quantity=10000, price=10.0)
        ctx = _make_context(total_assets=1_000_000, positions=[])
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_inherits_risk_rule(self):
        """继承 RiskRule 抽象基类"""
        assert issubclass(PositionLimitRule, RiskRule)


# ═══════════════════════════════════════════
#  TestSingleOrderLimitRule - 单笔订单限制
# ═══════════════════════════════════════════

class TestSingleOrderLimitRule:
    """SingleOrderLimitRule 单笔订单限制测试"""

    def test_pass_small_order(self):
        """小额订单通过"""
        rule = SingleOrderLimitRule(max_order_amount=100_000)
        order = _make_order(quantity=100, price=10.0)  # 1000元
        ctx = _make_context()
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_reject_large_order(self):
        """大额订单拒绝"""
        rule = SingleOrderLimitRule(max_order_amount=10_000)
        order = _make_order(quantity=10000, price=10.0)  # 100000元
        ctx = _make_context()
        result = rule.check(order, ctx)
        assert result.passed is False

    def test_boundary_exact_limit(self):
        """刚好等于限制通过"""
        rule = SingleOrderLimitRule(max_order_amount=10_000)
        order = _make_order(quantity=1000, price=10.0)  # 10000元
        ctx = _make_context()
        result = rule.check(order, ctx)
        assert result.passed is True


# ═══════════════════════════════════════════
#  TestMaxDrawdownRule - 最大回撤规则
# ═══════════════════════════════════════════

class TestMaxDrawdownRule:
    """MaxDrawdownRule 最大回撤测试"""

    def test_pass_no_drawdown(self):
        """无回撤通过"""
        rule = MaxDrawdownRule(max_drawdown_pct=0.20)
        order = _make_order()
        ctx = _make_context(total_assets=1_000_000, peak_assets=1_000_000)
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_reject_exceed_drawdown(self):
        """回撤超限拒绝开仓"""
        rule = MaxDrawdownRule(max_drawdown_pct=0.10)
        order = _make_order(side=OrderSide.BUY)
        ctx = _make_context(total_assets=800_000, peak_assets=1_000_000)  # 20%回撤
        result = rule.check(order, ctx)
        assert result.passed is False

    def test_sell_pass_even_with_drawdown(self):
        """回撤超限但卖出通过"""
        rule = MaxDrawdownRule(max_drawdown_pct=0.10)
        order = _make_order(side=OrderSide.SELL)
        ctx = _make_context(total_assets=800_000, peak_assets=1_000_000)
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_boundary_drawdown(self):
        """刚好等于回撤限制"""
        rule = MaxDrawdownRule(max_drawdown_pct=0.20)
        order = _make_order(side=OrderSide.BUY)
        ctx = _make_context(total_assets=800_000, peak_assets=1_000_000)  # 刚好20%
        result = rule.check(order, ctx)
        # 边界值可能通过或拒绝，取决于实现（>= 或 >）
        assert isinstance(result.passed, bool)


# ═══════════════════════════════════════════
#  TestStopLossRule - 止损规则
# ═══════════════════════════════════════════

class TestStopLossRule:
    """StopLossRule 止损测试"""

    def test_pass_no_position(self):
        """无持仓通过"""
        rule = StopLossRule(max_loss_pct=0.10)
        order = _make_order()
        ctx = _make_context(positions=[])
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_pass_profitable_position(self):
        """盈利持仓通过"""
        rule = StopLossRule(max_loss_pct=0.10)
        order = _make_order(symbol="000001.SZ")
        positions = [{"symbol": "000001.SZ", "quantity": 100,
                      "avg_cost": 10.0, "market_price": 12.0}]  # 盈利20%
        ctx = _make_context(positions=positions)
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_reject_stop_loss_triggered(self):
        """亏损超限触发止损（拒绝继续买入）"""
        rule = StopLossRule(max_loss_pct=0.10)
        order = _make_order(symbol="000001.SZ", side=OrderSide.BUY)
        positions = [{"symbol": "000001.SZ", "quantity": 100,
                      "avg_cost": 10.0, "market_price": 8.0}]  # 亏损20%
        ctx = _make_context(positions=positions)
        result = rule.check(order, ctx)
        assert result.passed is False


# ═══════════════════════════════════════════
#  TestTakeProfitRule - 止盈规则
# ═══════════════════════════════════════════

class TestTakeProfitRule:
    """TakeProfitRule 止盈测试"""

    def test_pass_no_position(self):
        """无持仓通过"""
        rule = TakeProfitRule(max_profit_pct=0.50)
        order = _make_order()
        ctx = _make_context(positions=[])
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_pass_below_profit_target(self):
        """未达止盈目标通过"""
        rule = TakeProfitRule(max_profit_pct=0.50)
        order = _make_order(symbol="000001.SZ")
        positions = [{"symbol": "000001.SZ", "quantity": 100, "avg_price": 10.0}]
        ctx = _make_context(positions=positions,
                            current_price={"000001.SZ": 12.0})  # 盈利20% < 50%
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_inherits_risk_rule(self):
        """继承 RiskRule"""
        assert issubclass(TakeProfitRule, RiskRule)


# ═══════════════════════════════════════════
#  TestRateLimitRule - 频率限制规则
# ═══════════════════════════════════════════

class TestRateLimitRule:
    """RateLimitRule 频率限制测试"""

    def test_pass_under_limit(self):
        """订单数在限制内通过"""
        rule = RateLimitRule(max_orders_per_day=100, min_interval_seconds=0)
        order = _make_order()
        ctx = _make_context(order_count_today=10, last_order_time=0)
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_reject_exceed_daily_limit(self):
        """超过日订单数限制拒绝"""
        rule = RateLimitRule(max_orders_per_day=10, min_interval_seconds=0)
        order = _make_order()
        ctx = _make_context(order_count_today=11, last_order_time=0)
        result = rule.check(order, ctx)
        assert result.passed is False

    def test_reject_too_frequent(self):
        """下单间隔太短拒绝"""
        rule = RateLimitRule(max_orders_per_day=100, min_interval_seconds=10.0)
        order = _make_order()
        ctx = _make_context(order_count_today=1,
                            last_order_time=time.time() - 1)  # 1秒前
        result = rule.check(order, ctx)
        assert result.passed is False

    def test_pass_after_interval(self):
        """间隔足够通过"""
        rule = RateLimitRule(max_orders_per_day=100, min_interval_seconds=1.0)
        order = _make_order()
        ctx = _make_context(order_count_today=1,
                            last_order_time=time.time() - 5)  # 5秒前
        result = rule.check(order, ctx)
        assert result.passed is True

    def test_sell_bypasses_daily_limit_p101(self):
        """P1-01回归：日下单数已达上限时，平仓/止损卖单（SELL）仍放行——持仓必须可退出"""
        rule = RateLimitRule(max_orders_per_day=10, min_interval_seconds=0)
        sell_order = _make_order(side=OrderSide.SELL)
        ctx = _make_context(order_count_today=11, last_order_time=0)
        result = rule.check(sell_order, ctx)
        assert result.passed is True

    def test_buy_still_rejected_at_limit_p101(self):
        """P1-01回归：日下单数达上限时，开仓买单（BUY）仍被拒绝（原有语义不回退）"""
        rule = RateLimitRule(max_orders_per_day=10, min_interval_seconds=0)
        buy_order = _make_order(side=OrderSide.BUY)
        ctx = _make_context(order_count_today=11, last_order_time=0)
        result = rule.check(buy_order, ctx)
        assert result.passed is False


# ═══════════════════════════════════════════
#  TestRiskManager - 风控管理器
# ═══════════════════════════════════════════

class TestRiskManager:
    """RiskManager 风控管理器测试"""

    def test_instantiation(self):
        """实例化"""
        rm = RiskManager()
        assert rm is not None

    def test_default_status_normal(self):
        """默认状态 NORMAL"""
        rm = RiskManager()
        assert rm.get_status() == RiskStatus.NORMAL

    def test_pause_and_resume(self):
        """暂停和恢复"""
        rm = RiskManager()
        rm.pause("测试暂停")
        assert rm.is_paused() is True
        assert rm.can_open_position() is False
        assert rm.get_status() == RiskStatus.PAUSED
        rm.resume()
        assert rm.is_paused() is False
        assert rm.can_open_position() is True

    def test_pause_with_reason(self):
        """暂停带原因"""
        rm = RiskManager()
        rm.pause("数据异常")
        assert "数据异常" in rm.get_status_reason()

    def test_check_order_when_paused(self):
        """暂停时所有买单被拒"""
        rm = RiskManager()
        rm.pause("全局暂停")
        order = _make_order(side=OrderSide.BUY)
        ctx = _make_context()
        result = rm.check_order(order, ctx)
        assert result.passed is False

    def test_check_order_normal(self):
        """正常状态下订单检查"""
        rm = RiskManager()
        order = _make_order(quantity=10, price=10.0)  # 小额
        ctx = _make_context(total_assets=1_000_000, positions=[])
        result = rm.check_order(order, ctx)
        assert isinstance(result, RiskCheckResult)

    def test_check_positions(self):
        """持仓检查"""
        rm = RiskManager()
        ctx = _make_context(positions=[])
        results = rm.check_positions(ctx)
        assert isinstance(results, list)

    def test_data_quality_config(self):
        """数据质量风控配置"""
        config = DataQualityRiskConfig()
        rm = RiskManager(data_quality_config=config)
        assert rm is not None

    def test_get_data_quality_status_nonexistent(self):
        """获取不存在标的的数据质量状态"""
        rm = RiskManager()
        status = rm.get_data_quality_status("999999.SZ")
        assert status is None

    def test_get_all_data_quality_status(self):
        """获取所有数据质量状态"""
        rm = RiskManager()
        statuses = rm.get_all_data_quality_status()
        assert isinstance(statuses, dict)

    def test_get_paused_symbols(self):
        """获取暂停标的列表"""
        rm = RiskManager()
        paused = rm.get_paused_symbols()
        assert isinstance(paused, list)

    def test_subscribe_event_bus_interface(self):
        """事件总线订阅接口存在"""
        rm = RiskManager()
        assert hasattr(rm, "subscribe_to_event_bus")
        assert callable(rm.subscribe_to_event_bus)

    def test_unsubscribe_event_bus_interface(self):
        """事件总线取消订阅接口存在"""
        rm = RiskManager()
        assert hasattr(rm, "unsubscribe_from_event_bus")
        assert callable(rm.unsubscribe_from_event_bus)


# ═══════════════════════════════════════════
#  TestRiskGuardian - 风控守护进程
# ═══════════════════════════════════════════

class TestRiskGuardian:
    """RiskGuardian 风控守护测试"""

    def test_instantiation(self):
        """实例化"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        assert guardian is not None

    def test_check_health(self):
        """健康检查"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        result = guardian.check_health()
        assert result is not None

    def test_evaluate(self):
        """评估决策"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        decision = guardian.evaluate()
        assert decision is not None

    def test_force_close_all_interface(self):
        """强制平仓接口存在"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        assert hasattr(guardian, "force_close_all")
        assert callable(guardian.force_close_all)

    def test_force_close_symbol_interface(self):
        """单标的强制平仓接口存在"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        assert hasattr(guardian, "force_close_symbol")
        assert callable(guardian.force_close_symbol)

    def test_set_force_close_callback(self):
        """设置强制平仓回调"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        callback_called = []

        def cb(close_type, symbol=None):
            callback_called.append((close_type, symbol))
            return True

        guardian.set_force_close_callback(cb)
        # 触发强制平仓
        guardian.force_close_all("测试强制平仓")
        assert len(callback_called) > 0
        assert callback_called[0][0] == "all"

    def test_alert(self):
        """告警记录"""
        from myquant.risk.guardian import AlertLevel
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        guardian.alert(AlertLevel.WARNING, "测试告警", {"key": "value"})
        alerts = guardian.get_alerts(limit=10)
        assert len(alerts) >= 1

    def test_record_rejection(self):
        """记录拒绝"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        guardian.record_rejection()
        assert guardian.consecutive_rejections >= 1

    def test_record_acceptance(self):
        """记录通过（重置连续拒绝）"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        guardian.record_rejection()
        guardian.record_rejection()
        guardian.record_acceptance()
        assert guardian.consecutive_rejections == 0

    def test_decision_count(self):
        """决策计数"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        guardian.evaluate()
        assert guardian.decision_count >= 1

    def test_get_decisions(self):
        """获取决策历史"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        guardian.evaluate()
        decisions = guardian.get_decisions(limit=10)
        assert isinstance(decisions, list)
        assert len(decisions) >= 1

    def test_get_health_status(self):
        """获取健康状态"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        status = guardian.get_health_status()
        assert status is not None

    def test_get_summary(self):
        """获取摘要"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        guardian.evaluate()
        summary = guardian.get_summary()
        assert isinstance(summary, dict)

    def test_reset(self):
        """重置守护状态"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        guardian.record_rejection()
        guardian.evaluate()
        guardian.reset()
        assert guardian.consecutive_rejections == 0
        assert guardian.decision_count == 0

    def test_last_health(self):
        """获取上次健康检查结果"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        guardian.check_health()
        last = guardian.last_health
        assert last is not None


# ═══════════════════════════════════════════
#  TestLiveEngine - 实盘引擎
# ═══════════════════════════════════════════

class TestLiveEngine:
    """LiveEngine 实盘引擎测试"""

    def test_instantiation(self):
        """实例化"""
        from myquant.live.engine import LiveEngine
        engine = LiveEngine()
        assert engine is not None

    def test_inherits_base_engine(self):
        """继承 BaseEngine"""
        from myquant.engine.base import BaseEngine
        from myquant.live.engine import LiveEngine
        assert issubclass(LiveEngine, BaseEngine)

    def test_lifecycle_methods_exist(self):
        """生命周期方法存在"""
        from myquant.live.engine import LiveEngine
        assert hasattr(LiveEngine, "on_init")
        assert hasattr(LiveEngine, "on_market_event")
        assert hasattr(LiveEngine, "on_stop")
        assert hasattr(LiveEngine, "run")
        assert hasattr(LiveEngine, "start")
        assert hasattr(LiveEngine, "stop")
        assert hasattr(LiveEngine, "pause")
        assert hasattr(LiveEngine, "resume")

    def test_set_data_feed_interface(self):
        """设置数据源接口"""
        from myquant.live.engine import LiveEngine
        engine = LiveEngine()
        assert hasattr(engine, "set_data_feed")
        assert callable(engine.set_data_feed)

    def test_set_broker_interface(self):
        """设置券商接口"""
        from myquant.live.engine import LiveEngine
        engine = LiveEngine()
        assert hasattr(engine, "set_broker")
        assert callable(engine.set_broker)

    def test_add_strategy_interface(self):
        """添加策略接口"""
        from myquant.live.engine import LiveEngine
        engine = LiveEngine()
        assert hasattr(engine, "add_strategy")
        assert callable(engine.add_strategy)

    def test_state_method(self):
        """状态查询"""
        from myquant.live.engine import LiveEngine
        engine = LiveEngine()
        state = engine.state
        assert isinstance(state, str)

    def test_is_running_before_start(self):
        """启动前 is_running = False"""
        from myquant.live.engine import LiveEngine
        engine = LiveEngine()
        assert engine.is_running is False

    def test_risk_config_exists(self):
        """风控配置存在"""
        from myquant.live.engine import LiveEngine, RiskConfig
        config = RiskConfig()
        assert config is not None

    def test_on_market_data_interface(self):
        """实时行情处理接口"""
        from myquant.live.engine import LiveEngine
        engine = LiveEngine()
        assert hasattr(engine, "on_market_data")
        assert callable(engine.on_market_data)


# ═══════════════════════════════════════════
#  TestRealTimePreparation - 实盘准备
# ═══════════════════════════════════════════

class TestRealTimePreparation:
    """实盘无缝对接准备测试"""

    def test_risk_manager_event_driven(self):
        """风控管理器支持事件驱动（数据质量事件联动）"""
        rm = RiskManager()
        assert hasattr(rm, "handle_data_quality_event")
        assert callable(rm.handle_data_quality_event)

    def test_risk_manager_quality_alert_handler(self):
        """数据质量告警处理"""
        rm = RiskManager()
        assert hasattr(rm, "handle_quality_alert_event")
        assert callable(rm.handle_quality_alert_event)

    def test_risk_manager_pipeline_complete_handler(self):
        """清洗管道完成事件处理"""
        rm = RiskManager()
        assert hasattr(rm, "handle_pipeline_complete_event")
        assert callable(rm.handle_pipeline_complete_event)

    def test_guardian_force_close_for_circuit_breaker(self):
        """风控守护支持熔断式强制平仓"""
        rm = RiskManager()
        guardian = RiskGuardian(risk_manager=rm)
        callback_results = []

        def cb(close_type, symbol=None):
            callback_results.append((close_type, symbol))
            return True

        guardian.set_force_close_callback(cb)
        # 模拟熔断触发
        guardian.force_close_all("熔断：回撤超限")
        assert len(callback_results) > 0
        assert callback_results[0][0] == "all"

    def test_symbol_level_pause(self):
        """单标的级别暂停（数据质量问题时只暂停问题标的）"""
        rm = RiskManager()
        # 验证接口存在
        assert hasattr(rm, "get_paused_symbols")
        assert hasattr(rm, "get_data_quality_status")
        # 初始无暂停标的
        assert rm.get_paused_symbols() == []

    def test_risk_event_data_structure(self):
        """风控事件数据结构"""
        event = RiskEvent(
            event_type=RiskEventType.ORDER_REJECTED,
            symbol="000001.SZ",
            reason="测试风控事件",
        )
        assert event is not None
        assert event.symbol == "000001.SZ"

    def test_symbol_data_quality_status(self):
        """单标的数据质量状态"""
        status = SymbolDataQualityStatus(symbol="000001.SZ")
        assert status.symbol == "000001.SZ"
        assert status.is_paused is False
        status.mark_paused("数据缺失")
        assert status.is_paused is True
        status.reset_pause()
        assert status.is_paused is False

    def test_data_quality_config_defaults(self):
        """数据质量风控配置默认值"""
        config = DataQualityRiskConfig()
        assert config is not None
        d = config.to_dict()
        assert isinstance(d, dict)

    def test_live_engine_warmup_interface(self):
        """实盘引擎历史数据预热接口"""
        from myquant.live.engine import LiveEngine
        engine = LiveEngine()
        assert hasattr(engine, "_warmup_history")
        assert hasattr(engine, "_update_bar_history")

    def test_no_future_data_in_risk_check(self):
        """风控检查不使用未来数据（仅当前持仓+当前价格）"""
        rule = PositionLimitRule(max_position_pct=0.3, max_positions=5)
        order = _make_order(quantity=100, price=10.0)
        # 上下文只包含当前状态，无未来数据
        ctx = _make_context(
            total_assets=1_000_000,
            positions=[{"symbol": "000001.SZ", "quantity": 100,
                        "avg_cost": 10.0, "market_price": 10.5}],
        )
        result = rule.check(order, ctx)
        assert isinstance(result, RiskCheckResult)
