# 文件名：myquant/live/portfolio.py
"""
portfolio.py - 实时持仓管理（持仓/市值/资金/盈亏）
====================================================
实盘持仓与资金管理层，负责实时持仓、市值、可用冻结资金、盈亏计算。

核心能力:
  1. 持仓管理 — 增删改查持仓，支持多标的
  2. 市值计算 — 行情更新时自动更新持仓市值
  3. 资金管理 — 可用资金、冻结资金、总资产
  4. 盈亏计算 — 已实现盈亏、未实现盈亏、收益率
  5. 与PaperBroker集成 — 从broker同步持仓和资金
  6. 与data_feed集成 — 行情回调自动更新市值
  7. 持仓变更回调 — 持仓/资金变更时通知策略
  8. 为多账户、资金调拨预留接口

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（多账户/资金调拨/回调全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 只使用当前已知价格计算市值，不预判未来价格

防未来函数说明:
  - 市值计算只使用当前已知的市场价（由data_feed或broker提供）
  - 不使用任何未来bar的数据
  - 盈亏计算基于已成交价格和当前市场价，不预判未来走势
  - 持仓变更只在成交确认后更新，不预挂单

v1实现范围:
  - 单账户持仓管理
  - 实时市值更新
  - 资金管理（可用/冻结/总资产）
  - 盈亏计算（已实现/未实现/收益率）
  - 与PaperBroker集成（sync_from_broker）
  - 与data_feed集成（on_market_data回调）
  - 持仓变更回调

TODO(后续迭代):
  - 多账户管理
  - 资金调拨
  - 持仓快照与历史追踪
  - 风险指标计算（VaR、波动率）
  - 持仓集中度分析

依赖: typing, time, myquant.models.trade, myquant.core.logger
"""

from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.models.trade import OrderSide, Position, Trade

logger = get_logger("live.portfolio")

# 账实对账默认现金容差（元）：现金绝对差超过该值即视为账实不一致
_DEFAULT_CASH_TOLERANCE = 0.01


@dataclass
class ReconciliationDiff:
    """账实差异快照（本地账本 vs 券商权威账本）。

    Attributes:
        cash_diff: 本地现金 - 券商现金（元）。
        position_diffs: symbol -> {local_quantity, broker_quantity, diff} 的持仓差异表。
        cash_tolerance: 现金容差（元），超过即视为不一致。
    """

    cash_diff: float = 0.0
    position_diffs: Dict[str, Dict[str, float]] = field(default_factory=dict)
    cash_tolerance: float = _DEFAULT_CASH_TOLERANCE

    @property
    def exceeds_threshold(self) -> bool:
        """是否超过对账容差（现金差超容差，或任一持仓数量不一致）。"""
        if abs(self.cash_diff) > self.cash_tolerance:
            return True
        return any(
            (d.get("diff", 0.0) or 0.0) != 0.0 for d in self.position_diffs.values()
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化（供对账日报落库/查询）。"""
        return {
            "cash_diff": self.cash_diff,
            "cash_tolerance": self.cash_tolerance,
            "position_diffs": self.position_diffs,
            "exceeds_threshold": self.exceeds_threshold,
        }

    def __str__(self) -> str:
        parts = [f"现金差={self.cash_diff:.2f}"]
        for symbol, d in self.position_diffs.items():
            parts.append(
                f"{symbol}: 本地={d.get('local_quantity', 0.0):g} "
                f"券商={d.get('broker_quantity', 0.0):g} 差={d.get('diff', 0.0):g}"
            )
        return "账实差异[" + "; ".join(parts) + "]"


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """线程锁装饰器：调用时获取 self._lock（RLock，可重入，支持嵌套调用）"""
    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper




# ═══════════════════════════════════════════════
#  类型别名
# ═══════════════════════════════════════════════

# 持仓变更回调: (portfolio: LivePortfolio, symbol: str) -> None
PortfolioCallback = Callable[["LivePortfolio", str], None]


# ═══════════════════════════════════════════════
#  LivePortfolio - 实时持仓管理
# ═══════════════════════════════════════════════

class LivePortfolio:
    """
    实时持仓管理（持仓/市值/资金/盈亏）

    v1单账户实现，为多账户预留接口。

    使用方式:
        from myquant.live.portfolio import LivePortfolio
        from myquant.live.brokers import PaperBroker

        # 初始化
        portfolio = LivePortfolio(initial_capital=1_000_000)

        # 与broker集成
        broker = PaperBroker(initial_capital=1_000_000)
        broker.connect()
        portfolio.sync_from_broker(broker)

        # 与data_feed集成（行情更新时自动更新市值）
        feed.subscribe("000001.SZ", portfolio.on_market_data)

        # 查询
        positions = portfolio.get_all_positions()
        account = portfolio.get_account()
        total_assets = portfolio.get_total_assets()
        total_pnl = portfolio.get_total_pnl()
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        account_id: str = "default",
    ):
        """
        初始化实时持仓管理

        Parameters
        ----------
        initial_capital : float
            初始资金（元）
        account_id : str
            账户ID（为多账户预留，v1默认"default"）
        """
        # 线程锁：保护资金/持仓/回调注册表的并发访问（RLock 可重入）
        self._lock = threading.RLock()

        self._account_id = account_id
        self._initial_capital = float(initial_capital)

        # 资金
        self._cash = float(initial_capital)        # 总资金
        self._frozen_cash = 0.0                      # 冻结资金（挂单中）

        # 持仓
        self._positions: Dict[str, Position] = {}   # symbol -> Position

        # 回调
        self._callbacks: List[PortfolioCallback] = []

        # 统计
        self._stats = {
            "total_trades": 0,
            "total_buy_amount": 0.0,
            "total_sell_amount": 0.0,
            "total_commission": 0.0,
            "last_update_time": 0.0,
        }

        logger.info(
            f"实时持仓管理初始化: account={account_id}, "
            f"初始资金={initial_capital:,.0f}"
        )

    # ═══════════════════════════════════════════
    #  账户信息
    # ═══════════════════════════════════════════

    @property
    def account_id(self) -> str:
        """账户ID"""
        return self._account_id

    @property
    def initial_capital(self) -> float:
        """初始资金"""
        return self._initial_capital

    # ═══════════════════════════════════════════
    #  资金管理
    # ═══════════════════════════════════════════

    @property
    @_locked
    def cash(self) -> float:
        """总资金"""
        return self._cash

    @property
    @_locked
    def frozen_cash(self) -> float:
        """冻结资金"""
        return self._frozen_cash

    @property
    @_locked
    def available_cash(self) -> float:
        """可用资金 = 总资金 - 冻结资金"""
        return max(0.0, self._cash - self._frozen_cash)

    @_locked
    def update_cash(self, cash: float, frozen_cash: float = 0.0) -> None:
        """
        更新资金（从broker同步时调用）

        Parameters
        ----------
        cash : float
            总资金
        frozen_cash : float
            冻结资金
        """
        self._cash = float(cash)
        self._frozen_cash = float(frozen_cash)
        self._stats["last_update_time"] = time.time()
        self._notify_callbacks("__cash__")

    @_locked
    def freeze_cash(self, amount: float) -> None:
        """冻结资金（挂单时调用）"""
        self._frozen_cash += amount
        self._notify_callbacks("__cash__")

    @_locked
    def unfreeze_cash(self, amount: float) -> None:
        """解冻资金（撤单时调用）"""
        self._frozen_cash = max(0.0, self._frozen_cash - amount)
        self._notify_callbacks("__cash__")

    # ═══════════════════════════════════════════
    #  持仓管理
    # ═══════════════════════════════════════════

    @_locked
    def get_position(self, symbol: str) -> Optional[Position]:
        """
        获取指定标的持仓

        Parameters
        ----------
        symbol : str
            标的代码

        Returns
        -------
        Position | None : 持仓对象，不存在返回None
        """
        return self._positions.get(symbol)

    @_locked
    def get_all_positions(self) -> List[Position]:
        """
        获取所有非零持仓

        Returns
        -------
        List[Position] : 持仓列表
        """
        return [p for p in self._positions.values() if p.quantity != 0]

    @_locked
    def get_position_symbols(self) -> List[str]:
        """获取所有持仓标的代码"""
        return [s for s, p in self._positions.items() if p.quantity != 0]

    @_locked
    def has_position(self, symbol: str) -> bool:
        """是否持有某标的"""
        pos = self._positions.get(symbol)
        return pos is not None and pos.quantity != 0

    @_locked
    def get_position_count(self) -> int:
        """持仓标的数量"""
        return len([p for p in self._positions.values() if p.quantity != 0])

    # ═══════════════════════════════════════════
    #  市值计算
    # ═══════════════════════════════════════════

    @_locked
    def update_market_price(self, symbol: str, price: float) -> None:
        """
        更新指定标的市场价（行情更新时调用）

        Parameters
        ----------
        symbol : str
            标的代码
        price : float
            当前市场价
        """
        pos = self._positions.get(symbol)
        if pos:
            pos.update_market_price(float(price))
            self._stats["last_update_time"] = time.time()
            self._notify_callbacks(symbol)

    @_locked
    def update_all_market_prices(self, price_map: Dict[str, float]) -> None:
        """
        批量更新市场价

        Parameters
        ----------
        price_map : Dict[str, float]
            {symbol: price} 价格映射
        """
        for symbol, price in price_map.items():
            self.update_market_price(symbol, price)

    @_locked
    def get_market_value(self, symbol: Optional[str] = None) -> float:
        """
        获取持仓市值

        Parameters
        ----------
        symbol : str | None
            指定标的，None则返回总市值

        Returns
        -------
        float : 市值
        """
        if symbol:
            pos = self._positions.get(symbol)
            return pos.market_value if pos else 0.0
        return sum(p.market_value for p in self._positions.values() if p.quantity != 0)

    # ═══════════════════════════════════════════
    #  盈亏计算
    # ═══════════════════════════════════════════

    @_locked
    def get_unrealized_pnl(self, symbol: Optional[str] = None) -> float:
        """
        获取未实现盈亏（浮动盈亏）

        Parameters
        ----------
        symbol : str | None
            指定标的，None则返回总浮动盈亏

        Returns
        -------
        float : 未实现盈亏
        """
        if symbol:
            pos = self._positions.get(symbol)
            return pos.unrealized_pnl if pos else 0.0
        return sum(p.unrealized_pnl for p in self._positions.values() if p.quantity != 0)

    @_locked
    def get_realized_pnl(self, symbol: Optional[str] = None) -> float:
        """
        获取已实现盈亏

        Parameters
        ----------
        symbol : str | None
            指定标的，None则返回总已实现盈亏

        Returns
        -------
        float : 已实现盈亏
        """
        if symbol:
            pos = self._positions.get(symbol)
            return pos.realized_pnl if pos else 0.0
        return sum(p.realized_pnl for p in self._positions.values())

    @_locked
    def get_total_pnl(self) -> float:
        """
        获取总盈亏 = 已实现盈亏 + 未实现盈亏

        Returns
        -------
        float : 总盈亏
        """
        return self.get_realized_pnl() + self.get_unrealized_pnl()

    @_locked
    def get_total_pnl_ratio(self) -> float:
        """
        获取总收益率 = 总盈亏 / 初始资金

        Returns
        -------
        float : 总收益率
        """
        if self._initial_capital <= 0:
            return 0.0
        return self.get_total_pnl() / self._initial_capital

    @_locked
    def get_position_pnl_ratio(self, symbol: str) -> float:
        """
        获取指定持仓的收益率

        Parameters
        ----------
        symbol : str
            标的代码

        Returns
        -------
        float : 收益率
        """
        pos = self._positions.get(symbol)
        return pos.pnl_ratio if pos else 0.0

    # ═══════════════════════════════════════════
    #  账户总览
    # ═══════════════════════════════════════════

    @_locked
    def get_total_assets(self) -> float:
        """
        获取总资产 = 总资金 + 持仓市值

        Returns
        -------
        float : 总资产
        """
        return self._cash + self.get_market_value()

    @_locked
    def get_equity(self) -> float:
        """
        获取净值 = 总资产 / 初始资金

        Returns
        -------
        float : 净值
        """
        if self._initial_capital <= 0:
            return 1.0
        return self.get_total_assets() / self._initial_capital

    @_locked
    def get_account(self) -> Dict[str, Any]:
        """
        获取账户资金总览

        Returns
        -------
        Dict[str, Any] :
            {
                "account_id": str,
                "initial_capital": float,
                "cash": float,
                "available_cash": float,
                "frozen_cash": float,
                "market_value": float,
                "total_assets": float,
                "equity": float,
                "realized_pnl": float,
                "unrealized_pnl": float,
                "total_pnl": float,
                "total_pnl_ratio": float,
                "position_count": int,
            }
        """
        return {
            "account_id": self._account_id,
            "initial_capital": round(self._initial_capital, 2),
            "cash": round(self._cash, 2),
            "available_cash": round(self.available_cash, 2),
            "frozen_cash": round(self._frozen_cash, 2),
            "market_value": round(self.get_market_value(), 2),
            "total_assets": round(self.get_total_assets(), 2),
            "equity": round(self.get_equity(), 4),
            "realized_pnl": round(self.get_realized_pnl(), 2),
            "unrealized_pnl": round(self.get_unrealized_pnl(), 2),
            "total_pnl": round(self.get_total_pnl(), 2),
            "total_pnl_ratio": round(self.get_total_pnl_ratio(), 4),
            "position_count": self.get_position_count(),
        }

    # ═══════════════════════════════════════════
    #  与PaperBroker集成
    # ═══════════════════════════════════════════

    @_locked
    def reconcile_vs_broker(
        self, broker: Any, cash_tolerance: float = _DEFAULT_CASH_TOLERANCE,
    ) -> ReconciliationDiff:
        """账实对账：本地账本 vs 券商权威账本，只 diff 不重建（先检查后同步）。

        KP-P4-01：sync_from_broker 原实现 clear() 后整体重建、无差异比对，
        券商返回空快照会静默覆盖本地持仓。本方法先产出差异清单供告警/落库，
        不修改任何本地状态。

        Parameters
        ----------
        broker : BaseBroker
            券商实例（PaperBroker 或真实券商）。
        cash_tolerance : float
            现金容差（元），超过即视为不一致，默认 0.01。

        Returns
        -------
        ReconciliationDiff : 账实差异快照（现金差 + 各持仓数量差 + 是否超阈值）。
        """
        account = broker.get_account()
        broker_cash = float(account.get("cash", 0.0))
        cash_diff = self._cash - broker_cash

        broker_positions = {p.symbol: p for p in broker.get_positions()}
        pos_diffs: Dict[str, Dict[str, float]] = {}
        all_symbols = set(broker_positions) | set(self._positions.keys())
        for symbol in sorted(all_symbols):
            local_pos = self._positions.get(symbol)
            broker_pos = broker_positions.get(symbol)
            local_qty = float(local_pos.quantity) if local_pos is not None else 0.0
            broker_qty = float(broker_pos.quantity) if broker_pos is not None else 0.0
            if local_qty != broker_qty:
                pos_diffs[symbol] = {
                    "local_quantity": local_qty,
                    "broker_quantity": broker_qty,
                    "diff": local_qty - broker_qty,
                }

        return ReconciliationDiff(
            cash_diff=cash_diff,
            position_diffs=pos_diffs,
            cash_tolerance=cash_tolerance,
        )

    @_locked
    def diff_vs_broker(
        self, broker: Any, cash_tolerance: float = _DEFAULT_CASH_TOLERANCE,
    ) -> ReconciliationDiff:
        """账实差异（KP-P3-08：风控基准券商权威账本时先取差异，只 diff 不重建）。"""
        return self.reconcile_vs_broker(broker, cash_tolerance=cash_tolerance)

    @_locked
    def sync_from_broker(
        self, broker: Any, cash_tolerance: float = _DEFAULT_CASH_TOLERANCE,
    ) -> ReconciliationDiff:
        """
        从broker同步持仓和资金（先 diff 后重建，禁止静默覆盖）。

        KP-P4-01：同步前先与券商对账——若账实不一致（现金差超容差或任一持仓
        数量不同），记录 critical 日志并通知 __reconciliation_diff__ 回调，
        不静默 clear 覆盖本地账本。

        Parameters
        ----------
        broker : BaseBroker
            券商实例（PaperBroker或真实券商）。
        cash_tolerance : float
            现金容差（元）。

        Returns
        -------
        ReconciliationDiff : 本次同步前的账实差异快照。
        """
        # #T-07(KP-P4-01)：先 diff 后重建
        diff = self.reconcile_vs_broker(broker, cash_tolerance=cash_tolerance)
        if diff.exceeds_threshold:
            self._stats["last_reconciliation_diff"] = str(diff)
            logger.critical(
                f"[portfolio] 账实差异 {diff}，禁止静默覆盖（仍按券商权威账本重建）"
            )
            self._notify_callbacks("__reconciliation_diff__")

        # 同步资金
        account = broker.get_account()
        self._cash = account.get("cash", self._cash)
        self._frozen_cash = account.get("frozen_cash", 0.0)

        # 同步持仓
        broker_positions = broker.get_positions()
        self._positions.clear()
        for pos in broker_positions:
            self._positions[pos.symbol] = Position(
                symbol=pos.symbol,
                quantity=pos.quantity,
                avg_cost=pos.avg_cost,
                market_price=pos.market_price,
                realized_pnl=pos.realized_pnl,
                opened_at=pos.opened_at,
                strategy_id=pos.strategy_id,
            )

        self._stats["last_update_time"] = time.time()
        logger.info(
            f"从broker同步完成: 持仓={self.get_position_count()}只, "
            f"资金={self._cash:,.2f}, 总资产={self.get_total_assets():,.2f}"
        )
        self._notify_callbacks("__sync__")
        return diff

    @_locked
    def apply_trade(self, trade: Trade) -> None:
        """
        应用成交到持仓和资金（成交回报时调用）

        Parameters
        ----------
        trade : Trade
            成交记录
        """
        symbol = trade.symbol

        # 获取或创建持仓
        pos = self._positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self._positions[symbol] = pos

        # 应用成交到持仓
        pos.apply_fill(trade.quantity, trade.price, trade.side)

        # 更新资金
        total_cost = trade.quantity * trade.price + trade.commission
        if trade.side == OrderSide.BUY:
            self._cash -= total_cost
            self._stats["total_buy_amount"] += trade.quantity * trade.price
        else:
            self._cash += (trade.quantity * trade.price - trade.commission)
            self._stats["total_sell_amount"] += trade.quantity * trade.price

        self._stats["total_commission"] += trade.commission
        self._stats["total_trades"] += 1
        self._stats["last_update_time"] = time.time()

        logger.info(
            f"成交应用: {symbol} {trade.side.value} "
            f"{trade.quantity:.0f}@{trade.price:.2f} "
            f"手续费={trade.commission:.2f}"
        )
        self._notify_callbacks(symbol)

    # ═══════════════════════════════════════════
    #  与data_feed集成
    # ═══════════════════════════════════════════

    @_locked
    def on_market_data(self, symbol: str, bar: Dict[str, Any]) -> None:
        """
        行情回调（供data_feed调用，自动更新市值）

        Parameters
        ----------
        symbol : str
            标的代码
        bar : Dict[str, Any]
            K线数据，包含close字段
        """
        close_price = bar.get("close")
        if close_price is not None:
            self.update_market_price(symbol, float(close_price))

    # ═══════════════════════════════════════════
    #  回调管理
    # ═══════════════════════════════════════════

    @_locked
    def register_callback(self, callback: PortfolioCallback) -> None:
        """
        注册持仓变更回调

        Parameters
        ----------
        callback : PortfolioCallback
            回调函数 (portfolio, symbol) -> None
        """
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    @_locked
    def unregister_callback(self, callback: PortfolioCallback) -> None:
        """注销持仓变更回调"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _notify_callbacks(self, symbol: str) -> None:
        """通知所有回调

        线程安全：锁内拷贝回调列表，锁外执行回调，
        避免回调内再取其他锁造成死锁或长时间持锁。
        """
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback(self, symbol)
            except Exception as e:
                logger.error(f"[portfolio] 回调异常: {e}")

    # ═══════════════════════════════════════════
    #  统计与快照
    # ═══════════════════════════════════════════

    @property
    @_locked
    def stats(self) -> Dict[str, Any]:
        """统计信息（只读）"""
        return dict(self._stats)

    @_locked
    def get_positions_snapshot(self) -> List[Dict[str, Any]]:
        """
        获取持仓快照（序列化）

        Returns
        -------
        List[Dict] : 持仓列表（字典格式）
        """
        return [pos.to_dict() for pos in self.get_all_positions()]

    @_locked
    def get_full_snapshot(self) -> Dict[str, Any]:
        """
        获取完整账户快照

        Returns
        -------
        Dict[str, Any] :
            {
                "account": Dict,
                "positions": List[Dict],
                "stats": Dict,
                "timestamp": float,
            }
        """
        return {
            "account": self.get_account(),
            "positions": self.get_positions_snapshot(),
            "stats": dict(self._stats),
            "timestamp": time.time(),
        }

    # ═══════════════════════════════════════════
    #  多账户预留接口（后续迭代）
    # ═══════════════════════════════════════════

    def transfer_cash(self, amount: float, target_account: str) -> bool:
        """
        资金调拨（预留，v1不实现）

        TODO: 后续迭代实现多账户资金调拨

        Parameters
        ----------
        amount : float
            调拨金额
        target_account : str
            目标账户ID

        Returns
        -------
        bool : True=成功
        """
        logger.warning(f"[portfolio] 资金调拨未实现（v1单账户）: {amount} -> {target_account}")
        return False

    @_locked
    def reset(self) -> None:
        """重置到初始状态"""
        self._cash = self._initial_capital
        self._frozen_cash = 0.0
        self._positions.clear()
        self._stats = {
            "total_trades": 0,
            "total_buy_amount": 0.0,
            "total_sell_amount": 0.0,
            "total_commission": 0.0,
            "last_update_time": 0.0,
        }
        logger.info("[portfolio] 已重置到初始状态")

    def __repr__(self) -> str:
        return (
            f"LivePortfolio(account={self._account_id}, "
            f"cash={self._cash:,.0f}, "
            f"positions={self.get_position_count()}, "
            f"equity={self.get_equity():.4f})"
        )
