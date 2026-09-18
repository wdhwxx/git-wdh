# 文件名：myquant/core/config_loader.py
"""
config_loader.py - 通用配置加载器
==================================
功能:
  1. 加载 YAML / JSON 配置文件
  2. 支持默认值 Schema 校验
  3. 支持环境变量插值 ${VAR_NAME}
  4. 支持 include/extends 继承
  5. 配置校验 (必填项、类型检查)

依赖: json, yaml(可选), re, os, pathlib
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    import yaml
except ImportError:
    yaml = None

# 统一使用 exceptions.py 中的异常体系，避免重复定义
from myquant.core.exceptions import ConfigValidationError


class ConfigLoader:
    """
    通用配置加载器

    功能:
      - 多格式加载 (YAML/JSON)
      - 环境变量插值: "host: ${DB_HOST}"
      - Schema 校验: 必填、类型、范围
      - 多文件合并
      - include 机制

    Usage:
        loader = ConfigLoader()
        config = loader.load("config/database.yaml")
        config = loader.load_with_schema("config/app.yaml", schema={
            "name": {"type": str, "required": True},
            "port": {"type": int, "default": 8080},
        })
    """

    # 环境变量插值正则: ${VAR} 或 ${VAR:default}
    _ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")

    def __init__(self, base_dir: Optional[Union[str, Path]] = None):
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()

    # ── 加载 ──
    def load(self, filepath: Union[str, Path]) -> Dict[str, Any]:
        """加载单个配置文件"""
        filepath = self._resolve(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"配置文件不存在: {filepath}")

        suffix = filepath.suffix.lower()
        if suffix in (".yaml", ".yml"):
            return self._load_yaml(filepath)
        elif suffix == ".json":
            return self._load_json(filepath)
        else:
            raise ValueError(f"不支持的配置格式: {suffix}")

    def load_multi(self, *filepaths: Union[str, Path]) -> Dict[str, Any]:
        """加载多个配置文件并合并 (后者覆盖前者)"""
        result: Dict[str, Any] = {}
        for fp in filepaths:
            data = self.load(fp)
            self._deep_merge(result, data)
        return result

    def _load_yaml(self, filepath: Path) -> Dict:
        if yaml is None:
            raise ImportError("需要安装 PyYAML: pip install pyyaml")
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = self._process_includes(data, filepath.parent)
        data = self._interpolate_env(data)
        return data

    def _load_json(self, filepath: Path) -> Dict:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = self._interpolate_env(data)
        return data

    # ── include 机制 ──
    def _process_includes(self, data: Dict, base_path: Path) -> Dict:
        """处理 __include__ 字段"""
        if not isinstance(data, dict):
            return data
        include = data.pop("__include__", None)
        if include:
            includes = include if isinstance(include, list) else [include]
            merged: Dict = {}
            for inc in includes:
                inc_path = (base_path / inc).resolve()
                inc_data = self.load(inc_path)
                self._deep_merge(merged, inc_data)
            self._deep_merge(merged, data)
            return merged
        return data

    # ── 环境变量插值 ──
    def _interpolate_env(self, data: Any) -> Any:
        """递归替换 ${VAR} 和 ${VAR:default}"""
        if isinstance(data, str):
            return self._replace_env(data)
        elif isinstance(data, dict):
            return {k: self._interpolate_env(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._interpolate_env(item) for item in data]
        return data

    def _replace_env(self, s: str) -> str:
        def replacer(match):
            var_name = match.group(1)
            default_val = match.group(2)
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            if default_val is not None:
                return default_val
            return match.group(0)  # 保持原样
        return self._ENV_PATTERN.sub(replacer, s)

    # ── Schema 校验 ──
    def validate(
        self,
        config: Dict[str, Any],
        schema: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        校验配置并填充默认值

        schema 格式:
        {
            "database.host": {"type": str, "required": True},
            "database.port": {"type": int, "default": 3306},
            "app.debug":     {"type": bool, "default": False},
            "app.name":      {"type": str, "required": True, "choices": ["prod","dev"]},
        }
        """
        errors: List[str] = []
        result = dict(config)

        for key, rules in schema.items():
            val = self._get_nested(result, key)

            # 必填检查
            if val is None:
                if rules.get("required", False):
                    errors.append(f"缺少必填配置: {key}")
                    continue
                elif "default" in rules:
                    self._set_nested(result, key, rules["default"])
                    continue
                else:
                    continue

            # 类型检查
            expected_type = rules.get("type")
            if expected_type and not isinstance(val, expected_type):
                try:
                    val = expected_type(val)
                    self._set_nested(result, key, val)
                except (TypeError, ValueError):
                    errors.append(f"配置 {key} 类型错误: 期望 {expected_type.__name__}, 实际 {type(val).__name__}")
                    continue

            # 枚举检查
            choices = rules.get("choices")
            if choices and val not in choices:
                errors.append(f"配置 {key} 值 '{val}' 不在允许范围 {choices}")

            # 范围检查
            min_val = rules.get("min")
            max_val = rules.get("max")
            if min_val is not None and val < min_val:
                errors.append(f"配置 {key} 值 {val} 小于最小值 {min_val}")
            if max_val is not None and val > max_val:
                errors.append(f"配置 {key} 值 {val} 大于最大值 {max_val}")

        if errors:
            raise ConfigValidationError(
                message=f"配置校验失败: {'; '.join(errors)}",
                details={"errors": errors},
            )
        return result

    def load_with_schema(
        self,
        filepath: Union[str, Path],
        schema: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """加载 + 校验 一步完成"""
        config = self.load(filepath)
        return self.validate(config, schema)

    # ── 工具方法 ──
    def _resolve(self, filepath: Union[str, Path]) -> Path:
        p = Path(filepath)
        if p.is_absolute():
            return p
        return self.base_dir / p

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                ConfigLoader._deep_merge(base[key], value)
            else:
                base[key] = value
        return base

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

    def __repr__(self) -> str:
        return f"ConfigLoader(base_dir='{self.base_dir}')"