# 文件名：myquant/execution/state_machine.py
"""
execution/state_machine.py - 订单生命周期状态机
==================================================
统一管理订单状态流转，确保状态转换合法性。

状态流转（A股写实）:
  待报(PENDING) → 已报(SUBMITTED) → 部成(PARTIAL_FILLED) → 已成(FILLED)
                    ↓                    ↓
                  废单(REJECTED)      已撤(CANCELLED)
                    ↓                    ↓
                  已过期(EXPIRED)    已过期(EXPIRED)

设计原则:
  - 与 models/trade.py 中的 OrderStatus 枚举和 Order.transition_to() 配合使用
  - 提供独立的状态机校验器，确保任何状态转换都合法
  - 回测模拟撮合和实盘券商适配器共用此状态机
  - 非法状态转换抛出异常，防止订单状态混乱

防未来函数（规则4）:
  - 状态机只管理订单状态，不涉及价格/时间
  - 撮合时机由引擎层控制（T+1成交），状态机不干预
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from typing import Dict, Set

from myquant.core.exceptions import ExecutionError
from myquant.core.logger import get_logger
from myquant.models.trade import Order, OrderStatus

logger = get_logger(__name__)


# ═══════════════════════════════════════════════
#  合法状态转换表（A股写实）
# ═══════════════════════════════════════════════

VALID_TRANSITIONS: Dict[OrderStatus, Set[OrderStatus]] = {
    # 待报：可提交/拒绝/撤销
    OrderStatus.PENDING: {
        OrderStatus.SUBMITTED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
    },
    # 已报：可部分成交/全部成交/撤销/拒绝/过期
    OrderStatus.SUBMITTED: {
        OrderStatus.PARTIAL_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    },
    # 部成：可继续部成/全部成交/撤销/过期
    OrderStatus.PARTIAL_FILLED: {
        OrderStatus.PARTIAL_FILLED,  # 继续部分成交
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    # 终态：不可再转换
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}


# ═══════════════════════════════════════════════
#  订单状态机
# ═══════════════════════════════════════════════

class OrderStateMachine:
    """
    订单生命周期状态机

    核心功能:
      - 校验状态转换合法性
      - 驱动订单状态流转
      - 提供状态查询工具
      - 记录状态转换历史（用于审计/调试）

    使用方式:
        sm = OrderStateMachine()
        sm.submit(order)       # PENDING → SUBMITTED
        sm.fill(order, qty, price)  # 部分/全部成交
        sm.cancel(order)       # 撤销
        sm.reject(order, reason)  # 废单
    """

    def __init__(self):
        """初始化状态机"""
        self._transition_history = []  # 状态转换历史（审计用）
        logger.debug("订单状态机初始化")

    # ------------------------------------------------------------------
    # 状态转换校验
    # ------------------------------------------------------------------

    @staticmethod
    def can_transition(from_status: OrderStatus, to_status: OrderStatus) -> bool:
        """
        校验状态转换是否合法

        参数:
            from_status: 当前状态
            to_status: 目标状态

        返回:
            True=合法，False=非法
        """
        allowed = VALID_TRANSITIONS.get(from_status, set())
        return to_status in allowed

    def _transition(self, order: Order, to_status: OrderStatus, reason: str = "") -> bool:
        """
        执行状态转换（内部方法）

        参数:
            order: 订单对象
            to_status: 目标状态
            reason: 转换原因（用于日志/审计）

        返回:
            True=转换成功

        异常:
            ExecutionError: 非法状态转换
        """
        from_status = order.status
        if not self.can_transition(from_status, to_status):
            raise ExecutionError(
                f"非法订单状态转换: {from_status.value} → {to_status.value}, "
                f"order_id={order.order_id}, reason={reason}"
            )

        success = order.transition_to(to_status)
        if success:
            self._transition_history.append({
                "order_id": order.order_id,
                "from": from_status.value,
                "to": to_status.value,
                "reason": reason,
            })
            logger.debug(
                f"订单状态转换: {order.order_id[:8]}.. "
                f"{from_status.value} → {to_status.value} ({reason})"
            )
        return success

    # ------------------------------------------------------------------
    # 状态驱动方法
    # ------------------------------------------------------------------

    def submit(self, order: Order) -> bool:
        """
        提交订单（PENDING → SUBMITTED）

        参数:
            order: 订单对象

        返回:
            True=提交成功
        """
        return self._transition(order, OrderStatus.SUBMITTED, "submit")

    def fill(self, order: Order, quantity: float, price: float) -> bool:
        """
        订单成交（部分或全部）

        - 首次成交: SUBMITTED → PARTIAL_FILLED 或 FILLED
        - 继续成交: PARTIAL_FILLED → PARTIAL_FILLED 或 FILLED

        参数:
            order: 订单对象
            quantity: 本次成交数量
            price: 本次成交价格

        返回:
            True=成交处理成功
        """
        # 调用 Order.fill() 更新成交数量和价格
        is_full = order.fill(quantity, price)

        if is_full:
            # 全部成交
            if order.status != OrderStatus.FILLED:
                self._transition(order, OrderStatus.FILLED, "full_fill")
        else:
            # 部分成交
            if order.status != OrderStatus.PARTIAL_FILLED:
                self._transition(order, OrderStatus.PARTIAL_FILLED, "partial_fill")

        return True

    def cancel(self, order: Order) -> bool:
        """
        撤销订单（SUBMITTED/PARTIAL_FILLED → CANCELLED）

        参数:
            order: 订单对象

        返回:
            True=撤销成功
        """
        return self._transition(order, OrderStatus.CANCELLED, "cancel")

    def reject(self, order: Order, reason: str = "") -> bool:
        """
        废单（PENDING/SUBMITTED → REJECTED）

        参数:
            order: 订单对象
            reason: 废单原因

        返回:
            True=废单成功
        """
        return self._transition(order, OrderStatus.REJECTED, f"reject: {reason}")

    def expire(self, order: Order) -> bool:
        """
        订单过期（SUBMITTED/PARTIAL_FILLED → EXPIRED）

        参数:
            order: 订单对象

        返回:
            True=过期成功
        """
        return self._transition(order, OrderStatus.EXPIRED, "expire")

    # ------------------------------------------------------------------
    # 状态查询工具
    # ------------------------------------------------------------------

    @staticmethod
    def is_active(order: Order) -> bool:
        """订单是否活跃（可撤销/可成交）"""
        return order.status.is_active

    @staticmethod
    def is_terminal(order: Order) -> bool:
        """订单是否终态（不可再变更）"""
        return order.status.is_terminal

    @staticmethod
    def is_filled(order: Order) -> bool:
        """订单是否全部成交"""
        return order.status == OrderStatus.FILLED

    @staticmethod
    def is_cancelled(order: Order) -> bool:
        """订单是否已撤销"""
        return order.status == OrderStatus.CANCELLED

    @staticmethod
    def is_rejected(order: Order) -> bool:
        """订单是否被拒绝"""
        return order.status == OrderStatus.REJECTED

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    @property
    def transition_history(self) -> list:
        """状态转换历史（审计用）"""
        return self._transition_history.copy()

    def get_order_transitions(self, order_id: str) -> list:
        """获取指定订单的状态转换历史"""
        return [t for t in self._transition_history if t["order_id"] == order_id]

    def __repr__(self) -> str:
        return f"OrderStateMachine(transitions={len(self._transition_history)})"
