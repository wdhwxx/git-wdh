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

import copy
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from myquant.live.brokers.base_broker import BaseBroker, Order, Position
from myquant.models.trade import OrderStatus

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

    def __init__(self, auto_failover: bool = True, failover_cooldown: float = 60.0):
        """
        Parameters
        ----------
        auto_failover : bool
            提交订单时主券商失败是否自动切换到备券商重试
        failover_cooldown : float
            failover 冷却时间（秒）：全部备券商失败后，冷却期内不再触发
            failover（防网络波动下信号重发触发 failover 风暴）；failover
            成功切换到备券商后，同样冷却 failover_cooldown 秒才允许恢复原主。
        """
        self._lock = threading.RLock()
        self._registrations: Dict[str, BrokerRegistration] = {}
        self._primary_alias: Optional[str] = None
        self._auto_failover = auto_failover
        self._failover_cooldown = float(failover_cooldown)

        # ── 任务44：failover 盲目重试约束化（失败分类 / 副本重试 / 冷却熔断 / 健康恢复）──
        self._last_failover_fail_at: float = 0.0      # 最近一次 failover 全部失败时刻（冷却计时）
        self._last_failover_success_at: float = 0.0   # 最近一次 failover 成功时刻（恢复冷却计时）
        self._pre_failover_primary: Optional[str] = None  # failover 前的原主（健康恢复切回用）
        self._failover_stats: Dict[str, Any] = {
            "attempts": 0,          # failover 触发总次数（含成功/失败）
            "success": 0,           # failover 成功次数（订单经备券商提交成功）
            "business_skipped": 0,  # 业务拒单（资金/持仓/风控等）不 failover 次数
            "cooldown_skipped": 0,  # 冷却期内触发被拦截次数
            "restore_count": 0,     # 主券商健康恢复切回次数
            "last_reason": "",      # 最近一次 failover 原因
            "last_at": 0.0,         # 最近一次 failover 时刻
        }
        # ── 任务45：断线补单对账（T3）── 最近一次 resend_pending_orders 结果（可观测）
        self._last_resend: Dict[str, Any] = {}
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

    def set_primary(self, alias: str, manual: bool = True) -> str:
        """
        主券商动态切换（实盘热切换：后续默认路由到新主券商）

        Parameters
        ----------
        alias : str
            券商别名
        manual : bool
            True=手动切换（清除 failover 恢复记忆，不自动切回）；
            False=failover 自动切换（保留原主别名，健康恢复后可切回）

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
            if manual:
                # 手动切换：无自动恢复语义（用户/上层决策优先）
                self._pre_failover_primary = None
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
                        # KP-P3-09：撮合价用当日 open（已发生事实，避免用当日 close 属防未来）；
                        # 昨收由行情快照前收字段注入，供涨跌停 fail-closed 判定。
                        if isinstance(bar, dict):
                            open_ = float(bar.get("open", 0) or 0)
                            prev_close = float(bar.get("prev_close", 0) or 0)
                        else:
                            open_ = float(getattr(bar, "open", 0) or 0)
                            prev_close = float(getattr(bar, "prev_close", 0) or 0)
                        if hasattr(broker, "set_prev_close") and prev_close > 0:
                            broker.set_prev_close(symbol, prev_close)
                        if open_ > 0:
                            trades = broker.update_market_price(symbol, open_)
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
                    self._pre_failover_primary = self._primary_alias
                    self._last_failover_success_at = time.time()
                    return self.set_primary(alias, manual=False)
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

        failover 决策（任务44：约束化，杜绝盲目重试）:
          1. 业务拒单（资金不足/持仓不足/T+1/风控/涨跌停/校验失败等，
             拒绝原因含业务关键词或无法分类）→ 不 failover，
             原样返回主券商拒单结果（换券商同样会拒，且防绕过主券商风控）。
          2. 通道类故障（未连接/委托异常/断线/超时/SDK 等）→ 用干净副本
             （同一 order_id、PENDING 状态）逐备券商重试，成功则切主。
          3. 冷却熔断：最近一次 failover 全部失败后 failover_cooldown 秒内
             不再触发 failover（防网络波动下信号重发导致 failover 风暴）。
        """
        do_failover = self._auto_failover if auto_failover is None else auto_failover
        alias = self._resolve_alias(broker_id)
        order.extra = dict(order.extra or {})
        order.extra["broker_id"] = alias
        try:
            result = self._registrations[alias].broker.submit_order(order)
        except Exception as e:
            if not do_failover:
                raise BrokerManagerError(f"订单提交失败 ({alias}): {e}") from e
            if self._in_failover_cooldown():
                self._failover_stats["cooldown_skipped"] += 1
                logger.warning(f"[broker] failover 冷却中，跳过重试: {e}")
                raise BrokerManagerError(f"订单提交失败 ({alias}): {e}") from e
            return self._failover_submit(order, str(e))
        # 主券商拒单 → 分类判定后才决定是否 failover
        if do_failover and self._is_order_rejected(result):
            kind = self._classify_submit_failure(result)
            if kind != "channel":
                # 业务拒单/无法分类：不盲目 failover（安全第一，防绕过风控）
                self._failover_stats["business_skipped"] += 1
                logger.warning(
                    f"[broker] {alias} 拒单为业务/未知原因({kind})，不 failover: "
                    f"{result.extra.get('reject_reason', '')}"
                )
                return result
            if self._in_failover_cooldown():
                self._failover_stats["cooldown_skipped"] += 1
                logger.warning(
                    f"[broker] failover 冷却中，跳过重试: "
                    f"{result.extra.get('reject_reason', '')}"
                )
                return result
            return self._failover_submit(order, f"{alias} 拒单: {result.extra.get('reject_reason', '')}")
        return result

    # ── 任务44：failover 约束化辅助 ─────────────────────────────

    # 业务拒单关键词：订单/账户级问题，换券商同样拒绝（绝不盲目重试）
    _BUSINESS_FAILURE_KEYWORDS = (
        "资金不足", "可卖数量", "持仓不足", "无持仓", "T+1", "冻结",
        "风控", "涨跌停", "涨停", "跌停", "校验失败", "订单状态无法提交",
        "QMT废单", "委托数量", "价格", "quantity", "symbol 不能为空",
    )
    # 通道故障关键词：连接/通道级问题，可 failover 到备券商
    _CHANNEL_FAILURE_KEYWORDS = (
        "未连接", "未配置", "委托异常", "委托返回空", "断线", "超时",
        "SDK", "连接失败", "connect", "心跳", "网络", "无法连接", "无法委托",
    )

    @classmethod
    def _classify_submit_failure(cls, order: Order) -> str:
        """
        分类订单提交失败原因

        T0-12：返回值三分类——
          - "channel"  通道故障（可 failover 到备券商）：连接/断线/超时/未连接等
          - "business" 业务拒单（绝不 failover）：资金不足/持仓不足/风控/涨跌停等
          - "unknown"  结果未知（绝不 failover）：无 reason / 关键词都不命中；
                        与 business 的区别是"不确定"，必须保守不重试，
                        且不允许把"委托异常/返回空/SDK"这类**券商端可能已受理**
                        的文案误判为 channel 而盲目 failover（双仓风险）。

        Returns
        -------
        str : "channel" / "business" / "unknown"
        """
        reason = str(getattr(order, "extra", {}).get("reject_reason", "") or "")
        if not reason:
            return "unknown"  # 无原因说明 → 未知，保守不重试
        text = reason.lower()
        if any(k.lower() in text for k in cls._BUSINESS_FAILURE_KEYWORDS):
            return "business"
        if any(k.lower() in text for k in cls._CHANNEL_FAILURE_KEYWORDS):
            # T0-12：通道关键词命中前，先显式排除"券商端可能已受理"的未知结果。
            # 这类文案（委托异常/委托返回空/order_stock/submit_order/SDK调用）
            # 即使含通道关键词，也不允许 failover——因为 SDK 抛异常时券商端可能
            # 已落单，failover = 双仓真金白银损失。
            # 走 unknown 分支，由调用方/查单闭环处理，不在这里盲目重发。
            if any(
                flag in text
                for flag in (
                    "委托异常", "委托返回空",
                    "order_stock", "submit_order",
                    "sdk 调用", "sdk调用",
                )
            ):
                return "unknown"
            return "channel"
        return "unknown"  # 无法分类 → 未知，保守不重试（绝不盲目 failover）

    @staticmethod
    def _clone_order_for_retry(order: Order) -> Order:
        """
        生成 failover 重试用的干净订单副本

        原订单已被主券商置为终态（REJECTED），状态机不允许再提交；
        副本重置为 PENDING、成交归零，保持 order_id 不变（确定性幂等键，
        券商端按订单 ID 去重仍生效），extra 深拷贝避免与原单共享引用。
        """
        return replace(
            order,
            status=OrderStatus.PENDING,
            filled_qty=0.0,
            filled_price=0.0,
            extra=copy.deepcopy(order.extra or {}),  # P1-18：真深拷贝，嵌套对象不与原单共享
        )

    def _in_failover_cooldown(self) -> bool:
        """是否处于 failover 冷却期（全部失败后熔断，防重试风暴）"""
        if self._last_failover_fail_at <= 0:
            return False
        return (time.time() - self._last_failover_fail_at) < self._failover_cooldown

    def restore_primary_if_healthy(self) -> Optional[str]:
        """
        主券商健康恢复切回（failover 成功切换到备券商后调用）

        条件:
          1. 存在 failover 前的原主（_pre_failover_primary）
          2. 距 failover 成功时刻已超过 failover_cooldown（防抖动反复切换）
          3. 原主 health_check 健康（status in healthy/degraded 且 connected）

        Returns
        -------
        str | None : 切回的原主别名；不满足条件返回 None
        """
        original = self._pre_failover_primary
        if original is None or original == self._primary_alias:
            return None
        if original not in self._registrations:
            self._pre_failover_primary = None
            return None
        if (time.time() - self._last_failover_success_at) < self._failover_cooldown:
            return None
        try:
            hc = self._registrations[original].broker.health_check()
            ok = hc.get("status") in ("healthy", "degraded") and hc.get("connected", False)
        except Exception:  # pragma: no cover
            ok = False
        if not ok:
            return None
        logger.info(f"[broker] 主券商健康恢复，切回: {self._primary_alias} → {original}")
        self.set_primary(original, manual=False)
        self._failover_stats["restore_count"] += 1
        self._pre_failover_primary = None
        return original

    def _failover_submit(self, order: Order, reason: str) -> Order:
        """
        failover 重试（任务44 约束化）：仅通道故障进入本方法

        流程:
          1. 记录 failover 统计（attempts/last_reason/last_at）
          2. 用干净副本（同 order_id、PENDING）逐备券商重试，
             原订单保持主券商拒单结果留痕（不污染副本、不伪造状态）
          3. 全部失败 → 记录冷却时刻（后续 submit_order 冷却期内熔断）
          4. 成功 → 切换主券商（保留原主供健康恢复切回）
        """
        current = self._primary_alias
        self._failover_stats["attempts"] += 1
        self._failover_stats["last_reason"] = reason
        self._failover_stats["last_at"] = time.time()
        retry_order = self._clone_order_for_retry(order)
        with self._lock:
            tried = {current}
            for alias, reg in self._registrations.items():
                if alias in tried:
                    continue
                tried.add(alias)
                retry_order.extra = dict(retry_order.extra or {})
                retry_order.extra["broker_id"] = alias
                retry_order.extra["failover_from"] = current
                try:
                    result = reg.broker.submit_order(retry_order)
                except Exception as e:  # pragma: no cover
                    logger.warning(f"[broker] {alias} failover 重试失败: {e}")
                    continue
                if not self._is_order_rejected(result):
                    logger.warning(
                        f"[broker] failover 重试成功: {current} → {alias}（{reason}）"
                    )
                    self._failover_stats["success"] += 1
                    self._last_failover_success_at = time.time()
                    self._pre_failover_primary = current
                    self.set_primary(alias, manual=False)
                    return result
            # 全部备券商失败 → 熔断（冷却期内不再 failover），返回原订单
            self._last_failover_fail_at = time.time()
            order.extra["broker_id"] = current
            logger.error(f"[broker] failover 全部失败: {reason}（已进入冷却期）")
            return order

    @staticmethod
    def _is_order_rejected(order: Order) -> bool:
        """判定订单是否被拒（支持 OrderStatus 枚举 / 字符串）"""
        status = getattr(order, "status", "")
        if hasattr(status, "name"):
            status = status.name
        return "REJECT" in str(status).upper()

    # ─────────────────────────────────────────────
    #  断线补单对账（任务45 / T3）
    # ─────────────────────────────────────────────

    def resend_pending_orders(
        self, symbol: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        断线补单对账（T3：重连后仅补发未成交订单，已成交订单不重发）

        适用场景: 本地已提交（SUBMITTED）但 QMT 端断线/网络波动，无法确认
        订单是否真正到达券商委托队列；重连后调用本方法对账。

        判定规则（逐券商独立）:
          1. 券商未连接 → 跳过（记 skipped；重连成功后再调用本方法）
          2. 本地 SUBMITTED 订单与券商端当日委托快照（query_broker_orders）对账：
             - 券商端【存在】该 order_id（任何状态）→ 不补发（证明 QMT 已收到）：
               · 在途（SUBMITTED/PARTIAL_FILLED）→ in_flight 计数
               · 终态（FILLED/CANCELLED/REJECTED）→ terminal 计数（已成交/已撤/废单
                 绝不重发；本地状态待券商回报同步，本方法不伪造状态）
             - 券商端【不存在】该 order_id → 判定丢失 → 用干净副本
               （同一 order_id、PENDING，extra 深拷贝）幂等补发
               （submit_order 重复 order_id 直接返回已有订单，防重复报单）
          3. 券商端查询失败（query_broker_orders 返回 None）→ 无法确认 →
             跳过不补发（保守：宁可漏单也不重复报单，防补单风暴）
          4. 防未来：只依据已发生的本地状态与券商端查询结果，无任何预测

        Parameters
        ----------
        symbol : str | None
            可选，仅对账指定标的

        Returns
        -------
        Dict[str, Any] :
            {
              "checked": 本地 SUBMITTED 且完成对账的订单数,
              "in_flight": 券商端在途（不补发）,
              "terminal":  券商端终态（不补发）,
              "resent":    补发成功数,
              "failed":    补发失败数,
              "skipped":   跳过数（未连接/查询失败/异常）,
              "per_broker": {alias: 同上各计数},
            }
        """
        result: Dict[str, Any] = {
            "checked": 0, "in_flight": 0, "terminal": 0,
            "resent": 0, "failed": 0, "skipped": 0,
            "per_broker": {},
        }
        with self._lock:
            for alias, reg in self._registrations.items():
                broker = reg.broker
                per: Dict[str, Any] = {
                    "checked": 0, "in_flight": 0, "terminal": 0,
                    "resent": 0, "failed": 0, "skipped": 0,
                }
                try:
                    # 1. 未连接 → 跳过（重连后由调用方再次触发）
                    if not broker.is_connected:
                        per["skipped"] += 1
                        result["skipped"] += 1
                        logger.info(f"[broker] 补单对账跳过({alias}): 券商未连接")
                        result["per_broker"][alias] = per
                        continue

                    # 2. 本地 SUBMITTED 订单（补发候选）
                    # T0-12 显式排除 ACK_UNKNOWN：该状态是"报单结果未知、已发起N轮查单"，
                    # 由 qmt_broker._handle_unknown_submit 自己闭环（查单命中→SUBMITTED，
                    # 窗口走完未命中→REJECTED）；绝不能进入补发集合——否则等于在查单
                    # 还没结论时就盲目重发，正是 RC-16 双仓要防的路径。
                    local_submitted = [
                        o for o in broker.get_orders(symbol)
                        if o.status is OrderStatus.SUBMITTED
                    ]
                    if not local_submitted:
                        result["per_broker"][alias] = per
                        continue

                    # 3. 券商端当日委托快照（查询失败 → 不补发，防重复单）
                    broker_orders = broker.query_broker_orders(symbol)
                    if broker_orders is None:
                        per["skipped"] += len(local_submitted)
                        result["skipped"] += len(local_submitted)
                        logger.warning(
                            f"[broker] 补单对账跳过({alias}): 券商端查询失败，"
                            f"无法确认 {len(local_submitted)} 笔在途单，不补发"
                        )
                        result["per_broker"][alias] = per
                        continue

                    # 4. 按 order_id 前 24 位建索引（与 QMT order_remark 写入规则一致）
                    by_key = {
                        str(o.get("order_id") or "")[:24]: o
                        for o in broker_orders
                    }
                    for order in local_submitted:
                        key = str(order.order_id)[:24]
                        result["checked"] += 1
                        per["checked"] += 1
                        remote = by_key.get(key)
                        if remote is None:
                            # 券商端无此单 → 丢失 → 幂等补发（同 order_id、PENDING 副本）
                            retry = self._clone_order_for_retry(order)
                            retry.extra = dict(retry.extra or {})
                            retry.extra["broker_id"] = alias
                            retry.extra["resend"] = True
                            try:
                                # 走 resubmit_order 强制重发通道：跳过 submit_order
                                # 的订单幂等查重（否则补发副本会被本地订单簿拦截而空转）；
                                # 基类默认实现=submit_order，无幂等拦截的券商天然兼容
                                res = broker.resubmit_order(retry)
                                if res is not None and not self._is_order_rejected(res):
                                    result["resent"] += 1
                                    per["resent"] += 1
                                    logger.warning(
                                        f"[broker] 断线补单成功: {alias} "
                                        f"{order.order_id} {order.symbol} "
                                        f"{order.quantity:.0f}@{order.price if order.price else '市价'}"
                                    )
                                else:
                                    result["failed"] += 1
                                    per["failed"] += 1
                                    logger.error(
                                        f"[broker] 断线补单被拒: {alias} {order.order_id}"
                                    )
                            except Exception as e:  # noqa: BLE001 - 补发异常记失败
                                result["failed"] += 1
                                per["failed"] += 1
                                logger.warning(f"[broker] {alias} 补单异常: {e}")
                        elif remote["status"] in (
                            OrderStatus.FILLED,
                            OrderStatus.CANCELLED,
                            OrderStatus.REJECTED,
                            OrderStatus.PARTIAL_FILLED,
                        ):
                            # 券商端已有成交/撤单/废单痕迹 → 绝不重发（本地状态待回报同步）
                            result["terminal"] += 1
                            per["terminal"] += 1
                            logger.info(
                                f"[broker] 对账不补发({alias}): {order.order_id} "
                                f"券商端={remote['status'].value}"
                            )
                        else:
                            # 券商端在途 → QMT 已收到，不补
                            result["in_flight"] += 1
                            per["in_flight"] += 1
                except Exception as e:  # noqa: BLE001 - 单券商对账异常不阻断其余
                    per["skipped"] += 1
                    result["skipped"] += 1
                    logger.warning(f"[broker] {alias} 补单对账异常: {e}")
                result["per_broker"][alias] = per
        self._last_resend = result
        logger.info(
            f"[broker] 断线补单对账完成: checked={result['checked']} "
            f"resent={result['resent']} failed={result['failed']} "
            f"in_flight={result['in_flight']} terminal={result['terminal']} "
            f"skipped={result['skipped']}"
        )
        return result

    def get_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """获取主券商在途订单（状态快照/启动对账用，KP-P3-03）。"""
        b = self.primary
        if b is None or not hasattr(b, "get_orders"):
            return []
        if symbol:
            return b.get_orders(symbol=symbol)
        return b.get_orders()

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
    #  T+1 日终结算（KP-P3-01）
    # ─────────────────────────────────────────────

    def settle_t1_all(self) -> Dict[str, Any]:
        """
        T+1 日终结算（遍历全部注册券商逐个调用 settle_t1，释放当日买入冻结）

        每个券商只结算自己账上的当日买入冻结（PaperBroker/QmtBroker 的
        _t1_holdings）；无 settle_t1 实现的券商（如真实 QMT 由券商自动结算）
        标记为 skipped，不报错。单券商结算异常不阻断其余券商。

        防未来：只释放【已发生】的当日买入冻结，不生成任何预测数据。

        Returns
        -------
        Dict[str, Any] :
            {
              "settled": [已结算券商别名],
              "skipped": [无 settle_t1 的券商别名],
              "errors": {alias: 错误信息},
            }
        """
        settled: List[str] = []
        skipped: List[str] = []
        errors: Dict[str, str] = {}
        with self._lock:
            for alias, reg in self._registrations.items():
                broker = reg.broker
                if not hasattr(broker, "settle_t1"):
                    skipped.append(alias)
                    logger.info(f"[broker] T+1日切跳过({alias}): 券商无 settle_t1 实现")
                    continue
                try:
                    broker.settle_t1()
                    settled.append(alias)
                    logger.info(f"[broker] T+1日切结算完成: {alias}")
                except Exception as e:  # noqa: BLE001 - 单券商结算失败不阻断其余
                    errors[alias] = f"{type(e).__name__}: {e}"
                    logger.error(f"[broker] T+1日切结算失败({alias}): {e}")
        return {"settled": settled, "skipped": skipped, "errors": errors}

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
        恢复管理器状态（崩溃恢复后重建主备角色 + 转发子券商恢复）。

        T0-10：除恢复 primary_alias 外，必须把 state 转发给每个已注册子券商的
        restore_state（duck-typing），否则 qmt_broker 等子券商的订单簿/映射表
        不恢复——崩溃恢复后子券商本地状态为空，查单/撤单全错。

        连接状态由调用方 connect_all() 重建（不恢复连接本身）。

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

        # T0-10：转发子券商 restore_state（duck-typing，单个失败不阻断其余）
        for alias, reg in self._registrations.items():
            broker = reg.broker
            if hasattr(broker, "restore_state"):
                try:
                    broker.restore_state(state)
                except Exception as e:  # noqa: BLE001 - 单子券商恢复失败不阻断
                    logger.warning(
                        f"[broker] 子券商 restore_state 失败({alias}): {type(e).__name__}: {e}"
                    )

    def get_summary(self) -> Dict[str, Any]:
        """管理器摘要（券商数 + 主券商 + 各券商健康/角色/连接 + failover 统计 + 最近补单对账）"""
        return {
            "broker_count": len(self._registrations),
            "primary_alias": self._primary_alias,
            "auto_failover": self._auto_failover,
            "failover_cooldown": self._failover_cooldown,
            "failover_stats": dict(self._failover_stats),
            "pre_failover_primary": self._pre_failover_primary,
            "last_resend": dict(self._last_resend),
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
