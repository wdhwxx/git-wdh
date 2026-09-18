# 文件名：myquant/risk/risk_manager.py
"""
risk_manager.py - 风控管理器（阶段9任务12增强：对接DataQualityEvent）
======================================================================
风控核心管理器，负责订单前置检查、持仓监控、风控状态管理、事件记录、数据质量风控联动。

阶段9任务12增强内容:
  1. DataQualityRiskConfig — 数据质量风控阈值配置
  2. SymbolDataQualityStatus — 按标的跟踪数据质量状态（支持单标的暂停）
  3. subscribe_to_event_bus() — 订阅CRITICAL/ALERT/PIPELINE_COMPLETE三类事件
  4. handle_quality_alert_event() — critical全局暂停，error+高占比单标的暂停
  5. handle_pipeline_complete_event() — 按质量等级分级处理
  6. check_order() 增强 — 单标的数据质量暂停时拒绝该标的买入
  7. resume() 增强 — 恢复时清除数据质量暂停标记

核心能力:
  1. 规则库管理 — 管理10类风控规则，支持添加/删除/开关
  2. 订单前置检查 — 订单提交到broker之前统一检查，拒绝违规订单
  3. 持仓监控 — 止损/止盈/回撤检查，发出警告（v1不自动平仓）
  4. 风控状态管理 — NORMAL/WARNING/PAUSED三级状态
  5. 风控事件记录 — 所有拒绝/警告/状态变更记录审计
  6. 数据质量风控 — 脏数据严重时自动暂停（全局/单标的）
  7. 与实盘引擎集成 — engine可直接调用check_order，零修改接入
  8. 配置驱动 — 从dict/yaml加载风控配置

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（事件联动/自动平仓/动态阈值全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前已知的账户/持仓/行情/事件数据

防未来函数说明:
  - 订单检查只使用当前已知的账户/持仓/行情数据
  - 持仓监控只使用当前市场价，不预判未来价格
  - 回撤计算基于已实现的历史净值，不包含未来数据
  - 风控状态变更只基于已发生的事件，不预判未来风险
  - 数据质量风控只使用事件已携带的当前脏数据统计，不预判未来数据质量

数据质量风控事件链路:
  清洗流水线 → DataQualityEvent(高频原始) → AlertAggregator(任务10聚合)
    → QualityAlertEvent(低频告警) → RiskManager(任务12消费)
    → 全局暂停/单标的暂停/风控警告

  清洗流水线 → PipelineCompleteEvent → RiskManager(任务12消费)
    → 按质量等级(critical/poor→暂停，fair→警告)

依赖: typing, dataclasses, time, enum, myquant.risk.rules, myquant.models.trade,
      myquant.core.logger, myquant.core.event_bus
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.core.event_bus import (
    EventBus,
    EventType,
    DataQualityEvent,
    QualityAlertEvent,
    PipelineCompleteEvent,
    StrategyPauseEvent,
)
from myquant.models.trade import Order, OrderSide
from myquant.risk.rules import (
    RiskCheckResult,
    RiskContext,
    RiskRule,
    RiskRuleLibrary,
    create_default_library,
)

logger = get_logger("risk.manager")


# ═══════════════════════════════════════════════
#  风控状态枚举
# ═══════════════════════════════════════════════

class RiskStatus(Enum):
    """风控状态"""
    NORMAL = "NORMAL"      # 正常：所有规则通过，可正常交易
    WARNING = "WARNING"    # 警告：部分规则触发，可交易但需关注
    PAUSED = "PAUSED"      # 暂停：严重风控事件，禁止新开仓（只允许平仓）


# ═══════════════════════════════════════════════
#  风控事件类型
# ═══════════════════════════════════════════════

class RiskEventType(Enum):
    """风控事件类型"""
    ORDER_REJECTED = "order_rejected"      # 订单被拒绝
    ORDER_PASSED = "order_passed"          # 订单通过检查
    POSITION_WARNING = "position_warning"  # 持仓警告（止损/止盈/回撤）
    STATUS_CHANGE = "status_change"        # 风控状态变更
    RULE_TRIGGERED = "rule_triggered"      # 规则触发（非拒绝类，如接近阈值）
    DATA_QUALITY_PAUSE = "data_quality_pause"  # 数据质量触发暂停
    DATA_QUALITY_WARNING = "data_quality_warning"  # 数据质量警告


# ═══════════════════════════════════════════════
#  风控事件
# ═══════════════════════════════════════════════

@dataclass
class RiskEvent:
    """
    风控事件（审计记录）

    Attributes
    ----------
    timestamp   : 事件时间戳
    event_type  : 事件类型
    rule_name   : 触发的规则名称
    symbol      : 相关标的
    reason      : 事件原因
    details     : 详细信息
    """
    timestamp: float = field(default_factory=time.time)
    event_type: RiskEventType = RiskEventType.ORDER_REJECTED
    rule_name: str = ""
    symbol: str = ""
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"RiskEvent({self.event_type.value}, rule={self.rule_name}, "
            f"symbol={self.symbol}, reason={self.reason[:30]})"
        )


# ═══════════════════════════════════════════════
#  数据质量风控配置（任务12新增）
# ═══════════════════════════════════════════════

@dataclass
class DataQualityRiskConfig:
    """
    数据质量风控配置

    控制数据质量事件如何触发风控动作（警告/暂停）。

    Attributes
    ----------
    enabled : bool
        是否启用数据质量风控（默认True）
    auto_pause_on_critical : bool
        收到CRITICAL级告警时自动全局暂停（默认True）
    auto_pause_on_pipeline_critical : bool
        流水线质量等级为critical时自动全局暂停（默认True）
    pause_bad_ratio_threshold : float
        脏数据占比超此值且级别为error时触发暂停（默认0.5）
    warning_bad_ratio_threshold : float
        脏数据占比超此值时触发警告（默认0.15）
    pause_quality_grades : List[str]
        触发暂停的质量等级列表（默认["critical", "poor"]）
    warning_quality_grades : List[str]
        触发警告的质量等级列表（默认["fair"]）
    min_bad_count_for_pause : int
        触发暂停的最少脏数据数（防止小样本误判，默认5）
    track_per_symbol : bool
        是否按标的跟踪数据质量状态（默认True）
    max_symbols_tracked : int
        最大跟踪标的数（超过则丢弃最早的，默认500）

    --- 任务12-E：数据质量恢复后自动恢复策略（可配置恢复阈值） ---
    auto_resume_enabled : bool
        是否启用数据质量自动恢复（默认True）
        关闭后暂停只能通过手动 resume() 解除（最保守模式）
    resume_quality_grades : List[str]
        达到这些质量等级之一即视为"数据质量已恢复"（默认["excellent", "good"]）
    resume_bad_ratio_threshold : float
        脏数据占比低于此值即视为"已恢复"（默认0.05，即5%）
    consecutive_good_required : int
        连续N次恢复信号才自动恢复（防单次偶然信号误恢复，实盘安全，默认1）
    """
    enabled: bool = True
    auto_pause_on_critical: bool = True
    auto_pause_on_pipeline_critical: bool = True
    pause_bad_ratio_threshold: float = 0.5
    warning_bad_ratio_threshold: float = 0.15
    pause_quality_grades: List[str] = field(
        default_factory=lambda: ["critical", "poor"]
    )
    warning_quality_grades: List[str] = field(
        default_factory=lambda: ["fair"]
    )
    min_bad_count_for_pause: int = 5
    track_per_symbol: bool = True
    max_symbols_tracked: int = 500

    # 任务12-E：数据质量恢复后自动恢复策略（可配置恢复阈值）
    auto_resume_enabled: bool = True
    resume_quality_grades: List[str] = field(
        default_factory=lambda: ["excellent", "good"]
    )
    resume_bad_ratio_threshold: float = 0.05
    consecutive_good_required: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "enabled": self.enabled,
            "auto_pause_on_critical": self.auto_pause_on_critical,
            "auto_pause_on_pipeline_critical": self.auto_pause_on_pipeline_critical,
            "pause_bad_ratio_threshold": self.pause_bad_ratio_threshold,
            "warning_bad_ratio_threshold": self.warning_bad_ratio_threshold,
            "pause_quality_grades": list(self.pause_quality_grades),
            "warning_quality_grades": list(self.warning_quality_grades),
            "min_bad_count_for_pause": self.min_bad_count_for_pause,
            "track_per_symbol": self.track_per_symbol,
            "max_symbols_tracked": self.max_symbols_tracked,
            "auto_resume_enabled": self.auto_resume_enabled,
            "resume_quality_grades": list(self.resume_quality_grades),
            "resume_bad_ratio_threshold": self.resume_bad_ratio_threshold,
            "consecutive_good_required": self.consecutive_good_required,
        }


# ═══════════════════════════════════════════════
#  单标的数据质量状态（任务12新增）
# ═══════════════════════════════════════════════

@dataclass
class SymbolDataQualityStatus:
    """
    单标的数据质量状态

    跟踪每个标的的数据质量事件历史，支持单标的暂停。

    Attributes
    ----------
    symbol : str
        标的代码
    current_grade : str
        当前数据质量等级（excellent/good/fair/poor/critical）
    current_bad_ratio : float
        当前脏数据占比
    current_bad_count : int
        当前脏数据数
    current_total_count : int
        当前总数据数
    last_event_time : float
        最后事件时间戳
    last_event_type : str
        最后事件类型
    event_count : int
        累计事件数
    critical_count : int
        critical级事件数
    error_count : int
        error级事件数
    warning_count : int
        warning级事件数
    is_paused : bool
        该标的是否因数据质量被暂停
    pause_reason : str
        暂停原因
    pause_time : float
        暂停时间戳
    dirty_types : Dict[str, int]
        各脏数据类型出现次数
    """
    symbol: str
    current_grade: str = "excellent"
    current_bad_ratio: float = 0.0
    current_bad_count: int = 0
    current_total_count: int = 0
    last_event_time: float = 0.0
    last_event_type: str = ""
    event_count: int = 0
    critical_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    is_paused: bool = False
    pause_reason: str = ""
    pause_time: float = 0.0
    dirty_types: Dict[str, int] = field(default_factory=dict)

    def record_event(
        self,
        severity: str,
        bad_ratio: float,
        bad_count: int,
        total_count: int,
        dirty_type: str = "",
        grade: str = "",
    ) -> None:
        """
        记录一次数据质量事件

        Parameters
        ----------
        severity : str
            事件级别（info/warning/error/critical）
        bad_ratio : float
            脏数据占比
        bad_count : int
            脏数据数
        total_count : int
            总数据数
        dirty_type : str
            脏数据类型
        grade : str
            数据质量等级
        """
        self.event_count += 1
        self.last_event_time = time.time()
        self.current_bad_ratio = bad_ratio
        self.current_bad_count = bad_count
        self.current_total_count = total_count

        if grade:
            self.current_grade = grade

        if severity == "critical":
            self.critical_count += 1
        elif severity == "error":
            self.error_count += 1
        elif severity == "warning":
            self.warning_count += 1

        if dirty_type:
            self.dirty_types[dirty_type] = self.dirty_types.get(dirty_type, 0) + 1

    def mark_paused(self, reason: str) -> None:
        """标记该标的为暂停状态"""
        self.is_paused = True
        self.pause_reason = reason
        self.pause_time = time.time()

    def reset_pause(self) -> None:
        """重置暂停状态"""
        self.is_paused = False
        self.pause_reason = ""
        self.pause_time = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "symbol": self.symbol,
            "current_grade": self.current_grade,
            "current_bad_ratio": self.current_bad_ratio,
            "current_bad_count": self.current_bad_count,
            "current_total_count": self.current_total_count,
            "last_event_time": self.last_event_time,
            "last_event_type": self.last_event_type,
            "event_count": self.event_count,
            "critical_count": self.critical_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "is_paused": self.is_paused,
            "pause_reason": self.pause_reason,
            "pause_time": self.pause_time,
            "dirty_types": dict(self.dirty_types),
        }


# ═══════════════════════════════════════════════
#  风控管理器
# ═══════════════════════════════════════════════

class RiskManager:
    """
    风控管理器（阶段9任务12增强：数据质量事件联动）

    负责订单前置检查、持仓监控、风控状态管理、事件记录、数据质量风控联动。
    与实盘引擎集成：engine在提交订单前调用 check_order，被拒绝的订单不提交到broker。

    数据质量风控链路（任务12）:
      1. subscribe_to_event_bus(bus) 订阅事件总线
      2. 清洗流水线发布 QualityAlertEvent / PipelineCompleteEvent
      3. RiskManager 自动接收并分级处理：
         - CRITICAL级告警 → 全局暂停（禁止所有新开仓）
         - ERROR级+高脏数据占比 → 单标的暂停（只禁止该标的买入）
         - 质量等级poor/critical → 暂停
         - 质量等级fair → 警告
      4. check_order() 自动拦截被暂停标的的买入订单
      5. resume() 恢复时清除所有数据质量暂停标记

    使用方式:
        from myquant.risk import RiskManager, RiskContext
        from myquant.core.event_bus import get_event_bus

        # 初始化（默认10类规则 + 数据质量风控）
        risk_mgr = RiskManager()

        # 订阅事件总线（数据质量风控自动生效）
        risk_mgr.subscribe_to_event_bus(get_event_bus())

        # 订单前置检查
        context = RiskContext(cash=100000, total_assets=100000, ...)
        result = risk_mgr.check_order(order, context)
        if not result.passed:
            print(f"风控拒绝: {result.reason}")

        # 查看数据质量状态
        dq_status = risk_mgr.get_data_quality_status("000001.SZ")

        # 恢复（清除所有暂停，包括数据质量暂停）
        risk_mgr.resume()
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        auto_pause_on_drawdown: bool = True,
        max_events: int = 1000,
        data_quality_config: Optional[DataQualityRiskConfig] = None,
    ):
        """
        初始化风控管理器

        Parameters
        ----------
        config : Dict | None
            风控配置（None则使用默认配置）
        auto_pause_on_drawdown : bool
            回撤超阈值时自动暂停（默认True）
        max_events : int
            最大事件记录数（超过则丢弃最早的）
        data_quality_config : DataQualityRiskConfig | None
            数据质量风控配置（None则使用默认配置）
        """
        self._auto_pause_on_drawdown = auto_pause_on_drawdown
        self._max_events = max_events

        # 规则库
        if config:
            self._library = RiskRuleLibrary.from_config(config)
        else:
            self._library = create_default_library()

        # 风控状态
        self._status = RiskStatus.NORMAL
        self._status_reason = ""

        # 风控事件
        self._events: List[RiskEvent] = []

        # 统计
        self._stats = {
            "total_orders_checked": 0,
            "orders_passed": 0,
            "orders_rejected": 0,
            "position_checks": 0,
            "position_warnings": 0,
            "status_changes": 0,
            "data_quality_events": 0,
            "data_quality_pauses": 0,
            "data_quality_warnings": 0,
            "data_quality_resumes": 0,
            "last_check_time": 0.0,
        }

        # ═══ 数据质量风控（任务12） ═══
        self._dq_config = data_quality_config or DataQualityRiskConfig()
        self._symbol_quality: Dict[str, SymbolDataQualityStatus] = {}
        self._event_bus: Optional[EventBus] = None
        self._dq_handler_ids: List[str] = []
        self._dq_global_paused: bool = False
        self._dq_pause_reason: str = ""

        # ═══ 数据质量自动恢复状态（任务12-E） ═══
        self._dq_global_pause_symbol: str = ""          # 触发全局暂停的标的（用于跟踪恢复）
        self._dq_global_resume_signals: int = 0         # 全局暂停期间的连续恢复信号数
        self._dq_symbol_resume_signals: Dict[str, int] = {}  # 单标的暂停期间的连续恢复信号数

        logger.info(
            f"风控管理器初始化: 规则数={len(self._library.rule_names)}, "
            f"状态={self._status.value}, "
            f"数据质量风控={'启用' if self._dq_config.enabled else '禁用'}"
        )

    # ═══════════════════════════════════════════
    #  订单前置检查（核心）
    # ═══════════════════════════════════════════

    def check_order(self, order: Order, context: RiskContext) -> RiskCheckResult:
        """
        订单前置检查（核心方法）

        在订单提交到broker之前调用，检查所有风控规则。
        被拒绝的订单不应提交到broker。

        检查流程:
          1. 如果风控状态为PAUSED且是买入订单，直接拒绝
          2. 如果该标的因数据质量被暂停且是买入订单，直接拒绝（任务12）
          3. 调用规则库批量检查
          4. 记录检查结果（通过/拒绝）
          5. 如果拒绝，记录风控事件
          6. 返回检查结果

        Parameters
        ----------
        order   : 待检查的订单
        context : 风控上下文（账户/持仓/行情等当前已知数据）

        Returns
        -------
        RiskCheckResult : 检查结果
        """
        self._stats["total_orders_checked"] += 1
        self._stats["last_check_time"] = time.time()

        # 1. PAUSED状态下禁止新开仓（只允许平仓/卖出）
        if self._status == RiskStatus.PAUSED and order.side == OrderSide.BUY:
            result = RiskCheckResult(
                passed=False,
                rule_name="risk_status_paused",
                reason=f"风控状态为PAUSED，禁止新开仓: {self._status_reason}",
            )
            self._stats["orders_rejected"] += 1
            self._record_event(RiskEvent(
                event_type=RiskEventType.ORDER_REJECTED,
                rule_name="risk_status_paused",
                symbol=order.symbol,
                reason=result.reason,
            ))
            logger.warning(f"[risk] 订单被拒（PAUSED）: {order.symbol} {order.side.value}")
            return result

        # 2. 数据质量单标的暂停检查（任务12）
        if self._dq_config.enabled and order.side == OrderSide.BUY and order.symbol:
            sym_status = self._symbol_quality.get(order.symbol)
            if sym_status and sym_status.is_paused:
                result = RiskCheckResult(
                    passed=False,
                    rule_name="data_quality_symbol_paused",
                    reason=(
                        f"标的{order.symbol}数据质量暂停，禁止买入: "
                        f"{sym_status.pause_reason}"
                    ),
                    details={
                        "symbol": order.symbol,
                        "current_grade": sym_status.current_grade,
                        "current_bad_ratio": sym_status.current_bad_ratio,
                        "pause_time": sym_status.pause_time,
                    },
                )
                self._stats["orders_rejected"] += 1
                self._record_event(RiskEvent(
                    event_type=RiskEventType.ORDER_REJECTED,
                    rule_name="data_quality_symbol_paused",
                    symbol=order.symbol,
                    reason=result.reason,
                    details=result.details,
                ))
                logger.warning(
                    f"[risk] 订单被拒（数据质量暂停）: {order.symbol} "
                    f"grade={sym_status.current_grade} "
                    f"bad_ratio={sym_status.current_bad_ratio*100:.1f}%"
                )
                return result

        # 3. 规则库批量检查
        result = self._library.check_all(order, context)

        # 4. 记录结果
        if result.passed:
            self._stats["orders_passed"] += 1
        else:
            self._stats["orders_rejected"] += 1
            self._record_event(RiskEvent(
                event_type=RiskEventType.ORDER_REJECTED,
                rule_name=result.rule_name,
                symbol=order.symbol,
                reason=result.reason,
                details=result.details,
            ))

        return result

    # ═══════════════════════════════════════════
    #  持仓监控
    # ═══════════════════════════════════════════

    def check_positions(self, context: RiskContext) -> List[RiskCheckResult]:
        """
        持仓监控（每根bar后调用）

        检查所有持仓的止损/止盈/回撤状态，发出警告。
        v1只发出警告，不自动平仓（自动平仓由guardian模块后续迭代实现）。

        检查项:
          1. 个股止损（亏损超阈值）
          2. 个股止盈（盈利超阈值，防追高警告）
          3. 账户最大回撤（超阈值则自动暂停）

        Parameters
        ----------
        context : 风控上下文

        Returns
        -------
        List[RiskCheckResult] : 警告列表（空列表=无警告）
        """
        self._stats["position_checks"] += 1
        warnings: List[RiskCheckResult] = []

        # 1. 个股止损/止盈检查
        stop_loss_rule = self._library.get_rule("stop_loss")
        take_profit_rule = self._library.get_rule("take_profit")

        for pos in context.positions:
            symbol = pos.get("symbol", "")
            avg_cost = pos.get("avg_cost", 0)
            market_price = pos.get("market_price", 0)
            quantity = pos.get("quantity", 0)

            if avg_cost <= 0 or market_price <= 0 or quantity == 0:
                continue

            # 多头持仓
            if quantity > 0:
                pnl_pct = (market_price - avg_cost) / avg_cost

                # 止损检查
                if stop_loss_rule and stop_loss_rule.enabled:
                    max_loss = getattr(stop_loss_rule, "max_loss_pct", 0.10)
                    if pnl_pct < -max_loss:
                        warning = RiskCheckResult(
                            passed=False,
                            rule_name="stop_loss",
                            reason=f"个股止损警告: {symbol} 亏损{pnl_pct*100:.1f}%, 阈值{max_loss*100:.1f}%",
                            details={"symbol": symbol, "pnl_pct": pnl_pct, "avg_cost": avg_cost, "market_price": market_price},
                        )
                        warnings.append(warning)
                        self._record_event(RiskEvent(
                            event_type=RiskEventType.POSITION_WARNING,
                            rule_name="stop_loss",
                            symbol=symbol,
                            reason=warning.reason,
                        ))

                # 止盈检查（防追高警告）
                if take_profit_rule and take_profit_rule.enabled:
                    max_profit = getattr(take_profit_rule, "max_profit_pct", 0.50)
                    if pnl_pct > max_profit:
                        warning = RiskCheckResult(
                            passed=False,
                            rule_name="take_profit",
                            reason=f"个股止盈警告: {symbol} 盈利{pnl_pct*100:.1f}%, 阈值{max_profit*100:.1f}%",
                            details={"symbol": symbol, "pnl_pct": pnl_pct},
                        )
                        warnings.append(warning)
                        self._record_event(RiskEvent(
                            event_type=RiskEventType.POSITION_WARNING,
                            rule_name="take_profit",
                            symbol=symbol,
                            reason=warning.reason,
                        ))

        # 2. 账户最大回撤检查
        drawdown_rule = self._library.get_rule("max_drawdown")
        if drawdown_rule and drawdown_rule.enabled:
            max_dd = getattr(drawdown_rule, "max_drawdown_pct", 0.20)
            current_dd = context.drawdown
            if current_dd > max_dd:
                warning = RiskCheckResult(
                    passed=False,
                    rule_name="max_drawdown",
                    reason=f"账户回撤警告: 当前{current_dd*100:.1f}%, 阈值{max_dd*100:.1f}%",
                    details={"drawdown": current_dd, "max_drawdown": max_dd},
                )
                warnings.append(warning)
                self._record_event(RiskEvent(
                    event_type=RiskEventType.POSITION_WARNING,
                    rule_name="max_drawdown",
                    reason=warning.reason,
                ))

                # 自动暂停（如果启用）
                if self._auto_pause_on_drawdown and self._status != RiskStatus.PAUSED:
                    self._set_status(RiskStatus.PAUSED, f"账户回撤{current_dd*100:.1f}%超阈值{max_dd*100:.1f}%")

        self._stats["position_warnings"] += len(warnings)

        # 更新风控状态（有警告则WARNING）
        if warnings and self._status == RiskStatus.NORMAL:
            self._set_status(RiskStatus.WARNING, "持仓监控发现警告")

        return warnings

    # ═══════════════════════════════════════════
    #  风控状态管理
    # ═══════════════════════════════════════════

    def get_status(self) -> RiskStatus:
        """获取当前风控状态"""
        return self._status

    def get_status_reason(self) -> str:
        """获取当前状态原因"""
        return self._status_reason

    def pause(self, reason: str = "手动暂停") -> None:
        """
        手动暂停风控（禁止新开仓）

        Parameters
        ----------
        reason : str
            暂停原因
        """
        self._set_status(RiskStatus.PAUSED, reason)
        logger.warning(f"[risk] 风控已暂停: {reason}")

        # 发布策略暂停事件（供实盘引擎/API消费，任务12-B）
        self._publish_pause_event(
            paused=True,
            pause_level="global",
            symbol="",
            reason=reason,
            severity="warning",
        )

    def resume(self) -> None:
        """
        恢复风控（从PAUSED恢复到NORMAL）

        同时清除所有数据质量暂停标记（任务12）。
        注意：需要手动确认风险已解除才能恢复。
        恢复时发布 RISK_RESUMED 事件（供实盘引擎/API消费，任务12-B）。
        """
        # 记录恢复前状态（用于发布恢复事件，避免重复发布）
        was_dq_global_paused = self._dq_global_paused
        was_status_paused = self._status == RiskStatus.PAUSED
        paused_symbols = [
            sym for sym, status in self._symbol_quality.items()
            if status.is_paused
        ]

        # 任务12-E：清除自动恢复信号计数（手动恢复后重新开始计数）
        self._dq_global_pause_symbol = ""
        self._dq_global_resume_signals = 0
        self._dq_symbol_resume_signals.clear()

        # 清除数据质量全局暂停
        if self._dq_global_paused:
            self._dq_global_paused = False
            self._dq_pause_reason = ""
            logger.info("[risk] 数据质量全局暂停已清除")

        # 清除所有单标的数据质量暂停
        dq_resume_count = 0
        for sym_status in self._symbol_quality.values():
            if sym_status.is_paused:
                sym_status.reset_pause()
                dq_resume_count += 1
        if dq_resume_count > 0:
            logger.info(f"[risk] {dq_resume_count}个标的的数据质量暂停已清除")

        if self._status == RiskStatus.PAUSED:
            self._set_status(RiskStatus.NORMAL, "手动恢复")
            logger.info("[risk] 风控已恢复")
        else:
            logger.warning(f"[risk] 当前状态为{self._status.value}，无需恢复")

        # 发布恢复事件（供实盘引擎/API消费，任务12-B）
        if was_dq_global_paused or was_status_paused:
            self._publish_pause_event(
                paused=False,
                pause_level="global",
                symbol="",
                reason="风控已恢复（数据质量/手动暂停已解除）",
            )
        for sym in paused_symbols:
            self._publish_pause_event(
                paused=False,
                pause_level="symbol",
                symbol=sym,
                reason="标的数据质量暂停已解除",
            )

    def _set_status(self, status: RiskStatus, reason: str) -> None:
        """设置风控状态（内部方法）"""
        if self._status != status:
            old_status = self._status
            self._status = status
            self._status_reason = reason
            self._stats["status_changes"] += 1
            self._record_event(RiskEvent(
                event_type=RiskEventType.STATUS_CHANGE,
                rule_name="status_manager",
                reason=f"{old_status.value} -> {status.value}: {reason}",
            ))
            logger.info(f"[risk] 风控状态变更: {old_status.value} -> {status.value} ({reason})")

    def is_paused(self) -> bool:
        """是否处于暂停状态"""
        return self._status == RiskStatus.PAUSED

    def can_open_position(self) -> bool:
        """是否允许新开仓"""
        return self._status != RiskStatus.PAUSED

    # ═══════════════════════════════════════════
    #  数据质量风控 — 事件总线订阅（任务12）
    # ═══════════════════════════════════════════

    def subscribe_to_event_bus(self, bus: EventBus) -> List[str]:
        """
        订阅事件总线的数据质量事件

        订阅3类事件（不订阅高频DATA_QUALITY_DIRTY，由任务10聚合后发布低频告警）:
          1. DATA_QUALITY_CRITICAL → critical级告警 → 全局暂停
          2. DATA_QUALITY_ALERT → warning/error级告警 → 分级处理
          3. DATA_QUALITY_PIPELINE_COMPLETE → 流水线完成 → 按质量等级处理

        Parameters
        ----------
        bus : EventBus
            事件总线实例

        Returns
        -------
        List[str] : 订阅的handler ID列表
        """
        if not self._dq_config.enabled:
            logger.info("[risk] 数据质量风控未启用，跳过事件总线订阅")
            return []

        self._event_bus = bus
        ids: List[str] = []

        # 1. CRITICAL级告警 → 全局暂停
        hid_critical = bus.subscribe(
            EventType.DATA_QUALITY_CRITICAL,
            self.handle_quality_alert_event,
        )
        ids.append(hid_critical)

        # 2. ALERT级告警（warning/error）→ 分级处理
        hid_alert = bus.subscribe(
            EventType.DATA_QUALITY_ALERT,
            self.handle_quality_alert_event,
        )
        ids.append(hid_alert)

        # 3. 流水线完成 → 按质量等级处理
        hid_pipeline = bus.subscribe(
            EventType.DATA_QUALITY_PIPELINE_COMPLETE,
            self.handle_pipeline_complete_event,
        )
        ids.append(hid_pipeline)

        self._dq_handler_ids = ids
        logger.info(
            f"[risk] 数据质量风控已订阅事件总线: "
            f"{len(ids)}个handler (critical/alert/pipeline)"
        )
        return ids

    def unsubscribe_from_event_bus(self) -> int:
        """
        取消事件总线订阅

        Returns
        -------
        int : 取消订阅的handler数量
        """
        if not self._event_bus or not self._dq_handler_ids:
            return 0

        count = 0
        for hid in self._dq_handler_ids:
            if self._event_bus.unsubscribe(hid):
                count += 1

        self._dq_handler_ids = []
        logger.info(f"[risk] 数据质量风控已取消事件总线订阅: {count}个handler")
        return count

    # ═══════════════════════════════════════════
    #  数据质量风控 — 事件处理（任务12）
    # ═══════════════════════════════════════════

    def handle_quality_alert_event(self, event: QualityAlertEvent) -> Optional[RiskStatus]:
        """
        处理质量告警事件（QualityAlertEvent）

        分级处理逻辑:
          1. critical级 → 全局暂停（所有标的禁止新开仓）
          2. error级 + bad_ratio >= pause阈值 → 单标的暂停
          3. warning/error级 → 风控状态置为WARNING（如果当前是NORMAL）

        Parameters
        ----------
        event : QualityAlertEvent
            质量告警事件

        Returns
        -------
        RiskStatus | None : 处理后的风控状态（None=未启用数据质量风控）
        """
        if not self._dq_config.enabled:
            return None

        self._stats["data_quality_events"] += 1

        symbol = event.symbol or "GLOBAL"
        status = self._get_or_create_symbol_status(symbol)
        status.last_event_type = "quality_alert"
        status.record_event(
            severity=event.alert_level,
            bad_ratio=event.bad_ratio,
            bad_count=int(event.bad_ratio * 100),
            total_count=100,
            dirty_type=event.dirty_type,
            grade=event.quality_grade,
        )

        # 0. info级 → 数据质量恢复信号（任务12-E：自动恢复检查）
        # 部分部署只发布告警事件，info级+低脏数据占比视为数据质量已恢复
        if event.alert_level == "info":
            self._check_auto_resume(symbol, "good", event.bad_ratio)
            return self._status

        # 1. critical级 → 全局暂停
        if event.alert_level == "critical" and self._dq_config.auto_pause_on_critical:
            reason = (
                f"数据质量CRITICAL告警: {event.message}, "
                f"symbol={symbol}, bad_ratio={event.bad_ratio*100:.1f}%"
            )
            self._pause_for_data_quality(symbol, reason, global_pause=True)
            self._stats["data_quality_pauses"] += 1
            return RiskStatus.PAUSED

        # 2. error级 + 高脏数据占比 → 单标的暂停
        if (
            event.alert_level == "error"
            and event.bad_ratio >= self._dq_config.pause_bad_ratio_threshold
        ):
            reason = (
                f"数据质量ERROR+高脏数据占比: {event.message}, "
                f"symbol={symbol}, bad_ratio={event.bad_ratio*100:.1f}%"
            )
            self._pause_for_data_quality(symbol, reason, global_pause=False)
            self._stats["data_quality_pauses"] += 1
            return RiskStatus.PAUSED

        # 3. warning/error级 → 风控状态WARNING
        if event.alert_level in ("warning", "error") and self._status == RiskStatus.NORMAL:
            warn_reason = f"数据质量告警: {event.message}, symbol={symbol}"
            self._set_status(RiskStatus.WARNING, warn_reason)
            self._stats["data_quality_warnings"] += 1
            self._record_event(RiskEvent(
                event_type=RiskEventType.DATA_QUALITY_WARNING,
                rule_name="data_quality_alert",
                symbol=symbol,
                reason=warn_reason,
                details={
                    "alert_level": event.alert_level,
                    "bad_ratio": event.bad_ratio,
                    "dirty_type": event.dirty_type,
                },
            ))

        return self._status

    def handle_pipeline_complete_event(self, event: PipelineCompleteEvent) -> Optional[RiskStatus]:
        """
        处理流水线完成事件（PipelineCompleteEvent）

        按质量等级分级处理:
          1. 流水线失败 → 风控状态WARNING
          2. 质量等级critical → 全局暂停
          3. 质量等级poor + bad_count>=阈值 + bad_ratio>=警告阈值 → 单标的暂停
          4. 质量等级fair → 风控状态WARNING

        Parameters
        ----------
        event : PipelineCompleteEvent
            流水线完成事件

        Returns
        -------
        RiskStatus | None : 处理后的风控状态
        """
        if not self._dq_config.enabled:
            return None

        self._stats["data_quality_events"] += 1

        symbol = event.symbol or "GLOBAL"
        status = self._get_or_create_symbol_status(symbol)
        status.last_event_type = "pipeline_complete"
        status.current_grade = event.quality_grade
        status.current_bad_ratio = event.bad_ratio
        status.current_bad_count = event.bad_count
        status.current_total_count = event.total_count
        status.last_event_time = event.timestamp
        status.event_count += 1

        # 1. 流水线失败 → 警告
        if event.status == "failed":
            fail_reason = f"清洗流水线失败: {symbol}, error={event.error}"
            if self._status == RiskStatus.NORMAL:
                self._set_status(RiskStatus.WARNING, fail_reason)
            self._stats["data_quality_warnings"] += 1
            self._record_event(RiskEvent(
                event_type=RiskEventType.DATA_QUALITY_WARNING,
                rule_name="pipeline_failed",
                symbol=symbol,
                reason=fail_reason,
                details={"error": event.error, "duration": event.duration},
            ))
            return self._status

        # 任务12-E：数据质量恢复检查（质量恢复后自动解除暂停）
        # 流水线成功完成时，以最新质量等级/脏数据占比作为恢复信号
        self._check_auto_resume(symbol, event.quality_grade, event.bad_ratio)

        # 2. critical质量等级 → 全局暂停
        if (
            event.quality_grade == "critical"
            and self._dq_config.auto_pause_on_pipeline_critical
        ):
            reason = (
                f"数据质量等级critical: symbol={symbol}, "
                f"bad_ratio={event.bad_ratio*100:.1f}%, "
                f"bad={event.bad_count}/{event.total_count}"
            )
            self._pause_for_data_quality(symbol, reason, global_pause=True)
            self._stats["data_quality_pauses"] += 1
            return RiskStatus.PAUSED

        # 3. poor质量等级 + 足够脏数据 → 单标的暂停
        if (
            event.quality_grade == "poor"
            and event.quality_grade in self._dq_config.pause_quality_grades
            and event.bad_count >= self._dq_config.min_bad_count_for_pause
            and event.bad_ratio >= self._dq_config.warning_bad_ratio_threshold
        ):
            reason = (
                f"数据质量等级poor: symbol={symbol}, "
                f"bad_ratio={event.bad_ratio*100:.1f}%, "
                f"bad={event.bad_count}/{event.total_count}"
            )
            self._pause_for_data_quality(symbol, reason, global_pause=False)
            self._stats["data_quality_pauses"] += 1
            return RiskStatus.PAUSED

        # 4. fair质量等级 → 警告
        if (
            event.quality_grade in self._dq_config.warning_quality_grades
            and self._status == RiskStatus.NORMAL
        ):
            warn_reason = (
                f"数据质量等级{event.quality_grade}: symbol={symbol}, "
                f"bad_ratio={event.bad_ratio*100:.1f}%"
            )
            self._set_status(RiskStatus.WARNING, warn_reason)
            self._stats["data_quality_warnings"] += 1
            self._record_event(RiskEvent(
                event_type=RiskEventType.DATA_QUALITY_WARNING,
                rule_name="data_quality_grade",
                symbol=symbol,
                reason=warn_reason,
                details={
                    "quality_grade": event.quality_grade,
                    "bad_ratio": event.bad_ratio,
                    "bad_count": event.bad_count,
                    "total_count": event.total_count,
                },
            ))

        return self._status

    def handle_data_quality_event(self, event: DataQualityEvent) -> Optional[RiskStatus]:
        """
        处理原始脏数据事件（DataQualityEvent）

        注意：默认不订阅此高频事件，仅在需要逐条跟踪时手动调用。
        推荐使用 handle_quality_alert_event() 处理聚合后的低频告警。

        Parameters
        ----------
        event : DataQualityEvent
            原始脏数据事件

        Returns
        -------
        RiskStatus | None : 处理后的风控状态
        """
        if not self._dq_config.enabled:
            return None

        self._stats["data_quality_events"] += 1

        symbol = event.symbol or "GLOBAL"
        status = self._get_or_create_symbol_status(symbol)
        status.last_event_type = "data_quality_dirty"
        status.record_event(
            severity=event.severity,
            bad_ratio=event.bad_ratio,
            bad_count=event.bad_count,
            total_count=event.total_count,
            dirty_type=event.dirty_type,
            grade=event.quality_grade,
        )

        # critical级原始事件 → 单标的暂停
        if event.severity == "critical":
            reason = (
                f"原始脏数据CRITICAL: symbol={symbol}, "
                f"dirty_type={event.dirty_type}, "
                f"bad_ratio={event.bad_ratio*100:.1f}%"
            )
            self._pause_for_data_quality(symbol, reason, global_pause=False)
            self._stats["data_quality_pauses"] += 1
            return RiskStatus.PAUSED

        return self._status

    # ═══════════════════════════════════════════
    #  数据质量风控 — 状态查询（任务12）
    # ═══════════════════════════════════════════

    def get_data_quality_status(self, symbol: str) -> Optional[SymbolDataQualityStatus]:
        """
        获取指定标的的数据质量状态

        Parameters
        ----------
        symbol : str
            标的代码

        Returns
        -------
        SymbolDataQualityStatus | None : 数据质量状态（None=未跟踪该标的）
        """
        return self._symbol_quality.get(symbol)

    def get_all_data_quality_status(self) -> Dict[str, SymbolDataQualityStatus]:
        """
        获取所有标的的数据质量状态

        Returns
        -------
        Dict[str, SymbolDataQualityStatus] : 标的→状态映射
        """
        return dict(self._symbol_quality)

    def get_paused_symbols(self) -> List[str]:
        """
        获取因数据质量被暂停的标的列表

        Returns
        -------
        List[str] : 被暂停的标的代码列表
        """
        return [
            sym for sym, status in self._symbol_quality.items()
            if status.is_paused
        ]

    def is_symbol_data_quality_paused(self, symbol: str) -> bool:
        """
        检查指定标的是否因数据质量被暂停

        Parameters
        ----------
        symbol : str
            标的代码

        Returns
        -------
        bool : 是否被暂停
        """
        status = self._symbol_quality.get(symbol)
        return status is not None and status.is_paused

    @property
    def data_quality_global_paused(self) -> bool:
        """是否因数据质量全局暂停"""
        return self._dq_global_paused

    @property
    def data_quality_pause_reason(self) -> str:
        """数据质量全局暂停原因"""
        return self._dq_pause_reason

    @property
    def data_quality_config(self) -> DataQualityRiskConfig:
        """数据质量风控配置（只读）"""
        return self._dq_config

    def reset_data_quality_status(self, symbol: Optional[str] = None) -> None:
        """
        重置数据质量状态

        Parameters
        ----------
        symbol : str | None
            指定标的（None=重置所有标的）
        """
        if symbol:
            if symbol in self._symbol_quality:
                was_paused = self._symbol_quality[symbol].is_paused
                self._symbol_quality[symbol].reset_pause()
                if was_paused:
                    # 发布恢复事件（供实盘引擎/API消费，任务12-B）
                    self._publish_pause_event(
                        paused=False,
                        pause_level="symbol",
                        symbol=symbol,
                        reason="标的数据质量状态已重置",
                    )
                logger.info(f"[risk] 标的{symbol}数据质量状态已重置")
        else:
            was_global = self._dq_global_paused
            paused_symbols = [
                sym for sym, status in self._symbol_quality.items()
                if status.is_paused
            ]
            for status in self._symbol_quality.values():
                status.reset_pause()
            self._dq_global_paused = False
            self._dq_pause_reason = ""
            # 任务12-E：清除自动恢复信号计数
            self._dq_global_pause_symbol = ""
            self._dq_global_resume_signals = 0
            self._dq_symbol_resume_signals.clear()
            # 发布恢复事件（供实盘引擎/API消费，任务12-B）
            if was_global:
                self._publish_pause_event(
                    paused=False,
                    pause_level="global",
                    symbol="",
                    reason="数据质量状态全部重置",
                )
            for sym in paused_symbols:
                self._publish_pause_event(
                    paused=False,
                    pause_level="symbol",
                    symbol=sym,
                    reason="标的数据质量状态已重置",
                )
            logger.info("[risk] 所有标的数据质量状态已重置")

    # ═══════════════════════════════════════════
    #  数据质量风控 — 内部方法（任务12）
    # ═══════════════════════════════════════════

    def _get_or_create_symbol_status(self, symbol: str) -> SymbolDataQualityStatus:
        """
        获取或创建标的数据质量状态

        Parameters
        ----------
        symbol : str
            标的代码

        Returns
        -------
        SymbolDataQualityStatus : 标的数据质量状态
        """
        if symbol not in self._symbol_quality:
            # 超过最大跟踪数则丢弃最早的
            if len(self._symbol_quality) >= self._dq_config.max_symbols_tracked:
                oldest_sym = min(
                    self._symbol_quality.keys(),
                    key=lambda s: self._symbol_quality[s].last_event_time,
                )
                del self._symbol_quality[oldest_sym]
                logger.debug(f"[risk] 数据质量跟踪超限，丢弃最早标的: {oldest_sym}")
            self._symbol_quality[symbol] = SymbolDataQualityStatus(symbol=symbol)
        return self._symbol_quality[symbol]

    def _pause_for_data_quality(
        self,
        symbol: str,
        reason: str,
        global_pause: bool = False,
    ) -> None:
        """
        数据质量触发暂停（内部方法）

        Parameters
        ----------
        symbol : str
            相关标的
        reason : str
            暂停原因
        global_pause : bool
            是否全局暂停（True=禁止所有新开仓，False=只暂停该标的）
        """
        status = self._get_or_create_symbol_status(symbol)
        status.mark_paused(reason)

        if global_pause:
            self._dq_global_paused = True
            self._dq_pause_reason = reason
            # 任务12-E：记录触发标的并重置恢复信号计数（新暂停需要新的恢复信号）
            self._dq_global_pause_symbol = symbol
            self._dq_global_resume_signals = 0
            self._set_status(RiskStatus.PAUSED, reason)
        else:
            # 单标的暂停，不改变全局状态（如果当前是NORMAL则置WARNING）
            if self._status == RiskStatus.NORMAL:
                self._set_status(
                    RiskStatus.WARNING,
                    f"单标的数据质量暂停: {symbol}",
                )
            # 任务12-E：重置该标的的恢复信号计数
            self._dq_symbol_resume_signals.pop(symbol, None)

        self._record_event(RiskEvent(
            event_type=RiskEventType.DATA_QUALITY_PAUSE,
            rule_name="data_quality_risk",
            symbol=symbol,
            reason=reason,
            details={
                "global_pause": global_pause,
                "symbol": symbol,
                "current_grade": status.current_grade,
                "current_bad_ratio": status.current_bad_ratio,
            },
        ))
        logger.warning(
            f"[risk] 数据质量触发暂停: {reason} (global={global_pause})"
        )

        # 发布策略暂停事件（供实盘引擎/API消费，任务12-B）
        self._publish_pause_event(
            paused=True,
            pause_level="global" if global_pause else "symbol",
            symbol=symbol,
            reason=reason,
            quality_grade=status.current_grade,
            bad_ratio=status.current_bad_ratio,
            severity="critical" if global_pause else "error",
        )

    def _publish_pause_event(
        self,
        paused: bool,
        pause_level: str,
        symbol: str,
        reason: str,
        severity: str = "warning",
        quality_grade: str = "",
        bad_ratio: float = 0.0,
    ) -> Optional[StrategyPauseEvent]:
        """
        发布策略暂停/恢复事件到事件总线（内部方法，任务12-B）

        供实盘引擎/API消费：实盘引擎订阅 RISK_PAUSED / RISK_RESUMED 事件，
        收到暂停事件立即停止策略下单，收到恢复事件重新开放。

        Parameters
        ----------
        paused : bool
            True=暂停，False=恢复
        pause_level : str
            暂停级别：global（全局）/ symbol（单标的）
        symbol : str
            相关标的代码（global 级别时为触发暂停的标的）
        reason : str
            暂停/恢复原因
        severity : str
            严重级别（info/warning/error/critical）
        quality_grade : str
            数据质量等级（数据质量触发时携带）
        bad_ratio : float
            脏数据占比（数据质量触发时携带）

        Returns
        -------
        StrategyPauseEvent | None : 发布成功返回事件对象；未订阅事件总线返回None
        """
        if self._event_bus is None:
            return None

        event = StrategyPauseEvent(
            event_type=EventType.RISK_RESUMED if not paused else EventType.RISK_PAUSED,
            paused=paused,
            pause_level=pause_level,
            symbol=symbol,
            reason=reason,
            severity=severity,
            quality_grade=quality_grade,
            bad_ratio=bad_ratio,
            source="risk_manager",
        )
        try:
            self._event_bus.publish(event)
            logger.debug(
                f"[risk] 发布{'暂停' if paused else '恢复'}事件: "
                f"level={pause_level}, symbol={symbol or 'ALL'}"
            )
            return event
        except Exception as e:
            logger.error(f"[risk] 发布策略{'暂停' if paused else '恢复'}事件失败: {e}")
            return None

    def _check_auto_resume(self, symbol: str, grade: str, bad_ratio: float) -> bool:
        """
        数据质量恢复检查并自动恢复策略（内部方法，任务12-E）

        数据质量恢复后（脏数据占比低于阈值 + 质量等级达标），自动解除
        数据质量暂停，无需人工干预。所有判定仅基于当前已收到的事件，
        不预测未来（严格禁用未来函数）。

        判定条件（可配置阈值，见 DataQualityRiskConfig）:
          1. auto_resume_enabled 已启用
          2. 质量等级 ∈ resume_quality_grades（默认 excellent/good）
          3. 脏数据占比 < resume_bad_ratio_threshold（默认 0.05）
          4. 存在可恢复的暂停（全局暂停 或 该标的暂停）
          5. 连续 consecutive_good_required 次恢复信号（默认1，防单次偶然信号）

        恢复动作:
          - 单标的暂停: 质量恢复后自动解除该标的暂停
          - 全局暂停: 连续N次恢复信号后自动解除全局暂停
          - 每次恢复都发布 RISK_RESUMED 事件（供实盘引擎/API/告警/指标联动）

        Parameters
        ----------
        symbol : str
            数据质量恢复涉及的标的
        grade : str
            当前质量等级（excellent/good/fair/poor/critical）
        bad_ratio : float
            当前脏数据占比

        Returns
        -------
        bool : 是否执行了自动恢复
        """
        if not self._dq_config.enabled or not self._dq_config.auto_resume_enabled:
            return False

        is_recovered = (
            grade in self._dq_config.resume_quality_grades
            and bad_ratio < self._dq_config.resume_bad_ratio_threshold
        )
        need = max(1, int(self._dq_config.consecutive_good_required))
        resumed_any = False

        # 1. 全局暂停自动恢复
        if self._dq_global_paused:
            if is_recovered:
                self._dq_global_resume_signals += 1
                logger.info(
                    f"[risk] 数据质量恢复信号({self._dq_global_resume_signals}/{need}): "
                    f"symbol={symbol}, grade={grade}, bad_ratio={bad_ratio:.2%}"
                )
                if self._dq_global_resume_signals >= need:
                    self._dq_global_paused = False
                    self._dq_pause_reason = ""
                    self._dq_global_pause_symbol = ""
                    self._dq_global_resume_signals = 0
                    if self._status == RiskStatus.PAUSED:
                        self._set_status(RiskStatus.NORMAL, "数据质量自动恢复")
                    self._stats["data_quality_resumes"] += 1
                    self._record_event(RiskEvent(
                        event_type=RiskEventType.STATUS_CHANGE,
                        rule_name="data_quality_auto_resume",
                        symbol=symbol,
                        reason="数据质量已恢复，自动解除全局暂停",
                        details={"grade": grade, "bad_ratio": bad_ratio},
                    ))
                    # 发布恢复事件（供实盘引擎/API/告警/指标联动）
                    self._publish_pause_event(
                        paused=False,
                        pause_level="global",
                        symbol=symbol,
                        reason="数据质量已恢复，自动解除全局暂停",
                    )
                    resumed_any = True
            else:
                # 坏信号：重置全局恢复计数
                self._dq_global_resume_signals = 0

        # 2. 单标的暂停自动恢复
        status = self._symbol_quality.get(symbol)
        if status is not None and status.is_paused:
            if is_recovered:
                cur = self._dq_symbol_resume_signals.get(symbol, 0) + 1
                self._dq_symbol_resume_signals[symbol] = cur
                logger.info(
                    f"[risk] 标的数据质量恢复信号({cur}/{need}): "
                    f"symbol={symbol}, grade={grade}, bad_ratio={bad_ratio:.2%}"
                )
                if cur >= need:
                    status.reset_pause()
                    self._dq_symbol_resume_signals.pop(symbol, None)
                    self._stats["data_quality_resumes"] += 1
                    self._record_event(RiskEvent(
                        event_type=RiskEventType.STATUS_CHANGE,
                        rule_name="data_quality_auto_resume",
                        symbol=symbol,
                        reason="标的数据质量已恢复，自动解除暂停",
                        details={"grade": grade, "bad_ratio": bad_ratio},
                    ))
                    # 发布恢复事件（供实盘引擎/API/告警/指标联动）
                    self._publish_pause_event(
                        paused=False,
                        pause_level="symbol",
                        symbol=symbol,
                        reason="标的数据质量已恢复，自动解除暂停",
                    )
                    resumed_any = True
            else:
                # 坏信号：重置该标的恢复计数
                self._dq_symbol_resume_signals.pop(symbol, None)

        return resumed_any

    # ═══════════════════════════════════════════
    #  规则库管理（委托给RiskRuleLibrary）
    # ═══════════════════════════════════════════

    def add_rule(self, rule: RiskRule) -> None:
        """添加风控规则"""
        self._library.add_rule(rule)

    def remove_rule(self, rule_name: str) -> bool:
        """移除风控规则"""
        return self._library.remove_rule(rule_name)

    def get_rule(self, rule_name: str) -> Optional[RiskRule]:
        """获取指定规则"""
        return self._library.get_rule(rule_name)

    def enable_rule(self, rule_name: str) -> bool:
        """启用规则"""
        return self._library.enable_rule(rule_name)

    def disable_rule(self, rule_name: str) -> bool:
        """禁用规则"""
        return self._library.disable_rule(rule_name)

    @property
    def rule_names(self) -> List[str]:
        """获取所有规则名称"""
        return self._library.rule_names

    @property
    def enabled_rules(self) -> List[str]:
        """获取已启用的规则名称"""
        return self._library.enabled_rules

    # ═══════════════════════════════════════════
    #  风控事件
    # ═══════════════════════════════════════════

    def _record_event(self, event: RiskEvent) -> None:
        """记录风控事件（内部方法）"""
        self._events.append(event)
        # 超过最大记录数则丢弃最早的
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]

    def get_events(
        self,
        event_type: Optional[RiskEventType] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
    ) -> List[RiskEvent]:
        """
        获取风控事件（支持过滤）

        Parameters
        ----------
        event_type : RiskEventType | None
            事件类型过滤（None=全部）
        symbol : str | None
            标的过滤（None=全部）
        limit : int
            返回最大数量

        Returns
        -------
        List[RiskEvent] : 风控事件列表（按时间倒序）
        """
        events = self._events
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if symbol:
            events = [e for e in events if e.symbol == symbol]
        # 按时间倒序
        events = sorted(events, key=lambda e: e.timestamp, reverse=True)
        return events[:limit]

    def get_recent_events(self, count: int = 10) -> List[RiskEvent]:
        """获取最近N条风控事件"""
        return self.get_events(limit=count)

    @property
    def event_count(self) -> int:
        """事件总数"""
        return len(self._events)

    # ═══════════════════════════════════════════
    #  统计
    # ═══════════════════════════════════════════

    @property
    def stats(self) -> Dict[str, Any]:
        """风控统计（只读）"""
        return dict(self._stats)

    def get_summary(self) -> Dict[str, Any]:
        """
        获取风控摘要（含数据质量风控状态，任务12增强）

        Returns
        -------
        Dict[str, Any] :
            {
                "status": str,
                "status_reason": str,
                "rules_count": int,
                "enabled_rules": int,
                "stats": Dict,
                "event_count": int,
                "recent_events": List,
                "data_quality": {
                    "enabled": bool,
                    "global_paused": bool,
                    "pause_reason": str,
                    "tracked_symbols": int,
                    "paused_symbols": List[str],
                    "config": Dict,
                },
            }
        """
        return {
            "status": self._status.value,
            "status_reason": self._status_reason,
            "rules_count": len(self._library.rule_names),
            "enabled_rules": len(self._library.enabled_rules),
            "stats": dict(self._stats),
            "event_count": len(self._events),
            "recent_events": [
                {"timestamp": e.timestamp, "type": e.event_type.value,
                 "rule": e.rule_name, "symbol": e.symbol, "reason": e.reason}
                for e in self.get_recent_events(5)
            ],
            "data_quality": {
                "enabled": self._dq_config.enabled,
                "global_paused": self._dq_global_paused,
                "pause_reason": self._dq_pause_reason,
                "tracked_symbols": len(self._symbol_quality),
                "paused_symbols": self.get_paused_symbols(),
                "auto_resume_enabled": self._dq_config.auto_resume_enabled,
                "global_resume_signals": self._dq_global_resume_signals,
                "symbol_resume_signals": dict(self._dq_symbol_resume_signals),
                "config": self._dq_config.to_dict(),
            },
        }

    # ═══════════════════════════════════════════
    #  重置
    # ═══════════════════════════════════════════

    def reset(self) -> None:
        """重置风控管理器（清空事件/统计/数据质量状态，恢复NORMAL状态）"""
        self._status = RiskStatus.NORMAL
        self._status_reason = ""
        self._events.clear()
        self._stats = {
            "total_orders_checked": 0,
            "orders_passed": 0,
            "orders_rejected": 0,
            "position_checks": 0,
            "position_warnings": 0,
            "status_changes": 0,
            "data_quality_events": 0,
            "data_quality_pauses": 0,
            "data_quality_warnings": 0,
            "data_quality_resumes": 0,
            "last_check_time": 0.0,
        }
        # 重置数据质量风控
        self._symbol_quality.clear()
        self._dq_global_paused = False
        self._dq_pause_reason = ""
        # 任务12-E：清除自动恢复信号计数
        self._dq_global_pause_symbol = ""
        self._dq_global_resume_signals = 0
        self._dq_symbol_resume_signals.clear()
        # 发布恢复事件（供实盘引擎/API消费，任务12-B）
        self._publish_pause_event(
            paused=False,
            pause_level="global",
            symbol="",
            reason="风控管理器已重置",
        )
        logger.info("[risk] 风控管理器已重置（含数据质量状态）")

    def __repr__(self) -> str:
        return (
            f"RiskManager(status={self._status.value}, "
            f"rules={len(self._library.rule_names)}, "
            f"events={len(self._events)}, "
            f"dq_paused={self._dq_global_paused})"
        )
