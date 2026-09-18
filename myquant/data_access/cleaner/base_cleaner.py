# 文件名：myquant/data_access/cleaner/base_cleaner.py
"""
base_cleaner.py - 清洗器抽象基类 (阶段9完整实现)
===================================================
阶段9高级增强迭代：将v1普通函数式清洗重构为面向对象清洗器体系。

核心类:
  - CleanRule: 清洗规则对象（规则名称/启用状态/阈值/检查函数/优先级）
  - CleanResult: 清洗结果对象（cleaned/bad/stats，兼容三元组解包）
  - CleanerBase: 清洗器抽象基类（模板方法模式 + 规则管理 + 事件钩子 + 插件注册）
  - CleanerRegistry: 清洗器注册器（插件式注册/发现/获取）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（事件钩子/流式清洗/规则热更新）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 清洗只使用当前及历史数据，不访问未来

防未来函数说明:
  - 单条校验只使用当前记录字段，不访问后续记录
  - 序列校验（价格跳变/成交量异常）只使用前一条记录，不使用后一条
  - 去重和排序基于时间戳，不引入未来数据
  - 事件钩子只传递当前已清洗的数据，不泄露未来信息
  - 实盘流式清洗时，每条记录只与已到达的历史记录比较

兼容性说明:
  - CleanResult 支持 `cleaned, bad, stats = result` 三元组解包
  - BarCleaner/TickCleaner 保留旧函数 `clean_bar_records()` / `clean_tick_records()`
  - 旧代码无需修改即可继续运行，新代码可使用面向对象接口

TODO(后续迭代):
  - 规则热更新（运行时动态修改规则阈值）
  - 清洗规则版本管理（规则变更历史/回滚）
  - 分布式清洗（多进程/多节点并行清洗）
  - 增量清洗（只清洗新增数据，不重复清洗全量）
  - 清洗结果缓存（相同输入复用清洗结果）

依赖: abc, typing, dataclasses, time, myquant.core.logger
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from myquant.core.logger import get_logger

logger = get_logger("data_clean.base")


# ═══════════════════════════════════════════════
#  清洗规则对象
# ═══════════════════════════════════════════════

@dataclass
class CleanRule:
    """
    清洗规则对象

    将v1的字典配置升级为对象化规则，支持：
      - 规则启用/禁用
      - 规则优先级（决定执行顺序）
      - 规则阈值（可动态调整）
      - 规则检查函数（可自定义）
      - 规则元数据（描述/分类/版本）

    Attributes
    ----------
    name : str
        规则名称（唯一标识，如 "ohlc_logic", "negative_price"）
    enabled : bool
        是否启用
    priority : int
        优先级（数字越小越先执行，默认0）
    threshold : Dict[str, Any]
        规则阈值参数（如 {"max_jump_pct": 0.22}）
    check_fn : Optional[Callable]
        自定义检查函数（None则使用清洗器内置逻辑）
    description : str
        规则描述
    category : str
        规则分类（single/sequence/dedup/anomaly）
    version : str
        规则版本
    """
    name: str
    enabled: bool = True
    priority: int = 0
    threshold: Dict[str, Any] = field(default_factory=dict)
    check_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], List[str]]] = None
    description: str = ""
    category: str = "single"
    version: str = "1.0.0"

    def __post_init__(self):
        if not self.name:
            raise ValueError("CleanRule.name 不能为空")

    def enable(self) -> None:
        """启用规则"""
        self.enabled = True
        logger.debug(f"规则已启用: {self.name}")

    def disable(self) -> None:
        """禁用规则"""
        self.enabled = False
        logger.debug(f"规则已禁用: {self.name}")

    def update_threshold(self, **kwargs: Any) -> None:
        """更新规则阈值"""
        self.threshold.update(kwargs)
        logger.debug(f"规则阈值已更新: {self.name} = {self.threshold}")

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（用于配置持久化）"""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "threshold": self.threshold,
            "description": self.description,
            "category": self.category,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CleanRule":
        """从字典反序列化"""
        return cls(
            name=data["name"],
            enabled=data.get("enabled", True),
            priority=data.get("priority", 0),
            threshold=data.get("threshold", {}),
            description=data.get("description", ""),
            category=data.get("category", "single"),
            version=data.get("version", "1.0.0"),
        )


# ═══════════════════════════════════════════════
#  清洗结果对象
# ═══════════════════════════════════════════════

@dataclass
class CleanResult:
    """
    清洗结果对象

    兼容v1三元组返回格式，支持解包：
        cleaned, bad, stats = clean_result

    Attributes
    ----------
    cleaned : List[Dict[str, Any]]
        清洗后有效数据列表
    bad : List[Dict[str, Any]]
        脏数据列表（每条附加 _errors 字段）
    stats : Dict[str, Any]
        清洗统计（total/valid/bad/removed_duplicates/price_jumps/...）
    duration_sec : float
        清洗耗时（秒）
    rules_applied : List[str]
        实际应用的规则名称列表
    warnings : List[str]
        清洗过程中的警告信息
    """
    cleaned: List[Dict[str, Any]] = field(default_factory=list)
    bad: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    duration_sec: float = 0.0
    rules_applied: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def __iter__(self) -> Tuple[List[Dict], List[Dict], Dict]:
        """
        支持三元组解包：
            cleaned, bad, stats = result
        """
        return iter((self.cleaned, self.bad, self.stats))

    def __getitem__(self, index: int) -> Any:
        """支持索引访问：result[0] = cleaned, result[1] = bad, result[2] = stats"""
        items = [self.cleaned, self.bad, self.stats]
        return items[index]

    def __len__(self) -> int:
        return 3

    @property
    def total(self) -> int:
        """总记录数"""
        return self.stats.get("total", 0)

    @property
    def valid_count(self) -> int:
        """有效记录数"""
        return self.stats.get("valid", 0)

    @property
    def bad_count(self) -> int:
        """脏数据数"""
        return self.stats.get("bad", 0)

    @property
    def quality_rate(self) -> float:
        """数据合格率（0~1）"""
        if self.total == 0:
            return 0.0
        return self.valid_count / self.total

    @property
    def quality_status(self) -> str:
        """
        质量状态评级
        - excellent: 合格率 >= 0.99
        - good: 合格率 >= 0.95
        - warning: 合格率 >= 0.80
        - poor: 合格率 < 0.80
        """
        rate = self.quality_rate
        if rate >= 0.99:
            return "excellent"
        elif rate >= 0.95:
            return "good"
        elif rate >= 0.80:
            return "warning"
        else:
            return "poor"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为完整字典（用于报告/持久化）"""
        return {
            "cleaned_count": len(self.cleaned),
            "bad_count": len(self.bad),
            "stats": self.stats,
            "duration_sec": self.duration_sec,
            "rules_applied": self.rules_applied,
            "warnings": self.warnings,
            "quality_rate": self.quality_rate,
            "quality_status": self.quality_status,
        }

    def summary(self) -> str:
        """生成人类可读的摘要"""
        return (
            f"清洗结果: 总{self.total}条, 有效{self.valid_count}条, "
            f"脏数据{self.bad_count}条, 合格率{self.quality_rate:.2%}, "
            f"评级{self.quality_status}, 耗时{self.duration_sec:.4f}s"
        )


# ═══════════════════════════════════════════════
#  清洗器抽象基类
# ═══════════════════════════════════════════════

class CleanerBase(ABC):
    """
    清洗器抽象基类（模板方法模式）

    子类必须实现:
      - data_type: 属性，返回数据类型标识（如 "bar", "tick"）
      - _validate_single(): 单条记录校验
      - _validate_sequence(): 序列级校验（可选，默认空实现）

    模板方法 clean() 执行流程:
      1. on_clean_start() — 清洗开始钩子
      2. pre_clean() — 预处理（可选重写）
      3. _validate_single() — 单条校验（抽象方法，子类实现）
      4. _validate_sequence() — 序列校验（可选重写）
      5. _deduplicate() — 去重（可选重写）
      6. _detect_anomalies() — 异常检测（可选重写）
      7. post_clean() — 后处理（可选重写）
      8. _sort_output() — 排序输出
      9. on_clean_complete() — 清洗完成钩子

    事件钩子（为阶段9事件总线联动做准备）:
      - on_clean_start(result_meta)
      - on_clean_complete(result)
      - on_bad_record(record, errors)
      - on_anomaly_detected(record, warning)
      - on_rule_triggered(rule_name, record)

    使用方式:
        class BarCleaner(CleanerBase):
            @property
            def data_type(self) -> str:
                return "bar"

            def _validate_single(self, record, config):
                errors = []
                # ... 校验逻辑 ...
                return errors

        cleaner = BarCleaner(config={"max_price_jump_pct": 0.2})
        result = cleaner.clean(raw_records)
        cleaned, bad, stats = result  # 兼容三元组解包
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        rules: Optional[List[CleanRule]] = None,
    ):
        """
        初始化清洗器

        Parameters
        ----------
        config : Dict | None
            清洗配置（字典形式，兼容v1配置格式）
        rules : List[CleanRule] | None
            清洗规则列表（对象化规则，优先级高于config）
        """
        self._config: Dict[str, Any] = self._default_config()
        if config:
            self._config.update(config)

        self._rules: Dict[str, CleanRule] = {}
        if rules:
            for rule in rules:
                self._rules[rule.name] = rule

        # 事件钩子回调函数列表
        self._hooks: Dict[str, List[Callable]] = {
            "on_clean_start": [],
            "on_clean_complete": [],
            "on_bad_record": [],
            "on_anomaly_detected": [],
            "on_rule_triggered": [],
        }

        # #C6修复：钩子异常处理策略
        # 默认 False（异常只记日志，不中断清洗）；
        # 可通过 config["raise_on_hook_error"]=True 在关键场景抛出异常
        self._raise_on_hook_error: bool = bool(self._config.get("raise_on_hook_error", False))

        logger.info(
            f"清洗器初始化: type={self.data_type}, "
            f"rules={len(self._rules)}, config_keys={len(self._config)}"
        )

    # ═══════════════════════════════════════════
    #  抽象属性/方法（子类必须实现）
    # ═══════════════════════════════════════════

    @property
    @abstractmethod
    def data_type(self) -> str:
        """数据类型标识（如 "bar", "tick"），子类必须实现"""
        ...

    @abstractmethod
    def _default_config(self) -> Dict[str, Any]:
        """返回默认配置字典，子类必须实现"""
        ...

    @abstractmethod
    def _validate_single(
        self,
        record: Dict[str, Any],
        config: Dict[str, Any],
    ) -> List[str]:
        """
        单条记录校验，返回错误列表

        Parameters
        ----------
        record : Dict
            单条记录
        config : Dict
            生效配置

        Returns
        -------
        List[str] : 错误信息列表（空列表表示通过）
        """
        ...

    def validate_single_with_rules(
        self,
        record: Dict[str, Any],
        config: Dict[str, Any],
    ) -> List[str]:
        """
        单条记录校验（含自定义规则）

        #C2修复：封装 _validate_single + 自定义规则执行逻辑，
        确保 clean() 和 CleanPipeline.validate() 行为一致。

        执行顺序:
          1. 调用子类实现的 _validate_single（内置校验逻辑）
          2. 遍历 self._rules 中 category=="single" 且启用的自定义规则，
             执行 rule.check_fn，将错误合并到结果中
          3. 触发 on_rule_triggered 钩子（当自定义规则触发时）

        Parameters
        ----------
        record : Dict
            单条记录
        config : Dict
            生效配置

        Returns
        -------
        List[str] : 错误信息列表（空列表表示通过）
        """
        errors = self._validate_single(record, config)
        # 应用自定义规则检查函数
        for rule in self._rules.values():
            if rule.enabled and rule.check_fn and rule.category == "single":
                rule_errors = rule.check_fn(record, config)
                if rule_errors:
                    errors.extend(rule_errors)
                    self._trigger_hook("on_rule_triggered", rule.name, record)
        # #C8修复：校验通过的记录统一初始化 _warnings 字段，
        # 避免后续序列校验/异常检测中分散的 setdefault 逻辑
        if not errors:
            record.setdefault("_warnings", [])
        return errors

    # ═══════════════════════════════════════════
    #  可重写方法（子类可选实现）
    # ═══════════════════════════════════════════

    def _validate_sequence(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """
        序列级校验（价格跳变/成交量异常/时间单调性等）

        默认空实现，子类可重写。
        注意：只允许访问当前记录和之前的记录，严禁访问后续记录（防未来函数）。

        Returns
        -------
        (records, anomaly_counts)
          records: 标记后的记录列表（原地修改 _warnings 字段）
          anomaly_counts: 异常类型计数
        """
        return records, {}

    def _deduplicate(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        去重处理

        默认空实现（不去重），子类可重写。

        Returns
        -------
        (deduped, removed_count)
        """
        return records, 0

    def _detect_anomalies(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
        """
        异常检测（在去重之后执行）

        默认委托给 _validate_sequence，子类可重写分离逻辑。
        返回 (records, [], anomaly_counts)——基类不产生剔除，bad 恒为空。

        Returns
        -------
        (records, bad_records, anomaly_counts)
        """
        records, counts = self._validate_sequence(records, config)
        return records, [], counts

    def _sort_output(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        排序输出

        默认按时间戳升序排序，子类可重写。
        """
        if config.get("sort_by_timestamp", True):
            records.sort(key=lambda r: self._get_timestamp(r))
        return records

    def pre_clean(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        预处理钩子（在单条校验之前执行）

        默认原样返回，子类可重写（如字段标准化、类型转换）。
        """
        return records

    def post_clean(
        self,
        cleaned: List[Dict[str, Any]],
        bad: List[Dict[str, Any]],
        stats: Dict[str, Any],
    ) -> Tuple[List[Dict], List[Dict], Dict]:
        """
        后处理钩子（在排序之后、返回结果之前执行）

        默认原样返回，子类可重写（如附加元数据、过滤警告记录）。
        """
        return cleaned, bad, stats

    # ═══════════════════════════════════════════
    #  模板方法：clean()
    # ═══════════════════════════════════════════

    def clean(
        self,
        records: List[Dict[str, Any]],
        config: Optional[Dict[str, Any]] = None,
    ) -> CleanResult:
        """
        执行清洗（模板方法，子类不应重写此方法）

        Parameters
        ----------
        records : List[Dict]
            原始数据列表
        config : Dict | None
            临时覆盖配置（None则使用初始化时的配置）

        Returns
        -------
        CleanResult : 清洗结果对象（支持三元组解包）
        """
        start_time = time.time()
        warnings: List[str] = []
        rules_applied: List[str] = []

        # 合并配置
        cfg = dict(self._config)
        if config:
            cfg.update(config)

        # 应用对象化规则到配置
        for rule in sorted(self._rules.values(), key=lambda r: r.priority):
            if rule.enabled:
                rules_applied.append(rule.name)
                # 将规则阈值合并到配置
                for key, value in rule.threshold.items():
                    cfg[key] = value

        total = len(records)
        result_meta = {
            "data_type": self.data_type,
            "total": total,
            "start_time": start_time,
        }
        self._trigger_hook("on_clean_start", result_meta)

        if total == 0:
            logger.info("输入数据为空，跳过清洗")
            stats = {
                "total": 0, "valid": 0, "bad": 0,
                "removed_duplicates": 0, "price_jumps": 0,
                "volume_anomalies": 0, "time_violations": 0,
                "duration_sec": 0.0,
            }
            result = CleanResult(
                cleaned=[], bad=[], stats=stats,
                duration_sec=0.0, rules_applied=rules_applied, warnings=warnings,
            )
            self._trigger_hook("on_clean_complete", result)
            return result

        logger.info(f"开始清洗{self.data_type}数据: 共 {total} 条")

        # ═══ 步骤1: 预处理 ═══
        records = self.pre_clean(records)

        # ═══ 步骤2: 单条基础校验 ═══
        valid: List[Dict[str, Any]] = []
        bad: List[Dict[str, Any]] = []

        for rec in records:
            # #C2修复：使用 validate_single_with_rules 统一执行内置校验+自定义规则
            errors = self.validate_single_with_rules(rec, cfg)

            if errors:
                bad_rec = dict(rec)
                bad_rec["_errors"] = errors
                bad.append(bad_rec)
                self._trigger_hook("on_bad_record", bad_rec, errors)
                for err in errors:
                    logger.debug(f"脏数据: {err} | record={rec}")
            else:
                valid.append(rec)

        logger.info(f"单条校验完成: 有效 {len(valid)}, 脏数据 {len(bad)}")

        # ═══ 步骤3: 序列校验（时间单调性等） ═══
        valid, seq_counts = self._validate_sequence(valid, cfg)
        for warning in self._extract_warnings(valid):
            warnings.append(warning)
            self._trigger_hook("on_anomaly_detected", warning)

        # ═══ 步骤4: 去重 ═══
        removed_duplicates = 0
        if cfg.get("check_duplicate_timestamp", True):
            valid, removed_duplicates = self._deduplicate(valid, cfg)
            if removed_duplicates > 0:
                logger.info(f"去重完成: 移除 {removed_duplicates} 条重复记录")

        # ═══ 步骤5: 异常检测（价格跳变/成交量异常，支持mark标记/drop剔除） ═══
        valid, anomaly_bad, anomaly_counts = self._detect_anomalies(valid, cfg)
        if anomaly_bad:
            bad.extend(anomaly_bad)
        for warning in self._extract_warnings(valid):
            if warning not in warnings:
                warnings.append(warning)
                self._trigger_hook("on_anomaly_detected", warning)

        # ═══ 步骤6: 后处理 ═══
        valid, bad, extra_stats = self.post_clean(valid, bad, {})

        # ═══ 步骤7: 排序输出 ═══
        valid = self._sort_output(valid, cfg)

        # ═══ 统计汇总 ═══
        duration = time.time() - start_time
        stats = {
            "total": total,
            "valid": len(valid),
            "bad": len(bad),
            "removed_duplicates": removed_duplicates,
            "price_jumps": anomaly_counts.get("price_jump", seq_counts.get("price_jump", 0)),
            "volume_anomalies": anomaly_counts.get("volume_anomaly", seq_counts.get("volume_anomaly", 0)),
            "time_violations": seq_counts.get("time_violation", 0),
            "duration_sec": round(duration, 4),
        }
        stats.update(extra_stats)

        result = CleanResult(
            cleaned=valid,
            bad=bad,
            stats=stats,
            duration_sec=round(duration, 4),
            rules_applied=rules_applied,
            warnings=warnings,
        )

        logger.info(
            f"{self.data_type}清洗完成: 有效={stats['valid']}, 脏数据={stats['bad']}, "
            f"去重={stats['removed_duplicates']}, 耗时={stats['duration_sec']}s"
        )

        self._trigger_hook("on_clean_complete", result)
        return result

    # ═══════════════════════════════════════════
    #  规则管理
    # ═══════════════════════════════════════════

    def add_rule(self, rule: CleanRule) -> None:
        """添加清洗规则"""
        self._rules[rule.name] = rule
        logger.info(f"添加清洗规则: {rule.name} (priority={rule.priority})")

    def remove_rule(self, rule_name: str) -> Optional[CleanRule]:
        """移除清洗规则，返回被移除的规则（不存在返回None）"""
        rule = self._rules.pop(rule_name, None)
        if rule:
            logger.info(f"移除清洗规则: {rule_name}")
        return rule

    def get_rule(self, rule_name: str) -> Optional[CleanRule]:
        """获取清洗规则"""
        return self._rules.get(rule_name)

    def enable_rule(self, rule_name: str) -> bool:
        """启用规则，返回是否成功"""
        rule = self._rules.get(rule_name)
        if rule:
            rule.enable()
            return True
        return False

    def disable_rule(self, rule_name: str) -> bool:
        """禁用规则，返回是否成功"""
        rule = self._rules.get(rule_name)
        if rule:
            rule.disable()
            return True
        return False

    def list_rules(self, enabled_only: bool = False) -> List[CleanRule]:
        """列出所有规则（可只列启用的）"""
        rules = list(self._rules.values())
        if enabled_only:
            rules = [r for r in rules if r.enabled]
        return sorted(rules, key=lambda r: r.priority)

    # ═══════════════════════════════════════════
    #  配置管理
    # ═══════════════════════════════════════════

    @property
    def config(self) -> Dict[str, Any]:
        """当前配置（只读副本）"""
        return dict(self._config)

    def update_config(self, **kwargs: Any) -> None:
        """更新配置"""
        self._config.update(kwargs)
        logger.info(f"清洗器配置已更新: {list(kwargs.keys())}")

    def reset_config(self) -> None:
        """重置为默认配置"""
        self._config = self._default_config()
        logger.info("清洗器配置已重置为默认")

    # ═══════════════════════════════════════════
    #  事件钩子
    # ═══════════════════════════════════════════

    def register_hook(self, event: str, callback: Callable) -> None:
        """
        注册事件钩子回调

        Parameters
        ----------
        event : str
            事件名称（on_clean_start/on_clean_complete/on_bad_record/on_anomaly_detected/on_rule_triggered）
        callback : Callable
            回调函数
        """
        if event not in self._hooks:
            raise ValueError(f"未知事件: {event}, 可选: {list(self._hooks.keys())}")
        self._hooks[event].append(callback)
        logger.debug(f"注册事件钩子: {event} -> {callback.__name__}")

    def unregister_hook(self, event: str, callback: Callable) -> bool:
        """注销事件钩子回调，返回是否成功"""
        if event in self._hooks and callback in self._hooks[event]:
            self._hooks[event].remove(callback)
            return True
        return False

    def _trigger_hook(self, event: str, *args: Any, **kwargs: Any) -> None:
        """
        触发事件钩子（内部方法）

        #C6修复：增加 raise_on_hook_error 配置支持。
        默认 False（异常只记日志，不中断清洗）；
        当 config["raise_on_hook_error"]=True 时，钩子异常重新抛出，
        便于关键场景（如实盘）及时发现回调失败。
        """
        for callback in self._hooks.get(event, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                logger.error(f"事件钩子执行异常: {event} -> {callback.__name__}: {e}", exc_info=True)
                # #C6修复：关键场景下重新抛出异常，不静默吞掉
                if self._raise_on_hook_error:
                    raise

    # ═══════════════════════════════════════════
    #  工具方法
    # ═══════════════════════════════════════════

    @staticmethod
    def _get_timestamp(record: Dict[str, Any]) -> float:
        """从记录中提取时间戳（优先timestamp字段，其次date字段）"""
        ts = record.get("timestamp")
        if ts is not None:
            try:
                return float(ts)
            except (TypeError, ValueError):
                pass
        date_str = record.get("date") or record.get("trade_date") or record.get("datetime")
        if date_str:
            try:
                from myquant.utils.time_utils import to_timestamp
                return to_timestamp(date_str)
            except (ValueError, TypeError):
                pass
        return 0.0

    @staticmethod
    def _extract_warnings(records: List[Dict[str, Any]]) -> List[str]:
        """
        从记录中提取所有_warnings信息

        #C5修复：使用 dict.fromkeys 保持顺序的 O(n) 去重，
        替代原 O(n²) 的列表去重，同时确保无重复警告。
        """
        warnings = []
        for rec in records:
            for w in rec.get("_warnings", []):
                warnings.append(w)
        # 保持插入顺序的去重（Python 3.7+ dict 有序）
        return list(dict.fromkeys(warnings))

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"type={self.data_type} "
            f"rules={len(self._rules)} "
            f"config_keys={len(self._config)}>"
        )


# ═══════════════════════════════════════════════
#  清洗器注册器（插件式）
# ═══════════════════════════════════════════════

class CleanerRegistry:
    """
    清洗器注册器（插件式注册/发现/获取）

    支持:
      - 按数据类型注册清洗器类
      - 按名称获取清洗器实例
      - 自动发现已注册的清洗器
      - 外部插件入口点注册（兼容setuptools entry_points）

    使用方式:
        registry = CleanerRegistry()
        registry.register("bar", BarCleaner)
        registry.register("tick", TickCleaner)

        cleaner = registry.create("bar", config={...})
        result = cleaner.clean(raw_data)

        # 列出所有已注册类型
        print(registry.list_types())  # ["bar", "tick"]
    """

    _instance: Optional["CleanerRegistry"] = None

    def __new__(cls) -> "CleanerRegistry":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._cleaners: Dict[str, Type[CleanerBase]] = {}
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_cleaners"):
            self._cleaners: Dict[str, Type[CleanerBase]] = {}

    def register(self, data_type: str, cleaner_class: Type[CleanerBase]) -> None:
        """
        注册清洗器类

        Parameters
        ----------
        data_type : str
            数据类型标识（如 "bar", "tick"）
        cleaner_class : Type[CleanerBase]
            清洗器类（必须是CleanerBase的子类）
        """
        if not issubclass(cleaner_class, CleanerBase):
            raise TypeError(f"cleaner_class 必须是 CleanerBase 的子类， got {cleaner_class}")
        self._cleaners[data_type] = cleaner_class
        logger.info(f"注册清洗器: {data_type} -> {cleaner_class.__name__}")

    def unregister(self, data_type: str) -> Optional[Type[CleanerBase]]:
        """注销清洗器，返回被注销的类"""
        return self._cleaners.pop(data_type, None)

    def get_class(self, data_type: str) -> Optional[Type[CleanerBase]]:
        """获取清洗器类"""
        return self._cleaners.get(data_type)

    def create(
        self,
        data_type: str,
        config: Optional[Dict[str, Any]] = None,
        rules: Optional[List[CleanRule]] = None,
    ) -> CleanerBase:
        """
        创建清洗器实例

        Parameters
        ----------
        data_type : str
            数据类型标识
        config : Dict | None
            清洗配置
        rules : List[CleanRule] | None
            清洗规则列表

        Returns
        -------
        CleanerBase : 清洗器实例

        Raises
        ------
        KeyError : 数据类型未注册
        """
        cleaner_class = self._cleaners.get(data_type)
        if cleaner_class is None:
            raise KeyError(
                f"未注册的数据类型: {data_type}, "
                f"已注册: {list(self._cleaners.keys())}"
            )
        return cleaner_class(config=config, rules=rules)

    def list_types(self) -> List[str]:
        """列出所有已注册的数据类型"""
        return sorted(self._cleaners.keys())

    def has(self, data_type: str) -> bool:
        """检查数据类型是否已注册"""
        return data_type in self._cleaners

    def discover_entry_points(self, group: str = "myquant.cleaners") -> int:
        """
        自动发现外部插件清洗器（通过setuptools entry_points）

        Parameters
        ----------
        group : str
            entry_points组名

        Returns
        -------
        int : 新注册的清洗器数量
        """
        try:
            from importlib.metadata import entry_points
            eps = entry_points()
            if hasattr(eps, "select"):
                cleaners = eps.select(group=group)
            else:
                cleaners = eps.get(group, [])  # type: ignore[attr-defined]

            count = 0
            for ep in cleaners:
                try:
                    cleaner_class = ep.load()
                    self.register(ep.name, cleaner_class)
                    count += 1
                except Exception as e:
                    logger.error(f"加载清洗器插件失败: {ep.name}: {e}")
            return count
        except ImportError:
            logger.debug("importlib.metadata 不可用，跳过插件发现")
            return 0

    def __repr__(self) -> str:
        return f"<CleanerRegistry types={list(self._cleaners.keys())}>"


# ═══════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════

def get_cleaner_registry() -> CleanerRegistry:
    """获取全局清洗器注册器单例"""
    return CleanerRegistry()


def create_cleaner(
    data_type: str,
    config: Optional[Dict[str, Any]] = None,
    rules: Optional[List[CleanRule]] = None,
) -> CleanerBase:
    """
    便捷函数：创建清洗器实例

    Parameters
    ----------
    data_type : str
        数据类型（如 "bar", "tick"）
    config : Dict | None
        清洗配置
    rules : List[CleanRule] | None
        清洗规则列表

    Returns
    -------
    CleanerBase : 清洗器实例
    """
    return get_cleaner_registry().create(data_type, config=config, rules=rules)
