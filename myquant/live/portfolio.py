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

from myquant.core.constants import SeverityLevel
from myquant.core.logger import get_logger
from myquant.models.trade import OrderSide, Position, Trade
from myquant.risk.guardian import (
    get_operation_guard,  # G1 EX-12C：L4→L3 合法（准入守卫注册表访问器）
)

logger = get_logger("live.portfolio")

# 对账告警分类（A-7：与 B-1 完全对齐，category=reconciliation）
RECONCILIATION_ALERT_CATEGORY = "reconciliation"


def _load_realtime_diff_tolerance() -> float:
    """R-04 阈值唯一真源：读取 settings.yaml 对账实时差异容差。

    经 ``myquant.core.settings.get_monitoring_config()`` 读取
    ``monitoring.slo.reconciliation.realtime_diff_tolerance``。P0-2 锁定清单：
    实时对账容差 = 0（与 B-1 完全对齐，禁止本项自定义阈值，R-04）。
    防御式：配置缺失/非法时回退 LOCKED=0.0 并记 warning 留痕，不静默使用脏值。

    防未来：只消费静态配置值，不涉及任何时间序列或预测。

    Returns:
        float: 实时对账现金容差（元），LOCKED=0。
    """
    try:
        from myquant.core.settings import get_monitoring_config
        slo = get_monitoring_config().get("slo") or {}
        rec = slo.get("reconciliation") or {}
        return float(rec.get("realtime_diff_tolerance", 0.0))
    except Exception as e:  # 配置读取失败不得阻断对账
        logger.warning("[portfolio] 读取对账容差配置失败，回退 LOCKED=0.0: %s", e)
        return 0.0


def _reconciliation_dedup_key(diff: "ReconciliationDiff") -> str:
    """构造对账告警去重键：日期 + 现金差 + 差异标的集合（内容相关，防刷屏）。

    Args:
        diff: 账实差异快照。

    Returns:
        str: dedup_key，形如 ``reconciliation:20261006:cash10000.00:600000.SH``。
    """
    date = time.strftime("%Y%m%d", time.localtime())
    symbols = ",".join(sorted(diff.position_diffs.keys()))
    return f"reconciliation:{date}:cash{diff.cash_diff:.2f}:{symbols}"


@dataclass
class ReconciliationDiff:
    """账实差异快照（本地账本 vs 券商权威账本）。

    Attributes:
        cash_diff: 本地现金 - 券商现金（元）。
        position_diffs: symbol -> {local_quantity, broker_quantity, diff} 的持仓差异表。
        cash_tolerance: 现金容差（元），超过即视为不一致（生产路径由
            reconcile_vs_broker 经 R-04 配置真源解析传入；此处 0.0 仅为
            dataclass 兜底默认，镜像 P0-2 LOCKED=0）。
    """

    cash_diff: float = 0.0
    position_diffs: Dict[str, Dict[str, float]] = field(default_factory=dict)
    cash_tolerance: float = 0.0

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
        alert_manager: Optional[Any] = None,
    ):
        """
        初始化实时持仓管理

        Parameters
        ----------
        initial_capital : float
            初始资金（元）
        account_id : str
            账户ID（为多账户预留，v1默认"default"）
        alert_manager : Any | None
            告警管理器实例（G1 EX-12B 注入点；None 时对账差异告警跳过，
            经 set_alert_manager 注入，装配责任在合法层/装配器）。
        """
        # 线程锁：保护资金/持仓/回调注册表的并发访问（RLock 可重入）
        self._lock = threading.RLock()

        self._account_id = account_id
        self._initial_capital = float(initial_capital)

        # 资金
        self._cash = float(initial_capital)        # 总资金（本地记账账，book_cash）
        self._frozen_cash = 0.0                      # 冻结资金（挂单中）
        # T0-1 双值并存：broker_cash 是券商快照值（仅比对用，不自动覆写本地账）
        self._broker_cash: float = 0.0               # 券商现金快照（sync_from_broker 时刷新）
        self._reconciliation_blocked: bool = False   # 对账冻结（diff 超阈 → 本地账不被覆写）
        # T0-3 健全性闸：上一次同步的持仓数/总资产（骤降 >50% 或绝对值 0 → 拒覆盖）
        self._last_sync_position_count: int = 0
        self._last_sync_total_assets: float = 0.0

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

        # A-7：事件驱动对账挂载状态（attach_reconciliation_event_trigger 写入）
        self._recon_bus: Any = None
        self._recon_broker: Any = None
        self._recon_handler_ids: List[str] = []

        # B-1：账户对账器（AccountReconciler）注入点。None 时事件回调走 A-7
        # 同步核对兜底（既有单测依赖同步语义）；注入后事件回调改为异步投递，
        # 且 PnL 防脏门据其冻结状态拒绝脏 PnL 记录。
        self._account_reconciler: Any = None
        # G1 EX-12B：告警管理器注入点（对账差异 CRITICAL 告警投递）。None 时
        # 对账差异告警跳过（warning 留痕），由装配器经 set_alert_manager 注入。
        self._alert_manager: Optional[Any] = alert_manager

        # 0.6（回写#292）：账实差异超阈 → 禁开仓注入点（用例5，实时链）。
        # 回调由装配层（create_live_engine）注入 engine.pause（带 reason，0.2 语义）；
        # portfolio **不 import engine**（禁止反向 import，grep 复验）。None 时
        # 超阈仅告警不阻断（F-4：hook 缺失/异常 → 降级 + CRITICAL，不阻断主链路）。
        self._pause_hook: Optional[Callable[[Any], None]] = None

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
    #  PnL 防脏门（B-1，B-2 依赖）
    # ═══════════════════════════════════════════

    def _resolve_reconciler(self) -> Any:
        """解析对账器状态源：仅用装配层注入的实例（G1 EX-12A 消除 L4→L5 反向）。

        Returns:
            AccountReconciler | None: 对账器；未注入返回 None（门视为放行，
            不因监控组件缺失阻断正常 PnL 记录）。
        """
        if self._account_reconciler is not None:
            return self._account_reconciler
        logger.warning("[portfolio] 未注入账户对账器（set_account_reconciler），PnL 门视为放行")
        return None

    @_locked
    def record_pnl_snapshot(self, reason: str = "periodic") -> Optional[Dict[str, Any]]:
        """记录一次 PnL 快照（B-2 消费入口）；账户对账冻结时拒绝并留痕。

        B-1 PnL 防脏门：当账户对账器报告账户冻结（差异未人工解除，is_frozen）时，
        拒绝 PnL 记录并经对账器审计留痕（action="pnl_block"），防止脏对账状态下
        写入不可信 PnL；一致（未冻结）后才允许记录。对账器缺失时视为放行。

        Parameters
        ----------
        reason : str
            记录触发原因（periodic / settle / manual 等，仅留痕用）。

        Returns
        -------
        dict | None :
            一致时返回 PnL 快照（realized/unrealized/total_pnl/ts/reason）；
            冻结被拒时返回 None。
        """
        reconciler = self._resolve_reconciler()
        if reconciler is not None and getattr(reconciler, "is_frozen", False):
            logger.warning(
                "[portfolio] PnL 记录被防脏门拒绝（账户对账冻结）: account=%s reason=%s",
                self._account_id, reason,
            )
            try:
                reconciler.note_pnl_blocked(reason)
            except Exception as e:  # 留痕失败不阻断拒绝判定
                logger.error("[portfolio] PnL 拒绝留痕失败: %s", e)
            return None

        return {
            "account_id": self._account_id,
            "realized_pnl": round(self.get_realized_pnl(), 2),
            "unrealized_pnl": round(self.get_unrealized_pnl(), 2),
            "total_pnl": round(self.get_total_pnl(), 2),
            "total_assets": round(self.get_total_assets(), 2),
            "ts": time.time(),  # 记录时刻（当前时点，防未来）
            "reason": reason,
        }

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

    # ── T0-1 双值并存访问器 ──
    @property
    def book_cash(self) -> float:
        """本地记账账现金（book_cash）——本地账权威，不被券商快照自动覆写。"""
        return self._cash

    @property
    def broker_cash(self) -> float:
        """券商快照现金（broker_cash）——仅比对用，双值并存。"""
        return self._broker_cash

    @property
    def is_reconciliation_blocked(self) -> bool:
        """对账是否冻结（diff 超阈 → 本地账不被覆写，需人工 adopt）。"""
        return self._reconciliation_blocked

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

    def set_pause_hook(self, callback: Optional[Callable[[Any], None]]) -> None:
        """
        0.6（回写#292）账实差异超阈 → 禁开仓注入点（用例5，实时链）。

        装配层（create_live_engine）注入 ``engine.pause`` 回调（带 reason，0.2
        ``_pause_source`` 语义）；portfolio **不 import engine**（禁止反向 import）。
        callback 为 None 表示不注入（超阈仅告警不阻断，F-4 降级语义）。

        Parameters
        ----------
        callback : Callable[[ReconciliationDiff], None] | None
            差异超阈时回调（参数为 ReconciliationDiff 快照）。
        """
        self._pause_hook = callback

    @_locked
    def reconcile_vs_broker(
        self, broker: Any, cash_tolerance: Optional[float] = None,
    ) -> ReconciliationDiff:
        """账实对账：本地账本 vs 券商权威账本，只 diff 不重建（先检查后同步）。

        KP-P4-01：sync_from_broker 原实现 clear() 后整体重建、无差异比对，
        券商返回空快照会静默覆盖本地持仓。本方法先产出差异清单供告警/落库，
        不修改任何本地状态。

        A-7（R-04 / R-02）：现金容差不再硬编码，默认经
        ``myquant.core.settings.get_monitoring_config()`` 读取
        ``monitoring.slo.reconciliation.realtime_diff_tolerance``（P0-2 LOCKED=0，
        与 B-1 完全对齐，不在本项自定义）。差异超阈 → 经 get_alert_manager().alert
        发 CRITICAL 告警（details 含 cash_diff / 各持仓 position_diffs / 容差，
        category=reconciliation，带内容相关 dedup_key，禁止静默覆盖）；一致 →
        logger.info 记录。

        Parameters
        ----------
        broker : BaseBroker
            券商实例（PaperBroker 或真实券商）。
        cash_tolerance : float | None
            现金容差（元），超过即视为不一致；None 时取 R-04 配置真源（LOCKED=0）。

        Returns
        -------
        ReconciliationDiff : 账实差异快照（现金差 + 各持仓数量差 + 是否超阈值）。
        """
        if cash_tolerance is None:
            cash_tolerance = _load_realtime_diff_tolerance()

        account = broker.get_account()
        broker_cash = float(account.get("cash", 0.0))
        # T0-15：cash_diff 先 round 2 位再比较（FI-22：1e-9 浮点尾差不冻结）
        cash_diff = round(self._cash - broker_cash, 2)

        broker_positions = {p.symbol: p for p in broker.get_positions()}
        pos_diffs: Dict[str, Dict[str, float]] = {}
        all_symbols = set(broker_positions) | set(self._positions.keys())
        for symbol in sorted(all_symbols):
            local_pos = self._positions.get(symbol)
            broker_pos = broker_positions.get(symbol)
            # T0-15：持仓数量用整数比较（A股数量必为整数，float 尾差不触发冻结）
            local_qty = int(local_pos.quantity) if local_pos is not None else 0
            broker_qty = int(broker_pos.quantity) if broker_pos is not None else 0
            if local_qty != broker_qty:
                pos_diffs[symbol] = {
                    "local_quantity": local_qty,
                    "broker_quantity": broker_qty,
                    "diff": local_qty - broker_qty,
                }

        diff = ReconciliationDiff(
            cash_diff=cash_diff,
            position_diffs=pos_diffs,
            cash_tolerance=cash_tolerance,
        )

        # A-7：差异 → CRITICAL 告警（禁止静默覆盖）；一致 → INFO 记录
        if diff.exceeds_threshold:
            self._fire_reconciliation_alert(diff)
        else:
            logger.info(
                "[portfolio] 账实对账一致: account=%s 现金差=%.4f 持仓差异数=%d",
                self._account_id, diff.cash_diff, len(diff.position_diffs),
            )
        return diff

    def _fire_reconciliation_alert(self, diff: ReconciliationDiff) -> None:
        """差异超阈 → CRITICAL 告警（A-7，禁止静默覆盖）。

        经全局告警管理器投递，details 含差异明细（cash_diff / cash_tolerance /
        各持仓 position_diffs），category=reconciliation，带内容相关 dedup_key。
        告警投递失败仅记 error 留痕，不阻断对账/交易（告警投递优先原则）。

        Args:
            diff: 超阈的账实差异快照。
        """
        logger.critical("[portfolio] 账实差异超阈: %s", diff)
        details: Dict[str, Any] = {
            "account_id": self._account_id,
            "cash_diff": round(diff.cash_diff, 4),
            "cash_tolerance": diff.cash_tolerance,
            "position_diff_count": len(diff.position_diffs),
            "position_diffs": diff.position_diffs,
        }
        dedup_key = _reconciliation_dedup_key(diff)
        alert_manager = self._alert_manager
        if alert_manager is None:
            logger.warning("[portfolio] 未注入告警管理器（set_alert_manager），对账差异告警跳过")
            return
        try:
            alert_manager.alert(
                level=SeverityLevel.CRITICAL,
                message=(
                    f"账实对账差异超阈（容差={diff.cash_tolerance}）: "
                    f"现金差={diff.cash_diff:.2f}, 持仓差异={len(diff.position_diffs)}只"
                ),
                source="live.portfolio",
                category=RECONCILIATION_ALERT_CATEGORY,
                details=details,
                deduplication_key=dedup_key,
            )
        except Exception as e:  # 告警投递失败不阻断对账主流程
            logger.error("[portfolio] 对账 CRITICAL 告警投递失败（不阻断）: %s", e)

        # 0.6（回写#292）：账实差异超阈 → 禁开仓（用例5，实时链）。
        # hook 由装配层注入（engine.pause，带 reason）；hook 缺失/异常 → 降级 +
        # CRITICAL 留痕，不阻断对账/交易主链路（F-4，不静默）。
        if self._pause_hook is not None:
            try:
                self._pause_hook(diff)
            except Exception as e:  # noqa: BLE001
                logger.critical(
                    "[portfolio] 对账禁开仓 hook 执行失败（F-4 降级，交易主链路不阻断）: %s", e
                )

    @_locked
    def diff_vs_broker(
        self, broker: Any, cash_tolerance: Optional[float] = None,
    ) -> ReconciliationDiff:
        """账实差异（KP-P3-08：风控基准券商权威账本时先取差异，只 diff 不重建）。

        容差默认取 R-04 配置真源（实时对账 LOCKED=0）。
        """
        return self.reconcile_vs_broker(broker, cash_tolerance=cash_tolerance)

    @_locked
    def sync_from_broker(
        self, broker: Any, cash_tolerance: Optional[float] = None,
    ) -> ReconciliationDiff:
        """
        从broker同步持仓和资金（先 diff 后比对，T0-1 双值并存不自动覆写）。

        T0-1 / C-1 双值并存规则：
          - 先 diff，记录 broker_cash 快照（仅比对，不覆写本地 _cash/_positions）；
          - diff 超阈 → 置 reconciliation_blocked=True + CRITICAL + 通知回调，
            本地账（book_cash / _positions）**保持不变**（不再被镜像抹平）；
          - diff 在容差内 → 才正常同步（旧行为，自动对齐）；
          - 要显式收敛到券商账 → 调 adopt_broker_ledger(reason, actor)（带审计）。

        Parameters
        ----------
        broker : BaseBroker
            券商实例（PaperBroker或真实券商）。
        cash_tolerance : float
            现金容差（元）。

        Returns
        -------
        ReconciliationDiff : 本次账实差异快照。
        """
        # T0-1：先 diff（不覆写），再决定是否同步
        diff = self.reconcile_vs_broker(broker, cash_tolerance=cash_tolerance)

        # 刷新 broker_cash 快照（双值并存：broker_cash 仅比对，不自动覆盖 book_cash）
        account = broker.get_account() or {}
        self._broker_cash = float(account.get("cash", 0.0) or 0.0)

        if diff.exceeds_threshold:
            # T0-1：差异超阈 → 置对账冻结 + CRITICAL + 通知回调，本地账保持不变
            self._reconciliation_blocked = True
            self._stats["last_reconciliation_diff"] = str(diff)
            self._stats["last_reconciliation_blocked_ts"] = time.time()
            logger.critical(
                f"[portfolio] 账实差异超容差 {diff}，置 reconciliation_blocked=True，"
                f"本地账(book_cash={self._cash:,.2f})不被券商快照(broker_cash={self._broker_cash:,.2f})覆写；"
                f"需人工显式 adopt_broker_ledger(reason, actor) 收敛"
            )
            self._notify_callbacks("__reconciliation_diff__")
            self._notify_callbacks("__reconciliation_blocked__")
            return diff

        # diff 在容差内 → 正常同步（旧行为，自动对齐）
        self._reconciliation_blocked = False

        # T0-3 健全性闸：券商持仓数/总资产相对上一快照骤降 >50% 或绝对值 0 → 拒覆盖 + CRITICAL
        broker_positions = broker.get_positions() or []
        broker_pos_count = len(broker_positions)
        broker_total_assets = self._broker_cash + sum(
            p.quantity * p.market_price for p in broker_positions
        )
        if self._last_sync_position_count > 0:
            # 与上一快照比较（不是第一次同步）
            pos_drop_ratio = 1.0 - (broker_pos_count / self._last_sync_position_count)
            asset_drop_ratio = 1.0 - (broker_total_assets / self._last_sync_total_assets) if self._last_sync_total_assets > 0 else 0.0
            if broker_pos_count == 0 or broker_total_assets == 0:
                logger.critical(
                    f"[portfolio] T0-3 健全性闸拒绝覆盖: 券商返回持仓数=0 或总资产=0 "
                    f"(上一快照: 持仓数={self._last_sync_position_count}, 总资产={self._last_sync_total_assets:,.2f})"
                )
                self._reconciliation_blocked = True
                self._notify_callbacks("__reconciliation_blocked__")
                return diff
            if pos_drop_ratio > 0.5 or asset_drop_ratio > 0.5:
                logger.critical(
                    f"[portfolio] T0-3 健全性闸拒绝覆盖: 持仓数骤降 {pos_drop_ratio:.0%} "
                    f"({self._last_sync_position_count}→{broker_pos_count}), "
                    f"总资产骤降 {asset_drop_ratio:.0%} "
                    f"({self._last_sync_total_assets:,.2f}→{broker_total_assets:,.2f})"
                )
                self._reconciliation_blocked = True
                self._notify_callbacks("__reconciliation_blocked__")
                return diff

        self._cash = self._broker_cash
        # T0-6：frozen_cash 缺键 → 保留原值（不用 0）+ CRITICAL（防虚增可用资金）
        if "frozen_cash" in account:
            self._frozen_cash = float(account.get("frozen_cash", 0.0) or 0.0)
        else:
            logger.critical(
                f"[portfolio] broker 账户 dict 缺 frozen_cash 键，保留原值={self._frozen_cash:,.2f}（T0-6 防虚增可用资金）"
            )

        # 同步持仓（broker_positions 已在 T0-3 健全性闸时获取，不重复调）
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

        # T0-3：更新上一快照（供下次健全性比较）
        self._last_sync_position_count = broker_pos_count
        self._last_sync_total_assets = broker_total_assets

        self._stats["last_update_time"] = time.time()
        logger.info(
            f"从broker同步完成(差异在容差内, 已自动对齐): 持仓={self.get_position_count()}只, "
            f"资金={self._cash:,.2f}, 总资产={self.get_total_assets():,.2f}"
        )
        self._notify_callbacks("__sync__")
        return diff

    def adopt_broker_ledger(self, reason: str, actor: str) -> None:
        """
        T0-1 显式收敛到券商账本（人工介入后调用，带审计）。

        双值并存规则下：diff 超阈时本地账不被自动覆写；人工核查确认券商账
        正确后，调本方法显式 adopt——把 broker_cash / broker 持仓写回本地账，
        并解除 reconciliation_blocked。

        Parameters
        ----------
        reason : str
            收敛原因（如"人工核查确认券商账正确"）。
        actor : str
            操作人（必填，审计留痕）。
        """
        if not reason or not actor:
            raise ValueError("adopt_broker_ledger 需要 reason 和 actor（审计必填）")
        logger.info(
            f"[portfolio] 显式 adopt_broker_ledger: actor={actor!r} reason={reason!r} "
            f"(book_cash {self._cash:,.2f} → broker_cash {self._broker_cash:,.2f})"
        )
        self._cash = self._broker_cash
        # 注意：持仓不在这里 adopt——持仓 adopt 需要从 broker 重新拉取，
        # 调用方应在 adopt 前先调 sync_from_broker（diff 超阈时它只比对不覆写），
        # 再调本方法收敛资金；持仓收敛由调用方显式处理。
        self._reconciliation_blocked = False
        self._stats["last_adopt_ts"] = time.time()
        self._stats["last_adopt_actor"] = actor
        self._stats["last_adopt_reason"] = reason
        self._notify_callbacks("__adopted__")

    # ═══════════════════════════════════════════
    #  A-7：事件驱动对账触发（R-02：实时核对由事件流触发，周期巡检仅兜底）
    # ═══════════════════════════════════════════

    def attach_reconciliation_event_trigger(self, bus: Any, broker: Any) -> List[str]:
        """挂载事件驱动对账：订阅 EventBus 成交/持仓变更事件，实时触发一次核对。

        R-02：持仓对账（A-7，MTTD<1min）的检测必须由事件流实时触发——每次
        ORDER_FILLED / POSITION_UPDATE 即用注入的 broker 数据源核对一次；周期巡检
        （盘后 cron）仅作漏检兜底，不作为检测来源。核对复用 reconcile_vs_broker：
        差异 → CRITICAL 告警（禁止静默覆盖），一致 → INFO 记录。

        Parameters
        ----------
        bus : EventBus
            事件总线实例。
        broker : BaseBroker
            券商数据源（与 reconcile_vs_broker 同一注入 broker）。

        Returns
        -------
        List[str] : 订阅 handler_id 列表（供 detach_reconciliation_event_trigger）。

        Raises
        ------
        ValueError: bus 或 broker 缺失时抛出（防御式，避免空挂载）。
        """
        from myquant.core.event_bus import EventType
        if bus is None or broker is None:
            raise ValueError("attach_reconciliation_event_trigger 需要 bus 与 broker 均非空")
        self._recon_broker = broker
        ids: List[str] = []
        for et in (EventType.ORDER_FILLED, EventType.POSITION_UPDATE):
            hid = bus.subscribe(et, self._on_reconciliation_event)
            ids.append(hid)
        self._recon_bus = bus
        self._recon_handler_ids = ids
        logger.info(
            "[portfolio] 事件驱动对账已挂载: account=%s "
            "events=ORDER_FILLED/POSITION_UPDATE handlers=%d",
            self._account_id, len(ids),
        )
        return ids

    def detach_reconciliation_event_trigger(self) -> int:
        """卸载事件驱动对账（退订已挂载的事件处理器）。

        Returns:
            int: 成功退订的 handler 数量。
        """
        bus = self._recon_bus
        ids = list(self._recon_handler_ids)
        removed = 0
        if bus is not None:
            for hid in ids:
                try:
                    if bus.unsubscribe(hid):
                        removed += 1
                except Exception as e:  # 退订失败留痕不抛
                    logger.warning("[portfolio] 退订对账事件 handler 失败: %s", e)
        self._recon_bus = None
        self._recon_broker = None
        self._recon_handler_ids = []
        return removed

    def _on_reconciliation_event(self, event: Any) -> None:
        """事件驱动对账回调：用注入的 broker 数据源核对一次。

        B-1：若已注入 AccountReconciler，则把核对投递到对账器的异步线程池
        （handler 立即返回，不阻塞事件流；同 account 在途去重）——核对仍复用
        ``reconcile_vs_broker``（A-7 告警路径兼容）。若未注入对账器（A-7 既有
        用法 / 既有单测），退回同步 ``reconcile_vs_broker``，保持既有告警与断言
        语义不变。

        防未来：只消费"已发生"的成交/持仓变更事件（业务发生时刻 event_ts），
        不引用任何未来数据。异常仅记日志，不阻断事件流/交易。

        Parameters
        ----------
        event : Event
            EventBus 事件（ORDER_FILLED / POSITION_UPDATE）。
        """
        broker = self._recon_broker
        if broker is None:
            return
        reconciler = self._account_reconciler
        if reconciler is not None:
            # B-1 异步路径：投递即返回，核对在对账器线程池执行（非阻塞事件流）
            try:
                reconciler.request_realtime_reconciliation(portfolio=self, broker=broker)
            except Exception as e:  # 投递异常不得阻断事件流
                logger.error("[portfolio] 异步对账投递异常（不阻断事件流）: %s", e)
            return
        # A-7 兼容路径：同步核对（既有单测依赖 publish 后立即告警的同步语义）
        try:
            self.reconcile_vs_broker(broker)
        except Exception as e:  # 对账异常不得阻断事件流
            logger.error("[portfolio] 事件驱动对账异常（不阻断事件流）: %s", e)

    def set_account_reconciler(self, reconciler: Any) -> None:
        """注入 B-1 账户对账器（AccountReconciler）。

        注入后：事件回调改为异步投递核对（非阻塞事件流）；PnL 防脏门据其
        ``is_frozen`` 状态拒绝脏 PnL 记录。传 None 可恢复 A-7 同步兜底路径。

        Parameters
        ----------
        reconciler : AccountReconciler | None
            账户对账器实例（鸭子类型，避免硬导入循环依赖）。
        """
        self._account_reconciler = reconciler
        logger.info(
            "[portfolio] B-1 账户对账器%s: account=%s",
            "已注入" if reconciler is not None else "已移除", self._account_id,
        )

    def set_alert_manager(self, alert_manager: Any) -> None:
        """注入对账差异告警管理器（G1 EX-12B）。

        注入后：账实差异超阈告警经该实例投递（category=reconciliation）。
        传 None 可恢复"告警跳过（warning 留痕）"降级路径。

        Parameters
        ----------
        alert_manager : Any | None
            告警管理器实例（鸭子类型，须有 ``alert(**kw) -> int``）。
        """
        self._alert_manager = alert_manager
        logger.info(
            "[portfolio] 对账告警管理器%s: account=%s",
            "已注入" if alert_manager is not None else "已移除", self._account_id,
        )

    @property
    def account_reconciler(self) -> Any:
        """当前注入的账户对账器（None=A-7 同步兜底路径）。"""
        return self._account_reconciler

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
        # KP-M04（T-30）：高危写操作准入——未审批拒绝（默认仅审计留痕，不阻断）
        guard = get_operation_guard()
        if guard is None:
            logger.warning("[portfolio] 未装配准入守卫（register_operation_guard），资金调拨默认放行（仅审计留痕）")
        else:
            ok, why = guard.check(
                "transfer_cash", operator="portfolio",
                action=f"transfer_cash {amount} -> {target_account}")
            if not ok:
                logger.error(f"[portfolio] 资金调拨被准入拒绝: {why}")
                return False
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


# ═══════════════════════════════════════════════
#  自测 Demo（__main__ 入口，可运行；不碰 quant.db，告警走内存桩）
# ═══════════════════════════════════════════════


class _SelfTestBroker:
    """自测用鸭子类型券商：仅提供对账所需 get_account / get_positions。"""

    def __init__(self, cash: float, positions: List[Position]) -> None:
        self._cash = float(cash)
        self._positions = positions

    def get_account(self) -> Dict[str, Any]:
        return {"cash": self._cash, "frozen_cash": 0.0}

    def get_positions(self) -> List[Position]:
        return self._positions


def _run_self_test() -> int:
    """portfolio.py 模块自测 Demo（A-7 持仓对账）。

    覆盖：默认容差取 R-04 配置真源（LOCKED=0）；一致不告警；差异 → CRITICAL
    告警含明细 + category + dedup_key；发布 ORDER_FILLED 事件触发核对并告警。

    Returns:
        int: 失败项数（0=全绿）。
    """
    from myquant.core.event_bus import Event, EventBus, EventType  # noqa: WPS433

    sent: List[Dict[str, Any]] = []

    class _StubAlertManager:
        """内存告警桩：记录 alert 调用，不碰 quant.db。"""

        def alert(self, **kw: Any) -> int:  # noqa: A003
            sent.append(kw)
            return 1

    stub_alert_manager = _StubAlertManager()

    passed = 0
    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed
        if ok:
            passed += 1
        else:
            failures.append(f"{name} ({detail})")

    # 1. 默认容差取配置真源（LOCKED=0）；一致 → 不告警
    p = LivePortfolio(initial_capital=100_000.0, alert_manager=stub_alert_manager)
    p._cash = 100_000.0
    b_ok = _SelfTestBroker(100_000.0, [])
    d = p.reconcile_vs_broker(b_ok)
    check("默认容差取配置=0", d.cash_tolerance == 0.0, f"tol={d.cash_tolerance}")
    check("一致快照不告警", d.exceeds_threshold is False and len(sent) == 0,
          f"sent={len(sent)}")

    # 2. 制造差异 → CRITICAL 告警含明细
    p2 = LivePortfolio(initial_capital=100_000.0, alert_manager=stub_alert_manager)
    p2._cash = 100_000.0
    p2._positions = {"600000.SH": Position(symbol="600000.SH", quantity=100.0)}
    b_diff = _SelfTestBroker(90_000.0, [])  # 现金差 10000 + 本地多仓 100
    d2 = p2.reconcile_vs_broker(b_diff)
    check("差异超阈", d2.exceeds_threshold is True, "")
    check("差异触发一条 CRITICAL 告警",
          len(sent) == 1 and sent[0]["level"] == SeverityLevel.CRITICAL,
          f"sent={len(sent)}")
    check("告警 category=reconciliation",
          bool(sent) and sent[0].get("category") == "reconciliation", "")
    check("告警 details 含现金差与持仓差异",
          bool(sent) and sent[0]["details"]["cash_diff"] == 10_000.0
          and "600000.SH" in sent[0]["details"]["position_diffs"], "")
    check("告警带 reconciliation: dedup_key",
          bool(sent) and sent[0].get("deduplication_key", "").startswith("reconciliation:"),
          "")

    # 3. 事件触发路径：发布 FILLED → 触发核对 → 告警
    bus = EventBus()
    p3 = LivePortfolio(initial_capital=100_000.0, alert_manager=stub_alert_manager)
    p3._cash = 100_000.0
    p3.attach_reconciliation_event_trigger(bus, b_diff)
    n_before = len(sent)
    bus.publish(Event(EventType.ORDER_FILLED, data={"symbol": "600000.SH"}))
    check("发布 FILLED 事件触发对账并告警", len(sent) == n_before + 1,
          f"sent={len(sent)}")

    total = passed + len(failures)
    print("=" * 60)
    print(f"portfolio.py 自测: {passed}/{total} 项通过")
    for f in failures:
        print("  FAIL:", f)
    print("=" * 60)
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
