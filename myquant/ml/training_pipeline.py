# 文件名：myquant/ml/training_pipeline.py
"""
training_pipeline.py - 训练流水线（内置模型 + 时间序列切分 + 评估 + 注册闭环）
=============================================================================
机器学习训练流水线核心（任务20，ML 完整 MLOps 链路第三环）。

核心能力:
  - 内置 numpy 模型（零额外依赖，可插拔）:
      LinearRegressor   岭回归（正规方程 (XᵀX+λI)⁻¹Xᵀy，处理奇异矩阵）
      LogisticRegressor 逻辑回归（梯度下降二分类，内置标准化 + sigmoid 数值稳定）
      model_factory()   可插拔工厂：自定义模型（fit/predict）直传，默认按 label_type 返回内置
  - time_series_split 时间序列切分（防未来核心）:
      绝不随机切分：按 timestamp 升序切 train（早期）/ valid（后期），验证集只用之后数据
      strict_split 严格模式：验证集再向后跳过 horizon 样本，防训练标签重叠
  - evaluate 评估指标（numpy 手写）:
      回归: MSE/RMSE/MAE/R²；分类: accuracy/precision/recall/F1
  - run() 训练注册闭环:
      build_dataset(FeatureEngine) → 时间序列切分 → 训练(model_factory)
      → 验证集评估 → ModelRegistry.register_model
      （绑定 feature_config_hash / feature_columns / train_end_time /
        metrics / hyperparameters / source）

防未来函数（铁律4，本任务核心）:
  - 时间序列切分绝不随机，验证集只使用训练之后的数据（strict_split 加 horizon 缓冲）
  - train_end_time 记录训练数据截止（任务21 推理只使用 <= 截止数据）
  - 特征由任务18 保证 trailing 因果；标签仅训练目标列；无任何预测未来方法

数据来源（铁律）:
  - 输入只能读取 processed 清洗后数据（或 FeatureEngine 构建的 FeatureDataset），
    禁止读取 raw 原始数据（与总图 myquant/ml/ 注释一致）

为实盘无缝对接（任务21-23 实盘增强前置）:
  - 训练产出注册 ModelRegistry：feature_config_hash + feature_columns + train_end_time
    → 推理侧校验特征一致 + 只用 <= 训练截止数据（防未来）
  - retrain() 基于最新版本重训（版本链 + parent_version）；source 区分 offline/live
  - 全部 numpy 实现，无 sklearn 依赖（当前环境 sklearn/lightgbm/xgboost 均不可用）

依赖: numpy, pandas, myquant.ml.features, myquant.ml.model_registry
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from myquant.core.logger import get_logger
from myquant.ml.feature_contract import build_signature_from_frame
from myquant.ml.features import (
    FeatureConfig,
    FeatureDataset,
    FeatureEngine,
    PanelDataset,
)
from myquant.ml.model_registry import ModelRegistry

logger = get_logger("ml.training_pipeline")


# ═══════════════════════════════════════════════
#  异常类
# ═══════════════════════════════════════════════

class TrainingError(Exception):
    """训练流水线错误（数据不足 / 切分失败 / 输入非法）"""


# ═══════════════════════════════════════════════
#  内置 numpy 模型（零额外依赖）
# ═══════════════════════════════════════════════

def _sigmoid(z: np.ndarray) -> np.ndarray:
    """数值稳定的 sigmoid（分段 exp，避免溢出）"""
    z = np.asarray(z, dtype=float)
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


class LinearRegressor:
    """
    岭回归（内置 numpy，正规方程闭式解）

    目标函数: min_w ‖Xb·w - y‖² + α·‖w‖²  （Xb 含截距列）
    解法:    (XᵀX + αI)⁻¹Xᵀy，α 正则化保证矩阵可逆（处理奇异矩阵）
    """

    def __init__(self, alpha: float = 1.0):
        if alpha < 0:
            raise ValueError(f"岭回归 alpha 不能为负，实际 {alpha}")
        self.alpha = float(alpha)
        self.intercept_: Optional[float] = None
        self.coef_: Optional[np.ndarray] = None
        self.n_features_in_: Optional[int] = None

    def fit(self, X: Any, y: Any) -> "LinearRegressor":
        """拟合：正规方程闭式解，含 L2 正则（防奇异）"""
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).ravel()
        if X.ndim != 2:
            raise ValueError(f"X 必须是二维数组，实际 {X.ndim} 维")
        if X.shape[0] != len(y):
            raise ValueError(f"X 行数 {X.shape[0]} 与 y 长度 {len(y)} 不一致")
        n, p = X.shape
        Xb = np.column_stack([np.ones(n), X])
        A = Xb.T @ Xb + self.alpha * np.eye(p + 1)
        b = Xb.T @ y
        try:
            w = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:  # pragma: no cover - 数值兜底
            w = np.linalg.lstsq(Xb, y, rcond=None)[0]
        self.intercept_ = float(w[0])
        self.coef_ = w[1:]
        self.n_features_in_ = p
        return self

    def predict(self, X: Any) -> np.ndarray:
        """预测：y = intercept_ + X·coef_"""
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if self.n_features_in_ is None or self.coef_ is None:
            raise RuntimeError("模型尚未训练，请先调用 fit()")
        if X.shape[1] != self.n_features_in_:
            raise ValueError(
                f"特征数不匹配: 训练 {self.n_features_in_}，输入 {X.shape[1]}")
        return self.intercept_ + X @ self.coef_

    def get_params(self) -> Dict[str, Any]:
        """超参数（注册进模型元数据）"""
        return {"model_type": "LinearRegressor", "alpha": self.alpha}


class LogisticRegressor:
    """
    逻辑回归（内置 numpy，梯度下降二分类）

    - 内置特征标准化（fit 计算 mean/std，predict 应用同一变换，保证收敛稳定）
    - 数值稳定 sigmoid + logaddexp 损失
    - L2 正则（不含截距）
    - 接口 fit / predict / predict_proba（与 sklearn 风格一致，可插拔）
    """

    def __init__(
        self,
        learning_rate: float = 0.05,
        n_iter: int = 1000,
        tol: float = 1e-6,
        alpha: float = 1e-4,
    ):
        if learning_rate <= 0:
            raise ValueError(f"learning_rate 必须为正，实际 {learning_rate}")
        if n_iter <= 0:
            raise ValueError(f"n_iter 必须为正，实际 {n_iter}")
        if tol <= 0:
            raise ValueError(f"tol 必须为正，实际 {tol}")
        self.learning_rate = float(learning_rate)
        self.n_iter = int(n_iter)
        self.tol = float(tol)
        self.alpha = float(alpha)
        self.intercept_: Optional[float] = None
        self.coef_: Optional[np.ndarray] = None
        self.mean_: Optional[np.ndarray] = None
        self.std_: Optional[np.ndarray] = None
        self.n_features_in_: Optional[int] = None
        self.classes_: Optional[np.ndarray] = None
        self.n_iter_used_: int = 0
        self.loss_: float = 0.0

    def fit(self, X: Any, y: Any) -> "LogisticRegressor":
        """拟合：批量梯度下降 + 早停（损失变化 < tol）"""
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).ravel()
        if X.ndim != 2:
            raise ValueError(f"X 必须是二维数组，实际 {X.ndim} 维")
        if X.shape[0] != len(y):
            raise ValueError(f"X 行数 {X.shape[0]} 与 y 长度 {len(y)} 不一致")
        uniq = np.unique(y)
        if len(uniq) < 2 or not np.all(np.isin(uniq, [0.0, 1.0])):
            raise ValueError("逻辑回归要求二分类标签 y∈{0,1} 且同时包含两类")

        n, p = X.shape
        # 特征标准化（训练/预测共用同一变换）
        self.mean_ = X.mean(axis=0)
        self.std_ = X.std(axis=0)
        self.std_[self.std_ == 0.0] = 1.0
        Xs = (X - self.mean_) / self.std_
        Xb = np.column_stack([np.ones(n), Xs])

        w = np.zeros(p + 1)
        for i in range(self.n_iter):
            p_hat = _sigmoid(Xb @ w)
            grad = (Xb.T @ (p_hat - y)) / n
            # L2 正则（不含截距）
            grad[1:] += self.alpha * w[1:]
            w_new = w - self.learning_rate * grad
            if np.linalg.norm(w_new - w) < self.tol:
                w = w_new
                self.n_iter_used_ = i + 1
                break
            w = w_new
        else:
            self.n_iter_used_ = self.n_iter

        self.intercept_ = float(w[0])
        self.coef_ = w[1:]
        self.n_features_in_ = p
        self.classes_ = np.array([0, 1])
        # 最终损失（logaddexp 数值稳定）
        z = Xb @ w
        self.loss_ = float(
            np.mean(np.logaddexp(0.0, z) - y * z)
            + 0.5 * self.alpha * np.sum(w[1:] ** 2)
        )
        return self

    def predict_proba(self, X: Any) -> np.ndarray:
        """预测正类概率 P(y=1|X) ∈ (0,1)"""
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if self.n_features_in_ is None or self.coef_ is None or self.mean_ is None:
            raise RuntimeError("模型尚未训练，请先调用 fit()")
        if X.shape[1] != self.n_features_in_:
            raise ValueError(
                f"特征数不匹配: 训练 {self.n_features_in_}，输入 {X.shape[1]}")
        Xs = (X - self.mean_) / self.std_
        z = self.intercept_ + Xs @ self.coef_
        return _sigmoid(z)

    def predict(self, X: Any) -> np.ndarray:
        """预测类别 {0,1}（阈值 0.5）"""
        return (self.predict_proba(X) >= 0.5).astype(int)

    def get_params(self) -> Dict[str, Any]:
        """超参数（注册进模型元数据）"""
        return {
            "model_type": "LogisticRegressor",
            "learning_rate": self.learning_rate,
            "n_iter": self.n_iter,
            "tol": self.tol,
            "alpha": self.alpha,
            "standardize": True,
            "n_iter_used": self.n_iter_used_,
        }


# ═══════════════════════════════════════════════
#  可插拔模型工厂
# ═══════════════════════════════════════════════

def model_factory(label_type: Any = None, model: Any = None, **hyperparams: Any) -> Any:
    """
    可插拔模型工厂

    Parameters
    ----------
    label_type : str | FeatureConfig | dict | None
        标签类型（direction→逻辑回归，return/None→岭回归）；
        也兼容传入 FeatureConfig/dict 等带 label_type 属性的对象
    model : Any | None
        用户自定义模型（可插拔）。若传入则直接返回（须具备 fit/predict 方法）
    **hyperparams
        内置模型的超参数（如 alpha/learning_rate/n_iter 等）

    Returns
    -------
    模型实例（fit/predict 接口）
    """
    if model is not None:
        for meth in ("fit", "predict"):
            if not callable(getattr(model, meth, None)):
                raise TypeError(
                    f"自定义模型缺少方法 {meth}()，无法接入训练流水线（需 fit/predict）")
        return model
    if isinstance(label_type, dict):
        label_type = label_type.get("label_type", "return") or "return"
    elif not isinstance(label_type, str):
        label_type = getattr(label_type, "label_type", "return") or "return"
    if label_type == "direction":
        return LogisticRegressor(**hyperparams)
    return LinearRegressor(**hyperparams)


# ═══════════════════════════════════════════════
#  评估指标（numpy 手写）
# ═══════════════════════════════════════════════

def regression_metrics(y_true: Any, y_pred: Any) -> Dict[str, float]:
    """
    回归指标：MSE / RMSE / MAE / R²

    R² 定义: 1 - SS_res/SS_tot；SS_tot≈0（标签无方差）时返回 0.0
    """
    y_true = np.asarray(y_true, dtype=float).ravel()
    y_pred = np.asarray(y_pred, dtype=float).ravel()
    if y_true.shape != y_pred.shape:
        raise ValueError(f"y_true 长度 {len(y_true)} 与 y_pred 长度 {len(y_pred)} 不一致")
    n = len(y_true)
    if n == 0:
        raise ValueError("空标签无法计算回归指标")
    diff = y_true - y_pred
    mse = float(np.mean(diff ** 2))
    rmse = float(np.sqrt(mse))
    mae = float(np.mean(np.abs(diff)))
    ss_res = float(np.sum(diff ** 2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return {"mse": mse, "rmse": rmse, "mae": mae, "r2": r2}


def classification_metrics(
    y_true: Any, y_pred: Any, positive_label: int = 1
) -> Dict[str, float]:
    """
    分类指标：accuracy / precision / recall / F1

    positive_label=1 为正类；某类在样本中缺失时 precision/recall/F1 记 0.0
    """
    y_true = np.asarray(y_true, dtype=float).ravel()
    y_pred = np.asarray(y_pred, dtype=float).ravel()
    if y_true.shape != y_pred.shape:
        raise ValueError(f"y_true 长度 {len(y_true)} 与 y_pred 长度 {len(y_pred)} 不一致")
    n = len(y_true)
    if n == 0:
        raise ValueError("空标签无法计算分类指标")
    tp = float(np.sum((y_true == positive_label) & (y_pred == positive_label)))
    fp = float(np.sum((y_true != positive_label) & (y_pred == positive_label)))
    fn = float(np.sum((y_true == positive_label) & (y_pred != positive_label)))
    tn = float(np.sum((y_true != positive_label) & (y_pred != positive_label)))
    accuracy = (tp + tn) / n
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return {"accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1}


def _rank_ic(continuous_pred: Any, y_true: Any) -> Optional[float]:
    """评估集 IC（numpy 手写 Spearman 秩相关，F-05 注册门控用）。

    连续预测（回归=预测值；分类=正类概率）与真实标签的秩相关，
    反映模型排序能力（>0 为正相关，|IC| 越大越有效）。

    本行使用的数据截至 t 时刻：仅消费验证集（训练之后的数据）的
    已得预测与标签，无未来数据。

    Args:
        continuous_pred: 连续预测值（1D）。
        y_true: 真实标签（1D）。

    Returns:
        Spearman 秩相关系数；样本<2 或方差为 0 返回 None（不可计算）。
    """
    pred = np.asarray(continuous_pred, dtype=float).ravel()
    y = np.asarray(y_true, dtype=float).ravel()
    if pred.shape != y.shape or len(pred) < 2:
        return None
    mask = np.isfinite(pred) & np.isfinite(y)
    pred, y = pred[mask], y[mask]
    if len(pred) < 2:
        return None

    def _rank(v: np.ndarray) -> np.ndarray:
        # 并列值取平均秩（Spearman 标准做法）
        order = np.argsort(v, kind="stable")
        ranks = np.empty_like(v, dtype=float)
        ranks[order] = np.arange(1, len(v) + 1, dtype=float)
        # 并列值平均化
        sorted_v = v[order]
        i = 0
        while i < len(sorted_v):
            j = i
            while j + 1 < len(sorted_v) and sorted_v[j + 1] == sorted_v[i]:
                j += 1
            if j > i:
                avg = (ranks[order[i]] + ranks[order[j]]) / 2.0
                ranks[order[i:j + 1]] = avg
            i = j + 1
        return ranks

    rp = _rank(pred)
    ry = _rank(y)
    if float(np.std(rp)) == 0.0 or float(np.std(ry)) == 0.0:
        return None
    return float(np.corrcoef(rp, ry)[0, 1])


def _max_drawdown(continuous_pred: Any, y_true: Any, label_type: str) -> float:
    """验证集策略最大回撤（F-07 常驻绩效）。

    策略收益 = 方向信号 × 真实标签：回归用预测收益符号（>0 做多）、
    分类用正类概率阈值 0.5（>0.5 做多）——方向信号仅由模型预测决定，
    真实标签为训练后已观测的验证集数据。

    本行使用的数据截至 t 时刻：全部来自验证集评估时点已得数据，无未来信息
    （铁律11）。

    Args:
        continuous_pred: 连续预测（回归=预测收益；分类=正类概率）。
        y_true: 真实标签（1D，回归=收益；分类=0/1）。
        label_type: 标签类型（direction 用 0.5 阈值，其余用 0 阈值）。

    Returns:
        累计净值最大回撤（净值峰值到谷值的最大跌幅，非负值）；
        样本<2 或不可计算返回 0.0（防御式）。
    """
    pred = np.asarray(continuous_pred, dtype=float).ravel()
    y = np.asarray(y_true, dtype=float).ravel()
    if pred.shape != y.shape or len(pred) < 2:
        return 0.0
    mask = np.isfinite(pred) & np.isfinite(y)
    pred, y = pred[mask], y[mask]
    if len(pred) < 2:
        return 0.0
    if label_type == "direction":
        direction = np.where(pred > 0.5, 1.0, -1.0)
    else:
        direction = np.where(pred > 0.0, 1.0, -1.0)
    # 策略累计收益净值与峰值（本行使用的数据截至验证集评估时刻）
    equity = np.cumsum(direction * y)
    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    return float(np.max(drawdown))


def _net_strategy(
    continuous_pred: Any, y_true: Any, label_type: str,
) -> tuple[float, float]:
    """策略毛收益与换手率（F-09 扣费后指标）。

    策略收益序列 = 方向信号 × 真实标签（与 _max_drawdown 同口径）：
    回归用预测收益符号（>0 做多）、分类用正类概率阈值 0.5（>0.5 做多）。

    换手率（turnover）= 方向信号反转比例：|diff|/2 归一（1↔-1 计一次反转，
    对应一次"卖出旧仓+买入新仓"的往返），取值范围 0~1。

    【防未来函数】只消费验证集评估时点已得数据（铁律11）；
    本行使用的数据截至验证集评估时刻。

    Args:
        continuous_pred: 连续预测（回归=预测收益；分类=正类概率）。
        y_true: 真实标签（1D，回归=收益；分类=0/1）。
        label_type: 标签类型（direction 用 0.5 阈值，其余用 0 阈值）。

    Returns:
        (gross_return, turnover)：毛策略累计收益；换手率（0~1）。
        样本<2 或不可计算返回 (0.0, 0.0)（防御式）。
    """
    pred = np.asarray(continuous_pred, dtype=float).ravel()
    y = np.asarray(y_true, dtype=float).ravel()
    if pred.shape != y.shape or len(pred) < 2:
        return 0.0, 0.0
    mask = np.isfinite(pred) & np.isfinite(y)
    pred, y = pred[mask], y[mask]
    if len(pred) < 2:
        return 0.0, 0.0
    if label_type == "direction":
        direction = np.where(pred > 0.5, 1.0, -1.0)
    else:
        direction = np.where(pred > 0.0, 1.0, -1.0)
    gross = float(np.sum(direction * y))
    turnover = float(np.mean(np.abs(np.diff(direction)) / 2.0))
    return gross, turnover


def evaluate_model(
    model: Any, X: Any, y: Any, label_type: str = "return",
    positive_label: int = 1, compute_ic: bool = False,
    compute_drawdown: bool = False, compute_net: bool = False,
    roundtrip_rate: float = 0.0,
) -> Dict[str, float]:
    """
    通用评估：按标签类型分发回归 / 分类指标

    label_type='direction' → 分类指标；其余（return 等）→ 回归指标

    compute_ic=True（F-05）时追加 "ic"：分类用正类概率（若模型支持
    predict_proba）否则用 0/1 预测，回归用预测值，与 y_true 的秩相关。

    compute_drawdown=True（F-07）时追加 "max_drawdown"：基于方向信号
    （回归=预测收益符号 / 分类=正类概率 0.5 阈值）× 真实标签的策略累计
    净值最大回撤——常驻绩效字段，仅用验证集已得数据（防未来）。

    compute_net=True（F-09）时追加 "turnover" 与 "net_return"：
      毛策略累计收益 = Σ(方向信号×真实标签)；换手率 = 方向信号反转比例；
      net_return = 毛策略累计收益 − 换手率 × roundtrip_rate（换手惩罚，
      往返成本率与训练标签成本化同口径）——毛/净指标并存，供选型阶段
      win_metric="net_return" 等扣费后指标比较。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float).ravel()
    pred = model.predict(X)
    if label_type == "direction":
        metrics = classification_metrics(y, pred, positive_label=positive_label)
        cont = pred
        if hasattr(model, "predict_proba"):
            try:
                proba = np.asarray(model.predict_proba(X), dtype=float)
                if proba.ndim == 2 and proba.shape[1] >= 2:
                    cont = proba[:, 1]  # 正类概率
            except Exception:  # pragma: no cover
                pass
    else:
        metrics = regression_metrics(y, pred)
        cont = pred
    if compute_ic:
        ic = _rank_ic(cont, y)
        if ic is not None:
            metrics["ic"] = float(ic)
        else:
            metrics["ic"] = 0.0  # 不可计算（方差为0等）记 0，避免缺失键
    if compute_drawdown:
        metrics["max_drawdown"] = _max_drawdown(cont, y, label_type)
    if compute_net:
        if roundtrip_rate < 0:
            raise ValueError(
                f"roundtrip_rate 不能为负: {roundtrip_rate}")
        gross, turnover = _net_strategy(cont, y, label_type)
        metrics["turnover"] = turnover
        metrics["net_return"] = gross - turnover * roundtrip_rate
    return metrics


# ═══════════════════════════════════════════════
#  时间序列切分（防未来核心）
# ═══════════════════════════════════════════════

def time_series_split(
    timestamps: Any,
    train_ratio: float = 0.8,
    strict_split: bool = False,
    horizon: int = 5,
    min_train: int = 2,
    min_valid: int = 1,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    时间序列切分（防未来核心）

    绝不随机切分：按 timestamp 升序，train 取早期样本，valid 取后期样本。
    strict_split=True 时，验证集起始再向后跳过 horizon 个样本缓冲，
    保证训练集样本标签（未来 horizon 期收益）不落入验证集时段（防标签重叠）。

    Parameters
    ----------
    timestamps : array-like
        样本时间戳（float epoch 秒），与样本一一对应
    train_ratio : float
        训练集占比（0~1）
    strict_split : bool
        严格模式：验证集跳过 horizon 缓冲
    horizon : int
        标签未来期数（缓冲步数）
    min_train / min_valid : int
        训练/验证集最小样本数（不足抛错）

    Returns
    -------
    (train_idx, valid_idx) : 两个 np.ndarray（位置索引，指向 timestamps 排序后数组）
    """
    ts = np.asarray(timestamps, dtype=float)
    n = len(ts)
    if n == 0:
        raise ValueError("时间戳为空，无法切分")
    if not (0.0 < train_ratio < 1.0):
        raise ValueError(f"train_ratio 必须在 (0,1) 区间，实际 {train_ratio}")
    if horizon < 0:
        raise ValueError(f"horizon 不能为负，实际 {horizon}")

    # 稳定排序（时间升序；重复时间戳保持原相对顺序）
    order = np.argsort(ts, kind="stable")
    train_end = int(n * train_ratio)
    valid_start = train_end + (horizon if strict_split else 0)

    if train_end < min_train:
        raise ValueError(f"训练样本过少: train={train_end} < {min_train}")
    if valid_start >= n or (n - valid_start) < min_valid:
        raise ValueError(
            f"验证集为空（strict_split={strict_split} 缓冲 horizon={horizon}），"
            f"请降低 train_ratio 或增大数据量")

    train_idx = order[:train_end]
    valid_idx = order[valid_start:]
    # 防未来保证：验证集样本在时间序列上严格位于训练集之后
    # （strict_split 缓冲已保证训练集标签时段不进入验证集）
    return train_idx, valid_idx


def panel_time_series_split(
    timestamps: Any,
    group_ids: Any,
    train_ratio: float = 0.8,
    horizon: int = 5,
    min_train: int = 2,
    min_valid: int = 1,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    面板时间序列切分（F-08：日历化缓冲 + 整组确定性归属）

    适配多标的面板数据（与单标的 time_series_split 语义区分）：
      1. 日历化缓冲：验证集起点 = 训练集最后时间戳之后第 (horizon+1) 个交易日
         （交易日刻度 = 面板数据自身唯一时间戳升序；间隔口径对齐 F-06
         单标的实测缓冲——strict 跳 horizon 行产生 horizon+1 根时点间隔）
      2. 整组归属：同一时间戳的全部标的样本永不切开——确定性规则：
         同时间戳样本整体归入 train 或 valid 一侧

    【防未来函数（铁律11）】切分只消费面板数据自身的时间戳（训练时已得
    信息），本行使用的数据截至 t 时刻；训练集末尾样本的标签时刻
    （train_end + horizon 个交易日）不落入验证集时段 → 0% 标签重叠。

    Parameters
    ----------
    timestamps : array-like
        面板全部样本时间戳（float epoch 秒，与样本一一对应）
    group_ids : array-like
        每个样本的标的归属 id（与样本一一对应；长度不匹配显式报错）
    train_ratio : float
        训练集样本占比（0~1，按样本数累计、跨越比例点的整组归 train）
    horizon : int
        标签未来期数（日历化缓冲 = 跳 horizon 个交易日）
    min_train / min_valid : int
        训练/验证集最小样本数（不足抛错）

    Returns
    -------
    (train_idx, valid_idx) : 两个 np.ndarray（位置索引，指向原始输入数组）

    Raises
    ------
    ValueError : 时间戳/组 id 为空或不匹配、比例越界、horizon 负、样本不足
    """
    ts = np.asarray(timestamps, dtype=float)
    gids = np.asarray(group_ids)
    n = len(ts)
    if n == 0:
        raise ValueError("时间戳为空，无法切分")
    if len(gids) != n:
        raise ValueError(f"group_ids 长度 {len(gids)} 与样本数 {n} 不一致")
    if not (0.0 < train_ratio < 1.0):
        raise ValueError(f"train_ratio 必须在 (0,1) 区间，实际 {train_ratio}")
    if horizon < 0:
        raise ValueError(f"horizon 不能为负，实际 {horizon}")
    if n < min_train + min_valid:
        raise ValueError(
            f"面板样本过少: n={n} < min_train+min_valid={min_train + min_valid}")

    # 交易日刻度 = 数据自身唯一时间戳（升序；本行使用的数据截至 t 时刻）
    t_levels = np.unique(ts)
    # 稳定排序：同时间戳样本相邻（整组归属的基础）
    order = np.argsort(ts, kind="stable")
    sorted_ts = ts[order]
    # 按时间戳分组累计样本数，跨越 train_ratio 的整组时刻 = train_end_ts
    train_end_ts: Optional[float] = None
    cum: int = 0
    i: int = 0
    while i < len(sorted_ts):
        t = sorted_ts[i]
        j = i
        while j + 1 < len(sorted_ts) and sorted_ts[j + 1] == t:
            j += 1
        cum += j - i + 1
        if cum >= n * train_ratio:
            train_end_ts = float(t)
            break
        i = j + 1
    if train_end_ts is None:
        train_end_ts = float(sorted_ts[-1])
    # valid_start_ts = train_end_ts 之后第 (horizon+1) 个交易日。
    # 口径对齐 F-06 单标的实测缓冲（strict 跳 horizon 行产生 horizon+1 根
    # 时点间隔：valid 首=85 / train 末=79 / horizon=5），保证训练末样本的
    # 标签时点（train_end + horizon 个交易日收盘价）严格早于验证集首个
    # 样本时点 → 未来 horizon 收益 0% 落入验证集（报告验收）。
    pos = int(np.searchsorted(t_levels, train_end_ts, side="right"))
    if pos + horizon >= len(t_levels):
        raise ValueError(
            f"验证集为空（日历化缓冲：train_end={train_end_ts} 后不足 "
            f"{horizon + 1} 个交易日），请降低 train_ratio 或增大数据量")
    valid_start_ts = float(t_levels[pos + horizon])
    # 整组归属：train = 时间戳 <= train_end_ts 的全部样本；
    # valid = 时间戳 >= valid_start_ts 的全部样本（缓冲期内样本被排除）
    train_idx = np.flatnonzero(ts <= train_end_ts)
    valid_idx = np.flatnonzero(ts >= valid_start_ts)
    if len(train_idx) < min_train:
        raise ValueError(f"训练样本过少: {len(train_idx)} < {min_train}")
    if len(valid_idx) < min_valid:
        raise ValueError(f"验证集样本过少: {len(valid_idx)} < {min_valid}")
    return train_idx, valid_idx


# ═══════════════════════════════════════════════
#  滚动/扩展窗口时序 CV 切分（F-12）
# ═══════════════════════════════════════════════

@dataclass
class CvSplitConfig:
    """
    时序 CV 切分配置（F-12）

    mode="expanding"：扩展窗口——每折训练集自数据起点累积（随折推进单调扩展）
    mode="rolling" ：滚动窗口——训练集固定 rolling_window_days 个交易日滑移

    折布局（两模式一致）：
      首个验证段起点 = 第 int(总交易日数 × initial_train_ratio) 个交易日位置；
      验证段连续推进、互不重叠（每折 valid_days 个交易日）；
      训练/验证之间保留 horizon+1 个交易日日历缓冲（口径对齐 F-08/F-06：
      训练末样本标签时点 train_end+horizon 交易日严格早于验证集首样本
      → 未来 horizon 收益 0% 落入验证段，铁律11）。
    """

    mode: str = "expanding"
    n_folds: int = 4
    initial_train_ratio: float = 0.3
    valid_days: int = 20
    horizon: int = 5
    min_train: int = 20
    min_valid: int = 5
    rolling_window_days: int = 120

    def __post_init__(self) -> None:
        """防御式校验：非法配置显式报错（不静默兜底）"""
        if self.mode not in ("expanding", "rolling"):
            raise ValueError(
                f"mode 必须为 'expanding'/'rolling'，实际 {self.mode!r}")
        if self.n_folds < 1:
            raise ValueError(f"n_folds 必须 >= 1，实际 {self.n_folds}")
        if not (0.0 < self.initial_train_ratio < 1.0):
            raise ValueError(
                f"initial_train_ratio 必须在 (0,1) 区间，"
                f"实际 {self.initial_train_ratio}")
        if self.valid_days < 1:
            raise ValueError(f"valid_days 必须 >= 1，实际 {self.valid_days}")
        if self.horizon < 0:
            raise ValueError(f"horizon 不能为负，实际 {self.horizon}")
        if self.min_train < 1:
            raise ValueError(f"min_train 必须 >= 1，实际 {self.min_train}")
        if self.min_valid < 1:
            raise ValueError(f"min_valid 必须 >= 1，实际 {self.min_valid}")
        if self.mode == "rolling" and self.rolling_window_days < 1:
            raise ValueError(
                f"rolling_window_days 必须 >= 1，实际 {self.rolling_window_days}")

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CvSplitConfig":
        """从字典构造（忽略未知键，向后兼容配置演进）"""
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__.keys())
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class CvFold:
    """
    单折：训练/验证索引 + 时间范围 + 评估指标（F-12）

    train_idx/valid_idx 指向原始 timestamps 数组的位置索引；
    时间范围全部为训练/验证时已得事实（防未来），供报告入档。
    """

    fold_index: int
    train_idx: np.ndarray
    valid_idx: np.ndarray
    train_start_ts: float
    train_end_ts: float
    valid_start_ts: float
    valid_end_ts: float
    n_train: int
    n_val: int
    metrics: Dict[str, float]


def time_series_cv_split(
    timestamps: Any,
    group_ids: Any = None,
    config: Optional[CvSplitConfig] = None,
) -> List[CvFold]:
    """
    滚动/扩展窗口时序 CV 切分（F-12）

    以数据自身唯一时间戳为交易日刻度（升序，无外部日历依赖防未来）；
    验证段 [起点, 起点+valid_days) 连续推进、互不重叠；
    训练集（expanding）= 时间戳 <= train_end_level 的全部样本（起点累积）；
    （rolling）= 最近 rolling_window_days 个交易日（<= train_end_level）；
    train_end_level = 验证段起点前第 (horizon+1) 个交易日（日历缓冲
    对齐 F-08/F-06）；
    group_ids 提供时按时间阈值整组归属（同一时间戳全部样本永不切开）。

    【防未来函数（铁律11）】切分只消费数据自身时间戳（训练时已得信息），
    本行使用的数据截至 t 时刻；训练末样本的标签时刻（train_end + horizon
    个交易日）严格早于验证集首样本 → 0% 标签重叠。

    Parameters
    ----------
    timestamps : array-like
        面板/单标的全部样本时间戳（float epoch 秒，与样本一一对应）
    group_ids : array-like | None
        每个样本的标的归属 id（与样本一一对应；None 时行级切分；
        提供时整组确定性归属，长度不匹配显式报错）
    config : CvSplitConfig | None
        切分配置（None → 默认 expanding/4 折）

    Returns
    -------
    List[CvFold] : 各折（fold_index 从 1 起；按验证段时间升序排列）

    Raises
    ------
    ValueError : 时间戳为空/组 id 不匹配/配置非法/数据不足（某折训练或
    验证样本低于下限/验证段越界）
    """
    cfg = config if config is not None else CvSplitConfig()
    ts = np.asarray(timestamps, dtype=float)
    n = len(ts)
    if n == 0:
        raise ValueError("时间戳为空，无法切分")
    if group_ids is not None:
        gids = np.asarray(group_ids)
        if len(gids) != n:
            raise ValueError(
                f"group_ids 长度 {len(gids)} 与样本数 {n} 不一致")
    # 交易日刻度 = 数据自身唯一时间戳（升序；本行使用的数据截至 t 时刻）
    t_levels = np.unique(ts)
    n_levels = len(t_levels)
    initial_pos = int(n_levels * cfg.initial_train_ratio)
    if initial_pos < 1:
        raise ValueError(
            f"首个验证段起点过早：initial_train_ratio={cfg.initial_train_ratio} "
            f"仅分出 {initial_pos} 个交易日前段，请调低比例或增大数据")
    folds: List[CvFold] = []
    for i in range(cfg.n_folds):
        v_start_pos = initial_pos + i * cfg.valid_days
        v_end_pos = v_start_pos + cfg.valid_days
        if v_end_pos > n_levels:
            raise ValueError(
                f"第 {i + 1} 折验证段越界：数据仅 {n_levels} 个交易日刻度，"
                f"该折验证段需覆盖到第 {v_end_pos} 个刻度（请减少 n_folds/"
                f"valid_days 或增大数据量）")
        # 训练截止 = 验证段起点前第 (horizon+1) 个交易日（日历缓冲）
        train_end_pos = v_start_pos - (cfg.horizon + 1)
        if train_end_pos < 1:
            raise ValueError(
                f"第 {i + 1} 折训练集为空（horizon={cfg.horizon} 日历缓冲过大），"
                f"请降低 initial_train_ratio 或增大数据量")
        train_end_level = float(t_levels[train_end_pos - 1])
        if cfg.mode == "expanding":
            train_idx = np.flatnonzero(ts <= train_end_level)
            train_start_ts = float(t_levels[0])
        else:  # rolling：固定窗滑移
            roll_start_pos = max(0, train_end_pos - cfg.rolling_window_days)
            roll_start_level = float(t_levels[roll_start_pos])
            train_idx = np.flatnonzero(
                (ts >= roll_start_level) & (ts <= train_end_level))
            train_start_ts = roll_start_level
        valid_start_ts = float(t_levels[v_start_pos])
        valid_end_ts = float(t_levels[v_end_pos - 1])
        valid_idx = np.flatnonzero(
            (ts >= valid_start_ts) & (ts <= valid_end_ts))
        if len(train_idx) < cfg.min_train:
            raise ValueError(
                f"第 {i + 1} 折训练样本过少: {len(train_idx)} < "
                f"{cfg.min_train}")
        if len(valid_idx) < cfg.min_valid:
            raise ValueError(
                f"第 {i + 1} 折验证样本过少: {len(valid_idx)} < "
                f"{cfg.min_valid}")
        folds.append(CvFold(
            fold_index=i + 1,
            train_idx=train_idx,
            valid_idx=valid_idx,
            train_start_ts=train_start_ts,
            train_end_ts=train_end_level,
            valid_start_ts=valid_start_ts,
            valid_end_ts=valid_end_ts,
            n_train=int(len(train_idx)),
            n_val=int(len(valid_idx)),
            metrics={},
        ))
    return folds


# ═══════════════════════════════════════════════
#  TrainingResult 训练结果
# ═══════════════════════════════════════════════

@dataclass
class TrainingResult:
    """
    训练结果（run() 产物）

    Attributes
    ----------
    model : Any
        已训练模型（fit/predict 接口）
    model_name : str
        模型名称
    version : int
        注册版本号
    metrics : Dict[str, float]
        验证集评估指标（回归/分类）
    train_start_time / train_end_time : Optional[str]
        训练数据时间范围（train_end_time 为防未来截止时间）
    n_train / n_val : int
        训练/验证样本数
    feature_columns : List[str]
        特征列名（与任务18 对齐）
    feature_config_hash : Optional[str]
        特征配置 hash（训练/推理特征绑定）
    source : str
        来源：offline / live
    training_seconds : float
        训练耗时（秒）
    label_type : str
        标签类型：return / direction
    """
    model: Any
    model_name: str
    version: int
    metrics: Dict[str, float]
    train_start_time: Optional[str]
    train_end_time: Optional[str]
    n_train: int
    n_val: int
    feature_columns: List[str]
    feature_config_hash: Optional[str]
    source: str
    training_seconds: float
    label_type: str

    @property
    def tag(self) -> str:
        """版本标签，如 model@v1"""
        return f"{self.model_name}@v{self.version}"

    def to_dict(self) -> Dict[str, Any]:
        """序列化（供报告 / 落盘）"""
        return {
            "model_name": self.model_name,
            "version": self.version,
            "metrics": dict(self.metrics),
            "train_start_time": self.train_start_time,
            "train_end_time": self.train_end_time,
            "n_train": self.n_train,
            "n_val": self.n_val,
            "feature_columns": list(self.feature_columns),
            "feature_config_hash": self.feature_config_hash,
            "source": self.source,
            "training_seconds": round(float(self.training_seconds), 6),
            "label_type": self.label_type,
        }

    def __repr__(self) -> str:
        return (
            f"TrainingResult({self.tag}, {self.label_type}, "
            f"n_train={self.n_train}, n_val={self.n_val}, metrics={self.metrics})"
        )


# ═══════════════════════════════════════════════
#  TrainingPipeline 训练流水线
# ═══════════════════════════════════════════════

def _ts_to_str(ts: float) -> str:
    """epoch 秒 → "YYYY-MM-DD HH:MM:SS"（训练时间范围记录，防未来基准）"""
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════
#  CV / walk-forward 报告结构（F-12）
# ═══════════════════════════════════════════════

def _fold_wins(folds: List[CvFold], win_metric: str) -> List[float]:
    """提取各折 win 指标值（缺失显式报错，不静默兜底）"""
    wins: List[float] = []
    for fold in folds:
        if win_metric not in fold.metrics:
            raise ValueError(
                f"第 {fold.fold_index} 折缺少 win_metric={win_metric!r}，"
                f"可选指标 {sorted(fold.metrics)}")
        wins.append(float(fold.metrics[win_metric]))
    return wins


def _series_stats(values: List[float]) -> Tuple[float, float, float]:
    """均值/标准差（样本 ddof=1，n<2 记 0 防御）/ICIR（均值÷标准差）"""
    arr = np.asarray(values, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    icir = float(mean / std) if std > 0 else 0.0
    return mean, std, icir


def _series_max_drawdown(values: List[float]) -> float:
    """累计序列最大回撤：peak−value 的最大值（全正序列记 0）"""
    equity = np.cumsum(np.asarray(values, dtype=float))
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(max(0.0, float((peak - equity).max())))


@dataclass
class CvReport:
    """
    滚动/扩展窗口 CV 报告（F-12，选型证据入档）

    win_metric 默认 "ic"：报告各折 win 值 + 折间均值/标准差/ICIR；
    超参选择必须基于本报告（CV 证据），禁止单次留出定参。
    """

    folds: List[CvFold]
    win_metric: str = "ic"
    fold_wins: List[float] = field(default_factory=list)
    win_mean: float = 0.0
    win_std: float = 0.0
    win_icir: float = 0.0
    best_fold: Optional[int] = None
    worst_fold: Optional[int] = None

    @classmethod
    def from_folds(cls, folds: List[CvFold],
                   win_metric: str = "ic") -> "CvReport":
        """由已评估折构建（逐折 win 提取 + 统计汇总）"""
        wins = _fold_wins(folds, win_metric)
        mean, std, icir = _series_stats(wins)
        best_fold = None
        worst_fold = None
        if wins:
            best_fold = int(folds[int(np.argmax(wins))].fold_index)
            worst_fold = int(folds[int(np.argmin(wins))].fold_index)
        return cls(
            folds=folds,
            win_metric=win_metric,
            fold_wins=wins,
            win_mean=mean,
            win_std=std,
            win_icir=icir,
            best_fold=best_fold,
            worst_fold=worst_fold,
        )

    def to_dict(self) -> Dict[str, Any]:
        """JSON 安全序列化（入档用）"""
        return {
            "win_metric": self.win_metric,
            "fold_wins": list(self.fold_wins),
            "win_mean": self.win_mean,
            "win_std": self.win_std,
            "win_icir": self.win_icir,
            "best_fold": self.best_fold,
            "worst_fold": self.worst_fold,
            "folds": [
                {
                    "fold_index": fold.fold_index,
                    "train_start_time": _ts_to_str(fold.train_start_ts),
                    "train_end_time": _ts_to_str(fold.train_end_ts),
                    "valid_start_time": _ts_to_str(fold.valid_start_ts),
                    "valid_end_time": _ts_to_str(fold.valid_end_ts),
                    "n_train": fold.n_train,
                    "n_val": fold.n_val,
                    "metrics": dict(fold.metrics),
                }
                for fold in self.folds
            ],
        }

    def __repr__(self) -> str:
        return (
            f"CvReport(folds={len(self.folds)}, win_metric={self.win_metric}, "
            f"fold_wins={self.fold_wins}, mean={self.win_mean:.4f}, "
            f"std={self.win_std:.4f}, icir={self.win_icir:.4f})"
        )


@dataclass
class WalkForwardPeriod:
    """walk-forward 单期（F-12）：逐期重训 + 下段验证，时间升序"""

    period_index: int
    train_start_ts: float
    train_end_ts: float
    valid_start_ts: float
    valid_end_ts: float
    n_train: int
    n_val: int
    metrics: Dict[str, float]


@dataclass
class WalkForwardResult:
    """
    walk-forward 逐期重训评估结果（F-12）

    periods 按时间升序：每期用此前（expanding）全部可训数据重训，验证下一段；
    cum_net = 逐期净收益累计曲线（稳定性曲线数据，供绘图/入档）；
    ic_values + ic_mean/ic_std/icir + positive_ic_ratio（正 IC 期占比）
    + max_drawdown（累计净收益曲线回撤）→ 策略时序稳定性画像。
    """

    periods: List[WalkForwardPeriod] = field(default_factory=list)
    cum_net: List[float] = field(default_factory=list)
    ic_values: List[float] = field(default_factory=list)
    ic_mean: float = 0.0
    ic_std: float = 0.0
    ic_icir: float = 0.0
    net_total: float = 0.0
    positive_ic_ratio: float = 0.0
    max_drawdown: float = 0.0

    @classmethod
    def from_periods(
        cls, periods: List[WalkForwardPeriod],
        win_metric: str = "net_return",
    ) -> "WalkForwardResult":
        """由逐期评估结果构建（累计曲线 + 统计汇总）"""
        ic_values = [
            float(p.metrics["ic"]) if "ic" in p.metrics else 0.0
            for p in periods
        ]
        period_nets: List[float] = []
        for p in periods:
            if win_metric not in p.metrics:
                raise ValueError(
                    f"第 {p.period_index} 期缺少 win_metric={win_metric!r}，"
                    f"可选指标 {sorted(p.metrics)}")
            period_nets.append(float(p.metrics[win_metric]))
        ic_mean, ic_std, ic_icir = _series_stats(ic_values)
        cum_net = list(np.cumsum(np.asarray(period_nets, dtype=float)))
        net_total = float(cum_net[-1]) if cum_net else 0.0
        positive_ratio = (
            float(np.mean([v > 0 for v in ic_values])) if ic_values else 0.0
        )
        return cls(
            periods=periods,
            cum_net=cum_net,
            ic_values=ic_values,
            ic_mean=ic_mean,
            ic_std=ic_std,
            ic_icir=ic_icir,
            net_total=net_total,
            positive_ic_ratio=positive_ratio,
            max_drawdown=_series_max_drawdown(period_nets),
        )

    def to_dict(self) -> Dict[str, Any]:
        """JSON 安全序列化（稳定性曲线入档用）"""
        return {
            "cum_net": list(self.cum_net),
            "ic_values": list(self.ic_values),
            "ic_mean": self.ic_mean,
            "ic_std": self.ic_std,
            "ic_icir": self.ic_icir,
            "net_total": self.net_total,
            "positive_ic_ratio": self.positive_ic_ratio,
            "max_drawdown": self.max_drawdown,
            "periods": [
                {
                    "period_index": p.period_index,
                    "train_start_time": _ts_to_str(p.train_start_ts),
                    "train_end_time": _ts_to_str(p.train_end_ts),
                    "valid_start_time": _ts_to_str(p.valid_start_ts),
                    "valid_end_time": _ts_to_str(p.valid_end_ts),
                    "n_train": p.n_train,
                    "n_val": p.n_val,
                    "metrics": dict(p.metrics),
                }
                for p in self.periods
            ],
        }

    def __repr__(self) -> str:
        return (
            f"WalkForwardResult(periods={len(self.periods)}, "
            f"ic_mean={self.ic_mean:.4f}, ic_std={self.ic_std:.4f}, "
            f"net_total={self.net_total:.4f}, "
            f"positive_ic_ratio={self.positive_ic_ratio:.2%})"
        )


class TrainingPipeline:
    """
    训练流水线（任务20）

    训练注册闭环:
      run() → build_dataset(FeatureEngine) → 时间序列切分 → 训练(model_factory)
            → 验证集评估 → ModelRegistry.register_model
      （绑定 feature_config_hash / feature_columns / train_end_time /
        metrics / hyperparameters / source）

    为实盘无缝对接:
      - retrain() 基于最新版本重训（parent_version 版本链）
      - 训练产出（特征绑定 + 截止时间）直接供任务21 推理服务校验
    """

    def __init__(
        self,
        feature_config: Optional[FeatureConfig] = None,
        registry: Optional[ModelRegistry] = None,
    ):
        self.feature_config = feature_config if feature_config is not None else FeatureConfig()
        self.registry = registry if registry is not None else ModelRegistry()
        logger.info(
            f"[ml] 训练流水线初始化：label_type={self.feature_config.label_type}, "
            f"horizon={self.feature_config.horizon}")

    # ─────────────────────────────────────────────
    #  特征数据集构建（复用任务18）
    # ─────────────────────────────────────────────

    def _effective_config(self, label_type: Optional[str]) -> FeatureConfig:
        """按生效 label_type 生成特征配置（覆盖时用 dataclasses.replace，不影响原配置）"""
        if label_type is not None and label_type != self.feature_config.label_type:
            return replace(self.feature_config, label_type=label_type)
        return self.feature_config

    def build_dataset(
        self, data: Any, label_type: Optional[str] = None
    ) -> FeatureDataset:
        """
        构建特征数据集（FeatureEngine.build_dataset）

        label_type 非空时覆盖标签类型：direction→0/1 分类标签，return→连续收益。
        训练与推理共用同一 FeatureConfig（含 label_type），保证标签/特征一致。
        """
        cfg = self._effective_config(label_type)
        engine = FeatureEngine(cfg)
        return engine.build_dataset(data)

    # ─────────────────────────────────────────────
    #  时间序列切分（公开，供复用/验证）
    # ─────────────────────────────────────────────

    def split(
        self,
        dataset: Any,
        train_ratio: float = 0.8,
        strict_split: bool = True,
        horizon: Optional[int] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """按时间序列切分数据集（默认 strict_split=True 防标签重叠）。

        F-08：数据集为 PanelDataset（多标的面板）时走面板日历化切分
        （panel_time_series_split：整组确定性归属 + 日历缓冲）；
        FeatureDataset（单标的）保持 time_series_split 原语义零变化。
        """
        eff_horizon = horizon if horizon is not None else self.feature_config.horizon
        if isinstance(dataset, PanelDataset):
            return panel_time_series_split(
                dataset.timestamps,
                dataset.group_ids,
                train_ratio=train_ratio,
                horizon=eff_horizon,
            )
        return time_series_split(
            dataset.timestamps,
            train_ratio=train_ratio,
            strict_split=strict_split,
            horizon=eff_horizon,
        )

    # ─────────────────────────────────────────────
    #  训练注册闭环
    # ─────────────────────────────────────────────

    def run(
        self,
        data: Any,
        model_name: str,
        *,
        model: Any = None,
        label_type: Optional[str] = None,
        train_ratio: float = 0.8,
        strict_split: bool = True,
        horizon: Optional[int] = None,
        source: str = "offline",
        description: str = "",
        parent_version: Optional[int] = None,
        enable_win_gate: bool = False,
        win_metric: str = "ic",
        **model_params: Any,
    ) -> TrainingResult:
        """
        训练注册闭环（完整流水线）

        Parameters
        ----------
        data : Bar 列表 / DataFrame / dict[str, 数据]
            已清洗 processed 行情数据（FeatureEngine 输入）；
            F-08：dict[symbol→数据] 为多标的面板输入，走面板日历化切分
            （PanelDataset + panel_time_series_split），单标的输入零变化
        model_name : str
            模型名称（注册到 ModelRegistry）
        model : Any | None
            自定义模型（可插拔，需 fit/predict）；默认按 label_type 取内置模型
        label_type : str | None
            覆盖特征配置标签类型（direction→逻辑回归 / return→岭回归）
        train_ratio : float
            训练集占比（默认 0.8）
        strict_split : bool
            严格时间序列切分（默认 True，防标签重叠）
        horizon : int | None
            覆盖标签未来期数（切分缓冲）
        source : str
            offline / live
        description : str
            版本描述
        parent_version : int | None
            父版本（重训链）
        enable_win_gate : bool
            F-05 注册门控：True 时验证集评估追加 IC（metrics["ic"]），
            且注册要求"新版本 win_metric 必须跑赢当前 active 方可激活"
            （默认 False 零变化；实盘 retrain 建议开启）
        win_metric : str
            F-05 门控比较指标（默认 "ic"）
        **model_params
            内置模型超参数（透传 model_factory）

        Returns
        -------
        TrainingResult : 训练结果（模型 + 版本 + 指标 + 训练范围）
        """
        t0 = time.perf_counter()
        eff_label = label_type if label_type is not None else self.feature_config.label_type
        eff_horizon = horizon if horizon is not None else self.feature_config.horizon
        # 生效特征配置（label_type 覆盖时同步，注册 feature_config_hash 与训练配置一致）
        eff_config = self._effective_config(eff_label)

        # 1. 特征数据集（复用任务18，与推理共用配置）
        # F-08：data 为 dict[symbol→数据] 时走面板路径（PanelDataset），
        # 单标的 DataFrame/Bar 列表保持 build_dataset 原路径零变化。
        if isinstance(data, dict):
            panel_cfg = self._effective_config(eff_label)
            dataset = FeatureEngine(panel_cfg).build_panel_dataset(data)
        else:
            dataset = self.build_dataset(data, label_type=eff_label)
        if dataset.n_samples == 0:
            raise TrainingError(
                "特征数据集为空，无法训练（请检查输入数据量 / 特征窗口是否足够）")

        # 2. 时间序列切分（防未来：绝不随机）
        train_idx, valid_idx = self.split(
            dataset, train_ratio=train_ratio,
            strict_split=strict_split, horizon=eff_horizon)

        # 3. 模型（可插拔 model_factory）
        model_obj = model_factory(eff_label, model=model, **model_params)

        # 4. 训练（仅用训练集）
        model_obj.fit(dataset.X.values[train_idx], dataset.y.values[train_idx])

        # 5. 验证集评估（只用训练之后的数据；enable_win_gate 时追加 IC；
        #    F-07：compute_drawdown=True 常驻绩效 max_drawdown 总在验证集上计算；
        #    F-09：成本化开启（FeatureConfig.cost）时 compute_net=True，追加
        #    turnover / net_return 扣费后指标，供选型 win_metric="net_return"）
        roundtrip = (
            eff_config.cost.roundtrip_rate()
            if eff_config.cost is not None else 0.0
        )
        metrics = evaluate_model(
            model_obj, dataset.X.values[valid_idx],
            dataset.y.values[valid_idx], eff_label,
            compute_ic=enable_win_gate,
            compute_drawdown=True,
            compute_net=eff_config.cost is not None,
            roundtrip_rate=roundtrip)

        # 6. 训练时间范围（防未来：train_end_time 记录训练数据截止）
        train_ts = [dataset.timestamps[int(i)] for i in train_idx]
        train_start = _ts_to_str(float(min(train_ts)))
        train_end = _ts_to_str(float(max(train_ts)))

        # 6b. M2-02/03/04 模型层门禁（逻辑下沉 leakage_gate；违规 → 拦截）
        from myquant.leakage_gate.sanity import check_training_sanity
        _sanity_bad = check_training_sanity(train_ts, float(min(train_ts)), float(max(train_ts)))
        if _sanity_bad:
            raise TrainingError("训练侧模型层门禁未通过（M2-02/03/04）：" + "; ".join(_sanity_bad))

        # 7. F-07 data_snapshot：训练数据快照引用（指纹+行数+起止时间）。
        #    指纹 = 训练实际 X/y 内容的 sha256（np 数组字节布局确定）。
        #    只登记训练时已得数据事实（铁律11 防未来）；data_version 预留由
        #    独立 data_versioner（F-15 报告期≠可得时点时点化）接入后回填。
        fp_hasher = hashlib.sha256()
        for arr in (dataset.X.values[train_idx], dataset.y.values[train_idx]):
            fp_hasher.update(np.ascontiguousarray(arr).tobytes())
        data_snapshot: Dict[str, Any] = {
            "fingerprint": fp_hasher.hexdigest(),
            "n_train": int(len(train_idx)),
            "n_val": int(len(valid_idx)),
            "train_start_time": train_start,
            "train_end_time": train_end,
            "data_version": None,
        }
        # F-08：面板训练时登记标的构成（可追溯面板范围）；单标的省略该键
        if isinstance(dataset, PanelDataset):
            data_snapshot["symbols"] = dataset.symbols

        # 8. 注册闭环（绑定特征 + 截止时间 + 指标 + 超参 + 特征签名 + source
        #    + F-07 data_snapshot；runtime_env/model_params_count 由 registry
        #    侧补全兜底）
        config_hash = (
            hashlib.sha256(eff_config.to_json().encode("utf-8")).hexdigest()
            if hasattr(eff_config, "to_json") else None
        )
        feature_signature = build_signature_from_frame(
            dataset.X, config_hash=config_hash)
        metadata = {
            "metrics": metrics,
            "hyperparameters": (
                model_obj.get_params() if hasattr(model_obj, "get_params") else {}),
            "label_column": dataset.label_column,
            "feature_columns": dataset.feature_columns,
            "feature_signature": feature_signature.to_dict(),
            "train_start_time": train_start,
            "train_end_time": train_end,
            "parent_version": parent_version,
            "data_snapshot": data_snapshot,
        }
        version_meta = self.registry.register_model(
            model_name, model_obj,
            metadata=metadata,
            feature_config=eff_config,
            source=source,
            description=description,
            require_win_gate=enable_win_gate,
            win_metric=win_metric,
        )

        training_seconds = time.perf_counter() - t0
        result = TrainingResult(
            model=model_obj,
            model_name=model_name,
            version=version_meta.version,
            metrics=metrics,
            train_start_time=train_start,
            train_end_time=train_end,
            n_train=len(train_idx),
            n_val=len(valid_idx),
            feature_columns=list(dataset.feature_columns),
            feature_config_hash=version_meta.feature_config_hash,
            source=source,
            training_seconds=training_seconds,
            label_type=eff_label,
        )
        # P1-11：训练出口泄漏审计报告（R-16 §4.3；薄接线 ≤3 行调用——
        # sanity 段复用 6b 门禁结果 _sanity_bad，decision_time=train_end；
        # 行数红线 1651 由 R-24 基线更新留痕 #358 承担）
        from myquant.leakage_gate.report import write_experiment_report

        try:
            write_experiment_report(
                f"training_{model_name}_{version_meta.version}", "training",
                strategy_id=model_name,
                decision_time=train_end,
                sanity=_sanity_bad,
                extra={
                    "n_train": int(len(train_idx)),
                    "n_val": int(len(valid_idx)),
                    "source": source,
                },
            )
        except Exception:
            logger.warning(
                "训练泄漏审计报告落盘失败（P1-11，审计旁路不阻断训练）", exc_info=True)

        logger.info(
            f"[ml] 训练完成 {result.tag}: {eff_label} "
            f"n_train={result.n_train} n_val={result.n_val} "
            f"metrics={metrics} 耗时={training_seconds:.3f}s")
        return result

    # ─────────────────────────────────────────────
    #  重训练（实盘定期更新模型）
    # ─────────────────────────────────────────────

    def retrain(self, data: Any, model_name: str, **kwargs: Any) -> TrainingResult:
        """
        基于最新版本重训练（版本链 + parent_version）

        实盘场景：定时/增量重训新数据，产出新版本，不影响当前 active 版本，
        评估通过后由任务19 activate_version 热切换上线。
        """
        latest = self.registry.get_latest_version(model_name)
        parent = latest.version if latest is not None else None
        kwargs.setdefault("parent_version", parent)
        return self.run(data, model_name, **kwargs)

    # ─────────────────────────────────────────────
    #  滚动/扩展窗口 CV + walk-forward（F-12）
    # ─────────────────────────────────────────────

    def run_cv(
        self,
        data: Any,
        *,
        cv_config: Optional[Dict[str, Any]] = None,
        model: Any = None,
        label_type: Optional[str] = None,
        win_metric: str = "ic",
        **model_params: Any,
    ) -> CvReport:
        """
        滚动/扩展窗口时序 CV（F-12，选型证据）

        复用 build_dataset 的 dict→面板 dispatch（PanelDataset 走整组归属）；
        每折用新模型实例 fit(train) → evaluate(valid)（compute_ic=True +
        max_drawdown 常驻；FeatureConfig.cost 开启时追加 turnover/net_return）；
        不注册 ModelRegistry（CV 是超参/模型选择的证据，非部署产物）。
        win_metric 默认 "ic"：报告折间 IC 均值/标准差/ICIR——
        任何超参调整都须附本报告 CV 证据，禁止单次留出定参（验收）。

        Parameters
        ----------
        data : Bar 列表 / DataFrame / dict[str, 数据]
            与 run() 相同输入形态（dict 走面板路径）
        cv_config : dict | None
            CvSplitConfig 字段（None → expanding/4 折默认）
        model : Any | None
            自定义模型（可插拔，需 fit/predict）
        label_type : str | None
            覆盖特征配置标签类型
        win_metric : str
            折间汇总指标（默认 "ic"；成本化开启时可选 "net_return"）
        **model_params
            内置模型超参数（透传 model_factory）

        Returns
        -------
        CvReport : 各折评估 + 折间均值/波动/ICIR（to_dict 入档）
        """
        eff_label = label_type if label_type is not None else self.feature_config.label_type
        eff_config = self._effective_config(eff_label)
        if isinstance(data, dict):
            dataset = FeatureEngine(eff_config).build_panel_dataset(data)
        else:
            dataset = self.build_dataset(data, label_type=eff_label)
        if dataset.n_samples == 0:
            raise TrainingError(
                "特征数据集为空，无法 CV（请检查输入数据量 / 特征窗口是否足够）")
        cfg = CvSplitConfig.from_dict(cv_config)
        group_ids = dataset.group_ids if isinstance(dataset, PanelDataset) else None
        folds = time_series_cv_split(
            dataset.timestamps, group_ids=group_ids, config=cfg)
        roundtrip = (
            eff_config.cost.roundtrip_rate()
            if eff_config.cost is not None else 0.0
        )
        X = dataset.X.values
        y = dataset.y.values
        for fold in folds:
            model_obj = model_factory(eff_label, model=model, **model_params)
            model_obj.fit(X[fold.train_idx], y[fold.train_idx])
            fold.metrics = evaluate_model(
                model_obj, X[fold.valid_idx], y[fold.valid_idx], eff_label,
                compute_ic=True,
                compute_drawdown=True,
                compute_net=eff_config.cost is not None,
                roundtrip_rate=roundtrip)
        report = CvReport.from_folds(folds, win_metric=win_metric)
        logger.info(
            f"[ml] 时序 CV 完成 {len(folds)} 折: win_metric={win_metric} "
            f"mean={report.win_mean:.4f} std={report.win_std:.4f} "
            f"icir={report.win_icir:.4f}")
        return report

    def walk_forward(
        self,
        data: Any,
        *,
        cv_config: Optional[Dict[str, Any]] = None,
        model: Any = None,
        label_type: Optional[str] = None,
        win_metric: str = "net_return",
        **model_params: Any,
    ) -> WalkForwardResult:
        """
        walk-forward 逐期重训评估（F-12，时序稳定性曲线）

        与 run_cv 共享同一折生成器（口径一致：同布局/同缓冲）；
        每期用此前（expanding）全部可训数据重训，验证下一段；
        逐期输出 IC/换手/毛净收益 + 累计净收益曲线（稳定性曲线数据）。

        注意：本方法统一 compute_net=True——roundtrip_rate 按 FeatureConfig.cost
        取（无 cost 时 0.0 → net_return=毛策略累计收益），保证稳定性曲线
        在任何配置下都可输出；成本化开启时自动扣费。

        Parameters
        ----------
        data / cv_config / model / label_type / **model_params
            与 run_cv 相同
        win_metric : str
            逐期累计指标（默认 "net_return"；也可 "ic" 等 evaluate 指标）

        Returns
        -------
        WalkForwardResult : 逐期记录 + 累计曲线 + 稳定性统计（to_dict 入档）
        """
        eff_label = label_type if label_type is not None else self.feature_config.label_type
        eff_config = self._effective_config(eff_label)
        if isinstance(data, dict):
            dataset = FeatureEngine(eff_config).build_panel_dataset(data)
        else:
            dataset = self.build_dataset(data, label_type=eff_label)
        if dataset.n_samples == 0:
            raise TrainingError(
                "特征数据集为空，无法 walk-forward（请检查输入数据量 / 特征窗口）")
        cfg = CvSplitConfig.from_dict(cv_config)
        group_ids = dataset.group_ids if isinstance(dataset, PanelDataset) else None
        folds = time_series_cv_split(
            dataset.timestamps, group_ids=group_ids, config=cfg)
        roundtrip = (
            eff_config.cost.roundtrip_rate()
            if eff_config.cost is not None else 0.0
        )
        X = dataset.X.values
        y = dataset.y.values
        periods: List[WalkForwardPeriod] = []
        for fold in folds:
            model_obj = model_factory(eff_label, model=model, **model_params)
            model_obj.fit(X[fold.train_idx], y[fold.train_idx])
            metrics = evaluate_model(
                model_obj, X[fold.valid_idx], y[fold.valid_idx], eff_label,
                compute_ic=True,
                compute_drawdown=True,
                compute_net=True,  # 稳定性曲线恒可输出（roundtrip=0 即毛收益）
                roundtrip_rate=roundtrip)
            periods.append(WalkForwardPeriod(
                period_index=fold.fold_index,
                train_start_ts=fold.train_start_ts,
                train_end_ts=fold.train_end_ts,
                valid_start_ts=fold.valid_start_ts,
                valid_end_ts=fold.valid_end_ts,
                n_train=fold.n_train,
                n_val=fold.n_val,
                metrics=metrics,
            ))
        result = WalkForwardResult.from_periods(periods, win_metric=win_metric)
        logger.info(
            f"[ml] walk-forward 完成 {len(periods)} 期: "
            f"ic_mean={result.ic_mean:.4f} ic_std={result.ic_std:.4f} "
            f"net_total={result.net_total:.4f} "
            f"positive_ic_ratio={result.positive_ic_ratio:.2%}")
        return result


__all__ = [
    # 训练流水线
    "TrainingPipeline",
    "TrainingResult",
    "TrainingError",
    # 内置模型
    "LinearRegressor",
    "LogisticRegressor",
    # 工厂 / 切分 / 评估
    "model_factory",
    "time_series_split",
    "panel_time_series_split",
    "evaluate_model",
    "regression_metrics",
    "classification_metrics",
    # F-12 滚动/扩展窗口 CV + walk-forward
    "CvSplitConfig",
    "CvFold",
    "CvReport",
    "WalkForwardPeriod",
    "WalkForwardResult",
    "time_series_cv_split",
]
