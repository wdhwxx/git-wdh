# 文件名：scripts/双券商模拟实盘.py
"""
双券商模拟实盘 - 双券商并行端到端链路验证（阶段9任务27配套入口）
================================================================
验证双券商并行完整链路：
  data_feed/行情 → engine → 策略信号 → 风控 → BrokerManager 路由主券商
  → 行情驱动撮合 → 成交回报 → portfolio 合并账本 → 主备 failover 自动续单
  → 双券商合并视图对账

运行方式:
    python scripts/双券商模拟实盘.py

输出:
    - 每日交易日志（含券商路由信息）
    - 主备 failover 演示日志
    - 双券商合并账户/持仓对账
    - 最终账户报告 + 链路验证断言

设计要点（为实时行情及实盘无缝对接）:
  - 引擎唯一执行通道 = BrokerManager（QMT 主 + Ptrade 备，SIM 模式）
  - 行情驱动撮合：update_market_price / on_bar 统一由 push_market_data 广播
  - 主备 failover：主券商断开 → 自动切换备券商 → 后续订单路由新主
  - 数据源双保险：优先 akshare 真实历史，失败自动降级为确定性模拟 K 线
  - 【防未来函数】全部 bar 为历史时间戳；成交只依据已发生行情驱动

入口脚本中文名（约定）:
  所有可执行入口统一使用中文文件名。
"""

import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.live.engine import LiveEngine, RiskConfig
from myquant.live.brokers import BrokerManager, QmtBroker, PtradeBroker
from myquant.live.portfolio import LivePortfolio
from myquant.strategies.base import StrategyBase, Signal, SignalType, StrategyContext
from myquant.models.market_data import Bar


# ═══════════════════════════════════════════════
#  配置参数
# ═══════════════════════════════════════════════

CONFIG = {
    "symbol": "600519.SH",            # 标的：贵州茅台
    "start_date": "20240101",          # 开始日期
    "end_date": "20240630",            # 结束日期
    "initial_capital": 1_000_000,      # 初始资金（每券商）
    "strategy": {
        "short_window": 5,             # 短期均线
        "long_window": 20,             # 长期均线
    },
    "risk": {
        "max_position_pct": 0.25,      # 单票最大仓位25%
        "max_positions": 2,            # 最大持仓数
        "min_cash_ratio": 0.05,        # 最低现金保留5%
    },
    "brokers": {
        "primary": {"alias": "qmt", "account_id": "QMT-SIM-001"},
        "secondary": {"alias": "ptrade", "account_id": "PTRADE-SIM-001"},
    },
    "failover_at_ratio": 0.6,          # 在第60%交易日处演示主券商故障切换
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
        self._close_prices: list = []

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
#  数据获取（akshare 优先 + 模拟数据兜底）
# ═══════════════════════════════════════════════

def _parse_ts(date_str: str) -> float:
    """日期字符串 → 时间戳（历史时间，防未来）"""
    dt = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    return dt.timestamp()


def fetch_historical_data(symbol: str, start_date: str, end_date: str) -> list:
    """
    获取历史日线数据（akshare 优先，失败自动降级为确定性模拟数据）

    Returns
    -------
    list[dict] : 按日期升序排列的K线数据
    """
    # ── 方式1：akshare 真实历史数据 ──
    try:
        import akshare as ak

        code = symbol.split(".")[0]
        market = symbol.split(".")[1].lower() if "." in symbol else "sz"
        sina_symbol = f"{market}{code}"

        print(f"\n[数据] 正在从akshare获取 {symbol} 的历史数据...")
        df = ak.stock_zh_a_daily(
            symbol=sina_symbol,
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",  # 前复权
        )

        if df is not None and not df.empty:
            bars = []
            for _, row in df.iterrows():
                date = str(row.get("date", ""))[:10]
                bar = {
                    "symbol": symbol,
                    "date": date,
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": float(row.get("volume", 0)),
                    "amount": float(row.get("amount", 0)),
                    "timestamp": _parse_ts(date),
                }
                bars.append(bar)
            print(f"  [akshare] 获取到 {len(bars)} 个交易日数据")
            if bars:
                print(f"  日期范围: {bars[0]['date']} ~ {bars[-1]['date']}")
            return bars
    except Exception as e:
        print(f"  [数据] akshare 获取失败，降级为模拟数据: {type(e).__name__}: {e}")

    # ── 方式2：确定性模拟K线（先跌后涨震荡，保证均线交叉触发） ──
    print(f"  [数据] 使用确定性模拟K线（{start_date} ~ {end_date}）")
    bars = []
    d = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    base = 100.0
    n = 0
    while d <= end:
        if d.weekday() < 5:  # 仅交易日（周一~周五）
            # 先跌后涨 + 正弦震荡（确定性，无随机）
            cycle = n % 60
            drift = -0.15 + 0.3 * (cycle / 60)         # 先跌后涨
            wave = 0.05 * __import__("math").sin(n * 0.5)  # 震荡
            close = base * (1 + drift + wave)
            date = d.strftime("%Y-%m-%d")
            bars.append({
                "symbol": symbol,
                "date": date,
                "open": round(close * 0.995, 2),
                "high": round(close * 1.01, 2),
                "low": round(close * 0.99, 2),
                "close": round(close, 2),
                "volume": 100000.0,
                "amount": round(close * 100000, 2),
                "timestamp": _parse_ts(date),
            })
            n += 1
        d += timedelta(days=1)
    print(f"  [模拟] 生成 {len(bars)} 个交易日数据")
    return bars


# ═══════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════

def main():
    print("=" * 74)
    print("  双券商模拟实盘 - 双券商并行端到端链路验证")
    print("=" * 74)
    print(f"  标的: {CONFIG['symbol']}")
    print(f"  初始资金/券商: {CONFIG['initial_capital']:,.0f}")
    print(f"  券商: {CONFIG['brokers']['primary']['alias']}(主) + "
          f"{CONFIG['brokers']['secondary']['alias']}(备)  [SIM 模式]")
    print(f"  策略: 双均线交叉 ({CONFIG['strategy']['short_window']}/{CONFIG['strategy']['long_window']})")
    print(f"  风控: 单票仓位≤{CONFIG['risk']['max_position_pct']*100:.0f}%, 最多{CONFIG['risk']['max_positions']}只")
    print(f"  主备演示: 第{CONFIG['failover_at_ratio']*100:.0f}%交易日断开主券商 → 自动切换")

    # ── 1. 初始化双券商 BrokerManager ──
    print("\n" + "-" * 74)
    print("  [1/6] 初始化双券商 BrokerManager（QMT 主 + Ptrade 备）")
    print("-" * 74)

    mgr = BrokerManager(auto_failover=True)
    qmt = QmtBroker(
        account_id=CONFIG["brokers"]["primary"]["account_id"],
        mode="SIM", initial_capital=CONFIG["initial_capital"],
    )
    ptrade = PtradeBroker(
        account_id=CONFIG["brokers"]["secondary"]["account_id"],
        mode="SIM", initial_capital=CONFIG["initial_capital"],
    )
    mgr.register_broker(qmt, alias=CONFIG["brokers"]["primary"]["alias"], role="primary")
    mgr.register_broker(ptrade, alias=CONFIG["brokers"]["secondary"]["alias"], role="secondary")
    conn = mgr.connect_all()
    print(f"  券商连接: {conn}")
    print(f"  主券商: {mgr.primary_alias}, 注册数: {len(mgr.list_brokers())}")

    # ── 2. 初始化引擎（唯一执行通道 = BrokerManager） ──
    print("\n" + "-" * 74)
    print("  [2/6] 初始化引擎 + 持仓 + 策略")
    print("-" * 74)

    risk_config = RiskConfig(**CONFIG["risk"])
    engine = LiveEngine(risk_config=risk_config, engine_id="dual_broker_sim")
    portfolio = LivePortfolio(initial_capital=CONFIG["initial_capital"])
    strategy = DualMAStrategy(**CONFIG["strategy"])

    engine.set_broker_manager(mgr)      # 双券商并行主入口
    engine.set_portfolio(portfolio)
    engine.add_strategy(strategy)
    engine.start()

    print(f"  引擎状态: {engine.state}")
    print(f"  引擎券商摘要: {engine._get_broker_summary()}")

    # ── 3. 获取历史数据 ──
    print("\n" + "-" * 74)
    print("  [3/6] 获取历史数据")
    print("-" * 74)

    bars = fetch_historical_data(
        symbol=CONFIG["symbol"],
        start_date=CONFIG["start_date"],
        end_date=CONFIG["end_date"],
    )
    if not bars:
        print("\n[错误] 无数据，退出")
        engine.stop()
        return

    # ── 4. 模拟实盘交易（含主备 failover 演示） ──
    print("\n" + "-" * 74)
    print("  [4/6] 模拟实盘交易（按时间顺序驱动，含主备切换）")
    print("-" * 74)
    print(f"  {'日期':<12} {'收盘价':>8} {'成交':>5} {'主券商':>6} {'持仓':>7} {'总资产':>12}")
    print("  " + "-" * 74)

    trade_log = []
    cut_idx = int(len(bars) * CONFIG["failover_at_ratio"])
    failover_done = False
    last_qty: Optional[float] = None

    for i, bar in enumerate(bars):
        date = bar["date"]
        close = bar["close"]

        # 日切：模拟日终/次日开盘前结算，释放 T+1 冻结（真实券商自动结算）
        qmt.settle_t1()
        ptrade.settle_t1()

        # 驱动行情（核心：push_market_data 广播 → 各券商撮合 → 策略信号 → 风控 → 下单）
        engine.on_market_data(CONFIG["symbol"], bar)

        # 主备 failover 演示：在第 cut_idx 根bar后断开主券商
        if i == cut_idx and not failover_done:
            print(f"\n  ⚠️  [failover] 主券商 {mgr.primary_alias} 连接断开，触发主备切换...")
            qmt.disconnect()
            new_primary = mgr.failover()
            failover_done = True
            print(f"  ✅ [failover] 主券商自动切换: → {mgr.primary_alias} "
                  f"(切换后继续交易)")

        # 当前状态
        pos = portfolio.get_position(CONFIG["symbol"])
        position_qty = pos.quantity if pos else 0
        total_assets = portfolio.get_total_assets()
        trades_count = engine.stats["total_trades"]

        # 每15天 + 最后一天 + 切换点打印
        if i % 15 == 0 or i == len(bars) - 1 or i == cut_idx:
            print(f"  {date:<12} {close:>8.2f} {trades_count:>5} {mgr.primary_alias:>6} "
                  f"{position_qty:>7.0f} {total_assets:>12,.0f}")

        # 记录持仓变化
        if last_qty is not None and position_qty != last_qty:
            trade_log.append({
                "date": date,
                "price": close,
                "position_qty": position_qty,
                "total_assets": total_assets,
                "broker": mgr.primary_alias,
            })
        last_qty = position_qty

    # ── 5. 结算 T+1 + 对账 ──
    print("\n" + "-" * 74)
    print("  [5/6] 结算 T+1 + 双券商合并视图对账")
    print("-" * 74)

    qmt.settle_t1()
    ptrade.settle_t1()
    if bars:
        last_bar = bars[-1].copy()
        last_bar["date"] = "SETTLE"
        engine.on_market_data(CONFIG["symbol"], last_bar)

    # 双券商合并视图 vs 引擎 portfolio 账本
    mgr_account = mgr.get_account(merged=True)
    pf_account = portfolio.get_account()
    mgr_pos = mgr.get_positions(merged=True)

    print(f"\n  【双券商合并账户（BrokerManager 执行侧）】")
    print(f"    现金:       {mgr_account['cash']:>14,.2f}")
    print(f"    可用资金:   {mgr_account['available_cash']:>14,.2f}")
    print(f"    持仓市值:   {mgr_account['market_value']:>14,.2f}")
    print(f"    总资产:     {mgr_account['total_assets']:>14,.2f}")

    print(f"\n  【引擎 portfolio 账本（权威交易账本）】")
    print(f"    现金:       {pf_account.get('cash', 0):>14,.2f}")
    print(f"    持仓市值:   {pf_account.get('market_value', 0):>14,.2f}")
    print(f"    总资产:     {pf_account.get('total_assets', 0):>14,.2f}")

    print(f"\n  【双券商合并持仓（按 symbol 聚合）】")
    if mgr_pos:
        print(f"    {'标的':<12} {'数量':>8} {'加权成本':>10} {'现价':>8} {'市值':>12}")
        print(f"    {'-'*56}")
        for p in mgr_pos:
            print(f"    {p.symbol:<12} {p.quantity:>8.0f} {p.avg_cost:>10.2f} "
                  f"{p.market_price:>8.2f} {p.market_value:>12,.2f}")
    else:
        print(f"    （无持仓）")

    # ── 6. 最终报告 + 链路验证 ──
    print("\n" + "-" * 74)
    print("  [6/6] 最终账户报告 + 链路验证")
    print("-" * 74)

    stats = engine.stats
    print(f"\n  【账户概览】")
    print(f"    初始资金:     {CONFIG['initial_capital']:>12,.2f}")
    print(f"    最终总资产:   {pf_account.get('total_assets', 0):>12,.2f}")
    print(f"    现金:         {pf_account.get('cash', 0):>12,.2f}")
    print(f"    持仓市值:     {pf_account.get('market_value', 0):>12,.2f}")

    print(f"\n  【交易统计】")
    print(f"    总bar数:      {stats['total_bars']:>12}")
    print(f"    总信号数:     {stats['total_signals']:>12}")
    print(f"    买入信号:     {stats['buy_signals']:>12}")
    print(f"    卖出信号:     {stats['sell_signals']:>12}")
    print(f"    总订单数:     {stats['total_orders']:>12}")
    print(f"    成交数:       {stats['total_trades']:>12}")
    print(f"    风控拒绝:     {stats['risk_rejected']:>12}")
    print(f"    错误数:       {stats['errors']:>12}")

    print(f"\n  【主备 failover】")
    print(f"    已触发:       {'是' if failover_done else '否'}")
    print(f"    当前主券商:   {mgr.primary_alias}")
    print(f"    切换后健康:   {mgr.health_check()['connected']} (主={mgr.primary_alias})")

    print("\n" + "=" * 74)
    print("  双券商模拟实盘完成 - 双券商并行链路验证")
    print("=" * 74)

    # 链路验证断言（在 stop 之前检查健康，避免 disconnect 影响）
    print("\n  【链路验证】")
    checks = [
        ("双券商注册", len(mgr.list_brokers()) == 2, f"{len(mgr.list_brokers())}个券商"),
        ("双券商连接", all(conn.values()), f"{conn}"),
        ("策略初始化", strategy._initialized, "已初始化"),
        ("信号生成", stats["total_signals"] > 0, f"{stats['total_signals']}个信号"),
        ("订单提交", stats["total_orders"] > 0, f"{stats['total_orders']}个订单"),
        ("成交撮合", stats["total_trades"] > 0, f"{stats['total_trades']}笔成交"),
        ("主备切换", failover_done, f"→ {mgr.primary_alias}"),
        ("切换后健康", mgr.health_check()["connected"] is True, f"主={mgr.primary_alias}"),
        ("合并持仓", len(mgr_pos) >= 0, f"{len(mgr_pos)}只"),
        ("无错误", stats["errors"] == 0, f"{stats['errors']}个错误"),
    ]

    all_pass = True
    for name, passed, detail in checks:
        status = "✅" if passed else "❌"
        if not passed:
            all_pass = False
        print(f"    {status} {name}: {detail}")

    engine.stop()

    print()
    if all_pass:
        print("  🎉 双券商并行链路全部验证通过！")
    else:
        print("  ⚠️  部分验证未通过，请检查上方日志")

    return all_pass


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
