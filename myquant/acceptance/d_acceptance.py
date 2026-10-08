# 文件名：myquant/monitoring/d_acceptance.py
"""
d_acceptance.py - D 包验收（D-9）
====================================
D-1~D-8 验收清单逐项断言（隔离环境），产出 JSON 验收报告（可入库）。

验收口径（对应《量化系统监控模块施工总清单_V1.2.md》D-9 / §10）：
  - D-1 自动化响应编排：规则引擎三阶段安全模型 + Kill Switch（test_action_engine.py）
  - D-2 自愈动作库：四类场景执行器 + 再分配/参数调整执行器（test_action_engine.py）
  - D-3 阈值自动校准：校准/回滚/人工覆盖（test_threshold_calibration.py）
  - D-4 风险预算再分配：再分配/审批/回滚（test_risk_reallocation.py）
  - D-5 参数动态调整：提议/确认/生效/回滚（test_param_tuner.py）
  - D-6 混沌工程与故障演练：P0-8 FI-01~07 逐用例演练捕获（ChaosDrillRunner 集成）
  - D-7 监控即代码完善：配置版本化 apply→rollback（MonitoringConfigRegistry 集成）
  - D-8 元监控与灾备：SQLite 备份恢复/RabbitMQ 持久化/通道切换/FI-07 临界不误判
    （DisasterDrillRunner 集成）

D-1~D-5 的验收以各自专项单测套件为证据（子进程 pytest 全绿即判通过）；
D-6~D-8 直接复用对应演练/配置 Runner 在隔离环境集成断言。
防未来：全部只消费已发生事实，时间戳一律系统时钟。

scripts/验收_D包自治.py 为薄入口。

依赖: 标准库 subprocess + myquant（chaos_drill / config_as_code / disaster_drill）
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("acceptance.d_acceptance")

# ═══════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════
DEFAULT_REPORT_TITLE = "D 包自治验收报告"
AUDIT_KIND = "d_acceptance"
# D-1~D-5 专项单测套件（验收证据来源）
D12_TEST_FILE = "tests/unit/test_action_engine.py"          # D-1 + D-2
D3_TEST_FILE = "tests/unit/test_threshold_calibration.py"   # D-3
D4_TEST_FILE = "tests/unit/test_risk_reallocation.py"       # D-4
D5_TEST_FILE = "tests/unit/test_param_tuner.py"             # D-5


@dataclass
class _Item:
    """单个验收项结果（JSON 可序列化）。"""

    item: str = ""
    criterion: str = ""
    passed: bool = False
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item": self.item,
            "criterion": self.criterion,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


class DAcceptanceRunner:
    """
    D 包验收执行器（D-9）。

    在隔离环境逐项断言 D-1~D-8 验收清单；D-1~D-5 以专项单测套件为证据，
    D-6~D-8 以对应 Runner 集成断言为证据；产出 JSON 验收报告（可入库）。
    """

    def __init__(
        self,
        project_root: Optional[str] = None,
        audit_log: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        project_root : str | None
            项目根目录（定位测试套件；None 自动探测）。
        audit_log : AuditLogStore | None
            审计存储（kind=d_acceptance）；None 惰性尝试，失败降级内存留痕。
        """
        self._root: Path = Path(project_root) if project_root else self._find_root()
        self._audit_log: Any = audit_log
        self._items: List[_Item] = []
        self._run_at: Optional[float] = None
        self._acceptance_id: str = uuid.uuid4().hex[:12]

    @staticmethod
    def _find_root() -> Path:
        """探测项目根（含 myquant/ 的最近父目录）。"""
        cur = Path.cwd()
        for p in [cur, *cur.parents]:
            if (p / "myquant").is_dir():
                return p
        return cur

    # ── 工具：运行专项测试套件 ──────────────────────────────

    def _run_test_file(self, rel_path: str) -> Dict[str, Any]:
        """以子进程 pytest 运行专项单测套件，返回 {exit/passed/collected}。"""
        target = self._root / rel_path
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(target), "-q"],
            capture_output=True, text=True, cwd=str(self._root),
            timeout=180,
        )
        out = proc.stdout + proc.stderr
        passed = 0
        collected = 0
        m = re.search(r"(\d+) passed", out)
        if m:
            passed = int(m.group(1))
        c = re.search(r"(\d+) collected", out)
        if c:
            collected = int(c.group(1))
        failed = bool(re.search(r"\d+ failed", out)) or proc.returncode != 0
        return {"exit": proc.returncode, "passed": passed,
                "collected": collected, "failed": failed}

    # ── 验收项 ──────────────────────────────────────────────

    def _check_unit_suite(self, item: str, criterion: str, rel_path: str) -> _Item:
        """D-1~D-5：专项单测套件全绿即验收通过。"""
        ev = self._run_test_file(rel_path)
        passed = ev["exit"] == 0 and not ev["failed"]
        return _Item(
            item=item, criterion=criterion, passed=passed,
            detail=f"专项单测 {rel_path} 全绿（{ev['passed']} passed"
                   f"/{ev['collected']} collected）" if passed
                   else f"专项单测 {rel_path} 未通过（exit={ev['exit']}）",
            evidence=ev,
        )

    def _check_d6(self) -> _Item:
        """D-6：P0-8 FI-01~07 逐用例演练，故障全部被捕获（TP）。"""
        from myquant.acceptance.chaos_drill import ChaosDrillRunner
        runner = ChaosDrillRunner()
        try:
            runner.register_builtin_cases()
            report = runner.run()
            s = report["summary"]
            passed = s["cases_total"] == 7 and s["fn"] == 0 and s["tp"] == 7
            return _Item(
                item="D-6", criterion="混沌工程与故障演练",
                passed=passed,
                detail=f"P0-8 FI-01~07 演练：{s['tp']} TP / {s['fp']} FP / "
                       f"{s['fn']} FN（检出率 {s['detect_rate']:.0%}）",
                evidence={"cases_total": s["cases_total"], "tp": s["tp"],
                          "fp": s["fp"], "fn": s["fn"]},
            )
        finally:
            runner.close()

    def _check_d7(self) -> _Item:
        """D-7：config/monitoring 配置版本化 apply→rollback（隔离环境）。"""
        from myquant.monitoring.config_as_code import MonitoringConfigRegistry
        tmp = Path(tempfile.mkdtemp(prefix="d_accept_cfg_"))
        try:
            cfg_dir = tmp / "cfg"
            cfg_dir.mkdir()
            reg = MonitoringConfigRegistry(config_dir=str(cfg_dir),
                                           ledger_path=str(tmp / "ledger.json"))
            (cfg_dir / "dashboard.yaml").write_text("v1\n", encoding="utf-8")
            reg.initialize()
            applied = reg.apply("dashboard", "v2\n", changed_by="acceptance")
            rolled = reg.rollback("dashboard", changed_by="acceptance")
            content = (cfg_dir / "dashboard.yaml").read_text(encoding="utf-8")
            passed = (applied["version"] == 2 and rolled.get("rolled_back") is True
                      and content == "v1\n")
            hist = reg.history("dashboard")
            return _Item(
                item="D-7", criterion="监控即代码完善（版本化+变更监控+回滚）",
                passed=passed,
                detail="配置 apply→回滚恢复上一版本内容；版本/变更史可追溯",
                evidence={"applied_version": applied["version"],
                          "rolled_back": rolled.get("rolled_back"),
                          "restored_content": content == "v1\n",
                          "history_len": len(hist)},
            )
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def _check_d8(self) -> _Item:
        """D-8：年度灾备演练四项全部通过（隔离环境）。"""
        from myquant.acceptance.disaster_drill import DisasterDrillRunner
        runner = DisasterDrillRunner()
        try:
            report = runner.run()
            s = report["summary"]
            passed = s["all_passed"] is True and s["items_passed"] == 4
            return _Item(
                item="D-8", criterion="元监控与灾备（年度灾备演练）",
                passed=passed,
                detail="SQLite 备份恢复 / RabbitMQ 持久化 / 告警通道切换 / "
                       "FI-07 临界不误判 四项全通过",
                evidence={"items_total": s["items_total"],
                          "items_passed": s["items_passed"]},
            )
        finally:
            runner.close()

    # ── 汇总 ───────────────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        """执行 D-1~D-8 全部验收，返回报告（JSON 可序列化，可入库）。"""
        self._run_at = time.time()
        self._items = []
        # D-1 + D-2 共用编排+动作库单测套件；D-3/D-4/D-5 各自套件
        self._items.append(self._check_unit_suite(
            "D-1", "自动化响应编排（三阶段安全模型+Kill Switch）", D12_TEST_FILE))
        self._items.append(self._check_unit_suite(
            "D-2", "自愈动作库（四类场景+再分配/参数执行器）", D12_TEST_FILE))
        self._items.append(self._check_unit_suite(
            "D-3", "阈值自动校准（校准/回滚/人工覆盖）", D3_TEST_FILE))
        self._items.append(self._check_unit_suite(
            "D-4", "风险预算再分配（再分配/审批/回滚）", D4_TEST_FILE))
        self._items.append(self._check_unit_suite(
            "D-5", "参数动态调整（提议/确认/生效/回滚）", D5_TEST_FILE))
        self._items.append(self._check_d6())
        self._items.append(self._check_d7())
        self._items.append(self._check_d8())
        report = self._build_report()
        self._audit(report.get("summary", {}))
        return report

    def _build_report(self) -> Dict[str, Any]:
        items = [i.to_dict() for i in self._items]
        passed = sum(1 for i in self._items if i.passed)
        return {
            "acceptance_id": self._acceptance_id,
            "title": DEFAULT_REPORT_TITLE,
            "run_at": self._run_at,
            "items": items,
            "summary": {"items_total": len(items), "items_passed": passed,
                        "all_passed": passed == len(items)},
        }

    def write_report(self, out_path: str) -> Path:
        """验收报告落盘（JSON）。"""
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self._build_report(), f, ensure_ascii=False, indent=2)
        return p

    def _audit(self, detail: Dict[str, Any]) -> None:
        """审计留痕（kind=d_acceptance；失败降级不阻断）。"""
        if self._audit_log is None:
            try:
                from myquant.monitoring.audit_log import AuditLogStore
                self._audit_log = AuditLogStore()
            except Exception as e:  # noqa: BLE001
                logger.warning("[d_acceptance] 审计存储不可用（降级内存留痕）: %s", e)
        if self._audit_log is None:
            return
        try:
            self._audit_log.append(AUDIT_KIND, "acceptance", "system", dict(detail or {}))
        except Exception as e:  # noqa: BLE001
            logger.warning("[d_acceptance] 审计留痕失败: %s", e)
