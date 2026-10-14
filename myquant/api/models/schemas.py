# 文件名：myquant/api/models/schemas.py
"""
schemas.py - API响应模型（Pydantic）
======================================
REST API请求/响应数据模型，基于Pydantic。

v1实现范围（基础骨架）:
  - ApiResponse 通用响应包装（code/message/data/timestamp，任务17泛型化）
  - HealthResponse 健康检查响应
  - StrategyInfo 策略信息
  - BacktestRequest 回测请求
  - BacktestResponse 回测响应
  - LiveStatusResponse 实盘状态响应
  - RiskStatusResponse 风控状态响应
  - DataQualityResponse 数据质量响应
  - ErrorResponse 错误响应
  - OrderRequest 下单请求
  - OrderResponse 订单响应

任务17新增（数据质量详细Pydantic响应schema，对齐任务16端点响应结构）:
  - QualityMetricsInfo 数据质量实时指标
  - CleanRunInfo 清洗运行信息（列表项/详情/最近运行）
  - CleanRunsData 清洗运行列表
  - GradeDistributionItem 质量等级分布项
  - DirtyStatInfo 脏数据统计明细项
  - DirtySummaryInfo 脏数据统计汇总
  - DirtyData 脏数据统计响应
  - QualityOverviewData 数据质量总览
  - AlertInfo 告警信息（对齐 Alert.to_dict）
  - AlertSummaryInfo 告警统计（对齐 AlertManager.stats）
  - AlertListData 告警列表响应
  - AlertAckData / AlertResolveData 告警确认/解决响应

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（实盘状态/订单/持仓模型全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 响应模型只包含当前已知数据，不预测未来

防未来函数说明:
  - 所有响应模型只包含当前已知的账户/持仓/行情/回测/清洗/告警数据
  - 不包含任何未来数据或预测字段
  - 回测结果响应严格按时间顺序，不泄露未来信息
  - 实盘状态响应只包含当前实时数据，不预判未来走势
  - 数据质量/告警模型只承载已发生事件的事实数据

兼容性保障（任务17，零破坏任务16的83项验证）:
  - 所有任务17新增模型 model_config = {"extra": "allow"}，FastAPI 序列化时
    保留任务16端点返回的全部字段，不丢弃额外字段
  - 字段全部 Optional 宽松类型，空值/None 不校验失败
  - ApiResponse 泛型化（Generic[T]，data: Optional[T]），不传类型参数时
    T 默认不约束（向后兼容阶段8所有 response_model=ApiResponse 用法）

TODO(后续迭代):
  - 订单请求/响应模型（下单/撤单/查单）
  - 持仓响应模型（持仓明细/盈亏）
  - 行情响应模型（实时行情/K线/Tick）
  - 策略参数配置模型
  - 风控规则配置模型
  - 分页响应模型
  - WebSocket消息模型

依赖: pydantic（可选）, typing, datetime
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar, Union

from myquant.core.logger import get_logger

logger = get_logger("api.models.schemas")

# 泛型类型变量（ApiResponse[T] 使用）
T = TypeVar("T")

# 检查pydantic是否安装
try:
    from pydantic import BaseModel, Field
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    logger.warning("[api] pydantic未安装，API响应模型将使用普通dataclass。请运行: pip install pydantic")

    # 降级：使用普通类作为BaseModel
    class BaseModel:  # type: ignore[no-redef]
        """Pydantic未安装时的降级BaseModel"""
        def __init__(self, **data: Any):
            for key, value in data.items():
                setattr(self, key, value)

        def model_dump(self) -> Dict[str, Any]:
            return self.__dict__

        def dict(self) -> Dict[str, Any]:
            return self.__dict__

    def Field(default: Any = None, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        return default


# ═══════════════════════════════════════════════
#  通用响应模型
# ═══════════════════════════════════════════════

class ApiResponse(BaseModel, Generic[T]):
    """
    通用API响应包装（任务17泛型化）

    Attributes
    ----------
    code : int
        响应码（0=成功，非0=错误）
    message : str
        响应消息
    data : T | None
        响应数据（泛型，可指定具体响应模型；不指定时默认 Any）
    timestamp : str
        响应时间戳
    """
    code: int = 0
    message: str = "success"
    data: Optional[T] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def success(cls, data: T = None, message: str = "success") -> "ApiResponse[T]":
        """成功响应"""
        return cls(code=0, message=message, data=data)

    @classmethod
    def error(cls, code: int = -1, message: str = "error", data: Any = None) -> "ApiResponse[T]":
        """错误响应"""
        return cls(code=code, message=message, data=data)


class ErrorResponse(BaseModel):
    """
    错误响应

    Attributes
    ----------
    error : str
        错误类型
    message : str
        错误消息
    detail : str
        详细信息
    timestamp : str
        时间戳
    """
    error: str = "InternalError"
    message: str = ""
    detail: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ═══════════════════════════════════════════════
#  健康检查
# ═══════════════════════════════════════════════

class HealthResponse(BaseModel):
    """
    健康检查响应

    Attributes
    ----------
    status : str
        系统状态（healthy/degraded/unhealthy）
    version : str
        系统版本
    modules : Dict[str, str]
        各模块状态
    timestamp : str
        时间戳
    """
    status: str = "healthy"
    version: str = "v1.0.0"
    modules: Dict[str, str] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ═══════════════════════════════════════════════
#  策略
# ═══════════════════════════════════════════════

class StrategyInfo(BaseModel):
    """
    策略信息

    Attributes
    ----------
    name : str
        策略名称
    class_name : str
        策略类名
    description : str
        策略描述
    params : Dict[str, Any]
        策略参数
    registered : bool
        是否已注册
    """
    name: str = ""
    class_name: str = ""
    description: str = ""
    params: Dict[str, Any] = Field(default_factory=dict)
    registered: bool = False


class StrategyListResponse(BaseModel):
    """策略列表响应"""
    strategies: List[StrategyInfo] = Field(default_factory=list)
    total: int = 0


# ═══════════════════════════════════════════════
#  回测
# ═══════════════════════════════════════════════

class BacktestRequest(BaseModel):
    """
    回测请求

    Attributes
    ----------
    strategy_name : str
        策略名称
    symbol : str
        标的代码
    start_date : str
        开始日期（YYYY-MM-DD）
    end_date : str
        结束日期（YYYY-MM-DD）
    initial_capital : float
        初始资金
    params : Dict[str, Any]
        策略参数
    """
    strategy_name: str = ""
    symbol: str = ""
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 1_000_000.0  # P3-D8-004 整改：对齐回测/实盘默认初始资金 100 万
    params: Dict[str, Any] = Field(default_factory=dict)


class BacktestMetrics(BaseModel):
    """回测绩效指标"""
    total_return: float = 0.0
    annual_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    calmar_ratio: float = 0.0
    sortino_ratio: float = 0.0


class BacktestResponse(BaseModel):
    """
    回测响应

    Attributes
    ----------
    backtest_id : str
        回测ID
    status : str
        状态（running/completed/failed）
    metrics : BacktestMetrics
        绩效指标
    equity_curve : List[Dict[str, Any]]
        净值曲线（严格按时间顺序，防未来函数）
    trades : List[Dict[str, Any]]
        交易明细
    message : str
        消息
    """
    backtest_id: str = ""
    status: str = "completed"
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)
    equity_curve: List[Dict[str, Any]] = Field(default_factory=list)
    trades: List[Dict[str, Any]] = Field(default_factory=list)
    message: str = ""


# ═══════════════════════════════════════════════
#  实盘
# ═══════════════════════════════════════════════

class LiveStatusResponse(BaseModel):
    """
    实盘状态响应

    Attributes
    ----------
    connected : bool
        是否已连接券商
    running : bool
        实盘是否运行中
    broker : str
        券商名称
    total_assets : float
        总资产
    available_cash : float
        可用资金
    position_value : float
        持仓市值
    today_pnl : float
        今日盈亏
    positions : List[Dict[str, Any]]
        持仓列表（当前已知数据，不包含未来）
    timestamp : str
        时间戳
    """
    connected: bool = False
    running: bool = False
    broker: str = ""
    total_assets: float = 0.0
    available_cash: float = 0.0
    position_value: float = 0.0
    today_pnl: float = 0.0
    positions: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ═══════════════════════════════════════════════
#  风控
# ═══════════════════════════════════════════════

class RiskStatusResponse(BaseModel):
    """
    风控状态响应

    Attributes
    ----------
    status : str
        风控状态（NORMAL/WARNING/PAUSED）
    health_status : str
        健康状态（HEALTHY/DEGRADED/UNHEALTHY）
    status_reason : str
        状态原因
    enabled_rules : List[str]
        已启用的规则列表
    today_rejections : int
        今日拦截次数
    recent_events : List[Dict[str, Any]]
        最近风控事件
    timestamp : str
        时间戳
    """
    status: str = "NORMAL"
    health_status: str = "HEALTHY"
    status_reason: str = ""
    enabled_rules: List[str] = Field(default_factory=list)
    today_rejections: int = 0
    recent_events: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ═══════════════════════════════════════════════
#  数据质量（TODO占位，后续迭代增强）
# ═══════════════════════════════════════════════

class DataQualityResponse(BaseModel):
    """
    数据质量响应

    ⚠️ v1基础骨架，后续迭代增强详细数据质量schema。

    Attributes
    ----------
    symbol : str
        标的代码
    data_type : str
        数据类型（bar/tick）
    total_records : int
        总记录数
    valid_records : int
        有效记录数
    bad_records : int
        脏数据记录数
    quality_rate : float
        合格率（0~1）
    status : str
        质量状态（good/warning/poor）
    bad_types : Dict[str, int]
        脏数据类型统计
    timestamp : str
        时间戳
    """
    symbol: str = ""
    data_type: str = "bar"
    total_records: int = 0
    valid_records: int = 0
    bad_records: int = 0
    quality_rate: float = 0.0
    status: str = "good"
    bad_types: Dict[str, int] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ═══════════════════════════════════════════════
#  订单（TODO占位，后续迭代增强）
# ═══════════════════════════════════════════════

class OrderRequest(BaseModel):
    """
    下单请求（TODO占位，后续迭代增强）

    Attributes
    ----------
    symbol : str
        标的代码
    side : str
        买卖方向（buy/sell）
    order_type : str
        订单类型（market/limit）
    quantity : float
        数量
    price : float
        价格（限价单必填）
    """
    symbol: str = ""
    side: str = "buy"
    order_type: str = "market"
    quantity: float = 0.0
    price: float = 0.0


class OrderResponse(BaseModel):
    """
    订单响应（TODO占位，后续迭代增强）
    """
    order_id: str = ""
    status: str = "submitted"
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    price: float = 0.0
    filled_quantity: float = 0.0
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# ═══════════════════════════════════════════════
#  数据质量详细响应（任务17新增，对齐任务16端点响应结构）
# ═══════════════════════════════════════════════
#
# 兼容性保障（零破坏任务16的83项验证）:
#   - 所有模型 model_config = {"extra": "allow"}：FastAPI 用 response_model
#     序列化时不丢弃任务16端点返回的任何额外字段
#   - 字段全部 Optional 宽松类型：空值/None 不触发校验失败
#   - metrics.quality_grade 支持数值（collector 中为 QUALITY_GRADE_MAP 数值）
#     与字符串双形态
# 防未来函数:
#   - 所有字段只承载【已发生】的清洗运行/脏数据/告警事实
#   - 无任何预测/未来字段
# 为实盘对接:
#   - source 区分 offline/live；告警 summary 承载实盘告警统计
#   - 模型字段与任务13 clean_run_meta / 任务10 Alert 数据类逐字段对齐
# ═══════════════════════════════════════════════


class QualityMetricsInfo(BaseModel):
    """
    数据质量实时指标（任务16 /api/data/quality/metrics 及 overview.metrics）

    Attributes
    ----------
    total_records : int | float | None
        总记录数
    dirty_count : int | float | None
        脏数据条数
    clean_count : int | float | None
        干净数据条数
    dirty_ratio : float | None
        脏数据占比（0~1）
    quality_grade : int | float | str | None
        质量等级（collector 中为 QUALITY_GRADE_MAP 数值，兼容字符串）
    pipeline_runs : int | float | None
        流水线运行次数
    pipeline_failures : int | float | None
        流水线失败次数
    repair_count : int | float | None
        修复条数
    """
    model_config = {"extra": "allow"}
    total_records: Optional[Union[int, float]] = None
    dirty_count: Optional[Union[int, float]] = None
    clean_count: Optional[Union[int, float]] = None
    dirty_ratio: Optional[float] = None
    quality_grade: Optional[Union[int, float, str]] = None
    pipeline_runs: Optional[Union[int, float]] = None
    pipeline_failures: Optional[Union[int, float]] = None
    repair_count: Optional[Union[int, float]] = None


class CleanRunInfo(BaseModel):
    """
    清洗运行信息（任务16 runs[] / runs/{run_id} / overview.latest_run）

    字段与任务13 clean_run_meta 表列逐字段对齐，steps/耗时自动反序列化。
    """
    model_config = {"extra": "allow"}
    run_id: Optional[str] = None
    data_type: Optional[str] = None
    symbol: Optional[str] = None
    status: Optional[str] = None
    total: Optional[int] = None
    valid: Optional[int] = None
    bad: Optional[int] = None
    valid_rate: Optional[float] = None
    quality_grade: Optional[str] = None
    severity: Optional[str] = None
    steps_executed: Optional[List[str]] = None
    step_durations: Optional[Dict[str, float]] = None
    total_duration: Optional[float] = None
    bad_file: Optional[str] = None
    report_file: Optional[str] = None
    processed_file: Optional[str] = None
    source: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None


class CleanRunsData(BaseModel):
    """
    清洗运行列表响应 data（任务16 /api/data/quality/runs）

    Attributes
    ----------
    total : int
        符合条件的运行总数
    limit : int
        本次返回条数上限
    runs : List[CleanRunInfo]
        运行列表（按 started_at 倒序）
    """
    model_config = {"extra": "allow"}
    total: int = 0
    limit: int = 20
    runs: List[CleanRunInfo] = Field(default_factory=list)


class GradeDistributionItem(BaseModel):
    """
    质量等级分布项（任务16 overview.grade_distribution[]）

    Attributes
    ----------
    grade : str | None
        质量等级（excellent/good/fair/poor/critical/unknown）
    grade_desc : str | None
        等级中文描述
    count : int
        该等级运行次数
    ratio : float
        占比（0~1）
    """
    model_config = {"extra": "allow"}
    grade: Optional[str] = None
    grade_desc: Optional[str] = None
    count: int = 0
    ratio: float = 0.0


class DirtyStatInfo(BaseModel):
    """
    脏数据统计明细项（任务16 dirty.details[]）

    字段与任务13 dirty_data_stats 表列 + dirty_type_desc 中文描述对齐。
    """
    model_config = {"extra": "allow"}
    run_id: Optional[str] = None
    symbol: Optional[str] = None
    data_type: Optional[str] = None
    dirty_type: Optional[str] = None
    severity: Optional[str] = None
    bad_count: int = 0
    repair_strategy: Optional[str] = None
    sample_data: Optional[Any] = None
    dirty_type_desc: Optional[str] = None


class DirtySummaryInfo(BaseModel):
    """
    脏数据统计汇总（任务16 overview.dirty_summary）

    Attributes
    ----------
    total_bad : int
        脏数据总条数
    total_types : int
        脏数据类型数
    error_count : int
        error 级条数
    warning_count : int
        warning 级条数
    by_type : Dict[str, int]
        按脏数据类型分布
    by_severity : Dict[str, int]
        按严重级别分布
    top_dirty_types : List[str]
        高发类型 TOP（按条数降序）
    by_type_desc : Dict[str, Any]
        类型中文描述映射（{dirty_type: {bad_count, desc}}）
    """
    model_config = {"extra": "allow"}
    total_bad: int = 0
    total_types: int = 0
    error_count: int = 0
    warning_count: int = 0
    by_type: Dict[str, int] = Field(default_factory=dict)
    by_severity: Dict[str, int] = Field(default_factory=dict)
    top_dirty_types: List[str] = Field(default_factory=list)
    by_type_desc: Dict[str, Any] = Field(default_factory=dict)


class DirtyData(BaseModel):
    """
    脏数据统计响应 data（任务16 /api/data/quality/dirty）

    汇总字段 + 可选明细 details[]（include_detail 控制）。
    """
    model_config = {"extra": "allow"}
    total_bad: int = 0
    total_types: int = 0
    error_count: int = 0
    warning_count: int = 0
    by_type: Dict[str, int] = Field(default_factory=dict)
    by_severity: Dict[str, int] = Field(default_factory=dict)
    top_dirty_types: List[str] = Field(default_factory=list)
    by_type_desc: Dict[str, Any] = Field(default_factory=dict)
    details: Optional[List[DirtyStatInfo]] = None


class QualityOverviewData(BaseModel):
    """
    数据质量总览响应 data（任务16 /api/data/quality/overview）

    Attributes
    ----------
    metrics : QualityMetricsInfo | None
        实时质量指标
    total_runs : int
        清洗运行总次数
    latest_run : CleanRunInfo | None
        最近一次清洗运行（无则 None）
    grade_distribution : List[GradeDistributionItem]
        质量等级分布（最近 N 次）
    dirty_summary : DirtySummaryInfo | None
        脏数据统计汇总
    """
    model_config = {"extra": "allow"}
    metrics: Optional[QualityMetricsInfo] = None
    total_runs: int = 0
    latest_run: Optional[CleanRunInfo] = None
    grade_distribution: List[GradeDistributionItem] = Field(default_factory=list)
    dirty_summary: Optional[DirtySummaryInfo] = None


class AlertInfo(BaseModel):
    """
    告警信息（任务16 alerts[]，字段对齐任务10 Alert.to_dict）

    Attributes
    ----------
    alert_id : str | None
        告警唯一ID
    level : str | None
        级别（info/warning/error/critical）
    message : str | None
        告警消息
    source : str | None
        来源
    category : str | None
        分类
    details : Dict | None
        详情
    deduplication_key : str | None
        去重键
    timestamp : float | None
        告警时间戳（秒）
    acknowledged : bool
        是否已确认
    resolved : bool
        是否已解决
    escalated_from : str | None
        升级前的级别
    parent_alert_id : str | None
        父告警ID
    aggregation_count : int
        聚合的原始事件数量
    """
    model_config = {"extra": "allow"}
    alert_id: Optional[str] = None
    level: Optional[str] = None
    message: Optional[str] = None
    source: Optional[str] = None
    category: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    deduplication_key: Optional[str] = None
    timestamp: Optional[float] = None
    acknowledged: bool = False
    resolved: bool = False
    escalated_from: Optional[str] = None
    parent_alert_id: Optional[str] = None
    aggregation_count: int = 1


class AlertSummaryInfo(BaseModel):
    """
    告警统计（任务16 alerts.summary，字段对齐任务10 AlertManager.stats property）

    承载告警管理器实时统计（历史量/级别分布/未确认数/升级数/静默状态等）。
    """
    model_config = {"extra": "allow"}
    channels: Optional[Dict[str, Any]] = None
    channels_count: int = 0
    history_size: int = 0
    level_counts: Dict[str, int] = Field(default_factory=dict)
    category_counts: Dict[str, int] = Field(default_factory=dict)
    unacknowledged_count: int = 0
    escalated_count: int = 0
    silenced: bool = False
    event_bus_subscribed: bool = False
    dedup_cache_size: int = 0
    aggregation_pending: int = 0
    aggregation_window: Optional[float] = None
    escalation_enabled: bool = False
    escalation_time: Optional[float] = None
    callbacks_count: int = 0


class AlertListData(BaseModel):
    """
    告警列表响应 data（任务16 /api/alerts）

    Attributes
    ----------
    scope : str
        查询范围（all/unacknowledged/escalated）
    count : int
        本次返回告警条数
    alerts : List[AlertInfo]
        告警列表
    summary : AlertSummaryInfo | None
        告警统计
    """
    model_config = {"extra": "allow"}
    scope: str = "all"
    count: int = 0
    alerts: List[AlertInfo] = Field(default_factory=list)
    summary: Optional[AlertSummaryInfo] = None


class AlertAckData(BaseModel):
    """
    告警确认响应 data（任务16 /api/alerts/{alert_id}/ack）
    """
    model_config = {"extra": "allow"}
    alert_id: Optional[str] = None
    acknowledged: bool = False


class AlertResolveData(BaseModel):
    """
    告警解决响应 data（任务16 /api/alerts/{alert_id}/resolve）
    """
    model_config = {"extra": "allow"}
    alert_id: Optional[str] = None
    resolved: bool = False
