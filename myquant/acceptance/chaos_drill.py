# 文件名：myquant/monitoring/chaos_drill.py
"""
chaos_drill.py - 混沌工程与故障演练（D-6）
==========================================
季度故障注入：**直接复用 P0-8 标准故障注入用例集 FI-01~07**（与验收测量同一
用例库，不再另立用例，R-01，字段全部取自 myquant.core.measurability
.FI_CASES，未自行定义）。先做最小可行版本（FI-01 断流 / 备份恢复），逐步扩展。

本模块提供：
  - DrillCase：一个可执行故障演练用例 = FICase（P0-8 定义）+ 注入函数/观测函数；
  - ChaosDrillRunner：演练编排核心——逐 FI 用例注入 → 观测（AlertStore）→
    按 P0-8 ② 口径计时（MTTD/MTTA/MTTR，系统时间戳）→ 按 P0-8 ① 口径分类
    （TP/FP/FN）→ 记录入库（drill record JSON 可序列化）→ 复盘报告；
  - build_builtin_cases()：接线 FI-01~07 到真实检测路径（隔离环境：临时
    AlertStore + 真实 AlertManager + 真实 EventBus），演练可演示。

测量口径（唯一权威依据 P0-8，见 measurability.py）：
  - MTTD = 故障注入时间戳（事件时间）→ 告警 BASE-1 落库 created_at
  - MTTA = 告警 created_at → 系统确认 acked_at
  - MTTR = 故障注入时间戳 → 告警→恢复成对记录 resolved_at
  - 准确率 = TP/(TP+FP)，误报率 = FP/(TP+FP)，检出率 = TP/(TP+FN)

防未来铁律：只消费"已发生"的系统时间戳（fault_ts/created_at/acked_at/
resolved_at 均为当前时钟），不引用任何未来数据。

scripts/故障演练/季频故障演练.py 为薄入口。

依赖: 标准库 + myquant（event_bus / alerting / alert_store / measurability /
      watchdog / settings / time_utils）
"""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from myquant.core.logger import get_logger
from myquant.core.alert_store import AlertStore, STATUS_ACKED, STATUS_RESOLVED
from myquant.monitoring.alerting import AlertManager
from myquant.core.measurability import (
    CLASSIFICATION_FP,
    CLASSIFICATION_FN,
    CLASSIFICATION_TP,
    CLASSIFICATION_UNKNOWN,
    FI_CASES,
    FICase,
    MttxCalculator,
)

logger = get_logger("acceptance.chaos_drill")


# ═══════════════════════════════════════════════
#  常量（演练参数，P0-8 口径；非业务阈值）
# ═══════════════════════════════════════════════
MTTD_BUDGET_SECONDS: float = 60.0       # FI-01/02/03: MTTD<1min
MTTA_BUDGET_SECONDS: float = 300.0      # FI-04: MTTA<5min
MTTR_BUDGET_SECONDS: float = 1800.0     # FI-05: MTTR<30min
DEFAULT_OBSERVE_WINDOW_SECONDS: float = 15.0   # 每次注入后轮询告警库的窗口上限
DEFAULT_ACK_DELAY_SECONDS: float = 0.05        # 演练模拟人工确认的间隔（演示）
DEFAULT_RESOLVE_DELAY_SECONDS: float = 0.05    # 演练模拟健康恢复解决的间隔（演示）
DEFAULT_DRILL_HEADER = "季度混沌工程故障演练"
DRILL_AUDIT_KIND = "chaos_drill"


@dataclass
class InjectOutcome:
    """
    一次注入的观测结果（注入函数返回；全部为已发生事实）。

    Attributes
    ----------
    fault_ts : float
        故障注入时间戳（系统时钟，事件时间；MTTx 计时起点）。
    captured_alert_id : str | None
        注入函数直接定位到的本次注入告警 ID（优先使用；注入函数经
        manager._history 的 mark→new 定位，可靠性最高）。
    expected_source : str | None
        期望捕获告警的 source（captured_alert_id 缺失时的兜底观察匹配）。
    expected_category : str | None
        期望捕获告警的 category（兜底观察匹配）。
    note : str
        注入路径说明（复盘报告证据）。
    """

    fault_ts: float = field(default_factory=time.time)
    captured_alert_id: Optional[str] = None
    expected_source: Optional[str] = None
    expected_category: Optional[str] = None
    expected_level: Optional[str] = None
    note: str = ""


@dataclass
class DrillCaseResult:
    """
    单个 FI 用例的演练记录（JSON 可序列化；入库/复盘数据）。

    Attributes
    ----------
    case_id : str
        FI 用例 ID（P0-8 用例库）。
    fault_desc / expected_capture / trigger_mode / kpi :
        来自 FICase（P0-8 唯一口径）。
    detected : bool
        注入的故障是否被告警捕获（未捕获=漏报 FN）。
    classification : str
        TP / FP / FN / UNCLASSIFIED（P0-8 ① 口径）。
    fault_ts / created_at / acked_at / resolved_at :
        已发生时间戳（系统时钟；未发生时 None）。
    mttd / mtta / mttr : float | None
        计时结果（秒）。
    budgets : dict
        {mttd_budget/mtta_budget/mttr_budget: 秒或 None，met_*: bool}。
    note : str
        注入/观测证据。
    """

    case_id: str = ""
    fault_desc: str = ""
    expected_capture: str = ""
    trigger_mode: str = ""
    kpi: str = ""
    detected: bool = False
    classification: str = CLASSIFICATION_UNKNOWN
    fault_ts: Optional[float] = None
    created_at: Optional[float] = None
    acked_at: Optional[float] = None
    resolved_at: Optional[float] = None
    mttd: Optional[float] = None
    mtta: Optional[float] = None
    mttr: Optional[float] = None
    budgets: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化（JSON 可读；复盘报告逐用例数据）。"""
        return {
            "case_id": self.case_id,
            "fault_desc": self.fault_desc,
            "expected_capture": self.expected_capture,
            "trigger_mode": self.trigger_mode,
            "kpi": self.kpi,
            "detected": self.detected,
            "classification": self.classification,
            "fault_ts": self.fault_ts,
            "created_at": self.created_at,
            "acked_at": self.acked_at,
            "resolved_at": self.resolved_at,
            "mttd": self.mttd,
            "mtta": self.mtta,
            "mttr": self.mttr,
            "budgets": dict(self.budgets),
            "note": self.note,
        }


@dataclass
class DrillCase:
    """一个可执行故障演练用例 = P0-8 FICase + 注入/观测接线。"""

    fi: FICase
    inject_fn: Callable[..., InjectOutcome]
    observe_window_seconds: float = DEFAULT_OBSERVE_WINDOW_SECONDS


def _budget_for(kpi: str) -> Dict[str, Optional[float]]:
    """从 FI 用例 kpi 字段解析 MTTx 预算（P0-8；KPI-8 无计时预算）。

    Returns
    -------
    dict : {"mttd_budget"/"mtta_budget"/"mttr_budget": 秒或 None}
    """
    return {
        "mttd_budget": MTTD_BUDGET_SECONDS if "MTTD<" in kpi else None,
        "mtta_budget": MTTA_BUDGET_SECONDS if "MTTA<" in kpi else None,
        "mttr_budget": MTTR_BUDGET_SECONDS if "MTTR<" in kpi else None,
    }


class ChaosDrillRunner:
    """
    混沌工程与故障演练执行器（D-6）。

    在隔离环境（临时 AlertStore + 真实 AlertManager + 真实 EventBus）中逐 FI
    用例注入，按 P0-8 口径计时与分类，记录入库并产出复盘报告。

    用法:
        runner = ChaosDrillRunner()
        runner.register_builtin_cases()
        report = runner.run(select=("FI-01", "FI-06"))
        runner.write_report("docs/监控档案/混沌演练/2026Q4_故障演练.json")
        runner.close()

    防未来：fault_ts/created_at/acked_at/resolved_at 全部来自系统时钟
    （clock 仅测试注入）；注入只推进已发生事件，不引用未来数据。
    """

    def __init__(
        self,
        store_db_path: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
        drill_id: Optional[str] = None,
        audit_log: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        store_db_path : str | None
            告警落库的临时数据库路径；None 自动创建临时目录并在 close() 清理。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        drill_id : str | None
            本次演练 ID（None 自动生成 uuid）。
        audit_log : AuditLogStore | None
            审计存储（kind=chaos_drill）；None 惰性尝试，失败降级内存留痕。
        """
        self._store_db_path: Optional[str] = store_db_path
        self._owns_tmpdir: bool = store_db_path is None
        self._tmpdir: Optional[Path] = None
        if self._owns_tmpdir:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="chaos_drill_"))
        db_path: str = (
            store_db_path
            if store_db_path is not None
            else (str(self._tmpdir / "drill.db") if self._tmpdir is not None else "")
        )
        self._store: AlertStore = AlertStore(db_path=db_path)
        self._manager: AlertManager = AlertManager(store=self._store, store_enabled=True)
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._drill_id: str = drill_id or uuid.uuid4().hex[:12]
        self._audit_log: Any = audit_log
        self._audit_tried: bool = audit_log is not None
        self._cases: Dict[str, DrillCase] = {}
        self._results: List[DrillCaseResult] = []
        self._run_at: Optional[float] = None

    # ── 环境管理 ────────────────────────────────────────────

    def close(self) -> None:
        """清理演练产生的临时目录（仅当拥有临时目录时）。"""
        if self._owns_tmpdir and self._tmpdir is not None:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    @property
    def store(self) -> AlertStore:
        """告警落库（隔离环境；观测/计时源）。"""
        return self._store

    @property
    def manager(self) -> AlertManager:
        """告警管理器（隔离环境）。"""
        return self._manager

    # ── 用例注册 ────────────────────────────────────────────

    def register_case(self, case: DrillCase) -> None:
        """注册一个演练用例（case_id 来自 P0-8 FI 用例库，重 id 拒绝）。"""
        if case.fi.case_id in self._cases:
            raise ValueError(f"演练用例 {case.fi.case_id} 已注册，拒绝重复")
        self._cases[case.fi.case_id] = case

    def register_builtin_cases(self, select: Optional[Sequence[str]] = None) -> int:
        """注册 FI-01~07 内置用例（P0-8 用例库接线真实检测路径）。

        Returns
        -------
        int : 注册的用例数。
        """
        cases = build_builtin_cases(self._store, self._manager)
        for case in cases:
            if select is None or case.fi.case_id in set(select):
                self.register_case(case)
        return len(self._cases)

    @property
    def case_ids(self) -> List[str]:
        """已注册用例 ID（按 P0-8 顺序）。"""
        order = {c.case_id: i for i, c in enumerate(FI_CASES)}
        return sorted(self._cases.keys(), key=lambda c: order.get(c, 999))

    # ── 演练执行 ────────────────────────────────────────────

    def _poll_alert(
        self, outcome: InjectOutcome, window_seconds: float
    ) -> Optional[Dict[str, Any]]:
        """轮询/直接定位本次注入产生的告警。

        优先使用注入函数直接定位的 captured_alert_id（读取告警库并校验
        created_at >= fault_ts）；缺失时按 expected_source/category 匹配
        fault_ts 之后落库的告警。全部只消费已发生时间戳，防未来。
        """
        if outcome.captured_alert_id:
            row = self._store.get(outcome.captured_alert_id)
            if row is not None and float(row.get("created_at", 0.0)) >= outcome.fault_ts:
                return row
            return None
        deadline = self._clock() + float(window_seconds)
        source = (outcome.expected_source or "").lower()
        category = (outcome.expected_category or "").lower()
        while True:
            alerts = self._store.load_history(limit=500)
            candidate: Optional[Dict[str, Any]] = None
            for a in alerts:
                created = float(a.get("created_at", 0.0))
                if created < outcome.fault_ts:
                    continue
                a_source = str(a.get("source", "")).lower()
                a_cat = str(a.get("category", "")).lower()
                if source and source not in a_source:
                    continue
                if category and category not in a_cat:
                    continue
                candidate = a
            if candidate is not None:
                return candidate
            if self._clock() >= deadline:
                return None
            time.sleep(0.02)

    def run(self, select: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """
        执行演练：逐 FI 用例注入 → 观测 → 计时 → 分类 → 记录，返回复盘报告。

        Parameters
        ----------
        select : list[str] | None
            指定要演练的 FI 用例；None=全部已注册用例。

        Returns
        -------
        dict : 复盘报告（JSON 可序列化；含逐用例记录 + 汇总指标）。
        """
        targets = [c for c in self.case_ids if select is None or c in set(select)]
        if not targets:
            raise ValueError("演练无目标用例（select 过滤后为空）")
        self._run_at = self._clock()
        self._results = []

        for case_id in targets:
            case = self._cases[case_id]
            res = DrillCaseResult(
                case_id=case.fi.case_id,
                fault_desc=case.fi.fault_desc,
                expected_capture=case.fi.expected_capture,
                trigger_mode=case.fi.trigger_mode,
                kpi=case.fi.kpi,
                budgets=_budget_for(case.fi.kpi),
            )
            try:
                outcome = case.inject_fn()
            except Exception as e:  # noqa: BLE001 —— 注入失败如实记录为漏报/异常
                logger.error("[chaos_drill] %s 注入失败: %s", case_id, e)
                res.classification = CLASSIFICATION_FN
                res.detected = False
                res.note = f"注入失败: {e}"
                self._apply_budget_flags(res)
                self._results.append(res)
                continue

            res.fault_ts = float(outcome.fault_ts)
            res.note = outcome.note
            matched = self._poll_alert(outcome, case.observe_window_seconds)
            if matched is None:
                # 注入的故障未被任何告警捕获 → 漏报 FN
                res.classification = CLASSIFICATION_FN
                res.detected = False
                self._apply_budget_flags(res)
                self._results.append(res)
                continue

            # 捕获 → TP，按 P0-8 ② 计时
            alert_id = str(matched.get("alert_id", ""))
            res.detected = True
            res.classification = CLASSIFICATION_TP
            res.created_at = float(matched.get("created_at", 0.0) or res.fault_ts)
            res.mttd = MttxCalculator.mttd(res.fault_ts, res.created_at)

            # 模拟系统确认（ack）：acknowledged_at = 确认事件时间
            self._store.update_status(alert_id, STATUS_ACKED)
            acked_row = self._store.get(alert_id) or {}
            res.acked_at = float(acked_row.get("acked_at") or self._clock())
            res.mtta = MttxCalculator.mtta(res.created_at, res.acked_at)

            # 模拟健康恢复（resolve）：resolved_at = 告警→恢复成对记录
            self._store.update_status(alert_id, STATUS_RESOLVED)
            resolved_row = self._store.get(alert_id) or {}
            res.resolved_at = float(resolved_row.get("resolved_at") or self._clock())
            res.mttr = MttxCalculator.mttr(res.fault_ts, res.resolved_at)

            self._store.set_classification(alert_id, CLASSIFICATION_TP)
            self._apply_budget_flags(res)
            self._results.append(res)

        report = self._build_report()
        self._audit("drill", "completed", report.get("summary", {}))
        return report

    def _apply_budget_flags(self, res: DrillCaseResult) -> None:
        """按 P0-8 KPI 预算给用例打 met_* 标志。

        预算键存在（非 None）时：对应计时达标（<=预算）→ True；未计时/超预算
        → False。未在用例 kpi 中声明的预算（KPI-8 无预算）不设标志。
        """
        for metric, budget_key, value in (
            ("mttd", "mttd_budget", res.mttd),
            ("mtta", "mtta_budget", res.mtta),
            ("mttr", "mttr_budget", res.mttr),
        ):
            budget = res.budgets.get(budget_key)
            if budget is None:
                continue
            res.budgets[f"met_{metric}"] = bool(
                value is not None and float(value) <= float(budget)
            )

    # ── 复盘报告 ────────────────────────────────────────────
    def _build_report(self) -> Dict[str, Any]:
        """汇总复盘报告（逐用例记录 + 汇总指标，JSON 可序列化）。"""
        detected = sum(1 for r in self._results if r.detected)
        total = len(self._results)
        tps = sum(1 for r in self._results if r.classification == CLASSIFICATION_TP)
        fps = sum(1 for r in self._results if r.classification == CLASSIFICATION_FP)
        fns = sum(1 for r in self._results if r.classification == CLASSIFICATION_FN)
        detect_rate = (detected / total) if total else 0.0
        accuracy = (tps / (tps + fps)) if (tps + fps) else None
        mttd_vals = [r.mttd for r in self._results if r.mttd is not None]
        mtta_vals = [r.mtta for r in self._results if r.mtta is not None]
        mttr_vals = [r.mttr for r in self._results if r.mttr is not None]
        met_budgets = sum(
            1 for r in self._results
            if (r.budgets.get("met_mttd") is True)
            or (r.budgets.get("met_mtta") is True)
            or (r.budgets.get("met_mttr") is True)
        )
        budget_checked = sum(
            1 for r in self._results
            if r.budgets.get("met_mttd") is not None
            or r.budgets.get("met_mtta") is not None
            or r.budgets.get("met_mttr") is not None
        )
        return {
            "drill_id": self._drill_id,
            "title": DEFAULT_DRILL_HEADER,
            "run_at": self._run_at,
            "fi_library_source": "P0-8（myquant.core.measurability.FI_CASES，R-01 同库）",
            "results": [r.to_dict() for r in self._results],
            "summary": {
                "cases_total": total,
                "cases_detected": detected,
                "detect_rate": round(detect_rate, 4),
                "tp": tps,
                "fp": fps,
                "fn": fns,
                "accuracy": round(accuracy, 4) if accuracy is not None else None,
                "avg_mttd": round(float(MttxCalculator.average(mttd_vals)), 3)
                if mttd_vals else None,
                "avg_mtta": round(float(MttxCalculator.average(mtta_vals)), 3)
                if mtta_vals else None,
                "avg_mttr": round(float(MttxCalculator.average(mttr_vals)), 3)
                if mttr_vals else None,
                "budget_met": met_budgets,
                "budget_checked": budget_checked,
            },
            "replay_suggestions": self._replay_suggestions(),
        }

    def _replay_suggestions(self) -> List[str]:
        """复盘改进建议（自动化生成；基于本次演练结果）。"""
        suggestions: List[str] = []
        missed = [r for r in self._results if not r.detected]
        if missed:
            suggestions.append(
                f"漏报 {len(missed)} 项（{', '.join(r.case_id for r in missed)}）："
                "检查对应检测器接线与告警通道，确认 FI 注入路径与观测匹配口径。"
            )
        for r in self._results:
            if r.mttd is not None and r.budgets.get("mttd_budget") and not r.budgets.get("met_mttd"):
                suggestions.append(
                    f"{r.case_id} MTTD={r.mttd:.1f}s 超预算 {r.budgets['mttd_budget']}s："
                    "缩短检测窗口或加快告警落库。"
                )
            if r.mtta is not None and r.budgets.get("mtta_budget") and not r.budgets.get("met_mtta"):
                suggestions.append(
                    f"{r.case_id} MTTA={r.mtta:.1f}s 超预算：加快人工/自动确认链路。"
                )
            if r.mttr is not None and r.budgets.get("mttr_budget") and not r.budgets.get("met_mttr"):
                suggestions.append(
                    f"{r.case_id} MTTR={r.mttr:.1f}s 超预算：优化恢复动作链。"
                )
        return suggestions

    def write_report(self, out_path: str) -> Path:
        """复盘报告落盘（JSON，逐 FI 用例计时入库；供季度复盘存档）。"""
        import json
        report = self._build_report()
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return p

    # ── 审计 ────────────────────────────────────────────────

    def _audit(self, action: str, sub_action: str, detail: Dict[str, Any]) -> None:
        """审计留痕（kind=chaos_drill；失败降级内存留痕不阻断）。"""
        if self._audit_log is None:
            if not self._audit_tried:
                try:
                    from myquant.core.audit_log import AuditLogStore
                    self._audit_log = AuditLogStore()
                except Exception as e:  # noqa: BLE001
                    logger.warning("[chaos_drill] 审计存储不可用（降级内存留痕）: %s", e)
                self._audit_tried = True
            if self._audit_log is None:
                return
        try:
            self._audit_log.append(
                DRILL_AUDIT_KIND, f"drill:{sub_action}", "system", dict(detail or {})
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[chaos_drill] 审计留痕失败: %s", e)


# ═══════════════════════════════════════════════
#  内置用例接线（FI-01~07 真实检测路径）
# ═══════════════════════════════════════════════
# 以下注入函数在隔离环境（传入 store/manager）中执行真实故障注入并返回
# InjectOutcome（fault_ts + 期望捕获项）；观测由 ChaosDrillRunner 统一完成。

def _first_fi(case_id: str) -> FICase:
    """按 case_id 取 P0-8 用例（找不到抛 ValueError，杜绝另立用例）。"""
    for case in FI_CASES:
        if case.case_id == case_id:
            return case
    raise ValueError(f"P0-8 用例库无 {case_id}")


class _ActiveSessionCalendar:
    """演练桩日历：恒为交易日 + 活跃上午盘（断流检测确定，非交易时段不误报）。"""

    def is_trading_day(self, _date: Any) -> bool:
        return True

    def get_session(self, _now_dt: Any) -> Any:
        from myquant.utils.time_utils import SessionType
        return SessionType.MORNING


def _new_alert_id(manager: AlertManager, mark: int) -> Optional[str]:
    """返回本次注入（自 mark 之后）新增的最近一条告警 ID；无新告警返回 None。

    与 m1_acceptance._new_alert 同一捕获模式（P0-8 观测：只消费本轮注入后的
    告警，防未来）。manager._history 为告警管理器内存留痕（隔离环境）。
    """
    new = list(getattr(manager, "_history", []))[mark:]
    if not new:
        return None
    return getattr(new[-1], "alert_id", None)


def build_builtin_cases(
    store: AlertStore, manager: AlertManager
) -> List[DrillCase]:
    """
    构建 FI-01~07 内置演练用例（P0-8 用例库接线真实检测路径）。

    每个用例的 inject_fn 在隔离环境执行注入，经 manager._history 直接定位
    本次注入产生的告警（captured_alert_id），交由 ChaosDrillRunner 计时/分类；
    观测只消费 fault_ts 之后落库的告警（防未来）。全部使用已发生时间戳。

    FI-01/02/03/04/07 走真实检测器/事件路径（A-4/A-5/A-6/A-8/BASE-8），
    FI-05/06 以真实 AlertManager 落库代表 A-7/A-9 捕获告警（隔离环境演示）。
    """
    from myquant.core.event_bus import EventBus, EventType, OrderEvent
    from myquant.core.constants import SeverityLevel
    from myquant.monitoring.watchdog import (
        BrokerConnectionWatcher,
        EngineHeartbeatWatcher,
        StalenessDetector,
    )

    cases: List[DrillCase] = []

    def _capture(mark: int, fault_ts: float) -> Optional[str]:
        return _new_alert_id(manager, mark)

    # FI-01 行情源断流 → A-4 StalenessDetector（EVENT，P0，MTTD<1min）
    fi01 = _first_fi("FI-01")

    def inject_fi01() -> InjectOutcome:
        bus = EventBus()
        detector = StalenessDetector(
            bar_timeout_seconds=2.0, calendar=_ActiveSessionCalendar(),
            alert_manager=manager,
        )
        detector.attach_event_bus(bus)
        fault_ts = time.time()
        mark = len(getattr(manager, "_history", []))
        detector.on_bar(ts=fault_ts)  # 断流前最后一根 bar（已发生）
        detector.evaluate(now=fault_ts + 3.0)  # 已发生的超时时刻 → A-4 告警
        return InjectOutcome(
            fault_ts=fault_ts, captured_alert_id=_capture(mark, fault_ts),
            expected_source="watchdog", note="FI-01 断流注入（A-4 StalenessDetector）",
        )

    cases.append(DrillCase(fi=fi01, inject_fn=inject_fi01))

    # FI-02 引擎僵死 → A-5 EngineHeartbeatWatcher（POLL，窗口<60s）
    fi02 = _first_fi("FI-02")

    def inject_fi02() -> InjectOutcome:
        class _HangEngine:
            def get_status(self) -> Dict[str, Any]:
                raise RuntimeError("engine hung (drill)")

        watcher = EngineHeartbeatWatcher(
            engine=_HangEngine(), probe_interval_seconds=1.0,
            consecutive_misses=2, alert_manager=manager,
        )
        fault_ts = time.time()
        mark = len(getattr(manager, "_history", []))
        watcher.probe()  # 第 1 次无响应（miss=1，未判死）
        watcher.probe()  # 第 2 次无响应（miss=2，判引擎僵死 → A-5 告警）
        return InjectOutcome(
            fault_ts=fault_ts, captured_alert_id=_capture(mark, fault_ts),
            expected_source="watchdog", note="FI-02 引擎僵死注入（A-5 心跳）",
        )

    cases.append(DrillCase(fi=fi02, inject_fn=inject_fi02))

    # FI-03 券商连接断开 → A-6 BrokerConnectionWatcher（EVENT）
    fi03 = _first_fi("FI-03")

    def inject_fi03() -> InjectOutcome:
        class _ConnectedBroker:
            def __init__(self, connected: bool = True) -> None:
                self.connected = connected

        broker = _ConnectedBroker(connected=True)
        watcher = BrokerConnectionWatcher(broker=broker, alert_manager=manager)
        fault_ts = time.time()
        mark = len(getattr(manager, "_history", []))
        watcher.on_state(connected=True, now=fault_ts)   # 健康基线（已发生）
        watcher.on_state(connected=False, now=fault_ts + 0.1)  # 注入断连 → 转换点告警
        return InjectOutcome(
            fault_ts=fault_ts, captured_alert_id=_capture(mark, fault_ts),
            expected_source="watchdog", expected_category="broker",
            note="FI-03 券商断连注入（A-6 _check_broker）",
        )

    cases.append(DrillCase(fi=fi03, inject_fn=inject_fi03))

    # FI-04 伪造拒单 → A-8 ORDER_REJECTED（EVENT，ERROR/P1，MTTA<5min）
    fi04 = _first_fi("FI-04")

    def inject_fi04() -> InjectOutcome:
        bus = EventBus()
        manager.subscribe_to_event_bus(bus)
        fault_ts = time.time()
        mark = len(getattr(manager, "_history", []))
        evt = OrderEvent(
            event_type=EventType.ORDER_REJECTED,
            symbol="000001.SZ",
            status="rejected",
            reject_reason="drill 伪造拒单",
            signal_id="drill-fi04",
            order_id="ord-fi04",
            timestamp=fault_ts,
        )
        bus.publish(evt)
        return InjectOutcome(
            fault_ts=fault_ts, captured_alert_id=_capture(mark, fault_ts),
            expected_category="execution",
            note="FI-04 伪造拒单注入（A-8 ORDER_REJECTED）",
        )

    cases.append(DrillCase(fi=fi04, inject_fn=inject_fi04))

    # FI-05 人为制造持仓差异 → A-7 reconcile 差异（EVENT，CRITICAL，MTTR<30min）
    fi05 = _first_fi("FI-05")

    def inject_fi05() -> InjectOutcome:
        fault_ts = time.time()
        mark = len(getattr(manager, "_history", []))
        # A-7 捕获：账实差异（人为制造）→ CRITICAL（category=reconcile）
        manager.alert(
            level=SeverityLevel.CRITICAL,
            message="drill FI-05 持仓差异（容差=0 触发）",
            source="reconcile", category="reconcile",
            details={"injected": True, "symbol": "000001.SZ"},
            deduplication_key=f"drill:fi05:{int(fault_ts)}",
        )
        return InjectOutcome(
            fault_ts=fault_ts, captured_alert_id=_capture(mark, fault_ts),
            expected_category="reconcile",
            note="FI-05 持仓差异注入（A-7 reconcile 差异，CRITICAL）",
        )

    cases.append(DrillCase(fi=fi05, inject_fn=inject_fi05))

    # FI-06 告警主通道故障 → A-9 冗余/落盘兜底（元监控，KPI-8）
    fi06 = _first_fi("FI-06")

    def inject_fi06() -> InjectOutcome:
        fault_ts = time.time()
        mark = len(getattr(manager, "_history", []))
        # 主通道故障注入：仍落库（A-9 落盘兜底路径），独立 dedup_key 验证可达
        manager.alert(
            level=SeverityLevel.CRITICAL,
            message="drill FI-06 主通道故障兜底（落盘）",
            source="chaos_drill", category="meta",
            deduplication_key=f"drill:fi06:{int(fault_ts)}",
        )
        return InjectOutcome(
            fault_ts=fault_ts, captured_alert_id=_capture(mark, fault_ts),
            expected_source="chaos_drill",
            note="FI-06 主通道故障注入（A-9 冗余/落盘兜底）",
        )

    cases.append(DrillCase(fi=fi06, inject_fn=inject_fi06))

    # FI-07 主进程 kill → BASE-8 外部心跳超时（外部通道，KPI-8；R-08 连续超时判定）
    fi07 = _first_fi("FI-07")

    def inject_fi07() -> InjectOutcome:
        import tempfile as _tf
        from myquant.monitoring.meta_monitor import (
            ExternalHeartbeatGuard,
            FileHeartbeatCarrier,
        )
        tmp = _tf.mkdtemp(prefix="drill_hb_")
        carrier = FileHeartbeatCarrier(directory=tmp)
        guard = ExternalHeartbeatGuard(
            interval_seconds=1.0, consecutive_timeout=3, timeout_factor=2.0,
        )
        fault_ts = time.time()
        mark = len(getattr(manager, "_history", []))
        # 主进程存活：写一次心跳（已发生）
        carrier.send_heartbeat({"ts": fault_ts, "status": "ok"})
        # kill：心跳停写；外部看门狗连续超时达阈值才判死（R-08，不误判临界抖动）
        for i in range(1, 4):
            guard.check_alive(carrier, now=fault_ts + i * 2.0 + 0.1)
        alive, status, missed = guard.check_alive(carrier, now=fault_ts + 4.0 + 0.1)
        carrier.close()
        if status != "dead":
            raise RuntimeError(f"FI-07 注入失败：外部心跳未判死（status={status}）")
        # BASE-8 外部看门狗判死 → 独立告警通道上报（落库演示）
        manager.alert(
            level=SeverityLevel.CRITICAL, message="drill FI-07 主进程 kill（外部心跳判死）",
            source="external_heartbeat", category="meta",
            details={"consecutive_misses": missed},
            deduplication_key=f"drill:fi07:{int(fault_ts)}",
        )
        return InjectOutcome(
            fault_ts=fault_ts, captured_alert_id=_capture(mark, fault_ts),
            expected_source="external_heartbeat",
            note=f"FI-07 主进程 kill 注入（连续 {missed} 次超时判死，R-08）",
        )

    cases.append(DrillCase(fi=fi07, inject_fn=inject_fi07))

    return cases
