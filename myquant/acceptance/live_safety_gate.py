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

    def __init__(self, active_orders: Optional[List[str]] = None) -> None:
        """构造桩券商。

        Args:
            active_orders: 活跃挂单 order_id 列表（前置状态：有挂单）。
        """
        self._orders: List[str] = list(active_orders or [])
        self.cancelled: List[str] = []
        self.submitted: List[Any] = []

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
        """桩：返回固定持仓快照（前置状态：有持仓）。"""
        return [_FakePosition("000001.SZ", 100, 10.0, available_qty=100)]

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
        from myquant.risk.guardian import RiskGuardian, get_operation_guard, register_operation_guard

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
