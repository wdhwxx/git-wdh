# 文件名：myquant/backtest/monte_carlo.py
"""
backtest/monte_carlo.py - 蒙特卡洛模拟分析
==========================================
基于历史日收益的统计模拟，评估策略收益路径的分布特性（回测报告增强·第4项A）。

核心能力:
  - bootstrap 重采样：从历史日收益中有放回随机抽取，重排模拟 N 条净值路径
  - 收益路径分布：P5/P25/P50/P75/P95 分位带（供 HTML/PDF 报告画分位带图）
  - 最终收益分布：N 条路径的期末收益分位数 + 直方图数据
  - 最大回撤分布：N 条路径的最大回撤分位数 + 95% 置信区间 + 直方图数据
  - 破产概率：期末资产低于初始资金×(1-阈值) 的路径占比

防未来函数（铁律）:
  - 蒙特卡洛只重采样【已发生的】历史收益序列，不预测未来价格
  - 每条模拟路径 = 历史日收益的有放回随机抽取（历史模拟法），非未来函数
  - 输入仅用已实现的净值曲线（List[EquityPoint]），无任何未来数据
  - 源码无预测方法/无硬编码未来日期

设计原则（为实盘无缝对接准备）:
  - 输入净值曲线（List[EquityPoint]），与 metrics.py 同接口，回测/实盘共用
  - 纯 numpy 向量化实现，无外部依赖（numpy/pandas 即可）
  - seed 可固定 → 完全可复现（回测报告默认固定；实盘监控如需对齐可传同一 seed）
  - 返回 MonteCarloResult 数据类，to_dict()/summary() 供报告/监控直接使用

使用方式:
    from myquant.backtest.monte_carlo import MonteCarloAnalyzer
    analyzer = MonteCarloAnalyzer(n_simulations=2000, horizon_days=252, seed=42)
    result = analyzer.analyze(equity_curve)
    print(result.summary())
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.models.backtest import EquityPoint

logger = get_logger(__name__)


# ═══════════════════════════════════════════════
#  MonteCarloResult - 蒙特卡洛结果容器
# ═══════════════════════════════════════════════

@dataclass
class MonteCarloResult:
    """
    蒙特卡洛模拟结果容器

    包含最终收益分布、最大回撤分布、分位路径带与破产概率，
    供 HTML/PDF 报告与实盘监控直接使用。

    字段说明:
      - final_returns      : N 条路径的期末收益数组（直方图数据）
      - max_drawdowns      : N 条路径的最大回撤数组（直方图数据）
      - quantile_equity    : 分位净值路径矩阵，形状 (len(percentiles), horizon_days+1)
      - quantile_percentiles: quantile_equity 对应的分位列表
    """

    n_simulations: int = 0
    horizon_days: int = 0
    seed: Optional[int] = None
    initial_capital: float = 1_000_000.0
    ruin_threshold: float = 0.5          # 破产阈值：期末资产 < 初始×(1-阈值) 视为破产

    # 最终收益分布
    final_returns: np.ndarray = field(default_factory=lambda: np.array([]))
    median_final_return: float = 0.0
    mean_final_return: float = 0.0
    p5_final_return: float = 0.0
    p25_final_return: float = 0.0
    p75_final_return: float = 0.0
    p95_final_return: float = 0.0
    prob_negative: float = 0.0           # 期末亏损概率

    # 最大回撤分布
    max_drawdowns: np.ndarray = field(default_factory=lambda: np.array([]))
    median_max_drawdown: float = 0.0
    p95_max_drawdown: float = 0.0        # 95%分位回撤（悲观情景）
    max_drawdown_ci_low: float = 0.0     # 95%置信区间下界（5%分位）
    max_drawdown_ci_high: float = 0.0    # 95%置信区间上界（95%分位）

    # 破产概率
    prob_ruin: float = 0.0

    # 分位路径带
    quantile_equity: np.ndarray = field(default_factory=lambda: np.array([]))
    quantile_percentiles: List[float] = field(default_factory=lambda: [5.0, 25.0, 50.0, 75.0, 95.0])

    # 元数据
    historical_days: int = 0             # 用于重采样的历史收益天数
    daily_volatility: float = 0.0        # 历史日收益标准差
    skipped: bool = False                # 是否被跳过（skip 开关，True=未执行模拟）

    # ── 序列化 ──
    def to_dict(self) -> Dict[str, object]:
        """导出为字典（JSON 安全，供 API/监控）"""
        return {
            "配置": {
                "模拟次数": self.n_simulations,
                "模拟期数(交易日)": self.horizon_days,
                "随机种子": self.seed,
                "初始资金": round(self.initial_capital, 2),
                "破产阈值": f"{self.ruin_threshold:.0%}",
                "历史收益天数": self.historical_days,
                "已跳过": "是" if self.skipped else "否",
            },
            "最终收益分布": {
                "中位数收益": f"{self.median_final_return:.4%}",
                "均值收益": f"{self.mean_final_return:.4%}",
                "P5": f"{self.p5_final_return:.4%}",
                "P25": f"{self.p25_final_return:.4%}",
                "P75": f"{self.p75_final_return:.4%}",
                "P95": f"{self.p95_final_return:.4%}",
                "亏损概率": f"{self.prob_negative:.2%}",
            },
            "最大回撤分布": {
                "中位最大回撤": f"{self.median_max_drawdown:.4%}",
                "P95最大回撤": f"{self.p95_max_drawdown:.4%}",
                "95%CI": [
                    f"{self.max_drawdown_ci_low:.4%}",
                    f"{self.max_drawdown_ci_high:.4%}",
                ],
            },
            "风险": {
                "破产概率": f"{self.prob_ruin:.4%}",
                "历史日波动率": f"{self.daily_volatility:.4%}",
            },
        }

    def summary(self) -> str:
        """格式化摘要输出"""
        if self.skipped:
            return (
                "╔══════════════════════════════════════════════╗\n"
                "║     蒙 特 卡 洛 模 拟 （已 跳 过 SKIP）        ║\n"
                "╚══════════════════════════════════════════════╝"
            )
        lines = [
            "╔══════════════════════════════════════════════╗",
            "║         蒙 特 卡 洛 模 拟 报 告               ║",
            "╠══════════════════════════════════════════════╣",
            f"║ 模拟次数:     {self.n_simulations:>10d}                  ║",
            f"║ 模拟期数:     {self.horizon_days:>10d}天                 ║",
            f"║ 中位数收益:   {self.median_final_return:>10.4%}                ║",
            f"║ 均值收益:     {self.mean_final_return:>10.4%}                ║",
            f"║ P5收益:       {self.p5_final_return:>10.4%}                ║",
            f"║ P95收益:      {self.p95_final_return:>10.4%}                ║",
            f"║ 亏损概率:     {self.prob_negative:>10.2%}                ║",
            f"║ 中位最大回撤: {self.median_max_drawdown:>10.4%}                ║",
            f"║ P95最大回撤:  {self.p95_max_drawdown:>10.4%}                ║",
            f"║ 回撤95%CI:    {self.max_drawdown_ci_low:>7.4%} ~ {self.max_drawdown_ci_high:.4%}         ║",
            f"║ 破产概率:     {self.prob_ruin:>10.4%}                ║",
            "╚══════════════════════════════════════════════╝",
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        status = "SKIP" if self.skipped else "DONE"
        return (
            f"MonteCarloResult({status}, n_sim={self.n_simulations}, "
            f"median_ret={self.median_final_return:.2%}, "
            f"median_maxdd={self.median_max_drawdown:.2%}, "
            f"ruin_p={self.prob_ruin:.2%})"
        )


# ═══════════════════════════════════════════════
#  MonteCarloAnalyzer - 蒙特卡洛分析器
# ═══════════════════════════════════════════════

class MonteCarloAnalyzer:
    """
    蒙特卡洛模拟分析器

    基于历史日收益做 bootstrap 有放回重采样，模拟多条未来净值路径，
    输出收益/回撤分布与破产概率。

    参数:
        n_simulations: 模拟路径条数（默认2000，报告精度与速度平衡）
        horizon_days:  模拟期数（交易日，默认252≈1年）
        seed:          随机种子（None=真随机；固定则完全可复现）
        initial_capital: 初始资金（用于破产概率计算，默认100万）
        ruin_threshold: 破产阈值（默认0.5，期末资产<初始×(1-0.5)=50万视为破产）
        skip:          skip开关（True=analyze 直接返回 skipped 空结果，不执行模拟）。
                       用于快速回测/参数扫描模式，配置层面跳过蒙特卡洛以节省算力。
    """

    def __init__(
        self,
        n_simulations: int = 2000,
        horizon_days: int = 252,
        seed: Optional[int] = None,
        initial_capital: float = 1_000_000.0,
        ruin_threshold: float = 0.5,
        skip: bool = False,
    ):
        # 参数校验
        if not isinstance(skip, bool):
            raise TypeError(f"skip 必须为 bool，收到: {type(skip).__name__}")
        if n_simulations < 1:
            raise ValueError(f"模拟次数({n_simulations})必须>=1")
        if horizon_days < 1:
            raise ValueError(f"模拟期数({horizon_days})必须>=1")
        if not 0 < ruin_threshold <= 1.0:
            raise ValueError(f"破产阈值({ruin_threshold})必须在(0,1]")
        if initial_capital <= 0:
            raise ValueError(f"初始资金({initial_capital})必须>0")

        self.n_simulations = n_simulations
        self.horizon_days = horizon_days
        self.seed = seed
        self.initial_capital = initial_capital
        self.ruin_threshold = ruin_threshold
        self.skip = skip

        logger.info(
            f"蒙特卡洛分析器初始化: sim={n_simulations}, horizon={horizon_days}天, "
            f"seed={seed}, 破产阈值={ruin_threshold:.0%}"
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def set_skip(self, enabled: bool) -> None:
        """设置/取消蒙特卡洛跳过开关。

        Args:
            enabled: True=跳过（analyze 直接返回 skipped 空结果，不执行模拟）；
                     False=正常执行模拟。

        Raises:
            TypeError: enabled 不是 bool 时抛出。
        """
        if not isinstance(enabled, bool):
            raise TypeError(f"enabled 必须为 bool，收到: {type(enabled).__name__}")
        self.skip = enabled
        logger.info("蒙特卡洛 skip 开关已设置: %s", enabled)

    def analyze(self, equity_curve: Sequence[EquityPoint]) -> MonteCarloResult:
        """
        执行蒙特卡洛模拟

        参数:
            equity_curve: 历史净值曲线（List[EquityPoint]）

        返回:
            MonteCarloResult: 模拟结果
        """
        if self.skip:
            logger.info("蒙特卡洛已跳过（skip=True），返回空结果")
            return MonteCarloResult(
                n_simulations=self.n_simulations,
                horizon_days=self.horizon_days,
                seed=self.seed,
                initial_capital=self.initial_capital,
                ruin_threshold=self.ruin_threshold,
                skipped=True,
            )

        if not equity_curve or len(equity_curve) < 2:
            logger.warning("净值曲线为空或不足2个点，返回空结果")
            return MonteCarloResult(
                n_simulations=self.n_simulations,
                horizon_days=self.horizon_days,
                seed=self.seed,
                initial_capital=self.initial_capital,
                ruin_threshold=self.ruin_threshold,
            )

        # 1. 提取历史日收益（已实现数据，pct_change 不含未来）
        eq_df = self._equity_to_series(equity_curve)
        returns = eq_df.pct_change().dropna()
        if len(returns) < 1:
            logger.warning("历史日收益为空，返回空结果")
            return MonteCarloResult(
                n_simulations=self.n_simulations,
                horizon_days=self.horizon_days,
                seed=self.seed,
                initial_capital=self.initial_capital,
                ruin_threshold=self.ruin_threshold,
            )

        returns_arr = returns.to_numpy(dtype=float)
        rng = np.random.default_rng(self.seed)

        # 2. bootstrap 有放回重采样：形状 (n_simulations, horizon_days)
        idx = rng.integers(0, len(returns_arr), size=(self.n_simulations, self.horizon_days))
        sim_returns = returns_arr[idx]

        # 3. 净值路径：起点1.0，逐期累乘 (1+收益)
        path_equity = np.cumprod(1.0 + sim_returns, axis=1)
        path_equity = np.hstack([np.ones((self.n_simulations, 1)), path_equity])

        # 4. 最终收益
        final_returns = path_equity[:, -1] - 1.0

        # 5. 最大回撤（每条路径）
        cummax = np.maximum.accumulate(path_equity, axis=1)
        drawdowns = (path_equity - cummax) / cummax
        max_drawdowns = -drawdowns.min(axis=1)

        # 6. 汇总分布
        result = MonteCarloResult(
            n_simulations=self.n_simulations,
            horizon_days=self.horizon_days,
            seed=self.seed,
            initial_capital=self.initial_capital,
            ruin_threshold=self.ruin_threshold,
            final_returns=final_returns,
            median_final_return=float(np.median(final_returns)),
            mean_final_return=float(np.mean(final_returns)),
            p5_final_return=float(np.percentile(final_returns, 5)),
            p25_final_return=float(np.percentile(final_returns, 25)),
            p75_final_return=float(np.percentile(final_returns, 75)),
            p95_final_return=float(np.percentile(final_returns, 95)),
            prob_negative=float(np.mean(final_returns < 0)),
            max_drawdowns=max_drawdowns,
            median_max_drawdown=float(np.median(max_drawdowns)),
            p95_max_drawdown=float(np.percentile(max_drawdowns, 95)),
            max_drawdown_ci_low=float(np.percentile(max_drawdowns, 5)),
            max_drawdown_ci_high=float(np.percentile(max_drawdowns, 95)),
            prob_ruin=float(np.mean(path_equity[:, -1] < (1.0 - self.ruin_threshold))),
            quantile_equity=np.percentile(
                path_equity, self._default_percentiles(), axis=0,
            ),
            historical_days=int(len(returns)),
            daily_volatility=float(returns.std()),
        )
        logger.info(
            f"蒙特卡洛模拟完成: sim={self.n_simulations}, "
            f"中位收益={result.median_final_return:.2%}, "
            f"中位回撤={result.median_max_drawdown:.2%}, "
            f"破产概率={result.prob_ruin:.2%}"
        )
        return result

    # ------------------------------------------------------------------
    # 分位路径带（自定义分位）
    # ------------------------------------------------------------------

    def get_quantile_paths(
        self,
        equity_curve: Sequence[EquityPoint],
        percentiles: Sequence[float] = (5.0, 25.0, 50.0, 75.0, 95.0),
    ) -> Tuple[np.ndarray, List[float]]:
        """
        计算指定分位的净值路径带

        参数:
            equity_curve: 历史净值曲线
            percentiles: 分位列表（默认 P5/P25/P50/P75/P95）

        返回:
            (quantile_equity, percentiles): 分位路径矩阵 + 对应分位列表
        """
        pcts = sorted(p for p in percentiles)
        result = self.analyze(equity_curve)
        return result.quantile_equity, result.quantile_percentiles

    @staticmethod
    def _default_percentiles() -> List[float]:
        """默认分位"""
        return [5.0, 25.0, 50.0, 75.0, 95.0]

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _equity_to_series(equity_curve: Sequence[EquityPoint]) -> pd.Series:
        """净值曲线 → 净值 Series（索引=date，值=equity）"""
        data = {ep.date or str(ep.timestamp): float(ep.equity) for ep in equity_curve}
        series = pd.Series(data)
        series.index = pd.to_datetime(series.index)
        series = series.sort_index()
        return series.astype(float)
