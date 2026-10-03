# 文件名：myquant/live/brokers/qmt_broker.py
"""
qmt_broker.py - 迅投 QMT 券商适配器
====================================
QMT（迅投极速策略交易系统）适配器，接口 100% 对齐 BaseBroker。
mode="SIM" 下内置完整模拟撮合引擎；mode="LIVE" 下走真实 QMT SDK（xtquant）
通道（connect / submit_order / cancel_order / 回调回报 / 资金持仓查询全部实现）。

继承关系:
  ExecutionInterface (myquant/execution/base.py)
    └── BaseBroker (base_broker.py)
          └── QmtBroker (本文件)

核心设计（为实时行情及实盘无缝对接）:
  1. 双模式切换 — mode="SIM" 本地模拟撮合；mode="LIVE" 走 xtquant 真实通道。
     上层（BrokerManager / 引擎）只依赖 ExecutionInterface，切换零修改。
  2. 回调驱动 — LIVE 模式下 xtquant 推送的委托/成交/错误/断线回报，
     经 _QmtCallbackHandler 转发到 _on_qmt_* 系列方法，
     成交落地走 _execute_fill 统一入口（与 SIM 模式同构）。
  3. 订单映射 — 本地 order_id（uuid）与 QMT 委托编号（int）双向映射，
     回调回报按 QMT 委托编号反查本地订单，状态同步一致。
  4. 权威账本 — LIVE 模式下资金/持仓以 QMT 查询为准（query_stock_asset /
     query_stock_positions），本地不做资金冻结与持仓推算，避免双账本打架。
  5. 多账户预留 — account_id 参数 + get_account 返回 account_id 字段。

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数

防未来函数说明:
  - SIM 模式成交只依据【已发生】的行情数据（update_market_price 传入的当前价）
  - LIVE 模式成交价格与数量由券商撮合回报决定，本类不做任何价格假设
  - T+1 冻结只累计当日【已成交】买入量，可卖数量=持仓-当日买入

LIVE 模式环境要求:
  - 已安装 QMT/miniQMT 客户端并登录（极简模式 XtMiniQmt.exe）
  - 已安装 xtquant 库：pip install xtquant（支持 64 位 Python 3.6~3.14）
  - qmt_path 指向 QMT 安装目录下的 userdata_mini（券商端极简模式）
  - account_id 为资金账号（QMT 客户端的资金账号）

依赖: typing, time, myquant.live.brokers.base_broker,
      myquant.models.trade, myquant.core.logger
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.live.brokers.base_broker import BaseBroker, _locked
from myquant.models.trade import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Trade,
)

logger = get_logger(__name__)

# ════════════════════════════════════════════════════════════════════
#  真实 QMT SDK（xtquant）对接说明
# ════════════════════════════════════════════════════════════════════
# xtquant 库（QMT 官方 Python 接口，支持 64 位 Python 3.6~3.14）:
#   from xtquant import xttrader, xtconstant
#   from xtquant.xttype import StockAccount
#
# 关键对象与本类挂载点:
#   - 连接   : XtQuantTrader(qmt_path, session_id) → start() → connect()
#             挂载点: _qmt_connect()（connect 返回 0 表示成功）
#   - 账户   : StockAccount(account_id)             → 挂载点: _qmt_connect()
#   - 下单   : order_stock(account, symbol, order_type, order_volume,
#                         price_type, price, strategy_name, order_remark)
#             挂载点: _qmt_submit_order()（返回 QMT 委托编号）
#   - 撤单   : cancel_order_stock(account, order_id) → 挂载点: _qmt_cancel_order()
#   - 回报   : XtQuantTraderCallback.on_stock_order / on_stock_trade /
#              on_order_error / on_cancel_error / on_disconnected /
#              on_account_status
#             挂载点: _QmtCallbackHandler → _on_qmt_* 系列方法
#   - 资金   : query_stock_asset(account)   → 挂载点: _qmt_get_account()
#   - 持仓   : query_stock_positions(account) → 挂载点: _qmt_get_positions()
#
# 委托回报/成交回报中的 order_id 为 QMT 委托编号（int），
# 通过 _qmt_to_local / _local_to_qmt 双向映射关联本地 Order（uuid）。
# ════════════════════════════════════════════════════════════════════


class _QmtCallbackHandler:
    """
    QMT 交易回调处理器（LIVE 模式）

    将 xtquant 推送的委托/成交/错误/断线/账户状态回报转发给
    QmtBroker 的 _on_qmt_* 系列方法，实现真实回报与本地订单簿同步。

    注意：不继承 XtQuantTraderCallback 以避免 SIM 模式下强制导入 xtquant；
    运行时（LIVE）经 _import_xtquant 动态继承并实例化，行为与官方回调完全一致。
    """

    def __init__(self, broker: "QmtBroker") -> None:
        """绑定所属 QmtBroker 实例（回调用回调转发目标）。"""
        self._broker = broker

    def on_stock_order(self, response: Any) -> None:
        """委托回报：更新本地订单状态。"""
        self._broker._on_qmt_order(response)

    def on_stock_trade(self, response: Any) -> None:
        """成交回报：落地成交（_execute_fill 统一入口）。"""
        self._broker._on_qmt_trade(response)

    def on_order_error(self, response: Any) -> None:
        """委托错误回报：订单拒绝。"""
        self._broker._on_qmt_order_error(response)

    def on_cancel_error(self, response: Any) -> None:
        """撤单错误回报：记录错误。"""
        self._broker._on_qmt_cancel_error(response)

    def on_disconnected(self, client_id: int) -> None:
        """断线回报：连接状态置 False。"""
        self._broker._on_qmt_disconnected(client_id)

    def on_account_status(self, response: Any) -> None:
        """账户登录状态回报：更新登录状态。"""
        self._broker._on_qmt_account_status(response)

    def on_stock_asset(self, response: Any) -> None:
        """资产变动主推：同步本地资金缓存（QMT 账本权威）。"""
        self._broker._on_qmt_asset(response)

    def on_stock_position(self, response: Any) -> None:
        """持仓变动主推：记录日志（持仓以查询为准，不增量推算）。"""
        self._broker._on_qmt_position(response)

    def on_connected(self, client_id: int) -> None:
        """连接成功回报：置连接状态。"""
        self._broker._on_qmt_connected(client_id)


class QmtBroker(BaseBroker):
    """
    迅投 QMT 券商适配器

    Parameters
    ----------
    qmt_path : str
        QMT 客户端安装路径（LIVE 模式必填；SIM 模式仅作日志展示）
    account_id : str
        资金账号（多账户预留）
    mode : str
        "SIM" 模拟撮合（默认，无 QMT 环境可完整验证）
        "LIVE" 真实 QMT SDK（xtquant），需已安装 QMT 客户端并登录、
        已安装 xtquant 库、qmt_path/account_id 已配置
    initial_capital : float
        初始资金（元），仅 SIM 模式使用，默认 100 万
    commission_rate : float
        佣金费率（万2.5=0.00025），买卖双向
    min_commission : float
        单笔最低佣金（元），默认 5 元
    stamp_tax_rate : float
        印花税率（千1=0.001），仅卖出
    transfer_fee_rate : float
        过户费率（万0.1=0.00001），买卖双向
    slippage_mode : str
        滑点模式：ratio=按比例 / fixed=按固定金额（仅 SIM）
    slippage_value : float
        滑点数值（ratio 模式为万2=0.0002）
    enable_t1 : bool
        是否启用 T+1 交易限制（A股标准）
    lot_size : int
        最小交易单位（股），A股 1手=100股
    price_limit_pct : float
        涨跌停限制比例（普通股票 10%=0.10）
    """

    broker_name = "qmt_broker"
    broker_type = "qmt"

    # QMT 支持 A股 限价单与市价单
    supported_order_types: List[OrderType] = [OrderType.LIMIT, OrderType.MARKET]

    @staticmethod
    def _mask_account_id(account_id: str) -> str:
        """日志脱敏：#P3-2 资金账号仅保留前3后2，中间掩码；空账号返回 (未绑定)"""
        raw = str(account_id or "")
        if not raw:
            return "(未绑定)"
        if len(raw) <= 6:
            return "****"
        return f"{raw[:3]}****{raw[-2:]}"

    def __init__(
        self,
        qmt_path: str = "",
        account_id: str = "",
        mode: str = "SIM",
        initial_capital: float = 1_000_000.0,
        commission_rate: float = 0.00025,
        min_commission: float = 5.0,
        stamp_tax_rate: float = 0.001,
        transfer_fee_rate: float = 0.00001,
        slippage_mode: str = "ratio",
        slippage_value: float = 0.0002,
        enable_t1: bool = True,
        lot_size: int = 100,
        price_limit_pct: float = 0.10,
    ):
        super().__init__(name="qmt_broker")

        self._qmt_path = str(qmt_path or "")
        self._account_id = str(account_id or "")
        mode = str(mode or "SIM").upper()
        if mode not in ("SIM", "LIVE"):
            raise ValueError(f"mode 必须为 SIM 或 LIVE, 当前: {mode}")
        self._mode = mode

        # 资金参数（SIM 模式）
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.frozen_cash = 0.0

        # 交易成本参数
        self.commission_rate = float(commission_rate)
        self.min_commission = float(min_commission)
        self.stamp_tax_rate = float(stamp_tax_rate)
        self.transfer_fee_rate = float(transfer_fee_rate)
        self.slippage_mode = slippage_mode
        self.slippage_value = float(slippage_value)

        # 交易规则
        self.enable_t1 = bool(enable_t1)
        self.lot_size = int(lot_size)
        self.price_limit_pct = float(price_limit_pct)

        # 数据存（SIM 模式撮合）
        self._orders: Dict[str, Order] = {}
        self._trades: List[Trade] = []
        self._positions: Dict[str, Position] = {}
        self._t1_holdings: Dict[str, float] = {}   # 当日已成交买入（T+1 冻结）
        self._market_prices: Dict[str, float] = {}  # 已发生的最新行情价
        self._prev_close: Dict[str, float] = {}     # 昨收（首次行情时记录，涨跌停基准）

        # QMT 环境状态
        self._qmt_session_id = int(time.time()) % 1000000  # 会话标识（LIVE 用）
        self._xt_trader: Any = None                  # XtQuantTrader 实例（LIVE 连接后持有）
        self._qmt_account: Any = None                # StockAccount 实例（LIVE 用）
        self._qmt_callback: Any = None               # _QmtCallbackHandler 实例（LIVE 用）
        self._qmt_login_ok: bool = False             # QMT 账号登录状态（LIVE 用）
        self._qmt_to_local: Dict[int, str] = {}      # QMT 委托编号 → 本地 order_id
        self._local_to_qmt: Dict[str, int] = {}      # 本地 order_id → QMT 委托编号

        # 真实 SDK 可用性标志（connect 成功后置 True）
        self._qmt_sdk_ready = False

        logger.info(
            f"[qmt_broker] 初始化: mode={self._mode} "
            f"account_id={self._mask_account_id(self._account_id)} "
            f"qmt_path={self._qmt_path or '(未配置)'}"
        )

    # ═══════════════════════════════════════════════
    #  配置加载
    # ═══════════════════════════════════════════════

    @_locked
    def load_config(self, config: Dict[str, Any]) -> None:
        """
        从配置字典加载 QMT 券商配置

        支持结构:
          {
            "qmt": {
              "qmt_path": "...",
              "account_id": "...",
              "mode": "SIM",
              "enable_t1": true,
              ...
            }
          }
        或扁平结构直接传 QMT 参数。
        """
        if not config:
            return
        cfg = config.get("qmt", config) if isinstance(config, dict) else {}
        if not isinstance(cfg, dict):
            cfg = {}

        if cfg.get("qmt_path"):
            self._qmt_path = str(cfg["qmt_path"])
        # 兼容两种配置键：build_broker_from_config 用 "account"，load_config 亦兼容 "account_id"
        if cfg.get("account_id"):
            self._account_id = str(cfg["account_id"])
        elif cfg.get("account"):
            self._account_id = str(cfg["account"])
        mode = str(cfg.get("mode", self._mode)).upper()
        if mode in ("SIM", "LIVE"):
            self._mode = mode

        for key in ("commission_rate", "min_commission", "stamp_tax_rate",
                    "transfer_fee_rate", "slippage_value", "price_limit_pct"):
            if key in cfg and cfg[key] is not None:
                setattr(self, key, float(cfg[key]))
        for key in ("enable_t1",):
            if key in cfg and cfg[key] is not None:
                setattr(self, key, bool(cfg[key]))
        if cfg.get("slippage_mode") in ("ratio", "fixed"):
            self.slippage_mode = cfg["slippage_mode"]

        self._config = dict(cfg)
        logger.info(
            f"[qmt_broker] 配置加载: mode={self._mode} "
            f"account_id={self._mask_account_id(self._account_id)}"
        )

    # ═══════════════════════════════════════════════
    #  连接管理
    # ═══════════════════════════════════════════════

    @_locked
    def connect(self) -> bool:
        """
        连接 QMT

        SIM 模式: 本地模拟连接成功（不依赖 QMT 客户端）。
        LIVE 模式: 调用真实 SDK（_qmt_connect）：
          XtQuantTrader(qmt_path, session_id) → register_callback → start()
          → connect() → subscribe(acc)；任一环节失败则连接失败并记录日志。

        Returns
        -------
        bool : True=连接成功，False=连接失败
        """
        if self._connected:
            logger.info(f"[qmt_broker] 已连接，忽略重复连接")
            return True

        if self._mode == "LIVE":
            if not self._call_qmt_sdk("connect"):
                logger.error(
                    f"[qmt_broker] LIVE 连接失败: qmt_path={self._qmt_path or '(未配置)'} "
                    f"account_id={self._mask_account_id(self._account_id)}"
                )
                self._connected = False
                self._stats["last_error"] = "qmt connect failed"
                return False
            # LIVE 连接成功：_connected 由本方法设置（下方统一逻辑）
        else:
            # SIM 模式无外部依赖，直接标记连接成功
            pass

        # 连接成功（SIM 恒成功；LIVE 仅在 _call_qmt_sdk 返回 True 时到达）
        self._connected = True
        self._connect_time = time.time()
        self._last_heartbeat = time.time()
        self._stats["connect_count"] += 1
        logger.info(
            f"[qmt_broker] 连接成功: mode={self._mode} "
            f"account_id={self._mask_account_id(self._account_id)}"
        )
        return True

    @_locked
    def disconnect(self) -> None:
        """断开连接（LIVE 模式同时停止 xtquant 交易线程）"""
        if self._mode == "LIVE":
            try:
                self._call_qmt_sdk("disconnect")
            except Exception as e:  # noqa: BLE001 - 断开失败不阻断状态清理
                logger.warning(f"[qmt_broker] LIVE 断开异常: {e}")
        if self._connected:
            self._connected = False
            self._qmt_login_ok = False
            self._stats["disconnect_count"] += 1
            logger.info(f"[qmt_broker] 已断开连接")

    @_locked
    def heartbeat(self) -> bool:
        """
        心跳检查（保持连接活跃）

        SIM 模式: 更新心跳时间戳，始终返回 True。
        LIVE 模式: 主动探测 xtquant 连接/登录状态，异常视为断线。
        """
        if not self._connected:
            return False
        if self._mode == "LIVE":
            ok = self._call_qmt_sdk("heartbeat")
            if not ok:
                self._connected = False
                logger.warning("[qmt_broker] 心跳失败，连接置断线（等待自动重连）")
                return False
        self._last_heartbeat = time.time()
        return True

    def auto_reconnect(self) -> bool:
        """自动重连（断线时由调度器调用）"""
        if self._connected:
            return True
        ok = self.connect()
        if ok:
            self._stats["reconnect_count"] += 1
        return ok

    def health_check(self) -> Dict[str, Any]:
        """健康检查（连接状态/心跳延迟/订单队列/账户绑定）"""
        base = super().health_check()
        base.update(
            {
                "mode": self._mode,
                "account_id": self._account_id or None,
                "qmt_sdk_ready": self._qmt_sdk_ready,
                "active_orders": len(self.get_active_orders()),
                "pending_orders": sum(
                    1 for o in self._orders.values() if o.status is OrderStatus.SUBMITTED
                ),
            }
        )
        return base

    # ═══════════════════════════════════════════════
    #  订单操作（6 个抽象方法）
    # ═══════════════════════════════════════════════

    @_locked
    def submit_order(self, order: Order) -> Order:
        """
        提交订单

        SIM 模式流程:
          1. 校验订单合法性（复用基类 _validate_order）
          2. 资金/持仓检查（买入冻结资金，卖出检查可卖+T1）
          3. 注册订单到订单簿，状态 PENDING → SUBMITTED
          4. 触发订单更新回调

        LIVE 模式流程（_submit_order_live）:
          1. 校验订单合法性
          2. QMT 权威资金/持仓校验（query_stock_asset / query_stock_positions）
          3. 状态 PENDING → SUBMITTED 并注册本地订单簿
          4. order_stock 真实委托 → 建立 QMT 委托编号 ↔ 本地 order_id 映射
          5. 委托失败（废单/异常）→ 订单拒绝

        【防未来函数】订单提交只使用当前已知信息，不假设成交价；
        成交由后续行情回报驱动（_execute_fill）。

        幂等去重（任务43）:
          同一 order_id（如确定性 signal_id 派生）重复提交 → 直接返回已有订单，
          不重复校验、不重复委托（SIM 不重复撮合，LIVE 不重复报单，
          防桥接网络波动导致信号重发触发频繁报撤单）。
        """
        # ── 任务43：订单幂等去重（LIVE + SIM 统一，防桥接重发）──
        existing = self._orders.get(order.order_id)
        if existing is not None:
            self._stats.setdefault("dedup_skipped", 0)
            self._stats["dedup_skipped"] += 1
            logger.info(
                f"[qmt_broker] 重复订单忽略(幂等): {order.order_id} "
                f"status={existing.status.value}"
            )
            return existing

        # LIVE 模式走真实通道
        if self._mode == "LIVE":
            return self._submit_order_live(order)

        # 校验订单
        errors = self._validate_order(order)
        if errors:
            logger.error(f"[qmt_broker] 订单校验失败: {errors}")
            order.reject("; ".join(errors))
            self._record_order_rejected(order, "; ".join(errors))
            self._notify_order_update(order)
            return order

        # 资金检查（买入冻结）
        if order.side == OrderSide.BUY:
            estimated_price = order.price or self._market_prices.get(order.symbol, 0.0)
            estimated_cost = order.quantity * estimated_price
            if estimated_cost > self.available_cash:
                reason = f"资金不足: 需要={estimated_cost:.2f}, 可用={self.available_cash:.2f}"
                logger.warning(f"[qmt_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order
            self.frozen_cash += estimated_cost

        # 持仓检查（卖出 + T+1）
        if order.side == OrderSide.SELL:
            pos = self._positions.get(order.symbol)
            if pos is None or pos.quantity <= 0:
                reason = f"持仓不足: 无 {order.symbol} 持仓"
                logger.warning(f"[qmt_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order
            available_qty = pos.quantity
            if self.enable_t1:
                available_qty = pos.quantity - self._t1_holdings.get(order.symbol, 0.0)
            if order.quantity > available_qty:
                reason = (
                    f"可卖数量不足: 委托={order.quantity:.0f}, 可卖={available_qty:.0f}"
                    + (f" (T+1冻结={self._t1_holdings.get(order.symbol, 0.0):.0f})"
                       if self.enable_t1 else "")
                )
                logger.warning(f"[qmt_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return order

        # 注册订单到订单簿（PENDING → SUBMITTED）
        if not order.submit():
            reason = f"订单状态无法提交: {order.status}"
            logger.warning(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._notify_order_update(order)
            return order
        self._orders[order.order_id] = order
        # 统计统一由 _record_order_submit 递增（避免与基类双计）
        self._record_order_submit(order)

        logger.info(
            f"[qmt_broker] 订单已提交: {order.order_id} "
            f"{order.symbol} {order.side.value} {order.quantity:.0f} "
            f"@{order.price if order.price is not None else '市价'}"
        )
        self._notify_order_update(order)
        return order

    @_locked
    def _submit_order_live(self, order: Order) -> Order:
        """
        LIVE 模式真实下单（QMT 权威校验 + 真实委托 + 本地跟踪）

        流程:
          1. 未连接 → 订单拒绝（风控前移：不在断线时盲目下单）
          2. _validate_order 合法性校验
          3. QMT 权威资金/持仓校验（query_stock_asset / query_stock_positions），
             本地不冻结资金、不推算持仓（QMT 账本权威）
          4. order.submit() PENDING → SUBMITTED，注册本地订单簿
          5. order_stock 真实委托 → 返回 QMT 委托编号 → 建立双向映射
          6. 委托失败（None/异常）→ 订单拒绝并记录

        Parameters
        ----------
        order : Order
            待提交订单（未提交状态）

        Returns
        -------
        Order : 提交后的订单（SUBMITTED 或 REJECTED）
        """
        # 0. 幂等去重（双保险：入口 submit_order 已查，此处防直接调用路径）
        existing = self._orders.get(order.order_id)
        if existing is not None:
            self._stats.setdefault("dedup_skipped", 0)
            self._stats["dedup_skipped"] += 1
            logger.info(
                f"[qmt_broker] 重复订单忽略(幂等/LIVE): {order.order_id} "
                f"status={existing.status.value}"
            )
            return existing

        # 1. 连接检查（风控前移：断线时拒绝下单，不排队、不重发）
        if not self._connected or self._xt_trader is None:
            reason = "QMT 未连接，拒绝下单"
            logger.error(f"[qmt_broker] {reason}: order={order.order_id}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return order

        # 2. 合法性校验（复用基类）
        errors = self._validate_order(order)
        if errors:
            reason = "; ".join(errors)
            logger.error(f"[qmt_broker] 订单校验失败: {reason}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return order

        # 3. QMT 权威资金/持仓校验
        if not self._check_live_capacity(order):
            return order

        # 4. 注册本地订单簿（PENDING → SUBMITTED）
        if not order.submit():
            reason = f"订单状态无法提交: {order.status}"
            logger.warning(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._notify_order_update(order)
            return order
        self._orders[order.order_id] = order

        # 5. 真实委托（返回 QMT 委托编号；失败则拒绝）
        try:
            qmt_order_id = self._call_qmt_sdk("submit_order", order=order)
        except Exception as e:  # noqa: BLE001 - SDK 异常统一按委托失败处理
            reason = f"QMT 委托异常: {e}"
            logger.error(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return order
        if qmt_order_id is None:
            reason = "QMT 委托返回空（可能被拒/废单），等待 QMT 废单回报或人工核对"
            logger.error(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return order

        # 6. 建立 QMT 委托编号 ↔ 本地 order_id 双向映射（回报反查用）
        self._local_to_qmt[order.order_id] = int(qmt_order_id)
        self._qmt_to_local[int(qmt_order_id)] = order.order_id
        order.extra["qmt_order_id"] = int(qmt_order_id)

        # 统计统一由 _record_order_submit 递增（避免与基类双计）
        self._record_order_submit(order)
        logger.info(
            f"[qmt_broker] 订单已提交(LIVE): {order.order_id} "
            f"{order.symbol} {order.side.value} {order.quantity:.0f} "
            f"qmt_order_id={qmt_order_id}"
        )
        self._notify_order_update(order)
        return order

    @_locked
    def resubmit_order(self, order: Order) -> Order:
        """
        断线补单强制重发（任务45 / T3，覆盖基类默认实现）

        与 submit_order 的区别: 跳过订单幂等查重（submit_order 的幂等
        用于防【桥接信号重发】；本方法用于【对账确认券商端无此单】后的
        显式补发，必须真正重新委托，否则补发会被幂等拦截而空转）。

        SIM 模式: 无真实断线场景，走标准提交路径（本地幂等语义一致）。
        LIVE 模式: 走 _resubmit_order_live（校验/容量/重新委托/重建映射）。

        Parameters
        ----------
        order : Order
            补发订单（BrokerManager 传入的 PENDING 干净副本，order_id 不变）

        Returns
        -------
        Order : 补发后的订单（SUBMITTED 或 REJECTED）
        """
        if self._mode == "LIVE":
            return self._resubmit_order_live(order)
        return self.submit_order(order)

    @_locked
    def _resubmit_order_live(self, order: Order) -> Order:
        """
        LIVE 补发核心：校验 → 容量 → 重新委托 → 重建 QMT 映射（跳过幂等）

        与 _submit_order_live 的唯一区别: 不做订单幂等查重（第0步）。
        其余一致：连接检查 / 合法性校验 / QMT 权威资金持仓校验 /
        order.submit() PENDING → SUBMITTED / order_stock 真实委托 /
        双向映射重建（以本次补发的 QMT 委托编号为准）。

        【防未来函数】只依据已发生的本地状态与券商端对账结果，
        不假设成交价；成交由后续回报驱动。
        """
        # 1. 连接检查（断线时拒绝补单——补发前必须先重连成功）
        if not self._connected or self._xt_trader is None:
            reason = "QMT 未连接，拒绝补单（请先重连成功再触发对账）"
            logger.error(f"[qmt_broker] {reason}: order={order.order_id}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return order

        # 2. 合法性校验（复用基类）
        errors = self._validate_order(order)
        if errors:
            reason = "; ".join(errors)
            logger.error(f"[qmt_broker] 补单校验失败: {reason}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return order

        # 3. QMT 权威资金/持仓校验（补发前再次确认可成交条件）
        if not self._check_live_capacity(order):
            return order

        # 4. 注册本地订单簿（副本 PENDING → SUBMITTED，同 order_id 覆盖原单）
        if not order.submit():
            reason = f"订单状态无法提交: {order.status}"
            logger.warning(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._notify_order_update(order)
            return order
        self._orders[order.order_id] = order

        # 5. 真实委托（返回 QMT 委托编号；失败则拒绝）
        try:
            qmt_order_id = self._call_qmt_sdk("submit_order", order=order)
        except Exception as e:  # noqa: BLE001 - SDK 异常统一按委托失败处理
            reason = f"QMT 补单委托异常: {e}"
            logger.error(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return order
        if qmt_order_id is None:
            reason = "QMT 补单委托返回空（可能被拒/废单），等待废单回报或人工核对"
            logger.error(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return order

        # 6. 重建 QMT 委托编号 ↔ 本地 order_id 双向映射（以本次补发为准）
        self._local_to_qmt[order.order_id] = int(qmt_order_id)
        self._qmt_to_local[int(qmt_order_id)] = order.order_id
        order.extra["qmt_order_id"] = int(qmt_order_id)

        # 统计统一由 _record_order_submit 递增（避免与基类双计）
        self._record_order_submit(order)
        logger.warning(
            f"[qmt_broker] 订单补发成功(LIVE): {order.order_id} "
            f"{order.symbol} {order.side.value} {order.quantity:.0f} "
            f"qmt_order_id={qmt_order_id}（原委托在断线期间丢失）"
        )
        self._notify_order_update(order)
        return order

    @_locked
    def _check_live_capacity(self, order: Order) -> bool:
        """
        LIVE 模式资金/持仓权威校验（QMT 实时查询，风控前移）

        买入: 可用资金 ≥ 预估金额（限价单按委托价，市价单按最新价未知→仅下限校验）
        卖出: 可用持仓（可卖数量）≥ 委托数量

        校验失败 → 订单拒绝并返回 False；成功返回 True。

        Parameters
        ----------
        order : Order
            待校验订单

        Returns
        -------
        bool : True=通过，False=拒绝
        """
        try:
            if order.side == OrderSide.BUY:
                asset = self._qmt_get_asset()
                available = float(getattr(asset, "cash", 0.0) or 0.0)
                estimated_price = order.price if order.price and order.price > 0 else 0.0
                estimated_cost = order.quantity * estimated_price
                if estimated_price > 0 and estimated_cost > available:
                    reason = (
                        f"资金不足(QMT): 需要≈{estimated_cost:.2f}, "
                        f"可用={available:.2f}"
                    )
                    logger.warning(f"[qmt_broker] {reason}")
                    order.reject(reason)
                    self._record_order_rejected(order, reason)
                    self._notify_order_update(order)
                    return False
                self.cash = float(getattr(asset, "cash", self.cash) or self.cash)
                self.frozen_cash = float(
                    getattr(asset, "frozen_cash", self.frozen_cash) or self.frozen_cash
                )
                return True

            # 卖出：可用持仓校验
            qmt_positions = self._qmt_get_positions_raw()
            available_qty = 0.0
            for pos in qmt_positions:
                code = str(getattr(pos, "stock_code", "") or "")
                if code == order.symbol:
                    available_qty = float(
                        getattr(pos, "can_use_volume", 0.0) or 0.0
                    )
                    break
            if order.quantity > available_qty:
                reason = (
                    f"可卖数量不足(QMT): 委托={order.quantity:.0f}, "
                    f"可用={available_qty:.0f}"
                )
                logger.warning(f"[qmt_broker] {reason}")
                order.reject(reason)
                self._record_order_rejected(order, reason)
                self._notify_order_update(order)
                return False
            return True
        except Exception as e:  # noqa: BLE001 - 查询失败按校验不通过处理（安全第一）
            reason = f"QMT 资金/持仓查询异常: {e}"
            logger.error(f"[qmt_broker] {reason}")
            order.reject(reason)
            self._record_order_rejected(order, reason)
            self._notify_order_update(order)
            return False

    @_locked
    def cancel_order(self, order_id: str) -> bool:
        """
        撤单（仅可撤活跃订单）

        SIM 模式: 本地直接置 CANCELLED。
        LIVE 模式: 通过 QMT 委托编号 cancel_order_stock 发起撤单，
        撤单结果以 on_stock_order 回报为准（异步确认，本方法仅表示撤单请求已受理）。
        """
        order = self._orders.get(order_id)
        if order is None or not order.status.is_active:
            logger.warning(f"[qmt_broker] 撤单失败: 订单不存在或非活跃 {order_id}")
            return False

        if self._mode == "LIVE":
            try:
                ret = self._call_qmt_sdk("cancel_order", order_id=order_id)
            except Exception as e:  # noqa: BLE001 - SDK 异常按撤单失败处理
                logger.error(f"[qmt_broker] 撤单异常: {e}")
                self._stats["last_error"] = f"cancel_order: {e}"
                return False
            if ret != 0:
                logger.error(f"[qmt_broker] 撤单失败(QMT): order_id={order_id} ret={ret}")
                self._stats["last_error"] = f"cancel_order ret={ret}"
                return False
            logger.info(f"[qmt_broker] 撤单请求已受理(QMT): {order_id}（等待回报确认）")
            return True

        # SIM 模式：本地直接撤销
        order.cancel()
        self._record_order_cancelled(order)
        logger.info(f"[qmt_broker] 撤单成功: {order_id}")
        self._notify_order_update(order)
        return True

    @_locked
    def get_order(self, order_id: str) -> Optional[Order]:
        """查询订单（本地订单簿；LIVE 模式可扩展为 SDK 查询）"""
        return self._orders.get(order_id)

    @_locked
    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """查询活跃订单（可筛选标的）"""
        active = [o for o in self._orders.values() if o.status.is_active]
        if symbol:
            active = [o for o in active if o.symbol == symbol]
        return active

    @_locked
    def query_broker_orders(
        self, symbol: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """
        查询 QMT 端当日委托状态快照（补单对账用，任务45 / T3 断线补单）

        LIVE 模式: 真实查询 QMT 当日委托（query_stock_orders），
          返回 [{"order_id": 本地订单ID（由 order_remark 反解）,
                 "qmt_order_id": int|None, "status": OrderStatus,
                 "symbol": str, "quantity": float}, ...]；
          查询失败返回 None（保守语义：调用方【不补发】，防重复报单）。
        SIM 模式: 本地订单簿即券商端（无真实断线场景），走基类实现。

        Parameters
        ----------
        symbol : str | None
            可选，按标的筛选

        Returns
        -------
        Optional[List[Dict[str, Any]]] : 券商端当日委托快照；查询失败返回 None
        """
        if self._mode == "LIVE":
            raw_list = self._call_qmt_sdk("query_orders", symbol=symbol or "")
            if raw_list is None:
                return None
            return self._map_qmt_orders(raw_list)
        return super().query_broker_orders(symbol)

    @_locked
    def get_trades(self, symbol: Optional[str] = None) -> List[Trade]:
        """查询成交记录"""
        trades = list(self._trades)
        if symbol:
            trades = [t for t in trades if t.symbol == symbol]
        return trades

    @_locked
    def get_positions(self) -> List[Position]:
        """
        查询持仓

        SIM 模式: 本地模拟持仓。
        LIVE 模式: 真实查询 QMT（query_stock_positions → Position 列表）。
        """
        if self._mode == "LIVE":
            positions: List[Position] = []
            try:
                raw_list = self._call_qmt_sdk("get_positions")
                for raw in raw_list or []:
                    pos = self._map_qmt_position(raw)
                    if pos is not None:
                        positions.append(pos)
            except Exception as e:  # noqa: BLE001 - 查询失败返回空列表并记录
                logger.error(f"[qmt_broker] 持仓查询异常: {e}")
                self._stats["last_error"] = f"get_positions: {e}"
            return positions
        return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[Position]:
        """查询单标的持仓（LIVE 模式从 QMT 实时查询）"""
        for pos in self.get_positions():
            if pos.symbol == symbol:
                return pos
        return None

    @_locked
    def get_account(self) -> Dict[str, Any]:
        """
        查询账户资金

        SIM 模式: 本地账户 + QMT 特有字段（account_id）。
        LIVE 模式: 真实查询 QMT（query_stock_asset）。
        """
        if self._mode == "LIVE":
            try:
                asset = self._call_qmt_sdk("get_account")
                if asset is None:
                    raise ValueError("QMT 资产查询返回空")
                market_value = float(getattr(asset, "market_value", 0.0) or 0.0)
                total_asset = float(getattr(asset, "total_asset", 0.0) or 0.0)
                cash = float(getattr(asset, "cash", 0.0) or 0.0)
                frozen_cash = float(getattr(asset, "frozen_cash", 0.0) or 0.0)
                return {
                    "cash": round(cash, 2),
                    "available_cash": round(cash - frozen_cash, 2),
                    "frozen_cash": round(frozen_cash, 2),
                    "market_value": round(market_value, 2),
                    "total_assets": round(total_asset, 2),
                    "account_id": self._account_id,
                    "mode": self._mode,
                }
            except Exception as e:  # noqa: BLE001 - 查询失败返回空账户并记录
                logger.error(f"[qmt_broker] 资产查询异常: {e}")
                self._stats["last_error"] = f"get_account: {e}"
                return {
                    "cash": 0.0,
                    "available_cash": 0.0,
                    "frozen_cash": 0.0,
                    "market_value": 0.0,
                    "total_assets": 0.0,
                    "account_id": self._account_id,
                    "mode": self._mode,
                }

        market_value = sum(
            p.quantity * p.market_price
            for p in self._positions.values()
            if p.quantity > 0
        )
        account = {
            "cash": round(self.cash, 2),
            "available_cash": round(self.available_cash, 2),
            "frozen_cash": round(self.frozen_cash, 2),
            "market_value": round(market_value, 2),
            "total_assets": round(self.cash + market_value, 2),
            "account_id": self._account_id,
            "mode": self._mode,
        }
        return account

    # ═══════════════════════════════════════════════
    #  行情订阅（为实时行情预留）
    # ═══════════════════════════════════════════════

    def subscribe(
        self,
        symbol: str,
        callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        data_type: str = "tick",
    ) -> bool:
        """
        订阅行情

        SIM 模式: 记录订阅列表（无外部行情源，由 update_market_price 喂入）。
        LIVE 模式: 记录订阅列表（真实行情推送由 data_feed 经 xtdata 通道接入，
        与交易通道分离；本方法只登记标的，确保订阅状态一致）。

        Returns
        -------
        bool : 是否订阅成功
        """
        self._subscribed_symbols.add(symbol)
        if callback is not None:
            self._market_data_callbacks.setdefault(symbol, []).append(callback)
        logger.info(
            f"[qmt_broker] 订阅行情: {symbol} ({data_type}) "
            f"mode={self._mode}"
        )
        return True

    def unsubscribe(self, symbol: str) -> bool:
        """
        取消订阅（SIM/LIVE 通用：移除本地订阅登记）

        LIVE 模式的行情推送由 data_feed 经 xtdata 通道管理，
        此处仅同步本地订阅状态，避免与交易通道重复管理。
        """
        existed = symbol in self._subscribed_symbols
        self._subscribed_symbols.discard(symbol)
        if symbol in self._market_data_callbacks:
            del self._market_data_callbacks[symbol]
        if existed:
            logger.info(f"[qmt_broker] 取消订阅: {symbol}")
        return existed

    def is_subscribed(self, symbol: str) -> bool:
        return symbol in self._subscribed_symbols

    def get_subscribed_symbols(self) -> List[str]:
        return sorted(self._subscribed_symbols)

    # ═══════════════════════════════════════════════
    #  模拟环境（SIM 模式撮合驱动）
    # ═══════════════════════════════════════════════

    @_locked
    def update_market_price(
        self,
        symbol: str,
        price: float,
        timestamp: Optional[float] = None,
    ) -> List[Trade]:
        """
        更新市场价（模拟实时行情），触发限价单撮合

        ★★★ 为实时行情预留的核心接口 ★★★
        实盘时由 data_feed 模块收到行情推送后调用此方法（与 PaperBroker 同接口）。
        收到行情后检查该标的全部活跃订单，价格触及申报价则成交。

        【防未来函数】只使用传入的当前价撮合，不预判未来价格。

        Parameters
        ----------
        symbol : str
            标的代码
        price : float
            当前市场价（已发生行情）
        timestamp : float | None
            时间戳（默认当前时间）

        Returns
        -------
        List[Trade] : 本次价格更新产生的成交记录
        """
        if timestamp is None:
            timestamp = time.time()

        # 首次行情记录为昨收基准（涨跌停计算用）
        if symbol not in self._prev_close or self._prev_close[symbol] <= 0:
            self._prev_close[symbol] = price

        # 更新市场价
        self._market_prices[symbol] = price

        # 更新持仓市场价
        pos = self._positions.get(symbol)
        if pos:
            pos.update_market_price(price)

        # 触发行情推送回调
        self._notify_market_data(symbol, {"price": price, "timestamp": timestamp})

        # 撮合当前标的的活跃订单
        trades: List[Trade] = []
        for order in self.get_active_orders(symbol=symbol):
            trade = self._match_order(order, price, timestamp)
            if trade is not None:
                trades.append(trade)
        return trades

    def simulate_fill(
        self,
        order_id: str,
        price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> Optional[Trade]:
        """
        强制成交指定订单（测试/调试用，跳过行情撮合条件）

        Parameters
        ----------
        order_id : str
            订单ID
        price : float | None
            成交价（默认取订单价或当前市场价）
        quantity : float | None
            成交数量（默认全量剩余）

        Returns
        -------
        Trade | None : 成交记录
        """
        order = self._orders.get(order_id)
        if order is None or not order.status.is_active:
            logger.warning(f"[qmt_broker] 强制成交失败: 订单不存在或非活跃 {order_id}")
            return None

        fill_price = price
        if fill_price is None:
            fill_price = order.price or self._market_prices.get(order.symbol)
        if fill_price is None:
            logger.warning(f"[qmt_broker] 强制成交失败: 无成交价 {order_id}")
            return None

        fill_qty = quantity if quantity is not None else order.remaining_qty
        return self._execute_fill(order, fill_price, time.time(), fill_qty)

    def set_market_price(self, symbol: str, price: float) -> None:
        """直接设置市场价（不触发撮合，初始化用）"""
        self._market_prices[symbol] = price
        if symbol not in self._prev_close or self._prev_close[symbol] <= 0:
            self._prev_close[symbol] = price

    # ═══════════════════════════════════════════════
    #  内部：撮合与成交回报（真实 QMT 回调同构）
    # ═══════════════════════════════════════════════

    @_locked
    def _match_order(
        self,
        order: Order,
        current_price: float,
        timestamp: float,
    ) -> Optional[Trade]:
        """
        撮合单个订单（限价触及/市价即时）

        买入: 当前价 <= 限价 → 成交（成交价=当前价）
        卖出: 当前价 >= 限价 → 成交（成交价=当前价）
        市价: 按当前价成交
        """
        if order.side == OrderSide.BUY:
            if order.order_type == OrderType.MARKET:
                return self._execute_fill(order, current_price, timestamp)
            if order.price is not None and current_price <= order.price:
                return self._execute_fill(order, current_price, timestamp)
        else:
            if order.order_type == OrderType.MARKET:
                return self._execute_fill(order, current_price, timestamp)
            if order.price is not None and current_price >= order.price:
                return self._execute_fill(order, current_price, timestamp)
        return None

    @_locked
    def _execute_fill(
        self,
        order: Order,
        fill_price: float,
        timestamp: float,
        fill_qty: Optional[float] = None,
    ) -> Optional[Trade]:
        """
        执行成交（统一成交回报入口）

        ★★★ SIM 与 LIVE 同构 ★★★
        SIM 模式: update_market_price 驱动撮合后调用本方法；
        LIVE 模式: QMT on_stock_trade 成交回报经 _on_qmt_trade 调用本方法。
        两种模式成交落地逻辑一致（创建记录/更新订单状态/触发回调），
        LIVE 模式跳过本地资金/持仓更新（QMT 账本权威，本地不推算）。

        流程:
          1. 涨跌停检查（仅 SIM；LIVE 由券商撮合决定）
          2. 创建成交记录
          3. 计算手续费（仅 SIM 估算；LIVE 以 QMT 实际扣费为准）
          4. 更新订单状态
          5. 更新持仓与资金（仅 SIM；LIVE 本地不维护账本）
          6. 触发回调
        """
        if not order.status.is_active:
            return None

        fill_quantity = fill_qty if fill_qty is not None else order.remaining_qty
        if fill_quantity <= 0:
            return None

        # LIVE 模式（真实 QMT 成交回报）：跳过本地涨跌停/账本推算
        if self._mode == "LIVE":
            trade = Trade(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=fill_quantity,
                price=round(fill_price, 4),
                timestamp=timestamp,
                strategy_id=order.strategy_id,
            )
            # 佣金/滑点以 QMT 实际扣费为准，本地不估算
            trade.commission = 0.0
            trade.slippage = 0.0
            # 更新订单状态（支持分批）
            order.fill(fill_quantity, fill_price)
            if order.is_filled:
                self._record_order_filled(order)
            # 注册成交（本地记录仅用于订单状态跟踪与回调通知）
            self._trades.append(trade)
            self._stats["total_trades"] += 1
            logger.info(
                f"[qmt_broker] 成交(LIVE): {order.symbol} {order.side.value} "
                f"{fill_quantity:.0f}@{fill_price:.2f} "
                f"（费用以QMT结算为准）"
            )
            self._notify_order_update(order)
            self._notify_trade(trade)
            return trade

        # SIM 模式：涨跌停检查（基于已发生行情与昨收）
        if not self._check_price_limit(order.symbol, fill_price, order.side):
            logger.debug(
                f"[qmt_broker] 涨跌停限制，未成交: {order.symbol} "
                f"price={fill_price:.2f} side={order.side.value}"
            )
            return None

        # 创建成交记录
        trade = Trade(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=fill_quantity,
            price=round(fill_price, 4),
            timestamp=timestamp,
            strategy_id=order.strategy_id,
        )
        trade.commission = self._calc_commission(trade)
        trade.slippage = self._calc_slippage_amount(fill_price, fill_quantity)

        # 更新订单状态（支持分批）
        order.fill(fill_quantity, fill_price)
        if order.is_filled:
            self._record_order_filled(order)

        # 更新持仓与资金（仅 SIM 本地账本）
        self._apply_trade(trade, order)

        # 注册成交
        self._trades.append(trade)
        self._stats["total_trades"] += 1

        logger.info(
            f"[qmt_broker] 成交: {order.symbol} {order.side.value} "
            f"{fill_quantity:.0f}@{fill_price:.2f} "
            f"手续费={trade.commission:.2f}"
        )

        # 触发回调
        self._notify_order_update(order)
        self._notify_trade(trade)
        return trade

    @_locked
    def _apply_trade(self, trade: Trade, order: Order) -> None:
        """应用成交到持仓和资金"""
        symbol = trade.symbol
        pos = self._positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self._positions[symbol] = pos

        # 释放冻结资金（买入订单）
        if order.side == OrderSide.BUY:
            frozen_to_release = trade.quantity * (order.price or trade.price)
            self.frozen_cash = max(0.0, self.frozen_cash - frozen_to_release)

        # 更新持仓
        pos.apply_fill(trade.quantity, trade.price, trade.side)

        # 更新资金
        total_cost = trade.quantity * trade.price + trade.commission
        if trade.side == OrderSide.BUY:
            self.cash -= total_cost
            if self.enable_t1:
                self._t1_holdings[symbol] = self._t1_holdings.get(symbol, 0.0) + trade.quantity
        else:
            self.cash += trade.quantity * trade.price - trade.commission

        # 更新市场价
        pos.update_market_price(trade.price)

    def _calc_commission(self, trade: Trade) -> float:
        """计算手续费（A股写实标准：佣金+过户费+印花税）"""
        amount = trade.notional
        commission = max(amount * self.commission_rate, self.min_commission)
        commission += amount * self.transfer_fee_rate
        if trade.side == OrderSide.SELL:
            commission += amount * self.stamp_tax_rate
        return round(commission, 2)

    def _calc_slippage_amount(self, price: float, quantity: float) -> float:
        """计算滑点金额（仅 SIM 模式统计用）"""
        notional = price * quantity
        if self.slippage_mode == "ratio":
            return round(notional * self.slippage_value, 2)
        return round(self.slippage_value, 2)

    def _check_price_limit(
        self,
        symbol: str,
        price: float,
        side: OrderSide,
    ) -> bool:
        """
        涨跌停检查

        涨停价 = 昨收 * (1 + price_limit_pct)
        跌停价 = 昨收 * (1 - price_limit_pct)

        买入: 价格不能超过涨停价
        卖出: 价格不能低于跌停价

        没有昨收价时不限制（模拟行情初始状态）。
        """
        prev_close = self._prev_close.get(symbol)
        if prev_close is None or prev_close <= 0:
            return True
        limit_up = prev_close * (1 + self.price_limit_pct)
        limit_down = prev_close * (1 - self.price_limit_pct)
        if side == OrderSide.BUY:
            return price <= limit_up
        return price >= limit_down

    # ═══════════════════════════════════════════════
    #  账户/持仓便捷属性
    # ═══════════════════════════════════════════════

    @property
    def available_cash(self) -> float:
        """可用资金 = 总资金 - 冻结资金"""
        return self.cash - self.frozen_cash

    @property
    def total_assets(self) -> float:
        """总资产 = 现金 + 持仓市值"""
        market_value = sum(
            p.quantity * p.market_price
            for p in self._positions.values()
            if p.quantity > 0
        )
        return self.cash + market_value

    @property
    def equity(self) -> float:
        """净资产（与总资产一致，QMT 语义）"""
        return self.total_assets

    @property
    def position_count(self) -> int:
        """持仓数量"""
        return len([p for p in self._positions.values() if p.quantity > 0])

    # ═══════════════════════════════════════════════
    #  T+1 结算
    # ═══════════════════════════════════════════════

    @_locked
    def settle_t1(self) -> None:
        """
        T+1 结算：清空当日买入冻结记录（日终/次日开盘前调用）

        说明: 真实 QMT 由券商结算，本方法仅 SIM 模式模拟日终结算，
        便于多日连续回测。防未来：只释放【已发生】的当日买入。
        """
        self._t1_holdings.clear()
        logger.info("[qmt_broker] T+1 结算完成（当日买入冻结已释放）")

    # ═══════════════════════════════════════════════
    #  真实 SDK 通道（LIVE 模式）
    # ═══════════════════════════════════════════════

    def _import_xtquant(self) -> tuple:
        """
        惰性导入 xtquant（仅 LIVE 模式需要；SIM 模式不引入外部依赖）

        Returns
        -------
        tuple : (xtconstant, XtQuantTrader, XtQuantTraderCallback, StockAccount)

        Raises
        ------
        RuntimeError
            xtquant 未安装时抛出（提示 pip install xtquant）
        """
        try:
            from xtquant import xtconstant
            from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
            from xtquant.xttype import StockAccount
        except ImportError as e:
            raise RuntimeError(
                "[qmt_broker] LIVE 模式需要 xtquant 库（QMT 官方 Python 接口），"
                "请先执行: pip install xtquant（支持 64 位 Python 3.6~3.14）"
            ) from e
        return xtconstant, XtQuantTrader, XtQuantTraderCallback, StockAccount

    def _call_qmt_sdk(self, method: str, **kwargs: Any) -> Any:
        """
        真实 QMT SDK 调用统一入口（LIVE 模式）

        method 分支:
          connect       → _qmt_connect() 建立 XtQuantTrader 连接并订阅账户
          disconnect    → _qmt_disconnect() 停止交易线程
          heartbeat     → _qmt_heartbeat() 探测连接/登录状态
          submit_order  → _qmt_submit_order(order) 真实委托，返回 QMT 委托编号
          cancel_order  → _qmt_cancel_order(order_id) 撤单，返回撤单结果码
          get_account   → _qmt_get_asset() 查询资金
          get_positions → _qmt_get_positions_raw() 查询持仓
          query_orders  → _qmt_query_orders(symbol) 查询当日委托（补单对账用）
          subscribe/unsubscribe → True（行情通道由 data_feed 经 xtdata 接入）

        Parameters
        ----------
        method : str
            上述方法名
        **kwargs : Any
            分支参数（order / order_id / symbol）

        Returns
        -------
        Any : 各分支返回值；不支持的方法抛 ValueError

        Raises
        ------
        RuntimeError
            SIM 模式调用本方法（上层应已按 mode 分支）
        ValueError
            未知 method
        """
        if self._mode != "LIVE":
            raise RuntimeError(
                f"[qmt_broker] _call_qmt_sdk 仅 LIVE 模式可用，当前 mode={self._mode}"
            )

        if method == "connect":
            return self._qmt_connect()
        if method == "disconnect":
            return self._qmt_disconnect()
        if method == "heartbeat":
            return self._qmt_heartbeat()
        if method == "submit_order":
            return self._qmt_submit_order(kwargs["order"])
        if method == "cancel_order":
            return self._qmt_cancel_order(kwargs["order_id"])
        if method == "get_account":
            return self._qmt_get_asset()
        if method == "get_positions":
            return self._qmt_get_positions_raw()
        if method == "query_orders":
            return self._qmt_query_orders(str(kwargs.get("symbol", "") or ""))
        if method in ("subscribe", "unsubscribe"):
            # 行情通道由 data_feed 经 xtdata 独立接入，交易通道仅登记订阅
            return True
        raise ValueError(f"[qmt_broker] 不支持的 QMT SDK 方法: {method}")

    def _qmt_connect(self) -> bool:
        """
        建立 XtQuantTrader 连接并订阅账户（LIVE 连接核心）

        流程:
          1. 校验 qmt_path / account_id 非空
          2. 惰性导入 xtquant
          3. XtQuantTrader(qmt_path, session_id) → register_callback → start()
          4. connect() 返回 0 表示连接成功
          5. subscribe(acc) 返回 0 表示账户订阅成功
          6. 全部成功 → _qmt_sdk_ready=True / _qmt_login_ok=True

        Returns
        -------
        bool : True=连接成功，False=失败（已记录日志）
        """
        if not self._qmt_path:
            logger.error("[qmt_broker] qmt_path 未配置，无法连接 LIVE")
            return False
        if not self._account_id:
            logger.error("[qmt_broker] account_id 未配置，无法连接 LIVE")
            return False

        try:
            xtconstant, XtQuantTrader, XtQuantTraderCallback, StockAccount = (
                self._import_xtquant()
            )
            trader = XtQuantTrader(self._qmt_path, self._qmt_session_id)
            account = StockAccount(self._account_id)
            callback = _QmtCallbackHandler(self)

            # 动态创建继承官方回调基类的处理器（满足协议且绑定 broker 引用）
            LiveCallbackCls = type(
                "_LiveQmtCallback",
                (XtQuantTraderCallback,),
                {
                    "on_stock_order": _QmtCallbackHandler.on_stock_order,
                    "on_stock_trade": _QmtCallbackHandler.on_stock_trade,
                    "on_order_error": _QmtCallbackHandler.on_order_error,
                    "on_cancel_error": _QmtCallbackHandler.on_cancel_error,
                    "on_disconnected": _QmtCallbackHandler.on_disconnected,
                    "on_account_status": _QmtCallbackHandler.on_account_status,
                    "on_stock_asset": _QmtCallbackHandler.on_stock_asset,
                    "on_stock_position": _QmtCallbackHandler.on_stock_position,
                    "on_connected": _QmtCallbackHandler.on_connected,
                    "__init__": lambda self: None,
                },
            )
            live_callback = LiveCallbackCls()
            live_callback._broker = self  # 绑定 broker 引用（协议方法经 broker 转发）

            trader.register_callback(live_callback)
            trader.start()

            connect_result = trader.connect()
            if connect_result != 0:
                logger.error(
                    f"[qmt_broker] QMT connect 失败: result={connect_result} "
                    f"(0=成功, 非0=失败；请确认 QMT 客户端已登录极简模式)"
                )
                trader.stop()
                return False

            subscribe_result = trader.subscribe(account)
            if subscribe_result != 0:
                logger.error(
                    f"[qmt_broker] QMT 账户订阅失败: result={subscribe_result} "
                    f"(0=成功；请确认账号已开通 QMT 交易权限)"
                )
                trader.stop()
                return False

            self._xt_trader = trader
            self._qmt_account = account
            self._qmt_callback = live_callback
            self._qmt_sdk_ready = True
            self._qmt_login_ok = True
            logger.info(
                f"[qmt_broker] LIVE 连接成功: session_id={self._qmt_session_id} "
                f"account_id={self._mask_account_id(self._account_id)}"
            )
            return True
        except Exception as e:  # noqa: BLE001 - 连接失败统一记录并返回 False
            logger.error(f"[qmt_broker] LIVE 连接异常: {e}")
            self._stats["last_error"] = f"qmt_connect: {e}"
            return False

    def _qmt_disconnect(self) -> None:
        """停止 xtquant 交易线程（断开 LIVE 连接）"""
        trader = self._xt_trader
        if trader is None:
            return
        try:
            trader.stop()
        except Exception as e:  # noqa: BLE001 - 停止失败仅记录
            logger.warning(f"[qmt_broker] QMT stop 异常: {e}")
        self._xt_trader = None
        self._qmt_sdk_ready = False
        self._qmt_login_ok = False
        logger.info("[qmt_broker] LIVE 连接已断开")

    def _qmt_heartbeat(self) -> bool:
        """
        LIVE 心跳探测：主动查询账户状态确认连接/登录正常

        Returns
        -------
        bool : True=连接正常
        """
        trader = self._xt_trader
        if trader is None:
            return False
        try:
            status_list = trader.query_account_status()
            if not status_list:
                return False
            # 任一账号状态 OK 即视为连接正常（ACCOUNT_STATUS_OK=0）
            for st in status_list:
                status = int(getattr(st, "status", -1))
                if status == 0:
                    return True
            return False
        except Exception as e:  # noqa: BLE001 - 探测异常按断线处理
            logger.warning(f"[qmt_broker] 心跳探测异常: {e}")
            return False

    def _qmt_submit_order(self, order: Order) -> Optional[int]:
        """
        QMT 真实委托（order_stock），返回 QMT 委托编号

        映射:
          side       : BUY→STOCK_BUY / SELL→STOCK_SELL
          order_type : LIMIT→FIX_PRICE / MARKET→MARKET_PEER_PRICE_FIRST
          price      : 限价单必须传价；市价单传 0
          order_remark : 写入本地 order_id（QMT 备注限 24 英文字符，16 位 hex 可容纳），
                        便于跨进程/事后追溯

        Parameters
        ----------
        order : Order
            已通过本地校验的订单（SUBMITTED 状态）

        Returns
        -------
        Optional[int] : QMT 委托编号；失败返回 None（上层已记录）
        """
        trader = self._xt_trader
        account = self._qmt_account
        if trader is None or account is None:
            logger.error("[qmt_broker] QMT 未连接，无法委托")
            return None

        xtconstant, _, _, _ = self._import_xtquant()
        if order.side == OrderSide.BUY:
            qmt_order_type = xtconstant.STOCK_BUY
        else:
            qmt_order_type = xtconstant.STOCK_SELL

        if order.order_type == OrderType.LIMIT:
            price_type = xtconstant.FIX_PRICE
            price = float(order.price) if order.price and order.price > 0 else 0.0
            if price <= 0:
                logger.error(f"[qmt_broker] 限价单缺少价格: {order.order_id}")
                return None
        else:
            # 市价单：对手方最优价格申报（A股标准市价类型之一）
            price_type = xtconstant.MARKET_PEER_PRICE_FIRST
            price = 0.0

        remark = str(order.order_id)[:24]  # QMT order_remark 限 24 英文字符
        try:
            qmt_order_id = trader.order_stock(
                account,
                order.symbol,
                qmt_order_type,
                int(order.quantity),
                price_type,
                price,
                order.strategy_id[:24] or "strategy",
                remark,
            )
        except Exception as e:  # noqa: BLE001 - SDK 异常按委托失败处理
            logger.error(f"[qmt_broker] order_stock 异常: {e}")
            return None

        if qmt_order_id is None or int(qmt_order_id) <= 0:
            logger.error(
                f"[qmt_broker] order_stock 返回非法委托编号: {qmt_order_id}"
            )
            return None
        return int(qmt_order_id)

    def _qmt_cancel_order(self, order_id: str) -> int:
        """
        QMT 真实撤单（cancel_order_stock），按本地 order_id 反查 QMT 委托编号

        Parameters
        ----------
        order_id : str
            本地 order_id

        Returns
        -------
        int : QMT 撤单结果（0=成功受理，非0=失败）
        """
        trader = self._xt_trader
        account = self._qmt_account
        qmt_order_id = self._local_to_qmt.get(order_id)
        if trader is None or account is None:
            logger.error("[qmt_broker] QMT 未连接，无法撤单")
            return -1
        if qmt_order_id is None:
            logger.error(f"[qmt_broker] 无 QMT 委托编号映射: local_order_id={order_id}")
            return -1
        try:
            return int(trader.cancel_order_stock(account, qmt_order_id))
        except Exception as e:  # noqa: BLE001 - SDK 异常按撤单失败处理
            logger.error(f"[qmt_broker] cancel_order_stock 异常: {e}")
            return -1

    def _qmt_get_asset(self) -> Any:
        """
        QMT 资金查询（query_stock_asset），返回 XtAsset 原始对象

        Returns
        -------
        Any : XtAsset 对象；失败抛异常（由调用方处理）
        """
        trader = self._xt_trader
        account = self._qmt_account
        if trader is None or account is None:
            raise RuntimeError("[qmt_broker] QMT 未连接，无法查询资金")
        return trader.query_stock_asset(account)

    def _qmt_get_positions_raw(self) -> List[Any]:
        """
        QMT 持仓查询（query_stock_positions），返回 XtPosition 原始对象列表

        Returns
        -------
        List[Any] : XtPosition 列表（可能为空）
        """
        trader = self._xt_trader
        account = self._qmt_account
        if trader is None or account is None:
            raise RuntimeError("[qmt_broker] QMT 未连接，无法查询持仓")
        result = trader.query_stock_positions(account)
        return list(result or [])

    def _qmt_query_orders(self, symbol: str = "") -> Optional[List[Any]]:
        """
        QMT 当日委托查询（query_stock_orders），返回原始委托对象列表

        补单对账数据源（任务45 / T3）：断线重连后，BrokerManager 以
        本查询结果为"券商端权威状态"，与本地订单簿对账，
        仅补发券商端确认不存在的未成交订单。

        Parameters
        ----------
        symbol : str
            标的代码；空串=查询全部（当日委托）

        Returns
        -------
        Optional[List[Any]] : 当日委托原始对象列表；
            未连接/查询异常返回 None（保守：调用方不补发）
        """
        trader = self._xt_trader
        account = self._qmt_account
        if trader is None or account is None:
            logger.error("[qmt_broker] QMT 未连接，无法查询当日委托")
            return None
        try:
            result = trader.query_stock_orders(account, symbol)
            # P0-11修复：SDK 因限流/瞬时故障返回 None 时必须保守透传 None
            # （调用方见 None 即跳过补发，不判"丢失"）。原 list(result or []) 把 None→[]，
            # 被对账层误判"券商端无此单"→ 对本地已终态单重复真实报单→重复仓位。
            if result is None:
                logger.warning(
                    "[qmt_broker] query_stock_orders 返回 None（查询失败/无法确认），"
                    "保守返回 None，调用方不补发"
                )
                return None
            return list(result)
        except Exception as e:  # noqa: BLE001 - 查询异常按"无法确认"处理
            logger.error(f"[qmt_broker] 当日委托查询异常: {e}")
            self._stats["last_error"] = f"query_stock_orders: {e}"
            return None

    def _map_qmt_orders(self, raw_list: List[Any]) -> List[Dict[str, Any]]:
        """
        QMT 当日委托对象列表 → 对账快照字典列表

        本地 order_id 经 order_remark 反解（下单时写入 str(order_id)[:24]），
        与 _qmt_submit_order 的 remark 写入规则一一对应；状态经
        _map_qmt_order_status 映射为本地 OrderStatus（与回报映射同源）。

        Parameters
        ----------
        raw_list : List[Any]
            query_stock_orders 返回的原始委托对象列表

        Returns
        -------
        List[Dict[str, Any]] : 对账快照（单条映射失败跳过并记日志）
        """
        result: List[Dict[str, Any]] = []
        for raw in raw_list or []:
            try:
                remark = str(getattr(raw, "order_remark", "") or "")
                qmt_id = getattr(raw, "order_id", None)
                status_code = int(getattr(raw, "order_status", -1) or -1)
                result.append(
                    {
                        "order_id": remark[:24] if remark else "",
                        "qmt_order_id": int(qmt_id) if qmt_id is not None else None,
                        "status": self._map_qmt_order_status(status_code),
                        "symbol": str(getattr(raw, "stock_code", "") or ""),
                        "quantity": float(getattr(raw, "order_volume", 0.0) or 0.0),
                    }
                )
            except Exception as e:  # noqa: BLE001 - 单条映射失败跳过
                logger.debug(f"[qmt_broker] 委托快照映射跳过: {e}")
        return result

    # ═══════════════════════════════════════════════
    #  QMT 回报处理（_QmtCallbackHandler → _on_qmt_*）
    # ═══════════════════════════════════════════════

    @_locked
    def _on_qmt_order(self, response: Any) -> None:
        """
        QMT 委托回报：同步本地订单状态

        QMT 委托状态（xtconstant.ORDER_*）→ OrderStatus 映射:
          ORDER_WAIT_REPORTING/ORDER_REPORTED/ORDER_REPORTED_CANCEL/
          ORDER_PARTSUCC_CANCEL       → SUBMITTED（在途）
          ORDER_PART_SUCC             → PARTIAL_FILLED（部成）
          ORDER_SUCCEEDED             → FILLED（已成，以成交回报落地为准）
          ORDER_CANCELED/PART_CANCEL  → CANCELLED（已撤/部撤）
          ORDER_JUNK                  → REJECTED（废单）
        """
        if response is None:
            return
        qmt_order_id = int(getattr(response, "order_id", 0) or 0)
        local_id = self._qmt_to_local.get(qmt_order_id)
        if local_id is None:
            logger.debug(f"[qmt_broker] 未知 QMT 委托回报 order_id={qmt_order_id}")
            return
        order = self._orders.get(local_id)
        if order is None:
            return

        status_code = int(getattr(response, "status", -1) or -1)
        status_msg = str(getattr(response, "status_msg", "") or "")
        traded_volume = float(getattr(response, "traded_volume", 0.0) or 0.0)
        traded_price = float(getattr(response, "traded_price", 0.0) or 0.0)

        new_status = self._map_qmt_order_status(status_code)
        if new_status == OrderStatus.REJECTED:
            reason = f"QMT废单: {status_msg or status_code}"
            if order.status.is_active:
                order.reject(reason)
                self._record_order_rejected(order, reason)
                logger.warning(f"[qmt_broker] 订单废单: {local_id} {reason}")
        elif new_status == OrderStatus.CANCELLED:
            if order.status.is_active:
                order.cancel()
                self._record_order_cancelled(order)
                logger.info(f"[qmt_broker] 订单已撤: {local_id}")
        elif new_status == OrderStatus.FILLED:
            # 已成：成交数量以成交回报（on_stock_trade）落地为准，
            # 此处若尚未收到成交回报则按已报数量同步状态
            if order.status.is_active and traded_volume > 0:
                order.fill(traded_volume, traded_price or order.price or 0.0)
                if order.is_filled:
                    self._record_order_filled(order)
                logger.info(
                    f"[qmt_broker] 订单已成(委托回报): {local_id} "
                    f"量={traded_volume:.0f} 价={traded_price:.2f}"
                )
        elif new_status == OrderStatus.PARTIAL_FILLED:
            # 部成：状态同步（成交明细由 on_stock_trade 落地）
            if traded_volume > 0:
                order.fill(traded_volume, traded_price or order.price or 0.0)
                logger.debug(f"[qmt_broker] 订单部成: {local_id} 量={traded_volume:.0f}")

        self._notify_order_update(order)

    @_locked
    def _on_qmt_trade(self, response: Any) -> None:
        """
        QMT 成交回报：落地成交（与 SIM 模式 _execute_fill 同构）

        成交价格/数量来自券商撮合回报（本类不做任何价格假设），
        通过 _execute_fill 更新订单状态并触发成交回调。
        """
        if response is None:
            return
        qmt_order_id = int(getattr(response, "order_id", 0) or 0)
        local_id = self._qmt_to_local.get(qmt_order_id)
        if local_id is None:
            logger.warning(f"[qmt_broker] 成交回报无映射: qmt_order_id={qmt_order_id}")
            return
        order = self._orders.get(local_id)
        if order is None or not order.status.is_active:
            logger.debug(f"[qmt_broker] 成交回报跳过（订单不存在或非活跃）: {local_id}")
            return

        fill_price = float(getattr(response, "traded_price", 0.0) or 0.0)
        fill_qty = float(getattr(response, "traded_volume", 0.0) or 0.0)
        if fill_price <= 0 or fill_qty <= 0:
            logger.warning(f"[qmt_broker] 成交回报非法: price={fill_price} qty={fill_qty}")
            return

        trade = self._execute_fill(order, fill_price, time.time(), fill_qty)
        if trade is not None:
            trade_id = str(getattr(response, "trade_id", "") or "")
            if trade_id:
                trade.extra["qmt_trade_id"] = trade_id

    @_locked
    def _on_qmt_order_error(self, response: Any) -> None:
        """
        QMT 委托错误回报：订单拒绝（风控前移/废单即时反馈）
        """
        if response is None:
            return
        qmt_order_id = int(getattr(response, "order_id", 0) or 0)
        local_id = self._qmt_to_local.get(qmt_order_id)
        error_msg = str(getattr(response, "error_msg", "") or "")
        if local_id is None:
            logger.warning(
                f"[qmt_broker] 委托错误无映射: qmt_order_id={qmt_order_id} "
                f"error={error_msg}"
            )
            return
        order = self._orders.get(local_id)
        if order is None:
            return
        reason = f"QMT委托错误: {error_msg or '未知错误'}"
        if order.status.is_active:
            order.reject(reason)
            self._record_order_rejected(order, reason)
            logger.error(f"[qmt_broker] {reason}: order={local_id}")
            self._notify_order_update(order)

    @_locked
    def _on_qmt_cancel_error(self, response: Any) -> None:
        """QMT 撤单错误回报：记录错误（订单保持原状态）"""
        if response is None:
            return
        qmt_order_id = int(getattr(response, "order_id", 0) or 0)
        local_id = self._qmt_to_local.get(qmt_order_id)
        error_msg = str(getattr(response, "error_msg", "") or "")
        logger.error(
            f"[qmt_broker] 撤单错误: qmt_order_id={qmt_order_id} "
            f"local={local_id or '无映射'} error={error_msg}"
        )
        self._stats["last_error"] = f"cancel_error: {error_msg}"

    @_locked
    def _on_qmt_disconnected(self, client_id: int) -> None:
        """QMT 断线回报：连接状态置 False（等待自动重连）"""
        logger.warning(f"[qmt_broker] QMT 断线回报: client_id={client_id}")
        self._connected = False
        self._qmt_login_ok = False
        self._stats["last_error"] = f"qmt disconnected, client_id={client_id}"

    @_locked
    def _on_qmt_connected(self, client_id: int) -> None:
        """QMT 连接成功回报：置连接状态（与 connect() 结果互为补充）"""
        logger.info(f"[qmt_broker] QMT 连接回报: client_id={client_id}")
        self._connected = True

    @_locked
    def _on_qmt_account_status(self, response: Any) -> None:
        """
        QMT 账户登录状态回报：更新登录状态

        ACCOUNT_STATUS_OK=0 → 登录正常；其他状态 → 登录异常
        """
        if response is None:
            return
        status = int(getattr(response, "status", -1) or -1)
        account_id = str(getattr(response, "account_id", "") or "")
        if status == 0:
            self._qmt_login_ok = True
            logger.debug(f"[qmt_broker] 账户登录正常: account_id={self._mask_account_id(account_id)}")
        else:
            self._qmt_login_ok = False
            logger.error(
                f"[qmt_broker] 账户状态异常: account_id={account_id} status={status}"
            )
            self._stats["last_error"] = f"account_status={status}"

    @_locked
    def _on_qmt_asset(self, response: Any) -> None:
        """资产变动主推：同步本地资金缓存（QMT 账本权威，本地仅做镜像）"""
        if response is None:
            return
        try:
            cash = float(getattr(response, "cash", self.cash) or self.cash)
            frozen = float(
                getattr(response, "frozen_cash", self.frozen_cash) or self.frozen_cash
            )
            self.cash = cash
            self.frozen_cash = frozen
        except Exception as e:  # noqa: BLE001 - 缓存同步失败不影响主链路
            logger.debug(f"[qmt_broker] 资产主推同步异常: {e}")

    @_locked
    def _on_qmt_position(self, response: Any) -> None:
        """持仓变动主推：记录日志（持仓以查询为准，不做增量推算）"""
        if response is None:
            return
        code = str(getattr(response, "stock_code", "") or "")
        volume = float(getattr(response, "volume", 0.0) or 0.0)
        logger.debug(f"[qmt_broker] 持仓主推: {code} volume={volume:.0f}")

    # ═══════════════════════════════════════════════
    #  QMT 对象 → 本地模型映射
    # ═══════════════════════════════════════════════

    def _map_qmt_order_status(self, status_code: int) -> OrderStatus:
        """
        QMT 委托状态（xtconstant.ORDER_*）→ 本地 OrderStatus 映射

        Parameters
        ----------
        status_code : int
            QMT 委托状态码（48~57, 255）

        Returns
        -------
        OrderStatus : 本地订单状态
        """
        mapping: Dict[int, OrderStatus] = {
            48: OrderStatus.SUBMITTED,       # ORDER_UNREPORTED 未报
            49: OrderStatus.SUBMITTED,       # ORDER_WAIT_REPORTING 待报
            50: OrderStatus.SUBMITTED,       # ORDER_REPORTED 已报
            51: OrderStatus.SUBMITTED,       # ORDER_REPORTED_CANCEL 已报待撤
            52: OrderStatus.SUBMITTED,       # ORDER_PARTSUCC_CANCEL 部成待撤
            53: OrderStatus.CANCELLED,       # ORDER_PART_CANCEL 部撤（终态）
            54: OrderStatus.CANCELLED,       # ORDER_CANCELED 已撤
            55: OrderStatus.PARTIAL_FILLED,  # ORDER_PART_SUCC 部成
            56: OrderStatus.FILLED,          # ORDER_SUCCEEDED 已成
            57: OrderStatus.REJECTED,        # ORDER_JUNK 废单
        }
        return mapping.get(int(status_code), OrderStatus.SUBMITTED)

    def _map_qmt_position(self, raw: Any) -> Optional[Position]:
        """
        QMT XtPosition → 本地 Position 映射

        QMT 字段说明:
          stock_code    标的代码（如 600000.SH）
          volume        总持仓（含冻结）
          can_use_volume 可卖数量（可用持仓）
          open_price    成本价
          market_value  市值（可用市值/数量反推市价，避免引额外行情）

        本地 Position 模型无 available/volume 字段，
        可卖数量写入 extra["available_qty"]（不修改模型，避免影响其他模块）。

        Parameters
        ----------
        raw : Any
            XtPosition 原始对象

        Returns
        -------
        Position | None : 映射后的本地持仓；无持仓/非法数据返回 None
        """
        if raw is None:
            return None
        try:
            symbol = str(getattr(raw, "stock_code", "") or "")
            volume = float(getattr(raw, "volume", 0.0) or 0.0)
            if not symbol or volume <= 0:
                return None
            pos = Position(symbol=symbol)
            pos.quantity = volume
            pos.extra["available_qty"] = float(
                getattr(raw, "can_use_volume", 0.0) or 0.0
            )
            pos.avg_cost = float(getattr(raw, "open_price", 0.0) or 0.0)
            market_value = float(getattr(raw, "market_value", 0.0) or 0.0)
            pos.market_price = (
                market_value / volume if market_value > 0 and volume > 0 else 0.0
            )
            return pos
        except Exception as e:  # noqa: BLE001 - 单条映射失败跳过
            logger.warning(f"[qmt_broker] 持仓映射异常: {e}")
            return None

    # ═══════════════════════════════════════════════
    #  重置
    # ═══════════════════════════════════════════════

    @_locked
    def reset(self) -> None:
        """重置券商状态（SIM 模式，测试用）"""
        self.cash = self.initial_capital
        self.frozen_cash = 0.0
        self._orders.clear()
        self._trades.clear()
        self._positions.clear()
        self._t1_holdings.clear()
        self._market_prices.clear()
        self._prev_close.clear()
        self.reset_stats()
        logger.info("[qmt_broker] 状态已重置")

    def __repr__(self) -> str:
        return (
            f"QmtBroker(mode={self._mode}, account_id={self._account_id or '未绑定'}, "
            f"connected={self._connected})"
        )
