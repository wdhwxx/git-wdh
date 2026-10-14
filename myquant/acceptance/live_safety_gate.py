# 文件名：myquant/acceptance/live_safety_gate.py
"""
live_safety_gate.py - 实盘安全门禁第0阶段验收（V3 §九 用例映射）
==========================================================================
依据：docs/风控实盘安全门禁/风控_实盘安全门禁施工清单.MD §12
      docs/风控实盘安全门禁/风控_实盘安全门禁施工方案优化版.MD（V3）§九

验收范式（沿用 A-15 / B-10）：
  - 隔离环境：临时 AlertStore + 真实 AlertManager + 真实 EventBus + 临时审计库；
    每次 run() 重建运行环境，隔离跨 run 注入产物。
  - 故障注入 → 预期拦截 → 审计留痕 三元组；异常不吞，如实记录该项 failed。
  - 时间戳一律取系统时钟 / 事件自带 event_ts，防未来（不使用未来数据）。
  - 阈值引用真源：P0-2（SLO 锁定）/ P0-8（MTTD<1min / MTTA<5min / MTTR<30min），
    不自行编造第二套数值。

验收项（item id 与施工清单 §12 映射一致）：
  0.1-9    用例9：并发第二引擎 → A 引擎 RISK_VIOLATION 不触发 B 动作链（引擎隔离）
  0.1-10   用例10：熔断触发 → 禁开仓 + 撤全部挂单 + 审计留痕 + CRITICAL 告警
  0.1-F2   F-2：事件总线不可用 → 熔断订阅安全降级（不抛、不阻断交易）
  （0.2~0.9 与 LIVE 门槛项随施工推进逐项登记）

依赖: 标准库 + myquant（monitoring / core / live / risk）
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from myquant.core.alert_store import AlertStore
from myquant.core.audit_log import AuditLogStore
from myquant.core.constants import SeverityLevel
from myquant.core.event_bus import Event, EventBus, EventType
from myquant.core.logger import get_logger
from myquant.monitoring.alerting import AlertManager
from myquant.monitoring.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
)

logger = get_logger("acceptance.live_safety_gate")


# ═══════════════════════════════════════════════
#  常量（验收结构；非业务阈值）
# ═══════════════════════════════════════════════

#: 第0阶段全部验收项 ID（顺序即报告顺序；随施工推进追加）
GATE_ITEMS: tuple = (
    "0.1-9",
    "0.1-10",
    "0.1-F2",
    "0.2-10",
    "0.2-R4",
    "0.3-R5",
    "0.4-4",
    "0.4-R6",
    "0.4-F1",
    "0.5-2",
    "0.5-F3",
    "0.5-LIVE",
    "0.6-5",
    "0.6-GREP",
    "0.6-F4",
    "0.7-8",
    "0.7-C2",
    "0.9-12",
    "0.9-R7",
    "0.8-4",
    "0.8-R5",
    "0.8-F1",
    "0.8-F2",
    "0.8-F5",
    "LIVE-1",
    "LIVE-3",
    "LIVE-6",
    "LIVE-7",
    "LIVE-13",
    "LIVE-F6",
    "LIVE-R123",
)

#: 阈值真源引用（I-5：只引用既有真源，不编造）
_MTTD_SOURCE: str = "P0-8 MTTD<1min"
_MTTA_SOURCE: str = "P0-8 MTTA<5min"


# ═══════════════════════════════════════════════
#  验收桩（鸭子类型，不侵入生产模块）
# ═══════════════════════════════════════════════


class _FakeOrder:
    """桩挂单对象：携带 order_id 属性（CB 撤单经 getattr(order, 'order_id') 读取）。"""

    def __init__(self, order_id: str) -> None:
        """构造桩挂单。

        Args:
            order_id: 挂单号。
        """
        self.order_id: str = order_id


class _FakePosition:
    """桩持仓对象：quantity/extra.available_qty/market_price（应急平仓读取方式）。"""

    def __init__(
        self,
        symbol: str,
        quantity: float,
        market_price: float,
        available_qty: Optional[float] = None,
    ) -> None:
        """构造桩持仓。

        Args:
            symbol: 标的代码。
            quantity: 持仓总量。
            market_price: 快照市价。
            available_qty: 可卖数量（None=不写 extra，走总量回退）。
        """
        self.symbol: str = symbol
        self.quantity: float = quantity
        self.market_price: float = market_price
        self.extra: Dict[str, Any] = (
            {"available_qty": available_qty} if available_qty is not None else {}
        )


class _FakeBroker:
    """熔断/应急验收桩券商：get_active_orders / cancel_order 可观测，应急平仓能力齐备。"""

    def __init__(
        self,
        active_orders: Optional[List[str]] = None,
        cash: float = 100_000.0,
        positions: Optional[List[Any]] = None,
    ) -> None:
        """构造桩券商。

        Args:
            active_orders: 活跃挂单 order_id 列表（前置状态：有挂单）。
            cash: 券商账户现金（0.6 对账验收用）。
            positions: 券商持仓快照；None 时返回默认固定持仓（应急平仓验收前置状态）。
        """
        self._orders: List[str] = list(active_orders or [])
        self._cash = float(cash)
        self._positions: Optional[List[Any]] = (
            list(positions) if positions is not None else None
        )
        self.cancelled: List[str] = []
        self.submitted: List[Any] = []

    def get_account(self) -> Dict[str, Any]:
        """券商权威账户快照（0.6 对账验收：现金）。"""
        return {"cash": self._cash, "account_id": "fake-broker"}

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Any]:
        """返回活跃挂单快照（带 order_id 属性的桩对象；symbol 过滤留空=全部）。"""
        return [_FakeOrder(oid) for oid in self._orders]

    def cancel_order(self, order_id: str) -> bool:
        """撤单（桩）：记录并移除挂单。"""
        self.cancelled.append(order_id)
        if order_id in self._orders:
            self._orders.remove(order_id)
        return True

    # ── 应急平仓（R-4/R-5）所需能力 ──

    def is_connected(self) -> bool:
        """桩：恒已连接。"""
        return True

    def connect(self) -> bool:
        """桩：连接成功。"""
        return True

    def get_positions(self) -> List[Any]:
        """桩：返回持仓快照（None→默认固定持仓供应急平仓验收；显式传参→按注入返回）。"""
        if self._positions is None:
            return [_FakePosition("000001.SZ", 100, 10.0, available_qty=100)]
        return list(self._positions)

    def submit_order(self, order: Any) -> Any:
        """桩：记录并返回订单（不真实提交）。"""
        self.submitted.append(order)
        return order


class _FakeRisk:
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


class _FakeAuditStore:
    """熔断验收桩审计：append 调用留痕（kind=circuit 断言用）。"""

    def __init__(self) -> None:
        """初始化空记录表。"""
        self.rows: List[Dict[str, Any]] = []

    def append(self, kind: str, action: str, actor: str = "",
               detail: Optional[Dict[str, Any]] = None) -> None:
        """追加审计记录（桩）。"""
        self.rows.append({"kind": kind, "action": action, "actor": actor,
                          "detail": dict(detail or {})})

    def query(self, kind: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """查询审计记录（桩）。"""
        rows = [r for r in self.rows if not kind or r["kind"] == kind]
        return list(reversed(rows))[:limit]


# ═══════════════════════════════════════════════
#  验收执行器
# ═══════════════════════════════════════════════


class _CaptureHandler(logging.Handler):
    """日志捕获 handler（0.8 验收：断言降级 CRITICAL 留痕）。"""

    def __init__(self, records: list) -> None:
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


class LiveSafetyGateRunner:
    """实盘安全门禁第0阶段验收执行器。

    每次 run() 重建隔离环境（临时 AlertStore/审计库 + 真实 AlertManager +
    真实 EventBus），隔离跨 run 注入产物；异常不吞，如实记录该项 failed。
    """

    def __init__(self) -> None:
        """初始化执行器（运行环境在 run() 时按需构建）。"""
        self._tmpdir: Optional[Path] = None
        self._owns_tmpdir: bool = False
        self._manager: Optional[AlertManager] = None
        self._bus: Optional[EventBus] = None
        self._audit: Optional[AuditLogStore] = None
        self._alert_store: Optional[AlertStore] = None

    def _reset_runtime(self) -> None:
        """重建运行环境（临时 AlertStore/审计库 + 真实 AlertManager + 真实 EventBus）。"""
        if self._owns_tmpdir and self._tmpdir is not None:
            import shutil

            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        self._tmpdir = Path(tempfile.mkdtemp(prefix="live_gate_accept_"))
        self._owns_tmpdir = True
        self._alert_store = AlertStore(db_path=str(self._tmpdir / "alerts.db"))
        self._manager = AlertManager(store=self._alert_store, store_enabled=True)
        self._bus = EventBus()
        self._audit = AuditLogStore(db_path=str(self._tmpdir / "audit.db"))

    def close(self) -> None:
        """清理临时目录（Windows SQLite 锁用 ignore_errors）。"""
        import shutil

        if self._owns_tmpdir and self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
            self._owns_tmpdir = False

    # ── 0.1 熔断入进程 ─────────────────────────────────────

    def _check_01_engine_isolation(self) -> Dict[str, Any]:
        """用例9：并发第二引擎 → A 引擎 RISK_VIOLATION 不触发 B 动作链（即时）。"""
        bus = EventBus()
        risk_a = _FakeRisk()
        broker_a = _FakeBroker(["o1"])
        risk_b = _FakeRisk()
        broker_b = _FakeBroker(["o2"])
        cb_a = CircuitBreaker(
            broker=broker_a, risk_manager=risk_a, event_bus=bus,
            engine_id="engine_a",
        )
        cb_b = CircuitBreaker(
            broker=broker_b, risk_manager=risk_b, event_bus=bus,
            engine_id="engine_b",
        )
        cb_a.subscribe_to_event_bus()
        cb_b.subscribe_to_event_bus()

        # 注入：仅 A 引擎的风控击穿事件（data.engine_id=engine_a）
        bus.publish(Event(
            event_type=EventType.RISK_VIOLATION,
            data={"reason": "A 引擎风控击穿", "engine_id": "engine_a"},
            source="live_safety_gate",
        ))

        passed = bool(
            cb_a.tripped
            and not cb_b.tripped
            and broker_a.cancelled == ["o1"]
            and broker_b.cancelled == []
            and not risk_a.can_open_position()
            and risk_b.can_open_position()
        )
        detail = (
            f"A.tripped={cb_a.tripped}, B.tripped={cb_b.tripped}, "
            f"A.撤单={broker_a.cancelled}, B.撤单={broker_b.cancelled}, "
            f"A.可开仓={risk_a.can_open_position()}, B.可开仓={risk_b.can_open_position()}"
        )
        return {
            "item": "0.1-9",
            "pass": passed,
            "detail": detail,
            "threshold_source": _MTTD_SOURCE,
        }

    def _check_01_circuit_chain(self) -> Dict[str, Any]:
        """用例10（0.1 侧）：熔断触发 → 禁开仓 + 撤全部挂单 + 审计留痕 + CRITICAL 告警。

        前置状态：有挂单（o1/o2）+ 初始可开仓（空仓注入不能证明任何事）。
        """
        risk = _FakeRisk()
        broker = _FakeBroker(["o1", "o2"])
        audit = _FakeAuditStore()
        cb = CircuitBreaker(
            broker=broker, risk_manager=risk, event_bus=self._bus,
            alert_manager=self._manager, audit_store=audit,
            config=CircuitBreakerConfig(auto_liquidate=False),
            engine_id="engine_a",
        )
        cb.subscribe_to_event_bus()

        self._bus.publish(Event(
            event_type=EventType.RISK_VIOLATION,
            data={"reason": "账户回撤击穿阈值", "engine_id": "engine_a"},
            source="live_safety_gate",
        ))

        trip_rows = [r for r in audit.rows if r["kind"] == "circuit"]
        critical_alerts = [
            r for r in self._alert_store.load_history(limit=50)
            if str(r.get("level", "")).lower() == SeverityLevel.CRITICAL.value.lower()
        ]
        passed = bool(
            cb.tripped
            and not risk.can_open_position()
            and broker.cancelled == ["o1", "o2"]
            and any(r["action"] == "trip" for r in trip_rows)
            and len(critical_alerts) >= 1
        )
        detail = (
            f"tripped={cb.tripped}, 禁开仓={not risk.can_open_position()}, "
            f"撤单={broker.cancelled}, 审计trip={any(r['action']=='trip' for r in trip_rows)}, "
            f"CRITICAL告警数={len(critical_alerts)}"
        )
        return {
            "item": "0.1-10",
            "pass": passed,
            "detail": detail,
            "threshold_source": _MTTD_SOURCE,
        }

    def _check_01_f2(self) -> Dict[str, Any]:
        """F-2：事件总线不可用 → 熔断订阅安全降级（不抛、不阻断交易）。

        0.1 层面验证降级行为；"能力缺失→拒启"的完整语义由 0.8 启动断言覆盖。
        """
        import unittest.mock as mock

        cb = CircuitBreaker(broker=_FakeBroker(), risk_manager=_FakeRisk(),
                            event_bus=None, engine_id="engine_a")
        try:
            with mock.patch(
                "myquant.monitoring.circuit_breaker.get_event_bus_safe",
                return_value=None,
            ):
                handler_ids = cb.subscribe_to_event_bus()
        except Exception as exc:  # 总线不可用不得抛出
            return {
                "item": "0.1-F2",
                "pass": False,
                "detail": f"总线不可用时 subscribe 抛出异常: {type(exc).__name__}: {exc}",
                "threshold_source": "V3 §十.5（外部依赖偶发不可用→降级+CRITICAL，不拒启）",
            }
        passed = handler_ids == []
        return {
            "item": "0.1-F2",
            "pass": passed,
            "detail": f"总线不可用→subscribe 安全降级，返回订阅数={len(handler_ids)}",
            "threshold_source": "V3 §十.5（外部依赖偶发不可用→降级+CRITICAL，不拒启）",
        }

    # ── 0.2 引擎注入 pause_on_critical + 隔离复位（A1） ──────────────

    def _check_02_sync_pause(self) -> Dict[str, Any]:
        """用例10（0.2 侧）+ A1：CRITICAL 健康 → engine.pause 同步风控 PAUSED；
        复位隔离（resume_engine_holds）且不误清数据质量暂停。"""
        from myquant.live.engine import EngineState, LiveEngine
        from myquant.risk.risk_manager import RiskManager

        # 场景A（A1 硬约束）：数据质量暂停在，引擎复位不得误清、引擎保持 PAUSED
        rm_a = RiskManager()
        eng_a = LiveEngine(risk_manager=rm_a)
        eng_a._state = EngineState.RUNNING
        rm_a._pause_for_data_quality(
            "000001.SZ", "验收注入数据质量暂停", global_pause=True,
        )
        eng_a.pause("CRITICAL 健康巡检")
        eng_a.resume("风险解除")
        a1_ok = bool(
            eng_a._state == EngineState.PAUSED
            and rm_a.is_paused()
            and rm_a._dq_global_paused  # 数据质量暂停未被误清
        )
        detail_a = (
            f"DQ暂停下: engine保持PAUSED={eng_a._state == EngineState.PAUSED}, "
            f"风控PAUSED={rm_a.is_paused()}, DQ标记未被误清={rm_a._dq_global_paused}"
        )

        # 场景B：正常联动（pause 同步置位 / resume 隔离复位）
        rm_b = RiskManager()
        eng_b = LiveEngine(risk_manager=rm_b)
        eng_b._state = EngineState.RUNNING
        eng_b.pause("CRITICAL 健康巡检")
        pause_ok = bool(
            eng_b._state == EngineState.PAUSED
            and rm_b.is_paused()
            and rm_b._engine_paused
            and eng_b._pause_source == "CRITICAL 健康巡检"
        )
        eng_b.resume("风险解除")
        resume_ok = bool(
            eng_b._state == EngineState.RUNNING
            and not rm_b.is_paused()
            and not rm_b._engine_paused
            and eng_b._pause_source == ""
        )
        detail_b = (
            f"联动: pause同步风控={pause_ok}, resume隔离复位={resume_ok} "
            f"(pause_source='{eng_b._pause_source}')"
        )

        passed = bool(a1_ok and pause_ok and resume_ok)
        return {
            "item": "0.2-10",
            "pass": passed,
            "detail": f"{detail_a}; {detail_b}",
            "threshold_source": "V3 A1（复位只清自己置位的，不误清数据质量暂停）；P0-8 MTTD<1min",
        }

    def _check_02_r4(self) -> Dict[str, Any]:
        """R-4：engine.pause()（风控 PAUSED）状态下，应急平仓路径仍可用（I-1/I-3）。"""
        from myquant.live.emergency_liquidation import EmergencyLiquidationService
        from myquant.live.engine import EngineState, LiveEngine
        from myquant.risk.risk_manager import RiskManager

        rm = RiskManager()
        eng = LiveEngine(risk_manager=rm)
        eng._state = EngineState.RUNNING
        eng.pause("CRITICAL 健康巡检")

        broker = _FakeBroker(["o1"])
        svc = EmergencyLiquidationService(broker=broker, event_bus=self._bus)
        report = svc.execute(confirm=False)  # dry_run：只生成计划不真实下单

        passed = bool(
            report.get("ok") is True
            and report.get("mode") == "DRY_RUN"
            and report.get("liquidation", {}).get("planned", 0) >= 1
            and len(report.get("cancelled_orders", [])) >= 1
            and report["cancelled_orders"][0].get("dry_run") is True  # dry_run 只生成撤单计划
            and rm.is_paused()  # 应急链不改变风控暂停状态
        )
        detail = (
            f"engine.PAUSED 下应急平仓: ok={report.get('ok')}, "
            f"mode={report.get('mode')}, 计划平仓={report.get('liquidation', {}).get('planned', 0)}, "
            f"撤单计划={len(report.get('cancelled_orders', []))}笔(dry_run), "
            f"风控保持PAUSED={rm.is_paused()}"
        )
        return {
            "item": "0.2-R4",
            "pass": passed,
            "detail": detail,
            "threshold_source": "V3 §九 R-4（pause 下应急路径可用）；I-1/I-3",
        }

    # ── 0.3 应急链独立断言（R-5） ──────────────────────────────

    def _check_03_r5(self) -> Dict[str, Any]:
        """R-5：应急链独立——一键平仓/应急服务/装配工厂不依赖引擎/风控/guardian/门禁；
        破坏装配（阻断业务链模块 import）后仍可装配券商并 dry_run 平仓（I-1）。"""
        import subprocess
        import sys as _sys

        root = Path(__file__).resolve().parents[2]  # myquant/acceptance/… → 项目根
        forbidden = (
            "from myquant.live.engine",
            "from myquant.risk",
            "from myquant.monitoring.circuit_breaker",
            "OperationGuard",
            "from myquant.live.guardian",
            "from myquant.live.acceptance",
        )
        files = [
            root / "scripts" / "一键平仓.py",
            root / "myquant" / "live" / "emergency_liquidation.py",
            root / "myquant" / "live" / "broker_factory.py",
        ]
        violations: List[str] = []
        for f in files:
            text = f.read_text(encoding="utf-8")
            for pat in forbidden:
                if pat in text:
                    violations.append(f"{f.name}: {pat}")
        static_ok = not violations

        # 动态断言：阻断 engine/risk/guardian/门禁 import 后应急链仍可用
        code = (
            "import sys\n"
            "for m in ['myquant.live.engine','myquant.risk.risk_manager',"
            "'myquant.live.guardian','myquant.monitoring.circuit_breaker',"
            "'myquant.live.acceptance']:\n"
            "    class _B:\n"
            "        def __getattr__(self, n): raise ImportError('破坏装配: ' + m)\n"
            "    sys.modules[m] = _B()\n"
            "from myquant.live.broker_factory import build_broker_from_config\n"
            "from myquant.live.emergency_liquidation import EmergencyLiquidationService\n"
            "b = build_broker_from_config({'type': 'paper'}, {}, 100000)\n"
            "r = EmergencyLiquidationService(b).execute(confirm=False)\n"
            "print('OK' if r.get('ok') else 'FAIL')\n"
        )
        try:
            res = subprocess.run(
                [_sys.executable, "-c", code],
                cwd=str(root), capture_output=True, text=True, timeout=120,
            )
        except Exception as exc:  # noqa: BLE001 - 子进程失败如实记录
            return {
                "item": "0.3-R5",
                "pass": False,
                "detail": f"阻断装配子进程执行失败: {type(exc).__name__}: {exc}",
                "threshold_source": "V3 §九 R-5（破坏装配仍可平仓）；I-1",
            }
        dynamic_ok = bool(res.returncode == 0 and "OK" in res.stdout)
        tail = (res.stderr or res.stdout or "")[-200:]
        detail = (
            f"静态违规import={violations or '无'}; "
            f"破坏装配后应急平仓: {'OK' if dynamic_ok else 'FAIL(' + tail + ')'}"
        )
        return {
            "item": "0.3-R5",
            "pass": bool(static_ok and dynamic_ok),
            "detail": detail,
            "threshold_source": "V3 §九 R-5（破坏装配仍可平仓）；I-1",
        }

    # ── 0.4 OperationGuard 注册 + fail-closed + actor_type（用例4/R-6/F-1） ──

    def _check_04_case4(self) -> Dict[str, Any]:
        """用例4：清空 guard 拒启——未装配准入守卫时进入 LIVE 被拒（fail-closed）。"""
        from myquant.live.run_mode import RunMode, apply_mode_to_config
        from myquant.risk.guardian import get_operation_guard, register_operation_guard

        prev = get_operation_guard()
        rejected = False
        try:
            register_operation_guard(None)  # 清空 guard（模拟未装配）
            cfg: Dict[str, Any] = {"live": {}, "broker": {"type": "qmt", "account": "a1"}}
            try:
                apply_mode_to_config(cfg, RunMode.LIVE)
            except ValueError:
                rejected = True
        finally:
            register_operation_guard(prev)

        allowed = False
        try:
            from myquant.api.middleware.auth import OperationGuard

            register_operation_guard(OperationGuard())  # 已装配（enabled=false 仅审计）
            cfg2: Dict[str, Any] = {"live": {}, "broker": {"type": "qmt", "account": "a1"}}
            out = apply_mode_to_config(cfg2, RunMode.LIVE)
            allowed = bool(out["live"]["trade_mode"] == "live" and out["broker"]["mode"] == "LIVE")
        finally:
            register_operation_guard(prev)

        return {
            "item": "0.4-4",
            "pass": bool(rejected and allowed),
            "detail": f"未装配守卫→LIVE拒启={rejected}; 已装配守卫→LIVE放行={allowed}",
            "threshold_source": "V3 §九 用例4（清空 guard 拒启）；0.4 fail-closed",
        }

    def _check_04_r6(self) -> Dict[str, Any]:
        """R-6：require_reviewer=true 下 system 角色（guardian/引擎/应急）force_close 仍可执行；
        human 角色 reviewer 缺失则拒绝；审计保留（actor_type 记录）。"""
        from myquant.api.middleware.auth import OperationGuard
        from myquant.risk import RiskManager
        from myquant.risk.guardian import (
            RiskGuardian,
            get_operation_guard,
            register_operation_guard,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "admission.yaml"
            cfg_path.write_text(
                "admission:\n  enabled: true\n  require_reviewer: true\n"
                '  reviewer: "lawyer_a"\n',
                encoding="utf-8",
            )
            guard = OperationGuard(str(cfg_path))
            # human：reviewer 缺失 → 拒绝
            ok_h, why_h = guard.check(
                "force_close", operator="ops", reviewer=None, action="",
                actor_type="human",
            )
            # system：跳过复核人校验 → 放行（审计保留）
            ok_s, why_s = guard.check(
                "force_close", operator="guardian", action="force_close_all",
                actor_type="system",
            )
            guard_ok = bool(not ok_h and "复核人" in why_h and ok_s)

            # guardian 装配 require_reviewer 守卫后（LIVE 模式）：force_close 不被准入焊死
            prev = get_operation_guard()
            try:
                register_operation_guard(guard)
                g = RiskGuardian(RiskManager(), trade_mode="live")
                before = len(g._decisions)
                g.force_close_all(reason="R-6 验收（system 角色）")
                # 决策被记录 = 准入放行（v1 无回调返回 False 是 KP-M02 语义，非准入拒绝）
                guardian_ok = len(g._decisions) > before
            finally:
                register_operation_guard(prev)

            # 审计保留：system 放行留痕带 actor_type
            audit_kept = any(
                r.get("actor_type") == "system" and r.get("result") == "approved"
                for r in self._last_audit_rows(guard)
            )

        return {
            "item": "0.4-R6",
            "pass": bool(guard_ok and guardian_ok and audit_kept),
            "detail": (
                f"human拒={not ok_h}, system放行={ok_s}, "
                f"guardian.force_close 不被焊死={guardian_ok}, 审计含system留痕={audit_kept}"
            ),
            "threshold_source": "V3 §九 R-6（require_reviewer 不焊死 system 角色应急链）；决策点3",
        }

    def _last_audit_rows(self, guard: Any) -> List[Dict[str, Any]]:
        """读取守卫 JSONL 审计尾部（验收断言审计保留用）。"""
        import json

        rows: List[Dict[str, Any]] = []
        try:
            audit_file = Path(getattr(guard, "_audit_dir", "logs/audit")) / "write_audit.jsonl"
            if audit_file.exists():
                for line in audit_file.read_text(encoding="utf-8").splitlines()[-20:]:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except Exception:
            rows = []
        return rows

    def _check_04_f1(self) -> Dict[str, Any]:
        """F-1：准入配置加载失败 → load_error 留痕 → LIVE 入口判定拒启（能力缺失 fail-closed）。"""
        from myquant.api.middleware.auth import OperationGuard

        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "no_such_admission.yaml"
            guard = OperationGuard(str(bad_path))
            load_failed = bool(guard.load_error)
            # 与 run_实盘入口.py LIVE 段判定一致：守卫非空且 load_error 非空 → 拒启
            entry_reject = guard is not None and bool(getattr(guard, "load_error", ""))

        return {
            "item": "0.4-F1",
            "pass": bool(load_failed and entry_reject),
            "detail": (
                f"配置加载失败→load_error留痕={load_failed}; "
                f"LIVE入口判定拒启={entry_reject}"
            ),
            "threshold_source": "V3 §十.5 F-1（配置加载失败→能力缺失→拒启）",
        }

    # ── 0.5 watchdog 收归 live 启动门（用例2 / F-3 / 启动断言） ──

    def _check_05_case2(self) -> Dict[str, Any]:
        """用例2：券商断连 → 看门狗告警 + 禁开仓（on_disconnect→engine.pause）；
        恢复 → on_recover（resume，仅补发未成交）；MTTD ≤ P0-8 1min（轮询间隔）。"""
        from myquant.core.constants import BROKER_PROBE_INTERVAL_SECONDS
        from myquant.monitoring.watchdog import BrokerConnectionWatcher

        events: List[str] = []
        mgr = self._fake_alert_manager()
        bw = BrokerConnectionWatcher(
            broker=_FakeBroker(), alert_manager=mgr,
            on_disconnect=lambda: events.append("pause"),
            on_recover=lambda: events.append("resume"),
        )
        # 健康 → 断连（模拟拔网线 60s 后看门狗检测到转换）
        d1 = bw.on_state(True)
        d2 = bw.on_state(False)
        # 恢复
        d3 = bw.on_state(True)

        mttd_ok = BROKER_PROBE_INTERVAL_SECONDS <= 60  # P0-8 MTTD ≤ 1min（轮询级）
        return {
            "item": "0.5-2",
            "pass": bool(
                d1["transition"] == "initial"
                and d2["transition"] == "connected->disconnected"
                and d3["transition"] == "disconnected->connected"
                and events == ["pause", "resume"]
                and mttd_ok
            ),
            "detail": (
                f"断连转换={d2['transition']}, 联动={events}, "
                f"恢复转换={d3['transition']}, 轮询间隔={BROKER_PROBE_INTERVAL_SECONDS}s≤60s={mttd_ok}"
            ),
            "threshold_source": "V3 §九 用例2（断连禁开仓 + 恢复仅补发未成交）；P0-8 MTTD ≤ 1min",
        }

    def _fake_alert_manager(self) -> Any:
        class _M:
            def alert(self, **kw: Any) -> None:
                return None

        return _M()

    def _check_05_f3(self) -> Dict[str, Any]:
        """F-3：单个 watchdog 检测器异常不拖垮——回调异常被吞（告警级）；轮询异常任务级隔离。"""
        from myquant.monitoring.watchdog import BrokerConnectionWatcher

        def boom() -> None:
            raise RuntimeError("断连联动回调爆炸（F-3 模拟）")

        bw = BrokerConnectionWatcher(
            broker=_FakeBroker(), alert_manager=self._fake_alert_manager(),
            on_disconnect=boom,
        )
        # 回调抛异常不应把 on_state 拖垮（转换判定仍返回）：先采健康，再断连触发回调
        try:
            bw.on_state(True)
            d = bw.on_state(False)
            callback_isolated = bool(d["transition"] == "connected->disconnected")
        except Exception:
            callback_isolated = False
        # 断连告警路径本身异常也应隔离（_fire_disconnect_alert 内部已有 try-except）
        bw2 = BrokerConnectionWatcher(broker=_FakeBroker(), alert_manager=None)
        try:
            bw2.on_state(True)
            bw2.on_state(False)
            alert_isolated = True
        except Exception:
            alert_isolated = False
        return {
            "item": "0.5-F3",
            "pass": bool(callback_isolated and alert_isolated),
            "detail": f"回调异常隔离={callback_isolated}, 告警异常隔离={alert_isolated}",
            "threshold_source": "V3 §九 F-3（单 watchdog 异常不拖垮巡检）",
        }

    def _check_05_live(self) -> Dict[str, Any]:
        """0.5 启动门（F-1 能力缺失）：LIVE + watchdog 未启用 → 拒启；启用 → 放行；
        paper 尊重配置原值（false 可运行）。"""
        from unittest import mock

        from myquant.live.engine import _assert_live_watchdog_gate

        # live + watchdog disabled → ValueError（拒启）
        with mock.patch(
            "myquant.core.settings.get_monitoring_config",
            return_value={"watchdog": {"enabled": False}},
        ):
            try:
                _assert_live_watchdog_gate("live")
                live_rejected = False
            except ValueError:
                live_rejected = True
        # live + watchdog enabled → 放行
        with mock.patch(
            "myquant.core.settings.get_monitoring_config",
            return_value={"watchdog": {"enabled": True}},
        ):
            try:
                _assert_live_watchdog_gate("live")
                live_allowed = True
            except ValueError:
                live_allowed = False
        # paper + disabled → 放行（尊重配置原值）
        with mock.patch(
            "myquant.core.settings.get_monitoring_config",
            return_value={"watchdog": {"enabled": False}},
        ):
            try:
                _assert_live_watchdog_gate("paper")
                paper_ok = True
            except ValueError:
                paper_ok = False
        return {
            "item": "0.5-LIVE",
            "pass": bool(live_rejected and live_allowed and paper_ok),
            "detail": (
                f"live+disabled拒启={live_rejected}, live+enabled放行={live_allowed}, "
                f"paper+disabled放行={paper_ok}"
            ),
            "threshold_source": "V3 §十.5 F-1（装配能力缺失拒启）；施工清单 §5 改动点2",
        }

    # ── 0.6 对账不一致 → pause（硬，经注入点，禁止反向 import）（用例5 / F-4） ──

    def _check_06_case5(self) -> Dict[str, Any]:
        """用例5：伪造账实差异 100 元 → 对账 CRITICAL + 禁开仓（pause hook 被调）；
        实时链事件触发（MTTD 无轮询延迟）；一致时 hook 不触发。"""
        from myquant.live.portfolio import LivePortfolio

        # 本地账 100_000 vs 券商账 99_900 → 现金差 100 元超阈（容差 LOCKED=0）
        p = LivePortfolio(initial_capital=100_000.0, account_id="acc-0.6")
        p.set_alert_manager(self._fake_alert_manager())
        calls: List[Dict[str, Any]] = []
        p.set_pause_hook(lambda diff: calls.append({"cash_diff": diff.cash_diff}))
        diff = p.reconcile_vs_broker(_FakeBroker(cash=99_900.0))
        hook_fired = bool(diff.exceeds_threshold and calls and calls[0]["cash_diff"] == 100.0)
        # 一致场景：hook 不触发（不误 pause）
        p2 = LivePortfolio(initial_capital=100_000.0, account_id="acc-0.6b")
        p2.set_alert_manager(self._fake_alert_manager())
        calls2: List[Any] = []
        p2.set_pause_hook(lambda d: calls2.append(d))
        d2 = p2.reconcile_vs_broker(_FakeBroker(cash=100_000.0, positions=[]))
        no_false_pause = bool(not d2.exceeds_threshold and not calls2)
        return {
            "item": "0.6-5",
            "pass": bool(hook_fired and no_false_pause),
            "detail": (
                f"差异100元→hook触发(禁开仓)={hook_fired}, "
                f"一致→不误pause={no_false_pause}"
            ),
            "threshold_source": "V3 §九 用例5（伪造账实差异→CRITICAL+禁开仓）；容差真源 LOCKED=0",
        }

    def _check_06_grep(self) -> Dict[str, Any]:
        """grep 复验：portfolio.py 无 engine 反向 import（0.6 禁止反向依赖 engine；
        myquant.risk.guardian 访问器为 G1 EX-12C 已批准合法依赖，不在禁止列）。"""
        import re
        from pathlib import Path

        src = Path("myquant/live/portfolio.py").read_text(encoding="utf-8")
        bad = [
            line for line in src.splitlines()
            if re.search(r"^\s*(from myquant\.live\.engine\b|import myquant\.live\.engine\b)", line)
        ]
        return {
            "item": "0.6-GREP",
            "pass": not bad,
            "detail": f"engine 反向 import 违规={len(bad)}（risk.guardian 访问器 G1 EX-12C 合法）",
            "threshold_source": "施工清单 §6 改动点4（portfolio 不反向依赖 engine）",
        }

    def _check_06_f4(self) -> Dict[str, Any]:
        """F-4：hook 缺失/异常 → 降级 + CRITICAL，不阻断对账主链路（reconcile 仍返回 diff）。"""
        from myquant.live.portfolio import LivePortfolio

        # hook 抛异常：reconcile_vs_broker 不抛、返回 diff（主链路不阻断）
        p = LivePortfolio(initial_capital=100_000.0, account_id="acc-0.6f4")
        p.set_alert_manager(self._fake_alert_manager())

        def boom(_d: Any) -> None:
            raise RuntimeError("pause hook 爆炸（F-4 模拟）")

        p.set_pause_hook(boom)
        try:
            diff = p.reconcile_vs_broker(_FakeBroker(cash=99_900.0))
            hook_exc_ok = bool(diff.exceeds_threshold)
        except Exception:
            hook_exc_ok = False
        # hook 未注入（None）：超阈仍告警不阻断
        p2 = LivePortfolio(initial_capital=100_000.0, account_id="acc-0.6f4b")
        p2.set_alert_manager(self._fake_alert_manager())
        try:
            d2 = p2.reconcile_vs_broker(_FakeBroker(cash=99_900.0))
            hook_none_ok = bool(d2.exceeds_threshold)
        except Exception:
            hook_none_ok = False
        return {
            "item": "0.6-F4",
            "pass": bool(hook_exc_ok and hook_none_ok),
            "detail": f"hook异常→主链路不阻断={hook_exc_ok}, hook未注入→仅告警不阻断={hook_none_ok}",
            "threshold_source": "V3 §九 F-4（hook 注入失败→降级+CRITICAL，不阻断主链路）",
        }

    # ── 0.7 启用三条异常交易规则（用例8 / C2 回测基线） ──

    def _check_07_case8(self) -> Dict[str, Any]:
        """用例8：高撤单率→拒新单；同标的反向未成交→拒同向单；胖手指金额/偏离→拒单；
        三条规则经实盘/回测同源工厂装配且参数取配置真源。"""
        from types import SimpleNamespace

        # 注意：rules 内 OrderSide 源为 myquant.models.trade（跨 enum 比较恒 False，
        # 须与规则实现同源，否则自成交互斥判定失效）
        from myquant.models.trade import OrderSide
        from myquant.risk.risk_manager import build_risk_manager_from_config
        from myquant.risk.rules import RiskContext

        risk_cfg: Dict[str, Any] = {
            "cancel_rate": {"max_rate": 0.5},
            "fat_finger": {"max_order_amount": 5000.0, "max_price_deviation_pct": 0.20},
        }
        rm = build_risk_manager_from_config(
            risk_cfg, {}, enable_trading_hours=False,
        )
        names = set(rm.rule_names)
        rules_enabled = {"self_trade", "cancel_rate", "fat_finger"} <= names
        # 参数真源核验
        params_ok = (
            rm.get_rule("cancel_rate").max_cancel_rate == 0.5
            and rm.get_rule("fat_finger").max_order_amount == 5000.0
        )

        ctx = RiskContext(
            cash=1_000_000.0, total_assets=1_000_000.0, initial_capital=1_000_000.0,
            current_price={"000001.SZ": 10.0},
            extra={"prev_close": {"000001.SZ": 10.0}},
        )

        def _buy(price: float = 10.0, qty: int = 100) -> Any:
            return SimpleNamespace(
                symbol="000001.SZ", side=OrderSide.BUY, price=price, quantity=qty,
            )

        # 撤单率：撤 5 次后下一单 → 撤单率 100% > 50% → 拒（rule=cancel_rate）
        rm_c = build_risk_manager_from_config(risk_cfg, {}, enable_trading_hours=False)
        for _ in range(5):
            rm_c.record_cancel_order(_buy())
        r1 = rm_c.check_order(_buy(), ctx)
        cancel_rejected = bool(not r1.passed and r1.rule_name == "cancel_rate")

        # 自成交：同标的 SELL 未成交挂单 → 拒 BUY 同向（rule=self_trade）
        rm_s = build_risk_manager_from_config(risk_cfg, {}, enable_trading_hours=False)
        rm_s.set_pending_orders([
            SimpleNamespace(symbol="000001.SZ", side=OrderSide.SELL, order_id="o1")
        ])
        r2 = rm_s.check_order(_buy(), ctx)
        self_trade_rejected = bool(not r2.passed and r2.rule_name == "self_trade")

        # 胖手指：100 股 × 100 元 = 10000 > max_order_amount 5000 → 拒（rule=fat_finger）
        rm_f = build_risk_manager_from_config(risk_cfg, {}, enable_trading_hours=False)
        r3 = rm_f.check_order(_buy(price=100.0, qty=100), ctx)
        fat_rejected = bool(not r3.passed and r3.rule_name == "fat_finger")

        return {
            "item": "0.7-8",
            "pass": bool(
                rules_enabled and params_ok
                and cancel_rejected and self_trade_rejected and fat_rejected
            ),
            "detail": (
                f"三条已装配={rules_enabled}, 参数真源={params_ok}, "
                f"撤单率拒={cancel_rejected}, 自成交拒={self_trade_rejected}, "
                f"胖手指拒={fat_rejected}"
            ),
            "threshold_source": "V3 §九 用例8；参数真源 risk_cfg（cancel_rate/fat_finger），缺省类默认",
        }

    def _check_07_c2(self) -> Dict[str, Any]:
        """C2：回测复用同一工厂（行为变化）——三条规则前后规则库差异显式记录；
        引擎回测（after，含三条）跑通并落指标；关闭三条经工厂参数显式控制（不静默）。"""
        from myquant.risk.risk_manager import build_risk_manager_from_config

        risk_cfg: Dict[str, Any] = {
            "cancel_rate": {"max_rate": 0.5},
            "fat_finger": {"max_order_amount": 1_000_000.0},
        }
        # before 基线：anomaly_rules=[] 显式关闭三条（C2 工厂参数控制）
        rm_before = build_risk_manager_from_config(
            risk_cfg, {}, enable_trading_hours=False, anomaly_rules=[],
        )
        # after：默认启用三条
        rm_after = build_risk_manager_from_config(
            risk_cfg, {}, enable_trading_hours=False,
        )
        before_names = set(rm_before.rule_names)
        after_names = set(rm_after.rule_names)
        added = sorted(after_names - before_names)
        baseline_recorded = bool(
            added == ["cancel_rate", "fat_finger", "self_trade"]
            and len(after_names) == len(before_names) + 3
        )
        # 引擎回测（after 路径）跑通合成行情并取指标（真实引擎 + 真工厂）。
        # 测试/校准场景对齐 tests/conftest.py 语义：门禁降审计不阻断，跑完恢复。
        from myquant.leakage_gate import GateConfig as _GC
        from myquant.leakage_gate import get_config as _lg_get
        from myquant.leakage_gate import set_config as _lg_set

        _prev = _lg_get()
        _lg_set(_GC(enabled=False, audit_violations=_prev.audit_violations))
        try:
            backtest_ok, bt_metrics = self._run_small_backtest()
        finally:
            _lg_set(_prev)

        return {
            "item": "0.7-C2",
            "pass": bool(baseline_recorded and backtest_ok),
            "detail": (
                f"基线差异(11→14, +{len(added)})={baseline_recorded}, "
                f"新增={added}, 引擎回测(含三条)成功={backtest_ok}, "
                f"指标={bt_metrics}"
            ),
            "threshold_source": "施工清单 §7 C2（回测基线重跑显式记录；工厂参数显式控制不静默）",
        }

    def _run_small_backtest(self) -> tuple:
        """最小合成行情回测（引擎内部同源工厂含三条规则）；返回 (成功, 指标摘要)。"""
        try:
            import numpy as np
            import pandas as pd

            from myquant.backtest.engine import (
                register_broker_factory,
                run_backtest,
            )
            from myquant.execution.sim_broker import SimBroker
            from myquant.models.backtest import BacktestConfig
            from myquant.strategies.base import (
                Bar,
                Signal,
                SignalType,
                StrategyBase,
                StrategyContext,
            )

            # G1 EX-09A：装配撮合执行器工厂（scripts/验收_回测引擎V5.py 同源方式）
            register_broker_factory(
                lambda cfg: SimBroker(
                    initial_capital=cfg.initial_capital,
                    commission_model=cfg.commission,
                    slippage_model=cfg.slippage,
                    price_limit_pct=float(cfg.price_limit_pct),
                    enforce_limit=bool(cfg.enforce_limit),
                    is_suspended=None,
                )
            )

            n = 30
            idx = pd.date_range("2024-01-01", periods=n, freq="B")
            close = 10.0 * 1.001 ** np.arange(n)
            df = pd.DataFrame(
                {"开盘价": close * 0.999, "最高价": close * 1.01,
                 "最低价": close * 0.99, "收盘价": close, "成交量": np.full(n, 1e6)},
                index=idx,
            )
            data = {"600519.SH": df}
            config = BacktestConfig(
                symbols=["600519.SH"], start_date="2024-01-01",
                end_date="2024-02-29", initial_capital=1_000_000.0,
            )

            class _HoldStrategy(StrategyBase):
                name = "c2_hold"

                def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
                    return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD)

            result = run_backtest(config, _HoldStrategy(), data=data, mode="auto")
            if result is None or not getattr(result, "is_success", False):
                return False, {}
            metrics = getattr(result, "metrics", None) or {}
            summary = {
                "total_return": float(getattr(metrics, "total_return", 0.0)),
                "trade_count": int(getattr(metrics, "trade_count", 0)),
                "max_drawdown": float(getattr(metrics, "max_drawdown", 0.0)),
            }
            return True, summary
        except Exception as e:  # noqa: BLE001
            return False, {"error": f"{type(e).__name__}: {e}"}

    # ── 0.9 broker 提交边界硬闸（用例12 / R-7） ──

    def _check_09_case12(self) -> Dict[str, Any]:
        """用例12：绕过引擎直调 broker.submit_order 的 BUY → kill_switch / risk_paused /
        非交易时段下被硬闸拒绝（REJECTED + gate 标记）；SELL 放行（即时）。"""
        from myquant.live.brokers.paper_broker import PaperBroker
        from myquant.models.trade import Order, OrderSide, OrderStatus, Position

        def _buy() -> Order:
            return Order(symbol="000001.SZ", side=OrderSide.BUY, quantity=100, price=10.0)

        def _sell() -> Order:
            return Order(symbol="000001.SZ", side=OrderSide.SELL, quantity=100, price=10.0)

        def _new_broker() -> PaperBroker:
            b = PaperBroker(initial_capital=1_000_000.0)
            b.positions["000001.SZ"] = Position(
                symbol="000001.SZ", quantity=100, avg_cost=9.0, market_price=10.0,
            )
            b.connect()
            return b

        # ── kill_switch（标志文件真源）──
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "kill_switch.flag"
            b1 = _new_broker()
            b1.set_kill_switch_flag_path(str(flag))
            # 无标志：BUY 正常放行（不误伤）
            normal_ok = b1.submit_order(_buy()).status != OrderStatus.REJECTED
            # 置位标志：BUY 拒
            flag.write_text("1", encoding="utf-8")
            r1 = b1.submit_order(_buy())
            ks_rejected = bool(
                r1.status == OrderStatus.REJECTED
                and r1.extra.get("gate") == "broker_submission_gate"
            )
            # SELL 放行（kill_switch 下仍可提交）
            r_sell1 = b1.submit_order(_sell())
            ks_sell_ok = r_sell1.extra.get("gate") != "broker_submission_gate"

        # ── risk_paused（注入判定）──
        b2 = _new_broker()
        b2.add_risk_pause_checker(lambda: True)
        r2 = b2.submit_order(_buy())
        rp_rejected = bool(
            r2.status == OrderStatus.REJECTED
            and r2.extra.get("gate") == "broker_submission_gate"
        )
        rp_sell_ok = b2.submit_order(_sell()).extra.get("gate") != "broker_submission_gate"

        # ── 非交易时段（注入判定）──
        b3 = _new_broker()
        b3.set_trading_hours_checker(lambda: False)
        r3 = b3.submit_order(_buy())
        th_rejected = bool(
            r3.status == OrderStatus.REJECTED
            and r3.extra.get("gate") == "broker_submission_gate"
        )
        th_sell_ok = b3.submit_order(_sell()).extra.get("gate") != "broker_submission_gate"

        return {
            "item": "0.9-12",
            "pass": bool(
                normal_ok and ks_rejected and ks_sell_ok
                and rp_rejected and rp_sell_ok
                and th_rejected and th_sell_ok
            ),
            "detail": (
                f"无闸BUY放行={normal_ok}, kill_switch拒BUY={ks_rejected}, "
                f"risk_paused拒BUY={rp_rejected}, 非交易时段拒BUY={th_rejected}, "
                f"三场景SELL均放行={ks_sell_ok and rp_sell_ok and th_sell_ok}"
            ),
            "threshold_source": "V3 §九 用例12（绕过引擎直调 broker 也被拦）；kill_switch 标志文件与阶段4 同真源",
        }

    def _check_09_r7(self) -> Dict[str, Any]:
        """R-7：0.9 硬闸置位 → SELL / 平仓 / 应急卖单仍能提交（硬闸只拒 BUY）。"""
        from myquant.live.brokers.paper_broker import PaperBroker
        from myquant.models.trade import Order, OrderSide, OrderStatus, Position

        b = PaperBroker(initial_capital=1_000_000.0)
        b.positions["000001.SZ"] = Position(
            symbol="000001.SZ", quantity=100, avg_cost=9.0, market_price=10.0,
        )
        b.connect()
        # 全部闸条件同时置位（kill_switch + risk_paused + 非交易时段）
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "kill_switch.flag"
            flag.write_text("1", encoding="utf-8")
            b.set_kill_switch_flag_path(str(flag))
            b.add_risk_pause_checker(lambda: True)
            b.set_trading_hours_checker(lambda: False)

            sell = b.submit_order(
                Order(symbol="000001.SZ", side=OrderSide.SELL, quantity=100, price=10.0)
            )
            sell_ok = sell.extra.get("gate") != "broker_submission_gate"
            # 平仓/应急卖单：SELL 全量 = 平仓语义
            emergency = b.submit_order(
                Order(symbol="000001.SZ", side=OrderSide.SELL, quantity=50, price=10.0,
                      tag="应急平仓")
            )
            emergency_ok = emergency.extra.get("gate") != "broker_submission_gate"
            # 对照：BUY 在全部置位下被拒（硬闸只拒 BUY）
            buy = b.submit_order(
                Order(symbol="000001.SZ", side=OrderSide.BUY, quantity=100, price=10.0)
            )
            buy_rejected = bool(
                buy.status == OrderStatus.REJECTED
                and buy.extra.get("gate") == "broker_submission_gate"
            )
        return {
            "item": "0.9-R7",
            "pass": bool(sell_ok and emergency_ok and buy_rejected),
            "detail": f"SELL放行={sell_ok}, 应急卖单放行={emergency_ok}, BUY拒={buy_rejected}",
            "threshold_source": "V3 §九 R-7（硬闸只拒 BUY；SELL/平仓/应急卖单永远放行 I-1/I-3）",
        }

    # ── 0.8 启动自检断言（用例4 / R-5 / F-1 / F-2 / F-5） ──

    def _run_08_problems(self, engine: Any, monkeypatch: Any, **monkey) -> list:
        """在受控环境跑 startup_assertions（monkey 安装后返回问题列表）。"""
        from myquant.live.engine import startup_assertions
        from myquant.live.run_mode import RunMode

        return list(startup_assertions(engine, {}, RunMode.LIVE))

    def _check_08_case4(self) -> Dict[str, Any]:
        """用例4：清空 guard 装配 → 自检问题列表含守卫项（LIVE 拒启语义）。"""
        from types import SimpleNamespace

        from myquant.risk.guardian import get_operation_guard, register_operation_guard

        old = get_operation_guard()
        try:
            register_operation_guard(None)
            engine_like = SimpleNamespace(
                _circuit_breaker=object(), _trade_mode="live", _scheduler_enabled=True,
            )
            problems = self._run_08_problems(engine_like, None)
            guard_problem = any("准入守卫未注册" in p for p in problems)
        finally:
            register_operation_guard(old)
        return {
            "item": "0.8-4",
            "pass": bool(guard_problem),
            "detail": f"清空 guard 后自检问题含守卫项={guard_problem}（问题数={len(problems)}）",
            "threshold_source": "V3 §九 用例4（清空 guard 装配 → 拒启，不是警告放行）；0.8 施工清单 §8",
        }

    def _check_08_f1(self) -> Dict[str, Any]:
        """F-1：守卫配置加载失败 → 自检问题含 F-1 项（能力缺失拒启）。"""
        from types import SimpleNamespace

        from myquant.risk.guardian import get_operation_guard, register_operation_guard

        old = get_operation_guard()
        try:
            register_operation_guard(SimpleNamespace(load_error="测试配置损坏", enabled=True))
            engine_like = SimpleNamespace(
                _circuit_breaker=object(), _trade_mode="live", _scheduler_enabled=True,
            )
            problems = self._run_08_problems(engine_like, None)
            f1_problem = any("F-1" in p and "加载失败" in p for p in problems)
        finally:
            register_operation_guard(old)
        return {
            "item": "0.8-F1",
            "pass": bool(f1_problem),
            "detail": f"守卫 load_error 非空 → 自检问题含 F-1 拒启项={f1_problem}",
            "threshold_source": "V3 §十.5 F-1（配置加载失败=能力缺失拒启）；0.4 装配层同一判定",
        }

    def _check_08_f2(self) -> Dict[str, Any]:
        """F-2：告警渠道未配置/不可用 → CRITICAL 降级，不产生拒启项。"""
        import logging
        from types import SimpleNamespace

        import pytest

        def _no_channels():
            return SimpleNamespace(channels=[])

        engine_like = SimpleNamespace(
            _circuit_breaker=object(), _trade_mode="live", _scheduler_enabled=True,
        )
        records: list = []
        handler = _CaptureHandler(records)
        logging.getLogger("live.engine").addHandler(handler)
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("myquant.monitoring.alerting.get_alert_manager", _no_channels)
                problems = self._run_08_problems(engine_like, mp)
        finally:
            logging.getLogger("live.engine").removeHandler(handler)
        alert_problem = any("告警" in p for p in problems)
        degraded_logged = any("告警渠道未配置" in r.getMessage() for r in records)
        return {
            "item": "0.8-F2",
            "pass": bool(not alert_problem and degraded_logged),
            "detail": (
                f"告警渠道为空 → 拒启项={alert_problem}（应无）, CRITICAL 降级日志={degraded_logged}"
            ),
            "threshold_source": "V3 §十.5 F-2（外部依赖不可用→降级+CRITICAL，不拒启）",
        }

    def _check_08_f5(self) -> Dict[str, Any]:
        """F-5：系统时钟不可达 → 拒绝 live 启动（V3 §九 9.3），paper 不受影响。"""
        from types import SimpleNamespace

        import pytest

        class _BrokenClock:
            @staticmethod
            def now():
                raise RuntimeError("时钟损坏")

        engine_like = SimpleNamespace(
            _circuit_breaker=object(), _trade_mode="live", _scheduler_enabled=True,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("myquant.live.engine.datetime", _BrokenClock)
            problems = self._run_08_problems(engine_like, mp)
        clock_problem = any("系统时钟不可用" in p and "F-5" in p for p in problems)
        return {
            "item": "0.8-F5",
            "pass": bool(clock_problem),
            "detail": (
                f"时钟不可达 → 拒启项={clock_problem}（V3 9.3：拒绝 live 启动，允许 paper；"
                f"不得默认放行）"
            ),
            "threshold_source": "V3 §九 9.3 F-5（时钟源不可达 → 拒绝 live 启动，允许 paper，不可默认放行）",
        }

    def _check_08_r5(self) -> Dict[str, Any]:
        """R-5：自检失败不拦应急链——自检只返回问题列表，不执行/依赖平仓；
        一键平仓脚本与 broker_factory 应急链仍存在可独立执行。"""
        from types import SimpleNamespace

        engine_like = SimpleNamespace(
            _circuit_breaker=object(), _trade_mode="live", _scheduler_enabled=True,
        )
        problems = self._run_08_problems(engine_like, None)
        # 自检返回列表（不抛异常、不执行平仓）；应急脚本文件存在
        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "一键平仓.py"
        script_ok = script.exists()
        # 应急链模块可导入（0.3 产物）
        from myquant.live.broker_factory import build_broker_from_config  # noqa: F401

        return {
            "item": "0.8-R5",
            "pass": bool(script_ok),
            "detail": (
                f"自检问题数={len(problems)}（仅报告不执行平仓）, 一键平仓脚本存在={script_ok}, "
                f"broker_factory 可导入=True"
            ),
            "threshold_source": "V3 §九 R-5（自检失败不拦应急链，一键平仓独立可用）",
        }

    # ── LIVE 门槛验收（用例 1/3/6/7/13 + R-1~R-3 + F-6，施工清单 §10） ──

    def _check_live_1(self) -> Dict[str, Any]:
        """用例1：杀引擎进程 → 状态快照可恢复（恢复机制存在 + StateStore 装配；
        执行级证据=既有单测 test_restore_broker_state / test_t1_settle 崩溃恢复域）。"""
        from myquant.live.engine import LiveEngine

        has_recover = any(
            name for name in ("_recover_state", "restore_state", "_restore_state")
            if hasattr(LiveEngine, name)
        )
        try:
            from myquant.live.state_store import StateStore  # noqa: F401
            store_ok = True
        except Exception:  # noqa: BLE001
            store_ok = False
        return {
            "item": "LIVE-1",
            "pass": bool(has_recover and store_ok),
            "detail": (
                f"引擎恢复方法存在={has_recover}, StateStore 可导入={store_ok}"
                "（执行级恢复/对账证据见 tests/unit/test_restore_broker_state.py 等）"
            ),
            "threshold_source": "V3 §九 用例1（杀引擎→快照可恢复+未成交单对账）；恢复时限引用 P0-2",
        }

    def _check_live_3(self) -> Dict[str, Any]:
        """用例3：max_daily_loss_pct 已突破 → 风控拒开仓（BUY 拒、SELL 平仓放行）。"""
        from myquant.models.trade import Order, OrderSide
        from myquant.risk.risk_manager import build_risk_manager_from_config
        from myquant.risk.rules import RiskContext

        rm = build_risk_manager_from_config(
            {"max_daily_loss_pct": 0.01}, {}, enable_trading_hours=False,
        )
        ctx = RiskContext(
            cash=1_000_000.0, total_assets=950_000.0, initial_capital=1_000_000.0,
            day_start_assets=1_000_000.0,  # 日初快照 100 万 → 当前 95 万 = 已亏 5% > 1% 上限
            current_price={"000001.SZ": 10.0},
            extra={"prev_close": {"000001.SZ": 10.0}},
        )
        buy = Order(symbol="000001.SZ", side=OrderSide.BUY, quantity=100, price=10.0)
        sell = Order(symbol="000001.SZ", side=OrderSide.SELL, quantity=100, price=10.0)
        r_buy = rm.check_order(buy, ctx)
        r_sell = rm.check_order(sell, ctx)
        buy_rejected = bool(not r_buy.passed and r_buy.rule_name == "daily_loss_limit")
        sell_allowed = bool(r_sell.passed)
        return {
            "item": "LIVE-3",
            "pass": bool(buy_rejected and sell_allowed),
            "detail": f"日亏突破→BUY拒={buy_rejected}（rule=daily_loss_limit）, SELL放行={sell_allowed}",
            "threshold_source": "V3 §九 用例3（手动改 max_daily_loss_pct 到已突破→风控拒绝开仓，不是警告）",
        }

    def _check_live_6(self) -> Dict[str, Any]:
        """用例6：系统时间被篡改（实际 20:00 判为交易时段外）→ 拒开仓（TradingHoursRule 同源）。"""
        from datetime import datetime

        from myquant.models.trade import Order, OrderSide
        from myquant.risk.rules import RiskContext, TradingHoursRule

        rule = TradingHoursRule(trading_calendar={datetime.now().date()})  # 注入日历使今天为交易日（隔离真实周六）
        ctx = RiskContext(
            cash=1_000_000.0, total_assets=1_000_000.0, initial_capital=1_000_000.0,
            current_price={"000001.SZ": 10.0},
            extra={"current_time": "20:00:00"},  # 篡改后的系统时间（实际非交易时段）
        )
        buy = Order(symbol="000001.SZ", side=OrderSide.BUY, quantity=100, price=10.0)
        r = rule.check(buy, ctx)
        rejected = bool(not r.passed and r.rule_name == "trading_hours")
        # 时段内（09:35）放行对照
        ctx2 = RiskContext(
            cash=1_000_000.0, total_assets=1_000_000.0, initial_capital=1_000_000.0,
            current_price={"000001.SZ": 10.0},
            extra={"current_time": "09:35:00"},
        )
        allowed = bool(rule.check(buy, ctx2).passed)
        return {
            "item": "LIVE-6",
            "pass": bool(rejected and allowed),
            "detail": f"时间篡改为20:00→拒开仓={rejected}, 09:35对照放行={allowed}",
            "threshold_source": "V3 §九 用例6（把系统时间改到 09:00（实际 20:00）→ 拒启或拒单）；0.9-12 非交易时段同源",
        }

    def _check_live_7(self) -> Dict[str, Any]:
        """用例7：同一 signal_id（同 order_id）三次 → 仅 1 笔委托 + 2 次 dedup 留痕。"""
        from myquant.live.brokers.qmt_broker import QmtBroker
        from myquant.models.trade import Order, OrderSide

        broker = QmtBroker(qmt_path="", mode="SIM", initial_capital=1_000_000.0)
        broker.connect()
        o = Order(symbol="000001.SZ", side=OrderSide.BUY, quantity=100, price=10.0)
        broker.submit_order(o)
        broker.submit_order(o)  # 同 order_id 重发
        broker.submit_order(o)
        dedup = int(broker._stats.get("dedup_skipped", 0))
        one_order = len(broker.get_orders()) == 1
        return {
            "item": "LIVE-7",
            "pass": bool(one_order and dedup == 2),
            "detail": f"同 signal_id 三发→订单簿仅1笔={one_order}, dedup 留痕={dedup}次",
            "threshold_source": "V3 §九 用例7（同一 signal_id 三次→仅 1 笔委托 + dedup 留痕）；qmt 幂等去重（任务43）",
        }

    def _check_live_13(self) -> Dict[str, Any]:
        """用例13：注入 CRITICAL → webhook 实测送达（本地 HTTP 端口真实 POST；MTTA 引用 P0-8 5min）。"""
        import http.server
        import threading
        import time

        received: list = []

        class _HookHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                received.append(self.rfile.read(length))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return

        srv = http.server.HTTPServer(("127.0.0.1", 0), _HookHandler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            from myquant.monitoring.alerting import (
                Alert,
                SeverityLevel,
                WebhookAlertChannel,
            )

            ch = WebhookAlertChannel(
                webhook_url=f"http://127.0.0.1:{port}/hook", retries=1, timeout=3.0,
            )
            ch.send(Alert(
                level=SeverityLevel.CRITICAL,
                message="[门禁验收] 用例13 告警送达测试（LIVE 门槛）",
                source="live_safety_gate",
            ))
            deadline = time.time() + 15
            while time.time() < deadline and not received:
                time.sleep(0.2)
            delivered = bool(received)
        finally:
            srv.shutdown()
            thread.join(timeout=2)
        return {
            "item": "LIVE-13",
            "pass": bool(delivered),
            "detail": f"CRITICAL→webhook 本地实测送达={delivered}（MTTA 阈值引用 P0-8 5min）",
            "threshold_source": "V3 §九 用例13（注入 CRITICAL→email/webhook 实测送达）；P0-8 MTTA≤5min",
        }

    def _check_live_f6(self) -> Dict[str, Any]:
        """F-6：审计存储不可用 → 门禁判定照常 + 本地降级缓冲 + 恢复补写 + CRITICAL（不阻断不静默）。"""
        import logging
        import tempfile

        from myquant.api.middleware.auth import OperationGuard

        tmp = Path(tempfile.mkdtemp())
        blocker = tmp / "blocker.txt"
        blocker.write_text("x", encoding="utf-8")  # 占用路径使 mkdir 失败 → 落盘失败
        guard = OperationGuard()
        guard._audit_dir = str(blocker)
        records: list = []
        handler = _CaptureHandler(records)
        logging.getLogger("api.middleware.auth").addHandler(handler)
        try:
            guard.audit("test", "op", "F-6故障注入", "denied", None, "审计存储不可用")
            buffered = len(guard._audit_buffer) == 1
            critical_logged = any("F-6" in r.getMessage() for r in records)
            # 恢复：audit_dir 改回可写 → 下一条 audit 触发补写
            guard._audit_dir = str(tmp)
            guard.audit("test", "op", "F-6恢复", "denied", None, "存储恢复")
            flushed = len(guard._audit_buffer) == 0
            recovered = (tmp / "write_audit.jsonl").exists()
        finally:
            logging.getLogger("api.middleware.auth").removeHandler(handler)
        return {
            "item": "LIVE-F6",
            "pass": bool(buffered and critical_logged and flushed and recovered),
            "detail": (
                f"故障时缓冲={buffered}, CRITICAL留痕={critical_logged}, "
                f"恢复后补写={flushed}, 审计文件生成={recovered}"
            ),
            "threshold_source": "V3 §九 F-6（审计存储不可用→判定照常+本地降级缓冲+补写+CRITICAL；不得阻断交易不得静默）",
        }

    def _check_live_r123(self) -> Dict[str, Any]:
        """R-1/R-2/R-3：止损卖单不被 rate_limit/daily_loss 拦、黑名单可卖出、
        kill_switch 置位下仍可撤单（硬闸只禁开仓，不禁止退出/撤单）。"""
        from myquant.live.brokers.paper_broker import PaperBroker
        from myquant.models.trade import Order, OrderSide, Position
        from myquant.risk.risk_manager import build_risk_manager_from_config
        from myquant.risk.rules import RiskContext

        # R-1：rate_limit 达上限 + daily_loss 突破 → SELL 止损卖单仍通过（只拦 BUY）
        rm = build_risk_manager_from_config(
            {"max_daily_loss_pct": 0.01, "max_orders_per_day": 5}, {},
            enable_trading_hours=False,
        )
        ctx = RiskContext(
            cash=1_000_000.0, total_assets=950_000.0, initial_capital=1_000_000.0,
            day_start_assets=1_000_000.0,  # 日亏 5% > 1% 上限
            current_price={"000001.SZ": 10.0},
            extra={"prev_close": {"000001.SZ": 10.0}},
            order_count_today=5,
        )
        sell = Order(symbol="000001.SZ", side=OrderSide.SELL, quantity=100, price=10.0)
        r1_ok = bool(rm.check_order(sell, ctx).passed)
        # R-2：黑名单标的仍可卖出
        rm2 = build_risk_manager_from_config(
            {"blacklist": {"symbols": ["000001.SZ"]}}, {}, enable_trading_hours=False,
        )
        r2_ok = bool(rm2.check_order(sell, ctx).passed)
        # R-3：kill_switch 置位 → 撤单仍可执行（cancel_order 不经提交薄闸）
        import tempfile

        b = PaperBroker(initial_capital=1_000_000.0)
        b.connect()
        b.positions["000001.SZ"] = Position(
            symbol="000001.SZ", quantity=100, avg_cost=9.0, market_price=10.0,
        )
        placed = b.submit_order(
            Order(symbol="000001.SZ", side=OrderSide.SELL, quantity=100, price=10.0)
        )
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "kill_switch.flag"
            flag.write_text("1", encoding="utf-8")
            b.set_kill_switch_flag_path(str(flag))
            r3_ok = b.cancel_order(placed.order_id)
        return {
            "item": "LIVE-R123",
            "pass": bool(r1_ok and r2_ok and r3_ok),
            "detail": (
                f"止损卖单过 rate_limit/daily_loss={r1_ok}, 黑名单可卖={r2_ok}, "
                f"kill_switch 下撤单可用={r3_ok}"
            ),
            "threshold_source": "V3 §九 R-1（止损卖单必须成交，前置可行价格）/R-2（黑名单可卖）/R-3（Kill Switch 仍可撤单可卖）",
        }

    # ── 运行入口 ────────────────────────────────────────────

    def run(self, mode: str = "full") -> Dict[str, Any]:
        """运行实盘安全门禁第0阶段验收。

        Parameters
        ----------
        mode : str
            档位；当前支持 "full"（全部已登记验收项）。--select 前缀过滤
            由薄入口在 items 层完成，本方法始终跑全量注册表。

        Returns
        -------
        dict : 验收报告
            {
              "mode": str,
              "items": [{"item", "pass", "detail", "threshold_source"}, ...],
              "summary": {"passed", "failed", "total"},
              "generated_at": str,
              "caveats": [str, ...],
            }

        Raises
        ------
        ValueError : mode 非法时抛出。
        """
        if mode != "full":
            raise ValueError(f"mode 必须为 full，实际 {mode!r}（当前仅全量档）")

        self._reset_runtime()

        check_dispatch: Dict[str, Callable[[], Dict[str, Any]]] = {
            "0.1-9": self._check_01_engine_isolation,
            "0.1-10": self._check_01_circuit_chain,
            "0.1-F2": self._check_01_f2,
            "0.2-10": self._check_02_sync_pause,
            "0.2-R4": self._check_02_r4,
            "0.3-R5": self._check_03_r5,
            "0.4-4": self._check_04_case4,
            "0.4-R6": self._check_04_r6,
            "0.4-F1": self._check_04_f1,
            "0.5-2": self._check_05_case2,
            "0.5-F3": self._check_05_f3,
            "0.5-LIVE": self._check_05_live,
            "0.6-5": self._check_06_case5,
            "0.6-GREP": self._check_06_grep,
            "0.6-F4": self._check_06_f4,
            "0.7-8": self._check_07_case8,
            "0.7-C2": self._check_07_c2,
            "0.9-12": self._check_09_case12,
            "0.9-R7": self._check_09_r7,
            "0.8-4": self._check_08_case4,
            "0.8-R5": self._check_08_r5,
            "0.8-F1": self._check_08_f1,
            "0.8-F2": self._check_08_f2,
            "0.8-F5": self._check_08_f5,
            "LIVE-1": self._check_live_1,
            "LIVE-3": self._check_live_3,
            "LIVE-6": self._check_live_6,
            "LIVE-7": self._check_live_7,
            "LIVE-13": self._check_live_13,
            "LIVE-F6": self._check_live_f6,
            "LIVE-R123": self._check_live_r123,
        }

        items: List[Dict[str, Any]] = []
        caveats: List[str] = []

        for item_id in GATE_ITEMS:
            fn = check_dispatch.get(item_id)
            if fn is None:
                items.append({
                    "item": item_id, "pass": False,
                    "detail": f"未知检查项 {item_id}",
                    "threshold_source": "N/A",
                })
                continue
            try:
                items.append(fn())
            except Exception as exc:  # 异常不吞：如实记录该项 failed
                logger.exception("[live_safety_gate] %s 检查异常", item_id)
                items.append({
                    "item": item_id,
                    "pass": False,
                    "detail": f"检查异常: {type(exc).__name__}: {exc}",
                    "threshold_source": "N/A",
                })

        passed: int = sum(1 for it in items if it.get("pass"))
        failed: int = len(items) - passed
        total: int = len(items)

        return {
            "mode": mode,
            "items": items,
            "summary": {"passed": passed, "failed": failed, "total": total},
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "caveats": caveats,
        }

    def write_report(self, report: Dict[str, Any], out_path: str) -> None:
        """验收报告落盘 JSON。

        Args:
            report: run() 返回的验收报告。
            out_path: JSON 输出路径（父目录不存在则创建）。
        """
        import json

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════
#  自测（__main__ 可运行 demo）
# ═══════════════════════════════════════════════


def _run_self_test() -> int:
    """运行模块自测（0.1 三项验收 + 报告结构可序列化）。"""
    import json

    runner = LiveSafetyGateRunner()
    try:
        report = runner.run(mode="full")
    finally:
        runner.close()

    check = logging.getLogger("live_safety_gate.self_test")
    ok = True
    if report["summary"]["failed"] != 0:
        ok = False
    try:
        json.dumps(report)
    except TypeError:
        ok = False
    check.info("自测结果: %s", json.dumps(report, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)-5s %(message)s")
    raise SystemExit(_run_self_test())
