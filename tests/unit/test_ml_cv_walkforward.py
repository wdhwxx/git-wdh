# 文件名：tests/unit/test_ml_cv_walkforward.py
"""
test_ml_cv_walkforward.py - F-12 滚动/扩展窗口 CV + walk-forward 测试
========================================================================
覆盖（22 项）：
  1. 切分防未来：各折 train 时间戳严格先于 valid + 日历缓冲（horizon+1 交易日）
  2. 验证段连续推进、互不重叠；expanding 训练窗单调递增；rolling 固定窗长
  3. 面板整组归属：同一时间戳样本永不跨 train/valid；确定性
  4. CvSplitConfig 默认值 / from_dict 忽略未知键 / 参数防御 / 数据不足显式报错
  5. run_cv 端到端（单标的 + 面板 dict）：4 折 IC 均值/波动/ICIR 手算对拍、
     best/worst 折、不注册 registry、成本化 win_metric="net_return"、
     win_metric 缺失显式报错、to_dict JSON 安全
  6. walk-forward 逐期输出 + 累计净收益曲线 + 稳定性统计手算对拍
  7. 既有 run() 单次训练零变化回归锚

运行：
  pytest tests/unit/test_ml_cv_walkforward.py -v
"""

from __future__ import annotations

import json
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pytest

from myquant.ml.features import (
    CommissionModel,
    FeatureConfig,
    SlippageModel,
    TradeCost,
)
from myquant.ml.model_registry import ModelRegistry
from myquant.ml.training_pipeline import (
    CvFold,
    CvReport,
    CvSplitConfig,
    TrainingPipeline,
    WalkForwardPeriod,
    WalkForwardResult,
    time_series_cv_split,
)
from tests.conftest import make_ohlcv_df


# ═══════════════════════════════════════════════
#  常量 / 合成数据构造
# ═══════════════════════════════════════════════

DAY_SEC: float = 86400.0
BASE_TS: float = 1700000000.0


def _make_ts(n_days: int = 200, n_syms: int = 1) -> Tuple[np.ndarray, np.ndarray]:
    """等间隔交易日时间戳（n_days × n_syms 样本，同时间戳并列）。

    本行使用的数据截至 t 时刻：纯合成时间戳（无真实行情/网络）。
    返回 (timestamps, group_ids)，样本数 = n_days * n_syms。
    """
    if n_days < 1 or n_syms < 1:
        raise ValueError(f"n_days/n_syms 必须 >=1，got: {n_days}/{n_syms}")
    days = np.arange(n_days, dtype=float)
    timestamps = np.repeat(BASE_TS + days * DAY_SEC, n_syms)
    gids = np.tile(np.array([f"s{i}" for i in range(n_syms)]), n_days)
    return timestamps, gids


def _make_df(n: int = 200, seed: int = 42) -> pd.DataFrame:
    """中文列 OHLCV 合成 DataFrame（n 个交易日 + timestamp unix 秒列）。

    本行使用的数据截至 t 时刻：纯合成随机游走（无真实行情/网络）。
    """
    if n < 1:
        raise ValueError(f"n 必须 >=1，got: {n}")
    rng = np.random.default_rng(seed)
    closes = [10.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.normal(0, 0.02)))
    df = make_ohlcv_df(closes=closes)
    df["timestamp"] = df.index.astype("int64") // 10**9
    return df


def _level(ts: np.ndarray, value: float) -> int:
    """value 在唯一时间戳升序中的等级位置（searchsorted left）"""
    return int(np.searchsorted(np.unique(ts), float(value), side="left"))


def _default_cv() -> Dict[str, Any]:
    """run_cv 便捷配置：4 折 / 前段 30% / 每折 20 交易日 / 缓冲 horizon=5"""
    return {
        "n_folds": 4,
        "initial_train_ratio": 0.3,
        "valid_days": 20,
        "horizon": 5,
        "min_train": 10,
        "min_valid": 5,
    }


# ═══════════════════════════════════════════════
#  1. 切分防未来：train 严格先于 valid + 日历缓冲
# ═══════════════════════════════════════════════

def test_cv_fold_train_strictly_before_valid() -> None:
    """每折 train 全部样本时间戳严格早于 valid 全部样本（防未来）。"""
    ts, _ = _make_ts(n_days=200)
    folds = time_series_cv_split(ts, config=CvSplitConfig())
    assert len(folds) == 4
    for fold in folds:
        assert float(np.max(ts[fold.train_idx])) < float(np.min(ts[fold.valid_idx]))


def test_cv_calendar_buffer_no_label_overlap() -> None:
    """日历缓冲：valid 首样本等级 > train 末样本等级 + horizon（标签 0% 重叠）。"""
    ts, _ = _make_ts(n_days=200)
    folds = time_series_cv_split(ts, config=CvSplitConfig(horizon=5))
    for fold in folds:
        train_last_level = _level(ts, fold.train_end_ts)
        valid_first_level = _level(ts, fold.valid_start_ts)
        # 训练末样本标签时点 = train_end + horizon 交易日；
        # valid 首等级必须严格在该时点之后（等级差 >= horizon+1）
        assert valid_first_level >= train_last_level + 5 + 1


def test_cv_valid_windows_contiguous_non_overlap() -> None:
    """验证段连续推进、互不重叠（起点逐折 +valid_days 交易日）。"""
    ts, _ = _make_ts(n_days=200)
    folds = time_series_cv_split(ts, config=CvSplitConfig(valid_days=20))
    prev_end = None
    for fold in folds:
        assert fold.valid_end_ts > fold.valid_start_ts
        if prev_end is not None:
            # 上一折验证段结束早于/等于本折起点（不重叠）
            assert float(fold.valid_start_ts) > float(prev_end)
        prev_end = fold.valid_end_ts
    # 验证段长度 = valid_days 个交易日（等级差）
    for fold in folds:
        assert _level(ts, fold.valid_end_ts) - _level(ts, fold.valid_start_ts) + 1 == 20


def test_cv_expanding_train_grows() -> None:
    """expanding：训练截止逐折单调后移、起点恒为数据起点、n_train 单调不减。"""
    ts, _ = _make_ts(n_days=200)
    folds = time_series_cv_split(ts, config=CvSplitConfig(mode="expanding"))
    first_start = folds[0].train_start_ts
    for i in range(1, len(folds)):
        assert folds[i].train_end_ts > folds[i - 1].train_end_ts
        assert folds[i].train_start_ts == first_start
        assert folds[i].n_train >= folds[i - 1].n_train
    # 首折训练起点 = 数据最早时间戳
    assert folds[0].train_start_ts == float(np.min(ts))


def test_cv_rolling_fixed_window() -> None:
    """rolling：训练窗固定 rolling_window_days 个交易日滑移。"""
    ts, _ = _make_ts(n_days=200)
    cfg = CvSplitConfig(mode="rolling", rolling_window_days=40)
    folds = time_series_cv_split(ts, config=cfg)
    for i in range(1, len(folds)):
        assert folds[i].train_end_ts > folds[i - 1].train_end_ts
    for fold in folds:
        # 训练窗等级跨度 <= rolling_window_days（固定窗，含端点）
        span = _level(ts, fold.train_end_ts) - _level(ts, fold.train_start_ts) + 1
        assert span <= 40


def test_cv_panel_group_integrity() -> None:
    """面板整组归属：同一时间戳的全部标的样本永不跨 train/valid。"""
    ts, gids = _make_ts(n_days=200, n_syms=3)
    folds = time_series_cv_split(ts, group_ids=gids)
    for fold in folds:
        for t in np.unique(ts[fold.valid_idx]):
            group_pos = np.flatnonzero(ts == t)
            assert np.isin(group_pos, fold.valid_idx).all()  # 整组在 valid
        for t in np.unique(ts[fold.train_idx]):
            group_pos = np.flatnonzero(ts == t)
            assert np.isin(group_pos, fold.train_idx).all()  # 整组在 train


def test_cv_split_deterministic() -> None:
    """确定性：同输入两次切分结果一致（索引完全相等）。"""
    ts, gids = _make_ts(n_days=120, n_syms=2)
    a = time_series_cv_split(ts, group_ids=gids, config=CvSplitConfig(n_folds=3))
    b = time_series_cv_split(ts, group_ids=gids, config=CvSplitConfig(n_folds=3))
    for fa, fb in zip(a, b):
        assert np.array_equal(fa.train_idx, fb.train_idx)
        assert np.array_equal(fa.valid_idx, fb.valid_idx)


# ═══════════════════════════════════════════════
#  2. CvSplitConfig：默认值 / from_dict / 防御
# ═══════════════════════════════════════════════

def test_cv_config_defaults() -> None:
    """默认配置：expanding / 4 折 / 前段 30% / 每折 20 交易日 / horizon=5。"""
    cfg = CvSplitConfig()
    assert cfg.mode == "expanding"
    assert cfg.n_folds == 4
    assert cfg.initial_train_ratio == 0.3
    assert cfg.valid_days == 20
    assert cfg.horizon == 5
    assert cfg.min_train == 20
    assert cfg.min_valid == 5
    assert cfg.rolling_window_days == 120


def test_cv_config_from_dict_ignores_unknown() -> None:
    """from_dict：字段透传、未知键忽略（向后兼容配置演进）。"""
    cfg = CvSplitConfig.from_dict(
        {"n_folds": 3, "valid_days": 15, "unknown_key": 1})
    assert cfg.n_folds == 3
    assert cfg.valid_days == 15
    assert cfg.mode == "expanding"  # 未提供 → 默认
    # None / 空 dict → 默认配置
    assert CvSplitConfig.from_dict(None) == CvSplitConfig()
    assert CvSplitConfig.from_dict({}) == CvSplitConfig()


def test_cv_config_defense() -> None:
    """参数防御：非法配置全部显式 ValueError。"""
    with pytest.raises(ValueError):
        CvSplitConfig(mode="random")
    with pytest.raises(ValueError):
        CvSplitConfig(n_folds=0)
    with pytest.raises(ValueError):
        CvSplitConfig(initial_train_ratio=0.0)
    with pytest.raises(ValueError):
        CvSplitConfig(initial_train_ratio=1.5)
    with pytest.raises(ValueError):
        CvSplitConfig(valid_days=0)
    with pytest.raises(ValueError):
        CvSplitConfig(horizon=-1)
    with pytest.raises(ValueError):
        CvSplitConfig(min_train=0)
    with pytest.raises(ValueError):
        CvSplitConfig(min_valid=0)
    with pytest.raises(ValueError):
        CvSplitConfig(mode="rolling", rolling_window_days=0)


def test_cv_split_insufficient_data() -> None:
    """数据不足：验证段越界 / 训练集为空 → 显式报错。"""
    ts, _ = _make_ts(n_days=50)
    # 折数太多 → 验证段越界
    with pytest.raises(ValueError):
        time_series_cv_split(ts, config=CvSplitConfig(n_folds=10))
    # 前段比例太小 + 大缓冲 → 首折训练集为空
    with pytest.raises(ValueError):
        time_series_cv_split(ts, config=CvSplitConfig(
            initial_train_ratio=0.1, horizon=20))
    # 空时间戳
    with pytest.raises(ValueError):
        time_series_cv_split(np.asarray([], dtype=float))
    # 组 id 长度不匹配
    ts2, gids2 = _make_ts(n_days=60, n_syms=2)
    with pytest.raises(ValueError):
        time_series_cv_split(ts2, group_ids=gids2[:-1])


# ═══════════════════════════════════════════════
#  3. run_cv：端到端 + 统计对拍 + 不注册 + 成本化
# ═══════════════════════════════════════════════

def _pipeline(cost: Optional[TradeCost] = None) -> TrainingPipeline:
    # P1-08修复：模型产物写入独立 tmp 目录，不再落盘默认 datasets/models。
    # （原 ModelRegistry() 默认根目录=项目 datasets/models，测试残留模型单副本、
    #  .gitignore 忽略、不在备份清单 → 磁盘损坏即永久丢失。改测tmp比"给残留做备份"更正确。）
    tmp_root = tempfile.mkdtemp(prefix="ml_test_models_")
    return TrainingPipeline(
        FeatureConfig(horizon=5, cost=cost),
        registry=ModelRegistry(models_root=tmp_root),
    )


def _default_cost() -> TradeCost:
    return TradeCost(CommissionModel(), SlippageModel(fixed_bps=2.0))


def test_run_cv_single_series_report() -> None:
    """单标的端到端：4 折、win_metric=ic、折间均值手算对拍、best/worst 折。"""
    df = _make_df(n=200)
    report = _pipeline().run_cv(df, cv_config=_default_cv())
    assert isinstance(report, CvReport)
    assert len(report.folds) == 4
    assert len(report.fold_wins) == 4
    # 每折已评估：metrics 含 ic / max_drawdown
    for fold in report.folds:
        assert "ic" in fold.metrics
        assert "max_drawdown" in fold.metrics
    # 折间均值/波动/ICIR 手算对拍（ddof=1）
    arr = np.asarray(report.fold_wins, dtype=float)
    assert report.win_mean == pytest.approx(float(arr.mean()))
    assert report.win_std == pytest.approx(float(arr.std(ddof=1)))
    assert report.win_icir == pytest.approx(float(arr.mean() / arr.std(ddof=1)))
    # best/worst 折索引正确（按 fold_index 1 起）
    assert report.best_fold in {f.fold_index for f in report.folds}
    assert report.worst_fold in {f.fold_index for f in report.folds}
    assert report.fold_wins[report.best_fold - 1] == float(arr.max())
    assert report.fold_wins[report.worst_fold - 1] == float(arr.min())


def test_run_cv_panel_end_to_end() -> None:
    """面板 dict 端到端：整组切分、每折验证样本 = valid_days × 标的数。"""
    df = _make_df(n=200)
    panel = {"A": df, "B": df}
    report = _pipeline().run_cv(panel, cv_config=_default_cv())
    assert len(report.folds) == 4
    for fold in report.folds:
        assert fold.n_val == 20 * 2  # 20 交易日 × 2 标的
        assert "ic" in fold.metrics


def test_run_cv_not_register() -> None:
    """CV 不注册 registry（选型证据非部署产物，前后模型集合不变）。"""
    df = _make_df(n=200)
    pipe = _pipeline()
    before = set(pipe.registry.list_models())
    pipe.run_cv(df, cv_config=_default_cv())
    after = set(pipe.registry.list_models())
    assert after == before  # CV 未新增任何模型
    assert pipe.registry.get_latest_version("cv_model") is None


def test_run_cv_cost_net_return() -> None:
    """成本化开启：fold.metrics 含 turnover/net_return；win_metric 可取 net_return。"""
    df = _make_df(n=200)
    pipe = _pipeline(cost=_default_cost())
    report = pipe.run_cv(df, cv_config=_default_cv(), win_metric="net_return")
    for fold in report.folds:
        assert "turnover" in fold.metrics
        assert "net_return" in fold.metrics
    assert len(report.fold_wins) == 4  # net_return 逐折可取
    assert report.win_metric == "net_return"


def test_cv_report_missing_win_metric_raises() -> None:
    """win_metric 缺失：CvReport.from_folds 显式 ValueError（不静默兜底）。"""
    folds = [CvFold(
        fold_index=1,
        train_idx=np.array([0, 1]),
        valid_idx=np.array([2]),
        train_start_ts=BASE_TS,
        train_end_ts=BASE_TS + DAY_SEC,
        valid_start_ts=BASE_TS + 2 * DAY_SEC,
        valid_end_ts=BASE_TS + 3 * DAY_SEC,
        n_train=2,
        n_val=1,
        metrics={"ic": 0.1},
    )]
    with pytest.raises(ValueError):
        CvReport.from_folds(folds, win_metric="nonexistent")


def test_cv_report_to_dict_json_safe() -> None:
    """CvReport.to_dict JSON 安全：字段齐全、可 json.dumps、时间字符串化。"""
    df = _make_df(n=200)
    report = _pipeline().run_cv(df, cv_config=_default_cv())
    d = report.to_dict()
    assert d["win_metric"] == "ic"
    assert len(d["fold_wins"]) == 4
    assert len(d["folds"]) == 4
    fold0 = d["folds"][0]
    assert set(fold0) >= {
        "fold_index", "train_start_time", "train_end_time",
        "valid_start_time", "valid_end_time", "n_train", "n_val", "metrics"}
    assert isinstance(fold0["train_start_time"], str)
    json.dumps(d, ensure_ascii=False)  # JSON 可序列化


# ═══════════════════════════════════════════════
#  4. walk-forward：逐期 + 累计曲线 + 统计对拍
# ═══════════════════════════════════════════════

def test_walk_forward_periods_sorted() -> None:
    """walk-forward 逐期输出：期数正确、验证段时间升序、metrics 齐全。"""
    df = _make_df(n=200)
    result = _pipeline().walk_forward(df, cv_config=_default_cv())
    assert isinstance(result, WalkForwardResult)
    assert len(result.periods) == 4
    for i in range(1, len(result.periods)):
        assert result.periods[i].valid_start_ts > result.periods[i - 1].valid_start_ts
    for period in result.periods:
        assert {"ic", "turnover", "net_return", "max_drawdown"} <= set(period.metrics)


def test_walk_forward_cum_net_hand_checked() -> None:
    """累计净收益曲线 = 逐期 net_return 的 cumsum（手算对拍）。"""
    periods = [
        WalkForwardPeriod(
            period_index=i + 1,
            train_start_ts=BASE_TS,
            train_end_ts=BASE_TS + i * DAY_SEC,
            valid_start_ts=BASE_TS + (i + 5) * DAY_SEC,
            valid_end_ts=BASE_TS + (i + 5 + 20) * DAY_SEC,
            n_train=50,
            n_val=20,
            metrics={"ic": 0.1 + 0.05 * i, "net_return": 0.1 * (i + 1)},
        )
        for i in range(3)
    ]
    result = WalkForwardResult.from_periods(periods, win_metric="net_return")
    # cum_net = [0.1, 0.3, 0.6]；net_total = 末值
    assert result.cum_net == pytest.approx([0.1, 0.3, 0.6])
    assert result.net_total == pytest.approx(0.6)


def test_walk_forward_stats_hand_checked() -> None:
    """稳定性统计手算对拍：ic 均值/波动/ICIR/正 IC 占比/累计回撤。"""
    periods = [
        WalkForwardPeriod(
            period_index=1,
            train_start_ts=BASE_TS,
            train_end_ts=BASE_TS + DAY_SEC,
            valid_start_ts=BASE_TS + 6 * DAY_SEC,
            valid_end_ts=BASE_TS + 26 * DAY_SEC,
            n_train=50,
            n_val=20,
            metrics={"ic": 0.2, "net_return": 0.1},
        ),
        WalkForwardPeriod(
            period_index=2,
            train_start_ts=BASE_TS,
            train_end_ts=BASE_TS + 2 * DAY_SEC,
            valid_start_ts=BASE_TS + 7 * DAY_SEC,
            valid_end_ts=BASE_TS + 27 * DAY_SEC,
            n_train=50,
            n_val=20,
            metrics={"ic": 0.4, "net_return": 0.2},
        ),
        WalkForwardPeriod(
            period_index=3,
            train_start_ts=BASE_TS,
            train_end_ts=BASE_TS + 3 * DAY_SEC,
            valid_start_ts=BASE_TS + 8 * DAY_SEC,
            valid_end_ts=BASE_TS + 28 * DAY_SEC,
            n_train=50,
            n_val=20,
            metrics={"ic": -0.1, "net_return": -0.2},
        ),
    ]
    result = WalkForwardResult.from_periods(periods, win_metric="net_return")
    arr = np.asarray([0.2, 0.4, -0.1], dtype=float)
    assert result.ic_mean == pytest.approx(float(arr.mean()))  # 0.1667
    assert result.ic_std == pytest.approx(float(arr.std(ddof=1)))
    assert result.ic_icir == pytest.approx(
        float(arr.mean() / arr.std(ddof=1)))
    assert result.positive_ic_ratio == pytest.approx(2 / 3)
    # 累计净值 [0.1, 0.3, 0.1] → 峰值 0.3 → 最大回撤 0.2
    assert result.max_drawdown == pytest.approx(0.2)
    assert result.cum_net == pytest.approx([0.1, 0.3, 0.1])
    assert result.net_total == pytest.approx(0.1)


def test_walk_forward_no_cost_net_is_gross() -> None:
    """无 cost 时 roundtrip=0 → net_return 恒可输出（稳定性曲线不依赖成本开关）。"""
    df = _make_df(n=200)
    result = _pipeline().walk_forward(df, cv_config=_default_cv())
    assert len(result.cum_net) == 4  # 累计曲线恒存在
    for period in result.periods:
        assert "net_return" in period.metrics
    # to_dict JSON 安全（曲线入档）
    d = result.to_dict()
    assert len(d["cum_net"]) == 4
    assert len(d["periods"]) == 4
    json.dumps(d, ensure_ascii=False)


# ═══════════════════════════════════════════════
#  5. 既有 run() 单次训练零变化回归锚
# ═══════════════════════════════════════════════

def test_run_still_works_unchanged() -> None:
    """回归锚：F-12 只增不改——run() 单次训练注册闭环仍正常。"""
    df = _make_df(n=200)
    pipe = _pipeline()
    result = pipe.run(df, "cv_reg_anchor")
    assert result.n_train > 0
    assert result.n_val > 0
    assert result.metrics  # 评估指标存在
    assert pipe.registry.get_latest_version("cv_reg_anchor") is not None
