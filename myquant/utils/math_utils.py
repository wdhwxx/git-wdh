# 文件名：myquant/utils/math_utils.py
"""
math_utils.py - 数学统计、数值格式化、性能计算辅助
===================================================
功能清单:
  1. 基础统计 (均值/标准差/偏度/峰度/分位数)
  2. 金融绩效指标 (年化收益/夏普/索提诺/最大回撤/Calmar/胜率)
  3. 数值格式化 (千分位/百分比/科学计数)
  4. 滚动计算辅助 (rolling mean/std)
  5. 收益率转换 (对数收益/简单收益/年化)
  6. 安全除法 / 数值裁剪

依赖: math, statistics, typing
可选: numpy, pandas (大数据量场景)
"""

from __future__ import annotations

import math
import statistics
from typing import List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

TRADING_DAYS_PER_YEAR = 252       # A股年交易日
TRADING_HOURS_PER_DAY = 4         # 4小时交易
RISK_FREE_RATE_ANNUAL = 0.02      # 无风险利率 (年化2%, 可配置)
EPSILON = 1e-10                   # 防除零


# ─────────────────────────────────────────────
# 安全运算
# ─────────────────────────────────────────────

def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """安全除法，分母为0时返回default"""
    if abs(denominator) < EPSILON:
        return default
    return numerator / denominator


def clip(value: float, min_val: float, max_val: float) -> float:
    """数值裁剪到 [min_val, max_val]"""
    return max(min_val, min(max_val, value))


def is_valid_number(x) -> bool:
    """判断是否为有效数字 (非None/NaN/Inf)"""
    if x is None:
        return False
    try:
        x = float(x)
        return math.isfinite(x)
    except (TypeError, ValueError):
        return False


def clean_series(data: Sequence[float]) -> List[float]:
    """过滤无效值 (None, NaN, Inf)"""
    return [float(x) for x in data if is_valid_number(x)]


# ─────────────────────────────────────────────
# 基础统计
# ─────────────────────────────────────────────

def mean(data: Sequence[float]) -> float:
    """算术平均值"""
    clean = clean_series(data)
    return statistics.mean(clean) if clean else 0.0


def median(data: Sequence[float]) -> float:
    """中位数"""
    clean = clean_series(data)
    return statistics.median(clean) if clean else 0.0


def std(data: Sequence[float], ddof: int = 1) -> float:
    """标准差 (ddof=1 样本标准差)"""
    clean = clean_series(data)
    if len(clean) <= ddof:
        return 0.0
    return statistics.stdev(clean) if ddof == 1 else _std_pop(clean)


def _std_pop(data: List[float]) -> float:
    """总体标准差"""
    n = len(data)
    if n == 0:
        return 0.0
    m = sum(data) / n
    return math.sqrt(sum((x - m) ** 2 for x in data) / n)


def variance(data: Sequence[float], ddof: int = 1) -> float:
    """方差"""
    s = std(data, ddof)
    return s * s


def skewness(data: Sequence[float]) -> float:
    """偏度 (Fisher)"""
    clean = clean_series(data)
    n = len(clean)
    if n < 3:
        return 0.0
    m = mean(clean)
    s = std(clean, ddof=1)
    if s < EPSILON:
        return 0.0
    return (n / ((n - 1) * (n - 2))) * sum(((x - m) / s) ** 3 for x in clean)


def kurtosis(data: Sequence[float], excess: bool = True) -> float:
    """
    峰度
    excess=True: 超额峰度 (正态=0)
    excess=False: 原始峰度 (正态=3)
    """
    clean = clean_series(data)
    n = len(clean)
    if n < 4:
        return 0.0
    m = mean(clean)
    s = std(clean, ddof=1)
    if s < EPSILON:
        return 0.0
    k4 = sum(((x - m) / s) ** 4 for x in clean) / n
    if excess:
        return k4 - 3.0
    return k4


def percentile(data: Sequence[float], p: float) -> float:
    """
    分位数 (0-100)
    线性插值法
    """
    clean = sorted(clean_series(data))
    if not clean:
        return 0.0
    if len(clean) == 1:
        return clean[0]
    k = (p / 100.0) * (len(clean) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return clean[int(k)]
    return clean[f] * (c - k) + clean[c] * (k - f)


def quantile_summary(data: Sequence[float]) -> dict:
    """五数概括 + 均值"""
    clean = clean_series(data)
    if not clean:
        return {}
    return {
        "count": len(clean),
        "mean": round(mean(clean), 6),
        "std": round(std(clean), 6),
        "min": min(clean),
        "q25": percentile(clean, 25),
        "median": percentile(clean, 50),
        "q75": percentile(clean, 75),
        "max": max(clean),
        "skewness": round(skewness(clean), 4),
        "kurtosis": round(kurtosis(clean), 4),
    }


# ─────────────────────────────────────────────
# 收益率计算
# ─────────────────────────────────────────────

def simple_return(price_start: float, price_end: float) -> float:
    """简单收益率"""
    return safe_divide(price_end - price_start, price_start)


def log_return(price_start: float, price_end: float) -> float:
    """对数收益率"""
    if price_start <= 0 or price_end <= 0:
        return 0.0
    return math.log(price_end / price_start)


def cumulative_return(returns: Sequence[float]) -> float:
    """
    累计收益率 (从收益率序列)
    cumulative = prod(1+r_i) - 1
    """
    clean = clean_series(returns)
    if not clean:
        return 0.0
    product = 1.0
    for r in clean:
        product *= (1.0 + r)
    return product - 1.0


def annualize_return(
    total_return: float,
    days: int,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    年化收益率
    annualized = (1 + total_return) ^ (trading_days / days) - 1
    """
    if days <= 0:
        return 0.0
    if total_return <= -1.0:
        return -1.0
    return (1.0 + total_return) ** (trading_days / days) - 1.0


def calc_annualized_return(
    returns: Sequence[float],
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """从日收益率序列计算年化收益率"""
    clean = clean_series(returns)
    if not clean:
        return 0.0
    total = cumulative_return(clean)
    return annualize_return(total, len(clean), trading_days)


# ─────────────────────────────────────────────
# 金融绩效指标
# ─────────────────────────────────────────────

def calc_sharpe_ratio(
    returns: Sequence[float],
    risk_free_rate: float = RISK_FREE_RATE_ANNUAL,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    年化夏普比率
    Sharpe = (annualized_return - rf) / annualized_volatility

    Parameters
    ----------
    returns : 日收益率序列
    risk_free_rate : 年化无风险利率
    trading_days : 年交易日数
    """
    clean = clean_series(returns)
    if len(clean) < 2:
        return 0.0
    ann_ret = calc_annualized_return(clean, trading_days)
    daily_rf = risk_free_rate / trading_days
    excess = [r - daily_rf for r in clean]
    vol = std(excess, ddof=1) * math.sqrt(trading_days)
    return safe_divide(ann_ret - risk_free_rate, vol)


def calc_sortino_ratio(
    returns: Sequence[float],
    risk_free_rate: float = RISK_FREE_RATE_ANNUAL,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    索提诺比率 (仅考虑下行波动)
    """
    clean = clean_series(returns)
    if len(clean) < 2:
        return 0.0
    ann_ret = calc_annualized_return(clean, trading_days)
    daily_rf = risk_free_rate / trading_days
    downside = [min(0.0, r - daily_rf) for r in clean]
    downside_vol = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
    ann_downside_vol = downside_vol * math.sqrt(trading_days)
    return safe_divide(ann_ret - risk_free_rate, ann_downside_vol)


def calc_max_drawdown(
    equity_curve: Sequence[float],
) -> Tuple[float, int, int]:
    """
    最大回撤

    Parameters
    ----------
    equity_curve : 净值序列 (如 [1.0, 1.05, 0.95, 1.1, ...])

    Returns
    -------
    (max_dd, peak_idx, trough_idx)
      max_dd : 最大回撤比例 (正数, 如0.15表示15%回撤)
      peak_idx : 峰值位置索引
      trough_idx : 谷值位置索引
    """
    clean = clean_series(equity_curve)
    if len(clean) < 2:
        return (0.0, 0, 0)

    max_dd = 0.0
    peak_idx = 0
    trough_idx = 0
    current_peak_idx = 0
    peak_val = clean[0]

    for i in range(1, len(clean)):
        if clean[i] > peak_val:
            peak_val = clean[i]
            current_peak_idx = i
        dd = (peak_val - clean[i]) / peak_val
        if dd > max_dd:
            max_dd = dd
            peak_idx = current_peak_idx
            trough_idx = i

    return (max_dd, peak_idx, trough_idx)


def calc_max_drawdown_duration(equity_curve: Sequence[float]) -> int:
    """最大回撤持续天数 (从峰值到恢复新高的最长天数)"""
    clean = clean_series(equity_curve)
    if len(clean) < 2:
        return 0
    max_duration = 0
    current_duration = 0
    peak_val = clean[0]
    for val in clean[1:]:
        if val >= peak_val:
            peak_val = val
            current_duration = 0
        else:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
    return max_duration


def calc_calmar_ratio(
    returns: Sequence[float],
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Calmar比率 = 年化收益 / 最大回撤
    """
    clean = clean_series(returns)
    if len(clean) < 2:
        return 0.0
    # 构建净值曲线
    equity = [1.0]
    for r in clean:
        equity.append(equity[-1] * (1 + r))
    ann_ret = calc_annualized_return(clean, trading_days)
    max_dd, _, _ = calc_max_drawdown(equity)
    return safe_divide(ann_ret, max_dd)


def calc_win_rate(returns: Sequence[float]) -> float:
    """胜率 (正收益天数占比)"""
    clean = clean_series(returns)
    if not clean:
        return 0.0
    wins = sum(1 for r in clean if r > 0)
    return wins / len(clean)


def calc_profit_loss_ratio(returns: Sequence[float]) -> float:
    """盈亏比 (平均盈利 / 平均亏损)"""
    clean = clean_series(returns)
    wins = [r for r in clean if r > 0]
    losses = [r for r in clean if r < 0]
    if not wins or not losses:
        return 0.0
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    return safe_divide(avg_win, avg_loss)


def calc_volatility(
    returns: Sequence[float],
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """年化波动率"""
    clean = clean_series(returns)
    if len(clean) < 2:
        return 0.0
    return std(clean, ddof=1) * math.sqrt(trading_days)


def calc_beta(
    asset_returns: Sequence[float],
    benchmark_returns: Sequence[float],
) -> float:
    """Beta系数"""
    a = clean_series(asset_returns)
    b = clean_series(benchmark_returns)
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[:n], b[:n]
    mean_a, mean_b = mean(a), mean(b)
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / (n - 1)
    var_b = variance(b, ddof=1)
    return safe_divide(cov, var_b)


def calc_alpha(
    asset_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    risk_free_rate: float = RISK_FREE_RATE_ANNUAL,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """年化Alpha"""
    beta = calc_beta(asset_returns, benchmark_returns)
    ann_asset = calc_annualized_return(asset_returns, trading_days)
    ann_bench = calc_annualized_return(benchmark_returns, trading_days)
    return ann_asset - (risk_free_rate + beta * (ann_bench - risk_free_rate))


def performance_report(
    returns: Sequence[float],
    benchmark_returns: Optional[Sequence[float]] = None,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> dict:
    """
    一键生成绩效报告

    Returns
    -------
    dict : 包含所有关键指标
    """
    clean = clean_series(returns)
    if not clean:
        return {"error": "无有效收益数据"}

    # 构建净值
    equity = [1.0]
    for r in clean:
        equity.append(equity[-1] * (1 + r))

    max_dd, peak_i, trough_i = calc_max_drawdown(equity)

    report = {
        "total_return": round(cumulative_return(clean), 6),
        "annualized_return": round(calc_annualized_return(clean, trading_days), 6),
        "annualized_volatility": round(calc_volatility(clean, trading_days), 6),
        "sharpe_ratio": round(calc_sharpe_ratio(clean, trading_days=trading_days), 4),
        "sortino_ratio": round(calc_sortino_ratio(clean, trading_days=trading_days), 4),
        "calmar_ratio": round(calc_calmar_ratio(clean, trading_days), 4),
        "max_drawdown": round(max_dd, 6),
        "max_dd_duration_days": calc_max_drawdown_duration(equity),
        "win_rate": round(calc_win_rate(clean), 4),
        "profit_loss_ratio": round(calc_profit_loss_ratio(clean), 4),
        "trading_days": len(clean),
        "skewness": round(skewness(clean), 4),
        "kurtosis": round(kurtosis(clean), 4),
    }

    if benchmark_returns:
        report["beta"] = round(calc_beta(clean, benchmark_returns), 4)
        report["alpha"] = round(calc_alpha(clean, benchmark_returns), 6)
    else:
        # KP-P2-04：无基准 → 显式置 None，不伪造 alpha/beta
        report["beta"] = None
        report["alpha"] = None

    return report


# ─────────────────────────────────────────────
# 数值格式化
# ─────────────────────────────────────────────

def fmt_number(
    x: float,
    decimals: int = 2,
    thousands_sep: bool = True,
    prefix: str = "",
    suffix: str = "",
) -> str:
    """
    数值格式化
    fmt_number(1234567.891) -> "1,234,567.89"
    """
    if not is_valid_number(x):
        return "N/A"
    if thousands_sep:
        formatted = f"{x:,.{decimals}f}"
    else:
        formatted = f"{x:.{decimals}f}"
    return f"{prefix}{formatted}{suffix}"


def fmt_percent(
    x: float,
    decimals: int = 2,
    signed: bool = True,
) -> str:
    """
    百分比格式化 (输入为小数, 如0.1523 -> '15.23%')
    """
    if not is_valid_number(x):
        return "N/A"
    val = x * 100
    sign = "+" if (signed and val > 0) else ""
    return f"{sign}{val:.{decimals}f}%"


def fmt_price(x: float, decimals: int = 2) -> str:
    """价格格式化"""
    return fmt_number(x, decimals=decimals, prefix="¥")


def fmt_volume(x: float) -> str:
    """成交量格式化 (自动选择 万/亿)"""
    if not is_valid_number(x):
        return "N/A"
    if abs(x) >= 1e8:
        return f"{x / 1e8:.2f}亿"
    elif abs(x) >= 1e4:
        return f"{x / 1e4:.2f}万"
    return f"{x:.0f}"


def fmt_duration(seconds: float) -> str:
    """秒数 -> 可读时间 '2h 15m 30s'"""
    if seconds < 0:
        return "0s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    parts = []
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


# ─────────────────────────────────────────────
# 滚动计算 (纯Python实现, 不依赖pandas)
# ─────────────────────────────────────────────

def rolling_mean(data: Sequence[float], window: int) -> List[Optional[float]]:
    """滚动均值"""
    result: List[Optional[float]] = [None] * len(data)
    for i in range(window - 1, len(data)):
        result[i] = mean(data[i - window + 1: i + 1])
    return result


def rolling_std(data: Sequence[float], window: int) -> List[Optional[float]]:
    """滚动标准差"""
    result: List[Optional[float]] = [None] * len(data)
    for i in range(window - 1, len(data)):
        result[i] = std(data[i - window + 1: i + 1])
    return result


def rolling_max_drawdown(equity: Sequence[float], window: int) -> List[Optional[float]]:
    """滚动窗口内最大回撤"""
    result: List[Optional[float]] = [None] * len(equity)
    for i in range(window - 1, len(equity)):
        segment = equity[i - window + 1: i + 1]
        dd, _, _ = calc_max_drawdown(segment)
        result[i] = dd
    return result


def ewma(data: Sequence[float], span: int) -> List[float]:
    """指数加权移动平均"""
    alpha = 2.0 / (span + 1)
    result = [data[0]] if data else []
    for i in range(1, len(data)):
        result.append(alpha * data[i] + (1 - alpha) * result[-1])
    return result


# ─────────────────────────────────────────────
# 自测
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  math_utils.py 自测")
    print("=" * 60)

    # 模拟日收益率序列
    import random
    random.seed(42)
    daily_returns = [random.gauss(0.0005, 0.02) for _ in range(252)]

    # 1) 基础统计
    print("\n[1] 基础统计:")
    print(f"    均值:   {mean(daily_returns):.6f}")
    print(f"    标准差: {std(daily_returns):.6f}")
    print(f"    偏度:   {skewness(daily_returns):.4f}")
    print(f"    峰度:   {kurtosis(daily_returns):.4f}")

    # 2) 绩效指标
    print("\n[2] 绩效指标:")
    print(f"    累计收益:   {fmt_percent(cumulative_return(daily_returns))}")
    print(f"    年化收益:   {fmt_percent(calc_annualized_return(daily_returns))}")
    print(f"    年化波动率: {fmt_percent(calc_volatility(daily_returns))}")
    print(f"    夏普比率:   {calc_sharpe_ratio(daily_returns):.4f}")
    print(f"    索提诺比率: {calc_sortino_ratio(daily_returns):.4f}")
    print(f"    Calmar比率: {calc_calmar_ratio(daily_returns):.4f}")
    print(f"    胜率:       {fmt_percent(calc_win_rate(daily_returns), signed=False)}")
    print(f"    盈亏比:     {calc_profit_loss_ratio(daily_returns):.4f}")

    # 3) 最大回撤
    equity = [1.0]
    for r in daily_returns:
        equity.append(equity[-1] * (1 + r))
    max_dd, pi, ti = calc_max_drawdown(equity)
    print(f"\n[3] 最大回撤: {fmt_percent(max_dd, signed=False)}")
    print(f"    峰值位置: day {pi}, 谷值位置: day {ti}")
    print(f"    最长回撤持续: {calc_max_drawdown_duration(equity)} 天")

    # 4) 格式化
    print("\n[4] 格式化:")
    print(f"    {fmt_number(1234567.891)}")
    print(f"    {fmt_percent(0.1523)}")
    print(f"    {fmt_price(1800.5)}")
    print(f"    {fmt_volume(356789012)}")
    print(f"    {fmt_duration(8130)}")

    # 5) 完整报告
    print("\n[5] 完整绩效报告:")
    report = performance_report(daily_returns)
    for k, v in report.items():
        print(f"    {k:>25s}: {v}")

    print("\n" + "=" * 60)
    print("  ✓ math_utils.py 自测完成")
    print("=" * 60)