# 文件名：tests/unit/test_broker_manager_failover.py
"""
test_broker_manager_failover.py - BrokerManager failover 盲目重试约束化测试（任务44）
=====================================================================================
覆盖第四项"failover 盲目重试"修复：
  1. 失败分类 —— 业务拒单（资金/持仓/风控/T+1/涨跌停/校验等）绝不 failover；
     通道故障（未连接/委托异常/断线/超时/SDK）才允许 failover；
     无法分类原因 → 保守不重试（安全第一，防绕过主券商风控）。
  2. 副本重试 —— failover 用干净副本（同一 order_id、PENDING 状态），
     原订单保留主券商拒单留痕，不伪造状态；副本 extra 独立不共享引用。
  3. 冷却熔断 —— failover 全部失败后进入冷却期，冷却期内不再触发
     （防网络波动信号重发导致 failover 风暴）。
  4. 健康恢复 —— failover 成功切换后，原主健康恢复且过冷却 → 切回原主。
  5. 统计可观测 —— get_summary 含 failover_stats。

依赖: pytest, myquant.live.brokers.manager（BrokerManager）
      myquant.live.brokers.base_broker（BaseBroker/BrokerType）
      myquant.models.trade（Order/OrderSide/OrderType/OrderStatus）
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from myquant.live.brokers.base_broker import BaseBroker, BrokerType
from myquant.live.brokers.manager import BrokerManager, BrokerManagerError
from myquant.models.trade import Order, OrderSide, OrderStatus, OrderType

# ═══════════════════════════════════════════════
#  FakeBroker - 可编程测试券商
# ═══════════════════════════════════════════════

class FakeBroker(BaseBroker):
    """
    可编程测试券商（继承 BaseBroker，行为由 mode 控制）

    mode:
      "success" → submit_order 成功（订单置 SUBMITTED 并返回）
      "reject"  → submit_order 以 reject_reason 拒单（订单置 REJECTED 并返回）
      "raise"   → submit_order 抛 RuntimeError（模拟通道异常）
    """

    broker_name = "fake_broker"
    broker_type = BrokerType.CUSTOM

    def __init__(
        self,
        name: str = "fake",
        mode: str = "success",
        reject_reason: str = "",
        healthy: bool = True,
    ) -> None:
        super().__init__(name=name)
        self._mode = mode
        self._reject_reason = reject_reason
        self._healthy = healthy
        self.submit_calls: List[Order] = []   # 收到的全部订单（含副本）
        self.received_statuses: List[OrderStatus] = []  # 提交前状态快照

    # ── 抽象方法实现 ──
    def _submit_order_impl(self, order: Order) -> Order:
        self.submit_calls.append(order)
        self.received_statuses.append(order.status)   # 提交前状态（验证副本为 PENDING）
        if self._mode == "raise":
            raise RuntimeError(self._reject_reason or "模拟通道异常")
        if self._mode == "reject":
            order.reject(self._reject_reason or "模拟拒单")
            return order
        order.submit()
        return order

    def cancel_order(self, order_id: str) -> bool:
        return True

    def get_order(self, order_id: str) -> Optional[Order]:
        return None

    def get_active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        return []

    def get_account(self) -> Dict[str, Any]:
        return {"cash": 1_000_000.0, "available_cash": 1_000_000.0}

    def get_positions(self) -> List[Any]:
        return []

    def get_trades(self, symbol: Optional[str] = None) -> List[Any]:
        return []

    # ── 健康检查覆盖 ──
    def health_check(self) -> Dict[str, Any]:
        return {
            "broker_name": self.broker_name,
            "broker_type": str(self.broker_type),
            "connected": self._connected and self._healthy,
            "status": "healthy" if (self._connected and self._healthy) else "down",
        }


# ═══════════════════════════════════════════════
#  测试辅助
# ═══════════════════════════════════════════════

def _make_order(order_id: str = "sig123") -> Order:
    return Order(
        order_id=order_id,
        symbol="600519.SH",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=1500.0,
        strategy_id="test_strategy",
    )


def _make_mgr(
    primary_mode: str = "success",
    primary_reason: str = "",
    backup_mode: str = "success",
    backup_reason: str = "",
    cooldown: float = 60.0,
) -> tuple:
    """构造 主+备 双券商 BrokerManager，返回 (mgr, primary, backup)"""
    mgr = BrokerManager(auto_failover=True, failover_cooldown=cooldown)
    primary = FakeBroker(name="primary", mode=primary_mode, reject_reason=primary_reason)
    backup = FakeBroker(name="backup", mode=backup_mode, reject_reason=backup_reason)
    mgr.register_broker(primary, alias="primary", role="primary")
    mgr.register_broker(backup, alias="backup", role="secondary")
    primary.connect()
    backup.connect()
    return mgr, primary, backup


# ═══════════════════════════════════════════════
#  1. 失败分类：业务拒单绝不 failover
# ═══════════════════════════════════════════════

@pytest.mark.parametrize("reason", [
    "资金不足: 需要=150000.00, 可用=100000.00",
    "可卖数量不足: 委托=100, 可卖=0",
    "持仓不足: 无 600519.SH 持仓",
    "T+1冻结=100",
    "风控拦截: 单票仓位超限",
    "涨跌停限制，未成交",
    "订单校验失败: quantity 必须 > 0",
    "订单状态无法提交: REJECTED",
    "QMT废单: 委托数量超过上限",
])
def test_business_reject_never_failover(reason: str) -> None:
    """业务/风控/资金类拒单 → 备券商零调用，主券商保持，统计 business_skipped"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason=reason)
    order = _make_order()
    result = mgr.submit_order(order)
    assert result.status == OrderStatus.REJECTED
    assert backup.submit_calls == []            # 未盲目重试
    assert mgr.primary_alias == "primary"        # 主券商未切换
    assert mgr._failover_stats["business_skipped"] == 1
    assert mgr._failover_stats["attempts"] == 0  # 未触发 failover


def test_unknown_reason_no_failover() -> None:
    """无法分类的拒单原因 → 保守不重试（安全第一）"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason="市场波动，暂停交易")
    result = mgr.submit_order(_make_order())
    assert result.status == OrderStatus.REJECTED
    assert backup.submit_calls == []
    assert mgr._failover_stats["business_skipped"] == 1


# ═══════════════════════════════════════════════
#  2. 通道故障：允许 failover（副本重试）
# ═══════════════════════════════════════════════

@pytest.mark.parametrize("reason", [
    "QMT 未连接，拒绝下单",
    "QMT 委托异常: order_stock 失败",
    "QMT 委托返回空（可能被拒/废单）",
    "QMT 断线回报",
    "连接超时",
    "SDK 调用失败",
])
def test_channel_failover_success(reason: str) -> None:
    """通道类拒单 → failover 到备券商成功，主券商切换、原主保留待恢复"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason=reason)
    order = _make_order()
    result = mgr.submit_order(order)
    assert result.status == OrderStatus.SUBMITTED   # 副本经备券商提交成功
    assert result.order_id == order.order_id        # 幂等键不变
    assert backup.submit_calls != []                # 备券商收到重试
    assert mgr.primary_alias == "backup"            # 主切换为备
    assert mgr._failover_stats["attempts"] == 1
    assert mgr._failover_stats["success"] == 1
    assert mgr._pre_failover_primary == "primary"   # 原主保留（健康恢复切回用）


def test_exception_failover_success() -> None:
    """主券商 submit_order 抛异常（通道异常）→ failover 成功"""
    mgr, primary, backup = _make_mgr(primary_mode="raise", primary_reason="连接断开")
    result = mgr.submit_order(_make_order())
    assert result.status == OrderStatus.SUBMITTED
    assert mgr.primary_alias == "backup"
    assert mgr._failover_stats["success"] == 1


def test_failover_uses_clean_clone() -> None:
    """副本重试：PENDING 状态、order_id 一致、extra 独立、路由标记正确"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason="QMT 未连接，拒绝下单")
    order = _make_order()
    order.extra = {"original_tag": "keep"}
    mgr.submit_order(order)

    assert len(backup.submit_calls) == 1
    retried = backup.submit_calls[0]
    assert backup.received_statuses == [OrderStatus.PENDING]  # 干净副本（提交前为 PENDING）
    assert retried.status == OrderStatus.SUBMITTED             # 备券商接受并提交成功
    assert retried.order_id == order.order_id          # 幂等键一致
    assert retried.extra["broker_id"] == "backup"      # 路由标记=备券商
    assert retried.extra["failover_from"] == "primary" # 记录原主
    assert retried.extra["original_tag"] == "keep"     # 原 extra 保留
    # 副本 extra 独立（不共享引用）
    retried.extra["original_tag"] = "mutated"
    assert order.extra["original_tag"] == "keep"
    # 原订单保持主券商拒单留痕
    assert order.status == OrderStatus.REJECTED
    assert "QMT 未连接" in order.extra["reject_reason"]


# ═══════════════════════════════════════════════
#  3. 冷却熔断：全部失败后不再盲目重试
# ═══════════════════════════════════════════════

def test_all_backup_fail_then_cooldown_blocks() -> None:
    """全部券商失败 → 冷却期内再次提交不再触发 failover（防重试风暴）"""
    mgr, primary, backup = _make_mgr(
        primary_mode="reject", primary_reason="QMT 未连接，拒绝下单",
        backup_mode="reject", backup_reason="QMT 未连接，拒绝下单",
    )
    # 第一次：failover 触发且全部失败
    r1 = mgr.submit_order(_make_order(order_id="sig_a"))
    assert r1.status == OrderStatus.REJECTED
    assert mgr._failover_stats["attempts"] == 1
    assert backup.submit_calls != []
    assert mgr._last_failover_fail_at > 0

    # 第二次（冷却期内）：不再触发 failover
    r2 = mgr.submit_order(_make_order(order_id="sig_b"))
    assert r2.status == OrderStatus.REJECTED
    assert len(backup.submit_calls) == 1             # 备券商调用未增加
    assert mgr._failover_stats["attempts"] == 1      # failover 未再次触发
    assert mgr._failover_stats["cooldown_skipped"] == 1


def test_cooldown_expired_allows_retry() -> None:
    """冷却期过后 failover 恢复可用"""
    mgr, primary, backup = _make_mgr(
        primary_mode="reject", primary_reason="QMT 未连接，拒绝下单",
        backup_mode="reject", backup_reason="QMT 未连接，拒绝下单",
    )
    mgr.submit_order(_make_order(order_id="sig_a"))
    assert mgr._failover_stats["attempts"] == 1
    # 拨过冷却期
    mgr._last_failover_fail_at = time.time() - mgr._failover_cooldown - 1
    mgr.submit_order(_make_order(order_id="sig_b"))
    assert mgr._failover_stats["attempts"] == 2      # 冷却解除后可再次 failover


# ═══════════════════════════════════════════════
#  4. 自动 failover 开关
# ═══════════════════════════════════════════════

def test_auto_failover_disabled_raises() -> None:
    """auto_failover=False → 主券商异常直接抛 BrokerManagerError，不重试"""
    mgr, primary, backup = _make_mgr(primary_mode="raise", primary_reason="连接断开")
    mgr._auto_failover = False
    with pytest.raises(BrokerManagerError):
        mgr.submit_order(_make_order())
    assert backup.submit_calls == []
    assert mgr.primary_alias == "primary"


# ═══════════════════════════════════════════════
#  5. 健康恢复：主券商恢复后切回
# ═══════════════════════════════════════════════

def _do_failover_success(mgr: BrokerManager) -> None:
    mgr.submit_order(_make_order())
    assert mgr.primary_alias == "backup"


def test_restore_primary_after_recovery() -> None:
    """原主健康恢复 + 过冷却 → 自动切回原主"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason="QMT 未连接，拒绝下单")
    _do_failover_success(mgr)
    # 拨过恢复冷却期
    mgr._last_failover_success_at = time.time() - mgr._failover_cooldown - 1
    restored = mgr.restore_primary_if_healthy()
    assert restored == "primary"
    assert mgr.primary_alias == "primary"
    assert mgr._failover_stats["restore_count"] == 1
    assert mgr._pre_failover_primary is None


def test_restore_skipped_when_unhealthy() -> None:
    """原主仍不健康 → 不切回"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason="QMT 未连接，拒绝下单")
    _do_failover_success(mgr)
    primary.disconnect()                              # 原主断线（不健康）
    mgr._last_failover_success_at = time.time() - mgr._failover_cooldown - 1
    assert mgr.restore_primary_if_healthy() is None
    assert mgr.primary_alias == "backup"
    assert mgr._failover_stats["restore_count"] == 0


def test_restore_skipped_in_cooldown() -> None:
    """恢复冷却期内 → 不切回（防抖动反复切换）"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason="QMT 未连接，拒绝下单")
    _do_failover_success(mgr)
    # 冷却期内（未拨钟）
    assert mgr.restore_primary_if_healthy() is None
    assert mgr.primary_alias == "backup"


def test_manual_set_primary_clears_restore() -> None:
    """failover 后手动切主 → 清除恢复记忆，不再自动切回"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason="QMT 未连接，拒绝下单")
    _do_failover_success(mgr)
    mgr.set_primary("primary")                        # 手动切回
    assert mgr._pre_failover_primary is None
    mgr._last_failover_success_at = time.time() - mgr._failover_cooldown - 1
    assert mgr.restore_primary_if_healthy() is None   # 无恢复语义


# ═══════════════════════════════════════════════
#  6. 统计可观测 / 摘要
# ═══════════════════════════════════════════════

def test_summary_contains_failover_stats() -> None:
    """get_summary 暴露 failover 统计（attempts/success/冷却/恢复记忆）"""
    mgr, primary, backup = _make_mgr(primary_mode="reject", primary_reason="QMT 未连接，拒绝下单")
    mgr.submit_order(_make_order())
    summary = mgr.get_summary()
    assert summary["failover_stats"]["success"] == 1
    assert summary["failover_cooldown"] == 60.0
    assert summary["pre_failover_primary"] == "primary"


def test_failover_stats_reset_per_instance() -> None:
    """不同实例统计隔离（每次新增实例从零计数）"""
    mgr_a, _, _ = _make_mgr(primary_mode="reject", primary_reason="QMT 未连接，拒绝下单")
    mgr_b, _, _ = _make_mgr(primary_mode="success")
    mgr_a.submit_order(_make_order())
    assert mgr_a._failover_stats["attempts"] == 1
    assert mgr_b._failover_stats["attempts"] == 0
