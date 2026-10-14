# 文件名：tests/unit/test_factor_preprocessor.py
"""
test_factor_preprocessor.py - F-03 因子预处理（去极值+中性化+残差检验）测试
==================================================================
覆盖：
  1. 分布形态判定（对称/偏态/样本不足）
  2. MAD 截断（极端值裁剪、常量序列不变）
  3. 分位数截断（1%/99% 边界）
  4. 自适应方法选择（对称→MAD，偏态→分位数）
  5. 市值中性化（残差后与市值相关≈0）
  6. 行业中性化（残差后行业组均值差≈0）
  7. 无暴露数据/样本不足降级（仅中心化）
  8. 暴露 t 值（强暴露被识别）
  9. 残差诊断（正态无告警、异方差告警）
  10. PreprocessReport 序列化往返 + 非法输入
  11. preprocess_panel 管线（仅去极值 / 全流程 / 空面板报错）
  12. MultiFactorStrategy 集成（默认 none 零变化、winsorize、neutralize、
      非法 mode 报错、流通市值现算）
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd
import pytest

from myquant.indicators.factor_preprocessor import (
    DIST_SKEWED,
    DIST_SYMMETRIC,
    METHOD_MAD,
    METHOD_QUANTILE,
    MIN_OBS_FOR_NEUTRALIZE,
    PreprocessReport,
    detect_distribution,
    neutralize,
    preprocess_panel,
    residual_diagnostics,
    winsorize_adaptive,
    winsorize_mad,
    winsorize_quantile,
)
from myquant.leakage_gate.cross_sectional import CrossSectionalAsOfError
from myquant.strategies.multi_factor_strategy import MultiFactorStrategy

# ═══════════════════════════════════════════════
#  测试辅助
# ═══════════════════════════════════════════════

def _make_factor_series(
    n: int = 12, seed: int = 7,
) -> Dict[str, pd.DataFrame]:
    """构造 n 只标的 × 4 基础因子的 trailing 因子序列字典"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    out: Dict[str, pd.DataFrame] = {}
    for i in range(n):
        sym = f"TEST{i:04d}"
        out[sym] = pd.DataFrame(
            {
                "momentum": rng.normal(0.01, 0.05, 30).cumsum(),
                "reversal": rng.normal(0.0, 0.03, 30).cumsum(),
                "low_vol": np.abs(rng.normal(0.02, 0.005, 30)),
                "vol_price": rng.normal(0.0, 0.02, 30).cumsum(),
            },
            index=idx,
        )
    return out


def _make_market_cap(symbols: List[str], seed: int = 3) -> Dict[str, float]:
    rng = np.random.default_rng(seed)
    return {s: float(rng.uniform(1e9, 5e11)) for s in symbols}


# ═══════════════════════════════════════════════
#  1. 分布形态判定
# ═══════════════════════════════════════════════

def test_detect_distribution_symmetric() -> None:
    """正态样本 → symmetric（MAD 截断）"""
    rng = np.random.default_rng(0)
    s = pd.Series(rng.normal(0, 1.0, 500))
    assert detect_distribution(s) == DIST_SYMMETRIC


def test_detect_distribution_skewed() -> None:
    """指数分布（强右偏）→ skewed（分位数截断）"""
    rng = np.random.default_rng(1)
    s = pd.Series(rng.exponential(1.0, 500))
    assert detect_distribution(s) == DIST_SKEWED


def test_detect_distribution_few_samples() -> None:
    """样本 <5 → 默认 symmetric（MAD 更稳健）"""
    s = pd.Series([1.0, 2.0, 3.0])
    assert detect_distribution(s) == DIST_SYMMETRIC


# ═══════════════════════════════════════════════
#  2. MAD 截断
# ═══════════════════════════════════════════════

def test_winsorize_mad_clips_extreme() -> None:
    """极端值被截断到 MAD 边界内，正常值不变"""
    vals = list(np.linspace(1.0, 10.0, 40)) + [1000.0]
    s = pd.Series(vals)
    out = winsorize_mad(s)
    assert out.max() < 100.0          # 1000 被截断
    assert out.min() >= 0.0           # 下界未伤及正常值
    assert abs(out.median() - 5.5) < 0.5  # 中位数不受极端值影响
    assert out.iloc[-1] == out.max()  # 极端值缩至边界


def test_winsorize_mad_constant_series() -> None:
    """无离散度（MAD=0）→ 原样返回"""
    s = pd.Series([3.0] * 20)
    out = winsorize_mad(s)
    assert (out == 3.0).all()


# ═══════════════════════════════════════════════
#  3. 分位数截断
# ═══════════════════════════════════════════════

def test_winsorize_quantile_clips() -> None:
    """右偏序列：99% 之外的极端值被截断，P1/P99 内正常值不变"""
    rng = np.random.default_rng(5)
    vals = list(rng.exponential(1.0, 300)) + [50.0, 80.0]
    s = pd.Series(vals)
    out = winsorize_quantile(s)
    assert out.max() < 50.0                 # 50/80 被截断
    p99 = float(np.percentile(s.dropna(), 99))
    assert out.max() <= p99 + 1e-9          # 上界=99% 分位
    p01 = float(np.percentile(s.dropna(), 1))
    assert out.min() >= p01 - 1e-9          # 下界=1% 分位


# ═══════════════════════════════════════════════
#  4. 自适应去极值
# ═══════════════════════════════════════════════

def test_winsorize_adaptive_symmetric_uses_mad() -> None:
    """对称分布 → 选 MAD 截断"""
    rng = np.random.default_rng(6)
    s = pd.Series(rng.normal(0, 1.0, 400))
    out, method = winsorize_adaptive(s)
    assert method == METHOD_MAD
    assert out.max() < 5.0  # 极端值被截


def test_winsorize_adaptive_skewed_uses_quantile() -> None:
    """偏态分布 → 选分位数截断（波动率类因子不再固定 MAD）"""
    rng = np.random.default_rng(7)
    s = pd.Series(rng.exponential(1.0, 400))
    out, method = winsorize_adaptive(s)
    assert method == METHOD_QUANTILE


# ═══════════════════════════════════════════════
#  5. 中性化
# ═══════════════════════════════════════════════

def test_neutralize_removes_market_cap() -> None:
    """因子=3×log市值+噪声：残差后与市值相关≈0，市值暴露被识别"""
    rng = np.random.default_rng(1)
    symbols = [f"S{i:04d}" for i in range(40)]
    log_mv = pd.Series(rng.uniform(10, 14, 40), index=symbols)
    noise = pd.Series(rng.normal(0, 0.01, 40), index=symbols)
    factor = pd.Series(3.0 * log_mv + noise, index=symbols)
    res, exposure_t, note = neutralize(factor, log_mv)
    common = res.index.intersection(log_mv.index)
    assert len(common) >= 30
    corr = res.loc[common].corr(log_mv.loc[common])
    assert abs(corr) < 0.1
    assert "log_mv" in exposure_t
    assert abs(exposure_t["log_mv"]) >= 2.0  # 回归系数显著


def test_neutralize_industry_dummies() -> None:
    """两行业：残差后行业组均值差≈0"""
    rng = np.random.default_rng(2)
    a = [f"A{i:04d}" for i in range(20)]
    b = [f"B{i:04d}" for i in range(20)]
    industry_map = {s: "行业A" for s in a}
    industry_map.update({s: "行业B" for s in b})
    noise = pd.Series(rng.normal(0, 0.01, 40),
                      index=a + b)
    factor = pd.Series(0.0, index=a + b)
    for s in b:
        factor[s] = 2.0
    factor = factor + noise
    res, exposure_t, _ = neutralize(factor, None, industry_map)
    mean_a = res.loc[a].mean()
    mean_b = res.loc[b].mean()
    assert abs(mean_a - mean_b) < 0.1
    assert any(k.startswith("industry_") for k in exposure_t)


def test_neutralize_no_exposure_data() -> None:
    """无市值/行业 → 仅中心化，不崩"""
    rng = np.random.default_rng(3)
    symbols = [f"S{i:04d}" for i in range(20)]
    factor = pd.Series(rng.normal(5, 1.0, 20), index=symbols)
    res, exposure_t, note = neutralize(factor)
    assert exposure_t == {}
    assert abs(res.mean()) < 1e-9  # 中心化后均值为 0
    assert "未提供市值" in note  # 降级口径说明


def test_neutralize_few_samples() -> None:
    """样本 < 阈值 → 仅中心化 + 口径说明"""
    rng = np.random.default_rng(4)
    symbols = [f"S{i:04d}" for i in range(MIN_OBS_FOR_NEUTRALIZE - 1)]
    factor = pd.Series(rng.normal(1, 0.5, len(symbols)), index=symbols)
    res, exposure_t, note = neutralize(factor)
    assert exposure_t == {}
    assert "样本不足" in note
    assert abs(res.mean()) < 1e-9


# ═══════════════════════════════════════════════
#  6. 残差诊断
# ═══════════════════════════════════════════════

def test_residual_diagnostics_normal_no_flags() -> None:
    """正态残差 → 无告警"""
    rng = np.random.default_rng(8)
    n = 300
    res = pd.Series(rng.normal(0, 0.1, n))
    fitted = pd.Series(rng.normal(0, 0.1, n))
    diag = residual_diagnostics(res, fitted)
    assert diag["flags"] == []
    assert abs(diag["residual_skew"]) < 1.0
    assert diag["hetero_corr"] < 0.3


def test_residual_diagnostics_hetero_flags() -> None:
    """残差幅值随拟合值增大（异方差）→ 告警"""
    rng = np.random.default_rng(9)
    n = 400
    x = np.linspace(0.1, 5.0, n)
    fitted = pd.Series(x)
    res = pd.Series(x * rng.normal(0, 0.2, n) + rng.normal(0, 0.02, n))
    diag = residual_diagnostics(res, fitted)
    assert any("异方差" in f for f in diag["flags"]) \
        or diag["hetero_corr"] >= 0.3


def test_residual_diagnostics_few_samples() -> None:
    """残差样本不足 → 跳过诊断并说明"""
    res = pd.Series([1.0, 2.0, 3.0])
    diag = residual_diagnostics(res)
    assert any("样本不足" in f for f in diag["flags"])


# ═══════════════════════════════════════════════
#  7. PreprocessReport 序列化
# ═══════════════════════════════════════════════

def test_preprocess_report_roundtrip() -> None:
    """to_dict/from_dict 往返等价"""
    rep = PreprocessReport(
        factor_name="momentum",
        method=METHOD_MAD,
        n_raw=40,
        n_valid=40,
        n_winsorized=2,
        mean_before=1.0,
        mean_after=0.98,
        exposure_t={"log_mv": 1.2, "industry_行业B": -0.8},
        residual_skew=0.1,
        residual_kurt=0.2,
        hetero_corr=0.05,
        flags=["去极值方法=mad"],
        note="测试",
    )
    rep2 = PreprocessReport.from_dict(rep.to_dict())
    assert rep2 == rep
    d = rep.to_dict()
    assert isinstance(d["exposure_t"], dict)
    assert isinstance(d["flags"], list)


def test_preprocess_report_from_dict_invalid() -> None:
    """非法输入（非 dict / 缺 factor_name）→ ValueError"""
    with pytest.raises(ValueError):
        PreprocessReport.from_dict({})
    with pytest.raises(ValueError):
        PreprocessReport.from_dict("bad")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════
#  8. preprocess_panel 管线
# ═══════════════════════════════════════════════

def _make_panel(n: int = 40, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    symbols = [f"S{i:04d}" for i in range(n)]
    log_mv = rng.uniform(10, 14, n)
    rows: Dict[str, Dict[str, float]] = {}
    for i, s in enumerate(symbols):
        rows[s] = {
            "market_linked": 2.0 * log_mv[i] + rng.normal(0, 0.05),
            "skewed_factor": rng.exponential(1.0),
            "normal_factor": rng.normal(0, 1.0),
        }
    return pd.DataFrame(rows).T  # index=symbol, columns=factor


def test_preprocess_panel_winsorize_only() -> None:
    """do_neutralize=False → 仅去极值，报告含方法"""
    panel = _make_panel()
    clean, reports = preprocess_panel(panel, do_neutralize=False)
    assert len(reports) == panel.shape[1]
    assert {r.method for r in reports} <= {METHOD_MAD, METHOD_QUANTILE}
    # 仅去极值：不产生暴露 t 值
    assert all(r.exposure_t == {} for r in reports)
    # 输出面板列数与顺序不变
    assert list(clean.columns) == list(panel.columns)
    assert set(clean.index) == set(panel.index)


def test_preprocess_panel_neutralize_full() -> None:
    """全流程（去极值+中性化+检验）：报告含市值/行业暴露与诊断"""
    panel = _make_panel(40, seed=12)
    symbols = list(panel.index)
    mcap = pd.Series(np.exp(np.log(np.array([
        1e10 + i * 1e9 for i in range(len(symbols))]))), index=symbols)
    industry_map = {s: ("行业A" if i < 20 else "行业B")
                    for i, s in enumerate(symbols)}
    clean, reports = preprocess_panel(
        panel, market_cap=mcap, industry_map=industry_map,
        do_neutralize=True,
    )
    assert len(reports) == 3
    by_name = {r.factor_name: r for r in reports}
    mr = by_name["market_linked"]
    # 强市值暴露因子：残差后市值暴露 t 应不显著（|t|<2，验收口径）
    assert abs(mr.exposure_t.get("log_mv", 0.0)) < 2.0
    assert "industry_行业B" in mr.exposure_t
    # 报告 flags 为列表、note 含口径
    assert isinstance(mr.flags, list)
    assert "去极值方法" in mr.note
    # 残差诊断字段齐全
    assert 0.0 <= mr.hetero_corr <= 1.0 or mr.hetero_corr == 0.0
    assert isinstance(mr.residual_skew, float)


def test_preprocess_panel_empty_raises() -> None:
    """空面板 → ValueError"""
    with pytest.raises(ValueError):
        preprocess_panel(pd.DataFrame())


# ═══════════════════════════════════════════════
#  9. MultiFactorStrategy 集成
# ═══════════════════════════════════════════════

def test_strategy_default_none_zero_change() -> None:
    """默认 preprocess_mode="none"：现有行为零变化（与手工 z-score 对拍）"""
    fs = _make_factor_series(12, seed=21)
    mf = MultiFactorStrategy()
    status = mf.get_multi_factor_status()
    assert status["preprocess_mode"] == "none"
    assert status["last_preprocess_report"] is None

    scores, active = mf._standardize_base_factors(fs)
    assert sorted(active) == sorted(list(MultiFactorStrategy.BASE_FACTORS))
    # 与手工 pandas 对拍（z=(x-mean)/std, ddof=0）
    latest = {s: df.iloc[-1] for s, df in fs.items()}
    raw = pd.DataFrame(latest).T
    for f in MultiFactorStrategy.BASE_FACTORS:
        manual = (raw[f] - raw[f].mean()) / raw[f].std(ddof=0)
        for sym in raw.index:
            assert scores[sym][f] == pytest.approx(float(manual[sym]), abs=1e-9)


def test_strategy_winsorize_integration() -> None:
    """preprocess_mode="winsorize"：仅去极值，报告记录方法"""
    fs = _make_factor_series(20, seed=22)
    mf = MultiFactorStrategy(preprocess_mode="winsorize")
    scores, active = mf._standardize_base_factors(fs, None)
    assert len(active) == len(MultiFactorStrategy.BASE_FACTORS)
    assert scores
    status = mf.get_multi_factor_status()
    rep = status["last_preprocess_report"]
    assert rep is not None
    assert rep["mode"] == "winsorize"
    methods = {r["method"] for r in rep["factors"]}
    assert methods <= {METHOD_MAD, METHOD_QUANTILE}
    # 无市值/行业 → 不产生暴露 t
    assert all(r["exposure_t"] == {} for r in rep["factors"])


def test_strategy_neutralize_integration() -> None:
    """preprocess_mode="neutralize"：去极值+市值/行业中性化+检验报告"""
    fs = _make_factor_series(20, seed=23)
    symbols = list(fs.keys())
    mcap = _make_market_cap(symbols, seed=24)
    industry_map = {s: ("行业A" if i < 10 else "行业B")
                    for i, s in enumerate(symbols)}
    mf = MultiFactorStrategy(
        preprocess_mode="neutralize",
        industry_map=industry_map,
    )
    scores, active = mf._standardize_base_factors(fs, mcap)
    assert len(active) == len(MultiFactorStrategy.BASE_FACTORS)
    assert scores
    status = mf.get_multi_factor_status()
    rep = status["last_preprocess_report"]
    assert rep is not None
    assert rep["mode"] == "neutralize"
    assert rep["industry_map_size"] == len(industry_map)
    # 市值/行业足够 → 报告应含暴露 t 值记录
    assert any(r["exposure_t"] for r in rep["factors"])


def test_strategy_invalid_mode_raises() -> None:
    """非法 preprocess_mode → ValueError"""
    with pytest.raises(ValueError):
        MultiFactorStrategy(preprocess_mode="bogus")


# ═══════════════════════════════════════════════
#  P1-07④ 横截面 as-of 兜底（validate_asof 接线）
# ═══════════════════════════════════════════════

def _inject_future_row(fs: Dict[str, pd.DataFrame], sym: str) -> Dict[str, pd.DataFrame]:
    """给指定标的的因子序列追加晚于决策时点（2026-10-10）的未来行。"""
    idx = pd.date_range("2026-12-01", periods=2, freq="B")
    extra = pd.DataFrame(
        {f: [0.1, 0.2] for f in MultiFactorStrategy.BASE_FACTORS},
        index=idx,
    )
    fs = dict(fs)
    fs[sym] = pd.concat([fs[sym], extra])
    return fs


def test_zscore_cross_sectional_asof_future_blocked() -> None:
    """截面因子序列含未来日期 → _zscore_cross_sectional 抛错阻断该截面。"""
    fs = _inject_future_row(_make_factor_series(12, seed=31), "TEST0000")
    mf = MultiFactorStrategy()
    with pytest.raises(CrossSectionalAsOfError, match="as-of 校验失败"):
        mf._zscore_cross_sectional(
            fs, list(MultiFactorStrategy.BASE_FACTORS), decision_time="2026-10-10",
        )


def test_zscore_cross_sectional_asof_clean() -> None:
    """全部序列索引 <= 决策日 → 正常合成当日截面。"""
    fs = _make_factor_series(12, seed=32)
    mf = MultiFactorStrategy()
    scores, active = mf._zscore_cross_sectional(
        fs, list(MultiFactorStrategy.BASE_FACTORS), decision_time="2026-10-10",
    )
    assert sorted(active) == sorted(list(MultiFactorStrategy.BASE_FACTORS))
    assert len(scores) == 12


def test_standardize_base_factors_asof_future_blocked() -> None:
    """_standardize_base_factors 透传 decision_time：含未来行经完整调用链亦阻断。"""
    fs = _inject_future_row(_make_factor_series(12, seed=33), "TEST0000")
    mf = MultiFactorStrategy()
    with pytest.raises(CrossSectionalAsOfError, match="as-of 校验失败"):
        mf._standardize_base_factors(fs, None, decision_time="2026-10-10")


def test_strategy_market_cap_calc() -> None:
    """流通市值现算：收盘价 × 流通股本（trailing 最新值）"""
    mf = MultiFactorStrategy()
    df = pd.DataFrame(
        {"收盘价": [10.0, 11.0], "流通股本": [1e8, 1.1e8]},
    )
    assert mf._calc_market_cap(df) == pytest.approx(11.0 * 1.1e8)
    # 缺流通股本 → None
    df2 = pd.DataFrame({"收盘价": [10.0]})
    assert mf._calc_market_cap(df2) is None
    # 空数据 → None
    assert mf._calc_market_cap(pd.DataFrame()) is None


__all__: List[str] = []
