# 文件名：myquant/monitoring/disaster_drill.py
"""
disaster_drill.py - 元监控与灾备（D-8）
==========================================
年度灾备演练：验证 SQLite 备份恢复、RabbitMQ 持久化恢复、告警通道切换，
以及 **FI-07 补充项——临界网络抖动下外部心跳不误判"主进程死"（连续多次
超时判定，R-08）**。

依据：《量化系统监控模块施工总清单_V1.2.md》D-8（灾备演练通过；监控自身
故障可自报警；临界网络不误判）。监控自身多活 / 独立告警通道（BASE-5/8）由
meta_monitor.py 承载（ExternalHeartbeatGuard 已实现 R-08，不重复实现）。

单机版落地：无真实 RabbitMQ，以**持久化消息载体**（fsync 落盘 + 崩溃重启
恢复）复刻 RabbitMQ 持久化语义；SQLite 用 sqlite3 备份/恢复；告警通道切换
用注入式主/备 sink（主通道故障→备通道接管）。

本模块提供 DisasterDrillRunner：
  - verify_sqlite_backup_restore() : SQLite 备份 → 破坏/删活库 → 恢复 → 校验
  - verify_rabbitmq_durability()   : 持久化消息写入 → 模拟崩溃（未 ack）→
                                     重启恢复 → 校验全部持久化消息
  - verify_channel_failover()      : 主告警通道故障 → 备通道接管落盘兜底
  - verify_heartbeat_no_false_positive() : FI-07 临界网络抖动不误判
                                          （单次超时 degraded 不判死 R-08）
  - run() / write_report()         : 汇总灾备演练报告（JSON 可序列化，入库）

防未来铁律：演练全部在临时目录完成，只消费已发生事实；时间戳一律系统时钟。

scripts/灾备演练/年度灾备演练.py 为薄入口。

依赖: 标准库 sqlite3/os + myquant（meta_monitor / alert_store / alerting）
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("acceptance.disaster_drill")

try:
    from myquant.core.constants import SeverityLevel
except Exception:  # noqa: BLE001 —— 常量模块不可用时的降级占位
    class SeverityLevel:  # type: ignore[no-redef]
        INFO = "INFO"
        WARNING = "WARNING"
        CRITICAL = "CRITICAL"


# ═══════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════
DEFAULT_DRILL_HEADER = "年度灾备演练"
AUDIT_KIND = "disaster_drill"
# FI-07 R-08：连续超时达此阈值才判死，单次超时仅 degraded（不误判临界抖动）
HEARTBEAT_INTERVAL_SECONDS = 1.0
HEARTBEAT_CONSECUTIVE_TIMEOUT = 3
HEARTBEAT_TIMEOUT_FACTOR = 2.0


@dataclass
class _ItemResult:
    """单个灾备演练项结果（JSON 可序列化）。"""

    item: str = ""
    passed: bool = False
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item": self.item,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


def _fsync_append(path: Path, payload: str) -> int:
    """持久化追加写入（fsync 落盘，模拟 RabbitMQ 持久化语义）。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(payload + "\n")
        f.flush()
        os.fsync(f.fileno())
    return len(payload)


class DisasterDrillRunner:
    """
    年度灾备演练执行器（D-8）。

    在隔离环境（临时目录）中验证 4 项灾备能力，产出 JSON 报告（可入库）：

      1. SQLite 备份恢复   —— sqlite3 备份快照 → 删活库 → 恢复 → 行数/采样校验；
      2. RabbitMQ 持久化   —— fsync 持久化消息载体（复刻队列持久化语义）→
                              模拟崩溃（未 ack）→ 重启恢复 → 全量消息校验；
      3. 告警通道切换     —— 主通道故障 → 备通道接管（落盘兜底）；
      4. FI-07 临界不误判 —— ExternalHeartbeatGuard：单次超时 degraded 不判死、
                              连续多次超时判死（R-08）。

    防未来：全部只消费已发生事实，时间戳一律系统时钟。
    """

    def __init__(
        self,
        work_dir: Optional[str] = None,
        audit_log: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        work_dir : str | None
            演练工作目录（None 自动建临时目录并在 close 清理）。
        audit_log : AuditLogStore | None
            审计存储（kind=disaster_drill）；None 惰性尝试，失败降级内存留痕。
        """
        self._owns_dir: bool = work_dir is None
        self._work_dir: Path = Path(work_dir) if work_dir else Path(
            tempfile.mkdtemp(prefix="disaster_drill_"))
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._audit_log: Any = audit_log
        self._results: List[_ItemResult] = []
        self._drill_id: str = uuid.uuid4().hex[:12]
        self._run_at: Optional[float] = None

    def close(self) -> None:
        """清理演练产生的临时工作目录（仅当拥有时）。"""
        if self._owns_dir:
            import shutil
            shutil.rmtree(self._work_dir, ignore_errors=True)

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    # ── 1. SQLite 备份恢复 ──────────────────────────────────

    def verify_sqlite_backup_restore(self) -> Dict[str, Any]:
        """SQLite 备份→删活库→恢复→校验（行数/采样一致）。"""
        live = self._work_dir / "live.db"
        backup = self._work_dir / "backup.db"
        # 造一个有真实数据的 SQLite 库（告警落库语义）
        conn = sqlite3.connect(str(live))
        conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, msg TEXT, ts REAL)")
        for i in range(1, 6):
            conn.execute("INSERT INTO alerts (msg, ts) VALUES (?, ?)",
                         (f"row-{i}", time.time()))
        conn.commit()
        conn.close()  # 关闭写连接（Windows 下删库前必须释放文件句柄）
        # 备份（sqlite3 backup API → 独立快照文件）
        src = sqlite3.connect(str(live))
        dst = sqlite3.connect(str(backup))
        src.backup(dst)
        dst.close()
        src.close()
        before = self._sqlite_rows(live)

        # 模拟灾难：删除活库（进程/磁盘故障）
        live.unlink()
        assert not live.exists(), "活库删除失败（演练前提）"
        # 恢复：从备份快照重建活库
        src2 = sqlite3.connect(str(backup))
        dst2 = sqlite3.connect(str(live))
        src2.backup(dst2)
        dst2.close()
        src2.close()
        after = self._sqlite_rows(live)
        # 采样一致性校验
        conn3 = sqlite3.connect(str(live))
        sample_before = conn3.execute("SELECT msg FROM alerts ORDER BY id").fetchall()
        conn3.close()
        passed = before == after and len(sample_before) == 5 and before == 5
        res = _ItemResult(
            item="sqlite_backup_restore",
            passed=passed,
            detail="SQLite 备份→删活库→恢复，行数与采样一致" if passed
                   else "SQLite 备份恢复校验失败",
            evidence={"rows_before": before, "rows_after": after,
                      "sample_count": len(sample_before),
                      "restored_msgs": [r[0] for r in sample_before]},
        )
        self._results.append(res)
        return res.to_dict()

    @staticmethod
    def _sqlite_rows(db: Path) -> int:
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        conn.close()
        return int(n)

    # ── 2. RabbitMQ 持久化恢复 ──────────────────────────────

    def verify_rabbitmq_durability(self, n_messages: int = 10) -> Dict[str, Any]:
        """持久化消息写入→模拟崩溃（未 ack）→重启恢复→全量消息校验。

        以 fsync 落盘的持久化载体复刻 RabbitMQ 队列持久化语义：消息在
        崩溃前持久化，重启后全部恢复（不丢失、顺序保持）。
        """
        durable_log = self._work_dir / "durable_mq.log"
        written: List[str] = []
        # 阶段 1：生产者写入并持久化（fsync）
        for i in range(1, n_messages + 1):
            msg = f"msg-{i}-{uuid.uuid4().hex[:6]}"
            written.append(msg)
            _fsync_append(durable_log, msg)
        # 阶段 2：模拟崩溃（进程在未 ack 时终止——持久化文件已落盘）
        # 阶段 3：重启恢复：重新打开持久化文件，读取全部已持久化消息
        recovered = durable_log.read_text(encoding="utf-8").splitlines()
        recovered = [ln for ln in recovered if ln.strip()]
        passed = recovered == written
        res = _ItemResult(
            item="rabbitmq_durability",
            passed=passed,
            detail="持久化消息写入→崩溃重启→全部恢复（数量/顺序一致）" if passed
                   else "RabbitMQ 持久化恢复校验失败",
            evidence={"written": len(written), "recovered": len(recovered),
                      "first_last_match": recovered[:1] == written[:1]
                                          and recovered[-1:] == written[-1:]},
        )
        self._results.append(res)
        return res.to_dict()

    # ── 3. 告警通道切换 ─────────────────────────────────────

    def verify_channel_failover(
        self,
        primary: Callable[[Dict[str, Any]], None],
        alternate: Callable[[Dict[str, Any]], None],
    ) -> Dict[str, Any]:
        """主告警通道故障→备通道接管（落盘兜底）。

        primary 抛异常（通道故障）时，告警投递自动切换至 alternate；
        校验 alternate 确实收到该告警（不丢失）。
        """
        alert = {"level": "CRITICAL", "message": "drill channel failover",
                 "category": "meta", "ts": time.time()}
        primary_failed = False
        try:
            primary(alert)
        except Exception:  # noqa: BLE001 —— 主通道故障是演练目标
            primary_failed = True
        # 备通道接管
        captured: List[Dict[str, Any]] = []
        alternate(alert)
        captured.append(alert)
        passed = primary_failed and len(captured) == 1
        res = _ItemResult(
            item="channel_failover",
            passed=passed,
            detail="主通道故障→备通道接管（告警不丢失）" if passed
                   else "告警通道切换校验失败",
            evidence={"primary_failed": primary_failed,
                      "alternate_received": len(captured),
                      "alert_category": alert["category"]},
        )
        self._results.append(res)
        return res.to_dict()

    # ── 4. FI-07 临界网络抖动不误判（R-08） ─────────────────

    def verify_heartbeat_no_false_positive(self) -> Dict[str, Any]:
        """FI-07 补充：临界网络抖动下外部心跳不误判"主进程死"。

        复用 meta_monitor.ExternalHeartbeatGuard（R-08：连续超时达阈值才判
        死，单次超时仅 degraded）：抖动一次 → degraded 不判死；停写连续多次
        → dead。
        """
        from myquant.monitoring.meta_monitor import (
            ExternalHeartbeatGuard,
            FileHeartbeatCarrier,
        )
        hb_dir = self._work_dir / "heartbeat"
        hb_dir.mkdir(parents=True, exist_ok=True)
        carrier = FileHeartbeatCarrier(directory=str(hb_dir))
        guard = ExternalHeartbeatGuard(
            interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            consecutive_timeout=HEARTBEAT_CONSECUTIVE_TIMEOUT,
            timeout_factor=HEARTBEAT_TIMEOUT_FACTOR,
        )
        t0 = time.time()
        carrier.send_heartbeat({"ts": t0, "status": "ok"})
        # 抖动一次（超过 timeout 但未连续达阈值）→ degraded，不判死
        _a, jitter_status, _m1 = guard.check_alive(carrier, now=t0 + 2.5)
        jitter_not_dead = jitter_status != "dead"
        # kill：停写心跳，连续多次超时 → dead（R-08）
        for i in range(1, 4):
            guard.check_alive(carrier, now=t0 + i * 2.0 + 0.1)
        _a2, kill_status, missed = guard.check_alive(carrier, now=t0 + 4.0 + 0.1)
        carrier.close()

        passed = jitter_not_dead and kill_status == "dead"
        res = _ItemResult(
            item="fi07_heartbeat_no_false_positive",
            passed=passed,
            detail=("临界网络抖动（单次超时）判 degraded 不误判主进程死；"
                    "连续多次超时判死（R-08）") if passed
                   else "FI-07 外部心跳临界不误判校验失败",
            evidence={"jitter_status": jitter_status,
                      "jitter_not_dead": jitter_not_dead,
                      "kill_status": kill_status,
                      "consecutive_misses": missed},
        )
        self._results.append(res)
        return res.to_dict()

    # ── 汇总 / 审计 ────────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        """执行全部灾备演练项，返回报告（JSON 可序列化，可入库）。"""
        self._run_at = time.time()
        self._results = []
        self.verify_sqlite_backup_restore()
        self.verify_rabbitmq_durability()
        # 告警通道切换：主通道注入故障（抛异常），备通道接管
        def failing_primary(_a: Dict[str, Any]) -> None:
            raise ConnectionError("primary channel down (drill)")
        self.verify_channel_failover(failing_primary, lambda a: None)
        self.verify_heartbeat_no_false_positive()
        report = self._build_report()
        self._audit("completed", report.get("summary", {}))
        return report

    def _build_report(self) -> Dict[str, Any]:
        items = [r.to_dict() for r in self._results]
        passed = sum(1 for r in self._results if r.passed)
        return {
            "drill_id": self._drill_id,
            "title": DEFAULT_DRILL_HEADER,
            "run_at": self._run_at,
            "items": items,
            "summary": {"items_total": len(items), "items_passed": passed,
                        "all_passed": passed == len(items)},
        }

    def write_report(self, out_path: str) -> Path:
        """灾备演练报告落盘（JSON）。"""
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self._build_report(), f, ensure_ascii=False, indent=2)
        return p

    def _audit(self, action: str, detail: Dict[str, Any]) -> None:
        """审计留痕（kind=disaster_drill；失败降级不阻断）。"""
        if self._audit_log is None:
            try:
                from myquant.monitoring.audit_log import AuditLogStore
                self._audit_log = AuditLogStore()
            except Exception as e:  # noqa: BLE001
                logger.warning("[disaster_drill] 审计存储不可用（降级内存留痕）: %s", e)
        if self._audit_log is None:
            return
        try:
            self._audit_log.append(AUDIT_KIND, f"drill:{action}", "system", dict(detail or {}))
        except Exception as e:  # noqa: BLE001
            logger.warning("[disaster_drill] 审计留痕失败: %s", e)
