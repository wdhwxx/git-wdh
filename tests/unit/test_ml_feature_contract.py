# 文件名：tests/unit/test_ml_feature_contract.py
"""
test_ml_feature_contract.py - F-01 训练-推理特征一致性单元测试
================================================================
覆盖《因子与机器学习层优化实施报告》F-01（修复训练-推理特征一致性断裂）：

  1. bars_to_df 输出中文列契约（timestamp+开盘价/最高价/最低价/收盘价/
     成交量/成交额+trades）
  2. FeatureConfig.price_col 默认值 = 收盘价（与指标层/processed 导出层一致）
  3. 英文列输入被显式拒绝（契约校验，不做静默兼容）
  4. 技术指标不再被静默跳过（中文列下 sma_20_* 等指标列实际产出，
     派生特征 sma_gap 可计算）
  5. 指标计算失败 fail-fast（未知指标显式抛错，禁止静默降级）
  6. feature_contract 签名构建 / 序列化往返 / 双向校验
     （一致通过；缺失 / 多余 / 顺序不一致均拦截）
  7. 训练 → 注册（含 feature_signature）→ 推理端到端一致性
  8. 注册签名与推理列不一致 → FeatureMisalignmentError 拦截

运行方式:
  pytest tests/unit/test_ml_feature_contract.py -v

设计原则:
  - 全部使用合成数据（确定性随机游走），零网络依赖
  - 端到端用例使用 tmp_path 独立模型目录，不污染真实 datasets/models
  - 覆盖 F-01 全部整改点 + 旧缺陷回归（静默跳过已被根治）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import pytest

from myquant.ml.feature_contract import (
    FeatureContractError,
    FeatureSignature,
    build_signature_from_frame,
    validate_feature_signature,
)
from myquant.ml.features import (
    BAR_CN_COLUMNS,
    FeatureConfig,
    FeatureEngine,
    bars_to_df,
)
from myquant.ml.inference_service import (
    FeatureMisalignmentError,
    InferenceService,
)
from myquant.ml.model_registry import ModelRegistry, ModelVersion
from myquant.ml.training_pipeline import TrainingPipeline

from tests.conftest import make_ohlcv_df


# ═══════════════════════════════════════════════
#  Fixture：中文列行情数据
# ═══════════════════════════════════════════════

@pytest.fixture
def cn_ohlcv_df() -> pd.DataFrame:
    """中文列 OHLCV DataFrame（160 个交易日，确定性随机游走，含 timestamp 列）"""
    np.random.seed(42)
    closes = [10.0]
    for _ in range(159):
        closes.append(closes[-1] * (1 + np.random.normal(0, 0.02)))
    df = make_ohlcv_df(closes=closes)
    # 补 timestamp 列（unix 秒，供训练截止/防未来守卫使用）
    df["timestamp"] = df.index.astype("int64") // 10**9
    return df


def _make_bars(n: int = 8, base: float = 10.0) -> List[Any]:
    """构造 Bar 对象列表（SimpleNamespace 模拟，零依赖）"""
    bars: List[Any] = []
    for i in range(n):
        c = base + float(i)
        bars.append(SimpleNamespace(
            timestamp=float(1700000000 + i),
            open=c, high=c + 0.5, low=c - 0.5, close=c,
            volume=1000.0, amount=c * 1000.0, trades=10,
        ))
    return bars


# ═══════════════════════════════════════════════
#  1. bars_to_df 中文列契约
# ═══════════════════════════════════════════════

def test_bars_to_df_chinese_columns() -> None:
    """bars_to_df 输出中文列契约并按 timestamp 升序"""
    df = bars_to_df(_make_bars(5))
    assert list(df.columns) == list(BAR_CN_COLUMNS)
    assert "收盘价" in df.columns and "close" not in df.columns
    assert list(df["收盘价"]) == [10.0, 11.0, 12.0, 13.0, 14.0]
    # 升序（乱序输入也稳定）
    df2 = bars_to_df(_make_bars(5)[::-1])
    assert list(df2["timestamp"]) == sorted(df2["timestamp"])


def test_bars_to_df_empty() -> None:
    """bars_to_df 空输入返回带中文契约列的空 DataFrame"""
    df = bars_to_df([])
    assert list(df.columns) == list(BAR_CN_COLUMNS)
    assert df.empty


# ═══════════════════════════════════════════════
#  2. price_col 默认值
# ═══════════════════════════════════════════════

def test_price_col_default_chinese() -> None:
    """FeatureConfig.price_col 默认 = 收盘价（与指标层/导出层中文契约一致）"""
    assert FeatureConfig().price_col == "收盘价"


# ═══════════════════════════════════════════════
#  3. 英文列输入显式拒绝（不做静默兼容）
# ═══════════════════════════════════════════════

def test_english_columns_rejected() -> None:
    """英文列（open/high/low/close）输入被显式拒绝，错误信息提示中文契约"""
    df_en = pd.DataFrame({
        "open": [10.0] * 30, "high": [11.0] * 30, "low": [9.0] * 30,
        "close": [10.0] * 30, "volume": [1000.0] * 30,
    })
    engine = FeatureEngine(FeatureConfig())
    with pytest.raises(ValueError, match="价格列|中文"):
        engine.compute_features(df_en)


# ═══════════════════════════════════════════════
#  4. 指标不再被静默跳过
# ═══════════════════════════════════════════════

def test_indicators_computed_not_skipped(cn_ohlcv_df: pd.DataFrame) -> None:
    """中文列下技术指标实际产出（旧缺陷：英文列导致指标被静默跳过）"""
    feat = FeatureEngine(FeatureConfig()).compute_features(cn_ohlcv_df)
    assert any(c.startswith("sma_20_") for c in feat.columns)
    assert any(c.startswith("atr_14_") for c in feat.columns)
    # 派生特征基于指标列可计算（旧缺陷下缺失）
    assert "sma_gap" in feat.columns


# ═══════════════════════════════════════════════
#  5. 指标失败 fail-fast（禁止静默降级）
# ═══════════════════════════════════════════════

def test_indicator_failure_fail_fast(cn_ohlcv_df: pd.DataFrame) -> None:
    """未知指标实例化失败显式抛错（旧缺陷：warning+continue 静默跳过）"""
    cfg = FeatureConfig(indicator_configs=[{"name": "no_such_indicator", "prefix": "x"}])
    engine = FeatureEngine(cfg)
    with pytest.raises(ValueError):
        engine.compute_features(cn_ohlcv_df)


# ═══════════════════════════════════════════════
#  6. feature_contract 签名构建与校验
# ═══════════════════════════════════════════════

def test_signature_build_roundtrip() -> None:
    """签名构建 / 序列化往返一致性"""
    frame = pd.DataFrame({
        "return_5": [0.01, 0.02],
        "sma_20_sma": [10.1, 10.2],
    })
    sig = build_signature_from_frame(frame, config_hash="abc123")
    assert sig.columns == ["return_5", "sma_20_sma"]
    assert sig.config_hash == "abc123"
    restored = FeatureSignature.from_dict(sig.to_dict())
    assert restored.columns == sig.columns
    assert restored.dtypes == sig.dtypes
    assert restored.config_hash == sig.config_hash


def test_signature_validate_consistent() -> None:
    """列集合与顺序一致时校验通过（含 dtype 软校验不阻断）"""
    sig = FeatureSignature(
        columns=["a", "b"],
        dtypes={"a": "float64", "b": "float64"},
    )
    # 一致：通过
    validate_feature_signature(sig, ["a", "b"])
    # dtype 差异：软校验仅告警，不抛错
    validate_feature_signature(sig, ["a", "b"], {"a": np.dtype("float32"), "b": np.dtype("float64")})


def test_signature_validate_mismatch() -> None:
    """缺失 / 多余 / 顺序不一致均被硬拦截"""
    sig = FeatureSignature(columns=["a", "b"], dtypes={"a": "float64", "b": "float64"})
    with pytest.raises(FeatureContractError, match="缺失"):
        validate_feature_signature(sig, ["a"])
    with pytest.raises(FeatureContractError, match="多余"):
        validate_feature_signature(sig, ["a", "b", "c"])
    with pytest.raises(FeatureContractError, match="顺序"):
        validate_feature_signature(sig, ["b", "a"])


def test_signature_from_dict_invalid() -> None:
    """非法签名数据拒绝（防御式）"""
    with pytest.raises(FeatureContractError):
        FeatureSignature.from_dict({"dtypes": {}})  # 缺 columns
    with pytest.raises(FeatureContractError):
        FeatureSignature.from_dict("not-a-dict")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════
#  7. 端到端：训练 → 注册（含签名）→ 推理一致
# ═══════════════════════════════════════════════

def test_training_to_inference_end_to_end(
    cn_ohlcv_df: pd.DataFrame, tmp_path: Any
) -> None:
    """训练注册（feature_signature 持久化）→ 推理特征一致性通过"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    cfg = FeatureConfig()
    pipe = TrainingPipeline(feature_config=cfg, registry=registry)
    result = pipe.run(cn_ohlcv_df, model_name="test_model_f01")

    # 注册元数据含 feature_signature 与 feature_columns
    meta = registry.get_model_info("test_model_f01")
    assert meta.feature_signature is not None
    assert meta.feature_columns == list(result.feature_columns)
    assert meta.feature_signature["columns"] == list(result.feature_columns)

    # 推理：同配置同数据 → 特征一致，正常预测
    svc = InferenceService(registry=registry, feature_config=cfg)
    inf = svc.predict_batch(cn_ohlcv_df, "test_model_f01")
    assert list(inf.feature_columns) == list(result.feature_columns)
    assert inf.n_samples > 0


# ═══════════════════════════════════════════════
#  8. 签名不一致 → 推理拦截
# ═══════════════════════════════════════════════

def test_inference_signature_mismatch_blocked(tmp_path: Any) -> None:
    """注册签名与实际特征列不一致 → FeatureMisalignmentError（防偏移硬拦截）"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    svc = InferenceService(registry=registry, feature_config=FeatureConfig())
    meta = ModelVersion(
        model_name="bad_model",
        version=1,
        feature_columns=["a", "b"],
        feature_signature={"columns": ["a", "b"], "dtypes": {}, "config_hash": None},
    )
    with pytest.raises(FeatureMisalignmentError):
        svc._build_engine(meta)


__all__: List[str] = []
