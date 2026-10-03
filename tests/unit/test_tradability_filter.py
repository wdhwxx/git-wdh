# 文件名：tests/unit/test_tradability_filter.py
"""
test_tradability_filter.py - F-11 可交易性过滤测试
====================================================
覆盖：
  1. filter_tradable 三规则（停牌/流动性/一字涨跌停）+ 列缺失跳过 + 防未来
  2. TradabilityConfig 参数防御与 from_dict 解析
  3. 特征层（FeatureConfig.tradability）：默认零变化 / 行级 mask 置 NaN /
     build_dataset 自动剔除 / 序列化往返
  4. 策略层（MultiFactorStrategy.tradability）：默认关闭零变化 / 启用后
     选层排除不可交易标的（验收：选层结果中 0 个不可交易标的）/
     get_multi_factor_status 摘要
  5. 拒因统计（rejected_summary）供验收对比入档
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd
import pytest

from myquant.indicators.tradability import (
    DEFAULT_LIMIT_PCT,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MIN_AVG_AMOUNT,
    TradabilityConfig,
    filter_tradable,
    rejected_summary,
)
from myquant.ml.features import FeatureConfig, FeatureEngine
from myquant.models.market_data import Bar, BarFrequency
from myquant.strategies import MultiFactorStrategy, Signal, SignalType, StrategyContext


# ═══════════════════════════════════════════
#  Fixture / 构造工具
# ═══════════════════════════════════════════

def _df_from_closes(closes: List[float], amounts: Optional[List[float]] = None,
                    pct_chg: Optional[List[float]] = None) -> pd.DataFrame:
    """构造标准中文列 DataFrame（直接构造，成交额/成交量完全可控）。

    默认成交额 = 收盘价×2000 万（日均 ~2 亿元，远高于 500 万阈值）；
    成交量带确定性波动（避免 收盘价-成交量 相关为 NaN → vol_price 有效）；
    默认涨跌幅 = 近 1 日涨幅。
    """
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame(index=idx)
    df["开盘价"] = closes
    df["最高价"] = [c * 1.01 for c in closes]
    df["最低价"] = [c * 0.99 for c in closes]
    df["收盘价"] = closes
    df["成交量"] = [100_000.0 + 10_000.0 * np.sin(float(i)) for i in range(n)]
    df["成交额"] = amounts if amounts is not None else [c * 20_000_000.0 for c in closes]
    if pct_chg is not None:
        df["涨跌幅"] = pct_chg
    else:
        prev = pd.Series(closes).shift(1)
        df["涨跌幅"] = ((pd.Series(closes) / prev - 1.0) * 100.0).fillna(0.0).tolist()
    return df


def _one_line_df(closes: List[float], pct: float) -> pd.DataFrame:
    """构造末尾一字板 DataFrame：最后一根 开盘价==最高价==最低价==收盘价。

    一字板判定只看最后一行的 OHLC 相等与涨跌幅，历史行正常 OHLC。
    """
    df = _df_from_closes(closes)
    n = len(df)
    last_close = closes[-1]
    for i in range(3):
        df.iloc[n - 1, df.columns.get_loc("开盘价")] = last_close
        df.iloc[n - 1, df.columns.get_loc("最高价")] = last_close
        df.iloc[n - 1, df.columns.get_loc("最低价")] = last_close
    df["涨跌幅"] = [0.0] * (n - 1) + [pct]
    return df


def _low_liq_df(closes: List[float], tail_amount: float = 100_000.0) -> pd.DataFrame:
    """构造末尾流动性枯竭 DataFrame：后 20 行成交额 = tail_amount（< 500 万）。

    20 行 >= 默认均额窗口 20 → 末尾窗口全枯竭，均额确定下穿阈值。
    """
    amounts = [c * 20_000_000.0 for c in closes]
    for i in range(-20, 0):
        amounts[i] = tail_amount
    return _df_from_closes(closes, amounts=amounts)


def _suspend_df(closes: List[float]) -> pd.DataFrame:
    """构造末尾停牌 DataFrame（is_suspended 列末尾 1 行 = 1）。"""
    df = _df_from_closes(closes)
    df["is_suspended"] = [0] * (len(closes) - 1) + [1]
    return df


def _strategy_bar(symbol: str, close: float) -> Bar:
    """调仓用 Bar（timestamp=0 不做数据截断，测试全量 df 直接参与）。"""
    return Bar(
        symbol=symbol, frequency=BarFrequency.DAILY, timestamp=0.0,
        open=close * 0.995, high=close * 1.015, low=close * 0.985,
        close=close, volume=100_000.0, amount=close * 100_000.0,
    )


# ═══════════════════════════════════════════
#  1. filter_tradable 三规则
# ═══════════════════════════════════════════

def test_filter_all_tradable() -> None:
    """正常标的（无停牌/高成交额/非一字板）→ mask 全 True。"""
    closes = [10.0 + 0.1 * i for i in range(30)]
    df = _df_from_closes(closes)
    mask, stats = filter_tradable(df)
    assert bool(mask.all())
    assert stats["total_rejected"] == 0
    # 停牌标记列缺失 → 停牌规则跳过（预期防御行为）；流动性/一字板规则生效
    assert "suspend(列缺失，跳过)" in stats["rules_skipped"]
    assert "liquidity(列缺失，跳过)" not in stats["rules_skipped"]
    assert "limit(列缺失，跳过)" not in stats["rules_skipped"]


def test_suspend_rule_rejects() -> None:
    """停牌规则：is_suspended==1 行被拒。"""
    closes = [10.0 + 0.1 * i for i in range(30)]
    df = _suspend_df(closes)
    mask, stats = filter_tradable(df)
    assert not bool(mask.iloc[-1])          # 末尾停牌行不可交易
    assert bool(mask.iloc[:-1].all())       # 历史行不受影响
    assert stats["suspended_rejected"] == 1


def test_liquidity_rule_rejects() -> None:
    """流动性规则：近 20 日均额 <= 阈值行被拒（后 20 行枯竭）。"""
    closes = [10.0 + 0.1 * i for i in range(30)]
    df = _low_liq_df(closes)
    mask, stats = filter_tradable(df)
    assert not bool(mask.iloc[-1])          # 末尾行枯竭 → 不可交易
    assert stats["liquidity_rejected"] >= 1


def test_liquidity_window_insufficient_keeps() -> None:
    """流动性窗口不足（有效期数 > 数据行数）→ 无法判定，不拒（保守保留）。"""
    closes = [10.0 + 0.1 * i for i in range(15)]  # 15 行 < min_periods=16
    df = _df_from_closes(closes)                  # 默认高额
    cfg = TradabilityConfig(lookback_days=20, min_periods=16)
    mask, stats = filter_tradable(df, cfg)
    assert bool(mask.all())                  # 全保留
    assert stats["liquidity_rejected"] == 0


def test_limit_up_one_line_rejects() -> None:
    """一字涨停（OHLC 全等 + 涨跌幅>=9.8）→ 拒。"""
    df = _one_line_df([10.0 + 0.1 * i for i in range(30)], pct=9.99)
    mask, stats = filter_tradable(df)
    assert not bool(mask.iloc[-1])
    assert stats["limit_rejected"] == 1
    # 阈值边界：pct=9.8 恰好等于阈值 → 也拒（>=）
    df2 = _one_line_df([10.0 + 0.1 * i for i in range(30)], pct=DEFAULT_LIMIT_PCT)
    mask2, _ = filter_tradable(df2)
    assert not bool(mask2.iloc[-1])


def test_limit_down_one_line_rejects() -> None:
    """一字跌停（OHLC 全等 + 涨跌幅<=-9.8）→ 拒。"""
    df = _one_line_df([10.0 + 0.1 * i for i in range(30)], pct=-9.99)
    mask, stats = filter_tradable(df)
    assert not bool(mask.iloc[-1])
    assert stats["limit_rejected"] == 1


def test_not_one_line_high_pct_keeps() -> None:
    """非一字板大阳线（OHLC 不全等 + 涨跌幅 9.9）→ 不拒（可买，非一字封死）。"""
    closes = [10.0 + 0.1 * i for i in range(30)]
    df = _df_from_closes(closes)
    n = len(df)
    df.iloc[n - 1, df.columns.get_loc("开盘价")] = closes[-1] * 0.98  # 开盘≠收盘 → 非一字
    df["涨跌幅"] = [0.0] * (n - 1) + [9.9]
    mask, stats = filter_tradable(df)
    assert bool(mask.iloc[-1])
    assert stats["limit_rejected"] == 0


def test_column_missing_skip_rule() -> None:
    """列缺失自动跳过该规则：无成交额/无涨跌幅列 → 对应规则跳过、不误杀。"""
    closes = [10.0 + 0.1 * i for i in range(30)]
    df = _df_from_closes(closes)
    df = df.drop(columns=["成交额"])  # 无成交额列
    mask, stats = filter_tradable(df)
    assert bool(mask.all())
    assert "liquidity(列缺失，跳过)" in stats["rules_skipped"]
    assert stats["liquidity_rejected"] == 0


def test_no_lookahead_liquidity() -> None:
    """防未来：后段流动性枯竭不影响前段判定（rolling 因果，仅用 <=t）。"""
    closes = [10.0 + 0.1 * i for i in range(30)]
    df = _low_liq_df(closes)  # 后 15 行枯竭（行 15 起）
    mask, _ = filter_tradable(df)
    # 前 20 行（枯竭前的历史窗口）：均额只含 <=t 高额 → 全部可交易
    assert bool(mask.iloc[:20].all())
    # 枯竭窗口未占满（行 15..25 均额仍被前段高额稀释）→ 仍可交易
    assert bool(mask.iloc[25])


def test_rule_switches() -> None:
    """规则独立开关：关闭停牌规则 → 停牌行保留。"""
    closes = [10.0 + 0.1 * i for i in range(30)]
    df = _suspend_df(closes)
    cfg = TradabilityConfig(filter_suspend=False)
    mask, stats = filter_tradable(df, cfg)
    assert bool(mask.all())                    # 停牌行不再被拒
    assert "suspend(规则关闭)" in stats["rules_skipped"]


def test_rejected_summary_output() -> None:
    """拒因统计摘要可读输出（验收对比入档）。"""
    closes = [10.0 + 0.1 * i for i in range(30)]
    df = _suspend_df(closes)
    _, stats = filter_tradable(df)
    text = rejected_summary(stats)
    assert "停牌拒1" in text and "合计拒1行" in text


# ═══════════════════════════════════════════
#  2. TradabilityConfig 参数防御与解析
# ═══════════════════════════════════════════

def test_config_defaults() -> None:
    """默认值符合报告 F-11 初始建议。"""
    cfg = TradabilityConfig()
    assert cfg.min_avg_amount == DEFAULT_MIN_AVG_AMOUNT          # 500 万
    assert cfg.lookback_days == DEFAULT_LOOKBACK_DAYS            # 20 日
    assert cfg.limit_pct == DEFAULT_LIMIT_PCT                    # 9.8%


def test_config_defense() -> None:
    """非法参数显式报错。"""
    with pytest.raises(ValueError):
        TradabilityConfig(min_avg_amount=0)
    with pytest.raises(ValueError):
        TradabilityConfig(min_avg_amount=-1)
    with pytest.raises(ValueError):
        TradabilityConfig(lookback_days=0)
    with pytest.raises(ValueError):
        TradabilityConfig(limit_pct=0)
    with pytest.raises(ValueError):
        TradabilityConfig(min_periods=0)


def test_config_from_dict() -> None:
    """from_dict：未知键忽略、缺失用默认、非法值报错。"""
    cfg = TradabilityConfig.from_dict({"min_avg_amount": 1_000_000.0, "unknown": 1})
    assert cfg.min_avg_amount == 1_000_000.0
    assert cfg.lookback_days == DEFAULT_LOOKBACK_DAYS
    assert TradabilityConfig.from_dict(None) == TradabilityConfig()
    assert TradabilityConfig.from_dict({}) == TradabilityConfig()
    with pytest.raises(ValueError):
        TradabilityConfig.from_dict({"lookback_days": -1})


# ═══════════════════════════════════════════
#  3. 特征层（FeatureConfig.tradability）
# ═══════════════════════════════════════════

def test_feature_default_zero_change() -> None:
    """无 tradability 配置 → 特征/标签全部有效（现有行为零变化）。"""
    closes = [10.0 + 0.1 * i for i in range(50)]
    df = _df_from_closes(closes)
    engine = FeatureEngine(FeatureConfig(horizon=5, min_periods=5))
    feat = engine.compute_features(df)
    labels = engine.generate_labels(df)
    assert feat.notna().sum().sum() > 0
    assert labels["forward_return"].notna().sum() > 0
    assert "tradability" not in FeatureConfig().to_dict() or \
        FeatureConfig().to_dict().get("tradability") is None


def test_feature_config_roundtrip() -> None:
    """tradability 配置序列化往返（to_dict/from_dict 保留）。"""
    cfg = FeatureConfig(tradability={"min_avg_amount": 3_000_000.0,
                                     "lookback_days": 10})
    d = cfg.to_dict()
    assert d["tradability"] == {"min_avg_amount": 3_000_000.0, "lookback_days": 10}
    cfg2 = FeatureConfig.from_dict(d)
    assert cfg2.tradability == cfg.tradability


def test_feature_mask_nan_and_dropna() -> None:
    """启用 tradability：不可交易行特征+标签置 NaN，build_dataset 自动剔除。"""
    closes = [10.0 + 0.1 * i for i in range(100)]  # > 默认滚动窗口 60
    df = _low_liq_df(closes)  # 后 18 行流动性枯竭
    engine = FeatureEngine(FeatureConfig(
        horizon=5, min_periods=5,
        tradability={"lookback_days": 10, "min_avg_amount": 5_000_000.0,
                     "min_periods": 5},
    ))
    feat = engine.compute_features(df)
    # 后 18 行中均额下穿的行 → 特征 NaN
    assert bool(feat.iloc[-1].isna().all())
    labels = engine.generate_labels(df)
    assert bool(pd.isna(labels.loc[labels.index[-1], "forward_return"]))  # NaN
    # 但行数未删（行级 mask 保对齐）
    assert len(feat) == len(df)
    # build_dataset 自动剔除不可交易样本（仍有可交易样本）
    ds = engine.build_dataset(df)
    assert 0 < ds.n_samples < len(df)


def test_feature_mask_incremental() -> None:
    """实盘增量特征继承过滤：最新行不可交易 → 特征 NaN → 输出 None。"""
    closes = [10.0 + 0.1 * i for i in range(100)]
    df = _low_liq_df(closes)
    engine = FeatureEngine(FeatureConfig(
        horizon=5, min_periods=5,
        tradability={"lookback_days": 10, "min_avg_amount": 5_000_000.0,
                     "min_periods": 5},
    ))
    row = engine.compute_incremental(df)
    assert all(v is None for v in row.values())


# ═══════════════════════════════════════════
#  4. 策略层（MultiFactorStrategy.tradability）
# ═══════════════════════════════════════════

def _multi_df(closes: List[float]) -> pd.DataFrame:
    """标准多因子可跑通的数据（60 根，确定性波动 → low_vol>0 可 z-score）。"""
    return _df_from_closes(closes)


def _mf_context(symbols: List[str], dfs: List[pd.DataFrame]) -> StrategyContext:
    """多标的面板上下文（portfolio=None，买入信号用 get_cash）。"""
    return StrategyContext(
        data={sym: df for sym, df in zip(symbols, dfs)},
        portfolio=None,
        executor=None,
        config={"initial_capital": 1_000_000.0},
    )


def test_strategy_default_zero_change() -> None:
    """tradability 默认 False：状态可见、不剔除任何候选（零变化）。"""
    mf = MultiFactorStrategy(top_n=2, max_positions=5)
    assert mf.tradability is False
    status = mf.get_multi_factor_status()
    assert status["tradability"] is False
    assert status["tradability_rejected_total"] == 0


def test_strategy_rejects_untradable_in_pool() -> None:
    """启用 tradability：选层结果中 0 个不可交易标的（验收）。

    5 标的：A/D/E 正常，B 末尾一字涨停，C 末尾流动性枯竭。
    断言：new_pool 只含正常标的；拒因统计入档。
    """
    closes = [10.0 + 0.05 * i + 0.03 * np.sin(i) for i in range(60)]
    df_a = _multi_df(closes)
    df_d = _multi_df([11.0 + 0.04 * i + 0.05 * np.sin(0.5 * i) for i in range(60)])
    df_e = _multi_df([12.0 + 0.06 * i + 0.04 * np.sin(0.3 * i) for i in range(60)])
    df_b = _one_line_df(closes, pct=9.99)                 # 末尾一字涨停
    df_c = _low_liq_df(closes, tail_amount=10_000.0)      # 末尾流动性枯竭
    ctx = _mf_context(["A", "B", "C", "D", "E"], [df_a, df_b, df_c, df_d, df_e])

    mf = MultiFactorStrategy(
        momentum_period=5, reversal_period=3, volatility_period=10,
        vol_price_period=5, top_n=5, max_positions=5, rebalance_days=1,
        tradability=True,
        tradability_params={"lookback_days": 10, "min_avg_amount": 5_000_000.0,
                            "min_periods": 5},
    )
    mf.on_init(ctx)
    signals = mf.on_bar(_strategy_bar("A", closes[-1]), ctx)

    new_pool = mf.current_pool
    assert "B" not in new_pool          # 一字涨停不可交易 → 绝不入选
    assert "C" not in new_pool          # 流动性枯竭 → 绝不入选
    assert all(s in ("A", "D", "E") for s in new_pool)
    # 验收：选层结果 0 个不可交易标的（对入选标的逐一复检可交易性）
    for sym in new_pool:
        df = ctx.get_data(sym)
        mask, _ = filter_tradable(df, mf._tradability_config)
        assert bool(mask.iloc[-1])
    # 拒因统计入档
    assert mf._tradability_stats["rejected"] == 2
    assert "B" in mf._tradability_stats["by_symbol"]
    assert "C" in mf._tradability_stats["by_symbol"]
    # 状态摘要
    status = mf.get_multi_factor_status()
    assert status["tradability"] is True
    assert status["tradability_rejected_total"] == 2
    assert status["tradability_config"]["min_avg_amount"] == 5_000_000.0
    # 信号中买入标的均为正常标的
    buy_targets = {s.symbol for s in signals if s.signal_type == SignalType.BUY}
    assert buy_targets <= {"A", "D", "E"}


def test_strategy_disabled_keeps_all() -> None:
    """tradability=False（默认）：一字板/低流动性标的仍可入选（零变化对照）。"""
    closes = [10.0 + 0.05 * i + 0.03 * np.sin(i) for i in range(60)]
    df_a = _multi_df(closes)
    df_b = _one_line_df(
        [13.0 + 0.07 * i + 0.03 * np.sin(0.7 * i) for i in range(60)], pct=9.99
    )  # 差异化走势（避免截面 z-score 除零）
    ctx = _mf_context(["A", "B"], [df_a, df_b])
    mf = MultiFactorStrategy(
        momentum_period=5, reversal_period=3, volatility_period=10,
        vol_price_period=5, top_n=2, max_positions=5, rebalance_days=1,
    )
    mf.on_init(ctx)
    mf.on_bar(_strategy_bar("A", closes[-1]), ctx)
    assert len(mf.current_pool) == 2    # B 未被过滤（零变化对照）
    assert mf._tradability_stats["rejected"] == 0


def test_strategy_invalid_params_raises() -> None:
    """tradability_params 非法值构造时显式报错。"""
    with pytest.raises(ValueError):
        MultiFactorStrategy(tradability=True,
                            tradability_params={"min_avg_amount": -1})
