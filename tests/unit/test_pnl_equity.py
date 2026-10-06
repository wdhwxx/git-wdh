# 文件名：tests/unit/test_pnl_equity.py
"""
test_pnl_equity.py - B-2 PnL 可信与权益曲线 单元测试
=================================================================
覆盖（不依赖真实 quant.db，全部用临时库 + 注入假对账器）：
  1. BuiltinMetrics 登记：B-2 五个 live_* PnL 指标 + 23 个 Wave1 指标已注册
  2. 防脏门：对账一致（未冻结）时 collect_pnl_snapshot 写时序库成功，
     落库值与 portfolio.get_account() 一致（PnL 与对账一致）
  3. 防脏门：对账冻结时不写时序库（written=False/blocked=True），且经
     reconciler.note_pnl_blocked 留痕，库内无新增 PnL 行
  4. 当日权益曲线回放：只回放当日（local 零点起）升序点，昨日点被排除
  5. 最新 PnL 快照读取：get_latest_pnl_snapshot 取各指标最新一点 + 对账状态
  6. API 端点：/api/monitor/pnl 与 /api/monitor/equity_curve 返回与采集一致
     （直接调用 route function，无 httpx；与 test_dashboard_monitor 同范式）

运行方式:
  pytest tests/unit/test_pnl_equity.py -v
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.api.routes import create_app  # noqa: E402
from myquant.api.dependencies.get_services import get_service_container  # noqa: E402
from myquant.database.connection import DatabaseConnection  # noqa: E402
from myquant.live.portfolio import LivePortfolio  # noqa: E402
from myquant.monitoring.field_crypto import FieldCrypto  # noqa: E402
from myquant.monitoring.metrics_collector import (  # noqa: E402
    BuiltinMetrics,
    collect_pnl_snapshot,
    get_latest_pnl_snapshot,
    replay_intraday_equity_curve,
)
from myquant.monitoring.metrics_history import MetricsHistoryStore  # noqa: E402


# ═══════════════════════════════════════════════
#  Fakes / Fixtures
# ═══════════════════════════════════════════════

class _FakeReconciler:
    """假账户对账器（对齐 AccountReconciler 的 is_frozen/is_consistent/to_dict/note_pnl_blocked）。"""

    def __init__(self, frozen: bool = False) -> None:
        self._frozen = frozen
        self.blocked_reasons: List[str] = []

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def is_consistent(self) -> bool:
        return not self._frozen

    def note_pnl_blocked(self, reason: str = "") -> None:
        self.blocked_reasons.append(reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": "default",
            "consistent": not self._frozen,
            "frozen": self._frozen,
            "in_flight": False,
            "last_check_ts": time.time(),
            "resolved_by": None,
            "last_diff": None,
            "last_t1_diff": None,
        }


@pytest.fixture()
def store(tmp_path: Path) -> MetricsHistoryStore:
    """临时库时序库（绑定 FieldCrypto，资金敏感名加密落盘）。"""
    conn = DatabaseConnection(str(tmp_path / "pnl_equity.db"))
    s = MetricsHistoryStore(connection=conn)
    s.attach_field_crypto(FieldCrypto(key_file=str(tmp_path / "k")))
    return s


@pytest.fixture()
def portfolio() -> LivePortfolio:
    """真实 LivePortfolio（初始资金 1,000,000；无持仓）。"""
    return LivePortfolio(initial_capital=1_000_000.0, account_id="default")


def _find_endpoint(app: Any, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", []):
            return route.endpoint
    raise AssertionError(f"未找到端点 {method} {path}")


def _run(ep, *args, **kwargs):
    return asyncio.run(ep(*args, **kwargs))


# ═══════════════════════════════════════════════
#  1. BuiltinMetrics 登记
# ═══════════════════════════════════════════════

class TestBuiltinRegistration:
    def test_b2_pnl_metrics_registered(self) -> None:
        names = {
            BuiltinMetrics.LIVE_TOTAL_ASSETS,
            BuiltinMetrics.LIVE_AVAILABLE_CASH,
            BuiltinMetrics.LIVE_POSITION_MARKET_VALUE,
            BuiltinMetrics.LIVE_REALIZED_PNL,
            BuiltinMetrics.LIVE_UNREALIZED_PNL,
        }
        # 23 个 Wave1 指标名（以各模块实际常量为准）
        wave1 = {
            "limit_usage_concentration", "limit_usage_leverage", "limit_usage_single_stock",
            "execution_signal_to_fill_p99_ms", "execution_ack_p99_ms",
            "execution_signal_to_submit_p99_ms", "execution_submit_to_fill_p99_ms",
            "slippage_buy_pct", "slippage_sell_pct",
            "slippage_buy_p99_pct", "slippage_sell_p99_pct",
            "order_reject_rate", "orders_submitted_total", "orders_rejected_total",
            "data_freshness_lag_seconds", "data_gap_count",
            "factor_sync_delay_seconds", "financial_sync_delay_seconds",
            "margin_usage_ratio", "maintenance_margin", "liquidation_distance_pct",
            "funding_rate", "funding_cost_estimate",
        }
        from myquant.monitoring.metrics_collector import MetricsCollector
        c = MetricsCollector()
        c.register_builtin_metrics()
        for n in names | wave1:
            assert c.is_registered(n), f"指标未登记: {n}"


# ═══════════════════════════════════════════════
#  2/3. 防脏门：一致写库 / 冻结不写+留痕
# ═══════════════════════════════════════════════

class TestPnlGate:
    def test_consistent_writes_to_history(self, store, portfolio) -> None:
        recon = _FakeReconciler(frozen=False)
        portfolio.set_account_reconciler(recon)
        result = collect_pnl_snapshot(portfolio=portfolio, reason="periodic",
                                      reconciler=recon, store=store)
        assert result is not None
        assert result["written"] is True
        assert result["blocked"] is False
        # 落库值与账户一致
        acct = portfolio.get_account()
        assert result["values"][BuiltinMetrics.LIVE_TOTAL_ASSETS] == pytest.approx(
            acct["total_assets"])
        assert result["values"][BuiltinMetrics.LIVE_AVAILABLE_CASH] == pytest.approx(
            acct["available_cash"])
        assert result["values"][BuiltinMetrics.LIVE_POSITION_MARKET_VALUE] == pytest.approx(
            acct["market_value"])
        # 库内确有行（敏感名解密回读真实值）
        rows = store.query(name=BuiltinMetrics.LIVE_TOTAL_ASSETS, limit=5)
        assert len(rows) == 1
        assert rows[0]["value"] == pytest.approx(acct["total_assets"])

    def test_frozen_not_written_and_audited(self, store, portfolio) -> None:
        recon = _FakeReconciler(frozen=True)
        portfolio.set_account_reconciler(recon)
        # 冻结前先写一行（应有 1 行）
        warm = _FakeReconciler(frozen=False)
        portfolio.set_account_reconciler(warm)
        collect_pnl_snapshot(portfolio=portfolio, store=store, reconciler=warm)
        before = len(store.query(name=BuiltinMetrics.LIVE_TOTAL_ASSETS, limit=10))
        assert before == 1
        # 冻结后再采集 → 拒绝
        portfolio.set_account_reconciler(recon)
        result = collect_pnl_snapshot(portfolio=portfolio, reason="manual",
                                      reconciler=recon, store=store)
        assert result["written"] is False
        assert result["blocked"] is True
        # 库内无新增
        after = len(store.query(name=BuiltinMetrics.LIVE_TOTAL_ASSETS, limit=10))
        assert after == before
        # 留痕（portfolio 防脏门调用 reconciler.note_pnl_blocked）
        assert recon.blocked_reasons == ["manual"]

    def test_none_portfolio_returns_none(self, store) -> None:
        assert collect_pnl_snapshot(portfolio=None, store=store) is None


# ═══════════════════════════════════════════════
#  4. 当日权益曲线回放
# ═══════════════════════════════════════════════

class TestEquityCurveReplay:
    def test_replay_only_today_ascending(self, store, portfolio) -> None:
        recon = _FakeReconciler(frozen=False)
        portfolio.set_account_reconciler(recon)
        now = time.time()
        # 当日三个点（防未来：均为当前时刻之前）
        for i in range(3):
            collect_pnl_snapshot(
                portfolio=portfolio, store=store, reconciler=recon,
                timestamp=now - 300 + i * 60,
            )
        curve = replay_intraday_equity_curve(store=store, now_ts=now)
        assert len(curve) == 3
        ts = [p["ts"] for p in curve]
        assert ts == sorted(ts)  # 升序
        # equity 字段 = live_total_assets
        assert all("equity" in p and "ts" in p for p in curve)

    def test_yesterday_point_excluded(self, store, portfolio) -> None:
        recon = _FakeReconciler(frozen=False)
        portfolio.set_account_reconciler(recon)
        now = time.time()
        # 昨日点（手工落库，绕过 collect 的当日约束）
        store.save_metric(BuiltinMetrics.LIVE_TOTAL_ASSETS, 999_999.0,
                          labels={"account_id": "default"}, timestamp=now - 2 * 86400)
        # 当日点
        collect_pnl_snapshot(portfolio=portfolio, store=store, reconciler=recon,
                             timestamp=now - 60)
        curve = replay_intraday_equity_curve(store=store, now_ts=now)
        values = [p["equity"] for p in curve]
        assert 999_999.0 not in values  # 昨日点被排除
        assert len(curve) == 1


# ═══════════════════════════════════════════════
#  5. 最新 PnL 快照读取
# ═══════════════════════════════════════════════

class TestLatestPnl:
    def test_latest_snapshot_matches(self, store, portfolio) -> None:
        recon = _FakeReconciler(frozen=False)
        portfolio.set_account_reconciler(recon)
        collect_pnl_snapshot(portfolio=portfolio, store=store, reconciler=recon)
        snap = get_latest_pnl_snapshot(store=store, reconciler=recon)
        acct = portfolio.get_account()
        assert snap["pnl"]["total_assets"] == pytest.approx(acct["total_assets"])
        assert snap["pnl"]["available_cash"] == pytest.approx(acct["available_cash"])
        assert snap["reconciliation"]["consistent"] is True

    def test_empty_store_graceful(self, tmp_path: Path) -> None:
        conn = DatabaseConnection(str(tmp_path / "empty.db"))
        empty = MetricsHistoryStore(connection=conn)
        snap = get_latest_pnl_snapshot(store=empty)
        assert snap["pnl"]["total_assets"] is None
        conn.close_all()


# ═══════════════════════════════════════════════
#  6. API 端点与采集一致
# ═══════════════════════════════════════════════

class TestPnlEndpoints:
    def _app(self, store: MetricsHistoryStore, recon: _FakeReconciler):
        container = get_service_container()
        container.register("metrics_history_store", store)
        container.register("account_reconciler", recon)
        app = create_app(enable_auth=False, enable_rate_limit=False)
        return app

    def test_pnl_and_equity_endpoints(self, store, portfolio) -> None:
        recon = _FakeReconciler(frozen=False)
        portfolio.set_account_reconciler(recon)
        collect_pnl_snapshot(portfolio=portfolio, store=store, reconciler=recon)
        app = self._app(store, recon)

        ep_pnl = _find_endpoint(app, "/api/monitor/pnl", "GET")
        resp = _run(ep_pnl)
        data = resp.data
        assert data["pnl"]["total_assets"] == pytest.approx(portfolio.get_total_assets())
        assert data["reconciliation"]["frozen"] is False

        ep_curve = _find_endpoint(app, "/api/monitor/equity_curve", "GET")
        resp2 = _run(ep_curve)
        assert resp2.data["count"] >= 1
        assert resp2.data["points"][0]["equity"] == pytest.approx(portfolio.get_total_assets())
