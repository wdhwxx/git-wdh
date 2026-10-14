# 文件名：myquant/data_access/cleaner/rules_loader.py
"""
rules_loader.py - 清洗规则加载器 (阶段9规则对象化)
=====================================================
阶段9高级增强迭代：从v1字典配置升级为 CleanRule 对象列表，支持规则对象化管理。

兼容性保证:
  - 旧函数 load_clean_config() 完全保留，返回字典配置
  - 新增 load_clean_rules() 返回 CleanRule 对象列表
  - 新增 CleanRuleLibrary 规则库管理类
  - 字典配置与规则对象可双向转换

功能:
  - 从 config/data_clean.yaml 加载完整清洗配置
  - 从全局 Settings 读取清洗配置
  - 将字典配置映射为 CleanRule 对象列表（bar/tick 两种数据类型）
  - CleanRuleLibrary 规则库管理（加载/查询/启用禁用/阈值更新/导出）
  - 规则分类：single（单条校验）、sequence（序列校验）、dedup（去重）、anomaly（异常检测）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（规则热更新/按标的差异化/配置热重载）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 规则只定义校验逻辑，不引入未来数据

防未来函数说明:
  - 规则对象只定义校验规则和阈值，不包含任何数据
  - 序列类规则（price_jump/volume_anomaly）在执行时只比较当前与前一条记录
  - 规则库不缓存任何行情数据，只管理规则定义
  - 按标的差异化配置只影响规则参数，不引入未来数据

TODO(后续迭代):
  - 按标的差异化配置 (symbol_overrides)
  - 配置热重载（文件变更自动重新加载）
  - 配置版本管理（规则变更历史/回滚）
  - 规则校验函数动态注册（插件式自定义规则）
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.data_access.cleaner.base_cleaner import CleanRule
from myquant.utils.file_utils import get_project_path

logger = get_logger("data_clean.rules_loader")


# ── 顶层默认配置 ──

DEFAULT_CLEAN_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "log_bad_records": True,
    "log_quality_report": True,
    "bar": {
        "check_ohlc_logic": True,
        "check_non_negative": True,
        "check_symbol": True,
        "check_timestamp": True,
        "check_time_monotonic": True,
        "check_duplicate_timestamp": True,
        "duplicate_strategy": "keep_last",
        "detect_price_jump": True,
        "max_price_jump_pct": 0.22,
        "detect_volume_anomaly": True,
        "max_volume_ratio": 100.0,
        "allow_zero_volume": True,
        "fill_missing": False,
        "fill_strategy": "ffill",
        "sort_by_timestamp": True,
    },
    "tick": {
        "check_price_non_negative": True,
        "check_volume_non_negative": True,
        "check_bid_ask": True,
        "check_timestamp": True,
        "check_duplicate": True,
        "duplicate_strategy": "keep_last",
        "detect_price_jump": True,
        "max_price_jump_pct": 0.22,
        "sort_by_timestamp": True,
    },
    "bad_data": {
        "output_dir": "datasets/bad",
        "format": "json",
        "include_reason": True,
        "max_records_per_file": 10000,
    },
    "quality_report": {
        "output_dir": "outputs",
        "format": "json",
        "include_details": True,
        "summary_only": False,
    },
}


def load_clean_config(
    config_path: Optional[str] = None,
    use_settings: bool = True,
) -> Dict[str, Any]:
    """
    加载清洗配置

    优先级 (高→低):
      1. config_path 指定的文件
      2. 全局 Settings 中的 data_clean 配置
      3. 内置默认配置

    Parameters
    ----------
    config_path : str | None
        配置文件路径，None 则尝试从默认路径加载
    use_settings : bool
        是否从全局 Settings 读取（config/ 下的 yaml 会被 Settings 自动加载）

    Returns
    -------
    Dict : 完整清洗配置字典
    """
    config = copy.deepcopy(DEFAULT_CLEAN_CONFIG)

    # 方式1: 从全局 Settings 读取
    if use_settings:
        try:
            from myquant.core.settings import get_settings
            settings = get_settings()
            # Settings 会自动加载 config/ 下所有 yaml，data_clean.yaml 的内容会在顶层
            # 尝试读取 data_clean 命名空间下的配置
            settings_config = settings.get_dict("data_clean")
            if settings_config:
                config = _deep_merge(config, settings_config)
                logger.info("已从全局 Settings 加载清洗配置")
        except Exception as e:
            logger.debug(f"从 Settings 加载清洗配置失败: {e}")

    # 方式2: 从指定文件加载 (优先级更高)
    if config_path:
        try:
            import yaml
            filepath = Path(config_path)
            if not filepath.is_absolute():
                filepath = get_project_path(config_path)
            if filepath.exists():
                with open(filepath, "r", encoding="utf-8") as f:
                    file_config = yaml.safe_load(f) or {}
                config = _deep_merge(config, file_config)
                logger.info(f"已从文件加载清洗配置: {filepath}")
            else:
                logger.warning(f"清洗配置文件不存在: {filepath}，使用默认配置")
        except ImportError:
            logger.warning("未安装 PyYAML，无法加载 yaml 配置文件，使用默认配置")
        except Exception as e:
            logger.error(f"加载清洗配置文件失败: {e}，使用默认配置")
    else:
        # 尝试默认路径 config/data_clean.yaml
        default_path = get_project_path("config", "data_clean.yaml")
        if default_path.exists():
            try:
                import yaml
                with open(default_path, "r", encoding="utf-8") as f:
                    file_config = yaml.safe_load(f) or {}
                config = _deep_merge(config, file_config)
                logger.info(f"已从默认路径加载清洗配置: {default_path}")
            except Exception as e:
                logger.debug(f"加载默认清洗配置失败: {e}")

    # P2-10：加载完成后跑值域校验（此前 validate_config 全项目无调用点），
    # 非法阈值不再静默生效，显式告警。
    try:
        ok, errs = validate_config(config)
        if not ok:
            logger.warning("清洗配置值域校验未通过: %s", "; ".join(errs))
    except Exception as e:
        logger.warning("清洗配置校验异常（不阻断加载）: %s", e)

    return config


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    深度合并字典（override 覆盖 base）

    #C12修复：返回新字典（deepcopy base），不修改调用方传入的字典，
    避免意外的副作用。调用方应使用返回值获取合并后的结果。

    Parameters
    ----------
    base : Dict
        基础字典（不会被修改）
    override : Dict
        覆盖字典

    Returns
    -------
    Dict : 合并后的新字典
    """
    # #C12修复：deepcopy base，不修改调用方传入的字典
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ═══════════════════════════════════════════════
#  规则映射定义 (阶段9新增)
# ═══════════════════════════════════════════════

# Bar数据规则映射：规则名 → (配置键, 分类, 阈值提取函数)
BAR_RULE_MAP: Dict[str, Dict[str, Any]] = {
    "ohlc_logic": {
        "config_key": "check_ohlc_logic",
        "category": "single",
        "priority": 10,
        "description": "OHLC逻辑校验（high>=max(open,close), low<=min(open,close), high>=low）",
        "threshold_keys": [],
    },
    "non_negative_price": {
        "config_key": "check_non_negative",
        "category": "single",
        "priority": 20,
        "description": "价格非负校验（open/high/low/close >= 0）",
        "threshold_keys": [],
    },
    "non_negative_volume": {
        "config_key": "check_non_negative",
        "category": "single",
        "priority": 21,
        "description": "成交量非负校验（volume >= 0）",
        "threshold_keys": [],
    },
    "symbol_check": {
        "config_key": "check_symbol",
        "category": "single",
        "priority": 5,
        "description": "标的代码非空校验",
        "threshold_keys": [],
    },
    "timestamp_check": {
        "config_key": "check_timestamp",
        "category": "single",
        "priority": 6,
        "description": "时间戳有效性校验",
        "threshold_keys": [],
    },
    "time_monotonic": {
        "config_key": "check_time_monotonic",
        "category": "sequence",
        "priority": 30,
        "description": "时间单调性校验（时间戳不回退）",
        "threshold_keys": [],
    },
    "duplicate_timestamp": {
        "config_key": "check_duplicate_timestamp",
        "category": "dedup",
        "priority": 40,
        "description": "重复时间戳去重",
        "threshold_keys": ["duplicate_strategy"],
    },
    "price_jump": {
        "config_key": "detect_price_jump",
        "category": "anomaly",
        "priority": 50,
        "description": "价格跳变检测（与前一条close比较，超阈值标记警告）",
        "threshold_keys": ["max_price_jump_pct"],
    },
    "volume_anomaly": {
        "config_key": "detect_volume_anomaly",
        "category": "anomaly",
        "priority": 51,
        "description": "成交量异常放大检测（与前一条volume比较，超阈值标记警告）",
        "threshold_keys": ["max_volume_ratio"],
    },
    "zero_volume_check": {
        "config_key": "allow_zero_volume",
        "category": "single",
        "priority": 22,
        "description": "零成交量校验（allow_zero_volume=False时，volume=0视为脏数据）",
        "threshold_keys": [],
    },
    "sort_output": {
        "config_key": "sort_by_timestamp",
        "category": "single",
        "priority": 100,
        "description": "输出按时间戳升序排序",
        "threshold_keys": [],
    },
}

# Tick数据规则映射
TICK_RULE_MAP: Dict[str, Dict[str, Any]] = {
    "price_non_negative": {
        "config_key": "check_price_non_negative",
        "category": "single",
        "priority": 10,
        "description": "价格非负校验（price >= 0）",
        "threshold_keys": [],
    },
    "volume_non_negative": {
        "config_key": "check_volume_non_negative",
        "category": "single",
        "priority": 11,
        "description": "成交量非负校验（volume >= 0）",
        "threshold_keys": [],
    },
    "bid_ask_check": {
        "config_key": "check_bid_ask",
        "category": "single",
        "priority": 20,
        "description": "买卖盘价格校验（bid < ask）",
        "threshold_keys": [],
    },
    "timestamp_check": {
        "config_key": "check_timestamp",
        "category": "single",
        "priority": 5,
        "description": "时间戳有效性校验",
        "threshold_keys": [],
    },
    "symbol_check": {
        "config_key": "check_symbol",
        "category": "single",
        "priority": 6,
        "description": "标的代码非空校验",
        "threshold_keys": [],
    },
    "duplicate_tick": {
        "config_key": "check_duplicate",
        "category": "dedup",
        "priority": 40,
        "description": "重复Tick去重（按 timestamp+price+volume 三元组）",
        "threshold_keys": ["duplicate_strategy"],
    },
    "price_jump": {
        "config_key": "detect_price_jump",
        "category": "anomaly",
        "priority": 50,
        "description": "价格跳变检测（与前一条price比较，超阈值标记警告）",
        "threshold_keys": ["max_price_jump_pct"],
    },
    "sort_output": {
        "config_key": "sort_by_timestamp",
        "category": "single",
        "priority": 100,
        "description": "输出按时间戳升序排序",
        "threshold_keys": [],
    },
}


def _extract_threshold(
    config: Dict[str, Any],
    threshold_keys: List[str],
) -> Dict[str, Any]:
    """从配置中提取阈值参数"""
    threshold = {}
    for key in threshold_keys:
        if key in config:
            threshold[key] = config[key]
    return threshold


def build_bar_rules(bar_config: Dict[str, Any]) -> List[CleanRule]:
    """
    从Bar配置字典构建 CleanRule 对象列表

    Parameters
    ----------
    bar_config : Dict
        Bar清洗配置字典（load_clean_config()["bar"]）

    Returns
    -------
    List[CleanRule] : 按优先级排序的规则列表
    """
    rules = []
    for rule_name, rule_def in BAR_RULE_MAP.items():
        config_key = rule_def["config_key"]
        # 特殊处理：allow_zero_volume 是反向开关（True=允许零成交量=不校验）
        if rule_name == "zero_volume_check":
            enabled = not bar_config.get(config_key, True)
        else:
            enabled = bool(bar_config.get(config_key, False))

        threshold = _extract_threshold(bar_config, rule_def["threshold_keys"])
        rule = CleanRule(
            name=rule_name,
            enabled=enabled,
            priority=rule_def["priority"],
            threshold=threshold,
            description=rule_def["description"],
            category=rule_def["category"],
        )
        rules.append(rule)

    rules.sort(key=lambda r: r.priority)
    logger.debug(f"构建Bar规则: {len(rules)}条, 启用{sum(1 for r in rules if r.enabled)}条")
    return rules


def build_tick_rules(tick_config: Dict[str, Any]) -> List[CleanRule]:
    """
    从Tick配置字典构建 CleanRule 对象列表

    Parameters
    ----------
    tick_config : Dict
        Tick清洗配置字典（load_clean_config()["tick"]）

    Returns
    -------
    List[CleanRule] : 按优先级排序的规则列表
    """
    rules = []
    for rule_name, rule_def in TICK_RULE_MAP.items():
        config_key = rule_def["config_key"]
        # symbol_check 在tick默认配置中没有显式键，默认启用
        if rule_name == "symbol_check":
            enabled = True  # Tick默认启用symbol校验
        else:
            enabled = bool(tick_config.get(config_key, False))

        threshold = _extract_threshold(tick_config, rule_def["threshold_keys"])
        rule = CleanRule(
            name=rule_name,
            enabled=enabled,
            priority=rule_def["priority"],
            threshold=threshold,
            description=rule_def["description"],
            category=rule_def["category"],
        )
        rules.append(rule)

    rules.sort(key=lambda r: r.priority)
    logger.debug(f"构建Tick规则: {len(rules)}条, 启用{sum(1 for r in rules if r.enabled)}条")
    return rules


def load_clean_rules(
    config_path: Optional[str] = None,
    use_settings: bool = True,
    data_type: Optional[str] = None,
) -> Dict[str, List[CleanRule]]:
    """
    加载清洗配置并转换为 CleanRule 对象列表

    Parameters
    ----------
    config_path : str | None
        配置文件路径
    use_settings : bool
        是否从全局 Settings 读取
    data_type : str | None
        指定数据类型（"bar"/"tick"），None则返回两种

    Returns
    -------
    Dict[str, List[CleanRule]] : {"bar": [...], "tick": [...]}
    """
    config = load_clean_config(config_path=config_path, use_settings=use_settings)
    result = {}

    if data_type is None or data_type == "bar":
        result["bar"] = build_bar_rules(config.get("bar", {}))
    if data_type is None or data_type == "tick":
        result["tick"] = build_tick_rules(config.get("tick", {}))

    return result


# ═══════════════════════════════════════════════
#  CleanRuleLibrary 规则库管理类 (阶段9新增)
# ═══════════════════════════════════════════════

class CleanRuleLibrary:
    """
    清洗规则库管理类

    统一管理 bar/tick 两种数据类型的清洗规则，支持：
      - 从配置加载规则
      - 按数据类型/分类/名称查询规则
      - 规则启用/禁用
      - 规则阈值更新
      - 导出为字典（用于持久化）
      - 与 CleanerBase 集成（应用规则到清洗器）

    使用方式:
        library = CleanRuleLibrary()
        library.load_from_config()

        # 获取Bar的单条校验规则
        bar_single_rules = library.get_rules("bar", category="single")

        # 禁用价格跳变检测
        library.disable_rule("bar", "price_jump")

        # 应用规则到清洗器
        cleaner = BarCleaner()
        library.apply_to_cleaner(cleaner, "bar")

    为实盘无缝对接预留:
      - 规则热更新：实盘运行中动态修改规则阈值
      - 按标的差异化：后续支持 symbol_overrides
      - 配置热重载：后续支持文件变更自动重新加载
    """

    def __init__(self):
        self._rules: Dict[str, Dict[str, CleanRule]] = {
            "bar": {},
            "tick": {},
        }
        self._loaded = False

    def load_from_config(
        self,
        config_path: Optional[str] = None,
        use_settings: bool = True,
    ) -> None:
        """
        从配置加载规则

        Parameters
        ----------
        config_path : str | None
            配置文件路径
        use_settings : bool
            是否从全局 Settings 读取
        """
        rules_dict = load_clean_rules(
            config_path=config_path,
            use_settings=use_settings,
        )
        for data_type, rules in rules_dict.items():
            self._rules[data_type] = {rule.name: rule for rule in rules}
        self._loaded = True
        logger.info(
            f"规则库加载完成: bar={len(self._rules['bar'])}条, "
            f"tick={len(self._rules['tick'])}条"
        )

    def load_from_dict(self, config: Dict[str, Any]) -> None:
        """
        从配置字典加载规则

        Parameters
        ----------
        config : Dict
            完整清洗配置字典（load_clean_config()的返回值）
        """
        if "bar" in config:
            self._rules["bar"] = {r.name: r for r in build_bar_rules(config["bar"])}
        if "tick" in config:
            self._rules["tick"] = {r.name: r for r in build_tick_rules(config["tick"])}
        self._loaded = True

    def get_rules(
        self,
        data_type: str,
        category: Optional[str] = None,
        enabled_only: bool = False,
    ) -> List[CleanRule]:
        """
        获取规则列表

        Parameters
        ----------
        data_type : str
            数据类型（"bar"/"tick"）
        category : str | None
            规则分类（"single"/"sequence"/"dedup"/"anomaly"），None则全部
        enabled_only : bool
            是否只返回启用的规则

        Returns
        -------
        List[CleanRule] : 按优先级排序的规则列表
        """
        if data_type not in self._rules:
            raise ValueError(f"未知数据类型: {data_type}, 可选: {list(self._rules.keys())}")

        rules = list(self._rules[data_type].values())
        if category:
            rules = [r for r in rules if r.category == category]
        if enabled_only:
            rules = [r for r in rules if r.enabled]

        rules.sort(key=lambda r: r.priority)
        return rules

    def get_rule(self, data_type: str, rule_name: str) -> Optional[CleanRule]:
        """获取单个规则"""
        return self._rules.get(data_type, {}).get(rule_name)

    def enable_rule(self, data_type: str, rule_name: str) -> bool:
        """启用规则，返回是否成功"""
        rule = self.get_rule(data_type, rule_name)
        if rule:
            rule.enable()
            return True
        return False

    def disable_rule(self, data_type: str, rule_name: str) -> bool:
        """禁用规则，返回是否成功"""
        rule = self.get_rule(data_type, rule_name)
        if rule:
            rule.disable()
            return True
        return False

    def update_threshold(
        self,
        data_type: str,
        rule_name: str,
        **threshold: Any,
    ) -> bool:
        """
        更新规则阈值

        Parameters
        ----------
        data_type : str
            数据类型
        rule_name : str
            规则名称
        **threshold : Any
            阈值参数（如 max_price_jump_pct=0.30）

        Returns
        -------
        bool : 是否成功
        """
        rule = self.get_rule(data_type, rule_name)
        if rule:
            rule.update_threshold(**threshold)
            return True
        return False

    def apply_to_cleaner(self, cleaner: Any, data_type: str) -> int:
        """
        将规则应用到清洗器（添加到cleaner的规则列表）

        Parameters
        ----------
        cleaner : CleanerBase
            清洗器实例
        data_type : str
            数据类型

        Returns
        -------
        int : 应用的规则数量
        """
        rules = self.get_rules(data_type, enabled_only=True)
        count = 0
        for rule in rules:
            # 只应用有自定义检查函数的规则，内置规则由cleaner自身实现
            if rule.check_fn is not None:
                cleaner.add_rule(rule)
                count += 1
        logger.debug(f"应用{count}条自定义规则到{data_type}清洗器")
        return count

    def to_dict(self, data_type: Optional[str] = None) -> Dict[str, Any]:
        """
        导出为字典（用于持久化）

        Parameters
        ----------
        data_type : str | None
            指定数据类型，None则全部

        Returns
        -------
        Dict : 规则字典
        """
        result = {}
        types = [data_type] if data_type else ["bar", "tick"]
        for dt in types:
            result[dt] = [rule.to_dict() for rule in self.get_rules(dt)]
        return result

    def get_summary(self) -> Dict[str, Any]:
        """获取规则库摘要"""
        summary = {}
        for data_type in ["bar", "tick"]:
            rules = self.get_rules(data_type)
            summary[data_type] = {
                "total": len(rules),
                "enabled": sum(1 for r in rules if r.enabled),
                "disabled": sum(1 for r in rules if not r.enabled),
                "by_category": {
                    cat: sum(1 for r in rules if r.category == cat)
                    for cat in ["single", "sequence", "dedup", "anomaly"]
                },
            }
        return summary

    @property
    def is_loaded(self) -> bool:
        """是否已加载规则"""
        return self._loaded

    def __repr__(self) -> str:
        if self._loaded:
            summary = self.get_summary()
            return (
                f"<CleanRuleLibrary "
                f"bar={summary['bar']['enabled']}/{summary['bar']['total']} "
                f"tick={summary['tick']['enabled']}/{summary['tick']['total']}>"
            )
        return "<CleanRuleLibrary (not loaded)>"


# ═══════════════════════════════════════════════
#  全局规则库单例 (阶段9新增)
# ═══════════════════════════════════════════════

_global_library: Optional[CleanRuleLibrary] = None


def get_rule_library() -> CleanRuleLibrary:
    """获取全局规则库单例（懒加载）"""
    global _global_library
    if _global_library is None:
        _global_library = CleanRuleLibrary()
        _global_library.load_from_config()
    return _global_library


def reload_rule_library() -> CleanRuleLibrary:
    """重新加载全局规则库（配置热重载）"""
    global _global_library
    _global_library = CleanRuleLibrary()
    _global_library.load_from_config()
    logger.info("全局规则库已重新加载")
    return _global_library


# ═══════════════════════════════════════════════
#  #C15修复：配置校验函数（值域校验）
# ═══════════════════════════════════════════════

def validate_config(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    #C15修复：校验清洗配置的值域，返回校验结果。

    校验关键配置项的取值范围，避免非法配置导致异常或误判。
    不修改传入的配置，只做只读校验。

    Parameters
    ----------
    config : Dict
        待校验的清洗配置字典

    Returns
    -------
    (bool, List[str]) : (是否通过, 错误信息列表)
    """
    errors: List[str] = []

    # 校验 bar/tick 配置中的数值阈值
    for data_type in ["bar", "tick"]:
        type_config = config.get(data_type, {})
        if not isinstance(type_config, dict):
            errors.append(f"{data_type} 配置必须是字典类型")
            continue

        # max_price_jump_pct: 必须 > 0
        val = type_config.get("max_price_jump_pct")
        if val is not None:
            try:
                if float(val) <= 0:
                    errors.append(f"{data_type}.max_price_jump_pct 必须 > 0，当前值: {val}")
            except (TypeError, ValueError):
                errors.append(f"{data_type}.max_price_jump_pct 必须是数值，当前值: {val}")

        # max_volume_ratio: 必须 > 0
        val = type_config.get("max_volume_ratio")
        if val is not None:
            try:
                if float(val) <= 0:
                    errors.append(f"{data_type}.max_volume_ratio 必须 > 0，当前值: {val}")
            except (TypeError, ValueError):
                errors.append(f"{data_type}.max_volume_ratio 必须是数值，当前值: {val}")

        # duplicate_strategy: 必须是合法值
        val = type_config.get("duplicate_strategy")
        if val is not None and val not in ["keep_first", "keep_last", "drop_all"]:
            errors.append(
                f"{data_type}.duplicate_strategy 必须是 keep_first/keep_last/drop_all，当前值: {val}"
            )

    # 校验 alert_thresholds
    alert_config = config.get("alert_thresholds", {})
    if alert_config:
        for level in ["critical", "error", "warning"]:
            val = alert_config.get(level)
            if val is not None:
                try:
                    if not (0 <= float(val) <= 1):
                        errors.append(f"alert_thresholds.{level} 必须在 [0, 1] 范围内，当前值: {val}")
                except (TypeError, ValueError):
                    errors.append(f"alert_thresholds.{level} 必须是数值，当前值: {val}")

    # 校验 allow_all_zero: 必须是布尔值
    for data_type in ["bar", "tick"]:
        val = config.get(data_type, {}).get("allow_all_zero")
        if val is not None and not isinstance(val, bool):
            errors.append(f"{data_type}.allow_all_zero 必须是布尔值，当前值: {val}")

    is_valid = len(errors) == 0
    if is_valid:
        logger.debug("配置校验通过")
    else:
        logger.warning(f"配置校验失败，共 {len(errors)} 个错误: {errors}")
    return is_valid, errors


# ═══════════════════════════════════════════════
#  #C17修复：规则热更新（配置文件变更自动重载）
# ═══════════════════════════════════════════════

class ConfigWatcher:
    """
    #C17修复：配置文件变更监视器（轮询方式，不依赖 watchdog）。

    定期检查配置文件的修改时间，发现变更时自动重新加载规则库。
    适用于实盘场景下动态调整清洗规则，无需重启服务。

    使用方式:
        watcher = ConfigWatcher(config_path="config/data_clean.yaml", interval=5.0)
        watcher.start()
        # ... 运行中修改配置文件，规则会自动重载
        watcher.stop()

    【防未来函数】只监控配置文件变更，不引入任何行情数据或未来信息。
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        interval: float = 5.0,
        on_reload: Optional[Callable[[], None]] = None,
    ):
        """
        初始化配置监视器

        Parameters
        ----------
        config_path : str | None
            配置文件路径，None则使用默认路径 config/data_clean.yaml
        interval : float
            轮询间隔（秒），默认5秒
        on_reload : Callable | None
            配置重载后的回调函数
        """
        if config_path is None:
            config_path = str(get_project_path("config", "data_clean.yaml"))
        self._config_path = Path(config_path)
        self._interval = interval
        self._on_reload = on_reload
        self._last_mtime: Optional[float] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 初始化文件修改时间
        if self._config_path.exists():
            self._last_mtime = self._config_path.stat().st_mtime

    def start(self) -> None:
        """启动监视器（后台线程）"""
        if self._running:
            logger.warning("配置监视器已在运行中")
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True, name="ConfigWatcher")
        self._thread.start()
        logger.info(f"配置监视器已启动: {self._config_path} (间隔={self._interval}s)")

    def stop(self) -> None:
        """停止监视器"""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("配置监视器已停止")

    def _watch_loop(self) -> None:
        """监视循环（后台线程执行）"""
        while not self._stop_event.is_set():
            try:
                if self._config_path.exists():
                    current_mtime = self._config_path.stat().st_mtime
                    if self._last_mtime is not None and current_mtime > self._last_mtime:
                        logger.info(f"检测到配置文件变更: {self._config_path}")
                        self._reload_config()
                    self._last_mtime = current_mtime
            except Exception as e:
                logger.warning(f"配置监视器检查失败: {e}")
            self._stop_event.wait(self._interval)

    def _reload_config(self) -> None:
        """重新加载配置和规则库"""
        try:
            # 重新加载全局规则库
            reload_rule_library()
            logger.info("规则库已自动重载")
            # 触发回调
            if self._on_reload:
                self._on_reload()
        except Exception as e:
            logger.error(f"配置重载失败: {e}", exc_info=True)

    @property
    def is_running(self) -> bool:
        """监视器是否在运行"""
        return self._running
