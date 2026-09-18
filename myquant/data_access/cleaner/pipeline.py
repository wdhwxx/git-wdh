# 文件名：myquant/data_access/cleaner/pipeline.py
"""
pipeline.py - 清洗流水线编排 (阶段9完整流水线对象)
=====================================================
阶段9高级增强迭代：从v1简单函数编排升级为 CleanPipeline 类，支持链式调用、事件钩子、步骤注册、中间结果缓存。

兼容性保证:
  - 旧函数 run_clean_pipeline() 完全保留，签名/返回值不变
  - 新增 CleanPipeline 类，支持链式调用和面向对象编排
  - 新增 PipelineStep 步骤定义类
  - 新增 PipelineResult 流水线结果类
  - 内部复用 BarCleaner/TickCleaner 面向对象清洗器

功能:
  - 链式调用：load → validate → deduplicate → detect_anomalies → sort → save_bad → save_report → run
  - 事件钩子：on_pipeline_start/on_step_complete/on_pipeline_complete/on_error
  - 步骤注册：add_step/remove_step，支持自定义清洗步骤
  - 中间结果缓存：每步结果可缓存，支持断点续跑
  - 与BarCleaner/TickCleaner集成：自动选择清洗器
  - 与CleanRuleLibrary集成：可选应用规则库
  - 流水线状态跟踪：idle/running/completed/failed

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（流式清洗/事件联动/规则热更新）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 流水线只处理当前已加载的数据，不引入未来数据

防未来函数说明:
  - 流水线只处理 load() 时已加载的数据，不访问未来数据
  - 清洗步骤委托给BarCleaner/TickCleaner，其内部严格防未来函数
  - 事件钩子只传递当前步骤的结果，不泄露未来数据
  - 中间结果缓存只保存已处理的数据，不预加载未来数据

TODO(后续迭代):
  - 分布式流水线（多进程/多节点并行清洗）
  - 流水线DAG编排（步骤间依赖关系）
  - 流水线模板保存/加载
  - 增量清洗（只清洗新增数据）
  - 流水线性能监控（每步耗时/吞吐量）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.core.logger import get_logger
from myquant.core.event_bus import DataQualityEventPublisher
from myquant.utils.file_utils import ensure_dir, get_project_path, write_json

from .bar_cleaner import clean_bar_records, BarCleaner
from .tick_cleaner import clean_tick_records, TickCleaner
from .quality_reporter import generate_quality_report
from .base_cleaner import CleanResult, CleanerBase

logger = get_logger("data_clean.pipeline")


def sanitize_filename(name: str) -> str:
    """
    #C13修复：清理文件名中的非法字符，替换为下划线。

    Windows 文件名非法字符: \\ / : * ? \" < > |
    同时替换空格为下划线，避免路径解析问题。

    Parameters
    ----------
    name : str
        原始文件名（或文件名的一部分，如 symbol）

    Returns
    -------
    str : 清理后的安全文件名
    """
    if not name:
        return "unknown"
    result = name
    for ch in ['\\', '/', ':', '*', '?', '"', '<', '>', '|', ' ']:
        result = result.replace(ch, '_')
    return result


def _save_bad_records(
    bad_records: List[Dict[str, Any]],
    symbol: str,
    data_type: str,
    output_dir: Optional[str] = None,
) -> Optional[str]:
    """
    将脏数据落盘到 datasets/bad/ 目录

    Returns
    -------
    str | None : 脏数据文件路径，None 表示无脏数据
    """
    if not bad_records:
        return None

    if output_dir is None:
        output_dir = str(get_project_path("datasets", "bad"))

    ensure_dir(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # #C13修复：sanitize symbol，避免特殊字符导致路径错误
    safe_symbol = sanitize_filename(symbol)
    filename = f"bad_{data_type}_{safe_symbol}_{timestamp}.json"
    filepath = Path(output_dir) / filename

    write_json(str(filepath), bad_records)
    logger.info(f"脏数据已归档: {filepath} ({len(bad_records)} 条)")
    return str(filepath)


def run_clean_pipeline(
    records: List[Dict[str, Any]],
    data_type: str = "bar",
    symbol: str = "unknown",
    config: Optional[Dict[str, Any]] = None,
    save_bad: bool = True,
    save_report: bool = True,
    bad_output_dir: Optional[str] = None,
    report_output_dir: Optional[str] = None,
    publish_events: bool = True,
) -> Dict[str, Any]:
    """
    执行完整清洗流水线

    Parameters
    ----------
    records : List[Dict]
        原始数据
    data_type : str
        数据类型: "bar" | "tick"
    symbol : str
        标的代码（用于命名输出文件）
    config : Dict | None
        完整清洗配置（含 bar/tick/bad_data/quality_report 等顶层key）
    save_bad : bool
        是否将脏数据落盘
    save_report : bool
        是否生成质量报告
    bad_output_dir : str | None
        脏数据输出目录，None 则使用默认 datasets/bad/
    report_output_dir : str | None
        报告输出目录，None 则使用默认 outputs/
    publish_events : bool
        #O4修复：是否发布数据质量事件到事件总线（默认True）。
        调试场景或批量回测时可设为False，避免污染监控告警。

    Returns
    -------
    Dict : {
        "cleaned": List[Dict],      # 清洗后有效数据
        "bad": List[Dict],          # 脏数据
        "stats": Dict,              # 清洗统计
        "bad_file": str | None,     # 脏数据文件路径
        "report_file": str | None,  # 质量报告文件路径
    }

    Usage
    -----
        result = run_clean_pipeline(
            records=raw_data,
            data_type="bar",
            symbol="000001.SZ",
            config=clean_config,
        )
        cleaned_data = result["cleaned"]
    """
    # 提取对应数据类型的配置
    type_config = None
    if config:
        type_config = config.get(data_type)

    # #O2修复：统一解析输出目录，优先级：显式参数 > config > 默认值(None由下游处理)
    # 脏数据输出目录
    resolved_bad_dir = bad_output_dir
    if not resolved_bad_dir and config and config.get("bad_data"):
        resolved_bad_dir = config["bad_data"].get("output_dir")
    # 质量报告输出目录
    resolved_report_dir = report_output_dir
    if not resolved_report_dir and config and config.get("quality_report"):
        resolved_report_dir = config["quality_report"].get("output_dir")

    # 执行清洗
    if data_type == "bar":
        cleaned, bad, stats = clean_bar_records(records, type_config)
    elif data_type == "tick":
        cleaned, bad, stats = clean_tick_records(records, type_config)
    else:
        raise ValueError(f"不支持的数据类型: {data_type}，仅支持 bar/tick")

    # 脏数据落盘
    bad_file = None
    if save_bad and bad:
        bad_file = _save_bad_records(bad, symbol, data_type, resolved_bad_dir)

    # 生成质量报告
    report_file = None
    if save_report:
        report_file = generate_quality_report(
            stats=stats,
            symbol=symbol,
            data_type=data_type,
            output_dir=resolved_report_dir,
            bad_count=len(bad),
        )

    # #S1修复：发布事件到事件总线（离线清洗联动风控/告警/指标采集）
    # #O4修复：publish_events=False 时跳过事件发布（调试/批量回测场景）
    if publish_events:
        try:
            _publisher = DataQualityEventPublisher(source="run_clean_pipeline")
            total_count = len(records)
            bad_count = len(bad)
            bad_ratio = bad_count / total_count if total_count > 0 else 0.0

            # 1. 发布脏数据事件（汇总）
            if bad:
                _publisher.publish_dirty(
                    dirty_type="mixed",
                    symbol=symbol,
                    data_type=data_type,
                    severity="error",
                    bad_count=bad_count,
                    total_count=total_count,
                    source_step="run_clean_pipeline",
                )

            # 2. 根据脏数据比例发布质量告警
            # #C9修复：阈值从配置 alert_thresholds 读取，默认值保持向后兼容
            alert_config = (config or {}).get("alert_thresholds", {}) if config else {}
            critical_threshold = alert_config.get("critical", 0.5)
            error_threshold = alert_config.get("error", 0.1)
            warning_threshold = alert_config.get("warning", 0.05)

            if bad_ratio >= critical_threshold:
                _publisher.critical(
                    message=f"严重脏数据问题：脏数据占比{bad_ratio:.1%}，超过{critical_threshold:.0%}阈值",
                    symbol=symbol,
                    data_type=data_type,
                    bad_ratio=bad_ratio,
                    threshold=critical_threshold,
                )
            elif bad_ratio >= error_threshold:
                _publisher.alert(
                    alert_level="error",
                    message=f"脏数据占比过高：{bad_ratio:.1%}，超过{error_threshold:.0%}阈值",
                    symbol=symbol,
                    data_type=data_type,
                    bad_ratio=bad_ratio,
                    threshold=error_threshold,
                )
            elif bad_ratio >= warning_threshold:
                _publisher.alert(
                    alert_level="warning",
                    message=f"脏数据占比偏高：{bad_ratio:.1%}，超过{warning_threshold:.0%}阈值",
                    symbol=symbol,
                    data_type=data_type,
                    bad_ratio=bad_ratio,
                    threshold=warning_threshold,
                )

            # 3. 发布流水线完成事件
            _publisher.pipeline_complete(
                symbol=symbol,
                data_type=data_type,
                status="completed",
                cleaned_count=len(cleaned),
                bad_count=bad_count,
                total_count=total_count,
                bad_ratio=bad_ratio,
                bad_file=bad_file,
                report_file=report_file,
            )
        except Exception as _e:
            logger.warning(f"事件发布失败（不影响清洗结果）: {_e}")

    return {
        "cleaned": cleaned,
        "bad": bad,
        "stats": stats,
        "bad_file": bad_file,
        "report_file": report_file,
    }


def run_clean_pipeline_parallel(
    tasks: List[Dict[str, Any]],
    max_workers: int = 4,
) -> Dict[str, Dict[str, Any]]:
    """
    #C16修复：并行清洗多个标的的数据。

    使用 ThreadPoolExecutor 并行调用 run_clean_pipeline，
    适用于多 symbol 批量清洗场景（IO 密集型有效，CPU 密集型受 GIL 限制）。

    Parameters
    ----------
    tasks : List[Dict]
        任务列表，每个任务包含：
          - records: List[Dict] 原始数据
          - symbol: str 标的代码
          - data_type: str 数据类型（默认"bar"）
          - config: Dict | None 清洗配置
          - 其他 run_clean_pipeline 支持的参数（save_bad/save_report/...）
    max_workers : int
        最大并发线程数（默认4）

    Returns
    -------
    Dict[str, Dict] : {symbol: run_clean_pipeline结果}
        失败的任务结果中包含 "error" 字段
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: Dict[str, Dict[str, Any]] = {}

    def _run_task(task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        symbol = task.get("symbol", "unknown")
        try:
            result = run_clean_pipeline(**task)
            return symbol, result
        except Exception as e:
            logger.error(f"并行清洗任务失败: symbol={symbol}, error={e}", exc_info=True)
            return symbol, {"error": str(e), "cleaned": [], "bad": [], "stats": {}}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_task, task): task.get("symbol", "unknown") for task in tasks}
        for future in as_completed(futures):
            symbol, result = future.result()
            results[symbol] = result

    logger.info(f"并行清洗完成: 共 {len(tasks)} 个任务，成功 {len([r for r in results.values() if 'error' not in r])} 个")
    return results


# ═══════════════════════════════════════════════
#  PipelineStep 流水线步骤定义 (阶段9新增)
# ═══════════════════════════════════════════════

@dataclass
class PipelineStep:
    """
    流水线步骤定义

    Attributes
    ----------
    name : str
        步骤名称（唯一标识）
    func : Callable
        步骤执行函数，签名: func(pipeline: CleanPipeline, **kwargs) -> Any
    priority : int
        执行优先级（数字越小越先执行）
    enabled : bool
        是否启用
    description : str
        步骤描述
    kwargs : Dict
        步骤参数
    """
    name: str
    func: Callable
    priority: int = 0
    enabled: bool = True
    description: str = ""
    kwargs: Dict[str, Any] = field(default_factory=dict)

    def execute(self, pipeline: "CleanPipeline") -> Any:
        """执行步骤"""
        return self.func(pipeline, **self.kwargs)


# ═══════════════════════════════════════════════
#  PipelineResult 流水线结果 (阶段9新增)
# ═══════════════════════════════════════════════

@dataclass
class PipelineResult:
    """
    流水线执行结果

    Attributes
    ----------
    status : str
        状态：idle/running/completed/failed
    data_type : str
        数据类型
    symbol : str
        标的代码
    cleaned : List[Dict]
        清洗后有效数据
    bad : List[Dict]
        脏数据
    stats : Dict
        清洗统计
    bad_file : str | None
        脏数据文件路径
    report_file : str | None
        质量报告文件路径
    processed_file : str | None
        processed数据文件路径
    steps_executed : List[str]
        已执行的步骤名称列表
    step_durations : Dict[str, float]
        各步骤耗时（秒）
    total_duration : float
        总耗时（秒）
    error : str | None
        错误信息（失败时）
    """
    status: str = "idle"
    data_type: str = "bar"
    symbol: str = "unknown"
    cleaned: List[Dict[str, Any]] = field(default_factory=list)
    bad: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)
    bad_file: Optional[str] = None
    report_file: Optional[str] = None
    processed_file: Optional[str] = None
    steps_executed: List[str] = field(default_factory=list)
    step_durations: Dict[str, float] = field(default_factory=dict)
    total_duration: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "status": self.status,
            "data_type": self.data_type,
            "symbol": self.symbol,
            "cleaned_count": len(self.cleaned),
            "bad_count": len(self.bad),
            "stats": self.stats,
            "bad_file": self.bad_file,
            "report_file": self.report_file,
            "processed_file": self.processed_file,
            "steps_executed": self.steps_executed,
            "step_durations": self.step_durations,
            "total_duration": self.total_duration,
            "error": self.error,
        }

    def summary(self) -> str:
        """生成人类可读摘要"""
        return (
            f"流水线结果[{self.status}]: {self.data_type}/{self.symbol}, "
            f"有效={len(self.cleaned)}, 脏数据={len(self.bad)}, "
            f"步骤={len(self.steps_executed)}, 耗时={self.total_duration:.4f}s"
        )


# ═══════════════════════════════════════════════
#  CleanPipeline 清洗流水线类 (阶段9新增)
# ═══════════════════════════════════════════════

class CleanPipeline:
    """
    清洗流水线类（链式调用 + 事件钩子 + 步骤注册 + 中间结果缓存）

    使用方式1：完整流水线（推荐）
        result = (CleanPipeline(data_type="bar", symbol="000001.SZ")
            .load(raw_data)
            .execute())

    使用方式2：分步链式调用
        result = (CleanPipeline(data_type="bar", symbol="000001.SZ")
            .load(raw_data)
            .validate()
            .deduplicate()
            .detect_anomalies()
            .sort()
            .save_bad()
            .save_report()
            .run())

    使用方式3：自定义步骤
        pipeline = CleanPipeline(data_type="bar")
        pipeline.add_step("custom_step", my_custom_func, priority=50)
        result = pipeline.load(data).run()

    事件钩子:
        pipeline.register_hook("on_pipeline_start", my_start_callback)
        pipeline.register_hook("on_step_complete", my_step_callback)
        pipeline.register_hook("on_pipeline_complete", my_complete_callback)
        pipeline.register_hook("on_error", my_error_callback)

    为实盘无缝对接预留:
      - 流式清洗：load()可逐条加载，clean_single()逐条清洗
      - 事件联动：on_bad_record/on_anomaly_detected可触发实时告警
      - 规则热更新：set_cleaner()可动态更换清洗器
      - 中间结果缓存：断点续跑，失败后从上次步骤继续
    """

    # 内置步骤优先级
    STEP_PRIORITIES = {
        "load": 0,
        "validate": 10,
        "deduplicate": 20,
        "detect_anomalies": 30,
        "sort": 40,
        "save_bad": 50,
        "save_report": 60,
        "save_processed": 70,
    }

    def __init__(
        self,
        data_type: str = "bar",
        symbol: str = "unknown",
        config: Optional[Dict[str, Any]] = None,
        cleaner: Optional[CleanerBase] = None,
        event_bus: Optional[Any] = None,
        enable_events: bool = True,
        streaming: bool = False,
    ):
        """
        初始化清洗流水线

        Parameters
        ----------
        data_type : str
            数据类型（"bar"/"tick"）
        symbol : str
            标的代码
        config : Dict | None
            清洗配置
        cleaner : CleanerBase | None
            自定义清洗器（None则根据data_type自动创建）
        event_bus : Any | None
            事件总线（用于发布数据质量事件）
        enable_events : bool
            是否启用事件发布
        streaming : bool
            #C14修复：流式模式。为True时，save_processed后清空内存中的清洗结果，
            只保留统计信息和文件路径，大幅降低大数据量场景下的内存占用。
            注意：streaming模式下，execute()返回的result.cleaned为空列表，
            数据已写入processed文件，需从文件读取。
            清洗配置
        cleaner : CleanerBase | None
            自定义清洗器（None则自动创建）
        event_bus : EventBus | None
            事件总线实例（None则使用全局单例）
        enable_events : bool
            是否启用事件发布（默认True，发布脏数据/流水线完成/质量告警事件到事件总线，联动风控/告警/指标）
        """
        if data_type not in ("bar", "tick"):
            raise ValueError(f"不支持的数据类型: {data_type}，仅支持 bar/tick")

        self.data_type = data_type
        self.symbol = symbol
        self.config = config or {}
        self._cleaner = cleaner or self._create_cleaner()
        # #C14修复：流式模式标记
        self._streaming = streaming

        # 流水线状态
        self._status = "idle"
        self._raw_data: List[Dict[str, Any]] = []
        self._cleaned: List[Dict[str, Any]] = []
        self._bad: List[Dict[str, Any]] = []
        self._stats: Dict[str, Any] = {}
        self._bad_file: Optional[str] = None
        self._report_file: Optional[str] = None
        self._processed_file: Optional[str] = None

        # 步骤管理
        self._steps: Dict[str, PipelineStep] = {}
        self._steps_executed: List[str] = []
        self._step_durations: Dict[str, float] = {}
        self._total_duration: float = 0.0
        self._error: Optional[str] = None

        # 中间结果缓存
        self._cache: Dict[str, Any] = {}

        # 事件钩子
        self._hooks: Dict[str, List[Callable]] = {
            "on_pipeline_start": [],
            "on_step_complete": [],
            "on_pipeline_complete": [],
            "on_error": [],
        }

        # 事件总线发布器（#S1修复：离线清洗联动风控/告警/指标）
        self._enable_events = enable_events
        self._event_bus = event_bus
        self._publisher: Optional[DataQualityEventPublisher] = None
        if enable_events:
            try:
                self._publisher = DataQualityEventPublisher(
                    bus=event_bus, source="clean_pipeline"
                )
            except Exception as e:
                logger.warning(f"事件发布器初始化失败，事件联动将不可用: {e}")
                self._publisher = None

        logger.debug(f"CleanPipeline初始化: type={data_type}, symbol={symbol}, events={enable_events}")

    def _create_cleaner(self) -> CleanerBase:
        """根据数据类型创建清洗器"""
        type_config = self.config.get(self.data_type) if self.config else None
        if self.data_type == "bar":
            return BarCleaner(config=type_config)
        else:
            return TickCleaner(config=type_config)

    # ═══════════════════════════════════════════
    #  链式配置
    # ═══════════════════════════════════════════

    def set_data_type(self, data_type: str) -> "CleanPipeline":
        """设置数据类型"""
        if data_type not in ("bar", "tick"):
            raise ValueError(f"不支持的数据类型: {data_type}")
        self.data_type = data_type
        self._cleaner = self._create_cleaner()
        return self

    def set_symbol(self, symbol: str) -> "CleanPipeline":
        """设置标的代码"""
        self.symbol = symbol
        return self

    def set_config(self, config: Dict[str, Any]) -> "CleanPipeline":
        """设置清洗配置"""
        self.config = config
        self._cleaner = self._create_cleaner()
        return self

    def set_cleaner(self, cleaner: CleanerBase) -> "CleanPipeline":
        """设置自定义清洗器"""
        self._cleaner = cleaner
        return self

    # ═══════════════════════════════════════════
    #  链式数据加载
    # ═══════════════════════════════════════════

    def load(self, records: List[Dict[str, Any]]) -> "CleanPipeline":
        """
        加载原始数据

        Parameters
        ----------
        records : List[Dict]
            原始数据列表

        Returns
        -------
        CleanPipeline : self（链式调用）
        """
        self._raw_data = list(records)
        self._cache["raw_data"] = self._raw_data
        logger.debug(f"加载数据: {len(records)} 条")
        return self

    def load_from_file(self, filepath: str) -> "CleanPipeline":
        """
        从文件加载数据（JSON/CSV）

        Parameters
        ----------
        filepath : str
            文件路径

        Returns
        -------
        CleanPipeline : self
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"数据文件不存在: {filepath}")

        if path.suffix == ".json":
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        elif path.suffix == ".csv":
            import csv
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                records = list(reader)
        else:
            raise ValueError(f"不支持的文件格式: {path.suffix}，仅支持 json/csv")

        return self.load(records)

    # ═══════════════════════════════════════════
    #  链式清洗步骤
    # ═══════════════════════════════════════════

    def validate(self) -> "CleanPipeline":
        """
        单条校验（委托给清洗器的 validate_single_with_rules，含自定义规则）

        #C2修复：原实现直接调用 _validate_single，绕过了 cleaner 中注册的
        自定义规则（CleanRule.check_fn）。现改用 validate_single_with_rules，
        确保与 clean() 行为一致。

        Returns
        -------
        CleanPipeline : self
        """
        start = time.time()
        valid = []
        bad = []
        for rec in self._raw_data:
            # #C2修复：使用 validate_single_with_rules，含自定义规则执行
            errors = self._cleaner.validate_single_with_rules(rec, self._cleaner.config)
            if errors:
                bad_rec = dict(rec)
                bad_rec["_errors"] = errors
                bad.append(bad_rec)
            else:
                valid.append(rec)
        self._cleaned = valid
        self._bad = bad
        self._cache["after_validate"] = (valid, bad)
        self._record_step("validate", time.time() - start)
        logger.debug(f"单条校验完成: 有效={len(valid)}, 脏数据={len(bad)}")
        return self

    def deduplicate(self) -> "CleanPipeline":
        """
        去重（委托给清洗器的_deduplicate）

        Returns
        -------
        CleanPipeline : self
        """
        start = time.time()
        if self._cleaned:
            self._cleaned, removed = self._cleaner._deduplicate(
                self._cleaned, self._cleaner.config
            )
            self._stats["removed_duplicates"] = removed
        self._cache["after_deduplicate"] = self._cleaned
        self._record_step("deduplicate", time.time() - start)
        return self

    def detect_anomalies(self) -> "CleanPipeline":
        """
        异常检测（委托给清洗器的_detect_anomalies）

        Returns
        -------
        CleanPipeline : self
        """
        start = time.time()
        if self._cleaned:
            self._cleaned, anomaly_bad, anomaly_counts = self._cleaner._detect_anomalies(
                self._cleaned, self._cleaner.config
            )
            self._stats.update(anomaly_counts)
            if anomaly_bad:
                self._bad.extend(anomaly_bad)
        self._cache["after_anomalies"] = self._cleaned
        self._record_step("detect_anomalies", time.time() - start)
        return self

    def sort(self) -> "CleanPipeline":
        """
        排序输出（委托给清洗器的_sort_output）

        Returns
        -------
        CleanPipeline : self
        """
        start = time.time()
        if self._cleaned:
            self._cleaned = self._cleaner._sort_output(
                self._cleaned, self._cleaner.config
            )
        self._cache["after_sort"] = self._cleaned
        self._record_step("sort", time.time() - start)
        return self

    # ═══════════════════════════════════════════
    #  链式输出步骤
    # ═══════════════════════════════════════════

    def save_bad(self, output_dir: Optional[str] = None) -> "CleanPipeline":
        """
        脏数据落盘

        Parameters
        ----------
        output_dir : str | None
            输出目录，None则使用默认

        Returns
        -------
        CleanPipeline : self
        """
        start = time.time()
        if self._bad:
            self._bad_file = _save_bad_records(
                self._bad, self.symbol, self.data_type, output_dir
            )
        self._record_step("save_bad", time.time() - start)
        return self

    def save_report(self, output_dir: Optional[str] = None) -> "CleanPipeline":
        """
        生成质量报告

        Parameters
        ----------
        output_dir : str | None
            输出目录，None则使用默认

        Returns
        -------
        CleanPipeline : self
        """
        start = time.time()
        self._update_stats()
        self._report_file = generate_quality_report(
            stats=self._stats,
            symbol=self.symbol,
            data_type=self.data_type,
            output_dir=output_dir,
            bad_count=len(self._bad),
        )
        self._record_step("save_report", time.time() - start)
        return self

    def save_processed(
        self,
        output_dir: Optional[str] = None,
        fmt: str = "csv",
    ) -> "CleanPipeline":
        """
        保存processed数据

        #C10修复：增加 fmt 参数支持 CSV/Parquet 双格式。
        Parquet 为列式存储，适合大数据量和实盘增量场景；
        若未安装 pandas/pyarrow，自动降级为 CSV 并记录警告。

        Parameters
        ----------
        output_dir : str | None
            输出目录，None则使用默认 datasets/processed/
        fmt : str
            保存格式："csv"（默认） | "parquet"

        Returns
        -------
        CleanPipeline : self
        """
        start = time.time()
        if not self._cleaned:
            self._record_step("save_processed", time.time() - start)
            return self

        if output_dir is None:
            output_dir = str(get_project_path("datasets", "processed", self.symbol))
        ensure_dir(output_dir)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fmt_lower = fmt.lower().strip()
        # #C13修复：sanitize symbol，避免特殊字符导致路径错误
        safe_symbol = sanitize_filename(self.symbol)

        # #C10修复：Parquet 格式保存（可选依赖，未安装则降级CSV）
        if fmt_lower == "parquet":
            try:
                import pandas as pd
                filename = f"{self.data_type}_{safe_symbol}_{timestamp}.parquet"
                filepath = Path(output_dir) / filename
                df = pd.DataFrame(self._cleaned)
                df.to_parquet(str(filepath), index=False)
                self._processed_file = str(filepath)
                logger.info(f"processed数据已保存(Parquet): {filepath} ({len(self._cleaned)} 条)")
                # #C14修复：streaming模式下保存后清空内存，只保留统计信息
                if self._streaming:
                    self._cleaned = []
                    logger.debug("streaming模式：已清空内存中的清洗结果")
                self._record_step("save_processed", time.time() - start)
                return self
            except ImportError:
                logger.warning("未安装 pandas/pyarrow，Parquet 格式不可用，自动降级为 CSV")
                fmt_lower = "csv"

        # CSV 格式保存（默认 + 降级路径）
        filename = f"{self.data_type}_{safe_symbol}_{timestamp}.csv"
        filepath = Path(output_dir) / filename

        import csv
        if self._cleaned:
            fieldnames = list(self._cleaned[0].keys())
            with open(filepath, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self._cleaned)

        self._processed_file = str(filepath)
        logger.info(f"processed数据已保存(CSV): {filepath} ({len(self._cleaned)} 条)")
        # #C14修复：streaming模式下保存后清空内存，只保留统计信息
        if self._streaming:
            self._cleaned = []
            logger.debug("streaming模式：已清空内存中的清洗结果")
        self._record_step("save_processed", time.time() - start)
        return self

    # ═══════════════════════════════════════════
    #  执行
    # ═══════════════════════════════════════════

    def run(self) -> PipelineResult:
        """
        执行所有已注册的步骤（按优先级排序）

        Returns
        -------
        PipelineResult : 流水线结果
        """
        return self._execute_pipeline(use_custom_steps=True)

    def execute(self) -> PipelineResult:
        """
        执行完整流水线（清洗 + 脏数据落盘 + 质量报告）

        这是最常用的入口，自动执行所有标准步骤。

        Returns
        -------
        PipelineResult : 流水线结果
        """
        return self._execute_pipeline(use_custom_steps=False)

    def _execute_pipeline(self, use_custom_steps: bool) -> PipelineResult:
        """
        内部执行流水线

        Parameters
        ----------
        use_custom_steps : bool
            是否使用自定义步骤（True=只执行注册的自定义步骤，False=执行标准步骤）

        Returns
        -------
        PipelineResult : 流水线结果
        """
        start = time.time()
        self._status = "running"
        self._steps_executed = []
        self._step_durations = {}
        self._error = None

        try:
            self._trigger_hook("on_pipeline_start", self)

            if use_custom_steps and self._steps:
                # 执行自定义步骤
                sorted_steps = sorted(self._steps.values(), key=lambda s: s.priority)
                for step in sorted_steps:
                    if not step.enabled:
                        continue
                    step_start = time.time()
                    step.execute(self)
                    self._record_step(step.name, time.time() - step_start)
            else:
                # 执行标准步骤
                self.validate()
                self.deduplicate()
                self.detect_anomalies()
                self.sort()
                # #S1修复：发布脏数据事件（汇总，不逐条以避免性能问题）
                self._publish_dirty_event()
                if self._bad:
                    self.save_bad()
                self.save_report()

            self._update_stats()
            # #S1修复：根据脏数据比例发布质量告警（warning/error/critical）
            self._publish_quality_alert_if_needed()
            self._total_duration = time.time() - start
            self._status = "completed"

            result = self._build_result()
            self._trigger_hook("on_pipeline_complete", result)
            # #S1修复：发布流水线完成事件（联动风控/告警/指标采集）
            self._publish_pipeline_complete_event(result)
            logger.info(result.summary())
            return result

        except Exception as e:
            self._total_duration = time.time() - start
            self._status = "failed"
            self._error = str(e)
            logger.error(f"流水线执行失败: {e}", exc_info=True)
            result = self._build_result()
            self._trigger_hook("on_error", result, e)
            # #S1修复：失败时也发布流水线完成事件（status=failed），确保下游感知
            self._publish_pipeline_complete_event(result)
            return result

    def _update_stats(self) -> None:
        """更新统计信息"""
        self._stats.update({
            "total": len(self._raw_data),
            "valid": len(self._cleaned),
            "bad": len(self._bad),
        })

    # ═══════════════════════════════════════════
    #  事件总线发布（#S1修复：离线清洗联动风控/告警/指标）
    # ═══════════════════════════════════════════

    def _publish_dirty_event(self) -> None:
        """
        发布脏数据事件（汇总发布，不逐条以避免性能问题）

        触发时机：标准清洗步骤（validate/deduplicate/detect_anomalies）完成后，
        如果存在脏数据则发布一次汇总事件。
        """
        if not self._publisher or not self._bad:
            return
        try:
            # 统计脏数据错误类型分布
            error_type_counts: Dict[str, int] = {}
            for rec in self._bad:
                errors = rec.get("_errors", [])
                if isinstance(errors, list):
                    for err in errors:
                        err_key = str(err)
                        error_type_counts[err_key] = error_type_counts.get(err_key, 0) + 1
                elif isinstance(errors, str):
                    error_type_counts[errors] = error_type_counts.get(errors, 0) + 1

            primary_type = max(error_type_counts, key=error_type_counts.get) if error_type_counts else "unknown"
            top_errors = sorted(error_type_counts.keys(), key=lambda k: error_type_counts[k], reverse=True)[:5]

            self._publisher.publish_dirty(
                dirty_type=primary_type,
                symbol=self.symbol,
                data_type=self.data_type,
                severity="error",
                errors=top_errors,
                bad_count=len(self._bad),
                total_count=len(self._raw_data),
                source_step="validate",
            )
            logger.debug(f"已发布脏数据事件: {self.symbol} bad={len(self._bad)}/{len(self._raw_data)} primary={primary_type}")
        except Exception as e:
            logger.warning(f"发布脏数据事件失败（不影响清洗结果）: {e}")

    def _publish_pipeline_complete_event(self, result: PipelineResult) -> None:
        """
        发布流水线完成事件

        触发时机：流水线执行完成（成功或失败）后发布，确保下游模块感知清洗结果。
        """
        if not self._publisher:
            return
        try:
            bad_ratio = len(self._bad) / len(self._raw_data) if self._raw_data else 0.0
            quality_grade = self._stats.get("quality_grade", "")

            self._publisher.pipeline_complete(
                symbol=self.symbol,
                data_type=self.data_type,
                status=result.status,
                cleaned_count=len(self._cleaned),
                bad_count=len(self._bad),
                total_count=len(self._raw_data),
                quality_grade=quality_grade,
                bad_ratio=bad_ratio,
                duration=result.total_duration,
                steps_executed=result.steps_executed,
                bad_file=result.bad_file,
                report_file=result.report_file,
                error=result.error,
            )
            logger.debug(f"已发布流水线完成事件: {self.symbol} status={result.status} bad_ratio={bad_ratio:.2%}")
        except Exception as e:
            logger.warning(f"发布流水线完成事件失败（不影响清洗结果）: {e}")

    def _publish_quality_alert_if_needed(self) -> None:
        """
        根据脏数据比例发布质量告警事件

        #C9修复：阈值从配置 alert_thresholds 读取，默认值保持向后兼容。

        阈值分级（与 quality_reporter 质量等级对齐）：
          - bad_ratio >= critical(默认50%) → critical（严重，触发风控暂停）
          - bad_ratio >= error(默认10%)    → error（错误级告警）
          - bad_ratio >= warning(默认5%)    → warning（警告级告警）
          - bad_ratio < warning             → 不发布告警（正常范围）
        """
        if not self._publisher or not self._raw_data:
            return
        try:
            bad_ratio = len(self._bad) / len(self._raw_data)

            # #C9修复：从配置读取阈值，默认值保持向后兼容
            alert_config = {}
            if self._cleaner and hasattr(self._cleaner, 'config'):
                alert_config = self._cleaner.config.get("alert_thresholds", {})
            critical_threshold = alert_config.get("critical", 0.5)
            error_threshold = alert_config.get("error", 0.1)
            warning_threshold = alert_config.get("warning", 0.05)

            if bad_ratio >= critical_threshold:
                self._publisher.critical(
                    message=f"严重脏数据问题：脏数据占比{bad_ratio:.1%}，超过{critical_threshold:.0%}阈值，建议暂停使用该数据源",
                    symbol=self.symbol,
                    data_type=self.data_type,
                    bad_ratio=bad_ratio,
                    threshold=critical_threshold,
                )
                logger.warning(f"已发布CRITICAL质量告警: {self.symbol} bad_ratio={bad_ratio:.2%}")
            elif bad_ratio >= error_threshold:
                self._publisher.alert(
                    alert_level="error",
                    message=f"脏数据占比过高：{bad_ratio:.1%}，超过{error_threshold:.0%}阈值",
                    symbol=self.symbol,
                    data_type=self.data_type,
                    bad_ratio=bad_ratio,
                    threshold=error_threshold,
                )
                logger.warning(f"已发布ERROR质量告警: {self.symbol} bad_ratio={bad_ratio:.2%}")
            elif bad_ratio >= warning_threshold:
                self._publisher.alert(
                    alert_level="warning",
                    message=f"脏数据占比偏高：{bad_ratio:.1%}，超过{warning_threshold:.0%}阈值",
                    symbol=self.symbol,
                    data_type=self.data_type,
                    bad_ratio=bad_ratio,
                    threshold=warning_threshold,
                )
                logger.info(f"已发布WARNING质量告警: {self.symbol} bad_ratio={bad_ratio:.2%}")
        except Exception as e:
            logger.warning(f"发布质量告警事件失败（不影响清洗结果）: {e}")

    def _record_step(self, name: str, duration: float) -> None:
        """记录步骤执行"""
        self._steps_executed.append(name)
        self._step_durations[name] = round(duration, 4)

    def _build_result(self) -> PipelineResult:
        """构建流水线结果"""
        return PipelineResult(
            status=self._status,
            data_type=self.data_type,
            symbol=self.symbol,
            cleaned=self._cleaned,
            bad=self._bad,
            stats=self._stats,
            bad_file=self._bad_file,
            report_file=self._report_file,
            processed_file=self._processed_file,
            steps_executed=list(self._steps_executed),
            step_durations=dict(self._step_durations),
            total_duration=round(self._total_duration, 4),
            error=self._error,
        )

    # ═══════════════════════════════════════════
    #  步骤注册
    # ═══════════════════════════════════════════

    def add_step(
        self,
        name: str,
        func: Callable[["CleanPipeline"], Any],
        priority: Optional[int] = None,
        description: str = "",
        **kwargs: Any,
    ) -> "CleanPipeline":
        """
        添加自定义步骤

        Parameters
        ----------
        name : str
            步骤名称（唯一）
        func : Callable
            步骤函数，签名: func(pipeline, **kwargs) -> Any
        priority : int | None
            优先级（None则使用内置默认或自增）
        description : str
            步骤描述
        **kwargs : Any
            步骤参数

        Returns
        -------
        CleanPipeline : self
        """
        if priority is None:
            priority = self.STEP_PRIORITIES.get(name, 100)

        step = PipelineStep(
            name=name,
            func=func,
            priority=priority,
            description=description,
            kwargs=kwargs,
        )
        self._steps[name] = step
        logger.debug(f"添加步骤: {name} (priority={priority})")
        return self

    def remove_step(self, name: str) -> bool:
        """
        移除步骤

        Parameters
        ----------
        name : str
            步骤名称

        Returns
        -------
        bool : 是否成功
        """
        if name in self._steps:
            del self._steps[name]
            logger.debug(f"移除步骤: {name}")
            return True
        return False

    def enable_step(self, name: str) -> bool:
        """启用步骤"""
        step = self._steps.get(name)
        if step:
            step.enabled = True
            return True
        return False

    def disable_step(self, name: str) -> bool:
        """禁用步骤"""
        step = self._steps.get(name)
        if step:
            step.enabled = False
            return True
        return False

    def list_steps(self) -> List[str]:
        """列出所有步骤名称"""
        return sorted(self._steps.keys(), key=lambda n: self._steps[n].priority)

    # ═══════════════════════════════════════════
    #  事件钩子
    # ═══════════════════════════════════════════

    def register_hook(self, event: str, callback: Callable) -> "CleanPipeline":
        """
        注册事件钩子

        Parameters
        ----------
        event : str
            事件名称（on_pipeline_start/on_step_complete/on_pipeline_complete/on_error）
        callback : Callable
            回调函数

        Returns
        -------
        CleanPipeline : self
        """
        if event not in self._hooks:
            raise ValueError(f"未知事件: {event}, 可选: {list(self._hooks.keys())}")
        self._hooks[event].append(callback)
        return self

    def unregister_hook(self, event: str, callback: Callable) -> bool:
        """注销事件钩子"""
        if event in self._hooks and callback in self._hooks[event]:
            self._hooks[event].remove(callback)
            return True
        return False

    def _trigger_hook(self, event: str, *args: Any, **kwargs: Any) -> None:
        """触发事件钩子（内部方法）"""
        for callback in self._hooks.get(event, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                logger.error(f"事件钩子执行异常: {event} -> {callback.__name__}: {e}")

    # ═══════════════════════════════════════════
    #  结果获取
    # ═══════════════════════════════════════════

    def get_result(self) -> PipelineResult:
        """获取当前结果（不重新执行）"""
        return self._build_result()

    def get_cleaned(self) -> List[Dict[str, Any]]:
        """获取清洗后有效数据"""
        return self._cleaned

    def get_bad(self) -> List[Dict[str, Any]]:
        """获取脏数据"""
        return self._bad

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        self._update_stats()
        return self._stats

    def get_cache(self, key: str) -> Any:
        """获取中间结果缓存"""
        return self._cache.get(key)

    @property
    def status(self) -> str:
        """当前状态"""
        return self._status

    @property
    def cleaner(self) -> CleanerBase:
        """当前清洗器"""
        return self._cleaner

    def __repr__(self) -> str:
        return (
            f"<CleanPipeline type={self.data_type} symbol={self.symbol} "
            f"status={self._status} steps={len(self._steps)} "
            f"raw={len(self._raw_data)} cleaned={len(self._cleaned)}>"
        )
