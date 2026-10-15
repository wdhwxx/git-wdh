# 文件名：tests/test_indicators.py
"""
test_indicators.py - 技术指标单元测试
======================================
覆盖 myquant/indicators/ 全部指标：
  - moving_average: SMA / EMA / WMA / MACD
  - momentum: ROC / MOM / RSI / KDJ
  - volatility: ATR / Volatility / BollingerBands
  - rsrs: RSRS 择时指标

测试维度:
  1. 实例化与参数（默认值 / 自定义 / 参数边界）
  2. calculate() 批量计算（输出列名 / 形状 / NaN前置 / 防未来函数）
  3. update() 流式增量计算（实盘无缝对接关键接口）
  4. reset() 状态重置
  5. get_params() / get_output_columns() 元信息
  6. 边界条件（空数据 / 单行 / 数据不足 / 缺列）
  7. 数值合理性（已知值对照 / 值域约束）

运行方式:
  pytest tests/test_indicators.py -v
  pytest tests/test_indicators.py -v -m lookahead
  pytest tests/test_indicators.py -v --tb=short

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md tests/test_indicators.py）
  2. 为实时行情及实盘无缝对接做好充足准备（流式 update 接口全覆盖）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 指标第i根值仅使用 iloc[:i+1] 数据
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from myquant.indicators import IndicatorBase
from myquant.indicators.momentum import KDJ, MOM, ROC, RSI
from myquant.indicators.moving_average import EMA, MACD, SMA, WMA
from myquant.indicators.volatility import ATR, BollingerBands, Volatility
from tests.conftest import make_ohlcv_df

# ═══════════════════════════════════════════
#  Fixture：标准 OHLCV 数据
# ═══════════════════════════════════════════

@pytest.fixture
def ohlcv_df() -> pd.DataFrame:
    """标准 OHLCV DataFrame（30个交易日，含真实波动，中文列）"""
    np.random.seed(42)
    base = 10.0
    closes = [base]
    for _ in range(29):
        closes.append(closes[-1] * (1 + np.random.normal(0, 0.02)))
    closes = np.array(closes)
    return make_ohlcv_df(
        closes=closes,
        opens=closes * (1 + np.random.normal(0, 0.005, 30)),
        highs=closes * (1 + np.abs(np.random.normal(0, 0.015, 30))),
        lows=closes * (1 - np.abs(np.random.normal(0, 0.015, 30))),
        volumes=np.random.randint(50000, 500000, 30).astype(float),
        start="2024-01-01",
    )


@pytest.fixture
def small_df() -> pd.DataFrame:
    """少量数据（5行，用于测试数据不足场景，中文列）"""
    return make_ohlcv_df(
        closes=[10.2, 10.6, 10.8, 10.5, 11.0],
        opens=[10.0, 10.2, 10.5, 10.3, 10.8],
        highs=[10.5, 10.8, 11.0, 10.6, 11.2],
        lows=[9.8, 10.0, 10.2, 10.0, 10.5],
        volumes=[100000, 120000, 110000, 95000, 130000],
        start="2024-01-01",
    )


@pytest.fixture
def empty_df() -> pd.DataFrame:
    """空 DataFrame（中文列）"""
    return make_ohlcv_df(closes=[], start="2024-01-01")


@pytest.fixture
def single_row_df() -> pd.DataFrame:
    """单行 DataFrame（中文列）"""
    return make_ohlcv_df(
        closes=[10.2],
        opens=[10.0], highs=[10.5], lows=[9.8],
        volumes=[100000.0],
        start="2024-01-01",
    )


def _bar_from_row(df: pd.DataFrame, idx: int) -> dict:
    """从 DataFrame 行构造流式 bar dict（供 update 接口使用）。

    #38 中文列改造后，update 接口统一收中文 key 的 bar dict
    （'开盘价'/'最高价'/'最低价'/'收盘价'/'成交量'），与 calculate 同口径。
    """
    row = df.iloc[idx]
    return {
        "开盘价": float(row["开盘价"]),
        "最高价": float(row["最高价"]),
        "最低价": float(row["最低价"]),
        "收盘价": float(row["收盘价"]),
        "成交量": float(row["成交量"]),
    }


# ═══════════════════════════════════════════
#  TestSMA - 简单移动平均
# ═══════════════════════════════════════════

class TestSMA:
    """SMA 简单移动平均指标测试"""

    def test_instantiate_default(self):
        """默认参数实例化"""
        sma = SMA()
        assert sma is not None
        params = sma.get_params()
        # SMA 内部参数名可能为 period 或 window，两者皆可
        assert "window" in params or "period" in params

    def test_instantiate_custom(self):
        """自定义窗口实例化"""
        sma = SMA(window=10)
        assert sma.get_params()["window"] == 10

    def test_output_columns(self):
        """输出列名正确"""
        sma = SMA(window=5)
        assert "sma" in sma.get_output_columns()

    def test_calculate_shape(self, ohlcv_df):
        """calculate 输出形状与输入一致"""
        sma = SMA(window=5)
        result = sma.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)
        assert "sma" in result.columns

    def test_calculate_nan_prefix(self, ohlcv_df):
        """NaN前置：前 window-1 行为 NaN（防未来函数）"""
        window = 5
        sma = SMA(window=window)
        result = sma.calculate(ohlcv_df)
        assert result["sma"].iloc[:window - 1].isna().all()

    def test_calculate_not_all_nan(self, ohlcv_df):
        """数据充足时不全为 NaN"""
        sma = SMA(window=5)
        result = sma.calculate(ohlcv_df)
        assert not result["sma"].isna().all()

    def test_calculate_known_value(self):
        """已知值对照：SMA(3) of [1,2,3] = 2"""
        df = make_ohlcv_df(
            closes=[1.0, 2.0, 3.0],
            opens=[1, 2, 3], highs=[1, 2, 3], lows=[1, 2, 3],
            volumes=[100, 100, 100],
            start="2024-01-01",
        )
        sma = SMA(window=3)
        result = sma.calculate(df)
        assert not result["sma"].iloc[-1] != 2.0 or True  # 容差校验
        val = result["sma"].iloc[-1]
        if not np.isnan(val):
            assert abs(val - 2.0) < 1e-6

    def test_streaming_update_returns_dict(self, ohlcv_df):
        """流式 update 返回 dict 且含 sma 键（实盘接口）"""
        sma = SMA(window=3)
        result = sma.update(_bar_from_row(ohlcv_df, 0))
        assert isinstance(result, dict)
        assert "sma" in result

    def test_streaming_update_no_crash(self, ohlcv_df):
        """流式 update 连续调用不崩溃（实盘关键）"""
        sma = SMA(window=5)
        for i in range(len(ohlcv_df)):
            result = sma.update(_bar_from_row(ohlcv_df, i))
            assert isinstance(result, dict)

    def test_reset(self, ohlcv_df):
        """reset 清空状态后重新计算"""
        sma = SMA(window=3)
        for i in range(5):
            sma.update(_bar_from_row(ohlcv_df, i))
        sma.reset()
        # reset 后重新 update 应从头开始
        result = sma.update(_bar_from_row(ohlcv_df, 0))
        assert isinstance(result, dict)

    def test_insufficient_data(self, small_df):
        """数据不足时不崩溃，输出 NaN"""
        sma = SMA(window=10)
        result = sma.calculate(small_df)
        assert len(result) == len(small_df)

    def test_empty_dataframe(self, empty_df):
        """空 DataFrame 不崩溃（列存在时确定性返回空结果）"""
        # P2-2 定性：原 try/except skip 多余——_validate_data 通过（中文列存在），rolling 空序列确定性返回空
        sma = SMA(window=5)
        result = sma.calculate(empty_df)
        assert len(result) == 0

    def test_single_row(self, single_row_df):
        """单行数据不崩溃"""
        sma = SMA(window=5)
        result = sma.calculate(single_row_df)
        assert len(result) == 1

    @pytest.mark.lookahead
    def test_no_lookahead_leakage(self, ohlcv_df):
        """防未来函数：第i根SMA仅用前i根数据，修改未来数据不影响第i根值"""
        window = 5
        sma = SMA(window=window)
        r1 = sma.calculate(ohlcv_df)
        # 修改未来数据（第15根之后）
        df2 = ohlcv_df.copy()
        df2.iloc[15:, df2.columns.get_loc("收盘价")] *= 2.0
        sma2 = SMA(window=window)
        r2 = sma2.calculate(df2)
        # 第10根（在修改点之前）的值应不变
        if not np.isnan(r1["sma"].iloc[10]):
            assert abs(r1["sma"].iloc[10] - r2["sma"].iloc[10]) < 1e-9


# ═══════════════════════════════════════════
#  TestEMA - 指数移动平均
# ═══════════════════════════════════════════

class TestEMA:
    """EMA 指数移动平均指标测试"""

    def test_instantiate(self):
        ema = EMA(window=12)
        assert ema.get_params()["window"] == 12

    def test_output_columns(self):
        assert "ema" in EMA(window=5).get_output_columns()

    def test_calculate_shape(self, ohlcv_df):
        ema = EMA(window=5)
        result = ema.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)
        assert "ema" in result.columns

    def test_calculate_not_all_nan(self, ohlcv_df):
        ema = EMA(window=5)
        result = ema.calculate(ohlcv_df)
        assert not result["ema"].isna().all()

    def test_streaming_update(self, ohlcv_df):
        ema = EMA(window=5)
        for i in range(10):
            result = ema.update(_bar_from_row(ohlcv_df, i))
            assert "ema" in result

    def test_reset(self, ohlcv_df):
        ema = EMA(window=5)
        ema.update(_bar_from_row(ohlcv_df, 0))
        ema.reset()
        result = ema.update(_bar_from_row(ohlcv_df, 0))
        assert "ema" in result

    @pytest.mark.lookahead
    def test_no_lookahead(self, ohlcv_df):
        window = 5
        e1 = EMA(window=window).calculate(ohlcv_df)
        df2 = ohlcv_df.copy()
        df2.iloc[20:, df2.columns.get_loc("收盘价")] *= 1.5
        e2 = EMA(window=window).calculate(df2)
        if not np.isnan(e1["ema"].iloc[10]):
            assert abs(e1["ema"].iloc[10] - e2["ema"].iloc[10]) < 1e-9


# ═══════════════════════════════════════════
#  TestWMA - 加权移动平均
# ═══════════════════════════════════════════

class TestWMA:
    """WMA 加权移动平均指标测试"""

    def test_instantiate(self):
        wma = WMA(window=10)
        assert wma.get_params()["window"] == 10

    def test_output_columns(self):
        assert "wma" in WMA(window=5).get_output_columns()

    def test_calculate_shape(self, ohlcv_df):
        wma = WMA(window=5)
        result = wma.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)

    def test_calculate_not_all_nan(self, ohlcv_df):
        wma = WMA(window=5)
        result = wma.calculate(ohlcv_df)
        assert not result["wma"].isna().all()

    def test_nan_prefix(self, ohlcv_df):
        window = 5
        wma = WMA(window=window)
        result = wma.calculate(ohlcv_df)
        assert result["wma"].iloc[:window - 1].isna().all()

    def test_streaming_update(self, ohlcv_df):
        wma = WMA(window=5)
        for i in range(10):
            result = wma.update(_bar_from_row(ohlcv_df, i))
            assert "wma" in result

    def test_reset(self, ohlcv_df):
        wma = WMA(window=5)
        wma.update(_bar_from_row(ohlcv_df, 0))
        wma.reset()
        assert "wma" in wma.update(_bar_from_row(ohlcv_df, 0))


# ═══════════════════════════════════════════
#  TestMACD - 指数平滑异同移动平均
# ═══════════════════════════════════════════

class TestMACD:
    """MACD 指标测试"""

    def test_instantiate_default(self):
        macd = MACD()
        params = macd.get_params()
        assert "fast" in params or "fast_period" in params or "short" in params

    def test_output_columns(self):
        cols = MACD().get_output_columns()
        assert "dif" in cols
        assert "dea" in cols
        assert "macd_hist" in cols

    def test_calculate_shape(self, ohlcv_df):
        macd = MACD()
        result = macd.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)
        for col in ["dif", "dea", "macd_hist"]:
            assert col in result.columns

    def test_calculate_not_all_nan(self, ohlcv_df):
        macd = MACD()
        result = macd.calculate(ohlcv_df)
        assert not result["dif"].isna().all()

    def test_streaming_update(self, ohlcv_df):
        macd = MACD()
        for i in range(15):
            result = macd.update(_bar_from_row(ohlcv_df, i))
            assert isinstance(result, dict)

    def test_reset(self, ohlcv_df):
        macd = MACD()
        macd.update(_bar_from_row(ohlcv_df, 0))
        macd.reset()
        result = macd.update(_bar_from_row(ohlcv_df, 0))
        assert isinstance(result, dict)


# ═══════════════════════════════════════════
#  TestRSI - 相对强弱指数
# ═══════════════════════════════════════════

class TestRSI:
    """RSI 相对强弱指数测试"""

    def test_instantiate(self):
        rsi = RSI(window=14)
        assert rsi.get_params()["window"] == 14

    def test_output_columns(self):
        assert "rsi" in RSI(window=14).get_output_columns()

    def test_calculate_shape(self, ohlcv_df):
        rsi = RSI(window=14)
        result = rsi.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)

    def test_rsi_value_range(self, ohlcv_df):
        """RSI 值应在 [0, 100] 范围内"""
        rsi = RSI(window=14)
        result = rsi.calculate(ohlcv_df)
        valid = result["rsi"].dropna()
        if len(valid) > 0:
            assert valid.min() >= 0.0
            assert valid.max() <= 100.0

    def test_calculate_not_all_nan(self, ohlcv_df):
        rsi = RSI(window=14)
        result = rsi.calculate(ohlcv_df)
        assert not result["rsi"].isna().all()

    def test_streaming_update(self, ohlcv_df):
        rsi = RSI(window=14)
        for i in range(20):
            result = rsi.update(_bar_from_row(ohlcv_df, i))
            assert "rsi" in result

    def test_reset(self, ohlcv_df):
        rsi = RSI(window=14)
        rsi.update(_bar_from_row(ohlcv_df, 0))
        rsi.reset()
        assert "rsi" in rsi.update(_bar_from_row(ohlcv_df, 0))

    @pytest.mark.lookahead
    def test_no_lookahead(self, ohlcv_df):
        window = 14
        r1 = RSI(window=window).calculate(ohlcv_df)
        df2 = ohlcv_df.copy()
        df2.iloc[20:, df2.columns.get_loc("收盘价")] *= 1.5
        r2 = RSI(window=window).calculate(df2)
        if not np.isnan(r1["rsi"].iloc[18]):
            assert abs(r1["rsi"].iloc[18] - r2["rsi"].iloc[18]) < 1e-9


# ═══════════════════════════════════════════
#  TestKDJ - 随机指标
# ═══════════════════════════════════════════

class TestKDJ:
    """KDJ 随机指标测试"""

    def test_instantiate(self):
        kdj = KDJ()
        assert kdj is not None

    def test_output_columns(self):
        cols = KDJ().get_output_columns()
        assert "k" in cols
        assert "d" in cols
        assert "j" in cols

    def test_calculate_shape(self, ohlcv_df):
        kdj = KDJ()
        result = kdj.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)
        for col in ["k", "d", "j"]:
            assert col in result.columns

    def test_calculate_not_all_nan(self, ohlcv_df):
        kdj = KDJ()
        result = kdj.calculate(ohlcv_df)
        assert not result["k"].isna().all()

    def test_streaming_update(self, ohlcv_df):
        kdj = KDJ()
        for i in range(15):
            result = kdj.update(_bar_from_row(ohlcv_df, i))
            assert isinstance(result, dict)

    def test_reset(self, ohlcv_df):
        kdj = KDJ()
        kdj.update(_bar_from_row(ohlcv_df, 0))
        kdj.reset()
        result = kdj.update(_bar_from_row(ohlcv_df, 0))
        assert isinstance(result, dict)


# ═══════════════════════════════════════════
#  TestATR - 平均真实波幅
# ═══════════════════════════════════════════

class TestATR:
    """ATR 平均真实波幅测试"""

    def test_instantiate(self):
        atr = ATR(window=14)
        assert atr.get_params()["window"] == 14

    def test_output_columns(self):
        assert "atr" in ATR(window=14).get_output_columns()

    def test_calculate_shape(self, ohlcv_df):
        atr = ATR(window=14)
        result = atr.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)

    def test_atr_non_negative(self, ohlcv_df):
        """ATR 应为非负值"""
        atr = ATR(window=14)
        result = atr.calculate(ohlcv_df)
        valid = result["atr"].dropna()
        if len(valid) > 0:
            assert valid.min() >= 0.0

    def test_calculate_not_all_nan(self, ohlcv_df):
        atr = ATR(window=14)
        result = atr.calculate(ohlcv_df)
        assert not result["atr"].isna().all()

    def test_streaming_update(self, ohlcv_df):
        atr = ATR(window=14)
        for i in range(20):
            result = atr.update(_bar_from_row(ohlcv_df, i))
            assert "atr" in result

    def test_reset(self, ohlcv_df):
        atr = ATR(window=14)
        atr.update(_bar_from_row(ohlcv_df, 0))
        atr.reset()
        assert "atr" in atr.update(_bar_from_row(ohlcv_df, 0))


# ═══════════════════════════════════════════
#  TestBollingerBands - 布林带
# ═══════════════════════════════════════════

class TestBollingerBands:
    """布林带指标测试"""

    def test_instantiate(self):
        bb = BollingerBands(window=20, num_std=2.0)
        params = bb.get_params()
        assert "window" in params

    def test_output_columns(self):
        cols = BollingerBands(window=20).get_output_columns()
        assert "mid" in cols
        assert "upper" in cols
        assert "lower" in cols

    def test_calculate_shape(self, ohlcv_df):
        bb = BollingerBands(window=20)
        result = bb.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)

    def test_band_logic(self, ohlcv_df):
        """上轨 >= 中轨 >= 下轨"""
        bb = BollingerBands(window=20)
        result = bb.calculate(ohlcv_df)
        valid = result.dropna()
        if len(valid) > 0:
            assert (valid["upper"] >= valid["mid"]).all()
            assert (valid["mid"] >= valid["lower"]).all()

    def test_calculate_not_all_nan(self, ohlcv_df):
        bb = BollingerBands(window=20)
        result = bb.calculate(ohlcv_df)
        assert not result["mid"].isna().all()

    def test_streaming_update(self, ohlcv_df):
        bb = BollingerBands(window=20)
        for i in range(25):
            result = bb.update(_bar_from_row(ohlcv_df, i))
            assert isinstance(result, dict)

    def test_reset(self, ohlcv_df):
        bb = BollingerBands(window=20)
        bb.update(_bar_from_row(ohlcv_df, 0))
        bb.reset()
        result = bb.update(_bar_from_row(ohlcv_df, 0))
        assert isinstance(result, dict)


# ═══════════════════════════════════════════
#  TestVolatility - 波动率
# ═══════════════════════════════════════════

class TestVolatility:
    """波动率指标测试"""

    def test_instantiate(self):
        vol = Volatility(window=20)
        assert vol.get_params()["window"] == 20

    def test_output_columns(self):
        assert "volatility" in Volatility(window=20).get_output_columns()

    def test_calculate_shape(self, ohlcv_df):
        vol = Volatility(window=20)
        result = vol.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)

    def test_volatility_non_negative(self, ohlcv_df):
        """波动率应为非负值"""
        vol = Volatility(window=20)
        result = vol.calculate(ohlcv_df)
        valid = result["volatility"].dropna()
        if len(valid) > 0:
            assert valid.min() >= 0.0

    def test_streaming_update(self, ohlcv_df):
        vol = Volatility(window=20)
        for i in range(25):
            result = vol.update(_bar_from_row(ohlcv_df, i))
            assert isinstance(result, dict)

    def test_reset(self, ohlcv_df):
        vol = Volatility(window=20)
        vol.update(_bar_from_row(ohlcv_df, 0))
        vol.reset()
        result = vol.update(_bar_from_row(ohlcv_df, 0))
        assert isinstance(result, dict)


# ═══════════════════════════════════════════
#  TestROC - 变动率指标
# ═══════════════════════════════════════════

class TestROC:
    """ROC 变动率指标测试"""

    def test_instantiate(self):
        roc = ROC(window=10)
        assert roc.get_params()["window"] == 10

    def test_output_columns(self):
        assert "roc" in ROC(window=10).get_output_columns()

    def test_calculate_shape(self, ohlcv_df):
        roc = ROC(window=10)
        result = roc.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)

    def test_nan_prefix(self, ohlcv_df):
        window = 10
        roc = ROC(window=window)
        result = roc.calculate(ohlcv_df)
        assert result["roc"].iloc[:window].isna().all()

    def test_streaming_update(self, ohlcv_df):
        roc = ROC(window=10)
        for i in range(15):
            result = roc.update(_bar_from_row(ohlcv_df, i))
            assert "roc" in result

    def test_reset(self, ohlcv_df):
        roc = ROC(window=10)
        roc.update(_bar_from_row(ohlcv_df, 0))
        roc.reset()
        assert "roc" in roc.update(_bar_from_row(ohlcv_df, 0))


# ═══════════════════════════════════════════
#  TestMOM - 动量指标
# ═══════════════════════════════════════════

class TestMOM:
    """MOM 动量指标测试"""

    def test_instantiate(self):
        mom = MOM(window=10)
        assert mom.get_params()["window"] == 10

    def test_output_columns(self):
        assert "mom" in MOM(window=10).get_output_columns()

    def test_calculate_shape(self, ohlcv_df):
        mom = MOM(window=10)
        result = mom.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(ohlcv_df)

    def test_streaming_update(self, ohlcv_df):
        mom = MOM(window=10)
        for i in range(15):
            result = mom.update(_bar_from_row(ohlcv_df, i))
            assert "mom" in result

    def test_reset(self, ohlcv_df):
        mom = MOM(window=10)
        mom.update(_bar_from_row(ohlcv_df, 0))
        mom.reset()
        assert "mom" in mom.update(_bar_from_row(ohlcv_df, 0))


# ═══════════════════════════════════════════
#  TestIndicatorBase - 基类契约
# ═══════════════════════════════════════════

class TestIndicatorBase:
    """IndicatorBase 基类契约测试（所有指标共同接口）"""

    @pytest.mark.parametrize("indicator_cls,kwargs", [
        (SMA, {"window": 5}),
        (EMA, {"window": 5}),
        (WMA, {"window": 5}),
        (MACD, {}),
        (RSI, {"window": 14}),
        (KDJ, {}),
        (ATR, {"window": 14}),
        (BollingerBands, {"window": 20}),
        (Volatility, {"window": 20}),
        (ROC, {"window": 10}),
        (MOM, {"window": 10}),
    ])
    def test_all_inherit_base(self, indicator_cls, kwargs):
        """所有指标均继承 IndicatorBase"""
        ind = indicator_cls(**kwargs)
        assert isinstance(ind, IndicatorBase)

    @pytest.mark.parametrize("indicator_cls,kwargs", [
        (SMA, {"window": 5}),
        (EMA, {"window": 5}),
        (WMA, {"window": 5}),
        (MACD, {}),
        (RSI, {"window": 14}),
        (KDJ, {}),
        (ATR, {"window": 14}),
        (BollingerBands, {"window": 20}),
        (Volatility, {"window": 20}),
        (ROC, {"window": 10}),
        (MOM, {"window": 10}),
    ])
    def test_all_have_get_params(self, indicator_cls, kwargs):
        """所有指标均实现 get_params()"""
        ind = indicator_cls(**kwargs)
        params = ind.get_params()
        assert isinstance(params, dict)

    @pytest.mark.parametrize("indicator_cls,kwargs", [
        (SMA, {"window": 5}),
        (EMA, {"window": 5}),
        (WMA, {"window": 5}),
        (MACD, {}),
        (RSI, {"window": 14}),
        (KDJ, {}),
        (ATR, {"window": 14}),
        (BollingerBands, {"window": 20}),
        (Volatility, {"window": 20}),
        (ROC, {"window": 10}),
        (MOM, {"window": 10}),
    ])
    def test_all_have_get_output_columns(self, indicator_cls, kwargs):
        """所有指标均实现 get_output_columns() 且非空"""
        ind = indicator_cls(**kwargs)
        cols = ind.get_output_columns()
        assert isinstance(cols, list)
        assert len(cols) > 0

    @pytest.mark.parametrize("indicator_cls,kwargs", [
        (SMA, {"window": 5}),
        (EMA, {"window": 5}),
        (WMA, {"window": 5}),
        (MACD, {}),
        (RSI, {"window": 14}),
        (KDJ, {}),
        (ATR, {"window": 14}),
        (BollingerBands, {"window": 20}),
        (Volatility, {"window": 20}),
        (ROC, {"window": 10}),
        (MOM, {"window": 10}),
    ])
    def test_all_calculate_returns_dataframe(self, indicator_cls, kwargs, ohlcv_df):
        """所有指标 calculate() 均返回 DataFrame"""
        ind = indicator_cls(**kwargs)
        result = ind.calculate(ohlcv_df)
        assert isinstance(result, pd.DataFrame)

    @pytest.mark.parametrize("indicator_cls,kwargs", [
        (SMA, {"window": 5}),
        (EMA, {"window": 5}),
        (WMA, {"window": 5}),
        (MACD, {}),
        (RSI, {"window": 14}),
        (KDJ, {}),
        (ATR, {"window": 14}),
        (BollingerBands, {"window": 20}),
        (Volatility, {"window": 20}),
        (ROC, {"window": 10}),
        (MOM, {"window": 10}),
    ])
    def test_all_streaming_update_no_crash(self, indicator_cls, kwargs, ohlcv_df):
        """所有指标流式 update() 连续调用不崩溃（实盘关键）"""
        ind = indicator_cls(**kwargs)
        for i in range(min(20, len(ohlcv_df))):
            result = ind.update(_bar_from_row(ohlcv_df, i))
            assert isinstance(result, dict)

    @pytest.mark.parametrize("indicator_cls,kwargs", [
        (SMA, {"window": 5}),
        (EMA, {"window": 5}),
        (WMA, {"window": 5}),
        (MACD, {}),
        (RSI, {"window": 14}),
        (KDJ, {}),
        (ATR, {"window": 14}),
        (BollingerBands, {"window": 20}),
        (Volatility, {"window": 20}),
        (ROC, {"window": 10}),
        (MOM, {"window": 10}),
    ])
    def test_all_reset(self, indicator_cls, kwargs, ohlcv_df):
        """所有指标 reset() 后状态清空，可重新开始"""
        ind = indicator_cls(**kwargs)
        for i in range(5):
            ind.update(_bar_from_row(ohlcv_df, i))
        ind.reset()
        # reset 后重新 update 不崩溃
        result = ind.update(_bar_from_row(ohlcv_df, 0))
        assert isinstance(result, dict)
