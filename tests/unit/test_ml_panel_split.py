# 文件名：tests/unit/test_ml_panel_split.py
"""
test_ml_panel_split.py - F-08 面板数据切分日历化测试
================================================================
覆盖《因子与机器学习层优化实施报告》阶段二 P1 第二项 F-08（回写#95）：

  1. 面板切分：同一时间戳整组确定性归属（同时间戳样本永不跨 train/valid 边界）
  2. 日历化缓冲：验证集起点 = 训练集最后时间戳之后第 (horizon+1) 个交易日
     （口径对齐 F-06 单标的实测缓冲，保证训练末样本标签时点严格早于验证集）
  3. 验收核心：面板数据下，训练集末尾样本的未来 horizon 收益 0% 落入验证集；
     负向对照（按行数跳 horizon 的旧语义）可检出标签重叠
  4. 参数防御 / 确定性 / 面板构建合并 / 端到端 dict→run 注册 / 单标的零变化

运行方式:
  pytest tests/unit/test_ml_panel_split.py -v

设计原则:
  - 全部合成数据（确定性随机游走防未来，零网络）
  - 独立临时 registry（不污染真实 datasets/models）
  - 面板时间戳等间隔交易日（86400s），断言可手工复算
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import pytest

from myquant.ml.features import FeatureConfig, FeatureEngine, PanelDataset
from myquant.ml.model_registry import ModelRegistry
from myquant.ml.training_pipeline import (
    TrainingPipeline,
    panel_time_series_split,
    time_series_split,
)

from tests.conftest import make_ohlcv_df


# ═══════════════════════════════════════════════
#  常量 / 合成数据构造
# ═══════════════════════════════════════════════

DAY_SEC: float = 86400.0
BASE_TS: float = 1700000000.0


def _make_panel(n_days: int = 100, n_syms: int = 2) -> Tuple[np.ndarray, np.ndarray]:
    """等间隔交易日面板：每交易日 n_syms 个标的样本（同时间戳并列）。

    本行使用的数据截至 t 时刻：纯合成时间戳（无真实行情/网络）。
    返回 (timestamps, group_ids)，样本数 = n_days * n_syms。
    """
    if n_days < 1 or n_syms < 1:
        raise ValueError(f"n_days/n_syms 必须 >=1，got: {n_days}/{n_syms}")
    days = np.arange(n_days, dtype=float)
    timestamps = np.repeat(BASE_TS + days * DAY_SEC, n_syms)
    gids = np.tile(np.array([f"s{i}" for i in range(n_syms)]), n_days)
    return timestamps, gids


def _synthetic_df(n: int, seed: int = 42) -> pd.DataFrame:
    """中文列 OHLCV 合成 DataFrame（n 个交易日 + timestamp unix 秒列）。

    本行使用的数据截至 t 时刻：纯合成随机游走（无真实行情/网络）。
    """
    if n < 1:
        raise ValueError(f"n 必须 >=1，got: {n}")
    np.random.seed(seed)
    closes = [10.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + np.random.normal(0, 0.02)))
    df = make_ohlcv_df(closes=closes)
    df["timestamp"] = df.index.astype("int64") // 10**9
    return df


# ═══════════════════════════════════════════════
#  1. 整组确定性归属
# ═══════════════════════════════════════════════

def test_panel_split_group_integrity() -> None:
    """同一时间戳的全部标的样本整组归属：train/valid 时间戳集合不相交。"""
    ts, gids = _make_panel(n_days=100, n_syms=3)
    train_idx, valid_idx = panel_time_series_split(ts, gids, train_ratio=0.8, horizon=5)
    train_ts = set(ts[train_idx].tolist())
    valid_ts = set(ts[valid_idx].tolist())
    assert not (train_ts & valid_ts)  # 无同一时间戳跨两侧
    # 任一 valid 样本所在时间戳组：同组样本要么全在 train 要么全在 valid
    for t in np.unique(ts[valid_idx]):
        group_pos = np.flatnonzero(ts == t)
        assert np.isin(group_pos, valid_idx).all()  # 整组在 valid
    for t in np.unique(ts[train_idx]):
        group_pos = np.flatnonzero(ts == t)
        assert np.isin(group_pos, train_idx).all()  # 整组在 train


def test_panel_split_repeated_timestamp_all_same_side() -> None:
    """重复时间戳多标的：同时间戳样本永不被切分到两侧（确定性归属）。"""
    ts, gids = _make_panel(n_days=60, n_syms=4)
    train_idx, valid_idx = panel_time_series_split(ts, gids, train_ratio=0.7, horizon=3)
    combined = set(train_idx.tolist()) & set(valid_idx.tolist())
    assert not combined  # 位置索引不重叠
    for t in np.unique(ts):
        pos = np.flatnonzero(ts == t)
        in_train = np.isin(pos, train_idx)
        in_valid = np.isin(pos, valid_idx)
        assert not (in_train.any() and in_valid.any())  # 同组不同侧 → 失败


# ═══════════════════════════════════════════════
#  2. 日历化缓冲
# ═══════════════════════════════════════════════

def test_panel_split_calendar_buffer() -> None:
    """日历缓冲：valid 首样本时点 - train 末样本时点 >= (horizon+1) 个交易日。"""
    ts, gids = _make_panel(n_days=100, n_syms=2)
    train_idx, valid_idx = panel_time_series_split(ts, gids, train_ratio=0.8, horizon=5)
    train_last = float(np.max(ts[train_idx]))
    valid_first = float(np.min(ts[valid_idx]))
    gap_days = (valid_first - train_last) / DAY_SEC
    assert gap_days >= 5 + 1  # horizon+1 = 6（口径对齐 F-06 单标的）


def test_panel_split_calendar_not_rows() -> None:
    """日历化按交易日刻度而非行数：train_end 为整组时刻，valid 首 =
    t_levels[k + horizon + 1]（k = train_end 组在交易日序列中的位置）。"""
    ts, gids = _make_panel(n_days=100, n_syms=2)
    train_idx, valid_idx = panel_time_series_split(ts, gids, train_ratio=0.8, horizon=5)
    levels = np.unique(ts)
    train_last = float(np.max(ts[train_idx]))
    valid_first = float(np.min(ts[valid_idx]))
    k = int(np.searchsorted(levels, train_last, side="right")) - 1  # train_end 组位置
    assert float(levels[k]) == train_last
    assert valid_first == float(levels[k + 5 + 1])  # 日历化缓冲，与行数无关


# ═══════════════════════════════════════════════
#  3. 验收核心：0% 标签落入验证集 + 负向对照
# ═══════════════════════════════════════════════

def test_panel_acceptance_zero_label_overlap() -> None:
    """验收核心：训练集末尾样本的未来 horizon 收益 0% 落入验证集。

    训练末样本标签时点（train_last + horizon 个交易日收盘价）严格早于
    验证集首个样本时点 → 标签计算窗口不落入验证集时段。
    """
    ts, gids = _make_panel(n_days=120, n_syms=2)
    horizon = 5
    train_idx, valid_idx = panel_time_series_split(ts, gids, train_ratio=0.8, horizon=horizon)
    train_last = float(np.max(ts[train_idx]))
    label_ts = train_last + horizon * DAY_SEC  # 训练末样本的标签时点
    valid_first = float(np.min(ts[valid_idx]))
    assert label_ts < valid_first  # 0% 落入：标签时点严格早于验证集首个样本


def test_panel_split_negative_row_buffer_overlap() -> None:
    """负向对照：按行数跳 horizon 的旧语义（非日历化）在同一面板上
    可检出标签重叠——证明日历化缓冲是必要的。"""
    ts, gids = _make_panel(n_days=100, n_syms=2)
    horizon = 5
    n = len(ts)
    order = np.argsort(ts, kind="stable")
    naive_train_end = int(n * 0.8)
    naive_valid = order[naive_train_end + horizon:]  # 旧语义：跳 horizon 行
    naive_valid_first = float(np.min(ts[naive_valid]))
    train_last = float(np.max(ts[order[:naive_train_end]]))
    label_ts = train_last + horizon * DAY_SEC
    assert naive_valid_first <= label_ts  # 旧语义下标签时点落入验证集时段


# ═══════════════════════════════════════════════
#  4. 参数防御 / 确定性
# ═══════════════════════════════════════════════

def test_panel_split_parameter_defense() -> None:
    """非法参数显式报错（防御式编程）。"""
    ts, gids = _make_panel(n_days=50, n_syms=2)
    with pytest.raises(ValueError, match="时间戳为空"):
        panel_time_series_split([], [], train_ratio=0.8)
    with pytest.raises(ValueError, match="group_ids 长度"):
        panel_time_series_split(ts, gids[:-1], train_ratio=0.8)
    with pytest.raises(ValueError, match="train_ratio"):
        panel_time_series_split(ts, gids, train_ratio=0.0)
    with pytest.raises(ValueError, match="train_ratio"):
        panel_time_series_split(ts, gids, train_ratio=1.0)
    with pytest.raises(ValueError, match="horizon"):
        panel_time_series_split(ts, gids, horizon=-1)
    with pytest.raises(ValueError, match="样本过少"):
        panel_time_series_split(ts, gids, train_ratio=0.8, min_train=1000)
    # 日历缓冲后验证集为空（数据过短）
    short_ts, short_gids = _make_panel(n_days=6, n_syms=2)
    with pytest.raises(ValueError, match="验证集为空"):
        panel_time_series_split(short_ts, short_gids, train_ratio=0.8, horizon=5)


def test_panel_split_deterministic() -> None:
    """同输入两次切分结果完全一致（确定性归属，无随机）。"""
    ts, gids = _make_panel(n_days=80, n_syms=3)
    t1, v1 = panel_time_series_split(ts, gids, train_ratio=0.8, horizon=5)
    t2, v2 = panel_time_series_split(ts, gids, train_ratio=0.8, horizon=5)
    assert np.array_equal(t1, t2) and np.array_equal(v1, v2)


# ═══════════════════════════════════════════════
#  5. 面板构建（build_panel_dataset）
# ═══════════════════════════════════════════════

def test_build_panel_dataset_merges() -> None:
    """面板构建：逐标的合并 X/y/timestamps/group_ids，symbols 正确。"""
    engine = FeatureEngine(FeatureConfig())
    df_a = _synthetic_df(120, seed=1)
    df_b = _synthetic_df(120, seed=2)
    panel = {"A": df_a, "B": df_b}
    ds = engine.build_panel_dataset(panel)
    n_a = engine.build_dataset(df_a).n_samples  # 单标的有效样本（已丢弃窗口不足/无标签尾部）
    n_b = engine.build_dataset(df_b).n_samples
    assert isinstance(ds, PanelDataset)
    assert ds.n_samples == n_a + n_b
    assert ds.symbols == ["A", "B"]
    assert len(ds.timestamps) == ds.n_samples
    assert len(ds.group_ids) == ds.n_samples
    assert ds.group_ids.count("A") == n_a and ds.group_ids.count("B") == n_b
    assert list(ds.feature_columns) == list(
        engine.build_dataset(panel["A"]).feature_columns)


def test_build_panel_dataset_feature_cols_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """各标的特征列不一致 → 显式报错（防御分支：monkeypatch 伪造不同列返回）。"""
    engine = FeatureEngine(FeatureConfig())
    df_a = _synthetic_df(120, seed=1)
    df_b = _synthetic_df(120, seed=2)
    n_a = engine.build_dataset(df_a).n_samples
    n_b = engine.build_dataset(df_b).n_samples

    def fake_build(sub: Any) -> "FeatureDataset":
        from myquant.ml.features import FeatureDataset
        if sub is df_a:
            return FeatureDataset(
                X=pd.DataFrame({"f1": np.ones(n_a)}),
                y=pd.Series(np.full(n_a, 0.01)),
                timestamps=[BASE_TS + i * DAY_SEC for i in range(n_a)],
                feature_columns=["f1"], label_column="forward_return")
        return FeatureDataset(
            X=pd.DataFrame({"f2": np.ones(n_b)}),
            y=pd.Series(np.full(n_b, 0.01)),
            timestamps=[BASE_TS + i * DAY_SEC for i in range(n_b)],
            feature_columns=["f2"], label_column="forward_return")

    monkeypatch.setattr(engine, "build_dataset", fake_build)
    with pytest.raises(ValueError, match="特征列不一致"):
        engine.build_panel_dataset({"A": df_a, "B": df_b})


def test_build_panel_dataset_invalid_inputs() -> None:
    """面板输入防御：非 dict / 空 dict / 全标的样本不足 → 显式报错。"""
    engine = FeatureEngine(FeatureConfig())
    with pytest.raises(ValueError, match="非空 dict"):
        engine.build_panel_dataset([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="非空 dict"):
        engine.build_panel_dataset({})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="无任何有效样本"):
        engine.build_panel_dataset({"A": _synthetic_df(2, seed=1)})  # 特征窗口不足


# ═══════════════════════════════════════════════
#  6. 端到端：dict→run 面板注册 / 单标的零变化
# ═══════════════════════════════════════════════

def test_e2e_panel_run_registers(tmp_path: Any) -> None:
    """端到端：面板 dict → run → 训练注册成功，data_snapshot 含 symbols。"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    cfg = FeatureConfig()
    pipe = TrainingPipeline(feature_config=cfg, registry=registry)
    panel = {"A": _synthetic_df(160, seed=1), "B": _synthetic_df(160, seed=2)}
    result = pipe.run(panel, model_name="panel_model")
    meta = registry.get_model_metadata("panel_model", result.version)
    assert meta.data_snapshot is not None
    assert meta.data_snapshot["symbols"] == ["A", "B"]
    assert meta.data_snapshot["n_train"] + meta.data_snapshot["n_val"] < 160 * 2
    # 常驻绩效（F-07 延续）在面板训练下正常
    assert "max_drawdown" in meta.metrics
    # 面板加载签名链路（无密钥降级模式）正常
    loaded = registry.load_model("panel_model", result.version)
    assert loaded is not None


def test_single_symbol_path_zero_change(tmp_path: Any) -> None:
    """单标的 DataFrame 输入零变化：仍走单标的路径，data_snapshot 无 symbols。"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    cfg = FeatureConfig()
    pipe = TrainingPipeline(feature_config=cfg, registry=registry)
    result = pipe.run(_synthetic_df(160, seed=3), model_name="single_model")
    meta = registry.get_model_metadata("single_model", result.version)
    assert meta.data_snapshot is not None
    assert "symbols" not in meta.data_snapshot  # 单标的省略（零变化）
    assert meta.data_snapshot["n_train"] > 0
