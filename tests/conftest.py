# 文件名：tests/conftest.py
"""
conftest.py - pytest 全局配置
==============================
通过 pytest_plugins 加载 fixtures 目录下的所有 fixture 模块；
并提供统一的中文列 OHLCV DataFrame 构造工厂 make_ohlcv_df
（#38 中文列改造后，单元测试统一产出中文列行情，禁止再喂英文列）。
"""

import sys
from pathlib import Path
from typing import Any, Optional, Sequence, Union

import pytest

import numpy as np
import pandas as pd

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 加载 fixtures
pytest_plugins = [
    "tests.fixtures.market_data",
    "tests.fixtures.strategies",
    "tests.fixtures.live_env",
    "tests.fixtures.database",
]


def backtest_broker_factory(config: Any) -> Any:
    """G1 EX-09A：撮合执行器工厂（模块级，Windows spawn 下可 pickle）。

    测试会话与进程池子进程（ParamOptimizer/BatchBacktester 并行评估）共用；
    tests 层引用 SimBroker 属合法装配（不在 myquant 分层违规扫描内）。
    """
    from myquant.execution.sim_broker import SimBroker

    _bcfg = config.to_dict()
    return SimBroker(
        initial_capital=config.initial_capital,
        commission_model=config.commission,
        slippage_model=config.slippage,
        price_limit_pct=float(_bcfg.get("price_limit_pct", 0.10)),
        enforce_limit=bool(_bcfg.get("enforce_limit", False)),
        is_suspended=None,
    )


@pytest.fixture(scope="session", autouse=True)
def _backtest_broker_factory_registered() -> None:
    """G1 EX-09A：测试会话装配回测撮合执行器工厂。

    BacktestEngine 不再顶层 import execution 层（L3→L4 反向）；tests 直接构造
    BacktestEngine 的文件（test_backtest.py 等）依赖本注册保持可用。
    """
    from myquant.backtest.engine import register_broker_factory

    register_broker_factory(backtest_broker_factory)


# ═══════════════════════════════════════════════════════════════
#  统一中文列 OHLCV 构造工厂（#38 中文列规范）
# ═══════════════════════════════════════════════════════════════

def make_ohlcv_df(
    closes: Sequence[float],
    volumes: Optional[Union[Sequence[float], float]] = None,
    opens: Optional[Union[Sequence[float], float]] = None,
    highs: Optional[Union[Sequence[float], float]] = None,
    lows: Optional[Union[Sequence[float], float]] = None,
    amounts: Optional[Union[Sequence[float], float]] = None,
    start: str = "2024-01-01",
    freq: str = "B",
    index_name: str = "日期",
) -> pd.DataFrame:
    """构造中文列 OHLCV DataFrame（#38 中文列改造后的统一测试数据源）。

    未显式提供的列按收盘价推导：open=close*0.995、high=close*1.015、
    low=close*0.985、volume=100000.0、amount=close*volume；
    标量参数自动广播到每根 K 线；closes 为空时返回带中文列名的空 DataFrame。

    Args:
        closes: 收盘价序列（必填）。
        volumes: 成交量序列或标量，默认 100000.0。
        opens / highs / lows: 序列或标量，默认按收盘价推导。
        amounts: 成交额序列或标量，默认 close*volume。
        start: 起始日期（默认 2024-01-01）。
        freq: 日期频率（默认 B=工作日，与回测数据一致）。
        index_name: 索引名，默认「日期」（#38 规范）。

    Returns:
        中文列 OHLCV DataFrame，index=DatetimeIndex。
    """
    n = len(closes)
    dates = pd.date_range(start, periods=n, freq=freq)

    def _broadcast(values: Optional[Union[Sequence[float], float]],
                   default: Sequence[float]) -> list:
        """标量广播为 n 元素列表；None 使用默认推导。"""
        if values is None:
            return list(default)
        if np.isscalar(values):
            return [values] * n
        return list(values)

    data: dict[str, Any] = {
        "开盘价": list(closes),
        "最高价": _broadcast(highs, [c * 1.015 for c in closes]),
        "最低价": _broadcast(lows, [c * 0.985 for c in closes]),
        "收盘价": list(closes),
        "成交量": _broadcast(volumes, [100000.0] * n),
    }
    # 成交额列始终保留（close*volume 推导；n=0 时为空列表）
    volumes_final = data["成交量"]
    data["成交额"] = _broadcast(
        amounts, [c * v for c, v in zip(closes, volumes_final)]
    )

    df = pd.DataFrame(data, index=dates)
    df.index.name = index_name
    return df
