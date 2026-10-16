# 文件名：myquant/live/engine.py
"""
engine.py - 实盘引擎（协调 data_feed/strategy/broker/portfolio）
====================================================================
实盘交易引擎，负责协调行情源、策略、券商、持仓管理四大组件。

核心能力:
  1. 组件协调 — data_feed / strategy / broker / portfolio 统一调度
  2. 行情驱动 — 行情到来时触发策略on_bar，生成信号
  3. 信号转订单 — 策略信号经风控检查后提交给broker
  4. 成交回报 — broker成交后更新portfolio和策略统计
  5. 风控检查 — 双层风控：内置资金/仓位检查 + RiskManager 完整规则库（H2修复）
  6. 生命周期管理 — start/stop/pause/resume
  7. 状态监控 — 运行状态、健康检查、统计信息
  8. 多券商并行 — 统一走 BrokerManager 执行通道（双券商路由/主备切换/合并视图）
  9. 为多策略、多账户预留接口

多券商接入（任务27，双券商并行）:
  - 引擎唯一执行通道 = BrokerManager（brokers/manager.py）
  - set_broker(单券商) → 自动包装成单券商 BrokerManager（向后兼容，现有调用零破坏）
  - set_broker_manager(manager) → 显式多券商并行入口（QMT主 + Ptrade备 等）
  - 下单/查单/持仓/账户/回调全部经 broker_manager 路由与合并
  - 行情广播 push_market_data 驱动各券商撮合（各券商只撮合自己账上的订单）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（多策略/多账户/多券商回调全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — T日信号T+1成交，只使用当前及历史数据

防未来函数说明:
  - 策略on_bar接收的bar视为"已收盘"数据，信号在收盘后生成
  - T日生成的信号，记录信号时间，由broker在下一交易日开盘成交（v1市价单即时成交模拟）
  - 风控检查只使用当前已知的资金和持仓，不预判未来
  - 行情数据时间戳严格校验，不接受未来时间的数据
  - 策略通过StrategyContext访问数据，context只包含截至当前bar的历史数据
  - 行情广播 push_market_data 只传已发生 bar，主备切换只依据已发生健康/拒单

v1实现范围:
  - 单策略多券商（多券商经 BrokerManager 并行）
  - 行情驱动（data_feed回调 → push_market_data）
  - 信号转订单（市价单）
  - 双层风控（内置资金/仓位检查 + RiskManager完整规则库，H2修复）
  - 实盘保障接线（H2修复）：风控管理器/状态持久化崩溃恢复/调度巡检日报/风控守护/事件总线联动
  - 成交回报更新portfolio
  - 生命周期管理
  - 状态监控

TODO(后续迭代):
  - 多策略管理
  - 多账户管理
  - 复杂风控（止损/止盈/波动率调整）
  - 订单管理（挂单/撤单/改单）
  - 交易时段控制
  - 信号延迟成交（T+1严格实现）
  - 策略级券商路由（策略A→券商X，策略B→券商Y）

依赖: typing, time, datetime, myquant.models, myquant.core.logger,
      myquant.strategies.base, myquant.live.brokers
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

# ── H2修复：实盘保障组件接线（风控/守护/状态/调度/事件总线）──
from pathlib import Path as _Path
from typing import Any, Callable, Dict, List, Optional, Set

import pandas as pd

from myquant.core.date_utils import format_bar_date, get_date_part
from myquant.core.event_bus import (
    ChainEvent,
    EventType,
    OrderEvent,
    PositionUpdateEvent,
    SignalEvent,
    SystemErrorEvent,
    get_event_bus,
)
from myquant.core.logger import get_logger
from myquant.core.lookahead_guard import LookaheadGuard
from myquant.engine.base import BaseEngine
from myquant.live.brokers import BrokerManager
from myquant.live.emergency_liquidation import EmergencyLiquidationService
from myquant.live.scheduler import LiveScheduler, attach_engine
from myquant.live.state_store import (
    SnapshotNotFoundError,
    StateStore,
    attach_snapshot,
    capture_live_state,
    restore_live_state,
)
from myquant.models.market_data import Bar, BarFrequency, MarketEvent
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType, Trade
from myquant.risk import (
    DataQualityRiskConfig,
    DecisionType,
    RiskContext,
    RiskGuardian,
    RiskManager,
)
from myquant.strategies.base import Signal, StrategyBase, StrategyContext

logger = get_logger("live.engine")


# ═══════════════════════════════════════════════
#  引擎状态枚举
# ═══════════════════════════════════════════════

class EngineState:
    """引擎状态"""
    INIT = "INIT"           # 初始化
    RUNNING = "RUNNING"     # 运行中
    PAUSED = "PAUSED"       # 已暂停
    STOPPED = "STOPPED"     # 已停止
    ERROR = "ERROR"         # 错误


# ═══════════════════════════════════════════════
#  风控配置
# ═══════════════════════════════════════════════

class RiskConfig:
    """
    风控配置

    v1基础风控参数，后续迭代可扩展。
    """
    def __init__(
        self,
        max_position_pct: float = 0.20,       # 单票最大仓位比例
        max_positions: int = 5,                 # 最大持仓数
        min_cash_ratio: float = 0.05,           # 最低现金保留比例
        max_order_amount: float = 0.0,          # 单笔最大金额（0=不限制）
        enable_stop_loss: bool = False,          # 是否启用止损（v1不实现）
        enable_take_profit: bool = False,        # 是否启用止盈（v1不实现）
    ):
        self.max_position_pct = max_position_pct
        self.max_positions = max_positions
        self.min_cash_ratio = min_cash_ratio
        self.max_order_amount = max_order_amount
        self.enable_stop_loss = enable_stop_loss
        self.enable_take_profit = enable_take_profit


# ═══════════════════════════════════════════════
#  LiveEngine - 实盘引擎
# ═══════════════════════════════════════════════

class LiveEngine(BaseEngine):
    """
    实盘引擎（协调 data_feed/strategy/broker/portfolio）

    继承 BaseEngine（engine/base.py），实现统一生命周期抽象方法：
      - on_init(config)      — 初始化钩子（适配现有 setter 模式）
      - on_market_event(evt) — 市场事件钩子（适配现有 on_market_data(symbol, bar)）
      - on_stop()            — 停止钩子（适配现有 stop() 清理逻辑）
      - run()                — 主循环入口（调用现有 start()）

    现有实盘 API（set_data_feed/set_broker/add_strategy/start/stop/on_market_data）
    全部保留，零破坏。BaseEngine 的 dispatch_event(event) 可作为统一事件入口，
    内部桥接到 on_market_data。

    v1单策略多券商实现（多券商经 BrokerManager 并行），为多策略多账户预留接口。

    使用方式（单券商，向后兼容）:
        from myquant.live.engine import LiveEngine, RiskConfig
        from myquant.live.data_feed import MarketDataFeed
        from myquant.live.brokers import PaperBroker
        from myquant.live.portfolio import LivePortfolio
        from myquant.strategies.ma_strategy import MAStrategy

        feed = MarketDataFeed(source="akshare", frequency="daily")
        broker = PaperBroker(initial_capital=1_000_000)
        portfolio = LivePortfolio(initial_capital=1_000_000)
        strategy = MAStrategy(short_window=5, long_window=20)

        engine = LiveEngine(risk_config=RiskConfig())
        engine.set_data_feed(feed)
        engine.set_broker(broker)          # 单券商自动包装
        engine.set_portfolio(portfolio)
        engine.add_strategy(strategy)
        engine.start()

    使用方式（双券商并行）:
        from myquant.live.brokers import BrokerManager, QmtBroker, PtradeBroker

        mgr = BrokerManager()
        mgr.register_broker(QmtBroker(account_id="Q1", mode="SIM", initial_capital=1_000_000),
                            alias="qmt", role="primary")
        mgr.register_broker(PtradeBroker(account_id="P1", mode="SIM", initial_capital=1_000_000),
                            alias="ptrade", role="secondary")
        mgr.connect_all()
        engine.set_broker_manager(mgr)     # 显式多券商并行入口
    """

    def __init__(
        self,
        risk_config: Optional[RiskConfig] = None,
        engine_id: str = "default",
        realtime_poll_interval: int = 3,
        warmup_bars: int = 200,  # T2-1：60→200，覆盖 RSRS(period18+norm60=78) 及其他递归指标种子需求
        enable_warmup: bool = True,
        # ── H2修复：实盘保障组件注入（全部可选，默认自动创建，向后兼容无参调用）──
        risk_manager: Optional[Any] = None,
        state_store: Optional[Any] = None,
        scheduler: Optional[Any] = None,
        guardian: Optional[Any] = None,
        lookahead_guard: Optional[LookaheadGuard] = None,
        enable_risk_manager: bool = True,
        enable_state_store: bool = True,
        enable_scheduler: bool = True,
        enable_guardian: bool = True,
        enable_event_bus: bool = True,
        enable_lookahead_guard: bool = True,
        auto_recover: bool = False,
        monitor_only: bool = False,
        snapshot_interval: float = 300.0,
        patrol_interval: float = 60.0,
        trade_mode: str = "paper",
        # ── KP-P4-02：订单/成交关系账（可选注入，None 时成交仅内存，不落库）──
        trade_ledger: Optional[Any] = None,
    ):
        """
        初始化实盘引擎

        Parameters
        ----------
        risk_config : RiskConfig | None
            风控配置（None则使用默认配置）
        engine_id : str
            引擎ID（为多引擎预留，v1默认"default"）
        realtime_poll_interval : int
            实时模式后台轮询间隔（秒），默认3秒（腾讯接口限流友好）
        warmup_bars : int
            启动时预热历史bar数量（默认60根，保证策略有足够窗口）
        enable_warmup : bool
            是否启用历史预热（默认True；False时运行中累积）
        risk_manager : RiskManager | None
            风控管理器（H2修复）。None时自动创建默认实例；
            订单提交到券商前强制 check_order（完整规则库+数据质量联动）
        state_store : StateStore | None
            状态持久化（H2修复）。None时自动创建；定时快照+停止快照，
            auto_recover=True 时启动自动崩溃恢复
        scheduler : LiveScheduler | None
            调度器（H2修复）。None时start()内自动创建；
            挂载巡检/收盘日报/风控守护/定时快照
        guardian : RiskGuardian | None
            风控守护（H2修复）。None时自动创建（依赖risk_manager）；
            巡检任务定期evaluate，PAUSE→风控暂停，强平决策→CRITICAL留痕
        lookahead_guard : LookaheadGuard | None
            防未来函数校验器（任务42）。None时自动创建；
            信号生成 record_signal / 成交回报 record_trade / 停止时 assert_pass，
            严格模式违规立即抛 LookaheadError（记录模式停止时统一校验）
        enable_risk_manager / enable_state_store / enable_scheduler /
        enable_guardian / enable_event_bus / enable_lookahead_guard : bool
            各保障组件开关（默认全True；显式False可完全关闭，测试/极简场景用）
        auto_recover : bool
            启动时是否自动恢复上次状态（默认False，对齐配置模板
            auto_recover_on_start；真钱实盘建议True）
        monitor_only : bool
            监控模式（默认False）：生成并记录信号，但不提交订单
        snapshot_interval : float
            状态快照间隔秒数（默认300）
        patrol_interval : float
            巡检/守护间隔秒数（默认60）
        trade_ledger : TradeLedger | None
            KP-P4-02：订单/成交关系账（None 时成交仅存内存不落库；
            注入后 on_order_update 逐笔订单、on_trade 逐笔成交同步落库，长期归档）
        """
        # 初始化 BaseEngine（统一生命周期状态：_initialized/_running/_stopped/_event_count）
        super().__init__(name=f"live_{engine_id}")

        self._engine_id = engine_id
        self._risk_config = risk_config or RiskConfig()
        self._realtime_poll_interval = max(1, int(realtime_poll_interval))
        self._warmup_bars = max(0, int(warmup_bars))
        self._enable_warmup = bool(enable_warmup)
        # 本地日线parquet目录（历史预热优先读本地，配置外置；create_live_engine 消费 market_data.local_history_dir）
        self._local_history_dir: str = "datasets/processed"

        # 组件
        self._data_feed = None
        self._broker = None                 # 当前主券商实例（兼容外部访问）
        self._broker_manager: Optional[BrokerManager] = None  # 唯一执行通道（任务27）
        self._portfolio = None
        self._strategies: List[StrategyBase] = []
        # ── BUG-04：guardian 一键平仓服务（延迟装配，默认 dry_run 不下真单）──
        self._emergency_service: Optional[Any] = None

        # 策略上下文
        self._context: Optional[StrategyContext] = None

        # 状态
        self._state = EngineState.INIT
        self._start_time = 0.0
        self._pause_time = 0.0
        # 0.2/决策点4：暂停来源记录（隔离复位区分"谁置位"，resume 时只清自己置位的）
        self._pause_source: str = ""

        # 行情历史（为策略提供历史数据）
        self._bar_history: Dict[str, List[Bar]] = {}
        self._max_history_bars = 500  # 最多保留500根bar

        # 信号记录
        self._signals: List[Signal] = []
        self._max_signals = 1000

        # 订单记录
        self._orders: List[Order] = []
        self._max_orders = 1000

        # ── BUG-03：订单决策锁 + BUY 待成交订单占位（TOCTOU 竞态防护）──
        # data_feed 回调单线程串行，但调度/成交回报/外部调用可并发进入；
        # 决策（读 portfolio 风控检查→数量→RiskManager 检查）与 BUY 占位登记
        # 在同一锁域内原子完成，锁内不做网络 I/O。
        self._order_lock = threading.RLock()
        self._pending_buy_order_ids: Set[str] = set()

        # 统计
        self._stats = {
            "total_bars": 0,
            "total_signals": 0,
            "buy_signals": 0,
            "sell_signals": 0,
            "total_orders": 0,
            "filled_orders": 0,
            "rejected_orders": 0,
            "total_trades": 0,
            "risk_rejected": 0,
            "dedup_skipped": 0,
            "bar_dedup_skipped": 0,
            "last_bar_time": 0.0,
            "last_signal_time": 0.0,
            "errors": 0,
        }

        # 回调
        self._on_signal_callbacks: List[Callable[[Signal], None]] = []
        self._on_order_callbacks: List[Callable[[Order], None]] = []
        self._on_trade_callbacks: List[Callable[[Trade], None]] = []

        # KP-P4-02：订单/成交关系账（可选；None 时成交仅内存，不落库）
        self._trade_ledger: Optional[Any] = trade_ledger

        # ═══════════════════════════════════════
        #  H2修复：实盘保障组件（风控/守护/状态/调度/事件总线）
        # ═══════════════════════════════════════
        self._monitor_only = bool(monitor_only)
        self._enable_event_bus_flag = bool(enable_event_bus)
        self._scheduler_enabled = bool(enable_scheduler)
        self._auto_recover = bool(auto_recover)
        self._snapshot_interval = max(30.0, float(snapshot_interval))
        self._patrol_interval = max(10.0, float(patrol_interval))
        self._accountability_interval = max(30.0, float(snapshot_interval))  # KP-P3-08: 券商账本对账周期

        # 风控上下文运行态（只记录"已发生"事实，严格防未来函数）
        self._peak_assets: float = 0.0
        self._order_count_today: int = 0
        self._last_order_time: float = 0.0
        self._last_prices: Dict[str, float] = {}
        self._prev_close_map: Dict[str, float] = {}  # KP-P3-04：昨收（行情快照前收字段，风控涨跌停用）
        # ── 任务46/P1：单日亏损上限（日初资产快照 + 当前交易日，跨日重置）──
        self._day_start_assets: float = 0.0
        self._active_trading_date: str = ""
        self._event_bus = None
        self._event_subscription_ids: List[str] = []
        self._own_scheduler: bool = False   # 调度器是否引擎创建（决定stop归属）
        self._state_recovered: bool = False

        # 1) RiskManager：订单前置检查 + 数据质量事件联动（铁律2：回测实盘共用风控）
        self._risk_manager = None
        if enable_risk_manager:
            self._risk_manager = (
                risk_manager if risk_manager is not None
                else self._create_default_risk_manager()
            )

        # P1-07：运行模式（monitor/paper/sim/live）。真钱(live)下风控 fail-closed：
        # 缺 RiskManager 一律拒单，配置 enabled:false 被强制忽略（见 build_live_engine）。
        self._trade_mode = str(trade_mode).lower()

        # 2) RiskGuardian：健康监控 + 熔断决策（依赖RiskManager）
        # 0.4：传 trade_mode——LIVE 未装配准入守卫时 force_close fail-closed 拒绝
        self._guardian = None
        if enable_guardian and self._risk_manager is not None:
            self._guardian = (
                guardian if guardian is not None
                else RiskGuardian(self._risk_manager, trade_mode=self._trade_mode)
            )

        # 2.5) LookaheadGuard：防未来函数校验（任务42）
        #      信号生成 record_signal / 成交回报 record_trade / 停止时 assert_pass；
        #      回测与实盘共用同一校验器（规则4，铁律2）
        self._enable_lookahead_guard = bool(enable_lookahead_guard)
        self._lookahead_guard: Optional[LookaheadGuard] = None
        self._order_signal_dates: Dict[str, str] = {}  # order_id → 信号日期(YYYY-MM-DD)
        if enable_lookahead_guard:
            self._lookahead_guard = (
                lookahead_guard if lookahead_guard is not None else LookaheadGuard()
            )

        # 2.6) 幂等去重（任务43）：已处理信号 ID 集合（确定性，防桥接重发）
        # T2-2：set → Dict[signal_id, unix_ts]，时间戳用于持久化后按交易日窗口裁剪，
        #       避免每次启动 clear 导致重启重复下单（旧快照里的已处理 id 被恢复）。
        self._processed_signal_ids: Dict[str, float] = {}

        # #T-04(KP-P3-02)：bar 级去重集合——键 (symbol, formatted_date)，
        # 同一根 bar 二次推送直接丢弃且不成交（不依赖墙钟单调，防同日重复撮合）
        self._seen_bars: Set[str] = set()

        # 3) StateStore：崩溃恢复 / 定时快照
        self._state_store = None
        if enable_state_store:
            self._state_store = (
                state_store if state_store is not None
                else StateStore(store_id=str(engine_id))
            )

        # 4) LiveScheduler：start()内组件就绪后延迟创建/挂载
        self._scheduler = scheduler

        logger.info(
            f"实盘引擎初始化: id={engine_id}, "
            f"风控: max_position_pct={self._risk_config.max_position_pct}, "
            f"max_positions={self._risk_config.max_positions}"
        )

    # ═══════════════════════════════════════════
    #  组件设置
    # ═══════════════════════════════════════════

    def set_data_feed(self, data_feed) -> None:
        """设置行情源"""
        self._data_feed = data_feed
        logger.info(f"[engine] 行情源已设置: {type(data_feed).__name__}")

    def set_broker(self, broker) -> None:
        """
        设置券商

        支持两种输入:
          1. BrokerManager 实例（多券商并行）→ 直接作为执行通道
          2. 单个券商（PaperBroker/QmtBroker/PtradeBroker 等）→ 自动包装成
             单券商 BrokerManager（主路由），完全向后兼容现有调用

        重复调用会【整体替换】执行通道（旧券商不残留），避免切换券商时残留。

        Parameters
        ----------
        broker : BaseBroker | BrokerManager
            券商实例或多券商管理器
        """
        if isinstance(broker, BrokerManager):
            self._broker_manager = broker
            self._broker = broker.primary if broker.registrations else None
            logger.info(
                f"[engine] 多券商管理器已设置: 注册数={len(broker.registrations)}, "
                f"主券商={broker.primary_alias}"
            )
        else:
            mgr = BrokerManager()
            alias = getattr(broker, "broker_name", "primary") or "primary"
            mgr.register_broker(broker, alias=alias, role="primary")
            self._broker_manager = mgr
            self._broker = broker
            logger.info(
                f"[engine] 券商已设置并包装为单券商管理器: "
                f"{type(broker).__name__} (alias={alias})"
            )

    def set_broker_manager(self, manager: BrokerManager) -> None:
        """
        显式设置多券商管理器（双券商并行主入口）

        用法:
            mgr = BrokerManager()
            mgr.register_broker(QmtBroker(...), alias="qmt", role="primary")
            mgr.register_broker(PtradeBroker(...), alias="ptrade", role="secondary")
            mgr.connect_all()
            engine.set_broker_manager(mgr)

        Parameters
        ----------
        manager : BrokerManager
            多券商管理器（已注册并连接）
        """
        if not isinstance(manager, BrokerManager):
            raise TypeError(f"manager 必须是 BrokerManager 实例, 当前: {type(manager).__name__}")
        self._broker_manager = manager
        self._broker = manager.primary if manager.registrations else None
        logger.info(
            f"[engine] 多券商管理器已设置: 注册数={len(manager.registrations)}, "
            f"主券商={manager.primary_alias}"
        )

    def set_portfolio(self, portfolio) -> None:
        """设置持仓管理"""
        self._portfolio = portfolio
        logger.info(f"[engine] 持仓管理已设置: {type(portfolio).__name__}")

    def add_strategy(self, strategy: StrategyBase) -> None:
        """
        添加策略

        v1支持单策略，添加多个策略时全部生效（后续迭代完善多策略调度）。
        """
        if strategy not in self._strategies:
            self._strategies.append(strategy)
            logger.info(f"[engine] 策略已添加: {strategy.name}")

    def remove_strategy(self, strategy: StrategyBase) -> None:
        """移除策略"""
        if strategy in self._strategies:
            self._strategies.remove(strategy)
            logger.info(f"[engine] 策略已移除: {strategy.name}")

    # ═══════════════════════════════════════════
    #  BaseEngine 抽象方法实现（统一生命周期适配）
    # ═══════════════════════════════════════════

    def on_init(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        BaseEngine 初始化钩子

        LiveEngine 采用 setter 模式（set_data_feed/set_broker/add_strategy），
        组件在 start() 前已注入。此方法作为 BaseEngine 统一接口的适配层：
          - config 为 None 时：无操作（组件已通过 setter 注入）
          - config 含参数时：可用于覆盖引擎配置（预留扩展）

        实际初始化（连接券商/订阅行情/预热数据）在 start() 中完成。
        """
        if config:
            # 预留：从 config 中提取引擎参数覆盖现有配置
            if "engine_id" in config:
                self._engine_id = str(config["engine_id"])
            if "realtime_poll_interval" in config:
                self._realtime_poll_interval = max(1, int(config["realtime_poll_interval"]))
            if "warmup_bars" in config:
                self._warmup_bars = max(0, int(config["warmup_bars"]))
            if "enable_warmup" in config:
                self._enable_warmup = bool(config["enable_warmup"])
            logger.info(f"[engine] on_init 配置已应用: {list(config.keys())}")

    def on_market_event(self, event: MarketEvent) -> None:
        """
        BaseEngine 市场事件钩子（统一事件入口）

        将 MarketEvent 适配为 LiveEngine 现有的 on_market_data(symbol, bar_dict)。
        支持 event_type="bar"（Bar 对象或 dict）和 "tick"（Tick 对象或 dict）。

        参数:
            event: MarketEvent（event.data 为 Bar/Tick/Snapshot 或 dict）

        防未来函数:
          - event 中的数据视为"已发生"，on_market_data 内部做时间戳校验
          - 不访问未来数据
        """
        if event is None:
            return
        symbol = event.symbol or ""
        data = event.data

        # Bar 对象 → dict
        if isinstance(data, Bar):
            bar_dict = {
                "open": float(data.open),
                "high": float(data.high),
                "low": float(data.low),
                "close": float(data.close),
                "volume": float(data.volume),
                "amount": float(data.amount),
                "timestamp": float(data.timestamp),
            }
        elif isinstance(data, dict):
            bar_dict = dict(data)
        elif data is not None:
            # Tick/Snapshot 等其他对象：尝试提取通用字段
            bar_dict = {
                "open": float(getattr(data, "open", 0) or 0),
                "high": float(getattr(data, "high", 0) or 0),
                "low": float(getattr(data, "low", 0) or 0),
                "close": float(getattr(data, "last_price", getattr(data, "close", 0)) or 0),
                "volume": float(getattr(data, "volume", 0) or 0),
                "amount": float(getattr(data, "amount", 0) or 0),
                "timestamp": float(getattr(data, "timestamp", event.timestamp) or event.timestamp),
            }
        else:
            # data 为空时，从 event.extra 或 event 本身提取
            bar_dict = dict(event.extra) if event.extra else {}
            if not bar_dict:
                logger.warning(f"[engine] on_market_event 无有效数据: event_id={event.event_id}")
                return

        # 确保 symbol 不为空
        if not symbol:
            symbol = bar_dict.get("symbol", "")
        if not symbol:
            logger.warning(f"[engine] on_market_event 缺少 symbol: event_id={event.event_id}")
            return

        self.on_market_data(symbol, bar_dict)

    def on_stop(self) -> None:
        """
        BaseEngine 停止钩子

        实际停止逻辑（断开券商/停止行情/策略 on_stop）在 stop() 中完成。
        此方法作为 BaseEngine 统一接口的适配层，stop() 会设置 BaseEngine 状态标志。
        """
        pass  # stop() 已包含完整清理逻辑，此处为接口适配

    def run(self) -> Dict[str, Any]:
        """
        BaseEngine 主循环入口

        调用现有 start() 启动引擎，返回引擎状态。
        实时模式下 start() 启动后台轮询线程，run() 立即返回；
        调用方可通过 is_running / get_status / health_check 监控运行状态。

        返回:
            Dict: 引擎状态（get_status() 结果）
        """
        success = self.start()
        if not success:
            logger.error("[engine] run() 启动失败")
        return self.get_status()

    # ═══════════════════════════════════════════
    #  生命周期管理
    # ═══════════════════════════════════════════

    def start(self) -> bool:
        """
        启动引擎

        流程:
          1. 检查组件是否齐全
          2. 连接broker和data_feed
          3. 初始化策略（on_init）
          4. 注册行情回调
          5. 启动data_feed轮询

        Returns
        -------
        bool : True=启动成功
        """
        if self._state == EngineState.RUNNING:
            logger.warning("[engine] 引擎已在运行中")
            return False

        # 检查组件
        if not self._check_components():
            self._state = EngineState.ERROR
            return False

        # KP-P5-07: 启动日志记录配置版本（config_version，配置变更留痕）
        logger.info(f"[engine] 启动: config_version={getattr(self, '_config_version', '')!r}")

        try:
            # ── 任务42：防未来函数校验器重置（每次启动全新会话，回测/实盘同源）──
            if self._lookahead_guard:
                self._lookahead_guard.reset()
                logger.info(f"[engine] 防未来函数校验器已启用: {self._lookahead_guard}")
                self._order_signal_dates.clear()

            # ── 任务43：幂等去重集合重置（每次启动全新会话）──
            self._processed_signal_ids.clear()

            # ── #T-04(KP-P3-02)：bar 级去重集合重置（每次启动全新会话）──
            self._seen_bars.clear()

            # ── H2修复：事件总线联动（风控订阅数据质量事件，行情流式清洗联动）──
            self._setup_event_bus()

            # ── H2修复：崩溃恢复（仅回写"已发生"的持仓/资金，不生成任何信号）──
            if self._auto_recover:
                self._recover_state()

            # 连接全部券商（BrokerManager 统一编排，含主备 failover 能力）
            if self._broker_manager and hasattr(self._broker_manager, 'connect_all'):
                self._broker_manager.connect_all()

            # 注册broker回调（成交回报/订单更新；BrokerManager 透传到全部券商）
            if self._broker_manager and hasattr(self._broker_manager, 'register_trade_callback'):
                self._broker_manager.register_trade_callback(self.on_trade)
            if self._broker_manager and hasattr(self._broker_manager, 'register_order_callback'):
                self._broker_manager.register_order_callback(self.on_order_update)

            # ── KP-P3-01/T-01：启动时对存量持仓做一次 T+1 解冻初始化 ──
            # 释放重启前残留的内存冻结（当日买入冻结在内存 _t1_holdings，重启后本应清零），
            # 确保已持有仓位在会话开始时即可卖出；日终解冻由调度器 15:30 日切任务负责。
            # 防未来：只操作【已发生】的持仓状态，不生成任何预测数据。
            if self._broker_manager and hasattr(self._broker_manager, 'settle_t1_all'):
                try:
                    self._broker_manager.settle_t1_all()
                except Exception as e:  # noqa: BLE001 - 解冻失败不阻断引擎启动
                    logger.warning(f"[engine] 启动 T+1 解冻失败（不阻断启动）: {e}")

            # ── T1-2：启动持仓断言（fail-closed，保命项）──
            # 启动时先从 broker 拉真实持仓对账；若 portfolio 空仓但 broker 有持仓，
            # 说明快照丢失/漂移，引擎会误以为空仓而重复开仓——直接拒绝启动，人工介入。
            # 全新启动（portfolio 空 AND broker 也空）正常放行。
            self._assert_startup_positions()

            # 连接data_feed
            if hasattr(self._data_feed, 'connect'):
                self._data_feed.connect()

            # 初始化策略上下文（executor = broker_manager，接口全覆盖）
            # T2-1：data 先空，warmup 完成后由 _sync_context_data 填充。
            self._context = StrategyContext(
                data={},
                portfolio=self._portfolio,
                executor=self._broker_manager,
                config={"engine_id": self._engine_id},
            )

            # 注册行情回调（需先算订阅标的，warmup 与实时订阅共用同一份 symbols）
            symbols: List[str] = []
            if self._data_feed:
                symbols = self._get_watch_symbols()
                for symbol in symbols:
                    self._data_feed.subscribe(symbol, self.on_market_data)

            # ── T2-1 修复：on_init 时序颠倒 ──
            # 原顺序：on_init(data={}) → warmup 填数据 → 回放。导致 RSRS 等依赖
            # context.data 的指标缓存只在 on_init 写入，空上下文空循环 → 恒 NaN 不发信号。
            # 新顺序：
            #   1) warmup(replay=False)：先把历史 bar 填进 context.data 与 _bar_history，不回放
            #   2) on_init(context)：此时 context.data 就绪，RSRS 缓存能基于历史数据正确预计算
            #   3) 逐 symbol replay：on_init 完成、缓存就绪后，再逐根 on_bar 重建策略状态
            # 防未来：warmup 只取启动时刻【已发生】的历史 bar，on_init 看到的就是"跑到启动日"的数据，不引入未来。
            if self._enable_warmup and self._warmup_bars > 0 and symbols:
                self._warmup_history(symbols, replay=False)

            # 初始化策略（context.data 已被 warmup 填充，指标缓存可正确预计算）
            for strategy in self._strategies:
                strategy.on_init(self._context)

            # on_init 完成后逐 symbol 回放历史 bar，重建跨 bar 状态（prev_diff / _rsrs_cache 读取路径等）
            if self._enable_warmup and self._warmup_bars > 0 and symbols:
                for symbol in symbols:
                    bar_objs = self._bar_history.get(symbol, [])
                    if bar_objs:
                        self._replay_warmup_strategies(symbol, bar_objs)

            # ── BUG-04：guardian 一键平仓接线（broker 已就绪，先于调度器巡检）──
            self._setup_force_close()

            # ── H2修复：调度器挂载启动（巡检/日报/风控守护/定时快照）──
            self._setup_scheduler()

            self._state = EngineState.RUNNING
            self._initialized = True   # BaseEngine 状态同步
            self._running = True       # BaseEngine 状态同步
            self._start_time = time.time()

            # C项：实时模式自动启动后台轮询（日线模式保持原行为，兼容LiveScheduler手动驱动）
            if self._is_realtime_feed() and self._data_feed:
                self._data_feed.start(poll_interval=self._realtime_poll_interval)
                logger.info(
                    f"[engine] 实时行情轮询已启动: interval={self._realtime_poll_interval}s, "
                    f"backend={getattr(self._data_feed, 'active_backend', '')}"
                )

            logger.info(
                f"[engine] 引擎已启动: 策略数={len(self._strategies)}, "
                f"关注标的={self._get_watch_symbols()}, "
                f"realtime={self._is_realtime_feed()}"
            )
            return True

        except Exception as e:
            logger.error(f"[engine] 启动失败: {e}")
            self._stats["errors"] += 1
            self._state = EngineState.ERROR
            return False

    def stop(self) -> None:
        """停止引擎"""
        if self._state not in (EngineState.RUNNING, EngineState.PAUSED):
            return

        try:
            # 停止策略
            if self._context:
                for strategy in self._strategies:
                    strategy.on_stop(self._context)

            # 停止data_feed
            if self._data_feed and hasattr(self._data_feed, 'stop'):
                self._data_feed.stop()
            # C项：断开data_feed（原只stop不断开）
            if self._data_feed and hasattr(self._data_feed, 'disconnect'):
                try:
                    self._data_feed.disconnect()
                except Exception as e:
                    logger.warning(f"[engine] data_feed断开异常: {e}")

            # ── H2修复：保存停止快照（供下次启动恢复/审计）──
            self._save_state(tag="shutdown")

            # ── H2修复：停止调度器（仅停止引擎自建者；外部注入由调用方管理）──
            if self._scheduler is not None and self._own_scheduler:
                try:
                    self._scheduler.stop()
                except Exception as e:
                    logger.warning(f"[engine] 调度器停止异常: {e}")

            # 断开全部券商（BrokerManager 统一编排）
            if self._broker_manager and hasattr(self._broker_manager, 'disconnect_all'):
                self._broker_manager.disconnect_all()

            # ── 任务42：停止时统一防未来校验（规则4：回测实盘都必须 assert_pass）──
            # strict 模式违规在记录时已抛 LookaheadError（由上层捕获）；
            # 记录模式在此统一校验，违规仅留痕告警，不阻断正常停机（停机本身是安全操作）
            try:
                self._run_lookahead_assert()
            except Exception as e:
                logger.error(f"[engine] 防未来函数校验未通过: {e}")
                self._stats["errors"] += 1
                self._stats["last_error"] = f"lookahead_violation: {e}"

            self._state = EngineState.STOPPED
            self._running = False    # BaseEngine 状态同步
            self._stopped = True     # BaseEngine 状态同步
            logger.info(
                f"[engine] 引擎已停止: 运行时长={time.time()-self._start_time:.1f}秒, "
                f"信号数={self._stats['total_signals']}, "
                f"成交数={self._stats['total_trades']}"
            )

        except Exception as e:
            logger.error(f"[engine] 停止异常: {e}")
            self._stats["errors"] += 1

    def _run_lookahead_assert(self) -> bool:
        """
        防未来函数统一校验（停止时调用，任务42）

        记录模式：违规统一在此抛 LookaheadError（由调用方决定处理）；
        无违规返回 True。guard 未启用时直接通过。

        Returns
        -------
        bool : True=校验通过

        Raises
        ------
        LookaheadError : 存在未来函数违规且 guard 启用时抛出
        """
        if not self._lookahead_guard:
            return True

        guard_stats = self._lookahead_guard.stats
        logger.info(
            f"[engine] 防未来函数校验: 信号={guard_stats['signals_recorded']}, "
            f"成交={guard_stats['trades_recorded']}, "
            f"违规={guard_stats['violations']}"
        )
        return self._lookahead_guard.assert_pass()

    def pause(self, reason: str = "手动暂停") -> None:
        """
        暂停引擎（实时模式同时停止行情轮询，节能）。

        0.2/决策点4：风控状态为权威、引擎为投影——同步置风控暂停
        （pause_for_engine 隔离语义，不碰数据质量暂停标记），并记录
        _pause_source 供隔离复位区分"谁置位"。
        """
        if self._state != EngineState.RUNNING:
            return
        self._state = EngineState.PAUSED
        self._pause_time = time.time()
        self._pause_source = reason
        # 0.2：同步风控暂停（隔离置位，仅当风控当前可开仓时生效）
        if self._risk_manager is not None:
            try:
                self._risk_manager.pause_for_engine(reason)
            except Exception as e:
                logger.warning(f"[engine] pause 同步风控暂停失败: {e}")
        # C项：实时模式暂停feed轮询（节能，避免无效请求）
        if self._is_realtime_feed() and self._data_feed and hasattr(self._data_feed, 'stop'):
            try:
                self._data_feed.stop()
            except Exception as e:
                logger.warning(f"[engine] pause时feed.stop异常: {e}")
        logger.info(f"[engine] 引擎已暂停: {reason}")

    def resume(self, reason: str = "引擎恢复") -> None:
        """
        恢复引擎（实时模式同时恢复行情轮询）。

        0.2/A1 硬约束：**禁止调用 risk_manager.resume()**（会清除所有数据质量
        暂停标记）。改为 resume_engine_holds 隔离复位（只回滚引擎自己置位的）；
        若风控仍有其他暂停（数据质量/熔断/手动），引擎保持 PAUSED
        （风控状态为权威、引擎为投影，决策点4）。
        """
        if self._state != EngineState.PAUSED:
            return
        if self._risk_manager is not None:
            try:
                released = self._risk_manager.resume_engine_holds(reason)
                if released:
                    self._pause_source = ""
                if not self._risk_manager.can_open_position():
                    logger.warning(
                        "[engine] 风控仍处于暂停（其他原因），引擎保持 PAUSED"
                    )
                    return
            except Exception as e:
                logger.warning(f"[engine] resume 同步风控复位失败: {e}")
        self._state = EngineState.RUNNING
        # C项：实时模式恢复feed轮询
        if self._is_realtime_feed() and self._data_feed and hasattr(self._data_feed, 'start'):
            try:
                self._data_feed.start(poll_interval=self._realtime_poll_interval)
            except Exception as e:
                logger.warning(f"[engine] resume时feed.start异常: {e}")
        logger.info(f"[engine] 引擎已恢复: {reason}")

    @property
    def state(self) -> str:
        """引擎状态"""
        return self._state

    @property
    def is_running(self) -> bool:
        """是否运行中"""
        return self._state == EngineState.RUNNING

    @property
    def data_feed(self) -> Optional[Any]:
        """行情源实例（只读访问，H2修复，供入口主循环驱动）"""
        return self._data_feed

    @property
    def risk_manager(self) -> Optional[Any]:
        """风控管理器实例（只读访问，H2修复）"""
        return self._risk_manager

    # ═══════════════════════════════════════════
    #  行情驱动（核心）
    # ═══════════════════════════════════════════

    def on_market_data(self, symbol: str, bar: Dict[str, Any]) -> None:
        """
        行情回调（供data_feed调用）

        流程:
          1. bar dict 转 Bar 对象
          2. 防未来函数校验（时间戳）
          3. 更新历史行情
          4. 更新portfolio市值
          5. 调用策略on_bar
          6. 处理策略信号
          7. 风控检查
          8. 提交订单

        Parameters
        ----------
        symbol : str
            标的代码
        bar : Dict[str, Any]
            K线数据（dict格式，来自data_feed）
        """
        if self._state != EngineState.RUNNING:
            return

        # #T-04(KP-P3-02)：bar 级去重——同一标的同一日期的 bar 只处理首根，
        # 二次推送直接丢弃且不进入撮合（不产生成交）。键用 bar 周期归属日期
        # （data_feed 已把 timestamp 改为周期归属时间，bar["date"] 即格式化日期）。
        try:
            _dedup_key = (symbol, bar["date"])
        except (KeyError, TypeError):
            _dedup_key = (symbol, None)
        if _dedup_key in self._seen_bars:
            self._stats["bar_dedup_skipped"] += 1
            logger.debug(
                f"[engine] 同根bar去重丢弃: {symbol} date={bar.get('date')}"
            )
            return
        self._seen_bars.add(_dedup_key)

        try:
            # 1. bar dict 转 Bar 对象
            bar_obj = self._dict_to_bar(symbol, bar)

            # 2. 防未来函数校验
            if not self._validate_bar_time(bar_obj):
                return

            # 3. 更新历史行情
            self._update_bar_history(symbol, bar_obj)

            # 4. 调用broker_manager.push_market_data 触发各券商撮合（已发生行情驱动）
            #    （PaperBroker T+1 on_bar / QmtBroker update_market_price 行情驱动）
            if self._broker_manager:
                try:
                    self._broker_manager.push_market_data(symbol, bar)
                except Exception as e:
                    logger.error(f"[engine] broker撮合异常: {e}")
                    self._stats["errors"] += 1

            # 5. 更新portfolio市值
            if self._portfolio:
                self._portfolio.update_market_price(symbol, bar_obj.close)

            # 5.1 H2修复：记录最新价/峰值资产（风控上下文，仅"已发生"事实）
            self._last_prices[symbol] = float(bar_obj.close)
            # KP-P3-04：捕获行情快照前收字段，注入风控上下文（涨跌停 fail-closed 判定）
            _pc = bar.get("prev_close")
            if _pc is not None:
                _pcv = float(_pc or 0)
                if _pcv > 0:
                    self._prev_close_map[symbol] = _pcv
            if self._portfolio:
                _total_assets = self._portfolio.get_total_assets()
                if _total_assets > self._peak_assets:
                    self._peak_assets = _total_assets

            # 6. 调用策略on_bar（基于当前bar收盘价生成信号）
            self._stats["total_bars"] += 1
            self._stats["last_bar_time"] = time.time()

            all_signals: List[Signal] = []
            for strategy in self._strategies:
                try:
                    signals = strategy.on_bar(bar_obj, self._context)
                    if signals:
                        all_signals.extend(signals)
                except Exception as e:
                    logger.error(f"[engine] 策略{strategy.name} on_bar异常: {e}")
                    self._stats["errors"] += 1

            # 7-8. 处理信号（风控+下单，订单进入活跃队列，下一根bar撮合）
            for signal in all_signals:
                self._process_signal(signal, bar_obj)

        except Exception as e:
            logger.error(f"[engine] 行情处理异常 ({symbol}): {e}")
            self._stats["errors"] += 1

    def _dict_to_bar(self, symbol: str, bar_dict: Dict[str, Any]) -> Bar:
        """将bar dict转换为Bar对象（频率自适应，不硬编码DAILY）"""
        return Bar(
            symbol=symbol,
            frequency=self._infer_bar_frequency(),
            timestamp=bar_dict.get("timestamp", time.time()),
            open=float(bar_dict.get("open", 0)),
            high=float(bar_dict.get("high", 0)),
            low=float(bar_dict.get("low", 0)),
            close=float(bar_dict.get("close", 0)),
            volume=float(bar_dict.get("volume", 0)),
            amount=float(bar_dict.get("amount", 0)),
        )

    def _infer_bar_frequency(self) -> BarFrequency:
        """
        从 data_feed._frequency 推断 BarFrequency（C项频率自适应）

        映射: 1m→MINUTE_1 / 5m→MINUTE_5 / 15m→MINUTE_15 / 30m→MINUTE_30 /
              60m/1h→HOUR_1 / daily→DAILY（默认）
        """
        if self._data_feed is None:
            return BarFrequency.DAILY
        freq = str(getattr(self._data_feed, '_frequency', 'daily')).lower()
        mapping = {
            "1m": BarFrequency.MINUTE_1,
            "5m": BarFrequency.MINUTE_5,
            "15m": BarFrequency.MINUTE_15,
            "30m": BarFrequency.MINUTE_30,
            "60m": BarFrequency.HOUR_1,
            "1h": BarFrequency.HOUR_1,
            "daily": BarFrequency.DAILY,
            "1d": BarFrequency.DAILY,
        }
        return mapping.get(freq, BarFrequency.DAILY)

    def _is_realtime_feed(self) -> bool:
        """判断当前行情源是否实时模式（C项实时驱动判定）"""
        if self._data_feed is None:
            return False
        return bool(getattr(self._data_feed, 'is_realtime', False))

    def _validate_bar_time(self, bar: Bar) -> bool:
        """
        防未来函数 + 时间单调（C项双重保护）

        1. 防未来：实时模式 bar.timestamp ≤ now+5s（与data_feed容忍度对齐）；
           日线模式 ≤ now+86400（时区/日期转换误差）
        2. 时间单调：bar.timestamp > 该标的最后一根bar时间戳（防旧快照晚到/重复）

        Returns
        -------
        bool : True=有效，False=丢弃
        """
        now = time.time()
        # 1. 防未来
        tolerance = 5.0 if self._is_realtime_feed() else 86400.0
        if bar.timestamp > now + tolerance:
            logger.warning(
                f"[engine] 未来数据丢弃: {bar.symbol} "
                f"timestamp={bar.timestamp} > now+{tolerance}={now + tolerance}"
            )
            return False
        # 2. 时间单调（防旧快照晚到/重复；预热后实时bar自动去重）
        history = self._bar_history.get(bar.symbol, [])
        if history:
            last_ts = history[-1].timestamp
            if bar.timestamp <= last_ts:
                logger.debug(
                    f"[engine] 非递增bar丢弃: {bar.symbol} "
                    f"ts={bar.timestamp} <= last={last_ts}"
                )
                return False
        return True

    def _update_bar_history(self, symbol: str, bar: Bar) -> None:
        """更新历史行情（为策略提供历史数据）"""
        if symbol not in self._bar_history:
            self._bar_history[symbol] = []
        self._bar_history[symbol].append(bar)
        # 只保留最近N根
        if len(self._bar_history[symbol]) > self._max_history_bars:
            self._bar_history[symbol] = self._bar_history[symbol][-self._max_history_bars:]
        # 同步到策略上下文（保证策略数据与回测同构）
        self._sync_context_data(symbol)

    def _sync_context_data(self, symbol: str) -> None:
        """
        将 bar_history 同步到策略上下文 context.data

        ★★★ 防未来函数 ★★★
        context.data[symbol] 只包含【截至当前 bar 及其之前】的历史数据，
        策略 generate_signal 按 bar.timestamp 截取，绝不访问未来数据。

        修复实盘引擎 v1 缺口：此前 context.data 为空导致策略恒"数据不足"，
        补齐后策略数据源与回测引擎同构（StrategyContext.data: Dict[str, DataFrame]）。
        """
        if self._context is None:
            return
        bars = self._bar_history.get(symbol, [])
        if not bars:
            return
        rows = []
        index = []
        for b in bars:
            index.append(datetime.fromtimestamp(b.timestamp))
            rows.append({
                "开盘价": float(b.open),
                "最高价": float(b.high),
                "最低价": float(b.low),
                "收盘价": float(b.close),
                "成交量": float(b.volume),
                "成交额": float(b.amount),
            })
        try:
            self._context.data[symbol] = pd.DataFrame(rows, index=index)
        except Exception as e:  # pragma: no cover
            logger.warning(f"[engine] 策略数据同步失败 ({symbol}): {e}")

    # ═══════════════════════════════════════════
    #  历史预热（C项：实盘无缝对接关键）
    # ═══════════════════════════════════════════

    def _load_local_history(
        self, symbol: str, count: int,
    ) -> List[Dict[str, Any]]:
        """
        从本地日线 parquet 读取历史bar（离线秒级，替代逐只 akshare 拉取）

        仅用于 daily 频率。数据源为 datasets/processed/<symbol>.parquet（已前复权清洗，
        中文列：开盘价/最高价/最低价/收盘价/成交量/成交额/日期）。本地文件缺失、
        结构不符或读取失败一律返回空列表，由调用方降级到 akshare，不阻断启动。

        ★★★ 防未来函数（规则4）★★★
        本方法只读取存量历史数据（截至数据文件最后日期），不生成信号、
        不引用当前时点之后的数据；返回 bar 按时间升序，仅保留最近 count 根。

        Parameters
        ----------
        symbol : str
            标的代码（如 "000001.SZ"）。
        count : int
            需要的最近根数。

        Returns
        -------
        List[Dict[str, Any]]
            标准 bar dict 列表（升序，至多 count 根）；失败/无数据返回空列表。
        """
        if not self._local_history_dir or count <= 0:
            return []
        parquet_path = _Path(self._local_history_dir) / f"{symbol}.parquet"
        try:
            df = pd.read_parquet(parquet_path)
        except Exception as e:  # 文件缺失/损坏 -> 降级 akshare
            logger.debug(f"[engine] 本地预热无parquet {symbol}: {type(e).__name__}: {e}")
            return []
        if df is None or df.empty or "日期" not in df.columns:
            return []
        required = {"开盘价", "最高价", "最低价", "收盘价", "成交量"}
        if not required.issubset(df.columns):
            logger.warning(f"[engine] 本地parquet缺列 {symbol}: 需{required}")
            return []
        try:
            df = df.sort_values("日期").tail(count)
        except Exception as e:
            logger.warning(f"[engine] 本地parquet排序失败 {symbol}: {e}")
            return []
        bars: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            try:
                date_val = row.get("日期")
                if isinstance(date_val, (int, float)):
                    d = int(date_val)
                    date_str = (
                        f"{d // 10000:04d}-{(d % 10000) // 100:02d}-{d % 100:02d}"
                        if d >= 10000000 else ""
                    )
                else:
                    date_str = str(date_val)[:10] if date_val is not None else ""
                ts = (
                    datetime.strptime(date_str, "%Y-%m-%d").timestamp()
                    if date_str else time.time()
                )
                bars.append({
                    "symbol": symbol,
                    "date": date_str,
                    "open": float(row["开盘价"]),
                    "high": float(row["最高价"]),
                    "low": float(row["最低价"]),
                    "close": float(row["收盘价"]),
                    "volume": float(row.get("成交量", 0)),
                    "amount": float(row.get("成交额", 0)),
                    "timestamp": ts,
                })
            except (ValueError, TypeError, KeyError):
                continue
        return bars

    def _warmup_history(self, symbols: List[str], count: Optional[int] = None,
                        replay: bool = True) -> None:
        """
        启动时历史预热（频率自适应 + 清洗过滤）

        用 akshare 拉取最近 N 根历史bar填充 _bar_history + context.data，
        策略启动即有足够窗口（如MA20），不会恒"数据不足"。

        频率自适应：
          - 1m/5m/15m/30m/60m → 优先 akshare 分钟级接口，失败降级日线
          - daily → akshare 日线
        预热数据经 clean_bar_records 清洗过滤脏数据。
        akshare 失败不阻塞启动（降级为空历史，运行中累积）。

        Parameters
        ----------
        symbols : List[str]
            待预热标的列表
        count : int | None
            预热根数（None则用 self._warmup_bars）
        replay : bool
            是否在填数据后立即逐根回放 on_bar 重建策略状态。
            T2-1 修复：on_init 必须在 context.data 就绪后调用，否则 RSRS 等
            依赖 context.data 的指标缓存会空转（on_init 时 data={} 空循环）。
            因此启动顺序为：warmup(replay=False) → on_init → 外部逐 symbol replay。
            默认 True 保持向后兼容（其他调用方一次调用即填数据+回放）。
        """
        if not symbols:
            return
        count = count if count is not None else self._warmup_bars
        if count <= 0:
            return
        freq = str(getattr(self._data_feed, '_frequency', 'daily')).lower() if self._data_feed else 'daily'

        for symbol in symbols:
            try:
                bars = self._load_local_history(symbol, count) if freq == "daily" else []
                if not bars:
                    bars = self._fetch_history_bars(symbol, count, freq)
                if not bars:
                    logger.info(f"[engine] 预热无数据: {symbol}（运行中累积）")
                    continue
                # 清洗过滤脏数据
                cleaned = self._clean_warmup_bars(bars)
                if not cleaned:
                    continue
                # 转换为 Bar 对象并填充（只保留最近 count 根）
                bar_objs = [self._dict_to_bar(symbol, b) for b in cleaned[-count:]]
                self._bar_history[symbol] = bar_objs[-self._max_history_bars:]
                self._sync_context_data(symbol)
                # 预热驱动策略逐根回放（重建策略状态，如均线交叉 prev_diff）
                # 仅最新一根 bar 产生的信号交给 _process_signal 推进当日，历史信号丢弃避免重复
                # T2-1：replay=False 时只填数据+context.data，回放延后到 on_init 之后
                #       （否则 on_bar 读到的 _rsrs_cache 为空，回放无法重建指标状态）。
                if replay:
                    self._replay_warmup_strategies(symbol, bar_objs)
                logger.info(
                    f"[engine] 预热完成: {symbol} {len(bar_objs)}根 "
                    f"({freq}, 清洗后{len(cleaned)}/{len(bars)})"
                )
            except Exception as e:
                logger.warning(f"[engine] 预热失败 {symbol}: {type(e).__name__}: {e}")

    def _replay_warmup_strategies(
        self, symbol: str, bar_objs: List[Any]
    ) -> None:
        """预热期回放历史 bar 驱动策略，重建策略状态并处理最新 bar 信号。

        均线交叉等策略依赖跨 bar 的前后比较（prev_diff vs current_diff），
        仅填充 context 数据而不逐根驱动 on_bar 会导致状态永远初始、无交叉信号。
        这里按时间升序逐根驱动重建策略状态；最新一根 bar 的信号在此产出并推进
        （本地数据场景 processed 已含最新交易日，当日信号由 warmup 产出，feed 无更新 bar）。
        历史信号丢弃避免全池下单触发风控熔断；异常只告警不阻断启动。
        """
        n = len(bar_objs)
        if n == 0:
            return
        for strategy in self._strategies:
            try:
                # 逐根回放重建策略状态（如均线 prev_diff）；历史信号丢弃不交易。
                # 本地数据场景：processed 已含最新交易日，feed 无更新 bar 可推，
                # 当日信号由最新一根 bar 在此产出并推进（订单进入活跃队列，T+1 下一交易日撮合）。
                for b in bar_objs[:-1]:
                    strategy.on_bar(b, self._context)
                latest = bar_objs[-1]
                # KP-P3-04 补充：warmup 路径不经过 bar 处理段（feed 无新 bar），
                # 手动注入昨收（上一根 bar 收盘价；跨休市语义：10-08 昨收=09-30 收盘）
                if len(bar_objs) >= 2 and hasattr(bar_objs[-2], "close"):
                    try:
                        self._prev_close_map[symbol] = float(bar_objs[-2].close)
                    except Exception:
                        pass
                for sig in (strategy.on_bar(latest, self._context) or []):
                    self._process_signal(sig, latest)
            except Exception as e:
                logger.warning(
                    f"[engine] 预热驱动策略失败 {symbol} "
                    f"({strategy.name}): {type(e).__name__}: {e}"
                )

    def _fetch_history_bars(
        self, symbol: str, count: int, freq: str
    ) -> List[Dict[str, Any]]:
        """
        从 akshare 拉取历史bar（频率自适应，分钟级优先、失败降级日线）

        Parameters
        ----------
        symbol : str
            标的代码（如 "000001.SZ"）
        count : int
            拉取根数
        freq : str
            频率（1m/5m/15m/30m/60m/daily）

        Returns
        -------
        List[Dict] : 标准bar dict 列表（按时间升序）
        """
        try:
            import akshare as ak
        except ImportError:
            logger.warning("[engine] akshare未安装，跳过预热")
            return []

        code = symbol.split(".")[0]
        market = symbol.split(".")[1].lower() if "." in symbol else "sz"
        sina_code = f"{market}{code}"

        # 分钟级频率 → 优先分钟接口
        minute_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
        if freq in minute_map:
            try:
                period = minute_map[freq]
                df = ak.stock_zh_a_hist_min_em(
                    symbol=code, period=period, adjust="hfq",
                )
                if df is not None and not df.empty:
                    return self._akshare_minute_to_bars(df, symbol)[-count:]
            except Exception as e:
                logger.debug(f"[engine] 分钟级预热失败 {symbol}({freq}): {e}，降级日线")

        # 日线（默认 + 分钟级降级）
        try:
            from datetime import timedelta as _td
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - _td(days=max(30, count * 2))).strftime("%Y%m%d")
            df = ak.stock_zh_a_daily(
                symbol=sina_code, start_date=start_date,
                end_date=end_date, adjust="hfq",
            )
            if df is None or df.empty:
                return []
            return self._akshare_daily_to_bars(df, symbol)[-count:]
        except Exception as e:
            logger.warning(f"[engine] 日线预热失败 {symbol}: {e}")
            return []

    @staticmethod
    def _akshare_daily_to_bars(df, symbol: str) -> List[Dict[str, Any]]:
        """akshare 日线 DataFrame → 标准bar dict 列表"""
        bars = []
        for _, row in df.iterrows():
            try:
                date_str = str(row.get("date", ""))[:10]
                ts = datetime.strptime(date_str, "%Y-%m-%d").timestamp() if date_str else time.time()
                bars.append({
                    "symbol": symbol,
                    "date": date_str,
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": float(row.get("volume", 0)),
                    "amount": float(row.get("amount", 0)),
                    "timestamp": ts,
                })
            except (ValueError, TypeError):
                continue
        return bars

    @staticmethod
    def _akshare_minute_to_bars(df, symbol: str) -> List[Dict[str, Any]]:
        """akshare 分钟级 DataFrame → 标准bar dict 列表

        #E1修复：分钟线 date 保留完整时间 "YYYY-MM-DD HH:MM:SS"，
        修复原 time_str[:10] 丢失时分秒导致同天多根bar date相同的BUG。
        """
        from myquant.core.date_utils import format_bar_date
        bars = []
        for _, row in df.iterrows():
            try:
                time_str = str(row.get("时间", row.get("time", "")))
                ts = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").timestamp() if time_str else time.time()
                bars.append({
                    "symbol": symbol,
                    # #E1修复：分钟线保留完整时间，不再只取日期部分
                    "date": format_bar_date(ts, "1min"),
                    "open": float(row.get("开盘", row.get("open", 0))),
                    "high": float(row.get("最高", row.get("high", 0))),
                    "low": float(row.get("最低", row.get("low", 0))),
                    "close": float(row.get("收盘", row.get("close", 0))),
                    "volume": float(row.get("成交量", row.get("volume", 0))),
                    "amount": float(row.get("成交额", row.get("amount", 0))),
                    "timestamp": ts,
                })
            except (ValueError, TypeError):
                continue
        return bars

    def _clean_warmup_bars(self, bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """预热数据清洗（复用 data_access.cleaner，过滤脏数据）"""
        try:
            from myquant.data_access.cleaner import clean_bar_records, load_clean_config
            config = load_clean_config(use_settings=False)
            result = clean_bar_records(records=bars, config=config)
            if isinstance(result, tuple) and len(result) >= 2:
                return result[0]
            if isinstance(result, dict):
                return result.get("cleaned", bars)
        except Exception as e:
            logger.warning(f"[engine] 预热清洗失败（使用原始数据）: {e}")
        return bars

    # ═══════════════════════════════════════════
    #  信号处理
    # ═══════════════════════════════════════════

    def _process_signal(self, signal: Signal, bar: Bar) -> None:
        """
        处理策略信号

        流程:
          1. 记录信号
          2. 风控检查
          3. 计算下单数量
          4. 信号转订单
          5. 提交订单

        Parameters
        ----------
        signal : Signal
            策略信号
        bar : Bar
            当前K线（用于计算价格和数量）
        """
        # 1. 记录信号
        self._signals.append(signal)
        if len(self._signals) > self._max_signals:
            self._signals = self._signals[-self._max_signals:]
        self._stats["total_signals"] += 1
        self._stats["last_signal_time"] = time.time()
        if signal.is_buy:
            self._stats["buy_signals"] += 1
        elif signal.is_sell:
            self._stats["sell_signals"] += 1

        # 触发信号回调
        for cb in self._on_signal_callbacks:
            try:
                cb(signal)
            except Exception as e:
                logger.debug(f"[engine] 信号回调异常: {e}")

        # HOLD信号不处理
        if signal.is_hold:
            return

        # ── 任务43：幂等去重（确定性信号ID，防桥接网络波动重发触发重复下单）──
        # 信号ID按 bar 的交易日/分钟归一化派生（同一根 bar 重发 → 同一 ID），
        # 已处理集合命中 → 直接丢弃（不重复生成订单、不重复走风控/券商）
        if not signal.signal_id:
            signal.signal_id = signal.make_signal_id(
                as_of_date=format_bar_date(bar.timestamp, period=str(bar.frequency.value))
            )
        if signal.signal_id in self._processed_signal_ids:
            self._stats["dedup_skipped"] += 1
            logger.info(
                f"[engine] 重复信号丢弃(幂等): {signal.signal_id} "
                f"{signal.symbol} {signal.signal_type.value}"
            )
            return
        self._processed_signal_ids[signal.signal_id] = time.time()

        # A-3 引擎统一埋点：策略信号事件（signal_id 已归一化，监控模式同样发布——
        # 信号已生成是已发生事实；链路 id 贯穿：signal_id 即后续 order_id 幂等源）
        self._publish_engine_event(SignalEvent(
            signal_id=signal.signal_id,
            event_ts=time.time(),
            strategy_id=signal.strategy_id or self._engine_id,
            symbol=signal.symbol,
            signal_type=(
                signal.signal_type.value
                if hasattr(signal.signal_type, "value")
                else str(signal.signal_type)
            ),
        ))

        # 监控模式（H2修复）：生成并记录信号，但不提交订单
        if self._monitor_only:
            logger.info(
                f"[engine] 监控模式: 信号不下单 {signal.symbol} "
                f"{signal.signal_type.value} reason={signal.reason}"
            )
            return

        # ── BUG-03：订单决策原子化（TOCTOU 竞态防护）──
        # 决策（读 portfolio 风控检查→数量计算→RiskManager 检查）与
        # BUY 待成交占位登记在同一锁域内完成；锁内不做网络 I/O（提交在锁外）。
        # paper 异步撮合下持仓不即时更新，占位使并发信号感知"在途买入"，
        # 防止多标的并发信号基于过时持仓同时放行、超 max_positions。
        with self._order_lock:
            # 2. 风控检查
            if not self._risk_check(signal, bar):
                self._stats["risk_rejected"] += 1
                logger.info(
                    f"[engine] 风控拒绝: {signal.symbol} "
                    f"{signal.signal_type.value} reason={signal.reason}"
                )
                return

            # 3. 计算下单数量
            quantity = self._calc_order_quantity(signal, bar)
            if quantity <= 0:
                logger.info(f"[engine] 下单数量为0，跳过: {signal.symbol}")
                return

            # 4. 信号转订单
            order = signal.to_order(
                quantity=quantity,
                order_type=OrderType.MARKET,
                price=bar.close,  # 市价单，价格参考当前收盘价
            )
            if order is None:
                return

            # ── 任务42：信号生成阶段即记录防未来校验（铁律4：因子仅用历史数据）──
            # 信号T日收盘生成，数据截止日=T日；与回测引擎 record_signal 同源
            if self._lookahead_guard:
                signal_date = get_date_part(bar.timestamp) or ""
                signal_type = "BUY" if signal.is_buy else ("SELL" if signal.is_sell else "CLOSE")
                self._lookahead_guard.record_signal(
                    symbol=signal.symbol,
                    signal_date=signal_date,
                    signal_type=signal_type,
                    data_end=signal_date,  # 信号基于T日收盘价，数据截止日=T日
                    factor_names=[self._strategies[0].name] if self._strategies else None,
                )
                # 登记 order_id → 信号日期（供成交回报 record_trade 铁律3校验）
                if signal_date:
                    self._order_signal_dates[order.order_id] = signal_date

            # 5. H2修复：RiskManager 完整规则库检查（提交券商前强制，铁律2）
            if not self._risk_manager_check(order, bar):
                self._stats["risk_rejected"] += 1
                return

            # ── BUG-03：BUY 待成交占位（锁内登记；成交回报/拒绝/撤销时移除）──
            if signal.is_buy:
                self._pending_buy_order_ids.add(order.order_id)

        # 6. 提交订单（锁外：broker 调用含网络 I/O，锁内不做网络 I/O）
        self._submit_order(order)

    def _risk_check(self, signal: Signal, bar: Bar) -> bool:
        """
        风控检查

        检查项:
          1. 买入：可用资金是否足够
          2. 买入：当前持仓数是否超过最大持仓数
          3. 买入：单票仓位是否超过限制
          4. 卖出：是否持有该标的
          5. 最低现金保留

        Parameters
        ----------
        signal : Signal
            策略信号
        bar : Bar
            当前K线

        Returns
        -------
        bool : True=通过，False=拒绝
        """
        if not self._portfolio:
            return True

        rc = self._risk_config

        if signal.is_buy:
            # 检查1：可用资金
            estimated_cost = bar.close * 100  # 至少1手（100股）
            if self._portfolio.available_cash < estimated_cost:
                logger.debug(f"[engine] 风控: 可用资金不足 {signal.symbol}")
                return False

            # 检查2：最大持仓数（BUG-03：计入 BUY 待成交占位，防并发超限）
            # 本行使用的数据截至当前时刻：持仓为已发生事实 + 锁内登记的待成交占位
            with self._order_lock:
                pending_buy = len(self._pending_buy_order_ids)
            current_positions = self._portfolio.get_position_count() + pending_buy
            if not self._portfolio.has_position(signal.symbol) and current_positions >= rc.max_positions:
                logger.debug(f"[engine] 风控: 持仓数已满 {signal.symbol}")
                return False

            # 检查3：单票仓位限制
            total_assets = self._portfolio.get_total_assets()
            max_position_value = total_assets * rc.max_position_pct
            current_position_value = self._portfolio.get_market_value(signal.symbol)
            if current_position_value + estimated_cost > max_position_value:
                logger.debug(f"[engine] 风控: 单票仓位超限 {signal.symbol}")
                return False

            # 检查4：最低现金保留
            min_cash = self._portfolio.initial_capital * rc.min_cash_ratio
            if self._portfolio.available_cash - estimated_cost < min_cash:
                logger.debug(f"[engine] 风控: 低于最低现金保留 {signal.symbol}")
                return False

        elif signal.is_sell:
            # 检查：是否持有该标的
            if not self._portfolio.has_position(signal.symbol):
                logger.debug(f"[engine] 风控: 无持仓可卖 {signal.symbol}")
                return False

        return True

    # ═══════════════════════════════════════════
    #  H2修复：RiskManager 接线（订单前置检查）
    # ═══════════════════════════════════════════

    def _create_default_risk_manager(self) -> RiskManager:
        """
        创建默认 RiskManager（H2修复）

        默认规则集（兼容模拟盘/回测驱动，不误伤）:
          启用: position_limit(同步RiskConfig) / cash_availability /
                price_limit(无prev_close自动跳过) / blacklist(默认空)
          禁用: single_order_limit / max_drawdown / stop_loss / take_profit /
                trading_hours / rate_limit（基于墙钟或特定阈值，
                由实盘入口 build_risk_manager_from_config 按配置全量启用）
        数据质量事件联动（订阅事件总线自动暂停/恢复）始终生效。
        """
        cfg = {
            "position_limit": {
                "max_position_pct": self._risk_config.max_position_pct,
                "max_positions": self._risk_config.max_positions,
            },
            "cash_availability": {"min_cash_reserve": 0.0},
            "price_limit": {},
            "blacklist": {},
            "single_order_limit": {"enabled": False},
            # ── 任务46/P1：单日亏损上限硬编码 1% 兜底启用（真钱实盘初始建议值，
            #    配置 risk_control.max_daily_loss_pct 可覆盖）──
            "daily_loss_limit": {"enabled": True, "max_daily_loss_pct": 0.01},
            "max_drawdown": {"enabled": False},
            "stop_loss": {"enabled": False},
            "take_profit": {"enabled": False},
            "trading_hours": {"enabled": False},
            "rate_limit": {"enabled": False},
        }
        return RiskManager(config=cfg, data_quality_config=DataQualityRiskConfig())

    def _build_risk_context(self, bar: Optional[Bar] = None) -> RiskContext:
        """
        构建风控上下文（H2修复）

        全部字段取自"已发生"事实（当前资金/持仓/最新价），
        不引用任何未来数据，符合铁律4（严格禁用未来函数）。
        """
        positions: List[Dict[str, Any]] = []
        cash = 0.0
        total_assets = 0.0
        initial_capital = 0.0
        if self._portfolio is not None:
            cash = float(self._portfolio.available_cash)
            total_assets = float(self._portfolio.get_total_assets())
            initial_capital = float(self._portfolio.initial_capital)
            for pos in self._portfolio.get_all_positions():
                positions.append({
                    "symbol": pos.symbol,
                    "quantity": float(pos.quantity),
                    "avg_cost": float(pos.avg_cost),
                    "market_price": float(pos.market_price),
                })
        if total_assets > self._peak_assets:
            self._peak_assets = total_assets
        # ── 任务46/P1：日初资产快照（跨日重置为当日首见资产，严格防未来）──
        today = datetime.now().strftime("%Y-%m-%d")
        if self._active_trading_date != today:
            self._active_trading_date = today
            self._day_start_assets = total_assets if total_assets > 0 else 0.0
            # P1-01修复：跨日必须重置每日下单计数（此前只增不减，累计 max_daily_trades 后
            # 连止损卖单都会被 RateLimitRule 永久拒绝，导致持仓无法退出——真钱红线）
            self._order_count_today = 0
        current_price: Dict[str, float] = dict(self._last_prices)
        if bar is not None:
            current_price[bar.symbol] = float(bar.close)
        # ── BUG-03：position_count 计入 BUY 待成交占位（RiskManager 规则库
        # 与内置风控检查口径一致，防并发信号超 max_positions）──
        with self._order_lock:
            pending_buy = len(self._pending_buy_order_ids)
        return RiskContext(
            cash=cash,
            total_assets=total_assets,
            initial_capital=initial_capital,
            positions=positions,
            position_count=len(positions) + pending_buy,
            peak_assets=self._peak_assets,
            current_price=current_price,
            order_count_today=self._order_count_today,
            last_order_time=self._last_order_time,
            trading_date=today,
            day_start_assets=self._day_start_assets,
            extra={"current_time": datetime.now().strftime("%H:%M:%S"),
                    "prev_close": dict(self._prev_close_map)},
        )

    def _risk_manager_check(self, order: Order, bar: Optional[Bar]) -> bool:
        """
        RiskManager 完整规则库检查（H2修复，订单提交券商前强制）

        - 未接入 RiskManager 时：paper/sim/monitor 回退内置风控返回 True；
          真钱(live) fail-closed：返回 False 并 CRITICAL（P1-07，禁止单键 fail-open）
        - 检查过程异常时按拒绝处理（真钱安全第一），记录 CRITICAL
        """
        if self._risk_manager is None:
            if self._trade_mode == "live":
                logger.critical(
                    "[engine] 真钱(live)模式未接入 RiskManager，订单拒绝（fail-closed，P1-07）"
                )
                self._stats["errors"] += 1
                if self._guardian is not None:
                    self._guardian.record_rejection()
                return False
            return True
        try:
            context = self._build_risk_context(bar)
            result = self._risk_manager.check_order(order, context)
        except Exception as e:
            logger.critical(f"[engine] RiskManager检查异常，订单拒绝（安全第一）: {e}")
            self._stats["errors"] += 1
            if self._guardian is not None:
                self._guardian.record_rejection()
            return False
        if result.passed:
            self._order_count_today += 1
            self._last_order_time = time.time()
            if self._guardian is not None:
                self._guardian.record_acceptance()
            return True
        if self._guardian is not None:
            self._guardian.record_rejection()
        logger.warning(
            f"[engine] RiskManager拒绝: {order.symbol} {order.side.value} "
            f"rule={result.rule_name} reason={result.reason}"
        )
        return False

    # ═══════════════════════════════════════════
    #  H2修复：事件总线 / 状态持久化 / 调度 / 守护
    # ═══════════════════════════════════════════

    # ═══════════════════════════════════════════
    #  A-3 引擎统一埋点：交易链路事件发布
    # ═══════════════════════════════════════════

    def _publish_engine_event(self, event: ChainEvent) -> None:
        """
        A-3 引擎统一埋点出口：发布交易链路事件（BASE-4 四元组链路 id）

        埋点纪律（埋点绝不影响交易主链路）：
          - 事件总线未启用/未装配（_event_bus=None）→ 静默跳过（埋点是增强）
          - publish_chain 失败（总线故障/回调异常）→ 记 warning，不阻断交易
          - event_ts 由发布方填 time.time() 业务发生时刻（防未来红线，
            禁止用未来时间戳；缺失不阻断，require_event_ts=False 过渡期语义）

        Parameters
        ----------
        event : ChainEvent
            交易链路事件（SignalEvent/OrderEvent/PositionUpdateEvent/SystemErrorEvent）
        """
        bus = self._event_bus
        if bus is None:
            return
        try:
            bus.publish_chain(event)
        except Exception as e:
            # 埋点是增强能力：总线故障时交易主链路必须继续，只记 warning
            logger.warning(f"[engine] 链路事件发布失败 {event.type_name}: {e}")

    def _setup_event_bus(self) -> None:
        """
        事件总线联动（H2修复）

        1. RiskManager 订阅数据质量事件
           （CRITICAL全局暂停 / ERROR高占比单标的暂停 / 质量等级分级处理）
        2. data_feed 挂载事件总线（实时流式清洗脏数据 → DataQualityEvent）
        全部 try-except 包裹：事件总线故障不影响交易主链路。
        """
        if not self._enable_event_bus_flag:
            return
        try:
            bus = get_event_bus()
        except Exception as e:
            logger.warning(f"[engine] 获取事件总线失败，降级为无联动: {e}")
            return
        self._event_bus = bus
        if self._risk_manager is not None:
            try:
                self._event_subscription_ids = self._risk_manager.subscribe_to_event_bus(bus)
                logger.info(
                    f"[engine] RiskManager已订阅事件总线: "
                    f"{len(self._event_subscription_ids)}类数据质量事件"
                )
            except Exception as e:
                logger.warning(f"[engine] RiskManager订阅事件总线失败（订单风控仍生效）: {e}")
        if self._data_feed is not None and hasattr(self._data_feed, "attach_event_bus"):
            try:
                self._data_feed.attach_event_bus(bus, source=f"live_{self._engine_id}")
                logger.info("[engine] data_feed已挂载事件总线（实时脏数据联动）")
            except Exception as e:
                logger.warning(f"[engine] data_feed挂载事件总线失败（清洗仍生效）: {e}")

    def _recover_state(self) -> None:
        """
        崩溃恢复（H2修复）

        从最新快照恢复持仓/资金:
          - 只回写"已发生"的持仓/资金事实，不生成任何交易信号（防未来函数）
          - 无快照/恢复失败 → 全新开始，不阻塞启动
        """
        if self._state_store is None:
            return
        try:
            snapshot = self._state_store.load_latest()
        except SnapshotNotFoundError:
            logger.info("[engine] 无历史状态快照，全新开始")
            return
        except Exception as e:
            logger.warning(f"[engine] 状态快照加载失败，全新开始: {e}")
            return
        try:
            result = restore_live_state(
                snapshot, portfolio=self._portfolio,
                broker=self._broker_manager or self._broker,
            )
            self._state_recovered = True
            # KP-P3-03：恢复后对账（快照订单 vs 券商在途订单差集，未领回挂单取消）
            self._reconcile_restored_orders(snapshot.state.get("orders") or [])
            # T2-2：恢复已处理信号 id（最近 5 个交易日 ≈ 7 自然日窗口），
            # 防止重启后同一信号被重复下单。过期 id 丢弃防无限膨胀。
            self._restore_processed_signal_ids(snapshot.state)
            logger.info(
                f"[engine] 崩溃恢复完成: 快照v{snapshot.version} "
                f"tag={snapshot.tag!r} result={result}"
            )
        except Exception as e:
            logger.warning(f"[engine] 状态恢复失败，全新开始: {e}")

    def _reconcile_restored_orders(
        self,
        snapshot_orders: List[Dict[str, Any]],
    ) -> None:
        """启动恢复后对账（KP-P3-03）。

        快照订单 vs 券商在途订单差集：未领回挂单 → cancel；
        超阈值 → pause + CRITICAL（双账本漂移风险）。
        """
        broker = self._broker_manager or self._broker
        if broker is None or not hasattr(broker, "get_orders"):
            return
        try:
            broker_ids = {o.order_id for o in broker.get_orders()}
        except Exception as e:
            logger.warning(f"[engine] 对账读取券商订单失败: {e}")
            return
        snapshot_ids = {
            od.get("order_id") for od in snapshot_orders
            if isinstance(od, dict) and od.get("order_id")
        }
        unclaimed = snapshot_ids - broker_ids
        if not unclaimed:
            return
        logger.warning(
            f"[engine] 恢复后未领回订单 {len(unclaimed)} 笔，尝试取消: {sorted(unclaimed)}"
        )
        if hasattr(broker, "cancel_order"):
            for oid in sorted(unclaimed):
                try:
                    broker.cancel_order(oid)
                except Exception as e:
                    logger.warning(f"[engine] 取消未领回订单 {oid} 失败: {e}")
        threshold = 1 if self._trade_mode == "live" else 5
        if len(unclaimed) > threshold:
            logger.critical(
                f"[engine] 未领回订单 {len(unclaimed)} 超过阈值 {threshold}，"
                "暂停引擎（双账本漂移风险）"
            )
            self.pause(reason="未领回订单超阈（双账本漂移风险）")

    # T2-2 恢复窗口：最近 5 个交易日 ≈ 7 个自然日（含周末/节假日余量）
    _PROCESSED_SIGNAL_WINDOW_SEC: float = 7.0 * 86400.0

    def _restore_processed_signal_ids(self, snapshot_state: Dict[str, Any]) -> None:
        """从快照恢复已处理信号 id（T2-2）。

        读取快照 extra.processed_signal_ids（[[signal_id, unix_ts], ...]），
        只保留最近 ``_PROCESSED_SIGNAL_WINDOW_SEC`` 内的 id，过期丢弃。
        恢复后同一信号重启不重复下单；读取失败/无数据时保持空集，不阻塞启动。
        """
        try:
            extra = (snapshot_state or {}).get("extra") or {}
            raw = extra.get("processed_signal_ids")
            if raw is None:
                # 旧快照无此字段：不覆盖（start 已先 clear，此处保持空集即可）
                return
            cutoff = time.time() - self._PROCESSED_SIGNAL_WINDOW_SEC
            restored: Dict[str, float] = {}
            for item in raw:
                try:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    sid, ts = str(item[0]), float(item[1])
                    if sid and ts >= cutoff:
                        restored[sid] = ts
                except (TypeError, ValueError):
                    # 单条脏数据跳过，不影响其余 id 恢复
                    continue
            self._processed_signal_ids = restored
            if restored:
                logger.info(
                    f"[engine] T2-2 已恢复已处理信号 id: {len(restored)} 条"
                    f"（窗口 {self._PROCESSED_SIGNAL_WINDOW_SEC/86400:.0f} 天）"
                )
        except Exception as e:  # noqa: BLE001 - 恢复失败不阻断启动
            logger.warning(f"[engine] T2-2 已处理信号 id 恢复失败（不阻断）: {e}")

    def _save_state(self, tag: str = "") -> None:
        """保存实盘状态快照（H2修复；停止时/定时快照调用）"""
        if self._state_store is None:
            return
        try:
            state = capture_live_state(
                engine=self,
                portfolio=self._portfolio,
                broker=self._broker_manager or self._broker,
                scheduler=self._scheduler,
                extra={
                    "monitor_only": self._monitor_only,
                    # KP-P5-07: config_version 落快照 metadata（配置变更版本可审计）
                    "config_version": getattr(self, "_config_version", ""),
                    # T2-2：已处理信号 id 持久化（[signal_id, unix_ts]），
                    # 重启后恢复最近窗口内的 id，防重启重复下单。
                    "processed_signal_ids": [
                        [sid, float(ts)] for sid, ts in self._processed_signal_ids.items()
                    ],
                },
            )
            snapshot = self._state_store.save(state, tag=tag or "auto")
            logger.info(f"[engine] 状态快照已保存: v{snapshot.version} tag={tag or 'auto'}")
        except Exception as e:
            logger.warning(f"[engine] 状态快照保存失败（不影响交易）: {e}")

    # ═══════════════════════════════════════════
    #  BUG-04：guardian 一键平仓接线（P2 实盘安全）
    # ═══════════════════════════════════════════

    def _setup_force_close(self) -> None:
        """
        装配 guardian 一键平仓回调（BUG-04）

        guardian.evaluate 产出 FORCE_CLOSE_* 决策后，由 _guardian_patrol
        触发 force_close_all/force_close_symbol → 回调本引擎接线的
        EmergencyLiquidationService（默认 dry_run/confirm=False）：
        paper/sim 通道只生成平仓计划并记录，绝不真实下单；
        真实平仓由人工 SOP（scripts/一键平仓.py --confirm）接管。

        装配时机：start 内 broker 就绪后、调度器挂载前。
        全部 try-except 包裹：接线失败不影响交易主链路（人工 SOP 兜底）。
        """
        if self._guardian is None:
            return
        broker = self._broker_manager or self._broker
        if broker is None:
            logger.warning("[engine] 无券商通道，guardian 一键平仓回调未装配（人工 SOP 兜底）")
            return
        try:
            self._emergency_service = EmergencyLiquidationService(broker)
            self._guardian.set_force_close_callback(self._on_force_close)
            logger.info("[engine] guardian 一键平仓已接线（EmergencyLiquidationService，dry_run 模式）")
        except Exception as e:
            self._emergency_service = None
            logger.warning(f"[engine] guardian 一键平仓接线失败（人工 SOP 兜底）: {e}")

    def _on_force_close(self, action: str, symbol: Optional[str] = None) -> bool:
        """
        guardian 强平回调执行（BUG-04）

        v1 安全语义：confirm=False（dry_run）→ 只生成平仓计划/记录，
        paper/sim 通道不下真实平仓单；真实执行由人工 SOP 确认后触发。

        Parameters
        ----------
        action : str
            强平动作（"all"=全部平仓 / "symbol"=单标的平仓）
        symbol : str | None
            action="symbol" 时指定标的

        Returns
        -------
        bool : True=服务执行成功（dry_run 生成计划）
        """
        if self._emergency_service is None:
            logger.error("[engine] 强平回调触发但紧急平仓服务未装配（人工 SOP 介入）")
            return False
        try:
            report = self._emergency_service.execute(
                confirm=False,  # BUG-04 v1 安全：只记录/生成计划，不下真单
                symbol=symbol if action == "symbol" else None,
            )
            ok = bool(report.get("ok"))
            executed = bool(report.get("executed"))
            if executed:
                logger.critical(
                    f"[engine] 强制平仓已真实执行: action={action} symbol={symbol or '-'} "
                    f"mode={report.get('mode', '?')} ok={ok}"
                )
            else:
                # KP-M02: dry-run 恒未真实执行，显式 CRITICAL 提示需人工 --confirm 平仓
                logger.critical(
                    f"[engine] 强制平仓【仅生成计划，未真实执行】action={action} "
                    f"symbol={symbol or '-'} executed=False，需人工确认后平仓"
                )
            return ok
        except Exception as e:
            logger.error(f"[engine] 强制平仓回调异常: {e}")
            return False

    def _setup_scheduler(self) -> None:
        """
        调度器挂载与启动（H2修复）

        任务:
          1. attach_engine — 健康巡检(60s) + 收盘日报(15:30)
          2. 风控守护巡检 — guardian.evaluate 定期决策
          3. attach_snapshot — 定时状态快照（默认300s）
        调度器在此延迟创建（组件就绪后才挂载）。
        全部 try-except 包裹：调度失败不阻塞引擎启动。
        """
        if not self._scheduler_enabled:
            return
        try:
            if self._scheduler is None:
                self._scheduler = LiveScheduler()
                self._own_scheduler = True
            attach_engine(
                self._scheduler,
                engine=self,
                portfolio=self._portfolio,
                broker=self._broker_manager or self._broker,
                data_feed=self._data_feed,
                risk_manager=self._risk_manager,
                patrol_interval=self._patrol_interval,
                # 0.2：真钱(live)模式 CRITICAL 健康自动停引擎；paper/sim 不自动停
                pause_on_critical=(self._trade_mode == "live"),
            )
            if self._guardian is not None:
                self._scheduler.add_interval_task(
                    self._guardian_patrol,
                    name="风控守护巡检",
                    interval_seconds=self._patrol_interval,
                    run_immediately=False,
                )
            if self._state_store is not None:
                attach_snapshot(
                    self._scheduler,
                    self._state_store,
                    engine=self,
                    portfolio=self._portfolio,
                    broker=self._broker_manager or self._broker,
                    snapshot_interval=self._snapshot_interval,
                )
            # KP-P3-08: 定时券商账本对账（sync_from_broker，超阈 pause+CRITICAL）
            if self._portfolio is not None:
                self._scheduler.add_interval_task(
                    self._broker_accountability_patrol,
                    name="券商账本对账",
                    interval_seconds=self._accountability_interval,
                    run_immediately=False,
                )
            self._scheduler.start()
            logger.info("[engine] 调度器已启动（巡检/日报/守护/快照）")
        except Exception as e:
            logger.warning(f"[engine] 调度器挂载失败（不影响交易主链路）: {e}")

    def _assert_startup_positions(self) -> None:
        """T1-2：启动持仓断言（fail-closed，保命项）。

        启动时（broker 已 connect、行情订阅之前）强制对账一次：
          - 若 portfolio 空仓但 broker 返回有持仓 → 快照丢失/漂移，
            引擎会误以为空仓而重复开仓 → 抛 RuntimeError 拒绝启动。
          - portfolio 空 AND broker 也空 → 全新启动，正常放行。
          - portfolio 有持仓 AND broker 有持仓 → 快照已恢复，正常 sync 对账。

        防未来：只读 broker/portfolio 【已发生】的持仓事实，不生成任何信号。
        """
        broker = self._broker_manager or self._broker
        if broker is None or self._portfolio is None:
            return  # 无 broker 场景（纯回测/干跑）跳过

        portfolio_count = len(self._portfolio.get_all_positions())
        try:
            broker_positions = broker.get_positions()
        except Exception as e:  # noqa: BLE001 - broker 读持仓失败不硬阻断，但告警
            logger.warning(f"[engine] T1-2 启动断言：broker 持仓读取失败（放行但告警）: {e}")
            return
        broker_count = len(broker_positions)

        if portfolio_count == 0 and broker_count > 0:
            symbols = [p.symbol for p in broker_positions[:10]]
            msg = (
                f"[engine] T1-2 启动断言：portfolio 空仓（{portfolio_count}）"
                f"但 broker 有持仓（{broker_count}，如 {symbols}）——快照丢失/账本漂移"
            )
            # 仅 live 真实交易模式 fail-closed（保命）；paper/sim 模拟盘只告警放行
            if getattr(self, "_trade_mode", "paper") == "live":
                raise RuntimeError(
                    msg + "，live 模式拒绝启动以防重复开仓。"
                    "请人工核对：① 快照目录；② broker 实际持仓；③ 手动恢复后再启动。"
                )
            else:
                logger.warning(msg + "，paper/sim 模式告警放行（模拟盘无真实资金风险）")

        # 正常对账同步（有持仓时 sync 一次，确保 portfolio 与 broker 一致）
        if broker_count > 0 or portfolio_count > 0:
            try:
                diff = self._portfolio.sync_from_broker(broker)
                logger.info(
                    f"[engine] T1-2 启动对账完成: portfolio={portfolio_count} -> "
                    f"{len(self._portfolio.get_all_positions())} 只, diff={diff}"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[engine] T1-2 启动对账失败（不阻断）: {e}")

    def _broker_accountability_patrol(self) -> None:
        """
        券商账本对账（KP-P3-08：定时 sync_from_broker，超阈值 pause+CRITICAL）。

        风控基准须取券商权威账本：本地 portfolio 定时向券商同步，
        账实差异超阈值 → 暂停引擎（避免在漂移账本上继续交易）。
        """
        broker = self._broker_manager or self._broker
        if broker is None or self._portfolio is None:
            return
        try:
            diff = self._portfolio.sync_from_broker(broker)
            if diff.exceeds_threshold:
                logger.critical(
                    f"[engine] 券商账本差异超阈值，暂停引擎（风控基准非权威账本）: {diff}"
                )
                self.pause(reason="账实差异超阈（风控基准非权威账本）")
        except Exception as e:
            logger.warning(f"[engine] 券商账本对账失败: {e}")

    def _guardian_patrol(self) -> None:
        """
        风控守护巡检（H2修复，调度器定时调用）

        决策处理:
          - PAUSE → RiskManager.pause（禁止新开仓，自动联动告警）
          - FORCE_CLOSE_* → 先 PAUSE 新开仓，再触发 guardian 强平回调
            （v1 dry_run 只记录/生成计划，paper/sim 不下真单）
          - ALERT → 仅记录
        只使用"已发生"事实（当前上下文），防未来函数。
        """
        if self._guardian is None:
            return
        try:
            context = self._build_risk_context(None)
            decision = self._guardian.evaluate(context)
        except Exception as e:
            logger.warning(f"[engine] 守护巡检异常: {e}")
            return
        if decision.decision_type == DecisionType.NO_ACTION:
            return
        if decision.decision_type == DecisionType.PAUSE:
            if self._risk_manager is not None:
                self._risk_manager.pause(decision.reason or "风控守护暂停")
            logger.warning(f"[engine] 守护决策-暂停开仓: {decision.reason}")
        elif decision.decision_type in (
            DecisionType.FORCE_CLOSE_ALL, DecisionType.FORCE_CLOSE_SYMBOL
        ):
            # ── BUG-04：先 PAUSE 新开仓 → 再触发强平（v1 dry_run：只记录/生成计划，
            # paper/sim 不下真单；真实平仓由人工 SOP（scripts/一键平仓.py --confirm）执行）──
            if self._risk_manager is not None:
                self._risk_manager.pause(decision.reason or "强制平仓前置暂停")
                logger.critical(f"[engine] 强制平仓前置：风控已暂停新开仓: {decision.reason}")
            if decision.decision_type == DecisionType.FORCE_CLOSE_ALL:
                self._guardian.force_close_all(decision.reason or "风控守护强制平仓")
            else:
                sym = (decision.details or {}).get("symbol") or ""
                self._guardian.force_close_symbol(sym, decision.reason or "风控守护强制平仓")
        else:
            logger.info(
                f"[engine] 守护决策-{decision.decision_type.value}: {decision.reason}"
            )

    def _get_risk_summary(self) -> Optional[Dict[str, Any]]:
        """风控状态摘要（H2修复，供get_status/health_check）"""
        if self._risk_manager is None:
            return None
        try:
            status = self._risk_manager.get_status()
            return {
                "status": status.value if hasattr(status, "value") else str(status),
                "reason": self._risk_manager.get_status_reason(),
                "paused_symbols": self._risk_manager.get_paused_symbols(),
            }
        except Exception as e:
            logger.debug(f"[engine] 风控状态获取失败: {e}")
            return None

    def _calc_order_quantity(self, signal: Signal, bar: Bar) -> int:
        """
        计算下单数量（A股100股整数倍）

        买入：按单票最大仓位比例计算
        卖出：卖出全部持仓

        Parameters
        ----------
        signal : Signal
            策略信号
        bar : Bar
            当前K线

        Returns
        -------
        int : 下单数量（股）
        """
        if not self._portfolio:
            return 100  # 默认1手

        if signal.is_buy:
            # 买入：按单票最大仓位比例计算
            total_assets = self._portfolio.get_total_assets()
            max_amount = total_assets * self._risk_config.max_position_pct
            current_value = self._portfolio.get_market_value(signal.symbol)
            available_amount = max(0, max_amount - current_value)
            # 不超过可用资金
            available_amount = min(available_amount, self._portfolio.available_cash)
            # 计算股数（100股整数倍）
            shares = int(available_amount / bar.close / 100) * 100
            return max(0, shares)

        elif signal.is_sell:
            # 卖出：卖出全部持仓
            pos = self._portfolio.get_position(signal.symbol)
            if pos and pos.quantity > 0:
                return int(pos.quantity)
            return 0

        return 0

    # ═══════════════════════════════════════════
    #  订单管理
    # ═══════════════════════════════════════════

    def _submit_order(self, order: Order) -> None:
        """提交订单到 broker_manager（自动路由主券商，拒单/断线自动 failover）"""
        if not self._broker_manager:
            logger.error("[engine] 无broker_manager，无法提交订单")
            return

        try:
            self._orders.append(order)
            if len(self._orders) > self._max_orders:
                self._orders = self._orders[-self._max_orders:]
            self._stats["total_orders"] += 1

            # 提交到 broker_manager（路由主券商 + 主备 failover 统一处理）
            result = self._broker_manager.submit_order(order)

            # A-3 引擎统一埋点：订单提交/拒单事件（SUBMITTED/PENDING→submit，
            # REJECTED→rejected；链路 id：order_id 与 signal_id 幂等同源）
            result_status = getattr(result, "status", None)
            if result_status == OrderStatus.REJECTED:
                self._publish_engine_event(OrderEvent(
                    event_type=EventType.ORDER_REJECTED,
                    order_id=order.order_id,
                    event_ts=time.time(),
                    symbol=order.symbol,
                    status="rejected",
                    reject_reason=str(
                        (getattr(result, "extra", None) or {}).get("reject_reason", "")
                        or (order.extra or {}).get("reject_reason", "")
                        or ""
                    ),
                ))
            else:
                self._publish_engine_event(OrderEvent(
                    event_type=EventType.ORDER_SUBMIT,
                    order_id=order.order_id,
                    event_ts=time.time(),
                    symbol=order.symbol,
                    status=(
                        result_status.value.lower()
                        if hasattr(result_status, "value")
                        else str(result_status or "submitted")
                    ),
                ))

            # ── BUG-03：提交被拒（REJECTED）时回滚 BUY 待成交占位
            # （锁外回滚与锁内登记互斥，避免"在途占位"永久悬挂）──
            if order.side == OrderSide.BUY and getattr(result, "status", None) == OrderStatus.REJECTED:
                with self._order_lock:
                    self._pending_buy_order_ids.discard(order.order_id)

            # 触发订单回调
            for cb in self._on_order_callbacks:
                try:
                    cb(result)
                except Exception as e:
                    # #R1修复：用户回调异常不应中断实盘引擎，但记录warning日志便于排查
                    logger.warning(f"[engine] 订单回调异常: {cb.__name__}: {e}", exc_info=True)

            logger.info(
                f"[engine] 订单已提交: {order.symbol} {order.side.value} "
                f"{order.quantity:.0f}@{order.price:.2f} "
                f"status={result.status.value if hasattr(result, 'status') else 'N/A'}"
            )

        except Exception as e:
            logger.error(f"[engine] 订单提交失败: {e}")
            self._stats["errors"] += 1
            self._stats["rejected_orders"] += 1
            # A-3 引擎统一埋点：系统错误事件（订单提交异常，携带订单链路 id）
            self._publish_engine_event(SystemErrorEvent(
                error=str(e),
                component="engine.submit",
                order_id=order.order_id,
                event_ts=time.time(),
            ))

    # ═══════════════════════════════════════════
    #  断线补单对账（任务45 / T3：重连后仅补发未成交订单）
    # ═══════════════════════════════════════════

    def resend_pending_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        断线补单对账（委托 BrokerManager 执行）

        QMT 断线/网络波动后，本地与券商端订单状态可能不一致：
        本地 SUBMITTED 但券商端未收到 → 本轮补发；
        券商端已成交/已撤/在途 → 不补发（防重复报单）。
        券商端查询失败 → 保守不补发（宁可漏单不重复单）。

        触发方式（v1 提供公开入口，供调度器巡检/重连回调/人工调用）:
          - 由 LiveScheduler 巡检周期调用（后续迭代自动接线）
          - 或重连成功后手动调用本方法

        Parameters
        ----------
        symbol : str | None
            可选，仅对账指定标的

        Returns
        -------
        Dict[str, Any] : BrokerManager.resend_pending_orders() 的对账结果
            {checked / in_flight / terminal / resent / failed / skipped / per_broker}
        """
        if not self._broker_manager:
            logger.error("[engine] 无 broker_manager，无法断线补单对账")
            return {"error": "no_broker_manager"}
        return self._broker_manager.resend_pending_orders(symbol)

    def on_order_update(self, order: Order) -> None:
        """
        订单更新回调（供broker调用）

        Parameters
        ----------
        order : Order
            更新后的订单
        """
        # A-3 引擎统一埋点：订单状态事件（FILLED/PARTIAL_FILLED/CANCELLED/REJECTED
        # ——订单级终态/部分成交回报；逐笔成交另由 on_trade 发布成交级事件）
        _status_event_map = {
            OrderStatus.FILLED: EventType.ORDER_FILLED,
            OrderStatus.PARTIAL_FILLED: EventType.ORDER_PARTIAL,
            OrderStatus.CANCELLED: EventType.ORDER_CANCELLED,
            OrderStatus.REJECTED: EventType.ORDER_REJECTED,
        }
        if order.status in _status_event_map:
            self._publish_engine_event(OrderEvent(
                event_type=_status_event_map[order.status],
                order_id=order.order_id,
                event_ts=time.time(),
                symbol=order.symbol,
                status=order.status.value.lower(),
                reject_reason=(
                    str((order.extra or {}).get("reject_reason", "") or "")
                    if order.status == OrderStatus.REJECTED
                    else ""
                ),
            ))

        if order.status == OrderStatus.FILLED:
            self._stats["filled_orders"] += 1
            price = order.price if order.price is not None else 0.0
            logger.info(f"[engine] 订单已成交: {order.symbol} {order.quantity:.0f}@{price:.2f}")
        # ── BUG-03：BUY 订单终态（拒绝/撤销）移除待成交占位（不再产生成交）──
        elif order.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED) and order.side == OrderSide.BUY:
            with self._order_lock:
                self._pending_buy_order_ids.discard(order.order_id)

        # KP-P4-02：逐笔订单落库（长期归档；注入 trade_ledger 时生效，失败不中断）
        if self._trade_ledger is not None:
            try:
                self._trade_ledger.record_order(order, engine_id=self._engine_id)
            except Exception as e:
                logger.error(f"[engine] 订单落库失败: {e}")

    def on_trade(self, trade: Trade) -> None:
        """
        成交回报回调（供broker调用）

        Parameters
        ----------
        trade : Trade
            成交记录
        """
        try:
            self._stats["total_trades"] += 1

            # ── BUG-03：BUY 成交后移除待成交占位（持仓已由 apply_trade 计入 portfolio）──
            if trade.side == OrderSide.BUY:
                with self._order_lock:
                    self._pending_buy_order_ids.discard(trade.order_id)

            # 更新portfolio
            if self._portfolio:
                self._portfolio.apply_trade(trade)

            # A-3 引擎统一埋点：成交事件（逐笔 FILLED，成交级链路 trade_id）+ 持仓更新
            # （apply_trade 后取更新后均价 avg_cost；链路 id：order_id 与 signal_id 幂等同源）
            self._publish_engine_event(OrderEvent(
                event_type=EventType.ORDER_FILLED,
                order_id=trade.order_id,
                trade_id=trade.trade_id,
                event_ts=time.time(),
                symbol=trade.symbol,
                status="filled",
            ))
            avg_price = 0.0
            try:
                if self._portfolio is not None:
                    pos = self._portfolio.get_position(trade.symbol)
                    avg_price = float(getattr(pos, "avg_cost", 0.0) or 0.0)
            except Exception as e:
                # 均价取不到时用 0 占位（不阻断成交链路）
                logger.debug(f"[engine] 持仓均价读取失败: {e}")
            self._publish_engine_event(PositionUpdateEvent(
                order_id=trade.order_id,
                trade_id=trade.trade_id,
                event_ts=time.time(),
                symbol=trade.symbol,
                side=trade.side.value.lower() if hasattr(trade.side, "value") else str(trade.side),
                quantity=trade.quantity,
                avg_price=avg_price,
            ))

            # KP-P4-02：逐笔成交落库（与 broker 逐笔一致，长期归档；失败不中断）
            if self._trade_ledger is not None:
                try:
                    self._trade_ledger.record_fill(trade, engine_id=self._engine_id)
                except Exception as e:
                    logger.error(f"[engine] 成交落库失败: {e}")

            # ── T4-1：撤销 T+1 自动豁免（红线 RL-08 不可豁免）──
            # 旧逻辑：trade_date==signal_date（信号日成交）自动豁免 T+1 检查。
            # 新逻辑：仅"无信号映射"（手动补单/风控强平）豁免；信号日成交属违规。
            # PaperBroker 已是 T+1 撮合（on_market_data 先 push_market_data 撮合旧单
            # 再生成新信号），正常路径不会出现 trade_date==signal_date。
            if self._lookahead_guard:
                trade_date = get_date_part(trade.timestamp) or ""
                signal_date = self._order_signal_dates.get(trade.order_id, "")
                skip_t1 = not signal_date and bool(trade_date)  # T4-1：只保留无信号映射豁免
                side = trade.side.value if hasattr(trade.side, 'value') else str(trade.side)
                self._lookahead_guard.record_trade(
                    symbol=trade.symbol,
                    trade_date=trade_date,
                    signal_date=signal_date,
                    price_type="OPEN",
                    side=side,
                    skip_t1_check=skip_t1,
                )

            # 触发成交回调
            for cb in self._on_trade_callbacks:
                try:
                    cb(trade)
                except Exception as e:
                    # #R1修复：用户回调异常不应中断实盘引擎，但记录warning日志便于排查
                    logger.warning(f"[engine] 成交回调异常: {cb.__name__}: {e}", exc_info=True)

            logger.info(
                f"[engine] 成交回报: {trade.symbol} {trade.side.value} "
                f"{trade.quantity:.0f}@{trade.price:.2f} "
                f"手续费={trade.commission:.2f}"
            )

        except Exception as e:
            logger.error(f"[engine] 成交回报处理失败: {e}")
            self._stats["errors"] += 1
            # A-3 引擎统一埋点：系统错误事件（成交处理异常，携带成交链路 id）
            self._publish_engine_event(SystemErrorEvent(
                error=str(e),
                component="engine.trade",
                order_id=trade.order_id,
                trade_id=trade.trade_id,
                event_ts=time.time(),
            ))

    # ═══════════════════════════════════════════
    #  回调注册
    # ═══════════════════════════════════════════

    def on_signal(self, callback: Callable[[Signal], None]) -> None:
        """注册信号回调"""
        self._on_signal_callbacks.append(callback)

    def on_order(self, callback: Callable[[Order], None]) -> None:
        """注册订单回调"""
        self._on_order_callbacks.append(callback)

    def register_trade_callback(self, callback: Callable[[Trade], None]) -> None:
        """注册成交回调（注意：不要命名为on_trade，会与成交回报处理方法冲突）"""
        self._on_trade_callbacks.append(callback)

    # ═══════════════════════════════════════════
    #  辅助方法
    # ═══════════════════════════════════════════

    def _check_components(self) -> bool:
        """
        检查组件是否齐全

        data_feed为可选组件（手动驱动行情时不需要），
        broker/portfolio/strategy为必需组件。
        """
        missing = []
        # data_feed可选（手动驱动行情时不需要）
        if not self._broker_manager:
            missing.append("broker_manager")
        if not self._portfolio:
            missing.append("portfolio")
        if not self._strategies:
            missing.append("strategy")

        if missing:
            logger.error(f"[engine] 缺少组件: {missing}")
            return False
        return True

    def _get_watch_symbols(self) -> List[str]:
        """
        获取关注标的列表

        v1：从 broker_manager 合并持仓 + 策略参数中提取（多券商持仓合并去重）
        后续迭代：策略可动态添加关注标的
        """
        symbols = set()

        # 从 live 配置订阅 + 股票池合并（paper 仿真/实盘关注标的；
        # 回测引擎无 _live_watch_symbols 属性，自动跳过，保持零影响）
        live_watch = getattr(self, "_live_watch_symbols", None)
        if live_watch:
            symbols.update(live_watch)

        # 从 broker_manager 合并持仓获取（多券商并行：各券商持仓合并）
        if self._broker_manager and hasattr(self._broker_manager, 'get_positions'):
            try:
                positions = self._broker_manager.get_positions(merged=True)
                for pos in positions:
                    if hasattr(pos, 'symbol') and getattr(pos, 'quantity', 0) != 0:
                        symbols.add(pos.symbol)
            except Exception as e:
                logger.debug(f"[engine] 从broker_manager获取持仓失败: {e}")

        # 从portfolio持仓获取
        if self._portfolio:
            for pos in self._portfolio.get_all_positions():
                symbols.add(pos.symbol)

        return sorted(symbols)

    def get_bar_history(self, symbol: str, count: int = 20) -> List[Bar]:
        """获取历史K线（供策略使用）"""
        bars = self._bar_history.get(symbol, [])
        return bars[-count:] if len(bars) >= count else bars

    # ═══════════════════════════════════════════
    #  状态与统计
    # ═══════════════════════════════════════════

    @property
    def stats(self) -> Dict[str, Any]:
        """引擎统计（只读）"""
        return dict(self._stats)

    def get_status(self) -> Dict[str, Any]:
        """
        获取引擎状态

        Returns
        -------
        Dict[str, Any] :
            {
                "engine_id": str,
                "state": str,
                "is_running": bool,
                "start_time": float,
                "run_seconds": float,
                "strategies": List[str],
                "watch_symbols": List[str],
                "stats": Dict,
            }
        """
        return {
            "engine_id": self._engine_id,
            "state": self._state,
            "is_running": self.is_running,
            "start_time": self._start_time,
            "run_seconds": time.time() - self._start_time if self._start_time else 0,
            "strategies": [s.name for s in self._strategies],
            "watch_symbols": self._get_watch_symbols(),
            "broker": self._get_broker_summary(),
            "risk": self._get_risk_summary(),
            "safeguards": {
                "risk_manager": self._risk_manager is not None,
                "guardian": self._guardian is not None,
                "state_store": self._state_store is not None,
                "scheduler": self._scheduler is not None,
                "event_bus": self._event_bus is not None,
                "monitor_only": self._monitor_only,
                "state_recovered": self._state_recovered,
            },
            "stats": dict(self._stats),
        }

    def _get_broker_summary(self) -> Optional[Dict[str, Any]]:
        """
        券商摘要（单/多券商统一结构）

        Returns
        -------
        Dict | None :
            {
                "mode": "single" | "multi",
                "primary": str,          # 主券商别名
                "broker_count": int,
                "brokers": List[str],    # 全部别名
            }
        """
        if not self._broker_manager:
            return None
        regs = self._broker_manager.registrations
        return {
            "mode": "multi" if len(regs) > 1 else "single",
            "primary": self._broker_manager.primary_alias,
            "broker_count": len(regs),
            "brokers": list(regs.keys()),
        }

    def health_check(self) -> Dict[str, Any]:
        """
        健康检查

        Returns
        -------
        Dict[str, Any] :
            {
                "engine": Dict,
                "data_feed": Dict | None,
                "broker": Dict | None,
                "portfolio": Dict | None,
                "status": "healthy" | "degraded" | "down",
            }
        """
        result = {
            "engine": self.get_status(),
            "data_feed": None,
            "broker": None,
            "portfolio": None,
            "status": "healthy",
        }

        # 检查data_feed
        if self._data_feed and hasattr(self._data_feed, 'health_check'):
            try:
                result["data_feed"] = self._data_feed.health_check()
            except Exception as e:
                logger.debug(f"[engine] data_feed健康检查失败: {e}")

        # 检查broker_manager（统一多券商 schema，单/多券商同构）
        if self._broker_manager:
            try:
                hc_all = self._broker_manager.health_check_all()
                result["broker"] = {
                    "primary": self._broker_manager.primary_alias,
                    "brokers": hc_all,
                    "connected": self._broker_manager.is_connected(),
                }
            except Exception as e:
                logger.debug(f"[engine] broker_manager健康检查失败: {e}")

        # 检查portfolio
        if self._portfolio:
            result["portfolio"] = self._portfolio.get_account()

        # 综合状态
        if self._state == EngineState.ERROR:
            result["status"] = "down"
        elif self._stats["errors"] > 0 or self._stats["risk_rejected"] > 5:
            result["status"] = "degraded"

        return result

    def __repr__(self) -> str:
        return (
            f"LiveEngine(id={self._engine_id}, "
            f"state={self._state}, "
            f"strategies={len(self._strategies)}, "
            f"signals={self._stats['total_signals']}, "
            f"trades={self._stats['total_trades']})"
        )


# ═══════════════════════════════════════════════
#  H2修复：实盘一键装配工厂（配置 → 完整实盘链路）
# ═══════════════════════════════════════════════
#
# 回写#289（broker_factory）/回写#422（engine_assembly）：装配层拆出。
# 装配层入口（create_live_engine / startup_assertions / _assert_live_watchdog_gate）
# 已由调用方（run_实盘入口.py / live_safety_gate.py / 测试）直连 engine_assembly；
# 本模块不再反向引用 assembly（R-01b 全量真环基线 0：图保持单向，
# 仅 assembly 侧函数内延迟导入 engine）。

