# 文件名：myquant/ml/feature_contract.py
"""
feature_contract.py - 特征签名契约（训练-推理一致性硬保证）
================================================================
F-01 整改产物（2026-10-03，对应《因子与机器学习层优化实施报告》F-01）。

背景（问题链）:
  - 指标层（myquant/indicators）要求中文列契约（开盘价/最高价/最低价/收盘价），
    而 ML 特征层 bars_to_df 曾产出英文列（open/high/low/close），
    导致指标计算被静默跳过、训练实际特征列与静态列名漂移，
    推理侧对齐校验直接失败（FeatureMisalignmentError）或使用错误特征集。
  - F-01 修复：特征层统一中文列契约 + 指标失败显式抛错（禁止静默降级），
    并由本模块提供"训练时构建签名、推理前校验签名"的硬保证。

本模块职责:
  - FeatureSignature：训练时从实际特征 DataFrame 构建的有序特征签名
    （列名顺序 + 规范化 dtype + 特征配置 hash）
  - build_signature_from_frame()：从特征 DataFrame 构建签名
  - validate_feature_signature()：推理前双向校验（缺失/多余/顺序为硬校验，
    dtype 不一致为软校验告警），不一致抛 FeatureContractError

设计原则:
  - 硬校验只依赖"列集合 + 列顺序"，避免浮点类型细微差异误伤主链路
  - dtype 校验为软校验（日志告警），不阻断推理
  - 全量类型注解 + Google 风格 docstring + 防御式校验（py 文件代码规则）

依赖: numpy, pandas, myquant.core.logger
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger

logger = get_logger("ml.feature_contract")


# ═══════════════════════════════════════════════
#  异常类
# ═══════════════════════════════════════════════

class FeatureContractError(ValueError):
    """特征签名与训练不一致（训练-推理偏移拦截，禁止静默降级）"""


# ═══════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════

def _normalize_dtype(dtype: Any) -> str:
    """规范化 dtype 为字符串（float64/float32/int64/bool/object）。

    Args:
        dtype: numpy/pandas dtype 或可转换对象。

    Returns:
        规范化的 dtype 字符串；无法解析时返回原始字符串表示。
    """
    try:
        return str(np.dtype(dtype))
    except (TypeError, ValueError):  # 非 numpy 可解析对象（如 object 类型标签）
        return str(dtype)


# ═══════════════════════════════════════════════
#  FeatureSignature 特征签名
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class FeatureSignature:
    """
    特征签名（训练时构建，推理时校验）

    Attributes
    ----------
    columns : List[str]
        有序特征列名（训练实际特征列顺序）
    dtypes : Dict[str, str]
        列名 → 规范化 dtype（软校验用）
    config_hash : Optional[str]
        特征配置 hash（FeatureConfig.to_json 的 sha256）
    """

    columns: List[str]
    dtypes: Dict[str, str]
    config_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 JSON 安全 dict（注册元数据持久化用）"""
        return {
            "columns": list(self.columns),
            "dtypes": dict(self.dtypes),
            "config_hash": self.config_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeatureSignature":
        """从 dict 反序列化（缺失字段用空值，防御式校验）"""
        if not isinstance(data, dict):
            raise FeatureContractError(f"非法签名数据: {type(data)!r}")
        columns = [str(c) for c in (data.get("columns") or [])]
        if not columns:
            raise FeatureContractError("签名缺少特征列（columns 为空）")
        dtypes = {str(k): str(v) for k, v in (data.get("dtypes") or {}).items()}
        config_hash = data.get("config_hash")
        return cls(
            columns=columns,
            dtypes=dtypes,
            config_hash=str(config_hash) if config_hash is not None else None,
        )

    def __len__(self) -> int:
        return len(self.columns)

    def __repr__(self) -> str:
        return (
            f"FeatureSignature(n_cols={len(self.columns)}, "
            f"config_hash={self.config_hash})"
        )


# ═══════════════════════════════════════════════
#  构建与校验
# ═══════════════════════════════════════════════

def build_signature_from_frame(
    features: pd.DataFrame,
    config_hash: Optional[str] = None,
) -> FeatureSignature:
    """从特征 DataFrame 构建签名（列顺序 = DataFrame 列顺序）。

    Args:
        features: 训练实际特征矩阵（FeatureDataset.X）。
        config_hash: 特征配置 hash（可选）。

    Returns:
        FeatureSignature: 包含列顺序与 dtype 的签名。

    Raises:
        FeatureContractError: features 为空或无语义列。
    """
    if features is None or features.empty:
        raise FeatureContractError("特征 DataFrame 为空，无法构建签名")
    columns = [str(c) for c in features.columns]
    if not columns:
        raise FeatureContractError("特征 DataFrame 无语义列，无法构建签名")
    dtypes: Dict[str, str] = {}
    for c in features.columns:
        dtypes[str(c)] = _normalize_dtype(features[c].dtype)
    return FeatureSignature(columns=columns, dtypes=dtypes, config_hash=config_hash)


def validate_feature_signature(
    signature: FeatureSignature,
    actual_columns: Sequence[str],
    actual_dtypes: Optional[Dict[str, Any]] = None,
) -> None:
    """校验实际特征列与签名一致（训练-推理偏移硬拦截）。

    硬校验（不一致抛 FeatureContractError）:
      - 列集合双向一致（缺失 / 多余）
      - 列顺序一致（集合一致但顺序不同同样拦截）
    软校验（不一致仅日志告警，不阻断）:
      - dtype 差异（避免浮点类型细节误伤主链路）

    Args:
        signature: 训练签名。
        actual_columns: 推理实际特征列。
        actual_dtypes: 推理实际 dtype 映射（可选，软校验用）。

    Raises:
        FeatureContractError: 列集合或顺序与签名不一致。
    """
    if signature is None:
        raise FeatureContractError("签名为空，无法校验")
    sig_cols = list(signature.columns)
    actual = [str(c) for c in actual_columns]
    if sig_cols != actual:
        missing = sorted(set(sig_cols) - set(actual))
        extra = sorted(set(actual) - set(sig_cols))
        details: List[str] = []
        if missing:
            details.append(f"缺失={missing}")
        if extra:
            details.append(f"多余={extra}")
        if not missing and not extra:
            details.append("列集合一致但顺序不同")
        raise FeatureContractError(
            f"特征签名校验失败（模型训练 {len(sig_cols)} 列 vs 实际 {len(actual)} 列）: "
            + "，".join(details)
        )
    # 软校验：dtype 不一致仅告警（不阻断，避免浮点类型差异误报）
    if actual_dtypes:
        for c in sig_cols:
            sig_dt = signature.dtypes.get(c)
            if not sig_dt:
                continue
            act_dt = _normalize_dtype(actual_dtypes.get(c))
            if act_dt != sig_dt:
                logger.warning(
                    "[ml] 特征列 %s dtype 不一致: 训练 %s vs 实际 %s（软校验，不阻断）",
                    c, sig_dt, act_dt,
                )


__all__ = [
    "FeatureContractError",
    "FeatureSignature",
    "build_signature_from_frame",
    "validate_feature_signature",
]
