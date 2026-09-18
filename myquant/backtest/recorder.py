# 文件名：myquant/backtest/recorder.py
"""
backtest/recorder.py - 交易记录器
====================================
记录回测/实盘的成交明细、持仓变动、资金曲线。

设计原则（规则2：为实盘无缝对接准备）:
  - TradeRecorder 回测/实盘共用，统一记录接口
  - 实盘运行时可实时记录成交和净值，支持增量查询
  - 记录数据可持久化到数据库/文件，支持崩溃恢复
  - 提供丰富的查询接口：按标的、按日期、按策略、按盈亏

防未来函数（规则4）:
  - 记录器只记录已发生的事件，不预测未来
  - 净值在每根bar收盘后记录，使用收盘价计算市值
  - 成交记录在撮合完成后记录，不使用未来价格
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from myquant.core.logger import get_logger
from myquant.models.backtest import EquityPoint, TradeRecord
from myquant.models.trade import OrderSide, Position, Trade

logger = get_logger(__name__)


class TradeRecorder:
    """
    交易记录器

    核心功能:
      - record_trade(trade): 记录一笔成交
      - record_equity(timestamp, cash, position_value, equity): 记录净值
      - get_trades(): 获取全部成交记录
      - get_equity_curve(): 获取净值曲线
      - get_trades_by_symbol(symbol): 按标的查询
      - get_trades_by_date(start, end): 按日期查询
      - calculate_trade_pnl(): 配对计算每笔平仓交易的盈亏

    使用方式:
        recorder = TradeRecorder(initial_capital=1000000)
        recorder.record_trade(trade)
        recorder.record_equity(timestamp, cash, position_value, equity)
        trades = recorder.get_trades()
        equity = recorder.get_equity_curve()
    """

    def __init__(self, initial_capital: float = 1_000_000.0):
        """
        初始化记录器

        参数:
            initial_capital: 初始资金
        """
        self.initial_capital = float(initial_capital)
        self._trades: List[TradeRecord] = []
        self._equity_curve: List[EquityPoint] = []
        # 持仓成本记录（用于计算平仓盈亏）
        self._position_costs: Dict[str, List[Dict]] = {}
        # 每日持仓快照
        self._daily_positions: Dict[str, List[Position]] = {}
        # 净值峰值（增量维护，避免 record_equity 每 bar 全量 max 导致 O(n²)）
        self._peak_equity: float = 0.0
        logger.info(f"交易记录器初始化: 初始资金={initial_capital:,.0f}")

    # ------------------------------------------------------------------
    # 记录成交
    # ------------------------------------------------------------------

    def record_trade(self, trade: Trade, signal_reason: str = "") -> TradeRecord:
        """
        记录一笔成交

        参数:
            trade: 成交对象（models.trade.Trade）
            signal_reason: 信号原因（用于策略归因）

        返回:
            TradeRecord: 记录的成交记录（含盈亏计算）
        """
        # 转换为TradeRecord
        record = TradeRecord(
            trade_id=getattr(trade, 'trade_id', '') or f"t_{len(self._trades)+1:06d}",
            symbol=trade.symbol,
            side=trade.side.value,
            quantity=trade.quantity,
            price=trade.price,
            timestamp=trade.timestamp,
            commission=getattr(trade, 'commission', 0.0),
            slippage=getattr(trade, 'slippage', 0.0),
            strategy_id=getattr(trade, 'strategy_id', ''),
            entry_reason=signal_reason,
        )

        # 计算平仓盈亏（FIFO配对）
        if trade.side == OrderSide.SELL:
            pnl, pnl_pct, holding_days = self._calc_sell_pnl(trade)
            record.pnl = pnl
            record.pnl_pct = pnl_pct
            record.holding_days = holding_days
            record.exit_reason = signal_reason
        else:
            # 买入：记录持仓成本
            self._add_position_cost(trade.symbol, trade.quantity, trade.price, trade.timestamp)

        self._trades.append(record)
        logger.debug(
            f"记录成交: {record.symbol} {record.side} "
            f"{record.quantity:.0f}@{record.price:.2f} "
            f"pnl={record.pnl:.2f}"
        )
        return record

    def _add_position_cost(self, symbol: str, quantity: float, price: float, timestamp: float) -> None:
        """记录买入持仓成本（FIFO队列）"""
        if symbol not in self._position_costs:
            self._position_costs[symbol] = []
        self._position_costs[symbol].append({
            'quantity': quantity,
            'price': price,
            'timestamp': timestamp,
        })

    def _calc_sell_pnl(self, trade: Trade) -> tuple:
        """
        计算卖出盈亏（FIFO配对）

        返回:
            (pnl, pnl_pct, holding_days)
        """
        symbol = trade.symbol
        if symbol not in self._position_costs or not self._position_costs[symbol]:
            return 0.0, 0.0, 0

        remaining = trade.quantity
        total_cost = 0.0
        total_qty = 0.0
        earliest_timestamp = trade.timestamp

        while remaining > 0 and self._position_costs[symbol]:
            lot = self._position_costs[symbol][0]
            if lot['quantity'] <= remaining:
                # 整批消耗
                total_cost += lot['quantity'] * lot['price']
                total_qty += lot['quantity']
                remaining -= lot['quantity']
                earliest_timestamp = min(earliest_timestamp, lot['timestamp'])
                self._position_costs[symbol].pop(0)
            else:
                # 部分消耗
                total_cost += remaining * lot['price']
                total_qty += remaining
                lot['quantity'] -= remaining
                earliest_timestamp = min(earliest_timestamp, lot['timestamp'])
                remaining = 0

        if total_qty <= 0:
            return 0.0, 0.0, 0

        avg_cost = total_cost / total_qty
        pnl = (trade.price - avg_cost) * total_qty - trade.commission
        pnl_pct = (trade.price - avg_cost) / avg_cost if avg_cost > 0 else 0.0

        # 持仓天数
        holding_days = max(1, int((trade.timestamp - earliest_timestamp) / 86400))

        return pnl, pnl_pct, holding_days

    # ------------------------------------------------------------------
    # 记录净值
    # ------------------------------------------------------------------

    def record_equity(
        self,
        timestamp: float,
        cash: float,
        position_value: float,
        equity: Optional[float] = None,
        benchmark_equity: float = 1.0,
    ) -> EquityPoint:
        """
        记录净值数据点

        参数:
            timestamp: 时间戳
            cash: 现金
            position_value: 持仓市值
            equity: 净值（相对初始资金，默认自动计算）
            benchmark_equity: 基准净值

        返回:
            EquityPoint: 净值数据点
        """
        if equity is None:
            total = cash + position_value
            equity = total / self.initial_capital if self.initial_capital > 0 else 1.0

        # 计算当前回撤（增量峰值维护，避免 O(n²) 全量遍历历史净值）
        peak = max(self._peak_equity, equity)
        self._peak_equity = peak
        drawdown = (equity - peak) / peak if peak > 0 else 0.0

        date_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d') if timestamp > 0 else ""

        point = EquityPoint(
            timestamp=timestamp,
            date=date_str,
            equity=equity,
            cash=cash,
            position_value=position_value,
            drawdown=drawdown,
            benchmark_equity=benchmark_equity,
        )
        self._equity_curve.append(point)
        return point

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_trades(self) -> List[TradeRecord]:
        """获取全部成交记录"""
        return self._trades.copy()

    def get_equity_curve(self) -> List[EquityPoint]:
        """获取净值曲线"""
        return self._equity_curve.copy()

    def get_trades_by_symbol(self, symbol: str) -> List[TradeRecord]:
        """按标的查询成交"""
        return [t for t in self._trades if t.symbol == symbol]

    def get_trades_by_date(self, start_date: str, end_date: str) -> List[TradeRecord]:
        """按日期范围查询成交"""
        result = []
        for t in self._trades:
            date_str = datetime.fromtimestamp(t.timestamp).strftime('%Y-%m-%d') if t.timestamp > 0 else ""
            if start_date <= date_str <= end_date:
                result.append(t)
        return result

    def get_closed_trades(self) -> List[TradeRecord]:
        """获取已平仓交易（有盈亏的卖出记录）"""
        return [t for t in self._trades if t.side == "SELL" and t.pnl != 0]

    def get_equity_df(self) -> pd.DataFrame:
        """获取净值曲线DataFrame"""
        data = []
        for ep in self._equity_curve:
            data.append({
                'date': ep.date,
                'equity': ep.equity,
                'cash': ep.cash,
                'position_value': ep.position_value,
                'drawdown': ep.drawdown,
                'benchmark_equity': ep.benchmark_equity,
            })
        return pd.DataFrame(data)

    def get_trades_df(self) -> pd.DataFrame:
        """获取成交记录DataFrame"""
        data = []
        for t in self._trades:
            data.append(t.to_dict())
        return pd.DataFrame(data)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    @property
    def trade_count(self) -> int:
        """成交笔数"""
        return len(self._trades)

    @property
    def equity_points(self) -> int:
        """净值数据点数"""
        return len(self._equity_curve)

    @property
    def final_equity(self) -> float:
        """最终净值"""
        if self._equity_curve:
            return self._equity_curve[-1].equity
        return 1.0

    @property
    def total_pnl(self) -> float:
        """总盈亏（已平仓）"""
        return sum(t.pnl for t in self._trades if t.side == "SELL")

    @property
    def total_commission(self) -> float:
        """总手续费"""
        return sum(t.commission for t in self._trades)

    def reset(self) -> None:
        """重置记录器"""
        self._trades.clear()
        self._equity_curve.clear()
        self._position_costs.clear()
        self._daily_positions.clear()
        self._peak_equity = 0.0
        logger.info("交易记录器已重置")

    def __repr__(self) -> str:
        return (
            f"TradeRecorder(trades={self.trade_count}, "
            f"equity_points={self.equity_points}, "
            f"final_equity={self.final_equity:.4f})"
        )
