# 文件名：tests/unit/test_live_warmup.py
"""
test_live_warmup.py - 实盘引擎本地parquet历史预热 + context.data中文列测试
==============================================================================
覆盖 myquant/live/engine.py 的预热增强（回写记录 #55）：
  1. _load_local_history    — 从本地 datasets/processed 日线parquet读取历史bar
                             （离线秒级，升序、限最近count根；缺失/损坏/缺列→空列表）
  2. _sync_context_data     — 输出的 context.data 列名为系统规范中文列
                             （开盘价/最高价/最低价/收盘价/成交量/成交额，与回测同构，铁律2）
  3. _warmup_history        — daily 频率优先本地parquet预热，填充 bar_history + context.data
  4. 降级                    — 本地无数据时回退 _fetch_history_bars（akshare）

回归依据: 交接#52 待办第3项 paper 仿真；3226只走 akshare 预热过慢（173s仅332根），
         改为本地parquet离线预热（秒级）再对最新bar走 akshare。
运行方式: pytest tests/unit/test_live_warmup.py -v
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from myquant.live.engine import LiveEngine, RiskConfig
from myquant.strategies.base import StrategyContext

_CHINESE_COLS = ["开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"]


def _make_local_parquet(tmp_path, symbol: str = "000001.SZ", n: int = 4) -> str:
    """写入一个含中文列 + 日期(int) 的日线 parquet，返回文件路径。"""
    df = pd.DataFrame({
        "日期": [20240102 + i for i in range(n)],
        "开盘价": [10.0 + 0.1 * i for i in range(n)],
        "最高价": [10.5 + 0.1 * i for i in range(n)],
        "最低价": [9.9 + 0.1 * i for i in range(n)],
        "收盘价": [10.2 + 0.1 * i for i in range(n)],
        "成交量": [1000 + 100 * i for i in range(n)],
        "成交额": [10000 + 1000 * i for i in range(n)],
    })
    path = tmp_path / f"{symbol}.parquet"
    df.to_parquet(path)
    return str(path)


def _make_engine(tmp_path) -> LiveEngine:
    """最小引擎（关闭全部保障组件，仅驱动预热相关方法）。"""
    engine = LiveEngine(
        engine_id="warmup_test",
        risk_config=RiskConfig(max_positions=10, max_position_pct=0.5),
        enable_risk_manager=False,
        enable_guardian=False,
        enable_state_store=False,
        enable_scheduler=False,
        enable_event_bus=False,
    )
    engine._local_history_dir = str(tmp_path)
    engine._context = StrategyContext()
    engine._warmup_bars = 60
    engine._data_feed = None  # 无行情源时 freq 默认 'daily'
    return engine


class TestLoadLocalHistory:
    """#55: _load_local_history 本地parquet读取"""

    def test_读取中文列并升序限count(self, tmp_path) -> None:
        _make_local_parquet(tmp_path)
        engine = _make_engine(tmp_path)
        bars = engine._load_local_history("000001.SZ", 60)
        assert len(bars) == 4
        # 升序：首根日期最早、收盘价最低
        assert bars[0]["date"] == "2024-01-02"
        assert bars[0]["close"] == pytest.approx(10.2)
        assert bars[-1]["close"] == pytest.approx(10.5)
        # 含 timestamp，供 _dict_to_bar / _sync_context_data 使用
        assert "timestamp" in bars[0]
        assert isinstance(bars[0]["timestamp"], float)

    def test_count限制仅返回最近N根(self, tmp_path) -> None:
        _make_local_parquet(tmp_path, n=6)
        engine = _make_engine(tmp_path)
        bars = engine._load_local_history("000001.SZ", 3)
        assert len(bars) == 3
        assert bars[0]["date"] == "2024-01-05"  # 跳过前3根

    def test_文件缺失返回空(self, tmp_path) -> None:
        engine = _make_engine(tmp_path)
        assert engine._load_local_history("999999.SZ", 60) == []

    def test_缺必要列返回空(self, tmp_path) -> None:
        (tmp_path / "000001.SZ.parquet").write_bytes(b"not parquet")
        engine = _make_engine(tmp_path)
        # 损坏parquet读取失败 → 空列表（降级akshare）
        assert engine._load_local_history("000001.SZ", 60) == []


class TestSyncContextDataChinese:
    """#55: _sync_context_data 输出中文列（与回测 context.data 同构，铁律2）"""

    def test_warmup后context_data为中文列(self, tmp_path) -> None:
        _make_local_parquet(tmp_path)
        engine = _make_engine(tmp_path)
        engine._warmup_history(["000001.SZ"], count=60)
        assert len(engine._bar_history["000001.SZ"]) == 4
        df = engine._context.data["000001.SZ"]
        assert list(df.columns) == _CHINESE_COLS
        assert len(df) == 4


class TestWarmupHistory:
    """#55: daily 优先本地parquet预热"""

    def test_daily优先本地填充history(self, tmp_path) -> None:
        _make_local_parquet(tmp_path)
        engine = _make_engine(tmp_path)
        engine._warmup_history(["000001.SZ"])
        assert len(engine._bar_history["000001.SZ"]) == 4

    def test_本地为空降级akshare(self, tmp_path, monkeypatch) -> None:
        engine = _make_engine(tmp_path)  # tmp 下无 parquet → 本地返回空
        fetched = []

        def fake_fetch(symbol, count, freq):
            fetched.append(symbol)
            ts = datetime(2024, 1, 2).timestamp()
            return [{"symbol": symbol, "date": "2024-01-02", "open": 10.0,
                     "high": 10.5, "low": 9.9, "close": 10.2, "volume": 1000,
                     "amount": 10000, "timestamp": ts}]

        monkeypatch.setattr(engine, "_fetch_history_bars", fake_fetch)
        engine._warmup_history(["000001.SZ"], count=60)
        assert fetched == ["000001.SZ"]  # 降级路径被调用
        assert len(engine._bar_history["000001.SZ"]) == 1
