# 文件名：myquant/indicators/rsrs.py
"""
鲲鹏量化交易系统 - RSRS核心指标（Resistance Support Relative Strength）
==========================================================================
阻力支撑相对强度指标，本系统核心择时指标

策略参数标准（交接文档7.2节）:
    period=18, norm_window=60, buy_threshold=0.7, sell_threshold=-0.7

算法原理:
    1. 取最近N根K线的最高价(high)和最低价(low)
    2. 线性回归: high = α + β * low  （以low为自变量，high为因变量）
    3. β（斜率 slope）即为RSRS原始值
    4. 标准化: 取最近M个β值，计算 z-score = (β - mean) / std
    5. 标准化后的RSRS值用于交易信号: >0.7买入，<-0.7卖出

★★★ 防未来函数铁律（规则4第1条）★★★
    RSRS斜率用 iloc[i-period:i]，不含第i根K线！
    即: 第i根K线的β值，仅使用第 i-period 到 i-1 根K线的高低价回归
    这样第i根K线开盘时，β值就已经确定（基于前一日数据），不存在未来函数

    标准化z-score: 第i根的z值使用包含第i根β在内的最近M个β值
    （因为β_i本身不含未来信息，在计算z_i时已知）

★★★ V5性能优化：闭式OLS纯向量化 ★★★
    原实现使用 Python for 循环逐窗口调用 _linear_regression（等价于 rolling.apply
    伪向量化），性能瓶颈在 Python 层循环开销。
    重构后使用闭式 OLS + pandas rolling 求和纯向量化计算：
      β = (n·Σxy - Σx·Σy) / (n·Σx² - (Σx)²)
      R² = (n·Σxy - Σx·Σy)² / [(n·Σx² - (Σx)²)·(n·Σy² - (Σy)²)]
    全部计算在 C 层完成，单标的 10 年日线从秒级降至毫秒级。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.indicators import IndicatorBase, lookahead_check, register_indicator

logger = get_logger(__name__)


@register_indicator
class RSRS(IndicatorBase):
    """
    RSRS指标（Resistance Support Relative Strength）

    参数:
        period: 回归窗口周期，默认18（策略标准）
        norm_window: 标准化窗口周期，默认60（策略标准）
        buy_threshold: 买入阈值，默认0.7
        sell_threshold: 卖出阈值，默认-0.7
        standardize: 是否输出标准化z-score，默认True

    防未来函数:
        - 斜率β_i = 回归(high[i-period:i], low[i-period:i])，不含第i根
        - 等价于: 先算含当日的β序列，再整体shift(1)
        - 标准化z_i = (β_i - mean(β_{i-M+1:i+1})) / std(β_{i-M+1:i+1})

    输出列:
        - rsrs: 标准化后的RSRS值（z-score），用于交易信号
        - rsrs_raw: 原始斜率β值（未标准化）
        - rsrs_r2: 回归决定系数 R²（拟合优度）
        - signal: 交易信号（1=买入, -1=卖出, 0=持有）
    """

    name = "rsrs"
    output_columns = ["rsrs", "rsrs_raw", "rsrs_r2", "signal"]

    def __init__(
        self,
        period: int = 18,
        norm_window: int = 60,
        buy_threshold: float = 0.7,
        sell_threshold: float = -0.7,
        standardize: bool = True,
        **kwargs: Any,
    ):
        super().__init__(
            period=period,
            norm_window=norm_window,
            buy_threshold=buy_threshold,
            sell_threshold=sell_threshold,
            standardize=standardize,
            **kwargs,
        )
        self.period = period
        self.norm_window = norm_window
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.standardize = standardize
        # 第一个有效值出现在第 period + norm_window - 1 行
        # （因为β需要period根，z需要norm_window个β，且β不含当日）
        self._min_periods = period + norm_window

    def _get_history_size(self) -> int:
        """流式模式下K线历史缓冲区大小 = 回归窗口 + 标准化窗口 + 余量"""
        return self.period + self.norm_window + 10

    def _get_indicator_history_size(self) -> int:
        """流式模式下β值历史缓冲区大小 = 标准化窗口 + 余量"""
        return self.norm_window + 10

    # ------------------------------------------------------------------
    # 核心: 闭式OLS纯向量化滚动回归（V5性能优化核心）
    # ------------------------------------------------------------------

    @staticmethod
    def _rolling_ols_vectorized(
        low: pd.Series,
        high: pd.Series,
        period: int,
    ) -> Tuple[pd.Series, pd.Series]:
        """
        闭式OLS纯向量化滚动线性回归: high = α + β * low

        使用滚动求和闭式公式，全部计算在 C 层完成，无 Python 层循环：
          β = (n·Σxy - Σx·Σy) / (n·Σx² - (Σx)²)
          R² = (n·Σxy - Σx·Σy)² / [(n·Σx² - (Σx)²)·(n·Σy² - (Σy)²)]

        本行使用的数据截至 t 时刻（rolling 窗口含当前行，不含未来数据）。
        防未来函数由调用方通过 shift(1) 保证。

        Args:
            low: 最低价 Series（自变量 x）。
            high: 最高价 Series（因变量 y）。
            period: 回归窗口周期。

        Returns:
            (beta_series, r2_series) 元组，均为与输入等长的 Series，
            前 period-1 行为 NaN（数据不足）。
        """
        n = float(period)

        # 滚动求和（min_periods=period 确保前 period-1 行为 NaN）
        # 本行使用的数据截至 t 时刻（rolling 窗口 [t-period+1, t]，不含未来）
        sum_x = low.rolling(window=period, min_periods=period).sum()
        sum_y = high.rolling(window=period, min_periods=period).sum()
        sum_xy = (low * high).rolling(window=period, min_periods=period).sum()
        sum_xx = (low ** 2).rolling(window=period, min_periods=period).sum()
        sum_yy = (high ** 2).rolling(window=period, min_periods=period).sum()

        # 闭式公式计算斜率 β
        # β = (n·Σxy - Σx·Σy) / (n·Σx² - (Σx)²)
        numerator = n * sum_xy - sum_x * sum_y
        denominator = n * sum_xx - sum_x ** 2

        # 处理分母为零（low 在窗口内全相同，无法回归）
        beta = numerator / denominator.replace(0, np.nan)

        # 闭式公式计算 R²
        # R² = (n·Σxy - Σx·Σy)² / [(n·Σx² - (Σx)²)·(n·Σy² - (Σy)²)]
        r2_numerator = numerator ** 2
        r2_denominator = denominator * (n * sum_yy - sum_y ** 2)
        r2 = r2_numerator / r2_denominator.replace(0, np.nan)

        # R² 应在 [0, 1] 范围内，浮点误差可能导致略微超出，截断处理
        r2 = r2.clip(lower=0.0, upper=1.0)

        return beta, r2

    # ------------------------------------------------------------------
    # 单窗口线性回归（流式 update / 校验仍需使用）
    # ------------------------------------------------------------------

    @staticmethod
    def _linear_regression(
        low: np.ndarray,
        high: np.ndarray,
    ) -> Tuple[float, float, float]:
        """
        一元线性回归: high = α + β * low

        仅用于流式 update() 和 validate_no_lookahead() 校验。
        批量计算请使用 _rolling_ols_vectorized() 纯向量化方法。

        Args:
            low: 最低价数组（自变量 x）。
            high: 最高价数组（因变量 y）。

        Returns:
            (slope, intercept, r_squared)
            slope: 斜率 β
            intercept: 截距 α
            r_squared: 决定系数 R²
        """
        n = len(low)
        if n < 2:
            return np.nan, np.nan, np.nan

        # 检查是否有NaN
        if np.any(np.isnan(low)) or np.any(np.isnan(high)):
            return np.nan, np.nan, np.nan

        # 检查low是否全相同（无法回归）
        if np.std(low) == 0:
            return np.nan, np.nan, np.nan

        # 最小二乘法（Closed-form）
        # β = Σ(x_i - x̄)(y_i - ȳ) / Σ(x_i - x̄)²
        x_mean = np.mean(low)
        y_mean = np.mean(high)

        ss_xy = np.sum((low - x_mean) * (high - y_mean))
        ss_xx = np.sum((low - x_mean) ** 2)

        if ss_xx == 0:
            return np.nan, np.nan, np.nan

        slope = ss_xy / ss_xx
        intercept = y_mean - slope * x_mean

        # R² = 1 - SS_res / SS_tot
        y_pred = slope * low + intercept
        ss_res = np.sum((high - y_pred) ** 2)
        ss_tot = np.sum((high - y_mean) ** 2)

        if ss_tot == 0:
            r_squared = np.nan
        else:
            r_squared = 1.0 - ss_res / ss_tot

        return float(slope), float(intercept), float(r_squared)

    # ------------------------------------------------------------------
    # 批量计算（回测模式）— V5闭式OLS纯向量化实现
    # ------------------------------------------------------------------

    @lookahead_check("rsrs")
    def calculate(self, data: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """
        批量计算RSRS指标（回测模式）— V5闭式OLS纯向量化实现

        ★★★ 防未来函数核心实现 ★★★
        第i根K线的β值 = 回归(high[i-period:i], low[i-period:i])，不含第i根
        实现方式: 先计算含当日的β序列（rolling含当前行），再整体shift(1)
        这样 β_i 实际使用的是 i-1 及之前的数据，严格不含当日

        性能优化:
          - β 和 R² 使用闭式 OLS + rolling 求和纯向量化计算
          - 消除原 Python for 循环逐窗口回归的性能瓶颈
          - shift 操作使用 pandas .shift(1) 替代手动循环
        """
        self._validate_data(data)

        high = data["high"].astype(float)
        low = data["low"].astype(float)
        n = len(data)

        # ==================================================================
        # 第一步: 闭式OLS纯向量化计算原始斜率β序列（含当日的rolling回归）
        # 本行使用的数据截至 t 时刻（rolling 窗口含当前行，不含未来）
        # ==================================================================
        beta_raw, r2_raw = self._rolling_ols_vectorized(low, high, self.period)

        # ==================================================================
        # 第二步: ★★★ 防未来函数 — 整体shift(1) ★★★
        # β_i（第i根的RSRS斜率）= beta_raw[i-1]
        # 即第i根使用的是第i-1根计算出的斜率（基于 i-period 到 i-1 的数据）
        # 严格不含第i根K线数据
        # ==================================================================
        beta_shifted = beta_raw.shift(1)
        r2_shifted = r2_raw.shift(1)

        # ==================================================================
        # 第三步: 标准化 z-score
        # 使用包含当前β在内的最近norm_window个β值
        # （因为β_i已经shift过，不含未来信息，计算z_i时β_i已知）
        # ==================================================================
        if self.standardize:
            # rolling(window=norm_window) 包含当前行
            # 本行使用的数据截至 t 时刻（β序列已shift，不含未来信息）
            beta_mean = beta_shifted.rolling(
                window=self.norm_window, min_periods=self.norm_window
            ).mean()
            beta_std = beta_shifted.rolling(
                window=self.norm_window, min_periods=self.norm_window
            ).std(ddof=1)

            # z-score = (β - mean) / std
            with np.errstate(divide="ignore", invalid="ignore"):
                rsrs_z = (beta_shifted - beta_mean) / beta_std
            rsrs_z = rsrs_z.values
        else:
            rsrs_z = beta_shifted.values.copy()

        # ==================================================================
        # 第四步: 生成交易信号
        # rsrs > buy_threshold → 买入信号(1)
        # rsrs < sell_threshold → 卖出信号(-1)
        # 其他 → 持有(0)
        # ==================================================================
        signal = np.zeros(n, dtype=int)
        with np.errstate(invalid="ignore"):
            signal[rsrs_z > self.buy_threshold] = 1
            signal[rsrs_z < self.sell_threshold] = -1
        # 数据不足的位置信号为0
        signal[np.isnan(rsrs_z)] = 0

        # ==================================================================
        # 构建结果DataFrame
        # ==================================================================
        result = pd.DataFrame(index=data.index)
        result["rsrs"] = rsrs_z              # 标准化RSRS值（用于交易信号）
        result["rsrs_raw"] = beta_shifted.values  # 原始斜率β（已shift，防未来函数）
        result["rsrs_r2"] = r2_shifted.values     # 回归R²（已shift）
        result["signal"] = signal                  # 交易信号

        logger.debug(
            "RSRS批量计算完成(闭式OLS向量化): %d根K线, "
            "period=%d, norm_window=%d, "
            "第一个有效值位置: %s",
            n, self.period, self.norm_window,
            np.argmax(~np.isnan(rsrs_z)) if np.any(~np.isnan(rsrs_z)) else "N/A",
        )

        return result

    # ------------------------------------------------------------------
    # 流式更新（实盘模式）
    # ------------------------------------------------------------------

    def update(self, bar: Dict[str, Any]) -> Dict[str, float]:
        """
        流式更新RSRS（实盘模式）

        ★★★ 防未来函数 ★★★
        传入的bar视为"已收盘"K线，先将其加入历史缓冲区，
        然后用 不含当前bar 的最近period根做回归（即取缓冲区中除最后一根外的最近period根）
        这样返回的RSRS值是基于当前bar之前的数据，在当前bar开盘时就已确定

        Args:
            bar: 单根K线字典，包含 high/low/close 等

        Returns:
            {rsrs, rsrs_raw, rsrs_r2, signal}
        """
        high = float(bar["high"])
        low = float(bar["low"])

        # 将当前bar加入历史缓冲区
        self._history.append({"high": high, "low": low})

        # ==================================================================
        # 防未来函数: 用不含当前bar的最近period根做回归
        # 即取 self._history[:-1] 中的最后period根
        # 等价于批量模式中的 shift(1)
        # ==================================================================
        history_list = list(self._history)

        if len(history_list) <= self.period:
            # 数据不足（需要至少period+1根，因为不含当前bar）
            self._indicator_history.append(np.nan)
            return {
                "rsrs": np.nan,
                "rsrs_raw": np.nan,
                "rsrs_r2": np.nan,
                "signal": 0,
            }

        # 取除最后一根（当前bar）外的最近period根
        regression_window = history_list[-(self.period + 1):-1]
        window_low = np.array([b["low"] for b in regression_window], dtype=np.float64)
        window_high = np.array([b["high"] for b in regression_window], dtype=np.float64)

        # 线性回归
        beta, _, r2 = self._linear_regression(window_low, window_high)

        # 将β值加入指标历史缓冲区
        self._indicator_history.append(beta)

        # ==================================================================
        # 标准化 z-score
        # 使用包含当前β在内的最近norm_window个β值
        # ==================================================================
        if not self.standardize:
            rsrs_value = beta
        else:
            beta_history = list(self._indicator_history)
            # 去掉末尾的NaN（数据不足时期）
            valid_betas = [b for b in beta_history if not np.isnan(b)]

            if len(valid_betas) < self.norm_window:
                rsrs_value = np.nan
            else:
                recent_betas = np.array(valid_betas[-self.norm_window:], dtype=np.float64)
                beta_mean = np.mean(recent_betas)
                beta_std = np.std(recent_betas, ddof=1)

                if beta_std == 0 or np.isnan(beta_std):
                    rsrs_value = 0.0
                else:
                    rsrs_value = (beta - beta_mean) / beta_std

        # ==================================================================
        # 交易信号
        # ==================================================================
        if np.isnan(rsrs_value):
            signal = 0
        elif rsrs_value > self.buy_threshold:
            signal = 1
        elif rsrs_value < self.sell_threshold:
            signal = -1
        else:
            signal = 0

        return {
            "rsrs": float(rsrs_value) if not np.isnan(rsrs_value) else np.nan,
            "rsrs_raw": float(beta) if not np.isnan(beta) else np.nan,
            "rsrs_r2": float(r2) if not np.isnan(r2) else np.nan,
            "signal": int(signal),
        }

    def reset(self) -> None:
        """重置RSRS状态"""
        super().reset()

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def get_signal_thresholds(self) -> Dict[str, float]:
        """获取信号阈值"""
        return {
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
        }

    def validate_no_lookahead(self, data: pd.DataFrame) -> bool:
        """
        防未来函数自校验（开发/测试用）

        验证: 第i行的rsrs_raw值，是否等于用 iloc[i-period:i] 回归的结果
        如果不等，说明存在未来函数

        Returns:
            True=通过校验，False=存在未来函数
        """
        result = self.calculate(data)
        high = data["high"].astype(float).values
        low = data["low"].astype(float).values

        for i in range(self.period, len(data)):
            expected_beta = result["rsrs_raw"].iloc[i]
            if np.isnan(expected_beta):
                continue

            # 用 iloc[i-period:i] 独立计算（不含第i根）
            window_low = low[i - self.period: i]
            window_high = high[i - self.period: i]
            actual_beta, _, _ = self._linear_regression(window_low, window_high)

            if not np.isclose(expected_beta, actual_beta, rtol=1e-10, equal_nan=True):
                logger.error(
                    "[防未来函数校验失败] 第%d行: "
                    "预期β=%.6f, 实际β=%.6f, 差值=%.2e",
                    i, expected_beta, actual_beta,
                    abs(expected_beta - actual_beta),
                )
                return False

        logger.info("[防未来函数校验通过] RSRS指标不存在未来函数")
        return True


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  rsrs.py — 闭式OLS纯向量化 自测 Demo")
    print("=" * 60)

    # 构造测试数据
    print("\n[1] 构造测试数据 (500根日线)")
    dates = pd.date_range("2023-01-02", periods=500, freq="B")
    np.random.seed(42)
    base_price = 100.0
    returns = np.random.randn(500) * 0.02
    close = base_price * np.cumprod(1 + returns)
    high = close * (1 + np.abs(np.random.randn(500) * 0.01))
    low = close * (1 - np.abs(np.random.randn(500) * 0.01))
    open_price = close * (1 + np.random.randn(500) * 0.005)
    volume = np.random.randint(100000, 500000, 500)

    test_data = pd.DataFrame(
        {"open": open_price, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )
    test_data.index.name = "date"
    print(f"    数据形状: {test_data.shape}")

    # ── 测试 2: 闭式OLS向量化计算 ──
    print("\n[2] 闭式OLS向量化计算")
    rsrs = RSRS(period=18, norm_window=60)
    result = rsrs.calculate(test_data)
    print(f"    结果形状: {result.shape}")
    print(f"    结果列: {list(result.columns)}")
    print(f"    第一个有效值位置: {result['rsrs_raw'].first_valid_index()}")
    print(f"    rsrs_raw 范围: [{result['rsrs_raw'].min():.4f}, {result['rsrs_raw'].max():.4f}]")
    print(f"    rsrs_r2 范围: [{result['rsrs_r2'].min():.4f}, {result['rsrs_r2'].max():.4f}]")
    assert result.shape[0] == 500
    assert result["rsrs_raw"].notna().sum() > 0
    print(f"    ✓ 闭式OLS向量化计算正常")

    # ── 测试 3: 正确性验证（与naive循环对比） ──
    print("\n[3] 正确性验证（与naive Python循环对比）")
    high_arr = test_data["high"].astype(float).values
    low_arr = test_data["low"].astype(float).values
    period = 18

    # naive 循环计算含当日的β
    beta_naive = np.full(500, np.nan)
    for i in range(period - 1, 500):
        w_low = low_arr[i - period + 1: i + 1]
        w_high = high_arr[i - period + 1: i + 1]
        b, _, _ = RSRS._linear_regression(w_low, w_high)
        beta_naive[i] = b

    # 向量化计算含当日的β
    beta_vec, r2_vec = RSRS._rolling_ols_vectorized(
        test_data["low"].astype(float), test_data["high"].astype(float), period
    )
    beta_vec_arr = beta_vec.values

    # 对比（忽略NaN位置）
    valid_mask = ~np.isnan(beta_naive) & ~np.isnan(beta_vec_arr)
    max_diff = np.max(np.abs(beta_naive[valid_mask] - beta_vec_arr[valid_mask]))
    print(f"    有效对比点数: {valid_mask.sum()}")
    print(f"    最大绝对差值: {max_diff:.2e}")
    assert max_diff < 1e-10, f"向量化结果与naive循环差异过大: {max_diff}"
    print(f"    ✓ 向量化结果与naive循环完全一致 (max_diff={max_diff:.2e})")

    # ── 测试 4: 性能基准 ──
    print("\n[4] 性能基准 (2000根日线)")
    dates_big = pd.date_range("2020-01-02", periods=2000, freq="B")
    np.random.seed(123)
    close_big = 100.0 * np.cumprod(1 + np.random.randn(2000) * 0.02)
    high_big = close_big * (1 + np.abs(np.random.randn(2000) * 0.01))
    low_big = close_big * (1 - np.abs(np.random.randn(2000) * 0.01))
    open_big = close_big * (1 + np.random.randn(2000) * 0.005)
    vol_big = np.random.randint(100000, 500000, 2000)
    big_data = pd.DataFrame(
        {"open": open_big, "high": high_big, "low": low_big, "close": close_big, "volume": vol_big},
        index=dates_big,
    )

    # 向量化版本计时
    rsrs_big = RSRS(period=18, norm_window=60)
    start = time.perf_counter()
    _ = rsrs_big.calculate(big_data)
    vec_time = time.perf_counter() - start
    print(f"    向量化版本: {vec_time*1000:.2f} ms")

    # naive 循环版本计时（仅β计算部分）
    high_big_arr = big_data["high"].astype(float).values
    low_big_arr = big_data["low"].astype(float).values
    start = time.perf_counter()
    beta_naive_big = np.full(2000, np.nan)
    for i in range(period - 1, 2000):
        w_low = low_big_arr[i - period + 1: i + 1]
        w_high = high_big_arr[i - period + 1: i + 1]
        b, _, _ = RSRS._linear_regression(w_low, w_high)
        beta_naive_big[i] = b
    naive_time = time.perf_counter() - start
    print(f"    Naive循环版本: {naive_time*1000:.2f} ms")
    speedup = naive_time / vec_time if vec_time > 0 else float("inf")
    print(f"    加速比: {speedup:.1f}x")
    assert speedup > 1.0, "向量化版本应比naive循环更快"
    print(f"    ✓ 性能提升验证通过 (加速比 {speedup:.1f}x)")

    # ── 测试 5: 防未来函数校验 ──
    print("\n[5] 防未来函数校验")
    passed = rsrs.validate_no_lookahead(test_data)
    assert passed, "防未来函数校验失败"
    print(f"    ✓ 防未来函数校验通过")

    # ── 测试 6: 流式更新 ──
    print("\n[6] 流式更新测试")
    rsrs_stream = RSRS(period=18, norm_window=60)
    stream_results = []
    for i in range(100):
        bar = {
            "date": test_data.index[i],
            "open": float(test_data["open"].iloc[i]),
            "high": float(test_data["high"].iloc[i]),
            "low": float(test_data["low"].iloc[i]),
            "close": float(test_data["close"].iloc[i]),
            "volume": float(test_data["volume"].iloc[i]),
        }
        res = rsrs_stream.update(bar)
        stream_results.append(res)

    # 检查流式结果与批量结果是否一致（在数据充足的位置）
    batch_result = rsrs.calculate(test_data.iloc[:100])
    norm_window = 60
    # 找一个有效值位置
    valid_pos = None
    for i in range(period + norm_window, 100):
        if not np.isnan(stream_results[i]["rsrs_raw"]):
            valid_pos = i
            break

    if valid_pos is not None:
        stream_beta = stream_results[valid_pos]["rsrs_raw"]
        batch_beta = batch_result["rsrs_raw"].iloc[valid_pos]
        print(f"    位置 {valid_pos}: 流式β={stream_beta:.6f}, 批量β={batch_beta:.6f}")
        assert np.isclose(stream_beta, batch_beta, rtol=1e-10), \
            f"流式与批量结果不一致: {stream_beta} vs {batch_beta}"
        print(f"    ✓ 流式更新与批量计算结果一致")
    else:
        print(f"    ⚠ 100根数据不足，跳过流式一致性验证 (需要 period+norm_window={period+norm_window} 根)")

    # ── 测试 7: 边界情况 ──
    print("\n[7] 边界情况测试")
    # low全相同的情况
    constant_data = test_data.copy()
    constant_data["low"] = 50.0
    result_const = rsrs.calculate(constant_data)
    # low全相同时β应为NaN（无法回归）
    assert result_const["rsrs_raw"].notna().sum() == 0, \
        "low全相同时β应全部为NaN"
    print(f"    ✓ low全相同时β全部为NaN")

    # 空数据
    try:
        rsrs.calculate(pd.DataFrame())
        assert False, "空数据应抛出ValueError"
    except ValueError:
        print(f"    ✓ 空数据正确抛出ValueError")

    # ── 测试 8: R² 范围 ──
    print("\n[8] R² 范围验证")
    r2_valid = result["rsrs_r2"].dropna()
    assert (r2_valid >= 0).all() and (r2_valid <= 1).all(), \
        f"R² 应在 [0,1] 范围内, 实际: [{r2_valid.min()}, {r2_valid.max()}]"
    print(f"    R² 范围: [{r2_valid.min():.4f}, {r2_valid.max():.4f}]")
    print(f"    ✓ R² 全部在 [0,1] 范围内")

    print("\n" + "=" * 60)
    print("  ✓ rsrs.py 闭式OLS纯向量化 全部自测通过")
    print("=" * 60)
