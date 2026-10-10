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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from myquant.core.audit_log import AuditLogStore

logger = logging.getLogger("myquant.leakage_gate.exemption")


class ExemptionError(RuntimeError):
    """豁免流错误（非法有效期 / 台账读写失败 / 未找到豁免）。"""


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
        rule_id: 豁免覆盖的规则 ID（R01~R11）。
        approver: 审批人。
        valid_from: 生效日（YYYY-MM-DD，含）。
        valid_until: 到期日（YYYY-MM-DD，含；到期后不压制违规 → 门禁拦截）。
        target: 适用策略名；空串 = 全局适用。
    """

    exemption_id: str
    reason: str
    rule_id: str
    approver: str
    valid_from: str
    valid_until: str
    target: str = ""

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
        """登记豁免并落 audit_log（append-only 哈希链）。

        Args:
            exemption: 豁免记录（valid_from <= valid_until 否则报错）。
            actor: 审计 actor（默认 exemption；approver 记录在 detail）。

        Returns:
            Exemption: 已登记的豁免。

        Raises:
            ExemptionError: 有效期非法（valid_from > valid_until）或 ID 重复。
        """
        if _to_date(exemption.valid_from) > _to_date(exemption.valid_until):
            raise ExemptionError(
                f"豁免有效期非法: valid_from {exemption.valid_from} > valid_until "
                f"{exemption.valid_until}")
        existing = [e.exemption_id for e in self.load()]
        if exemption.exemption_id in existing:
            raise ExemptionError(f"豁免 ID 重复: {exemption.exemption_id}")
        all_ex = self.load() + [exemption]
        self._save(all_ex)
        try:
            AuditLogStore().append(
                "leakage_gate", "exemption_grant", actor=actor,
                detail=exemption.to_dict(),
            )
        except Exception as e:  # noqa: BLE001 - 审计写失败不阻断主流程（防未来：已发生即记录，失败仅 error）
            logger.error(f"豁免落审计失败（不阻断主流程）: {e}")
        return exemption

    def revoke(self, exemption_id: str) -> bool:
        """撤销一条豁免（从台账移除，无此 ID 返回 False）。"""
        all_ex = self.load()
        kept = [e for e in all_ex if e.exemption_id != exemption_id]
        if len(kept) == len(all_ex):
            return False
        self._save(kept)
        return True

    def list_active(self, now: Optional[datetime] = None) -> List[Exemption]:
        """未过期且在有效期内的豁免。"""
        return [e for e in self.load() if e.is_active(now)]

    def list_expired(self, now: Optional[datetime] = None) -> List[Exemption]:
        """已过期（或尚未生效）的豁免。"""
        return [e for e in self.load() if not e.is_active(now)]

    def covers(self, violation: Any, now: Optional[datetime] = None) -> bool:
        """是否被某条"有效期内"的豁免覆盖（仅压制未过期豁免）。

        匹配：rule_id 相同 + target 为空（全局）或等于违规策略。
        过期豁免不压制 → 违规保留 → 门禁中心自动拦截（M3-02 验收）。

        Args:
            violation: 门禁违规（鸭子类型，需有 .rule_id/.strategy_id）。
            now: 判定时点（默认当前）。

        Returns:
            bool: 被未过期豁免覆盖返回 True（违规可被压制）。
        """
        rule = getattr(violation, "rule_id", None)
        strategy = getattr(violation, "strategy_id", "") or ""
        for e in self.load():
            if not e.is_active(now):
                continue
            if e.rule_id == rule and (not e.target or e.target == strategy):
                return True
        return False

    def suppress(
        self, violations: Sequence[Any], now: Optional[datetime] = None
    ) -> List[Any]:
        """按未过期豁免压制违规：被覆盖的移除，其余保留（过期豁免不压制 → 保留阻断）。"""
        return [v for v in violations if not self.covers(v, now)]


# ── 模块级默认库（gate 中心集成点）──────────────────────────────────────

_default_store: Optional[ExemptionStore] = None


def get_store() -> ExemptionStore:
    """获取模块级默认豁免库（惰性初始化，空库=无豁免压制）。"""
    global _default_store
    if _default_store is None:
        _default_store = ExemptionStore()
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
