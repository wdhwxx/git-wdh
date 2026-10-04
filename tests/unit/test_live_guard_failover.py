# -*- coding: utf-8 -*-
"""
H2 修复回归测试：实盘保障组件故障注入（事件总线断连降级）
============================================================
覆盖 engine.py H2 修复的全部 try-except 降级路径，验证"保护不旁路、故障不阻断"：
  A. 事件总线获取失败 → 降级为无联动，start() 仍成功（订单风控不依赖总线）
  B. RiskManager 订阅事件总线失败 → warning，订单前置风控仍生效
  C. data_feed.attach_event_bus 失败 → warning，流式清洗仍生效
  D. RiskManager.check_order 异常 → 订单拒绝（安全第一）+ guardian 记录拒绝
  E. 调度器挂载失败 → 主链路不阻塞，start() 仍成功
  F. 守护巡检 evaluate 异常 → _guardian_patrol 不抛出（仅记录）
  G. 状态快照加载失败 / 无快照 → 全新开始，不阻塞启动
  H. 事件总线不可用时订单链路仍完整（降级核心：可正常检查并提交）
  I. 全部保障组件关闭（enable_*=False）→ 极简模式 start/stop 正常

回归依据: 系统权威评估报告_20260901.md 第2节 H2（引擎接入保障组件）+ 事件总线断连降级核验
"""
from __future__ import annotations

from unittest import mock

import pytest

from myquant.live.brokers.paper_broker import PaperBroker
from myquant.live.engine import EngineState, LiveEngine, RiskConfig
from myquant.live.portfolio import LivePortfolio
from myquant.live.state_store import StateStore
from myquant.models.trade import Order, OrderSide, OrderType
from myquant.risk import RiskManager
from myquant.strategies.ma_strategy import MACrossStrategy


class _BoomFeed:
    """带 attach_event_bus 且必然失败的行情源（测试C用）"""

    is_realtime = False

    def connect(self) -> bool:
        return True

    def subscribe(self, symbol: str, callback) -> None:
        return None

    def attach_event_bus(self, bus, source: str = "") -> None:
        raise RuntimeError("feed bus attach fail")

    def stop(self) -> None:
        return None

    def disconnect(self) -> None:
        return None


class _BoomRiskManager(RiskManager):
    """check_order 必然抛异常的风控管理器（测试D用）"""

    def check_order(self, order, context):  # type: ignore[override]
        raise RuntimeError("rm broken")


@pytest.fixture()
def engine_factory(tmp_path):
    """最小可启动引擎装配（tmp_path 隔离 StateStore 落盘，不污染项目目录）"""

    def _build(**kw) -> LiveEngine:
        engine = LiveEngine(
            risk_config=RiskConfig(),
            state_store=StateStore(root=str(tmp_path), store_id="failover_test"),
            **kw,
        )
        broker = PaperBroker(initial_capital=1_000_000.0)
        broker.connect()
        engine.set_broker(broker)
        engine.set_portfolio(LivePortfolio(initial_capital=1_000_000.0))
        engine.add_strategy(MACrossStrategy())
        return engine

    return _build


def _buy_order(symbol: str = "000001.SZ", quantity: int = 100) -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
    )


# ── A. 事件总线获取失败 → 降级为无联动，start 仍成功 ──────────────────

def test_事件总线获取失败_降级无联动_start仍成功(engine_factory) -> None:
    engine = engine_factory()
    with mock.patch(
        "myquant.live.engine.get_event_bus", side_effect=RuntimeError("bus down")
    ):
        assert engine.start() is True
    assert engine._state == EngineState.RUNNING
    engine.stop()
    assert engine._state == EngineState.STOPPED


# ── B. RiskManager 订阅事件总线失败 → 订单前置风控仍生效 ─────────────

def test_风险管理器订阅总线失败_订单风控仍生效(engine_factory) -> None:
    engine = engine_factory()
    with mock.patch.object(
        RiskManager, "subscribe_to_event_bus", side_effect=RuntimeError("sub fail")
    ):
        assert engine.start() is True
    # 订单前置风控不依赖总线，仍可正常检查（KP-P3-04 需昨收+现价基准，注入使放行）
    engine._prev_close_map["000001.SZ"] = 10.0
    engine._last_prices["000001.SZ"] = 10.5
    assert engine._risk_manager_check(_buy_order(), None) is True
    engine.stop()


# ── C. data_feed 挂载事件总线失败 → 清洗仍生效 ───────────────────────

def test_data_feed挂载事件总线失败_清洗仍生效(engine_factory) -> None:
    engine = engine_factory()
    engine.set_data_feed(_BoomFeed())
    assert engine.start() is True
    engine.stop()
    assert engine._state == EngineState.STOPPED


# ── D. RiskManager.check_order 异常 → 订单拒绝（安全第一）─────────────

def test_风险检查异常_订单拒绝安全第一(engine_factory) -> None:
    engine = engine_factory(risk_manager=_BoomRiskManager())
    assert engine._guardian is not None
    assert engine._risk_manager_check(_buy_order(), None) is False
    assert engine._stats["errors"] == 1


# ── E. 调度器挂载失败 → 主链路不阻塞 ─────────────────────────────────

def test_调度器挂载失败_主链路不阻塞(engine_factory) -> None:
    engine = engine_factory()
    with mock.patch(
        "myquant.live.engine.attach_engine", side_effect=RuntimeError("sched fail")
    ):
        assert engine.start() is True
    engine.stop()
    assert engine._state == EngineState.STOPPED


# ── F. 守护巡检 evaluate 异常 → 不抛出（仅记录）──────────────────────

def test_守护巡检异常_仅记录不抛出(engine_factory) -> None:
    engine = engine_factory()
    assert engine._guardian is not None
    engine._guardian.evaluate = mock.Mock(side_effect=RuntimeError("guard fail"))
    engine._guardian_patrol()  # 不应抛出
    engine.stop()


# ── G. 状态快照加载失败 / 无快照 → 全新开始，不阻塞启动 ───────────────

def test_状态快照加载失败_全新开始不阻塞(engine_factory) -> None:
    engine = engine_factory(auto_recover=True)
    with mock.patch(
        "myquant.live.engine.StateStore.load_latest",
        side_effect=RuntimeError("store corrupt"),
    ):
        assert engine.start() is True
    engine.stop()


def test_无历史快照_全新开始不阻塞(engine_factory) -> None:
    # tmp_path 下无任何快照，load_latest 应抛 SnapshotNotFoundError 并被捕获
    engine = engine_factory(auto_recover=True)
    assert engine.start() is True
    engine.stop()


# ── H. 事件总线不可用时订单链路仍完整（降级核心）─────────────────────

def test_事件总线不可用_订单链路仍完整(engine_factory) -> None:
    engine = engine_factory()
    with mock.patch(
        "myquant.live.engine.get_event_bus", side_effect=RuntimeError("bus down")
    ):
        assert engine.start() is True
    # 风控检查通过（KP-P3-04 需昨收+现价基准，注入使放行）
    order = _buy_order()
    engine._prev_close_map["000001.SZ"] = 10.0
    engine._last_prices["000001.SZ"] = 10.5
    assert engine._risk_manager_check(order, None) is True
    # 订单正常提交至券商（先建立行情基准，市价单撮合需要）
    assert engine._broker_manager is not None
    engine._broker_manager.primary.update_market_price("000001.SZ", 10.0)
    engine._submit_order(order)
    engine.stop()
    assert engine._stats["total_orders"] == 1


# ── I. 全部保障组件关闭 → 极简模式 start/stop 正常 ───────────────────

def test_全部保障组件关闭_极简模式正常(engine_factory) -> None:
    engine = engine_factory(
        enable_risk_manager=False,
        enable_state_store=False,
        enable_scheduler=False,
        enable_guardian=False,
        enable_event_bus=False,
    )
    assert engine._risk_manager is None
    assert engine._guardian is None
    assert engine._state_store is None
    assert engine._scheduler is None
    assert engine.start() is True
    # 未接入 RiskManager 时回退内置风控（返回 True 不阻断）
    assert engine._risk_manager_check(_buy_order(), None) is True
    engine.stop()
    assert engine._state == EngineState.STOPPED
