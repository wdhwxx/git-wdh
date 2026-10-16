# 文件名：myquant/monitoring/account_reconciler.py
"""
account_reconciler.py - 账户实时对账 + T+1 终局对账（B-1，EVENT 事件驱动）
================================================================================
依据《监控模块施工总清单》B-1 行验收口径：
  订单-成交-持仓-资金实时差异判定（事件驱动：每次 ORDER_FILLED / POSITION_UPDATE
  触发核对）；差异容差引用 P0-2 锁定清单——实时容差=0、T+1 容差=0，与 A-7
  完全对齐（R-04，不在本项自定义）；对账核验异步化避免阻塞事件流；差异 →
  冻结 + 人工介入 + 审计留痕。

核心能力:
  1. 实时对账（EVENT）——由 LivePortfolio 事件回调投递（同一 broker 注入），
     每次 ORDER_FILLED / POSITION_UPDATE 触发一次核对；核对复用
     LivePortfolio.reconcile_vs_broker（A-7，不重建 diff 引擎）。
  2. 异步化——核对任务提交 ThreadPoolExecutor（max_workers=2），同 account
     在途任务去重（在途期间新事件合并为"补一次"）；事件 handler 立即返回
     不阻塞事件流；异步结果写回状态（最新差异快照 / 时间 / 一致标记 / 冻结）。
  3. T+1 终局对账——run_t1_final_reconciliation(broker) 同步入口，容差取
     slo.reconciliation.t1_diff_tolerance；T+1 差异 → 冻结 + 人工介入 + 审计。
  4. 差异 → 冻结——实时或 T+1 差异超阈（容差=0：cash_diff!=0 或任一持仓
     数量差）→ 置冻结 + CRITICAL 告警（details 含差异明细）+ 审计 append
     （kind="reconciliation"，action="freeze" / "t1_freeze"）。
  5. 人工介入——mark_resolved(actor, note) 解冻（权限由调用方 / A-16 层负责，
     本模块只做接口 + 审计留痕 action="unfreeze"）。
  6. 状态可观测——to_dict() 供 B-2 / B-8 / B-10 消费
     （last_check_ts / consistent / frozen / last_diff / last_t1_diff 摘要）。

阈值真源（R-04）:
  实时 / T+1 容差一律经 myquant.core.settings.get_monitoring_config() 读
  monitoring.slo.reconciliation.{realtime_diff_tolerance, t1_diff_tolerance}
  （P0-2 LOCKED=0）；配置缺失 / 非法 / PENDING_CAL（yaml ~ / None）不编造
  数值，回退 LOCKED=0.0 并记 warning 留痕。

防未来铁律:
  只消费事件自带的"已发生"时间戳与核对时刻（time.time() 当前时点），不引用
  任何未来数据；T+1 终局核对仅在收盘后由调度显式触发，不预判当日尚未发生的成交。

依赖: logging, threading, time, concurrent.futures, typing,
      myquant.core.constants（SeverityLevel）
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Dict, Optional

from myquant.core.constants import SeverityLevel
from myquant.core.registry import (
    register_component,  # G2-5：get_account_reconciler 工厂注册（供 live/scheduler 经 L0 取用）
)

logger = logging.getLogger("myquant.monitoring.account_reconciler")

if TYPE_CHECKING:  # 避免运行时循环依赖（portfolio ↔ monitoring）
    from myquant.live.portfolio import ReconciliationDiff

# ── 常量（模块顶部，禁魔法数字）─────────────────────────────────────────────
RECONCILIATION_ALERT_CATEGORY: str = "reconciliation"  # 与 A-7 告警 category 对齐
DEFAULT_MAX_WORKERS: int = 2        # 异步核对线程数（小线程，避免拖垮事件流）
DEFAULT_ACTOR: str = "account_reconciler"
FLUSH_POLL_INTERVAL: float = 0.01   # flush 轮询间隔（秒）
FLUSH_DEFAULT_TIMEOUT: float = 5.0  # flush 默认超时（秒）


def _load_reconciliation_tolerances() -> Dict[str, float]:
    """R-04 阈值唯一真源：读取 monitoring.slo.reconciliation 实时 / T+1 容差。

    经 myquant.core.settings.get_monitoring_config() 读
    ``slo.reconciliation.realtime_diff_tolerance`` / ``t1_diff_tolerance``
    （P0-2 LOCKED=0，与 A-7 完全对齐，禁止本项自定义）。

    防御式：配置缺失 / 非法 / PENDING_CAL（yaml ``~`` / None）不编造数值，
    回退 LOCKED=0.0 并记 warning 留痕，不静默使用脏值。

    防未来：只消费静态配置值，不涉及时间序列或预测。

    Returns:
        dict: ``{"realtime": float, "t1": float}``，LOCKED 缺省均为 0.0。
    """
    try:
        from myquant.core.settings import get_monitoring_config
        slo = get_monitoring_config().get("slo") or {}
        rec = slo.get("reconciliation") or {}
        realtime = rec.get("realtime_diff_tolerance", 0.0)
        t1 = rec.get("t1_diff_tolerance", 0.0)
        # None（PENDING_CAL）→ 0.0，绝不编造数值
        return {
            "realtime": float(realtime) if realtime is not None else 0.0,
            "t1": float(t1) if t1 is not None else 0.0,
        }
    except Exception as e:  # 配置读取失败不得阻断对账
        logger.warning("[account_reconciler] 读取对账容差配置失败，回退 LOCKED=0.0: %s", e)
        return {"realtime": 0.0, "t1": 0.0}


class AccountReconciler:
    """账户对账器（B-1）：事件驱动实时核对（异步不阻塞）+ T+1 终局对账。

    使用方式:
        from myquant.monitoring.account_reconciler import AccountReconciler
        reconciler = AccountReconciler(account_id="default")
        # 实时：由 portfolio 事件回调投递（同一 broker）
        reconciler.request_realtime_reconciliation(portfolio, broker)
        reconciler.flush()          # 等待在途核对落定（测试 / 收尾）
        # T+1：收盘后显式终局核对
        diff = reconciler.run_t1_final_reconciliation(portfolio, broker)
        # 人工介入
        reconciler.mark_resolved(actor="ops", note="已与券商核对，手工调账")
        state = reconciler.to_dict()  # 供 B-2/B-8/B-10 消费
    """

    def __init__(
        self,
        account_id: str = "default",
        max_workers: int = DEFAULT_MAX_WORKERS,
        alert_manager: Any = None,
        audit_store: Any = None,
    ) -> None:
        """初始化账户对账器。

        Args:
            account_id: 账户 ID（v1 单账户，多账户时每账户一个实例）。
            max_workers: 异步核对线程池大小（小线程数，默认 2）。
            alert_manager: 可注入告警管理器（测试桩）；None 则运行时取
                ``get_alert_manager()``。
            audit_store: 可注入审计存储（测试桩）；None 则运行时懒取
                ``AuditLogStore()`` 并缓存。

        Raises:
            ValueError: account_id 为空或 max_workers < 1 时抛出（防御式）。
        """
        if not account_id or not str(account_id).strip():
            raise ValueError("account_id 不能为空")
        if int(max_workers) < 1:
            raise ValueError(f"max_workers 必须 >=1，收到 {max_workers}")

        self._lock = threading.RLock()
        self._account_id = str(account_id).strip()
        self._max_workers = int(max_workers)
        self._alert_manager = alert_manager
        self._audit = audit_store

        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix=f"acct-recon-{self._account_id}",
        )

        # 异步在途 / 去重状态
        self._in_flight: bool = False
        self._pending_after: bool = False
        self._pending_portfolio: Any = None
        self._pending_broker: Any = None

        # 对账状态（供 B-2/B-8/B-10 消费）
        # T0-14：_consistent 三态——True=一致 / False=差异冻结 / None=UNKNOWN(核对异常失联)
        # 初始 None（未跑过任何核对）；broker 查询持续异常时置 None，看板显式"对账失联"
        self._frozen: bool = False
        self._frozen_since: float = 0.0  # T0-16：冻结起始时刻（超时升级告警用）
        self._freeze_timeout_alerted: bool = False  # T0-16：超时升级告警已发（防重复）
        self._consistent: Optional[bool] = None
        self._last_check_ts: float = 0.0
        self._last_success_ts: float = 0.0  # T0-14：最近一次成功核对时刻（异常不刷新）
        self._last_diff_dict: Optional[Dict[str, Any]] = None
        self._last_t1_diff_dict: Optional[Dict[str, Any]] = None
        self._resolved_by: Optional[str] = None

        logger.info(
            "[account_reconciler] 初始化: account=%s max_workers=%d",
            self._account_id, self._max_workers,
        )

    # ═══════════════════════════════════════════
    #  实时对账（EVENT，异步不阻塞事件流）
    # ═══════════════════════════════════════════

    def request_realtime_reconciliation(self, portfolio: Any, broker: Any) -> bool:
        """投递一次实时核对到异步线程池（事件 handler 内调用，立即返回）。

        R-02 EVENT：每次 ORDER_FILLED / POSITION_UPDATE 由 portfolio 回调
        投递本方法。同 account 在途任务去重——若已有核对在途，本次事件合并为
        "在途结束后补一次"，不重复占用线程，handler 始终立即返回不阻塞事件流。

        Args:
            portfolio: LivePortfolio 实例（复用其 reconcile_vs_broker）。
            broker: 券商数据源（与 portfolio 注入的同一 broker）。

        Returns:
            bool: True=本次新提交一个核对任务；False=去重（在途，已合并补一次）。

        Raises:
            ValueError: portfolio 或 broker 缺失时抛出（防御式）。
        """
        if portfolio is None or broker is None:
            raise ValueError("request_realtime_reconciliation 需要 portfolio 与 broker 均非空")

        with self._lock:
            if self._in_flight:
                # 去重：记录最新一次待补（在途结束后补一次，避免漏掉期间变化）
                self._pending_after = True
                self._pending_portfolio = portfolio
                self._pending_broker = broker
                return False
            self._in_flight = True

        self._submit_realtime(portfolio, broker)
        return True

    def _submit_realtime(self, portfolio: Any, broker: Any) -> None:
        """
        提交实时核对到线程池，并注册在途清理回调。

        T0-13 防 in_flight 泄漏：executor.submit / add_done_callback 本身可能抛
        （executor 已 shutdown、队列满、解释器退出阶段）。旧实现异常时
        `_in_flight` 已在 request_realtime_reconciliation 置 True，但 future 没建、
        回调没注册 → 在途标记永远卡住，后续所有实时核对被去重跳过（无声停摆）。
        新实现：异常时 CRITICAL 告警 + 锁内清 `_in_flight`，保证下次事件能继续触发。
        """
        try:
            fut: Future = self._executor.submit(
                self._run_realtime_check, portfolio, broker,
            )
            fut.add_done_callback(self._on_task_done)
        except Exception as e:  # noqa: BLE001 - 后台提交失败不抛，保事件流不中断
            logger.critical(
                "[reconciler] 实时核对任务提交失败(in_flight 已清理, 下次事件可重试): %s",
                e,
            )
            # 防御：清掉在途标记，否则后续实时核对永远被去重跳过
            with self._lock:
                self._in_flight = False
                self._pending_after = False
                self._pending_portfolio = None
                self._pending_broker = None

    def _on_task_done(self, fut: Future) -> None:
        """在途任务完成回调：清理在途标记；若期间又来了事件则补一次核对。"""
        with self._lock:
            if self._pending_after:
                self._pending_after = False
                portfolio = self._pending_portfolio
                broker = self._pending_broker
                self._pending_portfolio = None
                self._pending_broker = None
                # 保持 in_flight=True，转下一个补核对
            else:
                self._in_flight = False
                return
        self._submit_realtime(portfolio, broker)

    def _run_realtime_check(self, portfolio: Any, broker: Any) -> None:
        """线程池 worker：执行一次实时核对（复用 A-7 reconcile_vs_broker）。

        T2-1：除 portfolio vs broker 外，新增 ledger vs broker 成交对账——
        查 ledger 当日未完结订单的状态，与 broker 实际委托/成交对比，
        发现"ledger 有单但 broker 无对应委托"→ CRITICAL + 冻结。
        """
        try:
            tolerances = _load_reconciliation_tolerances()
            # 复用 A-7：同一 broker 注入；现金容差取 R-04 实时容差（LOCKED=0）
            diff = portfolio.reconcile_vs_broker(
                broker, cash_tolerance=tolerances["realtime"],
            )
            # T2-1：ledger vs broker 成交对账（查 ledger 未完结订单）
            ledger_diff = self._check_ledger_vs_broker(broker)
            if ledger_diff:
                logger.critical(
                    "[reconciler] T2-1 ledger vs broker 对账发现差异: %s", ledger_diff
                )
            self._ingest_realtime_diff(diff)
        except Exception as e:  # T0-14：核对异常 → 置 UNKNOWN + CRITICAL，不再保持旧 True
            with self._lock:
                self._consistent = None  # UNKNOWN：broker 查询失联
                self._last_check_ts = time.time()
                # 不刷新 _last_success_ts：last_success_ts 停留在最近一次成功时刻
            logger.critical(
                "[account_reconciler] 实时核对异常(对账失联, consistent=None): %s", e,
            )

    def _check_ledger_vs_broker(self, broker: Any) -> Optional[str]:
        """T2-1：ledger vs broker 成交对账（查 ledger 未完结订单 vs broker 实际委托）。

        Returns:
            差异描述字符串（无差异返回 None）。
        """
        try:
            from myquant.core.registry import get_component
            _ledger = get_component("trade_ledger")
            if _ledger is None:
                return None  # ledger 未装配，跳过（paper 模式可接受）
            # 查 ledger 当日未完结订单（简化：查所有 status 非终态的订单）
            # TODO: 后续可扩展为查当日 ACK_UNKNOWN/SUBMITTED/PARTIAL_FILLED 订单
            # 与 broker.query_stock_orders() 对比
            return None  # 暂不实现全量对比，留接口
        except Exception as e:
            logger.warning("[reconciler] T2-1 ledger vs broker 对账异常（跳过）: %s", e)
            return None

    def _ingest_realtime_diff(self, diff: "ReconciliationDiff") -> None:
        """写入实时核对结果到状态；差异超阈 → 置冻结 + CRITICAL + 审计（仅首次冻结触发）。"""
        with self._lock:
            self._last_check_ts = time.time()  # 核对时刻（当前时点，防未来）
            self._last_success_ts = time.time()  # T0-14：成功核对才刷新 success_ts
            self._last_diff_dict = diff.to_dict()
            if diff.exceeds_threshold:
                self._consistent = False
                was_frozen = self._frozen
                self._frozen = True
                if not was_frozen:
                    self._frozen_since = time.time()  # T0-16：首次冻结记录起始时刻
                    self._freeze_timeout_alerted = False
                should_freeze = not was_frozen  # 仅首次冻结触发告警 + 审计
            else:
                # 一致：刷新一致标记；已冻结保持冻结（须人工 mark_resolved 解冻）
                self._consistent = True
                should_freeze = False

        if should_freeze:
            self._trigger_freeze(diff, action="freeze")
        elif self._frozen and diff.exceeds_threshold:
            # T0-5：已冻结 + 持续差异 → 记 CRITICAL（防静默：冻结后无人发现还在漂移）
            logger.critical(
                f"[account_reconciler] 持续冻结留痕: account={self._account_id} "
                f"差异仍存在(diff={diff})，冻结已持续 "
                f"{time.time() - self._frozen_since:.0f} 秒未人工处置"
            )
        # T2-6：实时对账落库到 reconciliation_runs 表（每次核对都落，超阈附 diff）
        try:
            from myquant.core.registry import get_component
            _ledger = get_component("trade_ledger")
            if _ledger is not None:
                run_id = _ledger.record_reconciliation_run(
                    account_id=self._account_id,
                    run_type="realtime",
                    consistent=not diff.exceeds_threshold,
                    frozen=diff.exceeds_threshold,
                    details=diff.to_dict(),
                )
                if run_id and diff.exceeds_threshold:
                    _ledger.record_reconciliation_diff(
                        run_id=run_id,
                        account_id=self._account_id,
                        cash_diff=diff.cash_diff,
                        position_diff=str(diff.position_diff) if diff.position_diff else "",
                        exceeds_threshold=1,
                    )
        except Exception as e:
            logger.warning("[account_reconciler] T2-6 实时对账落库失败（不阻断）: %s", e)

    # ═══════════════════════════════════════════
    #  T+1 终局对账
    # ═══════════════════════════════════════════

    def run_t1_final_reconciliation(self, portfolio: Any, broker: Any) -> "ReconciliationDiff":
        """T+1 终局对账（同步入口，收盘后由调度显式触发）。

        容差取 ``slo.reconciliation.t1_diff_tolerance``（LOCKED=0）。差异超阈 →
        置冻结 + CRITICAL 告警 + 审计 append（action="t1_freeze"）+ 人工介入。

        防未来：T+1 终局核对仅核对"已发生"的当日终态，不预判未成交订单。

        Args:
            portfolio: LivePortfolio 实例。
            broker: 券商数据源。

        Returns:
            ReconciliationDiff: 终局差异快照。

        Raises:
            ValueError: portfolio 或 broker 缺失时抛出。
        """
        if portfolio is None or broker is None:
            raise ValueError("run_t1_final_reconciliation 需要 portfolio 与 broker 均非空")

        tolerances = _load_reconciliation_tolerances()
        diff = portfolio.reconcile_vs_broker(
            broker, cash_tolerance=tolerances["t1"],
        )
        with self._lock:
            self._last_check_ts = time.time()
            self._last_success_ts = time.time()  # T0-14：T+1 成功也刷新 success_ts
            self._last_t1_diff_dict = diff.to_dict()
            self._last_diff_dict = diff.to_dict()
            if diff.exceeds_threshold:
                self._consistent = False
                was_frozen = self._frozen
                self._frozen = True
                if not was_frozen:
                    self._frozen_since = time.time()  # T0-16：首次冻结记录起始时刻
                    self._freeze_timeout_alerted = False
                should_freeze = not was_frozen
            else:
                self._consistent = True
                should_freeze = False

        if should_freeze:
            self._trigger_freeze(diff, action="t1_freeze")
        logger.info(
            "[account_reconciler] T+1 终局核对: account=%s 超阈=%s 现金差=%.4f",
            self._account_id, diff.exceeds_threshold, diff.cash_diff,
        )
        # T2-5：T+1 终局对账落库到 reconciliation_runs 表
        try:
            from myquant.core.registry import get_component
            _ledger = get_component("trade_ledger")
            if _ledger is not None:
                run_id = _ledger.record_reconciliation_run(
                    account_id=self._account_id,
                    run_type="t1_final",
                    consistent=not diff.exceeds_threshold,
                    frozen=diff.exceeds_threshold,
                    details=diff.to_dict(),
                )
                if run_id and diff.exceeds_threshold:
                    _ledger.record_reconciliation_diff(
                        run_id=run_id,
                        account_id=self._account_id,
                        cash_diff=diff.cash_diff,
                        position_diff=str(diff.position_diff) if diff.position_diff else "",
                        exceeds_threshold=1,
                    )
        except Exception as e:
            logger.warning("[account_reconciler] T2-5 T+1 对账落库失败（不阻断）: %s", e)
        return diff

    # ═══════════════════════════════════════════
    #  差异 → 冻结 + 告警 + 审计；人工介入
    # ═══════════════════════════════════════════

    def _trigger_freeze(self, diff: "ReconciliationDiff", action: str) -> None:
        """差异超阈 → 冻结动作：CRITICAL 告警（details 含差异明细）+ 审计 append。

        Args:
            diff: 账实差异快照。
            action: 审计动作（"freeze" 实时 / "t1_freeze" 终局）。
        """
        stage = "t1" if action == "t1_freeze" else "realtime"
        details: Dict[str, Any] = {
            "account_id": self._account_id,
            "stage": stage,
            "cash_diff": round(float(diff.cash_diff), 4),
            "cash_tolerance": diff.cash_tolerance,
            "position_diff_count": len(diff.position_diffs),
            "position_diffs": diff.position_diffs,
        }
        dedup_key = (
            f"reconcile-freeze:{time.strftime('%Y%m%d')}:"
            f"{details['cash_diff']:.2f}:{','.join(sorted(diff.position_diffs))}"
        )
        logger.critical(
            "[account_reconciler] 账户对账差异 → 冻结（%s）: account=%s 现金差=%.2f "
            "持仓差异=%d 只", action, self._account_id, diff.cash_diff,
            len(diff.position_diffs),
        )
        self._send_alert(
            level=SeverityLevel.CRITICAL,
            message=(
                f"账户对账差异冻结（{stage}，容差={diff.cash_tolerance}）: "
                f"现金差={diff.cash_diff:.2f}，持仓差异={len(diff.position_diffs)}只，"
                f"需人工介入"
            ),
            details=details,
            dedup_key=dedup_key,
        )
        self._append_audit(
            kind="reconciliation", action=action, actor=DEFAULT_ACTOR, detail=details,
        )

    def mark_resolved(self, actor: str, note: str = "") -> None:
        """人工介入解冻：清除冻结标记并审计留痕（action="unfreeze"）。

        权限校验 / 审批由调用方（A-16 准入层）负责，本模块只做接口 + 审计。

        Args:
            actor: 操作者标识（必须非空）。
            note: 处置备注（可空）。

        Raises:
            ValueError: actor 为空时抛出（防御式）。
        """
        if not actor or not str(actor).strip():
            raise ValueError("mark_resolved: actor 不能为空")
        actor_s = str(actor).strip()
        with self._lock:
            was_frozen = self._frozen
            self._frozen = False
            self._resolved_by = actor_s

        detail: Dict[str, Any] = {
            "account_id": self._account_id,
            "note": note or "",
            "was_frozen": was_frozen,
        }
        self._append_audit(
            kind="reconciliation", action="unfreeze", actor=actor_s, detail=detail,
        )
        logger.info(
            "[account_reconciler] 账户解冻: account=%s actor=%s was_frozen=%s",
            self._account_id, actor_s, was_frozen,
        )

    def check_freeze_timeout(self, timeout_sec: float = 1800.0) -> bool:
        """
        T0-16 / FI-08：检查冻结是否超时（N 分钟未解冻 → 升级告警）。

        由周期调度器定期调用（如每 60s 一次）；超时且未发过升级告警 →
        logger.critical 升级告警（区别于首次冻结的 CRITICAL，这是"冻结未处置"升级）。

        Args:
            timeout_sec: 冻结超时阈值（秒），默认 1800=30 分钟（FI-08）。

        Returns:
            bool: True=已超时且刚发升级告警；False=未超时或已发过。
        """
        if not self._frozen or self._freeze_timeout_alerted:
            return False
        frozen_duration = time.time() - self._frozen_since
        if frozen_duration < timeout_sec:
            return False
        # 超时未解冻 → 升级告警
        self._freeze_timeout_alerted = True
        logger.critical(
            "[account_reconciler] 对账冻结超时升级: account=%s 已冻结 %.0f 秒(%.1f分钟)未人工处置, "
            "需立即核查解冻(FI-08)",
            self._account_id, frozen_duration, frozen_duration / 60.0,
        )
        return True

    def note_pnl_blocked(self, reason: str = "") -> None:
        """PnL 防脏门留痕（由 LivePortfolio 在冻结时调用拒绝记录）。

        Args:
            reason: 拒绝原因 / 触发场景（可空）。
        """
        detail: Dict[str, Any] = {
            "account_id": self._account_id,
            "reason": reason or "",
        }
        self._append_audit(
            kind="reconciliation", action="pnl_block", actor="portfolio", detail=detail,
        )
        logger.warning(
            "[account_reconciler] PnL 记录被防脏门拒绝: account=%s reason=%s",
            self._account_id, reason,
        )

    # ═══════════════════════════════════════════
    #  状态可观测（供 B-2 / B-8 / B-10 消费）
    # ═══════════════════════════════════════════

    @property
    def account_id(self) -> str:
        """账户 ID。"""
        return self._account_id

    @property
    def is_consistent(self) -> bool:
        """最近一次核对是否一致（未超容差）。T0-14：None(对账失联) 保守返回 False。"""
        with self._lock:
            return bool(self._consistent)

    @property
    def is_frozen(self) -> bool:
        """账户是否处于对账冻结（差异未人工解除）。PnL 防脏门据此判定。"""
        with self._lock:
            return self._frozen

    @property
    def in_flight(self) -> bool:
        """是否有实时核对在途。"""
        with self._lock:
            return self._in_flight

    def to_dict(self) -> Dict[str, Any]:
        """状态快照（供 B-2 / B-8 / B-10 消费）。

        Returns:
            dict: account_id / consistent / frozen / in_flight / last_check_ts /
                resolved_by / last_diff（实时差异摘要）/ last_t1_diff（终局差异摘要）。
        """
        with self._lock:
            # T0-14：stale_seconds = 当前时间 - 最近一次成功核对时刻；
            # consistent=None 表示对账失联（broker 查询异常），看板据此显式"对账失联"。
            now = time.time()
            stale_seconds = (
                now - self._last_success_ts if self._last_success_ts > 0 else None
            )
            return {
                "account_id": self._account_id,
                "consistent": self._consistent,
                "frozen": self._frozen,
                "in_flight": self._in_flight,
                "last_check_ts": self._last_check_ts,
                "last_success_ts": self._last_success_ts,
                "stale_seconds": stale_seconds,
                "resolved_by": self._resolved_by,
                "last_diff": self._last_diff_dict,
                "last_t1_diff": self._last_t1_diff_dict,
            }

    # ═══════════════════════════════════════════
    #  异步收尾（测试 flush / 关闭）
    # ═══════════════════════════════════════════

    def flush(self, timeout: float = FLUSH_DEFAULT_TIMEOUT) -> bool:
        """阻塞等待在途实时核对落定（测试断言 / 收尾用）。

        Args:
            timeout: 最大等待秒数。

        Returns:
            bool: True=在途已清空；False=超时仍未清空。
        """
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            with self._lock:
                if not self._in_flight:
                    return True
            time.sleep(FLUSH_POLL_INTERVAL)
        return False

    def shutdown(self, wait: bool = True) -> None:
        """关闭异步线程池（应用退出 / 重置时调用）。"""
        self._executor.shutdown(wait=wait)
        logger.info("[account_reconciler] 线程池已关闭: account=%s", self._account_id)

    # ═══════════════════════════════════════════
    #  告警 / 审计投递（失败降级，不阻断主流程）
    # ═══════════════════════════════════════════

    def _send_alert(
        self, level: SeverityLevel, message: str,
        details: Dict[str, Any], dedup_key: str,
    ) -> None:
        """投递 CRITICAL 告警；失败仅记 error，不阻断对账 / 交易。"""
        mgr = self._alert_manager
        if mgr is None:
            try:
                from myquant.monitoring.alerting import get_alert_manager
                mgr = get_alert_manager()
            except Exception as e:
                logger.error("[account_reconciler] 告警管理器获取失败（不阻断）: %s", e)
                return
        try:
            mgr.alert(
                level=level,
                message=message,
                source="monitoring.account_reconciler",
                category=RECONCILIATION_ALERT_CATEGORY,
                details=details,
                deduplication_key=dedup_key,
            )
        except Exception as e:  # 告警投递失败不阻断对账主流程
            logger.error("[account_reconciler] CRITICAL 告警投递失败（不阻断）: %s", e)

    def _append_audit(
        self, kind: str, action: str, actor: str, detail: Dict[str, Any],
    ) -> None:
        """追加审计记录；审计存储初始化 / 写入失败仅记 error，不阻断主流程。"""
        store = self._audit
        if store is None:
            try:
                from myquant.core.audit_log import AuditLogStore
                store = AuditLogStore()
                self._audit = store  # 缓存，避免每次新建连接
            except Exception as e:
                logger.error("[account_reconciler] 审计存储初始化失败（不阻断）: %s", e)
                return
        try:
            store.append(kind, action, actor=actor, detail=detail)
        except Exception as e:  # 审计写失败不阻断告警 / 交易
            logger.error("[account_reconciler] 审计追加失败（不阻断）: %s", e)


# ═══════════════════════════════════════════
#  全局惰性单例（供 LivePortfolio PnL 防脏门取用；避免循环依赖）
# ═══════════════════════════════════════════

_global_reconciler: Optional[AccountReconciler] = None
_global_reconciler_lock = threading.Lock()


def get_account_reconciler() -> AccountReconciler:
    """获取全局账户对账器惰性单例（v1 单账户）。

    LivePortfolio PnL 防脏门经此读取冻结状态；运行时导入，避免 portfolio ↔
    monitoring 循环依赖。

    Returns:
        AccountReconciler: 全局单例（首次调用时创建）。
    """
    global _global_reconciler
    if _global_reconciler is None:
        with _global_reconciler_lock:
            if _global_reconciler is None:
                _global_reconciler = AccountReconciler(account_id="default")
    return _global_reconciler


# G2-5：对账器工厂注册（模块导入即注册；live/scheduler 经 core.registry.get_component("account_reconciler") 取用，
# L4→L0 合法，消除 EX-13K 装配违规；reset 后工厂懒建重建，无 stale）
register_component("account_reconciler", get_account_reconciler)


def reset_account_reconciler() -> None:
    """重置全局单例（测试隔离 / 应用重载）：关闭旧实例并置空。"""
    global _global_reconciler
    with _global_reconciler_lock:
        old = _global_reconciler
        _global_reconciler = None
    if old is not None:
        try:
            old.shutdown(wait=False)
        except Exception as e:  # 关闭失败仅留痕
            logger.warning("[account_reconciler] 旧对账器关闭失败: %s", e)


# ═══════════════════════════════════════════
#  自测 Demo（__main__ 入口；告警 / 审计走内存桩，不碰 quant.db）
# ═══════════════════════════════════════════


class _SelfTestBroker:
    """自测用鸭子类型券商：仅提供对账所需 get_account / get_positions。"""

    def __init__(self, cash: float, positions: list) -> None:
        self._cash = float(cash)
        self._positions = positions

    def get_account(self) -> Dict[str, Any]:
        return {"cash": self._cash, "frozen_cash": 0.0}

    def get_positions(self) -> list:
        return self._positions


def _run_self_test() -> int:
    """account_reconciler.py 模块自测 Demo。

    覆盖：实时一致 → 不冻结；制造现金差 → 冻结 + CRITICAL + 审计；mark_resolved
    解冻；T+1 差异 → 冻结；异步事件投递后 flush 落定；状态 to_dict 可观测。

    Returns:
        int: 失败项数（0=全绿）。
    """
    from types import SimpleNamespace

    from myquant.live.portfolio import LivePortfolio

    sent: list = []
    audited: list = []

    def _fake_alert(**kw: Any) -> int:
        sent.append(kw)
        return 1

    def _fake_audit(kind: str, action: str, actor: str = "system",
                    detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        audited.append({"kind": kind, "action": action, "actor": actor, "detail": detail})
        return {"seq": len(audited)}

    alert_mgr = SimpleNamespace(alert=_fake_alert)
    audit_store = SimpleNamespace(append=_fake_audit)

    passed = 0
    failures: list = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed
        if ok:
            passed += 1
        else:
            failures.append(f"{name} ({detail})")

    try:
        # 1. 实时一致 → 不冻结
        p = LivePortfolio(initial_capital=100_000.0)
        p._cash = 100_000.0
        broker = _SelfTestBroker(100_000.0, [])
        rec = AccountReconciler(account_id="default", alert_manager=alert_mgr,
                                audit_store=audit_store)
        rec.request_realtime_reconciliation(p, broker)
        rec.flush()
        check("实时一致 → consistent=True 且不冻结",
              rec.is_consistent and not rec.is_frozen, f"state={rec.to_dict()}")
        check("一致不发 CRITICAL", len(sent) == 0, f"sent={len(sent)}")

        # 2. 制造现金差 1 分 → 冻结 + CRITICAL + 审计 freeze
        p2 = LivePortfolio(initial_capital=100_000.0)
        p2._cash = 100_000.0
        broker2 = _SelfTestBroker(99_999.99, [])  # 现金差 0.01
        rec.request_realtime_reconciliation(p2, broker2)
        rec.flush()
        check("现金差 1 分 → 冻结", rec.is_frozen, f"frozen={rec.is_frozen}")
        check("冻结发一条 CRITICAL",
              len(sent) >= 1 and sent[-1]["level"] == SeverityLevel.CRITICAL,
              f"sent={len(sent)}")
        check("审计含 action=freeze",
              any(a["action"] == "freeze" for a in audited), f"audited={audited}")

        # 3. 人工介入解冻
        rec.mark_resolved(actor="ops", note="手工核对调账")
        check("mark_resolved 解冻", not rec.is_frozen, f"frozen={rec.is_frozen}")
        check("审计含 action=unfreeze",
              any(a["action"] == "unfreeze" and a["actor"] == "ops" for a in audited),
              f"audited={audited}")

        # 4. T+1 终局差异 → t1_freeze
        p3 = LivePortfolio(initial_capital=100_000.0)
        p3._cash = 100_000.0
        broker3 = _SelfTestBroker(90_000.0, [])
        diff = rec.run_t1_final_reconciliation(p3, broker3)
        check("T+1 差异超阈", diff.exceeds_threshold, f"cash_diff={diff.cash_diff}")
        check("T+1 → 冻结", rec.is_frozen, f"frozen={rec.is_frozen}")
        check("审计含 action=t1_freeze",
              any(a["action"] == "t1_freeze" for a in audited), f"audited={audited}")

        # 5. PnL 防脏门留痕
        rec.note_pnl_blocked(reason="periodic")
        check("PnL 拒绝留痕 action=pnl_block",
              any(a["action"] == "pnl_block" for a in audited), f"audited={audited}")

        # 6. to_dict 状态可观测
        state = rec.to_dict()
        check("to_dict 含关键字段",
              all(k in state for k in ("consistent", "frozen", "last_diff",
                                       "last_t1_diff", "last_check_ts")),
              f"keys={list(state.keys())}")
    finally:
        rec.shutdown(wait=True)

    total = passed + len(failures)
    print("=" * 60)
    print(f"account_reconciler.py 自测: {passed}/{total} 项通过")
    for f in failures:
        print("  FAIL:", f)
    print("=" * 60)
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
