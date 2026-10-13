# 文件名：myquant/strategies/multi_factor_strategy.py
"""
strategies/multi_factor_strategy.py - 多因子选股策略
======================================================
纯价格因子多因子打分选股：动量 + 反转 + 低波动 + 量价趋势，
z-score 标准化后加权打分，定期调仓选 top_n 持有。
适合震荡与趋势混合市，分散化持仓，是动量/均值回归的进阶组合。

策略逻辑:
  1. 四个纯价格因子（全部 trailing 计算，严格防未来）:
     - momentum   动量(20日)     = N期收益率 (close/close.shift(N)-1)，越大越好
     - reversal   反转(5日)      = M期收益率，越小越好（短期超跌→反弹）
     - low_vol    低波动(20日)   = K期日收益率标准差，越小越好
     - vol_price  量价趋势(20日) = L期收盘价与成交量 Pearson 相关，越大越好
  2. 因子标准化（z-score，两种模式）:
     - cross_sectional（默认）: 调仓日横截面标准化 z=(x-mean)/std
     - ts_rolling: 个股滚动窗口标准化（trailing 均值/标准差）
  3. 加权打分 composite = Σ weight * direction * z（方向见 FACTOR_DIRECTION）
     选得分 top_n，支持 min_score 下限过滤
  4. 定期调仓（复用 base._should_rebalance），等权或因子加权分配
  5. 止损/止盈/单票仓位上限（复用 base.py 工具方法）
  6. 价值/质量因子预留 extra_factors + factor_provider 插拔接口
     （后续有基本面数据时，注入 provider.get_factor(symbol, name) 即可）

防未来函数（铁律）:
  - 所有因子基于 trailing 窗口（pct_change / rolling），不含未来
  - _get_data_upto 按当前bar时间戳截断 context.data
    （回测中 context.data 可能是全量历史，截断后杜绝未来函数；
      实盘 data 为增量同步，截断为无操作，天然一致）
  - 调仓在当前bar收盘后判断，成交由引擎在下一根bar处理（T日信号T+1成交）
  - 止损止盈用当前bar收盘价（已收盘数据）
  - 源码无预测方法/无硬编码未来日期

为实盘无缝对接准备:
  - 继承 StrategyBase，回测/实盘共用同一策略实例，切换零修改
  - on_bar 逐根驱动（回测/实盘统一入口）
  - 复用 base.py 通用工具：_should_rebalance / check_stop_loss / check_take_profit
  - 持仓状态用 context.get_position() 查询，不硬编码
  - factor_provider 可注入外部因子源（如基本面数据），回测/实盘统一
  - 指标实例化与因子计算均无状态依赖历史外部对象，支持增量行情
  - get_multi_factor_status() 供实盘监控/Web看板使用

使用方式:
    from myquant.strategies import create_strategy
    strategy = create_strategy("multi_factor",
        momentum_period=20, reversal_period=5, volatility_period=20,
        vol_price_period=20, top_n=5, rebalance_days=20,
        factor_weights={"momentum":0.35,"reversal":0.25,"low_vol":0.20,"vol_price":0.20})
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.indicators.factor_preprocessor import (
    PreprocessReport,
    preprocess_panel,
)
from myquant.indicators.factor_validator import (
    FACTOR_ECONOMIC_LOGIC,
    FactorProfile,
    ic_weighted_weights,
    validate_factor_logic,
)
from myquant.indicators.tradability import (
    TradabilityConfig,
    filter_tradable,
    rejected_summary,
)
from myquant.leakage_gate.cross_sectional import CrossSectionalAsOfError, validate_asof
from myquant.models.market_data import Bar
from myquant.models.trade import OrderSide, OrderType, Position
from myquant.strategies.base import (
    Signal, SignalType, StrategyBase, StrategyContext,
)
from myquant.strategies.registry import register_strategy

logger = get_logger(__name__)


@register_strategy
class MultiFactorStrategy(StrategyBase):
    """
    多因子选股策略

    核心参数:
      momentum_period:    动量因子周期（默认20，bar数）
      reversal_period:    反转因子周期（默认5，bar数）
      volatility_period:  低波动因子周期（默认20，bar数）
      vol_price_period:   量价趋势因子周期（默认20，bar数）
      top_n:              每次调仓选前N只综合得分最高标的（默认5）
      rebalance_days:     调仓周期（bar事件数，默认20）
      stop_loss_pct:      单票止损比例（默认0.10）
      take_profit_pct:    单票止盈比例（默认0.20）
      max_position_pct:   单票最大仓位比例（默认0.20）
      max_positions:      最大持仓数（默认5）
      equal_weight:       是否等权分配（默认True；False=按综合得分加权）
      factor_weights:     因子权重字典，覆盖默认权重（默认None=用默认权重）
      ic_based_weights:   是否启用 IC 加权权重（默认False=沿用默认/手工权重，
                          现有回测行为零变化；True 时须注入 factor_profiles，
                          权重 = IC 档案加权 + 下限约束，来源可追溯）
      factor_profiles:    IC 档案字典 {因子名: FactorProfile 或 dict}，
                          供 ic_based_weights=True 时计算 IC 加权权重
      ic_weight_min:      IC 加权单因子权重下限（默认0.05）
      preprocess_mode:    因子预处理模式（F-03，默认"none"=现有行为零变化）：
                          "none"不预处理 / "winsorize"仅自适应去极值 /
                          "neutralize"去极值+行业/市值中性化+残差检验 /
                          "full"同"neutralize"（全流程语义，当前实现一致）
      industry_map:       行业映射 {symbol: 行业}（中性化用；缺省时降级为
                          仅市值中性化，报告记录口径，不静默）
      preprocess_report_sink: 预处理检验报告 JSON 落盘路径（可选，None=不落盘）
      zscore_mode:        标准化模式："cross_sectional"(默认) / "ts_rolling"
      ts_roll_window:     ts_rolling 模式滚动标准化窗口（默认60）
      min_factor_stocks:  额外因子有效样本下限，不足则丢弃该因子（默认2）
      min_score:          综合得分下限过滤（默认None=不过滤）
      extra_factors:      额外因子配置字典，预留价值/质量因子，例:
                          {"quality_roe": {"weight":0.20, "higher_better":True,
                           "economic_logic":"高ROE企业盈利质量更优（质量溢价）"}}
                          缺省 economic_logic（经济逻辑说明）的额外因子禁止进组合
      factor_provider:    额外因子数据提供器，需实现 get_factor(symbol, name)
                          返回 Optional[float]；为None时额外因子全部跳过

    信号生成:
      - 调仓日换仓 → SignalType.BUY / SELL（多标的，返回 List[Signal]）
      - 单票止损 → SignalType.STOP_LOSS
      - 单票止盈 → SignalType.TAKE_PROFIT
      - 非调仓日无止损止盈 → SignalType.HOLD

    防未来函数:
      - 因子 trailing 窗口 + 调仓时按当前bar时间戳截断数据
      - 调仓收盘后判断，下一根bar成交
      - 止损止盈用已收盘价格
    """

    name = "multi_factor"

    # 防泄漏门禁契约元数据（M0-04，主键=name，DEC-01；对齐 config/strategy_registry.yaml）
    strategy_id: str = "multi_factor_strategy"
    decision_time: str = "T日收盘后 15:00"
    execution_time: str = "T+1开盘 09:30"
    holding_period: str = "20交易日调仓"
    universe: Dict[str, Any] = {
        "type": "static",
        "source": "config/stock_pool.yaml",
        "pit_status": "未建设",
    }
    data_availability: Dict[str, Any] = {
        "market_data": {
            "available_time": "T日15:00",
            "adjust": "hfq",
            "adjust_asof": "end_date 必传",
            "fields": ["开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"],
        },
        "financial_data": {
            "available_time": "ann_date",
            "basis": "eff_disclosure_date",
            "fields": ["流通股本"],
        },
    }
    consumed_columns: List[str] = ["收盘价", "成交量", "流通股本"]  # 流通股本现算（t 时刻可得）
    ruleset: str = "daily_close_decision"

    # 基础因子（纯价格，打分顺序即此顺序）
    BASE_FACTORS: Tuple[str, ...] = ("momentum", "reversal", "low_vol", "vol_price")

    # 默认权重（可被 factor_weights 整体覆盖）
    DEFAULT_FACTOR_WEIGHTS: Dict[str, float] = {
        "momentum": 0.35,
        "reversal": 0.25,
        "low_vol": 0.20,
        "vol_price": 0.20,
    }

    # 因子方向：1=越大越好，-1=越小越好
    FACTOR_DIRECTION: Dict[str, int] = {
        "momentum": 1,    # 动量越大越好
        "reversal": -1,   # 短期跌幅越大（越超跌）越好，博反弹
        "low_vol": -1,    # 波动率越低越好
        "vol_price": 1,   # 量价相关性越高越好（量价齐升）
    }

    def __init__(
        self,
        momentum_period: int = 20,
        reversal_period: int = 5,
        volatility_period: int = 20,
        vol_price_period: int = 20,
        top_n: int = 5,
        rebalance_days: int = 20,
        stop_loss_pct: float = 0.10,
        take_profit_pct: float = 0.20,
        max_position_pct: float = 0.20,
        max_positions: int = 5,
        equal_weight: bool = True,
        factor_weights: Optional[Dict[str, float]] = None,
        ic_based_weights: bool = False,
        factor_profiles: Optional[Dict[str, Any]] = None,
        ic_weight_min: float = 0.05,
        preprocess_mode: str = "none",
        industry_map: Optional[Dict[str, str]] = None,
        preprocess_report_sink: Optional[str] = None,
        zscore_mode: str = "cross_sectional",
        ts_roll_window: int = 60,
        min_factor_stocks: int = 2,
        min_score: Optional[float] = None,
        extra_factors: Optional[Dict[str, Dict[str, Any]]] = None,
        factor_provider: Any = None,
        vec_score_threshold: float = 0.0,
        capital_base: float = 1_000_000.0,
        **params: Any,
    ):
        # F-11：从透传参数提取可交易性过滤配置（不传给基类 super，默认关闭零变化）
        tradability = params.pop("tradability", False)
        tradability_params = params.pop("tradability_params", None)
        super().__init__(
            momentum_period=momentum_period,
            reversal_period=reversal_period,
            volatility_period=volatility_period,
            vol_price_period=vol_price_period,
            top_n=top_n,
            rebalance_days=rebalance_days,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            max_position_pct=max_position_pct,
            max_positions=max_positions,
            equal_weight=equal_weight,
            factor_weights=factor_weights,
            ic_based_weights=ic_based_weights,
            factor_profiles=factor_profiles,
            ic_weight_min=ic_weight_min,
            preprocess_mode=preprocess_mode,
            industry_map=industry_map,
            preprocess_report_sink=preprocess_report_sink,
            zscore_mode=zscore_mode,
            ts_roll_window=ts_roll_window,
            min_factor_stocks=min_factor_stocks,
            min_score=min_score,
            extra_factors=extra_factors,
            factor_provider=factor_provider,
            vec_score_threshold=vec_score_threshold,
            capital_base=capital_base,
            **params,
        )

        # ── 参数校验 ──
        if momentum_period < 1 or reversal_period < 1 or vol_price_period < 1:
            raise ValueError("动量/反转/量价因子周期必须>=1")
        if volatility_period < 2:
            raise ValueError(f"波动率周期({volatility_period})必须>=2（标准差至少需2个样本）")
        if top_n < 1:
            raise ValueError(f"选股数量({top_n})必须>=1")
        if rebalance_days < 1:
            raise ValueError(f"调仓周期({rebalance_days})必须>=1")
        if stop_loss_pct < 0 or take_profit_pct < 0:
            raise ValueError("止损/止盈比例不能为负")
        if not 0 < max_position_pct <= 1.0:
            raise ValueError(f"单票最大仓位({max_position_pct})必须在(0,1]")
        if max_positions < 1:
            raise ValueError(f"最大持仓数({max_positions})必须>=1")
        if zscore_mode not in ("cross_sectional", "ts_rolling"):
            raise ValueError(
                f"zscore_mode 必须为 'cross_sectional' 或 'ts_rolling'，当前: {zscore_mode}"
            )
        if zscore_mode == "ts_rolling" and ts_roll_window < 2:
            raise ValueError(f"ts_rolling 模式滚动窗口({ts_roll_window})必须>=2")
        if min_factor_stocks < 1:
            raise ValueError(f"额外因子有效样本下限({min_factor_stocks})必须>=1")
        if ic_based_weights and not (0.0 < ic_weight_min <= 1.0):
            raise ValueError(f"IC 加权权重下限({ic_weight_min})必须在(0,1]")
        if preprocess_mode not in ("none", "winsorize", "neutralize", "full"):
            raise ValueError(
                f"preprocess_mode 必须为 none/winsorize/neutralize/full，当前: {preprocess_mode}"
            )

        # ── 因子参数 ──
        self.momentum_period = momentum_period
        self.reversal_period = reversal_period
        self.volatility_period = volatility_period
        self.vol_price_period = vol_price_period
        self.top_n = top_n
        self.rebalance_days = rebalance_days
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_position_pct = max_position_pct
        self.max_positions = max_positions
        self.equal_weight = equal_weight
        self.zscore_mode = zscore_mode
        self.ts_roll_window = ts_roll_window
        self.min_factor_stocks = min_factor_stocks
        self.min_score = min_score
        self.vec_score_threshold = vec_score_threshold
        self.capital_base = capital_base

        # ── 因子权重（覆盖默认）──
        if factor_weights is None:
            base_weights = dict(self.DEFAULT_FACTOR_WEIGHTS)
        else:
            # 显式传入时，未提及的基础因子视为0（停用）
            base_weights = {
                f: float(factor_weights.get(f, 0.0)) for f in self.BASE_FACTORS
            }
        if sum(base_weights.values()) <= 0:
            raise ValueError("factor_weights 至少需要一个因子的权重>0")
        self._base_factor_weights: Dict[str, float] = {
            f: w for f, w in base_weights.items() if w > 0
        }

        # ── 额外因子配置（价值/质量，预留）──
        # F-02：缺省 economic_logic（经济逻辑说明）的额外因子禁止进组合
        self.extra_factors: Dict[str, Dict[str, Any]] = dict(extra_factors) if extra_factors else {}
        self._extra_factor_configs: Dict[str, Dict[str, Any]] = {}
        for fname, cfg in self.extra_factors.items():
            if not isinstance(cfg, dict):
                raise ValueError(f"额外因子 {fname} 配置必须是字典，含 weight/higher_better")
            logic = str(cfg.get("economic_logic", "")).strip()
            if not validate_factor_logic(fname, logic):
                logger.warning(f"[多因子] 额外因子 {fname} 无经济逻辑说明，禁止进组合")
                continue
            w = float(cfg.get("weight", 0.0))
            hb = bool(cfg.get("higher_better", True))
            if w <= 0:
                raise ValueError(f"额外因子 {fname} 的 weight 必须>0")
            self._extra_factor_configs[fname] = {
                "weight": w, "higher_better": hb, "economic_logic": logic,
            }

        # ── 额外因子数据提供器（可插拔）──
        self.factor_provider = factor_provider
        if factor_provider is not None and not hasattr(factor_provider, "get_factor"):
            logger.warning(
                "[多因子] factor_provider 未实现 get_factor(symbol, factor_name) 接口，"
                "额外因子将无法取数"
            )

        # ── IC 加权配置（F-02，默认关闭=现有回测行为零变化）──
        self.ic_based_weights = bool(ic_based_weights)
        self.ic_weight_min = float(ic_weight_min)
        self._factor_profiles: Dict[str, FactorProfile] = {}
        if factor_profiles:
            for fname, data in factor_profiles.items():
                if isinstance(data, FactorProfile):
                    self._factor_profiles[fname] = data
                elif isinstance(data, dict):
                    try:
                        self._factor_profiles[fname] = FactorProfile.from_dict(data)
                    except ValueError as e:
                        logger.warning(f"[多因子] 忽略非法 IC 档案 {fname}: {e}")
                else:
                    logger.warning(
                        f"[多因子] 忽略非法 IC 档案类型 {fname}: {type(data).__name__}"
                    )
        if self.ic_based_weights and not self._factor_profiles:
            logger.warning(
                "[多因子] ic_based_weights=True 但未注入 factor_profiles，回退默认权重"
            )

        # ── 因子预处理配置（F-03，默认 none=现有行为零变化）──
        self.preprocess_mode = preprocess_mode
        self.industry_map: Dict[str, str] = dict(industry_map) if industry_map else {}
        self.preprocess_report_sink = preprocess_report_sink
        self._last_preprocess_report: Optional[Dict[str, Any]] = None
        if self.preprocess_mode in ("neutralize", "full") and not self.industry_map:
            logger.warning(
                "[多因子] 中性化模式未提供 industry_map，降级为仅市值中性化"
                "（报告记录口径，不静默）"
            )

        # ── F-11 可交易性过滤配置（默认关闭=现有行为零变化）──
        self.tradability = bool(tradability)
        self._tradability_config: TradabilityConfig = TradabilityConfig.from_dict(
            tradability_params
        )
        self._tradability_stats: Dict[str, Any] = {
            "enabled": self.tradability,
            "rejected": 0,
            "by_symbol": {},
        }

        # 因子序列最少bar数（取最大周期+1）
        self._min_factor_bars: int = max(
            momentum_period, reversal_period, volatility_period, vol_price_period
        ) + 1

        # ── 运行时状态 ──
        self.current_pool: List[str] = []
        self._composite_scores: Dict[str, float] = {}
        self._factor_snapshot: Dict[str, Dict[str, float]] = {}
        self._rebalance_count: int = 0
        self._stop_loss_count: int = 0
        self._take_profit_count: int = 0

        logger.info(
            f"多因子策略初始化: 动量{momentum_period}/反转{reversal_period}/"
            f"低波动{volatility_period}/量价{vol_price_period}, "
            f"top_n={top_n}, 调仓={rebalance_days}bar, "
            f"权重={self._base_factor_weights}, IC加权={self.ic_based_weights}, "
            f"标准化={zscore_mode}, "
            f"等权={equal_weight}, 止损{stop_loss_pct:.0%}/止盈{take_profit_pct:.0%}, "
            f"额外因子={list(self._extra_factor_configs.keys()) if self._extra_factor_configs else '无'}"
        )

    # ═══════════════════════════════════════════════
    #  核心：on_bar（每根bar处理，返回信号列表）
    # ═══════════════════════════════════════════════

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """每根bar处理：止损止盈检查 + 调仓判断"""
        signals: List[Signal] = []

        # 1. 单票止损/止盈检查（每根bar都检查，复用 base.py 工具）
        sl_tp_signals = self._check_stop_loss_take_profit(bar, context)
        signals.extend(sl_tp_signals)

        # 2. 调仓判断（复用 base.py 通用工具）
        if self._should_rebalance(context, self.rebalance_days):
            rebalance_signals = self._do_rebalance(bar, context)
            signals.extend(rebalance_signals)

        # 统计信号数
        non_hold = [s for s in signals if not s.is_hold]
        if non_hold:
            self._signal_count += len(non_hold)

        # P0-06修复：策略只产信号，不再 create_order + context.submit_order
        # （_submit_signal_order 已删除）；订单由引擎统一执行，经风控/防未来/幂等链。
        return signals

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        """单信号生成（兼容基类抽象方法，实际逻辑在 on_bar）

        多因子策略是多标的调仓策略，核心逻辑在 on_bar 返回 List[Signal]。
        此方法返回当前bar标的的单信号（止损/止盈/HOLD），供单标的场景使用。
        """
        position = context.get_position(bar.symbol)
        if position and position.quantity > 0:
            if self.check_stop_loss(position, bar.close, self.stop_loss_pct):
                return Signal(
                    symbol=bar.symbol, signal_type=SignalType.STOP_LOSS,
                    strength=1.0, price=bar.close,
                    reason=f"stop_loss -{self.stop_loss_pct:.0%}",
                    strategy_id=self.name,
                )
            if self.check_take_profit(position, bar.close, self.take_profit_pct):
                return Signal(
                    symbol=bar.symbol, signal_type=SignalType.TAKE_PROFIT,
                    strength=1.0, price=bar.close,
                    reason=f"take_profit +{self.take_profit_pct:.0%}",
                    strategy_id=self.name,
                )
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD,
                      price=bar.close, reason="no_signal", strategy_id=self.name)

    # ═══════════════════════════════════════════════
    #  止损止盈检查
    # ═══════════════════════════════════════════════

    def _check_stop_loss_take_profit(
        self, bar: Bar, context: StrategyContext,
    ) -> List[Signal]:
        """检查当前bar标的的止损/止盈（复用 base.py 工具方法）"""
        signals: List[Signal] = []
        position = context.get_position(bar.symbol)
        if position is None or position.quantity <= 0:
            return signals

        # 止损
        if self.check_stop_loss(position, bar.close, self.stop_loss_pct):
            self._stop_loss_count += 1
            logger.warning(
                f"[多因子止损] {bar.symbol} 现价{bar.close:.2f} "
                f"成本{position.avg_cost:.2f} 跌幅>={self.stop_loss_pct:.0%}, 卖出"
            )
            signals.append(Signal(
                symbol=bar.symbol, signal_type=SignalType.STOP_LOSS,
                strength=1.0, price=bar.close,
                reason=f"stop_loss -{self.stop_loss_pct:.0%}",
                strategy_id=self.name,
            ))

        # 止盈
        elif self.check_take_profit(position, bar.close, self.take_profit_pct):
            self._take_profit_count += 1
            logger.info(
                f"[多因子止盈] {bar.symbol} 现价{bar.close:.2f} "
                f"成本{position.avg_cost:.2f} 涨幅>={self.take_profit_pct:.0%}, 卖出"
            )
            signals.append(Signal(
                symbol=bar.symbol, signal_type=SignalType.TAKE_PROFIT,
                strength=1.0, price=bar.close,
                reason=f"take_profit +{self.take_profit_pct:.0%}",
                strategy_id=self.name,
            ))

        return signals

    # ═══════════════════════════════════════════════
    #  调仓逻辑（核心）
    # ═══════════════════════════════════════════════

    def _do_rebalance(
        self, bar: Bar, context: StrategyContext,
    ) -> List[Signal]:
        """执行调仓：计算因子 → 标准化 → 打分 → 选股 → 卖出/买入"""
        self._rebalance_count += 1
        signals: List[Signal] = []

        # ── 1. 收集候选标的的因子序列（按当前bar截断，防未来）──
        candidates = self._get_all_symbols(context)
        factor_series: Dict[str, pd.DataFrame] = {}
        market_cap: Dict[str, float] = {}
        for sym in candidates:
            df = self._get_data_upto(context, sym, bar)
            if df is None or len(df) < self._min_factor_bars:
                continue
            series = self._calc_factor_series(sym, df)
            if series is not None:
                if self.tradability:
                    # F-11：因子计算/选层前过滤不可交易标的。df 已截断到当前
                    # bar 时点（防未来），filter_tradable 均额 rolling 仅用 <=t
                    # 数据；取末尾行=当前时点可交易性，False 则剔除出候选
                    mask, tstats = filter_tradable(df, self._tradability_config)
                    if not bool(mask.iloc[-1]):
                        self._tradability_stats["rejected"] += 1
                        self._tradability_stats["by_symbol"][sym] = tstats
                        logger.debug(
                            f"[多因子调仓#{self._rebalance_count}] {sym} 不可交易，"
                            f"剔除: {rejected_summary(tstats)}"
                        )
                        continue
                factor_series[sym] = series
                mc = self._calc_market_cap(df)
                if mc is not None:
                    market_cap[sym] = mc

        if len(factor_series) < 2:
            logger.warning(
                f"[多因子调仓#{self._rebalance_count}] 有效候选{len(factor_series)}<2，跳过"
            )
            return signals

        # ── 2. 标准化基础因子（F-03：可选去极值/中性化预处理前置）──
        # P1-07④：由当前调仓 bar 归一化决策时点，透传给横截面合成处的 as-of 校验；
        # bar.timestamp<=0（实盘增量无有效时间）→ 无决策日可验，跳过校验（增量数据无未来）
        decision_time: Optional[str] = None
        _ts = float(getattr(bar, "timestamp", 0) or 0)
        if _ts > 0:
            decision_time = pd.Timestamp.fromtimestamp(_ts).strftime("%Y-%m-%d")
        base_scores, active_base = self._standardize_base_factors(
            factor_series, market_cap, decision_time=decision_time)
        if not active_base or len(base_scores) < 2:
            logger.warning(
                f"[多因子调仓#{self._rebalance_count}] 标准化后有效标的{len(base_scores)}<2，跳过"
            )
            return signals

        # ── 3. 额外因子（价值/质量，预留插拔）──
        extra_scores, active_extra = self._compute_extra_factors(
            list(base_scores.keys()), context,
        )

        active_factors = active_base + active_extra
        # F-02：权重来源可追溯——IC 加权（启用时）或默认/手工权重（关闭时）
        weights = self._resolve_weights(active_factors)
        if not weights:
            logger.warning(
                f"[多因子调仓#{self._rebalance_count}] 激活因子权重为空，跳过"
            )
            return signals

        # ── 4. 综合打分 ──
        composite: Dict[str, float] = {}
        for sym in base_scores:
            score = 0.0
            for f in active_factors:
                w = weights[f]
                if w <= 0:
                    continue
                direction = self._factor_direction(f)
                if f in base_scores[sym]:
                    z = base_scores[sym][f]
                else:
                    z = extra_scores.get(sym, {}).get(f, 0.0)
                score += w * direction * z
            composite[sym] = score
        self._composite_scores = composite
        self._factor_snapshot = {
            sym: dict(base_scores[sym]) for sym in base_scores
        }

        # ── 5. 选股：按综合得分降序，过滤 min_score，取前 min(top_n, max_positions) ──
        ranked = sorted(composite.items(), key=lambda x: x[1], reverse=True)
        if self.min_score is not None:
            ranked = [(s, sc) for s, sc in ranked if sc >= self.min_score]
        new_pool = [s for s, _ in ranked][:min(self.top_n, self.max_positions)]

        logger.info(
            f"[多因子调仓#{self._rebalance_count}] 候选{len(composite)}只 → "
            f"入选{len(new_pool)}只: {new_pool}"
        )
        for sym in new_pool:
            logger.debug(
                f"  {sym}: score={composite[sym]:.3f} "
                f"factors={self._factor_snapshot.get(sym, {})}"
            )

        # ── 6. 卖出不在新股票池中的持仓 ──
        current_positions = self._get_current_positions(context)
        for sym in list(current_positions.keys()):
            if sym not in new_pool:
                pos = current_positions[sym]
                if pos and pos.quantity > 0:
                    sym_price = self._get_latest_price(context, sym, bar, bar.close)
                    logger.info(f"[多因子调仓卖出] {sym} 不在新池，清仓 @{sym_price:.2f}")
                    signals.append(Signal(
                        symbol=sym, signal_type=SignalType.SELL,
                        strength=1.0, price=sym_price,
                        reason="rebalance_sell (not in pool)",
                        strategy_id=self.name,
                    ))

        # ── 7. 买入新入池标的（等权或因子加权，单票上限）──
        cash = context.get_cash()
        if cash > 0 and new_pool:
            target_weights = self._alloc_weights(new_pool, composite)
            for sym in new_pool:
                # 已持仓的不再买入（与动量策略一致，只处理新增/剔除）
                if sym in current_positions:
                    continue
                sym_price = self._get_latest_price(context, sym, bar, bar.close)
                if sym_price <= 0:
                    continue
                target_amount = cash * target_weights[sym]
                # 单票仓位上限
                target_amount = min(target_amount, cash * self.max_position_pct)
                if target_amount <= 0:
                    continue
                quantity = int(target_amount / sym_price / 100) * 100
                quantity = max(0, quantity)
                if quantity > 0:
                    logger.info(
                        f"[多因子调仓买入] {sym} 数量{quantity}股 "
                        f"金额≈{quantity * sym_price:.0f}元 @{sym_price:.2f} "
                        f"score={composite[sym]:.3f}"
                    )
                    signals.append(Signal(
                        symbol=sym, signal_type=SignalType.BUY,
                        strength=1.0, price=sym_price,
                        reason=(
                            f"rebalance_buy (score={composite[sym]:.3f}, "
                            f"factors={self._factor_snapshot.get(sym, {})})"
                        ),
                        strategy_id=self.name,
                        extra={"quantity": quantity, "score": composite[sym]},
                    ))

        # 更新当前股票池
        self.current_pool = new_pool
        return signals

    # ═══════════════════════════════════════════════
    #  因子计算（trailing，严格防未来）
    # ═══════════════════════════════════════════════

    def _calc_factor_series(
        self, symbol: str, df: pd.DataFrame,
    ) -> Optional[pd.DataFrame]:
        """计算标的的因子序列（DataFrame，索引与df对齐，全部trailing）

        列:
          momentum:  N期收益率（N=momentum_period），越大越好
          reversal:  M期收益率（M=reversal_period），越小越好（超跌反转）
          low_vol:   K期日收益率标准差（K=volatility_period），越小越好
          vol_price: L期收盘价与成交量 Pearson 相关（L=vol_price_period），越大越好

        返回 None 表示数据不足。
        """
        if df is None or df.empty or '收盘价' not in df.columns:
            return None
        if len(df) < self._min_factor_bars:
            return None

        close = df['收盘价'].astype(float)
        volume = df['成交量'].astype(float) if '成交量' in df.columns else None

        result = pd.DataFrame(index=df.index)
        # 动量：N期收益率（trailing）
        result['momentum'] = close.pct_change(self.momentum_period)
        # 反转：M期收益率（trailing）
        result['reversal'] = close.pct_change(self.reversal_period)
        # 低波动：K期日收益率标准差（trailing）
        returns = close.pct_change()
        result['low_vol'] = returns.rolling(
            self.volatility_period, min_periods=self.volatility_period,
        ).std(ddof=1)
        # 量价趋势：L期收盘价与成交量相关（trailing）
        if volume is not None:
            result['vol_price'] = close.rolling(
                self.vol_price_period, min_periods=self.vol_price_period,
            ).corr(volume)

        return result

    def _calc_market_cap(self, df: pd.DataFrame) -> Optional[float]:
        """计算标的截至最新bar的流通市值（trailing，防未来）。

        流通市值 = 最新收盘价 × 最新流通股本（processed 列，t 时刻可得）。

        Args:
            df: 该标的截至当前bar的数据（含 收盘价/流通股本 列）。

        Returns:
            流通市值（元），数据缺失返回 None。
        """
        if df is None or df.empty:
            return None
        close = df['收盘价'].astype(float).dropna()
        if len(close) == 0:
            return None
        if '流通股本' not in df.columns:
            return None
        float_share = df['流通股本'].astype(float).dropna()
        if len(float_share) == 0:
            return None
        price = float(close.iloc[-1])
        shares = float(float_share.iloc[-1])
        if price <= 0 or shares <= 0:
            return None
        return price * shares

    def _standardize_base_factors(
        self, factor_series: Dict[str, pd.DataFrame],
        market_cap: Optional[Dict[str, float]] = None,
        decision_time: Optional[str] = None,
    ) -> Tuple[Dict[str, Dict[str, float]], List[str]]:
        """标准化基础因子（F-03：可先去极值/中性化预处理，再 z-score）

        Args:
            factor_series: {symbol: 因子时序 DataFrame}（trailing 计算）。
            market_cap: {symbol: 流通市值}，中性化预处理用。
            decision_time: 决策时点（YYYY-MM-DD，P1-07④），透传给横截面合成处
                as-of 校验；None 时不校验（实盘增量无决策日语义）。

        Returns:
            (factor_scores: {symbol: {factor: z}}, active_factors: List[str])
            只包含权重>0的基础因子。
        """
        factors = [f for f in self.BASE_FACTORS if f in self._base_factor_weights]
        if not factors:
            return {}, []
        if self.zscore_mode == "ts_rolling":
            return self._zscore_ts_rolling(factor_series, factors)
        return self._zscore_cross_sectional(factor_series, factors, market_cap, decision_time=decision_time)

    def _zscore_cross_sectional(
        self, factor_series: Dict[str, pd.DataFrame], factors: List[str],
        market_cap: Optional[Dict[str, float]] = None,
        decision_time: Optional[str] = None,
    ) -> Tuple[Dict[str, Dict[str, float]], List[str]]:
        """横截面z-score：调仓日对全池标的标准化（trailing值，无未来）

        P1-07④ 横截面兜底：合成当日截面（raw 面板）前对每只标的的因子时序做
        as-of 校验——任何标的时序索引晚于 decision_time → 抛 CrossSectionalAsOfError
        阻断该截面（fail-closed；context.get_data 已封死切片，此为第二道防线，
        防因子序列被外部构造/缓存污染引入未来行）。

        Args:
            factor_series: {symbol: 因子时序 DataFrame}（trailing 计算）。
            factors: 参与标准化的基础因子名列表。
            market_cap: {symbol: 流通市值}，中性化预处理用。
            decision_time: 决策时点（YYYY-MM-DD）；None 时不校验。

        Returns:
            (factor_scores, active_factors)。

        Raises:
            CrossSectionalAsOfError: 合成截面发现晚于决策时点的数据行。
        """
        if decision_time is not None:
            asof_bad: List[str] = []
            for sym, series in factor_series.items():
                if series is None or len(series) == 0:
                    continue
                asof_bad.extend(validate_asof(series, decision_time))
            if asof_bad:
                raise CrossSectionalAsOfError(
                    f"多因子当日截面 as-of 校验失败（P1-07④），阻断该截面 "
                    f"(decision_time={decision_time}): " + "; ".join(asof_bad)
                )

        rows: Dict[str, Dict[str, float]] = {}
        for sym, series in factor_series.items():
            latest = series.iloc[-1]
            row: Dict[str, float] = {}
            ok = True
            for f in factors:
                v = latest.get(f, np.nan)
                if v is None or pd.isna(v):
                    ok = False
                    break
                row[f] = float(v)
            if ok:
                rows[sym] = row

        if len(rows) < 2:
            logger.warning("[多因子] 横截面标准化有效标的<2，跳过")
            return {}, []

        raw = pd.DataFrame(rows).T  # index=symbol, columns=factor

        # ── F-03：因子预处理（去极值/中性化/残差检验），默认 none=零变化 ──
        if self.preprocess_mode != "none":
            do_neutralize = self.preprocess_mode in ("neutralize", "full")
            mc_series: Optional[pd.Series] = None
            if do_neutralize and market_cap:
                mc_series = pd.Series(
                    {sym: float(mc) for sym, mc in market_cap.items() if mc > 0},
                    dtype=float,
                )
            clean, reports = preprocess_panel(
                raw,
                market_cap=mc_series if do_neutralize else None,
                industry_map=self.industry_map if do_neutralize else None,
                do_neutralize=do_neutralize,
            )
            self._last_preprocess_report = {
                "mode": self.preprocess_mode,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "factors": [r.to_dict() for r in reports],
                "industry_map_size": len(self.industry_map),
                "note": "行业分类为当前快照近似历史（低频变动），报告披露口径",
            }
            if self.preprocess_report_sink:
                try:
                    self._save_preprocess_report(self._last_preprocess_report)
                except Exception as e:  # 报告落盘失败不阻断调仓（记录告警）
                    logger.warning(f"[多因子] 预处理报告落盘失败: {e}")
            raw = clean

        z = raw.copy()
        for f in factors:
            std = raw[f].std(ddof=0)
            if not np.isnan(std) and std > 0:
                z[f] = (raw[f] - raw[f].mean()) / std
            else:
                # 全池该因子无差异 → 中性0
                z[f] = 0.0

        factor_scores = {
            sym: {f: float(z.loc[sym, f]) for f in factors} for sym in z.index
        }
        return factor_scores, factors

    def _zscore_ts_rolling(
        self, factor_series: Dict[str, pd.DataFrame], factors: List[str],
    ) -> Tuple[Dict[str, Dict[str, float]], List[str]]:
        """个股滚动窗口z-score：对每只标的自身 trailing 均值/标准差标准化"""
        factor_scores: Dict[str, Dict[str, float]] = {}
        for sym, series in factor_series.items():
            latest_z: Dict[str, float] = {}
            ok = True
            for f in factors:
                col = series[f].dropna()
                if len(col) < self.ts_roll_window:
                    ok = False
                    break
                mean = col.rolling(
                    self.ts_roll_window, min_periods=self.ts_roll_window,
                ).mean()
                std = col.rolling(
                    self.ts_roll_window, min_periods=self.ts_roll_window,
                ).std(ddof=1)
                z_val = (col - mean) / std.replace(0, np.nan)
                last = z_val.iloc[-1]
                if pd.isna(last):
                    ok = False
                    break
                latest_z[f] = float(last)
            if ok:
                factor_scores[sym] = latest_z

        if len(factor_scores) < 2:
            logger.warning("[多因子] 滚动标准化有效标的<2，跳过")
            return {}, []
        return factor_scores, factors

    # ═══════════════════════════════════════════════
    #  额外因子（价值/质量，预留插拔接口）
    # ═══════════════════════════════════════════════

    def _compute_extra_factors(
        self, symbols: List[str], context: StrategyContext,
    ) -> Tuple[Dict[str, Dict[str, float]], List[str]]:
        """计算额外因子得分（横截面z-score）

        无 factor_provider 或有效样本不足 min_factor_stocks 的因子被丢弃。
        缺失样本的标的该因子记中性0（不排除）。
        """
        if not self._extra_factor_configs or self.factor_provider is None:
            return {}, []

        active: List[str] = []
        extra_scores: Dict[str, Dict[str, float]] = {sym: {} for sym in symbols}

        for fname in self._extra_factor_configs:
            values: Dict[str, float] = {}
            for sym in symbols:
                v = self._get_extra_factor_value(fname, sym, context)
                if v is not None:
                    values[sym] = v

            if len(values) < self.min_factor_stocks:
                logger.info(
                    f"[多因子] 额外因子 {fname} 有效样本{len(values)}"
                    f"<{self.min_factor_stocks}，丢弃"
                )
                continue

            mean = float(np.mean(list(values.values())))
            std = float(np.std(list(values.values()), ddof=0))
            for sym in symbols:
                if sym in values:
                    z = (values[sym] - mean) / std if std > 0 else 0.0
                    extra_scores[sym][fname] = float(z) if not np.isnan(z) else 0.0
                else:
                    extra_scores[sym][fname] = 0.0  # 缺失样本给中性0
            active.append(fname)
            logger.info(
                f"[多因子] 额外因子 {fname} 启用（样本{len(values)}只）"
            )

        return extra_scores, active

    def _get_extra_factor_value(
        self, factor_name: str, symbol: str, context: StrategyContext,
    ) -> Optional[float]:
        """从 factor_provider 获取额外因子值（可插拔，缺省返回None）"""
        if self.factor_provider is None:
            return None
        try:
            getter = getattr(self.factor_provider, "get_factor", None)
            if getter is None:
                return None
            value = getter(symbol, factor_name)
            if value is None:
                return None
            v = float(value)
            return v if not np.isnan(v) else None
        except Exception as e:
            logger.warning(
                f"[多因子] 额外因子获取失败 {factor_name} {symbol}: {e}"
            )
            return None

    # ═══════════════════════════════════════════════
    #  打分与权重
    # ═══════════════════════════════════════════════

    def _factor_direction(self, factor_name: str) -> int:
        """因子方向：基础因子查 FACTOR_DIRECTION，额外因子查配置"""
        if factor_name in self.FACTOR_DIRECTION:
            return self.FACTOR_DIRECTION[factor_name]
        cfg = self._extra_factor_configs.get(factor_name, {})
        return 1 if cfg.get("higher_better", True) else -1

    def _normalize_weights(
        self, active_factors: List[str],
    ) -> Dict[str, float]:
        """将激活因子的权重归一化到和为1"""
        weights: Dict[str, float] = {}
        total = 0.0
        for f in active_factors:
            w = self._base_factor_weights.get(f)
            if w is None:
                cfg = self._extra_factor_configs.get(f)
                w = float(cfg["weight"]) if cfg else 0.0
            if w is not None and w > 0:
                weights[f] = w
                total += w
        if total <= 0:
            return {}
        return {f: w / total for f, w in weights.items()}

    def _resolve_weights(
        self, active_factors: List[str],
    ) -> Dict[str, float]:
        """解析因子权重（F-02：IC 加权优先，回退默认/手工权重）

        ic_based_weights=True 且有对应 IC 档案 → IC 加权（方向校正 +
        下限约束 + 归一化），权重来源可追溯（档案样本区间/ICIR）；
        否则回退 _normalize_weights（默认/手工权重，与原行为一致）。
        """
        if self.ic_based_weights:
            ic_weights = self._ic_weighted_weights(active_factors)
            if ic_weights:
                logger.info(
                    f"[多因子] 使用 IC 加权权重: {ic_weights}（档案数="
                    f"{len(self._factor_profiles)}，下限={self.ic_weight_min}）"
                )
                return ic_weights
            logger.warning(
                "[多因子] IC 加权无可用档案/无正贡献，回退默认权重"
            )
        return self._normalize_weights(active_factors)

    def _ic_weighted_weights(
        self, active_factors: List[str],
    ) -> Dict[str, float]:
        """按 IC 档案计算权重（方向校正 + 下限约束 + 归一化）。

        仅对 active_factors 中已注入档案的因子计算；方向取策略方向
        （FACTOR_DIRECTION / 额外因子 higher_better），负向贡献因子权重为 0。
        """
        profiles: Dict[str, FactorProfile] = {
            f: self._factor_profiles[f] for f in active_factors
            if f in self._factor_profiles
        }
        if not profiles:
            return {}
        directions: Dict[str, int] = {
            f: self._factor_direction(f) for f in profiles
        }
        return ic_weighted_weights(
            profiles, min_weight=self.ic_weight_min, directions=directions)

    def _alloc_weights(
        self, new_pool: List[str], composite: Dict[str, float],
    ) -> Dict[str, float]:
        """调仓目标权重：等权 或 按综合得分加权（min-shift 保证非负）"""
        if self.equal_weight:
            w = 1.0 / len(new_pool)
            return {s: w for s in new_pool}
        comps = [composite[s] for s in new_pool]
        base = min(comps)
        shifted = [c - base + 1e-9 for c in comps]
        total = sum(shifted)
        return {s: sh / total for s, sh in zip(new_pool, shifted)}

    # ═══════════════════════════════════════════════
    #  数据工具（防未来 + 实盘无缝对接）
    # ═══════════════════════════════════════════════

    def _get_data_upto(
        self, context: StrategyContext, symbol: str, bar: Bar,
    ) -> Optional[pd.DataFrame]:
        """获取该标的截至当前bar的数据（按时间戳截断，防未来）

        回测中 context.data 可能是全量历史，截断后不含未来bar。
        实盘中 data 为增量同步，截断为无操作（当前bar即最新）。
        bar.timestamp<=0 时（无有效时间）不做截断，视为已增量数据。
        """
        df = context.get_data(symbol)
        if df is None or df.empty or '收盘价' not in df.columns:
            return None

        ts = float(getattr(bar, "timestamp", 0) or 0)
        if ts <= 0:
            return df

        try:
            idx = df.index
            if isinstance(idx, pd.DatetimeIndex):
                ts_dt = pd.Timestamp(ts, unit="s")
                df = df[df.index <= ts_dt]
            elif idx.dtype.kind in "iuf":
                # 数值索引（秒级时间戳）
                df = df[df.index <= ts]
            else:
                # 字符串等索引：尝试解析为时间
                parsed = pd.to_datetime(pd.Series(idx), errors="coerce")
                mask = parsed.notna().values
                if mask.any():
                    ts_dt = pd.Timestamp(ts, unit="s")
                    df = df[mask & (parsed.values <= ts_dt)]
        except Exception as e:
            logger.warning(f"[多因子] 数据截断失败 {symbol}: {e}")

        return df if df is not None and len(df) > 0 else None

    def _get_all_symbols(self, context: StrategyContext) -> List[str]:
        """获取所有可交易标的（context.data 中的标的）"""
        if context.data:
            return list(context.data.keys())
        return []

    def _get_latest_price(
        self, context: StrategyContext, symbol: str, bar: Bar,
        default_price: float,
    ) -> float:
        """获取某标的截至当前bar的最新收盘价（trailing，防未来）

        调仓时用各标的自身价格，而非当前bar价格；
        且用 _get_data_upto 截断，回测中不读取未来bar的收盘价。
        """
        df = self._get_data_upto(context, symbol, bar)
        # P1-17修复：判定改中文列「收盘价」（此前 'close' 恒False，跨标的取价失效）
        if df is not None and not df.empty and '收盘价' in df.columns:
            close = df['收盘价'].astype(float).dropna()
            if len(close) > 0:
                return float(close.iloc[-1])
        return default_price

    def _get_current_positions(
        self, context: StrategyContext,
    ) -> Dict[str, Optional[Position]]:
        """获取当前持仓字典"""
        positions: Dict[str, Optional[Position]] = {}
        if context.portfolio is None:
            return positions
        if hasattr(context.portfolio, 'positions'):
            pos_dict = context.portfolio.positions
            if isinstance(pos_dict, dict):
                return dict(pos_dict)
            try:
                for pos in pos_dict:
                    if hasattr(pos, 'symbol'):
                        positions[pos.symbol] = pos
            except TypeError:
                pass
        return positions

    # ═══════════════════════════════════════════════
    #  订单提交
    # ═══════════════════════════════════════════════

    # ═══════════════════════════════════════════════
    #  状态查询
    # ═══════════════════════════════════════════════

    def _save_preprocess_report(self, report: Dict[str, Any]) -> None:
        """将预处理检验报告落盘为 JSON（可选，失败不阻断调仓）。

        Args:
            report: 预处理报告 dict（_last_preprocess_report）。
        """
        if not self.preprocess_report_sink:
            return
        sink = os.path.abspath(self.preprocess_report_sink)
        os.makedirs(os.path.dirname(sink), exist_ok=True)
        with open(sink, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)

    def get_multi_factor_status(
        self, context: Optional[StrategyContext] = None,
    ) -> Dict[str, Any]:
        """获取多因子策略状态（供实盘监控/Web看板使用）"""
        last_report = self._last_preprocess_report
        preprocess_summary: Optional[Dict[str, Any]] = None
        if last_report:
            preprocess_summary = {
                "mode": last_report.get("mode"),
                "generated_at": last_report.get("generated_at"),
                "industry_map_size": last_report.get("industry_map_size"),
                "note": last_report.get("note"),
                "factors": [
                    {
                        "factor_name": r.get("factor_name"),
                        "method": r.get("method"),
                        "n_winsorized": r.get("n_winsorized"),
                        "exposure_t": r.get("exposure_t"),
                        "flags": r.get("flags"),
                    }
                    for r in last_report.get("factors", [])
                ],
            }
        return {
            "strategy": self.name,
            "momentum_period": self.momentum_period,
            "reversal_period": self.reversal_period,
            "volatility_period": self.volatility_period,
            "vol_price_period": self.vol_price_period,
            "top_n": self.top_n,
            "rebalance_days": self.rebalance_days,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "max_position_pct": self.max_position_pct,
            "max_positions": self.max_positions,
            "equal_weight": self.equal_weight,
            "zscore_mode": self.zscore_mode,
            "ts_roll_window": self.ts_roll_window,
            "min_score": self.min_score,
            "base_factor_weights": dict(self._base_factor_weights),
            "extra_factor_configs": dict(self._extra_factor_configs),
            "has_factor_provider": self.factor_provider is not None,
            "ic_based_weights": self.ic_based_weights,
            "ic_weight_min": self.ic_weight_min,
            "weight_source": (
                "ic_profile" if self.ic_based_weights and self._factor_profiles
                else "manual"
            ),
            "factor_profile_names": sorted(self._factor_profiles.keys()),
            "preprocess_mode": self.preprocess_mode,
            "industry_map_size": len(self.industry_map),
            "last_preprocess_report": preprocess_summary,
            "tradability": self.tradability,
            "tradability_rejected_total": self._tradability_stats["rejected"],
            "tradability_config": {
                "min_avg_amount": self._tradability_config.min_avg_amount,
                "lookback_days": self._tradability_config.lookback_days,
                "limit_pct": self._tradability_config.limit_pct,
                "filter_suspend": self._tradability_config.filter_suspend,
                "filter_liquidity": self._tradability_config.filter_liquidity,
                "filter_limit": self._tradability_config.filter_limit,
            },
            "current_pool": self.current_pool,
            "composite_scores": dict(self._composite_scores),
            "factor_snapshot": {
                sym: dict(vals) for sym, vals in self._factor_snapshot.items()
            },
            "rebalance_count": self._rebalance_count,
            "signal_count": self._signal_count,
            "stop_loss_count": self._stop_loss_count,
            "take_profit_count": self._take_profit_count,
        }

    def on_stop(self, context: StrategyContext) -> None:
        """停止钩子：输出调仓统计"""
        logger.info(
            f"多因子策略停止: 调仓{self._rebalance_count}次, "
            f"信号总数={self._signal_count}, "
            f"止损{self._stop_loss_count}次/止盈{self._take_profit_count}次, "
            f"当前池={self.current_pool}"
        )
        super().on_stop(context)

    # ═══════════════════════════════════════════════
    #  第7项：双套信号接口（mixed / vectorized）
    #  单标的近似口径（docstring 注明）：
    #    横截面 z-score + top_n 选股依赖全市场数据，mixed/vectorized
    #    按标的逐 bar 调用无法复现；此处退化为该标的自身动量/低波动
    #    滚动 z-score 综合打分，调仓日 得分 >= vec_score_threshold → 持有。
    # ═══════════════════════════════════════════════

    def _vec_score_series(self, df: pd.DataFrame) -> np.ndarray:
        """计算该标的的单标的综合得分序列（自身滚动 z-score，防未来）。

        得分 = w_momentum*z_momentum + w_low_vol*(-z_low_vol)
        低波动方向 -1（越小越好）。权重取 _base_factor_weights 中对应因子。

        Returns:
            float 数组（长度 = len(df)），数据不足段为 0（中性）。
        """
        n = len(df)
        if n < self._min_factor_bars:
            return np.zeros(n)
        close = df["收盘价"].astype(float)
        w_mom = float(self._base_factor_weights.get("momentum", 0.0))
        w_lv = float(self._base_factor_weights.get("low_vol", 0.0))
        mom = close.pct_change(self.momentum_period)
        rets = close.pct_change()
        lv = rets.rolling(
            self.volatility_period, min_periods=self.volatility_period,
        ).std(ddof=1)

        def _z(series: pd.Series) -> pd.Series:
            m = series.rolling(
                self.ts_roll_window, min_periods=self.ts_roll_window,
            ).mean()
            std = series.rolling(
                self.ts_roll_window, min_periods=self.ts_roll_window,
            ).std(ddof=1)
            return (series - m) / std.replace(0, np.nan)

        zm = _z(mom)
        zl = _z(lv)
        score = (w_mom * zm - w_lv * zl).fillna(0.0).to_numpy(dtype=float)
        return score

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        """mixed 引擎目标持仓：调仓日单标的得分阈值判断 + 止损止盈。"""
        symbol = bar.symbol
        # 止损止盈（持仓时）
        if position is not None and position.quantity > 0:
            if self.check_stop_loss(position, bar.close, self.stop_loss_pct):
                return 0.0
            if self.check_take_profit(position, bar.close, self.take_profit_pct):
                return 0.0
        # 调仓日：重新判断该标的综合得分（单标的近似，非横截面）
        if self._should_rebalance(context, self.rebalance_days):
            df = self._get_data_upto(context, symbol, bar)
            if df is not None and len(df) >= self._min_factor_bars:
                score = self._vec_score_series(df)
                if score[-1] >= self.vec_score_threshold:
                    return self._target_shares_from_capital(
                        self.capital_base, float(bar.close), self.max_position_pct,
                    )
            return 0.0
        # 非调仓日：保持当前持仓
        if position is not None:
            return float(position.quantity)
        return 0.0

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """vectorized 引擎信号：目标持仓股数数组（调仓日得分阈值状态机）。"""
        n = len(data)
        if n < self._min_factor_bars:
            return np.zeros(n)
        score = self._vec_score_series(data)
        close = data["收盘价"].astype(float).to_numpy(dtype=float)
        target = np.zeros(n)
        holding = False
        full_shares = 0.0
        cost = 0.0
        for i in range(n):
            # 本行使用的数据截至 t 时刻（第 i 根，含第 i 根）
            c = float(close[i])
            if holding:
                exit_flag = False
                if cost > 0 and c / cost - 1.0 <= -self.stop_loss_pct:
                    exit_flag = True
                elif cost > 0 and c / cost - 1.0 >= self.take_profit_pct:
                    exit_flag = True
                if exit_flag:
                    holding = False
                    full_shares = 0.0
            # 调仓日：重新判断（i 从 0 起每 rebalance_days 根）
            if i % self.rebalance_days == 0:
                if score[i] >= self.vec_score_threshold:
                    if not holding:
                        holding = True
                        full_shares = self._target_shares_from_capital(
                            self.capital_base, c, self.max_position_pct,
                        )
                        cost = c
                else:
                    holding = False
                    full_shares = 0.0
            target[i] = full_shares
        return target


if __name__ == "__main__":
    # 自测 Demo：双接口 + 三引擎跑通
    import numpy as np
    import pandas as pd

    from myquant.backtest.engine import run_backtest
    from myquant.models.backtest import BacktestConfig

    n = 120
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(13)
    close = 10.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.011, n)))
    df = pd.DataFrame(
        {"open": close * 0.995, "high": close * 1.02, "low": close * 0.98,
         "close": close, "volume": np.full(n, 1e6)},
        index=idx,
    )
    data = {"600519.SH": df}
    config = BacktestConfig(
        symbols=["600519.SH"], start_date="2024-01-01", end_date="2024-06-30",
        initial_capital=1_000_000.0,
    )
    st = MultiFactorStrategy(vec_score_threshold=0.0, capital_base=1_000_000.0)
    assert st.supports_mode("mixed") and st.supports_mode("vectorized")
    arr = st.generate_signals_vectorized(df)
    assert len(arr) == n and float(arr[-1]) >= 0.0
    for mode in ("event", "mixed", "vectorized"):
        r = run_backtest(config, st, data=data, mode=mode)
        assert r.is_success, f"multi_factor/{mode} 失败: {r.status}"
        print(
            f"  MultiFactorStrategy [{mode}] 净值点数={len(r.equity_curve)} "
            f"成交={len(r.trades)} 期末净值={r.final_equity:.4f}"
        )
    print("multi_factor_strategy 双接口自测通过 ✅")

