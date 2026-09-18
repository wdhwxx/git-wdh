# 文件名：run_实盘入口.py
"""
实盘交易入口脚本（薄入口）
==========================

功能：
    解析命令行参数，加载实盘配置，装配完整实盘链路并启动实盘交易引擎。
    本脚本是薄入口，不包含任何实盘业务逻辑——全部业务在 myquant 包内。

装配链路（H1/H2修复，对齐蓝图 docs/project_layout_gear2.md live/ 模块）:
    ConfigLoader加载配置
      → myquant.live.engine.create_live_engine（一键装配工厂）
          ├─ MarketDataFeed       实时行情接入（流式清洗）
          ├─ 券商适配器            paper/sim/qmt/ptrade（build_broker_from_config）
          ├─ LivePortfolio        实时持仓管理
          ├─ 策略注册器            与回测共用同一注册器（铁律2无缝迁移）
          ├─ LiveEngine           实盘引擎（双层风控：内置检查+RiskManager规则库）
          ├─ RiskManager          订单提交前强制检查+数据质量事件联动
          ├─ RiskGuardian         风控守护（巡检熔断决策）
          ├─ StateStore           状态持久化/崩溃恢复
          └─ LiveScheduler        巡检/收盘日报/定时快照
      → 事件总线联动（AlertManager告警 + MetricsCollector指标 自动订阅）
      → engine.start() 启动交易循环

使用方法：
    # 使用配置文件启动实盘（默认 config/live_config.yaml，缺失时回退example模板）
    python run_实盘入口.py --config config/live_config.yaml

    # 模拟盘模式（不连接真实券商，使用本地模拟撮合）
    python run_实盘入口.py --paper-trading

    # 指定券商适配器（paper=本地模拟 / qmt=迅投QMT / ptrade=恒生Ptrade）
    python run_实盘入口.py --broker qmt

    # 仅监控模式（订阅行情、生成信号，不提交订单）
    python run_实盘入口.py --monitor-only

    # 试运行（装配全部组件但不进入交易循环，用于配置验证）
    python run_实盘入口.py --dry-run

    # 查看实盘状态（状态快照/保障组件）
    python run_实盘入口.py --status

设计原则（薄入口，铁律2/铁律3）：
    - 本脚本只做：参数解析 → 配置加载 → 调用 myquant 内部函数 → 主循环等待
    - 所有实盘业务逻辑在 myquant.live 包中实现
    - 实盘与回测共用同一套策略类、指标计算、风控规则（铁律2：实盘无缝对接）
    - 防未来函数校验在实盘中同样强制（铁律4）；行情时间戳引擎内校验
    - 支持多券商适配（QMT/Ptrade），通过 pyproject 入口点 myquant.brokers 扩展

安全机制：
    - 启动前强制检查交易时间（真钱实盘模式，非交易时段默认拒绝，可 --force 覆盖）
    - 每笔订单提交前经双层风控强制检查（引擎内置 + RiskManager完整规则库）
    - 状态持久化（StateStore），崩溃后可自动恢复（配置 auto_recover_on_start）
    - 风控守护定期巡检，异常自动暂停开仓；告警/指标经事件总线联动
    - 日志完整记录所有操作，支持审计回溯（logs/ 主日志+错误+交易+数据质量）
"""

from __future__ import annotations

import argparse
import logging
import signal as os_signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


# ═══════════════════════════════════════════════
#  基础工具
# ═══════════════════════════════════════════════

def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    max_depth = 20
    depth = 0
    while current != current.parent and depth < max_depth:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        depth += 1
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def is_trading_time() -> bool:
    """
    检查当前是否为A股交易时间
    交易日：周一至周五（节假日需交易日历判断，此处简化）
    交易时段：9:30-11:30, 13:00-15:00
    """
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hour_minute = now.hour * 100 + now.minute
    morning = 930 <= hour_minute <= 1130
    afternoon = 1300 <= hour_minute <= 1500
    return morning or afternoon


# ═══════════════════════════════════════════════
#  命令行参数
# ═══════════════════════════════════════════════

def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="run_实盘入口",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 实盘交易入口（薄入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python run_实盘入口.py --config config/live_config.yaml
  python run_实盘入口.py --paper-trading
  python run_实盘入口.py --broker qmt --force
  python run_实盘入口.py --monitor-only
  python run_实盘入口.py --dry-run
  python run_实盘入口.py --status
        """,
    )

    # 配置文件
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="实盘配置文件路径（YAML格式），默认 config/live_config.yaml",
    )

    # 运行模式
    parser.add_argument(
        "--paper-trading",
        action="store_true",
        help="模拟盘模式（强制使用本地模拟撮合，不连接真实券商）",
    )
    parser.add_argument(
        "--monitor-only",
        action="store_true",
        help="仅监控模式（订阅行情、生成信号，不提交订单）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="查看实盘状态快照并退出",
    )

    # 券商配置
    parser.add_argument(
        "--broker", "-b",
        type=str,
        default=None,
        choices=["paper", "sim", "qmt", "ptrade"],
        help="券商适配器类型（paper=本地模拟 / qmt=迅投QMT / ptrade=恒生Ptrade）",
    )
    parser.add_argument(
        "--engine-id",
        type=str,
        default=None,
        help="引擎ID（覆盖配置 live.name；影响状态存储隔离）",
    )

    # 运行控制
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=60,
        help="日线行情轮询间隔秒数（默认60；实时模式由引擎控制）",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="运行时长秒数（0=持续运行直到 Ctrl+C，默认0）",
    )

    # 安全控制
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制启动（跳过交易时间检查，非交易时段也可启动）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="试运行（装配全部组件但不进入交易循环，用于配置验证）",
    )

    # 日志
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="启用详细日志输出（DEBUG级别）",
    )

    return parser


# ═══════════════════════════════════════════════
#  配置加载（H1修复：使用 ConfigLoader，不再引用不存在的 load_config）
# ═══════════════════════════════════════════════

def resolve_config_path(args: argparse.Namespace, project_root: Path) -> Optional[str]:
    """
    解析配置文件路径

    优先级：--config 参数 > config/live_config.yaml > config/live_config.example.yaml
    返回 None 表示未找到任何配置。
    """
    if args.config:
        p = Path(args.config)
        if not p.is_absolute():
            p = project_root / p
        if not p.exists():
            print(f"[错误] 指定的配置文件不存在: {p}")
            return None
        return str(p)

    default_config = project_root / "config" / "live_config.yaml"
    example_config = project_root / "config" / "live_config.example.yaml"
    if default_config.exists():
        return str(default_config)
    if example_config.exists():
        print(f"[提示] 未找到实际配置文件，使用示例配置：{example_config}")
        print("       建议复制为 config/live_config.yaml 后修改参数。")
        return str(example_config)
    return None


def load_live_config(config_path: str, project_root: Path) -> Dict[str, Any]:
    """
    加载实盘配置（H1修复）

    使用 myquant.core.config_loader.ConfigLoader（支持 YAML/JSON、
    环境变量插值 ${VAR}、include 继承、默认值校验）。
    """
    from myquant.core.config_loader import ConfigLoader

    loader = ConfigLoader(base_dir=project_root)
    return loader.load(config_path)


def apply_cli_overrides(config: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """命令行参数覆盖配置（模拟盘/券商类型/监控模式）"""
    config.setdefault("live", {})
    config.setdefault("broker", {})

    if args.paper_trading:
        config["live"]["trade_mode"] = "paper"
        config["broker"]["type"] = "paper"
    if args.broker:
        config["broker"]["type"] = args.broker
    if args.monitor_only:
        config["live"]["monitor_only"] = True
    if args.dry_run:
        config["live"]["dry_run"] = True
    return config


def validate_broker_platform(broker_type: Optional[str]) -> Optional[str]:
    """
    (疑点3确认) 校验券商适配器平台可用性

    qmt（迅投QMT）/ ptrade（恒生Ptrade）客户端仅支持 Windows；
    Linux/macOS（含 Docker live 容器）上即使传 --broker qmt/ptrade，
    也要到运行时调用核心动作才因 SDK 占位明确报错——此处提前到
    启动装配前拦截，并给出清晰平台原因（qmt_broker.py LIVE 通道为占位报错设计）。

    Returns
    -------
    Optional[str] : 校验失败的错误描述；None=校验通过
    """
    bt = (broker_type or "paper").lower()
    if bt not in ("qmt", "ptrade"):
        return None
    if sys.platform != "win32":
        return (
            f"券商适配器 {bt} 仅支持 Windows 平台（当前系统: {sys.platform}）。"
            "Docker live 容器为 Linux，无 QMT/Ptrade 客户端。"
            "请改用 --broker paper/sim 模拟运行，或在 Windows 本机执行实盘。"
        )
    # Windows 平台：SDK 未接入时适配器核心动作会明确报错（占位设计），无需在此强校验
    return None


# ═══════════════════════════════════════════════
#  监控联动（H2修复：告警/指标 经事件总线订阅）
# ═══════════════════════════════════════════════

def setup_monitoring_linkage() -> bool:
    """
    挂载监控告警联动（H2修复）

    AlertManager + MetricsCollector 订阅全局事件总线，自动接收:
      - 数据质量事件（脏数据告警、流水线完成、质量等级）
      - 风控暂停/恢复事件（RISK_PAUSED/RISK_RESUMED）
    AlertManager 单例默认注册控制台+日志双通道。
    全部 try-except 包裹：监控故障不影响交易主链路。

    Returns
    -------
    bool : True=联动挂载成功
    """
    try:
        from myquant.core.event_bus import get_event_bus
        from myquant.monitoring.alerting import get_alert_manager
        from myquant.monitoring.metrics_collector import get_metrics_collector

        bus = get_event_bus()
        alert_mgr = get_alert_manager()
        alert_mgr.subscribe_to_event_bus(bus)
        metrics = get_metrics_collector()
        metrics.subscribe_to_event_bus(bus)
        print("  [监控] 告警管理器 + 指标采集器 已订阅事件总线")
        return True
    except Exception as e:
        print(f"  [监控] 事件总线联动挂载失败（不影响交易主链路）: {e}")
        return False


# ═══════════════════════════════════════════════
#  状态查看（--status）
# ═══════════════════════════════════════════════

def show_live_status(project_root: Path) -> int:
    """
    显示实盘状态（H1修复：调用真实存在的 StateStore API）

    扫描状态存储根目录下的全部存储实例（按引擎ID隔离），
    打印存储摘要与最新快照信息。
    """
    from myquant.live.state_store import StateStore, SnapshotNotFoundError

    candidate_roots = [
        project_root / "datasets" / "live_state",
        project_root / "live_state",
    ]
    found_any = False

    for root in candidate_roots:
        if not root.exists():
            continue
        store_dirs = [d for d in root.iterdir() if d.is_dir()]
        if not store_dirs:
            continue
        found_any = True
        print(f"状态存储根目录：{root}")
        print("-" * 62)
        for store_dir in sorted(store_dirs):
            store = StateStore(root=root, store_id=store_dir.name)
            try:
                summary = store.get_summary()
            except Exception as e:
                print(f"  [{store_dir.name}] 摘要读取失败: {e}")
                continue
            latest = summary.get("latest_snapshot")
            print(f"  存储实例: {store_dir.name}")
            print(f"    快照总数: {summary.get('snapshot_count', 0)}")
            if latest:
                ts = latest.get("timestamp")
                ts_str = (
                    datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
                    if ts else "未知"
                )
                print(f"    最新快照: v{latest.get('version')} "
                      f"tag={latest.get('tag', '')!r} time={ts_str}")
            try:
                snap = store.load_latest()
                state = snap.state or {}
                account = state.get("account", {})
                positions = state.get("positions", [])
                engine_state = state.get("engine", {})
                if engine_state:
                    print(f"    引擎状态: {engine_state.get('state', '?')} "
                          f"策略={engine_state.get('strategies', [])}")
                if account:
                    print(f"    账户: 总资产={account.get('total_assets', '?')} "
                          f"可用={account.get('available_cash', '?')}")
                print(f"    持仓数: {len(positions)}")
            except SnapshotNotFoundError:
                print("    最新快照: （无）")
            except Exception as e:
                print(f"    最新快照读取失败: {e}")
            print()

    if not found_any:
        print("[提示] 未发现实盘状态快照（尚未运行过实盘或状态存储未启用）。")
        print(f"       扫描目录: {' / '.join(str(r) for r in candidate_roots)}")
    return 0


# ═══════════════════════════════════════════════
#  信号/成交可视化回调
# ═══════════════════════════════════════════════

def _print_signal(sig: Any) -> None:
    """信号回调（控制台可视化）"""
    try:
        print(f"  📈 [信号] {sig.symbol} {sig.signal_type.value} "
              f"@{sig.price:.2f} reason={sig.reason}")
    except Exception:
        pass


def _print_trade(trade: Any) -> None:
    """成交回报回调（控制台可视化）"""
    try:
        print(f"  ✅ [成交] {trade.symbol} {trade.side.value} "
              f"{trade.quantity:.0f}@{trade.price:.2f} 手续费={trade.commission:.2f}")
    except Exception:
        pass


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def run_live_trading(args: argparse.Namespace) -> int:
    """
    启动实盘交易（薄入口：调用 myquant 内部装配工厂）

    返回：
        0  正常退出
        1  配置错误
        2  组件装配/券商创建失败
        3  非交易时段且未使用 --force
        4  实盘启动/运行异常
    """
    project_root = find_project_root()

    # ── 0. 解析配置路径 ──
    config_path = resolve_config_path(args, project_root)
    if config_path is None:
        print("[错误] 未找到实盘配置文件。")
        print("       请创建 config/live_config.yaml 或使用 --config 指定。")
        return 1

    # ── 1. 加载配置（H1修复：ConfigLoader）──
    try:
        config = load_live_config(config_path, project_root)
    except Exception as e:
        print(f"[错误] 配置加载失败: {e}")
        return 1
    config = apply_cli_overrides(config, args)

    # ── 1.5 券商平台校验（疑点3确认：qmt/ptrade 仅 Windows，Linux/Docker 提前拦截）──
    broker_type = str(config.get("broker", {}).get("type", "paper")).lower()
    platform_error = validate_broker_platform(broker_type)
    if platform_error:
        print(f"[错误] {platform_error}")
        return 1

    live_cfg = config.get("live", {}) or {}
    trade_mode = str(live_cfg.get("trade_mode", "paper")).lower()

    # ── 2. 交易时间检查（安全机制：仅约束真钱实盘模式）──
    if not args.force and not args.paper_trading and not args.monitor_only and not args.dry_run:
        if trade_mode == "live" and not is_trading_time():
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[安全拦截] 当前时间 {now} 不在A股交易时段内。")
            print("  交易时段：周一至周五 9:30-11:30, 13:00-15:00")
            print("  如需强制启动，请添加 --force 参数。")
            print("  模拟盘(--paper-trading)/监控模式(--monitor-only)不受此限制。")
            return 3

    # ── 3. 初始化日志 ──
    log_level = logging.DEBUG if args.verbose else logging.INFO
    try:
        from myquant.core.logger import setup_logger
        setup_logger(level=log_level, log_dir=project_root / "logs")
    except Exception as e:
        print(f"[警告] 日志初始化失败（使用控制台输出）: {e}")

    # ── 4. 启动横幅 ──
    print("=" * 62)
    print("Pythonwdhmm 鲲鹏量化交易系统 — 实盘启动")
    print("=" * 62)
    print(f"项目根目录：  {project_root}")
    print(f"配置文件：    {config_path}")
    run_mode = (
        "监控模式(不下单)" if args.monitor_only
        else "模拟盘" if (trade_mode == "paper" or args.paper_trading)
        else "实盘(真钱)"
    )
    print(f"运行模式：    {run_mode}")
    print(f"券商适配器：  {args.broker or config.get('broker', {}).get('type', 'paper')}")
    print(f"策略：        {config.get('strategy', {}).get('name', 'ma_cross')}")
    print(f"试运行：      {'是' if args.dry_run else '否'}")
    print("-" * 62)

    # ── 5. 监控告警联动（H2修复：AlertManager/MetricsCollector 订阅事件总线）──
    setup_monitoring_linkage()

    # ── 6. 一键装配实盘引擎（H1/H2修复：create_live_engine 工厂）──
    try:
        from myquant.live.engine import create_live_engine
        engine = create_live_engine(
            config,
            engine_id=args.engine_id,
            monitor_only=bool(args.monitor_only),
        )
    except ValueError as e:
        print(f"[错误] 组件装配失败（券商/策略配置问题）: {e}")
        return 2
    except Exception as e:
        print(f"[错误] 实盘引擎装配失败: {type(e).__name__}: {e}")
        return 2

    # 控制台可视化回调
    engine.on_signal(_print_signal)
    engine.register_trade_callback(_print_trade)

    # ── 7. 试运行模式：打印装配结果后退出 ──
    if args.dry_run:
        status = engine.get_status()
        safeguards = status.get("safeguards", {})
        print("[试运行] 组件装配完成，保障组件状态：")
        print(f"  RiskManager（订单前置风控）: {'✅' if safeguards.get('risk_manager') else '❌'}")
        print(f"  RiskGuardian（风控守护）:    {'✅' if safeguards.get('guardian') else '❌'}")
        print(f"  StateStore（状态持久化）:    {'✅' if safeguards.get('state_store') else '❌'}")
        print(f"  LiveScheduler（调度/巡检）:  {'✅（启动时创建）' if safeguards.get('scheduler') is not None else '✅（start时自动创建）'}")
        print(f"  监控模式:                    {'是' if safeguards.get('monitor_only') else '否'}")
        print("[试运行] 未进入交易循环。去掉 --dry-run 即可正式运行。")
        return 0

    # ── 8. 启动引擎 ──
    print("[启动] 正在启动实盘引擎（连接券商/行情/策略/调度）...")
    try:
        started = engine.start()
    except KeyboardInterrupt:
        print("\n[中断] 启动被取消")
        return 0
    except Exception as e:
        print(f"[错误] 实盘引擎启动异常: {type(e).__name__}: {e}")
        return 4

    if not started:
        print("[错误] 实盘引擎启动失败（详见日志）")
        return 4

    status = engine.get_status()
    safeguards = status.get("safeguards", {})
    print(f"[启动] 引擎运行中: id={status.get('engine_id')} 策略={status.get('strategies')}")
    print(f"  保障组件: 风控={'✅' if safeguards.get('risk_manager') else '❌'} "
          f"守护={'✅' if safeguards.get('guardian') else '❌'} "
          f"状态={'✅' if safeguards.get('state_store') else '❌'} "
          f"调度={'✅' if safeguards.get('scheduler') else '❌'} "
          f"事件总线={'✅' if safeguards.get('event_bus') else '❌'}")
    if safeguards.get("state_recovered"):
        print("  [恢复] 已从上次状态快照恢复持仓/资金")
    print("[运行] Ctrl+C 优雅停止（自动保存状态快照）")
    print("-" * 62)

    # ── 9. 日线模式：启动后台轮询（实时模式引擎内已自动启动）──
    feed = engine.data_feed
    if feed is not None and not feed.is_realtime:
        try:
            feed.start(poll_interval=max(10, int(args.poll_interval)))
            print(f"[行情] 日线后台轮询已启动: 间隔={args.poll_interval}s")
        except Exception as e:
            print(f"[警告] 行情轮询启动失败: {e}")

    # ── 10. 主等待循环（优雅停止）──
    stop_event = threading.Event()
    start_time = time.time()
    last_report = 0.0

    def _request_stop(signum: Any = None, frame: Any = None) -> None:
        print("\n[停止] 收到停止信号，正在优雅停止...")
        stop_event.set()

    try:
        os_signal.signal(os_signal.SIGINT, _request_stop)
        os_signal.signal(os_signal.SIGTERM, _request_stop)
    except (ValueError, OSError):
        pass  # 非主线程环境降级为仅 KeyboardInterrupt

    try:
        while not stop_event.is_set():
            if args.duration > 0 and (time.time() - start_time) >= args.duration:
                print(f"\n[停止] 达到运行时长 {args.duration}s")
                break
            stop_event.wait(timeout=1.0)
            # 每60秒打印一次运行摘要
            now = time.time()
            if now - last_report >= 60.0:
                last_report = now
                st = engine.stats
                print(
                    f"  [状态] bar={st.get('total_bars', 0)} "
                    f"信号={st.get('total_signals', 0)} "
                    f"订单={st.get('total_orders', 0)} "
                    f"成交={st.get('total_trades', 0)} "
                    f"风控拒绝={st.get('risk_rejected', 0)} "
                    f"错误={st.get('errors', 0)}"
                )
    except KeyboardInterrupt:
        print("\n[停止] 收到 Ctrl+C，正在优雅停止...")
    finally:
        try:
            engine.stop()
            print("[停止] 引擎已停止（状态快照已保存）")
        except Exception as e:
            print(f"[警告] 引擎停止异常: {e}")
            return 4

    # ── 11. 运行总结 ──
    st = engine.stats
    print("-" * 62)
    print("运行总结：")
    print(f"  运行时长：  {time.time() - start_time:.1f}s")
    print(f"  行情bar：   {st.get('total_bars', 0)}")
    print(f"  信号：      {st.get('total_signals', 0)} "
          f"(买={st.get('buy_signals', 0)} 卖={st.get('sell_signals', 0)})")
    print(f"  订单：      {st.get('total_orders', 0)} "
          f"(成交={st.get('filled_orders', 0)})")
    print(f"  风控拒绝：  {st.get('risk_rejected', 0)}")
    print(f"  异常：      {st.get('errors', 0)}")
    risk = engine.get_status().get("risk")
    if risk:
        print(f"  风控终态：  {risk.get('status')} "
              f"暂停标的={risk.get('paused_symbols', [])}")
    return 0


def main() -> int:
    """主入口函数"""
    parser = setup_argument_parser()
    args = parser.parse_args()

    project_root = find_project_root()

    # 查看状态模式
    if args.status:
        return show_live_status(project_root)

    # 启动实盘
    return run_live_trading(args)


if __name__ == "__main__":
    sys.exit(main())
