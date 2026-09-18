# 文件名：tests/benchmarks/test_execution_latency.py
"""
执行层下单接口延迟基准测试
==============================
测试模拟券商（SimBroker）的下单、撤单、查单、查持仓、查账户等接口延迟，
为实盘券商对接提供延迟基线参考。

运行方式：
  独立运行：python tests/benchmarks/test_execution_latency.py
  pytest收集：pytest tests/benchmarks/test_execution_latency.py -v

阈值（conftest.py 中配置）：
  单次接口调用 < 5ms（均值）

设计原则：
  1. 全部使用内存模拟，无网络IO，延迟反映纯计算开销
  2. 为实盘券商对接预留：本地延迟基线 + 网络延迟 = 实盘端到端延迟
  3. 可独立运行也可 pytest 收集
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# 路径配置
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from _benchmark_utils import BenchmarkRunner  # noqa: E402
from conftest import BENCH_ITERATIONS, BENCH_WARMUP, EXECUTION_LATENCY_MS  # noqa: E402

from myquant.execution.sim_broker import SimBroker  # noqa: E402
from myquant.models.trade import Order, OrderSide, OrderType  # noqa: E402


def _make_broker() -> SimBroker:
    """创建已连接的模拟券商"""
    broker = SimBroker(initial_capital=1000000.0)
    broker.connect()
    return broker


def _make_order(symbol: str = "LAT.SZ", side: OrderSide = OrderSide.BUY,
                order_type: OrderType = OrderType.MARKET,
                quantity: float = 100.0, price: float = 10.0) -> Order:
    """创建订单"""
    return Order(symbol=symbol, side=side, order_type=order_type, quantity=quantity, price=price)


# ═══════════════════════════════════════════
#  下单延迟基准
# ═══════════════════════════════════════════

def test_submit_market_order_latency():
    """市价单下单延迟"""
    broker = _make_broker()
    order = _make_order(order_type=OrderType.MARKET)
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("市价单下单(submit_order)", lambda: broker.submit_order(_make_order()),
               threshold_ms=EXECUTION_LATENCY_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_submit_limit_order_latency():
    """限价单下单延迟"""
    broker = _make_broker()
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("限价单下单(submit_order)", lambda: broker.submit_order(_make_order(order_type=OrderType.LIMIT, price=9.5)),
               threshold_ms=EXECUTION_LATENCY_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_cancel_order_latency():
    """撤单延迟（先下限价单，再撤单）"""
    broker = _make_broker()
    # 预先提交一批限价单用于撤单测试
    pending_orders: List[str] = []
    for _ in range(BENCH_ITERATIONS + BENCH_WARMUP + 10):
        o = broker.submit_order(_make_order(order_type=OrderType.LIMIT, price=9.0))
        pending_orders.append(o.order_id)

    idx = [0]

    def _cancel():
        oid = pending_orders[idx[0]]
        idx[0] += 1
        broker.cancel_order(oid)

    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("撤单(cancel_order)", _cancel, threshold_ms=EXECUTION_LATENCY_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_get_order_latency():
    """查单延迟"""
    broker = _make_broker()
    o = broker.submit_order(_make_order(order_type=OrderType.LIMIT, price=9.0))
    oid = o.order_id
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("查单(get_order)", lambda: broker.get_order(oid), threshold_ms=EXECUTION_LATENCY_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_get_positions_latency():
    """查持仓延迟"""
    broker = _make_broker()
    # 先成交一些持仓
    for i in range(10):
        broker.submit_order(_make_order(symbol=f"POS{i}.SZ", order_type=OrderType.MARKET, quantity=100))
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("查持仓(get_positions)", lambda: broker.get_positions(), threshold_ms=EXECUTION_LATENCY_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_get_account_latency():
    """查账户延迟"""
    broker = _make_broker()
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("查账户(get_account)", lambda: broker.get_account(), threshold_ms=EXECUTION_LATENCY_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


def test_get_active_orders_latency():
    """查活动订单延迟"""
    broker = _make_broker()
    for i in range(20):
        broker.submit_order(_make_order(symbol=f"ACT{i}.SZ", order_type=OrderType.LIMIT, price=9.0))
    runner = BenchmarkRunner(warmup=BENCH_WARMUP, iterations=BENCH_ITERATIONS)
    runner.run("查活动订单(get_active_orders)", lambda: broker.get_active_orders(),
               threshold_ms=EXECUTION_LATENCY_MS)
    print(runner.results[-1].format())
    runner.assert_all_pass()


# ═══════════════════════════════════════════
#  独立运行入口
# ═══════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  执行层下单接口延迟基准测试")
    print("=" * 60)
    test_submit_market_order_latency()
    test_submit_limit_order_latency()
    test_cancel_order_latency()
    test_get_order_latency()
    test_get_positions_latency()
    test_get_account_latency()
    test_get_active_orders_latency()
    print("\n全部执行层延迟基准测试完成")
