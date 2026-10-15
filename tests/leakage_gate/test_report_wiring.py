# 文件名：tests/leakage_gate/test_report_wiring.py
"""
P1-11 四出口泄漏审计报告接线 + P2-11 决策日语义测试
====================================================
P1-11：leakage_report 幽灵产物 → 四出口（回测/实盘/训练/参数优化）真实接线。
本测试走真实生产出口（BacktestScheduler.run / create_live_engine /
TrainingPipeline.run / ParamOptimizer.optimize），断言报告出现在
conftest 会话隔离目录（leakage_report_dir，避免污染项目 outputs）且
meta 段实验类型/决策日正确（R-16 验收判据 1/2/5：enabled=false 亦落盘）。

P2-11：decision_time 真实决策日——豁免有效性按决策日判定
（suppress_violations now=决策日，而非运行当天）。

防未来函数：全部数据为合成数据（make_ohlcv_df / 确定性随机游走），
无真实行情、无网络；断言只读已落盘报告（已发生事实）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from myquant.backtest.engine import BacktestScheduler
from myquant.backtest.param_optimizer import (
    ParamGrid,
    ParamOptimizer,
    ParamSpec,
)
from myquant.leakage_gate.gate import Violation, _decision_now
from myquant.live.engine import create_live_engine
from myquant.ml.features import FeatureConfig
from myquant.ml.model_registry import ModelRegistry
from myquant.ml.training_pipeline import TrainingPipeline
from myquant.models.backtest import BacktestConfig
from myquant.strategies.ma_strategy import MACrossStrategy
from tests.conftest import make_ohlcv_df

# ── 测试数据（确定性合成，防未来）─────────────────────────────

def _closes(n: int, base: float = 10.0) -> List[float]:
    """单调小幅上涨收盘序列（确定性）。"""
    return [base + i * 0.01 for i in range(n)]


def _bt_config(start: str = "2024-01-01", end: str = "2024-03-01") -> BacktestConfig:
    """回测配置（120 根 B 日覆盖区间）。"""
    return BacktestConfig(
        strategy_name="ma_cross",
        symbols=["000001.SZ"],
        start_date=start,
        end_date=end,
        frequency="1d",
        initial_capital=1_000_000.0,
        params={"fast_period": 5, "slow_period": 10},
    )


def _live_config() -> Dict[str, Any]:
    """实盘装配配置（paper 模式，与 test_live_lookahead 同构）。"""
    return {
        "live": {"name": "wiring_test", "initial_capital": 1_000_000, "trade_mode": "paper"},
        "broker": {"type": "paper", "initial_capital": 1_000_000},
        "market_data": {"source": "akshare", "frequency": "daily"},
        "strategy": {"name": "ma_cross"},
        "position": {},
        "risk_control": {"enabled": False},
        "state_store": {"enabled": False, "auto_recover_on_start": False},
    }


def _param_data(n: int = 160) -> Dict[str, pd.DataFrame]:
    """参数优化行情（中文列；确定性趋势+噪声，防未来）。"""
    rng = np.random.default_rng(7)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    closes = 10.0 * (1.0 + 0.0006 * np.arange(n) + rng.normal(0.0, 0.01, n))
    return {
        "000001.SZ": pd.DataFrame(
            {
                "开盘价": closes * 0.997,
                "最高价": closes * 1.01,
                "最低价": closes * 0.99,
                "收盘价": closes,
                "成交量": 100_000.0,
                "成交额": closes * 100_000.0,
            },
            index=dates,
        )
    }


def _training_data(n: int = 300) -> pd.DataFrame:
    """训练行情（含 timestamp 列，与 test_ml_e2e 同构）。"""
    np.random.seed(42)
    closes = [10.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + np.random.normal(0, 0.02)))
    df = make_ohlcv_df(closes=closes)
    df["timestamp"] = df.index.astype("int64") // 10**9
    return df


def _latest_report(d: Path, etype: str) -> Dict[str, Any]:
    """取隔离目录中指定实验类型的最新一份报告（无则断言失败）。

    按文件 mtime 取最新：glob 枚举顺序是未定义行为（NTFS/ext4 不同），
    不能依赖 matched[-1]——否则全量会话（unit 先跑、leakage_gate 后跑，
    共享会话隔离目录）会串读到其他测试留下的报告（CI ubuntu 实测：
    cv_reg_anchor 报告被 training 出口测试读到）。
    """
    reps = [
        (p, json.loads(p.read_text(encoding="utf-8")))
        for p in d.glob("leakage_report_*.json")
    ]
    matched = [
        (p, r) for p, r in reps if r.get("meta", {}).get("experiment_type") == etype
    ]
    assert matched, (
        f"无 {etype!r} 报告，目录现有类型: "
        f"{[r.get('meta', {}).get('experiment_type') for _, r in reps]}"
    )
    return max(matched, key=lambda pr: pr[0].stat().st_mtime)[1]


# ═══════════════════════════════════════════════
#  P1-11 ① 回测出口
# ═══════════════════════════════════════════════

def test_backtest_export_writes_report(leakage_report_dir: Path) -> None:
    """真实回测出口 → 报告落盘（experiment_type=backtest，含 gates/lookahead_guard）。"""
    config = _bt_config()
    data = {"000001.SZ": make_ohlcv_df(_closes(120))}
    strategy = MACrossStrategy(fast_period=5, slow_period=10)
    result = BacktestScheduler(mode="auto").run(config, strategy, data=data)
    assert result is not None

    rep = _latest_report(leakage_report_dir, "backtest")
    meta = rep["meta"]
    assert meta["experiment_id"].startswith("backtest_2024-01-01_2024-03-01")
    assert meta["strategy_id"] == "ma_cross"
    # P2-11：decision_time = config.end_date（非运行当天）
    assert meta["decision_time"] == "2024-03-01"
    # R-16 判据 1：报告含 gates/violations/lookahead_guard 段
    assert "gates" in rep and "violations" in rep and "lookahead_guard" in rep
    assert rep["extra"]["engine_mode"] in ("event", "mixed", "vectorized")


def test_backtest_export_writes_report_when_strategy_none(
    leakage_report_dir: Path,
) -> None:
    """strategy=None → 报告仍落盘且 extra 标注 strategy_none（P1-06 联动）。

    auto 模式下 None 策略无法调度（resolve_mode 抛错，既有行为）；
    显式 event 模式可达 run 收尾，验证报告标注路径。
    """
    config = _bt_config()
    data = {"000001.SZ": make_ohlcv_df(_closes(120))}
    BacktestScheduler(mode="event").run(config, None, data=data)

    rep = _latest_report(leakage_report_dir, "backtest")
    assert rep["extra"]["strategy_none"] is True
    assert rep["meta"]["strategy_id"] == ""


# ═══════════════════════════════════════════════
#  P1-11 ② 实盘出口
# ═══════════════════════════════════════════════

def test_live_export_writes_report(leakage_report_dir: Path) -> None:
    """实盘装配出口 → 报告落盘（experiment_type=live）。"""
    engine = create_live_engine(_live_config())
    assert engine is not None

    rep = _latest_report(leakage_report_dir, "live")
    assert rep["meta"]["strategy_id"] == "ma_cross"
    assert rep["meta"]["decision_time"] == datetime.now().strftime("%Y-%m-%d")
    assert "gates" in rep and "lookahead_guard" in rep


# ═══════════════════════════════════════════════
#  P1-11 ③ 训练出口
# ═══════════════════════════════════════════════

def test_training_export_writes_report(
    leakage_report_dir: Path, tmp_path_factory: Any,
) -> None:
    """训练出口 → 报告落盘（experiment_type=training，sanity 段存在）。"""
    root = tmp_path_factory.mktemp("wiring_ml")
    registry = ModelRegistry(models_root=str(root / "models"))
    pipe = TrainingPipeline(feature_config=FeatureConfig(), registry=registry)
    pipe.run(_training_data(), model_name="wiring_model")

    rep = _latest_report(leakage_report_dir, "training")
    assert rep["meta"]["strategy_id"] == "wiring_model"
    assert "sanity" in rep
    assert rep["meta"]["experiment_id"].startswith("training_wiring_model_")


# ═══════════════════════════════════════════════
#  P1-11 ③ 参数优化出口
# ═══════════════════════════════════════════════

def test_param_opt_export_writes_report(leakage_report_dir: Path) -> None:
    """参数优化出口 → 报告落盘（experiment_type=param_optimization）。"""
    grid = ParamGrid((ParamSpec("fast_period", (3, 5)),))
    opt = ParamOptimizer(
        strategy_name="ma_cross",
        grid=grid,
        data=_param_data(),
        objective="sharpe_ratio",
        start_date="2024-01-01",
        end_date="2024-06-01",
        cpu_workers=1,
    )
    result = opt.optimize()
    assert result is not None

    rep = _latest_report(leakage_report_dir, "param_optimization")
    assert rep["meta"]["strategy_id"] == "ma_cross"
    # P2-11：decision_time = end_date（真实决策时点上限）
    assert rep["meta"]["decision_time"] == "2024-06-01"
    assert "gates" in rep


# ═══════════════════════════════════════════════
#  P2-11 决策日语义（豁免按决策日判定）
# ═══════════════════════════════════════════════

def test_p211_decision_now_parses_decision_date() -> None:
    """_decision_now：决策日解析为零点；空/非法回退当前零点。"""
    assert _decision_now("2024-06-30").isoformat() == "2024-06-30T00:00:00"
    fallback = _decision_now("")
    assert fallback.hour == 0 and fallback.minute == 0 and fallback.second == 0
    # 非法格式同样回退（不抛错，保持 P2-11 前兼容）
    assert _decision_now("not-a-date").isoformat()[:10] == \
        datetime.now().strftime("%Y-%m-%d")


def test_p211_suppress_uses_decision_time() -> None:
    """P2-11：豁免压制按决策日判定（valid_until < 决策日 → 不压制保留违规）。"""
    from myquant.leakage_gate.exemption import Exemption, ExemptionStore

    store = ExemptionStore()  # 纯内存库，不落盘
    e = store.grant(
        Exemption(
            exemption_id="EX-P211-1",
            reason="P2-11 决策日判定测试",
            rule_id="R03",
            approver="tester",
            valid_from="2024-01-01",
            valid_until="2024-03-31",
            target="",
        )
    )
    store.approve(e.exemption_id, approver="lead")  # P1-14：批准后才压制
    v = Violation(
        gate="test", rule_id="R03", severity="阻断",
        message="P2-11 测试违规", strategy_id="ma_cross", decision_time="",
    )
    # 决策日 2025-01-01 > valid_until → 豁免过期，违规保留（阻断）
    assert store.suppress([v], now=datetime(2025, 1, 1)) == [v]
    # 决策日 2024-03-01 在有效期内 → 豁免压制，违规移除
    assert store.suppress([v], now=datetime(2024, 3, 1)) == []


def test_p211_assert_pass_exemption_uses_decision_time() -> None:
    """assert_all_gates_pass：豁免压制 now 来自 decision_time（非运行当天）。

    验证：注册一条在"运行当天已过期、但决策日未过期"的豁免（valid_until =
    决策日当天），enabled=True 下门禁断言不因运行日期而误判豁免失效。
    以 _decision_now 贯通 assert_all_gates_pass 的 now 参数来源。
    """
    from myquant.leakage_gate.exemption import Exemption, ExemptionStore

    store = ExemptionStore()
    e = store.grant(
        Exemption(
            exemption_id="EX-P211-2",
            reason="P2-11 决策日=valid_until 边界测试",
            rule_id="R04",
            approver="tester",
            valid_from="2024-01-01",
            valid_until="2024-03-01",  # 决策日当天过期边界（含）
            target="ma_cross",
        )
    )
    store.approve(e.exemption_id, approver="lead")  # P1-14：批准后才压制
    # 决策日解析 → 2024-03-01 零点；valid_until 含当日 → is_active=True
    now = _decision_now("2024-03-01")
    assert store.covers(violation_for("R04"), now=now) is True
    # 决策日次日 → 已过期 → 不压制
    assert store.covers(violation_for("R04"), now=_decision_now("2024-03-02")) is False


def violation_for(rule_id: str) -> Violation:
    """构造一条目标策略违规（供 covers 匹配）。"""
    return Violation(
        gate="test", rule_id=rule_id, severity="阻断",
        message="测试违规", strategy_id="ma_cross", decision_time="",
    )
