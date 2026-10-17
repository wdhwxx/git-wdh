"""T0-17 测试：克隆订单副本移入循环内，每个备券商用全新副本。

原缺陷：第 1 个备券商提交后 retry_order 状态改变，第 2、3 个备券商再用同一对象会被幂等入口拒绝。
修复：每个备券商用全新副本（不共享状态）。
"""
from unittest.mock import MagicMock

from myquant.live.brokers.manager import BrokerManager
from myquant.models.trade import Order, OrderSide, OrderType


def make_mock_broker(alias: str):
    """创建 mock 券商。"""
    broker = MagicMock()
    broker.broker_name = alias
    broker.received_orders = []

    def submit_order_side_effect(order):
        broker.received_orders.append(order)
        # 返回 REJECTED（模拟备券商拒单，验证下一个备券商用全新副本重试）
        return Order(
            symbol=order.symbol, side=order.side, order_type=order.order_type,
            quantity=order.quantity, price=order.price,
            order_id=order.order_id, status="REJECTED",
            extra={"reject_reason": f"{alias} 拒单"},
        )

    broker.submit_order.side_effect = submit_order_side_effect
    return broker


class TestT017CloneOrderInLoop:
    """T0-17：克隆订单副本移入循环内。"""

    def _setup_3_brokers(self):
        """装配 3 个券商（直接操作 _registrations，跳过类型检查）。"""
        mgr = BrokerManager()
        primary = make_mock_broker("primary")
        backup1 = make_mock_broker("backup1")
        backup2 = make_mock_broker("backup2")

        # 直接往 _registrations 塞（绕过 register_broker 类型检查）
        from myquant.live.brokers.manager import BrokerRegistration
        mgr._registrations = {
            "primary": BrokerRegistration(broker=primary, alias="primary", role="primary"),
            "backup1": BrokerRegistration(broker=backup1, alias="backup1", role="secondary"),
            "backup2": BrokerRegistration(broker=backup2, alias="backup2", role="secondary"),
        }
        mgr._primary_alias = "primary"
        return mgr, primary, backup1, backup2

    def test_clone_order_in_loop_each_broker_gets_fresh_copy(self):
        """每个备券商收到的订单是全新副本（不共享状态）。"""
        mgr, primary, backup1, backup2 = self._setup_3_brokers()

        # 造一个订单
        order = Order(
            symbol="600000.SH", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=100, price=10.0, order_id="test_t017_001",
        )

        # 主券商抛异常（通道故障），触发 failover
        primary.submit_order.side_effect = Exception("主券商连接超时")

        # 调用 submit_order（auto_failover=True）
        _result = mgr.submit_order(order)

        # 验证：2 个备券商都收到了订单（每个用全新副本）
        assert len(backup1.received_orders) == 1, "backup1 应收到 1 单"
        assert len(backup2.received_orders) == 1, "backup2 应收到 1 单"

        # 验证：两个备券商收到的订单对象不是同一个（全新副本）
        assert backup1.received_orders[0] is not backup2.received_orders[0], \
            "两个备券商收到的订单应是不同对象（全新副本）"

        # 验证：两个备券商收到的订单 order_id 相同（同一次 failover 重试）
        assert backup1.received_orders[0].order_id == "test_t017_001"
        assert backup2.received_orders[0].order_id == "test_t017_001"

    def test_clone_order_in_loop_no_state_leak(self):
        """第 1 个备券商修改状态后，第 2 个备券商不受影响。"""
        mgr, primary, backup1, backup2 = self._setup_3_brokers()

        order = Order(
            symbol="600000.SH", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=100, price=10.0, order_id="test_t017_002",
        )

        # 主券商抛异常
        primary.submit_order.side_effect = Exception("主券商连接超时")

        # 第 1 个备券商提交时修改订单状态（模拟幂等入口置为 REJECTED）
        def submit_with_state_change(o):
            o.status = "REJECTED"  # 修改共享状态（如果是同一对象，会影响第 2 个备券商）
            backup1.received_orders.append(o)
            return Order(
                symbol=o.symbol, side=o.side, order_type=o.order_type,
                quantity=o.quantity, price=o.price,
                order_id=o.order_id, status="REJECTED",
                extra={"reject_reason": "backup1 拒单"},
            )
        backup1.submit_order.side_effect = submit_with_state_change

        # 第 2 个备券商：如果收到的是全新副本，status 应该是 PENDING（不是 REJECTED）
        backup2_status_received = []
        original_submit = backup2.submit_order.side_effect
        def submit_check_status(o):
            backup2_status_received.append(o.status)
            return original_submit(o)
        backup2.submit_order.side_effect = submit_check_status

        _result = mgr.submit_order(order)

        # 验证：第 2 个备券商收到的订单状态是 PENDING（全新副本，不受第 1 个影响）
        assert len(backup2_status_received) == 1, "backup2 应被调用"
        # OrderStatus.PENDING 是枚举对象，比较其值
        assert str(backup2_status_received[0]) == "PENDING" or backup2_status_received[0].value == "PENDING", \
            "第 2 个备券商收到的订单应是全新副本（status=PENDING），不受第 1 个备券商修改影响"
