# 文件名：tests/unit/test_ml_registry_signature.py
"""
test_ml_registry_signature.py - F-07 模型元数据补全与签名测试
================================================================
覆盖《因子与机器学习层优化实施报告》阶段二 P1 第一项 F-07（回写#94）：

  1. 元数据补全：data_snapshot（训练数据指纹/行数/起止）/ runtime_env
     （python/numpy/pandas 版本）/ model_params_count（防御式计算）/
     常驻绩效 max_drawdown（验证集策略最大回撤）
  2. 签名校验：HMAC-SHA256（密钥=环境变量 MYQUANT_MODEL_SIG_KEY）；
     无密钥降级=不签名+跳过校验（默认零变化）；元数据被篡改（任意字段，
     含 checksum 字段）→ 加载被拒 ModelSignatureError
  3. 不回归：checksum 篡改仍 ModelIntegrityError 拦截；retired 仍拒绝加载；
     旧版无签名 metadata.json 兼容读取

运行方式:
  pytest tests/unit/test_ml_registry_signature.py -v

设计原则:
  - 全部合成数据（确定性随机游走防未来，零网络）
  - 独立临时 registry（不污染真实 datasets/models）
  - 密钥经 monkeypatch 注入/清除，不留全局污染
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Tuple

import numpy as np
import pandas as pd
import pytest

from myquant.ml.features import FeatureConfig
from myquant.ml.model_registry import (
    ModelIntegrityError,
    ModelRegistry,
    ModelRetiredError,
    ModelSignatureError,
    ModelVersion,
)
from myquant.ml.training_pipeline import (
    TrainingPipeline,
    _max_drawdown,
    evaluate_model,
)

from tests.conftest import make_ohlcv_df


# ═══════════════════════════════════════════════
#  常量 / 合成数据 / fixtures
# ═══════════════════════════════════════════════

SIG_KEY: str = "unit-test-secret-key-9f8e7d6c5b4a3210"
ENV_SIG_KEY: str = "MYQUANT_MODEL_SIG_KEY"


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


def _make_countable_model() -> SimpleNamespace:
    """带 coef_ 的可计数模型（参数量 = 2x2 = 4）。"""
    return SimpleNamespace(coef_=np.array([[1.0, 2.0], [3.0, 4.0]]))


def _dummy_predict(x: Any) -> np.ndarray:
    """占位预测（恒 0），仅用于构造不可计数参数量的模型（模块级可 pickle）。"""
    return np.asarray(x, dtype=float).ravel() * 0.0


def _make_uncountable_model() -> SimpleNamespace:
    """无可计数属性、不可计算参数量的模型（应记 None 不阻断注册）。"""
    return SimpleNamespace(predict=_dummy_predict)


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    """独立临时 registry（不碰真实 datasets/models）。"""
    return ModelRegistry(models_root=str(tmp_path / "models"))


@pytest.fixture
def sig_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """注入签名密钥（仅本测试进程生效，不留全局污染）。"""
    monkeypatch.setenv(ENV_SIG_KEY, SIG_KEY)
    return SIG_KEY


def _register(
    registry: ModelRegistry,
    model_name: str = "sig_model",
    model: Any = None,
    metadata: Any = None,
    source: str = "offline",
) -> ModelVersion:
    """注册辅助：默认可计数模型 + 显式 data_snapshot，返回 ModelVersion。"""
    meta_input = dict(metadata or {})
    meta_input.setdefault(
        "data_snapshot",
        {
            "fingerprint": "a" * 64,
            "n_train": 80,
            "n_val": 20,
            "train_start_time": "2026-01-01 00:00:00",
            "train_end_time": "2026-08-31 00:00:00",
            "data_version": None,
        },
    )
    return registry.register_model(
        model_name,
        model if model is not None else _make_countable_model(),
        metadata=meta_input,
        source=source,
    )


def _tamper_metadata(
    registry: ModelRegistry, model_name: str, version: int,
    key: str, new_value: Any,
) -> None:
    """篡改 metadata.json 的任意字段（模拟元数据被修改）。"""
    mpath = registry._metadata_path(model_name, version)
    data = json.loads(Path(mpath).read_text(encoding="utf-8"))
    data[key] = new_value
    Path(mpath).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _tamper_artifact(registry: ModelRegistry, model_name: str, version: int) -> None:
    """篡改模型工件字节（模拟 model.pkl 被修改）。"""
    apath = registry._artifact_path(model_name, version)
    with open(apath, "ab") as f:
        f.write(b"\x00")


# ═══════════════════════════════════════════════
#  1. 元数据补全
# ═══════════════════════════════════════════════

def test_register_metadata_fields_complete(registry: ModelRegistry) -> None:
    """注册后元数据补全字段齐全：data_snapshot/runtime_env/model_params_count。"""
    meta = _register(registry)
    assert isinstance(meta.data_snapshot, dict)
    assert meta.data_snapshot["fingerprint"] == "a" * 64
    assert meta.data_snapshot["n_train"] == 80
    assert meta.data_snapshot["train_end_time"] == "2026-08-31 00:00:00"
    assert meta.runtime_env is not None and isinstance(meta.runtime_env, dict)
    assert meta.model_params_count is not None and meta.model_params_count > 0
    # 磁盘 metadata.json 同步含补全字段
    raw = json.loads(
        Path(registry._metadata_path("sig_model", 1)).read_text(encoding="utf-8"))
    assert "data_snapshot" in raw and "runtime_env" in raw
    assert "model_params_count" in raw


def test_runtime_env_has_python_numpy_pandas(registry: ModelRegistry) -> None:
    """runtime_env 含 python/numpy/pandas 版本且非空可追溯。"""
    meta = _register(registry)
    assert meta.runtime_env is not None
    for key in ("python", "numpy", "pandas"):
        val = meta.runtime_env.get(key)
        assert val is not None and str(val) != "" and str(val) != "unknown"


def test_model_params_count_counted(registry: ModelRegistry) -> None:
    """带 coef_ 的模型参数量 = coef_.size（确定性计数）。"""
    meta = _register(registry)
    assert meta.model_params_count == 4  # 2x2 coef_


def test_model_params_count_defensive_none(registry: ModelRegistry) -> None:
    """参数量不可计算 → 记 None + 告警，不阻断注册（防御式）。"""
    meta = _register(registry, model=_make_uncountable_model())
    assert meta.model_params_count is None
    assert meta.version == 1  # 注册成功


# ═══════════════════════════════════════════════
#  2. 签名校验
# ═══════════════════════════════════════════════

def test_signature_roundtrip_and_load(registry: ModelRegistry, sig_key: str) -> None:
    """注入密钥：注册产生签名 → 加载成功（签名-校验往返闭环）。"""
    meta = _register(registry)
    assert meta.signature is not None and len(meta.signature) == 64
    assert meta.signature_algorithm == "hmac-sha256"
    assert meta.signed_at is not None
    loaded = registry.load_model("sig_model", 1)
    assert loaded is not None  # 签名校验通过后正常加载


def test_no_key_unsigned_degrade_load(registry: ModelRegistry, caplog: Any) -> None:
    """无密钥：注册不签名（signature=None）+ 告警；加载跳过签名正常通过
    （默认零变化降级）。"""
    with caplog.at_level("WARNING", logger="ml.model_registry"):
        meta = _register(registry)
    assert meta.signature is None
    assert any("未设置" in r.message for r in caplog.records)
    loaded = registry.load_model("sig_model", 1)
    assert loaded is not None


def test_metadata_tamper_metrics_rejected(
    registry: ModelRegistry, sig_key: str,
) -> None:
    """篡改 metadata 的 metrics 字段 → 加载被拒 ModelSignatureError。"""
    _register(registry)
    _tamper_metadata(registry, "sig_model", 1, "metrics", {"ic": 0.999})
    with pytest.raises(ModelSignatureError):
        registry.load_model("sig_model", 1)


def test_metadata_tamper_checksum_field_rejected(
    registry: ModelRegistry, sig_key: str,
) -> None:
    """篡改 metadata 的 checksum 字段 → 签名层先拦截（杜绝连带重算式篡改）。"""
    _register(registry)
    _tamper_metadata(registry, "sig_model", 1, "checksum", "0" * 64)
    with pytest.raises(ModelSignatureError):
        registry.load_model("sig_model", 1)


def test_checksum_tamper_still_blocked(
    registry: ModelRegistry, sig_key: str,
) -> None:
    """篡改工件字节 → 仍被 checksum 拦截（ModelIntegrityError，不回归）。"""
    _register(registry)
    _tamper_artifact(registry, "sig_model", 1)
    with pytest.raises(ModelIntegrityError):
        registry.load_model("sig_model", 1)


def test_retired_still_blocked(registry: ModelRegistry, sig_key: str) -> None:
    """retired 版本仍拒绝加载（ModelRetiredError，签名链路不回归 F-05）。"""
    _register(registry)
    registry.mark_retired("sig_model", 1, reason="unit-test")
    with pytest.raises(ModelRetiredError):
        registry.load_model("sig_model", 1)


def test_signed_meta_key_missing_skips_with_warning(
    registry: ModelRegistry, sig_key: str, monkeypatch: pytest.MonkeyPatch,
    caplog: Any,
) -> None:
    """元数据已签名但密钥缺失 → 无法校验，告警降级跳过（不阻断加载）。"""
    _register(registry)
    monkeypatch.delenv(ENV_SIG_KEY, raising=False)
    with caplog.at_level("WARNING", logger="ml.model_registry"):
        loaded = registry.load_model("sig_model", 1)
    assert loaded is not None
    assert any("密钥缺失" in r.message for r in caplog.records)


def test_verify_model_signature_tamper_false(
    registry: ModelRegistry, sig_key: str,
) -> None:
    """verify_model：未篡改 True；篡改任意元数据字段 → False。"""
    _register(registry)
    assert registry.verify_model("sig_model", 1) is True
    _tamper_metadata(registry, "sig_model", 1, "description", "tampered")
    assert registry.verify_model("sig_model", 1) is False


# ═══════════════════════════════════════════════
#  3. 端到端 / 兼容 / 序列化
# ═══════════════════════════════════════════════

def test_e2e_training_register_signature_link(
    tmp_path: Path, sig_key: str,
) -> None:
    """端到端：训练 → 注册（data_snapshot + 常驻绩效 max_drawdown + 签名）
    → 加载签名链路完整，每个字段可追溯。"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    cfg = FeatureConfig()
    pipe = TrainingPipeline(feature_config=cfg, registry=registry)
    result = pipe.run(_synthetic_df(200), model_name="f07_e2e")
    meta = registry.get_model_metadata("f07_e2e", result.version)
    # 数据快照引用（训练时已得事实）
    assert meta.data_snapshot is not None
    assert len(meta.data_snapshot["fingerprint"]) == 64
    assert meta.data_snapshot["n_train"] == result.n_train
    assert meta.data_snapshot["n_val"] == result.n_val
    assert meta.data_snapshot["train_end_time"] == result.train_end_time
    # 常驻绩效：max_drawdown 存在且非负
    assert "max_drawdown" in meta.metrics
    assert meta.metrics["max_drawdown"] >= 0.0
    # 环境与参数量补全
    assert meta.runtime_env is not None
    assert meta.model_params_count is not None
    # 签名链路
    assert meta.signature is not None and meta.signature_algorithm == "hmac-sha256"
    loaded = registry.load_model("f07_e2e", result.version)
    assert loaded is not None


def test_old_metadata_without_signature_compatible(
    registry: ModelRegistry,
) -> None:
    """旧版 metadata.json（无签名字段）→ 兼容读取并正常加载（存量零变化）。"""
    meta = _register(registry)
    # 模拟旧版：写回无签名三字段的元数据（去掉 signature 相关键）
    mpath = registry._metadata_path("sig_model", meta.version)
    data = json.loads(Path(mpath).read_text(encoding="utf-8"))
    for key in ("signature", "signature_algorithm", "signed_at"):
        data.pop(key, None)
    Path(mpath).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    loaded = registry.load_model("sig_model", meta.version)
    assert loaded is not None
    assert registry.verify_model("sig_model", meta.version) is True


def test_to_dict_from_dict_roundtrip_new_fields() -> None:
    """ModelVersion 新增字段 to_dict/from_dict 往返无损。"""
    meta = ModelVersion(
        model_name="m", version=1,
        data_snapshot={"fingerprint": "b" * 64, "n_train": 10},
        runtime_env={"python": "3.14.6"},
        model_params_count=42,
        signature="ab" * 32,
        signature_algorithm="hmac-sha256",
        signed_at="2026-10-03 10:00:00",
    )
    restored = ModelVersion.from_dict(meta.to_dict())
    assert restored.data_snapshot == meta.data_snapshot
    assert restored.runtime_env == meta.runtime_env
    assert restored.model_params_count == 42
    assert restored.signature == meta.signature
    assert restored.signature_algorithm == "hmac-sha256"
    assert restored.signed_at == meta.signed_at


def test_canonical_json_stable() -> None:
    """canonical JSON：同内容不同字段顺序 → 相同序列化（签名可复算）。"""
    a = ModelRegistry._canonical_json({"metrics": {"ic": 0.1}, "status": "active"})
    b = ModelRegistry._canonical_json({"status": "active", "metrics": {"ic": 0.1}})
    assert a == b


def test_max_drawdown_numeric_known() -> None:
    """_max_drawdown 数值对拍（确定性序列，手工可复算）。"""
    y = np.array([0.1, -0.05, 0.2, -0.3, 0.15], dtype=float)
    pred = np.array([1.0, 1.0, 1.0, 1.0, 1.0], dtype=float)  # 全做多
    dd = _max_drawdown(pred, y, label_type="return")
    # equity=[0.1,0.05,0.25,-0.05,0.1] peak=[0.1,0.1,0.25,0.25,0.25]
    # dd=[0,0.05,0,0.3,0.15] → max=0.3
    assert dd == pytest.approx(0.3, abs=1e-12)


def test_evaluate_model_drawdown_switch() -> None:
    """evaluate_model：compute_drawdown=True 追加 max_drawdown；
    False 无该键（默认零变化）。"""
    model = SimpleNamespace(
        predict=lambda X: np.asarray(X, dtype=float)[:, 0] * 0.01)
    X = np.arange(50, dtype=float).reshape(50, 1)
    y = (np.arange(50, dtype=float) % 7 - 3) * 0.01
    with_dd = evaluate_model(model, X, y, "return", compute_drawdown=True)
    assert "max_drawdown" in with_dd and with_dd["max_drawdown"] >= 0.0
    without_dd = evaluate_model(model, X, y, "return", compute_drawdown=False)
    assert "max_drawdown" not in without_dd
