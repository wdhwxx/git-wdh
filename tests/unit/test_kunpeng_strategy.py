# 文件名：tests/unit/test_kunpeng_strategy.py
"""
test_kunpeng_strategy.py - 鲲鹏起爆点策略单元测试
==================================================
覆盖 myquant/strategies/kunpeng_strategy.py（docs/鲲鹏壹号策略_回测实施报告.md 表9 用例组）：
  1. 指标口径单测：构造已知 K 线，测试内独立手算 A/G/H/I/M/N/O/S/最终信号，与策略输出逐项对照
  2. BARSLAST 边界：条件从未发生→NaN→False；恰好第12/13根边界；量比恰好4.5边界（严格大于）
  3. 数据不足：<60根K线→全周期无信号（不抛异常）
  4. 信号真例/假例：量·价·MACD 三模块全满足→True；缺任一模块→False（含组件存在性断言）
  5. 三接口一致性：同一数据上 event/mixed/vectorized 产生相同 BUY/SELL 信号日集合
  6. 防未来：回测后 LookaheadGuard.assert_pass() 必须通过
  7. 出场规则：止损/止盈/移动止盈/持有上限四条件 + 三档位映射 + 显式覆盖
  8. 参数校验：全部非法参数 → ValueError
  9. 注册/工厂：create_strategy/engine_modes/批量 STRATEGY_REGISTRY

运行方式:
  pytest tests/unit/test_kunpeng_strategy.py -v
  pytest tests/unit/test_kunpeng_strategy.py -v -m lookahead

设计原则（与 test_strategies.py 一致）:
  1. 策略第 i 根 bar 的信号仅使用 iloc[:i+1] 数据（防未来铁律）
  2. 手算辅助全部在测试内独立实现（不复用被测代码），确保口径对照有效
  3. 浮点对照用 np.testing.assert_allclose，布尔序列用 assert_array_equal
"""

from __future__ import annotations

# ── 标准库 ──
from typing import Any, Dict, List, Optional

# ── 第三方库 ──
import numpy as np
import pandas as pd
import pytest

# ── 本地模块 ──
from myquant.backtest.batch_backtest import STRATEGY_REGISTRY
from myquant.backtest.engine import run_backtest
from myquant.core.lookahead_guard import LookaheadGuard
from myquant.models.backtest import BacktestConfig
from myquant.models.market_data import Bar, BarFrequency
from myquant.strategies import (
    KunpengStrategy,
    create_strategy,
    discover_strategies,
    list_strategies,
)
from myquant.strategies.base import Signal, SignalType, StrategyContext

from tests.conftest import make_ohlcv_df

# ═══════════════════════════════════════════════
#  常量（禁魔法数字）
# ═══════════════════════════════════════════════

WARMUP_BARS: int = 60
BASE_VOLUME: float = 1_000_000.0
SYMBOL: str = "600519.SH"
INITIAL_CAPITAL: float = 1_000_000.0

# 真例信号数据：70根横盘(10.0) → 4根上升(MACD转正) → 4根浅回调(MACD 4日递减)
# → 1根放量回升(信号日) → 拉升触发止盈(12.00/+16.5%) → 尾部留出 T+1 卖出成交bar。
# 信号日在索引79（已验证）；止盈信号在索引84收盘 → 索引85开盘成交。
SIGNAL_CLOSES: List[float] = (
    [10.0] * 70
    + [10.02, 10.05, 10.09, 10.14]
    + [10.12, 10.10, 10.09, 10.085]
    + [10.10, 10.30, 10.60, 11.00, 11.30, 11.60, 12.00, 12.10, 12.15, 12.20]
)
SIGNAL_BAR: int = 79
SIGNAL_SPIKE_FROM: int = 78  # 放量起始索引

# 出场档位预期（与 kunpeng_strategy.EXIT_TIERS 一致，测试内独立常量）
TIER_EXPECTATIONS: Dict[str, Dict[str, Optional[float]]] = {
    "conservative": {"stop_loss_pct": 0.05, "take_profit_pct": 0.10,
                     "trailing_activate_pct": None, "trailing_drawdown_pct": None},
    "standard": {"stop_loss_pct": 0.07, "take_profit_pct": 0.15,
                 "trailing_activate_pct": 0.08, "trailing_drawdown_pct": 0.03},
    "aggressive": {"stop_loss_pct": 0.10, "take_profit_pct": 0.25,
                   "trailing_activate_pct": 0.12, "trailing_drawdown_pct": 0.05},
}


# ═══════════════════════════════════════════════
#  数据与上下文构造工具
# ═══════════════════════════════════════════════

def _make_df(
    closes: List[float],
    volumes: Optional[List[float]] = None,
    symbol: str = SYMBOL,
) -> pd.DataFrame:
    """从收盘价/成交量序列构造标准 OHLCV DataFrame（中文列，#38 规范）。"""
    n = len(closes)
    if volumes is None:
        volumes = [BASE_VOLUME] * n
    return make_ohlcv_df(
        closes=closes,
        volumes=volumes,
        opens=[c * 0.995 for c in closes],
        highs=[c * 1.02 for c in closes],
        lows=[c * 0.98 for c in closes],
        amounts=[c * v for c, v in zip(closes, volumes)],
        start="2024-01-01",
    )


def _make_bar(symbol: str, close: float, ts: float, volume: float = BASE_VOLUME) -> Bar:
    """构造单根 Bar。"""
    return Bar(
        symbol=symbol,
        frequency=BarFrequency.DAILY,
        timestamp=ts,
        open=close * 0.995,
        high=close * 1.02,
        low=close * 0.98,
        close=close,
        volume=volume,
        amount=close * volume,
    )


def _make_context(df: pd.DataFrame, symbol: str = SYMBOL) -> StrategyContext:
    """构造含数据的策略上下文（portfolio=None：event 逐bar测试仅产出 BUY 信号）。"""
    return StrategyContext(
        data={symbol: df},
        portfolio=None,
        executor=None,
        config={"initial_capital": INITIAL_CAPITAL},
    )


def _signal_data(volumes: Optional[List[float]] = None) -> pd.DataFrame:
    """真例信号数据（量·价·MACD 三模块全满足）。"""
    if volumes is None:
        volumes = [BASE_VOLUME] * len(SIGNAL_CLOSES)
        for i in range(SIGNAL_SPIKE_FROM, len(SIGNAL_CLOSES)):
            volumes[i] = 6.0 * BASE_VOLUME
    return _make_df(SIGNAL_CLOSES, volumes)


# ═══════════════════════════════════════════════
#  手算辅助（测试内独立实现，不复用被测代码）
# ═══════════════════════════════════════════════

def _manual_ema(values: np.ndarray, span: int) -> np.ndarray:
    """独立复现 pandas ewm(span, adjust=False)：ema[0]=x[0]。"""
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _manual_rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """独立复现 rolling(window).mean()（严格窗口，前 window-1 根 NaN）。"""
    out = np.full(len(values), np.nan)
    for i in range(window - 1, len(values)):
        out[i] = float(np.mean(values[i - window + 1: i + 1]))
    return out


def _manual_bars_since(cond: np.ndarray) -> np.ndarray:
    """独立复现 BARSLAST（从未发生→NaN）。"""
    out = np.full(len(cond), np.nan)
    last = -1
    for i, flag in enumerate(cond):
        if flag:
            last = i
        if last >= 0:
            out[i] = float(i - last)
    return out


def _manual_kunpeng(
    closes: np.ndarray, vols: np.ndarray, st: KunpengStrategy,
) -> Dict[str, np.ndarray]:
    """独立手算鲲鹏全部关键序列（与策略 _compute_kunpeng 口径对照）。"""
    n = len(closes)

    # ── 量能模块 ──
    prev_ma5 = np.roll(_manual_rolling_mean(vols, 5), 1)
    prev_ma5[0] = np.nan
    vol_ratio = np.where(prev_ma5 > 0, vols / prev_ma5, 0.0)
    vol_ma_max = np.maximum.reduce([
        _manual_rolling_mean(vols, 5),
        _manual_rolling_mean(vols, 10),
        _manual_rolling_mean(vols, 20),
        _manual_rolling_mean(vols, 30),
        _manual_rolling_mean(vols, 60),
    ])
    over_max = vols > vol_ma_max
    h_count = np.full(n, np.nan)
    for i in range(st.vol_count_window - 1, n):
        h_count[i] = float(np.sum(over_max[i - st.vol_count_window + 1: i + 1]))
    bars_h = _manual_bars_since(h_count >= st.vol_active_days)
    bars_a = _manual_bars_since(vol_ratio > st.vol_ratio_threshold)
    vol_gate = (bars_h < st.barslast_vol_window) | (bars_a < st.barslast_ratio_window)

    # ── 均线粘合模块 ──
    ma20 = _manual_rolling_mean(closes, 20)
    ma30 = _manual_rolling_mean(closes, 30)
    ma60 = _manual_rolling_mean(closes, 60)
    ma_max = np.maximum.reduce([ma20, ma30, ma60])
    ma_min = np.maximum(np.minimum.reduce([ma20, ma30, ma60]), 1e-8)
    m_value = ma_max / ma_min
    sticky = m_value <= st.ma_sticky_threshold

    # ── MACD 模块 ──
    diff = _manual_ema(closes, 12) - _manual_ema(closes, 26)
    dea = _manual_ema(diff, 9)
    macd = 2.0 * (diff - dea)
    macd_decline = (
        (np.roll(macd, 1) < np.roll(macd, 2))
        & (np.roll(macd, 2) < np.roll(macd, 3))
        & (np.roll(macd, 3) < np.roll(macd, 4))
    )
    macd_decline[:4] = False  # 前4根无移位数据
    macd_cross = (macd > st.macd_rise_threshold) & (macd > np.roll(macd, 1))
    macd_cross[0] = False

    # ── 起爆点一 ──
    ema5 = _manual_ema(closes, 5)
    ema10 = _manual_ema(closes, 10)
    ema13 = _manual_ema(closes, 13)
    ema30 = _manual_ema(closes, 30)
    macd_tight = np.abs((diff - dea) / closes) < st.diff_dea_ratio
    macd_small = macd < st.macd_abs_threshold
    p_cond = macd_decline & macd_cross & (ema13 > np.roll(ema13, 1))
    q_cond = macd_decline & macd_cross & macd_tight
    r_cond = macd_decline & macd_cross & macd_small
    s1 = p_cond | q_cond | r_cond

    # ── 起爆点二 ──
    ema30_flat = ema30 >= np.roll(ema30, 1)
    bullish_align = (ema5 > ema10) & (ema10 > ema30)
    above_ema30 = closes > ema30
    x_cond = macd_decline & macd_cross & ema30_flat & bullish_align & above_ema30
    s2 = x_cond | (x_cond & macd_tight) | (x_cond & macd_small)

    signal = (s1 | s2) & vol_gate & sticky
    signal = np.nan_to_num(signal, nan=False).astype(bool)

    return {
        "vol_ratio": vol_ratio,
        "h_count": h_count,
        "m_value": m_value,
        "vol_gate": vol_gate.astype(bool),
        "sticky": sticky.astype(bool),
        "macd_decline": macd_decline.astype(bool),
        "macd_cross": macd_cross.astype(bool),
        "s1": s1.astype(bool),
        "s2": s2.astype(bool),
        "signal": signal,
    }


# ═══════════════════════════════════════════════
#  TestKunpengStrategyFormula - 指标口径单测（表9 第1/2/4组）
# ═══════════════════════════════════════════════

class TestKunpengStrategyFormula:
    """指标口径：测试内独立手算与策略输出逐项对照。"""

    def test_manual_consistency_full_series(self):
        """手算全序列对照：vol_ratio/h_count/m_value/macd_decline/macd_cross/s1/s2/vol_gate/sticky/signal。"""
        df = _signal_data()
        st = KunpengStrategy()
        kp = st._compute_kunpeng(df)
        manual = _manual_kunpeng(
            df["收盘价"].to_numpy(dtype=float),
            df["成交量"].to_numpy(dtype=float),
            st,
        )
        np.testing.assert_allclose(
            kp["vol_ratio"].to_numpy(), manual["vol_ratio"], rtol=1e-10, atol=1e-10,
            err_msg="vol_ratio 口径不一致",
        )
        np.testing.assert_allclose(
            kp["h_count"].to_numpy(), manual["h_count"], rtol=1e-10, atol=1e-10,
            err_msg="h_count 口径不一致",
        )
        np.testing.assert_allclose(
            kp["m_value"].to_numpy(), manual["m_value"], rtol=1e-10, atol=1e-10,
            err_msg="m_value 口径不一致",
        )
        for key in ("macd_decline", "macd_cross", "s1", "s2", "vol_gate", "sticky", "signal"):
            np.testing.assert_array_equal(
                kp[key].to_numpy(), manual[key], err_msg=f"{key} 口径不一致",
            )

    def test_vol_ratio_manual_sixx(self):
        """量比手算：前5日均量1e6 + 当日6e6 → A=6.0。"""
        closes = [10.0] * 6
        vols = [BASE_VOLUME] * 5 + [6.0 * BASE_VOLUME]
        st = KunpengStrategy()
        kp = st._compute_kunpeng(_make_df(closes, vols))
        assert kp["vol_ratio"].iloc[-1] == pytest.approx(6.0, rel=1e-12)

    def test_vol_ratio_denominator_zero(self):
        """决策8：前5日均量为0 → 量比置0（不抛异常）。"""
        closes = [10.0] * 6
        vols = [0.0] * 5 + [6.0 * BASE_VOLUME]
        st = KunpengStrategy()
        kp = st._compute_kunpeng(_make_df(closes, vols))
        assert kp["vol_ratio"].iloc[-1] == 0.0

    def test_h_count_manual_window(self):
        """H 手算：60根平量 + 5根递增放量 → 每根均超五档均量最大 → 最后一根 H=5（窗口6）。"""
        closes = [10.0] * 65
        # 递增放量保证每根 > 当日前5日均量（恒定放量会使第5根 vol==MA5 不满足严格大于）
        spike = [v * BASE_VOLUME for v in (2.0, 2.2, 2.4, 2.6, 2.8)]
        vols = [BASE_VOLUME] * 60 + spike
        st = KunpengStrategy()
        kp = st._compute_kunpeng(_make_df(closes, vols))
        assert kp["h_count"].iloc[-1] == pytest.approx(5.0)
        # 放量第1根（索引60）：窗口内仅1天放量
        assert kp["h_count"].iloc[60] == pytest.approx(1.0)

    def test_m_sticky_flat_vs_separated(self):
        """M 手算：60根平盘 → M≈1.0 粘合；平盘后急升 → M>1.22 不粘合。"""
        st = KunpengStrategy()
        flat = _make_df([10.0] * 60)
        kp_flat = st._compute_kunpeng(flat)
        assert kp_flat["m_value"].iloc[-1] == pytest.approx(1.0, rel=1e-12)
        assert bool(kp_flat["sticky"].iloc[-1]) is True

        closes = [10.0] * 40 + list(np.linspace(10.0, 18.0, 20))
        kp_sep = st._compute_kunpeng(_make_df(closes))
        # ma20≈17.0, ma30≈15.33, ma60≈12.33 → M≈17/12.33≈1.379
        assert kp_sep["m_value"].iloc[-1] > st.ma_sticky_threshold
        assert bool(kp_sep["sticky"].iloc[-1]) is False

    def test_signal_true_case(self):
        """信号真例：三模块全满足 → 仅在预期信号日(索引79)为 True。"""
        df = _signal_data()
        st = KunpengStrategy()
        kp = st._compute_kunpeng(df)
        sig_idx = np.where(kp["signal"].to_numpy())[0]
        assert len(sig_idx) == 1, f"期望恰好1个信号日，实际: {sig_idx}"
        assert sig_idx[0] == SIGNAL_BAR
        # 信号日组件全满足
        i = SIGNAL_BAR
        assert bool(kp["vol_gate"].iloc[i]) is True
        assert bool(kp["sticky"].iloc[i]) is True
        assert bool(kp["s1"].iloc[i]) is True
        assert bool(kp["s2"].iloc[i]) is True

    def test_signal_false_no_volume(self):
        """信号假例-缺量能：成交量全平 → vol_gate 全 False → 无信号。"""
        volumes = [BASE_VOLUME] * len(SIGNAL_CLOSES)  # 无放量
        df = _signal_data(volumes)
        st = KunpengStrategy()
        kp = st._compute_kunpeng(df)
        assert bool(kp["vol_gate"].any()) is False
        assert int(kp["signal"].sum()) == 0

    def test_signal_false_no_macd(self):
        """信号假例-缺MACD：平盘+放量+单调微升（无三连降转升）→ s1/s2 全 False。"""
        closes = [10.0] * 70 + [10.01, 10.02, 10.03, 10.04, 10.05, 10.06,
                                10.07, 10.08, 10.09, 10.10]
        vols = [BASE_VOLUME] * 70 + [6.0 * BASE_VOLUME] + [BASE_VOLUME] * 9
        st = KunpengStrategy()
        kp = st._compute_kunpeng(_make_df(closes, vols))
        # 组件隔离断言：量能门有触发、粘合达标，缺的是 MACD 起爆模块
        assert bool(kp["vol_gate"].any()) is True
        assert bool(kp["sticky"].any()) is True
        assert bool(kp["s1"].any()) is False
        assert bool(kp["s2"].any()) is False
        assert int(kp["signal"].sum()) == 0

    def test_signal_false_no_sticky(self):
        """信号假例-缺粘合：真例数据（量能+MACD起爆均触发）但粘合阈值收紧到 1.001
        （信号日 M≈1.0026 未达标）→ 粘合门关闭 → 无最终信号。"""
        df = _signal_data()
        st = KunpengStrategy(ma_sticky_threshold=1.001)  # 信号日 M≈1.0026 不达标
        kp = st._compute_kunpeng(df)
        i = SIGNAL_BAR
        # 组件隔离断言：起爆点一与量能门在信号日均触发，唯独粘合门被收紧关闭
        assert bool(kp["s1"].iloc[i]) is True, "信号日应触发起爆点一"
        assert bool(kp["vol_gate"].iloc[i]) is True, "信号日应触发量能门"
        assert float(kp["m_value"].iloc[i]) > st.ma_sticky_threshold, (
            "信号日粘合度应高于收紧后的阈值"
        )
        assert bool(kp["sticky"].iloc[i]) is False
        assert bool(kp["signal"].iloc[i]) is False
        assert int(kp["signal"].sum()) == 0

    def test_macd_rise_threshold_filters_near_zero_cross(self):
        """贴零轴临界假金叉过滤：MACD 微正(0,0.0001)转升日，默认阈值(0.0001)下
        macd_cross=False（不产出买入信号）；macd_rise_threshold=0.0（等价旧 MACD>=0）
        时同日 macd_cross=True——证明阈值参数生效、过滤的正是贴零轴假金叉。"""
        # 平台期（MACD=0）后单根微涨 1e-7 元 → MACD 微正（约 1e-8 量级，<0.0001）且上穿前值
        closes = [10.0] * 80 + [10.0000001] + [10.0] * 5
        df = _make_df(closes)
        st_default = KunpengStrategy()
        st_loose = KunpengStrategy(macd_rise_threshold=0.0)
        cross_default = st_default._compute_kunpeng(df)["macd_cross"]
        cross_loose = st_loose._compute_kunpeng(df)["macd_cross"]
        assert bool(cross_loose.iloc[80]) is True, "构造数据应产生贴零轴转升"
        assert bool(cross_default.iloc[80]) is False, "默认阈值应过滤贴零轴临界假金叉"
        assert int(cross_default.sum()) == 0, "默认阈值下全周期不应出现任意贴零轴翻红"


# ═══════════════════════════════════════════════
#  TestKunpengBarsLast - BARSLAST 边界（表9 第2组）
# ═══════════════════════════════════════════════

class TestKunpengBarsLast:
    """BARSLAST 边界语义（决策1）：从未发生→NaN；数值边界；量比恰好4.5。"""

    def test_bars_since_never_true_is_nan(self):
        """条件从未发生 → NaN。"""
        st = KunpengStrategy()
        cond = pd.Series([False] * 30)
        bars = st._bars_since(cond)
        assert bars.isna().all()

    def test_bars_since_numeric_boundary(self):
        """数值边界：首次 True 位置5 → 0；位置17 → 12；位置18 → 13（<13 与 >=13 分界）。"""
        st = KunpengStrategy()
        cond = pd.Series([False] * 20)
        cond.iloc[5] = True
        bars = st._bars_since(cond)
        assert bars.iloc[5] == 0
        assert bars.iloc[6] == 1
        assert bars.iloc[17] == 12
        assert bars.iloc[18] == 13
        assert bars.iloc[5:].notna().all()

    def test_vol_gate_boundary_12_13(self):
        """I 门边界：60根平量 + 5根递增放量(H>=5首次索引64) + 18根平量 →
        bars_since=12 在索引77（I True）、=13 在索引78（I False）（无A分支干扰）。"""
        closes = [10.0] * 83
        spike = [v * BASE_VOLUME for v in (2.0, 2.2, 2.4, 2.6, 2.8)]
        vols = [BASE_VOLUME] * 60 + spike + [BASE_VOLUME] * 18
        st = KunpengStrategy()
        kp = st._compute_kunpeng(_make_df(closes, vols))
        assert int(kp["h_count"].iloc[64]) == 5, "递增放量第5根 H 应为5"
        assert bool(kp["vol_gate"].iloc[77]) is True, "bars_since=12 应触发"
        assert bool(kp["vol_gate"].iloc[78]) is False, "bars_since=13 应关闭"
        # 平量段 A 分支不干扰（A≈1 < 4.5）
        assert float(kp["vol_ratio"].iloc[78]) == pytest.approx(1.0, rel=1e-6)

    def test_vol_ratio_exact_45_boundary(self):
        """量比恰好 4.5：严格大于 → 不触发；4.5+ε → 触发。"""
        st = KunpengStrategy()
        exact = _make_df([10.0] * 6, [BASE_VOLUME] * 5 + [4.5 * BASE_VOLUME])
        kp_exact = st._compute_kunpeng(exact)
        assert float(kp_exact["vol_ratio"].iloc[-1]) == pytest.approx(4.5, rel=1e-12)
        assert bool(kp_exact["vol_gate"].iloc[-1]) is False

        over = _make_df([10.0] * 6, [BASE_VOLUME] * 5 + [4.5 * BASE_VOLUME + 1.0])
        kp_over = st._compute_kunpeng(over)
        assert float(kp_over["vol_ratio"].iloc[-1]) > st.vol_ratio_threshold
        assert bool(kp_over["vol_gate"].iloc[-1]) is True


# ═══════════════════════════════════════════════
#  TestKunpengBoundary - 数据不足 / 缺列 / 空数据（表9 第3组）
# ═══════════════════════════════════════════════

class TestKunpengBoundary:
    """边界：<60根无信号不抛异常；缺列/空数据防御。"""

    def test_short_data_no_signal(self):
        """55根（<60预热期）→ 全周期无信号、不抛异常（三接口）。"""
        df = _make_df([10.0] * 55)
        st = KunpengStrategy()
        assert int(st._compute_kunpeng(df)["signal"].sum()) == 0
        vec = st.generate_signals_vectorized(df)
        assert vec.shape == (55,)
        assert not vec.any()
        ctx = _make_context(df)
        st.on_init(ctx)
        ts = df.index[-1].timestamp()
        bar = _make_bar(SYMBOL, 10.0, ts)
        assert st.on_bar(bar, ctx) == []
        assert st.generate_mixed_loop(bar, ctx, None) == 0.0

    def test_exactly_warmup_bars(self):
        """恰好60根 → 可产生信号（rolling(60) 首根有效），不抛异常。"""
        closes = [10.0] * 60
        st = KunpengStrategy()
        kp = st._compute_kunpeng(_make_df(closes))
        assert kp["m_value"].iloc[-1] == pytest.approx(1.0, rel=1e-12)
        assert not kp["signal"].isna().any()

    def test_missing_volume_column(self):
        """缺 volume 列 → _compute_kunpeng 抛 ValueError；on_bar 防御返回空信号。"""
        df = _make_df([10.0] * 70).drop(columns=["成交量"])
        st = KunpengStrategy()
        with pytest.raises(ValueError):
            st._compute_kunpeng(df)
        ctx = _make_context(df)
        st.on_init(ctx)
        ts = df.index[-1].timestamp()
        bar = _make_bar(SYMBOL, 10.0, ts)
        assert st.on_bar(bar, ctx) == []
        assert st.generate_mixed_loop(bar, ctx, None) == 0.0
        assert not st.generate_signals_vectorized(df).any()

    def test_empty_dataframe(self):
        """空 DataFrame → _compute_kunpeng 抛 ValueError；入口防御不崩溃。"""
        df = pd.DataFrame()
        st = KunpengStrategy()
        with pytest.raises(ValueError):
            st._compute_kunpeng(df)


# ═══════════════════════════════════════════════
#  TestKunpengExit - 出场规则（报告第7章 + 表4 出场组）
# ═══════════════════════════════════════════════

class TestKunpengExit:
    """出场四条件与三档位映射。"""

    def test_exit_tiers_mapping(self):
        """三档位映射：止损/止盈/移动止盈激活线与回撤线。"""
        for mode, expected in TIER_EXPECTATIONS.items():
            st = KunpengStrategy(exit_mode=mode)
            assert st.stop_loss_pct == pytest.approx(expected["stop_loss_pct"])
            assert st.take_profit_pct == pytest.approx(expected["take_profit_pct"])
            assert st.trailing_activate_pct == expected["trailing_activate_pct"]
            assert st.trailing_drawdown_pct == expected["trailing_drawdown_pct"]

    def test_exit_params_explicit_override(self):
        """显式 take_profit_pct/stop_loss_pct 覆盖档位默认。"""
        st = KunpengStrategy(take_profit_pct=0.30, stop_loss_pct=0.02)
        assert st.take_profit_pct == pytest.approx(0.30)
        assert st.stop_loss_pct == pytest.approx(0.02)
        # 移动止盈仍随档位（standard）
        assert st.trailing_activate_pct == pytest.approx(0.08)

    def test_exit_stop_loss(self):
        """止损：收益率 <= -止损线 → 触发。"""
        st = KunpengStrategy()
        assert st._evaluate_exit(ret=-0.08, days_held=1, peak_close=10.0,
                                 entry_close=10.0, close=9.2) is not None

    def test_exit_take_profit(self):
        """止盈：收益率 >= 止盈线 → 触发。"""
        st = KunpengStrategy()
        assert st._evaluate_exit(ret=0.16, days_held=1, peak_close=10.0,
                                 entry_close=10.0, close=11.6) is not None

    def test_exit_trailing_activated(self):
        """移动止盈：盈利>8% 后峰后回撤超3% → 触发（标准型）。"""
        st = KunpengStrategy()
        # peak=10.9（+9% ≥ 激活线8%），close=10.5（峰后回撤 -3.67% ≤ -3%）
        reason = st._evaluate_exit(ret=0.05, days_held=5, peak_close=10.9,
                                   entry_close=10.0, close=10.5)
        assert reason is not None
        assert "trailing" in reason

    def test_exit_trailing_not_activated(self):
        """移动止盈未达激活线（盈利7%<8%）→ 不触发，即使回撤超3%。"""
        st = KunpengStrategy()
        reason = st._evaluate_exit(ret=0.03, days_held=5, peak_close=10.7,
                                   entry_close=10.0, close=10.3)
        assert reason is None

    def test_exit_trailing_disabled_conservative(self):
        """保守型无移动止盈：盈利10% 后回撤3.67% → 不触发（仅止损/止盈/上限）。"""
        st = KunpengStrategy(exit_mode="conservative")
        reason = st._evaluate_exit(ret=0.06, days_held=5, peak_close=11.0,
                                   entry_close=10.0, close=10.6)
        assert reason is None

    def test_exit_hold_cap(self):
        """持有上限：days_held>=60 → 触发（兜底离场）。"""
        st = KunpengStrategy()
        reason = st._evaluate_exit(ret=0.01, days_held=60, peak_close=10.2,
                                   entry_close=10.0, close=10.1)
        assert reason is not None
        assert "hold_cap" in reason
        # 未到上限不触发
        assert st._evaluate_exit(ret=0.01, days_held=59, peak_close=10.2,
                                 entry_close=10.0, close=10.1) is None

    def test_exit_no_condition(self):
        """四条件均未触发 → None。"""
        st = KunpengStrategy()
        assert st._evaluate_exit(ret=0.02, days_held=5, peak_close=10.2,
                                 entry_close=10.0, close=10.2) is None


# ═══════════════════════════════════════════════
#  TestKunpengValidation - 参数校验
# ═══════════════════════════════════════════════

class TestKunpengValidation:
    """非法参数 → ValueError（表4 参数取值范围）。"""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"exit_mode": "bogus"},
            {"vol_ratio_threshold": 0.0},
            {"vol_ratio_threshold": -1.0},
            {"vol_count_window": 0},
            {"vol_active_days": 0},
            {"vol_active_days": 7},          # > vol_count_window=6
            {"barslast_vol_window": 0},
            {"barslast_ratio_window": 0},
            {"ma_sticky_threshold": 1.0},    # 极差比恒>=1，必须>1
            {"ma_sticky_threshold": 0.9},
            {"diff_dea_ratio": 0.0},
            {"diff_dea_ratio": 1.5},
            {"macd_abs_threshold": -0.1},
            {"macd_rise_threshold": -0.0001},
            {"hold_days_cap": 0},
            {"max_position_pct": 0.0},
            {"max_position_pct": 1.5},
            {"max_positions": 0},
            {"capital_base": 0.0},
            {"stop_loss_pct": 0.0},          # 显式越界仍校验
            {"take_profit_pct": 1.2},
        ],
    )
    def test_invalid_params_raise(self, kwargs: Dict[str, Any]):
        """非法参数抛 ValueError。"""
        with pytest.raises(ValueError):
            KunpengStrategy(**kwargs)

    def test_valid_custom_params(self):
        """合法自定义参数正常实例化。"""
        st = KunpengStrategy(
            vol_ratio_threshold=3.0, vol_count_window=8, vol_active_days=4,
            barslast_vol_window=8, barslast_ratio_window=10,
            ma_sticky_threshold=1.30, diff_dea_ratio=0.010, macd_abs_threshold=0.05,
            macd_rise_threshold=0.001,
            hold_days_cap=20, max_position_pct=0.10, max_positions=3,
            capital_base=500_000.0,
        )
        assert st.vol_ratio_threshold == 3.0
        assert st.hold_days_cap == 20
        assert st.capital_base == 500_000.0
        assert st.macd_rise_threshold == 0.001


# ═══════════════════════════════════════════════
#  TestKunpengThreeInterfaces - 三接口一致性（表9 第5组）
# ═══════════════════════════════════════════════

class TestKunpengThreeInterfaces:
    """同一数据上 event/mixed/vectorized 产生相同 BUY/SELL 信号日集合。"""

    @staticmethod
    def _buy_dates(result: Any) -> List[str]:
        return sorted(
            pd.Timestamp(t.timestamp, unit="s").strftime("%Y-%m-%d")
            for t in result.trades if t.side == "BUY"
        )

    @staticmethod
    def _sell_dates(result: Any) -> List[str]:
        return sorted(
            pd.Timestamp(t.timestamp, unit="s").strftime("%Y-%m-%d")
            for t in result.trades if t.side == "SELL"
        )

    def test_three_modes_same_signal_days(self):
        """三模式成交信号日集合一致（含入场与出场）。"""
        df = _signal_data()
        data = {SYMBOL: df}
        config = BacktestConfig(
            symbols=[SYMBOL],
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=INITIAL_CAPITAL,
        )
        results: Dict[str, Any] = {}
        for mode in ("event", "mixed", "vectorized"):
            result = run_backtest(config, KunpengStrategy(), data=data, mode=mode)
            assert result.is_success, f"{mode} 回测失败: {result.status}"
            results[mode] = result

        event_buy = self._buy_dates(results["event"])
        assert len(event_buy) >= 1, "构造数据应产生买入"
        for mode in ("mixed", "vectorized"):
            assert self._buy_dates(results[mode]) == event_buy, (
                f"{mode} 买入日与 event 不一致"
            )
        event_sell = self._sell_dates(results["event"])
        for mode in ("mixed", "vectorized"):
            assert self._sell_dates(results[mode]) == event_sell, (
                f"{mode} 卖出日与 event 不一致"
            )

    def test_take_profit_exit_reason(self):
        """三模式均止盈卖出；event 模式（策略信号路径）原因含 take_profit。

        注：mixed/vectorized 引擎对目标持仓归零生成的卖出单标记为 target=0
        （引擎层标记），原因断言仅适用于 event 策略信号路径。
        """
        df = _signal_data()
        data = {SYMBOL: df}
        config = BacktestConfig(
            symbols=[SYMBOL],
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=INITIAL_CAPITAL,
        )
        for mode in ("event", "mixed", "vectorized"):
            result = run_backtest(config, KunpengStrategy(), data=data, mode=mode)
            sells = [t for t in result.trades if t.side == "SELL"]
            assert len(sells) == 1, f"{mode} 应恰好1笔卖出"
            if mode == "event":
                assert "take_profit" in sells[0].exit_reason, (
                    f"{mode} 出场原因异常: {sells[0].exit_reason}"
                )


# ═══════════════════════════════════════════════
#  TestKunpengNoLookahead - 防未来（表9 第6组）
# ═══════════════════════════════════════════════

class TestKunpengNoLookahead:
    """回测后 LookaheadGuard.assert_pass() 必须通过（引擎 T日信号→T+1日开盘成交）。"""

    @pytest.mark.lookahead
    def test_event_mode_lookahead_guard_pass(self):
        """event 模式（真值基准）：防未来校验通过。"""
        df = _signal_data()
        config = BacktestConfig(
            symbols=[SYMBOL],
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=INITIAL_CAPITAL,
        )
        guard = LookaheadGuard()
        result = run_backtest(
            config, KunpengStrategy(), data={SYMBOL: df},
            mode="event", lookahead_guard=guard,
        )
        assert result.is_success
        guard.assert_pass()  # 违规会抛异常

    @pytest.mark.lookahead
    def test_vectorized_mode_lookahead_guard_pass(self):
        """vectorized 模式（参数扫描）：防未来校验通过。"""
        df = _signal_data()
        config = BacktestConfig(
            symbols=[SYMBOL],
            start_date=str(df.index[0].date()),
            end_date=str(df.index[-1].date()),
            initial_capital=INITIAL_CAPITAL,
        )
        guard = LookaheadGuard()
        result = run_backtest(
            config, KunpengStrategy(), data={SYMBOL: df},
            mode="vectorized", lookahead_guard=guard,
        )
        assert result.is_success
        guard.assert_pass()

    @pytest.mark.lookahead
    def test_future_change_does_not_affect_past_signals(self):
        """防未来铁律：修改未来数据不影响当前 bar 之前的信号（event 逐bar）。"""
        df1 = _signal_data()
        ctx1 = _make_context(df1)
        st1 = KunpengStrategy()
        st1.on_init(ctx1)
        sig_dates1: List[pd.Timestamp] = []
        for i in range(len(df1)):
            bar = _make_bar(SYMBOL, df1["收盘价"].iloc[i],
                            df1.index[i].timestamp(),
                            df1["成交量"].iloc[i])
            for s in st1.on_bar(bar, ctx1):
                if s.is_buy:
                    sig_dates1.append(df1.index[i])

        # 修改信号日(索引79)之后的数据（未来数据）：BUY 信号只依赖 [0..79]，不应漂移
        df2 = df1.copy()
        df2.iloc[80:, df2.columns.get_loc("收盘价")] *= 3.0
        ctx2 = _make_context(df2)
        st2 = KunpengStrategy()
        st2.on_init(ctx2)
        sig_dates2: List[pd.Timestamp] = []
        for i in range(len(df2)):
            bar = _make_bar(SYMBOL, df2["收盘价"].iloc[i],
                            df2.index[i].timestamp(),
                            df2["成交量"].iloc[i])
            for s in st2.on_bar(bar, ctx2):
                if s.is_buy:
                    sig_dates2.append(df2.index[i])

        # 校验点1：预热期(60根)内不应有信号
        assert not any(d < df1.index[WARMUP_BARS] for d in sig_dates1), (
            "预热期(60根)内不应有信号"
        )
        # 校验点2：修改信号日之后的数据，BUY 信号日集合应保持一致（策略只依赖历史数据）
        assert sig_dates1 == sig_dates2, (
            f"未来数据修改导致信号漂移: {sig_dates1} vs {sig_dates2}"
        )


# ═══════════════════════════════════════════════
#  TestKunpengRegistry - 注册与工厂（M1-B 接入验证）
# ═══════════════════════════════════════════════

class TestKunpengRegistry:
    """注册器/工厂/批量注册表接入。"""

    def test_factory_create(self):
        """create_strategy('kunpeng') 返回 KunpengStrategy。"""
        discover_strategies()
        st = create_strategy("kunpeng")
        assert isinstance(st, KunpengStrategy)
        assert st.name == "kunpeng"

    def test_list_strategies_contains(self):
        """list_strategies 含 kunpeng。"""
        discover_strategies()
        assert "kunpeng" in list_strategies()

    def test_engine_modes(self):
        """三引擎模式全部支持。"""
        st = KunpengStrategy()
        assert sorted(st.engine_modes()) == ["event", "mixed", "vectorized"]

    def test_batch_registry_contains(self):
        """批量回测 STRATEGY_REGISTRY 含 kunpeng。"""
        assert STRATEGY_REGISTRY["kunpeng"] is KunpengStrategy
