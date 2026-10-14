# 文件名：myquant/backtest/vectorized_engine.py
"""
backtest/vectorized_engine.py - 纯向量化回测引擎
==================================================
面向大批量参数扫描的高性能回测引擎，全程 numpy 数组运算，无逐 bar Python 循环。

定位（与 mixed / event 的差异）:
  - vectorized : 纯向量化，最快，面向大批量参数扫描（本模块）
  - mixed      : 向量化因子 + bar 循环处理路径依赖/止损/条件单（生产主力，单独模块）
  - event      : 逐事件引擎，保留作为真值基准（engine.py）

核心交易模型（严格 T+1，防未来函数）:
  1. 策略 generate_signals_vectorized(df) 在 T 日收盘输出目标持仓股数 target[T]；
  2. 引擎将持仓置为前一日目标：position[T] = target[T-1]（T 日开盘按 T-1 信号成交）；
  3. 成交数量 exec[T] = position[T] - position[T-1]（>0 买入 / <0 卖出）；
  4. 成交价 = T 日开盘价（市价），撮合统一走 order_matcher 向量化接口；
  5. 费用统一走 fee_common 向量化接口（佣金/印花税/过户费/滑点）；
  6. 资金/持仓市值/净值全程 numpy 递推，每根 bar 一个净值点。

资金模型:
  - capital_mode="per_symbol"（默认）: 每标的独立子账户，初始资金按标的数均分，
    无跨标的资金耦合，可完全向量化；组合净值 = 各子账户资产按日期求和 / 总初始资金。
  - 负现金（策略目标超资金）视为杠杆，触发告警但保持计算（文档注明：策略须自行
    保证 target 在可用资金内，或用固定仓位比例）。

防未来函数（规则4）:
  - target[T] 只由策略基于截至 T 日收盘的数据计算（策略职责）；
  - 引擎成交只使用 T 日开盘价（T 日信号 T+1 日开盘成交）；
  - 净值用 T 日收盘价计算市值；不使用任何未来数据。

依赖:
  标准库: dataclasses, logging, typing
  第三方: numpy, pandas
  本地  : myquant.models.backtest (BacktestConfig, BacktestResult, EquityPoint, PerformanceMetrics, TradeRecord)
          myquant.models.trade (OrderSide, OrderType)
          myquant.strategies.base (StrategyBase, StrategyContext)
          myquant.backtest.fee_common (FeeCalculator)
          myquant.backtest.order_matcher (OrderMatcher)
          myquant.backtest.metrics (MetricsCalculator)
          myquant.backtest.data_loader (DataProviderBase)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from myquant.backtest.data_loader import EN_TO_CN_COLUMN_MAP, DataProviderBase
from myquant.backtest.fee_common import FeeCalculator
from myquant.backtest.metrics import MetricsCalculator
from myquant.backtest.order_matcher import OrderMatcher
from myquant.core.lookahead_guard import LookaheadGuard  # #H2修复：防未来函数校验器
from myquant.models.backtest import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    TradeRecord,
)
from myquant.models.trade import OrderSide, OrderType
from myquant.strategies.base import StrategyBase, StrategyContext

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# A股一手 = 100 股
DEFAULT_LOT_SIZE: int = 100

# 支持的资金模式
SUPPORTED_CAPITAL_MODES: Tuple[str, ...] = ("per_symbol",)


# ─────────────────────────────────────────────
# SymbolResult - 单标的向量化回测结果
# ─────────────────────────────────────────────

@dataclass
class SymbolResult:
    """单标的向量化回测中间结果。

    Attributes:
        symbol        : 标的代码。
        dates         : 日期字符串列表（长度 n）。
        timestamps    : 时间戳列表（长度 n）。
        cash          : 现金数组（长度 n，绝对金额）。
        position      : 持仓股数数组（长度 n）。
        position_value: 持仓市值数组（长度 n）。
        total_asset   : 总资产数组（长度 n，cash + position_value）。
        trades        : 成交记录列表（含 FIFO 配对盈亏）。
        total_commission: 该标的总手续费。
        total_slippage  : 该标的总滑点金额。
    """

    symbol: str
    dates: List[str]
    timestamps: List[float]
    cash: np.ndarray
    position: np.ndarray
    position_value: np.ndarray
    total_asset: np.ndarray
    trades: List[TradeRecord]
    total_commission: float = 0.0
    total_slippage: float = 0.0

    def __repr__(self) -> str:
        return (
            f"SymbolResult({self.symbol} n={len(self.dates)} "
            f"trades={len(self.trades)} 期末资产={self.total_asset[-1]:,.0f})"
        )


# ─────────────────────────────────────────────
# VectorizedBacktestEngine - 纯向量化回测引擎
# ─────────────────────────────────────────────

class VectorizedBacktestEngine:
    """纯向量化回测引擎。

    用法:
        engine = VectorizedBacktestEngine(config=config, strategy=strategy, data=data)
        result = engine.run()   # BacktestResult

    或从数据提供器加载（为实盘无缝对接）:
        engine = VectorizedBacktestEngine.from_data_provider(provider, config, strategy)
    """

    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        strategy: Optional[StrategyBase] = None,
        data: Optional[Dict[str, pd.DataFrame]] = None,
        data_provider: Optional[DataProviderBase] = None,
        fee_calculator: Optional[FeeCalculator] = None,
        matcher: Optional[OrderMatcher] = None,
        lot_size: int = DEFAULT_LOT_SIZE,
        capital_mode: str = "per_symbol",
        name: str = "vectorized_backtest",
        lookahead_guard: Optional[LookaheadGuard] = None,
        enable_lookahead_guard: bool = True,
    ):
        """初始化纯向量化回测引擎。

        Args:
            config: 回测配置。
            strategy: 策略实例（须实现 generate_signals_vectorized）。
            data: 历史行情数据 {symbol: DataFrame}（方式1，向后兼容）。
            data_provider: 数据提供器（方式2，on_init 自动加载）。
            fee_calculator: 费用计算器（None 时按 config 创建，走统一 fee_common）。
            matcher: 统一撮合器（None 时创建默认实例）。
            lot_size: A股一手股数（默认 100）。
            capital_mode: 资金模式，仅支持 "per_symbol"。
            name: 引擎名称。

        Raises:
            ValueError: capital_mode 非法时抛出。
        """
        if capital_mode not in SUPPORTED_CAPITAL_MODES:
            raise ValueError(
                f"不支持的 capital_mode: {capital_mode!r}（支持: {SUPPORTED_CAPITAL_MODES}）"
            )
        if lot_size <= 0:
            raise ValueError(f"lot_size 必须 > 0: {lot_size!r}")
        self.config: BacktestConfig = config or BacktestConfig()
        self.strategy: Optional[StrategyBase] = strategy
        self.data: Dict[str, pd.DataFrame] = data or {}
        self.data_provider: Optional[DataProviderBase] = data_provider
        self.lot_size: int = int(lot_size)
        self.capital_mode: str = capital_mode
        self.name: str = name
        # 统计：因子账户预算不足一手而信号全部落空的标的数（run 末尾汇总告警）
        self._no_trade_symbols: int = 0
        self._cash_constraint_caps: int = 0  # KP-P2-03: 可用现金不足被缩 notional 的日次

        # 核心组件
        self.fee_calculator: FeeCalculator = fee_calculator or FeeCalculator(
            commission_model=self.config.commission,
            slippage_model=self.config.slippage,
        )
        # P0-03 卡本体收尾（#347）：OrderMatcher 装配传真实幅度（此前无参构造 →
        # 涨跌停幅度写死 10%）；撮合层涨跌停校验恒启用（KP-P2-10 A股写实契约）；
        # enforce_limit（YAML 开关）控制 event 路径 SimBroker。向量化批量路径无
        # symbol 数组，用全局幅度（config 真实值）；板块映射在标量路径生效。
        self.matcher: OrderMatcher = matcher or OrderMatcher(
            price_limit_pct=float(getattr(self.config, "price_limit_pct", 0.10)),
            enable_price_limit=True,
            lot_size=self.lot_size,
        )
        self.metrics_calc: MetricsCalculator = MetricsCalculator(
            risk_free_rate=self.config.risk_free_rate
        )

        # 回测结果与中间状态
        self.result: Optional[BacktestResult] = None
        self._symbol_results: List[SymbolResult] = []
        self._initialized = False
        # 策略上下文（on_init 创建；__init__ 先占位 None，与 event/mixed 引擎对齐，
        # 供 _refresh_causality_manifest 等 run 前路径防御访问）
        self.context: Optional[StrategyContext] = None

        # #H2修复：防未来函数校验器（默认开启，规则4强制）
        self._lookahead_guard: Optional[LookaheadGuard] = (
            lookahead_guard or (LookaheadGuard() if enable_lookahead_guard else None)
        )
        # P1-03：轻量防未来防线（第二道）时间线缓存，供收尾时钟单调校验
        self._bar_timestamps: List[float] = []

        logger.info(
            "向量化引擎初始化: 标的数=%d, lot=%d, 资金模式=%s, 初始资金=%.0f",
            len(self.data),
            self.lot_size,
            self.capital_mode,
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
        name: str = "vectorized_backtest",
    ) -> "VectorizedBacktestEngine":
        """从数据提供器创建向量化引擎（为实盘无缝对接）。

        Args:
            data_provider: 数据提供器。
            config: 回测配置（symbols/start_date/end_date 用于加载）。
            strategy: 策略实例。
            name: 引擎名称。

        Returns:
            VectorizedBacktestEngine 实例。
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
        """初始化引擎：加载数据、校验策略、初始化策略上下文。

        Args:
            config: 可选扩展配置（兼容 BaseEngine 生命周期）。
        """
        logger.info("=" * 60)
        logger.info("向量化回测引擎初始化开始")
        logger.info("=" * 60)

        if not self.data and self.data_provider is not None:
            self._load_data_from_provider()

        if self.strategy is None:
            raise ValueError("vectorized 引擎必须提供 strategy（须实现 generate_signals_vectorized）")
        if not self.strategy.supports_mode("vectorized"):
            raise NotImplementedError(
                f"策略 {getattr(self.strategy, 'name', '?')} 未实现 "
                "generate_signals_vectorized，无法用于 vectorized 引擎。"
                "请在策略中覆盖该接口（基于全量 DataFrame 输出目标持仓股数数组）。"
            )

        # 创建策略上下文（与 event 引擎一致的接口）
        context = StrategyContext(
            data=self.data,
            config=self.config.to_dict(),
            extra={
                "engine_mode": "vectorized",
                # P1-07 子项①：统一注入 lookahead_guard（与 event 引擎同构）
                **({"lookahead_guard": self._lookahead_guard}
                   if self._lookahead_guard is not None else {}),
            },
            # ── P0-08 子项②：全量声明改引擎构造参数（vectorized 逃生口）──
            # 目标持仓生成天然需要全量数据，走 full_sample_declared=True
            # （写入 manifest causality.full_sample_declared，在报告页披露；
            # 策略运行期不可自设该属性）
            _full_sample_declared=True,
        )
        self.strategy.on_init(context)

        self._initialized = True
        logger.info(
            "向量化引擎初始化完成: 数据标的=%d, 策略=%s",
            len(self.data),
            self.strategy.name,
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
    # 主入口：run
    # ─────────────────────────────────────────

    def run(self) -> BacktestResult:
        """运行向量化回测主流程。

        Returns:
            BacktestResult: 完整回测结果（净值曲线/成交记录/绩效指标）。
        """
        # ── P0-01 两阶段握手第⑤步（v2.2 §4.4）：run 入口断言 manifest 已封印 ──
        # create_engine 已 assert_realism_pass + seal；manifest 缺失（旁路直构，
        # P1-04 收敛前）跳过；已建未封印 → enabled=True 时阻断（禁止绕过出口断言直跑）。
        _rg_m = getattr(self, "_realism_manifest", None)
        if _rg_m is not None and not _rg_m.is_sealed():
            from myquant.realism_gate.gate import RealismGateError, get_config
            if get_config().enabled:
                raise RealismGateError(
                    "真实性门禁两阶段握手第⑤步失败：引擎未封印（manifest 未经 "
                    "create_engine 出口断言），禁止直接 run",
                    violations=[],
                    manifest=_rg_m,
                )

        if not self._initialized:
            self.on_init()

        if not self.data:
            logger.error("无历史数据，无法运行向量化回测")
            result = BacktestResult(config=self.config)
            result.mark_failed("无历史数据")
            return result

        self.result = BacktestResult(config=self.config)
        self.result.status = "running"
        self._no_trade_symbols = 0
        self._cash_constraint_caps = 0

        # #H2修复：防未来函数校验器重置（每次回测独立校验）
        if self._lookahead_guard:
            self._lookahead_guard.reset()

        # 资金均分（按有效候选标的数，先于回测确定子账户初始资金）
        valid_candidates = [
            s for s, df in self.data.items() if df is not None and len(df) > 0
        ]
        if not valid_candidates:
            self.result.mark_failed("无有效数据标的")
            return self.result
        n = len(valid_candidates)
        sub_initial = self.config.initial_capital / n

        # 逐标的向量化回测
        self._symbol_results = []
        valid_symbols: List[str] = []
        for symbol, df in self.data.items():
            if df is None or len(df) == 0:
                logger.warning("跳过无数据标的: %s", symbol)
                continue
            try:
                sym_result = self._run_symbol(symbol, df, sub_initial)
                self._symbol_results.append(sym_result)
                valid_symbols.append(symbol)
            except NotImplementedError:
                # 策略编程错误（未实现向量化接口）直接暴露，不静默跳过
                raise
            except Exception as exc:  # 单标的数据/运行时异常隔离，不中断整体回测
                logger.error("标的 %s 向量化回测失败: %s", symbol, exc)
                continue

        if not self._symbol_results:
            self.result.mark_failed("全部标的回测失败或无有效数据")
            return self.result

        # 组合聚合 → 净值曲线
        equity_curve, trades = self._aggregate_portfolio(sub_initial)

        # 绩效计算
        # P2-03：向量化路径每 symbol 独立回测再聚合，未加载基准指数数据（benchmark_equity
        # 恒 1.0 占位）。此处保持 None，让 metrics 把 alpha/beta/excess 置 None——
        # 绝不把恒 1.0 的占位曲线当真基准传入，否则会算出虚假的零基准超额收益。
        metrics = self.metrics_calc.calculate(
            equity_curve=equity_curve,
            trades=trades,
            benchmark_curve=None,
            start_date=self.config.start_date,
            end_date=self.config.end_date,
        )

        self.result.metrics = metrics
        self.result.equity_curve = equity_curve
        self.result.trades = trades
        self.result.extra["symbols"] = valid_symbols
        self.result.extra["capital_mode"] = self.capital_mode
        self.result.extra["per_symbol_initial"] = sub_initial
        # #H2修复：回测结束统一校验防未来函数（P0-01：通过才标记 completed；失败已 mark_failed）
        # P1-15：结果侧真实性不变量校验（RI-4 现金非负 / RI-6 持仓非负）
        if self._run_lookahead_assert() and self._run_realism_invariants():
            self.result.mark_complete()

        # KP-P2-03：可用现金不足被缩 notional 的日次写入结果
        if self._cash_constraint_caps > 0:
            self.result.extra["cash_constraint_caps"] = self._cash_constraint_caps
            logger.warning(
                "全量合计 %d 日次因可用现金不足缩 notional（KP-P2-03）", self._cash_constraint_caps
            )

        # 汇总告警：子账户预算不足导致信号落空的标的数（超大股票池提示换 mixed）
        if self._no_trade_symbols > 0:
            logger.warning(
                "有 %d 只标的目标信号因子账户预算不足一手而全部落空（超大股票池建议改用 mixed 引擎）",
                self._no_trade_symbols,
            )

        # P0-08 子项②：run 结束后按实际置位刷新 causality 组（真实采集）
        self._refresh_causality_manifest()
        # P0-01 端到端（#345）：清单快照挂载到 result（随 results.json 落盘）
        # getattr 防御：直构引擎（不经 create_engine）无 _realism_manifest
        _rg_m = getattr(self, "_realism_manifest", None)
        if _rg_m is not None:
            self.result.realism_manifest = _rg_m.to_dict()
        logger.info(
            "向量化回测完成: %d 标的, %d 笔成交, 总收益=%.2f%%, 夏普=%.2f",
            n,
            len(trades),
            metrics.total_return,
            metrics.sharpe_ratio,
        )
        return self.result

    def _refresh_causality_manifest(self) -> None:
        """P0-08 子项②：run 结束后按实际置位刷新 causality 组（止血 A 真实采集）。

        替代 create_engine 处的 run 前静态反推：event/mixed 逐 bar 置位后
        ``_slicing_enabled`` 为 True、``current_bar_ts`` 已推进 → 报真实 True；
        vectorized 全量逃生口从不置位 → 报 False（由 ``full_sample_declared=True`` 声明）。
        软接线：enabled=False 时刷新/断言失败仅审计不阻断（P1-05 模式）。
        """
        _m = getattr(self, "_realism_manifest", None)
        if _m is None or self.context is None:
            return
        try:
            from myquant.realism_gate import assert_realism_pass
            from myquant.realism_gate.gate import get_config

            _m.update_group("causality", {
                "context_sliced": bool(self.context._slicing_enabled),
                "current_bar_ts_enforced": self.context.current_bar_ts is not None,
                "full_sample_declared": bool(self.context.full_sample_declared),
                "guard_injected": self.context.extra.get("lookahead_guard") is not None,
                # P0-08 子项⑧：vectorized 逃生口（full_sample_declared=True）value 未封，
                # 但策略不经 context.data 取数（generate_signals_vectorized 收 df 参数）——
                # 诚实 False；逃生口已由 full_sample_declared 声明（critical 字段）
                "context_data_scoped": False,
                # P0-01（#344）：运行期真实采集已完成 → causality 判据正式生效
                # （vectorized 逃生口 full_sample_declared=True，判据不误报）
                "assessed_at_run_end": True,
            })
            # ── P0-08 子项⑥：访问器层实测记账评估（P1-07② 判据）──
            # accessed_max_date > current_bar_date → 实测读到未来（真拦截）；
            # 静态声明已封门却实测读到未来 → 交叉校验矛盾（用例⑭）。
            # vectorized 从不置位 current_bar_ts（逃生口路径）→ 评估不判（无当前 bar）。
            _acct = self.context.collect_access_accounting()
            _m.update_group("causality", {
                "accessed_future": bool(_acct["accessed_future"]),
                "static_declared_but_accessed_future": bool(
                    _acct["static_declared_but_accessed_future"]
                ),
            })
            if _acct["violations"]:
                _m.set_field("causality.accounting_violations", _acct["violations"])
            # ── P0-08 子项③：run 后从真实实例刷新 t1/slippage（broker 在 on_init 装配，
            # create_engine 处仅有 config 构造事实；无 broker 的引擎保留 config 值）──
            _broker = getattr(self, "broker", None)
            if _broker is not None:
                _broker_t1 = getattr(_broker, "enable_t1", None)
                if _broker_t1 is not None:
                    _m.set_field("t1_position_freeze.enabled", bool(_broker_t1))
                _broker_slip = getattr(_broker, "slippage_model", None)
                if _broker_slip is not None:
                    _bs_type = str(getattr(_broker_slip, "type", "fixed_bps"))
                    _bs_value = float(getattr(_broker_slip, "fixed_bps", 0.0))
                    if _bs_type == "none":
                        _bs_value = 0.0  # type="none" 实际滑点恒 0
                    _m.update_group("slippage", {
                        "type": _bs_type,
                        "value": _bs_value,
                        "is_zero_cost": bool(_bs_type == "none" or _bs_value == 0.0),
                    })
            # P0-04（#349）：收尾回写真实复权降级集合（provider 意图：回测后由引擎
            # 读取写入 manifest；assert 前回写 → data_pit 判据真实生效——此前仅入口
            # 空集且无回写点，判据永不触发，真实降级被隐藏。与 engine.py 同构）。
            from myquant.data_access.provider import get_adjust_degraded_symbols
            _m.set_field("adjust_degraded_symbols", get_adjust_degraded_symbols())
            assert_realism_pass(_m)
        except Exception as exc:  # noqa: BLE001
            if get_config().enabled:
                # P0-01 端到端（#345）：阻断路径产物落盘（§4.3.1）——被门禁拦下
                # 时无 result 对象，独立落 outputs/realism_gate_blocked_*.json 后 re-raise；
                # 仅 RealismGateError 携带 violations（配置损坏类异常直接传播，不产阻断产物）
                _v = getattr(exc, "violations", None)
                if _v is not None:
                    from myquant.realism_gate.report import write_blocked_artifact
                    write_blocked_artifact("outputs", _m, _v)
                raise
            logger.debug("真实性门禁 run 后刷新失败（软接线不阻断）: %s", exc)

    # ─────────────────────────────────────────
    # 单标的向量化回测
    # ─────────────────────────────────────────

    def _run_symbol(self, symbol: str, df: pd.DataFrame, sub_initial: float) -> SymbolResult:
        """对单标的执行纯向量化回测。

        Args:
            symbol: 标的代码。
            df: 行情 DataFrame（open/high/low/close/volume，index 为日期）。
            sub_initial: 该标的子账户初始资金。

        Returns:
            SymbolResult 单标的回测结果。

        Raises:
            ValueError: 数据列缺失、策略输出长度不符时抛出。
            NotImplementedError: 策略未实现 generate_signals_vectorized 时抛出。
        """
        # 列名统一为中文：兼容英文列名的合成数据/数据库来源（内部全链路中文表头）
        _rename = {en: cn for en, cn in EN_TO_CN_COLUMN_MAP.items() if en in df.columns}
        if _rename:
            df = df.rename(columns=_rename)
        # 按日期升序排序并过滤（防未来函数双重保障）
        df = df.sort_index()
        if self.config.start_date:
            df = df[df.index.astype(str) >= self.config.start_date]
        if self.config.end_date:
            df = df[df.index.astype(str) <= self.config.end_date]
        if len(df) == 0:
            raise ValueError(f"标的 {symbol} 在回测区间内无数据")

        required = {"开盘价", "最高价", "最低价", "收盘价", "成交量"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"标的 {symbol} 缺少列: {sorted(missing)}")

        opens = df["开盘价"].to_numpy(dtype=np.float64)
        highs = df["最高价"].to_numpy(dtype=np.float64)
        lows = df["最低价"].to_numpy(dtype=np.float64)
        closes = df["收盘价"].to_numpy(dtype=np.float64)
        volumes = df["成交量"].to_numpy(dtype=np.float64)
        # 昨收价（用于撮合层涨跌停校验）：真实前收盘价列（P1-10 换源 data_col）。
        # 数据层 provider.py:513/672 与 mixed_engine.py:946 已兜底派生“前收盘价”列，
        # 引擎层不再静默用前一日收盘近似（shift_approx）；列缺失即 fail-closed 抛错，
        # 与 order_matcher“pre_close 缺失不可空转（数据层需派生）”哲学一致。
        if "前收盘价" not in df.columns:
            raise ValueError(
                f"标的 {symbol} 缺少真实前收盘价列“前收盘价”（P1-10 fail-closed）："
                f"数据层须经 provider/mixed 派生该列，引擎禁止静默用 shift 近似"
            )
        pre_closes = df["前收盘价"].to_numpy(dtype=np.float64)
        n = len(df)

        # 策略目标持仓（T 日收盘决定，长度须为 n）
        target = self.strategy.generate_signals_vectorized(df)  # type: ignore[union-attr]
        target_arr = np.asarray(target, dtype=np.float64)
        if target_arr.size != n:
            raise ValueError(
                f"策略 generate_signals_vectorized 返回长度 {target_arr.size} "
                f"与数据长度 {n} 不一致（symbol={symbol}）"
            )
        # ★ P1修复：子账户资金收缩（预算兜底，目标优先）
        #    策略目标（股数）在子账户预算内时保持原样（与 mixed 组合资金语义一致，
        #    三模式净值可对拍）；仅当目标市值超出子账户资金时按预算比例收缩，
        #    防止 per_symbol 模式下多标的叠加导致杠杆。
        #    价格估算用 T 日开盘价（成交参考价；本行仅依赖策略目标与资金模型，
        #    无未来行情时序，防未来函数）。
        if self.config.initial_capital > 0.0 and sub_initial < self.config.initial_capital:
            target_notional = np.abs(target_arr) * opens
            over_budget = target_notional > sub_initial
            if bool(np.any(over_budget)):
                scale = np.where(
                    over_budget,
                    sub_initial / np.maximum(target_notional, 1e-9),
                    1.0,
                )
                target_arr = target_arr * scale
        # 收缩后最大目标（用于判断一手取整后是否全部落空）
        raw_target_max = float(np.max(target_arr)) if n else 0.0
        # A股一手取整（本行仅依赖策略目标，无行情时序）
        target_arr = np.floor(target_arr / self.lot_size) * self.lot_size

        # 子账户预算不足一手 → 策略信号全部落空（向量化近似不适合超大股票池）
        if raw_target_max > 0.0 and float(np.max(target_arr)) <= 0.0:
            self._no_trade_symbols += 1
            logger.debug(
                "标的 %s 因子账户预算 %.2f 元不足一手，信号全部落空（建议改用 mixed 引擎）",
                symbol,
                sub_initial,
            )

        # T+1 订单：目标持仓每日递推，当日开盘执行的订单量 = 目标变动
        # P0-10修复：持仓不得直接由 target 递推（未成交时记幽灵持仓、净值虚高）；
        #   实际持仓在撮合后按 fill_qty 回写（见下方 signed_fill / act_after）。
        target_pos = np.concatenate([[0.0], target_arr[:-1]])
        exec_shares = np.diff(np.concatenate([[0.0], target_pos]))
        qty_abs = np.abs(exec_shares)

        # 统一撮合（市价单，T 日开盘成交；逻辑与 event 引擎一致）
        sides = np.where(exec_shares > 0, OrderSide.BUY.value, OrderSide.SELL.value)
        fill_result = self.matcher.match_vectorized(
            sides=sides,
            order_types=np.full(n, OrderType.MARKET.value),
            limit_prices=np.full(n, np.nan),
            stop_prices=np.full(n, np.nan),
            opens=opens,
            highs=highs,
            lows=lows,
            quantities=qty_abs,
            pre_closes=pre_closes,
            volumes=volumes,
        )
        fill_price = fill_result.fill_prices
        fill_qty = fill_result.fill_quantities
        traded = fill_qty > 0
        # P0-10修复：按实际成交量回写持仓。原 position 全程由 target 递推，
        # 而现金流/费用只对 traded 生效——停牌(volume=0)/涨停拦截导致买单未成交时，
        # cashflow=0（未支出）但 position 仍记为已持有 → 幽灵持仓、净值虚高。
        # 正确口径：position[t] = 当日开盘实际净成交累加（signed_fill），
        # 全部成交时 signed_fill=exec_shares，cumsum 与原 target 递推逐点一致。
        signed_fill = np.where(exec_shares > 0, fill_qty, -fill_qty)
        position = np.cumsum(signed_fill)
        # 未成交的价格用 0 替代，避免 0*NaN=NaN 向下游传播（停牌/涨跌停未成交时 fill_price=NaN）
        fill_price_safe = np.where(traded, fill_price, 0.0)

        # 统一费用（向量化：佣金/印花税/过户费/滑点）
        notional = np.where(traded, fill_qty * fill_price_safe, 0.0)
        fees = self.fee_calculator.calc_fee_vectorized(
            side=sides,
            notional=notional,
            price=fill_price_safe,
            quantity=fill_qty,
            volume=volumes,
        )

        # 现金流与资金（sign=-1 买入支付 / +1 卖出收入）
        sign = np.where(exec_shares > 0, -1.0, 1.0)
        cashflow = np.zeros(n, dtype=np.float64)
        cashflow[traded] = sign[traded] * notional[traded] - fees.total_cost[traded]

        # 子账户现金 = 初始资金 + 累计现金流（T+1 成交后逐日演化）
        cash = sub_initial + np.cumsum(cashflow)
        position_value = position * closes
        total_asset = cash + position_value

        # 资金约束检查（KP-P2-03：负现金不再仅告警，按可用现金比例缩 notional，超限计数写入结果）
        min_cash = float(np.min(cash)) if n else 0.0
        cash_constraint_caps = 0
        if n and min_cash < -1.0:
            deficit = np.where(cash < 0.0)[0]
            cash_constraint_caps = int(len(deficit))
            for i in deficit:
                if int(exec_shares[i]) <= 0 or not bool(traded[i]):
                    continue
                need = float(notional[i]) + float(fees.total_cost[i])
                have = float(cash[i - 1]) if i > 0 else sub_initial
                if need <= 0.0:
                    continue
                scale = max(0.0, min(1.0, have / need))
                if scale >= 1.0:
                    continue
                capped = float(fill_qty[i]) * scale
                fill_qty[i] = capped
                exec_shares[i] = float(exec_shares[i]) * scale
                signed_fill[i] = capped * (1.0 if exec_shares[i] > 0 else -1.0)
                notional[i] = capped * float(fill_price_safe[i])
                # P1-10 缺陷②：notional 已按 scale 缩量，但 fees 仍是缩量前按全额量算出的费用
                # （佣金/印花税/滑点/过户费全部虚高），导致当日现金支出被高估、净值被压低。
                # 按缩量后的 quantity/notional/price 重算当日费用并原地回写 fees 各分量；
                # total_cost 是 commission+stamp_tax+transfer_fee+slippage 的派生 property，自动重算。
                refees = self.fee_calculator.calc_fee_vectorized(
                    side=np.array([sides[i]]),
                    notional=np.array([notional[i]]),
                    price=np.array([fill_price_safe[i]]),
                    quantity=np.array([fill_qty[i]]),
                    volume=np.array([volumes[i]]),
                )
                fees.commission[i] = float(refees.commission[0])
                fees.stamp_tax[i] = float(refees.stamp_tax[0])
                fees.transfer_fee[i] = float(refees.transfer_fee[0])
                fees.slippage[i] = float(refees.slippage[0])
                cashflow[i] = -notional[i] - float(fees.total_cost[i])
                position[i:] = np.cumsum(signed_fill[i:])
                cash[i:] = sub_initial + np.cumsum(cashflow[i:])
        self._cash_constraint_caps += cash_constraint_caps
        if cash_constraint_caps > 0:
            logger.warning(
                "标的 %s 出现负现金 %.2f，已按可用现金缩 notional %d 日（KP-P2-03）", symbol, min_cash, cash_constraint_caps
            )

        # 日期与时间戳
        dates = [str(idx)[:10] for idx in df.index]
        timestamps = [self._idx_to_timestamp(idx) for idx in df.index]
        # P1-03：缓存时间线供轻量防线收尾校验
        self._bar_timestamps = timestamps

        # #H2修复：记录信号/成交到防未来函数校验器（铁律3：position[t]=target[t-1]）
        #    成交发生在 T 日开盘，信号在 T-1 日收盘 → trade_date > signal_date
        if self._lookahead_guard:
            for i in range(n):
                if float(fill_qty[i]) > 0:
                    side = "BUY" if exec_shares[i] > 0 else "SELL"
                    sig_i = i - 1
                    if sig_i >= 0:
                        self._lookahead_guard.record_signal(
                            symbol=symbol,
                            signal_date=dates[sig_i],
                            signal_type=side,
                            data_end=dates[sig_i],
                            factor_names=[self.strategy.name] if self.strategy else None,
                        )
                    self._lookahead_guard.record_trade(
                        symbol=symbol,
                        trade_date=dates[i],
                        signal_date=dates[sig_i] if sig_i >= 0 else "",
                        price_type="OPEN",
                        side=side,
                    )

        # 构建成交记录（FIFO 配对盈亏）
        trades = self._build_trades(
            symbol=symbol,
            dates=dates,
            timestamps=timestamps,
            exec_shares=exec_shares,
            fill_qty=fill_qty,
            fill_price=fill_price,
            fees=fees,
        )

        return SymbolResult(
            symbol=symbol,
            dates=dates,
            timestamps=timestamps,
            cash=cash,
            position=position,
            position_value=position_value,
            total_asset=total_asset,
            trades=trades,
            total_commission=float(np.sum(fees.commission) + np.sum(fees.stamp_tax) + np.sum(fees.transfer_fee)),
            total_slippage=float(np.sum(fees.slippage)),
        )

    # ─────────────────────────────────────────
    # 成交记录构建（FIFO 配对盈亏）
    # ─────────────────────────────────────────

    def _build_trades(
        self,
        symbol: str,
        dates: List[str],
        timestamps: List[float],
        exec_shares: np.ndarray,
        fill_qty: np.ndarray,
        fill_price: np.ndarray,
        fees: Any,
    ) -> List[TradeRecord]:
        """按成交构建 TradeRecord 列表（FIFO 配对计算卖出盈亏）。

        本方法只在成交点上遍历（成交笔数 << bar 数），不构成逐 bar 循环。

        Args:
            symbol: 标的代码。
            dates: 日期字符串列表。
            timestamps: 时间戳列表。
            exec_shares: 成交股数（>0 买 / <0 卖）。
            fill_qty: 实际成交股数（绝对）。
            fill_price: 成交价。
            fees: VectorizedFeeResult。

        Returns:
            TradeRecord 列表。
        """
        trades: List[TradeRecord] = []
        cost_queue: List[List[float]] = []  # [剩余数量, 买入价, 买入时间戳]
        strategy_id = getattr(self.strategy, "name", "")

        for i in range(len(exec_shares)):
            qty = float(fill_qty[i])
            if qty <= 0:
                continue
            px = float(fill_price[i])
            # 总手续费（佣金+印花税+过户费），与 event 引擎 Trade.commission 口径一致
            comm = float(fees.commission[i] + fees.stamp_tax[i] + fees.transfer_fee[i])
            slip = float(fees.slippage[i])
            side = "BUY" if exec_shares[i] > 0 else "SELL"

            record = TradeRecord(
                symbol=symbol,
                side=side,
                quantity=qty,
                price=px,
                timestamp=timestamps[i],
                commission=comm,
                slippage=slip,
                strategy_id=strategy_id,
                entry_reason="vectorized",
            )

            if side == "BUY":
                cost_queue.append([qty, px, timestamps[i]])
            else:
                # FIFO 配对：用最先买入的成本计算盈亏
                remaining = qty
                total_cost = 0.0
                total_qty = 0.0
                earliest_ts = timestamps[i]
                while remaining > 1e-9 and cost_queue:
                    lot = cost_queue[0]
                    take = min(remaining, lot[0])
                    total_cost += take * lot[1]
                    total_qty += take
                    earliest_ts = min(earliest_ts, lot[2])
                    lot[0] -= take
                    remaining -= take
                    if lot[0] <= 1e-9:
                        cost_queue.pop(0)
                if total_qty > 0:
                    avg_cost = total_cost / total_qty
                    record.pnl = (px - avg_cost) * total_qty - comm
                    record.pnl_pct = (px - avg_cost) / avg_cost if avg_cost > 0 else 0.0
                    record.holding_days = max(1, int((timestamps[i] - earliest_ts) / 86400))
            trades.append(record)

        return trades

    # ─────────────────────────────────────────
    # #H2修复：防未来函数校验
    # ─────────────────────────────────────────

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
            # P1-03：第二道防线——轻量校验时间线单调推进（时钟倒退即违规）
            self._run_light_guard_check()
            return True
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

    def _run_realism_invariants(self) -> bool:
        """P1-15：结果侧真实性不变量校验（v2.2 §4.9）。

        遍历 per-symbol 逐 bar cash/position（RI-4/RI-6）+ 逐笔 trade 当日行情
        反查（RI-1 价界 / RI-2 量界 / RI-5 T+1）。RI-3 组合级 equity 恒等式需逐 bar
        close 明细，留后续 P1 卡。

        Returns:
            True 表示通过，可 mark_complete；
            False 表示违规，已 mark_failed，调用方不得再 mark_complete。
        """
        try:
            from myquant.realism_gate.invariants import (
                BarState,
                TradeRecord,
                check_result_invariants,
            )
        except ImportError:
            # realism_gate 未就绪，软接线放行（不阻断现有回测）
            logger.debug("realism_gate.invariants 未就绪，跳过 P1-15 校验")
            return True

        # 1. bar_states（RI-4 现金非负 / RI-6 持仓非负）
        bar_states: List[BarState] = []
        for sr in self._symbol_results:
            for i in range(len(sr.dates)):
                bar_states.append(BarState(
                    symbol=sr.symbol,
                    bar_date=sr.dates[i],
                    cash=float(sr.cash[i]),
                    position=float(sr.position[i]),
                    close=0.0,  # RI-3 暂不接线（无逐 bar close）
                ))

        # 2. trades 注入当日行情（RI-1 价界 / RI-2 量界 / RI-5 T+1）
        #    从 self.data[symbol] OHLCV DataFrame 按 trade_date 反查当日 low/high/volume
        inv_trades: List[TradeRecord] = []
        for t in self.result.trades:
            sym = t.symbol
            df = self.data.get(sym)
            if df is None:
                continue
            # timestamp(float) → 日期字符串（与 df.index 对齐）
            try:
                trade_date = pd.Timestamp(t.timestamp, unit="s").strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            # 在 df.index 反查当日行（查不到则跳过该 trade，防御式不报错）
            idx_str = df.index.astype(str)
            if trade_date not in idx_str:
                continue
            try:
                row = df.iloc[idx_str.get_loc(trade_date)]
                inv_trades.append(TradeRecord(
                    symbol=sym,
                    trade_date=trade_date,
                    side=t.side,
                    price=float(t.price),
                    quantity=float(t.quantity),
                    day_low=float(row["最低价"]),
                    day_high=float(row["最高价"]),
                    day_volume=float(row["成交量"]),
                    slippage_amount=abs(float(t.slippage)) * float(t.price),
                ))
            except (KeyError, ValueError, IndexError):
                continue

        if not bar_states and not inv_trades:
            return True

        try:
            # participation_cap 取 config.participation_rate（P1-09 已进配置，默认 1.0）
            violations = check_result_invariants(
                trades=inv_trades,
                bar_states=bar_states,
                initial_capital=float(self.config.initial_capital),
                participation_cap=float(
                    getattr(self.config, "participation_rate", 1.0)
                ),
            )
        except ValueError as exc:
            # 输入参数错误，软接线放行
            logger.warning("P1-15 invariants 输入参数错误，跳过: %s", exc)
            return True

        # 激活 RI-1/2/4/5/6（RI-3 因 close=0 自动跳过，不在 active 列表）
        active = (
            "invariants.price_bounds",
            "invariants.volume_bound",
            "invariants.cash_non_negative",
            "invariants.t1_consistency",
            "invariants.position_non_negative",
        )
        real = [v for v in violations if v.field_path in active]

        if real:
            msg = "; ".join(v.message for v in real[:5])
            logger.error("❌ P1-15 结果侧不变量未通过: %s", msg)
            if self.result is not None and hasattr(self.result, "mark_failed"):
                self.result.extra["realism_invariants_failed"] = True
                self.result.extra["realism_invariants_violations"] = [
                    {"field": v.field_path, "message": v.message}
                    for v in real[:10]
                ]
                self.result.mark_failed(f"realism_invariants_failed: {msg}")
            return False

        logger.info(
            "✅ P1-15 结果侧不变量校验通过（RI-1/2/4/5/6，trades=%d, bars=%d）",
            len(inv_trades), len(bar_states),
        )
        if self.result is not None:
            self.result.extra["realism_invariants_passed"] = True
        return True

    def _run_light_guard_check(self) -> bool:
        """P1-03：第二道防未来防线（轻量）——沿已处理时间线逐 bar 推进时钟，
        检测时间线倒退（乱序数据）；收尾 assert_pass。

        Returns:
            True 通过；False 已 mark_failed。
        """
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

    # ─────────────────────────────────────────
    # 组合聚合
    # ─────────────────────────────────────────

    def _aggregate_portfolio(self, sub_initial: float) -> Tuple[List[EquityPoint], List[TradeRecord]]:
        """聚合多标的子账户为组合净值曲线与成交记录。

        组合在全部日期的并集上取值：标的数据缺失的日期按其子账户
        初始现金（sub_initial）计资产，保证组合从初始资金完整演化。

        Args:
            sub_initial: 单标的子账户初始资金。

        Returns:
            (equity_curve, trades) 元组。
        """
        # 每标的: date -> (cash, position_value)
        per_symbol_maps: List[Tuple[Dict[str, float], Dict[str, float]]] = []
        all_dates: set = set()
        all_trades: List[TradeRecord] = []
        for sr in self._symbol_results:
            cash_map: Dict[str, float] = {}
            pos_map: Dict[str, float] = {}
            for i, d in enumerate(sr.dates):
                cash_map[d] = float(sr.cash[i])
                pos_map[d] = float(sr.position_value[i])
                all_dates.add(d)
            per_symbol_maps.append((cash_map, pos_map))
            all_trades.extend(sr.trades)

        ordered_dates = sorted(all_dates)
        # P2-08：日期轴对齐真实交易日历——区间内停牌/节假日补占位（组合按现金静止计
        # equity=1.0），避免多标的日期轴取数据并集漏掉真实交易日；日历缓存不可用回退并集。
        ordered_dates, calendar_source = self._align_to_trade_calendar(ordered_dates)
        if self.result is not None and hasattr(self.result, "extra"):
            self.result.extra["calendar_source"] = calendar_source
        initial = self.config.initial_capital

        equity_curve: List[EquityPoint] = []
        # 组合净值历史峰值（增量维护，只依赖截至 t 时刻的净值，避免未来函数）
        running_peak: float = 0.0
        for d in ordered_dates:
            total_cash = 0.0
            total_pos = 0.0
            for cash_map, pos_map in per_symbol_maps:
                # 标的数据缺失日期按其初始现金计（资金未动用）
                total_cash += cash_map.get(d, sub_initial)
                total_pos += pos_map.get(d, 0.0)
            total_asset = total_cash + total_pos
            equity = total_asset / initial if initial > 0 else 1.0
            # 本行使用的数据截至 t 时刻（当日组合总资产），峰值不含未来净值
            running_peak = max(running_peak, equity)
            drawdown = (equity - running_peak) / running_peak if running_peak > 0 else 0.0
            ts = self._date_to_timestamp(d)
            equity_curve.append(
                EquityPoint(
                    timestamp=ts,
                    date=d,
                    equity=equity,
                    cash=total_cash,
                    position_value=total_pos,
                    drawdown=drawdown,
                    benchmark_equity=1.0,
                )
            )
        return equity_curve, all_trades

    @staticmethod
    def _align_to_trade_calendar(dates: List[str]) -> Tuple[List[str], str]:
        """P2-08：把数据并集日期轴对齐到真实交易日历。

        区间内停牌/节假日补占位日期（组合在该日现金静止、equity=1.0），避免多标的
        日期轴取数据并集时漏掉真实交易日。真实日历缓存（data/trade_calendar.csv）
        不可用或区间无覆盖时，原样返回数据并集并标注来源，不伪造日历。

        Args:
            dates: 数据并集日期（ISO 'YYYY-MM-DD'，调用方已排序）。

        Returns:
            Tuple[List[str], str]: (对齐后日期升序列表, 来源标签 'real_calendar'/'data_union')。
        """
        if not dates:
            return dates, "data_union"
        try:
            from myquant.utils.trade_calendar import load_trade_calendar
            proj_root = Path(__file__).resolve().parents[2]
            cal = load_trade_calendar(proj_root)
        except Exception:
            return dates, "data_union"
        if not cal:
            return dates, "data_union"
        try:
            d0 = _date.fromisoformat(dates[0])
            d1 = _date.fromisoformat(dates[-1])
        except (ValueError, TypeError):
            return dates, "data_union"
        cal_dates = {d.isoformat() for d in cal if d0 <= d <= d1}
        if not cal_dates:
            return dates, "data_union"
        # 只补不删：数据并集 ∪ 日历交易日（日历覆盖区间内补齐缺失交易日，
        # 保留数据既有日期，避免误删测试夹具/停牌占位日导致日期轴缩水）。
        merged = set(dates) | cal_dates
        return sorted(merged), "real_calendar"

    # ─────────────────────────────────────────
    # 工具方法
    # ─────────────────────────────────────────

    @staticmethod
    def _idx_to_timestamp(idx: Any) -> float:
        """将 DataFrame index 元素转为时间戳。"""
        try:
            return float(pd.Timestamp(idx).timestamp())
        except (ValueError, TypeError, OverflowError):
            return 0.0

    @staticmethod
    def _date_to_timestamp(date_str: str) -> float:
        """将 YYYY-MM-DD 日期字符串转为时间戳。"""
        try:
            return float(pd.Timestamp(date_str).timestamp())
        except (ValueError, TypeError):
            return 0.0

    def get_result(self) -> Optional[BacktestResult]:
        """获取回测结果。"""
        return self.result

    def __repr__(self) -> str:
        return (
            f"VectorizedBacktestEngine(strategy={getattr(self.strategy, 'name', 'None')}, "
            f"symbols={len(self.data)}, mode={self.capital_mode})"
        )




if __name__ == "__main__":
    # 自测已迁至 myquant/backtest/vectorized_selftest.py（P0-00A 第三刀，回写#303）
    from myquant.backtest.vectorized_selftest import run_vectorized_selftest

    raise SystemExit(run_vectorized_selftest())
