# 文件名：myquant/data_access/cleaner/__init__.py
"""
cleaner - 数据清洗子包 (阶段9面向对象重构)
=============================================
v1普通函数 + 阶段9面向对象清洗器，两者并存、完全兼容。

v1普通函数:
  - clean_bar_records:  K线Bar批量清洗
  - clean_tick_records: Tick逐笔批量清洗
  - run_clean_pipeline: 清洗流水线编排
  - load_clean_config:  加载清洗配置
  - generate_quality_report: 生成质量报告

阶段9面向对象类:
  - CleanerBase:     清洗器抽象基类
  - CleanRule:       清洗规则对象
  - CleanResult:     清洗结果对象（兼容三元组解包）
  - CleanerRegistry: 清洗器注册器（插件式）
  - BarCleaner:      K线Bar清洗器（继承CleanerBase）
  - TickCleaner:     Tick清洗器（继承CleanerBase，阶段9任务3已实现）

兼容性:
  - 旧函数 clean_bar_records() 完全保留
  - BarCleaner.clean() 返回 CleanResult，支持 cleaned, bad, stats = result
  - 两者内部复用同一套校验逻辑，结果一致
"""

from .bar_cleaner import BarCleaner, clean_bar_records
from .base_cleaner import (
    CleanerBase,
    CleanerRegistry,
    CleanResult,
    CleanRule,
    create_cleaner,
    get_cleaner_registry,
)
from .pipeline import CleanPipeline, PipelineResult, PipelineStep, run_clean_pipeline
from .quality_reporter import generate_quality_report
from .rules_loader import (
    CleanRuleLibrary,
    build_bar_rules,
    build_tick_rules,
    get_rule_library,
    load_clean_config,
    load_clean_rules,
    reload_rule_library,
)
from .tick_cleaner import TickCleaner, clean_tick_records

__all__ = [
    # v1普通函数
    "clean_bar_records",
    "clean_tick_records",
    "run_clean_pipeline",
    "load_clean_config",
    "generate_quality_report",
    # 阶段9规则对象化
    "load_clean_rules",
    "build_bar_rules",
    "build_tick_rules",
    "CleanRuleLibrary",
    "get_rule_library",
    "reload_rule_library",
    # 阶段9流水线对象
    "CleanPipeline",
    "PipelineStep",
    "PipelineResult",
    # 阶段9面向对象类
    "CleanerBase",
    "CleanRule",
    "CleanResult",
    "CleanerRegistry",
    "get_cleaner_registry",
    "create_cleaner",
    "BarCleaner",
    "TickCleaner",
]
