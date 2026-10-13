# 文件名：myquant/realism_gate/manifest.py
"""
realism_gate/manifest.py - RealismManifest 数据契约（P0-01 第一刀）
================================================================

回测真实性门禁的核心数据载体。每次回测运行产出一份清单，由 L3/L4（backtest/
live）在装配期以**数据契约注入**方式采集字段，本模块只负责：

  - 定义字段取值域与 critical 阻断判据（v2.2 §4.2 字段表 + §4.5 阻断矩阵）
  - ``build()`` 完整性校验：缺 critical 字段 → ``ManifestIncompleteError``（fail-closed）
  - ``status`` 推导：ok / degraded / disabled / unverified（§4.2）
  - 序列化（to_dict / from_dict）与跨运行签名（§4.10，剔除 run_id/时间戳的字段哈希）

设计红线（v2.2 §4.3 第三处不同）：
  - **不使用 severity 字段**，只保留 ``critical`` 布尔——唯一阻断判据，无歧义。
  - **配置缺失 = 阻断**（与 leakage_gate 段缺失落默认 True 不同）。
  - **必须落盘**（清单强制写入 results.json 与报告页）。

分层：myquant.realism_gate 登记 MODULE_LAYER L0（test_architecture.py）；
仅依赖 myquant.core（L0 合法），**禁止** import myquant.backtest / myquant.execution /
myquant.data_access（R-02/R-16 跨层红线）。运行时事实由调用方注入。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("myquant.realism_gate.manifest")


# ═══════════════════════════════════════════════
#  异常体系（v2.2 §4.3 出口断言语义）
# ═══════════════════════════════════════════════


class RealismGateError(Exception):
    """出口断言失败异常：有 critical 违规且 enabled=True。

    Attributes:
        violations: 触发阻断的违规明细列表。
        manifest: 对应的 RealismManifest 实例（含完整字段快照）。
    """

    def __init__(self, message: str, violations: List["RealismViolation"],
                 manifest: Optional["RealismManifest"] = None) -> None:
        super().__init__(message)
        self.violations: List[RealismViolation] = violations
        self.manifest: Optional[RealismManifest] = manifest


class ManifestIncompleteError(Exception):
    """清单完整性失败：critical 字段缺失（fail-closed，不允许空值通过）。

    Attributes:
        missing: 缺失的 critical 字段路径列表（如 ["causality.context_sliced"]）。
    """

    def __init__(self, message: str, missing: List[str]) -> None:
        super().__init__(message)
        self.missing: List[str] = missing


# ═══════════════════════════════════════════════
#  违规记录（v2.2：无 severity，只有 critical 布尔）
# ═══════════════════════════════════════════════


@dataclass(frozen=True)
class RealismViolation:
    """单条真实性门禁违规记录。

    Attributes:
        field_path: 违规字段路径（如 "adjust_degraded_symbols"）。
        critical: 是否为 critical 阻断字段（True=未通过即阻断）。
        message: 人类可读的违规描述。
        actual_value: 实际取值（用于审计回溯）。
        decision_time: 判定时点（UTC ISO8601）。
    """

    field_path: str
    critical: bool
    message: str
    actual_value: Any = None
    decision_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ═══════════════════════════════════════════════
#  critical 字段清单（v2.2 §4.2 字段表）
# ═══════════════════════════════════════════════
#  字段路径用点号分隔；build() 校验这些路径在 fields dict 中存在且非 None。

CRITICAL_FIELDS: List[str] = [
    # 元信息
    "run_id",
    "strategy_id",
    "engine_mode_requested",
    "engine_mode_resolved",
    "start_date",
    "end_date",
    "initial_capital",
    # 因果性（P0-07 接入；本刀先占字段，取值由引擎注入）
    "causality.context_sliced",
    "causality.current_bar_ts_enforced",
    "causality.full_sample_declared",
    "causality.guard_injected",
    "causality.context_data_scoped",
    # P0-01 因果性判据正式生效点（#344）：运行期真实采集是否已完成——
    # create_engine 入口占位 False（引擎未运行，causality 判据不参与判定）；
    # run 收尾 _refresh 置 True → checks/causality 判据才生效（阻断语义）
    "causality.assessed_at_run_end",
    # 行情规则
    "price_limit.enabled",
    "price_limit.pct_by_board",
    "price_limit.pre_close_source",
    "suspension.enabled",
    "suspension.basis",
    "t1_position_freeze.enabled",
    "lot_size.enforced",
    "lot_size.value",
    # 流动性
    "liquidity.participation_cap",
    "liquidity.declared_unlimited",
    # 成本
    "slippage.type",
    "slippage.value",
    "slippage.is_zero_cost",
    "commission.buy_rate",
    "commission.sell_rate",
    "commission.min_commission",
    "commission.stamp_tax",
    "commission.transfer_fee",
    "fee_base_includes_slippage",
    "fee_dilution_basis",
    # 数据
    "adjust_effective_by_symbol",
    "adjust_degraded_symbols",
    "quality.nan_price_rows",
    "quality.ohlc_violation_rows",
    "quality.coverage_rate",
    "fingerprint_hash",
    "snapshot_id",
    # P1-12 数据快照 pin：是否适用目录快照（provider 场景 True / 内存预加载·live False）
    "data.snapshot_applicable",
    # P1-12：重跑发现数据目录与 sidecar 基线不一致（True → data_pit critical 阻断）
    "data.snapshot_mismatch",
    # 样本池
    "pool.pit_applied",
    "pool.exclusion_basis",
    "pool.requested_count",
    "pool.admitted_count",
    "pool.dropped_count",
    "pool.drop_reasons",
    "survivorship.discount",
    "survivorship.window",
    "survivorship.status",
    # 结果不变量（P1-15 接入）
    "invariants.price_bounds",
    "invariants.volume_bound",
    "invariants.equity_identity",
    "invariants.cash_non_negative",
    "invariants.t1_consistency",
]

# 非 critical 的警告级字段（不阻断，但记录）
WARNING_FIELDS: List[str] = [
    "calendar_source",
    "engine_consistency.checked",
    "engine_consistency.overall_ok",
    "engine_consistency.pairs",
]

# 方案 §4.2 取值域标注 null 允许的字段：None 不视为缺失（build 通过，状态语义由 _derive_status/§4.5 处理）
ALLOW_NONE_FIELDS: set = {
    "liquidity.participation_cap",       # float|null
    "survivorship.discount",             # float|null
    "lot_size.value",                    # P0-08 子项③：撮合层（SimBroker）无整手执行单位 → None（S-11 读不到写 None，禁止写 True）
    "fingerprint_hash",                  # str|null：null 即 unverified（方案 §4.2 L474）
    "snapshot_id",                       # str|null：null 即 unverified（方案 §4.2 L474）
}

# 取值域枚举（build 时校验）
ENUM_DOMAINS: Dict[str, List[str]] = {
    "price_limit.pre_close_source": ["data_col", "shift_approx", "none"],
    "suspension.basis": ["volume_zero", "data_col", "none"],
    "fee_dilution_basis": ["per_order", "per_bar"],
    "calendar_source": ["real_calendar", "data_union", "unknown"],
    "pool.exclusion_basis": ["as_of", "today"],
    "survivorship.status": ["ok", "unverified", "degraded", "unknown"],
}


# ═══════════════════════════════════════════════
#  RealismManifest 数据契约
# ═══════════════════════════════════════════════


@dataclass
class RealismManifest:
    """回测真实性清单（v2.2 §4.2）。

    持有一份扁平 + 分组嵌套的字段 dict。L3/L4 在引擎装配期通过
    ``set_field`` / ``update_group`` 注入运行时事实；``build()`` 做完整性校验。

    Attributes:
        fields: 字段 dict（点号路径 → 扁平；分组键 → 嵌套 dict）。
        status: 清单状态（ok/degraded/disabled/unverified），build 后推导。
        violations: build 后检出的违规列表。
        sealed: 是否已通过出口断言并封印（两阶段握手 §4.4 第⑤步）。
    """

    fields: Dict[str, Any] = field(default_factory=dict)
    status: str = "unverified"
    violations: List[RealismViolation] = field(default_factory=list)
    sealed: bool = False

    # ── 字段注入 ──────────────────────────────────────────────

    def set_field(self, path: str, value: Any) -> None:
        """注入单点字段（支持点号分组路径，如 "causality.context_sliced"）。

        Args:
            path: 字段路径（点号分隔分组）。
            value: 取值（不做类型校验，由调用方负责）。
        """
        parts = path.split(".")
        node = self.fields
        for p in parts[:-1]:
            if p not in node or not isinstance(node[p], dict):
                node[p] = {}
            node = node[p]
        node[parts[-1]] = value

    def get_field(self, path: str, default: Any = None) -> Any:
        """读取单点字段（点号路径）；不存在返回 default。"""
        node: Any = self.fields
        for p in path.split("."):
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                return default
        return node

    def update_group(self, group: str, values: Dict[str, Any]) -> None:
        """批量注入某分组字段（如 group="causality", values={...}）。"""
        if group not in self.fields or not isinstance(self.fields[group], dict):
            self.fields[group] = {}
        self.fields[group].update(values)

    # ── 完整性校验（fail-closed）──────────────────────────────

    def build(self) -> "RealismManifest":
        """完整性校验 + status 推导。

        遍历 CRITICAL_FIELDS，缺失或值为 None → 收集到 missing；
        有 missing → 抛 ManifestIncompleteError（fail-closed）。

        校验通过后按 §4.5 阻断矩阵推导 status：
          - 无任何 degraded/unverified 信号 → ok
          - 有 adjust_degraded_symbols 非空 / pool.exclusion_basis=="today" → degraded
          - fingerprint_hash/snapshot_id 为 None → unverified（P0-08 子项③起
            这两字段允许 None（方案 §4.2 str|null），build 不视为缺失，状态推导走 unverified）
          - enabled=False 且配置段缺失 → disabled

        Returns:
            self（链式调用）。

        Raises:
            ManifestIncompleteError: critical 字段缺失时抛出。
        """
        missing: List[str] = []
        for path in CRITICAL_FIELDS:
            val = self.get_field(path, None)
            if val is None and path not in ALLOW_NONE_FIELDS:
                missing.append(path)
        if missing:
            raise ManifestIncompleteError(
                f"RealismManifest 不完整：{len(missing)} 个 critical 字段缺失"
                f"（fail-closed，不允许空值通过）",
                missing=missing,
            )

        # 枚举取值域校验
        for path, allowed in ENUM_DOMAINS.items():
            val = self.get_field(path, None)
            if val is not None and val not in allowed:
                self.violations.append(RealismViolation(
                    field_path=path,
                    critical=True,
                    message=f"取值 {val!r} 不在允许域 {allowed} 内",
                    actual_value=val,
                ))

        # status 推导（§4.5 阻断矩阵）
        self.status = self._derive_status()
        return self

    def _derive_status(self) -> str:
        """按 §4.5 阻断矩阵推导清单状态。"""
        degraded_symbols = self.get_field("adjust_degraded_symbols", [])
        exclusion_basis = self.get_field("pool.exclusion_basis", "as_of")
        if isinstance(degraded_symbols, list) and len(degraded_symbols) > 0:
            return "degraded"
        if exclusion_basis == "today":
            return "degraded"
        # P1-12：快照适用（provider 场景）但指纹缺失 → unverified
        # （方案 §4.5 L542：fingerprint_hash/snapshot_id 为 null → unverified）
        if (
            self.get_field("data.snapshot_applicable", False)
            and self.get_field("fingerprint_hash", None) is None
        ):
            return "unverified"
        return "ok"

    # ── 跨运行签名（§4.10）────────────────────────────────────

    def manifest_signature(self) -> str:
        """计算跨运行可比性签名（§4.10）。

        剔除 ``run_id`` / 时间戳类字段后对剩余字段做规范化 JSON 哈希。
        用于 param_optimizer/batch_backtest 聚合时以签名为分组键，
        跨签名禁止合并排行。

        Returns:
            sha256 十六进制串（前 16 字符）。
        """
        # 剔除 run_id（每次运行唯一）与任何 *_ts/timestamp 字段
        def _purge(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {
                    k: _purge(v)
                    for k, v in sorted(obj.items())
                    if k not in ("run_id", "decision_time", "created_ts", "sealed")
                }
            if isinstance(obj, list):
                return [_purge(x) for x in obj]
            return obj

        canonical = _purge(self.fields)
        raw = repr(canonical).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    # ── 序列化 ──────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 落盘的 dict。"""
        return {
            "fields": self.fields,
            "status": self.status,
            "sealed": self.sealed,
            "violations": [
                {
                    "field_path": v.field_path,
                    "critical": v.critical,
                    "message": v.message,
                    "actual_value": v.actual_value,
                    "decision_time": v.decision_time,
                }
                for v in self.violations
            ],
            "manifest_signature": self.manifest_signature(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RealismManifest":
        """从落盘 dict 反序列化。"""
        violations = [
            RealismViolation(
                field_path=v["field_path"],
                critical=v["critical"],
                message=v["message"],
                actual_value=v.get("actual_value"),
                decision_time=v.get("decision_time", ""),
            )
            for v in data.get("violations", [])
        ]
        return cls(
            fields=data.get("fields", {}),
            status=data.get("status", "unverified"),
            violations=violations,
            sealed=data.get("sealed", False),
        )

    def seal(self) -> None:
        """封印清单（两阶段握手 §4.4 第⑤步：assert_realism_pass 通过后调用）。"""
        self.sealed = True

    def is_sealed(self) -> bool:
        """返回清单是否已封印（引擎入口处断言，§4.4 第⑤步）。"""
        return self.sealed
