# 文件名：myquant/leakage_gate/exemption.py
"""
leakage_gate/exemption.py - 豁免流（阶段 3 审计收口，M3-02）
================================================================
红线条目可豁免的工程化落点（v2.1 缺失的豁免流实现位置，v3.0 §6.7）：
  - 豁免记录：豁免 ID / 原因 / 有效期（valid_from~valid_until）/ 审批人 / 目标（rule_id + strategy）；
  - grant 时落 `audit_log`（append-only 哈希链，M0-05 BASE-7）；
  - 过期由门禁中心自动拦截：只有"未过期"的豁免压制对应违规；过期豁免不压制
    → 违规保留 → 门禁中心阻断（验收：豁免过期被拦截）。

分层：myquant.leakage_gate 登记 L0；本模块只依赖 myquant.core.audit_log（L0 合法），
不 import strategies（L0→L3 反向被 R-02 拦截）；Violation 以鸭子类型消费
（仅读 .rule_id/.severity/.strategy_id），避免与 gate.py 循环 import。

防未来函数：豁免有效性只按已给定的 valid_from/valid_until 与"当前时点"判定，
不预测未来；audit created_ts 由 AuditLogStore 取当前时点。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from myquant.core.audit_log import AuditLogStore

logger = logging.getLogger("myquant.leakage_gate.exemption")

# ── P1-14 豁免流加固约束常量（#365）────────────────────────────────────
# 合法规则 ID 枚举（static_scan.py L102 注释口径：R01a/R01b/R02/R03/R04/
# R05/R07/R09/R10/R11）。grant 时白名单校验，防 typo 导致豁免对不上违规。
ALLOWED_RULE_IDS: tuple[str, ...] = (
    "R01a", "R01b", "R02", "R03", "R04", "R05",
    "R07", "R09", "R10", "R11",
)

# 不可豁免规则：门禁自检失败产生的 gate-error（gate.py fail-closed 违规），
# 代表门禁自身异常，任何情况下都不得被豁免压制（宁可阻断不可放行）。
NON_EXEMPTABLE_RULE_IDS: frozenset[str] = frozenset({"gate-error"})

# 单次豁免有效期上限（自然日）：长期全局豁免被拒，强制周期性复审。
MAX_EXEMPTION_DAYS: int = 90

# 审批状态机：pending（已申请待批）→ approved（批准生效，仅此态压制违规）
# → revoked（已撤销，软删除留痕可追溯）。
EXEMPTION_STATUSES: tuple[str, ...] = ("pending", "approved", "revoked")

# 模块级默认台账的受控持久化路径（项目根/database/exemptions.json；
# 与 audit_log 的 database/quant.db 同数据区，重启不丢）。
_DEFAULT_LEDGER_PATH: Path = (
    Path(__file__).resolve().parents[2] / "database" / "exemptions.json"
)


class ExemptionError(RuntimeError):
    """豁免流错误（非法有效期 / 超期上限 / 规则非法 / 台账读写失败 / 未找到豁免）。"""


def _to_date(value: str) -> datetime:
    """把 YYYY-MM-DD 字符串解析为 datetime（非法显式报错）。"""
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError) as e:
        raise ExemptionError(f"豁免日期格式须为 YYYY-MM-DD，收到 {value!r}: {e}")


def _now() -> datetime:
    """当前时点（YYYY-MM-DD 粒度取今日零点，便于过期判定）。"""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Exemption:
    """一条豁免记录。

    Attributes:
        exemption_id: 豁免唯一 ID（如 "EX-2026-10-10-001"）。
        reason: 豁免原因（须写明为何合法/保守近似，红线清单 RL 档位约束）。
        rule_id: 豁免覆盖的规则 ID（白名单 ALLOWED_RULE_IDS，#365 校验）。
        approver: 审批人（approve 时填实；与 actor 分离，双审计）。
        valid_from: 生效日（YYYY-MM-DD，含）。
        valid_until: 到期日（YYYY-MM-DD，含；到期后不压制违规 → 门禁拦截）。
        target: 适用策略名；空串 = 全局适用。
        status: 审批状态（pending/approved/revoked；仅 approved 且未过期压制违规）。
        actor: 申请人（grant 发起人；与 approver 分离落双审计）。
    """

    exemption_id: str
    reason: str
    rule_id: str
    approver: str
    valid_from: str
    valid_until: str
    target: str = ""
    status: str = "pending"
    actor: str = ""

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """是否已过期（valid_until < now）。"""
        now_d = now or _now()
        return _to_date(self.valid_until) < now_d

    def is_active(self, now: Optional[datetime] = None) -> bool:
        """是否在有效期内（valid_from <= now <= valid_until）。"""
        now_d = now or _now()
        return _to_date(self.valid_from) <= now_d <= _to_date(self.valid_until)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 JSON 台账条目。"""
        return {
            "exemption_id": self.exemption_id,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "approver": self.approver,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "target": self.target,
            "status": self.status,
            "actor": self.actor,
        }


class ExemptionStore:
    """豁免台账存储（JSON 文件 + 内存），grant 落 audit_log 哈希链。

    Args:
        path: 台账文件路径；None = 仅内存（不持久化，测试/默认空库用）。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path: Optional[Path] = Path(path) if path else None
        self._memory: List[Exemption] = []

    def load(self) -> List[Exemption]:
        """读取全部豁免（文件缺失/损坏返回空，日志留痕不阻断）。"""
        if self.path is not None and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return [Exemption(**e) for e in raw if isinstance(e, dict)]
            except (OSError, json.JSONDecodeError, TypeError) as e:
                logger.error(f"豁免台账读取失败，按空库处理: {e}")
        return list(self._memory)

    def _save(self, exemptions: Sequence[Exemption]) -> None:
        """原子写回台账文件（目录不存在自动创建）。"""
        self._memory = list(exemptions)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps([e.to_dict() for e in exemptions], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def grant(self, exemption: Exemption, actor: str = "exemption") -> Exemption:
        """登记豁免申请（status=pending，未经 approve 不压制任何违规）。

        校验（#365 P1-14）：
          - valid_from <= valid_until；
          - 有效期跨度 ≤ MAX_EXEMPTION_DAYS（90 天），长期全局豁免被拒；
          - rule_id ∈ ALLOWED_RULE_IDS 白名单（防 typo 对不上违规）；
          - rule_id ∉ NON_EXEMPTABLE_RULE_IDS（gate-error 门禁自检故障不可豁免）；
          - ID 不重复。

        Args:
            exemption: 豁免记录（调用方传 status=approved 也被强制降级 pending，
                防绕过审批闭环；actor 空则取 actor 参数）。
            actor: 申请人（落 audit_log 与 exemption.actor）。

        Returns:
            Exemption: 已登记的 pending 豁免。

        Raises:
            ExemptionError: 校验失败（有效期/跨度/规则非法/重复 ID）。
        """
        if _to_date(exemption.valid_from) > _to_date(exemption.valid_until):
            raise ExemptionError(
                f"豁免有效期非法: valid_from {exemption.valid_from} > valid_until "
                f"{exemption.valid_until}")
        span_days = (_to_date(exemption.valid_until)
                     - _to_date(exemption.valid_from)).days
        if span_days > MAX_EXEMPTION_DAYS:
            raise ExemptionError(
                f"豁免有效期 {span_days} 天超过上限 {MAX_EXEMPTION_DAYS} 天"
                f"（长期全局豁免被拒，须周期性复审）")
        if exemption.rule_id in NON_EXEMPTABLE_RULE_IDS:
            raise ExemptionError(
                f"规则 {exemption.rule_id!r} 为门禁自检故障类违规，不可豁免")
        if exemption.rule_id not in ALLOWED_RULE_IDS:
            raise ExemptionError(
                f"豁免规则 ID 非法: {exemption.rule_id!r}，"
                f"白名单为 {ALLOWED_RULE_IDS}")
        existing = [e.exemption_id for e in self.load()]
        if exemption.exemption_id in existing:
            raise ExemptionError(f"豁免 ID 重复: {exemption.exemption_id}")
        # 强制 pending + 补申请人 + 清空审批人：防绕过审批闭环，approve 时才填实 approver
        stored = replace(
            exemption,
            status="pending",
            actor=exemption.actor or actor,
            approver="",
        )
        all_ex = self.load() + [stored]
        self._save(all_ex)
        self._audit("exemption_grant", stored.actor or actor, stored.to_dict())
        return stored

    def approve(self, exemption_id: str, approver: str) -> bool:
        """审批通过一条 pending 豁免（status→approved，自此压制违规）。

        approver 与 grant 的 actor 分离，各落一条 audit_log（双审计）。

        Args:
            exemption_id: 豁免 ID。
            approver: 审批人（须与申请人区分；写入 exemption.approver）。

        Returns:
            bool: 找到 pending 豁免并批准返回 True；不存在/非 pending 返回 False。
        """
        if not approver:
            raise ExemptionError("审批人 approver 不能为空（审批闭环需留痕）")
        all_ex = self.load()
        for idx, e in enumerate(all_ex):
            if e.exemption_id != exemption_id:
                continue
            if e.status != "pending":
                return False
            approved = replace(e, status="approved", approver=approver)
            all_ex[idx] = approved
            self._save(all_ex)
            self._audit("exemption_approve", approver, approved.to_dict())
            return True
        return False

    def revoke(self, exemption_id: str) -> bool:
        """撤销一条豁免（软删除：status→revoked，记录保留可追溯；不落物理删除）。

        Returns:
            bool: 找到并撤销返回 True；无此 ID（或已 revoked）返回 False。
        """
        all_ex = self.load()
        for idx, e in enumerate(all_ex):
            if e.exemption_id != exemption_id:
                continue
            if e.status == "revoked":
                return False
            revoked = replace(e, status="revoked")
            all_ex[idx] = revoked
            self._save(all_ex)
            self._audit("exemption_revoke", "exemption", revoked.to_dict())
            return True
        return False

    def _audit(self, kind: str, actor: str, detail: Dict[str, Any]) -> None:
        """落 audit_log（append-only 哈希链）；失败仅 error 不阻断主流程。"""
        try:
            AuditLogStore().append(
                "leakage_gate", kind, actor=actor, detail=detail,
            )
        except Exception as e:  # noqa: BLE001 - 审计写失败不阻断主流程
            logger.error(f"豁免落审计失败（不阻断主流程）: {e}")

    def list_active(self, now: Optional[datetime] = None) -> List[Exemption]:
        """未过期且在有效期内的豁免。"""
        return [e for e in self.load() if e.is_active(now)]

    def list_expired(self, now: Optional[datetime] = None) -> List[Exemption]:
        """已过期（或尚未生效）的豁免。"""
        return [e for e in self.load() if not e.is_active(now)]

    def covers(self, violation: Any, now: Optional[datetime] = None) -> bool:
        """是否被某条"有效期内"的豁免覆盖（仅压制未过期豁免）。

        匹配：rule_id 相同 + target 为空（全局）或等于违规策略。
        仅 status=approved 且未过期的豁免压制（#365 审批闭环）；pending/revoked
        与过期豁免均不压制 → 违规保留 → 门禁中心自动拦截（M3-02 验收）。
        gate-error（门禁自检故障）任何情况下不被豁免压制（双保险，grant 已拒）。

        Args:
            violation: 门禁违规（鸭子类型，需有 .rule_id/.strategy_id）。
            now: 判定时点（默认当前）。

        Returns:
            bool: 被"已批准且未过期"豁免覆盖返回 True（违规可被压制）。
        """
        rule = getattr(violation, "rule_id", None)
        strategy = getattr(violation, "strategy_id", "") or ""
        if rule in NON_EXEMPTABLE_RULE_IDS:
            return False  # 门禁自检故障不可豁免（fail-closed，绝不放行）
        for e in self.load():
            if e.status != "approved":
                continue  # pending/revoked 不压制
            if not e.is_active(now):
                continue
            if e.rule_id == rule and (not e.target or e.target == strategy):
                return True
        return False

    def suppress(
        self, violations: Sequence[Any], now: Optional[datetime] = None
    ) -> List[Any]:
        """按已批准且未过期豁免压制违规：被覆盖的移除，其余保留（不达标豁免不压制 → 保留阻断）。"""
        return [v for v in violations if not self.covers(v, now)]


# ── 模块级默认库（gate 中心集成点）──────────────────────────────────────

_default_store: Optional[ExemptionStore] = None


def get_store() -> ExemptionStore:
    """获取模块级默认豁免库（惰性初始化，指向受控持久化台账 database/exemptions.json，
    重启不丢；测试用 set_store 注入临时库隔离）。"""
    global _default_store
    if _default_store is None:
        _default_store = ExemptionStore(path=str(_DEFAULT_LEDGER_PATH))
    return _default_store


def set_store(store: Optional[ExemptionStore]) -> None:
    """覆盖模块级默认豁免库（测试隔离与装配用）。"""
    global _default_store
    _default_store = store


def grant_exemption(exemption: Exemption, actor: str = "exemption") -> Exemption:
    """向模块级默认库登记豁免（便捷入口）。"""
    return get_store().grant(exemption, actor=actor)


def suppress_violations(
    violations: Sequence[Any], now: Optional[datetime] = None
) -> List[Any]:
    """按未过期豁免压制违规（供门禁中心 assert_all_gates_pass 调用）。

    Args:
        violations: 门禁中心汇总的违规列表。
        now: 判定时点（默认当前）。

    Returns:
        List[Any]: 未被未过期豁免覆盖的违规（过期豁免不压制 → 保留阻断）。
    """
    return get_store().suppress(violations, now=now)
