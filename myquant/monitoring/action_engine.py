# 文件名：myquant/monitoring/action_engine.py
"""
自动化响应编排规则引擎（D-1）+ 自愈动作库（D-2）
=====================================================
依据：《量化系统监控模块施工总清单_V1.2.md》D-1/D-2 行
落点  ：monitoring/action_engine.py（D-1 规则引擎 + D-2 动作库同一文件）
蓝图  ：docs/project_layout_gear2.md（monitoring/ 段 action_engine.py，#243/#244）

D-1 自动化响应编排（规则引擎）
------------------------------
- 告警→动作规则引擎：条件（告警级别/类别/来源/指标阈值）→ 动作
  （暂停策略/切行情源/撤单/降仓/冻结/通知等，由执行器库提供）
- 三阶段安全模型（评估强制）：
  ① shadow 只读（仅记录建议，不执行任何动作）
  ② confirm（动作先进入待审批队列，人工 approve/reject 后执行）
  ③ auto（安全测试通过后放开，自动执行；每次动作全链路留痕）
- 幂等：action_key = rule_id + 告警去重键 + 动作类型 + 目标，冷却窗口内不重复执行
- 防抖/冷却：cooldown_seconds 内同 key 跳过（status=skipped）
- 回滚方案：每个执行动作记录 rollback_info，可经 rollback() 调 ActionExecutor.undo 还原
- Kill Switch：独立于规则引擎的一键停止（set_kill_switch 置位后 auto/approved
  一律 blocked 并留痕；复位后可继续执行；与规则状态完全解耦）

D-2 自愈动作库
--------------
- 场景动作执行器（register_action_executor 注册，缺注入回调不假成功 KP-M02）：
  - notify            通用通知（经 AlertManager）
  - pause_strategy    暂停策略/引擎（注入 pause_fn/resume_fn，可回滚）
  - switch_data_source 行情中断→切换备用行情源（注入 switch_fn，可回滚）
  - stop_signal_generation 数据质量异常→停止信号生成（注入 stop_fn，可回滚）
  - rollback_data_version 数据质量异常→回滚数据版本（注入 rollback_fn，可回滚）
  - notify_trader     拒单率飙升→通知交易员（经 AlertManager）
- 每个场景可演示：触发 → 自动动作 → 恢复（undo）→ 审计（kind=action_engine）

阈值/模式全部读 settings.yaml monitoring.action_engine（R-04 外置，禁止硬编码）；
attach_engine 装配默认关闭（自动化安全先行，需显式开启）。
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from myquant.core.constants import SeverityLevel
from myquant.core.settings import get_monitoring_config

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  常量（模块顶部，禁止魔法数字）
# ═══════════════════════════════════════════════════════════════

# 三阶段安全模型（评估强制：shadow → confirm → auto）
ACTION_MODES: Tuple[str, ...] = ("shadow", "confirm", "auto")

# 动作执行状态
STATUS_EXECUTED = "executed"
STATUS_SKIPPED = "skipped"
STATUS_PROPOSED = "proposed"           # shadow 只读建议
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_BLOCKED = "blocked"             # Kill Switch 拦截
STATUS_FAILED = "failed"
STATUS_NOT_SUPPORTED = "not_supported" # 缺注入回调，不假成功
STATUS_EXPIRED = "expired"             # 审批超时作废
STATUS_ROLLED_BACK = "rolled_back"

# 审批状态
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_EXPIRED = "expired"

# 默认动作冷却窗口（秒）：同 action_key 在此窗口内不重复执行（防抖）
DEFAULT_RULE_COOLDOWN_SECONDS: float = 300.0
# 默认人工确认超时（秒）：confirm 模式下 pending 审批超时自动作废
DEFAULT_APPROVAL_TIMEOUT_SECONDS: float = 3600.0
# 执行历史留存上限（条）
DEFAULT_HISTORY_LIMIT: int = 500
# 单周期巡检拉取未确认告警上限
DEFAULT_CYCLE_ALERT_LIMIT: int = 100
# 指标名（经 get_metrics_collector().set_gauge 动态注册，不改 metrics_collector.py）
METRIC_EXECUTIONS_TOTAL = "action_engine_executions_total"
METRIC_EXECUTED_TOTAL = "action_engine_executed_total"
METRIC_PROPOSED_TOTAL = "action_engine_proposed_total"
METRIC_BLOCKED_TOTAL = "action_engine_blocked_total"
METRIC_PENDING_APPROVALS = "action_engine_pending_approvals"
ACTION_ENGINE_METRICS: List[str] = [
    METRIC_EXECUTIONS_TOTAL,
    METRIC_EXECUTED_TOTAL,
    METRIC_PROPOSED_TOTAL,
    METRIC_BLOCKED_TOTAL,
    METRIC_PENDING_APPROVALS,
]

# 指标阈值条件操作符
_METRIC_OPS: Tuple[str, ...] = (">", ">=", "<", "<=", "==", "!=")
_VALID_LEVEL_NAMES: Tuple[str, ...] = ("INFO", "WARNING", "ERROR", "CRITICAL")


# ═══════════════════════════════════════════════════════════════
#  数据模型
# ═══════════════════════════════════════════════════════════════

@dataclass
class ActionRule:
    """
    告警→动作规则（条件 → 动作）。

    Attributes
    ----------
    rule_id : str
        规则唯一 ID（注册时校验非空且不重复）。
    name : str
        规则名称（可读）。
    enabled : bool
        是否启用（False 时不参与匹配）。
    mode : str | None
        规则级模式覆盖（shadow/confirm/auto）；None 用引擎全局默认模式。
    condition : dict
        匹配条件结构：{"levels": [...], "categories": [...], "sources": [...],
        "metrics": [{"name": ..., "op": ..., "value": ...}]}；空列表=任意。
    action : dict
        动作结构：{"type": "<已注册动作类型>", "params": {...}}。
    cooldown_seconds : float
        同 action_key 冷却窗口（防抖），默认 300 秒。
    description : str
        规则说明。
    created_at / updated_at : float
        注册/更新时间戳（当前时点，防未来）。
    """

    rule_id: str
    name: str = ""
    enabled: bool = True
    mode: Optional[str] = None
    condition: Dict[str, Any] = field(default_factory=dict)
    action: Dict[str, Any] = field(default_factory=dict)
    cooldown_seconds: float = DEFAULT_RULE_COOLDOWN_SECONDS
    description: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """序列化（JSON 可读；供规则查询/留痕）。"""
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "enabled": self.enabled,
            "mode": self.mode,
            "condition": dict(self.condition),
            "action": dict(self.action),
            "cooldown_seconds": self.cooldown_seconds,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ActionResult:
    """
    一次动作执行的全链路留痕（JSON 可序列化）。

    Attributes
    ----------
    action_id : str
        本次动作唯一 ID（回滚/查询句柄）。
    rule_id : str
        触发规则 ID。
    action_type : str
        动作类型（执行器 action_type）。
    status : str
        执行状态（executed/skipped/proposed/pending_approval/blocked/failed/
        not_supported/expired/rolled_back）。
    executed : bool
        是否真实执行（shadow 建议 / blocked / skipped 为 False）。
    dry_run : bool
        是否影子模式（仅记录不执行）。
    message : str
        结果消息。
    details : dict
        明细（含触发告警摘要/指标快照等）。
    timestamp : float
        记录时刻（当前时点，防未来）。
    action_key : str
        幂等键（rule_id+告警去重键+动作类型+目标）。
    rollback_info : dict | None
        回滚信息（交给执行器 undo；可含 callable，留痕时经 _audit_detail 摘要化）。
    approval : dict | None
        审批信息（confirm 模式：approval_id/status/actor/reason）。
    """

    action_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    rule_id: str = ""
    action_type: str = ""
    status: str = STATUS_EXECUTED
    executed: bool = False
    dry_run: bool = False
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    action_key: str = ""
    rollback_info: Optional[Dict[str, Any]] = None
    approval: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化（rollback_info 摘要化，剔除不可 JSON 序列化对象）。"""
        rb: Optional[Dict[str, Any]] = None
        if self.rollback_info:
            rb = _sanitize_rollback(self.rollback_info)
        return {
            "action_id": self.action_id,
            "rule_id": self.rule_id,
            "action_type": self.action_type,
            "status": self.status,
            "executed": self.executed,
            "dry_run": self.dry_run,
            "message": self.message,
            "details": dict(self.details),
            "timestamp": self.timestamp,
            "action_key": self.action_key,
            "rollback_info": rb,
            "approval": dict(self.approval) if self.approval else None,
        }


@dataclass
class ActionApproval:
    """
    confirm 模式的人工审批单（自动提议 → 人工确认 → 生效）。

    Attributes
    ----------
    approval_id : str
        审批单唯一 ID。
    action_id : str
        关联动作 ID。
    rule_id : str
        触发规则 ID。
    status : str
        pending / approved / rejected / expired。
    proposed_at : float
        提议时刻（当前时点，防未来）。
    decided_at : float | None
        决策时刻（approved/rejected/expired 时写入）。
    actor : str
        决策人（approve/reject 时写入）。
    reason : str
        决策理由。
    """

    approval_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    action_id: str = ""
    rule_id: str = ""
    status: str = APPROVAL_PENDING
    proposed_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    actor: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化（JSON 可读）。"""
        return {
            "approval_id": self.approval_id,
            "action_id": self.action_id,
            "rule_id": self.rule_id,
            "status": self.status,
            "proposed_at": self.proposed_at,
            "decided_at": self.decided_at,
            "actor": self.actor,
            "reason": self.reason,
        }


def _sanitize_rollback(rollback_info: Dict[str, Any]) -> Dict[str, Any]:
    """回滚信息摘要化（剔除 callable 等不可 JSON 序列化对象，留痕安全）。"""
    out: Dict[str, Any] = {}
    for key, value in dict(rollback_info or {}).items():
        if callable(value):
            out[key] = f"<callable:{getattr(value, '__name__', type(value).__name__)}>"
        else:
            out[key] = value
    return out


# ═══════════════════════════════════════════════════════════════
#  动作执行器（ABC + 注册表）
# ═══════════════════════════════════════════════════════════════

class ActionExecutor(ABC):
    """
    动作执行器抽象基类（D-1 通用 + D-2 场景动作库统一接口）。

    execute 返回结构（统一契约）：
        {"status": "executed"|"skipped"|"failed"|"not_supported",
         "message": str, "details": dict, "rollback_info": dict|None}
    dry_run=True 时只做能力预检/记录，不产生任何真实副作用（shadow 演练）。
    缺注入回调返回 not_supported（KP-M02：不假成功）。
    """

    action_type: str = ""

    @abstractmethod
    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """执行动作（详见类 docstring 契约）。"""

    @abstractmethod
    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """回滚动作；rollback_info 为 execute 返回的摘要化回滚信息。"""


_ACTION_EXECUTORS: Dict[str, Type[ActionExecutor]] = {}


def register_action_executor(cls: Type[ActionExecutor]) -> Type[ActionExecutor]:
    """
    注册动作执行器（装饰器；action_type 唯一）。

    Parameters
    ----------
    cls : Type[ActionExecutor]
        执行器类（须有非空 action_type）。

    Returns
    -------
    Type[ActionExecutor] : 原类（装饰器语义）。

    Raises
    ------
    ValueError : action_type 为空或重复时抛出。
    """
    atype = getattr(cls, "action_type", "")
    if not atype:
        raise ValueError(f"动作执行器 {cls.__name__} 缺少 action_type")
    if atype in _ACTION_EXECUTORS:
        raise ValueError(f"动作类型 {atype!r} 已注册（{_ACTION_EXECUTORS[atype].__name__}）")
    _ACTION_EXECUTORS[atype] = cls
    logger.info(f"[action] 执行器已注册: {atype} ({cls.__name__})")
    return cls


def get_action_executor(action_type: str) -> Optional[Type[ActionExecutor]]:
    """按动作类型取执行器类；未注册返回 None。"""
    return _ACTION_EXECUTORS.get(action_type)


def list_action_types() -> List[str]:
    """返回已注册动作类型列表。"""
    return sorted(_ACTION_EXECUTORS.keys())


def _parse_level(level: Any) -> SeverityLevel:
    """
    把级别（SeverityLevel/字符串名）解析为 SeverityLevel。

    Parameters
    ----------
    level : Any
        SeverityLevel 或 "INFO"/"WARNING"/"ERROR"/"CRITICAL"（大小写不敏感）。

    Returns
    -------
    SeverityLevel : 解析后的级别。

    Raises
    ------
    ValueError : 无法解析时抛出。
    """
    if isinstance(level, SeverityLevel):
        return level
    name = str(level or "").strip().upper()
    for member in SeverityLevel:
        if member.name == name:
            return member
    raise ValueError(f"非法告警级别: {level!r}（可选 {_VALID_LEVEL_NAMES}）")


# ── D-1 通用执行器 ─────────────────────────────────────────────

def _dispatch_notification(
    ctx: Dict[str, Any],
    params: Dict[str, Any],
    dry_run: bool,
    default_level: str,
    default_category: str,
    action_label: str,
) -> Dict[str, Any]:
    """
    通用告警投递（Notify/NotifyTrader 共用；通知不可撤销）。

    Parameters
    ----------
    ctx : dict
        执行上下文（alert_manager/rule_id/alert_summary）。
    params : dict
        level/message/category/source/dedup_key/runbook_id/details。
    dry_run : bool
        影子演练（只预检不真实投递）。
    default_level : str
        缺省级别名（如 WARNING/CRITICAL）。
    default_category : str
        缺省类别（如 action_engine/execution）。
    action_label : str
        审计用动作标签（notify / notify_trader）。

    Returns
    -------
    dict : 执行结果（契约同 ActionExecutor.execute）。
    """
    params = dict(params or {})
    try:
        level = _parse_level(params.get("level", default_level))
    except ValueError as e:
        return {
            "status": STATUS_FAILED,
            "message": str(e),
            "details": {"params": params},
            "rollback_info": None,
        }
    manager = ctx.get("alert_manager")
    if manager is None:
        try:
            from myquant.monitoring.alerting import get_alert_manager
            manager = get_alert_manager()
        except Exception as e:  # noqa: BLE001 —— 告警管理器缺失降级 not_supported
            logger.warning("[action] %s 执行器取告警管理器失败: %s", action_label, e)
            return {
                "status": STATUS_NOT_SUPPORTED,
                "message": "告警管理器不可用（通知无法投递）",
                "details": {},
                "rollback_info": None,
            }
    message = str(params.get("message", "自动化响应编排通知"))
    details: Dict[str, Any] = {
        "level": level.name,
        "message": message,
        "category": params.get("category", default_category),
        "dedup_key": params.get("dedup_key", ""),
        "dry_run": dry_run,
    }
    if dry_run:
        return {
            "status": STATUS_EXECUTED,
            "message": "dry-run：通知预检通过（未真实投递）",
            "details": details,
            "rollback_info": None,
        }
    try:
        channels = manager.alert(
            level=level,
            message=message,
            source=str(params.get("source", "monitoring.action_engine")),
            category=str(params.get("category", default_category)),
            details={
                "action_type": action_label,
                "rule_id": ctx.get("rule_id", ""),
                "trigger_alert": ctx.get("alert_summary", ""),
                **(params.get("details") if isinstance(params.get("details"), dict) else {}),
            },
            deduplication_key=str(params.get("dedup_key", "")),
            runbook_id=str(params.get("runbook_id", "")),
        )
        details["channels"] = channels
        return {
            "status": STATUS_EXECUTED,
            "message": f"通知已投递 {channels} 通道",
            "details": details,
            "rollback_info": None,
        }
    except Exception as e:  # noqa: BLE001 —— 投递异常如实报告
        details["error"] = str(e)
        return {
            "status": STATUS_FAILED,
            "message": f"通知投递失败: {e}",
            "details": details,
            "rollback_info": None,
        }


@register_action_executor
class NotifyExecutor(ActionExecutor):
    """
    通用通知动作（经 AlertManager 投递；通知不可撤销）。

    params 支持：level（INFO/WARNING/ERROR/CRITICAL，默认 WARNING）、
    message、category、source、dedup_key、runbook_id、details。
    """

    action_type = "notify"

    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        return _dispatch_notification(
            ctx, params, dry_run,
            default_level="WARNING",
            default_category="action_engine",
            action_label="notify",
        )

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """通知类动作不可撤销（如实返回 False）。"""
        logger.info("[action] notify 动作不可撤销（rollback_info=%s）", rollback_info)
        return False


@register_action_executor
class PauseStrategyExecutor(ActionExecutor):
    """
    暂停策略/引擎动作（可回滚）。

    params 支持：pause_fn（callable，必填；缺失返回 not_supported 不假成功）、
    resume_fn（callable，回滚用；缺失时 undo 返回 False）、target（目标描述）。
    """

    action_type = "pause_strategy"

    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        pause_fn = params.get("pause_fn")
        resume_fn = params.get("resume_fn")
        target = str(params.get("target", "engine"))
        if not callable(pause_fn):
            return {
                "status": STATUS_NOT_SUPPORTED,
                "message": f"未注入 pause_fn（不假成功，KP-M02），目标={target}",
                "details": {"target": target},
                "rollback_info": None,
            }
        rollback_info: Dict[str, Any] = {
            "target": target,
            "has_resume": callable(resume_fn),
        }
        if callable(resume_fn):
            rollback_info["resume_fn"] = resume_fn
        if dry_run:
            return {
                "status": STATUS_EXECUTED,
                "message": f"dry-run：将暂停 {target}（未真实执行）",
                "details": {"target": target, "dry_run": True},
                "rollback_info": rollback_info,
            }
        try:
            pause_fn()
            return {
                "status": STATUS_EXECUTED,
                "message": f"已暂停 {target}",
                "details": {"target": target},
                "rollback_info": rollback_info,
            }
        except Exception as e:  # noqa: BLE001 —— 暂停异常如实报告
            return {
                "status": STATUS_FAILED,
                "message": f"暂停 {target} 失败: {e}",
                "details": {"target": target, "error": str(e)},
                "rollback_info": rollback_info,
            }

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """回滚：调用注入的 resume_fn 恢复；未注入返回 False（不假成功）。"""
        resume_fn = (rollback_info or {}).get("resume_fn")
        target = (rollback_info or {}).get("target", "?")
        if not callable(resume_fn):
            logger.warning("[action] 暂停动作回滚失败：未注入 resume_fn（目标=%s）", target)
            return False
        try:
            resume_fn()
            logger.info("[action] 已恢复 %s（回滚完成）", target)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[action] 回滚恢复 %s 失败: %s", target, e)
            return False


# ═══════════════════════════════════════════════════════════════
#  D-2 自愈动作库（场景动作执行器）
# ═══════════════════════════════════════════════════════════════
# 场景矩阵（回写#244）：
#   - 行情中断（market_staleness）   → switch_data_source（切备用行情源，可回滚）
#                                     / pause_strategy（暂停相关策略，可回滚）
#   - 拒单率飙升（execution/reject）  → pause_strategy（暂停策略，可回滚）
#                                     / notify_trader（通知交易员，不可撤销）
#   - 数据质量异常（data_quality）    → stop_signal_generation（停止信号生成，可回滚）
#                                     / rollback_data_version（回滚数据版本，可回滚）
# 每个动作先 shadow 演练（dry_run 预检），再逐步放开（confirm→auto）；
# 缺注入回调返回 not_supported（KP-M02 不假成功）；执行留痕 + undo 恢复 + 审计。

@register_action_executor
class SwitchDataSourceExecutor(ActionExecutor):
    """
    行情中断→切换备用行情源（可回滚）。

    params 支持：switch_fn（callable，必填，切到备用源）、restore_fn
    （callable，回滚回主源）、target_source（当前源名）、backup_source
    （备用源名）、target（策略/引擎目标）。
    """

    action_type = "switch_data_source"

    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        switch_fn = params.get("switch_fn")
        restore_fn = params.get("restore_fn")
        target_source = str(params.get("target_source", "?"))

        def _base_rollback() -> Dict[str, Any]:
            rb: Dict[str, Any] = {
                "target_source": target_source,
                "backup_source": str(params.get("backup_source", "?")),
                "has_restore": callable(restore_fn),
            }
            if callable(restore_fn):
                rb["restore_fn"] = restore_fn
            return rb

        if not callable(switch_fn):
            return {
                "status": STATUS_NOT_SUPPORTED,
                "message": f"未注入 switch_fn（不假成功，KP-M02），源={target_source}",
                "details": {"target_source": target_source},
                "rollback_info": None,
            }
        rollback_info = _base_rollback()
        if dry_run:
            return {
                "status": STATUS_EXECUTED,
                "message": f"dry-run：将切行情源 {target_source} → "
                           f"{params.get('backup_source', '?')}（未真实执行）",
                "details": {"target_source": target_source, "dry_run": True},
                "rollback_info": rollback_info,
            }
        try:
            switch_fn()
            return {
                "status": STATUS_EXECUTED,
                "message": f"已切换行情源 → {params.get('backup_source', '?')}",
                "details": {"target_source": target_source,
                            "backup_source": params.get("backup_source", "?")},
                "rollback_info": rollback_info,
            }
        except Exception as e:  # noqa: BLE001 —— 切换异常如实报告
            return {
                "status": STATUS_FAILED,
                "message": f"切换行情源失败: {e}",
                "details": {"target_source": target_source, "error": str(e)},
                "rollback_info": rollback_info,
            }

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """回滚：调用注入的 restore_fn 切回主源；未注入返回 False。"""
        restore_fn = (rollback_info or {}).get("restore_fn")
        target_source = (rollback_info or {}).get("target_source", "?")
        if not callable(restore_fn):
            logger.warning(
                "[action] 切源回滚失败：未注入 restore_fn（源=%s）", target_source
            )
            return False
        try:
            restore_fn()
            logger.info("[action] 行情源已切回 %s（回滚完成）", target_source)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[action] 切源回滚 %s 失败: %s", target_source, e)
            return False


@register_action_executor
class StopSignalGenerationExecutor(ActionExecutor):
    """
    数据质量异常→停止信号生成（可回滚）。

    params 支持：stop_fn（callable，必填，关闭信号闸门）、resume_fn
    （callable，回滚重开闸门）、target（策略/引擎目标）。
    """

    action_type = "stop_signal_generation"

    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        stop_fn = params.get("stop_fn")
        resume_fn = params.get("resume_fn")
        target = str(params.get("target", "engine"))
        if not callable(stop_fn):
            return {
                "status": STATUS_NOT_SUPPORTED,
                "message": f"未注入 stop_fn（不假成功，KP-M02），目标={target}",
                "details": {"target": target},
                "rollback_info": None,
            }
        rollback_info: Dict[str, Any] = {"target": target, "has_resume": callable(resume_fn)}
        if callable(resume_fn):
            rollback_info["resume_fn"] = resume_fn
        if dry_run:
            return {
                "status": STATUS_EXECUTED,
                "message": f"dry-run：将停止 {target} 信号生成（未真实执行）",
                "details": {"target": target, "dry_run": True},
                "rollback_info": rollback_info,
            }
        try:
            stop_fn()
            return {
                "status": STATUS_EXECUTED,
                "message": f"已停止 {target} 信号生成",
                "details": {"target": target},
                "rollback_info": rollback_info,
            }
        except Exception as e:  # noqa: BLE001 —— 停止异常如实报告
            return {
                "status": STATUS_FAILED,
                "message": f"停止 {target} 信号生成失败: {e}",
                "details": {"target": target, "error": str(e)},
                "rollback_info": rollback_info,
            }

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """回滚：调用注入的 resume_fn 重开信号闸门；未注入返回 False。"""
        resume_fn = (rollback_info or {}).get("resume_fn")
        target = (rollback_info or {}).get("target", "?")
        if not callable(resume_fn):
            logger.warning("[action] 信号闸门回滚失败：未注入 resume_fn（目标=%s）", target)
            return False
        try:
            resume_fn()
            logger.info("[action] %s 信号生成已恢复（回滚完成）", target)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[action] 信号闸门回滚 %s 失败: %s", target, e)
            return False


@register_action_executor
class RollbackDataVersionExecutor(ActionExecutor):
    """
    数据质量异常→回滚数据版本（可回滚）。

    params 支持：rollback_fn（callable，必填，回滚到历史数据版本）、
    restore_fn（callable，恢复原版本）、current_version、target_version、
    target（数据集/模块）。
    """

    action_type = "rollback_data_version"

    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        rollback_fn = params.get("rollback_fn")
        restore_fn = params.get("restore_fn")
        current_version = str(params.get("current_version", "?"))
        target_version = str(params.get("target_version", "?"))
        target = str(params.get("target", "data"))
        if not callable(rollback_fn):
            return {
                "status": STATUS_NOT_SUPPORTED,
                "message": f"未注入 rollback_fn（不假成功，KP-M02），目标={target}",
                "details": {"target": target, "current_version": current_version},
                "rollback_info": None,
            }
        rollback_info: Dict[str, Any] = {
            "target": target,
            "current_version": current_version,
            "target_version": target_version,
            "has_restore": callable(restore_fn),
        }
        if callable(restore_fn):
            rollback_info["restore_fn"] = restore_fn
        if dry_run:
            return {
                "status": STATUS_EXECUTED,
                "message": f"dry-run：将回滚 {target} 数据 {current_version} → "
                           f"{target_version}（未真实执行）",
                "details": {"target": target, "dry_run": True},
                "rollback_info": rollback_info,
            }
        try:
            rollback_fn()
            return {
                "status": STATUS_EXECUTED,
                "message": f"已回滚 {target} 数据版本 → {target_version}",
                "details": {"target": target, "current_version": current_version,
                            "target_version": target_version},
                "rollback_info": rollback_info,
            }
        except Exception as e:  # noqa: BLE001 —— 回滚异常如实报告
            return {
                "status": STATUS_FAILED,
                "message": f"回滚 {target} 数据版本失败: {e}",
                "details": {"target": target, "error": str(e)},
                "rollback_info": rollback_info,
            }

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """回滚：调用注入的 restore_fn 恢复原版本；未注入返回 False。"""
        restore_fn = (rollback_info or {}).get("restore_fn")
        target = (rollback_info or {}).get("target", "?")
        if not callable(restore_fn):
            logger.warning("[action] 数据版本回滚失败：未注入 restore_fn（目标=%s）", target)
            return False
        try:
            restore_fn()
            logger.info("[action] %s 数据版本已恢复（回滚完成）", target)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[action] 数据版本恢复 %s 失败: %s", target, e)
            return False


@register_action_executor
class NotifyTraderExecutor(ActionExecutor):
    """
    拒单率飙升→通知交易员（经 AlertManager 投递 CRITICAL 执行类告警；不可撤销）。

    params 支持：level（默认 CRITICAL）、message、category（默认 execution）、
    source、dedup_key、runbook_id、details（可附拒单率/拒单明细）。
    """

    action_type = "notify_trader"

    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        return _dispatch_notification(
            ctx, params, dry_run,
            default_level="CRITICAL",
            default_category="execution",
            action_label="notify_trader",
        )

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """通知交易员动作不可撤销（如实返回 False）。"""
        logger.info("[action] notify_trader 动作不可撤销（rollback_info=%s）", rollback_info)
        return False


# ═══════════════════════════════════════════════════════════════
#  D-4 风险预算再分配执行器（回写#246）
# ═══════════════════════════════════════════════════════════════
# 业务类 RiskBudgetReallocator 在 myquant/allocation/risk_parity.py；
# 本执行器提供动作引擎接线（注入 reallocate_fn 回调，缺注入不假成功 KP-M02）。

@register_action_executor
class ReallocateRiskBudgetExecutor(ActionExecutor):
    """
    基于监控反馈的多策略风险预算再分配（可回滚）。

    params 支持：reallocate_fn（callable，必填，执行再分配——调用方注入
    RiskBudgetReallocator 接线）、rollback_fn（callable，回滚恢复旧权重）、
    weights（当前权重 {strategy: weight}）、feedback（监控反馈快照）、
    reason（再分配原因）。
    """

    action_type = "reallocate_risk"

    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        reallocate_fn = params.get("reallocate_fn")
        rollback_fn = params.get("rollback_fn")
        weights = params.get("weights")
        feedback = params.get("feedback")
        reason = str(params.get("reason", ""))
        if not callable(reallocate_fn):
            return {
                "status": STATUS_NOT_SUPPORTED,
                "message": "未注入 reallocate_fn（不假成功，KP-M02）",
                "details": {"reason": reason},
                "rollback_info": None,
            }
        rollback_info: Dict[str, Any] = {
            "has_rollback": callable(rollback_fn),
            "reason": reason,
        }
        if callable(rollback_fn):
            rollback_info["rollback_fn"] = rollback_fn
        if dry_run:
            return {
                "status": STATUS_EXECUTED,
                "message": "dry-run：将按监控反馈再分配风险预算（未真实执行）",
                "details": {"dry_run": True, "reason": reason},
                "rollback_info": rollback_info,
            }
        try:
            result = reallocate_fn(weights=weights, feedback=feedback, reason=reason)
            return {
                "status": STATUS_EXECUTED,
                "message": "风险预算再分配已执行",
                "details": {"reason": reason, "result": result or {}},
                "rollback_info": rollback_info,
            }
        except Exception as e:  # noqa: BLE001 —— 再分配异常如实报告
            return {
                "status": STATUS_FAILED,
                "message": f"风险预算再分配失败: {e}",
                "details": {"reason": reason, "error": str(e)},
                "rollback_info": rollback_info,
            }

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """回滚：调用注入的 rollback_fn 恢复旧权重；未注入返回 False。"""
        rollback_fn = (rollback_info or {}).get("rollback_fn")
        if not callable(rollback_fn):
            logger.warning("[action] 风险预算再分配回滚失败：未注入 rollback_fn")
            return False
        try:
            rollback_fn()
            logger.info("[action] 风险预算已恢复（回滚完成）")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[action] 风险预算回滚失败: %s", e)
            return False


# ═══════════════════════════════════════════════════════════════
#  D-5 参数动态调整执行器（回写#247）
# ═══════════════════════════════════════════════════════════════
# 业务类 ParamTuner 在 myquant/strategies/param_tuner.py；
# 本执行器提供动作引擎接线（注入 apply_fn/rollback_fn 回调，缺注入不假成功 KP-M02）。

@register_action_executor
class ApplyParamChangeExecutor(ActionExecutor):
    """
    策略参数动态调整（带审批流，可回滚）。

    params 支持：apply_fn（callable，必填，生效回调——调用方注入 ParamTuner
    接线）、rollback_fn（callable，回滚恢复旧参数）、strategy_name（目标策略）、
    param_changes（参数变更 {param: value}）、reason（调整原因）。
    """

    action_type = "apply_param_change"

    def execute(
        self,
        ctx: Dict[str, Any],
        params: Dict[str, Any],
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        params = dict(params or {})
        apply_fn = params.get("apply_fn")
        rollback_fn = params.get("rollback_fn")
        strategy_name = str(params.get("strategy_name", ""))
        param_changes = params.get("param_changes")
        reason = str(params.get("reason", ""))
        if not callable(apply_fn):
            return {
                "status": STATUS_NOT_SUPPORTED,
                "message": "未注入 apply_fn（不假成功，KP-M02）",
                "details": {"strategy_name": strategy_name, "reason": reason},
                "rollback_info": None,
            }
        rollback_info: Dict[str, Any] = {
            "has_rollback": callable(rollback_fn),
            "strategy_name": strategy_name,
            "reason": reason,
        }
        if callable(rollback_fn):
            rollback_info["rollback_fn"] = rollback_fn
        if dry_run:
            return {
                "status": STATUS_EXECUTED,
                "message": "dry-run：将按提议调整策略参数（未真实执行）",
                "details": {"dry_run": True, "strategy_name": strategy_name,
                            "param_changes": param_changes, "reason": reason},
                "rollback_info": rollback_info,
            }
        try:
            result = apply_fn({
                "strategy_name": strategy_name,
                "param_changes": dict(param_changes or {}),
                "reason": reason,
            })
            return {
                "status": STATUS_EXECUTED,
                "message": "策略参数调整已执行",
                "details": {"strategy_name": strategy_name,
                            "param_changes": param_changes, "reason": reason,
                            "result": result or {}},
                "rollback_info": rollback_info,
            }
        except Exception as e:  # noqa: BLE001 —— 调整异常如实报告
            return {
                "status": STATUS_FAILED,
                "message": f"策略参数调整失败: {e}",
                "details": {"strategy_name": strategy_name, "reason": reason,
                            "error": str(e)},
                "rollback_info": rollback_info,
            }

    def undo(self, rollback_info: Dict[str, Any]) -> bool:
        """回滚：调用注入的 rollback_fn 恢复旧参数；未注入返回 False。"""
        rollback_fn = (rollback_info or {}).get("rollback_fn")
        if not callable(rollback_fn):
            logger.warning("[action] 参数调整回滚失败：未注入 rollback_fn")
            return False
        try:
            rollback_fn()
            logger.info("[action] 策略参数已恢复（回滚完成）")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[action] 参数调整回滚失败: %s", e)
            return False


# ═══════════════════════════════════════════════════════════════
#  规则引擎（D-1）
# ═══════════════════════════════════════════════════════════════

class ActionEngine:
    """
    自动化响应编排规则引擎（D-1）。

    用法:
        from myquant.monitoring.action_engine import ActionEngine, ActionRule

        engine = ActionEngine(mode="shadow")            # 三阶段安全模型缺省 shadow
        engine.register_rule(ActionRule(rule_id="r1", condition={...}, action={...}))
        results = engine.handle_alert(alert.to_dict())   # 事件入口
        summary = engine.run_cycle()                     # 周期巡检（未确认告警）

    安全模型（评估强制）：
        shadow 只记录建议；confirm 需人工 approve；auto 直接执行；
        Kill Switch 置位后任何执行一律 blocked。
    """

    def __init__(
        self,
        mode: str = "shadow",
        cooldown_seconds: float = DEFAULT_RULE_COOLDOWN_SECONDS,
        approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        audit_log: Any = None,
        alert_manager: Any = None,
        clock: Optional[Callable[[], float]] = None,
        max_history: int = DEFAULT_HISTORY_LIMIT,
        rules: Optional[List[ActionRule]] = None,
    ) -> None:
        """
        Parameters
        ----------
        mode : str
            全局默认安全模式（shadow/confirm/auto）。
        cooldown_seconds : float
            同 action_key 冷却窗口（防抖，>0）。
        approval_timeout_seconds : float
            人工确认超时（>0；confirm 模式 pending 超时自动作废）。
        audit_log : AuditLogStore | None
            审计存储（BASE-7）；None 时惰性尝试 AuditLogStore()，
            失败降级为仅内存执行历史留痕（不阻断动作）。
        alert_manager : AlertManager | None
            告警管理器；None 时惰性取 get_alert_manager()（run_cycle 拉取告警用）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        max_history : int
            执行历史留存上限（正整数）。
        rules : List[ActionRule] | None
            预注册规则列表。
        """
        if mode not in ACTION_MODES:
            raise ValueError(f"mode 必须为 {ACTION_MODES} 之一，收到 {mode!r}")
        try:
            cd = float(cooldown_seconds)
            at = float(approval_timeout_seconds)
        except (TypeError, ValueError):
            raise ValueError("cooldown_seconds/approval_timeout_seconds 必须为数值") from None
        if cd <= 0:
            raise ValueError(f"cooldown_seconds 必须 >0，收到 {cooldown_seconds!r}")
        if at <= 0:
            raise ValueError(f"approval_timeout_seconds 必须 >0，收到 {approval_timeout_seconds!r}")
        if not isinstance(max_history, int) or max_history <= 0:
            raise ValueError(f"max_history 必须为正整数，收到 {max_history!r}")

        self._mode: str = mode
        self._cooldown_seconds: float = cd
        self._approval_timeout_seconds: float = at
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._max_history: int = int(max_history)
        self._audit_log: Any = audit_log
        self._audit_tried: bool = audit_log is not None
        self._alert_manager: Any = alert_manager

        # 规则注册表
        self._rules: Dict[str, ActionRule] = {}
        # Kill Switch（独立于规则引擎）
        self._kill_switch: bool = False
        self._kill_switch_history: List[Dict[str, Any]] = []
        # 模式切换留痕（三阶段逐步放开）
        self._mode_history: List[Dict[str, Any]] = [
            {"time": self._clock(), "old": None, "new": mode, "actor": "system", "reason": "初始化"}
        ]
        # 执行全链路留痕（JSON 可序列化；上限裁剪）
        self._executions: List[ActionResult] = []
        # 冷却表：action_key → 最近执行时刻
        self._last_execution_ts: Dict[str, float] = {}
        # confirm 模式待审批表：approval_id → ActionApproval
        self._pending_approvals: Dict[str, ActionApproval] = {}

        if rules:
            for rule in rules:
                self.register_rule(rule)

        logger.debug(
            "ActionEngine 初始化: mode=%s cooldown=%.0fs approval_timeout=%.0fs "
            "max_history=%d",
            self._mode, self._cooldown_seconds, self._approval_timeout_seconds,
            self._max_history,
        )

    # ── 规则管理 ───────────────────────────────────────────────

    @staticmethod
    def _validate_condition(condition: Dict[str, Any]) -> None:
        """校验规则条件结构；非法抛 ValueError（防御式编程）。"""
        if not isinstance(condition, dict):
            raise ValueError(f"规则条件必须为 dict，收到 {type(condition).__name__}")
        levels = condition.get("levels")
        if levels is not None and not isinstance(levels, list):
            raise ValueError("condition.levels 必须为 list（空=任意级别）")
        for lv in levels or []:
            if str(lv).upper() not in _VALID_LEVEL_NAMES:
                raise ValueError(
                    f"condition.levels 含非法级别 {lv!r}（可选 {_VALID_LEVEL_NAMES}）"
                )
        categories = condition.get("categories")
        if categories is not None and not isinstance(categories, list):
            raise ValueError("condition.categories 必须为 list（空=任意类别）")
        sources = condition.get("sources")
        if sources is not None and not isinstance(sources, list):
            raise ValueError("condition.sources 必须为 list（空=任意来源）")
        metrics = condition.get("metrics")
        if metrics is not None and not isinstance(metrics, list):
            raise ValueError("condition.metrics 必须为 list（空=无指标门限）")
        for i, m in enumerate(metrics or []):
            if not isinstance(m, dict):
                raise ValueError(f"condition.metrics[{i}] 必须为 dict")
            name = m.get("name")
            if not name:
                raise ValueError(f"condition.metrics[{i}] 缺少 name")
            op = str(m.get("op", "")).strip()
            if op not in _METRIC_OPS:
                raise ValueError(f"condition.metrics[{i}].op 非法: {op!r}（可选 {_METRIC_OPS}）")
            try:
                float(m.get("value"))
            except (TypeError, ValueError):
                raise ValueError(f"condition.metrics[{i}].value 必须为数值") from None

    def register_rule(self, rule: ActionRule) -> str:
        """
        注册一条规则（规则 ID 唯一；动作类型须已注册）。

        Parameters
        ----------
        rule : ActionRule
            规则对象。

        Returns
        -------
        str : rule_id。

        Raises
        ------
        ValueError : rule_id 空/重复、mode 非法、条件/动作非法时抛出。
        """
        if not isinstance(rule, ActionRule):
            raise ValueError(f"rule 必须为 ActionRule，收到 {type(rule).__name__}")
        rule_id = (rule.rule_id or "").strip()
        if not rule_id:
            raise ValueError("规则 rule_id 不能为空")
        if rule_id in self._rules:
            raise ValueError(f"规则 {rule_id!r} 已存在")
        if rule.mode is not None and rule.mode not in ACTION_MODES:
            raise ValueError(f"规则 {rule_id} mode 非法: {rule.mode!r}")
        self._validate_condition(rule.condition)
        action = rule.action
        if not isinstance(action, dict) or not action.get("type"):
            raise ValueError(f"规则 {rule_id} 缺少 action.type")
        action_type = str(action["type"]).strip()
        if get_action_executor(action_type) is None:
            raise ValueError(
                f"规则 {rule_id} 动作类型 {action_type!r} 未注册"
                f"（已注册: {list_action_types()}）"
            )
        params = action.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError(f"规则 {rule_id} action.params 必须为 dict")
        try:
            cd = float(rule.cooldown_seconds)
        except (TypeError, ValueError):
            raise ValueError(f"规则 {rule_id} cooldown_seconds 必须为数值") from None
        if cd <= 0:
            raise ValueError(f"规则 {rule_id} cooldown_seconds 必须 >0")
        rule.cooldown_seconds = cd
        rule.created_at = self._clock()
        rule.updated_at = rule.created_at
        self._rules[rule_id] = rule
        self._audit(
            "rule_register", "register", "system",
            {"rule_id": rule_id, "name": rule.name, "mode": rule.mode,
             "action_type": action_type},
        )
        logger.info("[action] 规则已注册: %s（%s）", rule_id, rule.name or action_type)
        return rule_id

    def remove_rule(self, rule_id: str) -> bool:
        """移除规则（不存在返回 False）。"""
        rule_id = (rule_id or "").strip()
        if rule_id not in self._rules:
            return False
        del self._rules[rule_id]
        self._audit("rule_register", "remove", "system", {"rule_id": rule_id})
        logger.info("[action] 规则已移除: %s", rule_id)
        return True

    def get_rules(self) -> List[ActionRule]:
        """返回全部规则（注册序）。"""
        return list(self._rules.values())

    def get_rule(self, rule_id: str) -> Optional[ActionRule]:
        """按 ID 取规则。"""
        return self._rules.get((rule_id or "").strip())

    def load_rules(self, rules_cfg: List[Dict[str, Any]]) -> int:
        """
        从配置字典列表批量注册规则（监控即代码 P0-6）。

        Parameters
        ----------
        rules_cfg : List[dict]
            规则配置列表；每条经 rule_from_dict 归一化。

        Returns
        -------
        int : 成功注册条数。

        Raises
        ------
        ValueError : rules_cfg 非 list 或单条非法（含非法动作类型）时抛出。
        """
        if not isinstance(rules_cfg, list):
            raise ValueError(f"rules_cfg 必须为 list，收到 {type(rules_cfg).__name__}")
        count = 0
        for data in rules_cfg:
            rule = self.rule_from_dict(data)
            self.register_rule(rule)
            count += 1
        logger.info("[action] 批量注册规则 %d 条", count)
        return count

    @staticmethod
    def rule_from_dict(data: Dict[str, Any]) -> ActionRule:
        """
        规则字典 → ActionRule（缺省默认值；非法字段防御式校验）。

        Parameters
        ----------
        data : dict
            {"rule_id"/"id", "name", "enabled", "mode", "condition", "action",
             "cooldown_seconds", "description"}。

        Returns
        -------
        ActionRule : 归一化后的规则对象。

        Raises
        ------
        ValueError : 关键字段缺失/非法时抛出。
        """
        data = dict(data or {})
        rule_id = str(data.get("rule_id") or data.get("id") or "").strip()
        if not rule_id:
            raise ValueError("规则配置缺少 rule_id/id")
        action = data.get("action")
        if not isinstance(action, dict) or not str(action.get("type", "")).strip():
            raise ValueError(f"规则 {rule_id} 缺少 action.type")
        mode = data.get("mode")
        if mode is not None and str(mode) not in ACTION_MODES:
            logger.warning("规则 %s mode 非法 %r，回退 None（跟随引擎默认）", rule_id, mode)
            mode = None
        cooldown = DEFAULT_RULE_COOLDOWN_SECONDS
        raw_cd = data.get("cooldown_seconds", DEFAULT_RULE_COOLDOWN_SECONDS)
        try:
            cd_f = float(raw_cd)
            if cd_f > 0:
                cooldown = cd_f
            else:
                logger.warning("规则 %s cooldown_seconds 非法 %r，回退默认", rule_id, raw_cd)
        except (TypeError, ValueError):
            logger.warning("规则 %s cooldown_seconds 非法 %r，回退默认", rule_id, raw_cd)
        return ActionRule(
            rule_id=rule_id,
            name=str(data.get("name", "")),
            enabled=bool(data.get("enabled", True)),
            mode=str(mode) if mode is not None else None,
            condition=dict(data.get("condition", {}) or {}),
            action=dict(action),
            cooldown_seconds=cooldown,
            description=str(data.get("description", "")),
        )

    # ── 三阶段安全模型：模式切换 + Kill Switch ─────────────────

    def set_mode(self, mode: str, actor: str = "system", reason: str = "") -> str:
        """
        切换全局安全模式（三阶段逐步放开；切换留痕 + 审计）。

        Parameters
        ----------
        mode : str
            shadow/confirm/auto。
        actor : str
            操作者。
        reason : str
            切换理由。

        Returns
        -------
        str : 新模式。

        Raises
        ------
        ValueError : mode 非法或 actor 为空时抛出。
        """
        if mode not in ACTION_MODES:
            raise ValueError(f"mode 必须为 {ACTION_MODES} 之一，收到 {mode!r}")
        if not (actor or "").strip():
            raise ValueError("set_mode actor 不能为空")
        old = self._mode
        self._mode = mode
        rec = {
            "time": self._clock(), "old": old, "new": mode,
            "actor": actor, "reason": reason,
        }
        self._mode_history.append(rec)
        self._audit("mode", "change", actor, {"old": old, "new": mode, "reason": reason})
        logger.info("[action] 安全模式切换: %s → %s（%s）", old, mode, actor)
        return mode

    def mode_history(self) -> List[Dict[str, Any]]:
        """返回模式切换留痕（含初始化记录）。"""
        return list(self._mode_history)

    @property
    def mode(self) -> str:
        """当前全局安全模式。"""
        return self._mode

    def set_kill_switch(self, active: bool, actor: str = "system", reason: str = "") -> bool:
        """
        Kill Switch（独立于规则引擎的一键停止）。

        置位后：auto 直接执行与 confirm 审批通过均被拦截（status=blocked），
        规则引擎仍可评估（只读），但任何真实动作一律拒绝执行并留痕；
        复位后恢复。切换本身同样审计留痕。

        Parameters
        ----------
        active : bool
            置位/复位。
        actor : str
            操作者（非空）。
        reason : str
            原因。

        Returns
        -------
        bool : 当前 Kill Switch 状态。
        """
        if not (actor or "").strip():
            raise ValueError("set_kill_switch actor 不能为空")
        active_b = bool(active)
        self._kill_switch = active_b
        self._kill_switch_history.append({
            "time": self._clock(), "active": active_b, "actor": actor, "reason": reason,
        })
        self._audit(
            "kill_switch", "enable" if active_b else "disable", actor,
            {"active": active_b, "reason": reason},
        )
        logger.warning(
            "[action] Kill Switch %s（%s: %s）",
            "置位" if active_b else "复位", actor, reason or "无原因",
        )
        return self._kill_switch

    @property
    def kill_switch(self) -> bool:
        """Kill Switch 当前状态（True=所有动作被拦截）。"""
        return self._kill_switch

    def kill_switch_history(self) -> List[Dict[str, Any]]:
        """返回 Kill Switch 操作留痕。"""
        return list(self._kill_switch_history)

    # ── 条件匹配 ───────────────────────────────────────────────

    @staticmethod
    def _normalize_alert(alert: Any) -> Dict[str, Any]:
        """
        把告警（Alert 或 dict）归一化为字典（级别转大写名）。

        Parameters
        ----------
        alert : Any
            Alert 对象（有 to_dict）或 dict。

        Returns
        -------
        dict : 归一化告警（level/category/source/deduplication_key 等键齐全）。

        Raises
        ------
        TypeError : 无法识别时抛出。
        """
        if hasattr(alert, "to_dict"):
            data = dict(alert.to_dict())
        elif isinstance(alert, dict):
            data = dict(alert)
        else:
            raise TypeError(f"告警必须为 Alert/字典，收到 {type(alert).__name__}")
        level = data.get("level")
        if not isinstance(level, str):
            level = getattr(level, "value", "info")
        data["level"] = str(level).upper()
        data.setdefault("category", "system")
        data.setdefault("source", "monitoring")
        data.setdefault("deduplication_key", "")
        data.setdefault("timestamp", time.time())
        return data

    @staticmethod
    def _metric_op_satisfied(value: float, op: str, threshold: float) -> bool:
        """指标阈值比较（单值 vs 阈值）。"""
        if op == ">":
            return value > threshold
        if op == ">=":
            return value >= threshold
        if op == "<":
            return value < threshold
        if op == "<=":
            return value <= threshold
        if op == "==":
            return abs(value - threshold) < 1e-9
        if op == "!=":
            return abs(value - threshold) >= 1e-9
        return False

    def _match_rule(
        self,
        rule: ActionRule,
        alert: Optional[Dict[str, Any]],
        metrics: Dict[str, float],
    ) -> bool:
        """
        规则条件匹配（级别/类别/来源为 OR 内任一命中；指标为 AND 全满足）。

        指标缺失时 fail-closed（不触发，宁可不动作不误动作）；
        规则带级别/类别/来源约束但无告警上下文时同样不触发。
        """
        condition = rule.condition or {}
        levels = [str(lv).upper() for lv in (condition.get("levels") or [])]
        categories = [str(c) for c in (condition.get("categories") or [])]
        sources = [str(s) for s in (condition.get("sources") or [])]
        metric_gates = condition.get("metrics") or []

        if alert is None:
            if levels or categories or sources:
                return False
        else:
            if levels and alert.get("level") not in levels:
                return False
            if categories and alert.get("category") not in categories:
                return False
            if sources:
                src = str(alert.get("source", ""))
                if not any(src == s or src.startswith(s) for s in sources):
                    return False
        # 指标门限（AND）：任一项不满足或指标缺失 → 不触发
        for gate in metric_gates:
            name = str(gate.get("name", ""))
            if name not in metrics:
                logger.debug("[action] 规则 %s 指标 %s 缺失，fail-closed 不触发", rule.rule_id, name)
                return False
            try:
                cur = float(metrics[name])
            except (TypeError, ValueError):
                return False
            try:
                threshold = float(gate.get("value"))
            except (TypeError, ValueError):
                return False
            if not self._metric_op_satisfied(cur, str(gate.get("op", ">")), threshold):
                return False
        return True

    # ── 执行管线 ───────────────────────────────────────────────

    def _action_key(self, rule: ActionRule, alert: Optional[Dict[str, Any]]) -> str:
        """幂等键：rule_id + 告警去重键 + 动作类型 + 目标。"""
        dedup = (alert or {}).get("deduplication_key") or (alert or {}).get("alert_id") or ""
        target = str((rule.action.get("params") or {}).get("target", ""))
        return f"{rule.rule_id}:{dedup}:{rule.action.get('type', '')}:{target}"

    def _resolve_mode(self, rule: ActionRule) -> str:
        """规则级模式覆盖 > 引擎全局模式。"""
        return rule.mode if rule.mode is not None else self._mode

    def _record(self, result: ActionResult) -> ActionResult:
        """执行历史留痕（上限裁剪最旧）。"""
        self._executions.append(result)
        if len(self._executions) > self._max_history:
            self._executions = self._executions[-self._max_history:]
        return result

    def _execute_via_executor(
        self,
        rule: ActionRule,
        ctx: Dict[str, Any],
        dry_run: bool = False,
    ) -> ActionResult:
        """经执行器执行动作（auto/confirm 通过后调用）。"""
        action_type = str(rule.action.get("type", ""))
        executor_cls = get_action_executor(action_type)
        result = ActionResult(
            rule_id=rule.rule_id,
            action_type=action_type,
            dry_run=dry_run,
            timestamp=self._clock(),
        )
        if executor_cls is None:
            result.status = STATUS_NOT_SUPPORTED
            result.message = f"动作类型 {action_type!r} 未注册"
            result.details = {"rule_id": rule.rule_id}
            self._audit(
                "execute", "not_supported", "action_engine",
                result.to_dict(),
            )
            return self._record(result)
        try:
            executor = executor_cls()
            outcome = executor.execute(
                ctx=ctx,
                params=dict(rule.action.get("params") or {}),
                dry_run=dry_run,
            )
        except Exception as e:  # noqa: BLE001 —— 执行器异常如实报告
            result.status = STATUS_FAILED
            result.message = f"执行器异常: {e}"
            result.details = {"rule_id": rule.rule_id, "error": str(e)}
            self._audit("execute", "failed", "action_engine", result.to_dict())
            return self._record(result)
        status = str(outcome.get("status", STATUS_FAILED))
        result.status = status
        result.executed = status == STATUS_EXECUTED and not dry_run
        result.message = str(outcome.get("message", ""))
        result.details = dict(outcome.get("details") or {})
        result.details.setdefault("rule_id", rule.rule_id)
        result.rollback_info = outcome.get("rollback_info")
        if status in (STATUS_EXECUTED, STATUS_SKIPPED):
            self._audit("execute", "executed" if result.executed else "skipped",
                        "action_engine", result.to_dict())
        else:
            self._audit("execute", status, "action_engine", result.to_dict())
        return self._record(result)

    def _execute_rule(
        self,
        rule: ActionRule,
        alert: Optional[Dict[str, Any]],
        metrics: Dict[str, float],
    ) -> ActionResult:
        """单条规则执行管线（shadow/confirm/auto + 冷却 + Kill Switch）。"""
        action_key = self._action_key(rule, alert)
        result = ActionResult(
            rule_id=rule.rule_id,
            action_type=str(rule.action.get("type", "")),
            action_key=action_key,
            timestamp=self._clock(),
        )
        result.details = {
            "trigger_alert": (alert or {}).get("message", "")[:120]
            if alert else "",
            "alert_level": (alert or {}).get("level", "") if alert else "",
            "alert_category": (alert or {}).get("category", "") if alert else "",
        }
        # 冷却防抖：同 key 在窗口内不重复执行
        last_ts = self._last_execution_ts.get(action_key)
        if last_ts is not None and (self._clock() - last_ts) < rule.cooldown_seconds:
            result.status = STATUS_SKIPPED
            result.message = f"冷却窗口内（{rule.cooldown_seconds:.0f}s）同 key 跳过"
            result.details["cooldown_remaining"] = (
                rule.cooldown_seconds - (self._clock() - last_ts)
            )
            return self._record(result)

        mode = self._resolve_mode(rule)
        ctx: Dict[str, Any] = {
            "rule_id": rule.rule_id,
            "alert_summary": (alert or {}).get("message", ""),
            "alert_manager": self._alert_manager,
        }

        if mode == "shadow":
            # 阶段①：shadow 只读——仅记录建议，不执行任何动作
            result.status = STATUS_PROPOSED
            result.dry_run = True
            result.executed = False
            result.message = f"shadow 只读建议：将执行 {result.action_type}"
            result.details["mode"] = "shadow"
            result.details["kill_switch"] = self._kill_switch
            self._audit("execute", "proposed", "action_engine", result.to_dict())
            return self._record(result)

        if mode == "confirm":
            # 阶段②：confirm 人工确认——先入待审批队列，approve 后执行
            existing = next(
                (a for a in self._pending_approvals.values()
                 if a.status == APPROVAL_PENDING
                 and self._execution_by_action_id(a.action_id) is not None
                 and self._execution_by_action_id(a.action_id).action_key == action_key),
                None,
            )
            if existing is not None:
                result.status = STATUS_SKIPPED
                result.message = f"同 key 已有待审批单 {existing.approval_id}"
                result.details["approval_id"] = existing.approval_id
                return self._record(result)
            approval = ActionApproval(
                action_id=result.action_id,
                rule_id=rule.rule_id,
                status=APPROVAL_PENDING,
                proposed_at=self._clock(),
            )
            # 预执行 dry_run（只读演练），把意图与回滚方案落进审批单
            dry = self._execute_via_executor(rule, ctx, dry_run=True)
            result.status = STATUS_PENDING_APPROVAL
            result.dry_run = True
            result.executed = False
            result.message = f"待人工确认（approval_id={approval.approval_id}）"
            result.approval = approval.to_dict()
            result.rollback_info = dry.rollback_info
            result.details["mode"] = "confirm"
            result.details["dry_run_result"] = dry.to_dict()
            self._pending_approvals[approval.approval_id] = approval
            self._audit("approval", "proposed", "action_engine", result.to_dict())
            return self._record(result)

        # 阶段③：auto 自动执行（安全测试通过后放开）
        if self._kill_switch:
            result.status = STATUS_BLOCKED
            result.executed = False
            result.message = "Kill Switch 置位：自动动作被拦截（独立一键停止生效）"
            result.details["mode"] = "auto"
            result.details["kill_switch"] = True
            self._audit("execute", "blocked", "action_engine", result.to_dict())
            return self._record(result)
        executed = self._execute_via_executor(rule, ctx, dry_run=False)
        executed.action_key = action_key
        if executed.status == STATUS_EXECUTED:
            self._last_execution_ts[action_key] = self._clock()
        return executed

    # ── 对外入口 ───────────────────────────────────────────────

    def evaluate(
        self,
        alert: Any = None,
        metrics: Optional[Dict[str, float]] = None,
    ) -> List[ActionResult]:
        """
        评估全部启用规则并执行/建议/拦截（事件入口）。

        Parameters
        ----------
        alert : Alert | dict | None
            触发告警；None 时仅评估纯指标规则。
        metrics : dict | None
            当前指标快照 {"name": value}（指标门限消费）。

        Returns
        -------
        List[ActionResult] : 本轮执行留痕（未匹配返回空列表）。
        """
        alert_dict = self._normalize_alert(alert) if alert is not None else None
        metric_map: Dict[str, float] = dict(metrics or {})
        results: List[ActionResult] = []
        for rule in self.get_rules():
            if not rule.enabled:
                continue
            if not self._match_rule(rule, alert_dict, metric_map):
                continue
            results.append(self._execute_rule(rule, alert_dict, metric_map))
        if results:
            self._publish_gauges(self.summary())
        return results

    def handle_alert(self, alert: Any) -> List[ActionResult]:
        """便捷入口：单条告警触发规则评估。"""
        return self.evaluate(alert=alert)

    def run_cycle(self, metrics: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """
        周期巡检（调度器装配）：拉取未确认告警逐条评估 + 过期审批清理。

        Parameters
        ----------
        metrics : dict | None
            当前指标快照（指标门限消费）。

        Returns
        -------
        dict : 汇总统计 {evaluated/executed/proposed/pending/blocked/skipped/
               failed/expired_approvals/error}。
        """
        summary: Dict[str, Any] = {
            "evaluated": 0, "executed": 0, "proposed": 0, "pending": 0,
            "blocked": 0, "skipped": 0, "failed": 0,
            "expired_approvals": 0, "error": None,
        }
        summary["expired_approvals"] = self._expire_pending_approvals()
        manager = self._alert_manager
        if manager is None:
            try:
                from myquant.monitoring.alerting import get_alert_manager
                manager = get_alert_manager()
                self._alert_manager = manager
            except Exception as e:  # noqa: BLE001 —— 管理器缺失降级
                summary["error"] = f"alert_manager_unavailable: {e}"
                logger.warning("[action] run_cycle 取告警管理器失败: %s", e)
                return summary
        try:
            alerts = manager.get_unacknowledged(limit=DEFAULT_CYCLE_ALERT_LIMIT)
        except Exception as e:  # noqa: BLE001 —— 拉取失败不阻断巡检
            summary["error"] = f"get_unacknowledged_failed: {e}"
            logger.warning("[action] run_cycle 拉取未确认告警失败: %s", e)
            return summary
        for alert in alerts:
            for result in self.evaluate(alert=alert, metrics=metrics):
                summary["evaluated"] += 1
                status = result.status
                if status == STATUS_EXECUTED:
                    summary["executed"] += 1
                elif status == STATUS_PROPOSED:
                    summary["proposed"] += 1
                elif status == STATUS_PENDING_APPROVAL:
                    summary["pending"] += 1
                elif status == STATUS_BLOCKED:
                    summary["blocked"] += 1
                elif status == STATUS_SKIPPED:
                    summary["skipped"] += 1
                elif status == STATUS_FAILED:
                    summary["failed"] += 1
        if summary["evaluated"]:
            self._publish_gauges(self.summary())
        logger.info(
            "[action] run_cycle 完成: evaluated=%d executed=%d proposed=%d "
            "pending=%d blocked=%d expired=%d error=%s",
            summary["evaluated"], summary["executed"], summary["proposed"],
            summary["pending"], summary["blocked"],
            summary["expired_approvals"], summary["error"],
        )
        return summary

    # ── 人工确认（confirm 模式）───────────────────────────────

    def _execution_by_action_id(self, action_id: str) -> Optional[ActionResult]:
        """按 action_id 反查执行留痕（用于审批单关联）。"""
        for result in reversed(self._executions):
            if result.action_id == action_id:
                return result
        return None

    def _expire_pending_approvals(self) -> int:
        """把超时未审批的 pending 单置为 expired（审计留痕）。"""
        expired = 0
        now = self._clock()
        for approval_id, approval in list(self._pending_approvals.items()):
            if approval.status != APPROVAL_PENDING:
                continue
            if (now - approval.proposed_at) > self._approval_timeout_seconds:
                approval.status = APPROVAL_EXPIRED
                approval.decided_at = now
                approval.actor = "system"
                approval.reason = "人工确认超时自动作废"
                self._audit(
                    "approval", "expired", "system",
                    {"approval_id": approval_id, "rule_id": approval.rule_id,
                     "timeout_seconds": self._approval_timeout_seconds},
                )
                expired += 1
        return expired

    def approve(
        self,
        approval_id: str,
        actor: str = "system",
        reason: str = "人工确认通过",
    ) -> ActionResult:
        """
        人工确认通过（confirm 模式）：执行关联动作。

        Kill Switch 置位时审批不生效（blocked 留痕，需复位后重新提议）。

        Parameters
        ----------
        approval_id : str
            待审批单 ID。
        actor : str
            审批人（非空）。
        reason : str
            审批理由。

        Returns
        -------
        ActionResult : 执行留痕（status=executed/failed/blocked/not_found）。

        Raises
        ------
        ValueError : approval_id 空、审批单不存在/非 pending、actor 空时抛出。
        """
        approval_id = (approval_id or "").strip()
        if not approval_id:
            raise ValueError("approve approval_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("approve actor 不能为空")
        approval = self._pending_approvals.get(approval_id)
        if approval is None or approval.status != APPROVAL_PENDING:
            raise ValueError(f"审批单 {approval_id} 不存在或非 pending")
        result = self._execution_by_action_id(approval.action_id)
        if result is None:
            raise ValueError(f"审批单 {approval_id} 关联动作不存在")
        approval.status = APPROVAL_APPROVED
        approval.decided_at = self._clock()
        approval.actor = actor
        approval.reason = reason
        if self._kill_switch:
            result.status = STATUS_BLOCKED
            result.executed = False
            result.message = "Kill Switch 置位：审批通过但动作被拦截（需复位后重新提议）"
            result.details["mode"] = "confirm"
            result.details["kill_switch"] = True
            result.approval = approval.to_dict()
            self._audit("approval", "blocked", actor,
                        {"approval_id": approval_id, "reason": reason})
            return self._record(result)
        # 正式执行（dry_run=False）
        rule = self.get_rule(approval.rule_id)
        if rule is None:
            result.status = STATUS_FAILED
            result.message = f"规则 {approval.rule_id} 已不存在，无法执行"
            result.approval = approval.to_dict()
            return self._record(result)
        ctx: Dict[str, Any] = {
            "rule_id": rule.rule_id,
            "alert_summary": (result.details or {}).get("trigger_alert", ""),
            "alert_manager": self._alert_manager,
        }
        executed = self._execute_via_executor(rule, ctx, dry_run=False)
        executed.action_key = result.action_key
        executed.approval = approval.to_dict()
        if executed.status == STATUS_EXECUTED:
            self._last_execution_ts[executed.action_key] = self._clock()
        self._audit("approval", "approved", actor,
                    {"approval_id": approval_id, "rule_id": rule.rule_id,
                     "action_id": executed.action_id, "status": executed.status,
                     "reason": reason})
        return executed

    def reject(
        self,
        approval_id: str,
        actor: str = "system",
        reason: str = "人工否决",
    ) -> ActionResult:
        """
        人工否决（confirm 模式）：审批单置 rejected，动作不执行。

        Parameters
        ----------
        approval_id : str
            待审批单 ID。
        actor : str
            否决人（非空）。
        reason : str
            否决理由。

        Returns
        -------
        ActionResult : 原始建议动作留痕（status 更新为 rejected）。

        Raises
        ------
        ValueError : approval_id 空、审批单不存在/非 pending、actor 空时抛出。
        """
        approval_id = (approval_id or "").strip()
        if not approval_id:
            raise ValueError("reject approval_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("reject actor 不能为空")
        approval = self._pending_approvals.get(approval_id)
        if approval is None or approval.status != APPROVAL_PENDING:
            raise ValueError(f"审批单 {approval_id} 不存在或非 pending")
        approval.status = APPROVAL_REJECTED
        approval.decided_at = self._clock()
        approval.actor = actor
        approval.reason = reason
        result = self._execution_by_action_id(approval.action_id)
        if result is not None:
            result.status = STATUS_REJECTED
            result.executed = False
            result.message = f"人工否决（{reason}）"
            result.approval = approval.to_dict()
        self._audit("approval", "rejected", actor,
                    {"approval_id": approval_id, "reason": reason})
        logger.info("[action] 审批已否决: %s（%s）", approval_id, reason)
        return result if result is not None else ActionResult(
            rule_id=approval.rule_id,
            status=STATUS_REJECTED,
            message=reason,
            approval=approval.to_dict(),
            timestamp=self._clock(),
        )

    def pending_approvals(self) -> List[Dict[str, Any]]:
        """返回全部待审批单（JSON 可序列化）。"""
        return [a.to_dict() for a in self._pending_approvals.values()
                if a.status == APPROVAL_PENDING]

    # ── 回滚 ───────────────────────────────────────────────────

    def rollback(self, action_id: str, actor: str = "system", reason: str = "人工回滚") -> bool:
        """
        回滚已执行动作（经 rollback_info 调执行器 undo）。

        Parameters
        ----------
        action_id : str
            目标动作 ID（history 中查询）。
        actor : str
            操作者（非空）。
        reason : str
            回滚原因。

        Returns
        -------
        bool : 回滚是否成功（无可回滚信息/执行器未注册/undo False 均返回 False）。
        """
        action_id = (action_id or "").strip()
        if not action_id:
            raise ValueError("rollback action_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("rollback actor 不能为空")
        result = self._execution_by_action_id(action_id)
        if result is None:
            logger.warning("[action] 回滚失败：动作 %s 不存在", action_id)
            return False
        if result.rollback_info is None:
            logger.warning("[action] 回滚失败：动作 %s 无可回滚信息", action_id)
            return False
        executor_cls = get_action_executor(result.action_type)
        if executor_cls is None:
            logger.warning("[action] 回滚失败：动作类型 %s 未注册", result.action_type)
            return False
        try:
            ok = executor_cls().undo(dict(result.rollback_info))
        except Exception as e:  # noqa: BLE001 —— 回滚异常如实报告
            logger.error("[action] 回滚动作 %s 异常: %s", action_id, e)
            ok = False
        rb_record = ActionResult(
            rule_id=result.rule_id,
            action_type=result.action_type,
            status=STATUS_ROLLED_BACK if ok else STATUS_FAILED,
            executed=False,
            message=f"回滚 {'成功' if ok else '失败'}（原始动作 {action_id}）",
            details={
                "original_action_id": action_id,
                "original_status": result.status,
                "reason": reason,
            },
            timestamp=self._clock(),
            action_key=result.action_key,
        )
        self._audit("rollback", "executed" if ok else "failed", actor,
                    rb_record.to_dict())
        return self._record(rb_record).status == STATUS_ROLLED_BACK

    # ── 观测 ───────────────────────────────────────────────────

    def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        返回执行全链路留痕（JSON 可序列化）。

        Parameters
        ----------
        limit : int
            返回条数上限（正整数）。

        Returns
        -------
        List[dict] : 最近 N 条执行留痕。
        """
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"history limit 必须为正整数，收到 {limit!r}")
        return [r.to_dict() for r in self._executions[-limit:]]

    def summary(self) -> Dict[str, Any]:
        """引擎状态汇总（模式/Kill Switch/规则数/执行统计）。"""
        return {
            "mode": self._mode,
            "kill_switch": self._kill_switch,
            "rules": len(self._rules),
            "enabled_rules": sum(1 for r in self._rules.values() if r.enabled),
            "executions_total": len(self._executions),
            "executed_total": sum(1 for e in self._executions if e.executed),
            "proposed_total": sum(
                1 for e in self._executions if e.status == STATUS_PROPOSED
            ),
            "blocked_total": sum(
                1 for e in self._executions if e.status == STATUS_BLOCKED
            ),
            "pending_approvals": len(self._pending_approvals),
        }

    # ── 审计 + 指标 ────────────────────────────────────────────

    def _audit(
        self,
        action: str,
        sub_action: str,
        actor: str,
        detail: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        审计留痕（kind=action_engine，BASE-7 append-only）。

        审计失败只记 warning，不阻断动作主流程（告警优先原则）。
        """
        if self._audit_log is None:
            if not self._audit_tried:
                try:
                    from myquant.core.audit_log import AuditLogStore
                    self._audit_log = AuditLogStore()
                except Exception as e:  # noqa: BLE001 —— 审计不可用降级内存留痕
                    logger.warning("[action] 审计存储不可用（降级内存留痕）: %s", e)
                self._audit_tried = True
            if self._audit_log is None:
                return None
        try:
            return self._audit_log.append(
                "action_engine", f"{action}:{sub_action}", actor,
                dict(detail or {}),
            )
        except Exception as e:  # noqa: BLE001 —— 审计失败不阻断动作
            logger.warning("[action] 审计留痕失败: %s", e)
            return None

    def _publish_gauges(self, s: Dict[str, Any]) -> None:
        """动作引擎指标进 /api/metrics（动态 set_gauge，失败不阻断）。"""
        try:
            from myquant.monitoring.metrics_collector import get_metrics_collector
            mc = get_metrics_collector()
            mc.set_gauge(METRIC_EXECUTIONS_TOTAL, float(s["executions_total"]))
            mc.set_gauge(METRIC_EXECUTED_TOTAL, float(s["executed_total"]))
            mc.set_gauge(METRIC_PROPOSED_TOTAL, float(s["proposed_total"]))
            mc.set_gauge(METRIC_BLOCKED_TOTAL, float(s["blocked_total"]))
            mc.set_gauge(METRIC_PENDING_APPROVALS, float(s["pending_approvals"]))
        except Exception as e:  # noqa: BLE001 —— 指标写入失败不影响动作
            logger.warning("[action] 动作引擎指标写入失败（不影响动作）: %s", e)


# ═══════════════════════════════════════════════════════════════
#  单例 + 配置装配
# ═══════════════════════════════════════════════════════════════

_ACTION_ENGINE_SINGLETON: Optional[ActionEngine] = None


def get_action_engine() -> ActionEngine:
    """
    动作引擎单例（按 monitoring.action_engine 配置装配，R-04 外置）。

    返回单例：mode/cooldown/approval_timeout 取自 settings.yaml
    monitoring.action_engine 块；auto_register_rules=true 时加载 rules 列表。
    """
    global _ACTION_ENGINE_SINGLETON
    if _ACTION_ENGINE_SINGLETON is None:
        cfg = get_monitoring_config().get("action_engine", {}) or {}
        _ACTION_ENGINE_SINGLETON = ActionEngine(
            mode=str(cfg.get("mode", "shadow")),
            cooldown_seconds=float(cfg.get("cooldown_seconds", DEFAULT_RULE_COOLDOWN_SECONDS)),
            approval_timeout_seconds=float(
                cfg.get("approval_timeout_seconds", DEFAULT_APPROVAL_TIMEOUT_SECONDS)
            ),
        )
        if cfg.get("auto_register_rules") and isinstance(cfg.get("rules"), list):
            _ACTION_ENGINE_SINGLETON.load_rules(cfg["rules"])
    return _ACTION_ENGINE_SINGLETON


def reset_action_engine() -> None:
    """重置动作引擎单例（测试隔离用）。"""
    global _ACTION_ENGINE_SINGLETON
    _ACTION_ENGINE_SINGLETON = None


def load_action_engine_config() -> Dict[str, Any]:
    """
    读取 monitoring.action_engine 配置块（R-04 外置真源）。

    Returns
    -------
    dict : 完整 action_engine 配置（enabled/mode/cooldown_seconds/
           approval_timeout_seconds/interval_seconds/auto_register_rules/rules）。
    """
    return get_monitoring_config().get("action_engine", {}) or {}


# ═══════════════════════════════════════════════════════════════
#  __main__ 自测
# ═══════════════════════════════════════════════════════════════

def _run_self_test() -> int:
    """模块自测（D-1 核心路径 + D-2 场景动作库 smoke）。"""
    logger.info("action_engine __main__ 自测开始")

    def check(name: str, cond: bool, info: str = "") -> None:
        if not cond:
            raise AssertionError(f"[自测失败] {name}: {info}")

    paused: List[str] = []

    def fake_pause() -> None:
        paused.append("engine")

    def fake_resume() -> None:
        if paused:
            paused.pop()

    # 1. 规则注册与校验
    engine = ActionEngine(mode="shadow", clock=lambda: 1000.0)
    rule = ActionRule(
        rule_id="r1", name="行情断流暂停策略",
        condition={"categories": ["market_staleness"], "levels": ["ERROR", "CRITICAL"]},
        action={"type": "pause_strategy",
                "params": {"pause_fn": fake_pause, "resume_fn": fake_resume,
                           "target": "engine"}},
    )
    engine.register_rule(rule)
    check("规则注册", engine.get_rule("r1") is rule)
    try:
        engine.register_rule(rule)
        check("重复规则拒绝", False)
    except ValueError:
        pass
    try:
        engine.register_rule(ActionRule(rule_id="r2", action={"type": "unknown_act"}))
        check("未注册动作类型拒绝", False)
    except ValueError:
        pass

    # 2. shadow 只读建议不执行
    results = engine.handle_alert(
        {"level": "ERROR", "category": "market_staleness", "source": "watchdog",
         "message": "行情断流", "deduplication_key": "watchdog:staleness"}
    )
    check("shadow 命中 1 条", len(results) == 1, str(results))
    check("shadow 状态 proposed", results[0].status == "proposed", results[0].to_dict())
    check("shadow 不真实执行", results[0].executed is False and not paused)

    # 3. confirm 人工确认
    engine.set_mode("confirm")
    results = engine.handle_alert(
        {"level": "CRITICAL", "category": "market_staleness", "source": "watchdog",
         "message": "行情断流", "deduplication_key": "watchdog:staleness"}
    )
    check("confirm 命中 pending", results[0].status == "pending_approval", results[0].to_dict())
    approvals = engine.pending_approvals()
    check("待审批 1 条", len(approvals) == 1)
    # 同 key 去重：冷却窗口内不重复提议
    again = engine.handle_alert(
        {"level": "CRITICAL", "category": "market_staleness", "source": "watchdog",
         "message": "行情断流", "deduplication_key": "watchdog:staleness"}
    )
    check("同 key 待审批去重", again[0].status == "skipped", again[0].to_dict())
    ok = engine.approve(approvals[0]["approval_id"], actor="trader", reason="确认")
    check("approve 执行成功", ok.status == "executed" and paused == ["engine"], ok.to_dict())

    # 4. Kill Switch 独立停止（auto 模式：置位后自动动作一律 blocked）
    engine.set_mode("auto", actor="admin", reason="放开自动执行，验证 Kill Switch")
    engine.set_kill_switch(True, actor="admin", reason="演练停止")
    results = engine.handle_alert(
        {"level": "CRITICAL", "category": "market_staleness", "source": "watchdog",
         "message": "行情断流", "deduplication_key": "watchdog:staleness-2"}
    )
    check("Kill Switch 拦截 auto", results[0].status == "blocked", results[0].to_dict())
    engine.set_kill_switch(False, actor="admin", reason="恢复")

    # 5. 回滚
    rb_ok = engine.rollback(
        next(r["action_id"] for r in engine.history() if r["executed"]),
        actor="trader", reason="恢复",
    )
    check("回滚成功", rb_ok and paused == [])

    # 6. run_cycle 周期巡检（注入假告警管理器）
    class _FakeMgr:
        def get_unacknowledged(self, limit: int = 50) -> List[Dict[str, Any]]:
            return [{"level": "ERROR", "category": "market_staleness",
                     "source": "watchdog", "message": "行情断流",
                     "deduplication_key": "watchdog:staleness-3"}]
    cycle_engine = ActionEngine(mode="shadow", clock=lambda: 2000.0)
    cycle_engine.register_rule(rule)
    cycle_engine._alert_manager = _FakeMgr()
    summary = cycle_engine.run_cycle()
    check("run_cycle 评估 1 条", summary["evaluated"] == 1, str(summary))
    check("run_cycle shadow 建议", summary["proposed"] == 1, str(summary))

    logger.info("action_engine __main__ 自测全部通过")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(_run_self_test())
