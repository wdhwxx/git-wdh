# 文件名：myquant/strategies/param_tuner.py
"""
param_tuner.py - 参数动态调整器（D-5）
=====================================
策略参数随监控反馈动态调整，带审批流：自动提议 → 人工确认 → 生效回滚。
模型版本管理（已有 registry）衔接：approve 生效时经注入 registry_fn 在模型
registry 登记新版本（metadata.hyperparameters=合并后参数）。

核心能力:
  1. propose — 自动提议参数变更（未批准不生效，只登记建议）
  2. approve — 人工确认后生效（经注入 apply_fn 应用；衔接模型版本 registry）
  3. reject — 人工否决（不生效）
  4. rollback — 回滚恢复 params_before（可回滚）
  5. 全流程留痕 — proposal_history JSON 可序列化 + AuditLogStore kind=param_tuner
  6. 参数校验 — 变更非空/数值有限/白名单可配/无通配键

安全模型（对齐 D-1 三阶段）:
  - mode=confirm（默认）：propose 只出建议 status=proposed，需 approve 人工
    确认后生效，reject 否决；
  - mode=auto：apply_auto 直接生效（安全测试通过后放开）。
  未批准不生效：proposed 状态绝不触碰策略生效参数。

防未来函数说明:
  - 本模块不预测参数最优值：自动提议内容由调用方注入 advisor_fn（基于已发生
    监控反馈），本模块只做审批流/生效/回滚/留痕；
  - created_at/decided_at 一律当前时点（clock 可注入，测试用）。

依赖: typing, dataclasses, uuid, time, myquant.core.logger
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("strategies.param_tuner")


# 提议状态
PARAM_STATUS_PROPOSED = "proposed"
PARAM_STATUS_APPLIED = "applied"
PARAM_STATUS_REJECTED = "rejected"
PARAM_STATUS_ROLLED_BACK = "rolled_back"
PARAM_MODES = ("confirm", "auto")

# 算法参数（非业务阈值）
PARAM_DEFAULT_HISTORY_LIMIT: int = 500


@dataclass
class ParamProposal:
    """
    一次参数调整提议的全链路留痕（JSON 可序列化）。

    Attributes
    ----------
    proposal_id : str
        提议唯一 ID（审批/回滚句柄）。
    strategy_name : str
        目标策略名。
    param_changes : dict
        提议的参数变更（键=参数名，值=新参数值）。
    params_before : dict
        生效前参数快照（未批准前=当前生效参数，回滚目标）。
    params_after : dict
        合并后的目标参数（params_before 局部覆盖 param_changes）。
    status : str
        proposed / applied / rejected / rolled_back。
    mode : str
        confirm / auto。
    reason : str
        提议原因（如监控反馈摘要）。
    actor : str
        操作者。
    created_at / decided_at : float
        提议/决策时刻（当前时点，防未来）。
    registry_version : int | None
        衔接模型版本 registry 登记的版本号（未注入 registry_fn 为 None）。
    """

    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    strategy_name: str = ""
    param_changes: Dict[str, Any] = field(default_factory=dict)
    params_before: Dict[str, Any] = field(default_factory=dict)
    params_after: Dict[str, Any] = field(default_factory=dict)
    status: str = PARAM_STATUS_PROPOSED
    mode: str = PARAM_MODES[0]
    reason: str = ""
    actor: str = "system"
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    registry_version: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化（JSON 可读）。"""
        return {
            "proposal_id": self.proposal_id,
            "strategy_name": self.strategy_name,
            "param_changes": dict(self.param_changes),
            "params_before": dict(self.params_before),
            "params_after": dict(self.params_after),
            "status": self.status,
            "mode": self.mode,
            "reason": self.reason,
            "actor": self.actor,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "registry_version": self.registry_version,
        }


class ParamTuner:
    """
    策略参数动态调整器（D-5）。

    用法:
        from myquant.strategies.param_tuner import ParamTuner
        tuner = ParamTuner(
            apply_fn=lambda p: strategy.params.update(p["param_changes"]),
            rollback_fn=lambda p: strategy.params.update(p["params_before"]),
            registry_fn=lambda p: registry.register_model(
                p["strategy_name"] + "_params", {...},
                metadata={"hyperparameters": p["params_after"]},
                source="live"),
        )
        prop = tuner.propose("ma_strategy", {"fast": 10}, reason="监控反馈")
        tuner.approve(prop.proposal_id, actor="risk_owner")  # 生效 + 登记版本

    未批准不生效：propose 只登记建议，绝不触碰生效参数。
    缺注入 apply_fn 时 approve 返回 not_supported（不假成功 KP-M02）。
    """

    def __init__(
        self,
        mode: str = PARAM_MODES[0],
        apply_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
        rollback_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
        registry_fn: Optional[Callable[[Dict[str, Any]], Any]] = None,
        allowed_keys: Optional[List[str]] = None,
        audit_log: Any = None,
        clock: Optional[Callable[[], float]] = None,
        max_history: int = PARAM_DEFAULT_HISTORY_LIMIT,
    ) -> None:
        """
        Parameters
        ----------
        mode : str
            confirm（默认，需 approve 人工确认）/ auto（自动生效）。
        apply_fn : callable | None
            生效回调（入参=提议 dict，部署方接线到策略参数更新）。
        rollback_fn : callable | None
            回滚回调（入参=提议 dict，恢复 params_before）。
        registry_fn : callable | None
            模型版本 registry 衔接回调（入参=提议 dict；登记新版本）。
        allowed_keys : list | None
            允许调整的参数白名单（None=不限制；空列表=拒绝一切变更）。
        audit_log : AuditLogStore | None
            审计存储（kind=param_tuner）；None 惰性尝试，失败降级内存留痕。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        max_history : int
            提议历史留存上限（正整数）。
        """
        if mode not in PARAM_MODES:
            raise ValueError(f"mode 必须为 {PARAM_MODES}，收到 {mode!r}")
        if not isinstance(max_history, int) or max_history <= 0:
            raise ValueError(f"max_history 必须为正整数，收到 {max_history!r}")
        self._mode: str = mode
        self._apply_fn: Optional[Callable[[Dict[str, Any]], Any]] = apply_fn
        self._rollback_fn: Optional[Callable[[Dict[str, Any]], Any]] = rollback_fn
        self._registry_fn: Optional[Callable[[Dict[str, Any]], Any]] = registry_fn
        self._allowed_keys: Optional[List[str]] = (
            list(allowed_keys) if allowed_keys is not None else None
        )
        self._audit_log: Any = audit_log
        self._audit_tried: bool = audit_log is not None
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._max_history: int = int(max_history)

        self._effective: Dict[str, Dict[str, Any]] = {}   # strategy → 生效参数
        self._history: List[ParamProposal] = []
        self._by_id: Dict[str, ParamProposal] = {}

        logger.debug(
            "ParamTuner 初始化: mode=%s allowed_keys=%s max_history=%d",
            self._mode, self._allowed_keys, self._max_history,
        )

    # ── 参数校验 ──────────────────────────────────────────────

    def _validate_changes(
        self, strategy_name: str, param_changes: Dict[str, Any]
    ) -> Dict[str, Any]:
        """校验参数变更（非空/有限/白名单/无通配/键可字符串化）。"""
        if not (strategy_name or "").strip():
            raise ValueError("strategy_name 不能为空")
        if not isinstance(param_changes, dict) or not param_changes:
            raise ValueError("param_changes 必须为非空 dict")
        for key in param_changes:
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"参数名必须为非空字符串，收到 {key!r}")
            if "*" in key or "?" in key:
                raise ValueError(f"参数名不允许通配符，收到 {key!r}")
            if self._allowed_keys is not None and key not in self._allowed_keys:
                raise ValueError(
                    f"参数 {key} 不在白名单 {self._allowed_keys} 内"
                )
        cleaned: Dict[str, Any] = {}
        for key, val in param_changes.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if not math.isfinite(float(val)):
                    raise ValueError(f"参数 {key} 必须为有限数值，收到 {val!r}")
            cleaned[key] = val
        if self._allowed_keys == []:
            raise ValueError("参数白名单为空，禁止任何参数变更")
        return cleaned

    # ── 提议 / 审批 / 生效 / 回滚 ────────────────────────────

    def propose(
        self,
        strategy_name: str,
        param_changes: Dict[str, Any],
        reason: str = "",
        actor: str = "system",
        base_params: Optional[Dict[str, Any]] = None,
    ) -> ParamProposal:
        """
        自动提议参数变更（未批准不生效：只登记建议，不触碰生效参数）。

        Parameters
        ----------
        base_params : dict | None
            生效前参数快照（None=取 tuner 内 effective[strategy]，仍无则用
            param_changes 合并自身作为建议快照，标注冷启动）。
        """
        if not (actor or "").strip():
            raise ValueError("propose actor 不能为空")
        changes = self._validate_changes(strategy_name, param_changes)
        current = self._effective.get(strategy_name)
        if current is None:
            current = dict(base_params) if base_params is not None else {}
        params_after = dict(current)
        params_after.update(changes)
        prop = ParamProposal(
            strategy_name=strategy_name,
            param_changes=changes,
            params_before=dict(current),
            params_after=params_after,
            status=PARAM_STATUS_PROPOSED,
            mode=self._mode,
            reason=reason,
            actor=actor,
            created_at=self._clock(),
        )
        self._record(prop)
        self._audit("param", "proposed", actor, prop.to_dict())
        return prop

    def approve(
        self,
        proposal_id: str,
        actor: str = "system",
        reason: str = "人工确认参数调整生效",
    ) -> ParamProposal:
        """
        人工确认参数调整生效（mode=confirm 必经；未批准不生效的生效点）。

        生效动作：
          1) 经注入 apply_fn 应用（缺注入返回 not_supported 不假成功 KP-M02）；
          2) 衔接模型版本 registry（registry_fn 注入时登记新版本并回填
             registry_version；未注入记录 registry_skipped 留痕不编造）。

        Raises
        ------
        ValueError : 提议不存在/非 proposed/actor 为空/生效失败时抛出。
        """
        proposal_id = (proposal_id or "").strip()
        if not proposal_id:
            raise ValueError("approve proposal_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("approve actor 不能为空")
        prop = self._by_id.get(proposal_id)
        if prop is None or prop.status != PARAM_STATUS_PROPOSED:
            raise ValueError(f"提议 {proposal_id} 不存在或非 proposed")
        if self._apply_fn is None:
            raise ValueError(
                "未注入 apply_fn（不假成功 KP-M02）：无法应用参数变更"
            )
        # 1. 应用参数变更（业务回调，异常如实上抛）
        try:
            self._apply_fn(prop.to_dict())
        except Exception as e:  # noqa: BLE001 —— 生效失败如实报告
            logger.error("[param_tuner] 参数生效失败: %s", e)
            raise ValueError(f"参数生效失败: {e}") from e
        # 2. 衔接模型版本 registry（可选注入）
        registry_version: Optional[int] = None
        registry_skipped = False
        if self._registry_fn is not None:
            try:
                out = self._registry_fn(prop.to_dict())
                if isinstance(out, dict) and "version" in out:
                    registry_version = out["version"]
                elif isinstance(out, int):
                    registry_version = out
            except Exception as e:  # noqa: BLE001 —— registry 登记失败不阻断生效
                logger.warning("[param_tuner] 模型版本登记失败（留痕继续）: %s", e)
        else:
            registry_skipped = True
        prop.registry_version = registry_version
        prop.status = PARAM_STATUS_APPLIED
        prop.decided_at = self._clock()
        prop.actor = actor
        prop.reason = reason
        self._effective[prop.strategy_name] = dict(prop.params_after)
        self._audit(
            "param", "approved", actor,
            {**prop.to_dict(), "registry_skipped": registry_skipped},
        )
        return prop

    def reject(
        self,
        proposal_id: str,
        actor: str = "system",
        reason: str = "人工否决",
    ) -> ParamProposal:
        """人工否决参数调整提议（不生效）。"""
        proposal_id = (proposal_id or "").strip()
        if not proposal_id:
            raise ValueError("reject proposal_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("reject actor 不能为空")
        prop = self._by_id.get(proposal_id)
        if prop is None or prop.status != PARAM_STATUS_PROPOSED:
            raise ValueError(f"提议 {proposal_id} 不存在或非 proposed")
        prop.status = PARAM_STATUS_REJECTED
        prop.decided_at = self._clock()
        prop.actor = actor
        prop.reason = reason
        self._audit("param", "rejected", actor, prop.to_dict())
        return prop

    def apply_auto(
        self,
        strategy_name: str,
        param_changes: Dict[str, Any],
        reason: str = "",
        actor: str = "system",
        base_params: Optional[Dict[str, Any]] = None,
    ) -> ParamProposal:
        """自动生效（auto 模式；安全测试通过后放开）。"""
        prop = self.propose(
            strategy_name, param_changes, reason=reason, actor=actor,
            base_params=base_params,
        )
        return self.approve(prop.proposal_id, actor=actor, reason=reason)

    def rollback(
        self, proposal_id: str, actor: str = "system",
        reason: str = "参数调整回滚",
    ) -> bool:
        """
        回滚一次已生效参数调整（恢复 params_before；可回滚）。

        Returns
        -------
        bool : 是否成功回滚（提议不存在/非 applied/缺 rollback_fn 返回 False）。
        """
        proposal_id = (proposal_id or "").strip()
        if not proposal_id:
            raise ValueError("rollback proposal_id 不能为空")
        if not (actor or "").strip():
            raise ValueError("rollback actor 不能为空")
        prop = self._by_id.get(proposal_id)
        if prop is None or prop.status != PARAM_STATUS_APPLIED:
            return False
        if self._rollback_fn is None:
            logger.warning("[param_tuner] 回滚失败：未注入 rollback_fn")
            return False
        try:
            self._rollback_fn(prop.to_dict())
        except Exception as e:  # noqa: BLE001
            logger.error("[param_tuner] 参数回滚失败: %s", e)
            return False
        self._effective[prop.strategy_name] = dict(prop.params_before)
        prop.status = PARAM_STATUS_ROLLED_BACK
        prop.decided_at = self._clock()
        prop.actor = actor
        prop.reason = reason
        self._audit("param", "rolled_back", actor, prop.to_dict())
        return True

    # ── 观测 ──────────────────────────────────────────────────

    def effective_params(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        """某策略当前生效参数（未生效/未应用返回 None）。"""
        if strategy_name not in self._effective:
            return None
        return dict(self._effective[strategy_name])

    def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """返回提议全量留痕（JSON 可序列化；可审计/可查）。"""
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"history limit 必须为正整数，收到 {limit!r}")
        return [p.to_dict() for p in self._history[-limit:]]

    def summary(self) -> Dict[str, Any]:
        """调整器状态汇总。"""
        return {
            "mode": self._mode,
            "strategies_tracked": len(self._effective),
            "proposed_total": sum(
                1 for p in self._history if p.status == PARAM_STATUS_PROPOSED
            ),
            "applied_total": sum(
                1 for p in self._history if p.status == PARAM_STATUS_APPLIED
            ),
            "rejected_total": sum(
                1 for p in self._history if p.status == PARAM_STATUS_REJECTED
            ),
            "rolled_back_total": sum(
                1 for p in self._history if p.status == PARAM_STATUS_ROLLED_BACK
            ),
            "history_total": len(self._history),
        }

    # ── 内部 ──────────────────────────────────────────────────

    def _record(self, prop: ParamProposal) -> ParamProposal:
        """留痕（上限裁剪最旧）。"""
        self._history.append(prop)
        self._by_id[prop.proposal_id] = prop
        if len(self._history) > self._max_history:
            drop = self._history[: -self._max_history]
            self._history = self._history[-self._max_history:]
            for p in drop:
                self._by_id.pop(p.proposal_id, None)
        return prop

    def _audit(
        self, action: str, sub_action: str, actor: str, detail: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """审计留痕（kind=param_tuner；失败降级内存留痕不阻断）。"""
        if self._audit_log is None:
            if not self._audit_tried:
                try:
                    from myquant.core.audit_log import AuditLogStore
                    self._audit_log = AuditLogStore()
                except Exception as e:  # noqa: BLE001 —— 审计不可用降级
                    logger.warning("[param_tuner] 审计存储不可用（降级内存留痕）: %s", e)
                self._audit_tried = True
            if self._audit_log is None:
                return None
        try:
            return self._audit_log.append(
                "param_tuner", f"param:{sub_action}", actor, dict(detail or {})
            )
        except Exception as e:  # noqa: BLE001 —— 审计失败不阻断调整
            logger.warning("[param_tuner] 审计留痕失败: %s", e)
            return None
