# 文件名：myquant/core/exceptions.py
"""
exceptions.py - 自定义异常体系
===============================
统一异常层次结构，便于上层统一捕获与处理。

异常树:
  QuantBaseError
  ├── ConfigError
  ├── DataError
  │   ├── DataNotFoundError
  │   ├── DataFormatError
  │   ├── DataConnectionError
  │   └── DataQualityError          (阶段9新增：脏数据异常)
  │       ├── DataQualityCriticalError  (严重脏数据，阻断流程)
  │       └── DataQualityWarning        (脏数据警告，不阻断)
  │   └── CleaningPipelineError     (阶段9新增：清洗流水线异常)
  │   └── DataRepairError           (阶段9新增：数据修复异常)
  ├── StrategyError
  │   ├── SignalError
  │   └── ExecutionError
  ├── RiskViolationError
  │   ├── PositionLimitError
  │   └── DrawdownLimitError
  ├── BacktestError
  ├── LookaheadError                (防未来函数异常)
  ├── TransportError
  │   ├── ConnectionError
  │   └── MessageError
  ├── CacheError
  └── EventError
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class QuantBaseError(Exception):
    """量化系统基础异常"""

    def __init__(
        self,
        message: str = "",
        code: int = -1,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None,
    ):
        self.message = message
        self.code = code
        self.details = details or {}
        self.cause = cause
        super().__init__(self.message)

    def __str__(self) -> str:
        parts = [f"[{self.__class__.__name__}] {self.message}"]
        if self.code != -1:
            parts.append(f"(code={self.code})")
        if self.details:
            parts.append(f" details={self.details}")
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "code": self.code,
            "details": self.details,
        }


# ── 配置异常 ──
class ConfigError(QuantBaseError):
    """配置加载/解析/校验错误"""
    pass


class ConfigNotFoundError(ConfigError):
    """配置文件不存在"""
    pass


class ConfigValidationError(ConfigError):
    """配置校验失败"""
    pass


# ── 数据异常 ──
class DataError(QuantBaseError):
    """数据层基础异常"""
    pass


class DataNotFoundError(DataError):
    """数据未找到"""
    def __init__(self, symbol: str = "", period: str = "", **kwargs):
        msg = f"数据未找到"
        if symbol:
            msg += f" symbol={symbol}"
        if period:
            msg += f" period={period}"
        super().__init__(message=msg, code=1001, **kwargs)
        self.symbol = symbol
        self.period = period


class DataFormatError(DataError):
    """数据格式错误"""
    pass


class DataConnectionError(DataError):
    """数据源连接失败"""
    pass


# ── 数据清洗异常 (阶段9新增) ──
class DataQualityError(DataError):
    """
    脏数据异常（数据清洗基础异常）

    当清洗过程中发现脏数据时抛出，包含脏数据类型、原始记录、错误列表等信息。
    可用于实盘实时告警、风控暂停、数据质量统计。

    Attributes
    ----------
    dirty_type : DirtyDataType | None
        脏数据类型枚举
    record : Dict | None
        脏数据原始记录
    errors : List[str]
        错误原因列表
    symbol : str
        标的代码
    data_type : str
        数据类型（bar/tick）
    quality_grade : QualityGrade | None
        数据质量等级
    """

    def __init__(
        self,
        message: str = "数据质量异常",
        dirty_type: Optional[Any] = None,
        record: Optional[Dict[str, Any]] = None,
        errors: Optional[List[str]] = None,
        symbol: str = "",
        data_type: str = "",
        quality_grade: Optional[Any] = None,
        code: int = 1101,
        **kwargs,
    ):
        self.dirty_type = dirty_type
        self.record = record
        self.errors = errors or []
        self.symbol = symbol
        self.data_type = data_type
        self.quality_grade = quality_grade

        # 构建详细消息
        parts = [message]
        if dirty_type is not None:
            parts.append(f"[类型={dirty_type.value if hasattr(dirty_type, 'value') else dirty_type}]")
        if symbol:
            parts.append(f"[标的={symbol}]")
        if data_type:
            parts.append(f"[数据类型={data_type}]")
        if self.errors:
            parts.append(f"[错误={'; '.join(self.errors[:3])}]")

        # 合并details（子类可能已传入）
        details = kwargs.pop("details", {}) or {}
        details.update({
            "dirty_type": dirty_type.value if hasattr(dirty_type, "value") else dirty_type,
            "symbol": symbol,
            "data_type": data_type,
            "errors": self.errors,
            "quality_grade": quality_grade.value if hasattr(quality_grade, "value") else quality_grade,
        })

        super().__init__(
            message=" ".join(parts),
            code=code,
            details=details,
            **kwargs,
        )

    @property
    def is_critical(self) -> bool:
        """是否为严重脏数据（ERROR级）"""
        if self.dirty_type is not None and hasattr(self.dirty_type, "is_critical"):
            return self.dirty_type.is_critical
        return True

    @property
    def severity(self) -> str:
        """严重级别：error / warning"""
        if self.dirty_type is not None and hasattr(self.dirty_type, "severity"):
            return self.dirty_type.severity
        return "error"

    def summary(self) -> str:
        """生成人类可读摘要"""
        return (
            f"DataQualityError[{self.severity}]: "
            f"symbol={self.symbol or 'N/A'}, "
            f"type={self.data_type or 'N/A'}, "
            f"dirty={self.dirty_type.value if self.dirty_type else 'unknown'}, "
            f"errors={len(self.errors)}"
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（扩展基类方法）"""
        d = super().to_dict()
        d["record"] = self.record
        d["is_critical"] = self.is_critical
        d["severity"] = self.severity
        return d


class DataQualityCriticalError(DataQualityError):
    """
    严重脏数据异常（阻断流程）

    当脏数据比例超过阈值，或发现CRITICAL级脏数据时抛出。
    实盘场景下应触发：暂停策略、告警通知、人工审核。

    典型触发场景:
      - 单批数据脏数据比例 > 50%
      - 发现OHLC逻辑错误且无法修复
      - 数据源连续返回无效数据
      - 数据质量等级为 CRITICAL
    """

    def __init__(
        self,
        message: str = "严重数据质量异常，流程已阻断",
        bad_ratio: float = 0.0,
        threshold: float = 0.5,
        **kwargs,
    ):
        self.bad_ratio = bad_ratio
        self.threshold = threshold
        if "details" not in kwargs:
            kwargs["details"] = {}
        kwargs["details"].update({
            "bad_ratio": bad_ratio,
            "threshold": threshold,
        })
        super().__init__(message=message, code=1102, **kwargs)

    @property
    def should_halt(self) -> bool:
        """是否应停止流程（脏数据比例超阈值）"""
        return self.bad_ratio >= self.threshold


class DataQualityWarning(DataQualityError):
    """
    脏数据警告（不阻断流程）

    WARNING级脏数据（价格跳变、成交量异常、零成交量等），
    不阻断清洗/回测/实盘流程，但记录告警供后续分析。

    典型触发场景:
      - 价格跳变超过阈值（可能是除权除息/重大消息）
      - 成交量异常放大（可能是龙虎榜/大宗交易）
      - 零成交量（可能是停牌）
    """

    def __init__(
        self,
        message: str = "数据质量警告",
        **kwargs,
    ):
        super().__init__(message=message, code=1103, **kwargs)

    @property
    def is_critical(self) -> bool:
        """警告级不是严重错误"""
        return False

    @property
    def severity(self) -> str:
        return "warning"


class CleaningPipelineError(DataError):
    """
    清洗流水线异常

    当CleanPipeline执行过程中某个步骤失败时抛出，
    包含失败步骤名称、步骤索引、已执行步骤列表。

    Attributes
    ----------
    failed_step : str
        失败的步骤名称
    step_index : int
        失败步骤的索引
    executed_steps : List[str]
        已成功执行的步骤列表
    pipeline_result : PipelineResult | None
        流水线部分结果
    """

    def __init__(
        self,
        message: str = "清洗流水线执行失败",
        failed_step: str = "",
        step_index: int = -1,
        executed_steps: Optional[List[str]] = None,
        pipeline_result: Optional[Any] = None,
        **kwargs,
    ):
        self.failed_step = failed_step
        self.step_index = step_index
        self.executed_steps = executed_steps or []
        self.pipeline_result = pipeline_result

        parts = [message]
        if failed_step:
            parts.append(f"[失败步骤={failed_step}]")
        if step_index >= 0:
            parts.append(f"[步骤索引={step_index}]")
        if self.executed_steps:
            parts.append(f"[已执行={len(self.executed_steps)}步]")

        super().__init__(
            message=" ".join(parts),
            code=1110,
            details={
                "failed_step": failed_step,
                "step_index": step_index,
                "executed_steps": self.executed_steps,
            },
            **kwargs,
        )

    @property
    def can_resume(self) -> bool:
        """是否可从失败步骤恢复（有中间结果缓存）"""
        return self.pipeline_result is not None and len(self.executed_steps) > 0


class DataRepairError(DataError):
    """
    数据修复异常

    当尝试修复脏数据（CORRECT策略）失败时抛出。

    Attributes
    ----------
    repair_strategy : RepairStrategy | None
        尝试的修复策略
    original_error : str
        原始脏数据错误
    repair_attempt : str
        修复尝试描述
    """

    def __init__(
        self,
        message: str = "数据修复失败",
        repair_strategy: Optional[Any] = None,
        original_error: str = "",
        repair_attempt: str = "",
        **kwargs,
    ):
        self.repair_strategy = repair_strategy
        self.original_error = original_error
        self.repair_attempt = repair_attempt

        parts = [message]
        if repair_strategy is not None:
            parts.append(f"[策略={repair_strategy.value if hasattr(repair_strategy, 'value') else repair_strategy}]")
        if original_error:
            parts.append(f"[原始错误={original_error}]")

        super().__init__(
            message=" ".join(parts),
            code=1120,
            details={
                "repair_strategy": repair_strategy.value if hasattr(repair_strategy, "value") else repair_strategy,
                "original_error": original_error,
                "repair_attempt": repair_attempt,
            },
            **kwargs,
        )


# ── 策略异常 ──
class StrategyError(QuantBaseError):
    """策略执行异常"""
    def __init__(self, strategy_name: str = "", **kwargs):
        self.strategy_name = strategy_name
        if strategy_name and "message" not in kwargs:
            kwargs["message"] = f"策略 '{strategy_name}' 执行异常"
        super().__init__(code=2001, **kwargs)


class SignalError(StrategyError):
    """信号生成异常"""
    pass


class ExecutionError(StrategyError):
    """订单执行异常"""
    pass


# ── 风控异常 ──
class RiskViolationError(QuantBaseError):
    """风控违规"""
    def __init__(self, rule_name: str = "", **kwargs):
        self.rule_name = rule_name
        if rule_name and "message" not in kwargs:
            kwargs["message"] = f"风控规则触发: {rule_name}"
        super().__init__(code=3001, **kwargs)


class PositionLimitError(RiskViolationError):
    """持仓超限"""
    pass


class DrawdownLimitError(RiskViolationError):
    """回撤超限"""
    pass


class OrderFrequencyError(RiskViolationError):
    """下单频率超限"""
    pass


# ── 回测异常 ──
class BacktestError(QuantBaseError):
    """回测引擎异常"""
    pass


class InsufficientDataError(BacktestError):
    """回测数据不足"""
    pass


# ── 防未来函数异常（规则4核心）──
class LookaheadError(QuantBaseError):
    """
    防未来函数校验失败（规则4：严格禁用未来函数）

    触发场景:
      - 指标计算使用了当日及未来数据
      - 移动止损先更新后检查（顺序错误）
      - 信号在T日收盘前生成并成交
      - 股票池筛选使用了信号日之后的数据
    """
    def __init__(
        self,
        rule_name: str = "",
        details: str = "",
        symbol: str = "",
        signal_date: str = "",
        **kwargs,
    ):
        self.rule_name = rule_name
        self.symbol = symbol
        self.signal_date = signal_date
        msg = "防未来函数校验失败"
        if rule_name:
            msg += f": {rule_name}"
        if symbol:
            msg += f" [股票={symbol}]"
        if signal_date:
            msg += f" [信号日={signal_date}]"
        if details:
            msg += f" ({details})"
        super().__init__(message=msg, code=8001, **kwargs)


# ── 传输异常 ──
class TransportError(QuantBaseError):
    """消息传输异常"""
    pass


class TransportConnectionError(TransportError):
    """传输层连接失败"""
    pass


class MessageSerializeError(TransportError):
    """消息序列化失败"""
    pass


# ── 缓存异常 ──
class CacheError(QuantBaseError):
    """缓存操作异常"""
    pass


class CacheExpiredError(CacheError):
    """缓存已过期"""
    pass


# ── 事件异常 ──
class EventError(QuantBaseError):
    """事件总线异常"""
    pass


class EventHandlerError(EventError):
    """事件处理器执行异常"""
    pass


# ── 工具函数 ──
def wrap_exception(
    func,
    *args,
    exception_class=QuantBaseError,
    **kwargs,
) -> Any:
    """
    安全执行包装器
    将任意异常转换为指定量化异常类型
    """
    try:
        return func(*args, **kwargs)
    except QuantBaseError:
        raise
    except Exception as e:
        raise exception_class(
            message=str(e),
            cause=e,
        ) from e