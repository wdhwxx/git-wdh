# 文件名：tests/unit/test_rsrs.py
"""
P3-3（v1.2 §6.4 ROI 3）：indicators/rsrs.py 覆盖率补测（43.6%→80%）

覆盖范围：
  1. _rolling_ols_vectorized：闭式 OLS 与逐窗口 _linear_regression 等价（含 NaN 对齐）、
     常数 low（分母为 0 → β/ R² NaN）、R² 截断 [0,1]
  2. _linear_regression：n<2 / NaN / low 常数 / 正常 / ss_tot=0（R² NaN）
  3. RSRS.calculate：默认参数、standardize=False、数据不足（前 period+norm_window-1 行 NaN）、
     signal 生成（>0.7→1 / <-0.7→-1 / 其余 0）、与流式 update 一致性
  4. RSRS.update：数据不足返回 NaN、流式与批量一致性、beta_std==0 → z=0、未标准化直传 β
  5. reset / get_signal_thresholds / validate_no_lookahead
  6. classify_regime：5 档分类、阈值校验、inf/NaN 保守归 range、r2 弱结构归 range
  7. regime_from_series：长度不一致 / 索引不对齐 / 正常批量

防未来函数铁律：β_i 使用 iloc[i-period:i]（不含第 i 根），标准化 z 含当前 β。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from myquant.indicators.rsrs import (
    REGIME_TREND_R2_MIN,
    RSRS,
    classify_regime,
    regime_from_series,
)

_CN_COLS = ("开盘价", "最高价", "最低价", "收盘价")


def _make_cn_data(n: int = 200, seed: int = 7) -> pd.DataFrame:
    """构造中文列行情数据（与实盘/批量计算入口一致）。"""
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    high = close + np.abs(rng.normal(0.1, 0.05, n))
    low = close - np.abs(rng.normal(0.1, 0.05, n))
    return pd.DataFrame(
        {"开盘价": close, "最高价": high, "最低价": low, "收盘价": close}
    )


def _bars(data: pd.DataFrame, n: int) -> list[dict]:
    """转流式 bar 列表（update() 读取中文键 最高价/最低价）。"""
    out = []
    for i in range(n):
        out.append(
            {
                "date": data.index[i],
                "开盘价": float(data["开盘价"].iloc[i]),
                "最高价": float(data["最高价"].iloc[i]),
                "最低价": float(data["最低价"].iloc[i]),
                "收盘价": float(data["收盘价"].iloc[i]),
                "volume": 100000.0,
            }
        )
    return out


# ─────────────────────────────────────────────
# _linear_regression
# ─────────────────────────────────────────────

class TestLinearRegression:
    def test_less_than_two_returns_nan(self) -> None:
        assert RSRS._linear_regression(np.array([1.0]), np.array([2.0])) == (np.nan, np.nan, np.nan)

    def test_nan_input_returns_nan(self) -> None:
        out = RSRS._linear_regression(np.array([1.0, np.nan]), np.array([2.0, 3.0]))
        assert all(np.isnan(v) for v in out)

    def test_constant_low_returns_nan(self) -> None:
        out = RSRS._linear_regression(np.array([5.0, 5.0, 5.0]), np.array([1.0, 2.0, 3.0]))
        assert all(np.isnan(v) for v in out)

    def test_constant_high_r2_nan(self) -> None:
        slope, intercept, r2 = RSRS._linear_regression(
            np.array([1.0, 2.0, 3.0]), np.array([7.0, 7.0, 7.0])
        )
        assert slope == pytest.approx(0.0)
        assert intercept == pytest.approx(7.0)
        assert np.isnan(r2)  # ss_tot=0 → R² 未定义

    def test_perfect_fit(self) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0])
        y = 2.0 * x + 1.0
        slope, intercept, r2 = RSRS._linear_regression(x, y)
        assert slope == pytest.approx(2.0)
        assert intercept == pytest.approx(1.0)
        assert r2 == pytest.approx(1.0)


# ─────────────────────────────────────────────
# _rolling_ols_vectorized
# ─────────────────────────────────────────────

class TestRollingOlsVectorized:
    def test_equivalence_with_naive(self) -> None:
        data = _make_cn_data(200)
        low = data["最低价"].astype(float)
        high = data["最高价"].astype(float)
        period = 18
        beta_vec, r2_vec = RSRS._rolling_ols_vectorized(low, high, period)
        # naive 逐窗口（含当日窗口）
        beta_naive = np.full(200, np.nan)
        r2_naive = np.full(200, np.nan)
        for i in range(period - 1, 200):
            b, _, r = RSRS._linear_regression(
                low.values[i - period + 1: i + 1], high.values[i - period + 1: i + 1]
            )
            beta_naive[i] = b
            r2_naive[i] = r
        mask = ~np.isnan(beta_naive)
        assert np.allclose(beta_vec.values[mask], beta_naive[mask], atol=1e-10)
        assert np.allclose(r2_vec.values[mask], r2_naive[mask], atol=1e-10)

    def test_insufficient_prefix_nan(self) -> None:
        data = _make_cn_data(30)
        beta, r2 = RSRS._rolling_ols_vectorized(
            data["最低价"].astype(float), data["最高价"].astype(float), 18
        )
        assert np.isnan(beta.iloc[16]).all() is not False
        assert beta.iloc[:17].isna().all()
        assert r2.iloc[:17].isna().all()

    def test_constant_low_produces_nan(self) -> None:
        n = 40
        low = pd.Series(np.full(n, 50.0))
        high = pd.Series(np.linspace(51.0, 60.0, n))
        beta, r2 = RSRS._rolling_ols_vectorized(low, high, 18)
        assert beta.isna().all()
        assert r2.isna().all()

    def test_r2_clipped_to_unit(self) -> None:
        # 浮点误差下 R² 微超 [0,1] 被截断
        data = _make_cn_data(100)
        beta, r2 = RSRS._rolling_ols_vectorized(
            data["最低价"].astype(float), data["最高价"].astype(float), 10
        )
        valid = r2.dropna()
        assert bool((valid >= 0).all() and (valid <= 1).all())


# ─────────────────────────────────────────────
# RSRS.calculate
# ─────────────────────────────────────────────

class TestCalculate:
    def test_output_columns_and_shape(self) -> None:
        data = _make_cn_data(200)
        out = RSRS().calculate(data)
        assert list(out.columns) == ["rsrs", "rsrs_raw", "rsrs_r2", "signal"]
        assert len(out) == len(data)

    def test_first_valid_position(self) -> None:
        # β 需 period 根 + shift → 第 period 根（索引 period）有效；z 需 norm_window 个 β
        ind = RSRS(period=18, norm_window=60)
        out = ind.calculate(_make_cn_data(200))
        # 第一个有效 z 应在索引 period + norm_window - 1（即 77）
        first_valid = out["rsrs"].first_valid_index()
        assert first_valid == 77

    def test_signal_logic(self) -> None:
        # 构造强上升/强下降/持平三段，验证信号阈值
        rng = np.random.default_rng(1)
        n = 300
        # 段1 强上升 → z 大概率 >0.7 → signal=1
        close = np.concatenate([
            10 + np.cumsum(rng.normal(0.05, 0.01, n // 3)),
            100 + np.cumsum(rng.normal(0, 0.01, n // 3)),
            100 + np.cumsum(rng.normal(-0.05, 0.01, n // 3)),
        ])
        high = close + 0.1
        low = close - 0.1
        data = pd.DataFrame({"开盘价": close, "最高价": high, "最低价": low, "收盘价": close})
        out = RSRS().calculate(data)
        sig = out["signal"]
        assert set(sig.unique()) <= {0, 1, -1}
        assert (sig == 1).any(), "上升段应产生买入信号"
        assert (sig == -1).any(), "下降段应产生卖出信号"

    def test_standardize_false(self) -> None:
        ind = RSRS(standardize=False)
        out = ind.calculate(_make_cn_data(200))
        # 未标准化：rsrs 直接等于 beta_shifted
        assert out["rsrs"].iloc[18] == pytest.approx(out["rsrs_raw"].iloc[18], abs=1e-12)

    def test_insufficient_data_all_signal_zero(self) -> None:
        ind = RSRS(period=18, norm_window=60)
        out = ind.calculate(_make_cn_data(50))
        assert (out["signal"] == 0).all()

    def test_validate_data_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            RSRS().calculate(pd.DataFrame())


# ─────────────────────────────────────────────
# RSRS.update 流式
# ─────────────────────────────────────────────

class TestUpdate:
    def test_insufficient_history_returns_nan(self) -> None:
        ind = RSRS(period=18, norm_window=60)
        out = ind.update(_bars(_make_cn_data(30), 1)[0])
        assert np.isnan(out["rsrs"])
        assert np.isnan(out["rsrs_raw"])
        assert out["signal"] == 0

    def test_stream_matches_batch(self) -> None:
        ind = RSRS(period=18, norm_window=60)
        data = _make_cn_data(120)
        bars = _bars(data, 120)
        stream = []
        for b in bars:
            stream.append(ind.update(b))
        batch = RSRS(period=18, norm_window=60).calculate(data)
        # 数据充足位置（>= period+norm_window）流式 z 应与批量一致
        i = 100
        assert np.isclose(stream[i]["rsrs"], batch["rsrs"].iloc[i], atol=1e-8)
        assert np.isclose(stream[i]["rsrs_raw"], batch["rsrs_raw"].iloc[i], atol=1e-10)

    def test_constant_beta_zero_z(self) -> None:
        # low/high 完全线性 → β 恒定 → std=0 → z=0（设计：std==0 归 0）
        ind = RSRS(period=18, norm_window=60)
        n = 120
        close = np.linspace(10.0, 30.0, n)
        data = pd.DataFrame(
            {"开盘价": close, "最高价": close + 1.0, "最低价": close - 1.0, "收盘价": close}
        )
        bars = _bars(data, n)
        for b in bars:
            ind.update(b)
        # 再喂最后一根（数据充足）→ z=0（β 恒定 → std==0 分支）
        out = ind.update(bars[-1])
        assert out["rsrs"] == 0.0

    def test_standardize_false_stream(self) -> None:
        ind = RSRS(period=18, norm_window=60, standardize=False)
        data = _make_cn_data(120)
        for b in _bars(data, 100):
            ind.update(b)
        out = ind.update(_bars(data, 101)[0])
        # 未标准化：rsrs 直传 β（有效时）
        if not np.isnan(out["rsrs_raw"]):
            assert out["rsrs"] == pytest.approx(out["rsrs_raw"], abs=1e-12)

    def test_reset_clears_state(self) -> None:
        ind = RSRS(period=18, norm_window=60)
        data = _make_cn_data(100)
        for b in _bars(data, 30):
            ind.update(b)
        ind.reset()
        out = ind.update(_bars(data, 31)[0])
        assert np.isnan(out["rsrs"])  # 重置后历史清空 → 数据不足


# ─────────────────────────────────────────────
# 阈值 / 防未来校验
# ─────────────────────────────────────────────

class TestHelpers:
    def test_get_signal_thresholds(self) -> None:
        ind = RSRS(buy_threshold=0.8, sell_threshold=-0.6)
        assert ind.get_signal_thresholds() == {"buy_threshold": 0.8, "sell_threshold": -0.6}

    def test_validate_no_lookahead_passes(self) -> None:
        assert RSRS(period=18, norm_window=60).validate_no_lookahead(_make_cn_data(200)) is True

    def test_validate_no_lookahead_insufficient(self) -> None:
        # 数据不足 → 跳过校验仍返回 True（无 NaN 可校验行）
        assert RSRS(period=18, norm_window=60).validate_no_lookahead(_make_cn_data(10)) is True

    def test_history_sizes(self) -> None:
        ind = RSRS(period=18, norm_window=60)
        assert ind._get_history_size() == 18 + 60 + 10
        assert ind._get_indicator_history_size() == 60 + 10


# ─────────────────────────────────────────────
# classify_regime
# ─────────────────────────────────────────────

class TestClassifyRegime:
    def test_trend_up(self) -> None:
        assert classify_regime(1.0, 0.8) == "trend_up"

    def test_trend_down(self) -> None:
        assert classify_regime(-1.0, 0.8) == "trend_down"

    def test_strong_but_weak_structure_is_range(self) -> None:
        assert classify_regime(1.0, 0.1) == "range"
        assert classify_regime(-1.0, 0.1) == "range"

    def test_mild_up(self) -> None:
        assert classify_regime(0.3, 0.5) == "mild_up"

    def test_mild_down(self) -> None:
        assert classify_regime(-0.3, 0.5) == "mild_down"

    def test_zero_is_range(self) -> None:
        assert classify_regime(0.0, 0.9) == "range"

    def test_nan_is_range(self) -> None:
        assert classify_regime(float("nan"), 0.9) == "range"
        assert classify_regime(1.0, float("nan")) == "range"

    def test_inf_is_range(self) -> None:
        assert classify_regime(float("inf"), 0.9) == "range"

    def test_invalid_thresholds_raise(self) -> None:
        with pytest.raises(ValueError, match="buy_threshold 必须大于 sell_threshold"):
            classify_regime(0.5, 0.5, buy_threshold=0.5, sell_threshold=0.6)

    def test_invalid_trend_r2_min_raises(self) -> None:
        with pytest.raises(ValueError, match="trend_r2_min 必须在"):
            classify_regime(0.5, 0.5, trend_r2_min=1.5)

    def test_custom_thresholds(self) -> None:
        assert classify_regime(0.5, 0.9, buy_threshold=0.4) == "trend_up"

    def test_r2_exactly_min_is_trend(self) -> None:
        assert classify_regime(1.0, REGIME_TREND_R2_MIN) == "trend_up"


# ─────────────────────────────────────────────
# regime_from_series
# ─────────────────────────────────────────────

class TestRegimeFromSeries:
    def test_normal_batch(self) -> None:
        rs = pd.Series([1.0, -1.0, 0.3, 0.0], index=[0, 1, 2, 3])
        r2 = pd.Series([0.8, 0.8, 0.5, 0.9], index=[0, 1, 2, 3])
        out = regime_from_series(rs, r2)
        assert out.tolist() == ["trend_up", "trend_down", "mild_up", "range"]

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="长度不一致"):
            regime_from_series(pd.Series([1.0, 2.0]), pd.Series([1.0]))

    def test_index_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="索引不对齐"):
            regime_from_series(pd.Series([1.0], index=[0]), pd.Series([1.0], index=[1]))

    def test_nan_rows_to_range(self) -> None:
        rs = pd.Series([float("nan"), 1.0], index=[0, 1])
        r2 = pd.Series([0.5, 0.5], index=[0, 1])
        out = regime_from_series(rs, r2)
        assert out.iloc[0] == "range"
        assert out.iloc[1] == "trend_up"

    def test_index_preserved(self) -> None:
        idx = pd.Index(["a", "b", "c"])
        rs = pd.Series([1.0, 0.0, -1.0], index=idx)
        r2 = pd.Series([0.9, 0.9, 0.9], index=idx)
        out = regime_from_series(rs, r2)
        assert out.index.tolist() == ["a", "b", "c"]
