# 文件名：tests/unit/test_ml_cost_labels.py
"""
test_ml_cost_labels.py - F-09 训练目标纳入交易成本测试
================================================================
覆盖《因子与机器学习层优化实施报告》阶段二 P1 第三项 F-09（回写#96）：

  1. TradeCost：双边往返成本率数值对拍（默认 0.00182，手工可复算）；
     from_sim_broker_cfg 与回测撮合层 config_builder 同口径对拍
  2. 成本化标签：扣费后 forward_return = 毛收益 − roundtrip_rate；
     direction 用扣费后方向（临界样本翻转可检出）
  3. 默认零变化：无 cost 时标签与既有实现完全一致；evaluate_model
     默认不产出 turnover/net_return
  4. 扣费后指标：net_return = 毛策略累计收益 − 换手率×往返成本率；
     换手率 = 方向信号反转比例（0~1）
  5. win gate：win_metric="net_return" 激活/拒绝分支
  6. 序列化往返 / 参数防御 / 端到端带 cost 训练注册

运行方式:
  pytest tests/unit/test_ml_cost_labels.py -v

设计原则:
  - 全部合成数据（确定性随机游走防未来，零网络）
  - 独立临时 registry（不污染真实 datasets/models）
  - 成本参数来源与 config/backtest_config.example.yaml sim_broker 段一致
    （验收：训练标签口径与回测撮合层成本参数一致）
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd
import pytest

from myquant.ml.features import FeatureConfig, FeatureEngine, TradeCost
from myquant.ml.model_registry import ModelRegistry
from myquant.ml.training_pipeline import TrainingPipeline, evaluate_model
from myquant.models.backtest import CommissionModel, SlippageModel
from tests.conftest import make_ohlcv_df

# ═══════════════════════════════════════════════
#  常量 / 合成数据构造
# ═══════════════════════════════════════════════

# 来源：config/backtest_config.example.yaml「sim_broker」段（90-95 行，与
# config_builder.py 155-173 构造 CommissionModel/SlippageModel 的参数同源）。
SIM_BROKER_CFG: Dict[str, Any] = {
    "commission_rate": 0.0002,        # 佣金费率（万2，买卖双向）
    "min_commission": 5.0,            # 单笔最低佣金（元）
    "stamp_tax_rate": 0.0005,        # 印花税率（万5=0.05%，仅卖出；2023-08-28起减半）
    "transfer_fee_rate": 0.00001,     # 过户费率（万0.1，买卖双向）
    "slippage_mode": "ratio",         # 滑点模式：ratio=按比例
    "slippage_value": 0.0002,         # 滑点数值（万2）
}
# 默认双边往返成本率（手工可复算）：
#   佣金 0.0002×2 + 印花税 0.0005 + 过户费 0.00001×2 + 滑点 2/10000×2
ROUNDTRIP_DEFAULT: float = 0.00132


class _ConstModel:
    """确定性伪模型：predict 返回常数（测试用，避免 sklearn 依赖）。"""

    def __init__(self, value: float) -> None:
        if not np.isfinite(value):
            raise ValueError(f"value 必须有限: {value}")
        self._value = float(value)

    def predict(self, X: Any) -> np.ndarray:
        return np.full(int(np.asarray(X).shape[0]), self._value)


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
    return make_ohlcv_df(closes=closes)


def _default_cost() -> TradeCost:
    """A股写实默认成本（与回测撮合层 sim_broker 段同口径）。"""
    return TradeCost.from_sim_broker_cfg(SIM_BROKER_CFG)


# ═══════════════════════════════════════════════
#  1. TradeCost 往返成本率 / 撮合口径对拍 / 序列化 / 防御
# ═══════════════════════════════════════════════

def test_tradecost_roundtrip_rate_numeric() -> None:
    """默认双边往返成本率数值对拍（0.00132，手工可复算；印花税2023-08-28起减半）。"""
    cost = TradeCost(CommissionModel(), SlippageModel())
    assert cost.roundtrip_rate() == pytest.approx(ROUNDTRIP_DEFAULT)
    assert cost.roundtrip_rate() == pytest.approx(0.00132)


def test_tradecost_from_sim_broker_cfg_parity() -> None:
    """from_sim_broker_cfg 与回测撮合层 config_builder 同口径对拍。

    config_builder.py 155-173：CommissionModel(type=RATIO, buy/sell_rate=
    commission_rate, min_commission, stamp_tax, transfer_fee) + ratio 滑点
    转 SlippageModel(fixed_bps=slippage_value*10000)。字段级对拍锁定。
    """
    cost = TradeCost.from_sim_broker_cfg(SIM_BROKER_CFG)
    assert cost.commission.buy_rate == pytest.approx(0.0002)
    assert cost.commission.sell_rate == pytest.approx(0.0002)
    assert cost.commission.min_commission == pytest.approx(5.0)
    assert cost.commission.stamp_tax == pytest.approx(0.0005)
    assert cost.commission.transfer_fee == pytest.approx(0.00001)
    assert cost.slippage.fixed_bps == pytest.approx(0.0002 * 10000.0)  # 2.0 基点
    assert cost.roundtrip_rate() == pytest.approx(ROUNDTRIP_DEFAULT)


def test_tradecost_dict_roundtrip() -> None:
    """TradeCost to_dict/from_dict 往返（枚举 type 字符串化）。"""
    cost = _default_cost()
    d = cost.to_dict()
    assert d["commission"]["type"] == "ratio"  # 枚举已字符串化（JSON 安全）
    cost2 = TradeCost.from_dict(d)
    assert cost2.commission == cost.commission
    assert cost2.slippage == cost.slippage
    assert cost2.roundtrip_rate() == pytest.approx(ROUNDTRIP_DEFAULT)


def test_tradecost_negative_rate_defense() -> None:
    """负费率 / 负滑点显式报错（防御式）。"""
    with pytest.raises(ValueError, match="佣金"):
        TradeCost(CommissionModel(buy_rate=-0.001), SlippageModel())
    with pytest.raises(ValueError, match="佣金"):
        TradeCost(CommissionModel(sell_rate=-0.001), SlippageModel())
    with pytest.raises(ValueError, match="印花税"):
        TradeCost(CommissionModel(stamp_tax=-0.1), SlippageModel())
    with pytest.raises(ValueError, match="过户费"):
        TradeCost(CommissionModel(transfer_fee=-0.1), SlippageModel())
    with pytest.raises(ValueError, match="滑点"):
        TradeCost(CommissionModel(), SlippageModel(fixed_bps=-1.0))


def test_feature_config_cost_serialization() -> None:
    """FeatureConfig 含 cost 的 JSON 全链路往返；无 cost 时零变化。"""
    cfg = FeatureConfig(cost=_default_cost())
    cfg2 = FeatureConfig.from_json(cfg.to_json())
    assert cfg2.cost is not None
    assert cfg2.cost.roundtrip_rate() == pytest.approx(ROUNDTRIP_DEFAULT)
    assert cfg2.horizon == cfg.horizon  # 其余字段不受影响
    assert cfg2.label_type == cfg.label_type
    # 无 cost：序列化往返后仍为 None（零变化）
    cfg_none = FeatureConfig()
    assert FeatureConfig.from_json(cfg_none.to_json()).cost is None


# ═══════════════════════════════════════════════
#  2. 成本化标签：扣费后收益 / direction 临界翻转 / 默认零变化
# ═══════════════════════════════════════════════

def test_cost_label_gross_minus_roundtrip() -> None:
    """扣费后 forward_return = 毛收益 − 双边往返成本率（逐行断言）。"""
    df = _synthetic_df(80, seed=7)
    engine_cost = FeatureEngine(FeatureConfig(cost=_default_cost()))
    engine_raw = FeatureEngine(FeatureConfig())
    labels_cost = engine_cost.generate_labels(df)
    labels_raw = engine_raw.generate_labels(df)
    rr = _default_cost().roundtrip_rate()
    pd.testing.assert_series_equal(
        labels_cost["forward_return"],
        labels_raw["forward_return"] - rr,
        check_names=False,
    )


def test_direction_label_net_threshold_flip() -> None:
    """direction 用扣费后方向：毛收益∈(0, rr) 的临界样本方向翻转。"""
    rr = _default_cost().roundtrip_rate()  # 0.00182
    # c0=10 → c1=10*(1+rr/2)：毛收益 = rr/2 > 0；扣费后 = −rr/2 < 0
    closes = [10.0, 10.0 * (1.0 + rr / 2.0), 10.0 * 1.01]
    df = make_ohlcv_df(closes=closes)
    cost_cfg = FeatureConfig(horizon=1, label_type="direction", cost=_default_cost())
    raw_cfg = FeatureConfig(horizon=1, label_type="direction")
    labels_cost = FeatureEngine(cost_cfg).generate_labels(df)
    labels_raw = FeatureEngine(raw_cfg).generate_labels(df)
    assert float(labels_raw["direction"].iloc[0]) == 1.0   # 毛方向：涨
    assert float(labels_cost["direction"].iloc[0]) == 0.0  # 扣费后：不涨
    # 扣费后收益应为负（rr/2 − rr = −rr/2）
    assert float(labels_cost["forward_return"].iloc[0]) == pytest.approx(-rr / 2.0)


def test_cost_disabled_zero_change() -> None:
    """无 cost：标签与既有实现完全一致（手工复算毛收益）。"""
    df = _synthetic_df(80, seed=11)
    labels = FeatureEngine(FeatureConfig()).generate_labels(df)
    close = df["收盘价"].astype(float)
    fwd = close.shift(-5) / close - 1.0
    np.testing.assert_allclose(
        labels["forward_return"].dropna().to_numpy(), fwd.dropna().to_numpy())
    # direction 型同样零变化
    labels_d = FeatureEngine(FeatureConfig(label_type="direction")).generate_labels(df)
    assert set(labels_d["direction"].dropna().unique()) <= {0.0, 1.0}


def test_cost_label_build_dataset_alignment_unchanged() -> None:
    """成本化不改变 build_dataset 的对齐语义（丢弃窗口不足/无标签尾部行数一致）。"""
    df = _synthetic_df(120, seed=13)
    ds_cost = FeatureEngine(FeatureConfig(cost=_default_cost())).build_dataset(df)
    ds_raw = FeatureEngine(FeatureConfig()).build_dataset(df)
    assert ds_cost.n_samples == ds_raw.n_samples
    assert list(ds_cost.feature_columns) == list(ds_raw.feature_columns)
    # 扣费后标签 = 毛标签 − 往返成本率（数据集层面同口径）
    rr = _default_cost().roundtrip_rate()
    np.testing.assert_allclose(
        ds_cost.y.to_numpy(), ds_raw.y.to_numpy() - rr, rtol=1e-9)


# ═══════════════════════════════════════════════
#  3. 扣费后指标：net_return / turnover / 换手惩罚 / 防御
# ═══════════════════════════════════════════════

def test_evaluate_net_metrics_produced() -> None:
    """compute_net=True 产出 turnover 与 net_return（回归与分类双路径）。"""
    X = np.arange(20.0).reshape(10, 2)
    y_reg = np.linspace(-0.02, 0.03, 10)
    m_reg = evaluate_model(
        _ConstModel(0.1), X, y_reg, label_type="return",
        compute_net=True, roundtrip_rate=ROUNDTRIP_DEFAULT)
    assert "turnover" in m_reg and "net_return" in m_reg
    assert m_reg["turnover"] >= 0.0
    assert isinstance(m_reg["net_return"], float)
    y_cls = np.array([0, 1, 0, 1, 0, 1, 0, 1, 0, 1], dtype=float)
    m_cls = evaluate_model(
        _ConstModel(0.8), X, y_cls, label_type="direction",
        compute_net=True, roundtrip_rate=ROUNDTRIP_DEFAULT)
    assert "turnover" in m_cls and "net_return" in m_cls
    # 默认关闭：compute_net=False 零变化（无 turnover/net_return 键）
    m_off = evaluate_model(
        _ConstModel(0.1), X, y_reg, label_type="return")
    assert "turnover" not in m_off and "net_return" not in m_off


def test_turnover_zero_net_equals_gross() -> None:
    """恒定方向：换手率=0 → net_return == 毛策略累计收益。"""
    y = np.array([0.01, 0.02, -0.005, 0.03])
    m = evaluate_model(
        _ConstModel(0.9), np.arange(4.0).reshape(4, 1), y,
        label_type="return", compute_net=True, roundtrip_rate=ROUNDTRIP_DEFAULT)
    assert m["turnover"] == pytest.approx(0.0)
    assert m["net_return"] == pytest.approx(float(np.sum(y)))


def test_net_return_penalty_less_than_gross() -> None:
    """有换手：net_return = 毛收益 − 换手率×往返成本率 < 毛收益。"""
    y = np.array([0.02, -0.01, 0.015, 0.01])
    pred = np.array([0.9, -0.4, -0.2, -0.1])  # 方向 [1, -1, -1, -1]：一次反转
    X = np.arange(4.0).reshape(4, 1)
    evaluate_model(
        _ConstModel(0.0), X, y, label_type="return",
        compute_net=True, roundtrip_rate=ROUNDTRIP_DEFAULT)
    # 用显式 pred 走 _net_strategy 等价路径（_ConstModel 无法表达变向信号，
    # 这里手工构造模型预测值）
    from myquant.ml.training_pipeline import _net_strategy  # 内部工具直接验证
    gross, turnover = _net_strategy(pred, y, "return")
    assert turnover == pytest.approx(1.0 / 3.0)  # 3 次 diff 中 1 次反转
    assert gross == pytest.approx(0.02 + 0.01 - 0.015 - 0.01)  # 方向×标签累计
    net = gross - turnover * ROUNDTRIP_DEFAULT
    assert net < gross
    # 端到端 evaluate_model：pred 反转时 net 严格小于毛收益
    m2 = evaluate_model(
        _NetModel(pred), X, y, label_type="return",
        compute_net=True, roundtrip_rate=ROUNDTRIP_DEFAULT)
    assert m2["net_return"] == pytest.approx(net)
    assert m2["net_return"] < gross


class _NetModel:
    """确定性伪模型：predict 返回给定预测序列（换手惩罚测试用）。"""

    def __init__(self, pred: np.ndarray) -> None:
        self._pred = np.asarray(pred, dtype=float)

    def predict(self, X: Any) -> np.ndarray:
        return np.asarray(self._pred).ravel()


def test_evaluate_net_negative_rate_defense() -> None:
    """roundtrip_rate 为负 → 显式报错（防御式）。"""
    X = np.arange(8.0).reshape(4, 2)
    y = np.linspace(-0.01, 0.02, 4)
    with pytest.raises(ValueError, match="roundtrip_rate"):
        evaluate_model(
            _ConstModel(0.1), X, y, label_type="return",
            compute_net=True, roundtrip_rate=-0.001)


# ═══════════════════════════════════════════════
#  4. 端到端：带 cost 训练注册 / win gate net_return
# ═══════════════════════════════════════════════

def test_e2e_cost_run_metrics(tmp_path: Any) -> None:
    """端到端：带 cost 的 FeatureConfig → run → 注册成功且 metrics 含扣费后指标。"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    pipe = TrainingPipeline(
        feature_config=FeatureConfig(cost=_default_cost()), registry=registry)
    result = pipe.run(_synthetic_df(160, seed=5), model_name="cost_model")
    meta = registry.get_model_metadata("cost_model", result.version)
    assert "turnover" in meta.metrics
    assert "net_return" in meta.metrics
    assert meta.metrics["turnover"] >= 0.0
    # 常驻绩效与注册链路不受成本化影响
    assert "max_drawdown" in meta.metrics
    loaded = registry.load_model("cost_model", result.version)
    assert loaded is not None


def test_e2e_no_cost_metrics_zero_change(tmp_path: Any) -> None:
    """无 cost：端到端 metrics 无 turnover/net_return（零变化）。"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    pipe = TrainingPipeline(feature_config=FeatureConfig(), registry=registry)
    result = pipe.run(_synthetic_df(160, seed=5), model_name="nocost_model")
    meta = registry.get_model_metadata("nocost_model", result.version)
    assert "turnover" not in meta.metrics
    assert "net_return" not in meta.metrics


def test_e2e_win_gate_net_return(tmp_path: Any) -> None:
    """win gate 用 win_metric="net_return"：更高净收益激活 / 更低 archived。"""
    registry = ModelRegistry(models_root=str(tmp_path / "models"))
    cfg = FeatureConfig()
    # v1：基准（无门控）→ 注册 pending_review，审批激活为 active（KP-P6-05）
    v1 = registry.register_model(
        "gate_model", _ConstModel(0.9),
        metadata={"metrics": {"net_return": 0.02, "turnover": 0.1}},
        feature_config=cfg)
    registry.approve_version("gate_model", v1.version, approver="test")
    assert registry.get_model_metadata("gate_model", v1.version).status == "active"
    # v2：net_return 更低 → 门控拒绝，archived 不激活
    v2 = registry.register_model(
        "gate_model", _ConstModel(0.8),
        metadata={"metrics": {"net_return": 0.01, "turnover": 0.5}},
        feature_config=cfg, require_win_gate=True, win_metric="net_return")
    assert registry.get_model_metadata("gate_model", v2.version).status == "archived"
    assert registry.get_model_metadata("gate_model", v1.version).status == "active"
    # v3：net_return 更高 → 门控通过，激活
    v3 = registry.register_model(
        "gate_model", _ConstModel(0.95),
        metadata={"metrics": {"net_return": 0.05, "turnover": 0.05}},
        feature_config=cfg, require_win_gate=True, win_metric="net_return")
    assert registry.get_model_metadata("gate_model", v3.version).status == "active"
    # 门控比较的是扣费后净收益（换手惩罚已含在 net_return 内）
    head = registry.get_model_metadata("gate_model", v3.version)
    assert head.metrics["net_return"] == pytest.approx(0.05)
