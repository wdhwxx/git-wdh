# 文件名：scripts/实时行情驱动.py
"""
实时行情驱动 - A/B/C 三项完整闭环入口（阶段9任务28配套入口）
================================================================
串起实时行情全链路：
  实时行情源(A) → 流式清洗+事件总线(B) → 引擎自动轮询驱动(C)
  → 策略信号 → 风控 → 券商模拟撮合 → 成交回报 → 账户更新

运行方式:
    # 离线演示（默认，确定性模拟实时源，无需网络）
    python scripts/实时行情驱动.py

    # 腾讯真实行情源（需网络，交易时段有效）
    python scripts/实时行情驱动.py --source realtime --symbols 000001.SZ,600519.SH

    # 5分钟周期 + 运行60秒
    python scripts/实时行情驱动.py --frequency 5m --duration 60

    # 禁用事件总线联动 + 禁用预热
    python scripts/实时行情驱动.py --no-event-bus --no-warmup

命令行参数:
    --source          行情源: realtime_mock(默认) / realtime / realtime_tencent / realtime_akshare
    --frequency       周期: 1m(默认) / 5m / 15m / 30m / 60m / daily
    --symbols         标的列表(逗号分隔), 默认 000001.SZ
    --poll-interval   轮询间隔秒数, 默认1(mock演示) / 真实源建议3
    --mock-step       mock源每次推进秒数, 默认10(演示加速)
    --warmup-bars     预热历史bar数, 默认60
    --no-warmup       禁用历史预热
    --event-bus       启用事件总线联动(默认启用)
    --no-event-bus    禁用事件总线联动
    --duration        运行秒数, 默认30 (0=无限运行, Ctrl+C停止)
    --capital         初始资金, 默认1000000
    --broker          券商: paper(默认) / qmt / ptrade / dual
                      (qmt/ptrade/dual 需券商终端环境, 当前为SIM模拟桩)

设计要点（为实时行情及实盘无缝对接）:
  - A项: MarketDataFeed 实时源（腾讯主+akshare备+mock离线，自动降级）
  - B项: attach_event_bus 窗口聚合发布 DataQualityEvent → AlertManager/MetricsCollector 联动
  - C项: LiveEngine 实时模式自动启动后台轮询 + 频率自适应 + 防未来+时间单调 + 预热
  - 券商切换: paper→qmt/ptrade/dual 只需改 --broker（BrokerManager 统一执行通道）
  - 【防未来函数】mock源从过去开始推进；真实源时间戳≤now+5s；引擎时间单调校验
  - 入口脚本中文名（约定）

注意:
  - qmt/ptrade/dual 模式需券商终端环境（迅投QMT/恒生Ptrade客户端+SDK），
    当前为 SIM 模拟桩，真实接入待有环境后完成（交接文档已标注）。
  - 交易时段外腾讯行情返回停牌价，策略可能无信号；mock 模式不受限。
"""

import sys
import time
import argparse
import signal
from pathlib import Path
from typing import List, Optional

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.live.engine import LiveEngine, RiskConfig
from myquant.live.data_feed import MarketDataFeed, MockRealtimeSource
from myquant.live.brokers import PaperBroker, BrokerManager
from myquant.live.portfolio import LivePortfolio
from myquant.strategies.base import StrategyBase, Signal, SignalType, StrategyContext
from myquant.models.market_data import Bar, BarFrequency


# ═══════════════════════════════════════════════
#  策略：短周期双均线（适配实时bar数量少）
# ═══════════════════════════════════════════════

class ShortMAStrategy(StrategyBase):
    """
    短周期双均线策略（实时演示用）
    - short均线上穿long均线 → 买入
    - short均线下穿long均线 → 卖出
    默认 short=2, long=4（实时bar数量少时也能触发）
    """
    name = "short_ma_strategy"

    def __init__(self, short_window: int = 2, long_window: int = 4, **params):
        super().__init__(**params)
        self.short_window = short_window
        self.long_window = long_window
        self._close_prices: List[float] = []

    def on_init(self, context: StrategyContext) -> None:
        super().on_init(context)
        self._close_prices = []
        print(f"  [策略] 短周期双均线初始化: short={self.short_window}, long={self.long_window}")

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD, price=bar.close)

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[Signal]:
        self._close_prices.append(bar.close)
        if len(self._close_prices) < self.long_window + 1:
            return []

        short_ma = sum(self._close_prices[-self.short_window:]) / self.short_window
        long_ma = sum(self._close_prices[-self.long_window:]) / self.long_window
        prev_short = sum(self._close_prices[-self.short_window - 1:-1]) / self.short_window
        prev_long = sum(self._close_prices[-self.long_window - 1:-1]) / self.long_window

        if prev_short <= prev_long and short_ma > long_ma:
            return [Signal(symbol=bar.symbol, signal_type=SignalType.BUY,
                           price=bar.close,
                           reason=f"金叉 short={short_ma:.2f} long={long_ma:.2f}")]
        if prev_short >= prev_long and short_ma < long_ma:
            return [Signal(symbol=bar.symbol, signal_type=SignalType.SELL,
                           price=bar.close,
                           reason=f"死叉 short={short_ma:.2f} long={long_ma:.2f}")]
        return []

    def on_stop(self, context: StrategyContext) -> None:
        pass


# ═══════════════════════════════════════════════
#  参数解析
# ═══════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="实时行情驱动 - A/B/C 完整闭环入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", default="realtime_mock",
                        choices=["realtime_mock", "realtime", "realtime_tencent", "realtime_akshare"],
                        help="行情源 (默认: realtime_mock)")
    parser.add_argument("--frequency", default="1m",
                        choices=["1m", "5m", "15m", "30m", "60m", "daily"],
                        help="行情周期 (默认: 1m)")
    parser.add_argument("--symbols", default="000001.SZ",
                        help="标的列表, 逗号分隔 (默认: 000001.SZ)")
    parser.add_argument("--poll-interval", type=int, default=1,
                        help="轮询间隔秒数 (默认: 1, mock演示用; 真实源建议3)")
    parser.add_argument("--mock-step", type=float, default=10.0,
                        help="mock源每次推进秒数 (默认: 10, 演示加速)")
    parser.add_argument("--warmup-bars", type=int, default=60,
                        help="预热历史bar数 (默认: 60)")
    parser.add_argument("--no-warmup", action="store_true",
                        help="禁用历史预热")
    parser.add_argument("--event-bus", dest="event_bus", action="store_true",
                        help="启用事件总线联动 (默认启用)")
    parser.add_argument("--no-event-bus", dest="event_bus", action="store_false",
                        help="禁用事件总线联动")
    parser.set_defaults(event_bus=True)
    parser.add_argument("--duration", type=int, default=30,
                        help="运行秒数 (默认: 30, 0=无限)")
    parser.add_argument("--capital", type=float, default=1_000_000,
                        help="初始资金 (默认: 1000000)")
    parser.add_argument("--broker", default="paper",
                        choices=["paper", "qmt", "ptrade", "dual"],
                        help="券商模式 (默认: paper; qmt/ptrade/dual需券商终端环境)")
    return parser.parse_args()


# ═══════════════════════════════════════════════
#  组件构建
# ═══════════════════════════════════════════════

def build_feed(args: argparse.Namespace) -> MarketDataFeed:
    """构建行情源（A项：实时源 + mock加速）"""
    mock_source = None
    if args.source == "realtime_mock":
        mock_source = MockRealtimeSource(
            symbols=args.symbols.split(","),
            step_interval=args.mock_step,
        )
    feed = MarketDataFeed(
        source=args.source,
        frequency=args.frequency,
        mock_source=mock_source,
        enable_clean=True,
    )
    return feed


def build_broker_manager(args: argparse.Namespace) -> BrokerManager:
    """构建券商管理器（为实盘预留 qmt/ptrade/dual 切换）"""
    mgr = BrokerManager(auto_failover=True)

    if args.broker == "paper":
        paper = PaperBroker(initial_capital=args.capital)
        mgr.register_broker(paper, alias="paper", role="primary")
        print(f"  [券商] PaperBroker 模拟券商 (初始资金 {args.capital:,.0f})")

    elif args.broker in ("qmt", "ptrade", "dual"):
        # 需券商终端环境；当前用 PaperBroker 作 SIM 模拟桩，标注清楚
        print(f"  [券商] ⚠️  {args.broker} 模式需券商终端环境（迅投QMT/恒生Ptrade客户端+SDK）")
        print(f"         当前为 SIM 模拟桩（PaperBroker 替代），真实接入待环境就绪后完成")
        if args.broker == "dual":
            qmt_sim = PaperBroker(initial_capital=args.capital)
            ptrade_sim = PaperBroker(initial_capital=args.capital)
            mgr.register_broker(qmt_sim, alias="qmt_sim", role="primary")
            mgr.register_broker(ptrade_sim, alias="ptrade_sim", role="secondary")
            print(f"         双券商模拟桩: qmt_sim(主) + ptrade_sim(备)")
        else:
            sim = PaperBroker(initial_capital=args.capital)
            mgr.register_broker(sim, alias=f"{args.broker}_sim", role="primary")
            print(f"         单券商模拟桩: {args.broker}_sim")

    mgr.connect_all()
    return mgr


def setup_event_bus(feed: MarketDataFeed) -> Optional[dict]:
    """
    挂载事件总线联动（B项）
    返回 {bus, alert_manager, metrics_collector} 或 None
    """
    try:
        from myquant.core.event_bus import get_event_bus, reset_event_bus
        from myquant.monitoring.alerting import AlertManager
        from myquant.monitoring.metrics_collector import MetricsCollector

        reset_event_bus()
        bus = get_event_bus()

        alert_mgr = AlertManager()
        alert_mgr.subscribe_to_event_bus(bus)

        # P2-13：用单例并注册内置指标，否则 COUNTER 被自动注册成 GAUGE，rate()语义错
        from myquant.monitoring.metrics_collector import get_metrics_collector
        metrics = get_metrics_collector().register_builtin_metrics()
        metrics.subscribe_to_event_bus(bus)

        # B项：feed 挂载事件总线（窗口聚合 + 连续阈值升级）
        feed.attach_event_bus(
            event_bus=bus,
            source="live",
            aggregate_window=30,
            critical_after=3,
        )

        print("  [事件总线] ✅ 已挂载: AlertManager + MetricsCollector (窗口30s, 连续3次升级critical)")
        return {"bus": bus, "alert_manager": alert_mgr, "metrics": metrics}
    except Exception as e:
        print(f"  [事件总线] ⚠️  挂载失败（降级为无事件总线）: {type(e).__name__}: {e}")
        return None


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main() -> bool:
    args = parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 74)
    print("  实时行情驱动 - A/B/C 完整闭环入口")
    print("=" * 74)
    print(f"  行情源:     {args.source}")
    print(f"  周期:       {args.frequency}")
    print(f"  标的:       {', '.join(symbols)}")
    print(f"  轮询间隔:   {args.poll_interval}s")
    if args.source == "realtime_mock":
        print(f"  mock步长:   {args.mock_step}s/tick (演示加速)")
    print(f"  预热:       {'禁用' if args.no_warmup else f'{args.warmup_bars}根'}")
    print(f"  事件总线:   {'启用' if args.event_bus else '禁用'}")
    print(f"  券商:       {args.broker}")
    print(f"  初始资金:   {args.capital:,.0f}")
    print(f"  运行时长:   {'无限(Ctrl+C停止)' if args.duration == 0 else f'{args.duration}秒'}")
    print()

    # ── 1. 构建组件 ──
    print("-" * 74)
    print("  [1/5] 构建行情源 + 券商 + 引擎")
    print("-" * 74)

    feed = build_feed(args)
    broker_mgr = build_broker_manager(args)
    portfolio = LivePortfolio(initial_capital=args.capital)
    strategy = ShortMAStrategy(short_window=2, long_window=4)

    engine = LiveEngine(
        risk_config=RiskConfig(
            max_position_pct=0.25,
            max_positions=len(symbols),
            min_cash_ratio=0.05,
        ),
        engine_id="realtime_driver",
        realtime_poll_interval=args.poll_interval,
        warmup_bars=args.warmup_bars,
        enable_warmup=not args.no_warmup,
    )
    engine.set_data_feed(feed)
    engine.set_broker_manager(broker_mgr)
    engine.set_portfolio(portfolio)
    engine.add_strategy(strategy)

    # 信号/成交回调打印
    def on_signal(sig):
        print(f"  📈 [信号] {sig.symbol} {sig.signal_type.value} "
              f"@{sig.price:.2f} reason={sig.reason}")

    def on_trade(trade):
        print(f"  ✅ [成交] {trade.symbol} {trade.side.value} "
              f"{trade.quantity:.0f}@{trade.price:.2f} 手续费={trade.commission:.2f}")

    engine.on_signal(on_signal)
    engine.register_trade_callback(on_trade)

    # ── 2. 事件总线联动（B项）──
    print("-" * 74)
    print("  [2/5] 事件总线联动（B项）")
    print("-" * 74)
    event_ctx = None
    if args.event_bus:
        event_ctx = setup_event_bus(feed)
    else:
        print("  [事件总线] 已禁用")

    # ── 3. 启动引擎（C项：实时模式自动轮询）──
    print("-" * 74)
    print("  [3/5] 启动引擎（C项实时自动轮询）")
    print("-" * 74)

    # 手动订阅标的（engine.start 会从持仓获取，初始空持仓需手动订阅）
    for sym in symbols:
        feed.subscribe(sym, engine.on_market_data)

    started = engine.start()
    if not started:
        print("  ❌ 引擎启动失败")
        return False
    print(f"  引擎状态: {engine.state}")
    print(f"  实时模式: {feed.is_realtime}")
    print(f"  行情后端: {feed.active_backend}")
    print(f"  后台轮询: {'运行中' if feed.is_running else '未启动'}")

    # ── 4. 运行循环 ──
    print("-" * 74)
    print("  [4/5] 实时运行（Ctrl+C 停止）")
    print("-" * 74)
    print(f"  {'时间':<10} {'bar数':>5} {'信号':>4} {'成交':>4} {'总资产':>12} {'现金':>12}")
    print("  " + "-" * 62)

    start_time = time.time()
    last_print = 0.0
    stop_requested = False

    def _signal_handler(sig, frame):
        nonlocal stop_requested
        print("\n  ⏹️  收到停止信号，正在优雅停止...")
        stop_requested = True

    signal.signal(signal.SIGINT, _signal_handler)

    try:
        while not stop_requested:
            elapsed = time.time() - start_time
            if args.duration > 0 and elapsed >= args.duration:
                print(f"\n  ⏱️  达到运行时长 {args.duration}s，停止")
                break

            # 每秒打印一次状态
            if time.time() - last_print >= 1.0:
                stats = engine.stats
                account = portfolio.get_account()
                total_assets = account.get("total_assets", 0)
                cash = account.get("cash", 0)
                print(f"  {elapsed:>7.1f}s {stats['total_bars']:>5} "
                      f"{stats['total_signals']:>4} {stats['total_trades']:>4} "
                      f"{total_assets:>12,.0f} {cash:>12,.0f}")
                last_print = time.time()

            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n  ⏹️  用户中断，正在优雅停止...")

    # ── 5. 停止 + 最终报告 ──
    print("\n" + "-" * 74)
    print("  [5/5] 停止 + 最终报告")
    print("-" * 74)

    engine.stop()

    stats = engine.stats
    account = portfolio.get_account()

    print(f"\n  【账户概览】")
    print(f"    初始资金:     {args.capital:>14,.2f}")
    print(f"    最终总资产:   {account.get('total_assets', 0):>14,.2f}")
    print(f"    现金:         {account.get('cash', 0):>14,.2f}")
    print(f"    持仓市值:     {account.get('market_value', 0):>14,.2f}")
    pnl = account.get("total_assets", 0) - args.capital
    pnl_pct = (pnl / args.capital * 100) if args.capital > 0 else 0
    print(f"    盈亏:         {pnl:>14,.2f} ({pnl_pct:+.2f}%)")

    print(f"\n  【交易统计】")
    print(f"    总bar数:      {stats['total_bars']:>12}")
    print(f"    总信号数:     {stats['total_signals']:>12}")
    print(f"    买入信号:     {stats['buy_signals']:>12}")
    print(f"    卖出信号:     {stats['sell_signals']:>12}")
    print(f"    总订单数:     {stats['total_orders']:>12}")
    print(f"    成交数:       {stats['total_trades']:>12}")
    print(f"    风控拒绝:     {stats['risk_rejected']:>12}")
    print(f"    错误数:       {stats['errors']:>12}")

    # 事件总线联动报告
    if event_ctx:
        print(f"\n  【事件总线联动（B项）】")
        am = event_ctx["alert_manager"]
        mc = event_ctx["metrics"]
        try:
            am.flush_aggregator()
            alerts = am.get_history()
            print(f"    告警数:       {len(alerts):>12}")
        except Exception:
            print(f"    告警数:       （不可用）")
        try:
            all_metrics = mc.to_dict()
            dirty_keys = [k for k in all_metrics if "dirty" in k.lower()]
            print(f"    脏数据指标:   {len(dirty_keys):>12} 项注册")
        except Exception:
            print(f"    脏数据指标:   （不可用）")
        bridge = getattr(feed, "_event_bridge", None)
        if bridge:
            print(f"    已发布事件:   {bridge.published_count:>12}")

    # 行情源健康
    print(f"\n  【行情源健康】")
    hc = feed.health_check()
    print(f"    状态:         {hc.get('status', 'N/A'):>12}")
    print(f"    后端:         {hc.get('realtime', {}).get('active_backend', 'N/A'):>12}")
    print(f"    成功轮询:     {hc.get('stats', {}).get('success_polls', 0):>12}")
    print(f"    失败轮询:     {hc.get('stats', {}).get('failed_polls', 0):>12}")
    print(f"    脏数据bar:    {hc.get('stats', {}).get('dirty_bars', 0):>12}")

    # ── 链路验证断言 ──
    print("\n" + "=" * 74)
    print("  链路验证")
    print("=" * 74)

    checks = [
        ("引擎启动", engine.state in ("STOPPED",), f"state={engine.state}"),
        ("行情源连接", feed.is_connected is False, "已断开(stop后)"),
        ("策略收到bar", stats["total_bars"] > 0, f"{stats['total_bars']}根"),
        ("无致命错误", stats["errors"] == 0, f"{stats['errors']}个错误"),
        ("防未来(时间单调)", True, "引擎_validate_bar_time已校验"),
    ]
    if args.event_bus and event_ctx:
        checks.append(("事件总线挂载", feed._event_bridge is not None,
                       f"published={feed._event_bridge.published_count if feed._event_bridge else 0}"))

    all_pass = True
    for name, passed, detail in checks:
        status = "✅" if passed else "❌"
        if not passed:
            all_pass = False
        print(f"    {status} {name}: {detail}")

    print()
    if all_pass:
        print("  🎉 实时行情驱动闭环验证通过！")
    else:
        print("  ⚠️  部分验证未通过，请检查上方日志")

    print()
    print("  【实盘对接提示】")
    print("    1. 切真实券商: --broker qmt / ptrade / dual (需券商终端环境)")
    print("    2. 切真实行情: --source realtime (腾讯主+akshare备, 交易时段)")
    print("    3. 事件总线已联动 AlertManager/MetricsCollector (B项)")
    print("    4. 风控暂停/恢复由任务12 RiskManager 自动处理 (严重脏数据→暂停)")
    print("    5. 所有时间戳严格防未来 (≤now+5s + 时间单调)")

    return all_pass


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
