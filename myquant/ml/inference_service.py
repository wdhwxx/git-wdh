# 文件名：myquant/ml/inference_service.py
"""
inference_service.py - 批量/实时预测服务
========================================
机器学习推理服务（任务21，ML 完整 MLOps 链路第四环）。

核心能力:
  - load_model()          加载模型 + 元数据（含特征绑定 feature_columns / 训练截止 train_end_time）
  - predict_batch()       批量预测：processed 数据 → 特征 → 模型 → 预测（按时间对齐输出）
  - predict_realtime()    实时预测：最新 bar 增量特征 → 模型 → 预测（逐 bar，实盘逐笔）
  - 特征对齐校验          推理特征列与训练 feature_columns 严格一致（防训练-推理偏移）
  - train_end_time 守卫   输入最新时间 < 训练截止 → 样本内预测告警/拦截（防未来）

防未来函数（铁律4，本任务核心）:
  - 推理只用因果特征：复用任务18 FeatureEngine 的 trailing 窗口，t 时刻特征只依赖 <=t 数据
  - 推理绝不生成标签：只调用 compute_features / compute_incremental，不调用 build_dataset
    （标签 forward_return 引用未来 horizon 期数据，禁止出现在推理路径）
  - 特征列对齐：推理特征列与训练 feature_columns 严格一致，不一致抛错（防偏移信号）
  - train_end_time 守卫：输入数据最新时间 < 训练截止 → 样本内预测告警/拦截
    （保证"预测"只发生在训练截止之后的真未来，不把历史拟合当预测）

数据来源（铁律）:
  - 推理输入只能读取 processed 清洗后数据（Bar 列表或 DataFrame），
    禁止读取 raw 原始数据（与总图 myquant/ml/ 注释一致）

为实盘无缝对接（任务22-23 实盘增强前置）:
  - predict_realtime 逐 bar 增量预测：实盘引擎每收到新 bar 调用一次，
    compute_incremental 与训练特征逐列一致（防训练-推理偏移）
  - 模型版本热切换：加载时取 ModelRegistry active 版本，实盘 activate_version 不停机切换
  - InferenceResult 含 train_end_time / feature_columns / version / source，供实盘记录与审计
  - 全部 numpy 实现，无 sklearn 依赖（当前环境 sklearn 不可用）

依赖: numpy, pandas, myquant.ml.features, myquant.ml.model_registry
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger
from myquant.ml.features import FeatureConfig, FeatureEngine
from myquant.ml.model_registry import ModelRegistry, ModelVersion

logger = get_logger("ml.inference_service")


def _dates_to_epoch(series: Any) -> List[float]:
    """P1-18：processed「日期」列（int/str YYYYMMDD）转 unix 秒列表。"""
    out: List[float] = []
    for v in series:
        s = str(int(float(v))) if not isinstance(v, str) else v
        try:
            out.append(datetime.strptime(s[:8], "%Y%m%d").timestamp())
        except ValueError:
            continue
    return out


# ═══════════════════════════════════════════════
#  异常类
# ═══════════════════════════════════════════════

class InferenceError(Exception):
    """推理服务通用错误"""


class FeatureMisalignmentError(InferenceError):
    """推理特征列与训练不一致（防训练-推理偏移）"""


class ModelNotReadyError(InferenceError):
    """模型未就绪：数据为空 / 特征窗口不足 / 无法预测"""


# ═══════════════════════════════════════════════
#  InferenceResult 推理结果
# ═══════════════════════════════════════════════

@dataclass
class InferenceResult:
    """
    推理结果（predict_batch / predict_realtime 产物）

    Attributes
    ----------
    predictions : np.ndarray
        预测值（回归：连续值；分类：0/1 类别）
    timestamps : List[float]
        样本时间戳（与 predictions 一一对应；实时模式为最新 bar 时间）
    model_name : str
        模型名称
    version : int
        模型版本
    model_type : str
        模型类型
    label_type : str
        标签类型：return / direction
    feature_columns : List[str]
        实际使用的特征列（与训练绑定）
    train_end_time : Optional[str]
        训练数据截止（防未来记录）
    probabilities : Optional[np.ndarray]
        分类模型正类概率 P(y=1)（回归模型为 None）
    source : str
        模型来源：offline / live
    """
    predictions: np.ndarray
    timestamps: List[float]
    model_name: str
    version: int
    model_type: str
    label_type: str
    feature_columns: List[str]
    train_end_time: Optional[str]
    probabilities: Optional[np.ndarray] = None
    source: str = "offline"

    @property
    def n_samples(self) -> int:
        """样本数"""
        return int(len(self.predictions))

    def to_frame(self) -> pd.DataFrame:
        """输出 DataFrame（timestamp/prediction/probability），供落盘/看板"""
        rows: Dict[str, Any] = {
            "timestamp": list(self.timestamps),
            "prediction": list(np.asarray(self.predictions).ravel()),
        }
        if self.probabilities is not None:
            rows["probability"] = list(np.asarray(self.probabilities).ravel())
        return pd.DataFrame(rows)

    def to_dict(self) -> Dict[str, Any]:
        """序列化（供日志/审计）"""
        return {
            "model_name": self.model_name,
            "version": self.version,
            "model_type": self.model_type,
            "label_type": self.label_type,
            "n_samples": self.n_samples,
            "train_end_time": self.train_end_time,
            "feature_columns": list(self.feature_columns),
            "source": self.source,
        }

    def __repr__(self) -> str:
        return (
            f"InferenceResult({self.model_name}@v{self.version}, "
            f"{self.label_type}, n={self.n_samples})"
        )


# ═══════════════════════════════════════════════
#  InferenceService 推理服务
# ═══════════════════════════════════════════════

class InferenceService:
    """
    批量/实时预测服务（任务21）

    推理路径严格防未来：
      - 特征因果（trailing，复用任务18）
      - 不生成标签（只 compute_features / compute_incremental）
      - 特征列与训练严格对齐
      - train_end_time 守卫（样本内预测告警/拦截）
    """

    def __init__(
        self,
        registry: Optional[ModelRegistry] = None,
        feature_config: Optional[FeatureConfig] = None,
        result_cache_ttl_ms: float = 0.0,
    ):
        """
        Args:
            registry: 模型注册中心（默认新建）。
            feature_config: 特征配置（默认 FeatureConfig()）。
            result_cache_ttl_ms: F-14 推理结果短 TTL 缓存（毫秒）；
                默认 0.0=关闭（零变化）。>0 时，同 (模型,版本,最新时间戳) 在 TTL 内
                重复调用 predict_realtime 直接复用上次结果，不重复特征+预测。
        """
        self.registry = registry if registry is not None else ModelRegistry()
        self.feature_config = feature_config if feature_config is not None else FeatureConfig()
        # F-14 结果短 TTL 缓存：{(model_name,version,latest_ts): (expiry_epoch, InferenceResult)}
        self._result_cache_ttl = max(0.0, float(result_cache_ttl_ms)) / 1000.0
        self._result_cache: Dict[Tuple[str, Optional[int], float], Tuple[float, InferenceResult]] = {}
        logger.info(
            f"[ml] 推理服务初始化：label_type={self.feature_config.label_type}, "
            f"result_cache_ttl_ms={result_cache_ttl_ms}")

    # ─────────────────────────────────────────────
    #  模型加载 / 查询
    # ─────────────────────────────────────────────

    def load_model(
        self, model_name: str, version: Optional[int] = None
    ) -> Tuple[Any, ModelVersion]:
        """
        加载模型对象 + 版本元数据

        Parameters
        ----------
        model_name : str
            模型名称
        version : int | None
            指定版本（默认 active 版本，实盘热切换生效）

        Returns
        -------
        (model, ModelVersion) : 模型对象与元数据（含 feature_columns/train_end_time）
        """
        meta = self.registry.get_model_info(model_name, version)
        model = self.registry.load_model(model_name, version)
        logger.info(f"[ml] 推理加载模型: {meta.tag} (train_end={meta.train_end_time})")
        return model, meta

    def get_model_info(self, model_name: str, version: Optional[int] = None) -> ModelVersion:
        """查询模型版本信息"""
        return self.registry.get_model_info(model_name, version)

    def list_models(self) -> List[str]:
        """列出已注册模型"""
        return self.registry.list_models()

    # ─────────────────────────────────────────────
    #  特征引擎构建 + 对齐校验（防训练-推理偏移）
    # ─────────────────────────────────────────────

    def _build_engine(
        self, meta: ModelVersion, feature_config: Optional[FeatureConfig] = None
    ) -> Tuple[FeatureEngine, FeatureConfig]:
        """
        构建推理特征引擎，并校验特征列与训练绑定一致（核心防偏移）

        F-01 整改：优先使用注册 feature_signature（有序列名 + 顺序双向校验），
        无签名旧模型回退到静态 feature_columns 比对（兼容存量模型）。

        Raises
        ------
        FeatureMisalignmentError : 推理特征列与训练不一致
        """
        cfg = feature_config if feature_config is not None else self.feature_config
        engine = FeatureEngine(cfg)
        train_cols = list(meta.feature_columns or [])
        if meta.feature_signature is not None:
            # F-01：签名校验（列集合 + 顺序硬校验，dtype 软校验）
            from myquant.ml.feature_contract import (
                FeatureContractError,
                FeatureSignature,
                validate_feature_signature,
            )
            try:
                signature = FeatureSignature.from_dict(meta.feature_signature)
            except FeatureContractError as e:
                raise FeatureMisalignmentError(
                    f"模型 {meta.tag} 注册签名损坏: {e}")
            infer_cols = list(engine.feature_columns)
            try:
                validate_feature_signature(signature, infer_cols)
            except FeatureContractError as e:
                raise FeatureMisalignmentError(
                    f"推理特征列与训练签名不一致（模型 {meta.tag}）: {e}")
        elif train_cols:
            # 兼容无签名旧模型：静态列名集合比对
            infer_cols = list(engine.feature_columns)
            missing = sorted(set(train_cols) - set(infer_cols))
            extra = sorted(set(infer_cols) - set(train_cols))
            if missing or extra:
                raise FeatureMisalignmentError(
                    f"推理特征列与训练不一致（模型 {meta.tag}）: "
                    f"训练 {len(train_cols)} 列 vs 推理 {len(infer_cols)} 列，"
                    f"缺失={missing}，多余={extra}")
        return engine, cfg

    @staticmethod
    def _require_columns(feats: pd.DataFrame, train_cols: List[str]) -> pd.DataFrame:
        """按训练列顺序取特征（缺失列抛错）"""
        missing = [c for c in train_cols if c not in feats.columns]
        if missing:
            raise FeatureMisalignmentError(f"推理特征缺失列: {missing}")
        return feats[train_cols]

    # ─────────────────────────────────────────────
    #  时间工具 + 防未来守卫
    # ─────────────────────────────────────────────

    @staticmethod
    def _extract_timestamps(data: Any, n: int) -> List[float]:
        """
        提取输入时间戳（按时间升序，与特征行对齐）
        无时间戳时返回行号占位
        """
        if isinstance(data, pd.DataFrame) and "timestamp" in data.columns and len(data):
            return data["timestamp"].astype(float).sort_values().tolist()
        # P1-18：processed 中文列 DataFrame 无 timestamp，识别「日期」(int YYYYMMDD) 列
        if isinstance(data, pd.DataFrame) and "日期" in data.columns and len(data):
            return _dates_to_epoch(data["日期"])
        if isinstance(data, (list, tuple)) and data:
            ts = [getattr(b, "timestamp", None) for b in data]
            if all(t is not None for t in ts):
                return sorted(float(t) for t in ts)
        return list(range(n))

    @staticmethod
    def _latest_timestamp(data: Any) -> Optional[float]:
        """输入最新 bar 时间戳（无则 None）"""
        if isinstance(data, pd.DataFrame) and "timestamp" in data.columns and len(data):
            return float(data["timestamp"].astype(float).max())
        if isinstance(data, pd.DataFrame) and "日期" in data.columns and len(data):
            ts = _dates_to_epoch(data["日期"])
            return max(ts) if ts else None
        if isinstance(data, (list, tuple)) and data:
            t = getattr(data[-1], "timestamp", None)
            return float(t) if t is not None else None
        return None

    def _check_time_window(
        self, latest_time: float, meta: ModelVersion, strict: bool
    ) -> None:
        """
        train_end_time 防未来守卫

        输入最新时间 < 训练截止 → 说明预测发生在训练期内（样本内拟合），
        非真未来预测。strict=True 抛错；否则告警。

        Raises
        ------
        InferenceError : strict=True 且触发样本内预测
        """
        if not meta.train_end_time:
            return
        try:
            train_end_epoch = datetime.strptime(
                meta.train_end_time, "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, OSError, OverflowError):
            # 极端历史时间（如 1970 前）平台解析失败 → 跳过守卫（不影响主流程）
            return
        if latest_time < train_end_epoch:
            msg = (
                f"样本内预测（防未来）：输入最新时间 {latest_time} < "
                f"训练截止 {meta.train_end_time}（模型 {meta.tag}），"
                f"该预测实为训练期内拟合而非真未来预测")
            if strict:
                raise InferenceError(msg)
            logger.warning(f"[ml] {msg}")

    # ─────────────────────────────────────────────
    #  批量预测
    # ─────────────────────────────────────────────

    def predict_batch(
        self,
        data: Any,
        model_name: str,
        version: Optional[int] = None,
        feature_config: Optional[FeatureConfig] = None,
        strict: bool = False,
        return_proba: bool = False,
    ) -> InferenceResult:
        """
        批量预测：processed 数据 → 特征（只特征，不生成标签）→ 模型 → 预测

        Parameters
        ----------
        data : Bar 列表 / DataFrame
            已清洗 processed 行情数据（推理输入，禁止 raw）
        model_name : str
            模型名称
        version : int | None
            模型版本（默认 active）
        feature_config : FeatureConfig | None
            推理特征配置（须与训练一致；默认用服务配置并做对齐校验）
        strict : bool
            True 时样本内预测抛错（防未来拦截）；False 时仅告警
        return_proba : bool
            分类模型同时返回正类概率

        Returns
        -------
        InferenceResult : 预测 + 时间戳 + 模型绑定信息
        """
        model, meta = self.load_model(model_name, version)
        engine, _cfg = self._build_engine(meta, feature_config)
        # 防未来：只算特征，绝不生成标签（build_dataset 含未来标签，禁止用于推理）
        feats = engine.compute_features(data)
        if feats.empty:
            raise ModelNotReadyError("输入数据为空，无法预测")

        train_cols = list(meta.feature_columns or feats.columns)
        X = self._require_columns(feats, train_cols)
        valid = X.notna().all(axis=1)
        if int(valid.sum()) == 0:
            raise ModelNotReadyError("无有效特征行（窗口不足），无法预测")

        Xv = X[valid].values
        timestamps = self._extract_timestamps(data, len(feats))
        ts_valid = [float(timestamps[i]) for i in np.where(valid.values)[0]]
        if ts_valid:
            self._check_time_window(float(max(ts_valid)), meta, strict)

        pred = np.asarray(model.predict(Xv))
        proba = None
        if return_proba and callable(getattr(model, "predict_proba", None)):
            proba = np.asarray(model.predict_proba(Xv))
        label_type = "direction" if meta.label_column == "direction" else "return"
        return InferenceResult(
            predictions=pred, timestamps=ts_valid, model_name=meta.model_name,
            version=meta.version, model_type=meta.model_type, label_type=label_type,
            feature_columns=train_cols, train_end_time=meta.train_end_time,
            probabilities=proba, source=meta.source,
        )

    # ─────────────────────────────────────────────
    #  实时预测（逐 bar，实盘无缝对接）
    # ─────────────────────────────────────────────

    def predict_realtime(
        self,
        data: Any,
        model_name: str,
        version: Optional[int] = None,
        feature_config: Optional[FeatureConfig] = None,
        lookback: Optional[int] = None,
        strict: bool = False,
        return_proba: bool = False,
    ) -> InferenceResult:
        """
        实时预测：对最新 bar 用增量特征（compute_incremental）预测

        实盘场景：引擎启动时加载 processed 历史，之后每收到一根新 bar
        追加调用本方法 → 返回最新预测（与训练特征逐列一致）。

        Parameters
        ----------
        data : Bar 列表 / DataFrame
            历史 + 最新 bar（推理输入，禁止 raw）
        lookback : int | None
            显式截断最近 N 根重算（默认 None=全量，与训练精确一致）
        strict : bool
            True 时样本内预测抛错（防未来拦截）
        return_proba : bool
            分类模型同时返回正类概率

        Returns
        -------
        InferenceResult : 最新 bar 预测（n_samples=1）
        """
        model, meta = self.load_model(model_name, version)

        # F-14 短 TTL 结果缓存：同 (模型,版本,最新时间戳) 在 TTL 内复用
        latest_ts = self._latest_timestamp(data)
        if self._result_cache_ttl > 0 and latest_ts is not None:
            ckey = (model_name, meta.version, float(latest_ts))
            hit = self._result_cache.get(ckey)
            if hit is not None:
                expiry, cached = hit
                if time.time() < expiry:
                    logger.debug("[ml] 推理结果缓存命中: %s", ckey)
                    return cached
                else:
                    self._result_cache.pop(ckey, None)

        engine, _cfg = self._build_engine(meta, feature_config)
        # 防未来：增量特征（trailing），与训练最后一行逐列一致
        features = engine.compute_incremental(data, lookback=lookback)
        if not features:
            raise ModelNotReadyError("无特征可预测（数据不足）")

        train_cols = list(meta.feature_columns or list(features))
        missing = [c for c in train_cols if c not in features]
        if missing:
            raise FeatureMisalignmentError(f"推理特征缺失列: {missing}")
        # ✨【回写#93】F-06 推理兜底：窗口不足时增量特征可能为 None，
        # 先于 X 构造检出并抛明确错误（原实现 float(None) 抛 TypeError 不明确）
        none_feat = [c for c in train_cols if features.get(c) is None]
        if none_feat:
            raise ModelNotReadyError(
                f"最新 bar 特征缺失（窗口不足），无法预测: {none_feat}")
        X = np.array([[float(features[c]) for c in train_cols]])
        if np.isnan(X).any():
            raise ModelNotReadyError("最新 bar 特征含 NaN（窗口不足），无法预测")

        pred = np.asarray(model.predict(X)).ravel()
        proba = None
        if return_proba and callable(getattr(model, "predict_proba", None)):
            proba = np.asarray(model.predict_proba(X))
        latest_ts = self._latest_timestamp(data)
        if latest_ts is not None:
            self._check_time_window(float(latest_ts), meta, strict)

        label_type = "direction" if meta.label_column == "direction" else "return"
        result = InferenceResult(
            predictions=pred,
            timestamps=[latest_ts] if latest_ts is not None else [],
            model_name=meta.model_name, version=meta.version,
            model_type=meta.model_type, label_type=label_type,
            feature_columns=train_cols, train_end_time=meta.train_end_time,
            probabilities=proba, source=meta.source,
        )
        # F-14 写回结果缓存
        if self._result_cache_ttl > 0 and latest_ts is not None:
            ckey = (model_name, meta.version, float(latest_ts))
            self._result_cache[ckey] = (time.time() + self._result_cache_ttl, result)
        return result


__all__ = [
    "InferenceService",
    "InferenceResult",
    "InferenceError",
    "FeatureMisalignmentError",
    "ModelNotReadyError",
]
