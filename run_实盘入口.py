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
import json
import logging
import signal as os_signal

logger = logging.getLogger("run_live")
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


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
    parser.add_argument(
        "--mode",
        type=str,
        default=None,
        choices=["monitor", "paper", "sim", "live"],
        help="运行模式（切割接口）：monitor=仅监控 / paper=本地模拟盘(默认) / "
             "sim=券商同源仿真 / live=真钱实盘",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="显式确认进入实盘(LIVE)模式（配合 --mode live，防止误接入）",
    )
    parser.add_argument(
        "--stats-json",
        type=str,
        default=None,
        help="运行结束将引擎统计写入指定 JSON 路径（绩效留痕，供准入评估消费）",
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


def apply_cli_overrides(config: Dict[str, Any], args: argparse.Namespace) -> Any:
    """命令行参数覆盖配置（运行模式 / 券商类型 / 监控模式）。

    以 RunMode（切割接口）为单一真相源：解析统一运行模式后写回配置，
    保证 create_live_engine 消费到与 CLI 一致的 trade_mode / broker.mode。

    Returns
    -------
    Any : 解析出的统一运行模式（myquant.live.run_mode.RunMode）。

    Raises
    ------
    ValueError : --mode live 与 --paper-trading 冲突，或非法模式。
    """
    from myquant.live.run_mode import RunMode, apply_mode_to_config, resolve_run_mode

    config.setdefault("live", {})
    config.setdefault("broker", {})

    if args.mode and args.mode.lower() == "live" and args.paper_trading:
        raise ValueError("--mode live 与 --paper-trading 冲突，请明确选择仿真或实盘")

    if args.mode:
        run_mode = resolve_run_mode(config, cli_mode=args.mode)
    elif args.paper_trading:
        run_mode = RunMode.PAPER
    else:
        run_mode = resolve_run_mode(
            config, monitor_only=bool(getattr(args, "monitor_only", False))
        )

    apply_mode_to_config(config, run_mode)

    if args.broker:
        config["broker"]["type"] = args.broker
    if args.monitor_only:
        config["live"]["monitor_only"] = True
    if args.dry_run:
        config["live"]["dry_run"] = True
    return run_mode


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
#  绩效留痕（--stats-json）
# ═══════════════════════════════════════════════

def write_stats_json(
    engine: Any,
    run_mode: Any,
    output_path: str,
    project_root: Path,
) -> Optional[str]:
    """运行结束导出引擎统计到 JSON（绩效留痕，供准入评估消费）。

    Args:
        engine: 已装配的 LiveEngine 实例。
        run_mode: 本次运行模式（myquant.live.run_mode.RunMode）。
        output_path: 输出 JSON 路径（相对路径以项目根为基准）。
        project_root: 项目根目录。

    Returns:
        写入的绝对路径；失败返回 None（仅告警，不阻断）。
    """
    try:
        from myquant.live.run_mode import describe

        stats = engine.stats
        status = engine.get_status()
        payload: Dict[str, Any] = {
            "engine_id": status.get("engine_id"),
            "run_mode": run_mode.value,
            "mode_desc": describe(run_mode),
            "strategies": status.get("strategies"),
            "timestamp": datetime.now().isoformat(),
            "stats": stats,
            "risk": status.get("risk"),
        }
        out = Path(output_path)
        if not out.is_absolute():
            out = project_root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        return str(out)
    except Exception as e:
        logger.warning("写入统计 JSON 失败: %s", e)
        return None


def _write_live_status(engine, project_root):
    """运行期间持续写 outputs/paper_stats/live_status.json（供 GUI 轮询展示盈亏）。

    与 write_stats_json（停止时一次性导出）不同，本函数在主循环里每 5 秒调用一次，
    把引擎 stats + 资产快照写到固定路径，GUI 弹窗每 2 秒轮询此文件刷新面板。
    """
    try:
        stats = engine.stats
        total_assets = 0.0
        day_start_assets = 0.0
        initial_capital = 0.0
        portfolio = getattr(engine, "_portfolio", None)
        if portfolio is not None:
            try:
                total_assets = float(portfolio.get_total_assets())
                initial_capital = float(getattr(portfolio, "initial_capital", 0) or 0)
            except Exception:
                pass
        day_start_assets = float(getattr(engine, "_day_start_assets", 0) or 0)

        payload = {
            "timestamp": datetime.now().isoformat(),
            "stats": stats,
            "total_assets": total_assets,
            "day_start_assets": day_start_assets,
            "initial_capital": initial_capital,
        }
        out = project_root / "outputs" / "paper_stats" / "live_status.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.debug("写 live_status.json 失败: %s", e)


def write_report_html(
    engine: Any,
    run_mode: Any,
    project_root: Path,
) -> Optional[str]:
    """运行结束生成自包含 HTML 报告（人可读，深色主题）。

    Args:
        engine: LiveEngine 实例。
        run_mode: 运行模式。
        project_root: 项目根目录。

    Returns:
        写入的 HTML 路径；失败返回 None。
    """
    try:
        from myquant.live.run_mode import describe
        stats = engine.stats
        status = engine.get_status()
        portfolio = getattr(engine, "_portfolio", None)
        account = portfolio.get_account() if portfolio is not None else {}
        positions = portfolio.get_all_positions() if portfolio is not None else []

        total_pnl = account.get("total_pnl", 0.0)
        pnl_color = "#F87171" if total_pnl > 0 else ("#34D399" if total_pnl < 0 else "#C7D2FE")
        pnl_sign = "+" if total_pnl > 0 else ""

        rows = ""
        for pos in positions:
            upnl = pos.unrealized_pnl
            upnl_color = "#F87171" if upnl > 0 else ("#34D399" if upnl < 0 else "#C7D2FE")
            rows += f"""<tr>
                <td>{pos.symbol}</td>
                <td style="text-align:right">{pos.quantity:,.0f}</td>
                <td style="text-align:right">{pos.avg_cost:,.2f}</td>
                <td style="text-align:right">{pos.market_price:,.2f}</td>
                <td style="text-align:right">{pos.market_value:,.0f}</td>
                <td style="text-align:right;color:{upnl_color}">{upnl:+,.0f}</td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>Paper 仿真报告 {datetime.now().strftime("%Y%m%d")}</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;background:#0B1120;color:#E0E7FF;padding:32px;max-width:900px;margin:0 auto;}}
h1{{color:#22D3EE;font-size:22px;border-bottom:2px solid #4F46E5;padding-bottom:10px;}}
h2{{color:#818CF8;font-size:15px;margin-top:28px;}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0;}}
.card{{background:#1E1B4B;border-radius:8px;padding:14px;}}
.card .label{{font-size:11px;color:#94A3B8;}}
.card .value{{font-size:20px;font-weight:700;margin-top:4px;font-family:Consolas,monospace;}}
table{{width:100%;border-collapse:collapse;margin-top:8px;}}
th{{background:#1E1B4B;color:#818CF8;padding:8px;text-align:left;font-size:12px;}}
td{{padding:8px;border-bottom:1px solid #334155;font-size:12px;font-family:Consolas,monospace;}}
.meta{{color:#94A3B8;font-size:12px;margin-top:8px;}}
</style></head><body>
<h1>Paper 仿真运行报告</h1>
<div class="meta">模式：{run_mode.value}（{describe(run_mode)}） · 引擎：{status.get("engine_id","")} · 时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>

<h2>运行摘要</h2>
<div class="grid">
  <div class="card"><div class="label">总 Bar</div><div class="value">{stats.get("total_bars",0)}</div></div>
  <div class="card"><div class="label">信号（买/卖）</div><div class="value"><span style="color:#F87171">{stats.get("buy_signals",0)}</span> / <span style="color:#34D399">{stats.get("sell_signals",0)}</span></div></div>
  <div class="card"><div class="label">订单（成交）</div><div class="value">{stats.get("total_orders",0)}（{stats.get("filled_orders",0)}）</div></div>
</div>

<h2>账户概览</h2>
<div class="grid">
  <div class="card"><div class="label">初始资金</div><div class="value">{account.get("initial_capital",0):,.0f}</div></div>
  <div class="card"><div class="label">总资产</div><div class="value">{account.get("total_assets",0):,.0f}</div></div>
  <div class="card"><div class="label">总盈亏</div><div class="value" style="color:{pnl_color}">{pnl_sign}{total_pnl:,.0f}</div></div>
  <div class="card"><div class="label">可用现金</div><div class="value">{account.get("available_cash",0):,.0f}</div></div>
  <div class="card"><div class="label">持仓市值</div><div class="value">{account.get("market_value",0):,.0f}</div></div>
  <div class="card"><div class="label">持仓数</div><div class="value">{account.get("position_count",0)}</div></div>
</div>

<h2>持仓明细（{len(positions)} 只）</h2>
<table>
<tr><th>标的</th><th style="text-align:right">数量</th><th style="text-align:right">成本价</th><th style="text-align:right">现价</th><th style="text-align:right">市值</th><th style="text-align:right">浮动盈亏</th></tr>
{rows if rows else '<tr><td colspan="6" style="text-align:center;color:#64748B">无持仓</td></tr>'}
</table>

<div class="meta" style="margin-top:24px">风控状态：{status.get("risk",{}).get("status","NORMAL")} · 数据来源：本地 processed(tdx) · 本报告由鲲鹏仿真自动生成</div>
</body></html>"""

        out = project_root / "outputs" / "paper_stats" / f"paper_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return str(out)
    except Exception as e:
        logger.warning("生成 HTML 报告失败: %s", e)
        return None


def html_to_pdf(html_path: str, project_root: Path) -> Optional[str]:
    """用系统已有的 Edge headless 把 HTML 转 PDF（零 pip 依赖）。

    Args:
        html_path: HTML 文件路径。
        project_root: 项目根目录（用于定位 Edge 可执行文件）。

    Returns:
        PDF 路径；失败返回 None。
    """
    try:
        import subprocess as _sp
        html_p = Path(html_path)
        pdf_path = str(html_p.with_suffix(".pdf"))
        # 定位 Edge / Chrome
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        browser = next((c for c in candidates if Path(c).exists()), None)
        if browser is None:
            logger.debug("未找到 Edge/Chrome，跳过 PDF 导出")
            return None
        cmd = [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            str(html_p),
        ]
        _sp.run(cmd, capture_output=True, timeout=30, check=False)
        if Path(pdf_path).exists() and Path(pdf_path).stat().st_size > 0:
            return pdf_path
        return None
    except Exception as e:
        logger.debug("HTML 转 PDF 失败: %s", e)
        return None


def write_positions_csv(
    engine: Any,
    project_root: Path,
) -> Optional[str]:
    """运行结束导出持仓明细 CSV（Excel 可直接打开）。

    Args:
        engine: LiveEngine 实例。
        project_root: 项目根目录。

    Returns:
        CSV 路径；失败返回 None。
    """
    try:
        import csv
        portfolio = getattr(engine, "_portfolio", None)
        if portfolio is None:
            return None
        positions = portfolio.get_all_positions()
        out = project_root / "outputs" / "paper_stats" / f"positions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["标的", "数量", "成本价", "现价", "市值", "成本", "浮动盈亏", "已实现盈亏"])
            for pos in positions:
                w.writerow([
                    pos.symbol, pos.quantity, f"{pos.avg_cost:.4f}",
                    f"{pos.market_price:.4f}", f"{pos.market_value:.2f}",
                    f"{pos.cost_value:.2f}", f"{pos.unrealized_pnl:.2f}",
                    f"{pos.realized_pnl:.2f}",
                ])
        return str(out)
    except Exception as e:
        logger.warning("导出持仓 CSV 失败: %s", e)
        return None


# ═══════════════════════════════════════════════
#  信号/成交可视化回调
# ═══════════════════════════════════════════════

def _print_signal(sig: Any) -> None:
    """信号回调（控制台可视化）"""
    try:
        print(f"  📈 [信号] {sig.symbol} {sig.signal_type.value} "
              f"@{sig.price:.2f} reason={sig.reason}")
    except Exception as e:
        logger.debug("信号回调打印失败: %s", e)


def _print_trade(trade: Any) -> None:
    """成交回报回调（控制台可视化）"""
    try:
        print(f"  ✅ [成交] {trade.symbol} {trade.side.value} "
              f"{trade.quantity:.0f}@{trade.price:.2f} 手续费={trade.commission:.2f}")
    except Exception as e:
        logger.debug("成交回调打印失败: %s", e)


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
    try:
        run_mode = apply_cli_overrides(config, args)
    except ValueError as e:
        print(f"[错误] 运行模式参数冲突: {e}")
        return 1

    # ── 1.4 运行模式一致性校验 + 实盘安全门（切割接口）──
    from myquant.live.run_mode import RunMode, check_mode_consistency, describe, validate_live

    consistency_issues = check_mode_consistency(config)
    if consistency_issues:
        print("[错误] 运行模式配置不一致：")
        for issue in consistency_issues:
            print(f"  - {issue}")
        print("       请修正 config/live_config.yaml 的 trade_mode / broker.type / broker.mode 组合。")
        return 1

    if run_mode is RunMode.LIVE:
        live_errors = validate_live(run_mode, config, confirm_live=bool(args.confirm_live))
        if args.dry_run:
            # dry-run 为装配预览：confirm 不足不阻断，仅提示；其余安全项仍拦
            live_errors = [e for e in live_errors if "--confirm-live" not in e]
            if live_errors:
                print("[安全拦截] 无法预览实盘(LIVE)装配：")
                for err in live_errors:
                    print(f"  - {err}")
                return 1
            if not args.confirm_live:
                print("[提示] 正在预览实盘(LIVE)装配；正式实盘（去掉 --dry-run）必须携带 --confirm-live 确认。")
        else:
            if live_errors:
                print("[安全拦截] 无法进入实盘(LIVE)模式：")
                for err in live_errors:
                    print(f"  - {err}")
                print("       实盘需同时满足：--confirm-live + 券商为 qmt/ptrade + 已配置账户。")
                return 1

    # ── 1.5 券商平台校验（疑点3确认：qmt/ptrade 仅 Windows，Linux/Docker 提前拦截）──
    broker_type = str(config.get("broker", {}).get("type", "paper")).lower()
    platform_error = validate_broker_platform(broker_type)
    if platform_error:
        print(f"[错误] {platform_error}")
        return 1

    live_cfg = config.get("live", {}) or {}
    trade_mode = str(live_cfg.get("trade_mode", "paper")).lower()

    # ── 2. 交易时间检查（安全机制：仅约束真钱实盘 LIVE 模式）──
    if not args.force and not args.dry_run and run_mode is RunMode.LIVE:
        if not is_trading_time():
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[安全拦截] 当前时间 {now} 不在A股交易时段内。")
            print("  交易时段：周一至周五 9:30-11:30, 13:00-15:00")
            print("  如需强制启动，请添加 --force 参数。")
            print("  仿真模式(--mode paper/sim)/监控模式(--mode monitor)不受此限制。")
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
    print(f"运行模式：    {describe(run_mode)}")
    print(f"券商适配器：  {args.broker or config.get('broker', {}).get('type', 'paper')}"
          f" / mode={config.get('broker', {}).get('mode', 'SIM')}")
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
    last_live_write = 0.0

    def _request_stop(signum: Any = None, frame: Any = None) -> None:
        print("\n[停止] 收到停止信号，正在优雅停止...")
        stop_event.set()

    try:
        os_signal.signal(os_signal.SIGINT, _request_stop)
        os_signal.signal(os_signal.SIGTERM, _request_stop)
    except (ValueError, OSError) as e:
        logger.debug("信号处理器注册失败（非主线程降级为仅 KeyboardInterrupt）: %s", e)

    try:
        while not stop_event.is_set():
            if args.duration > 0 and (time.time() - start_time) >= args.duration:
                print(f"\n[停止] 达到运行时长 {args.duration}s")
                break
            stop_event.wait(timeout=1.0)
            # 每5秒写 live_status.json（供 GUI 轮询盈亏面板）
            now = time.time()
            if now - last_live_write >= 5.0:
                last_live_write = now
                _write_live_status(engine, project_root)
            # 每60秒打印一次运行摘要
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

    # ── 10.5 绩效留痕：--stats-json 导出引擎统计（供准入评估消费）──
    if args.stats_json:
        stats_path = write_stats_json(engine, run_mode, args.stats_json, project_root)
        if stats_path:
            print(f"[绩效] 引擎统计已导出: {stats_path}")
        else:
            print("[警告] 引擎统计导出失败（详见日志）")
        # 同时导出 HTML 报告 + CSV 持仓明细（人可读）
        html_path = write_report_html(engine, run_mode, project_root)
        if html_path:
            print(f"[报告] HTML 报告已生成: {html_path}")
            pdf_path = html_to_pdf(html_path, project_root)
            if pdf_path:
                print(f"[报告] PDF 报告已导出: {pdf_path}")
        csv_path = write_positions_csv(engine, project_root)
        if csv_path:
            print(f"[报告] 持仓 CSV 已导出: {csv_path}")

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
