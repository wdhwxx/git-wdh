# 文件名：myquant/backtest/metrics.py
"""
backtest/metrics.py - 绩效指标计算器
======================================
计算回测/实盘的绩效指标，输出 PerformanceMetrics 对象。

设计原则（规则2：为实盘无缝对接准备）:
  - MetricsCalculator 回测/实盘共用，输入净值曲线和成交记录即可计算
  - 实盘运行时可定期调用 update() 增量计算最新指标
  - 所有指标基于历史数据计算，不涉及未来信息

指标清单（结构图要求）:
  - 夏普比率 (Sharpe Ratio)
  - 最大回撤 (Max Drawdown)
  - 胜率 (Win Rate)
  - Calmar比率 (Calmar Ratio)
  - 索提诺比率 (Sortino Ratio)
  - 额外：年化收益、年化波动、盈亏比、Alpha/Beta、VaR/CVaR等

防未来函数（规则4）:
  - 所有指标基于已实现的净值和成交记录计算
  - 不使用未来数据
  - 滚动窗口指标只使用截至当前时点的数据
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.models.backtest import (
    EquityPoint,
    PerformanceMetrics,
    TradeRecord,
)

logger = get_logger(__name__)


class MetricsCalculator:
    """
    绩效指标计算器

    输入净值曲线和成交记录，输出完整的 PerformanceMetrics。

    使用方式:
        calc = MetricsCalculator(risk_free_rate=0.02)
        metrics = calc.calculate(equity_curve, trades, benchmark_curve)
        print(metrics.summary())
    """

    TRADING_DAYS_PER_YEAR = 252

    def __init__(self, risk_free_rate: float = 0.02):
        """
        初始化绩效计算器

        参数:
            risk_free_rate: 无风险利率（年化），默认2%
        """
        self.risk_free_rate = risk_free_rate
        self._daily_rf = risk_free_rate / self.TRADING_DAYS_PER_YEAR
        logger.info(f"绩效计算器初始化: 无风险利率={risk_free_rate:.2%}")

    # ------------------------------------------------------------------
    # 主计算入口
    # ------------------------------------------------------------------

    def calculate(
        self,
        equity_curve: List[EquityPoint],
        trades: List[TradeRecord],
        benchmark_curve: Optional[List[EquityPoint]] = None,
        start_date: str = "",
        end_date: str = "",
    ) -> PerformanceMetrics:
        """
        计算全部绩效指标

        参数:
            equity_curve: 净值曲线
            trades: 成交记录
            benchmark_curve: 基准净值曲线（可选）
            start_date: 起始日期
            end_date: 结束日期

        返回:
            PerformanceMetrics: 完整绩效指标
        """
        metrics = PerformanceMetrics()
        metrics.start_date = start_date
        metrics.end_date = end_date

        if not equity_curve:
            logger.warning("净值曲线为空，返回零指标")
            return metrics

        # 转换为DataFrame便于计算
        eq_df = self._equity_to_df(equity_curve)
        returns = eq_df['equity'].pct_change().dropna()
        metrics.trading_days = len(eq_df)

        # 1. 收益指标
        self._calc_return_metrics(metrics, eq_df, returns)

        # 2. 风险指标
        self._calc_risk_metrics(metrics, eq_df, returns)

        # 3. 风险调整收益
        self._calc_risk_adjusted_metrics(metrics, returns)

        # 4. 交易统计
        self._calc_trade_metrics(metrics, trades)

        # 5. 成本
        self._calc_cost_metrics(metrics, trades, eq_df)

        # 6. 基准对比（Alpha/Beta/信息比率）
        if benchmark_curve:
            bench_df = self._equity_to_df(benchmark_curve)
            self._calc_benchmark_metrics(metrics, returns, bench_df)
        else:
            # KP-P2-04：无基准曲线 → 显式置 None，不伪造 0.0 基准指标
            metrics.benchmark_return = None
            metrics.excess_return = None
            metrics.alpha = None
            metrics.beta = None

        # 7. 收益分布（VaR/CVaR/偏度/峰度）
        self._calc_distribution_metrics(metrics, returns)

        logger.info(
            f"绩效计算完成: 总收益={metrics.total_return:.2%}, "
            f"夏普={metrics.sharpe_ratio:.2f}, "
            f"最大回撤={metrics.max_drawdown:.2%}, "
            f"胜率={metrics.win_rate:.2%}"
        )
        return metrics

    # ------------------------------------------------------------------
    # 收益指标
    # ------------------------------------------------------------------

    def _calc_return_metrics(
        self, metrics: PerformanceMetrics, eq_df: pd.DataFrame, returns: pd.Series,
    ) -> None:
        """计算收益指标"""
        final_equity = eq_df['equity'].iloc[-1]
        initial_equity = eq_df['equity'].iloc[0]
        metrics.total_return = final_equity / initial_equity - 1.0

        n_days = len(eq_df)
        if n_days > 0:
            metrics.annualized_return = (
                (1 + metrics.total_return) ** (self.TRADING_DAYS_PER_YEAR / n_days) - 1
            )

    # ------------------------------------------------------------------
    # 风险指标
    # ------------------------------------------------------------------

    def _calc_risk_metrics(
        self, metrics: PerformanceMetrics, eq_df: pd.DataFrame, returns: pd.Series,
    ) -> None:
        """计算风险指标"""
        if len(returns) > 0:
            metrics.annualized_volatility = returns.std() * np.sqrt(self.TRADING_DAYS_PER_YEAR)
            # 下行波动率（只计算负收益的波动）
            negative_returns = returns[returns < 0]
            if len(negative_returns) > 0:
                metrics.downside_volatility = (
                    negative_returns.std() * np.sqrt(self.TRADING_DAYS_PER_YEAR)
                )

        # 最大回撤
        cummax = eq_df['equity'].cummax()
        drawdown = (eq_df['equity'] - cummax) / cummax
        metrics.max_drawdown = abs(drawdown.min())

        # 最大回撤持续天数
        in_drawdown = drawdown < 0
        max_duration = 0
        current_duration = 0
        for in_dd in in_drawdown:
            if in_dd:
                current_duration += 1
                max_duration = max(max_duration, current_duration)
            else:
                current_duration = 0
        metrics.max_drawdown_duration = max_duration

    # ------------------------------------------------------------------
    # 风险调整收益
    # ------------------------------------------------------------------

    def _calc_risk_adjusted_metrics(
        self, metrics: PerformanceMetrics, returns: pd.Series,
    ) -> None:
        """计算风险调整收益指标"""
        if len(returns) == 0:
            return

        excess_returns = returns - self._daily_rf

        # 夏普比率
        if returns.std() > 0:
            metrics.sharpe_ratio = (
                excess_returns.mean() / returns.std() * np.sqrt(self.TRADING_DAYS_PER_YEAR)
            )

        # 索提诺比率（用下行波动率）
        negative_returns = returns[returns < 0]
        if len(negative_returns) > 0 and negative_returns.std() > 0:
            metrics.sortino_ratio = (
                excess_returns.mean() / negative_returns.std() * np.sqrt(self.TRADING_DAYS_PER_YEAR)
            )

        # Calmar比率 = 年化收益 / 最大回撤
        if metrics.max_drawdown > 0:
            metrics.calmar_ratio = metrics.annualized_return / metrics.max_drawdown

    # ------------------------------------------------------------------
    # 交易统计
    # ------------------------------------------------------------------

    def _calc_trade_metrics(
        self, metrics: PerformanceMetrics, trades: List[TradeRecord],
    ) -> None:
        """计算交易统计指标"""
        # 只统计有盈亏的平仓交易
        # M6修复：平仓交易口径 = SELL 且经记录器 FIFO 配对成功（holding_days>0）。
        #   - 未配对卖出（无持仓成本，holding_days=0）不构成平仓交易，不计入统计，
        #     消除原口径下交易次数虚高、胜率失真的问题；
        #   - 保本平仓（pnl=0 且已配对）正常计入，归为亏损样本（与原口径一致）；
        #   - 买入记录不产生平仓盈亏（记录器只在 SELL 时写 pnl），天然排除。
        closed_trades = [t for t in trades if t.side == "SELL" and t.holding_days > 0]
        metrics.total_trades = len(closed_trades)

        if metrics.total_trades == 0:
            return

        winning = [t for t in closed_trades if t.pnl > 0]
        losing = [t for t in closed_trades if t.pnl <= 0]
        metrics.winning_trades = len(winning)
        metrics.losing_trades = len(losing)
        metrics.win_rate = metrics.winning_trades / metrics.total_trades

        if winning:
            metrics.avg_win = np.mean([t.pnl for t in winning])
        if losing:
            metrics.avg_loss = np.mean([t.pnl for t in losing])

        # 盈亏比（全盈利时为无穷大，表示无亏损交易）
        if metrics.avg_loss != 0:
            metrics.profit_loss_ratio = abs(metrics.avg_win / metrics.avg_loss)
        elif metrics.avg_win > 0:
            # 全部交易盈利，无亏损样本，盈亏比趋于无穷
            metrics.profit_loss_ratio = float('inf')

        # 最大连胜/连亏
        max_win_streak = 0
        max_loss_streak = 0
        current_win = 0
        current_loss = 0
        for t in closed_trades:
            if t.pnl > 0:
                current_win += 1
                current_loss = 0
                max_win_streak = max(max_win_streak, current_win)
            else:
                current_loss += 1
                current_win = 0
                max_loss_streak = max(max_loss_streak, current_loss)
        metrics.max_consecutive_wins = max_win_streak
        metrics.max_consecutive_losses = max_loss_streak

    # ------------------------------------------------------------------
    # 成本
    # ------------------------------------------------------------------

    def _calc_cost_metrics(
        self, metrics: PerformanceMetrics, trades: List[TradeRecord], eq_df: pd.DataFrame,
    ) -> None:
        """计算成本指标"""
        metrics.total_commission = sum(t.commission for t in trades)
        metrics.total_slippage = sum(t.slippage for t in trades)

        # 换手率 = 总成交金额 / 平均资产
        # 注意：eq_df['equity'] 是净值比率（≈1.0），不是绝对金额；
        # 必须用 cash + position_value（绝对金额）作为平均资产，否则换手率被放大约100万倍。
        total_notional = sum(t.notional for t in trades)
        if len(eq_df) > 0:
            avg_total_asset = (eq_df['cash'] + eq_df['position_value']).mean()
        else:
            avg_total_asset = 1.0
        if avg_total_asset > 0:
            metrics.turnover = total_notional / avg_total_asset

    # ------------------------------------------------------------------
    # 基准对比
    # ------------------------------------------------------------------

    def _calc_benchmark_metrics(
        self, metrics: PerformanceMetrics, returns: pd.Series, bench_df: pd.DataFrame,
    ) -> None:
        """计算基准对比指标（Alpha/Beta/信息比率）"""
        bench_returns = bench_df['equity'].pct_change().dropna()

        # 对齐日期
        common_idx = returns.index.intersection(bench_returns.index)
        if len(common_idx) < 2:
            return

        ret = returns.loc[common_idx]
        bench = bench_returns.loc[common_idx]

        metrics.benchmark_return = bench_df['equity'].iloc[-1] / bench_df['equity'].iloc[0] - 1
        metrics.excess_return = metrics.total_return - metrics.benchmark_return

        # Beta = Cov(策略, 基准) / Var(基准)
        if bench.var() > 0:
            metrics.beta = np.cov(ret, bench)[0, 1] / bench.var()

        # Alpha = 策略年化收益 - 无风险利率 - Beta * (基准年化收益 - 无风险利率)
        n_days = len(common_idx)
        bench_annual = (1 + metrics.benchmark_return) ** (self.TRADING_DAYS_PER_YEAR / max(n_days, 1)) - 1
        metrics.alpha = (
            metrics.annualized_return - self.risk_free_rate
            - metrics.beta * (bench_annual - self.risk_free_rate)
        )

        # 信息比率 = 超额收益均值 / 跟踪误差
        active_return = ret - bench
        if active_return.std() > 0:
            metrics.tracking_error = active_return.std() * np.sqrt(self.TRADING_DAYS_PER_YEAR)
            metrics.information_ratio = (
                active_return.mean() / active_return.std() * np.sqrt(self.TRADING_DAYS_PER_YEAR)
            )

    # ------------------------------------------------------------------
    # 收益分布
    # ------------------------------------------------------------------

    def _calc_distribution_metrics(
        self, metrics: PerformanceMetrics, returns: pd.Series,
    ) -> None:
        """计算收益分布指标"""
        if len(returns) < 2:
            return

        # VaR(95%) = 5%分位数的负值
        metrics.var_95 = abs(np.percentile(returns, 5))

        # CVaR(95%) = 超过VaR部分的平均损失
        var_threshold = np.percentile(returns, 5)
        tail_losses = returns[returns <= var_threshold]
        if len(tail_losses) > 0:
            metrics.cvar_95 = abs(tail_losses.mean())

        # 偏度和峰度
        if returns.std() > 0:
            metrics.skewness = float(((returns - returns.mean()) ** 3).mean() / returns.std() ** 3)
            metrics.kurtosis = float(((returns - returns.mean()) ** 4).mean() / returns.std() ** 4 - 3)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _equity_to_df(equity_curve: List[EquityPoint]) -> pd.DataFrame:
        """将净值曲线列表转换为DataFrame

        注意：回测引擎每根bar（每只股票每天）都会记录净值，
        因此同一交易日会有多条记录。此处按日期去重，
        保留每日最后一条（即当日收盘净值），确保时间维度指标正确。
        实盘场景下record_equity可能按tick调用，同样适用此去重逻辑。

        M1修复（去重索引三级回退）:
          EquityPoint.date 默认为空字符串，调用方未填 date 时（如外部
          直接构造净值曲线），原实现会把全部数据点折叠成一行，导致
          total_return 等指标恒为0。现按三级回退构建去重索引：
            1. date 非空      → 按日期去重（日线场景，保留每日收盘净值）；
                                分钟级日期自动归一化为日期部分（同日多bar
                                仍按日聚合，与日线口径一致）
            2. date 空、时间戳有效(>0) → 从时间戳推断日期去重
            3. 时间戳也无效    → 顺序索引逐点保留（不做错误折叠）
        全部回退路径均只使用净值点自身的"已发生"字段，不引入未来数据。
        """
        if not equity_curve:
            return pd.DataFrame()

        # 第1级：优先使用数据点自带 date；空则从时间戳推断（第2级）
        dates = []
        any_valid = False
        for ep in equity_curve:
            d = str(ep.date or "").strip()
            if d:
                any_valid = True
                # 分钟级日期归一化："2024-01-15 10:30:00" → "2024-01-15"
                if len(d) > 10:
                    d = d[:10]
                dates.append(d)
            elif ep.timestamp > 0:
                try:
                    dates.append(
                        datetime.fromtimestamp(float(ep.timestamp)).strftime("%Y-%m-%d")
                    )
                    any_valid = True
                except (OverflowError, OSError, ValueError):
                    dates.append("")
            else:
                dates.append("")

        data = []
        for ep, d in zip(equity_curve, dates):
            data.append({
                'timestamp': ep.timestamp,
                'date': d,
                'equity': ep.equity,
                'cash': ep.cash,
                'position_value': ep.position_value,
                'drawdown': ep.drawdown,
            })
        df = pd.DataFrame(data)

        if not any_valid:
            # 第3级兜底：无有效日期/时间戳 → 顺序索引逐点保留，不做折叠
            df.index = pd.RangeIndex(len(df))
            return df

        df = df.set_index('date')
        # 按日期去重，保留每日最后一条（当日收盘净值）
        df = df[~df.index.duplicated(keep='last')]
        return df

    def incremental_update(
        self, metrics: PerformanceMetrics, new_point: EquityPoint,
    ) -> PerformanceMetrics:
        """
        增量更新指标（实盘实时监控用）

        参数:
            metrics: 当前指标
            new_point: 新的净值数据点

        返回:
            更新后的指标
        """
        # 简化实现：实盘场景下可维护内部状态增量计算
        # 当前版本返回原指标，实盘对接时可扩展
        logger.debug(f"增量更新指标: equity={new_point.equity:.4f}")
        return metrics
