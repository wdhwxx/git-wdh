# 文件名：myquant/indicators/tradability.py
"""
tradability.py - F-11 可交易性过滤（共享过滤函数）
====================================================
因子层/特征层统一的可交易性过滤（F-11 整改），供两条管线共享同一过滤函数：

  1. 多因子策略（myquant/strategies/multi_factor_strategy.py）：调仓候选收集后、
     因子计算/选层前剔除不可交易标的
  2. ML 特征层（myquant/ml/features.py）：特征/标签计算前行级标记，
     不可交易行特征+标签置 NaN（保行对齐，训练侧 dropna 自动剔除）

过滤规则（每条规则独立开关；数据列缺失时自动跳过该规则——防御式零变化）：
  - 当日停牌   ：is_suspended（或"停牌"）标记列 == 1 视为停牌不可交易
  - 流动性     ：近 N 日（默认 20）日均成交额 <= 阈值（默认 500 万元，
                 报告初始建议、按样本校准）视为流动性枯竭不可交易
  - 一字涨跌停 ：当日 开盘价==最高价==最低价==收盘价（一字板）且
                 |涨跌幅| >= 幅度阈值（默认 9.8%，百分比口径，10% 涨停留容差）
                 视为无法成交不可交易

【防未来函数】均额用 pandas rolling（min_periods 防御）只消费 <=t 的成交额；
一字板判定用当日 OHLC 与涨跌幅（t 时点收盘后已可得），无任何未来引用。

对外接口：
  - TradabilityConfig   过滤配置 dataclass（含参数防御 + from_dict 解析）
  - filter_tradable(df, config) -> (mask, stats)
      mask  : bool Series（True=可交易），index 与 df 对齐
      stats : 拒因统计（逐规则剔除数 + total + 规则跳过说明），供验收对比入档
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 默认阈值（报告 F-11 初始建议，按样本校准可调）──
DEFAULT_MIN_AVG_AMOUNT: float = 5_000_000.0  # 近 20 日均成交额阈值（元）：500 万
DEFAULT_LOOKBACK_DAYS: int = 20              # 均额回看窗口（交易日）
DEFAULT_LIMIT_PCT: float = 9.8               # 一字涨跌停幅度阈值（%），10% 涨停留 0.2 容差
_DEF_MIN_PERIODS: int = 5                    # 均额窗口最小有效期数（低于则不判，防早期误杀）
_EQ_TOL: float = 1e-6                        # OHLC 相等容差（浮点比较）

# ── 中文列契约（与 data_loader EN→CN 映射 / features 契约一致）──
COL_OPEN: str = "开盘价"
COL_HIGH: str = "最高价"
COL_LOW: str = "最低价"
COL_CLOSE: str = "收盘价"
COL_AMOUNT: str = "成交额"
COL_PCT_CHG: str = "涨跌幅"  # 百分比口径（(close_t-close_{t-1})/close_{t-1}*100）
# 停牌标记列别名（DB schema 为 is_suspended；processed 若转中文则用"停牌"）
SUSPEND_COLUMN_ALIASES: Tuple[str, ...] = ("is_suspended", "停牌")


@dataclass
class TradabilityConfig:
    """F-11 可交易性过滤配置。

    Attributes
    ----------
    min_avg_amount : float
        近 N 日均成交额阈值（元），默认 500 万（报告初始建议，按样本校准）。
    lookback_days : int
        均额回看窗口（交易日），默认 20。
    limit_pct : float
        一字涨跌停幅度阈值（%），默认 9.8（10% 涨停留 0.2 容差）。
    min_periods : Optional[int]
        均额窗口最小有效期数，默认 None=取 max(5, lookback_days)；
        窗口不足视为无法判定 → 不拒（保守保留，避免回测早期误杀）。
    filter_suspend : bool
        是否启用当日停牌过滤，默认 True。
    filter_liquidity : bool
        是否启用流动性过滤，默认 True。
    filter_limit : bool
        是否启用一字涨跌停过滤，默认 True。
    """

    min_avg_amount: float = DEFAULT_MIN_AVG_AMOUNT
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    limit_pct: float = DEFAULT_LIMIT_PCT
    min_periods: Optional[int] = None
    filter_suspend: bool = True
    filter_liquidity: bool = True
    filter_limit: bool = True

    def __post_init__(self) -> None:
        """防御式校验：非法参数显式报错。"""
        if self.min_avg_amount <= 0:
            raise ValueError(f"min_avg_amount 必须为正，当前: {self.min_avg_amount}")
        if self.lookback_days < 1:
            raise ValueError(f"lookback_days 必须>=1，当前: {self.lookback_days}")
        if self.limit_pct <= 0:
            raise ValueError(f"limit_pct 必须为正（百分比），当前: {self.limit_pct}")
        if self.min_periods is not None and self.min_periods < 1:
            raise ValueError(f"min_periods 必须>=1，当前: {self.min_periods}")

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TradabilityConfig":
        """从 dict 解析（未知键忽略，缺失用默认值）。

        Args:
            data: 配置字典（None 或空 dict → 全部默认值）。

        Returns:
            TradabilityConfig：解析后的配置。

        Raises:
            ValueError: 已知键值非法时由 __post_init__ 抛出。
        """
        if not data:
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


def _one_line_board(df: pd.DataFrame) -> pd.Series:
    """一字板判定：开盘价==最高价==最低价==收盘价（浮点容差）。

    Args:
        df: 中文列契约 DataFrame。

    Returns:
        bool Series：True=一字板；OHLC 任缺/NaN 行返回 False。
    """
    op = df[COL_OPEN].astype(float)
    hi = df[COL_HIGH].astype(float)
    lo = df[COL_LOW].astype(float)
    cl = df[COL_CLOSE].astype(float)
    equal = (
        (op - hi).abs() <= _EQ_TOL
    ) & (
        (hi - lo).abs() <= _EQ_TOL
    ) & (
        (lo - cl).abs() <= _EQ_TOL
    )
    return equal.fillna(False).astype(bool)


def filter_tradable(
    df: pd.DataFrame,
    config: Optional[TradabilityConfig] = None,
) -> Tuple[pd.Series, Dict[str, Any]]:
    """F-11 共享可交易性过滤（因子管线与 ML 特征层共用）。

    逐行判定是否可交易（True=可交易）。三条规则独立开关、列缺失自动跳过：
      - 停牌   ：停牌标记列 == 1 → 拒
      - 流动性 ：近 lookback_days 日均成交额 <= min_avg_amount → 拒
                 （窗口不足 min_periods → 无法判定，不拒）
      - 一字板 ：OHLC 全等且 |涨跌幅| >= limit_pct → 拒

    【防未来函数】均额 rolling 只含 <=t 的成交额（本行使用的数据截至 t 时刻）；
    一字板判定用 t 时点收盘后已得数据，无任何未来引用。

    Args:
        df: 中文列契约行情 DataFrame（时间升序）。
        config: 过滤配置（None=默认值）。

    Returns:
        Tuple[mask, stats]：
          mask  : bool Series，True=可交易，index 与 df 对齐；
          stats : Dict，含 suspended_rejected / liquidity_rejected /
                  limit_rejected / total_rejected / rules_skipped（验收对比入档）。
    """
    cfg = config if config is not None else TradabilityConfig()
    if not isinstance(df, pd.DataFrame) or df.empty:
        empty = pd.Series(dtype=bool)
        return empty, {
            "suspended_rejected": 0, "liquidity_rejected": 0,
            "limit_rejected": 0, "total_rejected": 0, "rules_skipped": ["输入为空"],
        }

    mask = pd.Series(True, index=df.index, dtype=bool)
    stats: Dict[str, Any] = {
        "suspended_rejected": 0,
        "liquidity_rejected": 0,
        "limit_rejected": 0,
        "total_rejected": 0,
        "rules_skipped": [],
    }

    # ── 1. 当日停牌过滤 ──
    if cfg.filter_suspend:
        suspend_col = next(
            (c for c in SUSPEND_COLUMN_ALIASES if c in df.columns), None
        )
        if suspend_col is not None:
            susp = df[suspend_col].astype(float).fillna(0.0)
            blocked = susp >= 1.0
            stats["suspended_rejected"] = int(blocked.sum())
            mask &= ~blocked
        else:
            stats["rules_skipped"].append("suspend(列缺失，跳过)")
    else:
        stats["rules_skipped"].append("suspend(规则关闭)")

    # ── 2. 流动性过滤（近 N 日均成交额）──
    if cfg.filter_liquidity:
        if COL_AMOUNT in df.columns:
            amt = df[COL_AMOUNT].astype(float)
            minp = cfg.min_periods
            if minp is None:
                minp = max(_DEF_MIN_PERIODS, cfg.lookback_days)
            avg_amount = amt.rolling(cfg.lookback_days, min_periods=minp).mean()
            low_liq = avg_amount <= cfg.min_avg_amount
            low_liq = low_liq.fillna(False).astype(bool)  # 窗口不足/NaN → 不拒
            stats["liquidity_rejected"] = int(low_liq.sum())
            mask &= ~low_liq
        else:
            stats["rules_skipped"].append("liquidity(列缺失，跳过)")
    else:
        stats["rules_skipped"].append("liquidity(规则关闭)")

    # ── 3. 一字涨跌停过滤 ──
    if cfg.filter_limit:
        has_ohlc = all(c in df.columns for c in (COL_OPEN, COL_HIGH, COL_LOW, COL_CLOSE))
        if has_ohlc and COL_PCT_CHG in df.columns:
            one_line = _one_line_board(df)
            pct = df[COL_PCT_CHG].astype(float)
            limit = (pct >= cfg.limit_pct) | (pct <= -cfg.limit_pct)
            limit = limit.fillna(False).astype(bool)
            blocked = one_line & limit
            stats["limit_rejected"] = int(blocked.sum())
            mask &= ~blocked
        else:
            stats["rules_skipped"].append("limit(列缺失，跳过)")
    else:
        stats["rules_skipped"].append("limit(规则关闭)")

    stats["total_rejected"] = int((~mask).sum())
    return mask, stats


def rejected_summary(stats: Dict[str, Any]) -> str:
    """拒因统计摘要（日志/状态展示用）。

    Args:
        stats: filter_tradable 返回的 stats。

    Returns:
        str：人类可读的拒因摘要。
    """
    parts = [
        f"停牌拒{stats['suspended_rejected']}",
        f"流动性拒{stats['liquidity_rejected']}",
        f"一字板拒{stats['limit_rejected']}",
    ]
    if stats.get("rules_skipped"):
        parts.append("跳过:" + ",".join(stats["rules_skipped"]))
    return "，".join(parts) + f"，合计拒{stats['total_rejected']}行"
