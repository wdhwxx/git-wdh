# 文件名：myquant/strategies/kunpeng_strategy.py
"""
strategies/kunpeng_strategy.py - 鲲鹏起爆点策略
================================================
中低频趋势启动型选股策略，捕捉个股在长期横盘整理后、成交量与均线系统
同步共振的起爆临界点。适用于 A 股主板及创业板，持仓周期 5-20 个交易日。

策略逻辑（量·价·均线三重共振）:
  1. 量能模块（鲲鹏A/H/I）：量比 A = 当日成交量/前5日平均成交量，超
     vol_ratio_threshold（默认4.5）视为资金涌入；近 vol_count_window 日内
     成交量超五档均线最大值的天数达到 vol_active_days → 近期放量门（鲲鹏I）。
  2. 均线粘合模块（鲲鹏M）：20/30/60 日收盘均线极差比 ≤ ma_sticky_threshold
     （默认1.22），股价处于长期横盘整理末端。
  3. MACD 起爆模块（鲲鹏N/O/S/起爆二）：MACD 柱连续4日递减（T-4~T-1）+ 当日
     MACD≥0 且转升，叠加 EMA13 上行/指标粘合/MACD小幅（起爆点一，鲲鹏S）；
     或增加 EMA30 不降 + 5>10>30 多头排列 + 站上EMA30（起爆点二）。
  最终信号 = (起爆点一 OR 起爆点二) AND 近期放量门 AND 均线粘合度达标。

出场规则（三档方案，默认标准型）:
  - conservative : -5% 止损 / +10% 止盈 / 无移动止盈
  - standard     : -7% 止损 / +15% 止盈 / 盈利>8% 后回撤3% 平仓（推荐默认）
  - aggressive   : -10% 止损 / +25% 止盈 / 盈利>12% 后回撤5% 平仓
  全部档位叠加"持有 hold_days_cap 个交易日兜底"强制离场，避免资金长期占用。

三引擎接口:
  - on_bar                    : event 引擎（真值基准），返回 List[Signal]
  - generate_mixed_loop       : mixed 引擎（生产主力），返回目标持仓股数
  - generate_signals_vectorized : vectorized 引擎（参数扫描），返回目标持仓数组
  三接口共用同一出场状态机（_evaluate_exit），保证信号日集合一致。

防未来函数（铁律）:
  - 全部指标只使用截至 T 日收盘的 rolling/shift/ewm 数据，天然无未来函数；
  - T 日收盘生成信号，订单由引擎在 T+1 日开盘撮合（引擎层统一执行）；
  - event/mixed 按当前 bar 时间戳截断 context.data，杜绝读取未来数据；
  - 数据不足最长滚动窗口（60日）的 bar 无信号（严格窗口，min_periods 不放宽）。

实现来源:
  docs/鲲鹏壹号策略_回测实施报告.md 第2/3/4/5/6章（通达信公式逐行转译）。
"""

from __future__ import annotations

# ── 标准库 ──
from typing import Any, Dict, List, Optional, Tuple

# ── 第三方库 ──
import numpy as np
import pandas as pd

# ── 本地模块 ──
from myquant.core.logger import get_logger
from myquant.models.market_data import Bar
from myquant.models.trade import Position
from myquant.strategies.base import (
    Signal,
    SignalType,
    StrategyBase,
    StrategyContext,
)
from myquant.strategies.registry import register_strategy

logger = get_logger(__name__)


# ═══════════════════════════════════════════════
#  常量（通达信公式参数，对应报告表4 全量参数清单）
# ═══════════════════════════════════════════════

# 量能模块
DEFAULT_VOL_RATIO_THRESHOLD: float = 4.5          # 鲲鹏A 量比阈值
DEFAULT_VOL_COUNT_WINDOW: int = 6                 # 鲲鹏H COUNT 窗口（原注"原数据7"）
DEFAULT_VOL_ACTIVE_DAYS: int = 5                  # 鲲鹏H>=5 放量天数门槛
DEFAULT_BARSLAST_VOL_WINDOW: int = 13             # BARSLAST(H>=5)<13 窗口
DEFAULT_BARSLAST_RATIO_WINDOW: int = 15           # BARSLAST(A>4.5)<15 窗口

# 均线模块
DEFAULT_MA_STICKY_THRESHOLD: float = 1.22         # 鲲鹏M<=1.22 粘合度阈值

# MACD 模块
DEFAULT_DIFF_DEA_RATIO: float = 0.018             # |DIFF-DEA|/C<0.018
DEFAULT_MACD_ABS_THRESHOLD: float = 0.10          # MACD<0.10
DEFAULT_MACD_RISE_THRESHOLD: float = 0.0001       # 鲲鹏O/U：MACD>阈值 严格翻红（排除贴零轴临界假金叉）

# 出场组
DEFAULT_EXIT_MODE: str = "standard"               # 三档：conservative/standard/aggressive
DEFAULT_HOLD_DAYS_CAP: int = 60                   # 持有上限兜底（交易日）

# 仓位组
DEFAULT_MAX_POSITION_PCT: float = 0.20            # 单票最大仓位
DEFAULT_MAX_POSITIONS: int = 5                    # 同时最大持仓数
DEFAULT_CAPITAL_BASE: float = 1_000_000.0         # 仓位资金基准（元）

# MACD 标准参数（通达信）
MACD_FAST_PERIOD: int = 12
MACD_SLOW_PERIOD: int = 26
MACD_SIGNAL_PERIOD: int = 9

# 均线粘合周期组（20/30/60 日收盘均线）
MA_STICKY_PERIODS: Tuple[int, ...] = (20, 30, 60)

# EMA13 起爆一上行周期
EMA13_PERIOD: int = 13

# 预热期 = 最长滚动窗口（60日均线），此前无信号（第5章决策10）
WARMUP_BARS: int = 60

# 粘合度分母防除零 epsilon（第5章决策7）
EPSILON: float = 1e-8

# 出场档位映射: exit_mode -> (止损, 止盈, 移动止盈激活线, 移动止盈回撤线)
# 移动止盈激活线/回撤线为 None 表示该档位不启用移动止盈
EXIT_TIERS: Dict[str, Tuple[float, float, Optional[float], Optional[float]]] = {
    "conservative": (0.05, 0.10, None, None),
    "standard": (0.07, 0.15, 0.08, 0.03),
    "aggressive": (0.10, 0.25, 0.12, 0.05),
}


@register_strategy
class KunpengStrategy(StrategyBase):
    """
    鲲鹏起爆点策略

    核心参数（与 docs/鲲鹏壹号策略_回测实施报告.md 表4 全量参数清单一致）:
      vol_ratio_threshold   : 量比阈值（鲲鹏A>阈值），默认4.5
      vol_count_window      : 放量天数 COUNT 窗口（鲲鹏H），默认6
      vol_active_days       : 放量天数门槛（鲲鹏H>=门槛），默认5
      barslast_vol_window   : BARSLAST(H>=门槛)<窗口，默认13
      barslast_ratio_window : BARSLAST(A>阈值)<窗口，默认15
      ma_sticky_threshold   : 均线粘合度阈值（鲲鹏M<=阈值），默认1.22
      diff_dea_ratio        : |DIFF-DEA|/C<阈值，默认0.018
      macd_abs_threshold    : MACD<阈值，默认0.10
      macd_rise_threshold   : 翻红严格阈值（鲲鹏O/U：MACD>阈值），默认0.0001
      exit_mode             : 出场档位（conservative/standard/aggressive），默认standard
      take_profit_pct       : 止盈阈值，None 时取档位默认（standard=0.15）
      stop_loss_pct         : 止损阈值，None 时取档位默认（standard=0.07）
      hold_days_cap         : 持有上限兜底（交易日），默认60
      max_position_pct      : 单票最大仓位，默认0.20
      max_positions         : 同时最大持仓数，默认5
      capital_base          : 仓位资金基准（元），默认1,000,000

    信号生成:
      - 最终信号 = (起爆点一 OR 起爆点二) AND 近期放量门 AND 均线粘合度达标
      - 入场：T 日收盘信号为 True 且无该标的持仓 → T+1 开盘买入
      - 出场：止损 / 止盈 / 移动止盈 / 持有上限 四条件逐日检查（T 日收盘判断）

    防未来函数:
      - 全部指标仅用截至当前 bar 的历史窗口（rolling/shift/ewm）
      - event/mixed 按 bar.timestamp 截断 context.data
      - T 日信号 T+1 成交（引擎层统一执行）
    """

    name = "kunpeng"

    def __init__(
        self,
        vol_ratio_threshold: float = DEFAULT_VOL_RATIO_THRESHOLD,
        vol_count_window: int = DEFAULT_VOL_COUNT_WINDOW,
        vol_active_days: int = DEFAULT_VOL_ACTIVE_DAYS,
        barslast_vol_window: int = DEFAULT_BARSLAST_VOL_WINDOW,
        barslast_ratio_window: int = DEFAULT_BARSLAST_RATIO_WINDOW,
        ma_sticky_threshold: float = DEFAULT_MA_STICKY_THRESHOLD,
        diff_dea_ratio: float = DEFAULT_DIFF_DEA_RATIO,
        macd_abs_threshold: float = DEFAULT_MACD_ABS_THRESHOLD,
        macd_rise_threshold: float = DEFAULT_MACD_RISE_THRESHOLD,
        exit_mode: str = DEFAULT_EXIT_MODE,
        take_profit_pct: Optional[float] = None,
        stop_loss_pct: Optional[float] = None,
        hold_days_cap: int = DEFAULT_HOLD_DAYS_CAP,
        max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
        max_positions: int = DEFAULT_MAX_POSITIONS,
        capital_base: float = DEFAULT_CAPITAL_BASE,
        **params: Any,
    ):
        """初始化鲲鹏策略并校验全部参数。"""
        super().__init__(**params)

        # ── 量能模块参数校验与赋值 ──
        if vol_count_window < 1:
            raise ValueError(f"vol_count_window({vol_count_window}) 必须>=1")
        if not 1 <= vol_active_days <= vol_count_window:
            raise ValueError(
                f"vol_active_days({vol_active_days}) 必须在 [1, vol_count_window={vol_count_window}]"
            )
        if vol_ratio_threshold <= 0:
            raise ValueError(f"vol_ratio_threshold({vol_ratio_threshold}) 必须>0")
        if barslast_vol_window < 1 or barslast_ratio_window < 1:
            raise ValueError("barslast_vol_window/barslast_ratio_window 必须>=1")
        self.vol_ratio_threshold: float = float(vol_ratio_threshold)
        self.vol_count_window: int = int(vol_count_window)
        self.vol_active_days: int = int(vol_active_days)
        self.barslast_vol_window: int = int(barslast_vol_window)
        self.barslast_ratio_window: int = int(barslast_ratio_window)

        # ── 均线模块 ──
        if ma_sticky_threshold <= 1.0:
            raise ValueError(
                f"ma_sticky_threshold({ma_sticky_threshold}) 必须>1.0（极差比恒>=1）"
            )
        self.ma_sticky_threshold: float = float(ma_sticky_threshold)

        # ── MACD 模块 ──
        if not 0 < diff_dea_ratio < 1:
            raise ValueError(f"diff_dea_ratio({diff_dea_ratio}) 必须在(0,1)")
        if macd_abs_threshold < 0:
            raise ValueError(f"macd_abs_threshold({macd_abs_threshold}) 必须>=0")
        if macd_rise_threshold < 0:
            raise ValueError(f"macd_rise_threshold({macd_rise_threshold}) 必须>=0")
        self.diff_dea_ratio: float = float(diff_dea_ratio)
        self.macd_abs_threshold: float = float(macd_abs_threshold)
        self.macd_rise_threshold: float = float(macd_rise_threshold)

        # ── 出场组：档位映射 + 显式覆盖 ──
        if exit_mode not in EXIT_TIERS:
            raise ValueError(
                f"exit_mode({exit_mode!r}) 非法，可选: {sorted(EXIT_TIERS.keys())}"
            )
        tier_sl, tier_tp, tier_activate, tier_dd = EXIT_TIERS[exit_mode]
        self.exit_mode: str = exit_mode
        self.stop_loss_pct: float = (
            float(stop_loss_pct) if stop_loss_pct is not None else tier_sl
        )
        self.take_profit_pct: float = (
            float(take_profit_pct) if take_profit_pct is not None else tier_tp
        )
        self.trailing_activate_pct: Optional[float] = tier_activate
        self.trailing_drawdown_pct: Optional[float] = tier_dd
        if not 0 < self.stop_loss_pct < 1:
            raise ValueError(f"stop_loss_pct({self.stop_loss_pct}) 必须在(0,1)")
        if not 0 < self.take_profit_pct < 1:
            raise ValueError(f"take_profit_pct({self.take_profit_pct}) 必须在(0,1)")
        if hold_days_cap < 1:
            raise ValueError(f"hold_days_cap({hold_days_cap}) 必须>=1")
        self.hold_days_cap: int = int(hold_days_cap)

        # ── 仓位组 ──
        if not 0 < max_position_pct <= 1.0:
            raise ValueError(f"max_position_pct({max_position_pct}) 必须在(0,1]")
        if max_positions < 1:
            raise ValueError(f"max_positions({max_positions}) 必须>=1")
        if capital_base <= 0:
            raise ValueError(f"capital_base({capital_base}) 必须>0")
        self.max_position_pct: float = float(max_position_pct)
        self.max_positions: int = int(max_positions)
        self.capital_base: float = float(capital_base)

        # ── 运行时状态（多标的、三接口共用）──
        # 每标的持仓状态: {entry_close, peak_close, days_held}
        self._state: Dict[str, Dict[str, Any]] = {}
        # 信号事件日志（6.5）：时间戳/代码/信号类型/触发条件明细/价格/成交量/粘合度
        self.signal_log: List[Dict[str, Any]] = []

        logger.info(
            "鲲鹏策略初始化: exit_mode=%s 止损%.0f%%/止盈%.0f%%/移动止盈%s "
            "持有上限%d日 量比阈值%.1f 粘合度<=%.2f 放量窗口%d/%d日 "
            "BARSLAST<%d/<%d |DIF-DEA|/C<%.3f MACD<%.2f 翻红阈值>%.4f",
            self.exit_mode,
            self.stop_loss_pct * 100,
            self.take_profit_pct * 100,
            f"{self.trailing_activate_pct:.0%}激活/{self.trailing_drawdown_pct:.0%}回撤"
            if self.trailing_activate_pct is not None
            else "无",
            self.hold_days_cap,
            self.vol_ratio_threshold,
            self.ma_sticky_threshold,
            self.vol_active_days,
            self.vol_count_window,
            self.barslast_vol_window,
            self.barslast_ratio_window,
            self.diff_dea_ratio,
            self.macd_abs_threshold,
            self.macd_rise_threshold,
        )

    # ═══════════════════════════════════════════════
    #  生命周期
    # ═══════════════════════════════════════════════

    def on_init(self, context: StrategyContext) -> None:
        """初始化钩子：清空跨运行状态（多轮回测复用同一实例时保证干净）。"""
        self.reset()
        super().on_init(context)

    def reset(self) -> None:
        """重置运行时状态（状态机与信号日志）。"""
        self._state.clear()
        self.signal_log.clear()
        self._signal_count = 0

    # ═══════════════════════════════════════════════
    #  核心指标计算（第4章公式逐行转译）
    # ═══════════════════════════════════════════════

    def _compute_kunpeng(self, df: pd.DataFrame) -> Dict[str, pd.Series]:
        """计算全部鲲鹏指标（通达信公式 100% pandas 向量化转译）。

        Args:
            df: 单标的行情 DataFrame（必须含 close/volume 列，按时间升序）。

        Returns:
            dict，包含以下 Series（与 df 同索引）:
              signal     : 最终信号布尔序列（已 fillna(False)）
              s1         : 起爆点一（鲲鹏S）
              s2         : 起爆点二（鲲鹏X/Y/Z）
              vol_gate   : 近期放量门（鲲鹏I）
              sticky     : 均线粘合度达标（鲲鹏M<=阈值）
              vol_ratio  : 量比（鲲鹏A）
              h_count    : 放量天数（鲲鹏H）
              m_value    : 均线粘合度（鲲鹏M）
              macd_decline: MACD柱连续4日递减（鲲鹏N）
              macd_cross : MACD>阈值且当日转升（鲲鹏O/U，默认阈值0.0001）

        Raises:
            ValueError: df 缺失 close/volume 列时抛出。
        """
        if df is None or df.empty:
            raise ValueError("_compute_kunpeng: df 为空")
        if "收盘价" not in df.columns or "成交量" not in df.columns:
            raise ValueError(
                f"_compute_kunpeng: df 必须含 收盘价/成交量 列，实际列: {list(df.columns)}"
            )

        close = df["收盘价"].astype(float)
        vol = df["成交量"].astype(float)

        # ── 量能模块（本行使用的数据截至 t 时刻）──
        # 鲲鹏A：量比 = 当日成交量 / 前5日（T-5~T-1，不含当日）平均成交量
        # 决策8：前5日均量为0（停牌/新股首日）→ 量比置0（视为无量），不抛异常
        prev_vol_ma5 = vol.shift(1).rolling(5).mean()
        vol_ratio = (vol / prev_vol_ma5).where(prev_vol_ma5 > 0, 0.0)

        # 鲲鹏B~F：5/10/20/30/60 日成交量均线（本行使用的数据截至 t 时刻）
        vol_ma5 = vol.rolling(5).mean()
        vol_ma10 = vol.rolling(10).mean()
        vol_ma20 = vol.rolling(20).mean()
        vol_ma30 = vol.rolling(30).mean()
        vol_ma60 = vol.rolling(60).mean()

        # 鲲鹏G：五档成交量均线的最大值（本行使用的数据截至 t 时刻）
        vol_ma_max = pd.Series(
            np.maximum.reduce([vol_ma5, vol_ma10, vol_ma20, vol_ma30, vol_ma60]),
            index=vol.index,
        )

        # 鲲鹏H：最近 vol_count_window 日内成交量超过 G 的天数（含当日）
        # 决策2：严格滚动窗口，窗口不足当日无信号（min_periods 不放宽）
        h_count = (vol > vol_ma_max).astype(int).rolling(self.vol_count_window).sum()

        # 鲲鹏I：近期放量门（决策1：条件从未发生→NaN→比较恒False）
        bars_since_h = self._bars_since(h_count >= self.vol_active_days)
        bars_since_a = self._bars_since(vol_ratio > self.vol_ratio_threshold)
        vol_gate = (bars_since_h < self.barslast_vol_window) | (
            bars_since_a < self.barslast_ratio_window
        )

        # ── 均线粘合模块（本行使用的数据截至 t 时刻）──
        ma20 = close.rolling(20).mean()
        ma30 = close.rolling(30).mean()
        ma60 = close.rolling(60).mean()
        # 鲲鹏M：20/30/60 收盘均线极差比（决策7：分母 clip(EPSILON) 防除零；
        # 任一条均线 NaN → m NaN → 粘合门 False）
        ma_max = pd.Series(
            np.maximum.reduce([ma20, ma30, ma60]), index=close.index,
        )
        ma_min = pd.Series(
            np.minimum.reduce([ma20, ma30, ma60]), index=close.index,
        ).clip(lower=EPSILON)
        m_value = ma_max / ma_min
        sticky = m_value <= self.ma_sticky_threshold

        # ── MACD 标准计算（本行使用的数据截至 t 时刻；EMA 一律 adjust=False）──
        ema_fast = close.ewm(span=MACD_FAST_PERIOD, adjust=False).mean()
        ema_slow = close.ewm(span=MACD_SLOW_PERIOD, adjust=False).mean()
        diff = ema_fast - ema_slow
        dea = diff.ewm(span=MACD_SIGNAL_PERIOD, adjust=False).mean()
        macd = 2.0 * (diff - dea)

        # 鲲鹏N：MACD柱连续4日递减（T-4~T-1）（本行使用的数据截至 t 时刻）
        macd_decline = (
            (macd.shift(1) < macd.shift(2))
            & (macd.shift(2) < macd.shift(3))
            & (macd.shift(3) < macd.shift(4))
        )
        # 鲲鹏O（≡U）：MACD>阈值（默认0.0001）且当日上穿前值（严格翻红，
        # 排除 605577 8-25 类 DIF=DEA 贴零轴临界假金叉：MACD=+0.000038 微正）
        macd_cross = (macd > self.macd_rise_threshold) & (macd > macd.shift(1))

        # ── 起爆点一（鲲鹏P/Q/R/S）──
        ema5 = close.ewm(span=5, adjust=False).mean()
        ema10 = close.ewm(span=10, adjust=False).mean()
        ema13 = close.ewm(span=EMA13_PERIOD, adjust=False).mean()
        ema30_line = close.ewm(span=30, adjust=False).mean()

        # |DIFF-DEA|/C<阈值（鲲鹏Q/Y 共用）
        macd_tight = np.abs((diff - dea) / close) < self.diff_dea_ratio
        # MACD<阈值（鲲鹏R/Z 共用）
        macd_small = macd < self.macd_abs_threshold

        # 鲲鹏P：三连降转升 + EMA13 上行
        p_cond = macd_decline & macd_cross & (ema13 > ema13.shift(1))
        # 鲲鹏Q：三连降转升 + MACD 粘合
        q_cond = macd_decline & macd_cross & macd_tight
        # 鲲鹏R：三连降转升 + MACD 小幅
        r_cond = macd_decline & macd_cross & macd_small
        # 鲲鹏S：起爆点一
        s1 = p_cond | q_cond | r_cond

        # ── 起爆点二（鲲鹏V/W/X/Y/Z）──
        # 鲲鹏V：EMA30 不降 + 5>10>30 多头排列（COUNT(...,1)=1 即当日条件本身）
        ema30_flat = ema30_line >= ema30_line.shift(1)
        bullish_align = (ema5 > ema10) & (ema10 > ema30_line)
        # 鲲鹏W：收盘站上 EMA30（决策4/14：CROSS OR C>EMA30 简化等价 close>ema30）
        above_ema30 = close > ema30_line
        # 鲲鹏X：T(≡N) AND U(≡O) AND V AND W
        x_cond = macd_decline & macd_cross & ema30_flat & bullish_align & above_ema30
        # 鲲鹏Y：X + MACD 粘合
        y_cond = x_cond & macd_tight
        # 鲲鹏Z：X + MACD 小幅
        z_cond = x_cond & macd_small
        # 起爆点二
        s2 = x_cond | y_cond | z_cond

        # ── 最终信号：NaN 一律视为 False（早期窗口不足/边界无信号）──
        signal = (s1 | s2) & vol_gate & sticky
        signal = signal.fillna(False).astype(bool)

        return {
            "signal": signal,
            "s1": s1.fillna(False).astype(bool),
            "s2": s2.fillna(False).astype(bool),
            "vol_gate": vol_gate.fillna(False).astype(bool),
            "sticky": sticky.fillna(False).astype(bool),
            "vol_ratio": vol_ratio,
            "h_count": h_count,
            "m_value": m_value,
            "macd_decline": macd_decline.fillna(False).astype(bool),
            "macd_cross": macd_cross.fillna(False).astype(bool),
        }

    @staticmethod
    def _bars_since(cond: pd.Series) -> pd.Series:
        """最近一次条件为 True 距今的 K 线根数（通达信 BARSLAST 语义）。

        条件从未发生 → NaN（参与比较恒 False，等价于填充大值 9999，
        对应第5章决策1）。本行使用的数据截至 t 时刻：仅依赖当前及历史行。

        Args:
            cond: 布尔条件序列（与数据索引对齐）。

        Returns:
            距最近一次 True 的根数序列（float，从未发生处为 NaN）。
        """
        arr = cond.to_numpy(dtype=bool)
        positions = np.arange(len(arr), dtype=float)
        last_true = np.where(arr, positions, np.nan)
        last_true_ffill = pd.Series(last_true, index=cond.index).ffill()
        return pd.Series(
            positions - last_true_ffill.to_numpy(dtype=float),
            index=cond.index,
        )

    # ═══════════════════════════════════════════════
    #  出场状态机（三接口共用）
    # ═══════════════════════════════════════════════

    def _evaluate_exit(
        self,
        ret: float,
        days_held: int,
        peak_close: float,
        entry_close: float,
        close: float,
    ) -> Optional[str]:
        """出场四条件检查：止损 / 止盈 / 移动止盈 / 持有上限。

        本行仅依赖入场以来已发生的价格（entry_close/peak_close/close），
        无任何未来数据。

        Args:
            ret: 当前相对入场价的收益率。
            days_held: 已持有交易日数（入场日计0，首个持仓日计1）。
            peak_close: 入场以来最高收盘价。
            entry_close: 入场日收盘价。
            close: 当前收盘价。

        Returns:
            触发条件的描述字符串；未触发返回 None。
        """
        if entry_close <= 0:
            return None
        if ret <= -self.stop_loss_pct:
            return f"stop_loss(-{self.stop_loss_pct:.0%})"
        if ret >= self.take_profit_pct:
            return f"take_profit(+{self.take_profit_pct:.0%})"
        if days_held >= self.hold_days_cap:
            return f"hold_cap({days_held}日)"
        if (
            self.trailing_activate_pct is not None
            and self.trailing_drawdown_pct is not None
            and peak_close / entry_close - 1.0 >= self.trailing_activate_pct
            and close / peak_close - 1.0 <= -self.trailing_drawdown_pct
        ):
            return (
                f"trailing_stop(峰后回撤{close / peak_close - 1.0:.2%},"
                f"激活线+{self.trailing_activate_pct:.0%})"
            )
        return None

    # ═══════════════════════════════════════════════
    #  信号事件日志（6.5）
    # ═══════════════════════════════════════════════

    def _log_signal(
        self,
        symbol: str,
        timestamp: Any,
        price: float,
        volume: float,
        m_value: float,
        s1: bool,
        s2: bool,
        components: Dict[str, bool],
    ) -> None:
        """记录一次入场信号（起爆点一/二 类型 + 触发条件明细）。

        Args:
            symbol: 标的代码。
            timestamp: 信号日时间戳。
            price: 信号日收盘价。
            volume: 信号日成交量。
            m_value: 均线粘合度值（鲲鹏M）。
            s1: 起爆点一是否触发。
            s2: 起爆点二是否触发。
            components: 触发条件明细（vol_gate/sticky/macd_decline/macd_cross 等）。
        """
        signal_type = "起爆点一" if s1 else ("起爆点二" if s2 else "未知")
        record: Dict[str, Any] = {
            "timestamp": str(timestamp),
            "symbol": symbol,
            "signal_type": signal_type,
            "price": float(price),
            "volume": float(volume),
            "ma_sticky": float(m_value) if m_value == m_value else None,
            "components": dict(components),
        }
        self.signal_log.append(record)
        logger.info(
            "鲲鹏信号[%s] %s %s 价格=%.2f 成交量=%.0f 粘合度=%.4f 明细=%s",
            record["timestamp"],
            symbol,
            signal_type,
            record["price"],
            record["volume"],
            record["ma_sticky"] or float("nan"),
            components,
        )

    # ═══════════════════════════════════════════════
    #  event 引擎接口（真值基准）
    # ═══════════════════════════════════════════════

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        """单信号生成（兼容基类抽象方法）。

        单标的简化场景使用；多标的完整入场/出场状态机见 on_bar。
        本行使用的数据截至 t 时刻：按当前 bar 截断后计算信号。
        """
        df = self._slice_upto(context.get_data(bar.symbol), bar.timestamp)
        if df is None or len(df) < WARMUP_BARS:
            return Signal(
                symbol=bar.symbol,
                signal_type=SignalType.HOLD,
                price=float(bar.close),
                reason="warmup_or_no_data",
                strategy_id=self.name,
            )
        try:
            sig = bool(self._compute_kunpeng(df)["signal"].iloc[-1])
        except ValueError as exc:
            logger.warning("鲲鹏 generate_signal 计算失败: %s", exc)
            return Signal(
                symbol=bar.symbol,
                signal_type=SignalType.HOLD,
                price=float(bar.close),
                reason="compute_error",
                strategy_id=self.name,
            )
        if sig:
            return Signal(
                symbol=bar.symbol,
                signal_type=SignalType.BUY,
                strength=1.0,
                price=float(bar.close),
                reason="kunpeng_buy",
                strategy_id=self.name,
            )
        return Signal(
            symbol=bar.symbol,
            signal_type=SignalType.HOLD,
            price=float(bar.close),
            reason="no_signal",
            strategy_id=self.name,
        )

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        """每根 bar 处理（event 引擎）：入场/出场状态机，返回信号列表。

        T 日收盘后判断，订单由引擎在 T+1 日开盘撮合（防未来函数）。
        本行使用的数据截至 t 时刻：按当前 bar 时间戳截断 context.data。
        """
        symbol = bar.symbol
        df = self._slice_upto(context.get_data(symbol), bar.timestamp)
        if df is None or len(df) < WARMUP_BARS:
            return []
        try:
            kp = self._compute_kunpeng(df)
        except ValueError as exc:
            logger.warning("鲲鹏 on_bar 计算失败: %s", exc)
            return []

        sig = bool(kp["signal"].iloc[-1])
        close = float(bar.close)
        position = context.get_position(symbol)
        holding = position is not None and position.quantity > 0
        state = self._state.setdefault(symbol, {})

        # ── 持仓：出场检查（T 日收盘判断，T+1 开盘卖出）──
        if holding:
            days_held = int(state.get("days_held", 0)) + 1
            entry_close = float(
                state.get(
                    "entry_close",
                    float(getattr(position, "avg_cost", close) or close),
                )
            )
            peak_close = max(float(state.get("peak_close", close)), close)
            ret = close / entry_close - 1.0 if entry_close > 0 else 0.0
            exit_reason = self._evaluate_exit(
                ret, days_held, peak_close, entry_close, close
            )
            if exit_reason is not None:
                state.clear()
                self._signal_count += 1
                logger.info(
                    "鲲鹏出场[%s] %s %s 入场价=%.2f 现价=%.2f",
                    self._ts_str(bar.timestamp),
                    symbol,
                    exit_reason,
                    entry_close,
                    close,
                )
                return [
                    Signal(
                        symbol=symbol,
                        signal_type=SignalType.SELL,
                        strength=1.0,
                        price=close,
                        reason=f"kunpeng_exit:{exit_reason}",
                        strategy_id=self.name,
                    )
                ]
            state["days_held"] = days_held
            state["peak_close"] = peak_close
            return []

        # ── 未持仓：入场判断（信号 + 持仓数上限）──
        if sig and self._count_positions(context) < self.max_positions:
            state.clear()
            state["entry_close"] = close
            state["peak_close"] = close
            state["days_held"] = 0
            self._signal_count += 1
            components = {
                "vol_gate": bool(kp["vol_gate"].iloc[-1]),
                "sticky": bool(kp["sticky"].iloc[-1]),
                "s1": bool(kp["s1"].iloc[-1]),
                "s2": bool(kp["s2"].iloc[-1]),
                "macd_decline": bool(kp["macd_decline"].iloc[-1]),
                "macd_cross": bool(kp["macd_cross"].iloc[-1]),
            }
            self._log_signal(
                symbol=symbol,
                timestamp=bar.timestamp,
                price=close,
                volume=float(getattr(bar, "volume", 0.0) or 0.0),
                m_value=float(kp["m_value"].iloc[-1]),
                s1=bool(kp["s1"].iloc[-1]),
                s2=bool(kp["s2"].iloc[-1]),
                components=components,
            )
            return [
                Signal(
                    symbol=symbol,
                    signal_type=SignalType.BUY,
                    strength=1.0,
                    price=close,
                    reason="kunpeng_buy",
                    strategy_id=self.name,
                    extra=components,
                )
            ]
        return []

    def calc_buy_quantity(self, price: float, context: StrategyContext) -> int:
        """event 引擎买入数量：单票 max_position_pct × capital_base 整手取整。

        Args:
            price: 信号日收盘价（成交按 T+1 开盘价，数量按信号日价格估算）。
            context: 策略上下文（未直接使用，保留签名以匹配引擎调用约定）。

        Returns:
            目标股数（int，100 股整数倍）。价格非法返回 0。
        """
        return int(self._target_shares_from_capital(
            self.capital_base, price, self.max_position_pct,
        ))

    # ═══════════════════════════════════════════════
    #  mixed 引擎接口（生产主力，目标持仓模式）
    # ═══════════════════════════════════════════════

    def generate_mixed_loop(
        self,
        bar: Bar,
        context: StrategyContext,
        position: Optional[Position],
    ) -> float:
        """mixed 引擎目标持仓：返回截至 T 日收盘决定的目标持仓股数。

        订单由引擎在 T+1 日开盘撮合（防未来函数）。
        本行使用的数据截至 t 时刻：按当前 bar 时间戳截断 context.data。

        Args:
            bar: 当前 bar（T 日，已收盘）。
            context: 策略上下文。
            position: 当前持仓（T 日开盘撮合后、T 日收盘决策前的实际持仓）。

        Returns:
            目标持仓股数（0 = 空仓）。持仓中无出场触发时返回当前持仓股数
            （不回补部分成交，对应 V1"涨停买不进则放弃"语义）。
        """
        symbol = bar.symbol
        df = self._slice_upto(context.get_data(symbol), bar.timestamp)
        if df is None or len(df) < WARMUP_BARS:
            return 0.0
        try:
            kp = self._compute_kunpeng(df)
        except ValueError as exc:
            logger.warning("鲲鹏 generate_mixed_loop 计算失败: %s", exc)
            return 0.0

        sig = bool(kp["signal"].iloc[-1])
        close = float(bar.close)
        holding = position is not None and position.quantity > 0
        state = self._state.setdefault(symbol, {})

        # ── 持仓：出场检查 ──
        if holding:
            days_held = int(state.get("days_held", 0)) + 1
            entry_close = float(
                state.get(
                    "entry_close",
                    float(getattr(position, "avg_cost", close) or close),
                )
            )
            peak_close = max(float(state.get("peak_close", close)), close)
            ret = close / entry_close - 1.0 if entry_close > 0 else 0.0
            exit_reason = self._evaluate_exit(
                ret, days_held, peak_close, entry_close, close
            )
            if exit_reason is not None:
                state.clear()
                self._signal_count += 1
                logger.info(
                    "鲲鹏出场[%s] %s %s 入场价=%.2f 现价=%.2f",
                    self._ts_str(bar.timestamp),
                    symbol,
                    exit_reason,
                    entry_close,
                    close,
                )
                return 0.0
            state["days_held"] = days_held
            state["peak_close"] = peak_close
            return float(position.quantity)

        # ── 未持仓：入场判断 ──
        if sig and self._count_positions(context) < self.max_positions:
            state.clear()
            state["entry_close"] = close
            state["peak_close"] = close
            state["days_held"] = 0
            self._signal_count += 1
            components = {
                "vol_gate": bool(kp["vol_gate"].iloc[-1]),
                "sticky": bool(kp["sticky"].iloc[-1]),
                "s1": bool(kp["s1"].iloc[-1]),
                "s2": bool(kp["s2"].iloc[-1]),
                "macd_decline": bool(kp["macd_decline"].iloc[-1]),
                "macd_cross": bool(kp["macd_cross"].iloc[-1]),
            }
            self._log_signal(
                symbol=symbol,
                timestamp=bar.timestamp,
                price=close,
                volume=float(getattr(bar, "volume", 0.0) or 0.0),
                m_value=float(kp["m_value"].iloc[-1]),
                s1=bool(kp["s1"].iloc[-1]),
                s2=bool(kp["s2"].iloc[-1]),
                components=components,
            )
            return self._target_shares_from_capital(
                self.capital_base, close, self.max_position_pct,
            )
        return 0.0

    # ═══════════════════════════════════════════════
    #  vectorized 引擎接口（纯向量化，参数扫描）
    # ═══════════════════════════════════════════════

    def generate_signals_vectorized(self, data: pd.DataFrame) -> np.ndarray:
        """vectorized 引擎信号：目标持仓股数数组（状态机，报告6.1伪代码）。

        T 日元素表示 T 日收盘决定的目标持仓，由引擎在 T+1 日开盘撮合。
        本行使用的数据截至 t 时刻：第 i 根仅依赖 [0, i] 区间的数据。

        Args:
            data: 单标的行情 DataFrame（open/high/low/close/volume，时间升序）。

        Returns:
            目标持仓股数数组（numpy.ndarray，长度 = len(data)）。
        """
        n = len(data)
        if n < WARMUP_BARS:
            return np.zeros(n)

        close = data["收盘价"].astype(float)
        close_np = close.to_numpy(dtype=float)
        try:
            kp = self._compute_kunpeng(data)
        except ValueError as exc:
            logger.warning("鲲鹏 generate_signals_vectorized 计算失败: %s", exc)
            return np.zeros(n)
        sig = kp["signal"].to_numpy(dtype=bool)

        target = np.zeros(n)
        holding = False
        entry_close = 0.0
        entry_day = 0
        peak_close = 0.0
        full_shares = 0.0

        for i in range(n):
            # 本行使用的数据截至 t 时刻（第 i 根，含第 i 根）
            c = float(close_np[i])
            if holding:
                peak_close = max(peak_close, c)
                ret = c / entry_close - 1.0 if entry_close > 0 else 0.0
                if self._evaluate_exit(
                    ret, i - entry_day, peak_close, entry_close, c
                ) is not None:
                    holding = False
                    full_shares = 0.0
            if not holding and i >= WARMUP_BARS - 1 and bool(sig[i]):
                holding = True
                entry_close = c
                entry_day = i
                peak_close = c
                full_shares = self._target_shares_from_capital(
                    self.capital_base, c, self.max_position_pct,
                )
                self._signal_count += 1
                self._log_signal(
                    symbol=str(data.index[i].date())
                    if hasattr(data.index[i], "date")
                    else str(i),
                    timestamp=data.index[i],
                    price=c,
                    volume=float(data["成交量"].iloc[i]),
                    m_value=float(kp["m_value"].iloc[i]),
                    s1=bool(kp["s1"].iloc[i]),
                    s2=bool(kp["s2"].iloc[i]),
                    components={
                        "vol_gate": bool(kp["vol_gate"].iloc[i]),
                        "sticky": bool(kp["sticky"].iloc[i]),
                        "macd_decline": bool(kp["macd_decline"].iloc[i]),
                        "macd_cross": bool(kp["macd_cross"].iloc[i]),
                    },
                )
            if holding:
                target[i] = full_shares
        return target

    # ═══════════════════════════════════════════════
    #  辅助方法
    # ═══════════════════════════════════════════════

    @staticmethod
    def _slice_upto(
        df: Optional[pd.DataFrame], ts: Optional[Any]
    ) -> Optional[pd.DataFrame]:
        """按当前 bar 时间戳截断数据（防未来铁律）。

        回测中 context.data 为全量历史，若不截断会读到当前 bar 之后的
        未来价格；实盘增量数据下数据只含已收盘 bar，ts 截断为无操作。

        Args:
            df: 全量行情 DataFrame。
            ts: 当前 bar 时间戳（秒级）。

        Returns:
            截断到 ts 的 DataFrame；输入为空返回 None。
        """
        if df is None or df.empty:
            return None
        if ts is not None:
            try:
                cutoff = pd.Timestamp(ts, unit="s")
                df = df[df.index <= cutoff]
            except Exception as exc:
                # 时间戳无法解析时退回全量（由上层数据保证无未来），记录 debug
                logger.debug(
                    "鲲鹏时间戳截断失败，退回全量: ts=%s, err=%s", ts, exc
                )
        return df if len(df) > 0 else None

    @staticmethod
    def _ts_str(ts: Any) -> str:
        """时间戳转字符串（日志用）。"""
        try:
            return str(pd.Timestamp(ts, unit="s"))
        except Exception:
            return str(ts)

    def _count_positions(self, context: StrategyContext) -> int:
        """统计当前持仓标的数（用于 max_positions 上限检查）。"""
        if context.portfolio is None:
            return 0
        raw = getattr(context.portfolio, "positions", None)
        if raw is None:
            return 0
        if isinstance(raw, dict):
            return sum(
                1 for pos in raw.values() if pos is not None and pos.quantity > 0
            )
        count = 0
        try:
            for pos in raw:
                if pos is not None and getattr(pos, "quantity", 0) > 0:
                    count += 1
        except TypeError:
            return 0
        return count


if __name__ == "__main__":
    # ── 自测 Demo：构造"长期横盘→浅回调→放量回升"K线，验证三接口跑通 ──
    # 依赖清单：numpy / pandas（项目既有依赖，见 pyproject.toml）
    from myquant.backtest.engine import run_backtest
    from myquant.models.backtest import BacktestConfig

    # 收盘价构造：70 根横盘(10.0) → 4 根上升(MACD转正) → 4 根浅回调(MACD 4日递减)
    # → 1 根放量回升(信号日) → 拉升触发止盈
    closes_list: List[float] = [10.0] * 70
    closes_list += [10.02, 10.05, 10.09, 10.14]
    closes_list += [10.12, 10.10, 10.09, 10.085]
    closes_list += [10.10, 10.30, 10.60, 11.00, 11.30, 11.60, 11.65]
    n_bars = len(closes_list)
    date_idx = pd.date_range("2024-01-01", periods=n_bars, freq="B")

    demo_close = np.asarray(closes_list, dtype=float)
    demo_volume = np.full(n_bars, 1.0e6)
    demo_volume[78:] = 6.0e6  # 信号日(索引78)起放量，触发量比>4.5
    demo_df = pd.DataFrame(
        {
            "open": demo_close * 0.995,
            "high": demo_close * 1.02,
            "low": demo_close * 0.98,
            "close": demo_close,
            "volume": demo_volume,
        },
        index=date_idx,
    )
    demo_data = {"600519.SH": demo_df}

    strategy = KunpengStrategy()
    kp_result = strategy._compute_kunpeng(demo_df)
    signal_days = int(kp_result["signal"].sum())
    print(f"KunpengStrategy 信号日数量: {signal_days}（构造数据应在索引78附近）")
    assert signal_days >= 1, "构造数据应至少产生1个信号（验证公式转译）"
    assert strategy.supports_mode("event")
    assert strategy.supports_mode("mixed")
    assert strategy.supports_mode("vectorized")

    demo_config = BacktestConfig(
        symbols=["600519.SH"],
        start_date=str(date_idx[0].date()),
        end_date=str(date_idx[-1].date()),
        initial_capital=1_000_000.0,
    )
    for mode in ("event", "mixed", "vectorized"):
        result = run_backtest(demo_config, strategy, data=demo_data, mode=mode)
        assert result.is_success, f"kunpeng/{mode} 失败: {result.status}"
        print(
            f"  KunpengStrategy [{mode}] 净值点数={len(result.equity_curve)} "
            f"成交={len(result.trades)} 期末净值={result.final_equity:.4f}"
        )
    print("KunpengStrategy 三接口自测通过 ✅")
