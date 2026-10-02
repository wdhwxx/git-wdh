# 文件名：tests/unit/test_factor_validator.py
"""
test_factor_validator.py - F-02 因子有效性校验（IC/ICIR）单元测试
================================================================
覆盖《因子与机器学习层优化实施报告》F-02（IC/ICIR 因子有效性校验）：

  1. build_panel：{symbol: Series} → date×symbol 面板对齐
  2. compute_ic_series：强相关因子 → 高 IC；随机因子 → 近 0 IC；
     RankIC（spearman）与 PearsonIC（pearson）双口径
  3. ICIR / t 统计量 / 胜率数值正确性（手算 pandas 对拍）
  4. 衰减曲线（1/5/10/20 持有期 IC）
  5. 分层收益与换手
  6. decide_status：active / observe / eliminated 三态判定
  7. ic_weighted_weights：方向校正 / 负贡献归零 / 下限约束 / 归一化
  8. validate_factor_logic：内置因子通过；缺省经济逻辑拒绝
  9. FactorProfile 序列化往返
  10. MultiFactorStrategy 集成：IC 加权权重可追溯；默认 False 零变化；
      额外因子缺 economic_logic 拒绝进组合

运行方式:
  pytest tests/unit/test_factor_validator.py -v

设计原则:
  - 全部合成数据（确定性随机），零网络依赖
  - IC/ICIR/t 用手算 pandas 对拍，不目测、不凑数
  - 防未来：评估标签（未来收益）仅用于档案校验，与信号路径隔离
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import pytest

from myquant.indicators.factor_validator import (
    FACTOR_STATUS_ACTIVE,
    FACTOR_STATUS_ELIMINATED,
    FACTOR_STATUS_OBSERVE,
    FactorProfile,
    build_panel,
    compute_forward_returns,
    compute_ic_series,
    decide_status,
    evaluate_factor,
    factor_turnover,
    ic_weighted_weights,
    layer_returns,
    validate_factor_logic,
)
from myquant.strategies.multi_factor_strategy import MultiFactorStrategy


# ═══════════════════════════════════════════════
#  Fixture：合成面板数据
# ═══════════════════════════════════════════════

def _make_panel(
    n_dates: int = 80, n_symbols: int = 40, seed: int = 7,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """构造 日期×标的 面板：close 随机游走 + 强相关因子。

    Returns:
        (dates_index, close_panel, factor_panel)
        factor_panel[t,s] ≈ forward_return[t,s] + 噪声（与未来收益强相关，
        用于检验 IC 计算正确性）。
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-02", periods=n_dates, freq="B")
    symbols = [f"S{i:03d}" for i in range(n_symbols)]
    rets = rng.normal(0.0005, 0.02, size=(n_dates, n_symbols))
    close = pd.DataFrame(
        100.0 * np.exp(np.cumsum(rets, axis=0)),
        index=dates, columns=symbols,
    )
    forward = compute_forward_returns(close, 5)
    # 信号放大的强相关因子：factor = 3*forward + 小噪声 → IC 显著高
    noise = pd.DataFrame(
        rng.normal(0, 0.05, size=(n_dates, n_symbols)),
        index=dates, columns=symbols,
    )
    factor = forward.fillna(0.0) * 3.0 + noise
    return dates, close, factor


def _make_random_factor(
    dates: pd.DatetimeIndex, symbols: List[str], seed: int = 2,
) -> pd.DataFrame:
    """随机因子（与未来收益无关，IC 应接近 0）"""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        rng.uniform(-1, 1, size=(len(dates), len(symbols))),
        index=dates, columns=symbols,
    )


def _make_profile(
    name: str, icir: float, ic_mean: float = 0.03, win_rate: float = 0.60,
) -> FactorProfile:
    """构造测试用 FactorProfile"""
    return FactorProfile(
        factor_name=name,
        economic_logic="测试经济逻辑",
        sample_start="2024-01-02",
        sample_end="2024-06-30",
        n_periods=60,
        ic_mean=ic_mean,
        ic_std=0.1,
        icir=icir,
        t_stat=icir * np.sqrt(60),
        win_rate=win_rate,
        pearson_ic_mean=ic_mean,
        layer_returns={},
        turnover=0.3,
        decay={},
        status=FACTOR_STATUS_ACTIVE,
        thresholds={},
        created_at="2024-07-01 00:00:00",
    )


# ═══════════════════════════════════════════════
#  1. build_panel
# ═══════════════════════════════════════════════

def test_build_panel() -> None:
    """{symbol: Series} → date×symbol 面板"""
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    panel = build_panel({
        "a": pd.Series([1.0, 2.0, 3.0], index=idx),
        "b": pd.Series([4.0, 5.0, 6.0], index=idx),
    })
    assert panel.shape == (3, 2)
    assert list(panel.columns) == ["a", "b"]


def test_build_panel_empty() -> None:
    """空输入返回空面板"""
    assert build_panel({}).empty


# ═══════════════════════════════════════════════
#  2. compute_ic_series
# ═══════════════════════════════════════════════

def test_compute_ic_series_strong() -> None:
    """强相关因子 → 高 IC（主口径 RankIC）"""
    _, close, factor = _make_panel(seed=7)
    forward = compute_forward_returns(close, 5)
    ic = compute_ic_series(factor, forward, method="spearman").dropna()
    assert len(ic) > 30
    assert float(ic.mean()) > 0.5


def test_compute_ic_series_random() -> None:
    """随机因子 → IC 接近 0"""
    dates, close, _ = _make_panel(seed=7)
    forward = compute_forward_returns(close, 5)
    rnd = _make_random_factor(dates, list(close.columns), seed=2)
    ic = compute_ic_series(rnd, forward, method="spearman").dropna()
    assert len(ic) > 0
    assert abs(float(ic.mean())) < 0.1


def test_rank_vs_pearson_dual() -> None:
    """RankIC 与 PearsonIC 双口径均产出"""
    _, close, factor = _make_panel(seed=7)
    forward = compute_forward_returns(close, 5)
    ic_rank = compute_ic_series(factor, forward, method="spearman").dropna()
    ic_pear = compute_ic_series(factor, forward, method="pearson").dropna()
    assert len(ic_rank) == len(ic_pear)
    # 强相关场景下两口径均显著为正
    assert float(ic_rank.mean()) > 0.5
    assert float(ic_pear.mean()) > 0.5


def test_compute_ic_series_invalid_method() -> None:
    """非法 method 显式报错"""
    _, close, factor = _make_panel(seed=7)
    forward = compute_forward_returns(close, 5)
    with pytest.raises(ValueError, match="method"):
        compute_ic_series(factor, forward, method="kendall")


# ═══════════════════════════════════════════════
#  3. ICIR / t / 胜率 数值正确性（手算对拍）
# ═══════════════════════════════════════════════

def test_icir_t_winrate_matches_manual() -> None:
    """evaluate_factor 输出与手算 pandas 对拍"""
    _, close, factor = _make_panel(seed=7)
    profile = evaluate_factor(
        "mom_test", factor_panel=factor, close_panel=close,
        horizon=5, economic_logic="测试逻辑",
    )
    assert profile is not None
    forward = compute_forward_returns(close, 5)
    ic = compute_ic_series(factor, forward, method="spearman").dropna()
    ic_mean = float(ic.mean())
    ic_std = float(ic.std(ddof=1))
    assert profile.ic_mean == pytest.approx(ic_mean, abs=1e-4)
    assert profile.icir == pytest.approx(ic_mean / ic_std, abs=1e-3)
    assert profile.t_stat == pytest.approx(
        (ic_mean / ic_std) * np.sqrt(len(ic)), abs=1e-3)
    assert profile.win_rate == pytest.approx(
        float((ic > 0).sum()) / len(ic), abs=1e-3)
    assert profile.n_periods == len(ic)
    assert profile.sample_start == str(ic.index[0])[:10]
    assert profile.status in (FACTOR_STATUS_ACTIVE, FACTOR_STATUS_OBSERVE)


# ═══════════════════════════════════════════════
#  4. 衰减曲线
# ═══════════════════════════════════════════════

def test_decay_curve() -> None:
    """衰减曲线包含 1/5/10/20 持有期"""
    _, close, factor = _make_panel(seed=7)
    profile = evaluate_factor(
        "decay_test", factor_panel=factor, close_panel=close,
        horizon=5, economic_logic="测试逻辑",
    )
    assert profile is not None
    assert set(profile.decay.keys()) == {"1", "5", "10", "20"}
    assert all(np.isfinite(v) for v in profile.decay.values())


# ═══════════════════════════════════════════════
#  5. 分层收益与换手
# ═══════════════════════════════════════════════

def test_layer_returns_and_turnover() -> None:
    """5 层分层收益 + 换手（0~1）"""
    _, close, factor = _make_panel(seed=7)
    forward = compute_forward_returns(close, 5)
    lr = layer_returns(factor, forward, n_layers=5)
    assert len(lr) == 5
    assert all(np.isfinite(v) for v in lr.values())
    to = factor_turnover(factor, n_layers=5)
    assert 0.0 <= to <= 1.0


def test_evaluate_factor_short_panel() -> None:
    """面板过短不产出档案（min_periods 防护）"""
    dates = pd.date_range("2024-01-02", periods=20, freq="B")
    symbols = [f"S{i:03d}" for i in range(20)]
    rng = np.random.default_rng(3)
    close = pd.DataFrame(
        100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, (20, 20)), axis=0)),
        index=dates, columns=symbols,
    )
    factor = pd.DataFrame(
        rng.normal(0, 1, (20, 20)), index=dates, columns=symbols,
    )
    profile = evaluate_factor(
        "short_test", factor_panel=factor, close_panel=close,
        horizon=5, economic_logic="测试",
        min_periods=30,
    )
    assert profile is None


# ═══════════════════════════════════════════════
#  6. decide_status 三态判定
# ═══════════════════════════════════════════════

def test_decide_status() -> None:
    """active / observe / eliminated 判定"""
    assert decide_status(0.05, 0.5, 3.0, 0.60) == FACTOR_STATUS_ACTIVE
    assert decide_status(0.03, 0.2, 1.5, 0.50) == FACTOR_STATUS_OBSERVE
    # ICIR<=0（方向失效）→ eliminated
    assert decide_status(-0.01, -0.2, -1.2, 0.40) == FACTOR_STATUS_ELIMINATED
    # |IC| 过低 → eliminated
    assert decide_status(0.005, 0.05, 0.4, 0.50) == FACTOR_STATUS_ELIMINATED


# ═══════════════════════════════════════════════
#  7. ic_weighted_weights
# ═══════════════════════════════════════════════

def test_ic_weighted_weights_basic() -> None:
    """ICIR 加权 + 归一化 + 下限约束"""
    profiles = {
        "a": _make_profile("a", 0.6),
        "b": _make_profile("b", 0.2),
    }
    w = ic_weighted_weights(profiles, min_weight=0.05, directions={"a": 1, "b": 1})
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["a"] > w["b"]
    assert all(v >= 0.05 - 1e-6 for v in w.values())


def test_ic_weighted_weights_negative_zeroed() -> None:
    """负贡献因子权重归零（不进组合）"""
    profiles = {
        "a": _make_profile("a", 0.6),
        "c": _make_profile("c", -0.3),
    }
    w = ic_weighted_weights(profiles, directions={"a": 1, "c": 1})
    assert "c" not in w
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_ic_weighted_weights_direction_correction() -> None:
    """方向校正：direction=-1 的负 ICIR 因子转正贡献"""
    profiles = {
        "a": _make_profile("a", 0.6),
        "c": _make_profile("c", -0.3),
    }
    w = ic_weighted_weights(profiles, directions={"a": 1, "c": -1})
    assert "c" in w
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_ic_weighted_weights_high_min_weight() -> None:
    """下限约束过高时仍收敛且归一化（不崩溃）"""
    profiles = {
        "a": _make_profile("a", 0.1),
        "b": _make_profile("b", 0.05),
    }
    w = ic_weighted_weights(profiles, min_weight=0.6, directions={"a": 1, "b": 1})
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_ic_weighted_weights_empty() -> None:
    """无正贡献 → 空 dict"""
    assert ic_weighted_weights({}) == {}
    w = ic_weighted_weights({"a": _make_profile("a", -0.5)})
    assert w == {}


# ═══════════════════════════════════════════════
#  8. validate_factor_logic
# ═══════════════════════════════════════════════

def test_validate_factor_logic() -> None:
    """内置因子通过；额外因子须显式提供经济逻辑"""
    assert validate_factor_logic("momentum") is True
    assert validate_factor_logic("quality_roe", "高 ROE 质量溢价") is True
    assert validate_factor_logic("quality_roe") is False


# ═══════════════════════════════════════════════
#  9. FactorProfile 序列化往返
# ═══════════════════════════════════════════════

def test_profile_roundtrip() -> None:
    """to_dict → from_dict 往返一致"""
    prof = _make_profile("x", 0.5, ic_mean=0.03, win_rate=0.61)
    restored = FactorProfile.from_dict(prof.to_dict())
    assert restored == prof


# ═══════════════════════════════════════════════
#  10. MultiFactorStrategy 集成
# ═══════════════════════════════════════════════

def test_strategy_ic_weights() -> None:
    """ic_based_weights=True：IC 加权生效且可追溯（reversal 方向校正）"""
    mf = MultiFactorStrategy(
        ic_based_weights=True,
        ic_weight_min=0.05,
        factor_profiles={
            "momentum": _make_profile("momentum", 0.8),
            "reversal": _make_profile("reversal", -0.4),  # direction=-1 → 正贡献
            "low_vol": _make_profile("low_vol", 0.5),
            "vol_price": _make_profile("vol_price", 0.1),
        },
    )
    status = mf.get_multi_factor_status()
    assert status["ic_based_weights"] is True
    assert status["weight_source"] == "ic_profile"
    assert status["factor_profile_names"] == ["low_vol", "momentum", "reversal", "vol_price"]

    w = mf._ic_weighted_weights(list(MultiFactorStrategy.BASE_FACTORS))
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert "momentum" in w
    assert "reversal" in w  # 负 ICIR 经 direction=-1 校正后有权重
    assert w["momentum"] > w["vol_price"]


def test_strategy_ic_fallback_default() -> None:
    """ic_based_weights=True 但无档案 → 回退默认权重（不静默崩溃）"""
    mf = MultiFactorStrategy(ic_based_weights=True)
    active = list(MultiFactorStrategy.BASE_FACTORS)
    assert mf._resolve_weights(active) == mf._normalize_weights(active)


def test_strategy_default_unchanged() -> None:
    """默认参数：ic_based_weights=False，权重来源 manual（行为零变化）"""
    mf = MultiFactorStrategy()
    assert mf.ic_based_weights is False
    status = mf.get_multi_factor_status()
    assert status["weight_source"] == "manual"
    assert status["ic_weight_min"] == pytest.approx(0.05)


def test_strategy_extra_factor_requires_logic() -> None:
    """额外因子缺 economic_logic → 拒绝进组合；提供后正常登记"""
    mf_rejected = MultiFactorStrategy(
        extra_factors={"quality_roe": {"weight": 0.20, "higher_better": True}},
    )
    assert "quality_roe" not in mf_rejected._extra_factor_configs

    mf_ok = MultiFactorStrategy(
        extra_factors={"quality_roe": {
            "weight": 0.20, "higher_better": True,
            "economic_logic": "高 ROE 反映盈利质量，质量溢价长期实证",
        }},
    )
    assert "quality_roe" in mf_ok._extra_factor_configs
    assert mf_ok._extra_factor_configs["quality_roe"]["economic_logic"]


__all__: List[str] = []
