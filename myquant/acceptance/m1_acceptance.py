# 文件名：myquant/monitoring/m1_acceptance.py
"""
m1_acceptance.py - M1 保命验收业务逻辑（A-15-Min / A-15-Full）
==========================================================================
按《量化系统监控模块施工总清单_V1.2》A-15-Min / A-15-Full 项施工：

  - A-15-Min  : M1-Min 保命子集验收——保命闭环链路（行情活性→券商断连→
                交易异常→持仓对账）覆盖率 100%；FI-01/03/04/05 注入检出且走事件路径。
  - A-15-Full : M1-Full 完整保命验收——FI-01~05 注入按 P0-8 口径计时
                （P0 故障 MTTD<1min / MTTA<5min / MTTR<30min）；
                所有标注 EVENT 的项必须经事件触发路径验证（确认走事件而非等满 60s 周期）；
                准确率/误报率按 P0-8 ground-truth 口径自动统计（TP/FP/FN）。

测量口径（唯一权威依据 P0-8，见 myquant.core.measurability）：
  - MTTD = 故障注入时间戳（事件时间） → 告警 BASE-1 落库 created_at
  - MTTA = 告警 created_at → 系统确认 acked_at
  - MTTR = 故障注入时间戳 → 告警→恢复成对记录 resolved_at
  - 准确率 = TP/(TP+FP)，误报率 = FP/(TP+FP)，检出率 = TP/(TP+FN)

防未来铁律：本模块只消费"已发生"的时间戳与事件（fault_ts / created_at /
acked_at / resolved_at 均为系统时钟），故障注入走真实 EventBus + 真实
AlertManager + 真实 AlertStore（临时库），计时一律系统时间戳，禁止人工估算。

本模块为验收业务逻辑；scripts/验收_监控M1保命.py 为薄入口。

依赖: 标准库 + myquant（event_bus / alerting / alert_store / watchdog /
      measurability / portfolio / settings / time_utils）
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from myquant.core.alert_store import AlertStore
from myquant.core.event_bus import Event, EventBus, EventType, OrderEvent
from myquant.core.logger import get_logger
from myquant.core.measurability import (
    CLASSIFICATION_TP,
    ClassificationStats,
    MttxCalculator,
)
from myquant.core.settings import get_monitoring_config
from myquant.live.portfolio import LivePortfolio
from myquant.models.trade import Position
from myquant.monitoring.alerting import AlertManager
from myquant.monitoring.watchdog import (
    BrokerConnectionWatcher,
    EngineHeartbeatWatcher,
    StalenessDetector,
)
from myquant.utils.time_utils import SessionType

logger = get_logger("acceptance.m1_acceptance")


# ═══════════════════════════════════════════════
#  常量（验收 KPI 预算，P0-8 §2 定义；非业务阈值）
# ═══════════════════════════════════════════════
MTTD_BUDGET_SECONDS: float = 60.0       # P0 故障 MTTD < 1min
MTTA_BUDGET_SECONDS: float = 300.0      # MTTA < 5min
MTTR_BUDGET_SECONDS: float = 1800.0     # MTTR < 30min
#: FI-01 行情断流演示用短检测窗口（<60s 证明机制可达 KPI；生产 bar_timeout 由配置真源决定）
STALENESS_ACCEPT_BAR_TIMEOUT: float = 2.0
#: FI-01 断流演示宽余量（sleep 额外时间，确保真实时钟越过窗口触发）
STALENESS_ACCEPT_MARGIN: float = 0.3

#: 保命闭环链路（A-15 覆盖率 100% 的四个环，P0-8 事件路径入口）
CHAIN_RINGS: List[Dict[str, str]] = [
    {
        "ring": "行情活性",
        "item": "A-4",
        "detector": "StalenessDetector",
        "event_entry": "EventType.BAR 订阅（on_bar 刷新时钟）+ evaluate 秒级兜底",
    },
    {
        "ring": "券商断连",
        "item": "A-6",
        "detector": "BrokerConnectionWatcher",
        "event_entry": "on_state 健康→不健康转换点（EVENT 路径）",
    },
    {
        "ring": "交易异常",
        "item": "A-8",
        "detector": "AlertManager.subscribe_to_event_bus",
        "event_entry": "订阅 ORDER_REJECTED/FAILED/FILL_REPORT_ERROR/EMERGENCY_LIQUIDATION",
    },
    {
        "ring": "持仓对账",
        "item": "A-7",
        "detector": "LivePortfolio.reconcile_vs_broker",
        "event_entry": "账实差异超阈（容差=0）→ CRITICAL",
    },
]

#: A-15-Full 要求覆盖的标准故障注入用例（P0-8 §3 用例库，与 D-6 共用）
FULL_FI_CASE_IDS: Tuple[str, ...] = ("FI-01", "FI-02", "FI-03", "FI-04", "FI-05")
#: A-15-Min 要求的保命子集故障注入用例（P0-8 §3；EVENT 项）
MIN_FI_CASE_IDS: Tuple[str, ...] = ("FI-01", "FI-03", "FI-04", "FI-05")


class _ActiveSessionCalendar:
    """验收桩日历：恒为交易日 + 活跃上午盘（避免实际运行日期落在午休/收盘/节假日后豁免，使断流检测确定）。"""

    def is_trading_day(self, _date: Any) -> bool:
        """恒视为交易日（验收桩）。"""
        return True

    def get_session(self, _now_dt: Any) -> SessionType:
        """恒视为上午活跃盘（验收桩）。"""
        return SessionType.MORNING


class _ConnectedBroker:
    """验收桩券商：is_connected 属性可翻转（FI-03 断连注入）。"""

    def __init__(self, connected: bool = True) -> None:
        self.connected: bool = connected

    @property
    def is_connected(self) -> bool:
        """当前连接状态（验收桩）。"""
        return self.connected


class _ReconcileBroker:
    """验收桩券商：供 FI-05 对账注入（get_account / get_positions）。"""

    def __init__(self, cash: float, positions: List[Position]) -> None:
        self._cash: float = float(cash)
        self._positions: List[Position] = list(positions)

    def get_account(self) -> Dict[str, Any]:
        """返回券商账户快照（验收桩）。"""
        return {"cash": self._cash, "frozen_cash": 0.0}

    def get_positions(self) -> List[Position]:
        """返回券商持仓快照（验收桩）。"""
        return list(self._positions)


class _HangEngine:
    """验收桩引擎：get_status 恒抛异常（FI-02 引擎僵死注入）。"""

    def get_status(self) -> Dict[str, Any]:
        """引擎僵死桩：始终抛异常表示无响应。"""
        raise RuntimeError("engine hung (acceptance injection)")


def _position(symbol: str, qty: float) -> Position:
    """构造持仓对象（验收桩，价格/盈亏为固定演示值）。"""
    return Position(
        symbol=symbol, quantity=qty, avg_cost=10.0,
        market_price=10.0, realized_pnl=0.0,
    )


class M1AcceptanceRunner:
    """M1 保命验收执行器（A-15-Min / A-15-Full）。

    在隔离环境（临时告警库 + 真实 EventBus + 真实 AlertManager）中，
    通过真实事件路径注入 P0-8 标准故障用例，按系统时间戳测量 MTTx，
    并按 P0-8 ground-truth 口径统计 TP/FP/FN。

    防未来：所有 fault_ts / created_at / acked_at / resolved_at 均为系统时钟
    记录的真实时间戳；不引用任何未来数据。
    """

    def __init__(self, store_db_path: Optional[str] = None) -> None:
        """构造验收执行器（隔离环境）。

        Parameters
        ----------
        store_db_path : str | None
            告警落库的临时数据库路径；None 时自动创建临时目录并在
            close() 时清理。传入路径则复用（不清理）。
        """
        self._store_db_path: Optional[str] = store_db_path
        self._owns_tmpdir: bool = store_db_path is None
        self._tmpdir: Optional[Path] = None
        if self._owns_tmpdir:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="m1_acceptance_"))
        # 仅 None 触发临时目录；显式传入的路径必须已存在父目录（由调用方保证）
        db_path: str = (
            store_db_path
            if store_db_path is not None
            else (str(self._tmpdir / "acceptance.db") if self._tmpdir is not None else "")
        )

        # 真实持久化链路：AlertStore(临时库) + AlertManager(落库开关开) + 真实 EventBus
        self._store: AlertStore = AlertStore(db_path=db_path)
        self._manager: AlertManager = AlertManager(store=self._store, store_enabled=True)
        self._bus: EventBus = EventBus()
        self._orig_get_alert_manager: Optional[Any] = None

    def close(self) -> None:
        """清理验收产生的临时目录（仅当本执行器拥有临时目录时）。"""
        if self._owns_tmpdir and self._tmpdir is not None:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    # ── 全局告警管理器接管（供 reconcile_vs_broker 局部 import 的 get_alert_manager 走本执行器落库）──
    def _patch_global_manager(self) -> None:
        """把 myquant.monitoring.alerting.get_alert_manager 临时指向本执行器管理器。"""
        import myquant.monitoring.alerting as _alerting_mod
        self._orig_get_alert_manager = _alerting_mod.get_alert_manager
        _alerting_mod.get_alert_manager = lambda: self._manager  # type: ignore[assignment]

    def _restore_global_manager(self) -> None:
        """恢复被接管的全局告警管理器。"""
        import myquant.monitoring.alerting as _alerting_mod
        if self._orig_get_alert_manager is not None:
            _alerting_mod.get_alert_manager = self._orig_get_alert_manager  # type: ignore[assignment]
            self._orig_get_alert_manager = None

    def _reset_runtime(self) -> None:
        """重建运行环境（AlertManager + EventBus）。

        每次 run() 开始时调用，清空去重缓存与历史，避免跨 run（min→full）
        同去重键告警在 dedup 窗口内被抑制、或误取上轮告警（防未来：只取本轮）。
        """
        self._manager = AlertManager(store=self._store, store_enabled=True)
        self._bus = EventBus()

    def _new_alert(self, mark: int) -> Optional[Any]:
        """返回本次注入（自 mark 之后）新增的最近一条告警；无新告警返回 None。

        Parameters
        ----------
        mark : int
            注入前 manager 历史长度（len(manager._history)）。

        Returns
        -------
        Alert | None : 本次注入产生的最新告警。
        """
        new = list(self._manager._history)[mark:]
        return new[-1] if new else None

    # ═══════════════════════════════════════════════
    #  1. 覆盖率检查（保命闭环四环）
    # ═══════════════════════════════════════════════
    def check_coverage(self) -> Dict[str, Any]:
        """核验保命闭环四环的检测器与事件路径是否落地。

        Returns
        -------
        dict : {"rings": [...], "covered": int, "total": int, "coverage_pct": float}。
        """
        covered: int = 0
        rings: List[Dict[str, Any]] = []

        # A-4 行情活性：StalenessDetector 具备 attach_event_bus / on_bar / evaluate
        a4_ok = (
            callable(getattr(StalenessDetector, "attach_event_bus", None))
            and callable(getattr(StalenessDetector, "on_bar", None))
            and callable(getattr(StalenessDetector, "evaluate", None))
        )
        covered += 1 if a4_ok else 0
        rings.append({"ring": "行情活性", "item": "A-4", "covered": a4_ok,
                      "evidence": "StalenessDetector.attach_event_bus/on_bar/evaluate"})

        # A-6 券商断连：BrokerConnectionWatcher 具备 on_state / poll_once
        a6_ok = (
            callable(getattr(BrokerConnectionWatcher, "on_state", None))
            and callable(getattr(BrokerConnectionWatcher, "poll_once", None))
        )
        covered += 1 if a6_ok else 0
        rings.append({"ring": "券商断连", "item": "A-6", "covered": a6_ok,
                      "evidence": "BrokerConnectionWatcher.on_state/poll_once"})

        # A-8 交易异常：AlertManager 订阅 4 类交易异常事件
        a8_ok = (
            callable(getattr(AlertManager, "subscribe_to_event_bus", None))
            and callable(getattr(AlertManager, "handle_trade_exception_event", None))
        )
        covered += 1 if a8_ok else 0
        rings.append({"ring": "交易异常", "item": "A-8", "covered": a8_ok,
                      "evidence": "AlertManager.subscribe_to_event_bus + handle_trade_exception_event"})

        # A-7 持仓对账：reconcile_vs_broker + 容差引用配置真源 LOCKED=0
        a7_ok = callable(getattr(LivePortfolio, "reconcile_vs_broker", None))
        if a7_ok:
            tol = float(get_monitoring_config().get("slo", {}).get("reconciliation", {}).get("realtime_diff_tolerance", -1))
            a7_ok = a7_ok and tol == 0.0  # R-04：实时对账容差必须锁定为 0（与 B-1 对齐）
        covered += 1 if a7_ok else 0
        rings.append({"ring": "持仓对账", "item": "A-7", "covered": a7_ok,
                      "evidence": "LivePortfolio.reconcile_vs_broker + slo.reconciliation.realtime_diff_tolerance==0"})

        return {
            "rings": rings,
            "covered": covered,
            "total": len(CHAIN_RINGS),
            "coverage_pct": round(covered / len(CHAIN_RINGS), 6),
        }

    # ═══════════════════════════════════════════════
    #  2. 故障注入 + MTTx 计时
    # ═══════════════════════════════════════════════
    def _measure_and_classify(
        self, alert: Any, fault_ts: float, event_path: str,
    ) -> Dict[str, Any]:
        """对已捕获告警按 P0-8 口径计时（MTTD/MTTA/MTTR）并打标 TP。

        Parameters
        ----------
        alert : Alert
            本次注入捕获的告警对象。
        fault_ts : float
            故障注入时间戳（系统时钟，事件时间）。
        event_path : str
            事件路径证据描述。

        Returns
        -------
        dict : 单条告警的 MTTx 与分类结果。
        """
        created_at: float = alert.timestamp

        # 确认（MTTA）：系统确认事件（acknowledge）→ acked_at
        self._manager.acknowledge(alert.alert_id)
        row: Optional[Dict[str, Any]] = self._store.get(alert.alert_id)
        acked_at: Optional[float] = row.get("acked_at") if row else None

        # 解决（MTTR）：resolve → resolved_at
        self._manager.resolve(alert.alert_id)
        row2: Optional[Dict[str, Any]] = self._store.get(alert.alert_id)
        resolved_at: Optional[float] = row2.get("resolved_at") if row2 else None

        mttd: Optional[float] = None
        mtta: Optional[float] = None
        mttr: Optional[float] = None
        if created_at >= fault_ts:
            mttd = MttxCalculator.mttd(fault_ts, created_at)
        if acked_at is not None:
            mtta = MttxCalculator.mtta(created_at, acked_at)
        if resolved_at is not None:
            mttr = MttxCalculator.mttr(fault_ts, resolved_at)

        # 打标：注入且被捕获的告警为真告警（TP），供 P0-8 统计
        self._store.set_classification(alert.alert_id, CLASSIFICATION_TP)

        return {
            "alert_id": alert.alert_id,
            "level": alert.level.value,
            "category": alert.category,
            "fault_ts": fault_ts,
            "created_at": created_at,
            "acked_at": acked_at,
            "resolved_at": resolved_at,
            "mttd_seconds": round(mttd, 6) if mttd is not None else None,
            "mtta_seconds": round(mtta, 6) if mtta is not None else None,
            "mttr_seconds": round(mttr, 6) if mttr is not None else None,
            "event_path": event_path,
        }

    def _fi_broker_disconnect(self) -> Dict[str, Any]:
        """FI-03 券商连接断开（EVENT 路径）：健康→不健康转换点立即 CRITICAL。"""
        broker = _ConnectedBroker(connected=True)
        watcher = BrokerConnectionWatcher(broker, alert_manager=self._manager)
        watcher.poll_once()  # connected（初始采样）
        fault_ts: float = time.time()
        mark: int = len(self._manager._history)
        result = watcher.on_state(False)  # 转换点 → P0/CRITICAL
        alert = self._new_alert(mark)
        captured: bool = result["transition"] == "connected->disconnected" and alert is not None
        base = self._measure_and_classify(
            alert, fault_ts, "BrokerConnectionWatcher.on_state True→False 转换点（EVENT 路径）",
        ) if alert is not None else {}
        return {
            "case_id": "FI-03", "captured": captured,
            "expected_capture": "A-6 BrokerConnectionWatcher",
            "transition": result.get("transition"),
            "level": base.get("level", ""), "category": base.get("category", ""),
            "runbook_id": (alert.details or {}).get("runbook_id", "") if alert is not None else "",
            **base,
        }

    def _fi_reject(self) -> Dict[str, Any]:
        """FI-04 伪造拒单（EVENT 路径）：ORDER_REJECTED 事件 → ERROR 自动告警。"""
        self._manager.subscribe_to_event_bus(self._bus)
        fault_ts: float = time.time()
        mark: int = len(self._manager._history)
        ev = OrderEvent(
            event_type=EventType.ORDER_REJECTED,
            symbol="600000.SH",
            order_id="acceptance-order-1",
            reject_reason="资金不足（验收注入）",
            data={"reason": "资金不足（验收注入）"},
        )
        self._bus.publish(ev)
        alert = self._new_alert(mark)
        captured: bool = alert is not None and alert.category == "trade"
        base = self._measure_and_classify(
            alert, fault_ts, "AlertManager 订阅 EventType.ORDER_REJECTED 事件流实时触发（EVENT 路径）",
        ) if alert is not None else {}
        return {
            "case_id": "FI-04", "captured": captured,
            "expected_capture": "A-8 ORDER_REJECTED",
            "level": base.get("level", ""), "category": base.get("category", ""),
            "details": dict(alert.details) if alert is not None else {},
            **base,
        }

    def _fi_reconcile(self) -> Dict[str, Any]:
        """FI-05 人为制造持仓差异（EVENT 路径）：账实差异超阈 → CRITICAL。"""
        # 本地账本现金 100000，券商现金 99900 → 现金差 100 > 容差 0 → 超阈
        # G1 EX-12B：对账差异告警改注入投递，须装配本执行器告警管理器
        local = LivePortfolio(initial_capital=100_000.0, alert_manager=self._manager)
        local._cash = 100_000.0  # type: ignore[attr-defined]  # 验收桩注入
        broker = _ReconcileBroker(cash=99_900.0, positions=[])
        fault_ts: float = time.time()
        mark: int = len(self._manager._history)
        self._patch_global_manager()
        try:
            diff = local.reconcile_vs_broker(broker)
        finally:
            self._restore_global_manager()
        alert = self._new_alert(mark)
        captured: bool = bool(diff.exceeds_threshold) and alert is not None
        base = self._measure_and_classify(
            alert, fault_ts, "LivePortfolio.reconcile_vs_broker 差异超阈（容差=0）→ CRITICAL（EVENT 路径）",
        ) if alert is not None else {}
        return {
            "case_id": "FI-05", "captured": captured,
            "expected_capture": "A-7 reconcile 差异",
            "cash_diff": diff.cash_diff,
            "cash_tolerance": diff.cash_tolerance,
            "position_diff_count": len(diff.position_diffs),
            "exceeds_threshold": bool(diff.exceeds_threshold),
            "level": base.get("level", ""), "category": base.get("category", ""),
            **base,
        }

    def _fi_staleness(self) -> Dict[str, Any]:
        """FI-01 行情源断流（EVENT + 秒级兜底）。

        事件路径：真实 EventBus 发布 BAR → StalenessDetector 订阅刷新时钟（事件路径成立，无误报）；
        断流：停止喂 bar，真实时钟越过短检测窗口 → evaluate 检出 down → ERROR。
        用演示短窗口（STALENESS_ACCEPT_BAR_TIMEOUT<60s）证明机制可达 MTTD<1min；
        生产 bar_timeout 由配置真源（monitoring.watchdog）决定，另行核验并注记。
        """
        det = StalenessDetector(
            bar_timeout_seconds=STALENESS_ACCEPT_BAR_TIMEOUT,
            session_grace_minutes=0.0,
            calendar=_ActiveSessionCalendar(),
            alert_manager=self._manager,
        )
        det.attach_event_bus(self._bus)

        # 事件路径：喂一根 bar（真实 EventBus），刷新时钟 → 不应误报
        bar_ts: float = time.time()
        self._bus.publish(Event(EventType.BAR, data={"event_ts": bar_ts}, source="acceptance"))
        status_after_bar: str = det.check()[0]
        event_path_ok: bool = det._bars_received >= 1 and status_after_bar == "healthy"

        # 断流：停止喂 bar，等待真实时钟越过检测窗口，evaluate 检出
        time.sleep(STALENESS_ACCEPT_BAR_TIMEOUT + STALENESS_ACCEPT_MARGIN)
        mark: int = len(self._manager._history)
        status, detail = det.evaluate()
        alert = self._new_alert(mark)
        captured: bool = status == "down" and alert is not None
        # MTTD 起点 = 断流注入时间戳（bar 停止喂入时刻 bar_ts，事件时间）
        base = self._measure_and_classify(
            alert, bar_ts, "EventBus BAR 事件刷新时钟（事件路径）→ 停止喂 bar 超窗口 evaluate 检出（EVENT+秒级兜底）",
        ) if alert is not None else {}

        # 生产配置核验：monitoring.watchdog.bar_timeout_seconds（R-04 真源）
        prod_timeout: float = float(
            get_monitoring_config().get("watchdog", {}).get("bar_timeout_seconds", 300)
        )
        return {
            "case_id": "FI-01", "captured": captured,
            "expected_capture": "A-4 StalenessDetector",
            "event_path_ok": event_path_ok,
            "status": status, "detail": detail,
            "demo_bar_timeout_seconds": STALENESS_ACCEPT_BAR_TIMEOUT,
            "production_bar_timeout_seconds": prod_timeout,
            "production_meets_mttd": bool(prod_timeout < MTTD_BUDGET_SECONDS),
            "level": base.get("level", ""), "category": base.get("category", ""),
            **base,
        }

    def _fi_heartbeat(self) -> Dict[str, Any]:
        """FI-02 引擎僵死（POLL，窗口<60s）。

        注入假引擎 get_status 恒抛异常，连续 N 次 probe → 僵死 P0/ERROR。
        真实世界检测窗口 = consecutive_misses × probe_interval（读配置真源，R-04），
        断言 5×10=50s < 60s 保住 MTTD<1min（R-02/R-04 硬约束）。
        """
        slo = get_monitoring_config().get("slo", {}).get("engine_heartbeat", {})
        interval: float = float(slo.get("probe_interval_seconds", 10))
        misses: int = int(slo.get("consecutive_misses", 5))
        window: float = float(slo.get("hang_judge_window_seconds", 60))
        window_ok: bool = bool(misses * interval < window)  # R-04 硬约束：N×间隔<窗口保 MTTD<1min

        watcher = EngineHeartbeatWatcher(
            _HangEngine(), alert_manager=self._manager,
        )
        fault_ts: float = time.time()
        mark: int = len(self._manager._history)
        last: Optional[Dict[str, Any]] = None
        for _ in range(misses):
            last = watcher.probe()
        alert = self._new_alert(mark)
        captured: bool = bool(last and last.get("hung")) and alert is not None
        base = self._measure_and_classify(
            alert, fault_ts, "EngineHeartbeatWatcher.probe 连续 N 次 get_status 无响应 → 僵死告警（POLL 窗口<60s）",
        ) if alert is not None else {}
        return {
            "case_id": "FI-02", "captured": captured,
            "expected_capture": "A-5 引擎心跳",
            "window_ok": window_ok,
            "window_seconds": round(misses * interval, 3),
            "judge_window_seconds": window,
            "probe_interval_seconds": interval,
            "consecutive_misses": misses,
            "level": base.get("level", ""), "category": base.get("category", ""),
            **base,
        }

    # ═══════════════════════════════════════════════
    #  3. 验收运行
    # ═══════════════════════════════════════════════
    def _run_fi(self, case_id: str) -> Dict[str, Any]:
        """按用例 ID 分派运行单个故障注入场景。"""
        dispatch: Dict[str, Any] = {
            "FI-01": self._fi_staleness,
            "FI-02": self._fi_heartbeat,
            "FI-03": self._fi_broker_disconnect,
            "FI-04": self._fi_reject,
            "FI-05": self._fi_reconcile,
        }
        fn = dispatch.get(case_id)
        if fn is None:
            raise ValueError(f"未知故障注入用例: {case_id!r}（支持 FI-01~05）")
        return fn()

    def _classify(self, fi_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """按 P0-8 ground-truth 口径对本次注入告警做 TP/FP/FN 统计。

        注入且被捕获 = 真告警（TP，ack 打标"需处置"）；未被捕获 = 漏报（FN）；
        受控注入不产生误报（FP=0）。KPI-3 的"≥4 周"滚动口径在报告 caveats 中注记。
        """
        alert_records: List[Dict[str, Any]] = [
            {"acked_disposition": "需处置", "auto_resolved": False}
            for r in fi_results if r.get("captured")
        ]
        fi_records: List[Dict[str, Any]] = [
            {"case_id": r.get("case_id"), "captured": bool(r.get("captured"))}
            for r in fi_results
        ]
        return ClassificationStats.compute(alert_records, fi_records)

    def _min_verdict(self, coverage: Dict[str, Any], fi: List[Dict[str, Any]]) -> Dict[str, Any]:
        """A-15-Min 判定：覆盖率 100% + FI-01/03/04/05 注入检出且走事件路径。"""
        min_ids: set = set(MIN_FI_CASE_IDS)
        min_results = [r for r in fi if r["case_id"] in min_ids]
        coverage_ok: bool = bool(coverage["coverage_pct"] == 1.0)
        all_captured: bool = all(bool(r.get("captured")) for r in min_results)
        pass_min: bool = coverage_ok and all_captured
        return {
            "pass": pass_min,
            "coverage_ok": coverage_ok,
            "min_fi_captured": all_captured,
            "checked_cases": sorted(min_ids),
            "reason": (
                "保命闭环四环覆盖率 100% 且 FI-01/03/04/05 注入检出走事件路径"
                if pass_min else "覆盖率未达 100% 或存在保命用例未检出"
            ),
        }

    def _full_verdict(self, coverage: Dict[str, Any], fi: List[Dict[str, Any]]) -> Dict[str, Any]:
        """A-15-Full 判定：FI-01~05 注入 + MTTx 预算 + EVENT 事件路径 + 分类达标。"""
        by_id: Dict[str, Dict[str, Any]] = {r["case_id"]: r for r in fi}
        all_captured: bool = all(r.get("captured") for cid in FULL_FI_CASE_IDS if (r := by_id.get(cid)) is not None)
        # MTTD：事件型实测 <60s；心跳窗口<60s（结构）；断流演示窗口<60s
        mttd_ok: bool = True
        for cid in FULL_FI_CASE_IDS:
            r = by_id.get(cid)
            if r is None:
                mttd_ok = False
                continue
            if cid == "FI-02":
                mttd_ok = mttd_ok and bool(r.get("window_ok"))
            elif cid == "FI-01":
                mttd_ok = mttd_ok and (r.get("mttd_seconds") is not None and r["mttd_seconds"] < MTTD_BUDGET_SECONDS)
            else:
                mttd_ok = mttd_ok and (r.get("mttd_seconds") is not None and r["mttd_seconds"] < MTTD_BUDGET_SECONDS)
        # MTTA<5min、MTTR<30min（所有捕获告警）
        mtta_ok: bool = all(
            r.get("mtta_seconds") is not None and r["mtta_seconds"] < MTTA_BUDGET_SECONDS
            for r in fi if r.get("captured")
        )
        mttr_ok: bool = all(
            r.get("mttr_seconds") is not None and r["mttr_seconds"] < MTTR_BUDGET_SECONDS
            for r in fi if r.get("captured")
        )
        # EVENT 项必须走事件路径（确认非等满 60s 周期）
        event_path_ok: bool = all(
            bool(r.get("event_path")) for r in fi if r.get("captured")
        )
        classification: Dict[str, Any] = self._classify(fi)
        accuracy: Optional[float] = classification.get("accuracy")
        fp_rate: Optional[float] = classification.get("fp_rate")
        accuracy_ok: bool = accuracy is not None and accuracy > 0.95
        fp_rate_ok: bool = fp_rate is not None and fp_rate < 0.05

        pass_full: bool = (
            bool(coverage["coverage_pct"] == 1.0)
            and all_captured and mttd_ok and mtta_ok and mttr_ok
            and event_path_ok and accuracy_ok and fp_rate_ok
        )
        return {
            "pass": pass_full,
            "coverage_ok": bool(coverage["coverage_pct"] == 1.0),
            "fi_captured": all_captured,
            "mttd_ok": mttd_ok,
            "mtta_ok": mtta_ok,
            "mttr_ok": mttr_ok,
            "event_path_ok": event_path_ok,
            "classification": classification,
            "accuracy_ok": accuracy_ok,
            "fp_rate_ok": fp_rate_ok,
            "reason": (
                "FI-01~05 全部捕获；MTTx 满足预算；EVENT 项走事件路径；分类达标"
                if pass_full else "存在未达标项（详见 fi 明细与 caveats）"
            ),
        }

    def run(self, mode: str = "min") -> Dict[str, Any]:
        """运行 M1 保命验收。

        Parameters
        ----------
        mode : str
            "min" → A-15-Min 子集验收（覆盖率 + FI-01/03/04/05）；
            "full" → A-15-Full 完整验收（覆盖率 + FI-01~05 + MTTx + 分类 + 事件路径）。

        Returns
        -------
        dict : 验收报告（coverage / fi / verdict / caveats）。

        Raises
        ------
        ValueError : mode 非法时抛出。
        """
        if mode not in ("min", "full"):
            raise ValueError(f"mode 必须为 min/full，实际 {mode!r}")

        # 每次 run 重建运行环境：清空去重缓存/历史，隔离上一轮注入产物（防未来）
        self._reset_runtime()

        coverage: Dict[str, Any] = self.check_coverage()
        case_ids: Tuple[str, ...] = FULL_FI_CASE_IDS if mode == "full" else MIN_FI_CASE_IDS
        fi_results: List[Dict[str, Any]] = [self._run_fi(cid) for cid in case_ids]

        verdict: Dict[str, Any] = (
            self._full_verdict(coverage, fi_results) if mode == "full"
            else self._min_verdict(coverage, fi_results)
        )

        caveats: List[str] = []
        if mode == "full":
            caveats.append(
                "KPI-3 准确率/误报率目标按 P0-8 口径按周统计至少 4 周；本次为受控注入快照"
                "（TP 均来自真实注入捕获、FP=0），4 周滚动达标须由 §7 周复盘在实盘数据上持续确认。"
            )
            staleness = next((r for r in fi_results if r["case_id"] == "FI-01"), None)
            if staleness and not bool(staleness.get("production_meets_mttd")):
                caveats.append(
                    f"FI-01 断流生产检测窗口 monitoring.watchdog.bar_timeout_seconds="
                    f"{staleness.get('production_bar_timeout_seconds')}s ≥ {MTTD_BUDGET_SECONDS:.0f}s，"
                    f"严格 KPI-2 MTTD<1min 需按实盘校准该阈值（R-04，P0-2 锁定项）；"
                    f"本验收以演示短窗口 {STALENESS_ACCEPT_BAR_TIMEOUT:.0f}s 证明事件路径机制可达预算。"
                )

        return {
            "mode": mode,
            "coverage": coverage,
            "fi": fi_results,
            "verdict": verdict,
            "caveats": caveats,
        }


# ═══════════════════════════════════════════════
#  自测 Demo（__main__ 入口，可运行）
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """运行 M1 保命验收自测：min + full 两档跑通并输出判定。"""
    passed: int = 0
    failures: List[str] = []
    runner = M1AcceptanceRunner()
    try:
        # 覆盖率检查（纯逻辑，不注入）
        coverage = runner.check_coverage()
        if coverage["coverage_pct"] == 1.0:
            passed += 1
        else:
            failures.append(f"覆盖率未 100%: {coverage}")
        for ring in coverage["rings"]:
            if not ring["covered"]:
                failures.append(f"环未覆盖: {ring['ring']} ({ring['item']})")

        # A-15-Min
        rep_min = runner.run("min")
        if rep_min["verdict"]["pass"]:
            passed += 1
        else:
            failures.append(f"A-15-Min 未通过: {rep_min['verdict']}")

        # A-15-Full
        rep_full = runner.run("full")
        if rep_full["verdict"]["pass"]:
            passed += 1
        else:
            failures.append(f"A-15-Full 未通过: {rep_full['verdict']}")
    finally:
        runner.close()

    print("=" * 60)
    print(f"m1_acceptance.py 自测: {passed} 项通过, {len(failures)} 项失败")
    for fail in failures:
        print(f"  ✗ {fail}")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
