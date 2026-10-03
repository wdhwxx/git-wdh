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
# F-10 波动率稳健化常量（RiskMetrics 日频 EWMA 标准）
# ============================================================================

# 常态指数平滑 λ（日频标准，RiskMetrics 1996）
EWMA_LAMBDA_NORMAL: float = 0.94
# 极端行情自适应 λ（更快响应：极端收益日降 λ 加速方差跟随）
EWMA_LAMBDA_HIGH: float = 0.85
# 极端日判定阈值：|r_t| > 3σ（EWMA σ）视为极端收益，触发 λ 降档
EWMA_SIGMA_K: float = 3.0
# MAD→σ 换算常数（正态假设，与 factor_preprocessor 同口径）
MAD_CONSTANT: float = 1.4826
# 输出前去极值（expanding MAD 截断）的最小历史样本数
WINSOR_MIN_PERIODS: int = 5


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

        high = data['最高价'].astype(float).values
        low = data['最低价'].astype(float).values
        close = data['收盘价'].astype(float).values
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
        high = float(bar['最高价'])
        low = float(bar['最低价'])
        close = float(bar['收盘价'])

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
        method: F-10 波动率稳健化：'std'（固定滚动 std，默认零变化）、
            'ewma'（RiskMetrics 方差递推 σ²_t = λ·σ²_{t-1} + (1−λ)·r²_{t-1}，
            λ 两档自适应：常态 0.94、|r|>3σ 极端日 0.85，极端行情响应更快）、
            'robust'（滚动窗口 MAD 稳健估计 σ = 1.4826·median(|r−median(r)|)）
        winsorize: F-10 输出前去极值：expanding MAD 截断（边界只含截至 t 的
            历史 volatility 值，防未来铁律11），默认False零变化
        winsorize_k: 去极值截断倍数（默认 3.0，等价 3σ 尺度）

    防未来函数: rolling(window=period) 第i行仅包含 iloc[i-period+1:i+1]；
    ewma 方差递推只消费 <=t 的收益（本行使用的数据截至 t 时刻）；去极值边界
    由 expanding 历史计算，不含任何未来值。
    输出列: volatility
    """

    name = "volatility"
    output_columns = ["volatility"]

    def __init__(
        self,
        period: int = 20,
        annualize: bool = False,
        use_log_return: bool = False,
        method: str = "std",
        winsorize: bool = False,
        winsorize_k: float = 3.0,
        **kwargs: Any,
    ):
        super().__init__(
            period=period, annualize=annualize, use_log_return=use_log_return,
            method=method, winsorize=winsorize, winsorize_k=winsorize_k, **kwargs
        )
        self.period = period
        self.annualize = annualize
        self.use_log_return = use_log_return
        self.method = method
        self.winsorize = winsorize
        self.winsorize_k = winsorize_k
        if method not in ("std", "ewma", "robust"):
            raise ValueError(
                f"Volatility method 必须为 'std'/'ewma'/'robust'，当前: {method}")
        if winsorize_k <= 0:
            raise ValueError(f"winsorize_k 必须为正，当前: {winsorize_k}")
        # 防未来校验最小期数：ewma 是全局递推（第 2 个有效收益即出值），
        # std/robust 需完整滚动窗口（period 根）
        self._min_periods = 2 if method == "ewma" else period
        # 流式输出历史（winsorize 去极值用，仅存已收盘 volatility 值）
        self._vol_history: list[float] = []

    def _get_history_size(self) -> int:
        return self.period + 10

    def _compute_returns(self, data: pd.DataFrame) -> pd.Series:
        """计算收益率序列"""
        if self.use_log_return:
            # 对数收益率: ln(close_t / close_{t-1})
            close = data['收盘价'].astype(float)
            returns = np.log(close / close.shift(1))
        else:
            # 简单收益率: 使用pct_chg列（百分比格式），除以100转小数
            if 'pct_chg' in data.columns:
                returns = data['pct_chg'].astype(float) / 100.0
            else:
                close = data['收盘价'].astype(float)
                returns = close.pct_change()
        return returns

    # ------------------------------------------------------------------
    # F-10 稳健化计算路径
    # ------------------------------------------------------------------

    def _ewma_vol(self, returns: pd.Series) -> pd.Series:
        """EWMA 波动率（RiskMetrics 方差递推，λ 两档自适应）。

        递推：σ²_t = λ·σ²_{t-1} + (1−λ)·r²_{t-1}（截至 t 的方差由昨日收益更新）；
        λ 自适应：|r_{t-1}| > 3·σ_{t-1} 视为极端日 → λ 降为 0.85（更快响应），
        否则 λ=0.94（日频标准）。首根收益作为递推种子但输出 NaN（与最小期数
        语义对齐）。

        本行使用的数据截至 t 时刻：只消费 <=t 的收益序列（铁律11）。
        """
        r = np.asarray(returns.to_numpy(dtype=float), dtype=float)
        n = len(r)
        var = np.full(n, np.nan)
        first_valid = next(
            (i for i in range(n) if np.isfinite(r[i])), None)
        if first_valid is None:
            return pd.Series(var, index=returns.index)
        var[first_valid] = r[first_valid] * r[first_valid]
        for i in range(first_valid + 1, n):
            if not np.isfinite(r[i - 1]):
                # 昨日收益缺失：沿用前值（不传播 NaN）
                var[i] = var[i - 1]
                continue
            lam = EWMA_LAMBDA_NORMAL
            prev_sigma = np.sqrt(max(var[i - 1], 0.0))
            if np.isfinite(prev_sigma) and abs(r[i - 1]) > EWMA_SIGMA_K * prev_sigma:
                lam = EWMA_LAMBDA_HIGH
            var[i] = lam * var[i - 1] + (1.0 - lam) * r[i - 1] * r[i - 1]
        out = np.sqrt(np.maximum(var, 0.0))
        out[first_valid] = np.nan  # 递推种子不输出（与最小期数语义对齐）
        return pd.Series(out, index=returns.index)

    def _robust_vol(self, returns: pd.Series) -> pd.Series:
        """滚动窗口 MAD 稳健波动率（F-10）：σ = 1.4826·median(|r−median(r)|)。

        窗口内 MAD 对极端收益不敏感，股灾/熔断区间估计更稳健；
        rolling(min_periods=period) 第 i 行仅含 iloc[i-period+1:i+1]（铁律11）。
        """
        def _mad(w: np.ndarray) -> float:
            med = float(np.median(w))
            if med != med:  # NaN 防御
                return np.nan
            mad = float(np.median(np.abs(w - med)))
            return float(MAD_CONSTANT * mad)

        return returns.rolling(
            window=self.period, min_periods=self.period).apply(_mad, raw=True)

    def _winsorize_expanding(self, series: pd.Series) -> pd.Series:
        """输出前去极值（F-10）：expanding MAD 截断。

        边界 = 截至 t 的历史 volatility 值的中位数 ± k·1.4826·MAD——
        只含 <=t 数据（铁律11，绝不引用未来）；样本 < WINSOR_MIN_PERIODS
        不截断（防御式）；MAD 为 0（多数值相同+单个尖峰）时降级用
        mean ± k·std 边界兜底，保证极端值仍被截断。
        """
        out = series.copy()
        acc: list[float] = []
        for i in range(len(series)):
            v = series.iloc[i]
            if not np.isfinite(v):
                out.iloc[i] = np.nan
                continue
            acc.append(float(v))
            if len(acc) < WINSOR_MIN_PERIODS:
                continue  # 样本不足不截断
            arr = np.asarray(acc, dtype=float)
            med = float(np.median(arr))
            mad = float(np.median(np.abs(arr - med)))
            if mad <= 0.0:
                # MAD 退化（重复值多）→ std 兜底：mean ± k·std
                mean_v = float(np.mean(arr))
                sd = float(np.std(arr))
                if sd <= 0.0:
                    continue
                lo = mean_v - self.winsorize_k * sd
                hi = mean_v + self.winsorize_k * sd
            else:
                lo = med - self.winsorize_k * MAD_CONSTANT * mad
                hi = med + self.winsorize_k * MAD_CONSTANT * mad
            out.iloc[i] = min(max(v, lo), hi)
        return out

    @lookahead_check("volatility")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算波动率"""
        self._validate_data(data)

        returns = self._compute_returns(data)

        if self.method == "std":
            # N日收益率标准差（ddof=1，样本标准差）——原实现零变化
            vol_values = returns.rolling(
                window=self.period, min_periods=self.period).std(ddof=1)
        elif self.method == "ewma":
            vol_values = self._ewma_vol(returns)
        elif self.method == "robust":
            vol_values = self._robust_vol(returns)
        else:  # pragma: no cover（__init__ 已防御）
            raise ValueError(f"未知 method: {self.method}")

        if self.annualize:
            # 年化: σ_annual = σ_daily * sqrt(252)
            vol_values = vol_values * np.sqrt(252)

        if self.winsorize:
            # 输出前去极值（expanding MAD 截断，边界仅含截至 t 历史）
            vol_values = self._winsorize_expanding(vol_values)

        return self._make_result_frame(data, {"volatility": vol_values})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新波动率（实盘模式，O(1) 递推）
        - std（原实现）：维护收益率滚动总和+平方和，一次算出 std(ddof=1)
        - ewma（F-10）：RiskMetrics 方差递推 σ²_t = λ·σ²_{t-1} + (1−λ)·r²_{t-1}
          （λ 两档自适应，与批量 _ewma_vol 同口径）
        - robust（F-10）：滚动窗口 MAD 稳健估计
        - winsorize（F-10）：输出前 expanding MAD 截断（仅用历史 volatility 值）

        递推公式: 新总和 = 旧总和 - 最老收益率 + 新收益率
                 std = sqrt((sum_sq/N - mean²) * N/(N-1))  (ddof=1)
        """
        close = float(bar['收盘价'])

        if 'prev_close' not in self._state:
            self._state['prev_close'] = close
            # pct_chg模式下，第一根的涨跌幅是有效收益率（与全量_compute_returns对齐）
            if not self.use_log_return and 'pct_chg' in bar and bar['pct_chg'] is not None:
                ret0 = float(bar['pct_chg']) / 100.0
                self._history.append(ret0)
                self._state['_vol_init_sum'] = ret0
                self._state['_vol_init_sum_sq'] = ret0 * ret0
                if self.method == "ewma":
                    self._state['_ewma_var'] = ret0 * ret0
                    self._state['_ewma_prev_ret'] = ret0
            return {"volatility": self._emit(np.nan)}

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

        if self.method == "ewma":
            # RiskMetrics 方差递推（本行使用的数据截至当前已收盘 bar，铁律11）
            if '_ewma_var' not in self._state:
                self._state['_ewma_var'] = ret * ret
                self._state['_ewma_prev_ret'] = ret
                return {"volatility": self._emit(np.nan)}
            prev_var = self._state['_ewma_var']
            prev_ret = self._state['_ewma_prev_ret']
            lam = EWMA_LAMBDA_NORMAL
            prev_sigma = np.sqrt(max(prev_var, 0.0))
            if abs(prev_ret) > EWMA_SIGMA_K * prev_sigma:
                lam = EWMA_LAMBDA_HIGH  # 极端日降 λ，更快响应
            var = lam * prev_var + (1.0 - lam) * prev_ret * prev_ret
            self._state['_ewma_var'] = var
            self._state['_ewma_prev_ret'] = ret
            vol_value = float(np.sqrt(max(var, 0.0)))
        elif self.method == "robust":
            if len(self._history) < self.period:
                return {"volatility": self._emit(np.nan)}
            w = np.asarray(list(self._history)[-self.period:], dtype=float)
            med = float(np.median(w))
            vol_value = float(MAD_CONSTANT * np.median(np.abs(w - med)))
        else:
            # std 原实现（零变化）
            if len(self._history) < self.period:
                # 数据不足，累积收益率总和与平方和（第period个收益率初始化用）
                self._state['_vol_init_sum'] = self._state.get('_vol_init_sum', 0.0) + ret
                self._state['_vol_init_sum_sq'] = self._state.get('_vol_init_sum_sq', 0.0) + ret * ret
                return {"volatility": self._emit(np.nan)}

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
            vol_value = float(np.sqrt(max(var_sample, 0.0)))

        if self.annualize:
            vol_value *= np.sqrt(252)

        return {"volatility": self._emit(vol_value)}

    def _emit(self, vol_value: float) -> float:
        """输出前去极值（F-10 winsorize）：expanding MAD 截断（流式）。

        边界只含截至当前已收盘 bar 的历史 volatility 值（铁律11，防未来）；
        样本 < WINSOR_MIN_PERIODS 不截断（防御式）；NaN 不进入历史。
        """
        if not self.winsorize or not np.isfinite(vol_value):
            return float(vol_value)
        hist = list(self._vol_history)
        hist.append(float(vol_value))
        self._vol_history.append(float(vol_value))
        if len(hist) < WINSOR_MIN_PERIODS:
            return float(vol_value)
        arr = np.asarray(hist, dtype=float)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        if mad <= 0.0:
            # MAD 退化（重复值多）→ std 兜底：mean ± k·std
            mean_v = float(np.mean(arr))
            sd = float(np.std(arr))
            if sd <= 0.0:
                return float(vol_value)
            lo = mean_v - self.winsorize_k * sd
            hi = mean_v + self.winsorize_k * sd
        else:
            lo = med - self.winsorize_k * MAD_CONSTANT * mad
            hi = med + self.winsorize_k * MAD_CONSTANT * mad
        return float(min(max(vol_value, lo), hi))

    def reset(self) -> None:
        """重置波动率状态"""
        super().reset()
        for key in ['prev_close', '_vol_window_sum', '_vol_window_sum_sq',
                    '_vol_init_sum', '_vol_init_sum_sq',
                    '_ewma_var', '_ewma_prev_ret']:
            self._state.pop(key, None)
        self._vol_history.clear()


# ============================================================================
# ExtremeRegime - 极端行情区制标记（F-10）
# ============================================================================

@register_indicator
class ExtremeRegime(IndicatorBase):
    """
    极端行情区制标记（Extreme Regime，F-10 波动率稳健化配套）

    公式:
        abs_r_t = |r_t|
        thr_t = 滚动窗口 abs_r 的分位数（period 根，含 t 时刻）
        regime_t = 1 若 abs_r_t > thr_t，否则 0

    极端行情区制：|收益| 超过滚动绝对收益 99 分位（默认）视为异常波动日，
    标记为 1 供下游特征/区制分析使用（与 EWMA/稳健波动率叠加使用）。

    参数:
        period: 滚动分位窗口，默认20
        quantile: 分位阈值，默认0.99（[0.90, 1.0) 范围，防御式校验）
        use_log_return: 是否使用对数收益率，默认False

    防未来函数: rolling(window=period) 第i行仅含 iloc[i-period+1:i+1]
    （本行使用的数据截至 t 时刻）；thr_t 由截至 t 的窗口计算，regime_t 用
    t 时刻已收盘收益比较，无任何未来引用。
    输出列: regime
    """

    name = "extreme_regime"
    output_columns = ["regime"]

    def __init__(
        self,
        period: int = 20,
        quantile: float = 0.99,
        use_log_return: bool = False,
        **kwargs: Any,
    ):
        super().__init__(
            period=period, quantile=quantile, use_log_return=use_log_return, **kwargs
        )
        self.period = period
        self.quantile = quantile
        self.use_log_return = use_log_return
        if period < 5:
            raise ValueError(f"ExtremeRegime period 必须 >=5，当前: {period}")
        if not 0.90 <= quantile < 1.0:
            raise ValueError(
                f"ExtremeRegime quantile 必须在 [0.90, 1.0)，当前: {quantile}")
        self._min_periods = period

    def _get_history_size(self) -> int:
        return self.period + 10

    def _compute_returns(self, data: pd.DataFrame) -> pd.Series:
        """计算收益率序列（与 Volatility 同口径）"""
        if self.use_log_return:
            close = data['收盘价'].astype(float)
            return np.log(close / close.shift(1))
        if 'pct_chg' in data.columns:
            return data['pct_chg'].astype(float) / 100.0
        close = data['收盘价'].astype(float)
        return close.pct_change()

    @lookahead_check("extreme_regime")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """批量计算区制标记"""
        self._validate_data(data)
        returns = self._compute_returns(data)
        abs_r = returns.abs()
        thr = abs_r.rolling(
            window=self.period, min_periods=self.period).quantile(self.quantile)
        regime = (abs_r > thr).astype(float)
        regime.loc[thr.isna()] = np.nan  # 窗口不足（阈值缺失）→ NaN 防御
        return self._make_result_frame(data, {"regime": regime})

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """流式更新区制标记（实盘模式：维护窗口绝对收益，达 period 后出值）"""
        close = float(bar['收盘价'])
        if 'prev_close' not in self._state:
            self._state['prev_close'] = close
            if not self.use_log_return and 'pct_chg' in bar and bar['pct_chg'] is not None:
                ret0 = float(bar['pct_chg']) / 100.0
                self._history.append(ret0)
            return {"regime": np.nan}
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
            return {"regime": np.nan}
        w = np.asarray(list(self._history)[-self.period:], dtype=float)
        if not np.all(np.isfinite(w)):
            return {"regime": np.nan}
        thr = float(np.quantile(np.abs(w), self.quantile))
        return {"regime": float(1.0 if abs(ret) > thr else 0.0)}


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
        price_col: str = "收盘价",
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
