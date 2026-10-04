# 文件名：myquant/ml/model_registry.py
"""
model_registry.py - 模型注册版本管理
====================================
模型注册版本管理（任务19，ML 完整 MLOps 链路第二环）。

核心能力:
  - register_model()    注册模型（版本自动递增 + 工件落盘 + 元数据 + checksum + 索引双写）
  - load_model()        加载模型对象（F-07：先验 HMAC 签名再验 checksum 完整性）
  - activate_version()  实盘热切换 active 版本
  - rollback()          回滚到前一版本（记录 last_rollback）
  - list/get/delete/verify 版本管理全能力

存储布局（对齐任务14 数据集版本管理成熟模式）:
  datasets/models/{model_name}/versions/
  ├── index.json            # 版本索引（head/active/last_rollback/versions 摘要）
  └── v{n}/
      ├── model.pkl         # 模型工件（pickle 标准库序列化，不绑定 ML 框架）
      └── metadata.json     # 版本元数据（ModelVersion，F-07 含 HMAC 签名）

数据来源（铁律）:
  - 本模块只管理模型工件文件，不读取行情数据（天然满足 ml/ 禁读 raw 约束）
  - 训练数据截止时间 train_end_time 记录在元数据中，供任务21 推理服务
    校验"只用 <= 训练截止数据"（防未来）

防未来函数（铁律4）:
  - train_end_time 记录训练数据截止（推理侧只使用 <= 截止数据）
  - data_snapshot 只登记训练时已得数据事实（指纹/行数/起止时间），无未来引用
  - 本模块无任何预测方法/属性
  - 无硬编码未来日期
  - load_model/verify_model 做签名+checksum 双重完整性校验（工件与元数据篡改均可检出）

F-07 元数据补全与签名（回写#94）:
  - ModelVersion 新增 data_snapshot/runtime_env/model_params_count 补全字段
  - 元数据 HMAC-SHA256 签名（密钥=环境变量 MYQUANT_MODEL_SIG_KEY，不入库/入 git；
    无密钥时降级跳过签名并告警=默认零变化）
  - load_model 校验顺序 = 签名校验 → checksum 校验（杜绝"连带重算"式篡改）
  - 新增 ModelSignatureError（元数据签名被篡改）

为实盘无缝对接（任务21-23 实盘增强前置）:
  - active 版本热切换（activate_version）：实盘模型不停机切换
  - 多版本并存：A/B 测试、回滚
  - source 区分 offline/live
  - 线程锁 + 跨进程文件锁（实盘多进程预留）
  - 模型与训练特征绑定（feature_config_hash + feature_columns，与任务18 对齐）

依赖: myquant.utils.file_utils, 标准库（pickle/json/threading/hashlib/hmac/os/platform）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pickle
import platform
import threading
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from myquant.utils.file_utils import (
    ensure_dir,
    file_lock,
    get_project_path,
    read_json,
    write_json,
)

try:
    from myquant.core.logger import get_logger
except ImportError:  # pragma: no cover
    def get_logger(name: str) -> Any:
        import logging
        return logging.getLogger(name)

logger = get_logger("ml.model_registry")


# ═══════════════════════════════════════════════
#  F-07 签名相关常量（回写#94）
# ═══════════════════════════════════════════════

# 签名密钥环境变量名（密钥不得入库/入 git；无密钥时降级跳过签名并告警）
ENV_SIG_KEY: str = "MYQUANT_MODEL_SIG_KEY"
# 签名算法标识（元数据签名，与 F-01 特征列契约签名区分：F-07 是 HMAC 完整性签名）
SIGNATURE_ALGORITHM: str = "hmac-sha256"
# 签名自身字段（不参与被签名内容，避免自指）
SIGNATURE_FIELDS: Tuple[str, ...] = ("signature", "signature_algorithm", "signed_at")


# ═══════════════════════════════════════════════
#  异常类
# ═══════════════════════════════════════════════

class ModelRegistryError(Exception):
    """模型注册通用错误"""


class ModelNotFoundError(ModelRegistryError):
    """模型不存在"""


class ModelVersionNotFoundError(ModelRegistryError):
    """模型版本不存在"""


class ModelIntegrityError(ModelRegistryError):
    """模型工件完整性校验失败（checksum 不匹配）"""


class ModelSignatureError(ModelRegistryError):
    """模型元数据签名校验失败（HMAC 不匹配，元数据被篡改）"""


class ModelRetiredError(ModelRegistryError):
    """模型版本已失效（retired），禁止调用"""


# ═══════════════════════════════════════════════
#  ModelVersion 版本元数据
# ═══════════════════════════════════════════════

@dataclass
class ModelVersion:
    """
    模型版本元数据（与 metadata.json 对齐）

    Attributes
    ----------
    model_name : str
        模型名称（目录安全化后作为存储目录名）
    version : int
        版本号（自增，从1开始）
    model_type : str
        模型类型（type(model).__module__ + type(model).__name__）
    artifact : str
        工件文件名（默认 model.pkl）
    checksum : str
        工件 sha256（完整性校验）
    created_at : str
        创建时间（YYYY-MM-DD HH:MM:SS）
    source : str
        来源：offline（离线训练）/ live（实盘）
    feature_config_hash : Optional[str]
        训练特征配置 hash（FeatureConfig JSON sha256，训练/推理特征绑定）
    feature_columns : List[str]
        训练特征列名（与任务18 对齐，推理必须一致）
    label_column : Optional[str]
        标签列名
    train_start_time : Optional[str]
        训练数据起始时间
    train_end_time : Optional[str]
        训练数据截止时间（防未来：推理只使用 <= 截止数据）
    metrics : Dict[str, Any]
        评估指标（accuracy/f1/sharpe 等，任务20 训练产出）
    hyperparameters : Dict[str, Any]
        超参数
    status : str
        状态：active（当前生效）/ archived（归档）/ retired（失效，禁止调用，
        F-05 失效监控自动标记并降级）
    parent_version : Optional[int]
        派生链：从哪个版本训练而来
    description : str
        描述
    data_snapshot : Optional[Dict[str, Any]]
        F-07 训练数据快照引用：fingerprint（训练数据 X/y 内容 sha256）/ 行数 /
        起止时间 / data_version（预留，独立 data_versioner 接入后回填）；
        只登记训练时已得数据事实（防未来）
    runtime_env : Optional[Dict[str, Any]]
        F-07 运行环境依赖：注册时点 python/numpy/pandas 版本（可追溯环境版本）
    model_params_count : Optional[int]
        F-07 模型参数量（防御式计算；不可计算记 None 并告警，不阻断注册）
    signature : Optional[str]
        F-07 元数据 HMAC-SHA256 签名（密钥=环境变量 MYQUANT_MODEL_SIG_KEY；
        无密钥时为 None=未签名）
    signature_algorithm : Optional[str]
        F-07 签名算法标识（hmac-sha256）
    signed_at : Optional[str]
        F-07 签名时间戳（YYYY-MM-DD HH:MM:SS，系统当前时间）
    """
    model_name: str
    version: int
    model_type: str = "unknown"
    artifact: str = "model.pkl"
    checksum: str = ""
    created_at: str = ""
    source: str = "offline"
    feature_config_hash: Optional[str] = None
    feature_columns: List[str] = field(default_factory=list)
    feature_signature: Optional[Dict[str, Any]] = None
    label_column: Optional[str] = None
    train_start_time: Optional[str] = None
    train_end_time: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    hyperparameters: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending_review"  # KP-P6-05: 未批准不 active
    parent_version: Optional[int] = None
    description: str = ""
    data_snapshot: Optional[Dict[str, Any]] = None
    runtime_env: Optional[Dict[str, Any]] = None
    model_params_count: Optional[int] = None
    signature: Optional[str] = None
    signature_algorithm: Optional[str] = None
    signed_at: Optional[str] = None
    # KP-P6-05: 模型治理字段
    tier: str = "dev"                      # 模型分级（dev/staging/prod）
    model_card: Optional[Dict[str, Any]] = None  # 模型卡（用途/限制/数据口径）
    frozen: bool = False                    # 冻结（批准后禁止训练覆盖，防漂移篡改）
    approval: Optional[Dict[str, Any]] = None   # 审批记录 {approver, at, note}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_name": self.model_name,
            "version": self.version,
            "model_type": self.model_type,
            "artifact": self.artifact,
            "checksum": self.checksum,
            "created_at": self.created_at,
            "source": self.source,
            "feature_config_hash": self.feature_config_hash,
            "feature_columns": list(self.feature_columns),
            "feature_signature": self.feature_signature,
            "label_column": self.label_column,
            "train_start_time": self.train_start_time,
            "train_end_time": self.train_end_time,
            "metrics": self.metrics,
            "hyperparameters": self.hyperparameters,
            "status": self.status,
            "parent_version": self.parent_version,
            "description": self.description,
            "data_snapshot": self.data_snapshot,
            "runtime_env": self.runtime_env,
            "model_params_count": self.model_params_count,
            "signature": self.signature,
            "signature_algorithm": self.signature_algorithm,
            "signed_at": self.signed_at,
            "tier": self.tier,
            "model_card": self.model_card,
            "frozen": self.frozen,
            "approval": self.approval,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelVersion":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def tag(self) -> str:
        """版本标签，如 model@v1"""
        return f"{self.model_name}@v{self.version}"

    def __repr__(self) -> str:
        return (
            f"ModelVersion({self.tag}, type={self.model_type}, "
            f"source={self.source}, status={self.status})"
        )


# ═══════════════════════════════════════════════
#  ModelRegistry 模型注册表
# ═══════════════════════════════════════════════

class ModelRegistry:
    """
    模型注册版本管理（任务19）

    存储布局:
      datasets/models/{model_name}/versions/
      ├── index.json            # 版本索引（head.active/head.latest/last_rollback/versions 摘要）
      └── v{n}/
          ├── model.pkl         # 模型工件
          └── metadata.json     # ModelVersion 元数据

    并发安全: 线程锁 + 跨进程文件锁（实盘多进程预留）
    """

    def __init__(self, models_root: Optional[str] = None):
        """
        Parameters
        ----------
        models_root : str | None
            模型根目录（默认 datasets/models）
        """
        self._lock = threading.RLock()
        self._models_root = (
            Path(models_root)
            if models_root else get_project_path("datasets", "models")
        )
        ensure_dir(self._models_root)
        logger.info(f"[ml] 模型注册表初始化: {self._models_root}")

    # ─────────────────────────────────────────────
    #  路径工具
    # ─────────────────────────────────────────────

    @staticmethod
    def _safe_name(model_name: str) -> str:
        """模型名目录安全化（非法字符替换为下划线）"""
        cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in model_name)
        return cleaned.strip("._") or "model"

    def _model_root(self, model_name: str) -> Path:
        return self._models_root / self._safe_name(model_name)

    def _versions_root(self, model_name: str) -> Path:
        return self._model_root(model_name) / "versions"

    def _version_dir(self, model_name: str, version: int) -> Path:
        return self._versions_root(model_name) / f"v{version}"

    def _artifact_path(self, model_name: str, version: int, artifact: str = "model.pkl") -> Path:
        return self._version_dir(model_name, version) / artifact

    def _metadata_path(self, model_name: str, version: int) -> Path:
        return self._version_dir(model_name, version) / "metadata.json"

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _file_sha256(path: Path) -> str:
        """计算文件 sha256"""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _feature_config_hash(feature_config: Any) -> str:
        """计算训练特征配置 hash（FeatureConfig JSON / dict / 任意对象）"""
        if hasattr(feature_config, "to_json"):
            payload = feature_config.to_json()
        elif isinstance(feature_config, dict):
            payload = json.dumps(feature_config, sort_keys=True, ensure_ascii=False, default=str)
        else:
            payload = str(feature_config)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ─────────────────────────────────────────────
    #  F-07 元数据签名 / 补全（回写#94）
    # ─────────────────────────────────────────────

    @staticmethod
    def _sig_key() -> Optional[str]:
        """
        读取签名密钥（环境变量 ENV_SIG_KEY）。

        密钥来源为环境变量（不入库/入 git）；缺失返回 None=未启用签名（降级模式）。
        """
        key = os.environ.get(ENV_SIG_KEY, "").strip()
        return key if key else None

    @staticmethod
    def _canonical_json(payload: Dict[str, Any]) -> str:
        """元数据 canonical JSON（sort_keys + ensure_ascii=False，字段顺序无关、稳定可复算）。"""
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)

    @classmethod
    def _sign_metadata(cls, meta_dict: Dict[str, Any]) -> Tuple[str, str, str]:
        """
        对元数据计算 HMAC-SHA256 签名（剔除签名自字段，避免自指）。

        Returns
        -------
        (signature_hex, algorithm, signed_at) : 签名值 / 算法标识 / 签名时间戳
        """
        key = cls._sig_key()
        if not key:
            raise ModelRegistryError("签名密钥缺失，无法计算元数据签名")
        payload = {k: v for k, v in meta_dict.items() if k not in SIGNATURE_FIELDS}
        canonical = cls._canonical_json(payload)
        signature = hmac.new(
            key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        signed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return signature, SIGNATURE_ALGORITHM, signed_at

    @classmethod
    def _verify_metadata_signature(cls, raw_meta: Dict[str, Any]) -> None:
        """
        校验元数据 HMAC 签名（读取侧，本行数据全部来自磁盘 metadata.json，截至加载时刻）。

        校验规则（防御式降级）：
          - 元数据无签名（旧版本/未签名注册）→ 跳过签名校验（兼容存量）；
          - 有签名但密钥缺失 → 无法校验，告警并跳过（默认零变化降级）；
          - 有签名且密钥可用 → 签名不匹配抛 ModelSignatureError（元数据被篡改）。

        Raises
        ------
        ModelSignatureError : 签名不匹配（元数据被篡改）
        """
        signature = raw_meta.get("signature") if isinstance(raw_meta, dict) else None
        if not signature:
            return  # 未签名元数据：跳过（存量/无密钥注册兼容）
        key = cls._sig_key()
        if not key:
            logger.warning(
                "[ml] 元数据已签名但密钥缺失（未设置 %s），跳过签名校验（降级）",
                ENV_SIG_KEY,
            )
            return
        payload = {k: v for k, v in raw_meta.items() if k not in SIGNATURE_FIELDS}
        canonical = cls._canonical_json(payload)
        expect = hmac.new(
            key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expect, str(signature)):
            raise ModelSignatureError(
                "模型元数据签名校验失败（元数据可能被篡改，拒绝加载）"
            )

    @staticmethod
    def _capture_runtime_env() -> Dict[str, str]:
        """
        采集注册时点运行环境版本（python/numpy/pandas）。

        版本来自注册发生时刻的真实环境（importlib.metadata 查询，不导入第三方模块），
        无未来信息；单个包查询失败记为 "unknown" 不阻断。
        """
        def _pkg_version_safe(name: str) -> str:
            try:
                return str(_pkg_version(name))
            except Exception:  # pragma: no cover - 包未安装/元数据异常
                return "unknown"

        return {
            "python": platform.python_version(),
            "numpy": _pkg_version_safe("numpy"),
            "pandas": _pkg_version_safe("pandas"),
        }

    @staticmethod
    def _count_model_params(model: Any) -> Optional[int]:
        """
        防御式计算模型参数量（常见数值参数属性求和）。

        统计 coef_/feature_importances_/weights/estimators_/support_vectors_
        等常见属性的元素个数；算不出（属性缺失/类型不可计数）返回 None 并告警，
        不阻断注册（防御式：元数据补全尽力而为，缺省不阻断训练闭环）。

        Returns
        -------
        Optional[int] : 参数量；不可计算返回 None
        """
        attr_candidates: Tuple[str, ...] = (
            "coef_", "feature_importances_", "weights",
            "estimators_", "support_vectors_", "intercept_",
        )
        total: int = 0
        found: bool = False
        for attr in attr_candidates:
            obj = getattr(model, attr, None)
            if obj is None:
                continue
            try:
                size = getattr(obj, "size", None)
                if isinstance(size, int) and size > 0:
                    total += size
                    found = True
                    continue
                # 非数组类（如 list/tuple）按 len 计
                if isinstance(obj, (list, tuple)):
                    total += len(obj)
                    found = True
            except Exception:  # pragma: no cover - 属性计数异常防御
                continue
        if not found:
            # dict 模型兜底（如 {"coef": [...]}）
            if isinstance(model, dict):
                for key, val in model.items():
                    if not isinstance(key, str):
                        continue
                    try:
                        size = getattr(val, "size", None)
                        if isinstance(size, int) and size > 0:
                            total += size
                            found = True
                    except Exception:  # pragma: no cover
                        continue
        if not found:
            logger.warning("[ml] 模型参数量不可计算，登记 None（不阻断注册）")
            return None
        return total

    def _resolve_feature_columns(self, metadata: Optional[Dict], feature_config: Any) -> List[str]:
        """
        解析特征列名（metadata 显式优先，其次 FeatureConfig/FeatureEngine）

        feature_config 支持：
          - FeatureConfig 对象：用 FeatureEngine(config).feature_columns 预演
          - FeatureEngine 对象 / 带 feature_columns 属性的对象：直接读取

        F-01 补充：metadata 显式 feature_columns 优先（训练实际列）；
        feature_columns 缺失时回退到 feature_signature.columns（有序列名）。
        """
        if metadata and isinstance(metadata.get("feature_columns"), list) and metadata["feature_columns"]:
            return [str(c) for c in metadata["feature_columns"]]
        # F-01：feature_signature 回退（训练实际列，与注册签名一致）
        if metadata and isinstance(metadata.get("feature_signature"), dict):
            sig_cols = metadata["feature_signature"].get("columns")
            if isinstance(sig_cols, list) and sig_cols:
                return [str(c) for c in sig_cols]
        if feature_config is not None:
            # 已带 feature_columns 属性（如 FeatureEngine）→ 直接读取
            if hasattr(feature_config, "feature_columns"):
                cols = list(getattr(feature_config, "feature_columns", []) or [])
                if cols:
                    return cols
            # FeatureConfig 对象 → 用 FeatureEngine 预演静态列名清单
            try:
                from myquant.ml.features import FeatureEngine
                engine = FeatureEngine(feature_config)
                cols = engine.feature_columns
                if cols:
                    return cols
            except Exception as e:  # pragma: no cover
                logger.warning(f"[ml] 特征列预演失败: {e}")
        return []

    # ─────────────────────────────────────────────
    #  索引读写
    # ─────────────────────────────────────────────

    def _read_index(self, model_name: str) -> Dict[str, Any]:
        """读取版本索引（不存在或损坏则返回空索引）"""
        index_path = self._versions_root(model_name) / "index.json"
        if not index_path.exists():
            return {"model_name": model_name, "updated_at": None, "head": {},
                    "last_rollback": None, "versions": {}}
        try:
            index = read_json(index_path)
            index.setdefault("model_name", model_name)
            index.setdefault("head", {})
            index.setdefault("last_rollback", None)
            index.setdefault("versions", {})
            index.setdefault("updated_at", None)
            return index
        except Exception:
            return {"model_name": model_name, "updated_at": None, "head": {},
                    "last_rollback": None, "versions": {}}

    def _write_index(self, model_name: str, index: Dict[str, Any]) -> None:
        """写版本索引（线程锁 + 跨进程文件锁保护，原子写）"""
        root = ensure_dir(self._versions_root(model_name))
        index_path = root / "index.json"
        index["updated_at"] = self._now()
        try:
            with file_lock(str(root / ".index.lock"), timeout=5.0):
                write_json(str(index_path), index)
        except Exception:
            write_json(str(index_path), index)

    @staticmethod
    def _next_version(index: Dict[str, Any]) -> int:
        """计算下一个版本号（当前最大+1，空则1）"""
        existing = [int(v) for v in index.get("versions", {}).keys()]
        return (max(existing) + 1) if existing else 1

    def _summary(self, meta: ModelVersion) -> Dict[str, Any]:
        """版本摘要（存入 index.versions）"""
        return {
            "version": meta.version,
            "model_type": meta.model_type,
            "created_at": meta.created_at,
            "source": meta.source,
            "status": meta.status,
            "train_end_time": meta.train_end_time,
            "checksum": meta.checksum,
            "description": meta.description,
        }

    # ─────────────────────────────────────────────
    #  注册
    # ─────────────────────────────────────────────

    def register_model(
        self,
        model_name: str,
        model: Any,
        metadata: Optional[Dict[str, Any]] = None,
        feature_config: Any = None,
        version: Optional[int] = None,
        source: str = "offline",
        description: str = "",
        require_win_gate: bool = False,
        win_metric: str = "ic",
    ) -> ModelVersion:
        """
        注册模型（版本自增 + 工件落盘 + 元数据 + checksum + 索引双写）

        Parameters
        ----------
        model_name : str
            模型名称（存储目录名，自动安全化）
        model : Any
            模型对象（pickle 序列化，任意可 pickle 对象；任务20 训练产出直接注册）
        metadata : dict | None
            版本元数据补充字段：
              metrics（评估指标）/ hyperparameters（超参数）/
              train_start_time / train_end_time（训练数据截止，防未来）/
              feature_columns / label_column / parent_version
        feature_config : FeatureConfig | None
            训练特征配置（自动计算 feature_config_hash + feature_columns，与任务18 对齐）
        version : int | None
            显式指定版本号（默认自增）
        source : str
            offline（离线训练）/ live（实盘）
        description : str
            版本描述
        require_win_gate : bool
            F-05：是否要求"新版本必须跑赢当前 active"方可激活
            （默认 False 零变化；True 时新旧版本均有 win_metric 指标才比较，
            新 < 旧 则注册为 archived 不激活并告警，缺指标跳过门控并告警）
        win_metric : str
            F-05：门控比较指标（默认 "ic"，从 metadata.metrics 取值）

        Returns
        -------
        ModelVersion : 注册后的版本元数据
        """
        if not model_name or not model_name.strip():
            raise ModelRegistryError("模型名称不能为空")
        metadata = dict(metadata or {})
        with self._lock:
            index = self._read_index(model_name)
            ver = version if version is not None else self._next_version(index)
            if str(ver) in index.get("versions", {}):
                raise ModelRegistryError(f"版本 {ver} 已存在，请显式指定新版本号")

            vdir = ensure_dir(self._version_dir(model_name, ver))
            artifact = "model.pkl"
            apath = vdir / artifact
            # 工件落盘（pickle）
            with open(apath, "wb") as f:
                pickle.dump(model, f)
            checksum = self._file_sha256(apath)

            # 解析特征列与配置 hash
            feature_columns = self._resolve_feature_columns(metadata, feature_config)
            feature_signature = metadata.get("feature_signature")
            if feature_signature is not None and not isinstance(feature_signature, dict):
                logger.warning("[ml] 非法 feature_signature 类型 %s，忽略持久化",
                               type(feature_signature).__name__)
                feature_signature = None
            fcfg_hash = None
            if feature_config is not None:
                fcfg_hash = self._feature_config_hash(feature_config)
            elif metadata.get("feature_config_hash"):
                fcfg_hash = str(metadata["feature_config_hash"])

            meta = ModelVersion(
                model_name=self._safe_name(model_name),
                version=ver,
                model_type=f"{type(model).__module__}.{type(model).__name__}",
                artifact=artifact,
                checksum=checksum,
                created_at=self._now(),
                source=source,
                feature_config_hash=fcfg_hash,
                feature_columns=feature_columns,
                feature_signature=feature_signature,
                label_column=metadata.get("label_column"),
                train_start_time=metadata.get("train_start_time"),
                train_end_time=metadata.get("train_end_time"),
                metrics=metadata.get("metrics") or {},
                hyperparameters=metadata.get("hyperparameters") or {},
                status="pending_review",  # KP-P6-05: 未批准不 active，需 approve_version 审批
                tier=str(metadata.get("tier") or "dev"),
                model_card=(metadata.get("model_card") if isinstance(metadata.get("model_card"), dict) else None),
                frozen=bool(metadata.get("frozen", False)),
                approval=(metadata.get("approval") if isinstance(metadata.get("approval"), dict) else None),
                parent_version=metadata.get("parent_version"),
                description=description,
                # F-07 元数据补全（回写#94）：
                # data_snapshot 只登记训练时已得数据事实（训练管线计算后传入；
                # 直接调 registry 时缺省 None 不阻断）
                data_snapshot=(
                    metadata.get("data_snapshot")
                    if isinstance(metadata.get("data_snapshot"), dict) else None
                ),
                # runtime_env：调用方显式传入优先，缺省自动采集注册时点环境版本
                runtime_env=(
                    metadata.get("runtime_env")
                    if isinstance(metadata.get("runtime_env"), dict)
                    else self._capture_runtime_env()
                ),
                # model_params_count：显式传入优先，缺省防御式计算（不可计算 None+告警）
                model_params_count=(
                    metadata.get("model_params_count")
                    if isinstance(metadata.get("model_params_count"), int)
                    else self._count_model_params(model)
                ),
            )
            # F-05：注册门控（require_win_gate 时新版本必须跑赢 active 方可激活）
            gate_passed = True
            if require_win_gate:
                gate_passed = self._win_gate_passes(index, ver, metadata, win_metric)
            if not gate_passed:
                meta.status = "archived"
            elif require_win_gate:
                # KP-P6-05：win_gate 门控即自动审批——通过则激活并留 approval 痕迹
                # （默认 require_win_gate=False 保持 pending_review，未批准不 active）
                meta.status = "active"
                meta.approval = {
                    "approver": "win_gate",
                    "at": meta.created_at,
                    "note": "win gate 自动审批（require_win_gate 通过）",
                }
            # F-07 元数据签名（回写#94）：密钥可用时对完整元数据（剔除签名自字段）
            # 计算 HMAC-SHA256 并落盘；无密钥时降级=不签名+告警（默认零变化）。
            # 签名覆盖 checksum/status 等全部字段——篡改任意字段（含 checksum 字段）
            # 都会在加载时被签名层拦截，杜绝"连带重算"式篡改。
            if self._sig_key():
                signature, algo, signed_at = self._sign_metadata(meta.to_dict())
                meta.signature = signature
                meta.signature_algorithm = algo
                meta.signed_at = signed_at
            else:
                logger.warning(
                    "[ml] 未设置 %s，元数据不签名（降级模式；加载时跳过签名校验）",
                    ENV_SIG_KEY,
                )
            # metadata.json 落盘
            write_json(str(self._metadata_path(model_name, ver)), meta.to_dict())
            # 索引双写（新增版本 + 该版本为最新；仅 active 状态激活）
            index.setdefault("versions", {})[str(ver)] = self._summary(meta)
            head = index.setdefault("head", {})
            head["latest"] = ver
            if meta.status == "active":
                head["active"] = ver
            else:
                logger.warning(
                    "[ml] 新版本 %s@v%d 注册为 %s 不激活（KP-P6-05 未批准不 active）",
                    meta.model_name, ver, meta.status,
                )
            self._write_index(model_name, index)
            logger.info(f"[ml] 模型注册成功: {meta.tag} ({meta.model_type})")
            return meta

    # ─────────────────────────────────────────────
    #  F-05 注册门控
    # ─────────────────────────────────────────────

    @staticmethod
    def _metric_value(metrics: Dict[str, Any], name: str) -> Optional[float]:
        """从绩效 dict 取数值指标（缺失/非法返回 None，防御式）。"""
        raw = metrics.get(name) if isinstance(metrics, dict) else None
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _win_gate_passes(
        self,
        index: Dict[str, Any],
        new_version: int,
        metadata: Dict[str, Any],
        win_metric: str,
    ) -> bool:
        """注册门控：新版本 win_metric 必须 >= 当前 active 版本方可通过。

        无 active 基准 → 通过；新旧缺指标 → 跳过门控（不阻断）并告警。
        """
        head = index.get("head", {})
        active = head.get("active")
        versions = index.get("versions", {})
        if active is None or str(active) not in versions:
            return True  # 无 active 基准（首版本）→ 直接通过
        new_v = self._metric_value(metadata.get("metrics") or {}, win_metric)
        try:
            old_meta = self.get_model_metadata(
                self._safe_name(index.get("model_name") or ""), int(active))
        except (ModelVersionNotFoundError, ModelIntegrityError):
            logger.warning("[ml] 读取 active 元数据失败，跳过注册门控")
            return True
        old_v = self._metric_value(old_meta.metrics, win_metric)
        if new_v is None or old_v is None:
            logger.warning(
                "[ml] 新旧版本缺少 win_metric=%s（新=%s 旧=%s），跳过门控不阻断",
                win_metric, new_v, old_v,
            )
            return True
        if new_v < old_v:
            logger.warning(
                "[ml] 新版本 %s=%.4f < active 版本 %s=%.4f，门控不通过",
                win_metric, new_v, win_metric, old_v,
            )
            return False
        return True

    # ─────────────────────────────────────────────
    #  查询
    # ─────────────────────────────────────────────

    def list_models(self) -> List[str]:
        """列出所有已注册模型名（目录名）"""
        if not self._models_root.exists():
            return []
        return sorted(
            d.name for d in self._models_root.iterdir()
            if d.is_dir() and (d / "versions" / "index.json").exists()
        )

    def list_versions(self, model_name: str) -> List[Dict[str, Any]]:
        """列出模型全部版本（按版本号倒序，返回摘要列表）"""
        index = self._read_index(model_name)
        versions = index.get("versions", {})
        ordered = sorted(versions.items(), key=lambda kv: int(kv[0]), reverse=True)
        result = []
        for ver_str, summary in ordered:
            item = dict(summary)
            item["version"] = int(ver_str)
            item["model_name"] = self._safe_name(model_name)
            result.append(item)
        return result

    def get_model_info(
        self, model_name: str, version: Optional[int] = None
    ) -> ModelVersion:
        """
        获取模型版本信息（默认 active 版本；无 active 取 latest）

        Raises
        ------
        ModelNotFoundError : 模型不存在
        ModelVersionNotFoundError : 版本不存在
        """
        with self._lock:
            index = self._read_index(model_name)
            versions = index.get("versions", {})
            if not versions:
                raise ModelNotFoundError(f"模型不存在: {model_name}")
            head = index.get("head", {})
            ver = version if version is not None else head.get("active") or head.get("latest")
            if ver is None or str(ver) not in versions:
                raise ModelVersionNotFoundError(f"模型 {model_name} 版本 {ver} 不存在")
            return self.get_model_metadata(model_name, int(ver))

    def get_model_metadata(self, model_name: str, version: int) -> ModelVersion:
        """读取版本完整元数据（metadata.json）"""
        mpath = self._metadata_path(model_name, version)
        if not mpath.exists():
            raise ModelVersionNotFoundError(f"模型 {model_name} 版本 {version} 不存在")
        try:
            data = read_json(mpath)
        except Exception as e:
            raise ModelIntegrityError(f"元数据损坏: {mpath} ({e})") from e
        meta = ModelVersion.from_dict(data)
        # 以索引中的 status 为准（active/archived 实时状态）
        index = self._read_index(model_name)
        summary = index.get("versions", {}).get(str(version))
        if summary:
            meta.status = summary.get("status", meta.status)
        return meta

    def get_active_version(self, model_name: str) -> Optional[ModelVersion]:
        """获取当前 active 版本（无则 None）"""
        index = self._read_index(model_name)
        head = index.get("head", {})
        ver = head.get("active")
        if ver is None or str(ver) not in index.get("versions", {}):
            return None
        return self.get_model_metadata(model_name, int(ver))

    def get_latest_version(self, model_name: str) -> Optional[ModelVersion]:
        """获取最新版本（无则 None）"""
        index = self._read_index(model_name)
        head = index.get("head", {})
        ver = head.get("latest")
        if ver is None or str(ver) not in index.get("versions", {}):
            versions = index.get("versions", {})
            if not versions:
                return None
            ver = max(int(v) for v in versions)
        return self.get_model_metadata(model_name, int(ver))

    # ─────────────────────────────────────────────
    #  加载 / 校验
    # ─────────────────────────────────────────────

    def load_model(
        self,
        model_name: str,
        version: Optional[int] = None,
        allow_retired: bool = False,
    ) -> Any:
        """
        加载模型对象（F-07：先验 HMAC 签名再验 checksum 完整性）

        【F-07】校验顺序 = 元数据签名校验 → 工件 checksum 校验（报告要求）；
        签名校验覆盖元数据全部字段（含 checksum 字段），篡改任意元数据字段
        均被签名层拦截（ModelSignatureError）；工件被篡改由 checksum 拦截。

        【F-05】失效（retired）版本默认拒绝加载（失效版本 0 次被调用）；
        allow_retired=True 供评估/审计显式加载历史失效版本。

        【防未来】加载后由调用方（任务21 推理服务）配合 train_end_time
        校验只使用 <= 训练截止的数据。

        Raises
        ------
        ModelRetiredError : 加载的版本已失效（且未显式 allow_retired）
        ModelSignatureError : 元数据签名校验失败（元数据被篡改）
        ModelIntegrityError : 工件 checksum 校验失败（被篡改/损坏）
        """
        meta = self.get_model_info(model_name, version)
        if meta.status == "retired" and not allow_retired:
            raise ModelRetiredError(
                f"模型已失效禁止调用: {meta.tag}（如需评估请 allow_retired=True）"
            )
        # F-07 签名校验（先于 checksum）：用磁盘 metadata.json 原始 dict 验签，
        # 与注册时签名对象同源，不受索引 status 修正影响。
        raw_meta = read_json(self._metadata_path(meta.model_name, meta.version))
        self._verify_metadata_signature(raw_meta)
        apath = self._artifact_path(meta.model_name, meta.version, meta.artifact)
        if not apath.exists():
            raise ModelVersionNotFoundError(f"模型工件缺失: {apath}")
        actual = self._file_sha256(apath)
        if meta.checksum and actual != meta.checksum:
            raise ModelIntegrityError(
                f"模型工件完整性校验失败 {meta.tag}: 期望 {meta.checksum[:12]} 实际 {actual[:12]}"
            )
        with open(apath, "rb") as f:
            model = pickle.load(f)
        logger.info(f"[ml] 模型加载成功: {meta.tag}")
        return model

    def verify_model(self, model_name: str, version: Optional[int] = None) -> bool:
        """校验模型完整性（F-07：元数据签名 + 工件 checksum 均匹配返回 True）"""
        try:
            meta = self.get_model_info(model_name, version)
            # F-07 签名校验（元数据被篡改 → False）
            raw_meta = read_json(self._metadata_path(meta.model_name, meta.version))
            self._verify_metadata_signature(raw_meta)
            apath = self._artifact_path(meta.model_name, meta.version, meta.artifact)
            if not apath.exists():
                return False
            return self._file_sha256(apath) == meta.checksum
        except (ModelNotFoundError, ModelVersionNotFoundError,
                ModelSignatureError, ModelIntegrityError):
            return False

    # ─────────────────────────────────────────────
    #  active 热切换 / 回滚
    # ─────────────────────────────────────────────

    def approve_version(self, model_name: str, version: int, approver: str, note: str = "") -> ModelVersion:
        """KP-P6-05: 审批模型版本（pending_review → active）。未批准不 active。

        需非空 approver；已 active 版本不可重复审批；审批写入 approval 记录。
        """
        with self._lock:
            index = self._read_index(model_name)
            versions = index.get("versions", {})
            if not versions:
                raise ModelNotFoundError(f"模型不存在: {model_name}")
            key = str(version)
            if key not in versions:
                raise ModelVersionNotFoundError(f"模型 {model_name} 版本 {version} 不存在")
            current = versions[key].get("status", "pending_review")
            if current == "active":
                raise ModelRegistryError(f"模型 {model_name} v{version} 已 active，无需重复审批")
            if not approver or not str(approver).strip():
                raise ModelRegistryError("approve_version 需要非空 approver")
            versions[key]["status"] = "active"
            versions[key]["approval"] = {"approver": str(approver), "at": self._now(), "note": note}
            head = index.setdefault("head", {})
            old_active = head.get("active")
            if old_active and str(old_active) != key:
                old = versions.get(str(old_active))
                if old:
                    old["status"] = "archived"
            head["active"] = int(version)
            head["approved_at"] = versions[key]["approval"]["at"]
            self._write_index(model_name, index)
            logger.info(f"[ml] 审批激活: {model_name} v{version} by {approver}")
            return self.get_model_metadata(model_name, version)

    def activate_version(self, model_name: str, version: int) -> ModelVersion:
        """
        实盘热切换：将指定版本设为 active

        实盘不停机切换模型：推理服务下次加载即用新 active 版本。
        """
        with self._lock:
            index = self._read_index(model_name)
            versions = index.get("versions", {})
            if not versions:
                raise ModelNotFoundError(f"模型不存在: {model_name}")
            if str(version) not in versions:
                raise ModelVersionNotFoundError(f"模型 {model_name} 版本 {version} 不存在")
            versions[str(version)]["status"] = "active"
            # 旧 active 归档
            head = index.setdefault("head", {})
            old_active = head.get("active")
            if old_active and str(old_active) != str(version):
                old_summary = versions.get(str(old_active))
                if old_summary:
                    old_summary["status"] = "archived"
            head["active"] = version
            self._write_index(model_name, index)
            logger.info(f"[ml] 模型热切换: {model_name} v{old_active} → v{version}")
            return self.get_model_metadata(model_name, version)

    def rollback(self, model_name: str) -> Optional[ModelVersion]:
        """
        回滚：active 版本回退到前一个版本（记录 last_rollback）

        若 active 已是最早版本（无更早版本可回退），返回 None。
        """
        with self._lock:
            index = self._read_index(model_name)
            versions = index.get("versions", {})
            if not versions:
                raise ModelNotFoundError(f"模型不存在: {model_name}")
            head = index.setdefault("head", {})
            active = head.get("active") or head.get("latest")
            if active is None:
                raise ModelVersionNotFoundError(f"模型 {model_name} 无有效版本")
            active = int(active)
            available = sorted(int(v) for v in versions)
            # 目标 = active 之前的版本号（按版本号降序找 < active 的最大者）
            targets = [v for v in available if v < active]
            if not targets:
                return None
            target = max(targets)
            versions[str(active)]["status"] = "archived"
            versions[str(target)]["status"] = "active"
            head["active"] = target
            index["last_rollback"] = {
                "from": active,
                "to": target,
                "time": self._now(),
            }
            self._write_index(model_name, index)
            logger.info(f"[ml] 模型回滚: {model_name} v{active} → v{target}")
            return self.get_model_metadata(model_name, target)

    def mark_retired(
        self,
        model_name: str,
        version: int,
        degrade_on_retire: bool = True,
        reason: str = "",
    ) -> Tuple[ModelVersion, Optional[int]]:
        """
        【F-05】标记版本失效（retired）：禁止调用 + 可选自动降级。

        若被标记的版本是当前 active：
          - degrade_on_retire=True：自动降级到"版本号最大的非失效版本"
            （无候选则 head.active 置空并告警）；
          - degrade_on_retire=False：仅标记失效，active 保持指向失效版本
            （由调用方决定后续处置）。
        降级动作记录 index.last_retire（可审计）。

        Args:
            model_name: 模型名称。
            version: 待标记失效的版本号。
            degrade_on_retire: 是否自动降级（默认 True）。
            reason: 失效原因（监控判定明细，写入 last_retire）。

        Returns:
            (被标记版本的 ModelVersion, 降级目标版本或 None)。

        Raises:
            ModelNotFoundError: 模型不存在。
            ModelVersionNotFoundError: 版本不存在。
        """
        with self._lock:
            index = self._read_index(model_name)
            versions = index.get("versions", {})
            if not versions:
                raise ModelNotFoundError(f"模型不存在: {model_name}")
            if str(version) not in versions:
                raise ModelVersionNotFoundError(f"模型 {model_name} 版本 {version} 不存在")
            versions[str(version)]["status"] = "retired"
            head = index.setdefault("head", {})
            degraded_to: Optional[int] = None
            if degrade_on_retire and head.get("active") == version:
                candidates = [
                    v for v in sorted(int(x) for x in versions)
                    if versions[str(v)]["status"] != "retired" and v < version
                ]
                if candidates:
                    degraded_to = max(candidates)
                    versions[str(degraded_to)]["status"] = "active"
                    head["active"] = degraded_to
                else:
                    head["active"] = None
            index["last_retire"] = {
                "retired": version,
                "degraded_to": degraded_to,
                "time": self._now(),
                "reason": reason,
            }
            self._write_index(model_name, index)
            if degraded_to is not None:
                logger.warning(
                    "[ml] 模型失效标记: %s v%d retired，降级至 v%d",
                    model_name, version, degraded_to,
                )
            else:
                logger.warning(
                    "[ml] 模型失效标记: %s v%d retired（无可用降级版本%s）",
                    model_name, version,
                    "，active 已置空" if head.get("active") is None else "",
                )
            return self.get_model_metadata(model_name, version), degraded_to

    # ─────────────────────────────────────────────
    #  删除
    # ─────────────────────────────────────────────

    def delete_version(self, model_name: str, version: int) -> bool:
        """
        删除指定版本（head 自动回退：删除 active/latest 时回退到剩余最大版本）
        """
        import shutil
        with self._lock:
            index = self._read_index(model_name)
            versions = index.get("versions", {})
            if str(version) not in versions:
                raise ModelVersionNotFoundError(f"模型 {model_name} 版本 {version} 不存在")
            vdir = self._version_dir(model_name, version)
            shutil.rmtree(vdir, ignore_errors=True)
            del versions[str(version)]
            head = index.setdefault("head", {})
            # head 回退
            remaining = [int(v) for v in versions]
            if not remaining:
                head.clear()
            else:
                new_latest = max(remaining)
                head["latest"] = new_latest
                if head.get("active") == version or head.get("active") not in versions:
                    head["active"] = new_latest
                # 更新新 active 摘要状态
                if head.get("active"):
                    versions[str(head["active"])]["status"] = "active"
            self._write_index(model_name, index)
            logger.info(f"[ml] 模型版本删除: {model_name} v{version}")
            return True

    def delete_model(self, model_name: str) -> bool:
        """删除整个模型（全部版本）"""
        import shutil
        with self._lock:
            root = self._model_root(model_name)
            if not root.exists():
                raise ModelNotFoundError(f"模型不存在: {model_name}")
            shutil.rmtree(root, ignore_errors=True)
            logger.info(f"[ml] 模型删除: {model_name}")
            return True

    def get_summary(self) -> Dict[str, Any]:
        """注册表摘要（模型数 + 各模型版本/active 概览）"""
        result: Dict[str, Any] = {"models_root": str(self._models_root), "models": {}}
        for name in self.list_models():
            versions = self.list_versions(name)
            index = self._read_index(name)
            result["models"][name] = {
                "version_count": len(versions),
                "latest": index.get("head", {}).get("latest"),
                "active": index.get("head", {}).get("active"),
                "last_rollback": index.get("last_rollback"),
            }
        return result


__all__ = [
    "ModelRegistry",
    "ModelVersion",
    "ModelRegistryError",
    "ModelNotFoundError",
    "ModelVersionNotFoundError",
    "ModelIntegrityError",
    "ModelSignatureError",
    "ModelRetiredError",
]
