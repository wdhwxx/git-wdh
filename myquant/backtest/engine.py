# 文件名：myquant/backtest/engine.py
"""
backtest/engine.py - 回测引擎
================================
事件驱动回测引擎，继承 engine.base.BaseEngine，实现同步时间序列事件循环。

设计原则（规则2：为实盘无缝对接准备）:
  - 继承 BaseEngine，与实盘引擎(live/engine.py)共用同一生命周期接口
  - 使用 SimBroker 撮合订单，实盘切换为 BrokerExecutor 零修改
  - 使用 StrategyContext 调用策略，策略不依赖具体引擎
  - 使用 TradeRecorder 记录交易和净值，实盘可复用
  - 使用 MetricsCalculator 计算绩效，回测/实盘共用
  - ★ 数据层抽象：支持 DataProviderBase，回测用 HistoricalDataProvider，
    实盘用 RealtimeDataProvider，引擎只依赖抽象接口，切换零修改

数据输入方式（两种，向后兼容）:
  方式1（传统）: 直接传入 data=Dict[str, pd.DataFrame]
  方式2（推荐）: 传入 data_provider=DataProviderBase，引擎自动从provider加载
                 便捷方法: BacktestEngine.from_data_provider(provider, config, strategy)

★★★ 严格T+1成交时序（规则4：禁用未来函数）★★★
  对于每根bar i（T日）:
    1. 撮合上一根bar（T-1日）创建的订单 → 用T日开盘价成交
    2. 更新组合市场价 → 用T日收盘价
    3. 调用策略 on_bar() → 基于T日收盘价生成信号
    4. 策略创建的订单提交到broker → T+1日开盘撮合
    5. 记录T日净值（用T日收盘价计算市值）

  关键: 市价单以开盘价成交（不是收盘价），T日信号T+1日才成交。
  策略在T日收盘后看到的信号，最早T+1日开盘才能成交。

★★★ 防未来函数：数据加载 ★★★
  从 DataProvider 加载数据时，严格传入 config.end_date，
  provider 内部过滤掉 end_date 之后的所有数据，绝不返回未来数据。
  _build_unified_bars() 中再次按日期过滤，双重保障。
"""

__version__ = "0.2.0"
__author__ = "Pythonwdhmm"

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.core.lookahead_guard import (
    LookaheadGuard,
    get_lookahead_guard,  # #H2修复：scheduler 统一注入单例
)
from myquant.engine.base import BaseEngine
from myquant.execution.sim_broker import SimBroker
from myquant.models.backtest import BacktestConfig, BacktestResult
from myquant.models.market_data import Bar, MarketEvent
from myquant.strategies.base import Signal, SignalType, StrategyBase, StrategyContext
from myquant.backtest.data_loader import DataProviderBase
from myquant.backtest.metrics import MetricsCalculator
from myquant.backtest.recorder import TradeRecorder
from myquant.backtest.mixed_engine import MixedBacktestEngine
from myquant.backtest.vectorized_engine import VectorizedBacktestEngine

logger = get_logger(__name__)


class BacktestEngine(BaseEngine):
    """
    事件驱动回测引擎

    继承 BaseEngine，实现同步时间序列事件循环。

    核心流程:
      1. on_init(config): 初始化broker、策略、记录器、从DataProvider加载数据
      2. run(): 逐bar遍历历史数据，执行T+1成交时序
      3. on_stop(): 计算绩效指标，生成回测结果

    使用方式（方式1：传统直接传数据）:
        config = BacktestConfig(
            strategy_name="rsrs",
            symbols=["600519.SH", "000001.SZ"],
            start_date="2020-01-01",
            end_date="2024-12-31",
            initial_capital=1000000,
        )
        engine = BacktestEngine(config=config, strategy=rsrs_strategy, data=data_dict)
        result = engine.run()

    使用方式（方式2：推荐，从DataProvider加载，为实盘无缝对接）:
        from myquant.backtest.data_loader import HistoricalDataProvider
        provider = HistoricalDataProvider()
        engine = BacktestEngine.from_data_provider(
            data_provider=provider,
            config=config,
            strategy=rsrs_strategy,
        )
        result = engine.run()
    """

    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        strategy: Optional[StrategyBase] = None,
        data: Optional[Dict[str, pd.DataFrame]] = None,
        data_provider: Optional[DataProviderBase] = None,
        name: str = "backtest",
        lookahead_guard: Optional[LookaheadGuard] = None,
        enable_lookahead_guard: bool = True,
    ):
        """
        初始化回测引擎

        参数:
            config: 回测配置
            strategy: 策略实例
            data: 历史行情数据 {symbol: DataFrame}（传统方式，向后兼容）
            data_provider: 数据提供器（推荐方式，为实盘无缝对接设计）
                           传入后，on_init() 中自动从provider加载数据
            name: 引擎名称
            lookahead_guard: 防未来函数校验器实例（None则自动创建，#S3修复）
            enable_lookahead_guard: 是否启用防未来函数显式校验（默认True，规则4要求强制开启）

        数据优先级:
            如果同时传入 data 和 data_provider，优先使用 data（向后兼容），
            data_provider 仅在 data 为空时生效。
        """
        super().__init__(name=name)
        self.config = config or BacktestConfig()
        self.strategy = strategy
        self.data = data or {}
        self.data_provider = data_provider

        # 核心组件
        self.broker: Optional[SimBroker] = None
        self.recorder: Optional[TradeRecorder] = None
        self.metrics_calc: Optional[MetricsCalculator] = None
        self.context: Optional[StrategyContext] = None

        # 回测结果
        self.result: Optional[BacktestResult] = None

        # 基准数据
        self._benchmark_data: Optional[pd.DataFrame] = None

        # #S3修复：防未来函数显式校验器（引擎内置能力，不再仅依赖入口脚本）
        self._enable_lookahead_guard = enable_lookahead_guard
        self._lookahead_guard: Optional[LookaheadGuard] = None
        if enable_lookahead_guard:
            self._lookahead_guard = lookahead_guard or LookaheadGuard()
        # 订单ID -> 信号日期映射（用于成交时关联信号日，校验T+1规则）
        self._order_signal_dates: Dict[str, str] = {}

        logger.info(f"回测引擎初始化: {self.config}")
        if data_provider:
            logger.info(f"数据提供器: {data_provider}")
        if self._lookahead_guard:
            logger.info(f"防未来函数校验器已启用: {self._lookahead_guard}")

    # ------------------------------------------------------------------
    # 便捷构造方法：从DataProvider创建引擎（为实盘无缝对接设计）
    # ------------------------------------------------------------------

    @classmethod
    def from_data_provider(
        cls,
        data_provider: DataProviderBase,
        config: BacktestConfig,
        strategy: Optional[StrategyBase] = None,
        name: str = "backtest",
    ) -> "BacktestEngine":
        """
        从数据提供器创建回测引擎（推荐方式）

        引擎在 on_init() 中自动从 data_provider 加载 config.symbols 的数据，
        严格按 config.end_date 过滤（防未来函数）。

        实盘无缝对接：将 HistoricalDataProvider 替换为 RealtimeDataProvider，
        引擎代码零修改。

        参数:
            data_provider: 数据提供器（HistoricalDataProvider 或 RealtimeDataProvider）
            config: 回测配置（symbols/start_date/end_date 用于数据加载）
            strategy: 策略实例
            name: 引擎名称

        返回:
            BacktestEngine 实例
        """
        return cls(
            config=config,
            strategy=strategy,
            data_provider=data_provider,
            name=name,
        )

    # ------------------------------------------------------------------
    # 生命周期：初始化
    # ------------------------------------------------------------------

    def on_init(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化回测引擎

        创建broker、策略上下文、记录器、绩效计算器，
        从 DataProvider 加载数据（防未来函数），调用策略on_init。
        """
        logger.info("=" * 60)
        logger.info("回测引擎初始化开始")
        logger.info("=" * 60)

        # #S3修复：每次回测开始前清空防未来函数校验记录
        if self._lookahead_guard:
            self._lookahead_guard.reset()
            self._order_signal_dates.clear()
            logger.debug("防未来函数校验器已重置")

        # 0. ★★★ 从 DataProvider 加载数据（防未来函数）★★★
        if not self.data and self.data_provider is not None:
            self._load_data_from_provider()

        # 1. 创建模拟券商
        self.broker = SimBroker(
            initial_capital=self.config.initial_capital,
            commission_model=self.config.commission,
            slippage_model=self.config.slippage,
        )
        self.broker.connect()

        # 2. 创建记录器
        self.recorder = TradeRecorder(initial_capital=self.config.initial_capital)

        # 3. 创建绩效计算器
        self.metrics_calc = MetricsCalculator(risk_free_rate=self.config.risk_free_rate)

        # 4. 创建策略上下文（此时 self.data 已加载完成）
        self.context = StrategyContext(
            data=self.data,
            portfolio=self.broker,
            executor=self.broker,
            config=self.config.to_dict(),
        )

        # 5. 初始化策略
        if self.strategy:
            self.strategy.on_init(self.context)
            logger.info(f"策略初始化完成: {self.strategy.name}")

        # 6. 加载基准数据
        if self.config.benchmark:
            if self.config.benchmark in self.data:
                self._benchmark_data = self.data[self.config.benchmark]
            elif self.data_provider is not None:
                # 基准不在symbols中，单独从provider加载
                self._benchmark_data = self.data_provider.get_bars(
                    symbol=self.config.benchmark,
                    start_date=self.config.start_date or None,
                    end_date=self.config.end_date or None,
                )
                if not self._benchmark_data.empty:
                    logger.info(
                        f"基准数据已加载: {self.config.benchmark}, "
                        f"{len(self._benchmark_data)} 根K线"
                    )

        self._initialized = True
        logger.info(
            f"回测引擎初始化完成: 资金={self.config.initial_capital:,.0f}, "
            f"标的数={len(self.data)}, 策略={self.strategy.name if self.strategy else 'None'}"
        )

    # ------------------------------------------------------------------
    # 数据加载：从DataProvider加载（防未来函数核心）
    # ------------------------------------------------------------------

    def _load_data_from_provider(self) -> None:
        """
        从 DataProvider 加载回测数据

        ★★★ 防未来函数 ★★★
        - 严格使用 config.end_date 作为截止日期，provider 内部过滤未来数据
        - 使用 config.start_date 作为开始日期
        - 只加载 config.symbols 中指定的标的
        - 加载后数据按日期升序排列

        数据加载失败的标的会被跳过并记录警告。
        """
        if self.data_provider is None:
            return

        symbols = self.config.symbols
        if not symbols:
            logger.warning("config.symbols 为空，无法从 DataProvider 加载数据")
            return

        start_date = self.config.start_date or None
        end_date = self.config.end_date or None
        period = self._frequency_to_period(self.config.frequency)

        logger.info(
            f"从 DataProvider 加载数据: symbols={symbols}, "
            f"range={start_date}~{end_date}, period={period}"
        )

        # 批量加载（provider.load_multiple 内部统一 end_date，防未来函数）
        loaded = self.data_provider.load_multiple(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            period=period,
        )

        self.data = loaded

        if not self.data:
            logger.error("从 DataProvider 未加载到任何有效数据")
        else:
            total_bars = sum(len(df) for df in self.data.values())
            logger.info(
                f"数据加载完成: {len(self.data)}/{len(symbols)} 个标的, "
                f"共 {total_bars} 根K线"
            )

    @staticmethod
    def _frequency_to_period(frequency: str) -> str:
        """
        将 BacktestConfig.frequency 转换为 DataProvider.get_bars() 的 period 参数

        映射:
          "1d" / "daily" / "D" -> "daily"
          "1w" / "weekly" / "W" -> "weekly"
          "1m" / "minute" / "M" -> "minute"
          其他 -> 原样返回
        """
        freq = (frequency or "1d").lower().strip()
        mapping = {
            "1d": "daily", "daily": "daily", "d": "daily",
            "1w": "weekly", "weekly": "weekly", "w": "weekly",
            "1m": "minute", "minute": "minute", "m": "minute",
        }
        return mapping.get(freq, freq)

    # ------------------------------------------------------------------
    # 生命周期：市场事件处理（核心）
    # ------------------------------------------------------------------

    def on_market_event(self, event: MarketEvent) -> None:
        """
        处理市场事件（单bar）

        ★★★ 严格T+1成交时序 ★★★
          1. 撮合上一根bar创建的订单 → 用当前bar开盘价成交
          2. 更新组合市场价 → 用当前bar收盘价
          3. 调用策略on_bar() → 基于当前bar收盘价生成信号
          4. 策略创建的订单提交到broker → 下一根bar开盘撮合
          5. 记录当前bar净值

        参数:
            event: 市场事件（包含bar数据）
        """
        bar = event.data if hasattr(event, 'data') else event
        if not isinstance(bar, Bar):
            return

        symbol = bar.symbol

        # ── 步骤1：撮合上一根bar创建的订单（用当前bar开盘价）──
        if self.broker:
            trades = self.broker.on_bar(bar)
            trade_date_str = self._bar_date_str(bar)
            for trade in trades:
                # 查找信号原因
                reason = ""
                order = self.broker.get_order(trade.order_id) if hasattr(self.broker, 'get_order') else None
                if order:
                    reason = order.tag
                self.recorder.record_trade(trade, signal_reason=reason)
                # #S3修复：记录成交到防未来函数校验器（铁律3：信号T日→T+1日开盘成交）
                if self._lookahead_guard:
                    signal_date = self._order_signal_dates.get(trade.order_id, "")
                    side = trade.side.value if hasattr(trade.side, 'value') else str(trade.side)
                    self._lookahead_guard.record_trade(
                        symbol=trade.symbol,
                        trade_date=trade_date_str,
                        signal_date=signal_date,
                        price_type="OPEN",
                        side=side,
                    )

        # ── 步骤2：更新组合市场价（用当前bar收盘价）──
        # SimBroker.on_bar已经更新了市场价，这里无需重复

        # ── 步骤3：调用策略on_bar()（基于当前bar收盘价生成信号）──
        if self.strategy and self.context:
            signals = self.strategy.on_bar(bar, self.context)
            for signal in signals:
                self._execute_signal(signal, bar)

        # ── 步骤4：记录当前bar净值（用当前bar收盘价计算市值）──
        if self.broker and self.recorder:
            account = self.broker.get_account()
            benchmark_equity = self._calc_benchmark_equity(bar.timestamp)
            self.recorder.record_equity(
                timestamp=bar.timestamp,
                cash=account['cash'],
                position_value=account['market_value'],
                benchmark_equity=benchmark_equity,
            )

        self._event_count += 1

    def _execute_signal(self, signal: Signal, bar: Bar) -> None:
        """
        执行信号：转换为订单并提交到broker

        ★★★ 防未来函数 ★★★
          信号在T日收盘后生成，订单提交后状态为SUBMITTED，
          将在T+1日开盘由broker.on_bar()撮合。
        """
        if signal.is_hold:
            return

        if signal.is_buy:
            # 买入：计算仓位
            quantity = self._calc_buy_quantity(symbol=signal.symbol, price=bar.close)
            if quantity <= 0:
                logger.debug(f"买入信号跳过: {signal.symbol} 数量为0（资金不足或满仓）")
                return
            order = signal.to_order(quantity=quantity, order_type=__import__('myquant.models.trade', fromlist=['OrderType']).OrderType.MARKET)
        else:
            # 卖出：全部持仓
            position = self.context.get_position(signal.symbol) if self.context else None
            if position is None or position.quantity <= 0:
                logger.debug(f"卖出信号跳过: {signal.symbol} 无持仓")
                return
            quantity = position.quantity
            order = signal.to_order(quantity=quantity, order_type=__import__('myquant.models.trade', fromlist=['OrderType']).OrderType.MARKET)

        if order and self.broker:
            self.broker.submit_order(order)
            logger.debug(
                f"提交订单: {order.symbol} {order.side.value} "
                f"{order.quantity:.0f}股 (信号: {signal.reason})"
            )
            # #S3修复：记录信号到防未来函数校验器（铁律4：信号T日收盘生成，因子仅用历史数据）
            if self._lookahead_guard:
                signal_date = self._bar_date_str(bar)
                signal_type = "BUY" if signal.is_buy else ("SELL" if signal.is_sell else "CLOSE")
                self._lookahead_guard.record_signal(
                    symbol=signal.symbol,
                    signal_date=signal_date,
                    signal_type=signal_type,
                    data_end=signal_date,  # 信号基于T日收盘价，数据截止日=T日
                    factor_names=[self.strategy.name] if self.strategy else None,
                )
                # 保存订单ID -> 信号日期映射，用于成交时校验T+1规则
                self._order_signal_dates[order.order_id] = signal_date

    def _calc_buy_quantity(self, symbol: str, price: float) -> int:
        """计算买入数量（基于策略的仓位管理规则）"""
        if not self.strategy or not self.context:
            return 0
        if hasattr(self.strategy, 'calc_buy_quantity'):
            return self.strategy.calc_buy_quantity(price, self.context)
        # 默认：20%仓位
        cash = self.context.get_cash()
        max_amount = cash * 0.20
        shares = int(max_amount / price / 100) * 100
        return max(0, shares)

    def _calc_benchmark_equity(self, timestamp: float) -> float:
        """计算基准净值（L8修复：按当前bar时间对齐，只使用<=当前时点数据，防未来函数）"""
        if self._benchmark_data is None or len(self._benchmark_data) == 0:
            return 1.0
        try:
            import pandas as pd

            close = self._benchmark_data['收盘价'].astype(float)
            if len(close) == 0 or close.iloc[0] <= 0:
                return 1.0
            # 基准数据时间轴：优先 index(DatetimeIndex)，其次 timestamp/date 列，最后字符串日期 index
            idx = self._benchmark_data.index
            if isinstance(idx, pd.DatetimeIndex):
                bench_ts = idx.astype("int64") // 10 ** 9
            elif 'timestamp' in self._benchmark_data.columns:
                bench_ts = self._benchmark_data['timestamp'].astype(float)
            elif 'date' in self._benchmark_data.columns:
                bench_ts = pd.to_datetime(self._benchmark_data['date']).astype("int64") // 10 ** 9
            else:
                bench_ts = pd.to_datetime(idx.astype(str)).astype("int64") // 10 ** 9
            bench_ts = bench_ts.astype(float)

            # 定位 <= 当前时点的最后一个基准行（防未来：当日收盘前不"预知"当日及之后行情）
            mask = bench_ts <= timestamp
            pos = int(mask.sum()) - 1
            if pos < 0:
                return 1.0
            return float(close.iloc[pos] / close.iloc[0])
        except Exception as e:
            logger.debug(f"基准净值计算失败，返回默认值1.0: {e}")
        return 1.0

    # ------------------------------------------------------------------
    # #S3修复：防未来函数校验辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _bar_date_str(bar: Bar) -> str:
        """
        将Bar的timestamp转换为日期字符串（YYYY-MM-DD）

        用于LookaheadGuard的日期比较（铁律1/3/4均基于日期字符串比较）。
        """
        from datetime import datetime
        try:
            return datetime.fromtimestamp(bar.timestamp).strftime('%Y-%m-%d')
        except (OSError, ValueError, OverflowError):
            # timestamp异常时返回空字符串，LookaheadGuard会跳过日期校验
            return ""

    def _run_lookahead_assert(self) -> None:
        """
        运行防未来函数统一校验（#S3修复）

        在回测结束时调用，校验所有记录的指标/信号/成交是否违反未来函数规则。
        校验失败时记录错误日志，但不中断回测结果（回测已完成，结果仍有效）。
        """
        if not self._lookahead_guard:
            return

        try:
            guard_stats = self._lookahead_guard.stats
            logger.info(
                f"防未来函数校验统计: "
                f"信号={guard_stats['signals_recorded']}, "
                f"成交={guard_stats['trades_recorded']}, "
                f"违规={guard_stats['violations']}"
            )
            self._lookahead_guard.assert_pass()
            logger.info("✅ 防未来函数校验通过：未发现未来函数违规")
        except Exception as e:
            # 校验失败时记录详细错误，但不中断回测结果
            logger.error(f"❌ 防未来函数校验未通过：{e}")
            # 将违规信息附加到回测结果（如果结果对象支持）
            if self.result is not None and hasattr(self.result, 'extra'):
                self.result.extra['lookahead_guard_violations'] = self._lookahead_guard.violations
                self.result.extra['lookahead_guard_passed'] = False

    # ------------------------------------------------------------------
    # 生命周期：停止
    # ------------------------------------------------------------------

    def on_stop(self) -> None:
        """
        停止回测引擎

        取消所有挂单，计算绩效指标，生成回测结果。
        """
        logger.info("回测引擎停止，开始计算绩效...")

        # 1. 取消所有活跃订单
        if self.broker:
            active_orders = self.broker.get_active_orders()
            for order in active_orders:
                self.broker.cancel_order(order.order_id)

        # 2. 计算绩效指标
        if self.recorder and self.metrics_calc:
            equity_curve = self.recorder.get_equity_curve()
            trades = self.recorder.get_trades()
            benchmark_curve = None  # 简化：基准曲线可从equity_curve提取

            metrics = self.metrics_calc.calculate(
                equity_curve=equity_curve,
                trades=trades,
                benchmark_curve=benchmark_curve,
                start_date=self.config.start_date,
                end_date=self.config.end_date,
            )
        else:
            from myquant.models.backtest import PerformanceMetrics
            metrics = PerformanceMetrics()

        # 3. 填充回测结果（result 已在 run() 主循环前创建，此处只填充数据，避免 started_at 被重置）
        if self.result is None:
            # 防御性：on_stop() 被异常调用且 result 尚未创建时
            self.result = BacktestResult(config=self.config)
        self.result.metrics = metrics
        self.result.equity_curve = self.recorder.get_equity_curve() if self.recorder else []
        self.result.trades = self.recorder.get_trades() if self.recorder else []
        self.result.mark_complete()

        # #S3修复：回测结束时运行防未来函数统一校验（引擎内置能力，不再仅依赖入口脚本）
        self._run_lookahead_assert()

        self._stopped = True
        logger.info(
            f"回测完成: 总收益={metrics.total_return:.2%}, "
            f"夏普={metrics.sharpe_ratio:.2f}, "
            f"最大回撤={metrics.max_drawdown:.2%}, "
            f"交易次数={metrics.total_trades}"
        )

    # ------------------------------------------------------------------
    # 主循环：run
    # ------------------------------------------------------------------

    def run(self) -> BacktestResult:
        """
        运行回测主循环

        逐bar遍历历史数据，执行T+1成交时序。

        返回:
            BacktestResult: 回测结果
        """
        if not self._initialized:
            self.on_init()

        if not self.data:
            logger.error("无历史数据，无法运行回测")
            self.result = BacktestResult(config=self.config)
            self.result.mark_failed("无历史数据")
            return self.result

        logger.info("=" * 60)
        logger.info("回测主循环开始")
        logger.info("=" * 60)

        self._running = True

        # 创建回测结果对象（在主循环前创建，确保 started_at 记录真实开始时间，运行耗时计算正确）
        self.result = BacktestResult(config=self.config)
        self.result.status = "running"

        # 构建统一的时间序列（多标的按日期对齐）
        all_bars = self._build_unified_bars()

        # 逐bar处理
        for bar in all_bars:
            if not self._running:
                break
            event = MarketEvent(
                event_type="bar",
                data=bar,
                symbol=bar.symbol,
                timestamp=bar.timestamp,
            )
            self.on_market_event(event)

        # 停止
        self.on_stop()
        self._running = False

        logger.info("=" * 60)
        logger.info("回测主循环结束")
        logger.info("=" * 60)

        return self.result

    def _build_unified_bars(self) -> List[Bar]:
        """
        构建统一的时间序列（多标的按日期对齐）

        将所有标的的K线按日期排序，同一日期的多只股票依次处理。

        ★★★ 防未来函数（双重保障）★★★
        从 DataProvider 加载时已按 config.end_date 过滤，
        此处再次按 config.start_date/end_date 过滤，确保不返回未来数据。
        """
        all_bars = []
        from myquant.backtest.data_loader import EN_TO_CN_COLUMN_MAP
        for symbol, df in self.data.items():
            if df is None or len(df) == 0:
                continue
            # 列名统一为中文：兼容英文列名的合成数据/数据库来源
            _rename = {en: cn for en, cn in EN_TO_CN_COLUMN_MAP.items() if en in df.columns}
            if _rename:
                df = df.rename(columns=_rename)
            # ★★★ 防未来函数：按日期范围过滤（双重保障）★★★
            if self.config.start_date:
                mask = df.index.astype(str) >= self.config.start_date
                df = df[mask]
            if self.config.end_date:
                mask = df.index.astype(str) <= self.config.end_date
                df = df[mask]

            for idx, row in df.iterrows():
                timestamp = self._row_to_timestamp(idx, row)
                bar = Bar(
                    symbol=symbol,
                    open=float(row.get('开盘价', 0)),
                    high=float(row.get('最高价', 0)),
                    low=float(row.get('最低价', 0)),
                    close=float(row.get('收盘价', 0)),
                    volume=float(row.get('成交量', 0)),
                    timestamp=timestamp,
                )
                all_bars.append(bar)

        # 按时间排序（同一时间按symbol排序，保证确定性）
        all_bars.sort(key=lambda b: (b.timestamp, b.symbol))
        logger.info(f"统一时间序列构建完成: {len(all_bars)} 根K线")
        return all_bars

    @staticmethod
    def _row_to_timestamp(idx: Any, row: pd.Series) -> float:
        """将DataFrame的index转换为时间戳"""
        import pandas as pd
        if isinstance(idx, pd.Timestamp):
            return idx.timestamp()
        if hasattr(idx, 'timestamp'):
            return idx.timestamp()
        # 尝试从row中获取timestamp
        if 'timestamp' in row:
            return float(row['timestamp'])
        # 尝试解析字符串日期
        try:
            return pd.Timestamp(str(idx)).timestamp()
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # 快捷方法
    # ------------------------------------------------------------------

    def get_result(self) -> Optional[BacktestResult]:
        """获取回测结果"""
        return self.result

    def stop(self) -> None:
        """停止回测"""
        self._running = False
        super().stop()

    def __repr__(self) -> str:
        return (
            f"BacktestEngine(strategy={self.strategy.name if self.strategy else 'None'}, "
            f"symbols={len(self.data)}, "
            f"events={self._event_count}, "
            f"status={'running' if self._running else 'stopped' if self._stopped else 'initialized'})"
        )



# ═══════════════════════════════════════════════
#  三模式调度器（第6项：engine.py 三模式调度器）
# ═══════════════════════════════════════════════

ENGINE_MODE_AUTO: str = "auto"
ENGINE_MODE_EVENT: str = "event"
ENGINE_MODE_MIXED: str = "mixed"
ENGINE_MODE_VECTORIZED: str = "vectorized"
SUPPORTED_ENGINE_MODES: List[str] = [
    ENGINE_MODE_AUTO,
    ENGINE_MODE_EVENT,
    ENGINE_MODE_MIXED,
    ENGINE_MODE_VECTORIZED,
]
# auto 优先级：mixed（生产主力，资金约束）> vectorized（参数扫描）> event（真值基准）
_ENGINE_PRIORITY: List[str] = [
    ENGINE_MODE_MIXED,
    ENGINE_MODE_VECTORIZED,
    ENGINE_MODE_EVENT,
]


class BacktestScheduler:
    """三模式回测调度器。

    根据策略能力（supports_mode / engine_modes）自动选择引擎:
      - mixed      : 策略实现 generate_mixed_loop（生产主力，bar 循环，资金约束）
      - vectorized : 策略实现 generate_signals_vectorized（纯向量化，参数扫描）
      - event      : 其他（真值基准，on_bar 通用）

    用法:
        scheduler = BacktestScheduler(mode="auto")
        result = scheduler.run(config, strategy, data=data_dict)

    或便捷函数:
        result = run_backtest(config, strategy, data=data_dict, mode="auto")
    """

    def __init__(self, mode: str = ENGINE_MODE_AUTO, name: str = "backtest_scheduler"):
        """初始化调度器。

        Args:
            mode: 引擎模式（auto/event/mixed/vectorized）。
            name: 调度器名称。

        Raises:
            ValueError: mode 非法时抛出。
        """
        if mode not in SUPPORTED_ENGINE_MODES:
            raise ValueError(
                f"不支持的引擎模式: {mode!r}（支持: {SUPPORTED_ENGINE_MODES}）"
            )
        self.mode: str = mode
        self.name: str = name
        self.selected_mode: Optional[str] = None
        self.engine: Any = None

    @staticmethod
    def detect_modes(strategy: Optional[StrategyBase]) -> List[str]:
        """探测策略支持的引擎模式列表。

        Args:
            strategy: 策略实例。

        Returns:
            支持的模式名列表（按推荐优先级降序）。

        Raises:
            TypeError: strategy 类型不合法时抛出。
        """
        if strategy is None:
            return []
        if not isinstance(strategy, StrategyBase):
            raise TypeError(
                f"strategy 必须为 StrategyBase 实例，收到: {type(strategy).__name__}"
            )
        return strategy.engine_modes()

    def resolve_mode(self, strategy: Optional[StrategyBase]) -> str:
        """解析最终引擎模式（auto 时按优先级选择）。

        Args:
            strategy: 策略实例。

        Returns:
            选定的引擎模式名。

        Raises:
            ValueError: 策略不支持任何引擎模式时抛出。
        """
        if self.mode != ENGINE_MODE_AUTO:
            return self.mode
        modes = set(self.detect_modes(strategy))
        if not modes:
            raise ValueError("策略不支持任何引擎模式，无法调度")
        for m in _ENGINE_PRIORITY:
            if m in modes:
                return m
        raise ValueError(f"策略支持的引擎模式无法映射: {sorted(modes)}")

    def create_engine(
        self,
        config: BacktestConfig,
        strategy: Optional[StrategyBase],
        data: Optional[Dict[str, pd.DataFrame]] = None,
        data_provider: Optional[DataProviderBase] = None,
        **kwargs: Any,
    ) -> Any:
        """按解析出的模式创建对应引擎实例。

        Args:
            config: 回测配置。
            strategy: 策略实例。
            data: 行情数据（可选）。
            data_provider: 数据提供器（可选）。
            **kwargs: 传递给目标引擎的额外参数（如 mixed 的 stop_loss_pct）。

        Returns:
            目标引擎实例。

        Raises:
            NotImplementedError: 显式模式但策略不支持时抛出。
        """
        mode = self.resolve_mode(strategy)
        if strategy is not None and not strategy.supports_mode(mode):
            raise NotImplementedError(
                f"策略 {getattr(strategy, 'name', '?')} 不支持 {mode} 引擎"
                f"（支持: {strategy.engine_modes()}）。请显式选择匹配的引擎模式。"
            )
        engine_name = kwargs.pop("name", f"{mode}_backtest")
        # #H2修复：mixed/vectorized 引擎统一接入全局防未来函数校验器（单例）
        kwargs.setdefault("lookahead_guard", get_lookahead_guard())
        kwargs.setdefault("enable_lookahead_guard", True)
        if mode == ENGINE_MODE_VECTORIZED:
            engine: Any = VectorizedBacktestEngine(
                config=config, strategy=strategy, data=data,
                data_provider=data_provider, name=engine_name, **kwargs,
            )
        elif mode == ENGINE_MODE_MIXED:
            engine = MixedBacktestEngine(
                config=config, strategy=strategy, data=data,
                data_provider=data_provider, name=engine_name, **kwargs,
            )
        else:
            engine = BacktestEngine(
                config=config, strategy=strategy, data=data,
                data_provider=data_provider, name=engine_name, **kwargs,
            )
        self.engine = engine
        self.selected_mode = mode
        logger.info(
            "调度器选中引擎: mode=%s, engine=%s", mode, type(engine).__name__
        )
        return engine

    def run(
        self,
        config: BacktestConfig,
        strategy: Optional[StrategyBase],
        data: Optional[Dict[str, pd.DataFrame]] = None,
        data_provider: Optional[DataProviderBase] = None,
        **kwargs: Any,
    ) -> BacktestResult:
        """运行回测（自动选择并执行对应引擎）。

        Args:
            config: 回测配置。
            strategy: 策略实例。
            data: 行情数据（可选）。
            data_provider: 数据提供器（可选）。
            **kwargs: 传递给目标引擎的额外参数。

        Returns:
            BacktestResult 回测结果。
        """
        engine = self.create_engine(config, strategy, data, data_provider, **kwargs)
        result = engine.run()
        if hasattr(result, "extra") and isinstance(result.extra, dict):
            result.extra["engine_mode"] = self.selected_mode
            result.extra["scheduler_mode"] = self.mode
        return result

    def __repr__(self) -> str:
        engine_type = type(self.engine).__name__ if self.engine is not None else "None"
        return (
            f"BacktestScheduler(mode={self.mode}, selected={self.selected_mode}, "
            f"engine={engine_type})"
        )


def run_backtest(
    config: BacktestConfig,
    strategy: Optional[StrategyBase],
    data: Optional[Dict[str, pd.DataFrame]] = None,
    data_provider: Optional[DataProviderBase] = None,
    mode: str = ENGINE_MODE_AUTO,
    **kwargs: Any,
) -> BacktestResult:
    """三模式回测便捷入口（自动选择引擎）。

    参数:
        config: 回测配置
        strategy: 策略实例
        data: 行情数据（可选）
        data_provider: 数据提供器（可选）
        mode: 引擎模式（auto/event/mixed/vectorized，默认 auto）
        **kwargs: 传递给目标引擎的额外参数

    返回:
        BacktestResult 回测结果
    """
    scheduler = BacktestScheduler(mode=mode)
    return scheduler.run(
        config=config,
        strategy=strategy,
        data=data,
        data_provider=data_provider,
        **kwargs,
    )


def _run_self_test() -> int:
    """运行 engine.py 三模式调度器自测。

    返回:
        通过的测试项数量。
    """
    import numpy as np

    from myquant.strategies.base import Signal, SignalType, StrategyBase, StrategyContext

    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}: {detail}")

    print("=" * 60)
    print("engine.py 三模式调度器自测开始")
    print("=" * 60)

    # 合成行情
    n = 40
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = 10.0 * 1.02 ** np.arange(n)
    df = pd.DataFrame(
        {"开盘价": close * 0.995, "最高价": close * 1.02, "最低价": close * 0.98,
         "收盘价": close, "成交量": np.full(n, 1e6)},
        index=idx,
    )
    data = {"600519.SH": df}
    config = BacktestConfig(
        symbols=["600519.SH"], start_date="2024-01-01",
        end_date="2024-02-28", initial_capital=1_000_000.0,
    )

    # 测试策略：仅 event / event+mixed / event+mixed+vectorized
    class _EventOnlyStrategy(StrategyBase):
        name = "event_only"

        def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

    class _MixedCapableStrategy(StrategyBase):
        name = "mixed_capable"

        def generate_mixed_loop(self, bar: Bar, context: StrategyContext,
                                position: Optional[Any]) -> float:
            return 1000.0

        def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

    class _VectorCapableStrategy(StrategyBase):
        name = "vector_capable"

        def generate_mixed_loop(self, bar: Bar, context: StrategyContext,
                                position: Optional[Any]) -> float:
            return 1000.0

        def generate_signals_vectorized(self, data_df: pd.DataFrame) -> np.ndarray:
            return np.zeros(len(data_df)) + 1000.0

        def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

    # 1. 能力探测
    eo = _EventOnlyStrategy()
    mc = _MixedCapableStrategy()
    vc = _VectorCapableStrategy()
    check("event-only 策略 modes == [event]", BacktestScheduler.detect_modes(eo) == ["event"], f"{BacktestScheduler.detect_modes(eo)}")
    check("mixed 策略 modes 含 mixed", "mixed" in BacktestScheduler.detect_modes(mc), f"{BacktestScheduler.detect_modes(mc)}")
    check("vector 策略 modes 含 vectorized", "vectorized" in BacktestScheduler.detect_modes(vc), f"{BacktestScheduler.detect_modes(vc)}")

    # 2. auto 选择
    s1 = BacktestScheduler(mode="auto")
    check("auto 选 event", s1.resolve_mode(eo) == "event", f"{s1.resolve_mode(eo)}")
    s2 = BacktestScheduler(mode="auto")
    check("auto 选 mixed", s2.resolve_mode(mc) == "mixed", f"{s2.resolve_mode(mc)}")
    s3 = BacktestScheduler(mode="auto")
    check("auto 选 vectorized", s3.resolve_mode(vc) == "vectorized", f"{s3.resolve_mode(vc)}")

    # 3. 全流程运行（三种模式都跑通）
    r1 = run_backtest(config, eo, data=data, mode="auto")
    check("event 模式回测成功", r1 is not None and r1.is_success and r1.extra.get("engine_mode") == "event", f"status={r1.status}, mode={r1.extra.get('engine_mode')}")
    r2 = run_backtest(config, mc, data=data, mode="auto")
    check("mixed 模式回测成功", r2 is not None and r2.is_success and r2.extra.get("engine_mode") == "mixed", f"status={r2.status}, mode={r2.extra.get('engine_mode')}")
    r3 = run_backtest(config, vc, data=data, mode="auto")
    check("vectorized 模式回测成功", r3 is not None and r3.is_success and r3.extra.get("engine_mode") == "vectorized", f"status={r3.status}, mode={r3.extra.get('engine_mode')}")

    # 4. 显式模式
    r4 = run_backtest(config, vc, data=data, mode="vectorized")
    check("显式 vectorized 模式成功", r4 is not None and r4.is_success, f"status={r4.status}")

    # 5. 显式模式不匹配 → 明确异常
    try:
        run_backtest(config, eo, data=data, mode="vectorized")
        check("显式 vectorized + event-only 策略抛异常", False, "未抛出")
    except NotImplementedError as e:
        check("显式 vectorized + event-only 策略抛异常", True)

    # 6. 非法 mode
    try:
        BacktestScheduler(mode="bad")
        check("非法 mode 抛 ValueError", False, "未抛出")
    except ValueError:
        check("非法 mode 抛 ValueError", True)

    print("=" * 60)
    if failures:
        print(f"自测结果: {passed} 项通过, {len(failures)} 项失败")
        for f in failures:
            print(f"  - {f}")
        return len(failures)
    print(f"自测结果: 全部 {passed} 项通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    # 自测 Demo：运行全部校验项
    raise SystemExit(_run_self_test())
