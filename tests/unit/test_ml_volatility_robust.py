# 文件名：tests/unit/test_ml_volatility_robust.py
"""
test_ml_volatility_robust.py - F-10 波动率稳健化与自适应测试
================================================================
覆盖《因子与机器学习层优化实施报告》阶段二 P1 第四项 F-10（回写#97）：

  1. Volatility method 三路径：
     - std（默认零变化）：与直接 rolling std(ddof=1) 逐值一致
     - ewma：RiskMetrics 方差递推 σ²_t = λ·σ²_{t-1} + (1−λ)·r²_{t-1}，
       λ 两档自适应（常态 0.94 / 极端日 |r|>3σ 降 0.85），数值手工复算
     - robust：滚动窗口 MAD 稳健估计（1.4826·median(|r−median(r)|)）
  2. winsorize 输出前去极值：expanding MAD 截断（边界只含截至 t 历史，
     防未来——后段 spike 不影响前段已输出值）
  3. ExtremeRegime：滚动绝对收益分位超阈值 → 1（极端行情区制标记）
  4. 默认零变化 / 特征层启用产出新列 / 流式 update 与批量一致 / 端到端
  5. 验收对比：合成股灾/熔断序列，EWMA 波动率相对真实波动的偏差
     < 固定 20 日 std 的偏差（"偏差收敛"入档）

运行方式:
  pytest tests/unit/test_ml_volatility_robust.py -v

设计原则:
  - 全部合成数据（确定性收益序列，零网络），pct_chg 列显式注入保证收益
    序列逐行可控、数值可手工复算
  - 独立临时 registry（端到端测试）
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import numpy as np
import pandas as pd
import pytest

from myquant.indicators import get_indicator
from myquant.indicators.volatility import (
    EWMA_LAMBDA_HIGH,
    EWMA_LAMBDA_NORMAL,
    EWMA_SIGMA_K,
    MAD_CONSTANT,
    ExtremeRegime,
    Volatility,
)
from myquant.ml.features import FeatureConfig, FeatureEngine
from myquant.ml.model_registry import ModelRegistry
from myquant.ml.training_pipeline import TrainingPipeline

from tests.conftest import make_ohlcv_df


# ═══════════════════════════════════════════════
#  合成数据构造
# ═══════════════════════════════════════════════

def _df_with_returns(r: Sequence[float]) -> pd.DataFrame:
    """构造带显式 pct_chg 列（百分比）的 OHLCV DataFrame。

    行 i（i>=1）的收益 = r[i-1]；行 0 收益 NaN（首日无涨跌，与 pct_change
    语义一致）——收益序列逐行可控，数值断言可手工复算。
    本行使用的数据截至 t 时刻：纯合成（无真实行情/网络）。
    """
    if not r:
        raise ValueError("收益序列不能为空")
    closes = [100.0]
    for ret in r:
        closes.append(closes[-1] * (1.0 + ret))
    df = make_ohlcv_df(closes=closes)
    df["pct_chg"] = [np.nan] + [float(v) * 100.0 for v in r]
    return df


def _calc(inst: Any, r: Sequence[float]) -> pd.Series:
    """指标批量计算并返回输出列（防御式包装）。"""
    df = _df_with_returns(r)
    res = inst.calculate(df)
    return res[inst.output_columns[0]]


def _stream(inst: Any, closes: Sequence[float]) -> List[float]:
    """逐 bar 流式 update（bar 只含收盘价，与批量 pct_chg 同收益语义）。"""
    out: List[float] = []
    for c in closes:
        out.append(float(inst.update({"收盘价": float(c)})[inst.output_columns[0]]))
    return out


def _regular_returns(n: int = 60, step: float = 0.01) -> List[float]:
    """低波交替收益序列（±step）。"""
    return [step if i % 2 == 0 else -step for i in range(n)]


def _crash_returns() -> List[float]:
    """低波 60 日 + 极端行情 20 日 + 恢复 15 日（股灾/熔断合成，确定性）。"""
    calm = _regular_returns(60)
    crash = [0.08, -0.09, 0.07, -0.06] * 5
    recover = _regular_returns(15)
    return calm + crash + recover


# ═══════════════════════════════════════════════
#  1. method 三路径：std 零变化 / ewma / robust
# ═══════════════════════════════════════════════

def test_std_method_zero_change() -> None:
    """method='std' 与直接 rolling std(ddof=1) 逐值一致（默认零变化）。"""
    r = _regular_returns(40)
    df = _df_with_returns(r)
    vol = Volatility(period=20).calculate(df)["volatility"]
    expected = pd.Series(r).rolling(window=20, min_periods=20).std(ddof=1)
    np.testing.assert_allclose(
        vol.dropna().to_numpy(), expected.dropna().to_numpy(), rtol=1e-9)


def test_ewma_numeric_replication() -> None:
    """ewma 递推数值对拍：σ²_t = λ·σ²_{t-1} + (1−λ)·r²_{t-1} 手工复算。"""
    r = [0.01, 0.01, 0.01, 0.01]
    vol = _calc(Volatility(period=20, method="ewma"), r)
    # 行0 NaN（首日无涨跌）；行1 递推种子不输出（NaN）；行2 起输出 σ
    assert np.isnan(vol.iloc[0]) and np.isnan(vol.iloc[1])
    # 恒常 0.01 收益：var 收敛 = 0.94*var + 0.06*r² → var = r² = 1e-4，σ = 0.01
    for i in range(2, 5):
        assert float(vol.iloc[i]) == pytest.approx(0.01)
    # 递推复算：var[2] = 0.94*var[1] + 0.06*r0²（var[1]=r0² 种子）
    var1 = r[0] ** 2
    var2 = EWMA_LAMBDA_NORMAL * var1 + (1.0 - EWMA_LAMBDA_NORMAL) * r[0] ** 2
    assert var2 == pytest.approx(1e-4)


def test_ewma_lambda_adaptive() -> None:
    """λ 自适应：极端收益日（|r|>3σ）λ 降档 → 波动率即时跳升。

    索引口径：行 i 的收益 = r[i-1]；var[i] 用行 i-1 的收益（= r[i-2]）更新。
    极端收益 r23=0.10 在行 24，其影响出现在 var[25]（用行 24 收益 r23）。
    """
    r = [0.01] * 23 + [0.10, 0.01]  # 25 个收益；r23=0.10 极端，r24 缓冲
    vol = _calc(Volatility(period=20, method="ewma"), r)
    # 行24：var 用行23 收益 r22=0.01（常态 λ=0.94）→ σ = 0.01
    assert float(vol.iloc[24]) == pytest.approx(0.01)
    # 行25：var 用行24 收益 r23=0.10（极端 → λ=0.85）→ σ = sqrt(0.85e-4 + 0.15*0.01)
    var25 = EWMA_LAMBDA_HIGH * 1e-4 + (1.0 - EWMA_LAMBDA_HIGH) * 0.10 ** 2
    sigma25 = float(np.sqrt(var25))
    assert float(vol.iloc[25]) == pytest.approx(sigma25)
    # 自适应响应：极端收益后一行波动率跳升 >3x（固定 std 需窗口滚动才逐步抬高）
    assert vol.iloc[25] > 3.0 * vol.iloc[24]


def test_robust_mad_replication() -> None:
    """robust 滚动 MAD 对拍：σ = 1.4826·median(|r−median(r)|)（窗口内）。"""
    r = [0.01, 0.02, -0.015, 0.03, 0.012]
    vol = _calc(Volatility(period=5, method="robust"), r)
    # 行5：窗口收益 r[0..4]（行1..5）→ med=0.012；|diff|=[0.002,0.008,0.027,0.018,0]
    med = float(np.median(r))
    mad = float(np.median(np.abs(np.asarray(r) - med)))
    expected = MAD_CONSTANT * mad
    assert expected == pytest.approx(1.4826 * 0.008)
    assert float(vol.iloc[5]) == pytest.approx(expected)


def test_method_invalid_defense() -> None:
    """method / winsorize_k 非法值显式报错（防御式）。"""
    with pytest.raises(ValueError, match="method"):
        Volatility(method="unknown")
    with pytest.raises(ValueError, match="winsorize_k"):
        Volatility(winsorize_k=0.0)


# ═══════════════════════════════════════════════
#  2. winsorize 输出前去极值（防未来）
# ═══════════════════════════════════════════════

def test_winsorize_clips_spike() -> None:
    """winsorize=True：极端收益导致的波动率尖峰被 expanding MAD 截断。

    前 4 个有效输出（expanding 样本 < WINSOR_MIN_PERIODS=5）防御式不截断；
    第 5 个有效输出起（样本 ≥5）尖峰段被压至边界内。
    """
    r = [0.01] * 15 + [0.50] + [0.01] * 10
    raw = _calc(Volatility(period=5), r)
    win = _calc(Volatility(period=5, winsorize=True), r)
    # 尖峰影响期内存在被截断的行（win < raw）
    assert bool((win < raw).any())
    # 全序列：winsorized 值 ≤ raw 同位置（不放大）
    mask = raw.notna() & win.notna()
    assert bool((win[mask] <= raw[mask] + 1e-12).all())


def test_winsorize_no_lookahead() -> None:
    """去极值防未来：后段 spike 不影响前段已输出值（expanding 边界因果）。"""
    r = [0.01] * 30 + [0.5, -0.45] * 5
    raw = _calc(Volatility(period=5), r)
    win = _calc(Volatility(period=5, winsorize=True), r)
    # 行20..30（spike 在行31 之后）：边界只含截至 t 历史 → 与 raw 完全一致
    np.testing.assert_allclose(
        win.iloc[20:31].to_numpy(), raw.iloc[20:31].to_numpy(), rtol=1e-12)


# ═══════════════════════════════════════════════
#  3. ExtremeRegime 极端行情区制标记
# ═══════════════════════════════════════════════

def test_extreme_regime_marking() -> None:
    """滚动绝对收益 99 分位超阈值 → 1；常规日 → 0。"""
    r = [0.01] * 20 + [0.10]
    regime = _calc(ExtremeRegime(period=20, quantile=0.99), r)
    # 行20（窗口 r[0..19] 全 0.01）：|0.01| > 0.01 否 → 0
    assert float(regime.iloc[20]) == 0.0
    # 行21（窗口 r[1..20] 含 0.10）：分位≈0.0991 < 0.10 → 1
    assert float(regime.iloc[21]) == 1.0
    # 窗口不足（前 20 行）→ NaN
    assert np.isnan(regime.iloc[0])


def test_extreme_regime_defense() -> None:
    """ExtremeRegime 参数防御：period/quantile 越界显式报错。"""
    with pytest.raises(ValueError, match="period"):
        ExtremeRegime(period=3)
    with pytest.raises(ValueError, match="quantile"):
        ExtremeRegime(quantile=0.5)
    with pytest.raises(ValueError, match="quantile"):
        ExtremeRegime(quantile=1.0)


def test_regime_streaming_consistency() -> None:
    """ExtremeRegime 流式 update 与批量一致（0/1 标记逐值对拍）。"""
    r = [0.01] * 20 + [0.10]
    df = _df_with_returns(r)
    batch = ExtremeRegime(period=20).calculate(df)["regime"]
    streamed = _stream(ExtremeRegime(period=20), df["收盘价"].tolist())
    assert len(streamed) == len(batch)
    for i in range(len(batch)):
        b, s = float(batch.iloc[i]), streamed[i]
        if np.isnan(b):
            assert np.isnan(s)
        else:
            assert s == pytest.approx(b)


# ═══════════════════════════════════════════════
#  4. 流式与批量一致性 / 特征层接入 / 端到端
# ═══════════════════════════════════════════════

def test_volatility_stream_batch_consistency() -> None:
    """std 与 ewma 双路径：流式 update 与批量 calculate 逐值一致。"""
    r = _regular_returns(40)
    df = _df_with_returns(r)
    closes = df["收盘价"].tolist()
    for method in ("std", "ewma"):
        batch = Volatility(period=20, method=method).calculate(df)["volatility"]
        streamed = _stream(Volatility(period=20, method=method), closes)
        assert len(streamed) == len(batch)
        for i in range(len(batch)):
            b, s = float(batch.iloc[i]), streamed[i]
            if np.isnan(b):
                assert np.isnan(s)
            else:
                assert s == pytest.approx(b, rel=1e-6)


def test_feature_default_zero_change() -> None:
    """默认 FeatureConfig：vol_20_volatility 存在、无 regime 列（零变化）。"""
    df = _df_with_returns(_regular_returns(60))
    feat = FeatureEngine(FeatureConfig()).compute_features(df)
    assert "vol_20_volatility" in feat.columns
    assert not [c for c in feat.columns if c.startswith("regime_")]


def test_feature_enabled_columns() -> None:
    """启用 F-10：ewma+winsorize 波动率 + extreme_regime → 产出新列。"""
    base = FeatureConfig()
    cfgs = [dict(c) for c in base.indicator_configs]
    for c in cfgs:
        if c["name"] == "volatility":
            c["params"] = {"period": 20, "method": "ewma", "winsorize": True}
    cfgs.append({
        "name": "extreme_regime",
        "params": {"period": 20, "quantile": 0.99},
        "prefix": "regime",
    })
    cfg = FeatureConfig(indicator_configs=cfgs)
    df = _df_with_returns(_regular_returns(60))
    feat = FeatureEngine(cfg).compute_features(df)
    assert "vol_20_volatility" in feat.columns
    assert "regime_regime" in feat.columns
    assert feat["regime_regime"].dropna().isin([0.0, 1.0]).all()


def test_e2e_training_with_robust_config(tmp_path: Any) -> None:
    """端到端：启用 F-10 配置 → run 注册成功，特征列含新列。"""
    base = FeatureConfig()
    cfgs = [dict(c) for c in base.indicator_configs]
    cfgs.append({
        "name": "extreme_regime",
        "params": {"period": 20, "quantile": 0.99},
        "prefix": "regime",
    })
    cfg = FeatureConfig(indicator_configs=cfgs)
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    pipe = TrainingPipeline(feature_config=cfg, registry=registry)
    result = pipe.run(_df_with_returns(_regular_returns(160)), model_name="robust_model")
    meta = registry.get_model_metadata("robust_model", result.version)
    assert "regime_regime" in meta.feature_columns
    assert "max_drawdown" in meta.metrics
    loaded = registry.load_model("robust_model", result.version)
    assert loaded is not None


# ═══════════════════════════════════════════════
#  5. 验收：股灾/熔断区间偏差收敛对比
# ═══════════════════════════════════════════════

def test_acceptance_crash_window_bias_converges() -> None:
    """验收对比：极端行情**恢复段** EWMA 波动率相对真实波动偏差 < 固定 20 日 std。

    报告问题点：固定 20 日 std 在极端行情下"估计滞后且残留高企"——股灾结束后
    20 日窗口仍含极端收益，波动率残留高企；EWMA（λ 递推指数衰减）回落更快、
    偏差收敛。"真实波动"基准 = 5 日滚动 std（接近近期真实波动）。
    恢复段（行 80..94）对比 MAE 入档。
    """
    r = pd.Series(_crash_returns(), dtype=float)
    real = r.rolling(5, min_periods=5).std(ddof=1)
    std20 = r.rolling(20, min_periods=20).std(ddof=1)
    ewma = _calc(Volatility(period=20, method="ewma"), r.tolist())
    # 指标输出比收益序列多 1 行（首行 NaN）：取尾部对齐收益索引
    ewma = pd.Series(ewma.to_numpy()[-len(r):], index=r.index)
    recover = r.index[80:]
    mae_std = float(np.nanmean((std20[recover] - real[recover]).abs()))
    mae_ewma = float(np.nanmean((ewma[recover] - real[recover]).abs()))
    assert np.isfinite(mae_std) and np.isfinite(mae_ewma)
    assert mae_ewma < mae_std
