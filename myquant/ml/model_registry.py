# 文件名：myquant/ml/model_registry.py
"""
model_registry.py - 模型注册版本管理
====================================
模型注册版本管理（任务19，ML 完整 MLOps 链路第二环）。

核心能力:
  - register_model()    注册模型（版本自动递增 + 工件落盘 + 元数据 + checksum + 索引双写）
  - load_model()        加载模型对象（checksum 完整性校验）
  - activate_version()  实盘热切换 active 版本
  - rollback()          回滚到前一版本（记录 last_rollback）
  - list/get/delete/verify 版本管理全能力

存储布局（对齐任务14 数据集版本管理成熟模式）:
  datasets/models/{model_name}/versions/
  ├── index.json            # 版本索引（head/active/last_rollback/versions 摘要）
  └── v{n}/
      ├── model.pkl         # 模型工件（pickle 标准库序列化，不绑定 ML 框架）
      └── metadata.json     # 版本元数据（ModelVersion）

数据来源（铁律）:
  - 本模块只管理模型工件文件，不读取行情数据（天然满足 ml/ 禁读 raw 约束）
  - 训练数据截止时间 train_end_time 记录在元数据中，供任务21 推理服务
    校验"只用 <= 训练截止数据"（防未来）

防未来函数（铁律4）:
  - train_end_time 记录训练数据截止（推理侧只使用 <= 截止数据）
  - 本模块无任何预测方法/属性
  - 无硬编码未来日期
  - load_model/verify_model 做 checksum 完整性校验（工件篡改可检出）

为实盘无缝对接（任务21-23 实盘增强前置）:
  - active 版本热切换（activate_version）：实盘模型不停机切换
  - 多版本并存：A/B 测试、回滚
  - source 区分 offline/live
  - 线程锁 + 跨进程文件锁（实盘多进程预留）
  - 模型与训练特征绑定（feature_config_hash + feature_columns，与任务18 对齐）

依赖: myquant.utils.file_utils, 标准库（pickle/json/threading/hashlib）
"""

from __future__ import annotations

import hashlib
import json
import pickle
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        状态：active（当前生效）/ archived（归档）
    parent_version : Optional[int]
        派生链：从哪个版本训练而来
    description : str
        描述
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
    status: str = "active"
    parent_version: Optional[int] = None
    description: str = ""

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
                status="active",
                parent_version=metadata.get("parent_version"),
                description=description,
            )
            # metadata.json 落盘
            write_json(str(self._metadata_path(model_name, ver)), meta.to_dict())
            # 索引双写（新增版本 + 该版本为最新 + active 默认指向最新）
            index.setdefault("versions", {})[str(ver)] = self._summary(meta)
            head = index.setdefault("head", {})
            head["latest"] = ver
            head["active"] = ver
            self._write_index(model_name, index)
            logger.info(f"[ml] 模型注册成功: {meta.tag} ({meta.model_type})")
            return meta

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

    def load_model(self, model_name: str, version: Optional[int] = None) -> Any:
        """
        加载模型对象（checksum 完整性校验）

        【防未来】加载后由调用方（任务21 推理服务）配合 train_end_time
        校验只使用 <= 训练截止的数据。

        Raises
        ------
        ModelIntegrityError : 工件 checksum 校验失败（被篡改/损坏）
        """
        meta = self.get_model_info(model_name, version)
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
        """校验模型工件完整性（checksum 匹配返回 True）"""
        try:
            meta = self.get_model_info(model_name, version)
            apath = self._artifact_path(meta.model_name, meta.version, meta.artifact)
            if not apath.exists():
                return False
            return self._file_sha256(apath) == meta.checksum
        except (ModelNotFoundError, ModelVersionNotFoundError):
            return False

    # ─────────────────────────────────────────────
    #  active 热切换 / 回滚
    # ─────────────────────────────────────────────

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
]
