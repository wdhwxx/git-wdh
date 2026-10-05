# 文件名：myquant/core/settings.py
"""
settings.py - 全局配置管理器
=============================
功能:
  1. 加载 .env 环境变量
  2. 加载 config/*.yaml 多级配置文件
  3. 支持 默认值 < 配置文件 < 环境变量 < 运行时覆盖 四级优先级
  4. 单例模式，全局唯一
  5. 类型安全访问 (get_int / get_float / get_bool / get_list)
  6. 热重载支持

依赖: os, pathlib, yaml, dotenv(可选)
"""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from myquant.core.logger import get_logger

logger = get_logger("core.settings")

# 尝试导入可选依赖
try:
    import yaml
except ImportError:
    yaml = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


class Settings:
    """
    全局配置管理器 (线程安全单例)

    优先级 (高→低):
      1. 运行时 set() 覆盖
      2. 环境变量
      3. 项目配置文件 (config/*.yaml)
      4. 内置默认值

    Usage:
        settings = get_settings()
        db_host = settings.get("database.host", default="localhost")
        debug = settings.get_bool("app.debug", default=False)
    """

    _instance: Optional["Settings"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(
        self,
        project_root: Optional[Union[str, Path]] = None,
        env_file: str = ".env",
        config_dir: str = "config",
        default_config: str = "settings.yaml",
    ):
        if self._initialized:
            return
        self._initialized = True

        # 项目根目录
        if project_root:
            self._root = Path(project_root).resolve()
        else:
            self._root = self._find_project_root()

        self._env_file = env_file
        self._config_dir = config_dir
        self._default_config = default_config

        # 存储层
        self._defaults: Dict[str, Any] = {}
        self._file_config: Dict[str, Any] = {}
        self._runtime_overrides: Dict[str, Any] = {}

        # 加载
        self._load_env()
        self._load_config_files()

    def _find_project_root(self) -> Path:
        """向上搜索项目根"""
        markers = ["requirements.txt", "pyproject.toml", "setup.py", ".git"]
        current = Path(__file__).resolve().parent
        for _ in range(6):
            for m in markers:
                if (current / m).exists():
                    return current
            if current == current.parent:
                break
            current = current.parent
        return Path.cwd()

    # ── 加载 ──
    def _load_env(self) -> None:
        """加载 .env 文件"""
        env_path = self._root / self._env_file
        if env_path.exists() and load_dotenv:
            load_dotenv(str(env_path), override=False)

    def _load_config_files(self) -> None:
        """加载 config/ 目录下所有 yaml 文件"""
        config_path = self._root / self._config_dir
        if not config_path.exists():
            return

        # 按文件名排序加载，settings.yaml 优先
        yaml_files = sorted(config_path.glob("*.yaml")) + sorted(config_path.glob("*.yml"))
        # M4修复: 跳过 example 模板文件（*.example.yaml / *.example.yml）——
        # 模板只作参考，加载会使占位值（如 password 示例）直接生效，
        # 并与正式配置同键覆盖（全量加载隐患）
        yaml_files = [
            f
            for f in yaml_files
            if ".example." not in f.name.lower()
            and not f.name.lower().startswith("example")
        ]
        for f in yaml_files:
            self._merge_yaml(f)

    def _merge_yaml(self, filepath: Path) -> None:
        """合并单个YAML文件到 _file_config"""
        if yaml is None:
            return
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                self._deep_merge(self._file_config, data)
        except Exception as e:
            logger.warning(f"配置文件加载失败: {filepath}, 错误: {e}")

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """深度合并字典"""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                Settings._deep_merge(base[key], value)
            else:
                base[key] = value
        return base

    # ── 核心访问 ──
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值
        key 支持点号路径: "database.host" -> {"database": {"host": ...}}
        """
        # 1. 运行时覆盖
        val = self._get_nested(self._runtime_overrides, key)
        if val is not None:
            return val

        # 2. 环境变量 (key转大写, 点号转下划线)
        env_key = key.upper().replace(".", "_")
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return env_val

        # 3. 配置文件
        val = self._get_nested(self._file_config, key)
        if val is not None:
            return val

        # 4. 默认值
        val = self._get_nested(self._defaults, key)
        if val is not None:
            return val

        return default

    def get_int(self, key: str, default: int = 0) -> int:
        val = self.get(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        val = self.get(key, default)
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes", "on")
        return bool(val)

    def get_list(self, key: str, default: Optional[List] = None) -> List:
        val = self.get(key, default)
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            return [x.strip() for x in val.split(",") if x.strip()]
        return default or []

    def get_dict(self, key: str, default: Optional[Dict] = None) -> Dict:
        val = self.get(key, default)
        return val if isinstance(val, dict) else (default or {})

    # ── 设置 ──
    def set(self, key: str, value: Any) -> None:
        """运行时覆盖 (最高优先级)"""
        self._set_nested(self._runtime_overrides, key, value)

    def set_default(self, key: str, value: Any) -> None:
        """设置默认值 (最低优先级)"""
        self._set_nested(self._defaults, key, value)

    # ── 工具 ──
    @staticmethod
    def _get_nested(data: dict, key: str) -> Any:
        keys = key.split(".")
        current = data
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return None
        return current

    @staticmethod
    def _set_nested(data: dict, key: str, value: Any) -> None:
        keys = key.split(".")
        current = data
        for k in keys[:-1]:
            if k not in current or not isinstance(current[k], dict):
                current[k] = {}
            current = current[k]
        current[keys[-1]] = value

    def reload(self) -> None:
        """热重载配置文件"""
        self._file_config.clear()
        self._load_config_files()

    def to_dict(self) -> Dict[str, Any]:
        """导出当前有效配置"""
        result = {}
        self._deep_merge(result, self._defaults)
        self._deep_merge(result, self._file_config)
        self._deep_merge(result, self._runtime_overrides)
        return result

    @property
    def project_root(self) -> Path:
        return self._root

    def __repr__(self) -> str:
        return f"Settings(root='{self._root}', keys={len(self._file_config)})"


# ── 全局单例访问 ──
def get_settings(**kwargs) -> Settings:
    """获取全局配置单例"""
    return Settings(**kwargs)


# ═══════════════════════════════════════════════
#  监控配置集中化（BASE-3：改 settings.yaml 调全部阈值，无需改代码）
# ═══════════════════════════════════════════════

DEFAULT_MONITORING: Dict[str, Any] = {
    "patrol_interval": 60,                       # 巡检周期（秒）
    "watchdog": {                                # 引擎心跳 watchdog（A-5 消费）
        "enabled": False,
        "bar_timeout_seconds": 300,
        "session_grace_minutes": 5,
        "order_pending_timeout_seconds": 300,
    },
    "alerts": {                                  # 告警行为（BASE-1 落库/去重/冷却/规则）
        "store_enabled": True,
        "dedup_window_seconds": 300.0,
        "cooldown_seconds": 60.0,
        "aggregation_window_seconds": 60.0,
        "escalation_time_seconds": 300.0,
        "escalation_enabled": True,
    },
    "slo": {},                                   # SLO 阈值由 settings.yaml 提供；缺省为空（不编造阈值）
}


def _build_monitoring_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    由配置原文构建完整 monitoring 配置（默认深合并 + 防御式校验）。

    Parameters
    ----------
    raw : dict
        settings.yaml 的 monitoring 块原文（可为空 dict）

    Returns
    -------
    dict :
        完整 monitoring 配置：未配置项回退默认值；非法值（非正数/非布尔）
        记 warning 并回退默认，不静默使用脏值。

    防未来：本函数只消费"当前配置"静态值，不涉及任何时间序列或预测。
    """
    result: Dict[str, Any] = {}

    def _deep_merge_default(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                Settings._deep_merge(base[key], value)
            else:
                base[key] = value
        return base

    result = _deep_merge_default(copy.deepcopy(DEFAULT_MONITORING), dict(raw or {}))

    # 防御式校验：非正数/非布尔回退默认（记 warning 留痕，不静默吞）
    def _positive_float(key: str, fallback: float) -> float:
        try:
            val = Settings._get_nested(result, key)
            val = float(val)
            if val > 0:
                return val
            logger.warning(f"monitoring.{key} 非法（须 >0）：{val!r}，回退默认 {fallback}")
            return fallback
        except (TypeError, ValueError):
            logger.warning(f"monitoring.{key} 非法：{val!r}，回退默认 {fallback}")
            return fallback

    def _positive_int(key: str, fallback: int) -> int:
        try:
            val = Settings._get_nested(result, key)
            val = int(val)
            if val > 0:
                return val
            logger.warning(f"monitoring.{key} 非法（须 >0）：{val!r}，回退默认 {fallback}")
            return fallback
        except (TypeError, ValueError):
            logger.warning(f"monitoring.{key} 非法：{val!r}，回退默认 {fallback}")
            return fallback

    def _bool(key: str, fallback: bool) -> bool:
        val = Settings._get_nested(result, key)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            if val.lower() in ("true", "1", "yes", "on"):
                return True
            if val.lower() in ("false", "0", "no", "off"):
                return False
        logger.warning(f"monitoring.{key} 非法：{val!r}，回退默认 {fallback}")
        return fallback

    result["patrol_interval"] = _positive_int(
        "patrol_interval", DEFAULT_MONITORING["patrol_interval"]
    )
    wd = result.get("watchdog") or {}
    result["watchdog"] = {
        "enabled": _bool("watchdog.enabled", DEFAULT_MONITORING["watchdog"]["enabled"]),
        "bar_timeout_seconds": _positive_float(
            "watchdog.bar_timeout_seconds",
            DEFAULT_MONITORING["watchdog"]["bar_timeout_seconds"],
        ),
        "session_grace_minutes": _positive_float(
            "watchdog.session_grace_minutes",
            DEFAULT_MONITORING["watchdog"]["session_grace_minutes"],
        ),
        "order_pending_timeout_seconds": _positive_float(
            "watchdog.order_pending_timeout_seconds",
            DEFAULT_MONITORING["watchdog"]["order_pending_timeout_seconds"],
        ),
    }
    al = result.get("alerts") or {}
    result["alerts"] = {
        "store_enabled": _bool("alerts.store_enabled", DEFAULT_MONITORING["alerts"]["store_enabled"]),
        "dedup_window_seconds": _positive_float(
            "alerts.dedup_window_seconds", DEFAULT_MONITORING["alerts"]["dedup_window_seconds"]
        ),
        "cooldown_seconds": _positive_float(
            "alerts.cooldown_seconds", DEFAULT_MONITORING["alerts"]["cooldown_seconds"]
        ),
        "aggregation_window_seconds": _positive_float(
            "alerts.aggregation_window_seconds",
            DEFAULT_MONITORING["alerts"]["aggregation_window_seconds"],
        ),
        "escalation_time_seconds": _positive_float(
            "alerts.escalation_time_seconds",
            DEFAULT_MONITORING["alerts"]["escalation_time_seconds"],
        ),
        "escalation_enabled": _bool(
            "alerts.escalation_enabled", DEFAULT_MONITORING["alerts"]["escalation_enabled"]
        ),
    }
    slo = result.get("slo")
    if slo is not None and not isinstance(slo, dict):
        logger.warning("monitoring.slo 非法（须为 dict），回退空")
        slo = {}
    result["slo"] = slo or {}
    return result


def get_monitoring_config(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """
    读取集中化监控配置（BASE-3 唯一真源入口）。

    Parameters
    ----------
    settings : Settings | None
        配置单例；None 用全局 get_settings()

    Returns
    -------
    dict :
        完整 monitoring 配置（patrol_interval / watchdog / alerts / slo），
        由 settings.yaml 原文 + 默认值合并 + 防御式校验得到。

    用法:
        cfg = get_monitoring_config()
        patrol_interval = cfg["patrol_interval"]
        alerts_cfg = cfg["alerts"]          # store_enabled / dedup_window_seconds / ...
        slo = cfg["slo"]                    # R-04 阈值真源（LOCKED 项不改动）
    """
    s = settings if settings is not None else get_settings()
    raw = s.get_dict("monitoring", {})
    return _build_monitoring_config(raw)