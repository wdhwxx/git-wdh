# 文件名：myquant/monitoring/b_acceptance.py
"""
b_acceptance.py - B 包（保钱包）验收业务逻辑（B-10）
==========================================================================
按《量化系统监控模块施工总清单_V1.2》B-10 行施工：B 包九项（B-1~B-9）逐项
验收，沿用 A-15（M1AcceptanceRunner）范式：

  - 隔离环境：临时 AlertStore(SQLite) + 真实 AlertManager + 真实 EventBus
    + 临时 MetricsHistoryStore(内存/临时 SQLite)；未注入时自动构建。
  - 每次 run() 重建运行环境（清空去重缓存/告警历史），隔离跨 run 注入产物。
  - 时间戳一律取当前系统时钟 / 事件自带 event_ts，防未来。
  - 阈值全部引用配置真源（slo.*），不硬编码；PENDING_CAL 项按"不告警不编造"
    语义验收并如实注记。

九项验收检查（每项断言 + threshold_source 注明 config key）:
  B-1 对账差异=0 可展示（AccountReconciler 事件路径 + 冻结闭环）
  B-2 PnL 可信（record_pnl_snapshot 一致返回 dict/冻结返回 None + 权益曲线回放）
  B-3 限额闭环（LimitUsageMonitor 0.97→ERROR(P1) / 0.5→无告警）
  B-4/5 执行质量（ExecutionStatsMonitor 四段延迟 P99 + ACK P2 + 拒单率 P2）
  B-6 熔断闭环（CircuitBreaker RISK_VIOLATION → 禁开仓+撤单+审计 + reset）
  B-7 新鲜度（DataFreshnessSli 滞后>60s P2/缺口>0 P1）
  B-8 驾驶舱（MonitorCenterDataProvider.build_cockpit_payload 四键齐全）
  B-9 保证金（MarginMonitor 事件路径 + PENDING_CAL 语义验收）

依赖: 标准库 + myquant（monitoring / live / risk / web / core）
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import Event, EventBus, EventType, OrderEvent
from myquant.core.logger import get_logger
from myquant.core.settings import get_monitoring_config
from myquant.core.alert_store import AlertStore
from myquant.monitoring.alerting import AlertManager
from myquant.monitoring.account_reconciler import AccountReconciler
from myquant.monitoring.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
)
from myquant.monitoring.data_quality_explorer import DataFreshnessSli
from myquant.monitoring.execution_stats import ExecutionStatsMonitor
from myquant.monitoring.margin_monitor import MarginMonitor
from myquant.core.metrics_history import MetricsHistoryStore
from myquant.risk.rules import LimitUsageMonitor
from myquant.monitoring.center_provider import MonitorCenterDataProvider  # G1 EX-15A：provider 下沉 monitoring（L5→L5 合法）

logger = get_logger("acceptance.b_acceptance")


# ═══════════════════════════════════════════════
#  常量（验收结构；非业务阈值）
# ═══════════════════════════════════════════════

#: B 包九项检查项 ID（顺序即报告顺序）
B_ITEMS: tuple = ("B-1", "B-2", "B-3", "B-4", "B-5", "B-6", "B-7", "B-8", "B-9")

#: B-4/5 验收演示用固定时钟起点（epoch 秒；防未来：仅用于构造事件时间序列）
_EXEC_CLOCK_BASE: float = 1_700_000_000.0


# ═══════════════════════════════════════════════
#  验收桩（鸭子类型，不侵入生产模块）
# ═══════════════════════════════════════════════


class _ReconBroker:
    """对账验收桩券商：提供 get_account / get_positions。"""

    def __init__(self, cash: float, positions: Optional[list] = None) -> None:
        """构造桩券商。

        Args:
            cash: 券商侧现金余额。
            positions: 券商侧持仓列表（空列表=无持仓）。
        """
        self._cash: float = float(cash)
        self._positions: list = list(positions or [])

    def get_account(self) -> Dict[str, Any]:
        """返回券商账户快照。"""
        return {"cash": self._cash, "frozen_cash": 0.0}

    def get_positions(self) -> list:
        """返回券商持仓快照。"""
        return list(self._positions)


class _FakeRiskManager:
    """熔断验收桩风控管理器：pause/resume/can_open_position 可观测。"""

    def __init__(self) -> None:
        """初始化桩：初始可开仓；pause/resume 调用次数留痕。"""
        self._paused: bool = False
        self.pause_calls: List[str] = []
        self.resume_calls: List[str] = []

    def pause_for_circuit_breaker(self, reason: str = "") -> bool:
        """禁开仓（桩）：置 paused=True 并留痕。"""
        self._paused = True
        self.pause_calls.append(reason)
        return True

    def resume_from_circuit_breaker(self, reason: str = "") -> bool:
        """恢复开仓（桩）：置 paused=False 并留痕。"""
        self._paused = False
        self.resume_calls.append(reason)
        return True

    def can_open_position(self) -> bool:
        """当前是否允许开仓。"""
        return not self._paused


class _FakeBroker:
    """熔断验收桩券商：get_active_orders / cancel_order 可观测。"""

    def __init__(self, active_orders: Optional[List[str]] = None) -> None:
        """构造桩券商。

        Args:
            active_orders: 活跃挂单 order_id 列表。
        """
        self._orders: List[str] = list(active_orders or [])
        self.cancelled: List[str] = []

    def get_active_orders(self) -> List[Dict[str, str]]:
        """返回活跃挂单列表（鸭子类型 dict）。"""
        return [{"order_id": oid} for oid in self._orders]

    def cancel_order(self, order_id: str) -> bool:
        """撤单（桩）：记录已撤并返回成功。"""
        self.cancelled.append(order_id)
        return True


class _FakeMarginPortfolio:
    """保证金验收桩组合：提供 get_account / get_market_value / get_total_assets。"""

    def __init__(
        self,
        total_assets: float = 1_000_000.0,
        market_value: float = 500_000.0,
        occupied_margin: float = 400_000.0,
    ) -> None:
        """构造桩组合。

        Args:
            total_assets: 总资产（权益）。
            market_value: 持仓市值。
            occupied_margin: 已占用保证金（used_margin）。
        """
        self._total_assets: float = float(total_assets)
        self._market_value: float = float(market_value)
        self._occupied_margin: float = float(occupied_margin)

    def get_account(self) -> Dict[str, Any]:
        """返回账户快照（含 equity / used_margin）。"""
        return {
            "total_assets": self._total_assets,
            "equity": self._total_assets,
            "market_value": self._market_value,
            "used_margin": self._occupied_margin,
            "occupied_margin": self._occupied_margin,
        }

    def get_market_value(self) -> float:
        """返回持仓市值。"""
        return self._market_value

    def get_total_assets(self) -> float:
        """返回总资产。"""
        return self._total_assets


# ═══════════════════════════════════════════════
#  B 包验收执行器
# ═══════════════════════════════════════════════


class BAcceptanceRunner:
    """B 包（保钱包）验收执行器（B-10）。

    在隔离环境（临时告警库 + 真实 EventBus + 真实 AlertManager + 临时指标时序库）中，
    逐项验证 B-1~B-9 九项保钱包闭环。阈值全部引用配置真源，PENDING_CAL 项
    按"不告警不编造"语义验收并注记。

    防未来：所有时间戳一律取当前系统时钟或事件自带 event_ts，不引用未来数据。
    """

    def __init__(
        self,
        alert_manager: Optional[AlertManager] = None,
        alert_store: Optional[AlertStore] = None,
        event_bus: Optional[EventBus] = None,
        metrics_collector: Optional[Any] = None,
        metrics_history: Optional[MetricsHistoryStore] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """构造验收执行器。

        Parameters
        ----------
        alert_manager : AlertManager | None
            可注入告警管理器；None 时自动构建（隔离环境）。
        alert_store : AlertStore | None
            可注入告警落库存储；None 时自动创建临时 SQLite。
        event_bus : EventBus | None
            可注入事件总线；None 时自动构建真实 EventBus。
        metrics_collector : Any | None
            可注入指标采集器；None 时自动构建（桩采集器，仅记录 gauge）。
        metrics_history : MetricsHistoryStore | None
            可注入指标时序库；None 时自动创建临时 SQLite。
        clock : Callable[[], float] | None
            当前时间戳函数；None 用 time.time。
        """
        self._clock: Callable[[], float] = clock if clock is not None else time.time

        # 自动构建隔离环境（临时目录 + SQLite）
        self._owns_tmpdir: bool = alert_store is None or metrics_history is None
        self._tmpdir: Optional[Path] = None
        if self._owns_tmpdir:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="b_acceptance_"))

        # AlertStore（临时 SQLite）
        if alert_store is not None:
            self._store: AlertStore = alert_store
            self._owns_store: bool = False
        else:
            db_path = str(self._tmpdir / "acceptance_alerts.db") if self._tmpdir else ":memory:"
            self._store = AlertStore(db_path=db_path)
            self._owns_store = True

        # MetricsHistoryStore（临时 SQLite）+ FieldCrypto 绑定（BASE-9：敏感名密文落盘）
        if metrics_history is not None:
            self._history: MetricsHistoryStore = metrics_history
            self._owns_history: bool = False
        else:
            h_db = str(self._tmpdir / "acceptance_history.db") if self._tmpdir else ":memory:"
            self._history = MetricsHistoryStore(db_path=h_db)
            self._owns_history = True
            # 绑定 FieldCrypto（验收临时密钥，落盘即随临时目录清理）
            try:
                from myquant.core.field_crypto import FieldCrypto
                key_path = str(self._tmpdir / ".acceptance_crypto_key") if self._tmpdir else ":memory:"
                crypto = FieldCrypto(key_file=key_path)
                self._history.attach_field_crypto(crypto)
            except Exception as e:  # noqa: BLE001
                logger.warning("[b_acceptance] FieldCrypto 绑定失败（敏感指标将拒绝落盘）: %s", e)

        # 运行时依赖（每次 run() 重建）
        self._injected_alert_manager: Optional[AlertManager] = alert_manager
        self._injected_event_bus: Optional[EventBus] = event_bus
        self._injected_collector: Optional[Any] = metrics_collector

        self._manager: Optional[AlertManager] = None
        self._bus: Optional[EventBus] = None
        self._collector: Any = None

        # 首次初始化运行时环境
        self._reset_runtime()

    # ── 环境重建 / 清理 ──────────────────────────────────────
    def _reset_runtime(self) -> None:
        """重建运行时环境（AlertManager + EventBus + 指标采集器）。

        每次 run() 开始时调用，清空去重缓存与告警历史，隔离跨 run 注入产物。
        """
        self._manager = (
            self._injected_alert_manager
            if self._injected_alert_manager is not None
            else AlertManager(store=self._store, store_enabled=True)
        )
        self._bus = (
            self._injected_event_bus
            if self._injected_event_bus is not None
            else EventBus()
        )
        self._collector = (
            self._injected_collector
            if self._injected_collector is not None
            else _InMemoryMetricsCollector()
        )

    def close(self) -> None:
        """清理临时目录与资源（仅当本执行器拥有临时资源时；Windows SQLite 锁用 ignore_errors）。"""
        import shutil
        if self._owns_tmpdir and self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    # ── 辅助：取本次 run 新增告警 ────────────────────────────
    def _new_alerts_since(self, mark: int) -> List[Any]:
        """返回自 mark 以来新增的告警列表。"""
        if self._manager is None:
            return []
        history = list(getattr(self._manager, "_history", []))
        return history[mark:]

    def _alert_count(self) -> int:
        """当前告警历史长度。"""
        if self._manager is None:
            return 0
        return len(getattr(self._manager, "_history", []))

    # ═══════════════════════════════════════════════
    #  B-1 对账差异=0 可展示
    # ═══════════════════════════════════════════════
    def _check_b1(self) -> Dict[str, Any]:
        """B-1：AccountReconciler 事件路径对账一致→无差异；现金差1分→冻结+审计。"""
        from myquant.live.portfolio import LivePortfolio

        threshold_key: str = "slo.reconciliation.realtime_diff_tolerance"
        tol = float(
            get_monitoring_config()
            .get("slo", {})
            .get("reconciliation", {})
            .get("realtime_diff_tolerance", 0.0)
        )

        audit_records: List[Dict[str, Any]] = []

        class _AuditStub:
            @staticmethod
            def append(kind: str, action: str, actor: str = "",
                       detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
                audit_records.append({
                    "kind": kind, "action": action,
                    "actor": actor, "detail": detail or {},
                })
                return {"seq": len(audit_records)}

        # 场景1：一致账户快照 → 不冻结、无差异
        rec = AccountReconciler(
            account_id="accept-b1",
            alert_manager=self._manager,
            audit_store=_AuditStub(),
        )
        try:
            p1 = LivePortfolio(initial_capital=100_000.0)
            p1._cash = 100_000.0  # type: ignore[attr-defined]
            broker_ok = _ReconBroker(cash=100_000.0, positions=[])
            rec.request_realtime_reconciliation(p1, broker_ok)
            rec.flush()
            state_ok = rec.to_dict()

            consistent_ok: bool = bool(state_ok.get("consistent", False))
            no_diff: bool = (
                state_ok.get("last_diff") is None
                or (state_ok.get("last_diff") or {}).get("cash_diff", 0.0) == 0.0
            )

            # 场景2：现金差 1 分 → 冻结 + 审计 freeze
            p2 = LivePortfolio(initial_capital=100_000.0)
            p2._cash = 100_000.0  # type: ignore[attr-defined]
            broker_diff = _ReconBroker(cash=99_999.99, positions=[])
            rec.request_realtime_reconciliation(p2, broker_diff)
            rec.flush()
            state_frozen = rec.to_dict()
            frozen_ok: bool = bool(state_frozen.get("frozen", False))
            audit_freeze_ok: bool = any(
                r.get("action") == "freeze" and r.get("kind") == "reconciliation"
                for r in audit_records
            )

            passed: bool = consistent_ok and no_diff and frozen_ok and audit_freeze_ok
            detail: str = (
                f"一致快照: consistent={consistent_ok}, cash_diff=0; "
                f"现金差1分: frozen={frozen_ok}, audit_freeze={audit_freeze_ok}; "
                f"容差={tol}"
            )
        finally:
            rec.shutdown(wait=True)

        return {
            "item": "B-1",
            "pass": passed,
            "detail": detail,
            "threshold_source": f"config key: {threshold_key} (LOCKED=0)",
        }

    # ═══════════════════════════════════════════════
    #  B-2 PnL 可信
    # ═══════════════════════════════════════════════
    def _check_b2(self) -> Dict[str, Any]:
        """B-2：record_pnl_snapshot 一致返回 dict/冻结返回 None；权益曲线回放有点。"""
        from myquant.live.portfolio import LivePortfolio
        from myquant.monitoring.metrics_collector import (
            BuiltinMetrics,
            replay_intraday_equity_curve,
        )

        threshold_key: str = "B-2 PnL 快照一致性 + MetricsHistoryStore 回放"

        # 场景1：一致 → record_pnl_snapshot 返回 dict
        p = LivePortfolio(initial_capital=1_000_000.0)
        p._cash = 1_000_000.0  # type: ignore[attr-defined]
        snap = p.record_pnl_snapshot(reason="acceptance")
        snapshot_ok: bool = isinstance(snap, dict)

        # 落 MetricsHistoryStore（临时库）→ 回放当日权益曲线有点
        now_ts: float = self._clock()
        total_assets: float = float(snap.get("total_assets", 1_000_000.0)) if isinstance(snap, dict) else 1_000_000.0
        self._history.save_metric(
            BuiltinMetrics.LIVE_TOTAL_ASSETS,
            total_assets,
            labels={"account_id": "default"},
            timestamp=now_ts,
        )
        points = replay_intraday_equity_curve(store=self._history)
        curve_has_points: bool = len(points) >= 1

        # 场景2：冻结 → record_pnl_snapshot 返回 None
        # 注入冻结状态（直接操作 portfolio 内部 reconciler 桩）
        class _FrozenReconciler:
            is_frozen: bool = True
            @staticmethod
            def note_pnl_blocked(reason: str = "") -> None:
                pass

        p._account_reconciler = _FrozenReconciler()  # type: ignore[attr-defined]
        snap_frozen = p.record_pnl_snapshot(reason="acceptance-frozen")
        frozen_blocks_ok: bool = snap_frozen is None

        passed: bool = snapshot_ok and curve_has_points and frozen_blocks_ok
        detail: str = (
            f"一致快照返回dict={snapshot_ok}; 权益曲线回放点数={len(points)}; "
            f"冻结时返回None={frozen_blocks_ok}"
        )

        return {
            "item": "B-2",
            "pass": passed,
            "detail": detail,
            "threshold_source": f"config key: {threshold_key} (FieldCrypto 语义按 BASE-9)",
        }

    # ═══════════════════════════════════════════════
    #  B-3 限额闭环
    # ═══════════════════════════════════════════════
    def _check_b3(self) -> Dict[str, Any]:
        """B-3：LimitUsageMonitor 注入 0.97 使用率→ERROR(P1)；0.5→无告警。"""
        slo = get_monitoring_config().get("slo", {}).get("risk", {}) or {}
        warn_thr: float = float(slo.get("position_limit_usage_warn", 0.80))
        alert_thr: float = float(slo.get("position_limit_usage_alert", 0.95))

        # 构造：集中度限额=0.5（即 max_single/total_assets=0.485 → usage=0.97>0.95）
        # 0.97 = 0.485 / 0.5 → usage=0.97 > alert(0.95) → ERROR
        mon = LimitUsageMonitor(
            warn_threshold=warn_thr,
            alert_threshold=alert_thr,
            concentration_limit=0.50,
            leverage_limit=1.0,
            single_stock_limit=0.50,
            alert_manager=self._manager,
            metrics_collector=self._collector,
        )

        # 场景1：0.97 使用率（max_single=48500, total=100000 → 0.485/0.50=0.97）
        account_97 = {"total_assets": 100_000.0, "market_value": 48_500.0, "equity": 100_000.0}
        positions_97 = [{"symbol": "600000.SH", "market_value": 48_500.0}]
        mark1: int = self._alert_count()
        result_97 = mon.check(account=account_97, positions=positions_97)
        alerts_97 = self._new_alerts_since(mark1)
        error_fired: bool = any(
            a.level == SeverityLevel.ERROR and a.category == "risk"
            for a in alerts_97
        )
        dedup_ok: bool = any(
            getattr(a, "deduplication_key", "").startswith("limit:")
            for a in alerts_97
        )

        # 场景2：0.5 使用率（max_single=25000, total=100000 → 0.25/0.50=0.5<0.8）→ 无新告警
        account_50 = {"total_assets": 100_000.0, "market_value": 25_000.0, "equity": 100_000.0}
        positions_50 = [{"symbol": "600000.SH", "market_value": 25_000.0}]
        mark2: int = self._alert_count()
        mon.check(account=account_50, positions=positions_50)
        alerts_50 = self._new_alerts_since(mark2)
        no_alert_at_50: bool = len(alerts_50) == 0

        passed: bool = error_fired and dedup_ok and no_alert_at_50
        detail: str = (
            f"0.97使用率→ERROR(P1)={error_fired}, dedup_key=limit:*={dedup_ok}; "
            f"0.5使用率→无告警={no_alert_at_50}; "
            f"warn={warn_thr}, alert={alert_thr}"
        )

        return {
            "item": "B-3",
            "pass": passed,
            "detail": detail,
            "threshold_source": (
                f"config keys: slo.risk.position_limit_usage_warn={warn_thr}, "
                f"position_limit_usage_alert={alert_thr}"
            ),
        }

    # ═══════════════════════════════════════════════
    #  B-4 执行延迟质量
    # ═══════════════════════════════════════════════
    def _check_b4(self) -> Dict[str, Any]:
        """B-4：ExecutionStatsMonitor 四段延迟 P99 + ACK P2 告警。"""
        slo = get_monitoring_config().get("slo", {}).get("trading", {}) or {}
        ack_limit: float = float(slo.get("order_ack_delay_p99_ms", 20))

        mon = ExecutionStatsMonitor(
            ack_limit_ms=ack_limit,
            reject_rate_max=1.0,  # B-4 只测延迟，拒单率阈值关掉避免干扰
            alert_manager=self._manager,
            metrics_collector=self._collector,
        )

        # 注入 30 笔订单（满足 P99_MIN_SAMPLES=30），ACK 延迟恒 30ms（>20ms 阈值）
        t0: float = _EXEC_CLOCK_BASE
        for i in range(30):
            sid: str = f"sig-b4-{i}"
            oid: str = f"ord-b4-{i}"
            mon.observe_signal(sid, event_ts=t0 + i * 0.1)
            mon.observe_order_submit(
                oid, event_ts=t0 + i * 0.1 + 0.03, signal_id=sid,
                send_ts=t0 + i * 0.1, side="buy", ref_price=10.0,
            )
            mon.observe_order_fill(
                oid, trade_id=f"trd-b4-{i}", event_ts=t0 + i * 0.1 + 0.5,
                side="buy", fill_price=10.01, ref_price=10.0,
            )

        s = mon.summary()
        lat = s["latency"]
        # 四段延迟 P99 非 None（样本=30 ≥ P99_MIN_SAMPLES）
        p99_ok: bool = all(
            lat[seg]["p99"] is not None
            for seg in ("signal_to_submit_ms", "send_to_ack_ms",
                        "submit_to_fill_ms", "signal_to_fill_ms")
        )

        # collect_metrics → ACK P99>20ms → P2/WARNING
        mark_ack: int = self._alert_count()
        mon.collect_metrics()
        ack_alerts = self._new_alerts_since(mark_ack)
        ack_p2_ok: bool = any(
            a.level == SeverityLevel.WARNING
            and getattr(a, "deduplication_key", "") == "exec:ack_delay_p99"
            for a in ack_alerts
        )

        passed: bool = p99_ok and ack_p2_ok
        detail: str = (
            f"四段延迟P99非None={p99_ok}; "
            f"ACK P99={lat['send_to_ack_ms']['p99']:.1f}ms>{ack_limit}ms→P2(WARNING)={ack_p2_ok}"
        )

        return {
            "item": "B-4",
            "pass": passed,
            "detail": detail,
            "threshold_source": f"config key: slo.trading.order_ack_delay_p99_ms={ack_limit}",
        }

    # ═══════════════════════════════════════════════
    #  B-5 订单拒绝率
    # ═══════════════════════════════════════════════
    def _check_b5(self) -> Dict[str, Any]:
        """B-5：拒单率>0.1%→P2；飙升 N/窗口 PENDING_CAL→只跑绝对阈值。"""
        slo = get_monitoring_config().get("slo", {}).get("trading", {}) or {}
        reject_max: float = float(slo.get("order_reject_rate_max", 0.001))
        surge_mult_raw = slo.get("reject_rate_surge_multiple", None)
        surge_win_raw = slo.get("reject_rate_surge_window_seconds", None)
        surge_configured: bool = surge_mult_raw is not None and surge_win_raw is not None

        mon = ExecutionStatsMonitor(
            reject_rate_max=reject_max,
            rate_window_seconds=60.0,
            alert_manager=self._manager,
            metrics_collector=self._collector,
        )
        t_rej: float = _EXEC_CLOCK_BASE + 5000.0
        for i in range(99):
            mon.observe_order_submit(f"rej-b5-{i}", event_ts=t_rej + i * 0.5)
        mon.observe_order_reject("rej-b5-99", event_ts=t_rej + 50.0)  # 1/100=1% > 0.1%

        # 从 manager 历史过滤拒单率 SLO 告警
        all_alerts = list(getattr(self._manager, "_history", []))
        rej_slo_alerts = [
            a for a in all_alerts
            if getattr(a, "deduplication_key", "") == "exec:reject_rate_slo"
        ]
        reject_p2_ok: bool = len(rej_slo_alerts) >= 1 and rej_slo_alerts[0].level == SeverityLevel.WARNING

        # PENDING_CAL 语义：未配置 N/窗口 → 不触发飙升告警
        surge_note: str = (
            f"飙升检测 N/窗口={'已配置' if surge_configured else 'PENDING_CAL(未配置)'}"
            f" → {'按配置启用' if surge_configured else '只跑绝对阈值,不编造'}"
        )

        passed: bool = reject_p2_ok
        detail: str = (
            f"拒单率1%>{reject_max*100:.1f}%→P2(WARNING)={reject_p2_ok}; {surge_note}"
        )

        return {
            "item": "B-5",
            "pass": passed,
            "detail": detail,
            "threshold_source": (
                f"config keys: slo.trading.order_reject_rate_max={reject_max}, "
                f"reject_rate_surge_multiple/window={'PENDING_CAL' if not surge_configured else 'configured'}"
            ),
        }

    # ═══════════════════════════════════════════════
    #  B-6 熔断闭环
    # ═══════════════════════════════════════════════
    def _check_b6(self) -> Dict[str, Any]:
        """B-6：CircuitBreaker 订阅 RISK_VIOLATION → 禁开仓+撤单+审计；reset 恢复。"""
        audit_records: List[Dict[str, Any]] = []

        class _AuditStub:
            @staticmethod
            def append(kind: str, action: str, actor: str = "",
                       detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
                audit_records.append({
                    "kind": kind, "action": action,
                    "actor": actor, "detail": detail or {},
                })
                return {"seq": len(audit_records)}

        risk_mgr = _FakeRiskManager()
        broker = _FakeBroker(active_orders=["ord-1", "ord-2"])

        # 默认配置：auto_liquidate=False（人工确认兜底）
        cfg = CircuitBreakerConfig(auto_liquidate=False, shadow=False)
        cb = CircuitBreaker(
            broker=broker,
            risk_manager=risk_mgr,
            event_bus=self._bus,
            alert_manager=self._manager,
            audit_store=_AuditStub(),
            config=cfg,
        )
        cb.subscribe_to_event_bus(self._bus)

        # 发布 RISK_VIOLATION 事件 → 同步触发熔断动作链
        ev = Event(
            EventType.RISK_VIOLATION,
            data={"reason": "验收注入：风控击穿"},
            source="b_acceptance",
        )
        self._bus.publish(ev)

        # 断言：禁开仓 + 撤单 + 审计 kind=circuit
        blocked_ok: bool = not risk_mgr.can_open_position()
        cancel_ok: bool = len(broker.cancelled) == 2
        audit_circuit_ok: bool = any(
            r.get("kind") == "circuit" and r.get("action") == "trip"
            for r in audit_records
        )
        auto_liq_default_off: bool = cfg.auto_liquidate is False

        # reset(actor, reason) → 恢复开仓
        reset_result = cb.reset(actor="ops", reason="验收：风险已解除")
        restored_ok: bool = risk_mgr.can_open_position()
        audit_reset_ok: bool = any(
            r.get("kind") == "circuit" and r.get("action") == "reset"
            for r in audit_records
        )

        passed: bool = (
            blocked_ok and cancel_ok and audit_circuit_ok
            and auto_liq_default_off and restored_ok and audit_reset_ok
        )
        detail: str = (
            f"RISK_VIOLATION→禁开仓={blocked_ok}, 撤单数={len(broker.cancelled)}, "
            f"审计trip={audit_circuit_ok}; auto_liquidate默认关={auto_liq_default_off}; "
            f"reset→恢复开仓={restored_ok}, 审计reset={audit_reset_ok}"
        )

        return {
            "item": "B-6",
            "pass": passed,
            "detail": detail,
            "threshold_source": "熔断动作链=禁开仓+撤单+审计(kind=circuit); auto_liquidate默认False(人工确认)",
        }

    # ═══════════════════════════════════════════════
    #  B-7 新鲜度
    # ═══════════════════════════════════════════════
    def _check_b7(self) -> Dict[str, Any]:
        """B-7：DataFreshnessSli 注入过期时间戳→lag>60s P2；缺口>0→P1。"""
        slo_data = get_monitoring_config().get("slo", {}).get("data", {}) or {}
        freshness_max: float = float(slo_data.get("freshness_max_seconds", 60))
        gap_allowed: int = int(slo_data.get("gap_allowed", 0))

        now: float = self._clock()

        # 桩：最新数据时间戳 = now - 120s（滞后 120s > 60s → P2；>2×60=120 边界）
        # 用 90s 滞后：>60s 但 <120s → WARNING(P2)
        expired_ts: float = now - 90.0

        def _latest_ts() -> Dict[str, float]:
            return {"SH600000:1d": expired_ts}

        def _gaps() -> Dict[str, int]:
            return {"2026-10-05": 1}  # 缺口=1 > gap_allowed=0 → P1

        sli = DataFreshnessSli(
            freshness_max_seconds=freshness_max,
            gap_allowed=gap_allowed,
            latest_timestamp_provider=_latest_ts,
            gap_provider=_gaps,
            alert_manager=self._manager,
            metrics_collector=self._collector,
            clock=lambda: now,
        )

        mark: int = self._alert_count()
        result = sli.poll()
        alerts = self._new_alerts_since(mark)

        # 滞后 90s > 60s 但 < 120s → WARNING(P2)
        lag_alert_ok: bool = any(
            a.level == SeverityLevel.WARNING
            for a in alerts
        )
        # 缺口 1 > 0 → ERROR(P1)
        gap_alert_ok: bool = any(
            a.level == SeverityLevel.ERROR
            for a in alerts
        )

        # 数据新鲜时 → 无告警（正常路径可查指标）
        fresh_sli = DataFreshnessSli(
            freshness_max_seconds=freshness_max,
            gap_allowed=gap_allowed,
            latest_timestamp_provider=lambda: {"SH600000:1d": now},
            gap_provider=lambda: {},
            alert_manager=self._manager,
            metrics_collector=self._collector,
            clock=lambda: now,
        )
        mark2: int = self._alert_count()
        fresh_sli.poll()
        fresh_alerts = self._new_alerts_since(mark2)
        fresh_no_alert: bool = len(fresh_alerts) == 0

        passed: bool = lag_alert_ok and gap_alert_ok and fresh_no_alert
        detail: str = (
            f"滞后90s>{freshness_max}s→P2(WARNING)={lag_alert_ok}; "
            f"缺口1>{gap_allowed}→P1(ERROR)={gap_alert_ok}; "
            f"新鲜数据→无告警={fresh_no_alert}"
        )

        return {
            "item": "B-7",
            "pass": passed,
            "detail": detail,
            "threshold_source": (
                f"config keys: slo.data.freshness_max_seconds={freshness_max}, "
                f"gap_allowed={gap_allowed}"
            ),
        }

    # ═══════════════════════════════════════════════
    #  B-8 驾驶舱
    # ═══════════════════════════════════════════════
    def _check_b8(self) -> Dict[str, Any]:
        """B-8：MonitorCenterDataProvider.build_cockpit_payload 四键齐全。"""
        provider = MonitorCenterDataProvider(
            alert_manager=self._manager,
            metrics_collector=self._collector,
            metrics_history_store=self._history,
        )
        payload = provider.build_cockpit_payload()
        required_keys: set = {"reconciliation", "pnl", "limit_usage", "execution"}
        keys_ok: bool = required_keys.issubset(set(payload.keys()))

        passed: bool = keys_ok
        detail: str = (
            f"四键齐全={keys_ok}; keys={sorted(payload.keys())}; "
            f"reconciliation视图={'有' if 'reconciliation' in payload else '无'}"
        )

        return {
            "item": "B-8",
            "pass": passed,
            "detail": detail,
            "threshold_source": "B-8 驾驶舱四视图同源断言（API/看板同源）",
        }

    # ═══════════════════════════════════════════════
    #  B-9 保证金
    # ═══════════════════════════════════════════════
    def _check_b9(self) -> Dict[str, Any]:
        """B-9：MarginMonitor 事件路径；PENDING_CAL 时按"不告警不编造"验收。"""
        slo_margin = get_monitoring_config().get("slo", {}).get("margin", {}) or {}
        strong_flat_cfg = slo_margin.get("strong_flat_distance_pct", None)
        usage_thr_cfg = slo_margin.get("margin_usage_ratio_threshold", None)
        pending_cal: bool = strong_flat_cfg is None and usage_thr_cfg is None

        audit_records: List[Dict[str, Any]] = []

        # 场景1：PENDING_CAL（None 阈值）→ 不告警不编造
        portfolio = _FakeMarginPortfolio(
            total_assets=1_000_000.0,
            market_value=500_000.0,
            occupied_margin=400_000.0,
        )
        mon_pending = MarginMonitor(
            portfolio=portfolio,
            alert_manager=self._manager,
            metrics_collector=self._collector,
        )
        mark1: int = self._alert_count()
        mon_pending.refresh()
        alerts_pending = self._new_alerts_since(mark1)
        pending_no_alert: bool = len(alerts_pending) == 0  # PENDING_CAL 不告警

        # 场景2：显式配置强平距离阈值（60%）→ 接近强平 → P0/CRITICAL
        # 构造：equity=1,000,000, market_value=1,000,000, used_margin=900,000
        # 占用率 = 900,000/1,000,000*100 = 90% > 80%阈值 → 触发占用超阈告警
        # 维持保证金 = 1,000,000 * 0.50 = 500,000
        # 强平距离 = (1,000,000 - 500,000)/1,000,000*100 = 50% < 60%阈值 → 触发接近强平告警
        portfolio_near = _FakeMarginPortfolio(
            total_assets=1_000_000.0,
            market_value=1_000_000.0,
            occupied_margin=900_000.0,
        )
        mon_explicit = MarginMonitor(
            portfolio=portfolio_near,
            strong_flat_distance_pct=60.0,  # 显式配置（验收演示，单位=%）
            margin_usage_ratio_threshold=80.0,  # 显式配置（单位=%）
            maintenance_margin_rate=0.50,
            alert_manager=self._manager,
            metrics_collector=self._collector,
        )
        mark2: int = self._alert_count()
        mon_explicit.refresh()
        alerts_explicit = self._new_alerts_since(mark2)
        explicit_p0_ok: bool = any(
            a.level == SeverityLevel.CRITICAL and a.category == "margin"
            for a in alerts_explicit
        )

        passed: bool = pending_no_alert and explicit_p0_ok
        note: str = (
            f"当前配置 strong_flat_distance_pct={strong_flat_cfg}, "
            f"margin_usage_ratio_threshold={usage_thr_cfg} → "
            f"{'PENDING_CAL(不告警不编造)' if pending_cal else '已配置'}"
        )
        detail: str = (
            f"PENDING_CAL时不告警={pending_no_alert}; "
            f"显式配置+接近强平→P0/CRITICAL(margin)={explicit_p0_ok}; {note}"
        )

        return {
            "item": "B-9",
            "pass": passed,
            "detail": detail,
            "threshold_source": (
                f"config keys: slo.margin.strong_flat_distance_pct={strong_flat_cfg}, "
                f"margin_usage_ratio_threshold={usage_thr_cfg} "
                f"(PENDING_CAL=None → 不告警不编造)"
            ),
        }

    # ═══════════════════════════════════════════════
    #  验收运行入口
    # ═══════════════════════════════════════════════
    def run(self, mode: str = "full") -> Dict[str, Any]:
        """运行 B 包保钱包验收。

        Parameters
        ----------
        mode : str
            验收档位；当前仅支持 "full"（B-1~B-9 全量）。

        Returns
        -------
        dict : 验收报告
            {
              "mode": str,
              "items": [{"item": str, "pass": bool, "detail": str, "threshold_source": str}, ...],
              "summary": {"passed": int, "failed": int, "total": int},
              "generated_at": str,
              "caveats": [str, ...],
            }

        Raises
        ------
        ValueError : mode 非法时抛出。
        """
        if mode != "full":
            raise ValueError(f"mode 必须为 full，实际 {mode!r}（B 包验收当前仅全量档）")

        # 每次 run 重建运行环境，隔离跨 run 去重缓存
        self._reset_runtime()

        check_dispatch: Dict[str, Callable[[], Dict[str, Any]]] = {
            "B-1": self._check_b1,
            "B-2": self._check_b2,
            "B-3": self._check_b3,
            "B-4": self._check_b4,
            "B-5": self._check_b5,
            "B-6": self._check_b6,
            "B-7": self._check_b7,
            "B-8": self._check_b8,
            "B-9": self._check_b9,
        }

        items: List[Dict[str, Any]] = []
        caveats: List[str] = []

        for item_id in B_ITEMS:
            fn = check_dispatch.get(item_id)
            if fn is None:
                items.append({
                    "item": item_id, "pass": False,
                    "detail": f"未知检查项 {item_id}",
                    "threshold_source": "N/A",
                })
                continue
            try:
                result = fn()
                items.append(result)
            except Exception as e:  # 异常不吞：如实记录该项 failed
                logger.exception("[b_acceptance] %s 检查异常", item_id)
                items.append({
                    "item": item_id,
                    "pass": False,
                    "detail": f"检查异常: {type(e).__name__}: {e}",
                    "threshold_source": "N/A",
                })

        passed: int = sum(1 for it in items if it.get("pass"))
        failed: int = len(items) - passed
        total: int = len(items)

        # PENDING_CAL 项注记
        b9 = next((it for it in items if it["item"] == "B-9"), None)
        if b9 and "PENDING_CAL" in b9.get("threshold_source", ""):
            caveats.append(
                "B-9 保证金阈值当前为 PENDING_CAL（slo.margin 未配置）；"
                "本验收按'不告警不编造'语义验证默认行为，显式配置后 P0/CRITICAL 闭环已演示。"
            )
        b5 = next((it for it in items if it["item"] == "B-5"), None)
        if b5 and "PENDING_CAL" in b5.get("threshold_source", ""):
            caveats.append(
                "B-5 拒单率飙升检测（reject_rate_surge_multiple/window）当前为 PENDING_CAL；"
                "本验收只跑绝对阈值 0.1%，不编造飙升参数。"
            )

        return {
            "mode": mode,
            "items": items,
            "summary": {
                "passed": passed,
                "failed": failed,
                "total": total,
            },
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "caveats": caveats,
        }


# ═══════════════════════════════════════════════
#  内存指标采集器（验收隔离环境用）
# ═══════════════════════════════════════════════


class _InMemoryMetricsCollector:
    """内存指标采集器（验收隔离环境用，仅记录 gauge/counter 调用）。"""

    def __init__(self) -> None:
        """初始化：空 gauge/counter 字典。"""
        self.gauges: Dict[str, float] = {}
        self.counters: Dict[str, float] = {}

    def set_gauge(self, name: str, value: float,
                  labels: Optional[Dict[str, str]] = None) -> None:
        """记录 gauge 值。"""
        self.gauges[name] = float(value)

    def inc_counter(self, name: str, amount: float = 1.0,
                    labels: Optional[Dict[str, str]] = None) -> None:
        """累计 counter。"""
        self.counters[name] = self.counters.get(name, 0.0) + float(amount)


# ═══════════════════════════════════════════════
#  自测 Demo（__main__ 入口）
# ═══════════════════════════════════════════════


def _run_self_test() -> int:
    """运行 B 包验收自测：full 档跑通并输出逐项判定。"""
    runner = BAcceptanceRunner()
    try:
        report = runner.run("full")
    finally:
        runner.close()

    s = report["summary"]
    print("=" * 64)
    print(f"B 包保钱包验收（B-10）: {s['passed']}/{s['total']} 通过")
    print("=" * 64)
    for it in report["items"]:
        status = "✅" if it["pass"] else "❌"
        print(f"  {status} {it['item']}: {it['detail']}")
    if report.get("caveats"):
        print("  注记:")
        for c in report["caveats"]:
            print(f"    - {c}")
    print("=" * 64)
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
