# 文件名：myquant/indicators/factor_validator.py
"""
factor_validator.py - 因子有效性校验器（IC/ICIR 档案）
========================================================
F-02 整改产物（2026-10-03，对应《因子与机器学习层优化实施报告》F-02）。

背景（问题链，审查确认）:
  - 全库无任何 IC/ICIR/因子显著性计算；
  - MultiFactorStrategy 权重为纯手工设定（动量0.35/反转0.25/低波0.20/量价0.20），
    运行时固化、不可追溯、无有效性依据、无淘汰机制；
  - 无经济学逻辑支撑的纯数据挖掘因子无从拦截。

本模块职责:
  1. IC 计算：RankIC（Spearman，主口径）+ PearsonIC（辅口径）双维度，
     截面-时间双维度（逐日截面相关 → IC 时间序列）
  2. ICIR = mean(IC)/std(IC)；IC t 统计量 = ICIR*sqrt(N)；IC 胜率
  3. 衰减曲线：不同持有期（1/5/10/20 日）的 IC 均值
  4. 5 层分层因子收益 + 调仓换手
  5. FactorProfile 档案：样本区间/IC 均值/ICIR/胜率/换手/经济逻辑/状态，
     按门槛自动流转 active → observe → eliminated
  6. ic_weighted_weights：IC 加权 + 下限约束 + 归一化（废除纯手工权重）
  7. 经济逻辑登记：内置基础因子逻辑表；额外因子须显式提供 economic_logic，
     无逻辑说明的因子默认禁止进组合

防未来函数（py 文件代码规则·铁律 11）:
  - 因子值：t 时刻 trailing 值（由策略/指标层保证，本模块只做校验不生成信号）
  - 未来收益：t→t+horizon 收益仅作为"评估标签"，用于事后审计档案；
    与实时信号路径严格隔离（compute_forward_returns 显式标注禁止在信号路径调用）
  - 截面相关：同一日期截面上因子值与未来收益对齐后计算

设计原则:
  - 全量类型注解 + Google 风格 docstring + 防御式校验
  - 统计工具复用 myquant.utils.math_utils（safe_divide/percentile）
  - 面板输入统一为 date×symbol DataFrame；{symbol: Series} 可用 build_panel 转换
  - 所有核心函数纯函数式，无状态、无 I/O，便于单测与档案复用

依赖: numpy, pandas, myquant.core.logger, myquant.utils.math_utils
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.utils.math_utils import percentile, safe_divide

logger = get_logger("indicators.factor_validator")


# ═══════════════════════════════════════════════
#  常量与门槛
# ═══════════════════════════════════════════════

TRADING_DAYS_PER_YEAR: int = 252
DEFAULT_HORIZON: int = 5
DEFAULT_MIN_PERIODS: int = 30          # IC 时序最少期数（不足不产出档案）
MIN_CROSS_STOCKS: int = 2              # 每期截面最少有效标的（相关至少 2 样本）
DECAY_HORIZONS: Tuple[int, ...] = (1, 5, 10, 20)
N_LAYERS: int = 5                      # 分层收益层数

# 因子状态
FACTOR_STATUS_ACTIVE: str = "active"
FACTOR_STATUS_OBSERVE: str = "observe"
FACTOR_STATUS_ELIMINATED: str = "eliminated"

# 有效性门槛（可参数化，见 evaluate_factor thresholds 参数）
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "ic": 0.02,          # |IC 均值| 下限
    "icir": 0.30,        # ICIR 下限
    "t_stat": 2.0,       # IC t 统计量下限
    "win_rate": 0.55,    # IC 胜率下限
}

# 内置基础因子经济逻辑（与 strategies/multi_factor_strategy.BASE_FACTORS 对应）
FACTOR_ECONOMIC_LOGIC: Dict[str, str] = {
    "momentum": "动量效应：过去 N 期相对强势的股票，未来倾向继续强势（跨市场长期实证）",
    "reversal": "短期反转：过去 M 期超跌的股票短期倾向反弹（过度反应修正与流动性补偿）",
    "low_vol": "低波动异象：低波动股票经风险调整后收益优于高波动（波动率溢价异象）",
    "vol_price": "量价确认：价量齐升反映资金合力，趋势延续概率更高（量价配合原理）",
}


# ═══════════════════════════════════════════════
#  FactorProfile 因子档案
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class FactorProfile:
    """
    因子有效性档案（可审计、可序列化）

    Attributes
    ----------
    factor_name : str
        因子名称
    economic_logic : str
        经济逻辑说明（无逻辑说明的因子不得进组合）
    sample_start / sample_end : str
        样本区间（YYYY-MM-DD）
    n_periods : int
        IC 时序期数（截面数）
    ic_mean : float
        RankIC 均值（主口径）
    ic_std : float
        RankIC 标准差
    icir : float
        ICIR = ic_mean / ic_std
    t_stat : float
        IC t 统计量 = icir * sqrt(n_periods)
    win_rate : float
        IC > 0 的期数占比
    pearson_ic_mean : float
        PearsonIC 均值（辅口径交叉验证）
    layer_returns : Dict[str, float]
        {layer_1..layer_N: 年化收益}（因子值分层组合收益）
    turnover : float
        分层组合调仓换手（相邻期成员集合差异比例均值）
    decay : Dict[str, float]
        {horizon: RankIC 均值}（衰减曲线）
    status : str
        active / observe / eliminated（阈值自动判定）
    thresholds : Dict[str, float]
        判定所用门槛
    created_at : str
        档案生成时间（ISO 格式）
    """

    factor_name: str
    economic_logic: str
    sample_start: str
    sample_end: str
    n_periods: int
    ic_mean: float
    ic_std: float
    icir: float
    t_stat: float
    win_rate: float
    pearson_ic_mean: float
    layer_returns: Dict[str, float]
    turnover: float
    decay: Dict[str, float]
    status: str
    thresholds: Dict[str, float]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 JSON 安全 dict（档案入档/注入策略用）"""
        return {
            "factor_name": self.factor_name,
            "economic_logic": self.economic_logic,
            "sample_start": self.sample_start,
            "sample_end": self.sample_end,
            "n_periods": self.n_periods,
            "ic_mean": self.ic_mean,
            "ic_std": self.ic_std,
            "icir": self.icir,
            "t_stat": self.t_stat,
            "win_rate": self.win_rate,
            "pearson_ic_mean": self.pearson_ic_mean,
            "layer_returns": dict(self.layer_returns),
            "turnover": self.turnover,
            "decay": {str(k): float(v) for k, v in self.decay.items()},
            "status": self.status,
            "thresholds": dict(self.thresholds),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FactorProfile":
        """从 dict 反序列化（防御式校验，缺失字段用安全默认）"""
        if not isinstance(data, dict):
            raise ValueError(f"FactorProfile 数据必须是 dict，got: {type(data).__name__}")
        factor_name = str(data.get("factor_name", ""))
        if not factor_name:
            raise ValueError("FactorProfile 缺少 factor_name")
        return cls(
            factor_name=factor_name,
            economic_logic=str(data.get("economic_logic", "")),
            sample_start=str(data.get("sample_start", "")),
            sample_end=str(data.get("sample_end", "")),
            n_periods=int(data.get("n_periods", 0)),
            ic_mean=float(data.get("ic_mean", 0.0)),
            ic_std=float(data.get("ic_std", 0.0)),
            icir=float(data.get("icir", 0.0)),
            t_stat=float(data.get("t_stat", 0.0)),
            win_rate=float(data.get("win_rate", 0.0)),
            pearson_ic_mean=float(data.get("pearson_ic_mean", 0.0)),
            layer_returns={str(k): float(v) for k, v in (data.get("layer_returns") or {}).items()},
            turnover=float(data.get("turnover", 0.0)),
            decay={str(k): float(v) for k, v in (data.get("decay") or {}).items()},
            status=str(data.get("status", FACTOR_STATUS_OBSERVE)),
            thresholds={str(k): float(v) for k, v in (data.get("thresholds") or {}).items()},
            created_at=str(data.get("created_at", "")),
        )

    def __repr__(self) -> str:
        return (
            f"FactorProfile({self.factor_name}, ic={self.ic_mean:.4f}, "
            f"icir={self.icir:.3f}, status={self.status})"
        )


# ═══════════════════════════════════════════════
#  面板工具
# ═══════════════════════════════════════════════

def build_panel(series_dict: Dict[str, pd.Series]) -> pd.DataFrame:
    """将 {symbol: Series} 转换为 date×symbol 面板 DataFrame。

    Args:
        series_dict: {标的: 因子值序列（index=日期）}。

    Returns:
        date×symbol 面板（索引取并集自动对齐，无数据处为 NaN）。
    """
    if not series_dict:
        return pd.DataFrame()
    frame = pd.DataFrame({sym: pd.Series(s) for sym, s in series_dict.items()})
    return frame.sort_index()


def compute_forward_returns(close_panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """计算未来 horizon 期收益（评估标签，仅审计用途）。

    【防未来函数·显式标注】本函数引用未来数据（t 时刻因子对应
    t→t+horizon 的未来收益），仅用于因子有效性的事后评估档案；
    实时信号路径/回测信号生成禁止调用本函数。

    Args:
        close_panel: date×symbol 收盘价面板。
        horizon: 持有期（bar 数，>=1）。

    Returns:
        未来收益面板（t 行 = close[t+horizon]/close[t] - 1；尾部 horizon 行为 NaN）。
    """
    if close_panel is None or close_panel.empty:
        return pd.DataFrame()
    horizon = int(horizon)
    if horizon < 1:
        raise ValueError(f"horizon 必须 >=1，got: {horizon}")
    return close_panel.shift(-horizon) / close_panel - 1.0


def compute_ic_series(
    factor_panel: pd.DataFrame,
    forward_returns: pd.DataFrame,
    method: str = "spearman",
) -> pd.Series:
    """计算逐日截面 IC 时间序列。

    每个交易日 t：对截面内所有有效（非 NaN）标的，计算
    因子值[t,:] 与 未来收益[t,:] 的相关性（本行使用的数据截至 t 时刻）。

    Args:
        factor_panel: date×symbol 因子值面板（t 时刻 trailing 值）。
        forward_returns: date×symbol 未来收益面板（评估标签）。
        method: "spearman"（RankIC，主口径）或 "pearson"（PearsonIC，辅口径）。

    Returns:
        IC 时间序列（index=日期；截面有效标的 < MIN_CROSS_STOCKS 的日期为 NaN）。
    """
    if factor_panel is None or factor_panel.empty or forward_returns is None or forward_returns.empty:
        return pd.Series(dtype=float)
    if method not in ("spearman", "pearson"):
        raise ValueError(f"method 必须为 'spearman' 或 'pearson'，got: {method!r}")

    # 对齐：日期交集 + 标的交集
    dates = factor_panel.index.intersection(forward_returns.index)
    symbols = factor_panel.columns.intersection(forward_returns.columns)
    if len(dates) == 0 or len(symbols) < MIN_CROSS_STOCKS:
        return pd.Series(dtype=float)
    f = factor_panel.loc[dates, symbols]
    fr = forward_returns.loc[dates, symbols]

    ic_values: List[float] = []
    ic_dates: List[Any] = []
    for dt in dates:
        x = f.loc[dt].astype(float)
        y = fr.loc[dt].astype(float)
        mask = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
        if int(mask.sum()) < MIN_CROSS_STOCKS:
            continue
        xv = x[mask].to_numpy(dtype=float)
        yv = y[mask].to_numpy(dtype=float)
        if float(np.std(xv)) == 0.0 or float(np.std(yv)) == 0.0:
            continue  # 截面无差异 → 相关性无定义，跳过
        corr = (
            pd.Series(xv).corr(pd.Series(yv), method=method)
            if method == "spearman"
            else float(np.corrcoef(xv, yv)[0, 1])
        )
        if corr is not None and not (isinstance(corr, float) and np.isnan(corr)):
            ic_values.append(float(corr))
            ic_dates.append(dt)
    return pd.Series(ic_values, index=ic_dates, dtype=float)


def _annualized_mean(values: Sequence[float]) -> float:
    """简单收益序列的年化均值（评估用途）"""
    clean = [float(v) for v in values if v is not None and np.isfinite(v)]
    if not clean:
        return 0.0
    mean_v = float(np.mean(clean))
    return mean_v * TRADING_DAYS_PER_YEAR


def layer_returns(
    factor_panel: pd.DataFrame,
    forward_returns: pd.DataFrame,
    n_layers: int = N_LAYERS,
) -> Dict[str, float]:
    """因子值分层组合的年化收益（分层收益曲线）。

    每期按因子值分位数分 n_layers 层（layer_1=因子值最低组，layer_N=最高组），
    层内等权组合未来收益，再对时间求均值后年化。

    Args:
        factor_panel: date×symbol 因子值面板。
        forward_returns: date×symbol 未来收益面板（评估标签）。
        n_layers: 分层数（>=2）。

    Returns:
        {layer_1..layer_N: 年化收益}。
    """
    n_layers = int(n_layers)
    if n_layers < 2:
        raise ValueError(f"n_layers 必须 >=2，got: {n_layers}")
    dates = factor_panel.index.intersection(forward_returns.index)
    symbols = factor_panel.columns.intersection(forward_returns.columns)
    if len(dates) == 0 or len(symbols) < n_layers:
        return {f"layer_{i + 1}": 0.0 for i in range(n_layers)}

    layer_series: Dict[str, List[float]] = {f"layer_{i + 1}": [] for i in range(n_layers)}
    for dt in dates:
        x = factor_panel.loc[dt, symbols].astype(float)
        y = forward_returns.loc[dt, symbols].astype(float)
        mask = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
        if int(mask.sum()) < n_layers:
            continue
        xv = x[mask]
        yv = y[mask]
        ranks = pd.Series(xv).rank(method="first")
        n = len(ranks)
        for i in range(n_layers):
            lo = int(np.floor(i * n / n_layers))
            hi = int(np.floor((i + 1) * n / n_layers))
            if hi <= lo:
                continue
            idx = ranks.iloc[lo:hi].index
            layer_series[f"layer_{i + 1}"].extend(float(v) for v in yv.loc[idx])
    return {k: round(_annualized_mean(v), 6) for k, v in layer_series.items()}


def factor_turnover(
    factor_panel: pd.DataFrame,
    n_layers: int = N_LAYERS,
) -> float:
    """分层组合调仓换手（相邻两期成员集合差异比例均值）。

    换手率 = mean(1 - |S_t ∩ S_{t+1}| / |S_t ∪ S_{t+1}|)（Jaccard 距离，
    逐层计算后取平均）。

    Args:
        factor_panel: date×symbol 因子值面板。
        n_layers: 分层数。

    Returns:
        平均换手率（0~1；数据不足返回 0.0）。
    """
    n_layers = int(n_layers)
    if n_layers < 2 or factor_panel is None or factor_panel.empty:
        return 0.0
    dates = list(factor_panel.index)
    if len(dates) < 2:
        return 0.0

    prev_layers: List[set] = []
    turnovers: List[float] = []
    for dt in dates:
        row = factor_panel.loc[dt].astype(float).dropna()
        if len(row) < n_layers:
            continue
        ranks = pd.Series(row).rank(method="first")
        n = len(ranks)
        layers: List[set] = []
        for i in range(n_layers):
            lo = int(np.floor(i * n / n_layers))
            hi = int(np.floor((i + 1) * n / n_layers))
            if hi <= lo:
                continue
            layers.append(set(ranks.iloc[lo:hi].index))
        if prev_layers:
            tos = []
            for a, b in zip(prev_layers, layers):
                union = a | b
                inter = a & b
                tos.append(1.0 - len(inter) / len(union) if union else 0.0)
            if tos:
                turnovers.append(float(np.mean(tos)))
        prev_layers = layers
    return round(float(np.mean(turnovers)), 6) if turnovers else 0.0


# ═══════════════════════════════════════════════
#  有效性判定与档案构建
# ═══════════════════════════════════════════════

def decide_status(
    ic_mean: float,
    icir: float,
    t_stat: float,
    win_rate: float,
    thresholds: Optional[Dict[str, float]] = None,
) -> str:
    """按门槛判定因子状态。

    规则:
      - 全部门槛达标（|IC|、ICIR、|t|、胜率）→ active
      - 方向性失效（ICIR <= 0 或 |t| < 1）→ eliminated
      - 其余（临界/样本不足但非完全失效）→ observe

    Args:
        ic_mean: RankIC 均值。
        icir: ICIR。
        t_stat: IC t 统计量。
        win_rate: IC 胜率。
        thresholds: 门槛字典（缺省用 DEFAULT_THRESHOLDS）。

    Returns:
        "active" / "observe" / "eliminated"。
    """
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update(thresholds)
    ic_th = float(th.get("ic", 0.02))
    icir_th = float(th.get("icir", 0.30))
    t_th = float(th.get("t_stat", 2.0))
    win_th = float(th.get("win_rate", 0.55))

    if (abs(ic_mean) >= ic_th and icir >= icir_th
            and abs(t_stat) >= t_th and win_rate >= win_th):
        return FACTOR_STATUS_ACTIVE
    if icir <= 0.0 or abs(t_stat) < 1.0 or abs(ic_mean) < ic_th * 0.5:
        return FACTOR_STATUS_ELIMINATED
    return FACTOR_STATUS_OBSERVE


def evaluate_factor(
    factor_name: str,
    factor_panel: pd.DataFrame,
    close_panel: pd.DataFrame,
    horizon: int = DEFAULT_HORIZON,
    economic_logic: str = "",
    n_layers: int = N_LAYERS,
    thresholds: Optional[Dict[str, float]] = None,
    min_periods: int = DEFAULT_MIN_PERIODS,
) -> Optional[FactorProfile]:
    """一站式构建因子有效性档案（IC/ICIR/衰减/分层/状态）。

    Args:
        factor_name: 因子名称（档案标识）。
        factor_panel: date×symbol 因子值面板（t 时刻 trailing 值）。
        close_panel: date×symbol 收盘价面板（用于未来收益评估标签）。
        horizon: 主持有期（默认 5）。
        economic_logic: 经济逻辑说明（内置因子可省略，额外因子必须提供）。
        n_layers: 分层收益层数。
        thresholds: 门槛字典（缺省 DEFAULT_THRESHOLDS）。
        min_periods: IC 时序最少期数；不足返回 None（不产出档案）。

    Returns:
        FactorProfile 或 None（面板过短/无有效截面）。

    Raises:
        ValueError: 面板为空 / horizon 非法 / n_layers 非法。
    """
    if not factor_name or not str(factor_name).strip():
        raise ValueError("factor_name 不能为空")
    if factor_panel is None or factor_panel.empty or close_panel is None or close_panel.empty:
        raise ValueError("factor_panel 与 close_panel 不能为空")
    horizon = int(horizon)
    if horizon < 1:
        raise ValueError(f"horizon 必须 >=1，got: {horizon}")
    n_layers = int(n_layers)
    if n_layers < 2:
        raise ValueError(f"n_layers 必须 >=2，got: {n_layers}")

    # 对齐面板
    dates = factor_panel.index.intersection(close_panel.index)
    symbols = factor_panel.columns.intersection(close_panel.columns)
    if len(dates) == 0 or len(symbols) < MIN_CROSS_STOCKS:
        logger.warning("[因子校验] 面板对齐后无有效数据，跳过档案构建")
        return None
    f = factor_panel.loc[dates, symbols]
    c = close_panel.loc[dates, symbols]

    forward = compute_forward_returns(c, horizon)
    rank_ic = compute_ic_series(f, forward, method="spearman")
    if len(rank_ic) < min_periods:
        logger.warning(
            "[因子校验] IC 时序期数 %d < %d，不产出档案",
            len(rank_ic), min_periods)
        return None

    ic_mean = float(rank_ic.mean())
    ic_std = float(rank_ic.std(ddof=1)) if len(rank_ic) > 1 else 0.0
    icir = safe_divide(ic_mean, ic_std)
    t_stat = icir * float(np.sqrt(len(rank_ic)))
    win_rate = safe_divide(float((rank_ic > 0).sum()), float(len(rank_ic)))
    pearson_ic = compute_ic_series(f, forward, method="pearson")
    pearson_mean = float(pearson_ic.mean()) if len(pearson_ic) > 0 else 0.0

    # 衰减曲线：各持有期 IC 均值
    decay: Dict[str, float] = {}
    for h in DECAY_HORIZONS:
        fwd_h = compute_forward_returns(c, int(h))
        ic_h = compute_ic_series(f, fwd_h, method="spearman")
        decay[str(h)] = round(float(ic_h.mean()), 6) if len(ic_h) > 0 else 0.0

    layer_ret = layer_returns(f, forward, n_layers=n_layers)
    turnover = factor_turnover(f, n_layers=n_layers)
    status = decide_status(ic_mean, icir, t_stat, win_rate, thresholds)

    return FactorProfile(
        factor_name=str(factor_name).strip(),
        economic_logic=economic_logic,
        sample_start=str(rank_ic.index[0])[:10] if len(rank_ic) > 0 else "",
        sample_end=str(rank_ic.index[-1])[:10] if len(rank_ic) > 0 else "",
        n_periods=len(rank_ic),
        ic_mean=round(ic_mean, 6),
        ic_std=round(ic_std, 6),
        icir=round(icir, 4),
        t_stat=round(t_stat, 4),
        win_rate=round(win_rate, 4),
        pearson_ic_mean=round(pearson_mean, 6),
        layer_returns=layer_ret,
        turnover=turnover,
        decay=decay,
        status=status,
        thresholds=dict(DEFAULT_THRESHOLDS if not thresholds else {**DEFAULT_THRESHOLDS, **thresholds}),
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


# ═══════════════════════════════════════════════
#  IC 加权权重（废除纯手工权重）
# ═══════════════════════════════════════════════

def ic_weighted_weights(
    profiles: Dict[str, FactorProfile],
    min_weight: float = 0.05,
    directions: Optional[Dict[str, int]] = None,
) -> Dict[str, float]:
    """按 IC 档案计算因子权重（IC 加权 + 下限约束 + 归一化）。

    规则:
      - 权重基础 = max(direction * ICIR, 0)：方向校正后 ICIR <= 0 的因子
        无正向贡献 → 权重 0（不进组合）；
      - 下限约束：启用因子权重均 >= min_weight（不足的提升到下限，
        超额部分按比例从高权重因子削减，迭代至收敛）；
      - 最终归一化和为 1。

    Args:
        profiles: {因子名: FactorProfile}。
        min_weight: 单因子权重下限（默认 0.05）。
        directions: {因子名: ±1}（策略方向；缺省全部视为 +1）。

    Returns:
        {因子名: 权重}（和为 1；无正贡献因子时返回空 dict）。
    """
    min_weight = float(min_weight)
    if min_weight < 0.0 or min_weight > 1.0:
        raise ValueError(f"min_weight 必须在 [0,1]，got: {min_weight}")
    if not profiles:
        return {}

    raw: Dict[str, float] = {}
    for name, prof in profiles.items():
        if prof is None or not isinstance(prof, FactorProfile):
            logger.warning("[因子校验] 忽略非法档案: %s", name)
            continue
        icir = float(prof.icir)
        d = int(directions.get(name, 1)) if directions else 1
        eff = d * icir
        if eff <= 0.0:
            continue
        raw[name] = eff
    if not raw:
        return {}

    total = float(sum(raw.values()))
    weights: Dict[str, float] = {n: w / total for n, w in raw.items()}

    # 下限约束迭代（比例提升 + 按比例削减，最多 20 轮收敛）
    eps = 1e-9
    for _ in range(20):
        below = [n for n, w in weights.items() if w < min_weight - eps]
        if not below:
            break
        deficit = float(sum(min_weight - weights[n] for n in below))
        above = [n for n, w in weights.items() if w >= min_weight - eps]
        above_total = float(sum(weights[n] for n in above))
        if not above or above_total <= eps:
            break
        for n in below:
            weights[n] = min_weight
        for n in above:
            weights[n] -= deficit * (weights[n] / above_total)

    s = float(sum(weights.values()))
    if s <= 0.0:
        return {}
    return {n: round(w / s, 6) for n, w in weights.items()}


# ═══════════════════════════════════════════════
#  经济逻辑校验
# ═══════════════════════════════════════════════

def validate_factor_logic(
    factor_name: str,
    economic_logic: Optional[str] = None,
) -> bool:
    """校验因子是否具备经济逻辑说明（无逻辑的因子默认禁止进组合）。

    Args:
        factor_name: 因子名称。
        economic_logic: 额外因子显式提供的经济逻辑说明。

    Returns:
        True=有逻辑说明；False=缺失（记录 warning）。
    """
    if factor_name in FACTOR_ECONOMIC_LOGIC:
        return True
    if economic_logic and str(economic_logic).strip():
        return True
    logger.warning(
        "[因子校验] 因子 %s 缺少经济逻辑说明（economic_logic），默认禁止进组合",
        factor_name)
    return False


__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "DEFAULT_HORIZON",
    "DEFAULT_THRESHOLDS",
    "FACTOR_ECONOMIC_LOGIC",
    "FACTOR_STATUS_ACTIVE",
    "FACTOR_STATUS_OBSERVE",
    "FACTOR_STATUS_ELIMINATED",
    "FactorProfile",
    "build_panel",
    "compute_forward_returns",
    "compute_ic_series",
    "layer_returns",
    "factor_turnover",
    "decide_status",
    "evaluate_factor",
    "ic_weighted_weights",
    "validate_factor_logic",
]
