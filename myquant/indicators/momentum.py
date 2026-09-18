# 文件名：myquant/indicators/momentum.py
"""
鲲鹏量化交易系统 - 动量指标（Momentum Indicators）
=====================================================
包含：ROC（变动率）、MOM（动量）、RSI（相对强弱指标）、KDJ（随机指标）

三因子股票池之动量因子：MOM(period=60)，取前1/3高动量股票

防未来函数说明：
- 所有动量指标基于历史价格差分/比率，第i行仅使用 iloc[:i+1] 数据
- RSI使用Wilder平滑（EWM），递推计算无未来函数
- KDJ的RSV使用区间内最高最低，rolling窗口严格左闭右闭
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Any, Dict

import numpy as np
import pandas as pd

from myquant.indicators import IndicatorBase, lookahead_check, register_indicator


# ============================================================================
# ROC - 变动率指标（Rate of Change）
# ============================================================================

@register_indicator
class ROC(IndicatorBase):
    """
    变动率指标（Rate of Change）

    公式: ROC_t = (P_t - P_{t-N}) / P_{t-N} * 100

    参数:
        period: 计算周期，默认10
        price_col: 使用的价格列名，默认'close'

    防未来函数: shift(period) 取N日前价格，第i行仅依赖 iloc[:i+1]
    输出列: roc（百分比格式，如5.0表示5%）
    """

    name = "roc"
    output_columns = ["roc"]

    def __init__(self, period: int = 10, price_col: str = "close", **kwargs: Any):
        super().__init__(period=period, price_col=price_col, **kwargs)
        self.period = period
        self.price_col = price_col
        self._min_periods = period + 1

    def _get_history_size(self) -> int:
        return self.period + 10

    @lookahead_check("roc")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算ROC"""
        self._validate_data(data)
        if self.price_col not in data.columns:
            raise ValueError(f"ROC 输入数据缺少价格列: {self.price_col}")

        prices = data[self.price_col].astype(float)
        # ROC = (当日收盘价 - N日前收盘价) / N日前收盘价 * 100
        roc_values = (prices - prices.shift(self.period)) / prices.shift(self.period) * 100.0

        return self._make_result_frame(data, {"roc": roc_values})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """流式更新ROC（实盘模式）"""
        price = float(bar[self.price_col])
        self._history.append(price)

        if len(self._history) <= self.period:
            return {"roc": np.nan}

        price_n_ago = self._history[-self.period - 1]
        if price_n_ago == 0:
            return {"roc": np.nan}

        roc_value = (price - price_n_ago) / price_n_ago * 100.0
        return {"roc": float(roc_value)}


# ============================================================================
# MOM - 动量指标（Momentum）
# ============================================================================

@register_indicator
class MOM(IndicatorBase):
    """
    动量指标（Momentum）

    公式: MOM_t = P_t - P_{t-N} （绝对动量）
          或 MOM_t = P_t / P_{t-N} （相对动量，默认）

    三因子股票池之动量因子：MOM(period=60)，取前1/3高动量股票

    参数:
        period: 计算周期，默认60（三因子标准）
        price_col: 使用的价格列名，默认'close'
        mode: 'relative'（相对动量，比率）或 'absolute'（绝对动量，差值）

    防未来函数: shift(period) 取N日前价格，第i行仅依赖 iloc[:i+1]
    输出列: mom
    """

    name = "mom"
    output_columns = ["mom"]

    def __init__(
        self,
        period: int = 60,
        price_col: str = "close",
        mode: str = "relative",
        **kwargs: Any,
    ):
        super().__init__(period=period, price_col=price_col, mode=mode, **kwargs)
        self.period = period
        self.price_col = price_col
        self.mode = mode
        if mode not in ('relative', 'absolute'):
            raise ValueError(f"MOM mode 必须为 'relative' 或 'absolute'，当前: {mode}")
        self._min_periods = period + 1

    def _get_history_size(self) -> int:
        return self.period + 10

    @lookahead_check("mom")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算MOM"""
        self._validate_data(data)
        if self.price_col not in data.columns:
            raise ValueError(f"MOM 输入数据缺少价格列: {self.price_col}")

        prices = data[self.price_col].astype(float)
        prices_n_ago = prices.shift(self.period)

        if self.mode == 'relative':
            # 相对动量: 今日收盘价 / N日前收盘价
            mom_values = prices / prices_n_ago
        else:
            # 绝对动量: 今日收盘价 - N日前收盘价
            mom_values = prices - prices_n_ago

        return self._make_result_frame(data, {"mom": mom_values})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """流式更新MOM（实盘模式）"""
        price = float(bar[self.price_col])
        self._history.append(price)

        if len(self._history) <= self.period:
            return {"mom": np.nan}

        price_n_ago = self._history[-self.period - 1]

        if self.mode == 'relative':
            if price_n_ago == 0:
                return {"mom": np.nan}
            mom_value = price / price_n_ago
        else:
            mom_value = price - price_n_ago

        return {"mom": float(mom_value)}


# ============================================================================
# RSI - 相对强弱指标（Relative Strength Index）
# ============================================================================

@register_indicator
class RSI(IndicatorBase):
    """
    相对强弱指标（Relative Strength Index）

    公式（Wilder平滑）:
        涨跌幅 = close_t - close_{t-1}
        涨幅 = max(涨跌幅, 0)，跌幅 = max(-涨跌幅, 0)
        平均涨幅 = EWM(涨幅, alpha=1/N) （Wilder平滑）
        平均跌幅 = EWM(跌幅, alpha=1/N)
        RS = 平均涨幅 / 平均跌幅
        RSI = 100 - 100 / (1 + RS)

    参数:
        period: 计算周期，默认14
        price_col: 使用的价格列名，默认'close'

    防未来函数: EWM递推，第i行仅依赖 iloc[:i+1]
    输出列: rsi（0-100）
    """

    name = "rsi"
    output_columns = ["rsi"]

    def __init__(self, period: int = 14, price_col: str = "close", **kwargs: Any):
        super().__init__(period=period, price_col=price_col, **kwargs)
        self.period = period
        self.price_col = price_col
        self.alpha = 1.0 / period  # Wilder平滑系数
        self._min_periods = period + 1

    def _get_history_size(self) -> int:
        return 2

    @lookahead_check("rsi")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算RSI（Wilder平滑）"""
        self._validate_data(data)
        if self.price_col not in data.columns:
            raise ValueError(f"RSI 输入数据缺少价格列: {self.price_col}")

        prices = data[self.price_col].astype(float)
        delta = prices.diff()

        # 涨幅和跌幅
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)

        # Wilder平滑（等价于 ewm(alpha=1/period, adjust=False)）
        avg_gain = gain.ewm(alpha=self.alpha, adjust=False, min_periods=self.period).mean()
        avg_loss = loss.ewm(alpha=self.alpha, adjust=False, min_periods=self.period).mean()

        # 相对强度 RS
        rs = avg_gain / avg_loss.replace(0, np.nan)

        # RSI
        rsi_values = 100.0 - (100.0 / (1.0 + rs))
        # 当平均跌幅为0时（全涨），RSI=100
        rsi_values = rsi_values.where(avg_loss != 0, 100.0)
        # 当平均涨幅为0时（全跌），RSI=0
        rsi_values = rsi_values.where(avg_gain != 0, 0.0)

        return self._make_result_frame(data, {"rsi": rsi_values})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新RSI（实盘模式）
        Wilder递推: avg_gain_t = (1-α)*avg_gain_{t-1} + α*gain_t
        """
        price = float(bar[self.price_col])

        if 'prev_close' not in self._state:
            self._state['prev_close'] = price
            self._state['avg_gain'] = np.nan
            self._state['avg_loss'] = np.nan
            self._state['bar_count'] = 1
            return {"rsi": np.nan}

        prev_close = self._state['prev_close']
        delta = price - prev_close
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        bar_count = self._state['bar_count'] + 1
        self._state['bar_count'] = bar_count

        if bar_count <= self.period:
            # 前period根用简单平均初始化
            if 'gain_sum' not in self._state:
                self._state['gain_sum'] = 0.0
                self._state['loss_sum'] = 0.0
            self._state['gain_sum'] += gain
            self._state['loss_sum'] += loss

            if bar_count == self.period:
                # 第period根时，用简单平均作为初始avg_gain/avg_loss
                self._state['avg_gain'] = self._state['gain_sum'] / self.period
                self._state['avg_loss'] = self._state['loss_sum'] / self.period
        else:
            # Wilder递推
            self._state['avg_gain'] = (
                (1.0 - self.alpha) * self._state['avg_gain'] + self.alpha * gain
            )
            self._state['avg_loss'] = (
                (1.0 - self.alpha) * self._state['avg_loss'] + self.alpha * loss
            )

        self._state['prev_close'] = price

        avg_gain = self._state['avg_gain']
        avg_loss = self._state['avg_loss']

        if np.isnan(avg_gain) or np.isnan(avg_loss):
            return {"rsi": np.nan}
        if avg_loss == 0:
            return {"rsi": 100.0}
        if avg_gain == 0:
            return {"rsi": 0.0}

        rs = avg_gain / avg_loss
        rsi_value = 100.0 - (100.0 / (1.0 + rs))
        return {"rsi": float(rsi_value)}

    def reset(self) -> None:
        """重置RSI状态"""
        super().reset()
        for key in ['prev_close', 'avg_gain', 'avg_loss', 'bar_count', 'gain_sum', 'loss_sum']:
            self._state.pop(key, None)


# ============================================================================
# KDJ - 随机指标（Stochastic Oscillator）
# ============================================================================

@register_indicator
class KDJ(IndicatorBase):
    """
    随机指标（Stochastic Oscillator / KDJ）

    公式:
        RSV_t = (C_t - LLV(L, N)) / (HHV(H, N) - LLV(L, N)) * 100
                （未成熟随机值，Raw Stochastic Value）
        K_t = 2/3 * K_{t-1} + 1/3 * RSV_t  （K线，平滑系数1/3）
        D_t = 2/3 * D_{t-1} + 1/3 * K_t     （D线，平滑系数1/3）
        J_t = 3 * K_t - 2 * D_t              （J线）

    参数:
        n: RSV周期，默认9
        m1: K线平滑周期，默认3（平滑系数1/m1）
        m2: D线平滑周期，默认3（平滑系数1/m2）

    防未来函数:
        - RSV的HHV/LLV使用 rolling(window=n)，第i行仅包含 iloc[i-n+1:i+1]
        - K/D递推计算，无未来函数
    输出列: k, d, j
    """

    name = "kdj"
    output_columns = ["k", "d", "j"]

    def __init__(self, n: int = 9, m1: int = 3, m2: int = 3, **kwargs: Any):
        super().__init__(n=n, m1=m1, m2=m2, **kwargs)
        self.n = n
        self.m1 = m1
        self.m2 = m2
        self.alpha_k = 1.0 / m1  # K线平滑系数
        self.alpha_d = 1.0 / m2  # D线平滑系数
        self._min_periods = n

    def _get_history_size(self) -> int:
        return self.n + 10

    @lookahead_check("kdj")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算KDJ"""
        self._validate_data(data)

        high = data['high'].astype(float)
        low = data['low'].astype(float)
        close = data['close'].astype(float)

        # 区间最高/最低（rolling窗口包含当前行）
        low_n = low.rolling(window=self.n, min_periods=self.n).min()
        high_n = high.rolling(window=self.n, min_periods=self.n).max()

        # RSV
        rsv = (close - low_n) / (high_n - low_n) * 100.0
        # 当 high_n == low_n 时（一字板），RSV设为50
        rsv = rsv.where(high_n != low_n, 50.0)

        # K线: EWM(RSV, alpha=1/m1)，初始值50
        k_values = rsv.ewm(alpha=self.alpha_k, adjust=False).mean()
        # D线: EWM(K, alpha=1/m2)，初始值50
        d_values = k_values.ewm(alpha=self.alpha_d, adjust=False).mean()
        # J线
        j_values = 3.0 * k_values - 2.0 * d_values

        # 前n-1行设为NaN（数据不足）
        k_values.iloc[:self.n - 1] = np.nan
        d_values.iloc[:self.n - 1] = np.nan
        j_values.iloc[:self.n - 1] = np.nan

        return self._make_result_frame(data, {
            "k": k_values,
            "d": d_values,
            "j": j_values,
        })

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新KDJ（实盘模式，Python list环形缓冲区）
        维护n周期high/low环形缓冲区，内置max/min求窗口极值
        （替代原 list(deque)切片+dict生成器，消除对象创建与切片开销）
        K/D初始值匹配全量 ewm(adjust=False)：第一个有效值=RSV本身
        """
        high = float(bar['high'])
        low = float(bar['low'])
        close = float(bar['close'])

        # 首次调用：初始化环形缓冲区（Python list，小窗口下比numpy快）
        if '_kdj_high_buf' not in self._state:
            self._state['_kdj_high_buf'] = [0.0] * self.n
            self._state['_kdj_low_buf'] = [0.0] * self.n
            self._state['_kdj_idx'] = 0
            self._state['_kdj_count'] = 0

        buf_h = self._state['_kdj_high_buf']
        buf_l = self._state['_kdj_low_buf']
        idx = self._state['_kdj_idx']
        count = self._state['_kdj_count']

        # 写入环形缓冲区
        buf_h[idx] = high
        buf_l[idx] = low
        self._state['_kdj_idx'] = (idx + 1) % self.n
        self._state['_kdj_count'] = count + 1

        if count + 1 < self.n:
            return {"k": np.nan, "d": np.nan, "j": np.nan}

        # 缓冲区已填满（无NaN），内置max/min直接求极值
        high_n = max(buf_h)
        low_n = min(buf_l)

        # RSV
        if high_n == low_n:
            rsv = 50.0
        else:
            rsv = (close - low_n) / (high_n - low_n) * 100.0

        # K/D递推（第一个有效值=RSV，匹配全量ewm(adjust=False)初始值）
        if 'k_prev' not in self._state:
            k_value = rsv
            d_value = rsv
        else:
            k_value = (1.0 - self.alpha_k) * self._state['k_prev'] + self.alpha_k * rsv
            d_value = (1.0 - self.alpha_d) * self._state['d_prev'] + self.alpha_d * k_value
        j_value = 3.0 * k_value - 2.0 * d_value

        self._state['k_prev'] = k_value
        self._state['d_prev'] = d_value

        return {
            "k": float(k_value),
            "d": float(d_value),
            "j": float(j_value),
        }

    def reset(self) -> None:
        """重置KDJ状态"""
        super().reset()
        for key in ['k_prev', 'd_prev', '_kdj_high_buf', '_kdj_low_buf',
                    '_kdj_idx', '_kdj_count']:
            self._state.pop(key, None)
