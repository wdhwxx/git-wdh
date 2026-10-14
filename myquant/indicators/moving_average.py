# 文件名：myquant/indicators/moving_average.py
"""
鲲鹏量化交易系统 - 移动平均线指标（Moving Average Indicators）
================================================================
包含：SMA（简单移动平均）、EMA（指数移动平均）、WMA（加权移动平均）、MACD（指数平滑异同移动平均线）

防未来函数说明：
- 所有移动平均使用 pandas rolling/ewm，第i行值仅包含 iloc[:i+1] 数据
- 流式更新模式维护递推状态，实盘逐根K线更新
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Any, Dict

import numpy as np
import pandas as pd

from myquant.indicators import IndicatorBase, lookahead_check, register_indicator

# ============================================================================
# SMA - 简单移动平均（Simple Moving Average）
# ============================================================================

@register_indicator
class SMA(IndicatorBase):
    """
    简单移动平均（Simple Moving Average）

    公式: SMA_t = (P_{t-N+1} + P_{t-N+2} + ... + P_t) / N

    参数:
        period: 计算周期，默认20
        price_col: 使用的价格列名，默认'close'（收盘价）

    防未来函数: rolling(window=period) 第i行仅包含 iloc[i-period+1:i+1]
    性能优化（第5项B项）:
        - 全量计算：pandas rolling.mean 向量化（已有，不变）
        - 增量更新：维护滚动总和 O(1) 递推
          递推公式: 新总和 = 旧总和 - 最老价格 + 新价格
          （替代原 list(deque) 切片 + np.mean，O(period)→O(1)）
    """

    name = "sma"
    output_columns = ["sma"]

    def __init__(self, period: int = 20, price_col: str = "收盘价", **kwargs: Any):
        super().__init__(period=period, price_col=price_col, **kwargs)
        self.period = period
        self.price_col = price_col
        self._min_periods = period

    def _get_history_size(self) -> int:
        return self.period + 10

    @lookahead_check("sma")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算SMA"""
        self._validate_data(data)
        if self.price_col not in data.columns:
            raise ValueError(f"SMA 输入数据缺少价格列: {self.price_col}")

        prices = data[self.price_col].astype(float)
        sma_values = prices.rolling(window=self.period, min_periods=self.period).mean()

        return self._make_result_frame(data, {"sma": sma_values})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新SMA（实盘模式，O(1) 递推）
        递推公式: 新总和 = 旧总和 - 最老价格 + 新价格
        """
        price = float(bar[self.price_col])
        self._history.append(price)

        if len(self._history) < self.period:
            # 数据不足，累积总和（第period根初始化用）
            self._state['_sma_init_sum'] = self._state.get('_sma_init_sum', 0.0) + price
            return {"sma": np.nan}

        if '_sma_window_sum' not in self._state:
            # 第period根：累积值含前period-1根，补上当前根
            self._state['_sma_window_sum'] = self._state.pop('_sma_init_sum', 0.0) + price
            return {"sma": float(self._state['_sma_window_sum'] / self.period)}

        # O(1) 递推：最老价格 = append 前的窗口首元素
        oldest = self._history[-self.period - 1]
        new_sum = self._state['_sma_window_sum'] - oldest + price
        self._state['_sma_window_sum'] = new_sum
        return {"sma": float(new_sum / self.period)}

    def reset(self) -> None:
        """重置SMA状态"""
        super().reset()
        for key in ['_sma_window_sum', '_sma_init_sum']:
            self._state.pop(key, None)


# ============================================================================
# EMA - 指数移动平均（Exponential Moving Average）
# ============================================================================

@register_indicator
class EMA(IndicatorBase):
    """
    指数移动平均（Exponential Moving Average）

    公式: EMA_t = α * P_t + (1-α) * EMA_{t-1}
          α = 2 / (N + 1) （平滑系数 Smoothing Factor）

    参数:
        period: 计算周期，默认12
        price_col: 使用的价格列名，默认'close'

    防未来函数: ewm(span=period) 递推计算，第i行仅依赖 iloc[:i+1]
    """

    name = "ema"
    output_columns = ["ema"]

    def __init__(self, period: int = 12, price_col: str = "收盘价", **kwargs: Any):
        super().__init__(period=period, price_col=price_col, **kwargs)
        self.period = period
        self.price_col = price_col
        self.alpha = 2.0 / (period + 1.0)  # 平滑系数
        self._min_periods = 1  # EMA从第一根就有值（用首值初始化）

    def _get_history_size(self) -> int:
        return 2  # EMA只需要前一个值递推，历史缓冲区不需要太大

    @lookahead_check("ema")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算EMA"""
        self._validate_data(data)
        if self.price_col not in data.columns:
            raise ValueError(f"EMA 输入数据缺少价格列: {self.price_col}")

        prices = data[self.price_col].astype(float)
        # adjust=False 使用递推公式，与实盘流式更新完全一致
        ema_values = prices.ewm(span=self.period, adjust=False).mean()

        return self._make_result_frame(data, {"ema": ema_values})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新EMA（实盘模式）
        递推计算: EMA_t = α * P_t + (1-α) * EMA_{t-1}
        """
        price = float(bar[self.price_col])

        if 'ema_prev' not in self._state:
            # 首次调用，用当前价格初始化
            self._state['ema_prev'] = price
        else:
            ema_prev = self._state['ema_prev']
            ema_value = self.alpha * price + (1.0 - self.alpha) * ema_prev
            self._state['ema_prev'] = ema_value

        return {"ema": float(self._state['ema_prev'])}

    def reset(self) -> None:
        """重置EMA状态"""
        super().reset()
        self._state.pop('ema_prev', None)


# ============================================================================
# WMA - 加权移动平均（Weighted Moving Average）
# ============================================================================

@register_indicator
class WMA(IndicatorBase):
    """
    加权移动平均（Weighted Moving Average）

    公式: WMA_t = (1*P_{t-N+1} + 2*P_{t-N+2} + ... + N*P_t) / (N*(N+1)/2)
          近期价格权重更高

    参数:
        period: 计算周期，默认20
        price_col: 使用的价格列名，默认'close'

    防未来函数: sliding_window_view 第i行仅包含 iloc[i-period+1:i+1]
    性能优化（第5项A项）:
        - 全量计算：numpy sliding_window_view 零拷贝窗口 + tensordot 矩阵乘向量化
          （替代原 Python for 循环逐窗口 np.dot，10000根预期提速10-50倍）
        - 增量更新：维护加权和+窗口和 O(1) 递推
          递推公式: 新加权和 = 旧加权和 - 旧窗口和 + N * 新价格
                   新窗口和 = 旧窗口和 - 最老价格 + 新价格
          （替代原每次 list(deque) 切片 + np.dot，O(period)→O(1)）
    """

    name = "wma"
    output_columns = ["wma"]

    def __init__(self, period: int = 20, price_col: str = "收盘价", **kwargs: Any):
        super().__init__(period=period, price_col=price_col, **kwargs)
        self.period = period
        self.price_col = price_col
        # 权重数组: [1, 2, 3, ..., N]
        self.weights = np.arange(1, period + 1, dtype=np.float64)
        self.weights_sum = self.weights.sum()
        self._min_periods = period

    def _get_history_size(self) -> int:
        return self.period + 10

    @lookahead_check("wma")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算WMA（numpy向量化：滑动窗口视图 + 矩阵乘，替代Python for循环）"""
        self._validate_data(data)
        if self.price_col not in data.columns:
            raise ValueError(f"WMA 输入数据缺少价格列: {self.price_col}")

        prices = data[self.price_col].astype(float).values
        n = len(prices)
        wma_values = np.full(n, np.nan)

        if n >= self.period:
            # sliding_window_view 创建形状 (n-period+1, period) 的零拷贝窗口视图
            windows = np.lib.stride_tricks.sliding_window_view(prices, self.period)
            # 向量化加权平均：tensordot 一次完成所有窗口的加权和
            wma_valid = np.tensordot(windows, self.weights, axes=1) / self.weights_sum
            # 窗口内含 NaN 时 tensordot 自然传播为 NaN，与原实现行为一致
            wma_values[self.period - 1:] = wma_valid

        return self._make_result_frame(data, {"wma": pd.Series(wma_values, index=data.index)})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新WMA（实盘模式，O(1) 递推）
        递推公式: 新加权和 = 旧加权和 - 旧窗口和 + N * 新价格
                 新窗口和 = 旧窗口和 - 最老价格 + 新价格
        """
        price = float(bar[self.price_col])
        self._history.append(price)

        if len(self._history) < self.period:
            # 数据不足，累积窗口和与加权和（第period根时初始化用）
            self._state['_wma_init_sum'] = self._state.get('_wma_init_sum', 0.0) + price
            self._state['_wma_init_wsum'] = (
                self._state.get('_wma_init_wsum', 0.0) + len(self._history) * price
            )
            return {"wma": np.nan}

        if '_wma_weighted_sum' not in self._state:
            # 第period根：累积值含前period-1根，补上当前根（权重period）
            init_sum = self._state.pop('_wma_init_sum', 0.0) + price
            init_wsum = self._state.pop('_wma_init_wsum', 0.0) + self.period * price
            self._state['_wma_window_sum'] = init_sum
            self._state['_wma_weighted_sum'] = init_wsum
            return {"wma": float(init_wsum / self.weights_sum)}

        # O(1) 递推：旧窗口最老价格 = append 前的窗口首元素
        oldest = self._history[-self.period - 1]
        old_window_sum = self._state['_wma_window_sum']
        old_weighted_sum = self._state['_wma_weighted_sum']
        new_window_sum = old_window_sum - oldest + price
        new_weighted_sum = old_weighted_sum - old_window_sum + self.period * price
        self._state['_wma_window_sum'] = new_window_sum
        self._state['_wma_weighted_sum'] = new_weighted_sum
        return {"wma": float(new_weighted_sum / self.weights_sum)}

    def reset(self) -> None:
        """重置WMA状态"""
        super().reset()
        for key in ['_wma_window_sum', '_wma_weighted_sum', '_wma_init_sum', '_wma_init_wsum']:
            self._state.pop(key, None)


# ============================================================================
# MACD - 指数平滑异同移动平均线（Moving Average Convergence Divergence）
# ============================================================================

@register_indicator
class MACD(IndicatorBase):
    """
    指数平滑异同移动平均线（Moving Average Convergence Divergence）

    公式:
        DIF = EMA(fast) - EMA(slow)          （差离值）
        DEA = EMA(DIF, signal)                （讯号线/信号線）
        MACD柱 = 2 * (DIF - DEA)              （柱状图，Histogram）

    参数:
        fast: 快速EMA周期，默认12
        slow: 慢速EMA周期，默认26
        signal: 信号线EMA周期，默认9
        price_col: 使用的价格列名，默认'close'

    防未来函数: 全部基于EMA递推，第i行仅依赖 iloc[:i+1]
    输出列: dif, dea, macd_hist
    """

    name = "macd"
    output_columns = ["dif", "dea", "macd_hist"]

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        price_col: str = "收盘价",
        **kwargs: Any,
    ):
        super().__init__(fast=fast, slow=slow, signal=signal, price_col=price_col, **kwargs)
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.price_col = price_col
        self.alpha_fast = 2.0 / (fast + 1.0)
        self.alpha_slow = 2.0 / (slow + 1.0)
        self.alpha_signal = 2.0 / (signal + 1.0)
        self._min_periods = 1  # EMA从首值开始

    def _get_history_size(self) -> int:
        return 2

    @lookahead_check("macd")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算MACD"""
        self._validate_data(data)
        if self.price_col not in data.columns:
            raise ValueError(f"MACD 输入数据缺少价格列: {self.price_col}")

        prices = data[self.price_col].astype(float)

        # 计算快慢EMA（adjust=False 与流式递推一致）
        ema_fast = prices.ewm(span=self.fast, adjust=False).mean()
        ema_slow = prices.ewm(span=self.slow, adjust=False).mean()

        # DIF = 快EMA - 慢EMA
        dif = ema_fast - ema_slow

        # DEA = DIF的EMA
        dea = dif.ewm(span=self.signal, adjust=False).mean()

        # MACD柱 = 2 * (DIF - DEA)
        macd_hist = 2.0 * (dif - dea)

        return self._make_result_frame(data, {
            "dif": dif,
            "dea": dea,
            "macd_hist": macd_hist,
        })

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新MACD（实盘模式）
        维护三个EMA递推状态
        """
        price = float(bar[self.price_col])

        # 更新快速EMA
        if 'ema_fast_prev' not in self._state:
            self._state['ema_fast_prev'] = price
            self._state['ema_slow_prev'] = price
            self._state['dea_prev'] = 0.0  # DIF初始为0，DEA初始为0
        else:
            self._state['ema_fast_prev'] = (
                self.alpha_fast * price + (1.0 - self.alpha_fast) * self._state['ema_fast_prev']
            )
            self._state['ema_slow_prev'] = (
                self.alpha_slow * price + (1.0 - self.alpha_slow) * self._state['ema_slow_prev']
            )

        # 计算DIF
        dif = self._state['ema_fast_prev'] - self._state['ema_slow_prev']

        # 更新DEA（DIF的EMA）
        self._state['dea_prev'] = (
            self.alpha_signal * dif + (1.0 - self.alpha_signal) * self._state['dea_prev']
        )
        dea = self._state['dea_prev']

        # MACD柱
        macd_hist = 2.0 * (dif - dea)

        return {
            "dif": float(dif),
            "dea": float(dea),
            "macd_hist": float(macd_hist),
        }

    def reset(self) -> None:
        """重置MACD状态"""
        super().reset()
        for key in ['ema_fast_prev', 'ema_slow_prev', 'dea_prev']:
            self._state.pop(key, None)
