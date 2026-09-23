# 文件名：tests/unit/test_acceptance.py
"""仿真绩效准入评估器（SimulationAcceptanceEvaluator）单元测试。

覆盖 myquant/live/acceptance.py：
  - 全达标样本判定
  - 回测基线比较（年化/回撤「不显著劣于回测」）
  - 未达标项判定与综合结论
  - 数据缺失→待补充(NA)，不误判通过
  - 阈值覆盖
  - 非法输入校验
"""

from __future__ import annotations

import pytest

from myquant.live.acceptance import (
    AcceptanceStatus,
    SimulationAcceptanceEvaluator,
)


def _sample() -> dict:
    """构造全达标样本。"""
    return {
        "run_days": 45,
        "annualized_return": 0.12,
        "max_drawdown": 0.07,
        "sharpe_ratio": 1.4,
        "avg_slippage": 0.0002,
        "missed_orders": 0,
        "major_risk_violations": 0,
        "interruptions": 1,
    }


def _backtest() -> dict:
    return {"annualized_return": 0.15, "max_drawdown": 0.06}


# ── 全达标 ──

def test_all_pass() -> None:
    """全达标样本：所有项 PASS，综合通过。"""
    report = SimulationAcceptanceEvaluator(
        _sample(), backtest=_backtest(), run_mode="paper"
    ).evaluate()
    assert report.overall is True
    assert report.failed == 0
    assert report.pending == 0
    assert report.passed == 8
    assert all(i.status is AcceptanceStatus.PASS for i in report.items)


# ── 未达标判定 ──

def test_fail_sharpe_and_slippage() -> None:
    """夏普过低、滑点过高 → 对应项 FAIL，综合不通过。"""
    sample = _sample()
    sample["sharpe_ratio"] = 0.5
    sample["avg_slippage"] = 0.002
    report = SimulationAcceptanceEvaluator(sample, backtest=_backtest()).evaluate()
    assert report.overall is False
    by_key = {i.key: i for i in report.items}
    assert by_key["sharpe_ratio"].status is AcceptanceStatus.FAIL
    assert by_key["avg_slippage"].status is AcceptanceStatus.FAIL


def test_fail_risk_violations() -> None:
    """重大风控违规 >0 → FAIL。"""
    sample = _sample()
    sample["major_risk_violations"] = 2
    report = SimulationAcceptanceEvaluator(sample).evaluate()
    by_key = {i.key: i for i in report.items}
    assert by_key["major_risk_violations"].status is AcceptanceStatus.FAIL
    assert report.overall is False


def test_fail_drawdown_exceeds_backtest() -> None:
    """回撤超过回测×1.3 → FAIL（默认 drawdown_ratio_limit=1.3）。"""
    sample = _sample()
    # 回测回撤 0.06，×1.3=0.078，样本回撤 0.10 超标
    sample["max_drawdown"] = 0.10
    report = SimulationAcceptanceEvaluator(sample, backtest=_backtest()).evaluate()
    by_key = {i.key: i for i in report.items}
    assert by_key["max_drawdown"].status is AcceptanceStatus.FAIL


def test_fail_annualized_inferior_to_backtest() -> None:
    """年化收益显著劣于回测 → FAIL（低于回测 - gap）。"""
    sample = _sample()
    # 回测年化 0.15，gap=0.05 → 阈值 0.10；样本 0.03 劣于
    sample["annualized_return"] = 0.03
    report = SimulationAcceptanceEvaluator(sample, backtest=_backtest()).evaluate()
    by_key = {i.key: i for i in report.items}
    assert by_key["annualized_return"].status is AcceptanceStatus.FAIL


# ── 数据缺失 ──

def test_missing_data_is_na_not_pass() -> None:
    """关键绩效字段缺失 → NA，综合不通过（不误判）。"""
    sample = {"run_days": 40}  # 仅运行时长
    report = SimulationAcceptanceEvaluator(sample).evaluate()
    assert report.overall is False
    assert report.passed == 1  # 仅运行时长一项有结论且 PASS
    assert report.pending == 7  # 其余 7 项数据不足
    assert all(
        i.status is AcceptanceStatus.NA
        for i in report.items if i.key != "run_days"
    )


# ── 阈值覆盖 ──

def test_threshold_override() -> None:
    """阈值覆盖生效：sharpe_min=1.5 使原 1.4 判 FAIL。"""
    sample = _sample()
    report = SimulationAcceptanceEvaluator(
        sample, thresholds={"sharpe_min": 1.5}
    ).evaluate()
    by_key = {i.key: i for i in report.items}
    assert by_key["sharpe_ratio"].status is AcceptanceStatus.FAIL


def test_threshold_override_loosen() -> None:
    """阈值放宽：sharpe_min=1.0 原 1.4 判 PASS。"""
    sample = _sample()
    report = SimulationAcceptanceEvaluator(
        sample, thresholds={"sharpe_min": 1.0}
    ).evaluate()
    by_key = {i.key: i for i in report.items}
    assert by_key["sharpe_ratio"].status is AcceptanceStatus.PASS


# ── 非法输入 ──

def test_invalid_metrics() -> None:
    """metrics 非 dict 或空 → ValueError。"""
    with pytest.raises(ValueError):
        SimulationAcceptanceEvaluator([])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SimulationAcceptanceEvaluator({})


def test_invalid_thresholds() -> None:
    """thresholds 非 dict → ValueError。"""
    with pytest.raises(ValueError):
        SimulationAcceptanceEvaluator(_sample(), thresholds=[1, 2])  # type: ignore[arg-type]


# ── 序列化 ──

def test_report_serialization() -> None:
    """to_dict / to_text 结构可用且含结论。"""
    report = SimulationAcceptanceEvaluator(_sample(), backtest=_backtest()).evaluate()
    d = report.to_dict()
    assert d["overall"] is True
    assert len(d["items"]) == 8
    text = report.to_text()
    assert "结论：" in text
    assert "通过" in text
