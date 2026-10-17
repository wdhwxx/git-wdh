"""T0-18 回路验证：paper 下制造 1 分钱差异，观察冻结→是否被镜像抹平→能否解冻。

PR-B 是否闭环的唯一判据：
- 冻结后差异不再被覆写（book_cash 保持不变）
- 解冻入口可用（adopt_broker_ledger 显式采用 broker 账本）
"""
from myquant.live.portfolio import LivePortfolio


class MockBroker:
    """模拟券商：返回指定 cash 和 positions。"""
    def __init__(self, cash: float = 100000.0, positions: dict = None):
        self._cash = cash
        self._positions = positions or {}

    def get_account(self):
        return {"cash": self._cash, "frozen_cash": 0.0, "total_assets": self._cash}

    def get_positions(self):
        return self._positions


class TestT018ReconciliationLoop:
    """T0-18 回路验证：冻结→不被抹平→解冻可用。"""

    def setup_method(self):
        """每个测试独立 portfolio。"""
        self.portfolio = LivePortfolio(initial_capital=100000.0)

    def test_freeze_not_overwrite_book_cash(self):
        """PR-B 核心判据：冻结后 book_cash 不被 broker_cash 覆写。"""
        # 初始状态：book_cash = 100000
        assert self.portfolio.book_cash == 100000.0
        assert self.portfolio.broker_cash == 0.0
        assert not self.portfolio.is_reconciliation_blocked

        # 制造 1 分钱差异：broker 返回 99999.99
        broker = MockBroker(cash=99999.99)
        _diff = self.portfolio.sync_from_broker(broker)

        # 应该冻结（差异超容差）
        assert self.portfolio.is_reconciliation_blocked, "差异超阈应置 blocked"

        # 关键判据：book_cash 保持 100000（不被 broker_cash 覆写）
        assert self.portfolio.book_cash == 100000.0, "冻结后 book_cash 不应被 broker_cash 覆写"

        # broker_cash 已刷新为 99999.99（双值并存）
        assert self.portfolio.broker_cash == 99999.99, "broker_cash 应已刷新为券商快照值"

    def test_adopt_broker_ledger_unfreeze(self):
        """解冻入口：adopt_broker_ledger 显式采用 broker 账本，解除 blocked。"""
        # 先制造差异触发冻结
        broker = MockBroker(cash=99999.99)
        self.portfolio.sync_from_broker(broker)
        assert self.portfolio.is_reconciliation_blocked

        # 人工核查后，显式采用 broker 账本
        self.portfolio.adopt_broker_ledger(reason="人工核查确认差异", actor="admin")

        # 解冻后：book_cash 变成 broker_cash，blocked 解除
        assert not self.portfolio.is_reconciliation_blocked, "adopt 后应解除 blocked"
        assert self.portfolio.book_cash == 99999.99, "adopt 后 book_cash 应等于 broker_cash"

    def test_no_freeze_when_diff_within_tolerance(self):
        """容差内差异不冻结，且正常同步（T0-15 round 2 位比较）。"""
        # 制造 0.001 元差异（round 2 位后 diff=0，不超阈）
        broker = MockBroker(cash=100000.001)
        _diff = self.portfolio.sync_from_broker(broker)

        # 不应冻结（round 2 位后 diff=0）
        assert not self.portfolio.is_reconciliation_blocked, "容差内差异不应冻结"
        # 容差内正常同步：book_cash 变成 broker_cash
        assert self.portfolio.book_cash == 100000.001, "容差内应正常同步，book_cash=broker_cash"

    def test_no_freeze_when_broker_returns_zero(self):
        """T0-3 健全性闸：broker 返回全 0 → 拒绝覆盖 + CRITICAL（不冻结但也不覆写）。"""
        # broker 返回 cash=0（异常情况）
        broker = MockBroker(cash=0.0, positions={})
        _diff = self.portfolio.sync_from_broker(broker)

        # 健全性闸应该触发（cash 从 100000 骤降到 0）
        # 预期：blocked=True（健全性闸拒绝覆盖），book_cash 保持 100000
        assert self.portfolio.is_reconciliation_blocked, "broker 返回全 0 应触发健全性闸"
        assert self.portfolio.book_cash == 100000.0, "健全性闸拒绝覆盖，book_cash 不变"
