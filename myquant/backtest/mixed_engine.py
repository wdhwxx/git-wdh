# 文件名：myquant/backtest/mixed_engine.py
"""
backtest/mixed_engine.py - 混合回测引擎（生产主力）
====================================================
bar 循环逐根处理，支持路径依赖/止损/条件单，是生产环境的主力引擎。

定位（与 vectorized / event 的差异）:
  - vectorized : 纯向量化，最快，面向大批量参数扫描（不支持止损/条件单）
  - mixed      : bar 循环 + 路径依赖/止损/条件单（本模块，生产主力）
  - event      : 逐事件引擎，保留作为真值基准（engine.py）

核心交易模型（严格 T+1，防未来函数）:
  对每根 bar i（T 日）:
    1. 撮合上一根 bar（T-1 日）提交的活跃订单 → 市价单用 T 日开盘价，
       限价/条件单用 T 日 high/low 判断触发（order_matcher 统一规则）；
    2. 止损检查（路径依赖）：持仓成本价回撤达 stop_loss_pct 时，
       STOP 卖单即时成交（intraday 止损，不等 T+1）；
    3. 更新持仓市值（用 T 日收盘价）；
    4. 调用策略生成信号（T 日收盘后）：
       - 策略实现 generate_mixed_loop(bar, context, position) -> 目标持仓股数
         （推荐，直接表达目标仓位，路径依赖友好）；
       - 否则回退 on_bar(bar, context) -> List[Signal]（与 event 引擎一致）；
    5. 订单提交到活跃订单队列 → T+1 日撮合；
    6. 记录 T 日净值。

防未来函数（规则4）:
  - 策略在 T 日收盘后生成信号，订单最早 T+1 日开盘成交；
  - 本行使用的数据截至 t 时刻：止损/条件单仅使用 T 日 bar 的 open/high/low
    （intraday 近似），信号仅使用 T 日收盘价，均不引用未来数据；
  - 数据加载严格按 config.end_date 过滤（双重保障）。

统一撮合与费用（三引擎共用同一套逻辑）:
  - 撮合: order_matcher.OrderMatcher（MARKET/LIMIT/STOP/STOP_LIMIT 四类）
  - 费用: fee_common.FeeCalculator（佣金/印花税/过户费/滑点）
  - 成交价为基础价（未含滑点），滑点金额单列并计入现金变动；
  - Trade.commission = 佣金+印花税+过户费（不含滑点，与 event Trade 口径一致）。

资金模型:
  - 全局资金池（与 event 引擎一致），初始资金 = config.initial_capital；
  - 撮合时实扣现金（不冻结下单预算），资金不足时现金为负并告警；
  - 引擎级止损即时成交；策略提交的条件单按 T+1 撮合。

依赖:
  标准库: logging, datetime, typing
  第三方: numpy, pandas
  本地  : myquant.models.backtest (BacktestConfig, BacktestResult, PerformanceMetrics)
          myquant.models.market_data (Bar, MarketEvent)
          myquant.models.trade (Order, OrderSide, OrderType, Position, Trade)
          myquant.strategies.base (Signal, StrategyBase, StrategyContext)
          myquant.backtest.data_loader (DataProviderBase)
          myquant.backtest.fee_common (FeeCalculator)
          myquant.backtest.order_matcher (OrderMatcher)
          myquant.backtest.metrics (MetricsCalculator)
          myquant.backtest.recorder (TradeRecorder)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from myquant.backtest.data_loader import DataProviderBase
from myquant.backtest.fee_common import FeeCalculator
from myquant.backtest.metrics import MetricsCalculator
from myquant.backtest.order_matcher import OrderMatcher
from myquant.backtest.recorder import TradeRecorder
from myquant.core.lookahead_guard import LookaheadGuard  # #H2修复：防未来函数校验器
from myquant.models.backtest import BacktestConfig, BacktestResult, PerformanceMetrics
from myquant.models.market_data import Bar, MarketEvent
from myquant.models.trade import Order, OrderSide, OrderType, Position, Trade
from myquant.risk.rules import StopLossRule  # KP-P3-07: 止损阈值与实盘同源
from myquant.strategies.base import Signal, SignalType, StrategyBase, StrategyContext

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# A股一手 = 100 股
DEFAULT_LOT_SIZE: int = 100

# 买入价格缓冲比例：预留/约束用 T 日收盘价 × (1+缓冲) 估算 T+1 成交额，
# 覆盖开盘跳空（默认 10% = 主板涨停上限），杜绝跳空导致的负现金透支。
DEFAULT_BUY_PRICE_BUFFER_PCT: float = 0.10

# 信号模式
SIGNAL_MODE_TARGET: str = "target"   # generate_mixed_loop -> 目标持仓股数
SIGNAL_MODE_EVENT: str = "event"     # on_bar -> List[Signal]（兼容 event 引擎接口）


# ─────────────────────────────────────────────
# MixedBacktestEngine - 混合回测引擎（生产主力）
# ─────────────────────────────────────────────

class MixedBacktestEngine:
    """bar 循环混合回测引擎（生产主力）。

    用法:
        engine = MixedBacktestEngine(config=config, strategy=strategy, data=data)
        result = engine.run()   # BacktestResult

    或从数据提供器加载（为实盘无缝对接）:
        engine = MixedBacktestEngine.from_data_provider(provider, config, strategy)
    """

    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        strategy: Optional[StrategyBase] = None,
        data: Optional[Dict[str, pd.DataFrame]] = None,
        data_provider: Optional[DataProviderBase] = None,
        fee_calculator: Optional[FeeCalculator] = None,
        matcher: Optional[OrderMatcher] = None,
        stop_loss_pct: float = 0.0,
        lot_size: int = DEFAULT_LOT_SIZE,
        name: str = "mixed_backtest",
        lookahead_guard: Optional[LookaheadGuard] = None,
        enable_lookahead_guard: bool = True,
    ):
        """初始化混合回测引擎。

        Args:
            config: 回测配置（extra 可含 stop_loss_pct，优先于构造参数）。
            strategy: 策略实例（优先 generate_mixed_loop，回退 on_bar）。
            data: 历史行情数据 {symbol: DataFrame}（方式1，向后兼容）。
            data_provider: 数据提供器（方式2，on_init 自动加载）。
            fee_calculator: 费用计算器（None 时按 config 创建，走统一 fee_common）。
            matcher: 统一撮合器（None 时创建默认实例）。
            stop_loss_pct: 止损比例（0=不启用；config.extra["stop_loss_pct"] 优先）。
            lot_size: A股一手股数（默认 100）。
            name: 引擎名称。

        Raises:
            ValueError: lot_size 非法、stop_loss_pct 越界时抛出。
        """
        if lot_size <= 0:
            raise ValueError(f"lot_size 必须 > 0: {lot_size!r}")
        if stop_loss_pct < 0 or stop_loss_pct >= 1:
            raise ValueError(f"stop_loss_pct 必须位于 [0, 1): {stop_loss_pct!r}")

        self.config: BacktestConfig = config or BacktestConfig()
        self.strategy: Optional[StrategyBase] = strategy
        self.data: Dict[str, pd.DataFrame] = data or {}
        self.data_provider: Optional[DataProviderBase] = data_provider
        self.lot_size: int = int(lot_size)
        self.name: str = name
        # 止损参数：config.extra 优先，其次构造参数
        cfg_stop = self.config.extra.get("stop_loss_pct", stop_loss_pct)
        self.stop_loss_pct: float = float(cfg_stop)
        # 买入价格缓冲（config.extra 外置，默认 10% 覆盖主板涨停）
        cfg_buffer = self.config.extra.get("buy_price_buffer_pct", DEFAULT_BUY_PRICE_BUFFER_PCT)
        self.buy_price_buffer: float = float(cfg_buffer)
        if self.buy_price_buffer < 0.0 or self.buy_price_buffer >= 1.0:
            raise ValueError(
                f"config.extra['buy_price_buffer_pct'] 必须位于 [0, 1): {self.buy_price_buffer!r}"
            )
        if self.stop_loss_pct < 0 or self.stop_loss_pct >= 1:
            raise ValueError(
                f"config.extra['stop_loss_pct'] 必须位于 [0, 1): {self.stop_loss_pct!r}"
            )

        # 核心组件
        self.fee_calculator: FeeCalculator = fee_calculator or FeeCalculator(
            commission_model=self.config.commission,
            slippage_model=self.config.slippage,
        )
        self.matcher: OrderMatcher = matcher or OrderMatcher()
        self.recorder: Optional[TradeRecorder] = None
        self.metrics_calc: Optional[MetricsCalculator] = None
        self.context: Optional[StrategyContext] = None

        # 运行状态
        self.cash: float = float(self.config.initial_capital)
        self.positions: Dict[str, Position] = {}
        self._active_orders: Dict[str, List[Order]] = {}
        # 已提交未成交买单的预估占用资金（T 日提交 → T+1 开盘成交，
        # 用于约束组合总仓位不超可用资金，杜绝透支/杠杆）
        self._pending_buy_notional: float = 0.0
        self._pending_buy_map: Dict[str, float] = {}
        self._signal_mode: str = SIGNAL_MODE_TARGET
        self._benchmark_data: Optional[pd.DataFrame] = None
        self._event_count: int = 0
        self._running: bool = False
        self._stopped: bool = False
        self._initialized: bool = False

        # 回测结果
        self.result: Optional[BacktestResult] = None

        # #H2修复：防未来函数校验器（默认开启，规则4强制）
        self._lookahead_guard: Optional[LookaheadGuard] = (
            lookahead_guard or (LookaheadGuard() if enable_lookahead_guard else None)
        )
        # P1-03：轻量防未来防线时间线缓存
        self._bar_timestamps: List[float] = []
        # 订单ID -> 信号日期（T日）映射，成交时校验 T+1
        self._order_signal_dates: Dict[str, str] = {}
        # 当前处理中的 bar（submit_order 无 bar 参数，信号日期经由此处传递）
        self._current_bar: Optional[Bar] = None

        logger.info(
            "混合引擎初始化: 标的数=%d, lot=%d, 止损=%.2f%%, 初始资金=%.0f",
            len(self.data),
            self.lot_size,
            self.stop_loss_pct,
            self.config.initial_capital,
        )

    # ─────────────────────────────────────────
    # 便捷构造：从数据提供器创建
    # ─────────────────────────────────────────

    @classmethod
    def from_data_provider(
        cls,
        data_provider: DataProviderBase,
        config: BacktestConfig,
        strategy: Optional[StrategyBase] = None,
        name: str = "mixed_backtest",
    ) -> "MixedBacktestEngine":
        """从数据提供器创建混合引擎（为实盘无缝对接）。

        Args:
            data_provider: 数据提供器。
            config: 回测配置（symbols/start_date/end_date 用于加载）。
            strategy: 策略实例。
            name: 引擎名称。

        Returns:
            MixedBacktestEngine 实例。
        """
        return cls(
            config=config,
            strategy=strategy,
            data_provider=data_provider,
            name=name,
        )

    # ─────────────────────────────────────────
    # 生命周期：初始化
    # ─────────────────────────────────────────

    def on_init(self, config: Optional[Dict[str, Any]] = None) -> None:
        """初始化引擎：加载数据、校验策略接口、初始化记录器与上下文。

        Args:
            config: 可选扩展配置（兼容 BaseEngine 生命周期）。
        """
        logger.info("=" * 60)
        logger.info("混合回测引擎初始化开始")
        logger.info("=" * 60)

        if not self.data and self.data_provider is not None:
            self._load_data_from_provider()

        if self.strategy is None:
            raise ValueError("mixed 引擎必须提供 strategy")
        # 策略接口探测：generate_mixed_loop 被覆盖 → target 模式；否则回退 on_bar
        if self.strategy.supports_mode("mixed"):
            self._signal_mode = SIGNAL_MODE_TARGET
            logger.info("策略 %s 使用目标持仓模式 generate_mixed_loop", self.strategy.name)
        else:
            self._signal_mode = SIGNAL_MODE_EVENT
            logger.info("策略 %s 使用事件信号模式 on_bar（兼容模式）", self.strategy.name)

        # 记录器与绩效计算器
        self.recorder = TradeRecorder(initial_capital=self.config.initial_capital)
        self.metrics_calc = MetricsCalculator(risk_free_rate=self.config.risk_free_rate)

        # 策略上下文（portfolio/executor 均指向本引擎，与 event 引擎 executor=broker 同构）
        self.context = StrategyContext(
            data=self.data,
            portfolio=self,
            executor=self,
            config=self.config.to_dict(),
            extra={"engine_mode": "mixed"},
        )
        self.strategy.on_init(self.context)
        logger.info("策略初始化完成: %s", self.strategy.name)

        # 基准数据
        if self.config.benchmark:
            if self.config.benchmark in self.data:
                self._benchmark_data = self.data[self.config.benchmark]
            elif self.data_provider is not None:
                self._benchmark_data = self.data_provider.get_bars(
                    symbol=self.config.benchmark,
                    start_date=self.config.start_date or None,
                    end_date=self.config.end_date or None,
                )

        self._initialized = True
        logger.info(
            "混合引擎初始化完成: 资金=%.0f, 标的数=%d, 策略=%s, 信号模式=%s",
            self.config.initial_capital,
            len(self.data),
            self.strategy.name,
            self._signal_mode,
        )

    # ─────────────────────────────────────────
    # 数据加载（防未来函数：严格按 config.end_date 过滤）
    # ─────────────────────────────────────────

    def _load_data_from_provider(self) -> None:
        """从 DataProvider 加载数据（严格 end_date 过滤，防未来函数）。"""
        if self.data_provider is None:
            return
        symbols = self.config.symbols
        if not symbols:
            logger.warning("config.symbols 为空，无法加载数据")
            return
        period = self._frequency_to_period(self.config.frequency)
        loaded = self.data_provider.load_multiple(
            symbols=symbols,
            start_date=self.config.start_date or None,
            end_date=self.config.end_date or None,
            period=period,
        )
        self.data = loaded
        total_bars = sum(len(df) for df in self.data.values() if df is not None)
        logger.info("数据加载完成: %d/%d 标的, %d 根K线", len(self.data), len(symbols), total_bars)

    @staticmethod
    def _frequency_to_period(frequency: str) -> str:
        """将配置频率转换为数据提供器 period 参数。"""
        freq = (frequency or "1d").lower().strip()
        mapping = {
            "1d": "daily", "daily": "daily", "d": "daily",
            "1w": "weekly", "weekly": "weekly", "w": "weekly",
            "1m": "minute", "minute": "minute", "m": "minute",
        }
        return mapping.get(freq, freq)

    # ─────────────────────────────────────────
    # 主循环：run
    # ─────────────────────────────────────────

    def run(self) -> BacktestResult:
        """运行混合回测主循环（逐 bar 执行 T+1 时序）。

        Returns:
            BacktestResult: 完整回测结果（净值曲线/成交记录/绩效指标）。
        """
        if not self._initialized:
            self.on_init()

        if not self.data:
            logger.error("无历史数据，无法运行混合回测")
            self.result = BacktestResult(config=self.config)
            self.result.mark_failed("无历史数据")
            return self.result

        # #H2修复：防未来函数校验器重置（每次回测独立校验）
        if self._lookahead_guard:
            self._lookahead_guard.reset()
            self._order_signal_dates.clear()

        logger.info("=" * 60)
        logger.info("混合回测主循环开始")
        logger.info("=" * 60)
        self._running = True

        # 重置运行状态（防御：重复 run 时清空上次状态）
        self.cash = float(self.config.initial_capital)
        self.positions = {}
        self._active_orders = {}
        # 防御：重复 run 时清空上次的买单占用状态
        self._pending_buy_notional = 0.0
        self._pending_buy_map = {}
        if self.recorder is not None:
            self.recorder.reset()

        self.result = BacktestResult(config=self.config)
        self.result.status = "running"

        # 构建统一时间序列（多标的按日期对齐）
        all_bars = self._build_unified_bars()

        # 逐 bar 处理（生产主力：bar 循环）
        for bar in all_bars:
            if not self._running:
                break
            event = MarketEvent(
                event_type="bar",
                data=bar,
                symbol=bar.symbol,
                timestamp=bar.timestamp,
            )
            self._process_bar(event)

        # 停止并生成结果（P0-01：防未来校验已在 on_stop 内、mark_complete 之前执行）
        self.on_stop()
        self._running = False
        logger.info("=" * 60)
        logger.info("混合回测主循环结束")
        logger.info("=" * 60)
        return self.result

    # ─────────────────────────────────────────
    # 单 bar 处理（核心逐 bar 循环）
    # ─────────────────────────────────────────

    def _process_bar(self, event: MarketEvent) -> None:
        """处理单根 bar（严格 T+1 时序）。

        Args:
            event: 市场事件（含 bar 数据）。
        """
        bar = event.data if hasattr(event, "data") else event
        if not isinstance(bar, Bar):
            return
        symbol = bar.symbol
        # #H2修复：记录当前 bar（策略信号以 T 日收盘计，成交以 T+1 计）
        self._current_bar = bar

        # ── 步骤1：撮合上一根 bar 提交的活跃订单（T 日开盘/高低价）──
        #    成交在 _apply_fill 内统一记录到 recorder
        self._match_active_orders(symbol, bar)

        # ── 步骤2：止损检查（路径依赖，STOP 卖单即时成交）──
        self._check_stop_loss(bar)

        # ── 步骤3：更新持仓市值（用 T 日收盘价）──
        pos = self.positions.get(symbol)
        if pos is not None:
            pos.update_market_price(bar.close)

        # ── 步骤4：策略生成信号（T 日收盘后）──
        self._generate_and_submit(bar)

        # ── 步骤5：记录 T 日净值（用 T 日收盘价计算市值）──
        if self.recorder is not None:
            account = self.get_account()
            benchmark_equity = self._calc_benchmark_equity(bar.timestamp)
            self.recorder.record_equity(
                timestamp=bar.timestamp,
                cash=account["cash"],
                position_value=account["market_value"],
                benchmark_equity=benchmark_equity,
            )

        self._event_count += 1

    # ─────────────────────────────────────────
    # 撮合活跃订单（条件单/限价单/市价单）
    # ─────────────────────────────────────────

    def _match_active_orders(self, symbol: str, bar: Bar) -> List[Trade]:
        """撮合某标的的活跃订单（统一走 order_matcher）。

        市价单以开盘价成交；限价/条件单以 T 日 high/low 判断触发。
        未成交订单保留在队列中（多日有效），回测结束时由 on_stop 取消。

        Args:
            symbol: 标的代码。
            bar: 当前 bar。

        Returns:
            本 bar 产生的成交记录列表。
        """
        orders = self._active_orders.get(symbol, [])
        if not orders:
            return []
        trades: List[Trade] = []
        remaining: List[Order] = []
        for order in orders:
            fill = self.matcher.match(order, bar)
            if fill is None or fill.quantity <= 0:
                remaining.append(order)  # 未成交，继续挂单
                continue
            trade = self._apply_fill(
                symbol=symbol,
                order=order,
                bar=bar,
                fill_price=float(fill.price),
                fill_qty=float(fill.quantity),
                reason=order.tag or "active_order",
            )
            trades.append(trade)
        self._active_orders[symbol] = remaining
        return trades

    # ─────────────────────────────────────────
    # 止损检查（路径依赖）
    # ─────────────────────────────────────────

    def _check_stop_loss(self, bar: Bar) -> None:
        """检查持仓止损（STOP 卖单即时成交，intraday 近似）。

        当 T 日最低价触及成本价×(1-stop_loss_pct) 时，以 STOP 卖单规则
        成交全部持仓（成交价 = min(止损价, 开盘价)，由 order_matcher 决定）。

        Args:
            bar: 当前 bar。
        """
        if self.stop_loss_pct <= 0 or self.strategy is None:
            return
        pos = self.positions.get(bar.symbol)
        if pos is None or pos.quantity <= 0 or pos.avg_cost <= 0:
            return
        if not hasattr(self, "_stop_loss_rule"):
            self._stop_loss_rule = StopLossRule(max_loss_pct=self.stop_loss_pct)
        stop_price = self._stop_loss_rule.stop_price(pos.avg_cost)
        stop_order = Order(
            symbol=bar.symbol,
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            quantity=pos.quantity,
            stop_price=stop_price,
            strategy_id=self.strategy.name,
            tag="stop_loss",
        )
        fill = self.matcher.match(stop_order, bar)
        if fill is not None and fill.quantity > 0:
            self._apply_fill(
                symbol=bar.symbol,
                order=stop_order,
                bar=bar,
                fill_price=float(fill.price),
                fill_qty=float(fill.quantity),
                reason="stop_loss",
            )
            logger.info(
                "止损触发: %s 成本=%.2f 止损价=%.2f 成交=%.0f股@%.2f",
                bar.symbol,
                pos.avg_cost,
                stop_price,
                float(fill.quantity),
                float(fill.price),
            )

    # ─────────────────────────────────────────
    # 成交应用（统一费用/持仓/资金/Trade 记录）
    # ─────────────────────────────────────────

    def _apply_fill(
        self,
        symbol: str,
        order: Order,
        bar: Bar,
        fill_price: float,
        fill_qty: float,
        reason: str,
    ) -> Trade:
        """应用一笔成交：统一走 fee_common 费用 + 更新持仓/资金 + 记录 Trade。

        Args:
            symbol: 标的代码。
            order: 订单对象。
            bar: 当前 bar（volume 用于滑点计算）。
            fill_price: 基础成交价（未含滑点，order_matcher 返回）。
            fill_qty: 成交数量。
            reason: 成交原因（订单 tag 或 "stop_loss"）。

        Returns:
            Trade 成交记录。
        """
        qty = float(fill_qty)
        price = float(fill_price)
        # 统一费用（佣金/印花税/过户费/滑点）
        fees = self.fee_calculator.calc_fee(
            side=order.side,
            notional=qty * price,
            price=price,
            quantity=qty,
            volume=bar.volume,
        )
        # 更新持仓（成交价为基础价，滑点单列；与 vectorized 成本口径一致）
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self.positions[symbol] = pos
        pos.apply_fill(qty, price, order.side)

        # 更新资金（现金变动含滑点：总费用 = 佣金+印花税+过户费+滑点）
        if order.side == OrderSide.BUY:
            self.cash -= qty * price + fees.total_cost
            # 现金已按实际成交额扣减，释放该订单的预估占用
            self._release_pending_buy(order.order_id)
        else:
            self.cash += qty * price - fees.total_cost
        if self.cash < -1.0:
            logger.warning("标的 %s 出现负现金 %.2f（资金不足，按透支处理）", symbol, self.cash)

        # Trade 记录（commission 为佣金+印花税+过户费，不含滑点；与 event 口径一致）
        trade = Trade(
            order_id=order.order_id,
            symbol=symbol,
            side=order.side,
            quantity=qty,
            price=price,
            commission=float(fees.commission + fees.stamp_tax + fees.transfer_fee),
            slippage=float(fees.slippage),
            timestamp=bar.timestamp,
            strategy_id=order.strategy_id,
        )
        # 更新订单状态为已成交
        order.fill(qty, price)
        # 统一记录成交（活跃订单撮合 / 止损即时成交均走此路径）
        if self.recorder is not None:
            self.recorder.record_trade(trade, signal_reason=reason)
        # #H2修复：记录成交到防未来函数校验器（铁律3：trade_date 必须 > signal_date）
        if self._lookahead_guard:
            trade_date = self._bar_date_str(bar)
            signal_date = self._order_signal_dates.get(order.order_id, "")
            side_val = order.side.value if hasattr(order.side, "value") else str(order.side)
            self._lookahead_guard.record_trade(
                symbol=symbol,
                trade_date=trade_date,
                signal_date=signal_date,
                price_type="OPEN",
                side=side_val,
            )
        logger.debug(
            "成交: %s %s %.0f股@%.2f 手续费=%.2f 滑点=%.2f (%s)",
            symbol,
            order.side.value,
            qty,
            price,
            trade.commission,
            trade.slippage,
            reason,
        )
        return trade

    # ─────────────────────────────────────────
    # 策略信号生成与提交
    # ─────────────────────────────────────────

    def _generate_and_submit(self, bar: Bar) -> None:
        """按策略信号模式生成信号并提交订单。"""
        if self._signal_mode == SIGNAL_MODE_TARGET:
            self._execute_target(bar)
        else:
            self._execute_signals(bar)

    def _execute_target(self, bar: Bar) -> None:
        """目标持仓模式：generate_mixed_loop -> 目标股数 -> 市价调仓订单。"""
        if self.strategy is None or self.context is None:
            return
        position = self.get_position(bar.symbol)
        target = self.strategy.generate_mixed_loop(bar, self.context, position)
        if target is None:
            return
        try:
            target_val = float(target)
        except (TypeError, ValueError):
            logger.warning(
                "generate_mixed_loop 返回非数值 %r，忽略（symbol=%s）",
                target,
                bar.symbol,
            )
            return
        if not np.isfinite(target_val) or target_val < 0:
            logger.warning(
                "generate_mixed_loop 返回非法目标 %.4f，忽略（symbol=%s）",
                target_val,
                bar.symbol,
            )
            return
        # 目标持仓取整到一手
        target_lot = float(np.floor(target_val / self.lot_size) * self.lot_size)
        current = position.quantity if position is not None else 0.0
        delta = target_lot - current
        if abs(delta) < 1e-9:
            return
        if delta > 0:
            side = OrderSide.BUY
            qty = delta
            # ★ 现金约束（防透支/杠杆）：买入不超可用现金（当前现金 - 已提交买单占用）
            #    订单 T+1 开盘成交，此处用 T 日收盘价 ×(1+缓冲) 估算成交额，
            #    覆盖开盘跳空（本行数据截至 T 日，防未来函数）
            available = max(0.0, self.available_cash())
            est_price = float(bar.close) * (1.0 + self.buy_price_buffer)
            max_shares = float(
                np.floor(available / max(est_price, 1e-9) / self.lot_size) * self.lot_size
            )
            # 费用缓冲：含估算费用后仍不超可用现金（防费用导致透支）
            while max_shares >= self.lot_size:
                est_total = max_shares * est_price + self.fee_calculator.calc_fee(
                    side=OrderSide.BUY,
                    notional=max_shares * est_price,
                    price=float(bar.close),
                    quantity=max_shares,
                    volume=float(bar.volume),
                ).total_cost
                if est_total <= available + 1e-9:
                    break
                max_shares -= float(self.lot_size)
            qty = min(qty, max_shares)
            if qty <= 1e-9:
                logger.debug(
                    "买入信号因资金不足跳过: %s target=%.0f 可用=%.2f",
                    bar.symbol,
                    target_lot,
                    self.available_cash(),
                )
                return
        else:
            side = OrderSide.SELL
            qty = min(abs(delta), current)  # 卖出不超持仓
            if qty <= 1e-9:
                return
        order = self.strategy.create_order(
            symbol=bar.symbol,
            side=side,
            quantity=qty,
            order_type=OrderType.MARKET,
            reason=f"target={target_lot:.0f}",
        )
        self.submit_order(order)
        if side == OrderSide.BUY:
            # 记录本次买单占用：带缓冲估算价 + 估算费用（成交/取消时释放）
            est_price = float(bar.close) * (1.0 + self.buy_price_buffer)
            est_fee = self.fee_calculator.calc_fee(
                side=OrderSide.BUY,
                notional=qty * est_price,
                price=float(bar.close),
                quantity=qty,
                volume=float(bar.volume),
            )
            notional_est = qty * est_price + est_fee.total_cost
            self._pending_buy_notional += notional_est
            self._pending_buy_map[order.order_id] = notional_est

    def _execute_signals(self, bar: Bar) -> None:
        """事件信号模式（兼容）：on_bar -> List[Signal]（与 event 引擎一致）。"""
        if self.strategy is None or self.context is None:
            return
        # ── P0-07 因果切片：逐 bar 推进 context.current_bar_ts + 开启切片 ──
        # Bar.timestamp 是秒级 Unix float（time.time()），必须用 unit='s'
        try:
            self.context.current_bar_ts = pd.Timestamp(bar.timestamp, unit='s')
            self.context._slicing_enabled = True
        except Exception:
            pass
        signals = self.strategy.on_bar(bar, self.context)
        if not signals:
            return
        for signal in signals:
            if not isinstance(signal, Signal) or signal.is_hold:
                continue
            if signal.is_buy:
                quantity = self._calc_buy_quantity(
                    signal.symbol, bar.close, volume=float(bar.volume)
                )
                if quantity <= 0:
                    continue
                order = signal.to_order(quantity=quantity, order_type=OrderType.MARKET)
            else:
                position = self.get_position(signal.symbol)
                if position is None or position.quantity <= 0:
                    continue
                order = signal.to_order(
                    quantity=position.quantity, order_type=OrderType.MARKET
                )
            if order is not None:
                self.submit_order(order)
                if order.side == OrderSide.BUY:
                    # 与 target 模式一致：记录买单占用（带缓冲估算价 + 费用，成交/取消时释放）
                    est_price = float(bar.close) * (1.0 + self.buy_price_buffer)
                    est_fee = self.fee_calculator.calc_fee(
                        side=OrderSide.BUY,
                        notional=order.quantity * est_price,
                        price=float(bar.close),
                        quantity=order.quantity,
                        volume=float(bar.volume),
                    )
                    notional_est = order.quantity * est_price + est_fee.total_cost
                    self._pending_buy_notional += notional_est
                    self._pending_buy_map[order.order_id] = notional_est

    def _calc_buy_quantity(
        self, symbol: str, price: float, volume: float = 0.0
    ) -> int:
        """计算买入数量（策略可覆盖 calc_buy_quantity，默认 20% 仓位）。

        Args:
            symbol: 标的代码。
            price: 参考价（T 日收盘价）。
            volume: 参考成交量（费用估算用，默认 0）。

        Returns:
            买入股数（100 股整数倍，≥0）。
        """
        if self.strategy is not None and hasattr(self.strategy, "calc_buy_quantity"):
            return self.strategy.calc_buy_quantity(price, self.context)  # type: ignore[no-any-return]
        cash = self.available_cash()
        max_amount = cash * 0.20
        est_price = price * (1.0 + self.buy_price_buffer)
        shares = int(max_amount / est_price / 100) * 100
        # 费用缓冲：含估算费用后仍不超预算（防费用导致透支）
        while shares >= 100:
            est_total = shares * est_price + self.fee_calculator.calc_fee(
                side=OrderSide.BUY,
                notional=shares * est_price,
                price=price,
                quantity=shares,
                volume=volume,
            ).total_cost
            if est_total <= max_amount + 1e-9:
                break
            shares -= 100
        return max(0, shares)

    # ─────────────────────────────────────────
    # 策略上下文接口（portfolio / executor）
    # ─────────────────────────────────────────

    def _release_pending_buy(self, order_id: str) -> None:
        """释放已提交买单的占用预留（订单成交或取消时调用）。

        Args:
            order_id: 订单 ID（_pending_buy_map 的键）。
        """
        notional = self._pending_buy_map.pop(order_id, 0.0)
        if notional > 0.0:
            self._pending_buy_notional = max(0.0, self._pending_buy_notional - notional)

    def submit_order(self, order: Order) -> Order:
        """提交订单到活跃订单队列（T+1 撮合）。

        供策略通过 context.executor.submit_order 调用（与 event 引擎同构）。

        Args:
            order: 待提交订单。

        Returns:
            已提交的订单（状态已置为 SUBMITTED）。
        """
        order.submit()
        self._active_orders.setdefault(order.symbol, []).append(order)
        # #H2修复：记录信号到防未来函数校验器（铁律3：信号T日收盘→T+1开盘）
        if self._lookahead_guard and self._current_bar is not None:
            signal_date = self._bar_date_str(self._current_bar)
            side_val = order.side.value if hasattr(order.side, "value") else str(order.side)
            signal_type = "BUY" if side_val in ("buy", "BUY") else "SELL"
            self._lookahead_guard.record_signal(
                symbol=order.symbol,
                signal_date=signal_date,
                signal_type=signal_type,
                data_end=signal_date,  # 信号基于 T 日收盘价，数据截止日 = T 日
                factor_names=[self.strategy.name] if self.strategy else None,
            )
            self._order_signal_dates[order.order_id] = signal_date
        logger.debug(
            "提交订单: %s %s %s %.0f股 (tag=%s)",
            order.symbol,
            order.side.value,
            order.order_type.value,
            order.quantity,
            order.tag,
        )
        return order

    def get_position(self, symbol: str) -> Optional[Position]:
        """获取某标的持仓（无则返回 None）。"""
        return self.positions.get(symbol)

    def get_cash(self) -> float:
        """获取当前现金。"""
        return self.cash

    def available_cash(self) -> float:
        """获取可用现金（当前现金 - 已提交未成交买单的预估占用）。"""
        return max(0.0, self.cash - self._pending_buy_notional)

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """获取活跃订单列表（可按标的过滤）。"""
        if symbol is not None:
            return list(self._active_orders.get(symbol, []))
        return [o for orders in self._active_orders.values() for o in orders]

    def get_account(self) -> Dict[str, Any]:
        """获取账户快照（现金/持仓市值/总资产）。"""
        market_value = sum(
            pos.market_value for pos in self.positions.values() if pos.quantity != 0
        )
        return {
            "cash": self.cash,
            "market_value": market_value,
            "total_assets": self.cash + market_value,
            "positions": {
                s: p.to_dict()
                for s, p in self.positions.items()
                if p.quantity != 0
            },
        }

    # ─────────────────────────────────────────
    # 基准净值
    # ─────────────────────────────────────────

    def _calc_benchmark_equity(self, timestamp: float) -> float:
        """计算基准净值（L8修复：按当前bar时间对齐，只使用<=当前时点数据，防未来函数）。"""
        if self._benchmark_data is None or len(self._benchmark_data) == 0:
            return 1.0
        try:
            import pandas as pd

            close = self._benchmark_data["收盘价"].astype(float)
            if len(close) == 0 or close.iloc[0] <= 0:
                return 1.0
            idx = self._benchmark_data.index
            if isinstance(idx, pd.DatetimeIndex):
                bench_ts = idx.astype("int64") // 10 ** 9
            elif "timestamp" in self._benchmark_data.columns:
                bench_ts = self._benchmark_data["timestamp"].astype(float)
            elif "date" in self._benchmark_data.columns:
                bench_ts = pd.to_datetime(self._benchmark_data["date"]).astype("int64") // 10 ** 9
            else:
                bench_ts = pd.to_datetime(idx.astype(str)).astype("int64") // 10 ** 9
            bench_ts = bench_ts.astype(float)

            mask = bench_ts <= timestamp
            pos = int(mask.sum()) - 1
            if pos < 0:
                return 1.0
            return float(close.iloc[pos] / close.iloc[0])
        except Exception as exc:  # 防御：基准计算异常时返回默认值
            logger.debug("基准净值计算失败，返回默认值1.0: %s", exc)
        return 1.0

    # ─────────────────────────────────────────
    # 统一时间序列
    # ─────────────────────────────────────────

    def _build_unified_bars(self) -> List[Bar]:
        """构建统一时间序列（多标的按日期对齐，防未来函数双重过滤）。"""
        all_bars: List[Bar] = []
        from myquant.backtest.data_loader import EN_TO_CN_COLUMN_MAP
        for symbol, df in self.data.items():
            if df is None or len(df) == 0:
                continue
            # 列名统一为中文：兼容英文列名的合成数据/数据库来源
            _rename = {en: cn for en, cn in EN_TO_CN_COLUMN_MAP.items() if en in df.columns}
            if _rename:
                df = df.rename(columns=_rename)
            # KP-P2-10/T-02：前收盘价兜底派生——数据源未含该列时按收盘价.shift(1) 派生，
            # 防涨跌停校验因 pre_close 恒 0 而空转（首行无前驱为 0，由撮合层跳过不误触发）。
            if "前收盘价" not in df.columns and "收盘价" in df.columns:
                df["前收盘价"] = pd.to_numeric(df["收盘价"], errors="coerce").shift(1)
            # ★ 防未来函数：按日期范围过滤（双重保障）
            if self.config.start_date:
                df = df[df.index.astype(str) >= self.config.start_date]
            if self.config.end_date:
                df = df[df.index.astype(str) <= self.config.end_date]
            for idx, row in df.iterrows():
                timestamp = self._row_to_timestamp(idx, row)
                all_bars.append(
                    Bar(
                        symbol=symbol,
                        open=float(row.get("开盘价", 0)),
                        high=float(row.get("最高价", 0)),
                        low=float(row.get("最低价", 0)),
                        close=float(row.get("收盘价", 0)),
                        pre_close=float(row.get("前收盘价", 0)),
                        volume=float(row.get("成交量", 0)),
                        timestamp=timestamp,
                    )
                )
        all_bars.sort(key=lambda b: (b.timestamp, b.symbol))
        # P1-03：缓存时间线供轻量防线收尾时钟单调校验
        self._bar_timestamps = [b.timestamp for b in all_bars]
        logger.info("统一时间序列构建完成: %d 根K线", len(all_bars))
        return all_bars

    @staticmethod
    def _row_to_timestamp(idx: Any, row: pd.Series) -> float:
        """将 DataFrame index 转为时间戳。"""
        if isinstance(idx, pd.Timestamp):
            return idx.timestamp()
        if hasattr(idx, "timestamp"):
            return idx.timestamp()  # type: ignore[attr-defined]
        if "timestamp" in row:
            return float(row["timestamp"])
        try:
            return pd.Timestamp(str(idx)).timestamp()
        except (ValueError, TypeError):
            return 0.0

    # ─────────────────────────────────────────
    # 生命周期：停止
    # ─────────────────────────────────────────

    # ─────────────────────────────────────────
    # #H2修复：防未来函数校验辅助
    # ─────────────────────────────────────────

    @staticmethod
    def _bar_date_str(bar: Bar) -> str:
        """将 Bar.timestamp 转为日期字符串（YYYY-MM-DD），异常返回空串跳过校验。"""
        from datetime import datetime

        try:
            return datetime.fromtimestamp(bar.timestamp).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            return ""

    def _run_lookahead_assert(self) -> bool:
        """回测结束统一校验防未来函数（P0-01修复）。

        本方法不读取任何行情数据，仅消费回测过程中按 t 时刻记录的 guard 快照。

        Returns:
            True 表示通过（或 guard 被显式关闭），调用方可标记 completed；
            False 表示发现违规，本方法已将结果 mark_failed，调用方不得再标记 completed。
        """
        if not self._lookahead_guard:
            return True
        try:
            guard_stats = self._lookahead_guard.stats
            logger.info(
                "防未来函数校验统计: 信号=%d, 成交=%d, 违规=%d",
                guard_stats["signals_recorded"],
                guard_stats["trades_recorded"],
                guard_stats["violations"],
            )
            self._lookahead_guard.assert_pass()
            logger.info("✅ 防未来函数校验通过：未发现未来函数违规")
            # P1-03：第二道防线——轻量时间线单调校验
            # KP-P2-11/T-03：保留返回值，不再无条件 return True——
            # 否则 light guard 已 mark_failed 时 on_stop 仍会 mark_complete 覆盖失败态。
            ok_light = self._run_light_guard_check()
            return ok_light
        except Exception as exc:
            # P0-01修复：违规回测必须标记为失败，禁止以 completed / is_success=True 收尾
            logger.error("❌ 防未来函数校验未通过：%s", exc)
            if self.result is not None:
                if hasattr(self.result, "extra"):
                    self.result.extra["lookahead_guard_violations"] = (
                        self._lookahead_guard.violations
                    )
                    self.result.extra["lookahead_guard_passed"] = False
                if hasattr(self.result, "mark_failed"):
                    self.result.mark_failed(f"防未来函数校验未通过: {exc}")
            return False

    def _run_light_guard_check(self) -> bool:
        """P1-03：第二道防未来防线（轻量）——沿时间线逐 bar 推进时钟，
        检测时间线倒退（乱序数据）；收尾 assert_pass。"""
        from myquant.core.exceptions import LookaheadError as _LightErr
        from myquant.core.light_lookahead_guard import get_light_lookahead_guard

        lg = get_light_lookahead_guard()
        if not lg.enabled:
            return True
        try:
            lg.reset()
            for ts in self._bar_timestamps:
                lg.on_bar_close(ts)
            lg.assert_pass()
            if self.result is not None and hasattr(self.result, "extra"):
                self.result.extra["light_lookahead_passed"] = True
            return True
        except _LightErr as exc:
            logger.error("❌ 轻量防未来防线校验未通过: %s", exc)
            if self.result is not None and hasattr(self.result, "mark_failed"):
                self.result.mark_failed(f"轻量防未来防线校验未通过: {exc}")
            return False

    def on_stop(self) -> None:
        """停止引擎：取消活跃订单、计算绩效指标、生成回测结果。"""
        logger.info("混合引擎停止，开始计算绩效...")

        # 1. 取消所有活跃订单（回测结束，剩余挂单作废）
        for orders in self._active_orders.values():
            for order in orders:
                order.cancel()
                if order.side == OrderSide.BUY:
                    self._release_pending_buy(order.order_id)
        self._active_orders = {}
        # 防御：清空残留占用（全部订单已取消）
        self._pending_buy_notional = 0.0
        self._pending_buy_map = {}

        # 2. 计算绩效指标
        metrics = PerformanceMetrics()
        if self.recorder is not None and self.metrics_calc is not None:
            equity_curve = self.recorder.get_equity_curve()
            trades = self.recorder.get_trades()
            metrics = self.metrics_calc.calculate(
                equity_curve=equity_curve,
                trades=trades,
                benchmark_curve=None,
                start_date=self.config.start_date,
                end_date=self.config.end_date,
            )
        else:
            logger.warning("记录器或绩效计算器未初始化，绩效指标为空")

        # 3. 填充回测结果
        if self.result is None:
            self.result = BacktestResult(config=self.config)
        self.result.metrics = metrics
        self.result.equity_curve = self.recorder.get_equity_curve() if self.recorder else []
        self.result.trades = self.recorder.get_trades() if self.recorder else []
        self.result.extra["engine_mode"] = "mixed"
        self.result.extra["signal_mode"] = self._signal_mode
        self.result.extra["stop_loss_pct"] = self.stop_loss_pct
        # P0-01修复：先防未来校验，通过才标记 completed；失败已 mark_failed，不得覆盖
        if self._run_lookahead_assert():
            self.result.mark_complete()

        self._stopped = True
        logger.info(
            "混合回测完成: 总收益=%.2f%%, 夏普=%.2f, 最大回撤=%.2f%%, 交易次数=%d",
            metrics.total_return,
            metrics.sharpe_ratio,
            metrics.max_drawdown,
            metrics.total_trades,
        )

    # ─────────────────────────────────────────
    # 快捷方法
    # ─────────────────────────────────────────

    def get_result(self) -> Optional[BacktestResult]:
        """获取回测结果。"""
        return self.result

    def stop(self) -> None:
        """停止回测。"""
        self._running = False
        self._stopped = True

    def __repr__(self) -> str:
        return (
            f"MixedBacktestEngine(strategy={getattr(self.strategy, 'name', 'None')}, "
            f"symbols={len(self.data)}, mode={self._signal_mode}, "
            f"stop_loss={self.stop_loss_pct:.2%})"
        )




if __name__ == "__main__":
    # 自测已迁至 myquant/backtest/mixed_selftest.py（P0-00A 第二刀，回写#302）
    from myquant.backtest.mixed_selftest import run_mixed_selftest

    raise SystemExit(run_mixed_selftest())
