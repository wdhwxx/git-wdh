# 文件名：myquant/indicators/factor_preprocessor.py
"""
factor_preprocessor.py - 因子预处理（去极值 + 中性化 + 残差检验）
==================================================================
F-03 整改产物（2026-10-03，对应《因子与机器学习层优化实施报告》F-03）。

背景（问题链，审查确认）:
  - 因子计算后无去极值：极端值直接进入 z-score 与综合打分，
    少数异常标的可扭曲截面均值/方差，进而扭曲排名；
  - 无中性化：因子与行业/市值风格暴露未被剔除，选股结果混入风格 Beta，
    并非纯粹因子 alpha；
  - 无残差检验：暴露显著性、残差正态性/异方差无从审计，无法判定
    中性化是否彻底。

本模块职责:
  1. 自适应去极值：按因子截面分布特性自动选择方法
     - |skew| < SKEW_SYMMETRIC_THRESHOLD（对称分布）→ MAD 截断
       （中位数 ± k * 1.4826 * MAD，等价 3σ 尺度）
     - 否则（偏态分布）→ 分位数截断（1%/99%）
     - 波动率类因子不再固定 MAD 一刀切（任务卡硬性要求）
  2. 中性化：行业哑变量（N-1 列避免共线）+ 对数流通市值，
     numpy lstsq 最小二乘回归取残差（零 sklearn 依赖，项目约束）
  3. 残差检验：行业/市值暴露 t 值（β/se，se 来自 (X'X)^-1 对角）、
     残差偏度/峰度（正态性告警）、残差幅值-拟合幅值相关（异方差近似检验），
     超阈值告警并入档
  4. PreprocessReport：可序列化检验报告（去极值方法/截断数/暴露 t 值/
     残差诊断/告警 flags），供策略入档与审计

防未来函数（py 文件代码规则·铁律 11）:
  - 去极值与中性化的统计量均基于"当前调仓截面内"数据计算：
    单截面计算，无跨期引用，天然不含未来信息；
  - 市值 = processed 当日流通股本 × 当日收盘价现算（t 时刻可得）；
  - 行业映射为当前分类快照（行业分类低频变动，近似历史，报告披露口径）；
  - 本行使用的数据截至 t 时刻。

实现:
  - 全量 numpy/pandas 实现，零 sklearn/scipy 依赖
  - 统计工具复用 myquant.utils.math_utils（skewness/kurtosis/percentile）
  - 纯函数无状态，便于单测与审计
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.utils.math_utils import kurtosis, percentile, skewness

logger = get_logger("indicators.factor_preprocessor")


# ═══════════════════════════════════════════════
#  常量与阈值
# ═══════════════════════════════════════════════

# 分布形态判定：|偏度| < 阈值 视为对称分布 → MAD 截断；否则偏态 → 分位数截断
SKEW_SYMMETRIC_THRESHOLD: float = 0.5
# MAD 截断倍数（等价 3σ）；MAD→σ 换算常数（正态假设）
MAD_K: float = 3.0
MAD_CONSTANT: float = 1.4826
# 偏态分布分位数截断边界
QUANTILE_LOW: float = 0.01
QUANTILE_HIGH: float = 0.99
# 中性化最少有效样本（不足则跳过回归仅中心化）
MIN_OBS_FOR_NEUTRALIZE: int = 10
# 暴露显著性阈值：|t| >= 2 视为显著暴露（验收：中性化后全部 < 2）
T_EXPOSURE_THRESHOLD: float = 2.0
# 残差诊断告警阈值
SKEW_ALERT_THRESHOLD: float = 1.0      # 残差 |偏度| 超阈值告警
KURT_ALERT_THRESHOLD: float = 8.0      # 残差峰度超阈值告警（厚尾）
HETERO_CORR_THRESHOLD: float = 0.3     # |残差|-|拟合| 相关超阈值告警（异方差迹象）

DIST_SYMMETRIC: str = "symmetric"
DIST_SKEWED: str = "skewed"
METHOD_MAD: str = "mad"
METHOD_QUANTILE: str = "quantile"


# ═══════════════════════════════════════════════
#  PreprocessReport 预处理检验报告
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class PreprocessReport:
    """
    因子预处理检验报告（可序列化，供策略入档与审计）。

    Attributes
    ----------
    factor_name : str
        因子名称
    method : str
        去极值方法：mad / quantile / none（未去极值）
    n_raw / n_valid : int
        去极值前后有效样本数
    n_winsorized : int
        被截断的极端值数量
    mean_before / mean_after : float
        去极值前后截面均值（均值偏移 = 极端值影响，验收口径）
    exposure_t : Dict[str, float]
        中性化回归各变量（行业哑变量/对数市值）暴露 t 值
    residual_skew / residual_kurt : float
        残差偏度/峰度（正态性诊断）
    hetero_corr : float
        残差幅值-拟合幅值相关（异方差近似检验）
    flags : List[str]
        告警信息列表（超阈值项），空=全部通过
    note : str
        口径说明（如行业缺失降级、市值缺失降级）
    created_at : str
        报告生成时间
    """

    factor_name: str
    method: str
    n_raw: int
    n_valid: int
    n_winsorized: int
    mean_before: float
    mean_after: float
    exposure_t: Dict[str, float]
    residual_skew: float
    residual_kurt: float
    hetero_corr: float
    flags: List[str]
    note: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 JSON 安全 dict（报告入档/看板）"""
        return {
            "factor_name": self.factor_name,
            "method": self.method,
            "n_raw": self.n_raw,
            "n_valid": self.n_valid,
            "n_winsorized": self.n_winsorized,
            "mean_before": self.mean_before,
            "mean_after": self.mean_after,
            "exposure_t": dict(self.exposure_t),
            "residual_skew": self.residual_skew,
            "residual_kurt": self.residual_kurt,
            "hetero_corr": self.hetero_corr,
            "flags": list(self.flags),
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PreprocessReport":
        """从 dict 反序列化（防御式校验）"""
        if not isinstance(data, dict):
            raise ValueError(f"PreprocessReport 数据必须是 dict，got: {type(data).__name__}")
        factor_name = str(data.get("factor_name", ""))
        if not factor_name:
            raise ValueError("PreprocessReport 缺少 factor_name")
        return cls(
            factor_name=factor_name,
            method=str(data.get("method", METHOD_MAD)),
            n_raw=int(data.get("n_raw", 0)),
            n_valid=int(data.get("n_valid", 0)),
            n_winsorized=int(data.get("n_winsorized", 0)),
            mean_before=float(data.get("mean_before", 0.0)),
            mean_after=float(data.get("mean_after", 0.0)),
            exposure_t={str(k): float(v) for k, v in (data.get("exposure_t") or {}).items()},
            residual_skew=float(data.get("residual_skew", 0.0)),
            residual_kurt=float(data.get("residual_kurt", 0.0)),
            hetero_corr=float(data.get("hetero_corr", 0.0)),
            flags=[str(f) for f in (data.get("flags") or [])],
            note=str(data.get("note", "")),
            created_at=str(data.get("created_at", "")),
        )


# ═══════════════════════════════════════════════
#  1. 自适应去极值
# ═══════════════════════════════════════════════

def detect_distribution(series: pd.Series) -> str:
    """按截面偏度判定分布形态。

    Args:
        series: 因子值序列（可含 NaN）。

    Returns:
        "symmetric"（|skew|<阈值，MAD 截断）或 "skewed"（分位数截断）。
    """
    clean = series.dropna()
    if len(clean) < 5:
        return DIST_SYMMETRIC  # 样本不足默认对称（MAD 更稳健）
    sk = float(skewness(clean.tolist()))
    if not np.isfinite(sk):
        return DIST_SYMMETRIC
    return DIST_SYMMETRIC if abs(sk) < SKEW_SYMMETRIC_THRESHOLD else DIST_SKEWED


def winsorize_mad(series: pd.Series, k: float = MAD_K) -> pd.Series:
    """MAD 截断（对称分布）：中位数 ± k * 1.4826 * MAD。

    Args:
        series: 因子值序列。
        k: 截断倍数（默认 3，等价 3σ）。

    Returns:
        截断后的序列（超出边界者缩至边界，边界本身为 NaN 时输出 NaN）。
    """
    clean = series.dropna()
    if len(clean) < 5:
        return series
    median_v = float(clean.median())
    mad = float(np.median(np.abs(clean - median_v)))
    if mad <= 0.0:
        return series  # 无离散度，无需截断
    lo = median_v - k * MAD_CONSTANT * mad
    hi = median_v + k * MAD_CONSTANT * mad
    return series.clip(lower=lo, upper=hi)


def winsorize_quantile(
    series: pd.Series,
    low: float = QUANTILE_LOW,
    high: float = QUANTILE_HIGH,
) -> pd.Series:
    """分位数截断（偏态分布）：[low, high] 分位数之外的极端值缩至边界。

    Args:
        series: 因子值序列。
        low / high: 分位数边界（默认 1%/99%）。

    Returns:
        截断后的序列。
    """
    clean = series.dropna()
    if len(clean) < 5:
        return series
    # math_utils.percentile 的 p 为 0-100 范围
    lo = float(percentile(clean.tolist(), low * 100.0))
    hi = float(percentile(clean.tolist(), high * 100.0))
    return series.clip(lower=lo, upper=hi)


def winsorize_adaptive(
    series: pd.Series,
) -> Tuple[pd.Series, str]:
    """自适应去极值：按分布特性选择方法（任务卡要求）。

    - 对称分布（|skew|<阈值）→ MAD 截断
    - 偏态分布 → 1%/99% 分位数截断
    - 波动率类因子不再固定 MAD 一刀切

    Args:
        series: 因子值序列。

    Returns:
        (截断后序列, 使用的方法标识 "mad"/"quantile")。
    """
    dist = detect_distribution(series)
    if dist == DIST_SKEWED:
        return winsorize_quantile(series), METHOD_QUANTILE
    return winsorize_mad(series), METHOD_MAD


# ═══════════════════════════════════════════════
#  2. 中性化（OLS 残差化）
# ═══════════════════════════════════════════════

def _ols_with_t_values(
    y: np.ndarray,
    x: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """numpy 最小二乘回归：返回 (残差, β, t 值)。

    se(β_j) = sqrt(MSE * diag((X'X)^-1))；t_j = β_j / se(β_j)。

    Args:
        y: 因变量（因子值，n×1）。
        x: 设计矩阵（含截距列，n×p）。

    Returns:
        (残差 n×1, β p×1, t 值 p×1)。
    """
    n, p = x.shape
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    residual = y - x @ beta
    df_resid = n - p
    if df_resid <= 0:
        return residual, beta, np.full(p, np.nan)
    mse = float(np.sum(residual ** 2) / df_resid)
    try:
        xtx_inv = np.linalg.inv(x.T @ x)
    except np.linalg.LinAlgError:
        xtx_inv = np.linalg.pinv(x.T @ x)
    se = np.sqrt(np.maximum(np.diag(xtx_inv) * mse, 0.0))
    t_values = np.divide(
        beta, se, out=np.full_like(beta, np.nan), where=np.abs(se) > 1e-12,
    )
    return residual, beta, t_values


def neutralize(
    factor_values: pd.Series,
    log_mv: Optional[pd.Series] = None,
    industry_map: Optional[Dict[str, str]] = None,
) -> Tuple[pd.Series, Dict[str, float], str]:
    """行业 + 对数市值中性化（OLS 回归取残差）。

    设计矩阵 X = [1, log_mv?, 行业哑变量(N-1)?]；
    残差 = y - Xβ，即为剔除行业/市值风格暴露后的因子值。
    任一暴露变量缺失的标的在回归中剔除（当前截面内），防御 NaN 传播。

    Args:
        factor_values: 因子值（index=symbol）。
        log_mv: 对数流通市值（index=symbol，可选）。
        industry_map: {symbol: 行业}（可选；行业数>=2 才加入哑变量）。

    Returns:
        (残差序列, 暴露 t 值 {变量名: t}, 口径说明 note)。
    """
    symbols0 = list(factor_values.dropna().index)
    if len(symbols0) < MIN_OBS_FOR_NEUTRALIZE:
        # 样本不足：仅中心化，不回归
        clean = factor_values - factor_values.mean()
        return clean, {}, "样本不足，仅中心化"

    notes: List[str] = []
    use_mv = False
    mv: Optional[pd.Series] = None
    if log_mv is not None:
        mv = log_mv.reindex(symbols0)
        use_mv = mv.notna().sum() >= MIN_OBS_FOR_NEUTRALIZE
        if not use_mv:
            notes.append("市值数据不足，跳过市值中性化")
    else:
        notes.append("未提供市值，跳过市值中性化")

    use_ind = False
    unique_inds: List[str] = []
    inds: List[str] = []
    if industry_map:
        inds = [str(industry_map.get(s, "")) for s in symbols0]
        unique_inds = sorted({i for i in inds if i})
        use_ind = len(unique_inds) >= 2
        if not use_ind:
            notes.append("行业类别<2，跳过行业中性化")
    else:
        notes.append("未提供行业映射，跳过行业中性化")

    if not use_mv and not use_ind:
        # 无暴露变量：残差 = 中心化
        residual = factor_values - factor_values.mean()
        note = "；".join(notes) if notes else "无暴露变量，仅中心化"
        return residual, {}, note

    # 过滤任一暴露变量缺失的标的（防御 NaN 传播）
    keep = np.ones(len(symbols0), dtype=bool)
    if use_mv and mv is not None:
        keep &= mv.notna().to_numpy()
    symbols = [s for s, k in zip(symbols0, keep) if k]
    if len(symbols) < MIN_OBS_FOR_NEUTRALIZE:
        clean = factor_values - factor_values.mean()
        return clean, {}, "有效样本不足，仅中心化"

    regressors: List[str] = ["const"]
    if use_mv:
        regressors.append("log_mv")
    if use_ind:
        for ind in unique_inds[1:]:
            # 行业缺失的标的在各哑变量均为 0（归入基准组）
            regressors.append(f"industry_{ind}")

    y = factor_values.loc[symbols].to_numpy(dtype=float)
    # 显式构造设计矩阵（首列截距）
    x = np.ones((len(symbols), len(regressors)), dtype=float)
    for j, rn in enumerate(regressors[1:], start=1):
        if rn == "log_mv" and mv is not None:
            x[:, j] = mv.loc[symbols].to_numpy(dtype=float)
        elif rn.startswith("industry_"):
            ind = rn[len("industry_"):]
            x[:, j] = np.array(
                [1.0 if str(industry_map.get(s, "")) == ind else 0.0
                 for s in symbols],
                dtype=float,
            )

    residual_np, _, t_values = _ols_with_t_values(y, x)
    exposure_t: Dict[str, float] = {}
    for j, rn in enumerate(regressors):
        tv = float(t_values[j])
        if np.isfinite(tv):
            exposure_t[rn] = round(tv, 4)
    residual = pd.Series(residual_np, index=symbols)
    note = "；".join(notes)
    return residual, exposure_t, note


# ═══════════════════════════════════════════════
#  3. 残差检验
# ═══════════════════════════════════════════════

def residual_diagnostics(
    residual: pd.Series,
    fitted: Optional[pd.Series] = None,
) -> Dict[str, Any]:
    """残差正态性（偏度/峰度）与异方差（残差幅值-拟合幅值相关）检验。

    Args:
        residual: 中性化残差序列。
        fitted: 拟合值序列（可选；缺失时仅做分布诊断）。

    Returns:
        {"residual_skew": ..., "residual_kurt": ..., "hetero_corr": ...,
         "flags": [告警...]}。
    """
    clean = residual.dropna()
    flags: List[str] = []
    if len(clean) < 5:
        return {"residual_skew": 0.0, "residual_kurt": 0.0, "hetero_corr": 0.0,
                "flags": ["残差样本不足，跳过诊断"]}
    vals = clean.tolist()
    sk = float(skewness(vals))
    kt = float(kurtosis(vals, excess=True))
    if abs(sk) >= SKEW_ALERT_THRESHOLD:
        flags.append(f"残差偏度 {sk:.3f} 超阈值（|skew|>={SKEW_ALERT_THRESHOLD}）")
    if kt >= KURT_ALERT_THRESHOLD:
        flags.append(f"残差峰度 {kt:.3f} 超阈值（厚尾>={KURT_ALERT_THRESHOLD}）")

    hetero_corr = 0.0
    if fitted is not None:
        f_clean = fitted.reindex(clean.index).dropna()
        common = clean.index.intersection(f_clean.index)
        if len(common) >= 10:
            a = np.abs(clean.loc[common].to_numpy(dtype=float))
            b = np.abs(f_clean.loc[common].to_numpy(dtype=float))
            if float(np.std(a)) > 0 and float(np.std(b)) > 0:
                hetero_corr = float(np.corrcoef(a, b)[0, 1])
                if abs(hetero_corr) >= HETERO_CORR_THRESHOLD:
                    flags.append(
                        f"残差-拟合幅值相关 {hetero_corr:.3f} 超阈值（异方差迹象）")
    return {
        "residual_skew": round(sk, 4),
        "residual_kurt": round(kt, 4),
        "hetero_corr": round(hetero_corr, 4),
        "flags": flags,
    }


# ═══════════════════════════════════════════════
#  4. 一站式预处理管线
# ═══════════════════════════════════════════════

def preprocess_panel(
    factor_panel: pd.DataFrame,
    market_cap: Optional[pd.Series] = None,
    industry_map: Optional[Dict[str, str]] = None,
    do_neutralize: bool = True,
) -> Tuple[pd.DataFrame, List[PreprocessReport]]:
    """因子面板一站式预处理（去极值 → 中性化 → 残差检验）。

    对面板中每个因子列独立执行：
      1. 自适应去极值（MAD/分位数）
      2. （do_neutralize=True 时）行业/市值中性化取残差
      3. 残差检验并生成 PreprocessReport

    Args:
        factor_panel: index=symbol, columns=factor 的原始因子面板。
        market_cap: 流通市值（index=symbol，可选；None 时跳过市值中性化）。
        industry_map: {symbol: 行业}（可选；None/类别<2 时跳过行业中性化）。
        do_neutralize: 是否执行中性化（False=仅去极值）。

    Returns:
        (预处理后面板, 每因子检验报告列表)。

    Raises:
        ValueError: factor_panel 为空或列名为空。
    """
    if factor_panel is None or factor_panel.empty:
        raise ValueError("factor_panel 不能为空")
    clean_panel = factor_panel.copy()
    reports: List[PreprocessReport] = []

    log_mv: Optional[pd.Series] = None
    if market_cap is not None and not market_cap.empty:
        mc = market_cap.reindex(factor_panel.index)
        mc = mc[mc > 0].dropna()
        if len(mc) >= MIN_OBS_FOR_NEUTRALIZE:
            log_mv = np.log(mc)

    for col in factor_panel.columns:
        raw = factor_panel[col].astype(float)
        # 1. 去极值（本行统计量仅基于当前截面，截至 t 时刻）
        winsorized, method = winsorize_adaptive(raw)
        n_raw = int(raw.notna().sum())
        n_valid = int(winsorized.notna().sum())
        n_winsorized = 0
        if n_raw > 0 and n_valid > 0:
            # 截断数 = 原始值超出截断边界范围的数量（与裁剪后不同值）
            diff = (raw != winsorized).sum()
            n_winsorized = int(diff)

        report_flags: List[str] = []
        note = f"去极值方法={method}"
        final = winsorized

        # 2. 中性化（本行回归仅基于当前截面，截至 t 时刻）
        if do_neutralize:
            res, exposure_t, n_note = neutralize(winsorized, log_mv, industry_map)
            final = res
            if exposure_t:
                sig = [f"{k}:t={v:.2f}" for k, v in exposure_t.items()
                       if k != "const" and abs(v) >= T_EXPOSURE_THRESHOLD]
                if sig:
                    report_flags.append("显著暴露: " + ", ".join(sig))
            note = f"{note}；{n_note}" if n_note else note
            # 3. 残差检验
            fitted = winsorized.reindex(final.index) - final  # 拟合 = y - 残差
            diag = residual_diagnostics(final, fitted)
            report_flags.extend(diag["flags"])
        else:
            exposure_t = {}
            diag = {"residual_skew": 0.0, "residual_kurt": 0.0, "hetero_corr": 0.0,
                    "flags": []}

        clean_panel[col] = final
        reports.append(PreprocessReport(
            factor_name=str(col),
            method=method,
            n_raw=n_raw,
            n_valid=n_valid,
            n_winsorized=n_winsorized,
            mean_before=round(float(raw.mean()), 6) if n_raw else 0.0,
            mean_after=round(float(final.mean()), 6) if len(final.dropna()) else 0.0,
            exposure_t=exposure_t,
            residual_skew=float(diag.get("residual_skew", 0.0)),
            residual_kurt=float(diag.get("residual_kurt", 0.0)),
            hetero_corr=float(diag.get("hetero_corr", 0.0)),
            flags=report_flags + diag.get("flags", []),
            note=note,
        ))

    return clean_panel, reports


__all__ = [
    "SKEW_SYMMETRIC_THRESHOLD",
    "MAD_K",
    "MAD_CONSTANT",
    "QUANTILE_LOW",
    "QUANTILE_HIGH",
    "MIN_OBS_FOR_NEUTRALIZE",
    "T_EXPOSURE_THRESHOLD",
    "DIST_SYMMETRIC",
    "DIST_SKEWED",
    "METHOD_MAD",
    "METHOD_QUANTILE",
    "PreprocessReport",
    "detect_distribution",
    "winsorize_mad",
    "winsorize_quantile",
    "winsorize_adaptive",
    "neutralize",
    "residual_diagnostics",
    "preprocess_panel",
]
