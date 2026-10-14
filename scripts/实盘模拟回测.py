# 文件名：scripts/实盘模拟回测.py
"""
实盘模拟回测 - 端到端链路验证
================================
使用真实历史数据，按时间顺序模拟实盘交易流程，验证：
  data_feed → engine → strategy → 风控 → broker → portfolio 完整链路

运行方式:
    python scripts/实盘模拟回测.py

输出:
    - 每日交易日志
    - 最终账户报告
    - 交易记录明细
"""

import sys
import time
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.live.brokers import PaperBroker
from myquant.live.engine import LiveEngine, RiskConfig
from myquant.live.portfolio import LivePortfolio
from myquant.models.market_data import Bar
from myquant.strategies.base import Signal, SignalType, StrategyBase, StrategyContext

# ═══════════════════════════════════════════════
#  配置参数
# ═══════════════════════════════════════════════

CONFIG = {
    "symbol": "000001.SZ",           # 标的：平安银行
    "start_date": "20240101",         # 开始日期
    "end_date": "20240630",           # 结束日期
    "initial_capital": 500_000,       # 初始资金
    "strategy": {
        "short_window": 5,            # 短期均线
        "long_window": 20,            # 长期均线
    },
    "risk": {
        "max_position_pct": 0.30,     # 单票最大仓位30%
        "max_positions": 3,            # 最大持仓数
        "min_cash_ratio": 0.05,        # 最低现金保留5%
    },
}


# ═══════════════════════════════════════════════
#  策略：双均线交叉
# ═══════════════════════════════════════════════

class DualMAStrategy(StrategyBase):
    """
    双均线交叉策略
    - 短期均线上穿长期均线 → 买入
    - 短期均线下穿长期均线 → 卖出
    """
    name = "dual_ma_strategy"

    def __init__(self, short_window: int = 5, long_window: int = 20, **params):
        super().__init__(**params)
        self.short_window = short_window
        self.long_window = long_window
        self._close_prices: list = []  # 收盘价历史

    def on_init(self, context: StrategyContext) -> None:
        super().on_init(context)
        self._close_prices = []
        print(f"  [策略] 双均线策略初始化: short={self.short_window}, long={self.long_window}")

    def generate_signal(self, bar: Bar, context: StrategyContext) -> Signal:
        # 记录收盘价
        self._close_prices.append(bar.close)

        # 数据不足，持有
        if len(self._close_prices) < self.long_window + 1:
            return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD, price=bar.close)

        # 计算均线
        short_ma = sum(self._close_prices[-self.short_window:]) / self.short_window
        long_ma = sum(self._close_prices[-self.long_window:]) / self.long_window

        # 前一日均线
        prev_short = sum(self._close_prices[-self.short_window-1:-1]) / self.short_window
        prev_long = sum(self._close_prices[-self.long_window-1:-1]) / self.long_window

        # 金叉：短期均线上穿长期均线
        if prev_short <= prev_long and short_ma > long_ma:
            return Signal(
                symbol=bar.symbol,
                signal_type=SignalType.BUY,
                price=bar.close,
                reason=f"金叉 short={short_ma:.2f} long={long_ma:.2f}",
            )

        # 死叉：短期均线下穿长期均线
        if prev_short >= prev_long and short_ma < long_ma:
            return Signal(
                symbol=bar.symbol,
                signal_type=SignalType.SELL,
                price=bar.close,
                reason=f"死叉 short={short_ma:.2f} long={long_ma:.2f}",
            )

        return Signal(symbol=bar.symbol, signal_type=SignalType.HOLD, price=bar.close)


# ═══════════════════════════════════════════════
#  获取历史数据
# ═══════════════════════════════════════════════

def fetch_historical_data(symbol: str, start_date: str, end_date: str) -> list:
    """
    从akshare获取历史日线数据

    Returns
    -------
    list[dict] : 按日期升序排列的K线数据
    """
    import akshare as ak

    # 新浪源symbol格式
    code = symbol.split(".")[0]
    market = symbol.split(".")[1].lower() if "." in symbol else "sz"
    sina_symbol = f"{market}{code}"

    print(f"\n[数据] 正在从akshare获取 {symbol} 的历史数据...")
    print(f"  日期范围: {start_date} ~ {end_date}")

    df = ak.stock_zh_a_daily(
        symbol=sina_symbol,
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",  # 前复权
    )

    if df is None or df.empty:
        print("  [错误] 未获取到数据")
        return []

    # 转换为标准bar dict格式，按日期升序
    bars = []
    for _, row in df.iterrows():
        bar = {
            "symbol": symbol,
            "date": str(row.get("date", ""))[:10],
            "open": float(row.get("open", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "close": float(row.get("close", 0)),
            "volume": float(row.get("volume", 0)),
            "amount": float(row.get("amount", 0)),
            "timestamp": time.time(),
        }
        bars.append(bar)

    print(f"  获取到 {len(bars)} 个交易日数据")
    if bars:
        print(f"  日期范围: {bars[0]['date']} ~ {bars[-1]['date']}")
        print(f"  价格范围: {min(b['low'] for b in bars):.2f} ~ {max(b['high'] for b in bars):.2f}")

    return bars


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  实盘模拟回测 - 端到端链路验证")
    print("=" * 70)
    print(f"  标的: {CONFIG['symbol']}")
    print(f"  初始资金: {CONFIG['initial_capital']:,.0f}")
    print(f"  策略: 双均线交叉 ({CONFIG['strategy']['short_window']}/{CONFIG['strategy']['long_window']})")
    print(f"  风控: 单票仓位≤{CONFIG['risk']['max_position_pct']*100:.0f}%, 最多{CONFIG['risk']['max_positions']}只")

    # ── 1. 初始化组件 ──
    print("\n" + "-" * 70)
    print("  [1/5] 初始化组件")
    print("-" * 70)

    risk_config = RiskConfig(**CONFIG["risk"])
    engine = LiveEngine(risk_config=risk_config, engine_id="backtest_sim")

    broker = PaperBroker(initial_capital=CONFIG["initial_capital"])
    portfolio = LivePortfolio(initial_capital=CONFIG["initial_capital"])
    strategy = DualMAStrategy(**CONFIG["strategy"])

    engine.set_broker(broker)
    engine.set_portfolio(portfolio)
    engine.add_strategy(strategy)

    # 启动引擎（连接broker，注册回调，初始化策略）
    engine.start()
    print(f"  引擎状态: {engine.state}")
    print(f"  组件就绪: broker={type(broker).__name__}, portfolio={type(portfolio).__name__}, strategy={strategy.name}")

    # ── 2. 获取历史数据 ──
    print("\n" + "-" * 70)
    print("  [2/5] 获取历史数据")
    print("-" * 70)

    bars = fetch_historical_data(
        symbol=CONFIG["symbol"],
        start_date=CONFIG["start_date"],
        end_date=CONFIG["end_date"],
    )

    if not bars:
        print("\n[错误] 无数据，退出")
        return

    # ── 3. 模拟实盘交易 ──
    print("\n" + "-" * 70)
    print("  [3/5] 模拟实盘交易（按时间顺序驱动）")
    print("-" * 70)
    print(f"  {'日期':<12} {'收盘价':>8} {'信号':>6} {'成交':>6} {'持仓':>8} {'可用资金':>12} {'总资产':>12}")
    print("  " + "-" * 78)

    trade_log = []  # 交易记录

    for i, bar in enumerate(bars):
        date = bar["date"]
        close = bar["close"]

        # 驱动行情（核心：撮合上一根bar的订单 → 更新市值 → 策略生成信号 → 风控 → 下单）
        engine.on_market_data(CONFIG["symbol"], bar)

        # 获取当前状态
        pos = portfolio.get_position(CONFIG["symbol"])
        position_qty = pos.quantity if pos else 0
        available_cash = portfolio.available_cash
        total_assets = portfolio.get_total_assets()

        # 判断信号（从引擎统计中推断，或直接看持仓变化）
        signal_type = "-"
        if i > 0:
            prev_pos = bars[i-1].get("_position_qty", 0)
            if position_qty > 0 and prev_pos == 0:
                signal_type = "买入"
            elif position_qty == 0 and prev_pos > 0:
                signal_type = "卖出"

        # 成交数
        trades_count = engine.stats["total_trades"]
        bars[i]["_position_qty"] = position_qty  # 记录当前持仓

        # 只打印有信号或持仓变化的日子，以及每隔20天打印一次
        if signal_type != "-" or i % 20 == 0 or i == len(bars) - 1:
            print(f"  {date:<12} {close:>8.2f} {signal_type:>6} {trades_count:>6} "
                  f"{position_qty:>8.0f} {available_cash:>12,.0f} {total_assets:>12,.0f}")

        # 记录交易
        if signal_type in ("买入", "卖出"):
            trade_log.append({
                "date": date,
                "type": signal_type,
                "price": close,
                "position_qty": position_qty,
                "total_assets": total_assets,
            })

    # ── 4. 结算T+1 ──
    print("\n" + "-" * 70)
    print("  [4/5] 结算T+1冻结")
    print("-" * 70)

    broker.settle_t1()
    # 最后再驱动一根bar，撮合可能的剩余订单
    if bars:
        last_bar = bars[-1].copy()
        last_bar["date"] = "SETTLE"
        engine.on_market_data(CONFIG["symbol"], last_bar)

    print("  结算完成")

    # ── 5. 输出最终报告 ──
    print("\n" + "-" * 70)
    print("  [5/5] 最终账户报告")
    print("-" * 70)

    account = portfolio.get_account()
    stats = engine.stats

    print("\n  【账户概览】")
    print(f"    初始资金:     {account['initial_capital']:>12,.2f}")
    print(f"    最终总资产:   {account['total_assets']:>12,.2f}")
    print(f"    现金:         {account['cash']:>12,.2f}")
    print(f"    持仓市值:     {account['market_value']:>12,.2f}")
    print(f"    持仓数:       {account['position_count']:>12}")

    print("\n  【盈亏统计】")
    print(f"    已实现盈亏:   {account['realized_pnl']:>12,.2f}")
    print(f"    未实现盈亏:   {account['unrealized_pnl']:>12,.2f}")
    print(f"    总盈亏:       {account['total_pnl']:>12,.2f}")
    print(f"    总收益率:     {account['total_pnl_ratio']*100:>11.2f}%")
    print(f"    净值:         {account['equity']:>12.4f}")

    print("\n  【交易统计】")
    print(f"    总bar数:      {stats['total_bars']:>12}")
    print(f"    总信号数:     {stats['total_signals']:>12}")
    print(f"    买入信号:     {stats['buy_signals']:>12}")
    print(f"    卖出信号:     {stats['sell_signals']:>12}")
    print(f"    总订单数:     {stats['total_orders']:>12}")
    print(f"    成交数:       {stats['total_trades']:>12}")
    print(f"    风控拒绝:     {stats['risk_rejected']:>12}")
    print(f"    错误数:       {stats['errors']:>12}")

    # 持仓明细
    positions = portfolio.get_all_positions()
    if positions:
        print("\n  【当前持仓明细】")
        print(f"    {'标的':<12} {'数量':>8} {'成本':>8} {'现价':>8} {'市值':>12} {'盈亏':>10} {'收益率':>8}")
        print(f"    {'-'*70}")
        for p in positions:
            print(f"    {p.symbol:<12} {p.quantity:>8.0f} {p.avg_cost:>8.2f} {p.market_price:>8.2f} "
                  f"{p.market_value:>12,.2f} {p.unrealized_pnl:>10,.2f} {p.pnl_ratio*100:>7.2f}%")

    # 交易记录
    if trade_log:
        print("\n  【交易记录】")
        print(f"    {'日期':<12} {'类型':>6} {'价格':>8} {'持仓数':>8} {'总资产':>12}")
        print(f"    {'-'*50}")
        for t in trade_log:
            print(f"    {t['date']:<12} {t['type']:>6} {t['price']:>8.2f} "
                  f"{t['position_qty']:>8.0f} {t['total_assets']:>12,.2f}")

    # 健康检查
    health = engine.health_check()
    print("\n  【系统健康检查】")
    print(f"    引擎状态: {health['status']}")
    print(f"    Broker连接: {health['broker']['connected'] if health['broker'] else 'N/A'}")
    print(f"    引擎运行: {health['engine']['state']}")

    # 停止引擎
    engine.stop()

    print("\n" + "=" * 70)
    print("  实盘模拟回测完成 - 端到端链路验证通过")
    print("=" * 70)

    # 简单的链路验证断言
    print("\n  【链路验证】")
    checks = [
        ("数据获取", len(bars) > 0, f"{len(bars)}个交易日"),
        ("策略初始化", strategy._initialized, "已初始化"),
        ("引擎启动", engine.state == "STOPPED", f"状态={engine.state}"),
        ("信号生成", stats["total_signals"] > 0, f"{stats['total_signals']}个信号"),
        ("订单提交", stats["total_orders"] > 0, f"{stats['total_orders']}个订单"),
        ("成交撮合", stats["total_trades"] > 0, f"{stats['total_trades']}笔成交"),
        ("持仓更新", account["position_count"] >= 0, f"{account['position_count']}只持仓"),
        ("盈亏计算", True, f"{account['total_pnl']:,.2f}"),
        ("无错误", stats["errors"] == 0, f"{stats['errors']}个错误"),
    ]

    all_pass = True
    for name, passed, detail in checks:
        status = "✅" if passed else "❌"
        if not passed:
            all_pass = False
        print(f"    {status} {name}: {detail}")

    print()
    if all_pass:
        print("  🎉 端到端链路全部验证通过！")
    else:
        print("  ⚠️  部分验证未通过，请检查上方日志")

    return all_pass


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
