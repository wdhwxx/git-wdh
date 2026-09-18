# 文件名：myquant/live/brokers/manager.py
"""
manager.py - 多券商管理器（双券商并行核心）
============================================
多券商管理器（未完成任务第1项 实盘券商对接 · 双券商并行主备 Failover 架构）。

核心能力:
  - register_broker()     注册任意多个 BaseBroker 券商（QMT + Ptrade / QMT + Paper）
  - submit_order()        订单路由（默认主券商，可指定；主故障自动 failover 重试）
  - set_primary()         主券商动态切换
  - failover()            主券商健康失败 → 自动切到健康备券商
  - get_merged_positions() 多券商持仓合并视图
  - get_merged_account()   多券商资金合并视图
  - health_check()        逐券商健康巡检
  - get_state()/restore_state()  状态持久化衔接（StateStore，崩溃后恢复主备角色）
  - 回调透传              order/trade/account 回调注册透传到全部券商

架构（对齐总图 live/brokers/ 券商适配层）:
  BaseBroker（抽象基类） ← PaperBroker / QmtBroker / PtradeBroker（真实券商适配器）
  BrokerManager 在其上层做多券商编排：路由 / 主备切换 / 合并视图 / 健康监控

双券商并行典型模式（全兼容，零重构切换）:
  - 主备 Failover：QMT 主 + Ptrade 备，主故障自动切换（推荐，本任务主目标）
  - 多账户并行：不同资金/策略路由不同券商（register 多 primary 即可）
  - 仿真+实盘：PaperBroker 验证 + QMT 实盘并行

防未来函数（铁律4）:
  - 只使用已成交/已确认/已返回的数据（订单状态、持仓、资金均由券商返回）
  - 不生成任何交易信号，不假设成交价（成交价由券商撮合异步通知）
  - 主备切换只依据已发生的健康检查结果（time.time() 当前时钟），无任何预测
  - 无硬编码未来日期

为实盘无缝对接:
  - 订单带 broker_id 标记（Order.extra），取消/查询按券商路由
  - get_state()/restore_state() 与任务23 StateStore 衔接（崩溃恢复主备角色）
  - 与 paper_broker 接口 100% 对齐（真实券商适配器继承 BaseBroker 即可注册）

依赖: myquant.live.brokers.base_broker（BaseBroker/Order/Position）
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from myquant.live.brokers.base_broker import BaseBroker, Order, Position

try:
    from myquant.core.logger import get_logger
except ImportError:  # pragma: no cover
    def get_logger(name: str) -> Any:
        import logging
        return logging.getLogger(name)

logger = get_logger("live.brokers.manager")


# ═══════════════════════════════════════════════
#  异常类
# ═══════════════════════════════════════════════

class BrokerManagerError(Exception):
    """多券商管理器通用错误"""


class BrokerNotFoundError(BrokerManagerError):
    """券商未注册"""


class NoBrokerAvailableError(BrokerManagerError):
    """无可用券商（全部不可用，无法执行操作）"""


# ═══════════════════════════════════════════════
#  券商注册信息
# ═══════════════════════════════════════════════

@dataclass
class BrokerRegistration:
    """
    券商注册信息

    Attributes
    ----------
    alias : str
        券商别名（路由标识，如 qmt / ptrade / paper）
    broker : BaseBroker
        券商实例
    role : str
        角色：primary（主）/ secondary（备）
    registered_at : str
        注册时间
    """
    alias: str
    broker: BaseBroker
    role: str = "secondary"
    registered_at: str = ""

    @property
    def broker_name(self) -> str:
        return getattr(self.broker, "broker_name", self.alias)

    @property
    def broker_type(self) -> str:
        return str(getattr(self.broker, "broker_type", "unknown"))

    @property
    def connected(self) -> bool:
        return bool(getattr(self.broker, "is_connected", False))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alias": self.alias,
            "broker_name": self.broker_name,
            "broker_type": self.broker_type,
            "role": self.role,
            "connected": self.connected,
            "registered_at": self.registered_at,
        }


# ═══════════════════════════════════════════════
#  BrokerManager 多券商管理器
# ═══════════════════════════════════════════════

class BrokerManager:
    """
    多券商管理器（双券商并行核心）

    用法:
        from myquant.live.brokers import BrokerManager, PaperBroker

        mgr = BrokerManager()
        mgr.register_broker(PaperBroker(initial_capital=100_000), alias="paper", role="secondary")
        mgr.register_broker(qmt_broker, alias="qmt", role="primary")   # 真实券商适配器
        mgr.connect_all()
        order = mgr.submit_order(order_obj)          # 默认路由主券商，主故障自动 failover
        mgr.set_primary("ptrade")                    # 手动切换主券商
    """

    def __init__(self, auto_failover: bool = True):
        """
        Parameters
        ----------
        auto_failover : bool
            提交订单时主券商失败是否自动切换到备券商重试
        """
        self._lock = threading.RLock()
        self._registrations: Dict[str, BrokerRegistration] = {}
        self._primary_alias: Optional[str] = None
        self._auto_failover = auto_failover
        logger.info("[broker] 多券商管理器初始化")

    # ─────────────────────────────────────────────
    #  注册 / 查询
    # ─────────────────────────────────────────────

    def register_broker(
        self,
        broker: BaseBroker,
        alias: Optional[str] = None,
        role: str = "secondary",
    ) -> str:
        """
        注册券商

        Parameters
        ----------
        broker : BaseBroker
            券商实例（PaperBroker / QmtBroker / PtradeBroker，均继承 BaseBroker）
        alias : str | None
            券商别名（路由标识）；默认取 broker.broker_name
        role : str
            primary（主）/ secondary（备）

        Returns
        -------
        str : 券商别名
        """
        if not isinstance(broker, BaseBroker):
            raise BrokerManagerError(f"券商必须继承 BaseBroker，收到 {type(broker)}")
        if role not in ("primary", "secondary"):
            raise BrokerManagerError(f"非法角色: {role}（仅支持 primary/secondary）")
        alias = alias or str(getattr(broker, "broker_name", "broker"))
        if alias in self._registrations:
            raise BrokerManagerError(f"券商别名已存在: {alias}")
        with self._lock:
            self._registrations[alias] = BrokerRegistration(
                alias=alias,
                broker=broker,
                role=role,
                registered_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            # 首个注册的 primary 自动成为主券商
            if self._primary_alias is None and role == "primary":
                self._primary_alias = alias
            logger.info(f"[broker] 注册券商: {alias} ({broker.broker_name}, role={role})")
            return alias

    @property
    def registrations(self) -> Dict[str, BrokerRegistration]:
        return dict(self._registrations)

    def list_brokers(self) -> List[Dict[str, Any]]:
        """列出全部券商注册信息"""
        return [r.to_dict() for r in self._registrations.values()]

    def get_broker(self, alias: Optional[str] = None) -> BaseBroker:
        """获取券商实例（默认主券商）"""
        alias = self._resolve_alias(alias)
        return self._registrations[alias].broker

    def get_registration(self, alias: Optional[str] = None) -> BrokerRegistration:
        alias = self._resolve_alias(alias)
        return self._registrations[alias]

    def _resolve_alias(self, alias: Optional[str] = None) -> str:
        """解析别名（None → 主券商）"""
        if alias is not None:
            if alias not in self._registrations:
                raise BrokerNotFoundError(f"券商未注册: {alias}")
            return alias
        if self._primary_alias is None:
            raise NoBrokerAvailableError("未注册任何券商或未设置主券商")
        return self._primary_alias

    @property
    def primary_alias(self) -> Optional[str]:
        return self._primary_alias

    @property
    def primary(self) -> Optional[BaseBroker]:
        if self._primary_alias is None:
            return None
        return self._registrations[self._primary_alias].broker

    def set_primary(self, alias: str) -> str:
        """
        主券商动态切换（实盘热切换：后续默认路由到新主券商）

        Parameters
        ----------
        alias : str
            券商别名

        Returns
        -------
        str : 新主券商别名
        """
        if alias not in self._registrations:
            raise BrokerNotFoundError(f"券商未注册: {alias}")
        with self._lock:
            old = self._primary_alias
            self._registrations[alias].role = "primary"
            if old and old != alias:
                self._registrations[old].role = "secondary"
            self._primary_alias = alias
            logger.info(f"[broker] 主券商切换: {old} → {alias}")
            return alias

    def remove_broker(self, alias: str) -> bool:
        """移除券商（主券商被移除时自动指定剩余 primary 为新主）"""
        if alias not in self._registrations:
            raise BrokerNotFoundError(f"券商未注册: {alias}")
        with self._lock:
            del self._registrations[alias]
            if self._primary_alias == alias:
                self._primary_alias = None
                for a, r in self._registrations.items():
                    if r.role == "primary":
                        self._primary_alias = a
                        break
            logger.info(f"[broker] 移除券商: {alias}")
            return True

    # ─────────────────────────────────────────────
    #  连接管理
    # ─────────────────────────────────────────────

    def connect_all(self) -> Dict[str, bool]:
        """连接全部券商（返回 alias → 是否连接成功）"""
        results: Dict[str, bool] = {}
        for alias, reg in self._registrations.items():
            try:
                results[alias] = bool(reg.broker.connect())
            except Exception as e:  # pragma: no cover
                logger.warning(f"[broker] {alias} 连接失败: {e}")
                results[alias] = False
        return results

    def disconnect_all(self) -> None:
        """断开全部券商"""
        for alias, reg in self._registrations.items():
            try:
                reg.broker.disconnect()
            except Exception as e:  # pragma: no cover
                logger.warning(f"[broker] {alias} 断开失败: {e}")

    def is_connected(self, alias: Optional[str] = None) -> bool:
        """指定券商是否已连接（默认主券商）"""
        return bool(self.get_broker(alias).is_connected)

    # ─────────────────────────────────────────────
    #  健康检查 / 主备切换
    # ─────────────────────────────────────────────

    def health_check(self, alias: Optional[str] = None) -> Dict[str, Any]:
        """指定券商健康检查（默认主券商）"""
        return self.get_broker(alias).health_check()

    def health_check_all(self) -> Dict[str, Dict[str, Any]]:
        """全部券商健康巡检（返回 alias → health_check 结果）"""
        return {alias: reg.broker.health_check() for alias, reg in self._registrations.items()}

    def push_market_data(self, symbol: str, bar: Dict[str, Any]) -> Dict[str, int]:
        """
        行情广播（引擎 on_market_data 统一调用，驱动各券商撮合）

        语义（重要，避免"重复成交"误解）:
          - 广播到全部注册券商；**每个券商只撮合【自己账上】的订单**
          - 主备模式下备券商没有该标的订单 → 不撮合、无成交回报，天然无重复成交
          - 分账户并行下两个券商各自撮合各自挂单，合并视图正确
          - 若需严格"同账户主备"场景，可扩展 broadcast_to=all|primary 选项（v1 广播全部）

        撮合驱动 duck-typing:
          - 券商有 on_bar(bar) → 调用（PaperBroker T+1 撮合，传入完整 bar dict）
          - 券商有 update_market_price(symbol, price) → 调用 close 价（QmtBroker 行情驱动）
          - 两者都没有 → 跳过该券商

        【防未来函数】只传已发生 bar；撮合由券商按已发生行情驱动，不做价格预测。

        Parameters
        ----------
        symbol : str
            标的代码
        bar : Dict[str, Any]
            K线数据（已发生），含 symbol/open/high/low/close/volume/timestamp

        Returns
        -------
        Dict[str, int] : alias → 本 bar 该券商产生的成交笔数
        """
        results: Dict[str, int] = {}
        with self._lock:
            for alias, reg in self._registrations.items():
                broker = reg.broker
                matched = 0
                try:
                    if hasattr(broker, "on_bar"):
                        trades = broker.on_bar(bar)
                        matched = len(trades) if trades else 0
                    elif hasattr(broker, "update_market_price"):
                        if isinstance(bar, dict):
                            close = float(bar.get("close", 0) or 0)
                        else:
                            close = float(getattr(bar, "close", 0) or 0)
                        if close > 0:
                            trades = broker.update_market_price(symbol, close)
                            matched = len(trades) if trades else 0
                    else:
                        continue
                except Exception as e:  # pragma: no cover
                    logger.warning(f"[broker] {alias} 行情处理异常: {e}")
                results[alias] = matched
        return results

    def failover(self) -> Optional[str]:
        """
        主备切换：主券商健康失败 → 自动切到第一个健康的备券商

        Returns
        -------
        str | None : 新主券商别名（无需切换或切换失败返回 None）
        """
        with self._lock:
            if self._primary_alias is None:
                return None
            primary = self._registrations[self._primary_alias].broker
            try:
                primary_hc = primary.health_check()
                primary_ok = primary_hc.get("status") in ("healthy", "degraded") and primary_hc.get("connected", False)
            except Exception:  # pragma: no cover
                primary_ok = False
            if primary_ok:
                return None
            # 主券商不可用 → 找健康备券商
            for alias, reg in self._registrations.items():
                if alias == self._primary_alias:
                    continue
                try:
                    hc = reg.broker.health_check()
                    ok = hc.get("status") in ("healthy", "degraded") and hc.get("connected", False)
                except Exception:  # pragma: no cover
                    ok = False
                if ok:
                    logger.warning(f"[broker] 主券商故障，自动切换: {self._primary_alias} → {alias}")
                    return self.set_primary(alias)
            logger.error("[broker] 主券商故障，且无可用备券商")
            return None

    # ─────────────────────────────────────────────
    #  订单路由（防未来：只基于已确认状态）
    # ─────────────────────────────────────────────

    def submit_order(
        self,
        order: Order,
        broker_id: Optional[str] = None,
        auto_failover: Optional[bool] = None,
    ) -> Order:
        """
        提交订单（默认路由主券商；主故障自动 failover 重试）

        【防未来函数】订单提交只使用当前已知信息；成交价由券商撮合
        异步通知，本方法不假设成交价。

        Parameters
        ----------
        order : Order
            订单对象（extra 会被写入 broker_id 路由标记）
        broker_id : str | None
            指定券商别名；None → 主券商
        auto_failover : bool | None
            覆盖全局 auto_failover 设置

        Returns
        -------
        Order : 订单（状态已更新，通常为 SUBMITTED / REJECTED / FILLED）
        """
        do_failover = self._auto_failover if auto_failover is None else auto_failover
        alias = self._resolve_alias(broker_id)
        order.extra = dict(order.extra or {})
        order.extra["broker_id"] = alias
        try:
            result = self._registrations[alias].broker.submit_order(order)
        except Exception as e:
            if do_failover:
                return self._failover_submit(order, str(e))
            raise BrokerManagerError(f"订单提交失败 ({alias}): {e}") from e
        # 主券商拒单 / 主券商不可用 → 自动 failover 重试
        if do_failover and self._is_order_rejected(result):
            return self._failover_submit(order, f"{alias} 拒单")
        return result

    def _failover_submit(self, order: Order, reason: str) -> Order:
        """
        failover 重试：逐备券商重试，成功后切为主

        注意：拒单场景主券商连接可能健康（如风控拒单/资金不足），
        因此不能依赖连接健康检查（failover()），而是直接逐个尝试备券商。
        """
        current = self._primary_alias
        with self._lock:
            tried = {current}
            for alias, reg in self._registrations.items():
                if alias in tried:
                    continue
                tried.add(alias)
                order.extra = dict(order.extra or {})
                order.extra["broker_id"] = alias
                try:
                    result = reg.broker.submit_order(order)
                except Exception as e:  # pragma: no cover
                    logger.warning(f"[broker] {alias} failover 重试失败: {e}")
                    continue
                if not self._is_order_rejected(result):
                    logger.warning(f"[broker] failover 重试成功: {current} → {alias}（{reason}）")
                    self.set_primary(alias)
                    return result
            # 全部备券商失败 → 回滚 broker_id 标记，返回原订单
            order.extra["broker_id"] = current
            logger.error(f"[broker] failover 全部失败: {reason}")
            return order

    @staticmethod
    def _is_order_rejected(order: Order) -> bool:
        """判定订单是否被拒（支持 OrderStatus 枚举 / 字符串）"""
        status = getattr(order, "status", "")
        if hasattr(status, "name"):
            status = status.name
        return "REJECT" in str(status).upper()

    def cancel_order(self, order_id: str, broker_id: Optional[str] = None) -> bool:
        """撤单（默认在全部券商中查找订单；指定 broker_id 则定向）"""
        if broker_id is not None:
            return bool(self.get_broker(broker_id).cancel_order(order_id))
        # 在全部券商中查找并撤单
        for alias, reg in self._registrations.items():
            try:
                order = reg.broker.get_order(order_id)
                if order is not None and str(getattr(order, "extra", {}).get("broker_id", "")) == alias:
                    return bool(reg.broker.cancel_order(order_id))
            except Exception:  # pragma: no cover
                continue
        # 未找到 → 全部尝试一次（兼容无 extra 标记的订单）
        for alias, reg in self._registrations.items():
            try:
                if reg.broker.cancel_order(order_id):
                    return True
            except Exception:  # pragma: no cover
                continue
        return False

    # ─────────────────────────────────────────────
    #  查询（合并视图）
    # ─────────────────────────────────────────────

    def get_order(self, order_id: str, broker_id: Optional[str] = None) -> Optional[Order]:
        """查询订单（指定券商或全部券商查找）"""
        if broker_id is not None:
            return self.get_broker(broker_id).get_order(order_id)
        for reg in self._registrations.values():
            try:
                order = reg.broker.get_order(order_id)
                if order is not None:
                    return order
            except Exception:  # pragma: no cover
                continue
        return None

    def get_active_orders(self, broker_id: Optional[str] = None) -> List[Order]:
        """获取活动订单（指定券商或合并全部券商）"""
        if broker_id is not None:
            return list(self.get_broker(broker_id).get_active_orders())
        result: List[Order] = []
        for reg in self._registrations.values():
            try:
                result.extend(reg.broker.get_active_orders())
            except Exception:  # pragma: no cover
                continue
        return result

    def get_trades(self, broker_id: Optional[str] = None) -> List[Any]:
        """获取成交（指定券商或合并全部券商）"""
        if broker_id is not None:
            return list(self.get_broker(broker_id).get_trades())
        result: List[Any] = []
        for reg in self._registrations.values():
            try:
                result.extend(reg.broker.get_trades())
            except Exception:  # pragma: no cover
                continue
        return result

    def get_positions(self, broker_id: Optional[str] = None, merged: bool = True) -> List[Position]:
        """
        获取持仓

        merged=True：合并全部券商持仓（按 symbol 聚合 quantity、加权 avg_cost）
        merged=False：返回全部券商原始持仓（Position.extra 含 broker_id）
        """
        if broker_id is not None:
            return list(self.get_broker(broker_id).get_positions())
        all_positions: List[Position] = []
        for alias, reg in self._registrations.items():
            try:
                for pos in reg.broker.get_positions():
                    pos.extra = dict(pos.extra or {})
                    pos.extra["broker_id"] = alias
                    all_positions.append(pos)
            except Exception:  # pragma: no cover
                continue
        if not merged:
            return all_positions
        return self._merge_positions(all_positions)

    @staticmethod
    def _merge_positions(positions: List[Position]) -> List[Position]:
        """
        合并多券商持仓（按 symbol 聚合）

        quantity 累加；avg_cost 按数量加权平均；market_price 取第一个非空；
        realized_pnl 累加；strategy_id 保留第一个；extra 含 broker_ids 列表。
        """
        merged: Dict[str, Dict[str, Any]] = {}
        order = []
        for pos in positions:
            sym = pos.symbol
            if sym not in merged:
                merged[sym] = {
                    "quantity": 0.0, "cost_qty": 0.0, "cost_sum": 0.0,
                    "market_price": None, "realized_pnl": 0.0,
                    "strategy_id": pos.strategy_id, "broker_ids": [],
                    "opened_at": pos.opened_at,
                }
                order.append(sym)
            m = merged[sym]
            qty = float(pos.quantity or 0)
            cost = float(pos.avg_cost or 0)
            m["quantity"] += qty
            m["cost_qty"] += qty
            m["cost_sum"] += qty * cost
            m["realized_pnl"] += float(pos.realized_pnl or 0)
            if m["market_price"] is None and pos.market_price is not None:
                m["market_price"] = pos.market_price
            bid = (pos.extra or {}).get("broker_id")
            if bid and bid not in m["broker_ids"]:
                m["broker_ids"].append(bid)
        result: List[Position] = []
        for sym in order:
            m = merged[sym]
            avg_cost = (m["cost_sum"] / m["cost_qty"]) if m["cost_qty"] > 0 else 0.0
            result.append(Position(
                symbol=sym,
                quantity=m["quantity"],
                avg_cost=avg_cost,
                market_price=m["market_price"],
                realized_pnl=m["realized_pnl"],
                opened_at=m["opened_at"],
                strategy_id=m["strategy_id"],
                extra={"broker_ids": m["broker_ids"]},
            ))
        return result

    def get_account(self, broker_id: Optional[str] = None, merged: bool = True) -> Dict[str, Any]:
        """
        获取账户资金

        merged=True：合并全部券商（cash/available/frozen/market_value/total_assets 累加）
        merged=False：返回 {alias: account_dict} 明细
        """
        if broker_id is not None:
            return dict(self.get_broker(broker_id).get_account())
        accounts: Dict[str, Dict[str, Any]] = {}
        for alias, reg in self._registrations.items():
            try:
                accounts[alias] = dict(reg.broker.get_account())
            except Exception:  # pragma: no cover
                continue
        if not merged:
            return accounts
        merged_account: Dict[str, Any] = {
            "cash": 0.0, "available_cash": 0.0, "frozen_cash": 0.0,
            "market_value": 0.0, "total_assets": 0.0, "broker_ids": list(accounts),
        }
        for acc in accounts.values():
            for key in ("cash", "available_cash", "frozen_cash", "market_value", "total_assets"):
                merged_account[key] += float(acc.get(key, 0.0))
        return merged_account

    # ─────────────────────────────────────────────
    #  回调透传
    # ─────────────────────────────────────────────

    def register_order_callback(self, callback: Callable[[Order], None]) -> None:
        """订单回调透传到全部券商"""
        for reg in self._registrations.values():
            reg.broker.register_order_callback(callback)

    def register_trade_callback(self, callback: Callable[[Any], None]) -> None:
        """成交回调透传到全部券商"""
        for reg in self._registrations.values():
            reg.broker.register_trade_callback(callback)

    def register_account_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """账户回调透传到全部券商"""
        for reg in self._registrations.values():
            reg.broker.register_account_callback(callback)

    # ─────────────────────────────────────────────
    #  状态持久化衔接（StateStore）
    # ─────────────────────────────────────────────

    def get_state(self) -> Dict[str, Any]:
        """
        采集管理器状态（供 StateStore 持久化，崩溃恢复）

        只记录已发生的注册/角色/连接状态，不记录任何预测数据。
        """
        return {
            "primary_alias": self._primary_alias,
            "auto_failover": self._auto_failover,
            "brokers": {alias: reg.to_dict() for alias, reg in self._registrations.items()},
        }

    def restore_state(self, state: Dict[str, Any]) -> None:
        """
        恢复管理器状态（崩溃恢复后重建主备角色）

        仅恢复角色配置；连接状态由调用方 connect_all() 重建
        （不恢复连接本身——连接状态不应从快照"恢复"，必须重新连接验证）。

        Parameters
        ----------
        state : dict
            get_state() 产物
        """
        if not state:
            return
        primary = state.get("primary_alias")
        if primary and primary in self._registrations:
            self.set_primary(primary)

    def get_summary(self) -> Dict[str, Any]:
        """管理器摘要（券商数 + 主券商 + 各券商健康/角色/连接）"""
        return {
            "broker_count": len(self._registrations),
            "primary_alias": self._primary_alias,
            "auto_failover": self._auto_failover,
            "brokers": self.list_brokers(),
            "health": self.health_check_all(),
        }


__all__ = [
    "BrokerManager",
    "BrokerRegistration",
    "BrokerManagerError",
    "BrokerNotFoundError",
    "NoBrokerAvailableError",
]
