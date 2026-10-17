# 文件名：myquant/live/engine_assembly.py
"""
engine_assembly.py - 实盘装配层（拆包瘦身 PR2 · 回写#422）
=================================================================
从 live/engine.py 迁出的"装配层"：配置 → 完整实盘引擎链路的组装代码。

核心能力:
  1. create_live_engine — H2 一键装配工厂（feed→broker→portfolio→strategy→
     RiskManager→StateStore→LookaheadGuard→CircuitBreaker→LiveEngine 全链路）
  2. startup_assertions — 0.8 启动自检断言（能力缺失拒启 / 外部依赖降级）
  3. _assert_live_watchdog_gate — 0.5 LIVE 看门狗启动门（fail-closed）
  4. _write_live_leakage_report — P1-11 实盘出口泄漏审计报告落盘
  5. _require_auto_recover — KP-P5-08 auto_recover_on_start 显式必填

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md，live/ 段已登记回写#422）
  2. 为实时行情及实盘无缝对接做好充足准备（统一装配入口，回测/实盘共用策略注册器）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 策略参数来自配置，行情由 data_feed 已发生bar驱动，
     信号只使用当前及历史数据；恢复状态只回写已发生事实

防未来函数说明:
  - 装配决策只依据当前配置与已发生事实（无预测、无未来数据引用）
  - 熔断/看门狗/审计报告均以"已发生"状态为准（P1-11 决策时点=装配当天）
  - LiveEngine/RiskConfig 函数内延迟导入（from myquant.live.engine）——
    打破 engine↔assembly 模块级循环导入环（拆包#422）

依赖: datetime, pathlib, typing,
       myquant.core.logger, myquant.live.broker_factory（顶部）
       其余依赖均为函数内延迟导入（与拆出前原代码一致，最小行为变化）

"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.live.broker_factory import build_broker_from_config

if TYPE_CHECKING:
    # 类型注解专用（运行时不可达，不构成 engine↔assembly 运行时环；拆包#422）
    from myquant.live.engine import LiveEngine

logger = get_logger("live.engine_assembly")


# ═══════════════════════════════════════════════
#  H2修复：实盘一键装配工厂（配置 → 完整实盘链路）
# ═══════════════════════════════════════════════
#
# 拆包#422：装配层自 engine.py 迁出（原版逐字搬移 + import 调整，零行为修改）。
# 调用点直连本模块（run_实盘入口.py / live_safety_gate.py / 测试），engine.py
# 不再反向引用（R-01b 真环基线 0）；LiveEngine/RiskConfig 函数内延迟导入 engine。



def _require_auto_recover(state_cfg: Dict[str, Any]) -> bool:
    """KP-P5-08: auto_recover_on_start 显式必填（杜绝静默默认跳过崩溃恢复）。"""
    if "auto_recover_on_start" not in state_cfg:
        raise ValueError(
            "state_store.auto_recover_on_start 必须显式配置（KP-P5-08 崩溃恢复显式必填）"
        )
    return bool(state_cfg.get("auto_recover_on_start"))


def create_live_engine(
    config: Dict[str, Any],
    engine_id: Optional[str] = None,
    monitor_only: bool = False,
) -> LiveEngine:
    """
    从实盘配置一键装配实盘引擎（H2修复核心工厂）

    配置结构遵循 config/live_config.example.yaml:
      live / broker / execution / market_data / strategy / position /
      risk_control / lookahead_guard / state_store / scheduler

    装配链路（对齐蓝图 live/ 模块职责）:
      MarketDataFeed → 券商适配器 → LivePortfolio → 策略注册器 →
      LiveEngine(RiskConfig) → RiskManager → StateStore → LiveScheduler

    防未来函数: 策略参数来自配置，行情由 data_feed 已发生bar驱动，
    信号只使用当前及历史数据；恢复状态只回写已发生事实。

    Parameters
    ----------
    config : Dict
        完整实盘配置（ConfigLoader.load 结果）
    engine_id : str | None
        引擎ID（默认取 live.name）
    monitor_only : bool
        监控模式：生成信号不提交订单

    Returns
    -------
    LiveEngine : 已装配全部组件的实盘引擎（未start）
    """
    # 防环：LiveEngine/RiskConfig 属 engine.py 核心类，延迟导入（engine↔assembly 单向，拆包#422）
    # 其余符号原为 engine.py 模块级 import（L85/L90/L106），工厂区直接引用，随拆包迁入
    from myquant.core.lookahead_guard import LookaheadGuard
    from myquant.live.data_feed import MarketDataFeed
    from myquant.live.engine import LiveEngine, RiskConfig
    from myquant.live.portfolio import LivePortfolio
    from myquant.live.state_store import StateStore
    from myquant.risk.risk_manager import build_risk_manager_from_config
    from myquant.strategies import create_strategy

    live_cfg = dict(config.get("live", {}) or {})
    broker_cfg = dict(config.get("broker", {}) or {})
    execution_cfg = dict(config.get("execution", {}) or {})
    market_cfg = dict(config.get("market_data", {}) or {})
    strategy_cfg = dict(config.get("strategy", {}) or {})
    position_cfg = dict(config.get("position", {}) or {})
    risk_cfg = dict(config.get("risk_control", {}) or {})
    state_cfg = dict(config.get("state_store", {}) or {})
    scheduler_cfg = dict(config.get("scheduler", {}) or {})
    lookahead_cfg = dict(config.get("lookahead_guard", {}) or {})

    name = engine_id or str(live_cfg.get("name", "live"))
    initial_capital = float(live_cfg.get("initial_capital", 1_000_000))
    trade_mode = str(live_cfg.get("trade_mode", "paper")).lower()

    # 0.5) 运行模式一致性校验（切割接口：防误接入，即使绕过 CLI 直调工厂也拦截）
    from myquant.live.run_mode import check_mode_consistency

    consistency_issues = check_mode_consistency(config)
    if consistency_issues:
        raise ValueError("运行模式配置不一致: " + "; ".join(consistency_issues))

    # 0.6) KP-P6-01：实盘(LIVE)程序化交易报备合规门禁（装配前拦截，防绕过 CLI）
    if trade_mode == "live":
        from myquant.live.acceptance import ComplianceGate

        comp = ComplianceGate(config).check()
        if not comp.overall:
            raise ValueError("实盘合规门禁未通过，禁止启动: " + "; ".join(comp.errors))

    # 1) 引擎级风控参数（与 position 段一致，双层风控同源）
    risk_config = RiskConfig(
        max_position_pct=float(position_cfg.get("max_single_position", 0.20)),
        max_positions=int(position_cfg.get("max_total_positions", 5)),
        min_cash_ratio=max(0.0, 1.0 - float(position_cfg.get("max_total_position_pct", 0.95))),
    )

    # 2) 券商适配器
    broker = build_broker_from_config(broker_cfg, execution_cfg, initial_capital)
    # 0.9（回写#294）：broker 提交边界硬闸——状态源接线（薄闸在基类不可绕过）。
    # 非交易时段判定与 TradingHoursRule 同源（交易日历 + 时段，broker 不重写）；
    # risk_paused 判定经延迟闭包引用后续装配的 risk_manager（submit 时才求值）。
    from myquant.risk.rules import TradingHoursRule as _GateTradingHoursRule

    _gate_th = _GateTradingHoursRule()
    broker.set_trading_hours_checker(
        lambda: _gate_th._is_trading_day(datetime.now().date())
        and _gate_th._is_trading_time()
    )

    # 3) 行情接入（流式清洗默认开启，与回测数据纪律一致）
    # T3-1：指标层统一 hfq（与回测 backtest_config.adjust=hfq 一致），
    #   除权日不跳变；撮合/下单仍用 broker 真实 raw 价（券商认的就是这个）。
    feed = MarketDataFeed(
        source=str(market_cfg.get("source", "akshare")),
        frequency=str(market_cfg.get("frequency", "daily")),
        enable_clean=bool(market_cfg.get("enable_clean", True)),
        local_history_dir=str(market_cfg.get("local_history_dir", "datasets/processed")),
        adjust_out=str(market_cfg.get("adjust_out", "hfq")),
    )

    # 4) 策略（与回测共用同一注册器，无缝迁移，铁律2）
    strategy_name = str(strategy_cfg.get("name", "ma_cross"))
    strategy_params = dict(strategy_cfg.get("params", {}) or {})
    # 任务二修复：策略资金基准与账户一致（读 live 配置 initial_capital，避免默认 100 万口径错位）。
    # 配置已显式指定 capital_base 时尊重配置；否则注入账户资金。
    strategy_params.setdefault("capital_base", initial_capital)
    strategy = create_strategy(strategy_name, **strategy_params)

    # ── M0-09 出口强制：防泄漏门禁（v3.0 §5.4 唯一出口收敛点，实盘出口同拦）──
    # 与回测出口（BacktestScheduler.create_engine，M0-08）对齐：无契约/引用阻断列的
    # 策略在实盘引擎装配前即被拦。
    # 注：测试环境经 tests/conftest.py 关闭 enabled（仅审计），真实出口默认阻断。
    from myquant.leakage_gate import assert_all_gates_pass

    assert_all_gates_pass(
        strategy_id=strategy_name,
        decision_time=datetime.now().strftime("%Y-%m-%d"),
    )

    # 5) RiskManager（真钱实盘启用交易时段规则；模拟盘不启用）
    # P1-07：真钱(live)模式忽略 enabled:false——配置错=无保护是 fail-open，
    # 强制启用完整规则库并 CRITICAL 告警；paper/sim/monitor 尊重配置。
    risk_manager = None
    risk_enabled = bool(risk_cfg.get("enabled", True))
    if trade_mode == "live" and not risk_enabled:
        logger.critical(
            "[build] trade_mode=live 但 risk.enabled=false 被忽略：真钱模式强制启用风控（P1-07）"
        )
        risk_enabled = True
    if risk_enabled:
        risk_manager = build_risk_manager_from_config(
            risk_cfg, position_cfg, strategy_cfg,
            enable_trading_hours=(trade_mode != "paper"),
        )
        # 0.9（回写#294）：risk_paused 判定注入 broker 薄闸（状态非 NORMAL 或熔断禁开仓）
        from myquant.risk.risk_manager import RiskStatus as _GateRiskStatus

        broker.add_risk_pause_checker(
            lambda: risk_manager.status != _GateRiskStatus.NORMAL
            or bool(getattr(risk_manager, "_circuit_paused", False))
        )

    # 6) StateStore（崩溃恢复；目录遵循配置，默认 datasets/live_state）
    state_store = None
    if bool(state_cfg.get("enabled", True)):
        state_dir = state_cfg.get("state_dir")
        store_root = _Path(state_dir).expanduser() if state_dir else None
        state_store = StateStore(root=store_root, store_id=str(name))

    # 6.1) TradeLedger（T1-1：本地账本落地，真钱硬前置 RC-07）
    #      live 模式必须装配，否则 _assert_live_reconciliation_gate 拒启
    trade_ledger = None
    try:
        from myquant.database.connection import DatabaseConnection
        from myquant.database.trade_log import TradeLedger
        trade_ledger = TradeLedger(DatabaseConnection("database/quant.db"))
        logger.info("[build] TradeLedger 已装配（T1-1 本地账本）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[build] TradeLedger 装配失败（live 模式将拒启）: {exc}")

    # 6.5) LookaheadGuard（任务42：消费配置 lookahead_guard.enabled / strict_mode 段）
    #      配置模板默认 enabled:true + strict_mode:true（规则4强制防未来）
    lookahead_enabled = bool(lookahead_cfg.get("enabled", True))
    lookahead_guard = None
    if lookahead_enabled:
        lookahead_guard = LookaheadGuard(strict=bool(lookahead_cfg.get("strict_mode", True)))

    # 6.7) CircuitBreaker（缺口1：熔断动作链在实盘进程通电；缺口13：按 engine_id 隔离）
    #      auto_liquidate 保持 False（人工确认兜底），不允许被配置覆盖。
    from myquant.core.audit_log import AuditLogStore
    from myquant.monitoring import get_alert_manager as _build_get_alert_manager
    from myquant.monitoring.circuit_breaker import (
        CircuitBreaker,
        CircuitBreakerConfig,
        get_event_bus_safe,
    )

    _cb_audit_store: Optional[AuditLogStore] = None
    try:
        _cb_audit_store = AuditLogStore()
    except Exception as exc:  # F-6：审计存储不可用不阻断交易，熔断审计降级为 logger
        logger.warning("[build] 审计存储不可用，熔断审计降级为 logger: %s", exc)

    circuit_breaker = CircuitBreaker(
        broker=broker,
        risk_manager=risk_manager,
        event_bus=get_event_bus_safe(),
        alert_manager=_build_get_alert_manager(),
        audit_store=_cb_audit_store,
        config=CircuitBreakerConfig(auto_liquidate=False),
        engine_id=str(name),
    )
    circuit_breaker.subscribe_to_event_bus()

    # T1-1：TradeLedger 注册到 registry（引擎通过 get_component 取用）
    if trade_ledger is not None:
        from myquant.core.registry import register_component
        register_component("trade_ledger", trade_ledger)

    # 7) 引擎装配（保障组件经构造函数注入，调度器由引擎自建自停）
    engine = LiveEngine(
        risk_config=risk_config,
        engine_id=str(name),
        risk_manager=risk_manager,
        state_store=state_store,
        scheduler=None,
        lookahead_guard=lookahead_guard,
        enable_lookahead_guard=lookahead_enabled,
        enable_scheduler=bool(scheduler_cfg.get("enabled", True)),
        auto_recover=_require_auto_recover(state_cfg),
        monitor_only=monitor_only,
        snapshot_interval=float(state_cfg.get("backup_interval", 300)),
        trade_mode=trade_mode,
    )
    # 缺口1 产物：熔断挂引擎（供 0.8 启动断言 / 阶段1 /api/risk/status 读取）
    engine._circuit_breaker = circuit_breaker
    engine.set_data_feed(feed)
    engine.set_broker(broker)
    engine.set_portfolio(LivePortfolio(initial_capital=initial_capital, account_id=str(name)))
    # 0.6（回写#292）：实时对账差异超阈 → 禁开仓（用例5）。注入点方式——
    # portfolio 不反向依赖 engine；回调走 engine.pause（带 reason，0.2 _pause_source
    # 语义，与周期链 _broker_accountability_patrol 的 pause 原因可区分）。
    engine._portfolio.set_pause_hook(
        lambda diff: engine.pause(reason="账实差异超阈（实时对账，禁开仓）")
    )
    engine.add_strategy(strategy)

    # 8) 订阅配置标的（market_data.subscriptions + stock_pool.codes 合并去重）
    subscriptions = list(market_cfg.get("subscriptions", []) or [])
    pool_codes = list((config.get("stock_pool", {}) or {}).get("codes", []) or [])
    combined_watch = list(dict.fromkeys(subscriptions + pool_codes))
    engine._live_watch_symbols = combined_watch
    for symbol in combined_watch:
        feed.subscribe(str(symbol), engine.on_market_data)

    # 本地历史预热目录（配置外置：market_data.local_history_dir）
    engine._local_history_dir = str(market_cfg.get("local_history_dir", "datasets/processed"))

    # KP-P5-07: config_version 落引擎（来自 live_config.config_version，变更管理版本留痕）
    engine._config_version = str(config.get("config_version", "") or "")

    # 0.5（回写#291）：LIVE 启动门——watchdog 为 live 必备装配能力（A-4 行情活性 /
    # A-5 引擎心跳 / A-6 券商断连 三看门狗，装配开关 monitoring.watchdog.enabled 唯一真源
    # config/settings.yaml，scheduler 侧不跨真源覆盖）。live 且未启用 → 拒启
    # （F-1 能力缺失 fail-closed）；paper/sim 尊重配置原值（false 可运行）。
    _assert_live_watchdog_gate(trade_mode)

    # T0-8：LIVE 实时对账装配门——scheduler/account_reconciler/alert_manager
    # 三者缺一不可（在册未接线防复发）；paper/sim 尊重配置原值。
    _assert_live_reconciliation_gate(trade_mode, scheduler_cfg)

    # P1-11：实盘出口泄漏审计报告（R-16 §4.2；断言通过后、返回引擎前落盘；
    # 实盘不 reset guard——统计为装配期进程累计，报告标注语义）
    _write_live_leakage_report(strategy_name, lookahead_guard, name)

    return engine


def _assert_live_watchdog_gate(trade_mode: str) -> None:
    """
    0.5（回写#291）LIVE 启动门：watchdog 未启用 → 拒启（F-1 能力缺失 fail-closed）。

    三看门狗（A-4 行情活性 / A-5 引擎心跳 / A-6 券商断连）是 LIVE 必备装配能力，
    装配开关 monitoring.watchdog.enabled 唯一真源 config/settings.yaml
    （scheduler 侧不跨真源覆盖）；paper/sim 尊重配置原值（false 可运行）。

    Parameters
    ----------
    trade_mode : str
        引擎运行模式（live/paper/sim）。

    Raises
    ------
    ValueError
        trade_mode=live 且 watchdog 未启用时拒启。
    """
    if str(trade_mode).lower() == "live":
        from myquant.core.settings import get_monitoring_config

        if not get_monitoring_config().get("watchdog", {}).get("enabled", False):
            raise ValueError(
                "LIVE 模式拒绝启动：monitoring.watchdog.enabled=false"
                "（A-4/A-5/A-6 看门狗未启用，能力缺失 fail-closed；"
                "请在 config/settings.yaml 开启 monitoring.watchdog.enabled）"
            )


def _assert_live_reconciliation_gate(
    trade_mode: str,
    scheduler_cfg: Dict[str, Any],
) -> None:
    """
    T0-8 LIVE 启动门：实时对账 + 告警 + 调度器为 live 必备装配能力，未接线 → 拒启。

    三项硬前置（缺一拒启）：
      1. scheduler.enabled=true（周期核对链必须跑，否则实时对账只靠事件触发，
         券商侧状态漂移无人兜底）；
      2. account_reconciler 已注册（registry 有工厂，否则差异超阈无法冻结/告警）；
      3. alert_manager 已注册（registry 有工厂，否则 CRITICAL 告警无人接收）。

    paper/sim 尊重配置原值（false / 未注册可运行）。

    Parameters
    ----------
    trade_mode : str
        引擎运行模式（live/paper/sim）。
    scheduler_cfg : dict
        scheduler 配置段（来自 config["scheduler"]）。

    Raises
    ------
    ValueError
        trade_mode=live 且任一硬前置未满足时拒启。
    """
    if str(trade_mode).lower() != "live":
        return

    from myquant.core.registry import get_component

    errors: List[str] = []

    # 1) scheduler.enabled
    if not bool(scheduler_cfg.get("enabled", True)):
        errors.append(
            "scheduler.enabled=false（周期核对链未启用，券商侧状态漂移无人兜底）"
        )

    # 2) account_reconciler 已注册
    if get_component("account_reconciler") is None:
        errors.append(
            "account_reconciler 未注册（registry 无工厂，差异超阈无法冻结/告警；"
            "请确认 myquant.monitoring.account_reconciler 已被 import 触发自注册）"
        )

    # 3) alert_manager 已注册
    if get_component("alert_manager") is None:
        errors.append(
            "alert_manager 未注册（registry 无工厂，CRITICAL 告警无人接收；"
            "请确认 myquant.monitoring.alerting 已被 import 触发自注册）"
        )

    # 4) trade_ledger 已注册（T1-1：本地账本落地，真钱硬前置 RC-07）
    if get_component("trade_ledger") is None:
        errors.append(
            "trade_ledger 未注册（本地账本未装配，orders/fills/reconciliation 无法落库；"
            "请确认 TradeLedger(DatabaseConnection()) 已装配）"
        )

    if errors:
        detail = "；".join(errors)
        raise ValueError(
            f"LIVE 模式拒绝启动：{detail}"
            "（T0-8 实时对账装配门 fail-closed）"
        )


def _write_live_leakage_report(
    strategy_name: str,
    lookahead_guard: Optional[Any],
    engine_id: str,
) -> None:
    """实盘引擎装配泄漏审计报告落盘（P1-11 步骤②，R-16 §4.2）。

    实盘决策时点 = 装配当天（无回测 end_date 概念，与 create_live_engine 内
    assert_all_gates_pass 的 decision_time 同源）；lookahead_guard 为装配期
    新实例（live 非全局单例，lookahead_guard.py:2838），统计取实例快照，
    None（guard 未启用）记空段。报告落盘失败不阻断装配（审计旁路）。

    Parameters
    ----------
    strategy_name : str
        装配策略名。
    lookahead_guard : LookaheadGuard | None
        装配期 guard 实例（未启用时为 None）。
    engine_id : str
        引擎标识（写入实验 ID）。
    """
    from myquant.leakage_gate.gate import collect_gate_stats
    from myquant.leakage_gate.report import write_experiment_report

    decision_time = datetime.now().strftime("%Y-%m-%d")
    try:
        gate_stats = collect_gate_stats(strategy_name, decision_time)
        write_experiment_report(
            experiment_id=f"live_{strategy_name}_{engine_id}",
            experiment_type="live",
            strategy_id=strategy_name,
            decision_time=decision_time,
            gate_stats=gate_stats,
            lookahead_guard=lookahead_guard.stats if lookahead_guard else None,
        )
    except Exception:
        # 报告是审计产物：失败仅记录可见，不得阻断实盘装配（py 规则：记录后恢复）
        logger.warning("实盘泄漏审计报告落盘失败（P1-11，审计旁路不阻断装配）", exc_info=True)


def startup_assertions(
    engine: Any,
    config: Dict[str, Any],
    run_mode: Any,
) -> List[str]:
    """
    0.8（回写#295）启动自检断言（装配=0 的根因治理）。

    把"装配"变成"不满足就拒启"：防止第0阶段（0.1~0.9）做完被后续改动拆掉。
    返回问题列表（空 = 通过）。二分处置（决策点5，V3 §十.5）：
      - 能力缺失（代码装配产物）→ 计入问题列表（入口拒启，return 2）；
      - 外部依赖不可用（告警渠道/系统时钟）→ logger.critical 降级 + CRITICAL，
        不计入问题列表（不因单个外部依赖抖动拒启，F-2 语义）。
    每项独立 try-except：单项异常不影响其余断言（决策点5 可开关语义）。

    Parameters
    ----------
    engine : LiveEngine
        已装配的实盘引擎实例。
    config : Dict
        live 配置（当前仅作签名预留，断言对象全部来自装配产物/单读配置入口）。
    run_mode : RunMode
        运行模式（LIVE 全断言；paper/sim/monitor 只断言引擎装配，即已通过）。

    Returns
    -------
    List[str] : 自检问题列表（空 = 通过；非空 = 能力缺失，入口应拒启）。
    """
    from myquant.live.run_mode import RunMode as _RunMode

    if run_mode is not _RunMode.LIVE:
        # paper/sim/monitor：引擎装配成功即通过（live 专属门禁不适用）
        return []

    problems: List[str] = []

    # ① 熔断器已注入且已订阅（0.1 产物；能力缺失）
    try:
        if getattr(engine, "_circuit_breaker", None) is None:
            problems.append(
                "熔断器未注入（engine._circuit_breaker is None，0.1 产物缺失，能力缺失拒启）"
            )
    except Exception as e:  # noqa: BLE001
        problems.append(f"熔断断言异常（视为能力缺失拒启）: {e}")

    # ② watchdog enabled（0.5 启动门；能力缺失——LIVE 必备三看门狗）
    try:
        from myquant.core.settings import get_monitoring_config

        if not get_monitoring_config().get("watchdog", {}).get("enabled", False):
            problems.append(
                "monitoring.watchdog.enabled=false（0.5 启动门被关，LIVE 能力缺失拒启）"
            )
    except Exception as e:  # noqa: BLE001
        problems.append(f"watchdog 配置不可读（0.5 能力缺失拒启）: {e}")

    # ③ 准入守卫已注册且配置加载成功（0.4/F-1；能力缺失）
    try:
        from myquant.risk.guardian import get_operation_guard

        guard = get_operation_guard()
        if guard is None:
            problems.append("准入守卫未注册（0.4 产物缺失，能力缺失拒启）")
        elif getattr(guard, "load_error", ""):
            problems.append(
                f"准入守卫配置加载失败（{getattr(guard, 'load_error', '')}，F-1 能力缺失拒启）"
            )
    except Exception as e:  # noqa: BLE001
        problems.append(f"准入守卫断言异常（能力缺失拒启）: {e}")

    # ④ pause_on_critical 生效（0.2 产物：live 模式 CRITICAL 健康自动停引擎）
    try:
        if getattr(engine, "_trade_mode", None) != "live":
            problems.append(
                "engine._trade_mode != live（pause_on_critical 装配前提缺失，0.2 产物）"
            )
        elif not getattr(engine, "_scheduler_enabled", True):
            problems.append(
                "调度器被禁用（pause_on_critical 巡检无法装配，0.2 产物，能力缺失拒启）"
            )
    except Exception as e:  # noqa: BLE001
        problems.append(f"pause_on_critical 断言异常（能力缺失拒启）: {e}")

    # ⑤ 告警渠道配置非空（B2；外部依赖 → 降级 CRITICAL，不拒启）
    try:
        from myquant.monitoring.alerting import get_alert_manager

        am = get_alert_manager()
        channels = getattr(am, "channels", None) or []
        if not channels:
            logger.critical(
                "[startup_assertions] 告警渠道未配置（B2）：LIVE 下关键告警无法送达"
                "（外部依赖降级，不拒启，F-2 语义）"
            )
    except Exception as e:  # noqa: BLE001
        logger.critical(
            f"[startup_assertions] 告警管理器不可用（外部依赖降级，不拒启）: {e}"
        )

    # ⑥ 系统时钟可用（F-5：V3 §九 9.3 明确"时钟源不可达 → 拒绝 live 启动，允许 paper"；
    #     时间被篡改/离线会触发错误交易判定，属能力缺失而非可降级外部依赖）
    try:
        datetime.now()
    except Exception as e:  # noqa: BLE001
        problems.append(f"系统时钟不可用（F-5：拒绝 live 启动，允许 paper）: {e}")

    # ⑦ 应急链旁路可用（0.3 产物；能力缺失——弱断言：工厂模块可导入 + 一键平仓脚本存在）
    try:
        from myquant.live.broker_factory import build_broker_from_config  # noqa: F401

        emergency_script = (
            _Path(__file__).resolve().parent.parent.parent / "scripts" / "一键平仓.py"
        )
        if not emergency_script.exists():
            problems.append("一键平仓应急脚本缺失（0.3 应急链，能力缺失拒启）")
    except Exception as e:  # noqa: BLE001
        problems.append(f"应急链不可用（0.3 产物缺失，能力缺失拒启）: {e}")

    return problems
