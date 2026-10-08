# 文件名：tests/unit/test_daily_loss_limit.py
"""
test_daily_loss_limit.py - 单日亏损上限规则测试（任务46 / P1）
================================================================
覆盖 P1「每日亏损 1% 硬编码（DailyLossLimitRule）」：
  1. RiskContext.daily_loss_pct 计算 —— 日初基准 / 只算负向（盈利=0）/
     无日初快照=0
  2. DailyLossLimitRule 判定 —— 亏损超阈值拒 BUY / 未超阈值放行 /
     阈值恰好相等放行（严格大于）/ SELL 恒放行（不拦减仓止损）
  3. 注册与装配 —— RULE_REGISTRY 可注册 / from_config 配置装配 /
     create_default_library 默认含 1% 规则
  4. 引擎接线 —— _build_risk_context 跨日重置日初快照（同日不变）/
     _create_default_risk_manager 硬编码 1% 兜底启用 /
     build_risk_manager_from_config 消费 max_daily_loss_pct（缺省 1%）/
     端到端：亏损超限拒 BUY、放行 SELL

依赖: pytest, myquant.risk.rules（RiskContext/DailyLossLimitRule/RiskRuleLibrary/
      RULE_REGISTRY/create_default_library）, myquant.live.engine
"""

from __future__ import annotations

from unittest import mock

import pytest

from myquant.live.engine import LiveEngine, RiskConfig
from myquant.live.portfolio import LivePortfolio
from myquant.live.state_store import StateStore
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType
from myquant.risk.risk_manager import build_risk_manager_from_config  # 回写#255 P0-2：装配工厂下沉风控层
from myquant.risk.rules import (
    RULE_REGISTRY,
    DailyLossLimitRule,
    RiskContext,
    RiskRuleLibrary,
    create_default_library,
)


# ═══════════════════════════════════════════════
#  测试辅助
# ═══════════════════════════════════════════════

def _make_order(side: OrderSide = OrderSide.BUY) -> Order:
    return Order(
        symbol="600519.SH",
        side=side,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=1500.0,
        strategy_id="test_strategy",
    )


def _make_ctx(
    total_assets: float = 1_000_000.0,
    day_start_assets: float = 1_000_000.0,
) -> RiskContext:
    return RiskContext(
        cash=total_assets,
        total_assets=total_assets,
        initial_capital=1_000_000.0,
        day_start_assets=day_start_assets,
        trading_date="2026-09-21",
    )


def _make_engine(tmp_path) -> LiveEngine:
    engine = LiveEngine(
        risk_config=RiskConfig(),
        state_store=StateStore(root=str(tmp_path), store_id="daily_loss_test"),
    )
    engine.set_portfolio(LivePortfolio(initial_capital=1_000_000.0))
    return engine


# ═══════════════════════════════════════════════
#  1. RiskContext.daily_loss_pct 计算
# ═══════════════════════════════════════════════

def test_日亏损比例_低于日初() -> None:
    ctx = _make_ctx(total_assets=990_000.0, day_start_assets=1_000_000.0)
    assert ctx.daily_loss_pct == pytest.approx(0.01)


def test_日亏损比例_持平为零() -> None:
    ctx = _make_ctx(total_assets=1_000_000.0, day_start_assets=1_000_000.0)
    assert ctx.daily_loss_pct == 0.0


def test_日亏损比例_盈利为零_只算负向() -> None:
    """当前资产高于日初（盈利）→ 亏损 0（不把盈利算成负亏损）"""
    ctx = _make_ctx(total_assets=1_020_000.0, day_start_assets=1_000_000.0)
    assert ctx.daily_loss_pct == 0.0


def test_日亏损比例_无日初快照为零() -> None:
    ctx = RiskContext(total_assets=990_000.0, day_start_assets=0.0)
    assert ctx.daily_loss_pct == 0.0


# ═══════════════════════════════════════════════
#  2. DailyLossLimitRule 判定
# ═══════════════════════════════════════════════

def test_亏损超阈值_拒绝买入() -> None:
    rule = DailyLossLimitRule(max_daily_loss_pct=0.01)
    ctx = _make_ctx(total_assets=980_000.0, day_start_assets=1_000_000.0)  # 亏2%
    result = rule.check(_make_order(OrderSide.BUY), ctx)
    assert not result.passed
    assert result.rule_name == "daily_loss_limit"
    assert "当日亏损超限" in result.reason


def test_亏损未超阈值_放行买入() -> None:
    rule = DailyLossLimitRule(max_daily_loss_pct=0.01)
    ctx = _make_ctx(total_assets=995_000.0, day_start_assets=1_000_000.0)  # 亏0.5%
    result = rule.check(_make_order(OrderSide.BUY), ctx)
    assert result.passed
    assert result.details.get("daily_loss") == pytest.approx(0.005)


def test_亏损恰好等于阈值_放行() -> None:
    """严格大于才拒绝（1.00% 恰好等于 1% → 放行）"""
    rule = DailyLossLimitRule(max_daily_loss_pct=0.01)
    ctx = _make_ctx(total_assets=990_000.0, day_start_assets=1_000_000.0)  # 亏1.0%
    result = rule.check(_make_order(OrderSide.BUY), ctx)
    assert result.passed


def test_卖出恒放行_亏损超阈值也不拦() -> None:
    """触发后允许减仓/止损出局（只禁新开仓，不拦 SELL）"""
    rule = DailyLossLimitRule(max_daily_loss_pct=0.01)
    ctx = _make_ctx(total_assets=950_000.0, day_start_assets=1_000_000.0)  # 亏5%
    result = rule.check(_make_order(OrderSide.SELL), ctx)
    assert result.passed


# ═══════════════════════════════════════════════
#  3. 注册与装配
# ═══════════════════════════════════════════════

def test_规则注册表包含daily_loss_limit() -> None:
    assert "daily_loss_limit" in RULE_REGISTRY
    assert RULE_REGISTRY["daily_loss_limit"] is DailyLossLimitRule


def test_from_config装配规则() -> None:
    library = RiskRuleLibrary.from_config(
        {"daily_loss_limit": {"max_daily_loss_pct": 0.02}}
    )
    rule = library.get_rule("daily_loss_limit")
    assert rule is not None
    assert rule.enabled
    assert rule.max_daily_loss_pct == 0.02


def test_from_config支持禁用() -> None:
    library = RiskRuleLibrary.from_config(
        {"daily_loss_limit": {"enabled": False, "max_daily_loss_pct": 0.02}}
    )
    rule = library.get_rule("daily_loss_limit")
    assert rule is not None
    assert not rule.enabled


def test_默认规则库含百分之一单日亏损() -> None:
    library = create_default_library()
    rule = library.get_rule("daily_loss_limit")
    assert rule is not None
    assert rule.enabled
    assert rule.max_daily_loss_pct == 0.01


# ═══════════════════════════════════════════════
#  4. 引擎接线
# ═══════════════════════════════════════════════

def test_引擎日初快照_同日不变跨日重置(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    with mock.patch.object(
        engine._portfolio, "get_total_assets", return_value=1_000_000.0
    ):
        ctx1 = engine._build_risk_context(None)
    assert ctx1.day_start_assets == 1_000_000.0
    assert engine._day_start_assets == 1_000_000.0

    # 同日资产下跌：日初快照不变，daily_loss_pct 反映当日亏损
    with mock.patch.object(
        engine._portfolio, "get_total_assets", return_value=990_000.0
    ):
        ctx2 = engine._build_risk_context(None)
    assert ctx2.day_start_assets == 1_000_000.0
    assert ctx2.daily_loss_pct == pytest.approx(0.01)

    # 跨日（模拟下一交易日）：日初快照重置为当日首见资产
    engine._active_trading_date = "2000-01-01"
    with mock.patch.object(
        engine._portfolio, "get_total_assets", return_value=995_000.0
    ):
        ctx3 = engine._build_risk_context(None)
    assert ctx3.day_start_assets == 995_000.0
    assert ctx3.daily_loss_pct == 0.0


def test_默认风控硬编码百分之一兜底(tmp_path) -> None:
    engine = _make_engine(tmp_path)
    rule = engine._create_default_risk_manager()._library.get_rule("daily_loss_limit")
    assert rule is not None
    assert rule.enabled
    assert rule.max_daily_loss_pct == 0.01  # 硬编码 1%（配置可覆盖）


def test_配置工厂消费max_daily_loss_pct() -> None:
    rm = build_risk_manager_from_config(
        {"max_daily_loss_pct": 0.02},
        {"max_single_position": 0.2, "max_total_positions": 5},
    )
    rule = rm._library.get_rule("daily_loss_limit")
    assert rule is not None
    assert rule.enabled
    assert rule.max_daily_loss_pct == 0.02


def test_配置工厂缺省默认百分之一() -> None:
    rm = build_risk_manager_from_config({}, {})
    rule = rm._library.get_rule("daily_loss_limit")
    assert rule is not None
    assert rule.max_daily_loss_pct == 0.01


def test_端到端_亏损超限拒买放行卖(tmp_path) -> None:
    """RiskManager 全链路：当日亏损 2%（>1%）→ BUY 拒绝、SELL 放行"""
    engine = _make_engine(tmp_path)
    # KP-P3-04 fail-closed：注入昨收与现价，使 PriceLimitRule 放行（本测聚焦 DailyLossLimitRule）
    engine._prev_close_map["600519.SH"] = 1500.0
    engine._last_prices["600519.SH"] = 1500.0
    # 第一步：建立日初快照（当日首见资产 100 万）
    with mock.patch.object(
        engine._portfolio, "get_total_assets", return_value=1_000_000.0
    ):
        engine._build_risk_context(None)
    # 第二步：当日资产跌至 98 万（亏损 2% > 1%）→ BUY 拒绝、SELL 放行
    with mock.patch.object(
        engine._portfolio, "get_total_assets", return_value=980_000.0
    ):
        assert engine._risk_manager_check(_make_order(OrderSide.BUY), None) is False
        assert engine._risk_manager_check(_make_order(OrderSide.SELL), None) is True
