# 文件名：tests/unit/test_live_lookahead.py
"""
test_live_lookahead.py - 实盘引擎防未来函数校验接线测试（任务42）
====================================================================
覆盖 myquant/live/engine.py 的 LookaheadGuard 实盘接线：
  1. LiveEngine 接线      — 默认启用 / 显式关闭 / 注入自定义 guard / start 时 reset
  2. 信号生成阶段校验      — _process_signal 记录 record_signal + 登记 order_id→信号日期
                            （HOLD 不记录、风控拒绝不记录，与回测语义一致）
  3. 成交回报校验          — on_trade 记录 record_trade（铁律3）
                            同日模拟撮合跳过 T+1 校验；成交早于信号日保留校验（真违规）
  4. 停止统一校验          — stop() 调 assert_pass（无违规通过；记录模式违规留痕不阻断停机）
  5. 工厂配置消费          — create_live_engine 消费 lookahead_guard.enabled / strict_mode

回归依据: 审计问题 42（实盘引擎未接入防未来校验，lookahead_guard 配置为死配置）
运行方式: pytest tests/unit/test_live_lookahead.py -v
"""
from __future__ import annotations

from datetime import datetime

import pytest

from myquant.core.lookahead_guard import LookaheadGuard
from myquant.live.engine import EngineState, LiveEngine, RiskConfig, create_live_engine
from myquant.live.brokers.paper_broker import PaperBroker
from myquant.live.portfolio import LivePortfolio
from myquant.live.state_store import StateStore
from myquant.models.market_data import Bar
from myquant.models.trade import OrderSide, Trade
from myquant.strategies.base import Signal, SignalType
from myquant.strategies.ma_strategy import MACrossStrategy


# ═══════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════

def _ts(year: int, month: int, day: int, hour: int = 15, minute: int = 0) -> float:
    """构造收盘时点时间戳（默认 15:00 收盘后）"""
    return datetime(year, month, day, hour, minute).timestamp()


def _make_bar(symbol: str = "000001.SZ", ts: float | None = None, close: float = 10.0) -> Bar:
    return Bar(symbol=symbol, timestamp=ts if ts is not None else _ts(2024, 1, 15), close=close)


def _make_signal(symbol: str = "000001.SZ", st: SignalType = SignalType.BUY) -> Signal:
    return Signal(symbol=symbol, signal_type=st, reason="test")


def _real_order_id(engine: LiveEngine) -> str:
    """取引擎登记的真实 order_id（供成交测试关联信号映射）"""
    assert engine._order_signal_dates, "订单映射为空，信号未登记"
    return next(iter(engine._order_signal_dates))


def _make_trade(
    order_id: str = "ord_001",
    symbol: str = "000001.SZ",
    ts: float | None = None,
    side: OrderSide = OrderSide.BUY,
    quantity: float = 100,
    price: float = 10.0,
) -> Trade:
    return Trade(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        timestamp=ts if ts is not None else _ts(2024, 1, 16),
        commission=0.0,
    )


def _make_min_engine(guard: LookaheadGuard | None = None) -> LiveEngine:
    """最小引擎（关闭全部保障组件，直接驱动 _process_signal / on_trade）"""
    return LiveEngine(
        engine_id="test_guard_min",
        risk_config=RiskConfig(max_positions=10, max_position_pct=0.5),
        enable_risk_manager=False,
        enable_guardian=False,
        enable_state_store=False,
        enable_scheduler=False,
        enable_event_bus=False,
        lookahead_guard=guard,
    )


def _make_full_engine(tmp_path, guard: LookaheadGuard | None = None) -> LiveEngine:
    """完整可启动引擎（start/stop 全链路）"""
    engine = LiveEngine(
        engine_id="test_guard_full",
        risk_config=RiskConfig(),
        state_store=StateStore(root=str(tmp_path), store_id="guard_test"),
        enable_scheduler=False,
        lookahead_guard=guard,
    )
    broker = PaperBroker(initial_capital=1_000_000.0)
    broker.connect()
    engine.set_broker(broker)
    engine.set_portfolio(LivePortfolio(initial_capital=1_000_000.0))
    engine.add_strategy(MACrossStrategy())
    return engine


# ═══════════════════════════════════════════
#  1. LiveEngine 接线
# ═══════════════════════════════════════════

class TestLiveEngineWiring:
    """任务42：LiveEngine 防未来校验器接线"""

    def test_默认启用(self) -> None:
        engine = LiveEngine()
        assert engine._lookahead_guard is not None
        assert engine._enable_lookahead_guard is True

    def test_可显式关闭(self) -> None:
        engine = LiveEngine(enable_lookahead_guard=False)
        assert engine._lookahead_guard is None

    def test_注入自定义guard(self) -> None:
        guard = LookaheadGuard()
        engine = LiveEngine(lookahead_guard=guard)
        assert engine._lookahead_guard is guard

    def test_start_reset_guard(self, tmp_path) -> None:
        """start() 应重置 guard（每次启动全新会话），预置记录被清空"""
        guard = LookaheadGuard()
        guard.record_signal("000001.SZ", "2024-01-15", "BUY", data_end="2024-01-15")
        engine = _make_full_engine(tmp_path, guard=guard)
        assert guard.stats["signals_recorded"] == 1
        assert engine.start() is True
        # start 已 reset：预置记录清空
        assert guard.stats["signals_recorded"] == 0
        assert engine._order_signal_dates == {}
        engine.stop()


# ═══════════════════════════════════════════
#  2. 信号生成阶段校验（_process_signal）
# ═══════════════════════════════════════════

class TestSignalRecording:
    """任务42：信号生成阶段即记录防未来校验（铁律4：因子仅用历史数据）"""

    def test_buy信号记录signal并登记订单映射(self) -> None:
        guard = LookaheadGuard()
        engine = _make_min_engine(guard)
        bar = _make_bar(ts=_ts(2024, 1, 15))
        engine._process_signal(_make_signal(st=SignalType.BUY), bar)

        stats = guard.stats
        assert stats["signals_recorded"] == 1
        assert stats["violations"] == 0
        # 订单映射已登记（order_id → 信号日 2024-01-15）
        assert engine._order_signal_dates, "订单映射不应为空"

    def test_signal日期取自bar且data_end等于信号日(self) -> None:
        guard = LookaheadGuard()
        engine = _make_min_engine(guard)
        engine._process_signal(_make_signal(st=SignalType.BUY), _make_bar(ts=_ts(2024, 1, 15)))
        rec = guard._signals[0]
        assert rec.signal_date == "2024-01-15"
        assert rec.data_end == "2024-01-15"  # 信号基于T日收盘，数据截止日=T日
        assert rec.signal_type == "BUY"

    def test_hold信号不记录(self) -> None:
        guard = LookaheadGuard()
        engine = _make_min_engine(guard)
        engine._process_signal(_make_signal(st=SignalType.HOLD), _make_bar())
        assert guard.stats["signals_recorded"] == 0

    def test_风控拒绝不记录(self) -> None:
        """资金不足被内置风控拒绝 → 未到 record_signal 阶段（信号未生效）"""
        guard = LookaheadGuard()
        engine = _make_min_engine(guard)
        engine.set_portfolio(LivePortfolio(initial_capital=100.0))  # 资金不足以买 1 手
        engine._process_signal(_make_signal(st=SignalType.BUY), _make_bar(close=10.0))
        assert guard.stats["signals_recorded"] == 0


# ═══════════════════════════════════════════
#  3. 成交回报校验（on_trade）
# ═══════════════════════════════════════════

class TestTradeRecording:
    """任务42：成交回报记录防未来校验（铁律3：信号T日→T+1成交）"""

    def test_t_plus_1成交无违规(self) -> None:
        guard = LookaheadGuard()
        engine = _make_min_engine(guard)
        # T日(01-15)收盘生成信号 → T+1日(01-16)成交（用真实 order_id 关联信号映射）
        engine._process_signal(_make_signal(st=SignalType.BUY), _make_bar(ts=_ts(2024, 1, 15)))
        engine.on_trade(_make_trade(order_id=_real_order_id(engine), ts=_ts(2024, 1, 16)))

        stats = guard.stats
        assert stats["trades_recorded"] == 1
        assert stats["violations"] == 0

    def test_同日模拟撮合跳过T1校验不误报(self) -> None:
        """paper模式信号当日收盘生成、当日撮合（trade_date==signal_date）→ 跳过T+1不误报"""
        guard = LookaheadGuard()
        engine = _make_min_engine(guard)
        engine._process_signal(_make_signal(st=SignalType.BUY), _make_bar(ts=_ts(2024, 1, 15)))
        engine.on_trade(_make_trade(order_id=_real_order_id(engine), ts=_ts(2024, 1, 15)))  # 同日成交
        stats = guard.stats
        assert stats["trades_recorded"] == 1
        assert stats["violations"] == 0

    def test_成交早于信号日真违规(self) -> None:
        """成交日早于信号日（未来函数）必须被 guard 捕获"""
        guard = LookaheadGuard()
        engine = _make_min_engine(guard)
        # 信号日 01-16，成交却发生在 01-15（早于信号日）
        engine._process_signal(_make_signal(st=SignalType.BUY), _make_bar(ts=_ts(2024, 1, 16)))
        engine.on_trade(_make_trade(order_id=_real_order_id(engine), ts=_ts(2024, 1, 15)))
        assert guard.stats["violations"] == 1

    def test_无信号关联的成交仅记录(self) -> None:
        """手动补单/无信号映射的成交（如风控强平）→ 仅记录 trade，不触发铁律3"""
        guard = LookaheadGuard()
        engine = _make_min_engine(guard)
        engine.on_trade(_make_trade(order_id="manual_001", ts=_ts(2024, 1, 16)))
        stats = guard.stats
        assert stats["trades_recorded"] == 1
        assert stats["violations"] == 0


# ═══════════════════════════════════════════
#  4. 停止统一校验（stop / _run_lookahead_assert）
# ═══════════════════════════════════════════

class TestStopAssert:
    """任务42：stop() 停止时统一 assert_pass（规则4）"""

    def test_无违规通过且正常停机(self, tmp_path) -> None:
        guard = LookaheadGuard()
        engine = _make_full_engine(tmp_path, guard=guard)
        assert engine.start() is True
        # 驱动一次信号+次日成交（无违规）
        engine._process_signal(_make_signal(SignalType.BUY), _make_bar(ts=_ts(2024, 1, 15)))
        engine.on_trade(_make_trade(ts=_ts(2024, 1, 16)))
        engine.stop()
        assert engine._state == EngineState.STOPPED
        assert engine._stats["errors"] == 0
        assert guard.stats["violations"] == 0

    def test_记录模式违规留痕不阻断停机(self, tmp_path) -> None:
        """记录模式：违规在 stop 时统一校验 → 留痕（errors+1/last_error），停机不被阻断"""
        guard = LookaheadGuard()
        engine = _make_full_engine(tmp_path, guard=guard)
        assert engine.start() is True
        # start 已 reset；在运行中注入真违规（T日信号T日成交，模拟未来函数场景）
        guard.record_trade(
            symbol="000001.SZ", trade_date="2024-01-15",
            signal_date="2024-01-15", price_type="OPEN", side="BUY",
        )
        engine.stop()
        assert engine._state == EngineState.STOPPED  # 停机不被阻断
        assert engine._stats["errors"] == 1          # 违规留痕
        assert "lookahead_violation" in str(engine._stats.get("last_error", ""))

    def test_guard关闭时_stop不校验(self, tmp_path) -> None:
        engine = _make_full_engine(tmp_path, guard=None)
        engine._enable_lookahead_guard = False
        engine._lookahead_guard = None
        assert engine.start() is True
        engine.stop()
        assert engine._state == EngineState.STOPPED
        assert engine._stats["errors"] == 0


# ═══════════════════════════════════════════
#  5. 工厂配置消费（create_live_engine）
# ═══════════════════════════════════════════

class TestFactoryConfig:
    """任务42：create_live_engine 消费 lookahead_guard.enabled / strict_mode 段"""

    @staticmethod
    def _base_config() -> dict:
        return {
            "live": {"name": "guard_test", "initial_capital": 1_000_000, "trade_mode": "paper"},
            "broker": {"type": "paper", "initial_capital": 1_000_000},
            "market_data": {"source": "akshare", "frequency": "daily"},
            "strategy": {"name": "ma_cross"},
            "position": {},
            "risk_control": {"enabled": False},
            "state_store": {"enabled": False},
        }

    def test_消费enabled_strict(self) -> None:
        config = self._base_config()
        config["lookahead_guard"] = {"enabled": True, "strict_mode": True}
        engine = create_live_engine(config)
        assert engine._lookahead_guard is not None
        assert engine._lookahead_guard.strict is True

    def test_消费strict_mode_false(self) -> None:
        config = self._base_config()
        config["lookahead_guard"] = {"enabled": True, "strict_mode": False}
        engine = create_live_engine(config)
        assert engine._lookahead_guard is not None
        assert engine._lookahead_guard.strict is False

    def test_消费enabled_false(self) -> None:
        config = self._base_config()
        config["lookahead_guard"] = {"enabled": False}
        engine = create_live_engine(config)
        assert engine._lookahead_guard is None

    def test_缺省配置默认启用strict(self) -> None:
        """配置无 lookahead_guard 段 → 默认启用且 strict=True（规则4强制防未来）"""
        engine = create_live_engine(self._base_config())
        assert engine._lookahead_guard is not None
        assert engine._lookahead_guard.strict is True
