# 文件名：tests/unit/test_ml_e2e.py
"""
test_ml_e2e.py - F-06 ML 端到端与负向测试
=========================================
覆盖《P0整改任务清单》F-06 整改内容 1（tests/unit/test_ml_*.py）：

  1. 特征一致性端到端：合成数据 → FeatureEngine → TrainingPipeline.run
     → InferenceService.predict_realtime 全链路
  2. 时序切分：时间升序、strict_split+horizon 缓冲（验证集起点 ≥ 训练集
     末尾 + horizon）、日历化缓冲负向测试（关闭 strict 时标签窗口重叠可检出）、
     参数防御（train_ratio 越界 / horizon 负 / 数据不足）
  3. 标签无泄露：标签 = 未来 horizon 期收益；未来突变负向（t+horizon 突变
     不影响 t 时刻特征）；推理路径不含标签列（"推理绝不生成标签"）
  4. registry 读写：注册/列表/指定版本回滚/checksum 篡改检测/删除
  5. 推理兜底：空输入 / 缺失列 / NaN 特征 / 模型不存在 → 明确错误不崩溃不静默
  6. 未来函数负向用例：样本内预测 strict 拦截 / 非 strict 告警 /
     无 train_end_time 守卫跳过

运行方式:
  pytest tests/unit/test_ml_e2e.py -v

设计原则:
  - 全部合成数据（确定性随机游走防未来，零网络）
  - 独立临时 registry（不污染真实 datasets/models）
  - 复用 myquant.ml 公开 API + tests.conftest.make_ohlcv_df
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import numpy as np
import pandas as pd
import pytest

from myquant.ml.features import FeatureConfig, FeatureEngine
from myquant.ml.inference_service import (
    FeatureMisalignmentError,
    InferenceError,
    InferenceService,
    ModelNotReadyError,
)
from myquant.ml.model_registry import (
    ModelIntegrityError,
    ModelNotFoundError,
    ModelRegistry,
    ModelVersion,
)
from myquant.ml.training_pipeline import (
    TrainingPipeline,
    time_series_split,
)

from tests.conftest import make_ohlcv_df


# ═══════════════════════════════════════════════
#  合成数据（确定性随机游走，防未来）
# ═══════════════════════════════════════════════

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


@pytest.fixture(scope="module")
def trained_env(tmp_path_factory: Any) -> SimpleNamespace:
    """训练注册一次（300 行合成数据），供端到端/兜底/未来函数用例共享。

    registry 位于独立临时目录，不碰真实 datasets/models。
    """
    root = tmp_path_factory.mktemp("ml_e2e")
    registry = ModelRegistry(models_root=str(root / "models"))
    cfg = FeatureConfig()
    pipe = TrainingPipeline(feature_config=cfg, registry=registry)
    data = _synthetic_df(300)
    result = pipe.run(data, model_name="e2e_model")
    svc = InferenceService(registry=registry, feature_config=cfg)
    meta = registry.get_model_info("e2e_model")
    return SimpleNamespace(
        registry=registry,
        svc=svc,
        data=data,
        model_name="e2e_model",
        meta=meta,
        feature_columns=list(result.feature_columns),
    )


# ═══════════════════════════════════════════════
#  1. 特征一致性端到端
# ═══════════════════════════════════════════════

def test_e2e_train_register_predict_realtime(trained_env: SimpleNamespace) -> None:
    """训练→注册→实时推理全链路：签名一致、预测有限、n_samples=1"""
    inf = trained_env.svc.predict_realtime(
        trained_env.data, trained_env.model_name)
    assert inf.n_samples == 1
    assert np.isfinite(inf.predictions).all()
    assert list(inf.feature_columns) == trained_env.feature_columns
    assert inf.model_name == trained_env.model_name
    assert inf.train_end_time is not None  # 防未来守卫有据可依


def test_e2e_feature_signature_roundtrip(trained_env: SimpleNamespace) -> None:
    """注册签名字段与推理实际特征列双向一致"""
    meta = trained_env.meta
    assert meta.feature_signature is not None
    assert meta.feature_signature["columns"] == trained_env.feature_columns
    assert list(meta.feature_columns) == trained_env.feature_columns


# ═══════════════════════════════════════════════
#  2. 时序切分（时间序列，绝不随机）
# ═══════════════════════════════════════════════

def test_time_series_split_ordered() -> None:
    """切分索引时间升序且无重叠：训练在前、验证在后"""
    train, valid = time_series_split(np.arange(100, dtype=float), train_ratio=0.8, strict_split=False)
    assert len(train) == 80 and len(valid) == 20
    assert train[-1] < valid[0]


def test_time_series_split_strict_buffer() -> None:
    """strict_split=True 时验证集起点再向后跳过 horizon 缓冲"""
    train, valid = time_series_split(
        np.arange(100, dtype=float), train_ratio=0.8, strict_split=True, horizon=5)
    assert len(train) == 80
    # 缓冲生效：valid 起点 = 80 + 5
    assert int(valid[0]) == 85
    assert len(valid) == 15


def test_time_series_split_calendar_buffer_negative() -> None:
    """日历化缓冲负向：strict=True 验证集首样本与训练集末样本的
    日历间隔 >= horizon 根 bar；strict=False 时重叠可被检出。"""
    timestamps = [1700000000.0 + 60.0 * i for i in range(100)]
    horizon = 5
    _, valid_strict = time_series_split(
        timestamps, train_ratio=0.8, strict_split=True, horizon=horizon)
    _, valid_loose = time_series_split(
        timestamps, train_ratio=0.8, strict_split=False, horizon=horizon)
    # 严格：验证集首样本时间 ≥ 训练集末尾 + horizon*60 秒（标签窗口不重叠）
    assert valid_strict[0] == 85
    gap_strict = timestamps[int(valid_strict[0])] - timestamps[79]
    assert gap_strict >= horizon * 60.0
    # 非严格：验证集紧贴训练集，标签窗口（未来 horizon 期）落入验证集时段
    gap_loose = timestamps[int(valid_loose[0])] - timestamps[79]
    assert gap_loose < horizon * 60.0


def test_time_series_split_parameter_defense() -> None:
    """非法参数显式报错（防御式编程）"""
    with pytest.raises(ValueError, match="train_ratio"):
        time_series_split(np.arange(100), train_ratio=0.0)
    with pytest.raises(ValueError, match="train_ratio"):
        time_series_split(np.arange(100), train_ratio=1.0)
    with pytest.raises(ValueError, match="train_ratio"):
        time_series_split(np.arange(100), train_ratio=-0.1)
    with pytest.raises(ValueError, match="horizon"):
        time_series_split(np.arange(100), horizon=-1)
    # 训练样本不足 min_train
    with pytest.raises(ValueError, match="训练样本过少"):
        time_series_split(np.arange(3), train_ratio=0.8, min_train=4)
    # 验证集为空（strict 缓冲后越界）
    with pytest.raises(ValueError, match="验证集为空"):
        time_series_split(np.arange(20), train_ratio=0.95, strict_split=True, horizon=5)
    # 验证样本不足 min_valid
    with pytest.raises(ValueError, match="验证集为空"):
        time_series_split(np.arange(10), train_ratio=0.9, min_valid=2)


def test_pipeline_split_strict_via_timestamps(trained_env: SimpleNamespace) -> None:
    """TrainingPipeline.split 集成：基于 dataset.timestamps 的严格切分
    验证集时间全部晚于训练集（防标签重叠）。"""
    dataset = FeatureEngine(FeatureConfig()).build_dataset(trained_env.data)
    train_idx, valid_idx = TrainingPipeline(
        registry=trained_env.registry,
        feature_config=FeatureConfig()).split(dataset, strict_split=True)
    ts = np.asarray(dataset.timestamps, dtype=float)
    assert ts[valid_idx].min() > ts[train_idx].max()


# ═══════════════════════════════════════════════
#  3. 标签无泄露
# ═══════════════════════════════════════════════

def test_dataset_label_is_forward_return(trained_env: SimpleNamespace) -> None:
    """标签 = 未来 horizon 期收益（close.shift(-horizon)/close - 1），
    且特征列不含标签列。"""
    engine = FeatureEngine(FeatureConfig())
    dataset = engine.build_dataset(trained_env.data)
    assert dataset.label_column == "forward_return"
    assert "forward_return" not in dataset.feature_columns
    # 全样本向量断言：y[t] == close[t+horizon]/close[t] - 1（按时间戳精确对齐）
    horizon = FeatureConfig().horizon
    ts_arr = trained_env.data["timestamp"].to_numpy(dtype=float)
    close = trained_env.data["收盘价"].to_numpy(dtype=float)
    ts_ds = np.asarray(dataset.timestamps, dtype=float)
    pos = np.searchsorted(ts_arr, ts_ds)  # 严格递增时间戳 → 精确位置
    expected = close[pos + horizon] / close[pos] - 1.0
    assert np.allclose(dataset.y.to_numpy(dtype=float), expected, atol=1e-9)


def test_features_no_future_leak() -> None:
    """未来突变负向：把 t+horizon 之后（未来）收盘价 ×10，
    t 时刻（及之前）的特征必须纹丝不动（trailing 计算不引用未来）。"""
    df1 = _synthetic_df(160)
    df2 = df1.copy()
    horizon = FeatureConfig().horizon
    df2.loc[df2.index[-horizon:], "收盘价"] *= 10.0
    engine = FeatureEngine(FeatureConfig())
    f1 = engine.compute_features(df1)
    f2 = engine.compute_features(df2)
    # 未来 horizon 行之前的特征逐列完全一致（NaN 位置也一致）
    pd.testing.assert_frame_equal(f1.iloc[:-horizon], f2.iloc[:-horizon])


def test_inference_path_no_label() -> None:
    """推理绝不生成标签：compute_incremental 输出不含标签列"""
    engine = FeatureEngine(FeatureConfig())
    data = _synthetic_df(60)
    feats = engine.compute_incremental(data)
    assert "forward_return" not in feats
    assert "direction" not in feats


# ═══════════════════════════════════════════════
#  4. registry 读写
# ═══════════════════════════════════════════════

def _register_two_versions(tmp_path: Any) -> ModelRegistry:
    """注册 v1/v2 两个版本（模型对象为可 pickle 的 dict）"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    registry.register_model("rw_model", {"tag": "v1"}, description="v1")
    registry.register_model("rw_model", {"tag": "v2"}, description="v2")
    return registry


def test_registry_register_list_load(tmp_path: Any) -> None:
    """注册→列表→加载：版本自增、最新为 active、工件可加载"""
    registry = _register_two_versions(tmp_path)
    assert registry.list_models() == ["rw_model"]
    versions = registry.list_versions("rw_model")
    # 版本列表最新在前（降序）
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["status"] == "active"
    loaded = registry.load_model("rw_model", version=2)
    assert loaded == {"tag": "v2"}


def test_registry_version_rollback(tmp_path: Any) -> None:
    """版本回滚：显式加载旧版本（version=1）"""
    registry = _register_two_versions(tmp_path)
    v1 = registry.load_model("rw_model", version=1)
    assert v1 == {"tag": "v1"}
    # 默认加载最新 active
    assert registry.load_model("rw_model") == {"tag": "v2"}


def test_registry_checksum_tamper_detected(tmp_path: Any) -> None:
    """工件篡改检测：改动 model 工件字节后 load 抛 ModelIntegrityError、
    verify_model 返回 False"""
    registry = _register_two_versions(tmp_path)
    meta = registry.get_model_info("rw_model", version=1)
    apath = registry._artifact_path(meta.model_name, meta.version, meta.artifact)
    assert registry.verify_model("rw_model", version=1)
    # 篡改：追加字节
    with open(apath, "ab") as f:
        f.write(b"TAMPERED")
    assert not registry.verify_model("rw_model", version=1)
    with pytest.raises(ModelIntegrityError, match="完整性校验失败"):
        registry.load_model("rw_model", version=1)
    # 未篡改版本不受影响
    assert registry.load_model("rw_model", version=2) == {"tag": "v2"}


def test_registry_delete_version_and_model(tmp_path: Any) -> None:
    """删除版本 / 删除模型"""
    registry = _register_two_versions(tmp_path)
    assert registry.delete_version("rw_model", version=1)
    versions = registry.list_versions("rw_model")
    assert [v["version"] for v in versions] == [2]
    assert registry.delete_model("rw_model")
    with pytest.raises(ModelNotFoundError):
        registry.get_model_info("rw_model")


# ═══════════════════════════════════════════════
#  5. 推理兜底（明确错误，不崩溃不静默）
# ═══════════════════════════════════════════════

def test_predict_realtime_empty_data(trained_env: SimpleNamespace) -> None:
    """空输入 → 明确错误（无特征可预测）"""
    with pytest.raises(ModelNotReadyError):
        trained_env.svc.predict_realtime([], trained_env.model_name)


def test_predict_realtime_missing_columns(trained_env: SimpleNamespace) -> None:
    """缺失列输入 → 契约错误（拒绝静默兼容）"""
    bad = _synthetic_df(60).drop(columns=["最高价"])
    with pytest.raises((ValueError, FeatureMisalignmentError)):
        trained_env.svc.predict_realtime(bad, trained_env.model_name)


def test_predict_realtime_nan_features(trained_env: SimpleNamespace) -> None:
    """数据不足窗口 → NaN/None 特征 → 明确错误（非 TypeError）"""
    tiny = _synthetic_df(10)  # sma_20 窗口不足
    with pytest.raises(ModelNotReadyError, match="窗口不足"):
        trained_env.svc.predict_realtime(tiny, trained_env.model_name)


def test_predict_realtime_model_not_found(trained_env: SimpleNamespace) -> None:
    """模型不存在 → ModelNotFoundError"""
    with pytest.raises(ModelNotFoundError):
        trained_env.svc.predict_realtime(
            trained_env.data, "no_such_model")


# ═══════════════════════════════════════════════
#  6. 未来函数负向用例
# ═══════════════════════════════════════════════

def test_future_function_strict_blocked(trained_env: SimpleNamespace) -> None:
    """样本内预测负向：输入最新时间 < 训练截止 → strict 抛 InferenceError"""
    early = trained_env.data.iloc[:100]  # 早于训练截止
    assert float(early["timestamp"].max()) < float(
        trained_env.data["timestamp"].max())
    with pytest.raises(InferenceError, match="样本内预测"):
        trained_env.svc.predict_realtime(
            early, trained_env.model_name, strict=True)


def test_future_function_non_strict_allowed(trained_env: SimpleNamespace) -> None:
    """非 strict：样本内预测仅告警不阻断（返回结果）"""
    early = trained_env.data.iloc[:100]
    inf = trained_env.svc.predict_realtime(
        early, trained_env.model_name, strict=False)
    assert inf.n_samples == 1


def test_future_function_no_train_end_guard() -> None:
    """无 train_end_time（旧模型）→ 守卫跳过（不抛错）"""
    svc = InferenceService(registry=ModelRegistry(models_root="."))
    meta = ModelVersion(model_name="legacy", version=1, train_end_time=None)
    # 不抛（返回 None）
    assert svc._check_time_window(1234567890.0, meta, strict=True) is None


# ═══════════════════════════════════════════════
#  7. 延迟基准（F-06 整改内容 2 的单元覆盖）
# ═══════════════════════════════════════════════

def test_benchmark_measure_structure(tmp_path: Any) -> None:
    """InferenceBenchmark.measure 报告结构完整、JSON 可序列化"""
    from myquant.ml.inference_benchmark import InferenceBenchmark
    bench = InferenceBenchmark(repeat=2)
    report = bench.measure(lengths=[100], model_name="bench_ut",
                           registry=ModelRegistry(
                               models_root=str(tmp_path / "bench_models")))
    import json
    json.dumps(report, ensure_ascii=False)  # 可序列化
    assert "lengths" in report and len(report["lengths"]) == 1
    assert report["hard_limit_ms"] == 500.0
    assert report["target_ms"] == 200.0
    assert "p99_ms" in report["lengths"][0]
    assert isinstance(report["overall_passed"], bool)


def test_benchmark_parameter_defense() -> None:
    """基准参数防御：非法阈值/重复次数/长度显式报错"""
    from myquant.ml.inference_benchmark import InferenceBenchmark
    with pytest.raises(ValueError, match="hard_limit_ms"):
        InferenceBenchmark(hard_limit_ms=0)
    with pytest.raises(ValueError, match="target_ms"):
        InferenceBenchmark(target_ms=-1.0)
    with pytest.raises(ValueError, match="repeat"):
        InferenceBenchmark(repeat=0)
    bench = InferenceBenchmark()
    with pytest.raises(ValueError, match="lengths"):
        bench._validate_lengths([])
    with pytest.raises(ValueError, match="升序"):
        bench._validate_lengths([500, 100])
    with pytest.raises(ValueError, match="正整数"):
        bench._validate_lengths([100, 0])


__all__: List[str] = []
