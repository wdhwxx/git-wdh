# 文件名：myquant/ml/__init__.py
"""
ml/__init__.py - 机器学习模块
=============================
ML 完整 MLOps 链路（阶段9 第五组，任务18-21）：
  - 任务18 features.py          特征工程（因子计算 + 标签生成）✅
  - 任务19 model_registry.py    模型注册版本管理 ✅
  - 任务20 training_pipeline.py 训练流水线 ✅
  - 任务21 inference_service.py 批量/实时预测服务 ✅

铁律（总图 myquant/ml/ 注释）:
  - 输入只能读取 processed 清洗后数据，禁止读取 raw 原始数据
  - 严格禁用未来函数：特征 trailing 窗口（仅用 <=t），标签仅训练集使用，
    模型元数据记录 train_end_time 训练数据截止（推理只用 <= 截止数据）
  - 所有文件开头标注文件名，入口脚本用中文名

当前导出（任务18-21）:
  - FeatureConfig     特征配置（训练/推理共用，JSON 可序列化）
  - FeatureEngine     特征引擎（compute_features/generate_labels/build_dataset/compute_incremental/lookahead_guard）
  - FeatureDataset    特征数据集（X/y/timestamps）
  - bars_to_df        Bar 列表 → 标准化 DataFrame
  - ModelRegistry     模型注册版本管理（register/load/activate/rollback/delete/verify）
  - ModelVersion      模型版本元数据
  - 异常类            ModelRegistryError/ModelNotFoundError/ModelVersionNotFoundError/ModelIntegrityError
  - TrainingPipeline  训练流水线（run/retrain/split/build_dataset，注册闭环）
  - TrainingResult    训练结果（模型/版本/指标/训练范围）
  - LinearRegressor   内置岭回归（numpy 手写，可插拔）
  - LogisticRegressor 内置逻辑回归（numpy 手写二分类，可插拔）
  - model_factory     可插拔模型工厂 / time_series_split 防未来切分
  - evaluate_model / regression_metrics / classification_metrics  评估指标
  - 异常类            TrainingError
  - InferenceService  批量/实时预测服务（predict_batch/predict_realtime，防未来推理）
  - InferenceResult   推理结果（预测/时间戳/概率/版本/训练截止）
  - 异常类            InferenceError/FeatureMisalignmentError/ModelNotReadyError

依赖: pandas, numpy, myquant.indicators, myquant.utils.file_utils
"""

from myquant.ml.features import (
    DEFAULT_DERIVED_FEATURES,
    DEFAULT_INDICATOR_CONFIGS,
    FeatureConfig,
    FeatureDataset,
    FeatureEngine,
    bars_to_df,
)
from myquant.ml.inference_service import (
    FeatureMisalignmentError,
    InferenceError,
    InferenceResult,
    InferenceService,
    ModelNotReadyError,
)
from myquant.ml.model_registry import (
    ModelIntegrityError,
    ModelNotFoundError,
    ModelRegistry,
    ModelRegistryError,
    ModelVersion,
    ModelVersionNotFoundError,
)
from myquant.ml.training_pipeline import (
    LinearRegressor,
    LogisticRegressor,
    TrainingError,
    TrainingPipeline,
    TrainingResult,
    classification_metrics,
    evaluate_model,
    model_factory,
    regression_metrics,
    time_series_split,
)

__version__ = "0.1.0"

__all__ = [
    # 任务18 特征工程
    "FeatureConfig",
    "FeatureEngine",
    "FeatureDataset",
    "bars_to_df",
    "DEFAULT_INDICATOR_CONFIGS",
    "DEFAULT_DERIVED_FEATURES",
    # 任务19 模型注册版本管理
    "ModelRegistry",
    "ModelVersion",
    "ModelRegistryError",
    "ModelNotFoundError",
    "ModelVersionNotFoundError",
    "ModelIntegrityError",
    # 任务20 训练流水线
    "TrainingPipeline",
    "TrainingResult",
    "TrainingError",
    "LinearRegressor",
    "LogisticRegressor",
    "model_factory",
    "time_series_split",
    "evaluate_model",
    "regression_metrics",
    "classification_metrics",
    # 任务21 批量/实时预测服务
    "InferenceService",
    "InferenceResult",
    "InferenceError",
    "FeatureMisalignmentError",
    "ModelNotReadyError",
]
