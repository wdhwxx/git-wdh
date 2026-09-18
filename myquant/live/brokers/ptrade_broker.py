# 文件名：myquant/live/brokers/ptrade_broker.py
"""
ptrade_broker.py - 恒生 Ptrade 券商适配器
==========================================
恒生 Ptrade（量化交易终端，券商提供）适配器，接口 100% 对齐 BaseBroker。

设计说明（与 QmtBroker 同构）:
  - 模拟撮合引擎【继承 QmtBroker】——两券商模拟引擎完全同构
    （行情驱动撮合/T+1/涨跌停/成本计算），该引擎已经 84 项验证。
    避免重复代码，Ptrade 文件只聚焦 Ptrade 特有部分。
  - LIVE 模式真实 Ptrade API 通道在 _call_ptrade_api 统一占位，
    接入点见下方「真实 Ptrade API 对接说明」。

继承关系:
  ExecutionInterface (myquant/execution/base.py)
    └── BaseBroker (base_broker.py)
          └── QmtBroker (qmt_broker.py)     ← 共享模拟撮合引擎
                └── PtradeBroker (本文件)

核心设计（为实时行情及实盘无缝对接）:
  1. 行情驱动撮合 — update_market_price(symbol, price) 与 PaperBroker/QmtBroker
     同接口，data_feed 收到行情推送后统一调用。
  2. 双模式切换 — mode="SIM" 本地模拟撮合；mode="LIVE" 走 _call_ptrade_api 占位。
     上层（BrokerManager / 引擎）只依赖 ExecutionInterface，切换零修改。
  3. Ptrade 策略语义接口 — order_shares / order_value / order_target_percent
     对齐 Ptrade 策略常用 API，内部统一收敛到 submit_order(Order)。
  4. LIVE 分支复用 — QmtBroker 的 connect/submit_order 等 LIVE 分支调用
     _call_qmt_sdk(...)，Ptrade 覆写该方法委托给 _call_ptrade_api，零重复。

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数

防未来函数说明:
  - 成交只依据【已发生】的行情数据（update_market_price 传入的当前价）
  - 不预测未来价格，不假设成交价；限价单以当前价触及申报价才成交
  - T+1 冻结只累计当日【已成交】买入量，可卖数量=持仓-当日买入

依赖: typing, myquant.live.brokers.qmt_broker, myquant.models.trade
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.live.brokers.qmt_broker import QmtBroker
from myquant.models.trade import Order, OrderSide, OrderType

logger = get_logger(__name__)

# ════════════════════════════════════════════════════════════════════
#  真实 Ptrade API 对接说明（后续迭代在 _call_ptrade_api 中实现）
# ════════════════════════════════════════════════════════════════════
# 恒生 Ptrade 提供策略运行环境 API（券商终端内置），常用函数:
#
#   - 下单    : order_shares(symbol, amount)               按股数委托
#               order_value(symbol, value)                 按金额委托
#               order_target_percent(symbol, percent)      按目标仓位比例
#               cancel(order_id)                           撤单
#   - 持仓    : get_position(symbol)                       查询持仓
#               get_positions()                            全部持仓
#   - 资金    : get_total_assets() / get_cash()            总资产/可用资金
#   - 行情    : get_full_tick(symbol)                      最新行情
#               subscribe_quote(symbol)                    订阅行情推送
#   - 回报    : trade/order 状态查询                        成交回报轮询或推送
#
# 挂载点（接入后上层代码零修改）:
#   - connect()      → 检查 Ptrade 运行环境（策略运行即已连接）
#   - submit_order() → order_shares / order_value（按 order.order_type）
#   - cancel_order() → cancel(order_id)
#   - get_account()  → get_total_assets() / get_cash()
#   - get_positions()-> get_positions()
#   - subscribe()    → subscribe_quote(symbol)（回调 → update_market_price）
#   - 成交回报       → 轮询/推送 trade 回报 → self._execute_fill()（本类已实现）
#
# 接入步骤:
#   1. 在 Ptrade 终端策略运行环境中部署（终端内嵌 Python 环境）
#   2. 在 _call_ptrade_api 中实现各 method 分支（见各方法 TODO 注释）
#   3. 将 mode 置为 "LIVE"，并填写 account_id
#   4. 上层（BrokerManager/引擎）无需任何改动
# ════════════════════════════════════════════════════════════════════


class PtradeBroker(QmtBroker):
    """
    恒生 Ptrade 券商适配器

    模拟撮合引擎继承 QmtBroker（同构、共享已验证逻辑），
    本类覆写 Ptrade 特有部分：
      - 平台路径/API 占位（_call_ptrade_api）
      - Ptrade 策略语义接口（order_shares / order_value / order_target_percent）

    Parameters
    ----------
    ptrade_path : str
        Ptrade 终端/环境路径（LIVE 模式语义，SIM 模式仅日志展示）
    account_id : str
        资金账号（多账户预留）
    mode : str
        "SIM" 模拟撮合（默认，无 Ptrade 环境可完整验证）
        "LIVE" 真实 Ptrade API（占位，未接入前调用核心动作明确报错）
    **kwargs : 其余参数透传 QmtBroker（initial_capital / 手续费 / T+1 等）
    """

    broker_name = "ptrade_broker"
    broker_type = "ptrade"

    # Ptrade A股支持限价单与市价单
    supported_order_types: List[OrderType] = [OrderType.LIMIT, OrderType.MARKET]

    def __init__(
        self,
        ptrade_path: str = "",
        account_id: str = "",
        mode: str = "SIM",
        **kwargs: Any,
    ) -> None:
        # 复用 QmtBroker 模拟引擎初始化（qmt_path 字段作为平台路径语义复用）
        super().__init__(qmt_path=ptrade_path, account_id=account_id, mode=mode, **kwargs)
        # Ptrade 语义字段（复用父类模拟引擎字段，此处仅作语义别名与日志）
        self._ptrade_path = self._qmt_path
        self._ptrade_api_ready = False

        logger.info(
            f"[ptrade_broker] 初始化: mode={self._mode} "
            f"account_id={self._account_id or '(未绑定)'} "
            f"ptrade_path={self._ptrade_path or '(未配置)'}"
        )

    # ═══════════════════════════════════════════════
    #  配置加载（支持 ptrade: 段）
    # ═══════════════════════════════════════════════

    def load_config(self, config: Dict[str, Any]) -> None:
        """
        从配置字典加载 Ptrade 券商配置

        支持结构:
          {
            "ptrade": {
              "ptrade_path": "...",
              "account_id": "...",
              "mode": "SIM",
              "enable_t1": true,
              ...
            }
          }
        或扁平结构直接传 Ptrade 参数。
        """
        if not config:
            return
        cfg = config.get("ptrade", config) if isinstance(config, dict) else {}
        if not isinstance(cfg, dict):
            cfg = {}

        # 透传 QmtBroker 字段解析（commission/enable_t1 等共享字段）
        super().load_config({"qmt": cfg})

        # Ptrade 特有字段
        if cfg.get("ptrade_path"):
            self._ptrade_path = str(cfg["ptrade_path"])
            self._qmt_path = self._ptrade_path

        logger.info(
            f"[ptrade_broker] 配置加载: mode={self._mode} "
            f"account_id={self._account_id or '(未绑定)'}"
        )

    # ═══════════════════════════════════════════════
    #  Ptrade 策略语义接口（SIM 模式可用，LIVE 模式占位）
    # ═══════════════════════════════════════════════

    def order_shares(
        self,
        symbol: str,
        amount: float,
        side: OrderSide = OrderSide.BUY,
        price: Optional[float] = None,
        order_type: OrderType = OrderType.LIMIT,
        strategy_id: str = "",
        tag: str = "",
    ) -> Order:
        """
        按股数委托（对齐 Ptrade order_shares 语义）

        内部收敛到 submit_order(Order)，保证下单/风控/撮合链路统一。

        Parameters
        ----------
        symbol : str
            标的代码
        amount : float
            委托股数
        side : OrderSide
            买卖方向（默认买入）
        price : float | None
            委托价（LIMIT 必填；None 时为市价单）
        order_type : OrderType
            订单类型（默认 LIMIT）
        strategy_id / tag : 订单溯源信息

        Returns
        -------
        Order : 订单（状态已更新）
        """
        if order_type == OrderType.LIMIT and price is None:
            raise ValueError(f"[ptrade_broker] LIMIT 委托必须指定 price: {symbol}")
        order = Order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=float(amount),
            price=price,
            strategy_id=strategy_id,
            tag=tag,
        )
        return self.submit_order(order)

    def order_value(
        self,
        symbol: str,
        value: float,
        side: OrderSide = OrderSide.BUY,
        price: Optional[float] = None,
        lot_size: Optional[int] = None,
        strategy_id: str = "",
        tag: str = "",
    ) -> Order:
        """
        按金额委托（对齐 Ptrade order_value 语义）

        金额 → 股数换算: 股数 = floor(value / price / lot_size) * lot_size
        （A股整手，向下取整；price 缺省用当前市场价）

        Parameters
        ----------
        symbol : str
            标的代码
        value : float
            委托金额（元）
        side : OrderSide
            买卖方向（默认买入）
        price : float | None
            委托价（默认当前市场价，无行情则报错）
        lot_size : int | None
            整手单位（默认 self.lot_size=100）
        """
        lot = lot_size or self.lot_size
        px = price or self._market_prices.get(symbol)
        if px is None or px <= 0:
            raise ValueError(
                f"[ptrade_broker] order_value 无成交价可用: {symbol}（请先 update_market_price 或传 price）"
            )
        shares = int(value / px / lot) * lot
        if shares <= 0:
            raise ValueError(
                f"[ptrade_broker] 委托金额过小，无法凑整手: value={value}, price={px}"
            )
        order = Order(
            symbol=symbol,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=float(shares),
            price=px,
            strategy_id=strategy_id,
            tag=tag,
        )
        return self.submit_order(order)

    def order_target_percent(
        self,
        symbol: str,
        percent: float,
        price: Optional[float] = None,
        lot_size: Optional[int] = None,
        strategy_id: str = "",
        tag: str = "",
    ) -> Order:
        """
        按目标仓位比例委托（对齐 Ptrade order_target_percent 语义）

        目标金额 = 总资产 * percent；金额 → 股数（整手，向下取整）。
        该接口为"目标仓位"便捷入口，实际调仓/风控仍由策略层控制。

        Parameters
        ----------
        symbol : str
            标的代码
        percent : float
            目标仓位比例（0~1，如 0.3 = 30%）
        price : float | None
            委托价（默认当前市场价）
        """
        if not 0.0 < percent <= 1.0:
            raise ValueError(f"[ptrade_broker] percent 必须在 (0,1] 区间, 当前: {percent}")
        target_value = self.total_assets * percent
        return self.order_value(
            symbol=symbol, value=target_value, side=OrderSide.BUY,
            price=price, lot_size=lot_size, strategy_id=strategy_id, tag=tag,
        )

    # ═══════════════════════════════════════════════
    #  健康检查（Ptrade 语义覆写）
    # ═══════════════════════════════════════════════

    def health_check(self) -> Dict[str, Any]:
        """健康检查（Ptrade 语义：平台API就绪状态）"""
        base = super().health_check()
        base.pop("qmt_sdk_ready", None)
        base["ptrade_api_ready"] = self._ptrade_api_ready
        base["ptrade_path"] = self._ptrade_path or None
        return base

    # ═══════════════════════════════════════════════
    #  真实 Ptrade API 占位
    # ═══════════════════════════════════════════════

    def _call_qmt_sdk(self, method: str, **kwargs: Any) -> Any:
        """QmtBroker LIVE 分支重定向 → Ptrade API 占位（零重复）"""
        return self._call_ptrade_api(method, **kwargs)

    def _call_ptrade_api(self, method: str, **kwargs: Any) -> Any:
        """
        真实 Ptrade API 调用统一占位

        未接入前调用会抛 NotImplementedError（明确报错，不静默降级）。
        接入步骤见模块头部「真实 Ptrade API 对接说明」。

        Parameters
        ----------
        method : str
            connect / disconnect / heartbeat / submit_order / cancel_order
            / subscribe / unsubscribe / get_account / get_positions
        """
        raise NotImplementedError(
            f"[ptrade_broker] 真实Ptrade API 通道未接入 (method={method}, "
            f"mode={self._mode})。接入方法见模块头部『真实Ptrade API对接说明』"
        )

    # ═══════════════════════════════════════════════
    #  重置（Ptrade 语义保留）
    # ═══════════════════════════════════════════════

    def __repr__(self) -> str:
        return (
            f"PtradeBroker(mode={self._mode}, "
            f"account_id={self._account_id or '未绑定'}, connected={self._connected})"
        )
