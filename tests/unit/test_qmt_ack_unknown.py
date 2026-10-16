# 文件名：tests/unit/test_qmt_ack_unknown.py
"""
test_qmt_ack_unknown.py - T0-12 ACK_UNKNOWN 查单确认闭环专项测试
================================================================
覆盖 myquant/live/brokers/qmt_broker.py 的 _handle_unknown_submit()：
  1. 置位 ACK_UNKNOWN（状态转移走 OrderStateMachine，非法抛 ExecutionError）
  2. query_broker_orders 返回 None（查询本身失败）→ 保持 ACK_UNKNOWN，不判死
  3. 查单成功但 N 轮全部未命中 → 窗口走完转 REJECTED
  4. 查单命中 → 建立 _local_to_qmt/_qmt_to_local 映射 + 状态对齐 SUBMITTED
  5. 查单命中且券商已 PARTIAL_FILLED/FILLED → 状态对齐成交态
  6. 非法转移（订单已在终态时调 _handle_unknown_submit）→ 不崩溃、留痕
  7. _build_ack_unknown_delays 退避序列生成正确性（窗口 30s → 指数分布 ≥3 轮）

测试方式：SIM 模式实例 + mock query_broker_orders，不连真 SDK。
为加速测试，_ack_unknown_window 设为极小值（0.1s），time.sleep 实际等待可忽略。

运行方式:
  pytest tests/unit/test_qmt_ack_unknown.py -v
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from myquant.live.brokers.qmt_broker import QmtBroker
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType


def _make_broker() -> QmtBroker:
    """构造 SIM 模式 QmtBroker（不连真 SDK），窗口压到极小加速测试"""
    b = QmtBroker(mode="SIM")
    b._ack_unknown_window = 0.1  # 测试用极小窗口，sleep 可忽略
    return b


def _make_order() -> Order:
    """构造一个已 SUBMITTED 的本地订单（_handle_unknown_submit 的前置状态）"""
    o = Order(
        order_id="test_ack_unknown_001",
        symbol="600519.SH",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100.0,
        price=1500.0,
    )
    o.status = OrderStatus.SUBMITTED
    return o


def _patch_query(b: QmtBroker, return_value: Optional[List[Dict[str, Any]]]) -> None:
    """mock query_broker_orders，固定返回预设值"""
    b.query_broker_orders = lambda symbol=None: return_value  # type: ignore[assignment]


# ═══════════════════════════════════════════
#  1. 置位 ACK_UNKNOWN
# ═══════════════════════════════════════════

def test_set_ack_unknown_status() -> None:
    """SUBMITTED → ACK_UNKNOWN 置位成功，extra 留痕"""
    b = _make_broker()
    _patch_query(b, [])  # 空列表=查单成功但未命中，会走 REJECTED 分支
    o = _make_order()
    result = b._handle_unknown_submit(o, "test reason")
    # 窗口走完未命中 → REJECTED（空列表=查单成功但券商端无此单）
    assert result.status == OrderStatus.REJECTED
    # ack_unknown 标记已清理
    assert not result.extra.get("ack_unknown")


def test_ack_unknown_extra_留痕() -> None:
    """ACK_UNKNOWN 置位时 extra 写入 reason 和 ts"""
    b = _make_broker()
    # 让查询失败（None）→ 保持 ACK_UNKNOWN
    _patch_query(b, None)
    o = _make_order()
    result = b._handle_unknown_submit(o, "SDK boom")
    assert result.status == OrderStatus.ACK_UNKNOWN
    assert result.extra.get("ack_unknown") is True
    assert "SDK boom" in result.extra.get("ack_unknown_reason", "")
    assert "ack_unknown_ts" in result.extra


# ═══════════════════════════════════════════
#  2. 查询本身失败 → 保持 ACK_UNKNOWN
# ═══════════════════════════════════════════

def test_query_none_keeps_ack_unknown() -> None:
    """query_broker_orders 返回 None（券商端未连接/SDK异常）→ 保持 ACK_UNKNOWN，不判 REJECTED"""
    b = _make_broker()
    _patch_query(b, None)
    o = _make_order()
    result = b._handle_unknown_submit(o, "连接断了")
    assert result.status == OrderStatus.ACK_UNKNOWN
    # 保持在 ACK_UNKNOWN，交对账循环闭环


def test_query_exception_keeps_ack_unknown() -> None:
    """query_broker_orders 抛异常 → 按查询失败处理，保持 ACK_UNKNOWN"""
    b = _make_broker()
    def _raise(symbol=None):
        raise RuntimeError("SDK timeout")
    b.query_broker_orders = _raise  # type: ignore[assignment]
    o = _make_order()
    result = b._handle_unknown_submit(o, "查单炸了")
    assert result.status == OrderStatus.ACK_UNKNOWN


# ═══════════════════════════════════════════
#  3. 查单成功但未命中 → 窗口走完 REJECTED
# ═══════════════════════════════════════════

def test_no_match_rejected() -> None:
    """查单成功（返回列表）但没有匹配 order_id → 窗口走完转 REJECTED"""
    b = _make_broker()
    # 返回其他订单，不是本单
    _patch_query(b, [{"order_id": "other_order_xxx", "qmt_order_id": 999, "status": "SUBMITTED"}])
    o = _make_order()
    result = b._handle_unknown_submit(o, "券商端没这单")
    assert result.status == OrderStatus.REJECTED


# ═══════════════════════════════════════════
#  4. 命中 → 建立映射 + 状态对齐 SUBMITTED
# ═══════════════════════════════════════════

def test_match_maps_and_aligns_submitted() -> None:
    """查单命中 → 建立双向映射 + 状态从 ACK_UNKNOWN 对齐回 SUBMITTED"""
    b = _make_broker()
    o = _make_order()
    key = str(o.order_id)[:24]
    _patch_query(b, [{
        "order_id": key,
        "qmt_order_id": 12345,
        "status": OrderStatus.SUBMITTED,
    }])
    result = b._handle_unknown_submit(o, "SDK 异常但已受理")
    # 命中 → 状态对齐 SUBMITTED
    assert result.status == OrderStatus.SUBMITTED
    # 双向映射建立
    assert b._local_to_qmt[o.order_id] == 12345
    assert b._qmt_to_local[12345] == o.order_id
    assert result.extra.get("qmt_order_id") == 12345
    # ack_unknown 标记清理
    assert not result.extra.get("ack_unknown")


# ═══════════════════════════════════════════
#  5. 命中且券商已成交 → 对齐成交态
# ═══════════════════════════════════════════

@pytest.mark.parametrize("remote_status", [
    OrderStatus.PARTIAL_FILLED,
    OrderStatus.FILLED,
])
def test_match_aligns_filled(remote_status: OrderStatus) -> None:
    """查单命中且券商已 PARTIAL_FILLED/FILLED → 直接对齐成交态"""
    b = _make_broker()
    o = _make_order()
    key = str(o.order_id)[:24]
    _patch_query(b, [{
        "order_id": key,
        "qmt_order_id": 54321,
        "status": remote_status,
    }])
    result = b._handle_unknown_submit(o, "成交了但回报丢了")
    assert result.status == remote_status


# ═══════════════════════════════════════════
#  6. 非法转移（订单已在终态）不崩溃
# ═══════════════════════════════════════════

def test_illegal_transition_does_not_crash() -> None:
    """订单已在终态（如 FILLED）时调 _handle_unknown_submit → 状态机抛错被捕获，不崩溃"""
    b = _make_broker()
    _patch_query(b, None)  # 保持 ACK_UNKNOWN
    o = _make_order()
    o.status = OrderStatus.FILLED  # 终态，不能转 ACK_UNKNOWN
    # 不应抛异常（内部 try/except 捕获状态机错误）
    result = b._handle_unknown_submit(o, "非法转移测试")
    # 状态机转移失败，但方法仍返回订单（留痕）
    assert result is o


# ═══════════════════════════════════════════
#  7. 退避序列生成
# ═══════════════════════════════════════════

def test_build_delays_default_30s() -> None:
    """窗口 30 秒 → 指数退避序列 ≥3 轮，累加≈30"""
    b = _make_broker()
    delays = b._build_ack_unknown_delays(30.0)
    assert len(delays) >= 3
    # 前几轮是指数翻倍
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    # 总累加 ≈ 窗口
    assert abs(sum(delays) - 30.0) < 1.0


def test_build_delays_tiny_window() -> None:
    """窗口过小（0.1s）→ 至少 3 轮（用户要求 ≥3）"""
    b = _make_broker()
    delays = b._build_ack_unknown_delays(0.1)
    assert len(delays) >= 3


def test_build_delays_zero_window() -> None:
    """窗口 ≤0 → 防御性返回单轮"""
    b = _make_broker()
    delays = b._build_ack_unknown_delays(0)
    assert len(delays) >= 1
