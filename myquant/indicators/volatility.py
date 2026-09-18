# 文件名：myquant/indicators/volatility.py
"""
鲲鹏量化交易系统 - 波动率指标（Volatility Indicators）
=========================================================
包含：ATR（平均真实波幅）、Volatility（收益率波动率）、BollingerBands（布林带）

三因子股票池之波动率因子：Volatility(period=20)，取前1/3高波动股票

防未来函数说明：
- ATR的TR使用当根K线的high/low/prev_close，rolling窗口严格左闭右闭
- Volatility使用pct_chg列（百分比格式），计算时除以100转小数
- 布林带基于SMA+标准差，rolling窗口无未来函数
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Any, Dict

import numpy as np
import pandas as pd

from myquant.indicators import IndicatorBase, lookahead_check, register_indicator


# ============================================================================
# ATR - 平均真实波幅（Average True Range）
# ============================================================================

@register_indicator
class ATR(IndicatorBase):
    """
    平均真实波幅（Average True Range）

    公式:
        TR_t = max(
            high_t - low_t,                    # 当日振幅
            abs(high_t - close_{t-1}),         # 最高与前收差
            abs(low_t - close_{t-1})           # 最低与前收差
        )
        ATR_t = EWM(TR, alpha=1/N)  （Wilder平滑，默认）
        或 ATR_t = SMA(TR, N)       （简单平均）

    参数:
        period: 计算周期，默认14
        mode: 'wilder'（Wilder平滑，默认）或 'sma'（简单平均）

    防未来函数: TR的prev_close用shift(1)，rolling/ewm第i行仅依赖 iloc[:i+1]
    输出列: atr
    """

    name = "atr"
    output_columns = ["atr"]

    def __init__(self, period: int = 14, mode: str = "wilder", **kwargs: Any):
        super().__init__(period=period, mode=mode, **kwargs)
        self.period = period
        self.mode = mode
        if mode not in ('wilder', 'sma'):
            raise ValueError(f"ATR mode 必须为 'wilder' 或 'sma'，当前: {mode}")
        self.alpha = 1.0 / period  # Wilder平滑系数
        self._min_periods = period

    def _get_history_size(self) -> int:
        return 2

    @lookahead_check("atr")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算ATR（numpy向量化TR计算，替代pd.concat.max，零临时对象）"""
        self._validate_data(data)

        high = data['high'].astype(float).values
        low = data['low'].astype(float).values
        close = data['close'].astype(float).values
        n = len(close)

        # prev_close：第0根为NaN，之后为前一根close（防未来函数：只用历史）
        prev_close = np.full(n, np.nan)
        prev_close[1:] = close[:-1]

        # 真实波幅 TR（numpy链式maximum，零临时DataFrame）
        tr1 = high - low
        tr2 = np.abs(high - prev_close)
        tr3 = np.abs(low - prev_close)
        tr_values = np.maximum(tr1, np.maximum(tr2, tr3))
        # 第一根K线没有prev_close，TR=high-low
        tr_values[0] = high[0] - low[0]

        tr_series = pd.Series(tr_values, index=data.index)

        if self.mode == 'wilder':
            # Wilder平滑
            atr_values = tr_series.ewm(alpha=self.alpha, adjust=False, min_periods=self.period).mean()
        else:
            # 简单平均
            atr_values = tr_series.rolling(window=self.period, min_periods=self.period).mean()

        return self._make_result_frame(data, {"atr": atr_values})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新ATR（实盘模式，O(1) 递推）
        与全量 calculate() 完全对齐：
          - wilder 模式匹配 pandas ewm(alpha=1/N, adjust=False, min_periods=N)：
            从第1根开始递推（y₀=TR₀），前 N-1 根 min_periods 屏蔽为 NaN
          - sma 模式匹配 rolling(window=N, min_periods=N)
        """
        high = float(bar['high'])
        low = float(bar['low'])
        close = float(bar['close'])

        if 'prev_close' not in self._state:
            # 首次调用：第一根 TR = high - low（无prev_close）
            self._state['prev_close'] = close
            self._state['bar_count'] = 1
            tr0 = high - low
            self._state['tr_history'] = [tr0]  # sma模式历史队列
            if self.mode == 'wilder':
                self._state['atr_ewm'] = tr0  # wilder递推初始化（pandas ewm从索引0开始）
            return {"atr": np.nan}

        prev_close = self._state['prev_close']
        # 计算TR
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )

        bar_count = self._state['bar_count'] + 1
        self._state['bar_count'] = bar_count

        if self.mode == 'wilder':
            # Wilder递推（pandas ewm adjust=False：从第1根持续递推）
            self._state['atr_ewm'] = (
                (1.0 - self.alpha) * self._state['atr_ewm'] + self.alpha * tr
            )
            # 前 period-1 根 min_periods 屏蔽为 NaN
            atr_value = self._state['atr_ewm'] if bar_count >= self.period else np.nan
        else:
            # SMA模式：维护TR历史滑动窗口
            self._state['tr_history'].append(tr)
            if len(self._state['tr_history']) > self.period:
                self._state['tr_history'].pop(0)
            if len(self._state['tr_history']) >= self.period:
                atr_value = float(np.mean(self._state['tr_history']))
            else:
                atr_value = np.nan

        self._state['prev_close'] = close
        return {"atr": float(atr_value) if not np.isnan(atr_value) else np.nan}

    def reset(self) -> None:
        """重置ATR状态"""
        super().reset()
        for key in ['prev_close', 'atr_prev', 'atr_ewm', 'bar_count', 'tr_history', 'tr_sum']:
            self._state.pop(key, None)


# ============================================================================
# Volatility - 收益率波动率（Return Volatility）
# ============================================================================

@register_indicator
class Volatility(IndicatorBase):
    """
    收益率波动率（Return Volatility / Standard Deviation）

    公式:
        日收益率 r_t = pct_chg_t / 100  （pct_chg是百分比格式，转小数）
        波动率 σ = std(r_{t-N+1}, ..., r_t)  （N日收益率标准差）
        年化波动率 = σ * sqrt(252)

    三因子股票池之波动率因子：Volatility(period=20)，取前1/3高波动股票

    参数:
        period: 计算周期，默认20（三因子标准）
        annualize: 是否年化，默认False
        use_log_return: 是否使用对数收益率，默认False（用简单收益率）

    防未来函数: rolling(window=period) 第i行仅包含 iloc[i-period+1:i+1]
    输出列: volatility
    """

    name = "volatility"
    output_columns = ["volatility"]

    def __init__(
        self,
        period: int = 20,
        annualize: bool = False,
        use_log_return: bool = False,
        **kwargs: Any,
    ):
        super().__init__(
            period=period, annualize=annualize, use_log_return=use_log_return, **kwargs
        )
        self.period = period
        self.annualize = annualize
        self.use_log_return = use_log_return
        self._min_periods = period

    def _get_history_size(self) -> int:
        return self.period + 10

    def _compute_returns(self, data: pd.DataFrame) -> pd.Series:
        """计算收益率序列"""
        if self.use_log_return:
            # 对数收益率: ln(close_t / close_{t-1})
            close = data['close'].astype(float)
            returns = np.log(close / close.shift(1))
        else:
            # 简单收益率: 使用pct_chg列（百分比格式），除以100转小数
            if 'pct_chg' in data.columns:
                returns = data['pct_chg'].astype(float) / 100.0
            else:
                close = data['close'].astype(float)
                returns = close.pct_change()
        return returns

    @lookahead_check("volatility")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算波动率"""
        self._validate_data(data)

        returns = self._compute_returns(data)

        # N日收益率标准差（ddof=1，样本标准差）
        vol_values = returns.rolling(window=self.period, min_periods=self.period).std(ddof=1)

        if self.annualize:
            # 年化: σ_annual = σ_daily * sqrt(252)
            vol_values = vol_values * np.sqrt(252)

        return self._make_result_frame(data, {"volatility": vol_values})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新波动率（实盘模式，O(1) 递推）
        维护收益率滚动总和+平方和，一次算出 std(ddof=1)
        递推公式: 新总和 = 旧总和 - 最老收益率 + 新收益率
                 std = sqrt((sum_sq/N - mean²) * N/(N-1))  (ddof=1)
        """
        close = float(bar['close'])

        if 'prev_close' not in self._state:
            self._state['prev_close'] = close
            # pct_chg模式下，第一根的涨跌幅是有效收益率（与全量_compute_returns对齐）
            if not self.use_log_return and 'pct_chg' in bar and bar['pct_chg'] is not None:
                ret0 = float(bar['pct_chg']) / 100.0
                self._history.append(ret0)
                self._state['_vol_init_sum'] = ret0
                self._state['_vol_init_sum_sq'] = ret0 * ret0
            return {"volatility": np.nan}

        prev_close = self._state['prev_close']

        if self.use_log_return:
            ret = float(np.log(close / prev_close)) if prev_close > 0 else np.nan
        else:
            if 'pct_chg' in bar and bar['pct_chg'] is not None:
                ret = float(bar['pct_chg']) / 100.0
            else:
                ret = (close - prev_close) / prev_close if prev_close != 0 else np.nan

        self._history.append(ret)
        self._state['prev_close'] = close

        if len(self._history) < self.period:
            # 数据不足，累积收益率总和与平方和（第period个收益率初始化用）
            self._state['_vol_init_sum'] = self._state.get('_vol_init_sum', 0.0) + ret
            self._state['_vol_init_sum_sq'] = self._state.get('_vol_init_sum_sq', 0.0) + ret * ret
            return {"volatility": np.nan}

        if '_vol_window_sum' not in self._state:
            # 第period个收益率：累积值含前period-1个，补上当前收益率
            self._state['_vol_window_sum'] = self._state.pop('_vol_init_sum', 0.0) + ret
            self._state['_vol_window_sum_sq'] = self._state.pop('_vol_init_sum_sq', 0.0) + ret * ret
        else:
            # O(1) 递推：最老收益率 = append 前的窗口首元素
            oldest = self._history[-self.period - 1]
            self._state['_vol_window_sum'] = self._state['_vol_window_sum'] - oldest + ret
            self._state['_vol_window_sum_sq'] = self._state['_vol_window_sum_sq'] - oldest * oldest + ret * ret

        n = self.period
        s = self._state['_vol_window_sum']
        sq = self._state['_vol_window_sum_sq']
        mean_ret = s / n
        # ddof=1 样本标准差（与全量 rolling.std(ddof=1) 一致）
        var_pop = sq / n - mean_ret * mean_ret
        var_sample = var_pop * n / (n - 1) if n > 1 else 0.0
        vol_value = np.sqrt(max(var_sample, 0.0))

        if self.annualize:
            vol_value *= np.sqrt(252)

        return {"volatility": float(vol_value)}

    def reset(self) -> None:
        """重置波动率状态"""
        super().reset()
        for key in ['prev_close', '_vol_window_sum', '_vol_window_sum_sq',
                    '_vol_init_sum', '_vol_init_sum_sq']:
            self._state.pop(key, None)


# ============================================================================
# BollingerBands - 布林带（Bollinger Bands）
# ============================================================================

@register_indicator
class BollingerBands(IndicatorBase):
    """
    布林带（Bollinger Bands）

    公式:
        中轨 MID = SMA(close, N)
        上轨 UP = MID + K * STD(close, N)
        下轨 DOWN = MID - K * STD(close, N)
        带宽 = (UP - DOWN) / MID
        %B = (close - DOWN) / (UP - DOWN)

    参数:
        period: 移动平均周期，默认20
        std_dev: 标准差倍数，默认2.0
        price_col: 使用的价格列名，默认'close'

    防未来函数: rolling(window=period) 第i行仅包含 iloc[i-period+1:i+1]
    输出列: mid, upper, lower, bandwidth, percent_b
    """

    name = "bollinger_bands"
    output_columns = ["mid", "upper", "lower", "bandwidth", "percent_b"]

    def __init__(
        self,
        period: int = 20,
        std_dev: float = 2.0,
        price_col: str = "close",
        **kwargs: Any,
    ):
        super().__init__(period=period, std_dev=std_dev, price_col=price_col, **kwargs)
        self.period = period
        self.std_dev = std_dev
        self.price_col = price_col
        self._min_periods = period

    def _get_history_size(self) -> int:
        return self.period + 10

    @lookahead_check("bollinger_bands")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算布林带"""
        self._validate_data(data)
        if self.price_col not in data.columns:
            raise ValueError(f"布林带 输入数据缺少价格列: {self.price_col}")

        prices = data[self.price_col].astype(float)

        # 中轨（SMA）
        mid = prices.rolling(window=self.period, min_periods=self.period).mean()
        # 标准差
        std = prices.rolling(window=self.period, min_periods=self.period).std(ddof=1)

        # 上下轨
        upper = mid + self.std_dev * std
        lower = mid - self.std_dev * std

        # 带宽
        bandwidth = (upper - lower) / mid.replace(0, np.nan)

        # %B
        percent_b = (prices - lower) / (upper - lower).replace(0, np.nan)

        return self._make_result_frame(data, {
            "mid": mid,
            "upper": upper,
            "lower": lower,
            "bandwidth": bandwidth,
            "percent_b": percent_b,
        })

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新布林带（实盘模式，O(1) 递推）
        维护价格滚动总和+平方和，一次算出 mean 和 std(ddof=1)
        递推公式: 新总和 = 旧总和 - 最老价格 + 新价格
                 新平方和 = 旧平方和 - 最老价格² + 新价格²
                 std = sqrt((sum_sq/N - mean²) * N/(N-1))  (ddof=1)
        """
        price = float(bar[self.price_col])
        self._history.append(price)

        if len(self._history) < self.period:
            # 数据不足，累积总和与平方和（第period根初始化用）
            self._state['_bb_init_sum'] = self._state.get('_bb_init_sum', 0.0) + price
            self._state['_bb_init_sum_sq'] = self._state.get('_bb_init_sum_sq', 0.0) + price * price
            return {
                "mid": np.nan, "upper": np.nan, "lower": np.nan,
                "bandwidth": np.nan, "percent_b": np.nan,
            }

        if '_bb_window_sum' not in self._state:
            # 第period根：累积值含前period-1根，补上当前根
            self._state['_bb_window_sum'] = self._state.pop('_bb_init_sum', 0.0) + price
            self._state['_bb_window_sum_sq'] = self._state.pop('_bb_init_sum_sq', 0.0) + price * price
        else:
            # O(1) 递推：最老价格 = append 前的窗口首元素
            oldest = self._history[-self.period - 1]
            self._state['_bb_window_sum'] = self._state['_bb_window_sum'] - oldest + price
            self._state['_bb_window_sum_sq'] = self._state['_bb_window_sum_sq'] - oldest * oldest + price * price

        n = self.period
        s = self._state['_bb_window_sum']
        sq = self._state['_bb_window_sum_sq']
        mid = s / n
        # ddof=1 样本标准差（与全量 rolling.std(ddof=1) 一致）
        var_pop = sq / n - mid * mid
        var_sample = var_pop * n / (n - 1) if n > 1 else 0.0
        std = np.sqrt(max(var_sample, 0.0))

        upper = mid + self.std_dev * std
        lower = mid - self.std_dev * std
        bandwidth = (upper - lower) / mid if mid != 0 else np.nan
        percent_b = (price - lower) / (upper - lower) if (upper - lower) != 0 else np.nan

        return {
            "mid": mid,
            "upper": upper,
            "lower": lower,
            "bandwidth": bandwidth,
            "percent_b": percent_b,
        }

    def reset(self) -> None:
        """重置布林带状态"""
        super().reset()
        for key in ['_bb_window_sum', '_bb_window_sum_sq', '_bb_init_sum', '_bb_init_sum_sq']:
            self._state.pop(key, None)
